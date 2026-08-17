"""Fail-closed creation-side actuation use case for ``sublane start`` (Redmine #13299).

The use case owns the decision flow and never touches IO. It drives the injected
:class:`...application.sublane_actuator_ops.SublaneActuatorOps` port, consults the pure
launch and dispatch gates, and assembles the replayable
:class:`SublaneActuationOutcome`; concrete side effects live behind the port.
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
    REASON_ADOPT_OWNER_UNBOUND,
    REASON_ANCHOR_REQUIRED,
    REASON_HANDOFF_FAILED,
    REASON_LANE_MISMATCH,
    REASON_LAUNCH_BLOCKED,
    REASON_LAUNCHER_INCOMPATIBLE,
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
    SublaneLauncherIncompatibleError,
    SublaneStartupObservation,
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
    SublaneDispatchAttempt,
    SublaneActuatorOps,
    decide_create_launch,
    drive_dispatch_implementation_request,
    resolve_create_identity,
    resolve_lane_runtime_root,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_gates import (  # noqa: E501
    pair_attestation_admission,
    pair_split_admission,
    pre_mutation_admission,
    sender_authority_admission,
    startup_health_admission,
)


# ---------------------------------------------------------------------------
# Use case: fail-closed live actuation over the injected port.
# ---------------------------------------------------------------------------


@dataclass
class SublaneActuateUseCase:
    """Drive additive actuation over the injected port, stopping fail-closed.

    A dry-run resolves the plan and performs nothing.
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

        The probe itself does not veto dispatch; the governed send still returns its
        own confirmed, not-sent, or uncertain terminal.
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
        """True iff label and issue resolve to the requested lane (j#70250)."""
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

        # 2. Epic/feature units require an explicit owner decision anchor (#13002).
        unit_decision = request.work_unit_decision()
        if not unit_decision.is_allowed:
            return self._blocked(
                request,
                launch_action=None,
                reason=unit_decision.reason,
                reasons=(REASON_WORK_UNIT_BLOCKED, unit_decision.diagnostic),
                dispatch=dispatch,
            )

        # 3. Anchor requirement: ANY live actuation needs a durable journal id —
        #    scoped to `execute and dispatch` until #15152 R2 (review j#106834
        #    finding_authoritybypass), which let `--no-dispatch` mutate the
        #    workspace with no durable authority. A lane hangs from the record.
        anchor = (request.journal or "").strip()
        if execute and not anchor:
            return self._blocked(
                request,
                launch_action=None,
                reason="a live actuation requires a durable-anchor journal id "
                "(--journal); refusing to create or adopt a lane without one "
                "(dispatch and --no-dispatch alike)",
                reasons=(REASON_ANCHOR_REQUIRED,),
                dispatch=dispatch,
            )

        # 3b. Live dispatch fails closed on a fill stop unless explicitly overridden.
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

        # 3c. Sender authority for EVERY actuation (#15152 R2 j#106834; R3
        # j#106868 made capability ABSENCE fail closed too). Gate body lives in
        # `sublane_actuator_gates.sender_authority_admission`.
        if execute:
            gate_outcome = sender_authority_admission(
                self,
                request,
                dispatch=dispatch,
                fill_decision=fill_decision_token,
                fill_override_reason=fill_override_reason,
            )
            if gate_outcome is not None:
                return gate_outcome

        # 4. Resolve the launch decision and preserve a fail-closed future contract.
        launch = decide_create_launch(self.ops, request, self.policy)
        if launch.action == LAUNCH_BLOCKED:
            return self._blocked(
                request,
                launch_action=launch.action,
                reason=launch.reason,
                reasons=(REASON_LAUNCH_BLOCKED,),
                dispatch=dispatch,
            )

        # 4b. Pre-mutation admission: runtime fingerprint (#13705) + managed-launch launcher
        # compatibility (#14258), before the worktree. Includes ``--no-dispatch``.
        if execute:
            request, gate_outcome = pre_mutation_admission(
                self, request, launch_action=launch.action, dispatch=dispatch,
                fill_decision=fill_decision_token, fill_override_reason=fill_override_reason,
            )
            if gate_outcome is not None:
                return gate_outcome

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
        dispatch_target: Optional[str] = None,
        dispatch_result: str = DISPATCH_NOT_ATTEMPTED,
        dispatch_injection_stage: Optional[str] = None,
        dispatch_blind_retry_prohibited: bool = False,
        adopted: bool = False,
        fill_decision: Optional[str] = None,
        fill_override_reason: Optional[str] = None,
        gateway_ready: Optional[bool] = None,
        startup: Optional[SublaneStartupObservation] = None,
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
            dispatch_target=dispatch_target,
            dispatch_result=dispatch_result,
            dispatch_injection_stage=dispatch_injection_stage,
            dispatch_blind_retry_prohibited=dispatch_blind_retry_prohibited,
            durable_anchor=(request.journal or None),
            adopted=adopted,
            steps=steps,
            blocked_reasons=reasons,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            gateway_ready=gateway_ready,
            startup=startup,
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
            ActuationStep(
                order=4,
                title="confirm gateway readiness",
                status=STEP_READY if dispatch else STEP_SKIPPED,
                detail="wait (bounded, non-fatal) for the gateway TUI to boot + render "
                "before the queue-enter dispatch so the input lands on a live composer; "
                "an unconfirmed probe records gateway_ready=false; the governed send "
                "then returns its own confirmed or fail-closed result"
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
                    # #13368: pasteable prose names the portable sibling basename, not
                    # the absolute path (the replay command below keeps the abs path).
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

        # Step 2 — adopt/append only after the resolved lane identity matches (j#70250).
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
                startup = self.ops.append_lane_column(lane_runtime_root)
            except SublaneLauncherIncompatibleError as exc:
                # Capability skew is a typed pre-launch block, not a transient append error.
                steps.append(
                    ActuationStep(
                        order=2,
                        title="managed-launch capability preflight",
                        status=STEP_BLOCKED,
                        detail=f"launcher incompatible ({exc.reason}): {exc}",
                        command=None,
                    )
                )
                return self._blocked(
                    request,
                    launch_action=launch.action,
                    reason="managed-launch launcher capability-incompatible "
                    "(attestation-store schema mismatch); refusing to launch a pair that "
                    "would boot live but unattested — upgrade the launcher and re-run",
                    reasons=(REASON_LAUNCHER_INCOMPATIBLE, exc.reason),
                    dispatch=dispatch,
                    steps=tuple(steps),
                    fill_decision=fill_decision,
                    fill_override_reason=fill_override_reason,
                )
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
            startup_block = startup_health_admission(
                self,
                request,
                startup,
                launch_action=launch.action,
                dispatch=dispatch,
                steps=steps,
                fill_decision=fill_decision,
                fill_override_reason=fill_override_reason,
            )
            if startup_block is not None:
                return startup_block
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

        # Redmine #13705 R1-F2: admit ONLY an operable same-tab pair — a `pair_split`
        # lane is a degraded state the adopt path would otherwise dispatch to (gate in
        # ``sublane_actuator_gates``; covers both read-back sites).
        split_outcome = pair_split_admission(
            self, request, lane, launch_action=launch.action, dispatch=dispatch,
            adopted=adopted, steps=steps, fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
        )
        if split_outcome is not None:
            return split_outcome

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
        # Redmine #13809 / #13810 R3-F3: a live ADOPT declares the lane's owner row via the
        # common declaration service and MUST succeed (fresh or idempotent) before dispatch;
        # an owner-unbound outcome (ambiguous / stale / unattested / recycled / conflict)
        # fails closed here rather than dispatching to a lane hibernate can never resolve.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
            ADOPT_DECL_OWNER_UNBOUND,
        )

        adopt_outcome = self.ops.declare_adopted_lane_lifecycle(
            lane_runtime_root, adopted=adopted
        )
        if adopt_outcome in ADOPT_DECL_OWNER_UNBOUND:
            steps.append(
                ActuationStep(
                    order=3,
                    title="declare adopted lane owner",
                    status=STEP_BLOCKED,
                    detail=f"adopted lane owner binding not declared ({adopt_outcome}); "
                    "refusing to dispatch to an owner-unbound lane",
                    command=None,
                )
            )
            return self._blocked(
                request,
                launch_action=launch.action,
                reason=f"adopted lane owner binding not declared ({adopt_outcome}); "
                "fail-closed before dispatch (owner-unbound lane)",
                reasons=(REASON_ADOPT_OWNER_UNBOUND,),
                dispatch=dispatch,
                steps=tuple(steps),
                gateway_pane=gateway_pane,
                worker_pane=worker_pane,
                adopted=adopted,
                fill_decision=fill_decision,
                fill_override_reason=fill_override_reason,
            )

        # Step 3c — post-launch pair self-attestation gate (Redmine #13847): a fresh launch
        # that does not confirm BOTH slots' startup self-attestation booted partially (live
        # but unattested/stale) and must NOT be promoted to `executed`. Fires before the
        # dispatch branch so it covers `--no-dispatch` create (the live-evidence case) too.
        attest_outcome = pair_attestation_admission(
            self,
            request,
            launch_action=launch.action,
            dispatch=dispatch,
            adopted=adopted,
            gateway_pane=gateway_pane,
            worker_pane=worker_pane,
            lane_runtime_root=lane_runtime_root,
            steps=steps,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
        )
        if attest_outcome is not None:
            return attest_outcome

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

        # Step 4: the readiness probe is advisory; the governed send remains authoritative.
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
                "probe(s); attempting the governed send, which must independently "
                "confirm submission or fail closed. Recorded gateway_ready=false"
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
            dispatch_attempt = drive_dispatch_implementation_request(
                self.ops,
                issue=request.issue,
                journal=(request.journal or ""),
                gateway_pane=gateway_pane or "",
                lane_label=request.lane_label,
                upstream_coordinator=request.resolved_upstream_coordinator(),
                target_repo=target_repo,
            )
        except SystemExit as exc:
            code = exc.code
            dispatch_attempt = SublaneDispatchAttempt.untyped(
                code if type(code) is int and code != 0 else 1
            )
            dispatch_detail = "handoff dispatch exited without a typed delivery result"
        except Exception as exc:  # noqa: BLE001 — fail-closed on any dispatch failure.
            dispatch_attempt = SublaneDispatchAttempt.untyped(1)
            dispatch_detail = f"handoff dispatch raised: {exc}"
        else:
            dispatch_detail = (
                f"handoff send to gateway {gateway_pane} "
                f"exit={dispatch_attempt.exit_code}"
            )
        if not dispatch_attempt.confirmed:
            # Redmine #13378: a failed dispatch whose gateway slot is GONE on read-back
            # may self-heal and replay only with explicit pre-injection ``not_sent``
            # proof. Untyped/nonzero and post-injection uncertainty never retype body.
            healed_outcome = (
                self._heal_and_retry_dispatch(
                    request,
                    launch,
                    steps=steps,
                    failed_dispatch_detail=dispatch_detail,
                    failed_dispatch_attempt=dispatch_attempt,
                    failed_gateway_pane=gateway_pane,
                    dispatch=dispatch,
                    target_repo=target_repo,
                    lane_runtime_root=lane_runtime_root,
                    adopted=adopted,
                    fill_decision=fill_decision,
                    fill_override_reason=fill_override_reason,
                    gateway_ready=gateway_ready,
                )
                if dispatch_attempt.retry_safe
                else None
            )
            if healed_outcome is not None:
                return healed_outcome
            fate = (
                "nothing reached the gateway; retry is safe after fixing the refusal"
                if dispatch_attempt.retry_safe
                else "body and/or Enter may have reached the gateway; blind retry prohibited"
            )
            steps.append(
                ActuationStep(
                    order=5,
                    title="dispatch implementation_request",
                    status=STEP_BLOCKED,
                    detail=f"{dispatch_detail}; {fate}",
                    command=self._dispatch_command(request),
                )
            )
            return self._blocked(
                request,
                launch_action=launch.action,
                reason=f"gateway implementation_request dispatch failed; {fate} "
                "(fail-closed)",
                reasons=(REASON_HANDOFF_FAILED,),
                dispatch=dispatch,
                steps=tuple(steps),
                gateway_pane=gateway_pane,
                worker_pane=worker_pane,
                dispatch_target=gateway_pane,
                dispatch_result=dispatch_attempt.public_result,
                dispatch_injection_stage=dispatch_attempt.public_injection_stage,
                dispatch_blind_retry_prohibited=(
                    dispatch_attempt.blind_retry_prohibited
                ),
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
            # Redmine #12986: a gateway send exit 0 is gateway notification only — record
            # `gateway_notified` (not `sent`) so a quiet lane is not read as worker-started.
            dispatch_result=DISPATCH_GATEWAY_NOTIFIED,
            adopted=adopted,
            steps=tuple(steps),
            dispatch_injection_stage=dispatch_attempt.public_injection_stage,
            dispatch_blind_retry_prohibited=(
                dispatch_attempt.blind_retry_prohibited
            ),
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
        failed_dispatch_attempt: SublaneDispatchAttempt,
        failed_gateway_pane: Optional[str],
        dispatch: bool,
        target_repo: str,
        lane_runtime_root: str,
        adopted: bool,
        fill_decision: Optional[str],
        fill_override_reason: Optional[str],
        gateway_ready: Optional[bool],
    ) -> Optional[SublaneActuationOutcome]:
        """Delegate the explicit-not-sent vanished-gateway recovery (#13378)."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_heal import (  # noqa: E501
            heal_and_retry_dispatch,
        )

        return heal_and_retry_dispatch(
            self,
            request,
            launch,
            steps=steps,
            failed_dispatch_detail=failed_dispatch_detail,
            failed_dispatch_attempt=failed_dispatch_attempt,
            failed_gateway_pane=failed_gateway_pane,
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
        dispatch_injection_stage: Optional[str] = None,
        dispatch_blind_retry_prohibited: bool = False,
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
            dispatch_injection_stage=dispatch_injection_stage,
            dispatch_blind_retry_prohibited=dispatch_blind_retry_prohibited,
            durable_anchor=(request.journal or None),
            adopted=adopted,
            steps=steps,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            gateway_ready=gateway_ready,
        )

    @staticmethod
    def _dispatch_reason_suffix(dispatch_result: str) -> str:
        """Keep gateway notification distinct from worker dispatch (#12986)."""
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
        """Record that explicit-not-sent recovery relaunched the lane (#13378)."""
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
        """Record an advisory readiness miss only after confirmed notification."""
        if dispatch_result == DISPATCH_GATEWAY_NOTIFIED and gateway_ready is False:
            return (
                " — WARN gateway readiness NOT confirmed before dispatch, but the "
                "governed send subsequently confirmed gateway notification"
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
