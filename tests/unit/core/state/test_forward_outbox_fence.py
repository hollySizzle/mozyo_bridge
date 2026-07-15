"""Forward route+generation lifecycle store tests (Redmine #13583 R1-F1 correction).

Pins the correlated-generation authority (Design Answer j#76528): a route (target-name-free key)
holds exactly one active generation with an opaque ``forward_action_id``; a repeat while reserved /
delivered / uncertain is never-send; only the correlated callback (``complete`` / matching
``complete_by_correlation``) advances the exact delivered generation to ``completed``, after which
the next reserve mints a NEW id; a stale / mismatched / duplicate callback id never advances; a
target rename cannot advance (the target name is not in the key); and the DB-external sidecar fails a
deleted / replaced store closed (a lost store never re-sends).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.forward_outbox_fence import (
    FORWARD_ABSENT,
    FORWARD_COMPLETED,
    FORWARD_DELIVERED,
    FORWARD_RESERVED,
    FORWARD_UNCERTAIN,
    ForwardOutboxFence,
    ForwardOutboxFenceError,
    ForwardRouteKey,
)

GP = ("ws1", "default", "grandparent_coordinator", "project_gateway", "")


def _route(*, ws="ws1", lane="default", from_role="grandparent_coordinator",
           to_role="project_gateway", scope=""):
    return ForwardRouteKey(ws, lane, from_role, to_role, scope)


class GenerationLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.fence = ForwardOutboxFence(home=self.home)
        self.fence.bootstrap()

    def test_first_generation_reserve_mints_and_wins(self):
        r = self.fence.reserve(_route())
        self.assertTrue(r.won)
        self.assertTrue(r.action_id.startswith("fwd_"))
        self.assertEqual(r.current_state, FORWARD_RESERVED)

    def test_same_generation_repeat_is_never_send(self):
        route = _route()
        first = self.fence.reserve(route)
        self.assertTrue(first.won)
        # reserved re-entry (crash window) -> uncertain, never-send.
        second = self.fence.reserve(route)
        self.assertFalse(second.won)
        self.assertEqual(second.current_state, FORWARD_UNCERTAIN)

    def test_delivered_but_callback_pending_repeat_is_never_send(self):
        route = _route()
        r = self.fence.reserve(route)
        self.assertTrue(self.fence.mark_delivered(route, r.action_id))
        self.assertTrue(self.fence.is_active(route))
        again = self.fence.reserve(route)
        self.assertFalse(again.won)  # delivered generation blocks until the callback completes it

    def test_matching_callback_completes_then_next_generation_sends(self):
        route = _route()
        r1 = self.fence.reserve(route)
        self.fence.mark_delivered(route, r1.action_id)
        self.assertTrue(self.fence.complete(route, r1.action_id))
        self.assertEqual(self.fence.active(route).state, FORWARD_COMPLETED)
        r2 = self.fence.reserve(route)  # a NEW generation after completion
        self.assertTrue(r2.won)
        self.assertNotEqual(r2.action_id, r1.action_id)

    def test_complete_wrong_id_does_not_advance(self):
        route = _route()
        r = self.fence.reserve(route)
        self.fence.mark_delivered(route, r.action_id)
        self.assertFalse(self.fence.complete(route, "fwd_bogus"))
        self.assertEqual(self.fence.active(route).state, FORWARD_DELIVERED)

    def test_complete_only_advances_from_delivered(self):
        route = _route()
        r = self.fence.reserve(route)  # reserved, not yet delivered
        self.assertFalse(self.fence.complete(route, r.action_id))  # cannot complete a reserved gen

    def test_old_duplicate_callback_does_not_close_new_generation(self):
        route = _route()
        r1 = self.fence.reserve(route)
        self.fence.mark_delivered(route, r1.action_id)
        self.fence.complete(route, r1.action_id)
        r2 = self.fence.reserve(route)  # new generation, new id
        self.fence.mark_delivered(route, r2.action_id)
        # a duplicate of the OLD callback (old id) must not complete the NEW active generation.
        self.assertFalse(self.fence.complete(route, r1.action_id))
        self.assertEqual(self.fence.active(route).state, FORWARD_DELIVERED)
        self.assertEqual(self.fence.active(route).action_id, r2.action_id)

    def test_target_rename_cannot_advance_generation(self):
        # The route key has no target assigned name, so a target rename is the SAME route/generation.
        route = _route()
        r = self.fence.reserve(route)
        self.fence.mark_delivered(route, r.action_id)
        # "renaming the target" does not change the route key; the generation is still active.
        self.assertTrue(self.fence.is_active(route))
        self.assertFalse(self.fence.reserve(route).won)

    def test_distinct_routes_are_independent(self):
        a = _route(scope="")  # grandparent
        b = _route(from_role="project_gateway", to_role="delegated_coordinator", scope="alpha")
        self.assertTrue(self.fence.reserve(a).won)
        self.assertTrue(self.fence.reserve(b).won)


class CompleteByCorrelationTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.fence = ForwardOutboxFence(home=self.home)
        self.fence.bootstrap()

    def _delivered(self, route):
        r = self.fence.reserve(route)
        self.fence.mark_delivered(route, r.action_id)
        return r.action_id

    def test_correlated_callback_completes_delivered_generation(self):
        route = _route()
        aid = self._delivered(route)
        ok = self.fence.complete_by_correlation(
            aid, workspace_id="ws1", from_role="grandparent_coordinator"
        )
        self.assertTrue(ok)
        self.assertEqual(self.fence.active(route).state, FORWARD_COMPLETED)

    def test_route_drift_does_not_advance(self):
        route = _route()
        aid = self._delivered(route)
        # a callback echoing a valid id but a DRIFTED from_role contract must not complete.
        self.assertFalse(
            self.fence.complete_by_correlation(aid, workspace_id="ws1", from_role="project_gateway")
        )
        self.assertEqual(self.fence.active(route).state, FORWARD_DELIVERED)

    def test_stale_id_does_not_advance(self):
        route = _route()
        self._delivered(route)
        self.assertFalse(
            self.fence.complete_by_correlation(
                "fwd_stale", workspace_id="ws1", from_role="grandparent_coordinator"
            )
        )

    def test_empty_id_no_ops(self):
        self.assertFalse(
            self.fence.complete_by_correlation(
                "", workspace_id="ws1", from_role="grandparent_coordinator"
            )
        )


class StoreIdentityLossTest(unittest.TestCase):
    def test_reserve_without_bootstrap_fails_closed(self):
        fence = ForwardOutboxFence(home=Path(tempfile.mkdtemp()))
        with self.assertRaises(ForwardOutboxFenceError):
            fence.reserve(_route())

    def test_total_loss_after_delivered_fails_closed(self):
        # delivered -> delete BOTH DB and sidecar -> the store is not bootstrapped (no resurrection).
        home = Path(tempfile.mkdtemp())
        fence = ForwardOutboxFence(home=home)
        fence.bootstrap()
        route = _route()
        r = fence.reserve(route)
        fence.mark_delivered(route, r.action_id)
        fence.path.unlink()
        fence.sidecar_path.unlink()
        self.assertFalse(fence.is_bootstrapped())  # execution path must fail closed, not re-create
        with self.assertRaises(ForwardOutboxFenceError):
            fence.reserve(route)

    def test_deleted_db_with_sidecar_fails_closed(self):
        home = Path(tempfile.mkdtemp())
        fence = ForwardOutboxFence(home=home)
        fence.bootstrap()
        fence.path.unlink()  # DB lost, sidecar remains
        with self.assertRaises(ForwardOutboxFenceError):
            fence.reserve(_route())

    def test_active_of_unbootstrapped_is_absent(self):
        fence = ForwardOutboxFence(home=Path(tempfile.mkdtemp()))
        self.assertEqual(fence.active(_route()).state, FORWARD_ABSENT)

    def test_recover_mints_fresh_store(self):
        home = Path(tempfile.mkdtemp())
        fence = ForwardOutboxFence(home=home)
        fence.bootstrap()
        route = _route()
        r = fence.reserve(route)
        fence.mark_delivered(route, r.action_id)
        fence.recover()  # operator-gated fresh store
        self.assertEqual(fence.active(route).state, FORWARD_ABSENT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
