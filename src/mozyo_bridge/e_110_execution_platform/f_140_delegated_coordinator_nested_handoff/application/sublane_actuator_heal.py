"""Bounded lane self-heal + dispatch retry for a vanished gateway (Redmine #13378).

The measured vanish mode (#13378 j#73606): an **idle, pre-session** gateway agent is
killed by a host-level event — live-measured, a global agent-CLI update
(``npm install -g @openai/codex``) cleanly exits every idle codex TUI — between the
lane launch and the first dispatch, so the ``sublane create --execute`` dispatch fails
with a gateway slot that is gone on read-back. This module holds the one bounded
recovery :class:`~.sublane_actuator_use_case.SublaneActuateUseCase` runs for that mode
(carved out of the use-case module so it stays under the module-health ceiling):

1. applicability — only when the ops adapter offers the *optional*
   ``heal_lane_column`` capability (the herdr adapter does; tmux deliberately not) AND
   the lane read-back shows the gateway slot is no longer resolvable. Anything else
   returns ``None`` so the caller's plain fail-closed block stays byte-for-byte the
   pre-#13378 behaviour;
2. heal — ``heal_lane_column`` relaunches the missing slot(s); the herdr session
   preparation is adopt-or-launch idempotent, so the surviving slot is adopted (its
   locator pins the workspace — the runbook relaunch standard) and only the dead slot
   is relaunched;
3. re-verify — the healed lane must be visible with both panes AND match the requested
   identity (the j#70250 guard applies to a healed lane too);
4. retry — one readiness wait + one dispatch retry. Every failure along the way is a
   fail-closed block; there is never a second heal.

The function drives the use case as a collaborator (its injected ops port, readiness
wait, identity check, and outcome builders) rather than importing it, so the module
dependency stays one-way (use case → this module → domain).
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    DISPATCH_GATEWAY_NOTIFIED,
    REASON_HANDOFF_FAILED,
    REASON_LANE_MISMATCH,
    REASON_PANE_CREATE_FAILED,
    STEP_BLOCKED,
    STEP_EXECUTED,
    STEP_SKIPPED,
    ActuationStep,
    SublaneActuationOutcome,
)


def heal_and_retry_dispatch(
    use_case,
    request,
    launch,
    *,
    steps: list,
    failed_dispatch_detail: str,
    dispatch: bool,
    target_repo: str,
    lane_runtime_root: str,
    adopted: bool,
    fill_decision: Optional[str],
    fill_override_reason: Optional[str],
    gateway_ready: Optional[bool],
) -> Optional[SublaneActuationOutcome]:
    """One bounded self-heal + dispatch retry for a vanished gateway (#13378).

    Returns ``None`` when the heal is not applicable (no ops capability, or the
    gateway is still resolvable — the failure is not the vanish mode), leaving
    ``steps`` untouched so the caller's plain fail-closed block is unchanged. Once
    applicable it always returns a terminal outcome (executed or blocked).

    ``lane_runtime_root`` (#13392) is the root the read-back / relaunch drive against —
    the Git worktree, or a non-git lane's workspace root (never the phantom path).
    """
    heal = getattr(use_case.ops, "heal_lane_column", None)
    if not callable(heal):
        return None
    lane_after = use_case.ops.read_lane(lane_runtime_root)
    if lane_after is not None and lane_after.gateway_pane:
        # The gateway is still resolvable: the dispatch failed for some other
        # reason (route / anchor / rail), which a relaunch cannot fix.
        return None
    steps.append(
        ActuationStep(
            order=5,
            title="dispatch implementation_request",
            status=STEP_BLOCKED,
            detail=f"{failed_dispatch_detail}; gateway pane no longer resolvable "
            "on read-back (vanished before delivery) — attempting one lane "
            "self-heal (#13378)",
            command=use_case._dispatch_command(request),
        )
    )
    try:
        heal(lane_runtime_root)
    except Exception as exc:  # noqa: BLE001 — fail-closed on any heal failure.
        steps.append(
            ActuationStep(
                order=6,
                title="relaunch lane column (self-heal)",
                status=STEP_BLOCKED,
                detail=f"lane self-heal relaunch failed: {exc}",
                command=None,
            )
        )
        return use_case._blocked(
            request,
            launch_action=launch.action,
            reason="gateway vanished before dispatch and the lane self-heal "
            "relaunch failed; fail-closed",
            reasons=(REASON_HANDOFF_FAILED, REASON_PANE_CREATE_FAILED),
            dispatch=dispatch,
            steps=tuple(steps),
            worker_pane=(lane_after.worker_pane if lane_after else None),
            adopted=adopted,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            gateway_ready=gateway_ready,
        )
    healed_lane = use_case.ops.read_lane(lane_runtime_root)
    if not (healed_lane and healed_lane.gateway_pane and healed_lane.worker_pane):
        steps.append(
            ActuationStep(
                order=6,
                title="relaunch lane column (self-heal)",
                status=STEP_BLOCKED,
                detail="self-heal relaunch returned but the lane is not visible "
                "with a gateway + worker pane pair on read-back",
                command=None,
            )
        )
        return use_case._blocked(
            request,
            launch_action=launch.action,
            reason="lane not visible with both panes after the self-heal "
            "relaunch; fail-closed",
            reasons=(REASON_HANDOFF_FAILED, REASON_PANE_CREATE_FAILED),
            dispatch=dispatch,
            steps=tuple(steps),
            gateway_pane=(healed_lane.gateway_pane if healed_lane else None),
            worker_pane=(healed_lane.worker_pane if healed_lane else None),
            adopted=adopted,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            gateway_ready=gateway_ready,
        )
    if not use_case._identity_matches(healed_lane, request):
        steps.append(
            ActuationStep(
                order=6,
                title="relaunch lane column (self-heal)",
                status=STEP_BLOCKED,
                detail=f"healed lane identity (label={healed_lane.lane_label!r} "
                f"issue={healed_lane.issue!r}) does not match the requested lane "
                f"(label={request.lane_label!r} issue={request.issue!r}); "
                "refusing to dispatch to a mismatched lane",
                command=None,
            )
        )
        return use_case._blocked(
            request,
            launch_action=launch.action,
            reason="healed lane identity does not match the requested lane; "
            "fail-closed before the dispatch retry",
            reasons=(REASON_HANDOFF_FAILED, REASON_LANE_MISMATCH),
            dispatch=dispatch,
            steps=tuple(steps),
            gateway_pane=healed_lane.gateway_pane,
            worker_pane=healed_lane.worker_pane,
            adopted=adopted,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            gateway_ready=gateway_ready,
        )
    gateway_pane = healed_lane.gateway_pane
    worker_pane = healed_lane.worker_pane
    steps.append(
        ActuationStep(
            order=6,
            title="relaunch lane column (self-heal)",
            status=STEP_EXECUTED,
            detail="gateway vanished before delivery; relaunched via the "
            "adopt-or-launch session preparation (surviving slot adopted, dead "
            f"slot relaunched) — gateway {gateway_pane} + worker {worker_pane} "
            "live on read-back",
            command=None,
        )
    )
    healed_ready, ready_probes = use_case._wait_gateway_ready(gateway_pane)
    if healed_ready is None:
        readiness_detail = (
            "gateway readiness wait disabled; retrying the dispatch immediately"
        )
        readiness_status = STEP_SKIPPED
    elif healed_ready:
        readiness_detail = (
            f"healed gateway {gateway_pane} ready after {ready_probes} probe(s); "
            "retrying the dispatch into a live composer"
        )
        readiness_status = STEP_EXECUTED
    else:
        readiness_detail = (
            f"healed gateway {gateway_pane} readiness unconfirmed after "
            f"{ready_probes} probe(s); retrying the dispatch anyway (queue-enter "
            "rail never hard-blocks — the handoff Enter-only retry is the "
            "landing safety net)"
        )
        readiness_status = STEP_SKIPPED
    steps.append(
        ActuationStep(
            order=7,
            title="confirm gateway readiness (post-heal)",
            status=readiness_status,
            detail=readiness_detail,
            command=None,
        )
    )
    try:
        retry_rc = use_case.ops.dispatch_implementation_request(
            issue=request.issue,
            journal=(request.journal or ""),
            gateway_pane=gateway_pane or "",
            lane_label=request.lane_label,
            upstream_coordinator=request.upstream_coordinator,
            target_repo=target_repo,
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed on any dispatch failure.
        retry_rc = 1
        retry_detail = f"handoff dispatch raised: {exc}"
    else:
        retry_detail = f"handoff send to gateway {gateway_pane} exit={retry_rc}"
    if retry_rc != 0:
        steps.append(
            ActuationStep(
                order=8,
                title="dispatch implementation_request (retry)",
                status=STEP_BLOCKED,
                detail=retry_detail,
                command=use_case._dispatch_command(request),
            )
        )
        return use_case._blocked(
            request,
            launch_action=launch.action,
            reason="gateway implementation_request dispatch failed again after "
            "the lane self-heal; fail-closed (no second heal)",
            reasons=(REASON_HANDOFF_FAILED,),
            dispatch=dispatch,
            steps=tuple(steps),
            gateway_pane=gateway_pane,
            worker_pane=worker_pane,
            adopted=adopted,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            gateway_ready=healed_ready,
        )
    steps.append(
        ActuationStep(
            order=8,
            title="dispatch implementation_request (retry)",
            status=STEP_EXECUTED,
            detail=f"gateway {gateway_pane} notified ({retry_detail}); "
            "worker dispatch not yet confirmed — gateway must forward to the "
            "same-lane worker",
            command=use_case._dispatch_command(request),
        )
    )
    return use_case._executed(
        request,
        launch,
        gateway_pane=gateway_pane,
        worker_pane=worker_pane,
        dispatch_target=gateway_pane,
        dispatch_result=DISPATCH_GATEWAY_NOTIFIED,
        adopted=adopted,
        steps=tuple(steps),
        fill_decision=fill_decision,
        fill_override_reason=fill_override_reason,
        gateway_ready=healed_ready,
        healed=True,
    )


__all__ = ("heal_and_retry_dispatch",)
