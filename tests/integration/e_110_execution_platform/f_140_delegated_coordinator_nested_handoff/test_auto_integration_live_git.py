"""Real-``git`` tests for the #13686 live adapter (Redmine #13686).

R6 review j#96391 finding 6 asked for these specifically, and the reason is on the record:
that round's findings were both properties of what ``git`` actually does, and the recording
fake could not have caught either. A fake answers what it was told to answer — it cannot tell
you that ``git merge`` will take whatever the checked-out tip happens to be as its first
parent, or what a ref-deleting primitive does to a worktree standing on that ref.

What is pinned against a real binary here is **the merge's target parent**: it must be the
commit the action measured on the remote, not the dedicated worktree's local tip (a worktree
carrying one extra unreviewed commit produced a merge containing it, and the push was
accepted because it was still a fast-forward).

The branch-delete tests this module also carried are gone with the operation. R7 review
j#96396 finding 1 reproduced the residual its docstring admitted — a commit landing between
the tip verification and the delete was destroyed while the step recorded ``done`` — and the
delete was retired rather than guarded again, because no single ``git`` invocation enforces
both its tip condition and its no-holding-worktree condition. :class:`NoRefDeleteTest` pins
the absence against the same real binary the delete used to run through.

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
class NoRefDeleteTest(unittest.TestCase):
    """The adapter deletes no ref, and the reason is measurable on the binary itself."""

    def test_the_adapter_exposes_no_ref_deleting_operation(self) -> None:
        for gone in ("delete_local_branch", "delete_remote_branch"):
            self.assertFalse(hasattr(LiveAutoIntegrationGitOperations, gone), gone)

    def test_no_git_primitive_enforces_both_delete_conditions_at_once(self) -> None:
        """The measurement the retirement rests on, kept executable rather than asserted.

        If a future git makes one invocation enforce both conditions — the ref still points
        at the recorded tip AND no worktree holds it — this test is what will notice, and the
        ruling can be revisited on evidence instead of on memory.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # (a) `update-ref -d <ref> <tip>` compare-and-swaps the tip, and deletes the ref
            #     out from under a worktree that is standing on it.
            repo = root / "cas"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "branch", "lane")
            held = root / "held"
            _git(repo, "worktree", "add", "-q", str(held), "lane")
            tip = _git(repo, "rev-parse", "lane")
            cas = subprocess.run(
                ["git", "update-ref", "-d", "refs/heads/lane", tip],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(cas.returncode, 0, cas.stderr)
            self.assertNotEqual(
                subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=held, capture_output=True
                ).returncode,
                0,
                "the held worktree's HEAD should have been orphaned by the delete",
            )

            # (b) `branch -D` refuses the held branch atomically, and takes no tip constraint:
            #     a second argument is read as another BRANCH NAME, not as an expected tip.
            repo = root / "force"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "branch", "lane")
            held = root / "held2"
            _git(repo, "worktree", "add", "-q", str(held), "lane")
            forced = subprocess.run(
                ["git", "branch", "-D", "lane"], cwd=repo, capture_output=True, text=True
            )
            self.assertNotEqual(forced.returncode, 0)
            self.assertIn("used by worktree", forced.stderr)
            _git(repo, "worktree", "remove", str(held))
            stale = "0" * 40
            with_tip = subprocess.run(
                ["git", "branch", "-D", "lane", stale],
                cwd=repo, capture_output=True, text=True,
            )
            # The branch is gone despite the "expected tip" being nonsense — it was never a
            # constraint. What failed is the attempt to delete a branch NAMED by that SHA.
            self.assertNotEqual(
                subprocess.run(
                    ["git", "rev-parse", "--verify", "--quiet", "refs/heads/lane"],
                    cwd=repo, capture_output=True,
                ).returncode,
                0,
            )
            self.assertIn(stale, with_tip.stderr)

            # (c) A transaction cannot combine the two: one ref, one update.
            repo = root / "txn"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "branch", "lane")
            tip = _git(repo, "rev-parse", "lane")
            txn = subprocess.run(
                ["git", "update-ref", "--stdin"],
                cwd=repo,
                input=(
                    f"start\nverify refs/heads/lane {tip}\n"
                    f"delete refs/heads/lane {tip}\nprepare\ncommit\n"
                ),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(txn.returncode, 0)
            self.assertIn("multiple updates", txn.stderr)
            self.assertEqual(_git(repo, "rev-parse", "lane"), tip)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
