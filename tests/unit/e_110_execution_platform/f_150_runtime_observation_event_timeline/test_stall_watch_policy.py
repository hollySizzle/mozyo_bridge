"""Stall-watch runtime policy tests (Redmine #15855).

Cadence, threshold and scope are operator runtime policy, not product defaults
(``stall-watcher-screen-diff.md`` `## 既存正本との境界`). The property that matters most
here is what an **absent** block resolves to: nothing watched, not everything watched with
defaults. The three off-states (absent / declared-without-scope / invalid) are checked as
distinct, because a status surface has to tell an operator which one they are in.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_watch_policy import (  # noqa: E501
    DEFAULT_STALL_WATCH_CADENCE_SECONDS,
    DEFAULT_STALL_WATCH_THRESHOLD,
    MINIMUM_CADENCE_SECONDS,
    POLICY_ABSENT,
    POLICY_CONFIGURED,
    POLICY_INVALID,
    POLICY_NO_SCOPE,
    STALL_WATCH_KEYS,
    StallWatchPolicy,
    StallWatchPolicyError,
)


class AbsentTest(unittest.TestCase):
    def test_an_absent_block_watches_nothing(self) -> None:
        # NOT "everything with defaults": a watcher that silently reads every pane it can
        # find is a surveillance surface nobody asked for.
        policy = StallWatchPolicy.from_record(None)
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.reason, POLICY_ABSENT)
        self.assertFalse(policy.watches_lane("any_lane"))
        self.assertFalse(policy.watches_role("claude"))
        self.assertFalse(policy.admits(lane_id="any_lane", role="claude"))

    def test_the_default_is_the_absent_policy(self) -> None:
        self.assertEqual(StallWatchPolicy.default(), StallWatchPolicy.from_record(None))

    def test_portable_defaults_are_still_reported(self) -> None:
        # The numbers exist so a status surface can show what WOULD be used; they do not
        # make the watcher run.
        policy = StallWatchPolicy.default()
        self.assertEqual(policy.cadence_seconds, DEFAULT_STALL_WATCH_CADENCE_SECONDS)
        self.assertEqual(policy.threshold, DEFAULT_STALL_WATCH_THRESHOLD)


class ScopeTest(unittest.TestCase):
    def test_explicit_lanes_are_admitted_and_others_are_not(self) -> None:
        policy = StallWatchPolicy.from_record({"lanes": ["lane_a", "lane_b"]})
        self.assertTrue(policy.enabled)
        self.assertTrue(policy.admits(lane_id="lane_a", role="claude"))
        self.assertFalse(policy.admits(lane_id="lane_c", role="claude"))

    def test_all_managed_lanes_is_an_explicit_opt_in(self) -> None:
        # Expressible, but only as a key an operator had to type -- never as a pattern that
        # quietly widens when the cockpit grows.
        policy = StallWatchPolicy.from_record({"all_managed_lanes": True})
        self.assertTrue(policy.admits(lane_id="anything", role="claude"))

    def test_there_is_no_wildcard_lane(self) -> None:
        policy = StallWatchPolicy.from_record({"lanes": ["*"]})
        self.assertFalse(policy.admits(lane_id="lane_a", role="claude"))
        self.assertTrue(policy.admits(lane_id="*", role="claude"))

    def test_declaring_both_scopes_is_refused_rather_than_merged(self) -> None:
        with self.assertRaises(StallWatchPolicyError):
            StallWatchPolicy.from_record({"all_managed_lanes": True, "lanes": ["lane_a"]})

    def test_a_declared_block_with_no_scope_is_a_legible_off_state(self) -> None:
        policy = StallWatchPolicy.from_record({"threshold": 3})
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.reason, POLICY_NO_SCOPE)
        self.assertEqual(policy.threshold, 3)
        self.assertIn("names no lanes", policy.detail)

    def test_an_empty_roles_list_admits_every_role_inside_an_admitted_lane(self) -> None:
        policy = StallWatchPolicy.from_record({"lanes": ["lane_a"]})
        self.assertTrue(policy.admits(lane_id="lane_a", role="claude"))
        self.assertTrue(policy.admits(lane_id="lane_a", role="codex"))

    def test_a_role_list_refines_within_an_admitted_lane(self) -> None:
        policy = StallWatchPolicy.from_record({"lanes": ["lane_a"], "roles": ["claude"]})
        self.assertTrue(policy.admits(lane_id="lane_a", role="claude"))
        self.assertFalse(policy.admits(lane_id="lane_a", role="codex"))

    def test_a_role_list_never_widens_the_lane_scope(self) -> None:
        policy = StallWatchPolicy.from_record({"lanes": ["lane_a"], "roles": ["claude"]})
        self.assertFalse(policy.admits(lane_id="lane_z", role="claude"))


class DisabledGuardTest(unittest.TestCase):
    """The ``enabled`` guard is tested against inputs the parser cannot produce.

    Every disabled policy :meth:`from_record` builds also has an empty scope, so the guard
    and the empty scope agree on the answer and neither is observable behind the other.
    That agreement is a property of the parser, not of the record: :class:`StallWatchPolicy`
    is a plain dataclass, and a caller assembling one by hand (a migration, a future
    resolver, a test fixture) can hold a broad scope with ``enabled`` false. The guard is
    what makes that combination refuse rather than watch, so it is exercised directly.
    """

    def test_a_disabled_policy_with_a_broad_scope_still_watches_nothing(self) -> None:
        policy = StallWatchPolicy(enabled=False, all_managed_lanes=True)
        self.assertFalse(policy.watches_lane("anything"))
        self.assertFalse(policy.admits(lane_id="anything", role="claude"))

    def test_a_disabled_policy_with_explicit_lanes_still_watches_nothing(self) -> None:
        policy = StallWatchPolicy(enabled=False, lanes=("lane_a",), roles=("claude",))
        self.assertFalse(policy.watches_lane("lane_a"))
        self.assertFalse(policy.watches_role("claude"))
        self.assertFalse(policy.admits(lane_id="lane_a", role="claude"))


class ValidationTest(unittest.TestCase):
    def test_unknown_keys_are_refused(self) -> None:
        with self.assertRaises(StallWatchPolicyError):
            StallWatchPolicy.from_record({"lanes": ["a"], "cadence": 300})

    def test_the_key_set_is_closed_and_matches_the_parser(self) -> None:
        # Enumeration-independent: every declared key must actually parse.
        for key in STALL_WATCH_KEYS:
            with self.subTest(key=key):
                value = {
                    "cadence_seconds": 300,
                    "threshold": 2,
                    "roles": ["claude"],
                    "lanes": ["lane_a"],
                    "all_managed_lanes": False,
                }[key]
                StallWatchPolicy.from_record({key: value, "lanes": ["lane_a"]}
                                             if key != "lanes" else {key: value})

    def test_a_non_mapping_block_is_refused(self) -> None:
        for bad in ([], "lanes", 5):
            with self.subTest(bad=bad):
                with self.assertRaises(StallWatchPolicyError):
                    StallWatchPolicy.from_record(bad)

    def test_a_sub_minimum_cadence_is_refused_not_rounded(self) -> None:
        # A sub-tick cadence cannot be honoured and would make the watermark meaningless.
        with self.assertRaises(StallWatchPolicyError):
            StallWatchPolicy.from_record(
                {"lanes": ["a"], "cadence_seconds": MINIMUM_CADENCE_SECONDS - 1}
            )

    def test_a_threshold_below_one_is_refused(self) -> None:
        with self.assertRaises(StallWatchPolicyError):
            StallWatchPolicy.from_record({"lanes": ["a"], "threshold": 0})

    def test_a_boolean_is_not_accepted_as_a_number(self) -> None:
        # `threshold: true` is a mistake, not the number 1.
        for field in ("cadence_seconds", "threshold"):
            with self.subTest(field=field):
                with self.assertRaises(StallWatchPolicyError):
                    StallWatchPolicy.from_record({"lanes": ["a"], field: True})

    def test_a_bare_string_is_not_a_lane_list(self) -> None:
        with self.assertRaises(StallWatchPolicyError):
            StallWatchPolicy.from_record({"lanes": "lane_a"})

    def test_blank_and_duplicate_entries_are_refused(self) -> None:
        with self.assertRaises(StallWatchPolicyError):
            StallWatchPolicy.from_record({"lanes": ["lane_a", ""]})
        with self.assertRaises(StallWatchPolicyError):
            StallWatchPolicy.from_record({"lanes": ["lane_a", "lane_a"]})

    def test_a_non_boolean_all_managed_lanes_is_refused(self) -> None:
        with self.assertRaises(StallWatchPolicyError):
            StallWatchPolicy.from_record({"all_managed_lanes": "yes"})


class ResolveTest(unittest.TestCase):
    def test_resolve_turns_a_malformed_block_into_a_disabled_policy(self) -> None:
        # A watcher tick must not die on a bad config, and must not silently run on values
        # nobody chose.
        policy = StallWatchPolicy.resolve({"cadence_seconds": "soon"})
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.reason, POLICY_INVALID)
        self.assertIn("cadence_seconds", policy.detail)
        self.assertFalse(policy.admits(lane_id="lane_a", role="claude"))

    def test_resolve_does_not_repair_invalid_into_defaults(self) -> None:
        policy = StallWatchPolicy.resolve(
            {"all_managed_lanes": True, "cadence_seconds": 1}
        )
        self.assertFalse(policy.enabled)
        self.assertFalse(policy.admits(lane_id="lane_a", role="claude"))

    def test_resolve_passes_a_valid_block_through(self) -> None:
        policy = StallWatchPolicy.resolve({"lanes": ["lane_a"], "cadence_seconds": 600})
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.reason, POLICY_CONFIGURED)
        self.assertEqual(policy.cadence_seconds, 600)


class SourceTest(unittest.TestCase):
    def test_source_distinguishes_configured_from_every_off_state(self) -> None:
        # `source` lands in the escalation record's `policy` field, so a reader can tell a
        # deliberately-configured cadence from a shipped one.
        self.assertEqual(
            StallWatchPolicy.from_record({"lanes": ["a"]}).source, "repo_local_config"
        )
        self.assertEqual(StallWatchPolicy.default().source, POLICY_ABSENT)
        self.assertEqual(
            StallWatchPolicy.resolve({"cadence_seconds": "x"}).source, POLICY_INVALID
        )
        self.assertEqual(
            StallWatchPolicy.from_record({"threshold": 2}).source, POLICY_NO_SCOPE
        )

    def test_telemetry_reports_the_effective_values_and_the_reason(self) -> None:
        payload = StallWatchPolicy.from_record(
            {"lanes": ["lane_a"], "roles": ["claude"], "threshold": 4}
        ).telemetry()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["reason"], POLICY_CONFIGURED)
        self.assertEqual(payload["threshold"], 4)
        self.assertEqual(payload["lanes"], ["lane_a"])
        self.assertEqual(payload["roles"], ["claude"])
        self.assertFalse(payload["all_managed_lanes"])


class ConfigSchemaTest(unittest.TestCase):
    """The block reaches the policy through the closed repo-local config schema."""

    def test_the_block_is_a_recognized_top_level_key(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
            REPO_LOCAL_CONFIG_KEYS,
            RepoLocalConfig,
        )

        self.assertIn("stall_watch", REPO_LOCAL_CONFIG_KEYS)
        self.assertEqual(
            RepoLocalConfig.from_record({"version": 2, "stall_watch": {"lanes": ["a"]}})
            .stall_watch.lanes,
            ("a",),
        )

    def test_a_config_with_no_block_is_behavior_preserving(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
            RepoLocalConfig,
        )

        self.assertFalse(RepoLocalConfig.from_record({}).stall_watch.enabled)

    def test_a_malformed_block_fails_the_loader_closed(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
            RepoLocalConfig,
            RepoLocalConfigError,
        )

        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"version": 2, "stall_watch": {"cadence_seconds": "soon"}}
            )

    def test_the_effective_values_are_readable_from_the_status_surface(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_status import (  # noqa: E501
            CONFIG_BLOCK_KEYS,
            CONFIG_LEAF_KEYS,
        )

        self.assertIn("stall_watch", CONFIG_BLOCK_KEYS)
        leaves = {name for name, _ in CONFIG_LEAF_KEYS}
        for leaf in (
            "stall_watch.cadence_seconds",
            "stall_watch.threshold",
            "stall_watch.all_managed_lanes",
            "stall_watch.lanes",
            "stall_watch.roles",
        ):
            with self.subTest(leaf=leaf):
                self.assertIn(leaf, leaves)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
