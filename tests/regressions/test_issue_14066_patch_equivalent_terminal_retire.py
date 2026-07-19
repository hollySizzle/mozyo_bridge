"""Regression pins for the #14066 patch-equivalent terminal retire.

Redmine #14066 (parent #13490). The #13845 hibernated bound terminal retire accepts a lane head
as integrated ONLY when ``--branch`` is a **literal ancestor** of ``--integration-branch``. But
the workflow's integration disposition (central preset ``統合責務``) also admits a
``patch_equivalent`` integration: the coordinator cherry-picks the review-approved commits onto
the integration / staging branch and records the stable patch-id / commit map in a durable
integration journal. There the original issue branch is NOT an ancestor of the integration
branch (the cherry-picks carry different commit hashes), so ``merge-base --is-ancestor`` reports
``False`` forever and a drained, closed, hibernated / released lane can never reach the terminal
``retired`` disposition (``head_not_integrated``). The live residual: #13846 (two rows) and
#13879 (one row) — all resident process 0, issue closed, Review / CI / installed acceptance done.

The AUTHORITY is the EXACT Redmine integration journal, read fresh at action-time through the
credential-gated live journal source (review j#82298 F1): the coordinator embeds the structured
disposition as a ``mozyo-patch-equivalent-integration`` fenced block in the durable journal, and
the retire re-reads that exact journal, RECOMPUTES the git facts (stable patch-ids, origin
reachability against the derived ``origin/<integration_branch>`` ref), and terminalizes only when
the recomputed patch-ids prove every mapped cherry-pick equivalent, the recorded heads match the
current branches, and the integration head is origin-reachable. A caller-supplied local file is
never the authority; passing a fake integration branch or an arbitrary local ref does not work.

The literal-ancestor path is byte-identical to #13845: when ``--branch`` is a literal ancestor
the retire never even constructs the patch-equivalent resolver (review j#82298 F2), so no file
IO / git probe / Redmine read / exception surface is added to that path.

Layers, all synthetic (isolated ``MOZYO_BRIDGE_HOME``, a fake herdr inventory, a fake injected
Redmine journal source — never a real network / credential, real local git repos with real
cherry-picks, never a live pane / process / route mutation, never a git push/fetch by the tool):

1. the pure fence (:func:`evaluate_patch_equivalent_integrated`) — the claim-vs-recomputed-facts
   matrix, every refusal axis;
2. the disposition journal block (render / parse / project) — round-trip, ambiguous, malformed;
3. the credential-gated exact-journal read (:func:`read_integration_disposition`) — unconfigured
   / unreadable / not-found / absent / ambiguous / malformed all zero-write;
4. the action-time resolver over real cherry-picked git — green, stale head, tampered patch-id,
   fabricated integration commit, origin-unreachable (bound to origin/<branch>); and
5. the command boundary over the three residual-lane journal shapes — positive / negative /
   replay, the literal ``head_not_integrated`` non-regression when no journal is supplied, and
   the F2 non-regression that a literal-ancestor green lane never calls the resolver.

Boundary (Redmine #14066): no process launch / close / resume, no worktree / branch removal, no
raw Herdr / tmux, no origin/main, no production / tag / publish, no git push/fetch or Redmine
write by the tool.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    LaneLifecycleKey,
    LaneLifecycleStore,
    RELEASE_RELEASED,
    ReleasePin,
    DecisionPointer,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    ProcessGenerationPin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_herdr_projection,
    sublane_herdr_retire,
    sublane_lifecycle_command,
    sublane_patch_equivalent_integration as spe,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E402,E501
    LiveRedmineJournalError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E402,E501
    HerdrRetireCloseResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_retire import (  # noqa: E402,E501
    BOUND_RETIRE_ALREADY_RETIRED,
    BOUND_RETIRE_BLOCKED,
    BOUND_RETIRE_HEAD_NOT_INTEGRATED,
    BOUND_RETIRE_LIVE_PAIR_PRESENT,
    BOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED,
    BOUND_RETIRE_RETIRED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_patch_equivalent_integration import (  # noqa: E402,E501
    PE_DISPOSITION_ABSENT,
    PE_DISPOSITION_AMBIGUOUS,
    PE_DISPOSITION_MALFORMED,
    PE_JOURNAL_NOT_FOUND,
    PE_PROBE_UNRESOLVED,
    PE_REDMINE_UNCONFIGURED,
    PE_REDMINE_UNREADABLE,
    probe_patch_equivalent_observation,
    read_integration_disposition,
    resolve_patch_equivalent_integration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.patch_equivalent_integration import (  # noqa: E402,E501
    PE_BRANCH_MISMATCH,
    PE_COMMIT_MAP_INCOMPLETE,
    PE_EMPTY_MAP,
    PE_INTEGRATION_BRANCH_MISMATCH,
    PE_INTEGRATION_COMMIT_UNREACHABLE,
    PE_INTEGRATION_HEAD_STALE,
    PE_INTEGRATION_COMMIT_REUSED,
    PE_ISSUE_MISMATCH,
    PE_LANE_MISMATCH,
    PE_OK,
    PE_ORIGIN_UNREACHABLE,
    PE_PATCH_ID_MISMATCH,
    PE_PATCH_ID_UNRESOLVED,
    PE_SOURCE_HEAD_STALE,
    CommitPatchMapping,
    PatchEquivalentDisposition,
    PatchEquivalentObservation,
    disposition_from_block,
    disposition_from_mapping,
    evaluate_patch_equivalent_integrated,
    parse_integration_disposition_blocks,
    render_integration_disposition_block,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E402,E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    derive_lane_workspace_token,
    encode_assigned_name,
)

_WORKSPACE_ID = "b3d17ac95e6f4802"
_INTEGRATION_BRANCH = "int_13472_session_continuity"
_INTEGRATION_JOURNAL = "82290"


def _git(*args: str, cwd: Path, capture: bool = False):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _rev_parse(cwd: Path, ref: str) -> str:
    return _git("rev-parse", ref, cwd=cwd, capture=True).stdout.strip()


def _patch_id(cwd: Path, sha: str) -> str:
    show = subprocess.run(
        ["git", "show", "--no-color", "--format=", sha],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    pid = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=show.stdout,
        text=True,
        capture_output=True,
    )
    out = pid.stdout.strip()
    return out.split()[0] if out else ""


# ---------------------------------------------------------------------------
# A fake, injectable Redmine journal source (no network, no credentials).
# ---------------------------------------------------------------------------


class _FakeJournalSource:
    """Stand-in for ``LiveRedmineJournalSource`` installed on the app module for a test.

    ``from_environment`` raises when ``configured`` is False (the unconfigured-credentials fail
    -closed shape); ``read_entries`` raises when ``read_error`` is set (the unreadable-Redmine
    shape). Otherwise it returns the pre-built entries — exactly the port ``read_integration_
    disposition`` consumes, so no real network / credential is ever touched.
    """

    entries: list = []
    configured: bool = True
    read_error: bool = False
    reads: list = []

    @classmethod
    def from_environment(cls):
        if not cls.configured:
            raise LiveRedmineJournalError("live Redmine poll is unconfigured (test)")
        return cls()

    def read_entries(self, issue_id):
        type(self).reads.append(str(issue_id))
        if type(self).read_error:
            raise LiveRedmineJournalError("redmine transport failed (test)")
        return list(type(self).entries)


def _install_fake_journal(
    test: unittest.TestCase,
    entries: list | None = None,
    *,
    configured: bool = True,
    read_error: bool = False,
) -> None:
    """Install a fresh fake journal source on the app module for the duration of ``test``."""
    fake = type(
        "FakeJournalSource",
        (_FakeJournalSource,),
        {
            "entries": list(entries or []),
            "configured": configured,
            "read_error": read_error,
            "reads": [],
        },
    )
    real = spe.LiveRedmineJournalSource
    spe.LiveRedmineJournalSource = fake
    test.addCleanup(lambda: setattr(spe, "LiveRedmineJournalSource", real))
    test._fake_journal = fake


def _entry(journal_id: str, notes: str, issue: str) -> RedmineJournalEntry:
    return RedmineJournalEntry(issue_id=issue, journal_id=journal_id, notes=notes)


# ---------------------------------------------------------------------------
# 1. The pure fence matrix (no git, no IO).
# ---------------------------------------------------------------------------


class PatchEquivalentFenceMatrix(unittest.TestCase):
    """``evaluate_patch_equivalent_integrated`` claim-vs-recomputed-facts matrix."""

    ISSUE = "13846"
    LANE = "issue_13846_fresh_generation_binding"
    BRANCH = "issue_13846_fresh_generation_binding"

    def _disposition(self, **over) -> PatchEquivalentDisposition:
        base = dict(
            issue=self.ISSUE,
            lane=self.LANE,
            branch=self.BRANCH,
            integration_branch=_INTEGRATION_BRANCH,
            source_head="s" * 40,
            integration_head="i" * 40,
            origin_reachable=True,
            commit_map=(
                CommitPatchMapping("a" * 40, "A" * 40, "pid_a"),
                CommitPatchMapping("b" * 40, "B" * 40, "pid_b"),
            ),
        )
        base.update(over)
        return PatchEquivalentDisposition(**base)

    def _observation(self, **over) -> PatchEquivalentObservation:
        base = dict(
            actual_source_head="s" * 40,
            actual_integration_head="i" * 40,
            unintegrated_source_commits=frozenset({"a" * 40, "b" * 40}),
            integration_commit_reachable={"A" * 40: True, "B" * 40: True},
            patch_ids={
                "a" * 40: "pid_a",
                "b" * 40: "pid_b",
                "A" * 40: "pid_a",
                "B" * 40: "pid_b",
            },
            integration_head_origin_reachable=True,
        )
        base.update(over)
        return PatchEquivalentObservation(**base)

    def _eval(self, disposition, observation):
        return evaluate_patch_equivalent_integrated(
            disposition,
            observation,
            issue=self.ISSUE,
            lane=self.LANE,
            branch=self.BRANCH,
            integration_branch=_INTEGRATION_BRANCH,
        )

    def test_fully_consistent_evidence_is_admissible(self) -> None:
        out = self._eval(self._disposition(), self._observation())
        self.assertTrue(out.admissible)
        self.assertEqual(out.reason, PE_OK)

    def test_issue_mismatch_refused(self) -> None:
        out = self._eval(self._disposition(issue="99999"), self._observation())
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_ISSUE_MISMATCH)

    def test_lane_mismatch_refused(self) -> None:
        out = self._eval(self._disposition(lane="issue_13846_other"), self._observation())
        self.assertEqual(out.reason, PE_LANE_MISMATCH)

    def test_branch_mismatch_refused(self) -> None:
        out = self._eval(self._disposition(branch="other_branch"), self._observation())
        self.assertEqual(out.reason, PE_BRANCH_MISMATCH)

    def test_integration_branch_mismatch_refused(self) -> None:
        out = self._eval(
            self._disposition(integration_branch="release/x"), self._observation()
        )
        self.assertEqual(out.reason, PE_INTEGRATION_BRANCH_MISMATCH)

    def test_stale_source_head_refused(self) -> None:
        out = self._eval(
            self._disposition(), self._observation(actual_source_head="z" * 40)
        )
        self.assertEqual(out.reason, PE_SOURCE_HEAD_STALE)

    def test_stale_integration_head_refused(self) -> None:
        out = self._eval(
            self._disposition(), self._observation(actual_integration_head="z" * 40)
        )
        self.assertEqual(out.reason, PE_INTEGRATION_HEAD_STALE)

    def test_empty_commit_map_refused(self) -> None:
        out = self._eval(self._disposition(commit_map=()), self._observation())
        self.assertEqual(out.reason, PE_EMPTY_MAP)

    def test_map_missing_a_branch_commit_refused(self) -> None:
        out = self._eval(
            self._disposition(),
            self._observation(
                unintegrated_source_commits=frozenset({"a" * 40, "b" * 40, "c" * 40})
            ),
        )
        self.assertEqual(out.reason, PE_COMMIT_MAP_INCOMPLETE)

    def test_map_claims_a_commit_not_on_branch_refused(self) -> None:
        out = self._eval(
            self._disposition(),
            self._observation(unintegrated_source_commits=frozenset({"a" * 40})),
        )
        self.assertEqual(out.reason, PE_COMMIT_MAP_INCOMPLETE)

    def test_duplicate_source_in_map_refused(self) -> None:
        out = self._eval(
            self._disposition(
                commit_map=(
                    CommitPatchMapping("a" * 40, "A" * 40, "pid_a"),
                    CommitPatchMapping("a" * 40, "B" * 40, "pid_b"),
                )
            ),
            self._observation(),
        )
        self.assertEqual(out.reason, PE_COMMIT_MAP_INCOMPLETE)

    def test_reused_integration_commit_refused(self) -> None:
        # review j#82305 F1: two sources mapped onto the SAME integration commit is not one-to-one.
        out = self._eval(
            self._disposition(
                commit_map=(
                    CommitPatchMapping("a" * 40, "A" * 40, "pid_a"),
                    CommitPatchMapping("b" * 40, "A" * 40, "pid_a"),
                )
            ),
            self._observation(
                unintegrated_source_commits=frozenset({"a" * 40, "b" * 40}),
                integration_commit_reachable={"A" * 40: True},
                patch_ids={"a" * 40: "pid_a", "b" * 40: "pid_a", "A" * 40: "pid_a"},
            ),
        )
        self.assertEqual(out.reason, PE_INTEGRATION_COMMIT_REUSED)

    def test_unreachable_integration_commit_refused(self) -> None:
        out = self._eval(
            self._disposition(),
            self._observation(
                integration_commit_reachable={"A" * 40: True, "B" * 40: False}
            ),
        )
        self.assertEqual(out.reason, PE_INTEGRATION_COMMIT_UNREACHABLE)

    def test_unresolved_patch_id_refused(self) -> None:
        pids = {"a" * 40: "pid_a", "b" * 40: "", "A" * 40: "pid_a", "B" * 40: "pid_b"}
        out = self._eval(self._disposition(), self._observation(patch_ids=pids))
        self.assertEqual(out.reason, PE_PATCH_ID_UNRESOLVED)

    def test_patch_id_mismatch_refused(self) -> None:
        pids = {
            "a" * 40: "pid_a",
            "b" * 40: "pid_b",
            "A" * 40: "pid_a",
            "B" * 40: "DIFFERENT",
        }
        out = self._eval(self._disposition(), self._observation(patch_ids=pids))
        self.assertEqual(out.reason, PE_PATCH_ID_MISMATCH)

    def test_disposition_patch_id_disagreeing_with_recompute_refused(self) -> None:
        out = self._eval(
            self._disposition(
                commit_map=(
                    CommitPatchMapping("a" * 40, "A" * 40, "pid_a"),
                    CommitPatchMapping("b" * 40, "B" * 40, "STALE_RECORD"),
                )
            ),
            self._observation(),
        )
        self.assertEqual(out.reason, PE_PATCH_ID_MISMATCH)

    def test_origin_unreachable_observed_refused(self) -> None:
        out = self._eval(
            self._disposition(),
            self._observation(integration_head_origin_reachable=False),
        )
        self.assertEqual(out.reason, PE_ORIGIN_UNREACHABLE)

    def test_origin_not_asserted_by_disposition_refused(self) -> None:
        out = self._eval(self._disposition(origin_reachable=False), self._observation())
        self.assertEqual(out.reason, PE_ORIGIN_UNREACHABLE)


# ---------------------------------------------------------------------------
# 2. The disposition journal block (render / parse / project).
# ---------------------------------------------------------------------------


class IntegrationDispositionBlockTests(unittest.TestCase):
    def _disposition(self) -> PatchEquivalentDisposition:
        return PatchEquivalentDisposition(
            issue="13879",
            lane="issue_13879_hibernated_pin_repair",
            branch="issue_13879_hibernated_pin_repair",
            integration_branch=_INTEGRATION_BRANCH,
            source_head="a" * 40,
            integration_head="b" * 40,
            origin_reachable=True,
            commit_map=(CommitPatchMapping("c" * 40, "d" * 40, "pid_a"),),
        )

    def test_render_parse_round_trip(self) -> None:
        disp = self._disposition()
        note = "coordinator prose\n\n" + render_integration_disposition_block(disp)
        blocks = parse_integration_disposition_blocks(note)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(disposition_from_block(blocks[0]), disp)

    def test_no_block_is_empty(self) -> None:
        self.assertEqual(parse_integration_disposition_blocks("just prose"), ())
        self.assertEqual(parse_integration_disposition_blocks(""), ())

    def test_two_blocks_are_both_counted_as_ambiguous(self) -> None:
        disp = self._disposition()
        note = (
            render_integration_disposition_block(disp)
            + "\n\n"
            + render_integration_disposition_block(disp)
        )
        self.assertEqual(len(parse_integration_disposition_blocks(note)), 2)

    def test_malformed_block_still_counts_as_a_raw_occurrence(self) -> None:
        # review j#82301 F2: a malformed fence is NOT dropped — it is a raw block occurrence, so
        # malformed + valid is TWO blocks (ambiguous), never silently the one valid block.
        note = (
            "```mozyo-patch-equivalent-integration\n{not json\n```\n\n"
            + render_integration_disposition_block(self._disposition())
        )
        blocks = parse_integration_disposition_blocks(note)
        self.assertEqual(len(blocks), 2)
        self.assertIsNone(disposition_from_block(blocks[0]))  # the malformed one

    def test_single_malformed_block_does_not_project(self) -> None:
        note = "```mozyo-patch-equivalent-integration\n{not json\n```"
        blocks = parse_integration_disposition_blocks(note)
        self.assertEqual(len(blocks), 1)
        self.assertIsNone(disposition_from_block(blocks[0]))

    def _valid_dict(self, **over) -> dict:
        base = {
            "issue": "13879",
            "lane": "issue_13879_hibernated_pin_repair",
            "branch": "issue_13879_hibernated_pin_repair",
            "integration_branch": _INTEGRATION_BRANCH,
            "source_head": "a" * 40,
            "integration_head": "b" * 40,
            "origin_reachable": True,
            "commit_map": [{"source": "c" * 40, "integration": "d" * 40, "patch_id": "p"}],
        }
        base.update(over)
        return base

    def test_stringified_origin_reachable_is_not_coerced(self) -> None:
        # review j#82301 F2: JSON string "false" must not read as truthy True — it is malformed.
        self.assertIsNone(
            disposition_from_block(json.dumps(self._valid_dict(origin_reachable="false")))
        )

    def test_duplicate_json_key_is_malformed(self) -> None:
        # review j#82305 F2: a duplicate object key is ambiguous (default json takes last-wins),
        # so `origin_reachable` declared twice must be malformed, not silently the last value.
        body = (
            "{\"issue\": \"13879\", \"lane\": \"issue_13879_hibernated_pin_repair\", "
            "\"branch\": \"issue_13879_hibernated_pin_repair\", \"integration_branch\": "
            f"\"{_INTEGRATION_BRANCH}\", \"source_head\": \"{'a' * 40}\", \"integration_head\": "
            f"\"{'b' * 40}\", \"origin_reachable\": false, \"origin_reachable\": true, "
            f"\"commit_map\": [{{\"source\": \"{'c' * 40}\", \"integration\": \"{'d' * 40}\", "
            "\"patch_id\": \"p\"}]}"
        )
        self.assertIsNone(disposition_from_block(body))

    def test_non_canonical_commit_identity_is_malformed(self) -> None:
        # review j#82305 F2: short SHAs / branch names must not be accepted as commit identity.
        self.assertIsNone(
            disposition_from_block(json.dumps(self._valid_dict(integration_head="abc1234")))
        )
        self.assertIsNone(
            disposition_from_block(
                json.dumps(
                    self._valid_dict(
                        commit_map=[
                            {"source": "c" * 40, "integration": _INTEGRATION_BRANCH, "patch_id": "p"}
                        ]
                    )
                )
            )
        )
        self.assertIsNone(
            disposition_from_block(json.dumps(self._valid_dict(source_head="A" * 40)))
        )  # uppercase is not git's lowercase hex

    def test_missing_or_non_bool_fields_do_not_project(self) -> None:
        self.assertIsNone(disposition_from_mapping({"issue": "1"}))
        raw = self._valid_dict()
        del raw["origin_reachable"]
        self.assertIsNone(disposition_from_mapping(raw))


# ---------------------------------------------------------------------------
# Shared real-git scenario builder.
# ---------------------------------------------------------------------------


def _init_herdr_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    mb = root / ".mozyo-bridge"
    mb.mkdir(parents=True, exist_ok=True)
    (mb / "config.yaml").write_text(
        "terminal_transport:\n  backend: herdr\n", encoding="utf-8"
    )
    (mb / "workspace-anchor.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": _WORKSPACE_ID,
                "canonical_session": "mzb-test",
                "project_name": "mozyo_bridge",
                "created_at": "2026-07-19T00:00:00+00:00",
                "updated_at": "2026-07-19T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "base", cwd=root)


class _Scenario:
    """A real repo whose lane branch was patch-equivalent-integrated by cherry-pick.

    ``lane`` branch (checked out in ``lane_worktree``) carries ``n_commits`` commits ahead of
    main; ``integration_branch`` first takes a DIVERGENT staging-base commit (so the cherry-picks
    land on a different parent and get different hashes — otherwise cherry-picking onto main
    reproduces the identical SHAs and the lane is a literal ancestor, not a patch-equivalent one),
    then cherry-picks each lane commit (same diff -> same stable patch-id). A bare ``origin``
    clone makes the integration head origin-reachable; ``origin_has_integration=False`` removes
    the integration ref from origin so the origin-reachability axis fails. Nothing is pushed by
    the tool — the origin is scaffolded by ``git clone --bare`` in the test.
    """

    def __init__(
        self,
        tmp: Path,
        lane: str,
        issue: str,
        *,
        n_commits: int = 2,
        origin_has_integration: bool = True,
    ) -> None:
        self.primary = tmp / "primary"
        _init_herdr_repo(self.primary)
        self.lane = lane
        self.issue = issue
        self.lane_worktree = tmp / f"wt_{lane}"
        _git(
            "worktree", "add", "-b", lane, str(self.lane_worktree), "main",
            cwd=self.primary,
        )
        self.source_commits: list[str] = []
        for i in range(n_commits):
            (self.lane_worktree / f"lane_{i}.txt").write_text(
                f"lane change {i}\n", encoding="utf-8"
            )
            _git("add", "-A", cwd=self.lane_worktree)
            _git("commit", "-m", f"{lane} c{i}", cwd=self.lane_worktree)
            self.source_commits.append(_rev_parse(self.lane_worktree, "HEAD"))
        self.source_head = _rev_parse(self.lane_worktree, lane)
        _git("branch", _INTEGRATION_BRANCH, "main", cwd=self.primary)
        _git("checkout", _INTEGRATION_BRANCH, cwd=self.primary)
        (self.primary / "staging_base.txt").write_text("staging base\n", encoding="utf-8")
        _git("add", "-A", cwd=self.primary)
        _git("commit", "-m", "staging base (divergent)", cwd=self.primary)
        self.integration_commits: list[str] = []
        for sha in self.source_commits:
            _git("cherry-pick", sha, cwd=self.primary)
            self.integration_commits.append(_rev_parse(self.primary, "HEAD"))
        self.integration_head = _rev_parse(self.primary, _INTEGRATION_BRANCH)
        _git("checkout", "main", cwd=self.primary)
        self.origin = tmp / "origin.git"
        _git("clone", "--bare", str(self.primary), str(self.origin), cwd=tmp)
        if not origin_has_integration:
            _git(
                "update-ref", "-d", f"refs/heads/{_INTEGRATION_BRANCH}", cwd=self.origin
            )
        _git("remote", "add", "origin", str(self.origin), cwd=self.primary)
        _git("fetch", "origin", cwd=self.primary)
        self.bound_token = derive_lane_workspace_token(str(self.lane_worktree.resolve()))

    def drop_origin_branch(self) -> None:
        """Delete the integration branch on the bare origin AFTER the fetch (review j#82301 F1).

        The local ``refs/remotes/origin/<branch>`` tracking ref stays behind, so a check that
        trusts the cached ref would still pass — but a fresh ``git ls-remote`` now sees the branch
        gone. This is the reviewer's exact stale-cache reproduction.
        """
        _git("update-ref", "-d", f"refs/heads/{_INTEGRATION_BRANCH}", cwd=self.origin)

    def disposition(self, **over) -> PatchEquivalentDisposition:
        base = PatchEquivalentDisposition(
            issue=self.issue,
            lane=self.lane,
            branch=self.lane,
            integration_branch=_INTEGRATION_BRANCH,
            source_head=self.source_head,
            integration_head=self.integration_head,
            origin_reachable=True,
            commit_map=tuple(
                CommitPatchMapping(src, integ, _patch_id(self.primary, src))
                for src, integ in zip(self.source_commits, self.integration_commits)
            ),
        )
        return dataclasses.replace(base, **over) if over else base

    def journal_note(self, disposition: PatchEquivalentDisposition | None = None) -> str:
        disp = disposition if disposition is not None else self.disposition()
        return "## Integration disposition: patch_equivalent\n\n" + (
            render_integration_disposition_block(disp)
        )

    def entry(self, disposition: PatchEquivalentDisposition | None = None) -> RedmineJournalEntry:
        return _entry(_INTEGRATION_JOURNAL, self.journal_note(disposition), self.issue)


def _pins(lane: str) -> tuple[ProcessGenerationPin, ...]:
    return (
        ProcessGenerationPin(
            role="gateway",
            provider="codex",
            assigned_name=encode_assigned_name(_WORKSPACE_ID, "codex", lane),
            locator="w28:p3S",
        ),
        ProcessGenerationPin(
            role="worker",
            provider="claude",
            assigned_name=encode_assigned_name(_WORKSPACE_ID, "claude", lane),
            locator="w28:p3T",
        ),
    )


def _seed_hibernated_released_bound(
    key: LaneLifecycleKey, issue: str, worktree_identity: str
) -> None:
    from mozyo_bridge.core.state.lane_lifecycle import DISPOSITION_ACTIVE

    dec = DecisionPointer(source="redmine", issue_id=issue, journal_id="82000")
    lifecycle = LaneLifecycleStore()
    declaration = LaneDeclarationStore()
    out = declaration.declare_lane(
        key,
        decision=dec,
        issue_id=issue,
        declared_slots=_pins(key.lane_id),
        worktree_identity=worktree_identity,
    )
    assert out.applied, f"seed declare refused: {out.reason}"
    rec = lifecycle.get(key)
    lifecycle.transition_disposition(
        key,
        expected_disposition=DISPOSITION_ACTIVE,
        expected_revision=rec.revision,
        target=DISPOSITION_HIBERNATED,
        decision=dec,
    )
    rec = lifecycle.get(key)
    lifecycle.request_release(
        key,
        expected_revision=rec.revision,
        action_id="rel-1",
        pins=[
            ReleasePin("gateway", "codex-mzb1", "w28:p3S"),
            ReleasePin("worker", "claude-mzb1", "w28:p3T"),
        ],
    )
    rec = lifecycle.get(key)
    lifecycle.record_release_outcome(
        key, action_id="rel-1", expected_revision=rec.revision, target=RELEASE_RELEASED
    )


# ---------------------------------------------------------------------------
# 3. The credential-gated exact-journal read (authority).
# ---------------------------------------------------------------------------


class ReadIntegrationDispositionTests(unittest.TestCase):
    ISSUE = "13846"
    LANE = "issue_13846_fresh_generation_binding"

    def _disp(self) -> PatchEquivalentDisposition:
        return PatchEquivalentDisposition(
            issue=self.ISSUE,
            lane=self.LANE,
            branch=self.LANE,
            integration_branch=_INTEGRATION_BRANCH,
            source_head="a" * 40,
            integration_head="b" * 40,
            origin_reachable=True,
            commit_map=(CommitPatchMapping("c" * 40, "d" * 40, "pid_a"),),
        )

    def _note(self, disp=None) -> str:
        return render_integration_disposition_block(disp or self._disp())

    def test_unconfigured_credentials_fail_closed(self) -> None:
        _install_fake_journal(self, [], configured=False)
        disp, failure = read_integration_disposition(self.ISSUE, _INTEGRATION_JOURNAL)
        self.assertIsNone(disp)
        self.assertEqual(failure.reason, PE_REDMINE_UNCONFIGURED)

    def test_unreadable_redmine_fails_closed(self) -> None:
        _install_fake_journal(self, [], read_error=True)
        disp, failure = read_integration_disposition(self.ISSUE, _INTEGRATION_JOURNAL)
        self.assertIsNone(disp)
        self.assertEqual(failure.reason, PE_REDMINE_UNREADABLE)

    def test_journal_not_found_fails_closed(self) -> None:
        _install_fake_journal(
            self, [_entry("99999", self._note(), self.ISSUE)]
        )
        disp, failure = read_integration_disposition(self.ISSUE, _INTEGRATION_JOURNAL)
        self.assertIsNone(disp)
        self.assertEqual(failure.reason, PE_JOURNAL_NOT_FOUND)

    def test_absent_block_fails_closed(self) -> None:
        _install_fake_journal(
            self, [_entry(_INTEGRATION_JOURNAL, "just prose, no block", self.ISSUE)]
        )
        disp, failure = read_integration_disposition(self.ISSUE, _INTEGRATION_JOURNAL)
        self.assertIsNone(disp)
        self.assertEqual(failure.reason, PE_DISPOSITION_ABSENT)

    def test_ambiguous_blocks_fail_closed(self) -> None:
        note = self._note() + "\n\n" + self._note()
        _install_fake_journal(self, [_entry(_INTEGRATION_JOURNAL, note, self.ISSUE)])
        disp, failure = read_integration_disposition(self.ISSUE, _INTEGRATION_JOURNAL)
        self.assertIsNone(disp)
        self.assertEqual(failure.reason, PE_DISPOSITION_AMBIGUOUS)

    def test_malformed_block_fails_closed(self) -> None:
        note = "```mozyo-patch-equivalent-integration\n{\"issue\": \"x\"}\n```"
        _install_fake_journal(self, [_entry(_INTEGRATION_JOURNAL, note, self.ISSUE)])
        disp, failure = read_integration_disposition(self.ISSUE, _INTEGRATION_JOURNAL)
        self.assertIsNone(disp)
        self.assertEqual(failure.reason, PE_DISPOSITION_MALFORMED)

    def test_exact_journal_disposition_is_read(self) -> None:
        want = self._disp()
        _install_fake_journal(
            self,
            [
                _entry("82000", "unrelated", self.ISSUE),
                _entry(_INTEGRATION_JOURNAL, self._note(want), self.ISSUE),
            ],
        )
        disp, failure = read_integration_disposition(self.ISSUE, _INTEGRATION_JOURNAL)
        self.assertIsNone(failure)
        self.assertEqual(disp, want)
        self.assertEqual(self._fake_journal.reads, [self.ISSUE])


# ---------------------------------------------------------------------------
# 4. The action-time resolver over real cherry-picked git.
# ---------------------------------------------------------------------------


class PatchEquivalentResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _scenario(self, **kw) -> _Scenario:
        return _Scenario(self.tmp, "issue_13879_hibernated_pin_repair", "13879", **kw)

    def _args(self, s: _Scenario, journal=_INTEGRATION_JOURNAL) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(s.primary),
            issue=s.issue,
            lane_label=s.lane,
            branch=s.lane,
            integration_branch=_INTEGRATION_BRANCH,
            integration_journal=journal,
        )

    def test_green_cherry_pick_disposition_is_admissible(self) -> None:
        s = self._scenario()
        _install_fake_journal(self, [s.entry()])
        out = resolve_patch_equivalent_integration(self._args(s), s.primary)
        self.assertTrue(out.admissible, msg=out.detail)
        self.assertEqual(out.reason, PE_OK)

    def test_literal_ancestor_not_required_here(self) -> None:
        s = self._scenario()
        rc = subprocess.run(
            ["git", "-C", str(s.primary), "merge-base", "--is-ancestor",
             s.lane, _INTEGRATION_BRANCH],
        ).returncode
        self.assertNotEqual(rc, 0)

    def test_no_journal_returns_none(self) -> None:
        s = self._scenario()
        _install_fake_journal(self, [s.entry()])
        self.assertIsNone(
            resolve_patch_equivalent_integration(self._args(s, journal=None), s.primary)
        )

    def test_stale_source_head_fails_closed(self) -> None:
        s = self._scenario()
        _install_fake_journal(self, [s.entry(s.disposition(source_head="d" * 40))])
        out = resolve_patch_equivalent_integration(self._args(s), s.primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_SOURCE_HEAD_STALE)

    def test_tampered_patch_id_fails_closed(self) -> None:
        s = self._scenario()
        disp = s.disposition()
        tampered = dataclasses.replace(
            disp,
            commit_map=(
                dataclasses.replace(disp.commit_map[0], patch_id="deadbeef" * 5),
            )
            + disp.commit_map[1:],
        )
        _install_fake_journal(self, [s.entry(tampered)])
        out = resolve_patch_equivalent_integration(self._args(s), s.primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_PATCH_ID_MISMATCH)

    def test_fabricated_integration_commit_fails_closed(self) -> None:
        s = self._scenario()
        disp = s.disposition()
        fabricated = dataclasses.replace(
            disp,
            commit_map=(
                dataclasses.replace(disp.commit_map[0], integration_commit="f" * 40),
            )
            + disp.commit_map[1:],
        )
        _install_fake_journal(self, [s.entry(fabricated)])
        out = resolve_patch_equivalent_integration(self._args(s), s.primary)
        self.assertFalse(out.admissible)
        self.assertIn(
            out.reason, {PE_INTEGRATION_COMMIT_UNREACHABLE, PE_PATCH_ID_UNRESOLVED}
        )

    def test_origin_unreachable_when_branch_absent_before_fetch(self) -> None:
        # origin never had the integration ref: ls-remote sees nothing -> unreachable.
        s = self._scenario(origin_has_integration=False)
        _install_fake_journal(self, [s.entry()])
        out = resolve_patch_equivalent_integration(self._args(s), s.primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_ORIGIN_UNREACHABLE)

    def test_stale_remote_tracking_ref_is_not_authority(self) -> None:
        # review j#82301 F1: the branch is deleted on origin AFTER the fetch, so the cached
        # refs/remotes/origin/<branch> ref survives locally. A fresh ls-remote must still see the
        # branch gone and fail closed — the stale tracking ref is NOT origin authority.
        s = self._scenario()
        s.drop_origin_branch()
        _install_fake_journal(self, [s.entry()])
        out = resolve_patch_equivalent_integration(self._args(s), s.primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_ORIGIN_UNREACHABLE)

    def test_malformed_plus_valid_block_is_ambiguous(self) -> None:
        # review j#82301 F2: a malformed fence alongside a valid one is ambiguous, not the valid
        # one silently winning.
        s = self._scenario()
        note = (
            "```mozyo-patch-equivalent-integration\n{not json\n```\n\n" + s.journal_note()
        )
        _install_fake_journal(self, [_entry(_INTEGRATION_JOURNAL, note, s.issue)])
        out = resolve_patch_equivalent_integration(self._args(s), s.primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_DISPOSITION_AMBIGUOUS)

    def test_unresolvable_integration_branch_probe_fails_closed(self) -> None:
        s = self._scenario()
        _install_fake_journal(self, [s.entry()])
        args = self._args(s)
        args.integration_branch = "no_such_branch"
        out = resolve_patch_equivalent_integration(args, s.primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_PROBE_UNRESOLVED)

    def test_reused_integration_commit_with_nonequivalent_tree_refused(self) -> None:
        # review j#82305 F1, real git: lane `add x; delete x; add x` vs integration `add x;
        # delete x`. Mapping source 1 & 3 onto the single integration `add x` commit makes every
        # pair's patch-id match, but the final trees differ (lane has x, integration does not).
        # The one-to-one check must refuse it.
        primary = self.tmp / "reuse"
        _init_herdr_repo(primary)
        lane = "issue_13846_reuse"
        wt = self.tmp / "reuse_wt"
        _git("worktree", "add", "-b", lane, str(wt), "main", cwd=primary)
        for msg, write in (
            ("add x", True),
            ("delete x", False),
            ("add x again", True),
        ):
            if write:
                (wt / "x.txt").write_text("content X\n", encoding="utf-8")
            else:
                (wt / "x.txt").unlink()
            _git("add", "-A", cwd=wt)
            _git("commit", "-m", msg, cwd=wt)
        c1, c2, c3 = (
            _rev_parse(wt, f"{lane}~2"),
            _rev_parse(wt, f"{lane}~1"),
            _rev_parse(wt, lane),
        )
        _git("branch", _INTEGRATION_BRANCH, "main", cwd=primary)
        _git("checkout", _INTEGRATION_BRANCH, cwd=primary)
        (primary / "sb.txt").write_text("sb\n", encoding="utf-8")
        _git("add", "-A", cwd=primary)
        _git("commit", "-m", "staging base", cwd=primary)
        _git("cherry-pick", c1, cwd=primary)
        i1 = _rev_parse(primary, "HEAD")
        _git("cherry-pick", c2, cwd=primary)
        i2 = _rev_parse(primary, "HEAD")
        _git("checkout", "main", cwd=primary)
        # The trees really are non-equivalent (the exploit's whole point).
        self.assertEqual(_git("ls-tree", lane, "x.txt", cwd=wt, capture=True).stdout.strip() != "", True)
        self.assertEqual(
            _git("ls-tree", _INTEGRATION_BRANCH, "x.txt", cwd=primary, capture=True).stdout.strip(),
            "",
        )
        disp = PatchEquivalentDisposition(
            issue="13846",
            lane=lane,
            branch=lane,
            integration_branch=_INTEGRATION_BRANCH,
            source_head=_rev_parse(wt, lane),
            integration_head=_rev_parse(primary, _INTEGRATION_BRANCH),
            origin_reachable=True,
            commit_map=(
                CommitPatchMapping(c1, i1, _patch_id(primary, c1)),
                CommitPatchMapping(c2, i2, _patch_id(primary, c2)),
                CommitPatchMapping(c3, i1, _patch_id(primary, c3)),  # reuse i1
            ),
        )
        _install_fake_journal(
            self, [_entry(_INTEGRATION_JOURNAL, render_integration_disposition_block(disp), "13846")]
        )
        args = argparse.Namespace(
            repo=str(primary),
            issue="13846",
            lane_label=lane,
            branch=lane,
            integration_branch=_INTEGRATION_BRANCH,
            integration_journal=_INTEGRATION_JOURNAL,
        )
        out = resolve_patch_equivalent_integration(args, primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_INTEGRATION_COMMIT_REUSED)

    def test_probe_observation_recomputes_matching_patch_ids(self) -> None:
        s = self._scenario()
        obs = probe_patch_equivalent_observation(
            s.primary, s.disposition(), branch=s.lane, integration_branch=_INTEGRATION_BRANCH
        )
        self.assertEqual(obs.actual_source_head, s.source_head)
        self.assertEqual(obs.actual_integration_head, s.integration_head)
        self.assertEqual(obs.unintegrated_source_commits, frozenset(s.source_commits))
        for src, integ in zip(s.source_commits, s.integration_commits):
            self.assertTrue(obs.patch_ids[src])
            self.assertEqual(obs.patch_ids[src], obs.patch_ids[integ])
        self.assertTrue(obs.integration_head_origin_reachable)


# ---------------------------------------------------------------------------
# 5. The command boundary over the three residual-lane journal shapes.
# ---------------------------------------------------------------------------

_RESIDUAL_LANES = (
    ("13846", "issue_13846_fresh_generation_binding"),
    ("13846", "issue_13846_worker_dispatch_admission"),
    ("13879", "issue_13879_hibernated_pin_repair"),
)


class PatchEquivalentCommandBoundary(unittest.TestCase):
    """``sublane retire --retire-hibernated-bound --integration-journal`` end to end."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)

        self.rows: list[dict] = [
            {"name": encode_assigned_name(_WORKSPACE_ID, "codex", ""), "pane_id": "w28:p1"},
            {"name": encode_assigned_name(_WORKSPACE_ID, "claude", ""), "pane_id": "w28:p2"},
        ]
        self._real_rows = sublane_herdr_projection.list_herdr_agent_rows
        self._real_execute = sublane_herdr_retire.execute_herdr_retire_close
        self.executed_closes: list[tuple[str, str]] = []

        def fake_rows(env):
            return list(self.rows)

        def fake_execute(plan, **kwargs):
            closed = []
            for role, locator in plan.close_targets:
                closed.append((role, locator))
                self.executed_closes.append((role, locator))
            return HerdrRetireCloseResult(
                workspace_id=plan.workspace_id,
                lane_id=plan.lane_id,
                closed=tuple(closed),
                foreign_names=plan.foreign_names,
            )

        sublane_herdr_projection.list_herdr_agent_rows = fake_rows
        sublane_herdr_retire.execute_herdr_retire_close = fake_execute

        def _restore():
            sublane_herdr_projection.list_herdr_agent_rows = self._real_rows
            sublane_herdr_retire.execute_herdr_retire_close = self._real_execute
            if self._prev_home is None:
                os.environ.pop("MOZYO_BRIDGE_HOME", None)
            else:
                os.environ["MOZYO_BRIDGE_HOME"] = self._prev_home

        self.addCleanup(_restore)

    def _scenario(self, issue: str, lane: str, **kw) -> _Scenario:
        return _Scenario(self.tmp / f"sc_{lane}", lane, issue, **kw)

    def _args(
        self, s: _Scenario, *, journal=_INTEGRATION_JOURNAL, integration_branch=_INTEGRATION_BRANCH
    ) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(s.primary),
            issue=s.issue,
            journal="82000",
            lane_label=s.lane,
            worktree=str(s.lane_worktree),
            branch=s.lane,
            integration_branch=integration_branch,
            execute=False,
            migrate_hibernated_legacy=False,
            reconcile_hibernated_live=False,
            retire_hibernated_bound=True,
            json=True,
            issue_closed=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            latest_generation_admissible=True,
            review_generation_json=None,
            integration_journal=journal,
        )

    def _run(self, args) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        return code, json.loads(buffer.getvalue())

    def _verdict(self, payload) -> dict:
        return payload.get("hibernated_bound_retire", {})

    def _seed(self, s: _Scenario) -> None:
        _seed_hibernated_released_bound(
            LaneLifecycleKey(_WORKSPACE_ID, s.lane), s.issue, s.bound_token
        )

    def _disposition_of(self, lane: str) -> str:
        rec = LaneLifecycleStore().get(LaneLifecycleKey(_WORKSPACE_ID, lane))
        return "" if rec is None else rec.lane_disposition

    # -- the three residual-lane journal shapes ---------------------------

    def test_all_three_residual_lanes_terminalize(self) -> None:
        for issue, lane in _RESIDUAL_LANES:
            with self.subTest(lane=lane):
                s = self._scenario(issue, lane)
                self._seed(s)
                _install_fake_journal(self, [s.entry()])
                code, payload = self._run(self._args(s))
                self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
                self.assertEqual(self._verdict(payload)["state"], BOUND_RETIRE_RETIRED)
                self.assertTrue(payload["retire_ok"])
                self.assertEqual(self._disposition_of(lane), DISPOSITION_RETIRED)
                self.assertEqual(self.executed_closes, [])

    def test_residual_lane_replay_is_verified_noop(self) -> None:
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        _install_fake_journal(self, [s.entry()])
        self.assertEqual(self._run(self._args(s))[0], 0)
        code, payload = self._run(self._args(s))
        self.assertEqual(code, 0)
        self.assertEqual(self._verdict(payload)["state"], BOUND_RETIRE_ALREADY_RETIRED)
        self.assertEqual(self.executed_closes, [])

    def test_residual_lane_replay_with_relaunched_pair_fails_closed(self) -> None:
        issue, lane = _RESIDUAL_LANES[1]
        s = self._scenario(issue, lane)
        self._seed(s)
        _install_fake_journal(self, [s.entry()])
        self.assertEqual(self._run(self._args(s))[0], 0)
        self.rows.extend(
            [
                {"name": encode_assigned_name(_WORKSPACE_ID, "codex", lane), "pane_id": "w9:pA"},
                {"name": encode_assigned_name(_WORKSPACE_ID, "claude", lane), "pane_id": "w9:pB"},
            ]
        )
        code, payload = self._run(self._args(s))
        self.assertEqual(code, 1)
        self.assertEqual(self._verdict(payload)["reason"], BOUND_RETIRE_LIVE_PAIR_PRESENT)
        self.assertFalse(payload["retire_ok"])

    # -- non-regression: no journal keeps the literal head_not_integrated -

    def test_no_journal_keeps_literal_head_not_integrated(self) -> None:
        issue, lane = _RESIDUAL_LANES[2]
        s = self._scenario(issue, lane)
        self._seed(s)
        _install_fake_journal(self, [s.entry()])
        code, payload = self._run(self._args(s, journal=None))
        self.assertEqual(code, 1)
        self.assertEqual(
            self._verdict(payload)["reason"], BOUND_RETIRE_HEAD_NOT_INTEGRATED
        )
        self.assertEqual(self._disposition_of(lane), DISPOSITION_HIBERNATED)

    # -- negative: authority failures fail closed -------------------------

    def test_tampered_patch_id_fails_closed(self) -> None:
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        disp = s.disposition()
        tampered = dataclasses.replace(
            disp,
            commit_map=(dataclasses.replace(disp.commit_map[0], patch_id="cafebabe" * 5),)
            + disp.commit_map[1:],
        )
        _install_fake_journal(self, [s.entry(tampered)])
        code, payload = self._run(self._args(s))
        self.assertEqual(code, 1)
        verdict = self._verdict(payload)
        self.assertEqual(verdict["state"], BOUND_RETIRE_BLOCKED)
        self.assertEqual(verdict["reason"], BOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED)
        self.assertIn(PE_PATCH_ID_MISMATCH, verdict["detail"])
        self.assertEqual(self._disposition_of(lane), DISPOSITION_HIBERNATED)
        self.assertEqual(self.executed_closes, [])

    def test_unconfigured_credentials_fail_closed(self) -> None:
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        _install_fake_journal(self, [s.entry()], configured=False)
        code, payload = self._run(self._args(s))
        self.assertEqual(code, 1)
        verdict = self._verdict(payload)
        self.assertEqual(verdict["reason"], BOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED)
        self.assertIn(PE_REDMINE_UNCONFIGURED, verdict["detail"])
        self.assertEqual(self._disposition_of(lane), DISPOSITION_HIBERNATED)

    def test_journal_not_found_fails_closed(self) -> None:
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        _install_fake_journal(self, [s.entry()])  # entry lives at _INTEGRATION_JOURNAL
        code, payload = self._run(self._args(s, journal="70000"))
        self.assertEqual(code, 1)
        verdict = self._verdict(payload)
        self.assertEqual(verdict["reason"], BOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED)
        self.assertIn(PE_JOURNAL_NOT_FOUND, verdict["detail"])

    def test_stale_remote_tracking_ref_fails_closed(self) -> None:
        # review j#82301 F1 at the command boundary: origin branch dropped after fetch, cached
        # tracking ref survives — the retire must fail closed on the fresh ls-remote.
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        s.drop_origin_branch()
        _install_fake_journal(self, [s.entry()])
        code, payload = self._run(self._args(s))
        self.assertEqual(code, 1)
        verdict = self._verdict(payload)
        self.assertEqual(verdict["reason"], BOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED)
        self.assertIn(PE_ORIGIN_UNREACHABLE, verdict["detail"])
        self.assertEqual(self._disposition_of(lane), DISPOSITION_HIBERNATED)

    def test_wrong_lane_disposition_fails_closed(self) -> None:
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        _install_fake_journal(
            self, [s.entry(s.disposition(lane="issue_99999_foreign"))]
        )
        code, payload = self._run(self._args(s))
        self.assertEqual(code, 1)
        self.assertEqual(
            self._verdict(payload)["reason"], BOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED
        )

    # -- F2: literal-ancestor green never constructs the resolver ---------

    def test_literal_ancestor_green_never_calls_the_resolver(self) -> None:
        """Review j#82298 F2: a literal-ancestor lane must not touch the patch-equivalent path.

        Point --integration-branch at the lane branch itself (a trivial literal ancestor) and
        replace the resolver with a landmine: if the command constructs it on the literal path,
        the mine fires. The retire must terminalize via the literal path alone.
        """
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)

        def _landmine(*a, **k):
            raise AssertionError("resolver called on the literal-ancestor path")

        real = spe.resolve_patch_equivalent_integration
        spe.resolve_patch_equivalent_integration = _landmine
        self.addCleanup(
            lambda: setattr(spe, "resolve_patch_equivalent_integration", real)
        )
        # Also make the Redmine source a landmine: the literal path must never read Redmine.
        _install_fake_journal(self, [], read_error=True)
        args = self._args(s, integration_branch=lane, journal=_INTEGRATION_JOURNAL)
        code, payload = self._run(args)
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._verdict(payload)["state"], BOUND_RETIRE_RETIRED)
        self.assertEqual(self._fake_journal.reads, [])  # Redmine never read


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
