"""Coordinator dependency drain-queue projection (Redmine #13967).

The coordinator-sublane spine (``coordinator-sublane-development-flow.md`` `### Drain
Order`) fixes the order in which a coordinator drains the work only it can do —
production/release blockers, ``owner_waiting``, ``review_waiting``,
``integration_waiting``, ``close_waiting``, ``blocked`` / ``callback_due``,
``retire_ready`` — before opening optional new work. `### Lane Actionability` adds the
orthogonal question *who owns the next action*. Until now that queue only lived as a
per-lane fill decision; a coordinator deciding **whether it still needs to keep an
active process resident** (the early-hibernate question, Redmine #13967 item 1) had to
hand-re-derive it.

This module is the pure, read-only **projection**: given the already-classified active
lane set (each lane's :data:`...workflow_fill_decision.LANE_STATE_*` class, its resolved
:data:`...lane_actionability.ACTIONABILITY_*`, and whether its remaining obligation is a
centralized release/dogfood one), it buckets the lanes into the fixed drain-queue
vocabulary and returns, per bucket, the actionable-vs-non-actionable ownership split plus
a single :data:`PROCESS_*` retention verdict.

Design invariants (they mirror the spine and the fill policy, never a second authority):

- **the bucket vocabulary is the drain order.** :data:`DRAIN_BUCKETS` is exactly the
  anchor's ``callback / review / owner / integration / close / blocked / retirement /
  release-dogfood`` list, in drain order. The state→bucket map reuses the
  :mod:`...domain.workflow_fill_decision` state authority; this module does not invent a
  second state machine.
- **release_dogfood is the delegated terminal bucket.** A lane whose only remaining
  obligation is the centralized TestPyPI / installed dogfood (Redmine #13967 item 2 —
  delegated to a dedicated release issue) routes here **only when no coordinator-blocking
  drain remains** on it. A lane that still owes a review / callback / owner / integration
  / close / blocker is bucketed by that blocking obligation first — a delegated dogfood
  never hides live coordinator debt.
- **process retention is earned, not declared.** :data:`PROCESS_HOLD` fires only when a
  :data:`PROCESS_HOLDING_BUCKETS` bucket holds a lane whose *effective* actionability is
  ``coordinator_actionable`` — the coordinator has drain work it alone can do now.
  ``retirement`` and ``release_dogfood`` are deliberately **excluded** from the holding
  set: retirement is a batchable cleanup cadence and release-dogfood is delegated to the
  release issue, so neither alone forces the feature-lane coordinator process to stay
  resident (this is what lets a review-approved + integrated lane hibernate early while
  its dogfood/close ride the release issue).
- **fail-closed.** An unrecognized actionability token resolves to
  ``coordinator_actionable`` (the blocking sink); an unrecognized state class routes to
  :data:`BUCKET_UNKNOWN` (surfaced, never dropped). A lane is never silently omitted.

Scope: this module discovers nothing — every :class:`DrainLane` field is supplied by the
caller from the durable record (the CLI in ``cli_workflow_drain`` resolves the state /
actionability from the same read model ``workflow glance`` / ``fill-decision`` use).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_actionability import (
    ACTIONABILITIES,
    ACTIONABILITY_COORDINATOR_ACTIONABLE,
    ACTIONABILITY_DELEGATED_IN_FLIGHT,
    ACTIONABILITY_NON_ACTIONABLE_WAIT,
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

# ---------------------------------------------------------------------------
# Drain-queue bucket vocabulary (machine-readable; literal regardless of UI language).
#
# The eight DRAIN_BUCKETS are exactly the anchor's list (Redmine #13967 item 5) in the
# spine's `### Drain Order`. The non-drain buckets exist so a lane that owes nothing to
# the queue (a worker is implementing, the lane is idle, or its state was unreadable) is
# still projected explicitly rather than dropped.
# ---------------------------------------------------------------------------

BUCKET_CALLBACK = "callback"
BUCKET_REVIEW = "review"
BUCKET_OWNER = "owner"
BUCKET_INTEGRATION = "integration"
BUCKET_CLOSE = "close"
BUCKET_BLOCKED = "blocked"
BUCKET_RETIREMENT = "retirement"
BUCKET_RELEASE_DOGFOOD = "release_dogfood"

# Non-drain (informational) buckets.
BUCKET_IMPLEMENTING = "implementing"
BUCKET_IDLE = "idle"
BUCKET_UNKNOWN = "unknown"

# The eight drain buckets, in drain order (production/release blockers fold into the
# callback/blocked/owner buckets by state; the projection preserves this order).
DRAIN_BUCKETS = (
    BUCKET_CALLBACK,
    BUCKET_REVIEW,
    BUCKET_OWNER,
    BUCKET_INTEGRATION,
    BUCKET_CLOSE,
    BUCKET_BLOCKED,
    BUCKET_RETIREMENT,
    BUCKET_RELEASE_DOGFOOD,
)

NON_DRAIN_BUCKETS = (
    BUCKET_IMPLEMENTING,
    BUCKET_IDLE,
    BUCKET_UNKNOWN,
)

BUCKETS = DRAIN_BUCKETS + NON_DRAIN_BUCKETS

# The drain buckets whose work only the main coordinator can perform *now*. A
# coordinator_actionable lane in one of these forces :data:`PROCESS_HOLD`. Mirrors
# :data:`...workflow_fill_decision.COORDINATOR_BLOCKING_STATES`. `retirement` /
# `release_dogfood` are intentionally absent (see the module docstring).
PROCESS_HOLDING_BUCKETS = frozenset(
    {
        BUCKET_CALLBACK,
        BUCKET_REVIEW,
        BUCKET_OWNER,
        BUCKET_INTEGRATION,
        BUCKET_CLOSE,
        BUCKET_BLOCKED,
    }
)

# State class -> drain bucket. Reuses the fill-decision state authority so the drain
# queue and the fill preflight classify a lane identically.
_STATE_BUCKET: dict[str, str] = {
    LANE_STATE_CALLBACK_DUE: BUCKET_CALLBACK,
    LANE_STATE_CALLBACK_DELIVERY_FAILED: BUCKET_CALLBACK,
    LANE_STATE_REVIEW_WAITING: BUCKET_REVIEW,
    LANE_STATE_OWNER_WAITING: BUCKET_OWNER,
    LANE_STATE_INTEGRATION_WAITING: BUCKET_INTEGRATION,
    LANE_STATE_CLOSE_WAITING: BUCKET_CLOSE,
    LANE_STATE_BLOCKED: BUCKET_BLOCKED,
    LANE_STATE_RETIRE_READY: BUCKET_RETIREMENT,
    LANE_STATE_IMPLEMENTING: BUCKET_IMPLEMENTING,
    LANE_STATE_IDLE: BUCKET_IDLE,
}


def bucket_for_state(state_class: str, *, release_pending: bool = False) -> str:
    """Map a lane state class (+ release-pending flag) to a drain bucket (pure).

    ``release_pending`` routes a lane into :data:`BUCKET_RELEASE_DOGFOOD` **only** when it
    carries no coordinator-blocking drain (its base bucket is not a
    :data:`PROCESS_HOLDING_BUCKETS` one) — a delegated dogfood never masks a live review /
    callback / owner / integration / close / blocker. An unrecognized state class is
    surfaced as :data:`BUCKET_UNKNOWN`, never dropped.
    """
    base = _STATE_BUCKET.get(state_class, BUCKET_UNKNOWN)
    if release_pending and base not in PROCESS_HOLDING_BUCKETS:
        return BUCKET_RELEASE_DOGFOOD
    return base


# ---------------------------------------------------------------------------
# Process-retention verdict vocabulary.
# ---------------------------------------------------------------------------

PROCESS_HOLD = "hold"
PROCESS_RELEASABLE = "releasable"


# ---------------------------------------------------------------------------
# Inputs / outputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DrainLane:
    """One active lane, already classified from the durable record (fail-closed defaults).

    - ``state_class`` — the lane's :data:`...workflow_fill_decision.LANE_STATE_*` class.
    - ``actionability`` — the *effective* :data:`...lane_actionability.ACTIONABILITY_*`
      (already resolved by :func:`...lane_actionability.resolve_actionability`; an
      unrecognized token folds to ``coordinator_actionable``, the blocking sink).
    - ``next_action_owner`` — display pointer for who owns the next action (advisory).
    - ``release_pending`` — the lane's remaining obligation is the centralized TestPyPI /
      installed dogfood delegated to the dedicated release issue (Redmine #13967 item 2).
    - ``reason`` — the fixed actionability reason token (advisory; carried for the journal).
    """

    issue: str
    state_class: str
    actionability: str = ACTIONABILITY_COORDINATOR_ACTIONABLE
    next_action_owner: str = ""
    lane: str = ""
    release_pending: bool = False
    reason: str = ""

    @property
    def bucket(self) -> str:
        return bucket_for_state(self.state_class, release_pending=self.release_pending)

    @property
    def effective_actionability(self) -> str:
        """The validated actionability — an out-of-vocabulary token fails closed."""
        return (
            self.actionability
            if self.actionability in ACTIONABILITIES
            else ACTIONABILITY_COORDINATOR_ACTIONABLE
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "state_class": self.state_class,
            "bucket": self.bucket,
            "actionability": self.effective_actionability,
            "next_action_owner": self.next_action_owner,
            "release_pending": bool(self.release_pending),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DrainBucketProjection:
    """One drain bucket's ownership split (pure)."""

    bucket: str
    total: int
    coordinator_actionable: int
    delegated_in_flight: int
    non_actionable_wait: int
    issues: tuple[str, ...]

    @property
    def is_drain_bucket(self) -> bool:
        return self.bucket in DRAIN_BUCKETS

    @property
    def holds_process(self) -> bool:
        """True when this bucket forces the coordinator to keep an active process."""
        return self.bucket in PROCESS_HOLDING_BUCKETS and self.coordinator_actionable > 0

    def as_payload(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "total": self.total,
            "coordinator_actionable": self.coordinator_actionable,
            "delegated_in_flight": self.delegated_in_flight,
            "non_actionable_wait": self.non_actionable_wait,
            "issues": list(self.issues),
            "holds_process": self.holds_process,
        }


@dataclass(frozen=True)
class DrainQueueProjection:
    """The bucketed drain queue + the single process-retention verdict (pure)."""

    buckets: tuple[DrainBucketProjection, ...]
    process_retention: str
    hold_buckets: tuple[str, ...]
    coordinator_actionable_total: int
    retirement_pending: int
    release_dogfood_pending: int
    lane_count: int

    @property
    def process_releasable(self) -> bool:
        return self.process_retention == PROCESS_RELEASABLE

    def bucket(self, name: str) -> DrainBucketProjection | None:
        for entry in self.buckets:
            if entry.bucket == name:
                return entry
        return None

    def as_payload(self) -> dict[str, object]:
        return {
            "process_retention": self.process_retention,
            "hold_buckets": list(self.hold_buckets),
            "coordinator_actionable_total": self.coordinator_actionable_total,
            "retirement_pending": self.retirement_pending,
            "release_dogfood_pending": self.release_dogfood_pending,
            "lane_count": self.lane_count,
            "buckets": [b.as_payload() for b in self.buckets],
        }


def _bucket_projection(name: str, lanes: list[DrainLane]) -> DrainBucketProjection:
    coordinator = sum(
        1 for l in lanes if l.effective_actionability == ACTIONABILITY_COORDINATOR_ACTIONABLE
    )
    delegated = sum(
        1 for l in lanes if l.effective_actionability == ACTIONABILITY_DELEGATED_IN_FLIGHT
    )
    external = sum(
        1 for l in lanes if l.effective_actionability == ACTIONABILITY_NON_ACTIONABLE_WAIT
    )
    return DrainBucketProjection(
        bucket=name,
        total=len(lanes),
        coordinator_actionable=coordinator,
        delegated_in_flight=delegated,
        non_actionable_wait=external,
        issues=tuple(l.issue for l in lanes),
    )


def project_drain_queue(lanes: Iterable[DrainLane]) -> DrainQueueProjection:
    """Fold the active lane set into the bucketed drain queue + retention verdict (pure).

    Every one of the eight :data:`DRAIN_BUCKETS` is always emitted (even empty) so the
    projection is a stable contract; a non-drain bucket
    (``implementing`` / ``idle`` / ``unknown``) is emitted only when it holds a lane.
    The verdict is :data:`PROCESS_HOLD` iff some :data:`PROCESS_HOLDING_BUCKETS` bucket
    holds a ``coordinator_actionable`` lane, else :data:`PROCESS_RELEASABLE`.
    """
    lanes = tuple(lanes)
    grouped: dict[str, list[DrainLane]] = {}
    for lane in lanes:
        grouped.setdefault(lane.bucket, []).append(lane)

    buckets: list[DrainBucketProjection] = []
    for name in DRAIN_BUCKETS:
        buckets.append(_bucket_projection(name, grouped.get(name, [])))
    for name in NON_DRAIN_BUCKETS:
        entries = grouped.get(name)
        if entries:
            buckets.append(_bucket_projection(name, entries))

    hold_buckets = tuple(b.bucket for b in buckets if b.holds_process)
    coordinator_total = sum(
        b.coordinator_actionable for b in buckets if b.bucket in PROCESS_HOLDING_BUCKETS
    )
    retirement = next(
        (b.total for b in buckets if b.bucket == BUCKET_RETIREMENT), 0
    )
    release_dogfood = next(
        (b.total for b in buckets if b.bucket == BUCKET_RELEASE_DOGFOOD), 0
    )
    retention = PROCESS_HOLD if hold_buckets else PROCESS_RELEASABLE
    return DrainQueueProjection(
        buckets=tuple(buckets),
        process_retention=retention,
        hold_buckets=hold_buckets,
        coordinator_actionable_total=coordinator_total,
        retirement_pending=retirement,
        release_dogfood_pending=release_dogfood,
        lane_count=len(lanes),
    )


# ---------------------------------------------------------------------------
# Rendering (pure string / payload builders; no I/O).
# ---------------------------------------------------------------------------


def drain_queue_payload(projection: DrainQueueProjection) -> dict[str, object]:
    """The structured ``--json`` envelope (pure)."""
    return projection.as_payload()


def render_drain_queue_table(projection: DrainQueueProjection) -> str:
    """A fixed-width human table of the drain queue + the retention verdict (pure)."""
    headers = (
        "BUCKET",
        "TOTAL",
        "COORD_ACTIONABLE",
        "DELEGATED",
        "EXTERNAL_WAIT",
        "HOLDS_PROCESS",
        "ISSUES",
    )
    cells = [
        (
            b.bucket,
            str(b.total),
            str(b.coordinator_actionable),
            str(b.delegated_in_flight),
            str(b.non_actionable_wait),
            "yes" if b.holds_process else "-",
            ",".join(b.issues) if b.issues else "-",
        )
        for b in projection.buckets
    ]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _line(row) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()

    lines = [_line(headers), _line(tuple("-" * w for w in widths))]
    lines.extend(_line(row) for row in cells)
    verdict = (
        f"process_retention: {projection.process_retention}"
        + (
            f" (hold: {', '.join(projection.hold_buckets)})"
            if projection.hold_buckets
            else ""
        )
    )
    tail = (
        f"retirement_pending={projection.retirement_pending} "
        f"release_dogfood_pending={projection.release_dogfood_pending}"
    )
    lines.append("")
    lines.append(verdict)
    lines.append(tail)
    return "\n".join(lines)


__all__ = (
    "BUCKET_CALLBACK",
    "BUCKET_REVIEW",
    "BUCKET_OWNER",
    "BUCKET_INTEGRATION",
    "BUCKET_CLOSE",
    "BUCKET_BLOCKED",
    "BUCKET_RETIREMENT",
    "BUCKET_RELEASE_DOGFOOD",
    "BUCKET_IMPLEMENTING",
    "BUCKET_IDLE",
    "BUCKET_UNKNOWN",
    "DRAIN_BUCKETS",
    "NON_DRAIN_BUCKETS",
    "BUCKETS",
    "PROCESS_HOLDING_BUCKETS",
    "PROCESS_HOLD",
    "PROCESS_RELEASABLE",
    "bucket_for_state",
    "DrainLane",
    "DrainBucketProjection",
    "DrainQueueProjection",
    "project_drain_queue",
    "drain_queue_payload",
    "render_drain_queue_table",
)
