"""Served cockpit HTML / static page + browser-smoke tests (Redmine #12323 split).

Focused on :mod:`mozyo_bridge.application.cockpit_page`: the served document's
structure, safety properties (DOM-only rendering, no off-host assets), and
visual-fit / browser-smoke contract (Redmine #12298). Split out of
``test_cockpit_ui`` and ``test_grouped_served_cockpit`` (#12323) so the
page-rendering responsibility is tested on its own, separate from the served-API
payload contract and the action / preflight bridge.

``ServedCockpitSmokeTest`` asserts the served document is a non-blank page whose
core controls, freshness / unavailable state, and unit rows stay visible and
contained at desktop and mobile-ish viewport widths, with no off-host assets and
a stable ``/api/units`` data contract. Those assertions pin the structural
anchors and CSS fit properties the rendering must keep; a real-browser pass is
recorded out of band in the issue journal.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_page import INDEX_HTML_TEMPLATE
from mozyo_bridge.application.otel_receiver import build_server


def pane(pane_id: str, session: str, agent: str, cwd: str = "") -> dict:
    return {
        "id": pane_id,
        "location": f"{session}:1.0",
        "command": agent,
        "cwd": cwd,
        "window_name": agent,
        "pane_active": "1",
    }


def _js_string_list(template: str, const_name: str) -> list[str]:
    """Extract a ``const NAME = ["a", "b"];`` whitelist from the page script.

    Keeps the test's notion of the front-end's state vocabulary in sync with the
    served template instead of hard-coding a copy that can silently drift.
    """
    match = re.search(rf"const {const_name} = \[([^\]]*)\];", template)
    assert match, f"{const_name} whitelist not found in served template"
    return re.findall(r'"([^"]+)"', match.group(1))


class IndexHtmlTemplateTest(unittest.TestCase):
    """Pure assertions on the served template string (no HTTP server needed)."""

    def test_rendering_never_uses_innerhtml(self) -> None:
        # Review #56197 finding 2: payload strings (workspace / session /
        # path names) are local but untrusted input; the page must build
        # DOM via textContent / createElement so HTML metacharacters in
        # them render as text instead of executing. Pin the approach.
        self.assertNotIn("innerHTML", INDEX_HTML_TEMPLATE)
        self.assertNotIn("outerHTML", INDEX_HTML_TEMPLATE)
        self.assertNotIn("insertAdjacentHTML", INDEX_HTML_TEMPLATE)
        self.assertNotIn("document.write", INDEX_HTML_TEMPLATE)
        self.assertIn("textContent", INDEX_HTML_TEMPLATE)
        self.assertIn("createElement", INDEX_HTML_TEMPLATE)

    def test_index_has_reload_button_and_freshness_display(self) -> None:
        # Redmine #12225: the page exposes a manual Reload affordance and a
        # freshness line, rendered via DOM APIs (whitelisted display-state
        # class), never innerHTML.
        self.assertIn('id="reload"', INDEX_HTML_TEMPLATE)
        self.assertIn('id="observation"', INDEX_HTML_TEMPLATE)
        self.assertIn("renderObservation", INDEX_HTML_TEMPLATE)
        self.assertIn("KNOWN_DISPLAY_STATES", INDEX_HTML_TEMPLATE)
        self.assertIn("data.observation", INDEX_HTML_TEMPLATE)
        # The reload button drives an explicit re-fetch.
        self.assertIn(
            "getElementById('reload').addEventListener('click', refresh)",
            INDEX_HTML_TEMPLATE,
        )
        # Still DOM-API only (no HTML injection sink introduced).
        self.assertNotIn("innerHTML", INDEX_HTML_TEMPLATE)


class ServedCockpitSmokeTest(unittest.TestCase):
    """Page-level browser smoke against the daemon-served cockpit document."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        env_patch = patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.server = build_server(host="127.0.0.1", port=0, home=self.home)
        self.port = self.server.server_address[1]
        threading.Thread(
            target=self.server.serve_forever, daemon=True
        ).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _get(self, path: str):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5
        ) as response:
            return response.status, response.read()

    def _served_html(self) -> str:
        status, body = self._get("/")
        self.assertEqual(200, status)
        return body.decode("utf-8")

    # --- self-contained document --------------------------------------------

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

    # --- blank-page guard ---------------------------------------------------

    def test_served_page_is_not_blank(self) -> None:
        # Acceptance: the operator must never get a blank page. A served
        # document that is a complete HTML page with a visible heading and a
        # rendered body is the minimum smoke that the page actually painted.
        html = self._served_html()
        self.assertGreater(len(html), 500, "served page is suspiciously small")
        self.assertIn("<body>", html)
        self.assertIn("</html>", html)
        # A visible heading is the first thing painted before any fetch lands.
        self.assertRegex(html, r"<h2>\s*mozyo cockpit\s*</h2>")

    # --- core controls / state anchors --------------------------------------

    def test_core_controls_and_states_are_present(self) -> None:
        # Acceptance: Unit rows, freshness, and the unavailable state must be
        # representable in the served page. In the current (pre-grouped) served
        # UI these are: the unit table (Unit/Target row), the observation
        # freshness line, and the stale "runtime unavailable" banner, plus the
        # explicit Reload affordance and the recent-transitions list.
        html = self._served_html()
        for anchor in (
            'id="reload"',          # explicit Reload control
            'id="observation"',     # freshness line (observed_at / freshness)
            'id="stale"',           # tmux-runtime-unavailable banner
            'stale-banner',
            'id="units"',           # the unit rows table
            'id="transitions"',     # recent state transitions
        ):
            self.assertIn(anchor, html, anchor)
        # Every column the operator reads per unit is present as a header.
        for header in ("state", "agent", "session", "workspace",
                       "redmine", "actions"):
            self.assertIn(f"<th>{header}</th>", html, header)

    # --- mobile-ish fit -----------------------------------------------------

    def test_responsive_viewport_meta_present(self) -> None:
        # Without a responsive viewport meta a phone browser lays the page out
        # on an emulated ~980px desktop canvas and shrinks it, so controls and
        # rows render tiny / clipped. Pin device-width layout for mobile fit.
        html = self._served_html()
        self.assertRegex(
            html,
            r'<meta\s+name="viewport"\s+content="[^"]*width=device-width',
        )

    def test_overflow_containment_css_present(self) -> None:
        # Acceptance: detect text overflow / button-label overflow. These CSS
        # properties are the structural guards that long workspace / session /
        # path strings and the controls row stay inside the viewport instead of
        # forcing horizontal overflow or overlapping neighbours.
        html = self._served_html()
        style = html[html.index("<style>"):html.index("</style>")]
        # The wide unit table scrolls inside its own wrapper, never the body.
        self.assertIn("#units-wrap", html)
        self.assertRegex(style, r"#units-wrap\s*\{[^}]*overflow-x:\s*auto")
        # Long cell strings wrap instead of widening the table past the screen.
        self.assertRegex(style, r"\btd\b[^}]*overflow-wrap:\s*anywhere")
        # Button labels stay on one line (no mid-label wrap) but the controls
        # row itself wraps so the freshness line never overlaps the button.
        self.assertRegex(style, r"button\s*\{[^}]*white-space:\s*nowrap")
        self.assertRegex(style, r"#controls\s*\{[^}]*flex-wrap:\s*wrap")

    def test_every_runtime_state_class_is_styled(self) -> None:
        # A subtler "blank / invisible text" failure: the front end tags each
        # row's state / redmine / observation cell with a whitelisted class. If
        # any whitelisted state lacks a CSS rule it renders as unstyled
        # (often invisible / indistinguishable) text. Require a rule for each.
        html = self._served_html()
        style = html[html.index("<style>"):html.index("</style>")]
        classes: list[str] = []
        classes += _js_string_list(INDEX_HTML_TEMPLATE, "KNOWN_STATES")
        classes += [
            f"rm-{s}"
            for s in _js_string_list(INDEX_HTML_TEMPLATE, "KNOWN_RM_STATES")
        ]
        classes += [
            f"obs-{s}"
            for s in _js_string_list(
                INDEX_HTML_TEMPLATE, "KNOWN_DISPLAY_STATES"
            )
        ]
        for cls in classes:
            self.assertRegex(
                style,
                rf"\.{re.escape(cls)}\s*\{{",
                f"state class .{cls} has no CSS rule (would render unstyled)",
            )

    def test_no_external_assets(self) -> None:
        # Loopback / no-exfiltration posture, and a fit guard: an off-host
        # asset that fails to load can leave the page blank or unstyled. The
        # served document must reference nothing off 127.0.0.1.
        html = self._served_html()
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html.replace("http://127.0.0.1", ""))
        self.assertNotIn("@import", html)
        self.assertNotIn("<link", html)
        # No external script / image sources either.
        self.assertNotRegex(html, r'src\s*=\s*"https?://')

    # --- data contract that keeps the page from rendering blank --------------

    def test_units_payload_feeds_render_without_blanking(self) -> None:
        # The page paints rows from /api/units. Pin that a live snapshot returns
        # exactly the fields the render loop reads, so the table is not silently
        # blank because a field the front end expects went missing.
        panes = [pane("%1", "mozyo-demo", "claude")]
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            status, body = self._get("/api/units")
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertFalse(payload["stale"])
        self.assertIsInstance(payload["panes"], list)
        self.assertIn("observation", payload)
        self.assertIn("display_state", payload["observation"])
        row = payload["panes"][0]
        for field in ("pane_id", "agent_kind", "session", "activity"):
            self.assertIn(field, row, field)
        self.assertIn("state", row["activity"])
        # workspace may be None but the key the front end reads is present.
        self.assertIn("workspace", row)

    def test_unavailable_freshness_state_is_surfaced(self) -> None:
        # Acceptance: the freshness / unavailable state must be visible, not
        # hidden. When tmux is unreadable the cache snapshot is served stale —
        # the banner trigger (`stale: true`) fires and the observation envelope
        # derives a fail-closed display state (never healthy), so the operator
        # sees "outdated / unavailable" instead of a falsely-current view.
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
        self.assertIn(
            payload["observation"]["display_state"],
            ("reload_required", "unknown"),
        )


if __name__ == "__main__":
    unittest.main()
