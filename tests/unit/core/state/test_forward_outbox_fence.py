"""Forward outbox fence tests (Redmine #13583 Increment 3).

Pins the dedicated at-most-once fence for herdr coordinator one-step forwards: a fresh key
reserves (won once), a duplicate never-sends, a crash-window re-entry surfaces uncertain (never
auto-retried), delivered / cancelled keys never re-send, and the DB-external sidecar fails a
deleted / replaced store closed (a lost store can never re-send a delivered forward). The key is
the forward's own anchor-free identity — no synthetic Redmine issue / journal.
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
    FORWARD_CANCELLED,
    FORWARD_DELIVERED,
    FORWARD_RESERVED,
    FORWARD_UNCERTAIN,
    ForwardFenceKey,
    ForwardOutboxFence,
    ForwardOutboxFenceError,
)


def _key(target="mzb1_ws1_codex_pgwv1_x", *, from_role="grandparent_coordinator",
         to_role="project_gateway", scope="", lane="default"):
    return ForwardFenceKey(
        workspace_id="ws1",
        from_lane_id=lane,
        from_role=from_role,
        to_role=to_role,
        project_scope=scope,
        target_assigned_name=target,
    )


class ForwardOutboxFenceTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.fence = ForwardOutboxFence(home=self.home)
        self.fence.bootstrap()

    def test_state_absent_before_any_reserve(self):
        self.assertEqual(self.fence.state_of(_key()), FORWARD_ABSENT)

    def test_fresh_key_wins_reserve(self):
        r = self.fence.reserve(_key())
        self.assertTrue(r.won)
        self.assertEqual(r.prior_state, FORWARD_ABSENT)
        self.assertEqual(r.current_state, FORWARD_RESERVED)

    def test_duplicate_reserve_crash_window_is_uncertain_not_retried(self):
        k = _key()
        self.assertTrue(self.fence.reserve(k).won)
        r2 = self.fence.reserve(k)
        self.assertFalse(r2.won)
        self.assertEqual(r2.prior_state, FORWARD_RESERVED)
        self.assertEqual(r2.current_state, FORWARD_UNCERTAIN)
        self.assertTrue(r2.needs_reconcile)

    def test_delivered_key_never_re_sends(self):
        k = _key()
        self.assertTrue(self.fence.reserve(k).won)
        self.assertTrue(self.fence.mark_delivered(k))
        self.assertEqual(self.fence.state_of(k), FORWARD_DELIVERED)
        r = self.fence.reserve(k)
        self.assertFalse(r.won)
        self.assertEqual(r.prior_state, FORWARD_DELIVERED)

    def test_cancelled_key_never_re_sends(self):
        k = _key()
        self.assertTrue(self.fence.reserve(k).won)
        self.assertTrue(self.fence.mark_cancelled(k))
        r = self.fence.reserve(k)
        self.assertFalse(r.won)
        self.assertEqual(r.prior_state, FORWARD_CANCELLED)

    def test_distinct_keys_are_independent(self):
        a = _key(target="mzb1_ws1_codex_a")
        b = _key(target="mzb1_ws1_codex_b")
        self.assertTrue(self.fence.reserve(a).won)
        self.assertTrue(self.fence.reserve(b).won)  # a different target is a different forward

    def test_child_intake_key_scope_is_part_of_identity(self):
        a = _key(from_role="project_gateway", to_role="delegated_coordinator", scope="alpha")
        b = _key(from_role="project_gateway", to_role="delegated_coordinator", scope="beta")
        self.assertTrue(self.fence.reserve(a).won)
        self.assertTrue(self.fence.reserve(b).won)

    def test_bootstrap_is_idempotent(self):
        self.fence.bootstrap()  # co-existing DB + sidecar at the same nonce -> no-op
        self.assertTrue(self.fence.is_bootstrapped())


class ForwardOutboxFenceStoreIdentityTest(unittest.TestCase):
    def test_reserve_without_bootstrap_fails_closed(self):
        home = Path(tempfile.mkdtemp())
        fence = ForwardOutboxFence(home=home)  # never bootstrapped
        with self.assertRaises(ForwardOutboxFenceError):
            fence.reserve(_key())

    def test_deleted_db_with_sidecar_fails_closed(self):
        home = Path(tempfile.mkdtemp())
        fence = ForwardOutboxFence(home=home)
        fence.bootstrap()
        fence.path.unlink()  # DB lost, sidecar remains: a store loss / replacement
        with self.assertRaises(ForwardOutboxFenceError):
            fence.reserve(_key())

    def test_state_of_unbootstrapped_is_absent_not_raising(self):
        home = Path(tempfile.mkdtemp())
        fence = ForwardOutboxFence(home=home)
        self.assertEqual(fence.state_of(_key()), FORWARD_ABSENT)

    def test_recover_mints_fresh_store(self):
        home = Path(tempfile.mkdtemp())
        fence = ForwardOutboxFence(home=home)
        fence.bootstrap()
        k = _key()
        fence.reserve(k)
        fence.mark_delivered(k)
        fence.recover()  # operator-gated fresh store
        self.assertEqual(fence.state_of(k), FORWARD_ABSENT)
        self.assertTrue(fence.reserve(k).won)  # a brand-new store; re-authorized upstream


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
