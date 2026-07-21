"""Governed work-unit granularity schema + dispatch decision tests (Redmine #13002).

Pins the #13002 acceptance contract:

- the closed granularity enum (``epic`` / ``feature`` / ``user_story`` /
  ``leaf_issue``) with the ``user_story`` default (1 UserStory = 1 work unit);
- ``WorkUnitGranularityConfig.from_record`` fail-closed schema (unknown key,
  unsupported version, non-mapping, non-enum granularity — never a silent
  default);
- the pure ``decide_work_unit_dispatch`` gate: ``user_story`` allowed as the
  standard, ``leaf_issue`` allowed as the task-level-exception unit, ``epic`` /
  ``feature`` blocked unless an explicit owner / operator decision anchor
  (durable journal id) is supplied — a blank anchor never counts;
- the CLI-side ``resolve_work_unit_request_fields`` precedence: explicit flag >
  repo-local config > ``user_story`` default, failing closed on a broken config.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.work_unit_granularity import (
    DEFAULT_WORK_UNIT_GRANULARITY,
    DISPATCH_ALLOWED,
    DISPATCH_BLOCKED,
    EXPLICIT_DECISION_GRANULARITIES,
    WORK_UNIT_EPIC,
    WORK_UNIT_EXPLICIT_DECISION_RECORDED,
    WORK_UNIT_EXPLICIT_DECISION_REQUIRED,
    WORK_UNIT_FEATURE,
    WORK_UNIT_GRANULARITIES,
    WORK_UNIT_LEAF_DECISION_RECORDED,
    WORK_UNIT_LEAF_DECISION_REQUIRED,
    WORK_UNIT_LEAF_ISSUE,
    WORK_UNIT_LEAF_STANDALONE,
    WORK_UNIT_STANDARD,
    WORK_UNIT_USER_STORY,
    WorkUnitDispatchDecision,
    WorkUnitGranularityConfig,
    WorkUnitGranularityError,
    decide_work_unit_dispatch,
    normalize_work_unit_granularity,
)


class VocabularyTests(unittest.TestCase):
    def test_enum_is_the_acceptance_vocabulary(self):
        self.assertEqual(
            WORK_UNIT_GRANULARITIES,
            {"epic", "feature", "user_story", "leaf_issue"},
        )

    def test_default_is_user_story(self):
        self.assertEqual(DEFAULT_WORK_UNIT_GRANULARITY, WORK_UNIT_USER_STORY)

    def test_explicit_decision_set_is_epic_and_feature(self):
        self.assertEqual(
            EXPLICIT_DECISION_GRANULARITIES, {WORK_UNIT_EPIC, WORK_UNIT_FEATURE}
        )

    def test_normalize_accepts_every_enum_token(self):
        for token in WORK_UNIT_GRANULARITIES:
            self.assertEqual(normalize_work_unit_granularity(token), token)

    def test_normalize_strips_whitespace(self):
        self.assertEqual(
            normalize_work_unit_granularity("  user_story "), WORK_UNIT_USER_STORY
        )

    def test_normalize_rejects_unknown_blank_and_non_string(self):
        for bad in ("story", "US", "", "   ", None, 1, True, ["user_story"]):
            with self.assertRaises(WorkUnitGranularityError):
                normalize_work_unit_granularity(bad)


class ConfigSchemaTests(unittest.TestCase):
    def test_none_and_empty_are_the_user_story_default(self):
        self.assertEqual(
            WorkUnitGranularityConfig.from_record(None),
            WorkUnitGranularityConfig.default(),
        )
        self.assertEqual(
            WorkUnitGranularityConfig.from_record({}).granularity,
            WORK_UNIT_USER_STORY,
        )

    def test_valid_record_selects_granularity(self):
        config = WorkUnitGranularityConfig.from_record(
            {"version": 1, "granularity": "leaf_issue"}
        )
        self.assertEqual(config.granularity, WORK_UNIT_LEAF_ISSUE)

    def test_non_mapping_fails_closed(self):
        for bad in ("user_story", ["user_story"], 1):
            with self.assertRaises(WorkUnitGranularityError):
                WorkUnitGranularityConfig.from_record(bad)  # type: ignore[arg-type]

    def test_unknown_key_fails_closed(self):
        with self.assertRaises(WorkUnitGranularityError):
            WorkUnitGranularityConfig.from_record({"granularities": "user_story"})

    def test_unsupported_or_bool_version_fails_closed(self):
        for version in (2, 0, "1", True):
            with self.assertRaises(WorkUnitGranularityError):
                WorkUnitGranularityConfig.from_record(
                    {"version": version, "granularity": "user_story"}
                )

    def test_non_enum_granularity_never_silently_defaults(self):
        with self.assertRaises(WorkUnitGranularityError):
            WorkUnitGranularityConfig.from_record({"granularity": "userstory"})

    def test_direct_construction_validates_too(self):
        with self.assertRaises(WorkUnitGranularityError):
            WorkUnitGranularityConfig(granularity="sprint")


class DispatchDecisionTests(unittest.TestCase):
    def test_user_story_is_the_allowed_standard(self):
        decision = decide_work_unit_dispatch(WORK_UNIT_USER_STORY)
        self.assertTrue(decision.is_allowed)
        self.assertEqual(decision.status, DISPATCH_ALLOWED)
        self.assertEqual(decision.diagnostic, WORK_UNIT_STANDARD)

    def test_leaf_issue_blocks_by_default_redmine_14224(self):
        # Redmine #14224: a leaf_issue dispatch with NO standalone declaration and NO
        # decision anchor is blocked by default -- this is the fix itself (leaf_issue
        # was unconditionally allowed before #14224, which is how 8/9 active lanes
        # ended up leaf-sized despite the user_story standard).
        decision = decide_work_unit_dispatch(WORK_UNIT_LEAF_ISSUE)
        self.assertFalse(decision.is_allowed)
        self.assertEqual(decision.status, DISPATCH_BLOCKED)
        self.assertEqual(decision.diagnostic, WORK_UNIT_LEAF_DECISION_REQUIRED)

    def test_leaf_issue_standalone_is_allowed_no_anchor_needed(self):
        decision = decide_work_unit_dispatch(
            WORK_UNIT_LEAF_ISSUE, leaf_standalone=True
        )
        self.assertTrue(decision.is_allowed)
        self.assertEqual(decision.diagnostic, WORK_UNIT_LEAF_STANDALONE)

    def test_leaf_issue_with_parent_us_allowed_with_durable_anchor(self):
        decision = decide_work_unit_dispatch(
            WORK_UNIT_LEAF_ISSUE, explicit_decision_anchor="70719"
        )
        self.assertTrue(decision.is_allowed)
        self.assertEqual(decision.diagnostic, WORK_UNIT_LEAF_DECISION_RECORDED)
        self.assertEqual(decision.decision_anchor, "70719")

    def test_leaf_issue_blank_anchor_counts_as_absent(self):
        decision = decide_work_unit_dispatch(
            WORK_UNIT_LEAF_ISSUE, explicit_decision_anchor="   "
        )
        self.assertFalse(decision.is_allowed)
        self.assertEqual(decision.diagnostic, WORK_UNIT_LEAF_DECISION_REQUIRED)

    def test_leaf_issue_standalone_takes_precedence_over_missing_anchor(self):
        # standalone=True needs no anchor at all -- it is a genuinely separate escape
        # valve from the anchor mechanism, not a fallback that still checks for one.
        decision = decide_work_unit_dispatch(
            WORK_UNIT_LEAF_ISSUE, leaf_standalone=True, explicit_decision_anchor=None
        )
        self.assertTrue(decision.is_allowed)
        self.assertEqual(decision.diagnostic, WORK_UNIT_LEAF_STANDALONE)

    def test_leaf_issue_review_exception_alone_does_not_bypass_the_fence(self):
        # Redmine #14222/#14224 close condition 2: "review exceptionだけではblock
        # 解除されない" -- there is no parameter for a review-exception flag at all, so
        # passing only the granularity (as if a task-level review need were somehow
        # itself the anchor) stays blocked. The ONLY escapes are leaf_standalone=True
        # or a real explicit_decision_anchor.
        decision = decide_work_unit_dispatch(WORK_UNIT_LEAF_ISSUE)
        self.assertFalse(decision.is_allowed)
        self.assertIn("task-level review", decision.reason)

    def test_epic_and_feature_block_without_explicit_decision(self):
        for unit in sorted(EXPLICIT_DECISION_GRANULARITIES):
            decision = decide_work_unit_dispatch(unit)
            self.assertFalse(decision.is_allowed)
            self.assertEqual(decision.status, DISPATCH_BLOCKED)
            self.assertEqual(
                decision.diagnostic, WORK_UNIT_EXPLICIT_DECISION_REQUIRED
            )

    def test_blank_anchor_counts_as_absent(self):
        decision = decide_work_unit_dispatch(
            WORK_UNIT_EPIC, explicit_decision_anchor="   "
        )
        self.assertFalse(decision.is_allowed)

    def test_epic_and_feature_allowed_with_durable_anchor(self):
        for unit in sorted(EXPLICIT_DECISION_GRANULARITIES):
            decision = decide_work_unit_dispatch(
                unit, explicit_decision_anchor="70719"
            )
            self.assertTrue(decision.is_allowed)
            self.assertEqual(
                decision.diagnostic, WORK_UNIT_EXPLICIT_DECISION_RECORDED
            )
            self.assertEqual(decision.decision_anchor, "70719")
            self.assertIn("70719", decision.reason)

    def test_unknown_granularity_raises_never_defaults(self):
        with self.assertRaises(WorkUnitGranularityError):
            decide_work_unit_dispatch("story")

    def test_payload_round_trip_fields(self):
        decision = decide_work_unit_dispatch(WORK_UNIT_FEATURE)
        payload = decision.as_payload()
        self.assertEqual(
            set(payload),
            {"granularity", "status", "diagnostic", "reason", "decision_anchor"},
        )
        self.assertIsInstance(decision, WorkUnitDispatchDecision)


class ResolveRequestFieldsTests(unittest.TestCase):
    """CLI-side resolution: flag > repo-local config > user_story default."""

    def _args(self, **kw):
        ns = argparse.Namespace()
        for key, value in kw.items():
            setattr(ns, key, value)
        return ns

    def test_explicit_flag_wins_over_config(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
            resolve_work_unit_request_fields,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mozyo-bridge").mkdir()
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "work_unit:\n  granularity: leaf_issue\n", encoding="utf-8"
            )
            unit, anchor = resolve_work_unit_request_fields(
                self._args(work_unit="epic", work_unit_decision_journal="70719"),
                root,
            )
        self.assertEqual(unit, WORK_UNIT_EPIC)
        self.assertEqual(anchor, "70719")

    def test_config_fallback_when_no_flag(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
            resolve_work_unit_request_fields,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mozyo-bridge").mkdir()
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "work_unit:\n  granularity: leaf_issue\n", encoding="utf-8"
            )
            unit, anchor = resolve_work_unit_request_fields(
                self._args(work_unit=None, work_unit_decision_journal=None), root
            )
        self.assertEqual(unit, WORK_UNIT_LEAF_ISSUE)
        self.assertIsNone(anchor)

    def test_missing_config_is_the_user_story_default(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
            resolve_work_unit_request_fields,
        )

        with tempfile.TemporaryDirectory() as tmp:
            unit, _ = resolve_work_unit_request_fields(
                self._args(work_unit=None, work_unit_decision_journal=None),
                Path(tmp),
            )
        self.assertEqual(unit, WORK_UNIT_USER_STORY)

    def test_broken_config_fails_closed(self):
        from mozyo_bridge.application.repo_local_config_loader import (
            RepoLocalConfigLoadError,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (
            resolve_work_unit_request_fields,
        )
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
            RepoLocalConfigError,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mozyo-bridge").mkdir()
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "work_unit:\n  granularity: sprint\n", encoding="utf-8"
            )
            with self.assertRaises(RepoLocalConfigError):
                resolve_work_unit_request_fields(
                    self._args(work_unit=None, work_unit_decision_journal=None),
                    root,
                )
        # The loader error type stays a subclass so one except catches all.
        self.assertTrue(issubclass(RepoLocalConfigLoadError, RepoLocalConfigError))


if __name__ == "__main__":
    unittest.main()
