from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
    CONFIDENCE_STRONG,
    ROLE_SOURCE_PANE_OPTION,
    infer_repo_root,
    resolve_agent_role,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import DEFAULT_LANE
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    pane_lines,
    resolve_pane_id,
    run_tmux,
    validate_target,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import READ_MARK_PREFIX, normalize_path_unicode


AGENT_PROCESSES = {"claude", "codex", "node"}
AGENT_COMMANDS = {
    "claude": "claude",
    "codex": "codex",
}
AGENT_LABELS = frozenset(AGENT_COMMANDS)
# Pseudo-target label (Redmine #12015): resolves to the sender workspace's main
# coordinator Codex (the default-lane Codex), so a sublane can call back the
# coordinator without hand-picking its `%pane`. Not an `AGENT_LABELS` member —
# it routes through a dedicated workspace-scoped resolver, not window resolution.
COORDINATOR_LABEL = "coordinator"
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


def current_pane_lane_unit() -> tuple[str | None, str | None]:
    """The ``(workspace_id, lane_id)`` Unit of the *sender* pane, or ``(None, None)``.

    Redmine #12918: the gateway-route enforcement gate needs the sender's own lane
    Unit to tell a legitimate same-lane ``gateway -> worker`` dispatch from a
    coordinator reaching directly into a different lane's worker. The authority for
    that Unit is the live pane inventory, not a bare tmux option read: the sender
    pane (``TMUX_PANE``) is matched against the same :func:`pane_lines` rows the
    target is resolved from, so a sender that the inventory does not carry — run
    outside tmux, or from a pane the managed inventory does not know — resolves to
    ``(None, None)``. The caller treats that as "sender identity unknown" and skips
    the gate (it cannot prove a cross-lane bypass), mirroring how the cross-session
    gate is skipped when the sender session is unknown.

    Best-effort and fail-open for *resolution*: any inventory read failure (no tmux)
    yields ``(None, None)`` rather than raising, so the enforcement gate never turns
    a discovery hiccup into a spurious block.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None, None
    try:
        rows = pane_lines()
    except (Exception, SystemExit):
        return None, None
    for row in rows:
        if row.get("id") == pane:
            workspace_id, lane_id = _pane_lane_identity(row)
            return (workspace_id or None), lane_id
    return None, None


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
    (``vibes/docs/logics/coordinator-sublane-development-flow.md`` ``## 役割`` —
    cross-lane routing goes through the target lane's Codex gateway) — a
    cross-lane handoff still has to be addressed explicitly
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


def _repo_identity_matches(
    sender: dict[str, str], candidate: dict[str, str]
) -> bool:
    """Repo identity gate for same-session local Claude auto-select (#12070).

    Design condition 6 (Redmine #12069 j#59568): the sender and a candidate must
    resolve to the same repo identity before the candidate can be auto-selected.

    - When both cwds infer a :func:`infer_repo_root`, the roots must be equal
      (Unicode-normalized, matching the cross-workspace identity gate in
      ``orchestrate_handoff``). This is what disambiguates several Claude panes
      that share a ``(workspace_id, lane_id)`` but live in different repo
      checkouts within one cockpit session.
    - When *neither* cwd infers a repo root, the shared registered
      ``workspace_id`` (already required by the same-lane gate upstream) carries
      the match, so a non-git scaffolded workspace still auto-selects.
    - A root inferable on only one side, or two different roots, is fail-closed
      (the candidate is dropped) — "missing or mismatched repo root is
      fail-closed".

    This selects the *pane*; it deliberately does not solve nested project
    execution-root propagation (Redmine #12098), which is a handoff-payload /
    durable-record concern, not a pane-selection one.
    """
    sender_root = infer_repo_root(sender.get("cwd") or "")
    candidate_root = infer_repo_root(candidate.get("cwd") or "")
    if sender_root is not None and candidate_root is not None:
        return normalize_path_unicode(sender_root) == normalize_path_unicode(
            candidate_root
        )
    if sender_root is None and candidate_root is None:
        sender_ws = (sender.get("workspace_id") or "").strip()
        candidate_ws = (candidate.get("workspace_id") or "").strip()
        return bool(sender_ws) and sender_ws == candidate_ws
    return False


def narrow_to_local_claude(
    targets: list[dict[str, str]],
    sender: dict[str, str] | None,
) -> list[dict[str, str]]:
    """Narrow same-session Claude candidates for ``--to claude`` auto-select (#12070).

    Stricter superset of :func:`narrow_to_sender_lane`, encoding the safe
    auto-select conditions fixed in the design (Redmine #12069 j#59568):

    - **condition 2** — the sender must carry a machine-checkable, non-empty
      ``workspace_id``. A ``default`` lane is admissible only with a workspace
      id; a sender with no workspace marker cannot disambiguate, so ``targets``
      is returned unchanged and the caller fails closed.
    - **condition 5** — candidates are restricted to the sender's own
      ``(workspace_id, lane_id)``. A different lane is never selected (it routes
      through that lane's Codex gateway,
      ``coordinator-sublane-development-flow.md`` ``## 役割``).
    - **condition 6** — each kept candidate must pass :func:`_repo_identity_matches`.

    Like :func:`narrow_to_sender_lane`, this can only *shrink* the candidate set,
    so the caller's "exactly one" check (condition 7) and its ``> 1`` / ``== 0``
    fail-closed handling stay intact, and it never crosses a lane / session
    boundary. The identity source is the live tmux ``@mozyo_*`` pane options and
    cwd in the snapshot, never a pane title.
    """
    if sender is None:
        return targets
    sender_ws, sender_lane = _pane_lane_identity(sender)
    if not sender_ws:
        return targets
    same_lane = [
        pane
        for pane in targets
        if _pane_lane_identity(pane) == (sender_ws, sender_lane)
    ]
    return [pane for pane in same_lane if _repo_identity_matches(sender, pane)]


def _format_agent_candidate(pane: dict[str, str]) -> str:
    """One diagnostics row for the fail-closed candidate list (Redmine #12071).

    Surfaces the identity an operator needs to pick the right pane by hand
    without re-running ``mozyo-bridge agents targets``: the pane id, the resolved
    role source (which signal decided the role — ``pane_option`` / ``window_name``
    / ``inferred``), the ``(workspace, lane)`` identity, the inferred repo root,
    the cwd, and whether the pane is the active split of its window. These are
    exactly the fields that disambiguate several same-session Claude panes and the
    ones a Redmine fail-closed journal needs transcribed. Identity comes from the
    live tmux ``@mozyo_*`` pane options and cwd in the snapshot, never a pane
    title.
    """
    workspace_id, lane_id = _pane_lane_identity(pane)
    lane_label = (pane.get("lane_label") or "").strip()
    pane_id = pane.get("id") or pane.get("location") or "?"
    cwd = (pane.get("cwd") or "").strip()
    repo_root = infer_repo_root(cwd) if cwd else None
    role_source = resolve_agent_role(
        pane_option_role=pane.get("agent_role"),
        window_name=pane.get("window_name"),
        process=pane.get("command"),
    ).role_source
    active = "active" if pane.get("pane_active") == "1" else "inactive"
    return (
        f"{pane_id} (workspace={workspace_id or '<none>'}, "
        f"lane={lane_label or lane_id}, role_source={role_source}, "
        f"repo_root={repo_root or '<none>'}, cwd={cwd or '<none>'}, {active})"
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
        f"{sender_clause}. Name the exact pane with "
        "`--target %pane --target-repo auto` — the explicit pane plus the auto "
        "repo-identity gate is the safest retry, since it pins the receiver by "
        "pane id and re-checks the workspace/repo root from that pane's own cwd "
        "(see `mozyo-bridge agents targets` for the candidate identities)."
    )


def same_lane_receiver_duplicates(
    target: dict[str, str],
    snapshot: list[dict[str, str]],
    receiver: str,
) -> list[dict[str, str]]:
    """Other live panes in the resolved target's lane that also are ``receiver`` (#12229).

    A cockpit repair (Redmine #12226 j#61213: ``mozyo cockpit append`` after a
    missing-Codex gateway) can leave **two** same-lane Claude panes alive. A
    handoff is then addressed by an explicit ``--target %pane`` to one of them
    (``%14``), but the operator may be watching — and the implementation actor
    may report from — the duplicate (``%16``). Worse, an earlier failed
    ``--mode standard`` send leaves residual prompt text in a duplicate (the
    strict rail issues ``C-u`` but cannot verify the receiver composer cleared,
    ``vibes/docs/logics/tmux-send-safety-contract.md``). The durable record then
    diverges: the delivery record names ``%14`` as receiver while the
    Implementation Done journal names ``%16`` as actor (#12226 j#61224 vs
    j#61228).

    This returns the OTHER same-``(workspace, lane)`` panes that resolve to the
    same ``receiver`` role, so the caller can surface them in the durable
    delivery record and keep the receiver pane and any stale-input duplicate
    both visible. It does NOT block: an explicit ``--target %pane`` is the
    documented escape hatch and the queue-enter Step 11 active-split gate already
    fail-closes the inactive duplicate; this is a diagnostic surface only.

    Only meaningful when the target carries a concrete ``(workspace, lane)``
    identity (:func:`_has_concrete_lane_identity`); a ``default``-lane /
    no-workspace target has nothing to disambiguate against and yields ``[]``.
    The target pane itself is always excluded. Role identity comes from
    :func:`resolve_agent_role` over the live tmux ``@mozyo_*`` pane options /
    window name / process in the snapshot, never a pane title — so a same-lane
    Codex gateway pane (role=codex) is not mistaken for a duplicate Claude
    receiver.
    """
    if receiver not in AGENT_LABELS:
        return []
    target_id = target.get("id")
    target_identity = _pane_lane_identity(target)
    if not _has_concrete_lane_identity(*target_identity):
        return []
    duplicates: list[dict[str, str]] = []
    for pane in snapshot:
        if pane.get("id") == target_id:
            continue
        if _pane_lane_identity(pane) != target_identity:
            continue
        resolution = resolve_agent_role(
            pane_option_role=pane.get("agent_role"),
            window_name=pane.get("window_name"),
            process=pane.get("command"),
        )
        if resolution.role != receiver:
            continue
        duplicates.append(pane)
    return duplicates


def duplicate_pane_record_row(pane: dict[str, str]) -> str:
    """One durable-record-safe identity row for a same-lane duplicate (#12229).

    A redacted sibling of :func:`_format_agent_candidate`: it carries the same
    disambiguating identity (pane id, ``(workspace, lane)``, role source, active
    split) but deliberately omits the absolute ``cwd`` / ``repo_root`` so the row
    is safe to paste into a Redmine journal or Asana comment
    (``vibes/docs/rules/public-private-boundary.md``; auto-memory
    ``feedback_pasteable_records_redact_abs_paths``). Identity comes from the
    live tmux ``@mozyo_*`` pane options, never a pane title.
    """
    workspace_id, lane_id = _pane_lane_identity(pane)
    lane_label = (pane.get("lane_label") or "").strip() or lane_id
    pane_id = pane.get("id") or pane.get("location") or "?"
    role_source = resolve_agent_role(
        pane_option_role=pane.get("agent_role"),
        window_name=pane.get("window_name"),
        process=pane.get("command"),
    ).role_source
    active = "active" if pane.get("pane_active") == "1" else "inactive"
    return (
        f"{pane_id} (workspace={workspace_id or '<none>'}, "
        f"lane={lane_label}, role_source={role_source}, {active})"
    )


def _is_strong_provider(pane: dict[str, str], provider: str) -> bool:
    """True when ``pane`` strongly, non-ambiguously resolves to ``provider``'s role."""
    resolution = resolve_agent_role(
        pane_option_role=pane.get("agent_role"),
        window_name=pane.get("window_name"),
        process=pane.get("command"),
    )
    return (
        resolution.role == provider
        and resolution.confidence == CONFIDENCE_STRONG
        and not resolution.ambiguous
    )


def _is_strong_codex(pane: dict[str, str]) -> bool:
    """True when ``pane`` strongly, non-ambiguously resolves to the Codex role."""
    return _is_strong_provider(pane, AGENT_KIND_CODEX)


def coordinator_codex_candidates(
    panes: list[dict[str, str]],
    workspace_id: str,
    *,
    provider: str = AGENT_KIND_CODEX,
) -> list[dict[str, str]]:
    """Default-lane (coordinator) panes bound to ``provider`` in ``workspace_id``.

    Redmine #12015 (role-based provider since Redmine #13174). The coordinator lane
    is a workspace's primary checkout — the lane the cockpit stamps as
    :data:`DEFAULT_LANE` (``cockpit_layout.resolve_lane_identity``), as opposed to a
    linked-worktree / clone sublane that carries a hashed lane id. Its owner-facing
    actor is the pane running the **coordinator role's** runtime provider.

    ``provider`` defaults to :data:`~...agent_discovery.AGENT_KIND_CODEX` so an
    unconfigured / default-binding callback resolves the coordinator to the Codex
    pane exactly as before #13174; the caller resolves the coordinator role's provider
    from the :class:`RoleProviderBinding` and passes it here when a rebind moves the
    coordinator to a different surface. Candidates are deduplicated by ``pane_id``
    (grouped-session views collapse to one). Identity comes from the live tmux
    ``@mozyo_*`` pane options in ``panes``, never a pane title.
    """
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for pane in panes:
        pane_ws, pane_lane = _pane_lane_identity(pane)
        if pane_ws != workspace_id or pane_lane != DEFAULT_LANE:
            continue
        if not _is_strong_provider(pane, provider):
            continue
        pane_id = pane.get("id") or ""
        if pane_id in seen:
            continue
        seen.add(pane_id)
        unique.append(pane)
    return unique


def resolve_coordinator_codex(
    panes: list[dict[str, str]],
    sender: dict[str, str] | None,
    *,
    provider: str = AGENT_KIND_CODEX,
) -> dict[str, str] | None:
    """The main coordinator pane for the sender's workspace (Redmine #12015).

    A sublane resolves its coordinator by selecting the pane that shares its own
    ``workspace_id``, sits in the :data:`DEFAULT_LANE`, and runs the coordinator
    role's runtime provider. This is the sanctioned cross-lane sublane->coordinator
    callback path (see ``skills/mozyo-bridge-agent/references/workflow.md`` ``## Owner
    Approval Aggregation`` and ``## Sublane Coordinator Callback``). It is strictly
    **workspace-scoped**: it never reaches another workspace's coordinator (that is
    the cross-workspace consult primitive's job, Redmine #11779), and it stays
    fail-closed — returns ``None`` when the sender is unknown, carries no workspace
    identity, or the match is not unique. Live tmux pane options are the identity
    source, never a pane title.

    ``provider`` is the coordinator role's runtime provider; it defaults to
    :data:`~...agent_discovery.AGENT_KIND_CODEX` (Redmine #13174), so the default
    binding resolves the coordinator to Codex exactly as before, while a rebind can
    resolve it to a different surface.
    """
    if sender is None:
        return None
    sender_ws, _sender_lane = _pane_lane_identity(sender)
    if not sender_ws:
        return None
    candidates = coordinator_codex_candidates(panes, sender_ws, provider=provider)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _canonical_state_is_broken(canonical_state: dict[str, object] | None) -> bool:
    """True when a canonical_path liveness fact shows a dead / non-main path (#13152).

    Pure. ``canonical_state`` is a
    ``workspace_registry.probe_canonical_liveness`` mapping (or ``None``). A dead
    path (missing / not a dir) or a git checkout that is a linked worktree rather
    than the main worktree is "broken" — the #13152 registry-hijack signature.
    """
    if not canonical_state:
        return False
    if not canonical_state.get("exists") or not canonical_state.get("is_dir"):
        return True
    return bool(
        canonical_state.get("is_git")
        and canonical_state.get("is_main_worktree") is False
    )


def _sender_canonical_state(
    sender: dict[str, str] | None,
) -> dict[str, object] | None:
    """Best-effort registry canonical_path liveness for the sender workspace (#13152).

    Impure (registry read), kept out of the pure :func:`_no_coordinator_message`
    builder. Returns ``None`` on any error / unknown workspace so the caller
    degrades to the generic no-coordinator hint.
    """
    if sender is None:
        return None
    sender_ws, _sender_lane = _pane_lane_identity(sender)
    if not sender_ws:
        return None
    try:
        from mozyo_bridge.workspace_registry import (
            load_workspace_by_id,
            probe_canonical_liveness,
        )

        record = load_workspace_by_id(sender_ws)
        if record is None:
            return None
        return probe_canonical_liveness(record.canonical_path)
    except Exception:
        return None


def _no_coordinator_message(
    panes: list[dict[str, str]],
    sender: dict[str, str] | None,
    *,
    canonical_state: dict[str, object] | None = None,
    provider: str = AGENT_KIND_CODEX,
) -> str:
    """Fail-closed guidance when the coordinator pane cannot be resolved (#12015).

    ``canonical_state`` (Redmine #13152) is the optional liveness fact for the
    sender workspace's registered ``canonical_path`` (from
    ``workspace_registry.probe_canonical_liveness``), supplied by the impure
    caller so this stays a pure message builder. When the registry canonical_path
    is a dead / non-main-worktree path, the no-candidate branch names that true
    cause — the registry was hijacked — instead of the misleading "stand up a
    Codex pane in the main checkout" hint (which is the opposite of the fix).
    """
    if sender is None:
        return (
            "cannot resolve `coordinator`: the sender pane is unknown (run from "
            "inside the lane's pane). Name the coordinator pane explicitly with "
            "`--target %pane` (see `mozyo-bridge agents targets`)."
        )
    sender_ws, _sender_lane = _pane_lane_identity(sender)
    if not sender_ws:
        return (
            "cannot resolve `coordinator`: the sender pane carries no workspace "
            "identity, so its workspace's coordinator lane cannot be selected. "
            "Name the coordinator pane explicitly with `--target %pane` "
            "(see `mozyo-bridge agents targets`)."
        )
    candidates = coordinator_codex_candidates(panes, sender_ws, provider=provider)
    if not candidates and _canonical_state_is_broken(canonical_state):
        canonical_path = canonical_state.get("canonical_path") if canonical_state else None
        where = f" ({canonical_path})" if canonical_path else ""
        return (
            "cannot resolve `coordinator`: the registered canonical_path for "
            f"workspace {sender_ws!r}{where} does not point at a live main "
            "checkout (it is missing or a linked worktree), so the coordinator "
            "lane cannot be found. This is a registry defect, not a missing "
            f"{provider} pane: run `mozyo-bridge workspace register` from the "
            "workspace's main checkout to repair it (Redmine #13152), or name the "
            "coordinator pane explicitly with `--target %pane`."
        )
    if not candidates:
        reason = (
            f"no default-lane (coordinator) {provider} pane was found in workspace "
            f"{sender_ws!r}. Ensure the workspace's main checkout has a running "
            f"{provider} pane"
        )
    else:
        listed = ", ".join(
            _format_agent_candidate(pane)
            for pane in sorted(candidates, key=lambda pane: pane.get("id") or "")
        )
        reason = (
            f"multiple default-lane {provider} panes resolved in workspace "
            f"{sender_ws!r}: {listed}"
        )
    return (
        f"cannot resolve `coordinator`: {reason}. Name the coordinator pane "
        "explicitly with `--target %pane` (see `mozyo-bridge agents targets`)."
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
        if agent == AGENT_KIND_CLAUDE:
            # Same-session local Claude auto-select (Redmine #12070): layer the
            # repo identity gate and the stricter non-empty `workspace_id`
            # requirement on top of #12011 lane narrowing, so a cockpit hosting
            # several Claude panes that share a `(workspace_id, lane_id)` but
            # live in different repo checkouts still resolves the sender's own
            # local Claude. This only shrinks the set; cross-session and
            # cross-lane Claude stay blocked (here and downstream).
            narrowed = narrow_to_local_claude(targets, sender_pane)
        else:
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
    if target == COORDINATOR_LABEL:
        # Workspace-scoped coordinator resolution (Redmine #12015): pick the
        # sender workspace's default-lane pane running the coordinator role's
        # provider so a sublane callback does not need a hand-picked `%pane`.
        # Role-based since Redmine #13174 (j#72023): the provider is resolved
        # from the repo-local RoleProviderBinding (default -> codex,
        # byte-identical) via a call-time import of the f_140 boundary — a
        # module-level import here would create an f_120 -> f_140 import cycle
        # (f_140 already imports this module at load). Fail-closed with
        # concrete guidance.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (
            resolve_coordinator_provider,
        )

        coordinator_provider = resolve_coordinator_provider()
        panes = pane_lines()
        sender = _sender_pane(panes)
        coordinator = resolve_coordinator_codex(
            panes, sender, provider=coordinator_provider
        )
        if coordinator is not None:
            return coordinator["id"]
        die(
            _no_coordinator_message(
                panes,
                sender,
                canonical_state=_sender_canonical_state(sender),
                provider=coordinator_provider,
            )
        )
        raise AssertionError("unreachable")
    if target not in AGENT_LABELS:
        die(
            f"unknown target '{target}'. Pass a tmux pane id (`%nnn`), a "
            "location (`session:window.pane`), an agent label "
            f"({', '.join(sorted(AGENT_LABELS))}), or `{COORDINATOR_LABEL}` "
            "(the sender workspace's main coordinator pane)."
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
