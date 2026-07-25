"""Redmine #14231 — the optional additive startup-execution-event projection.

Design Consultation Answer j#84724 (on #14222 j#84721): a durable, append-only stage
projection that never touches ``startup_actions``' schema / authority, so an old reader
(and existing #14222 grandfathered active lanes) is byte-invariant against it. These
tests pin the contract:

1. the sibling table is genuinely optional / additive — a store with only the
   ``startup_actions`` shape (no execution-events table at all, simulating an
   older-vintage store / launcher) reads as ``None`` from
   :func:`read_execution_events`, never as an error, and ``startup_actions`` behavior
   is completely unaffected by the new table existing or not;
2. :func:`ensure_execution_events_table` is the fail-closed preflight (raises on a
   genuine failure, requires an already-reserved action) while
   :func:`append_execution_event` is best-effort / never-raises, matching the
   wrapper's existing never-block-the-boot contract;
3. :func:`classify_startup_evidence` is pure and distinguishes "no evidence" /
   "evidence gap" (store unreadable) / "stopped before exec" / "exec reached, live
   confirmed" / "exec reached, locator absent" / "exec reached, inventory unreadable"
   without ever collapsing them into the old undifferentiated ``provider_exited``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.startup_execution_events import (
    EXECUTION_EVENT_STAGES,
    JOIN_EXEC_STOPPED_LOCATOR_LIVE,
    JOIN_INVENTORY_UNREADABLE,
    JOIN_NOT_APPLICABLE,
    JOIN_POST_EXEC_LOCATOR_ABSENT,
    JOIN_PROVIDER_LIVE_CONFIRMED,
    JOIN_PROVIDER_LIVE_EXEC_UNRECORDED,
    REASON_STARTUP_EVIDENCE_EXEC_UNRECORDED,
    REASON_STARTUP_EVIDENCE_UNAVAILABLE,
    STAGE_ATTESTATION_WRITE_FAILED,
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    STAGE_NO_EVIDENCE,
    STAGE_PROVIDER_EXEC_CALL_REACHED,
    STAGE_PROVIDER_EXEC_REJECTED,
    STAGE_SELF_LOOKUP_SUCCEEDED,
    STAGE_WRAPPER_ENTERED,
    ExecutionEvent,
    append_execution_event,
    classify_startup_evidence,
    ensure_execution_events_table,
    read_execution_events,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    StartupTransactionError,
    StartupTransactionFence,
    StartupUnit,
)


class ExecutionEventProjectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.fence = StartupTransactionFence(home=self.home)
        self.unit = StartupUnit(
            workspace_id="ws1", lane_id="lane-1", providers=("claude", "codex")
        )

    def _reserve(self, nonce: str = "n1"):
        return self.fence.reserve(self.unit, nonce)

    def _sibling_fence(self) -> StartupTransactionFence:
        """A second fence over the SAME store — the sibling wrapper's view of the lock.

        `_hold()` nests on the instance that already holds it, so contention can only be
        modelled by a distinct instance (as two wrapper processes are).
        """
        return StartupTransactionFence(home=self.home)

    # -- optionality / additivity ----------------------------------------------

    def test_absent_table_reads_as_none_not_error(self):
        """A store with startup_actions but no events table ever created."""
        action = self._reserve()
        result = read_execution_events(self.fence, action.action_id)
        self.assertIsNone(result)

    def test_ensure_then_empty_read_is_empty_tuple_not_none(self):
        """After the preflight lands, zero appends reads as an observed empty set."""
        action = self._reserve()
        ensure_execution_events_table(self.fence, action.action_id)
        result = read_execution_events(self.fence, action.action_id)
        self.assertEqual(result, ())

    def test_absent_store_reads_as_none(self):
        fence = StartupTransactionFence(home=Path(self._tmp.name) / "never_created")
        self.assertIsNone(read_execution_events(fence, "startup-does-not-exist"))

    def test_new_table_does_not_disturb_startup_actions_read(self):
        """Creating + appending to the sibling table never mutates startup_actions."""
        action = self._reserve()
        before = self.fence.read(action.action_id)
        ensure_execution_events_table(self.fence, action.action_id)
        append_execution_event(self.fence, action.action_id, STAGE_WRAPPER_ENTERED)
        append_execution_event(
            self.fence, action.action_id, STAGE_PROVIDER_EXEC_CALL_REACHED
        )
        after = self.fence.read(action.action_id)
        self.assertEqual(before, after)
        self.assertEqual(after.phase, before.phase)
        self.assertEqual(after.revision, before.revision)

    # -- ensure_execution_events_table: fail-closed preflight -------------------

    def test_ensure_requires_a_reserved_action(self):
        with self.assertRaises(StartupTransactionError):
            ensure_execution_events_table(self.fence, "startup-never-reserved")

    def test_ensure_is_idempotent(self):
        action = self._reserve()
        ensure_execution_events_table(self.fence, action.action_id)
        ensure_execution_events_table(self.fence, action.action_id)  # no raise
        self.assertEqual(read_execution_events(self.fence, action.action_id), ())

    # -- append_execution_event: best-effort / never-raises ---------------------

    def test_append_unrecognized_stage_returns_false_never_raises(self):
        action = self._reserve()
        ok = append_execution_event(self.fence, action.action_id, "not_a_real_stage")
        self.assertFalse(ok)
        # And nothing landed.
        self.assertEqual(
            read_execution_events(self.fence, action.action_id) or (), ()
        )

    def test_append_against_unreserved_action_returns_false(self):
        ok = append_execution_event(
            self.fence, "startup-never-reserved", STAGE_WRAPPER_ENTERED
        )
        self.assertFalse(ok)

    def test_append_self_creates_table_without_prior_ensure_call(self):
        """append is self-sufficient: a caller that skips the preflight still lands."""
        action = self._reserve()
        ok = append_execution_event(self.fence, action.action_id, STAGE_WRAPPER_ENTERED)
        self.assertTrue(ok)
        events = read_execution_events(self.fence, action.action_id)
        self.assertEqual([e.stage for e in events], [STAGE_WRAPPER_ENTERED])

    def test_append_against_damaged_store_returns_false(self):
        action = self._reserve()
        # Corrupt the store shape: leave a temp artifact + remove the seal to force
        # STORE_DAMAGED (mirrors the fence's own store_shape() contract).
        self.fence.seal_path.unlink()
        ok = append_execution_event(self.fence, action.action_id, STAGE_WRAPPER_ENTERED)
        self.assertFalse(ok)

    def test_events_are_ordered_and_carry_bounded_reason(self):
        action = self._reserve()
        append_execution_event(self.fence, action.action_id, STAGE_WRAPPER_ENTERED)
        append_execution_event(
            self.fence, action.action_id, STAGE_SELF_LOOKUP_SUCCEEDED
        )
        append_execution_event(
            self.fence,
            action.action_id,
            STAGE_ATTESTATION_WRITE_FAILED,
            bounded_reason="store_write_error",
        )
        events = read_execution_events(self.fence, action.action_id)
        self.assertEqual(
            [e.stage for e in events],
            [
                STAGE_WRAPPER_ENTERED,
                STAGE_SELF_LOOKUP_SUCCEEDED,
                STAGE_ATTESTATION_WRITE_FAILED,
            ],
        )
        self.assertEqual(events[-1].bounded_reason, "store_write_error")
        self.assertEqual([e.sequence for e in events], sorted(e.sequence for e in events))

    def test_events_scoped_per_action_id(self):
        action_a = self._reserve("n1")
        action_b = self.fence.reserve(
            StartupUnit(workspace_id="ws1", lane_id="lane-2", providers=("claude",)),
            "n2",
        )
        append_execution_event(self.fence, action_a.action_id, STAGE_WRAPPER_ENTERED)
        append_execution_event(
            self.fence, action_b.action_id, STAGE_PROVIDER_EXEC_CALL_REACHED
        )
        events_a = read_execution_events(self.fence, action_a.action_id)
        events_b = read_execution_events(self.fence, action_b.action_id)
        self.assertEqual([e.stage for e in events_a], [STAGE_WRAPPER_ENTERED])
        self.assertEqual(
            [e.stage for e in events_b], [STAGE_PROVIDER_EXEC_CALL_REACHED]
        )

    # -- append_execution_event: contention is retried, nothing else is (#14456) ----

    def test_append_lands_while_the_lock_is_briefly_held_by_a_sibling(self):
        """Redmine #14456 root cause: the loser of a two-wrapper race lost its stage.

        The fence lock is `LOCK_EX | LOCK_NB`, so a sibling holding it for its own
        single-row write made this append fail instantly and forever. Driven through the
        REAL lock, not a stubbed error: the holder is a SEPARATE fence instance over the
        same store, because `_hold()` is reentrant per instance and a same-instance
        holder would never contend (which is exactly how the first version of this probe
        passed vacuously). Two wrapper processes are two instances.
        """
        action = self._reserve()
        holder = self._sibling_fence()._hold()
        holder.__enter__()
        released = []

        def _release_after_first_wait(_seconds):
            if not released:
                holder.__exit__(None, None, None)
                released.append(True)

        ok = append_execution_event(
            self.fence,
            action.action_id,
            STAGE_PROVIDER_EXEC_CALL_REACHED,
            sleep=_release_after_first_wait,
        )
        self.assertTrue(released, "the probe never contended -- it proves nothing")
        self.assertTrue(ok)
        events = read_execution_events(self.fence, action.action_id)
        self.assertEqual(
            [e.stage for e in events], [STAGE_PROVIDER_EXEC_CALL_REACHED]
        )

    def test_append_gives_up_when_the_lock_is_never_released(self):
        """Bounded: a permanently held lock still returns False and never raises."""
        action = self._reserve()
        with self._sibling_fence()._hold():
            ticks = iter([0.0, 0.01, 0.02, 99.0])
            ok = append_execution_event(
                self.fence,
                action.action_id,
                STAGE_WRAPPER_ENTERED,
                sleep=lambda _s: None,
                monotonic=lambda: next(ticks),
            )
        self.assertFalse(ok)

    def test_append_does_not_retry_a_non_contention_failure(self):
        """A damaged store cannot be waited out -- it must fail fast, not spin."""
        action = self._reserve()
        self.fence.seal_path.unlink()
        slept = []
        ok = append_execution_event(
            self.fence,
            action.action_id,
            STAGE_WRAPPER_ENTERED,
            sleep=lambda s: slept.append(s),
        )
        self.assertFalse(ok)
        self.assertEqual(slept, [], "a damaged store must not be retried")

    def test_append_retry_never_sleeps_past_its_budget(self):
        action = self._reserve()
        with self._sibling_fence()._hold():
            slept = []
            ticks = iter([0.0, 0.0, 0.0, 100.0])
            append_execution_event(
                self.fence,
                action.action_id,
                STAGE_WRAPPER_ENTERED,
                busy_retry_budget_seconds=0.001,
                sleep=lambda s: slept.append(s),
                monotonic=lambda: next(ticks),
            )
        self.assertTrue(slept)
        for pause in slept:
            self.assertLessEqual(pause, 0.001)

    def test_every_vocabulary_stage_is_appendable(self):
        action = self._reserve()
        for stage in EXECUTION_EVENT_STAGES:
            ok = append_execution_event(self.fence, action.action_id, stage)
            self.assertTrue(ok, f"stage {stage!r} should be appendable")


class ClassifyStartupEvidenceTest(unittest.TestCase):
    """Pure classifier tests — no I/O, no fence."""

    def _event(self, stage: str, *, seq: int = 1, reason: str = "") -> ExecutionEvent:
        return ExecutionEvent(
            sequence=seq,
            action_id="startup-x",
            stage=stage,
            bounded_reason=reason,
            recorded_at="2026-07-21T00:00:00+00:00",
            format_version=1,
        )

    def test_none_events_is_evidence_gap(self):
        verdict = classify_startup_evidence(
            None, live_locator_observed=False, inventory_readable=True
        )
        self.assertEqual(verdict.last_stage, STAGE_NO_EVIDENCE)
        self.assertTrue(verdict.evidence_gap)
        self.assertEqual(verdict.inventory_join, JOIN_NOT_APPLICABLE)
        self.assertEqual(verdict.bounded_reason, REASON_STARTUP_EVIDENCE_UNAVAILABLE)

    def test_empty_events_is_no_evidence_but_not_a_gap(self):
        verdict = classify_startup_evidence(
            (), live_locator_observed=False, inventory_readable=True
        )
        self.assertEqual(verdict.last_stage, STAGE_NO_EVIDENCE)
        self.assertFalse(verdict.evidence_gap)

    def test_stopped_before_exec_with_no_live_locator_is_not_applicable_join(self):
        # A pre-exec stage AND nothing live: there is genuinely no liveness conclusion
        # to draw. (Before #14456 this verdict was also returned when the locator WAS
        # live -- see StoppedBeforeExecWithLiveLocatorTest for that case.)
        events = (self._event(STAGE_WRAPPER_ENTERED),)
        verdict = classify_startup_evidence(
            events, live_locator_observed=False, inventory_readable=True
        )
        self.assertEqual(verdict.last_stage, STAGE_WRAPPER_ENTERED)
        self.assertEqual(verdict.inventory_join, JOIN_NOT_APPLICABLE)
        self.assertFalse(verdict.evidence_gap)

    def test_stopped_before_exec_with_unreadable_inventory_is_not_applicable(self):
        # An unreadable inventory is not a live observation, so it can never promote a
        # pre-exec timeline -- not even when a (nonsensical) True is passed alongside it.
        events = (self._event(STAGE_WRAPPER_ENTERED),)
        verdict = classify_startup_evidence(
            events, live_locator_observed=True, inventory_readable=False
        )
        self.assertEqual(verdict.inventory_join, JOIN_NOT_APPLICABLE)

    def test_explicit_exec_rejection_overrides_reached_flag(self):
        events = (
            self._event(STAGE_PROVIDER_EXEC_CALL_REACHED, seq=1),
            self._event(STAGE_PROVIDER_EXEC_REJECTED, seq=2, reason="argv0_alias_unbound"),
        )
        verdict = classify_startup_evidence(
            events, live_locator_observed=False, inventory_readable=True
        )
        self.assertEqual(verdict.last_stage, STAGE_PROVIDER_EXEC_REJECTED)
        self.assertEqual(verdict.inventory_join, JOIN_NOT_APPLICABLE)
        self.assertEqual(verdict.bounded_reason, "argv0_alias_unbound")

    def test_explicit_exec_rejection_is_never_promoted_by_a_live_locator(self):
        # #14456: a live locator must NOT turn an evidenced exec rejection into a
        # success. It is reported as the contradiction it is (the live pane belongs to
        # something else), keeping the recorded reason.
        events = (
            self._event(STAGE_PROVIDER_EXEC_CALL_REACHED, seq=1),
            self._event(STAGE_PROVIDER_EXEC_REJECTED, seq=2, reason="argv0_alias_unbound"),
        )
        verdict = classify_startup_evidence(
            events, live_locator_observed=True, inventory_readable=True
        )
        self.assertEqual(verdict.inventory_join, JOIN_EXEC_STOPPED_LOCATOR_LIVE)
        self.assertNotEqual(verdict.inventory_join, JOIN_PROVIDER_LIVE_CONFIRMED)
        self.assertNotEqual(verdict.inventory_join, JOIN_PROVIDER_LIVE_EXEC_UNRECORDED)
        self.assertEqual(verdict.last_stage, STAGE_PROVIDER_EXEC_REJECTED)
        self.assertEqual(verdict.bounded_reason, "argv0_alias_unbound")

    def test_exec_reached_and_live_locator_is_confirmed(self):
        events = (self._event(STAGE_PROVIDER_EXEC_CALL_REACHED),)
        verdict = classify_startup_evidence(
            events, live_locator_observed=True, inventory_readable=True
        )
        self.assertEqual(verdict.inventory_join, JOIN_PROVIDER_LIVE_CONFIRMED)

    def test_exec_reached_and_locator_absent_from_readable_inventory(self):
        events = (self._event(STAGE_PROVIDER_EXEC_CALL_REACHED),)
        verdict = classify_startup_evidence(
            events, live_locator_observed=False, inventory_readable=True
        )
        self.assertEqual(verdict.inventory_join, JOIN_POST_EXEC_LOCATOR_ABSENT)

    def test_exec_reached_but_inventory_unreadable_is_distinguished(self):
        events = (self._event(STAGE_PROVIDER_EXEC_CALL_REACHED),)
        verdict = classify_startup_evidence(
            events, live_locator_observed=False, inventory_readable=False
        )
        self.assertEqual(verdict.inventory_join, JOIN_INVENTORY_UNREADABLE)
        # Even a (nonsensical) True locator observation must not override an
        # unreadable inventory -- unreadable takes precedence, never guessed past.
        verdict2 = classify_startup_evidence(
            events, live_locator_observed=True, inventory_readable=False
        )
        self.assertEqual(verdict2.inventory_join, JOIN_INVENTORY_UNREADABLE)

    def test_live_locator_without_an_exec_row_is_a_terminal_success_not_pre_exec(self):
        """Redmine #14456: the exact shape observed on the live pair launch.

        The wrapper's `attestation_write_succeeded` / `provider_exec_call_reached`
        appends were dropped by lock contention, leaving `self_lookup_succeeded` last,
        while the provider was demonstrably running at the locator. The pre-#14456 fold
        answered `not_applicable` ("the wrapper stopped before the provider exec call")
        about a live provider.
        """
        events = (
            self._event(STAGE_WRAPPER_ENTERED, seq=1),
            self._event(STAGE_SELF_LOOKUP_SUCCEEDED, seq=2),
        )
        verdict = classify_startup_evidence(
            events, live_locator_observed=True, inventory_readable=True
        )
        self.assertEqual(verdict.inventory_join, JOIN_PROVIDER_LIVE_EXEC_UNRECORDED)
        self.assertNotEqual(verdict.inventory_join, JOIN_NOT_APPLICABLE)
        # The hole is declared rather than papered over...
        self.assertTrue(verdict.evidence_gap)
        self.assertEqual(
            verdict.bounded_reason, REASON_STARTUP_EVIDENCE_EXEC_UNRECORDED
        )
        # ...and no stage that was never recorded is invented to hide it.
        self.assertEqual(verdict.last_stage, STAGE_SELF_LOOKUP_SUCCEEDED)

    def test_live_locator_with_no_evidence_at_all_is_still_a_terminal_success(self):
        """A store predating the projection still must not under-report a live pane."""
        for events in (None, ()):
            with self.subTest(events=events):
                verdict = classify_startup_evidence(
                    events, live_locator_observed=True, inventory_readable=True
                )
                self.assertEqual(
                    verdict.inventory_join, JOIN_PROVIDER_LIVE_EXEC_UNRECORDED
                )
                self.assertEqual(verdict.last_stage, STAGE_NO_EVIDENCE)
                self.assertTrue(verdict.evidence_gap)

    def test_no_evidence_without_a_live_locator_keeps_its_14231_verdict(self):
        """Non-regression: the #14231 contract is untouched where nothing is live."""
        gap = classify_startup_evidence(
            None, live_locator_observed=False, inventory_readable=True
        )
        self.assertEqual(gap.inventory_join, JOIN_NOT_APPLICABLE)
        self.assertTrue(gap.evidence_gap)
        self.assertEqual(gap.bounded_reason, REASON_STARTUP_EVIDENCE_UNAVAILABLE)
        empty = classify_startup_evidence(
            (), live_locator_observed=False, inventory_readable=True
        )
        self.assertEqual(empty.inventory_join, JOIN_NOT_APPLICABLE)
        self.assertFalse(empty.evidence_gap)
        self.assertEqual(empty.bounded_reason, "")

    def test_fully_evidenced_live_launch_reports_no_gap(self):
        """The two live joins are distinguishable: only the derived one flags a gap."""
        events = (self._event(STAGE_PROVIDER_EXEC_CALL_REACHED),)
        verdict = classify_startup_evidence(
            events, live_locator_observed=True, inventory_readable=True
        )
        self.assertEqual(verdict.inventory_join, JOIN_PROVIDER_LIVE_CONFIRMED)
        self.assertFalse(verdict.evidence_gap)

    def test_last_stage_is_the_most_advanced_recorded(self):
        events = (
            self._event(STAGE_WRAPPER_ENTERED, seq=1),
            self._event(STAGE_ATTESTATION_WRITE_SUCCEEDED, seq=2),
            self._event(STAGE_PROVIDER_EXEC_CALL_REACHED, seq=3),
        )
        verdict = classify_startup_evidence(
            events, live_locator_observed=True, inventory_readable=True
        )
        self.assertEqual(verdict.last_stage, STAGE_PROVIDER_EXEC_CALL_REACHED)


if __name__ == "__main__":
    unittest.main()
