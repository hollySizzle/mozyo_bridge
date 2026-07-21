"""Non-reclaimable publication fence for the callback sweep (Redmine #13889 R9-F1 / j#80383).

The authority that makes the sweep's durable record **at-most-once**, keyed by the exact record
identity ``(workspace, lane, issue, lane_generation, dispatch_anchor, outcome)``.

Why this exists as a separate thing from the attempt lease
---------------------------------------------------------
Three revisions tried to make :class:`...callback_sweep_lease.CallbackSweepLease` carry this, and a
review broke each one (R7 → R8 → R9). All three were the same move: check ownership, then write, and
try to shrink the gap. That cannot work — Redmine exposes no conditional append, so it will never
reject a stale writer, and an arbitrarily long process suspension always fits between a check and a
write.

The error was not the size of the gap. It was giving one authority two jobs with **opposite**
requirements:

- the **attempt lease** serializes slow reads. A crashed holder must not block the anchor forever,
  so it *must* expire and be reclaimable;
- a **publication** is a remote write. Once started, its outcome may be unknown, and it must *never*
  be automatically retried or handed to someone else — which is exactly what reclaim does.

So publication is not a lease action at all. It is an **outbox action**, and the repo already had
the right shape for one in :class:`...dispatch_outbox_fence.DispatchOutboxFence`: reserve before
acting, and on an unknown outcome go ``uncertain`` and stop. This applies that shape to the write.

The trade this makes, stated plainly
------------------------------------
Nothing here can stop a suspended process from eventually issuing its PUT. What it does is ensure
**nobody else ever issues one for the same record**. A reservation is never reclaimed on a timer, so
a suspended or crashed owner does not lose its claim — the anchor stalls instead, and an operator
reconciles it. Arbitrary suspension therefore becomes an **availability** loss, never a duplicate.
That is the whole point: safety is bought with availability, deliberately.

State machine
-------------
``absent -> reserved(owner) -> published(journal) | uncertain``

- ``reserved`` is won once, under ``BEGIN IMMEDIATE`` + a UNIQUE key;
- a re-entry (another owner, or this one after a crash) is **passive**: it returns
  :data:`PUBLICATION_HELD` and does **not** rewrite the live owner's row — a writer that may be
  mid-PUT is not a crash to be cleaned up;
- ``published`` is set owner-conditionally after the PUT and an exact-one read-back;
- a PUT whose fate is unknown (timeout / exception / unresolved read-back) is ``uncertain`` and is
  never auto-retried;
- there is no TTL and no reclaim. Recovery from ``reserved``/``uncertain`` is an operator act,
  because only a human can tell whether the record actually landed;
- and there is no ``recover()`` at all, unlike every sibling store (R11-F1). Minting a fresh store
  forgets live reservations, which is a reclaim of everything at once — the one thing this fence
  exists to refuse. A lost store therefore stays fail-closed. See the comment where the sibling
  stores' ``recover()`` would be;
- nor can :meth:`bootstrap` stand in for one (R12-F1). Its both-absent branch *was* the same
  reclaim, reachable from ordinary execute, because "no DB and no sidecar" reads identically for a
  fresh install and a total loss. A first-init seal (:attr:`seal_path`) now separates the two, and
  ordinary execute never bootstraps — it checks, and stops if the store is not there.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import fcntl
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

CALLBACK_PUBLICATION_FENCE_FILENAME = "callback-publication-fence.sqlite"
CALLBACK_PUBLICATION_FENCE_SIDECAR_SUFFIX = ".anchor"
#: First-init seal: proof the fence has operated here, so a both-absent pair reads as loss (R12-F1).
CALLBACK_PUBLICATION_FENCE_SEAL_SUFFIX = ".sealed"
#: Seal lifecycle. The seal is written BEFORE the store it seals, so the crash window lands here
#: rather than on an operational-but-unsealed store (R13-F1).
_SEAL_INITIALIZING = "initializing"   # a store is being minted; it has never served a reservation
SEAL_INITIALIZING = _SEAL_INITIALIZING
_SEAL_OPERATIONAL = "operational"     # this fence has run here; a missing pair is now a LOSS
#: A seal written by the first version to ship one (R13), before the lifecycle existed. It only ever
#: meant "operational", and MUST be read as such: folding it into `absent` re-mints a lost store on
#: upgrade, which is the very thing the seal was added to stop (R14-F1).
_SEAL_LEGACY_OPERATIONAL = "legacy_operational"
SEAL_LEGACY_OPERATIONAL = _SEAL_LEGACY_OPERATIONAL
#: The SECOND legacy spelling: a bare lifecycle word on the first line, written by the build that
#: introduced the lifecycle but not the format header. Missing it from the matrix left those stores
#: permanently refused as unreadable (R16-F2) -- safe, but bricked on upgrade.
_SEAL_LEGACY_INITIALIZING = "legacy_initializing"
SEAL_LEGACY_INITIALIZING = _SEAL_LEGACY_INITIALIZING
#: The seal exists but cannot be trusted — unknown content, wrong version, or unreadable. NEVER
#: `absent`: "I cannot read the record of whether this fence operated" is not "it never did".
_SEAL_INVALID = "invalid_or_unreadable"
SEAL_INVALID = _SEAL_INVALID
#: Sentinel: the seal file genuinely does not exist (FileNotFoundError, and nothing else).
_SEAL_ABSENT = "absent"
SEAL_ABSENT = _SEAL_ABSENT
#: Seals that mean "this fence has served, or could have served, publication rights here".
_SEAL_OPERATED_STATES = (_SEAL_OPERATIONAL, _SEAL_LEGACY_OPERATIONAL)
#: Seals that mean "a mint was in progress"; the two builds that wrote them differ only in spelling.
_SEAL_INITIALIZING_STATES = (_SEAL_INITIALIZING, _SEAL_LEGACY_INITIALIZING)

_SEAL_FORMAT = "mozyo-callback-publication-seal"
_SEAL_FORMAT_VERSION = 1
#: The version token exactly as it must appear on disk. Compared literally, never parsed.
_SEAL_VERSION_TOKEN = "v1"
#: Temp filenames this project has shipped. The FIRST build to write one used a fixed name; only a
#: later build made it process-unique. A format history is spelled in filenames as much as in file
#: bodies, and counting only the current spelling leaves the older machines' evidence invisible
#: (R19-F1).
_LEGACY_SEAL_TEMP_SUFFIX = ".tmp"
#: Exactly how R13 spelled its seal. Matched in full, not by prefix.
_LEGACY_SEAL_FIRST_LINE = "callback publication fence first initialized at "
CALLBACK_PUBLICATION_FENCE_SCHEMA_VERSION = 1

#: This caller won the single reservation and MAY perform the one PUT.
PUBLICATION_RESERVED = "reserved"
#: The PUT landed and an exact-one read-back confirmed it.
PUBLICATION_PUBLISHED = "published"
#: A PUT was started and its fate is unknown. NEVER auto-retried; operator reconcile only.
PUBLICATION_UNCERTAIN = "uncertain"
#: Sentinel: no row for this record identity.
PUBLICATION_ABSENT = "absent"
#: Reserve outcome for a caller that did NOT win: someone else owns this publication. Passive — the
#: live owner's row is untouched, because a writer that may be mid-PUT is not a crash.
PUBLICATION_HELD = "held"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS publication_fence (
    workspace_id    TEXT NOT NULL,
    lane_id         TEXT NOT NULL,
    issue           TEXT NOT NULL,
    lane_generation TEXT NOT NULL,
    dispatch_anchor TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    state           TEXT NOT NULL,
    owner_token     TEXT NOT NULL,
    journal_id      TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT '',
    reserved_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(workspace_id, lane_id, issue, lane_generation, dispatch_anchor, outcome)
)
"""

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_STORE_NONCE_KEY = "store_nonce"

#: SQLite keeps uncommitted rows OUTSIDE the main DB file: a rollback journal, or a WAL and its
#: shared-memory index. Any of these alone can carry a live reservation, so none of them may be
#: read as "nothing is here" (R17-F2).
_SQLITE_AUXILIARY_SUFFIXES = ("-journal", "-wal", "-shm")


class CallbackPublicationFenceError(RuntimeError):
    """The publication fence is unusable, so no PUT may be attempted (fail-closed)."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def callback_publication_fence_path(home: Optional[Path] = None) -> Path:
    return (home or mozyo_bridge_home()) / CALLBACK_PUBLICATION_FENCE_FILENAME


@dataclass(frozen=True)
class PublicationKey:
    """The EXACT record identity a publication is fenced on.

    Two sweeps publishing "the same resolution" mean the same six fields. Keying on anything
    coarser would fence unrelated records together; on anything finer would let the same record be
    written twice.
    """

    workspace_id: str
    lane_id: str
    issue: str
    lane_generation: str
    dispatch_anchor: str
    outcome: str

    def as_row(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.workspace_id,
            self.lane_id,
            self.issue,
            str(self.lane_generation),
            self.dispatch_anchor,
            self.outcome,
        )


@dataclass(frozen=True)
class PublicationReservation:
    """The outcome of a reserve. ``token`` is set only when this caller may perform the PUT."""

    status: str
    token: str = ""
    prior_state: str = PUBLICATION_ABSENT
    journal_id: str = ""
    needs_reconcile: bool = False
    detail: str = ""

    @property
    def may_publish(self) -> bool:
        return self.status == PUBLICATION_RESERVED


class CallbackPublicationFence:
    """Reserve-before-PUT authority for the sweep's durable record. No TTL, no reclaim."""

    def __init__(self, path: Optional[Path] = None, *, home: Optional[Path] = None) -> None:
        self.path = Path(path) if path else callback_publication_fence_path(home)

    @property
    def sidecar_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + CALLBACK_PUBLICATION_FENCE_SIDECAR_SUFFIX)

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
            conn.execute(f"PRAGMA user_version = {CALLBACK_PUBLICATION_FENCE_SCHEMA_VERSION}")
        finally:
            conn.close()
        self.sidecar_path.write_text(nonce, encoding="utf-8")

    def is_bootstrapped(self) -> bool:
        """True only when the store is usable AND sealed — readiness includes the seal (R13-F1).

        The seal was originally written *after* the pair and never consulted here, which made it a
        key attached to no lock: a healthy-but-unsealed store (every store created before the seal
        existed, and any first init interrupted before its seal landed) passed this check, held
        real reservations, and then re-minted on pair loss because nothing recorded that it had run.
        That is R12-F1 again. So an unsealed store is *not ready*, and the adoption path in
        :meth:`bootstrap` — not ordinary execute — is what makes it so.
        """
        return self._pair_is_healthy() and self.seal_state() == _SEAL_OPERATIONAL

    def _pair_is_healthy(self) -> bool:
        """True when DB + sidecar co-exist at the same nonce AND schema version (read-only probe)."""
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None or not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.DatabaseError:
            return False
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != CALLBACK_PUBLICATION_FENCE_SCHEMA_VERSION:
                return False
            return self._db_nonce(conn) == sidecar_nonce
        except (sqlite3.DatabaseError, TypeError, ValueError):
            return False
        finally:
            conn.close()

    @property
    def seal_path(self) -> Path:
        """The first-init seal: proof this fence has operated here, kept outside the DB+sidecar pair.

        Without it, "DB and sidecar are both absent" is indistinguishable from a fresh install, so
        bootstrap re-mints the store and forgets every live reservation — the same store-wide
        reclaim ``recover()`` performed, reachable from ordinary operation (R12-F1). The seal is
        what makes total loss *detectable*: once operational, absence of the pair means loss.

        It carries a lifecycle rather than mere existence, so that an interrupted first init is
        resumable while an operational store's loss stays fatal (R13-F1).
        """
        return self.path.with_suffix(self.path.suffix + CALLBACK_PUBLICATION_FENCE_SEAL_SUFFIX)

    def seal_state(self) -> str:
        """Read the main seal exactly, and never guess.

        Every non-``absent`` answer means "this fence may have operated here", so the only input
        that may produce :data:`_SEAL_ABSENT` is the file genuinely not existing. A permission
        error, a truncated file, a version this build does not know, or a seal written by an older
        build are all *evidence that something is here* — folding any of them into "never sealed"
        lets bootstrap re-mint a lost store, which is R14-F1.
        """
        return self._read_seal_file(self.seal_path)

    @staticmethod
    def _read_seal_file(path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # A dangling symlink raises this too, though its entry is right there. The docstring
            # above promises that only a file which genuinely does not exist reads as absent, and
            # for two rounds this line quietly broke that promise (R19-F2). Ask the directory, not
            # the reader: lexists sees the entry, read_text sees through it.
            return _SEAL_ABSENT if not os.path.lexists(path) else _SEAL_INVALID
        except OSError:
            return _SEAL_INVALID      # unreadable is not absent
        except UnicodeDecodeError:
            return _SEAL_INVALID

        first = text.splitlines()[0].strip() if text.strip() else ""
        if first.startswith(_LEGACY_SEAL_FIRST_LINE):
            return _SEAL_LEGACY_OPERATIONAL
        # Every shipped spelling of this file is a known input, including the ones written by the
        # two builds before this one. Each is a state some machine has on disk right now.
        if first == _SEAL_OPERATIONAL:
            return _SEAL_LEGACY_OPERATIONAL
        if first == _SEAL_INITIALIZING:
            return _SEAL_LEGACY_INITIALIZING
        # Exact format + version, not a prefix match: `startswith` would accept anything that
        # merely opened with the right word, and version drift would read as a valid state.
        # Literal equality on every token. Parsing the version with int() accepted `1`, `v01` and
        # even `v+1` as version 1 -- a lenient reader on the one field whose whole job is to say
        # "a different build wrote this, do not act on it" (R15-F2).
        parts = first.split(" ")
        if (
            len(parts) == 3
            and parts[0] == _SEAL_FORMAT
            and parts[1] == _SEAL_VERSION_TOKEN
            and parts[2] in (_SEAL_INITIALIZING, _SEAL_OPERATIONAL)
        ):
            return parts[2]
        return _SEAL_INVALID

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lifecycle.lock")

    @contextmanager
    def _lifecycle_lock(self):
        """Serialize bootstrap/adopt/resume across processes; released by the OS on crash.

        Without this, two processes both read "no store here", both decide to mint, and the second
        re-mints the store the first has already made operational and reserved against (R14-F2).
        The fence itself has always been exclusive (``BEGIN IMMEDIATE`` + a UNIQUE key); the
        procedure that *builds* the fence had no exclusion at all.

        ``flock`` dies with the process, so a crashed initializer never wedges the lifecycle — the
        one place where a reclaimable lock is right, because it guards a decision, not a record.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _assert_ready(self) -> None:
        """Every publication mutation calls this. Readiness is the authority's job, not the caller's.

        The composition root checks ``is_bootstrapped()`` before sweeping, but a check at the door
        is not a guard on the safe: ``reserve()`` happily granted publication rights on a store
        whose seal said ``initializing``, which bootstrap would then re-mint underneath it (R14-F2).
        """
        state = self.seal_state()
        if state in _SEAL_OPERATED_STATES:
            return
        raise CallbackPublicationFenceError(
            f"callback publication fence {self.path} is not operational (seal: {state}): refusing "
            f"every publication action. A store that has not recorded its own existence can be "
            f"re-minted underneath a live reservation, which publishes a record twice. Run "
            f"`mozyo-bridge workflow callback-publication --bootstrap`"
        )

    def seal_temp_paths(self) -> list[Path]:
        """Half-written seals left by a crash between the temp write and its replace.

        These are the OTHER kind of artifact, and the one I built and then forgot to count: not
        row-bearing like the DB and its journal, but AUTHORITY-bearing. A temp reading
        ``operational`` is durable evidence that this store was about to be — or already was —
        allowed to grant publication rights. Ignoring it let a pair loss look like a fresh install
        while that evidence sat right next to the store (R18-F1).
        """
        parent = self.seal_path.parent
        if not parent.is_dir():
            return []
        candidates = set(parent.glob(self.seal_path.name + ".*.tmp"))     # <seal>.<pid>.tmp
        candidates.add(self.seal_path.with_name(                          # <seal>.tmp (shipped)
            self.seal_path.name + _LEGACY_SEAL_TEMP_SUFFIX))
        return sorted(c for c in candidates if os.path.lexists(c))

    def seal_temp_states(self) -> list[str]:
        """What each leftover temp claims, read by the same exact rules as the main seal."""
        return [self._read_seal_file(p) for p in self.seal_temp_paths()]

    def lifecycle_artifacts(self) -> list[Path]:
        """Everything here that carries a row OR an authority claim — including the seal itself.

        The main seal was missing from this list while the docstring said "everything", which is
        how a dangling seal entry read as an empty machine (R19-F2).
        """
        seal = [self.seal_path] if os.path.lexists(self.seal_path) else []
        return self.store_artifacts() + seal + self.seal_temp_paths()

    def _seal_says_operated(self) -> bool:
        """True when the seal is any flavour of 'this fence has run here'."""
        return self.seal_state() in _SEAL_OPERATED_STATES

    def _write_seal(self, state: str) -> None:
        """Replace the seal in one step. This is a single-write atomicity, NOT mutual exclusion.

        Saying otherwise is how R14-F2 happened: an indivisible write says nothing about two
        processes deciding to write. The exclusion lives in :meth:`_lifecycle_lock`.
        """
        self.seal_path.parent.mkdir(parents=True, exist_ok=True)
        note = {
            _SEAL_INITIALIZING: "a store is being minted here; it has never served a reservation, "
                                "so re-minting it is safe",
            _SEAL_OPERATIONAL: "this fence has operated here; a missing store is a LOSS, never a "
                               "fresh install, and must be restored rather than re-created",
        }[state]
        # Process-unique temp: a fixed name is a collision between concurrent writers (R14-F2).
        tmp = self.seal_path.with_suffix(f"{self.seal_path.suffix}.{os.getpid()}.tmp")
        tmp.write_text(
            f"{_SEAL_FORMAT} {_SEAL_VERSION_TOKEN} {state}\nsealed at {_utc_now()}\n{note}\n",
            encoding="utf-8",
        )
        tmp.replace(self.seal_path)

    def store_artifacts(self) -> list[Path]:
        """Every directory entry here that could hold, or hide, a row.

        The main DB is not the only file that carries reservations: SQLite writes a rollback
        journal (``-journal``) or a write-ahead log (``-wal`` plus ``-shm``) alongside it, and an
        in-flight transaction's rows live *there* until it commits. A live journal beside a deleted
        DB is a reservation, not an absence (R17-F2).

        ``lexists`` rather than ``exists``: a broken symlink is a directory entry someone put here
        on purpose, and ``exists()`` follows the link and reports nothing at all — after which
        minting writes straight through it to the target.
        """
        candidates = [self.path, self.sidecar_path]
        candidates += [
            self.path.with_name(self.path.name + suffix)
            for suffix in _SQLITE_AUXILIARY_SUFFIXES
        ]
        return [c for c in candidates if os.path.lexists(c)]

    def has_store(self) -> bool:
        """True when ANY store artifact exists here — usable or not.

        Not the same question as :meth:`_pair_is_healthy`, and conflating them is R16-F1: a torn
        store (DB without its sidecar, mismatched nonce, unreadable file) is unusable but NOT
        empty. Its DB still holds whatever rows were reserved through it. "I cannot open this" is
        not "there is nothing here" — so re-minting on `not healthy` destroyed live reservations
        and re-granted their identities.
        """
        return bool(self.store_artifacts())

    def has_usable_store(self) -> bool:
        """True when the artifacts here actually work together (the adoption candidate)."""
        return self._pair_is_healthy()

    def has_operated(self) -> bool:
        """True once this fence has been sealed here, whether or not its store still exists.

        Invariant, and the point of R13-F1: ``is_bootstrapped()`` implies this. A store that can
        serve a reservation has always recorded that it exists.

        Note the comparison: :meth:`seal_state` returns the *string* ``absent``, so an ``is not
        None`` test here silently reports every store as sealed — which is how this briefly said
        "yes" for a store that had never run.
        """
        return self.seal_state() != _SEAL_ABSENT

    def bootstrap(self) -> None:
        """Operator-only init / adoption / resume. NEVER call from ordinary execute (R12-F1).

        Every branch answers one question: could this store already hold a reservation that a
        re-mint would forget? The whole decision runs **under the lifecycle lock, on state re-read
        after the lock is held** — reading first and acting later is what let two processes both
        decide "fresh" and the loser re-mint the winner's operational store (R14-F2).

        Decided on ARTIFACT PRESENCE, never on usability:

        ====================================  ======================================================
        state                                 action
        ====================================  ======================================================
        invalid seal                          REFUSE. Something is here and cannot be read; that is
                                              not evidence it never ran.
        healthy pair (any seal)               ADOPT in place, keeping nonce and rows; legacy and
                                              half-written seals are rewritten to the current
                                              format, the store itself untouched.
        any row-bearing artifact, unusable    REFUSE. Unusable is not empty (R16-F1): a torn
                                              store's DB, journal or WAL still holds its rows.
                                              Restore from backup -- deleting is not a recovery.
        no store + operated seal              REFUSE. It ran here and its store is gone: a loss.
        no store + LEGACY initializing        REFUSE. The build that wrote that word granted rights
                                              without checking the seal, so its owner is unknown
                                              (R17-F1) -- not the same state as the one below.
        no store + operational seal temp      REFUSE. A half-written seal is still evidence this
                                              store could grant; unknown owner (R18-F1).
        no store + ambiguous seal temps       REFUSE. "Cannot tell" is not "nothing was here".
        no store + current initializing       mint. This build's reserve() demands an operational
        (main seal, or temps that ALL say     seal, so no grant can have been issued under it --
        exactly this, however many)           however many times the crash was recorded.
        no store + absent seal                mint. First init -- see the limit below.
        ====================================  ======================================================

        Legacy spellings map onto these states, EXCEPT legacy `initializing`, whose build attached
        a different guarantee to the same word.

        The seal's own history is spelled in FILENAMES as well as bodies: the first build to write
        a temp used a fixed name, a later one made it process-unique, and both are enumerated here
        (R19-F1).

        Known limit, stated rather than hidden: a store from before the seal existed, whose files
        are all gone, is indistinguishable from a fresh install, as is one copied from another
        machine. "No artifact and no seal" therefore does not PROVE a first install -- it is simply
        the best a local check can do, and the coordinator has assigned that gap to the
        external-lifecycle / quiescence follow-up (j#80408, j#80428).

        Known limit, stated rather than hidden: deleting the seal along with the pair is
        indistinguishable from a fresh install, as is a store copied from another machine. Neither
        is decidable from local state; the coordinator has assigned both to the external-lifecycle
        follow-up (j#80408).
        """
        with self._lifecycle_lock():
            seal = self.seal_state()          # re-read UNDER the lock: any earlier read is stale
            pair_ok = self._pair_is_healthy()
            artifacts = self.has_store()
            temp_states = self.seal_temp_states()   # authority claims, re-read under the lock too

            if seal == _SEAL_INVALID:
                raise CallbackPublicationFenceError(
                    f"callback publication fence seal at {self.seal_path} exists but cannot be "
                    f"trusted (unreadable, truncated, or written by a version this build does not "
                    f"know). Refusing every action: an unreadable record of whether this fence "
                    f"operated is not a record that it never did, and acting on that guess "
                    f"re-mints a lost store and publishes its record twice"
                )

            if seal == _SEAL_ABSENT and temp_states and not pair_ok:
                # A crash between writing a seal and replacing it leaves the claim behind. With no
                # main seal and no usable store, that temp is the ONLY record of what this store
                # was allowed to do -- and reading it as "nothing here" re-grants an identity whose
                # owner may still be running (R18-F1). Only a temp that unambiguously says "a mint
                # had not finished" is safe to walk past.
                if any(t in _SEAL_OPERATED_STATES for t in temp_states):
                    raise CallbackPublicationFenceError(
                        f"callback publication fence {self.path} has no seal, but a half-written "
                        f"one at {', '.join(p.name for p in self.seal_temp_paths())} claims this "
                        f"store was operational, and the store itself is gone or unusable. A sweep "
                        f"may still hold a grant from it, so this is a loss with an unknown owner, "
                        f"not a fresh install. Restore the store from backup"
                    )
                if set(temp_states) != {_SEAL_INITIALIZING}:
                    # Unknown, unreadable, legacy, or CONFLICTING claims: not a fact we can act on,
                    # and "cannot tell" must never collapse into "nothing was here". Several temps
                    # that all say this build's `initializing` are NOT conflicting -- they are one
                    # fact recorded more than once, and this build's reserve() demands an
                    # operational seal, so none of them can have granted anything. Refusing those
                    # would brick a machine for having crashed twice, with no safety bought (the
                    # contract this now states, after claiming "lone only" while doing this --
                    # R19-F3).
                    raise CallbackPublicationFenceError(
                        f"callback publication fence {self.path} has no seal and leftover seal "
                        f"claims that cannot be trusted ({', '.join(temp_states)}). Refusing to "
                        f"decide whether this store ever operated. Restore it from backup"
                    )
                # else: an interrupted FIRST init -- every claim here is this build's `initializing`
                # and nothing else. Fall through and finish the job; that is the availability this
                # must not cost, and no count of identical claims changes what they mean.

            if pair_ok:
                # ADOPT, never re-mint -- whatever the seal says. `initializing` only proves
                # "nothing was reserved" for stores this version made; older builds could reserve
                # against an unsealed one (R15-F1). A crashed init has no rows to keep anyway, so
                # adoption is right in both cases and re-minting is right in neither.
                if seal != _SEAL_OPERATIONAL:
                    self._write_seal(_SEAL_OPERATIONAL)
                return

            if artifacts:
                # Unusable is NOT empty (R16-F1). A torn store -- DB without its sidecar, nonce
                # mismatch, unreadable file -- still holds every row that was reserved through it,
                # and re-minting here destroyed live reservations and re-granted their identities.
                # `_pair_is_healthy() == False` says "I cannot use this", never "there is nothing
                # here", and only the second would justify a mint. So this stalls for an operator,
                # whose only safe move is restoring from backup: an init that crashed mid-mint is
                # the availability price of never guessing about rows we cannot read.
                raise CallbackPublicationFenceError(
                    f"callback publication fence {self.path} has store artifacts that cannot be "
                    f"used together (only one of the DB / sidecar exists, their nonces differ, or "
                    f"the DB is unreadable). Refusing to re-create it: an unusable store is not an "
                    f"empty one, and its DB or its SQLite journal may still hold a reservation a "
                    f"suspended sweep is about to publish against. Restore the store from backup. "
                    f"Deleting the artifacts is NOT a recovery: Redmine showing nothing in flight "
                    f"right now does not prove a stalled owner will not PUT later, which is the "
                    f"same guess this fence exists to refuse. Artifacts: "
                    f"{', '.join(a.name for a in self.store_artifacts())}"
                )

            # Nothing on disk can hold a row. That makes "no reservation survives HERE" an
            # observation -- but it says nothing about an owner still running elsewhere, and only
            # the seal can speak to that.
            if seal == _SEAL_LEGACY_INITIALIZING:
                # The build that wrote a bare `initializing` had no seal check in reserve(), so it
                # could and did hand out publication rights under this very seal. The word is the
                # same as the current one; the guarantee behind it is not. Recognizing an old
                # format is not the same as reproducing what it meant (R17-F1).
                raise CallbackPublicationFenceError(
                    f"callback publication fence {self.path} carries an `initializing` seal from a "
                    f"build that granted publication rights without checking it, and its store is "
                    f"gone. Whether a sweep is still holding a grant from that store cannot be "
                    f"determined from here, so this is a loss with an unknown owner, not an "
                    f"interrupted first install. Restore the store; it cannot be re-created safely "
                    f"without a protocol that proves the old owner cannot resume (see "
                    f"{self.seal_path})"
                )

            if seal in _SEAL_OPERATED_STATES:
                raise CallbackPublicationFenceError(
                    f"callback publication fence {self.path} is sealed as previously operational, "
                    f"but its store and identity sidecar are gone: a total store loss, not a fresh "
                    f"install. Re-creating it would forget any reservation a suspended sweep still "
                    f"holds and publish its record a second time. Restore the store from backup; "
                    f"there is deliberately no reset (see {self.seal_path})"
                )

            self._write_seal(_SEAL_INITIALIZING)
            self._create_fresh(secrets.token_hex(16))
            self._write_seal(_SEAL_OPERATIONAL)

    # NO recover() — deliberately, and unlike every sibling store (R11-F1).
    #
    # `CallbackSweepLease.recover()` and `DispatchOutboxFence.recover()` are sound because those
    # stores are reclaimable by contract: the lease has a TTL and expects to be taken from a slow
    # owner, and the outbox fence treats a lingering reservation as crash residue. This fence's
    # entire contract is the opposite — a reservation is NEVER reclaimed — so "mint a fresh store
    # and forget every reservation" is not recovery here. It is a reclaim of everything at once,
    # performed on exactly the state that must not be reclaimed.
    #
    # It read as safe because it was spelled `--recover` and its help asked the operator to confirm
    # no sweep was mid-attempt. But a request is not a fence: an owner suspended between its reserve
    # and its PUT is invisible and unstoppable, and nothing local proves it will not resume. The
    # probe in j#80395 ran exactly that sequence through a *healthy* store and got two records.
    #
    # So a lost store leaves this fence permanently fail-closed, and so does an owner that crashed
    # while holding a reservation. That is the honest cost of the guarantee, not an oversight.
    # Restoring availability needs a quiescence / owner-termination protocol that can actually prove
    # the old owner cannot resume; that is follow-up work (j#80393 requirement 4), and until it
    # exists there is no safe reset to offer.

    def _connect(self) -> sqlite3.Connection:
        sidecar_nonce = self._read_sidecar_nonce()
        if sidecar_nonce is None:
            raise CallbackPublicationFenceError(
                f"callback publication fence {self.path} has no identity sidecar (never "
                f"bootstrapped / lost); fail closed rather than publish unfenced"
            )
        if not self.path.exists():
            raise CallbackPublicationFenceError(
                f"callback publication fence {self.path} is missing while its sidecar remains "
                f"(store loss); fail closed rather than auto-create and republish"
            )
        try:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute(_TABLE_SQL)
            conn.execute(_META_TABLE_SQL)
            if self._db_nonce(conn) != sidecar_nonce:
                conn.close()
                raise CallbackPublicationFenceError(
                    f"callback publication fence {self.path} nonce does not match its sidecar "
                    f"(replaced / recreated store); fail closed"
                )
            return conn
        except CallbackPublicationFenceError:
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            raise CallbackPublicationFenceError(
                f"callback publication fence {self.path} is unusable ({type(exc).__name__}); "
                f"fail closed rather than publish unfenced"
            ) from exc

    def reserve(self, key: PublicationKey, *, now: Optional[str] = None) -> PublicationReservation:
        """Claim the single right to PUT this exact record, or report never-publish.

        Refuses outright unless the seal says this fence is operational: granting publication
        rights from a store that has not recorded its own existence lets bootstrap re-mint it
        underneath this very reservation (R14-F2).

        A fresh key is won once. **Any** existing row — ``reserved`` (someone may be mid-PUT),
        ``uncertain`` (a PUT of unknown fate), or ``published`` — yields :data:`PUBLICATION_HELD`
        and leaves that row **untouched**. There is deliberately no timer that turns a lingering
        ``reserved`` into a retry: that is precisely the reclaim which produced duplicate records.
        """
        self._assert_ready()
        stamp = now or _utc_now()
        token = secrets.token_hex(16)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, journal_id FROM publication_fence WHERE workspace_id=? AND "
                "lane_id=? AND issue=? AND lane_generation=? AND dispatch_anchor=? AND outcome=?",
                key.as_row(),
            ).fetchone()
            if row is not None:
                prior = str(row[0])
                conn.execute("ROLLBACK")
                return PublicationReservation(
                    status=PUBLICATION_HELD,
                    prior_state=prior,
                    journal_id=str(row[1] or ""),
                    needs_reconcile=prior in (PUBLICATION_RESERVED, PUBLICATION_UNCERTAIN),
                    detail=(
                        f"this record is already {prior}; never publish it twice. A lingering "
                        f"reservation is NOT reclaimed on a timer — reconcile against Redmine"
                        if prior != PUBLICATION_PUBLISHED
                        else "this record is already published"
                    ),
                )
            try:
                conn.execute(
                    "INSERT INTO publication_fence (workspace_id, lane_id, issue, lane_generation, "
                    "dispatch_anchor, outcome, state, owner_token, reserved_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*key.as_row(), PUBLICATION_RESERVED, token, stamp, stamp),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return PublicationReservation(
                    status=PUBLICATION_HELD,
                    prior_state=PUBLICATION_RESERVED,
                    detail="lost a concurrent reserve race; the winner publishes",
                )
            conn.execute("COMMIT")
            return PublicationReservation(
                status=PUBLICATION_RESERVED, token=token,
                detail="reserved the single publication of this record",
            )
        except CallbackPublicationFenceError:
            raise
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise CallbackPublicationFenceError(
                f"callback publication fence reserve failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def _resolve(
        self, key: PublicationKey, token: str, state: str, *, journal_id: str, detail: str,
        now: Optional[str],
    ) -> bool:
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE publication_fence SET state=?, journal_id=?, detail=?, updated_at=? "
                "WHERE workspace_id=? AND lane_id=? AND issue=? AND lane_generation=? AND "
                "dispatch_anchor=? AND outcome=? AND owner_token=? AND state=?",
                (state, str(journal_id), detail, stamp, *key.as_row(), str(token),
                 PUBLICATION_RESERVED),
            )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except sqlite3.DatabaseError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise CallbackPublicationFenceError(
                f"callback publication fence update failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def mark_published(
        self, key: PublicationKey, token: str, journal_id: str, *, now: Optional[str] = None
    ) -> bool:
        """Owner-conditionally record that the PUT landed at ``journal_id``."""
        self._assert_ready()
        return self._resolve(key, token, PUBLICATION_PUBLISHED, journal_id=journal_id,
                             detail="published and confirmed by an exact-one read-back", now=now)

    def mark_uncertain(
        self, key: PublicationKey, token: str, *, detail: str = "", now: Optional[str] = None
    ) -> bool:
        """Owner-conditionally record that a PUT was started and its fate is unknown."""
        self._assert_ready()
        return self._resolve(key, token, PUBLICATION_UNCERTAIN, journal_id="",
                             detail=detail or "PUT outcome unknown; never auto-retried", now=now)

    def pending(self) -> list[dict]:
        """Every anchor this fence is currently blocking, for the operator surface.

        ``reserved`` here means "an owner may be mid-PUT, or died mid-PUT" — the fence cannot tell
        the two apart, which is exactly why it refuses to guess. ``uncertain`` means a PUT was
        started and its fate is unknown. Both stall their anchor until someone looks at Redmine.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT workspace_id, lane_id, issue, lane_generation, dispatch_anchor, outcome, "
                "state, journal_id, detail FROM publication_fence WHERE state IN (?, ?) "
                "ORDER BY issue, dispatch_anchor",
                (PUBLICATION_RESERVED, PUBLICATION_UNCERTAIN),
            ).fetchall()
        cols = ("workspace_id", "lane_id", "issue", "lane_generation", "dispatch_anchor",
                "outcome", "state", "journal_id", "detail")
        return [dict(zip(cols, r)) for r in rows]

    def reconcile(self, key: PublicationKey, *, published_journal: str | None) -> None:
        """Operator disposition for one stalled anchor, after reading the actual Redmine journal.

        Deliberately manual — no automatic rule can decide this correctly — but *manual is not
        unconditional*, which is the R10-F1 defect this method carries the scar of. An operator
        surface that can delete a live ``reserved`` row is a second, hand-operated reclaim path
        around the very authority the fence exists to be: A reserves, stalls before its PUT, an
        operator reads zero records in Redmine and releases the identity, B publishes, A resumes
        and publishes too. Two records, from the mechanism built to prevent them.

        So each transition is allowed only where it cannot increase the record count:

        - ``reserved``  + ``--landed``      -> ``published``. Safe in one direction only: it can
          never permit a write, only forbid one more.
        - ``uncertain`` + ``--landed``      -> ``published``. Same.
        - ``uncertain`` + ``none landed``   -> released. The owner already finished its PUT attempt
          and reported the outcome as unknown; it will not resume and write again.
        - ``reserved``  + ``none landed``   -> **REFUSED**. ``reserved`` means "an owner may be
          mid-PUT". Redmine reading zero *now* does not prove a stalled owner will not PUT *later*,
          and no local signal proves it either (an expired lease is exactly the case that started
          this — slow is not dead). Releasing it needs a quiescence / owner-termination protocol
          this fence does not have, so it stays fail-closed and the anchor stalls. See the residual
          declared in j#80390.
        - ``published`` / absent            -> **REFUSED**. Terminal, or nothing to dispose of.

        Raises :class:`CallbackPublicationFenceError` on any refused transition; the row is left
        exactly as it was.
        """
        self._assert_ready()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM publication_fence WHERE workspace_id=? AND lane_id=? AND "
                "issue=? AND lane_generation=? AND dispatch_anchor=? AND outcome=?",
                key.as_row(),
            ).fetchone()
            state = str(row[0]) if row is not None else PUBLICATION_ABSENT
            self._assert_reconcilable(key, state, published_journal)
            if published_journal is None:
                cur = conn.execute(
                    "DELETE FROM publication_fence WHERE workspace_id=? AND lane_id=? AND issue=? "
                    "AND lane_generation=? AND dispatch_anchor=? AND outcome=? AND state=?",
                    (*key.as_row(), PUBLICATION_UNCERTAIN),
                )
            else:
                cur = conn.execute(
                    "UPDATE publication_fence SET state=?, journal_id=?, owner_token='', "
                    "detail='operator reconcile', updated_at=? WHERE workspace_id=? AND "
                    "lane_id=? AND issue=? AND lane_generation=? AND dispatch_anchor=? AND "
                    "outcome=? AND state IN (?, ?)",
                    (PUBLICATION_PUBLISHED, str(published_journal), _utc_now(), *key.as_row(),
                     PUBLICATION_RESERVED, PUBLICATION_UNCERTAIN),
                )
            if cur.rowcount != 1:
                # The state moved under us between the read and the write, or matched nothing.
                conn.rollback()
                raise CallbackPublicationFenceError(
                    f"publication reconcile for {key.issue}/{key.dispatch_anchor}/{key.outcome} "
                    f"changed {cur.rowcount} row(s), expected exactly 1 (state raced or absent); "
                    f"nothing was changed — re-run `--list` and reconcile from the current state"
                )
            conn.commit()

    @staticmethod
    def _assert_reconcilable(
        key: PublicationKey, state: str, published_journal: Optional[str]
    ) -> None:
        """Refuse every operator transition that could let a second record be written."""
        if state == PUBLICATION_ABSENT:
            raise CallbackPublicationFenceError(
                f"publication fence has no row for {key.issue}/{key.dispatch_anchor}/"
                f"{key.outcome}; there is nothing to reconcile (a reconcile that silently "
                f"succeeds on an absent key hides a mistyped anchor)"
            )
        if state == PUBLICATION_PUBLISHED:
            raise CallbackPublicationFenceError(
                f"{key.issue}/{key.dispatch_anchor}/{key.outcome} is already published; that is "
                f"terminal and reopening it is how a duplicate record gets written"
            )
        if published_journal is None and state == PUBLICATION_RESERVED:
            raise CallbackPublicationFenceError(
                f"{key.issue}/{key.dispatch_anchor}/{key.outcome} is `reserved`: an owner may be "
                f"mid-PUT right now. Zero records in Redmine at this moment does not prove it will "
                f"not PUT later, so releasing it could produce a duplicate. Use `--landed <id>` if "
                f"a record did land; otherwise this anchor stays fail-closed (releasing a reserved "
                f"row needs an owner-termination protocol this fence does not have)"
            )

    def state_of(self, key: PublicationKey) -> str:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state FROM publication_fence WHERE workspace_id=? AND lane_id=? AND "
                "issue=? AND lane_generation=? AND dispatch_anchor=? AND outcome=?",
                key.as_row(),
            ).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row else PUBLICATION_ABSENT


__all__ = (
    "CALLBACK_PUBLICATION_FENCE_FILENAME",
    "CALLBACK_PUBLICATION_FENCE_SIDECAR_SUFFIX",
    "PUBLICATION_RESERVED",
    "PUBLICATION_PUBLISHED",
    "PUBLICATION_UNCERTAIN",
    "PUBLICATION_ABSENT",
    "PUBLICATION_HELD",
    "CallbackPublicationFenceError",
    "PublicationKey",
    "PublicationReservation",
    "CallbackPublicationFence",
    "callback_publication_fence_path",
)
