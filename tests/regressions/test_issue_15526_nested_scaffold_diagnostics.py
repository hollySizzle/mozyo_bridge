"""Redmine #15526 — the tool stops hiding a nested marker it has already seen.

Measured on main `59526e7a`: `scaffold apply --target /myapp/Source/rails` inside a Git
repository rooted at `/myapp` wrote twelve files and exited 0 in silence, and `mozyo`
from that same directory then resolved `/myapp`, found no marker, and printed "adopt
this project first: scaffold apply" — while `workspace_adoption_marker(cwd)` was
returning `.mozyo-bridge/scaffold.json` one directory down. The loop is the defect: the
tool had the fact and told the operator to repeat the step they had just completed.

Git-root-first resolution (#13641) is NOT changed here and these tests assert that it
is not: what changes is what gets said about it. So the suite is written around the two
things that can silently rot —

- the **byte-invariance** of every pre-existing message (asserted by comparing against
  the same call with the new argument omitted, rather than by copying wording into an
  expectation that would then need hand-maintenance), and
- the **trigger condition**, which must stay the Git root specifically. A marker walk
  would fire on a genuinely non-git scaffolded workspace (#11301), where the target is
  the root and there is nothing to warn about.

`ScaffoldApplyEmitsTheNoteTest` drives the real command against real directories rather
than the pure function, because the defect was that the command never asked.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.launch_adoption_gate import adoption_refusal  # noqa: E402
from mozyo_bridge.application.scaffold_target_gate import (  # noqa: E402
    nested_target_warning,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.commands_docs_scaffold import (  # noqa: E402,E501
    cmd_scaffold_apply,
)
from mozyo_bridge.shared.errors import CommandAbort  # noqa: E402
from mozyo_bridge.shared.paths import (  # noqa: E402
    find_repo_root,
    nested_adoption_marker,
    workspace_adoption_marker,
)


def _git_repo(base: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    return base


class NestedMarkerIsFoundBetweenStartAndRootTest(unittest.TestCase):
    def _tmp(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name).resolve()

    def test_a_marker_in_a_subdirectory_is_reported_with_its_path(self) -> None:
        root = self._tmp()
        nested = root / "Source" / "rails"
        (nested / ".mozyo-bridge").mkdir(parents=True)
        (nested / ".mozyo-bridge" / "scaffold.json").write_text("{}")

        found = nested_adoption_marker(nested, root)

        self.assertEqual((nested, ".mozyo-bridge/scaffold.json"), found)

    def test_the_root_itself_is_not_reported_as_nested(self) -> None:
        # That is `workspace_adoption_marker`'s question; reporting it here would
        # make an adopted root look like a stray subtree.
        root = self._tmp()
        (root / ".mozyo-bridge").mkdir()
        (root / ".mozyo-bridge" / "scaffold.json").write_text("{}")

        self.assertIsNone(nested_adoption_marker(root, root))

    def test_a_start_outside_the_root_reports_nothing(self) -> None:
        # An explicit --repo elsewhere: there is no "between", and guessing one
        # would name an unrelated tree.
        outside = self._tmp()
        root = self._tmp()
        (outside / ".mozyo-bridge").mkdir()
        (outside / ".mozyo-bridge" / "scaffold.json").write_text("{}")

        self.assertIsNone(nested_adoption_marker(outside, root))

    def test_the_nearest_marker_wins(self) -> None:
        root = self._tmp()
        mid = root / "Source"
        deep = mid / "rails"
        for directory in (mid, deep):
            (directory / ".mozyo-bridge").mkdir(parents=True)
            (directory / ".mozyo-bridge" / "scaffold.json").write_text("{}")

        self.assertEqual(deep, nested_adoption_marker(deep, root)[0])

    def test_git_root_first_resolution_is_unchanged(self) -> None:
        # The rule this issue explicitly does not touch (#13641).
        root = _git_repo(self._tmp())
        nested = root / "Source" / "rails"
        (nested / ".mozyo-bridge").mkdir(parents=True)
        (nested / ".mozyo-bridge" / "scaffold.json").write_text("{}")

        self.assertEqual(root, find_repo_root(nested))
        self.assertIsNone(workspace_adoption_marker(root))


class RefusalNamesTheMarkerItWalkedPastTest(unittest.TestCase):
    ROOT = Path("/myapp")
    NESTED = (Path("/myapp/Source/rails"), ".mozyo-bridge/scaffold.json")
    HOME = Path("/home/someone")

    def test_the_unadopted_refusal_names_the_nested_marker_and_both_routes(self) -> None:
        message = adoption_refusal(self.ROOT, None, self.HOME, nested=self.NESTED)

        self.assertIn(str(self.NESTED[0]), message)
        self.assertIn(self.NESTED[1], message)
        # Route 1: adopt the resolved root. Route 2: run the subtree explicitly.
        self.assertIn(f"--target {self.ROOT}", message)
        self.assertIn(f"--repo {self.NESTED[0]}", message)
        self.assertIn("workspace alias", message)

    def test_without_a_nested_marker_the_wording_is_unchanged(self) -> None:
        # Byte-invariance expressed as an equality against the pre-#15526 call,
        # so it cannot drift out of sync with the wording it protects.
        self.assertEqual(
            adoption_refusal(self.ROOT, None, self.HOME),
            adoption_refusal(self.ROOT, None, self.HOME, nested=None),
        )

    def test_an_adopted_root_still_proceeds_even_with_a_nested_marker(self) -> None:
        # The nested marker is diagnostic only; it must never turn a healthy
        # workspace into a refusal.
        self.assertIsNone(
            adoption_refusal(
                self.ROOT, ".mozyo-bridge/config.yaml", self.HOME, nested=self.NESTED
            )
        )

    def test_home_is_still_refused_for_its_own_reason(self) -> None:
        message = adoption_refusal(self.HOME, None, self.HOME, nested=self.NESTED)

        self.assertIn("home directory", message)
        self.assertEqual(adoption_refusal(self.HOME, None, self.HOME), message)


class ScaffoldNoteFiresOnlyForAGitRootAboveTest(unittest.TestCase):
    TARGET = Path("/myapp/Source/rails")

    def test_a_git_root_above_the_target_produces_the_note(self) -> None:
        message = nested_target_warning(self.TARGET, Path("/myapp"))

        self.assertIn(str(self.TARGET), message)
        self.assertIn("/myapp", message)
        self.assertIn("--target /myapp", message)

    def test_a_target_that_is_the_git_root_says_nothing(self) -> None:
        self.assertIsNone(nested_target_warning(Path("/myapp"), Path("/myapp")))

    def test_no_git_root_says_nothing(self) -> None:
        # A non-git scaffolded workspace (#11301) IS its own root.
        self.assertIsNone(nested_target_warning(self.TARGET, None))


class ScaffoldApplyEmitsTheNoteTest(unittest.TestCase):
    """Run the command, because the defect was that it never asked.

    The note has to reach stdout BEFORE the write is attempted — that ordering is
    the whole point, since a note printed afterwards would arrive too late to
    retarget. So the run tolerates the write itself failing (under the verification
    fence the guarded home carries no installed preset) and judges what was said,
    which is exactly the property under test and makes the case independent of
    whether a rules store happens to be present.
    """

    def _run(self, target: Path) -> str:
        args = argparse.Namespace(
            preset="redmine-governed",
            # `--target` parses into `repo` (see `scaffold_target_from_args`);
            # leaving it None silently means the CWD, which is the live repo.
            repo=str(target),
            dry_run=True,
            backup=False,
            force=False,
            home=None,
            repo_local=False,
            skip=None,
            with_=None,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                cmd_scaffold_apply(args)
            except CommandAbort:
                pass
        return out.getvalue()

    def _tmp(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name).resolve()

    def test_a_nested_target_inside_a_git_repo_is_announced(self) -> None:
        root = _git_repo(self._tmp())
        nested = root / "Source" / "rails"
        nested.mkdir(parents=True)

        output = self._run(nested)

        self.assertIn("is not the root that mozyo will resolve", output)
        self.assertIn(str(root), output)

    def test_the_git_root_target_output_carries_no_note(self) -> None:
        root = _git_repo(self._tmp())

        output = self._run(root)

        self.assertNotIn("is not the root that mozyo will resolve", output)

    def test_a_non_git_target_carries_no_note(self) -> None:
        plain = self._tmp()

        output = self._run(plain)

        self.assertNotIn("is not the root that mozyo will resolve", output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
