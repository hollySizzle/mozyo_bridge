"""Runtime observation snapshot model (Redmine #12224).

This module codifies, in pure code, the *runtime observation snapshot
contract* defined in
``vibes/docs/logics/runtime-observability-boundary.md`` (section
``## Runtime Observation Snapshot Contract``, closed under #12223). A runtime
observation is a **timestamped snapshot**, independent of where it was stored
(SQLite / JSON / YAML / memory cache / event store). "Newly written" and
"persisted" never mean "currently true": freshness is derived from how old the
underlying observation is, not from the act of reading it.

The reload command (#12224) re-captures these snapshots so an operator can
explicitly refresh a diagnostic / display view. It is deliberately narrow:

- A snapshot is a display / diagnostic projection. It is **not** workflow
  truth, owner approval, review state, routing, close, or task completion —
  those stay with the Redmine durable record and the governed workflow rules.
- A snapshot never implies action safety. Side-effecting commands run their own
  action-time live preflight regardless of any displayed snapshot age
  (#12226 owns that path).
- Fail-closed: a stale / unreadable / contradictory snapshot derives
  ``unknown`` or ``reload_required`` (see :data:`DISPLAY_STATE_UNKNOWN` /
  :data:`DISPLAY_STATE_RELOAD_REQUIRED`). It is **never** derived to
  ``healthy``. This mirrors the Attention Derivation Boundary rule
  ("unreadable or contradictory input becomes unknown").

The envelope field names avoid truth-like generic names
(:data:`FORBIDDEN_GENERIC_SNAPSHOT_FIELDS`); those belong to the proper durable
/ ACK source, not to an observation-quality snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple

# --- envelope vocabulary (contract: ### Snapshot envelope vocabulary) ----------

# ``source``: which responsibility layer the observation came from.
SOURCE_REDMINE = "redmine"
SOURCE_TMUX = "tmux"
SOURCE_OTEL = "otel"
SOURCE_SIDECAR = "sidecar"
SOURCE_MANAGED_EVENT = "managed_event"
SOURCE_CACHE = "cache"
# The live herdr ``agent list`` inventory (Redmine #13356): a runtime observation
# source distinct from tmux capture, so a herdr-backed row's freshness envelope
# names which runtime it was read from (additive vocabulary evolution).
SOURCE_HERDR = "herdr"
SOURCE_OTHER = "other"

# ``method``: how this observation was captured.
METHOD_LIVE_QUERY = "live_query"
METHOD_COMMAND_BOUNDARY_EVENT = "command_boundary_event"
METHOD_RELOAD = "reload"
METHOD_POLL = "poll"
METHOD_PROJECTION_READ = "projection_read"
METHOD_IMPORTED_EVENT = "imported_event"

# ``freshness``: age class of the underlying observation, relative to now.
FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_EXPIRED = "expired"
FRESHNESS_UNKNOWN = "unknown"

# ``readability``: whether the source could be read this time.
READABILITY_READABLE = "readable"
READABILITY_UNREADABLE = "unreadable"
READABILITY_PARTIAL = "partial"

# ``strength``: strength of the *observation method*. Never global truth.
STRENGTH_AUTHORITATIVE_FOR_SOURCE = "authoritative_for_source"
STRENGTH_STRONG_RUNTIME_SIGNAL = "strong_runtime_signal"
STRENGTH_WEAK_OBSERVATION = "weak_observation"
STRENGTH_PROJECTION_ONLY = "projection_only"
STRENGTH_UNKNOWN = "unknown"

# ``stale_reason``: why a non-fresh snapshot is non-fresh (``None`` when fresh).
STALE_REASON_AGE_EXCEEDED = "age_exceeded"
STALE_REASON_SOURCE_UNREADABLE = "source_unreadable"
STALE_REASON_SOURCE_CHANGED = "source_changed"
STALE_REASON_RELOAD_REQUIRED = "reload_required"
STALE_REASON_CONTRADICTED = "contradicted"
STALE_REASON_UNSUPPORTED_SCHEMA = "unsupported_schema"
STALE_REASON_MISSING_SOURCE = "missing_source"

# ``contradiction``: cross-source / internal conflict (``None`` when consistent).
# Detecting contradictions across sources is future scope (#12226 action-time
# preflight); this field stays ``None`` here but the derivation honors it
# defensively so a future caller that sets it fails closed.
CONTRADICTION_SOURCE_CONFLICT = "source_conflict"
CONTRADICTION_LIVE_RUNTIME_CONFLICT = "live_runtime_conflict"
CONTRADICTION_DURABLE_RECORD_CONFLICT = "durable_record_conflict"
CONTRADICTION_INTERNAL_INCONSISTENCY = "internal_inconsistency"

# ``display_state``: derived observation-quality projection for a display
# consumer. It is NOT an attention / workflow state; it only answers "can this
# snapshot be shown as current, or must it be reloaded / treated as unknown?".
# It is deliberately fail-closed and has no soft "stale" value: a stale
# observation derives ``reload_required`` (see :func:`derive_display_state`).
# The visible "this is stale" diagnostic label is carried by the separate
# ``freshness`` field, so a stale snapshot can still be shown — it just never
# reads as current/healthy from ``display_state`` or the exit status.
DISPLAY_STATE_HEALTHY = "healthy"
DISPLAY_STATE_RELOAD_REQUIRED = "reload_required"
DISPLAY_STATE_UNKNOWN = "unknown"

# Generic snapshot envelope field names that must never carry truth-like
# meaning. They belong to the durable / ACK source, scoped to that source
# (e.g. ``redmine_status`` / ``delivery_ack_status``), not to an
# observation-quality snapshot. Pinned so the envelope cannot regress into
# implying workflow completion / approval.
FORBIDDEN_GENERIC_SNAPSHOT_FIELDS = frozenset(
    {"completed", "approved", "current_status", "delivered", "accepted"}
)

# The boundary statement every reload output carries. A snapshot refresh is a
# diagnostic / display act only; it does not move any workflow gate, and it does
# not authorize a side-effecting action.
RELOAD_DIAGNOSTIC_ONLY_NOTE = (
    "Snapshot refresh is diagnostic/display only: it does not update workflow "
    "truth, approval, review, routing, close, or completion (those stay with "
    "the Redmine durable record), and it does not authorize any action — "
    "side-effecting commands run their own action-time live preflight."
)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO8601 timestamp; return ``None`` on any failure.

    Naive timestamps are read as UTC so an age comparison is always well
    defined. Pure / string-only; never raises.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def derive_freshness(
    observed_at: Optional[str],
    *,
    now: datetime,
    max_age_seconds: float,
    expired_after_seconds: float,
    readable: bool = True,
) -> str:
    """Classify the age of an observation captured at ``observed_at``.

    - An unreadable source, or a missing / unparsable timestamp, is
      :data:`FRESHNESS_UNKNOWN` — age cannot be asserted, so do not pretend it
      is fresh.
    - ``age <= max_age_seconds`` -> :data:`FRESHNESS_FRESH`.
    - ``age <= expired_after_seconds`` -> :data:`FRESHNESS_STALE`.
    - otherwise -> :data:`FRESHNESS_EXPIRED`.

    A negative age (observation timestamp in the future, e.g. clock skew) is
    treated as fresh rather than failing.
    """
    if not readable:
        return FRESHNESS_UNKNOWN
    parsed = _parse_iso(observed_at)
    if parsed is None:
        return FRESHNESS_UNKNOWN
    age = (now - parsed).total_seconds()
    if age <= max_age_seconds:
        return FRESHNESS_FRESH
    if age <= expired_after_seconds:
        return FRESHNESS_STALE
    return FRESHNESS_EXPIRED


def derive_display_state(
    *, freshness: str, readability: str, contradiction: Optional[str]
) -> str:
    """Derive the display-quality state, fail-closed.

    The invariant this function guarantees (#12224 acceptance; contract
    ``runtime-observability-boundary.md`` ``### Freshness / fail-safe
    semantics``): a stale / unreadable / contradictory snapshot is **never**
    ``healthy``, and it never derives a soft state that a caller could read as
    "current". ``healthy`` requires a readable source AND a fresh observation
    AND no contradiction. Everything else is fail-closed:

    - contradiction, unknown freshness -> ``unknown`` (cannot determine age).
    - stale / expired observation, partial or unreadable source ->
      ``reload_required`` (readable enough to show, but must be reloaded /
      live-preflighted before it is trusted as current).

    A stale snapshot may still be *displayed* for diagnostics — the visible
    "stale" label lives in the ``freshness`` field; only this derived
    fail-safe state refuses to call it current.
    """
    if contradiction is not None:
        return DISPLAY_STATE_UNKNOWN
    if readability == READABILITY_UNREADABLE:
        return DISPLAY_STATE_RELOAD_REQUIRED
    if freshness == FRESHNESS_UNKNOWN:
        return DISPLAY_STATE_UNKNOWN
    if freshness in (FRESHNESS_STALE, FRESHNESS_EXPIRED):
        return DISPLAY_STATE_RELOAD_REQUIRED
    if readability == READABILITY_PARTIAL:
        return DISPLAY_STATE_RELOAD_REQUIRED
    if freshness == FRESHNESS_FRESH and readability == READABILITY_READABLE:
        return DISPLAY_STATE_HEALTHY
    return DISPLAY_STATE_UNKNOWN


def _derive_stale_reason(
    *,
    readability: str,
    freshness: str,
    contradiction: Optional[str],
    observed_at: Optional[str],
) -> Optional[str]:
    if contradiction is not None:
        return STALE_REASON_CONTRADICTED
    if readability == READABILITY_UNREADABLE:
        return STALE_REASON_SOURCE_UNREADABLE
    if freshness == FRESHNESS_FRESH:
        return None
    if _parse_iso(observed_at) is None:
        return STALE_REASON_MISSING_SOURCE
    if freshness == FRESHNESS_EXPIRED:
        return STALE_REASON_RELOAD_REQUIRED
    if freshness == FRESHNESS_STALE:
        return STALE_REASON_AGE_EXCEEDED
    return STALE_REASON_RELOAD_REQUIRED


@dataclass(frozen=True)
class RuntimeObservationSnapshot:
    """One runtime observation snapshot envelope.

    The fields mirror the contract envelope verbatim, plus a derived
    ``display_state`` (observation-quality projection) and free-text ``notes``.
    Evolve additively only, so display consumers keep working across upgrades.
    """

    observed_at: Optional[str]
    source: str
    method: str
    freshness: str
    readability: str
    strength: str
    stale_reason: Optional[str]
    contradiction: Optional[str]
    display_state: str
    source_refs: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def is_fresh(self) -> bool:
        return self.freshness == FRESHNESS_FRESH

    @property
    def needs_reload(self) -> bool:
        """True when the snapshot is fail-closed (unknown / reload_required).

        This is every snapshot except a fresh, readable, uncontradicted one —
        a stale / expired / partial / unreadable / contradictory snapshot all
        count. The snapshot may still be *shown* (its ``freshness`` field
        carries the visible label); ``needs_reload`` only reports that it must
        not be trusted as current without a reload / live preflight.
        """
        return self.display_state in (
            DISPLAY_STATE_RELOAD_REQUIRED,
            DISPLAY_STATE_UNKNOWN,
        )

    def as_payload(self) -> dict:
        return {
            "observed_at": self.observed_at,
            "source": self.source,
            "method": self.method,
            "freshness": self.freshness,
            "readability": self.readability,
            "strength": self.strength,
            "stale_reason": self.stale_reason,
            "contradiction": self.contradiction,
            "display_state": self.display_state,
            "source_refs": list(self.source_refs),
            "notes": list(self.notes),
        }


def make_snapshot(
    *,
    source: str,
    method: str,
    observed_at: Optional[str],
    readability: str,
    strength: str,
    now: datetime,
    max_age_seconds: float,
    expired_after_seconds: float,
    contradiction: Optional[str] = None,
    source_refs: Tuple[str, ...] = (),
    notes: Tuple[str, ...] = (),
) -> RuntimeObservationSnapshot:
    """Build a snapshot, deriving freshness / stale_reason / display_state.

    The derivation is the contract's fail-safe semantics in one place: the
    derived ``display_state`` is never ``healthy`` unless the source was
    readable, the observation is fresh, and there is no contradiction.
    """
    readable = readability != READABILITY_UNREADABLE
    freshness = derive_freshness(
        observed_at,
        now=now,
        max_age_seconds=max_age_seconds,
        expired_after_seconds=expired_after_seconds,
        readable=readable,
    )
    stale_reason = _derive_stale_reason(
        readability=readability,
        freshness=freshness,
        contradiction=contradiction,
        observed_at=observed_at,
    )
    display_state = derive_display_state(
        freshness=freshness,
        readability=readability,
        contradiction=contradiction,
    )
    return RuntimeObservationSnapshot(
        observed_at=observed_at,
        source=source,
        method=method,
        freshness=freshness,
        readability=readability,
        strength=strength,
        stale_reason=stale_reason,
        contradiction=contradiction,
        display_state=display_state,
        source_refs=tuple(source_refs),
        notes=tuple(notes),
    )


def forbidden_generic_fields(payload: dict) -> list:
    """Return any truth-like generic field names present in ``payload``.

    Used to pin the envelope: a snapshot must not carry a generic
    ``completed`` / ``approved`` / ``current_status`` / ``delivered`` /
    ``accepted`` field, because those imply workflow truth the snapshot does
    not own.
    """
    return sorted(set(payload) & FORBIDDEN_GENERIC_SNAPSHOT_FIELDS)
