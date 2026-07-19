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

Passing a fake integration branch to dodge the literal probe is forbidden. Instead the retire
re-reads the coordinator's structured integration disposition (source head, integration head,
commit map, stable patch-id, origin reachability) from the exact journal at action-time and
RECOMPUTES the git facts, terminalizing only when the recomputed patch-ids prove every mapped
cherry-pick equivalent, the recorded heads match the current branches, and the integration head
is origin-reachable. Missing / ambiguous / stale / mismatched evidence, an unreadable
disposition, a live / foreign slot: all zero-write.

Three layers are pinned, all synthetic (isolated ``MOZYO_BRIDGE_HOME``, a fake herdr inventory,
real local git repos with real cherry-picks, never the shared ``$HOME/.mozyo_bridge`` and never
a live pane / process / route mutation, never a network / origin push by the tool):

1. the pure fence (:func:`evaluate_patch_equivalent_integrated`) — the claim-vs-recomputed-facts
   matrix, every refusal axis;
2. the action-time resolver (:func:`resolve_patch_equivalent_integration`) over real cherry-picked
   git — the green proof, a stale head, a tampered patch-id, an unreachable origin, an unreadable
   disposition; and
3. the command boundary (``sublane retire --retire-hibernated-bound
   --integration-disposition-json``) over the three residual-lane journal shapes — positive
   (terminalizes), negative (tampered evidence fails closed), and replay (idempotent no-op),
   plus non-regression of the literal ``head_not_integrated`` when no disposition is supplied.

Boundary (Redmine #14066): no process launch / close / resume, no worktree / branch removal, no
raw Herdr / tmux, no origin/main, no production / tag / publish, no push / fetch by the tool.
"""

from __future__ import annotations

import argparse
import contextlib
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
    RELEASE_REQUESTED,
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
    PE_EVIDENCE_UNREADABLE,
    PE_PROBE_UNRESOLVED,
    load_patch_equivalent_disposition,
    probe_patch_equivalent_observation,
    resolve_patch_equivalent_integration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.patch_equivalent_integration import (  # noqa: E402,E501
    PE_BRANCH_MISMATCH,
    PE_COMMIT_MAP_INCOMPLETE,
    PE_EMPTY_MAP,
    PE_INTEGRATION_BRANCH_MISMATCH,
    PE_INTEGRATION_COMMIT_UNREACHABLE,
    PE_INTEGRATION_HEAD_STALE,
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
    evaluate_patch_equivalent_integrated,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    derive_lane_workspace_token,
    encode_assigned_name,
)

_WORKSPACE_ID = "b3d17ac95e6f4802"
_INTEGRATION_BRANCH = "int_13472_session_continuity"
_ORIGIN_REF = f"origin/{_INTEGRATION_BRANCH}"


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
            origin_ref=_ORIGIN_REF,
            origin_reachable=True,
            commit_map=(
                CommitPatchMapping("a" * 40, "A" * 40, "pid_a"),
                CommitPatchMapping("b" * 40, "B" * 40, "pid_b"),
            ),
            journal_id="82064",
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
        # The branch carries an unintegrated commit the map does not mention.
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
        # Recomputed source patch-id disagrees with the recomputed integration patch-id: the
        # cherry-pick is not actually patch-equivalent.
        pids = {
            "a" * 40: "pid_a",
            "b" * 40: "pid_b",
            "A" * 40: "pid_a",
            "B" * 40: "DIFFERENT",
        }
        out = self._eval(self._disposition(), self._observation(patch_ids=pids))
        self.assertEqual(out.reason, PE_PATCH_ID_MISMATCH)

    def test_disposition_patch_id_disagreeing_with_recompute_refused(self) -> None:
        # The recomputed source == integration, but the coordinator's RECORDED patch-id differs:
        # a stale / wrong durable record must not license the retire.
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
# Shared real-git scenario builder for the resolver + command layers.
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
    main; ``integration_branch`` cherry-picks each (different hashes, identical diffs); a bare
    ``origin`` clone makes the integration head origin-reachable. Nothing is pushed by the tool
    — the origin is scaffolded by ``git clone --bare`` in the test.
    """

    def __init__(self, tmp: Path, lane: str, issue: str, *, n_commits: int = 2) -> None:
        self.primary = tmp / "primary"
        _init_herdr_repo(self.primary)
        self.lane = lane
        self.issue = issue
        self.lane_worktree = tmp / f"wt_{lane}"
        _git(
            "worktree", "add", "-b", lane, str(self.lane_worktree), "main",
            cwd=self.primary,
        )
        # Lane commits (ahead of main by hash) — NOT a literal ancestor of the integration branch.
        self.source_commits: list[str] = []
        for i in range(n_commits):
            f = self.lane_worktree / f"lane_{i}.txt"
            f.write_text(f"lane change {i}\n", encoding="utf-8")
            _git("add", "-A", cwd=self.lane_worktree)
            _git("commit", "-m", f"{lane} c{i}", cwd=self.lane_worktree)
            self.source_commits.append(_rev_parse(self.lane_worktree, "HEAD"))
        self.source_head = _rev_parse(self.lane_worktree, lane)
        # Integration branch: a DIVERGENT staging base (so the cherry-picks land on a different
        # parent and get different commit hashes — otherwise cherry-picking onto main reproduces
        # the identical SHAs and the lane is a literal ancestor, not a patch-equivalent one), then
        # cherry-pick every lane commit (same diff -> same stable patch-id, new hash).
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
        # Origin: a bare clone carrying the integration branch (scaffold only, no tool push).
        self.origin = tmp / "origin.git"
        _git("clone", "--bare", str(self.primary), str(self.origin), cwd=tmp)
        _git("remote", "add", "origin", str(self.origin), cwd=self.primary)
        _git("fetch", "origin", cwd=self.primary)
        self.bound_token = derive_lane_workspace_token(str(self.lane_worktree.resolve()))

    def commit_map(self) -> list[dict]:
        rows = []
        for src, integ in zip(self.source_commits, self.integration_commits):
            rows.append(
                {
                    "source": src,
                    "integration": integ,
                    "patch_id": _patch_id(self.primary, src),
                }
            )
        return rows

    def disposition_dict(self, **over) -> dict:
        d = {
            "issue": self.issue,
            "lane": self.lane,
            "branch": self.lane,
            "integration_branch": _INTEGRATION_BRANCH,
            "source_head": self.source_head,
            "integration_head": self.integration_head,
            "origin_ref": _ORIGIN_REF,
            "origin_reachable": True,
            "journal_id": "82000",
            "commit_map": self.commit_map(),
        }
        d.update(over)
        return d


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
    key: LaneLifecycleKey, issue: str, worktree_identity: str, *, released: bool = True
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
    if not released:
        return
    rec = lifecycle.get(key)
    lifecycle.record_release_outcome(
        key, action_id="rel-1", expected_revision=rec.revision, target=RELEASE_RELEASED
    )


# ---------------------------------------------------------------------------
# 2. The action-time resolver over real cherry-picked git.
# ---------------------------------------------------------------------------


class PatchEquivalentResolverTests(unittest.TestCase):
    """``resolve_patch_equivalent_integration`` / probe over real git."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.s = _Scenario(self.tmp, "issue_13879_hibernated_pin_repair", "13879")

    def _write(self, name: str, payload: dict) -> str:
        p = self.tmp / name
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def _args(self, path) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(self.s.primary),
            issue=self.s.issue,
            lane_label=self.s.lane,
            branch=self.s.lane,
            integration_branch=_INTEGRATION_BRANCH,
            integration_disposition_json=path,
        )

    def test_green_cherry_pick_disposition_is_admissible(self) -> None:
        path = self._write("disp.json", self.s.disposition_dict())
        out = resolve_patch_equivalent_integration(self._args(path), self.s.primary)
        self.assertTrue(out.admissible, msg=out.detail)
        self.assertEqual(out.reason, PE_OK)

    def test_literal_ancestor_not_required_here(self) -> None:
        # Sanity: the lane branch really is NOT a literal ancestor of the integration branch,
        # so this path is the only route to an integrated verdict.
        rc = subprocess.run(
            ["git", "-C", str(self.s.primary), "merge-base", "--is-ancestor",
             self.s.lane, _INTEGRATION_BRANCH],
        ).returncode
        self.assertNotEqual(rc, 0)

    def test_no_disposition_returns_none(self) -> None:
        args = self._args(None)
        self.assertIsNone(resolve_patch_equivalent_integration(args, self.s.primary))

    def test_unreadable_disposition_fails_closed(self) -> None:
        out = resolve_patch_equivalent_integration(
            self._args(str(self.tmp / "nope.json")), self.s.primary
        )
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_EVIDENCE_UNREADABLE)

    def test_malformed_json_fails_closed(self) -> None:
        p = self.tmp / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        out = resolve_patch_equivalent_integration(
            self._args(str(p)), self.s.primary
        )
        self.assertEqual(out.reason, PE_EVIDENCE_UNREADABLE)

    def test_stale_source_head_fails_closed(self) -> None:
        path = self._write("disp.json", self.s.disposition_dict(source_head="d" * 40))
        out = resolve_patch_equivalent_integration(self._args(path), self.s.primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_SOURCE_HEAD_STALE)

    def test_tampered_patch_id_fails_closed(self) -> None:
        d = self.s.disposition_dict()
        d["commit_map"][0]["patch_id"] = "deadbeef" * 5
        path = self._write("disp.json", d)
        out = resolve_patch_equivalent_integration(self._args(path), self.s.primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_PATCH_ID_MISMATCH)

    def test_fabricated_integration_commit_fails_closed(self) -> None:
        # A disposition naming an integration commit that is not on the integration branch
        # (the "fake integration branch" dodge the ticket forbids) is refused.
        d = self.s.disposition_dict()
        d["commit_map"][0]["integration"] = "f" * 40
        path = self._write("disp.json", d)
        out = resolve_patch_equivalent_integration(self._args(path), self.s.primary)
        self.assertFalse(out.admissible)
        self.assertIn(
            out.reason, {PE_INTEGRATION_COMMIT_UNREACHABLE, PE_PATCH_ID_UNRESOLVED}
        )

    def test_unresolvable_integration_branch_probe_fails_closed(self) -> None:
        args = self._args(self._write("disp.json", self.s.disposition_dict()))
        args.integration_branch = "no_such_branch"
        out = resolve_patch_equivalent_integration(args, self.s.primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_PROBE_UNRESOLVED)

    def test_origin_unreachable_fails_closed(self) -> None:
        # Point the disposition's origin ref at a ref the integration head is not reachable from.
        path = self._write(
            "disp.json", self.s.disposition_dict(origin_ref="origin/main")
        )
        out = resolve_patch_equivalent_integration(self._args(path), self.s.primary)
        self.assertFalse(out.admissible)
        self.assertEqual(out.reason, PE_ORIGIN_UNREACHABLE)

    def test_probe_observation_recomputes_matching_patch_ids(self) -> None:
        disp = load_patch_equivalent_disposition(
            self._write("disp.json", self.s.disposition_dict())
        )
        obs = probe_patch_equivalent_observation(
            self.s.primary, disp, branch=self.s.lane, integration_branch=_INTEGRATION_BRANCH
        )
        self.assertEqual(obs.actual_source_head, self.s.source_head)
        self.assertEqual(obs.actual_integration_head, self.s.integration_head)
        self.assertEqual(
            obs.unintegrated_source_commits, frozenset(self.s.source_commits)
        )
        for src, integ in zip(self.s.source_commits, self.s.integration_commits):
            self.assertTrue(obs.patch_ids[src])
            self.assertEqual(obs.patch_ids[src], obs.patch_ids[integ])


# ---------------------------------------------------------------------------
# 3. The command boundary over the three residual-lane journal shapes.
# ---------------------------------------------------------------------------

#: The live #14066 residual rows (issue, lane label): #13846 two rows, #13879 one row. The lane
#: labels are the representative journal shape the retire terminalizes via patch-equivalence.
_RESIDUAL_LANES = (
    ("13846", "issue_13846_fresh_generation_binding"),
    ("13846", "issue_13846_worker_dispatch_admission"),
    ("13879", "issue_13879_hibernated_pin_repair"),
)


class PatchEquivalentCommandBoundary(unittest.TestCase):
    """``sublane retire --retire-hibernated-bound --integration-disposition-json`` end to end."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._prev_home = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)

        # A fake herdr inventory: the coordinator's default-lane pair only (no lane slot), so
        # every lane unit measures ZERO live managed slots — the #13845 live-zero shape.
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

    def _scenario(self, issue: str, lane: str) -> _Scenario:
        return _Scenario(self.tmp / f"sc_{lane}", lane, issue)

    def _write(self, root: Path, payload: dict) -> str:
        p = root / "disposition.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def _args(
        self, s: _Scenario, *, disposition_path, integration_branch=_INTEGRATION_BRANCH
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
            integration_disposition_json=disposition_path,
        )

    def _run(self, args) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sublane_lifecycle_command.cmd_sublane_retire(args)
        return code, json.loads(buffer.getvalue())

    def _disposition(self, payload) -> dict:
        return payload.get("hibernated_bound_retire", {})

    def _seed(self, s: _Scenario) -> None:
        _seed_hibernated_released_bound(
            LaneLifecycleKey(_WORKSPACE_ID, s.lane), s.issue, s.bound_token
        )

    # -- the three residual-lane journal shapes ---------------------------

    def test_all_three_residual_lanes_terminalize(self) -> None:
        for issue, lane in _RESIDUAL_LANES:
            with self.subTest(lane=lane):
                s = self._scenario(issue, lane)
                self._seed(s)
                path = self._write(s.primary, s.disposition_dict())
                code, payload = self._run(self._args(s, disposition_path=path))
                self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
                self.assertEqual(
                    self._disposition(payload)["state"], BOUND_RETIRE_RETIRED
                )
                self.assertTrue(payload["retire_ok"])
                rec = LaneLifecycleStore().get(LaneLifecycleKey(_WORKSPACE_ID, lane))
                self.assertEqual(rec.lane_disposition, DISPOSITION_RETIRED)
                # Metadata only: the guarded close was never reached.
                self.assertEqual(self.executed_closes, [])

    def test_residual_lane_replay_is_verified_noop(self) -> None:
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        path = self._write(s.primary, s.disposition_dict())
        self.assertEqual(self._run(self._args(s, disposition_path=path))[0], 0)
        code, payload = self._run(self._args(s, disposition_path=path))
        self.assertEqual(code, 0)
        self.assertEqual(
            self._disposition(payload)["state"], BOUND_RETIRE_ALREADY_RETIRED
        )
        self.assertEqual(self.executed_closes, [])

    def test_residual_lane_replay_with_relaunched_pair_fails_closed(self) -> None:
        # The #13841 j#79150 F2 invariant, carried into #14066: a persisted `retired` never
        # reports success while a pair is live again.
        issue, lane = _RESIDUAL_LANES[1]
        s = self._scenario(issue, lane)
        self._seed(s)
        path = self._write(s.primary, s.disposition_dict())
        self.assertEqual(self._run(self._args(s, disposition_path=path))[0], 0)
        self.rows.extend(
            [
                {"name": encode_assigned_name(_WORKSPACE_ID, "codex", lane), "pane_id": "w9:pA"},
                {"name": encode_assigned_name(_WORKSPACE_ID, "claude", lane), "pane_id": "w9:pB"},
            ]
        )
        code, payload = self._run(self._args(s, disposition_path=path))
        self.assertEqual(code, 1)
        self.assertEqual(
            self._disposition(payload)["reason"], BOUND_RETIRE_LIVE_PAIR_PRESENT
        )
        self.assertFalse(payload["retire_ok"])

    # -- non-regression: no disposition keeps the literal head_not_integrated

    def test_no_disposition_keeps_literal_head_not_integrated(self) -> None:
        issue, lane = _RESIDUAL_LANES[2]
        s = self._scenario(issue, lane)
        self._seed(s)
        code, payload = self._run(self._args(s, disposition_path=None))
        self.assertEqual(code, 1)
        self.assertEqual(
            self._disposition(payload)["reason"], BOUND_RETIRE_HEAD_NOT_INTEGRATED
        )
        rec = LaneLifecycleStore().get(LaneLifecycleKey(_WORKSPACE_ID, lane))
        self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)

    # -- negative: tampered / stale / malformed evidence fails closed -----

    def test_tampered_patch_id_disposition_fails_closed(self) -> None:
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        d = s.disposition_dict()
        d["commit_map"][0]["patch_id"] = "cafebabe" * 5
        path = self._write(s.primary, d)
        code, payload = self._run(self._args(s, disposition_path=path))
        self.assertEqual(code, 1)
        verdict = self._disposition(payload)
        self.assertEqual(verdict["state"], BOUND_RETIRE_BLOCKED)
        self.assertEqual(verdict["reason"], BOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED)
        self.assertIn(PE_PATCH_ID_MISMATCH, verdict["detail"])
        rec = LaneLifecycleStore().get(LaneLifecycleKey(_WORKSPACE_ID, lane))
        self.assertEqual(rec.lane_disposition, DISPOSITION_HIBERNATED)
        self.assertEqual(self.executed_closes, [])

    def test_stale_integration_head_disposition_fails_closed(self) -> None:
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        path = self._write(s.primary, s.disposition_dict(integration_head="a" * 40))
        code, payload = self._run(self._args(s, disposition_path=path))
        self.assertEqual(code, 1)
        verdict = self._disposition(payload)
        self.assertEqual(verdict["reason"], BOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED)
        self.assertIn(PE_INTEGRATION_HEAD_STALE, verdict["detail"])

    def test_malformed_disposition_fails_closed(self) -> None:
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        p = s.primary / "disposition.json"
        p.write_text("{ broken", encoding="utf-8")
        code, payload = self._run(self._args(s, disposition_path=str(p)))
        self.assertEqual(code, 1)
        verdict = self._disposition(payload)
        self.assertEqual(verdict["reason"], BOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED)
        self.assertIn(PE_EVIDENCE_UNREADABLE, verdict["detail"])

    def test_wrong_lane_disposition_fails_closed(self) -> None:
        # A disposition captured for another lane must never license this one.
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        path = self._write(s.primary, s.disposition_dict(lane="issue_99999_foreign"))
        code, payload = self._run(self._args(s, disposition_path=path))
        self.assertEqual(code, 1)
        self.assertEqual(
            self._disposition(payload)["reason"],
            BOUND_RETIRE_PATCH_EQUIVALENCE_UNVERIFIED,
        )

    def test_literal_ancestor_ignores_disposition(self) -> None:
        # When --branch IS a literal ancestor of --integration-branch, the literal path wins and
        # the disposition is not even consulted (byte-identical #13845). Point --integration-branch
        # at the lane branch itself (a trivial literal ancestor) with a bogus disposition present.
        issue, lane = _RESIDUAL_LANES[0]
        s = self._scenario(issue, lane)
        self._seed(s)
        bogus = self._write(s.primary, {"garbage": True})
        args = self._args(s, disposition_path=bogus, integration_branch=lane)
        code, payload = self._run(args)
        self.assertEqual(code, 0, msg=json.dumps(payload, indent=2))
        self.assertEqual(self._disposition(payload)["state"], BOUND_RETIRE_RETIRED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
