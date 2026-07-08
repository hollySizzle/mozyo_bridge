"""Cockpit grouped-view herdr supply tests (Redmine #13356, design j#73386).

Pins the backend axis end to end: the grouped read model carries ``backend`` /
per-role runtime receiver-states / the lane metadata display join; the display
view surfaces them (row runtime label, runtime-blocked flag, herdr summary
counts); and the :func:`herdr_observed_units` supplier is default-off,
fail-visible on an unreadable inventory, and fail-open on a missing lane
record. tmux rows stay display-compatible throughout (backend defaults).
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_metadata import LaneMetadataRecord  # noqa: E402
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.grouped_read_model import (  # noqa: E402,E501
    BACKEND_HERDR,
    BACKEND_TMUX,
    ObservedUnit,
    build_grouped_read_model,
)
from mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application.cockpit_payload import (  # noqa: E402,E501
    HERDR_INVENTORY_UNAVAILABLE_DIAGNOSTIC,
    herdr_observed_units,
    reconcile_whole_view_observation,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation import (  # noqa: E402,E501
    CONTRADICTION_LIVE_RUNTIME_CONFLICT,
    DISPLAY_STATE_HEALTHY,
    DISPLAY_STATE_RELOAD_REQUIRED,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    METHOD_LIVE_QUERY,
    READABILITY_READABLE,
    SOURCE_HERDR,
    SOURCE_TMUX,
    STALE_REASON_SOURCE_UNREADABLE,
    STRENGTH_STRONG_RUNTIME_SIGNAL,
    RuntimeObservationSnapshot,
)
from mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.domain.grouped_display import (  # noqa: E402,E501
    build_grouped_display_view,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

_NOW = datetime(2026, 7, 8, 1, 0, 0, tzinfo=timezone.utc)


def _agent_row(ws: str, role: str, locator: str, status: str) -> dict:
    return {
        "name": encode_assigned_name(ws, role, ""),
        "pane_id": locator,
        "agent_status": status,
    }


class _FakeLister:
    def __init__(self, rows=None, error=None):
        self._rows = rows or []
        self._error = error

    def list_agent_rows(self):
        if self._error is not None:
            raise self._error
        return self._rows


class _FakeRegistryRecord:
    def __init__(self, project_name: str):
        self.project_name = project_name
        self.canonical_session = project_name


class ReadModelBackendAxisTest(unittest.TestCase):
    def test_tmux_default_keeps_unit_id_and_backend(self) -> None:
        unit = ObservedUnit(workspace_id="ws-a")
        self.assertEqual(unit.backend, BACKEND_TMUX)
        self.assertEqual(unit.unit_id(), "unit:local:ws-a:default")

    def test_herdr_unit_id_is_backend_qualified(self) -> None:
        unit = ObservedUnit(workspace_id="wt_abc", backend=BACKEND_HERDR)
        self.assertEqual(unit.unit_id(), "unit:local:wt_abc:default:herdr")

    def test_unit_payload_carries_backend_runtime_and_lane_join(self) -> None:
        model = build_grouped_read_model(
            None,
            [
                ObservedUnit(
                    workspace_id="wt_abc",
                    repo_label="alpha",
                    active=True,
                    roles=("codex", "claude"),
                    backend=BACKEND_HERDR,
                    role_runtime_states=(
                        ("codex", "awaiting_input"),
                        ("claude", "busy"),
                    ),
                    lane_label="issue_13356_cockpit_aggregate",
                    issue="13356",
                )
            ],
        )
        payload = model.as_payload()["groups"][0]["units"][0]
        self.assertEqual(payload["backend"], BACKEND_HERDR)
        self.assertEqual(
            payload["runtime_states"],
            {"codex": "awaiting_input", "claude": "busy"},
        )
        self.assertEqual(payload["lane_label"], "issue_13356_cockpit_aggregate")
        self.assertEqual(payload["issue"], "13356")


class DisplayRuntimeObservationTest(unittest.TestCase):
    def _view(self, units):
        return build_grouped_display_view(build_grouped_read_model(None, units))

    def test_herdr_row_shows_lane_label_issue_and_runtime(self) -> None:
        view = self._view(
            [
                ObservedUnit(
                    workspace_id="wt_abc",
                    repo_label="alpha",
                    active=True,
                    roles=("codex", "claude"),
                    backend=BACKEND_HERDR,
                    role_runtime_states=(
                        ("codex", "busy"),
                        ("claude", "awaiting_input"),
                    ),
                    lane_label="issue_13356_cockpit_aggregate",
                    issue="13356",
                )
            ]
        )
        row = view.all_units()[0]
        self.assertEqual(row.backend, BACKEND_HERDR)
        self.assertEqual(row.lane_label, "issue_13356_cockpit_aggregate")
        self.assertEqual(row.issue, "13356")
        self.assertEqual(row.runtime_label, "codex:busy, claude:awaiting_input")
        self.assertFalse(row.runtime_blocked)

    def test_runtime_blocked_is_row_level_and_labelled_apart(self) -> None:
        view = self._view(
            [
                ObservedUnit(
                    workspace_id="wt_x",
                    repo_label="alpha",
                    active=True,
                    roles=("codex",),
                    backend=BACKEND_HERDR,
                    role_runtime_states=(("codex", "blocked"),),
                    lane_label="issue_1_x",
                    issue="1",
                )
            ]
        )
        row = view.all_units()[0]
        self.assertTrue(row.runtime_blocked)
        # Deliberately NOT a summary field (the summary vocabulary never carries
        # a token readable as the Redmine workflow gate).
        self.assertNotIn("blocked", str(sorted(view.summary.as_payload())))

    def test_summary_counts_herdr_roles(self) -> None:
        view = self._view(
            [
                ObservedUnit(
                    workspace_id="ws-tmux", repo_label="alpha", active=True,
                    roles=("codex", "claude"),
                ),
                ObservedUnit(
                    workspace_id="wt_a", repo_label="alpha", active=True,
                    roles=("codex", "claude"), backend=BACKEND_HERDR,
                    role_runtime_states=(("codex", "busy"), ("claude", "unknown")),
                    lane_label="issue_2_a", issue="2",
                ),
            ]
        )
        summary = view.summary
        self.assertEqual(summary.herdr_units, 1)
        self.assertEqual(summary.herdr_live_roles, 2)
        self.assertEqual(summary.herdr_working_roles, 1)
        self.assertEqual(summary.herdr_unknown_roles, 1)

    def test_tmux_only_summary_counts_are_zero(self) -> None:
        view = self._view(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha", active=True)]
        )
        summary = view.summary.as_payload()
        self.assertEqual(summary["herdr_units"], 0)
        self.assertEqual(summary["herdr_live_roles"], 0)

    def test_tmux_row_display_fields_unchanged(self) -> None:
        view = self._view(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha", active=True,
                          roles=("codex",))]
        )
        row = view.all_units()[0]
        self.assertEqual(row.backend, BACKEND_TMUX)
        self.assertEqual(row.lane_label, "default")  # lane_id verbatim
        self.assertIsNone(row.issue)
        self.assertEqual(row.runtime_label, "")
        self.assertFalse(row.runtime_blocked)


class HerdrObservedUnitsSupplierTest(unittest.TestCase):
    """The live-supply fold, with the config / lister / stores patched."""

    def _run(
        self,
        *,
        lister,
        lane_records=None,
        registry=None,
    ):
        config = object()

        class _TT:
            terminal_transport = config

        with mock.patch(
            "mozyo_bridge.application.repo_local_config_loader.load_repo_local_config",
            return_value=_TT(),
        ), mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "infrastructure.herdr_discovery.resolve_agent_lister",
            return_value=lister,
        ), mock.patch(
            "mozyo_bridge.core.state.lane_metadata.load_lane_records",
            return_value=lane_records or {},
        ), mock.patch(
            "mozyo_bridge.core.state.workspace_registry.load_workspace_by_id",
            side_effect=(registry or {}).get,
        ):
            return herdr_observed_units(repo_root=None, now=_NOW)

    def test_backend_off_yields_nothing(self) -> None:
        units, diagnostics = self._run(lister=None)
        self.assertEqual(units, [])
        self.assertEqual(diagnostics, [])

    def test_unreadable_inventory_is_a_visible_diagnostic(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
            TerminalTransportError,
        )

        units, diagnostics = self._run(
            lister=_FakeLister(error=TerminalTransportError("server down"))
        )
        self.assertEqual(units, [])
        self.assertEqual(len(diagnostics), 1)
        self.assertIn(HERDR_INVENTORY_UNAVAILABLE_DIAGNOSTIC, diagnostics[0])

    def test_folds_lane_and_main_units_with_metadata_join(self) -> None:
        record = LaneMetadataRecord(
            lane_workspace_token="wt_abc",
            repo_workspace_id="wsMain",
            issue_id="13356",
            lane_label="issue_13356_cockpit_aggregate",
        )
        units, diagnostics = self._run(
            lister=_FakeLister(
                rows=[
                    _agent_row("wt_abc", "codex", "wD:p2", "idle"),
                    _agent_row("wt_abc", "claude", "wD:p3", "working"),
                    _agent_row("wsMain", "codex", "w2:p3", "idle"),
                    _agent_row("wsMain", "claude", "w2:p2", "working"),
                    # A foreign non-mzb1 agent is dropped.
                    {"name": "someones-shell", "pane_id": "wZ:p1"},
                ]
            ),
            lane_records={"wt_abc": record},
            registry={"wsMain": _FakeRegistryRecord("mozyo_bridge")},
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual(len(units), 2)
        by_ws = {unit.workspace_id: unit for unit in units}
        lane = by_ws["wt_abc"]
        self.assertEqual(lane.backend, BACKEND_HERDR)
        # The lane groups under its project label via repo_workspace_id.
        self.assertEqual(lane.repo_label, "mozyo_bridge")
        self.assertEqual(lane.lane_label, "issue_13356_cockpit_aggregate")
        self.assertEqual(lane.issue, "13356")
        # herdr agent_status maps through the core receiver-state vocabulary.
        self.assertEqual(
            dict(lane.role_runtime_states),
            {"codex": "awaiting_input", "claude": "busy"},
        )
        self.assertTrue(lane.active)
        self.assertEqual(lane.observation.source, "herdr")
        self.assertEqual(lane.observation.freshness, "fresh")
        main = by_ws["wsMain"]
        self.assertEqual(main.repo_label, "mozyo_bridge")
        self.assertIsNone(main.lane_label)

    def test_missing_lane_record_degrades_to_token_with_diagnostic(self) -> None:
        units, diagnostics = self._run(
            lister=_FakeLister(rows=[_agent_row("wt_orphan", "codex", "wX:p2", "idle")]),
            lane_records={},
            registry={},
        )
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].repo_label, "wt_orphan")
        # j#73386 Q2 / j#73436 finding 1: the raw token IS the degraded lane
        # label, so the display row never collapses it to the lane id.
        self.assertEqual(units[0].lane_label, "wt_orphan")
        self.assertTrue(any("lane_record_missing" in d for d in diagnostics))

    def test_missing_lane_record_display_row_shows_raw_token(self) -> None:
        # Regression for j#73436 finding 1: pin the degrade all the way to the
        # rendered display row — its lane_label must be the raw wt_<hash>
        # token, never the lane id (`default`).
        units, _diagnostics = self._run(
            lister=_FakeLister(rows=[_agent_row("wt_orphan", "codex", "wX:p2", "idle")]),
            lane_records={},
            registry={},
        )
        view = build_grouped_display_view(build_grouped_read_model(None, units))
        row = view.all_units()[0]
        self.assertEqual(row.lane_label, "wt_orphan")
        self.assertEqual(row.backend, BACKEND_HERDR)
        self.assertIsNone(row.issue)

    def test_duplicate_role_degrades_to_contradiction(self) -> None:
        units, _diagnostics = self._run(
            lister=_FakeLister(
                rows=[
                    _agent_row("wt_a", "codex", "w1:p2", "idle"),
                    _agent_row("wt_a", "codex", "w1:p9", "idle"),
                ]
            ),
            lane_records={},
            registry={},
        )
        self.assertEqual(len(units), 1)
        self.assertIsNotNone(units[0].observation.contradiction)


def _tmux_stale_envelope() -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at="2026-07-08T00:00:00+00:00",
        source=SOURCE_TMUX,
        method=METHOD_LIVE_QUERY,
        freshness=FRESHNESS_STALE,
        readability=READABILITY_READABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason=STALE_REASON_SOURCE_UNREADABLE,
        contradiction=None,
        display_state=DISPLAY_STATE_RELOAD_REQUIRED,
    )


def _herdr_fresh_envelope() -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at="2026-07-08T01:00:00+00:00",
        source=SOURCE_HERDR,
        method=METHOD_LIVE_QUERY,
        freshness=FRESHNESS_FRESH,
        readability=READABILITY_READABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason=None,
        contradiction=None,
        display_state=DISPLAY_STATE_HEALTHY,
    )


class ReconcileWholeViewObservationTest(unittest.TestCase):
    """The #13367 whole-view freshness reconciliation (pure)."""

    def _herdr_unit(self, observation):
        return ObservedUnit(
            workspace_id="wt_abc",
            repo_label="alpha",
            active=True,
            roles=("codex",),
            backend=BACKEND_HERDR,
            observation=observation,
            lane_label="issue_1_x",
            issue="1",
        )

    def test_no_herdr_units_keeps_tmux_envelope(self) -> None:
        tmux = _tmux_stale_envelope()
        got = reconcile_whole_view_observation(
            tmux_observation=tmux, tmux_unit_count=2, herdr_units=[]
        )
        self.assertIs(got, tmux)

    def test_herdr_only_uses_fresh_herdr_envelope_not_stale_tmux(self) -> None:
        # The misleading case: stale tmux snapshot, a herdr-only display of fresh
        # live-queried lanes. The whole-view must read the herdr envelope.
        herdr = _herdr_fresh_envelope()
        got = reconcile_whole_view_observation(
            tmux_observation=_tmux_stale_envelope(),
            tmux_unit_count=0,
            herdr_units=[self._herdr_unit(herdr)],
        )
        self.assertEqual(SOURCE_HERDR, got.source)
        self.assertEqual(FRESHNESS_FRESH, got.freshness)
        self.assertFalse(got.needs_reload)

    def test_herdr_only_worst_wins_surfaces_degraded_unit(self) -> None:
        # An ambiguous / contradicted herdr Unit makes the whole herdr view read as
        # needing reload — never healthier than its rows.
        degraded = _herdr_fresh_envelope()
        degraded = RuntimeObservationSnapshot(
            **{
                **degraded.as_payload(),
                "contradiction": CONTRADICTION_LIVE_RUNTIME_CONFLICT,
                "display_state": DISPLAY_STATE_RELOAD_REQUIRED,
                "source_refs": (),
                "notes": (),
            }
        )
        got = reconcile_whole_view_observation(
            tmux_observation=_tmux_stale_envelope(),
            tmux_unit_count=0,
            herdr_units=[
                self._herdr_unit(_herdr_fresh_envelope()),
                self._herdr_unit(degraded),
            ],
        )
        self.assertTrue(got.needs_reload)

    def test_both_present_keeps_tmux_envelope(self) -> None:
        # When tmux rows ARE shown, their staleness legitimately applies to the
        # whole view; each herdr row keeps its own fresh per-row envelope.
        tmux = _tmux_stale_envelope()
        got = reconcile_whole_view_observation(
            tmux_observation=tmux,
            tmux_unit_count=1,
            herdr_units=[self._herdr_unit(_herdr_fresh_envelope())],
        )
        self.assertIs(got, tmux)


class GroupedPayloadWholeViewFreshnessTest(unittest.TestCase):
    """End-to-end #13367: the served grouped payload's whole-view freshness line.

    A herdr-only display over a stale / empty tmux inventory must NOT read as stale:
    the freshly-queried herdr rows would otherwise be tarred by the tmux snapshot's
    staleness. Drives ``grouped_units_payload`` with the tmux inventory and the herdr
    supply both mocked.
    """

    _PAYLOAD = (
        "mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui."
        "application.cockpit_payload"
    )

    def _fake_snapshot(self, *, records, stale):
        from mozyo_bridge.session_inventory import InventorySnapshot

        return InventorySnapshot(
            records=tuple(records),
            collected_at=None,
            source="cache" if stale else "runtime",
            stale=stale,
            inventory_path=Path("/tmp/inv.sqlite"),
        )

    def _run(self, *, tmux_snapshot, herdr_rows):
        from mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application import (  # noqa: E501
            cockpit_payload,
        )
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping import (  # noqa: E501
            PresentationGroupingConfig,
        )

        class _Config:
            presentation = type(
                "_P", (), {"grouping": PresentationGroupingConfig.default()}
            )()
            terminal_transport = object()

        record = LaneMetadataRecord(
            lane_workspace_token="wt_abc",
            repo_workspace_id="wsMain",
            issue_id="13367",
            lane_label="issue_13367_cockpit_herdr_polish",
        )
        with mock.patch.object(
            cockpit_payload, "take_inventory", return_value=tmux_snapshot
        ), mock.patch(
            "mozyo_bridge.application.repo_local_config_loader.load_repo_local_config",
            return_value=_Config(),
        ), mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "infrastructure.herdr_discovery.resolve_agent_lister",
            return_value=_FakeLister(rows=herdr_rows),
        ), mock.patch(
            "mozyo_bridge.core.state.lane_metadata.load_lane_records",
            return_value={"wt_abc": record},
        ), mock.patch(
            "mozyo_bridge.core.state.workspace_registry.load_workspace_by_id",
            side_effect={"wsMain": _FakeRegistryRecord("mozyo_bridge")}.get,
        ):
            return cockpit_payload.grouped_units_payload(now=_NOW)

    def test_herdr_only_over_stale_tmux_reads_fresh(self) -> None:
        payload = self._run(
            tmux_snapshot=self._fake_snapshot(records=[], stale=True),
            herdr_rows=[
                _agent_row("wt_abc", "codex", "wD:p2", "idle"),
                _agent_row("wt_abc", "claude", "wD:p3", "working"),
            ],
        )
        # The whole-view freshness line reflects the live herdr query, not the
        # stale tmux snapshot: fresh, not reload_required.
        self.assertEqual(FRESHNESS_FRESH, payload["freshness"])
        self.assertFalse(payload["reload_required"])
        # The herdr lane row still shows its recorded identity.
        rows = [u for g in payload["groups"] for u in g["units"]]
        self.assertTrue(
            any(u["lane_label"] == "issue_13367_cockpit_herdr_polish" for u in rows)
        )

    def test_tmux_present_stale_keeps_reload_required(self) -> None:
        # A genuine tmux Unit that IS stale keeps the whole view reload_required —
        # its staleness legitimately applies (byte-invariant behaviour retained).
        tmux_record = _record_inventory()
        payload = self._run(
            tmux_snapshot=self._fake_snapshot(records=[tmux_record], stale=True),
            herdr_rows=[_agent_row("wt_abc", "codex", "wD:p2", "idle")],
        )
        self.assertTrue(payload["reload_required"])


def _record_inventory():
    from mozyo_bridge.session_inventory import InventoryRecord, WorkspaceIdentity

    return InventoryRecord(
        pane_id="%1",
        session="mozyo-demo",
        window_index="1",
        window_name="codex",
        pane_index="0",
        pane_active=True,
        process="codex",
        cwd="/tmp",
        repo_root="/tmp",
        agent_kind="codex",
        lane_id="default",
        workspace=WorkspaceIdentity(
            workspace_id="wsTmux",
            canonical_session="mozyo-demo",
            project_name="mozyo_bridge",
            source="test",
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
