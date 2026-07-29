"""Legacy mirror sync wrapper CLI operator workflow (Redmine #13483 / #14580).

Behavior-preserving move out of the 3,865-line
`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
per the #14660 characterization (§5.5 移設先 module の確定) and the placement
ruling in `vibes/docs/logics/tests-placement-discovery-policy.md`
`## #14660 legacy mirror family 裁定`. Test bodies are unchanged; only the
module frame and import paths moved (Redmine #14666, T1 move-only).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from tests.support.legacy_mirror_tree_fixture import (  # noqa: E402
    SYNC_SCRIPT_PATH,
    _MirrorTreeFixture,
)


class LegacyMirrorWrapperCliTest(_MirrorTreeFixture):
    """The `scripts/` wrapper: operator-facing contract, black-box.

    The wrapper carries no mirror logic; it `exec`s the Python CLI. These
    cases drive it the way an operator (and `release check drift`) does, so
    they cross the shell-module -> python-CLI boundary end to end."""

    def _run(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_check_and_sync_round_trip(self) -> None:
        repo = self._stage_with_wrapper()
        self.assertEqual(0, self._run(repo, "--check").returncode)
        canonical = self._source(repo) / "workflow.md"
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\nEDIT\n", encoding="utf-8")
        self.assertEqual(1, self._run(repo, "--check").returncode)
        synced = self._run(repo)
        self.assertEqual(0, synced.returncode, msg=synced.stderr)
        self.assertIn("synced legacy project skill mirror", synced.stdout)
        self.assertEqual(0, self._run(repo, "--check").returncode)

    def test_check_reports_a_violation_and_writes_nothing(self) -> None:
        repo = self._stage_with_wrapper()
        (self._mirror(repo) / "unpinned.txt").write_text("smuggled\n", encoding="utf-8")
        result = self._run(repo, "--check")
        self.assertEqual(1, result.returncode)
        self.assertNotIn("is up to date", result.stdout)
        self.assertIn("unpinned_entry", result.stderr)
        self.assertTrue((self._mirror(repo) / "unpinned.txt").exists())

    def test_help_exits_zero(self) -> None:
        repo = self._stage_with_wrapper()
        result = self._run(repo, "--help")
        self.assertEqual(0, result.returncode)
        self.assertIn("--check", result.stdout)

    def test_unknown_argument_exits_64(self) -> None:
        repo = self._stage_with_wrapper()
        result = self._run(repo, "--force")
        self.assertEqual(64, result.returncode)
        self.assertIn("unknown argument", result.stderr)

    def test_repo_cannot_be_redirected_by_operator_argv(self) -> None:
        """j#90418 R6-F2. The wrapper passed `--repo <own root>` and then
        appended `"$@"`, and the parser took the last value — so an operator
        could audit, and in default mode write, a different checkout entirely.
        """
        tree_a = self._stage_with_wrapper()
        tree_b = self._stage_with_wrapper()
        smuggled = self._mirror(tree_b) / "unpinned.txt"
        smuggled.write_text("smuggled\n", encoding="utf-8")

        for args in (["--check", "--repo", str(tree_b)], ["--repo", str(tree_b)]):
            with self.subTest(args=args):
                result = self._run(tree_a, *args)
                self.assertEqual(64, result.returncode)
                self.assertIn("unknown argument: --repo", result.stderr)
                self.assertNotIn("unpinned.txt", result.stdout + result.stderr)

        self.assertTrue(smuggled.exists(), "tree B was modified from tree A")
        self.assertEqual(0, self._run(tree_a, "--check").returncode)

    def test_repo_env_is_overwritten_by_the_wrapper(self) -> None:
        """The internal channel must not be hijackable from the environment."""
        tree_a = self._stage_with_wrapper()
        tree_b = self._stage_with_wrapper()
        (self._mirror(tree_b) / "unpinned.txt").write_text("smuggled\n", encoding="utf-8")

        env = dict(os.environ)
        env["MOZYO_LEGACY_MIRROR_REPO_ROOT"] = str(tree_b)
        result = subprocess.run(
            ["sh", str(tree_a / "scripts" / SYNC_SCRIPT_PATH.name), "--check"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertNotIn("unpinned.txt", result.stdout + result.stderr)

    def test_wrapper_targets_its_own_repo_not_the_cwd(self) -> None:
        """`release check drift` runs the staged tree's wrapper; it must check
        that tree, not whichever repo the process happens to sit in."""
        repo = self._stage_with_wrapper()
        (self._mirror(repo) / "unpinned.txt").write_text("smuggled\n", encoding="utf-8")
        result = subprocess.run(
            ["sh", str(repo / "scripts" / SYNC_SCRIPT_PATH.name), "--check"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=120,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("unpinned.txt", result.stderr)


if __name__ == "__main__":
    unittest.main()
