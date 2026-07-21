"""End-to-end review-generation fence (Redmine #13518 review R2-F7 / R3-F2).

Reproduces the #13586 last-write-wins incident and proves the fence closes it end to end:
concurrent review writers over one durable generation -> the stale approver's WRITE is refused
(durable single-consumer lease + pre-approval reread fence) -> and integration ACTION-TIME re-derives
admissibility and refuses the stale/unclean generation. The pure evaluators + the durable lease now
have real application callers, not only unit tests.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.review_admission import (  # noqa: E501
    REASON_LEASE_HELD_BY_OTHER,
    GenerationLeaseStore,
    admit_review_approval,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (  # noqa: E501
    DECISION_APPROVAL,
    DECISION_FINDING,
    DISPOSITION_UNRESOLVED,
    REASON_NEWER_BLOCKING_FINDING,
    ReviewDecision,
    ReviewGeneration,
    evaluate_integration_admissible,
)


def _gen() -> ReviewGeneration:
    return ReviewGeneration(issue="13586", review_request_journal="75719", target_head="deadbeef")


def _finding(seq: int, *, blocking=True, disposition=DISPOSITION_UNRESOLVED) -> ReviewDecision:
    return ReviewDecision(
        generation=_gen(), kind=DECISION_FINDING, seq=seq, blocking=blocking, disposition=disposition
    )


def _approval(seq: int) -> ReviewDecision:
    return ReviewDecision(generation=_gen(), kind=DECISION_APPROVAL, seq=seq)


class ReviewGenerationFenceEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = WorkflowRuntimeStore(path=Path(self._tmp.name) / "wf.sqlite")
        self.lease = GenerationLeaseStore(store=self.store)

    def test_concurrent_writer_then_stale_approval_refusal_then_integration_refusal(self):
        gen = _gen()
        # Reviewer A acted on the review request at seq=10 and, after re-reading, filed a NEWER
        # unresolved blocking finding at seq=12 (the real latest state).
        request_seq = 10
        decisions = [_approval(11), _finding(12)]

        # Reviewer A commits the generation (acquires the durable single-consumer lease). A's own
        # approval attempt is refused too, because the reread fence sees A's newer blocking finding.
        a = admit_review_approval(
            lease=self.lease, generation=gen, consumer_id="reviewer-A",
            source_request_seq=request_seq, decisions=decisions,
        )
        self.assertFalse(a.admissible)
        self.assertEqual(a.reason, REASON_NEWER_BLOCKING_FINDING)

        # Reviewer B raced in on a STALE snapshot (predating the seq=12 finding). Two fences stop B:
        #  (1) the durable lease is already held by A -> a different consumer is refused; and
        #  (2) even alone, the reread fence would refuse the stale approval.
        b = admit_review_approval(
            lease=self.lease, generation=gen, consumer_id="reviewer-B",
            source_request_seq=request_seq, decisions=decisions,
        )
        self.assertFalse(b.admissible)
        self.assertEqual(b.reason, REASON_LEASE_HELD_BY_OTHER)

        # And the integration ACTION-TIME backstop: even if a stale approval had been written, the
        # latest generation is NOT admissible (an unresolved blocking finding remains).
        integ = evaluate_integration_admissible(gen, decisions)
        self.assertFalse(integ.admissible)

    def test_lease_is_durable_across_store_instances(self):
        gen = _gen()
        self.assertTrue(self.lease.acquire(gen, "reviewer-A"))
        # A fresh store instance over the same file still sees A as the holder (durable, not in-mem).
        reopened = GenerationLeaseStore(store=WorkflowRuntimeStore(path=self.store.path))
        self.assertFalse(reopened.acquire(gen, "reviewer-B"))
        self.assertEqual(reopened.holder(gen), "reviewer-A")
        self.assertTrue(reopened.acquire(gen, "reviewer-A"))  # idempotent for the same consumer

    def test_clean_generation_admits_the_single_consumer(self):
        gen = _gen()
        decisions = [_approval(11)]  # approved, no blocking finding
        result = admit_review_approval(
            lease=self.lease, generation=gen, consumer_id="reviewer-A",
            source_request_seq=10, decisions=decisions,
        )
        self.assertTrue(result.admissible)
        # And integration admits it.
        self.assertTrue(evaluate_integration_admissible(gen, decisions).admissible)


if __name__ == "__main__":
    unittest.main()
