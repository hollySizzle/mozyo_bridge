"""Real-``git`` tests for the #13686 live adapter (Redmine #13686).

R6 review j#96391 finding 6 asked for these specifically, and the reason is on the record:
that round's findings were both properties of what ``git`` actually does, and the recording
fake could not have caught either. A fake answers what it was told to answer — it cannot tell
you that ``git merge`` will take whatever the checked-out tip happens to be as its first
parent, or what a ref-deleting primitive does to a worktree standing on that ref.

What is pinned against a real binary here is that **the merge is built from objects and
touches no checkout**, and that the same action rebuilds the same commit **on the same git
version, given the same repository content** — the limit R12-R14 kept overstating. It used to be performed inside a dedicated worktree, with that path's
identity established by an earlier probe; review j#96406 finding 1 reproduced a foreign lane's
clean checkout swapped onto the path between the probe and the merge being switched off its
own branch and having the merge commit built on it — and ``apply_merge`` returned
``conflicted=False``. A non-force push and an exact-SHA CI gate what *lands*; neither undoes a
checkout somebody else was standing in. :class:`UseCaseWorktreeSwapRegressionTest` performs
that swap in the middle of a real ``run_integration`` and asserts the foreign checkout is
untouched — R10 claimed such a test and shipped one that did neither (j#96412 finding 4).

Two more properties of the object-level merge are pinned here because a durable record depends
on them: the commit is a **function of the action** (the same action rebuilds the same SHA on
the same git version and repository content), and each failure carries **its own status** (a missing object and a
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
    MERGE_SANDBOX_ERROR,
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

    def test_the_child_process_does_not_inherit_the_parents_git_environment(self) -> None:
        """j#96428 finding 1 — and the reason this test runs real git rather than a stub.

        R13 built an allowlist environment and handed it to a ``_run`` that merged it straight
        back into ``os.environ``. The dict was right; the child process was not. The unit test
        inspected the dict, which is the wrong side of the boundary where the merge happened,
        and passed. A sealed environment is only observable from the child.

        Measured before the fix: an inherited ``GIT_OBJECT_DIRECTORY`` pointing at an empty
        directory changed the outcome, because git looked for the objects there.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "target.txt", "moved on")
            operations = LiveAutoIntegrationGitOperations(repo_root=repo)

            baseline = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=target
            )
            self.assertEqual(baseline.status, MERGE_MERGED, baseline.detail)

            empty_objects = root / "empty-objects"
            empty_objects.mkdir()
            hostile = {
                "GIT_OBJECT_DIRECTORY": str(empty_objects),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "i18n.commitEncoding",
                "GIT_CONFIG_VALUE_0": "ISO-8859-1",
            }
            previous = {name: os.environ.get(name) for name in hostile}
            os.environ.update(hostile)
            try:
                sealed = operations.apply_merge(
                    source_head=source, target_ref="main", expected_target_head=target
                )
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

            self.assertEqual(sealed.status, MERGE_MERGED, sealed.detail)
            self.assertEqual(sealed.integration_head, baseline.integration_head)

    def test_a_replace_ref_cannot_change_the_commit_through_the_timestamp(self) -> None:
        """j#96428 finding 2: R13 sealed the merge and left the date reads in the open.

        A replace ref substitutes one object for another everywhere git looks — including the
        ``git show`` that decides the merge commit's timestamps. Measured: a replacement whose
        tree was identical but whose committer date differed moved the integration head.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "target.txt", "moved on")
            operations = LiveAutoIntegrationGitOperations(repo_root=repo)

            before = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=target
            )
            self.assertEqual(before.status, MERGE_MERGED, before.detail)

            # Same tree, same parent, different committer date.
            tree = _git(repo, "rev-parse", f"{source}^{{tree}}")
            parent = _git(repo, "rev-parse", f"{source}^")
            replacement = subprocess.run(
                ["git", "commit-tree", tree, "-p", parent, "-m", "lane"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_DATE": "2030-01-01T00:00:00+00:00",
                    "GIT_COMMITTER_DATE": "2030-01-01T00:00:00+00:00",
                },
            ).stdout.strip()
            _git(repo, "replace", "-f", source, replacement)

            after = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=target
            )
            self.assertEqual(after.status, MERGE_MERGED, after.detail)
            self.assertEqual(after.integration_head, before.integration_head)

    def test_hostile_repository_state_cannot_reach_the_merge(self) -> None:
        """R14 review j#96435 finding 1, adversarially: the state is present, and inert.

        R12-R14 probed for a merge driver and an ``info/attributes`` file and refused when
        either was found. The reviewer added a driver *between* the probe and the merge and
        watched its shell command run and rewrite the merged content — a check in one
        invocation cannot bind a mutation in another, which is the defect that retired three
        destructive operations. There is no probe now: the merge runs in a git directory where
        this state does not exist, so each of these is present throughout and changes nothing.

        ``.git/shallow`` is here for the same reason (finding 3): in the repository it turned
        the merge into ``refusing to merge unrelated histories``; in the sanitized directory
        the merge is ordinary. And ``info/attributes`` is exercised as a *directory* as well as
        a file, because R14's presence check used ``is_file()`` and missed that (finding 2) —
        a distinction that stops mattering once nothing is being checked.
        """
        def build(repo: Path) -> tuple:
            _init(repo)
            (repo / ".gitattributes").write_text("f.txt merge=mine\n", encoding="utf-8")
            _commit(repo, "f.txt", "base\n")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "f.txt", "lane\n")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "f.txt", "target\n")
            return source, target

        def git_dir(repo: Path) -> Path:
            return Path(_git(repo, "rev-parse", "--absolute-git-dir"))

        hostilities = {
            "merge driver": lambda repo: _git(
                repo, "config", "merge.mine.driver", "printf 'DRIVER WON\\n' > %A"
            ),
            "info/attributes file": lambda repo: (
                (git_dir(repo) / "info").mkdir(exist_ok=True),
                (git_dir(repo) / "info" / "attributes").write_text(
                    "f.txt merge=union\n", encoding="utf-8"
                ),
            ),
            "info/attributes directory": lambda repo: (
                git_dir(repo) / "info" / "attributes"
            ).mkdir(parents=True, exist_ok=True),
            "shallow": lambda repo: (git_dir(repo) / "shallow").write_text(
                f"{_git(repo, 'rev-parse', 'main')}\n{_git(repo, 'rev-parse', 'lane')}\n",
                encoding="utf-8",
            ),
        }

        for label, make_hostile in hostilities.items():
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                source, target = build(repo)
                operations = LiveAutoIntegrationGitOperations(repo_root=repo)
                before = operations.apply_merge(
                    source_head=source, target_ref="main", expected_target_head=target
                )
                make_hostile(repo)
                after = operations.apply_merge(
                    source_head=source, target_ref="main", expected_target_head=target
                )
                self.assertEqual(after.status, before.status, label)
                self.assertEqual(after.integration_head, before.integration_head, label)
                # ...and the run is not refused either: a present-but-unreachable input is
                # not a hazard to report (the false positive of j#96422 finding 4).
                self.assertNotEqual(after.status, MERGE_NONDETERMINISTIC_CONFIG, label)

    def test_every_repository_shape_produces_the_same_commit(self) -> None:
        """j#96441 finding 2 — including the shape this lane itself is.

        R15 located the object store by appending ``objects`` to ``--absolute-git-dir``. In a
        linked worktree that answers ``$GIT_COMMON_DIR/worktrees/<name>``, which holds no
        object database, so the merge failed outright — and the lane this issue is developed
        in *is* a linked worktree. Every scene I had tested was a plain checkout. The store is
        resolved from ``--git-common-dir`` now, and all three shapes are pinned because
        "it works here" was exactly the assumption that failed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "target.txt", "moved on")

            linked = root / "linked"
            _git(repo, "branch", "work")
            _git(repo, "worktree", "add", "-q", str(linked), "work")
            bare = root / "bare.git"
            subprocess.run(
                ["git", "clone", "--bare", "-q", str(repo), str(bare)],
                check=True, capture_output=True,
            )

            results = {}
            for shape, root_path in (
                ("normal checkout", repo),
                ("linked worktree", linked),
                ("bare repository", bare),
            ):
                results[shape] = LiveAutoIntegrationGitOperations(
                    repo_root=root_path
                ).apply_merge(
                    source_head=source, target_ref="main", expected_target_head=target
                )

            for shape, result in results.items():
                self.assertEqual(result.status, MERGE_MERGED, f"{shape}: {result.detail}")
                self.assertTrue(result.git_version, shape)
            self.assertEqual(
                len({result.integration_head for result in results.values()}),
                1,
                {shape: result.integration_head for shape, result in results.items()},
            )

    def test_a_ref_whose_meaning_depends_on_the_repository_is_refused(self) -> None:
        """j#96447 finding 1: `check-ref-format --branch` is not a validator.

        It also expands `@{-n}` into whatever branch was checked out n switches ago, so it
        reads repository state — measured, `@{-1}` returned rc=0 and `other` here and rc=128
        under a different `GIT_DIR`. R16 then threw the expansion away and kept `@{-1}` as the
        target, which would have built a merge for a ref no push could use. The literal form
        is checked now, in a sealed environment, so the answer cannot depend on where the
        process happens to be standing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "target.txt", "moved on")
            # A previous checkout, so `@{-1}` resolves for real git.
            _git(repo, "checkout", "-q", "-b", "other")
            _git(repo, "checkout", "-q", "main")
            self.assertEqual(
                subprocess.run(
                    ["git", "check-ref-format", "--branch", "@{-1}"],
                    cwd=repo, capture_output=True, text=True,
                ).stdout.strip(),
                "other",
                "the scene must be one where --branch really expands",
            )

            operations = LiveAutoIntegrationGitOperations(repo_root=repo)
            refused = operations.apply_merge(
                source_head=source, target_ref="@{-1}", expected_target_head=target
            )
            self.assertEqual(refused.status, MERGE_INVALID_INPUT)
            self.assertEqual(refused.integration_head, "")

            # ...and the same answer from somewhere else entirely.
            elsewhere = root / "elsewhere"
            _init(elsewhere)
            previous = os.environ.get("GIT_DIR")
            os.environ["GIT_DIR"] = str(elsewhere / ".git")
            try:
                self.assertEqual(
                    operations.apply_merge(
                        source_head=source, target_ref="@{-1}", expected_target_head=target
                    ).status,
                    MERGE_INVALID_INPUT,
                )
            finally:
                if previous is None:
                    os.environ.pop("GIT_DIR", None)
                else:
                    os.environ["GIT_DIR"] = previous

    def test_a_sandbox_that_cannot_be_removed_is_not_a_success(self) -> None:
        """j#96453 finding 1: cleanup failure was swallowed and reported as `merged`.

        Two things were wrong and the second is worse. R17 decided on its own that a leaked
        sandbox was acceptable and returned `merged` — a change to the contract accepted in
        j#96449, made without a ruling. And the test it shipped alongside called the real
        cleanup first and *then* raised, so the directory was always removed: the leak path it
        claimed to cover never executed. This injection removes nothing and asserts the
        directory survives, so a regression cannot pass by tidying up behind itself.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "target.txt", "moved on")

            original = tempfile.TemporaryDirectory.cleanup
            leaked: list = []

            def refusing(self) -> None:
                leaked.append(self.name)
                raise OSError("simulated: the sandbox cannot be removed")

            tempfile.TemporaryDirectory.cleanup = refusing
            try:
                result = LiveAutoIntegrationGitOperations(repo_root=repo).apply_merge(
                    source_head=source, target_ref="main", expected_target_head=target
                )
            finally:
                tempfile.TemporaryDirectory.cleanup = original
                for path in leaked:
                    shutil.rmtree(path, ignore_errors=True)

            self.assertEqual(result.status, MERGE_SANDBOX_ERROR, result.detail)
            self.assertEqual(result.integration_head, "")
            self.assertTrue(leaked, "the injection must have been reached")

    def test_a_ref_carrying_a_control_character_is_invalid_input(self) -> None:
        """j#96453 finding 2: a NUL made `subprocess.run` raise before git was spawned.

        `ValueError` is not an `OSError`, so it went straight past the adapter's fail-closed
        wrapper and out of `apply_merge` — for an input the port contract says must come back
        as `invalid_input`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "a.txt", "base")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "lane.txt", "reviewed")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "target.txt", "moved on")
            operations = LiveAutoIntegrationGitOperations(repo_root=repo)

            for name in ("main\x00bad", "refs/heads/main\x00bad", "main\tbad", "main\x7f"):
                result = operations.apply_merge(
                    source_head=source, target_ref=name, expected_target_head=target
                )
                self.assertEqual(result.status, MERGE_INVALID_INPUT, repr(name))
                self.assertEqual(result.integration_head, "", repr(name))

    def test_an_init_template_cannot_seed_the_sandbox(self) -> None:
        """j#96441 finding 1: the sandbox's own creation was running unsealed.

        Measured — a ``GIT_TEMPLATE_DIR`` in the parent process placed an ``info/attributes``
        into the "empty" sandbox and turned a content conflict into a clean merge. Sealing
        what runs *inside* an isolation while building it in the open is not an isolation.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init(repo)
            (repo / ".gitattributes").write_text("f.txt merge=text\n", encoding="utf-8")
            _commit(repo, "f.txt", "base\n")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "f.txt", "lane\n")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "f.txt", "target\n")
            operations = LiveAutoIntegrationGitOperations(repo_root=repo)

            baseline = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=target
            )
            self.assertEqual(baseline.status, MERGE_CONTENT_CONFLICT)

            template = root / "template"
            (template / "info").mkdir(parents=True)
            (template / "info" / "attributes").write_text(
                "f.txt merge=union\n", encoding="utf-8"
            )
            previous = os.environ.get("GIT_TEMPLATE_DIR")
            os.environ["GIT_TEMPLATE_DIR"] = str(template)
            try:
                injected = operations.apply_merge(
                    source_head=source, target_ref="main", expected_target_head=target
                )
            finally:
                if previous is None:
                    os.environ.pop("GIT_TEMPLATE_DIR", None)
                else:
                    os.environ["GIT_TEMPLATE_DIR"] = previous
            self.assertEqual(injected.status, MERGE_CONTENT_CONFLICT, injected.detail)

    def test_the_merge_driver_would_have_fired_without_the_sanitized_directory(self) -> None:
        """The scene above is only meaningful if raw git really would run the driver."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            (repo / ".gitattributes").write_text("f.txt merge=mine\n", encoding="utf-8")
            _commit(repo, "f.txt", "base\n")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "f.txt", "lane\n")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "f.txt", "target\n")
            _git(repo, "config", "merge.mine.driver", "printf 'DRIVER WON\\n' > %A")

            raw = subprocess.run(
                ["git", "merge-tree", "--write-tree", target, source],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(raw.returncode, 0, "the driver must turn the conflict clean")
            content = subprocess.run(
                ["git", "cat-file", "-p", f"{raw.stdout.strip()}:f.txt"],
                cwd=repo, capture_output=True, text=True, check=True,
            ).stdout
            self.assertIn("DRIVER WON", content)

            # The adapter, on the same repository, is untouched by it.
            result = LiveAutoIntegrationGitOperations(repo_root=repo).apply_merge(
                source_head=source, target_ref="main", expected_target_head=target
            )
            self.assertEqual(result.status, MERGE_CONTENT_CONFLICT)

    def test_merge_semantics_config_cannot_change_the_result(self) -> None:
        """`merge.default` picks a driver for paths no attribute names (j#96428 finding 3)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init(repo)
            _commit(repo, "f.txt", "base\n")
            _git(repo, "checkout", "-q", "-b", "lane")
            source = _commit(repo, "f.txt", "lane\n")
            _git(repo, "checkout", "-q", "main")
            target = _commit(repo, "f.txt", "target\n")
            operations = LiveAutoIntegrationGitOperations(repo_root=repo)

            baseline = operations.apply_merge(
                source_head=source, target_ref="main", expected_target_head=target
            )
            for key, value in (
                ("merge.default", "union"),
                ("merge.renormalize", "true"),
            ):
                _git(repo, "config", key, value)
                self.assertEqual(
                    operations.apply_merge(
                        source_head=source, target_ref="main", expected_target_head=target
                    ).status,
                    baseline.status,
                    key,
                )
                _git(repo, "config", "--unset", key)

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
