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
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
    _parse_started_agent,
    _workspace_prefix,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
    build_agent_start_argv,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _invoke,
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
    launch_argv = build_agent_start_argv(
        assigned_name=plan.assigned_name,
        provider=plan.provider,
        repo_root=repo_root,
        workspace_id=workspace_id,
        lane=lane,
        target_workspace=target_workspace,
        target_tab=target_tab,
        split=split,
        focus=focus,
        binary=binary,
        attest_launcher=attest_launcher,
        store_home=store_home,
        resolved=resolved,
        launch_argv_extra=launch_argv_extra,
        replacement_action_id=replacement_action_id,
        action_id=action_id,
    )
    started = _invoke(
        binary,
        launch_argv,
        runner,
        timeout,
        env=dict(env),
    )
    started_agent = _parse_started_agent(started.stdout)
    if started_agent is None:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} returned no usable live locator "
            "(expected result.agent.pane_id in an agent_started payload); refuse to "
            "return a blank handle"
        )
    locator, landed_tab = started_agent
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
    detail = "launched with the durable name and self-identity env (--env) at start"
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
