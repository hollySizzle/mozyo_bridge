"""Replacement transaction store: the atomic self-replacement CAS (Redmine #13806).

Tranche A of the "1 action generation = 1 durable replacement transaction" design
(Design Answer j#78384, Coordinator Verdict j#78406). The **store** half of the
component: schema, registration, and the guarded writes over the single
``replacement_transactions`` row per ``(workspace_id, action_id)``. The closed
vocabularies, the transition matrices, and the typed records / codecs are the pure
:mod:`mozyo_bridge.core.state.replacement_transaction_model`, re-exported here so callers
have one import surface.

A **native component** of the consolidated ``state.sqlite`` (the sibling
:mod:`...lane_lifecycle` precedent): it uses the container guard only to create / validate
the container, then opens its own autocommit connection for the CAS (the guard's
default-isolation connection cannot drive ``BEGIN IMMEDIATE``). Every write is CAS: it
takes ``BEGIN IMMEDIATE``, re-reads the locked row, and matches an **exact expected
revision** (and, for a lease / effect, the exact lease holder). A stale, duplicate, or
out-of-order caller updates nothing and is told why (:class:`CasOutcome`) rather than
clobbering a newer decision.

What this component owns (tranche A): the durable transaction header, the phase DAG, the
participant-owed progression, and the lease. What it does NOT own (tranches B / C): the
exact process close / launch, the self-close arm, the bare-``mozyo`` pre-attach
reconcile, and the fresh-coordinator continuation drain. This store never touches the
#13810 ``lane_lifecycle_records`` row (scope item 4): a participant that is an issue lane
carries that lane's ``(revision, generation)`` as an immutable pin in the manifest, read
only, never written back.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.replacement_transaction_model import (
    CAS_ALREADY_DECLARED,
    CAS_APPLIED,
    CAS_FORBIDDEN_TRANSITION,
    CAS_LEASE_CONFLICT,
    CAS_LEASE_NOT_HELD,
    CAS_NOT_FOUND,
    CAS_PARTICIPANT_NOT_FOUND,
    CAS_STALE_REVISION,
    PARTICIPANT_PHASES,
    PHASE_PLANNED,
    TRANSACTION_PHASES,
    CasOutcome,
    ContinuationPointer,
    DecisionPointer,
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionRecord,
    decode_participants,
    encode_participants,
    norm,
    participant_transition_allowed,
    transaction_transition_allowed,
    validate_participants,
)
from mozyo_bridge.core.state.replacement_transaction_schema import (
    COLUMNS as _COLUMNS,
    READONLY_COMPONENT_ABSENT,
    READONLY_COMPONENT_RECOGNIZED,
    REPLACEMENT_TRANSACTION_COMPONENT,
    REPLACEMENT_TRANSACTION_RECOVERY_POLICY,
    REPLACEMENT_TRANSACTION_SCHEMA_VERSION,
    TABLE as _TABLE,
    ReplacementTransactionError,
    _utc_now,
    ensure_replacement_transaction_schema,
    readonly_component_status,
    replacement_transaction_path,
)


# -- row plumbing ------------------------------------------------------------


def _record(row: Sequence[object]) -> ReplacementTransactionRecord:
    return ReplacementTransactionRecord(
        workspace_id=str(row[0]),
        action_id=str(row[1]),
        action_generation=int(row[2]),
        phase=str(row[3]),
        revision=int(row[4]),
        decision_source=str(row[5] or ""),
        decision_issue_id=str(row[6] or ""),
        decision_journal=str(row[7] or ""),
        continuation_source=str(row[8] or ""),
        continuation_issue_id=str(row[9] or ""),
        continuation_journal=str(row[10] or ""),
        continuation_expected_gate=str(row[11] or ""),
        continuation_next_action=str(row[12] or ""),
        participants_manifest=str(row[13] or ""),
        lease_holder=str(row[14] or ""),
        lease_epoch=int(row[15]),
        lease_expires_at=str(row[16] or ""),
        created_at=str(row[17]),
        updated_at=str(row[18]),
    )


def _locked_row(
    conn: sqlite3.Connection, key: ReplacementTransactionKey
) -> Optional[ReplacementTransactionRecord]:
    """Read the row inside the already-open ``BEGIN IMMEDIATE`` write lock."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM {_TABLE} WHERE workspace_id = ? AND action_id = ?",
        key.as_row(),
    ).fetchone()
    return _record(row) if row is not None else None


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.DatabaseError:
        pass


def _header_matches(
    existing: ReplacementTransactionRecord,
    *,
    action_generation: int,
    decision: DecisionPointer,
    continuation: ContinuationPointer,
    participants_manifest: str,
) -> bool:
    """Is ``existing`` an exact-header duplicate of this plan? (the idempotency test)

    Compares the immutable header AND the encoded participant manifest. The manifest
    encodes each participant's phase, so a transaction whose participants have advanced
    (an in-flight one) never matches a fresh planned manifest — only a genuinely untouched
    re-plan of the identical transaction is idempotent; anything else is a conflict.
    """
    return (
        existing.action_generation == action_generation
        and existing.decision_source == decision.source
        and existing.decision_issue_id == decision.issue_id
        and existing.decision_journal == decision.journal_id
        and existing.continuation_source == continuation.source
        and existing.continuation_issue_id == continuation.issue_id
        and existing.continuation_journal == continuation.journal_id
        and existing.continuation_expected_gate == continuation.expected_gate
        and existing.continuation_next_action == continuation.next_semantic_action
        and existing.participants_manifest == participants_manifest
    )


# -- store -------------------------------------------------------------------


class ReplacementTransactionStore:
    """CAS store for one atomic self-replacement transaction (native ``state.sqlite``)."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self.path = path if path is not None else replacement_transaction_path(home)

    # -- schema / connections ------------------------------------------------

    def ensure_schema(self) -> None:
        """Create / validate this component's schema (see the schema module)."""
        ensure_replacement_transaction_schema(self.path)

    def _connect(self) -> sqlite3.Connection:
        """An autocommit connection for the CAS (the container guard's is not)."""
        ensure_replacement_transaction_schema(self.path)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn

    # -- reads ---------------------------------------------------------------

    def get(
        self, key: ReplacementTransactionKey
    ) -> Optional[ReplacementTransactionRecord]:
        """The transaction row, or ``None``. Raises when unreadable (fail closed)."""
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} "
                "WHERE workspace_id = ? AND action_id = ?",
                key.as_row(),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ReplacementTransactionError(
                f"replacement transaction read failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()
        return _record(row) if row is not None else None

    def records(self) -> tuple[ReplacementTransactionRecord, ...]:
        """Every row (the all-transaction diagnostic source). Raises when unreadable."""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} ORDER BY workspace_id, action_id"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ReplacementTransactionError(
                f"replacement transaction read failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()
        return tuple(_record(row) for row in rows)

    # -- writes (CAS) --------------------------------------------------------

    def plan_transaction(
        self,
        key: ReplacementTransactionKey,
        *,
        action_generation: int,
        decision: DecisionPointer,
        continuation: ContinuationPointer,
        participants: Sequence[ParticipantPin],
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Write the immutable transaction header at :data:`PHASE_PLANNED`, revision 1.

        The header — ``action_generation``, the decision + continuation pointers, and the
        participant identities — is fixed here and never mutated again (j#78384 §1). Every
        participant lands at :data:`PARTICIPANT_CLOSE_OWED`.

        Idempotent by exact header (the ``declare_lane`` precedent, the #13806 duplicate-
        invocation requirement): re-planning the **exact** same transaction — same
        generation, decision, continuation, and untouched participant manifest — is a no-op
        success. A row at the same key whose header **differs**, or whose participants have
        already advanced, is :data:`CAS_ALREADY_DECLARED` — a re-plan never silently
        overwrites an in-flight authority row. Every refusal is zero-write.
        """
        if not isinstance(action_generation, int) or isinstance(
            action_generation, bool
        ):
            raise ValueError("action_generation must be an integer")
        if action_generation < 1:
            raise ValueError("action_generation is a positive counter (>= 1)")
        # An unusable participant (missing identity/evidence), a duplicate identity, or more
        # than one self participant fails here, never stored (the manifest R1-F4 discipline).
        pinned = validate_participants(tuple(participants))
        encoded = encode_participants(pinned)
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = _locked_row(conn, key)
            if existing is not None:
                if _header_matches(
                    existing,
                    action_generation=action_generation,
                    decision=decision,
                    continuation=continuation,
                    participants_manifest=encoded,
                ):
                    conn.execute("ROLLBACK")
                    return CasOutcome(
                        applied=True,
                        reason=CAS_APPLIED,
                        revision=existing.revision,
                    )
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_ALREADY_DECLARED,
                    revision=existing.revision,
                )
            conn.execute(
                f"INSERT INTO {_TABLE} ({_COLUMNS}) "
                f"VALUES ({', '.join(['?'] * len(_COLUMNS.split(',')))})",
                (
                    key.workspace_id,
                    key.action_id,
                    action_generation,
                    PHASE_PLANNED,
                    1,
                    decision.source,
                    decision.issue_id,
                    decision.journal_id,
                    continuation.source,
                    continuation.issue_id,
                    continuation.journal_id,
                    continuation.expected_gate,
                    continuation.next_semantic_action,
                    encoded,
                    "",  # lease_holder
                    0,  # lease_epoch
                    "",  # lease_expires_at
                    stamp,
                    stamp,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=1)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise ReplacementTransactionError(
                f"replacement transaction plan failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def claim(
        self,
        key: ReplacementTransactionKey,
        *,
        expected_revision: int,
        holder: str,
        lease_expires_at: str,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Acquire the transaction lease for ``holder``, bumping its epoch.

        The lease is the mutual-exclusion token every effect re-checks (j#78384 §2 "各side
        effect直前にlease ownershipを再確認する"). A claim succeeds when the lease is free,
        **expired**, or already held by this same ``holder`` (a resuming holder re-claims);
        a *different, still-live* holder wins and this claim is refused
        :data:`CAS_LEASE_CONFLICT` with no write. ``holder`` is an action-bound identity
        token (the fresh coordinator's attested name); tranche A stores it, and the
        actuator that mints it is tranche C.
        """
        who = norm(holder)
        if not who:
            raise ValueError("a lease claim requires a non-empty holder token")
        expires = norm(lease_expires_at)
        if not expires:
            raise ValueError("a lease claim requires a non-empty expiry")
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if (
                current.lease_is_live(stamp)
                and current.lease_holder != who
            ):
                # A different holder owns a live lease — never steal it.
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_LEASE_CONFLICT,
                    revision=current.revision,
                )
            revision = current.revision + 1
            epoch = current.lease_epoch + 1
            conn.execute(
                f"UPDATE {_TABLE} SET lease_holder = ?, lease_epoch = ?, "
                "lease_expires_at = ?, revision = ?, updated_at = ? "
                "WHERE workspace_id = ? AND action_id = ? AND revision = ?",
                (
                    who,
                    epoch,
                    expires,
                    revision,
                    stamp,
                    key.workspace_id,
                    key.action_id,
                    current.revision,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise ReplacementTransactionError(
                f"replacement transaction claim failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def renew(
        self,
        key: ReplacementTransactionKey,
        *,
        expected_revision: int,
        holder: str,
        lease_expires_at: str,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Extend the lease expiry — only the current holder may renew.

        A caller that is not the current ``lease_holder`` is refused
        :data:`CAS_LEASE_NOT_HELD` (never silently re-acquires); the epoch is unchanged (a
        renewal is not a fresh acquisition).
        """
        who = norm(holder)
        expires = norm(lease_expires_at)
        if not expires:
            raise ValueError("a lease renew requires a non-empty expiry")
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if not who or current.lease_holder != who:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_LEASE_NOT_HELD,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET lease_expires_at = ?, revision = ?, updated_at = ? "
                "WHERE workspace_id = ? AND action_id = ? AND revision = ? "
                "AND lease_holder = ?",
                (
                    expires,
                    revision,
                    stamp,
                    key.workspace_id,
                    key.action_id,
                    current.revision,
                    who,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise ReplacementTransactionError(
                f"replacement transaction renew failed ({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()

    def release(
        self,
        key: ReplacementTransactionKey,
        *,
        expected_revision: int,
        holder: str,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Release the lease — only the current holder may release.

        Clears the holder + expiry (keeping the epoch, so a later claim still moves it
        forward). A non-holder is refused :data:`CAS_LEASE_NOT_HELD`, zero-write.
        """
        who = norm(holder)
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if not who or current.lease_holder != who:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_LEASE_NOT_HELD,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET lease_holder = '', lease_expires_at = '', "
                "revision = ?, updated_at = ? "
                "WHERE workspace_id = ? AND action_id = ? AND revision = ? "
                "AND lease_holder = ?",
                (
                    revision,
                    stamp,
                    key.workspace_id,
                    key.action_id,
                    current.revision,
                    who,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise ReplacementTransactionError(
                f"replacement transaction release failed ({type(exc).__name__}); "
                f"fail closed"
            ) from exc
        finally:
            conn.close()

    def transition_phase(
        self,
        key: ReplacementTransactionKey,
        *,
        expected_revision: int,
        target: str,
        holder: str,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Advance the transaction phase, held by the current lease holder.

        A phase move is a transaction effect, so it re-checks lease ownership (j#78384 §2):
        a caller that is not the current **live** ``holder`` is refused
        :data:`CAS_LEASE_NOT_HELD`, and an illegal edge (:func:`transaction_transition_allowed`)
        is :data:`CAS_FORBIDDEN_TRANSITION`. A duplicate transition simply fails the exact
        expected-revision guard, so a resuming holder re-reads the phase and continues.
        """
        if target not in TRANSACTION_PHASES:
            raise ValueError(f"unknown transaction phase {target!r}")
        who = norm(holder)
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if not who or current.lease_holder != who or not current.lease_is_live(stamp):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_LEASE_NOT_HELD,
                    revision=current.revision,
                )
            if not transaction_transition_allowed(current.phase, target):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET phase = ?, revision = ?, updated_at = ? "
                "WHERE workspace_id = ? AND action_id = ? AND revision = ?",
                (
                    target,
                    revision,
                    stamp,
                    key.workspace_id,
                    key.action_id,
                    current.revision,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise ReplacementTransactionError(
                f"replacement transaction phase move failed ({type(exc).__name__}); "
                f"fail closed"
            ) from exc
        finally:
            conn.close()

    def transition_participant(
        self,
        key: ReplacementTransactionKey,
        *,
        expected_revision: int,
        identity: tuple[str, str, str, str],
        target: str,
        holder: str,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Record a participant's owed phase, held by the current lease holder.

        The owed progression ``close_owed -> launch_owed -> verify_owed -> replaced`` with
        its retry self-loops (:func:`participant_transition_allowed`) is the partial-replay
        fence: "effect前に次のowed stateをCAS記録する" (j#78384 §2), so a close-then-crash
        resumes at ``launch_owed`` and never re-closes. Only the participant's **phase**
        moves — its identity manifest is preserved byte-for-byte, so a transition can never
        re-identify a participant.

        Refusals are zero-write: a non-holder / expired lease is :data:`CAS_LEASE_NOT_HELD`,
        an unknown participant is :data:`CAS_PARTICIPANT_NOT_FOUND`, an illegal edge is
        :data:`CAS_FORBIDDEN_TRANSITION`, and a stale caller is :data:`CAS_STALE_REVISION`.
        """
        if target not in PARTICIPANT_PHASES:
            raise ValueError(f"unknown participant phase {target!r}")
        who = norm(holder)
        wanted = tuple(norm(part) for part in identity)
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if not who or current.lease_holder != who or not current.lease_is_live(stamp):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_LEASE_NOT_HELD,
                    revision=current.revision,
                )
            pins = current.participants
            match = next((p for p in pins if p.identity == wanted), None)
            if match is None:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_PARTICIPANT_NOT_FOUND,
                    revision=current.revision,
                )
            if not participant_transition_allowed(match.phase, target):
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_FORBIDDEN_TRANSITION,
                    revision=current.revision,
                )
            updated = tuple(
                p.with_phase(target) if p.identity == wanted else p for p in pins
            )
            revision = current.revision + 1
            conn.execute(
                f"UPDATE {_TABLE} SET participants_manifest = ?, revision = ?, "
                "updated_at = ? WHERE workspace_id = ? AND action_id = ? AND revision = ?",
                (
                    encode_participants(updated),
                    revision,
                    stamp,
                    key.workspace_id,
                    key.action_id,
                    current.revision,
                ),
            )
            conn.execute("COMMIT")
            return CasOutcome(applied=True, reason=CAS_APPLIED, revision=revision)
        except sqlite3.DatabaseError as exc:
            _rollback(conn)
            raise ReplacementTransactionError(
                f"replacement transaction participant move failed "
                f"({type(exc).__name__}); fail closed"
            ) from exc
        finally:
            conn.close()


# -- module-level read wrappers (fail closed) --------------------------------


def load_replacement_transactions(
    *, home: Path | None = None
) -> Optional[tuple[ReplacementTransactionRecord, ...]]:
    """Every transaction row, or ``None`` when the store is unusable (fail closed)."""
    try:
        return ReplacementTransactionStore(home=home).records()
    except (ReplacementTransactionError, OSError):
        return None


def load_replacement_transactions_readonly(
    *, home: Path | None = None
) -> Optional[tuple[ReplacementTransactionRecord, ...]]:
    """Every transaction row via a **non-creating** read (the #13681 R2-F2 mirror).

    Unlike :func:`load_replacement_transactions` (which opens read-write and runs the
    schema ensure, creating the container / table when absent), this never writes: an
    absent state file, or an existing store with no replacement-transaction component yet,
    yields ``()``. It honours the same downgrade guard as the write path — an unknown /
    newer / malformed / partial component schema yields ``None`` (fail closed).
    """
    path = replacement_transaction_path(home)
    if not path.exists():
        return ()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return None
    try:
        status = readonly_component_status(conn)
        if status == READONLY_COMPONENT_ABSENT:
            return ()
        if status != READONLY_COMPONENT_RECOGNIZED:
            return None
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM {_TABLE} ORDER BY workspace_id, action_id"
        ).fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    return tuple(_record(row) for row in rows)


__all__ = (
    "ReplacementTransactionStore",
    "ReplacementTransactionError",
    "load_replacement_transactions",
    "load_replacement_transactions_readonly",
    "replacement_transaction_path",
    "ensure_replacement_transaction_schema",
    "REPLACEMENT_TRANSACTION_COMPONENT",
    "REPLACEMENT_TRANSACTION_RECOVERY_POLICY",
    "REPLACEMENT_TRANSACTION_SCHEMA_VERSION",
    # re-exported from the model so the component has one import surface
    "CAS_ALREADY_DECLARED",
    "CAS_APPLIED",
    "CAS_FORBIDDEN_TRANSITION",
    "CAS_LEASE_CONFLICT",
    "CAS_LEASE_NOT_HELD",
    "CAS_NOT_FOUND",
    "CAS_PARTICIPANT_NOT_FOUND",
    "CAS_STALE_REVISION",
    "CasOutcome",
    "ContinuationPointer",
    "DecisionPointer",
    "ParticipantPin",
    "ReplacementTransactionKey",
    "ReplacementTransactionRecord",
    "decode_participants",
    "encode_participants",
    "participant_transition_allowed",
    "transaction_transition_allowed",
    "validate_participants",
)
