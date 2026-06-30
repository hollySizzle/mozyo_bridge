"""Risk-based lane admission decision policy tests (Redmine #12921).

Pins the pure per-candidate admission decision that sits beside the #12855 fill /
#12856 Redmine-aware admission policies
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` `### Lane State Classes`
/ `### Admission Rule` / `### Drain Order`):

- a candidate with no concrete risk is admitted (``allow_dispatch``);
- the owner correction (Redmine #12670 j#69283): coordinator-convenience signals
  (callback miss worry / management load / broad bucket) are recorded as rejected
  non-reasons but NEVER move the decision off ``allow_dispatch`` on their own;
- each concrete risk maps to its decision: file/invariant overlap and merge-order
  conflict and a coordinator-owned-queue dependency -> ``serialize``; unresolved design
  / release / credential gate -> ``needs_owner_decision``; a blocked / callback-failed
  (or unreadable) dependency -> ``blocked``;
- decision severity precedence picks the most severe across all fired risks, and every
  fired risk is still listed;
- active lane states are classified from durable-record facts (reusing
  :func:`classify_lane_state`), including the fail-closed unreadable-dependency case.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_admission_risk import (
    ADMISSION_DECISIONS,
    ADMIT_ALLOW_DISPATCH,
    ADMIT_BLOCKED,
    ADMIT_NEEDS_OWNER_DECISION,
    ADMIT_SERIALIZE,
    INVALID_SERIALIZATION_NONREASONS,
    NONREASON_BROAD_BUCKET,
    NONREASON_CALLBACK_MISS_RISK,
    NONREASON_COORDINATOR_MANAGEMENT_LOAD,
    RISK_BLOCKED_OR_CALLBACK_FAILURE,
    RISK_COORDINATOR_OWNED_QUEUE,
    RISK_CREDENTIAL_DESTRUCTIVE_EXTERNAL_GATE,
    RISK_FILE_OVERLAP,
    RISK_INVARIANT_OVERLAP,
    RISK_MERGE_ORDER_CONFLICT,
    RISK_RELEASE_PUBLISH_GATE,
    RISK_UNRESOLVED_DESIGN_DECISION,
    VALID_ADMISSION_RISKS,
    LaneAdmissionInputs,
    evaluate_lane_admission,
    render_lane_admission_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    CALLBACK_DELIVERY_FAILED,
    GATE_BLOCKED,
    GATE_REVIEW_REQUEST,
    GATE_START,
    LaneSignal,
)


def _signal(issue, gate=GATE_START, **kw):
    return LaneSignal(issue=issue, latest_gate=gate, **kw)


class VocabularyIntegrityTest(unittest.TestCase):
    def test_every_valid_risk_maps_to_a_known_decision(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_admission_risk import (
            _RISK_DECISION,
        )

        self.assertEqual(set(_RISK_DECISION), set(VALID_ADMISSION_RISKS))
        for decision in _RISK_DECISION.values():
            self.assertIn(decision, ADMISSION_DECISIONS)
            self.assertNotEqual(decision, ADMIT_ALLOW_DISPATCH)

    def test_nonreasons_are_disjoint_from_valid_risks(self):
        self.assertFalse(INVALID_SERIALIZATION_NONREASONS & VALID_ADMISSION_RISKS)


class AllowDispatchTest(unittest.TestCase):
    def test_no_risk_allows_parallel_dispatch(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                active_lane_signals=(_signal("12639"), _signal("12651")),
            )
        )
        self.assertEqual(out.decision, ADMIT_ALLOW_DISPATCH)
        self.assertTrue(out.should_dispatch)
        self.assertEqual(out.risks, ())
        self.assertEqual(out.rejected_nonreasons, ())

    def test_implementing_active_lanes_are_not_a_stop_reason(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                active_lane_signals=(_signal("12639"), _signal("12651")),
            )
        )
        self.assertEqual(
            {lane.state_class for lane in out.classified_lanes}, {"implementing"}
        )
        self.assertEqual(out.decision, ADMIT_ALLOW_DISPATCH)


class CoordinatorConvenienceRejectedTest(unittest.TestCase):
    """The owner correction (j#69283), made machine-checkable."""

    def test_callback_miss_concern_alone_does_not_serialize(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(candidate_issue="12921", callback_miss_concern=True)
        )
        self.assertEqual(out.decision, ADMIT_ALLOW_DISPATCH)
        self.assertEqual(out.rejected_nonreasons, (NONREASON_CALLBACK_MISS_RISK,))

    def test_management_load_alone_does_not_serialize(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921", coordinator_management_load=True
            )
        )
        self.assertEqual(out.decision, ADMIT_ALLOW_DISPATCH)
        self.assertEqual(
            out.rejected_nonreasons, (NONREASON_COORDINATOR_MANAGEMENT_LOAD,)
        )

    def test_broad_bucket_alone_does_not_serialize(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(candidate_issue="12921", broad_bucket_only=True)
        )
        self.assertEqual(out.decision, ADMIT_ALLOW_DISPATCH)
        self.assertEqual(out.rejected_nonreasons, (NONREASON_BROAD_BUCKET,))

    def test_all_nonreasons_together_still_allow_dispatch(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                callback_miss_concern=True,
                coordinator_management_load=True,
                broad_bucket_only=True,
            )
        )
        self.assertEqual(out.decision, ADMIT_ALLOW_DISPATCH)
        self.assertEqual(
            set(out.rejected_nonreasons), set(INVALID_SERIALIZATION_NONREASONS)
        )

    def test_nonreason_does_not_suppress_a_real_risk(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                file_overlap_lanes=("12639",),
                broad_bucket_only=True,
            )
        )
        self.assertEqual(out.decision, ADMIT_SERIALIZE)
        self.assertEqual(out.rejected_nonreasons, (NONREASON_BROAD_BUCKET,))
        self.assertEqual(out.risk_reasons, (RISK_FILE_OVERLAP,))


class SerializeTest(unittest.TestCase):
    def test_file_overlap_serializes_with_offending_lane(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921", file_overlap_lanes=("12440", "12639")
            )
        )
        self.assertEqual(out.decision, ADMIT_SERIALIZE)
        self.assertEqual(out.risk_reasons, (RISK_FILE_OVERLAP,))
        self.assertEqual(out.risks[0].lanes, ("12440", "12639"))

    def test_invariant_overlap_serializes(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921", invariant_overlap_lanes=("12639",)
            )
        )
        self.assertEqual(out.decision, ADMIT_SERIALIZE)
        self.assertEqual(out.risk_reasons, (RISK_INVARIANT_OVERLAP,))

    def test_merge_order_conflict_serializes(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921", merge_order_conflict_lanes=("12920",)
            )
        )
        self.assertEqual(out.decision, ADMIT_SERIALIZE)
        self.assertEqual(out.risk_reasons, (RISK_MERGE_ORDER_CONFLICT,))

    def test_dependency_on_coordinator_owned_queue_serializes(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                active_lane_signals=(_signal("12639", GATE_REVIEW_REQUEST),),
                dependency_lanes=("12639",),
            )
        )
        self.assertEqual(out.decision, ADMIT_SERIALIZE)
        self.assertEqual(out.risk_reasons, (RISK_COORDINATOR_OWNED_QUEUE,))
        self.assertEqual(out.risks[0].lanes, ("12639",))

    def test_dependency_on_implementing_lane_is_not_a_risk(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                active_lane_signals=(_signal("12639", GATE_START),),
                dependency_lanes=("12639",),
            )
        )
        self.assertEqual(out.decision, ADMIT_ALLOW_DISPATCH)
        self.assertEqual(out.risks, ())


class BlockedTest(unittest.TestCase):
    def test_dependency_on_blocked_lane_blocks(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                active_lane_signals=(_signal("12639", GATE_BLOCKED),),
                dependency_lanes=("12639",),
            )
        )
        self.assertEqual(out.decision, ADMIT_BLOCKED)
        self.assertEqual(out.risk_reasons, (RISK_BLOCKED_OR_CALLBACK_FAILURE,))

    def test_dependency_on_callback_delivery_failed_lane_blocks(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                active_lane_signals=(
                    _signal("12639", GATE_START, callback_state=CALLBACK_DELIVERY_FAILED),
                ),
                dependency_lanes=("12639",),
            )
        )
        self.assertEqual(out.decision, ADMIT_BLOCKED)
        self.assertEqual(out.risk_reasons, (RISK_BLOCKED_OR_CALLBACK_FAILURE,))

    def test_dependency_with_no_signal_is_fail_closed_to_blocked(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(candidate_issue="12921", dependency_lanes=("99999",))
        )
        self.assertEqual(out.decision, ADMIT_BLOCKED)
        self.assertEqual(out.risk_reasons, (RISK_BLOCKED_OR_CALLBACK_FAILURE,))
        self.assertEqual(out.risks[0].lanes, ("99999",))


class NeedsOwnerDecisionTest(unittest.TestCase):
    def test_unresolved_design_needs_owner(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921", unresolved_design_decision=True
            )
        )
        self.assertEqual(out.decision, ADMIT_NEEDS_OWNER_DECISION)
        self.assertEqual(out.risk_reasons, (RISK_UNRESOLVED_DESIGN_DECISION,))

    def test_release_gate_needs_owner(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921", release_publish_gate_active=True
            )
        )
        self.assertEqual(out.decision, ADMIT_NEEDS_OWNER_DECISION)
        self.assertEqual(out.risk_reasons, (RISK_RELEASE_PUBLISH_GATE,))

    def test_credential_destructive_gate_needs_owner(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                credential_destructive_external_gate_active=True,
            )
        )
        self.assertEqual(out.decision, ADMIT_NEEDS_OWNER_DECISION)
        self.assertEqual(
            out.risk_reasons, (RISK_CREDENTIAL_DESTRUCTIVE_EXTERNAL_GATE,)
        )


class PrecedenceTest(unittest.TestCase):
    def test_owner_gate_outranks_serialize_but_lists_both(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                file_overlap_lanes=("12639",),
                release_publish_gate_active=True,
            )
        )
        self.assertEqual(out.decision, ADMIT_NEEDS_OWNER_DECISION)
        self.assertEqual(
            set(out.risk_reasons), {RISK_FILE_OVERLAP, RISK_RELEASE_PUBLISH_GATE}
        )

    def test_blocked_outranks_serialize(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                active_lane_signals=(_signal("12500", GATE_BLOCKED),),
                dependency_lanes=("12500",),
                file_overlap_lanes=("12639",),
            )
        )
        self.assertEqual(out.decision, ADMIT_BLOCKED)
        self.assertEqual(
            set(out.risk_reasons),
            {RISK_FILE_OVERLAP, RISK_BLOCKED_OR_CALLBACK_FAILURE},
        )

    def test_owner_gate_outranks_blocked(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                active_lane_signals=(_signal("12500", GATE_BLOCKED),),
                dependency_lanes=("12500",),
                unresolved_design_decision=True,
            )
        )
        self.assertEqual(out.decision, ADMIT_NEEDS_OWNER_DECISION)


class RenderAndPayloadTest(unittest.TestCase):
    def test_next_safe_action_per_decision(self):
        allow = evaluate_lane_admission(LaneAdmissionInputs(candidate_issue="12921"))
        self.assertIn("dispatch 12921", allow.next_safe_action)
        serialize = evaluate_lane_admission(
            LaneAdmissionInputs(candidate_issue="12921", file_overlap_lanes=("12639",))
        )
        self.assertIn("serialize 12921", serialize.next_safe_action)
        self.assertIn("12639", serialize.next_safe_action)

    def test_payload_round_trips_keys(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(candidate_issue="12921", file_overlap_lanes=("12639",))
        )
        payload = out.as_payload()
        self.assertEqual(payload["candidate_issue"], "12921")
        self.assertEqual(payload["decision"], ADMIT_SERIALIZE)
        self.assertFalse(payload["should_dispatch"])
        self.assertEqual(payload["risks"][0]["reason"], RISK_FILE_OVERLAP)

    def test_journal_has_decision_and_no_private_path(self):
        out = evaluate_lane_admission(
            LaneAdmissionInputs(
                candidate_issue="12921",
                active_lane_signals=(_signal("12639", GATE_REVIEW_REQUEST),),
                file_overlap_lanes=("12639",),
                callback_miss_concern=True,
            )
        )
        text = render_lane_admission_journal(out)
        self.assertIn("## Lane admission decision", text)
        self.assertIn("admission_decision: serialize", text)
        self.assertIn(RISK_FILE_OVERLAP, text)
        self.assertIn(NONREASON_CALLBACK_MISS_RISK, text)
        self.assertNotIn("/Users/", text)
        self.assertNotIn("%", text)


if __name__ == "__main__":
    unittest.main()
