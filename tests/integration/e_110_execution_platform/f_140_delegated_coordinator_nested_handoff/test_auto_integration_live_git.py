"""Real-``git`` tests for the #13686 live adapter (Redmine #13686).

R6 review j#96391 finding 6 asked for these specifically, and the reason is on the record:
that round's findings were both properties of what ``git`` actually does, and the recording
fake could not have caught either. A fake answers what it was told to answer — it cannot tell
you that ``git merge`` will take whatever the checked-out tip happens to be as its first
parent, or what a ref-deleting primitive does to a worktree standing on that ref.

What is pinned against a real binary here is that **the merge is built from objects and
touches no checkout**. It used to be performed inside a dedicated worktree, with that path's
identity established by an earlier probe; review j#96406 finding 1 reproduced a foreign lane's
clean checkout swapped onto the path between the probe and the merge being switched off its
own branch and having the merge commit built on it — and ``apply_merge`` returned
``conflicted=False``. A non-force push and an exact-SHA CI gate what *lands*; neither undoes a
checkout somebody else was standing in. :class:`WorktreeSwapRegressionTest` performs that same
swap and asserts the foreign checkout is untouched.

The destructive-operation tests this module also carried are gone with the operations
themselves. Three were withdrawn, each because the property that made it safe was established
in a *different* invocation from the one that acted — j#96396 finding 1 for the local branch
delete (a commit landing in the window was destroyed) and j#96401 finding 1 for the worktree
removal (a foreign lane's checkout swapped onto the measured path was removed).
:class:`NoDestructiveOperationTest` pins the absence, and keeps the measurements the
withdrawals rest on executable against the same real binary — so a future ``git`` that closes
either gap will say so rather than being remembered wrongly.

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
class ObjectLevelMergeTest(unittest.TestCase):
    """The merge is objects: the right parent, the right content, and nothing else moved."""

    def test_the_merge_sits_on_the_measured_target_and_moves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            base = _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            result = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=base
            )

            self.assertFalse(result.conflicted, result.detail)
            self.assertEqual(len(result.integration_head), 40)
            parents = _git(
                repo, "rev-list", "--parents", "-n", "1", result.integration_head
            ).split()
            self.assertEqual(parents[1], base, "the measured target must be the FIRST parent")
            self.assertEqual(parents[2], source)
            self.assertEqual(
                subprocess.run(
                    ["git", "cat-file", "-e", f"{result.integration_head}:lane.txt"],
                    cwd=repo,
                    capture_output=True,
                ).returncode,
                0,
            )
            # Nothing published, nothing switched, nothing dirtied.
            self.assertEqual(_git(repo, "rev-parse", "main"), base)
            self.assertEqual(_git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
            self.assertEqual(_git(repo, "status", "--porcelain"), "")

    def test_a_conflict_is_reported_and_still_moves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "a.txt", "lane version")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "a.txt", "target version")

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            result = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=target
            )

            self.assertTrue(result.conflicted)
            self.assertIn("merge conflicted", result.detail)
            self.assertEqual(result.integration_head, "")
            self.assertEqual(_git(repo, "rev-parse", "main"), target)
            self.assertEqual(_git(repo, "status", "--porcelain"), "")


@unittest.skipIf(_GIT is None, "git is not available on PATH")
class WorktreeSwapRegressionTest(unittest.TestCase):
    """R9 review j#96406 finding 1, reproduced and then pinned closed.

    The scenario is the reviewer's: a checkout is measured, a foreign lane's clean checkout
    takes its place, and the merge runs. Under the worktree merge the foreign checkout was
    switched to the target branch and the merge commit was built on it, and the call still
    reported success. Nothing the actuator does now can reach a checkout at all, so the swap
    is a no-op — which is what a withdrawal-free fix has to demonstrate.
    """

    def test_swapping_a_foreign_checkout_in_mid_run_cannot_touch_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            base = _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "branch", "foreign_lane")
            _git(repo, "checkout", "-q", "-b", "parking")

            # The path a previous round would have merged inside, now holding a FOREIGN lane.
            contested = root / "contested"
            _git(repo, "worktree", "add", "-q", str(contested), "foreign_lane")
            before_head = _git(contested, "rev-parse", "HEAD")
            before_branch = _git(contested, "rev-parse", "--abbrev-ref", "HEAD")

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            result = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=base
            )

            self.assertFalse(result.conflicted, result.detail)
            # The merge exists as an object, on the measured target, and the foreign lane's
            # checkout is exactly where it was — same branch, same HEAD, still clean.
            self.assertEqual(
                _git(
                    repo, "rev-list", "--parents", "-n", "1", result.integration_head
                ).split()[1],
                base,
            )
            self.assertEqual(
                _git(contested, "rev-parse", "--abbrev-ref", "HEAD"), before_branch
            )
            self.assertEqual(_git(contested, "rev-parse", "HEAD"), before_head)
            self.assertEqual(_git(contested, "status", "--porcelain"), "")
            self.assertEqual(_git(repo, "rev-parse", "foreign_lane"), before_head)


@unittest.skipIf(_GIT is None, "git is not available on PATH")
class NoDestructiveOperationTest(unittest.TestCase):
    """The adapter destroys nothing, and the reason is measurable on the binary itself."""

    def test_the_adapter_exposes_no_destructive_operation(self) -> None:
        for gone in ("delete_local_branch", "delete_remote_branch", "remove_worktree"):
            self.assertFalse(hasattr(LiveAutoIntegrationGitOperations, gone), gone)

    def test_no_git_primitive_enforces_both_delete_conditions_at_once(self) -> None:
        """The measurement the branch-delete withdrawal rests on, kept executable.

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

    def test_no_git_primitive_binds_a_worktree_removal_to_an_identity(self) -> None:
        """The measurement the worktree-removal withdrawal rests on, kept executable.

        Review j#96401 finding 1 asked for the identity to be verified *inside* the mutation
        primitive or under a lock held across it. Neither is constructible, and this is where
        that claim is checked rather than asserted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # (a) `worktree remove` takes no expected-identity argument at all.
            repo = root / "flags"
            _init(repo)
            _commit(repo, "a.txt", "base")
            usage = subprocess.run(
                ["git", "worktree", "remove", "-h"], cwd=repo, capture_output=True, text=True
            )
            options = (usage.stdout + usage.stderr).lower()
            # The whole option surface is one flag, and it is the one we may not use.
            self.assertIn("force", options)
            for absent in ("--expect", "--branch", "--if-", "--verify", "--head"):
                self.assertNotIn(absent, options, absent)

            # (b) the admin entry name is REUSED after a swap, so it is not instance identity.
            repo = root / "name"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "branch", "lane")
            _git(repo, "branch", "foreign")
            path = root / "wt-name"
            _git(repo, "worktree", "add", "-q", str(path), "lane")
            admin = repo / ".git" / "worktrees"
            before = sorted(entry.name for entry in admin.iterdir())
            _git(repo, "worktree", "remove", str(path))
            _git(repo, "worktree", "add", "-q", str(path), "foreign")
            self.assertEqual(before, sorted(entry.name for entry in admin.iterdir()))

            # (c) `worktree lock` DOES pin the path->entry binding against every git-level
            #     takeover — and that is why it looks like an answer...
            repo = root / "lock"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "branch", "lane")
            _git(repo, "branch", "foreign")
            path = root / "wt-lock"
            _git(repo, "worktree", "add", "-q", str(path), "lane")
            _git(repo, "worktree", "lock", "--reason", "ours", str(path))
            competitor = subprocess.run(
                ["git", "worktree", "remove", str(path)],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertNotEqual(competitor.returncode, 0)
            shutil.rmtree(path)
            _git(repo, "worktree", "prune")
            readd = subprocess.run(
                ["git", "worktree", "add", "-q", str(path), "foreign"],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertNotEqual(readd.returncode, 0)
            self.assertIn("locked", readd.stderr)

            # ... (d) ...but NO mutation runs while it is held, so the unlock that must come
            #     first reopens exactly the window the lock was supposed to close.
            repo = root / "held-lock"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "branch", "lane")
            path = root / "wt-held"
            _git(repo, "worktree", "add", "-q", str(path), "lane")
            _git(repo, "worktree", "lock", "--reason", "ours", str(path))
            for argv in (
                ["git", "worktree", "remove", str(path)],
                ["git", "worktree", "remove", "--force", str(path)],
                ["git", "worktree", "move", str(path), str(root / "moved")],
            ):
                result = subprocess.run(cwd=repo, args=argv, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, argv)
                self.assertIn("locked", result.stderr, argv)

            # (e) and the lock is not even an ownership token: anyone unlocks it without
            #     presenting the reason.
            self.assertEqual(
                subprocess.run(
                    ["git", "worktree", "unlock", str(path)],
                    cwd=repo, capture_output=True,
                ).returncode,
                0,
            )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
