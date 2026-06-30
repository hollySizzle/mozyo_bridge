"""Served grouped cockpit HTTP-endpoint tests (Redmine #12286).

Focused on the daemon-served ``/api/grouped-units`` endpoint wiring in
``otel_receiver``: the happy path (a grouped display payload built from the live
inventory + repo-local grouping config) and the fail-closed path (an invalid
repo-local config returns 400 rather than a silent default). After the #12323
cockpit split the pure payload builder + aggregation contract is tested in
``test_cockpit_payload`` and the served HTML / browser smoke in
``test_cockpit_page``; this file pins only how the receiver serves the grouped
payload over HTTP.

The boundary the endpoint preserves: the served view is a display projection. Its
rows carry identity + role presence only (never a pane / target), and an action
re-resolves its candidate Unit live (``grouped-reveal`` / ``grouped-jump``).
Everything runs on an ephemeral port with a patched inventory — no real tmux.
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

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.otel_receiver import build_server
from mozyo_bridge.session_inventory import (
    InventoryRecord,
    InventorySnapshot,
    WorkspaceIdentity,
)

# The served endpoint builds its payload via cockpit_payload.grouped_units_payload,
# which imports take_inventory into the cockpit_payload namespace — patch it there.
COCKPIT_PAYLOAD = "mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application.cockpit_payload"


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
        with patch(f"{COCKPIT_PAYLOAD}.take_inventory", lambda **_: snapshot):
            status, payload = self._get("/api/grouped-units")
        self.assertEqual(200, status)
        self.assertIn("groups", payload)
        self.assertIn("project_group_presentation", payload)
        self.assertIn("reload", payload)

    def test_endpoint_fails_closed_on_invalid_config(self) -> None:
        self._write_config(
            "presentation:\n  project_group_presentation: iterm_tab\n"
        )
        with patch(f"{COCKPIT_PAYLOAD}.take_inventory", lambda **_: _snapshot([])):
            status, payload = self._get("/api/grouped-units")
        self.assertEqual(400, status)
        self.assertIn("invalid", payload["error"])


if __name__ == "__main__":
    unittest.main()
