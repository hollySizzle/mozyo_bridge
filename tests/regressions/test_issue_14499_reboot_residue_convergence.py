"""Regression pins for the #14499 post-reboot residue / missing-worktree convergence.

Redmine #14499 (parent #13490), live evidence #13490 j#89060. After a Mac reboot the host was
left in a shape no public rail could converge, and one read-only preflight *crashed* on it:

- 15 of 23 assigned herdr panes carried no Codex / Claude process — a foreground ``-zsh``,
  cwd ``$HOME``, revision 0, status unknown (the #13518 shell residue) — across 8 issue lanes;
- ``sublane list`` read ``detached`` for all 8 while every lifecycle row read
  ``active / process_release=not_requested``;
- every recorded worktree (all under ``/private/tmp``) was gone, leaving prunable git
  administrative entries. Branches and origin commits survived;
- the production ``sublane retire`` read-only preflight for six closed issues returned an
  **uncaught ``FileNotFoundError``** rather than a typed block, effects 0 (#14203 j#89077,
  #14476 j#89076, #14478 j#89078, #14479 j#89079, #14480 j#89080, #14482 j#89081);
- and #14456 j#87973 recorded a lane that no terminal rail accepted at all: closed issue,
  ACTIVE row, **empty** ``worktree_identity``, live-zero.

Five layers are pinned here, all synthetic (an isolated lifecycle sqlite, fabricated
inventory rows, real temporary git repos where a git fact is under test) — never the shared
``$HOME/.mozyo_bridge``, never a live pane, process, worktree or route mutation:

1. **RB1** — a missing worktree is a typed ``worktree_missing_after_reboot`` block and the
   ``FileNotFoundError`` cannot escape the read-only probe (over a REAL missing path, so the
   pin fails if the OSError guard is removed);
2. **RB2 / RB3 / RB5 / RB8** — the pure convergence planner's full matrix, including that
   unknown axes never become values, that cleanup is offered only after terminalization, and
   that no plan ever proposes deleting a branch or a commit;
3. **RB6** — the residue close planner, including the 15-pane reboot fixture with **zero
   foreign close**, the #14479 partial-pair shape, and idempotent replay;
4. **RB4** — the ``LaneActiveUnboundRetireStore`` CAS guard matrix (the #14456 shape
   terminalizes; every off-signature shape is refused zero-write, including the bound row
   that belongs to #14242);
5. **non-regression** — the four pre-existing retire intents keep refusing what they refused,
   and the new one refuses their targets.

Boundary (Redmine #14499): no process launch / close / resume, no worktree / branch / commit
removal, no raw Herdr / tmux, no origin/main, no production / tag / publish.
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
from unittest import mock
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.lane_active_unbound_retire import (  # noqa: E402
    LaneActiveUnboundRetireStore,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    RELEASE_NOT_REQUESTED,
    RELEASE_PARTIAL,
    RELEASE_REQUESTED,
    DecisionPointer,
    LaneLifecycleKey,
    ProcessGenerationPin,
    ReleasePin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (  # noqa: E402
    LiveSublaneGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_close_plan import (  # noqa: E402
    PRESERVED_ACTIVE_STATUS,
    PRESERVED_LIVE_AGENT,
    PRESERVED_NO_LOCATOR,
    expected_lane_slot_names,
    plan_residue_close,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E402
    CONVERGE_ALREADY_TERMINAL,
    CONVERGE_BLOCKED,
    CONVERGE_CLOSE_RESIDUE,
    CONVERGE_GUARDED_CLOSE,
    CONVERGE_HIBERNATE,
    CONVERGE_RESTORE_WORKTREE,
    CONVERGE_RESUME,
    CONVERGE_SUPERSEDE,
    CONVERGE_TERMINALIZE_BOUND,
    CONVERGE_TERMINALIZE_UNBOUND,
    CONVERGE_UNKNOWN,
    REASON_FOREIGN_OCCUPANT,
    REASON_HEAD_NOT_INTEGRATED,
    REASON_INVENTORY_UNREADABLE,
    REASON_ISSUE_STATE_UNREADABLE,
    REASON_RELEASE_IN_FLIGHT,
    REASON_WORKTREE_PRESENCE_UNKNOWN,
    RebootLaneFacts,
    RebootSlotFact,
    plan_lane_convergence,
    summarize_convergences,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E402
    BLOCKED_DIRTY_WORKTREE,
    BLOCKED_WORKTREE_MISSING_AFTER_REBOOT,
    INTEGRATION_BLOCKED,
    RETIRE_OK,
    RetirePreflight,
    SublaneIntegrationPolicy,
    decide_retire_integration,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E402
    SLOT_LIVE,
    SLOT_STALE,
)

_WORKSPACE_ID = "wProj"
_LANE = "issue_14456_startup_status_projection_r2"
_ISSUE = "14456"
_JOURNAL = "87973"
_ROLES = ("codex", "claude")


def _key(ws: str = _WORKSPACE_ID, lane: str = _LANE) -> LaneLifecycleKey:
    return LaneLifecycleKey(ws, lane)


def _decision(issue: str = _ISSUE, journal: str = _JOURNAL) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=issue, journal_id=journal)


def _name(role: str, lane: str = _LANE, ws: str = _WORKSPACE_ID) -> str:
    return encode_assigned_name(ws, role, lane)


def _residue_row(role: str, locator: str, lane: str = _LANE, ws: str = _WORKSPACE_ID):
    """The measured reboot shape: the name row survives, no agent, status unknown.

    Mirrors #13490 j#89060 exactly — a foreground ``-zsh`` with the ``agent`` field reported
    but blank, ``revision 0`` and an unrecognised status.
    """
    return {
        "name": _name(role, lane, ws),
        "pane": locator,
        "agent": "",
        "agent_status": "unknown",
        "revision": 0,
    }


def _live_row(role: str, locator: str, lane: str = _LANE, ws: str = _WORKSPACE_ID):
    return {
        "name": _name(role, lane, ws),
        "pane": locator,
        "agent": role,
        "agent_status": "idle",
    }


# ---------------------------------------------------------------------------
# 1. RB1 — the missing worktree is typed, and the traceback cannot escape.
# ---------------------------------------------------------------------------


class MissingWorktreePreflightTests(unittest.TestCase):
    """The six production zero-effect runs: a gone worktree must be a typed block."""

    def test_probe_over_a_real_missing_path_does_not_raise(self):
        """The root cause, pinned over a REAL missing directory.

        ``subprocess.run(cwd=<missing>)`` raises ``FileNotFoundError`` rather than exiting
        non-zero, which is how an OS error escaped a read-only preflight. Removing the guard
        in ``LiveSublaneGitOperations._run`` makes this test error out, not merely assert.
        """
        with tempfile.TemporaryDirectory() as tmp:
            gone = Path(tmp) / "worktree_removed_by_reboot"
            self.assertFalse(gone.exists())
            ops = LiveSublaneGitOperations(repo_root=gone)
            # Each probe keeps its own documented fail-closed reading of a failed result.
            self.assertTrue(ops.worktree_dirty(), "an uninspectable worktree must read dirty")
            self.assertFalse(ops.is_git_workspace())
            self.assertFalse(ops.worktree_exists("any-branch"))
            self.assertFalse(ops.integration_branch_resolved("any-branch"))

    def test_sanity_the_raw_subprocess_really_does_raise(self):
        """Non-vacuity: the defect this guards is real, not hypothetical."""
        with tempfile.TemporaryDirectory() as tmp:
            gone = Path(tmp) / "nope"
            with self.assertRaises(FileNotFoundError):
                subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=gone,
                    text=True,
                    capture_output=True,
                )

    def test_missing_worktree_blocks_as_itself_not_as_dirty(self):
        preflight = RetirePreflight(
            is_git_workspace=True,
            worktree_dirty=True,  # the fail-closed reading of the unreadable path
            worktree_missing=True,
            target_identity_known=True,
            verification_passed=True,
            issue_closed=True,
            callbacks_drained=True,
            durable_record_recorded=True,
            latest_generation_admissible=True,
        )
        decision = decide_retire_integration(
            SublaneIntegrationPolicy(merge_on_retire=False), preflight
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertEqual(decision.primary_reason, BLOCKED_WORKTREE_MISSING_AFTER_REBOOT)
        # "Dirty" would tell the coordinator to go commit work that has no checkout to
        # live in. The two are mutually exclusive.
        self.assertNotIn(BLOCKED_DIRTY_WORKTREE, decision.blocked_reasons)

    def test_a_genuinely_dirty_present_worktree_still_reports_dirty(self):
        preflight = RetirePreflight(
            is_git_workspace=True,
            worktree_dirty=True,
            worktree_missing=False,
            target_identity_known=True,
            verification_passed=True,
            issue_closed=True,
            callbacks_drained=True,
            durable_record_recorded=True,
            latest_generation_admissible=True,
        )
        decision = decide_retire_integration(
            SublaneIntegrationPolicy(merge_on_retire=False), preflight
        )
        self.assertEqual(decision.primary_reason, BLOCKED_DIRTY_WORKTREE)
        self.assertNotIn(
            BLOCKED_WORKTREE_MISSING_AFTER_REBOOT, decision.blocked_reasons
        )

    def test_default_preflight_is_byte_for_byte_unchanged(self):
        """The new field defaults to the non-blocking value: no existing caller moves."""
        clean = RetirePreflight(
            is_git_workspace=True,
            target_identity_known=True,
            verification_passed=True,
            issue_closed=True,
            callbacks_drained=True,
            durable_record_recorded=True,
            latest_generation_admissible=True,
        )
        self.assertFalse(clean.worktree_missing)
        self.assertEqual(
            decide_retire_integration(
                SublaneIntegrationPolicy(merge_on_retire=False), clean
            ).state,
            RETIRE_OK,
        )

    def test_retire_command_over_a_missing_worktree_is_typed_not_a_traceback(self):
        """The command boundary: the six production runs, replayed.

        A real (present) coordinator repo, a ``--worktree`` that does not exist, and every
        durable-record invariant asserted so nothing else can be the blocker. The command
        must exit non-zero with the typed reason in its JSON — and must not raise.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            cmd_sublane_retire,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            gone = Path(tmp) / "private_tmp_worktree_wiped_by_reboot"
            args = argparse.Namespace(
                issue="14203",
                journal="89077",
                lane_label="issue_14203_lane",
                worktree=str(gone),
                branch="issue_14203_lane",
                integration_branch="main",
                issue_closed=True,
                callbacks_drained=True,
                verified=True,
                durable_record=True,
                target_identity_known=True,
                latest_generation_admissible=True,
                review_generation_json=None,
                execute=False,
                migrate_hibernated_legacy=False,
                reconcile_hibernated_live=False,
                retire_hibernated_bound=False,
                retire_active_live_zero=False,
                retire_active_unbound_live_zero=False,
                integration_journal=None,
                repo=str(repo),
                json=True,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cmd_sublane_retire(args)
        self.assertEqual(rc, 1, "a blocked preflight must not exit 0")
        payload = json.loads(buf.getvalue())
        reasons = payload["decision"]["blocked_reasons"]
        self.assertIn(BLOCKED_WORKTREE_MISSING_AFTER_REBOOT, reasons)
        self.assertNotIn(BLOCKED_DIRTY_WORKTREE, reasons)
        self.assertFalse(payload["retire_ok"])


# ---------------------------------------------------------------------------
# 2. RB2 / RB3 / RB5 / RB8 — the pure convergence planner.
# ---------------------------------------------------------------------------


def _facts(**kw) -> RebootLaneFacts:
    base = dict(
        workspace_id=_WORKSPACE_ID,
        lane_id=_LANE,
        issue_id=_ISSUE,
        lane_disposition=DISPOSITION_ACTIVE,
        process_release=RELEASE_NOT_REQUESTED,
        worktree_identity="wt_lane_token",
        recorded_worktree="/private/tmp/mozyo_bridge_issue_14456_r2",
        worktree_present=True,
        branch="issue_14456_r2",
        branch_exists=True,
        head_integrated=True,
        issue_closed=True,
        slots=(),
    )
    base.update(kw)
    return RebootLaneFacts(**base)


def _slot(role: str, liveness: str, *, locator: str = "w3N:pF", foreign: bool = False):
    return RebootSlotFact(
        role=role,
        assigned_name=_name(role) if not foreign else "mzb1_wProj_gemini_other",
        locator=locator,
        liveness=liveness,
        foreign=foreign,
    )


class ConvergencePlannerMatrix(unittest.TestCase):
    def test_unreadable_inventory_yields_unknown_not_a_plan(self):
        plan = plan_lane_convergence(_facts(slots=None))
        self.assertEqual(plan.convergence, CONVERGE_UNKNOWN)
        self.assertEqual(plan.reason, REASON_INVENTORY_UNREADABLE)
        self.assertFalse(plan.actionable)

    def test_unread_issue_state_yields_unknown_never_closed(self):
        plan = plan_lane_convergence(_facts(issue_closed=None))
        self.assertEqual(plan.convergence, CONVERGE_UNKNOWN)
        self.assertEqual(plan.reason, REASON_ISSUE_STATE_UNREADABLE)

    def test_retired_row_is_terminal_and_unlocks_cleanup(self):
        plan = plan_lane_convergence(
            _facts(lane_disposition=DISPOSITION_RETIRED, worktree_present=False)
        )
        self.assertEqual(plan.convergence, CONVERGE_ALREADY_TERMINAL)
        self.assertTrue(plan.cleanup_permitted)
        self.assertTrue(any("prune" in step for step in plan.steps))

    def test_cleanup_is_offered_only_after_terminalization(self):
        """RB8: every non-terminal verdict withholds cleanup."""
        for kw in (
            {},
            {"issue_closed": False},
            {"worktree_present": False},
            {"slots": (_slot("codex", SLOT_STALE),)},
            {"worktree_identity": ""},
        ):
            plan = plan_lane_convergence(_facts(**kw))
            self.assertFalse(
                plan.cleanup_permitted,
                f"{plan.convergence} must not unlock cleanup",
            )

    def test_no_plan_ever_proposes_deleting_a_branch_or_commit(self):
        """RB8: branches and commits survived the reboot and are never cleanup targets."""
        forbidden = ("branch -d", "branch -D", "push --delete", "reset --hard", "reflog")
        for kw in (
            {},
            {"lane_disposition": DISPOSITION_RETIRED},
            {"lane_disposition": DISPOSITION_RETIRED, "worktree_present": False},
            {"issue_closed": False},
            {"worktree_present": False},
            {"worktree_identity": ""},
            {"slots": (_slot("codex", SLOT_STALE),)},
            {"peer_active_lanes": ("issue_14456_r1",), "issue_closed": False},
        ):
            plan = plan_lane_convergence(_facts(**kw))
            for step in plan.steps:
                for token in forbidden:
                    self.assertNotIn(token, step, f"{plan.convergence}: {step}")

    # -- RB5: the open-issue classification ---------------------------------

    def test_open_issue_with_a_live_pair_resumes(self):
        plan = plan_lane_convergence(
            _facts(
                issue_closed=False,
                slots=(_slot("codex", SLOT_LIVE), _slot("claude", SLOT_LIVE)),
            )
        )
        self.assertEqual(plan.convergence, CONVERGE_RESUME)

    def test_open_issue_with_live_zero_hibernates_never_terminalizes(self):
        plan = plan_lane_convergence(
            _facts(issue_closed=False, slots=(_slot("codex", SLOT_STALE),))
        )
        self.assertEqual(plan.convergence, CONVERGE_HIBERNATE)

    def test_open_issue_with_a_peer_active_owner_supersedes(self):
        plan = plan_lane_convergence(
            _facts(issue_closed=False, peer_active_lanes=("issue_14456_r1",))
        )
        self.assertEqual(plan.convergence, CONVERGE_SUPERSEDE)

    def test_an_open_lane_never_receives_a_terminal_verdict(self):
        for kw in (
            {"slots": ()},
            {"slots": (_slot("codex", SLOT_STALE),)},
            {"worktree_identity": ""},
            {"worktree_present": False},
        ):
            plan = plan_lane_convergence(_facts(issue_closed=False, **kw))
            self.assertNotIn(
                plan.convergence,
                (CONVERGE_TERMINALIZE_BOUND, CONVERGE_TERMINALIZE_UNBOUND),
            )

    # -- RB3 / RB4: the closed-issue classification -------------------------

    def test_closed_issue_with_a_live_pair_uses_the_guarded_close(self):
        plan = plan_lane_convergence(_facts(slots=(_slot("codex", SLOT_LIVE),)))
        self.assertEqual(plan.convergence, CONVERGE_GUARDED_CLOSE)

    def test_closed_issue_with_residue_closes_residue_first(self):
        plan = plan_lane_convergence(_facts(slots=(_slot("codex", SLOT_STALE),)))
        self.assertEqual(plan.convergence, CONVERGE_CLOSE_RESIDUE)
        self.assertEqual(len(plan.residue_slots), 1)

    def test_closed_bound_live_zero_with_a_present_worktree_terminalizes(self):
        plan = plan_lane_convergence(_facts())
        self.assertEqual(plan.convergence, CONVERGE_TERMINALIZE_BOUND)

    def test_closed_bound_live_zero_with_a_missing_worktree_offers_restore(self):
        """RB3: the reboot's characteristic shape — restore, or terminalize as metadata."""
        plan = plan_lane_convergence(_facts(worktree_present=False))
        self.assertEqual(plan.convergence, CONVERGE_RESTORE_WORKTREE)
        self.assertIn(CONVERGE_TERMINALIZE_BOUND, plan.alternatives)
        self.assertTrue(any("git worktree add" in s for s in plan.steps))

    def test_unknown_worktree_presence_is_not_a_missing_worktree(self):
        plan = plan_lane_convergence(_facts(worktree_present=None))
        self.assertEqual(plan.convergence, CONVERGE_UNKNOWN)
        self.assertEqual(plan.reason, REASON_WORKTREE_PRESENCE_UNKNOWN)

    def test_closed_unbound_live_zero_is_the_14456_shape(self):
        """RB4: the j#87973 row every pre-#14499 rail refused."""
        plan = plan_lane_convergence(
            _facts(worktree_identity="", worktree_present=False)
        )
        self.assertEqual(plan.convergence, CONVERGE_TERMINALIZE_UNBOUND)
        self.assertTrue(
            any("--retire-active-unbound-live-zero" in s for s in plan.steps)
        )
        # The generation/revision fence is what replaces the worktree attestation, so the
        # recommended command must carry it.
        joined = " ".join(plan.steps)
        self.assertIn("--expect-lane-generation", joined)
        self.assertIn("--expect-lane-revision", joined)

    def test_unintegrated_head_blocks_every_terminal_verdict(self):
        for kw in ({"worktree_identity": ""}, {}, {"worktree_present": False}):
            plan = plan_lane_convergence(_facts(head_integrated=False, **kw))
            self.assertEqual(plan.convergence, CONVERGE_BLOCKED)
            self.assertEqual(plan.reason, REASON_HEAD_NOT_INTEGRATED)

    def test_unmeasurable_integration_also_blocks(self):
        plan = plan_lane_convergence(_facts(head_integrated=None))
        self.assertEqual(plan.reason, REASON_HEAD_NOT_INTEGRATED)

    def test_a_foreign_occupant_blocks_everything(self):
        plan = plan_lane_convergence(
            _facts(slots=(_slot("gemini", SLOT_LIVE, foreign=True),))
        )
        self.assertEqual(plan.convergence, CONVERGE_BLOCKED)
        self.assertEqual(plan.reason, REASON_FOREIGN_OCCUPANT)

    def test_a_release_in_flight_blocks_every_verdict(self):
        for state in (RELEASE_REQUESTED, RELEASE_PARTIAL):
            plan = plan_lane_convergence(_facts(process_release=state))
            self.assertEqual(plan.convergence, CONVERGE_BLOCKED)
            self.assertEqual(plan.reason, REASON_RELEASE_IN_FLIGHT)

    def test_summary_is_a_count_not_an_action(self):
        plans = [
            plan_lane_convergence(_facts()),
            plan_lane_convergence(_facts(worktree_present=False)),
            plan_lane_convergence(_facts(issue_closed=False)),
        ]
        summary = summarize_convergences(plans)
        self.assertEqual(
            summary,
            {
                CONVERGE_HIBERNATE: 1,
                CONVERGE_RESTORE_WORKTREE: 1,
                CONVERGE_TERMINALIZE_BOUND: 1,
            },
        )


class RebootFifteenPaneSnapshotTest(unittest.TestCase):
    """The j#89060 shape: 8 lanes, mixed dispositions, every worktree gone.

    Pins that one snapshot yields DIFFERENT rails per lane — the reason Required behavior 5
    forbids a workspace-wide action.
    """

    def test_eight_lane_reboot_snapshot_needs_several_different_rails(self):
        lanes = [
            # closed + bound + live-zero + worktree gone -> restore (or terminalize)
            _facts(lane_id="l1", worktree_present=False),
            # closed + UNBOUND + live-zero -> the #14456 rail
            _facts(lane_id="l2", worktree_identity="", worktree_present=False),
            # closed + residue still present -> close residue first
            _facts(
                lane_id="l3",
                worktree_present=False,
                slots=(_slot("codex", SLOT_STALE), _slot("claude", SLOT_STALE)),
            ),
            # open + live-zero -> hibernate, never terminal
            _facts(lane_id="l4", issue_closed=False, worktree_present=False),
            # open + live pair -> resume
            _facts(
                lane_id="l5",
                issue_closed=False,
                slots=(_slot("codex", SLOT_LIVE), _slot("claude", SLOT_LIVE)),
            ),
            # already retired -> cleanup unlocked
            _facts(
                lane_id="l6",
                lane_disposition=DISPOSITION_RETIRED,
                worktree_present=False,
            ),
            # unintegrated -> blocked
            _facts(lane_id="l7", head_integrated=False, worktree_present=False),
            # inventory unreadable -> unknown
            _facts(lane_id="l8", slots=None),
        ]
        plans = [plan_lane_convergence(f) for f in lanes]
        verdicts = [p.convergence for p in plans]
        self.assertEqual(
            verdicts,
            [
                CONVERGE_RESTORE_WORKTREE,
                CONVERGE_TERMINALIZE_UNBOUND,
                CONVERGE_CLOSE_RESIDUE,
                CONVERGE_HIBERNATE,
                CONVERGE_RESUME,
                CONVERGE_ALREADY_TERMINAL,
                CONVERGE_BLOCKED,
                CONVERGE_UNKNOWN,
            ],
        )
        # Exactly one lane may be cleaned up, and it is the terminal one.
        self.assertEqual([p.lane_id for p in plans if p.cleanup_permitted], ["l6"])
        self.assertGreaterEqual(len(summarize_convergences(plans)), 7)


# ---------------------------------------------------------------------------
# 3. RB6 — the lane-scoped residue close planner.
# ---------------------------------------------------------------------------


class ResidueClosePlannerTests(unittest.TestCase):
    def _plan(self, rows, lane: str = _LANE, legacy: str = ""):
        return plan_residue_close(
            rows,
            workspace_id=_WORKSPACE_ID,
            lane_id=lane,
            legacy_workspace_id=legacy,
            managed_roles=_ROLES,
        )

    def test_exact_name_residue_closes(self):
        plan = self._plan([_residue_row("codex", "w3N:pF")])
        self.assertEqual(
            plan.close_targets, ((_name("codex"), "w3N:pF"),)
        )
        self.assertFalse(plan.pair_fence_tripped)

    def test_a_foreign_row_is_never_a_target(self):
        rows = [
            {"name": "mzb1_wProj_gemini_" + _LANE, "pane": "w3N:pZ", "agent": ""},
            {"name": "not-an-mzb1-name", "pane": "w3N:pY"},
        ]
        plan = self._plan(rows)
        self.assertEqual(plan.close_targets, ())

    def test_the_default_lane_coordinator_pair_is_structurally_unreachable(self):
        """A default-lane slot of a project workspace is the coordinator; never a target."""
        rows = [
            {
                "name": encode_assigned_name(_WORKSPACE_ID, "codex", "default"),
                "pane": "w1:p1",
                "agent": "",
                "agent_status": "unknown",
            }
        ]
        self.assertEqual(self._plan(rows).close_targets, ())
        # And the lane's own expected-name set never contains it.
        self.assertNotIn(
            encode_assigned_name(_WORKSPACE_ID, "codex", "default"),
            expected_lane_slot_names(
                workspace_id=_WORKSPACE_ID, lane_id=_LANE, managed_roles=_ROLES
            ),
        )

    def test_another_lanes_residue_is_never_a_target(self):
        plan = self._plan([_residue_row("codex", "w9:p9", lane="issue_99999_other")])
        self.assertEqual(plan.close_targets, ())
        self.assertEqual(len(plan.untouched_names), 1)

    def test_a_busy_or_prompted_pane_is_preserved_even_when_classified_stale(self):
        """Stricter than the liveness classifier: observable activity is never closed."""
        for status in ("working", "blocked", "idle", "done"):
            row = _residue_row("codex", "w3N:pF")
            row["agent_status"] = status
            plan = self._plan([row])
            self.assertEqual(plan.close_targets, (), f"status={status}")
            self.assertEqual(plan.preserved[0].preserved_reason, PRESERVED_ACTIVE_STATUS)

    def test_a_locator_less_row_has_nothing_to_close(self):
        row = _residue_row("codex", "")
        row.pop("pane")
        plan = self._plan([row])
        self.assertEqual(plan.close_targets, ())
        self.assertEqual(plan.preserved[0].preserved_reason, PRESERVED_NO_LOCATOR)

    def test_a_live_half_collapses_the_whole_plan(self):
        """The pair fence: never close one half of a working pair."""
        plan = self._plan([_residue_row("codex", "w3N:pF"), _live_row("claude", "w3N:pG")])
        self.assertTrue(plan.pair_fence_tripped)
        self.assertEqual(plan.close_targets, ())
        self.assertTrue(
            any(c.preserved_reason == PRESERVED_LIVE_AGENT for c in plan.preserved)
        )

    def test_partial_pair_one_residue_one_absent_still_converges(self):
        """The #14479 shape: an absent slot is not a live one."""
        plan = self._plan([_residue_row("codex", "w3N:pF")])
        self.assertFalse(plan.pair_fence_tripped)
        self.assertEqual(len(plan.close_targets), 1)

    def test_idempotent_replay_after_the_close_plans_nothing(self):
        rows = [_residue_row("codex", "w3N:pF"), _residue_row("claude", "w3N:pG")]
        first = self._plan(rows)
        self.assertEqual(len(first.close_targets), 2)
        # After the close the rows are gone from the inventory.
        self.assertEqual(self._plan([]).close_targets, ())

    def test_fifteen_pane_reboot_fixture_closes_only_this_lane_zero_foreign(self):
        """The j#89060 fixture: 8 lanes' residue + coordinator + foreign, one lane targeted."""
        rows = []
        lane_labels = [f"issue_1{n}_lane" for n in range(8)]
        for label in lane_labels:
            rows.append(_residue_row("codex", f"w3N:p{label}c", lane=label))
            rows.append(_residue_row("claude", f"w3N:p{label}w", lane=label))
        # 16 lane slots; drop one to reach the measured 15 (the #14479 partial pair).
        rows.pop()
        # The coordinator's own default-lane pair, plus foreign / undecodable occupants.
        rows.append(
            {
                "name": encode_assigned_name(_WORKSPACE_ID, "codex", "default"),
                "pane": "w1:p1",
                "agent": "codex",
                "agent_status": "idle",
            }
        )
        rows.append(
            {
                "name": encode_assigned_name(_WORKSPACE_ID, "claude", "default"),
                "pane": "w1:p2",
                "agent": "claude",
                "agent_status": "idle",
            }
        )
        rows.append({"name": "some-hand-rolled-shell", "pane": "w1:p3"})
        self.assertEqual(len(rows), 15 + 3)

        target = lane_labels[0]
        plan = self._plan(rows, lane=target)
        # Exactly this lane's two slots, and nothing else in the whole 18-row inventory.
        self.assertEqual(
            sorted(name for name, _ in plan.close_targets),
            sorted([_name("codex", target), _name("claude", target)]),
        )
        # Zero foreign close: every other row in the 18-row inventory — the other seven
        # lanes' 13 residue slots, the coordinator's live default-lane pair, and the
        # undecodable hand-rolled shell — is outside the target set entirely.
        targeted_locators = {loc for _n, loc in plan.close_targets}
        self.assertEqual(len(targeted_locators), 2)
        self.assertNotIn("w1:p1", targeted_locators)
        self.assertNotIn("w1:p2", targeted_locators)
        self.assertNotIn("w1:p3", targeted_locators)
        # And each of the other seven lanes converges independently, never in bulk: a plan
        # aimed at one lane only ever names that lane's own canonical slot names.
        for label in lane_labels[1:]:
            other = self._plan(rows, lane=label)
            allowed = {_name(role, label) for role in _ROLES}
            self.assertTrue(
                {name for name, _loc in other.close_targets} <= allowed,
                f"{label} plan reached outside its own slots",
            )
            self.assertTrue(other.close_targets, f"{label} should still converge")


# ---------------------------------------------------------------------------
# 4. RB4 — the bounded active-UNBOUND retire CAS guard matrix.
# ---------------------------------------------------------------------------


def _pins() -> tuple[ProcessGenerationPin, ...]:
    return (
        ProcessGenerationPin(
            role="gateway",
            provider="codex",
            assigned_name=_name("codex"),
            locator="w3N:pF",
        ),
        ProcessGenerationPin(
            role="worker",
            provider="claude",
            assigned_name=_name("claude"),
            locator="w3N:pG",
        ),
    )


class ActiveUnboundRetireCasMatrix(unittest.TestCase):
    """The #14456 j#87973 signature terminalizes; every other shape is zero-write."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lifecycle.sqlite"
        self.store = LaneActiveUnboundRetireStore(path=self.path)

    def _seed(self, *, worktree_identity: str = "", issue: str = _ISSUE) -> None:
        out = LaneDeclarationStore(path=self.path).declare_lane(
            _key(),
            decision=_decision(issue),
            issue_id=issue,
            declared_slots=_pins(),
            worktree_identity=worktree_identity,
        )
        self.assertTrue(out.applied, f"seed refused: {out.reason}")

    def _row(self):
        return LaneLifecycleStore(path=self.path).get(_key())

    def _retire(self, *, issue: str = _ISSUE, generation=None, revision=None):
        row = self._row()
        return self.store.retire_active_unbound_live_zero(
            _key(),
            expected_revision=row.revision if revision is None else revision,
            expected_generation=(
                row.lane_generation if generation is None else generation
            ),
            issue_id=issue,
            decision=_decision(issue),
        )

    def test_the_14456_shape_terminalizes(self):
        self._seed()
        before = self._row()
        self.assertEqual(before.lane_disposition, DISPOSITION_ACTIVE)
        self.assertEqual(before.process_release, RELEASE_NOT_REQUESTED)
        self.assertEqual(before.worktree_identity, "")
        out = self._retire()
        self.assertTrue(out.applied)
        self.assertEqual(out.reason, CAS_APPLIED)
        after = self._row()
        self.assertEqual(after.lane_disposition, DISPOSITION_RETIRED)
        # Metadata only: the generation, pins and (empty) binding are preserved.
        self.assertEqual(after.lane_generation, before.lane_generation)
        self.assertEqual(after.declared_slots, before.declared_slots)
        self.assertEqual(after.worktree_identity, "")
        self.assertEqual(after.issue_id, before.issue_id)
        self.assertEqual(after.revision, before.revision + 1)

    def test_a_bound_row_belongs_to_14242_and_is_refused_here(self):
        self._seed(worktree_identity="wt_some_lane")
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._row().lane_disposition, DISPOSITION_ACTIVE)

    def test_a_hibernated_row_is_refused(self):
        self._seed()
        row = self._row()
        LaneLifecycleStore(path=self.path).transition_disposition(
            _key(),
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=row.revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_a_different_issue_is_refused(self):
        self._seed()
        out = self._retire(issue="99999")
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_a_stale_revision_loses(self):
        self._seed()
        out = self._retire(revision=self._row().revision + 5)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_STALE_REVISION)
        self.assertEqual(self._row().lane_disposition, DISPOSITION_ACTIVE)

    def test_a_mismatched_generation_loses(self):
        """The fence that replaces the worktree attestation."""
        self._seed()
        out = self._retire(generation=self._row().lane_generation + 1)
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_STALE_REVISION)
        self.assertEqual(self._row().lane_disposition, DISPOSITION_ACTIVE)

    def test_an_undeclared_generation_is_a_construction_error(self):
        self._seed()
        with self.assertRaises(ValueError):
            self.store.retire_active_unbound_live_zero(
                _key(),
                expected_revision=self._row().revision,
                expected_generation=0,
                issue_id=_ISSUE,
                decision=_decision(),
            )

    def test_an_empty_issue_is_a_construction_error(self):
        self._seed()
        with self.assertRaises(ValueError):
            self.store.retire_active_unbound_live_zero(
                _key(),
                expected_revision=self._row().revision,
                expected_generation=1,
                issue_id="",
                decision=_decision(),
            )

    def test_a_release_in_flight_is_refused(self):
        self._seed()
        _force_release_state(self.path, _key(), RELEASE_REQUESTED)
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_FORBIDDEN_TRANSITION)
        self.assertEqual(self._row().lane_disposition, DISPOSITION_ACTIVE)

    def test_an_absent_row_is_not_found(self):
        out = self.store.retire_active_unbound_live_zero(
            _key(),
            expected_revision=1,
            expected_generation=1,
            issue_id=_ISSUE,
            decision=_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)

    def test_a_second_apply_is_refused_the_caller_owns_idempotence(self):
        """The CAS stays strictly ``active -> retired``; replay success is the rail's job."""
        self._seed()
        self.assertTrue(self._retire().applied)
        out = self._retire()
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)


def _force_release_state(path: Path, key: LaneLifecycleKey, state: str) -> None:
    """White-box: set ``process_release`` directly, WITHOUT touching ``revision``.

    The real transitions cannot put an ``active`` row into an in-flight release, so the CAS's
    release backstop is otherwise untestable. The revision is left alone so the CAS still
    sees the revision the caller measured against, isolating the release guard.
    """
    import sqlite3

    from mozyo_bridge.core.state.lane_lifecycle_schema import TABLE

    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            f"UPDATE {TABLE} SET process_release = ? "
            "WHERE repo_workspace_id = ? AND lane_id = ?",
            (state, key.repo_workspace_id, key.lane_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. Non-regression against the four pre-existing retire intents.
# ---------------------------------------------------------------------------


class SiblingIntentsAreNotEroded(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "lifecycle.sqlite"

    def test_14242_still_refuses_an_unbound_row(self):
        """The bound surface must not have been widened to cover this one."""
        from mozyo_bridge.core.state.lane_active_retire import LaneActiveRetireStore

        LaneDeclarationStore(path=self.path).declare_lane(
            _key(),
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=_pins(),
            worktree_identity="",
        )
        row = LaneLifecycleStore(path=self.path).get(_key())
        with self.assertRaises(ValueError):
            LaneActiveRetireStore(path=self.path).retire_active_live_zero(
                _key(),
                expected_revision=row.revision,
                issue_id=_ISSUE,
                worktree_identity="",
                decision=_decision(),
            )

    def test_14499_refuses_the_14242_target(self):
        """And the new surface must not cover the bound one. Disjoint signatures."""
        LaneDeclarationStore(path=self.path).declare_lane(
            _key(),
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=_pins(),
            worktree_identity="wt_bound_lane",
        )
        row = LaneLifecycleStore(path=self.path).get(_key())
        out = LaneActiveUnboundRetireStore(path=self.path).retire_active_unbound_live_zero(
            _key(),
            expected_revision=row.revision,
            expected_generation=row.lane_generation,
            issue_id=_ISSUE,
            decision=_decision(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)

    def test_all_retire_intents_remain_mutually_exclusive(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            cmd_sublane_retire,
        )

        args = argparse.Namespace(
            issue=_ISSUE,
            lane_label=_LANE,
            retire_active_live_zero=True,
            retire_active_unbound_live_zero=True,
            execute=False,
            migrate_hibernated_legacy=False,
            reconcile_hibernated_live=False,
            retire_hibernated_bound=False,
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cmd_sublane_retire(args)
        self.assertEqual(rc, 1)
        self.assertIn("mutually exclusive", err.getvalue())
        self.assertIn("--retire-active-unbound-live-zero", err.getvalue())


class LiveZeroMeasurementIsSharedNotDuplicated(unittest.TestCase):
    """#14242's four fences and #14499's are one definition, not two copies."""

    def test_both_rails_import_the_same_measurement(self):
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_active_live_zero_retire as bound,
            sublane_active_unbound_live_zero_retire as unbound,
        )

        for module in (bound, unbound):
            source = inspect.getsource(module)
            self.assertIn("measure_live_zero", source)
            # The duplicate-slot scan must exist in exactly one place.
            self.assertNotIn("seen_slots", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# 6. Review j#89191 findings 1-4: the adversarial pins the re-review requires.
# ---------------------------------------------------------------------------


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class Finding1LaneSelectionProof(unittest.TestCase):
    """j#89191 finding 1: the CAS must prove WHICH lane, not only that it is fresh.

    **What this fence actually is.** The review asked for a lane-selection proof because the
    CAS's generation/revision predicates are freshness, not selection. That reasoning is
    correct. The failure scenario it described — two ACTIVE rows owning one issue, both at
    generation 1 / revision 1, one of them terminalized by mistake — is nonetheless
    unreachable, and measurably so at **three** independent layers:

    1. the application path refuses (``declare_lane`` and the hibernated→active rehydrate
       both return ``owner_conflict``);
    2. the storage engine makes it unrepresentable — a partial UNIQUE index
       ``idx_lane_lifecycle_active_owner`` on ``(repo_workspace_id, issue_id) WHERE
       lane_disposition = 'active' AND issue_id <> ''``, whose own comment says "original +
       recovery both own the issue" is *unrepresentable rather than merely detected
       afterwards*. A raw ``INSERT`` raises ``sqlite3.IntegrityError``;
    3. and dropping that index to force the state does not help either: the store's schema
       signature check then declares the whole authority corrupt, so **every read fails
       closed** and the rail never reaches its CAS.

    So the owner fence added for this finding is **defense in depth, not a fix for a
    reachable bug**, and the re-review record says so. It is still worth having — this is the
    only terminal rail with no second identity axis — and it does improve one reachable case:
    a ``--lane-label`` naming a row that is not the issue's owner now refuses early with a
    reason that names the owner, instead of falling through to a generic CAS refusal.

    These tests pin all of that: the unreachability (so a future relaxation is caught here,
    next to the reason it matters), and the fence's behaviour at its own boundary.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.path = self.home / "state.sqlite"

    def _seed(self, lane: str, issue: str = _ISSUE) -> None:
        out = LaneDeclarationStore(path=self.path).declare_lane(
            LaneLifecycleKey(_WORKSPACE_ID, lane),
            decision=_decision(issue),
            issue_id=issue,
            declared_slots=_pins(),
            worktree_identity="",
        )
        self.assertTrue(out.applied, out.reason)

    def _terminalize(self, lane_label: str, *, generation: int = 1, revision: int = 1):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
            _terminalize_under_exclusion,
        )

        return _terminalize_under_exclusion(
            argparse.Namespace(
                lane_label=lane_label, issue=_ISSUE, journal=_JOURNAL, worktree=None,
                branch="b", integration_branch="main",
            ),
            Path("."),
            workspace_id=_WORKSPACE_ID,
            lane_label=lane_label,
            issue=_ISSUE,
            journal=_JOURNAL,
            expect_generation=generation,
            expect_revision=revision,
        )

    def test_two_active_owners_are_unrepresentable_not_merely_refused(self):
        """Layers 1 and 2 of the unreachability, measured rather than assumed."""
        import sqlite3

        from mozyo_bridge.core.state.lane_lifecycle_schema import TABLE

        self._seed("lane_r1")
        second = LaneDeclarationStore(path=self.path).declare_lane(
            LaneLifecycleKey(_WORKSPACE_ID, "lane_r2"),
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=_pins(),
            worktree_identity="",
        )
        self.assertFalse(second.applied)
        self.assertEqual(second.reason, "owner_conflict")

        conn = sqlite3.connect(str(self.path))
        try:
            row = conn.execute(
                f"SELECT * FROM {TABLE} WHERE lane_id = ?", ("lane_r1",)
            ).fetchone()
            cols = [d[0] for d in conn.execute(f"SELECT * FROM {TABLE} LIMIT 0").description]
            values = dict(zip(cols, row))
            values["lane_id"] = "lane_r2"
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT INTO {TABLE} ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})",
                    [values[c] for c in cols],
                )
        finally:
            conn.close()

    def test_dropping_the_owner_index_makes_the_whole_authority_unreadable(self):
        """Layer 3: forcing the state past the index does not open a path either."""
        import sqlite3

        from mozyo_bridge.core.state.lane_lifecycle_schema import LaneLifecycleError

        self._seed("lane_r1")
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute("DROP INDEX IF EXISTS idx_lane_lifecycle_active_owner")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleStore(path=self.path).records()
        # And the rail reports that as unreadable rather than proceeding.
        verdict = self._terminalize("lane_r1")
        self.assertEqual(verdict.reason, "lifecycle_unreadable")

    def test_ambiguous_owner_resolution_refuses_the_retire(self):
        """The fence at its own boundary: a non-unique owner licenses no terminal write."""
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore as _Store
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            OWNER_AMBIGUOUS,
            OwnerResolution,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
            UNBOUND_RETIRE_LANE_SELECTION_UNPROVEN,
        )

        self._seed("lane_r1")
        with mock.patch.object(
            _Store,
            "resolve_owner",
            return_value=OwnerResolution(
                status=OWNER_AMBIGUOUS, detail="2 active owners; the owner index is not holding"
            ),
        ):
            verdict = self._terminalize("lane_r1")
        self.assertEqual(verdict.reason, UNBOUND_RETIRE_LANE_SELECTION_UNPROVEN)
        self.assertIn("ambiguous", verdict.detail)
        self.assertEqual(
            LaneLifecycleStore(path=self.path)
            .get(LaneLifecycleKey(_WORKSPACE_ID, "lane_r1"))
            .lane_disposition,
            DISPOSITION_ACTIVE,
            "zero-write: the lane must be untouched",
        )

    def test_owner_resolving_to_a_different_lane_refuses(self):
        """If the resolved owner is some other lane, this one is not the caller's target."""
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore as _Store
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            OWNER_RESOLVED,
            OwnerResolution,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
            UNBOUND_RETIRE_LANE_SELECTION_UNPROVEN,
        )

        self._seed("lane_r1")
        with mock.patch.object(
            _Store,
            "resolve_owner",
            return_value=OwnerResolution(status=OWNER_RESOLVED, lane_id="lane_somewhere_else"),
        ):
            verdict = self._terminalize("lane_r1")
        self.assertEqual(verdict.reason, UNBOUND_RETIRE_LANE_SELECTION_UNPROVEN)
        self.assertIn("lane_somewhere_else", verdict.detail)

    def test_a_lane_that_is_not_the_issues_owner_refuses_without_any_patching(self):
        """The one reachable case the fence improves — real store, no mocks."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
            UNBOUND_RETIRE_LANE_SELECTION_UNPROVEN,
        )

        self._seed("lane_owner", issue=_ISSUE)
        self._seed("lane_other", issue="99999")
        verdict = self._terminalize("lane_other")
        self.assertEqual(verdict.reason, UNBOUND_RETIRE_LANE_SELECTION_UNPROVEN)
        self.assertIn("lane_owner", verdict.detail)
        self.assertEqual(
            LaneLifecycleStore(path=self.path)
            .get(LaneLifecycleKey(_WORKSPACE_ID, "lane_other"))
            .lane_disposition,
            DISPOSITION_ACTIVE,
        )


class Finding2BranchIsBoundToTheLane(unittest.TestCase):
    """j#89191 finding 2: --branch must be THIS lane's branch, not any integrated one."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def _record_lane_branch(self, branch: str) -> None:
        from mozyo_bridge.core.state.lane_metadata import record_lane_created

        record_lane_created(
            lane_workspace_token="wt_probe",
            repo_workspace_id=_WORKSPACE_ID,
            issue_id=_ISSUE,
            lane_label=_LANE,
            branch=branch,
            worktree_path="/private/tmp/gone",
            lane_id=_LANE,
        )

    def _verify(self, branch, integration="main"):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
            _verify_branch_binds_to_lane,
        )

        return _verify_branch_binds_to_lane(
            argparse.Namespace(branch=branch, integration_branch=integration),
            workspace_id=_WORKSPACE_ID,
            lane_label=_LANE,
        )

    def test_the_ancestry_probe_really_does_pass_for_a_self_ancestor(self):
        """Non-vacuity: the fail-open this fence closes is real, measured over real git."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            LiveSublaneLifecycleOps,
        )

        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        _git("init", "-q", "-b", "main", cwd=repo)
        _git("config", "user.email", "t@example.com", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        _git("add", ".", cwd=repo)
        _git("commit", "-qm", "base", cwd=repo)
        _git("branch", "integration", cwd=repo)
        _git("checkout", "-q", "-b", "lane_branch", cwd=repo)
        (repo / "g.txt").write_text("y\n", encoding="utf-8")
        _git("add", ".", cwd=repo)
        _git("commit", "-qm", "unintegrated lane work", cwd=repo)
        ops = LiveSublaneLifecycleOps(repo_root=repo)
        self.assertFalse(
            ops.branch_integrated("lane_branch", "integration"),
            "the lane's real head is unintegrated",
        )
        self.assertTrue(
            ops.branch_integrated("integration", "integration"),
            "a branch is trivially its own ancestor — the fail-open this fence closes",
        )

    def test_an_unrelated_integrated_branch_is_refused(self):
        self._record_lane_branch("lane_branch")
        ok, detail = self._verify("some_other_integrated_branch")
        self.assertFalse(ok)
        self.assertIn("lane_branch", detail)

    def test_the_integration_branch_itself_is_refused(self):
        self._record_lane_branch("integration")
        ok, detail = self._verify("integration", integration="integration")
        self.assertFalse(ok)
        self.assertIn("its own ancestor", detail)

    def test_the_lanes_own_branch_passes(self):
        self._record_lane_branch("lane_branch")
        ok, detail = self._verify("lane_branch")
        self.assertTrue(ok, detail)

    def test_absent_metadata_refuses_rather_than_trusting_the_caller(self):
        ok, detail = self._verify("anything")
        self.assertFalse(ok)
        self.assertIn("no lane metadata record", detail)

    def test_a_record_without_a_branch_refuses(self):
        self._record_lane_branch("")
        ok, detail = self._verify("anything")
        self.assertFalse(ok)
        self.assertIn("carries no branch", detail)

    def test_an_empty_branch_argument_refuses(self):
        self._record_lane_branch("lane_branch")
        ok, _ = self._verify("")
        self.assertFalse(ok)


class Finding3PreCloseIdentityRecheck(unittest.TestCase):
    """j#89191 finding 3: re-verify name+locator immediately before each close."""

    def _execute(self, plan, recheck_rows=None, *, recheck_raises=False):
        """Drive ``_execute_closes`` with a controlled pre-close inventory re-read.

        ``recheck_rows`` is what every re-read returns (the state the world moved to between
        the plan and the close); ``recheck_raises`` makes the re-read fail instead.
        ``attempted`` records every locator an actual close was issued for — the assertion
        that matters for "zero foreign close" is that it stays empty.
        """
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as projection,
            herdr_destructive_close_identity as close_identity,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_pane_lifecycle,
            herdr_session_start,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_residue_close import (  # noqa: E501
            _execute_closes,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )

        attempted: list[str] = []

        def _reread(*_a, **_k):
            if recheck_raises:
                raise HerdrSessionStartError("inventory unreadable")
            return list(recheck_rows or [])

        def _close(_binary, locator, *_a, **_k):
            attempted.append(locator)
            return True, ""

        pin = ReleasePin(
            "codex", encode_assigned_name(_WORKSPACE_ID, "codex", _LANE),
            "w3N:pF", "startup-current",
        )
        with tempfile.TemporaryDirectory() as home, mock.patch.object(
            projection, "list_herdr_agent_rows", side_effect=_reread
        ), mock.patch.object(
            herdr_session_start, "_resolve_binary_or_die", return_value="herdr"
        ), mock.patch.object(
            herdr_pane_lifecycle, "_close_base_pane", side_effect=_close
        ), mock.patch.object(
            close_identity, "current_generation_release_pin", return_value=pin
        ), mock.patch.object(
            close_identity, "pinned_generations_absent", return_value=True
        ):
            closed, failed, skipped = _execute_closes(
                plan,
                workspace_id=_WORKSPACE_ID,
                lane_id=_LANE,
                legacy_workspace_id="",
                managed_roles=_ROLES,
                home=Path(home),
            )
        return closed, failed, skipped, attempted

    def _plan(self, rows):
        return plan_residue_close(
            rows, workspace_id=_WORKSPACE_ID, lane_id=_LANE, managed_roles=_ROLES
        )

    def test_unchanged_inventory_closes_normally(self):
        rows = [_residue_row("codex", "w3N:pF")]
        plan = self._plan(rows)
        closed, failed, skipped, attempted = self._execute(plan, rows)
        self.assertEqual(len(closed), 1)
        self.assertEqual(skipped, ())
        self.assertEqual(attempted, ["w3N:pF"])

    def test_same_name_locator_new_terminal_is_zero_close(self):
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore, IdentityAttestationRecord,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as projection,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_residue_close import (  # noqa: E501
            _execute_closes,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_pane_lifecycle, herdr_session_start,
        )
        from tests.support.current_launch_authority import seed_completed_current_generation

        initial = _residue_row("codex", "w3N:pF") | {"terminal_id": "terminal-old"}
        moved = initial | {"terminal_id": "terminal-new"}
        plan = self._plan([initial])
        attempted = []
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            name = initial["name"]
            seed_completed_current_generation(
                home, workspace_id=_WORKSPACE_ID, lane_id=_LANE, role="codex",
                assigned_name=name, locator="w3N:pF", terminal_id="terminal-old",
            )
            HerdrIdentityAttestationStore(home=home).upsert(IdentityAttestationRecord(
                name, _WORKSPACE_ID, "codex", _LANE, "w3N:pF", "present",
                observed_at="2026-08-11T00:00:00+00:00", terminal_id="terminal-old",
            ))
            with mock.patch.object(
                projection, "list_herdr_agent_rows", return_value=[moved]
            ), mock.patch.object(
                herdr_session_start, "_resolve_binary_or_die", return_value="herdr"
            ), mock.patch.object(
                herdr_pane_lifecycle, "_close_base_pane",
                side_effect=lambda _b, locator, *_a, **_k: (attempted.append(locator), ""),
            ):
                closed, failed, skipped = _execute_closes(
                    plan, workspace_id=_WORKSPACE_ID, lane_id=_LANE,
                    legacy_workspace_id="", managed_roles=_ROLES, home=home,
                )
        self.assertEqual((closed, failed), ((), ()))
        self.assertTrue(skipped)
        self.assertFalse(attempted)

    def test_a_locator_reassigned_to_a_foreign_occupant_is_not_closed(self):
        """The finding's scenario: same pane id, different (foreign) occupant."""
        rows = [_residue_row("codex", "w3N:pF")]
        plan = self._plan(rows)
        # Between the plan and the close the residue shell exits and herdr hands w3N:pF to a
        # completely different agent.
        after = [{"name": "mzb1_otherws_claude_someone_elses_lane", "pane": "w3N:pF",
                  "agent": "claude", "agent_status": "working"}]
        closed, failed, skipped, attempted = self._execute(plan, after)
        self.assertEqual(closed, (), "a reassigned locator must not be closed")
        self.assertEqual(attempted, [], "zero foreign close: nothing was even attempted")
        self.assertEqual(len(skipped), 1)
        self.assertIn("no longer a residue target", skipped[0][2])

    def test_a_slot_that_came_back_to_life_is_not_closed(self):
        rows = [_residue_row("codex", "w3N:pF")]
        plan = self._plan(rows)
        revived = [_live_row("codex", "w3N:pF")]
        closed, _failed, skipped, attempted = self._execute(plan, revived)
        self.assertEqual(closed, ())
        self.assertEqual(attempted, [])
        self.assertEqual(len(skipped), 1)

    def test_a_live_half_appearing_late_collapses_the_whole_close(self):
        """The pair fence is re-applied at close time, not only at plan time."""
        rows = [_residue_row("codex", "w3N:pF"), _residue_row("claude", "w3N:pG")]
        plan = self._plan(rows)
        self.assertEqual(len(plan.close_targets), 2)
        late = [_residue_row("codex", "w3N:pF"), _live_row("claude", "w3N:pG")]
        closed, _failed, skipped, attempted = self._execute(plan, late)
        self.assertEqual(closed, ())
        self.assertEqual(attempted, [])
        self.assertEqual(len(skipped), 2)
        self.assertIn("live agent appeared", skipped[0][2])

    def test_an_unreadable_recheck_skips_rather_than_closing_blind(self):
        rows = [_residue_row("codex", "w3N:pF")]
        plan = self._plan(rows)
        closed, _failed, skipped, attempted = self._execute(plan, recheck_raises=True)
        self.assertEqual(closed, ())
        self.assertEqual(attempted, [])
        self.assertIn("could not be re-read", skipped[0][2])

    def test_all_targets_skipped_is_blocked_not_a_success(self):
        """An all-skipped run must not read as the verified `no_residue` state.

        Nothing was closed and nothing failed, but the lane still holds slots that looked
        like residue moments ago — reporting success there is the unproven-no-op misread the
        whole surface exists to avoid.
        """
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_residue_close as module,
        )

        rows = [_residue_row("codex", "w3N:pF")]
        plan = self._plan(rows)
        moved = [{"name": "mzb1_otherws_claude_elsewhere", "pane": "w3N:pF",
                  "agent": "claude", "agent_status": "working"}]
        closed, failed, skipped, attempted = self._execute(plan, moved)
        self.assertEqual((closed, failed, attempted), ((), (), []))
        self.assertEqual(len(skipped), 1)
        # And the verdict that shape produces is blocked, not a success.
        verdict = module.ResidueCloseVerdict(
            state=module.RESIDUE_BLOCKED,
            reason=module.RESIDUE_IDENTITY_MOVED,
            skipped=tuple(skipped),
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(len(verdict.as_payload()["skipped"]), 1)


class CurrentGenerationResidueCallerMatrix(unittest.TestCase):
    """#15227: the public preflight and execute share the current-generation gate."""

    def _run(self, authority, *, execute, failure_detail=""):
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore, IdentityAttestationRecord,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as projection,
            workflow_provider_resolution as providers,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_residue_close import (  # noqa: E501
            run_residue_close,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_pane_lifecycle,
        )
        from tests.support.current_launch_authority import seed_completed_current_generation

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        name = encode_assigned_name(_WORKSPACE_ID, "codex", _LANE)
        terminal = "terminal-current" if authority != "mismatch" else "terminal-new"
        rows = [_residue_row("codex", "w3N:pF") | {"terminal_id": terminal}]
        if authority in ("current", "mismatch"):
            recorded_terminal = "terminal-current" if authority == "current" else "terminal-old"
            seed_completed_current_generation(
                home, workspace_id=_WORKSPACE_ID, lane_id=_LANE, role="codex",
                assigned_name=name, locator="w3N:pF", terminal_id=recorded_terminal,
            )
            HerdrIdentityAttestationStore(home=home).upsert(IdentityAttestationRecord(
                name, _WORKSPACE_ID, "codex", _LANE, "w3N:pF", "present",
                observed_at="2026-08-11T00:00:00+00:00",
                terminal_id=recorded_terminal,
            ))
        inventories = [rows] if not execute else [rows, rows, []]
        close_result = (not failure_detail, failure_detail)
        args = argparse.Namespace(
            lane_label=_LANE, issue="14499", worktree="",
        )
        with mock.patch.object(
            projection, "repo_backend_is_herdr", return_value=True
        ), mock.patch.object(
            projection, "repo_scope_workspace_id", return_value=_WORKSPACE_ID
        ), mock.patch.object(
            projection, "list_herdr_agent_rows", side_effect=inventories
        ), mock.patch.object(
            providers, "resolve_gateway_provider", return_value="codex"
        ), mock.patch.object(
            providers, "resolve_worker_provider", return_value="claude"
        ), mock.patch(
            "mozyo_bridge.shared.paths.mozyo_bridge_home", return_value=home
        ), mock.patch.object(
            LaneLifecycleStore, "get", return_value=mock.Mock(issue_id="14499")
        ), mock.patch(
            "mozyo_bridge.core.state.herdr_identity_attestation_schema."
            "attestation_store_lock", return_value=contextlib.nullcontext()
        ), mock.patch.object(
            herdr_pane_lifecycle, "_close_base_pane", return_value=close_result
        ):
            return run_residue_close(args, Path("/repo"), execute=execute)

    def test_preflight_and_execute_require_the_same_current_generation(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_residue_close as close,
        )

        self.assertEqual(self._run("current", execute=False).state, close.RESIDUE_PREFLIGHT)
        self.assertEqual(self._run("current", execute=True).state, close.RESIDUE_CLOSED)
        for authority in ("missing", "mismatch"):
            for execute in (False, True):
                with self.subTest(authority=authority, execute=execute):
                    verdict = self._run(authority, execute=execute)
                    self.assertEqual(verdict.reason, close.RESIDUE_GENERATION_UNVERIFIED)
                    self.assertEqual(verdict.closed, ())

    def test_provider_failure_cannot_render_a_private_terminal(self):
        secret = "terminal-secret-T"
        verdict = self._run("current", execute=True, failure_detail=secret)
        rendered = repr(verdict) + json.dumps(verdict.as_payload(), sort_keys=True)
        self.assertNotIn(secret, rendered)
        self.assertIn("provider close failed", rendered)


class Finding4UnreadableAuthorityIsNotEmpty(unittest.TestCase):
    """j#89191 finding 4: an unreadable authority must not read as 'nothing to converge'."""

    def test_an_unreadable_lifecycle_store_raises_rather_than_reporting_empty(self):
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_reboot_audit as audit,
        )

        with mock.patch.object(
            audit, "gather_reboot_facts", wraps=audit.gather_reboot_facts
        ):
            with mock.patch(
                "mozyo_bridge.core.state.lane_lifecycle_readonly.load_lane_lifecycle_readonly",
                return_value=None,
            ), mock.patch.object(
                audit, "_DEFAULT_INTEGRATION_BRANCH", "main"
            ):
                with mock.patch(
                    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
                    "application.sublane_herdr_projection.repo_scope_workspace_id",
                    return_value=_WORKSPACE_ID,
                ):
                    with self.assertRaises(audit.RebootAuditUnavailable) as caught:
                        audit.gather_reboot_facts(Path("."))
        self.assertIn("could not be read", str(caught.exception))
        self.assertIn("NOT the same as the store having no rows", str(caught.exception))

    def test_an_unresolvable_workspace_raises_rather_than_reporting_empty(self):
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_reboot_audit as audit,
        )

        with mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_herdr_projection.repo_scope_workspace_id",
            return_value="",
        ):
            with self.assertRaises(audit.RebootAuditUnavailable) as caught:
                audit.gather_reboot_facts(Path("."))
        self.assertIn("workspace identity", str(caught.exception))

    def test_the_command_exits_non_zero_when_the_snapshot_is_unavailable(self):
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_reboot_audit as audit,
        )

        with mock.patch.object(
            audit,
            "gather_reboot_facts",
            side_effect=audit.RebootAuditUnavailable("store unreadable"),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = audit.cmd_sublane_reboot_audit(
                    argparse.Namespace(repo=".", integration_branch="", lane_label="", json=True)
                )
        self.assertEqual(rc, 1, "an unreadable authority must not exit 0")
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["state"], "unavailable")
        self.assertEqual(payload["lane_count"], 0)

    def test_a_genuinely_empty_workspace_still_reports_zero_lanes_at_exit_0(self):
        """The distinction: empty is a normal result, unreadable is not."""
        from unittest import mock

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_reboot_audit as audit,
        )

        with mock.patch.object(audit, "gather_reboot_facts", return_value=()):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = audit.cmd_sublane_reboot_audit(
                    argparse.Namespace(repo=".", integration_branch="", lane_label="", json=True)
                )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["lane_count"], 0)


# ---------------------------------------------------------------------------
# 7. Review j#89238: the idempotent replay the R2 owner fence broke.
# ---------------------------------------------------------------------------


class Finding5IdempotentReplayThroughTheRail(unittest.TestCase):
    """j#89238: first apply -> replay must be an `already_retired` zero-write success.

    The R2 owner fence (j#89191 finding 1) ran before the disposition branch, and
    ``resolve_owner`` only considers ACTIVE rows — so a terminalized row dropped out of the
    owner index and its replay returned ``lane_selection_unproven`` instead. A fence guarding
    a write must not run on a path that performs none.

    Why R2's 74 pins missed it: the only replay test was
    ``test_a_second_apply_is_refused_the_caller_owns_idempotence``, which drives the **CAS
    store** directly. There, refusing a second apply IS the contract — idempotence is the
    caller's job. Nothing exercised the *rail*, which is the caller. These tests are
    deliberately application-level for that reason.

    The live-zero measurement is **not** mocked here: the inventory rows are injected and the
    real four-fence measurement runs over them. Mocking it would make the "a retired replay
    still blocks on a live pair" case vacuous, since that block comes from the measurement.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.path = self.home / "state.sqlite"
        self.key = LaneLifecycleKey(_WORKSPACE_ID, _LANE)

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as projection,
            workflow_provider_resolution as providers,
        )

        self.inventory: list[dict] = []
        rows = mock.patch.object(
            projection, "list_herdr_agent_rows",
            side_effect=lambda *_a, **_k: list(self.inventory),
        )
        rows.start()
        self.addCleanup(rows.stop)
        for attr, value in (
            ("resolve_gateway_provider", "codex"),
            ("resolve_worker_provider", "claude"),
        ):
            patcher = mock.patch.object(providers, attr, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        out = LaneDeclarationStore(path=self.path).declare_lane(
            self.key,
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=_pins(),
            worktree_identity="",
        )
        self.assertTrue(out.applied, out.reason)

    def _row(self):
        return LaneLifecycleStore(path=self.path).get(self.key)

    def _terminalize(self, *, lane_label: str = _LANE, generation=None, revision=None):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
            _terminalize_under_exclusion,
        )

        current = self._row()
        return _terminalize_under_exclusion(
            argparse.Namespace(
                lane_label=lane_label, issue=_ISSUE, journal=_JOURNAL, worktree=None,
                branch="b", integration_branch="main",
            ),
            Path("."),
            workspace_id=_WORKSPACE_ID,
            lane_label=lane_label,
            issue=_ISSUE,
            journal=_JOURNAL,
            expect_generation=(
                current.lane_generation if generation is None else generation
            ),
            expect_revision=current.revision if revision is None else revision,
        )

    # -- (a) the regression itself -------------------------------------------

    def test_first_apply_then_replay_is_an_idempotent_success(self):
        first = self._terminalize()
        self.assertEqual(first.state, "retired", first.reason)
        after_first = self._row()
        self.assertEqual(after_first.lane_disposition, DISPOSITION_RETIRED)

        replay = self._terminalize()
        self.assertEqual(
            replay.state,
            "already_retired",
            f"the replay must be an idempotent success, got {replay.reason}: {replay.detail}",
        )
        self.assertTrue(replay.ok)
        # And it is a ZERO-write success: the row did not move again.
        after_replay = self._row()
        self.assertEqual(after_replay.revision, after_first.revision)
        self.assertEqual(after_replay.lane_disposition, DISPOSITION_RETIRED)

    def test_a_third_replay_is_still_idempotent(self):
        self.assertEqual(self._terminalize().state, "retired")
        rev = self._row().revision
        for _ in range(2):
            self.assertEqual(self._terminalize().state, "already_retired")
        self.assertEqual(self._row().revision, rev, "replays must never write")

    # -- (b) a retired replay must still be measured, not rubber-stamped ------

    def test_a_retired_replay_with_a_live_pair_blocks_instead_of_succeeding(self):
        """A persisted `retired` is not proof the pair is currently gone."""
        self.assertEqual(self._terminalize().state, "retired")
        self.inventory = [_live_row("codex", "w3N:pF"), _live_row("claude", "w3N:pG")]
        replay = self._terminalize()
        self.assertEqual(replay.state, "blocked")
        self.assertEqual(replay.reason, "live_pair_present")
        self.assertFalse(replay.ok)

    def test_a_retired_replay_with_a_foreign_occupant_blocks(self):
        self.assertEqual(self._terminalize().state, "retired")
        self.inventory = [
            {
                "name": encode_assigned_name(_WORKSPACE_ID, "gemini", _LANE),
                "pane": "w3N:pZ",
                "agent": "gemini",
                "agent_status": "idle",
            }
        ]
        replay = self._terminalize()
        self.assertEqual(replay.state, "blocked")
        self.assertEqual(replay.reason, "foreign_inventory_present")

    def test_a_retired_replay_of_a_DIFFERENT_issue_is_not_a_success(self):
        """The idempotent branch requires the row to own THIS issue."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
            _terminalize_under_exclusion,
        )

        self.assertEqual(self._terminalize().state, "retired")
        current = self._row()
        verdict = _terminalize_under_exclusion(
            argparse.Namespace(
                lane_label=_LANE, issue="99999", journal=_JOURNAL, worktree=None,
                branch="b", integration_branch="main",
            ),
            Path("."),
            workspace_id=_WORKSPACE_ID,
            lane_label=_LANE,
            issue="99999",
            journal=_JOURNAL,
            expect_generation=current.lane_generation,
            expect_revision=current.revision,
        )
        self.assertEqual(verdict.state, "blocked")
        self.assertNotEqual(verdict.reason, "already_retired")

    # -- (c) the ACTIVE-path fence is untouched ------------------------------

    def test_the_owner_fence_still_guards_the_active_write_path(self):
        """Scoping the fence to `active` must not disarm it where it matters."""
        from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore as _Store
        from mozyo_bridge.core.state.lane_lifecycle_model import (
            OWNER_RESOLVED,
            OwnerResolution,
        )

        with mock.patch.object(
            _Store,
            "resolve_owner",
            return_value=OwnerResolution(status=OWNER_RESOLVED, lane_id="a_different_lane"),
        ):
            verdict = self._terminalize()
        self.assertEqual(verdict.reason, "lane_selection_unproven")
        self.assertEqual(
            self._row().lane_disposition,
            DISPOSITION_ACTIVE,
            "zero-write: the ACTIVE row must be untouched",
        )

    def test_a_hibernated_row_now_reports_its_disposition_not_lane_selection(self):
        """Side effect of scoping the fence: the refusal names the real problem again."""
        row = self._row()
        LaneLifecycleStore(path=self.path).transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=row.revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        verdict = self._terminalize()
        self.assertEqual(verdict.state, "blocked")
        self.assertEqual(
            verdict.reason,
            "not_active_unbound_state",
            "a hibernated row's problem is its disposition, not lane selection",
        )


# ---------------------------------------------------------------------------
# 8. j#89291: the metadata-only rail must not consume a caller worktree at all.
# ---------------------------------------------------------------------------


class MetadataOnlyRailIsDecoupledFromCallerWorktree(unittest.TestCase):
    """j#89291 live-acceptance blocker, measured on #14482 and reproduced here.

    ``--retire-active-unbound-live-zero`` states in its own CLI help that ``--worktree`` is
    optional and used only to widen the legacy live-zero inventory scan. The generic retire
    preflight and runbook did not honour that, and live acceptance found all three ways it
    went wrong on a lane that was closed, ACTIVE+UNBOUND, integrated and live-zero:

    1. ``--worktree`` omitted -> the preflight fell back to probing the PRIMARY repo root and
       blocked on unrelated user-owned dirtiness (``dirty_worktree``);
    2. the recorded (reboot-wiped) worktree supplied -> ``worktree_missing_after_reboot``;
    3. an unrelated clean worktree supplied -> ``retire_ok``, but the runbook then proposed
       ``git worktree remove`` against *that* worktree and ``git branch -d`` against the lane
       branch.

    None of the three says anything about the lane being terminalized. The correction is that
    a metadata-only intent has no checkout in scope, so the checkout gates are not measured
    and the cleanup runbook is not emitted. All three inputs must now reach the SAME
    metadata-only decision, subject only to the rail's own identity and live-zero fences.

    The bound intents keep every one of those behaviours — pinned below, because the whole
    risk of this change is that it leaks into them.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "primary"
        self.repo.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.repo)
        _git("config", "user.email", "t@example.com", cwd=self.repo)
        _git("config", "user.name", "t", cwd=self.repo)
        (self.repo / "f.txt").write_text("x\n", encoding="utf-8")
        _git("add", ".", cwd=self.repo)
        _git("commit", "-qm", "base", cwd=self.repo)
        self.branch = "issue_14482_0_14_0a4_release_r2"
        _git("branch", self.branch, cwd=self.repo)

        # Unrelated, user-owned dirtiness in the primary checkout: tracked and then modified,
        # exactly the `.claude/settings.local.json` shape j#89291 hit.
        (self.repo / ".claude").mkdir()
        user_file = self.repo / ".claude" / "settings.local.json"
        user_file.write_text("{}\n", encoding="utf-8")
        _git("add", "-f", ".claude/settings.local.json", cwd=self.repo)
        _git("commit", "-qm", "user settings", cwd=self.repo)
        user_file.write_text('{"user": "edited"}\n', encoding="utf-8")

        # An unrelated CLEAN worktree (the case-3 input that must not become a cleanup target).
        self.unrelated = self.root / "unrelated_integration_wt"
        _git("worktree", "add", "-q", "--detach", str(self.unrelated), "main", cwd=self.repo)
        # The lane's recorded worktree, wiped by the reboot.
        self.gone = self.root / "private_tmp_wiped_by_reboot"

    def _args(self, worktree, *, unbound: bool) -> argparse.Namespace:
        return argparse.Namespace(
            issue="14482",
            journal="89072",
            lane_label=self.branch,
            worktree=(str(worktree) if worktree else None),
            branch=self.branch,
            integration_branch="main",
            issue_closed=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            latest_generation_admissible=True,
            review_generation_json=None,
            execute=False,
            migrate_hibernated_legacy=False,
            reconcile_hibernated_live=False,
            retire_hibernated_bound=False,
            retire_active_live_zero=not unbound,
            retire_active_unbound_live_zero=unbound,
            expect_lane_generation=1,
            expect_lane_revision=1,
            integration_journal=None,
            repo=str(self.repo),
            json=True,
        )

    def _run(self, worktree, *, unbound: bool) -> dict:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E501
            cmd_sublane_retire,
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_sublane_retire(self._args(worktree, unbound=unbound))
        return json.loads(buf.getvalue())

    def _cleanup_commands(self, payload: dict) -> list:
        return [
            step["command"]
            for step in payload.get("runbook", [])
            if step.get("command")
            and ("worktree remove" in step["command"] or "branch -d" in step["command"])
        ]

    # -- the three required regression pins -----------------------------------

    def test_all_three_worktree_inputs_reach_the_same_metadata_only_decision(self):
        results = {
            "omitted (dirty primary repo)": self._run(None, unbound=True),
            "recorded missing worktree": self._run(self.gone, unbound=True),
            "unrelated clean worktree": self._run(self.unrelated, unbound=True),
        }
        for label, payload in results.items():
            decision = payload["decision"]
            self.assertEqual(
                decision["blocked_reasons"], [], f"{label}: unexpected checkout gate"
            )
            self.assertEqual(decision["state"], "retire_ok", label)
            self.assertEqual(
                self._cleanup_commands(payload),
                [],
                f"{label}: a metadata-only intent proposed checkout cleanup",
            )
        # And they are the same decision, not merely three passing ones.
        decisions = {json.dumps(p["decision"], sort_keys=True) for p in results.values()}
        self.assertEqual(len(decisions), 1, "the three inputs diverged")

    def test_omitted_worktree_does_not_consume_unrelated_primary_dirtiness(self):
        """Case 1: the primary checkout's user-owned changes are none of this rail's business."""
        self.assertTrue(
            _git_out("status", "--porcelain", cwd=self.repo).strip(),
            "precondition: the primary checkout must be dirty",
        )
        payload = self._run(None, unbound=True)
        self.assertNotIn("dirty_worktree", payload["decision"]["blocked_reasons"])
        self.assertTrue(payload["retire_ok"])

    def test_the_recorded_missing_worktree_does_not_block_the_metadata_only_rail(self):
        """Case 2: the reboot-wiped path is exactly what this surface exists to converge."""
        self.assertFalse(self.gone.exists())
        payload = self._run(self.gone, unbound=True)
        self.assertNotIn(
            "worktree_missing_after_reboot", payload["decision"]["blocked_reasons"]
        )

    def test_an_unrelated_worktree_is_never_proposed_for_removal(self):
        """Case 3: the sharpest one — the runbook named somebody else's checkout."""
        payload = self._run(self.unrelated, unbound=True)
        joined = json.dumps(payload)
        self.assertNotIn("worktree remove", joined)
        self.assertNotIn("branch -d", joined)
        self.assertTrue(
            self.unrelated.is_dir(), "the unrelated worktree must still exist (dry run)"
        )

    def test_the_runbook_says_what_the_intent_actually_does(self):
        payload = self._run(None, unbound=True)
        titles = [s["title"] for s in payload["runbook"]]
        self.assertEqual(titles, ["metadata-only terminalization"])

    # -- bound-lane behaviour must be untouched --------------------------------

    def test_bound_intent_still_blocks_on_a_dirty_repo_when_worktree_is_omitted(self):
        payload = self._run(None, unbound=False)
        self.assertIn("dirty_worktree", payload["decision"]["blocked_reasons"])

    def test_bound_intent_still_blocks_on_a_missing_recorded_worktree(self):
        payload = self._run(self.gone, unbound=False)
        self.assertIn(
            "worktree_missing_after_reboot", payload["decision"]["blocked_reasons"]
        )

    def test_bound_intent_still_emits_the_full_cleanup_runbook(self):
        payload = self._run(self.unrelated, unbound=False)
        commands = self._cleanup_commands(payload)
        self.assertTrue(
            any("worktree remove" in c for c in commands), commands
        )
        self.assertTrue(any("branch -d" in c for c in commands), commands)

    def test_the_default_preflight_runbook_is_unchanged_for_every_other_caller(self):
        """The domain default stays `True`, so no existing caller's runbook moves."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            preflight_sublane_retire,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E501
            RetireDecision,
            RETIRE_OK,
        )

        decision = RetireDecision(state=RETIRE_OK)
        default = preflight_sublane_retire(
            decision, issue="1", lane_label="l", worktree_path="/wt", branch="b"
        )
        explicit = preflight_sublane_retire(
            decision, issue="1", lane_label="l", worktree_path="/wt", branch="b",
            checkout_cleanup_in_scope=True,
        )
        self.assertEqual(default.runbook, explicit.runbook)
        self.assertTrue(any("worktree remove" in (s.command or "") for s in default.runbook))


class MetadataOnlyExecuteTouchesLifecycleOnly(unittest.TestCase):
    """j#89291: an executed metadata-only terminalize must have lifecycle-CAS-only effects."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.path = self.home / "state.sqlite"

        self.repo = self.root / "primary"
        self.repo.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.repo)
        _git("config", "user.email", "t@example.com", cwd=self.repo)
        _git("config", "user.name", "t", cwd=self.repo)
        (self.repo / "f.txt").write_text("x\n", encoding="utf-8")
        _git("add", ".", cwd=self.repo)
        _git("commit", "-qm", "base", cwd=self.repo)
        _git("branch", _LANE, cwd=self.repo)
        self.head = _git_out("rev-parse", _LANE, cwd=self.repo).strip()
        self.sibling = self.root / "sibling_wt"
        _git("worktree", "add", "-q", "--detach", str(self.sibling), "main", cwd=self.repo)

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_herdr_projection as projection,
            sublane_herdr_retire as herdr_retire,
            workflow_provider_resolution as providers,
        )

        rows = mock.patch.object(
            projection, "list_herdr_agent_rows", side_effect=lambda *_a, **_k: []
        )
        rows.start()
        self.addCleanup(rows.stop)
        for attr, value in (
            ("resolve_gateway_provider", "codex"),
            ("resolve_worker_provider", "claude"),
        ):
            patcher = mock.patch.object(providers, attr, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Any pane close at all is a contract violation for this surface.
        close_patch = mock.patch.object(
            herdr_retire,
            "execute_herdr_retire_close",
            side_effect=AssertionError("a metadata-only retire must close nothing"),
        )
        close_patch.start()
        self.addCleanup(close_patch.stop)

        out = LaneDeclarationStore(path=self.path).declare_lane(
            LaneLifecycleKey(_WORKSPACE_ID, _LANE),
            decision=_decision(),
            issue_id=_ISSUE,
            declared_slots=_pins(),
            worktree_identity="",
        )
        self.assertTrue(out.applied, out.reason)

    def test_execute_writes_the_lifecycle_row_and_nothing_else(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_active_unbound_live_zero_retire import (  # noqa: E501
            _terminalize_under_exclusion,
        )

        key = LaneLifecycleKey(_WORKSPACE_ID, _LANE)
        before = LaneLifecycleStore(path=self.path).get(key)
        verdict = _terminalize_under_exclusion(
            argparse.Namespace(
                lane_label=_LANE, issue=_ISSUE, journal=_JOURNAL,
                worktree=str(self.sibling), branch=_LANE, integration_branch="main",
            ),
            self.repo,
            workspace_id=_WORKSPACE_ID,
            lane_label=_LANE,
            issue=_ISSUE,
            journal=_JOURNAL,
            expect_generation=before.lane_generation,
            expect_revision=before.revision,
        )
        self.assertEqual(verdict.state, "retired", f"{verdict.reason}: {verdict.detail}")

        # 1. the lifecycle row moved — and ONLY its disposition / anchor / revision.
        after = LaneLifecycleStore(path=self.path).get(key)
        self.assertEqual(after.lane_disposition, DISPOSITION_RETIRED)
        self.assertEqual(after.lane_generation, before.lane_generation)
        self.assertEqual(after.declared_slots, before.declared_slots)
        self.assertEqual(after.worktree_identity, "")
        self.assertEqual(after.process_release, before.process_release)

        # 2. no worktree was removed — including the unrelated one passed as --worktree.
        self.assertTrue(self.sibling.is_dir())
        self.assertIn(str(self.sibling), _git_out("worktree", "list", cwd=self.repo))

        # 3. the branch and its commit survive, unmoved.
        self.assertEqual(_git_out("rev-parse", _LANE, cwd=self.repo).strip(), self.head)

        # 4. no pane close was attempted (the patched actuator would have raised).


def _git_out(*args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout
