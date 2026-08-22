"""How ONE pending row's persistence state advances: atomically, checked, and sealed.

Split out of :mod:`mozyo_bridge.core.state.stall_escalation` under the module-health gate
(``vibes/docs/logics/module-health-gate.md``), along a seam review j#110254 made obvious:
the store is SQL plumbing, and "what a transition is allowed to do" is a rule. Every rule
here is CONSUMED from :mod:`mozyo_bridge.core.state.stall_pending_contract` — the column
set, the per-field grammar, and the seal derivation are all imported, never restated. That
is the whole discipline this module exists to hold: the finding it answers was three faces
of one rule disagreeing because two of them were written a second time by hand.

Two properties every transition here has, and neither is optional:

- **Read-modify-write, in the transaction.** ``attempts=attempts+1`` computed in SQL means
  Python never sees the value it is about to seal, and a seal over values you did not see
  is not a seal.
- **No laundering.** A row that does not currently satisfy the row contract is refused.
  Writing a freshly derived seal over tampered values would turn the store into the
  forger's accomplice on the next ordinary pass.
"""

from __future__ import annotations

from typing import Callable, Optional

from mozyo_bridge.core.state.stall_pending_contract import (
    COUNT_MAX,
    PENDING_FIELD_CHECKERS,
    ROW_SEAL_FIELDS,
    StallPendingContractError,
    pending_row_seal,
)

#: The columns a transition may change. The seal covers MORE than these — every column the
#: identity key does not — so a transition recomputes it from the row it just read plus the
#: values it is about to write, never from its own changes alone.
TRANSITION_FIELDS: tuple[str, ...] = (
    "journal_id", "written_at", "woke_at", "attempts", "last_attempt_at", "last_reason",
)

#: ``(current_row) -> next persistence state``, or ``None`` to refuse the transition.
TransitionPlan = Callable[[object], Optional[dict]]


def bumped_attempts(attempts: object) -> int:
    """One more attempt, saturating at the count grammar's declared ceiling.

    Saturation rather than an unbounded increment, because the alternative is a counter that
    eventually leaves its own grammar and quarantines a row for the crime of having been
    refused too many times. At the watcher's cadence the ceiling is roughly a year of
    uninterrupted refusals on one firing, and ``last_reason`` keeps updating past it, so
    nothing an operator reads is lost. The saturation point is asserted by test rather than
    left as a comment.
    """
    return min(int(attempts) + 1, COUNT_MAX)


def apply_sealed_transition(
    conn,
    *,
    idempotency_key: str,
    select_sql: str,
    row_reader,
    plan: TransitionPlan,
    sql_guard: str = "",
) -> bool:
    """Advance one row's persistence state inside an open transaction.

    ``select_sql`` reads the row's full column set; ``row_reader`` lifts it into the value
    object (so the integrity verdict this refuses on is the SAME verdict every read surface
    computes, not a second opinion). ``sql_guard`` is an additional SQL-side fence, so a
    predicate the caller wants held holds even if the Python path is ever bypassed.
    """
    key = str(idempotency_key or "")
    if not key:
        return False
    raw = conn.execute(select_sql, (key,)).fetchone()
    if raw is None:
        return False
    current = row_reader(raw)
    # The laundering fence. A quarantined row is preserved and reported; it is never
    # advanced, and never re-sealed.
    if not getattr(current, "externally_writable", False):
        return False
    planned = plan(current)
    if planned is None:
        return False
    try:
        values = {
            name: PENDING_FIELD_CHECKERS[name](planned[name])
            for name in TRANSITION_FIELDS
        }
    except StallPendingContractError:
        # A refused WRITE, not a stored bad value: the firing stays exactly where it was,
        # which for an unrecorded firing means it stays retryable (review j#110254).
        return False
    # The seal covers the whole non-identity row, so it is recomputed from the row as it
    # will be AFTER this transition: the columns being written, plus the ones this
    # transition leaves alone. Sealing only the changed columns would leave every other
    # column rewritable, which is the exact shape of the last three rounds' findings.
    sealed = {name: getattr(current, name, "") for name in ROW_SEAL_FIELDS}
    sealed.update(values)
    seal = pending_row_seal(idempotency_key=key, values=sealed)
    assignments = ", ".join(f"{name}=?" for name in TRANSITION_FIELDS)
    guard = f" AND ({sql_guard})" if sql_guard else ""
    cursor = conn.execute(
        f"UPDATE stall_escalation_pending SET {assignments}, row_seal=? "
        f"WHERE idempotency_key=?{guard}",
        (*(values[name] for name in TRANSITION_FIELDS), seal, key),
    )
    return cursor.rowcount > 0


def plan_attempt(*, reason: str, stamp: str) -> TransitionPlan:
    """Count one refused / failed write attempt, with its reason. Unrecorded rows only."""

    def plan(current) -> Optional[dict]:
        if current.journal_id:
            return None
        return {
            "journal_id": current.journal_id,
            "written_at": current.written_at,
            "woke_at": current.woke_at,
            "attempts": bumped_attempts(current.attempts),
            "last_attempt_at": stamp,
            "last_reason": reason,
        }

    return plan


def plan_recorded(*, journal_id: str, stamp: str) -> TransitionPlan:
    """Bind a firing to the journal that carries it. Refused once one is already bound.

    The only thing checked here is the one thing the column grammar CANNOT say: ``""`` is a
    perfectly valid stored ``journal_id`` (every unrecorded row has one), but "record this
    firing against no journal at all" is not a transition. Whether a non-empty id is
    well-formed is the table's question, asked once in :func:`apply_sealed_transition`;
    re-asking it here was provably equivalent, and a duplicate check nobody can measure is
    how the faces drifted apart in the first place.
    """

    def plan(current) -> Optional[dict]:
        if current.journal_id or not journal_id:
            return None
        return {
            "journal_id": journal_id,
            "written_at": stamp,
            "woke_at": current.woke_at,
            "attempts": bumped_attempts(current.attempts),
            "last_attempt_at": stamp,
            "last_reason": "",
        }

    return plan


def plan_woken(*, stamp: str) -> TransitionPlan:
    """Mark the coordinator wake. Refused unless the row carries a canonical journal id."""

    def plan(current) -> Optional[dict]:
        if current.woke_at or not current.recorded:
            return None
        return {
            "journal_id": current.journal_id,
            "written_at": current.written_at,
            "woke_at": stamp,
            "attempts": current.attempts,
            "last_attempt_at": current.last_attempt_at,
            "last_reason": current.last_reason,
        }

    return plan


__all__ = (
    "TRANSITION_FIELDS",
    "TransitionPlan",
    "apply_sealed_transition",
    "bumped_attempts",
    "plan_attempt",
    "plan_recorded",
    "plan_woken",
)
