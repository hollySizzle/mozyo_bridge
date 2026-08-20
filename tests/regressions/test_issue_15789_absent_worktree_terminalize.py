"""Regression pins for the #15789 planner / retire-executor contract gap.

Redmine #15789 (parent #15631), measured on #15151 j#108983 and reproduced on #15789 j#109076.
``sublane reboot-audit`` describes a closed / live-zero / BOUND lane whose recorded worktree the
reboot wiped as having two safe rails — ``restore_worktree``, or ``terminalize_bound_metadata``
"when the checkout is not wanted back" — but the second one had no executable path: every bound
terminal retire kept the checkout in preflight scope and refused that exact shape with
``integration_blocked`` / ``worktree_missing_after_reboot``. The lane drain therefore escalated
to L1 every time the audit prescribed the alternative.

The fix is the opt-in ``--worktree-absent`` on the two BOUND metadata-only terminal retires.
It substitutes exactly the two facts a wiped checkout takes away, both re-read from git's own
SURVIVING worktree administrative entry rather than from a display cache: the worktree ↔ branch
tie, and the ``wt_`` binding-token family that :func:`is_git_worktree_root` (a live disk probe)
can no longer determine. Nothing else moves.

What is pinned here, in one file per the R3-c same-issue grouping rule:

1. **the gap** — the audit's alternative now names a runnable command, and that command
   terminalizes the wiped-checkout lane end to end (a real temp repo, a real wiped worktree, a
   real lifecycle CAS);
2. **the invariants the fix must not touch** — an unintegrated head is still refused, no branch
   / commit / worktree entry is ever deleted, a live slot and a foreign occupant still block,
   and the binding must still attest byte-for-byte (a ``dl_`` row cannot be reached by asserting
   the ``wt_`` family);
3. **the new evidence path's own refusals** — a checkout that still EXISTS, an already-pruned
   entry, a locked / non-prunable entry, a detached or differently-branched entry, an unreadable
   ``git worktree list``, and a non-applicable intent are each zero-write;
4. **the default is unchanged** — without the flag the bound rails block on a missing worktree
   exactly as #14499 pinned them to;
5. **the absence is re-proven at the terminal mutation boundary** — review j#109127
   ``finding_actiontimerace`` (verdict j#109134, accepted after independent reproduction): a
   checkout restored and dirtied at the recorded path AFTER the preflight's proof must not reach
   a terminal write.

Boundary: synthetic lifecycle sqlite under a task-specific temp home, fabricated inventory rows,
real temporary git repos where a git fact is under test. Never the operator's shared
``$HOME/.mozyo_bridge``, never a live pane / process, never a branch or commit deletion, never
``git worktree prune``.
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
from unittest import mock

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle_model import (  # noqa: E402
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DISPOSITION_RETIRED,
    DecisionPointer,
    LaneLifecycleKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402
    sublane_herdr_projection as projection,
    sublane_herdr_retire as herdr_retire,
    sublane_lifecycle_command,
    workflow_provider_resolution as providers,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_absent_worktree_evidence import (  # noqa: E402
    ABSENT_WT_BRANCH_MISMATCH,
    ABSENT_WT_DRIFT,
    ABSENT_WT_LIST_UNREADABLE,
    ABSENT_WT_NOT_PRUNABLE,
    ABSENT_WT_NOT_REGISTERED,
    ABSENT_WT_WORKTREE_PRESENT,
    parse_worktree_list_porcelain,
    resolve_absent_worktree_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E402
    declared_lane_root_identity,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_application import (  # noqa: E402
    REASON_WORKTREE_ABSENT_NOT_APPLICABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reboot_residue_convergence import (  # noqa: E402
    CONVERGE_RESTORE_WORKTREE,
    CONVERGE_TERMINALIZE_BOUND,
    RebootLaneFacts,
    plan_lane_convergence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402
    encode_assigned_name,
)

_WORKSPACE_ID = "wProj15789"
_LANE = "issue_15789_terminalize_alt_rail"
_ISSUE = "15789"
_JOURNAL = "109078"


def _git(*args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _git_out(*args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def _anchor(root: Path) -> None:
    """The real workspace anchor + herdr backend the identity resolvers read.

    Written for real rather than patched: the axis under test is which root the wiped-checkout
    rail asks for the lane's inherited workspace identity, and stubbing the resolver would
    answer that question for it.
    """
    (root / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (root / ".mozyo-bridge" / "config.yaml").write_text(
        "terminal_transport:\n  backend: herdr\n", encoding="utf-8"
    )
    (root / ".mozyo-bridge" / "workspace-anchor.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace_id": _WORKSPACE_ID,
                "canonical_session": "mzb-test",
                "project_name": "mozyo_bridge",
                "created_at": "2026-08-20T00:00:00+00:00",
                "updated_at": "2026-08-20T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


class _WipedCheckoutFixture(unittest.TestCase):
    """A real main checkout whose real linked lane worktree is then really deleted.

    Deleted rather than never created: the whole shape under test is git's administrative entry
    SURVIVING a checkout that once existed, and a path that was never added has no entry at all
    (which this fixture also exercises, as the pruned case).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()

        self.home = root / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

        self.primary = root / "primary"
        self.primary.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.primary)
        _git("config", "user.email", "t@example.invalid", cwd=self.primary)
        _git("config", "user.name", "t", cwd=self.primary)
        _anchor(self.primary)
        (self.primary / "f.txt").write_text("x\n", encoding="utf-8")
        _git("add", "-A", cwd=self.primary)
        _git("commit", "-qm", "base", cwd=self.primary)
        self.primary = self.primary.resolve()

        # No extra commit on the lane branch: its head is a literal ancestor of main, so the
        # head-integration probe is green and cannot mask the axis under test.
        self.lane_wt = (root / "lane_worktree").resolve()
        _git(
            "worktree", "add", "-q", "-b", _LANE, str(self.lane_wt), "main",
            cwd=self.primary,
        )
        self.lane_head = _git_out("rev-parse", _LANE, cwd=self.primary).strip()

        # The binding is recorded exactly as `sublane create` records it — WHILE the checkout
        # exists, through the same canonical helper — and only then is the checkout wiped.
        self.recorded_binding = declared_lane_root_identity(
            self.lane_wt, _LANE
        ).metadata_token
        self.assertTrue(
            self.recorded_binding.startswith("wt_"),
            f"precondition: a linked worktree records the wt_ family, got "
            f"{self.recorded_binding!r}",
        )

        self.rows: list[dict] = []
        rows_patch = mock.patch.object(
            projection,
            "list_herdr_agent_rows",
            side_effect=lambda *_a, **_k: list(self.rows),
        )
        rows_patch.start()
        self.addCleanup(rows_patch.stop)
        for attr, value in (
            ("resolve_gateway_provider", "codex"),
            ("resolve_worker_provider", "claude"),
        ):
            patcher = mock.patch.object(providers, attr, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Any pane close at all is a contract violation for a metadata-only rail.
        close_patch = mock.patch.object(
            herdr_retire,
            "execute_herdr_retire_close",
            side_effect=AssertionError("a metadata-only retire must close nothing"),
        )
        close_patch.start()
        self.addCleanup(close_patch.stop)

    # -- helpers ------------------------------------------------------------------

    def wipe_checkout(self) -> None:
        subprocess.run(["rm", "-rf", str(self.lane_wt)], check=True)
        self.assertFalse(self.lane_wt.exists())

    def declare_active(self, *, binding: str = "") -> None:
        LaneLifecycleStore().declare_active(
            LaneLifecycleKey(_WORKSPACE_ID, _LANE),
            decision=DecisionPointer(
                source="redmine", issue_id=_ISSUE, journal_id=_JOURNAL
            ),
            issue_id=_ISSUE,
            worktree_identity=binding or self.recorded_binding,
        )

    def disposition(self) -> str:
        record = LaneLifecycleStore().get(LaneLifecycleKey(_WORKSPACE_ID, _LANE))
        return "" if record is None else record.lane_disposition

    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            repo=str(self.primary),
            home=self.home,
            issue=_ISSUE,
            journal=_JOURNAL,
            lane_label=_LANE,
            worktree=str(self.lane_wt),
            branch=_LANE,
            integration_branch="main",
            json=True,
            # Every durable-record invariant asserted, so the PREFLIGHT is green and any block
            # can only come from the axis under test.
            issue_closed=True,
            callbacks_drained=True,
            verified=True,
            durable_record=True,
            target_identity_known=True,
            latest_generation_admissible=True,
            review_generation_json=None,
            integration_journal=None,
            execute=False,
            migrate_hibernated_legacy=False,
            reconcile_hibernated_live=False,
            retire_hibernated_bound=False,
            retire_active_live_zero=True,
            retire_active_unbound_live_zero=False,
            retire_hibernated_unbound_live_zero=False,
            worktree_absent=True,
            expect_lane_generation=0,
            expect_lane_revision=0,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def run_retire(self, **overrides):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sublane_lifecycle_command.cmd_sublane_retire(self._args(**overrides))
        return code, json.loads(buffer.getvalue())

    def assert_refused(self, payload: dict, reason: str) -> None:
        self.assertEqual(
            payload.get("retire_application", {}).get("reason"),
            reason,
            json.dumps(payload, sort_keys=True)[:600],
        )
        self.assertFalse(payload.get("retire_ok"))
        self.assertFalse(payload.get("retire_application", {}).get("mutated"))


# ---------------------------------------------------------------------------
# 1. The gap: the prescribed alternative is now runnable, and it runs.
# ---------------------------------------------------------------------------


class TheAuditAlternativeNamesARunnableCommand(unittest.TestCase):
    """The planner half. Naming an alternative without an invocation is what left the
    coordinator to infer a command that did not exist (#15151 j#108983)."""

    def _plan(self, *, hibernated: bool = False):
        from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_HIBERNATED

        return plan_lane_convergence(
            RebootLaneFacts(
                workspace_id=_WORKSPACE_ID,
                lane_id=_LANE,
                issue_id=_ISSUE,
                lane_disposition=(
                    DISPOSITION_HIBERNATED if hibernated else DISPOSITION_ACTIVE
                ),
                process_release="released" if hibernated else "not_requested",
                worktree_identity="wt_recorded",
                recorded_worktree="/private/tmp/wiped_by_reboot",
                worktree_present=False,
                branch=_LANE,
                branch_exists=True,
                head_integrated=True,
                issue_closed=True,
                slots=(),
            )
        )

    def test_the_reboot_shape_still_plans_restore_and_still_offers_the_alternative(self):
        plan = self._plan()
        self.assertEqual(plan.convergence, CONVERGE_RESTORE_WORKTREE)
        self.assertEqual(plan.alternatives, (CONVERGE_TERMINALIZE_BOUND,))

    def test_the_alternative_now_carries_its_own_invocation(self):
        plan = self._plan()
        self.assertEqual(len(plan.alternative_steps), 1, plan.alternative_steps)
        command = plan.alternative_steps[0]
        self.assertIn("--worktree-absent", command)
        self.assertIn("--retire-active-live-zero", command)
        self.assertIn("/private/tmp/wiped_by_reboot", command)
        self.assertIn(f"--branch {_LANE}", command)

    def test_the_hibernated_variant_names_its_own_bound_rail(self):
        command = self._plan(hibernated=True).alternative_steps[0]
        self.assertIn("--worktree-absent", command)
        self.assertIn("--retire-hibernated-bound", command)

    def test_the_alternative_is_never_folded_into_the_primary_steps(self):
        """`steps` is the restore rail. Running both would restore a checkout and then
        terminalize with the flag that asserts it is absent."""
        plan = self._plan()
        self.assertFalse(
            any("--worktree-absent" in step for step in plan.steps), plan.steps
        )

    def test_every_other_plan_keeps_an_empty_alternative_steps(self):
        """The new field is additive: no plan that offers no alternative gains a command."""
        plan = plan_lane_convergence(
            RebootLaneFacts(
                workspace_id=_WORKSPACE_ID,
                lane_id=_LANE,
                issue_id=_ISSUE,
                worktree_identity="wt_recorded",
                recorded_worktree="/tmp/present",
                worktree_present=True,
                branch=_LANE,
                head_integrated=True,
                issue_closed=True,
                slots=(),
            )
        )
        self.assertEqual(plan.convergence, CONVERGE_TERMINALIZE_BOUND)
        self.assertEqual(plan.alternative_steps, ())
        self.assertEqual(plan.as_payload()["alternative_steps"], [])


class TheAlternativeTerminalizesTheWipedLane(_WipedCheckoutFixture):
    """The headline regression: the shape the audit prescribes now converges in one command."""

    def test_the_wiped_checkout_lane_terminalizes_without_restoring_anything(self):
        self.declare_active()
        self.wipe_checkout()
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)

        code, payload = self.run_retire()
        self.assertEqual(
            payload.get("retire_application", {}).get("state"),
            "retired",
            json.dumps(payload, sort_keys=True)[:800],
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.disposition(), DISPOSITION_RETIRED)

    def test_the_checkout_is_never_restored_or_pruned_by_the_rail(self):
        self.declare_active()
        self.wipe_checkout()
        listed_before = _git_out("worktree", "list", "--porcelain", cwd=self.primary)
        self.run_retire()
        self.assertFalse(self.lane_wt.exists(), "the rail materialized a checkout")
        self.assertEqual(
            _git_out("worktree", "list", "--porcelain", cwd=self.primary),
            listed_before,
            "the rail mutated git's worktree administrative record",
        )

    def test_the_preflight_no_longer_blocks_and_emits_the_metadata_only_runbook(self):
        self.declare_active()
        self.wipe_checkout()
        _, payload = self.run_retire()
        self.assertEqual(payload["decision"]["state"], "retire_ok", payload["decision"])
        self.assertEqual(payload["decision"]["blocked_reasons"], [])
        self.assertEqual(
            [step["title"] for step in payload["runbook"]],
            ["metadata-only terminalization"],
        )
        serialized = json.dumps(payload)
        self.assertNotIn("worktree remove", serialized)
        self.assertNotIn("branch -d", serialized)


# ---------------------------------------------------------------------------
# 2. The invariants the fix must not touch.
# ---------------------------------------------------------------------------


class TheInvariantsAreUnchanged(_WipedCheckoutFixture):
    """The whole risk of this change is that it leaks past the two facts it substitutes."""

    def test_an_unintegrated_head_is_still_refused(self):
        """The requirement is not relaxed by the checkout being gone — ancestry is measured
        from refs, which never needed one."""
        _git("checkout", "-q", _LANE, cwd=self.lane_wt)
        (self.lane_wt / "unintegrated.txt").write_text("y\n", encoding="utf-8")
        _git("add", "-A", cwd=self.lane_wt)
        _git("commit", "-qm", "unintegrated lane work", cwd=self.lane_wt)
        head = _git_out("rev-parse", _LANE, cwd=self.primary).strip()
        self.declare_active()
        self.wipe_checkout()

        _, payload = self.run_retire()
        self.assert_refused(payload, "head_not_integrated")
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)
        self.assertEqual(
            _git_out("rev-parse", _LANE, cwd=self.primary).strip(),
            head,
            "the refused retire moved the branch",
        )

    def test_no_branch_or_commit_is_deleted_by_a_successful_terminalize(self):
        self.declare_active()
        self.wipe_checkout()
        self.run_retire()
        self.assertEqual(self.disposition(), DISPOSITION_RETIRED)
        self.assertEqual(
            _git_out("rev-parse", _LANE, cwd=self.primary).strip(),
            self.lane_head,
            "the lane branch moved or was recreated",
        )
        self.assertIn(
            _LANE,
            _git_out("branch", "--list", _LANE, cwd=self.primary),
            "the lane branch was deleted",
        )
        self.assertEqual(
            _git_out("cat-file", "-t", self.lane_head, cwd=self.primary).strip(),
            "commit",
            "the lane commit was removed",
        )

    def test_a_live_expected_slot_still_blocks_the_terminalize(self):
        self.declare_active()
        self.wipe_checkout()
        self.rows = [
            {
                "name": encode_assigned_name(_WORKSPACE_ID, "codex", _LANE),
                "pane_id": "w1:p3",
                "terminal_id": "terminal:w1:p3",
                "agent": "codex",
                "agent_status": "idle",
            }
        ]
        _, payload = self.run_retire()
        self.assert_refused(payload, "live_pair_present")
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)

    def test_a_foreign_occupant_still_blocks_the_terminalize(self):
        self.declare_active()
        self.wipe_checkout()
        self.rows = [
            {
                "name": encode_assigned_name(_WORKSPACE_ID, "gemini", _LANE),
                "pane_id": "w1:p9",
                "terminal_id": "terminal:w1:p9",
                "agent": "gemini",
                "agent_status": "idle",
            }
        ]
        _, payload = self.run_retire()
        self.assert_refused(payload, "foreign_inventory_present")
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)

    def test_asserting_the_wt_family_cannot_reach_a_row_bound_otherwise(self):
        """The family is asserted, the BINDING is not. A row recording anything but this
        path's ``wt_`` token must still fail the attestation — otherwise asserting the family
        would have quietly become a way to terminalize a lane the caller does not own."""
        self.declare_active(binding="dl_some_other_lane_token")
        self.wipe_checkout()
        _, payload = self.run_retire()
        self.assert_refused(payload, "worktree_binding_mismatch")
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)

    def test_without_the_flag_a_missing_worktree_still_blocks_exactly_as_before(self):
        """#14499's pin, restated against the new flag's default: opting out is the default."""
        self.declare_active()
        self.wipe_checkout()
        _, payload = self.run_retire(worktree_absent=False)
        self.assertIn(
            "worktree_missing_after_reboot", payload["decision"]["blocked_reasons"]
        )
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)


# ---------------------------------------------------------------------------
# 3. The evidence path's own refusals.
# ---------------------------------------------------------------------------


class TheEvidencePathRefusesEveryUnprovenShape(_WipedCheckoutFixture):
    def test_a_checkout_that_still_exists_is_refused(self):
        """The sharpest one: the flag must not become a way to skip a real checkout's
        dirty gate. The refusal is taken BEFORE the preflight decides scope."""
        self.declare_active()
        (self.lane_wt / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        _, payload = self.run_retire()
        self.assert_refused(payload, ABSENT_WT_WORKTREE_PRESENT)
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)
        self.assertTrue(self.lane_wt.is_dir())

    def test_an_already_pruned_entry_is_refused(self):
        """Nothing then ties --branch to the lane, so the retire fails closed rather than
        trusting the caller's --branch."""
        self.declare_active()
        self.wipe_checkout()
        _git("worktree", "prune", cwd=self.primary)
        _, payload = self.run_retire()
        self.assert_refused(payload, ABSENT_WT_NOT_REGISTERED)
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)

    def test_a_locked_entry_is_refused(self):
        """A lock is an operator's deliberate hold on the entry; git does not call it
        prunable and neither does this rail."""
        _git("worktree", "lock", str(self.lane_wt), cwd=self.primary)
        self.declare_active()
        self.wipe_checkout()
        _, payload = self.run_retire()
        self.assert_refused(payload, ABSENT_WT_NOT_PRUNABLE)
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)

    def test_a_differently_branched_entry_is_refused(self):
        self.declare_active()
        self.wipe_checkout()
        _git("branch", "unrelated_branch", cwd=self.primary)
        _, payload = self.run_retire(branch="unrelated_branch")
        self.assert_refused(payload, ABSENT_WT_BRANCH_MISMATCH)
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)

    def test_a_non_applicable_intent_is_a_zero_write_refusal(self):
        """Refused rather than silently ignored, so the flag never reads as honoured where
        it changed nothing."""
        self.declare_active()
        self.wipe_checkout()
        _, payload = self.run_retire(
            retire_active_live_zero=False, retire_active_unbound_live_zero=True
        )
        self.assert_refused(payload, REASON_WORKTREE_ABSENT_NOT_APPLICABLE)
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)


class TheAbsenceIsReProvenAtTheTerminalMutationBoundary(_WipedCheckoutFixture):
    """Review j#109127 ``finding_actiontimerace``, verdict j#109134 (accepted, reproduced).

    The preflight needs the evidence EARLY — that is what keeps a wiped path out of the dirty /
    missing gates — but an early proof is a statement about the past. Measured before the fix:
    restoring the checkout at the recorded path (with uncommitted work) after that proof and
    before the CAS still produced ``exit_code=0`` / ``retired`` / ``worktree_present=true`` /
    ``dirty_file_present=true``.

    The concurrent event is injected with a one-shot hook around the evidence resolver; the code
    under test is unmodified. A real restore cannot be quiet — ``git worktree add`` refuses
    while the entry is still registered — so these fixtures perform the prune-then-add sequence
    git actually forces, which is exactly why the re-proof sees a changed world.
    """

    def _run_with_event_in_the_window(self, event):
        """Run the retire with ``event`` firing once, right after the first absence proof."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_absent_worktree_evidence as evidence_module,
        )

        real = evidence_module.resolve_absent_worktree_evidence
        fired: list[bool] = []

        def hooked(*args, **kwargs):
            result = real(*args, **kwargs)
            if not fired:
                fired.append(True)
                event()
            return result

        with mock.patch.object(
            evidence_module, "resolve_absent_worktree_evidence", hooked
        ):
            outcome = self.run_retire()
        self.assertTrue(fired, "the concurrent event never entered the window")
        return outcome

    def _restore_and_dirty(self):
        """Bring the checkout back at the exact recorded path, carrying uncommitted work."""
        result = subprocess.run(
            ["git", "worktree", "add", "-q", str(self.lane_wt), _LANE],
            cwd=str(self.primary), capture_output=True, text=True,
        )
        self.assertNotEqual(
            result.returncode, 0,
            "precondition: git must refuse to add over a still-registered entry",
        )
        self.assertIn("already registered", result.stderr)
        _git("worktree", "prune", cwd=self.primary)
        _git("worktree", "add", "-q", str(self.lane_wt), _LANE, cwd=self.primary)
        (self.lane_wt / "uncommitted.txt").write_text("precious\n", encoding="utf-8")

    def test_a_checkout_restored_after_the_initial_proof_blocks_the_terminal_write(self):
        self.declare_active()
        self.wipe_checkout()
        _, payload = self._run_with_event_in_the_window(self._restore_and_dirty)

        self.assert_refused(payload, ABSENT_WT_WORKTREE_PRESENT)
        self.assertEqual(
            self.disposition(),
            DISPOSITION_ACTIVE,
            "a terminal write landed against a checkout that came back",
        )
        # The uncommitted work is still there and was never this rail's to touch.
        self.assertTrue((self.lane_wt / "uncommitted.txt").exists())

    def test_an_entry_pruned_after_the_initial_proof_blocks_the_terminal_write(self):
        """The prune alone already destroys the evidence, before any re-add."""
        self.declare_active()
        self.wipe_checkout()
        _, payload = self._run_with_event_in_the_window(
            lambda: _git("worktree", "prune", cwd=self.primary)
        )
        self.assert_refused(payload, ABSENT_WT_NOT_REGISTERED)
        self.assertEqual(self.disposition(), DISPOSITION_ACTIVE)

    def test_the_re_proof_does_not_disturb_a_run_with_no_concurrent_event(self):
        """Control: the second read must not become a second way to fail a good run."""
        self.declare_active()
        self.wipe_checkout()
        code, payload = self.run_retire()
        self.assertEqual(payload.get("retire_application", {}).get("state"), "retired")
        self.assertEqual(code, 0)
        self.assertEqual(self.disposition(), DISPOSITION_RETIRED)

    def test_a_drifting_re_proof_is_refused_rather_than_silently_adopted(self):
        """A re-proof that verifies against a DIFFERENT lane is never adopted."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_absent_worktree_evidence import (  # noqa: E501
            AbsentWorktreeEvidence,
            revalidate_absent_worktree_evidence,
        )

        self.wipe_checkout()
        prior = AbsentWorktreeEvidence(
            admissible=True,
            worktree_path=str(self.lane_wt),
            branch=_LANE,
            metadata_token="wt_a_different_token",
            legacy_token="wt_a_different_token",
        )
        result = revalidate_absent_worktree_evidence(
            self.primary,
            prior=prior,
            worktree=str(self.lane_wt),
            branch=_LANE,
            lane_label=_LANE,
        )
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, ABSENT_WT_DRIFT)
        self.assertIn("metadata_token", result.detail)


class TheHibernatedBoundRailCarriesTheSameContract(_WipedCheckoutFixture):
    """The second BOUND rail, driven for real rather than assumed symmetric.

    ``--worktree-absent`` modifies two rails and the planner emits the flag for both
    dispositions, so the hibernated / released rail is exercised end to end here too — the same
    substitution and the same action-time re-proof. Seeding reuses #13845's real store
    transitions (`_seed_hibernated_released_bound`) rather than a re-derived fixture; only the
    helper is imported, so its TestCases are not collected twice.
    """

    def _seed_hibernated(self) -> None:
        from regressions.test_issue_13845_hibernated_bound_live_zero_retire import (  # noqa: E501
            _seed_hibernated_released_bound,
        )

        _seed_hibernated_released_bound(
            path=None,
            key=LaneLifecycleKey(_WORKSPACE_ID, _LANE),
            issue=_ISSUE,
            worktree_identity=self.recorded_binding,
            declared_slots=(),
        )

    def test_the_hibernated_bound_rail_terminalizes_a_wiped_checkout(self):
        self._seed_hibernated()
        self.wipe_checkout()
        code, payload = self.run_retire(
            retire_active_live_zero=False, retire_hibernated_bound=True
        )
        self.assertEqual(
            payload.get("retire_application", {}).get("state"),
            "retired",
            json.dumps(payload, sort_keys=True)[:800],
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.disposition(), DISPOSITION_RETIRED)
        self.assertEqual(
            _git_out("rev-parse", _LANE, cwd=self.primary).strip(),
            self.lane_head,
            "the lane branch moved",
        )

    def test_the_hibernated_bound_rail_re_proves_absence_before_its_cas(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_absent_worktree_evidence as evidence_module,
        )

        self._seed_hibernated()
        self.wipe_checkout()
        real = evidence_module.resolve_absent_worktree_evidence
        fired: list[bool] = []

        def hooked(*args, **kwargs):
            result = real(*args, **kwargs)
            if not fired:
                fired.append(True)
                _git("worktree", "prune", cwd=self.primary)
                _git("worktree", "add", "-q", str(self.lane_wt), _LANE, cwd=self.primary)
                (self.lane_wt / "uncommitted.txt").write_text("x\n", encoding="utf-8")
            return result

        with mock.patch.object(
            evidence_module, "resolve_absent_worktree_evidence", hooked
        ):
            _, payload = self.run_retire(
                retire_active_live_zero=False, retire_hibernated_bound=True
            )
        self.assertTrue(fired, "the concurrent event never entered the window")
        self.assert_refused(payload, ABSENT_WT_WORKTREE_PRESENT)
        self.assertEqual(self.disposition(), DISPOSITION_HIBERNATED)


class TheEvidenceResolverIsFailClosed(unittest.TestCase):
    """The resolver itself, driven directly — including the git failures a real repo cannot
    be talked into producing on demand."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.gone = self.root / "wiped"

    def test_an_unreadable_worktree_list_is_refused_not_assumed_empty(self):
        def _failing(_repo, _args):
            return subprocess.CompletedProcess(
                args=["git"], returncode=128, stdout="", stderr="not a git repository"
            )

        evidence = resolve_absent_worktree_evidence(
            self.root,
            worktree=str(self.gone),
            branch="lane",
            lane_label="lane",
            runner=_failing,
        )
        self.assertFalse(evidence.admissible)
        self.assertEqual(evidence.reason, ABSENT_WT_LIST_UNREADABLE)

    def test_a_git_that_cannot_be_spawned_is_refused(self):
        def _raising(_repo, _args):
            raise OSError("git not found")

        evidence = resolve_absent_worktree_evidence(
            self.root,
            worktree=str(self.gone),
            branch="lane",
            lane_label="lane",
            runner=_raising,
        )
        self.assertFalse(evidence.admissible)
        self.assertEqual(evidence.reason, ABSENT_WT_LIST_UNREADABLE)

    def test_a_detached_entry_carries_no_branch_and_is_a_mismatch(self):
        def _detached(_repo, _args):
            return subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=(
                    f"worktree {self.gone}\n"
                    "HEAD 1111111111111111111111111111111111111111\n"
                    "detached\n"
                    "prunable gitdir file points to non-existent location\n"
                ),
                stderr="",
            )

        evidence = resolve_absent_worktree_evidence(
            self.root,
            worktree=str(self.gone),
            branch="lane",
            lane_label="lane",
            runner=_detached,
        )
        self.assertFalse(evidence.admissible)
        self.assertEqual(evidence.reason, ABSENT_WT_BRANCH_MISMATCH)

    def test_the_porcelain_decoder_reads_prunable_locked_and_short_branch_names(self):
        entries = parse_worktree_list_porcelain(
            "worktree /a\n"
            "HEAD aaaa\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /b\n"
            "HEAD bbbb\n"
            "branch refs/heads/lane_b\n"
            "prunable gitdir file points to non-existent location\n"
            "\n"
            "worktree /c\n"
            "HEAD cccc\n"
            "detached\n"
            "locked\n"
        )
        self.assertEqual([e.path for e in entries], ["/a", "/b", "/c"])
        self.assertEqual([e.branch for e in entries], ["main", "lane_b", ""])
        self.assertEqual([e.prunable for e in entries], [False, True, False])
        self.assertEqual([e.locked for e in entries], [False, False, True])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
