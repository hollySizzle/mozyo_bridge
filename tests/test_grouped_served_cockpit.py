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

from mozyo_bridge.application.cockpit_ui import (
    grouped_units_payload,
    observed_units_from_inventory,
)
from mozyo_bridge.application.otel_receiver import build_server
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


if __name__ == "__main__":
    unittest.main()
