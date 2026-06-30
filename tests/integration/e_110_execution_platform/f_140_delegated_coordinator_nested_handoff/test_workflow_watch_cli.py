"""`workflow watch` CLI + intake/persist/resume integration tests (Redmine #12672).

Covers the event-watcher entrypoint that ingests structured Redmine journal markers into a
pending workflow action in the mozyo DB:

- ``workflow watch`` registers under the ``workflow`` family;
- a ``review_request`` marker with a matching codex route resolves to a ``ready`` pending
  action anchored at ``redmine:<issue>:<journal>``; persisting then ``workflow resume``
  reproduces the same decision (the watch -> resume loop);
- duplicate suppression across runs: re-observing the same journal is suppressed and does
  not double-fold;
- a missing route is a fail-closed ``failed`` pending action (never sent), and two distinct
  same-issue codex routes fail closed ``route_ambiguous``;
- ``--dry-run`` classifies without persisting;
- ``--json`` emits the intake outcome; an unknown gate fails at parse (exit 2);
- the persisted ``last_seen_pane_id`` is never emitted (pane id is cache / evidence only).
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
from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore


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
    def test_watch_is_registered(self):
        parser = build_parser()
        ns = parser.parse_args(["workflow", "watch", "--json"])
        self.assertEqual(ns.func.__name__, "cmd_workflow_watch")
        self.assertTrue(ns.as_json)


class ReadyAndAnchorTest(_StoreCase):
    def test_review_request_with_route_is_ready_and_anchored(self):
        rc, out = _run(
            [
                "workflow", "watch",
                "--marker", "12672:68978:review_request",
                "--route-identity",
                "route_id=r1,issue=12672,ws=ws1,role=codex,pane_name=audit",
                "--store-path", self.store_path,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("pending_status: ready", out)
        self.assertIn("anchor: redmine:12672:68978", out)
        self.assertIn("action: perform_review", out)

    def test_watch_then_resume_reproduces(self):
        _run(
            [
                "workflow", "watch",
                "--marker", "12672:68978:review_request",
                "--route-identity",
                "route_id=r1,issue=12672,ws=ws1,role=codex,pane_name=audit",
                "--store-path", self.store_path,
            ]
        )
        rc, out = _run(["workflow", "resume", "--store-path", self.store_path])
        self.assertEqual(rc, 0)
        self.assertIn("next_action: perform_review", out)
        self.assertIn("anchor: redmine:12672:68978", out)


class DuplicateSuppressionTest(_StoreCase):
    def test_reobserving_same_journal_is_suppressed(self):
        _run(
            [
                "workflow", "watch", "--marker", "12672:68978:review_request",
                "--store-path", self.store_path,
            ]
        )
        rc, out = _run(
            [
                "workflow", "watch", "--marker", "12672:68978:review_request",
                "--store-path", self.store_path,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("accepted: 0 suppressed: 1", out)
        self.assertIn("-> suppressed", out)
        # The store still holds exactly one event for that anchor.
        store = WorkflowRuntimeStore(path=Path(self.store_path))
        rows = store.read_events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].event_id, "redmine:12672:68978")


class FailClosedTest(_StoreCase):
    def test_missing_route_is_failed_and_exit_zero(self):
        rc, out = _run(
            [
                "workflow", "watch", "--marker", "12672:68978:review_request",
                "--store-path", self.store_path,
            ]
        )
        # Advisory: a failed pending action is still recorded, exit 0 (never sends).
        self.assertEqual(rc, 0)
        self.assertIn("pending_status: failed", out)
        self.assertIn("failed_reason: route_identity_unresolved", out)

    def test_two_distinct_codex_routes_same_issue_is_ambiguous(self):
        _run(
            [
                "workflow", "watch", "--marker", "12672:1:start",
                "--route-identity",
                "route_id=r1,issue=12672,ws=ws1,role=codex,pane_name=a",
                "--store-path", self.store_path,
            ]
        )
        rc, out = _run(
            [
                "workflow", "watch", "--marker", "12672:68978:review_request",
                "--route-identity",
                "route_id=r2,issue=12672,ws=ws1,role=codex,pane_name=b",
                "--store-path", self.store_path,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("pending_status: failed", out)
        self.assertIn("failed_reason: route_ambiguous", out)


class DryRunTest(_StoreCase):
    def test_dry_run_does_not_persist(self):
        rc, out = _run(
            [
                "workflow", "watch", "--marker", "12672:68978:review_request",
                "--dry-run", "--store-path", self.store_path,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("-> accepted", out)
        # Nothing was written: a resume sees an empty store (well-formed hold).
        store = WorkflowRuntimeStore(path=Path(self.store_path))
        self.assertEqual(store.read_events(), ())


class JsonAndParseTest(_StoreCase):
    def test_json_emits_intake_and_pending_action(self):
        rc, out = _run(
            [
                "workflow", "watch", "--marker", "12672:68978:review_request",
                "--json", "--store-path", self.store_path,
            ]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertIn("workflow", payload)
        self.assertIn("pending_action", payload)
        self.assertEqual(payload["intake"][0]["event_id"], "redmine:12672:68978")
        self.assertEqual(payload["intake"][0]["disposition"], "accepted")

    def test_unknown_gate_fails_at_parse(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "watch", "--marker", "12672:1:frobnicate"])


class RedmineSourceTest(_StoreCase):
    """`workflow watch --redmine-json` reads Redmine journal history (review j#68992 fix)."""

    def _write_issue_json(self) -> str:
        payload = {
            "issue": {"id": "12672"},
            "journals": [
                {"id": "68978", "notes": "## Start\nno structured marker here"},
                {
                    "id": "68989",
                    "notes": (
                        "## Implementation Done / Review Request\n"
                        "[mozyo:handoff:source=redmine:issue=12672:journal=68989:"
                        "kind=review_request:to=codex]"
                    ),
                },
                {"id": "69200", "notes": "field-only prose mentioning review, no marker"},
            ],
        }
        path = Path(self._tmp.name) / "issue.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_redmine_json_extracts_structured_markers_only(self):
        rc, out = _run(
            [
                "workflow", "watch",
                "--redmine-json", self._write_issue_json(),
                "--store-path", self.store_path,
            ]
        )
        self.assertEqual(rc, 0)
        # Only the journal carrying a structured marker is ingested; the prose-only
        # journals (68978, 69200) yield nothing (no natural-language parse).
        self.assertIn("redmine:12672:68989 review_request -> accepted", out)
        self.assertNotIn("redmine:12672:68978", out)
        self.assertNotIn("redmine:12672:69200", out)
        self.assertIn("accepted: 1", out)

    def test_redmine_json_persists_for_resume(self):
        path = self._write_issue_json()
        _run(["workflow", "watch", "--redmine-json", path, "--store-path", self.store_path])
        store = WorkflowRuntimeStore(path=Path(self.store_path))
        self.assertEqual([r.event_id for r in store.read_events()], ["redmine:12672:68989"])

    def test_redmine_json_dedup_on_rerun(self):
        path = self._write_issue_json()
        _run(["workflow", "watch", "--redmine-json", path, "--store-path", self.store_path])
        rc, out = _run(
            ["workflow", "watch", "--redmine-json", path, "--store-path", self.store_path]
        )
        self.assertEqual(rc, 0)
        self.assertIn("accepted: 0 suppressed: 1", out)

    def test_nested_redmine_rest_shape_is_read(self):
        # The standard /issues/<id>.json?include=journals shape nests journals under the
        # issue (review j#69006): it must not silently drop them.
        payload = {
            "issue": {
                "id": "12672",
                "journals": [
                    {
                        "id": "68989",
                        "notes": (
                            "[mozyo:handoff:source=redmine:issue=12672:journal=68989:"
                            "kind=review_request:to=codex]"
                        ),
                    }
                ],
            }
        }
        path = Path(self._tmp.name) / "rest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        rc, out = _run(
            ["workflow", "watch", "--redmine-json", str(path), "--store-path", self.store_path]
        )
        self.assertEqual(rc, 0)
        self.assertIn("redmine:12672:68989 review_request -> accepted", out)
        self.assertIn("accepted: 1", out)


class PaneIdNeverEmittedTest(_StoreCase):
    def test_pane_id_is_not_in_output(self):
        rc, out = _run(
            [
                "workflow", "watch",
                "--marker", "12672:68978:review_request",
                "--route-identity",
                "route_id=r1,issue=12672,ws=ws1,role=codex,pane_name=audit,pane_id=%999",
                "--store-path", self.store_path,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("%999", out)
        # The public pointer is emitted instead.
        self.assertIn("pane_name=audit", out)


if __name__ == "__main__":
    unittest.main()
