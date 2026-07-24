"""Issuer POLICY binding (Redmine #14219 T2c Fork A, ruling j#86718).

The ruling's own wiring-test pins, plus the fail-closed edges: the binding answers "which role
is CONTRACTED to write this gate kind" from the canonical gate structure and the committed
policy anchor — never "who actually typed it".
"""

from __future__ import annotations

import inspect
import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    ISSUER_UNKNOWN,
    check_issuer_resolution,
    contract_writer_role,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy import (  # noqa: E501
    POLICY_RULING_POINTER,
    config_policy_pointer,
    resolve_journal_issuer,
)

POINTER = config_policy_pointer("d" * 40)
WS, LANE = "wsPolicy", "lane_policy_1"
HEAD = "a" * 40

PARK = f"[mozyo:workflow-event:gate=park_declared:workspace={WS}:lane={LANE}:lane_generation=3]"
REVIEW = (
    f"[mozyo:workflow-event:gate=review_result:conclusion=approved:head={HEAD}:req=10"
    f":workspace={WS}:lane={LANE}:lane_generation=3]"
)
CI = (
    f"[mozyo:workflow-event:gate=required_ci_green:workspace={WS}:lane={LANE}"
    f":lane_generation=3:head={HEAD}:workflow=tests:run=5:conclusion=success]"
)


class PolicyNotAuthenticationTest(unittest.TestCase):
    def test_resolution_takes_no_author_metadata_at_all(self):
        # Ruling pin: issuer policy binding != identity authentication. With one shared Redmine
        # account, author-derived confidence would be theater — so the resolver's SIGNATURE has
        # no author parameter, structurally.
        parameters = inspect.signature(resolve_journal_issuer).parameters
        self.assertEqual(set(parameters), {"journal_id", "notes", "policy_pointer"})

    def test_identical_structure_resolves_identically(self):
        # The same record structure resolves to the same role no matter who posted it — the
        # honest limit of a single-author workspace, stated instead of faked.
        first = resolve_journal_issuer("1", PARK, policy_pointer=POINTER)
        second = resolve_journal_issuer("1", PARK, policy_pointer=POINTER)
        self.assertEqual(first, second)
        self.assertEqual(first.role, "lane_worker")

    def test_the_docstrings_state_the_policy_limit(self):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_issuer_policy as module  # noqa: E501

        self.assertIn("NOT identity authentication", module.__doc__)
        self.assertIn("policy, not authentication", resolve_journal_issuer.__doc__)


class GateStructureResolutionTest(unittest.TestCase):
    def test_lane_scoped_gates_bind_the_evidences_own_envelope(self):
        issuer = resolve_journal_issuer("100", PARK, policy_pointer=POINTER)
        self.assertEqual(issuer.role, "lane_worker")
        self.assertEqual((issuer.workspace, issuer.lane, issuer.lane_generation), (WS, LANE, 3))
        self.assertIsNone(check_issuer_resolution("park_declared", issuer))

        gateway = resolve_journal_issuer("101", REVIEW, policy_pointer=POINTER)
        self.assertEqual(gateway.role, "review_gateway")
        self.assertEqual((gateway.workspace, gateway.lane), (WS, LANE))

    def test_workspace_scoped_gates_bind_without_a_lane(self):
        issuer = resolve_journal_issuer("102", CI, policy_pointer=POINTER)
        self.assertEqual(issuer.role, "coordinator")
        self.assertEqual(issuer.lane, "")
        self.assertTrue(issuer.is_anchored)
        self.assertIsNone(check_issuer_resolution("required_ci_green", issuer))

    def test_the_anchor_names_the_ruling_the_config_blob_and_the_evidence(self):
        issuer = resolve_journal_issuer("100", PARK, policy_pointer=POINTER)
        self.assertIn(POLICY_RULING_POINTER, issuer.authority_anchor)
        self.assertIn(POINTER, issuer.authority_anchor)
        self.assertIn("j#100", issuer.authority_anchor)
        self.assertIn("gate=park_declared", issuer.authority_anchor)
        self.assertIn(f"lane={LANE}", issuer.authority_anchor)

    def test_the_role_mapping_is_the_producers_own(self):
        # Single authority: the mapping comes from contract_writer_role, not a second table.
        self.assertEqual(contract_writer_role("park_declared"), "lane_worker")
        self.assertEqual(contract_writer_role("review_result"), "review_gateway")
        self.assertEqual(contract_writer_role("required_ci_green"), "coordinator")
        self.assertEqual(contract_writer_role("progress_log"), ISSUER_UNKNOWN)


class FailClosedEdgesTest(unittest.TestCase):
    def test_no_authority_gate_resolves_unknown(self):
        for notes in ("plain prose", "[mozyo:workflow-event:gate=progress_log]", ""):
            with self.subTest(notes=notes[:30]):
                issuer = resolve_journal_issuer("103", notes, policy_pointer=POINTER)
                self.assertEqual(issuer.role, ISSUER_UNKNOWN)
                self.assertFalse(issuer.is_anchored)

    def test_two_different_authority_gates_prove_neither(self):
        issuer = resolve_journal_issuer("104", PARK + "\n" + CI, policy_pointer=POINTER)
        self.assertEqual(issuer.role, ISSUER_UNKNOWN)

    def test_a_missing_policy_pointer_binds_nothing(self):
        issuer = resolve_journal_issuer("105", PARK, policy_pointer="")
        self.assertEqual(issuer.role, ISSUER_UNKNOWN)
        self.assertFalse(issuer.is_anchored)

    def test_a_malformed_lane_envelope_resolves_unbound(self):
        malformed = f"[mozyo:workflow-event:gate=park_declared:workspace={WS}:lane_generation=3]"
        issuer = resolve_journal_issuer("106", malformed, policy_pointer=POINTER)
        self.assertEqual(issuer.role, ISSUER_UNKNOWN)

    def test_same_gate_with_conflicting_envelopes_resolves_unbound(self):
        other = PARK.replace("lane_generation=3", "lane_generation=4")
        issuer = resolve_journal_issuer("107", PARK + "\n" + other, policy_pointer=POINTER)
        self.assertEqual(issuer.role, ISSUER_UNKNOWN)

    def test_identical_duplicate_markers_collapse(self):
        issuer = resolve_journal_issuer("108", PARK + "\n" + PARK, policy_pointer=POINTER)
        self.assertEqual(issuer.role, "lane_worker")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
