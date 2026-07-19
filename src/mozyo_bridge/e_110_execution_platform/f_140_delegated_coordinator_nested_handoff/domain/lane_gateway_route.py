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
    current_review_generation_head,
    is_full_commit_head,
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
#: The gate is the issue's LATEST ``review_request`` (it LOOKS like the current gate) but its
#: ``target_head`` is not a confirmable full commit head — missing / malformed / ambiguous (Redmine
#: #14094). Distinct from :data:`LANE_PREVIOUS_GENERATION` (a genuine historical fence: an older
#: superseded request or a non-review_request gate) so a refusal diagnostic separates "current-head
#: rejected" from "historical fence" (secret-safe: names neither the SHA nor a locator). A fail-closed
#: zero-send — a current generation we cannot pin by a unique full head never authorizes a send, and a
#: prose SHA is never guessed to complete it.
LANE_CURRENT_HEAD_UNCONFIRMED = "current_head_unconfirmed"
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
        LANE_CURRENT_HEAD_UNCONFIRMED,
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


def latest_review_journal(markers: Iterable[JournalMarker], issue: str) -> Optional[int]:
    """The newest review-side (``review`` / ``review_result`` -> GATE_REVIEW) journal id on ``issue``.

    ``None`` when the issue carries no numeric review-side marker. This is the shadowing authority a
    worker gate is measured against: a worker gate older than it has already been reviewed by the
    gateway (:func:`gate_is_shadowed`). Pure and duck-typed on ``marker.issue`` / ``.gate`` / ``.journal``.
    """
    issue_s = str(issue or "").strip()
    review_journals = [
        j
        for m in markers
        if str(getattr(m, "issue", "") or "").strip() == issue_s
        and str(getattr(m, "gate", "") or "").strip() == GATE_REVIEW
        for j in (_as_int(getattr(m, "journal", "")),)
        if j is not None
    ]
    return max(review_journals) if review_journals else None


def gate_is_shadowed(gate_journal: object, latest_review: Optional[int]) -> bool:
    """Has the worker gate at ``gate_journal`` already been answered by a later review? (pure).

    True iff a numeric ``latest_review`` exists and the numeric gate journal is strictly older than it —
    the gateway already reviewed this work, so a (re-)wake would be spurious. A fresh unreviewed gate
    (``latest_review is None``) and a re-work gate newer than the last review are NOT shadowed.
    """
    gj = _as_int(gate_journal)
    return latest_review is not None and gj is not None and gj < latest_review


def latest_review_request_journal(markers: Iterable[JournalMarker], issue: str) -> Optional[int]:
    """The newest ``review_request`` marker journal id on ``issue``, or ``None`` (pure; Redmine #14094).

    Redmine journal ids are monotonic, so the greatest id is the most recent review round's request.
    ``None`` when the issue carries no numeric ``review_request`` marker. Duck-typed on
    ``marker.issue`` / ``.gate`` / ``.journal``.
    """
    issue_s = str(issue or "").strip()
    request_journals = [
        j
        for m in markers
        if str(getattr(m, "issue", "") or "").strip() == issue_s
        and str(getattr(m, "gate", "") or "").strip() == GATE_REVIEW_REQUEST
        for j in (_as_int(getattr(m, "journal", "")),)
        if j is not None
    ]
    return max(request_journals) if request_journals else None


def current_review_request_journal(markers: Iterable[JournalMarker], issue: str) -> str:
    """The journal id of the CURRENT review generation's full-head ``review_request``, or ``""`` (#14094).

    The "current decision anchor" for a RESUMED correction lane whose fresh generation carries no new
    ``implementation_request`` marker (so the IR dispatch anchor is unresolvable): the current gate is
    then pinned by the issue's latest ``review_request`` itself, but ONLY when its ``target_head`` is a
    confirmable full commit head that equals the current review generation head
    (:func:`...review_return_route.current_review_generation_head` — blank on a missing / malformed /
    ambiguous head). ``""`` when there is no ``review_request`` marker, or the latest one's head is not a
    confirmable full head (fail-closed — never a prose SHA guess). The value is a durable journal id (no
    SHA / locator), so it is secret-safe to thread into the send-edge fence.
    """
    marker_list = list(markers)
    latest = latest_review_request_journal(marker_list, issue)
    if latest is None:
        return ""
    if not is_full_commit_head(current_review_generation_head(marker_list, issue)):
        return ""
    return str(latest)


def _current_generation_rescue(
    marker: JournalMarker, marker_list: "list[JournalMarker]", issue: str, gate: str
) -> Optional[str]:
    """Classify a worker gate under an UNRESOLVABLE dispatch anchor (pure; Redmine #14094).

    A RESUMED correction lane opens a fresh generation WITHOUT a new ``implementation_request`` marker,
    so its IR dispatch anchor is unresolvable (``""``) even though the lane's latest full-head Review
    Request IS the current gate. The current decision anchor is then the ``review_request`` itself, not
    the IR marker. Returns ``None`` (the current gate — emit) ONLY for the issue's current review
    generation request (:func:`current_review_request_journal`). Every other gate stays fail-closed with
    a diagnostic that DISTINGUISHES a historical fence (:data:`LANE_PREVIOUS_GENERATION` — a
    non-review_request gate, or an older superseded request) from a "looks current but unconfirmable
    head" refusal (:data:`LANE_CURRENT_HEAD_UNCONFIRMED` — the latest request but its head is missing /
    malformed / ambiguous). Never guesses a prose SHA.
    """
    if gate != GATE_REVIEW_REQUEST:
        return LANE_PREVIOUS_GENERATION  # only a review_request is pinned by the review generation head
    gate_journal = _as_int(getattr(marker, "journal", ""))
    latest_rr = latest_review_request_journal(marker_list, issue)
    if latest_rr is None or gate_journal is None or gate_journal < latest_rr:
        return LANE_PREVIOUS_GENERATION  # superseded by a newer review_request -> historical fence
    # This IS the issue's latest review_request. It is the current gate only when its full head is the
    # confirmed current review generation head; a missing / malformed / ambiguous head looks current but
    # cannot be pinned (a distinct diagnostic from a historical fence).
    if not current_review_request_journal(marker_list, issue):
        return LANE_CURRENT_HEAD_UNCONFIRMED
    return None  # the current full-head review_request -> the current gate


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
    marker_list: "list[JournalMarker]",
    issue: str,
    dispatch_anchor_journal: Optional[str],
    latest_review: Optional[int],
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
    # request whose journal is newer than the last review are unshadowed and still send. The SAME check
    # runs action-time at the send edge (:func:`gate_is_shadowed` via ``review_round_send_fence``), so a
    # row that went pending BEFORE the review still terminally zero-sends once the review lands (j#82382 F1).
    if gate_is_shadowed(journal, latest_review):
        return LaneGatewaySendPlan(
            emit=False, reason=LANE_SHADOWED, gate_journal=journal, gate=gate
        )
    # The generation fence (design answer j#82367 + Redmine #14094): a worker gate on a journal OLDER
    # than the current owning lane+generation's dispatch anchor is a previous-generation gate on a
    # restarted lane. Redmine #14094: a RESUMED correction lane opens a fresh generation WITHOUT a new
    # implementation_request marker, so its IR dispatch anchor is UNRESOLVABLE (``""``) even though the
    # lane's latest full-head Review Request IS the current gate. So a supplied-but-unresolvable anchor
    # no longer blanket-fences: the current gate is instead selected by the latest full-head
    # review_request (:func:`_current_generation_rescue`). A RESOLVABLE anchor keeps the original fence
    # (an older gate is previous-generation); an unresolvable anchor fails closed for every gate EXCEPT
    # the confirmed current review generation request (previous / malformed-or-missing head /
    # implementation_done all stay 0-send, with a diagnostic that separates the two refusal kinds).
    if dispatch_anchor_journal is not None:
        anchor = _as_int(dispatch_anchor_journal)
        gate_journal = _as_int(journal)
        if anchor is None:
            rescue_reason = _current_generation_rescue(marker, marker_list, issue, gate)
            if rescue_reason is not None:
                return LaneGatewaySendPlan(
                    emit=False, reason=rescue_reason, gate_journal=journal, gate=gate
                )
        elif gate_journal is None or gate_journal < anchor:
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
    latest_review = latest_review_journal(marker_list, issue_s)
    plans: list[LaneGatewaySendPlan] = []
    for marker in marker_list:
        if str(getattr(marker, "gate", "") or "").strip() not in LANE_GATEWAY_GATES:
            continue
        plans.append(
            _plan_one(
                marker, owner,
                marker_list=marker_list, issue=issue_s,
                dispatch_anchor_journal=dispatch_anchor_journal,
                latest_review=latest_review,
            )
        )
    return plans


def make_lane_gateway_send_edge_fence(anchor: object, current_request_journal: object = ""):
    """Build a per-row send-edge fence for PRE-EXISTING ``lane_gateway`` backlog rows (j#82367; #14094).

    Returns ``send_fence_fn(row) -> (fence, reason)``. A row is fenced when it is a ``lane_gateway:``
    route AND its journal is older than ``anchor`` (a previous-generation gate), or ``anchor`` is
    unresolvable (``None`` / blank / non-numeric — fail-closed). Non-``lane_gateway`` rows are exempt
    (they carry their own route's fence). This stops a pre-existing pending / recovered-then-reclaimed
    worker-gate row from waking a new generation's gateway — the ingest-side plan fence only stops newly
    discovered markers.

    Redmine #14094: a RESUMED correction lane's fresh generation carries no new implementation_request
    marker, so ``anchor`` is unresolvable even though the lane's latest full-head Review Request IS the
    current gate. When the caller supplies ``current_request_journal`` (the durable journal id of that
    current full-head ``review_request`` — :func:`current_review_request_journal`), a row whose journal
    equals it is the current gate and is NEVER fenced by the unresolvable anchor — mirroring the
    discovery-side rescue so a resumed-lane current row is not terminally dropped at the send edge. The
    exemption applies ONLY under an unresolvable anchor (the resumed-lane case); a RESOLVABLE anchor
    keeps the strict older-than-anchor fence. ``current_request_journal`` is a journal id, never a SHA /
    locator, so the fence stays secret-safe. Pure and duck-typed on ``row.callback_route`` /
    ``row.journal``.
    """
    anchor_int = _as_int(anchor)
    current_request = str(current_request_journal or "").strip()

    def _fence(row) -> tuple[bool, str]:
        if not is_lane_gateway_route(str(getattr(row, "callback_route", "") or "")):
            return (False, "")  # another route: own fence, exempt
        row_journal = str(getattr(row, "journal", "") or "").strip()
        journal = _as_int(row_journal)
        if anchor_int is not None and journal is not None and journal >= anchor_int:
            return (False, "")
        # #14094: under an unresolvable anchor, the current full-head review_request row is the current
        # decision anchor for a RESUMED lane — never fenced by the missing IR anchor.
        if anchor_int is None and current_request and row_journal == current_request:
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
    "LANE_CURRENT_HEAD_UNCONFIRMED",
    "LANE_SHADOWED",
    "LANE_REFUSAL_REASONS",
    "lane_gateway_route",
    "is_lane_gateway_route",
    "latest_review_journal",
    "latest_review_request_journal",
    "current_review_request_journal",
    "gate_is_shadowed",
    "LaneGatewaySendPlan",
    "plan_lane_gateway_sends",
    "make_lane_gateway_send_edge_fence",
)
