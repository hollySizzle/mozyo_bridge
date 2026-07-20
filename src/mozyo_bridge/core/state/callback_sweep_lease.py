"""Owner-token attempt lease for the callback sweep (Redmine #13889 review R5-F1).

The sweep needs an authority it can **hold across slow I/O** — a Redmine read, a record write,
another read — so that a concurrent sweep cannot publish a duplicate record or race the send.

:class:`...dispatch_outbox_fence.DispatchOutboxFence` cannot be that authority, and the attempt to
make it one was rejected (R5-F1). Its contract assumes a reservation is **instantaneous**:
``reserve`` treats an existing ``reserved`` row as *crash residue* and transitions it to
``uncertain``. That is correct for its own callers, which reserve and send in one breath. But a
sweep that holds a reservation across seconds of I/O gets its live row rewritten to ``uncertain``
by any concurrent sweep — the owner can then no longer stand down, and the anchor is blocked
permanently. Adding a ``release`` did not fix that: ``state == reserved`` **does not prove the send
never happened**, because the row carries no owner identity.

So this is a separate, lane-local authority with the property the fence deliberately lacks:
**every row names its owner**. That single addition is what makes the difference:

- a **loser is passive** (``LEASE_HELD``): it observes that someone else owns the attempt and
  changes *nothing*, so an active owner is never corrupted and never mistaken for a crash;
- **release is owner-conditional**: only the holder can drop its own lease, so "stand down" is a
  claim only the party that knows it did not send can make;
- an **expired** lease is reclaimable, so a genuinely crashed owner does not block the anchor
  forever — the failure mode the fence avoids by declaring the crash window ``uncertain``, solved
  here by a deadline instead, because here a crashed owner has provably not sent (the send happens
  under the *fence*, after the lease work is done).

The division of labour is deliberate. This lease serializes the **attempt** (reads + record
publication). The dispatch outbox fence still serializes the **send**, in exactly the short,
native way its contract supports. Neither is asked to do the other's job.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

CALLBACK_SWEEP_LEASE_FILENAME = "callback-sweep-lease.sqlite"
CALLBACK_SWEEP_LEASE_SIDECAR_SUFFIX = ".anchor"
CALLBACK_SWEEP_LEASE_SCHEMA_VERSION = 1

#: This caller now owns the attempt and must release it when done.
LEASE_ACQUIRED = "acquired"
#: Another owner holds a live lease. The caller does NOTHING — it does not mutate the row, does not
#: reclassify the owner as crashed, and does not send. Passivity is the whole point (R5-F1).
LEASE_HELD = "held"
#: A previous owner's lease passed its deadline and was reclaimed by this caller.
LEASE_RECLAIMED = "reclaimed"

# --- Diagnosis states (Redmine #13951) ---------------------------------------
# A read-only classification of the store's DB/sidecar pair, used by the operator status surface and
# the recovery gate. Every state names exactly ONE of: healthy, a clean loss that recovery may mint
# past, or a state recovery must NOT touch (a live owner, or an unreadable store that could still
# hold one). The values are the public, redaction-safe vocabulary — no owner token, no absolute
# path, no raw row is ever part of a diagnosis.

#: DB + sidecar co-exist at the same nonce AND schema version. Nothing to recover.
LEASE_HEALTHY = "healthy"
#: Neither the DB nor the sidecar exists: never bootstrapped. Use ``bootstrap()``, not recovery.
LEASE_ABSENT = "absent"
#: The sidecar remains but the DB is gone. A clean loss: no DB means no readable owner row.
LEASE_MISSING_DB = "missing_db"
#: The DB remains but the sidecar is gone. The DB may still hold a live owner row, so this is only
#: recoverable when the DB reads clean AND holds no live lease.
LEASE_MISSING_SIDECAR = "missing_sidecar"
#: Both exist but the DB nonce (or schema version) disagrees with the sidecar: a replaced / foreign
#: store. A grant issued under the DB nonce cannot pass an owner's ``store_nonce`` check, so no row
#: is a send-capable owner; a clean loss recovery may mint past it.
LEASE_NONCE_MISMATCH = "nonce_mismatch"
#: The DB file exists but cannot be read (corrupt). Recovery must NOT mint past it: an unreadable
#: store could hold a live owner we cannot see. Restore from backup, do not re-create.
LEASE_UNREADABLE = "unreadable"

# --- Recovery outcomes (Redmine #13951) --------------------------------------
# The result vocabulary of :meth:`CallbackSweepLease.recover_guarded`. Exactly ONE status per call.
# ``RECOVERY_APPLIED`` mints a fresh store (a write). Every refusal is zero-write / zero-send EXCEPT
# ``RECOVERY_ROLLBACK_INCOMPLETE``, where a concurrent mutation was caught but this call's backup
# copies could not be removed, so a residue remains (``zero_write`` is then False). Read the outcome's
# ``zero_write`` — it is computed from what actually happened, not assumed from the status name.

#: A fresh store was minted under a new nonce, after the prior artifacts were backed up first.
RECOVERY_APPLIED = "applied"
#: Dry-run: the store is a recoverable clean loss and the fingerprint still matches, so an
#: ``--apply`` would mint. Nothing was written.
RECOVERY_PLANNED = "planned"
#: The store is already healthy: recovery is for a LOSS, not a re-mint of a live store. Zero-write.
RECOVERY_REFUSED_HEALTHY = "refused_healthy"
#: The store was never bootstrapped. Use ``bootstrap()`` (a first init), not recovery. Zero-write.
RECOVERY_REFUSED_ABSENT = "refused_absent"
#: A live lease owner is present (healthy store, or a sidecar-lost DB that still holds a live row).
#: Re-minting would invalidate that owner's grant and hand its anchor to a second owner. Zero-write.
RECOVERY_REFUSED_LIVE_OWNER = "refused_live_owner"
#: The DB is unreadable, so we cannot prove no live owner exists. Restore from backup. Zero-write.
RECOVERY_REFUSED_UNREADABLE = "refused_unreadable"
#: The artifacts changed since the diagnosis whose fingerprint the caller asserted: a concurrent
#: mutation (another process, or a replay against a stale fingerprint). Zero-write.
RECOVERY_REFUSED_CONCURRENT = "refused_concurrent_mutation"
#: ``--apply`` was requested without the ``expected_fingerprint`` that binds it to a diagnosis. An
#: unbound apply cannot detect a concurrent mutation, so it is refused. Zero-write.
RECOVERY_REFUSED_UNBOUND = "refused_unbound"
#: A concurrent mutation was caught DURING the backup, but rolling back this call's backup copies
#: failed (an ``unlink`` error), so a backup residue remains on disk. This is NOT zero-write — the
#: residue is reported (``residue``) with a recovery action so the operator can remove it. Swallowing
#: the cleanup error and reporting zero-write would hide a real write (review R2 #13951).
RECOVERY_ROLLBACK_INCOMPLETE = "rollback_incomplete"

#: Backup filename infix for the artifacts recovery preserves before minting a fresh store.
RECOVERY_BACKUP_INFIX = ".recovery-backup-"

#: How long an attempt may hold the lease before another sweep may reclaim it. Generous relative to
#: a sweep (a few Redmine round-trips) so a slow-but-live owner is never stolen from; short enough
#: that a crashed owner does not strand the anchor. A crashed owner cannot have sent: the send
#: happens under the outbox fence *after* the leased work.
DEFAULT_LEASE_TTL_SECONDS = 120.0

#: The attempt lease serializes the sweep's slow READS. It is intentionally reclaimable: a crashed
#: holder must not block the anchor forever.
#:
#: It is NOT the publication authority. An earlier revision tried to make it one -- first by moving
#: an ownership check closer to the write, then by requiring a safety margin -- and each was broken
#: (R7 / R8 / R9): a check and a remote write are never one transaction, and reclaim is exactly what
#: lets a second owner publish. Publication is a non-retryable outbox action and belongs to
#: :class:`...callback_publication_fence.CallbackPublicationFence`, which never reclaims.

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_STORE_NONCE_KEY = "store_nonce"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sweep_lease (
    workspace_id TEXT NOT NULL,
    lane_id      TEXT NOT NULL,
    issue        TEXT NOT NULL,
    anchor       TEXT NOT NULL,
    owner_token  TEXT NOT NULL,
    expires_at   REAL NOT NULL,
    acquired_at  REAL NOT NULL,
    UNIQUE(workspace_id, lane_id, issue, anchor)
)
"""


class CallbackSweepLeaseError(RuntimeError):
    """The lease store is unusable, so the attempt must not proceed (fail-closed)."""


@dataclass(frozen=True)
class LeaseKey:
    """The attempt identity: one lease per (workspace, lane, issue, dispatch anchor)."""

    workspace_id: str
    lane_id: str
    issue: str
    anchor: str

    def as_row(self) -> tuple[str, str, str, str]:
        return (self.workspace_id, self.lane_id, self.issue, self.anchor)


@dataclass(frozen=True)
class LeaseResult:
    """The outcome of an acquire. ``token`` is set only when this caller owns the attempt."""

    status: str
    token: str = ""
    detail: str = ""
    #: The store identity this grant was issued under (review R6-F2). An owner that later finds a
    #: DIFFERENT nonce is holding a lease from a store that no longer exists, so it must stand down.
    store_nonce: str = ""

    @property
    def owned(self) -> bool:
        return self.status in (LEASE_ACQUIRED, LEASE_RECLAIMED)


@dataclass(frozen=True)
class LeaseDiagnosis:
    """A read-only, redaction-safe classification of the lease store's DB/sidecar pair (#13951).

    Carries only booleans, counts, a typed ``state`` and an artifact ``fingerprint`` — never an
    owner token, a raw row, or an absolute path. ``fingerprint`` is the action-generation identity
    the recovery gate binds to: an ``--apply`` that quotes a fingerprint different from the store's
    current one is acting on a state that has since changed, and is refused.
    """

    state: str
    db_present: bool
    sidecar_present: bool
    #: True only when the DB and sidecar nonces (and schema version) agree; meaningless unless both
    #: artifacts are present and the DB is readable.
    nonce_matches: bool
    #: True when the DB opened and its nonce could be read; False for a missing or corrupt DB.
    readable: bool
    #: Non-expired lease rows the DB holds. 0 for a missing / unreadable DB.
    live_lease_count: int
    #: True when a non-expired row is one an owner could still act on: a healthy store, or a
    #: sidecar-lost DB (conservatively). A row under a mismatched nonce is NOT send-capable.
    has_live_owner: bool
    #: True when :meth:`CallbackSweepLease.recover_guarded` would mint past this state.
    recoverable: bool
    fingerprint: str
    reason: str
    recovery_action: str

    def as_dict(self) -> dict[str, object]:
        """The redaction-safe projection for a durable blocker / typed status (no path, no token)."""
        return {
            "state": self.state,
            "db_present": self.db_present,
            "sidecar_present": self.sidecar_present,
            "nonce_matches": self.nonce_matches,
            "readable": self.readable,
            "live_lease_count": self.live_lease_count,
            "has_live_owner": self.has_live_owner,
            "recoverable": self.recoverable,
            "fingerprint": self.fingerprint,
            "reason": self.reason,
            "recovery_action": self.recovery_action,
        }


@dataclass(frozen=True)
class LeaseRecoveryOutcome:
    """The outcome of a guarded recovery (#13951).

    ``RECOVERY_APPLIED`` wrote (a fresh store). Every refusal is zero-write EXCEPT
    ``RECOVERY_ROLLBACK_INCOMPLETE``, which leaves a backup residue on disk (a cleanup ``unlink``
    failed) — read :attr:`zero_write`, which reflects what actually happened rather than the status.
    """

    status: str
    #: The diagnosis the decision was made on (its fingerprint is the identity that was checked).
    diagnosis: LeaseDiagnosis
    #: Basenames (redaction-safe) of the artifacts backed up before a mint; empty unless applied.
    backups: tuple[str, ...]
    reason: str
    #: Basenames (redaction-safe) of backup copies THIS call created but could NOT remove after a
    #: concurrent-mutation refusal (a rollback ``unlink`` failed). Non-empty means a real write
    #: remains on disk, so the outcome is NOT zero-write — never a hidden residue (review R2 #13951).
    residue: tuple[str, ...] = ()

    @property
    def applied(self) -> bool:
        return self.status == RECOVERY_APPLIED

    @property
    def zero_write(self) -> bool:
        """True only when this outcome left the filesystem untouched.

        A mint (``RECOVERY_APPLIED``) writes, and a rollback that could not remove its own backup
        copies (``residue`` non-empty) also leaves a write behind — both are NOT zero-write. Every
        other refusal is genuinely write-0. This is computed from what actually happened, not from
        "non-applied ⇒ nothing written", which hid a backup residue on a cleanup failure (R2 #13951).
        """
        return self.status != RECOVERY_APPLIED and not self.residue

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "applied": self.applied,
            "zero_write": self.zero_write,
            "backups": list(self.backups),
            "residue": list(self.residue),
            "reason": self.reason,
            "diagnosis": self.diagnosis.as_dict(),
        }


def callback_sweep_lease_path(home: Optional[Path] = None) -> Path:
    return (home or mozyo_bridge_home()) / CALLBACK_SWEEP_LEASE_FILENAME


class CallbackSweepLease:
    """Owner-token attempt leases, serialized by ``BEGIN IMMEDIATE`` (home-scoped SQLite)."""

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path else callback_sweep_lease_path(home)

    @property
    def sidecar_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + CALLBACK_SWEEP_LEASE_SIDECAR_SUFFIX)

    def _read_sidecar_nonce(self) -> Optional[str]:
        try:
            value = self.sidecar_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    @staticmethod
    def _db_nonce(conn: sqlite3.Connection) -> Optional[str]:
        try:
            row = conn.execute(
                "SELECT value FROM store_meta WHERE key = ?", (_STORE_NONCE_KEY,)
            ).fetchone()
        except sqlite3.DatabaseError:
            return None
        return str(row[0]) if row else None

    def _create_fresh(self, nonce: str) -> None:
        """Create the DB and its sidecar together at ``nonce`` (the only creation path)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute(_TABLE_SQL)
            conn.execute(_META_TABLE_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                (_STORE_NONCE_KEY, nonce),
            )
            conn.execute(f"PRAGMA user_version = {CALLBACK_SWEEP_LEASE_SCHEMA_VERSION}")
        finally:
            conn.close()
        self.sidecar_path.write_text(nonce, encoding="utf-8")

    def is_bootstrapped(self) -> bool:
        """True when the DB and sidecar co-exist at the same nonce AND schema version (fail-soft).

        A genuine probe: opened ``mode=ro`` so the check cannot create, migrate, or otherwise touch
        the store it is inspecting. An earlier revision opened a read/write connection and executed
        DDL here while its docstring claimed "read-only" (review R8-F4) — a probe that mutates is
        not a probe. The schema version is verified too: a foreign / older store is not this store.
        """
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None or not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return False
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != CALLBACK_SWEEP_LEASE_SCHEMA_VERSION:
                return False
            return self._db_nonce(conn) == sidecar_nonce
        except (sqlite3.DatabaseError, TypeError, ValueError):
            return False
        finally:
            conn.close()

    def bootstrap(self) -> None:
        """Initial-only creation of the lease store + its DB-external identity (review R7-F2).

        Mirrors :meth:`...dispatch_outbox_fence.DispatchOutboxFence.bootstrap` exactly, because the
        hazard is exactly the same and an earlier revision copied only half of it:

        - **both** the DB and the sidecar absent (a genuine first bootstrap) -> mint a random
          ``store_nonce`` and create the DB + sidecar together at that nonce;
        - DB and sidecar co-exist at the same nonce -> idempotent no-op;
        - **any** other state -- sidecar present but DB missing, **DB present but the sidecar
          missing**, or a nonce mismatch -> **fail closed**.

        The sidecar-only-loss case is why "half the contract" is not a partial fix but a hole: the
        production composition root calls ``bootstrap()`` on EVERY ``--execute``, so silently
        re-minting a nonce onto a live DB invalidates the grant of an owner that is still working
        and hands the anchor to someone else. A single-sided store is a loss or a replacement, and
        it must go through the deliberate, operator-gated :meth:`recover` -- never a silent
        re-create.
        """
        sidecar_nonce = self._read_sidecar_nonce()
        db_exists = self.path.exists()
        if sidecar_nonce is None and not db_exists:
            self._create_fresh(secrets.token_hex(16))  # both absent: the only genuine first init
            return
        if self.is_bootstrapped():
            return  # DB + sidecar at the same nonce: already bootstrapped
        raise CallbackSweepLeaseError(
            f"callback sweep lease {self.path} is in an inconsistent state (only one of the DB / "
            f"sidecar exists, or their nonces differ): a store loss or replacement. Refusing to "
            f"silently re-create, which would invalidate a live owner's grant and hand the same "
            f"anchor to a second owner. Use recover() for a deliberate, operator-gated recovery."
        )

    def recover(self) -> None:
        """Deliberate, operator-gated loss recovery: mint a NEW nonce and a fresh store.

        The sanctioned way OUT of the fail-closed state :meth:`bootstrap` raises — a guard with no
        release path is a permanent stall, not a safety property. An operator invokes this after
        confirming no sweep is mid-attempt; every lingering grant is invalidated by the new nonce,
        which is the point.

        This is the low-level, UNCONDITIONAL primitive. The operator-facing recovery is
        :meth:`recover_guarded`, which backs the artifacts up first, refuses on a live owner / an
        unreadable store / a concurrent mutation, and binds to a diagnosis fingerprint. This method
        stays for that guarded path to call once every gate has passed.
        """
        self._create_fresh(secrets.token_hex(16))

    # -- read-only diagnosis + guarded recovery (Redmine #13951) -----------

    def _read_sidecar_bytes(self) -> Optional[bytes]:
        try:
            return self.sidecar_path.read_bytes()
        except OSError:
            return None

    def fingerprint(self) -> str:
        """A content-addressed identity over BOTH artifacts — the recovery action-generation token.

        Deterministic in the artifacts' bytes (no clock, no path), so a diagnosis and a later
        ``--apply`` that quote the same fingerprint prove the store did not change between them. Any
        write to the DB or the sidecar (a fresh lease, a re-mint, a partial swap) changes it, which
        is what lets :meth:`recover_guarded` detect a concurrent mutation and zero-write.
        """
        digest = hashlib.sha256()
        sidecar = self._read_sidecar_bytes()
        digest.update(b"sidecar:")
        digest.update(b"\x00absent" if sidecar is None else sidecar)
        digest.update(b"|db:")
        if self.path.exists():
            data = self.path.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(hashlib.sha256(data).digest())
        else:
            digest.update(b"\x00absent")
        return digest.hexdigest()[:32]

    def _probe_db(self, stamp: float) -> tuple[bool, bool, Optional[str], int]:
        """Read-only probe of an existing DB: ``(readable, version_ok, db_nonce, live_count)``.

        Opened ``mode=ro`` so inspecting the store cannot create, migrate, or otherwise touch it (the
        same discipline :meth:`is_bootstrapped` keeps). A corrupt DB reads as ``(False, ...)`` — an
        unreadable store is diagnosed, never assumed empty.
        """
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return (False, False, None, 0)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            version_ok = version == CALLBACK_SWEEP_LEASE_SCHEMA_VERSION
            db_nonce = self._db_nonce(conn)
            try:
                live = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM sweep_lease WHERE expires_at > ?", (stamp,)
                    ).fetchone()[0]
                )
            except sqlite3.DatabaseError:
                # The identity meta may exist without the lease table (a half-written store): a
                # readable DB with no countable rows, not a corruption.
                live = 0
            return (True, version_ok, db_nonce, live)
        except (sqlite3.DatabaseError, TypeError, ValueError):
            return (False, False, None, 0)
        finally:
            conn.close()

    def diagnose(self, *, now: Optional[float] = None) -> LeaseDiagnosis:
        """Classify the DB/sidecar pair for the operator status surface and the recovery gate.

        Pure read-only: opens the DB ``mode=ro`` and never writes. Returns a redaction-safe
        :class:`LeaseDiagnosis` — a typed state, the DB/sidecar presence, nonce agreement, the live
        lease count, whether a live owner is present, whether recovery may mint past the state, and
        the artifact ``fingerprint``. No owner token, raw row, or absolute path is exposed.
        """
        stamp = float(now if now is not None else time.time())
        sidecar_nonce = self._read_sidecar_nonce()
        sidecar_present = sidecar_nonce is not None
        db_present = self.path.exists()
        fp = self.fingerprint()

        def build(state, *, readable, nonce_matches, live, has_live_owner, recoverable, reason, action):
            return LeaseDiagnosis(
                state=state, db_present=db_present, sidecar_present=sidecar_present,
                nonce_matches=nonce_matches, readable=readable, live_lease_count=live,
                has_live_owner=has_live_owner, recoverable=recoverable, fingerprint=fp,
                reason=reason, recovery_action=action,
            )

        if not db_present and not sidecar_present:
            return build(
                LEASE_ABSENT, readable=False, nonce_matches=False, live=0,
                has_live_owner=False, recoverable=False,
                reason="neither the DB nor the sidecar exists (never bootstrapped)",
                action="run `--bootstrap` (a first init), not recovery",
            )
        if sidecar_present and not db_present:
            # Clean loss: no DB means no readable owner row could exist. Recoverable.
            return build(
                LEASE_MISSING_DB, readable=False, nonce_matches=False, live=0,
                has_live_owner=False, recoverable=True,
                reason="the sidecar remains but the DB is gone (store loss)",
                action="recover: backup-first mint past the lost DB",
            )
        # DB is present past here.
        readable, version_ok, db_nonce, live = self._probe_db(stamp)
        if not readable:
            return build(
                LEASE_UNREADABLE, readable=False, nonce_matches=False, live=0,
                has_live_owner=False, recoverable=False,
                reason="the DB exists but cannot be read (corrupt); a live owner cannot be ruled out",
                action="restore the store from backup — do NOT re-create it",
            )
        if not sidecar_present:
            # DB present, sidecar gone: the DB may hold a live owner. Conservatively treat a
            # non-expired row as a live owner (zero-write); only a clean, ownerless DB is recoverable.
            has_live = live > 0
            return build(
                LEASE_MISSING_SIDECAR, readable=True, nonce_matches=False, live=live,
                has_live_owner=has_live, recoverable=not has_live,
                reason=(
                    "the DB remains but the sidecar is gone; the DB still holds a live lease"
                    if has_live else
                    "the DB remains but the sidecar is gone (store loss); no live lease"
                ),
                action=(
                    "wait for the live lease to expire or confirm the owner is gone, then recover"
                    if has_live else "recover: backup-first mint past the lost sidecar"
                ),
            )
        if not version_ok or db_nonce != sidecar_nonce:
            # Replaced / foreign store: a grant under this DB nonce fails an owner's store_nonce
            # check, so no row is a send-capable owner. A clean loss recovery may mint past it.
            return build(
                LEASE_NONCE_MISMATCH, readable=True, nonce_matches=False, live=live,
                has_live_owner=False, recoverable=True,
                reason=(
                    "the DB schema version is foreign to this store"
                    if not version_ok else
                    "the DB nonce disagrees with the sidecar (replaced / recreated store)"
                ),
                action="recover: backup-first mint a fresh identity past the mismatch",
            )
        # Healthy: DB + sidecar agree. A non-expired row is a genuine live owner.
        has_live = live > 0
        return build(
            LEASE_HEALTHY, readable=True, nonce_matches=True, live=live,
            has_live_owner=has_live, recoverable=False,
            reason=(
                f"healthy; {live} live lease(s) held" if has_live else "healthy; no live lease"
            ),
            action="no recovery needed" if not has_live else (
                "no recovery: a live owner holds a lease — recovery would strand its anchor"
            ),
        )

    def _backup_artifacts(self, recovery_id: str) -> tuple[tuple[str, ...], tuple[Path, ...]]:
        """Copy the existing DB + sidecar to backup files BEFORE a mint. Idempotent, redaction-safe.

        Returns ``(names, newly_created)`` — the backup basenames (never absolute paths) for the
        outcome, and the full paths this call actually created (so a caller can roll them back if a
        later gate refuses). An existing backup for the same recovery id is never clobbered, so a
        replayed apply reuses it rather than overwriting the forensic copy — and reused files are NOT
        in ``newly_created`` (rolling back must never delete a pre-existing forensic copy).
        """
        infix = f"{RECOVERY_BACKUP_INFIX}{recovery_id[:16]}"
        names: list[str] = []
        created: list[Path] = []
        for src in (self.path, self.sidecar_path):
            if src.exists():
                dst = src.with_name(src.name + infix)
                if not dst.exists():
                    dst.write_bytes(src.read_bytes())
                    created.append(dst)
                names.append(dst.name)
        return tuple(names), tuple(created)

    def recover_guarded(
        self, *, expected_fingerprint: str = "", apply: bool = False, now: Optional[float] = None,
    ) -> LeaseRecoveryOutcome:
        """Operator-gated, backup-first, identity-bound loss recovery (Redmine #13951).

        Default is read-only / dry-run (``apply=False``): it classifies the store and reports what an
        ``--apply`` WOULD do, writing nothing. Every one of the following is zero-write / zero-send:

        - a **live owner** (a healthy store with a live lease, or a sidecar-lost DB that still holds
          one) — re-minting would invalidate a live grant and hand its anchor to a second owner;
        - an **unreadable** DB — a live owner cannot be ruled out, so the store must be restored,
          not re-created;
        - a **concurrent mutation** — the artifacts changed since the diagnosis whose
          ``expected_fingerprint`` the caller quoted (another process, or a replay against a stale
          fingerprint), so the action no longer matches the state it was authorized for;
        - an already **healthy** or never-bootstrapped store — recovery is for a loss, not a re-mint.

        Only a clean loss (missing DB, missing sidecar with no live lease, or a nonce/schema
        mismatch) is minted past, and only under ``apply=True`` with a matching
        ``expected_fingerprint``: the artifacts are backed up first, the fingerprint is re-checked
        immediately before the mint (catching a mutation during the backup), and only then is a fresh
        store created. Replaying an apply is idempotent: once recovered the store is healthy under a
        new fingerprint, so the stale ``expected_fingerprint`` no longer matches and the replay
        zero-writes. Recovery never sends a callback — it only mints the store.
        """
        expected = str(expected_fingerprint or "").strip()
        diagnosis = self.diagnose(now=now)

        def outcome(status, reason, backups=()):
            return LeaseRecoveryOutcome(
                status=status, diagnosis=diagnosis, backups=tuple(backups), reason=reason,
            )

        # Identity / action-generation bind + concurrent-mutation gate: if the caller asserted a
        # fingerprint and the store no longer matches it, the state changed under them — zero-write.
        if expected and diagnosis.fingerprint != expected:
            return outcome(
                RECOVERY_REFUSED_CONCURRENT,
                "the store changed since the diagnosis this apply was bound to "
                "(concurrent mutation or a replay against a stale fingerprint); zero-write",
            )
        if diagnosis.state == LEASE_HEALTHY:
            return outcome(
                RECOVERY_REFUSED_LIVE_OWNER if diagnosis.has_live_owner else RECOVERY_REFUSED_HEALTHY,
                "a live owner holds a lease; recovery would strand its anchor"
                if diagnosis.has_live_owner else "the store is already healthy; nothing to recover",
            )
        if diagnosis.state == LEASE_ABSENT:
            return outcome(RECOVERY_REFUSED_ABSENT, "never bootstrapped; run `--bootstrap` instead")
        if diagnosis.state == LEASE_UNREADABLE:
            return outcome(
                RECOVERY_REFUSED_UNREADABLE,
                "the DB is unreadable; a live owner cannot be ruled out — restore from backup",
            )
        if diagnosis.has_live_owner:
            return outcome(
                RECOVERY_REFUSED_LIVE_OWNER,
                "the DB still holds a live lease; wait for expiry or confirm the owner is gone",
            )
        # A recoverable clean loss from here (missing DB / sidecar-no-live / nonce mismatch).
        if not apply:
            return outcome(
                RECOVERY_PLANNED,
                "recoverable clean loss; re-run with `--apply` (and this fingerprint) to mint past it",
            )
        if not expected:
            # An unbound apply cannot detect a concurrent mutation, so it is refused: recovery is
            # deliberately bound to a diagnosis the operator read.
            return outcome(
                RECOVERY_REFUSED_UNBOUND,
                "an apply must quote the fingerprint from a prior diagnosis so a concurrent "
                "mutation is detectable; re-run status and pass its fingerprint",
            )
        # Re-read the LIVE fingerprint immediately BEFORE any write (review R1-F2 #13951). The entry
        # gate above compared the caller's asserted fingerprint against the diagnosis; this catches a
        # mutation between that diagnosis and now with ZERO side-effect — no backup is written, so a
        # refusal here is genuinely write-0 (the earlier revision backed up first and then reported
        # ``zero_write=True`` while a backup file remained on disk — a false claim).
        if self.fingerprint() != diagnosis.fingerprint:
            return outcome(
                RECOVERY_REFUSED_CONCURRENT,
                "the store changed since the diagnosis this apply was bound to (concurrent "
                "mutation); zero-write, nothing backed up",
            )
        names, created = self._backup_artifacts(diagnosis.fingerprint)
        # Re-check once more: a mutation DURING the backup means the artifacts changed under us. Roll
        # back the backups THIS call created (never a pre-existing forensic copy) so the refusal is
        # truly zero net write, then refuse.
        if self.fingerprint() != diagnosis.fingerprint:
            residue: list[str] = []
            for path in created:
                try:
                    path.unlink()
                except OSError:
                    # Cleanup failed: the backup copy remains on disk. Do NOT swallow this and claim
                    # zero-write (review R2 #13951) — record the residue so it is reported honestly.
                    residue.append(path.name)
            if residue:
                return LeaseRecoveryOutcome(
                    status=RECOVERY_ROLLBACK_INCOMPLETE,
                    diagnosis=diagnosis,
                    backups=(),
                    reason=(
                        "the store changed while backing it up (concurrent mutation) and this "
                        "call's backup copies could not be removed — a backup residue remains on "
                        "disk (NOT zero-write). Remove the listed residue file(s) by hand, then "
                        "re-run status before retrying recovery"
                    ),
                    residue=tuple(residue),
                )
            return outcome(
                RECOVERY_REFUSED_CONCURRENT,
                "the store changed while backing it up (concurrent mutation); rolled back this "
                "call's backups — zero net write",
            )
        self._create_fresh(secrets.token_hex(16))
        return outcome(
            RECOVERY_APPLIED,
            "backed the prior artifacts up and minted a fresh store under a new nonce; every "
            "outstanding grant is now invalid",
            backups=names,
        )

    def _connect(self) -> sqlite3.Connection:
        """Open an EXISTING, identity-matched store, or fail closed (mirrors the outbox fence).

        Never creates the container. A deleted / replaced / recreated store is the #13889 R6-F2
        split-brain: the old owner keeps its grant against a store that is gone while a new process
        hands the same anchor to somebody else. The DB and its DB-external sidecar must co-exist at
        the SAME nonce; a missing file, an empty swap-in, or a nonce mismatch all fail closed.
        """
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None:
            raise CallbackSweepLeaseError(
                f"callback sweep lease {self.path} has no identity sidecar (never bootstrapped / "
                f"lost); fail closed rather than run an unserialized attempt"
            )
        if not self.path.exists():
            raise CallbackSweepLeaseError(
                f"callback sweep lease {self.path} is missing while its sidecar remains (store "
                f"loss); fail closed rather than auto-create and hand out a duplicate lease"
            )
        try:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute(_TABLE_SQL)
            conn.execute(_META_TABLE_SQL)
            if self._db_nonce(conn) != sidecar_nonce:
                conn.close()
                raise CallbackSweepLeaseError(
                    f"callback sweep lease {self.path} nonce does not match its sidecar "
                    f"(replaced / recreated store); fail closed"
                )
            return conn
        except CallbackSweepLeaseError:
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            raise CallbackSweepLeaseError(
                f"callback sweep lease store {self.path} is unusable ({type(exc).__name__}); "
                f"fail closed rather than run an unserialized attempt"
            ) from exc

    def acquire(
        self, key: LeaseKey, *, ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
        now: Optional[float] = None,
    ) -> LeaseResult:
        """Take the attempt lease, or report that a live owner holds it (passively).

        A live owner's row is **never** mutated by a loser — the defect that made the shared fence
        unusable here (R5-F1). An expired row is reclaimed with a fresh token, which also
        invalidates the dead owner's release.
        """
        stamp = float(now if now is not None else time.time())
        token = secrets.token_hex(16)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_token, expires_at FROM sweep_lease WHERE workspace_id=? AND "
                "lane_id=? AND issue=? AND anchor=?",
                key.as_row(),
            ).fetchone()
            if row is not None and float(row[1]) > stamp:
                conn.execute("ROLLBACK")
                return LeaseResult(
                    status=LEASE_HELD,
                    detail="another sweep owns this attempt; standing down without touching it",
                )
            reclaimed = row is not None
            conn.execute(
                "INSERT INTO sweep_lease (workspace_id, lane_id, issue, anchor, owner_token, "
                "expires_at, acquired_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, lane_id, issue, anchor) DO UPDATE SET "
                "owner_token=excluded.owner_token, expires_at=excluded.expires_at, "
                "acquired_at=excluded.acquired_at",
                (*key.as_row(), token, stamp + float(ttl_seconds), stamp),
            )
            nonce = self._db_nonce(conn) or ""
            conn.execute("COMMIT")
            return LeaseResult(
                status=LEASE_RECLAIMED if reclaimed else LEASE_ACQUIRED,
                token=token,
                store_nonce=nonce,
                detail="reclaimed an expired lease" if reclaimed else "acquired a fresh lease",
            )
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise CallbackSweepLeaseError(
                f"callback sweep lease acquire failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def release(self, key: LeaseKey, token: str) -> bool:
        """Drop the lease, but only if ``token`` still owns it. Returns True when dropped.

        Owner-conditional by construction: a caller that has been superseded (its lease expired and
        was reclaimed) cannot delete the new owner's lease.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM sweep_lease WHERE workspace_id=? AND lane_id=? AND issue=? AND "
                "anchor=? AND owner_token=?",
                (*key.as_row(), str(token)),
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise CallbackSweepLeaseError(
                f"callback sweep lease release failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def owns(
        self,
        key: LeaseKey,
        token: str,
        *,
        store_nonce: str = "",
        min_remaining: float = 0.0,
    ) -> bool:
        """True only if ``token`` STILL owns a live lease on ``key`` in the SAME store (R6-F1/F2).

        The check a caller must make immediately before every durable publication and before the
        send. Acquiring is not enough: the TTL can expire while the owner is merely *slow* rather
        than dead -- a few Redmine round-trips is all it takes -- and a new owner then reclaims the
        anchor. Both would publish, producing exactly the duplicate durable record this issue
        exists to remove. "The owner is dead so it cannot have sent" only ever covered the SEND
        (which the outbox fence gates); it never covered the publication, which only the lease
        gates.

        ``min_remaining`` lets a caller ask for headroom before a bounded local step. It is NOT a
        publication guard: no margin can make a check and a remote write atomic (review R9-F1), and
        treating one as safety is what allowed a suspended owner to publish a duplicate. The
        publication fence is the authority for writes.

        Fail-closed by construction: an unreadable / lost / replaced store raises out of
        :meth:`_connect`, an expired (or too-nearly-expired) lease reads as not-owned, and a
        mismatched ``store_nonce`` means the grant came from a store that no longer exists.
        """
        conn = self._connect()
        try:
            if store_nonce and self._db_nonce(conn) != str(store_nonce):
                return False
            row = conn.execute(
                "SELECT owner_token, expires_at FROM sweep_lease WHERE workspace_id=? AND "
                "lane_id=? AND issue=? AND anchor=?",
                key.as_row(),
            ).fetchone()
        finally:
            conn.close()
        if row is None or float(row[1]) <= time.time() + float(min_remaining):
            return False
        return str(row[0]) == str(token)

    def owner_of(self, key: LeaseKey) -> str:
        """The current owner token, or ``""`` when unheld / expired (read-only, for tests+debug)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT owner_token, expires_at FROM sweep_lease WHERE workspace_id=? AND "
                "lane_id=? AND issue=? AND anchor=?",
                key.as_row(),
            ).fetchone()
        finally:
            conn.close()
        if row is None or float(row[1]) <= time.time():
            return ""
        return str(row[0])


__all__ = (
    "CALLBACK_SWEEP_LEASE_FILENAME",
    "LEASE_ACQUIRED",
    "LEASE_HELD",
    "LEASE_RECLAIMED",
    "DEFAULT_LEASE_TTL_SECONDS",
    "CallbackSweepLeaseError",
    "LeaseKey",
    "LeaseResult",
    "CallbackSweepLease",
    "callback_sweep_lease_path",
    "CALLBACK_SWEEP_LEASE_SIDECAR_SUFFIX",
    # Diagnosis (#13951)
    "LEASE_HEALTHY",
    "LEASE_ABSENT",
    "LEASE_MISSING_DB",
    "LEASE_MISSING_SIDECAR",
    "LEASE_NONCE_MISMATCH",
    "LEASE_UNREADABLE",
    "LeaseDiagnosis",
    # Guarded recovery (#13951)
    "RECOVERY_APPLIED",
    "RECOVERY_PLANNED",
    "RECOVERY_REFUSED_HEALTHY",
    "RECOVERY_REFUSED_ABSENT",
    "RECOVERY_REFUSED_LIVE_OWNER",
    "RECOVERY_REFUSED_UNREADABLE",
    "RECOVERY_REFUSED_CONCURRENT",
    "RECOVERY_REFUSED_UNBOUND",
    "RECOVERY_ROLLBACK_INCOMPLETE",
    "RECOVERY_BACKUP_INFIX",
    "LeaseRecoveryOutcome",
)
