"""Home-scoped current-generation pointer for managed launches (Redmine #14203).

The #14203 gateway-recovery generation authority must answer, at recovery time, one
question with no collision: *is the delivery record's persisted binding the SAME process
generation as the gateway I am about to close?* Review j#87445 showed the seconds-precision
``observed_at`` on the main attestation cannot answer it — two same-second launches of the
same slot share that timestamp, so an ABA relaunch can be mistaken for the original. The
design consultation answer (j#87472) rejected two weaker fixes:

* option (b) — carrying the token on the main attestation (v2->v3) would make a v3 runtime
  refuse every launch onto an un-migrated shared v1/v2 home, because a non-empty required
  field cannot survive the conservative write policy #13882 depends on; and
* a token-only sidecar read separately from the attestation — a torn write/read pair could
  compose the identity of one generation with the token of another.

This store is the sanctioned third design: a **single home-scoped row per ``assigned_name``**
holding the whole generation as one atomic fact — the collision-free per-launch token
(``startup_action_id``, the reserved startup-transaction action id) together with the exact
identity a binding or a recovery must match. Binding and recovery read the destructive
generation authority from *this one row*; the main attestation is verified independently as
a health prerequisite but is never joined with this row as a single fact.

Two phases, mirroring the reserved/bound discipline of the replacement-binding store:

* ``pending`` — reserved by the parent **before the launch's first Herdr side effect**
  (:func:`reserve_pending`). The reservation atomically supersedes any prior row for the
  same ``assigned_name`` — a *newer* generation invalidates the old ``attested`` current
  pointer at once, so the relaunch window reads ``pending`` (fail-closed), never the stale
  ``attested`` generation.
* ``attested`` — the parent launcher finalizes the reservation only after the launch
  receipt, the startup-transaction participant, the wrapper's own
  ``attestation_write_succeeded`` execution event, and the exact main-attestation
  identity/locator all agree (:func:`HerdrLaunchGenerationStore.finalize`). The caller owns
  that composite authority; this store performs only the byte-exact compare-and-set, so an
  older launch's late finalize after a newer ``pending`` reservation is refused, and a
  wrapper best-effort token-only write is never the thing that flips the phase.

Recovery policy is ``rebuildable_cache`` (``managed-state-model.md`` ``### recovery policy
vocabulary``): a lost / absent / corrupt store degrades to fail-closed (binding and recovery
both refuse), never a speculative rebuild of a live process, and the next managed relaunch
re-establishes the row. ``observed_at`` is carried for diagnostics only and is NEVER used to
compare generations.

The file holds identity tokens and timestamps only — no argv, environment, credential,
message, or pane-content field. Initial publication is atomic and mode ``0600``; every
mutation uses a SQLite ``BEGIN IMMEDIATE`` transaction. This store never migrates or repairs
an unknown shape: pure stdlib + sqlite, so the dependency never points core -> provider.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

HERDR_LAUNCH_GENERATION_FILENAME = "herdr-launch-generation.sqlite"
HERDR_LAUNCH_GENERATION_SCHEMA_VERSION = 1

#: The wire version of the *generation protocol* a managed launcher must implement for the
#: parent to be able to finalize a launch generation (Redmine #14203 review j#87479 F1):
#: the wrapped child emits the ``attestation_write_succeeded`` startup execution event
#: keyed by its own assigned name, so the parent's finalize can require that composite
#: evidence. This is advertised and preflighted INDEPENDENTLY of the attestation-store
#: schema version — a launcher can carry ``agent-attest`` + a matching attestation schema
#: yet predate the wrapper event, in which case a generation reserved before the first
#: Herdr side effect would only ever be discovered un-finalizable AFTER actuation. Bumping
#: this refuses such a launcher before any side effect.
HERDR_LAUNCH_GENERATION_PROTOCOL_VERSION = 1

#: Recovery policy (``managed-state-model.md`` ``### recovery policy vocabulary``): losing
#: it degrades to fail-closed (binding + recovery refuse) and the next managed relaunch
#: re-reserves it. It is never speculatively rebuilt from a live process.
HERDR_LAUNCH_GENERATION_RECOVERY_POLICY = "rebuildable_cache"

GENERATION_PENDING = "pending"
GENERATION_ATTESTED = "attested"

# --- Store status vocabulary (for the public maintenance rail, Redmine #14203 F2). ----
#: No store file yet — a fresh / rebuilt home. The next managed launch creates it.
GENERATION_STORE_ABSENT = "generation_store_absent"
#: The file exists and presents the exact recognized v1 schema — usable.
GENERATION_STORE_HEALTHY = "generation_store_healthy"
#: The file exists but cannot be opened / validated (corrupt, partial, foreign, or a
#: non-database). Reads fail closed; the ``rebuildable_cache`` policy admits a backup-first
#: public rebuild to recover, never an implicit repair.
GENERATION_STORE_CORRUPT = "generation_store_corrupt"

_TABLE = "herdr_launch_generations"
_COLUMNS = (
    "assigned_name",
    "startup_action_id",
    "phase",
    "workspace_id",
    "role",
    "lane_id",
    "locator",
    "verdict",
    "observed_at",
    "reserved_at",
    "attested_at",
)
#: The exact ``PRAGMA table_info`` projection (name, TYPE, notnull, pk) a valid store must
#: present. An extra / missing / reshaped column fails closed rather than being adopted.
_EXPECTED_INFO = (
    ("assigned_name", "TEXT", 1, 1),
    ("startup_action_id", "TEXT", 1, 0),
    ("phase", "TEXT", 1, 0),
    ("workspace_id", "TEXT", 1, 0),
    ("role", "TEXT", 1, 0),
    ("lane_id", "TEXT", 1, 0),
    ("locator", "TEXT", 1, 0),
    ("verdict", "TEXT", 1, 0),
    ("observed_at", "TEXT", 1, 0),
    ("reserved_at", "TEXT", 1, 0),
    ("attested_at", "TEXT", 1, 0),
)
#: One current row per ``assigned_name`` (the PRIMARY KEY). The phase CHECK makes an
#: ``attested`` row structurally distinct from a ``pending`` one, so a partial write can
#: never be decoded as a usable generation.
_CREATE_SQL = f"""
CREATE TABLE {_TABLE} (
    assigned_name TEXT NOT NULL,
    startup_action_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    role TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    locator TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL DEFAULT '',
    reserved_at TEXT NOT NULL,
    attested_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (assigned_name),
    CHECK (phase IN ('pending', 'attested')),
    CHECK (
        (phase = 'pending'
            AND locator = '' AND verdict = '' AND observed_at = '' AND attested_at = '')
        OR
        (phase = 'attested'
            AND locator <> '' AND verdict <> '' AND observed_at <> '' AND attested_at <> '')
    )
)
"""


class HerdrLaunchGenerationError(RuntimeError):
    """The launch-generation authority is absent, malformed, or its write was refused."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _token(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise HerdrLaunchGenerationError(f"{field} is not a text token")
    if value != value.strip() or (not value and not allow_empty):
        raise HerdrLaunchGenerationError(
            f"{field} is empty or has surrounding whitespace"
        )
    return value


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except sqlite3.DatabaseError:
        pass


def herdr_launch_generation_path(home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / HERDR_LAUNCH_GENERATION_FILENAME


class LaunchGenerationStoreLockBusy(RuntimeError):
    """The store lock is held by a peer, so this operation must not proceed."""


class LaunchGenerationStoreLockUnavailable(RuntimeError):
    """Advisory locking is not available, so the protocol cannot be honored."""


class LaunchGenerationStoreLockReleaseError(RuntimeError):
    """The lock was acquired and the body completed, but the lock could NOT be released.

    Distinct from an acquisition failure (review j#87512 F1): a release failure after a
    successful body means the OS lock may still be held, so a subsequent managed write
    (blocking SHARED) could stall. It is surfaced fail-closed — never swallowed as success —
    because hiding it would report a completed operation while leaking a live lock.
    """


#: Home-scoped advisory lock coordinating the two boundaries that touch this store (Redmine
#: #14203 review j#87488 P1). Its own file: it is neither store content nor a backup
#: artifact, carries no credential, and is private (0600). A managed launch's reserve /
#: finalize write takes it SHARED (blocking); public maintenance (rebuild) takes it EXCLUSIVE
#: (non-blocking) from BEFORE its probe through completion, so the generation the probe
#: observed cannot be replaced underneath it — the path ABA that let a stale rebuild
#: quarantine and delete a fresh, valid store the reviewer reproduced.
HERDR_LAUNCH_GENERATION_LOCK_FILENAME = ".herdr-launch-generation.lock"


def launch_generation_store_lock_path(home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / HERDR_LAUNCH_GENERATION_LOCK_FILENAME


@contextmanager
def launch_generation_store_lock(home: Path, *, exclusive: bool, blocking: bool):
    """Hold the home's launch-generation-store advisory lock (Redmine #14203 j#87488 P1).

    Mirrors ``attestation_store_lock``: the store's generation cannot be replaced underneath
    an operation because the boundaries that touch it are coordinated through one lock. A
    holder's crash releases it at the OS level.

    Three explicit phases, so a lock failure is a TYPED outcome a public rail can render as a
    structured refusal — never a raw traceback across the recovery boundary (review j#87496 F1)
    — while NEVER hiding a lock leak behind a reported success (review j#87512 F1):

    * **acquire** (``mkdir`` / ``os.open`` / ``flock``): any :class:`OSError` here is a
      fail-closed :class:`LaunchGenerationStoreLockUnavailable` (contention →
      :class:`LaunchGenerationStoreLockBusy`). Nothing has been yielded, so nothing was done.
    * **body** (``yield``): the caller's operation runs; ITS errors propagate unchanged.
    * **release** (``flock`` unlock + ``os.close``, ALWAYS both attempted): a failure here is
      handled by the body's outcome (mirroring ``startup_transaction_fence`` /
      ``coordinator_placement_fence``). If the body RAISED, its exception is the real fault and
      a secondary release error is suppressed so the body exception propagates unchanged. If
      the body SUCCEEDED, a release error is raised as :class:`LaunchGenerationStoreLockReleaseError`
      — never swallowed — because the OS lock may still be held and a caller reporting success
      would hide a live lock leak.
    """
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - POSIX-only platforms in practice
        raise LaunchGenerationStoreLockUnavailable(
            "advisory file locking (fcntl.flock) is unavailable on this platform, so the "
            "launch-generation-store lock protocol cannot be honored; refusing to proceed "
            "unlocked"
        ) from exc

    path = launch_generation_store_lock_path(home)
    # --- acquire phase: every OSError is a typed refusal, nothing has happened yet. ---------
    fd: Optional[int] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        flags = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (
            0 if blocking else fcntl.LOCK_NB
        )
        fcntl.flock(fd, flags)
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if not blocking and getattr(exc, "errno", None) in (errno.EACCES, errno.EAGAIN):
            raise LaunchGenerationStoreLockBusy(
                "the launch-generation store is locked by another operation on this home "
                "(maintenance, or a managed launch's reserve / finalize write)"
            ) from exc
        raise LaunchGenerationStoreLockUnavailable(
            f"the launch-generation store lock could not be acquired at {path.name} "
            f"({exc.__class__.__name__}: {exc}); the home may be unwritable or its filesystem "
            f"may not support advisory locks"
        ) from exc
    # --- body phase: the caller's error, if any, is theirs and propagates as-is. ------------
    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        # --- release phase: ALWAYS attempt BOTH unlock and close; the fd is always closed. ---
        release_error: Optional[OSError] = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as unlock_exc:  # pragma: no cover - unlock rarely errors
            release_error = unlock_exc
        try:
            os.close(fd)
        except OSError as close_exc:  # pragma: no cover - close rarely errors
            release_error = release_error or close_exc
        # A release error is surfaced ONLY when the body succeeded: a body exception is the
        # real fault and must not be overwritten by a secondary release error (whose swallow
        # is the ONE place hiding it is correct). On body success, a leaked lock is never
        # hidden behind a reported success — the caller renders it phase-aware.
        if release_error is not None and not body_failed:
            raise LaunchGenerationStoreLockReleaseError(
                f"the launch-generation store lock at {path.name} could not be released "
                f"({release_error.__class__.__name__}: {release_error}); the lock may still "
                f"be held, so a subsequent managed write could stall until this process exits"
            ) from release_error


def probe_launch_generation_store(path: Path) -> tuple[str, str]:
    """Read-only classify the store at ``path`` -> ``(state, detail)`` (never mutates).

    The read side of the public maintenance rail (Redmine #14203 F2). An absent file is a
    legitimate fresh / rebuilt home; a file that opens and presents the exact v1 schema is
    healthy; anything else (unopenable, wrong shape, wrong version) is corrupt and fails
    closed — the ``rebuildable_cache`` state a backup-first rebuild recovers.
    """
    if not path.exists():
        return GENERATION_STORE_ABSENT, "no store yet"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            HerdrLaunchGenerationStore._validate_schema(conn)
        finally:
            conn.close()
    except (HerdrLaunchGenerationError, sqlite3.DatabaseError, OSError) as exc:
        return GENERATION_STORE_CORRUPT, f"{exc.__class__.__name__}: {exc}"
    return GENERATION_STORE_HEALTHY, f"recognized v{HERDR_LAUNCH_GENERATION_SCHEMA_VERSION}"


@dataclass(frozen=True)
class LaunchGeneration:
    """The whole generation for one ``assigned_name`` as a single atomic fact."""

    assigned_name: str
    startup_action_id: str
    phase: str
    workspace_id: str
    role: str
    lane_id: str
    locator: str = ""
    verdict: str = ""
    observed_at: str = ""
    reserved_at: str = ""
    attested_at: str = ""

    def as_payload(self) -> dict:
        return {
            "assigned_name": self.assigned_name,
            "startup_action_id": self.startup_action_id,
            "phase": self.phase,
            "workspace_id": self.workspace_id,
            "role": self.role,
            "lane_id": self.lane_id,
            "locator": self.locator,
            "verdict": self.verdict,
            "observed_at": self.observed_at,
            "reserved_at": self.reserved_at,
            "attested_at": self.attested_at,
        }


def _decode(row: tuple) -> LaunchGeneration:
    if len(row) != len(_COLUMNS):
        raise HerdrLaunchGenerationError("generation row has an unexpected width")
    values = tuple(
        _token(
            value,
            name,
            allow_empty=name in {"locator", "verdict", "observed_at", "attested_at"},
        )
        for name, value in zip(_COLUMNS, row)
    )
    generation = LaunchGeneration(**dict(zip(_COLUMNS, values)))
    if generation.phase not in (GENERATION_PENDING, GENERATION_ATTESTED):
        raise HerdrLaunchGenerationError("generation row has an unknown phase")
    if generation.phase == GENERATION_PENDING and any(
        (generation.locator, generation.verdict, generation.observed_at,
         generation.attested_at)
    ):
        raise HerdrLaunchGenerationError("pending generation carries attested fields")
    if generation.phase == GENERATION_ATTESTED and not all(
        (generation.locator, generation.verdict, generation.observed_at,
         generation.attested_at)
    ):
        raise HerdrLaunchGenerationError("attested generation is incomplete")
    return generation


class HerdrLaunchGenerationStore:
    """Fail-closed, home-scoped current-generation pointer keyed by ``assigned_name``."""

    def __init__(self, path: Path | None = None, *, home: Path | None = None):
        self.path = path or herdr_launch_generation_path(home)

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = tuple(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        info = tuple(
            (row[1], str(row[2]).upper(), row[3], row[5])
            for row in conn.execute(f"PRAGMA table_info({_TABLE})").fetchall()
        )
        if (
            version != HERDR_LAUNCH_GENERATION_SCHEMA_VERSION
            or tables != (_TABLE,)
            or info != _EXPECTED_INFO
        ):
            raise HerdrLaunchGenerationError(
                "herdr launch-generation store has an unknown or partial schema; "
                "refusing to migrate or repair it implicitly"
            )

    def _validate_file_security(self) -> None:
        """Require an operator-owned regular 0600 file; never repair it implicitly."""
        try:
            metadata = self.path.lstat()
        except OSError as exc:
            raise HerdrLaunchGenerationError(
                "herdr launch-generation store metadata is unreadable"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise HerdrLaunchGenerationError(
                "herdr launch-generation store is not a regular file"
            )
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise HerdrLaunchGenerationError(
                "herdr launch-generation store is not owned by the current operator"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise HerdrLaunchGenerationError(
                "herdr launch-generation store permissions are not exactly 0600"
            )

    def _publish_fresh(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(
            prefix=".launch-generation-", suffix=".sqlite", dir=self.path.parent
        )
        candidate = Path(raw)
        os.close(fd)
        try:
            os.chmod(candidate, 0o600)
            conn = sqlite3.connect(candidate)
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute(
                    f"PRAGMA user_version={HERDR_LAUNCH_GENERATION_SCHEMA_VERSION}"
                )
                conn.execute(_CREATE_SQL)
                conn.commit()
                self._validate_schema(conn)
            finally:
                conn.close()
            try:
                os.link(candidate, self.path)
            except FileExistsError:
                pass  # an atomically-published peer won; validate it below
        finally:
            candidate.unlink(missing_ok=True)

    def _connect_existing(self, *, readonly: bool) -> sqlite3.Connection:
        self._validate_file_security()
        mode = "ro" if readonly else "rw"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode={mode}", uri=True)
            conn.execute("PRAGMA busy_timeout=2000")
            self._validate_schema(conn)
            return conn
        except HerdrLaunchGenerationError:
            if conn is not None:
                conn.close()
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            if conn is not None:
                conn.close()
            raise HerdrLaunchGenerationError(
                "herdr launch-generation store is unreadable"
            ) from exc
        except BaseException:
            if conn is not None:
                conn.close()
            raise

    def _ensure_store(self) -> None:
        try:
            if not self.path.exists():
                self._publish_fresh()
            conn = self._connect_existing(readonly=True)
            conn.close()
        except HerdrLaunchGenerationError:
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            raise HerdrLaunchGenerationError(
                "herdr launch-generation store could not be published atomically"
            ) from exc

    @staticmethod
    def _row(conn: sqlite3.Connection, assigned_name: str):
        return conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM {_TABLE} WHERE assigned_name=?",
            (assigned_name,),
        ).fetchone()

    def _with_shared_write_lock(self, run):
        """Run a mutating write while holding the home's store lock SHARED (blocking).

        Redmine #14203 review j#87488 P1: a managed launch's reserve / finalize takes the
        lock shared so it coexists with peer writes but is excluded during exclusive
        maintenance — the ABA that let a stale rebuild rotate a fresh store away. A lock the
        platform cannot honor is a fail-closed :class:`HerdrLaunchGenerationError`.
        """
        try:
            with launch_generation_store_lock(
                self.path.parent, exclusive=False, blocking=True
            ):
                return run()
        except (
            LaunchGenerationStoreLockBusy,
            LaunchGenerationStoreLockUnavailable,
        ) as exc:
            # Lock acquisition failed (busy / unavailable / an unwritable home) — the lock
            # normalizes every acquire OSError to these typed outcomes, so this is a
            # fail-closed write refusal. The locked write body's own sqlite/OS errors are
            # already HerdrLaunchGenerationError and never reach here.
            raise HerdrLaunchGenerationError(
                f"the launch-generation store lock could not be taken for a write ({exc})"
            ) from exc
        except LaunchGenerationStoreLockReleaseError as exc:
            # The write's DB transaction COMMITTED inside the body (the reservation / finalize
            # may have landed), but the store lock could not be released (review j#87512 F1).
            # Fail closed with a typed error that does NOT hide the commit — this process must
            # be restarted before it writes again, since the lock may still be held.
            raise HerdrLaunchGenerationError(
                f"the launch-generation write completed (its row may have committed) but the "
                f"store lock could not be released ({exc}); restart this process before it "
                f"writes again — the lock may still be held"
            ) from exc

    def read(self, assigned_name: str) -> Optional[LaunchGeneration]:
        """The current generation row for ``assigned_name``, or ``None`` (fail-closed).

        An absent store is a legitimate fresh / rebuildable_cache home and returns ``None``;
        an unreadable / partial store raises so the caller fails closed rather than treating
        a corrupt file as "no generation".
        """
        name = _token(assigned_name, "assigned_name")
        if not self.path.exists():
            return None
        try:
            conn = self._connect_existing(readonly=True)
            try:
                conn.execute("BEGIN")
                row = self._row(conn, name)
            finally:
                conn.close()
        except HerdrLaunchGenerationError:
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            raise HerdrLaunchGenerationError(
                "herdr launch-generation store is unreadable"
            ) from exc
        return _decode(row) if row is not None else None

    def assigned_names(self) -> Optional[frozenset]:
        """Every ``assigned_name`` this store holds, or ``None`` if it cannot be read.

        The live-consumer evidence the public maintenance rail intersects with the live
        fleet (Redmine #14203 F2). ``None`` is a measured "unreadable", never folded into an
        empty set — a corrupt store while agents are live must fail the rebuild gate closed.
        """
        if not self.path.exists():
            return frozenset()
        try:
            conn = self._connect_existing(readonly=True)
            try:
                conn.execute("BEGIN")
                rows = conn.execute(f"SELECT assigned_name FROM {_TABLE}").fetchall()
            finally:
                conn.close()
        except (HerdrLaunchGenerationError, sqlite3.DatabaseError, OSError):
            return None
        return frozenset(str(row[0]) for row in rows)

    def reserve_pending(
        self,
        *,
        assigned_name: str,
        startup_action_id: str,
        workspace_id: str,
        role: str,
        lane_id: str,
    ) -> LaunchGeneration:
        """Reserve a fresh ``pending`` generation BEFORE the launch's first Herdr side effect.

        Atomically supersedes any prior row for ``assigned_name`` — the newer generation
        invalidates the old ``attested`` current pointer at once, closing the relaunch ABA
        window (the row reads ``pending`` until this generation itself attests). Held under
        the home's SHARED store lock so maintenance cannot rotate the store mid-write. A store
        write (or lock) that cannot proceed raises :class:`HerdrLaunchGenerationError`, which
        the caller turns into a typed zero-actuation launch refusal.
        """
        return self._with_shared_write_lock(
            lambda: self._reserve_pending_locked(
                assigned_name=assigned_name,
                startup_action_id=startup_action_id,
                workspace_id=workspace_id,
                role=role,
                lane_id=lane_id,
            )
        )

    def _reserve_pending_locked(
        self,
        *,
        assigned_name: str,
        startup_action_id: str,
        workspace_id: str,
        role: str,
        lane_id: str,
    ) -> LaunchGeneration:
        fields = {
            "assigned_name": _token(assigned_name, "assigned_name"),
            "startup_action_id": _token(startup_action_id, "startup_action_id"),
            "workspace_id": _token(workspace_id, "workspace_id"),
            "role": _token(role, "role"),
            "lane_id": _token(lane_id, "lane_id"),
        }
        self._ensure_store()
        conn = self._connect_existing(readonly=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = _utc_now()
            # INSERT OR REPLACE on the assigned_name PK: a newer generation supersedes any
            # prior pending/attested row for this slot in one statement, so no window
            # exposes the stale attested pointer.
            conn.execute(
                f"INSERT OR REPLACE INTO {_TABLE} ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                (
                    fields["assigned_name"],
                    fields["startup_action_id"],
                    GENERATION_PENDING,
                    fields["workspace_id"],
                    fields["role"],
                    fields["lane_id"],
                    "",  # locator
                    "",  # verdict
                    "",  # observed_at
                    now,  # reserved_at
                    "",  # attested_at
                ),
            )
            conn.commit()
            return LaunchGeneration(
                phase=GENERATION_PENDING, reserved_at=now, **fields
            )
        except HerdrLaunchGenerationError:
            _rollback_quietly(conn)
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            _rollback_quietly(conn)
            raise HerdrLaunchGenerationError(
                "herdr launch-generation reservation write failed"
            ) from exc
        except BaseException:
            _rollback_quietly(conn)
            raise
        finally:
            conn.close()

    def finalize(
        self,
        *,
        assigned_name: str,
        startup_action_id: str,
        workspace_id: str,
        role: str,
        lane_id: str,
        locator: str,
        verdict: str,
        observed_at: str,
    ) -> LaunchGeneration:
        """Compare-and-set the reserved generation to ``attested``.

        The caller owns the composite authority (launch receipt + startup-transaction
        participant + wrapper ``attestation_write_succeeded`` event + exact main-attestation
        identity/locator). This store performs only the byte-exact CAS on
        ``(assigned_name, startup_action_id, phase='pending')`` and the reserved identity, so
        an older launch's late finalize after a *newer* ``pending`` reservation matches zero
        rows and is refused — never overwriting the newer generation. Held under the home's
        SHARED store lock so maintenance cannot rotate the store mid-write.
        """
        return self._with_shared_write_lock(
            lambda: self._finalize_locked(
                assigned_name=assigned_name,
                startup_action_id=startup_action_id,
                workspace_id=workspace_id,
                role=role,
                lane_id=lane_id,
                locator=locator,
                verdict=verdict,
                observed_at=observed_at,
            )
        )

    def _finalize_locked(
        self,
        *,
        assigned_name: str,
        startup_action_id: str,
        workspace_id: str,
        role: str,
        lane_id: str,
        locator: str,
        verdict: str,
        observed_at: str,
    ) -> LaunchGeneration:
        fields = {
            "assigned_name": _token(assigned_name, "assigned_name"),
            "startup_action_id": _token(startup_action_id, "startup_action_id"),
            "workspace_id": _token(workspace_id, "workspace_id"),
            "role": _token(role, "role"),
            "lane_id": _token(lane_id, "lane_id"),
            "locator": _token(locator, "locator"),
            "verdict": _token(verdict, "verdict"),
            "observed_at": _token(observed_at, "observed_at"),
        }
        if not self.path.exists():
            raise HerdrLaunchGenerationError(
                "cannot finalize a generation: the store does not exist (no reservation)"
            )
        conn = self._connect_existing(readonly=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = _utc_now()
            conn.execute(
                f"UPDATE {_TABLE} SET phase=?, locator=?, verdict=?, observed_at=?, "
                "attested_at=? "
                "WHERE assigned_name=? AND startup_action_id=? AND phase=? "
                "AND workspace_id=? AND role=? AND lane_id=?",
                (
                    GENERATION_ATTESTED,
                    fields["locator"],
                    fields["verdict"],
                    fields["observed_at"],
                    now,
                    fields["assigned_name"],
                    fields["startup_action_id"],
                    GENERATION_PENDING,
                    fields["workspace_id"],
                    fields["role"],
                    fields["lane_id"],
                ),
            )
            if conn.total_changes != 1:
                raise HerdrLaunchGenerationError(
                    "launch-generation finalize compare-and-set was refused (no matching "
                    "pending reservation for this exact identity and token — a newer "
                    "generation may have superseded it)"
                )
            row = self._row(conn, fields["assigned_name"])
            conn.commit()
            attested = _decode(row)
            return attested
        except HerdrLaunchGenerationError:
            _rollback_quietly(conn)
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            _rollback_quietly(conn)
            raise HerdrLaunchGenerationError(
                "launch-generation finalize write failed"
            ) from exc
        except BaseException:
            _rollback_quietly(conn)
            raise
        finally:
            conn.close()


def verified_generation_token(
    home: Path | None,
    *,
    assigned_name: str,
    workspace_id: str,
    role: str,
    lane_id: str,
    locator: str,
    norm,
    norm_lane,
    participant_receipt_matches=None,
) -> str:
    """The attested generation token for this exact gateway, or ``""`` (read-only, j#87472).

    The ONE generation authority shared by the queue-enter binding (delivery time) and the
    gateway recovery (recovery time), so the two can never drift. Returns the collision-free
    per-launch token (``startup_action_id``) iff, all exact and fail-closed:

    * an ``attested`` generation row exists for ``assigned_name`` with a non-empty token,
      ``verdict == present``, and ``workspace_id`` / ``role`` / ``lane_id`` / ``locator``
      equal to the expected identity (the whole generation is ONE atomic row — identity and
      token are never read from two files that could tear); AND
    * that token names a startup transaction that reached ``completed_success`` whose
      participant for ``role`` is exactly this gateway (``assigned_name`` + ``locator``, not
      closed) — a rolled-back / foreign / superseded generation never lends its token.

    ``norm`` / ``norm_lane`` are injected by the caller so this core module never imports the
    provider identity helpers (the dependency never points core -> provider). Any unreadable
    / absent / pending / mismatched input yields ``""``.
    """
    from mozyo_bridge.core.state.herdr_identity_attestation import VERDICT_PRESENT
    from mozyo_bridge.core.state.startup_transaction_fence import (
        PHASE_COMPLETED_SUCCESS,
        StartupTransactionError,
        StartupTransactionFence,
    )

    try:
        generation = HerdrLaunchGenerationStore(home=home).read(norm(assigned_name))
    except (HerdrLaunchGenerationError, Exception):  # noqa: BLE001 - unreadable => none
        return ""
    if generation is None:
        return ""
    token = norm(getattr(generation, "startup_action_id", "") or "")
    if not (
        norm(getattr(generation, "phase", "")) == GENERATION_ATTESTED
        and token
        and norm(getattr(generation, "verdict", "")) == VERDICT_PRESENT
        and norm(getattr(generation, "assigned_name", "")) == norm(assigned_name)
        and norm(getattr(generation, "role", "")) == norm(role)
        and norm_lane(getattr(generation, "lane_id", "")) == norm_lane(lane_id)
        and norm(getattr(generation, "locator", "")) == norm(locator)
        and norm(getattr(generation, "workspace_id", "")) == norm(workspace_id)
    ):
        return ""
    try:
        action = StartupTransactionFence(home=home).read(token)
    except (StartupTransactionError, Exception):  # noqa: BLE001 - unreadable => none
        return ""
    if action is None or norm(getattr(action, "phase", "")) != PHASE_COMPLETED_SUCCESS:
        return ""
    participant = action.participant_for(norm(role))
    if participant is None or getattr(participant, "closed", True):
        return ""
    if not (
        norm(getattr(participant, "assigned_name", "")) == norm(assigned_name)
        and norm(getattr(participant, "locator", "")) == norm(locator)
    ):
        return ""
    if participant_receipt_matches is not None:
        try:
            if not participant_receipt_matches(
                getattr(participant, "receipt", "")
            ):
                return ""
        except Exception:  # noqa: BLE001 - malformed receipt cannot grant authority
            return ""
    return token


__all__ = (
    "GENERATION_ATTESTED",
    "GENERATION_PENDING",
    "GENERATION_STORE_ABSENT",
    "GENERATION_STORE_CORRUPT",
    "GENERATION_STORE_HEALTHY",
    "HERDR_LAUNCH_GENERATION_FILENAME",
    "HERDR_LAUNCH_GENERATION_LOCK_FILENAME",
    "HERDR_LAUNCH_GENERATION_PROTOCOL_VERSION",
    "HERDR_LAUNCH_GENERATION_RECOVERY_POLICY",
    "HERDR_LAUNCH_GENERATION_SCHEMA_VERSION",
    "HerdrLaunchGenerationError",
    "HerdrLaunchGenerationStore",
    "LaunchGeneration",
    "LaunchGenerationStoreLockBusy",
    "LaunchGenerationStoreLockReleaseError",
    "LaunchGenerationStoreLockUnavailable",
    "herdr_launch_generation_path",
    "launch_generation_store_lock",
    "launch_generation_store_lock_path",
    "probe_launch_generation_store",
    "verified_generation_token",
)
