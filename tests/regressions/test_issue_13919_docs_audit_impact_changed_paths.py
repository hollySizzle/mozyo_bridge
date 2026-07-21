"""Changed-path selection for the docs impact gate (Redmine #13919).

`git_changed_paths` feeds both `mozyo-bridge docs audit-impact` and
`mozyo-bridge tests resolve`. `--all-changed` queried unstaged + untracked
only, so a fully staged worktree resolved to zero paths: the gate printed
"No changed paths." and exited 0 while `--staged` on the same worktree
reported the real set (#13892 j#80495 observed 7 staged files vs 0). A
verification gate that reads "nothing changed" when everything changed is
worse than no gate, so each source of the union is pinned here.

Placement per vibes/docs/logics/tests-placement-discovery-policy.md
`## 配置決定木`: branch 3 (a fixed defect's re-occurrence pin) settles this
file and precedes the real-collaborator branch, so driving a real `git`
against a temp repo does not move it to `integration/`.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.docs_tools.impact import (  # noqa: E402
    git_changed_paths,
    git_changed_paths_since,
)


class GitChangedPathsTest(unittest.TestCase):
    """Pin the cached/unstaged/untracked union `--all-changed` promises."""

    def _git(self, repo: Path, *cmd: str) -> None:
        subprocess.run(
            ["git", *cmd],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _repo(self, stack: tempfile.TemporaryDirectory) -> Path:
        repo = Path(stack.name) / "repo"
        repo.mkdir()
        self._git(repo, "init", "--initial-branch=main")
        self._git(repo, "config", "user.email", "test@example.invalid")
        self._git(repo, "config", "user.name", "Test")
        return repo

    def _commit_files(self, repo: Path, *names: str, body: str = "seed\n") -> None:
        """Seed and commit *names* in one commit.

        One commit per call, and every seed committed before any test stages
        an edit: `git commit` takes the whole index, so seeding a later file
        would otherwise sweep an already-staged edit into the commit and the
        staged-scope assertions would pass for the wrong reason.
        """
        for name in names:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            self._git(repo, "add", name)
        self._git(repo, "commit", "-m", f"add {', '.join(names)}")

    def setUp(self) -> None:
        self._stack = tempfile.TemporaryDirectory()
        self.addCleanup(self._stack.cleanup)
        self.repo = self._repo(self._stack)

    # --- the reported defect -------------------------------------------

    def test_all_changed_finds_a_staged_only_change(self) -> None:
        """The #13919 defect: a staged-only worktree resolved to zero paths."""
        self._commit_files(self.repo, "tracked.txt")
        (self.repo / "tracked.txt").write_text("staged edit\n", encoding="utf-8")
        self._git(self.repo, "add", "tracked.txt")

        # Nothing is unstaged or untracked now, so the pre-fix union
        # (unstaged + untracked) returned []. Assert on the value, not on
        # "it changed": [] is exactly the silent pass being fixed.
        self.assertEqual(
            ["tracked.txt"],
            git_changed_paths(self.repo, all_changed=True),
        )

    def test_staged_scope_is_unchanged(self) -> None:
        """`--staged` stays cached-only; it must not absorb the union."""
        self._commit_files(self.repo, "cached.txt", "dirty.txt")
        (self.repo / "cached.txt").write_text("staged\n", encoding="utf-8")
        self._git(self.repo, "add", "cached.txt")
        (self.repo / "dirty.txt").write_text("unstaged\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("new\n", encoding="utf-8")

        self.assertEqual(["cached.txt"], git_changed_paths(self.repo, staged=True))

    def test_default_scope_is_unchanged(self) -> None:
        """No flag stays unstaged-only."""
        self._commit_files(self.repo, "cached.txt", "dirty.txt")
        (self.repo / "cached.txt").write_text("staged\n", encoding="utf-8")
        self._git(self.repo, "add", "cached.txt")
        (self.repo / "dirty.txt").write_text("unstaged\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("new\n", encoding="utf-8")

        self.assertEqual(["dirty.txt"], git_changed_paths(self.repo))

    # --- union / dedup --------------------------------------------------

    def test_all_changed_unions_every_source_and_dedups_overlap(self) -> None:
        """Mixed worktree: each source contributes, overlap appears once."""
        self._commit_files(self.repo, "cached.txt", "dirty.txt", "both.txt")

        (self.repo / "cached.txt").write_text("staged\n", encoding="utf-8")
        self._git(self.repo, "add", "cached.txt")
        (self.repo / "dirty.txt").write_text("unstaged\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("new\n", encoding="utf-8")
        # `both.txt` is staged AND further modified in the worktree, so it
        # is reported by the cached diff and the worktree diff alike — the
        # case where a naive concatenation would double-report.
        (self.repo / "both.txt").write_text("staged\n", encoding="utf-8")
        self._git(self.repo, "add", "both.txt")
        (self.repo / "both.txt").write_text("staged then edited\n", encoding="utf-8")

        paths = git_changed_paths(self.repo, all_changed=True)

        self.assertEqual(
            {"cached.txt", "dirty.txt", "both.txt", "untracked.txt"},
            set(paths),
        )
        self.assertEqual(len(paths), len(set(paths)), msg=f"duplicates in {paths}")

    def test_all_changed_order_is_deterministic(self) -> None:
        """Same worktree, repeated calls, identical listing."""
        self._commit_files(self.repo, "cached.txt", "dirty.txt")
        (self.repo / "cached.txt").write_text("staged\n", encoding="utf-8")
        self._git(self.repo, "add", "cached.txt")
        (self.repo / "dirty.txt").write_text("unstaged\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("new\n", encoding="utf-8")

        first = git_changed_paths(self.repo, all_changed=True)
        self.assertEqual(first, git_changed_paths(self.repo, all_changed=True))
        # Fixed source order + git's per-source path sort.
        self.assertEqual(["cached.txt", "dirty.txt", "untracked.txt"], first)

    def test_all_changed_wins_when_both_flags_are_set(self) -> None:
        """`tests resolve` accepts both flags; the superset must win."""
        self._commit_files(self.repo, "cached.txt")
        (self.repo / "cached.txt").write_text("staged\n", encoding="utf-8")
        self._git(self.repo, "add", "cached.txt")
        (self.repo / "untracked.txt").write_text("new\n", encoding="utf-8")

        self.assertEqual(
            ["cached.txt", "untracked.txt"],
            git_changed_paths(self.repo, staged=True, all_changed=True),
        )

    # --- rename / delete ------------------------------------------------

    def test_rename_reports_source_and_destination(self) -> None:
        """A rename's source path is impact too and must not be dropped.

        Rename detection collapses `R old -> new` to the destination path
        alone, so the docs bound to `old` would go unresolved.
        """
        self._commit_files(self.repo, "old.txt", body="line\n" * 40)
        self._git(self.repo, "mv", "old.txt", "new.txt")

        self.assertEqual(
            ["new.txt", "old.txt"],
            git_changed_paths(self.repo, all_changed=True),
        )

    def test_rename_listing_ignores_ambient_diff_renames_config(self) -> None:
        """`diff.renames` is operator config; the gate must not vary with it.

        Without `--no-renames` this repo-local `diff.renames=true` would hide
        `old.txt`, so the same worktree would audit differently per machine.
        """
        self._commit_files(self.repo, "old.txt", body="line\n" * 40)
        self._git(self.repo, "config", "diff.renames", "true")
        self._git(self.repo, "mv", "old.txt", "new.txt")

        self.assertIn("old.txt", git_changed_paths(self.repo, all_changed=True))

    def test_staged_delete_is_reported(self) -> None:
        self._commit_files(self.repo, "doomed.txt")
        self._git(self.repo, "rm", "doomed.txt")

        self.assertEqual(["doomed.txt"], git_changed_paths(self.repo, all_changed=True))
        self.assertEqual(["doomed.txt"], git_changed_paths(self.repo, staged=True))

    def test_unstaged_delete_is_reported(self) -> None:
        self._commit_files(self.repo, "doomed.txt")
        (self.repo / "doomed.txt").unlink()

        self.assertEqual(["doomed.txt"], git_changed_paths(self.repo, all_changed=True))

    # --- fail-closed ----------------------------------------------------

    def test_git_failure_raises_rather_than_returning_empty(self) -> None:
        """A git error must fail the gate, not read as "nothing changed"."""
        with tempfile.TemporaryDirectory() as outside:
            for kwargs in ({"all_changed": True}, {"staged": True}, {}):
                with self.subTest(scope=kwargs):
                    with self.assertRaises(subprocess.CalledProcessError):
                        git_changed_paths(Path(outside), **kwargs)

    def test_git_changed_paths_since_failure_is_fail_closed(self) -> None:
        self._commit_files(self.repo, "tracked.txt")
        with self.assertRaises(subprocess.CalledProcessError):
            git_changed_paths_since(self.repo, "no-such-ref")

    def test_git_changed_paths_since_reports_rename_source(self) -> None:
        """CI's `--base` lane keeps rename listing at parity with local."""
        self._commit_files(self.repo, "old.txt", body="line\n" * 40)
        self._git(self.repo, "checkout", "-q", "-b", "topic")
        self._git(self.repo, "mv", "old.txt", "new.txt")
        self._git(self.repo, "commit", "-m", "rename")

        self.assertEqual(
            ["new.txt", "old.txt"],
            git_changed_paths_since(self.repo, "main"),
        )


if __name__ == "__main__":
    unittest.main()
