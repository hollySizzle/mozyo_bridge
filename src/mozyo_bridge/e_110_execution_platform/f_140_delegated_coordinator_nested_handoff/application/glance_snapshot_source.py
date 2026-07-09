"""Source adapters that build glance snapshots for `workflow glance` (Redmine #13435).

The pure fold (:mod:`...domain.workflow_glance`) turns one
:class:`...domain.workflow_glance.IssueGlanceSnapshot` into a projected row. This
module is the design j#74172 split step 2 — the adapters that *produce* those
snapshots from real sources, kept out of the pure domain so the fold stays free of
Redmine / herdr / store I/O:

- :class:`MappingGlanceSnapshotSource` reads an **already-composed structured
  snapshot** (a coordinator / MCP sweep, or a test fixture) — the same "read a
  supplied structured payload" boundary :class:`...MappingRedmineJournalSource` uses.
  Structured facts only; no prose is parsed.
- :func:`store_active_lane_snapshots` enumerates the active lanes from the persisted
  workflow-runtime store (the events `workflow watch` recorded) and, optionally, joins
  the herdr delivery ledger for the transport dimension. It is fail-open: an absent /
  unreadable store or ledger degrades to fewer facts, never an exception.

The delivery ledger join is deliberately conservative: :func:`anomaly_from_ledger_record`
maps only the **recognized** turn-start / disposition telemetry to a delivery anomaly
and otherwise reports a healthy delivery, so an uninterpretable ledger row never
fabricates a false alarm.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    CALLBACK_NONE,
    CALLBACK_STATES,
    GATE_KINDS,
    GATE_NONE,
    LaneSignal,
    REVIEW_CONCLUSIONS,
    REVIEW_PENDING,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (
    ANOMALY_CALLBACK_DELIVERY_FAILED,
    ANOMALY_NONE,
    ANOMALY_STAGED_NOT_SUBMITTED,
    ANOMALY_TURN_START_UNCONFIRMED,
    DELIVERY_ANOMALIES,
    DELIVERY_SOURCE_HERDR_LEDGER,
    DELIVERY_SOURCE_NONE,
    DeliveryObservation,
    IssueGlanceSnapshot,
    RECEIVE_UNKNOWN,
    RUNTIME_UNKNOWN,
)


def _as_bool(value: object, default: bool = False) -> bool:
    """Coerce a JSON scalar to bool, tolerating ``true``/``1``/``yes`` strings."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _lane_signal_from_mapping(issue_id: str, facts: Mapping[str, object]) -> LaneSignal:
    """Build a :class:`LaneSignal` from one structured snapshot entry (fail-closed).

    Only recognized gate / review / callback vocabulary is accepted; an out-of-vocabulary
    ``latest_gate`` is folded to ``GATE_NONE`` (and an unknown gate would otherwise be
    classified as blocked, which we do not want to fabricate from a typo). The remaining
    flags default to a conservative resting state.
    """
    gate = str(facts.get("latest_gate", GATE_NONE) or GATE_NONE).strip()
    if gate not in GATE_KINDS:
        gate = GATE_NONE
    review = str(facts.get("review_conclusion", REVIEW_PENDING) or REVIEW_PENDING).strip()
    if review not in REVIEW_CONCLUSIONS:
        review = REVIEW_PENDING
    callback = str(facts.get("callback_state", CALLBACK_NONE) or CALLBACK_NONE).strip()
    if callback not in CALLBACK_STATES:
        callback = CALLBACK_NONE
    return LaneSignal(
        issue=issue_id,
        latest_gate=gate,
        review_conclusion=review,
        callback_state=callback,
        commit_bearing=_as_bool(facts.get("commit_bearing")),
        integration_recorded=_as_bool(facts.get("integration_recorded")),
        issue_open=_as_bool(facts.get("issue_open"), default=True),
        blocker_recorded=_as_bool(facts.get("blocker_recorded")),
    )


def _delivery_from_mapping(raw: object) -> DeliveryObservation:
    """Build a :class:`DeliveryObservation` from a snapshot's ``delivery`` sub-mapping.

    A missing / non-mapping ``delivery`` yields a healthy, unobserved delivery. The fold
    re-validates every token, so this adapter passes values through verbatim (trimmed).
    """
    if not isinstance(raw, Mapping):
        return DeliveryObservation()
    return DeliveryObservation(
        anomaly=str(raw.get("anomaly", ANOMALY_NONE) or ANOMALY_NONE).strip(),
        source=str(raw.get("source", DELIVERY_SOURCE_NONE) or DELIVERY_SOURCE_NONE).strip(),
        observed_journal=str(raw.get("observed_journal", "") or "").strip(),
        runtime_state=str(raw.get("runtime_state", RUNTIME_UNKNOWN) or RUNTIME_UNKNOWN).strip(),
        receive_method=str(raw.get("receive_method", RECEIVE_UNKNOWN) or RECEIVE_UNKNOWN).strip(),
    )


@dataclass(frozen=True)
class MappingGlanceSnapshotSource:
    """A glance-snapshot source over an already-composed structured payload.

    ``payload`` is either ``{"issues": [ {...}, ... ]}`` or a bare ``[ {...}, ... ]`` list
    (both accepted). Each entry is a structured lane fact-set:

    - ``issue`` (required), ``subject``, ``lane``;
    - ``latest_gate`` + ``latest_gate_journal`` + ``review_conclusion`` /
      ``callback_state`` / ``commit_bearing`` / ``integration_recorded`` / ``issue_open``
      / ``blocker_recorded`` (the :class:`LaneSignal` durable facts);
    - an optional ``delivery`` sub-mapping (:class:`DeliveryObservation`).

    Pure — it reads a supplied snapshot and performs no I/O. An entry without an
    ``issue`` id is skipped (a snapshot row with no durable anchor cannot be projected).
    """

    payload: object

    def _entries(self) -> Sequence[Mapping[str, object]]:
        raw = self.payload
        if isinstance(raw, Mapping):
            raw = raw.get("issues", [])
        if isinstance(raw, str) or not isinstance(raw, Sequence):
            return []
        return [e for e in raw if isinstance(e, Mapping)]

    def snapshots(self) -> tuple[IssueGlanceSnapshot, ...]:
        snaps: list[IssueGlanceSnapshot] = []
        for entry in self._entries():
            issue_id = str(entry.get("issue", "") or "").strip()
            if not issue_id:
                continue
            snaps.append(
                IssueGlanceSnapshot(
                    issue_id=issue_id,
                    signal=_lane_signal_from_mapping(issue_id, entry),
                    subject=str(entry.get("subject", "") or "").strip(),
                    lane=str(entry.get("lane", "") or "").strip(),
                    latest_gate_journal=str(entry.get("latest_gate_journal", "") or "").strip(),
                    delivery=_delivery_from_mapping(entry.get("delivery")),
                )
            )
        return tuple(snaps)


# ---------------------------------------------------------------------------
# herdr delivery ledger -> delivery anomaly (conservative, recognized signals only).
# ---------------------------------------------------------------------------

# The turn-start outcome tokens (#13255) that map to a delivery anomaly. ``started`` is
# healthy (a turn ran); the rest are the transport failure modes the motivating session
# hit. Kept as a literal table so an unrecognized outcome reports no anomaly rather than
# guessing.
_TURN_START_OUTCOME_ANOMALY: dict[str, str] = {
    "delivered_not_started": ANOMALY_TURN_START_UNCONFIRMED,
    "inject_failed": ANOMALY_STAGED_NOT_SUBMITTED,
    "precondition_not_idle": ANOMALY_STAGED_NOT_SUBMITTED,
    "absent": ANOMALY_CALLBACK_DELIVERY_FAILED,
}


def anomaly_from_ledger_record(record) -> DeliveryObservation:
    """Derive a :class:`DeliveryObservation` from one herdr delivery-ledger record (pure).

    Recognized signals, in precedence:

    1. a ``disposition`` / ``status`` that is *itself* a known delivery-anomaly token is
       used verbatim (forward-compatible: a future ledger writer can record the anomaly
       directly);
    2. otherwise the #13255 ``turn_start_outcome.outcome`` is mapped through
       :data:`_TURN_START_OUTCOME_ANOMALY` (``delivered_not_started`` ->
       ``turn_start_unconfirmed``, ``inject_failed`` -> ``staged_not_submitted``, ...);
    3. otherwise the delivery is healthy (:data:`ANOMALY_NONE`).

    The ``source`` is always :data:`DELIVERY_SOURCE_HERDR_LEDGER`; the ``observed_journal``
    is the record's ``journal_id`` so the fold can decide whether a later durable gate has
    already superseded it. Conservative by design — an uninterpretable row is healthy, not
    ``unknown``, so the ledger join never raises a false alarm.
    """
    journal = str(getattr(record, "journal_id", "") or "").strip()

    for token in (getattr(record, "disposition", None), getattr(record, "status", None)):
        candidate = str(token or "").strip()
        if candidate in DELIVERY_ANOMALIES and candidate != ANOMALY_NONE:
            return DeliveryObservation(
                anomaly=candidate,
                source=DELIVERY_SOURCE_HERDR_LEDGER,
                observed_journal=journal,
            )

    turn_start = getattr(record, "turn_start_outcome", None)
    if isinstance(turn_start, Mapping):
        outcome = str(turn_start.get("outcome", "") or "").strip()
        anomaly = _TURN_START_OUTCOME_ANOMALY.get(outcome)
        if anomaly is not None:
            return DeliveryObservation(
                anomaly=anomaly,
                source=DELIVERY_SOURCE_HERDR_LEDGER,
                observed_journal=journal,
            )

    return DeliveryObservation(source=DELIVERY_SOURCE_HERDR_LEDGER, observed_journal=journal)


def _journal_from_event_id(event_id: str) -> str:
    """Extract the journal id from a ``redmine:<issue>:<journal>`` durable event id."""
    parts = str(event_id or "").split(":")
    if len(parts) >= 3 and parts[0] == "redmine":
        return parts[2].strip()
    return ""


def store_active_lane_snapshots(store, *, ledger=None) -> tuple[IssueGlanceSnapshot, ...]:
    """Enumerate active-lane snapshots from the persisted workflow-runtime store (fail-open).

    Folds the store's recorded events (the facts `workflow watch` ingested) into one
    :class:`IssueGlanceSnapshot` per issue: the **latest** event for an issue supplies the
    :class:`LaneSignal` durable facts, and its ``redmine:<issue>:<journal>`` event id
    supplies the ``latest_gate_journal``. Route identities supply the lane label. When a
    ``ledger`` is given, the issue's most recent ledger record is joined for the delivery
    dimension via :func:`anomaly_from_ledger_record`.

    Fail-open at every read: a store / ledger that raises is treated as "no facts" for the
    affected slice, so the glance degrades to fewer columns rather than failing. Issues are
    returned in first-seen (apply) order for a stable projection.
    """
    try:
        events = store.read_events()
    except Exception:  # noqa: BLE001 - a store read never breaks the read-only glance
        return ()

    order: list[str] = []
    latest_event: dict[str, object] = {}
    latest_journal: dict[str, str] = {}
    for row in events:
        issue = str(getattr(row, "issue", "") or "").strip()
        if not issue:
            continue
        if issue not in latest_event:
            order.append(issue)
        latest_event[issue] = row
        latest_journal[issue] = _journal_from_event_id(getattr(row, "event_id", ""))

    lanes = _issue_lane_labels(store)
    deliveries = _issue_deliveries(order, ledger)

    snaps: list[IssueGlanceSnapshot] = []
    for issue in order:
        row = latest_event[issue]
        signal = LaneSignal(
            issue=issue,
            latest_gate=str(getattr(row, "gate", GATE_NONE) or GATE_NONE),
            review_conclusion=str(getattr(row, "review_conclusion", REVIEW_PENDING) or REVIEW_PENDING),
            callback_state=str(getattr(row, "callback_state", CALLBACK_NONE) or CALLBACK_NONE),
            commit_bearing=bool(getattr(row, "commit_bearing", False)),
            integration_recorded=bool(getattr(row, "integration_recorded", False)),
            issue_open=bool(getattr(row, "issue_open", True)),
            blocker_recorded=bool(getattr(row, "blocker_recorded", False)),
        )
        snaps.append(
            IssueGlanceSnapshot(
                issue_id=issue,
                signal=signal,
                lane=lanes.get(issue, ""),
                latest_gate_journal=latest_journal.get(issue, ""),
                delivery=deliveries.get(issue, DeliveryObservation()),
            )
        )
    return tuple(snaps)


def _issue_lane_labels(store) -> dict[str, str]:
    """Map issue -> lane label from persisted route identities (fail-open, last wins)."""
    labels: dict[str, str] = {}
    try:
        routes = store.read_route_identities()
    except Exception:  # noqa: BLE001 - route read is best-effort supplementary data
        return labels
    for row in routes:
        issue = str(getattr(row, "issue", "") or "").strip()
        lane = str(getattr(row, "lane_id", "") or "").strip()
        if issue and lane:
            labels[issue] = lane
    return labels


def _issue_deliveries(issues, ledger) -> dict[str, DeliveryObservation]:
    """Map issue -> delivery observation from the ledger's most recent record (fail-open)."""
    deliveries: dict[str, DeliveryObservation] = {}
    if ledger is None:
        return deliveries
    for issue in issues:
        try:
            records = ledger.records_for_issue(issue)
        except Exception:  # noqa: BLE001 - a ledger read never breaks the glance
            continue
        if records:
            deliveries[issue] = anomaly_from_ledger_record(records[-1])
    return deliveries


__all__ = (
    "MappingGlanceSnapshotSource",
    "anomaly_from_ledger_record",
    "store_active_lane_snapshots",
)
