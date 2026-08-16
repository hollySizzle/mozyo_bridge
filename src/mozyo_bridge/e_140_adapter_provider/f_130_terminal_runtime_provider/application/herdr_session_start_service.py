"""Typed configured-session service shared by CLI and offline restore (#15227).

The CLI presentation must not become a private nonce transport.  This service contains
the existing config/placement composition and lets the exact candidate offline runner
call the same use case in-process with a nonce already sealed in its action record.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.core.state.lane_kind import LANE_KIND_COORDINATOR
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfigError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
    herdr_session_start as _use_case,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.coordinator_placement_loader import (  # noqa: E501
    load_coordinator_placement_for_launch,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.coordinator_placement_mode import (  # noqa: E501
    CoordinatorPlacementError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_context import (  # noqa: E501
    LaneLaunchContext,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_identity import (  # noqa: E501
    PrivateRestoreContainerBinding,
    PrivateWorktreeBinding,
)


def prepare_configured_session(
    *,
    repo_root,
    agents: Sequence[str],
    lane_id: str,
    env: Mapping[str, str],
    dry_run: bool,
    claude_permission_mode_default: str,
    action_nonce: str = "",
    expected_workspace_id: str = "",
    expected_worktree: PrivateWorktreeBinding | None = None,
    restore_container: PrivateRestoreContainerBinding | None = None,
    restore_effect_fence: Callable[[], None] | None = None,
    restore_pane_owner=None,
    restore_startup_overlap_guard: bool = False,
    session_gate_lease=None,
    session_preparer: Callable | None = None,
):
    """Load the two configured launch axes and invoke the canonical use case.

    ``action_nonce`` is an application-only seam.  It is never accepted by argparse,
    rendered, logged, or copied into a provider environment.
    """

    try:
        repo_config = load_repo_local_config(repo_root)
    except RepoLocalConfigError as exc:
        raise HerdrSessionStartError(f"invalid repo-local config: {exc}") from exc
    try:
        coordinator_placement = load_coordinator_placement_for_launch()
    except CoordinatorPlacementError as exc:
        raise HerdrSessionStartError(
            f"invalid operator coordinator placement: {exc}"
        ) from exc
    launch_context = (
        None
        if lane_id
        else LaneLaunchContext(lane_kind=LANE_KIND_COORDINATOR)
    )
    prepare = session_preparer or _use_case.prepare_session
    call = dict(
        repo_root=repo_root,
        providers=list(agents),
        lane_id=lane_id,
        launch_context=launch_context,
        env=dict(env),
        dry_run=dry_run,
        claude_permission_mode_default=claude_permission_mode_default,
        agent_launch=repo_config.agent_launch,
        lane_placement=repo_config.lane_placement,
        coordinator_placement_mode=coordinator_placement.mode,
        coordinator_top_workspace_id=coordinator_placement.top_workspace_id,
        action_nonce=action_nonce,
    )
    if expected_workspace_id:
        # Private equality assertion only.  CLI callers leave it absent; offline restore
        # binds the path-derived identity before registration or launch can occur.
        call["expected_workspace_id"] = expected_workspace_id
    if expected_worktree is not None:
        call["expected_worktree"] = expected_worktree
    if restore_container is not None:
        call["_restore_container"] = restore_container
    if restore_effect_fence is not None:
        call["_restore_effect_fence"] = restore_effect_fence
    if restore_startup_overlap_guard:
        call["_restore_startup_overlap_guard"] = True
    if restore_pane_owner is not None:
        call["_restore_pane_owner"] = restore_pane_owner
    if session_gate_lease is not None:
        call["_session_gate_lease"] = session_gate_lease
    return prepare(**call)


__all__ = ("prepare_configured_session",)
