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

- the **byte-invariance** of every pre-existing message, asserted against golden text
  reproduced literally from base `59526e7a` (review j#105978 finding_2: an equivalence
  between two calls of the same new implementation passes even when the legacy wording
  changes, so only a pre-change literal actually pins the contract), and
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
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_adoption import (  # noqa: E402,E501
    nested_adoption_marker,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.commands_docs_scaffold import (  # noqa: E402,E501
    cmd_scaffold_apply,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.scaffold_target_note import (  # noqa: E402,E501
    nested_target_warning,
)
from mozyo_bridge.shared.errors import CommandAbort  # noqa: E402
from mozyo_bridge.shared.paths import (  # noqa: E402
    find_repo_root,
    workspace_adoption_marker,
)


def _git_repo(base: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    return base


class NestedMarkerIsFoundFromStartUpToRootTest(unittest.TestCase):
    def _tmp(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name).resolve()

    def test_a_marker_at_start_itself_is_reported_start_is_inclusive(self) -> None:
        # The live reproduction: the CWD IS the freshly scaffolded subdirectory,
        # so `start` must be included in the scan (review j#105978 finding_3).
        root = self._tmp()
        nested = root / "Source" / "rails"
        (nested / ".mozyo-bridge").mkdir(parents=True)
        (nested / ".mozyo-bridge" / "scaffold.json").write_text("{}")

        found = nested_adoption_marker(nested, root)

        self.assertEqual((nested, ".mozyo-bridge/scaffold.json"), found)

    def test_the_root_itself_is_excluded(self) -> None:
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


#: The refusal wordings as they stood on base `59526e7a`, BEFORE this issue —
#: reproduced literally from that commit's `launch_adoption_gate.py` with the
#: fixture paths below substituted. Golden text, not a call into the current
#: implementation: an equivalence between two calls of the same code passes even
#: when the legacy wording changes, which is exactly the regression these exist
#: to catch (review j#105978 finding_2).
_GOLDEN_UNADOPTED_REFUSAL = (
    "bare `mozyo` resolved repo root /myapp, which is not an "
    "adopted mozyo workspace (no .mozyo-bridge/config.yaml or "
    "scaffold/workspace marker); refusing to start agent sessions "
    "there. cd into an adopted project root, or adopt this project "
    "first: `mozyo-bridge scaffold apply <preset> --target "
    "<project_root>` (see `mozyo-bridge scaffold --help` for "
    "presets), then re-run `mozyo`."
)
_GOLDEN_HOME_REFUSAL = (
    "bare `mozyo` resolved repo root to the home directory "
    "/home/someone; refusing to start agent sessions there (an "
    "unadopted directory resolves up to incidental home markers). "
    "cd into an adopted project root, or adopt the project first: "
    "`mozyo-bridge scaffold apply <preset> --target <project_root>`."
)


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

    def test_without_a_nested_marker_the_wording_is_the_base_bytes(self) -> None:
        # Against the pre-change golden, for both spellings of "no nested marker".
        self.assertEqual(
            _GOLDEN_UNADOPTED_REFUSAL, adoption_refusal(self.ROOT, None, self.HOME)
        )
        self.assertEqual(
            _GOLDEN_UNADOPTED_REFUSAL,
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

    def test_home_is_still_refused_with_the_base_bytes(self) -> None:
        # The home refusal outranks the nested note and keeps its exact wording,
        # with or without a nested marker in hand.
        self.assertEqual(
            _GOLDEN_HOME_REFUSAL,
            adoption_refusal(self.HOME, None, self.HOME, nested=self.NESTED),
        )
        self.assertEqual(
            _GOLDEN_HOME_REFUSAL, adoption_refusal(self.HOME, None, self.HOME)
        )


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

    def _assert_pre_write_stdout_is_empty(self, output: str) -> None:
        # The byte-invariance claim for the untriggered cases (review j#105978
        # finding_2): before this issue, NOTHING preceded the write output, so
        # everything up to the first write line (or up to the abort, when the
        # environment has no installed preset) must still be the empty string —
        # not merely "does not contain the note".
        for marker in ("would write:", "wrote:"):
            index = output.find(marker)
            if index != -1:
                self.assertEqual("", output[:index])
                return
        self.assertEqual("", output)

    def test_the_git_root_target_output_carries_no_note(self) -> None:
        root = _git_repo(self._tmp())

        self._assert_pre_write_stdout_is_empty(self._run(root))

    def test_a_non_git_target_carries_no_note(self) -> None:
        plain = self._tmp()

        self._assert_pre_write_stdout_is_empty(self._run(plain))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
