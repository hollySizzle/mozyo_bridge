"""Module-to-test impact resolver tests (Redmine #12752).

Covers the bounded-context mapping (numbered ``e_*/f_*`` source -> mirror
tests), direct vs neighbor classification, the fail-closed fallbacks (no direct
test -> neighbor; unmapped path -> full suite) the acceptance criteria require
instead of fail-open behavior, the aggregate plan escalation, and the
filesystem lister + CLI handler glue. The pure resolver is fed a synthetic test
file list so the mapping is exercised without a real tree.
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_impact import (  # noqa: E402
    FALLBACK_FULL,
    FALLBACK_NEIGHBOR,
    NEIGHBOR_FALLBACK,
    RESOLVED,
    STEM_RESOLVED,
    TEST_CHANGED,
    UNMAPPED,
    list_test_files,
    parse_source_target,
    resolve_impact,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (  # noqa: E402
    commands_test_impact,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.commands_test_impact import (  # noqa: E402
    cmd_tests_resolve,
)
from mozyo_bridge.docs_tools.impact import git_changed_paths_since  # noqa: E402

# A small synthetic mirror tree for the execution_platform / delegated
# coordinator feature.
TEST_FILES = (
    "tests/unit/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/test_delegation_route_planner.py",
    "tests/unit/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/test_grandchild_dispatch.py",
    "tests/unit/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/test_grandchild_stamp.py",
    "tests/unit/e_110_execution_platform/f_130_handoff_routing/test_handoff.py",
    "tests/integration/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/test_route_plan_integration.py",
    "tests/unit/e_150_quality_architecture/f_130_module_health/test_module_health.py",
)


class ParseSourceTargetTest(unittest.TestCase):
    def test_numbered_source_parses_epic_feature_layer_stem(self) -> None:
        target = parse_source_target(
            "src/mozyo_bridge/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff/domain/delegation_route_planner.py"
        )
        self.assertEqual(target.kind, "numbered_source")
        self.assertEqual(target.epic, "e_110_execution_platform")
        self.assertEqual(target.feature, "f_140_delegated_coordinator_nested_handoff")
        self.assertEqual(target.layer, "domain")
        self.assertEqual(target.module_stem, "delegation_route_planner")

    def test_flat_source_has_no_context(self) -> None:
        target = parse_source_target("src/mozyo_bridge/application/cli_handoff.py")
        self.assertEqual(target.kind, "flat_source")
        self.assertIsNone(target.epic)
        self.assertEqual(target.module_stem, "cli_handoff")

    def test_init_module_is_other(self) -> None:
        target = parse_source_target(
            "src/mozyo_bridge/e_110_execution_platform/__init__.py"
        )
        self.assertEqual(target.kind, "other")

    def test_test_path_is_test_kind(self) -> None:
        target = parse_source_target("tests/unit/e_110_execution_platform/x/test_foo.py")
        self.assertEqual(target.kind, "test")

    def test_non_source_path_is_other(self) -> None:
        self.assertEqual(parse_source_target("README.md").kind, "other")
        self.assertEqual(parse_source_target("vibes/docs/specs/x.md").kind, "other")


class ResolveNumberedTest(unittest.TestCase):
    def test_direct_test_resolved_with_feature_neighbors(self) -> None:
        plan = resolve_impact(
            [
                "src/mozyo_bridge/e_110_execution_platform/"
                "f_140_delegated_coordinator_nested_handoff/domain/delegation_route_planner.py"
            ],
            test_files=TEST_FILES,
        )
        res = plan.resolutions[0]
        self.assertEqual(res.status, RESOLVED)
        self.assertIn(
            "tests/unit/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff/test_delegation_route_planner.py",
            res.direct_tests,
        )
        # Same-feature neighbors are surfaced (unit + integration), not the
        # direct test itself.
        self.assertIn(
            "tests/integration/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff/test_route_plan_integration.py",
            res.neighbor_tests,
        )
        self.assertNotIn(
            "tests/unit/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff/test_delegation_route_planner.py",
            res.neighbor_tests,
        )
        # Neighbors stay focused to the same feature: a sibling feature in the
        # same epic is NOT pulled in when the feature has its own tests.
        self.assertNotIn(
            "tests/unit/e_110_execution_platform/f_130_handoff_routing/test_handoff.py",
            res.neighbor_tests,
        )
        # A different bounded context is never a neighbor.
        self.assertNotIn(
            "tests/unit/e_150_quality_architecture/f_130_module_health/test_module_health.py",
            res.neighbor_tests,
        )
        self.assertEqual(plan.recommendation, "selected")
        self.assertIsNone(plan.fallback)

    def test_feature_with_no_other_tests_widens_to_epic(self) -> None:
        # A feature whose only test is the direct one -> neighbors widen to epic.
        plan = resolve_impact(
            ["src/mozyo_bridge/e_110_execution_platform/f_130_handoff_routing/domain/handoff.py"],
            test_files=TEST_FILES,
        )
        res = plan.resolutions[0]
        self.assertEqual(res.status, RESOLVED)
        # f_130 has only test_handoff.py (the direct test); neighbors come from
        # the rest of the e_110 epic.
        self.assertIn(
            "tests/unit/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff/test_grandchild_dispatch.py",
            res.neighbor_tests,
        )
        self.assertTrue(any("widened neighbors" in n for n in res.notes))

    def test_no_direct_test_falls_back_to_neighbor_not_open(self) -> None:
        # A module in a known context but with no test_<stem>.py.
        plan = resolve_impact(
            [
                "src/mozyo_bridge/e_110_execution_platform/"
                "f_140_delegated_coordinator_nested_handoff/domain/brand_new_module.py"
            ],
            test_files=TEST_FILES,
        )
        res = plan.resolutions[0]
        self.assertEqual(res.status, NEIGHBOR_FALLBACK)
        self.assertEqual(res.direct_tests, ())
        self.assertIsNotNone(res.fallback)
        self.assertEqual(res.fallback.kind, FALLBACK_NEIGHBOR)
        self.assertTrue(res.fallback.reason)
        self.assertTrue(res.neighbor_tests)
        # Neighbor roots point at the feature dirs that actually hold tests.
        self.assertIn(
            "tests/unit/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff",
            res.fallback.roots,
        )
        # No unmapped path -> aggregate stays focused (not escalated to full).
        self.assertEqual(plan.recommendation, "selected")

    def test_epic_only_source_uses_epic_neighbors(self) -> None:
        # Numbered epic but no feature segment.
        plan = resolve_impact(
            ["src/mozyo_bridge/e_110_execution_platform/domain/something.py"],
            test_files=TEST_FILES,
        )
        res = plan.resolutions[0]
        self.assertEqual(res.status, NEIGHBOR_FALLBACK)
        # Every e_110 test is a neighbor candidate.
        self.assertIn(
            "tests/unit/e_110_execution_platform/f_130_handoff_routing/test_handoff.py",
            res.neighbor_tests,
        )


class ResolveFlatTest(unittest.TestCase):
    def test_flat_source_matched_by_stem(self) -> None:
        plan = resolve_impact(
            ["src/mozyo_bridge/application/module_health.py"],
            test_files=TEST_FILES,
        )
        res = plan.resolutions[0]
        self.assertEqual(res.status, STEM_RESOLVED)
        self.assertEqual(
            res.direct_tests,
            ("tests/unit/e_150_quality_architecture/f_130_module_health/test_module_health.py",),
        )

    def test_flat_source_without_test_is_unmapped_full(self) -> None:
        plan = resolve_impact(
            ["src/mozyo_bridge/application/no_such_thing.py"],
            test_files=TEST_FILES,
        )
        res = plan.resolutions[0]
        self.assertEqual(res.status, UNMAPPED)
        self.assertEqual(res.fallback.kind, FALLBACK_FULL)
        self.assertEqual(res.fallback.roots, ("tests",))


class ResolveOtherKindsTest(unittest.TestCase):
    def test_changed_test_is_its_own_target(self) -> None:
        path = (
            "tests/unit/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff/test_grandchild_dispatch.py"
        )
        plan = resolve_impact([path], test_files=TEST_FILES)
        res = plan.resolutions[0]
        self.assertEqual(res.status, TEST_CHANGED)
        self.assertEqual(res.direct_tests, (path,))

    def test_non_source_path_unmapped_full(self) -> None:
        plan = resolve_impact(["README.md"], test_files=TEST_FILES)
        res = plan.resolutions[0]
        self.assertEqual(res.status, UNMAPPED)
        self.assertEqual(res.fallback.kind, FALLBACK_FULL)


class AggregatePlanTest(unittest.TestCase):
    def test_any_unmapped_escalates_whole_plan_to_full(self) -> None:
        plan = resolve_impact(
            [
                "src/mozyo_bridge/e_110_execution_platform/"
                "f_140_delegated_coordinator_nested_handoff/domain/delegation_route_planner.py",
                "config/weird.toml",
            ],
            test_files=TEST_FILES,
        )
        self.assertTrue(plan.has_unmapped)
        self.assertEqual(plan.recommendation, "full")
        self.assertEqual(plan.fallback.kind, FALLBACK_FULL)
        self.assertIn("config/weird.toml", plan.fallback.reason)
        # The focused selection is still reported even while recommending full.
        self.assertTrue(plan.selected_tests)

    def test_known_context_with_no_tests_escalates_to_full(self) -> None:
        # Regression (Codex j#67568 finding 2): a numbered source in a context
        # that holds no test files at all must not report selected/empty (which
        # a runner reads as fail-open) — escalate to the full suite.
        plan = resolve_impact(
            ["src/mozyo_bridge/e_999_new_context/f_110_new_feature/domain/new_module.py"],
            test_files=(),
        )
        res = plan.resolutions[0]
        self.assertEqual(res.status, UNMAPPED)
        self.assertEqual(res.fallback.kind, FALLBACK_FULL)
        self.assertEqual(plan.recommendation, "full")
        self.assertEqual(plan.selected_tests, ())
        self.assertEqual(plan.fallback.kind, FALLBACK_FULL)

    def test_empty_selection_backstops_to_full(self) -> None:
        # Defense-in-depth: even if some resolution path produced an empty
        # selection while looking "mappable", the aggregate never returns
        # selected with nothing to run.
        plan = resolve_impact(
            ["src/mozyo_bridge/e_110_execution_platform/__init__.py"],
            test_files=TEST_FILES,
        )
        # __init__ is "other" -> unmapped -> full (covered), but assert the
        # aggregate never yields selected+empty.
        self.assertFalse(
            plan.recommendation == "selected" and not plan.selected_tests
        )

    def test_empty_change_set_recommends_full(self) -> None:
        plan = resolve_impact([], test_files=TEST_FILES)
        self.assertEqual(plan.recommendation, "full")
        self.assertEqual(plan.selected_tests, ())
        self.assertEqual(plan.fallback.kind, FALLBACK_FULL)

    def test_selected_tests_dedup_direct_before_neighbor(self) -> None:
        plan = resolve_impact(
            [
                "src/mozyo_bridge/e_110_execution_platform/"
                "f_140_delegated_coordinator_nested_handoff/domain/delegation_route_planner.py",
                "src/mozyo_bridge/e_110_execution_platform/"
                "f_140_delegated_coordinator_nested_handoff/domain/grandchild_dispatch.py",
            ],
            test_files=TEST_FILES,
        )
        # Both direct tests appear, exactly once each.
        self.assertEqual(len(plan.selected_tests), len(set(plan.selected_tests)))
        self.assertIn(
            "tests/unit/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff/test_delegation_route_planner.py",
            plan.selected_tests,
        )
        self.assertIn(
            "tests/unit/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff/test_grandchild_dispatch.py",
            plan.selected_tests,
        )
        self.assertEqual(plan.recommendation, "selected")

    def test_as_dict_is_json_safe_shape(self) -> None:
        plan = resolve_impact(["README.md"], test_files=TEST_FILES)
        payload = plan.as_dict()
        self.assertEqual(payload["recommendation"], "full")
        self.assertEqual(payload["fallback"]["kind"], FALLBACK_FULL)
        self.assertIsInstance(payload["resolutions"], list)
        self.assertIsInstance(payload["resolutions"][0]["direct_tests"], list)


class ListTestFilesTest(unittest.TestCase):
    def test_lists_test_files_relative_sorted_skips_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "tests" / "unit" / "e_110_execution_platform"
            d.mkdir(parents=True)
            (d / "test_b.py").write_text("")
            (d / "test_a.py").write_text("")
            (d / "helper.py").write_text("")  # not a test_*.py
            cache = d / "__pycache__"
            cache.mkdir()
            (cache / "test_cached.py").write_text("")
            found = list_test_files(root)
        self.assertEqual(
            found,
            (
                "tests/unit/e_110_execution_platform/test_a.py",
                "tests/unit/e_110_execution_platform/test_b.py",
            ),
        )

    def test_missing_tests_dir_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(list_test_files(Path(tmp)), ())


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "checkout", "-q", "-b", "main")


class GitChangedPathsSinceTest(unittest.TestCase):
    """The CI counterpart to working-tree change derivation (#12753).

    The merge-base (three-dot) diff lists what the branch ADDED since ``base``,
    not commits that landed on ``base`` after the branch started, and applies
    the same skip filtering as the working-tree path.
    """

    def test_three_dot_diff_lists_only_branch_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            (root / "base.txt").write_text("base")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "base")

            # A commit lands on main AFTER the feature branch forks.
            _git(root, "checkout", "-q", "-b", "feature")
            (root / "feature.py").write_text("x = 1")
            sub = root / "src" / "pkg"
            sub.mkdir(parents=True)
            (sub / "mod.py").write_text("y = 2")
            (root / "junk.pyc").write_text("")  # skipped suffix
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "feature work")

            _git(root, "checkout", "-q", "main")
            (root / "unrelated_on_main.txt").write_text("z")
            _git(root, "add", "-A")
            _git(root, "commit", "-q", "-m", "later on main")
            _git(root, "checkout", "-q", "feature")

            changed = git_changed_paths_since(root, "main")

        self.assertIn("feature.py", changed)
        self.assertIn("src/pkg/mod.py", changed)
        # Branch did not touch main's later commit, and .pyc is filtered.
        self.assertNotIn("unrelated_on_main.txt", changed)
        self.assertNotIn("junk.pyc", changed)


class CommandHandlerTest(unittest.TestCase):
    def _run(self, **kwargs) -> tuple[int, str]:
        args = argparse.Namespace(
            repo=None,
            paths=kwargs.get("paths", []),
            staged=False,
            all_changed=False,
            base=kwargs.get("base", None),
            format=kwargs.get("format", "text"),
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_tests_resolve(args)
        return rc, buf.getvalue()

    def test_base_routes_through_merge_base_diff(self) -> None:
        # With no explicit PATHS, --base derives changed paths from the
        # merge-base diff (the CI quick-lane entry point), not the working tree.
        captured: dict[str, object] = {}

        def fake_since(repo_root, base):
            captured["base"] = base
            return ["README.md"]

        original = commands_test_impact.git_changed_paths_since
        commands_test_impact.git_changed_paths_since = fake_since
        try:
            rc, out = self._run(base="origin/main", format="targets")
        finally:
            commands_test_impact.git_changed_paths_since = original

        self.assertEqual(rc, 0)
        self.assertEqual(captured["base"], "origin/main")
        # README.md is unmapped -> fail-closed full -> discover form.
        self.assertEqual(out.strip().splitlines(), ["discover", "-s", "tests"])

    def test_targets_format_prints_selected(self) -> None:
        rc, out = self._run(
            paths=[
                "src/mozyo_bridge/e_150_quality_architecture/"
                "f_130_module_health/domain/module_health.py"
            ],
            format="targets",
        )
        self.assertEqual(rc, 0)
        # The repo's own module-health test should resolve as a direct target.
        self.assertIn("test_module_health.py", out)

    def test_targets_format_full_is_unittest_runner_ready(self) -> None:
        # Regression (Codex j#67568 finding 1): a bare `tests` dir is not a
        # valid `python -m unittest` argument and runs nothing. The full
        # fallback must emit the discover form so the documented pipe works.
        rc, out = self._run(paths=["README.md"], format="targets")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip().splitlines(), ["discover", "-s", "tests"])

    def test_json_format_emits_recommendation(self) -> None:
        rc, out = self._run(paths=["README.md"], format="json")
        self.assertEqual(rc, 0)
        self.assertIn('"recommendation": "full"', out)

    def test_text_format_human_readable(self) -> None:
        rc, out = self._run(paths=["README.md"], format="text")
        self.assertEqual(rc, 0)
        self.assertIn("recommendation: full", out)
        self.assertIn("fallback[full]", out)


if __name__ == "__main__":
    unittest.main()
