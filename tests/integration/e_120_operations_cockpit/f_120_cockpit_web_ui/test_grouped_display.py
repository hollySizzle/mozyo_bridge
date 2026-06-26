"""Grouped cockpit sublane display view tests (Redmine #12255).

Pins the display / render projection over the #12264 grouped read model and the
#12266 reload / freshness view — the slice that makes the grouped cockpit
renderable as one row set:

- Project Group header rendering (label / source / managed), including an
  unmanaged default / ungrouped bucket that stays distinguishable;
- per-Unit lane label, issue label, and Codex / Claude role-pane presence
  (role names only, canonical order, "no roles" placeholder) — the acceptance
  criterion "Unit ごとに lane label / issue label / Codex・Claude role panes が
  判別できる";
- stale / unknown / unmanaged state kept visible: degraded status / freshness /
  reload_required surfaced, never collapsed to current; a no-live-target group
  stays stale; hidden rows shown in a separate bucket;
- reuse of the #12266 reload view (freshness labels, reload_required) and its
  reload affordance (always available, never auto, display-only);
- public-safe, no routing / approval / close authority leakage in fields or
  payload.

Pure projection only — no tmux, file IO, or CLI is exercised.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.grouped_display import (
    ATTENTION_CANDIDATE_STATUSES,
    GROUPED_DISPLAY_DIAGNOSTIC_ONLY_NOTE,
    GROUPED_SUMMARY_DIAGNOSTIC_ONLY_NOTE,
    NO_ROLES_LABEL,
    GroupAttentionSummary,
    GroupDisplaySection,
    GroupedDisplayView,
    UnitDisplayRow,
    build_grouped_display_view,
)
from mozyo_bridge.domain.grouped_read_model import (
    GROUP_SOURCE_DEFAULT,
    GROUP_SOURCE_DESIRED,
    ObservedUnit,
    build_grouped_read_model,
)
from mozyo_bridge.domain.grouped_reload_view import build_grouped_reload_view
from mozyo_bridge.domain.presentation_grouping import (
    PresentationGroupingConfig,
)
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


def _config() -> PresentationGroupingConfig:
    return PresentationGroupingConfig.from_record(GROUPED_CONFIG_RECORD)


def _view_from(units, *, config=None, observation=None) -> GroupedDisplayView:
    model = build_grouped_read_model(config, units, observation=observation)
    return build_grouped_display_view(model)


def _row_by_unit(view: GroupedDisplayView, unit_id: str) -> UnitDisplayRow:
    for row in view.all_units():
        if row.unit_id == unit_id:
            return row
    raise AssertionError(f"no display row for {unit_id!r}")


class ProjectGroupHeaderTests(unittest.TestCase):
    def test_declared_group_header_is_managed_with_its_label(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a",
                    repo_label="alpha",
                    active=True,
                    roles=("codex", "claude"),
                    observation=_fresh_observation(),
                )
            ],
            config=_config(),
            observation=_fresh_observation(),
        )
        sections = {g.group_id: g for g in view.groups}
        alpha = sections["project:alpha"]
        self.assertEqual(alpha.header_label, "Alpha")
        self.assertEqual(alpha.source, GROUP_SOURCE_DESIRED)
        self.assertTrue(alpha.managed)

    def test_declared_groups_keep_sort_order_even_when_empty(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    observation=_fresh_observation(),
                )
            ],
            config=_config(),
            observation=_fresh_observation(),
        )
        ordered_ids = [g.group_id for g in view.groups]
        # bravo (sort_key 10) before alpha (sort_key 20); empty bravo still shown.
        self.assertEqual(ordered_ids[:2], ["project:bravo", "project:alpha"])
        bravo = next(g for g in view.groups if g.group_id == "project:bravo")
        self.assertEqual(bravo.units, ())
        self.assertTrue(bravo.stale)  # no live target -> visible stale, not dropped

    def test_default_group_is_unmanaged_and_distinguishable(self) -> None:
        # No config: a Unit lands in a default bucket keyed on its repo label.
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    roles=("codex",), observation=_fresh_observation(),
                )
            ],
            observation=_fresh_observation(),
        )
        (section,) = view.groups
        self.assertEqual(section.source, GROUP_SOURCE_DEFAULT)
        self.assertFalse(section.managed)
        self.assertEqual(section.header_label, "alpha")
        # The unmanaged-ness propagates to its rows so it is never hidden.
        self.assertFalse(section.units[0].managed)


class UnitRowDisplayTests(unittest.TestCase):
    def test_row_shows_lane_issue_and_role_panes(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a",
                    lane_id="issue_12255",
                    repo_label="alpha",
                    active=True,
                    roles=("claude", "codex"),
                    observation=_fresh_observation(),
                )
            ],
            config=_config(),
            observation=_fresh_observation(),
        )
        row = _row_by_unit(view, "unit:local:ws-a:issue_12255")
        self.assertEqual(row.lane_label, "issue_12255")
        self.assertEqual(row.issue_label, "Alpha")
        # Codex / Claude role panes are distinguishable, codex shown first.
        self.assertEqual(row.roles, ("codex", "claude"))
        self.assertEqual(row.role_label, "codex, claude")

    def test_role_presence_is_canonicalized(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    roles=("claude", "CODEX", " claude ", "watcher"),
                    observation=_fresh_observation(),
                )
            ],
            observation=_fresh_observation(),
        )
        row = view.all_units()[0]
        # Deduped (case-folded), known roles in canonical order, extras sorted last.
        self.assertEqual(row.roles, ("CODEX", "claude", "watcher"))

    def test_no_observed_role_pane_reads_as_placeholder(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha",
                    observation=_fresh_observation(),
                )
            ],
            observation=_fresh_observation(),
        )
        row = view.all_units()[0]
        self.assertEqual(row.roles, ())
        self.assertEqual(row.role_label, NO_ROLES_LABEL)


class VisibleDegradedStateTests(unittest.TestCase):
    def test_stale_row_stays_visible_and_reload_required(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    roles=("codex",), observation=_stale_observation(),
                )
            ],
            observation=_stale_observation(),
        )
        row = view.all_units()[0]
        self.assertEqual(row.status, "stale")
        self.assertEqual(row.state_label, "stale")
        self.assertTrue(row.reload_required)
        self.assertEqual(row.freshness_label, "stale (age_exceeded)")
        self.assertNotEqual(view.display_state, DISPLAY_STATE_HEALTHY)

    def test_unreadable_row_is_unknown_freshness_never_current(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    observation=_unreadable_observation(),
                )
            ],
            observation=_unreadable_observation(),
        )
        row = view.all_units()[0]
        self.assertEqual(row.status, "unreadable")
        self.assertTrue(row.reload_required)

    def test_contradicted_row_surfaces_contradiction(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    observation=_contradicted_observation(),
                )
            ],
            observation=_contradicted_observation(),
        )
        row = view.all_units()[0]
        self.assertEqual(row.status, "contradicted")
        self.assertEqual(row.contradiction, CONTRADICTION_LIVE_RUNTIME_CONFLICT)
        self.assertIn("contradicted", row.freshness_label)
        self.assertTrue(row.reload_required)

    def test_never_observed_unit_reads_unknown(self) -> None:
        view = _view_from([ObservedUnit(workspace_id="ws-a", repo_label="alpha")])
        row = view.all_units()[0]
        self.assertEqual(row.status, "unknown")
        self.assertEqual(row.freshness, FRESHNESS_UNKNOWN)
        self.assertTrue(row.reload_required)


class HiddenAndStaleBucketTests(unittest.TestCase):
    def test_hidden_but_active_unit_shown_in_hidden_bucket(self) -> None:
        record = {
            "version": 1,
            "project_groups": [
                {"group_id": "project:alpha", "label": "Alpha", "sort_key": 10},
            ],
            "grouping": {
                "membership_rules": [
                    {"when": {"repo_label": "alpha"}, "group_id": "project:alpha"},
                ],
                "unit_overrides": [
                    {
                        "workspace_id": "ws-hidden",
                        "lane_id": "default",
                        "preferred_group": "project:alpha",
                        "hidden": True,
                    }
                ],
            },
        }
        config = PresentationGroupingConfig.from_record(record)
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    roles=("codex",), observation=_fresh_observation(),
                ),
                ObservedUnit(
                    workspace_id="ws-hidden", repo_label="alpha", active=True,
                    roles=("claude",), observation=_fresh_observation(),
                ),
            ],
            config=config,
            observation=_fresh_observation(),
        )
        alpha = next(g for g in view.groups if g.group_id == "project:alpha")
        self.assertEqual(len(alpha.units), 1)
        self.assertEqual(len(alpha.hidden_units), 1)
        hidden_row = alpha.hidden_units[0]
        self.assertTrue(hidden_row.hidden)
        self.assertTrue(hidden_row.active)  # hidden preference != killed
        self.assertIn(hidden_row, alpha.all_units())

    def test_group_with_no_live_target_is_stale(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=False,
                    observation=_stale_observation(),
                )
            ],
            config=_config(),
            observation=_stale_observation(),
        )
        alpha = next(g for g in view.groups if g.group_id == "project:alpha")
        self.assertTrue(alpha.stale)


class ReloadReuseTests(unittest.TestCase):
    def test_whole_view_freshness_comes_from_reload_view(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    roles=("codex", "claude"), observation=_fresh_observation(),
                )
            ],
            observation=_fresh_observation(),
        )
        self.assertEqual(view.display_state, DISPLAY_STATE_HEALTHY)
        self.assertEqual(view.observed_at, "2026-06-19T14:00:00Z")
        self.assertEqual(view.freshness_label, "fresh")
        self.assertFalse(view.reload_required)
        self.assertFalse(view.needs_attention)

    def test_reload_affordance_is_always_available_never_auto(self) -> None:
        view = _view_from([ObservedUnit(workspace_id="ws-a", repo_label="alpha")])
        self.assertTrue(view.reload.available)
        self.assertFalse(view.reload.auto)

    def test_needs_attention_when_a_member_is_degraded(self) -> None:
        # Whole-projection snapshot fresh, but a per-Unit observation is stale.
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    observation=_stale_observation(),
                )
            ],
            observation=_fresh_observation(),
        )
        self.assertFalse(view.reload_required)  # whole snapshot fresh
        self.assertTrue(view.needs_attention)  # but a member needs reload
        section = view.groups[0]
        self.assertTrue(section.reload_required)

    def test_accepts_a_shared_reload_view(self) -> None:
        model = build_grouped_read_model(
            None,
            [ObservedUnit(workspace_id="ws-a", repo_label="alpha",
                          observation=_fresh_observation())],
            observation=_fresh_observation(),
        )
        shared = build_grouped_reload_view(model)
        view = build_grouped_display_view(model, reload_view=shared)
        self.assertEqual(view.observed_at, shared.observed_at)
        self.assertEqual(view.freshness_label, shared.freshness_label)


class NoActionPermissionLeakageTests(unittest.TestCase):
    # The routing / authority token set (matching the #12266 reload-view test).
    # The broader config-loading tokens in ``_FORBIDDEN_KEY_PARTS`` (e.g. "load")
    # are not used here because a display row legitimately carries reload-freshness
    # semantics (``reload_required``); what must never appear is a routing /
    # delivery / approval field.
    AUTHORITY_TOKENS = (
        "target", "pane", "route", "send", "approval", "credential", "secret",
    )

    def _assert_no_boundary_tokens(self, names) -> None:
        for name in names:
            lowered = name.lower()
            for token in self.AUTHORITY_TOKENS:
                self.assertNotIn(
                    token,
                    lowered,
                    msg=f"display field {name!r} carries authority token {token!r}",
                )

    def test_dataclass_fields_carry_no_routing_or_authority_token(self) -> None:
        for cls in (
            UnitDisplayRow,
            GroupDisplaySection,
            GroupedDisplayView,
            GroupAttentionSummary,
        ):
            self._assert_no_boundary_tokens(cls.__dataclass_fields__.keys())

    def test_payload_keys_carry_no_routing_or_authority_token(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    roles=("codex", "claude"), observation=_fresh_observation(),
                )
            ],
            config=_config(),
            observation=_fresh_observation(),
        )

        def walk(node) -> None:
            if isinstance(node, dict):
                self._assert_no_boundary_tokens(node.keys())
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(view.as_payload())

    def test_payload_carries_projection_only_boundary_note(self) -> None:
        view = _view_from([])
        self.assertEqual(
            view.as_payload()["boundary_note"],
            GROUPED_DISPLAY_DIAGNOSTIC_ONLY_NOTE,
        )

    def test_roles_value_carries_only_role_names_not_panes(self) -> None:
        # Even if an observation smuggled a pane-shaped token, roles are names; the
        # display carries no pane / target field to put it in.
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    roles=("codex", "claude"), observation=_fresh_observation(),
                )
            ],
            observation=_fresh_observation(),
        )
        unit_payload = view.as_payload()["groups"][0]["units"][0]
        self.assertEqual(unit_payload["roles"], ["codex", "claude"])
        self.assertNotIn("pane", unit_payload)
        self.assertNotIn("target", unit_payload)


class DisplayViewPlacementAndIdentityTest(unittest.TestCase):
    """#12286: the display view carries the placement mode + Unit identity facts."""

    def _view(self, config, observed):
        model = build_grouped_read_model(
            config, observed, observation=_fresh_observation()
        )
        return build_grouped_display_view(model)

    def test_placement_mode_flows_to_display_payload(self) -> None:
        config = PresentationGroupingConfig.from_record(
            {"project_group_presentation": "project_group_tmux_window"}
        )
        view = self._view(config, [])
        self.assertEqual(
            view.project_group_presentation, "project_group_tmux_window"
        )
        self.assertEqual(
            view.as_payload()["project_group_presentation"],
            "project_group_tmux_window",
        )

    def test_default_placement_is_same_cockpit_column(self) -> None:
        view = self._view(None, [])
        self.assertEqual(
            view.project_group_presentation, "same_cockpit_column"
        )

    def test_unit_row_carries_identity_for_action_wiring(self) -> None:
        observed = [
            ObservedUnit(
                workspace_id="ws-a",
                lane_id="default",
                host_id="local",
                repo_label="Alpha",
                active=True,
                roles=("codex", "claude"),
                observation=_fresh_observation(),
            )
        ]
        view = self._view(None, observed)
        row = view.all_units()[0]
        self.assertEqual(row.workspace_id, "ws-a")
        self.assertEqual(row.lane_id, "default")
        self.assertEqual(row.host_id, "local")
        payload = row.as_payload()
        self.assertEqual(payload["workspace_id"], "ws-a")
        self.assertEqual(payload["lane_id"], "default")
        self.assertEqual(payload["host_id"], "local")
        # Identity only — still no routing endpoint on the row.
        for forbidden in ("pane", "target", "route", "send", "approval"):
            self.assertNotIn(forbidden, payload)


class GroupHeaderSummaryTests(unittest.TestCase):
    """Redmine #12297: the Project Group header attention / freshness summary."""

    def _mixed_alpha_view(self) -> GroupedDisplayView:
        # Four Units in the declared "alpha" group spanning the summary axes:
        #   a1 fresh+active            -> observed, active, not reload
        #   a2 stale+active            -> reload_required (not an attention candidate)
        #   a3 contradicted+inactive   -> attention candidate + reload_required
        #   a4 identity-conflict+active-> attention candidate + reload_required + active
        return _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a1", repo_label="alpha", active=True,
                    roles=("codex", "claude"), observation=_fresh_observation(),
                ),
                ObservedUnit(
                    workspace_id="ws-a2", repo_label="alpha", active=True,
                    roles=("codex",), observation=_stale_observation(),
                ),
                ObservedUnit(
                    workspace_id="ws-a3", repo_label="alpha", active=False,
                    observation=_contradicted_observation(),
                ),
                ObservedUnit(
                    workspace_id="ws-a4", repo_label="alpha", active=True,
                    observed_workspace_id="ws-other",
                    observation=_fresh_observation(),
                ),
            ],
            config=_config(),
            observation=_fresh_observation(),
        )

    def test_header_summary_counts_active_reload_and_attention(self) -> None:
        view = self._mixed_alpha_view()
        alpha = next(g for g in view.groups if g.group_id == "project:alpha")
        summary = alpha.summary
        self.assertEqual(summary.total, 4)
        self.assertEqual(summary.active_lanes, 3)  # a1, a2, a4
        self.assertEqual(summary.reload_required, 3)  # a2, a3, a4
        self.assertEqual(summary.attention, 2)  # a3 contradicted, a4 identity conflict
        self.assertTrue(summary.needs_attention)

    def test_attention_candidate_statuses_are_the_contradiction_class(self) -> None:
        # The attention count is the contradiction-class subset only, never a mere
        # staleness; it is exactly the documented status set.
        self.assertEqual(
            ATTENTION_CANDIDATE_STATUSES,
            frozenset({"contradicted", "identity_conflict", "desired_unit_missing"}),
        )

    def test_desired_unit_missing_counts_as_attention_candidate(self) -> None:
        record = {
            "version": 1,
            "project_groups": [
                {"group_id": "project:alpha", "label": "Alpha", "sort_key": 10},
            ],
            "grouping": {
                "membership_rules": [
                    {"when": {"repo_label": "alpha"}, "group_id": "project:alpha"},
                ],
                "unit_overrides": [
                    {
                        "workspace_id": "ws-ghost",
                        "lane_id": "default",
                        "preferred_group": "project:alpha",
                    }
                ],
            },
        }
        config = PresentationGroupingConfig.from_record(record)
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    roles=("codex",), observation=_fresh_observation(),
                )
            ],
            config=config,
            observation=_fresh_observation(),
        )
        alpha = next(g for g in view.groups if g.group_id == "project:alpha")
        # The override names a Unit not in the observed set -> a desired_unit_missing
        # row that counts as an attention candidate (and reload_required).
        self.assertEqual(alpha.summary.attention, 1)
        self.assertEqual(alpha.summary.reload_required, 1)
        self.assertEqual(alpha.summary.active_lanes, 1)

    def test_summary_counts_hidden_members_too(self) -> None:
        record = {
            "version": 1,
            "project_groups": [
                {"group_id": "project:alpha", "label": "Alpha", "sort_key": 10},
            ],
            "grouping": {
                "membership_rules": [
                    {"when": {"repo_label": "alpha"}, "group_id": "project:alpha"},
                ],
                "unit_overrides": [
                    {
                        "workspace_id": "ws-hidden",
                        "lane_id": "default",
                        "preferred_group": "project:alpha",
                        "hidden": True,
                    }
                ],
            },
        }
        config = PresentationGroupingConfig.from_record(record)
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    roles=("codex",), observation=_fresh_observation(),
                ),
                ObservedUnit(
                    workspace_id="ws-hidden", repo_label="alpha", active=True,
                    roles=("claude",), observation=_fresh_observation(),
                ),
            ],
            config=config,
            observation=_fresh_observation(),
        )
        alpha = next(g for g in view.groups if g.group_id == "project:alpha")
        # One visible + one hidden, both active: the hidden member is counted.
        self.assertEqual(alpha.summary.total, 2)
        self.assertEqual(alpha.summary.active_lanes, 2)

    def test_empty_declared_group_summary_is_all_zero(self) -> None:
        view = self._mixed_alpha_view()
        bravo = next(g for g in view.groups if g.group_id == "project:bravo")
        self.assertEqual(bravo.summary, GroupAttentionSummary())
        self.assertFalse(bravo.summary.needs_attention)

    def test_whole_view_summary_aggregates_every_member(self) -> None:
        view = self._mixed_alpha_view()
        # Only the alpha group has members, so the roll-up equals its summary.
        self.assertEqual(view.summary.total, 4)
        self.assertEqual(view.summary.active_lanes, 3)
        self.assertEqual(view.summary.reload_required, 3)
        self.assertEqual(view.summary.attention, 2)
        self.assertTrue(view.summary.needs_attention)

    def test_healthy_view_summary_has_no_attention(self) -> None:
        view = _view_from(
            [
                ObservedUnit(
                    workspace_id="ws-a", repo_label="alpha", active=True,
                    roles=("codex", "claude"), observation=_fresh_observation(),
                )
            ],
            config=_config(),
            observation=_fresh_observation(),
        )
        self.assertEqual(view.summary.active_lanes, 1)
        self.assertEqual(view.summary.reload_required, 0)
        self.assertEqual(view.summary.attention, 0)
        self.assertFalse(view.summary.needs_attention)

    def test_summary_payload_keys_are_projection_only(self) -> None:
        view = self._mixed_alpha_view()
        alpha = next(
            g for g in view.as_payload()["groups"]
            if g["group_id"] == "project:alpha"
        )
        self.assertEqual(
            set(alpha["summary"].keys()),
            {"total", "active_lanes", "reload_required", "attention", "needs_attention"},
        )
        self.assertEqual(
            set(view.as_payload()["summary"].keys()),
            {"total", "active_lanes", "reload_required", "attention", "needs_attention"},
        )

    def test_summary_carries_no_governance_or_routing_vocabulary(self) -> None:
        # The summary is a projection: its field names name counts, never a
        # Redmine journal / owner-approval / review / blocked / routing concept.
        forbidden = (
            "journal", "owner", "approval", "review", "blocked", "close",
            "target", "pane", "route", "send", "credential", "secret",
        )
        for name in GroupAttentionSummary.__dataclass_fields__:
            lowered = name.lower()
            for token in forbidden:
                self.assertNotIn(token, lowered)
        # The boundary note pins the projection-only contract.
        self.assertIn("projection", GROUPED_SUMMARY_DIAGNOSTIC_ONLY_NOTE.lower())
        self.assertIn("governance", GROUPED_SUMMARY_DIAGNOSTIC_ONLY_NOTE.lower())


if __name__ == "__main__":
    unittest.main()
