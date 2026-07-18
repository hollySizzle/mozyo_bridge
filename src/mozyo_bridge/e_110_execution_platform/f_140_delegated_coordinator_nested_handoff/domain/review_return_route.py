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
#: The review round this result answers belongs to a PREVIOUS lane generation (Redmine #13974): its
#: correlated ``review_request`` journal predates the current owning lane's dispatch anchor (the
#: ``implementation_request`` journal that opened the live generation). The latest-review fence
#: (:data:`RETURN_NOT_LATEST`) only compares review markers against each other, so an old-generation
#: result that is still the newest review MARKER on the issue (a new generation has restarted the lane
#: but not yet produced its own review) slips through — this reason fails it closed so a stale result
#: is never retargeted onto the new generation's gateway. Only meaningful when a dispatch anchor is
#: supplied (the fenced production supervisor); an unresolvable anchor under fenced mode also lands
#: here (fail-closed — a generation we cannot pin never authorizes a return).
RETURN_PREVIOUS_GENERATION = "previous_generation_review"
#: The review-gate contract head could not be confirmed (Redmine #13974 / j#81454 A): the review_result
#: marker or its correlated review_request marker carries no ``target_head`` (a legacy head-less marker,
#: or a malformed one). Without a confirmable head the callback cannot conjoin the target-head dimension,
#: so it fails closed rather than returning a head-unverified review.
RETURN_MISSING_REVIEW_HEAD = "missing_review_head"
#: The review_result reviewed a DIFFERENT head than the review_request it answers pinned (Redmine
#: #13974): ``result.target_head != request.target_head``. The pushed head drifted under the round, so
#: the review outcome is against a stale head and is never returned.
RETURN_REVIEW_HEAD_DRIFT = "review_head_drift"
#: The review_result marker did not DECLARE the review_request it answers, or its declared ``req``
#: does not exact-match the provider-correlated review_request (Redmine #13974 / j#81487 F1). The v2
#: contract makes the marker's own ``review_request_journal`` the action identity; a missing or drifted
#: declared req is fail-closed — the round correlation is never silently re-derived as a substitute.
RETURN_REVIEW_REQUEST_UNCONFIRMED = "review_request_unconfirmed"
#: A review-gate ``target_head`` is not a well-formed full commit head (Redmine #13974 / j#81487 F1):
#: not exactly 40 (SHA-1) or 64 (SHA-256) lowercase hex chars. A truncated / abbreviated / non-hex head
#: cannot be exact-matched against the durable review generation, so it is fail-closed.
RETURN_MALFORMED_REVIEW_HEAD = "malformed_review_head"

#: The refusal reasons — every one is a fail-closed no-return (never a guessed / self / stale /
#: uncorrelated / previous-generation / head-unconfirmed send).
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
        RETURN_PREVIOUS_GENERATION,
        RETURN_MISSING_REVIEW_HEAD,
        RETURN_REVIEW_HEAD_DRIFT,
        RETURN_REVIEW_REQUEST_UNCONFIRMED,
        RETURN_MALFORMED_REVIEW_HEAD,
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
    #: The exact commit head the review reviewed (Redmine #13974 / j#81454 A): read from the review
    #: markers' ``target_head`` (never prose). Recorded on the outbox row so the send authority conjoins
    #: it with the current review generation head at action time — a head drift / mismatch zero-sends.
    target_head: str = ""

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
            "target_head": self.target_head,
        }


#: The outbox-row payload key carrying the correlated review_request journal (action identity). Kept
#: in the row ``payload`` (not a new column) so the existing outbox idempotency key is untouched
#: (#13684 review R1-F2 / j#77892 correction 4: action/generation are payload authority, no new ledger).
_PAYLOAD_REVIEW_REQUEST_JOURNAL = "review_request_journal"
#: The outbox-row payload key carrying the reviewed head (Redmine #13974). Kept in the row ``payload``
#: (not a new column) so the existing outbox idempotency key is untouched.
_PAYLOAD_TARGET_HEAD = "target_head"


def encode_review_return_payload(review_request_journal: str, target_head: str = "") -> str:
    """Encode the review-return correlation into a compact JSON outbox payload (pure).

    Carries the correlated ``review_request_journal`` (the action identity the return is bound to) and
    the reviewed ``target_head`` (Redmine #13974) so the send authority can re-verify the review round
    AND the head at action time. Returns ``""`` for a blank request journal (nothing to carry); the
    ``target_head`` is included when present.
    """
    j = str(review_request_journal or "").strip()
    if not j:
        return ""
    obj: dict[str, str] = {_PAYLOAD_REVIEW_REQUEST_JOURNAL: j}
    head = str(target_head or "").strip()
    if head:
        obj[_PAYLOAD_TARGET_HEAD] = head
    return json.dumps(obj, sort_keys=True)


def _decode_payload_obj(payload: str) -> dict:
    raw = str(payload or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def decode_review_return_payload(payload: str) -> str:
    """Read the correlated ``review_request_journal`` from an outbox-row payload, or ``""`` (pure).

    Fail-safe: a blank / non-JSON / unexpected-shape payload yields ``""`` (the send authority then
    treats the round correlation as unrecorded and re-derives it from the live markers).
    """
    return str(_decode_payload_obj(payload).get(_PAYLOAD_REVIEW_REQUEST_JOURNAL, "") or "").strip()


def decode_review_return_target_head(payload: str) -> str:
    """Read the recorded reviewed ``target_head`` from an outbox-row payload, or ``""`` (pure; #13974).

    Fail-safe: a blank / non-JSON / head-less payload (a legacy row written before the #13974 contract)
    yields ``""`` — which the send-edge head fence treats as a fail-closed missing head (terminal).
    """
    return str(_decode_payload_obj(payload).get(_PAYLOAD_TARGET_HEAD, "") or "").strip()


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


def review_round_within_generation(request_journal: str, dispatch_anchor_journal: str) -> bool:
    """Whether a review round belongs to the current lane generation (pure; Redmine #13974).

    ``dispatch_anchor_journal`` is the current owning lane+generation's dispatch anchor — the OWNING
    journal of its ``implementation_request`` marker. A review round's identity is the
    ``review_request`` journal the result answers. Redmine journal ids are monotonic, and a
    generation opens at its ``implementation_request``, so the round is part of the current generation
    iff its request journal is at or after the anchor. A round whose request predates the anchor is a
    previous generation's round (the lane restarted and this is an OLD review). Fail-closed: an
    unresolvable / blank / non-numeric anchor, or a blank / non-numeric request journal, is NOT within
    the current generation (a generation we cannot pin never authorizes a return).
    """
    anchor_int = _as_int(dispatch_anchor_journal)
    request_int = _as_int(request_journal)
    if anchor_int is None or request_int is None:
        return False
    return request_int >= anchor_int


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


#: The exact lengths of a full git commit head: SHA-1 (40) or SHA-256 (64) hex characters.
_FULL_COMMIT_HEAD_LENGTHS = frozenset({40, 64})


def is_full_commit_head(head: object) -> bool:
    """Whether ``head`` is a well-formed FULL commit head (pure; Redmine #13974 / j#81487 F1).

    A full head is exactly 40 (SHA-1) or 64 (SHA-256) lowercase hex characters. A truncated /
    abbreviated / upper-case / non-hex / blank head is NOT full — it cannot be exact-matched against
    the durable review generation, so the callback fence treats it as malformed and fails closed.
    """
    token = str(head or "").strip()
    if len(token) not in _FULL_COMMIT_HEAD_LENGTHS:
        return False
    return all(c in "0123456789abcdef" for c in token)


def review_result_marker_request(
    markers: Iterable[JournalMarker], issue: str, review_journal: str
) -> str:
    """The ``review_request_journal`` DECLARED on the review_result marker at ``review_journal`` (pure).

    Redmine #13974 / j#81487 F1: the v2 marker's own ``req`` is the action identity, read from the
    structured marker (never re-derived). Blank when the marker is legacy / declares no request.
    """
    issue_s = str(issue).strip()
    journal_s = str(review_journal).strip()
    for mk in markers:
        if (
            str(mk.issue).strip() == issue_s
            and str(mk.journal).strip() == journal_s
            and str(mk.gate).strip() == GATE_REVIEW
        ):
            return str(getattr(mk, "review_request_journal", "") or "").strip()
    return ""


def review_result_head(markers: Iterable[JournalMarker], issue: str, review_journal: str) -> str:
    """The reviewed ``target_head`` on the review_result marker at ``review_journal``, or ``""`` (pure).

    Redmine #13974 (j#81454 A): the callback conjoins the exact commit head the review reviewed, read
    ONLY from the structured marker's ``target_head`` (never parsed from prose). Blank when the marker
    is legacy / head-less.
    """
    issue_s = str(issue).strip()
    journal_s = str(review_journal).strip()
    for mk in markers:
        if (
            str(mk.issue).strip() == issue_s
            and str(mk.journal).strip() == journal_s
            and str(mk.gate).strip() == GATE_REVIEW
        ):
            return str(getattr(mk, "target_head", "") or "").strip()
    return ""


def review_request_head(markers: Iterable[JournalMarker], issue: str, request_journal: str) -> str:
    """The requested ``target_head`` on the review_request marker at ``request_journal``, or ``""`` (pure).

    The head the review round pinned (#13974). Blank when the request marker is legacy / head-less.
    """
    issue_s = str(issue).strip()
    journal_s = str(request_journal).strip()
    for mk in markers:
        if (
            str(mk.issue).strip() == issue_s
            and str(mk.journal).strip() == journal_s
            and str(mk.gate).strip() == GATE_REVIEW_REQUEST
        ):
            return str(getattr(mk, "target_head", "") or "").strip()
    return ""


def current_review_generation_head(markers: Iterable[JournalMarker], issue: str) -> str:
    """The ``target_head`` of the LATEST review_request marker on ``issue``, or ``""`` (pure; #13974).

    The head of the issue's current review generation — the action-time authority a reserved
    review_return row's recorded head must still match. The latest review_request is the one with the
    greatest numeric journal id (Redmine ids are monotonic). Blank when no review_request marker
    carries a head (legacy) — a generation head we cannot confirm fails the callback closed.
    """
    issue_s = str(issue).strip()
    best_id: Optional[int] = None
    best_head = ""
    for mk in markers:
        if str(mk.issue).strip() != issue_s or str(mk.gate).strip() != GATE_REVIEW_REQUEST:
            continue
        rid = _as_int(mk.journal)
        if rid is None:
            continue
        if best_id is None or rid > best_id:
            best_id = rid
            best_head = str(getattr(mk, "target_head", "") or "").strip()
    return best_head


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
    *,
    dispatch_anchor_journal: Optional[str] = None,
) -> ReviewReturnPlan:
    """Plan the return of the review_result on ``(issue, review_journal)`` to its owning gateway (pure).

    Ordered, fail-closed checks (design answer j#77892 corrections 2 + 3 + 4; #13974 generation fence):

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
    2c. when a ``dispatch_anchor_journal`` is supplied (the fenced production supervisor), the review
       round it answers must belong to the CURRENT lane generation — its correlated ``review_request``
       journal must be at or after the anchor (:data:`RETURN_PREVIOUS_GENERATION` otherwise; #13974).
       The latest-review fence (step 2) only compares review markers against each other, so an
       old-generation result that is still the newest review MARKER on the issue (the lane restarted
       into a new generation but has not produced its own review yet) would otherwise be retargeted
       onto the new generation's gateway. ``dispatch_anchor_journal=None`` skips this check (unfenced
       callers — behavior unchanged); a supplied-but-unresolvable anchor is fail-closed here.
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

    # 2c. generation fence (#13974): when the caller supplies the current dispatch anchor, the review
    # round must belong to the CURRENT lane generation. An old-generation result that is still the
    # newest review marker on the issue (the lane restarted but has not produced its own review yet)
    # would otherwise be retargeted onto the new generation's gateway. Fail-closed on an unresolvable
    # anchor (a generation we cannot pin never authorizes a return). ``None`` skips the fence.
    if dispatch_anchor_journal is not None and not review_round_within_generation(
        request_journal, dispatch_anchor_journal
    ):
        return _refuse(RETURN_PREVIOUS_GENERATION, review_journal_s)

    # 2d. review-gate v2 fence (#13974 / j#81454 A + j#81487 F1): in a fenced pass, conjoin the exact
    # review_request identity AND the exact commit head, all from the structured marker (never prose):
    #  (a) the review_result marker must DECLARE the review_request it answers (``req``) and that
    #      declared request must exact-match the provider-correlated request — a missing / drifted
    #      declared req is fail-closed (never re-derived as a wildcard substitute);
    #  (b) the review_result and its review_request must BOTH carry a ``target_head``;
    #  (c) both heads must be well-formed FULL commit heads (40/64 hex — a truncated / malformed head
    #      cannot be exact-matched against the durable review generation);
    #  (d) and the two heads must be EQUAL (the result reviewed the head the round pinned).
    # Only enforced when fenced; the recorded head is carried on the plan so the send authority
    # re-verifies it against the current review generation head at action time.
    result_head = ""
    if dispatch_anchor_journal is not None:
        declared_req = review_result_marker_request(markers, issue, review_journal_s)
        if not declared_req or declared_req != request_journal:
            return _refuse(RETURN_REVIEW_REQUEST_UNCONFIRMED, review_journal_s)
        result_head = review_result_head(markers, issue, review_journal_s)
        req_head = review_request_head(markers, issue, request_journal)
        if not result_head or not req_head:
            return _refuse(RETURN_MISSING_REVIEW_HEAD, review_journal_s)
        if not is_full_commit_head(result_head) or not is_full_commit_head(req_head):
            return _refuse(RETURN_MALFORMED_REVIEW_HEAD, review_journal_s)
        if result_head != req_head:
            return _refuse(RETURN_REVIEW_HEAD_DRIFT, review_journal_s)

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
        target_head=result_head,
    )


def plan_review_returns(
    markers: Iterable[JournalMarker],
    issue: str,
    owner: OwningLaneBinding,
    *,
    dispatch_anchor_journal: Optional[str] = None,
) -> tuple[ReviewReturnPlan, ...]:
    """Plan every returnable review_result on ``issue`` (pure).

    In practice at most one plan emits (the latest-review fence collapses the issue's review_result
    markers to the single newest, unshadowed one), but the function is total over the issue's markers
    so the caller enumerates without pre-filtering. Only :data:`RETURN_OK` plans carry a route; the
    refusals are returned too so the caller can record why nothing was returned (observability).

    ``dispatch_anchor_journal`` (Redmine #13974) is threaded to each :func:`plan_review_return` so the
    generation fence refuses a review round that predates the current lane generation. ``None`` (the
    default) leaves every plan unfenced (behavior unchanged for pre-#13974 callers).
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
        plan_review_return(
            marker_list, issue, journal, owner,
            dispatch_anchor_journal=dispatch_anchor_journal,
        )
        for journal in review_journals
    )


#: The secret-safe send-edge fence reason tokens for a review_return backlog row (#13974). Literal
#: regardless of UI language (they land in the redaction-safe supervisor report).
REVIEW_RETURN_FENCE_PREVIOUS_GENERATION = "superseded: review round older than current dispatch anchor"
REVIEW_RETURN_FENCE_UNRESOLVED_ANCHOR = "fenced: dispatch anchor unresolvable"
REVIEW_RETURN_FENCE_NO_CORRELATION = "fenced: review_return row missing round correlation"
REVIEW_RETURN_FENCE_HEAD_UNCONFIRMED = "fenced: review head unconfirmed against current generation"
REVIEW_RETURN_FENCE_HEAD_DRIFT = "superseded: review head drifted from current review generation"
REVIEW_RETURN_FENCE_MALFORMED_HEAD = "fenced: review head not a full commit head"


def make_review_return_send_edge_fence(
    dispatch_anchor_journal: object, current_review_head: object = None
):
    """Build the terminal send-edge fence for pre-existing ``review_return`` backlog rows (#13974).

    Returns ``send_fence_fn(row) -> (fence, reason)``. The #13974 discovery-side fences stop NEW stale
    candidates from enqueuing, but a row already pending from an earlier pass (when it WAS current, or
    before the fence existed) must also reach a terminal disposition rather than retry forever. This
    fence, composed into the supervisor's ``send_fence_fn``, makes the outbox deliver pass mark such a
    row terminally uncertain (zero-send, no retry, never dead-lettered — a stale backlog neither
    replays nor amplifies).

    A ``review_return:<lane>`` row is fenced when EITHER:

    - its correlated ``review_request`` journal (decoded from the payload) is NOT within the current
      lane generation (:func:`review_round_within_generation` — older than the anchor, an unresolvable
      anchor, or a missing round correlation); OR
    - (j#81454 A) its recorded reviewed ``target_head`` does not conjoin with the CURRENT review
      generation head (``current_review_head``): a missing recorded head, an unresolvable current head,
      or a head drift. The head is the review-generation dimension the callback must conjoin with the
      lifecycle generation — a review returned to the current lane must be the review of the current
      head, never a stale-head outcome.

    A current-generation, current-head row is NOT fenced: its exactly-once delivery is handled by the
    #13684 send authorities (owning-lane live generation + review-round re-verification). Non-
    ``review_return`` routes are exempt (the coordinator route has its own
    :func:`...workspace_supervisor.make_send_edge_fence`). Pure; duck-typed on ``row.callback_route`` /
    ``row.payload``.
    """
    anchor_int = _as_int(dispatch_anchor_journal)
    cur_head = str(current_review_head or "").strip()

    def _fence(row) -> "tuple[bool, str]":
        route = str(getattr(row, "callback_route", "") or "").strip()
        if not is_review_return_route(route):
            return (False, "")  # coordinator / other: own fence, exempt here
        payload = str(getattr(row, "payload", "") or "")
        request_journal = decode_review_return_payload(payload)
        if not review_round_within_generation(request_journal, str(dispatch_anchor_journal or "")):
            if anchor_int is None:
                return (True, REVIEW_RETURN_FENCE_UNRESOLVED_ANCHOR)
            if _as_int(request_journal) is None:
                return (True, REVIEW_RETURN_FENCE_NO_CORRELATION)
            return (True, REVIEW_RETURN_FENCE_PREVIOUS_GENERATION)
        # Current lane generation: conjoin the review-generation head (#13974 / j#81454 A + j#81487 F1).
        recorded_head = decode_review_return_target_head(payload)
        if not cur_head or not recorded_head:
            return (True, REVIEW_RETURN_FENCE_HEAD_UNCONFIRMED)
        if not is_full_commit_head(recorded_head) or not is_full_commit_head(cur_head):
            return (True, REVIEW_RETURN_FENCE_MALFORMED_HEAD)  # malformed head -> action-time terminal
        if recorded_head != cur_head:
            return (True, REVIEW_RETURN_FENCE_HEAD_DRIFT)
        return (False, "")  # current generation + current head -> #13684 authorities own exactly-once

    return _fence


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
    "RETURN_PREVIOUS_GENERATION",
    "RETURN_MISSING_REVIEW_HEAD",
    "RETURN_REVIEW_HEAD_DRIFT",
    "RETURN_REVIEW_REQUEST_UNCONFIRMED",
    "RETURN_MALFORMED_REVIEW_HEAD",
    "RETURN_REFUSAL_REASONS",
    "REVIEW_RETURN_FENCE_PREVIOUS_GENERATION",
    "REVIEW_RETURN_FENCE_UNRESOLVED_ANCHOR",
    "REVIEW_RETURN_FENCE_NO_CORRELATION",
    "REVIEW_RETURN_FENCE_HEAD_UNCONFIRMED",
    "REVIEW_RETURN_FENCE_HEAD_DRIFT",
    "REVIEW_RETURN_FENCE_MALFORMED_HEAD",
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
    "review_result_head",
    "review_request_head",
    "review_result_marker_request",
    "is_full_commit_head",
    "current_review_generation_head",
    "review_return_is_current",
    "review_round_within_generation",
    "make_review_return_send_edge_fence",
    "encode_review_return_payload",
    "decode_review_return_payload",
    "decode_review_return_target_head",
    "plan_review_return",
    "plan_review_returns",
)
