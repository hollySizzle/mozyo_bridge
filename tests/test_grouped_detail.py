"""Grouped cockpit Unit detail / command preview tests (Redmine #12296).

Pins the boundary the US fixes: a selected grouped Unit gets a one-screen detail
listing its safe actions as a *command preview*, where availability is derived
fail-closed and a previewed action still routes through the action-time live
preflight before any side effect. Covers three surfaces:

- the pure detail projection (``build_grouped_unit_detail``): an observed / active
  row lists available commands per actionable role pane; a degraded
  (``needs_reload``) / non-local-host / non-default-lane / no-live-target row lists
  the actions as *unavailable* with a visible reason; the payload is public-safe
  (no pane id / path / credential / prompt);
- the non-mutating live preview (``grouped_action_preview``): runs the same live
  preflight as the real actions but performs no side effect, reporting
  stale / ambiguous / missing / remote / non-default-lane candidates as
  ``available: False`` with the preflight reason; and
- the served ``/api/actions/grouped-preview`` endpoint: token-gated, always 200,
  never mutates.

Everything runs on temp homes / an ephemeral port with a patched inventory — no
real tmux mutations.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_ui import (
    CockpitActionError,  # noqa: F401  (kept for parity / explicit contract)
    grouped_action_preview,
)
from mozyo_bridge.application.grouped_detail import (
    GROUPED_DETAIL_DIAGNOSTIC_ONLY_NOTE,
    build_grouped_unit_detail,
)
from mozyo_bridge.application.otel_receiver import build_server
from mozyo_bridge.domain.grouped_read_model import (
    UNIT_STATUS_CONTRADICTED,
    UNIT_STATUS_OBSERVED,
    UNIT_STATUS_PARTIAL,
    UNIT_STATUS_STALE,
    UNIT_STATUS_UNKNOWN,
    UNIT_STATUS_UNREADABLE,
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

# Public-safe keys the detail / command payloads are allowed to carry. The detail
# is a display projection: it must never surface a pane id, repo path, credential,
# or prompt body (public-private-boundary.md "Public Record Constraints").
_DETAIL_KEYS = {
    "unit_id",
    "workspace_id",
    "lane_id",
    "host_id",
    "label",
    "status",
    "freshness",
    "observed_at",
    "stale_reason",
    "contradiction",
    "active",
    "roles",
    "actions_available",
    "unavailable_reason",
    "commands",
    "boundary_note",
}
_COMMAND_KEYS = {
    "kind",
    "role",
    "endpoint",
    "summary",
    "available",
    "live_preflight_required",
    "unavailable_reason",
    "selector",
}
_SELECTOR_KEYS = {"workspace_id", "lane_id", "host_id", "role"}
# Substrings that would betray a leak of a private path / pane / secret / prompt.
_FORBIDDEN_SUBSTRINGS = (
    "pane_id",
    "repo_root",
    "cwd",
    "credential",
    "secret",
    "token",
    "prompt",
    "/Users/",
    ".sqlite",
)


def _row(
    status: str,
    *,
    workspace_id: str = "ws-a",
    lane_id: str = "default",
    host_id: str = "local",
    active: bool = True,
    roles: "tuple[str, ...]" = ("codex", "claude"),
    label: str | None = "Project A",
) -> UnitView:
    return UnitView(
        unit_id=f"unit:{host_id}:{workspace_id}:{lane_id}",
        workspace_id=workspace_id,
        lane_id=lane_id,
        host_id=host_id,
        label=label,
        group_id="project:a",
        status=status,
        position=0,
        active=active,
        roles=roles,
    )


class BuildDetailTest(unittest.TestCase):
    def test_observed_active_row_lists_available_commands_per_role(self) -> None:
        detail = build_grouped_unit_detail(_row(UNIT_STATUS_OBSERVED))
        self.assertTrue(detail.actions_available)
        self.assertIsNone(detail.unavailable_reason)
        # Roles canonically ordered: codex before claude.
        self.assertEqual(("codex", "claude"), detail.roles)
        # One command per actionable role x action kind (2 x 2 = 4), all available.
        self.assertEqual(4, len(detail.commands))
        self.assertTrue(all(c.available for c in detail.commands))
        self.assertTrue(all(c.live_preflight_required for c in detail.commands))
        kinds = {(c.role, c.kind) for c in detail.commands}
        self.assertEqual(
            {
                ("codex", "reveal"),
                ("codex", "jump"),
                ("claude", "reveal"),
                ("claude", "jump"),
            },
            kinds,
        )
        # Each available command carries the public-safe identity selector the
        # grouped action endpoint accepts — and only that.
        for command in detail.commands:
            self.assertEqual(
                {
                    "workspace_id": "ws-a",
                    "lane_id": "default",
                    "host_id": "local",
                    "role": command.role,
                },
                command.selector,
            )

    def test_single_role_row_lists_only_that_role(self) -> None:
        detail = build_grouped_unit_detail(
            _row(UNIT_STATUS_OBSERVED, roles=("claude",))
        )
        self.assertEqual(("claude",), detail.roles)
        self.assertEqual(
            {("claude", "reveal"), ("claude", "jump")},
            {(c.role, c.kind) for c in detail.commands},
        )

    def test_non_agent_role_is_not_actionable(self) -> None:
        # An observed role outside the agent set (codex/claude) is not a cockpit
        # action target; with no agent role the unit reads as no-live-target.
        detail = build_grouped_unit_detail(
            _row(UNIT_STATUS_OBSERVED, roles=("watcher",))
        )
        self.assertEqual((), detail.roles)
        self.assertFalse(detail.actions_available)
        self.assertIn("no observed live target", detail.unavailable_reason)

    def test_degraded_rows_are_unavailable(self) -> None:
        for status in (
            UNIT_STATUS_STALE,
            UNIT_STATUS_CONTRADICTED,
            UNIT_STATUS_PARTIAL,
            UNIT_STATUS_UNREADABLE,
            UNIT_STATUS_UNKNOWN,
            STATUS_IDENTITY_CONFLICT,
            STATUS_DESIRED_UNIT_MISSING,
        ):
            with self.subTest(status=status):
                detail = build_grouped_unit_detail(_row(status))
                self.assertFalse(detail.actions_available)
                self.assertIn("not current", detail.unavailable_reason)
                # The actions are still listed (never silently dropped), each
                # unavailable with the visible reason and no seedable selector.
                self.assertEqual(
                    {"reveal", "jump"}, {c.kind for c in detail.commands}
                )
                for command in detail.commands:
                    self.assertFalse(command.available)
                    self.assertIsNone(command.role)
                    self.assertIsNone(command.selector)
                    self.assertIn("not current", command.unavailable_reason)
                    self.assertTrue(command.live_preflight_required)

    def test_non_local_host_is_unavailable(self) -> None:
        detail = build_grouped_unit_detail(
            _row(UNIT_STATUS_OBSERVED, host_id="remote")
        )
        self.assertFalse(detail.actions_available)
        self.assertIn("non-local host", detail.unavailable_reason)

    def test_non_default_lane_is_unavailable(self) -> None:
        detail = build_grouped_unit_detail(
            _row(UNIT_STATUS_OBSERVED, lane_id="issue_123")
        )
        self.assertFalse(detail.actions_available)
        self.assertIn("non-default lane", detail.unavailable_reason)

    def test_observed_but_inactive_is_unavailable(self) -> None:
        # Fresh / readable row, but no live Target observed (active False): there
        # is nothing to act on, so the actions fail closed.
        detail = build_grouped_unit_detail(
            _row(UNIT_STATUS_OBSERVED, active=False)
        )
        self.assertFalse(detail.actions_available)
        self.assertIn("no observed live target", detail.unavailable_reason)

    def test_observed_active_but_no_roles_is_unavailable(self) -> None:
        detail = build_grouped_unit_detail(
            _row(UNIT_STATUS_OBSERVED, roles=())
        )
        self.assertFalse(detail.actions_available)
        self.assertIn("no observed live target", detail.unavailable_reason)

    def test_payload_is_public_safe(self) -> None:
        for status in (UNIT_STATUS_OBSERVED, UNIT_STATUS_STALE):
            with self.subTest(status=status):
                detail = build_grouped_unit_detail(_row(status))
                payload = detail.as_payload()
                # JSON-serializable and shaped to the public-safe key sets only.
                serialized = json.dumps(payload)
                self.assertEqual(_DETAIL_KEYS, set(payload))
                self.assertEqual(
                    GROUPED_DETAIL_DIAGNOSTIC_ONLY_NOTE, payload["boundary_note"]
                )
                for command in payload["commands"]:
                    self.assertEqual(_COMMAND_KEYS, set(command))
                    if command["selector"] is not None:
                        self.assertEqual(_SELECTOR_KEYS, set(command["selector"]))
                # No private path / pane / secret / prompt leaks anywhere.
                for needle in _FORBIDDEN_SUBSTRINGS:
                    self.assertNotIn(needle, serialized)


def _record(
    pane_id: str,
    role: str,
    workspace_id: str | None,
    *,
    repo_root: str | None = None,
    session: str = "mozyo-demo",
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
        window_index="1",
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


class GroupedActionPreviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"

    def test_available_for_single_live_pane_without_side_effect(self) -> None:
        snapshot = _snapshot([_record("%7", "claude", "ws-a")])
        # The preview must never perform a side effect: a subprocess call would be
        # a reveal/jump leaking through.
        with _patch_inventory(snapshot), patch(
            f"{COCKPIT_UI}.subprocess.run",
            side_effect=AssertionError("preview must not perform a side effect"),
        ):
            result = grouped_action_preview(
                workspace_id="ws-a", role="claude", home=self.home
            )
        self.assertTrue(result["available"])
        self.assertEqual("preview", result["action"])
        self.assertEqual(["reveal", "jump"], result["actions"])
        self.assertTrue(result["live_preflight_required"])
        # No pane id is surfaced by an available preview (candidate identity only).
        self.assertNotIn("pane_id", result)

    def test_stale_reports_unavailable(self) -> None:
        snapshot = _snapshot([_record("%7", "claude", "ws-a")], stale=True)
        with _patch_inventory(snapshot):
            result = grouped_action_preview(
                workspace_id="ws-a", role="claude", home=self.home
            )
        self.assertFalse(result["available"])
        self.assertIn("stale", result["reason"])

    def test_missing_reports_unavailable(self) -> None:
        with _patch_inventory(_snapshot([])):
            result = grouped_action_preview(
                workspace_id="ws-a", role="claude", home=self.home
            )
        self.assertFalse(result["available"])
        self.assertIn("no live claude pane", result["reason"])

    def test_ambiguous_reports_unavailable(self) -> None:
        snapshot = _snapshot(
            [_record("%7", "claude", "ws-a"), _record("%9", "claude", "ws-a")]
        )
        with _patch_inventory(snapshot):
            result = grouped_action_preview(
                workspace_id="ws-a", role="claude", home=self.home
            )
        self.assertFalse(result["available"])
        self.assertIn("ambiguous", result["reason"])
        self.assertIn("%7", result["reason"])
        self.assertIn("%9", result["reason"])

    def test_bad_candidate_reports_unavailable_without_reading_inventory(self) -> None:
        def boom(**_):
            raise AssertionError("inventory must not be read for a bad candidate")

        with patch(f"{COCKPIT_UI}.take_inventory", boom):
            for kwargs, needle in (
                ({"workspace_id": "ws-a", "role": "claude", "lane_id": "issue_1"},
                 "non-default lane"),
                ({"workspace_id": "ws-a", "role": "claude", "host_id": "remote"},
                 "non-local host"),
                ({"workspace_id": "ws-a", "role": None}, "agent role"),
                ({"workspace_id": "", "role": "claude"}, "workspace_id"),
            ):
                with self.subTest(needle=needle):
                    result = grouped_action_preview(home=self.home, **kwargs)
                    self.assertFalse(result["available"])
                    self.assertIn(needle, result["reason"])


class GroupedPreviewHttpTest(unittest.TestCase):
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

    def test_preview_without_token_is_403(self) -> None:
        status, _ = self._post(
            "/api/actions/grouped-preview",
            {"workspace_id": "ws-a", "role": "claude"},
            with_token=False,
        )
        self.assertEqual(403, status)

    def test_preview_available_is_200(self) -> None:
        snapshot = _snapshot([_record("%7", "claude", "ws-a")])
        with _patch_inventory(snapshot):
            status, payload = self._post(
                "/api/actions/grouped-preview",
                {"workspace_id": "ws-a", "role": "claude"},
            )
        self.assertEqual(200, status)
        self.assertTrue(payload["available"])
        self.assertEqual(["reveal", "jump"], payload["actions"])

    def test_preview_missing_is_200_unavailable(self) -> None:
        with _patch_inventory(_snapshot([])):
            status, payload = self._post(
                "/api/actions/grouped-preview",
                {"workspace_id": "ws-a", "role": "claude"},
            )
        self.assertEqual(200, status)
        self.assertFalse(payload["available"])
        self.assertIn("no live claude pane", payload["reason"])

    def test_preview_stale_is_200_unavailable(self) -> None:
        snapshot = _snapshot([_record("%7", "claude", "ws-a")], stale=True)
        with _patch_inventory(snapshot):
            status, payload = self._post(
                "/api/actions/grouped-preview",
                {"workspace_id": "ws-a", "role": "claude"},
            )
        self.assertEqual(200, status)
        self.assertFalse(payload["available"])
        self.assertIn("stale", payload["reason"])


if __name__ == "__main__":
    unittest.main()
