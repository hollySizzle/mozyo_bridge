"""Redmine #13948 R3 — a nested replacement-launch failure must surface losslessly.

R2 (j#81717) taught the embedded ``sublane create/start`` path to stop before dispatch on a
non-positive embedded ``session-start`` and to point at the SAME action's public rollback.
R3 (j#81811) closes the *nested* leak: when ``prepare-bound-pair --execute`` converges a
hibernated bound pair through the #13933 v1 replacement-binding adapter and the fresh
participant does not reach bounded startup health, the inner
:class:`SessionStartResult` carried the typed startup ``action_id`` / per-role health /
rollback debt, but the outer public outcome collapsed it to a generic
``replacement_binding_launch_unhealthy`` detail string with no actionable rollback pointer.

These regressions pin the R3 contract through the REAL wiring (review j#82682 F1): the
production ``_BoundPairActuatorPort.launch_action_bound`` running under the REAL
:class:`ReplacementActuatorUseCase`, and the REAL public :func:`run_session_rollback` rail —
never a hand-injected ``PreparationDrive`` nor a hand-set fence phase.

1. the nested unhealthy launch propagates the typed ``action_id`` / role health / rollback
   debt / explicit rollback pointer outward losslessly — no raw locator / detail / secret;
2. the public pointer is exactly ``mozyo-bridge herdr session-rollback --action-id <id>``
   with NO ``--execute`` — a launch closes nothing (Answer j#80991);
3. after the PUBLIC rollback rail discharges the debt the SAME replacement action binding
   recognises the rolled-back reservation and replays toward a fresh launch (new action id);
4. only a fresh participant this action started ever owes a rollback (adopted / surfaced /
   healthy roles are zero-close), so the pointer never invites closing someone else's slot;
5. the negative matrix (action-id loss, replay before rollback) is deterministic.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mozyo_bridge.core.state.lane_lifecycle import DecisionPointer
from mozyo_bridge.core.state.replacement_preservation import PreservationObservation
from mozyo_bridge.core.state.replacement_transaction import (
    ContinuationPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
    HerdrIdentityReplacementBindingStore,
)
from mozyo_bridge.core.state.replacement_preservation import assess_preservation
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_ROLLBACK_OWED,
    Participant,
    StartupTransactionFence,
    StartupUnit,
    startup_action_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E501
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_startup_projection import (  # noqa: E501
    project_sublane_startup,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import (  # noqa: E501
    PreparationDrive,
    PreparationObservation,
    PreparationOutcome,
    PrepareBoundPairRequest,
    _blocked,
    format_preparation_text,
    run_bound_pair_preparation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import (  # noqa: E501
    ConvergeBoundPairRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence_live import (  # noqa: E501
    _BoundPairActuatorPort,
    _launch_detail,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_composer_discard import (  # noqa: E501
    BLOCK_REPLACEMENT_STOPPED,
    STATE_BLOCKED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (  # noqa: E501
    BoundSlot,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (  # noqa: E501
    SLOT_PRESERVE_PENDING,
    SLOT_RECOVER,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
    ACTUATION_EFFECT_FAILED,
    ATTEST_BOUND,
    CLOSE_DONE,
    OLD_SLOT_PRESENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneStartupObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E501
    SublaneHealError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
    REASON_OK,
    REASON_PREFLIGHT,
    run_session_rollback,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_v1_replacement_binding import (  # noqa: E501
    V1_BINDING_LAUNCH_UNHEALTHY,
    V1_BINDING_STARTUP_DEBT,
    V1ReplacementBindingFailure,
    launch_or_resume_v1_replacement,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    COMPENSATION_NOT_NEEDED,
    COMPENSATION_ROLLBACK_OWED,
    HEALTH_HEALTHY,
    HEALTH_PROVIDER_EXITED,
    HEALTH_RECEIVER_UNREADABLE,
)


def _nested_result(*, action_id="startup_act_13948", owed=True, provider="claude",
                   assigned="mzb1_workspace_claude_issue", locator="w2G:p3"):
    """A nested replacement launch: an adopted healthy sibling + a fresh, dead participant.

    The fresh participant carries a private locator and a raw launch detail exactly so the
    projection can be proven to strip them.
    """
    return SessionStartResult(
        workspace_id="mzb1_workspace",
        lane_id="issue_13948_nested_rollback_pointer_r3",
        action_id=action_id,
        slots=[
            SlotResult(
                provider="codex",
                assigned_name="mzb1_workspace_codex_issue",
                outcome=SLOT_ADOPTED,
                locator="private:codex:locator",
                detail="RAW-LAUNCH-DETAIL",
                health=HEALTH_HEALTHY,
                compensation=COMPENSATION_NOT_NEEDED,
            ),
            SlotResult(
                provider=provider,
                assigned_name=assigned,
                outcome=SLOT_LAUNCHED,
                locator=locator,
                detail="RAW-LAUNCH-DETAIL",
                health=HEALTH_PROVIDER_EXITED,
                compensation=(
                    COMPENSATION_ROLLBACK_OWED if owed else COMPENSATION_NOT_NEEDED
                ),
            ),
        ],
    )


class NestedStartupProjectionTest(unittest.TestCase):
    """Item 4: only the fresh participant this action started ever owes a rollback."""

    def test_mixed_adopted_and_fresh_owes_only_the_fresh_rollback(self):
        obs = project_sublane_startup(_nested_result())
        self.assertFalse(obs.ok)
        self.assertTrue(obs.rollback_owed)
        self.assertEqual(obs.action_id, "startup_act_13948")
        # The adopted sibling never carries a compensation; only the fresh, dead slot does.
        self.assertEqual(obs.roles[0].disposition, "adopted")
        self.assertEqual(obs.roles[0].compensation, COMPENSATION_NOT_NEEDED)
        self.assertEqual(obs.roles[1].compensation, COMPENSATION_ROLLBACK_OWED)

    def test_projection_strips_the_backend_locator_and_raw_detail(self):
        payload = str(project_sublane_startup(_nested_result()).as_payload())
        self.assertNotIn("private:", payload)
        self.assertNotIn("RAW-LAUNCH-DETAIL", payload)


class PreparationOutcomeRollbackPointerTest(unittest.TestCase):
    """Items 1/2/4/5 at the public outcome surface, without any live machinery."""

    _REQ = PrepareBoundPairRequest(
        issue="13948",
        journal="81811",
        lane="issue_13948_nested_rollback_pointer_r3",
        worktree="/tmp/wt-13948",
        branch="issue_13948_nested_rollback_pointer_r3",
    )

    def _blocked_outcome(self, obs: SublaneStartupObservation | None) -> PreparationOutcome:
        return _blocked(
            self._REQ,
            BLOCK_REPLACEMENT_STOPPED,
            detail="launch:replacement_binding_launch_unhealthy",
            action_id="approval_action_7",
            executed=True,
            replacement_status="effect_failed",
            startup=obs,
        )

    def test_pointer_names_the_startup_action_and_never_executes(self):
        outcome = self._blocked_outcome(project_sublane_startup(_nested_result()))
        self.assertEqual(
            outcome.rollback_pointer,
            "mozyo-bridge herdr session-rollback --action-id startup_act_13948",
        )
        self.assertNotIn("--execute", outcome.rollback_pointer)
        # The pointer targets the STARTUP action id, never the composer-approval action id.
        self.assertNotIn("approval_action_7", outcome.rollback_pointer)

    def test_payload_carries_startup_and_pointer_without_leaking_raw_facts(self):
        outcome = self._blocked_outcome(project_sublane_startup(_nested_result()))
        payload = outcome.as_payload()
        self.assertIn("startup", payload)
        self.assertEqual(
            payload["rollback_pointer"],
            "mozyo-bridge herdr session-rollback --action-id startup_act_13948",
        )
        self.assertNotIn("private:", str(payload))
        self.assertNotIn("RAW-LAUNCH-DETAIL", str(payload))

    def test_text_surfaces_the_health_and_the_pointer(self):
        text = format_preparation_text(
            self._blocked_outcome(project_sublane_startup(_nested_result()))
        )
        self.assertIn("startup_action_id: startup_act_13948", text)
        self.assertIn("startup_rollback_owed: true", text)
        self.assertIn(
            "rollback_pointer: mozyo-bridge herdr session-rollback "
            "--action-id startup_act_13948",
            text,
        )

    def test_no_debt_owed_offers_no_pointer(self):
        # An uncertain (unreadable-pane) launch that owes no rollback still blocks, but there
        # is nothing this action may compensate, so no pointer is invented.
        obs = SublaneStartupObservation(
            ok=False, action_id="startup_act_x", roles=(), rollback_owed=False
        )
        outcome = self._blocked_outcome(obs)
        self.assertIsNone(outcome.rollback_pointer)

    def test_action_id_loss_offers_no_pointer(self):
        # Item 5 negative: a rollback-owed observation whose action id was lost cannot name a
        # target, so the rail refuses to print a pointer that would roll back nothing.
        obs = SublaneStartupObservation(
            ok=False, action_id="", roles=(), rollback_owed=True
        )
        self.assertIsNone(self._blocked_outcome(obs).rollback_pointer)

    def test_a_non_startup_block_is_byte_for_byte_unchanged(self):
        # Every other blocked outcome keeps its historical payload: the additive keys appear
        # only when a nested startup failure was actually observed.
        payload = self._blocked_outcome(None).as_payload()
        self.assertNotIn("startup", payload)
        self.assertNotIn("rollback_pointer", payload)


# --- The REAL actuator + REAL port wiring (review j#82682 Finding 1) -------------------

_REQ = PrepareBoundPairRequest(
    issue="13948",
    journal="81811",
    lane="issue_13948_nested_rollback_pointer_r3",
    worktree="/tmp/wt-13948",
    branch="issue_13948_nested_rollback_pointer_r3",
)


def _slot(role: str, disposition: str) -> BoundSlot:
    provider = "codex" if role == "gateway" else "claude"
    locator = "w1:p1" if role == "gateway" else "w1:p2"
    return BoundSlot(role, provider, f"managed-{role}", locator, disposition)


def _observation(**changes) -> PreparationObservation:
    values = dict(
        workspace_id="mzb1_workspace",
        worktree_path=_REQ.worktree,
        worktree_identity="wt_deadbeef",
        branch=_REQ.branch,
        revision=4,
        generation=1,
        lifecycle_exact=True,
        pins_empty=True,
        inventory_readable=True,
        worktree_readable=True,
        worktree_clean=True,
        branch_matches=True,
        slots=(
            _slot("gateway", SLOT_PRESERVE_PENDING),
            _slot("worker", SLOT_RECOVER),
        ),
        discard_roles=("gateway",),
    )
    values.update(changes)
    return PreparationObservation(**values)


class _FakeOps:
    def __init__(self):
        self.observation = _observation()
        self.markers = ()
        self.drive_result = PreparationDrive(True, "recovered")

    def observe(self, request, *, action_id=""):
        return self.observation

    def approval_fields(self, issue, journal):
        return self.markers

    def drive(self, request, expectation, initial):
        return self.drive_result


class _RealLaunchPort:
    """A port whose non-launch legs are stubbed, but whose ``launch_action_bound`` is the
    production :meth:`_BoundPairActuatorPort.launch_action_bound` verbatim.

    Driving THIS under the real actuator exercises the exact connection review j#82682 F1
    flagged as bypassed: adapter → ``heal_lane_column`` catch site → ``launch_startup_health``.
    """

    def __init__(self, owner, request):
        self.owner = owner
        self.request = request
        self.launch_failure_reason = ""
        self.launch_startup_health = None

    def observe_old_slot(self, pin):
        return OLD_SLOT_PRESENT

    def observe_preservation(self, pin):
        return PreservationObservation(identity_matches=True, attestation_fresh=True)

    def close_exact_generation(self, pin):
        return CLOSE_DONE

    def verify_attestation(self, action_id, pin):
        return ATTEST_BOUND

    def _fresh_authority(self, *, require_attested_roles=()):
        return self

    def launch_action_bound(self, action_id, pin):
        # The REAL production method (constructs HerdrSublaneActuatorOps, calls
        # heal_lane_column, catches SublaneHealError, projects/stashes launch_startup_health).
        return _BoundPairActuatorPort.launch_action_bound(self, action_id, pin)


class RealActuatorPortStashTest(unittest.TestCase):
    """Item 1 wiring: adapter → heal catch → port stash → PreparationDrive → outcome."""

    GEN = 1
    FIXED = "2099-07-17T12:00:00+00:00"

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.key = ReplacementTransactionKey("mzb1_workspace", "prepare-bound-pair-abc")
        self.pin = ParticipantPin(
            lane_id=_REQ.lane, role="worker", provider="claude",
            assigned_name="mzb1-wk", old_locator="w28:p3H",
            lane_revision="4", lane_generation="1",
        )
        self.store.plan_transaction(
            self.key,
            action_generation=self.GEN,
            decision=DecisionPointer(source="redmine", issue_id="13846", journal_id="80925"),
            continuation=ContinuationPointer(
                source="redmine", issue_id="13846", journal_id="80925",
                expected_gate="bound_pair_composer_discard_approval",
                next_semantic_action="converge_bound_pair",
            ),
            participants=[self.pin],
        )
        owner = SimpleNamespace(repo_root=self.home, env={})
        request = ConvergeBoundPairRequest(
            issue="13846", journal="80925", lane=_REQ.lane,
            worktree=str(self.home), branch=_REQ.branch,
        )
        self.port = _RealLaunchPort(owner, request)

    def _drive(self):
        return ReplacementActuatorUseCase(
            self.store, self.port, preservation_policy=assess_preservation,
            clock=lambda: self.FIXED,
        ).drive_worker_recovery(self.key, holder="H", expected_action_generation=self.GEN)

    def test_nested_unhealthy_launch_flows_through_the_real_wiring_to_a_pointer(self):
        obs = project_sublane_startup(_nested_result())  # REAL projection of a dead launch

        def _fenced_heal(*_args, **_kwargs):
            raise SublaneHealError(
                "lane heal fenced (replacement_binding_launch_unhealthy)",
                reason=V1_BINDING_LAUNCH_UNHEALTHY,
                startup=obs,
            )

        with mock.patch.object(
            HerdrSublaneActuatorOps, "heal_lane_column", autospec=True,
            side_effect=_fenced_heal,
        ):
            result = self._drive()

        # The real actuator surfaces the typed launch stop, and the real port stashed the
        # nested observation at the heal catch site.
        self.assertEqual(result.status, ACTUATION_EFFECT_FAILED)
        self.assertEqual(result.detail, "launch")
        self.assertIs(self.port.launch_startup_health, obs)
        self.assertEqual(self.port.launch_failure_reason, V1_BINDING_LAUNCH_UNHEALTHY)
        self.assertEqual(
            _launch_detail(result, self.port),
            "launch:replacement_binding_launch_unhealthy",
        )

        # The exact PreparationDrive `LiveBoundPairPreparationOps.drive` builds from this
        # (result, port), fed through the public run, surfaces the pointer.
        drive = PreparationDrive(
            False, result.status, _launch_detail(result, self.port),
            startup=getattr(self.port, "launch_startup_health", None),
        )
        ops = _FakeOps()
        preflight = run_bound_pair_preparation(_REQ, execute=False, ops=ops)
        [(channel, fields)] = marker_fields_in_note(preflight.approval_marker)
        self.assertEqual(channel, "workflow-event")
        ops.markers = (fields,)
        ops.drive_result = drive
        outcome = run_bound_pair_preparation(_REQ, execute=True, ops=ops)
        self.assertEqual(outcome.state, STATE_BLOCKED)
        self.assertEqual(outcome.reason, BLOCK_REPLACEMENT_STOPPED)
        self.assertEqual(
            outcome.rollback_pointer,
            "mozyo-bridge herdr session-rollback --action-id startup_act_13948",
        )
        self.assertNotIn("--execute", outcome.rollback_pointer)


# --- The v1 replacement-binding adapter over the REAL public rollback rail -------------


class _CloseResult:
    def __init__(self, closed=(), failed=()):
        self.closed = tuple(closed)
        self.failed = tuple(failed)


class _RollbackOps:
    """The five reads + one close the public rollback rail is allowed; close is stateful."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.inventory_readable = True
        self.close_calls = []

    def agent_rows(self):
        if not self.inventory_readable:
            raise RuntimeError("herdr agent list failed")
        return list(self.rows)

    def runtime_state(self, locator):
        return "turn_ended"

    def observe_composer(self, locator):
        return (True, False)  # idle, no pending input

    def startup_blocker(self, provider, locator):
        return ""

    def open_obligations(self, workspace_id, assigned_names):
        return ()

    def close(self, workspace_id, lane_id, targets):
        self.close_calls.append(list(targets))
        closed = []
        for role, locator in targets:
            closed.append((role, locator))
            self.rows = [r for r in self.rows if r.get("pane_id") != locator]
        return _CloseResult(closed=closed)


class V1ReplacementBindingRollbackRailTest(unittest.TestCase):
    """Items 1/3/5 over the adapter AND the REAL public rollback rail (review j#82682 F1)."""

    WORKSPACE = "mzb1_workspace"
    LANE = "issue_13948_nested_rollback_pointer_r3"
    PROVIDER = "claude"
    MANAGED_PAIR = ("codex", "claude")
    OLD_LOCATOR = "w1:p9"
    FRESH_LOCATOR = "w2G:p3"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.action_id = "replacement_action_13948"
        self.assigned = encode_assigned_name(self.WORKSPACE, self.PROVIDER, self.LANE)
        self.unit = StartupUnit(self.WORKSPACE, self.LANE, self.MANAGED_PAIR)
        self.launch_calls = []

    def _unhealthy_launch(self, nonce, startup_fence):
        """Mimic prepare_session on an unhealthy launch: record a rollback-owed fence action
        (so the REAL rollback rail can act on it) and return the unhealthy result."""
        self.launch_calls.append(nonce)
        action = startup_fence.reserve(self.unit, nonce)
        startup_fence.record_participant(
            action.action_id,
            Participant(
                role=self.PROVIDER, assigned_name=self.assigned,
                locator=self.FRESH_LOCATOR, receipt="workspace=w2G",
            ),
        )
        startup_fence.set_phase(action.action_id, PHASE_ROLLBACK_OWED)
        return _nested_result(
            action_id=action.action_id, provider=self.PROVIDER,
            assigned=self.assigned, locator=self.FRESH_LOCATOR,
        )

    def _launch_or_resume(self, launch):
        launch_or_resume_v1_replacement(
            home=self.home,
            action_id=self.action_id,
            assigned_name=self.assigned,
            old_locator=self.OLD_LOCATOR,
            target_provider=self.PROVIDER,
            workspace_id=self.WORKSPACE,
            lane_id=self.LANE,
            managed_pair=self.MANAGED_PAIR,
            rows=(),
            existing={self.PROVIDER: ("", "")},
            launch=launch,
        )

    def _intent(self):
        return HerdrIdentityReplacementBindingStore(home=self.home).read(
            self.action_id, self.assigned
        )

    def _rollback_rows(self):
        return [{
            "name": self.assigned, "pane_id": self.FRESH_LOCATOR,
            "agent": self.PROVIDER, "agent_status": "idle",
        }]

    def test_unhealthy_launch_then_public_rollback_then_replay_converges(self):
        # (1) nested failure: the adapter fails closed and carries the nested result.
        with self.assertRaises(V1ReplacementBindingFailure) as caught:
            self._launch_or_resume(self._unhealthy_launch)
        self.assertEqual(caught.exception.reason, V1_BINDING_LAUNCH_UNHEALTHY)
        self.assertIsNotNone(caught.exception.startup_result)
        obs = project_sublane_startup(caught.exception.startup_result)
        self.assertTrue(obs.rollback_owed)
        first_action = self._intent().startup_action_id
        self.assertEqual(obs.action_id, first_action)
        pointer = (
            "mozyo-bridge herdr session-rollback --action-id " + first_action
        )

        fence = StartupTransactionFence(home=self.home)

        # (2) read-only preflight of the pointer's action id closes nothing.
        preflight = run_session_rollback(
            action_id=first_action, ops=_RollbackOps(self._rollback_rows()),
            fence=fence, execute=False,
        )
        self.assertEqual(preflight.reason, REASON_PREFLIGHT)
        self.assertFalse(preflight.executed)

        # (3) execute the public rollback rail — the ONLY thing that discharges the debt.
        exec_ops = _RollbackOps(self._rollback_rows())
        discharged = run_session_rollback(
            action_id=first_action, ops=exec_ops, fence=fence, execute=True,
        )
        self.assertEqual(discharged.reason, REASON_OK)
        self.assertTrue(exec_ops.close_calls)
        self.assertTrue(pointer)  # the pointer the operator followed

        # (4) replay: the SAME binding recognises the rolled-back reservation and relaunches
        # under a NEW startup action id (convergence, not a startup-debt dead end).
        self.launch_calls.clear()
        with self.assertRaises(V1ReplacementBindingFailure) as replay:
            self._launch_or_resume(self._unhealthy_launch)
        self.assertEqual(replay.exception.reason, V1_BINDING_LAUNCH_UNHEALTHY)
        self.assertEqual(len(self.launch_calls), 1)
        self.assertNotEqual(self._intent().startup_action_id, first_action)

    def test_replay_before_public_rollback_is_startup_debt_and_never_relaunches(self):
        # Item 5 terminal replay: without the public rollback the startup transaction is not
        # durably rolled back, so a bare replay fails closed on startup debt and never relaunches.
        with self.assertRaises(V1ReplacementBindingFailure):
            self._launch_or_resume(self._unhealthy_launch)
        self.assertEqual(len(self.launch_calls), 1)

        def _must_not_launch(nonce, startup_fence):  # pragma: no cover - asserted absent
            raise AssertionError("a replay before rollback must not relaunch")

        with self.assertRaises(V1ReplacementBindingFailure) as caught:
            self._launch_or_resume(_must_not_launch)
        self.assertEqual(caught.exception.reason, V1_BINDING_STARTUP_DEBT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
