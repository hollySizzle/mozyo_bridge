"""`layout preset` CLI integration tests (Redmine #15708).

Hermetic integration over real collaborators: the CLI handlers wired to the real
YAML config-document adapter on a real temp-dir filesystem, the real repo-local
config loader, and the full built parser (registration wiring). Pins the
display-only regression of acceptance 2 — ``apply --write`` changes exactly the
``lane_placement`` key of ``.mozyo-bridge/config.yaml`` while every other
top-level block survives — the dry-run-first / atomic-write / ``.bak`` behavior,
the fail-closed CLI paths, and (j#108183 finding_liveeffect) that every human
success rendering, including the already-matching no-op, prints the typed
live-effect boundary.

The pure preset vocabulary / expansion / classification and the apply service
against a fake port live in ``tests/unit/e_130_governance_distribution/
f_140_rules_docs_catalog/test_layout_preset.py``.
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

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.layout_preset import (  # noqa: E501
    HERDR_API_GAPS,
    LIVE_EFFECT_MATRIX,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.cli_layout_preset import (  # noqa: E501
    cmd_layout_preset_apply,
    cmd_layout_preset_list,
    cmd_layout_preset_status,
)


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

    def test_human_outputs_always_carry_the_live_effect_boundary(self) -> None:
        # j#108183 finding_liveeffect: EVERY human success rendering — the
        # already-matching no-op included — states the typed fresh/live boundary.
        self._write_config(
            {
                "version": 2,
                "lane_placement": {
                    "default": {"split": "down"},
                    "sublane": {"split": "down"},
                },
            }
        )
        scenarios = [
            ("already_matching", "stacked", True),
            ("dry_run", "side-by-side", False),
            ("written", "side-by-side", True),
        ]
        for label, preset_name, write in scenarios:
            with self.subTest(label):
                code, out, _ = _run(
                    cmd_layout_preset_apply,
                    _args(
                        json=False,
                        repo=str(self.repo),
                        preset_name=preset_name,
                        ratio=None,
                        write=write,
                    ),
                )
                self.assertEqual(code, 0)
                self.assertIn("live effect (typed):", out)
                for population, token in LIVE_EFFECT_MATRIX.items():
                    self.assertIn(f"{population}: {token}", out)
                for gap in HERDR_API_GAPS:
                    self.assertIn(gap, out)

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
