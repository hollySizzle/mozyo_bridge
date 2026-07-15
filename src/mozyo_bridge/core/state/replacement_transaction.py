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
    CAS_GENERATION_MISMATCH,
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
    participant_actuation_phase_allowed,
    participant_transition_allowed,
    transaction_phase_prerequisite_met,
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


def _require_exact_generation(value: object) -> int:
    """Validate an effect's ``expected_action_generation`` as a positive exact int.

    An authority token is never compared by Python numeric equality (Redmine #13806 R2-F2):
    ``7 == 7.0`` and ``1 == True`` fold, so a ``float`` / ``bool`` / string generation token
    would slip through a bare ``!=`` fence and be accepted as an approved exact generation.
    This rejects any non-``int`` (``bool`` is an ``int`` subclass and is not a generation) or
    non-positive value BEFORE any DB read/write, mirroring ``plan_transaction``'s
    ``action_generation`` validation and the manifest / component-version bool-float
    fail-closed discipline. Raising (not a zero-write outcome) is deliberate: a malformed
    token is a caller type error, exactly as ``plan_transaction`` treats a malformed
    ``action_generation`` — distinct from a well-formed but *stale* generation, which is the
    :data:`CAS_GENERATION_MISMATCH` zero-write path.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            "expected_action_generation must be an exact integer, "
            f"got {type(value).__name__}"
        )
    if value < 1:
        raise ValueError("expected_action_generation is a positive counter (>= 1)")
    return value


def _is_pristine_replan(
    existing: ReplacementTransactionRecord,
    *,
    action_generation: int,
    decision: DecisionPointer,
    continuation: ContinuationPointer,
    participants_manifest: str,
) -> bool:
    """Is ``existing`` a *genuinely untouched* exact-header duplicate? (the idempotency test)

    Idempotent no-op requires BOTH an exact header/manifest match AND a pristine row
    (Redmine #13806 R1-F5): the row is still at :data:`PHASE_PLANNED`, at its initial
    revision 1, and holds no lease (``lease_holder`` empty, ``lease_epoch`` 0). The manifest
    match already asserts every participant is ``close_owed`` (a fresh plan's manifest is
    all-``close_owed``); the pristine gate additionally excludes a row that has been
    **claimed or phase-advanced** without moving a participant. Reporting an in-flight,
    lease-held authority row as an ``applied`` re-plan would let a caller mistake a claimed
    transaction for a fresh, untouched one — so anything short of pristine is
    :data:`CAS_ALREADY_DECLARED`, never a silent success.
    """
    header_matches = (
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
    pristine = (
        existing.phase == PHASE_PLANNED
        and existing.revision == 1
        and existing.lease_holder == ""
        and existing.lease_epoch == 0
    )
    return header_matches and pristine


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

        Idempotent by exact header on a **pristine** row (the ``declare_lane`` precedent, the
        #13806 duplicate-invocation requirement, R1-F5): re-planning the exact same
        transaction — same generation, decision, continuation, untouched manifest — is a
        no-op success ONLY while the row is still pristine (phase ``planned``, revision 1, no
        lease). A row whose header **differs**, or which has been claimed / phase-advanced /
        participant-advanced, is :data:`CAS_ALREADY_DECLARED` — a re-plan never silently
        overwrites, nor reports as fresh, an in-flight authority row. Every refusal is
        zero-write.
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
                if _is_pristine_replan(
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
        expected_action_generation: int,
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

        ``expected_action_generation`` pins the immutable approval generation (R1-F2): a
        caller acting on a stale / recycled action generation is refused
        :data:`CAS_GENERATION_MISMATCH`, zero-write. The row's generation never moves, so
        this proves the caller and the row agree on *which* approved action they are on —
        the concurrency ``revision`` cannot express that.
        """
        who = norm(holder)
        if not who:
            raise ValueError("a lease claim requires a non-empty holder token")
        expires = norm(lease_expires_at)
        if not expires:
            raise ValueError("a lease claim requires a non-empty expiry")
        _require_exact_generation(expected_action_generation)
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.action_generation != expected_action_generation:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_GENERATION_MISMATCH,
                    revision=current.revision,
                )
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
        expected_action_generation: int,
        holder: str,
        lease_expires_at: str,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Extend the lease expiry — only the current, **live** holder may renew.

        A renewal *extends a lease the caller still holds*; it is not a fresh acquisition
        (R1-F4). So a caller that is not the current ``lease_holder``, OR whose lease has
        already **expired**, is refused :data:`CAS_LEASE_NOT_HELD` — an expired holder must
        go through :meth:`claim` (which bumps the epoch and can be contested by a new
        holder), never resurrect a lapsed lease and thereby skip the epoch fence. The epoch
        is unchanged on a successful renew. ``expected_action_generation`` pins the immutable
        generation (R1-F2), refusing a stale-generation caller :data:`CAS_GENERATION_MISMATCH`.
        """
        who = norm(holder)
        expires = norm(lease_expires_at)
        if not expires:
            raise ValueError("a lease renew requires a non-empty expiry")
        _require_exact_generation(expected_action_generation)
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.action_generation != expected_action_generation:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_GENERATION_MISMATCH,
                    revision=current.revision,
                )
            if current.revision != expected_revision:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_STALE_REVISION,
                    revision=current.revision,
                )
            if (
                not who
                or current.lease_holder != who
                or not current.lease_is_live(stamp)
            ):
                # An expired holder must re-``claim`` (epoch bump), never renew a lapsed
                # lease — that would revive side-effect authority behind the epoch fence.
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
        expected_action_generation: int,
        holder: str,
        now: Optional[str] = None,
    ) -> CasOutcome:
        """Release the lease — only the current holder may release.

        Clears the holder + expiry (keeping the epoch, so a later claim still moves it
        forward). A non-holder is refused :data:`CAS_LEASE_NOT_HELD`, zero-write. Releasing a
        lease is giving up authority, so it is allowed for the recorded holder even after
        expiry (it never revives authority). ``expected_action_generation`` pins the immutable
        generation (R1-F2).
        """
        who = norm(holder)
        _require_exact_generation(expected_action_generation)
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.action_generation != expected_action_generation:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_GENERATION_MISMATCH,
                    revision=current.revision,
                )
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
        expected_action_generation: int,
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

        It also enforces the **cross-axis prerequisite** (R1-F1 / R2-F1,
        :func:`transaction_phase_prerequisite_met`): the transaction may not leave
        ``replacing_nonself`` until every non-self participant is ``replaced``; may not enter
        ``fresh_coordinator_claimed`` until the self participant exists and is ``replaced``
        (a fresh coordinator only claims after the old self is closed, relaunched, and
        attested); and may not reach ``completed`` until *all* participants are. So
        ``completed`` with an unfinished participant, a self-close before the non-self
        participants, or a ``fresh_coordinator_claimed`` with an un-replaced self, is
        unrepresentable. A prerequisite miss is :data:`CAS_FORBIDDEN_TRANSITION`, zero-write.
        ``expected_action_generation`` pins the immutable generation (R1-F2), validated as a
        positive exact ``int`` (R2-F2).
        """
        if target not in TRANSACTION_PHASES:
            raise ValueError(f"unknown transaction phase {target!r}")
        _require_exact_generation(expected_action_generation)
        who = norm(holder)
        stamp = now or _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _locked_row(conn, key)
            if current is None:
                conn.execute("ROLLBACK")
                return CasOutcome(applied=False, reason=CAS_NOT_FOUND)
            if current.action_generation != expected_action_generation:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_GENERATION_MISMATCH,
                    revision=current.revision,
                )
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
            if not transaction_phase_prerequisite_met(current.participants, target):
                # Cross-axis ordering: e.g. -> completed while a participant is un-replaced.
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
        expected_action_generation: int,
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

        It also enforces the **cross-axis actuation gate** (R1-F1 / R2-F1,
        :func:`participant_actuation_phase_allowed`): a non-self participant may be actuated
        only while the transaction is ``replacing_nonself``, and the self (current
        coordinator) participant only while the transaction is ``self_close_armed`` — that
        one armed window only, never after. The old self is closed, relaunched, and attested
        entirely within ``self_close_armed``; by the time the transaction enters
        ``fresh_coordinator_claimed`` the self is already ``replaced`` (its prerequisite), so
        the current coordinator is always replaced last and is never advanced past its window.
        A move outside the participant's allowed transaction phase is
        :data:`CAS_FORBIDDEN_TRANSITION`, zero-write.

        Refusals are zero-write: a non-holder / expired lease is :data:`CAS_LEASE_NOT_HELD`,
        a stale generation is :data:`CAS_GENERATION_MISMATCH` (R1-F2), an unknown participant
        is :data:`CAS_PARTICIPANT_NOT_FOUND`, an illegal owed edge or a wrong-phase actuation
        is :data:`CAS_FORBIDDEN_TRANSITION`, and a stale caller is :data:`CAS_STALE_REVISION`.
        """
        if target not in PARTICIPANT_PHASES:
            raise ValueError(f"unknown participant phase {target!r}")
        _require_exact_generation(expected_action_generation)
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
            if current.action_generation != expected_action_generation:
                conn.execute("ROLLBACK")
                return CasOutcome(
                    applied=False,
                    reason=CAS_GENERATION_MISMATCH,
                    revision=current.revision,
                )
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
            if not participant_actuation_phase_allowed(match.is_self, current.phase):
                # Cross-axis ordering: a non-self participant may move only in
                # ``replacing_nonself``; the self participant only once self-close is armed.
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
    "CAS_GENERATION_MISMATCH",
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
    "participant_actuation_phase_allowed",
    "participant_transition_allowed",
    "transaction_phase_prerequisite_met",
    "transaction_transition_allowed",
    "validate_participants",
)
