"""Optional additive startup-execution-event projection (Redmine #14231).

Design Consultation Answer (j#84724, on Redmine #14222 j#84721): a fresh v2-config
managed sublane launch could leave BOTH Codex and Claude classified ``provider_exited``
by :mod:`.herdr_startup_health` — a liveness-only verdict (the locator's row vanished
from live inventory) that keeps no exit code, stderr, or stage. Two durable-store
options were considered and rejected before this one:

- Extending :mod:`.startup_transaction_fence`'s ``startup_actions`` / ``Participant``
  schema: that store's schema version is an EXACT-match gate (``_verify``) and its
  participant payload is an exact-key-set contract (``strict_from_payload``) hardened
  through 7+ adversarial review rounds. A schema bump would make every existing v1
  store (including any of #14222's grandfathered active lanes with an unterminated
  rollback-owed action at cutover) unreadable — blast radius disproportionate to this
  diagnostic's value.
- Putting a stage on :mod:`.herdr_identity_attestation`: rejected on its own terms — a
  ``present`` record only proves the attestation write landed, not that the
  post-attestation argv0 validation + ``exec`` succeeded; the store is a per-assigned-name
  snapshot-replace *cache*, not an action timeline; and a write failure cannot be
  recorded through the same store whose write just failed.

This module is the third option: a SIBLING table, ``startup_execution_events``, living
in the SAME on-disk store as ``startup_actions`` (:func:`.startup_transaction_fence.
startup_transaction_fence_path`) but entirely OPTIONAL and ADDITIVE:

- it is never checked by :meth:`.startup_transaction_fence.StartupTransactionFence.
  _verify_shape` (not in ``_EXPECTED_COLUMNS``), so a store that predates this feature —
  or a store a not-yet-upgraded launcher of a different vintage writes to (Redmine
  #13882 mixed-runtime concern) — reads exactly as it always has; ``startup_actions``
  rollback / replay / participant behavior is untouched, byte-for-byte;
- it carries NO rollback / close / liveness authority. Reading it absent, or a table
  that does not exist yet, must NEVER be read as proof the wrapper never ran — the only
  authority for "what this action started" and "what a rollback may close" stays
  :mod:`.startup_transaction_fence` exclusively;
- writes are split into two postures matching where they run: the table's own
  existence is a **preflight** the launcher proves right after ``reserve()`` and before
  the first Herdr side effect (:func:`ensure_execution_events_table`, RAISES — a
  preflight failure is a reserve-time failure, zero-actuation, never a silent gap);
  a per-stage **append** during the actual wrapper run
  (:func:`append_execution_event`) is best-effort / never-raises, matching the
  wrapper's existing never-block-the-boot contract (mirrors
  :func:`.herdr_identity_attestation.record_identity_attestation`) — an append failure
  must not stop the provider boot, and the post-launch join treats missing evidence as
  its own typed gap (``evidence_incomplete`` / :data:`REASON_STARTUP_EVIDENCE_UNAVAILABLE`),
  never silently strengthened into "wrapper never ran" or "provider exited".

Event vocabulary (bounded, value-free tokens only — no path / env value / pane text /
stderr body / credential is ever stored, matching the sibling attestation store's
privacy contract): :data:`EXECUTION_EVENT_STAGES`. The wrapper appends them in order as
its own control flow reaches each point (module docstring order mirrors
``herdr_agent_attest.cmd_herdr_agent_attest``'s real call sequence: entered -> self
lookup -> attestation write -> exec). ``provider_exec_call_reached`` is evidence that
control flow reached the ``exec`` call, never a claim that the provider executed even
one instruction — the wrapper cannot write anything AFTER a successful ``exec`` (it has
replaced itself), so live provider confirmation only ever comes from joining this event
against a live inventory observation (:func:`classify_startup_evidence`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence

from mozyo_bridge.core.state.startup_transaction_fence import (
    STORE_DAMAGED,
    StartupTransactionBusy,
    StartupTransactionError,
    StartupTransactionFence,
    _norm,
    _utc_now,
)

#: The optional additive table's own format version (distinct from
#: ``STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION``, which gates ``startup_actions`` /
#: ``store_meta`` only). Stamped on every row so a future format change can be told
#: apart from this one without touching the mandatory ``_verify_shape`` gate at all.
#: v2 (Redmine #14222 review j#85125 F1): rows carry the PARTICIPANT identity (the
#: assigned name) so a two-provider action's stages never blur into one shared
#: timeline. v1 rows (no ``participant`` column / empty value) stay readable as
#: UNATTRIBUTED — they are never guessed onto a participant of a multi-participant
#: action (see :func:`scope_events_to_participant`).
STARTUP_EXECUTION_EVENTS_FORMAT_VERSION = 2

EXECUTION_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS startup_execution_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    bounded_reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    format_version INTEGER NOT NULL,
    participant TEXT NOT NULL DEFAULT ''
)
"""

#: Additive column upgrade for a v1 table (created before ``participant`` existed).
#: Applied opportunistically by the write paths; a failure to add the column leaves the
#: table exactly as it was (the append then fails its own way, still never raising).
_PARTICIPANT_COLUMN_SQL = (
    "ALTER TABLE startup_execution_events ADD COLUMN participant TEXT NOT NULL DEFAULT ''"
)


def _table_has_participant(conn: sqlite3.Connection) -> bool:
    """Whether the (existing) events table carries the v2 ``participant`` column."""
    columns = conn.execute("PRAGMA table_info(startup_execution_events)").fetchall()
    return any(row[1] == "participant" for row in columns)


def _ensure_participant_column(conn: sqlite3.Connection) -> None:
    """Upgrade a v1 table in place (idempotent; raises on a genuinely failed ALTER)."""
    if not _table_has_participant(conn):
        conn.execute(_PARTICIPANT_COLUMN_SQL)

# --- Event stage vocabulary (Design Consultation Answer j#84724). Bounded tokens ----
# only; a `:<bounded_reason>` suffix in the consultation answer is this module's
# separate `bounded_reason` column, never concatenated into the stage token itself, so
# the stage set stays closed and enumerable.
STAGE_WRAPPER_ENTERED = "wrapper_entered"
STAGE_SELF_LOOKUP_STARTED = "self_lookup_started"
STAGE_SELF_LOOKUP_SUCCEEDED = "self_lookup_succeeded"
STAGE_SELF_LOOKUP_TIMED_OUT = "self_lookup_timed_out"
STAGE_SELF_LOOKUP_FAILED = "self_lookup_failed"
STAGE_ATTESTATION_WRITE_SUCCEEDED = "attestation_write_succeeded"
STAGE_ATTESTATION_WRITE_FAILED = "attestation_write_failed"
STAGE_PROVIDER_EXEC_CALL_REACHED = "provider_exec_call_reached"
STAGE_PROVIDER_EXEC_REJECTED = "provider_exec_rejected"
STAGE_PROVIDER_EXEC_FAILED = "provider_exec_failed"

#: The closed, ordered set of recognized stage tokens. An unrecognized stage is refused
#: by :func:`append_execution_event` (fail-closed on the CALLER's mistake, still
#: never-raises) rather than landing an un-enumerable value in the projection.
EXECUTION_EVENT_STAGES: tuple[str, ...] = (
    STAGE_WRAPPER_ENTERED,
    STAGE_SELF_LOOKUP_STARTED,
    STAGE_SELF_LOOKUP_SUCCEEDED,
    STAGE_SELF_LOOKUP_TIMED_OUT,
    STAGE_SELF_LOOKUP_FAILED,
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    STAGE_ATTESTATION_WRITE_FAILED,
    STAGE_PROVIDER_EXEC_CALL_REACHED,
    STAGE_PROVIDER_EXEC_REJECTED,
    STAGE_PROVIDER_EXEC_FAILED,
)
_EXECUTION_EVENT_STAGE_SET: frozenset[str] = frozenset(EXECUTION_EVENT_STAGES)

# --- Derived join / evidence-completeness vocabulary (read side). -------------------
#: The exec call was reached AND a live inventory observation confirms the locator —
#: the strongest positive evidence this module can produce (still not "the provider
#: ran a full turn", only "started and is currently live").
JOIN_PROVIDER_LIVE_CONFIRMED = "provider_live_confirmed"
#: The exec call was reached but the live locator is not observed in a readable
#: inventory — the exec call happened and the process is not there now. Deliberately
#: NOT named ``provider_exited``: whether that is the provider exiting or the wrapper
#: failing between ``exec`` and process registration is not resolvable from this
#: evidence alone.
JOIN_POST_EXEC_LOCATOR_ABSENT = "post_exec_locator_absent"
#: The live inventory itself could not be read — never conflated with "absent".
JOIN_INVENTORY_UNREADABLE = "inventory_unreadable"
#: The exec call was never reached (or no events exist at all) — the join has nothing
#: to say about provider liveness; the caller reads ``last_stage`` for what evidence
#: does exist.
JOIN_NOT_APPLICABLE = "not_applicable"

#: No execution-events row exists for this action at all (table absent, or a read
#: table with zero matching rows) — genuinely no evidence, distinct from evidence that
#: stopped partway. Never treated as proof of "wrapper never invoked" (the append path
#: is best-effort and can itself be lost) — surfaced as its own honest gap.
STAGE_NO_EVIDENCE = "no_evidence"

#: Public post-launch-gate fail-closed reason (Design Consultation Answer j#84724):
#: the projection is missing / unreadable when a caller needed it to confirm a launch,
#: and the gap itself — not a false "wrapper never ran" — is what is reported.
REASON_STARTUP_EVIDENCE_UNAVAILABLE = "startup_evidence_unavailable"

#: Legacy (v1, pre-participant) rows exist for this action but cannot be attributed to
#: THIS participant of a multi-participant action (Redmine #14222 j#85125 F1). Reported
#: as its own honest gap — never guessed onto a participant, never silently read as
#: "no evidence at all".
REASON_STARTUP_EVIDENCE_UNATTRIBUTED = "startup_evidence_unattributed"


@dataclass(frozen=True)
class ExecutionEvent:
    """One append-only row of the optional startup-execution-event projection.

    Diagnostic only — never authority. ``bounded_reason`` is a fixed, enumerable token
    (mirroring the sibling stores' ``detail`` / reason conventions), never free text,
    a path, an env value, pane body, or a credential.
    """

    sequence: int
    action_id: str
    stage: str
    bounded_reason: str
    recorded_at: str
    format_version: int
    #: The participant (assigned name) this stage belongs to. Empty on a v1 legacy row —
    #: an UNATTRIBUTED event, handled conservatively by the read-side scoping.
    participant: str = ""

    def as_payload(self) -> dict:
        return {
            "sequence": self.sequence,
            "action_id": self.action_id,
            "stage": self.stage,
            "bounded_reason": self.bounded_reason,
            "recorded_at": self.recorded_at,
            "format_version": self.format_version,
            "participant": self.participant,
        }


def ensure_execution_events_table(fence: StartupTransactionFence, action_id: str) -> None:
    """Preflight: prove the optional events table is writable for ``action_id`` (raises).

    Called by the launcher immediately after :meth:`StartupTransactionFence.reserve`,
    before the first Herdr side effect (Design Consultation Answer j#84724). This is
    the ONE point where an evidence-projection failure is treated as fatal: a caller
    that cannot even create/open the table here should treat it as zero-actuation,
    exactly like every other reserve-before-effect failure in this store. Idempotent
    (``CREATE TABLE IF NOT EXISTS``) — safe to call once per action.

    Raises :class:`StartupTransactionError` (never a raw ``sqlite3`` / ``OSError``) on
    any failure, including ``action_id`` not being a reserved action in this store.
    """
    fence._require(action_id)
    with fence._hold():
        with fence._connection("rw") as conn:
            try:
                conn.execute(EXECUTION_EVENTS_TABLE_SQL)
                _ensure_participant_column(conn)
            except (sqlite3.DatabaseError, OSError) as exc:
                raise StartupTransactionError(
                    "the optional startup execution events table could not be "
                    f"ensured for action {action_id!r} ({exc}); fail closed before "
                    "any Herdr side effect"
                ) from exc


def append_execution_event(
    fence: StartupTransactionFence,
    action_id: str,
    stage: str,
    *,
    bounded_reason: str = "",
    participant: str = "",
) -> bool:
    """Best-effort append of one execution-stage event. Never raises.

    Returns ``True`` on a landed append, ``False`` on ANY failure — an unrecognized
    ``stage``, lock contention, a damaged/absent store, or a write error. An append
    failure must never stop the provider boot (the wrapper's existing never-block
    contract, mirrored from :func:`.herdr_identity_attestation.
    record_identity_attestation`); the read side treats missing/incomplete evidence as
    its own typed gap, never as proof of what did or did not happen.

    ``participant`` is the appending wrapper's own assigned name (Redmine #14222
    j#85125 F1) — the identity a multi-participant action's read side scopes by. An
    empty value is accepted (it lands an UNATTRIBUTED row, the v1 shape) so an older
    caller keeps its exact behavior, but every current wrapper passes its name.

    Creates the table on demand (idempotent) so a caller is never blocked on whether
    :func:`ensure_execution_events_table` happened to run first — this call is
    self-sufficient, matching its own best-effort contract.
    """
    token = _norm(stage)
    if token not in _EXECUTION_EVENT_STAGE_SET:
        return False
    normalized_id = _norm(action_id)
    if not normalized_id:
        return False
    try:
        with fence._hold():
            with fence._connection("rw") as conn:
                conn.execute(EXECUTION_EVENTS_TABLE_SQL)
                _ensure_participant_column(conn)
                conn.execute(
                    "INSERT INTO startup_execution_events "
                    "(action_id, stage, bounded_reason, recorded_at, format_version, "
                    "participant) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        normalized_id,
                        token,
                        _norm(bounded_reason),
                        _utc_now(),
                        STARTUP_EXECUTION_EVENTS_FORMAT_VERSION,
                        _norm(participant),
                    ),
                )
        return True
    except (StartupTransactionError, StartupTransactionBusy, sqlite3.DatabaseError, OSError):
        return False


def read_execution_events(
    fence: StartupTransactionFence, action_id: str
) -> Optional[tuple[ExecutionEvent, ...]]:
    """Read the ordered execution-stage events for one action. Never raises.

    Returns an empty tuple when the table exists but carries no row for
    ``action_id`` (a genuinely observed absence of evidence). Returns ``None`` when
    the table itself does not exist, or the store is absent / damaged / unreadable —
    callers MUST NOT conflate the two: ``None`` means "this store predates the
    feature, or the preflight never landed", an empty tuple means "the preflight
    landed and nothing was appended after it" (itself informative — see
    :func:`classify_startup_evidence`).
    """
    shape = fence.store_shape()
    if shape.absent or shape.state == STORE_DAMAGED:
        return None
    normalized_id = _norm(action_id)
    try:
        with fence._connection("ro") as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND "
                "name = 'startup_execution_events'"
            ).fetchone()
            if exists is None:
                return None
            # A v1 table (no `participant` column) reads as all-unattributed rows —
            # never rejected, never guessed (Redmine #14222 j#85125 F1 read compat).
            has_participant = _table_has_participant(conn)
            participant_col = "participant" if has_participant else "''"
            rows = conn.execute(
                "SELECT event_seq, stage, bounded_reason, recorded_at, format_version, "
                f"{participant_col} "
                "FROM startup_execution_events WHERE action_id = ? ORDER BY event_seq",
                (normalized_id,),
            ).fetchall()
    except (StartupTransactionError, sqlite3.DatabaseError, OSError):
        return None
    return tuple(
        ExecutionEvent(
            sequence=row[0],
            action_id=normalized_id,
            stage=row[1],
            bounded_reason=row[2],
            recorded_at=row[3],
            format_version=row[4],
            participant=_norm(row[5]),
        )
        for row in rows
    )


@dataclass(frozen=True)
class StartupEvidenceVerdict:
    """Pure classification of one action's execution evidence (read-side, no I/O).

    ``last_stage`` is the most-advanced recorded stage, or :data:`STAGE_NO_EVIDENCE`
    when the events read is ``None`` or empty. ``inventory_join`` is one of
    :data:`JOIN_PROVIDER_LIVE_CONFIRMED` / :data:`JOIN_POST_EXEC_LOCATOR_ABSENT` /
    :data:`JOIN_INVENTORY_UNREADABLE` / :data:`JOIN_NOT_APPLICABLE`. ``evidence_gap``
    is ``True`` whenever the projection itself is missing (table absent / unreadable
    store) — distinct from a genuinely empty, readable projection.
    """

    last_stage: str
    inventory_join: str
    evidence_gap: bool
    bounded_reason: str = ""

    def as_payload(self) -> dict:
        return {
            "last_stage": self.last_stage,
            "inventory_join": self.inventory_join,
            "evidence_gap": self.evidence_gap,
            "bounded_reason": self.bounded_reason,
        }


def scope_events_to_participant(
    events: Optional[Sequence[ExecutionEvent]],
    *,
    participant: str,
    sole_participant: bool,
) -> tuple[Optional[tuple[ExecutionEvent, ...]], bool]:
    """Scope one action's events to ONE participant (pure; Redmine j#85125 F1).

    Returns ``(scoped_events, unattributed_only)``:

    - ``None`` events (projection unreadable) pass through as ``(None, False)`` — the
      evidence-gap classification is the classifier's, not this helper's.
    - Rows attributed to ``participant`` (exact assigned-name match) are returned in
      order. Rows attributed to a DIFFERENT participant never leak in — that is the F1
      defect (one provider's ``provider_exec_rejected`` poisoning its sibling's join).
    - Legacy UNATTRIBUTED rows (empty ``participant``, the v1 shape) are returned ONLY
      when ``sole_participant`` is ``True`` — a single-participant action's rows are
      unambiguous. For a multi-participant action they are dropped from every scope and
      ``unattributed_only`` is ``True`` iff they were the only rows present, so the
      caller can surface :data:`REASON_STARTUP_EVIDENCE_UNATTRIBUTED` instead of a
      false "no evidence".
    """
    if events is None:
        return None, False
    name = _norm(participant)
    attributed = tuple(e for e in events if e.participant == name and name)
    if attributed:
        return attributed, False
    legacy = tuple(e for e in events if not e.participant)
    if legacy and sole_participant:
        return legacy, False
    return (), bool(legacy)


def classify_startup_evidence(
    events: Optional[Sequence[ExecutionEvent]],
    *,
    live_locator_observed: bool,
    inventory_readable: bool,
) -> StartupEvidenceVerdict:
    """Pure policy: derive a typed evidence verdict from events + a live-inventory join.

    No I/O. ``events`` is the result of :func:`read_execution_events` (``None`` /
    empty tuple / ordered non-empty tuple, all handled distinctly). The inventory join
    is only ever computed relative to :data:`STAGE_PROVIDER_EXEC_CALL_REACHED` — an
    exec call that was rejected or failed, or that was never reached, has nothing to
    join against a live locator (the wrapper never got far enough to hand off to the
    provider), so ``inventory_join`` is :data:`JOIN_NOT_APPLICABLE` in every one of
    those cases regardless of the ``live_locator_observed`` value passed in.
    """
    if events is None:
        return StartupEvidenceVerdict(
            last_stage=STAGE_NO_EVIDENCE,
            inventory_join=JOIN_NOT_APPLICABLE,
            evidence_gap=True,
            bounded_reason=REASON_STARTUP_EVIDENCE_UNAVAILABLE,
        )
    if not events:
        return StartupEvidenceVerdict(
            last_stage=STAGE_NO_EVIDENCE,
            inventory_join=JOIN_NOT_APPLICABLE,
            evidence_gap=False,
        )
    last = events[-1]
    reached_exec = any(e.stage == STAGE_PROVIDER_EXEC_CALL_REACHED for e in events)
    exec_explicitly_stopped = any(
        e.stage in (STAGE_PROVIDER_EXEC_REJECTED, STAGE_PROVIDER_EXEC_FAILED)
        for e in events
    )
    if not reached_exec or exec_explicitly_stopped:
        return StartupEvidenceVerdict(
            last_stage=last.stage,
            inventory_join=JOIN_NOT_APPLICABLE,
            evidence_gap=False,
            bounded_reason=last.bounded_reason,
        )
    if not inventory_readable:
        join = JOIN_INVENTORY_UNREADABLE
    elif live_locator_observed:
        join = JOIN_PROVIDER_LIVE_CONFIRMED
    else:
        join = JOIN_POST_EXEC_LOCATOR_ABSENT
    return StartupEvidenceVerdict(
        last_stage=last.stage,
        inventory_join=join,
        evidence_gap=False,
        bounded_reason=last.bounded_reason,
    )


__all__ = (
    "EXECUTION_EVENTS_TABLE_SQL",
    "EXECUTION_EVENT_STAGES",
    "JOIN_INVENTORY_UNREADABLE",
    "JOIN_NOT_APPLICABLE",
    "JOIN_POST_EXEC_LOCATOR_ABSENT",
    "JOIN_PROVIDER_LIVE_CONFIRMED",
    "REASON_STARTUP_EVIDENCE_UNATTRIBUTED",
    "REASON_STARTUP_EVIDENCE_UNAVAILABLE",
    "STAGE_ATTESTATION_WRITE_FAILED",
    "STAGE_ATTESTATION_WRITE_SUCCEEDED",
    "STAGE_NO_EVIDENCE",
    "STAGE_PROVIDER_EXEC_CALL_REACHED",
    "STAGE_PROVIDER_EXEC_FAILED",
    "STAGE_PROVIDER_EXEC_REJECTED",
    "STAGE_SELF_LOOKUP_FAILED",
    "STAGE_SELF_LOOKUP_STARTED",
    "STAGE_SELF_LOOKUP_SUCCEEDED",
    "STAGE_SELF_LOOKUP_TIMED_OUT",
    "STAGE_WRAPPER_ENTERED",
    "STARTUP_EXECUTION_EVENTS_FORMAT_VERSION",
    "ExecutionEvent",
    "StartupEvidenceVerdict",
    "append_execution_event",
    "classify_startup_evidence",
    "ensure_execution_events_table",
    "read_execution_events",
    "scope_events_to_participant",
)
