"""Redmine #13806 tranche C — pre-attach reconcile + self-close executor + drain.

The final leg of the atomic self-replacement (Implementation Request j#79209, design
j#78384 / Verdict j#78406): the bare-``mozyo`` pre-attach reconciliation seam, the
process-external self-close executor (which replaces the current coordinator by reusing the
tranche B actuator), and the fresh-coordinator claim + continuation drain. All live effects
are behind injected ports (live process mutation is non-scope, j#79209) — tests drive
synthetic fakes and an isolated home (never the shared ``$HOME/.mozyo_bridge``).

Pins the j#79209 matrix: valid adopt/launch compatibility (the launch seam is inert when
no reconciler is injected or the session is ready), approval absent/stale/ambiguous/
unreadable → typed blocked, turn-working / pending-composer → seal blocked, Redmine
unreadable → blocked, fresh attestation mismatch → zero send, drain uncertain / restart →
no blind resend, duplicate invoke / lease loss, and self-close-then-crash replay.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.replacement_preservation import (  # noqa: E402
    PreservationObservation,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    PARTICIPANT_CLOSE_OWED,
    PARTICIPANT_REPLACED,
    PHASE_COMPLETED,
    PHASE_DRAINING_CONTINUATION,
    PHASE_SELF_CLOSE_ARMED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E402
    DRAIN_ATTESTATION_FAILED,
    DRAIN_COMPLETED,
    DRAIN_GENERATION_MISMATCH,
    DRAIN_NOT_READY,
    DRAIN_SEND_ERROR,
    DRAIN_SEND_FAILED,
    DRAIN_SEND_OK,
    DRAIN_UNCERTAIN_STATUS,
    ContinuationDrainPort,
    FreshCoordinatorDrainUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.pre_attach_reconcile import (  # noqa: E402
    PreAttachReconcileUseCase,
    ReconcileResolution,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_actuator import (  # noqa: E402
    ReplacementActuatorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.self_close_executor import (  # noqa: E402
    SELF_CLOSE_BLOCKED,
    SELF_CLOSE_GENERATION_MISMATCH,
    SELF_CLOSE_INVALID_TOPOLOGY,
    SELF_CLOSE_REPLACED,
    SelfCloseExecutorUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402
    ATTEST_BOUND,
    CLOSE_DONE,
    LAUNCH_DONE,
    OLD_SLOT_PRESENT,
    OLD_SLOT_RECYCLED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.session_replacement_reconcile import (  # noqa: E402
    BLOCKED_APPROVAL_ABSENT,
    BLOCKED_APPROVAL_AMBIGUOUS,
    BLOCKED_APPROVAL_STALE,
    BLOCKED_STORE_UNREADABLE,
    DRAIN_ATTEMPTED,
    DRAIN_CONFIRMED,
    DRAIN_NOT_ATTEMPTED,
    DRAIN_UNCERTAIN,
    RECONCILE_BLOCKED,
    RECONCILE_ONCE,
    RECONCILE_PASS_THROUGH,
    SELF_BLOCK_CONTINUATION_UNSEALED,
    SELF_BLOCK_PENDING_COMPOSER,
    SELF_BLOCK_PRESERVATION,
    SELF_BLOCK_TURN_ACTIVE,
    SELF_CLOSE_MAY_PROCEED,
    SelfCloseObservation,
    TXN_ABSENT,
    TXN_AMBIGUOUS,
    TXN_RESOLVED_EXACT,
    TXN_STALE,
    TXN_UNREADABLE,
    decide_pre_attach,
    decide_self_close,
    drain_state_for,
    may_attempt_drain,
)

GEN = 7
FIXED = "2026-07-15T12:00:00+00:00"


class FakeActuatorPort:
    def __init__(self):
        self.old = {}
        self.closed = []
        self.launched = []

    def observe_old_slot(self, pin):
        return self.old.get(pin.identity, OLD_SLOT_PRESENT)

    def observe_preservation(self, pin):
        return PreservationObservation(identity_matches=True, attestation_fresh=True)

    def close_exact_generation(self, pin):
        self.closed.append(pin.identity)
        return CLOSE_DONE

    def launch_action_bound(self, action_id, pin):
        self.launched.append((action_id, pin.identity))
        return LAUNCH_DONE

    def verify_attestation(self, action_id, pin):
        return ATTEST_BOUND


class FakeSealPort:
    def __init__(self, observation):
        self.observation = observation

    def observe_self_close_seals(self, record, self_pin):
        return self.observation


class FlippingSealPort:
    """Returns ``first`` on the initial observation, then ``rest`` on every later one.

    Models a seal that regresses between the executor's fast-fail check and the close
    boundary (R1-F1).
    """

    def __init__(self, first, rest):
        self.first = first
        self.rest = rest
        self.calls = 0

    def observe_self_close_seals(self, record, self_pin):
        self.calls += 1
        return self.first if self.calls == 1 else self.rest


class FakeDrainPort:
    def __init__(self, *, attest=True, send=DRAIN_SEND_OK, confirm_after_send=True):
        self.attest = attest
        self.send_result = send
        self.confirm_after_send = confirm_after_send
        self.confirmed = False
        self.sent = []

    def verify_fresh_attestation(self, action_id, holder):
        return self.attest

    def drain_send(self, continuation):
        self.sent.append(continuation.next_semantic_action)
        if self.send_result == DRAIN_SEND_OK and self.confirm_after_send:
            self.confirmed = True
        return self.send_result

    def drain_gate_confirmed(self, continuation):
        return self.confirmed


def _all_seals(**overrides):
    base = dict(
        at_self_close_armed=True, generation_matches=True,
        old_coordinator_matches=True, turn_ended=True, idle=True,
        no_pending_composer=True, preservation_clear=True, continuation_sealed=True,
    )
    base.update(overrides)
    return SelfCloseObservation(**base)


class _TrancheCCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.key = ReplacementTransactionKey("ws", "a:gen7")
        self.gw = ParticipantPin(
            lane_id="issue_x", role="gateway", provider="codex",
            assigned_name="gw", old_locator="w:1",
        )
        self.sc = ParticipantPin(
            lane_id="default", role="coordinator", provider="codex",
            assigned_name="cx", old_locator="w:3", is_self=True,
        )
        self.store.plan_transaction(
            self.key, action_generation=GEN,
            decision=DecisionPointer(
                source="redmine", issue_id="13806", journal_id="78948"
            ),
            continuation=ContinuationPointer(
                source="redmine", issue_id="13806", journal_id="78948",
                expected_gate="review_request", next_semantic_action="dispatch_once",
            ),
            participants=[self.gw, self.sc],
        )
        self.act_port = FakeActuatorPort()
        self.actuator = ReplacementActuatorUseCase(
            self.store, self.act_port, clock=lambda: FIXED
        )

    def _arm(self):
        # Drive the non-self participant and arm at self_close_armed (tranche B).
        result = self.actuator.run(
            self.key, holder="EXEC", expected_action_generation=GEN
        )
        self.assertEqual(self.store.get(self.key).phase, PHASE_SELF_CLOSE_ARMED)
        return result

    def _executor(self, seals):
        # The executor takes the raw actuation PORT (it builds its own self-seal-aware
        # actuator internally, R1-F1). It shares `act_port` with the arming actuator so the
        # `closed` / `launched` trace stays consistent.
        return SelfCloseExecutorUseCase(
            self.store, self.act_port, FakeSealPort(seals), clock=lambda: FIXED
        )

    def _phase_of(self, pin):
        return self.store.get(self.key).find_participant(pin.identity).phase


class SelfCloseExecutorTests(_TrancheCCase):
    def test_all_seals_pass_replaces_self(self):
        self._arm()
        result = self._executor(_all_seals()).run(
            self.key, holder="EXEC", expected_action_generation=GEN
        )
        self.assertEqual(result.status, SELF_CLOSE_REPLACED)
        self.assertEqual(self._phase_of(self.sc), PARTICIPANT_REPLACED)
        self.assertIn(self.sc.identity, self.act_port.closed)
        # the lease is released so a fresh coordinator can claim
        self.assertEqual(self.store.get(self.key).lease_holder, "")

    def test_turn_active_seal_blocks_zero_close(self):
        self._arm()
        result = self._executor(_all_seals(turn_ended=False)).run(
            self.key, holder="EXEC", expected_action_generation=GEN
        )
        self.assertEqual(result.status, SELF_CLOSE_BLOCKED)
        self.assertEqual(result.blocked_reason, SELF_BLOCK_TURN_ACTIVE)
        self.assertNotIn(self.sc.identity, self.act_port.closed)  # zero close
        self.assertEqual(self._phase_of(self.sc), PARTICIPANT_CLOSE_OWED)

    def test_pending_composer_seal_blocks(self):
        self._arm()
        result = self._executor(_all_seals(no_pending_composer=False)).run(
            self.key, holder="EXEC", expected_action_generation=GEN
        )
        self.assertEqual(result.status, SELF_CLOSE_BLOCKED)
        self.assertEqual(result.blocked_reason, SELF_BLOCK_PENDING_COMPOSER)
        self.assertNotIn(self.sc.identity, self.act_port.closed)

    def test_preservation_seal_blocks(self):
        self._arm()
        result = self._executor(_all_seals(preservation_clear=False)).run(
            self.key, holder="EXEC", expected_action_generation=GEN
        )
        self.assertEqual(result.blocked_reason, SELF_BLOCK_PRESERVATION)
        self.assertNotIn(self.sc.identity, self.act_port.closed)

    def test_continuation_unsealed_blocks(self):
        self._arm()
        result = self._executor(_all_seals(continuation_sealed=False)).run(
            self.key, holder="EXEC", expected_action_generation=GEN
        )
        self.assertEqual(result.blocked_reason, SELF_BLOCK_CONTINUATION_UNSEALED)

    def test_generation_mismatch_zero_effect(self):
        self._arm()
        result = self._executor(_all_seals()).run(
            self.key, holder="EXEC", expected_action_generation=GEN + 1
        )
        self.assertEqual(result.status, SELF_CLOSE_GENERATION_MISMATCH)
        self.assertNotIn(self.sc.identity, self.act_port.closed)

    def test_recycled_old_coordinator_is_zero_close(self):
        # A same-name recycled old coordinator slot is never closed (tranche B fence, reused).
        self._arm()
        self.act_port.old[self.sc.identity] = OLD_SLOT_RECYCLED
        result = self._executor(_all_seals()).run(
            self.key, holder="EXEC", expected_action_generation=GEN
        )
        self.assertNotEqual(result.status, SELF_CLOSE_REPLACED)
        self.assertNotIn(self.sc.identity, self.act_port.closed)

    def test_self_seal_regression_at_close_boundary_blocks_zero_close(self):
        # R1-F1: a self seal (pending composer, turn resuming) regressing AFTER the executor's
        # initial check but BEFORE the destructive close must block the close, not slip through.
        self._arm()
        flipping = FlippingSealPort(
            first=_all_seals(),
            rest=_all_seals(no_pending_composer=False, turn_ended=False),
        )
        executor = SelfCloseExecutorUseCase(
            self.store, self.act_port, flipping, clock=lambda: FIXED
        )
        result = executor.run(self.key, holder="EXEC", expected_action_generation=GEN)
        self.assertEqual(result.status, SELF_CLOSE_BLOCKED)
        self.assertNotIn(self.sc.identity, self.act_port.closed)  # zero close
        self.assertEqual(self._phase_of(self.sc), PARTICIPANT_CLOSE_OWED)  # owed unchanged
        # the seal was re-observed at the close boundary (more than the one fast-fail check)
        self.assertGreater(flipping.calls, 1)

    def test_self_close_then_crash_replays(self):
        # First run replaces the self; a re-run is idempotent (self already replaced) and
        # does not re-close.
        self._arm()
        self._executor(_all_seals()).run(
            self.key, holder="EXEC", expected_action_generation=GEN
        )
        closed_once = list(self.act_port.closed)
        again = self._executor(_all_seals()).run(
            self.key, holder="EXEC", expected_action_generation=GEN
        )
        self.assertEqual(again.status, SELF_CLOSE_REPLACED)
        self.assertEqual(self.act_port.closed, closed_once)  # no second close


class ContinuationDrainTests(_TrancheCCase):
    def _replace_self(self):
        self._arm()
        self._executor(_all_seals()).run(
            self.key, holder="EXEC", expected_action_generation=GEN
        )

    def test_fresh_coordinator_claims_and_drains_to_completed(self):
        self._replace_self()
        port = FakeDrainPort()
        result = FreshCoordinatorDrainUseCase(
            self.store, port, clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN)
        self.assertEqual(result.status, DRAIN_COMPLETED)
        self.assertEqual(result.drain_state, DRAIN_CONFIRMED)
        self.assertEqual(self.store.get(self.key).phase, PHASE_COMPLETED)
        self.assertEqual(port.sent, ["dispatch_once"])

    def test_non_attested_fresh_cannot_claim_or_send(self):
        self._replace_self()
        port = FakeDrainPort(attest=False)
        result = FreshCoordinatorDrainUseCase(
            self.store, port, clock=lambda: FIXED
        ).run(self.key, holder="IMPOSTER", expected_action_generation=GEN)
        self.assertEqual(result.status, DRAIN_ATTESTATION_FAILED)
        self.assertEqual(port.sent, [])
        self.assertEqual(self.store.get(self.key).lease_holder, "")  # never claimed

    def test_send_fails_stays_attempted_no_completion(self):
        self._replace_self()
        port = FakeDrainPort(send=DRAIN_SEND_ERROR)
        result = FreshCoordinatorDrainUseCase(
            self.store, port, clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN)
        self.assertEqual(result.status, DRAIN_SEND_FAILED)
        # phase advanced to draining_continuation (attempted recorded before the send)
        self.assertEqual(self.store.get(self.key).phase, PHASE_DRAINING_CONTINUATION)

    def test_sent_but_unconfirmed_is_uncertain_no_completion(self):
        self._replace_self()
        port = FakeDrainPort(confirm_after_send=False)  # send ok but gate never confirms
        result = FreshCoordinatorDrainUseCase(
            self.store, port, clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN)
        self.assertEqual(result.status, DRAIN_UNCERTAIN_STATUS)
        self.assertEqual(result.drain_state, DRAIN_UNCERTAIN)
        self.assertEqual(self.store.get(self.key).phase, PHASE_DRAINING_CONTINUATION)
        self.assertEqual(port.sent, ["dispatch_once"])

    def test_resume_after_attempt_does_not_blind_resend(self):
        # First run sends but the gate never confirms -> uncertain, phase draining_continuation.
        self._replace_self()
        port = FakeDrainPort(confirm_after_send=False)
        FreshCoordinatorDrainUseCase(self.store, port, clock=lambda: FIXED).run(
            self.key, holder="FRESH", expected_action_generation=GEN
        )
        self.assertEqual(port.sent, ["dispatch_once"])
        # A re-run must NOT blind-resend; it re-checks the gate (still unconfirmed) -> uncertain.
        resume = FreshCoordinatorDrainUseCase(
            self.store, port, clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN)
        self.assertEqual(resume.status, DRAIN_UNCERTAIN_STATUS)
        self.assertEqual(port.sent, ["dispatch_once"])  # NOT resent

    def test_resume_after_attempt_completes_once_gate_confirms(self):
        self._replace_self()
        port = FakeDrainPort(confirm_after_send=False)
        FreshCoordinatorDrainUseCase(self.store, port, clock=lambda: FIXED).run(
            self.key, holder="FRESH", expected_action_generation=GEN
        )
        # The durable gate now confirms the earlier attempt landed.
        port.confirmed = True
        resume = FreshCoordinatorDrainUseCase(
            self.store, port, clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN)
        self.assertEqual(resume.status, DRAIN_COMPLETED)
        self.assertEqual(self.store.get(self.key).phase, PHASE_COMPLETED)
        self.assertEqual(port.sent, ["dispatch_once"])  # still only one send total

    def test_claim_refused_on_not_ready_transaction_zero_write(self):
        # R1-F2: a fresh coordinator must NOT claim a not-ready transaction (still `planned`,
        # or self_close_armed with an un-replaced self) — a premature claim would block the
        # legitimate executor for the TTL.
        # (a) planned (nothing armed yet)
        before = self.store.get(self.key)
        port = FakeDrainPort()
        result = FreshCoordinatorDrainUseCase(
            self.store, port, clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN)
        after = self.store.get(self.key)
        self.assertEqual(result.status, DRAIN_NOT_READY)
        self.assertEqual(after.revision, before.revision)  # zero write
        self.assertEqual(after.lease_holder, "")  # never claimed
        self.assertEqual(port.sent, [])
        # (b) self_close_armed but the self is NOT yet replaced. (The arm leaves the lease
        # held by the executor; the point is the drain does not PREMATURELY re-claim it.)
        self._arm()
        before = self.store.get(self.key)
        self.assertEqual(before.phase, PHASE_SELF_CLOSE_ARMED)
        port2 = FakeDrainPort()
        result2 = FreshCoordinatorDrainUseCase(
            self.store, port2, clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN)
        after = self.store.get(self.key)
        self.assertEqual(result2.status, DRAIN_NOT_READY)
        self.assertEqual(after.revision, before.revision)  # zero write
        self.assertEqual(after.lease_holder, before.lease_holder)  # not re-claimed by FRESH
        self.assertNotEqual(after.lease_holder, "FRESH")
        self.assertEqual(port2.sent, [])

    def test_valid_resume_still_claims_and_drains(self):
        # R1-F2 must not break a valid resume: a transaction already at draining_continuation
        # (attempted) is claimable and drains to completion.
        self._replace_self()
        port = FakeDrainPort(confirm_after_send=False)  # first pass: uncertain
        FreshCoordinatorDrainUseCase(self.store, port, clock=lambda: FIXED).run(
            self.key, holder="FRESH", expected_action_generation=GEN
        )
        self.assertEqual(self.store.get(self.key).phase, PHASE_DRAINING_CONTINUATION)
        port.confirmed = True  # the gate now confirms
        resume = FreshCoordinatorDrainUseCase(
            self.store, port, clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN)
        self.assertEqual(resume.status, DRAIN_COMPLETED)

    def test_drain_generation_mismatch(self):
        self._replace_self()
        port = FakeDrainPort()
        result = FreshCoordinatorDrainUseCase(
            self.store, port, clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN + 1)
        self.assertEqual(result.status, DRAIN_GENERATION_MISMATCH)
        self.assertEqual(port.sent, [])

    def test_rerun_of_completed_is_idempotent(self):
        self._replace_self()
        FreshCoordinatorDrainUseCase(
            self.store, FakeDrainPort(), clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN)
        port = FakeDrainPort()
        again = FreshCoordinatorDrainUseCase(
            self.store, port, clock=lambda: FIXED
        ).run(self.key, holder="FRESH", expected_action_generation=GEN)
        self.assertEqual(again.status, DRAIN_COMPLETED)
        self.assertEqual(port.sent, [])  # nothing re-sent


class PreAttachSeamTests(_TrancheCCase):
    def _seam(self, drain_port=None):
        return PreAttachReconcileUseCase(
            self._executor(_all_seals()),
            FreshCoordinatorDrainUseCase(
                self.store, drain_port or FakeDrainPort(), clock=lambda: FIXED
            ),
        )

    def test_ready_session_passes_through_no_effect(self):
        # A ready session passes through without ever touching the actuator/executor.
        outcome = self._seam().reconcile(
            ReconcileResolution(token=TXN_RESOLVED_EXACT), session_ready=True
        )
        self.assertTrue(outcome.pass_through)
        self.assertEqual(self.act_port.closed, [])  # untouched

    def test_absent_approval_is_typed_blocked_no_effect(self):
        outcome = self._seam().reconcile(
            ReconcileResolution(token=TXN_ABSENT), session_ready=False
        )
        self.assertTrue(outcome.blocked)
        self.assertEqual(outcome.blocked_reason, BLOCKED_APPROVAL_ABSENT)
        self.assertEqual(self.act_port.closed, [])

    def test_ambiguous_and_stale_and_unreadable_block(self):
        for token, reason in (
            (TXN_STALE, BLOCKED_APPROVAL_STALE),
            (TXN_AMBIGUOUS, BLOCKED_APPROVAL_AMBIGUOUS),
            (TXN_UNREADABLE, BLOCKED_STORE_UNREADABLE),
        ):
            outcome = self._seam().reconcile(
                ReconcileResolution(token=token), session_ready=False
            )
            self.assertTrue(outcome.blocked)
            self.assertEqual(outcome.blocked_reason, reason)

    def test_reconcile_once_runs_executor_then_drain(self):
        self._arm()
        port = FakeDrainPort()
        outcome = self._seam(port).reconcile(
            ReconcileResolution(
                token=TXN_RESOLVED_EXACT, key=self.key, action_generation=GEN,
                executor_holder="EXEC", fresh_holder="FRESH",
            ),
            session_ready=False,
        )
        self.assertEqual(outcome.kind, RECONCILE_ONCE)
        self.assertEqual(outcome.self_close.status, SELF_CLOSE_REPLACED)
        self.assertEqual(outcome.drain.status, DRAIN_COMPLETED)
        self.assertEqual(self.store.get(self.key).phase, PHASE_COMPLETED)

    def test_reconcile_once_stops_at_blocked_self_close_no_drain(self):
        self._arm()
        # a blocked seal stops before the drain
        seam = PreAttachReconcileUseCase(
            self._executor(_all_seals(turn_ended=False)),
            FreshCoordinatorDrainUseCase(
                self.store, FakeDrainPort(), clock=lambda: FIXED
            ),
        )
        outcome = seam.reconcile(
            ReconcileResolution(
                token=TXN_RESOLVED_EXACT, key=self.key, action_generation=GEN,
                executor_holder="EXEC", fresh_holder="FRESH",
            ),
            session_ready=False,
        )
        self.assertEqual(outcome.kind, RECONCILE_ONCE)
        self.assertEqual(outcome.self_close.status, SELF_CLOSE_BLOCKED)
        self.assertIsNone(outcome.drain)  # drain never ran


class HerdrLaunchSeamWiringTests(unittest.TestCase):
    """The bare-`mozyo` launch seam is inert unless a reconciler is injected AND not ready."""

    def _run(self, *, ready, reconcile_seam):
        from mozyo_bridge.application.herdr_launch_command import (
            MozyoHerdrLaunchUseCase,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            SLOT_ADOPTED,
            SLOT_UNATTESTED,
            SessionStartResult,
            SlotResult,
        )

        outcome_flag = SLOT_ADOPTED if ready else SLOT_UNATTESTED
        result = SessionStartResult(
            workspace_id="ws", lane_id="",
            slots=[
                SlotResult(
                    provider="codex", assigned_name="cx",
                    outcome=outcome_flag, locator="w:3" if ready else "",
                ),
            ],
        )

        class Ops:
            def repo_root(self, args):
                return Path("/repo")

            def in_tmux(self):
                return False

            def resolve_binary(self):
                return "herdr"

            def prepare(self, repo_root):
                return result

            def attach(self, argv):  # pragma: no cover - not reached in tests
                raise AssertionError("attach")

            def emit(self, text, end="\n"):
                pass

            def die(self, message):  # pragma: no cover
                raise AssertionError(message)

        args = argparse.Namespace(
            cc=False, session=None, json_output=False, no_attach=True,
        )
        use_case = MozyoHerdrLaunchUseCase(Ops(), reconcile_seam=reconcile_seam)
        return use_case.run(args)

    def test_no_reconciler_preserves_behavior_even_when_not_ready(self):
        out = self._run(ready=False, reconcile_seam=None)
        self.assertIsNone(out.error_message)
        self.assertIsNotNone(out.pre_attach_text)  # proceeds to the attach summary

    def test_ready_session_skips_the_seam(self):
        seen = []
        out = self._run(ready=True, reconcile_seam=lambda r: seen.append(r) or "blocked!")
        self.assertIsNone(out.error_message)
        self.assertEqual(seen, [])  # seam not consulted on a ready session

    def test_not_ready_with_blocking_seam_fails_closed(self):
        out = self._run(ready=False, reconcile_seam=lambda r: "no approved replacement plan")
        self.assertEqual(out.error_message, "no approved replacement plan")
        self.assertIsNone(out.pre_attach_text)  # never reaches the attach summary

    def test_not_ready_with_passthrough_seam_proceeds(self):
        out = self._run(ready=False, reconcile_seam=lambda r: None)
        self.assertIsNone(out.error_message)
        self.assertIsNotNone(out.pre_attach_text)


class PureDecisionTests(unittest.TestCase):
    def test_pre_attach_decision(self):
        self.assertEqual(
            decide_pre_attach(session_ready=True, resolution=TXN_ABSENT).kind,
            RECONCILE_PASS_THROUGH,
        )
        self.assertEqual(
            decide_pre_attach(
                session_ready=False, resolution=TXN_RESOLVED_EXACT
            ).kind,
            RECONCILE_ONCE,
        )
        blocked = decide_pre_attach(session_ready=False, resolution=TXN_STALE)
        self.assertEqual(blocked.kind, RECONCILE_BLOCKED)
        self.assertEqual(blocked.blocked_reason, BLOCKED_APPROVAL_STALE)
        # an unknown resolution token fails closed as unreadable, never reconciled
        unknown = decide_pre_attach(session_ready=False, resolution="bogus")
        self.assertEqual(unknown.kind, RECONCILE_BLOCKED)
        self.assertEqual(unknown.blocked_reason, BLOCKED_STORE_UNREADABLE)

    def test_decide_self_close_ordered_and_fail_closed(self):
        self.assertEqual(decide_self_close(_all_seals()), SELF_CLOSE_MAY_PROCEED)
        # missing everything -> the first (most fundamental) failing seal
        self.assertEqual(
            decide_self_close(SelfCloseObservation()), "not_self_close_armed"
        )
        self.assertEqual(
            decide_self_close(_all_seals(idle=False)), "target_not_idle"
        )

    def test_drain_state_machine(self):
        from mozyo_bridge.core.state.replacement_transaction_model import (
            PHASE_FRESH_COORDINATOR_CLAIMED,
        )

        self.assertEqual(
            drain_state_for(PHASE_FRESH_COORDINATOR_CLAIMED, gate_confirmed=False),
            DRAIN_NOT_ATTEMPTED,
        )
        self.assertEqual(
            drain_state_for(PHASE_DRAINING_CONTINUATION, gate_confirmed=False),
            DRAIN_ATTEMPTED,
        )
        self.assertEqual(
            drain_state_for(PHASE_DRAINING_CONTINUATION, gate_confirmed=True),
            DRAIN_CONFIRMED,
        )
        self.assertEqual(
            drain_state_for(PHASE_COMPLETED, gate_confirmed=False), DRAIN_CONFIRMED
        )
        self.assertTrue(may_attempt_drain(DRAIN_NOT_ATTEMPTED))
        self.assertFalse(may_attempt_drain(DRAIN_ATTEMPTED))
        self.assertFalse(may_attempt_drain(DRAIN_UNCERTAIN))
        self.assertFalse(may_attempt_drain(DRAIN_CONFIRMED))

    def test_ports_are_runtime_checkable(self):
        self.assertIsInstance(FakeDrainPort(), ContinuationDrainPort)


if __name__ == "__main__":
    unittest.main()
