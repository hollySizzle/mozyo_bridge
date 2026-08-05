"""Role-aware Herdr workspace placement for coordinator lanes (Redmine #14996).

``role_grouped_space`` keeps the explicitly identified top coordinator in its
dedicated workspace, collects every project coordinator in one labelled
workspace, and leaves implementation lanes on the existing per-project
sublane-host path. The operator's stable logical ``top_workspace_id`` plus the
durable ``lane_kind`` are the role authority; pane position, provider, repo name,
and display labels are never promoted into role authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from mozyo_bridge.core.state.coordinator_placement_fence import (
    CoordinatorSharedCreateLockUnavailable,
    CoordinatorSharedCreateReleaseError,
    coordinator_shared_create_lock,
)
from mozyo_bridge.core.state.lane_kind import (
    LANE_KIND_COORDINATOR,
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.coordinator_placement_loader import (  # noqa: E501
    validate_top_workspace_reference,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    ContainerPlan,
    HerdrSessionStartError,
    _lane_live_slot_tabs,
    _launch_target_for_lane,
    _workspace_prefix,
    resolve_focus_first_launch,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    _create_workspace,
    _list_workspace_labels,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (
    SLOT_STALE,
    classify_named_slot,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


#: Exact Herdr label used solely as the adopt authority for the shared project-
#: coordinator workspace.  It is deliberately distinct from ``coordinators`` so
#: changing modes cannot silently adopt the legacy all-coordinators workspace.
PROJECT_COORDINATOR_WORKSPACE_LABEL = "project-coordinators"


def require_registered_top_workspace(top_workspace_id: str, *, home: Path) -> None:
    """Prove the role-grouped top authority against the workspace registry."""
    validate_top_workspace_reference(
        top_workspace_id, home=home, error_type=HerdrSessionStartError
    )


def is_role_grouped_project_coordinator(
    lane_id: str,
    lane_kind: object,
    *,
    workspace_id: str,
    top_workspace_id: str,
) -> bool:
    """Validate the role-grouped lane fact and identify a project coordinator.

    Every default lane requires the canonical coordinator kind. The one whose
    stable logical workspace id exactly equals the operator-configured top id is
    the top coordinator; every other default coordinator is a project
    coordinator. A named lane cannot be placed without a durable kind:
    ``delegated_coordinator`` joins the shared project-coordinator workspace,
    while ``implementation`` retains the existing host/tab path. Contradictions
    fail before any Herdr actuation.
    """
    lane = _norm(lane_id) or DEFAULT_LANE
    workspace = _norm(workspace_id)
    top_workspace = _norm(top_workspace_id)
    if not workspace or not top_workspace:
        raise HerdrSessionStartError(
            "role_grouped_space requires non-empty current and top stable workspace "
            "identities. No workspace / tab / agent was created."
        )
    if lane == DEFAULT_LANE:
        if lane_kind == LANE_KIND_COORDINATOR:
            return workspace != top_workspace
        raise HerdrSessionStartError(
            "role_grouped_space requires every default lane to be a coordinator "
            f"({LANE_KIND_COORDINATOR!r}), got lane-kind {lane_kind!r}. No workspace / "
            "tab / agent was created."
        )
    if lane_kind == LANE_KIND_DELEGATED_COORDINATOR:
        return True
    if lane_kind == LANE_KIND_IMPLEMENTATION:
        return False
    if lane_kind is None:
        detail = "has no durable lane-kind"
    else:
        detail = f"has lane-kind {lane_kind!r}"
    raise HerdrSessionStartError(
        f"role_grouped_space cannot place named lane {lane!r}: it {detail}; expected "
        f"{LANE_KIND_DELEGATED_COORDINATOR!r} (shared project coordinators) or "
        f"{LANE_KIND_IMPLEMENTATION!r} (per-project implementation host). No "
        "workspace / tab / agent was created."
    )


def classify_role_grouped_placement(
    *,
    lane_id: str,
    lane_kind: object,
    workspace_id: str,
    top_workspace_id: str,
) -> tuple[bool, bool]:
    """Return ``(project_coordinator, implementation)`` from preflighted identities."""
    project = is_role_grouped_project_coordinator(
        lane_id,
        lane_kind,
        workspace_id=workspace_id,
        top_workspace_id=top_workspace_id,
    )
    return project, (_norm(lane_id) or DEFAULT_LANE) != DEFAULT_LANE and not project


def _shared_project_coordinator_own_target(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    lane_id: str,
    adopted_locators: Sequence[str],
) -> str:
    """Return this exact project-coordinator lane's live workspace pin, if any."""
    lane = _norm(lane_id) or DEFAULT_LANE
    own = [locator for locator in adopted_locators if locator]
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decoded.ok or decoded.identity is None:
            continue
        identity = decoded.identity
        if identity.workspace_id != workspace_id:
            continue
        if (identity.lane_id or DEFAULT_LANE) != lane:
            continue
        locator = _agent_locator(row)
        if locator:
            own.append(locator)
    prefixes = {prefix for prefix in map(_workspace_prefix, own) if prefix}
    if len(prefixes) > 1:
        raise HerdrSessionStartError(
            f"live project-coordinator slots of lane {lane!r} in workspace "
            f"{workspace_id!r} span multiple herdr workspaces {sorted(prefixes)!r}; "
            "refuse to guess which one new launches belong to"
        )
    return next(iter(prefixes)) if prefixes else ""


def validate_role_grouped_inventory(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    lane_id: str,
    plan_states: Sequence[tuple[str, str]],
    *,
    shared_project_coordinator: bool,
) -> None:
    """Reject stale own identities and split shared-coordinator placement."""
    lane = _norm(lane_id) or DEFAULT_LANE
    stale_own_row = False
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decoded.ok or decoded.identity is None:
            continue
        identity = decoded.identity
        if identity.workspace_id != workspace_id:
            continue
        if (identity.lane_id or DEFAULT_LANE) != lane:
            continue
        if classify_named_slot(row) == SLOT_STALE:
            stale_own_row = True
            break
    if stale_own_row or any(kind == "stale" for kind, _ in plan_states):
        raise HerdrSessionStartError(
            "role_grouped_space found a stale identity in the requested lane; recover or "
            "retire it before relaunch. No workspace / tab / agent was created."
        )
    if not shared_project_coordinator:
        return
    _shared_project_coordinator_own_target(
        rows,
        workspace_id,
        lane_id,
        [locator for kind, locator in plan_states if kind == "adopt"],
    )


def resolve_role_grouped_implementation_target(
    *,
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    lane_id: str,
    adopted_locators: Sequence[str],
    binary: str,
    runner,
    timeout: float,
) -> str:
    """Resolve an implementation host without joining project coordinators.

    An existing implementation lane keeps its own workspace pin. A fresh lane
    excludes every row located in the exact-labelled project-coordinator workspace
    before applying the established sublane-host resolver, so a project coordinator
    from the same repository cannot be mistaken for an implementation sibling.
    """
    own_target = _shared_project_coordinator_own_target(
        rows, workspace_id, lane_id, adopted_locators
    )
    if own_target:
        return own_target
    labels = _list_workspace_labels(binary, runner, timeout)
    project_coordinators = _shared_project_coordinator_target(
        labels, workspace_id=workspace_id
    )
    eligible_rows = tuple(
        row
        for row in rows
        if not project_coordinators
        or _workspace_prefix(_agent_locator(row)) != project_coordinators
    )
    return _launch_target_for_lane(
        eligible_rows, workspace_id, lane_id, adopted_locators
    )


def _shared_project_coordinator_target(
    workspace_labels: Mapping[str, str] | None,
    *,
    workspace_id: str,
    shared_label: str = PROJECT_COORDINATOR_WORKSPACE_LABEL,
) -> str:
    """Resolve the one exact-labelled project-coordinator workspace, or create signal.

    Unlike legacy ``shared_space``, unrelated default-lane workspaces are expected:
    the top coordinator deliberately occupies one.  Therefore only the exact label
    can authorize a cross-project adopt; inventory proximity is never consulted.
    """
    if workspace_labels is None:
        raise HerdrSessionStartError(
            "shared project-coordinator workspace labels are unreadable; refuse to "
            f"guess the shared space for workspace {workspace_id!r}"
        )
    candidates = sorted(
        workspace
        for workspace, label in workspace_labels.items()
        if label == shared_label
    )
    if len(candidates) > 1:
        raise HerdrSessionStartError(
            "multiple herdr workspaces carry the shared project-coordinator label "
            f"{shared_label!r} ({candidates!r}); refuse to guess which one is canonical"
        )
    return candidates[0] if candidates else ""


@dataclass(frozen=True)
class SharedProjectCoordinatorWorkspace:
    """Resolved workspace plus the root pane created by this run, when any."""

    workspace_id: str
    base_pane_id: str = ""


def resolve_project_coordinator_workspace(
    *,
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    lane_id: str,
    adopted_locators: Sequence[str],
    binary: str,
    repo_root: Path,
    runner,
    timeout: float,
    env: Mapping[str, str],
) -> SharedProjectCoordinatorWorkspace:
    """Resolve/adopt/create the role-grouped shared project-coordinator workspace."""
    own_target = _shared_project_coordinator_own_target(
        rows, workspace_id, lane_id, adopted_locators
    )
    try:
        with coordinator_shared_create_lock(mozyo_bridge_home()):
            labels = _list_workspace_labels(binary, runner, timeout)
            target = _shared_project_coordinator_target(
                labels, workspace_id=workspace_id
            )
            if own_target:
                # Launch-time-only migration rule: a live lane stays pinned until it
                # is explicitly retired and relaunched; this function never moves a
                # live pane.  The global exact-label authority must still be readable
                # and singular before any heal, otherwise a duplicate shared surface
                # would survive undetected merely because this project has an own pin.
                return SharedProjectCoordinatorWorkspace(own_target)
            if target:
                return SharedProjectCoordinatorWorkspace(target)
            target, base_pane = _create_workspace(
                binary,
                repo_root,
                runner,
                timeout,
                env,
                label=PROJECT_COORDINATOR_WORKSPACE_LABEL,
            )
            return SharedProjectCoordinatorWorkspace(target, base_pane)
    except CoordinatorSharedCreateReleaseError as exc:
        raise HerdrSessionStartError(
            "managed launch resolved the shared project-coordinator workspace but "
            f"could not release the single-flight lock ({exc}); agents were not "
            "started. A labelled 'project-coordinators' workspace may remain; a "
            "retry adopts it without creating a duplicate."
        ) from exc
    except CoordinatorSharedCreateLockUnavailable as exc:
        raise HerdrSessionStartError(
            "managed launch could not acquire the shared project-coordinator "
            f"single-flight lock ({exc}); no workspace / tab / agent was created."
        ) from exc


def preflight_project_coordinator_label_authority(
    *,
    workspace_id: str,
    binary: str,
    runner,
    timeout: float,
    acquire_lock: bool = True,
) -> None:
    """Prove the global shared-label authority before durable launch reserves.

    This read-only preflight runs even when every requested slot is already
    adopted. A later create-capable resolution repeats the check under the same
    single-flight lock, closing the list/create race without leaving a startup
    action or launch generation behind when the initial authority is ambiguous.
    """
    def _validate() -> None:
        _shared_project_coordinator_target(
            _list_workspace_labels(binary, runner, timeout),
            workspace_id=workspace_id,
        )

    if not acquire_lock:
        _validate()
        return
    try:
        with coordinator_shared_create_lock(mozyo_bridge_home()):
            _validate()
    except CoordinatorSharedCreateReleaseError as exc:
        raise HerdrSessionStartError(
            "managed launch read the shared project-coordinator labels but could "
            f"not release the single-flight lock ({exc}); no workspace / tab / agent "
            "was created and no startup action was reserved."
        ) from exc
    except CoordinatorSharedCreateLockUnavailable as exc:
        raise HerdrSessionStartError(
            "managed launch could not acquire the shared project-coordinator "
            f"single-flight lock ({exc}); no workspace / tab / agent was created "
            "and no startup action was reserved."
        ) from exc


def resolve_project_coordinator_container_plan(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
    lane_id: str,
    *,
    config_split: str | None,
    launch_count: int,
) -> ContainerPlan:
    """Plan one loose project-coordinator pair (a column, never a per-lane tab)."""
    occupancy = len(
        _lane_live_slot_tabs(rows, workspace_id, target_workspace, lane_id)
    )
    split = config_split or ""
    return ContainerPlan(
        split_direction=split,
        occupancy=occupancy,
        focus_first=resolve_focus_first_launch(
            split_direction=split,
            launch_count=launch_count,
            container_occupancy=occupancy,
        ),
    )


__all__ = (
    "PROJECT_COORDINATOR_WORKSPACE_LABEL",
    "SharedProjectCoordinatorWorkspace",
    "_shared_project_coordinator_own_target",
    "_shared_project_coordinator_target",
    "classify_role_grouped_placement",
    "is_role_grouped_project_coordinator",
    "preflight_project_coordinator_label_authority",
    "require_registered_top_workspace",
    "resolve_project_coordinator_container_plan",
    "resolve_project_coordinator_workspace",
    "resolve_role_grouped_implementation_target",
    "validate_role_grouped_inventory",
)
