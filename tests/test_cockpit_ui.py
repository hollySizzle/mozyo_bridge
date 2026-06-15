"""Cockpit Web UI tests (Redmine #11679 / #11680 / #11681 / #11683).

Covers: the daemon-served HTML / units / transitions endpoints, the
reveal / jump actions (structured argv, stale-safe failure, attached
client selection with control-mode demotion), and the transition tracker
(state-change detection, ring buffer bound, no observation on stale
snapshots). Loopback-only bind is pinned in test_otel_store. Everything
runs on an ephemeral port with temp homes — no real tmux mutations.
"""

from __future__ import annotations

import argparse  # noqa: F401  (kept for parity with sibling test modules)
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_ui import (
    CockpitActionError,
    attach_attention,
    jump_to_unit,
    reveal_in_finder,
)
from mozyo_bridge.application.otel_receiver import build_server
from mozyo_bridge.domain.agent_activity import TransitionTracker
from mozyo_bridge.session_inventory import take_inventory


def pane(pane_id: str, session: str, agent: str, cwd: str = "") -> dict:
    return {
        "id": pane_id,
        "location": f"{session}:1.0",
        "command": agent,
        "cwd": cwd,
        "window_name": agent,
        "pane_active": "1",
    }


class CockpitHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        env_patch = patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.server = build_server(
            host="127.0.0.1", port=0, home=self.home
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _get(self, path: str):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5
        ) as response:
            return response.status, response.read()

    def _post(
        self,
        path: str,
        payload: dict,
        *,
        with_token: bool = True,
        content_type: str = "application/json",
        origin: str | None = None,
    ):
        headers = {"Content-Type": content_type}
        if with_token:
            headers["X-Mozyo-Cockpit-Token"] = self.server.cockpit_token
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_index_serves_self_contained_html(self) -> None:
        status, body = self._get("/")
        self.assertEqual(200, status)
        text = body.decode("utf-8")
        self.assertIn("mozyo cockpit", text)
        # Self-contained: no external asset loads (loopback / no-exfil).
        self.assertNotIn("http://", text.replace("http://127.0.0.1", ""))
        self.assertNotIn("https://", text)
        # The per-process action token is embedded for the action header.
        self.assertIn(self.server.cockpit_token, text)

    def test_rendering_never_uses_innerhtml(self) -> None:
        # Review #56197 finding 2: payload strings (workspace / session /
        # path names) are local but untrusted input; the page must build
        # DOM via textContent / createElement so HTML metacharacters in
        # them render as text instead of executing. Pin the approach.
        from mozyo_bridge.application.cockpit_ui import INDEX_HTML_TEMPLATE

        self.assertNotIn("innerHTML", INDEX_HTML_TEMPLATE)
        self.assertNotIn("outerHTML", INDEX_HTML_TEMPLATE)
        self.assertNotIn("insertAdjacentHTML", INDEX_HTML_TEMPLATE)
        self.assertNotIn("document.write", INDEX_HTML_TEMPLATE)
        self.assertIn("textContent", INDEX_HTML_TEMPLATE)
        self.assertIn("createElement", INDEX_HTML_TEMPLATE)

    def test_cross_site_simple_request_never_reaches_action_handler(self) -> None:
        # Review #56197 finding 1 reproduction: a browser simple request
        # (text/plain, foreign Origin, no preflight) must be rejected at
        # the intent gate (415), NOT answered by the action handler (409).
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=[pane("%1", "mozyo-demo", "claude")],
        ):
            status, payload = self._post(
                "/api/actions/jump",
                {"pane_id": "%1"},
                with_token=False,
                content_type="text/plain",
                origin="https://example.invalid",
            )
        self.assertEqual(415, status)
        self.assertNotIn("inventory", payload["error"])
        self.assertIn("application/json", payload["error"])

    def test_action_without_token_is_403(self) -> None:
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=[pane("%1", "mozyo-demo", "claude")],
        ):
            status, payload = self._post(
                "/api/actions/jump", {"pane_id": "%1"}, with_token=False
            )
        self.assertEqual(403, status)
        self.assertIn("token", payload["error"])

    def test_action_with_foreign_origin_is_403_even_with_token(self) -> None:
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=[pane("%1", "mozyo-demo", "claude")],
        ):
            status, payload = self._post(
                "/api/actions/jump",
                {"pane_id": "%1"},
                origin="https://example.invalid",
            )
        self.assertEqual(403, status)
        self.assertIn("cross-origin", payload["error"])

    def test_loopback_prefixed_hostile_origins_are_403(self) -> None:
        # Review #56212: a prefix match admitted Origins whose registrable
        # domain merely STARTS with a loopback string. Exact parsed-host
        # comparison must reject them even with a valid token.
        for origin in (
            "http://localhost.evil.example",
            "http://127.0.0.1.evil.example",
            f"http://localhost.evil.example:{self.port}",
            "https://localhost",  # scheme must be http (the served origin)
        ):
            with patch(
                "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
                return_value=[pane("%1", "mozyo-demo", "claude")],
            ):
                status, payload = self._post(
                    "/api/actions/jump", {"pane_id": "%1"}, origin=origin
                )
            self.assertEqual(403, status, origin)
            self.assertIn("cross-origin", payload["error"])

    def test_action_with_loopback_origin_and_token_reaches_handler(self) -> None:
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=[pane("%1", "mozyo-demo", "claude")],
        ):
            status, payload = self._post(
                "/api/actions/jump",
                {"pane_id": "%404"},
                origin=f"http://127.0.0.1:{self.port}",
            )
        # Past the intent gate; fails on the (intended) stale-pane check.
        self.assertEqual(409, status)
        self.assertIn("no longer in the runtime inventory", payload["error"])

    def test_units_endpoint_returns_inventory_payload(self) -> None:
        panes = [pane("%1", "mozyo-demo", "claude")]
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            status, body = self._get("/api/units")
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertFalse(payload["stale"])
        self.assertEqual(1, len(payload["panes"]))
        self.assertEqual("%1", payload["panes"][0]["pane_id"])
        self.assertIn("activity", payload["panes"][0])

    def test_units_endpoint_attaches_attention_projection(self) -> None:
        # Redmine #12007: the cockpit data source gains the same derived
        # AttentionRecord vocabulary `agents targets --json` already exposes,
        # as an additive fourth layer that never disturbs the existing ones.
        panes = [pane("%1", "mozyo-demo", "claude")]
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            status, body = self._get("/api/units")
        self.assertEqual(200, status)
        payload = json.loads(body)
        row = payload["panes"][0]
        # Additive: the prior layers are untouched.
        self.assertEqual("%1", row["pane_id"])
        self.assertIn("activity", row)
        attention = row["attention"]
        # No durable attention source is wired, so a cleanly-identified pane
        # derives healthy / no_attention_source — never a fabricated gate.
        self.assertEqual("healthy", attention["attention_state"])
        self.assertEqual("no_attention_source", attention["reason_code"])
        self.assertEqual("claude", attention["role"])
        # Public-safe: provenance carries only the tmux pane id, no path/secret.
        self.assertEqual(["tmux:%1"], attention["source_refs"])
        for ref in attention["source_refs"]:
            self.assertNotIn("/", ref)

    def test_attach_attention_is_additive_and_fails_safe(self) -> None:
        # Pure-join unit test: a readable identity → healthy; an unreadable
        # one (role_source unknown) → unknown, never healthy; existing keys
        # are preserved and only `attention` is added.
        payload = {
            "panes": [
                {
                    "pane_id": "%7",
                    "agent_kind": "codex",
                    "role_source": "pane_option",
                    "confidence": "strong",
                    "workspace": {"workspace_id": "ws-codex"},
                    "activity": {"state": "active"},
                },
                {
                    "pane_id": "%8",
                    "agent_kind": "claude",
                    "role_source": "unknown",
                    "confidence": "none",
                    "workspace": None,
                    "activity": {"state": "unknown"},
                },
            ]
        }
        out = attach_attention(payload, observed_at="2026-06-15T00:00:00+00:00")
        readable, unreadable = out["panes"]
        self.assertEqual("healthy", readable["attention"]["attention_state"])
        self.assertEqual("ws-codex", readable["attention"]["workspace_id"])
        # Existing layers preserved (additive only).
        self.assertEqual("active", readable["activity"]["state"])
        # Fail-safe: unreadable identity is unknown, not healthy.
        self.assertEqual("unknown", unreadable["attention"]["attention_state"])
        self.assertEqual(
            "source_unreadable", unreadable["attention"]["reason_code"]
        )

    def test_attach_attention_stale_snapshot_degrades_to_unknown(self) -> None:
        # Review #58888: a stale snapshot (tmux runtime unreadable, cached
        # rows) cannot honestly assert liveness, so even a strong cached
        # identity must derive unknown / source_unreadable, never healthy
        # (cockpit-attention-state.md / runtime-observability-boundary.md).
        payload = {
            "stale": True,
            "panes": [
                {
                    "pane_id": "%9",
                    "agent_kind": "codex",
                    "role_source": "pane_option",
                    "confidence": "strong",
                    "workspace": {"workspace_id": "ws-codex"},
                }
            ],
        }
        out = attach_attention(payload, observed_at="2026-06-15T00:00:00+00:00")
        attention = out["panes"][0]["attention"]
        self.assertEqual("unknown", attention["attention_state"])
        self.assertEqual("source_unreadable", attention["reason_code"])

    def test_units_endpoint_stale_cache_attention_is_unknown(self) -> None:
        # End-to-end: a live poll seeds the inventory cache, then a poll with
        # tmux unavailable serves that cache as stale — the cached claude row's
        # attention must read unknown, not healthy, at the /api/units boundary.
        panes = [pane("%1", "mozyo-demo", "claude")]
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            self._get("/api/units")  # seed the cache from a live snapshot
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=None,  # tmux unavailable -> stale cache snapshot
        ):
            status, body = self._get("/api/units")
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertTrue(payload["stale"])
        self.assertEqual(1, len(payload["panes"]))
        attention = payload["panes"][0]["attention"]
        self.assertEqual("unknown", attention["attention_state"])
        self.assertEqual("source_unreadable", attention["reason_code"])

    def test_transitions_endpoint_reports_observed_changes(self) -> None:
        # First poll establishes baseline (unknown), second poll after an
        # activity change yields a transition.
        from mozyo_bridge.otel_store import OtelEvent, OtelEventStore

        panes = [pane("%1", "mozyo-demo", "claude")]
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            self._get("/api/units")
            store = OtelEventStore(home=self.home)
            try:
                store.insert_events(
                    [
                        OtelEvent(
                            signal="logs",
                            event_name="api_request",
                            service_name="claude-code",
                            session_id="cli-1",
                            attrs={
                                "mozyo.session": "mozyo-demo",
                                "mozyo.agent": "claude",
                            },
                        )
                    ]
                )
            finally:
                store.close()
            self._get("/api/units")
        status, body = self._get("/api/transitions")
        self.assertEqual(200, status)
        transitions = json.loads(body)["transitions"]
        self.assertEqual(1, len(transitions))
        self.assertEqual("unknown", transitions[0]["previous_state"])
        self.assertEqual("active", transitions[0]["state"])
        self.assertEqual("%1", transitions[0]["pane_id"])

    def test_action_with_unknown_pane_is_409_with_refresh_hint(self) -> None:
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=[pane("%1", "mozyo-demo", "claude")],
        ):
            status, payload = self._post(
                "/api/actions/jump", {"pane_id": "%404"}
            )
        self.assertEqual(409, status)
        self.assertIn("no longer in the runtime inventory", payload["error"])

    def test_action_on_stale_snapshot_is_409(self) -> None:
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=None,
        ):
            status, payload = self._post(
                "/api/actions/reveal", {"pane_id": "%1"}
            )
        self.assertEqual(409, status)
        self.assertIn("stale", payload["error"])


class CockpitActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def _panes(self) -> list[dict]:
        return [pane("%1", "mozyo-demo", "claude", cwd=str(self.repo))]

    def test_reveal_runs_structured_open_on_repo_root(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, capture_output, text, check):
            calls.append(argv)
            return type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": ""}
            )()

        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=self._panes(),
        ), patch(
            "mozyo_bridge.application.cockpit_ui.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "mozyo_bridge.application.cockpit_ui.sys.platform", "darwin"
        ):
            result = reveal_in_finder("%1", home=self.home)
        # Structured argv: the path rides as one argument, never through a
        # shell string — spaces / Japanese segments cannot inject.
        self.assertEqual([["open", str(self.repo.resolve())]], calls)
        self.assertEqual("reveal", result["action"])

    def test_jump_switches_most_recent_regular_client(self) -> None:
        tmux_calls: list[tuple] = []

        def fake_run_tmux(*args, check: bool = True):
            tmux_calls.append(args)
            if args[0] == "list-clients":
                return type(
                    "R",
                    (),
                    {
                        "returncode": 0,
                        # control-mode client is newer but demoted; the
                        # regular client wins (jump v1 contract).
                        "stdout": (
                            "200\t1\t/dev/ttys-cc\n"
                            "100\t0\t/dev/ttys-old\n"
                            "150\t0\t/dev/ttys-new\n"
                        ),
                        "stderr": "",
                    },
                )()
            return type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": ""}
            )()

        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=self._panes(),
        ), patch(
            "mozyo_bridge.infrastructure.tmux_client.run_tmux",
            side_effect=fake_run_tmux,
        ):
            result = jump_to_unit("%1", home=self.home)
        self.assertEqual("/dev/ttys-new", result["client"])
        self.assertEqual("mozyo-demo:1", result["target"])
        switch = [c for c in tmux_calls if c[0] == "switch-client"]
        self.assertEqual(
            [("switch-client", "-c", "/dev/ttys-new", "-t", "mozyo-demo:1")],
            switch,
        )

    def test_jump_without_attached_client_fails_safely(self) -> None:
        def fake_run_tmux(*args, check: bool = True):
            return type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": ""}
            )()

        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=self._panes(),
        ), patch(
            "mozyo_bridge.infrastructure.tmux_client.run_tmux",
            side_effect=fake_run_tmux,
        ):
            with self.assertRaises(CockpitActionError) as ctx:
                jump_to_unit("%1", home=self.home)
        self.assertIn("no attached tmux client", str(ctx.exception))

    def test_reveal_refuses_missing_directory(self) -> None:
        panes = [pane("%1", "mozyo-demo", "claude", cwd="/no/such/dir")]
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            with self.assertRaises(CockpitActionError):
                reveal_in_finder("%1", home=self.home)


class TransitionTrackerTest(unittest.TestCase):
    def _records(self, state: str):
        from mozyo_bridge.otel_store import OtelEvent, OtelEventStore

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            if state != "unknown":
                store = OtelEventStore(home=home)
                try:
                    store.insert_events(
                        [
                            OtelEvent(
                                signal="logs",
                                event_name="api_request",
                                service_name="claude-code",
                                session_id="cli-1",
                                attrs={
                                    "mozyo.session": "mozyo-demo",
                                    "mozyo.agent": "claude",
                                },
                            )
                        ]
                    )
                finally:
                    store.close()
            snapshot = take_inventory(
                home=home, panes=[pane("%1", "mozyo-demo", "claude")]
            )
            return list(snapshot.records)

    def test_state_change_is_recorded_once(self) -> None:
        tracker = TransitionTracker()
        self.assertEqual([], tracker.observe(self._records("unknown")))
        transitions = tracker.observe(self._records("active"))
        self.assertEqual(1, len(transitions))
        self.assertEqual("unknown", transitions[0].previous_state)
        self.assertEqual("active", transitions[0].state)
        # Same state again: no new transition.
        self.assertEqual([], tracker.observe(self._records("active")))
        self.assertEqual(1, len(tracker.recent()))

    def test_ring_buffer_is_bounded(self) -> None:
        tracker = TransitionTracker(max_transitions=3)
        for index in range(5):
            state = "active" if index % 2 == 0 else "unknown"
            tracker.observe(self._records(state))
        self.assertLessEqual(len(tracker.recent(limit=100)), 3)


if __name__ == "__main__":
    unittest.main()
