"""Opt-in pre-commit focused-verification hook behavior (Redmine #13079).

Runs the real ``scripts/pre-commit-focused.sh`` inside a throwaway fixture git
repo and pins its contract: staged whitespace problems block the commit, a
focused resolver selection runs (and a red focused test blocks), a fail-closed
``full`` recommendation is surfaced but NEVER executed by the hook (the full
suite stays a pre-push / CI duty), and the docs audit step is skipped in a repo
without a docs catalog. The adoption boundary itself (opt-in only, no
auto-install) is documentation — see
``vibes/docs/logics/pre-commit-focused-verification.md``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HOOK = ROOT / "scripts" / "pre-commit-focused.sh"


def _run(argv, cwd, env=None):
    return subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


class PreCommitHookTest(unittest.TestCase):
    """Fixture-repo runs of the committed hook script."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        for argv in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "hook-test@example.invalid"],
            ["git", "config", "user.name", "hook test"],
        ):
            proc = _run(argv, self.repo)
            if proc.returncode != 0:
                self.skipTest(f"git unavailable: {proc.stderr.strip()}")
        self.env = dict(
            os.environ,
            MOZYO_BRIDGE_CMD=f"{sys.executable} -m mozyo_bridge",
            MOZYO_PYTHON=sys.executable,
            PYTHONPATH=str(ROOT / "src"),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stage(self, rel: str, content: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _run(["git", "add", rel], self.repo)

    def _hook(self):
        return _run(["sh", str(HOOK)], self.repo, env=self.env)

    def test_staged_whitespace_problem_blocks_the_commit(self) -> None:
        self._stage("notes.py", "x = 1  \n")  # trailing whitespace
        proc = self._hook()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAIL", proc.stdout + proc.stderr)

    def test_focused_selection_runs_the_staged_test(self) -> None:
        self._stage(
            "tests/test_ok.py",
            "import unittest\n\n\n"
            "class OkTest(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
        )
        proc = self._hook()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("running focused tests", proc.stdout)
        self.assertIn("tests/test_ok.py", proc.stdout)
        self.assertIn("OK", proc.stdout)
        # No docs catalog in the fixture -> the docs step is skipped, not run.
        self.assertIn("skipping docs audit-impact", proc.stdout)

    def test_red_focused_test_blocks_the_commit(self) -> None:
        self._stage(
            "tests/test_bad.py",
            "import unittest\n\n\n"
            "class BadTest(unittest.TestCase):\n"
            "    def test_bad(self):\n"
            "        self.fail('red')\n",
        )
        proc = self._hook()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("focused tests failed", proc.stdout + proc.stderr)

    def test_full_recommendation_is_surfaced_but_never_run(self) -> None:
        # An unmapped path fail-closes the resolver to a full recommendation;
        # the hook must NOT run the full suite (boundary: full stays a
        # pre-push / CI duty) and must say so while letting the commit pass.
        self._stage("config.toml", "[x]\ny = 1\n")
        proc = self._hook()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("recommends the FULL suite", proc.stdout)
        self.assertIn("NOT running it in the hook", proc.stdout)
        self.assertNotIn("running focused tests", proc.stdout)

    def test_clean_stage_with_no_targets_passes(self) -> None:
        # Nothing staged -> the resolver fail-closes to full (empty change
        # set); the hook surfaces it and passes without running anything.
        proc = self._hook()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
