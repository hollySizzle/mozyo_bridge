"""Live auto-integration Git adapter argv / refusal tests (Redmine #13686).

The adapter is where the actuator's safety story becomes real ``git`` argv, so what it can
and cannot construct is pinned here rather than left to inspection:

- a branch name that would change a refspec's meaning (a leading ``+`` spells a force, a
  leading ``-`` becomes an option) is refused before any argv is built;
- the push is a plain non-force push of an exact SHA to ``refs/heads/<branch>`` — no
  ``--force``, no ``--force-with-lease``, no ``+`` refspec;
- the merge runs in the dedicated worktree and aborts on conflict rather than resolving it;
- the adapter has **no destructive operation at all** — no ref delete local or remote, and
  no worktree removal (reviews j#96344 / j#96396 / j#96401, each finding 1). None of the
  three could enforce its own condition in one invocation, so none exists to be called;
- ``describe_integration_worktree`` measures the dedicated-worktree identity and reads every
  failure to measure as the unsafe answer;
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
TARGET = "b" * 40
OTHER_HEAD = "e" * 40


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
        # switch -> verify the local target tip -> merge -> read the head
        recorder = _Recorder([_ok(), _ok(TARGET), _ok(), _ok(MERGE_HEAD)])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE,
            target_ref="main",
            integration_worktree="/wt",
            expected_target_head=TARGET,
        )
        self.assertFalse(result.conflicted)
        self.assertEqual(result.integration_head, MERGE_HEAD)
        # Every command ran in the dedicated worktree, never in the lane's repo root.
        self.assertTrue(all(cwd == Path("/wt") for _, cwd in recorder.calls))
        self.assertEqual(recorder.argvs[0][:2], ("switch", "main"))
        self.assertIn("--no-ff", recorder.argvs[2])

    def test_a_local_target_that_is_not_the_expected_remote_head_is_refused(self) -> None:
        # R6 review j#96391 finding 1: merging onto whatever the dedicated worktree happened
        # to have checked out put an unreviewed commit on the integration branch.
        recorder = _Recorder([_ok(), _ok(OTHER_HEAD)])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE,
            target_ref="main",
            integration_worktree="/wt",
            expected_target_head=TARGET,
        )
        self.assertTrue(result.conflicted)
        self.assertIn("refusing to merge onto an unverified parent", result.detail)
        # It never reached the merge.
        self.assertNotIn("merge", [argv[0] for argv in recorder.argvs])

    def test_a_conflict_aborts_and_is_not_resolved(self) -> None:
        recorder = _Recorder([_ok(), _ok(TARGET), _fail("CONFLICT"), _ok()])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE,
            target_ref="main",
            integration_worktree="/wt",
            expected_target_head=TARGET,
        )
        self.assertTrue(result.conflicted)
        self.assertIn(("merge", "--abort"), [argv[:2] for argv in recorder.argvs])
        # No strategy flag was used to make the conflict go away.
        flat = " ".join(" ".join(argv) for argv in recorder.argvs)
        for forbidden in ("--strategy", "-X", "theirs", "ours", "rebase"):
            self.assertNotIn(forbidden, flat, forbidden)

    def test_a_merge_whose_head_cannot_be_resolved_is_treated_as_failed(self) -> None:
        recorder = _Recorder([_ok(), _ok(TARGET), _ok(), _fail()])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE,
            target_ref="main",
            integration_worktree="/wt",
            expected_target_head=TARGET,
        )
        self.assertTrue(result.conflicted)
        self.assertEqual(result.integration_head, "")


class NoDestructiveOperationTest(unittest.TestCase):
    def test_the_adapter_exposes_no_destructive_operation(self) -> None:
        # Three were shipped and three were retired, each because the property that made it
        # safe was established in a different invocation from the one that acted:
        # `delete_remote_branch` (j#96344 finding 1) had no CAS against the remote tip at all;
        # `delete_local_branch` (j#96396 finding 1) verified the tip and then deleted, and a
        # commit landing between the two was destroyed; `remove_worktree` (j#96401 finding 1)
        # took a path whose identity an earlier probe had established, and a foreign lane's
        # checkout swapped onto that path was removed. Removed rather than guarded, so there
        # is no argv left to get wrong.
        for gone in ("delete_remote_branch", "delete_local_branch", "remove_worktree"):
            self.assertFalse(
                hasattr(LiveAutoIntegrationGitOperations, gone), gone
            )
            self.assertFalse(
                hasattr(AutoIntegrationGitOperations, gone),
                "the port must not declare an operation the adapter refuses to provide",
            )

    def test_no_mutation_spells_a_delete_or_a_removal(self) -> None:
        # A ref delete is spelled either as an EMPTY refspec source (`:refs/heads/x`) or as a
        # branch/update-ref delete flag; a checkout removal as `worktree remove`. Drive every
        # mutation the adapter has left and assert none produces any of those shapes.
        recorder = _Recorder([_ok(), _ok(TARGET), _ok(), _ok(MERGE_HEAD), _ok(), _ok(), _ok()])
        operations = _adapter(recorder)
        operations.apply_merge(
            source_head=SOURCE,
            target_ref="main",
            integration_worktree="/wt",
            expected_target_head=TARGET,
        )
        operations.push_non_force(source_head=SOURCE, target_ref="main")
        for argv in recorder.argvs:
            self.assertNotIn("-D", argv)
            self.assertNotEqual(argv[:1], ("update-ref",))
            self.assertNotEqual(argv[:2], ("worktree", "remove"))
            for token in argv:
                self.assertFalse(
                    token.startswith(":"), f"{token!r} is a ref-deleting refspec"
                )


class IntegrationWorktreeProbeTest(unittest.TestCase):
    def test_the_probe_is_part_of_the_port_the_use_case_calls(self) -> None:
        # R2 review j#96350 finding 3: R2 had this probe on the adapter only, so the use case
        # could not call it and re-checked the caller's booleans instead. Being on the port is
        # what makes the actuator — not the caller — the authority for this fact.
        self.assertTrue(
            hasattr(AutoIntegrationGitOperations, "describe_integration_worktree")
        )
        self.assertIsInstance(
            LiveAutoIntegrationGitOperations(repo_root=Path("/x")),
            AutoIntegrationGitOperations,
        )

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
