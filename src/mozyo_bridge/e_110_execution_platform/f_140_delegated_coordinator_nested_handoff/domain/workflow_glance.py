"""Coordinator pipeline glance projection (Redmine #13435).

A single read-only projection that answers "what is every active lane/US doing, what
is the next action, and is anything **stuck in delivery**?" — so a coordinator no
longer has to hand-correlate ``mozyo-bridge status`` + each Redmine journal + a
``herdr agent read`` pane to notice that "the whole pipeline looks stopped" is really
"the work is done but a turn-start submit failed / a callback self-looped" (the
session that motivated the issue: #13392 / #13408 / #13425 delivery stalls).

This module is the **pure domain fold** (design j#74172 split step 1): given one
issue's durable-record facts plus an optional delivery observation, it produces one
:class:`WorkflowGlanceRow`. It depends only on the durable-record classification
(:func:`...domain.sublane_admission.classify_lane_state`) — never on Redmine, herdr,
or tmux. The source adapters (store / Redmine / herdr ledger) and the CLI renderer
live in the application layer.

Design contract (Redmine #13435 j#74172):

- **workflow_state is folded from the durable record only.** It reuses the existing
  :data:`...LANE_STATE_*` vocabulary (implementing / review_waiting / owner_waiting /
  integration_waiting / close_waiting / blocked / callback_due /
  callback_delivery_failed / retire_ready / idle) so the glance and the admission
  preflight classify a lane the same way — the glance does not invent a second state
  machine.
- **delivery anomaly is a separate dimension joined on top.** It is an observation
  about the *transport* (a turn-start submit that never confirmed, a composer that
  staged a marker but did not submit, a coordinator callback that self-looped), never
  a workflow truth. Because :func:`fold_glance_row` derives ``workflow_state`` purely
  from the :class:`...LaneSignal`, a delivery anomaly **cannot roll the workflow state
  back** — the design's load-bearing invariant (a completed-but-stuck lane still reads
  as review_waiting, flagged with the delivery anomaly, not demoted to implementing).
- **a later durable gate supersedes an earlier delivery observation.** When the
  delivery observation was recorded at a journal *before* the journal that set the
  latest gate, the anomaly is marked ``stale`` (the durable record already moved past
  it) so a resolved #13392-style poll does not raise a false alarm.
- **pane scrollback is never the source of workflow truth.** A ``runtime_state`` that
  came from a live pane read carries ``delivery_source=runtime_observation`` so the
  reader can see it is a supplementary signal, not a gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_authority_projection import (
    AuthorityFacts,
    ExecutionSurfaceFacts,
    ReconcileFacts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    GATE_NONE,
    LaneSignal,
    classify_lane_state,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    LANE_STATE_BLOCKED,
    LANE_STATE_CALLBACK_DELIVERY_FAILED,
    LANE_STATE_CALLBACK_DUE,
    LANE_STATE_CLOSE_WAITING,
    LANE_STATE_IDLE,
    LANE_STATE_IMPLEMENTING,
    LANE_STATE_INTEGRATION_WAITING,
    LANE_STATE_OWNER_WAITING,
    LANE_STATE_RETIRE_READY,
    LANE_STATE_REVIEW_WAITING,
)

# ``unknown`` is the glance-only degraded workflow state: the lane was enumerated from the
# active roster but its durable gate could not be resolved — the Redmine source was
# unavailable, or the issue's journals carried no recognized canonical ``## Gate:`` template
# (Redmine #13435 j#74307 point 6). It is deliberately distinct from ``idle`` (a lane with a
# readable, gate-free durable record): ``unknown`` means "we could not read", not "there is
# no work". A row is always emitted for it (never silently dropped), owned by the coordinator
# to investigate. It is never produced from a successfully-folded durable record.
WORKFLOW_STATE_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Delivery anomaly vocabulary (machine-readable; literal regardless of UI language).
#
# The transport-layer failure modes the motivating session hit. `none` is a healthy
# lane; `unknown` is a fail-closed catch-all for an unreadable / out-of-vocabulary
# observation (never guessed). Mirrors design j#74172's delivery_anomaly enum.
# ---------------------------------------------------------------------------

ANOMALY_NONE = "none"
ANOMALY_TURN_START_UNCONFIRMED = "turn_start_unconfirmed"
ANOMALY_STAGED_NOT_SUBMITTED = "staged_not_submitted"
ANOMALY_MARKER_UNOBSERVED = "marker_unobserved"
ANOMALY_CALLBACK_SELF_LOOP = "callback_self_loop"
ANOMALY_CALLBACK_DELIVERY_FAILED = "callback_delivery_failed"
ANOMALY_CALLBACK_NOT_ATTEMPTED = "callback_not_attempted"
ANOMALY_UNKNOWN = "unknown"

DELIVERY_ANOMALIES = frozenset(
    {
        ANOMALY_NONE,
        ANOMALY_TURN_START_UNCONFIRMED,
        ANOMALY_STAGED_NOT_SUBMITTED,
        ANOMALY_MARKER_UNOBSERVED,
        ANOMALY_CALLBACK_SELF_LOOP,
        ANOMALY_CALLBACK_DELIVERY_FAILED,
        ANOMALY_CALLBACK_NOT_ATTEMPTED,
        ANOMALY_UNKNOWN,
    }
)

# ``delivery_source`` — where the delivery signal came from. The durable record
# (Redmine journal) is authoritative; the herdr ledger is durable delivery telemetry;
# a runtime observation (a pane read) is a supplementary signal only.
DELIVERY_SOURCE_NONE = "none"
DELIVERY_SOURCE_REDMINE_JOURNAL = "redmine_journal"
DELIVERY_SOURCE_HERDR_LEDGER = "herdr_ledger"
DELIVERY_SOURCE_RUNTIME_OBSERVATION = "runtime_observation"

DELIVERY_SOURCES = frozenset(
    {
        DELIVERY_SOURCE_NONE,
        DELIVERY_SOURCE_REDMINE_JOURNAL,
        DELIVERY_SOURCE_HERDR_LEDGER,
        DELIVERY_SOURCE_RUNTIME_OBSERVATION,
    }
)

# ``receive_method`` — how the receiver is expected to pick the handoff up. A durable
# journal poll is the resilient path (the receiver reads the anchor from Redmine); a
# callback is the pushed path (and the one that self-loops when the route resolves to
# the sender's own lane).
RECEIVE_DURABLE_JOURNAL_POLL = "durable_journal_poll"
RECEIVE_CALLBACK = "callback"
RECEIVE_UNKNOWN = "unknown"

RECEIVE_METHODS = frozenset(
    {RECEIVE_DURABLE_JOURNAL_POLL, RECEIVE_CALLBACK, RECEIVE_UNKNOWN}
)

# ``runtime_state`` — the herdr receiver state. Mirrored locally (like
# ``redmine_journal_source.GATE_BEARING_KINDS`` mirrors the adapter's gate kinds) so
# this bounded context does not import the e_140 terminal-runtime adapter. An
# out-of-vocabulary value folds to ``unknown``.
RUNTIME_BUSY = "busy"
RUNTIME_AWAITING_INPUT = "awaiting_input"
RUNTIME_IDLE = "idle"
RUNTIME_BLOCKED = "blocked"
RUNTIME_TURN_ENDED = "turn_ended"
RUNTIME_UNKNOWN = "unknown"

RUNTIME_STATES = frozenset(
    {
        RUNTIME_BUSY,
        RUNTIME_AWAITING_INPUT,
        RUNTIME_IDLE,
        RUNTIME_BLOCKED,
        RUNTIME_TURN_ENDED,
        RUNTIME_UNKNOWN,
    }
)

# ``next_owner`` — who the next action belongs to. Role names, not providers (the
# #13157 binding decides which pane a role resolves to). ``coordinator`` owns routing
# / delivery repair / integration disposition; ``worker`` is implementing; ``auditor``
# owns the review; ``owner`` owns close approval / ff push.
OWNER_WORKER = "worker"
OWNER_AUDITOR = "auditor"
OWNER_COORDINATOR = "coordinator"
OWNER_OWNER = "owner"
OWNER_NONE = "none"

# Base next-action / next-owner per workflow state class (before a live delivery
# anomaly is considered). A live (non-stale) delivery anomaly overrides the owner to
# the coordinator, because repairing a stuck transport is the coordinator's job — this
# is what turns a "done but not delivered" lane into a visible stall.
_STATE_NEXT: dict[str, tuple[str, str]] = {
    LANE_STATE_IMPLEMENTING: (
        OWNER_WORKER,
        "worker implementing; await implementation_done",
    ),
    LANE_STATE_REVIEW_WAITING: (
        OWNER_AUDITOR,
        "auditor review owed (US-level audit)",
    ),
    LANE_STATE_OWNER_WAITING: (
        OWNER_COORDINATOR,
        "coordinator: collect owner close approval",
    ),
    LANE_STATE_INTEGRATION_WAITING: (
        OWNER_COORDINATOR,
        "coordinator: integration disposition (merge / ff push)",
    ),
    LANE_STATE_CLOSE_WAITING: (
        OWNER_COORDINATOR,
        "coordinator: record close on the durable issue",
    ),
    LANE_STATE_CALLBACK_DUE: (
        OWNER_COORDINATOR,
        "coordinator: callback due; poll durable record or re-route",
    ),
    LANE_STATE_CALLBACK_DELIVERY_FAILED: (
        OWNER_COORDINATOR,
        "coordinator: callback delivery failed; re-route",
    ),
    LANE_STATE_BLOCKED: (
        OWNER_COORDINATOR,
        "coordinator: lane blocked; resolve blocker / design consultation",
    ),
    LANE_STATE_RETIRE_READY: (
        OWNER_COORDINATOR,
        "coordinator: retire the drained lane",
    ),
    LANE_STATE_IDLE: (
        OWNER_COORDINATOR,
        "coordinator: no active durable work; dispatch or leave idle",
    ),
    WORKFLOW_STATE_UNKNOWN: (
        OWNER_COORDINATOR,
        "coordinator: durable gate unresolved (source unavailable / unrecognized "
        "template); verify lane",
    ),
}


def next_action_for_state(state_class: str) -> tuple[str, str]:
    """The base ``(next_owner, next_action)`` for a workflow state class (pure).

    Fail-closed: an unrecognized state class is routed to the coordinator to
    investigate rather than silently dropped.
    """
    return _STATE_NEXT.get(
        state_class,
        (OWNER_COORDINATOR, f"coordinator: investigate unrecognized state {state_class}"),
    )


# ---------------------------------------------------------------------------
# Inputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryObservation:
    """A transport-layer observation joined onto a lane's workflow state.

    Every field is a fact an adapter already produced (a herdr ledger record, a
    runtime pane read); the fold reads them, it does not discover them.

    ``anomaly`` is one of :data:`ANOMALY_*` (an out-of-vocabulary value folds to
    :data:`ANOMALY_UNKNOWN`). ``source`` is one of :data:`DELIVERY_SOURCE_*`.
    ``observed_journal`` is the Redmine journal id (or empty) the observation was made
    against — compared with the lane's latest gate journal to decide whether the
    durable record has already moved past the anomaly (``stale``). ``runtime_state`` is
    one of :data:`RUNTIME_*`; ``receive_method`` one of :data:`RECEIVE_*`.
    """

    anomaly: str = ANOMALY_NONE
    source: str = DELIVERY_SOURCE_NONE
    observed_journal: str = ""
    runtime_state: str = RUNTIME_UNKNOWN
    receive_method: str = RECEIVE_UNKNOWN


@dataclass(frozen=True)
class IssueGlanceSnapshot:
    """One active lane/US's durable-record facts plus an optional delivery observation.

    ``signal`` carries the durable-gate facts the workflow state is folded from (the
    same :class:`LaneSignal` the admission preflight consumes). ``latest_gate_journal``
    is the Redmine journal id the latest gate was recorded at (used to detect a stale
    delivery anomaly). ``subject`` / ``lane`` are display pointers. ``delivery`` is the
    joined transport observation (defaults to a healthy, unobserved delivery).
    """

    issue_id: str
    signal: LaneSignal
    subject: str = ""
    lane: str = ""
    latest_gate_journal: str = ""
    delivery: DeliveryObservation = field(default_factory=DeliveryObservation)
    #: False when the lane was enumerated (it is a real active lane) but its durable gate
    #: could not be resolved — the Redmine source was unavailable, or the journals carried no
    #: recognized ``## Gate:`` template. The fold then reports ``workflow_state=unknown``
    #: instead of a state derived from the (empty / unread) signal, so a degraded lane is a
    #: visible unknown, never a fabricated ``idle`` (Redmine #13435 j#74307).
    durable_facts_available: bool = True
    #: The event-driven reconciler's central-query projection groups (Redmine #13758). Each
    #: defaults to its fail-closed empty facts, so an existing snapshot (a producer that does
    #: not yet fill them) folds to ``unknown`` / blank tokens, never a fabricated authority.
    reconcile: ReconcileFacts = field(default_factory=ReconcileFacts)
    authority: AuthorityFacts = field(default_factory=AuthorityFacts)
    execution: ExecutionSurfaceFacts = field(default_factory=ExecutionSurfaceFacts)


# ---------------------------------------------------------------------------
# Output.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowGlanceRow:
    """One projected glance row: workflow state + next action + delivery anomaly.

    ``workflow_state`` / ``state_class`` are the durable-record classification (equal;
    both names are kept because the design's JSON contract names both). ``latest_gate``
    / ``latest_journal`` are the gate pointer. ``next_action`` / ``next_owner`` are the
    resolved next step. ``delivery_anomaly`` + ``delivery_anomaly_stale`` +
    ``delivery_source`` are the transport dimension; ``runtime_state`` /
    ``receive_method`` are supplementary signals.
    """

    issue_id: str
    subject: str
    lane: str
    workflow_state: str
    state_class: str
    latest_gate: str
    latest_journal: str
    next_action: str
    next_owner: str
    delivery_anomaly: str
    delivery_anomaly_stale: bool
    delivery_source: str
    runtime_state: str
    receive_method: str
    #: The event-driven reconciler's projection groups (Redmine #13758), joined without pane
    #: inspection — the reconcile ladder, the active execution role / provider / authority
    #: transition, and the execution-surface provenance. Each is a fail-closed fixed-token
    #: sub-record; the JSON contract emits them as the ``reconcile`` / ``authority`` /
    #: ``execution_surface`` payload groups.
    reconcile: ReconcileFacts = field(default_factory=ReconcileFacts)
    authority: AuthorityFacts = field(default_factory=AuthorityFacts)
    execution: ExecutionSurfaceFacts = field(default_factory=ExecutionSurfaceFacts)

    @property
    def has_active_anomaly(self) -> bool:
        """True when a delivery anomaly is present and not superseded by a later gate."""
        return self.delivery_anomaly != ANOMALY_NONE and not self.delivery_anomaly_stale

    def as_payload(self) -> dict[str, object]:
        return {
            "issue_id": self.issue_id,
            "subject": self.subject,
            "lane": self.lane,
            "workflow_state": self.workflow_state,
            "state_class": self.state_class,
            "latest_gate": self.latest_gate,
            "latest_journal": self.latest_journal,
            "next_action": self.next_action,
            "next_owner": self.next_owner,
            "delivery_anomaly": self.delivery_anomaly,
            "delivery_anomaly_stale": self.delivery_anomaly_stale,
            "delivery_source": self.delivery_source,
            "runtime_state": self.runtime_state,
            "receive_method": self.receive_method,
            "has_active_anomaly": self.has_active_anomaly,
            "reconcile": self.reconcile.as_payload(),
            "authority": self.authority.as_payload(),
            "execution_surface": self.execution.as_payload(),
        }


def _int_or_none(value: str) -> int | None:
    """Parse a journal id to int for chronological comparison, or None if non-numeric."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _anomaly_is_stale(anomaly: str, observed_journal: str, latest_gate_journal: str) -> bool:
    """True when a later durable gate supersedes the delivery observation.

    A stale anomaly is one recorded at a journal strictly *before* the journal that set
    the lane's latest gate: the durable record already advanced past the transport
    hiccup (the #13392 durable-journal-poll case where a later poll confirmed the gate).
    A healthy (``none``) anomaly is never "stale". When either journal id is missing or
    non-numeric the comparison cannot be made and the anomaly is treated as live
    (fail toward surfacing, not hiding).
    """
    if anomaly == ANOMALY_NONE:
        return False
    observed = _int_or_none(observed_journal)
    latest = _int_or_none(latest_gate_journal)
    if observed is None or latest is None:
        return False
    return observed < latest


def fold_glance_row(snapshot: IssueGlanceSnapshot) -> WorkflowGlanceRow:
    """Fold one issue snapshot into a :class:`WorkflowGlanceRow` (pure).

    ``workflow_state`` is derived **only** from the durable-record
    :class:`LaneSignal` via :func:`classify_lane_state`, so a delivery anomaly can
    never demote it (the design's non-rollback invariant). The delivery observation is
    validated against the vocabularies (an out-of-vocabulary value fails closed to the
    ``unknown`` / ``none`` catch-all) and joined as a separate dimension. A live
    (non-stale) anomaly re-owns the next action to the coordinator, because repairing a
    stuck transport is the coordinator's routing job — this is what makes a
    "completed but not delivered" lane read as a stall instead of silently looking done.

    A snapshot whose ``durable_facts_available`` is False (the lane is real but its durable
    gate could not be resolved) folds to :data:`WORKFLOW_STATE_UNKNOWN`, owned by the
    coordinator to investigate — a visible degraded row, never a fabricated ``idle``.
    """
    if not snapshot.durable_facts_available:
        state_class = WORKFLOW_STATE_UNKNOWN
    else:
        state_class = classify_lane_state(snapshot.signal)

    delivery = snapshot.delivery
    anomaly = delivery.anomaly if delivery.anomaly in DELIVERY_ANOMALIES else ANOMALY_UNKNOWN
    source = delivery.source if delivery.source in DELIVERY_SOURCES else DELIVERY_SOURCE_NONE
    runtime_state = (
        delivery.runtime_state if delivery.runtime_state in RUNTIME_STATES else RUNTIME_UNKNOWN
    )
    receive_method = (
        delivery.receive_method if delivery.receive_method in RECEIVE_METHODS else RECEIVE_UNKNOWN
    )
    stale = _anomaly_is_stale(anomaly, delivery.observed_journal, snapshot.latest_gate_journal)

    next_owner, next_action = next_action_for_state(state_class)
    if anomaly != ANOMALY_NONE and not stale:
        # A live delivery anomaly is a coordinator routing/repair concern regardless of
        # the underlying workflow state (a done lane whose handoff never submitted is a
        # stall the coordinator must clear). The workflow_state itself is untouched.
        next_owner = OWNER_COORDINATOR
        next_action = f"coordinator: resolve delivery anomaly ({anomaly}); {next_action}"

    return WorkflowGlanceRow(
        issue_id=snapshot.issue_id,
        subject=snapshot.subject,
        lane=snapshot.lane,
        workflow_state=state_class,
        state_class=state_class,
        latest_gate=snapshot.signal.latest_gate or GATE_NONE,
        latest_journal=snapshot.latest_gate_journal,
        next_action=next_action,
        next_owner=next_owner,
        delivery_anomaly=anomaly,
        delivery_anomaly_stale=stale,
        delivery_source=source,
        runtime_state=runtime_state,
        receive_method=receive_method,
        # The reconciler projection groups are validated (fail-closed to unknown / blank
        # tokens) and joined as separate dimensions — like the delivery observation, they
        # never demote the durable-record workflow_state.
        reconcile=snapshot.reconcile.validated(),
        authority=snapshot.authority.validated(),
        execution=snapshot.execution.validated(),
    )


def fold_glance_rows(snapshots) -> tuple[WorkflowGlanceRow, ...]:
    """Fold an ordered sequence of snapshots into glance rows (pure; order-stable)."""
    return tuple(fold_glance_row(s) for s in snapshots)


# ---------------------------------------------------------------------------
# Rendering (pure string / payload builders; no I/O).
# ---------------------------------------------------------------------------


def glance_payload(rows, *, degraded: bool = False, notes=()) -> dict[str, object]:
    """The structured ``--json`` envelope for a set of glance rows (pure).

    Carries the per-row payloads plus a small summary (row count, and the issues that
    carry a live delivery anomaly) so a caller / cockpit projection can spot the
    "looks stopped but is really delivery-stuck" lanes without re-deriving them.

    ``degraded`` / ``notes`` report source health: ``degraded`` is true when a source a
    lane needed was unavailable or unreadable (so an empty / partial projection is *not*
    silently read as "nothing active"), and ``notes`` carries the per-source explanations
    (Redmine #13435 j#74295 Finding 1: distinguish "no active lanes" from "source
    unavailable").
    """
    rows = tuple(rows)
    active_anomalies = [r.issue_id for r in rows if r.has_active_anomaly]
    return {
        "rows": [r.as_payload() for r in rows],
        "count": len(rows),
        "active_anomaly_issues": active_anomalies,
        "degraded": bool(degraded),
        "notes": list(notes),
    }


def _truncate(value: str, width: int) -> str:
    value = value or ""
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


def render_glance_table(rows) -> str:
    """A fixed-width human table of glance rows (pure).

    Columns: issue, lane, workflow_state, delivery (anomaly + a ``(stale)`` marker and a
    ``~`` prefix when the signal is a runtime observation, not the durable record),
    runtime, next_owner, next_action. Empty input renders a single explanatory line so
    the surface never prints a bare header with no rows.
    """
    rows = tuple(rows)
    if not rows:
        return "no active lanes/US to glance (durable record empty)"

    def _delivery_cell(r: WorkflowGlanceRow) -> str:
        if r.delivery_anomaly == ANOMALY_NONE:
            return "-"
        marker = r.delivery_anomaly
        if r.delivery_anomaly_stale:
            marker += " (stale)"
        if r.delivery_source == DELIVERY_SOURCE_RUNTIME_OBSERVATION:
            marker = "~" + marker  # observed, not durable
        return marker

    headers = (
        "ISSUE",
        "LANE",
        "WORKFLOW_STATE",
        "DELIVERY",
        "RUNTIME",
        "NEXT_OWNER",
        "NEXT_ACTION",
    )
    cells = [
        (
            r.issue_id,
            _truncate(r.lane, 28),
            r.workflow_state,
            _delivery_cell(r),
            r.runtime_state,
            r.next_owner,
            r.next_action,
        )
        for r in rows
    ]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _line(row) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()

    lines = [_line(headers), _line(tuple("-" * w for w in widths))]
    lines.extend(_line(row) for row in cells)
    return "\n".join(lines)


__all__ = (
    "WORKFLOW_STATE_UNKNOWN",
    "ANOMALY_NONE",
    "ANOMALY_TURN_START_UNCONFIRMED",
    "ANOMALY_STAGED_NOT_SUBMITTED",
    "ANOMALY_MARKER_UNOBSERVED",
    "ANOMALY_CALLBACK_SELF_LOOP",
    "ANOMALY_CALLBACK_DELIVERY_FAILED",
    "ANOMALY_CALLBACK_NOT_ATTEMPTED",
    "ANOMALY_UNKNOWN",
    "DELIVERY_ANOMALIES",
    "DELIVERY_SOURCE_NONE",
    "DELIVERY_SOURCE_REDMINE_JOURNAL",
    "DELIVERY_SOURCE_HERDR_LEDGER",
    "DELIVERY_SOURCE_RUNTIME_OBSERVATION",
    "DELIVERY_SOURCES",
    "RECEIVE_DURABLE_JOURNAL_POLL",
    "RECEIVE_CALLBACK",
    "RECEIVE_UNKNOWN",
    "RECEIVE_METHODS",
    "RUNTIME_BUSY",
    "RUNTIME_AWAITING_INPUT",
    "RUNTIME_IDLE",
    "RUNTIME_BLOCKED",
    "RUNTIME_TURN_ENDED",
    "RUNTIME_UNKNOWN",
    "RUNTIME_STATES",
    "OWNER_WORKER",
    "OWNER_AUDITOR",
    "OWNER_COORDINATOR",
    "OWNER_OWNER",
    "OWNER_NONE",
    "next_action_for_state",
    "DeliveryObservation",
    "IssueGlanceSnapshot",
    "WorkflowGlanceRow",
    "fold_glance_row",
    "fold_glance_rows",
    "glance_payload",
    "render_glance_table",
)
