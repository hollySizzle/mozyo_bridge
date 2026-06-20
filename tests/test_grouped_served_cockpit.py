"""Served grouped cockpit HTML / live wiring tests (Redmine #12286).

Pins the wiring this US adds on top of the predecessors' pure object-to-object
slices (#12263 schema/resolver, #12264 read model, #12266 reload view, #12255
display view): the grouped read model is now built from (a) the repo-local
desired grouping config loaded from ``.mozyo-bridge/config.yaml`` and (b) the
live tmux inventory snapshot, and served as a display view from the cockpit
daemon's ``/api/grouped-units`` endpoint.

The boundary stays the one every predecessor enforced: the served view is a
display projection. Its rows carry identity + role presence only (never a pane /
target), its reload / freshness line derives from the same snapshot the rows do,
and an action re-resolves its candidate Unit live (``grouped-reveal`` /
``grouped-jump``). This file exercises the aggregation, the payload builder, and
the served endpoint (happy path + fail-closed on an invalid repo-local config).

It also carries the served browser-smoke / visual-fit tests (Redmine #12298):
``ServedCockpitSmokeTest`` asserts the served document is a non-blank page whose
core controls, freshness / unavailable state, and unit rows stay visible and
contained at desktop and mobile-ish viewport widths, with no off-host assets and
a stable ``/api/units`` data contract. Those assertions pin the structural
anchors and CSS fit properties the grouped rendering must keep; a real-browser
pass is recorded out of band in the issue journal.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_ui import (
    INDEX_HTML_TEMPLATE,
    CockpitActionError,
    candidate_unit_selector,
    grouped_units_payload,
    observed_units_from_inventory,
)
from mozyo_bridge.application.otel_receiver import build_server
from mozyo_bridge.domain.grouped_read_model import (
    UNIT_STATUS_CONTRADICTED,
    build_grouped_read_model,
)
from mozyo_bridge.domain.runtime_observation import (
    DISPLAY_STATE_HEALTHY,
    FRESHNESS_FRESH,
    READABILITY_READABLE,
    SOURCE_TMUX,
    STRENGTH_STRONG_RUNTIME_SIGNAL,
    RuntimeObservationSnapshot,
)
from mozyo_bridge.session_inventory import (
    InventoryRecord,
    InventorySnapshot,
    WorkspaceIdentity,
)

COCKPIT_UI = "mozyo_bridge.application.cockpit_ui"


def _fresh_observation() -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at="2026-06-20T12:00:00Z",
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_FRESH,
        readability=READABILITY_READABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason=None,
        contradiction=None,
        display_state=DISPLAY_STATE_HEALTHY,
    )


def _record(
    pane_id: str,
    role: str,
    workspace_id: str | None,
    *,
    project_name: str | None = None,
    session: str = "mozyo-demo",
    lane_id: str = "default",
) -> InventoryRecord:
    workspace = (
        WorkspaceIdentity(
            workspace_id=workspace_id,
            canonical_session=session,
            project_name=project_name,
            source="test",
        )
        if workspace_id is not None
        else None
    )
    return InventoryRecord(
        pane_id=pane_id,
        session=session,
        window_index="1",
        window_name=role,
        pane_index="0",
        pane_active=True,
        process=role,
        cwd="/tmp",
        repo_root="/tmp",
        agent_kind=role,
        lane_id=lane_id,
        workspace=workspace,
    )


def _snapshot(records, *, stale: bool = False) -> InventorySnapshot:
    return InventorySnapshot(
        records=tuple(records),
        collected_at=None,
        source="cache" if stale else "runtime",
        stale=stale,
        inventory_path=Path("/tmp/inv.sqlite"),
    )


class ObservedUnitsFromInventoryTest(unittest.TestCase):
    """Aggregating the pane-centric inventory into Unit-centric ObservedUnits."""

    def test_panes_aggregate_by_workspace_into_role_set(self) -> None:
        snapshot = _snapshot(
            [
                _record("%1", "codex", "ws-a", project_name="Alpha"),
                _record("%2", "claude", "ws-a", project_name="Alpha"),
                _record("%3", "claude", "ws-b", project_name="Beta"),
            ]
        )
        units = observed_units_from_inventory(
            snapshot, observation=_fresh_observation()
        )
        self.assertEqual([u.workspace_id for u in units], ["ws-a", "ws-b"])
        ws_a = units[0]
        self.assertEqual(set(ws_a.roles), {"codex", "claude"})
        self.assertEqual(ws_a.repo_label, "Alpha")
        self.assertEqual(ws_a.lane_id, "default")
        self.assertEqual(ws_a.host_id, "local")
        self.assertTrue(ws_a.active)

    def test_non_agent_and_workspaceless_panes_are_skipped(self) -> None:
        snapshot = _snapshot(
            [
                _record("%1", "unknown", "ws-a"),
                _record("%2", "claude", None),
                _record("%3", "codex", "ws-c", project_name="Gamma"),
            ]
        )
        units = observed_units_from_inventory(
            snapshot, observation=_fresh_observation()
        )
        self.assertEqual([u.workspace_id for u in units], ["ws-c"])

    def test_stale_snapshot_yields_inactive_units(self) -> None:
        # No live Target is asserted from a cache: a stale snapshot's Units read
        # inactive (the fail-safe posture) while still being shown.
        snapshot = _snapshot(
            [_record("%1", "claude", "ws-a", project_name="Alpha")], stale=True
        )
        units = observed_units_from_inventory(
            snapshot, observation=_fresh_observation()
        )
        self.assertEqual(len(units), 1)
        self.assertFalse(units[0].active)


class GroupedUnitsPayloadTest(unittest.TestCase):
    """The served payload builder: live inventory + repo-local grouping config."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".mozyo-bridge").mkdir(parents=True)
        (self.repo / ".git").mkdir()

    def _write_config(self, text: str) -> None:
        (self.repo / ".mozyo-bridge" / "config.yaml").write_text(
            text, encoding="utf-8"
        )

    def test_default_config_serves_default_grouped_view(self) -> None:
        # No repo-local config: behavior-preserving default placement, and the
        # observed Unit lands in a labeled default group.
        snapshot = _snapshot(
            [_record("%1", "claude", "ws-a", project_name="Alpha")]
        )
        with patch(f"{COCKPIT_UI}.take_inventory", lambda **_: snapshot):
            payload = grouped_units_payload(repo_root=self.repo)
        self.assertEqual(
            payload["project_group_presentation"], "same_cockpit_column"
        )
        labels = [g["header_label"] for g in payload["groups"]]
        self.assertIn("Alpha", labels)

    def test_config_groups_and_placement_drive_the_served_view(self) -> None:
        self._write_config(
            "presentation:\n"
            "  project_group_presentation: project_group_tmux_window\n"
            "  project_groups:\n"
            "    - group_id: 'project:alpha'\n"
            "      label: 'Alpha Group'\n"
            "  grouping:\n"
            "    membership_rules:\n"
            "      - when:\n"
            "          repo_label: 'Alpha'\n"
            "        group_id: 'project:alpha'\n"
        )
        snapshot = _snapshot(
            [_record("%1", "claude", "ws-a", project_name="Alpha")]
        )
        with patch(f"{COCKPIT_UI}.take_inventory", lambda **_: snapshot):
            payload = grouped_units_payload(repo_root=self.repo)
        self.assertEqual(
            payload["project_group_presentation"], "project_group_tmux_window"
        )
        declared = [
            g for g in payload["groups"] if g["group_id"] == "project:alpha"
        ]
        self.assertEqual(len(declared), 1)
        self.assertEqual(declared[0]["header_label"], "Alpha Group")
        # The Unit was placed into the declared group by the membership rule, and
        # its row carries identity for action wiring (never a pane / target).
        unit = declared[0]["units"][0]
        self.assertEqual(unit["workspace_id"], "ws-a")
        self.assertNotIn("pane", unit)
        self.assertNotIn("target", unit)

    def test_invalid_placement_config_raises(self) -> None:
        from mozyo_bridge.domain.repo_local_config import RepoLocalConfigError

        self._write_config(
            "presentation:\n  project_group_presentation: iterm_tab\n"
        )
        snapshot = _snapshot([])
        with patch(f"{COCKPIT_UI}.take_inventory", lambda **_: snapshot):
            with self.assertRaises(RepoLocalConfigError):
                grouped_units_payload(repo_root=self.repo)


class SameWorkspaceMultiLaneTest(unittest.TestCase):
    """Fail-closed fallback when the lane discriminator is unreadable (#12286
    review j#61995, preserved by #12293): Codex/Claude pairs sharing one
    workspace_id AND one lane (no readable @mozyo_lane_id) must not collapse into a
    single healthy actionable Unit — they degrade to a visible contradicted row."""

    def _multi_lane_snapshot(self) -> InventorySnapshot:
        # Two worktrees of one repo whose panes carry NO @mozyo_lane_id option, so
        # they all project to the same (workspace_id, default lane): each role then
        # has two live panes under one projected Unit and cannot be faithfully
        # split. (The distinct-lane-id case that DOES split is in
        # LaneIdentitySplitTest below.)
        return _snapshot(
            [
                _record("%0", "codex", "ws-shared", project_name="Shared",
                        session="main-lane"),
                _record("%1", "claude", "ws-shared", project_name="Shared",
                        session="main-lane"),
                _record("%52", "codex", "ws-shared", project_name="Shared",
                        session="sublane"),
                _record("%53", "claude", "ws-shared", project_name="Shared",
                        session="sublane"),
            ]
        )

    def test_aggregation_degrades_to_contradicted_not_collapsed_healthy(self) -> None:
        units = observed_units_from_inventory(
            self._multi_lane_snapshot(), observation=_fresh_observation()
        )
        # Still one projected Unit (no faithful lane discriminator to split on)…
        self.assertEqual(len(units), 1)
        # …but it is NOT a healthy actionable Unit: its observation carries a
        # visible contradiction so it reads needs_reload / unactionable.
        self.assertIsNotNone(units[0].observation.contradiction)

    def test_read_model_marks_unit_contradicted_and_unactionable(self) -> None:
        units = observed_units_from_inventory(
            self._multi_lane_snapshot(), observation=_fresh_observation()
        )
        model = build_grouped_read_model(
            None, units, observation=_fresh_observation()
        )
        row = model.all_units()[0]
        self.assertEqual(row.status, UNIT_STATUS_CONTRADICTED)
        self.assertTrue(row.needs_reload)
        # The candidate selector fails closed on the degraded row: the served UI
        # cannot seed a grouped action from it.
        with self.assertRaises(CockpitActionError):
            candidate_unit_selector(row)

    def test_single_pair_stays_healthy_and_actionable(self) -> None:
        # Control: one pane per role under one workspace is still a healthy Unit.
        units = observed_units_from_inventory(
            _snapshot(
                [
                    _record("%0", "codex", "ws-solo", project_name="Solo"),
                    _record("%1", "claude", "ws-solo", project_name="Solo"),
                ]
            ),
            observation=_fresh_observation(),
        )
        self.assertEqual(len(units), 1)
        self.assertIsNone(units[0].observation.contradiction)
        model = build_grouped_read_model(
            None, units, observation=_fresh_observation()
        )
        row = model.all_units()[0]
        self.assertFalse(row.needs_reload)
        # A healthy row yields an identity selector (actionable).
        self.assertEqual(
            candidate_unit_selector(row)["workspace_id"], "ws-solo"
        )

    def test_served_payload_row_is_unactionable(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        (repo / ".mozyo-bridge").mkdir(parents=True)
        (repo / ".git").mkdir()
        with patch(
            f"{COCKPIT_UI}.take_inventory",
            lambda **_: self._multi_lane_snapshot(),
        ):
            payload = grouped_units_payload(repo_root=repo)
        rows = [u for g in payload["groups"] for u in g["units"]]
        shared = [r for r in rows if r["workspace_id"] == "ws-shared"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["status"], "contradicted")
        self.assertTrue(shared[0]["reload_required"])


class LaneIdentitySplitTest(unittest.TestCase):
    """Redmine #12293: same-workspace panes carrying distinct @mozyo_lane_id values
    split into distinct, faithful ``Unit = workspace + lane + role set`` rows — each
    a healthy actionable Unit, never collapsed and never degraded to contradicted."""

    def _distinct_lane_snapshot(self) -> InventorySnapshot:
        # One repo (ws-shared) running two lanes/worktrees, each pane tagged with
        # its checkout-local @mozyo_lane_id, one Codex+Claude pair per lane.
        return _snapshot(
            [
                _record("%0", "codex", "ws-shared", project_name="Shared",
                        session="main", lane_id="lane-main"),
                _record("%1", "claude", "ws-shared", project_name="Shared",
                        session="main", lane_id="lane-main"),
                _record("%52", "codex", "ws-shared", project_name="Shared",
                        session="sub", lane_id="issue_12293"),
                _record("%53", "claude", "ws-shared", project_name="Shared",
                        session="sub", lane_id="issue_12293"),
            ]
        )

    def test_distinct_lanes_split_into_faithful_units(self) -> None:
        units = observed_units_from_inventory(
            self._distinct_lane_snapshot(), observation=_fresh_observation()
        )
        # Two distinct Units, one per lane, each carrying the full role set.
        self.assertEqual(
            sorted(u.lane_id for u in units), ["issue_12293", "lane-main"]
        )
        for unit in units:
            self.assertEqual(unit.workspace_id, "ws-shared")
            self.assertEqual(set(unit.roles), {"codex", "claude"})
            # Faithful split → no contradiction, stays actionable.
            self.assertIsNone(unit.observation.contradiction)

    def test_split_units_are_healthy_and_actionable(self) -> None:
        units = observed_units_from_inventory(
            self._distinct_lane_snapshot(), observation=_fresh_observation()
        )
        model = build_grouped_read_model(
            None, units, observation=_fresh_observation()
        )
        rows = model.all_units()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertFalse(row.needs_reload)
            # Each row yields a candidate selector carrying its own lane.
            selector = candidate_unit_selector(row)
            self.assertEqual(selector["workspace_id"], "ws-shared")
            self.assertIn(selector["lane_id"], ("lane-main", "issue_12293"))

    def test_served_payload_shows_two_actionable_lane_rows(self) -> None:
        from datetime import datetime, timezone

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        (repo / ".mozyo-bridge").mkdir(parents=True)
        (repo / ".git").mkdir()
        # The served path derives freshness from the snapshot's collected_at, so
        # give the runtime snapshot a fresh collection time relative to ``now`` to
        # exercise the healthy (observed) path end to end.
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        snapshot = InventorySnapshot(
            records=self._distinct_lane_snapshot().records,
            collected_at="2026-06-20T12:00:00+00:00",
            source="runtime",
            stale=False,
            inventory_path=Path("/tmp/inv.sqlite"),
        )
        with patch(
            f"{COCKPIT_UI}.take_inventory",
            lambda **_: snapshot,
        ):
            payload = grouped_units_payload(repo_root=repo, now=now)
        rows = [u for g in payload["groups"] for u in g["units"]]
        shared = [r for r in rows if r["workspace_id"] == "ws-shared"]
        self.assertEqual(
            sorted(r["lane_id"] for r in shared), ["issue_12293", "lane-main"]
        )
        for row in shared:
            self.assertEqual(row["status"], "observed")
            self.assertFalse(row["reload_required"])
            # The lane is surfaced as the row's distinguishing lane label.
            self.assertEqual(row["lane_label"], row["lane_id"])

    def test_mixed_readable_and_unreadable_lanes(self) -> None:
        # One faithful lane (distinct id) plus two panes that share the default
        # lane: the faithful lane stays a healthy Unit while the unreadable-lane
        # collision degrades to contradicted — the fallback is per-lane, not all
        # or nothing.
        snapshot = _snapshot(
            [
                _record("%0", "codex", "ws-shared", project_name="Shared",
                        lane_id="issue_12293"),
                _record("%10", "codex", "ws-shared", project_name="Shared"),
                _record("%11", "codex", "ws-shared", project_name="Shared"),
            ]
        )
        units = observed_units_from_inventory(
            snapshot, observation=_fresh_observation()
        )
        by_lane = {u.lane_id: u for u in units}
        self.assertIsNone(by_lane["issue_12293"].observation.contradiction)
        self.assertIsNotNone(by_lane["default"].observation.contradiction)


class GroupedUnitsHttpTest(unittest.TestCase):
    """The served ``/api/grouped-units`` endpoint."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".mozyo-bridge").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        env_patch = patch.dict(
            "os.environ",
            {"MOZYO_BRIDGE_HOME": str(self.home), "MOZYO_REPO": str(self.repo)},
            clear=False,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.server = build_server(host="127.0.0.1", port=0, home=self.home)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _write_config(self, text: str) -> None:
        (self.repo / ".mozyo-bridge" / "config.yaml").write_text(
            text, encoding="utf-8"
        )

    def _get(self, path: str):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_endpoint_serves_grouped_display_payload(self) -> None:
        snapshot = _snapshot(
            [_record("%1", "claude", "ws-a", project_name="Alpha")]
        )
        with patch(f"{COCKPIT_UI}.take_inventory", lambda **_: snapshot):
            status, payload = self._get("/api/grouped-units")
        self.assertEqual(200, status)
        self.assertIn("groups", payload)
        self.assertIn("project_group_presentation", payload)
        self.assertIn("reload", payload)

    def test_endpoint_fails_closed_on_invalid_config(self) -> None:
        self._write_config(
            "presentation:\n  project_group_presentation: iterm_tab\n"
        )
        with patch(f"{COCKPIT_UI}.take_inventory", lambda **_: _snapshot([])):
            status, payload = self._get("/api/grouped-units")
        self.assertEqual(400, status)
        self.assertIn("invalid", payload["error"])


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
