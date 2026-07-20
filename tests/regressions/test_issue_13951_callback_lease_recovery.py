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

    def test_execute_sweep_projects_the_blocker_to_the_durable_journal(self):
        # Wire the actuating sweep just far enough to reach the lease bootstrap on an inconsistent
        # store, and assert it PROJECTS the typed blocker onto the issue's durable journal (review
        # R1-F1) — not merely returns a payload — while never delivering a coordinator callback
        # (the recovery send path returns before it runs). The projection note is written via the
        # note transport; that is a durable projection, not a callback send.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (  # noqa: E501
            CALLBACK_LEASE_INCONSISTENT,
            PROJECTION_RECORDED,
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

            def read_entries(self, issue_id):
                return []  # no prior projection -> the write happens

        posted = []

        class _Transport:
            def post_issue_note(self, issue_id, notes):
                posted.append((issue_id, notes))
                return "90210"

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
        self.assertEqual(result["projection"], PROJECTION_RECORDED)
        self.assertEqual(result["projection_journal"], "90210")
        # exactly ONE journal write happened — the durable projection, not a coordinator callback
        self.assertEqual(len(posted), 1)
        posted_issue, posted_note = posted[0]
        self.assertEqual(posted_issue, "13951")
        # the durable note is bound to the exact dispatch round and is redaction-safe
        self.assertIn("issue=13951", posted_note)
        self.assertIn("lane=lane-1", posted_note)
        self.assertIn("generation=g1", posted_note)
        self.assertNotIn(str(home), posted_note)  # no absolute home path in the durable record


class DurableProjectionTest(unittest.TestCase):
    """The blocker is recorded to the durable journal idempotently, fail-closed on write (#13951 #3)."""

    def _args(self):
        return types.SimpleNamespace(
            issue="13951", lane="lane-9", lane_generation="g3",
            dispatch_delivered=False, stale_cli=False,
        )

    def _diag(self):
        lease = _bootstrapped()
        lease.path.unlink()  # a store loss -> inconsistent
        return lease.diagnose()

    class _Source:
        def __init__(self, entries=()):
            self._entries = list(entries)

        def read_entries(self, issue_id):
            return self._entries

    def test_recorded_when_no_prior_projection_exists(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (  # noqa: E501
            PROJECTION_RECORDED,
            _project_lease_blocker,
        )

        posted = []
        blk = _project_lease_blocker(
            self._diag(), self._args(), source=self._Source(),
            post_note=lambda i, n: (posted.append((i, n)), "j1")[1],
        )
        self.assertEqual(blk["projection"], PROJECTION_RECORDED)
        self.assertEqual(blk["projection_journal"], "j1")
        self.assertEqual(len(posted), 1)
        # the marker binds issue/lane/generation + the artifact fingerprint
        marker = blk["blocker_marker"]
        self.assertIn("issue=13951", marker)
        self.assertIn("lane=lane-9", marker)
        self.assertIn("generation=g3", marker)
        self.assertIn(blk["callback_lease_diagnosis"]["fingerprint"], marker)
        # redaction: the durable note carries no absolute path
        self.assertNotIn(str(Path.home()), posted[0][1])

    def test_duplicate_projection_is_an_idempotent_skip(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (  # noqa: E501
            PROJECTION_SKIPPED_DUPLICATE,
            _lease_blocker_marker,
            _callback_lease_blocker,
            _project_lease_blocker,
        )

        diag = self._diag()
        args = self._args()
        marker = _lease_blocker_marker(_callback_lease_blocker(diag, args), diag)
        prior = types.SimpleNamespace(notes=f"## Gate: Blocked\n{marker}\n...")
        posted = []
        blk = _project_lease_blocker(
            diag, args, source=self._Source([prior]),
            post_note=lambda i, n: (posted.append(1), "jX")[1],
        )
        self.assertEqual(blk["projection"], PROJECTION_SKIPPED_DUPLICATE)
        self.assertEqual(len(posted), 0)  # no duplicate journal

    def test_write_failure_is_fail_closed_and_retryable(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (  # noqa: E501
            PROJECTION_FAILED,
            _project_lease_blocker,
        )

        def boom(issue_id, notes):
            raise RuntimeError("redmine down")

        blk = _project_lease_blocker(
            self._diag(), self._args(), source=self._Source(), post_note=boom,
        )
        self.assertEqual(blk["projection"], PROJECTION_FAILED)
        self.assertEqual(blk["projection_error"], "RuntimeError")
        # still an actionable blocker (non-zero / is_stall) so the failure is not swallowed; the
        # marker was NOT recorded, so the next sweep re-attempts (retryable).
        self.assertTrue(blk["is_stall"])

    def test_unreadable_source_falls_open_to_projecting(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (  # noqa: E501
            PROJECTION_RECORDED,
            _project_lease_blocker,
        )

        class _Broken:
            def read_entries(self, issue_id):
                raise OSError("cannot read")

        posted = []
        blk = _project_lease_blocker(
            self._diag(), self._args(), source=_Broken(),
            post_note=lambda i, n: (posted.append(1), "jZ")[1],
        )
        # a read failure must not SUPPRESS a real blocker: it projects (at-least-once), never skips.
        self.assertEqual(blk["projection"], PROJECTION_RECORDED)
        self.assertEqual(len(posted), 1)


class ConcurrentRefusalZeroWriteTest(unittest.TestCase):
    """A concurrent-mutation refusal is truly zero net write — no backup file left behind (R1-F2)."""

    def _lost(self):
        lease = _bootstrapped()
        lease.path.unlink()  # recoverable clean loss
        return lease

    def _backup_files(self, lease):
        return [p for p in lease.path.parent.iterdir() if "recovery-backup" in p.name]

    def test_mutation_before_backup_writes_nothing(self):
        from mozyo_bridge.core.state.callback_sweep_lease import RECOVERY_REFUSED_CONCURRENT

        lease = self._lost()
        diag = lease.diagnose()
        lease.sidecar_path.write_text("changed-before-apply", encoding="utf-8")
        out = lease.recover_guarded(expected_fingerprint=diag.fingerprint, apply=True)
        self.assertEqual(out.status, RECOVERY_REFUSED_CONCURRENT)
        self.assertTrue(out.zero_write)
        self.assertFalse(self._backup_files(lease))  # nothing was backed up

    def test_mutation_during_backup_is_rolled_back_to_zero_write(self):
        from mozyo_bridge.core.state.callback_sweep_lease import RECOVERY_REFUSED_CONCURRENT

        lease = self._lost()
        diag = lease.diagnose()
        original = lease._backup_artifacts

        def mutate_mid_backup(recovery_id):
            result = original(recovery_id)
            lease.sidecar_path.write_text("mutated-during-backup", encoding="utf-8")
            return result

        lease._backup_artifacts = mutate_mid_backup
        out = lease.recover_guarded(expected_fingerprint=diag.fingerprint, apply=True)
        self.assertEqual(out.status, RECOVERY_REFUSED_CONCURRENT)
        # zero_write is honest: the backup this call created was rolled back.
        self.assertTrue(out.zero_write)
        self.assertFalse(self._backup_files(lease))

    def test_a_preexisting_backup_is_never_deleted_by_a_rollback(self):
        from mozyo_bridge.core.state.callback_sweep_lease import RECOVERY_REFUSED_CONCURRENT

        lease = self._lost()
        diag = lease.diagnose()
        # a prior attempt already left a forensic backup for this recovery id
        lease._backup_artifacts(diag.fingerprint)
        preexisting = self._backup_files(lease)
        self.assertTrue(preexisting)

        original = lease._backup_artifacts

        def mutate_mid_backup(recovery_id):
            result = original(recovery_id)  # reuses the pre-existing backup (creates nothing new)
            lease.sidecar_path.write_text("mutated-again", encoding="utf-8")
            return result

        lease._backup_artifacts = mutate_mid_backup
        out = lease.recover_guarded(expected_fingerprint=diag.fingerprint, apply=True)
        self.assertEqual(out.status, RECOVERY_REFUSED_CONCURRENT)
        # the rollback deletes only files THIS call created — the pre-existing forensic copy survives.
        self.assertTrue(self._backup_files(lease))

    def test_rollback_unlink_failure_is_reported_not_hidden(self):
        # Review R2: if the rollback's unlink FAILS, a backup residue remains on disk. The earlier
        # revision swallowed the error and still reported zero_write=True with no backups — hiding a
        # real write. The outcome must instead be a typed rollback_incomplete: zero_write=False, the
        # residue basenames named (redaction-safe), and a recovery action in the reason.
        from mozyo_bridge.core.state.callback_sweep_lease import RECOVERY_ROLLBACK_INCOMPLETE

        lease = self._lost()
        diag = lease.diagnose()
        original = lease._backup_artifacts

        def mutate_mid_backup(recovery_id):
            result = original(recovery_id)
            lease.sidecar_path.write_text("mutated-during-backup", encoding="utf-8")
            return result

        lease._backup_artifacts = mutate_mid_backup
        real_unlink = Path.unlink

        def failing_unlink(self, *a, **k):
            if "recovery-backup" in self.name:
                raise OSError("permission denied")
            return real_unlink(self, *a, **k)

        with mock.patch.object(Path, "unlink", failing_unlink):
            out = lease.recover_guarded(expected_fingerprint=diag.fingerprint, apply=True)

        self.assertEqual(out.status, RECOVERY_ROLLBACK_INCOMPLETE)
        self.assertFalse(out.zero_write)  # a residue remains — NOT zero-write
        self.assertTrue(out.residue)
        # the residue names match what is actually on disk, and are redaction-safe basenames
        on_disk = {p.name for p in self._backup_files(lease)}
        self.assertEqual(set(out.residue), on_disk)
        for name in out.residue:
            self.assertNotIn("/", name)
        self.assertNotIn("/", json.dumps(out.as_dict()["residue"]))
        # the operator is told to remove it by hand
        self.assertIn("residue", out.reason.lower())

    def test_rollback_failure_still_never_deletes_a_preexisting_forensic_backup(self):
        # The residue-reporting path must keep the R2 invariant: a rollback (even a failing one) only
        # ever targets THIS call's backups, never a pre-existing forensic copy.
        from mozyo_bridge.core.state.callback_sweep_lease import RECOVERY_ROLLBACK_INCOMPLETE

        lease = self._lost()
        diag = lease.diagnose()
        lease._backup_artifacts(diag.fingerprint)  # a prior forensic backup already exists
        preexisting = {p.name for p in self._backup_files(lease)}
        self.assertTrue(preexisting)

        original = lease._backup_artifacts

        def mutate_and_new_backup(recovery_id):
            # a fresh backup path this call creates (distinct id) + a mid-backup mutation
            self_lease = lease
            result = original(recovery_id)
            self_lease.sidecar_path.write_text("mutated-again", encoding="utf-8")
            return result

        # force a NEW created backup by unlinking the reused one first is unnecessary — instead make
        # unlink fail so any created copy becomes residue; the pre-existing copy must survive either way.
        real_unlink = Path.unlink

        def failing_unlink(self, *a, **k):
            if "recovery-backup" in self.name:
                raise OSError("permission denied")
            return real_unlink(self, *a, **k)

        lease._backup_artifacts = mutate_and_new_backup
        with mock.patch.object(Path, "unlink", failing_unlink):
            out = lease.recover_guarded(expected_fingerprint=diag.fingerprint, apply=True)

        # whatever the disposition, the pre-existing forensic backup is still on disk.
        surviving = {p.name for p in self._backup_files(lease)}
        self.assertTrue(preexisting.issubset(surviving))
        self.assertIn(out.status, (RECOVERY_ROLLBACK_INCOMPLETE, RECOVERY_REFUSED_CONCURRENT))


if __name__ == "__main__":
    unittest.main()
