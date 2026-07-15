"""`sublane quarantine` use case + adapter tests (Redmine #13763 j#78011 / j#78052).

Drives :class:`SublaneQuarantineUseCase` over a fake IO port (fake inspection / close /
heal / verify — nothing live is touched) against a real :class:`LaneReplacementStore` on a
temp home, and covers the regression matrix the implementation request pins:

- known marker -> q-enter guidance, **zero close** (an input we can drive is never destroyed);
- uncorrelated + no positive approval -> **zero close**;
- exact approval -> close the one pinned old receiver + fresh attested replace;
- stale generation (newer composer revision, approval predating the live agent generation,
  wrong action id) -> refuse before any close;
- foreign / inactive lane owner, working agent, unattested identity, unreadable inventory
  -> fail closed;
- partial launch -> durable ``replacement_pending``, and a redrive of the SAME generation
  relaunches **without closing a second time** (contract 5);
- restart idempotency -> a completed generation is a no-op, never a second close.

The adapter tests pin the two boundaries the use case cannot see: the composer observer
never lets pane body cross into the domain (contract 8), and the close plan only ever
targets the exact pinned locator (never a foreign or recycled pane).
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ReleasePin,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (
    REPLACEMENT_NOT_REQUESTED,
    REPLACEMENT_PENDING,
    REPLACEMENT_REPLACED,
    REPLACEMENT_REQUESTED,
)
from mozyo_bridge.core.state.lane_replacement import LaneReplacementStore
from mozyo_bridge.core.state.lane_replacement_model import quarantine_action_id
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_quarantine as quarantine_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
    CloseReceiverResult,
    ComposerObservation,
    FreshReceiverVerification,
    LiveSublaneQuarantineOps,
    QuarantineInspection,
    QuarantineRequest,
    SublaneQuarantineUseCase,
    observe_composer_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    AGENT_WORKING,
    CORRELATED_KNOWN_MARKER,
    GENERATION_MISMATCH,
    IDENTITY_UNATTESTED,
    INVENTORY_UNREADABLE,
    UNCORRELATED,
    PendingComposerSignal,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS = "wProj"
ISSUE = "13763"
OTHER_ISSUE = "13683"
LANE = "issue_13763_pending_composer_quarantine"
ROLE = "claude"
GATEWAY_ROLE = "codex"
JOURNAL = "78011"
APPROVAL_JOURNAL = "78200"

NAME = encode_assigned_name(WS, ROLE, LANE)
GATEWAY_NAME = encode_assigned_name(WS, GATEWAY_ROLE, LANE)
OLD_LOCATOR = f"{WS}:p44"
FRESH_LOCATOR = f"{WS}:p61"

#: The approval was written after the live agent generation attested, and pinned that
#: generation's composer revision. Both are the stale-approval fences.
ATTESTED_AT = "2026-07-14T09:00:00+00:00"
APPROVED_AT = "2026-07-14T09:05:00+00:00"
NEWER_ATTESTED_AT = "2026-07-14T09:30:00+00:00"
AGENT_REVISION = 7

MARKER = "[mozyo:handoff:source=redmine:issue=13763:journal=78011:kind=implementation_request:to=claude]"

ACTION = quarantine_action_id(lane_id=LANE, role=ROLE, locator=OLD_LOCATOR)


def _signal(**kw) -> PendingComposerSignal:
    """An attested, idle receiver stuck on an uncorrelatable composer."""
    base = dict(
        inventory_readable=True,
        has_pending=True,
        agent_state="idle",
        identity_attested=True,
        generation_matches=True,
        correlated_marker_ids=(),
        correlation_ambiguous=False,
    )
    base.update(kw)
    return PendingComposerSignal(**base)


class _FakeOps:
    """Fake quarantine IO port: canned classification + recorded actuation."""

    def __init__(
        self,
        *,
        signal: PendingComposerSignal | None = None,
        row_revision: int = AGENT_REVISION,
        attested_at: str = ATTESTED_AT,
        receiver_present: bool | None = True,
        close: CloseReceiverResult | None = None,
        verify: FreshReceiverVerification | None = None,
        heal_error: Exception | None = None,
    ) -> None:
        self._signal = signal if signal is not None else _signal()
        self._row_revision = row_revision
        self._attested_at = attested_at
        self._receiver_present = receiver_present
        self._close = close if close is not None else CloseReceiverResult(True)
        self._verify = (
            verify
            if verify is not None
            else FreshReceiverVerification(True, locator=FRESH_LOCATOR)
        )
        self._heal_error = heal_error
        self.closed_pins: list[ReleasePin] = []
        self.heals = 0
        self.verifies = 0

    def inspect(self, request: QuarantineRequest) -> QuarantineInspection:
        return QuarantineInspection(
            workspace_id=WS,
            signal=self._signal,
            row_revision=self._row_revision,
            attested_at=self._attested_at,
            receiver_present=self._receiver_present,
        )

    def close_receiver(
        self, request: QuarantineRequest, pin: ReleasePin
    ) -> CloseReceiverResult:
        self.closed_pins.append(pin)
        return self._close

    def heal_receiver(self, request: QuarantineRequest) -> None:
        self.heals += 1
        if self._heal_error is not None:
            raise self._heal_error

    def verify_fresh_receiver(
        self, request: QuarantineRequest, *, fresh_after: str
    ) -> FreshReceiverVerification:
        self.verifies += 1
        return self._verify


def _request(**kw) -> QuarantineRequest:
    base = dict(
        issue=ISSUE,
        lane=LANE,
        journal=APPROVAL_JOURNAL,
        role=ROLE,
        assigned_name=NAME,
        locator=OLD_LOCATOR,
        action_generation=ACTION,
        approval_observed_at=APPROVED_AT,
        approved_revision=AGENT_REVISION,
    )
    base.update(kw)
    return QuarantineRequest(**base)


class _QuarantineCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.key = LaneLifecycleKey(WS, LANE)
        self.lifecycle = LaneLifecycleStore(home=self.home)
        self.store = LaneReplacementStore(home=self.home)

    def _active_lane(self, issue: str = ISSUE) -> None:
        self.lifecycle.declare_active(
            self.key,
            decision=DecisionPointer(
                source="redmine", issue_id=issue, journal_id=JOURNAL
            ),
            issue_id=issue,
        )

    def _row(self):
        return self.lifecycle.get(self.key)

    def _run(self, ops: _FakeOps, *, execute: bool = True, request=None):
        return SublaneQuarantineUseCase(ops=ops, store=self.store).run(
            request or _request(), execute=execute
        )


class PreflightTest(_QuarantineCase):
    def test_known_marker_is_guided_to_q_enter_and_closes_nothing(self) -> None:
        # Contract 2: a composer correlated to a delivery marker we own is drivable.
        # Even with --execute and a complete approval, quarantine must refuse it —
        # destroying a submittable input is exactly the loss the safety contract exists
        # to prevent.
        self._active_lane()
        ops = _FakeOps(signal=_signal(correlated_marker_ids=(MARKER,)))
        outcome = self._run(ops, execute=True)

        self.assertEqual(outcome.classification.label, CORRELATED_KNOWN_MARKER)
        self.assertTrue(outcome.classification.q_enter_recommended)
        self.assertEqual(ops.closed_pins, [])
        self.assertEqual(ops.heals, 0)
        self.assertEqual(outcome.replacement_state, REPLACEMENT_NOT_REQUESTED)
        row = self._row()
        self.assertEqual(row.replacement_state, REPLACEMENT_NOT_REQUESTED)
        self.assertEqual(row.revision, 1)

    def test_preflight_classifies_a_candidate_without_mutating_anything(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(ops, execute=False)

        self.assertEqual(outcome.classification.label, UNCORRELATED)
        self.assertTrue(outcome.classification.quarantine_candidate)
        self.assertFalse(outcome.executed)
        self.assertEqual(ops.closed_pins, [])
        self.assertEqual(ops.heals, 0)
        self.assertEqual(self._row().replacement_state, REPLACEMENT_NOT_REQUESTED)
        self.assertEqual(self._row().revision, 1)

    def test_preflight_on_a_known_marker_names_the_q_enter_rail(self) -> None:
        self._active_lane()
        outcome = self._run(
            _FakeOps(signal=_signal(correlated_marker_ids=(MARKER,))), execute=False
        )
        self.assertIn("q-enter", outcome.detail)
        self.assertFalse(outcome.is_blocked)


class ApprovalFenceTest(_QuarantineCase):
    """Nothing is closed without a positive, exact, current owner approval."""

    def test_wrong_action_generation_refuses_before_any_close(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(
            ops,
            request=_request(
                action_generation=quarantine_action_id(
                    lane_id=LANE, role=ROLE, locator=f"{WS}:pOTHER"
                )
            ),
        )
        self.assertEqual(ops.closed_pins, [])
        self.assertIn("action generation", outcome.detail)
        self.assertEqual(self._row().replacement_state, REPLACEMENT_NOT_REQUESTED)

    def test_incomplete_approval_journal_refuses(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(ops, request=_request(journal=""))
        self.assertEqual(ops.closed_pins, [])
        self.assertIn("approval journal", outcome.detail)

    def test_stale_approval_for_a_newer_composer_revision_refuses(self) -> None:
        # Contract 3: the live composer moved on after the owner looked at it. The
        # approval names a generation that no longer exists -> zero actuation.
        self._active_lane()
        ops = _FakeOps(row_revision=AGENT_REVISION + 1)
        outcome = self._run(ops)
        self.assertEqual(ops.closed_pins, [])
        self.assertIn("stale", outcome.detail)
        self.assertEqual(self._row().replacement_state, REPLACEMENT_NOT_REQUESTED)

    def test_approval_predating_the_live_agent_generation_refuses(self) -> None:
        # The receiver restarted (attested) AFTER the approval was written: the approval
        # applies to a dead generation, not the agent now holding the composer.
        self._active_lane()
        ops = _FakeOps(attested_at=NEWER_ATTESTED_AT)
        outcome = self._run(ops)
        self.assertEqual(ops.closed_pins, [])
        self.assertIn("predates", outcome.detail)

    def test_foreign_issue_owner_refuses(self) -> None:
        # The lane row is active but bound to a different issue than the approval:
        # this approval has no authority over this lane.
        self._active_lane(issue=OTHER_ISSUE)
        ops = _FakeOps()
        outcome = self._run(ops)
        self.assertEqual(ops.closed_pins, [])
        self.assertIn("foreign", outcome.detail)

    def test_absent_lane_row_refuses(self) -> None:
        ops = _FakeOps()
        outcome = self._run(ops)
        self.assertEqual(ops.closed_pins, [])
        self.assertIn("absent", outcome.detail)

    def test_hibernated_lane_refuses(self) -> None:
        # A lane that has left `active` is the release path's business, not a
        # receiver replacement's.
        self._active_lane()
        self.lifecycle.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target="hibernated",
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id=JOURNAL
            ),
        )
        ops = _FakeOps()
        outcome = self._run(ops)
        self.assertEqual(ops.closed_pins, [])
        self.assertIn("inactive", outcome.detail)


class BlockedClassificationTest(_QuarantineCase):
    """A non-candidate classification is never actuated, approval or not."""

    def _assert_zero_actuation(self, signal, label) -> None:
        self._active_lane()
        ops = _FakeOps(signal=signal)
        outcome = self._run(ops)
        self.assertEqual(outcome.classification.label, label)
        self.assertEqual(ops.closed_pins, [])
        self.assertEqual(ops.heals, 0)
        self.assertEqual(self._row().replacement_state, REPLACEMENT_NOT_REQUESTED)
        self.assertEqual(self._row().revision, 1)
        self.assertIn("zero actuation", outcome.detail)

    def test_working_agent_is_never_replaced(self) -> None:
        self._assert_zero_actuation(_signal(agent_state="busy"), AGENT_WORKING)

    def test_unattested_identity_is_never_replaced(self) -> None:
        self._assert_zero_actuation(
            _signal(identity_attested=False), IDENTITY_UNATTESTED
        )

    def test_generation_mismatch_is_never_replaced(self) -> None:
        self._assert_zero_actuation(
            _signal(generation_matches=False), GENERATION_MISMATCH
        )

    def test_unreadable_inventory_is_never_replaced(self) -> None:
        self._assert_zero_actuation(
            _signal(inventory_readable=False), INVENTORY_UNREADABLE
        )


class ReplacementTest(_QuarantineCase):
    def test_exact_approval_closes_the_pinned_receiver_and_replaces_it(self) -> None:
        self._active_lane()
        ops = _FakeOps()
        outcome = self._run(ops)

        self.assertFalse(outcome.is_blocked)
        self.assertEqual(outcome.replacement_state, REPLACEMENT_REPLACED)
        self.assertTrue(outcome.closed_old_receiver)
        self.assertEqual(outcome.fresh_locator, FRESH_LOCATOR)
        # Exactly one process was closed, and it is the approved one.
        self.assertEqual(len(ops.closed_pins), 1)
        pin = ops.closed_pins[0]
        self.assertEqual((pin.role, pin.assigned_name, pin.locator), (ROLE, NAME, OLD_LOCATOR))
        self.assertEqual(ops.heals, 1)

        row = self._row()
        self.assertEqual(row.replacement_state, REPLACEMENT_REPLACED)
        self.assertEqual(row.replacement_action_id, ACTION)
        # The lane keeps its issue and stays active: only the receiver was exchanged.
        self.assertEqual(row.lane_disposition, DISPOSITION_ACTIVE)
        self.assertEqual(row.issue_id, ISSUE)
        self.assertEqual(row.decision_journal, APPROVAL_JOURNAL)

    def test_close_failure_leaves_the_generation_requested_and_launches_nothing(
        self,
    ) -> None:
        # The old receiver could not be closed. Launching a fresh slot now would leave
        # two live receivers on one lane, so the generation stays `requested`.
        self._active_lane()
        ops = _FakeOps(close=CloseReceiverResult(False, detail="close_failed"))
        outcome = self._run(ops)

        self.assertTrue(outcome.is_blocked)
        self.assertEqual(outcome.replacement_state, REPLACEMENT_REQUESTED)
        self.assertEqual(ops.heals, 0)
        self.assertEqual(self._row().replacement_state, REPLACEMENT_REQUESTED)

    def test_partial_launch_is_durable_pending_and_redrive_never_closes_twice(
        self,
    ) -> None:
        # Contract 5: close succeeded, launch failed. The durable state must be
        # `pending` so a redrive resumes at the LAUNCH. Re-closing would be a close
        # against a locator that no longer belongs to this generation.
        self._active_lane()
        first_ops = _FakeOps(heal_error=RuntimeError("herdr launch failed"))
        first = self._run(first_ops)

        self.assertTrue(first.is_blocked)
        self.assertEqual(first.replacement_state, REPLACEMENT_PENDING)
        self.assertTrue(first.closed_old_receiver)
        self.assertEqual(len(first_ops.closed_pins), 1)
        self.assertEqual(self._row().replacement_state, REPLACEMENT_PENDING)

        # The redrive sees the old receiver GONE — which classifies as a generation
        # mismatch — yet must still finish the stored generation rather than refuse.
        redrive_ops = _FakeOps(signal=_signal(generation_matches=False))
        second = self._run(redrive_ops)

        self.assertFalse(second.is_blocked)
        self.assertEqual(second.replacement_state, REPLACEMENT_REPLACED)
        self.assertEqual(redrive_ops.closed_pins, [])  # no second close
        self.assertEqual(redrive_ops.heals, 1)
        self.assertEqual(self._row().replacement_state, REPLACEMENT_REPLACED)

    def test_unverifiable_fresh_receiver_stays_pending(self) -> None:
        # The launch ran but the fresh slot did not attest to this lane/role/locator:
        # never record `replaced` on an unproven receiver.
        self._active_lane()
        ops = _FakeOps(verify=FreshReceiverVerification(False, detail="not attested"))
        outcome = self._run(ops)

        self.assertTrue(outcome.is_blocked)
        self.assertEqual(outcome.replacement_state, REPLACEMENT_PENDING)
        self.assertEqual(outcome.fresh_locator, "")
        self.assertEqual(self._row().replacement_state, REPLACEMENT_PENDING)

    def test_completed_generation_is_idempotent_across_restart(self) -> None:
        self._active_lane()
        first = self._run(_FakeOps())
        self.assertEqual(first.replacement_state, REPLACEMENT_REPLACED)
        revision = self._row().revision

        # Restart / duplicate apply of the SAME approval: no close, no launch, no write.
        replay_ops = _FakeOps(signal=_signal(generation_matches=False))
        second = self._run(replay_ops)

        self.assertFalse(second.is_blocked)
        self.assertEqual(second.replacement_state, REPLACEMENT_REPLACED)
        self.assertIn("idempotent", second.detail)
        self.assertEqual(replay_ops.closed_pins, [])
        self.assertEqual(replay_ops.heals, 0)
        self.assertEqual(self._row().revision, revision)

    def test_a_different_generation_cannot_hijack_one_in_flight(self) -> None:
        # A replacement is mid-flight (pending). A second approval for a DIFFERENT
        # receiver must not act on this lane while an actuator may still be launching.
        self._active_lane()
        self._run(_FakeOps(heal_error=RuntimeError("launch failed")))
        self.assertEqual(self._row().replacement_state, REPLACEMENT_PENDING)

        other_locator = f"{WS}:p99"
        intruder = _FakeOps()
        outcome = self._run(
            intruder,
            request=_request(
                locator=other_locator,
                journal="78299",
                action_generation=quarantine_action_id(
                    lane_id=LANE, role=ROLE, locator=other_locator
                ),
            ),
        )
        self.assertTrue(outcome.is_blocked)
        self.assertIn("in flight", outcome.detail)
        self.assertEqual(intruder.closed_pins, [])
        self.assertEqual(intruder.heals, 0)
        self.assertEqual(self._row().replacement_state, REPLACEMENT_PENDING)

    def test_in_flight_replacement_fences_a_concurrent_hibernate(self) -> None:
        # The race the shared revision exists for: while a replacement is closing /
        # launching this lane's receiver, a hibernate must not move the disposition
        # under it.
        self._active_lane()
        self._run(_FakeOps(heal_error=RuntimeError("launch failed")))
        row = self._row()
        self.assertEqual(row.replacement_state, REPLACEMENT_PENDING)

        blocked = self.lifecycle.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=row.revision,
            target="hibernated",
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id=JOURNAL
            ),
        )
        self.assertFalse(blocked.applied)
        self.assertEqual(self._row().lane_disposition, DISPOSITION_ACTIVE)


class OwedCloseRevalidationTest(_QuarantineCase):
    """R1-F1 (j#78347): an owed close re-validates the receiver that is live NOW.

    A crash between the request CAS and the close leaves ``requested`` durable with the
    close still owed. By the time it is redriven, the pinned locator may hold a receiver
    that has taken new input or started working — an input the owner never approved
    discarding. The exact-pin match alone cannot see that, so the approval fences must
    run again at the close edge.
    """

    def _crashed_after_request(self) -> None:
        """Open the generation durably, then stop — exactly as a crash would."""
        self._active_lane()
        row = self._row()
        opened = self.store.request_replacement(
            self.key,
            expected_revision=row.revision,
            action_id=ACTION,
            pins=(ReleasePin(role=ROLE, assigned_name=NAME, locator=OLD_LOCATOR),),
            decision=DecisionPointer(
                source="redmine", issue_id=ISSUE, journal_id=APPROVAL_JOURNAL
            ),
        )
        self.assertTrue(opened.applied)
        self.assertEqual(self._row().replacement_state, REPLACEMENT_REQUESTED)

    def _assert_owed_close_withheld(self, ops: _FakeOps) -> None:
        outcome = self._run(ops)
        self.assertTrue(outcome.is_blocked)
        self.assertEqual(ops.closed_pins, [])  # the live receiver survives
        self.assertEqual(ops.heals, 0)
        self.assertEqual(outcome.replacement_state, REPLACEMENT_REQUESTED)
        self.assertIn("owed close withheld", outcome.detail)
        # The generation stays open and redrivable; nothing was destroyed or lost.
        self.assertEqual(self._row().replacement_state, REPLACEMENT_REQUESTED)

    def test_receiver_at_a_newer_composer_revision_is_not_closed(self) -> None:
        # The pinned receiver took new input while the replacement was owed.
        self._crashed_after_request()
        self._assert_owed_close_withheld(
            _FakeOps(row_revision=AGENT_REVISION + 1, receiver_present=True)
        )

    def test_receiver_that_started_working_is_not_closed(self) -> None:
        self._crashed_after_request()
        self._assert_owed_close_withheld(
            _FakeOps(signal=_signal(agent_state="busy"), receiver_present=True)
        )

    def test_receiver_that_is_no_longer_a_candidate_is_not_closed(self) -> None:
        # The composer we approved discarding is gone: the receiver now holds a
        # correlated, drivable marker (or nothing at all).
        self._crashed_after_request()
        self._assert_owed_close_withheld(
            _FakeOps(
                signal=_signal(correlated_marker_ids=(MARKER,)), receiver_present=True
            )
        )

    def test_receiver_reattested_after_the_approval_is_not_closed(self) -> None:
        # The slot restarted while the close was owed: a brand-new agent generation
        # now sits at the same locator.
        self._crashed_after_request()
        self._assert_owed_close_withheld(
            _FakeOps(attested_at=NEWER_ATTESTED_AT, receiver_present=True)
        )

    def test_unprovable_absence_withholds_the_owed_close(self) -> None:
        # The inventory could not tell us whether the pinned receiver is still live.
        # "Unknown" must never be read as "already gone" — that would license a close
        # (and then a relaunch) against a receiver nobody looked at.
        self._crashed_after_request()
        self._assert_owed_close_withheld(
            _FakeOps(
                signal=_signal(inventory_readable=False), receiver_present=None
            )
        )

    def test_unchanged_receiver_is_closed_exactly_once(self) -> None:
        # The approved composer / generation is still exactly what the owner saw:
        # the owed close proceeds, against the one pinned slot.
        self._crashed_after_request()
        ops = _FakeOps(receiver_present=True)
        outcome = self._run(ops)

        self.assertFalse(outcome.is_blocked)
        self.assertEqual(outcome.replacement_state, REPLACEMENT_REPLACED)
        self.assertEqual(len(ops.closed_pins), 1)
        pin = ops.closed_pins[0]
        self.assertEqual(
            (pin.role, pin.assigned_name, pin.locator), (ROLE, NAME, OLD_LOCATOR)
        )
        self.assertEqual(ops.heals, 1)

    def test_absent_receiver_resumes_the_generation_without_a_second_close(
        self,
    ) -> None:
        # The crash happened AFTER the close: the old receiver is positively gone, so
        # the redrive owes only the launch. It must not refuse just because the vanished
        # receiver no longer classifies as a candidate.
        self._crashed_after_request()
        ops = _FakeOps(
            signal=_signal(generation_matches=False),
            receiver_present=False,
            close=CloseReceiverResult(False, old_absent=True),
        )
        outcome = self._run(ops)

        self.assertFalse(outcome.is_blocked)
        self.assertEqual(outcome.replacement_state, REPLACEMENT_REPLACED)
        self.assertFalse(outcome.closed_old_receiver)  # nothing was killed twice
        self.assertEqual(ops.heals, 1)
        self.assertEqual(self._row().replacement_state, REPLACEMENT_REPLACED)


class ComposerObservationTest(unittest.TestCase):
    """The adapter boundary: pane text enters, only content-free facts leave."""

    def test_observation_exposes_no_body_hash_or_length(self) -> None:
        fields = {f.name for f in dataclasses.fields(ComposerObservation)}
        self.assertEqual(fields, {"readable", "has_pending", "marker_ids"})

    def test_pending_body_is_never_carried_out_of_the_observer(self) -> None:
        secret = "coordinatorへ返信します: private draft text"
        observation = observe_composer_text(f"some scrollback\n> {secret}")
        self.assertTrue(observation.readable)
        self.assertTrue(observation.has_pending)
        self.assertEqual(observation.marker_ids, ())
        # Nothing derived from the body (excerpt, hash, length) survives the boundary.
        self.assertNotIn(secret, repr(observation))

    def test_empty_composer_is_readable_and_not_pending(self) -> None:
        observation = observe_composer_text("output line\n> ")
        self.assertTrue(observation.readable)
        self.assertFalse(observation.has_pending)

    def test_no_prompt_rendered_is_unreadable_not_empty(self) -> None:
        # We could not find the composer at all: that is "unknown", never "nothing
        # pending" (the classifier fails closed on it).
        for content in ("", "just scrollback with no prompt", None, 17):
            with self.subTest(content=content):
                observation = observe_composer_text(content)
                self.assertFalse(observation.readable)
                self.assertIsNone(observation.has_pending)

    def test_known_marker_is_extracted_even_when_hard_wrapped(self) -> None:
        # The composer renders a delivery marker wrapped mid-token across pane lines;
        # correlation must still recognise the action identity (otherwise a drivable
        # input would misclassify as uncorrelated and become a close candidate).
        head, tail = MARKER[:40], MARKER[40:]
        observation = observe_composer_text(f"› {head}\n{tail} please continue")
        self.assertTrue(observation.has_pending)
        self.assertEqual(observation.marker_ids, (MARKER,))

    def test_two_markers_are_both_reported_for_ambiguity(self) -> None:
        other = MARKER.replace("13763", "13683")
        observation = observe_composer_text(f"> {MARKER} {other}")
        self.assertEqual(len(observation.marker_ids), 2)

    def test_only_the_current_composer_is_observed_not_scrollback(self) -> None:
        # A marker sitting in earlier scrollback (above the last prompt) is history,
        # not pending input, and must not be correlated as the composer's identity.
        observation = observe_composer_text(f"› {MARKER}\nagent output\n> ")
        self.assertTrue(observation.readable)
        self.assertFalse(observation.has_pending)
        self.assertEqual(observation.marker_ids, ())


def _agent_row(name: str, locator: str) -> dict:
    return {"name": name, "pane_id": locator}


class _StubOps(LiveSublaneQuarantineOps):
    """Live ops with only the inventory / provider probes stubbed."""

    def __init__(self, rows, **kw):
        super().__init__(repo_root=Path("/repo"), env={}, **kw)
        self._stub_rows = list(rows)

    def _rows(self):
        return list(self._stub_rows)

    def _providers(self):
        return (GATEWAY_ROLE, ROLE)


class InspectPresenceTest(unittest.TestCase):
    """`receiver_present` is what lets a redrive skip an owed close — so it must
    only ever say ``False`` when the pinned slot is provably gone (R1-F1 j#78347)."""

    def setUp(self) -> None:
        patcher = mock.patch.object(
            quarantine_module, "repo_scope_workspace_id", return_value=WS
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _inspect(self, rows):
        return _StubOps(rows).inspect(_request()).receiver_present

    def test_live_pinned_locator_is_present(self) -> None:
        self.assertTrue(self._inspect([_agent_row(NAME, OLD_LOCATOR)]))

    def test_ambiguous_inventory_is_present_not_absent(self) -> None:
        # The same assigned name is live at two locators, one of them the pinned one.
        # The generation is unusable, but something IS live there: reading this as
        # absence would let the owed close proceed unvalidated.
        self.assertTrue(
            self._inspect(
                [_agent_row(NAME, OLD_LOCATOR), _agent_row(NAME, FRESH_LOCATOR)]
            )
        )

    def test_recycled_locator_only_is_absent(self) -> None:
        # Nothing is live at the pinned locator: the pinned process is gone.
        self.assertFalse(self._inspect([_agent_row(NAME, FRESH_LOCATOR)]))

    def test_unreadable_inventory_is_unknown_not_absent(self) -> None:
        class _Unreadable(_StubOps):
            def _rows(self):
                raise RuntimeError("herdr inventory unreadable")

        self.assertIsNone(_Unreadable([]).inspect(_request()).receiver_present)


class CloseReceiverTest(unittest.TestCase):
    """The close plan may only ever target the exact pinned generation."""

    def setUp(self) -> None:
        patcher = mock.patch.object(
            quarantine_module, "repo_scope_workspace_id", return_value=WS
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.executed = mock.Mock(
            return_value=HerdrRetireCloseResult(
                workspace_id=WS, lane_id=LANE, closed=((ROLE, OLD_LOCATOR),)
            )
        )
        exec_patcher = mock.patch.object(
            quarantine_module, "execute_herdr_retire_close", self.executed
        )
        exec_patcher.start()
        self.addCleanup(exec_patcher.stop)
        self.pin = ReleasePin(role=ROLE, assigned_name=NAME, locator=OLD_LOCATOR)

    def test_exact_pinned_locator_is_closed(self) -> None:
        ops = _StubOps([_agent_row(NAME, OLD_LOCATOR), _agent_row(GATEWAY_NAME, f"{WS}:p43")])
        result = ops.close_receiver(_request(), self.pin)

        self.assertTrue(result.closed)
        self.assertEqual(self.executed.call_count, 1)
        plan = self.executed.call_args.args[0]
        self.assertEqual(plan.close_targets, ((ROLE, OLD_LOCATOR),))

    def test_foreign_lane_pane_is_never_closed(self) -> None:
        # The live pane at the approved locator belongs to a DIFFERENT lane: the pin
        # set is inconsistent, so the whole generation fails closed.
        foreign = encode_assigned_name(WS, ROLE, "issue_99999_other")
        ops = _StubOps([_agent_row(foreign, OLD_LOCATOR)])
        result = ops.close_receiver(_request(), ReleasePin(
            role=ROLE, assigned_name=foreign, locator=OLD_LOCATOR
        ))

        self.assertFalse(result.closed)
        self.assertFalse(result.old_absent)
        self.assertEqual(result.detail, "close_pin_inconsistent")
        self.executed.assert_not_called()

    def test_vanished_receiver_is_absent_not_a_failure(self) -> None:
        # The old process is already gone (crash / operator close). Nothing to close;
        # the generation may proceed to relaunch.
        ops = _StubOps([_agent_row(GATEWAY_NAME, f"{WS}:p43")])
        result = ops.close_receiver(_request(), self.pin)

        self.assertFalse(result.closed)
        self.assertTrue(result.old_absent)
        self.executed.assert_not_called()

    def test_recycled_assigned_name_at_a_new_locator_is_not_absence(self) -> None:
        # The slot was relaunched into a NEWER agent generation under the same assigned
        # name. Treating that as "old receiver absent" would let a stale approval march
        # on and replace a receiver the owner never looked at.
        ops = _StubOps([_agent_row(NAME, FRESH_LOCATOR)])
        result = ops.close_receiver(_request(), self.pin)

        self.assertFalse(result.closed)
        self.assertFalse(result.old_absent)
        self.assertEqual(result.detail, "assigned_name_recycled")
        self.executed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
