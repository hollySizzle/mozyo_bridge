"""Typed bound-signature faults report the exact broken axis, and never fabricate one.

Redmine #13933 R7 (design answer j#81046 Decision 2, review finding F1 j#81182). The collapsed
`not_hibernated_released_bound_pins_empty` token hid WHICH premise broke, so #13846 j#81024 read
a worktree-identity mismatch as a partial-effect defect for a whole round. These tests drive the
real per-axis evaluator against real `LaneLifecycleRecord` shapes (one axis perturbed at a time)
and the two rails' classifiers against unknown-vs-known pin state.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    LaneLifecycleRecord,
    RELEASE_NOT_REQUESTED,
    RELEASE_RELEASED,
    REPLACEMENT_NOT_REQUESTED,
    REPLACEMENT_REQUESTED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence import (
    BLOCK_NOT_BOUND_SIGNATURE,
    BoundPairObservation,
    ConvergeBoundPairRequest,
    _classify as convergence_classify,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_composer_discard import (
    PrepareBoundPairRequest,
    PreparationObservation,
    _classify as prepare_classify,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_bound_pair_convergence_live import (
    LiveBoundPairConvergenceOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
    FAULT_IDENTITY_ABSENT,
    FAULT_IDENTITY_MISMATCH,
    FAULT_ISSUE_MISMATCH,
    FAULT_NOT_HIBERNATED,
    FAULT_NOT_ISSUE_BOUND,
    FAULT_NOT_RELEASED,
    FAULT_PINS_NOT_EMPTY,
    FAULT_PROJECT_SCOPED,
    FAULT_REPLACEMENT_UNSETTLED,
)

_ISSUE = "13846"
_LANE = "issue_13846_stale_worker_dispatch_admission"
_IDENTITY = "wt_c0ffee1234abcd"


def _green_record() -> LaneLifecycleRecord:
    """A record whose bound signature holds on every axis."""
    return LaneLifecycleRecord(
        repo_workspace_id="ws",
        lane_id=_LANE,
        issue_id=_ISSUE,
        lane_disposition=DISPOSITION_HIBERNATED,
        process_release=RELEASE_RELEASED,
        revision=4,
        replacement_state=REPLACEMENT_NOT_REQUESTED,
        worktree_identity=_IDENTITY,
        binding_kind=BINDING_KIND_ISSUE,
        project_scope="",
        lane_generation=1,
        declared_slots="",
    )


def _request() -> ConvergeBoundPairRequest:
    return ConvergeBoundPairRequest(
        issue=_ISSUE, journal="80925", lane=_LANE, worktree="/x", branch="b"
    )


def _faults(record: LaneLifecycleRecord, *, identity: str = _IDENTITY):
    return LiveBoundPairConvergenceOps._bound_signature_faults(_request(), record, identity)


class PerAxisFaultTests(unittest.TestCase):
    """Each broken axis is named; a whole-green record names nothing."""

    def test_green_record_has_no_faults(self):
        self.assertEqual(_faults(_green_record()), ())

    def test_not_hibernated(self):
        self.assertEqual(
            _faults(replace(_green_record(), lane_disposition=DISPOSITION_ACTIVE)),
            (FAULT_NOT_HIBERNATED,),
        )

    def test_not_issue_bound(self):
        self.assertEqual(
            _faults(replace(_green_record(), binding_kind="project_gateway")),
            (FAULT_NOT_ISSUE_BOUND,),
        )

    def test_issue_mismatch(self):
        self.assertEqual(
            _faults(replace(_green_record(), issue_id="99999")),
            (FAULT_ISSUE_MISMATCH,),
        )

    def test_project_scoped(self):
        self.assertEqual(
            _faults(replace(_green_record(), project_scope="some/project")),
            (FAULT_PROJECT_SCOPED,),
        )

    def test_worktree_identity_absent(self):
        self.assertEqual(
            _faults(replace(_green_record(), worktree_identity="")),
            (FAULT_IDENTITY_ABSENT,),
        )

    def test_worktree_identity_mismatch(self):
        # The exact #13846 j#81024 shape: a non-empty row identity that is not the one the
        # target root derives.  ABSENT and MISMATCH are distinct axes, never conflated.
        self.assertEqual(
            _faults(_green_record(), identity="wt_something_else"),
            (FAULT_IDENTITY_MISMATCH,),
        )

    def test_not_released(self):
        self.assertEqual(
            _faults(replace(_green_record(), process_release=RELEASE_NOT_REQUESTED)),
            (FAULT_NOT_RELEASED,),
        )

    def test_replacement_unsettled(self):
        self.assertEqual(
            _faults(replace(_green_record(), replacement_state=REPLACEMENT_REQUESTED)),
            (FAULT_REPLACEMENT_UNSETTLED,),
        )

    def test_multiple_axes_are_all_named_in_order(self):
        broken = replace(
            _green_record(),
            lane_disposition=DISPOSITION_ACTIVE,
            worktree_identity="",
            process_release=RELEASE_NOT_REQUESTED,
        )
        self.assertEqual(
            _faults(broken),
            (FAULT_NOT_HIBERNATED, FAULT_IDENTITY_ABSENT, FAULT_NOT_RELEASED),
        )

    def test_lifecycle_exact_agrees_with_the_fault_evaluator(self):
        # The bool authority delegates to the typed axes, so the two can never disagree.
        self.assertTrue(
            LiveBoundPairConvergenceOps._lifecycle_exact(_request(), _green_record(), _IDENTITY)
        )
        self.assertFalse(
            LiveBoundPairConvergenceOps._lifecycle_exact(
                _request(), _green_record(), "wt_something_else"
            )
        )


class RawValueNonLeakTests(unittest.TestCase):
    """A fault detail names the axis only; the row's private values never appear."""

    def test_identity_mismatch_detail_leaks_no_token(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_bound_pair_convergence import (
            bound_signature_detail,
        )

        detail = bound_signature_detail(_faults(_green_record(), identity="wt_secret_value"))
        self.assertEqual(detail, "bound signature faults: worktree_identity_mismatch")
        self.assertNotIn("wt_secret_value", detail)
        self.assertNotIn(_IDENTITY, detail)


class UnknownPinsNeverFabricatesFaultTests(unittest.TestCase):
    """F1: an unread row leaves pins UNKNOWN — never reported as `pins_not_empty`."""

    def _prepare_request(self) -> PrepareBoundPairRequest:
        return PrepareBoundPairRequest(
            issue=_ISSUE, journal="80925", lane=_LANE, worktree="/x", branch="b"
        )

    def test_prepare_unread_row_reports_the_real_reason_not_pins(self):
        # worktree/lifecycle unresolved: pins_known defaults False.  Pre-fix this reported
        # `pins_not_empty`; the real reason is the observation's own detail.
        obs = PreparationObservation(detail="worktree/workspace identity unresolved")
        terminal, expected = prepare_classify(self._prepare_request(), obs)
        self.assertIsNone(expected)
        self.assertEqual(terminal.reason, BLOCK_NOT_BOUND_SIGNATURE)
        self.assertNotIn("pins_not_empty", terminal.detail)
        self.assertEqual(terminal.detail, "worktree/workspace identity unresolved")

    def test_prepare_positive_nonempty_pins_still_reports_the_axis(self):
        obs = PreparationObservation(
            workspace_id="ws", worktree_path="/x", worktree_identity=_IDENTITY, branch="b",
            revision=4, generation=1, lifecycle_exact=True, pins_empty=False, pins_known=True,
            inventory_readable=True, worktree_readable=True, worktree_clean=True,
            branch_matches=True,
        )
        terminal, _ = prepare_classify(self._prepare_request(), obs)
        self.assertEqual(terminal.detail, "bound signature faults: pins_not_empty")

    def test_convergence_unread_row_reports_the_real_reason_not_pins(self):
        obs = BoundPairObservation(detail="worktree/workspace identity unresolved")
        terminal, expected = convergence_classify(_request(), obs)
        self.assertIsNone(expected)
        self.assertEqual(terminal.verdict.reason, BLOCK_NOT_BOUND_SIGNATURE)
        self.assertNotIn("pins_not_empty", terminal.verdict.detail)

    def test_convergence_positive_nonempty_pins_still_reports_the_axis(self):
        obs = BoundPairObservation(
            workspace_id="ws", worktree_path="/x", worktree_identity=_IDENTITY, branch="b",
            revision=4, generation=1, lifecycle_exact=True, pins_empty=False, pins_exact=False,
            pins_known=True, inventory_readable=True, worktree_readable=True, worktree_clean=True,
            branch_matches=True,
        )
        terminal, _ = convergence_classify(_request(), obs)
        self.assertIn(FAULT_PINS_NOT_EMPTY, terminal.verdict.detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
