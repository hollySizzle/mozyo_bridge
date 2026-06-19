"""Grouped cockpit reload / freshness UX view tests (Redmine #12266).

Pins the reload / freshness UX layer over the #12264 grouped read model — the
grouped-view counterpart of the #12225 flat-cockpit observation line + Reload
button:

- observed_at / freshness / reload_required display semantics for the whole
  projection and per group / per Unit (a degraded row reads as needing reload,
  never as current);
- fail-safe freshness: a stale / unreadable / contradicted / unobserved snapshot
  derives reload_required, never healthy / current;
- manual reload affordance semantics (always available, never auto-triggered,
  display-only — authorizes no side effect, moves no workflow gate);
- no continuous polling / push / sidecar observer is introduced (v1 = explicit
  reload + action-time live preflight);
- public-safe, no routing / authority leakage in fields or payload.

Pure projection only — no tmux, file IO, or CLI is exercised.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.grouped_read_model import (
    ObservedUnit,
    build_grouped_read_model,
)
from mozyo_bridge.domain.grouped_reload_view import (
    GROUPED_RELOAD_DIAGNOSTIC_ONLY_NOTE,
    RELOAD_AFFORDANCE_LABEL,
    GroupedReloadView,
    ReloadAffordance,
    build_grouped_reload_view,
)
from mozyo_bridge.domain.presentation_grouping import PresentationGroupingConfig
from mozyo_bridge.domain.runtime_observation import (
    CONTRADICTION_LIVE_RUNTIME_CONFLICT,
    DISPLAY_STATE_HEALTHY,
    DISPLAY_STATE_RELOAD_REQUIRED,
    DISPLAY_STATE_UNKNOWN,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    READABILITY_READABLE,
    READABILITY_UNREADABLE,
    SOURCE_TMUX,
    STALE_REASON_AGE_EXCEEDED,
    STALE_REASON_SOURCE_UNREADABLE,
    STRENGTH_STRONG_RUNTIME_SIGNAL,
    RuntimeObservationSnapshot,
)


def _fresh_observation(
    observed_at: str = "2026-06-19T14:00:00Z",
) -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at=observed_at,
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_FRESH,
        readability=READABILITY_READABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason=None,
        contradiction=None,
        display_state=DISPLAY_STATE_HEALTHY,
    )


def _stale_observation(
    observed_at: str = "2026-06-19T10:00:00Z",
) -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at=observed_at,
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_STALE,
        readability=READABILITY_READABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason=STALE_REASON_AGE_EXCEEDED,
        contradiction=None,
        display_state=DISPLAY_STATE_RELOAD_REQUIRED,
    )


def _unreadable_observation() -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at=None,
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_UNKNOWN,
        readability=READABILITY_UNREADABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason=STALE_REASON_SOURCE_UNREADABLE,
        contradiction=None,
        display_state=DISPLAY_STATE_RELOAD_REQUIRED,
    )


def _contradicted_observation() -> RuntimeObservationSnapshot:
    return RuntimeObservationSnapshot(
        observed_at="2026-06-19T14:00:00Z",
        source=SOURCE_TMUX,
        method="live_query",
        freshness=FRESHNESS_FRESH,
        readability=READABILITY_READABLE,
        strength=STRENGTH_STRONG_RUNTIME_SIGNAL,
        stale_reason="contradicted",
        contradiction=CONTRADICTION_LIVE_RUNTIME_CONFLICT,
        display_state=DISPLAY_STATE_UNKNOWN,
    )


GROUPED_CONFIG_RECORD = {
    "version": 1,
    "project_groups": [
        {"group_id": "project:alpha", "label": "Alpha", "sort_key": 20},
        {"group_id": "project:bravo", "label": "Bravo", "sort_key": 10},
    ],
    "grouping": {
        "membership_rules": [
            {"when": {"repo_label": "alpha"}, "group_id": "project:alpha"},
            {"when": {"repo_label": "bravo"}, "group_id": "project:bravo"},
        ],
    },
}


def _view_from(units, *, config=None, observation=None) -> GroupedReloadView:
    model = build_grouped_read_model(config, units, observation=observation)
    return build_grouped_reload_view(model)


class WholeProjectionFreshnessTests(unittest.TestCase):
    def test_fresh_projection_is_current_and_not_reload_required(self) -> None:
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha",
                          active=True, observation=_fresh_observation())],
            observation=_fresh_observation(),
        )
        self.assertEqual(view.display_state, DISPLAY_STATE_HEALTHY)
        self.assertEqual(view.observed_at, "2026-06-19T14:00:00Z")
        self.assertEqual(view.freshness, FRESHNESS_FRESH)
        self.assertEqual(view.freshness_label, "fresh")
        self.assertFalse(view.reload_required)
        self.assertFalse(view.needs_attention)

    def test_degraded_overall_snapshot_is_reload_required_never_healthy(self) -> None:
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha",
                          active=True, observation=_fresh_observation())],
            observation=_stale_observation(),
        )
        self.assertNotEqual(view.display_state, DISPLAY_STATE_HEALTHY)
        self.assertEqual(view.display_state, DISPLAY_STATE_RELOAD_REQUIRED)
        self.assertTrue(view.reload_required)
        self.assertTrue(view.needs_attention)
        self.assertEqual(view.freshness_label, "stale (age_exceeded)")

    def test_carries_read_model_diagnostics(self) -> None:
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", observation=_fresh_observation())],
            observation=_stale_observation(),
        )
        self.assertTrue(any("reload" in note for note in view.diagnostics))


class UnitFreshnessTests(unittest.TestCase):
    def test_observed_unit_row_is_not_reload_required(self) -> None:
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha",
                          active=True, observation=_fresh_observation())],
            config=PresentationGroupingConfig.from_record(GROUPED_CONFIG_RECORD),
        )
        (unit,) = view.all_units()
        self.assertEqual(unit.freshness, FRESHNESS_FRESH)
        self.assertEqual(unit.freshness_label, "fresh")
        self.assertEqual(unit.observed_at, "2026-06-19T14:00:00Z")
        self.assertFalse(unit.reload_required)

    def test_stale_unit_row_is_reload_required_with_visible_label(self) -> None:
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha",
                          active=True, observation=_stale_observation())],
        )
        (unit,) = view.all_units()
        self.assertEqual(unit.freshness, FRESHNESS_STALE)
        self.assertEqual(unit.stale_reason, STALE_REASON_AGE_EXCEEDED)
        self.assertEqual(unit.freshness_label, "stale (age_exceeded)")
        self.assertTrue(unit.reload_required)

    def test_unobserved_unit_row_reads_unknown_and_reload_required(self) -> None:
        view = _view_from([ObservedUnit(workspace_id="ws-a", repo_label="alpha")])
        (unit,) = view.all_units()
        self.assertEqual(unit.freshness, FRESHNESS_UNKNOWN)
        self.assertEqual(unit.freshness_label, "unknown (missing_source)")
        self.assertIsNone(unit.observed_at)
        self.assertTrue(unit.reload_required)

    def test_unreadable_unit_row_is_reload_required(self) -> None:
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha",
                          observation=_unreadable_observation())],
        )
        (unit,) = view.all_units()
        self.assertTrue(unit.reload_required)
        self.assertEqual(unit.freshness_label, "unknown (source_unreadable)")

    def test_contradicted_unit_row_label_is_contradicted(self) -> None:
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha", active=True,
                          observation=_contradicted_observation())],
        )
        (unit,) = view.all_units()
        self.assertTrue(unit.reload_required)
        self.assertEqual(unit.contradiction, CONTRADICTION_LIVE_RUNTIME_CONFLICT)
        self.assertEqual(
            unit.freshness_label,
            f"contradicted ({CONTRADICTION_LIVE_RUNTIME_CONFLICT})",
        )


class GroupFreshnessTests(unittest.TestCase):
    def test_group_with_stale_member_is_reload_required(self) -> None:
        # Overall snapshot fresh, but one Unit's own observation is stale: the
        # group (and the roll-up) must still surface reload attention.
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha", active=True,
                          observation=_stale_observation())],
            config=PresentationGroupingConfig.from_record(GROUPED_CONFIG_RECORD),
            observation=_fresh_observation(),
        )
        self.assertFalse(view.reload_required)  # whole-snapshot envelope is fresh
        by_id = {group.group_id: group for group in view.groups}
        self.assertTrue(by_id["project:alpha"].reload_required)
        self.assertTrue(view.needs_attention)  # roll-up catches the stale member

    def test_empty_declared_group_is_stale_but_not_reload_required(self) -> None:
        # Bravo has no observed Unit: stale (no live target) yet nothing to reload.
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha", active=True,
                          observation=_fresh_observation())],
            config=PresentationGroupingConfig.from_record(GROUPED_CONFIG_RECORD),
            observation=_fresh_observation(),
        )
        by_id = {group.group_id: group for group in view.groups}
        self.assertTrue(by_id["project:bravo"].stale)
        self.assertFalse(by_id["project:bravo"].reload_required)
        self.assertEqual(by_id["project:bravo"].units, ())

    def test_hidden_unit_freshness_is_surfaced(self) -> None:
        config_record = {
            "version": 1,
            "project_groups": [
                {"group_id": "project:alpha", "label": "Alpha"},
            ],
            "grouping": {
                "membership_rules": [
                    {"when": {"repo_label": "alpha"}, "group_id": "project:alpha"},
                ],
                "unit_overrides": [
                    {"workspace_id": "ws-a", "lane_id": "default", "hidden": True},
                ],
            },
        }
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", lane_id="default",
                          repo_label="alpha", active=True,
                          observation=_stale_observation())],
            config=PresentationGroupingConfig.from_record(config_record),
        )
        # The hidden Unit still appears in the group's freshness rows.
        (unit,) = view.all_units()
        self.assertTrue(unit.reload_required)


class ReloadAffordanceTests(unittest.TestCase):
    def test_reload_is_always_available_even_when_fresh(self) -> None:
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", observation=_fresh_observation())],
            observation=_fresh_observation(),
        )
        self.assertEqual(view.reload.label, RELOAD_AFFORDANCE_LABEL)
        self.assertTrue(view.reload.available)
        self.assertFalse(view.reload.auto)  # operator-driven, never auto

    def test_reload_affordance_is_diagnostic_only(self) -> None:
        affordance = ReloadAffordance()
        self.assertIn("authorizes", affordance.diagnostic_only_note)
        self.assertIn("no side-effecting action", affordance.diagnostic_only_note)

    def test_no_background_observer_introduced(self) -> None:
        # v1 = explicit reload + action-time preflight. The affordance must state
        # it adds no polling / push / sidecar observer, and never auto-fires.
        affordance = ReloadAffordance()
        self.assertFalse(affordance.auto)
        for term in ("polling", "push", "sidecar"):
            self.assertIn(term, affordance.explicit_only_note)


class NoActionPermissionLeakageTests(unittest.TestCase):
    AUTHORITY_TOKENS = (
        "target", "pane", "route", "send", "approval", "credential", "secret",
    )

    def _payload(self) -> dict:
        view = _view_from(
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha", active=True,
                          observation=_fresh_observation())],
            config=PresentationGroupingConfig.from_record(GROUPED_CONFIG_RECORD),
            observation=_fresh_observation(),
        )
        return view.as_payload()

    def test_payload_keys_carry_no_routing_or_authority_token(self) -> None:
        def walk(node) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    lowered = key.lower()
                    for token in self.AUTHORITY_TOKENS:
                        self.assertNotIn(
                            token, lowered, f"authority token {token!r} in key {key!r}"
                        )
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self._payload())

    def test_payload_carries_diagnostic_only_boundary_note(self) -> None:
        self.assertEqual(
            self._payload()["boundary_note"], GROUPED_RELOAD_DIAGNOSTIC_ONLY_NOTE
        )

    def test_payload_has_no_truth_like_workflow_fields(self) -> None:
        forbidden = {"completed", "approved", "current_status", "delivered",
                     "accepted"}

        def keys(node) -> set:
            found: set = set()
            if isinstance(node, dict):
                found |= set(node)
                for value in node.values():
                    found |= keys(value)
            elif isinstance(node, list):
                for item in node:
                    found |= keys(item)
            return found

        self.assertEqual(keys(self._payload()) & forbidden, set())


class EmptyInputTests(unittest.TestCase):
    def test_no_units_no_config_yields_empty_reloadable_view(self) -> None:
        view = _view_from([])
        self.assertEqual(view.groups, ())
        self.assertEqual(view.all_units(), ())
        # No observation supplied -> never-refreshed -> reload_required, unknown.
        self.assertTrue(view.reload_required)
        self.assertEqual(view.freshness, FRESHNESS_UNKNOWN)
        self.assertTrue(view.reload.available)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
