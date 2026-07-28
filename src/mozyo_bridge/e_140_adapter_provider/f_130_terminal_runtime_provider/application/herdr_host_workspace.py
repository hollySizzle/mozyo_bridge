"""herdr WORKSPACE resolution for a launch (Redmine #13330 / #13377 / #13380 / #14139).

The outer of the two container axes a launch resolves, and the sibling of
:mod:`herdr_shared_tab` (the inner, tab axis): *which herdr terminal workspace do this
run's launches join, and did we have to create it*. Extracted from ``herdr_session_start``
as a leaf so the orchestrator holds orchestration only — both axes now answer through one
call each — and so that module stays inside its module-health budget. It is a boundary
move: the resolution, its order, its fail-closed messages, and the single-flight fence
handling arrived unchanged.

The pure join rules live one layer down in :mod:`herdr_lane_topology`
(:func:`_launch_target_for_lane`, :func:`_shared_coordinator_target`); this module is the
composition that runs them against the live inventory and issues the ``workspace list`` /
``workspace create`` commands under the right fence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.core.state.coordinator_placement_fence import (
    CoordinatorSharedCreateLockUnavailable,
    CoordinatorSharedCreateReleaseError,
    coordinator_shared_create_lock,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    SHARED_COORDINATOR_WORKSPACE_LABEL,
    HerdrSessionStartError,
    _host_workspace_label,
    _launch_target_for_lane,
    _shared_coordinator_own_target,
    _shared_coordinator_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _create_workspace,
    _list_workspace_labels,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.coordinator_placement_mode import (  # noqa: E501
    SHARED_SPACE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DEFAULT_LANE,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


def resolve_host_workspace(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    lane_id: str,
    *,
    launching: bool,
    adopt_locators: Sequence[str],
    coordinator_placement_mode: str,
    repo_root: Path,
    resolved_root: Path,
    binary: str,
    runner,
    timeout: float,
    env: Mapping[str, str],
    home: "Optional[Path]" = None,
) -> "tuple[str, str]":
    """The herdr workspace this run's launches join: ``(workspace_id, created_base_pane)``.

    ``created_base_pane`` is the empty root pane of a workspace this run CREATED (``""``
    when one was adopted or none was needed), so the caller reclaims exactly what it made
    and never a pane it merely found.

    Nothing to launch (``launching`` False — all adopt / dry-run) means no ``workspace
    create`` and no reclaim: returns ``("", "")``, byte-invariant.

    Placement is lane-aware (#13380 dedicated sublane host): a lane's own live/adopted
    slots pin the target first (a heal never splits a pair); otherwise a lane slot joins
    the sublane host workspace the other lane slots occupy (never the coordinator's), and
    the default lane joins only its own pins — one mozyo workspace thus occupies a constant
    "project 1 + host 1" herdr workspaces. When nothing pins a target the workspace is
    created explicitly (labelled for a lane slot) so its empty root pane is a known handle
    to reclaim, not one we scan for.

    Operator placement mode (Redmine #14139): in ``shared_space`` mode the DEFAULT lane
    (coordinator pair) instead joins one stable shared coordinators workspace across
    projects (:func:`_shared_coordinator_target`), created with the stable
    :data:`SHARED_COORDINATOR_WORKSPACE_LABEL`. Only the default lane in shared mode
    diverges; ``per_project_space`` (the default) and every sublane path stay byte-for-byte
    the pre-#14139 resolution — the shared branch is never taken for a lane slot, so the
    #13380 / #13411 / #14567 sublane axes are untouched.
    """
    if not launching:
        return "", ""
    base_pane_id = ""
    shared_coordinator_space = (
        coordinator_placement_mode == SHARED_SPACE and lane_id == DEFAULT_LANE
    )
    if shared_coordinator_space:
        # The shared coordinators space is identified by its stable LABEL, the
        # backend-readable authority (Redmine #14139 review j#83383 F1 / Design
        # Answer j#83385 Decision 1) — never a locator-prefix guess that would
        # adopt a per-project coordinator window on a mode transition.
        #
        # Resolve this project's OWN pin FIRST (R4 review j#83473 F2): an own-pin
        # heal rejoins its own live space by identity and must NOT depend on the
        # `workspace list` command succeeding, so the label read is skipped when
        # an own pin exists. Only a fresh / mode-transition launch with no own pin
        # reads the labels — and per_project / sublane launches never reach here,
        # so they issue no extra `workspace list` (byte-invariant).
        target_workspace = _shared_coordinator_own_target(
            rows, workspace_id, adopt_locators
        )
        if not target_workspace:
            # No own pin -> the shared space must be adopted or created. Run the
            # whole list->resolve->create under a home-scoped single-flight fence
            # (R5 review j#83516 F1) so concurrent clean-slate launches converge to
            # ONE workspace: the first creates it under the lock; the rest wait,
            # re-read the labels under the lock and ADOPT it (double-checked). A
            # partial-failure husk is adopted the same way (resolver F1). Own-pin
            # heal above never takes the lock (it creates nothing). Unreadable
            # labels / ambiguity / mode-transition all fail closed in the resolver.
            #
            # The fence's ACQUISITION runs before any herdr command, so an
            # acquisition failure is zero-actuation; its RELEASE runs AFTER the
            # body, so on the clean-slate path the shared `workspace create` has
            # already happened. Both convert into the launch's typed error boundary
            # (no raw traceback at the CLI, R6 review j#83569 F2), but the message
            # must be phase-accurate: an acquisition failure created nothing, while
            # a release failure may have left a labelled `coordinators` workspace a
            # re-run adopts idempotently (R8 review j#83633 F1).
            try:
                with coordinator_shared_create_lock(home or mozyo_bridge_home()):
                    workspace_labels = _list_workspace_labels(binary, runner, timeout)
                    target_workspace = _shared_coordinator_target(
                        rows,
                        workspace_id,
                        adopt_locators,
                        workspace_labels,
                        SHARED_COORDINATOR_WORKSPACE_LABEL,
                    )
                    if not target_workspace:
                        target_workspace, base_pane_id = _create_workspace(
                            binary,
                            repo_root,
                            runner,
                            timeout,
                            env,
                            label=SHARED_COORDINATOR_WORKSPACE_LABEL,
                        )
            except CoordinatorSharedCreateReleaseError as exc:
                # Release runs AFTER the body: the shared workspace was already
                # resolved (created on a clean slate, or adopted), and the
                # coordinator agents were NOT started. A labelled `coordinators`
                # workspace may exist as an empty husk; a re-run adopts it
                # idempotently (no duplicate is created).
                raise HerdrSessionStartError(
                    "managed-launch admission resolved the shared coordinators "
                    f"workspace but could not release the single-flight lock ({exc}); "
                    "the coordinator agents were NOT started. A labelled "
                    "'coordinators' workspace may have been created and remain as an "
                    "empty husk — re-run to adopt it idempotently (no duplicate is "
                    "created)."
                ) from exc
            except CoordinatorSharedCreateLockUnavailable as exc:
                raise HerdrSessionStartError(
                    "managed-launch admission could not acquire the shared "
                    f"coordinators single-flight lock ({exc}); no workspace / tab / "
                    "agent was created. Re-run once the home lock is reachable."
                ) from exc
    else:
        target_workspace = _launch_target_for_lane(
            rows,
            workspace_id,
            lane_id,
            adopt_locators,
        )
        if not target_workspace:
            create_label = (
                _host_workspace_label(resolved_root) if lane_id != DEFAULT_LANE else ""
            )
            target_workspace, base_pane_id = _create_workspace(
                binary,
                repo_root,
                runner,
                timeout,
                env,
                label=create_label,
            )
    return target_workspace, base_pane_id


__all__ = ("resolve_host_workspace",)
