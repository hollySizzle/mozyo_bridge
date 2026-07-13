"""Supervisor lease store tests (Redmine #13683 Phase A).

The lease is the **duplicate-supervisor fence**: a second supervisor that cannot acquire a
workspace's lease must deliver nothing. These tests probe the fence adversarially — not just the
happy grant, but the cases a weak lease silently passes: a still-live different holder (must be
REFUSED), an expired holder (must be TAKEN OVER), a non-owner release / renew after takeover (must
be REFUSED), and a foreign schema version (must fail closed, never rewrite).
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.supervisor_lease import (
    LEASE_GRANTED_FRESH,
    LEASE_GRANTED_SAME_HOLDER,
    LEASE_GRANTED_TAKEOVER,
    LEASE_REFUSED_HELD,
    SUPERVISOR_LEASE_SCHEMA_VERSION,
    SupervisorLease,
    SupervisorLeaseError,
    SupervisorLeaseStore,
)

T0 = "2026-07-13T00:00:00+00:00"


def _at(seconds: int) -> str:
    return f"2026-07-13T00:{seconds // 60:02d}:{seconds % 60:02d}+00:00"


class SupervisorLeaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = SupervisorLeaseStore(path=self.dir / "supervisor-lease.sqlite")

    def test_fresh_grant_and_read(self) -> None:
        r = self.store.acquire("wsA", "superX", now=T0, ttl_seconds=100)
        self.assertTrue(r.acquired)
        self.assertEqual(r.reason, LEASE_GRANTED_FRESH)
        held = self.store.holder_of("wsA")
        self.assertIsInstance(held, SupervisorLease)
        self.assertEqual(held.holder, "superX")
        self.assertEqual(held.expires_at, "2026-07-13T00:01:40+00:00")

    def test_duplicate_supervisor_is_refused_while_lease_is_live(self) -> None:
        self.store.acquire("wsA", "superX", now=T0, ttl_seconds=100)
        r = self.store.acquire("wsA", "superY", now=_at(10), ttl_seconds=100)
        self.assertFalse(r.acquired)
        self.assertEqual(r.reason, LEASE_REFUSED_HELD)
        self.assertEqual(r.holder, "superX")  # echoes the incumbent, not the loser
        # The refusal did not mutate the incumbent's lease.
        self.assertEqual(self.store.holder_of("wsA").holder, "superX")

    def test_same_holder_reacquire_is_idempotent_and_renews(self) -> None:
        self.store.acquire("wsA", "superX", now=T0, ttl_seconds=100)
        r = self.store.acquire("wsA", "superX", now=_at(30), ttl_seconds=100)
        self.assertTrue(r.acquired)
        self.assertEqual(r.reason, LEASE_GRANTED_SAME_HOLDER)
        held = self.store.holder_of("wsA")
        self.assertEqual(held.acquired_at, T0)  # original acquisition preserved
        self.assertEqual(held.expires_at, "2026-07-13T00:02:10+00:00")  # deadline advanced

    def test_expired_lease_is_taken_over_by_a_new_holder(self) -> None:
        self.store.acquire("wsA", "superX", now=T0, ttl_seconds=100)
        # now is strictly past the incumbent's expires_at (00:01:40) -> takeover.
        r = self.store.acquire("wsA", "superY", now="2026-07-13T01:00:00+00:00", ttl_seconds=100)
        self.assertTrue(r.acquired)
        self.assertEqual(r.reason, LEASE_GRANTED_TAKEOVER)
        self.assertEqual(self.store.holder_of("wsA").holder, "superY")

    def test_release_and_renew_are_owner_conditional_after_takeover(self) -> None:
        self.store.acquire("wsA", "superX", now=T0, ttl_seconds=100)
        self.store.acquire("wsA", "superY", now="2026-07-13T01:00:00+00:00", ttl_seconds=100)
        # The taken-over prior owner can neither release nor renew the new owner's lease.
        self.assertFalse(self.store.release("wsA", "superX"))
        self.assertFalse(self.store.renew("wsA", "superX", now="2026-07-13T01:00:05+00:00"))
        self.assertEqual(self.store.holder_of("wsA").holder, "superY")
        # The real owner can.
        self.assertTrue(self.store.renew("wsA", "superY", now="2026-07-13T01:00:05+00:00", ttl_seconds=100))
        self.assertTrue(self.store.release("wsA", "superY"))
        self.assertIsNone(self.store.holder_of("wsA"))

    def test_release_after_frees_the_workspace_for_next_acquire(self) -> None:
        self.store.acquire("wsA", "superX", now=T0, ttl_seconds=100)
        self.store.release("wsA", "superX")
        # A different holder acquires cleanly once released, even before the TTL would expire.
        r = self.store.acquire("wsA", "superY", now=_at(5), ttl_seconds=100)
        self.assertTrue(r.acquired)
        self.assertEqual(r.reason, LEASE_GRANTED_FRESH)

    def test_boundary_now_equal_to_expiry_is_a_takeover(self) -> None:
        self.store.acquire("wsA", "superX", now=T0, ttl_seconds=100)
        # expires_at is exactly 00:01:40; now == expiry is treated as expired (<=), so a takeover.
        r = self.store.acquire("wsA", "superY", now="2026-07-13T00:01:40+00:00", ttl_seconds=100)
        self.assertTrue(r.acquired)
        self.assertEqual(r.reason, LEASE_GRANTED_TAKEOVER)

    def test_blank_workspace_or_holder_is_rejected(self) -> None:
        with self.assertRaises(SupervisorLeaseError):
            self.store.acquire("", "superX", now=T0)
        with self.assertRaises(SupervisorLeaseError):
            self.store.acquire("wsA", "  ", now=T0)

    def test_absent_db_reads_empty(self) -> None:
        fresh = SupervisorLeaseStore(path=self.dir / "never-written.sqlite")
        self.assertIsNone(fresh.holder_of("wsA"))
        self.assertEqual(fresh.leases(), ())

    def test_foreign_schema_version_fails_closed(self) -> None:
        # Write the container at an unrecognized (future) version and confirm we never rewrite it.
        path = self.dir / "supervisor-lease.sqlite"
        self.store.acquire("wsA", "superX", now=T0)
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {SUPERVISOR_LEASE_SCHEMA_VERSION + 99}")
        conn.commit()
        conn.close()
        with self.assertRaises(SupervisorLeaseError):
            self.store.acquire("wsB", "superZ", now=T0)
        with self.assertRaises(SupervisorLeaseError):
            self.store.holder_of("wsA")


if __name__ == "__main__":
    unittest.main()
