"""Real-``git`` tests for the #13686 live adapter's two destructive primitives.

R6 review j#96391 finding 6 asked for these specifically, and the reason is on the record:
findings 1 and 3 were both properties of what ``git`` actually does, and the recording fake
could not have caught either. A fake answers what it was told to answer — it cannot tell you
that ``git update-ref -d`` will happily delete a branch a linked worktree is standing on, or
that ``git merge`` will take whatever the checked-out tip happens to be as its first parent.

So these two facts are pinned against a real binary in a temp repository:

- **the merge's target parent** must be the commit the action measured on the remote, not the
  dedicated worktree's local tip (finding 1: a worktree carrying one extra unreviewed commit
  produced a merge containing it, and the push was accepted because it was still a
  fast-forward);
- **the branch delete** must refuse while any worktree holds the branch (finding 3: measured,
  ``update-ref -d`` deleted the ref and left that worktree's ``HEAD`` unresolvable, while
  ``git branch -D`` refuses atomically).

Hermetic: every repository is created under a fresh ``TemporaryDirectory`` and no remote is
contacted. The tests skip when ``git`` is unavailable rather than failing, so the suite stays
runnable where the binary is not installed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_ops import (
    LiveAutoIntegrationGitOperations,
)

_GIT = shutil.which("git")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "actuator@example.invalid")
    _git(repo, "config", "user.name", "actuator")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@unittest.skipIf(_GIT is None, "git is not available on PATH")
class MergeParentBindingTest(unittest.TestCase):
    """The merge's first parent is the measured remote target, or there is no merge."""

    def test_a_dedicated_worktree_carrying_extra_work_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            base = _commit(repo, "a.txt", "base")

            # The lane's reviewed work.
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            # The primary checkout parks off `main` so the dedicated worktree can hold it —
            # git refuses to check one branch out in two worktrees, which is the same reason
            # the lane's own worktree must never hold the target.
            _git(repo, "checkout", "-q", "-b", "parking")

            # A dedicated integration worktree that has drifted ahead of the remote target
            # with a commit nobody reviewed.
            dedicated = root / "dedicated"
            _git(repo, "worktree", "add", "-q", str(dedicated), "main")
            unreviewed = _commit(dedicated, "sneaky.txt", "never reviewed")
            self.assertNotEqual(unreviewed, base)

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            result = operations.apply_merge(
                source_head=source,
                target_ref="main",
                integration_worktree=str(dedicated),
                # What the action measured on the remote — the pre-drift commit.
                expected_target_head=base,
            )

            self.assertTrue(result.conflicted, result.detail)
            self.assertEqual(result.integration_head, "")
            self.assertIn("unverified parent", result.detail)
            # Nothing was merged: the dedicated worktree still sits on its own commit.
            self.assertEqual(_git(dedicated, "rev-parse", "HEAD"), unreviewed)

    def test_a_dedicated_worktree_at_the_expected_target_merges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            base = _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "-b", "parking")

            dedicated = root / "dedicated"
            _git(repo, "worktree", "add", "-q", str(dedicated), "main")

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            result = operations.apply_merge(
                source_head=source,
                target_ref="main",
                integration_worktree=str(dedicated),
                expected_target_head=base,
            )

            self.assertFalse(result.conflicted, result.detail)
            self.assertEqual(len(result.integration_head), 40)
            # The merge sits on the expected target and contains the reviewed source.
            parents = _git(dedicated, "rev-list", "--parents", "-n", "1", "HEAD").split()
            self.assertIn(base, parents)
            self.assertIn(source, parents)


@unittest.skipIf(_GIT is None, "git is not available on PATH")
class BranchDeleteEnforcementTest(unittest.TestCase):
    """A branch a worktree is standing on is not deletable, and git is what enforces it."""

    def test_a_branch_held_by_a_linked_worktree_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "branch", "lane")
            held = root / "held"
            _git(repo, "worktree", "add", "-q", str(held), "lane")
            tip = _git(repo, "rev-parse", "lane")

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            self.assertFalse(
                operations.delete_local_branch(branch="lane", expected_tip=tip)
            )
            # The ref survives and the worktree's HEAD still resolves — which is exactly what
            # `git update-ref -d` did NOT preserve (j#96391 finding 3, measured).
            self.assertEqual(_git(repo, "rev-parse", "lane"), tip)
            self.assertEqual(_git(held, "rev-parse", "HEAD"), tip)

    def test_the_branch_is_deleted_once_no_worktree_holds_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "branch", "lane")
            held = root / "held"
            _git(repo, "worktree", "add", "-q", str(held), "lane")
            tip = _git(repo, "rev-parse", "lane")
            _git(repo, "worktree", "remove", str(held))

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            self.assertTrue(
                operations.delete_local_branch(branch="lane", expected_tip=tip)
            )
            self.assertNotEqual(
                subprocess.run(
                    ["git", "rev-parse", "--verify", "--quiet", "refs/heads/lane"],
                    cwd=repo,
                    capture_output=True,
                ).returncode,
                0,
            )

    def test_a_tip_that_moved_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            stale = _git(repo, "rev-parse", "lane")
            _commit(repo, "b.txt", "moved")  # the branch advanced since the action was formed
            _git(repo, "checkout", "-q", "main")

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            self.assertFalse(
                operations.delete_local_branch(branch="lane", expected_tip=stale)
            )
            self.assertTrue(_git(repo, "rev-parse", "lane"))


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
