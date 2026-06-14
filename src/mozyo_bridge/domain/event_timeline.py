"""Consumer-facing event timeline projection (Redmine #11813).

Display consumers (cockpit / private GUI / iTerm WebViewer) need a stable
event timeline source. They must NOT couple to the OTel store's internal
shape (raw OTLP signal vocabulary, ``attrs_json`` keys) — when the OTel
schema moves, the consumer contract must not. This module is that
decoupling layer: it projects a normalized :class:`~mozyo_bridge.otel_store.OtelEvent`
into a stable :class:`TimelineEvent` envelope.

Design constraints from the source-of-truth design record
(``vibes/docs/logics/event-timeline-source.md``, owner frame in Redmine
#11813 journal #57772):

- **Source layering is explicit.** Each event carries a ``source_layer``
  tag so the consumer can tell the trust level apart: ``runtime`` (OTel
  best-effort cache, never the source of truth), ``delivery`` (handoff
  notification fact — reserved, not yet fed), ``anchor`` (Redmine
  durable-record pointer — pointer only, never copied content). This
  module emits ``runtime`` today; the other layers are reserved枠.
- **No durable-record copying.** Redmine journal content is never
  reproduced here. ``anchor`` is an id pointer only and stays ``None`` for
  runtime events.
- **Redaction is double.** The store already drops prompt bodies and
  denied keys; this projection re-asserts that boundary (defense in
  depth) and additionally refuses to emit full filesystem paths — ``cwd``
  collapses to its leaf basename (``workspace_hint``) and ``workspace.dir``
  is never emitted. A private absolute path leaves neither the JSON nor
  the text face.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Tuple

from mozyo_bridge.otel_store import DENIED_KEY_TOKENS, OtelEvent

# Source-layer tags. Only ``runtime`` is emitted today; the others are
# reserved so the envelope is stable when a delivery feed / anchor feed
# is added (design consultation, see the design doc's "未採用" section).
LAYER_RUNTIME = "runtime"
LAYER_DELIVERY = "delivery"
LAYER_ANCHOR = "anchor"

# Numeric usage attributes surfaced in the envelope's ``usage`` block.
# Identity / event-kind metadata is surfaced elsewhere; this is the
# numbers-only subset a timeline consumer charts.
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "total_tokens",
    "cost_usd",
    "duration_ms",
)


@dataclass(frozen=True)
class TimelineEvent:
    """One event in the consumer-facing timeline envelope.

    Evolve additively only: never change the meaning or type of an existing
    field, so display consumers keep working across mozyo-bridge upgrades.
    """

    id: str
    source_layer: str
    observed_at: Optional[str]
    event_time: Optional[str]
    category: str
    signal: str
    event_name: str
    agent: dict = field(default_factory=dict)
    workspace_hint: Optional[str] = None
    usage: dict = field(default_factory=dict)
    summary: str = ""
    anchor: Optional[dict] = None

    def as_payload(self) -> dict:
        return {
            "id": self.id,
            "source_layer": self.source_layer,
            "observed_at": self.observed_at,
            "event_time": self.event_time,
            "category": self.category,
            "signal": self.signal,
            "event_name": self.event_name,
            "agent": self.agent,
            "workspace_hint": self.workspace_hint,
            "usage": self.usage,
            "summary": self.summary,
            "anchor": self.anchor,
        }


def _basename_hint(cwd: Optional[str]) -> Optional[str]:
    """Leaf component of ``cwd``; never the full path.

    A timeline consumer needs to tell workspaces apart, but a private
    absolute path must not leave the process. ``/Users/alice/work/proj`` ->
    ``proj``. Pure/string-only — no filesystem access, so it is safe on a
    path shape from any host.
    """
    if not cwd:
        return None
    leaf = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return leaf or None


def _is_denied_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in DENIED_KEY_TOKENS)


def _categorize(signal: str, event_name: str) -> str:
    """Coarse, stable category for the consumer.

    Deliberately small and forgiving: an unknown event maps to ``event``
    rather than failing, so a new CLI's telemetry still lands on the
    timeline.
    """
    name = (event_name or "").lower()
    if (signal or "").lower() == "metrics":
        return "usage"
    if "tool" in name:
        return "tool"
    if "api" in name or "request" in name:
        return "api"
    if "session" in name or "start" in name or "stop" in name:
        return "session"
    return "event"


def _usage_subset(attrs: dict) -> dict:
    out: dict = {}
    for key in _USAGE_KEYS:
        value = attrs.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    return out


def _agent_identity(event: OtelEvent) -> dict:
    attrs = event.attrs or {}
    return {
        "service": event.service_name,
        "session": event.session_id,
        "mozyo_session": attrs.get("mozyo.session"),
        "mozyo_agent": attrs.get("mozyo.agent"),
        "mozyo_workspace_id": attrs.get("mozyo.workspace_id"),
    }


def project_event(event: OtelEvent, *, row_id: Optional[int] = None) -> TimelineEvent:
    """Project one runtime OTel event into the stable timeline envelope.

    ``row_id`` is the store's monotonic row id when available (the consumer
    uses it as a best-effort cursor / dedup key). Falls back to a content
    hint when absent so the field is never empty.
    """
    attrs = {
        key: value
        for key, value in (event.attrs or {}).items()
        if not _is_denied_key(str(key))
    }
    identity = _agent_identity(event)
    service = event.service_name or "?"
    return TimelineEvent(
        id=str(row_id) if row_id is not None else f"{event.received_at}:{event.event_name}",
        source_layer=LAYER_RUNTIME,
        observed_at=event.received_at,
        event_time=event.event_time,
        category=_categorize(event.signal, event.event_name),
        signal=event.signal,
        event_name=event.event_name,
        agent={key: value for key, value in identity.items() if value is not None},
        workspace_hint=_basename_hint(event.cwd),
        usage=_usage_subset(attrs),
        summary=f"{service} {event.event_name}".strip(),
        anchor=None,
    )


def project_rows(rows: Iterable[Tuple[int, OtelEvent]]) -> list[TimelineEvent]:
    """Project ``(row_id, event)`` pairs into the timeline envelope.

    This is the path the CLI uses: the store's timeline query returns the
    monotonic row id alongside each event so the consumer gets a stable
    ``id`` for cursor / dedup. Input order is preserved (the store sorts
    newest-first).
    """
    return [project_event(event, row_id=row_id) for row_id, event in rows]


def project_events(events: Iterable[OtelEvent]) -> list[TimelineEvent]:
    """Project bare events without store row ids (content-hint ids).

    For callers that do not have store row ids (e.g. a synthetic feed);
    prefer :func:`project_rows` when row ids are available.
    """
    return [project_event(event) for event in events]
