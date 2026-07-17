"""Lane identity must name the lane, not the caller's cwd (Redmine #13933 j#81046).

The live defect (#13846 j#81024, diagnosed in #13933 j#81043): ``_worktree`` chose the lane
identity token family with ``resolved == repo_root``.  ``repo_root`` is only the caller's
``--repo`` / cwd, so the SAME lane derived ``wt_`` from one directory and ``dl_`` from
another.  Run from the lane's own worktree -- the execution root operators were told to use
-- the lane stopped matching its own durable row and the public rail dead-ended with a
collapsed ``not_hibernated_released_bound_pins_empty`` that named no axis.

These tests drive the real derivation against real git worktrees; nothing here hand-writes an
identity or a fault set.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import (
    ConvergeBoundPairRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence_live import (
    LiveBoundPairConvergenceOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
    is_git_worktree_root,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    derive_directory_lane_token,
    derive_lane_workspace_token,
    lane_runtime_identity,
)

LANE = "issue_13933_identity_lane"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ("git", "-C", str(cwd), *args), check=True, capture_output=True, text=True
    )


class _GitFixture(unittest.TestCase):
    """A real main checkout plus a real linked worktree, built per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.main = root / "main_checkout"
        self.main.mkdir()
        _git(self.main, "init", "-q", "-b", "main")
        _git(self.main, "config", "user.email", "t@example.invalid")
        _git(self.main, "config", "user.name", "t")
        (self.main / "f.txt").write_text("x\n")
        _git(self.main, "add", "f.txt")
        _git(self.main, "commit", "-qm", "init")
        self.worktree = root / "lane_worktree"
        _git(self.main, "worktree", "add", "-q", "-b", LANE, str(self.worktree))
        self.worktree = self.worktree.resolve()
        # A non-git directory scaffold lane: a plain directory, no git of its own.
        self.scaffold = root / "scaffold_root"
        self.scaffold.mkdir()
        self.scaffold = self.scaffold.resolve()


class GitWorktreeRootProbeTests(_GitFixture):
    def test_linked_worktree_and_main_checkout_are_worktree_roots(self):
        self.assertTrue(is_git_worktree_root(self.worktree))
        self.assertTrue(is_git_worktree_root(self.main.resolve()))

    def test_plain_directory_is_not_a_worktree_root(self):
        self.assertFalse(is_git_worktree_root(self.scaffold))

    def test_a_directory_nested_inside_a_repo_is_not_its_own_worktree_root(self):
        # `--is-inside-work-tree` would call this a worktree and mint it a `wt_` token that
        # collides with the enclosing checkout's. Only the toplevel names a worktree.
        nested = self.main / "sub" / "dir"
        nested.mkdir(parents=True)
        self.assertFalse(is_git_worktree_root(nested.resolve()))


class LaneRuntimeIdentityTests(unittest.TestCase):
    """The pure selector: the root's kind decides, and only that."""

    def test_git_worktree_uses_the_workspace_token(self):
        self.assertEqual(
            lane_runtime_identity("/w/lane", LANE, git_worktree=True),
            derive_lane_workspace_token("/w/lane"),
        )

    def test_non_git_scaffold_uses_the_lane_scoped_directory_token(self):
        self.assertEqual(
            lane_runtime_identity("/w/root", LANE, git_worktree=False),
            derive_directory_lane_token("/w/root", LANE),
        )

    def test_the_two_families_are_distinct_for_one_root(self):
        # Guards the whole point: these are different identities, not spellings.
        self.assertNotEqual(
            lane_runtime_identity("/w/x", LANE, git_worktree=True),
            lane_runtime_identity("/w/x", LANE, git_worktree=False),
        )

    def test_lane_id_cannot_move_a_git_worktree_identity(self):
        # A worktree is named by its path alone, so a lane rename must not re-key it.
        self.assertEqual(
            lane_runtime_identity("/w/lane", "lane_a", git_worktree=True),
            lane_runtime_identity("/w/lane", "lane_b", git_worktree=True),
        )


class ExecutionRootInvarianceTests(_GitFixture):
    """The regression itself: identity is a fact about the lane, not about the caller."""

    def _identity_from(self, repo_root: Path) -> str:
        ops = LiveBoundPairConvergenceOps(repo_root=repo_root)
        request = ConvergeBoundPairRequest(
            issue="13846",
            journal="80925",
            lane=LANE,
            worktree=str(self.worktree),
            branch=LANE,
        )
        _resolved, _workspace, identity = ops._worktree(request)
        return identity

    def test_identity_is_the_same_from_every_execution_root(self):
        # Pre-fix this returned the `dl_` token from the lane worktree and the `wt_` token
        # from anywhere else -- the exact live split of #13846 j#81024.
        from_lane_root = self._identity_from(self.worktree)
        from_main_checkout = self._identity_from(self.main)
        from_elsewhere = self._identity_from(self.scaffold)

        self.assertEqual(from_lane_root, from_main_checkout)
        self.assertEqual(from_lane_root, from_elsewhere)

    def test_a_git_worktree_lane_derives_the_workspace_token_family(self):
        # And it is specifically the family the durable row carries for a git lane.
        self.assertEqual(
            self._identity_from(self.worktree),
            derive_lane_workspace_token(str(self.worktree)),
        )

    def test_running_from_the_lane_worktree_does_not_mint_a_directory_token(self):
        # The precise live failure: `--repo <the lane worktree>` collapsed the proxy and
        # flipped the token family, so the row's own identity read as foreign.
        self.assertNotEqual(
            self._identity_from(self.worktree),
            derive_directory_lane_token(str(self.worktree), LANE),
        )

    def test_a_non_git_scaffold_lane_keeps_its_lane_scoped_token(self):
        # The #13392 case must not regress: a non-git lane runs in the shared root, where a
        # path-only token would collide across every lane on it.
        ops = LiveBoundPairConvergenceOps(repo_root=self.main)
        request = ConvergeBoundPairRequest(
            issue="13846", journal="80925", lane=LANE,
            worktree=str(self.scaffold), branch=LANE,
        )
        _resolved, _workspace, identity = ops._worktree(request)
        self.assertEqual(identity, derive_directory_lane_token(str(self.scaffold), LANE))

    def test_two_non_git_lanes_on_one_root_stay_distinct(self):
        def identity_for(lane: str) -> str:
            ops = LiveBoundPairConvergenceOps(repo_root=self.main)
            _r, _w, identity = ops._worktree(
                ConvergeBoundPairRequest(
                    issue="13846", journal="80925", lane=lane,
                    worktree=str(self.scaffold), branch=lane,
                )
            )
            return identity

        self.assertNotEqual(identity_for("lane_a"), identity_for("lane_b"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
