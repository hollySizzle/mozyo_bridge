"""`workflow glance` CLI integration tests (Redmine #13435).

Covers the read-only coordinator pipeline projection entrypoint:

- ``workflow glance`` registers under the ``workflow`` family;
- ``--snapshot-json`` renders a fixed-width table and a ``--json`` envelope whose
  ``active_anomaly_issues`` names only the live (non-stale) delivery stalls;
- a done-but-not-delivered lane reads as a stall (workflow_state not rolled back,
  re-owned to the coordinator), while a later-gate-superseded anomaly is ``(stale)``;
- ``--issue`` narrows the projection;
- ``--active-lanes`` enumerates a temp workflow-runtime store and joins a temp herdr
  delivery ledger, and the read is non-mutating (the store is unchanged after);
- the command always exits 0 (a projection, never a delivery).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.core.state.herdr_delivery_ledger import (
    HerdrDeliveryLedger,
    HerdrDeliveryLedgerRecord,
)
from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


def _write_snapshot(path: Path, issues) -> None:
    path.write_text(json.dumps({"issues": issues}), encoding="utf-8")


class RegistrationTest(unittest.TestCase):
    def test_glance_is_registered(self):
        ns = build_parser().parse_args(["workflow", "glance", "--json"])
        self.assertTrue(hasattr(ns, "func"))


class SnapshotJsonTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.snap = Path(self._tmp.name) / "snap.json"
        _write_snapshot(
            self.snap,
            [
                {
                    "issue": "13435",
                    "subject": "pipeline glance cmd",
                    "lane": "issue_13435_pipeline_glance",
                    "latest_gate": "review_request",
                    "latest_gate_journal": "74210",
                    "delivery": {
                        "anomaly": "staged_not_submitted",
                        "source": "runtime_observation",
                        "observed_journal": "74210",
                        "runtime_state": "awaiting_input",
                    },
                },
                {"issue": "13446", "lane": "issue_13446", "latest_gate": "progress"},
                {
                    "issue": "13408",
                    "lane": "issue_13408",
                    "latest_gate": "review",
                    "review_conclusion": "approved",
                    "latest_gate_journal": "74130",
                    "delivery": {"anomaly": "callback_self_loop", "observed_journal": "74118"},
                },
            ],
        )

    def test_table_shows_stall_and_stale_markers(self):
        rc, out = _run(
            ["workflow", "glance", "--snapshot-json", str(self.snap), "--no-ledger"]
        )
        self.assertEqual(rc, 0)
        self.assertIn("WORKFLOW_STATE", out)
        self.assertIn("~staged_not_submitted", out)  # runtime-observed live stall
        self.assertIn("(stale)", out)  # superseded self-loop
        self.assertIn("review_waiting", out)  # not rolled back

    def test_json_envelope_lists_only_live_anomaly_issues(self):
        rc, out = _run(
            ["workflow", "glance", "--snapshot-json", str(self.snap), "--no-ledger", "--json"]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["active_anomaly_issues"], ["13435"])
        rows = {r["issue_id"]: r for r in payload["rows"]}
        self.assertEqual(rows["13435"]["workflow_state"], "review_waiting")
        self.assertEqual(rows["13435"]["next_owner"], "coordinator")
        self.assertFalse(rows["13435"]["delivery_anomaly_stale"])
        self.assertTrue(rows["13408"]["delivery_anomaly_stale"])
        self.assertEqual(rows["13446"]["next_owner"], "worker")

    def test_issue_filter_narrows_projection(self):
        rc, out = _run(
            [
                "workflow", "glance", "--snapshot-json", str(self.snap),
                "--no-ledger", "--json", "--issue", "13435",
            ]
        )
        payload = json.loads(out)
        self.assertEqual([r["issue_id"] for r in payload["rows"]], ["13435"])


class ActiveLanesStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "workflow-runtime.sqlite"
        self.ledger_path = Path(self._tmp.name) / "herdr-delivery-ledger.sqlite"

        store = WorkflowRuntimeStore(path=self.store_path)
        store.append_events(
            [
                {
                    "event_id": "redmine:13425:73900",
                    "issue": "13425",
                    "gate": "progress",
                },
                {
                    "event_id": "redmine:13425:73980",
                    "issue": "13425",
                    "gate": "implementation_done",
                },
            ]
        )
        store.put_route_identities(
            [
                {
                    "route_id": "r1",
                    "issue": "13425",
                    "workspace_id": "wZ",
                    "lane_id": "issue_13425_lane",
                    "role": "implementation_worker",
                    "pane_name": "claude_default",
                }
            ]
        )
        # A turn-start that injected but never confirmed -> turn_start_unconfirmed.
        HerdrDeliveryLedger(path=self.ledger_path).append(
            HerdrDeliveryLedgerRecord(
                issue_id="13425",
                journal_id="73980",
                turn_start_outcome={"outcome": "delivered_not_started"},
            )
        )

    def test_active_lanes_enumerates_store_and_joins_ledger(self):
        rc, out = _run(
            [
                "workflow", "glance", "--active-lanes", "--json",
                "--store-path", str(self.store_path),
                "--ledger-path", str(self.ledger_path),
            ]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        rows = {r["issue_id"]: r for r in payload["rows"]}
        self.assertIn("13425", rows)
        row = rows["13425"]
        self.assertEqual(row["workflow_state"], "review_waiting")  # latest event = impl_done
        self.assertEqual(row["lane"], "issue_13425_lane")
        self.assertEqual(row["latest_journal"], "73980")
        self.assertEqual(row["delivery_anomaly"], "turn_start_unconfirmed")
        self.assertEqual(row["next_owner"], "coordinator")
        self.assertEqual(payload["active_anomaly_issues"], ["13425"])

    def test_glance_is_read_only_store_unchanged(self):
        before = WorkflowRuntimeStore(path=self.store_path).read_events()
        _run(
            [
                "workflow", "glance", "--active-lanes", "--json",
                "--store-path", str(self.store_path),
                "--ledger-path", str(self.ledger_path),
            ]
        )
        after = WorkflowRuntimeStore(path=self.store_path).read_events()
        self.assertEqual([e.as_payload() for e in before], [e.as_payload() for e in after])

    def test_no_ledger_flag_drops_delivery_join(self):
        rc, out = _run(
            [
                "workflow", "glance", "--active-lanes", "--json", "--no-ledger",
                "--store-path", str(self.store_path),
                "--ledger-path", str(self.ledger_path),
            ]
        )
        payload = json.loads(out)
        rows = {r["issue_id"]: r for r in payload["rows"]}
        self.assertEqual(rows["13425"]["delivery_anomaly"], "none")


if __name__ == "__main__":
    unittest.main()
