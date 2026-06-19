"""Grouped cockpit Unit action preflight tests (Redmine #12265).

Pins the boundary the US fixes: a grouped cockpit UI action goes through the
mozyo command boundary and an *action-time live preflight*, and the #12264
grouped read model is a display / candidate input only — never a routing
authority. Covers, at the ``cockpit_ui`` command surface and over the
``otel_receiver`` POST path:

- a candidate Unit identity (workspace_id / role) resolves to the live pane via
  a fresh inventory and routes through the existing pane-centric action;
- a stale / missing / ambiguous / non-default-lane / non-local-host candidate
  fails closed (the projection only *names* a candidate, never authorizes);
- the displayed snapshot's ``active`` flag / group geometry cannot bypass live
  resolution (a row that claims ``active`` but has no live pane still fails); and
- ``candidate_unit_selector`` yields identity only from a fresh row and refuses
  a degraded (``needs_reload``) row outright.

Everything runs on temp homes / an ephemeral port with a patched inventory — no
real tmux mutations.
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
    CockpitActionError,
    candidate_unit_selector,
    grouped_jump,
    grouped_reveal,
)
from mozyo_bridge.application.otel_receiver import build_server
from mozyo_bridge.domain.grouped_read_model import (
    UNIT_STATUS_CONTRADICTED,
    UNIT_STATUS_OBSERVED,
    UNIT_STATUS_STALE,
    UnitView,
)
from mozyo_bridge.domain.presentation_grouping import (
    STATUS_DESIRED_UNIT_MISSING,
    STATUS_IDENTITY_CONFLICT,
)
from mozyo_bridge.session_inventory import (
    InventoryRecord,
    InventorySnapshot,
    WorkspaceIdentity,
)

COCKPIT_UI = "mozyo_bridge.application.cockpit_ui"


def _record(
    pane_id: str,
    role: str,
    workspace_id: str | None,
    *,
    repo_root: str | None = None,
    session: str = "mozyo-demo",
    window_index: str = "1",
) -> InventoryRecord:
    workspace = (
        WorkspaceIdentity(
            workspace_id=workspace_id,
            canonical_session=session,
            project_name=None,
            source="test",
        )
        if workspace_id is not None
        else None
    )
    return InventoryRecord(
        pane_id=pane_id,
        session=session,
        window_index=window_index,
        window_name=role,
        pane_index="0",
        pane_active=True,
        process=role,
        cwd=repo_root or "/tmp",
        repo_root=repo_root,
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


def _patch_inventory(snapshot: InventorySnapshot):
    return patch(f"{COCKPIT_UI}.take_inventory", lambda **_: snapshot)


class GroupedActionResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def test_candidate_identity_resolves_to_live_pane_and_reveals(self) -> None:
        snapshot = _snapshot(
            [_record("%7", "claude", "ws-a", repo_root=str(self.repo))]
        )
        calls: list[list[str]] = []

        def fake_run(argv, capture_output, text, check):
            calls.append(argv)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with _patch_inventory(snapshot), patch(
            f"{COCKPIT_UI}.subprocess.run", side_effect=fake_run
        ), patch(f"{COCKPIT_UI}.sys.platform", "darwin"):
            result = grouped_reveal(
                workspace_id="ws-a", role="claude", home=self.home
            )
        # The side effect ran on the *live* pane's repo root, resolved from the
        # inventory — not from any read-model row.
        self.assertEqual([["open", str(self.repo)]], calls)
        self.assertEqual("reveal", result["action"])
        self.assertEqual("%7", result["pane_id"])
        self.assertEqual("ws-a", result["workspace_id"])
        self.assertEqual("claude", result["role"])

    def test_candidate_identity_resolves_to_live_pane_and_jumps(self) -> None:
        snapshot = _snapshot(
            [_record("%7", "codex", "ws-a", repo_root=str(self.repo))]
        )

        def fake_run_tmux(*args, check: bool = True):
            if args[0] == "list-clients":
                return type(
                    "R",
                    (),
                    {"returncode": 0, "stdout": "100\t0\t/dev/ttys1\n", "stderr": ""},
                )()
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with _patch_inventory(snapshot), patch(
            "mozyo_bridge.infrastructure.tmux_client.run_tmux",
            side_effect=fake_run_tmux,
        ):
            result = grouped_jump(
                workspace_id="ws-a", role="codex", home=self.home
            )
        self.assertEqual("jump", result["action"])
        self.assertEqual("%7", result["pane_id"])
        self.assertEqual("mozyo-demo:1", result["target"])

    def test_stale_snapshot_fails_closed(self) -> None:
        snapshot = _snapshot(
            [_record("%7", "claude", "ws-a", repo_root=str(self.repo))],
            stale=True,
        )
        with _patch_inventory(snapshot):
            with self.assertRaises(CockpitActionError) as ctx:
                grouped_reveal(workspace_id="ws-a", role="claude", home=self.home)
        self.assertIn("stale", str(ctx.exception))

    def test_missing_live_pane_fails_closed(self) -> None:
        # Inventory has a different workspace / role; the candidate has no live
        # pane, so the action fails closed even though a UI row might mark the
        # unit "active" (the snapshot flag does not authorize).
        snapshot = _snapshot(
            [_record("%7", "codex", "ws-b", repo_root=str(self.repo))]
        )
        with _patch_inventory(snapshot):
            with self.assertRaises(CockpitActionError) as ctx:
                grouped_reveal(workspace_id="ws-a", role="claude", home=self.home)
        self.assertIn("no live claude pane", str(ctx.exception))

    def test_ambiguous_live_panes_fail_closed(self) -> None:
        snapshot = _snapshot(
            [
                _record("%7", "claude", "ws-a", repo_root=str(self.repo)),
                _record("%9", "claude", "ws-a", repo_root=str(self.repo)),
            ]
        )
        with _patch_inventory(snapshot):
            with self.assertRaises(CockpitActionError) as ctx:
                grouped_reveal(workspace_id="ws-a", role="claude", home=self.home)
        msg = str(ctx.exception)
        self.assertIn("ambiguous", msg)
        self.assertIn("%7", msg)
        self.assertIn("%9", msg)

    def test_non_default_lane_fails_closed_without_reading_inventory(self) -> None:
        def boom(**_):
            raise AssertionError("inventory must not be read for a bad candidate")

        with patch(f"{COCKPIT_UI}.take_inventory", boom):
            with self.assertRaises(CockpitActionError) as ctx:
                grouped_reveal(
                    workspace_id="ws-a",
                    role="claude",
                    lane_id="issue_123",
                    home=self.home,
                )
        self.assertIn("non-default lane", str(ctx.exception))

    def test_non_local_host_fails_closed(self) -> None:
        def boom(**_):
            raise AssertionError("inventory must not be read for a bad candidate")

        with patch(f"{COCKPIT_UI}.take_inventory", boom):
            with self.assertRaises(CockpitActionError) as ctx:
                grouped_reveal(
                    workspace_id="ws-a",
                    role="claude",
                    host_id="remote",
                    home=self.home,
                )
        self.assertIn("non-local host", str(ctx.exception))

    def test_missing_role_fails_closed(self) -> None:
        with patch(f"{COCKPIT_UI}.take_inventory") as inv:
            with self.assertRaises(CockpitActionError) as ctx:
                grouped_reveal(workspace_id="ws-a", role=None, home=self.home)
        inv.assert_not_called()
        self.assertIn("agent role", str(ctx.exception))

    def test_missing_workspace_id_fails_closed(self) -> None:
        with self.assertRaises(CockpitActionError) as ctx:
            grouped_reveal(workspace_id="", role="claude", home=self.home)
        self.assertIn("workspace_id", str(ctx.exception))


class CandidateSelectorTest(unittest.TestCase):
    def _row(self, status: str) -> UnitView:
        return UnitView(
            unit_id="unit:local:ws-a:default",
            workspace_id="ws-a",
            lane_id="default",
            host_id="local",
            label="Project A",
            group_id="project:a",
            status=status,
            position=5,
            active=True,
        )

    def test_observed_row_yields_identity_only(self) -> None:
        selector = candidate_unit_selector(self._row(UNIT_STATUS_OBSERVED))
        # Identity only — no group_id / active / position / status leaks into the
        # selector that seeds the action.
        self.assertEqual(
            {"workspace_id": "ws-a", "lane_id": "default", "host_id": "local"},
            selector,
        )

    def test_degraded_rows_fail_closed(self) -> None:
        for status in (
            UNIT_STATUS_STALE,
            UNIT_STATUS_CONTRADICTED,
            STATUS_IDENTITY_CONFLICT,
            STATUS_DESIRED_UNIT_MISSING,
        ):
            with self.subTest(status=status):
                with self.assertRaises(CockpitActionError) as ctx:
                    candidate_unit_selector(self._row(status))
                self.assertIn("not current", str(ctx.exception))


class GroupingGeometryCannotBypassTest(unittest.TestCase):
    """A read-model row's display facts never route a side effect."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def _observed_row(self, *, active: bool) -> UnitView:
        return UnitView(
            unit_id="unit:local:ws-a:default",
            workspace_id="ws-a",
            lane_id="default",
            host_id="local",
            label="Project A",
            group_id="project:a",
            status=UNIT_STATUS_OBSERVED,
            position=0,
            active=active,
        )

    def test_active_row_with_no_live_pane_still_fails_closed(self) -> None:
        # The row claims active=True and sits in a config-declared group, but the
        # live inventory has no such pane: routing authority is the live
        # inventory, not the read model, so the action fails closed.
        selector = candidate_unit_selector(self._observed_row(active=True))
        with _patch_inventory(_snapshot([])):
            with self.assertRaises(CockpitActionError) as ctx:
                grouped_reveal(role="claude", home=self.home, **selector)
        self.assertIn("no live claude pane", str(ctx.exception))

    def test_action_targets_live_pane_not_row_geometry(self) -> None:
        # The selector carries identity; the pane that gets acted on is resolved
        # purely from the inventory (%42), proving group/position/active geometry
        # is not the route.
        selector = candidate_unit_selector(self._observed_row(active=True))
        snapshot = _snapshot(
            [_record("%42", "claude", "ws-a", repo_root=str(self.repo))]
        )
        calls: list[list[str]] = []

        def fake_run(argv, capture_output, text, check):
            calls.append(argv)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with _patch_inventory(snapshot), patch(
            f"{COCKPIT_UI}.subprocess.run", side_effect=fake_run
        ), patch(f"{COCKPIT_UI}.sys.platform", "darwin"):
            result = grouped_reveal(role="claude", home=self.home, **selector)
        self.assertEqual("%42", result["pane_id"])
        self.assertEqual([["open", str(self.repo)]], calls)


class GroupedActionHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".git").mkdir(parents=True)
        env_patch = patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.server = build_server(host="127.0.0.1", port=0, home=self.home)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _post(self, path: str, payload: dict, *, with_token: bool = True):
        headers = {"Content-Type": "application/json"}
        if with_token:
            headers["X-Mozyo-Cockpit-Token"] = self.server.cockpit_token
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

    def test_grouped_action_without_token_is_403(self) -> None:
        status, payload = self._post(
            "/api/actions/grouped-reveal",
            {"workspace_id": "ws-a", "role": "claude"},
            with_token=False,
        )
        self.assertEqual(403, status)

    def test_grouped_action_missing_unit_is_409(self) -> None:
        with _patch_inventory(_snapshot([])):
            status, payload = self._post(
                "/api/actions/grouped-reveal",
                {"workspace_id": "ws-a", "role": "claude"},
            )
        self.assertEqual(409, status)
        self.assertIn("no live claude pane", payload["error"])

    def test_grouped_action_stale_is_409(self) -> None:
        snapshot = _snapshot(
            [_record("%7", "claude", "ws-a", repo_root=str(self.repo))],
            stale=True,
        )
        with _patch_inventory(snapshot):
            status, payload = self._post(
                "/api/actions/grouped-reveal",
                {"workspace_id": "ws-a", "role": "claude"},
            )
        self.assertEqual(409, status)
        self.assertIn("stale", payload["error"])

    def test_grouped_reveal_endpoint_happy_path(self) -> None:
        snapshot = _snapshot(
            [_record("%7", "claude", "ws-a", repo_root=str(self.repo))]
        )

        def fake_run(argv, capture_output, text, check):
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with _patch_inventory(snapshot), patch(
            f"{COCKPIT_UI}.subprocess.run", side_effect=fake_run
        ), patch(f"{COCKPIT_UI}.sys.platform", "darwin"):
            status, payload = self._post(
                "/api/actions/grouped-reveal",
                {"workspace_id": "ws-a", "role": "claude"},
            )
        self.assertEqual(200, status)
        self.assertEqual("reveal", payload["action"])
        self.assertEqual("%7", payload["pane_id"])


if __name__ == "__main__":
    unittest.main()
