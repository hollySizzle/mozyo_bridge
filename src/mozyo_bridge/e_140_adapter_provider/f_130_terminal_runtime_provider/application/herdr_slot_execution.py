"""The per-slot actuation of a session-start run (Redmine #13948).

The last piece of :mod:`herdr_session_start` that was neither a pure decision
(:mod:`herdr_lane_topology`), a pure argv assembly (:mod:`herdr_launch_argv`), a pure
value (:mod:`herdr_session_result`), nor a bare herdr command (:mod:`herdr_pane_lifecycle`):
the function that turns ONE classified slot plan into one adopted / surfaced / launched
outcome. Homed here so the composition root keeps only orchestration — and so it stays
under the 1000-line module-health gate while #13948 adds the startup-health probe and the
startup-transaction binding (Answer j#80989: "new module, do not grow the modules already
near the ceiling").

Behaviour-preserving by construction: the function, its fail-closed guards and its
returned values are the same objects that were there before, only relocated.

What it does NOT do is as load-bearing as what it does. Every `raise` here fires BEFORE
any reclaim, and none of them closes anything: a launch that lands in the wrong workspace
or hands back a malformed locator is surfaced, not cleaned up behind the operator's back.
Closing what a run started is the explicit public rollback rail's authority alone
(Answer j#80991).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_native_identity_binding import (
    HerdrNativeIdentityBinding,
    native_name_for,
)

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
    _parse_started_agent_identity,
    _workspace_prefix,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
    build_agent_start_argv,
    build_pane_launch_env,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
    resolve_launch_lane_epoch,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _invoke,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_bound_launch import (  # noqa: E501
    split_prepared_pane,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SLOT_PLANNED,
    SLOT_STALE,
    SLOT_UNATTESTED,
    SlotResult,
    _SlotPlan,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    Runner,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
    ResolvedProviderLaunch,
)


def _execute_slot(
    plan: _SlotPlan,
    *,
    repo_root: Path,
    workspace_id: str,
    lane: str,
    target_workspace: str,
    target_tab: str = "",
    split: str = "",
    focus: bool = False,
    anchor_locator: str = "",
    binary: str,
    attest_launcher: str = "",
    store_home: str = "",
    env: Mapping[str, str],
    runner: Runner,
    timeout: float,
    resolved: Optional[ResolvedProviderLaunch] = None,
    launch_argv_extra: Sequence[str] = (),
    order_deferred: bool = False,
    replacement_action_id: str = "",
    action_id: str = "",
    prepared_callback: Optional[Callable[..., None]] = None,
    native_binding: Optional[HerdrNativeIdentityBinding] = None,
    shim_dir: str = "",
) -> SlotResult:
    if plan.kind == "adopt":
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_ADOPTED,
            locator=plan.locator,
            detail="live agent already carries the durable name; adopted",
        )
    if plan.kind == "planned":
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_PLANNED,
            detail="would launch (dry-run)",
        )
    if plan.kind == "stale":
        # A host-restart shell-residue slot (Redmine #13518 j#75329): surfaced read-only with
        # its residue locator so an owner-approved recovery (j#75331) can close that exact pane
        # and relaunch the same slot — this run performs no destructive side effect.
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_STALE,
            locator=plan.locator,
            detail=(
                "durable name held by a shell-residue pane with no live agent; requires an "
                "owner-approved close + same-slot relaunch (dirty worktree preserved)"
            ),
        )
    if plan.kind == "unattested":
        # A live slot whose startup self-attestation is absent / stale / missing /
        # conflicting (Redmine #13637): surfaced read-only with the exact fail-closed
        # reason and its live locator. herdr cannot read or repair a running process's
        # env, so recovery is an OWNER-approved close + same-slot relaunch (which re-runs
        # the self-check and writes a fresh present record) — never an automatic
        # destructive repair here.
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_UNATTESTED,
            locator=plan.locator,
            detail=(
                f"{plan.detail}; requires an owner-approved close + same-slot relaunch "
                "(the relaunch re-runs the startup self-attestation self-check)"
            ),
        )
    # Launch with the durable name at start; the full `agent start` argv (self-identity
    # `--env`, `MOZYO_HERDR_BINARY`, `--permission-mode`, config tokens, Codex `-c`
    # overrides, lane `--tab`, and the #13637 self-attestation wrap) is assembled by the
    # cohesive sibling `herdr_launch_argv.build_agent_start_argv`.
    #
    # `env` is threaded in so argv[0] resolves to the provider's verified absolute
    # executable from the SAME trusted environment the launch itself runs under
    # (Redmine #13441): resolving against a different env than the one handed to
    # `_invoke` would verify one binary and exec another. An unresolvable / ambiguous
    # provider binary raises here — before `agent start` runs — so a failed resolution
    # never leaves a live pane behind.
    if resolved is None:
        raise HerdrSessionStartError(
            f"launch plan for {plan.provider!r} has no preflighted provider executable"
        )
    if prepared_callback is None:
        raise HerdrSessionStartError(
            "Herdr 0.8 launch has no startup participant recorder; refuse to create an "
            "untracked pane"
        )
    if not shim_dir:
        raise HerdrSessionStartError(
            "Herdr 0.8 launch has no preflighted provider shim; refuse to create a "
            "pane after an incomplete whole-plan preflight"
        )
    if (
        native_binding is None
        or native_binding.logical_name != plan.assigned_name
        or native_binding.native_name != native_name_for(plan.assigned_name)
    ):
        raise HerdrSessionStartError(
            "the complete launch preflight supplied no matching collision-checked "
            "Herdr native identity; no pane was created"
        )
    binding = native_binding
    lane_epoch = resolve_launch_lane_epoch(workspace_id, lane, store_home=store_home)
    pane_env = build_pane_launch_env(
        provider=plan.provider,
        native_name=binding.native_name,
        workspace_id=workspace_id,
        lane=lane,
        binary=binary,
        shim_dir=shim_dir,
        source_path=str(env.get("PATH") or ""),
        resolved=resolved,
        attest_launcher=attest_launcher,
        store_home=store_home,
        action_id=action_id,
        lane_epoch=lane_epoch,
    )
    prepared = split_prepared_pane(
        binary=binary,
        anchor_locator=anchor_locator,
        direction=split or "right",
        repo_root=repo_root,
        env_entries=pane_env,
        runner=runner,
        timeout=timeout,
        env=env,
    )
    # This callback is intentionally adjacent to the successful split parse.  Herdr
    # exposes no atomic "split + durable receipt" primitive, so this is the smallest
    # possible unrecorded interval; agent start is never attempted before it returns.
    prepared_callback(
        role=plan.provider,
        assigned_name=plan.assigned_name,
        locator=prepared.locator,
        native_name=binding.native_name,
        target_workspace=prepared.workspace_id,
        target_tab=prepared.tab_id,
    )
    # Record the exact returned pane before validating its placement. A mislocated
    # split is still this run's side effect and must remain reachable by rollback.
    if prepared.workspace_id != target_workspace:
        raise HerdrSessionStartError(
            "herdr pane split landed outside the requested workspace; the exact pane "
            "was recorded and agent start was refused"
        )
    if target_tab and prepared.tab_id != target_tab:
        raise HerdrSessionStartError(
            "herdr pane split landed outside the requested tab; the exact pane was "
            "recorded and agent start was refused"
        )
    launch_argv = build_agent_start_argv(
        assigned_name=plan.assigned_name,
        native_name=binding.native_name,
        pane_locator=prepared.locator,
        provider=plan.provider,
        workspace_id=workspace_id,
        lane=lane,
        attest_launcher=attest_launcher,
        resolved=resolved,
        launch_argv_extra=launch_argv_extra,
        replacement_action_id=replacement_action_id,
        action_id=action_id,
        lane_epoch=lane_epoch,
    )
    started = _invoke(
        binary,
        launch_argv,
        runner,
        max(timeout, 31.0),
        env=dict(env),
    )
    started_agent = _parse_started_agent_identity(started.stdout)
    if started_agent is None:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} returned no usable live locator "
            "(expected result.agent.pane_id in an agent_started payload); refuse to "
            "return a blank handle"
        )
    locator, landed_tab, returned_native_name = started_agent
    if returned_native_name != binding.native_name:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} returned a different native "
            "identity than the collision-checked launch binding"
        )
    if not valid_target(locator):
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} returned an invalid live locator "
            f"{locator!r}; refuse to return a malformed handle"
        )
    # Verify the launch actually landed in the requested workspace (Redmine #13330
    # review j#73231). Passing `--workspace` is what keeps herdr from auto-creating a
    # second workspace (with its own empty base pane); if the returned locator is in a
    # DIFFERENT workspace (herdr ignored the flag / spec drift), trusting it would let
    # us close our created root pane while an auto-created base pane survives elsewhere,
    # unseen — exactly the failure this US must prevent. Fail closed instead (before
    # any reclaim), so the mislocated launch is surfaced rather than papered over.
    if locator != prepared.locator:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} reported pane {locator!r} after "
            f"the startup action prepared {prepared.locator!r}; refuse a changed target"
        )
    landed = _workspace_prefix(locator)
    if landed != target_workspace:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} landed in workspace "
            f"{landed or '<none>'!r} but --workspace {target_workspace!r} was requested; "
            "refuse to trust a mislocated launch (herdr may have auto-created another "
            "workspace with its own base pane)"
        )
    # Verify the launch actually landed in the requested TAB (Redmine #13411 review
    # j#74434 finding 2) — the tab-axis analogue of the workspace guard above. When
    # a lane tab is requested (`--tab`, non-default lane), the `agent_started`
    # envelope returns the landed `tab_id` (live probe #13411 j#74434); if herdr
    # ignored / misplaced `--tab` and landed in a DIFFERENT tab of the same
    # workspace, trusting it would leave the pair split and let us reclaim this run's
    # tab root pane against a mislocated launch. A missing tab id is equally
    # unverifiable, so both fail closed before any reclaim. The default lane passes
    # no `target_tab`, so this guard is skipped and its behaviour is byte-invariant.
    if target_tab and landed_tab != target_tab:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} landed in tab "
            f"{landed_tab or '<none>'!r} but --tab {target_tab!r} was requested; "
            "refuse to trust a mislocated launch (the gateway/worker pair must "
            "share one dedicated lane tab)"
        )
    # `order_deferred` (see `slot_placement`): the configured primary could only be placed
    # as a split beside an already-live sibling, so the physical order waits for a full
    # relaunch. Say so rather than silently claim the order was applied.
    detail = (
        "launched in an action-recorded Herdr 0.8 pane; logical identity and "
        "self-identity env retained"
    )
    if order_deferred:
        detail += "; order_deferred_until_full_relaunch (no swap/bounce)"
    return SlotResult(
        provider=plan.provider,
        assigned_name=plan.assigned_name,
        outcome=SLOT_LAUNCHED,
        locator=locator,
        detail=detail,
    )


__all__ = ("_execute_slot",)
