"""#14687 R1-F1: the worker-progress read fails closed toward "progress". (Redmine j#93273)

``notes_carry_worker_progress`` answers "did this worker deliver a gate after the anchor". The
answer reaches an EFFECT: ``False`` becomes ``expected_gate_absent`` on the turn observation,
which is the only route to ``turn_failed_no_durable_gate`` — the single class that admits the
guarded worker refresh, a destructive close.

Read through the lenient fold it took the unsafe direction. With every other axis held at its
refresh-admitting value, a note declaring ``implementation_done`` twice-over classified as
``turn_failed_no_durable_gate``: last-write-wins erased the first declaration, so the reader
positively asserted the gate was ABSENT and admitted closing the worker that had delivered it.
Whether that happened depended on which occurrence came last.

These pin the EFFECT, not just the boolean. A test asserting only ``notes_carry_worker_progress``
would have gone green on a fix that flipped the flag while leaving the classification reachable
some other way, and the whole point of the finding is what the flag turns into.

The negative control is load-bearing in the other direction: "return True always" closes the hole
and also disables the surface, so the genuine no-progress cases must still admit.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh import (  # noqa: E501
    WorkerRefreshRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh_durable_read import (  # noqa: E501
    notes_carry_worker_progress,
    worker_progress_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    TURN_CLASS_FAILED,
    TURN_CLASS_PRODUCTIVE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_turn_recovery import (  # noqa: E501
    WORKER_PROGRESS_GATES,
    WorkerTurnObservation,
    classify_worker_turn,
)

LANE = "r1"
GENERATION = "1"
ANCHOR = "10"
#: Strictly after :data:`ANCHOR`, so the entry is in scope for the ordered re-read.
AFTER_ANCHOR = "11"


def _request() -> WorkerRefreshRequest:
    return WorkerRefreshRequest(
        issue="1", lane=LANE, role="implementation", provider="claude",
        assigned_name="w", locator="w:1.1", journal="1", action_id="a",
        action_generation=GENERATION, worker_revision="1", lane_revision="1",
        lane_generation=GENERATION, anchor_issue="1", resume_anchor_journal=ANCHOR,
        resume_gate="implementation_request", reason_token="t",
    )


class _Entry:
    def __init__(self, journal_id: str, notes: str) -> None:
        self.journal_id = journal_id
        self.notes = notes


def _classify(notes: str) -> str:
    """The turn class this note produces, every OTHER axis held refresh-admitting. (pure)

    Delivery confirmed, turn started, runtime settled, source fresh, identity fully bound — so
    the classification turns on the durable-progress facts alone. Anything that is not
    ``turn_failed_no_durable_gate`` refuses the refresh.
    """
    landed, absent, fresh = worker_progress_facts(
        _request(),
        journal_reader=lambda _issue: [_Entry(AFTER_ANCHOR, notes)],
        journal_reader_fresh=True,
    )
    return classify_worker_turn(
        WorkerTurnObservation(
            delivery_confirmed=True, turn_started=True, settled_turn_ended=True,
            expected_gate_landed=landed, expected_gate_absent=absent,
            durable_source_fresh=fresh, reason_token="t", anchor_bound=True,
            lane_generation_bound=True, participant_revision_bound=True,
        )
    )


class RepeatedGateKeyTests(unittest.TestCase):
    """R1-F1: a repeated ``gate`` must not decide, in EITHER order."""

    #: The regression itself. Both orders are pinned because only one of them was broken —
    #: the fold kept whichever declaration came last, so testing one order would have passed
    #: against the very reader that admitted the close.
    ORDERS = (
        ("real gate first", "[mozyo:workflow-event:gate=implementation_done:gate=zzz]"),
        ("real gate last", "[mozyo:workflow-event:gate=zzz:gate=implementation_done]"),
    )

    def test_a_repeated_gate_key_never_admits_the_refresh(self):
        for label, note in self.ORDERS:
            with self.subTest(label):
                self.assertNotEqual(
                    _classify(note),
                    TURN_CLASS_FAILED,
                    "a note declaring a worker-progress gate must never classify as the one "
                    "turn class that admits the destructive refresh",
                )

    def test_a_repeated_gate_key_reads_as_progress_in_either_order(self):
        for label, note in self.ORDERS:
            with self.subTest(label):
                self.assertTrue(notes_carry_worker_progress(_request(), note))

    def test_the_two_orders_agree(self):
        """The safety of a note must not depend on which occurrence came last."""
        classes = {_classify(note) for _label, note in self.ORDERS}
        self.assertEqual(len(classes), 1, f"order-dependent classification: {classes}")


class ProducerImpossibleBodyTests(unittest.TestCase):
    """Every body no canonical producer could render counts as progress (unknown provenance)."""

    CASES = (
        ("whitespace around the value", "[mozyo:workflow-event:gate = implementation_done]"),
        ("empty component", "[mozyo:workflow-event:gate=implementation_done::x=1]"),
        ("empty key", "[mozyo:workflow-event:=implementation_done]"),
        ("fragment with no '='", "[mozyo:workflow-event:gate=implementation_done:bare]"),
        ("two gate aliases", "[mozyo:workflow-event:gate=implementation_done:kind=zzz]"),
    )

    def test_an_unrenderable_body_never_admits_the_refresh(self):
        for label, note in self.CASES:
            with self.subTest(label):
                self.assertNotEqual(_classify(note), TURN_CLASS_FAILED)

    def test_a_clean_progress_gate_is_productive(self):
        self.assertEqual(
            _classify("[mozyo:workflow-event:gate=implementation_done]"),
            TURN_CLASS_PRODUCTIVE,
        )

    def test_an_unreadable_sibling_poisons_the_whole_note(self):
        """The refusal unit is the NOTE, not the marker (the contract's own rule).

        Dropping the unreadable marker and matching on the rest is what makes a note carrying one
        clean and one forged marker read exactly like a clean note.
        """
        note = (
            "[mozyo:workflow-event:gate=review_result]\n"
            "[mozyo:workflow-event:gate = forged]"
        )
        self.assertTrue(notes_carry_worker_progress(_request(), note))
        self.assertNotEqual(_classify(note), TURN_CLASS_FAILED)

    def test_every_worker_progress_gate_is_covered_in_both_orders(self):
        """Derived from the vocabulary, not re-listed — a gate added upstream is pinned here."""
        for gate in sorted(WORKER_PROGRESS_GATES):
            for label, template in (
                ("first", f"[mozyo:workflow-event:gate={gate}:gate=zzz]"),
                ("last", f"[mozyo:workflow-event:gate=zzz:gate={gate}]"),
            ):
                with self.subTest(gate=gate, order=label):
                    self.assertNotEqual(_classify(template), TURN_CLASS_FAILED)


class SurfaceStillWorksTests(unittest.TestCase):
    """The negative control: failing closed must not disable the refresh surface.

    ``return True`` closes the finding and makes the guarded refresh unreachable for every input.
    These are the cases where the worker genuinely delivered nothing, so the refresh MUST still be
    admitted — otherwise the fix is a silent feature removal.
    """

    CASES = (
        ("no marker at all", "the worker wrote prose and no gate"),
        ("empty note", ""),
        ("a non-progress gate", "[mozyo:workflow-event:gate=review_result]"),
        (
            "another lane's enveloped gate",
            "[mozyo:workflow-event:gate=implementation_done:lane=OTHER:lane_generation=1]",
        ),
        (
            "a superseded generation's enveloped gate",
            "[mozyo:workflow-event:gate=implementation_done:lane=r1:lane_generation=9]",
        ),
    )

    def test_a_genuinely_absent_gate_still_admits_the_refresh(self):
        for label, note in self.CASES:
            with self.subTest(label):
                self.assertEqual(
                    _classify(note),
                    TURN_CLASS_FAILED,
                    "failing closed toward progress must not make the guarded refresh "
                    "unreachable; this worker delivered nothing",
                )

    def test_this_lane_s_enveloped_gate_is_progress(self):
        note = (
            f"[mozyo:workflow-event:gate=implementation_done:lane={LANE}"
            f":lane_generation={GENERATION}]"
        )
        self.assertEqual(_classify(note), TURN_CLASS_PRODUCTIVE)

    def test_a_gate_at_or_before_the_anchor_is_not_this_turn_s_progress(self):
        """Ordering is unchanged by the strict read: only entries AFTER the anchor count."""
        landed, absent, fresh = worker_progress_facts(
            _request(),
            journal_reader=lambda _issue: [
                _Entry(ANCHOR, "[mozyo:workflow-event:gate=implementation_done]")
            ],
            journal_reader_fresh=True,
        )
        self.assertEqual((landed, absent, fresh), (False, True, True))

    def test_an_unobservable_source_is_never_absence(self):
        """A missing / snapshot reader leaves every fact False — unobservable, not absent."""
        for label, kwargs in (
            ("no reader", {"journal_reader": None, "journal_reader_fresh": True}),
            (
                "snapshot reader",
                {
                    "journal_reader": lambda _i: [_Entry(AFTER_ANCHOR, "")],
                    "journal_reader_fresh": False,
                },
            ),
        ):
            with self.subTest(label):
                self.assertEqual(worker_progress_facts(_request(), **kwargs), (False, False, False))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
