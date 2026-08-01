"""Live auto-integration Git adapter argv / refusal tests (Redmine #13686).

The adapter is where the actuator's safety story becomes real ``git`` argv, so what it can
and cannot construct is pinned here rather than left to inspection:

- a branch name that would change a refspec's meaning (a leading ``+`` spells a force, a
  leading ``-`` becomes an option) is refused before any argv is built;
- the push is a plain non-force push of an exact SHA to ``refs/heads/<branch>`` — no
  ``--force``, no ``--force-with-lease``, no ``+`` refspec;
- the merge runs in the dedicated worktree and aborts on conflict rather than resolving it;
- ``git worktree remove`` carries no ``--force``;
- the adapter has **no remote-ref delete at all** (R1 review j#96344 finding 1);
- ``describe_integration_worktree`` measures the dedicated-worktree identity and reads every
  failure to measure as the unsafe answer;
- the local branch delete is ``git update-ref -d <ref> <old_value>`` — a real
  compare-and-swap — and never ``git branch -d`` / ``-D``;
- every read probe fails closed when ``git`` could not run.

Hermetic: ``_run`` is stubbed to record argv and return canned results. No real ``git``
process is spawned and no network is touched.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (
    AutoIntegrationGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_ops import (
    LiveAutoIntegrationGitOperations,
    UnsafeRefspecError,
    _checked_branch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (
    EMPTY_TARGET_HEAD,
)

SOURCE = "a" * 40
MERGE_HEAD = "d" * 40


class _Recorder:
    """Records every ``git`` invocation and replays canned results in order."""

    def __init__(self, results: List[subprocess.CompletedProcess]) -> None:
        self.results = list(results)
        self.calls: List[Tuple[Tuple[str, ...], object]] = []

    def __call__(self, *args: str, cwd: object = None) -> subprocess.CompletedProcess:
        self.calls.append((args, cwd))
        if self.results:
            return self.results.pop(0)
        return _ok("")

    @property
    def argvs(self) -> List[Tuple[str, ...]]:
        return [args for args, _ in self.calls]


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=1, stdout="", stderr=stderr)


def _adapter(recorder: _Recorder) -> LiveAutoIntegrationGitOperations:
    operations = LiveAutoIntegrationGitOperations(repo_root=Path("/nonexistent-repo-root"))
    object.__setattr__(operations, "_run", recorder)
    return operations


class RefspecSafetyTest(unittest.TestCase):
    def test_a_plus_prefixed_branch_is_refused(self) -> None:
        # `+` is how a force is spelled INSIDE a refspec, so a branch name carrying one must
        # never reach argv construction.
        with self.assertRaises(UnsafeRefspecError):
            _checked_branch("+main")

    def test_an_option_shaped_branch_is_refused(self) -> None:
        with self.assertRaises(UnsafeRefspecError):
            _checked_branch("--force")

    def test_meaning_changing_characters_are_refused(self) -> None:
        for name in ("ma in", "main:other", "main^", "main~1", "ma*n", "main\tx", ""):
            with self.assertRaises(UnsafeRefspecError, msg=name):
                _checked_branch(name)

    def test_a_fully_qualified_ref_normalizes_to_its_bare_name(self) -> None:
        self.assertEqual(_checked_branch("refs/heads/feature/x"), "feature/x")
        self.assertEqual(_checked_branch("  main  "), "main")

    def test_the_push_refuses_an_unsafe_target_before_spawning_git(self) -> None:
        recorder = _Recorder([])
        with self.assertRaises(UnsafeRefspecError):
            _adapter(recorder).push_non_force(source_head=SOURCE, target_ref="+main")
        self.assertEqual(recorder.argvs, [])


class PushTest(unittest.TestCase):
    def test_the_push_is_non_force_and_names_an_exact_sha(self) -> None:
        recorder = _Recorder([_ok()])
        result = _adapter(recorder).push_non_force(source_head=SOURCE, target_ref="main")
        self.assertTrue(result.accepted)
        argv = recorder.argvs[0]
        self.assertEqual(argv[0], "push")
        self.assertIn(f"{SOURCE}:refs/heads/main", argv)
        flat = " ".join(argv)
        for forbidden in ("--force", "--force-with-lease", "-f", "+refs", f"+{SOURCE}"):
            self.assertNotIn(forbidden, flat, forbidden)

    def test_a_non_sha_source_is_refused_without_spawning_git(self) -> None:
        recorder = _Recorder([])
        result = _adapter(recorder).push_non_force(source_head="main", target_ref="main")
        self.assertFalse(result.accepted)
        self.assertFalse(result.rejected)
        self.assertEqual(recorder.argvs, [])

    def test_a_rejected_push_reports_rejection_and_offers_no_force(self) -> None:
        recorder = _Recorder([_fail("non-fast-forward")])
        result = _adapter(recorder).push_non_force(source_head=SOURCE, target_ref="main")
        self.assertFalse(result.accepted)
        self.assertTrue(result.rejected)
        self.assertIn("never force, never rebase", result.detail)
        self.assertEqual(len(recorder.argvs), 1)


class MergeTest(unittest.TestCase):
    def test_the_merge_runs_in_the_dedicated_worktree(self) -> None:
        recorder = _Recorder([_ok(), _ok(), _ok(MERGE_HEAD)])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", integration_worktree="/wt"
        )
        self.assertFalse(result.conflicted)
        self.assertEqual(result.integration_head, MERGE_HEAD)
        # Every command ran in the dedicated worktree, never in the lane's repo root.
        self.assertTrue(all(cwd == Path("/wt") for _, cwd in recorder.calls))
        self.assertEqual(recorder.argvs[0][:2], ("switch", "main"))
        self.assertIn("--no-ff", recorder.argvs[1])

    def test_a_conflict_aborts_and_is_not_resolved(self) -> None:
        recorder = _Recorder([_ok(), _fail("CONFLICT"), _ok()])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", integration_worktree="/wt"
        )
        self.assertTrue(result.conflicted)
        self.assertIn(("merge", "--abort"), [argv[:2] for argv in recorder.argvs])
        # No strategy flag was used to make the conflict go away.
        flat = " ".join(" ".join(argv) for argv in recorder.argvs)
        for forbidden in ("--strategy", "-X", "theirs", "ours", "rebase"):
            self.assertNotIn(forbidden, flat, forbidden)

    def test_a_merge_whose_head_cannot_be_resolved_is_treated_as_failed(self) -> None:
        recorder = _Recorder([_ok(), _ok(), _fail()])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", integration_worktree="/wt"
        )
        self.assertTrue(result.conflicted)
        self.assertEqual(result.integration_head, "")


class CleanupOperationTest(unittest.TestCase):
    def test_worktree_remove_carries_no_force(self) -> None:
        recorder = _Recorder([_ok()])
        self.assertTrue(_adapter(recorder).remove_worktree(worktree_path="/wt"))
        argv = recorder.argvs[0]
        self.assertEqual(argv[:2], ("worktree", "remove"))
        self.assertNotIn("--force", argv)
        self.assertNotIn("-f", argv)

    def test_local_delete_is_a_compare_and_swap_not_a_branch_delete(self) -> None:
        recorder = _Recorder([_ok()])
        self.assertTrue(
            _adapter(recorder).delete_local_branch(branch="lane", expected_tip=SOURCE)
        )
        argv = recorder.argvs[0]
        self.assertEqual(argv[:2], ("update-ref", "-d"))
        # The old value is supplied, which is what makes the delete conditional.
        self.assertIn("refs/heads/lane", argv)
        self.assertIn(SOURCE, argv)
        self.assertNotIn("branch", argv)
        self.assertNotIn("-D", argv)

    def test_a_non_sha_expected_tip_refuses_without_spawning_git(self) -> None:
        recorder = _Recorder([])
        self.assertFalse(
            _adapter(recorder).delete_local_branch(branch="lane", expected_tip="HEAD")
        )
        self.assertEqual(recorder.argvs, [])

    def test_a_failed_delete_reports_failure_rather_than_escalating(self) -> None:
        recorder = _Recorder([_fail("ref changed")])
        self.assertFalse(
            _adapter(recorder).delete_local_branch(branch="lane", expected_tip=SOURCE)
        )
        self.assertEqual(len(recorder.argvs), 1)


class NoRemoteRefDeleteTest(unittest.TestCase):
    def test_the_adapter_cannot_delete_a_remote_ref(self) -> None:
        # R1 had `delete_remote_branch`; review j#96344 finding 1 found it had no CAS against
        # the remote tip, and a real CAS needs the prohibited `--force-with-lease`. Removed
        # rather than guarded, so there is no argv to get wrong.
        self.assertFalse(hasattr(LiveAutoIntegrationGitOperations, "delete_remote_branch"))
        self.assertFalse(
            hasattr(AutoIntegrationGitOperations, "delete_remote_branch"),
            "the port must not declare an operation the adapter refuses to provide",
        )

    def test_no_mutation_constructs_a_deleting_refspec(self) -> None:
        # A ref delete is spelled as an EMPTY source in the refspec (`:refs/heads/x`). Drive
        # every mutation the adapter has and assert none of them produces that shape.
        recorder = _Recorder([_ok(), _ok(), _ok(MERGE_HEAD), _ok(), _ok(), _ok()])
        operations = _adapter(recorder)
        operations.apply_merge(
            source_head=SOURCE, target_ref="main", integration_worktree="/wt"
        )
        operations.push_non_force(source_head=SOURCE, target_ref="main")
        operations.remove_worktree(worktree_path="/wt")
        operations.delete_local_branch(branch="lane", expected_tip=SOURCE)
        for argv in recorder.argvs:
            for token in argv:
                self.assertFalse(
                    token.startswith(":"), f"{token!r} is a ref-deleting refspec"
                )


class IntegrationWorktreeProbeTest(unittest.TestCase):
    def test_a_registered_non_lane_clean_worktree_is_admissible(self) -> None:
        recorder = _Recorder(
            [_ok("worktree /wt\nHEAD abc\n"), _ok("main\n"), _ok("")]
        )
        described = _adapter(recorder).describe_integration_worktree(
            path="/wt", lane_worktree="/lane"
        )
        self.assertEqual(described.admissibility_errors(), ())
        self.assertEqual(described.checked_out_branch, "main")

    def test_the_lane_s_own_path_is_reported_as_the_lane_s(self) -> None:
        recorder = _Recorder([_ok("worktree /lane\n"), _ok("lane\n"), _ok("")])
        described = _adapter(recorder).describe_integration_worktree(
            path="/lane", lane_worktree="/lane"
        )
        self.assertTrue(described.is_lane_worktree)
        self.assertTrue(described.admissibility_errors())

    def test_an_unreadable_worktree_list_reads_as_unregistered(self) -> None:
        recorder = _Recorder([_fail(), _fail(), _fail()])
        described = _adapter(recorder).describe_integration_worktree(
            path="/wt", lane_worktree="/lane"
        )
        self.assertFalse(described.registered)
        # ...and an unreadable status reads as not clean.
        self.assertFalse(described.clean)
        self.assertTrue(described.admissibility_errors())

    def test_a_dirty_worktree_is_reported_unclean(self) -> None:
        recorder = _Recorder([_ok("worktree /wt\n"), _ok("main\n"), _ok(" M a.py\n")])
        described = _adapter(recorder).describe_integration_worktree(
            path="/wt", lane_worktree="/lane"
        )
        self.assertFalse(described.clean)


class ReadProbeTest(unittest.TestCase):
    def test_probes_fail_closed_when_git_could_not_run(self) -> None:
        operations = _adapter(_Recorder([_fail()]))
        self.assertFalse(operations.is_git_workspace())

        operations = _adapter(_Recorder([_fail()]))
        self.assertEqual(operations.resolve_head("main"), EMPTY_TARGET_HEAD)

        operations = _adapter(_Recorder([_fail()]))
        self.assertFalse(
            operations.is_ancestor(ancestor=SOURCE, descendant=MERGE_HEAD)
        )

        # An unreadable status reads DIRTY: a checkout we cannot inspect is never clean.
        operations = _adapter(_Recorder([_fail()]))
        self.assertTrue(operations.worktree_dirty())

        # An unreadable worktree list reads "the branch is still checked out".
        operations = _adapter(_Recorder([_fail()]))
        self.assertTrue(operations.branch_checked_out_elsewhere("lane"))

        operations = _adapter(_Recorder([_fail()]))
        self.assertEqual(operations.branch_tip("lane"), "")

    def test_a_spawn_failure_does_not_escape_a_read_only_probe(self) -> None:
        # The real `_run` maps OSError (missing cwd after a reboot, git absent from PATH) onto
        # a failed result rather than raising out of a preflight (#14499).
        operations = LiveAutoIntegrationGitOperations(
            repo_root=Path("/definitely/not/a/directory/13686")
        )
        self.assertFalse(operations.is_git_workspace())
        self.assertTrue(operations.worktree_dirty())

    def test_an_empty_target_is_a_fast_forward_from_nothing(self) -> None:
        recorder = _Recorder([])
        self.assertTrue(
            _adapter(recorder).is_ancestor(
                ancestor=EMPTY_TARGET_HEAD, descendant=SOURCE
            )
        )
        self.assertEqual(recorder.argvs, [])

    def test_remote_reachability_reads_the_remote_not_a_tracking_ref(self) -> None:
        # `ls-remote` asks the remote; a cached `refs/remotes/*` tracking ref can assert a tip
        # the remote no longer has (#14066's class of false proof).
        recorder = _Recorder(
            [_ok(f"{MERGE_HEAD}\trefs/heads/main\n"), _ok(), _ok()]
        )
        self.assertTrue(_adapter(recorder).commit_on_remote(SOURCE, branch="main"))
        self.assertEqual(recorder.argvs[0][0], "ls-remote")
        flat = " ".join(" ".join(argv) for argv in recorder.argvs)
        self.assertNotIn("--remotes", flat)
        self.assertNotIn("fetch", flat)

    def test_a_remote_tip_the_local_clone_lacks_fails_closed(self) -> None:
        recorder = _Recorder([_ok(f"{MERGE_HEAD}\trefs/heads/main\n"), _fail()])
        self.assertFalse(_adapter(recorder).commit_on_remote(SOURCE, branch="main"))

    def test_an_absent_remote_branch_fails_closed(self) -> None:
        recorder = _Recorder([_ok("")])
        self.assertFalse(_adapter(recorder).commit_on_remote(SOURCE, branch="main"))


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
