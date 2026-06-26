"""Activity / idle judgement over the OTel event store (Redmine #11673).

Pre-stage of the three-layer cockpit join (Redmine #11639): Redmine says
whose turn it is, the OTel store says whether the unit is emitting, the
tmux layer says whether it is alive. This module owns only the middle
layer's judgement and deliberately keeps its vocabulary incapable of
claiming death:

- ``active`` — the source emitted telemetry within the window; the agent
  is doing something.
- ``idle`` — the source has history but went quiet. This is "waiting for
  input OR finished OR dead OR receiver was down"; the caller MUST
  consult the tmux liveness layer before drawing a stronger conclusion.
- ``unknown`` — no telemetry for the queried unit at all (env not
  injected, store lost, receiver never up). Same degradation rule.

Identity: OTel sources are keyed by (service_name, session_id) — the CLI
session, not a tmux pane. The phase-2 inventory join maps sources onto
``pane_id`` (the #11628 identity key) via ``match_hints`` (pid, cwd);
this module only carries the hints and never invents a pane identity, so
grouped sessions cannot regress into duplicate rows here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from mozyo_bridge.otel_store import OtelEvent, OtelEventStore

STATE_ACTIVE = "active"
STATE_IDLE = "idle"
STATE_UNKNOWN = "unknown"

# A turn of agent work emits telemetry at least every API round-trip;
# two minutes of silence reliably separates "working" from "waiting"
# without flapping on slow tool calls.
DEFAULT_ACTIVE_WINDOW_SECONDS = 120


@dataclass(frozen=True)
class ActivityRecord:
    """Latest-known activity for one telemetry source."""

    service_name: str | None
    session_id: str | None
    state: str
    last_event_at: str | None
    last_event_name: str | None
    seconds_since_event: float | None
    match_hints: dict

    def as_payload(self) -> dict:
        return {
            "service_name": self.service_name,
            "session_id": self.session_id,
            "state": self.state,
            "last_event_at": self.last_event_at,
            "last_event_name": self.last_event_name,
            "seconds_since_event": self.seconds_since_event,
            "match_hints": self.match_hints,
        }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def classify_event(
    event: OtelEvent,
    *,
    now: datetime | None = None,
    active_window_seconds: int = DEFAULT_ACTIVE_WINDOW_SECONDS,
) -> ActivityRecord:
    """Classify one latest-per-source event into an activity record."""
    moment = now or datetime.now(timezone.utc)
    last = _parse_iso(event.received_at)
    seconds: float | None = None
    state = STATE_UNKNOWN
    if last is not None:
        seconds = max(0.0, (moment - last).total_seconds())
        state = (
            STATE_ACTIVE if seconds <= active_window_seconds else STATE_IDLE
        )
    return ActivityRecord(
        service_name=event.service_name,
        session_id=event.session_id,
        state=state,
        last_event_at=event.received_at,
        last_event_name=event.event_name,
        seconds_since_event=seconds,
        match_hints={
            "pid": event.pid,
            "cwd": event.cwd,
            # Bootstrap-injected join keys (Redmine #11676): the canonical
            # join path, since measured CLIs carry no pid/cwd of their own.
            "session": event.attrs.get("mozyo.session"),
            "agent": event.attrs.get("mozyo.agent"),
            "workspace_id": event.attrs.get("mozyo.workspace_id"),
        },
    )


def summarize_activity(
    store: OtelEventStore,
    *,
    now: datetime | None = None,
    active_window_seconds: int = DEFAULT_ACTIVE_WINDOW_SECONDS,
) -> list[ActivityRecord]:
    """Activity records for every source the store has seen.

    Sources absent from the result are ``unknown`` by definition — query
    :func:`activity_state_for` when asking about a specific unit. An empty
    list means "no telemetry at all": configuration gap or receiver down,
    never "all agents dead".
    """
    return [
        classify_event(
            event, now=now, active_window_seconds=active_window_seconds
        )
        for event in store.latest_per_source()
    ]


@dataclass(frozen=True)
class ActivityTransition:
    """One observed state change for a pane (Redmine #11681)."""

    pane_id: str
    session: str
    agent_kind: str
    previous_state: str
    state: str
    observed_at: str

    def as_payload(self) -> dict:
        return {
            "pane_id": self.pane_id,
            "session": self.session,
            "agent_kind": self.agent_kind,
            "previous_state": self.previous_state,
            "state": self.state,
            "observed_at": self.observed_at,
        }


class TransitionTracker:
    """In-memory activity-transition feed for the cockpit (Redmine #11681).

    Deliberately modest: transitions are recorded in a bounded ring buffer
    served to the UI for display only — no OS-level notification, no focus
    change, no Redmine write (US constraints 3 / 5). State vocabulary stays
    active / idle / unknown; a transition to idle means "may be waiting for
    input", never "dead" — liveness remains the tmux layer's call. Memory
    only by design: a daemon restart starts a fresh feed, consistent with
    the store's best-effort posture. Structured to extend in phase 4
    (Redmine gate context can join onto pane_id at read time).
    """

    def __init__(self, *, max_transitions: int = 100):
        self._last_states: dict[str, tuple[str, str, str]] = {}
        self._transitions: list[ActivityTransition] = []
        self._max = max_transitions

    def observe(self, records: list) -> list[ActivityTransition]:
        """Record state changes from an inventory snapshot's records.

        Returns the new transitions from this observation. Panes that
        disappear from the snapshot are dropped from tracking without a
        transition — absence is a tmux-layer fact, not an activity state.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new: list[ActivityTransition] = []
        seen: dict[str, tuple[str, str, str]] = {}
        for record in records:
            state = (record.activity or {}).get("state") or STATE_UNKNOWN
            seen[record.pane_id] = (state, record.session, record.agent_kind)
            previous = self._last_states.get(record.pane_id)
            if previous is not None and previous[0] != state:
                new.append(
                    ActivityTransition(
                        pane_id=record.pane_id,
                        session=record.session,
                        agent_kind=record.agent_kind,
                        previous_state=previous[0],
                        state=state,
                        observed_at=now,
                    )
                )
        self._last_states = seen
        if new:
            self._transitions.extend(new)
            self._transitions = self._transitions[-self._max:]
        return new

    def recent(self, *, limit: int = 50) -> list[ActivityTransition]:
        return list(self._transitions[-limit:])[::-1]


def activity_state_for(
    records: list[ActivityRecord],
    *,
    pid: str | None = None,
    cwd: str | None = None,
) -> str:
    """Best-effort state lookup by phase-2 join hints.

    Matches pid first (strongest), then cwd. ``unknown`` when nothing
    matches — the caller falls back to the tmux liveness layer.
    """
    if pid:
        for record in records:
            if record.match_hints.get("pid") == pid:
                return record.state
    if cwd:
        for record in records:
            if record.match_hints.get("cwd") == cwd:
                return record.state
    return STATE_UNKNOWN
