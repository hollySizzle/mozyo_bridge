"""Tests for the forward ticketless no-anchor consultation payload (Redmine #12740).

The GK3500 rerun (after `#12739 Cockpit must allow root and project-scoped units
to coexist`) hit the mirror of the `#12703` gap on the *forward* leg: the
department-root coordinator found exactly one project gateway but had no
product-standard no-anchor primitive to hand the consultation *to* it
(``handoff send --source redmine`` failed closed with ``invalid_anchor``). The fix
carries a structured, fail-closed forward consultation payload over the standard
delivery rail WITHOUT a Redmine anchor and without fabricating one, while keeping
the worker-dispatch / implementation / domain-probe Redmine-anchor gate intact and
naming the callback return contract so the gateway can return a structured result.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    SOURCE_TICKETLESS,
    TicketlessConsultationAnchor,
    build_delivery_record,
    build_marker,
    build_notification_body,
    make_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
    CALLBACK_METHODS,
    CALLBACK_TO_ROLES,
    CALLBACK_VIA_Q_ENTER_CONSULTATION_CALLBACK,
    CALLBACK_VIA_TICKETLESS_CALLBACK,
    CONSULTATION_KINDS,
    CONSULTATION_PROJECT_DOMAIN,
    READ_CONTRACT_TOKENS,
    WORKER_DISPATCH_REQUIRES_ANCHOR,
    TicketlessConsultation,
    TicketlessConsultationError,
    ticketless_consultation_from_payload,
)


def _consultation(**overrides) -> TicketlessConsultation:
    fields = dict(
        consultation_kind=CONSULTATION_PROJECT_DOMAIN,
        callback_to_role="grandparent_coordinator",
        callback_methods=list(CALLBACK_METHODS),
        read_contract="project_gateway",
    )
    fields.update(overrides)
    return TicketlessConsultation(**fields)


class TicketlessConsultationConstructionTest(unittest.TestCase):
    def test_builds_and_carries_worker_dispatch_invariant(self) -> None:
        c = _consultation()
        self.assertTrue(c.worker_dispatch_requires_anchor)
        self.assertEqual(
            c.to_structured_dict(),
            {
                "consultation_kind": "project_domain_consultation",
                "callback_to_role": "grandparent_coordinator",
                "callback_methods": [
                    "ticketless_callback",
                    "q_enter_consultation_callback",
                ],
                "read_contract": "project_gateway",
                "worker_dispatch_requires_anchor": True,
            },
        )

    def test_single_callback_method_accepted(self) -> None:
        c = _consultation(callback_methods=[CALLBACK_VIA_TICKETLESS_CALLBACK])
        self.assertEqual(c.callback_methods, (CALLBACK_VIA_TICKETLESS_CALLBACK,))

    def test_callback_methods_dedupe_preserving_order(self) -> None:
        c = _consultation(
            callback_methods=[
                CALLBACK_VIA_Q_ENTER_CONSULTATION_CALLBACK,
                CALLBACK_VIA_TICKETLESS_CALLBACK,
                CALLBACK_VIA_Q_ENTER_CONSULTATION_CALLBACK,
            ]
        )
        self.assertEqual(
            c.callback_methods,
            (
                CALLBACK_VIA_Q_ENTER_CONSULTATION_CALLBACK,
                CALLBACK_VIA_TICKETLESS_CALLBACK,
            ),
        )

    def test_empty_callback_methods_fails_closed(self) -> None:
        with self.assertRaises(TicketlessConsultationError):
            _consultation(callback_methods=[])

    def test_string_callback_methods_fails_closed(self) -> None:
        # A bare string is a sequence of characters; it must be rejected, not
        # iterated into single-char "methods".
        with self.assertRaises(TicketlessConsultationError):
            _consultation(callback_methods="ticketless_callback")

    def test_unknown_callback_method_fails_closed(self) -> None:
        with self.assertRaises(TicketlessConsultationError):
            _consultation(callback_methods=["smoke_signal"])

    def test_unknown_consultation_kind_fails_closed(self) -> None:
        with self.assertRaises(TicketlessConsultationError):
            _consultation(consultation_kind="dispatch_worker")

    def test_unknown_callback_role_or_contract_fails_closed(self) -> None:
        with self.assertRaises(TicketlessConsultationError):
            _consultation(callback_to_role="implementation_worker")
        with self.assertRaises(TicketlessConsultationError):
            _consultation(read_contract="some_other_role")

    def test_blank_token_fails_closed(self) -> None:
        with self.assertRaises(TicketlessConsultationError):
            _consultation(consultation_kind="   ")


class TicketlessConsultationWorkerDispatchBoundaryTest(unittest.TestCase):
    """The worker-dispatch Redmine-anchor gate is not relaxed by this forward rail."""

    def test_worker_dispatch_invariant_is_fixed_true(self) -> None:
        self.assertTrue(WORKER_DISPATCH_REQUIRES_ANCHOR)
        self.assertTrue(_consultation().to_structured_dict()["worker_dispatch_requires_anchor"])

    def test_no_dispatch_token_is_expressible(self) -> None:
        # The consultation kinds are consultation-phase classes only; no worker
        # dispatch token exists in the choice set.
        for kind in CONSULTATION_KINDS:
            self.assertNotIn("dispatch", kind)


class TicketlessConsultationRoundTripTest(unittest.TestCase):
    def test_roundtrips_through_payload(self) -> None:
        for kind in CONSULTATION_KINDS:
            for role in CALLBACK_TO_ROLES:
                for contract in READ_CONTRACT_TOKENS:
                    c = _consultation(
                        consultation_kind=kind,
                        callback_to_role=role,
                        read_contract=contract,
                    )
                    rebuilt = ticketless_consultation_from_payload(
                        c.to_structured_dict()
                    )
                    self.assertEqual(c, rebuilt)

    def test_missing_field_fails_closed(self) -> None:
        payload = _consultation().to_structured_dict()
        del payload["consultation_kind"]
        with self.assertRaises(TicketlessConsultationError):
            ticketless_consultation_from_payload(payload)

    def test_choice_sets_are_disjoint_and_nonempty(self) -> None:
        for choices in (
            CONSULTATION_KINDS,
            CALLBACK_METHODS,
            CALLBACK_TO_ROLES,
            READ_CONTRACT_TOKENS,
        ):
            self.assertTrue(choices)
            self.assertEqual(len(choices), len(set(choices)))


class TicketlessConsultationRenderTest(unittest.TestCase):
    def test_pointer_clause_is_single_line_and_states_no_anchor(self) -> None:
        clause = _consultation().pointer_clause()
        self.assertNotIn("\n", clause)
        self.assertIn("no Redmine anchor was fabricated", clause)
        self.assertIn("project_domain_consultation", clause)
        # The worker-dispatch anchor rule is restated for the receiver.
        self.assertIn("requires a Redmine anchor", clause)

    def test_record_lines_carry_every_field(self) -> None:
        lines = "\n".join(_consultation().record_lines())
        self.assertIn("kind `project_domain_consultation`", lines)
        self.assertIn("Return result to role: `grandparent_coordinator`", lines)
        self.assertIn("`ticketless_callback`", lines)
        self.assertIn("`q_enter_consultation_callback`", lines)
        self.assertIn("Read contract: `project_gateway`", lines)
        self.assertIn("Worker dispatch requires Redmine anchor: `true`", lines)


class TicketlessConsultationAnchorRailTest(unittest.TestCase):
    """The forward anchor carries the marker/body/outcome rail without a ticket."""

    def test_anchor_source_and_marker(self) -> None:
        anchor = TicketlessConsultationAnchor(
            consultation_kind=CONSULTATION_PROJECT_DOMAIN,
            callback_to_role="grandparent_coordinator",
        )
        self.assertEqual(anchor.source, SOURCE_TICKETLESS)
        marker = build_marker(anchor, "design_consultation", "codex")
        self.assertEqual(
            marker,
            "[mozyo:handoff:source=ticketless:"
            "consultation=project_domain_consultation:"
            "callback_to=grandparent_coordinator:kind=design_consultation:to=codex]",
        )

    def test_notification_body_uses_consultation_lead_not_ticket_read(self) -> None:
        c = _consultation()
        anchor = TicketlessConsultationAnchor(
            consultation_kind=c.consultation_kind,
            callback_to_role=c.callback_to_role,
        )
        body = build_notification_body(
            anchor,
            "design_consultation",
            "classify this for the project",
            "codex",
            ticketless_consultation=c,
        )
        self.assertNotIn("read it from the source-of-truth system", body)
        self.assertIn("ticketless no-anchor consultation", body)
        # It must NOT mislabel itself as the return callback leg.
        self.assertNotIn("ticketless no-anchor callback", body)
        self.assertIn(c.pointer_clause(), body)

    def test_make_outcome_records_consultation_distinctly_from_transport(self) -> None:
        c = _consultation()
        anchor = TicketlessConsultationAnchor(
            consultation_kind=c.consultation_kind,
            callback_to_role=c.callback_to_role,
        )
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="codex",
            target="%0",
            anchor=anchor,
            mode="standard",
            kind="design_consultation",
            notification_marker=build_marker(anchor, "design_consultation", "codex"),
            ticketless_consultation=c,
        )
        self.assertEqual(outcome.status, "sent")
        self.assertEqual(outcome.source, SOURCE_TICKETLESS)
        self.assertEqual(outcome.ticketless_consultation, c.to_structured_dict())
        # The return-leg field stays None on the forward leg.
        self.assertIsNone(outcome.ticketless_callback)
        # No Redmine issue/journal anchor was fabricated.
        self.assertNotIn("issue", outcome.anchor)
        self.assertNotIn("journal", outcome.anchor)

    def test_delivery_record_renders_consultation_block(self) -> None:
        c = _consultation()
        anchor = TicketlessConsultationAnchor(
            consultation_kind=c.consultation_kind,
            callback_to_role=c.callback_to_role,
        )
        outcome = make_outcome(
            status="sent", reason="ok", receiver="codex", target="%0", anchor=anchor,
            mode="standard", kind="design_consultation",
            notification_marker=build_marker(anchor, "design_consultation", "codex"),
            ticketless_consultation=c,
        )
        record = build_delivery_record(outcome)
        self.assertIn("Ticketless consultation: kind `project_domain_consultation`", record)
        self.assertIn("ticketless (no Redmine anchor — see ticketless consultation below)", record)
        # The callback return block stays a single `—` line on the forward leg.
        self.assertIn("- Ticketless callback: —", record)

    def test_delivery_record_omits_consultation_block_for_anchored_outcome(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            RedmineAnchor,
        )

        outcome = make_outcome(
            status="sent", reason="ok", receiver="codex", target="%0",
            anchor=RedmineAnchor(issue="9020", journal="46005"), mode="standard",
            kind="reply", notification_marker="m",
        )
        self.assertIsNone(outcome.ticketless_consultation)
        self.assertIn("- Ticketless consultation: —", build_delivery_record(outcome))


if __name__ == "__main__":
    unittest.main()
