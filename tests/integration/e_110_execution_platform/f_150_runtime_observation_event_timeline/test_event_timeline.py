"""Tests for the consumer event timeline source (Redmine #11813).

Covers the projection envelope, the redaction posture (no prompt bodies,
no full filesystem paths), the store-side `query_events` filters, and the
`events tail` / `events query` CLI faces (text + JSON).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import sys

# Self-contained src bootstrap so isolated discovery (unittest discover
# scoped to this subpackage or a single file) imports mozyo_bridge without
# relying on a sibling test inserting src first (Redmine #12490 j#64426).
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.application.commands import cmd_events_query, cmd_events_tail
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.event_timeline import (
    LAYER_RUNTIME,
    project_event,
    project_rows,
)
from mozyo_bridge.otel_store import OtelEvent, OtelEventStore


def _event(**overrides) -> OtelEvent:
    base = dict(
        signal="logs",
        event_name="api_request",
        service_name="claude-code",
        session_id="sess-abc-123456",
        cwd="/workspace/project-alpha",
        received_at="2026-06-14T03:00:00+00:00",
        attrs={
            "mozyo.agent": "claude",
            "mozyo.session": "lane-1",
            "total_tokens": 1234,
            "cost_usd": 0.04,
            "model": "opus",
        },
    )
    base.update(overrides)
    return OtelEvent(**base)


class ProjectionTest(unittest.TestCase):
    def test_runtime_envelope_shape(self) -> None:
        ev = project_event(_event(), row_id=7)
        self.assertEqual("7", ev.id)
        self.assertEqual(LAYER_RUNTIME, ev.source_layer)
        self.assertEqual("2026-06-14T03:00:00+00:00", ev.observed_at)
        self.assertEqual("api", ev.category)
        self.assertEqual("claude-code", ev.agent["service"])
        self.assertEqual("claude", ev.agent["mozyo_agent"])
        self.assertEqual(1234, ev.usage["total_tokens"])
        self.assertEqual(0.04, ev.usage["cost_usd"])
        self.assertEqual("claude-code api_request", ev.summary)
        # Anchor is pointer-only and unset for runtime events: the timeline
        # never copies durable-record content.
        self.assertIsNone(ev.anchor)

    def test_category_mapping(self) -> None:
        cases = {
            ("metrics", "token.usage"): "usage",
            ("logs", "tool_decision"): "tool",
            ("logs", "api_request"): "api",
            ("logs", "session_start"): "session",
            ("logs", "mystery"): "event",
        }
        for (signal, name), expected in cases.items():
            ev = project_event(_event(signal=signal, event_name=name))
            self.assertEqual(expected, ev.category, f"{signal}/{name}")

    def test_id_falls_back_to_content_hint_without_row_id(self) -> None:
        ev = project_event(_event())
        self.assertEqual("2026-06-14T03:00:00+00:00:api_request", ev.id)


class RedactionTest(unittest.TestCase):
    def test_full_path_never_emitted_only_basename(self) -> None:
        ev = project_event(_event(cwd="/workspace/project-alpha"))
        self.assertEqual("project-alpha", ev.workspace_hint)
        blob = json.dumps(ev.as_payload())
        self.assertNotIn("/workspace", blob)
        self.assertNotIn("/workspace/project-alpha", blob)

    def test_denied_keys_dropped_even_if_persisted(self) -> None:
        # The store persists attrs verbatim; the projection re-asserts the
        # deny boundary so a prompt-shaped attribute that slipped into the
        # store cannot reach a consumer. The sentinel values are neutral
        # (neither personal-home-path nor secret-shaped) so the test itself
        # does not trip the public/private boundary or release scan.
        ev = project_event(
            _event(attrs={
                "prompt": "DROP-PROMPT-SENTINEL",
                "authorization": "DROP-AUTH-SENTINEL",
                "total_tokens": 9,
            })
        )
        blob = json.dumps(ev.as_payload())
        self.assertNotIn("DROP-PROMPT-SENTINEL", blob)
        self.assertNotIn("DROP-AUTH-SENTINEL", blob)
        self.assertEqual(9, ev.usage["total_tokens"])

    def test_missing_cwd_yields_no_hint(self) -> None:
        ev = project_event(_event(cwd=None))
        self.assertIsNone(ev.workspace_hint)


class StoreQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "otel-events.sqlite"
        self.store = OtelEventStore(self.db)
        self.addCleanup(self.store.close)
        self.store.insert_events(
            [
                _event(
                    service_name="claude-code",
                    event_name="early",
                    received_at="2026-06-14T01:00:00+00:00",
                ),
                _event(
                    service_name="codex",
                    event_name="mid",
                    received_at="2026-06-14T02:00:00+00:00",
                ),
                _event(
                    service_name="claude-code",
                    event_name="late",
                    received_at="2026-06-14T03:00:00+00:00",
                ),
            ]
        )

    def test_returns_row_id_and_newest_first(self) -> None:
        rows = self.store.query_events()
        self.assertEqual(3, len(rows))
        # newest first
        self.assertEqual("late", rows[0][1].event_name)
        # row ids are the store's monotonic ids
        self.assertTrue(all(isinstance(row_id, int) for row_id, _ in rows))
        self.assertGreater(rows[0][0], rows[-1][0])

    def test_since_filter(self) -> None:
        rows = self.store.query_events(since="2026-06-14T02:00:00+00:00")
        names = {ev.event_name for _, ev in rows}
        self.assertEqual({"mid", "late"}, names)

    def test_source_filter(self) -> None:
        rows = self.store.query_events(source="codex")
        names = [ev.event_name for _, ev in rows]
        self.assertEqual(["mid"], names)

    def test_limit(self) -> None:
        rows = self.store.query_events(limit=1)
        self.assertEqual(1, len(rows))
        self.assertEqual("late", rows[0][1].event_name)

    def test_project_rows_carries_store_ids(self) -> None:
        rows = self.store.query_events()
        events = project_rows(rows)
        self.assertEqual([str(row_id) for row_id, _ in rows], [e.id for e in events])

    def test_missing_store_reads_empty(self) -> None:
        absent = OtelEventStore(Path(self._tmp.name) / "absent.sqlite")
        self.assertEqual([], absent.query_events())


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "otel-events.sqlite"
        store = OtelEventStore(self.db)
        store.insert_events(
            [
                _event(service_name="codex", event_name="codex_evt",
                       received_at="2026-06-14T02:00:00+00:00"),
                _event(service_name="claude-code", event_name="claude_evt",
                       received_at="2026-06-14T03:00:00+00:00"),
            ]
        )
        store.close()

    def _run(self, func, **kwargs) -> str:
        ns = argparse.Namespace(db=str(self.db), **kwargs)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = func(ns)
        self.assertEqual(0, rc)
        return out.getvalue()

    def test_tail_text(self) -> None:
        text = self._run(cmd_events_tail, limit=None, as_json=False)
        self.assertIn("OBSERVED\tLAYER\tCATEGORY", text)
        self.assertIn("claude_evt", text)
        self.assertIn("project-alpha", text)
        self.assertNotIn("/workspace", text)

    def test_tail_json_envelope(self) -> None:
        payload = json.loads(
            self._run(cmd_events_tail, limit=None, as_json=True)
        )
        self.assertEqual(2, len(payload))
        self.assertEqual("runtime", payload[0]["source_layer"])
        self.assertIn("source_layer", payload[0])
        self.assertIn("agent", payload[0])

    def test_query_source_filter_json(self) -> None:
        payload = json.loads(
            self._run(
                cmd_events_query, since=None, source="codex",
                limit=None, as_json=True,
            )
        )
        self.assertEqual(1, len(payload))
        self.assertEqual("codex_evt", payload[0]["event_name"])

    def test_query_since_filter(self) -> None:
        payload = json.loads(
            self._run(
                cmd_events_query, since="2026-06-14T03:00:00+00:00",
                source=None, limit=None, as_json=True,
            )
        )
        self.assertEqual(["claude_evt"], [e["event_name"] for e in payload])


class ParserTest(unittest.TestCase):
    def test_events_subcommands_register(self) -> None:
        parser = build_parser()
        tail = parser.parse_args(["events", "tail", "--json"])
        self.assertEqual("events", tail.command)
        self.assertEqual("tail", tail.events_command)
        self.assertTrue(tail.as_json)
        query = parser.parse_args(
            ["events", "query", "--source", "codex", "--since", "2026-06-14T00:00:00+00:00"]
        )
        self.assertEqual("query", query.events_command)
        self.assertEqual("codex", query.source)


if __name__ == "__main__":
    unittest.main()
