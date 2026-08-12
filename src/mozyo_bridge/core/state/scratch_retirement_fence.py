"""Retirement transaction fence for record-less scratch pairs (Redmine #13892, design j#80526).

A ``herdr session-start`` scratch pair owns no lane lifecycle row, so the public retire rail
(``herdr session-retire``) has no authority to prove the two things it must prove:

1. **replay** — is a zero-slot observation "I already retired this pair" or "this pair never
   existed here"? Absence cannot tell them apart, so a mistyped ``--lane`` read as a
   successful retirement (review j#80506 F1);
2. **completion** — a durable outcome whose write failure makes the command non-success and
   which the next run repairs (review j#80506 F2).

This component is that authority. Per design answer j#80526 (Option A-prime) it is
deliberately **none of** the existing state kinds:

- not **workflow truth** — Redmine gates / owner approval stay outside the DB;
- not **desired-state history** — ``managed_events`` is ``append_only_lossy``, its charter
  forbids completion truth, and its contract says an append failure must not break the caller
  (the exact opposite of what F2 requires);
- not **lifecycle authority** — no lane lifecycle row is minted (#13892 acceptance 4; the
  "declare a row so the existing retire accepts it" route was rejected by #13882 j#80066).

It is an **operational action-idempotency / side-effect transaction authority**.

Why not reuse :mod:`...dispatch_outbox_fence` (j#80526): that fence's ``reserved`` semantics
assume an *instantaneous* reserve around a single send. This transaction **holds** across an
external side effect (closing panes) and must be resumable after a crash, which its states and
table were never designed for. The pattern is borrowed; the store is not.

**Why an OS advisory lock and not only ``BEGIN IMMEDIATE``** (j#80526): SQLite's write lock
lives only for the duration of a transaction. This authority must stay exclusive across the
*close* — an external process operation between the reserve and the completion — so a second
caller cannot enter the same unit mid-flight. An exclusive **non-blocking** ``flock`` held for
the whole transaction provides that, and the kernel releases it if the process dies, which is
what makes a crashed attempt resumable rather than permanently stuck.

**How this differs from the #13842 isolated-ledger anti-pattern** (removed at j#79346 R5): that
ledger could be lost while its lane still had live panes, stranding them forever with no way to
converge. Here a ``pending`` row is resumed under a held / crash-released OS lock, and losing a
``completed`` row never fabricates success — it withholds it. If the store is lost after the
panes are already gone, nothing leaks: the panes are closed, and the surface merely declines to
claim a retirement it can no longer prove. The residual is the same local total-loss
indistinguishability every sibling authority carries; nothing more is claimed.
"""

from __future__ import annotations

import errno
import fcntl
import os
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.scratch_retirement_pin import (
    ScratchRetirementPin,
    ScratchRetirementPinError,
    decode_scratch_retirement_pin_projection,
    encode_scratch_retirement_pins,
)
from mozyo_bridge.core.state.scratch_retirement_migration import (
    ScratchRetirementMigrationError,
    migrate_scratch_retirement_v1_locked,
)
from mozyo_bridge.core.state.scratch_retirement_store_security import (
    ScratchRetirementStoreSecurityError,
    classify_artifacts,
    primary_artifact_paths,
    primary_security_snapshot,
)
from mozyo_bridge.core.state.scratch_retirement_attempt_codec import (
    ScratchRetirementAttemptCodecError,
    decode_attempt_detail as _decode_attempt_detail,
    decode_close_pairs as _decode_pairs,
    encode_attempt_detail as _encode_attempt_detail,
    encode_close_pairs as _encode_pairs,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

SCRATCH_RETIREMENT_FENCE_FILENAME = "scratch-retirement-fence.sqlite"
#: The DB-external identity artifact (the store's nonce).
SCRATCH_RETIREMENT_FENCE_SEAL_SUFFIX = ".seal"
#: The advisory-lock file. Separate from the DB so the lock survives a DB replacement and so
#: taking it never creates or touches the authority itself.
SCRATCH_RETIREMENT_FENCE_LOCK_SUFFIX = ".lock"
#: The bootstrap staging artifact. A bootstrap builds here and renames into place, so a crash
#: mid-write leaves THIS behind rather than a half-built authority at the real path.
SCRATCH_RETIREMENT_FENCE_TEMP_SUFFIX = ".tmp"
SCRATCH_RETIREMENT_FENCE_SCHEMA_VERSION = 2

#: A retirement is authorized and in flight: its close may be partially done and its fate is
#: not yet proven. A crash leaves this row, and that is what makes the flow resumable.
RETIRE_PENDING = "pending"
#: The retirement is proven: the pinned panes closed AND the whole unit re-measured empty.
#: The only state that proves an idempotent replay.
RETIRE_COMPLETED = "completed"
#: Sentinel: no attempt exists for the unit.
RETIRE_ABSENT = "absent"

RETIRE_STATES = frozenset({RETIRE_PENDING, RETIRE_COMPLETED})

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS scratch_retirement (
    workspace_id   TEXT NOT NULL,
    lane_id        TEXT NOT NULL,
    slot_digest    TEXT NOT NULL,
    attempt_id     TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    state          TEXT NOT NULL,
    pinned_json    TEXT NOT NULL DEFAULT '',
    closed_json    TEXT NOT NULL DEFAULT '',
    detail         TEXT NOT NULL DEFAULT '',
    reserved_at    TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(workspace_id, lane_id, slot_digest)
)
"""

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_STORE_NONCE_KEY = "store_nonce"


class ScratchRetirementFenceError(RuntimeError):
    """The retirement authority is unavailable / unprovable (fail-closed).

    The caller treats this as "do not close, and do not claim a retirement": the replay
    authority cannot be trusted, so a close could neither be proven nor resumed.
    """


class ScratchRetirementBusy(ScratchRetirementFenceError):
    """Another caller holds this unit's transaction. Zero-close; never wait, never steal."""


def _norm_locator(value: str) -> str:
    return (value or "").strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scratch_retirement_fence_path(home: Optional[Path] = None) -> Path:
    return (home or mozyo_bridge_home()) / SCRATCH_RETIREMENT_FENCE_FILENAME


def slot_digest(assigned_names: Sequence[str]) -> str:
    """A canonical, **order-independent** fingerprint of the exact expected slot set (pure).

    The unit identity is ``workspace_id`` + ``lane_id`` + this exact set (j#80526), so a
    retirement proves something about *those* panes and not merely about a lane label.
    Sorting makes a gateway-first and a worker-first caller agree; de-duplication stops a
    repeated name forging a different unit. Empty raises — an unnamed retirement could never
    identify one exact pair.
    """
    names = sorted({n.strip() for n in assigned_names if n and n.strip()})
    if not names:
        raise ValueError(
            "a scratch retirement digest requires at least one expected assigned name"
        )
    import hashlib

    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RetirementUnit:
    """The unit a retirement transaction is scoped to."""

    workspace_id: str
    lane_id: str
    slot_digest: str

    def as_row(self) -> tuple[str, str, str]:
        return (self.workspace_id, self.lane_id, self.slot_digest)


@dataclass(frozen=True)
class RetirementAttempt:
    """One durable retirement attempt over a unit."""

    unit: RetirementUnit
    state: str
    attempt_id: str
    revision: int = 1
    pinned: tuple[tuple[str, str], ...] = ()
    pin_version: int = 0
    generation_pins: tuple[ScratchRetirementPin, ...] = field(
        default_factory=tuple, repr=False
    )
    closed: tuple[tuple[str, str], ...] = ()
    #: Canonical, caller-verified destructive-approval evidence.
    approval_evidence: str = ""
    detail: str = ""
    reserved_at: str = ""
    updated_at: str = ""

    @property
    def pending(self) -> bool:
        return self.state == RETIRE_PENDING

    @property
    def completed(self) -> bool:
        return self.state == RETIRE_COMPLETED

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.unit.workspace_id,
            "lane_id": self.unit.lane_id,
            "slot_digest": self.unit.slot_digest,
            "state": self.state,
            "attempt_id": self.attempt_id,
            "revision": self.revision,
            "pinned": [{"role": r, "locator": loc} for r, loc in self.pinned],
            "pin_version": self.pin_version,
            "closed": [{"role": r, "locator": loc} for r, loc in self.closed],
            "approval_evidence": self.approval_evidence,
            "detail": self.detail,
            "reserved_at": self.reserved_at,
            "updated_at": self.updated_at,
        }


#: Store artifact shapes (j#80526: absent / readable / unreadable must never be collapsed).
STORE_ABSENT = "absent"  # every artifact absent: nothing was ever operated here
STORE_PRESENT = "present"  # row-bearing artifacts present; identity verified on connect
STORE_DAMAGED = "damaged"  # a partial / orphaned artifact set: something WAS here


@dataclass(frozen=True)
class StoreShape:
    """The tri-state artifact inventory of the store."""

    state: str
    present_artifacts: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    @property
    def absent(self) -> bool:
        return self.state == STORE_ABSENT


class ScratchRetirementFence:
    """The home-scoped retirement transaction authority.

    Construction never touches the filesystem. The normal flow is a single
    :meth:`transaction` that holds an exclusive advisory lock across
    read → reserve → close → re-measure → complete.
    """

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = path or scratch_retirement_fence_path(home)

    @property
    def seal_path(self) -> Path:
        return self.path.with_name(self.path.name + SCRATCH_RETIREMENT_FENCE_SEAL_SUFFIX)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + SCRATCH_RETIREMENT_FENCE_LOCK_SUFFIX)

    @property
    def temp_path(self) -> Path:
        """The bootstrap staging file. Present only while a bootstrap is in flight (or died)."""
        return self.path.with_name(self.path.name + SCRATCH_RETIREMENT_FENCE_TEMP_SUFFIX)

    # -- artifact inventory ------------------------------------------------

    def store_shape(self) -> StoreShape:
        """Classify primary plus migration evidence without collapsing damage to absence."""
        state, present, detail = classify_artifacts(
            self.path, self.seal_path, self.temp_path
        )
        return StoreShape(state=state, present_artifacts=present, detail=detail)

    # -- store identity ----------------------------------------------------

    def _read_seal_nonce(self) -> Optional[str]:
        try:
            value = self.seal_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
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
        return str(row[0]) if row is not None else None

    def _connect_ro(self) -> sqlite3.Connection:
        """A strict read-only connection over a present, identity-matched store."""
        identity = self._primary_security_snapshot()
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise ScratchRetirementFenceError(
                f"the retirement authority {self.path} is unreadable ({exc}); fail closed "
                "rather than treat an unreadable store as an empty one"
            ) from exc
        self._recheck_primary_security(identity, conn)
        return self._verify_identity(conn, version, writable=False)

    def _connect_rw(self) -> sqlite3.Connection:
        identity = self._primary_security_snapshot()
        try:
            conn = sqlite3.connect(
                f"file:{self.path}?mode=rw", uri=True, isolation_level=None
            )
            conn.execute("PRAGMA busy_timeout = 2000")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise ScratchRetirementFenceError(
                f"the retirement authority {self.path} is unreadable ({exc}); fail closed"
            ) from exc
        self._recheck_primary_security(identity, conn)
        return self._verify_identity(conn, version, writable=True)

    def _primary_security_snapshot(self) -> dict[str, tuple[int, int, int]]:
        try:
            return primary_security_snapshot(
                self.path, self.seal_path, self.temp_path
            )
        except ScratchRetirementStoreSecurityError as exc:
            raise ScratchRetirementFenceError(str(exc)) from exc

    def _recheck_primary_security(
        self, identity: dict[str, tuple[int, int, int]], conn: sqlite3.Connection
    ) -> None:
        try:
            current = self._primary_security_snapshot()
        except ScratchRetirementFenceError:
            conn.close()
            raise
        if current != identity:
            conn.close()
            raise ScratchRetirementFenceError(
                "the retirement authority artifact identity changed while it was opened"
            )

    def _verify_identity(
        self, conn: sqlite3.Connection, version: int, *, writable: bool
    ):
        try:
            if version not in (1, SCRATCH_RETIREMENT_FENCE_SCHEMA_VERSION) or (
                writable and version != SCRATCH_RETIREMENT_FENCE_SCHEMA_VERSION
            ):
                raise ScratchRetirementFenceError(
                    f"the retirement authority {self.path} is at schema version {version}, "
                    f"not {SCRATCH_RETIREMENT_FENCE_SCHEMA_VERSION}; an unknown schema is "
                    "never read past nor rewritten"
                )
            seal = self._read_seal_nonce()
            if seal is None:
                raise ScratchRetirementFenceError(
                    f"the retirement authority {self.path} has no identity seal; its prior "
                    "retirements cannot be trusted"
                )
            if self._db_nonce(conn) != seal:
                raise ScratchRetirementFenceError(
                    f"the retirement authority {self.path} does not match its identity seal "
                    "(store replacement); fail closed"
                )
        except ScratchRetirementFenceError:
            conn.close()
            raise
        return conn

    # -- the held transaction ---------------------------------------------

    def peek(self, unit: RetirementUnit) -> Optional[RetirementAttempt]:
        """Observe the unit's attempt WITHOUT writing anything at all. (review j#80523 R3-F4)

        The read-only preflight must leave the authority byte-identical: it takes no lock (even
        ``open(O_CREAT)`` on the lock file is an artifact write), creates no DB and no seal.
        A ``--execute``-less run that bootstrapped the store would both contradict its own
        "closes nothing and writes nothing" contract and, worse, silently re-create an
        authority that was *lost* — erasing the evidence of prior retirements.

        ``None`` = no attempt (including a genuinely absent store: truthfully "nothing was
        recorded here"). A damaged / unreadable store raises — an unobservable authority is
        never an empty one.
        """
        shape = self.store_shape()
        if shape.state == STORE_DAMAGED:
            raise ScratchRetirementFenceError(
                shape.detail
                or "the retirement authority's artifacts are incomplete; fail closed"
            )
        if shape.absent:
            return None
        conn = self._connect_ro()
        try:
            row = conn.execute(
                "SELECT state, attempt_id, revision, pinned_json, closed_json, detail, "
                "reserved_at, updated_at FROM scratch_retirement "
                "WHERE workspace_id=? AND lane_id=? AND slot_digest=?",
                unit.as_row(),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_attempt(unit, row) if row is not None else None

    def blocking_attempt_for_target(
        self,
        *,
        workspace_id: str,
        lane_id: str,
        target_assigned_name: str,
        live_locator: str = "",
        live_generation_token: Optional[str] = None,
    ) -> Optional[RetirementAttempt]:
        """Read-only send guard: pending blocks; v2 completed binds the current token."""
        shape = self.store_shape()
        if shape.absent:
            return None
        if shape.state == STORE_DAMAGED:
            raise ScratchRetirementFenceError(
                shape.detail
                or "the retirement authority's artifacts are incomplete; fail closed"
            )
        conn = self._connect_ro()
        try:
            rows = conn.execute(
                "SELECT state, attempt_id, revision, pinned_json, closed_json, detail, "
                "reserved_at, updated_at, slot_digest FROM scratch_retirement "
                "WHERE workspace_id=? AND lane_id=?",
                (workspace_id, lane_id),
            ).fetchall()
        finally:
            conn.close()
        want = (target_assigned_name or "").strip()
        for row in rows:
            unit = RetirementUnit(workspace_id, lane_id, str(row[8]))
            attempt = _row_to_attempt(unit, row)
            current_pin = next(
                (
                    pin
                    for pin in attempt.generation_pins
                    if pin.assigned_name == want
                ),
                None,
            )
            if current_pin is not None:
                if attempt.pending:
                    return attempt
                if live_generation_token is None:
                    return attempt
                return (
                    attempt
                    if current_pin.locator == live_locator
                    and current_pin.startup_action_id == live_generation_token
                    else None
                )
            # `pinned` holds (role, locator); the assigned name is rebuilt from the unit's
            # identity, so match on the encoded name for each pinned role.
            for role, locator in attempt.pinned:
                try:
                    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
                        encode_assigned_name,
                    )

                    if encode_assigned_name(workspace_id, role, lane_id) != want:
                        continue
                except Exception:  # noqa: BLE001 - an unencodable role cannot match
                    continue
                if attempt.pending:
                    return attempt
                # completed: only the pane this attempt actually closed is off limits.
                if not live_locator:
                    return attempt  # cannot compare -> ambiguous -> fail closed
                if _norm_locator(live_locator) == _norm_locator(locator):
                    return attempt  # a stale dispatch to the pane we closed
                # A different locator at the same name: a relaunched pair. The old completion
                # has no authority over it, and blocking would strand every future dispatch.
                return None
        return None

    def transaction(self, unit: RetirementUnit, *, live_pair_present: bool):
        """Open the non-blocking exclusive retirement transaction for one unit."""
        return _RetirementTransaction(self, unit, live_pair_present=live_pair_present)

    def migrate_v1_backup_first(self) -> int:
        """Explicitly migrate exact v1 after publishing a durable logical backup."""
        lock = self.lock_path
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ScratchRetirementBusy(
                        "another retirement transaction holds the authority"
                    ) from exc
                raise
            primary = {
                name for name, path in primary_artifact_paths(
                    self.path, self.seal_path, self.temp_path
                ) if os.path.lexists(path)
            }
            if (
                not {"db", "seal"}.issubset(primary)
                or "temp" in primary
            ):
                raise ScratchRetirementFenceError(
                    "the retirement authority is absent or damaged; migration refused"
                )
            try:
                return migrate_scratch_retirement_v1_locked(self.path, self.seal_path)
            except (ScratchRetirementMigrationError, OSError, sqlite3.DatabaseError) as exc:
                raise ScratchRetirementFenceError(
                    "the retirement authority could not be migrated backup-first"
                ) from exc
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _create_fresh(self, nonce: str) -> None:
        """Build the store in a temp and rename it into place, then seal it.

        Staging through ``temp`` means a crash mid-build leaves the temp — which the artifact
        inventory sees, so the next caller reports DAMAGED instead of finding a plausible but
        half-built authority at the real path (review j#80523 R3-F5).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.temp_path
        if temp.exists():
            temp.unlink()
        conn = sqlite3.connect(temp, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute(_TABLE_SQL)
            conn.execute(_META_TABLE_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                (_STORE_NONCE_KEY, nonce),
            )
            conn.execute(f"PRAGMA user_version = {SCRATCH_RETIREMENT_FENCE_SCHEMA_VERSION}")
        finally:
            conn.close()
        os.chmod(temp, 0o600)
        self._bootstrap_hook("built_temp")
        os.replace(temp, self.path)
        self._bootstrap_hook("renamed")
        # The seal is written LAST: an interrupted bootstrap then leaves a db-without-seal,
        # which `store_shape` reports as DAMAGED (fail-closed) rather than as a healthy store.
        self.seal_path.write_text(nonce, encoding="utf-8")
        os.chmod(self.seal_path, 0o600)

    def _bootstrap_hook(self, stage: str) -> None:
        """Crash-injection seam: tests raise here to drive REAL interrupted-bootstrap boundaries.

        Without it an "interrupted bootstrap" test can only delete artifacts after a SUCCESSFUL
        bootstrap, which is a different shape and pins nothing about the write path itself
        (review j#80523 R3-F5).
        """

    def status(self) -> dict:
        """Operator-visible status (doctor surface). Never raises; never mutates."""
        shape = self.store_shape()
        out: dict = {
            "path": str(self.path),
            "store_state": shape.state,
            "present_artifacts": list(shape.present_artifacts),
            "detail": shape.detail,
            "attempts": None,
            "readable": None,
        }
        if shape.absent:
            out["readable"] = True  # provably nothing recorded
            out["attempts"] = 0
            return out
        try:
            conn = self._connect_ro()
        except ScratchRetirementFenceError as exc:
            out["readable"] = False
            out["detail"] = out["detail"] or str(exc)
            return out
        try:
            rows = conn.execute(
                "SELECT state, COUNT(*) FROM scratch_retirement GROUP BY state"
            ).fetchall()
            out["readable"] = True
            out["attempts"] = {str(r[0]): int(r[1]) for r in rows}
        except sqlite3.DatabaseError as exc:
            out["readable"] = False
            out["detail"] = str(exc)
        finally:
            conn.close()
        return out


def _row_to_attempt(unit: "RetirementUnit", row) -> "RetirementAttempt":
    try:
        approval_evidence, detail = _decode_attempt_detail(row[5])
        projection = decode_scratch_retirement_pin_projection(row[3])
        closed = _decode_pairs(row[4])
    except (ScratchRetirementPinError, ScratchRetirementAttemptCodecError) as exc:
        raise ScratchRetirementFenceError(
            "the retirement attempt's private generation pins are unreadable; fail closed"
        ) from exc
    public_pins = (
        tuple((pin.role, pin.locator) for pin in projection.pins)
        if projection.current_authority
        else projection.legacy_pairs
    )
    return RetirementAttempt(
        unit=unit,
        state=str(row[0]),
        attempt_id=str(row[1]),
        revision=int(row[2]),
        pinned=public_pins,
        pin_version=projection.version,
        generation_pins=projection.pins,
        closed=closed,
        approval_evidence=approval_evidence,
        detail=detail,
        reserved_at=str(row[6] or ""),
        updated_at=str(row[7] or ""),
    )
class _RetirementTransaction:
    """The held, exclusive retirement transaction (see :meth:`ScratchRetirementFence.transaction`)."""

    def __init__(
        self,
        fence: ScratchRetirementFence,
        unit: RetirementUnit,
        *,
        live_pair_present: bool,
    ) -> None:
        self._fence = fence
        self._unit = unit
        self._live = live_pair_present
        self._fd: Optional[int] = None
        self._bootstrapped = False
        self._store_absent = False

    def __enter__(self) -> "_RetirementTransaction":
        lock = self._fence.lock_path
        lock.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ScratchRetirementBusy(
                    "another retirement transaction holds this unit; refusing to wait or "
                    "steal the authority — nothing was closed"
                ) from exc
            raise ScratchRetirementFenceError(
                f"could not take the retirement transaction lock ({exc}); fail closed"
            ) from exc
        try:
            self._enter_locked()
        except BaseException:
            # Never leak the lock (or its fd) when the post-lock setup fails: a leaked
            # exclusive lock would make every later retire of this unit report `busy`.
            self._release()
            raise
        return self

    def _enter_locked(self) -> None:
        # Bootstrap decision happens under the lock, so two first-callers serialize.
        shape = self._fence.store_shape()
        if shape.state == STORE_DAMAGED:
            raise ScratchRetirementFenceError(
                shape.detail
                or "the retirement authority's artifacts are incomplete; fail closed"
            )
        if shape.absent:
            if not self._live:
                # A zero-slot run must NEVER create the authority (j#80526): re-creating a
                # lost store here would erase the evidence of prior retirements and let this
                # run claim a fresh, empty world. It is not an error though — an absent store
                # is a truthful "nothing was ever retired here", which is exactly the answer a
                # mistyped --lane deserves. `current()` reports no attempt and the caller
                # returns `retire_evidence_absent`.
                self._store_absent = True
                return
            # A true first bootstrap: a live exact pair is positively present AND every
            # authority artifact is absent, decided under this lock so two first-callers
            # serialize rather than both creating a store.
            self._fence._create_fresh(secrets.token_hex(16))
            self._bootstrapped = True

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._release()
        return False

    def _release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    @property
    def bootstrapped(self) -> bool:
        return self._bootstrapped

    def current(self) -> Optional[RetirementAttempt]:
        """The unit's active attempt, or ``None``. Fails closed on a damaged store.

        An absent store returns ``None`` — a truthful "no attempt was ever recorded here",
        which is NOT an authority failure. Damage is a different thing and raises.
        """
        if self._store_absent:
            return None
        conn = self._fence._connect_ro()
        try:
            row = conn.execute(
                "SELECT state, attempt_id, revision, pinned_json, closed_json, detail, "
                "reserved_at, updated_at FROM scratch_retirement "
                "WHERE workspace_id=? AND lane_id=? AND slot_digest=?",
                self._unit.as_row(),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_attempt(self._unit, row) if row is not None else None

    def reserve(
        self,
        *,
        pinned: Sequence[ScratchRetirementPin],
        approval_evidence: str = "",
        now: Optional[str] = None,
    ) -> RetirementAttempt:
        """Authorize a retirement for this unit BEFORE any close.

        Reserving first is what makes a crash recoverable: a closed pair can never be left
        with no durable evidence that this command did it (review j#80506 F2).

        A ``completed`` attempt followed by newly live slots opens a **new attempt** at the
        next revision (j#80526): herdr assigned names are deterministic, so a relaunched pair
        can occupy the exact same names — an old completion must never prove the retirement of
        a pair that is running now.
        """
        stamp = now or _utc_now()
        stored_detail = _encode_attempt_detail(
            approval_evidence=approval_evidence, detail=""
        )
        conn = self._fence._connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, revision FROM scratch_retirement "
                "WHERE workspace_id=? AND lane_id=? AND slot_digest=?",
                self._unit.as_row(),
            ).fetchone()
            attempt = secrets.token_hex(8)
            if row is None:
                revision = 1
                conn.execute(
                    "INSERT INTO scratch_retirement (workspace_id, lane_id, slot_digest, "
                    "attempt_id, revision, state, pinned_json, closed_json, detail, "
                    "reserved_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
                    (
                        *self._unit.as_row(),
                        attempt,
                        revision,
                        RETIRE_PENDING,
                        encode_scratch_retirement_pins(pinned),
                        stored_detail,
                        stamp,
                        stamp,
                    ),
                )
            else:
                revision = int(row[1]) + 1
                conn.execute(
                    "UPDATE scratch_retirement SET attempt_id=?, revision=?, state=?, "
                    "pinned_json=?, closed_json='', detail=?, reserved_at=?, updated_at=? "
                    "WHERE workspace_id=? AND lane_id=? AND slot_digest=?",
                    (
                        attempt,
                        revision,
                        RETIRE_PENDING,
                        encode_scratch_retirement_pins(pinned),
                        stored_detail,
                        stamp,
                        stamp,
                        *self._unit.as_row(),
                    ),
                )
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ScratchRetirementFenceError(
                f"could not reserve the retirement ({exc}); fail closed"
            ) from exc
        finally:
            conn.close()
        return RetirementAttempt(
            unit=self._unit,
            state=RETIRE_PENDING,
            attempt_id=attempt,
            revision=revision,
            pinned=tuple((pin.role, pin.locator) for pin in pinned),
            pin_version=2,
            generation_pins=tuple(pinned),
            approval_evidence=approval_evidence,
            reserved_at=stamp,
            updated_at=stamp,
        )

    def record_progress(
        self, *, attempt_id: str, closed: Sequence[tuple[str, str]], now: Optional[str] = None
    ) -> None:
        """Persist the closes that committed, so a resumed attempt knows what is already done."""
        conn = self._fence._connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE scratch_retirement SET closed_json=?, updated_at=? "
                "WHERE workspace_id=? AND lane_id=? AND slot_digest=? AND attempt_id=? "
                "AND state=?",
                (
                    _encode_pairs(closed),
                    now or _utc_now(),
                    *self._unit.as_row(),
                    attempt_id,
                    RETIRE_PENDING,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ScratchRetirementFenceError(
                f"could not record close progress ({exc}); fail closed"
            ) from exc
        finally:
            conn.close()

    def mark_completed(
        self,
        *,
        attempt_id: str,
        closed: Sequence[tuple[str, str]],
        detail: str = "",
        now: Optional[str] = None,
    ) -> RetirementAttempt:
        """Prove the retirement: pending -> completed. Only after a fresh whole-unit measure.

        A CAS on ``(unit, attempt_id, pending)``: a concurrent change means this attempt is no
        longer the one in flight, so it must not claim the completion. A failure here leaves
        the ``pending`` row, and the next run repairs it.
        """
        stamp = now or _utc_now()
        conn = self._fence._connect_rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision, pinned_json, reserved_at, detail FROM scratch_retirement "
                "WHERE workspace_id=? AND lane_id=? AND slot_digest=? AND attempt_id=? "
                "AND state=?",
                (
                    *self._unit.as_row(),
                    attempt_id,
                    RETIRE_PENDING,
                ),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ScratchRetirementFenceError(
                    "the retirement could not be completed: this attempt is no longer the "
                    "pending one for the unit (a concurrent run or a store change); the "
                    "closes that committed are reported, but no completion is claimed"
                )
            approval_evidence, _ = _decode_attempt_detail(row[3])
            stored_detail = _encode_attempt_detail(
                approval_evidence=approval_evidence, detail=detail
            )
            cur = conn.execute(
                "UPDATE scratch_retirement SET state=?, closed_json=?, detail=?, updated_at=? "
                "WHERE workspace_id=? AND lane_id=? AND slot_digest=? AND attempt_id=? "
                "AND state=?",
                (
                    RETIRE_COMPLETED,
                    _encode_pairs(closed),
                    stored_detail,
                    stamp,
                    *self._unit.as_row(),
                    attempt_id,
                    RETIRE_PENDING,
                ),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise ScratchRetirementFenceError(
                    "the retirement could not be completed: this attempt is no longer the "
                    "pending one for the unit (a concurrent run or a store change); the "
                    "closes that committed are reported, but no completion is claimed"
                )
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise ScratchRetirementFenceError(
                f"could not complete the retirement ({exc}); fail closed"
            ) from exc
        finally:
            conn.close()
        pin_projection = decode_scratch_retirement_pin_projection(row[1])
        public_pins = (
            tuple((pin.role, pin.locator) for pin in pin_projection.pins)
            if pin_projection.current_authority
            else pin_projection.legacy_pairs
        )
        return RetirementAttempt(
            unit=self._unit,
            state=RETIRE_COMPLETED,
            attempt_id=attempt_id,
            revision=int(row[0]),
            pinned=public_pins,
            pin_version=pin_projection.version,
            generation_pins=pin_projection.pins,
            closed=tuple(closed),
            approval_evidence=approval_evidence,
            detail=detail,
            reserved_at=str(row[2] or ""),
            updated_at=stamp,
        )


__all__ = (
    "SCRATCH_RETIREMENT_FENCE_FILENAME",
    "SCRATCH_RETIREMENT_FENCE_SEAL_SUFFIX",
    "SCRATCH_RETIREMENT_FENCE_LOCK_SUFFIX",
    "SCRATCH_RETIREMENT_FENCE_SCHEMA_VERSION",
    "RETIRE_PENDING",
    "RETIRE_COMPLETED",
    "RETIRE_ABSENT",
    "RETIRE_STATES",
    "STORE_ABSENT",
    "STORE_PRESENT",
    "STORE_DAMAGED",
    "StoreShape",
    "ScratchRetirementFenceError",
    "ScratchRetirementBusy",
    "scratch_retirement_fence_path",
    "slot_digest",
    "RetirementUnit",
    "RetirementAttempt",
    "ScratchRetirementFence",
    "SCRATCH_RETIREMENT_FENCE_TEMP_SUFFIX",
)
