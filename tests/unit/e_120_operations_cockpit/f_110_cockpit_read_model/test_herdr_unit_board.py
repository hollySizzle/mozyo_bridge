from __future__ import annotations

import unittest

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AUTHORITY_MISSING,
    AUTHORITY_RESOLVED,
    AgentObservation,
    build_unit_board,
    clip_display,
    format_board,
    lane_work_label,
    metadata_for_unit,
    safe_text,
)


def observation(provider: str, pane: str, **overrides) -> AgentObservation:
    values = {
        "workspace_id": "workspace-a",
        "lane_id": "default",
        "provider": provider,
        "pane_id": pane,
        "runtime_state": "idle",
        "interactive_ready": True,
        "project_label": "giken-3800-mozyo-bridge",
        "workflow_role": "coordinator",
        "responsibility": "giken-3800-mozyo-bridge",
        "work_label": "default lane",
        "authority_state": AUTHORITY_RESOLVED,
    }
    values.update(overrides)
    return AgentObservation(**values)


class UnitBoardReadModelTests(unittest.TestCase):
    def test_groups_pair_and_never_exposes_transient_pane_ids(self) -> None:
        snapshot = build_unit_board(
            (observation("codex", "w1:p1"), observation("claude", "w1:p2")),
            observed_at="2026-08-08T00:00:00+00:00",
        )

        self.assertTrue(snapshot.ok)
        self.assertEqual(len(snapshot.units), 1)
        unit = snapshot.units[0]
        self.assertEqual(unit.identity_state, "resolved")
        self.assertEqual([a.provider for a in unit.agents], ["claude", "codex"])
        rendered = repr(snapshot.as_payload())
        self.assertNotIn("w1:p1", rendered)
        self.assertNotIn("w1:p2", rendered)

    def test_duplicate_provider_and_conflicting_labels_are_ambiguous(self) -> None:
        snapshot = build_unit_board(
            (
                observation("codex", "w1:p1"),
                observation(
                    "codex",
                    "w1:p2",
                    project_label="another-project",
                    authority_state=AUTHORITY_MISSING,
                ),
            ),
            observed_at="now",
        )

        unit = snapshot.units[0]
        self.assertEqual(unit.identity_state, "ambiguous")
        self.assertEqual(unit.project_label, "ambiguous")
        self.assertEqual(unit.authority_state, "ambiguous")

    def test_issue_lane_label_keeps_readable_words_beside_id(self) -> None:
        self.assertEqual(
            lane_work_label("issue_15114_herdr_unit_board"),
            "#15114 herdr unit board",
        )
        self.assertEqual(lane_work_label("default"), "default lane")

    def test_display_values_strip_controls_and_obey_metadata_cap(self) -> None:
        value = safe_text("  project\nname\x00  " + "x" * 100)
        self.assertNotIn("\n", value)
        self.assertNotIn("\x00", value)
        self.assertLessEqual(len(value), 80)

        unit = build_unit_board(
            (observation("codex", "w1:p1", work_label="x" * 200),),
            observed_at="now",
        ).units[0]
        tokens, title = metadata_for_unit(unit)
        self.assertTrue(all(len(item) <= 80 for item in tokens.values()))
        self.assertLessEqual(len(title), 80)

    def test_narrow_text_render_clips_wide_characters_without_control_data(self) -> None:
        snapshot = build_unit_board(
            (
                observation(
                    "codex",
                    "w1:p1",
                    project_label="情報運用部のとても長い担当名",
                    work_label="長い作業名" * 20,
                ),
            ),
            observed_at="now",
        )
        rendered = format_board(snapshot, width=60)
        self.assertIn("mozyo Unit board", rendered)
        self.assertIn("responsibility:", rendered)
        self.assertIn("…", rendered)
        self.assertNotIn("w1:p1", rendered)
        self.assertLessEqual(len(clip_display("情報運用部", 6)), 4)


if __name__ == "__main__":
    unittest.main()
