"""Stall escalation gate wiring (Redmine #15855).

Real collaborators wired together — the pure policy
(``domain.stall_escalation_policy``) and the durable store
(``core.state.stall_escalation``) — driven by real :class:`StallObservation` values across
a sequence of passes. Only the journal writer and the wake enqueue are fakes, because they
are the two seams that reach outside.

What this file characterizes that neither unit file can:

- a run of *passes* produces one escalation, and a busy or unreadable lane produces none;
- the run follows the durable slot across a locator rebind and restarts across a
  generation change;
- **phase 1 makes no external mutation at all** — a threshold crossing becomes a local
  pending row, never a write;
- **phase 2 writes at most one journal per pass**, only when the pass-wide budget is
  unspent, oldest firing first, and wakes only after the journal id was read back.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.stall_escalation import (
    PendingEscalation,
    escalation_idempotency_key,
    StallEscalationStore,
    StallEscalationStoreError,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_escalation_pass import (  # noqa: E501
    SETTLE_ANCHOR_UNRESOLVED,
    SETTLE_BUDGET_SPENT,
    SETTLE_NOTHING_PENDING,
    SETTLE_RECORDED,
    SETTLE_WRITE_REFUSED,
    SETTLE_WRITE_UNCERTAIN,
    WRITE_NOT_SENT,
    WRITE_RECORDED,
    WRITE_UNCERTAIN,
    JournalWriteResult,
    ObservedUnit,
    apply_escalation_gate,
    settle_pending_escalations,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_pass import (  # noqa: E501
    StallObservation,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.pane_stall_sensor import (  # noqa: E501
    DIFF_IDENTICAL,
    ScreenDiff,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
    CLASS_BUSY_LIKELY,
    CLASS_CONTENT_REFUSAL,
    CLASS_SCREEN_UNREADABLE,
    CLASS_UNRESPONSIVE_INDETERMINATE,
    prescribe,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_escalation_policy import (  # noqa: E501
    WatchIdentity,
)

WS = "wsA"
LANE = "issue_15855_stall_wiring"


def _identity(lane_id=LANE, role="claude", generation="g1", target="w1V:pK", workspace_id=WS):
    return WatchIdentity(
        workspace_id=workspace_id,
        lane_id=lane_id,
        role=role,
        generation=generation,
        target=target,
    )


def _unit(stall_class, *, identity=None, issue="15855", provider_id="claude",
          matched_id="", evidence_tier=""):
    identity = identity or _identity()
    return ObservedUnit(
        identity=identity,
        observation=StallObservation(
            target=identity.target,
            provider_id=provider_id,
            diff=ScreenDiff(
                target=identity.target,
                state=DIFF_IDENTICAL,
                similarity=1.0,
                elapsed_seconds=8.0,
            ),
            stall_class=stall_class,
            prescription=prescribe(stall_class),
            matched_id=matched_id,
        ),
        issue=issue,
        evidence_tier=evidence_tier,
    )


class _Clock:
    """Deterministic monotonic stamps, so each pass has its own instant."""

    def __init__(self) -> None:
        self.tick = 0

    def __call__(self) -> str:
        self.tick += 1
        return f"2026-08-22T09:{self.tick:02d}:00+00:00"


class _Writer:
    """Fake canonical-journal writer: records what it was asked to write.

    ``outcome`` defaults to a DETERMINISTIC no-send, which is the only refusal shape that
    leaves the shared pass budget untouched. Tests that want the possibly-landed shape pass
    ``outcome=WRITE_UNCERTAIN`` explicitly, so the two can never be confused in an assertion.
    """

    def __init__(self, *, journal_ids=None, reason="write_optin_unset",
                 outcome=WRITE_NOT_SENT, raises=False) -> None:
        self.calls = []
        self.journal_ids = list(journal_ids or [])
        self.reason = reason
        self.outcome = outcome
        self.raises = raises

    def __call__(self, pending):
        self.calls.append(pending)
        if self.raises:
            raise RuntimeError("transport exploded")
        if self.journal_ids:
            return JournalWriteResult(
                outcome=WRITE_RECORDED,
                journal_id=self.journal_ids.pop(0),
                reason="recorded",
            )
        return JournalWriteResult(outcome=self.outcome, reason=self.reason)


class _Wake:
    def __init__(self, *, succeed=True, raises=False) -> None:
        self.calls = []
        self.succeed = succeed
        self.raises = raises

    def __call__(self, workspace_id, issue):
        self.calls.append((workspace_id, issue))
        if self.raises:
            raise RuntimeError("wake queue unavailable")
        return self.succeed


class _Redmine:
    """The one external system: which journal carries which firing.

    Deliberately NOT per-writer state. A later pass builds a new writer while Redmine still
    holds the journal an earlier pass wrote, so a per-writer authority would make "verify
    what an earlier pass recorded" untestable — which is precisely the path review j#110254
    found unguarded.
    """

    def __init__(self) -> None:
        self.journals: dict = {}
        self.reads = 0

    def wrap(self, writer):
        """A writer whose successful writes actually land in this fake Redmine."""

        def write(pending):
            result = writer(pending)
            if result.outcome == WRITE_RECORDED and result.journal_id:
                self.journals[(pending.issue, pending.idempotency_key)] = result.journal_id
            return result

        return write

    def verify(self, pending) -> str:
        self.reads += 1
        return self.journals.get((pending.issue, pending.idempotency_key), "")


class GateBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = StallEscalationStore(path=self.dir / "stall-escalation.sqlite")
        self.clock = _Clock()
        self.redmine = _Redmine()

    def observe(self, units, **kwargs):
        return apply_escalation_gate(
            units, workspace_id=WS, store=self.store, now=self.clock, **kwargs
        )

    def settle(self, **kwargs):
        # The writer's successful writes land in the fake Redmine, and the wake admission
        # asks that same Redmine. One external system, two faces: a test where the writer
        # could report a journal the verifier cannot find would be modelling an inconsistent
        # Redmine rather than a hostile one, and would pass for the wrong reason.
        if kwargs.get("write_journal") is not None:
            kwargs["write_journal"] = self.redmine.wrap(kwargs["write_journal"])
        kwargs.setdefault("verify_journal", self.redmine.verify)
        return settle_pending_escalations(
            workspace_id=WS, store=self.store, now=self.clock, **kwargs
        )


# ------------------------------------------------------------------------------------
# Phase 1
# ------------------------------------------------------------------------------------


class ThresholdTest(GateBase):
    def test_one_pass_records_a_run_but_does_not_fire(self) -> None:
        outcome = self.observe([_unit(CLASS_CONTENT_REFUSAL)])
        self.assertEqual(outcome.observed, 1)
        self.assertEqual(outcome.advanced, 1)
        self.assertEqual(outcome.escalated, 0)
        self.assertEqual(self.store.open_pending(WS), ())
        self.assertEqual(self.store.read_streaks(WS)[(WS, LANE, "claude")].consecutive, 1)

    def test_two_same_class_passes_fire_once(self) -> None:
        units = [_unit(CLASS_CONTENT_REFUSAL, matched_id="sig-7")]
        self.observe(units)
        second = self.observe(units)
        self.assertEqual(second.escalated, 1)
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.stall_class, CLASS_CONTENT_REFUSAL)
        self.assertEqual(pending.prescription, "context_reset_reinjection")
        self.assertEqual(pending.consecutive, 2)
        self.assertEqual(pending.issue, "15855")
        self.assertEqual(pending.matched_id, "sig-7")
        self.assertFalse(pending.recorded)

    def test_a_persisting_stall_enqueues_exactly_one_firing(self) -> None:
        units = [_unit(CLASS_UNRESPONSIVE_INDETERMINATE)]
        fired = sum(self.observe(units).escalated for _ in range(12))
        self.assertEqual(fired, 1)
        self.assertEqual(len(self.store.open_pending(WS)), 1)
        self.assertEqual(
            self.store.read_streaks(WS)[(WS, LANE, "claude")].consecutive, 12
        )

    def test_operator_threshold_is_honoured(self) -> None:
        units = [_unit(CLASS_CONTENT_REFUSAL)]
        for _ in range(3):
            self.assertEqual(self.observe(units, threshold=4).escalated, 0)
        self.assertEqual(self.observe(units, threshold=4).escalated, 1)


class PhaseOneIsFreeTest(GateBase):
    def test_observing_never_calls_a_writer_or_a_wake(self) -> None:
        # Phase 1 has no writer/wake parameter at all: a threshold crossing becomes a local
        # pending row, and only phase 2 can spend the pass's external mutation. This is the
        # signature-level guarantee, asserted so a future parameter cannot be added
        # silently.
        import inspect

        params = set(inspect.signature(apply_escalation_gate).parameters)
        self.assertNotIn("wake", params)
        self.assertNotIn("write_journal", params)
        self.assertNotIn("budget", params)

    def test_a_firing_leaves_the_durable_record_untouched(self) -> None:
        units = [_unit(CLASS_CONTENT_REFUSAL)]
        self.observe(units)
        self.observe(units)
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.journal_id, "")
        self.assertEqual(pending.woke_at, "")
        self.assertEqual(pending.attempts, 0)


class NoiseSuppressionTest(GateBase):
    def test_a_busy_lane_never_fires(self) -> None:
        units = [_unit(CLASS_BUSY_LIKELY)]
        for _ in range(20):
            self.assertEqual(self.observe(units).escalated, 0)
        self.assertEqual(self.store.open_pending(WS), ())
        self.assertEqual(self.store.read_streaks(WS), {})

    def test_progress_between_stalls_prevents_the_threshold(self) -> None:
        for _ in range(6):
            self.observe([_unit(CLASS_CONTENT_REFUSAL)])
            self.observe([_unit(CLASS_BUSY_LIKELY)])
        self.assertEqual(self.store.open_pending(WS), ())

    def test_an_unreadable_slot_alone_never_fires(self) -> None:
        units = [_unit(CLASS_SCREEN_UNREADABLE)]
        for _ in range(20):
            self.assertEqual(self.observe(units).escalated, 0)
        self.assertEqual(self.store.read_streaks(WS), {})

    def test_a_held_pass_writes_nothing_at_all(self) -> None:
        # HOLD returns the previous state unchanged, so writing it back would be
        # byte-identical and invisible in the stored row -- but not free. A cockpit whose
        # panes are all momentarily unreadable would issue one pointless SQLite write per
        # slot per tick. The guard is a zero-write property, so it is measured as one.
        self.observe([_unit(CLASS_CONTENT_REFUSAL)])
        before = self.store.read_streaks(WS)[(WS, LANE, "claude")]
        counted = _CountingStore(self.store)
        outcome = apply_escalation_gate(
            [_unit(CLASS_SCREEN_UNREADABLE)],
            workspace_id=WS,
            store=counted,
            now=self.clock,
        )
        self.assertEqual(outcome.held, 1)
        self.assertEqual(counted.writes, 0)
        self.assertEqual(counted.clears, 0)
        self.assertEqual(self.store.read_streaks(WS)[(WS, LANE, "claude")], before)

    def test_an_unreadable_pass_neither_erases_nor_completes_a_run(self) -> None:
        self.observe([_unit(CLASS_CONTENT_REFUSAL)])
        held = self.observe([_unit(CLASS_SCREEN_UNREADABLE)])
        self.assertEqual(held.held, 1)
        self.assertEqual(held.escalated, 0)
        self.assertEqual(self.store.read_streaks(WS)[(WS, LANE, "claude")].consecutive, 1)
        self.assertEqual(self.observe([_unit(CLASS_CONTENT_REFUSAL)]).escalated, 1)


class _CountingStore:
    """Delegating spy over the REAL store: counts the mutating calls the gate makes.

    Delegating rather than faking keeps the store a real collaborator — the counted pass
    still reads and writes the same SQLite file — so a zero-write assertion is about what
    the gate did, not about what a fake was asked to pretend.
    """

    def __init__(self, inner):
        self._inner = inner
        self.writes = 0
        self.clears = 0

    def write_streak(self, row):
        self.writes += 1
        return self._inner.write_streak(row)

    def clear_streak(self, key):
        self.clears += 1
        return self._inner.clear_streak(key)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class IdentityTest(GateBase):
    def test_a_run_survives_a_locator_rebind(self) -> None:
        # herdr locators are recycled and rebound; a locator-keyed run would restart for a
        # unit that never moved and so could never reach any threshold.
        self.observe([_unit(CLASS_CONTENT_REFUSAL, identity=_identity(target="w1V:pK"))])
        second = self.observe(
            [_unit(CLASS_CONTENT_REFUSAL, identity=_identity(target="w9Q:pZ"))]
        )
        self.assertEqual(second.escalated, 1)
        self.assertEqual(len(self.store.read_streaks(WS)), 1)

    def test_a_generation_change_restarts_the_run(self) -> None:
        self.observe([_unit(CLASS_CONTENT_REFUSAL, identity=_identity(generation="g1"))])
        second = self.observe(
            [_unit(CLASS_CONTENT_REFUSAL, identity=_identity(generation="g2"))]
        )
        self.assertEqual(second.escalated, 0)
        self.assertEqual(self.store.read_streaks(WS)[(WS, LANE, "claude")].consecutive, 1)

    def test_roles_on_one_lane_are_independent_runs(self) -> None:
        units = [
            _unit(CLASS_CONTENT_REFUSAL, identity=_identity(role="claude")),
            _unit(CLASS_BUSY_LIKELY, identity=_identity(role="codex")),
        ]
        self.observe(units)
        outcome = self.observe(units)
        self.assertEqual(outcome.escalated, 1)
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.role, "claude")

    def test_a_foreign_workspace_unit_is_refused_not_folded(self) -> None:
        outcome = self.observe(
            [_unit(CLASS_CONTENT_REFUSAL, identity=_identity(workspace_id="wsB"))]
        )
        self.assertTrue(outcome.errors)
        self.assertEqual(self.store.read_streaks(WS), {})
        self.assertEqual(self.store.read_streaks("wsB"), {})


class SweepTest(GateBase):
    def test_absent_slots_are_forgotten_when_the_whole_inventory_was_observed(self) -> None:
        self.observe(
            [
                _unit(CLASS_CONTENT_REFUSAL, identity=_identity(role="claude")),
                _unit(CLASS_CONTENT_REFUSAL, identity=_identity(role="codex")),
            ]
        )
        outcome = self.observe(
            [_unit(CLASS_CONTENT_REFUSAL, identity=_identity(role="claude"))]
        )
        self.assertEqual(outcome.forgotten, 1)
        self.assertEqual(set(self.store.read_streaks(WS)), {(WS, LANE, "claude")})

    def test_a_subset_watcher_must_not_forget(self) -> None:
        self.observe(
            [
                _unit(CLASS_CONTENT_REFUSAL, identity=_identity(role="claude")),
                _unit(CLASS_CONTENT_REFUSAL, identity=_identity(role="codex")),
            ]
        )
        outcome = self.observe(
            [_unit(CLASS_CONTENT_REFUSAL, identity=_identity(role="claude"))],
            forget_absent=False,
        )
        self.assertEqual(outcome.forgotten, 0)
        self.assertEqual(len(self.store.read_streaks(WS)), 2)

    def test_a_corrupt_stored_row_does_not_blind_the_watcher(self) -> None:
        import sqlite3

        self.observe([_unit(CLASS_CONTENT_REFUSAL)])
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute("UPDATE stall_watch_streak SET stall_class='no_such_class'")
            conn.commit()
        finally:
            conn.close()
        outcome = self.observe(
            [
                _unit(CLASS_CONTENT_REFUSAL, identity=_identity(role="claude")),
                _unit(CLASS_CONTENT_REFUSAL, identity=_identity(role="codex")),
            ]
        )
        # The corrupt slot restarts from this pass: delays an escalation, never invents one.
        self.assertEqual(outcome.advanced, 2)
        self.assertEqual(outcome.escalated, 0)


class PhaseOneRefusalTest(GateBase):
    def test_a_blank_workspace_is_refused_with_zero_writes(self) -> None:
        outcome = apply_escalation_gate(
            [_unit(CLASS_CONTENT_REFUSAL)],
            workspace_id="",
            store=self.store,
            now=self.clock,
        )
        self.assertEqual(outcome.escalated, 0)
        self.assertFalse(self.store.path.exists())
        # The SPECIFIC refusal, not merely "some error": the downstream
        # foreign-workspace check would also reject these units and produce a
        # superficially identical outcome, so asserting only `errors` truthy would
        # leave this guard untested behind it.
        self.assertEqual(outcome.workspace_id, "")
        self.assertEqual(len(outcome.errors), 1)
        self.assertIn("workspace_id is required", outcome.errors[0])

    def test_an_unreadable_store_is_reported_not_raised(self) -> None:
        import sqlite3

        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute("PRAGMA user_version = 999")
        finally:
            conn.close()
        outcome = self.observe([_unit(CLASS_CONTENT_REFUSAL)])
        self.assertEqual(outcome.escalated, 0)
        self.assertTrue(outcome.errors)
        with self.assertRaises(StallEscalationStoreError):
            self.store.read_streaks(WS)

    def test_an_empty_pass_is_a_clean_no_op(self) -> None:
        outcome = self.observe([])
        self.assertEqual(outcome.observed, 0)
        self.assertEqual(outcome.errors, ())


# ------------------------------------------------------------------------------------
# Phase 2
# ------------------------------------------------------------------------------------


class SettleBase(GateBase):
    def fire(self, *, issue="15855", role="claude", lane_id=LANE):
        """Drive one slot to its threshold so exactly one firing is pending."""
        units = [
            _unit(
                CLASS_CONTENT_REFUSAL,
                identity=_identity(role=role, lane_id=lane_id),
                issue=issue,
            )
        ]
        self.observe(units, forget_absent=False)
        self.observe(units, forget_absent=False)


class BudgetTest(SettleBase):
    def test_a_spent_budget_writes_nothing(self) -> None:
        # Callback delivery holds first priority for the pass's one external mutation.
        self.fire()
        writer = _Writer(journal_ids=["110130"])
        budget = {"reads": 0, "mutated": True, "uncertain": False}
        outcome = self.settle(budget=budget, write_journal=writer, wake=_Wake())
        self.assertEqual(outcome.reason, SETTLE_BUDGET_SPENT)
        self.assertEqual(writer.calls, [])
        self.assertEqual(outcome.unrecorded, 1)
        self.assertFalse(outcome.spent_budget)

    def test_an_unspent_budget_writes_exactly_one_journal_and_spends_it(self) -> None:
        self.fire()
        writer = _Writer(journal_ids=["110130"])
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        outcome = self.settle(budget=budget, write_journal=writer, wake=_Wake())
        self.assertEqual(outcome.reason, SETTLE_RECORDED)
        self.assertEqual(len(writer.calls), 1)
        self.assertTrue(budget["mutated"])
        self.assertTrue(outcome.spent_budget)
        self.assertEqual(outcome.recorded.journal_id, "110130")

    def test_only_one_journal_is_written_per_pass(self) -> None:
        self.fire(role="claude")
        self.fire(role="codex")
        self.assertEqual(len(self.store.unrecorded_pending(WS)), 2)
        writer = _Writer(journal_ids=["110130", "110131"])
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        self.settle(budget=budget, write_journal=writer, wake=_Wake())
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(len(self.store.unrecorded_pending(WS)), 1)

    def test_a_later_pass_settles_the_remainder(self) -> None:
        self.fire(role="claude")
        self.fire(role="codex")
        writer = _Writer(journal_ids=["110130", "110131"])
        for _ in range(2):
            self.settle(
                budget={"reads": 0, "mutated": False, "uncertain": False},
                write_journal=writer,
                wake=_Wake(),
            )
        self.assertEqual(len(writer.calls), 2)
        self.assertEqual(self.store.open_pending(WS), ())

    def test_an_uncertain_budget_defers_exactly_like_a_mutated_one(self) -> None:
        # `budget_spent` is `mutated OR uncertain` (pass_external_budget). Checking only
        # `mutated` -- the pre-j#110132 behaviour -- let this rail write behind another
        # leg's UNCERTAIN partial effect.
        self.fire()
        writer = _Writer(journal_ids=["110130"])
        budget = {"reads": 0, "mutated": False, "uncertain": True}
        outcome = self.settle(budget=budget, write_journal=writer, wake=_Wake())
        self.assertEqual(outcome.reason, SETTLE_BUDGET_SPENT)
        self.assertEqual(writer.calls, [])

    def test_an_uncertain_write_spends_the_budget_as_uncertain(self) -> None:
        self.fire()
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        wake = _Wake()
        outcome = self.settle(
            budget=budget,
            write_journal=_Writer(outcome=WRITE_UNCERTAIN, reason="readback_unverified"),
            wake=wake,
        )
        self.assertEqual(outcome.reason, SETTLE_WRITE_UNCERTAIN)
        self.assertTrue(budget["uncertain"])
        self.assertFalse(budget["mutated"])
        # Unverifiable is not recorded, so nobody is woken to read a journal that may not
        # exist -- and the firing stays retryable.
        self.assertEqual(wake.calls, [])
        self.assertEqual(len(self.store.unrecorded_pending(WS)), 1)

    def test_a_deterministic_no_send_leaves_the_budget_untouched(self) -> None:
        # A refusal that never reached Redmine must not cost the pass its slot.
        self.fire()
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        outcome = self.settle(
            budget=budget,
            write_journal=_Writer(outcome=WRITE_NOT_SENT, reason="write_optin_unset"),
            wake=_Wake(),
        )
        self.assertEqual(outcome.reason, SETTLE_WRITE_REFUSED)
        self.assertFalse(budget["mutated"])
        self.assertFalse(budget["uncertain"])

    def test_an_uncertain_write_blocks_a_later_workspace_in_the_same_pass(self) -> None:
        # The budget dict is shared across every workspace in one bounded pass, so the
        # second workspace must see the first one's uncertainty and defer.
        self.fire(role="claude")
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        first = self.settle(
            budget=budget,
            write_journal=_Writer(outcome=WRITE_UNCERTAIN, reason="transport_error"),
            wake=_Wake(),
        )
        self.assertEqual(first.reason, SETTLE_WRITE_UNCERTAIN)

        # A real pending firing in the OTHER workspace, so the deferral is about the budget
        # rather than about there being nothing to write.
        self.store.enqueue_pending(
            PendingEscalation(
                idempotency_key=escalation_idempotency_key(
                    workspace_id="wsB",
                    lane_id="lane_b",
                    role="claude",
                    generation="",
                    stall_class=CLASS_CONTENT_REFUSAL,
                    first_observed_at="2026-08-22T09:01:00+00:00",
                    issue="15999",
                ),
                workspace_id="wsB",
                lane_id="lane_b",
                role="claude",
                stall_class=CLASS_CONTENT_REFUSAL,
                prescription="context_reset_reinjection",
                consecutive=2,
                first_observed_at="2026-08-22T09:01:00+00:00",
                escalated_at="2026-08-22T09:02:00+00:00",
                issue="15999",
            )
        )
        other_ws = _Writer(journal_ids=["110131"])
        second = settle_pending_escalations(
            workspace_id="wsB",
            store=self.store,
            now=self.clock,
            budget=budget,
            write_journal=other_ws,
            wake=_Wake(),
        )
        self.assertEqual(second.reason, SETTLE_BUDGET_SPENT)
        self.assertEqual(other_ws.calls, [])

    def test_no_budget_object_means_unconstrained(self) -> None:
        # A standalone caller (an operator command) has no supervisor budget to respect.
        self.fire()
        writer = _Writer(journal_ids=["110130"])
        outcome = self.settle(write_journal=writer, wake=_Wake())
        self.assertEqual(outcome.reason, SETTLE_RECORDED)


class WriteResultShapeTest(unittest.TestCase):
    """The write result is the budget's vocabulary, so its shape is enforced, not assumed."""

    def test_a_recorded_write_must_carry_the_journal_id_it_read_back(self) -> None:
        # A "recorded" result with no id would sail past the readback fence and let a
        # coordinator be woken to read a journal nobody proved exists.
        with self.assertRaises(ValueError):
            JournalWriteResult(outcome=WRITE_RECORDED)
        with self.assertRaises(ValueError):
            JournalWriteResult(outcome=WRITE_RECORDED, journal_id="")

    def test_an_unknown_outcome_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            JournalWriteResult(outcome="probably_fine")

    def test_the_non_recorded_outcomes_need_no_journal_id(self) -> None:
        for outcome in (WRITE_NOT_SENT, WRITE_UNCERTAIN):
            with self.subTest(outcome=outcome):
                self.assertEqual(JournalWriteResult(outcome=outcome).journal_id, "")


class FairnessTest(SettleBase):
    def test_the_oldest_firing_takes_the_slot(self) -> None:
        self.fire(role="claude")
        self.fire(role="codex")
        oldest = self.store.unrecorded_pending(WS)[0]
        writer = _Writer(journal_ids=["110130"])
        self.settle(
            budget={"reads": 0, "mutated": False, "uncertain": False},
            write_journal=writer,
            wake=_Wake(),
        )
        self.assertEqual(writer.calls[0].idempotency_key, oldest.idempotency_key)

    def test_a_newer_firing_cannot_overtake_an_older_one(self) -> None:
        self.fire(role="claude")
        first_key = self.store.unrecorded_pending(WS)[0].idempotency_key
        writer = _Writer(journal_ids=[])  # every write refused
        for _ in range(5):
            self.fire(role="codex")
            self.settle(
                budget={"reads": 0, "mutated": False, "uncertain": False},
                write_journal=writer,
                wake=_Wake(),
            )
        self.assertTrue(all(c.idempotency_key == first_key for c in writer.calls))

    def test_the_oldest_pending_age_is_reported(self) -> None:
        # The starvation-visibility requirement: a queue that is not moving must say so.
        self.fire()
        outcome = self.settle(
            budget={"reads": 0, "mutated": True, "uncertain": False},
            write_journal=_Writer(),
            wake=_Wake(),
        )
        self.assertTrue(outcome.oldest_unrecorded_at)
        self.assertEqual(
            outcome.oldest_unrecorded_at,
            self.store.unrecorded_pending(WS)[0].escalated_at,
        )


class ReadbackFenceTest(SettleBase):
    def test_a_refused_write_leaves_the_firing_pending_and_counted(self) -> None:
        self.fire()
        writer = _Writer(reason="write_optin_unset")
        outcome = self.settle(
            budget={"reads": 0, "mutated": False, "uncertain": False},
            write_journal=writer,
            wake=_Wake(),
        )
        self.assertEqual(outcome.reason, SETTLE_WRITE_REFUSED)
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.attempts, 1)
        self.assertEqual(pending.last_reason, "write_optin_unset")

    def test_a_refused_write_does_not_spend_the_budget(self) -> None:
        self.fire()
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        self.settle(budget=budget, write_journal=_Writer(), wake=_Wake())
        self.assertFalse(budget["mutated"])

    def test_a_raising_writer_never_aborts_the_pass(self) -> None:
        self.fire()
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        outcome = self.settle(
            budget=budget, write_journal=_Writer(raises=True), wake=_Wake()
        )
        # A raise is indistinguishable from a landed POST, so it is UNCERTAIN -- not a
        # refusal. Reporting it as refused would leave a possibly-landed external mutation
        # off the shared budget (review j#110132 finding_1).
        self.assertEqual(outcome.reason, SETTLE_WRITE_UNCERTAIN)
        self.assertTrue(budget["uncertain"])
        self.assertFalse(budget["mutated"])
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertIn("writer_raised", pending.last_reason)

    def test_a_retried_firing_never_writes_a_second_journal(self) -> None:
        units = [_unit(CLASS_CONTENT_REFUSAL)]
        self.observe(units)
        self.observe(units)  # fires
        writer = _Writer(journal_ids=["110130", "110131"])
        self.settle(write_journal=writer, wake=_Wake())
        # The same run keeps being observed and keeps not escalating (latched), and the
        # idempotency key would collide even if it did.
        for _ in range(5):
            self.observe(units)
            self.settle(write_journal=writer, wake=_Wake())
        self.assertEqual(len(writer.calls), 1)


class WakeOrderingTest(SettleBase):
    def test_the_wake_happens_only_after_the_journal_id_is_read_back(self) -> None:
        self.fire()
        writer = _Writer(journal_ids=["110130"])
        wake = _Wake()
        self.settle(write_journal=writer, wake=wake)
        self.assertEqual(wake.calls, [(WS, "15855")])
        self.assertEqual(self.store.open_pending(WS), ())

    def test_a_refused_write_wakes_nobody(self) -> None:
        # A coordinator woken to read a journal that does not exist is the one inversion
        # this rail is built to prevent.
        self.fire()
        wake = _Wake()
        self.settle(write_journal=_Writer(), wake=wake)
        self.assertEqual(wake.calls, [])

    def test_a_recorded_but_unwoken_firing_is_woken_on_a_later_pass_for_free(self) -> None:
        self.fire()
        wake_fails = _Wake(succeed=False)
        self.settle(write_journal=_Writer(journal_ids=["110130"]), wake=wake_fails)
        (pending,) = self.store.unwoken_pending(WS)
        self.assertEqual(pending.journal_id, "110130")
        # The retry costs no external mutation, so a spent budget does not block it.
        wake = _Wake()
        budget = {"reads": 0, "mutated": True, "uncertain": False}
        outcome = self.settle(budget=budget, write_journal=_Writer(), wake=wake)
        self.assertEqual(wake.calls, [(WS, "15855")])
        self.assertEqual(outcome.woke, (pending.idempotency_key,))
        self.assertEqual(self.store.open_pending(WS), ())

    def test_a_raising_wake_leaves_the_journal_recorded(self) -> None:
        self.fire()
        self.settle(write_journal=_Writer(journal_ids=["110130"]), wake=_Wake(raises=True))
        (pending,) = self.store.unwoken_pending(WS)
        self.assertEqual(pending.journal_id, "110130")


class AnchorTest(SettleBase):
    def test_a_firing_with_no_issue_anchor_is_never_written(self) -> None:
        # j#110121-5 forbids guessing an issue: it stays local and visible instead.
        self.fire(issue="")
        writer = _Writer(journal_ids=["110130"])
        outcome = self.settle(write_journal=writer, wake=_Wake())
        self.assertEqual(outcome.reason, SETTLE_ANCHOR_UNRESOLVED)
        self.assertEqual(writer.calls, [])
        self.assertEqual(outcome.anchorless, 1)
        self.assertEqual(len(self.store.unrecorded_pending(WS)), 1)

    def test_an_anchorless_firing_does_not_block_an_anchored_one(self) -> None:
        self.fire(role="claude", issue="")
        self.fire(role="codex", issue="15855")
        writer = _Writer(journal_ids=["110130"])
        outcome = self.settle(write_journal=writer, wake=_Wake())
        self.assertEqual(outcome.reason, SETTLE_RECORDED)
        self.assertEqual(writer.calls[0].role, "codex")
        self.assertEqual(outcome.anchorless, 1)

    def test_an_anchorless_firing_does_not_inflate_its_attempt_count(self) -> None:
        self.fire(issue="")
        for _ in range(4):
            self.settle(write_journal=_Writer(journal_ids=["1"]), wake=_Wake())
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.attempts, 0)


class SettleRefusalTest(SettleBase):
    def test_an_empty_queue_is_a_clean_no_op(self) -> None:
        outcome = self.settle(write_journal=_Writer(), wake=_Wake())
        self.assertEqual(outcome.reason, SETTLE_NOTHING_PENDING)
        self.assertEqual(outcome.unrecorded, 0)

    def test_a_blank_workspace_is_refused(self) -> None:
        outcome = settle_pending_escalations(
            workspace_id="", store=self.store, now=self.clock
        )
        self.assertTrue(outcome.errors)
        self.assertFalse(outcome.spent_budget)

    def test_no_writer_wired_reports_a_refusal_rather_than_silence(self) -> None:
        self.fire()
        outcome = self.settle(wake=_Wake())
        self.assertEqual(outcome.reason, SETTLE_WRITE_REFUSED)
        self.assertEqual(outcome.unrecorded, 1)


class SettleTelemetryTest(SettleBase):
    def test_settle_telemetry_carries_tokens_only(self) -> None:
        self.fire()
        payload = self.settle(
            write_journal=_Writer(journal_ids=["110130"]), wake=_Wake()
        ).telemetry()
        self.assertEqual(payload["settle_reason"], SETTLE_RECORDED)
        self.assertTrue(payload["spent_budget"])
        self.assertEqual(payload["recorded"]["journal_id"], "110130")
        self.assertEqual(payload["recorded"]["stall_class"], CLASS_CONTENT_REFUSAL)

    def test_observe_telemetry_carries_tokens_only(self) -> None:
        units = [_unit(CLASS_CONTENT_REFUSAL, matched_id="sig-7")]
        self.observe(units)
        payload = self.observe(units).telemetry()
        self.assertEqual(payload["escalated"], 1)
        (fired,) = payload["fired"]
        self.assertEqual(fired["stall_class"], CLASS_CONTENT_REFUSAL)
        self.assertEqual(fired["matched_id"], "sig-7")
        self.assertFalse(fired["recorded"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
