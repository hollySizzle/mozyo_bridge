"""herdr sublane read-model projection + backend selection (Redmine #13331 / #13377).

Under ``terminal_transport.backend: herdr`` a lane is a **lane slot unit of the shared
project workspace** (Redmine #13377 Opt3, design j#73613): its two managed agents are
``mzb1_<project-ws>_codex_<lane>`` / ``mzb1_<project-ws>_claude_<lane>``. This module
folds the live ``herdr agent list`` inventory into the SAME :class:`SublaneLaneView`
read model ``sublane list`` renders for tmux — one row per ``(workspace_id, lane_id)``
unit — so the coordinator can see herdr lanes the same way (#13303 cockpit_present fold
lesson: a new backend's rows join the existing read model). The fold rule:

* a **non-default lane** unit is a sublane (any workspace — host-global enumeration);
* a **default-lane** unit is a coordinator pair (the project's codex auditor + claude
  coordinator) and is never a sublane row — EXCEPT a legacy pre-#13377 per-lane
  workspace (a ``wt_<hash>`` token, #13331 j#73314 option A), whose default-lane pair
  IS a lane and stays visible as a compatibility read until it retires.

Two backend-selection notes keep the tmux path byte-invariant:

* the projection is a **separate** code path chosen by :func:`repo_backend_is_herdr`;
  the tmux ``project_sublanes`` fold and its ``SublaneLaneView`` payload are untouched,
  so ``backend: tmux`` output does not change;
* the excluded workspace (the caller's own) only suppresses its legacy live rows — the
  default-lane coordinator pair is already excluded structurally by the fold rule.

Stale / retire hints (Redmine #13358): the fold also supplies the herdr analogue of the
#13086 tmux advisory diagnosis material into the same ``stale_hints`` field — a lost
gateway / worker slot, a live-but-vanished lane (an active lane metadata record with no
live managed slot), duplicate lanes carrying one issue id, and a recorded worktree that
no longer resolves to a live git checkout. Exactly like the tmux hints they are retire
*decision material* for a human / coordinator: advisory display output only, never an
auto-retire trigger, and routing / callback target resolution never reads them.
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    _tab_id_of_row,
    _workspace_prefix,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    _list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    HerdrSessionStartError,
    _resolve_binary_or_die,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
    derive_lane_workspace_token,
    is_lane_workspace_token,
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

#: The lane unit has no live ``codex`` gateway slot (dispatch / callback
#: rail lost) — the herdr analogue of the tmux ``gateway_pane_missing``.
GATEWAY_SLOT_MISSING_HINT = "gateway_slot_missing"
#: The lane unit has no live ``claude`` worker slot (implementer lost /
#: never adopted) — the herdr analogue of the tmux ``worker_pane_missing``.
WORKER_SLOT_MISSING_HINT = "worker_slot_missing"
#: An ACTIVE lane metadata record's lane unit has NO live managed slot at all:
#: the lane vanished (agents closed outside retire) while the durable display
#: record still says active. Rendered as a detached row so the loss stays
#: visible instead of silently dropping out of ``sublane list``. Renamed from
#: the pre-#13377 ``lane_workspace_missing``: under the shared project workspace
#: model (design j#73613) a vanished lane no longer implies a missing herdr
#: workspace — only its ``(workspace_id, lane_id)`` slots are gone.
LANE_SLOTS_MISSING_HINT = "lane_slots_missing"


def list_herdr_agent_rows(env: Mapping[str, str]) -> Sequence[Mapping[str, object]]:
    """The live ``herdr agent list`` rows (fail-closed on binary / inventory failure)."""
    import subprocess

    binary = _resolve_binary_or_die(env)
    return _list_rows(binary, subprocess.run, 30.0)


def repo_scope_workspace_id(repo_root: Path) -> str:
    """The caller repo's MAIN workspace identity — the record-scoping key (j#73469).

    ``sublane create`` stamps each lane record's ``repo_workspace_id`` with the
    creating repo root's segment — the coordinator's **main checkout**, so the
    registry / anchor workspace id. A linked worktree *inherits* that identity
    (#13152) rather than owning one, and its own mzb1 segment is a per-lane
    ``wt_<hash>`` token no record ever carries — so the scope key must resolve
    through the main worktree: the same ``_main_worktree_root`` probe the #13152
    registry inheritance uses, then the same shared segment resolver the create
    site stamped with. A main / standalone checkout resolves as itself
    (byte-for-byte the create-site value). ``""`` on any failure — the vanished /
    duplicate diagnosis then stays quiet rather than guessing (fail-safe).
    """
    from mozyo_bridge.core.state.workspace_registry import _main_worktree_root
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        herdr_workspace_segment,
    )

    try:
        resolved = Path(repo_root).expanduser().resolve()
        main_root = _main_worktree_root(resolved)
        return herdr_workspace_segment(
            main_root if main_root is not None else resolved
        )
    except (OSError, ValueError):
        return ""


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


def is_git_worktree_root(resolved: Path | str) -> bool:
    """True when ``resolved`` is itself the root of a git worktree (Redmine #13933).

    The discriminant for the lane-identity token family (``wt_`` linked git worktree vs
    ``dl_`` non-git directory-scaffold lane), probed on the TARGET root rather than inferred
    from the caller's cwd (design answer j#81046 Decision 1).  Both a linked worktree and the
    main checkout are worktree roots, so ``--show-toplevel`` must equal the path itself: a
    plain directory that merely sits INSIDE some enclosing repository is not a worktree of its
    own, yet ``--is-inside-work-tree`` would call it one.  Both sides are resolved so a symlink
    cannot read as a mismatch.  Never raises; git unavailable / non-git reads ``False``.
    """
    import subprocess

    try:
        root = Path(resolved).resolve()
        if not root.is_dir():
            return False
    except OSError:
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            text=True, capture_output=True,
        )
    except OSError:
        return False
    top = (getattr(result, "stdout", "") or "").strip()
    if result.returncode != 0 or not top:
        return False
    try:
        return Path(top).resolve() == root
    except OSError:
        return False


@dataclass(frozen=True)
class _LaneEntry:
    """One pre-hint lane row of the fold (internal assembly record)."""

    workspace_id: str
    lane_id: str
    gateway: Optional[str]
    worker: Optional[str]
    #: Each live slot's placement-container key ``(herdr_workspace, tab_id)`` — the
    #: pair-split discriminant (Redmine #13705). ``None`` for an absent slot.
    gateway_placement: Optional[tuple]
    worker_placement: Optional[tuple]
    lane_label: str
    issue: Optional[str]
    branch: Optional[str]
    repo_root: Optional[str]
    identity_hints: tuple[str, ...]
    slots_missing: bool
    #: The lane's record attributes it to the CALLER's repo (j#73459 finding 1):
    #: only repo-scoped entries participate in duplicate-issue grouping, so a
    #: same-issue lane of a *different* repo never fabricates a duplicate hint.
    repo_scoped: bool


def _managed_pair_for(
    workspace_id: str,
    resolve_repo_root: "Callable[[str], Optional[str]]",
) -> tuple[str, str]:
    """The (gateway, worker) provider pair a unit's lane is expected to run (Redmine #13569).

    Resolves the unit's repo root (via the injected ``resolve_repo_root``) and reads the
    repo-local ``RoleProviderBinding`` for that repo, so a lane whose binding rebound its
    gateway / worker providers is projected by ITS providers. Any failure (unresolved repo,
    broken / unbound binding) falls back to the built-in ``(GATEWAY_ROLE, WORKER_ROLE)`` pair
    — the projection is a read-model and must never raise; the built-in pair is byte-identical.
    """
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            resolve_gateway_provider,
            resolve_worker_provider,
        )

        repo_root = resolve_repo_root(workspace_id) if resolve_repo_root else None
        if not repo_root:
            return (GATEWAY_ROLE, WORKER_ROLE)
        return (resolve_gateway_provider(repo_root), resolve_worker_provider(repo_root))
    except Exception:  # noqa: BLE001 — a read-model projection never raises.
        return (GATEWAY_ROLE, WORKER_ROLE)


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
    """Fold the live herdr inventory into one :class:`SublaneLaneView` per lane unit.

    Decodes each ``agent list`` row's ``mzb1`` name (#13247); a row is a managed lane
    slot iff it decodes to a ``codex`` / ``claude`` role with a live locator in a lane
    unit ``(workspace_id, lane_id)`` that is a sublane (Redmine #13377, design j#73613):

    - a **non-default lane** of any workspace (the shared-project-workspace lane slots);
    - a **default-lane** unit ONLY when its workspace is a legacy pre-#13377 per-lane
      token (``wt_<hash>``, :func:`is_lane_workspace_token`) — the compatibility read. A
      default-lane pair of a registry workspace is a coordinator pair (the caller's own
      or another project's), never a sublane row. ``exclude_workspace_id`` additionally
      suppresses legacy live rows of the caller's own segment.

    Rows are grouped by unit; each unit with at least one managed slot becomes a lane row
    (a gateway-only / worker-only unit is a degraded lane, surfaced with its state).
    Foreign (non-mzb1) rows are dropped.

    Lane identity resolution (Redmine #13356 j#73386 / #13377): the host-local lane
    metadata record written at ``sublane create`` is the primary source of the lane's
    human identity (``lane_label`` / ``issue`` / ``branch`` / worktree ``repo_root``) —
    a **display join, never routing authority**. A shared-model unit joins on
    ``(repo_workspace_id, lane_id)`` (via ``lane_records``); a legacy unit joins on its
    token (``resolve_lane_record`` / ``lane_records[token]``). When no record exists a
    legacy unit falls back to ``resolve_repo_root(workspace_id)`` (registry canonical
    path basename) and finally the raw token; a shared-model unit falls back to its lane
    id (the lane segment IS the requested lane label at create). Fallbacks carry the
    :data:`LANE_RECORD_MISSING_HINT` stale hint so the degrade stays visible.

    Stale / retire hints (Redmine #13358), all advisory-only:

    - a lane with only one live managed slot carries :data:`GATEWAY_SLOT_MISSING_HINT` /
      :data:`WORKER_SLOT_MISSING_HINT` for the lost slot;
    - an ACTIVE record in ``lane_records`` whose lane unit has NO live managed slot is
      emitted as an extra detached row (appended after the live lanes, token-sorted)
      carrying :data:`LANE_SLOTS_MISSING_HINT` — a vanished lane stays visible instead
      of silently dropping out (retired tombstones and the excluded workspace never
      produce such a row);
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
    records_by_unit: dict[tuple[str, str], object] = {}
    if lane_records:
        for record in lane_records.values():
            rec_ws = _norm(getattr(record, "repo_workspace_id", ""))
            rec_lane = _norm(getattr(record, "lane_id", ""))
            if rec_ws and rec_lane:
                records_by_unit.setdefault((rec_ws, rec_lane), record)
    slots: dict[tuple[str, str], dict[str, str]] = {}
    #: role -> placement-container key ``(herdr_workspace, tab_id)`` per lane unit,
    #: captured alongside the locator so a pair split across tabs / workspaces reads
    #: as ``pair_split`` instead of ``active`` (Redmine #13705).
    placements: dict[tuple[str, str], dict[str, tuple]] = {}
    order: list[tuple[str, str]] = []
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
        # Collect any decoded managed-scheme slot; the gateway/worker pair is picked
        # per-unit below using that unit's binding-resolved providers (Redmine #13569
        # R2-F2), so a lane whose binding rebound its providers is still projected rather
        # than filtered out against a fixed ``codex/claude`` pair. A default-lane coordinator
        # pair is still excluded below.
        ws = identity.workspace_id
        lane = _norm_lane(identity.lane_id)
        if not ws:
            continue
        if lane == DEFAULT_LANE:
            # A default-lane pair is a coordinator pair unless the workspace is a
            # legacy pre-#13377 per-lane token (compatibility read).
            if not is_lane_workspace_token(ws) or ws == exclude:
                continue
        locator = _agent_locator(row)
        if not locator:
            continue
        unit = (ws, lane)
        if unit not in slots:
            slots[unit] = {}
            placements[unit] = {}
            order.append(unit)
        if identity.role not in slots[unit]:
            slots[unit][identity.role] = locator
            # The placement key pairs the herdr terminal workspace (locator prefix,
            # the #13380 axis) with the tab (#13411 axis); an equal key for both
            # slots proves one operable pair.
            placements[unit][identity.role] = (
                _workspace_prefix(locator),
                _tab_id_of_row(row),
            )

    entries: list[_LaneEntry] = []
    for unit in order:
        ws, lane = unit
        gateway_provider, worker_provider = _managed_pair_for(ws, resolve_repo_root)
        gateway = slots[unit].get(gateway_provider)
        worker = slots[unit].get(worker_provider)
        legacy_unit = lane == DEFAULT_LANE
        if legacy_unit:
            record = resolve_lane_record(ws) if resolve_lane_record is not None else None
        else:
            record = records_by_unit.get(unit)
        if record is not None and getattr(record, "lane_label", ""):
            lane_label = getattr(record, "lane_label")
            issue = getattr(record, "issue_id", "") or parse_issue_from_lane_label(
                lane_label
            )
            branch = getattr(record, "branch", "") or None
            repo_root = getattr(record, "worktree_path", "") or (
                resolve_repo_root(ws) if legacy_unit else None
            )
            identity_hints: tuple[str, ...] = ()
        elif legacy_unit:
            repo_root = resolve_repo_root(ws)
            lane_label = Path(repo_root).name if repo_root else ws
            issue = parse_issue_from_lane_label(lane_label)
            branch = None
            identity_hints = () if repo_root else (LANE_RECORD_MISSING_HINT,)
        else:
            # A record-less shared-model unit: the lane segment is the requested
            # lane label at create, so it stays the honest display fallback.
            repo_root = None
            lane_label = lane
            issue = parse_issue_from_lane_label(lane_label)
            branch = None
            identity_hints = (LANE_RECORD_MISSING_HINT,)
        entries.append(
            _LaneEntry(
                workspace_id=ws,
                lane_id=lane,
                gateway=gateway,
                worker=worker,
                # Key the placement lookup on the SAME binding-resolved pair as the slot
                # lookup above (Redmine #13569 R2-F2 invariant): a rebound lane stored its
                # placement under its own provider ids, so keying on the fixed
                # GATEWAY_ROLE / WORKER_ROLE would miss it and read the pair-split fence as
                # "no placement" — the exact read-back skew #13705 fences against.
                gateway_placement=placements[unit].get(gateway_provider),
                worker_placement=placements[unit].get(worker_provider),
                lane_label=lane_label,
                issue=issue or None,
                branch=branch,
                repo_root=repo_root,
                identity_hints=identity_hints,
                slots_missing=False,
                repo_scoped=_record_repo_scoped(record),
            )
        )

    # Vanished lanes (#13358): an ACTIVE lane record with no live managed slot in its
    # lane unit. Only records can reveal these — the live fold above never sees them.
    # Repo-scoped (j#73459 finding 1): a foreign repo's record never becomes a row here.
    if lane_records:
        live_units = {(_norm(ws), _norm_lane(lane)) for ws, lane in order}
        for token in sorted(lane_records):
            record = lane_records[token]
            if getattr(record, "retired", False):
                continue
            if not _record_repo_scoped(record):
                continue
            rec_lane = _norm(getattr(record, "lane_id", ""))
            if rec_lane:
                # Shared-model record: its live unit is (repo_workspace_id, lane_id).
                unit_ws = _norm(getattr(record, "repo_workspace_id", ""))
                unit_lane = rec_lane
            else:
                # Legacy record: its live unit is the token's default-lane pair.
                unit_ws = _norm(token)
                unit_lane = DEFAULT_LANE
                if unit_ws == exclude:
                    continue
            if (unit_ws, unit_lane) in live_units:
                continue
            lane_label = getattr(record, "lane_label", "") or token
            issue = getattr(record, "issue_id", "") or parse_issue_from_lane_label(
                lane_label
            )
            entries.append(
                _LaneEntry(
                    workspace_id=unit_ws or token,
                    lane_id=unit_lane,
                    gateway=None,
                    worker=None,
                    gateway_placement=None,
                    worker_placement=None,
                    lane_label=lane_label,
                    issue=issue or None,
                    branch=getattr(record, "branch", "") or None,
                    repo_root=getattr(record, "worktree_path", "") or None,
                    identity_hints=(),
                    slots_missing=True,
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
        if entry.slots_missing:
            hints.append(LANE_SLOTS_MISSING_HINT)
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
                lane_id=entry.lane_id,
                lane_label=entry.lane_label,
                issue=entry.issue,
                branch=entry.branch,
                repo_root=entry.repo_root,
                gateway_pane=entry.gateway,
                worker_pane=entry.worker,
                state=_lane_state(
                    entry.gateway,
                    entry.worker,
                    gateway_placement=entry.gateway_placement,
                    worker_placement=entry.worker_placement,
                ),
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
    # The caller's OWN workspace segment (the project / main registry id under the #13377
    # shared model — same shared resolver as the mint / send / retire sites). The fold
    # already excludes every registry workspace's default-lane coordinator pair
    # structurally; this value additionally suppresses the caller's own legacy live rows.
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

    # Repo scope key (j#73459 finding 1, corrected per j#73469 finding 1): the
    # caller's MAIN workspace identity — inherited through the main worktree for
    # a linked-worktree caller — so `sublane list` run from a lane worktree
    # repo-local CLI still scopes this repo's records into the vanished /
    # duplicate diagnosis. NOT own_ws: that is the mzb1 segment (a per-lane
    # token for a linked worktree) and only correct as the live-row exclusion.
    return project_herdr_sublanes(
        rows,
        exclude_workspace_id=own_ws,
        resolve_repo_root=_resolve,
        lane_records=lane_records,
        worktree_resolved=probe_worktree_resolved,
        repo_workspace_id=repo_scope_workspace_id(repo_root),
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
        resolved = Path(worktree_path).expanduser().resolve()
        project_ws = herdr_workspace_segment(resolved)
    except (OSError, ValueError):
        return None
    legacy_ws = derive_lane_workspace_token(str(resolved))
    record = load_lane_records().get(legacy_ws)
    try:
        rows = list_herdr_agent_rows(environ)
    except HerdrSessionStartError:
        return None

    slot_placements: dict[str, tuple] = {}

    def _unit_slots(want_ws: str, want_lane: str) -> dict[str, str]:
        unit_slots: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
            if not decode.ok or decode.identity is None:
                continue
            identity = decode.identity
            if identity.workspace_id != want_ws:
                continue
            if _norm_lane(identity.lane_id) != want_lane:
                continue
            if identity.role not in (GATEWAY_ROLE, WORKER_ROLE):
                continue
            locator = _agent_locator(row)
            if locator and identity.role not in unit_slots:
                unit_slots[identity.role] = locator
                # Placement key for the pair-split verdict (Redmine #13705).
                slot_placements[identity.role] = (
                    _workspace_prefix(locator),
                    _tab_id_of_row(row),
                )
        return unit_slots

    # Shared project workspace model (#13377): the lane unit is (project workspace,
    # recorded lane id). Without a record the shared-model lane id is unknowable from
    # the path alone, so only the legacy unit below can still resolve (fail-safe).
    workspace_id = ""
    lane_id = ""
    slots: dict[str, str] = {}
    record_lane = ""
    if record is not None:
        record_lane = _norm(getattr(record, "lane_id", "")) or _norm(
            getattr(record, "lane_label", "")
        )
    if project_ws and record_lane:
        candidate = _unit_slots(project_ws, _norm_lane(record_lane))
        if candidate:
            workspace_id, lane_id, slots = project_ws, _norm_lane(record_lane), candidate
    if not slots:
        # Legacy compatibility (pre-#13377): the lane's own `wt_<hash>` workspace.
        candidate = _unit_slots(legacy_ws, DEFAULT_LANE)
        if candidate:
            workspace_id, lane_id, slots = legacy_ws, DEFAULT_LANE, candidate
    gateway = slots.get(GATEWAY_ROLE)
    worker = slots.get(WORKER_ROLE)
    if not gateway and not worker:
        return None
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
        lane_id=lane_id,
        lane_label=lane_label,
        issue=issue or None,
        branch=branch,
        repo_root=str(worktree_path),
        gateway_pane=gateway,
        worker_pane=worker,
        state=_lane_state(
            gateway,
            worker,
            gateway_placement=slot_placements.get(GATEWAY_ROLE),
            worker_placement=slot_placements.get(WORKER_ROLE),
        ),
        stale_hints=hints,
    )


__all__ = (
    "GATEWAY_SLOT_MISSING_HINT",
    "LANE_RECORD_MISSING_HINT",
    "LANE_SLOTS_MISSING_HINT",
    "WORKER_SLOT_MISSING_HINT",
    "herdr_lane_view_for_worktree",
    "herdr_sublane_views",
    "is_git_worktree_root",
    "list_herdr_agent_rows",
    "probe_worktree_resolved",
    "project_herdr_sublanes",
    "repo_backend_is_herdr",
    "repo_scope_workspace_id",
)
