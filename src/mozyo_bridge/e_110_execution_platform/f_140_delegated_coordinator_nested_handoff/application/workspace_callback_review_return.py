"""Callback-supervisor review-return send authorities (Redmine #13684; extracted #13844 R7).

The owning-lane resolution + send-time authorities the callback supervisor wires into its sender.
Kept in its own leaf so the supervisor composition root
(:mod:`...workspace_callback_supervisor`) stays a cohesive, under-threshold unit while the
combined #13758 (event-driven reconcile) + #13844 (read-only lifecycle routing) wiring lands on
the same feature. Nothing about the behavior changes — this is a move-only responsibility split
(the supervisor imports and re-exports these, so every caller's import surface is preserved):

- :func:`coordinator_target_tuple` — the durable expected ``(lane, receiver)`` a coordinator
  callback row records at ingest, from the provider binding (fail-closed to ``("", "")``);
- :func:`owning_lane_binding` — an issue's durable owning-lane binding + generation + gateway
  receiver (the #13681/#13689 authority, fail-closed to ``OWNER_UNKNOWN``);
- :func:`owning_lane_generation_reader` — the independent send-time live-generation authority for
  a ``review_return`` row (#13684 correction 1: read the live generation, never copy the row's);
- :func:`review_round_send_fence` — the action-time review-round fence re-verifying a correlated
  ``review_return`` row is STILL the current round against live Redmine markers (#13684 R1-F1).

The supervisor-specific machinery (workspace fan-out, lease fence, the class) stays in the
composition root; this module owns only the review-return owning-lane authorities.
"""

from __future__ import annotations

from typing import Callable, Optional

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (
    DEFAULT_CALLBACK_ROUTE,
    discover_review_returns,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    OWNER_RESOLVED,
    OWNER_UNKNOWN,
    OwningLaneBinding,
    current_review_generation_conclusion,
    current_review_generation_head,
    current_review_generation_request,
    is_review_return_route,
    make_review_return_send_edge_fence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalSource,
    markers_from_source,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    compose_send_edge_fences,
    make_send_edge_fence,
)


_COORDINATOR_LANE = "default"


def coordinator_target_tuple(binding: object, route: str) -> "tuple[str, str]":
    """The durable expected ``(lane, receiver)`` for a callback route, from the provider binding (R4-F2).

    For the coordinator route the receiver is the **binding-resolved coordinator provider**
    (``claude`` / ``codex``) and the lane is the coordinator default lane — recorded on the outbox
    row so the delivery authority binds the live target to the exact expected role (not just a lane).
    An unresolved binding (or a non-coordinator route) yields ``("", "")`` so the row's expected
    receiver is blank and the delivery fails closed (R4-F1) rather than routing to a wrong role.
    """
    if str(route or "").strip() != DEFAULT_CALLBACK_ROUTE:
        return "", ""
    provider = ""
    if binding is not None:
        try:
            provider = str(binding.provider_for("coordinator") or "").strip()
        except Exception:  # noqa: BLE001 - an unresolvable binding -> blank -> fail-closed delivery
            provider = ""
    return (_COORDINATOR_LANE, provider) if provider else ("", "")


#: The workflow role whose provider is the target-lane Codex gateway (the review_result return
#: receiver). ``project_gateway`` binds to codex in the default binding, so a return never lands on a
#: cross-lane Claude worker (design answer j#77892: coordinator -> target-lane Codex gateway only).
_GATEWAY_ROLE = "project_gateway"


def owning_lane_binding(
    workspace_id: str, issue: str, binding: object, *, lifecycle_store: object
) -> OwningLaneBinding:
    """Resolve an issue's durable owning-lane binding + generation + gateway receiver (#13684).

    The correlated-return target authority is the #13681/#13689 owning-lane binding, never a pane
    locator or an issue-id scan: :meth:`...core.state.lane_lifecycle.LaneLifecycleStore.resolve_owner`
    yields the single active owning lane (fail-closed on absent / ambiguous), and that lane's durable
    ``revision`` is the *expected* generation the outbox row records at ingest. The gateway receiver is
    the binding-resolved ``project_gateway`` provider (codex). Any store / read failure fails closed to
    :data:`OWNER_UNKNOWN` (no return), never a guess.
    """
    from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey

    wsid = str(workspace_id or "").strip()
    issue_s = str(issue or "").strip()
    try:
        owner = lifecycle_store.resolve_owner(wsid, issue_s)
    except Exception:  # noqa: BLE001 - an unreadable lifecycle store is a fail-closed unknown owner
        return OwningLaneBinding(status=OWNER_UNKNOWN)
    if not getattr(owner, "resolved", False):
        return OwningLaneBinding(status=str(getattr(owner, "status", OWNER_UNKNOWN)))
    lane = str(getattr(owner, "lane_id", "") or "").strip()
    generation = ""
    try:
        record = lifecycle_store.get(LaneLifecycleKey(wsid, lane))
        if record is not None:
            generation = str(record.revision)
    except Exception:  # noqa: BLE001 - a broken key / read is a fail-closed blank generation
        generation = ""
    gateway_receiver = ""
    if binding is not None:
        try:
            gateway_receiver = str(binding.provider_for(_GATEWAY_ROLE) or "").strip()
        except Exception:  # noqa: BLE001 - an unresolvable binding -> blank -> RETURN_NO_GATEWAY
            gateway_receiver = ""
    return OwningLaneBinding(
        status=OWNER_RESOLVED,
        lane_id=lane,
        generation=generation,
        gateway_receiver=gateway_receiver,
    )


def owning_lane_generation_reader(
    workspace_id: str, *, lifecycle_store: object
) -> "Callable[[CallbackOutboxRow], str]":
    """The independent send-time live-generation authority for a review_result return row (#13684).

    Correction 1: the delivery authority must read the live generation from an authority independent of
    the row, never copy the row's expected value. This returns the owning lane's **current** durable
    revision ONLY when (a) the row is a ``review_return:<lane>`` route and (b) the issue's current active
    owner is still exactly the row's recorded ``target_lane``. A supersession that switched the owner to
    a different lane (owner mismatch) or an absent / ambiguous / unreadable owner yields ``""`` -> a
    generation mismatch at :func:`authorize_background_delivery` -> zero-send the stale row. A same-lane
    revision bump likewise yields a live revision that differs from the row's expected -> zero-send. Any
    other route (coordinator) returns ``""`` so its Phase A fail-closed-disabled delivery is unchanged.
    """
    from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey

    wsid = str(workspace_id or "").strip()

    def _read(row: CallbackOutboxRow) -> str:
        if not is_review_return_route(str(getattr(row, "callback_route", "") or "")):
            return ""
        issue = str(getattr(row, "issue", "") or "").strip()
        target_lane = str(getattr(row, "target_lane", "") or "").strip()
        if not issue or not target_lane:
            return ""
        try:
            owner = lifecycle_store.resolve_owner(wsid, issue)
        except Exception:  # noqa: BLE001 - an unreadable owner is a blank -> fail-closed
            return ""
        if not getattr(owner, "resolved", False):
            return ""
        if str(getattr(owner, "lane_id", "") or "").strip() != target_lane:
            return ""  # a supersession switched the owner -> zero-send the stale-lane row
        try:
            record = lifecycle_store.get(LaneLifecycleKey(wsid, target_lane))
        except Exception:  # noqa: BLE001 - a broken read is a blank -> fail-closed
            return ""
        return str(record.revision) if record is not None else ""

    return _read


def review_round_send_fence(
    source_fn: Callable[[], Optional[RedmineJournalSource]]
) -> "Callable[[CallbackOutboxRow], str]":
    """The action-time review-round fence for a review_result return row (#13684 R1-F1 / #13974 R8-F1).

    At the send edge the sender calls this to re-verify a correlated ``review_return`` row is STILL
    the current review round: it re-reads the issue's live structured markers through ``source_fn``
    (the ticket-provider boundary — Redmine is the authority, never a notification kind), decodes the
    row's FULL persisted identity (req + head + conclusion) from its payload, and delegates to the pure
    :func:`...review_return_route.review_return_is_current`.

    #13974 R8-F1: the fence returns a THREE-state disposition, not a bool, so the sender does not
    collapse every refusal into a retryable pending row. A *readable* provider that deterministically
    supersedes / invalidates the round (a newer review_request / result / correction, a single-marker
    head / conclusion drift, an ambiguous / conflicting identity, or a row with no verifiable identity)
    yields :data:`REVIEW_ROUND_STALE` -> a terminal zero-send (retry 0, operator-visible). A merely
    *unreadable* provider (source unresolvable / ``None`` / markers unreadable) yields
    :data:`REVIEW_ROUND_UNVERIFIABLE` -> a retryable zero-send, so a genuinely-current callback that hit
    a transient outage is never terminally dropped. A non-return row is unaffected
    (:data:`REVIEW_ROUND_CURRENT`).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
        REVIEW_ROUND_CURRENT,
        REVIEW_ROUND_STALE,
        REVIEW_ROUND_UNVERIFIABLE,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
        markers_from_source,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
        decode_review_return_conclusion,
        decode_review_return_payload,
        decode_review_return_target_head,
        review_return_is_current,
    )

    def _fence(row: CallbackOutboxRow) -> str:
        if not is_review_return_route(str(getattr(row, "callback_route", "") or "")):
            return REVIEW_ROUND_CURRENT  # a non-return row is not fenced
        issue = str(getattr(row, "issue", "") or "").strip()
        review_journal = str(getattr(row, "journal", "") or "").strip()
        if not issue or not review_journal:
            # A review_return row with no verifiable (issue, journal) identity can NEVER be re-verified
            # as current -> deterministic terminal, not a transient we should keep retrying.
            return REVIEW_ROUND_STALE
        try:
            source = source_fn()
        except Exception:  # noqa: BLE001 - a transient unresolvable source -> retryable, not dropped
            return REVIEW_ROUND_UNVERIFIABLE
        if source is None:
            return REVIEW_ROUND_UNVERIFIABLE
        try:
            markers = markers_from_source(source, issue)
        except Exception:  # noqa: BLE001 - a transient unreadable source -> retryable, not dropped
            return REVIEW_ROUND_UNVERIFIABLE
        # The provider IS readable here: decode the FULL persisted identity (req + head + conclusion)
        # and exact-match it against the live markers. A mismatch / drift / ambiguity / conflict is a
        # DETERMINISTIC supersession -> terminal (the round is genuinely no longer current), never a
        # retryable pending row.
        payload = str(getattr(row, "payload", "") or "")
        is_current = review_return_is_current(
            markers, issue, review_journal,
            decode_review_return_payload(payload),
            decode_review_return_target_head(payload),
            decode_review_return_conclusion(payload),
        )
        return REVIEW_ROUND_CURRENT if is_current else REVIEW_ROUND_STALE

    return _fence


#: A review_return discovery pass whose owning-lane binding read raised (fail-open per issue): the
#: issue pass records this single refusal token and returns no candidate, never aborting the sweep.
REVIEW_RETURN_OWNER_READ_ERROR = "owner_binding_read_error"


def discover_fenced_review_returns(
    owner_binding_fn: Callable[[str, str, object], OwningLaneBinding],
    source: object,
    *,
    workspace_id: str,
    issue: str,
    binding: object,
    fence_active: bool,
    anchor: object,
) -> "tuple[tuple, tuple[str, ...]]":
    """Resolve the issue owner + discover generation-fenced review_return candidates (Redmine #13974).

    Returns ``(return_candidates, refusal_reasons)``. In a fenced pass the current dispatch anchor is
    threaded (:func:`review_return_discovery_anchor`) so a review round predating the current lane
    generation is refused at discovery (0-enqueue) rather than retargeted onto the new generation's
    gateway. The refusal reasons are surfaced for observability (a fail-closed zero-send is not a
    silent drop). Fail-open per issue: an owner-read failure yields no candidate + a single
    :data:`REVIEW_RETURN_OWNER_READ_ERROR` token, never aborting the issue pass.
    """
    try:
        owner = owner_binding_fn(workspace_id, issue, binding)
        return_candidates, plans = discover_review_returns(
            source, issue, owner, workspace_id=workspace_id,
            dispatch_anchor_journal=review_return_discovery_anchor(fence_active, anchor),
        )
        return tuple(return_candidates), tuple(p.reason for p in plans if not p.emit)
    except Exception:  # noqa: BLE001 - an owner-read failure never aborts the issue pass
        return (), (REVIEW_RETURN_OWNER_READ_ERROR,)


def review_return_discovery_anchor(fence_active: bool, anchor: object) -> "Optional[str]":
    """The dispatch anchor to thread into review_return discovery (Redmine #13974).

    In a fenced production pass the current dispatch anchor is threaded so a review round predating
    the current lane generation is refused at discovery (an unresolvable anchor becomes ``""`` — fail
    closed). The unfenced supervisor passes ``None`` so discovery behaves exactly as pre-#13974.
    """
    return (str(anchor or "")) if fence_active else None


def resolve_current_review_identity(source: object, issue: str) -> "tuple[str, str, str]":
    """The CURRENT review generation ``(head, request, conclusion)`` from ``issue``'s markers (#13974).

    ``head`` is the latest ``review_request`` marker's ``target_head``
    (:func:`current_review_generation_head`); ``request`` is the latest review_result's LIVE declared
    ``req`` iff it equals its correlated request (:func:`current_review_generation_request`, j#81496 F1);
    ``conclusion`` is the latest review_result's LIVE explicit conclusion
    (:func:`current_review_generation_conclusion`, j#81506) — the three action-time authorities a
    reserved ``review_return`` row must still match. Fail-safe: an unreadable source, or a missing /
    drifted head / req / conclusion, yields blanks so the send-edge fence treats the row as unconfirmed
    → terminal. Never parses prose; the structured marker is the authority.
    """
    if source is None:
        return "", "", ""
    try:
        markers = list(markers_from_source(source, str(issue).strip()))
    except Exception:  # noqa: BLE001 - an unreadable source is a fail-closed blank identity
        return "", "", ""
    issue_s = str(issue).strip()
    return (
        current_review_generation_head(markers, issue_s),
        current_review_generation_request(markers, issue_s),
        current_review_generation_conclusion(markers, issue_s),
    )


def build_supervisor_send_edge_fence(
    anchor: object, coordinator_route: str,
    current_review_head: object = None, current_review_request: object = None,
    current_review_conclusion: object = None,
) -> "Callable[[CallbackOutboxRow], tuple[bool, str]]":
    """Compose the supervisor's per-row send-edge fence (Redmine #13974).

    One ``send_fence_fn`` that terminally fences BOTH a historical coordinator row
    (:func:`...workspace_supervisor.make_send_edge_fence`) and a previous-generation / head- / req- /
    conclusion-drifted review_return row (:func:`...review_return_route.make_review_return_send_edge_fence`,
    conjoining the current review generation ``head`` (j#81454 A), ``request`` (j#81496 F1), AND
    ``conclusion`` (j#81506)) in the same deliver pass, each exempt on the other's rows. The deliver
    pass marks a fenced row terminally uncertain (zero-send, no retry), so a pre-existing misbound
    backlog row converges instead of retrying forever.
    """
    return compose_send_edge_fences(
        make_send_edge_fence(anchor, coordinator_route),
        make_review_return_send_edge_fence(
            anchor, current_review_head, current_review_request, current_review_conclusion
        ),
    )


__all__ = (
    "coordinator_target_tuple",
    "owning_lane_binding",
    "owning_lane_generation_reader",
    "review_round_send_fence",
    "review_return_discovery_anchor",
    "resolve_current_review_identity",
    "build_supervisor_send_edge_fence",
    "discover_fenced_review_returns",
    "REVIEW_RETURN_OWNER_READ_ERROR",
)
