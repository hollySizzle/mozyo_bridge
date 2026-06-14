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
        self.assertEqual(
            {
                "pid": "4242",
                "cwd": "/repo",
                "session": None,
                "agent": None,
                "workspace_id": None,
            },
            record.match_hints,
        )

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


class ReceiverLoopbackGateTest(unittest.TestCase):
    def test_non_loopback_bind_is_rejected(self) -> None:
        # Review #56128 finding 2: the receiver is localhost-only by
        # contract; a wildcard bind must be refused at the library layer.
        from mozyo_bridge.application.otel_receiver import (
            OtelReceiverError,
            build_server,
        )

        with tempfile.TemporaryDirectory() as tmp:
            for host in ("0.0.0.0", "::", "192.168.1.10", "example.test"):
                with self.assertRaises(OtelReceiverError, msg=host):
                    build_server(
                        host=host, port=0, db_path=Path(tmp) / "db.sqlite"
                    )

    def test_loopback_spellings_are_accepted(self) -> None:
        from mozyo_bridge.application.otel_receiver import build_server

        # 127.0.0.2 passes the validator but is not bindable on default
        # macOS, so only actually bind the universally configured ones.
        with tempfile.TemporaryDirectory() as tmp:
            for host in ("127.0.0.1", "localhost"):
                server = build_server(
                    host=host, port=0, db_path=Path(tmp) / "db.sqlite"
                )
                server.server_close()

    def test_cli_serve_dies_on_non_loopback_host(self) -> None:
        from mozyo_bridge.application.commands import cmd_otel_serve

        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    cmd_otel_serve(
                        argparse.Namespace(
                            host="0.0.0.0",
                            port="0",
                            db=str(Path(tmp) / "db.sqlite"),
                        )
                    )
            self.assertIn("localhost-only", stderr.getvalue())


class PackagingMetadataTest(unittest.TestCase):
    def test_optional_dependencies_carry_only_dependency_extras(self) -> None:
        # Review #56128 finding 1: a TOML table-placement mistake moved
        # `keywords` / `classifiers` under [project.optional-dependencies],
        # breaking wheel builds. Pin the table memberships.
        try:
            import tomllib
        except ImportError:  # Python 3.10
            import tomli as tomllib

        pyproject = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = pyproject["project"]
        self.assertIn("keywords", project)
        self.assertIn("classifiers", project)
        extras = project["optional-dependencies"]
        self.assertEqual(["otel"], sorted(extras))
        for requirement in extras["otel"]:
            # Every extras entry must look like a PEP 508 requirement,
            # not orphaned project metadata.
            self.assertRegex(requirement, r"^[A-Za-z0-9_.-]+[><=~!]")


class BootstrapInjectionTest(unittest.TestCase):
    """Redmine #11676: OTel env rides on the agent launch command."""

    def test_bootstrap_env_carries_join_keys_and_never_prompt_logging(self) -> None:
        from mozyo_bridge.application.commands import otel_bootstrap_env

        env = otel_bootstrap_env("claude", "mozyo-demo", cwd=None)
        self.assertEqual("http/json", env["OTEL_EXPORTER_OTLP_PROTOCOL"])
        self.assertEqual(
            "http://127.0.0.1:4318", env["OTEL_EXPORTER_OTLP_ENDPOINT"]
        )
        self.assertEqual("1", env["CLAUDE_CODE_ENABLE_TELEMETRY"])
        self.assertEqual(
            "mozyo.session=mozyo-demo,mozyo.agent=claude",
            env["OTEL_RESOURCE_ATTRIBUTES"],
        )
        # Prompt-content recording stays OFF by contract: the variable
        # must never even be set.
        self.assertNotIn("OTEL_LOG_USER_PROMPTS", env)

    def test_bootstrap_env_includes_workspace_id_when_registered(self) -> None:
        from mozyo_bridge.application.commands import otel_bootstrap_env
        from mozyo_bridge.workspace_registry import register_workspace

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            with patch.dict(
                "os.environ", {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ):
                registered = register_workspace(repo, home=home)
                env = otel_bootstrap_env("codex", "mozyo-x", cwd=str(repo))
        self.assertIn(
            f"mozyo.workspace_id={registered.record.workspace_id}",
            env["OTEL_RESOURCE_ATTRIBUTES"],
        )

    def test_new_agent_window_launches_with_env_wrapper(self) -> None:
        from mozyo_bridge.application.commands import new_agent_window

        captured: list[tuple] = []

        def fake_run_tmux(*args, check: bool = True):
            captured.append(args)
            return argparse.Namespace(returncode=0, stdout="%5\n", stderr="")

        # new_agent_window now records a best-effort desired-state event
        # (Redmine #11726). Pin MOZYO_BRIDGE_HOME to a temp dir so that
        # append lands in a throwaway managed-events.sqlite and never
        # pollutes the operator's real home DB.
        with tempfile.TemporaryDirectory() as tmp, \
            patch.dict(
                "os.environ",
                {
                    "MOZYO_BRIDGE_HOME": str(Path(tmp) / "home"),
                    # Standalone window keeps the historical bare `claude`
                    # launch (no cockpit policy default), so neutralize any
                    # MOZYO_CLAUDE_PERMISSION_MODE the operator exported in
                    # their shell (#11925 override rail) to keep this test
                    # hermetic regardless of the launching environment.
                    "MOZYO_CLAUDE_PERMISSION_MODE": "",
                },
                clear=False,
            ), \
            patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.application.commands.run_tmux",
                side_effect=fake_run_tmux,
            ):
            pane = new_agent_window("claude", "mozyo-demo")
        self.assertEqual("%5", pane)
        command = captured[0][-1]
        self.assertTrue(command.startswith("env "), command)
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318", command)
        self.assertIn("mozyo.session=mozyo-demo,mozyo.agent=claude", command)
        self.assertTrue(command.endswith(" claude"), command)
        self.assertNotIn("OTEL_LOG_USER_PROMPTS", command)


class ClaudePermissionModeLaunchTest(unittest.TestCase):
    """Redmine #11857: opt-in `--permission-mode` for managed Claude panes.

    The launch command is built once in ``_agent_launch_command`` and
    fans out to every pane chokepoint (cockpit / layout / sublane /
    standalone window), so asserting on that builder covers all of them.
    """

    def _command(self, agent: str, env: dict[str, str]) -> str:
        from mozyo_bridge.application.commands import _agent_launch_command

        with patch.dict("os.environ", env, clear=False):
            return _agent_launch_command(agent, "mozyo-demo", cwd=None)

    def test_unset_keeps_bare_claude_launch(self) -> None:
        # Unset (the historical default) appends no flag: existing
        # behavior must never change silently.
        env = {"MOZYO_CLAUDE_PERMISSION_MODE": ""}
        command = self._command("claude", env)
        self.assertTrue(command.endswith(" claude"), command)
        self.assertNotIn("--permission-mode", command)

    def test_auto_mode_appended_for_claude(self) -> None:
        env = {"MOZYO_CLAUDE_PERMISSION_MODE": "auto"}
        command = self._command("claude", env)
        self.assertTrue(
            command.endswith(" claude --permission-mode auto"), command
        )

    def test_blank_whitespace_value_is_treated_as_unset(self) -> None:
        env = {"MOZYO_CLAUDE_PERMISSION_MODE": "  "}
        command = self._command("claude", env)
        self.assertTrue(command.endswith(" claude"), command)
        self.assertNotIn("--permission-mode", command)

    def test_codex_pane_is_never_affected(self) -> None:
        # The flag is Claude-only; Codex launches stay untouched even when
        # the operator has the env var exported in their shell.
        env = {"MOZYO_CLAUDE_PERMISSION_MODE": "auto"}
        command = self._command("codex", env)
        self.assertTrue(command.endswith(" codex"), command)
        self.assertNotIn("--permission-mode", command)

    def test_other_valid_modes_are_accepted(self) -> None:
        for mode in ("acceptEdits", "bypassPermissions", "default", "dontAsk", "plan"):
            with self.subTest(mode=mode):
                env = {"MOZYO_CLAUDE_PERMISSION_MODE": mode}
                command = self._command("claude", env)
                self.assertTrue(
                    command.endswith(f" claude --permission-mode {mode}"), command
                )

    def test_invalid_mode_is_a_hard_error(self) -> None:
        # A typo must fail loudly rather than silently launch a
        # default-permission pane the operator did not intend.
        env = {"MOZYO_CLAUDE_PERMISSION_MODE": "autopilot"}
        with self.assertRaises(SystemExit):
            self._command("claude", env)


class InventoryActivityJoinTest(unittest.TestCase):
    """Redmine #11675: activity joins by bootstrap hints onto pane_id rows."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        env_patch = patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _store_event(self, *, session: str, agent: str) -> None:
        from mozyo_bridge.otel_store import OtelEvent, OtelEventStore

        store = OtelEventStore(home=self.home)
        try:
            store.insert_events(
                [
                    OtelEvent(
                        signal="logs",
                        event_name="claude_code.api_request",
                        service_name="claude-code",
                        session_id="cli-sess",
                        attrs={
                            "mozyo.session": session,
                            "mozyo.agent": agent,
                        },
                    )
                ]
            )
        finally:
            store.close()

    def _pane(self, pane_id: str, session: str, agent: str) -> dict:
        return {
            "id": pane_id,
            "location": f"{session}:1.0",
            "command": agent,
            "cwd": "",
            "window_name": agent,
            "pane_active": "1",
        }

    def test_matching_source_attaches_and_missing_is_unknown(self) -> None:
        from mozyo_bridge.session_inventory import take_inventory

        self._store_event(session="mozyo-demo", agent="claude")
        snapshot = take_inventory(
            home=self.home,
            panes=[
                self._pane("%1", "mozyo-demo", "claude"),
                self._pane("%2", "mozyo-demo", "codex"),
            ],
        )
        payload = {r.pane_id: r.as_payload() for r in snapshot.records}
        self.assertEqual("active", payload["%1"]["activity"]["state"])
        self.assertEqual("otel", payload["%1"]["activity"]["source"])
        self.assertEqual("unknown", payload["%2"]["activity"]["state"])
        self.assertIsNone(payload["%2"]["activity"]["source"])

    def test_newest_source_wins_when_cli_session_restarts(self) -> None:
        # Review #56160 (High): a restarted agent CLI mints a new
        # session.id but keeps the same bootstrap hints, so two OTel
        # sources share one (mozyo.session, mozyo.agent) key. The stale
        # pre-restart source must never override the live one.
        from datetime import datetime, timedelta, timezone

        from mozyo_bridge.otel_store import OtelEvent, OtelEventStore
        from mozyo_bridge.session_inventory import take_inventory

        now = datetime.now(timezone.utc)
        store = OtelEventStore(home=self.home)
        try:
            store.insert_events(
                [
                    OtelEvent(
                        signal="logs",
                        event_name="old-idle",
                        service_name="claude-code",
                        session_id="cli-old",
                        received_at=(now - timedelta(minutes=10)).isoformat(
                            timespec="seconds"
                        ),
                        attrs={
                            "mozyo.session": "mozyo-demo",
                            "mozyo.agent": "claude",
                        },
                    ),
                    OtelEvent(
                        signal="logs",
                        event_name="new-active",
                        service_name="claude-code",
                        session_id="cli-new",
                        received_at=now.isoformat(timespec="seconds"),
                        attrs={
                            "mozyo.session": "mozyo-demo",
                            "mozyo.agent": "claude",
                        },
                    ),
                ]
            )
        finally:
            store.close()
        snapshot = take_inventory(
            home=self.home,
            panes=[self._pane("%1", "mozyo-demo", "claude")],
        )
        activity = snapshot.records[0].activity or {}
        self.assertEqual("active", activity.get("state"))
        self.assertEqual("new-active", activity.get("last_event_name"))

    def test_ambiguous_session_kind_pair_stays_unknown(self) -> None:
        # Two claude panes in one session cannot be attributed honestly.
        from mozyo_bridge.session_inventory import take_inventory

        self._store_event(session="mozyo-demo", agent="claude")
        snapshot = take_inventory(
            home=self.home,
            panes=[
                self._pane("%1", "mozyo-demo", "claude"),
                self._pane("%9", "mozyo-demo", "claude"),
            ],
        )
        for record in snapshot.records:
            self.assertEqual("unknown", (record.activity or {}).get("state"))

    def test_grouped_views_still_fold_and_join_via_any_view(self) -> None:
        from mozyo_bridge.session_inventory import take_inventory

        self._store_event(session="mozyo-demo", agent="claude")
        snapshot = take_inventory(
            home=self.home,
            panes=[
                self._pane("%1", "alias-view", "claude"),
                self._pane("%1", "mozyo-demo", "claude"),
            ],
        )
        self.assertEqual(1, len(snapshot.records))
        self.assertEqual(
            "active", (snapshot.records[0].activity or {}).get("state")
        )


class DoctorOtelSectionTest(unittest.TestCase):
    def test_receiver_down_is_by_design_and_gaps_are_listed(self) -> None:
        from mozyo_bridge.application.doctor import doctor_otel_section

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            panes = [
                {
                    "id": "%1",
                    "location": "mozyo-demo:1.0",
                    "command": "claude",
                    "cwd": "",
                    "window_name": "claude",
                    "pane_active": "1",
                },
            ]
            with patch.dict(
                "os.environ", {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ), patch(
                "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
                return_value=panes,
            ):
                section = doctor_otel_section(argparse.Namespace())
        self.assertEqual("ok", section["status"])
        self.assertFalse(section["receiver_reachable"])
        self.assertTrue(
            any("BY DESIGN" in note for note in section["notes"])
        )
        self.assertEqual(
            [{"pane_id": "%1", "session": "mozyo-demo", "agent": "claude"}],
            section["unobserved_agents"],
        )


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
