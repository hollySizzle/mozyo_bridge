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
