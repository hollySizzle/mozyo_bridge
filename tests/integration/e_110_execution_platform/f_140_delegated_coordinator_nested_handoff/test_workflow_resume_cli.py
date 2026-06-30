"""`workflow resume` CLI + persist-loop integration tests (Redmine #12671).

Covers the explicit-execution entrypoint that reads persisted mozyo-DB runtime state and
reports the current ``workflow.state`` + the enriched ``workflow.next_action``:

- ``workflow resume`` registers under the ``workflow`` family;
- ``workflow runtime --persist`` then ``workflow resume`` reproduces the same decision from
  the durable runtime state (the persist -> resume loop), enriched with route_identity /
  anchor / risk_level / requires_confirmation;
- the persisted ``last_seen_pane_id`` is **never** emitted in the resume output (pane id is
  cache / evidence only);
- a lane-targeted routing action with no persisted route identity fails closed
  (``route_identity_unresolved`` + requires_confirmation);
- ``--json`` nests ``workflow.{state,next_action}``; ``--journal`` emits the durable record;
- an empty store resumes to a well-formed hold; the pure ``assemble_command_result`` folds
  fake rows without a DB.
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
from mozyo_bridge.core.state.workflow_runtime_store import (
    WorkflowEventRow,
    WorkflowRouteRow,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_resume import (
    assemble_command_result,
    resume_command_result,
)


def _run(argv):
    parser = build_parser()
    ns = parser.parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ns.func(ns)
    return rc, out.getvalue()


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = str(Path(self._tmp.name) / "workflow-runtime.sqlite")


class RegistrationTest(unittest.TestCase):
    def test_resume_is_registered(self):
        parser = build_parser()
        ns = parser.parse_args(["workflow", "resume", "--json"])
        self.assertEqual(ns.func.__name__, "cmd_workflow_resume")
        self.assertTrue(ns.as_json)


class PersistResumeLoopTest(_StoreCase):
    def _persist(self):
        rc, _ = _run(
            [
                "workflow", "runtime",
                "--event", "12671:review_request,id=12671:68864,commit=1",
                "--event", "12671:review,id=12671:68900,conclusion=approved,commit=1",
                "--ready-independent", "1", "--capacity", "2",
                "--persist", "--store-path", self.store_path,
                "--route-identity",
                "route_id=r-12671,issue=12671,ws=ws1,role=codex,pane_name=gw-12671,pane_id=%17",
                "--json",
            ]
        )
        self.assertEqual(rc, 0)

    def test_resume_reproduces_enriched_next_action(self):
        self._persist()
        rc, text = _run(["workflow", "resume", "--store-path", self.store_path, "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(text)
        na = payload["workflow"]["next_action"]
        # review approved -> owner aggregation, high risk, confirm required.
        self.assertEqual(na["action"], "aggregate_owner_approval")
        self.assertEqual(na["owner_role"], "coordinator")
        self.assertEqual(na["risk_level"], "high")
        self.assertTrue(na["requires_confirmation"])
        self.assertEqual(na["blocked_reason"], "")
        # anchor is the latest persisted durable event; route is the public-safe pointer.
        self.assertEqual(na["anchor"], "12671:68900")
        self.assertIn("pane_name=gw-12671", na["route_identity"])
        # the persisted lane state replays from durable runtime state
        self.assertEqual(payload["workflow"]["state"]["applied_event_ids"], ["12671:68864", "12671:68900"])

    def test_resume_never_emits_pane_id(self):
        self._persist()
        rc, text = _run(["workflow", "resume", "--store-path", self.store_path, "--json"])
        self.assertEqual(rc, 0)
        self.assertNotIn("%17", text)  # cache/evidence pane id stays out of the payload
        rc2, journal = _run(["workflow", "resume", "--store-path", self.store_path, "--journal"])
        self.assertEqual(rc2, 0)
        self.assertNotIn("%17", journal)

    def test_resume_text_explains_state_and_next_action(self):
        self._persist()
        rc, text = _run(["workflow", "resume", "--store-path", self.store_path])
        self.assertEqual(rc, 0)
        self.assertIn("action: aggregate_owner_approval", text)
        self.assertIn("requires_confirmation: true", text)
        self.assertIn("lane: 12671 -> owner_waiting", text)


class SameIssueRouteSelectionTest(_StoreCase):
    def test_auditor_action_selects_gateway_not_worker_route(self):
        # j#68908 finding 1: persisting a worker(claude) route then a gateway(codex) route
        # for the same issue must NOT make an auditor action point at the worker route.
        rc, _ = _run(
            [
                "workflow", "runtime",
                "--event", "12671:review_request,id=12671:68864,commit=1",
                "--persist", "--store-path", self.store_path,
                "--route-identity",
                "route_id=z-worker,issue=12671,ws=ws1,role=claude,pane_name=worker,pane_id=%20",
                "--route-identity",
                "route_id=a-gateway,issue=12671,ws=ws1,role=codex,pane_name=gateway,pane_id=%17",
                "--json",
            ]
        )
        self.assertEqual(rc, 0)
        rc2, text = _run(["workflow", "resume", "--store-path", self.store_path, "--json"])
        self.assertEqual(rc2, 0)
        na = json.loads(text)["workflow"]["next_action"]
        self.assertEqual(na["action"], "perform_review")
        self.assertEqual(na["owner_role"], "auditor")
        self.assertIn("pane_name=gateway", na["route_identity"])
        self.assertNotIn("worker", na["route_identity"])
        self.assertEqual(na["blocked_reason"], "")
        self.assertNotIn("%17", text)
        self.assertNotIn("%20", text)


class FailClosedRouteTest(_StoreCase):
    def test_routing_action_without_persisted_route_fails_closed(self):
        # Persist an event that yields a routing action (review_request -> perform_review)
        # but NO route identity, so resume must fail closed on the missing route.
        rc, _ = _run(
            [
                "workflow", "runtime",
                "--event", "12671:review_request,id=12671:68864,commit=1",
                "--persist", "--store-path", self.store_path, "--json",
            ]
        )
        self.assertEqual(rc, 0)
        rc2, text = _run(["workflow", "resume", "--store-path", self.store_path, "--json"])
        self.assertEqual(rc2, 0)
        na = json.loads(text)["workflow"]["next_action"]
        self.assertEqual(na["action"], "perform_review")
        self.assertEqual(na["blocked_reason"], "route_identity_unresolved")
        self.assertTrue(na["requires_confirmation"])
        self.assertEqual(na["route_identity"], "")


class EmptyAndPureTest(unittest.TestCase):
    def test_empty_store_resumes_to_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "absent.sqlite")
            rc, text = _run(["workflow", "resume", "--store-path", path, "--json"])
            self.assertEqual(rc, 0)
            na = json.loads(text)["workflow"]["next_action"]
            self.assertEqual(na["action"], "hold")
            self.assertEqual(na["risk_level"], "none")

    def test_assemble_command_result_folds_fake_rows_without_db(self):
        events = [
            WorkflowEventRow(
                event_id="12671:68900", issue="12671", gate="review",
                review_conclusion="approved", callback_state="none",
                commit_bearing=True, integration_recorded=False, issue_open=True,
                blocker_recorded=False,
            ),
        ]
        routes = [
            WorkflowRouteRow(
                route_id="r", issue="12671", workspace_id="ws1", lane_id="default",
                role="codex", pane_name="gw", last_seen_pane_id="%17", observed_at="",
            ),
        ]
        result = assemble_command_result(events, routes, {"capacity_remaining": "2"})
        na = result.next_action
        self.assertEqual(na.action, "aggregate_owner_approval")
        self.assertEqual(na.anchor, "12671:68900")
        self.assertIn("pane_name=gw", na.route_identity)
        self.assertNotIn("%17", na.route_identity)

    def test_resume_command_result_uses_store_port(self):
        # A minimal fake satisfying the read port (no SQLite) drives the use case.
        class _FakeStore:
            def read_events(self):
                return [
                    WorkflowEventRow(
                        event_id="a", issue="12671", gate="start",
                        review_conclusion="pending", callback_state="none",
                        commit_bearing=False, integration_recorded=False,
                        issue_open=True, blocker_recorded=False,
                    )
                ]

            def read_route_identities(self):
                return []

            def read_meta(self):
                return {}

        result = resume_command_result(_FakeStore())
        self.assertIn("workflow", result.as_payload())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
