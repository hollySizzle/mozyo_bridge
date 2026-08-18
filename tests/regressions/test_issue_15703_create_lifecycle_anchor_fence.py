"""Redmine #15703 — the anchorless fresh create's silent owner-rowless lane regression.

The live failure this pins (lane ``issue_15692_write_transport``, 2026-08-18): a
``sublane create --execute`` with no ``--journal`` created the worktree and the pane
pair, while ``declare_created_lane_lifecycle`` skipped the lifecycle owner declaration
**silently** (its missing-anchor early return printed nothing). The lane looked started
but had no owner row, so every downstream owner-row rail (dispatch-worker / retire /
recover) failed closed against it with no visible cause — a permanently blocked lane
whose defect left no trace at create time.

Two axes, pinned separately:

* **the fail-closed fence** (design option (a) of the issue): a live ``--execute`` that
  would birth a FRESH lane now requires the durable ``--journal`` anchor even when the
  dispatch leg is skipped (``--no-dispatch``), refused typed
  (``lifecycle_anchor_required``) BEFORE any worktree / pane mutation, via
  ``create_lifecycle_anchor_gate`` in the shared ``pre_mutation_admission`` boundary.
  Exempt: an existing live matching pair, which the execute ADOPTS — its owner row is
  validated by the anchor-tolerant adopt declaration gate (#13810 R3-F3), so the adopt /
  self-heal re-run keeps its pre-#15703 behavior.
* **typed skip observability** (option (b), for the branches no pre-mutation fence can
  see, e.g. an unresolved workspace segment): every remaining skip branch of
  ``declare_created_lane_lifecycle`` prints a typed stderr warning and returns its
  ``DECLARE_SKIPPED_*`` token instead of returning silently.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E402,E501
    SublaneActuateUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_create_lifecycle_declaration import (  # noqa: E402,E501
    DECLARE_SKIPPED_INVALID_ANCHOR,
    DECLARE_SKIPPED_MISSING_INPUTS,
    declare_created_lane_lifecycle,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E402,E501
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    ACTUATE_READY,
    BLOCKED_REASONS,
    DISPATCH_SKIPPED,
    REASON_LIFECYCLE_ANCHOR_REQUIRED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E402,E501
    SublaneCreateRequest,
    SublaneLaneView,
)


def _lane(*, lane_label="issue_15703_x", issue="15703", repo_root="/wt/15703"):
    return SublaneLaneView(
        workspace_id="ws",
        lane_id="l1",
        lane_label=lane_label,
        issue=issue,
        branch="b",
        repo_root=repo_root,
        gateway_pane="%120",
        worker_pane="%121",
        state="active",
    )


class FakeOps:
    """Minimal scriptable :class:`SublaneActuatorOps` port recording every mutation."""

    def __init__(self, *, git=True, worktree_exists=False, lanes=()):
        self._git = git
        self._we = worktree_exists
        self._lane_seq = list(lanes)
        self.calls = []

    def canonical_workspace_root(self):
        return "/ws"

    def is_git_workspace(self):
        return self._git

    def worktree_exists(self, branch):
        return self._we

    def create_worktree(self, *, branch, worktree_path, base_ref=None):
        self.calls.append(("create_worktree", branch))

    def append_lane_column(self, worktree_path):
        self.calls.append(("append_lane_column", worktree_path))

    def append_lane_argv(self, worktree_path):
        return ["cockpit", "append", "--repo", worktree_path, "--no-attach"]

    def read_lane(self, worktree_path):
        if not self._lane_seq:
            return None
        return self._lane_seq.pop(0)

    def declare_adopted_lane_lifecycle(self, worktree_path, *, adopted):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
            ADOPT_DECL_DECLARED,
            ADOPT_DECL_NOT_ADOPTED,
        )

        self.calls.append(("declare_adopted_lane_lifecycle", adopted))
        return ADOPT_DECL_DECLARED if adopted else ADOPT_DECL_NOT_ADOPTED

    def probe_gateway_ready(self, gateway_pane):
        return True

    def dispatch_implementation_request(self, **kwargs):
        self.calls.append(("dispatch", kwargs))
        return 0

    def _mutations(self):
        return [c[0] for c in self.calls if c[0] in ("create_worktree", "append_lane_column", "dispatch")]  # noqa: E501


def _req(**kw):
    base = dict(
        issue="15703",
        lane_label="issue_15703_x",
        branch="b",
        worktree_path="/wt/15703",
        journal=None,
        upstream_coordinator="%2",
    )
    base.update(kw)
    return SublaneCreateRequest(**base)


class FreshCreateAnchorFenceTests(unittest.TestCase):
    """An anchorless execute that would birth a fresh lane refuses, zero-mutation."""

    def test_fresh_worktree_create_without_journal_blocks_pre_mutation(self):
        ops = FakeOps(git=True, worktree_exists=False, lanes=[None])
        outcome = SublaneActuateUseCase(ops).run(
            _req(), execute=True, dispatch=False
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LIFECYCLE_ANCHOR_REQUIRED, outcome.blocked_reasons)
        self.assertEqual(ops._mutations(), [])

    def test_reuse_worktree_fresh_column_without_journal_blocks_pre_append(self):
        # The dispatch-anchor check never saw this shape (--no-dispatch) and the
        # worktree already exists (reuse), so pre-#15703 the fresh COLUMN appended
        # and declared with an empty anchor — the exact silent owner-rowless birth.
        ops = FakeOps(git=True, worktree_exists=True, lanes=[None])
        outcome = SublaneActuateUseCase(ops).run(
            _req(), execute=True, dispatch=False
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LIFECYCLE_ANCHOR_REQUIRED, outcome.blocked_reasons)
        self.assertEqual(ops._mutations(), [])

    def test_non_git_fresh_lane_without_journal_blocks(self):
        # #13392 non-git (skip_no_git) lanes declare an owner row too — same fence.
        ops = FakeOps(git=False, lanes=[None])
        outcome = SublaneActuateUseCase(ops).run(
            _req(branch="", worktree_path=""), execute=True, dispatch=False
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LIFECYCLE_ANCHOR_REQUIRED, outcome.blocked_reasons)
        self.assertEqual(ops._mutations(), [])

    def test_identity_mismatched_live_pair_is_not_an_adopt_exemption(self):
        # A live pair for a DIFFERENT lane never stands in for the adopt exemption:
        # the create would still birth a fresh lane, so the anchor stays required.
        ops = FakeOps(
            git=True,
            worktree_exists=True,
            lanes=[_lane(lane_label="issue_9999_other", issue="9999")],
        )
        outcome = SublaneActuateUseCase(ops).run(
            _req(), execute=True, dispatch=False
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LIFECYCLE_ANCHOR_REQUIRED, outcome.blocked_reasons)
        self.assertEqual(ops._mutations(), [])

    def test_registry_carries_the_typed_reason(self):
        self.assertIn(REASON_LIFECYCLE_ANCHOR_REQUIRED, BLOCKED_REASONS)


class ExistingBehaviorRegressionTests(unittest.TestCase):
    """The adopt / anchored / plan-only shapes keep their pre-#15703 behavior."""

    def test_adopt_of_live_matching_pair_without_journal_still_executes(self):
        # Self-heal / adopt re-run: the owner row is validated by the adopt
        # declaration gate (#13810 R3-F3), not by this fence. First read: the gate's
        # probe; second: the execute's adopt resolution.
        ops = FakeOps(git=True, worktree_exists=True, lanes=[_lane(), _lane()])
        outcome = SublaneActuateUseCase(ops).run(
            _req(), execute=True, dispatch=False
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertEqual(outcome.dispatch_result, DISPATCH_SKIPPED)
        self.assertTrue(outcome.adopted)
        self.assertIn(("declare_adopted_lane_lifecycle", True), ops.calls)

    def test_fresh_create_with_journal_passes_the_fence(self):
        ops = FakeOps(git=True, worktree_exists=False, lanes=[None, _lane()])
        outcome = SublaneActuateUseCase(ops).run(
            _req(journal="107966"), execute=True, dispatch=False
        )
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertIn("create_worktree", ops._mutations())
        self.assertIn("append_lane_column", ops._mutations())

    def test_dry_run_without_journal_stays_ready(self):
        # Plan surfaces stay side-effect-free and un-fenced (parity with the
        # dispatch-anchor check, which is also execute-only).
        ops = FakeOps(git=True)
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=False)
        self.assertEqual(outcome.status, ACTUATE_READY)


class DeclareTypedSkipObservabilityTests(unittest.TestCase):
    """Every declare skip branch is typed and observable, never silent (option (b))."""

    def _declare(self, **kw):
        base = dict(
            repo_workspace_id="mzb1_ws",
            lane_label="issue_15703_x",
            issue="15703",
            journal="107966",
        )
        base.update(kw)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            outcome = declare_created_lane_lifecycle(**base)
        return outcome, stderr.getvalue()

    def test_missing_journal_returns_typed_skip_and_warns(self):
        outcome, err = self._declare(journal="")
        self.assertEqual(outcome, DECLARE_SKIPPED_MISSING_INPUTS)
        self.assertIn("lane lifecycle declare skipped", err)
        self.assertIn("journal", err)
        self.assertIn("owner-unbound", err)

    def test_unresolved_workspace_returns_typed_skip_and_warns(self):
        # The live-incident-adjacent branch no fence can see pre-mutation: the
        # workspace segment failed to resolve at declare time.
        outcome, err = self._declare(repo_workspace_id="")
        self.assertEqual(outcome, DECLARE_SKIPPED_MISSING_INPUTS)
        self.assertIn("workspace", err)
        self.assertIn("owner-unbound", err)

    def test_non_decimal_anchor_returns_typed_skip_and_warns(self):
        outcome, err = self._declare(journal="j#107966")
        self.assertEqual(outcome, DECLARE_SKIPPED_INVALID_ANCHOR)
        self.assertIn("non-decimal", err)
        self.assertIn("owner-unbound", err)


if __name__ == "__main__":
    unittest.main()
