from __future__ import annotations

import unicodedata
import unittest

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AUTHORITY_MISSING,
    AUTHORITY_RESOLVED,
    REDACTED_TEXT,
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


def terminal_width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in value
    )


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

    def test_projection_redacts_paths_and_credentials_and_neutralizes_controls(self) -> None:
        private_path = "/" + "/".join(("synthetic", "private", "project"))
        credential_key = "_".join(("API", "TOKEN"))
        credential_shape = "=".join((credential_key, "synthetic-value"))
        controlled = "safe\u009b\u202etext"
        unit = build_unit_board(
            (
                observation(
                    "codex",
                    "w1:p1",
                    project_label=private_path,
                    responsibility=credential_shape,
                    work_label=controlled,
                ),
            ),
            observed_at="now",
        ).units[0]

        payload = repr(unit.as_payload())
        tokens, title = metadata_for_unit(unit)
        metadata = repr((tokens, title))
        self.assertEqual(unit.project_label, REDACTED_TEXT)
        self.assertEqual(unit.responsibility, REDACTED_TEXT)
        self.assertNotIn(private_path, payload)
        self.assertNotIn(credential_shape, payload)
        self.assertNotIn(private_path, metadata)
        self.assertNotIn(credential_shape, metadata)
        for control in ("\u009b", "\u202e"):
            self.assertNotIn(control, payload)
            self.assertNotIn(control, metadata)

    def test_public_safe_text_rejects_cross_platform_path_and_opaque_credential_shapes(self) -> None:
        windows_path = "".join(("C", ":", "\\", "synthetic", "\\", "project"))
        unc_path = "".join(("\\", "\\", "synthetic", "\\", "project"))
        opaque_prefix = "".join(("g", "h", "p", "_"))
        opaque_credential = opaque_prefix + ("x" * 24)
        controlled_credential = "".join(
            ("AUTH_", "\u202e", "TOKEN", "=", "synthetic-value")
        )

        for unsafe in (
            "/" + "/".join(("synthetic", "project")),
            "~/synthetic/project",
            windows_path,
            unc_path,
            opaque_credential,
            controlled_credential,
            "label(config:/synthetic/project)",
        ):
            with self.subTest(shape=unsafe[:2]):
                self.assertEqual(safe_text(unsafe), REDACTED_TEXT)
        self.assertEqual(
            safe_text("https://example.invalid/project"),
            REDACTED_TEXT,
        )

    def test_long_distinct_lane_identities_keep_distinct_unit_and_metadata_ids(self) -> None:
        common = "lane-" + ("x" * 90)
        snapshot = build_unit_board(
            (
                observation("codex", "w1:p1", lane_id=common + "a"),
                observation("codex", "w1:p2", lane_id=common + "b"),
            ),
            observed_at="now",
        )

        self.assertEqual(len(snapshot.units), 2)
        self.assertEqual(len({unit.unit_id for unit in snapshot.units}), 2)
        metadata_ids = {
            metadata_for_unit(unit)[0]["mozyo_unit"] for unit in snapshot.units
        }
        self.assertEqual(len(metadata_ids), 2)
        self.assertTrue(all(len(value) <= 80 for value in metadata_ids))

        delimiter_snapshot = build_unit_board(
            (
                observation(
                    "codex", "w1:p3", workspace_id="a", lane_id="b\x00c"
                ),
                observation(
                    "codex", "w1:p4", workspace_id="a\x00b", lane_id="c"
                ),
            ),
            observed_at="now",
        )
        self.assertEqual(
            len({unit.unit_id for unit in delimiter_snapshot.units}),
            2,
        )

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

    def test_render_never_exceeds_requested_positive_terminal_width(self) -> None:
        snapshot = build_unit_board(
            (
                observation(
                    "codex",
                    "w1:p1",
                    project_label="情報運用部の長い担当名",
                    work_label="長い作業名" * 20,
                ),
            ),
            observed_at="now",
        )

        for width in (1, 10, 20, 40):
            with self.subTest(width=width):
                rendered = format_board(snapshot, width=width)
                self.assertTrue(rendered)
                self.assertTrue(
                    all(terminal_width(line) <= width for line in rendered.splitlines())
                )


if __name__ == "__main__":
    unittest.main()
