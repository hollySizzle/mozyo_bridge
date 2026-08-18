"""Layout preset vocabulary + CLI tests (Redmine #15708).

Pins the declarative pair-geometry preset surface: the closed ``stacked`` /
``side-by-side`` vocabulary and its pure expansion into a ``lane_placement``
declaration (preserving ``order`` / caller-absent ``ratio`` / ``by_lane_kind``
verbatim), the fail-closed paths (unknown preset, out-of-domain ratio, malformed
block), the effective-preset classification the status surface reads, and the
display-only regression pins of acceptance 2: ``layout preset apply`` rewrites
EXACTLY the ``lane_placement`` key of the config record — every other top-level
block survives byte-identically — and never touches a live pane (the CLI has no
pane / tmux / herdr client to reach one; the typed ``live_effect`` matrix states
the boundary instead).
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LanePlacementConfig,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.layout_preset import (  # noqa: E501
    HERDR_API_GAPS,
    LAYOUT_PRESETS,
    LAYOUT_PRESET_CUSTOM,
    LIVE_EFFECT_MATRIX,
    LayoutPresetError,
    apply_preset_to_lane_placement,
    classify_effective_preset,
    normalize_preset_name,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.cli_layout_preset import (  # noqa: E501
    cmd_layout_preset_apply,
    cmd_layout_preset_list,
    cmd_layout_preset_status,
)


class PresetVocabularyTest(unittest.TestCase):
    def test_closed_vocabulary_maps_to_herdr_split_directions(self) -> None:
        self.assertEqual(dict(LAYOUT_PRESETS), {"stacked": "down", "side-by-side": "right"})

    def test_unknown_preset_fails_closed(self) -> None:
        for bogus in ("vertical", "", None, 3, "STACKED"):
            with self.assertRaises(LayoutPresetError):
                normalize_preset_name(bogus)

    def test_live_effect_matrix_is_typed_and_names_the_herdr_gap(self) -> None:
        # Acceptance 3: the boundary is stated typed, not simulated.
        self.assertEqual(
            dict(LIVE_EFFECT_MATRIX),
            {
                "fresh_units": "applied_at_fresh_launch",
                "existing_dedicated_pair": "requires_explicit_pair_placement_apply",
                "existing_shared_tab_columns": "unsupported_same_tab_re_split",
            },
        )
        self.assertEqual(HERDR_API_GAPS, ("same_tab_re_split_api_absent",))


class PresetExpansionTest(unittest.TestCase):
    def test_expands_over_absent_block(self) -> None:
        app = apply_preset_to_lane_placement(None, preset="side-by-side")
        self.assertEqual(
            dict(app.lane_placement_record),
            {"default": {"split": "right"}, "sublane": {"split": "right"}},
        )
        self.assertFalse(app.already_matching)
        self.assertEqual(app.shadowed_lane_kinds, ())
        self.assertEqual(len(app.changes), 2)

    def test_preserves_declared_order_and_ratio_when_caller_passes_none(self) -> None:
        existing = {
            "default": {"split": "down", "order": ["claude", "codex"], "ratio": 0.7},
            "sublane": {"ratio": 0.3},
        }
        app = apply_preset_to_lane_placement(existing, preset="side-by-side")
        self.assertEqual(
            dict(app.lane_placement_record),
            {
                "default": {"split": "right", "order": ["claude", "codex"], "ratio": 0.7},
                "sublane": {"split": "right", "ratio": 0.3},
            },
        )

    def test_caller_ratio_overrides_both_classes(self) -> None:
        app = apply_preset_to_lane_placement(
            {"default": {"ratio": 0.7}}, preset="stacked", ratio=0.6
        )
        self.assertEqual(
            dict(app.lane_placement_record),
            {
                "default": {"ratio": 0.6, "split": "down"},
                "sublane": {"split": "down", "ratio": 0.6},
            },
        )

    def test_by_lane_kind_preserved_verbatim_and_reported_as_shadowing(self) -> None:
        existing = {
            "by_lane_kind": {"coordinator": {"order": ["codex", "claude"]}},
            "default": {"split": "down"},
        }
        app = apply_preset_to_lane_placement(existing, preset="side-by-side")
        self.assertEqual(
            app.lane_placement_record["by_lane_kind"],
            {"coordinator": {"order": ["codex", "claude"]}},
        )
        # An order-only kind still shadows wholesale (#13647), so it is reported.
        self.assertEqual(app.shadowed_lane_kinds, ("coordinator",))

    def test_version_key_passes_through(self) -> None:
        app = apply_preset_to_lane_placement({"version": 1}, preset="stacked")
        self.assertEqual(app.lane_placement_record["version"], 1)

    def test_already_matching_is_a_semantic_comparison(self) -> None:
        existing = {"default": {"split": "down"}, "sublane": {"split": "down"}}
        app = apply_preset_to_lane_placement(existing, preset="stacked")
        self.assertTrue(app.already_matching)
        self.assertEqual(app.changes, ())

    def test_declaring_over_undeclared_is_a_change_even_when_geometry_matches(self) -> None:
        # #14568 keeps "declared" and "product default" distinguishable; declaring the
        # default geometry is still a declaration change.
        app = apply_preset_to_lane_placement(None, preset="stacked")
        self.assertFalse(app.already_matching)

    def test_out_of_domain_ratio_fails_closed(self) -> None:
        for ratio in (0.05, 0.95, float("nan"), float("inf")):
            with self.assertRaises(LayoutPresetError):
                apply_preset_to_lane_placement(None, preset="stacked", ratio=ratio)

    def test_malformed_block_fails_closed(self) -> None:
        with self.assertRaises(LayoutPresetError):
            apply_preset_to_lane_placement("down", preset="stacked")
        with self.assertRaises(LayoutPresetError):
            apply_preset_to_lane_placement({"default": "down"}, preset="stacked")
        with self.assertRaises(LayoutPresetError):
            apply_preset_to_lane_placement(
                {"by_lane_kind": {"parent": {"split": "down"}}}, preset="stacked"
            )

    def test_expansion_writes_only_geometry_vocabulary(self) -> None:
        # Acceptance 2: the produced block re-parses under the closed lane_placement
        # schema, which can carry split / order / ratio only — no authority-shaped key
        # can survive the final re-parse.
        app = apply_preset_to_lane_placement(
            {"default": {"split": "down", "ratio": 0.7}}, preset="side-by-side", ratio=0.4
        )
        LanePlacementConfig.from_record(dict(app.lane_placement_record))
        for entry in ("default", "sublane"):
            self.assertLessEqual(
                set(app.lane_placement_record[entry]), {"split", "order", "ratio"}
            )


class EffectivePresetClassificationTest(unittest.TestCase):
    def test_undeclared_config_matches_stacked_product_default(self) -> None:
        self.assertEqual(
            classify_effective_preset(LanePlacementConfig.default()), "stacked"
        )

    def test_declared_side_by_side_matches(self) -> None:
        config = LanePlacementConfig.from_record(
            {"default": {"split": "right"}, "sublane": {"split": "right"}}
        )
        self.assertEqual(classify_effective_preset(config), "side-by-side")

    def test_mixed_geometry_is_custom(self) -> None:
        config = LanePlacementConfig.from_record({"default": {"split": "right"}})
        self.assertEqual(classify_effective_preset(config), LAYOUT_PRESET_CUSTOM)


def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _run(func, args) -> "tuple[int, str, str]":
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = func(args)
    return code, out.getvalue(), err.getvalue()


#: A representative config carrying every kind of unrelated top-level block the apply
#: rewrite must leave untouched (acceptance 2).
_UNRELATED_RICH_CONFIG: "dict[str, object]" = {
    "version": 2,
    "work_unit": {"granularity": "user_story"},
    "sublane_integration": {"integration_branch": "main"},
    "agents": {
        "profiles": {
            "implementation": {"provider": "claude"},
            "coordination": {"provider": "codex"},
        },
        "roles": {"coordinator": "implementation"},
    },
    "presentation": {
        "project_groups": [{"group_id": "project:demo", "label": "demo"}],
    },
    "terminal_transport": {"backend": "herdr"},
}


class CliLayoutPresetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / ".mozyo-bridge").mkdir()
        self.config_path = self.repo / ".mozyo-bridge" / "config.yaml"

    def _write_config(self, record: "dict[str, object]") -> None:
        self.config_path.write_text(
            yaml.safe_dump(record, sort_keys=False), encoding="utf-8"
        )

    def test_list_json_carries_vocabulary_and_live_effect(self) -> None:
        code, out, _ = _run(cmd_layout_preset_list, _args(json=True))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            {entry["preset"] for entry in payload["presets"]},
            {"stacked", "side-by-side"},
        )
        self.assertEqual(payload["live_effect"], dict(LIVE_EFFECT_MATRIX))
        self.assertEqual(payload["herdr_api_gaps"], list(HERDR_API_GAPS))

    def test_status_reports_matched_preset_and_axes(self) -> None:
        self._write_config(
            {
                "version": 2,
                "lane_placement": {
                    "default": {"split": "right"},
                    "sublane": {"split": "right"},
                    "by_lane_kind": {"implementation": {"ratio": 0.7}},
                },
            }
        )
        code, out, _ = _run(
            cmd_layout_preset_status, _args(json=True, repo=str(self.repo))
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["matched_preset"], "side-by-side")
        by_class = {entry["lane_class"]: entry for entry in payload["lane_classes"]}
        self.assertEqual(by_class["default"]["effective"]["split"], "right")
        self.assertEqual(by_class["default"]["declared"]["split"], "right")
        self.assertEqual(
            payload["by_lane_kind"],
            [{"lane_kind": "implementation", "split": None, "order": None, "ratio": 0.7}],
        )

    def test_status_on_unreadable_config_fails_closed(self) -> None:
        self.config_path.write_text("terminal_transport: [broken", encoding="utf-8")
        code, _, err = _run(
            cmd_layout_preset_status, _args(json=False, repo=str(self.repo))
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot read config", err)

    def test_apply_dry_run_by_default_writes_nothing(self) -> None:
        self._write_config(dict(_UNRELATED_RICH_CONFIG))
        before = self.config_path.read_bytes()
        code, out, _ = _run(
            cmd_layout_preset_apply,
            _args(
                json=True,
                repo=str(self.repo),
                preset_name="side-by-side",
                ratio=None,
                write=False,
            ),
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["written"])
        self.assertIn("document", payload)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.config_path.with_name("config.yaml.bak").exists())

    def test_apply_write_touches_only_lane_placement(self) -> None:
        self._write_config(dict(_UNRELATED_RICH_CONFIG))
        before_record = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        code, out, _ = _run(
            cmd_layout_preset_apply,
            _args(
                json=True,
                repo=str(self.repo),
                preset_name="side-by-side",
                ratio=0.6,
                write=True,
            ),
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["written"])
        after_record = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        # Acceptance 2 pin: exactly the lane_placement key changed; every other
        # top-level block (agents / roles / presentation / transport / work unit)
        # survives structurally identical.
        self.assertEqual(
            after_record.pop("lane_placement"),
            {
                "default": {"split": "right", "ratio": 0.6},
                "sublane": {"split": "right", "ratio": 0.6},
            },
        )
        self.assertEqual(after_record, before_record)
        # The atomic write left a backup of the original bytes.
        backup = self.config_path.with_name("config.yaml.bak")
        self.assertTrue(backup.exists())
        self.assertEqual(yaml.safe_load(backup.read_text(encoding="utf-8")), before_record)

    def test_apply_write_then_status_round_trips(self) -> None:
        self._write_config(dict(_UNRELATED_RICH_CONFIG))
        code, _, _ = _run(
            cmd_layout_preset_apply,
            _args(
                json=True,
                repo=str(self.repo),
                preset_name="side-by-side",
                ratio=None,
                write=True,
            ),
        )
        self.assertEqual(code, 0)
        code, out, _ = _run(
            cmd_layout_preset_status, _args(json=True, repo=str(self.repo))
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["matched_preset"], "side-by-side")

    def test_apply_already_matching_is_a_no_op(self) -> None:
        self._write_config(
            {
                "version": 2,
                "lane_placement": {
                    "default": {"split": "down"},
                    "sublane": {"split": "down"},
                },
            }
        )
        before = self.config_path.read_bytes()
        code, out, _ = _run(
            cmd_layout_preset_apply,
            _args(
                json=True,
                repo=str(self.repo),
                preset_name="stacked",
                ratio=None,
                write=True,
            ),
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["already_matching"])
        self.assertFalse(payload["written"])
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_apply_missing_config_file_creates_minimal_declaration(self) -> None:
        code, out, _ = _run(
            cmd_layout_preset_apply,
            _args(
                json=True,
                repo=str(self.repo),
                preset_name="side-by-side",
                ratio=None,
                write=True,
            ),
        )
        self.assertEqual(code, 0)
        record = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            record,
            {
                "lane_placement": {
                    "default": {"split": "right"},
                    "sublane": {"split": "right"},
                }
            },
        )

    def test_apply_out_of_domain_ratio_fails_closed_without_writing(self) -> None:
        self._write_config(dict(_UNRELATED_RICH_CONFIG))
        before = self.config_path.read_bytes()
        code, out, _ = _run(
            cmd_layout_preset_apply,
            _args(
                json=True,
                repo=str(self.repo),
                preset_name="stacked",
                ratio=0.95,
                write=True,
            ),
        )
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_apply_reports_by_lane_kind_shadowing(self) -> None:
        self._write_config(
            {
                "version": 2,
                "lane_placement": {
                    "by_lane_kind": {"coordinator": {"split": "down"}},
                },
            }
        )
        code, out, _ = _run(
            cmd_layout_preset_apply,
            _args(
                json=True,
                repo=str(self.repo),
                preset_name="side-by-side",
                ratio=None,
                write=True,
            ),
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["shadowed_by_lane_kind"], ["coordinator"])
        record = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        # The shadowing declaration is preserved verbatim, never rewritten.
        self.assertEqual(
            record["lane_placement"]["by_lane_kind"],
            {"coordinator": {"split": "down"}},
        )

    def test_apply_non_mapping_config_fails_closed(self) -> None:
        self.config_path.write_text("- a\n- b\n", encoding="utf-8")
        code, _, err = _run(
            cmd_layout_preset_apply,
            _args(
                json=False,
                repo=str(self.repo),
                preset_name="stacked",
                ratio=None,
                write=True,
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("not a YAML mapping", err)


class CliParserWiringTest(unittest.TestCase):
    def test_layout_preset_is_registered_under_layout(self) -> None:
        from mozyo_bridge.application.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["layout", "preset", "list"])
        self.assertIs(args.func, cmd_layout_preset_list)
        args = parser.parse_args(
            ["layout", "preset", "apply", "side-by-side", "--ratio", "0.6"]
        )
        self.assertIs(args.func, cmd_layout_preset_apply)
        self.assertEqual(args.preset_name, "side-by-side")
        self.assertEqual(args.ratio, 0.6)
        self.assertFalse(args.write)
        args = parser.parse_args(["layout", "preset", "status"])
        self.assertIs(args.func, cmd_layout_preset_status)

    def test_unknown_preset_rejected_at_parse_time(self) -> None:
        from mozyo_bridge.application.cli import build_parser

        parser = build_parser()
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parser.parse_args(["layout", "preset", "apply", "vertical"])


if __name__ == "__main__":
    unittest.main()
