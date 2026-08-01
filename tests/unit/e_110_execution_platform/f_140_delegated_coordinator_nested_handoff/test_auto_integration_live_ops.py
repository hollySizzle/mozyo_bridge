"""Live auto-integration Git adapter argv / refusal tests (Redmine #13686).

The adapter is where the actuator's safety story becomes real ``git`` argv, so what it can
and cannot construct is pinned here rather than left to inspection:

- a branch name that would change a refspec's meaning (a leading ``+`` spells a force, a
  leading ``-`` becomes an option) is refused before any argv is built;
- the push is a plain non-force push of an exact SHA to ``refs/heads/<branch>`` — no
  ``--force``, no ``--force-with-lease``, no ``+`` refspec;
- the merge is built from objects (``merge-tree --write-tree`` + ``commit-tree``): no
  checkout is switched, no ref moves, the commit's identity and timestamps come from the
  action rather than from the host and the clock, and every failure carries its own typed
  status rather than one boolean;
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

import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (
    MERGE_COMMIT_ERROR,
    MERGE_CONTENT_CONFLICT,
    MERGE_ERROR,
    MERGE_INVALID_INPUT,
    MERGE_MERGED,
    MERGE_NONDETERMINISTIC_CONFIG,
    MERGE_PRIMITIVE_UNSUPPORTED,
    MERGE_PROBE_ERROR,
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
DATE = "2026-08-01T12:00:00+09:00"
OTHER_DATE = "2026-07-01T12:00:00+09:00"
#: The config every object-building invocation pins, in order. Asserted as a whole so a
#: silently dropped key fails rather than passing a prefix check.
_PINNED = (
    "-c",
    "i18n.commitEncoding=UTF-8",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "merge.directoryRenames=conflict",
    "-c",
    "merge.renames=true",
    "-c",
    "diff.renames=true",
    "-c",
    "merge.renameLimit=32767",
    "-c",
    "diff.renameLimit=32767",
)


class _Recorder:
    """Records every ``git`` invocation and replays canned results in order."""

    def __init__(self, results: List[subprocess.CompletedProcess]) -> None:
        self.results = list(results)
        self.calls: List[Tuple[Tuple[str, ...], object, object]] = []

    def __call__(
        self, *args: str, cwd: object = None, env: object = None
    ) -> subprocess.CompletedProcess:
        self.calls.append((args, cwd, env))
        if self.results:
            return self.results.pop(0)
        return _ok("")

    @property
    def argvs(self) -> List[Tuple[str, ...]]:
        return [args for args, _, _ in self.calls]

    @property
    def envs(self) -> List[dict]:
        """The environment overlays, so a test can pin what was made deterministic."""
        return [env or {} for _, _, env in self.calls]


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
    """The merge is objects only, deterministic, and honest about how it failed."""

    def _ok_version(self) -> subprocess.CompletedProcess:
        return _ok("git version 2.50.1")

    def _no_driver(self) -> subprocess.CompletedProcess:
        """`config --get-regexp` finds nothing: exit 1 with no output, which is not an error."""
        return _fail_rc(1)

    def _preamble(self) -> list:
        """Everything asked before a merge: is the ref legal, can this git, is it deterministic."""
        return [_ok("main"), self._ok_version(), self._no_driver()]

    def _dates(self) -> list:
        """Both parents' committer dates, as ISO strings then as epoch seconds."""
        return [_ok(DATE), _ok(OTHER_DATE), _ok("2000"), _ok("1000")]

    def test_the_merge_writes_a_tree_and_commits_it_with_the_measured_parent(self) -> None:
        recorder = _Recorder([*self._preamble(), _ok(TREE), *self._dates(), _ok(MERGE_HEAD)])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertEqual(result.status, MERGE_MERGED)
        self.assertFalse(result.conflicted)
        self.assertEqual(result.integration_head, MERGE_HEAD)

        ref_check, version, driver, write_tree = recorder.argvs[:4]
        commit = recorder.argvs[-1]
        # The target ref is validated against git's own grammar before any object is built
        # (j#96422 finding 3).
        self.assertEqual(ref_check[:2], ("check-ref-format", "--branch"))
        self.assertEqual(version, ("--version",))
        # Determinism is CHECKED before it is claimed: a configured merge driver would make
        # the tree host-dependent, so the adapter asks (j#96417 finding 1).
        self.assertEqual(driver[:2], ("config", "--get-regexp"))
        self.assertEqual(
            write_tree[len(_PINNED) : len(_PINNED) + 3],
            ("--no-replace-objects", "merge-tree", "--write-tree"),
        )
        # The merge's inputs are object ids, in the order that makes the measured target the
        # first parent — not a branch name anything could re-point.
        self.assertEqual(write_tree[-2:], (TARGET, SOURCE))
        self.assertEqual(
            commit[len(_PINNED) : len(_PINNED) + 3],
            ("--no-replace-objects", "commit-tree", TREE),
        )
        self.assertEqual(commit.count("-p"), 2)
        self.assertEqual(commit[commit.index("-p") + 1], TARGET)
        # Every measured host input is pinned on BOTH object-building commands: the encoding
        # that changes the commit id, and the rename settings that change the merged tree
        # (j#96417 finding 1, j#96422 finding 1).
        for argv in (write_tree, commit):
            self.assertEqual(argv[: len(_PINNED)], _PINNED)
        # ...and the environment is built rather than inherited, so no GIT_* variable the
        # caller happens to have set reaches git.
        object_envs = [
            env
            for argv, _, env in recorder.calls
            if "merge-tree" in argv or "commit-tree" in argv
        ]
        self.assertEqual(len(object_envs), 2)
        for env in object_envs:
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"], "1")
            self.assertNotIn("GIT_DIR", env)

    def test_the_commit_takes_its_identity_from_the_action_not_the_host(self) -> None:
        # R10 review j#96412 finding 1: the same action produced two SHAs a second apart,
        # because `commit-tree` reads `user.name` and the clock — two inputs no action key
        # covers. Both are pinned, and the timestamp comes from the SOURCE COMMIT, which is
        # an object the action key already covers.
        recorder = _Recorder([*self._preamble(), _ok(TREE), *self._dates(), _ok(MERGE_HEAD)])
        _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        environment = recorder.envs[-1]
        self.assertEqual(environment["GIT_AUTHOR_DATE"], DATE)
        self.assertEqual(environment["GIT_COMMITTER_DATE"], DATE)
        self.assertEqual(
            environment["GIT_AUTHOR_NAME"], environment["GIT_COMMITTER_NAME"]
        )
        self.assertNotIn("@", environment["GIT_AUTHOR_NAME"])
        self.assertIn("@", environment["GIT_AUTHOR_EMAIL"])

    def test_an_unreadable_parent_date_refuses_rather_than_using_the_clock(self) -> None:
        recorder = _Recorder([*self._preamble(), _ok(TREE), _fail("no such object")])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertEqual(result.status, MERGE_ERROR)
        self.assertIn("refusing to fall back to the clock", result.detail)
        self.assertNotIn("commit-tree", [token for argv in recorder.argvs for token in argv])

    def test_the_timestamp_is_the_later_parent_not_the_source(self) -> None:
        # j#96417 finding 5: in the ordinary non-ff case the target has moved on since the
        # lane branched, so a source-only timestamp puts the merge BEFORE its own first
        # parent and `git log --since` can lose it.
        recorder = _Recorder(
            [
                *self._preamble(),
                _ok(TREE),
                _ok(DATE),        # source ISO
                _ok(OTHER_DATE),  # target ISO
                _ok("1000"),      # source epoch — older
                _ok("2000"),      # target epoch — newer
                _ok(MERGE_HEAD),
            ]
        )
        _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertEqual(recorder.envs[-1]["GIT_COMMITTER_DATE"], OTHER_DATE)

    def test_a_configured_merge_driver_refuses_rather_than_merging(self) -> None:
        # Measured on real git: a `merge.<name>.driver` rewrites the merged content as
        # arbitrary host-local code, so the same action would not rebuild the same commit.
        recorder = _Recorder(
            [_ok("main"), self._ok_version(), _ok("merge.mine.driver cp %A %B")]
        )
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertEqual(result.status, MERGE_NONDETERMINISTIC_CONFIG)
        self.assertIn("merge.mine.driver", result.detail)
        self.assertEqual(len(recorder.argvs), 3)  # it never merged
        # The probe asks through the SAME config view the merge would use, so a driver the
        # merge could never have seen does not refuse it (j#96422 finding 4).
        self.assertEqual(recorder.envs[-1]["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_an_unsafe_ref_name_is_invalid_input_not_an_exception(self) -> None:
        # R11 declared this status covers unusable ref names and then let the exception
        # escape into the actuator (j#96417 finding 3). `+` is a legal ref character that
        # spells a force inside a refspec, so this one never reaches git.
        recorder = _Recorder([])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="ma+in", expected_target_head=TARGET
        )
        self.assertEqual(result.status, MERGE_INVALID_INPUT)
        self.assertEqual(recorder.argvs, [])

    def test_a_name_git_itself_rejects_is_invalid_input(self) -> None:
        # The other half of the grammar: `main..bad` and friends carry no refspec-unsafe
        # character, so only git's own validator catches them — and R12 shipped without it,
        # merging and committing for all four (measured, j#96422 finding 3).
        recorder = _Recorder([_fail("is not a valid branch name")])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main..bad", expected_target_head=TARGET
        )
        self.assertEqual(result.status, MERGE_INVALID_INPUT)
        self.assertEqual(len(recorder.argvs), 1)  # nothing was built

    def test_nothing_runs_in_a_worktree_and_nothing_switches_or_moves_a_ref(self) -> None:
        recorder = _Recorder([*self._preamble(), _ok(TREE), *self._dates(), _ok(MERGE_HEAD)])
        _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertTrue(all(cwd is None for _, cwd, _ in recorder.calls))
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
            self.assertEqual(result.status, MERGE_INVALID_INPUT, kwargs)
            self.assertEqual(recorder.argvs, [], kwargs)

    def test_a_content_conflict_names_itself_and_commits_nothing(self) -> None:
        # A real conflict exits 1 AND names the tree it produced.
        recorder = _Recorder(
            [
                *self._preamble(),
                _fail_rc(1, out=f"{TREE}\n100644 abc 1\ta.py\nCONFLICT (content)"),
            ]
        )
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertEqual(result.status, MERGE_CONTENT_CONFLICT)
        self.assertTrue(result.is_content_conflict)
        self.assertEqual(result.integration_head, "")
        self.assertEqual(len(recorder.argvs), 4)  # nothing was committed
        flat = " ".join(" ".join(argv) for argv in recorder.argvs)
        for forbidden in ("--strategy", "-X", "theirs", "ours", "rebase"):
            self.assertNotIn(forbidden, flat, forbidden)

    def test_an_operational_failure_at_exit_1_is_not_called_a_conflict(self) -> None:
        # MEASURED on real git: a missing object exits 1 exactly as a conflict does, and
        # names no tree. R10 classified on the exit code alone and wrote "the branches
        # conflict" into the durable record for an object that does not exist.
        recorder = _Recorder(
            [*self._preamble(), _fail_rc(1, err="merge-tree: 000... - not something we can merge")]
        )
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertEqual(result.status, MERGE_ERROR)
        self.assertFalse(result.is_content_conflict)
        self.assertIn("NOT a content conflict", result.detail)
        self.assertIn("NOT proof that the primitive is unavailable", result.detail)

    def test_unsupported_is_established_by_the_version_not_by_an_exit_code(self) -> None:
        recorder = _Recorder([_ok("main"), _ok("git version 2.37.9")])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertEqual(result.status, MERGE_PRIMITIVE_UNSUPPORTED)
        # It refused BEFORE attempting the merge, rather than running it and guessing from
        # whatever came back (j#96412 finding 2: an unknown exit code is not evidence).
        self.assertEqual(len(recorder.argvs), 2)

    def test_an_unanswerable_capability_question_is_not_an_answer(self) -> None:
        # R11 folded "the version command failed" and "its output was unparseable" into
        # `primitive_unsupported` — asserting a fact it had just failed to establish, in the
        # same round whose review required the opposite (j#96417 finding 3).
        for unreadable in (_fail("git: command not found"), _ok("garbage")):
            recorder = _Recorder([_ok("main"), unreadable])
            self.assertEqual(
                _adapter(recorder)
                .apply_merge(
                    source_head=SOURCE, target_ref="main", expected_target_head=TARGET
                )
                .status,
                MERGE_PROBE_ERROR,
            )

    def test_a_merge_that_names_no_tree_is_not_committed(self) -> None:
        recorder = _Recorder([*self._preamble(), _ok("")])
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertEqual(result.status, MERGE_ERROR)
        self.assertEqual(len(recorder.argvs), 4)

    def test_a_tree_that_cannot_be_committed_says_so(self) -> None:
        recorder = _Recorder(
            [*self._preamble(), _ok(TREE), *self._dates(), _fail("could not write commit")]
        )
        result = _adapter(recorder).apply_merge(
            source_head=SOURCE, target_ref="main", expected_target_head=TARGET
        )
        self.assertEqual(result.status, MERGE_COMMIT_ERROR)
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
