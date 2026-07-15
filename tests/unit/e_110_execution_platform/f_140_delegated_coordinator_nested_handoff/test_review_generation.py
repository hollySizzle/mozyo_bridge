"""Review-generation fencing tests (Redmine #13518 review R2-F7).

The deterministic concurrent scenario (reproduced from #13586 j#75832 -> j#75833): reviewer A
appends a blocking finding; reviewer B, on a snapshot taken before that finding, tries to approve —
and is refused (not last-write-wins). Plus generation identity, integration latest-generation fence,
and the duplicate-consumer CAS lease.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (
    DECISION_APPROVAL,
    DECISION_FINDING,
    DISPOSITION_RESOLVED,
    DISPOSITION_UNRESOLVED,
    REASON_NEWER_BLOCKING_FINDING,
    REASON_NO_APPROVAL_FOR_LATEST,
    REASON_OK,
    REASON_UNRESOLVED_BLOCKING_FINDING,
    GenerationLease,
    GenerationLeaseError,
    ReviewDecision,
    ReviewGeneration,
    evaluate_approval_admissible,
    evaluate_integration_admissible,
)

GEN = ReviewGeneration(issue="13586", review_request_journal="75829", target_head="4857f7e")


class GenerationIdentityTest(unittest.TestCase):
    def test_identity_includes_issue_request_and_head(self):
        self.assertEqual(GEN.identity, "13586:75829:4857f7e")

    def test_a_new_head_is_a_new_generation(self):
        self.assertNotEqual(GEN, ReviewGeneration("13586", "75829", "deadbee"))


class PreApprovalFenceTest(unittest.TestCase):
    def test_stale_approval_refused_when_a_newer_blocking_finding_exists(self):
        # #13586: the approving consumer acted on the review request at seq 10; A's blocking finding
        # landed at seq 12 (after B's snapshot). B's approval must be refused, not last-write-wins.
        decisions = [
            ReviewDecision(GEN, DECISION_FINDING, seq=12, blocking=True, disposition=DISPOSITION_UNRESOLVED),
        ]
        result = evaluate_approval_admissible(source_request_seq=10, decisions=decisions)
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_NEWER_BLOCKING_FINDING)

    def test_approval_admissible_when_no_newer_blocking_finding(self):
        decisions = [
            ReviewDecision(GEN, DECISION_FINDING, seq=8, blocking=True, disposition=DISPOSITION_RESOLVED),
        ]
        result = evaluate_approval_admissible(source_request_seq=10, decisions=decisions)
        self.assertTrue(result.admissible)
        self.assertEqual(result.reason, REASON_OK)

    def test_older_or_resolved_findings_do_not_block(self):
        decisions = [
            ReviewDecision(GEN, DECISION_FINDING, seq=5, blocking=True, disposition=DISPOSITION_UNRESOLVED),
            ReviewDecision(GEN, DECISION_FINDING, seq=15, blocking=True, disposition=DISPOSITION_RESOLVED),
        ]
        self.assertTrue(evaluate_approval_admissible(10, decisions).admissible)


class IntegrationFenceTest(unittest.TestCase):
    def test_integration_requires_latest_generation_approved(self):
        # An approval for an OLDER generation does not satisfy the latest one.
        old = ReviewGeneration("13586", "75000", "oldhead")
        decisions = [ReviewDecision(old, DECISION_APPROVAL, seq=3)]
        result = evaluate_integration_admissible(GEN, decisions)
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_NO_APPROVAL_FOR_LATEST)

    def test_integration_blocked_by_unresolved_blocking_finding_in_latest(self):
        decisions = [
            ReviewDecision(GEN, DECISION_APPROVAL, seq=20),
            ReviewDecision(GEN, DECISION_FINDING, seq=12, blocking=True, disposition=DISPOSITION_UNRESOLVED),
        ]
        result = evaluate_integration_admissible(GEN, decisions)
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_UNRESOLVED_BLOCKING_FINDING)

    def test_integration_admissible_when_latest_approved_and_clean(self):
        decisions = [
            ReviewDecision(GEN, DECISION_FINDING, seq=12, blocking=True, disposition=DISPOSITION_RESOLVED),
            ReviewDecision(GEN, DECISION_APPROVAL, seq=20),
        ]
        result = evaluate_integration_admissible(GEN, decisions)
        self.assertTrue(result.admissible)


class GenerationLeaseTest(unittest.TestCase):
    def test_second_consumer_of_same_generation_is_refused(self):
        lease = GenerationLease()
        self.assertTrue(lease.acquire(GEN, "consumer-A"))
        self.assertFalse(lease.acquire(GEN, "consumer-B"))  # single winner
        self.assertEqual(lease.holder(GEN), "consumer-A")

    def test_same_consumer_reacquire_is_idempotent(self):
        lease = GenerationLease()
        self.assertTrue(lease.acquire(GEN, "consumer-A"))
        self.assertTrue(lease.acquire(GEN, "consumer-A"))

    def test_empty_consumer_id_fails_closed(self):
        with self.assertRaises(GenerationLeaseError):
            GenerationLease().acquire(GEN, "")


class DeterministicConcurrentScenarioTest(unittest.TestCase):
    """The full #13586 race: A appends a finding; B's stale approval is superseded, not committed."""

    def test_reviewer_b_stale_approval_is_rejected_after_reviewer_a_finding(self):
        lease = GenerationLease()
        # Both reviewers claim the same generation; the lease admits ONE processing consumer.
        self.assertTrue(lease.acquire(GEN, "reviewer-A"))
        self.assertFalse(lease.acquire(GEN, "reviewer-B"))  # B is not admitted to double-process

        # Timeline: review request at seq 10; A appends a blocking finding at seq 12.
        journal = [ReviewDecision(GEN, DECISION_FINDING, seq=12, blocking=True, disposition=DISPOSITION_UNRESOLVED)]

        # B, acting on the pre-finding snapshot (seq 10), rereads before committing approval -> refused.
        pre_approval = evaluate_approval_admissible(source_request_seq=10, decisions=journal)
        self.assertFalse(pre_approval.admissible)

        # Even if a stale approval had been appended, integration requires the latest generation to
        # be clean -> the unresolved finding blocks integration (never last-write-wins).
        journal.append(ReviewDecision(GEN, DECISION_APPROVAL, seq=13))
        self.assertFalse(evaluate_integration_admissible(GEN, journal).admissible)

        # Only after A's finding is dispositioned does integration become admissible.
        resolved = [
            ReviewDecision(GEN, DECISION_FINDING, seq=12, blocking=True, disposition=DISPOSITION_RESOLVED),
            ReviewDecision(GEN, DECISION_APPROVAL, seq=25),
        ]
        self.assertTrue(evaluate_integration_admissible(GEN, resolved).admissible)


if __name__ == "__main__":
    unittest.main()
