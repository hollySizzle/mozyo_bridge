"""OTel event store / receiver / activity tests (Redmine #11672 / #11673).

Covers: OTLP/JSON decode for the three signals, allowlist attribute
filtering (prompt-shaped keys and log bodies never persisted), SQLite
schema creation / single-writer insert / prune / read-side degradation,
the activity / idle / unknown judgement (silence is never death), and an
end-to-end localhost receiver round trip including /healthz, gzip, and
the protobuf-without-extra 415. Everything runs against temp dirs and an
ephemeral port — no real agents, no real ~/.mozyo_bridge.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.otel_receiver import (
    build_server,
    decode_otlp_json,
)
from mozyo_bridge.domain.agent_activity import (
    STATE_ACTIVE,
    STATE_IDLE,
    STATE_UNKNOWN,
    activity_state_for,
    classify_event,
    summarize_activity,
)
from mozyo_bridge.otel_store import (
    OtelEvent,
    OtelEventStore,
    filter_attributes,
)


def _attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def logs_payload(
    *,
    event_name: str = "claude_code.api_request",
    session: str = "sess-1",
    cwd: str = "/repo",
    extra_attrs: list | None = None,
    body: dict | None = None,
) -> dict:
    record = {
        "timeUnixNano": "1765459200000000000",
        "eventName": event_name,
        "attributes": [
            _attr("session.id", session),
            _attr("cwd", cwd),
            {"key": "input_tokens", "value": {"intValue": "120"}},
            *(extra_attrs or []),
        ],
    }
    if body is not None:
        record["body"] = body
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        _attr("service.name", "claude-code"),
                        {"key": "process.pid", "value": {"intValue": "4242"}},
                    ]
                },
                "scopeLogs": [{"logRecords": [record]}],
            }
        ]
    }


class FilterAttributesTest(unittest.TestCase):
    def test_allowlist_keeps_usage_and_identity_only(self) -> None:
        filtered = filter_attributes(
            {
                "session.id": "s",
                "input_tokens": 12,
                "model": "claude-x",
                "unlisted.key": "dropped",
                "nested": {"deep": "dropped"},
            }
        )
        self.assertEqual(
            {"session.id": "s", "input_tokens": 12, "model": "claude-x"},
            filtered,
        )

    def test_deny_tokens_beat_the_allowlist(self) -> None:
        # Even if a prompt-shaped key were ever allowlisted by mistake,
        # the deny tokens drop it: there is no opt-in path for content.
        for key in (
            "prompt",
            "user_prompt",
            "prompt_text",
            "message.content",
            "api_key",
            "authorization",
        ):
            self.assertEqual({}, filter_attributes({key: "secret stuff"}))


class DecodeOtlpJsonTest(unittest.TestCase):
    def test_log_record_decodes_with_identity_and_no_body(self) -> None:
        events = decode_otlp_json(
            "logs",
            logs_payload(body={"stringValue": "the actual prompt text"}),
        )
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("claude_code.api_request", event.event_name)
        self.assertEqual("claude-code", event.service_name)
        self.assertEqual("sess-1", event.session_id)
        self.assertEqual("4242", event.pid)
        self.assertEqual("/repo", event.cwd)
        self.assertEqual(120, event.attrs["input_tokens"])
        # The log body is never read into the event.
        payload_text = json.dumps(event.as_payload())
        self.assertNotIn("actual prompt text", payload_text)

    def test_prompt_shaped_attribute_is_not_persisted(self) -> None:
        events = decode_otlp_json(
            "logs",
            logs_payload(extra_attrs=[_attr("prompt", "do the thing")]),
        )
        self.assertNotIn("prompt", events[0].attrs)
        self.assertNotIn("do the thing", json.dumps(events[0].as_payload()))

    def test_metric_datapoints_become_events(self) -> None:
        payload = {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [_attr("service.name", "claude-code")]
                    },
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "sum": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": "1765459200000000000",
                                                "asInt": "55",
                                                "attributes": [
                                                    _attr("session.id", "sess-9"),
                                                    _attr("type", "input"),
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        events = decode_otlp_json("metrics", payload)
        self.assertEqual(1, len(events))
        self.assertEqual("claude_code.token.usage", events[0].event_name)
        self.assertEqual("sess-9", events[0].session_id)
        self.assertEqual("input", events[0].attrs["type"])

    def test_span_decodes_via_end_time(self) -> None:
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [_attr("service.name", "codex")]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "name": "tool.exec",
                                    "endTimeUnixNano": "1765459200000000000",
                                    "attributes": [],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        events = decode_otlp_json("traces", payload)
        self.assertEqual("tool.exec", events[0].event_name)
        self.assertEqual("codex", events[0].service_name)
        self.assertIsNotNone(events[0].event_time)


class OtelEventStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "otel-events.sqlite"
        self.store = OtelEventStore(self.db)
        self.addCleanup(self.store.close)

    def test_insert_and_read_round_trip(self) -> None:
        events = decode_otlp_json("logs", logs_payload())
        self.assertEqual(1, self.store.insert_events(events))
        self.assertTrue(self.db.exists())
        read = self.store.recent_events()
        self.assertEqual(1, len(read))
        self.assertEqual("claude_code.api_request", read[0].event_name)
        counts = self.store.counts()
        self.assertEqual(1, counts["total"])
        self.assertIsNotNone(counts["last_write"])

    def test_latest_per_source_collapses_history(self) -> None:
        for index in range(3):
            self.store.insert_events(
                decode_otlp_json(
                    "logs",
                    logs_payload(event_name=f"event-{index}", session="sess-a"),
                )
            )
        self.store.insert_events(
            decode_otlp_json("logs", logs_payload(session="sess-b"))
        )
        latest = self.store.latest_per_source()
        self.assertEqual(2, len(latest))
        by_session = {event.session_id: event for event in latest}
        self.assertEqual("event-2", by_session["sess-a"].event_name)

    def test_prune_drops_expired_events(self) -> None:
        old = OtelEvent(
            signal="logs",
            event_name="old",
            received_at="2020-01-01T00:00:00+00:00",
        )
        self.store.insert_events([old])
        self.store.insert_events(decode_otlp_json("logs", logs_payload()))
        self.assertEqual(1, self.store.prune(retention_days=7))
        names = [event.event_name for event in self.store.recent_events()]
        self.assertNotIn("old", names)

    def test_missing_or_corrupt_store_reads_as_empty(self) -> None:
        absent = OtelEventStore(Path(self._tmp.name) / "absent.sqlite")
        self.assertEqual([], absent.recent_events())
        self.assertEqual(0, absent.counts()["total"])
        corrupt_path = Path(self._tmp.name) / "corrupt.sqlite"
        corrupt_path.write_text("not a database at all")
        corrupt = OtelEventStore(corrupt_path)
        self.assertEqual([], corrupt.recent_events())


class AgentActivityTest(unittest.TestCase):
    def _event(self, *, age_seconds: float, session: str = "s") -> OtelEvent:
        moment = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return OtelEvent(
            signal="logs",
            event_name="claude_code.api_request",
            received_at=moment.isoformat(timespec="seconds"),
            service_name="claude-code",
            session_id=session,
            pid="4242",
            cwd="/repo",
        )

    def test_recent_event_is_active(self) -> None:
        record = classify_event(self._event(age_seconds=5))
        self.assertEqual(STATE_ACTIVE, record.state)
        self.assertEqual({"pid": "4242", "cwd": "/repo"}, record.match_hints)

    def test_silence_is_idle_never_dead(self) -> None:
        record = classify_event(self._event(age_seconds=600))
        self.assertEqual(STATE_IDLE, record.state)

    def test_unparseable_timestamp_is_unknown(self) -> None:
        event = OtelEvent(signal="logs", event_name="x", received_at="bogus")
        self.assertEqual(STATE_UNKNOWN, classify_event(event).state)

    def test_summarize_and_hint_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OtelEventStore(Path(tmp) / "db.sqlite")
            try:
                store.insert_events([self._event(age_seconds=5)])
                records = summarize_activity(store)
            finally:
                store.close()
        self.assertEqual(1, len(records))
        self.assertEqual(
            STATE_ACTIVE, activity_state_for(records, pid="4242")
        )
        self.assertEqual(
            STATE_ACTIVE, activity_state_for(records, cwd="/repo")
        )
        # No telemetry for a unit is unknown — degradation to the tmux
        # liveness layer, never a death claim.
        self.assertEqual(
            STATE_UNKNOWN, activity_state_for(records, pid="999")
        )

    def test_empty_store_summarizes_to_no_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OtelEventStore(Path(tmp) / "db.sqlite")
            self.assertEqual([], summarize_activity(store))


class ReceiverEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "otel-events.sqlite"
        # Port 0 = ephemeral; the bound port is read back from the server.
        self.server = build_server(host="127.0.0.1", port=0, db_path=self.db)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _post(
        self,
        path: str,
        body: bytes,
        *,
        content_type: str = "application/json",
        gzip_body: bool = False,
    ):
        headers = {"Content-Type": content_type}
        if gzip_body:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def test_logs_post_persists_and_healthz_reports(self) -> None:
        status, _ = self._post(
            "/v1/logs", json.dumps(logs_payload()).encode("utf-8")
        )
        self.assertEqual(200, status)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/healthz", timeout=5
        ) as response:
            health = json.loads(response.read())
        self.assertTrue(health["ok"])
        self.assertEqual(1, health["total"])
        read_store = OtelEventStore(self.db)
        self.assertEqual(
            "claude_code.api_request",
            read_store.recent_events()[0].event_name,
        )

    def test_gzip_body_is_accepted(self) -> None:
        status, _ = self._post(
            "/v1/logs",
            json.dumps(logs_payload()).encode("utf-8"),
            gzip_body=True,
        )
        self.assertEqual(200, status)

    def test_protobuf_without_extra_is_415_with_remediation(self) -> None:
        with patch(
            "mozyo_bridge.application.otel_receiver.decode_otlp_protobuf",
            return_value=None,
        ):
            status, payload = self._post(
                "/v1/logs", b"\x0a\x00", content_type="application/x-protobuf"
            )
        self.assertEqual(415, status)
        self.assertIn("mozyo-bridge[otel]", payload["error"])
        self.assertIn("http/json", payload["error"])

    def test_bad_json_is_400_and_unknown_path_404(self) -> None:
        status, _ = self._post("/v1/logs", b"not json{")
        self.assertEqual(400, status)
        status, _ = self._post("/v1/nope", b"{}")
        self.assertEqual(404, status)


class OtelCliTest(unittest.TestCase):
    def test_status_and_activity_json(self) -> None:
        from mozyo_bridge.application.commands import (
            cmd_otel_activity,
            cmd_otel_status,
        )

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite"
            store = OtelEventStore(db)
            store.insert_events(decode_otlp_json("logs", logs_payload()))
            store.close()

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cmd_otel_status(
                    argparse.Namespace(
                        db=str(db), host="127.0.0.1", port="1", as_json=True
                    )
                )
            self.assertEqual(0, code)
            payload = json.loads(out.getvalue())
            self.assertEqual(1, payload["total"])
            self.assertFalse(payload["receiver"]["reachable"])

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cmd_otel_activity(
                    argparse.Namespace(db=str(db), window=None, as_json=True)
                )
            self.assertEqual(0, code)
            records = json.loads(out.getvalue())
            self.assertEqual(1, len(records))
            self.assertIn(records[0]["state"], ("active", "idle"))
            self.assertEqual(
                sorted(records[0]),
                [
                    "last_event_at",
                    "last_event_name",
                    "match_hints",
                    "seconds_since_event",
                    "service_name",
                    "session_id",
                    "state",
                ],
            )


if __name__ == "__main__":
    unittest.main()
