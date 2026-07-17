"""Pure decode + validation of a startup-action authority row (Redmine #13948).

Split from :mod:`startup_transaction_fence` (review j#81202 correction) as the module's
module-health reduction: the byte-exact row contract grew large enough — validating every
cell as a typed authority shape rather than a lenient decode — to earn its own home
alongside the fence I/O it serves. The fence imports these lazily (inside ``read``) so this
value layer and the I/O layer stay a one-way dependency with no import cycle.

Every function here fails closed by raising :class:`StartupTransactionError`: a row read
back from the store is byte-exact authority (Answer j#80989 Q1/Q3) or it is unreadable —
there is no lenient middle that closes, or forgets, panes.
"""

from __future__ import annotations

import json

from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASES,
    STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION,
    Participant,
    StartupAction,
    StartupTransactionError,
    StartupUnit,
    _PARTICIPANT_KEYS,
    canonical_providers,
)


def _require_text(value: object, action_id: str, field: str) -> str:
    """A cell the schema declares NOT NULL identity text. Corrupt if not a clean token.

    NULL / non-string is corrupt (``NULL`` slipping through as an empty string is how a
    byte-exact workspace / lane was lost — R5-F1). A value carrying surrounding whitespace
    is corrupt too (R6-F1): these are canonical identity tokens, and a " ws1 " read
    verbatim would silently mismatch every derived name. ``strip()`` here is a validation
    comparison, never a mutation — the stored bytes are returned unchanged.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise StartupTransactionError(
            f"startup action {action_id!r} field {field!r} is missing, empty, or has "
            f"surrounding whitespace ({value!r}); the authority row is malformed"
        )
    return value


def _row_to_action(row) -> StartupAction:
    """Validate a row as a versioned authority record, field by field (fail-closed).

    Every cell is a strict typed contract, not a value to coerce (review j#81166 R5-F1).
    The earlier "validate" only rejected the shapes that happened to crash — it still let
    ``participants`` NULL/"" read as an empty set, ``closed="false"`` coerce to ``True``,
    ``workspace_id`` NULL pass, and ``revision=1.5`` truncate. Each of those turned a
    CORRUPT authority row into a plausible "all participants absent" record, so the public
    rail erased a real rollback debt into a terminal ``completed_rolled_back``. A row read
    from the store is byte-exact authority (j#80989 Q1/Q3) or it is unreadable; there is
    no lenient middle that closes — or forgets — panes.
    """
    if row is None or len(row) != 9:
        raise StartupTransactionError(
            "a startup action row does not have the 9 expected columns; malformed"
        )
    action_id = _require_text(row[0], "<unknown>", "action_id")
    workspace_id = _require_text(row[1], action_id, "workspace_id")
    lane_id = _require_text(row[2], action_id, "lane_id")
    providers_cell = _require_text(row[3], action_id, "providers")
    phase = row[4]
    revision_cell = row[5]
    participants_cell = row[6]
    _require_text(row[7], action_id, "reserved_at")
    _require_text(row[8], action_id, "updated_at")

    if phase not in PHASES:
        raise StartupTransactionError(
            f"startup action {action_id!r} has an unknown phase {phase!r}; a corrupt "
            "phase is an unreadable authority, not a no-op action"
        )
    # revision must be an EXACT integer — a stored float (1.5) truncating to 1 is silent
    # authority drift, so bool / float / non-numeric string are all rejected (bool is an
    # int subclass, hence the explicit guard).
    if isinstance(revision_cell, bool) or not isinstance(revision_cell, int):
        raise StartupTransactionError(
            f"startup action {action_id!r} has a non-integer revision {revision_cell!r}; "
            "the authority row is malformed"
        )
    revision = revision_cell

    if not isinstance(participants_cell, str):
        raise StartupTransactionError(
            f"startup action {action_id!r} participants cell is not text "
            f"({type(participants_cell).__name__}); a NULL / non-text cell is malformed, "
            "not an empty participant set"
        )
    try:
        raw_participants = json.loads(participants_cell)
    except (TypeError, ValueError) as exc:
        raise StartupTransactionError(
            f"startup action {action_id!r} has a participants cell that is not JSON; "
            "the authority row is malformed"
        ) from exc
    if not isinstance(raw_participants, list):
        raise StartupTransactionError(
            f"startup action {action_id!r} participants is not a JSON array "
            f"({type(raw_participants).__name__}); the authority row is malformed"
        )
    participants = tuple(
        Participant.strict_from_payload(entry, action_id) for entry in raw_participants
    )
    # The providers cell is the canonical comma-join of a non-empty, sorted, unique set
    # (`",".join(canonical_providers(...))`). A trailing comma / empty segment / duplicate
    # is drift that `split(...) if p` used to silently repair (review j#81202 R6-F1), so it
    # is validated against the exact serialization instead of filtered.
    provider_segments = providers_cell.split(",")
    if any(not seg for seg in provider_segments):
        raise StartupTransactionError(
            f"startup action {action_id!r} providers cell {providers_cell!r} has an empty "
            "segment; the authority row is malformed"
        )
    providers = tuple(provider_segments)
    if providers_cell != ",".join(canonical_providers(providers)):
        raise StartupTransactionError(
            f"startup action {action_id!r} providers cell {providers_cell!r} is not the "
            "canonical sorted-unique serialization; the authority row is malformed"
        )
    return StartupAction(
        action_id=action_id,
        unit=StartupUnit(
            workspace_id=workspace_id,
            lane_id=lane_id,
            providers=providers,
        ),
        phase=phase,
        revision=revision,
        participants=participants,
        reserved_at=row[7],
        updated_at=row[8],
    )


__all__ = ("_require_text", "_row_to_action")
