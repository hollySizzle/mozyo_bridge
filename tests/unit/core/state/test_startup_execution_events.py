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
    JOIN_INVENTORY_UNREADABLE,
    JOIN_NOT_APPLICABLE,
    JOIN_POST_EXEC_LOCATOR_ABSENT,
    JOIN_PROVIDER_LIVE_CONFIRMED,
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

    def test_stopped_before_exec_is_not_applicable_join(self):
        events = (self._event(STAGE_WRAPPER_ENTERED),)
        verdict = classify_startup_evidence(
            events, live_locator_observed=True, inventory_readable=True
        )
        self.assertEqual(verdict.last_stage, STAGE_WRAPPER_ENTERED)
        self.assertEqual(verdict.inventory_join, JOIN_NOT_APPLICABLE)
        self.assertFalse(verdict.evidence_gap)

    def test_explicit_exec_rejection_overrides_reached_flag(self):
        events = (
            self._event(STAGE_PROVIDER_EXEC_CALL_REACHED, seq=1),
            self._event(STAGE_PROVIDER_EXEC_REJECTED, seq=2, reason="argv0_alias_unbound"),
        )
        verdict = classify_startup_evidence(
            events, live_locator_observed=True, inventory_readable=True
        )
        self.assertEqual(verdict.last_stage, STAGE_PROVIDER_EXEC_REJECTED)
        self.assertEqual(verdict.inventory_join, JOIN_NOT_APPLICABLE)
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
