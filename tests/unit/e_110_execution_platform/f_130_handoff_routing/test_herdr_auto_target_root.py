"""The ``--target-repo auto`` target-frame policy under herdr (Redmine #14249 R2).

Pins the two pure halves of :mod:`...application.herdr_auto_target_root` — which frame answers
"what is the TARGET's repo root", and what the cross-lane answer is — over stated facts, with no
git / sqlite / herdr. The live composition's own I/O (the lifecycle read and the
``git worktree list`` topology join) is exercised by the regression fixture.

The one property every cell here defends: **no refusal degrades to the sender's cwd**. That
degradation is the defect j#94419 measured, so a refusal must carry an empty ``root``.
"""
from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.herdr_auto_target_root import (  # noqa: E501
    BASIS_LANE_WORKTREE,
    BASIS_SENDER_LANE,
    BASIS_WORKSPACE_CANONICAL,
    REFUSE_FOREIGN_WORKSPACE,
    REFUSE_IDENTITY_UNATTESTED,
    REFUSE_LANE_BINDING_ABSENT,
    REFUSE_LANE_BINDING_UNBOUND,
    REFUSE_LANE_WORKTREE_UNRESOLVED,
    classify_auto_target_basis,
    decide_default_lane_root,
    decide_lane_worktree_root,
)

WS = "project-workspace"
SENDER_LANE = "main_next_integration"
TARGET_LANE = "issue_14249_target_repo_workdir_resolution"


def _classify(**over):
    kwargs = {
        "sender_workspace_id": WS,
        "target_workspace_id": WS,
        "sender_lane_id": SENDER_LANE,
        "target_lane_id": TARGET_LANE,
    }
    kwargs.update(over)
    return classify_auto_target_basis(**kwargs)


class BasisClassificationTest(unittest.TestCase):
    def test_the_senders_own_lane_keeps_the_sender_frame(self) -> None:
        """The #13331 case: same unit, so this checkout IS the target's root."""
        classified = _classify(target_lane_id=SENDER_LANE)
        self.assertEqual(classified.basis, BASIS_SENDER_LANE)
        self.assertEqual(classified.reason, "")

    def test_an_unset_lane_on_both_sides_is_still_the_same_unit(self) -> None:
        # An empty lane normalizes to the workspace-default lane on BOTH sides, so a legacy
        # default-lane pair must not read as cross-lane.
        classified = _classify(sender_lane_id="", target_lane_id="")
        self.assertEqual(classified.basis, BASIS_SENDER_LANE)

    def test_another_lane_of_this_workspace_resolves_that_lanes_worktree(self) -> None:
        """The j#94419 arrangement: coordinator lane -> issue lane."""
        classified = _classify()
        self.assertEqual(classified.basis, BASIS_LANE_WORKTREE)
        self.assertIn(TARGET_LANE, classified.detail)

    def test_a_foreign_workspace_refuses_instead_of_guessing(self) -> None:
        classified = _classify(target_workspace_id="some-other-project")
        self.assertEqual(classified.basis, "")
        self.assertEqual(classified.reason, REFUSE_FOREIGN_WORKSPACE)

    def test_an_unattested_sender_workspace_refuses(self) -> None:
        # An env-less / unattested sender must not collapse onto "same workspace" (which would
        # then read as the sender lane and hand back exactly the root this issue removed).
        classified = _classify(sender_workspace_id="")
        self.assertEqual(classified.reason, REFUSE_IDENTITY_UNATTESTED)

    def test_an_unattested_target_workspace_refuses(self) -> None:
        classified = _classify(target_workspace_id="   ")
        self.assertEqual(classified.reason, REFUSE_IDENTITY_UNATTESTED)

    def test_a_legacy_per_lane_workspace_unit_is_its_own_sender_lane(self) -> None:
        # A pre-#13377 lane's unit is its own `wt_` token, not the project workspace. Comparing
        # the two live units (rather than re-deriving one from the repo) keeps that same-lane
        # dispatch resolving instead of reading as a foreign workspace.
        legacy = "wt_0f1e2d3c4b5a6978"
        classified = _classify(
            sender_workspace_id=legacy,
            target_workspace_id=legacy,
            sender_lane_id="default",
            target_lane_id="",
        )
        self.assertEqual(classified.basis, BASIS_SENDER_LANE)


class LaneWorktreeDecisionTest(unittest.TestCase):
    def test_a_verified_worktree_is_the_target_root(self) -> None:
        decided = decide_lane_worktree_root(
            target_lane_id=TARGET_LANE,
            lane_binding="wt_0123456789abcdef",
            lane_worktree="/checkouts/lane_14249",
        )
        self.assertTrue(decided.ok)
        self.assertEqual(decided.root, "/checkouts/lane_14249")
        self.assertEqual(decided.basis, BASIS_LANE_WORKTREE)
        self.assertEqual(decided.reason, "")

    def test_an_absent_lifecycle_row_refuses_with_its_own_reason(self) -> None:
        decided = decide_lane_worktree_root(
            target_lane_id=TARGET_LANE, lane_binding=None, lane_worktree=""
        )
        self.assertFalse(decided.ok)
        self.assertEqual(decided.root, "")
        self.assertEqual(decided.reason, REFUSE_LANE_BINDING_ABSENT)

    def test_a_present_but_unbound_row_is_a_distinct_refusal(self) -> None:
        # A v1/v2/v3 row (empty `worktree_identity`) is a DIFFERENT fact from "no row": the
        # #14475 repair rail converges it, so the operator needs the two named apart.
        decided = decide_lane_worktree_root(
            target_lane_id=TARGET_LANE, lane_binding="", lane_worktree="/anything"
        )
        self.assertFalse(decided.ok)
        self.assertEqual(decided.reason, REFUSE_LANE_BINDING_UNBOUND)

    def test_a_bound_row_whose_join_finds_nothing_refuses(self) -> None:
        decided = decide_lane_worktree_root(
            target_lane_id=TARGET_LANE,
            lane_binding="wt_0123456789abcdef",
            lane_worktree="",
        )
        self.assertFalse(decided.ok)
        self.assertEqual(decided.reason, REFUSE_LANE_WORKTREE_UNRESOLVED)

    def test_no_refusal_ever_carries_a_root(self) -> None:
        """The load-bearing invariant: a refusal cannot be read as a sender-cwd answer."""
        refusals = [
            decide_lane_worktree_root(
                target_lane_id=TARGET_LANE, lane_binding=None, lane_worktree="/x"
            ),
            decide_lane_worktree_root(
                target_lane_id=TARGET_LANE, lane_binding="   ", lane_worktree="/x"
            ),
            decide_lane_worktree_root(
                target_lane_id=TARGET_LANE, lane_binding="wt_a", lane_worktree="  "
            ),
        ]
        for decided in refusals:
            self.assertEqual(decided.root, "", decided.reason)
            self.assertFalse(decided.ok, decided.reason)
            self.assertTrue(decided.reason)


_LIVE_MAIN = {"exists": True, "is_dir": True, "is_git": True, "is_main_worktree": True}


def _decide_default(**over):
    kwargs = {
        "target_lane_id": "default",
        "canonical_root": "/registry/canonical",
        "liveness": dict(_LIVE_MAIN),
        "scope_matches": True,
    }
    kwargs.update(over)
    return decide_default_lane_root(**kwargs)


class DefaultLaneDecisionTest(unittest.TestCase):
    """#15707 (b): the coordinator (default) lane answers from the VERIFIED registry canonical.

    The default lane structurally owns no lifecycle row (only ``sublane create`` writes rows),
    so the row-based decision above systematically refused every gateway->coordinator callback
    with ``lane_binding_absent`` (#15701 j#107992 / #15704 j#108011). The registry fallback is
    admitted only fully verified; every partial fact keeps the same refusal.
    """

    def test_a_fully_verified_canonical_is_the_default_lane_root(self) -> None:
        decided = _decide_default()
        self.assertTrue(decided.ok)
        self.assertEqual(decided.root, "/registry/canonical")
        self.assertEqual(decided.basis, BASIS_WORKSPACE_CANONICAL)

    def test_a_non_default_lane_never_gets_the_registry_answer(self) -> None:
        decided = _decide_default(target_lane_id=TARGET_LANE)
        self.assertFalse(decided.ok)
        self.assertEqual(decided.reason, REFUSE_LANE_BINDING_ABSENT)

    def test_a_registry_with_no_canonical_keeps_the_refusal(self) -> None:
        decided = _decide_default(canonical_root="")
        self.assertFalse(decided.ok)
        self.assertEqual(decided.reason, REFUSE_LANE_BINDING_ABSENT)

    def test_a_linked_worktree_canonical_refuses(self) -> None:
        # The #13152 shape: a registry hijacked by a linked worktree must not answer.
        decided = _decide_default(liveness={**_LIVE_MAIN, "is_main_worktree": False})
        self.assertFalse(decided.ok)
        self.assertEqual(decided.reason, REFUSE_LANE_BINDING_ABSENT)

    def test_a_dead_or_non_git_canonical_refuses(self) -> None:
        for liveness in (
            None,
            {**_LIVE_MAIN, "exists": False},
            {**_LIVE_MAIN, "is_dir": False},
            {**_LIVE_MAIN, "is_git": False},
            {**_LIVE_MAIN, "is_main_worktree": None},
        ):
            decided = _decide_default(liveness=liveness)
            self.assertFalse(decided.ok, liveness)
            self.assertEqual(decided.reason, REFUSE_LANE_BINDING_ABSENT, liveness)

    def test_a_scope_mismatch_refuses(self) -> None:
        # A same-named registry row for a DIFFERENT repo must never answer this workspace.
        decided = _decide_default(scope_matches=False)
        self.assertFalse(decided.ok)
        self.assertEqual(decided.reason, REFUSE_LANE_BINDING_ABSENT)

    def test_no_default_lane_refusal_carries_a_root_or_a_path(self) -> None:
        refusals = [
            _decide_default(canonical_root=""),
            _decide_default(liveness=None),
            _decide_default(scope_matches=False),
            _decide_default(target_lane_id=TARGET_LANE),
        ]
        for decided in refusals:
            self.assertEqual(decided.root, "")
            self.assertFalse(decided.ok)
            self.assertTrue(decided.reason)
            self.assertNotIn("/registry", decided.detail)  # detail stays path-free


if __name__ == "__main__":
    unittest.main()
