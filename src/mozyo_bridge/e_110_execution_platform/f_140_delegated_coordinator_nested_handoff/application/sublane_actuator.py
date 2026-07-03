"""`mozyo-bridge sublane start --execute` live actuator boundary (Redmine #12973).

#12955 shipped ``sublane create`` / ``start`` as *planning only* — it emits the fail-closed,
replayable worktree + gateway pane + worker pane + dispatch plan but actuates nothing. This
module adds the **creation-side live actuator** that executes that plan in one action, wiring
the choreography a coordinator otherwise hand-assembles for every max-5 sublane:

1. create (or reuse) the lane git worktree / branch — the additive #12604
   :meth:`LiveSublaneGitOperations.create_worktree`, already inside the
   ``worktree-lifecycle-boundary.md`` boundary;
2. append a cockpit-visible Codex gateway + Claude worker column, binding the lane / role /
   workspace / repo-root stamps (or **adopt** an already-live lane, never clobbering it);
3. confirm the lane is visible with both panes and the identity stamps (fail-closed
   read-back);
4. dispatch the governed ``implementation_request`` to the gateway pane (coordinator ->
   sublane Codex gateway -> same-lane Claude worker), with the durable Redmine journal as
   the anchor.

Design decision (issue scope — "``workflow step`` に統合するか ``sublane start --dispatch`` を
内部 primitive とするか"): the live actuator is delivered as an **opt-in** ``sublane start
--execute`` path and kept as the internal primitive. ``sublane start`` without ``--execute``
remains the #12955 plan-only surface byte-for-byte (back-compat), so the standard,
side-effect-free UX is unchanged; ``--execute`` is the one-action live path and
``--no-dispatch`` stops after pane creation. The #12755 ``workflow step`` standard entrypoint
can later wrap this primitive without re-deriving the choreography. Rationale: #12955 already
owns the ``sublane`` surface and lane-identity vocabulary, ``workflow step`` is a not-yet-
implemented higher-level state machine, and the boundary-doc runbook already sequences
git-worktree-add -> cockpit-append -> dispatch as discrete steps.

Boundary: this actuator is **creation-side / additive only**. It never removes a worktree,
kills a pane, deletes a branch, or attempts a merge — the destructive retire half stays gated
behind a Design Consultation (``worktree-lifecycle-boundary.md``) and is untouched
(:func:`decide_retire_integration` / ``sublane retire`` still preflight-only). It self-
authorizes no close / carve-out / owner decision, and it dispatches only to the Codex gateway
rail (never a cross-lane Claude direct send, never a hidden subagent). Raw ``%pane`` typing is
never a normal-UX surface: pane ids are runtime evidence resolved from the inventory.

OOP-first boundary (mirrors #12933 ``launch_command`` / #12955 ``sublane_lifecycle_command``):
:class:`SublaneActuateUseCase` holds the fail-closed decision flow and never touches IO; the
:class:`SublaneActuatorOps` port owns every side effect; :class:`LiveSublaneActuatorOps`
composes the real primitives (the #12604 git ops + the ``cockpit append`` / ``handoff send``
CLI contract the coordinator already drives by hand); a typed
:class:`~...domain.sublane_actuation.SublaneActuationOutcome` carries the machine-readable,
replayable payload; and the thin ``cmd_sublane_start`` handler owns stdout and the exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_append_argv import resolve_append_lane_argv  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (
    LiveSublaneGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
    cmd_sublane_create,
    resolve_work_unit_request_fields,
)
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
    render_actuation_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    LAUNCH_BLOCKED,
    LAUNCH_CREATE_WORKTREE,
    LAUNCH_REUSE_WORKTREE,
    LaunchPreflight,
    SublaneIntegrationPolicy,
    WorktreeLaunchDecision,
    decide_worktree_launch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    SublaneCreateRequest,
    SublaneLaneView,
    parse_issue_from_lane_label,
    project_sublanes,
)


# ---------------------------------------------------------------------------
# Injected actuation operations port.
# ---------------------------------------------------------------------------


@runtime_checkable
class SublaneActuatorOps(Protocol):
    """Every side effect the live actuator needs, injected so tests drive fakes.

    Read probes (``is_git_workspace`` / ``worktree_exists``) mirror the #12604 git port.
    ``create_worktree`` is the single additive git mutation. ``append_lane_column`` stands
    up the cockpit-visible gateway + worker column for a worktree (binding the identity
    stamps). ``read_lane`` resolves the lane's :class:`SublaneLaneView` from the live pane
    inventory (used to adopt an existing lane and to confirm the created one on read-back).
    ``dispatch_implementation_request`` routes the governed handoff to the gateway pane and
    returns its exit code. There is intentionally no remove / kill / delete / merge method —
    the destructive half is gated and coordinator-owned.
    """

    def is_git_workspace(self) -> bool: ...

    def worktree_exists(self, branch: str) -> bool: ...

    def create_worktree(self, *, branch: str, worktree_path: str) -> None: ...

    def append_lane_column(self, worktree_path: str) -> None: ...

    def append_lane_argv(self, worktree_path: str) -> list[str]: ...

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]: ...

    def dispatch_implementation_request(
        self,
        *,
        issue: str,
        journal: str,
        gateway_pane: str,
        lane_label: str,
        upstream_coordinator: Optional[str],
        target_repo: str,
    ) -> int: ...


@dataclass(frozen=True)
class LiveSublaneActuatorOps:
    """Live adapter composing the real creation-side primitives for ``repo_root``.

    Git probes / additive ``git worktree add`` delegate to the #12604
    :class:`LiveSublaneGitOperations`. The cockpit append and the gateway dispatch drive the
    *existing CLI contract* the coordinator already runs by hand (``cockpit append
    --repo <worktree> --no-attach`` / ``handoff send ...``) through the composed argument
    parser, so this adapter reuses the proven, fully-defaulted code path instead of
    reconstructing a fragile Namespace. ``read_lane`` folds the live tmux pane inventory and
    matches the lane by repo-root.
    """

    repo_root: Path

    def _git(self) -> LiveSublaneGitOperations:
        return LiveSublaneGitOperations(repo_root=self.repo_root)

    def is_git_workspace(self) -> bool:
        return self._git().is_git_workspace()

    def worktree_exists(self, branch: str) -> bool:
        return self._git().worktree_exists(branch)

    def create_worktree(self, *, branch: str, worktree_path: str) -> None:
        self._git().create_worktree(branch=branch, worktree_path=worktree_path)

    def _drive_cli(self, argv: list[str]) -> int:
        """Parse ``argv`` with the composed CLI parser and run its handler (live).

        Mirrors :func:`mozyo_bridge.application.cli.main`'s dispatch (parse + normalize
        paths + ``func``) so an appended pane / dispatch is byte-for-byte what the operator
        would get from the shell command — the same fully-defaulted Namespace, the same
        ``require_tmux`` gate, the same outcome emission. Imported lazily so the pure use
        case / tests never require the CLI infrastructure.
        """
        from mozyo_bridge.application.cli import build_parser, normalize_paths

        args = build_parser().parse_args(argv)
        args = normalize_paths(args)
        return int(args.func(args))

    def append_lane_argv(self, worktree_path: str) -> list[str]:
        # #13155: one resolver shared by the live drive below and the dry-run preview.
        return resolve_append_lane_argv(worktree_path)

    def append_lane_column(self, worktree_path: str) -> None:
        rc = self._drive_cli(self.append_lane_argv(worktree_path))
        if rc != 0:
            raise RuntimeError(
                f"cockpit append failed for worktree {worktree_path!r} (exit {rc})"
            )

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
            try_pane_lines,
        )

        rows = try_pane_lines() or []
        target = _normalize_path(worktree_path)
        target_base = Path(worktree_path).name
        lanes = [lane for lane in project_sublanes(rows) if lane.repo_root]
        # Prefer an exact repo-root match; the returned lane's *identity* is still
        # validated against the request by the use case, so this only narrows the
        # candidate.
        for lane in lanes:
            if _normalize_path(lane.repo_root) == target:
                return lane
        # Basename fallback only when it is unambiguous — a single lane shares the
        # worktree basename. Returning an arbitrary basename collision would hand a
        # different repo's lane to the identity check (or, worse, pass it); require
        # uniqueness here and still let the use case validate identity.
        basename_matches = [
            lane for lane in lanes if Path(lane.repo_root).name == target_base
        ]
        if len(basename_matches) == 1:
            return basename_matches[0]
        return None

    def dispatch_implementation_request(
        self,
        *,
        issue: str,
        journal: str,
        gateway_pane: str,
        lane_label: str,
        upstream_coordinator: Optional[str],
        target_repo: str,
    ) -> int:
        argv = [
            "handoff",
            "send",
            "--to",
            "codex",
            "--source",
            "redmine",
            "--issue",
            issue,
            "--journal",
            journal,
            "--kind",
            "implementation_request",
            "--target",
            gateway_pane,
            "--target-repo",
            target_repo,
            "--mode",
            "queue-enter",
            "--role-profile",
            "implementation_gateway",
            "--profile-field",
            f"lane={lane_label}",
        ]
        if upstream_coordinator:
            argv += ["--profile-field", f"upstream_coordinator={upstream_coordinator}"]
        return self._drive_cli(argv)


def _normalize_path(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return path.strip().rstrip("/")


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

    def _decide_launch(
        self, request: SublaneCreateRequest
    ) -> WorktreeLaunchDecision:
        is_git = self.ops.is_git_workspace()
        identity_known = bool(request.branch) and bool(request.worktree_path)
        worktree_exists = (
            self.ops.worktree_exists(request.branch)
            if is_git and identity_known
            else False
        )
        preflight = LaunchPreflight(
            is_git_workspace=is_git,
            worktree_exists=worktree_exists,
            branch_resolved=bool(request.branch),
            target_identity_known=identity_known,
        )
        return decide_worktree_launch(self.policy, preflight)

    def run(
        self,
        request: SublaneCreateRequest,
        *,
        execute: bool,
        dispatch: bool = True,
        target_repo: str = "auto",
    ) -> SublaneActuationOutcome:
        # 1. Fail closed on missing identity before any probe.
        missing = request.missing_fields()
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

        # 4. Resolve the launch decision; a blocked launch is fail-closed. With every
        # identity field present (step 1 passed) the pure decision does not currently
        # return LAUNCH_BLOCKED, but this stays fail-closed if that contract changes.
        launch = self._decide_launch(request)
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
        return self._execute(request, launch, dispatch=dispatch, target_repo=target_repo)

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
        )

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
            f"git worktree add {request.worktree_path} -b {request.branch}"
            if launch.action == LAUNCH_CREATE_WORKTREE
            else None
        )
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
                command="mozyo-bridge " + " ".join(self.ops.append_lane_argv(request.worktree_path)),  # noqa: E501
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
    ) -> SublaneActuationOutcome:
        steps: list[ActuationStep] = []

        # Step 1 — worktree (create / reuse / skip).
        if launch.action == LAUNCH_CREATE_WORKTREE:
            try:
                self.ops.create_worktree(
                    branch=request.branch, worktree_path=request.worktree_path
                )
            except Exception as exc:  # noqa: BLE001 — surface any git failure fail-closed.
                steps.append(
                    ActuationStep(
                        order=1,
                        title="create worktree",
                        status=STEP_BLOCKED,
                        detail=f"git worktree add failed: {exc}",
                        command=f"git worktree add {request.worktree_path} "
                        f"-b {request.branch}",
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
                )
            steps.append(
                ActuationStep(
                    order=1,
                    title="create worktree",
                    status=STEP_EXECUTED,
                    detail=f"created worktree {request.worktree_path} on branch "
                    f"{request.branch}",
                    command=f"git worktree add {request.worktree_path} -b "
                    f"{request.branch}",
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
        existing = self.ops.read_lane(request.worktree_path)
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
                self.ops.append_lane_column(request.worktree_path)
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
                )
            lane = self.ops.read_lane(request.worktree_path)
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
            )
        gateway_pane = lane.gateway_pane
        worker_pane = lane.worker_pane
        steps.append(
            ActuationStep(
                order=3,
                title="confirm lane stamps",
                status=STEP_EXECUTED,
                detail=f"lane visible: repo_root={lane.repo_root} "
                f"gateway={gateway_pane} worker={worker_pane} state={lane.state}",
                command=None,
            )
        )

        # Step 4 — dispatch the governed implementation_request to the gateway.
        if not dispatch:
            steps.append(
                ActuationStep(
                    order=4,
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
            )

        try:
            rc = self.ops.dispatch_implementation_request(
                issue=request.issue,
                journal=(request.journal or ""),
                gateway_pane=gateway_pane or "",
                lane_label=request.lane_label,
                upstream_coordinator=request.upstream_coordinator,
                target_repo=target_repo,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed on any dispatch failure.
            rc = 1
            dispatch_detail = f"handoff dispatch raised: {exc}"
        else:
            dispatch_detail = f"handoff send to gateway {gateway_pane} exit={rc}"
        if rc != 0:
            steps.append(
                ActuationStep(
                    order=4,
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
            )
        steps.append(
            ActuationStep(
                order=4,
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
    ) -> SublaneActuationOutcome:
        return SublaneActuationOutcome(
            status=ACTUATE_EXECUTED,
            execute=True,
            reason="sublane actuated: "
            + ("adopted existing lane" if adopted else "created lane")
            + f"; launch action {launch.action!r}"
            + self._dispatch_reason_suffix(dispatch_result),
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

    def _dispatch_command(self, request: SublaneCreateRequest) -> str:
        journal = request.journal or "<journal>"
        coordinator = request.upstream_coordinator or "<coordinator-pane>"
        return (
            "mozyo-bridge handoff send --to codex --source redmine "
            f"--issue {request.issue} --journal {journal} "
            "--kind implementation_request --target <gateway-pane> --target-repo auto "
            "--mode queue-enter --role-profile implementation_gateway "
            f"--profile-field lane={request.lane_label} "
            f"--profile-field upstream_coordinator={coordinator}"
        )


# ---------------------------------------------------------------------------
# Text rendering (pure).
# ---------------------------------------------------------------------------


def format_actuate_text(outcome: SublaneActuationOutcome) -> str:
    header = "sublane start"
    if not outcome.execute:
        header += " (dry-run)"
    lines = [f"{header}: {outcome.status}", f"  reason: {outcome.reason}"]
    if outcome.launch_action:
        lines.append(f"  launch_action: {outcome.launch_action}")
    if outcome.gateway_pane or outcome.worker_pane:
        lines.append(
            f"  lane: gateway={outcome.gateway_pane or '-'} "
            f"worker={outcome.worker_pane or '-'} "
            f"adopted={str(outcome.adopted).lower()}"
        )
    if outcome.dispatch_target:
        lines.append(
            f"  dispatch: {outcome.dispatch_result} -> {outcome.dispatch_target}"
        )
        if outcome.dispatch_result == DISPATCH_GATEWAY_NOTIFIED:
            lines.append(
                "  ! gateway notified only; worker dispatch NOT confirmed "
                "(worker_dispatch_confirmed=false). Drive the ack with "
                "`sublane dispatch-worker --execute` (#12988); if no worker "
                "progress lands, classify with `sublane callback-recovery "
                "--dispatch-delivered`"
            )
    if outcome.is_blocked:
        lines.append("  -> blocked: " + ", ".join(outcome.blocked_reasons))
    else:
        for step in outcome.steps:
            lines.append(f"  {step.order}. [{step.status}] {step.title}: {step.detail}")
            if step.command:
                lines.append(f"       $ {step.command}")
    lines.append("  durable record:")
    for jline in render_actuation_journal(outcome).splitlines():
        lines.append(f"    {jline}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Thin CLI handler.
# ---------------------------------------------------------------------------


def _repo_root(args: argparse.Namespace) -> Path:
    repo = getattr(args, "repo", None)
    return Path(repo).expanduser() if repo else Path.cwd()


def cmd_sublane_start(args: argparse.Namespace) -> int:
    """``sublane create`` / ``start`` dispatcher.

    With neither ``--execute`` nor ``--dry-run`` this is the #12955 plan-only surface
    (delegates byte-for-byte to :func:`cmd_sublane_create`, back-compat). ``--dry-run``
    previews the one-action actuation plan with no side effect (and wins over ``--execute``
    when both are given); ``--execute`` runs the live actuator.
    """
    execute = bool(getattr(args, "execute", False))
    dry_run = bool(getattr(args, "dry_run", False))
    if not execute and not dry_run:
        return cmd_sublane_create(args)

    repo_root = _repo_root(args)
    # Resolve the #13002 work-unit granularity exactly as the plan-only surface
    # does (flag > repo-local config > user_story default), failing closed on a
    # present-but-broken config instead of silently actuating the default unit.
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
        RepoLocalConfigError,
    )

    try:
        work_unit, decision_anchor = resolve_work_unit_request_fields(args, repo_root)
    except RepoLocalConfigError as exc:
        print(f"invalid repo-local config: {exc}", file=sys.stderr)
        return 1

    request = SublaneCreateRequest(
        issue=getattr(args, "issue", "") or "",
        lane_label=getattr(args, "lane_label", "") or "",
        branch=getattr(args, "branch", "") or "",
        worktree_path=getattr(args, "worktree", "") or "",
        journal=getattr(args, "journal", None),
        upstream_coordinator=getattr(args, "upstream_coordinator", None),
        work_unit=work_unit,
        work_unit_decision_anchor=decision_anchor,
    )
    use_case = SublaneActuateUseCase(LiveSublaneActuatorOps(repo_root=repo_root))
    outcome = use_case.run(
        request,
        execute=execute and not dry_run,
        dispatch=not getattr(args, "no_dispatch", False),
        target_repo=getattr(args, "target_repo", None) or "auto",
    )
    if getattr(args, "json", False):
        print(
            json.dumps(
                outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        print(format_actuate_text(outcome))
    return 1 if outcome.is_blocked else 0


__all__ = (
    "SublaneActuatorOps",
    "LiveSublaneActuatorOps",
    "SublaneActuateUseCase",
    "format_actuate_text",
    "cmd_sublane_start",
)
