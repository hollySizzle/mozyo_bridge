from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

from mozyo_bridge.domain.agent_discovery import (
    CONFIDENCE_STRONG,
    ROLE_SOURCE_PANE_OPTION,
    resolve_agent_role,
)
from mozyo_bridge.infrastructure.tmux_client import (
    pane_lines,
    resolve_pane_id,
    run_tmux,
    validate_target,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import READ_MARK_PREFIX


AGENT_PROCESSES = {"claude", "codex", "node"}
AGENT_COMMANDS = {
    "claude": "claude",
    "codex": "codex",
}
AGENT_LABELS = frozenset(AGENT_COMMANDS)
VERSIONED_NATIVE_BINARY_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+].*)?")
READ_MARK_TTL_SECONDS = 300


def read_mark_path(pane_id: str) -> Path:
    return Path(f"{READ_MARK_PREFIX}{pane_id.replace('%', '_')}")


def mark_read(pane_id: str) -> None:
    payload = {
        "pane_id": pane_id,
        "sender_pane": os.environ.get("TMUX_PANE", ""),
        "created_at": time.time(),
    }
    read_mark_path(pane_id).write_text(json.dumps(payload), encoding="utf-8")


def require_read(pane_id: str) -> None:
    path = read_mark_path(pane_id)
    if not path.exists():
        die(f"must read target before interacting: mozyo-bridge read {pane_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        clear_read(pane_id)
        die(f"stale read marker for {pane_id}; read target again before interacting")
    if payload.get("pane_id") != pane_id:
        clear_read(pane_id)
        die(f"read marker target mismatch for {pane_id}; read target again before interacting")
    created_at = payload.get("created_at")
    if not isinstance(created_at, (int, float)) or time.time() - created_at > READ_MARK_TTL_SECONDS:
        clear_read(pane_id)
        die(f"read marker expired for {pane_id}; read target again before interacting")


def clear_read(pane_id: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        read_mark_path(pane_id).unlink()


def is_tmux_target(target: str) -> bool:
    return target.startswith("%") or ":" in target or "." in target


def current_pane() -> str:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        die("TMUX_PANE is not set; run from inside tmux for this command")
    return pane


def current_session_name() -> str | None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    result = run_tmux("display-message", "-t", pane, "-p", "#{session_name}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _active_or_first(panes: list[dict[str, str]]) -> dict[str, str]:
    """The active pane among ``panes``, else the first (split-window tie-break)."""
    for pane in panes:
        if pane.get("pane_active") == "1":
            return pane
    return panes[0]


def _pane_lane_identity(pane: dict[str, str]) -> tuple[str, str]:
    """The ``(workspace_id, lane_id)`` a pane belongs to (Redmine #11820).

    The lane normalizes an empty / missing ``@mozyo_lane_id`` to the
    backward-compatible ``default`` lane, matching the compact-discovery
    projection so the two surfaces agree on lane identity.
    """
    workspace_id = (pane.get("workspace_id") or "").strip()
    lane_id = (pane.get("lane_id") or "").strip() or "default"
    return workspace_id, lane_id


def _has_concrete_lane_identity(workspace_id: str, lane_id: str) -> bool:
    """True when a pane carries enough identity to narrow same-lane on (#12011).

    A pane with no workspace marker that sits in only the backward-compatible
    ``default`` lane — a normal-``mozyo`` window, or any pane the cockpit never
    stamped — has nothing to disambiguate against, so same-lane narrowing stays
    off and the caller keeps its existing fail-closed behavior.
    """
    return bool(workspace_id) or lane_id != "default"


def _optional_current_pane_id() -> str | None:
    """The sender's ``TMUX_PANE`` id, or ``None`` outside tmux (best-effort)."""
    return os.environ.get("TMUX_PANE") or None


def _sender_pane(panes: list[dict[str, str]]) -> dict[str, str] | None:
    """The sender's own pane within ``panes`` (live tmux snapshot), if known."""
    pane_id = _optional_current_pane_id()
    if not pane_id:
        return None
    return next((pane for pane in panes if pane.get("id") == pane_id), None)


def narrow_to_sender_lane(
    targets: list[dict[str, str]],
    sender: dict[str, str] | None,
) -> list[dict[str, str]]:
    """Narrow agent-pane candidates to the sender's own workspace + lane (#12011).

    Returns the subset of ``targets`` that share the sender pane's
    ``(workspace_id, lane_id)`` identity. This is **same-lane addressing only**:
    it can only shrink the candidate set and never selects a pane outside the
    sender's own lane, so it cannot cross the lane governance boundary
    (``vibes/docs/logics/cockpit-sublane-operating-model.md`` "Cross-Lane
    Routing Rule") — a cross-lane handoff still has to be addressed explicitly
    through the target lane's Codex gateway. It returns ``targets`` unchanged,
    leaving the caller's fail-closed ambiguity handling intact, when the sender
    pane is unknown or carries no concrete lane identity to match on. Live tmux
    stays the identity source: both the sender's and the candidates' lanes come
    from the ``@mozyo_*`` pane options in the snapshot, never a pane title.
    """
    if sender is None:
        return targets
    sender_identity = _pane_lane_identity(sender)
    if not _has_concrete_lane_identity(*sender_identity):
        return targets
    return [pane for pane in targets if _pane_lane_identity(pane) == sender_identity]


def _format_agent_candidate(pane: dict[str, str]) -> str:
    """One ``%pane (workspace=..., lane=...)`` row for the fail-closed message."""
    workspace_id, lane_id = _pane_lane_identity(pane)
    lane_label = (pane.get("lane_label") or "").strip()
    pane_id = pane.get("id") or pane.get("location") or "?"
    return (
        f"{pane_id} (workspace={workspace_id or '<none>'}, "
        f"lane={lane_label or lane_id})"
    )


def _ambiguous_agent_targets_message(
    agent: str,
    session: str,
    targets: list[dict[str, str]],
    sender: dict[str, str] | None,
) -> str:
    """Fail-closed guidance naming the candidates, the reason, and the retry.

    Same-lane narrowing (#12011) could not pick a unique target, so surface the
    concrete candidate identities, *why* the sender lane did not resolve them,
    and the explicit ``--target %pane`` override rather than guessing.
    """
    candidates = "; ".join(
        _format_agent_candidate(pane)
        for pane in sorted(targets, key=lambda pane: pane.get("id") or "")
    )
    if sender is None:
        sender_clause = (
            "the sender pane is unknown (run from inside the lane's pane), so "
            "same-lane resolution could not narrow the candidates"
        )
    else:
        sender_ws, sender_lane = _pane_lane_identity(sender)
        sender_lane_label = (sender.get("lane_label") or "").strip() or sender_lane
        if not _has_concrete_lane_identity(sender_ws, sender_lane):
            sender_clause = (
                "the sender pane carries no workspace/lane identity "
                "(workspace=<none>, lane=default), so same-lane resolution could "
                "not narrow the candidates"
            )
        else:
            sender_clause = (
                f"the sender lane (workspace={sender_ws or '<none>'}, "
                f"lane={sender_lane_label}) matched no unique same-lane "
                f"'{agent}' pane among the candidates"
            )
    return (
        f"multiple '{agent}' panes found in session '{session}': {candidates}. "
        f"{sender_clause}. Name the exact pane with `--target %pane` "
        "(see `mozyo-bridge agents targets` for the candidate identities)."
    )


def find_agent_window(agent: str, session: str) -> dict[str, str] | None:
    """Resolve the pane in ``session`` whose *resolved role* is ``agent``.

    Runtime resolver for agent identity under the unified role model (Redmine
    #11822). A pane's role is decided by :func:`resolve_agent_role` over its
    runtime facts, so this matches both the normal-``mozyo`` rail (role on the
    ``<agent>``-named window) and a cockpit pane (role on the
    ``@mozyo_agent_role`` option, window named ``cockpit``). Only *strong*,
    non-ambiguous matches count — a weak process hint or a pane/window signal
    conflict never auto-targets. Returns ``None`` when nothing in ``session``
    resolves to ``agent``.

    Fails closed on more than one distinct logical target, *after* attempting
    same-lane narrowing (Redmine #12011). A window-named match collapses its
    split panes to one target (the active pane); cockpit packs several agents
    into one window, so each pane-option match is its own target. When more than
    one distinct target survives, the sender's own ``(workspace_id, lane_id)``
    narrows the set to its same-lane pane — a cockpit hosting several lanes
    auto-resolves ``--to codex`` to the sender lane's Codex gateway without an
    explicit ``--target``. That is same-lane addressing only and never crosses a
    lane boundary. If the sender lane still does not pick a unique pane (sender
    unknown / no lane identity / no or several same-lane matches) the resolver
    dies with the concrete candidates rather than picking one silently — tmux
    tolerates the duplication, so resolver safety has to fail closed.
    """
    panes = pane_lines()
    window_groups: dict[str, list[dict[str, str]]] = {}
    option_panes: list[dict[str, str]] = []
    for pane in panes:
        location = pane.get("location") or ""
        if location.split(":", 1)[0] != session:
            continue
        resolution = resolve_agent_role(
            pane_option_role=pane.get("agent_role"),
            window_name=pane.get("window_name"),
            process=pane.get("command"),
        )
        if (
            resolution.role != agent
            or resolution.confidence != CONFIDENCE_STRONG
            or resolution.ambiguous
        ):
            continue
        if resolution.role_source == ROLE_SOURCE_PANE_OPTION:
            option_panes.append(pane)
        else:
            window_index = (
                location.split(":", 1)[1].split(".", 1)[0] if ":" in location else ""
            )
            window_groups.setdefault(window_index, []).append(pane)

    targets: list[dict[str, str]] = [
        _active_or_first(panes) for panes in window_groups.values()
    ]
    seen_ids = {target.get("id") for target in targets}
    for pane in option_panes:
        if pane.get("id") not in seen_ids:
            targets.append(pane)
            seen_ids.add(pane.get("id"))

    if not targets:
        return None
    if len(targets) > 1:
        # Same-lane narrowing (Redmine #12011): a multi-lane cockpit resolves
        # `--to codex` with no explicit `--target` to the sender lane's own
        # gateway. Same-lane addressing only — never a foreign lane's pane.
        sender_pane = _sender_pane(panes)
        narrowed = narrow_to_sender_lane(targets, sender_pane)
        if len(narrowed) == 1:
            return narrowed[0]
        die(_ambiguous_agent_targets_message(agent, session, targets, sender_pane))
    return targets[0]


def resolve_agent_label(agent: str, session: str | None) -> dict[str, str] | None:
    """Resolve an agent label to its target pane under the window-only model.

    Thin wrapper over :func:`find_agent_window`. There is no compatibility
    fallback; cross-session resolution stays explicitly absent — it was the
    documented mis-route root cause (task 1214743574772820 comment
    1214746077864452).
    """
    if not session:
        return None
    return find_agent_window(agent, session)


def resolve_target(target: str) -> str:
    """Resolve a CLI ``--target`` to a tmux pane id (`%n`).

    Every branch returns a pane id: downstream consumers (notably
    :func:`pane_info`) match the result against ``pane_lines()`` ids, so a
    location form like ``session:window`` must be normalized here instead of
    being passed through — passing the raw location made every location
    target die with ``pane disappeared after resolve`` (Redmine #11666).
    A window-level location resolves to that window's active pane, matching
    tmux's own addressing and the queue-enter rail's active-split preflight.
    """
    if is_tmux_target(target):
        validate_target(target)
        if target.startswith("%"):
            return target
        return resolve_pane_id(target)
    if target not in AGENT_LABELS:
        die(
            f"unknown target '{target}'. Pass a tmux pane id (`%nnn`), a "
            "location (`session:window.pane`), or an agent label "
            f"({', '.join(sorted(AGENT_LABELS))})."
        )
    session = current_session_name()
    if not session:
        die(
            f"cannot resolve agent label '{target}' outside a tmux session; "
            "run from inside the repo session or pass an explicit tmux pane id"
        )
    pane = resolve_agent_label(target, session)
    if pane:
        return pane["id"]
    die(
        f"no {target} window found in session '{session}'. "
        f"Run `mozyo` to ensure the repo-scoped session, or `mozyo-bridge init {target}` "
        "on the right pane to rename its window."
    )
    raise AssertionError("unreachable")


def pane_info(target: str) -> dict[str, str]:
    pane_id = resolve_target(target)
    for pane in pane_lines():
        if pane["id"] == pane_id:
            return pane
    die(f"pane disappeared after resolve: {target}")
    raise AssertionError("unreachable")


def is_agent_process(command: str) -> bool:
    name = Path(command or "").name
    return name in AGENT_PROCESSES or VERSIONED_NATIVE_BINARY_RE.fullmatch(name) is not None


def is_receiver_agent_process(command: str, receiver: str) -> bool:
    """Per-receiver foreground process check for the relaxed `queue-enter` rail.

    Stricter than :func:`is_agent_process`. The contract
    (`vibes/docs/logics/tmux-send-safety-contract.md` v0.3,
    `### Per-Receiver Foreground Process Allowlist`) splits identity into:

    - **strong identity** — literal basename matches the named receiver
      (`claude` for receiver=`claude`; `codex` for receiver=`codex`). Cross-
      binding is fully detectable here: a literal `codex` process for
      receiver=`claude` (or vice versa) returns False.
    - **weak identity** — `node` literal or `VERSIONED_NATIVE_BINARY_RE`
      match. Both the Claude Code TUI and the Codex CLI are Node-based
      applications, so a `node` foreground process can belong to either
      receiver. Native distributions of either CLI surface as a versioned
      native binary basename. Both signals are therefore receiver-agnostic
      and only confirm the pane is running *some* agent runtime. Cross-
      binding protection in the weak case retreats to Step 9
      (`window_name == receiver`) plus Layer A operator discipline; closing
      the gap is tracked as Open Question 8 in the contract. Callers must
      not advertise stronger receiver-identity confidence than this
      function can give.

    Unknown receivers return False for the strong branch but still admit
    weak-branch matches (the weak branch is receiver-agnostic by design).
    Shells (e.g. `zsh`, `bash`) and empty commands return False.
    """
    name = Path(command or "").name
    if not name:
        return False
    if receiver == "claude" and name == "claude":
        return True
    if receiver == "codex" and name == "codex":
        return True
    # Weak identity branch: `node` literal and versioned native binary
    # basenames are receiver-agnostic. See docstring; do not pretend either
    # confirms receiver identity. Cross-binding protection here retreats to
    # Step 9 (window-name binding) plus Layer A operator discipline.
    if name == "node":
        return True
    if VERSIONED_NATIVE_BINARY_RE.fullmatch(name):
        return True
    return False


def ensure_agent_target(pane: dict[str, str], expected_agent: str, force: bool = False) -> None:
    """Confirm `pane` belongs to `expected_agent` under the window-only model.

    Identity is established by the resolver (the pane came out of the
    `<agent>`-named window). This guard only verifies that the pane is
    actually running an agent process, so a stray `zsh` or `bash` pane that
    accidentally lives inside a `claude` / `codex` window does not get
    notification input. `--force` lets the operator override for explicit
    out-of-band sends.
    """
    if force:
        return
    command = Path(pane.get("command") or "").name
    if is_agent_process(command):
        return
    die(
        "target pane does not look like an agent pane; "
        f"process={command or '-'} expected_agent={expected_agent}. "
        "Use --force only for an explicit operator-approved send."
    )
