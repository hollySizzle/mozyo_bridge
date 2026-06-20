"""Served cockpit browser-smoke / visual-fit tests (Redmine #12298).

US #12298 stabilizes the *served* grouped cockpit UI so an operator who opens
it in a real browser — at desktop **and** mobile-ish viewport widths — gets a
non-blank page whose core controls, freshness / unavailable state, and unit
rows stay visible without overlapping or overflowing off-screen.

Scope boundary (issue #12298 journal #62066): this module builds the browser /
served smoke *scaffolding* and visual-fit assertions from the **current** served
cockpit UI (the flat unit table plus the Reload / observation-freshness controls
and the stale "runtime unavailable" banner). The grouped Project Group -> Unit
*HTML rendering* itself is the sibling lane's (#12296/#12297) work; these
assertions pin the structural anchors and CSS fit properties that grouped
rendering must keep, so they extend rather than churn behavior. No marketing
chrome and no private operator layout policy are introduced or assumed.

What "visual fit" means without a headless browser in the test deps: Playwright
is not a project dependency and a CI runner has no browser, so the deterministic
tests here assert the *fit-load-bearing* contract of the served document — the
responsive viewport meta, overflow-containment CSS, styled-for-every-state
classes, no off-host assets, and the /api/units data contract that keeps the
page from rendering blank. A real-browser pass is recorded out of band; the
runbook is::

    # 1. launch the loopback daemon (serves the cockpit on 127.0.0.1):
    mozyo-bridge serve --host 127.0.0.1 --port 8765   # or build_server(...)
    # 2. open http://127.0.0.1:8765/ in a browser.
    # 3. at a desktop width (~1280x800) and a mobile-ish width (~375x667),
    #    confirm: the page is not blank; the Reload button, observation
    #    freshness line, and (when tmux is down) the stale banner are visible;
    #    no element overflows the viewport horizontally
    #    (document.documentElement.scrollWidth <= window.innerWidth); and unit
    #    rows / labels do not overlap. Record the result in the issue journal.

Everything runs on an ephemeral port with a temp home — no real tmux mutation,
no network, no credentials.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_ui import INDEX_HTML_TEMPLATE
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
