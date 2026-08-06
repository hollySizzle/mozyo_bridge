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
import site
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
        # A task-local HOME for the hook's children (#14757 j#100496). The hook
        # runs `tests run`, whose `ambient_homes()` always guards the operator
        # default `~/.mozyo_bridge`. Inheriting the real HOME made these tests fail
        # whenever the operator's cockpit wrote during the run -- a verdict that
        # depended on unrelated activity. Production still guards every ambient
        # home; only the home a TEST's child resolves is a fixture.
        self.fake_home = self.repo / "fake-operator-account"
        (self.fake_home / ".mozyo_bridge").mkdir(parents=True)
        self.fixture_home = self.repo / "fixture-operator-home"
        self.fixture_home.mkdir()
        self.env = dict(
            os.environ,
            HOME=str(self.fake_home),
            # See the note in the runner tests: a stand-in HOME loses user
            # site-packages unless PYTHONUSERBASE names the real one.
            PYTHONUSERBASE=site.getuserbase(),
            # Item 5: name the fixture home explicitly. Relying only on the
            # synthetic HOME would leave the explicit spelling unpinned, and the
            # deny/guard set covers both spellings.
            MOZYO_BRIDGE_HOME=str(self.fixture_home),
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

    def test_focused_tests_run_through_the_isolated_runner(self) -> None:
        """Focused runs go through `tests run`, not bare unittest (#14757).

        A focused run reaches the operator's shared mozyo-bridge home just as a
        full one does — #14757 j#94599 recorded a focused run migrating the shared
        store v8 -> v9 — so the hook must not invoke `python -m unittest` directly
        when the CLI can isolate it.
        """
        self._stage(
            "tests/test_ok.py",
            "import unittest\n\n\n"
            "class OkTest(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
        )
        proc = self._hook()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("-- unchanged", combined)
        self.assertIn("refused write attempts: none", combined)
        self.assertNotIn("NON-ISOLATED", combined)

    def test_a_stale_cli_falls_back_to_the_repo_source_cli_and_stays_isolated(
        self,
    ) -> None:
        """A stale installed CLI must not cost isolation (#14757 j#100408 finding_3).

        The hook prefers an installed `mozyo-bridge` on PATH over this repo's source
        (#13079), so the resolved CLI can predate `tests run`. R1 fell back to a bare
        `python -m unittest` with a warning; the review rejected that, because a
        warning does not prevent a shared-home mutation. The hook now reaches for the
        repo's own committed CLI, which necessarily has the subcommand, and the run
        stays isolated.
        """
        stale = self.repo / "stale-mozyo"
        stale.write_text(
            "#!/bin/sh\n"
            "case \"$1 $2\" in\n"
            "  'tests resolve') printf 'tests/test_ok.py\\n'; exit 0 ;;\n"
            "  'tests run') echo \"error: invalid choice: 'run'\" >&2; exit 2 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        stale.chmod(0o755)
        self._stage(
            "tests/test_ok.py",
            "import unittest\n\n\n"
            "class OkTest(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
        )
        env = dict(self.env, MOZYO_BRIDGE_CMD=str(stale))
        proc = _run(["sh", str(HOOK)], self.repo, env=env)
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("trying this repo's source CLI", combined)
        # It ran isolated, not un-isolated: the fence reported in.
        self.assertIn("-- unchanged", combined)
        self.assertNotIn("NON-ISOLATED", combined)

    def test_with_no_isolating_cli_available_at_all_the_hook_fails_closed(self) -> None:
        """The end of the line is a failed hook, never an un-isolated run.

        The repo-source fallback is reached through the hook's own location, so this
        copies the hook somewhere with no `../src/mozyo_bridge` beside it and hands it
        a stale CLI. With no way left to isolate, the hook must block the commit
        rather than run the tests anyway (j#94599 / j#100402 item 3).
        """
        detached = self.repo / "detached-hook.sh"
        detached.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
        stale = self.repo / "stale-mozyo"
        stale.write_text(
            "#!/bin/sh\n"
            "case \"$1 $2\" in\n"
            "  'tests resolve') printf 'tests/test_ok.py\\n'; exit 0 ;;\n"
            "  'tests run') echo \"error: invalid choice: 'run'\" >&2; exit 2 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        stale.chmod(0o755)
        self._stage(
            "tests/test_ok.py",
            "import unittest\n\n\n"
            "class OkTest(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
        )
        env = dict(self.env, MOZYO_BRIDGE_CMD=str(stale))
        proc = _run(["sh", str(detached)], self.repo, env=env)
        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn("cannot be isolated", combined)
        self.assertNotIn("NON-ISOLATED", combined)

    def test_the_repo_source_cli_wins_over_an_installed_one_on_path(self) -> None:
        """This repo's source beats whatever is installed (#14757 j#100490 item 3).

        The hook used to prefer any `mozyo-bridge` on PATH that merely answered
        `tests run --help`. An older build satisfies that probe while implementing
        an earlier isolation contract, so the hook would report an isolated run
        under rules this very commit is changing. The source beside the script is by
        construction the contract being committed.
        """
        older = self.repo / "bin"
        older.mkdir()
        marker = self.repo / "older-was-used"
        installed = older / "mozyo-bridge"
        installed.write_text(
            "#!/bin/sh\n"
            f"touch {marker}\n"
            "case \"$1 $2\" in\n"
            "  'tests resolve') printf 'tests/test_ok.py\\n'; exit 0 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        installed.chmod(0o755)
        self._stage(
            "tests/test_ok.py",
            "import unittest\n\n\n"
            "class OkTest(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
        )
        env = dict(self.env, PATH=f"{older}:{os.environ['PATH']}")
        env.pop("MOZYO_BRIDGE_CMD", None)
        proc = _run(["sh", str(HOOK)], self.repo, env=env)
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertFalse(
            marker.exists(), "the installed CLI on PATH was preferred over repo source"
        )
        self.assertIn("-- unchanged", combined)

    def test_an_explicit_operator_override_still_wins(self) -> None:
        """The precedence change must not take the override away from the operator."""
        chosen = self.repo / "chosen-mozyo"
        marker = self.repo / "override-was-used"
        chosen.write_text(
            "#!/bin/sh\n"
            f"touch {marker}\n"
            "case \"$1 $2\" in\n"
            "  'tests resolve') printf '\\n'; exit 0 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        chosen.chmod(0o755)
        proc = _run(
            ["sh", str(HOOK)], self.repo, env=dict(self.env, MOZYO_BRIDGE_CMD=str(chosen))
        )
        self.assertTrue(marker.exists(), "MOZYO_BRIDGE_CMD was ignored")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_clean_stage_with_no_targets_passes(self) -> None:
        # Nothing staged -> the resolver fail-closes to full (empty change
        # set); the hook surfaces it and passes without running anything.
        proc = self._hook()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
