"""Pure core of test-process home isolation (Redmine #14757).

Unit scope: the value objects and decisions in
``domain/test_home_isolation.py``. No filesystem, no sqlite, no subprocess —
snapshots are constructed directly, so what is characterised here is the
*algebra* (which tier a difference lands in, and how a suite verdict composes
with a home verdict), not the reading of a real home. The reading side is
integration-tested; the defect itself is pinned in
``tests/regressions/test_issue_14757_test_process_home_isolation.py``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (  # noqa: E402,E501
    GUARDED_TIERS,
    LIVE_LANE_ENV_KEYS,
    HomeDelta,
    HomeGuardVerdict,
    HomeSnapshot,
    IsolatedRunOutcome,
    IsolationLayout,
    apply_isolation,
    compare_snapshots,
    digest,
    isolation_env,
)


def _snapshot(**overrides) -> HomeSnapshot:
    base = dict(
        home="/guarded/home",
        entry_count=3,
        entry_digest="entries0",
        schema_digest="schema0",
        identity_digest="identity0",
        backup_count=1,
        backup_digest="backups0",
        store_count=2,
    )
    base.update(overrides)
    return HomeSnapshot(**base)


class LayoutTest(unittest.TestCase):
    def test_every_role_is_a_distinct_directory_under_the_task_root(self) -> None:
        """One root per run, and a file's location says which resolver made it."""
        layout = IsolationLayout(root=Path("/task/root"))
        directories = layout.directories
        self.assertEqual(len(set(directories)), len(directories))
        for directory in directories:
            self.assertTrue(directory.is_relative_to(Path("/task/root")))

    def test_the_home_and_tmp_roots_are_not_the_same_directory(self) -> None:
        layout = IsolationLayout(root=Path("/task/root"))
        self.assertNotEqual(layout.home, layout.tmp)


class IsolationEnvTest(unittest.TestCase):
    def test_no_home_pin_is_emitted(self) -> None:
        """#14757 acceptance 1: isolation comes from the resolver, not from HOME."""
        pins = isolation_env(
            IsolationLayout(root=Path("/task")),
            denied_homes=(Path("/operator/.mozyo_bridge"),),
            deny_separator=":",
            fence_root_key="FENCE",
            fence_deny_key="DENY",
        )
        self.assertNotIn("HOME", pins)

    def test_the_deny_list_is_deduplicated_and_ordered(self) -> None:
        """Two spellings of one home must not produce two deny entries."""
        pins = isolation_env(
            IsolationLayout(root=Path("/task")),
            denied_homes=(Path("/b/home"), Path("/a/home"), Path("/b/home")),
            deny_separator=":",
            fence_root_key="FENCE",
            fence_deny_key="DENY",
        )
        self.assertEqual(pins["DENY"], "/a/home:/b/home")

    def test_the_fence_root_is_the_pinned_home(self) -> None:
        """A cleared env must land where MOZYO_BRIDGE_HOME pointed, not elsewhere."""
        layout = IsolationLayout(root=Path("/task"))
        pins = isolation_env(
            layout,
            denied_homes=(),
            deny_separator=":",
            fence_root_key="FENCE",
            fence_deny_key="DENY",
        )
        self.assertEqual(pins["FENCE"], pins["MOZYO_BRIDGE_HOME"])
        self.assertEqual(pins["FENCE"], str(layout.home))

    def test_applying_isolation_keeps_the_base_env_and_drops_the_lane_pins(
        self,
    ) -> None:
        base = {"PATH": "/usr/bin", "HOME": "/operator", "TMUX": "live"}
        env = apply_isolation(base, {"MOZYO_BRIDGE_HOME": "/task/home"})
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/operator")
        self.assertEqual(env["MOZYO_BRIDGE_HOME"], "/task/home")
        self.assertNotIn("TMUX", env)

    def test_applying_isolation_does_not_mutate_the_caller_env(self) -> None:
        base = {"TMUX": "live"}
        apply_isolation(base, {})
        self.assertEqual(base, {"TMUX": "live"})

    def test_every_live_lane_key_is_stripped(self) -> None:
        env = apply_isolation({key: "live" for key in LIVE_LANE_ENV_KEYS}, {})
        self.assertEqual(env, {})


class DigestTest(unittest.TestCase):
    def test_the_digest_carries_no_operator_value(self) -> None:
        """Snapshots are recorded in journals, so they must disclose nothing."""
        secret = "/Users/someone/.mozyo_bridge/redmine-credentials.yaml"
        self.assertNotIn("someone", digest((secret,)))
        self.assertNotIn("mozyo_bridge", digest((secret,)))

    def test_order_is_significant_and_the_digest_is_stable(self) -> None:
        self.assertEqual(digest(("a", "b")), digest(("a", "b")))
        self.assertNotEqual(digest(("a", "b")), digest(("b", "a")))

    def test_an_empty_component_set_still_digests(self) -> None:
        """A home with no stores must produce a comparable value, not a blank."""
        self.assertTrue(digest(()))


class CompareSnapshotsTest(unittest.TestCase):
    def test_identical_snapshots_are_unchanged(self) -> None:
        verdict = compare_snapshots(_snapshot(), _snapshot())
        self.assertTrue(verdict.unchanged)
        self.assertEqual(verdict.deltas, ())

    def test_each_tier_is_reported_under_its_own_name(self) -> None:
        """A reader must be able to tell schema drift from a new file."""
        for tier, field in (
            ("entries", "entry_digest"),
            ("schema", "schema_digest"),
            ("identity", "identity_digest"),
            ("backups", "backup_digest"),
        ):
            with self.subTest(tier=tier):
                verdict = compare_snapshots(
                    _snapshot(), _snapshot(**{field: "moved"})
                )
                self.assertEqual([d.tier for d in verdict.deltas], [tier])

    def test_every_guarded_tier_is_actually_compared(self) -> None:
        """The declared tier list and the compared tier list must not drift."""
        compared = set()
        for field in (
            "entry_digest",
            "schema_digest",
            "identity_digest",
            "backup_digest",
        ):
            verdict = compare_snapshots(_snapshot(), _snapshot(**{field: "moved"}))
            compared.update(delta.tier for delta in verdict.deltas)
        self.assertEqual(compared, set(GUARDED_TIERS))

    def test_a_counted_tier_reports_the_count_alongside_the_digest(self) -> None:
        verdict = compare_snapshots(
            _snapshot(entry_count=3, entry_digest="a"),
            _snapshot(entry_count=4, entry_digest="b"),
        )
        self.assertEqual(verdict.deltas[0].before, "3/a")
        self.assertEqual(verdict.deltas[0].after, "4/b")

    def test_appearing_and_disappearing_are_both_existence_deltas(self) -> None:
        appeared = compare_snapshots(
            HomeSnapshot(home="/h", missing=True), _snapshot(home="/h")
        )
        vanished = compare_snapshots(
            _snapshot(home="/h"), HomeSnapshot(home="/h", missing=True)
        )
        self.assertEqual(appeared.deltas[0].tier, "existence")
        self.assertEqual(vanished.deltas[0].tier, "existence")

    def test_an_unreadable_component_is_never_a_pass(self) -> None:
        """"Could not look" must not compose into "nothing changed"."""
        snapshot = _snapshot(unreadable=("registry.sqlite (DatabaseError)",))
        verdict = compare_snapshots(snapshot, snapshot)
        self.assertEqual(verdict.deltas, ())
        self.assertFalse(verdict.unchanged)
        self.assertTrue(any("unreadable" in reason for reason in verdict.reasons))

    def test_unreadable_components_from_both_sides_are_merged(self) -> None:
        verdict = compare_snapshots(
            _snapshot(unreadable=("a",)), _snapshot(unreadable=("b", "a"))
        )
        self.assertEqual(verdict.unreadable, ("a", "b"))


class IsolatedRunOutcomeTest(unittest.TestCase):
    """The composition that makes the #14477 shape unreportable as a pass."""

    def test_a_green_suite_over_a_changed_home_is_not_a_pass(self) -> None:
        """This is the whole point: #14477's tests were green.

        The suite verdict alone said PASS while the operator's shared store had
        been forward-migrated underneath it.
        """
        outcome = IsolatedRunOutcome(
            suite_success=True,
            guards=(
                HomeGuardVerdict(
                    home="/h",
                    deltas=(HomeDelta(tier="schema", before="v7", after="v8"),),
                ),
            ),
            returncode=0,
        )
        self.assertTrue(outcome.suite_success)
        self.assertFalse(outcome.success)
        self.assertTrue(
            any("operator shared home changed" in r for r in outcome.all_reasons)
        )

    def test_a_red_suite_over_an_untouched_home_is_still_red(self) -> None:
        outcome = IsolatedRunOutcome(
            suite_success=False, guards=(HomeGuardVerdict(home="/h"),), returncode=1
        )
        self.assertFalse(outcome.success)
        self.assertTrue(outcome.all_reasons)

    def test_both_green_is_a_pass_with_no_reasons(self) -> None:
        outcome = IsolatedRunOutcome(
            suite_success=True, guards=(HomeGuardVerdict(home="/h"),), returncode=0
        )
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.all_reasons, ())

    def test_a_red_suite_reports_its_returncode_when_no_detail_is_given(self) -> None:
        outcome = IsolatedRunOutcome(
            suite_success=False, guards=(HomeGuardVerdict(home="/h"),), returncode=7
        )
        self.assertTrue(any("returncode=7" in r for r in outcome.all_reasons))

    def test_the_payload_reports_both_halves_separately(self) -> None:
        """A reader must see why a run failed, not just that it did."""
        outcome = IsolatedRunOutcome(
            suite_success=True,
            guards=(
                HomeGuardVerdict(
                    home="/h",
                    deltas=(HomeDelta(tier="identity", before="3", after="4"),),
                ),
            ),
            returncode=0,
            fence_root="/task/home",
        )
        payload = outcome.as_dict()
        self.assertFalse(payload["success"])
        self.assertTrue(payload["suite_success"])
        self.assertFalse(payload["home_guards"][0]["unchanged"])
        # Default output is journal-safe: a role/ordinal/digest label, not the
        # absolute task root (j#100490 item 4).
        self.assertNotIn("/task/home", payload["fence_root"])
        self.assertTrue(payload["fence_root"].startswith("fence-root["))
        # The absolute paths remain available behind the explicit local-debug opt-in.
        self.assertEqual(
            outcome.as_dict(reveal_paths=True)["fence_root"], "/task/home"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
