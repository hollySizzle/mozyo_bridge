from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

from mozyo_bridge.infrastructure.tmux_client import pane_lines, run_tmux, validate_target
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


def find_agent_window(agent: str, session: str) -> dict[str, str] | None:
    """Return the (active) pane of the agent-named window in ``session``.

    Sole runtime resolver for agent identity under the window-only model. A
    tmux window whose ``window_name`` equals ``agent`` is the authoritative
    target; the previous ``@agent_name`` user-option fallback has been retired
    (Asana task 1214759644692283). Returns ``None`` when no window in
    ``session`` matches. Hard-errors when more than one window in ``session``
    is named ``agent`` — tmux tolerates duplicates silently, so resolver
    safety has to fail closed.
    """
    windows: dict[str, list[dict[str, str]]] = {}
    for pane in pane_lines():
        location = pane.get("location") or ""
        if location.split(":", 1)[0] != session:
            continue
        if pane.get("window_name") != agent:
            continue
        window_index = location.split(":", 1)[1].split(".", 1)[0] if ":" in location else ""
        windows.setdefault(window_index, []).append(pane)
    if not windows:
        return None
    if len(windows) > 1:
        labels = ", ".join(f"{session}:{idx}" for idx in sorted(windows.keys()))
        die(f"multiple windows named '{agent}' found in session '{session}': {labels}")
    only = next(iter(windows.values()))
    if len(only) == 1:
        return only[0]
    for pane in only:
        if pane.get("pane_active") == "1":
            return pane
    return only[0]


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
    if is_tmux_target(target):
        validate_target(target)
        return target
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
