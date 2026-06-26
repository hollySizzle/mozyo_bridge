"""Cockpit attention-state derivation read model (Redmine #11951 / #11935).

Pure projection layer: derive an :class:`AttentionRecord` from already-extracted
durable / observed input facts. The design source of truth is
``vibes/docs/logics/cockpit-attention-state.md``; this module implements only
the first thin read model from that design (the `Implementation Split` step 1).

Boundaries (pinned by tests in ``tests/test_attention_state.py``):

- **Read model, no I/O.** This module performs no Redmine / tmux / event-store
  reads. The caller extracts the input facts (from Redmine journals, managed
  events, live tmux observation, the target cache, ...) and passes them in
  :class:`AttentionInputs`. ``observed_at`` is caller-supplied so derivation is
  pure and clock-free.
- **Projection, never a routing key.** The derived attention state is a display
  / triage projection. This module does not import or touch handoff routing, the
  target resolver, or pane-send preflight, and the attention value is never used
  to pick a target. ``target_key`` is opaque identity provenance, not a routing
  decision.
- **No tmux color / user option / ``agent-ui.conf``.** Those projections are
  separate later tasks; this module stops at the derived record.
- **Fail-safe to ``unknown``.** Unreadable or contradictory sources derive
  ``unknown``, never ``healthy`` — a broken projection must not look healthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- attention states -------------------------------------------------------
STATE_HEALTHY = "healthy"
STATE_OWNER_WAITING = "owner_waiting"
STATE_REVIEW_WAITING = "review_waiting"
STATE_BLOCKED = "blocked"
STATE_STALLED = "stalled"
STATE_DONE = "done"
STATE_RETIRED_CANDIDATE = "retired_candidate"
STATE_UNKNOWN = "unknown"

ATTENTION_STATES = frozenset(
    {
        STATE_HEALTHY,
        STATE_OWNER_WAITING,
        STATE_REVIEW_WAITING,
        STATE_BLOCKED,
        STATE_STALLED,
        STATE_DONE,
        STATE_RETIRED_CANDIDATE,
        STATE_UNKNOWN,
    }
)

# --- severity (generic default projection; refinable by later UI tasks) -----
SEVERITY_NORMAL = "normal"
SEVERITY_NOTICE = "notice"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

# --- role (codex | claude | other, per the design doc) ----------------------
ROLE_CODEX = "codex"
ROLE_CLAUDE = "claude"
ROLE_OTHER = "other"

# Generic reason codes. The caller may override with a more specific code (for
# example ``review_request_pending`` plus ``target_dead``); these are the
# OSS-default fallbacks and carry no private project / operator policy.
REASON_SOURCE_UNREADABLE = "source_unreadable"
REASON_CONTRADICTORY = "contradictory_sources"

_DEFAULT_REASON = {
    STATE_OWNER_WAITING: "owner_approval_pending",
    STATE_BLOCKED: "blocked_recorded",
    STATE_REVIEW_WAITING: "review_request_pending",
    STATE_STALLED: "no_progress_after_handoff",
    STATE_RETIRED_CANDIDATE: "retire_conditions_met",
    STATE_DONE: "close_gate_satisfied",
    STATE_HEALTHY: "healthy",
    STATE_UNKNOWN: "unknown",
}

_DEFAULT_SEVERITY = {
    STATE_OWNER_WAITING: SEVERITY_WARNING,
    STATE_BLOCKED: SEVERITY_CRITICAL,
    STATE_REVIEW_WAITING: SEVERITY_NOTICE,
    STATE_STALLED: SEVERITY_WARNING,
    STATE_RETIRED_CANDIDATE: SEVERITY_NOTICE,
    STATE_DONE: SEVERITY_NORMAL,
    STATE_HEALTHY: SEVERITY_NORMAL,
    STATE_UNKNOWN: SEVERITY_WARNING,
}


@dataclass(frozen=True)
class AttentionInputs:
    """Already-extracted facts a caller derives attention from.

    Identity / provenance fields are opaque strings — this module never parses
    them (``unit_id`` is ``unit:<host>:<workspace_id>:<lane_id>`` and
    ``target_key`` is ``tmux:<host>:<pane_id>`` by convention, but that shape is
    the caller's contract, not this module's). The boolean signals are the
    durable / observed facts the design doc enumerates per state; the caller is
    responsible for extracting them safely and for setting ``source_readable`` /
    ``contradictory`` honestly so the fail-safe to ``unknown`` works.
    """

    unit_id: str
    observed_at: str
    host_id: str = "local"
    workspace_id: str = ""
    lane_id: str = "default"
    role: str = ROLE_OTHER
    target_key: str | None = None
    source_refs: tuple[str, ...] = ()
    expires_at: str | None = None

    # Fail-safe inputs: either of these forces ``unknown``.
    source_readable: bool = True
    contradictory: bool = False

    # Attention signals (extracted from durable / observed inputs).
    owner_waiting: bool = False
    blocked: bool = False
    review_waiting: bool = False
    stalled: bool = False
    done: bool = False  # close gate satisfied
    retired_candidate: bool = False  # operational cleanup conditions met

    # Optional caller-supplied reason override (e.g. ``target_dead``).
    reason_code: str | None = None


@dataclass(frozen=True)
class AttentionRecord:
    """Derived attention projection for one unit / target.

    A read model. ``attention_state`` is a triage / display value; it is not a
    routing key and must not be used to select a handoff target.
    """

    unit_id: str
    host_id: str
    workspace_id: str
    lane_id: str
    role: str
    target_key: str | None
    attention_state: str
    severity: str
    reason_code: str
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    observed_at: str = ""
    expires_at: str | None = None

    def as_payload(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "host_id": self.host_id,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "role": self.role,
            "target_key": self.target_key,
            "attention_state": self.attention_state,
            "severity": self.severity,
            "reason_code": self.reason_code,
            "source_refs": list(self.source_refs),
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
        }


def _derive_state(inputs: AttentionInputs) -> tuple[str, str | None]:
    """Return ``(attention_state, forced_reason)`` from input facts.

    Priority follows ``cockpit-attention-state.md`` `Derivation Priority`: a
    fail-safe to ``unknown`` first, then the explicit attention signals in
    descending strength (owner_waiting > blocked > review_waiting > stalled),
    then the operational done states (retired_candidate is the stronger,
    more-operational form of done), then ``healthy``. Ties between an active
    attention signal and a done signal resolve to the active signal so an owner
    or review gate is never hidden behind a "done" projection.
    """
    # Fail-safe: a projection over unreadable or contradictory sources must not
    # look healthy.
    if not inputs.source_readable:
        return STATE_UNKNOWN, REASON_SOURCE_UNREADABLE
    if inputs.contradictory:
        return STATE_UNKNOWN, REASON_CONTRADICTORY

    if inputs.owner_waiting:
        return STATE_OWNER_WAITING, None
    if inputs.blocked:
        return STATE_BLOCKED, None
    if inputs.review_waiting:
        return STATE_REVIEW_WAITING, None
    if inputs.stalled:
        return STATE_STALLED, None
    if inputs.retired_candidate:
        return STATE_RETIRED_CANDIDATE, None
    if inputs.done:
        return STATE_DONE, None
    return STATE_HEALTHY, None


def derive_attention(inputs: AttentionInputs) -> AttentionRecord:
    """Derive the :class:`AttentionRecord` projection for ``inputs``.

    Pure and deterministic: identical inputs always yield an identical record,
    and no Redmine / tmux / event-store / clock access happens here.
    """
    state, forced_reason = _derive_state(inputs)
    reason = inputs.reason_code or forced_reason or _DEFAULT_REASON[state]
    return AttentionRecord(
        unit_id=inputs.unit_id,
        host_id=inputs.host_id,
        workspace_id=inputs.workspace_id,
        lane_id=inputs.lane_id,
        role=inputs.role,
        target_key=inputs.target_key,
        attention_state=state,
        severity=_DEFAULT_SEVERITY[state],
        reason_code=reason,
        source_refs=tuple(inputs.source_refs),
        observed_at=inputs.observed_at,
        expires_at=inputs.expires_at,
    )


# No durable attention source (Redmine gate / owner / review / managed event)
# is wired into the read-only projections yet, so a cleanly-identified target
# derives ``healthy`` with this reason rather than a fabricated owner/review
# signal. See ``vibes/docs/logics/cockpit-attention-state.md`` `Implementation
# Split`.
NO_ATTENTION_SOURCE_REASON = "no_attention_source"


def conservative_attention(
    *,
    observed_at: str,
    role: str,
    identity_readable: bool,
    contradictory: bool,
    host: str = "local",
    workspace_id: str = "",
    lane_id: str = "default",
    pane_id: str | None = None,
) -> AttentionRecord:
    """Conservative read-only :class:`AttentionRecord` for one discovered target.

    The single shared convention behind both read-only attention projections —
    ``agents targets --json`` (#11952) and the cockpit ``/api/units`` join
    (#12007) — so the two never drift. No durable attention source is connected
    yet, so this never fabricates an owner / review / blocked / stalled signal:
    a cleanly identified target (``identity_readable`` and not ``contradictory``)
    derives ``healthy`` with reason :data:`NO_ATTENTION_SOURCE_REASON`, and an
    ambiguous / unreadable identity derives ``unknown``.

    The caller extracts ``identity_readable`` / ``contradictory`` from its own
    field vocabulary (a target candidate's ``confidence`` / ``role_source`` /
    ``ambiguous`` vs an inventory pane's), and the ``unit_id`` / ``target_key``
    provenance conventions from ``unit-target-model.md`` are stamped here once.
    ``source_refs`` carry only the tmux ``pane_id`` (no path / secret), keeping
    the projection public-safe. This stays a triage / display projection and is
    never used to pick a routing target.
    """
    role_norm = role if role in (ROLE_CLAUDE, ROLE_CODEX) else ROLE_OTHER
    inputs = AttentionInputs(
        unit_id=f"unit:{host}:{workspace_id}:{lane_id}",
        observed_at=observed_at,
        host_id=host,
        workspace_id=workspace_id,
        lane_id=lane_id,
        role=role_norm,
        target_key=f"tmux:{host}:{pane_id}" if pane_id else None,
        source_refs=(f"tmux:{pane_id}",) if pane_id else (),
        source_readable=identity_readable,
        contradictory=contradictory,
        reason_code=(
            NO_ATTENTION_SOURCE_REASON
            if identity_readable and not contradictory
            else None
        ),
    )
    return derive_attention(inputs)
