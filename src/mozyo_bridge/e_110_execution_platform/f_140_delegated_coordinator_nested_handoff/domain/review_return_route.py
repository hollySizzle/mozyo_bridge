"""Correlated review_result return routing (Redmine #13684).

Phase A (#13683) built the whole background_service callback delivery machinery — supervisor lease
+ outbox claim, route re-resolution against the live inventory, and a **mandatory
generation-correlated authority** (:func:`...domain.background_service_delivery.authorize_background_delivery`
requires a non-blank expected generation AND a non-blank live generation AND their exact match).
It deliberately left DELIVERY fail-closed-disabled: Phase A had no live generation authority, so a
blank live generation failed every send closed (#13683 R6-F1 / R7).

This module is the pure heart of #13684's correlated review_result return: the durable correlation
that ties a coordinator-recorded ``review_result`` back to the exact target-lane Codex gateway that
**owns the issue**, so the review outcome returns generation-correlated rather than depending on a
manual gateway relay or self-route (the issue's verified gap). It authorizes nothing and reads no
I/O — the application layer reads the owning-lane binding + the Redmine journal markers and consults
these pure evaluators.

Boundaries the design answer (j#77892 ``accepted_with_corrections``) pins, enforced here as pure
policy:

- **return route identity (correction 4).** The callback route for a returned review_result encodes
  the OWNING LANE (``review_return:<lane_id>``), so it is a distinct outbox idempotency key from the
  coordinator callback and from a *different* owning lane. A recovery-lane switch (supersession, #13681)
  therefore reserves a NEW row for the new owner while the old-owner row fails closed — the existing
  ``(workspace, source, issue, journal, normalized_gate, callback_route)`` idempotency is preserved,
  never a second delivery ledger.
- **owning-lane binding is the target authority (correction 2).** The return target lane / receiver /
  generation come from the durable #13681/#13689 owning-lane binding (``resolve_owner`` + the lane's
  revision), never a pane locator, an issue-id scan, or a "current-looking" pane. An absent /
  ambiguous / coordinator-self owner yields no return candidate (fail-closed; no self-route, no
  cross-lane Claude direct send — the receiver is the lane's Codex gateway).
- **latest-review fence (correction 3).** A review_result is returnable only when it is the LATEST
  review outcome on the issue AND no newer ``review_request`` restarted the round: the re-fetched
  Redmine structured gate markers are the authority, never a notification's claimed kind. A stale
  review_result (a newer finding / correction / review request exists) is never returned.
- **generation is EXPECTED-only here; the live generation is read independently at delivery
  (correction 1).** This module stamps the row's *expected* generation from the owning-lane revision
  observed at ingest. The delivery authority re-reads the *live* owning-lane generation and refuses
  unless both are non-blank and match — this module never copies one side onto the other and never
  authorizes a send.

The module is pure: total functions over the plain owning-lane facts + already-read
:class:`...domain.redmine_event_intake.JournalMarker` values. Reading the owning-lane store and the
Redmine journal, and firing the delivery, are the application layer's job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    JournalMarker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    GATE_IMPLEMENTATION_DONE,
    GATE_REVIEW,
    GATE_REVIEW_REQUEST,
)

#: The callback-route prefix for a returned review_result. It is part of the outbox idempotency key
#: (``callback_route``), so a return to lane A and a return to lane B (a recovery-lane switch) are
#: distinct rows — the supersession follow reserves a fresh row for the new owner while the old row
#: fails closed, without a second delivery ledger (correction 4). Deliberately distinct from
#: :data:`...callback_runtime.DEFAULT_CALLBACK_ROUTE` (``coordinator``) so the coordinator callback
#: and the return callback never collide on one key.
REVIEW_RETURN_ROUTE_PREFIX = "review_return"

#: The coordinator's own durable lane in the route model. A review_result whose issue is owned by the
#: coordinator lane itself has no sublane gateway to return to — returning it would be a self-route,
#: so it is refused (:data:`RETURN_SELF_ROUTE`).
COORDINATOR_LANE = "default"

# ---------------------------------------------------------------------------
# Return-plan reason vocabulary (machine-readable; literal regardless of UI language).
# ---------------------------------------------------------------------------
RETURN_OK = "return_ok"
RETURN_NO_OWNER = "no_active_owner"  # resolve_owner found no active owning lane
RETURN_AMBIGUOUS_OWNER = "ambiguous_owner"  # more than one active owner (fail-closed, never guess)
RETURN_SELF_ROUTE = "self_route"  # the owning lane is the coordinator lane itself
RETURN_NO_GATEWAY = "no_gateway_receiver"  # the lane gateway provider did not resolve
RETURN_NOT_LATEST = "not_latest_review"  # a newer review_result / review_request superseded this one
RETURN_NOT_REVIEW_RESULT = "not_review_result"  # the anchored journal carries no review outcome
RETURN_BLANK_GENERATION = "blank_owning_generation"  # the owning lane carries no generation stamp
#: The review_result is not correlated to any preceding review_request (no review round it answers) —
#: an uncorrelated review outcome is never returned (#13684 review R1-F2 / j#77892 correction 3: the
#: result must bind to its review_request / action identity).
RETURN_NO_REVIEW_REQUEST = "no_correlated_review_request"

#: The refusal reasons — every one is a fail-closed no-return (never a guessed / self / stale /
#: uncorrelated send).
RETURN_REFUSAL_REASONS = frozenset(
    {
        RETURN_NO_OWNER,
        RETURN_AMBIGUOUS_OWNER,
        RETURN_SELF_ROUTE,
        RETURN_NO_GATEWAY,
        RETURN_NOT_LATEST,
        RETURN_NOT_REVIEW_RESULT,
        RETURN_BLANK_GENERATION,
        RETURN_NO_REVIEW_REQUEST,
    }
)

# ---------------------------------------------------------------------------
# Owner resolution status tokens (mirror ...core.state.lane_lifecycle_model, kept local so this
# domain stays inside its bounded context and does not import the core store).
# ---------------------------------------------------------------------------
OWNER_RESOLVED = "resolved"
OWNER_ABSENT = "absent"
OWNER_AMBIGUOUS = "ambiguous"
OWNER_UNKNOWN = "unknown"


def review_return_callback_route(lane_id: str) -> str:
    """The ``review_return:<lane_id>`` callback route for a returned review_result (pure).

    Encodes the owning lane so the outbox idempotency key partitions returns by owner — a
    supersession to a new lane is a new key (new row) while the old-owner row fails closed. A blank
    lane id cannot address a return and raises (a fail-closed programming error, never a silent
    ``review_return:`` route that would collide across owners).
    """
    lane = str(lane_id or "").strip()
    if not lane:
        raise ValueError("review_return route requires a non-empty owning lane id")
    return f"{REVIEW_RETURN_ROUTE_PREFIX}:{lane}"


def is_review_return_route(route: str) -> bool:
    """Whether ``route`` is a review_result return route (``review_return:<lane>``) (pure)."""
    return str(route or "").strip().startswith(f"{REVIEW_RETURN_ROUTE_PREFIX}:")


@dataclass(frozen=True)
class OwningLaneBinding:
    """The durable owning-lane facts a return plan needs (from #13681/#13689, read by the caller).

    ``status`` is the :func:`...core.state.lane_lifecycle.LaneLifecycleStore.resolve_owner` outcome
    (:data:`OWNER_RESOLVED` / :data:`OWNER_ABSENT` / :data:`OWNER_AMBIGUOUS` / :data:`OWNER_UNKNOWN`);
    ``lane_id`` is the single active owning lane (empty unless resolved); ``generation`` is that lane's
    durable revision stamp (a monotonic CAS generation that bumps on any lifecycle transition incl.
    supersession — the *expected* generation the row records at ingest); ``gateway_receiver`` is the
    binding-resolved Codex gateway provider for that lane (the send target — never a cross-lane Claude
    worker).
    """

    status: str
    lane_id: str = ""
    generation: str = ""
    gateway_receiver: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == OWNER_RESOLVED and bool(str(self.lane_id or "").strip())


@dataclass(frozen=True)
class ReviewReturnPlan:
    """The pure plan for returning one review_result to its owning-lane gateway.

    ``emit`` is True only when every fail-closed check passes; ``reason`` is a member of
    :data:`RETURN_REFUSAL_REASONS` on refusal (or :data:`RETURN_OK` on emit). When ``emit`` is True the
    ``callback_route`` / ``target_lane`` / ``target_receiver`` / ``target_generation`` are the durable
    correlation the application stamps on the outbox row so the background_service delivery authority
    binds the re-resolved live target + live generation to it.
    """

    emit: bool
    reason: str
    callback_route: str = ""
    target_lane: str = ""
    target_receiver: str = ""
    target_generation: str = ""
    review_journal: str = ""
    #: The correlated review_request journal this result answers (#13684 review R1-F2): the action
    #: identity the return is bound to, so the send authority can re-verify the round at action time
    #: and refuse a result whose round was superseded by a newer request.
    review_request_journal: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "emit": self.emit,
            "reason": self.reason,
            "callback_route": self.callback_route,
            "target_lane": self.target_lane,
            "target_receiver": self.target_receiver,
            "target_generation": self.target_generation,
            "review_journal": self.review_journal,
            "review_request_journal": self.review_request_journal,
        }


#: The outbox-row payload key carrying the correlated review_request journal (action identity). Kept
#: in the row ``payload`` (not a new column) so the existing outbox idempotency key is untouched
#: (#13684 review R1-F2 / j#77892 correction 4: action/generation are payload authority, no new ledger).
_PAYLOAD_REVIEW_REQUEST_JOURNAL = "review_request_journal"


def encode_review_return_payload(review_request_journal: str) -> str:
    """Encode the review-return correlation into a compact JSON outbox payload (pure).

    Carries the correlated ``review_request_journal`` (the action identity the return is bound to) so
    the send authority can re-verify the review round at action time. Returns ``""`` for a blank
    request journal (nothing to carry).
    """
    j = str(review_request_journal or "").strip()
    return json.dumps({_PAYLOAD_REVIEW_REQUEST_JOURNAL: j}, sort_keys=True) if j else ""


def decode_review_return_payload(payload: str) -> str:
    """Read the correlated ``review_request_journal`` from an outbox-row payload, or ``""`` (pure).

    Fail-safe: a blank / non-JSON / unexpected-shape payload yields ``""`` (the send authority then
    treats the round correlation as unrecorded and re-derives it from the live markers).
    """
    raw = str(payload or "").strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    return str(obj.get(_PAYLOAD_REVIEW_REQUEST_JOURNAL, "") or "").strip()


def _as_int(journal: object) -> Optional[int]:
    """Parse a Redmine journal id (positive ASCII decimal) to an int, or ``None`` (pure).

    Journal ordering is by the record's own monotonic id, so a numeric compare is chronological. A
    non-numeric / blank id yields ``None`` and is excluded from the latest computation (never guessed
    as newest / oldest).
    """
    token = str(journal or "").strip()
    if not token or not token.isascii() or not token.isdigit():
        return None
    try:
        return int(token)
    except ValueError:
        return None


def _refuse(reason: str, review_journal: str = "") -> ReviewReturnPlan:
    return ReviewReturnPlan(emit=False, reason=reason, review_journal=review_journal)


def latest_review_result_journal(markers: Iterable[JournalMarker], issue: str) -> str:
    """The journal id of the LATEST review_result (``review`` gate) on ``issue``, or ``""`` (pure).

    A review_result marker normalizes to the runtime ``review`` gate
    (:data:`...redmine_event_intake.MARKER_GATE_ALIASES`); the latest is the one with the greatest
    numeric journal id (Redmine ids are monotonic). Returns ``""`` when the issue carries no
    review_result marker.
    """
    issue_s = str(issue).strip()
    best_id: Optional[int] = None
    best_journal = ""
    for mk in markers:
        if str(mk.issue).strip() != issue_s or str(mk.gate).strip() != GATE_REVIEW:
            continue
        jid = _as_int(mk.journal)
        if jid is None:
            continue
        if best_id is None or jid > best_id:
            best_id = jid
            best_journal = str(mk.journal).strip()
    return best_journal


def _has_newer_marker(
    markers: Iterable[JournalMarker], issue: str, gate: str, journal_int: int
) -> bool:
    """Whether a marker of ``gate`` on ``issue`` is newer than ``journal_int`` (pure)."""
    issue_s = str(issue).strip()
    gate_s = str(gate).strip()
    for mk in markers:
        if str(mk.issue).strip() != issue_s or str(mk.gate).strip() != gate_s:
            continue
        rid = _as_int(mk.journal)
        if rid is not None and rid > journal_int:
            return True
    return False


def _has_newer_review_request(markers: Iterable[JournalMarker], issue: str, journal_int: int) -> bool:
    """Whether a ``review_request`` marker on ``issue`` is newer than ``journal_int`` (pure).

    A newer review request means the review round restarted, so an older review_result is stale and
    must not be returned (correction 3: a newer review_request supersedes an old result).
    """
    return _has_newer_marker(markers, issue, GATE_REVIEW_REQUEST, journal_int)


def _has_newer_correction(markers: Iterable[JournalMarker], issue: str, journal_int: int) -> bool:
    """Whether an ``implementation_done`` (correction) marker on ``issue`` is newer than the result (pure).

    #13684 review R1-re-review F1: a worker responds to a ``review_result`` (changes_requested) by
    recording a correction (``implementation_done``) and then a fresh ``review_request``. A correction
    newer than the result means the finding is already being addressed, so returning the old result
    would deliver a stale "please correct" — correction 3's "newer finding / correction" arm. The
    correction is the ``implementation_done`` gate (mapped from that marker kind), read from the
    re-fetched structured markers, never a notification kind.
    """
    return _has_newer_marker(markers, issue, GATE_IMPLEMENTATION_DONE, journal_int)


def correlated_review_request_journal(
    markers: Iterable[JournalMarker], issue: str, review_journal: str
) -> str:
    """The review_request journal a review_result answers, or ``""`` (pure; #13684 review R1-F2).

    The review round a ``review_result`` (on ``review_journal``) belongs to is the LATEST
    ``review_request`` on the issue with a journal id strictly **before** the result (the request the
    result answers). Redmine ids are monotonic, so this is the greatest request id ``< review_journal``.
    Returns ``""`` when the result has no preceding review_request — an uncorrelated review outcome
    (never part of a real review round), which the plan refuses (:data:`RETURN_NO_REVIEW_REQUEST`).
    """
    issue_s = str(issue).strip()
    review_int = _as_int(review_journal)
    if review_int is None:
        return ""
    best_id: Optional[int] = None
    best_journal = ""
    for mk in markers:
        if str(mk.issue).strip() != issue_s or str(mk.gate).strip() != GATE_REVIEW_REQUEST:
            continue
        rid = _as_int(mk.journal)
        if rid is None or rid >= review_int:
            continue
        if best_id is None or rid > best_id:
            best_id = rid
            best_journal = str(mk.journal).strip()
    return best_journal


def review_return_is_current(
    markers: Iterable[JournalMarker],
    issue: str,
    review_journal: str,
    review_request_journal: str = "",
) -> bool:
    """Whether a reserved review_result return is STILL the current round at action time (pure).

    #13684 review R1-F1: the latest-review fence must be re-verified at the reserve / irreversible
    send edge, not only at discovery — a newer review_request / review_result landing after the row
    was reserved makes the reserved result stale. Re-reading the issue's structured markers, the
    return is current iff:

    1. ``review_journal`` is still the LATEST review_result on the issue (no newer review outcome);
    2. no ``review_request`` is newer than it (the round did not restart) AND no ``implementation_done``
       correction is newer than it (the finding is not already being addressed) — the "newer finding /
       correction" arm of correction 3 (R1-re-review F1);
    3. it still correlates to a preceding review_request, and the row's recorded correlation is
       **non-blank and equal** to that current one (R1-re-review F2): a review_return row without a
       durable recorded action identity fails closed here — a blank / lost / drifted correlation is
       never re-derived from the live markers as a substitute (the payload is the authority).

    Any failure is a stale / uncorrelated row -> the caller zero-sends. The re-fetched Redmine
    structured gate is the authority (never a notification kind).
    """
    review_int = _as_int(review_journal)
    if review_int is None:
        return False
    if str(review_journal).strip() != latest_review_result_journal(markers, issue):
        return False
    if _has_newer_review_request(markers, issue, review_int):
        return False
    if _has_newer_correction(markers, issue, review_int):
        return False
    current_request = correlated_review_request_journal(markers, issue, review_journal)
    if not current_request:
        return False
    # R1-re-review F2: the recorded correlation must be present AND match — a blank recorded
    # correlation is fail-closed, never a wildcard re-derived from the live markers.
    recorded = str(review_request_journal or "").strip()
    if not recorded or recorded != current_request:
        return False
    return True


def plan_review_return(
    markers: Iterable[JournalMarker],
    issue: str,
    review_journal: str,
    owner: OwningLaneBinding,
) -> ReviewReturnPlan:
    """Plan the return of the review_result on ``(issue, review_journal)`` to its owning gateway (pure).

    Ordered, fail-closed checks (design answer j#77892 corrections 2 + 3 + 4):

    1. the anchored journal must carry a review_result (``review`` gate) marker on this issue
       (:data:`RETURN_NOT_REVIEW_RESULT` otherwise — a callback is never returned against a
       non-review journal);
    2. it must be the LATEST review_result on the issue AND no ``review_request`` may be newer than it
       (:data:`RETURN_NOT_LATEST` otherwise — a stale outcome is never returned; the re-fetched
       structured markers are the authority, never a notification kind);
    2b. it must correlate to a preceding ``review_request`` — the review round it answers
       (:data:`RETURN_NO_REVIEW_REQUEST` otherwise; #13684 review R1-F2 / j#77892 correction 3: an
       uncorrelated review outcome is never returned, and the correlated request journal is carried on
       the plan so the send authority can re-verify the round at action time);
    3. the issue must have exactly one active owning lane from the durable binding
       (:data:`RETURN_NO_OWNER` / :data:`RETURN_AMBIGUOUS_OWNER` otherwise — never "the newest lane" or
       a pane guess);
    4. the owning lane must not be the coordinator lane itself (:data:`RETURN_SELF_ROUTE` otherwise —
       no self-route);
    5. the lane must resolve a Codex gateway receiver (:data:`RETURN_NO_GATEWAY` otherwise) and carry a
       non-blank owning-lane generation (:data:`RETURN_BLANK_GENERATION` otherwise — a blank expected
       generation cannot be generation-correlated, so the delivery would fail closed anyway).

    Only when every check passes is the plan :data:`RETURN_OK` carrying the durable correlation the
    application stamps on the outbox row. The live generation is re-read independently at delivery
    (correction 1); this plan supplies only the *expected* generation.
    """
    review_journal_s = str(review_journal).strip()

    # 1. the anchored journal is a review_result on this issue.
    latest = latest_review_result_journal(markers, issue)
    review_int = _as_int(review_journal_s)
    if review_int is None or not latest:
        return _refuse(RETURN_NOT_REVIEW_RESULT, review_journal_s)
    issue_s = str(issue).strip()
    on_this_journal = any(
        str(mk.issue).strip() == issue_s
        and str(mk.journal).strip() == review_journal_s
        and str(mk.gate).strip() == GATE_REVIEW
        for mk in markers
    )
    if not on_this_journal:
        return _refuse(RETURN_NOT_REVIEW_RESULT, review_journal_s)

    # 2. latest-review fence: this must be the newest review_result and unshadowed by a newer request
    # OR a newer implementation_done correction (R1-re-review F1 — the finding is already being
    # addressed, so the old result is stale).
    if review_journal_s != latest:
        return _refuse(RETURN_NOT_LATEST, review_journal_s)
    if _has_newer_review_request(markers, issue, review_int):
        return _refuse(RETURN_NOT_LATEST, review_journal_s)
    if _has_newer_correction(markers, issue, review_int):
        return _refuse(RETURN_NOT_LATEST, review_journal_s)

    # 2b. round correlation: the result must answer a preceding review_request (R1-F2). An
    # uncorrelated review outcome (no request before it) is never returned; the correlated request
    # journal is carried so the send authority can re-verify the round at action time.
    request_journal = correlated_review_request_journal(markers, issue, review_journal_s)
    if not request_journal:
        return _refuse(RETURN_NO_REVIEW_REQUEST, review_journal_s)

    # 3. owning-lane binding is the target authority (never a pane / issue-id guess).
    if owner.status == OWNER_AMBIGUOUS:
        return _refuse(RETURN_AMBIGUOUS_OWNER, review_journal_s)
    if not owner.resolved:
        return _refuse(RETURN_NO_OWNER, review_journal_s)
    lane = str(owner.lane_id).strip()

    # 4. no self-route to the coordinator lane.
    if lane == COORDINATOR_LANE:
        return _refuse(RETURN_SELF_ROUTE, review_journal_s)

    # 5. a resolvable Codex gateway receiver + a non-blank owning-lane generation.
    receiver = str(owner.gateway_receiver or "").strip()
    if not receiver:
        return _refuse(RETURN_NO_GATEWAY, review_journal_s)
    generation = str(owner.generation or "").strip()
    if not generation:
        return _refuse(RETURN_BLANK_GENERATION, review_journal_s)

    return ReviewReturnPlan(
        emit=True,
        reason=RETURN_OK,
        callback_route=review_return_callback_route(lane),
        target_lane=lane,
        target_receiver=receiver,
        target_generation=generation,
        review_journal=review_journal_s,
        review_request_journal=request_journal,
    )


def plan_review_returns(
    markers: Iterable[JournalMarker],
    issue: str,
    owner: OwningLaneBinding,
) -> tuple[ReviewReturnPlan, ...]:
    """Plan every returnable review_result on ``issue`` (pure).

    In practice at most one plan emits (the latest-review fence collapses the issue's review_result
    markers to the single newest, unshadowed one), but the function is total over the issue's markers
    so the caller enumerates without pre-filtering. Only :data:`RETURN_OK` plans carry a route; the
    refusals are returned too so the caller can record why nothing was returned (observability).
    """
    review_journals: list[str] = []
    issue_s = str(issue).strip()
    marker_list = list(markers)
    for mk in marker_list:
        if str(mk.issue).strip() != issue_s or str(mk.gate).strip() != GATE_REVIEW:
            continue
        jid = str(mk.journal).strip()
        if jid and jid not in review_journals:
            review_journals.append(jid)
    return tuple(
        plan_review_return(marker_list, issue, journal, owner) for journal in review_journals
    )


__all__ = (
    "REVIEW_RETURN_ROUTE_PREFIX",
    "COORDINATOR_LANE",
    "RETURN_OK",
    "RETURN_NO_OWNER",
    "RETURN_AMBIGUOUS_OWNER",
    "RETURN_SELF_ROUTE",
    "RETURN_NO_GATEWAY",
    "RETURN_NOT_LATEST",
    "RETURN_NOT_REVIEW_RESULT",
    "RETURN_BLANK_GENERATION",
    "RETURN_NO_REVIEW_REQUEST",
    "RETURN_REFUSAL_REASONS",
    "OWNER_RESOLVED",
    "OWNER_ABSENT",
    "OWNER_AMBIGUOUS",
    "OWNER_UNKNOWN",
    "OwningLaneBinding",
    "ReviewReturnPlan",
    "review_return_callback_route",
    "is_review_return_route",
    "latest_review_result_journal",
    "correlated_review_request_journal",
    "review_return_is_current",
    "encode_review_return_payload",
    "decode_review_return_payload",
    "plan_review_return",
    "plan_review_returns",
)
