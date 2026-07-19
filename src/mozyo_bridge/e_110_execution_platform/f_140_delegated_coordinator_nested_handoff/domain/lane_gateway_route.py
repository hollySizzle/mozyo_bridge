"""Same-lane gateway route for worker-produced gate markers (Redmine #13683 R2, design answer j#82367).

The pure policy that routes a worker's ``implementation_done`` / ``review_request`` gate marker to the
issue's OWN owning-lane implementation_gateway (the same-lane Codex reviewer), generation-fenced — NOT
the coordinator. The pre-R2 blanket coordinator route sent EVERY gate to the ``default`` lane, so a
same-lane ``review_request`` woke the coordinator and never the reviewing gateway (installed a16
j#82329: the target same-lane gateway stayed ``turn_ended``).

Design-answer boundaries (j#82367), enforced here as pure policy:

- **A** — a DISTINCT route key ``lane_gateway:<lane_id>`` (never the ``review_return`` machinery):
  worker → same-lane gateway and coordinator → owning gateway review-return are different purposes with
  different idempotency partitions, so they never collide on one outbox key.
- **B** — these gates are routed ONLY to the same-lane gateway; the caller excludes them from the
  coordinator route, so there is no double wake.
- **C** — #13684 review_return topology is untouched; this adds ONLY the worker-gate lane route, and a
  gate that cannot be classified to a single active non-coordinator owner is fail-closed (a refusal,
  never a guess) — the gateway never self-routes its own review_result.

The application reads the durable owning-lane binding (#13681/#13689) + the issue's structured markers
and consults these evaluators; this module authorizes nothing and reads no I/O. The target lane /
gateway receiver / generation come only from the owning-lane binding (never a pane / issue-id guess).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    JournalMarker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    COORDINATOR_LANE,
    OWNER_AMBIGUOUS,
    OWNER_RESOLVED,
    OwningLaneBinding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    GATE_IMPLEMENTATION_DONE,
    GATE_REVIEW,
    GATE_REVIEW_REQUEST,
)

#: The callback-route prefix for a worker gate returned to its same-lane gateway. Part of the outbox
#: idempotency key (``callback_route``), deliberately DISTINCT from
#: :data:`...callback_runtime.DEFAULT_CALLBACK_ROUTE` (``coordinator``) and
#: :data:`...review_return_route.REVIEW_RETURN_ROUTE_PREFIX` (``review_return``) so a worker-gate wake,
#: a coordinator callback, and a review-result return never collide on one key (design answer j#82367 A).
LANE_GATEWAY_ROUTE_PREFIX = "lane_gateway"

#: The worker-produced gate kinds that route to the same-lane gateway (design answer j#82367 B). Every
#: other gate (``review`` / ``review_result`` / progress / blocker / …) keeps its existing route.
LANE_GATEWAY_GATES = frozenset({GATE_IMPLEMENTATION_DONE, GATE_REVIEW_REQUEST})

# ---------------------------------------------------------------------------
# Refusal vocabulary (machine-readable; literal regardless of UI language). Every one is a fail-closed
# zero-send that the caller surfaces so a refusal is operator-visible, never a silent drop.
# ---------------------------------------------------------------------------
LANE_SEND_OK = "lane_gateway_ok"
LANE_NO_OWNER = "no_active_owner"  # resolve_owner found no active owning lane (absent / unknown)
LANE_AMBIGUOUS_OWNER = "ambiguous_owner"  # more than one active owner (fail-closed, never guess)
LANE_SELF_ROUTE = "self_route"  # the owning lane IS the coordinator lane — no separate gateway to wake
LANE_NO_GATEWAY = "no_gateway_receiver"  # the owning-lane gateway provider did not resolve
LANE_BLANK_GENERATION = "blank_owning_generation"  # the owning lane carries no generation stamp
#: The gate marker predates the current owning lane+generation's dispatch anchor (a previous-generation
#: gate on a restarted lane). Only meaningful when a dispatch anchor is supplied (the fenced production
#: supervisor); a supplied-but-unresolvable anchor also lands here (fail-closed — a generation we cannot
#: pin never authorizes a send).
LANE_PREVIOUS_GENERATION = "previous_generation_gate"
#: The worker gate has already been answered by a later review-side gate (``review`` / ``review_result``)
#: on the issue — the gateway ALREADY reviewed this work, so re-waking it to "please review" would be a
#: spurious duplicate wake (design answer j#82367: no over-send / no duplicate wake). A worker gate wakes
#: the gateway only while it still AWAITS gateway action; a fresh unreviewed request, and a later re-work
#: request whose journal is newer than the last review, are unshadowed and still send. Mirrors the
#: review_return latest-unshadowed philosophy (:data:`...review_return_route.RETURN_NOT_LATEST`).
LANE_SHADOWED = "shadowed_by_completed_review"

#: The refusal reasons — a fail-closed no-send (never a guessed / self / foreign / stale / owner-less /
#: already-reviewed gateway send).
LANE_REFUSAL_REASONS = frozenset(
    {
        LANE_NO_OWNER,
        LANE_AMBIGUOUS_OWNER,
        LANE_SELF_ROUTE,
        LANE_NO_GATEWAY,
        LANE_BLANK_GENERATION,
        LANE_PREVIOUS_GENERATION,
        LANE_SHADOWED,
    }
)


def lane_gateway_route(lane_id: str) -> str:
    """Build the ``lane_gateway:<lane_id>`` route (pure). A blank lane id cannot address a gateway.

    Encodes the owning lane so the outbox idempotency key partitions worker-gate wakes by owner — a
    supersession to a new lane is a new key (new row) while the old-owner row fails closed. A blank
    lane id raises (a fail-closed programming error, never a silent ``lane_gateway:`` route that would
    collide across owners).
    """
    lane = str(lane_id or "").strip()
    if not lane:
        raise ValueError("lane_gateway route requires a non-empty owning lane id")
    return f"{LANE_GATEWAY_ROUTE_PREFIX}:{lane}"


def is_lane_gateway_route(route: str) -> bool:
    """Whether ``route`` is a same-lane gateway route (``lane_gateway:<lane>``) (pure)."""
    return str(route or "").strip().startswith(f"{LANE_GATEWAY_ROUTE_PREFIX}:")


def _as_int(value: object) -> Optional[int]:
    """Parse a Redmine journal id to ``int`` for chronological compare (``None`` if non-numeric)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class LaneGatewaySendPlan:
    """The pure plan for waking one worker gate's same-lane implementation_gateway.

    ``emit`` is True only when every fail-closed check passes; ``reason`` is a member of
    :data:`LANE_REFUSAL_REASONS` on refusal (or :data:`LANE_SEND_OK` on emit). When ``emit`` is True the
    ``callback_route`` / ``target_lane`` / ``target_receiver`` / ``target_generation`` are the durable
    correlation the application stamps on the outbox row so the background_service delivery authority
    binds the re-resolved live target + independently-read live generation to it. ``gate_journal`` is the
    worker gate marker's durable journal anchor; ``gate`` is the marker's gate kind.
    """

    emit: bool
    reason: str
    callback_route: str = ""
    target_lane: str = ""
    target_receiver: str = ""
    target_generation: str = ""
    gate_journal: str = ""
    gate: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "emit": self.emit,
            "reason": self.reason,
            "callback_route": self.callback_route,
            "target_lane": self.target_lane,
            "target_receiver": self.target_receiver,
            "target_generation": self.target_generation,
            "gate_journal": self.gate_journal,
            "gate": self.gate,
        }


def _plan_one(
    marker: JournalMarker,
    owner: OwningLaneBinding,
    *,
    dispatch_anchor_journal: Optional[str],
    latest_review_journal: Optional[int],
) -> LaneGatewaySendPlan:
    gate = str(getattr(marker, "gate", "") or "").strip()
    journal = str(getattr(marker, "journal", "") or "").strip()
    status = str(getattr(owner, "status", "") or "").strip()
    if status != OWNER_RESOLVED:
        reason = LANE_AMBIGUOUS_OWNER if status == OWNER_AMBIGUOUS else LANE_NO_OWNER
        return LaneGatewaySendPlan(emit=False, reason=reason, gate_journal=journal, gate=gate)
    lane = str(getattr(owner, "lane_id", "") or "").strip()
    if not lane or lane == COORDINATOR_LANE:
        # The coordinator lane owns the issue directly — there is no separate same-lane gateway to
        # wake, and waking the coordinator here would be the self-route the design answer forbids.
        return LaneGatewaySendPlan(
            emit=False, reason=LANE_SELF_ROUTE, gate_journal=journal, gate=gate
        )
    gateway = str(getattr(owner, "gateway_receiver", "") or "").strip()
    if not gateway:
        return LaneGatewaySendPlan(
            emit=False, reason=LANE_NO_GATEWAY, gate_journal=journal, gate=gate
        )
    generation = str(getattr(owner, "generation", "") or "").strip()
    if not generation:
        return LaneGatewaySendPlan(
            emit=False, reason=LANE_BLANK_GENERATION, gate_journal=journal, gate=gate
        )
    # The shadowing fence (design answer j#82367 — no over-send / no duplicate wake): a worker gate
    # already answered by a LATER review-side gate on the issue means the gateway ALREADY reviewed this
    # work, so re-waking it would be spurious. A fresh unreviewed request (no later review) and a re-work
    # request whose journal is newer than the last review are unshadowed and still send.
    gate_journal_int = _as_int(journal)
    if (
        latest_review_journal is not None
        and gate_journal_int is not None
        and gate_journal_int < latest_review_journal
    ):
        return LaneGatewaySendPlan(
            emit=False, reason=LANE_SHADOWED, gate_journal=journal, gate=gate
        )
    # The generation fence (design answer j#82367): a worker gate on a journal OLDER than the current
    # owning lane+generation's dispatch anchor is a previous-generation gate on a restarted lane, and a
    # supplied-but-unresolvable anchor (``""``) is fail-closed — a generation we cannot pin never sends.
    if dispatch_anchor_journal is not None:
        anchor = _as_int(dispatch_anchor_journal)
        gate_journal = _as_int(journal)
        if anchor is None or gate_journal is None or gate_journal < anchor:
            return LaneGatewaySendPlan(
                emit=False, reason=LANE_PREVIOUS_GENERATION, gate_journal=journal, gate=gate
            )
    return LaneGatewaySendPlan(
        emit=True,
        reason=LANE_SEND_OK,
        callback_route=lane_gateway_route(lane),
        target_lane=lane,
        target_receiver=gateway,
        target_generation=generation,
        gate_journal=journal,
        gate=gate,
    )


def plan_lane_gateway_sends(
    markers: Iterable[JournalMarker],
    issue: str,
    owner: OwningLaneBinding,
    *,
    dispatch_anchor_journal: Optional[str] = None,
) -> list[LaneGatewaySendPlan]:
    """Plan a same-lane gateway wake for each worker gate marker on ``issue`` (pure, fail-closed).

    For every ``implementation_done`` / ``review_request`` marker (:data:`LANE_GATEWAY_GATES`) the plan
    resolves the durable owning-lane binding to a single active non-coordinator owner + its gateway
    receiver + generation, refusing (fail-closed) on: no active owner, ambiguous owner, a coordinator
    self-route, an unresolved gateway, a blank generation, or a previous-generation gate (older than the
    threaded ``dispatch_anchor_journal``). ``dispatch_anchor_journal=None`` skips the generation fence
    (the unfenced supervisor); a supplied blank / unresolvable anchor fails closed. Non-worker gates
    yield no plan (they keep their existing route). Returns one plan per worker gate marker (emit +
    refusals) so the caller can enqueue the emits and surface the refusals (observability).
    """
    issue_s = str(issue or "").strip()
    marker_list = [
        m for m in markers if str(getattr(m, "issue", "") or "").strip() == issue_s
    ]
    # The latest review-side journal on the issue (``review`` / ``review_result`` both normalize to
    # GATE_REVIEW). A worker gate older than it has already been reviewed → shadowed (see ``_plan_one``).
    review_journals = [
        j
        for m in marker_list
        if str(getattr(m, "gate", "") or "").strip() == GATE_REVIEW
        for j in (_as_int(getattr(m, "journal", "")),)
        if j is not None
    ]
    latest_review_journal = max(review_journals) if review_journals else None
    plans: list[LaneGatewaySendPlan] = []
    for marker in marker_list:
        if str(getattr(marker, "gate", "") or "").strip() not in LANE_GATEWAY_GATES:
            continue
        plans.append(
            _plan_one(
                marker, owner,
                dispatch_anchor_journal=dispatch_anchor_journal,
                latest_review_journal=latest_review_journal,
            )
        )
    return plans


def make_lane_gateway_send_edge_fence(anchor: object):
    """Build a per-row send-edge fence for PRE-EXISTING ``lane_gateway`` backlog rows (j#82367).

    Returns ``send_fence_fn(row) -> (fence, reason)``. A row is fenced when it is a ``lane_gateway:``
    route AND its journal is older than ``anchor`` (a previous-generation gate), or ``anchor`` is
    unresolvable (``None`` / blank / non-numeric — fail-closed). Non-``lane_gateway`` rows are exempt
    (they carry their own route's fence). This stops a pre-existing pending / recovered-then-reclaimed
    worker-gate row from waking a new generation's gateway — the ingest-side plan fence only stops newly
    discovered markers. The reason token is secret-safe. Pure and duck-typed on ``row.callback_route`` /
    ``row.journal``.
    """
    anchor_int = _as_int(anchor)

    def _fence(row) -> tuple[bool, str]:
        if not is_lane_gateway_route(str(getattr(row, "callback_route", "") or "")):
            return (False, "")  # another route: own fence, exempt
        journal = _as_int(getattr(row, "journal", ""))
        if anchor_int is not None and journal is not None and journal >= anchor_int:
            return (False, "")
        reason = (
            "fenced: dispatch anchor unresolvable"
            if anchor_int is None
            else "superseded: gate journal older than current dispatch anchor"
        )
        return (True, reason)

    return _fence


__all__ = (
    "LANE_GATEWAY_ROUTE_PREFIX",
    "LANE_GATEWAY_GATES",
    "LANE_SEND_OK",
    "LANE_NO_OWNER",
    "LANE_AMBIGUOUS_OWNER",
    "LANE_SELF_ROUTE",
    "LANE_NO_GATEWAY",
    "LANE_BLANK_GENERATION",
    "LANE_PREVIOUS_GENERATION",
    "LANE_SHADOWED",
    "LANE_REFUSAL_REASONS",
    "lane_gateway_route",
    "is_lane_gateway_route",
    "LaneGatewaySendPlan",
    "plan_lane_gateway_sends",
    "make_lane_gateway_send_edge_fence",
)
