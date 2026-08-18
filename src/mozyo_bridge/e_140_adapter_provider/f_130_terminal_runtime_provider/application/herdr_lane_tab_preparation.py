"""Lane-container preparation for a session-start run (#13411 / #15702 / #15705).

The composition root's one call for the lane=tab axis: which herdr tab this run's
non-default-lane launches join, and — when the run must mint a fresh tab — the tab
created so its born root pane IS the first launch slot's prepared pane. Redmine
#15705 adds the workspace axis of the same occupation: a DEFAULT-lane run that must
mint a fresh workspace (its container is the workspace's own root tab — there is no
lane tab) mints it first-slot-prepared through :func:`workspace_root_minter`, so the
coordinator pair's workspace root is occupied exactly like a lane tab's root.

Why occupation instead of reclaim-by-close (Redmine #15702): the pre-#15227 runtime
closed the lane tab's empty root pane after every launch succeeded; #15227 removed that
close because ``pane close <locator>`` cannot atomically condition on terminal
generation, and the root became a permanent empty shell column beside every lane pair.
A shell root can never carry the fresh-v4 + generation-v2 authority a managed close
requires, so any post-append close would be a locator-based check-then-close — the
exact TOCTOU shape #15227 eliminated. Creating the tab with the first slot's ``--cwd`` /
``--env`` (measured herdr 0.8.0: both reach the root shell, and ``tab_created``
returns the root's full pane identity including ``terminal_id``) removes the empty pane
with ZERO destructive operations: the root is born prepared and the first ``agent
start`` lands in it under the same exact-identity checks a split pane gets.

Join / adopt / heal / restore paths are unchanged: a tab pinned by live slots, a loose
legacy pair, or a restore container never mints a tab here, so nothing is occupied.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_native_identity_binding import (
    HerdrNativeIdentityBinding,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
    _lane_live_slot_tabs,
    _tab_target_for_lane,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
    build_pane_launch_env,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
    resolve_launch_lane_epoch,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_bound_launch import (  # noqa: E501
    PreparedPane,
    create_prepared_lane_tab,
    create_prepared_workspace,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    Runner,
)


@dataclass(frozen=True)
class LaneTabResolution:
    """The lane's resolved tab join plus the fresh-tab occupation target (pure value).

    - :attr:`target_tab` — the tab this run's launches join (``""`` = loose legacy pair).
    - :attr:`lane_slot_tabs` — the lane's live slot tabs (the fresh-vs-loose basis).
    - :attr:`occupy` — the run-created tab's prepared root pane the FIRST launch slot
      starts in (``None`` on every joined / loose / restore path). Its ``locator`` is
      also the run's ``tab_pane_id`` observation.
    """

    target_tab: str
    lane_slot_tabs: list
    occupy: Optional[PreparedPane]


def _first_slot_env_entries(
    *,
    first_launch_plan,
    native_bindings: Mapping[str, HerdrNativeIdentityBinding],
    resolved_launches: Mapping[str, object],
    workspace_id: str,
    lane_id: str,
    binary: str,
    env: Mapping[str, str],
    attest_launcher: str,
    store_home: str,
    action_id: str,
    surface: str,
) -> "list[str]":
    """The first launch slot's pane env, from the same preflighted inputs
    ``_execute_slot`` uses — built BEFORE the container side effect so every
    resolution error fires with zero herdr write. Shared by the lane-tab (#15702)
    and workspace-root (#15705) occupation paths so the env a root pane is born
    with is exactly the env a split pane for that slot would have carried.
    """
    binding = native_bindings.get(first_launch_plan.assigned_name)
    resolved = resolved_launches.get(first_launch_plan.provider)
    if binding is None or resolved is None:
        raise HerdrSessionStartError(
            f"{surface} has no preflighted native binding / provider for the first "
            f"launch slot; refuse to mint a container for an unresolved launch"
        )
    return build_pane_launch_env(
        provider=first_launch_plan.provider,
        native_name=binding.native_name,
        workspace_id=workspace_id,
        lane=lane_id,
        binary=binary,
        source_path=str(env.get("PATH") or ""),
        resolved=resolved,
        attest_launcher=attest_launcher,
        store_home=store_home,
        action_id=action_id,
        lane_epoch=resolve_launch_lane_epoch(
            workspace_id, lane_id, store_home=store_home
        ),
    )


def workspace_root_minter(
    *,
    first_launch_plan,
    native_bindings: Mapping[str, HerdrNativeIdentityBinding],
    resolved_launches: Mapping[str, object],
    attest_launcher: str,
    store_home: str,
    action_id: str,
    workspace_id: str,
    lane_id: str,
    repo_root: Path,
    binary: str,
    env: Mapping[str, str],
    runner: Runner,
    timeout: float,
    effect_fence: Optional[Callable[[], None]] = None,
) -> "Callable[[str], tuple[str, PreparedPane]]":
    """A ``mint(label) -> (workspace_id, prepared root)`` closure (Redmine #15705).

    The DEFAULT lane has no lane tab: its container is the minted workspace's own
    root tab, so the fresh-workspace mint is where occupation must happen. The
    session-start composition root builds this once (after the whole-plan preflight
    resolved the first launch slot) and each fresh-mint site — the per-project
    default pair, the shared coordinators space, the role-grouped project
    coordinators space — calls it with only its label, keeping every identity /
    env decision in this module. The first slot's env is built lazily inside the
    call, still BEFORE the workspace side effect. Container placement is NOT
    re-validated here — the occupying slot's execution records the prepared root
    in the startup transaction FIRST and then applies the same fail-closed
    workspace landing checks a split pane gets (the ``_execute_slot`` parity rule).
    """

    def _mint(label: str) -> "tuple[str, PreparedPane]":
        entries = _first_slot_env_entries(
            first_launch_plan=first_launch_plan,
            native_bindings=native_bindings,
            resolved_launches=resolved_launches,
            workspace_id=workspace_id,
            lane_id=lane_id,
            binary=binary,
            env=env,
            attest_launcher=attest_launcher,
            store_home=store_home,
            action_id=action_id,
            surface="prepared workspace creation",
        )
        return create_prepared_workspace(
            binary=binary,
            label=label,
            repo_root=repo_root,
            env_entries=entries,
            runner=runner,
            timeout=timeout,
            env=env,
            effect_fence=effect_fence,
        )

    return _mint


def resolve_lane_tab(
    *,
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    target_workspace: str,
    lane_id: str,
    restore_tab: Optional[str],
    first_launch_plan,
    native_bindings: Mapping[str, HerdrNativeIdentityBinding],
    resolved_launches: Mapping[str, object],
    attest_launcher: str,
    store_home: str,
    action_id: str,
    repo_root: Path,
    binary: str,
    env: Mapping[str, str],
    runner: Runner,
    timeout: float,
    effect_fence: Optional[Callable[[], None]] = None,
) -> LaneTabResolution:
    """Resolve the lane's tab, minting a first-slot-prepared tab when none exists.

    The relocated #13411 decision the session-start composition root used to hold
    verbatim (live tabs → restore/own-tab pin → fresh mint), with the fresh mint
    upgraded to :func:`create_prepared_lane_tab` (#15702). The first launch slot's
    pane env is built HERE — before the tab exists — from the same builder and the
    same preflighted inputs ``_execute_slot`` uses, so the env the root pane is born
    with is exactly the env a split pane for that slot would have carried. Every
    resolution error therefore fires before the tab side effect.
    """
    lane_slot_tabs = _lane_live_slot_tabs(
        rows, workspace_id, target_workspace, lane_id
    )
    target_tab = (
        restore_tab
        if restore_tab is not None
        else _tab_target_for_lane(rows, workspace_id, target_workspace, lane_id)
    )
    if target_tab or lane_slot_tabs:
        return LaneTabResolution(target_tab, lane_slot_tabs, None)
    entries = _first_slot_env_entries(
        first_launch_plan=first_launch_plan,
        native_bindings=native_bindings,
        resolved_launches=resolved_launches,
        workspace_id=workspace_id,
        lane_id=lane_id,
        binary=binary,
        env=env,
        attest_launcher=attest_launcher,
        store_home=store_home,
        action_id=action_id,
        surface="prepared lane tab creation",
    )
    # Container placement is NOT re-validated here: the occupying slot's execution
    # records the prepared root in the startup transaction FIRST and then applies the
    # same fail-closed workspace / tab landing checks a split pane gets, so even a
    # mislocated root stays reachable by rollback (the `_execute_slot` parity rule).
    target_tab, prepared = create_prepared_lane_tab(
        binary=binary,
        workspace_id=target_workspace,
        label=lane_id,
        repo_root=repo_root,
        env_entries=entries,
        runner=runner,
        timeout=timeout,
        env=env,
        effect_fence=effect_fence,
    )
    return LaneTabResolution(target_tab, lane_slot_tabs, prepared)


__all__ = ("LaneTabResolution", "resolve_lane_tab", "workspace_root_minter")
