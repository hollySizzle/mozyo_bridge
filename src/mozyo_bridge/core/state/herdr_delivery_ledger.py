"""herdr delivery ledger — durable turn-start outcome persistence (Redmine #13296).

The first installment of the #13263 program (owner-confirmed re-scope of the
frozen #12656 "pending delivery ledger" to a *herdr delivery ledger*, j#72705).
The herdr event rail (#13255) and the queue-enter observation rail (#13292)
already produce closed, redaction-safe turn-start telemetry on the structured
``DeliveryOutcome``; those telemetry docstrings each name a "future #12656 ledger"
as the durable reader that would replay the rail. This module IS that ledger: a
home-scoped SQLite append-only log that persists each herdr send's outcome so an
auditor / a retry driver / a disposition flow can read it after the fact.

Responsibility boundary (#13263 j#72594 — deliberately narrow):

- **ACK semantics are NOT reinvented here.** The #13255 ``turn_start_outcome`` and
  #13292 ``queue_enter_turn_start_observation`` are the source of truth for what
  happened at the receiver; this ledger *receives* those tokens verbatim and adds
  no new judgement vocabulary. ``status`` / ``reason`` are also stored verbatim.
- **What the ledger owns:** record identity (the autoincrement ``id``), causality
  (``notification_marker`` correlates a send with its outcome and any later
  retry / disposition entry), receiver / provider / backend, target identity
  (the pane), the Redmine anchor (issue / journal), the timestamp, retry /
  disposition, and the fallback (rail) classification.
- **Append-only.** State is the fold of entries; there is no UPDATE. A later
  retry or disposition is a NEW entry chained on the same ``notification_marker``
  (``entry_kind`` distinguishes them), mirroring the desired-state
  :mod:`mozyo_bridge.core.state.managed_events` log.

Migration / recovery classification (required by #13296 for any new schema):

- ``schema_version`` = 1; the ``PRAGMA user_version`` guard mirrors the sibling
  home-scoped stores. A newer schema is reported unsupported and left untouched
  (downgrade-safe); an unreadable file degrades to empty reads rather than raising
  into the caller.
- **recovery policy: ``append_only_lossy``** (the vocabulary of
  ``vibes/docs/logics/managed-state-model.md`` ``### recovery policy vocabulary``,
  the same class as ``managed_events``). Losing ``herdr-delivery-ledger.sqlite``
  loses audit / retry / disposition *history* only: identity comes from the
  Redmine anchor, the outcome is re-derivable at send time from the live rail, and
  liveness stays tmux-authoritative. There is therefore no rebuild path by design.
- This is a NEW post-consolidation store, not a legacy-import component of the
  :mod:`mozyo_bridge.core.state.state_store` container (there is no pre-existing
  legacy file to migrate *from*; ``migrated_from`` / ``table_map`` do not apply). A
  future consolidation would add it as a native container table rather than a
  legacy-file component.

Redaction (pasteable-record safety, #13296 / ``feedback_pasteable_records_redact_abs_paths``)
is two-layered. (1) The projection :func:`build_herdr_delivery_ledger_record` is a
**whitelist** — it copies only token / id / number / bool fields off the outcome
and never touches ``execution_root`` or any path-bearing field, so no outcome path
can reach a row. (2) The caller-supplied ``retry`` / ``disposition`` fields have no
such contract, so :meth:`HerdrDeliveryLedger.append` runs them through
:func:`_redact_json_safe` at the persist boundary: a path-shaped string is replaced
with :data:`REDACTED_PATH` and any non-JSON value (e.g. a :class:`pathlib.Path`) is
coerced, so no personal path lands in a row and ``json.dumps`` cannot raise
(Redmine #13296 j#72883 findings 1 / 2). The two rail telemetry dicts are stored
verbatim because their own #13255 / #13292 contracts already forbid free text and
absolute paths.

Conventions mirror the sibling home-scoped stores (a ``*_FILENAME`` constant, a
``*_path(home=None)`` helper resolving through
:func:`mozyo_bridge.shared.paths.mozyo_bridge_home`, a ``PRAGMA user_version``
schema guard, a frozen dataclass with ``as_payload()``, ISO-second UTC
timestamps, and a best-effort command-boundary append that never raises into the
caller). This module is a pure leaf: it imports only stdlib and shared paths, and
reads the outcome by duck typing (``outcome: Any``) exactly like
``delivery_record_sink.build_delivery_record_note`` so the dependency never points
core -> execution-platform.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

HERDR_DELIVERY_LEDGER_FILENAME = "herdr-delivery-ledger.sqlite"
HERDR_DELIVERY_LEDGER_SCHEMA_VERSION = 1

#: Recovery policy (managed-state-model.md ``### recovery policy vocabulary``).
#: Losing the ledger loses audit/retry/disposition history only; there is no
#: rebuild path by design.
HERDR_DELIVERY_LEDGER_RECOVERY_POLICY = "append_only_lossy"

# Entry kinds. An initial send outcome is a ``delivery_outcome`` entry; a later
# retry or disposition is appended as its own entry chained on the same
# ``notification_marker`` (append-only — never an UPDATE).
ENTRY_DELIVERY_OUTCOME = "delivery_outcome"
ENTRY_RETRY = "retry"
ENTRY_DISPOSITION = "disposition"

# Fallback (rail) classification. Which herdr rail produced the outcome, derived
# from which telemetry field the outcome carries. ``other`` covers a non-herdr /
# no-telemetry outcome that is still recorded for completeness.
RAIL_EVENT = "event_rail"  # herdr --mode standard event-driven rail (#13255)
RAIL_QUEUE_ENTER = "queue_enter_rail"  # herdr queue-enter observation rail (#13292)
RAIL_OTHER = "other"

# The terminal backend a herdr outcome came through. Derived (herdr) when either
# herdr telemetry field is present and the caller passed no explicit backend.
BACKEND_HERDR = "herdr"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS herdr_delivery_ledger (
    id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    entry_kind TEXT NOT NULL,
    notification_marker TEXT,
    receiver TEXT,
    provider TEXT,
    backend TEXT,
    rail TEXT,
    target TEXT,
    source TEXT,
    issue_id TEXT,
    journal_id TEXT,
    status TEXT,
    reason TEXT,
    next_action_owner TEXT,
    disposition TEXT,
    turn_start_outcome_json TEXT,
    queue_enter_observation_json TEXT,
    retry_json TEXT
)
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_herdr_ledger_marker "
    "ON herdr_delivery_ledger(notification_marker, id)",
    "CREATE INDEX IF NOT EXISTS idx_herdr_ledger_issue "
    "ON herdr_delivery_ledger(issue_id, id)",
)

_COLUMNS = (
    "recorded_at, entry_kind, notification_marker, receiver, provider, backend, "
    "rail, target, source, issue_id, journal_id, status, reason, "
    "next_action_owner, disposition, turn_start_outcome_json, "
    "queue_enter_observation_json, retry_json"
)

#: Read projection: the autoincrement ``id`` (record identity) then the insert
#: columns. ``id`` is DB-assigned, so it is absent from the INSERT column list
#: above but present on every read.
_SELECT_COLUMNS = f"id, {_COLUMNS}"


def herdr_delivery_ledger_path(home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / HERDR_DELIVERY_LEDGER_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_or_none(value: object) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads_or_none(value: object) -> Optional[dict]:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


#: Marker a path-shaped value is replaced with before it can reach a row.
REDACTED_PATH = "<redacted-path>"


def _looks_like_path(text: str) -> bool:
    """True for a string that carries a filesystem path separator / home / drive.

    The caller-supplied ``retry`` / ``disposition`` telemetry is tokens + numbers
    only by contract, so ANY ``/`` / ``\\`` / leading ``~`` / drive prefix is a
    path leak, not legitimate content. ISO timestamps use ``-`` / ``:`` (no ``/``),
    so they are never caught.
    """
    return "/" in text or "\\" in text or text.startswith("~")


def _redact_key(key: object) -> str:
    """A mapping key coerced to a string and path-redacted.

    JSON object keys must be strings, so a non-string key is coerced first; a
    path-shaped key (including a coerced :class:`pathlib.Path`) is replaced with
    :data:`REDACTED_PATH` so a personal path in a ``retry`` key cannot survive the
    way it did before Redmine #13296 j#72889 (the prior sanitizer redacted values
    but left keys untouched).
    """
    text = key if isinstance(key, str) else str(key)
    return REDACTED_PATH if _looks_like_path(text) else text


def _redact_json_safe(value: object) -> object:
    """Recursively make a value JSON-serializable and free of absolute paths.

    Applied to the caller-supplied ``retry`` / ``disposition`` fields at the
    persist boundary so no row can carry a personal path (Redmine #13296 j#72883
    finding 1) and ``json.dumps`` can never raise on a non-JSON type such as
    :class:`pathlib.Path` (finding 2). Scalars pass through; a path-shaped string
    is replaced with :data:`REDACTED_PATH`; a mapping is walked with BOTH its keys
    (via :func:`_redact_key`) and values redacted — a path-shaped key leaks just as
    much as a value (Redmine #13296 j#72889) — a sequence is walked; any
    other type is coerced to ``str`` (then path-redacted), which is what closes the
    ``TypeError`` hole. The whitelist projection off the ``DeliveryOutcome`` still
    keeps path-bearing outcome fields (``execution_root``) out entirely; this is
    the second layer for the fields the ledger accepts verbatim from the caller.
    """
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return REDACTED_PATH if _looks_like_path(value) else value
    if isinstance(value, dict):
        # Redact BOTH key and value at every nesting level (Redmine #13296
        # j#72889): a path-shaped key is a path leak just as much as a value.
        return {_redact_key(k): _redact_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_json_safe(v) for v in value]
    text = str(value)
    return REDACTED_PATH if _looks_like_path(text) else text


@dataclass(frozen=True)
class HerdrDeliveryLedgerRecord:
    """One durable herdr delivery-ledger entry.

    Every field is a token / id / number / bool / a redaction-safe telemetry dict —
    never an absolute path or secret. ``turn_start_outcome`` (#13255) and
    ``queue_enter_observation`` (#13292) are the verbatim rail telemetry; the ledger
    stores them as-is (ACK semantics are not reinvented).

    ``entry_id`` is the durable record identity — the ``id INTEGER PRIMARY KEY``
    autoincrement assigned at :meth:`HerdrDeliveryLedger.append`. It is ``None`` on
    a record that has not been persisted yet and populated on the append return
    value and every read, so a caller / auditor can stably reference an individual
    ledger entry (Redmine #13296 j#72883 finding 3).
    """

    entry_id: Optional[int] = None
    entry_kind: str = ENTRY_DELIVERY_OUTCOME
    notification_marker: Optional[str] = None
    receiver: Optional[str] = None
    provider: Optional[str] = None
    backend: Optional[str] = None
    rail: Optional[str] = None
    target: Optional[str] = None
    source: Optional[str] = None
    issue_id: Optional[str] = None
    journal_id: Optional[str] = None
    status: Optional[str] = None
    reason: Optional[str] = None
    next_action_owner: Optional[str] = None
    disposition: Optional[str] = None
    turn_start_outcome: Optional[dict] = None
    queue_enter_observation: Optional[dict] = None
    retry: Optional[dict] = None
    recorded_at: Optional[str] = None

    def as_payload(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "recorded_at": self.recorded_at,
            "entry_kind": self.entry_kind,
            "notification_marker": self.notification_marker,
            "receiver": self.receiver,
            "provider": self.provider,
            "backend": self.backend,
            "rail": self.rail,
            "target": self.target,
            "source": self.source,
            "issue_id": self.issue_id,
            "journal_id": self.journal_id,
            "status": self.status,
            "reason": self.reason,
            "next_action_owner": self.next_action_owner,
            "disposition": self.disposition,
            "turn_start_outcome": self.turn_start_outcome,
            "queue_enter_observation": self.queue_enter_observation,
            "retry": self.retry,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_payload(), ensure_ascii=False, sort_keys=True)


class HerdrDeliveryLedgerError(RuntimeError):
    """A user-actionable ledger store error (schema mismatch, corruption)."""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 2000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.execute(_TABLE_SQL)
        for sql in _INDEX_SQL:
            conn.execute(sql)
        conn.execute(f"PRAGMA user_version = {HERDR_DELIVERY_LEDGER_SCHEMA_VERSION}")
        conn.commit()
    elif version != HERDR_DELIVERY_LEDGER_SCHEMA_VERSION:
        conn.close()
        raise HerdrDeliveryLedgerError(
            f"herdr delivery ledger {path} has schema version {version}; this "
            f"mozyo-bridge supports {HERDR_DELIVERY_LEDGER_SCHEMA_VERSION}. The "
            f"file is left untouched (downgrade-safe)."
        )
    return conn


class HerdrDeliveryLedger:
    """Append-only durable herdr delivery ledger. Single-writer by construction."""

    def __init__(self, path: Path | None = None, *, home: Path | None = None):
        self.path = path or herdr_delivery_ledger_path(home)

    def append(self, record: HerdrDeliveryLedgerRecord) -> HerdrDeliveryLedgerRecord:
        """Append one ledger entry, stamping ``recorded_at`` when absent.

        The caller-supplied ``retry`` / ``disposition`` are run through
        :func:`_redact_json_safe` at this persist boundary so no personal path can
        reach a row and a non-JSON ``retry`` value cannot raise (Redmine #13296
        j#72883 findings 1 / 2). The rail telemetry dicts are safe by their #13255 /
        #13292 contracts and are stored verbatim. Returns a copy carrying the
        DB-assigned ``entry_id`` (finding 3) and the redacted fields as persisted.
        """
        recorded_at = record.recorded_at or _utc_now()
        safe_disposition = (
            _redact_json_safe(record.disposition)
            if record.disposition is not None
            else None
        )
        safe_retry = (
            _redact_json_safe(record.retry) if record.retry is not None else None
        )
        conn = _connect(self.path)
        try:
            with conn:
                cursor = conn.execute(
                    f"INSERT INTO herdr_delivery_ledger ({_COLUMNS}) "
                    f"VALUES ({', '.join('?' * 18)})",
                    (
                        recorded_at,
                        record.entry_kind,
                        record.notification_marker,
                        record.receiver,
                        record.provider,
                        record.backend,
                        record.rail,
                        record.target,
                        record.source,
                        record.issue_id,
                        record.journal_id,
                        record.status,
                        record.reason,
                        record.next_action_owner,
                        safe_disposition,
                        _json_or_none(record.turn_start_outcome),
                        _json_or_none(record.queue_enter_observation),
                        _json_or_none(safe_retry),
                    ),
                )
                entry_id = cursor.lastrowid
        finally:
            conn.close()
        return HerdrDeliveryLedgerRecord(
            entry_id=entry_id,
            entry_kind=record.entry_kind,
            notification_marker=record.notification_marker,
            receiver=record.receiver,
            provider=record.provider,
            backend=record.backend,
            rail=record.rail,
            target=record.target,
            source=record.source,
            issue_id=record.issue_id,
            journal_id=record.journal_id,
            status=record.status,
            reason=record.reason,
            next_action_owner=record.next_action_owner,
            disposition=safe_disposition,
            turn_start_outcome=record.turn_start_outcome,
            queue_enter_observation=record.queue_enter_observation,
            retry=safe_retry,
            recorded_at=recorded_at,
        )

    def _read(self, sql: str, params: tuple = ()) -> list[HerdrDeliveryLedgerRecord]:
        if not self.path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != HERDR_DELIVERY_LEDGER_SCHEMA_VERSION:
                    return []
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return []
        return [self._row_to_record(row) for row in rows]

    def recent(self, *, limit: int = 50) -> list[HerdrDeliveryLedgerRecord]:
        return self._read(
            f"SELECT {_SELECT_COLUMNS} FROM herdr_delivery_ledger "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def records_for_marker(self, marker: str) -> list[HerdrDeliveryLedgerRecord]:
        """Causality lookup: every entry (outcome + retry + disposition) for a send."""
        return self._read(
            f"SELECT {_SELECT_COLUMNS} FROM herdr_delivery_ledger "
            "WHERE notification_marker = ? ORDER BY id",
            (marker,),
        )

    def records_for_issue(self, issue_id: str) -> list[HerdrDeliveryLedgerRecord]:
        return self._read(
            f"SELECT {_SELECT_COLUMNS} FROM herdr_delivery_ledger "
            "WHERE issue_id = ? ORDER BY id",
            (issue_id,),
        )

    @staticmethod
    def _row_to_record(row: tuple) -> HerdrDeliveryLedgerRecord:
        return HerdrDeliveryLedgerRecord(
            entry_id=row[0],
            recorded_at=row[1],
            entry_kind=row[2],
            notification_marker=row[3],
            receiver=row[4],
            provider=row[5],
            backend=row[6],
            rail=row[7],
            target=row[8],
            source=row[9],
            issue_id=row[10],
            journal_id=row[11],
            status=row[12],
            reason=row[13],
            next_action_owner=row[14],
            disposition=row[15],
            turn_start_outcome=_loads_or_none(row[16]),
            queue_enter_observation=_loads_or_none(row[17]),
            retry=_loads_or_none(row[18]),
        )


def _rail_for(
    turn_start_outcome: object, queue_enter_observation: object
) -> str:
    """Classify the fallback rail from which telemetry the outcome carries.

    ``turn_start_outcome`` is set ONLY on the herdr event-driven standard rail
    (#13255) and ``queue_enter_turn_start_observation`` ONLY on the herdr
    queue-enter rail (#13292); the two are mutually exclusive by construction, so a
    single field decides the rail. ``other`` covers a non-herdr / no-telemetry
    outcome.
    """
    if isinstance(turn_start_outcome, dict):
        return RAIL_EVENT
    if isinstance(queue_enter_observation, dict):
        return RAIL_QUEUE_ENTER
    return RAIL_OTHER


def build_herdr_delivery_ledger_record(
    outcome: Any,
    *,
    provider: Optional[str] = None,
    backend: Optional[str] = None,
    retry: Optional[dict] = None,
    disposition: Optional[str] = None,
    entry_kind: str = ENTRY_DELIVERY_OUTCOME,
    recorded_at: Optional[str] = None,
) -> HerdrDeliveryLedgerRecord:
    """Project a structured ``DeliveryOutcome`` into a ledger record. Pure; no I/O.

    A **whitelist** projection (redaction by construction): it copies only
    token / id / number / bool fields and the two already-safe telemetry dicts off
    the outcome, and never reads ``execution_root`` or any other path-bearing
    field, so no absolute / private path or secret can reach a persisted row. The
    outcome is read by duck typing (``outcome: Any``), mirroring
    ``delivery_record_sink.build_delivery_record_note``, so this stays a core leaf.

    ``status`` / ``reason`` / ``turn_start_outcome`` /
    ``queue_enter_turn_start_observation`` are stored verbatim — ACK semantics are
    the #13255 / #13292 layers', not the ledger's. ``provider`` and ``backend`` are
    not on the transport outcome, so the caller supplies them (the caller knows the
    provider binding and the terminal backend); ``backend`` defaults to ``herdr``
    when either herdr telemetry field is present and the caller passed none.
    """
    turn_start_outcome = getattr(outcome, "turn_start_outcome", None)
    if not isinstance(turn_start_outcome, dict):
        turn_start_outcome = None
    queue_enter_observation = getattr(
        outcome, "queue_enter_turn_start_observation", None
    )
    if not isinstance(queue_enter_observation, dict):
        queue_enter_observation = None

    rail = _rail_for(turn_start_outcome, queue_enter_observation)
    resolved_backend = backend
    if resolved_backend is None and rail in (RAIL_EVENT, RAIL_QUEUE_ENTER):
        resolved_backend = BACKEND_HERDR

    anchor = getattr(outcome, "anchor", None)
    anchor = anchor if isinstance(anchor, dict) else {}
    issue_id = anchor.get("issue")
    journal_id = anchor.get("journal")

    return HerdrDeliveryLedgerRecord(
        entry_kind=entry_kind,
        notification_marker=getattr(outcome, "notification_marker", None),
        receiver=getattr(outcome, "receiver", None),
        provider=provider,
        backend=resolved_backend,
        rail=rail,
        target=getattr(outcome, "target", None),
        source=getattr(outcome, "source", None) or anchor.get("source"),
        issue_id=issue_id,
        journal_id=journal_id,
        status=getattr(outcome, "status", None),
        reason=getattr(outcome, "reason", None),
        next_action_owner=getattr(outcome, "next_action_owner", None),
        disposition=disposition,
        turn_start_outcome=turn_start_outcome,
        queue_enter_observation=queue_enter_observation,
        retry=retry,
        recorded_at=recorded_at,
    )


def record_herdr_delivery(
    outcome: Any,
    *,
    provider: Optional[str] = None,
    backend: Optional[str] = None,
    retry: Optional[dict] = None,
    disposition: Optional[str] = None,
    entry_kind: str = ENTRY_DELIVERY_OUTCOME,
    home: Path | None = None,
) -> HerdrDeliveryLedgerRecord | None:
    """Best-effort ledger append at a send boundary. Never raises into the caller.

    The append surface a herdr send-site would call after building its
    ``DeliveryOutcome``. Best-effort by design: a ledger failure must not break the
    send that triggered it, exactly like telemetry (mirrors
    :func:`mozyo_bridge.core.state.managed_events.record_managed_event`). Returns
    the appended record, or ``None`` on any failure.
    """
    try:
        record = build_herdr_delivery_ledger_record(
            outcome,
            provider=provider,
            backend=backend,
            retry=retry,
            disposition=disposition,
            entry_kind=entry_kind,
        )
        return HerdrDeliveryLedger(home=home).append(record)
    except (
        HerdrDeliveryLedgerError,
        sqlite3.DatabaseError,
        OSError,
        AttributeError,
        TypeError,
        ValueError,
    ):
        # `TypeError` / `ValueError` are defense-in-depth for a non-JSON value that
        # slipped past `_redact_json_safe`; the sanitizer already coerces such
        # values, so this keeps the "never raises into caller" contract total
        # (Redmine #13296 j#72883 finding 2).
        return None


__all__ = (
    "HERDR_DELIVERY_LEDGER_FILENAME",
    "HERDR_DELIVERY_LEDGER_SCHEMA_VERSION",
    "HERDR_DELIVERY_LEDGER_RECOVERY_POLICY",
    "ENTRY_DELIVERY_OUTCOME",
    "ENTRY_RETRY",
    "ENTRY_DISPOSITION",
    "RAIL_EVENT",
    "RAIL_QUEUE_ENTER",
    "RAIL_OTHER",
    "BACKEND_HERDR",
    "REDACTED_PATH",
    "HerdrDeliveryLedgerRecord",
    "HerdrDeliveryLedgerError",
    "HerdrDeliveryLedger",
    "herdr_delivery_ledger_path",
    "build_herdr_delivery_ledger_record",
    "record_herdr_delivery",
)
