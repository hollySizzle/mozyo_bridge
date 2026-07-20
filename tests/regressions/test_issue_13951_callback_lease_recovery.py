"""Redmine #13951 — public callback-sweep lease recovery rail, pinned deterministically.

#13948's durable Implementation-Done recovery hit a ``callback-sweep-lease.sqlite`` / sidecar
inconsistency: the store's two-sided identity fence fail-closed (correctly — that zero-send is what
stops a duplicate owner), but there was no *public* way to diagnose or recover the state without raw
SQLite surgery, so the sublane→coordinator callback silently stopped.

This file pins the rail that replaces that: a read-only typed :meth:`diagnose`, a backup-first,
identity-bound, dry-run-default :meth:`recover_guarded`, and the actuating sweep's projection of the
inconsistency as an actionable typed blocker. Every scenario the issue enumerates — missing DB,
missing sidecar, nonce mismatch, live owner, dead owner, concurrent recovery, terminal replay — is a
deterministic case here, built from public API + file operations only (never raw store mutation of a
live lease). The #13948 live incident is read as canonical but reproduced from a *fixture*, not by
mutating a real store; the installed live dogfood is a separate post-review gate.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.core.state.callback_sweep_lease import (
    LEASE_ABSENT,
    LEASE_HEALTHY,
    LEASE_MISSING_DB,
    LEASE_MISSING_SIDECAR,
    LEASE_NONCE_MISMATCH,
    LEASE_UNREADABLE,
    RECOVERY_APPLIED,
    RECOVERY_PLANNED,
    RECOVERY_REFUSED_ABSENT,
    RECOVERY_REFUSED_CONCURRENT,
    RECOVERY_REFUSED_HEALTHY,
    RECOVERY_REFUSED_LIVE_OWNER,
    RECOVERY_REFUSED_UNBOUND,
    RECOVERY_REFUSED_UNREADABLE,
    CallbackSweepLease,
    LeaseKey,
)


def _bootstrapped(home=None):
    lease = CallbackSweepLease(home=home or Path(tempfile.mkdtemp()))
    lease.bootstrap()
    return lease


KEY = LeaseKey("ws-1", "lane-1", "13951", "anchor-a")


class DiagnoseStateTest(unittest.TestCase):
    """Each DB/sidecar state maps to exactly ONE typed diagnosis (the operator's read-only status)."""

    def test_healthy_pair_is_healthy_and_not_recoverable(self):
        d = _bootstrapped().diagnose()
        self.assertEqual(d.state, LEASE_HEALTHY)
        self.assertTrue(d.readable and d.nonce_matches and d.db_present and d.sidecar_present)
        self.assertFalse(d.recoverable)  # recovery is for a LOSS, not a healthy store
        self.assertFalse(d.has_live_owner)

    def test_absent_when_neither_artifact_exists(self):
        d = CallbackSweepLease(home=Path(tempfile.mkdtemp())).diagnose()
        self.assertEqual(d.state, LEASE_ABSENT)
        self.assertFalse(d.recoverable)  # use --bootstrap, not recovery

    def test_missing_db_with_surviving_sidecar_is_a_recoverable_clean_loss(self):
        lease = _bootstrapped()
        lease.path.unlink()
        d = lease.diagnose()
        self.assertEqual(d.state, LEASE_MISSING_DB)
        self.assertTrue(d.sidecar_present and not d.db_present)
        self.assertTrue(d.recoverable and not d.has_live_owner)

    def test_missing_sidecar_without_a_live_lease_is_recoverable(self):
        lease = _bootstrapped()
        lease.sidecar_path.unlink()
        d = lease.diagnose()
        self.assertEqual(d.state, LEASE_MISSING_SIDECAR)
        self.assertTrue(d.db_present and not d.sidecar_present)
        self.assertTrue(d.recoverable and not d.has_live_owner)

    def test_nonce_mismatch_is_a_replaced_store_and_recoverable(self):
        lease = _bootstrapped()
        lease.sidecar_path.write_text("deadbeefdeadbeef", encoding="utf-8")
        d = lease.diagnose()
        self.assertEqual(d.state, LEASE_NONCE_MISMATCH)
        # A grant under the DB nonce cannot pass an owner's store_nonce check, so no row is a
        # send-capable owner: a clean loss recovery may mint past it.
        self.assertFalse(d.has_live_owner)
        self.assertTrue(d.recoverable)

    def test_corrupt_db_is_unreadable_and_not_recoverable(self):
        lease = _bootstrapped()
        lease.path.write_bytes(b"this is not a sqlite database")
        d = lease.diagnose()
        self.assertEqual(d.state, LEASE_UNREADABLE)
        # A live owner cannot be ruled out on a store we cannot read -> restore, do not re-create.
        self.assertFalse(d.recoverable)

    def test_diagnosis_projection_leaks_no_path_or_token(self):
        lease = _bootstrapped()
        lease.acquire(KEY)  # a real owner token exists in the store
        payload = json.dumps(lease.diagnose().as_dict())
        self.assertNotIn("/", payload)  # no absolute path
        self.assertNotIn(str(lease.path), payload)
        # the owner token is 32 hex chars; the redaction-safe projection carries only a count
        owner = lease.owner_of(KEY)
        self.assertTrue(owner)
        self.assertNotIn(owner, payload)


class LiveOwnerRefusalTest(unittest.TestCase):
    """A live lease owner is the case recovery must NEVER mint past (it would strand the anchor)."""

    def test_healthy_store_with_a_live_owner_refuses_recovery_zero_write(self):
        lease = _bootstrapped()
        acq = lease.acquire(KEY)
        d = lease.diagnose()
        self.assertTrue(d.has_live_owner and d.live_lease_count == 1)
        out = lease.recover_guarded(expected_fingerprint=d.fingerprint, apply=True)
        self.assertEqual(out.status, RECOVERY_REFUSED_LIVE_OWNER)
        self.assertTrue(out.zero_write and not out.applied)
        # the live owner still owns its lease afterwards — nothing was minted
        self.assertTrue(lease.owns(KEY, acq.token, store_nonce=acq.store_nonce))

    def test_sidecar_lost_db_with_a_live_lease_refuses_conservatively(self):
        lease = _bootstrapped()
        lease.acquire(KEY, ttl_seconds=9999)
        lease.sidecar_path.unlink()
        d = lease.diagnose()
        self.assertEqual(d.state, LEASE_MISSING_SIDECAR)
        self.assertTrue(d.has_live_owner and not d.recoverable)
        out = lease.recover_guarded(expected_fingerprint=d.fingerprint, apply=True)
        self.assertEqual(out.status, RECOVERY_REFUSED_LIVE_OWNER)
        self.assertTrue(out.zero_write)


class DeadOwnerRecoverTest(unittest.TestCase):
    """A DEAD (expired) owner does not block recovery — the contrast with a live owner."""

    def test_expired_owner_on_a_sidecar_loss_is_recoverable_and_applies(self):
        lease = _bootstrapped()
        lease.acquire(KEY, ttl_seconds=0.01)
        time.sleep(0.05)  # the owner's lease lapses -> a dead owner
        lease.sidecar_path.unlink()
        d = lease.diagnose()
        self.assertEqual(d.state, LEASE_MISSING_SIDECAR)
        self.assertFalse(d.has_live_owner)  # expired -> not live
        self.assertTrue(d.recoverable)
        out = lease.recover_guarded(expected_fingerprint=d.fingerprint, apply=True)
        self.assertEqual(out.status, RECOVERY_APPLIED)
        self.assertEqual(lease.diagnose().state, LEASE_HEALTHY)


class BackupFirstAndDryRunTest(unittest.TestCase):
    """Recovery is dry-run by default and backs the prior artifacts up BEFORE it mints."""

    def test_dry_run_is_zero_write_and_reports_the_plan(self):
        lease = _bootstrapped()
        lease.path.unlink()
        before = lease.fingerprint()
        out = lease.recover_guarded(expected_fingerprint=before, apply=False)
        self.assertEqual(out.status, RECOVERY_PLANNED)
        self.assertTrue(out.zero_write)
        self.assertFalse(lease.path.exists())  # nothing was created
        self.assertEqual(lease.fingerprint(), before)

    def test_apply_backs_up_before_minting_and_leaves_the_store_healthy(self):
        lease = _bootstrapped()
        surviving_sidecar = lease.sidecar_path.read_bytes()
        lease.path.unlink()  # missing DB, sidecar survives
        d = lease.diagnose()
        out = lease.recover_guarded(expected_fingerprint=d.fingerprint, apply=True)
        self.assertEqual(out.status, RECOVERY_APPLIED)
        self.assertTrue(out.backups)  # the surviving sidecar was preserved first
        for name in out.backups:
            self.assertNotIn("/", name)  # basename only (redaction-safe)
        preserved = [p for p in lease.path.parent.iterdir() if "recovery-backup" in p.name]
        self.assertTrue(preserved)
        self.assertEqual(preserved[0].read_bytes(), surviving_sidecar)
        self.assertEqual(lease.diagnose().state, LEASE_HEALTHY)

    def test_absent_store_points_at_bootstrap_not_recovery(self):
        lease = CallbackSweepLease(home=Path(tempfile.mkdtemp()))
        d = lease.diagnose()
        out = lease.recover_guarded(expected_fingerprint=d.fingerprint, apply=True)
        self.assertEqual(out.status, RECOVERY_REFUSED_ABSENT)
        self.assertTrue(out.zero_write)

    def test_unreadable_store_refuses_apply_zero_write(self):
        lease = _bootstrapped()
        lease.path.write_bytes(b"corrupt")
        d = lease.diagnose()
        out = lease.recover_guarded(expected_fingerprint=d.fingerprint, apply=True)
        self.assertEqual(out.status, RECOVERY_REFUSED_UNREADABLE)
        self.assertTrue(out.zero_write)

    def test_apply_without_a_fingerprint_is_refused_as_unbound(self):
        lease = _bootstrapped()
        lease.path.unlink()
        out = lease.recover_guarded(expected_fingerprint="", apply=True)
        self.assertEqual(out.status, RECOVERY_REFUSED_UNBOUND)
        self.assertTrue(out.zero_write)


class ConcurrentMutationTest(unittest.TestCase):
    """A store that changed since the diagnosis its apply was bound to is zero-write (identity bind)."""

    def test_mutation_between_diagnose_and_apply_is_refused(self):
        lease = _bootstrapped()
        lease.path.unlink()
        d = lease.diagnose()
        # a concurrent process swaps the sidecar after the operator read the fingerprint
        lease.sidecar_path.write_text("a-different-store-nonce", encoding="utf-8")
        out = lease.recover_guarded(expected_fingerprint=d.fingerprint, apply=True)
        self.assertEqual(out.status, RECOVERY_REFUSED_CONCURRENT)
        self.assertTrue(out.zero_write)

    def test_concurrent_recovery_the_second_apply_finds_a_stale_fingerprint(self):
        # Two operators diagnose the SAME loss and both hold the same fingerprint. The first apply
        # mints (a new nonce -> new fingerprint); the second apply, still quoting the old
        # fingerprint, must zero-write rather than re-mint over the fresh store (which would
        # invalidate a NEW owner that acquired a lease after the first recovery).
        lease = _bootstrapped()
        lease.path.unlink()
        shared_fp = lease.diagnose().fingerprint
        first = lease.recover_guarded(expected_fingerprint=shared_fp, apply=True)
        self.assertEqual(first.status, RECOVERY_APPLIED)
        second = lease.recover_guarded(expected_fingerprint=shared_fp, apply=True)
        self.assertEqual(second.status, RECOVERY_REFUSED_CONCURRENT)
        self.assertTrue(second.zero_write)


class TerminalReplayTest(unittest.TestCase):
    """Replaying a completed recovery is idempotent and never re-mints / re-sends."""

    def test_replay_on_a_recovered_store_is_a_no_op(self):
        lease = _bootstrapped()
        lease.sidecar_path.unlink()  # missing sidecar, no live lease
        d = lease.diagnose()
        applied = lease.recover_guarded(expected_fingerprint=d.fingerprint, apply=True)
        self.assertEqual(applied.status, RECOVERY_APPLIED)
        healthy_fp = lease.diagnose().fingerprint
        self.assertNotEqual(healthy_fp, d.fingerprint)  # the store identity changed
        # Re-diagnosing the now-healthy store and re-applying is a terminal no-op: recovery is for a
        # loss, and there is none. Nothing is minted; recovery never sends a callback.
        replay = lease.recover_guarded(expected_fingerprint=healthy_fp, apply=True)
        self.assertEqual(replay.status, RECOVERY_REFUSED_HEALTHY)
        self.assertTrue(replay.zero_write)
        self.assertEqual(lease.diagnose().fingerprint, healthy_fp)  # unchanged by the replay


class SupervisorBlockerProjectionTest(unittest.TestCase):
    """An inconsistent lease store PROJECTS as an actionable typed blocker, not a silent stop (#3)."""

    def _args(self, **kw):
        base = dict(
            lease_recover=False, lease_bootstrap=False, lease_apply=False,
            lease_expect_fingerprint="", dispatch_delivered=False, stale_cli=False,
        )
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_blocker_is_typed_actionable_and_redaction_safe(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (  # noqa: E501
            CALLBACK_LEASE_INCONSISTENT,
            _callback_lease_blocker,
            format_callback_recovery_text,
        )

        lease = _bootstrapped()
        lease.path.unlink()  # a store loss
        blocker = _callback_lease_blocker(lease.diagnose(), self._args())
        self.assertEqual(blocker["state"], CALLBACK_LEASE_INCONSISTENT)
        self.assertTrue(blocker["is_stall"])  # a stall -> non-zero exit, never swallowed
        # the actionable recovery rail names the public command, not raw SQLite
        joined = " ".join(blocker["recovery"])
        self.assertIn("workflow callback-lease", joined)
        self.assertNotIn("sqlite3", joined.lower())
        # zero-send invariant is stated, and the projection carries no path/token
        self.assertTrue(any("zero-send" in inv for inv in blocker["invariants"]))
        self.assertNotIn("/", json.dumps(blocker["callback_lease_diagnosis"]))
        # the shared formatter renders it without KeyError (same shape as a callback verdict)
        self.assertIn(CALLBACK_LEASE_INCONSISTENT, format_callback_recovery_text(blocker))

    def test_execute_sweep_returns_the_blocker_instead_of_raising(self):
        # Wire the actuating sweep just far enough to reach the lease bootstrap on an inconsistent
        # store, and assert it returns a typed blocker (zero-send) rather than propagating the
        # fail-closed CallbackSweepLeaseError as an opaque crash.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_diagnostics,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (  # noqa: E501
            CALLBACK_LEASE_INCONSISTENT,
            _execute_sweep,
        )

        home = Path(tempfile.mkdtemp())
        lease = _bootstrapped(home)
        lease.path.unlink()  # store loss -> bootstrap() will fail closed

        args = self._args(
            issue="13951", lane="lane-1", lane_generation="g1", target="coordinator",
            journals_json=None, workspace_id="",
        )

        class _Source:
            @classmethod
            def from_environment(cls):
                return cls()

        class _Transport:
            def post_issue_note(self, *a, **k):  # pragma: no cover - never reached (zero-send)
                raise AssertionError("no send while the lease is inconsistent")

        with mock.patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}), \
                mock.patch(
                    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
                    "application.live_redmine_journal_source.LiveRedmineJournalSource", _Source), \
                mock.patch(
                    "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure."
                    "redmine_note_transport.redmine_delivery_transport_from_env",
                    lambda: _Transport()):
            result = _execute_sweep(args)

        self.assertEqual(result["state"], CALLBACK_LEASE_INCONSISTENT)
        self.assertTrue(result["is_stall"])
        self.assertIn("callback_lease_diagnosis", result)


if __name__ == "__main__":
    unittest.main()
