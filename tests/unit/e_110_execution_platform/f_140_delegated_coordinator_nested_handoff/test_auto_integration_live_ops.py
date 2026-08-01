"""Live auto-integration Git adapter argv / refusal tests (Redmine #13686).

The adapter is where the actuator's safety story becomes real ``git`` argv, so what it can
and cannot construct is pinned here rather than left to inspection:

- a branch name that would change a refspec's meaning (a leading ``+`` spells a force, a
  leading ``-`` becomes an option) is refused before any argv is built;
- the push is a plain non-force push of an exact SHA to ``refs/heads/<branch>`` — no
  ``--force``, no ``--force-with-lease``, no ``+`` refspec;
- the merge is built from objects (``merge-tree --write-tree`` + ``commit-tree``): no
  checkout is switched, no ref moves, a conflict is reported rather than resolved, and an
  unusable primitive is reported as something other than a conflict;
- the adapter has **no destructive operation at all** — no ref delete local or remote, and
  no worktree removal (reviews j#96344 / j#96396 / j#96401, each finding 1). None of the
  three could enforce its own condition in one invocation, so none exists to be called;
- ``describe_lane_worktree`` measures the LANE's checkout and reads every failure to measure
  as the unsafe answer;
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
TREE = "f" * 40


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


def _fail_rc(returncode: int, *, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    """A failure with an EXACT exit code — `merge-tree` distinguishes 1 from everything else."""
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=out, stderr=err
    )


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
    """The merge is objects only. Review j#96406 finding 1 is why there is no worktree here."""

    def test_the_merge_writes_a_tree_and_commits_it_with_the_measured_parent(self) -> None:
        recorder = _Recorder([_ok(TREE), _ok(MERGE_HEAD)])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertFalse(result.conflicted)
        self.assertEqual(result.integration_head, MERGE_HEAD)

        write_tree, commit = recorder.argvs
        self.assertEqual(write_tree[:2], ("merge-tree", "--write-tree"))
        # The merge's inputs are object ids, in the order that makes the measured target the
        # first parent — not a branch name anything could re-point.
        self.assertEqual(write_tree[-2:], (TARGET, SOURCE))
        self.assertEqual(commit[:2], ("commit-tree", TREE))
        self.assertEqual(commit.count("-p"), 2)
        self.assertEqual(commit[commit.index("-p") + 1], TARGET)

    def test_nothing_runs_in_a_worktree_and_nothing_switches_or_moves_a_ref(self) -> None:
        # The reproduction that retired the old form switched a foreign lane's checkout onto
        # the target branch. Nothing here may switch, checkout, reset or update a ref.
        recorder = _Recorder([_ok(TREE), _ok(MERGE_HEAD)])
        _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertTrue(all(cwd is None for _, cwd in recorder.calls))
        verbs = {argv[0] for argv in recorder.argvs}
        for forbidden in ("switch", "checkout", "merge", "reset", "update-ref", "branch"):
            self.assertNotIn(forbidden, verbs, forbidden)

    def test_a_non_sha_input_refuses_without_spawning_git(self) -> None:
        for kwargs in (
            {"source_head": "HEAD", "expected_target_head": TARGET},
            {"source_head": SOURCE, "expected_target_head": "main"},
        ):
            recorder = _Recorder([])
            result = _adapter(recorder).apply_merge(target_ref="main", **kwargs)
            self.assertTrue(result.conflicted)
            self.assertEqual(recorder.argvs, [], kwargs)

    def test_a_conflict_is_reported_and_never_resolved(self) -> None:
        # `merge-tree` exits 1 for a real conflict and prints the conflicted paths.
        recorder = _Recorder([_fail_rc(1, out=f"{TREE}\n100644 abc 1\ta.py\nCONFLICT (content)")])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertTrue(result.conflicted)
        self.assertIn("merge conflicted", result.detail)
        self.assertEqual(result.integration_head, "")
        # It never committed the conflicted tree, and used no strategy to make it go away.
        self.assertEqual(len(recorder.argvs), 1)
        flat = " ".join(" ".join(argv) for argv in recorder.argvs)
        for forbidden in ("--strategy", "-X", "theirs", "ours", "rebase"):
            self.assertNotIn(forbidden, flat, forbidden)

    def test_an_unusable_primitive_is_not_reported_as_a_conflict(self) -> None:
        # `--write-tree` needs git >= 2.38. Both refuse, but a durable record that says
        # "the branches conflict" when the truth is "this git cannot do it" is the class of
        # lie j#96396 finding 2 was about.
        recorder = _Recorder([_fail_rc(129, err="error: unknown option `write-tree'")])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertTrue(result.conflicted)
        self.assertIn("unavailable", result.detail)
        self.assertIn("NOT a content conflict", result.detail)

    def test_a_merge_that_names_no_tree_is_not_committed(self) -> None:
        recorder = _Recorder([_ok("")])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertTrue(result.conflicted)
        self.assertEqual(len(recorder.argvs), 1)

    def test_a_tree_that_cannot_be_committed_is_reported_as_failed(self) -> None:
        recorder = _Recorder([_ok(TREE), _fail("could not write commit")])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
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
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
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


class LaneWorktreeProbeTest(unittest.TestCase):
    def test_the_probe_is_part_of_the_port_the_use_case_calls(self) -> None:
        # R2 review j#96350 finding 3: R2 had this probe on the adapter only, so the use case
        # could not call it and re-checked the caller's booleans instead. Being on the port is
        # what makes the actuator — not the caller — the authority for this fact.
        self.assertTrue(hasattr(AutoIntegrationGitOperations, "describe_lane_worktree"))
        self.assertIsInstance(
            LiveAutoIntegrationGitOperations(repo_root=Path("/x")),
            AutoIntegrationGitOperations,
        )

    def test_it_no_longer_describes_a_checkout_anything_is_performed_in(self) -> None:
        # The probe outlived the dedicated worktree it was written for; what is gone with it
        # is the admissibility verdict, which only ever meant "you may mutate here".
        described = _adapter(
            _Recorder([_ok("worktree /lane\n"), _ok("lane\n"), _ok("")])
        ).describe_lane_worktree(path="/lane")
        self.assertFalse(hasattr(described, "admissibility_errors"))
        self.assertFalse(hasattr(described, "is_lane_worktree"))

    def test_a_registered_clean_lane_is_measured(self) -> None:
        recorder = _Recorder([_ok("worktree /lane\nHEAD abc\n"), _ok("lane\n"), _ok("")])
        described = _adapter(recorder).describe_lane_worktree(path="/lane")
        self.assertTrue(described.registered)
        self.assertTrue(described.clean)
        self.assertEqual(described.checked_out_branch, "lane")

    def test_an_unreadable_worktree_list_reads_as_unregistered(self) -> None:
        recorder = _Recorder([_fail(), _fail(), _fail()])
        described = _adapter(recorder).describe_lane_worktree(path="/lane")
        self.assertFalse(described.registered)
        # ...and an unreadable status reads as not clean.
        self.assertFalse(described.clean)
        self.assertEqual(described.checked_out_branch, "")

    def test_a_dirty_lane_is_reported_unclean(self) -> None:
        recorder = _Recorder([_ok("worktree /lane\n"), _ok("lane\n"), _ok(" M a.py\n")])
        described = _adapter(recorder).describe_lane_worktree(path="/lane")
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
