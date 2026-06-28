"""Forward ticketless no-anchor parent -> child work-intake payload (Redmine #12748).

Pins the pure :class:`TicketlessWorkIntake` envelope the ``project-gateway
child-intake`` rail carries: fixed-token construction + fail-closed validation, the
structured/marker/pointer/record projections, the round-trip, and the three fixed
invariants (worker dispatch stays anchor-gated, the parent must not answer
domain/design, the child owns the anchor decision).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_work_intake import (
    ANCHOR_DECISION_OWNER,
    CALLBACK_METHODS,
    CALLBACK_TO_ROLES,
    READ_CONTRACT_TOKENS,
    ROLE_DELEGATED_COORDINATOR,
    WORK_SHAPE_DOMAIN_DESIGN,
    WORK_SHAPE_IMPLEMENTATION,
    TicketlessWorkIntake,
    TicketlessWorkIntakeError,
    ticketless_work_intake_from_payload,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_PROJECT_GATEWAY,
)


def _intake(**overrides) -> TicketlessWorkIntake:
    base = dict(
        work_shape=WORK_SHAPE_DOMAIN_DESIGN,
        callback_to_role=ROLE_PROJECT_GATEWAY,
        callback_methods=list(CALLBACK_METHODS),
        read_contract=ROLE_DELEGATED_COORDINATOR,
    )
    base.update(overrides)
    return TicketlessWorkIntake(**base)


class WorkIntakeConstructionTest(unittest.TestCase):
    def test_fixed_token_contract(self):
        # The child returns to the parent gateway; the read contract is the child's.
        self.assertEqual(CALLBACK_TO_ROLES, (ROLE_PROJECT_GATEWAY,))
        self.assertEqual(READ_CONTRACT_TOKENS, (ROLE_DELEGATED_COORDINATOR,))
        self.assertEqual(ANCHOR_DECISION_OWNER, ROLE_DELEGATED_COORDINATOR)

    def test_invariants_are_fixed_true(self):
        wi = _intake()
        self.assertTrue(wi.worker_dispatch_requires_anchor)
        self.assertTrue(wi.parent_must_not_answer_domain)
        self.assertTrue(wi.child_owns_anchor_decision)
        self.assertEqual(wi.anchor_decision_owner, ROLE_DELEGATED_COORDINATOR)

    def test_unknown_work_shape_fails_closed(self):
        with self.assertRaises(TicketlessWorkIntakeError):
            _intake(work_shape="worker_dispatch")  # no dispatch token is expressible

    def test_unknown_callback_role_fails_closed(self):
        # The child returns to the parent only; grandparent is not a valid target.
        with self.assertRaises(TicketlessWorkIntakeError):
            _intake(callback_to_role="grandparent_coordinator")

    def test_unknown_read_contract_fails_closed(self):
        with self.assertRaises(TicketlessWorkIntakeError):
            _intake(read_contract=ROLE_PROJECT_GATEWAY)  # the receiver is the child

    def test_empty_callback_methods_fails_closed(self):
        with self.assertRaises(TicketlessWorkIntakeError):
            _intake(callback_methods=[])

    def test_string_callback_methods_rejected(self):
        # A bare string is iterable but is not a valid method *set*.
        with self.assertRaises(TicketlessWorkIntakeError):
            _intake(callback_methods="ticketless_callback")

    def test_unknown_callback_method_fails_closed(self):
        with self.assertRaises(TicketlessWorkIntakeError):
            _intake(callback_methods=["raw_pane_typing"])

    def test_callback_methods_deduped_order_preserving(self):
        wi = _intake(
            callback_methods=[
                CALLBACK_METHODS[1],
                CALLBACK_METHODS[0],
                CALLBACK_METHODS[1],
            ]
        )
        self.assertEqual(
            wi.callback_methods, (CALLBACK_METHODS[1], CALLBACK_METHODS[0])
        )


class WorkIntakeProjectionTest(unittest.TestCase):
    def test_structured_dict_carries_invariants(self):
        payload = _intake().to_structured_dict()
        self.assertEqual(payload["work_shape"], WORK_SHAPE_DOMAIN_DESIGN)
        self.assertEqual(payload["callback_to_role"], ROLE_PROJECT_GATEWAY)
        self.assertEqual(payload["read_contract"], ROLE_DELEGATED_COORDINATOR)
        self.assertEqual(payload["anchor_decision_owner"], ROLE_DELEGATED_COORDINATOR)
        self.assertTrue(payload["worker_dispatch_requires_anchor"])
        self.assertTrue(payload["parent_must_not_answer_domain"])
        self.assertTrue(payload["child_owns_anchor_decision"])

    def test_marker_fields_distinct_from_consultation(self):
        # The marker key is `work_intake`, not the consultation rail's `consultation`.
        marker = dict(_intake().marker_fields())
        self.assertEqual(marker["work_intake"], WORK_SHAPE_DOMAIN_DESIGN)
        self.assertEqual(marker["callback_to"], ROLE_PROJECT_GATEWAY)
        self.assertNotIn("consultation", marker)

    def test_pointer_clause_single_line_and_states_ownership(self):
        clause = _intake().pointer_clause()
        self.assertNotIn("\n", clause)
        self.assertIn("work-intake", clause)
        self.assertIn(ROLE_DELEGATED_COORDINATOR, clause)
        self.assertIn("Redmine anchor", clause)

    def test_record_lines_name_anchor_owner_and_anchor_rule(self):
        lines = _intake().record_lines()
        text = "\n".join(lines)
        self.assertIn(f"`{ANCHOR_DECISION_OWNER}`", text)
        self.assertIn("Parent must not answer domain/design: `true`", text)
        self.assertIn("Worker dispatch requires Redmine anchor: `true`", text)


class WorkIntakeRoundTripTest(unittest.TestCase):
    def test_round_trip(self):
        wi = _intake(work_shape=WORK_SHAPE_IMPLEMENTATION)
        rebuilt = ticketless_work_intake_from_payload(wi.to_structured_dict())
        self.assertEqual(rebuilt, wi)

    def test_missing_field_fails_closed(self):
        payload = _intake().to_structured_dict()
        del payload["read_contract"]
        with self.assertRaises(TicketlessWorkIntakeError):
            ticketless_work_intake_from_payload(payload)

    def test_tampered_invariant_cannot_relax_anchor_gate(self):
        # A payload that tries to smuggle a relaxed anchor gate is re-asserted true
        # by construction (the invariant is a fixed constant, not a carried field).
        payload = _intake().to_structured_dict()
        payload["worker_dispatch_requires_anchor"] = False
        payload["child_owns_anchor_decision"] = False
        rebuilt = ticketless_work_intake_from_payload(payload)
        self.assertTrue(rebuilt.worker_dispatch_requires_anchor)
        self.assertTrue(rebuilt.child_owns_anchor_decision)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
