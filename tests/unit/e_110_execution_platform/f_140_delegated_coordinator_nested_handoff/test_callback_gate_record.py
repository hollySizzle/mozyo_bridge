"""Canonical gate-record writer tests (Redmine #13520 review F1a).

The production producer wiring: a callback-required gate is recorded through the single canonical
renderer (always marker-bearing) and posted via the credential-gated, opt-in note transport. No
live Redmine — the write seam is injected.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_gate_record import (
    GATE_RECORD_OK,
    GATE_RECORD_WRITE_OPTIN_UNSET,
    emit_gate_record,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    extract_markers_from_note,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.delivery_record_sink import (
    DeliveryTransportError,
    PERSIST_CREDENTIAL_MISSING,
)


class _FakeTransport:
    def __init__(self, *, fail_reason=None):
        self._fail_reason = fail_reason
        self.posted: list = []

    def post_issue_note(self, issue_id, notes):
        if self._fail_reason is not None:
            raise DeliveryTransportError("boom", reason=self._fail_reason)
        self.posted.append((issue_id, notes))
        return f"redmine:issue={issue_id}"


class EmitGateRecordTest(unittest.TestCase):
    def test_posts_a_discoverable_marker_bearing_note(self):
        tx = _FakeTransport()
        receipt = emit_gate_record("13518", "implementation_done", body="done", transport=tx)
        self.assertTrue(receipt.recorded)
        self.assertEqual(receipt.reason, GATE_RECORD_OK)
        self.assertEqual(receipt.location, "redmine:issue=13518")
        # The posted note carries the discoverable structured marker (not a hand-written fixture).
        (_issue, notes) = tx.posted[0]
        markers = extract_markers_from_note("13518", "75500", notes)
        self.assertEqual(markers[0].gate, "implementation_done")
        self.assertIn("done", notes)

    def test_no_transport_is_fail_closed_write_optin_unset(self):
        receipt = emit_gate_record("13518", "review_request", transport=None)
        self.assertFalse(receipt.recorded)
        self.assertEqual(receipt.reason, GATE_RECORD_WRITE_OPTIN_UNSET)  # nothing written, not silent

    def test_transport_failure_maps_to_explicit_reason(self):
        tx = _FakeTransport(fail_reason=PERSIST_CREDENTIAL_MISSING)
        receipt = emit_gate_record("13518", "blocked", transport=tx)
        self.assertFalse(receipt.recorded)
        self.assertEqual(receipt.reason, PERSIST_CREDENTIAL_MISSING)

    def test_non_callback_gate_is_rejected(self):
        with self.assertRaises(ValueError):
            emit_gate_record("13518", "close", transport=_FakeTransport())


if __name__ == "__main__":
    unittest.main()
