"""Fleet rehydrate actuation: the injected port, its live adapter, and the use case (#15745).

The ``--execute`` half of :mod:`.sublane_fleet_rehydrate`. It introduces **no new rail**: the
pair heal is the existing ``sublane create`` adopt-or-launch
(:class:`...sublane_actuator_use_case.SublaneActuateUseCase`), the dispatch restore is that
same create's governed dispatch leg re-issued under the SAME durable anchor, and the resume
brief is the canonical ``handoff send`` primitive with the fixed role profile. Raw Herdr /
tmux calls, manual ``MOZYO_*`` injection, provider-UI answers, direct store writes, worktree
clobbering and branch rewrites are all absent by construction — every side effect this module
can produce goes through one of those two composed commands.

Three properties the use case enforces and a regression pins:

- **Identity is re-joined at action time.** A plan is an observation; between it and the
  effect a peer lane can supersede, hibernate, retire or re-declare the row. Before each
  lane's first effect the use case re-reads the lifecycle authority and refuses
  (:data:`...fleet_rehydrate.BLOCK_LANE_MOVED`, zero effect) when the disposition, revision,
  generation or worktree binding moved.
- **Failure is truthful and does not cascade.** A lane records exactly the actions that were
  applied. A failed heal blocks that lane's remaining actions — a dispatch must never land on
  a pair that was not brought up — and the lane's outcome says so instead of reporting a
  partial success. Other lanes are unaffected: one lane's block is not the fleet's.
- **Re-running is idempotent because the authorities are re-read, not remembered.** The use
  case holds no cross-run state; a second pass re-derives every fact, so a dispatch that
  landed on the first pass is no longer ``owed`` and is not re-issued.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, Optional, Protocol, Sequence

from mozyo_bridge.core.state.lane_kind import LANE_KIND_DELEGATED_COORDINATOR
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate import (  # noqa: E501
    ACTION_HEAL_PAIR,
    ACTION_RESTORE_DISPATCH,
    ACTION_RESUME_BRIEF,
    BLOCKED,
    BLOCK_LANE_MOVED,
    DISPATCH_ATTRIBUTION_UNKNOWN,
    DISPATCH_NOT_APPLICABLE,
    DISPATCH_OWED,
    DISPATCH_UNREADABLE,
    FleetLaneFacts,
    FleetLanePlan,
    REHYDRATE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate_dispatch_fold import (  # noqa: E501
    KIND_IMPLEMENTATION_REQUEST,
    KIND_REPLY,
    fold_dispatch_state,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SublaneCreateRequest,
)

#: The lane's planned actions were all applied.
STATUS_APPLIED = "applied"
#: The lane was not actionable (a typed plan skip / block); nothing was attempted.
STATUS_SKIPPED = "skipped"
#: An action was attempted and refused, or a pre-effect re-join failed. ``applied`` still
#: names whatever landed BEFORE the refusal — a partial run is reported, never rounded up.
STATUS_BLOCKED = "blocked"

#: The refusal token for a heal whose composed create returned non-zero.
REFUSED_HEAL_FAILED = "heal_failed"
#: The refusal token for a dispatch restore whose composed send returned non-zero.
REFUSED_DISPATCH_FAILED = "dispatch_failed"
#: The refusal token for a resume brief whose composed send returned non-zero.
REFUSED_RESUME_BRIEF_FAILED = "resume_brief_failed"
#: The lane's gateway target could not be resolved after the heal, so no send is addressable.
REFUSED_GATEWAY_UNRESOLVED = "gateway_target_unresolved"
#: The lane has no recorded worktree / branch to compose a create request from.
REFUSED_LANE_IDENTITY_INCOMPLETE = "lane_identity_incomplete"
#: The action-time re-fold found the causal key no longer ``owed`` (it landed by another
#: path, its receiver was replaced, or the authority became unreadable) — Redmine #15745
#: review j#108920 ``finding_actiontimefence``. Zero additional effect.
REFUSED_DISPATCH_STATE_MOVED = "dispatch_state_moved"


@dataclass(frozen=True)
class LaneActuationOutcome:
    """One lane's truthful actuation result (Redmine #15745 acceptance 8)."""

    lane_id: str
    issue_id: str
    status: str
    applied: tuple[str, ...] = ()
    attempted: tuple[str, ...] = ()
    reason: str = ""
    detail: str = ""
    gateway_target: str = ""

    def as_payload(self) -> dict:
        return {
            "lane_id": self.lane_id,
            "issue_id": self.issue_id,
            "status": self.status,
            "applied": list(self.applied),
            "attempted": list(self.attempted),
            "reason": self.reason,
            "detail": self.detail,
            "gateway_target": self.gateway_target,
        }


@dataclass(frozen=True)
class HealResult:
    """The composed create's outcome, with the refusal named rather than a bare exit code.

    ``reason`` is empty on success and otherwise one of the ``REFUSED_*`` tokens, so a
    caller distinguishes "the create rail refused" from "this lane never had a composable
    identity" without re-deriving either from a number.
    """

    ok: bool
    gateway_target: str = ""
    reason: str = ""


@dataclass(frozen=True)
class LaneIdentityPin:
    """The lifecycle axes a plan is bound to, re-measured at action time."""

    disposition: str = ""
    revision: int = 0
    lane_generation: int = 0
    worktree_identity: str = ""
    issue_id: str = ""

    @classmethod
    def from_facts(cls, facts: FleetLaneFacts) -> "LaneIdentityPin":
        reboot = facts.reboot
        return cls(
            disposition=reboot.lane_disposition,
            revision=reboot.revision,
            lane_generation=reboot.lane_generation,
            worktree_identity=reboot.worktree_identity,
            issue_id=reboot.issue_id,
        )


class FleetRehydrateOps(Protocol):
    """Every side effect the fleet rehydrate can produce, as one injected boundary."""

    def current_identity(
        self, *, workspace_id: str, lane_id: str
    ) -> Optional[LaneIdentityPin]:
        """The lane's lifecycle identity right now (``None`` when unreadable / absent)."""
        ...

    def heal_lane(self, facts: FleetLaneFacts, *, dispatch: bool) -> HealResult:
        """Adopt-or-launch the lane's pair, optionally re-issuing its anchored dispatch.

        A non-``ok`` result means nothing further may be attempted for that lane.
        """
        ...

    def current_dispatch_state(self, facts: FleetLaneFacts, *, kind: str) -> str:
        """Re-fold ONE causal key against freshly-read authorities (Redmine #15745 j#108920).

        The action-time half of the replay fence: the plan's fold is an observation, and the
        window between it and an irreversible send is long enough for the same key to land
        by another path, for the receiver to be replaced, or for the ledger to become
        unreadable. Returns a :data:`...fleet_rehydrate.DISPATCH_STATES` token.
        """
        ...

    def send_resume_brief(
        self, facts: FleetLaneFacts, *, gateway_target: str
    ) -> int:
        """Deliver the delegated-coordinator resume pointer with its fixed role profile."""
        ...


@dataclass
class LiveFleetRehydrateOps:
    """The live adapter: composes the existing create + handoff commands, nothing else."""

    repo_root: Path
    quiet_stdout: bool = False
    target_repo: str = "auto"

    # -- identity -----------------------------------------------------------

    def current_identity(
        self, *, workspace_id: str, lane_id: str
    ) -> Optional[LaneIdentityPin]:
        from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey
        from mozyo_bridge.core.state.lane_lifecycle_readonly import (
            LaneLifecycleReader,
        )
        from mozyo_bridge.core.state.lane_lifecycle_schema import LaneLifecycleError

        try:
            record = LaneLifecycleReader().get(LaneLifecycleKey(workspace_id, lane_id))
        except (LaneLifecycleError, OSError):
            # Unreadable is NOT absent: returning None here makes the use case refuse,
            # which is the fail-closed direction (an unreadable authority never licenses
            # an effect).
            return None
        if record is None:
            return None
        return LaneIdentityPin(
            disposition=record.lane_disposition,
            revision=record.revision,
            lane_generation=record.lane_generation,
            worktree_identity=record.worktree_identity,
            issue_id=record.issue_id,
        )

    # -- composed effects ---------------------------------------------------

    def _create_request(self, facts: FleetLaneFacts) -> Optional[SublaneCreateRequest]:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            resolve_work_unit_request_fields,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_fleet_rehydrate import (  # noqa: E501
            parent_callback_route,
        )

        reboot = facts.reboot
        worktree = (reboot.recorded_worktree or "").strip()
        branch = (reboot.branch or "").strip()
        anchor = facts.dispatch.anchor_journal
        if not worktree or not branch or not anchor:
            return None
        # The SAME precedence `sublane create` applies (explicit flag > repo-local
        # `work_unit.granularity` > `user_story`). This rail asserts no granularity of its
        # own: a restore re-issues a send that never reached the receiver, so the unit it
        # carries is the one the repo's configured standard would have carried anyway.
        work_unit, decision_anchor = resolve_work_unit_request_fields(
            SimpleNamespace(work_unit=None, work_unit_decision_journal=None),
            self.repo_root,
        )
        return SublaneCreateRequest(
            issue=reboot.issue_id,
            lane_label=reboot.lane_id,
            branch=branch,
            worktree_path=worktree,
            journal=anchor,
            upstream_coordinator=parent_callback_route(facts.parent_lane_id),
            work_unit=work_unit,
            work_unit_decision_anchor=decision_anchor,
            lane_kind=facts.lane_kind,
        )

    def heal_lane(self, facts: FleetLaneFacts, *, dispatch: bool) -> HealResult:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
            _resolve_sublane_ops,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_use_case import (  # noqa: E501
            SublaneActuateUseCase,
        )

        request = self._create_request(facts)
        if request is None:
            return HealResult(ok=False, reason=REFUSED_LANE_IDENTITY_INCOMPLETE)
        ops = _resolve_sublane_ops(
            SimpleNamespace(),
            repo_root=self.repo_root,
            request=request,
            quiet_stdout=self.quiet_stdout,
        )
        outcome = SublaneActuateUseCase(ops).run(
            request,
            execute=True,
            dispatch=dispatch,
            target_repo=self.target_repo,
            fill_inputs=None,
            override_fill_stop=None,
        )
        gateway = (getattr(outcome, "gateway_pane", "") or "").strip()
        if outcome.is_blocked:
            return HealResult(
                ok=False,
                gateway_target=gateway,
                reason=REFUSED_DISPATCH_FAILED if dispatch else REFUSED_HEAL_FAILED,
            )
        return HealResult(ok=True, gateway_target=gateway)

    def current_dispatch_state(self, facts: FleetLaneFacts, *, kind: str) -> str:
        """Re-read ledger + live inventory and re-fold this key (action-time, read-only)."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_fleet_rehydrate import (  # noqa: E501
            ledger_records_for_issue,
            receiver_binding_for,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )

        key = facts.dispatch if kind == KIND_IMPLEMENTATION_REQUEST else facts.resume_brief
        if not key.anchor_journal:
            return DISPATCH_NOT_APPLICABLE
        records, detail = ledger_records_for_issue(key.anchor_issue, home=None)
        if records is None:
            return DISPATCH_UNREADABLE
        try:
            rows = list_herdr_agent_rows(os.environ)
        except HerdrSessionStartError:
            # The inventory is the attribution authority; without it no record can be
            # placed, and an unplaceable record is never folded into "owed".
            return DISPATCH_ATTRIBUTION_UNKNOWN
        receiver = facts.managed_roles[0] if facts.managed_roles else ""
        binding = receiver_binding_for(
            rows,
            workspace_id=facts.workspace_id,
            lane_id=facts.lane_id,
            role=receiver,
            managed_roles=facts.managed_roles,
        )
        return fold_dispatch_state(
            records,
            marker=key.marker,
            kind=kind,
            receiver=receiver,
            binding=binding,
        )

    def send_resume_brief(self, facts: FleetLaneFacts, *, gateway_target: str) -> int:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            WorkflowProviderUnresolved,
            resolve_gateway_provider,
        )

        try:
            receiver = resolve_gateway_provider(str(self.repo_root))
        except WorkflowProviderUnresolved:
            return 1
        argv = [
            "handoff",
            "send",
            "--to",
            receiver,
            "--source",
            "redmine",
            "--issue",
            facts.resume_brief.anchor_issue,
            "--journal",
            facts.resume_brief.anchor_journal,
            # A resume is a POINTER at an existing anchor (the #14203 / #14661 shape); it
            # never regenerates an implementation_request or a review_request.
            "--kind",
            "reply",
            "--target",
            gateway_target,
            "--target-repo",
            (facts.reboot.recorded_worktree or "").strip() or self.target_repo,
            "--target-lane",
            facts.lane_id,
            "--mode",
            "queue-enter",
            "--role-profile",
            LANE_KIND_DELEGATED_COORDINATOR,
        ]
        for name, value in facts.resume_profile_fields:
            argv += ["--profile-field", f"{name}={value}"]
        return self._drive_cli(argv)

    def _drive_cli(self, argv: list[str]) -> int:
        """Parse ``argv`` with the composed CLI parser and run its handler (live).

        The same dispatch :class:`...sublane_actuator_ops.LiveSublaneActuatorOps` uses, so a
        composed send is byte-for-byte what the operator would get from the shell command —
        the same fully-defaulted namespace, the same gates, the same outcome emission.
        """
        from mozyo_bridge.application.cli import build_parser, normalize_paths

        args = normalize_paths(build_parser().parse_args(argv))
        if self.quiet_stdout:
            with contextlib.redirect_stdout(sys.stderr):
                return int(args.func(args))
        return int(args.func(args))


@dataclass
class FleetRehydrateUseCase:
    """Drive the planned actions lane by lane, fail-closed and truthfully (#15745)."""

    ops: FleetRehydrateOps

    def run(
        self,
        facts: Sequence[FleetLaneFacts],
        plans: Sequence[FleetLanePlan],
    ) -> tuple[Mapping[str, object], ...]:
        by_lane = {f.lane_id: f for f in facts}
        return tuple(
            self._run_lane(by_lane[p.lane_id], p).as_payload()
            for p in plans
            if p.lane_id in by_lane
        )

    def _run_lane(
        self, facts: FleetLaneFacts, plan: FleetLanePlan
    ) -> LaneActuationOutcome:
        if plan.disposition != REHYDRATE or not plan.actions:
            # A plan-level BLOCK is carried through as a blocked OUTCOME, not folded into
            # `skipped`: "this lane was deliberately out of scope" and "this lane could not
            # be rehydrated" are different facts, and only the second should colour the
            # command's exit code.
            return LaneActuationOutcome(
                lane_id=plan.lane_id,
                issue_id=plan.issue_id,
                status=STATUS_BLOCKED if plan.disposition == BLOCKED else STATUS_SKIPPED,
                reason=plan.reason,
                detail=plan.detail,
            )
        moved = self._identity_moved(facts)
        if moved is not None:
            return LaneActuationOutcome(
                lane_id=plan.lane_id,
                issue_id=plan.issue_id,
                status=STATUS_BLOCKED,
                attempted=(),
                reason=BLOCK_LANE_MOVED,
                detail=moved,
            )

        applied: list[str] = []
        attempted: list[str] = []
        heal = plan.has(ACTION_HEAL_PAIR)
        restore = plan.has(ACTION_RESTORE_DISPATCH)
        gateway_target = self._observed_gateway(facts)

        if restore:
            # The LAST thing before the composed create's dispatch leg: re-fold this key
            # against freshly-read authorities (review j#108920 finding_actiontimefence).
            # A key that stopped being `owed` between the plan and here must not be sent.
            fresh = self.ops.current_dispatch_state(
                facts, kind=KIND_IMPLEMENTATION_REQUEST
            )
            if fresh != DISPATCH_OWED:
                if not heal:
                    return self._dispatch_state_moved(
                        plan, applied, (ACTION_RESTORE_DISPATCH,), fresh, gateway_target
                    )
                # The pair still needs bringing up; drop only the send. Healing a lane is
                # additive and is not invalidated by the dispatch having landed elsewhere.
                restore = False

        if heal or restore:
            # ONE composed `sublane create --execute` covers both: its adopt-or-launch is
            # idempotent for a surviving pair, and its dispatch leg is the governed send.
            # Running them as two rails would either double-adopt or invent a second
            # dispatch path.
            attempted.append(ACTION_HEAL_PAIR if heal else ACTION_RESTORE_DISPATCH)
            if heal and restore:
                attempted.append(ACTION_RESTORE_DISPATCH)
            result = self.ops.heal_lane(facts, dispatch=restore)
            if result.gateway_target:
                gateway_target = result.gateway_target
            if not result.ok:
                return LaneActuationOutcome(
                    lane_id=plan.lane_id,
                    issue_id=plan.issue_id,
                    status=STATUS_BLOCKED,
                    applied=(),
                    attempted=tuple(attempted),
                    reason=result.reason or REFUSED_HEAL_FAILED,
                    detail="the composed `sublane create --execute` refused; the lane's "
                    "remaining actions were not attempted",
                    gateway_target=gateway_target,
                )
            if heal:
                applied.append(ACTION_HEAL_PAIR)
            if restore:
                applied.append(ACTION_RESTORE_DISPATCH)

        if plan.has(ACTION_RESUME_BRIEF):
            attempted.append(ACTION_RESUME_BRIEF)
            # The heal above launched processes and ran a governed send; that window is
            # long enough for the lane to move or for this very brief to land by another
            # path. Re-join BOTH authorities immediately before the brief's own send
            # (review j#108920 finding_actiontimefence) — "once before the lane's first
            # effect" is exactly the shape #14661 j#92656 F2 rejected.
            moved = self._identity_moved(facts)
            if moved is not None:
                return LaneActuationOutcome(
                    lane_id=plan.lane_id,
                    issue_id=plan.issue_id,
                    status=STATUS_BLOCKED,
                    applied=tuple(applied),
                    attempted=tuple(attempted),
                    reason=BLOCK_LANE_MOVED,
                    detail=moved,
                    gateway_target=gateway_target,
                )
            fresh_brief = self.ops.current_dispatch_state(facts, kind=KIND_REPLY)
            if fresh_brief != DISPATCH_OWED:
                return self._dispatch_state_moved(
                    plan, applied, tuple(attempted), fresh_brief, gateway_target
                )
            if not gateway_target:
                return LaneActuationOutcome(
                    lane_id=plan.lane_id,
                    issue_id=plan.issue_id,
                    status=STATUS_BLOCKED,
                    applied=tuple(applied),
                    attempted=tuple(attempted),
                    reason=REFUSED_GATEWAY_UNRESOLVED,
                    detail="no gateway target resolved for the brief; nothing was sent",
                )
            if self.ops.send_resume_brief(facts, gateway_target=gateway_target) != 0:
                return LaneActuationOutcome(
                    lane_id=plan.lane_id,
                    issue_id=plan.issue_id,
                    status=STATUS_BLOCKED,
                    applied=tuple(applied),
                    attempted=tuple(attempted),
                    reason=REFUSED_RESUME_BRIEF_FAILED,
                    detail="the composed resume `handoff send` refused",
                    gateway_target=gateway_target,
                )
            applied.append(ACTION_RESUME_BRIEF)

        return LaneActuationOutcome(
            lane_id=plan.lane_id,
            issue_id=plan.issue_id,
            status=STATUS_APPLIED,
            applied=tuple(applied),
            attempted=tuple(attempted),
            gateway_target=gateway_target,
        )

    @staticmethod
    def _dispatch_state_moved(
        plan: FleetLanePlan,
        applied: Sequence[str],
        attempted: Sequence[str],
        observed: str,
        gateway_target: str,
    ) -> LaneActuationOutcome:
        """Zero additional effect: the key stopped being owed before its send."""
        return LaneActuationOutcome(
            lane_id=plan.lane_id,
            issue_id=plan.issue_id,
            status=STATUS_BLOCKED,
            applied=tuple(applied),
            attempted=tuple(attempted),
            reason=REFUSED_DISPATCH_STATE_MOVED,
            detail=(
                "the causal key was re-folded immediately before its send and is no longer "
                f"owed (observed={observed}); nothing further was sent"
            ),
            gateway_target=gateway_target,
        )

    def _identity_moved(self, facts: FleetLaneFacts) -> Optional[str]:
        """Re-join the lifecycle identity immediately before the lane's first effect."""
        expected = LaneIdentityPin.from_facts(facts)
        current = self.ops.current_identity(
            workspace_id=facts.workspace_id, lane_id=facts.lane_id
        )
        if current is None:
            return "the lane's lifecycle row is absent or unreadable at action time"
        if current != expected:
            return (
                "the lifecycle row moved between the plan and the effect "
                f"(disposition={current.disposition} rev={current.revision} "
                f"gen={current.lane_generation})"
            )
        return None

    @staticmethod
    def _observed_gateway(facts: FleetLaneFacts) -> str:
        """The live locator of the lane's gateway slot, when exactly one resolves."""
        if not facts.managed_roles:
            return ""
        role = facts.managed_roles[0]
        matching = [
            s
            for s in (facts.reboot.slots or ())
            if s.role == role and s.is_live_agent
        ]
        return matching[0].locator if len(matching) == 1 else ""


__all__ = (
    "FleetRehydrateOps",
    "FleetRehydrateUseCase",
    "HealResult",
    "LaneActuationOutcome",
    "LaneIdentityPin",
    "LiveFleetRehydrateOps",
    "REFUSED_DISPATCH_FAILED",
    "REFUSED_DISPATCH_STATE_MOVED",
    "REFUSED_GATEWAY_UNRESOLVED",
    "REFUSED_HEAL_FAILED",
    "REFUSED_LANE_IDENTITY_INCOMPLETE",
    "REFUSED_RESUME_BRIEF_FAILED",
    "STATUS_APPLIED",
    "STATUS_BLOCKED",
    "STATUS_SKIPPED",
)
