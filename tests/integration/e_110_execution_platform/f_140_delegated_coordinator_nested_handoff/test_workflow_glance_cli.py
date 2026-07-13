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


class ActiveLanesRedmineFoldTest(unittest.TestCase):
    """Default roster path: enumerate lanes (--issue) + fold canonical ``## Gate:`` journals.

    Redmine #13435 review j#74295 Finding 1 / design j#74307: a known canonical gate returns
    a concrete workflow state even with an **empty** runtime store; an unrecognized template
    or an unavailable source is an explicit degraded ``unknown`` row, never silently dropped.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "workflow-runtime.sqlite"  # left empty
        self.redmine = Path(self._tmp.name) / "redmine.json"
        self.redmine.write_text(
            json.dumps(
                {
                    "13425": {
                        "issue": {"subject": "impl lane", "status": {"is_closed": False}},
                        "journals": [
                            {"id": "73900", "notes": "## Gate: Start (worker)\n- lane: x"},
                            {
                                "id": "73980",
                                "notes": "## Gate: Implementation Done + Review Request (worker)\n"
                                "- commit: `abc1234`",
                            },
                        ],
                    },
                    "13480": {  # journals present but no canonical gate -> unknown template
                        "issue": {"subject": "noise lane", "status": {"is_closed": False}},
                        "journals": [{"id": "1", "notes": "## Progress Log: hi"}],
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_known_gate_folds_concretely_with_empty_store(self):
        rc, out = _run(
            [
                "workflow", "glance", "--active-lanes", "--json", "--no-ledger",
                "--issue", "13425",
                "--redmine-json", str(self.redmine),
                "--store-path", str(self.store_path),
            ]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        rows = {r["issue_id"]: r for r in payload["rows"]}
        self.assertIn("13425", rows)  # roster + Redmine fold, NOT the (empty) store
        self.assertEqual(rows["13425"]["workflow_state"], "review_waiting")  # impl_done+review_request
        self.assertEqual(rows["13425"]["latest_journal"], "73980")
        self.assertEqual(rows["13425"]["next_owner"], "auditor")
        self.assertFalse(payload["degraded"])

    def test_unrecognized_template_is_degraded_unknown_not_dropped(self):
        rc, out = _run(
            [
                "workflow", "glance", "--active-lanes", "--json", "--no-ledger",
                "--issue", "13480",
                "--redmine-json", str(self.redmine),
                "--store-path", str(self.store_path),
            ]
        )
        payload = json.loads(out)
        rows = {r["issue_id"]: r for r in payload["rows"]}
        self.assertIn("13480", rows)  # never silently dropped
        self.assertEqual(rows["13480"]["workflow_state"], "unknown")
        self.assertEqual(rows["13480"]["next_owner"], "coordinator")
        self.assertTrue(payload["degraded"])
        self.assertTrue(payload["notes"])

    def test_source_unavailable_is_degraded_not_silent_empty(self):
        # An issue absent from the fixture -> the Redmine read raises -> a degraded unknown
        # row (source unavailable), distinct from "no active lanes".
        rc, out = _run(
            [
                "workflow", "glance", "--active-lanes", "--json", "--no-ledger",
                "--issue", "99999",
                "--redmine-json", str(self.redmine),
                "--store-path", str(self.store_path),
            ]
        )
        payload = json.loads(out)
        rows = {r["issue_id"]: r for r in payload["rows"]}
        self.assertIn("99999", rows)
        self.assertEqual(rows["99999"]["workflow_state"], "unknown")
        self.assertTrue(payload["degraded"])


class ActiveLanesStoreAdvisoryTest(unittest.TestCase):
    """``--no-redmine``: roster + advisory runtime store + ledger (offline fallback)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "workflow-runtime.sqlite"
        self.ledger_path = Path(self._tmp.name) / "herdr-delivery-ledger.sqlite"

        store = WorkflowRuntimeStore(path=self.store_path)
        store.append_events(
            [
                {"event_id": "redmine:13425:73900", "issue": "13425", "gate": "progress"},
                {"event_id": "redmine:13425:73980", "issue": "13425", "gate": "implementation_done"},
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

    def _argv(self, *extra):
        return [
            "workflow", "glance", "--active-lanes", "--json", "--no-redmine",
            "--issue", "13425",
            "--store-path", str(self.store_path),
            "--ledger-path", str(self.ledger_path),
            *extra,
        ]

    def test_advisory_store_supplies_state_and_ledger_anomaly(self):
        rc, out = _run(self._argv())
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        rows = {r["issue_id"]: r for r in payload["rows"]}
        self.assertIn("13425", rows)
        row = rows["13425"]
        self.assertEqual(row["workflow_state"], "review_waiting")  # latest advisory event = impl_done
        self.assertEqual(row["latest_journal"], "73980")
        self.assertEqual(row["delivery_anomaly"], "turn_start_unconfirmed")
        self.assertEqual(row["next_owner"], "coordinator")
        self.assertEqual(payload["active_anomaly_issues"], ["13425"])
        self.assertFalse(payload["degraded"])  # advisory store satisfied the lane

    def test_glance_is_read_only_store_unchanged(self):
        before = WorkflowRuntimeStore(path=self.store_path).read_events()
        _run(self._argv())
        after = WorkflowRuntimeStore(path=self.store_path).read_events()
        self.assertEqual([e.as_payload() for e in before], [e.as_payload() for e in after])

    def test_no_ledger_flag_drops_delivery_join(self):
        rc, out = _run(self._argv("--no-ledger"))
        payload = json.loads(out)
        rows = {r["issue_id"]: r for r in payload["rows"]}
        self.assertEqual(rows["13425"]["delivery_anomaly"], "none")


class LifecycleDiagnosticTest(unittest.TestCase):
    """R1 F4 (j#77247): a superseded lane's authority stays operator-visible in glance."""

    def _hss(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start as hss,
        )

        return hss

    def test_superseded_lane_appears_in_lifecycle_diagnostic(self):
        import os
        from unittest.mock import patch

        from mozyo_bridge.core.state.lane_lifecycle import (
            DISPOSITION_ACTIVE,
            DISPOSITION_SUPERSEDED,
            DecisionPointer,
            LaneLifecycleKey,
            LaneLifecycleStore,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = LaneLifecycleStore(home=home)
            key = LaneLifecycleKey("wProj", "issue_13583_x")
            dec = DecisionPointer(
                source="redmine", issue_id="13583", journal_id="76630"
            )
            store.declare_active(key, decision=dec, issue_id="13583")
            store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=1,
                target=DISPOSITION_SUPERSEDED,
                decision=dec,
            )
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ), patch.object(
                self._hss(), "herdr_workspace_segment", return_value="wProj"
            ):
                rc, out = _run(["workflow", "glance", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        diag = payload.get("lifecycle_diagnostic", [])
        entry = next((d for d in diag if d["lane"] == "issue_13583_x"), None)
        self.assertIsNotNone(entry, f"superseded lane missing from diagnostic: {diag}")
        self.assertEqual(entry["lane_disposition"], "superseded")
        self.assertEqual(entry["issue"], "13583")
        # And it is NOT resurfaced into the active roster (capacity excludes it).
        active_issues = {r.get("issue_id") for r in payload.get("rows", [])}
        self.assertNotIn("13583", active_issues)

    def test_snapshot_json_glance_does_not_create_state_store(self):
        # R2-F2 (j#77292): a read-only --snapshot-json glance must not create state.sqlite
        # just to fold the lifecycle diagnostic (the command's store-free contract).
        import os
        from unittest.mock import patch

        from mozyo_bridge.core.state.lane_lifecycle import lane_lifecycle_path

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            snap = home / "snap.json"
            _write_snapshot(snap, [])
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ):
                rc, _ = _run(
                    ["workflow", "glance", "--snapshot-json", str(snap), "--json"]
                )
            self.assertEqual(rc, 0)
            self.assertFalse(
                lane_lifecycle_path(home).exists(),
                "read-only glance created the lifecycle state store",
            )


if __name__ == "__main__":
    unittest.main()
