"""Fail-closed creation-side actuation use case for ``sublane start`` (Redmine #13299).

Byte-preserving carve of :class:`SublaneActuateUseCase` out of the #12973
``sublane_actuator`` facade (module-health decomposition of the at-ceiling use case;
the facade re-exports this class, so the public import surface is unchanged).

The use case holds the fail-closed decision flow and never touches IO: it drives the
injected :class:`...application.sublane_actuator_ops.SublaneActuatorOps` port, consults the
pure :func:`decide_worktree_launch` launch policy and the #13290
:func:`evaluate_dispatch_admission` gate (whose concrete stop vocabulary stays the single
#12855 fill-decision authority — never re-implemented here), and assembles the typed,
replayable :class:`SublaneActuationOutcome`. The concrete side effects live behind the
port in :mod:`...application.sublane_actuator_ops`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    ACTUATE_READY,
    DISPATCH_GATEWAY_NOTIFIED,
    DISPATCH_NOT_ATTEMPTED,
    DISPATCH_SKIPPED,
    REASON_ANCHOR_REQUIRED,
    REASON_HANDOFF_FAILED,
    REASON_LANE_MISMATCH,
    REASON_LAUNCH_BLOCKED,
    REASON_FILL_STOP,
    REASON_MISSING_IDENTITY,
    REASON_PANE_CREATE_FAILED,
    REASON_STAMP_FAILED,
    REASON_WORK_UNIT_BLOCKED,
    REASON_WORKTREE_CREATE_FAILED,
    STEP_BLOCKED,
    STEP_EXECUTED,
    STEP_READY,
    STEP_SKIPPED,
    ActuationStep,
    SublaneActuationOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_dispatch_admission import (
    evaluate_dispatch_admission,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    FillDecisionInputs,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    LAUNCH_BLOCKED,
    LAUNCH_CREATE_WORKTREE,
    LAUNCH_REUSE_WORKTREE,
    LAUNCH_SKIP_NO_GIT,
    SublaneIntegrationPolicy,
    WorktreeLaunchDecision,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    SublaneCreateRequest,
    SublaneLaneView,
    parse_issue_from_lane_label,
    portable_worktree_label,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (  # noqa: E501
    DEFAULT_GATEWAY_READY_INTERVAL_SECONDS,
    DEFAULT_GATEWAY_READY_PROBES,
    SublaneActuatorOps,
    decide_create_launch,
    resolve_create_identity,
    resolve_lane_runtime_root,
)


# ---------------------------------------------------------------------------
# Use case: fail-closed live actuation over the injected port.
# ---------------------------------------------------------------------------


@dataclass
class SublaneActuateUseCase:
    """Drive the fail-closed creation-side actuation over :class:`SublaneActuatorOps`.

    The runtime preflight is the final authority (the #12604 acceptance style): the use
    case probes git through the port, asks the pure :func:`decide_worktree_launch`, and — in
    a live ``--execute`` run — performs only the additive side effects the plan authorizes,
    **stopping at the first failure and reporting the partial state** rather than a partial
    success. A dry-run resolves the plan and performs nothing.
    """

    ops: SublaneActuatorOps
    policy: SublaneIntegrationPolicy = SublaneIntegrationPolicy.default()
    # #13293 pre-dispatch gateway readiness wait (injectable for tests). ``probes<=0``
    # disables the wait (back-compat immediate dispatch, ``gateway_ready`` stays None).
    gateway_ready_probes: int = DEFAULT_GATEWAY_READY_PROBES
    gateway_ready_interval_seconds: float = DEFAULT_GATEWAY_READY_INTERVAL_SECONDS
    sleep: Callable[[float], None] = field(default=time.sleep)

    def _wait_gateway_ready(
        self, gateway_pane: Optional[str]
    ) -> tuple[Optional[bool], int]:
        """Bounded, non-fatal pre-dispatch readiness wait (#13293).

        Polls :meth:`SublaneActuatorOps.probe_gateway_ready` up to
        ``gateway_ready_probes`` times, ``gateway_ready_interval_seconds`` apart, so a
        freshly-launched gateway TUI has time to boot before the queue-enter dispatch.
        Returns ``(ready, probes_run)``. ``ready`` is ``None`` when the wait is disabled
        (``probes<=0``) or no gateway pane resolved — nothing was probed. Otherwise it is
        ``True`` on the first ready observation or ``False`` when the window elapses
        unconfirmed. It NEVER raises and NEVER blocks the dispatch: an unconfirmed
        ``False`` degrades to a recorded observation and the caller dispatches anyway
        (the queue-enter rail never hard-blocks; the handoff Enter-only retry is the
        landing safety net).
        """
        probes = self.gateway_ready_probes
        if probes <= 0 or not gateway_pane:
            return None, 0
        for attempt in range(probes):
            if self.ops.probe_gateway_ready(gateway_pane):
                return True, attempt + 1
            if attempt + 1 < probes:
                self.sleep(self.gateway_ready_interval_seconds)
        return False, probes

    @staticmethod
    def _identity_matches(
        lane: SublaneLaneView, request: SublaneCreateRequest
    ) -> bool:
        """True iff the resolved ``lane`` is the requested target (pure).

        The lane's ``lane_label`` must equal the requested label, and its issue must
        match the requested issue. The lane's ``issue`` is parsed from its label by
        :func:`project_sublanes`; ``parse_issue_from_lane_label`` is used as the fallback
        so a lane whose ``issue`` field was not pre-populated is still validated by re-
        parsing its label. A blank requested label / issue, a mismatched label, or a
        mismatched issue all fail closed — this is the guard that stops a repo-root /
        basename collision from misdelivering to the wrong gateway (Review j#70250).
        """
        want_label = (request.lane_label or "").strip()
        got_label = (lane.lane_label or "").strip()
        if not want_label or got_label != want_label:
            return False
        want_issue = (request.issue or "").strip()
        got_issue = (lane.issue or "").strip() or (
            parse_issue_from_lane_label(got_label) or ""
        )
        if want_issue and got_issue != want_issue:
            return False
        return True

    def run(
        self,
        request: SublaneCreateRequest,
        *,
        execute: bool,
        dispatch: bool = True,
        target_repo: str = "auto",
        fill_inputs: Optional[FillDecisionInputs] = None,
        override_fill_stop: Optional[str] = None,
    ) -> SublaneActuationOutcome:
        # 1. Fail closed on missing identity (#13432: a non-git workspace relaxes
        # --branch/--worktree and defaults the omitted worktree to the workspace root).
        request, missing = resolve_create_identity(self.ops, request)
        if missing:
            return self._blocked(
                request,
                launch_action=None,
                reason="required sublane identity fields are missing; refusing to "
                "actuate against an incomplete target",
                reasons=(REASON_MISSING_IDENTITY,)
                + tuple(f"missing_field:{name}" for name in missing),
                dispatch=dispatch,
            )

        # 2. Work-unit granularity gate (#13002): an epic / feature unit is never
        # actuated / dispatched without an explicit owner / operator decision anchor.
        unit_decision = request.work_unit_decision()
        if not unit_decision.is_allowed:
            return self._blocked(
                request,
                launch_action=None,
                reason=unit_decision.reason,
                reasons=(REASON_WORK_UNIT_BLOCKED, unit_decision.diagnostic),
                dispatch=dispatch,
            )

        # 3. Anchor requirement: a live dispatch needs a durable journal id.
        anchor = (request.journal or "").strip()
        if execute and dispatch and not anchor:
            return self._blocked(
                request,
                launch_action=None,
                reason="a live dispatch requires a durable-anchor journal id "
                "(--journal); refusing to dispatch a worker without an anchor",
                reasons=(REASON_ANCHOR_REQUIRED,),
                dispatch=dispatch,
            )

        # 3b. Dispatch admission gate (#13290, live-dispatch path only): consult the
        # caller-supplied fill decision (the single #12855 authority) and fail closed
        # on a concrete stop unless an explicit override reason is supplied. When no
        # fill context is supplied the gate is not armed and this is a no-op, keeping
        # the #12973 live-actuation contract byte-for-byte back-compatible. Scoped to
        # ``execute and dispatch`` (same as the anchor gate above): #13290 gates the
        # ``--execute`` *dispatch*, while ``--no-dispatch`` is a create/adopt-only
        # surface that dispatches no worker and so has nothing to gate. A dry-run
        # (``execute=False``) likewise performs nothing to gate (Review j#72744 #2).
        fill_decision_token: Optional[str] = None
        fill_override_reason: Optional[str] = None
        if execute and dispatch:
            admission = evaluate_dispatch_admission(
                fill_inputs, override_reason=override_fill_stop
            )
            if admission.is_blocked:
                return self._blocked(
                    request,
                    launch_action=None,
                    reason=admission.reason,
                    reasons=(REASON_FILL_STOP,)
                    + ((admission.fill_decision,) if admission.fill_decision else ()),
                    dispatch=dispatch,
                    fill_decision=admission.fill_decision,
                )
            fill_decision_token = admission.fill_decision
            fill_override_reason = admission.override_reason

            # #13613: optional herdr sender attestation must fail before mutation;
            # absence preserves tmux and existing test-port compatibility.
            sender_preflight = getattr(self.ops, "preflight_dispatch_sender", None)
            if callable(sender_preflight):
                sender_ok, sender_detail = sender_preflight()
                if not sender_ok:
                    return self._blocked(
                        request,
                        launch_action=None,
                        reason="dispatch sender attestation failed before actuation; "
                        f"{sender_detail}",
                        reasons=(REASON_MISSING_IDENTITY, "sender_attestation"),
                        dispatch=dispatch,
                        fill_decision=fill_decision_token,
                        fill_override_reason=fill_override_reason,
                    )

        # 4. Resolve the launch decision; a blocked launch is fail-closed. With every
        # identity field present (step 1 passed) the pure decision does not currently
        # return LAUNCH_BLOCKED, but this stays fail-closed if that contract changes.
        launch = decide_create_launch(self.ops, request, self.policy)
        if launch.action == LAUNCH_BLOCKED:
            return self._blocked(
                request,
                launch_action=launch.action,
                reason=launch.reason,
                reasons=(REASON_LAUNCH_BLOCKED,),
                dispatch=dispatch,
            )

        # 5. Dry-run: resolve the plan; perform nothing.
        if not execute:
            return self._dry_run(request, launch, dispatch=dispatch)

        # 6. Live actuation, fail-closed, stopping at the first failure.
        return self._execute(
            request,
            launch,
            dispatch=dispatch,
            target_repo=target_repo,
            fill_decision=fill_decision_token,
            fill_override_reason=fill_override_reason,
        )

    # -- helpers ------------------------------------------------------------

    def _blocked(
        self,
        request: SublaneCreateRequest,
        *,
        launch_action: Optional[str],
        reason: str,
        reasons: tuple[str, ...],
        dispatch: bool,
        steps: tuple[ActuationStep, ...] = (),
        gateway_pane: Optional[str] = None,
        worker_pane: Optional[str] = None,
        adopted: bool = False,
        fill_decision: Optional[str] = None,
        fill_override_reason: Optional[str] = None,
        gateway_ready: Optional[bool] = None,
    ) -> SublaneActuationOutcome:
        return SublaneActuationOutcome(
            status=ACTUATE_BLOCKED,
            execute=True,
            reason=reason,
            issue=request.issue,
            lane_label=request.lane_label,
            branch=request.branch or None,
            worktree_path=request.worktree_path or None,
            launch_action=launch_action,
            gateway_pane=gateway_pane,
            worker_pane=worker_pane,
            dispatch_target=None,
            dispatch_result=DISPATCH_NOT_ATTEMPTED,
            durable_anchor=(request.journal or None),
            adopted=adopted,
            steps=steps,
            blocked_reasons=reasons,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            gateway_ready=gateway_ready,
        )

    @staticmethod
    def _worktree_add_command(request: SublaneCreateRequest) -> str:
        """The replayable ``git worktree add`` command for the request (pure, #13293).

        Appends the explicit ``base_ref`` positional when supplied so the recorded /
        previewed command matches what the live actuator runs (base off the pinned ref
        instead of the ambient checkout HEAD).
        """
        base = (request.base_ref or "").strip()
        command = f"git worktree add {request.worktree_path} -b {request.branch}"
        return f"{command} {base}" if base else command

    def _worktree_step_title(self, launch: WorktreeLaunchDecision) -> str:
        if launch.action == LAUNCH_CREATE_WORKTREE:
            return "create worktree"
        if launch.action == LAUNCH_REUSE_WORKTREE:
            return "reuse worktree"
        return "skip worktree"

    def _dry_run(
        self,
        request: SublaneCreateRequest,
        launch: WorktreeLaunchDecision,
        *,
        dispatch: bool,
    ) -> SublaneActuationOutcome:
        wt_command = (
            self._worktree_add_command(request)
            if launch.action == LAUNCH_CREATE_WORKTREE
            else None
        )
        lane_runtime_root = resolve_lane_runtime_root(self.ops, request.worktree_path or "", skip_no_git=launch.action == LAUNCH_SKIP_NO_GIT)  # noqa: E501
        steps = [
            ActuationStep(
                order=1,
                title=self._worktree_step_title(launch),
                status=STEP_READY,
                detail=launch.reason,
                command=wt_command,
            ),
            ActuationStep(
                order=2,
                title="append lane column",
                status=STEP_READY,
                detail="append (or adopt) a cockpit-visible gateway + worker column and "
                "bind the lane / role / workspace / repo-root stamps",
                # #13155: render the SAME argv the live append drives (incl. --claude-model).
                command="mozyo-bridge " + " ".join(self.ops.append_lane_argv(lane_runtime_root)),  # noqa: E501
            ),
            ActuationStep(
                order=3,
                title="confirm lane stamps",
                status=STEP_READY,
                detail="read back the pane inventory and confirm the lane is visible with "
                "both panes and its identity stamps",
                command=None,
            ),
            # #13293: the pre-dispatch gateway readiness wait (bounded, non-fatal) that
            # --execute would run before the queue-enter dispatch.
            ActuationStep(
                order=4,
                title="confirm gateway readiness",
                status=STEP_READY if dispatch else STEP_SKIPPED,
                detail="wait (bounded, non-fatal) for the gateway TUI to boot + render "
                "before the queue-enter dispatch so the input lands on a live composer; "
                "an unconfirmed readiness degrades to gateway_ready=false and dispatches "
                "anyway (never hard-blocks the queue-enter rail)"
                if dispatch
                else "dispatch skipped (--no-dispatch); gateway readiness not probed",
                command=None,
            ),
            ActuationStep(
                order=5,
                title="dispatch implementation_request",
                status=STEP_READY if dispatch else STEP_SKIPPED,
                detail="route the governed implementation_request to the gateway "
                "(coordinator -> sublane Codex gateway -> same-lane Claude worker)"
                if dispatch
                else "dispatch skipped (--no-dispatch); create/adopt only",
                command=self._dispatch_command(request) if dispatch else None,
            ),
        ]
        return SublaneActuationOutcome(
            status=ACTUATE_READY,
            execute=False,
            reason="sublane identity resolved; launch action "
            f"{launch.action!r}: {launch.reason} (dry-run; nothing actuated)",
            issue=request.issue,
            lane_label=request.lane_label,
            branch=request.branch or None,
            worktree_path=request.worktree_path or None,
            launch_action=launch.action,
            gateway_pane=None,
            worker_pane=None,
            dispatch_target=None,
            dispatch_result=DISPATCH_SKIPPED if not dispatch else DISPATCH_NOT_ATTEMPTED,
            durable_anchor=(request.journal or None),
            adopted=False,
            steps=tuple(steps),
        )

    def _execute(
        self,
        request: SublaneCreateRequest,
        launch: WorktreeLaunchDecision,
        *,
        dispatch: bool,
        target_repo: str,
        fill_decision: Optional[str] = None,
        fill_override_reason: Optional[str] = None,
    ) -> SublaneActuationOutcome:
        steps: list[ActuationStep] = []
        # #13392: the lane runtime root — worktree (Git) or workspace root (non-git); the
        # dispatch repo/cwd gate collapses to it too (a non-git lane's agent cwd IS it).
        lane_runtime_root = resolve_lane_runtime_root(self.ops, request.worktree_path or "", skip_no_git=launch.action == LAUNCH_SKIP_NO_GIT)  # noqa: E501
        if launch.action == LAUNCH_SKIP_NO_GIT:
            target_repo = lane_runtime_root

        # Step 1 — worktree (create / reuse / skip).
        if launch.action == LAUNCH_CREATE_WORKTREE:
            try:
                self.ops.create_worktree(
                    branch=request.branch,
                    worktree_path=request.worktree_path,
                    base_ref=request.base_ref,
                )
            except Exception as exc:  # noqa: BLE001 — surface any git failure fail-closed.
                steps.append(
                    ActuationStep(
                        order=1,
                        title="create worktree",
                        status=STEP_BLOCKED,
                        detail=f"git worktree add failed: {exc}",
                        command=self._worktree_add_command(request),
                    )
                )
                return self._blocked(
                    request,
                    launch_action=launch.action,
                    reason="worktree creation failed (branch / path collision or git "
                    "refusal); lane not actuated",
                    reasons=(REASON_WORKTREE_CREATE_FAILED,),
                    dispatch=dispatch,
                    steps=tuple(steps),
                    fill_decision=fill_decision,
                    fill_override_reason=fill_override_reason,
                )
            steps.append(
                ActuationStep(
                    order=1,
                    title="create worktree",
                    status=STEP_EXECUTED,
                    # #13368: prose detail is pasteable; name the portable sibling
                    # basename, not the host-local absolute path (the replayable
                    # `git worktree add` command below keeps the absolute path for
                    # local replay, redacted only in the human-readable text render).
                    detail=f"created worktree {portable_worktree_label(request.worktree_path)} "
                    f"on branch {request.branch}"
                    + (
                        f" from base {request.base_ref}"
                        if (request.base_ref or "").strip()
                        else ""
                    ),
                    command=self._worktree_add_command(request),
                )
            )
        elif launch.action == LAUNCH_REUSE_WORKTREE:
            steps.append(
                ActuationStep(
                    order=1,
                    title="reuse worktree",
                    status=STEP_EXECUTED,
                    detail=f"worktree for branch {request.branch!r} already exists; "
                    "reusing it (never clobbered)",
                    command=None,
                )
            )
        else:  # skip_no_git / skip_disabled
            steps.append(
                ActuationStep(
                    order=1,
                    title="skip worktree",
                    status=STEP_SKIPPED,
                    detail=launch.reason,
                    command=None,
                )
            )

        # Step 2 — append (or adopt) the cockpit lane column. A lane that already resolves
        # for this worktree MUST match the requested identity before we adopt or dispatch
        # to it: a repo-root / basename collision with a different (or stale) lane is an
        # ambiguous target and fails closed here. Never adopt / append onto — nor later
        # dispatch to — a lane whose lane_label / issue does not match the request, which
        # would misdeliver the implementation_request to the wrong gateway (Review j#70250).
        existing = self.ops.read_lane(lane_runtime_root)
        if existing is not None and not self._identity_matches(existing, request):
            steps.append(
                ActuationStep(
                    order=2,
                    title="resolve lane column",
                    status=STEP_BLOCKED,
                    detail=f"a different lane (label={existing.lane_label!r} "
                    f"issue={existing.issue!r}) already resolves for this worktree; "
                    "refusing to adopt / append onto an ambiguous target",
                    command=None,
                )
            )
            return self._blocked(
                request,
                launch_action=launch.action,
                reason="resolved lane identity does not match the requested lane "
                "(repo-root / basename collision or stale lane); fail-closed before "
                "adopt / dispatch",
                reasons=(REASON_LANE_MISMATCH,),
                dispatch=dispatch,
                steps=tuple(steps),
                gateway_pane=existing.gateway_pane,
                worker_pane=existing.worker_pane,
                fill_decision=fill_decision,
                fill_override_reason=fill_override_reason,
            )
        adopted = bool(existing and existing.gateway_pane and existing.worker_pane)
        if adopted:
            lane = existing
            steps.append(
                ActuationStep(
                    order=2,
                    title="adopt lane column",
                    status=STEP_SKIPPED,
                    detail="a live gateway + worker column already exists for this "
                    "worktree and matches the requested identity; adopting it (no new "
                    "panes appended)",
                    command=None,
                )
            )
        else:
            try:
                self.ops.append_lane_column(lane_runtime_root)
            except Exception as exc:  # noqa: BLE001 — fail-closed on any append failure.
                steps.append(
                    ActuationStep(
                        order=2,
                        title="append lane column",
                        status=STEP_BLOCKED,
                        detail=f"cockpit append failed: {exc}",
                        command=None,
                    )
                )
                return self._blocked(
                    request,
                    launch_action=launch.action,
                    reason="cockpit lane column append failed; lane not actuated",
                    reasons=(REASON_PANE_CREATE_FAILED,),
                    dispatch=dispatch,
                    steps=tuple(steps),
                    fill_decision=fill_decision,
                    fill_override_reason=fill_override_reason,
                )
            lane = self.ops.read_lane(lane_runtime_root)
            if not (lane and lane.gateway_pane and lane.worker_pane):
                steps.append(
                    ActuationStep(
                        order=2,
                        title="append lane column",
                        status=STEP_BLOCKED,
                        detail="append returned but the lane is not visible with a "
                        "gateway + worker pane pair on read-back",
                        command=None,
                    )
                )
                return self._blocked(
                    request,
                    launch_action=launch.action,
                    reason="lane not visible with both panes after append; fail-closed",
                    reasons=(REASON_PANE_CREATE_FAILED,),
                    dispatch=dispatch,
                    steps=tuple(steps),
                    fill_decision=fill_decision,
                    fill_override_reason=fill_override_reason,
                )
            steps.append(
                ActuationStep(
                    order=2,
                    title="append lane column",
                    status=STEP_EXECUTED,
                    detail=f"appended gateway {lane.gateway_pane} + worker "
                    f"{lane.worker_pane} for lane {request.lane_label!r}",
                    command=None,
                )
            )

        # Step 3 — confirm the identity stamps landed on the resolved lane.
        if not lane or not lane.repo_root:
            steps.append(
                ActuationStep(
                    order=3,
                    title="confirm lane stamps",
                    status=STEP_BLOCKED,
                    detail="the lane did not carry a repo-root stamp on read-back; "
                    "cannot positively confirm the lane identity",
                    command=None,
                )
            )
            return self._blocked(
                request,
                launch_action=launch.action,
                reason="lane identity stamps missing on read-back; fail-closed",
                reasons=(REASON_STAMP_FAILED,),
                dispatch=dispatch,
                steps=tuple(steps),
                gateway_pane=lane.gateway_pane if lane else None,
                worker_pane=lane.worker_pane if lane else None,
                adopted=adopted,
                fill_decision=fill_decision,
                fill_override_reason=fill_override_reason,
            )
        # Identity re-confirm on the resolved lane (covers the append path: an appended
        # lane whose stamped label / issue does not match the request is a mismatch too).
        if not self._identity_matches(lane, request):
            steps.append(
                ActuationStep(
                    order=3,
                    title="confirm lane identity",
                    status=STEP_BLOCKED,
                    detail=f"resolved lane identity (label={lane.lane_label!r} "
                    f"issue={lane.issue!r}) does not match the requested lane "
                    f"(label={request.lane_label!r} issue={request.issue!r}); refusing "
                    "to dispatch to a mismatched lane",
                    command=None,
                )
            )
            return self._blocked(
                request,
                launch_action=launch.action,
                reason="resolved lane identity does not match the requested lane; "
                "fail-closed before dispatch",
                reasons=(REASON_LANE_MISMATCH,),
                dispatch=dispatch,
                steps=tuple(steps),
                gateway_pane=lane.gateway_pane,
                worker_pane=lane.worker_pane,
                adopted=adopted,
                fill_decision=fill_decision,
                fill_override_reason=fill_override_reason,
            )
        gateway_pane = lane.gateway_pane
        worker_pane = lane.worker_pane
        steps.append(
            ActuationStep(
                order=3,
                title="confirm lane stamps",
                status=STEP_EXECUTED,
                # #13368: redact the pasteable prose detail to the portable sibling
                # basename (the absolute repo root remains in the structured payload).
                detail=f"lane visible: repo_root={portable_worktree_label(lane.repo_root)} "
                f"gateway={gateway_pane} worker={worker_pane} state={lane.state}",
                command=None,
            )
        )

        # Step 4 (--no-dispatch) — nothing to dispatch, so nothing to make ready.
        if not dispatch:
            steps.append(
                ActuationStep(
                    order=4,
                    title="confirm gateway readiness",
                    status=STEP_SKIPPED,
                    detail="dispatch skipped (--no-dispatch); gateway readiness not "
                    "probed (no queue-enter dispatch to land)",
                    command=None,
                )
            )
            steps.append(
                ActuationStep(
                    order=5,
                    title="dispatch implementation_request",
                    status=STEP_SKIPPED,
                    detail="dispatch skipped (--no-dispatch); create/adopt only",
                    command=None,
                )
            )
            return self._executed(
                request,
                launch,
                gateway_pane=gateway_pane,
                worker_pane=worker_pane,
                dispatch_target=None,
                dispatch_result=DISPATCH_SKIPPED,
                adopted=adopted,
                steps=tuple(steps),
                fill_decision=fill_decision,
                fill_override_reason=fill_override_reason,
            )

        # Step 4 — pre-dispatch gateway readiness wait (#13293). Give a freshly-launched
        # gateway TUI time to boot before the queue-enter dispatch so the input lands on
        # a live composer instead of vanishing into a still-booting one (the j#72677 /
        # 5-example, 100%-reproduction dispatch-loss failure mode). This NEVER hard-blocks
        # the queue-enter rail (#13262/#13255): an unconfirmed readiness degrades to a
        # recorded ``gateway_ready=false`` and the dispatch proceeds anyway — the handoff
        # Enter-only retry (#12580/#12581) is the landing safety net and the coordinator
        # watches for a no-progress lane.
        gateway_ready, ready_probes = self._wait_gateway_ready(gateway_pane)
        if gateway_ready is None:
            readiness_detail = (
                "gateway readiness wait disabled (--gateway-ready-timeout 0); "
                "dispatching immediately"
            )
            readiness_status = STEP_SKIPPED
        elif gateway_ready:
            readiness_detail = (
                f"gateway {gateway_pane} ready (codex TUI booted + rendered) after "
                f"{ready_probes} probe(s); dispatching into a live composer"
            )
            readiness_status = STEP_EXECUTED
        else:
            readiness_detail = (
                f"gateway {gateway_pane} readiness unconfirmed after {ready_probes} "
                "probe(s); dispatching anyway (queue-enter rail never hard-blocks — the "
                "handoff Enter-only retry is the landing safety net). Recorded "
                "gateway_ready=false so a no-progress lane is watched for"
            )
            readiness_status = STEP_SKIPPED
        steps.append(
            ActuationStep(
                order=4,
                title="confirm gateway readiness",
                status=readiness_status,
                detail=readiness_detail,
                command=None,
            )
        )

        try:
            rc = self.ops.dispatch_implementation_request(
                issue=request.issue,
                journal=(request.journal or ""),
                gateway_pane=gateway_pane or "",
                lane_label=request.lane_label,
                upstream_coordinator=request.resolved_upstream_coordinator(),
                target_repo=target_repo,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed on any dispatch failure.
            rc = 1
            dispatch_detail = f"handoff dispatch raised: {exc}"
        else:
            dispatch_detail = f"handoff send to gateway {gateway_pane} exit={rc}"
        if rc != 0:
            # Redmine #13378: a failed dispatch whose gateway slot is GONE on
            # read-back is the measured vanish mode (an idle pre-session gateway
            # killed by a host-level agent-CLI update) — self-heal once and retry,
            # when the ops adapter offers the capability. Any other failure keeps
            # the plain fail-closed block below, byte-for-byte.
            healed_outcome = self._heal_and_retry_dispatch(
                request,
                launch,
                steps=steps,
                failed_dispatch_detail=dispatch_detail,
                dispatch=dispatch,
                target_repo=target_repo,
                lane_runtime_root=lane_runtime_root,
                adopted=adopted,
                fill_decision=fill_decision,
                fill_override_reason=fill_override_reason,
                gateway_ready=gateway_ready,
            )
            if healed_outcome is not None:
                return healed_outcome
            steps.append(
                ActuationStep(
                    order=5,
                    title="dispatch implementation_request",
                    status=STEP_BLOCKED,
                    detail=dispatch_detail,
                    command=self._dispatch_command(request),
                )
            )
            return self._blocked(
                request,
                launch_action=launch.action,
                reason="gateway implementation_request dispatch failed; panes created "
                "but not dispatched (fail-closed)",
                reasons=(REASON_HANDOFF_FAILED,),
                dispatch=dispatch,
                steps=tuple(steps),
                gateway_pane=gateway_pane,
                worker_pane=worker_pane,
                adopted=adopted,
                fill_decision=fill_decision,
                fill_override_reason=fill_override_reason,
                gateway_ready=gateway_ready,
            )
        steps.append(
            ActuationStep(
                order=5,
                title="dispatch implementation_request",
                status=STEP_EXECUTED,
                # #12986: name the step for what it proves — the gateway was
                # notified, not that the worker was dispatched / started.
                detail=f"gateway {gateway_pane} notified ({dispatch_detail}); "
                "worker dispatch not yet confirmed — gateway must forward to the "
                "same-lane worker",
                command=self._dispatch_command(request),
            )
        )
        return self._executed(
            request,
            launch,
            gateway_pane=gateway_pane,
            worker_pane=worker_pane,
            dispatch_target=gateway_pane,
            # Redmine #12986: a gateway `handoff send` exit 0 proves gateway
            # notification only, never that the same-lane worker was dispatched or
            # started. Record it honestly as `gateway_notified` (not `sent`) so a
            # gateway-notified-but-quiet lane is never read as worker-started.
            dispatch_result=DISPATCH_GATEWAY_NOTIFIED,
            adopted=adopted,
            steps=tuple(steps),
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            gateway_ready=gateway_ready,
        )

    def _heal_and_retry_dispatch(
        self,
        request: SublaneCreateRequest,
        launch: WorktreeLaunchDecision,
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

        Thin delegator to :func:`~.sublane_actuator_heal.heal_and_retry_dispatch`
        (carved into its own module for the module-health ceiling — the recovery
        contract lives there). Returns ``None`` when the heal is not applicable
        (no ops capability, or the gateway is still resolvable), leaving ``steps``
        untouched so the caller's plain fail-closed block is byte-for-byte the
        pre-#13378 behaviour; otherwise a terminal executed / blocked outcome.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_heal import (  # noqa: E501
            heal_and_retry_dispatch,
        )

        return heal_and_retry_dispatch(
            self,
            request,
            launch,
            steps=steps,
            failed_dispatch_detail=failed_dispatch_detail,
            dispatch=dispatch,
            target_repo=target_repo,
            lane_runtime_root=lane_runtime_root,
            adopted=adopted,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            gateway_ready=gateway_ready,
        )

    def _executed(
        self,
        request: SublaneCreateRequest,
        launch: WorktreeLaunchDecision,
        *,
        gateway_pane: Optional[str],
        worker_pane: Optional[str],
        dispatch_target: Optional[str],
        dispatch_result: str,
        adopted: bool,
        steps: tuple[ActuationStep, ...],
        fill_decision: Optional[str] = None,
        fill_override_reason: Optional[str] = None,
        gateway_ready: Optional[bool] = None,
        healed: bool = False,
    ) -> SublaneActuationOutcome:
        return SublaneActuationOutcome(
            status=ACTUATE_EXECUTED,
            execute=True,
            reason="sublane actuated: "
            + ("adopted existing lane" if adopted else "created lane")
            + f"; launch action {launch.action!r}"
            + self._dispatch_reason_suffix(dispatch_result)
            + self._heal_reason_suffix(healed)
            + self._gateway_ready_reason_suffix(dispatch_result, gateway_ready)
            + self._fill_override_reason_suffix(fill_override_reason),
            issue=request.issue,
            lane_label=request.lane_label,
            branch=request.branch or None,
            worktree_path=request.worktree_path or None,
            launch_action=launch.action,
            gateway_pane=gateway_pane,
            worker_pane=worker_pane,
            dispatch_target=dispatch_target,
            dispatch_result=dispatch_result,
            durable_anchor=(request.journal or None),
            adopted=adopted,
            steps=steps,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            gateway_ready=gateway_ready,
        )

    @staticmethod
    def _dispatch_reason_suffix(dispatch_result: str) -> str:
        """Honest dispatch clause appended to an executed outcome's reason (pure).

        Redmine #12986: keep the reason from reading as full success when only the
        gateway was notified — spell out that worker dispatch is unconfirmed.
        """
        if dispatch_result == DISPATCH_GATEWAY_NOTIFIED:
            return (
                " — gateway notified only; worker dispatch NOT yet confirmed "
                "(worker-dispatch ack still owed)"
            )
        if dispatch_result == DISPATCH_SKIPPED:
            return " — dispatch skipped (--no-dispatch)"
        return ""

    @staticmethod
    def _heal_reason_suffix(healed: bool) -> str:
        """Spell out that the lane was self-healed before this dispatch (pure, #13378).

        A healed outcome means the originally-launched gateway vanished before the
        first delivery (a host-level event killed the idle agent) and the lane column
        was relaunched in-flow; the coordinator record must say so — the gateway
        locator in this outcome is the *relaunched* pane, not the created one.
        """
        if not healed:
            return ""
        return (
            " — gateway vanished before the first dispatch; lane column self-healed "
            "(relaunched) and the dispatch was retried (#13378)"
        )

    @staticmethod
    def _gateway_ready_reason_suffix(
        dispatch_result: str, gateway_ready: Optional[bool]
    ) -> str:
        """Spell out an unconfirmed pre-dispatch gateway readiness (pure, #13293).

        Only a gateway-notified dispatch whose pre-dispatch readiness wait elapsed
        unconfirmed (``gateway_ready is False``) gets a clause: the input was dispatched
        into a gateway TUI that never confirmed ready, so the coordinator is told to
        watch for a no-progress lane. A confirmed-ready or not-probed dispatch adds
        nothing (keeps the reason quiet in the healthy / back-compat case).
        """
        if dispatch_result == DISPATCH_GATEWAY_NOTIFIED and gateway_ready is False:
            return (
                " — WARN gateway readiness NOT confirmed before dispatch; the input may "
                "have landed on a still-booting composer (watch for no_progress; the "
                "handoff Enter-only retry is the landing safety net)"
            )
        return ""

    @staticmethod
    def _fill_override_reason_suffix(fill_override_reason: Optional[str]) -> str:
        """Spell out that a fill-decision stop was intentionally overridden (pure, #13290).

        Keeps the executed outcome's reason honest: when the dispatch admission gate
        classified a stop and the coordinator proceeded via an explicit override, the
        record must say so (the override reason is also stored on the outcome and
        rendered into the durable journal).
        """
        reason = (fill_override_reason or "").strip()
        if not reason:
            return ""
        return f" — fill-decision stop overridden (reason: {reason})"

    def _dispatch_command(self, request: SublaneCreateRequest) -> str:
        journal = request.journal or "<journal>"
        coordinator = request.resolved_upstream_coordinator()
        return (
            "mozyo-bridge handoff send --to codex --source redmine "
            f"--issue {request.issue} --journal {journal} "
            "--kind implementation_request --target <gateway-pane> --target-repo auto "
            "--mode queue-enter --role-profile implementation_gateway "
            f"--profile-field lane={request.lane_label} "
            f"--profile-field upstream_coordinator={coordinator}"
        )


__all__ = ("SublaneActuateUseCase",)
