"""Layout preset domain + apply-service unit tests (Redmine #15708).

Pure isolation tests: the closed ``stacked`` / ``side-by-side`` vocabulary and its
expansion into a ``lane_placement`` declaration (preserving ``order`` / caller-absent
``ratio`` / ``by_lane_kind`` verbatim), the fail-closed paths (unknown preset,
out-of-domain ratio, malformed block), the effective-preset classification, and the
:class:`LayoutPresetApplyService` state transitions against a fake config-document
port (j#108183 finding_oopboundary: the flow is specified via a port fake, not
monkeypatched IO — no real filesystem is touched anywhere in this module).

The CLI handlers, real-filesystem adapter, and parser wiring are exercised in
``tests/integration/e_130_governance_distribution/f_140_rules_docs_catalog/
test_cli_layout_preset.py``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[4]
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
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.layout_preset_apply import (  # noqa: E501
    LayoutPresetApplyInput,
    LayoutPresetApplyService,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfigError,
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


class FakeConfigDocument:
    """In-memory :class:`ConfigDocumentPort` fake expressing the document lifecycle.

    The fake specifies the port contract as state transitions (loaded record →
    rendered text → replaced document), with injectable failures, so the service's
    flow is characterized without any real filesystem.
    """

    def __init__(
        self,
        record: object = None,
        *,
        load_error: "Optional[RepoLocalConfigError]" = None,
        write_error: "Optional[OSError]" = None,
    ) -> None:
        self._record = record
        self._load_error = load_error
        self._write_error = write_error
        self.written_text: "Optional[str]" = None
        self.rendered: "list[dict[str, object]]" = []
        self.backup_path = Path("/fake/.mozyo-bridge/config.yaml.bak")

    @property
    def path(self) -> Path:
        return Path("/fake/.mozyo-bridge/config.yaml")

    def load_raw(self) -> object:
        if self._load_error is not None:
            raise self._load_error
        return self._record

    def render(self, record: "dict[str, object]") -> str:
        self.rendered.append(record)
        return f"<document {len(self.rendered)}>"

    def replace_atomic(self, text: str) -> "Optional[Path]":
        if self._write_error is not None:
            raise self._write_error
        self.written_text = text
        return self.backup_path


class LayoutPresetApplyServiceTest(unittest.TestCase):
    def test_preview_is_the_default_and_writes_nothing(self) -> None:
        port = FakeConfigDocument({"version": 2})
        result = LayoutPresetApplyService(port).apply(
            LayoutPresetApplyInput(preset_name="side-by-side")
        )
        self.assertEqual(result.status, "previewed")
        self.assertTrue(result.ok)
        self.assertEqual(result.document, "<document 1>")
        self.assertIsNone(port.written_text)

    def test_write_renders_once_and_replaces_atomically(self) -> None:
        port = FakeConfigDocument({"version": 2})
        result = LayoutPresetApplyService(port).apply(
            LayoutPresetApplyInput(preset_name="side-by-side", ratio=0.6, write=True)
        )
        self.assertEqual(result.status, "written")
        self.assertEqual(port.written_text, "<document 1>")
        self.assertEqual(result.backup, port.backup_path)
        # The produced record touches exactly the lane_placement key (acceptance 2).
        (rendered,) = port.rendered
        self.assertEqual(
            rendered,
            {
                "version": 2,
                "lane_placement": {
                    "default": {"split": "right", "ratio": 0.6},
                    "sublane": {"split": "right", "ratio": 0.6},
                },
            },
        )

    def test_unrelated_blocks_pass_through_untouched(self) -> None:
        record = {
            "version": 2,
            "work_unit": {"granularity": "user_story"},
            "terminal_transport": {"backend": "herdr"},
        }
        port = FakeConfigDocument(dict(record))
        LayoutPresetApplyService(port).apply(
            LayoutPresetApplyInput(preset_name="stacked", write=True)
        )
        (rendered,) = port.rendered
        rendered = dict(rendered)
        rendered.pop("lane_placement")
        self.assertEqual(rendered, record)

    def test_already_matching_never_renders_or_writes(self) -> None:
        port = FakeConfigDocument(
            {
                "lane_placement": {
                    "default": {"split": "down"},
                    "sublane": {"split": "down"},
                }
            }
        )
        result = LayoutPresetApplyService(port).apply(
            LayoutPresetApplyInput(preset_name="stacked", write=True)
        )
        self.assertEqual(result.status, "already_matching")
        self.assertTrue(result.ok)
        self.assertEqual(port.rendered, [])
        self.assertIsNone(port.written_text)

    def test_missing_document_yields_minimal_declaration(self) -> None:
        port = FakeConfigDocument(None)
        result = LayoutPresetApplyService(port).apply(
            LayoutPresetApplyInput(preset_name="side-by-side", write=True)
        )
        self.assertEqual(result.status, "written")
        (rendered,) = port.rendered
        self.assertEqual(
            rendered,
            {
                "lane_placement": {
                    "default": {"split": "right"},
                    "sublane": {"split": "right"},
                }
            },
        )

    def test_load_failure_is_a_typed_result(self) -> None:
        port = FakeConfigDocument(load_error=RepoLocalConfigError("boom"))
        result = LayoutPresetApplyService(port).apply(
            LayoutPresetApplyInput(preset_name="stacked", write=True)
        )
        self.assertEqual(result.status, "config_unreadable")
        self.assertFalse(result.ok)
        self.assertIn("boom", result.error or "")

    def test_non_mapping_document_is_a_typed_result(self) -> None:
        port = FakeConfigDocument(["a", "b"])
        result = LayoutPresetApplyService(port).apply(
            LayoutPresetApplyInput(preset_name="stacked")
        )
        self.assertEqual(result.status, "not_a_mapping")
        self.assertFalse(result.ok)

    def test_invalid_ratio_is_a_typed_result_and_writes_nothing(self) -> None:
        port = FakeConfigDocument({"version": 2})
        result = LayoutPresetApplyService(port).apply(
            LayoutPresetApplyInput(preset_name="stacked", ratio=0.95, write=True)
        )
        self.assertEqual(result.status, "invalid_input")
        self.assertFalse(result.ok)
        self.assertEqual(port.rendered, [])
        self.assertIsNone(port.written_text)

    def test_write_failure_is_a_typed_result(self) -> None:
        port = FakeConfigDocument({"version": 2}, write_error=OSError("disk full"))
        result = LayoutPresetApplyService(port).apply(
            LayoutPresetApplyInput(preset_name="side-by-side", write=True)
        )
        self.assertEqual(result.status, "write_failed")
        self.assertFalse(result.ok)
        self.assertIn("disk full", result.error or "")

    def test_shadowed_lane_kinds_flow_into_the_result(self) -> None:
        port = FakeConfigDocument(
            {"lane_placement": {"by_lane_kind": {"coordinator": {"split": "down"}}}}
        )
        result = LayoutPresetApplyService(port).apply(
            LayoutPresetApplyInput(preset_name="side-by-side")
        )
        self.assertEqual(result.shadowed_by_lane_kind, ("coordinator",))


if __name__ == "__main__":
    unittest.main()
