"""Tests for the ticketless no-anchor callback payload (Redmine #12703).

GK3500 smoke #12698 surfaced that a ticketless consultation hands-off result
(``no_dispatch`` / ``consultation_result`` / ``blocked`` / ``anchor_required``)
could not be returned to the caller lane: the standard ``handoff reply`` rail
requires a Redmine anchor and failed closed with ``invalid_anchor``. The fix
carries a structured, fail-closed ticketless callback payload over the standard
delivery rail WITHOUT a Redmine anchor and without fabricating one, while keeping
the child -> grandchild worker-dispatch anchor requirement intact.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    SOURCE_TICKETLESS,
    TicketlessAnchor,
    build_delivery_record,
    build_marker,
    build_notification_body,
    make_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_callback import (
    ANCHOR_REQUIRED_DISPATCH_DECISIONS,
    CALLBACK_REASONS,
    CLASSIFICATIONS,
    CLASSIFICATION_ANCHOR_REQUIRED,
    CLASSIFICATION_NO_DISPATCH,
    DISPATCH_ANCHOR_REQUIRED_BEFORE_WORKER,
    DISPATCH_HAND_BACK_TO_CALLER,
    NEXT_ACTION_OWNERS,
    READ_CONTRACT_TOKENS,
    TICKETLESS_DISPATCH_DECISIONS,
    TicketlessCallback,
    TicketlessCallbackError,
    ticketless_callback_from_payload,
)


def _callback(**overrides) -> TicketlessCallback:
    fields = dict(
        classification=CLASSIFICATION_NO_DISPATCH,
        dispatch_decision=DISPATCH_HAND_BACK_TO_CALLER,
        next_action_owner="caller",
        callback_reason="no_dispatch_decided",
        read_contract="grandparent_coordinator",
    )
    fields.update(overrides)
    return TicketlessCallback(**fields)


class TicketlessCallbackConstructionTest(unittest.TestCase):
    def test_builds_and_derives_no_anchor_for_no_dispatch(self) -> None:
        cb = _callback()
        self.assertFalse(cb.redmine_anchor_required)
        self.assertEqual(
            cb.to_structured_dict(),
            {
                "classification": "no_dispatch",
                "dispatch_decision": "hand_back_to_caller",
                "next_action_owner": "caller",
                "callback_reason": "no_dispatch_decided",
                "read_contract": "grandparent_coordinator",
                "redmine_anchor_required": False,
            },
        )

    def test_anchor_required_classification_derives_true(self) -> None:
        cb = _callback(
            classification=CLASSIFICATION_ANCHOR_REQUIRED,
            dispatch_decision=DISPATCH_ANCHOR_REQUIRED_BEFORE_WORKER,
            callback_reason="anchor_required_for_worker_dispatch",
            read_contract="project_gateway",
        )
        self.assertTrue(cb.redmine_anchor_required)

    def test_anchor_required_dispatch_derives_true_even_for_other_class(self) -> None:
        cb = _callback(
            classification="consultation_result",
            dispatch_decision=DISPATCH_ANCHOR_REQUIRED_BEFORE_WORKER,
            callback_reason="anchor_required_for_worker_dispatch",
        )
        self.assertTrue(cb.redmine_anchor_required)

    def test_explicit_matching_anchor_required_accepted(self) -> None:
        cb = _callback(redmine_anchor_required=False)
        self.assertFalse(cb.redmine_anchor_required)

    def test_explicit_contradicting_anchor_required_fails_closed(self) -> None:
        with self.assertRaises(TicketlessCallbackError):
            _callback(redmine_anchor_required=True)

    def test_unknown_classification_fails_closed(self) -> None:
        with self.assertRaises(TicketlessCallbackError):
            _callback(classification="implementation")

    def test_unknown_owner_reason_contract_fail_closed(self) -> None:
        with self.assertRaises(TicketlessCallbackError):
            _callback(next_action_owner="nobody")
        with self.assertRaises(TicketlessCallbackError):
            _callback(callback_reason="because")
        with self.assertRaises(TicketlessCallbackError):
            _callback(read_contract="some_other_role")

    def test_blank_token_fails_closed(self) -> None:
        with self.assertRaises(TicketlessCallbackError):
            _callback(classification="   ")


class TicketlessWorkerDispatchBoundaryTest(unittest.TestCase):
    """The child -> grandchild worker dispatch anchor requirement is not relaxed."""

    def test_every_anchored_worker_dispatch_is_refused(self) -> None:
        for decision in ANCHOR_REQUIRED_DISPATCH_DECISIONS:
            with self.assertRaises(TicketlessCallbackError) as ctx:
                _callback(dispatch_decision=decision)
            # The error must point the caller at the anchored rail.
            self.assertIn("handoff send", str(ctx.exception))

    def test_anchored_dispatch_disjoint_from_ticketless_dispatch(self) -> None:
        self.assertEqual(
            set(ANCHOR_REQUIRED_DISPATCH_DECISIONS) & set(TICKETLESS_DISPATCH_DECISIONS),
            set(),
        )

    def test_unknown_dispatch_decision_fails_closed(self) -> None:
        with self.assertRaises(TicketlessCallbackError):
            _callback(dispatch_decision="ship_it")


class TicketlessCallbackRoundTripTest(unittest.TestCase):
    def test_roundtrips_through_payload(self) -> None:
        for classification in CLASSIFICATIONS:
            for dispatch in TICKETLESS_DISPATCH_DECISIONS:
                cb = _callback(classification=classification, dispatch_decision=dispatch)
                rebuilt = ticketless_callback_from_payload(cb.to_structured_dict())
                self.assertEqual(cb, rebuilt)

    def test_missing_field_fails_closed(self) -> None:
        payload = _callback().to_structured_dict()
        del payload["classification"]
        with self.assertRaises(TicketlessCallbackError):
            ticketless_callback_from_payload(payload)

    def test_tampered_anchor_required_fails_closed_on_rebuild(self) -> None:
        payload = _callback().to_structured_dict()
        payload["redmine_anchor_required"] = True  # incoherent with no_dispatch
        with self.assertRaises(TicketlessCallbackError):
            ticketless_callback_from_payload(payload)

    def test_choice_sets_are_disjoint_and_nonempty(self) -> None:
        for choices in (CLASSIFICATIONS, NEXT_ACTION_OWNERS, CALLBACK_REASONS,
                        READ_CONTRACT_TOKENS, TICKETLESS_DISPATCH_DECISIONS):
            self.assertTrue(choices)
            self.assertEqual(len(choices), len(set(choices)))


class TicketlessCallbackRenderTest(unittest.TestCase):
    def test_pointer_clause_is_single_line_and_states_no_anchor(self) -> None:
        clause = _callback().pointer_clause()
        self.assertNotIn("\n", clause)
        self.assertIn("no Redmine anchor was fabricated", clause)
        self.assertIn("no_dispatch", clause)

    def test_record_lines_carry_every_field(self) -> None:
        lines = "\n".join(_callback().record_lines())
        self.assertIn("classification `no_dispatch`", lines)
        self.assertIn("dispatch `hand_back_to_caller`", lines)
        self.assertIn("Redmine anchor required (next worker phase): `false`", lines)
        self.assertIn("Workflow next-action owner: `caller`", lines)
        self.assertIn("Callback reason: `no_dispatch_decided`", lines)
        self.assertIn("Read contract: `grandparent_coordinator`", lines)


class TicketlessAnchorRailTest(unittest.TestCase):
    """The TicketlessAnchor carries the marker/body/outcome rail without a ticket."""

    def test_anchor_source_and_marker(self) -> None:
        anchor = TicketlessAnchor(
            classification="no_dispatch", dispatch_decision="hand_back_to_caller"
        )
        self.assertEqual(anchor.source, SOURCE_TICKETLESS)
        marker = build_marker(anchor, "reply", "codex")
        self.assertEqual(
            marker,
            "[mozyo:handoff:source=ticketless:classification=no_dispatch:"
            "dispatch=hand_back_to_caller:kind=reply:to=codex]",
        )

    def test_notification_body_uses_ticketless_lead_not_ticket_read(self) -> None:
        cb = _callback()
        anchor = TicketlessAnchor(
            classification=cb.classification, dispatch_decision=cb.dispatch_decision
        )
        body = build_notification_body(
            anchor, "reply", "no implementation needed", "codex", ticketless_callback=cb
        )
        self.assertNotIn("read it from the source-of-truth system", body)
        self.assertIn("ticketless no-anchor callback", body)
        self.assertIn(cb.pointer_clause(), body)

    def test_make_outcome_records_callback_distinctly_from_transport(self) -> None:
        cb = _callback()
        anchor = TicketlessAnchor(
            classification=cb.classification, dispatch_decision=cb.dispatch_decision
        )
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%0",
            anchor=anchor,
            mode="standard",
            kind="reply",
            notification_marker=build_marker(anchor, "reply", "codex"),
            ticketless_callback=cb,
        )
        # Transport outcome
        self.assertEqual(outcome.status, "sent")
        self.assertEqual(outcome.source, SOURCE_TICKETLESS)
        # Workflow result is a distinct field
        self.assertEqual(outcome.ticketless_callback, cb.to_structured_dict())
        # No Redmine issue/journal anchor was fabricated
        self.assertNotIn("issue", outcome.anchor)
        self.assertNotIn("journal", outcome.anchor)

    def test_delivery_record_renders_ticketless_block(self) -> None:
        cb = _callback()
        anchor = TicketlessAnchor(
            classification=cb.classification, dispatch_decision=cb.dispatch_decision
        )
        outcome = make_outcome(
            status="sent", reason="ok", receiver="codex", target="%0", anchor=anchor,
            mode="standard", kind="reply",
            notification_marker=build_marker(anchor, "reply", "codex"),
            ticketless_callback=cb,
        )
        record = build_delivery_record(outcome)
        self.assertIn("Ticketless callback: classification `no_dispatch`", record)
        self.assertIn("ticketless (no Redmine anchor", record)

    def test_delivery_record_omits_ticketless_block_for_anchored_outcome(self) -> None:
        # An anchored send must still render a single `—` ticketless line.
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            RedmineAnchor,
        )

        outcome = make_outcome(
            status="sent", reason="ok", receiver="codex", target="%0",
            anchor=RedmineAnchor(issue="9020", journal="46005"), mode="standard",
            kind="reply", notification_marker="m",
        )
        self.assertIsNone(outcome.ticketless_callback)
        self.assertIn("- Ticketless callback: —", build_delivery_record(outcome))


if __name__ == "__main__":
    unittest.main()
