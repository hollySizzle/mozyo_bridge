"""Served cockpit HTTP-endpoint wiring tests (Redmine #11679 / #11680 / #11681).

Focused on the daemon-served HTTP surface (``otel_receiver``): the ``/api/units``
endpoint and its additive attention / observation join layers, the action intent
gate (token + origin), the action endpoints' stale-/unknown-pane failures, the
transitions endpoint, and the transition tracker. After the #12323 cockpit split
the page rendering lives in ``test_cockpit_page``, the pure served-API payload
contract in ``test_cockpit_payload``, and the pane-centric action / preflight
bridge in ``test_cockpit_actions``; this file pins how the receiver wires those
pieces together over HTTP. Loopback-only bind is pinned in test_otel_store.
Everything runs on an ephemeral port with temp homes — no real tmux mutations.
"""

from __future__ import annotations

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

    def test_units_endpoint_attaches_observation_envelope(self) -> None:
        # Redmine #12225: an additive top-level `observation` envelope carries
        # the runtime observation snapshot freshness (observed_at / source /
        # method / freshness / readability / display_state) for the displayed
        # snapshot. A live snapshot is fresh + readable + healthy.
        from mozyo_bridge.domain import runtime_observation as ro

        panes = [pane("%1", "mozyo-demo", "claude")]
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            status, body = self._get("/api/units")
        self.assertEqual(200, status)
        payload = json.loads(body)
        # Additive: the existing layers are untouched.
        self.assertFalse(payload["stale"])
        self.assertIn("activity", payload["panes"][0])
        obs = payload["observation"]
        self.assertEqual(ro.SOURCE_TMUX, obs["source"])
        self.assertEqual(ro.METHOD_LIVE_QUERY, obs["method"])
        self.assertEqual(ro.FRESHNESS_FRESH, obs["freshness"])
        self.assertEqual(ro.READABILITY_READABLE, obs["readability"])
        self.assertEqual(ro.DISPLAY_STATE_HEALTHY, obs["display_state"])
        self.assertIsNotNone(obs["observed_at"])
        # The required envelope fields the UI renders are all present.
        for key in (
            "observed_at",
            "source",
            "method",
            "freshness",
            "readability",
            "strength",
            "stale_reason",
            "display_state",
        ):
            self.assertIn(key, obs)
        # No truth-like generic field leaks into the observation envelope.
        self.assertEqual([], ro.forbidden_generic_fields(obs))

    def test_units_observation_stale_is_fail_closed(self) -> None:
        # End-to-end: a stale cache snapshot must never read as healthy. The
        # observation envelope derives reload_required (readability is partial
        # for a cache projection), the visible "this is cached" label stays in
        # the snapshot, and no truth-like field appears
        # (runtime-observability-boundary.md fail-safe semantics).
        from mozyo_bridge.domain import runtime_observation as ro

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
        obs = payload["observation"]
        self.assertNotEqual(ro.DISPLAY_STATE_HEALTHY, obs["display_state"])
        self.assertEqual(ro.DISPLAY_STATE_RELOAD_REQUIRED, obs["display_state"])
        self.assertEqual(ro.SOURCE_CACHE, obs["source"])
        self.assertEqual(ro.READABILITY_PARTIAL, obs["readability"])
        self.assertEqual([], ro.forbidden_generic_fields(obs))

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
