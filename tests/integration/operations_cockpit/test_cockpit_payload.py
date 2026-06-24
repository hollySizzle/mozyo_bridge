"""Served cockpit API payload-assembly contract tests (Redmine #12323 split).

Focused on :mod:`mozyo_bridge.application.cockpit_payload`: the read-only
served-API projections, independent of the HTTP server, the HTML page, and the
action / preflight bridge. Split out of ``test_cockpit_ui`` and
``test_grouped_served_cockpit`` (#12323) so the payload contract is tested by
responsibility:

- the flat units payload + its additive join layers (``attach_attention`` /
  ``attach_observation``) stay additive and fail closed on a stale snapshot;
- ``observed_units_from_inventory`` aggregates the pane-centric inventory into
  Unit-centric rows (role set, lane split, stale = inactive, lane-ambiguous =
  contradicted); and
- ``grouped_units_payload`` composes the repo-local grouping config with a live
  snapshot into the served grouped display payload (identity + role presence
  only, never a pane / target).

Everything runs on a patched inventory — no real tmux. The HTTP wiring that
serves these payloads is in ``test_cockpit_ui`` / ``test_grouped_served_cockpit``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cockpit_actions import (
    CockpitActionError,
    candidate_unit_selector,
)
from mozyo_bridge.application.cockpit_payload import (
    attach_attention,
    attach_observation,
    grouped_units_payload,
    observed_units_from_inventory,
    units_payload,
)
from mozyo_bridge.domain import runtime_observation as ro
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
    take_inventory,
)

# The payload builders import take_inventory into the cockpit_payload namespace,
# so patch it there (not at session_inventory).
COCKPIT_PAYLOAD = "mozyo_bridge.application.cockpit_payload"


def pane(pane_id: str, session: str, agent: str, cwd: str = "") -> dict:
    return {
        "id": pane_id,
        "location": f"{session}:1.0",
        "command": agent,
        "cwd": cwd,
        "window_name": agent,
        "pane_active": "1",
    }


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


class UnitsPayloadContractTest(unittest.TestCase):
    """The flat ``units_payload`` + its additive served layers form one contract."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"

    def test_units_payload_is_the_inventory_snapshot_payload(self) -> None:
        snapshot = _snapshot([_record("%1", "claude", "ws-a")])
        with patch(f"{COCKPIT_PAYLOAD}.take_inventory", lambda **_: snapshot):
            payload = units_payload(home=self.home)
        self.assertFalse(payload["stale"])
        self.assertEqual(1, len(payload["panes"]))
        self.assertEqual("%1", payload["panes"][0]["pane_id"])
        # The flat payload carries no attention / observation layer yet: those are
        # additive joins the served endpoint attaches on top.
        self.assertNotIn("attention", payload["panes"][0])
        self.assertNotIn("observation", payload)

    def test_full_served_assembly_layers_attention_and_observation(self) -> None:
        # Pin the served /api/units assembly as a pure contract: units_payload ->
        # attach_attention -> attach_observation produces a per-pane attention
        # layer and a top-level observation envelope, additively, with no
        # truth-like generic field leaking into the observation.
        snapshot = take_inventory(
            home=self.home, panes=[pane("%1", "mozyo-demo", "claude")]
        )
        now = datetime.now(timezone.utc)
        with patch(f"{COCKPIT_PAYLOAD}.take_inventory", lambda **_: snapshot):
            payload = units_payload(home=self.home)
        payload = attach_attention(
            payload, observed_at=now.isoformat(timespec="seconds")
        )
        payload = attach_observation(payload, snapshot, now=now)
        row = payload["panes"][0]
        self.assertEqual("%1", row["pane_id"])
        self.assertIn("activity", row)
        self.assertEqual("healthy", row["attention"]["attention_state"])
        self.assertEqual(
            DISPLAY_STATE_HEALTHY, payload["observation"]["display_state"]
        )
        self.assertEqual(
            [], ro.forbidden_generic_fields(payload["observation"])
        )


class AttachAttentionTest(unittest.TestCase):
    """The additive ``attention`` join layer (Redmine #12007)."""

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


class AttachObservationTest(unittest.TestCase):
    """The additive runtime-observation freshness envelope (Redmine #12225)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"

    def test_attach_observation_is_additive(self) -> None:
        # Pure-join unit test: `attach_observation` adds only the top-level
        # `observation` key and never disturbs the panes / stale / attention
        # layers already on the payload.
        snapshot = take_inventory(
            home=self.home, panes=[pane("%1", "mozyo-demo", "claude")]
        )
        payload = snapshot.as_payload()
        payload["panes"][0]["attention"] = {"attention_state": "healthy"}
        before = json.dumps(payload, sort_keys=True)
        out = attach_observation(
            payload, snapshot, now=datetime.now(timezone.utc)
        )
        self.assertIs(out, payload)
        self.assertIn("observation", out)
        # Existing layers preserved (additive only).
        self.assertEqual("%1", out["panes"][0]["pane_id"])
        self.assertEqual(
            "healthy", out["panes"][0]["attention"]["attention_state"]
        )
        # Removing the added key restores the original payload exactly.
        out.pop("observation")
        self.assertEqual(before, json.dumps(out, sort_keys=True))


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
        with patch(f"{COCKPIT_PAYLOAD}.take_inventory", lambda **_: snapshot):
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
        with patch(f"{COCKPIT_PAYLOAD}.take_inventory", lambda **_: snapshot):
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
        with patch(f"{COCKPIT_PAYLOAD}.take_inventory", lambda **_: snapshot):
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
            f"{COCKPIT_PAYLOAD}.take_inventory",
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
            f"{COCKPIT_PAYLOAD}.take_inventory",
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


if __name__ == "__main__":
    unittest.main()
