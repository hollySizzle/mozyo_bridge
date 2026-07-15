"""Unit tests for the reconcile gate-chain expectation (Redmine #13758 review F1)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_gate_chain import (
    OWNER_GATEWAY,
    OWNER_WORKER,
    expected_next,
)


class GateChainTest(unittest.TestCase):
    def test_no_marker_awaits_worker_implementation_done(self):
        self.assertEqual(expected_next(None), ("implementation_done", OWNER_WORKER))
        self.assertEqual(expected_next(""), ("implementation_done", OWNER_WORKER))

    def test_implementation_done_awaits_worker_review_request(self):
        self.assertEqual(expected_next("implementation_done"), ("review_request", OWNER_WORKER))

    def test_review_request_awaits_gateway_review_result(self):
        self.assertEqual(expected_next("review_request"), ("review_result", OWNER_GATEWAY))

    def test_review_result_changes_requested_returns_to_worker(self):
        self.assertEqual(
            expected_next("review_result", review_conclusion="changes_requested"),
            ("implementation_done", OWNER_WORKER),
        )

    def test_review_result_approved_is_owner_owed_none(self):
        self.assertIsNone(expected_next("review_result", review_conclusion="approved"))
        self.assertIsNone(expected_next("review_result"))  # blank conclusion

    def test_blocked_and_owner_close_and_unknown_are_none(self):
        self.assertIsNone(expected_next("blocked"))
        self.assertIsNone(expected_next("owner_close_approval_waiting"))
        self.assertIsNone(expected_next("some_unknown_gate"))


if __name__ == "__main__":
    unittest.main()
