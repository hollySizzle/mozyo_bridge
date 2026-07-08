"""herdr sublane read-model projection + backend selection (Redmine #13331).

Under ``terminal_transport.backend: herdr`` a lane is its own herdr workspace (option A,
j#73314): its two managed agents are ``mzb1_<lane-ws>_codex_default`` /
``mzb1_<lane-ws>_claude_default``. This module folds the live ``herdr agent list``
inventory into the SAME :class:`SublaneLaneView` read model ``sublane list`` renders for
tmux — one row per lane workspace — so the coordinator can see herdr lanes the same way
(#13303 cockpit_present fold lesson: a new backend's rows join the existing read model).

Two backend-selection notes keep the tmux path byte-invariant:

* the projection is a **separate** code path chosen by :func:`repo_backend_is_herdr`; the
  tmux ``project_sublanes`` fold and its ``SublaneLaneView`` payload are untouched, so
  ``backend: tmux`` output does not change (a lane is default-lane within its own herdr
  workspace, which the tmux fold would *exclude* — the two "lane" notions do not share a
  fold);
* the sender's OWN workspace is excluded: the coordinator / main workspace also carries a
  codex (auditor) + claude (coordinator) default-lane pair, which is not a sublane.

Stale / retire hints (Redmine #13358): the fold also supplies the herdr analogue of the
#13086 tmux advisory diagnosis material into the same ``stale_hints`` field — a lost
gateway / worker slot, a live-but-vanished lane workspace (an active lane metadata record
with no live managed slot), duplicate lanes carrying one issue id, and a recorded worktree
that no longer resolves to a live git checkout. Exactly like the tmux hints they are
retire *decision material* for a human / coordinator: advisory display output only, never
an auto-retire trigger, and routing / callback target resolution never reads them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    GATEWAY_ROLE,
    STALE_HINT_DUPLICATE_ISSUE_LANE,
    STALE_HINT_WORKTREE_UNRESOLVED,
    WORKER_ROLE,
    SublaneLaneView,
    _lane_state,
    parse_issue_from_lane_label,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    HerdrSessionStartError,
    _list_rows,
    _resolve_binary_or_die,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
)


def repo_backend_is_herdr(repo_root: Path) -> bool:
    """True iff ``repo_root``'s repo-local config selects the herdr terminal backend.

    A broken / unreadable / absent config is NOT a herdr selection (it resolves to the tmux
    default), exactly like the send path's ``herdr_backend_selected`` — so any tmux path
    guarded on this stays byte-for-byte the pre-#13331 behaviour when herdr is not selected.
    """
    from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
        RepoLocalConfigError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
        BACKEND_HERDR,
    )

    try:
        config = load_repo_local_config(repo_root).terminal_transport
    except RepoLocalConfigError:
        return False
    return config is not None and config.backend == BACKEND_HERDR


# ---------------------------------------------------------------------------
# herdr stale / retire hints (#13358): the herdr lane analogue of the #13086
# tmux ``STALE_HINT_*`` vocabulary. Each token names one observed inconsistency
# between the lane's durable identity and the live herdr inventory. Advisory
# retire decision material only — never an auto-retire trigger, never a routing
# input, and unknown never fabricates a hint. Where a tmux token's meaning
# carries over unchanged (``duplicate_issue_lane:<peer>``,
# ``worktree_unresolved``) the shared domain token is reused; the herdr-only
# conditions get their own tokens below.
# ---------------------------------------------------------------------------

#: Machine-readable ``stale_hints`` token: the live lane workspace has no lane
#: metadata record, so its human identity (lane_label / issue / branch /
#: worktree) could not be resolved and the row degrades to the raw token
#: (Redmine #13356 j#73386 fail-open degrade). Advisory display material only.
LANE_RECORD_MISSING_HINT = "lane_record_missing"

#: The lane workspace has no live ``codex`` gateway slot (dispatch / callback
#: rail lost) — the herdr analogue of the tmux ``gateway_pane_missing``.
GATEWAY_SLOT_MISSING_HINT = "gateway_slot_missing"
#: The lane workspace has no live ``claude`` worker slot (implementer lost /
#: never adopted) — the herdr analogue of the tmux ``worker_pane_missing``.
WORKER_SLOT_MISSING_HINT = "worker_slot_missing"
#: An ACTIVE lane metadata record's workspace has NO live managed slot at all:
#: the lane vanished (herdr down-scoped / agents closed outside retire) while
#: the durable display record still says active. Rendered as a detached row so
#: the loss stays visible instead of silently dropping out of ``sublane list``.
LANE_WORKSPACE_MISSING_HINT = "lane_workspace_missing"


def list_herdr_agent_rows(env: Mapping[str, str]) -> Sequence[Mapping[str, object]]:
    """The live ``herdr agent list`` rows (fail-closed on binary / inventory failure)."""
    import subprocess

    binary = _resolve_binary_or_die(env)
    return _list_rows(binary, subprocess.run, 30.0)


def probe_worktree_resolved(path: str) -> Optional[bool]:
    """Read-only probe: does ``path`` still resolve to a live git checkout?

    The herdr twin of the tmux ``branch_for`` unresolved-worktree probe (#13086):
    ``False`` means the recorded worktree is gone / not a git checkout (removed,
    moved, or never created) — stale retire material. ``None`` is *unknown* (empty
    path, git binary unavailable), and unknown never fabricates a hint.
    """
    import subprocess

    if not path:
        return None
    if not Path(path).is_dir():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
            capture_output=True,
        )
    except OSError:
        return None
    return result.returncode == 0


@dataclass(frozen=True)
class _LaneEntry:
    """One pre-hint lane row of the fold (internal assembly record)."""

    workspace_id: str
    gateway: Optional[str]
    worker: Optional[str]
    lane_label: str
    issue: Optional[str]
    branch: Optional[str]
    repo_root: Optional[str]
    identity_hints: tuple[str, ...]
    workspace_missing: bool
    #: The lane's record attributes it to the CALLER's repo (j#73459 finding 1):
    #: only repo-scoped entries participate in duplicate-issue grouping, so a
    #: same-issue lane of a *different* repo never fabricates a duplicate hint.
    repo_scoped: bool


def project_herdr_sublanes(
    rows: Sequence[Mapping[str, object]],
    *,
    exclude_workspace_id: str,
    resolve_repo_root: Callable[[str], Optional[str]],
    resolve_lane_record: Optional[Callable[[str], Optional[object]]] = None,
    lane_records: Optional[Mapping[str, object]] = None,
    worktree_resolved: Optional[Callable[[str], Optional[bool]]] = None,
    repo_workspace_id: str = "",
) -> tuple[SublaneLaneView, ...]:
    """Fold the live herdr inventory into one :class:`SublaneLaneView` per lane workspace.

    Decodes each ``agent list`` row's ``mzb1`` name (#13247); a row is a managed lane slot
    iff it decodes to ``(workspace_id != exclude_workspace_id, default lane, codex|claude)``
    and carries a live locator. Rows are grouped by workspace; each workspace with at least
    one managed slot becomes a lane row (a gateway-only / worker-only workspace is a
    degraded lane, surfaced with its state). Foreign (non-mzb1) rows and the excluded
    workspace are dropped.

    Lane identity resolution (Redmine #13356 j#73386): ``resolve_lane_record(workspace_id)``
    — the host-local lane metadata record written at ``sublane create`` — is the primary
    source of the lane's human identity (``lane_label`` / ``issue`` / ``branch`` /
    worktree ``repo_root``); it is a **display join, never routing authority**. When no
    record exists the fold falls back to ``resolve_repo_root(workspace_id)`` (the mozyo
    registry's canonical path — its basename is the lane label; only resolvable for a
    registry-id workspace, never for a ``wt_<hash>`` lane token) and finally to the raw
    workspace id as the label (never guessed), with the
    :data:`LANE_RECORD_MISSING_HINT` stale hint so the degrade stays visible.
    ``lane_records`` (the full token-keyed record mapping) doubles as the record join
    source when ``resolve_lane_record`` is not supplied.

    Stale / retire hints (Redmine #13358), all advisory-only:

    - a lane with only one live managed slot carries :data:`GATEWAY_SLOT_MISSING_HINT` /
      :data:`WORKER_SLOT_MISSING_HINT` for the lost slot;
    - an ACTIVE record in ``lane_records`` whose workspace has NO live managed slot is
      emitted as an extra detached row (appended after the live lanes, token-sorted)
      carrying :data:`LANE_WORKSPACE_MISSING_HINT` — a vanished lane stays visible
      instead of silently dropping out (retired tombstones and the excluded workspace
      never produce such a row);
    - every emitted lane (live or vanished) resolving to the same issue id names each
      peer as ``duplicate_issue_lane:<peer label or token>`` (the shared #13086 token);
    - a lane whose resolved worktree path ``worktree_resolved`` reports as ``False`` (no
      longer a live git checkout) carries ``worktree_unresolved``; ``None`` / no probe is
      *unknown*, and unknown never fabricates a hint.

    Repo scope (j#73459 finding 1): the lane metadata store is host-global, so vanished
    rows and duplicate-issue grouping consider ONLY records whose ``repo_workspace_id``
    equals the caller's ``repo_workspace_id`` (the workspace segment ``sublane create``
    stamped, resolved by the same shared resolver). An empty caller / record value never
    matches — a foreign repo's lost lane never leaks a detached row or a duplicate hint
    into this repo's list (fail-safe: unattributable lanes just carry no such hint).
    The LIVE row enumeration itself stays host-global per the #13331 contract.

    Pure over the injected rows + resolvers (no subprocess / config read); live lanes keep
    deterministic first-seen ordering.
    """
    if resolve_lane_record is None and lane_records is not None:
        resolve_lane_record = lane_records.get
    slots: dict[str, dict[str, str]] = {}
    order: list[str] = []
    exclude = _norm(exclude_workspace_id)
    repo_scope = _norm(repo_workspace_id)

    def _record_repo_scoped(record: object) -> bool:
        """True iff ``record`` attributes its lane to the caller's repo (never on empty)."""
        if not repo_scope or record is None:
            return False
        return _norm(getattr(record, "repo_workspace_id", "")) == repo_scope

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if _norm_lane(identity.lane_id) != DEFAULT_LANE:
            continue
        if identity.role not in (GATEWAY_ROLE, WORKER_ROLE):
            continue
        ws = identity.workspace_id
        if not ws or ws == exclude:
            continue
        locator = _agent_locator(row)
        if not locator:
            continue
        if ws not in slots:
            slots[ws] = {}
            order.append(ws)
        slots[ws].setdefault(identity.role, locator)

    entries: list[_LaneEntry] = []
    for ws in order:
        gateway = slots[ws].get(GATEWAY_ROLE)
        worker = slots[ws].get(WORKER_ROLE)
        record = resolve_lane_record(ws) if resolve_lane_record is not None else None
        if record is not None and getattr(record, "lane_label", ""):
            lane_label = getattr(record, "lane_label")
            issue = getattr(record, "issue_id", "") or parse_issue_from_lane_label(
                lane_label
            )
            branch = getattr(record, "branch", "") or None
            repo_root = getattr(record, "worktree_path", "") or resolve_repo_root(ws)
            identity_hints: tuple[str, ...] = ()
        else:
            repo_root = resolve_repo_root(ws)
            lane_label = Path(repo_root).name if repo_root else ws
            issue = parse_issue_from_lane_label(lane_label)
            branch = None
            identity_hints = () if repo_root else (LANE_RECORD_MISSING_HINT,)
        entries.append(
            _LaneEntry(
                workspace_id=ws,
                gateway=gateway,
                worker=worker,
                lane_label=lane_label,
                issue=issue or None,
                branch=branch,
                repo_root=repo_root,
                identity_hints=identity_hints,
                workspace_missing=False,
                repo_scoped=_record_repo_scoped(record),
            )
        )

    # Vanished lane workspaces (#13358): an ACTIVE lane record with no live managed
    # slot. Only records can reveal these — the live fold above never sees them.
    # Repo-scoped (j#73459 finding 1): a foreign repo's record never becomes a row here.
    if lane_records:
        live = {_norm(ws) for ws in order}
        for token in sorted(lane_records):
            record = lane_records[token]
            if getattr(record, "retired", False):
                continue
            if not _record_repo_scoped(record):
                continue
            if _norm(token) in live or _norm(token) == exclude:
                continue
            lane_label = getattr(record, "lane_label", "") or token
            issue = getattr(record, "issue_id", "") or parse_issue_from_lane_label(
                lane_label
            )
            entries.append(
                _LaneEntry(
                    workspace_id=token,
                    gateway=None,
                    worker=None,
                    lane_label=lane_label,
                    issue=issue or None,
                    branch=getattr(record, "branch", "") or None,
                    repo_root=getattr(record, "worktree_path", "") or None,
                    identity_hints=(),
                    workspace_missing=True,
                    repo_scoped=True,
                )
            )

    # Duplicate-issue detection needs the whole emitted lane set (live + vanished),
    # so every duplicate lane can name its peers (mirrors the tmux fold). Only
    # repo-scoped entries participate (j#73459 finding 1): a lane whose record
    # attributes it to another repo — or whose repo attribution is unknown — never
    # raises or receives a duplicate hint (unknown never fabricates a hint).
    lanes_by_issue: dict[str, list[int]] = {}
    for idx, entry in enumerate(entries):
        if entry.issue and entry.repo_scoped:
            lanes_by_issue.setdefault(entry.issue, []).append(idx)

    views: list[SublaneLaneView] = []
    for idx, entry in enumerate(entries):
        hints: list[str] = []
        if entry.workspace_missing:
            hints.append(LANE_WORKSPACE_MISSING_HINT)
        else:
            if not entry.gateway:
                hints.append(GATEWAY_SLOT_MISSING_HINT)
            if not entry.worker:
                hints.append(WORKER_SLOT_MISSING_HINT)
        if entry.repo_scoped:
            for peer_idx in lanes_by_issue.get(entry.issue or "", ()):
                if peer_idx == idx:
                    continue
                peer = entries[peer_idx]
                hints.append(
                    f"{STALE_HINT_DUPLICATE_ISSUE_LANE}:{peer.lane_label or peer.workspace_id}"
                )
        if (
            entry.repo_root
            and worktree_resolved is not None
            and worktree_resolved(entry.repo_root) is False
        ):
            hints.append(STALE_HINT_WORKTREE_UNRESOLVED)
        hints.extend(entry.identity_hints)
        views.append(
            SublaneLaneView(
                workspace_id=entry.workspace_id,
                lane_id=DEFAULT_LANE,
                lane_label=entry.lane_label,
                issue=entry.issue,
                branch=entry.branch,
                repo_root=entry.repo_root,
                gateway_pane=entry.gateway,
                worker_pane=entry.worker,
                state=_lane_state(entry.gateway, entry.worker),
                stale_hints=tuple(hints),
            )
        )
    return tuple(views)


def herdr_sublane_views(
    repo_root: Path, *, env: Optional[Mapping[str, str]] = None
) -> tuple[SublaneLaneView, ...]:
    """Resolve the live herdr lane rows for ``sublane list`` under the herdr backend.

    Excludes the coordinator's own workspace (the anchor of ``repo_root``) and resolves each
    lane workspace's repo root from the mozyo registry. Returns an empty tuple when the
    herdr inventory is unavailable (a down herdr degrades to "no lanes", never a crash) —
    matching the tmux ``sublane list`` degrade-to-empty contract.
    """
    from mozyo_bridge.core.state.lane_metadata import load_lane_records
    from mozyo_bridge.core.state.workspace_registry import load_workspace_by_id
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        herdr_workspace_segment,
    )

    environ = env if env is not None else os.environ
    # The coordinator's OWN workspace segment (a lane token if `sublane list` is somehow run
    # from a lane worktree, else the main registry id) — excluded from the lane projection
    # (#13331 j#73357: same shared resolver as the mint / send / retire sites).
    own_ws = herdr_workspace_segment(repo_root)
    try:
        rows = list_herdr_agent_rows(environ)
    except HerdrSessionStartError:
        return ()

    def _resolve(ws: str) -> Optional[str]:
        record = load_workspace_by_id(ws)
        return record.canonical_path if record is not None else None

    # Fail-open display join (#13356 j#73386): tombstones stay resolvable so a
    # retired-but-still-live lane keeps its label; a missing / unreadable store
    # yields no records and the fold degrades to the raw token. The full mapping
    # also feeds the #13358 vanished-workspace detection (active record, no live
    # slot) and the worktree probe supplies the ``worktree_unresolved`` material.
    lane_records = load_lane_records()

    # own_ws doubles as the repo scope key (j#73459 finding 1): `sublane create`
    # stamps each record's `repo_workspace_id` through the SAME shared resolver
    # over the creating repo root, so only this repo's records feed the vanished /
    # duplicate diagnosis. (Run from a lane worktree the segment is a wt_<hash>
    # token no record carries — the diagnosis then stays quiet, fail-safe.)
    return project_herdr_sublanes(
        rows,
        exclude_workspace_id=own_ws,
        resolve_repo_root=_resolve,
        lane_records=lane_records,
        worktree_resolved=probe_worktree_resolved,
        repo_workspace_id=own_ws,
    )


def herdr_lane_view_for_worktree(
    worktree_path: str, *, env: Optional[Mapping[str, str]] = None
) -> Optional[SublaneLaneView]:
    """Resolve ONE lane worktree's live herdr lane view (fail-safe to ``None``).

    The single-lane twin of :func:`herdr_sublane_views` (Redmine #13356): where the
    list fold enumerates every lane workspace, this resolves the lane anchored on
    ``worktree_path``, with its human identity (``lane_label`` / ``issue`` /
    ``branch``) joined from the lane metadata record written at ``sublane create`` —
    unlike the #13331 actuator read-back, which only echoes the *requested* identity.
    The record is a display join, never routing authority — the gateway / worker
    locators still come only from the live ``agent list`` inventory. Returns ``None``
    when the worktree has no resolvable segment, the inventory is unavailable, or
    neither managed slot is live.

    Deliberately **not wired into ``sublane dispatch-worker`` here**: the herdr
    dispatch drive (lane read-back + measured-ACK forward) is Redmine #13357's
    surface (``sublane_worker_dispatch_herdr_ops``, developed in a sibling lane);
    this helper is the lane-record-joined read-back that surface can adopt when it
    wants a recorded (not merely echoed) identity check.
    """
    from mozyo_bridge.core.state.lane_metadata import load_lane_records
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        herdr_workspace_segment,
    )

    environ = env if env is not None else os.environ
    try:
        workspace_id = herdr_workspace_segment(Path(worktree_path))
    except (OSError, ValueError):
        return None
    if not workspace_id:
        return None
    try:
        rows = list_herdr_agent_rows(environ)
    except HerdrSessionStartError:
        return None
    slots: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != workspace_id:
            continue
        if _norm_lane(identity.lane_id) != DEFAULT_LANE:
            continue
        if identity.role not in (GATEWAY_ROLE, WORKER_ROLE):
            continue
        locator = _agent_locator(row)
        if locator:
            slots.setdefault(identity.role, locator)
    gateway = slots.get(GATEWAY_ROLE)
    worker = slots.get(WORKER_ROLE)
    if not gateway and not worker:
        return None
    record = load_lane_records().get(workspace_id)
    if record is not None and record.lane_label:
        lane_label = record.lane_label
        issue = record.issue_id or parse_issue_from_lane_label(lane_label)
        branch = record.branch or None
        hints: tuple[str, ...] = ()
    else:
        lane_label = Path(worktree_path).name
        issue = parse_issue_from_lane_label(lane_label)
        branch = None
        hints = (LANE_RECORD_MISSING_HINT,)
    return SublaneLaneView(
        workspace_id=workspace_id,
        lane_id=DEFAULT_LANE,
        lane_label=lane_label,
        issue=issue or None,
        branch=branch,
        repo_root=str(worktree_path),
        gateway_pane=gateway,
        worker_pane=worker,
        state=_lane_state(gateway, worker),
        stale_hints=hints,
    )


__all__ = (
    "GATEWAY_SLOT_MISSING_HINT",
    "LANE_RECORD_MISSING_HINT",
    "LANE_WORKSPACE_MISSING_HINT",
    "WORKER_SLOT_MISSING_HINT",
    "herdr_lane_view_for_worktree",
    "herdr_sublane_views",
    "list_herdr_agent_rows",
    "probe_worktree_resolved",
    "project_herdr_sublanes",
    "repo_backend_is_herdr",
)
