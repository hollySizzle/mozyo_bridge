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
checkout somebody else was standing in. :class:`UseCaseWorktreeSwapRegressionTest` performs
that swap in the middle of a real ``run_integration`` and asserts the foreign checkout is
untouched — R10 claimed such a test and shipped one that did neither (j#96412 finding 4).

Two more properties of the object-level merge are pinned here because a durable record depends
on them: the commit is a **function of the action** (the same action rebuilds the same SHA on
any host at any time), and each failure carries **its own status** (a missing object and a
content conflict both exit 1, and calling the first a conflict is a lie the record would keep).

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

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_actuator import (
    MERGE_CONTENT_CONFLICT,
    MERGE_ERROR,
    MERGE_MERGED,
    MERGE_INVALID_INPUT,
    MERGE_NONDETERMINISTIC_CONFIG,
    AutoIntegrationUseCase,
    IntegrationAuthority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_ops import (
    ACTUATOR_IDENTITY_EMAIL,
    ACTUATOR_IDENTITY_NAME,
    LiveAutoIntegrationGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_policy import (
    MODE_AUTO,
    STEP_INTEGRATION_APPLY,
    AutoIntegrationPolicy,
    IntegrationCiEvidence,
    build_integration_action_record,
)


@dataclass
class _FullyAuthorizedReader:
    """Every durable gate satisfied, so the run reaches the apply and the git facts decide."""

    source_head: str

    def read_integration_authority(self, *, record) -> IntegrationAuthority:
        return IntegrationAuthority(
            review_generation_admissible=True,
            reviewed_head=self.source_head,
            target_identity_known=True,
            callbacks_drained=True,
            owner_gates_resolved=True,
            source_ci=IntegrationCiEvidence(
                integration_head=self.source_head,
                workflow="required-ci",
                run="src-1",
                conclusion="success",
            ),
        )

    def read_integration_ci(self, *, record, integration_head):
        return None

    def read_cleanup_authority(self, *, record):  # pragma: no cover - unused here
        raise AssertionError("the integration path must not read cleanup authority")

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
            self.assertEqual(result.status, MERGE_CONTENT_CONFLICT)
            self.assertIn("the branches conflict in content", result.detail)
            self.assertEqual(result.integration_head, "")
            self.assertEqual(_git(repo, "rev-parse", "main"), target)
            self.assertEqual(_git(repo, "status", "--porcelain"), "")


@unittest.skipIf(_GIT is None, "git is not available on PATH")
class DeterministicMergeCommitTest(unittest.TestCase):
    """The same action rebuilds the same commit — R10 review j#96412 finding 1.

    Measured before the fix: one action produced two different SHAs a second apart, because
    ``commit-tree`` reads the host's ``user.name`` and the clock. The action key covers
    neither, so a crash between the apply and the ledger receipt left a replay building a
    *different* object than the one CI would be asked about.
    """

    def _merge_twice(self, repo: Path, *, name: str, email: str) -> str:
        _git(repo, "config", "user.name", name)
        _git(repo, "config", "user.email", email)
        base = _git(repo, "rev-parse", "main")
        source = _git(repo, "rev-parse", "lane")
        operations = LiveAutoIntegrationGitOperations(repo_root=repo)
        result = operations.apply_merge(
            source_head=source, target_ref="main", expected_target_head=base
        )
        self.assertEqual(result.status, MERGE_MERGED, result.detail)
        return result.integration_head

    def test_replaying_the_same_action_rebuilds_the_same_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")

            first = self._merge_twice(repo, name="actuator", email="a@example.invalid")
            time.sleep(1.1)  # the clock has moved past a whole second
            second = self._merge_twice(repo, name="actuator", email="a@example.invalid")
            self.assertEqual(first, second)

            # ...and a different host, with different git identity configuration, agrees.
            third = self._merge_twice(repo, name="somebody else", email="b@example.invalid")
            self.assertEqual(first, third)

    def test_git_config_cannot_change_the_commit(self) -> None:
        """j#96417 finding 1: `i18n.commitEncoding` produced a different SHA for one action.

        Measured before the fix — the encoding header changes the commit object. The same
        went for anything a *global* config could set, since the adapter inherited it. Both
        are pinned per-invocation now, and this drives the actual knobs rather than trusting
        the argv.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")
            base = _git(repo, "rev-parse", "main")
            source = _git(repo, "rev-parse", "lane")
            operations = LiveAutoIntegrationGitOperations(repo_root=repo)

            def merged() -> str:
                result = operations.apply_merge(
                    source_head=source, target_ref="main", expected_target_head=base
                )
                self.assertEqual(result.status, MERGE_MERGED, result.detail)
                return result.integration_head

            baseline = merged()

            # A repo-local encoding, which `-c` must override.
            _git(repo, "config", "i18n.commitEncoding", "ISO-8859-1")
            self.assertEqual(merged(), baseline)

            # A global config, which the isolated environment must not see.
            global_config = root / "gitconfig"
            global_config.write_text(
                "[i18n]\n\tcommitEncoding = ISO-8859-1\n", encoding="utf-8"
            )
            previous = os.environ.get("GIT_CONFIG_GLOBAL")
            os.environ["GIT_CONFIG_GLOBAL"] = str(global_config)
            try:
                self.assertEqual(merged(), baseline)
            finally:
                if previous is None:
                    os.environ.pop("GIT_CONFIG_GLOBAL", None)
                else:
                    os.environ["GIT_CONFIG_GLOBAL"] = previous

    def test_merge_config_cannot_change_the_tree(self) -> None:
        """j#96422 finding 1: `merge.directoryRenames` produced a different tree per host.

        Measured before the fix. Unlike a custom driver, these keys have canonical names, so
        `-c` can pin them — which is the distinction R12 missed when it treated the driver as
        the only unpinnable input.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            (repo / "dir").mkdir()
            for index in range(3):
                (repo / "dir" / f"f{index}.txt").write_text(
                    f"content {index}\n" * 20, encoding="utf-8"
                )
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            (repo / "dir" / "new.txt").write_text("new\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "lane adds a file")
            source = _git(repo, "rev-parse", "HEAD")
            _git(repo, "checkout", "-q", "main")
            _git(repo, "mv", "dir", "moved")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "target renames the directory")
            target = _git(repo, "rev-parse", "HEAD")

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            outcomes = []
            for value in ("false", "true", "conflict"):
                _git(repo, "config", "merge.directoryRenames", value)
                outcomes.append(
                    operations.apply_merge(
                        source_head=source,
                        target_ref="main",
                        expected_target_head=target,
                    )
                )
            # Raw git disagrees with itself across those settings; the adapter does not.
            raw = {
                subprocess.run(
                    ["git", "-c", f"merge.directoryRenames={value}",
                     "merge-tree", "--write-tree", target, source],
                    cwd=repo, capture_output=True, text=True,
                ).stdout.strip()
                for value in ("false", "true")
            }
            self.assertEqual(len(raw), 2, "the scene must be sensitive to the setting")
            self.assertEqual(
                len({(outcome.status, outcome.integration_head) for outcome in outcomes}),
                1,
                [outcome.status for outcome in outcomes],
            )

    def test_a_ref_git_itself_rejects_never_reaches_an_object(self) -> None:
        """j#96422 finding 3: four unusable branch names merged and produced commits."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")
            target = _git(repo, "rev-parse", "main")
            operations = LiveAutoIntegrationGitOperations(repo_root=repo)

            for name in ("main..bad", "main.lock", "main@{bad", "main//bad", "ma+in"):
                result = operations.apply_merge(
                    source_head=source, target_ref=name, expected_target_head=target
                )
                self.assertEqual(result.status, MERGE_INVALID_INPUT, name)
                self.assertEqual(result.integration_head, "", name)

    def test_a_driver_the_merge_would_never_see_does_not_refuse_it(self) -> None:
        """j#96422 finding 4: an unused driver in GLOBAL config refused a clean merge.

        The merge isolates global config; R12's probe did not, so it reported a determinism
        hazard about an input that could not reach the operation it was guarding.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")
            target = _git(repo, "rev-parse", "main")

            global_config = root / "gitconfig"
            global_config.write_text(
                '[merge "unused"]\n\tdriver = false\n', encoding="utf-8"
            )
            previous = os.environ.get("GIT_CONFIG_GLOBAL")
            os.environ["GIT_CONFIG_GLOBAL"] = str(global_config)
            try:
                result = LiveAutoIntegrationGitOperations(repo_root=repo).apply_merge(
                    source_head=source, target_ref="main", expected_target_head=target
                )
            finally:
                if previous is None:
                    os.environ.pop("GIT_CONFIG_GLOBAL", None)
                else:
                    os.environ["GIT_CONFIG_GLOBAL"] = previous
            self.assertEqual(result.status, MERGE_MERGED, result.detail)

    def test_a_merge_driver_is_refused_rather_than_silently_obeyed(self) -> None:
        """The input that cannot be pinned, so it is checked instead.

        Measured: a configured `merge.<name>.driver` selected by an in-tree `.gitattributes`
        turns a conflict into a clean merge whose content is whatever the driver wrote. It
        lives in repo-local config, which `GIT_CONFIG_GLOBAL`/`SYSTEM` do not cover, and its
        name comes from the tree so it cannot be enumerated in advance. An actuator promising
        that the same action rebuilds the same commit cannot keep that promise here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            (repo / ".gitattributes").write_text("a.txt merge=mine\n", encoding="utf-8")
            _commit(repo, "a.txt", "base\n")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "a.txt", "lane\n")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "a.txt", "target\n")
            operations = LiveAutoIntegrationGitOperations(repo_root=repo)

            # Without the driver configured this is an ordinary content conflict.
            self.assertEqual(
                operations.apply_merge(
                    source_head=source, target_ref="main", expected_target_head=target
                ).status,
                MERGE_CONTENT_CONFLICT,
            )

            # With it, real git would produce a clean tree of the driver's choosing...
            _git(repo, "config", "merge.mine.driver", "printf 'DRIVER WON\\n' > %A")
            direct = subprocess.run(
                ["git", "merge-tree", "--write-tree", target, source],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(direct.returncode, 0)
            # ...and the adapter refuses instead of committing what it cannot reproduce.
            refused = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=target
            )
            self.assertEqual(refused.status, MERGE_NONDETERMINISTIC_CONFIG)
            self.assertEqual(refused.integration_head, "")

    def test_the_commit_says_it_was_not_written_by_a_person(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")
            base = _git(repo, "rev-parse", "main")

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            head = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=base
            ).integration_head

            author = _git(repo, "show", "-s", "--format=%an <%ae>", head)
            self.assertEqual(
                author, f"{ACTUATOR_IDENTITY_NAME} <{ACTUATOR_IDENTITY_EMAIL}>"
            )
            self.assertEqual(
                _git(repo, "show", "-s", "--format=%an <%ae>", head),
                _git(repo, "show", "-s", "--format=%cn <%ce>", head),
            )
            # The timestamp is the SOURCE's, not the clock's: a value the action key covers.
            self.assertEqual(
                _git(repo, "show", "-s", "--format=%cI", head),
                _git(repo, "show", "-s", "--format=%cI", source),
            )


@unittest.skipIf(_GIT is None, "git is not available on PATH")
class MergeFailureClassificationTest(unittest.TestCase):
    """Real git, real failures: what the adapter calls each one — j#96412 finding 2."""

    def test_a_content_conflict_and_a_missing_object_are_different_statuses(self) -> None:
        # Both exit 1. R10 classified on the exit code and recorded "the branches conflict"
        # for an object that does not exist.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base\n")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "a.txt", "lane version\n")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "a.txt", "target version\n")
            operations = LiveAutoIntegrationGitOperations(repo_root=repo)

            conflict = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=target
            )
            self.assertEqual(conflict.status, MERGE_CONTENT_CONFLICT)

            absent = operations.apply_merge(
                source_head="0" * 40, target_ref="main", expected_target_head=target
            )
            self.assertEqual(absent.status, MERGE_ERROR)
            self.assertNotEqual(absent.status, conflict.status)


@unittest.skipIf(_GIT is None, "git is not available on PATH")
class UseCaseWorktreeSwapRegressionTest(unittest.TestCase):
    """R9 review j#96406 finding 1, driven through the USE CASE against a real remote.

    R10 claimed a use-case-level regression test for this and shipped one that called the
    adapter directly and never performed the swap (j#96412 finding 4 — the claim was mine and
    it was false). This is the test that claim described: a real bare remote, the real live
    adapter, ``AutoIntegrationUseCase.run_integration``, and a foreign lane's checkout moved
    onto the contested path *in the middle of the run* — between the actuator's own
    measurement and the apply, which is exactly where the old form lost.
    """

    def test_a_foreign_checkout_swapped_in_mid_run_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "origin.git"
            subprocess.run(
                ["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True
            )
            repo = root / "repo"
            _init(repo)
            base = _commit(repo, "a.txt", "base")
            _git(repo, "remote", "add", "origin", str(remote))
            _git(repo, "push", "-q", "origin", "main")

            lane_worktree = root / "lane_wt"
            _git(repo, "branch", "lane")
            _git(repo, "worktree", "add", "-q", str(lane_worktree), "lane")
            source = _commit(lane_worktree, "lane.txt", "reviewed")
            _git(lane_worktree, "push", "-q", "origin", "lane")

            # The target moves, so a fast-forward is impossible and the run must take the
            # merge-commit disposition — the path this regression is about.
            _commit(repo, "target.txt", "moved on")
            _git(repo, "push", "-q", "origin", "main")
            base = _git(repo, "rev-parse", "main")

            # The branch the swap will put at the lane's own path, mid-run.
            _git(repo, "branch", "foreign_lane")
            foreign_head = _git(repo, "rev-parse", "foreign_lane")

            swapped: list[str] = []

            class SwappingOperations(LiveAutoIntegrationGitOperations):
                """Real adapter that PERFORMS the swap once the preflight has measured.

                R11 shipped this class with a body that appended to a list and swapped
                nothing, while its docstring and my journal both said otherwise (j#96417
                finding 4 — the second round running). It does the thing now: the lane's
                worktree is removed and a foreign lane's checkout is put at the same path,
                after the measurement and before the apply.
                """

                def describe_lane_worktree(self, *, path: str):
                    described = super().describe_lane_worktree(path=path)
                    if not swapped:
                        swapped.append(path)
                        _git(repo, "worktree", "remove", path)
                        _git(repo, "worktree", "add", "-q", path, "foreign_lane")
                    return described

            operations = SwappingOperations(repo_root=repo)
            use_case = AutoIntegrationUseCase(
                operations=operations,
                integration_policy=AutoIntegrationPolicy(
                    mode=MODE_AUTO, integration_branch="main", ff_only=False
                ),
                authority=_FullyAuthorizedReader(source_head=source),
                lane_worktree=str(lane_worktree),
                lane_branch="lane",
                lane_issue="13686",
                lane_generation=1,
            )
            report = use_case.run_integration(
                build_integration_action_record(
                    configured_branch="main",
                    issue="13686",
                    lane_generation=1,
                    source_head=source,
                    expected_target_head=base,
                    review_generation="1",
                )
            )

            self.assertEqual(
                swapped, [str(lane_worktree)], "the swap must have happened, once"
            )
            # The swap really took: that path now holds the foreign lane.
            self.assertEqual(
                _git(lane_worktree, "rev-parse", "--abbrev-ref", "HEAD"), "foreign_lane"
            )
            # And the run touched none of it. Under the worktree merge this checkout would
            # have been switched to `main` with the merge commit built on top.
            self.assertEqual(_git(lane_worktree, "rev-parse", "HEAD"), foreign_head)
            self.assertEqual(_git(lane_worktree, "status", "--porcelain"), "")
            self.assertEqual(_git(repo, "rev-parse", "foreign_lane"), foreign_head)
            # Not a vacuous pass: the run reached the apply, and the apply succeeded.
            applied = [
                outcome
                for outcome in report.outcomes
                if outcome.step == STEP_INTEGRATION_APPLY
            ]
            self.assertEqual(len(applied), 1)
            self.assertEqual(applied[0].merge_status, MERGE_MERGED)


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
