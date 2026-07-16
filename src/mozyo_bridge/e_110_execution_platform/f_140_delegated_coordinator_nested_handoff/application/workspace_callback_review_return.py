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
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    OWNER_RESOLVED,
    OWNER_UNKNOWN,
    OwningLaneBinding,
    is_review_return_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalSource,
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
) -> "Callable[[CallbackOutboxRow], bool]":
    """The action-time review-round fence for a review_result return row (#13684 review R1-F1).

    At the send edge the sender calls this to re-verify a correlated ``review_return`` row is STILL
    the current review round: it re-reads the issue's live structured markers through ``source_fn``
    (the ticket-provider boundary — Redmine is the authority, never a notification kind), decodes the
    row's correlated ``review_request_journal`` from its payload, and delegates to the pure
    :func:`...review_return_route.review_return_is_current`. A newer review_request / review_result /
    correction landing after the row was reserved makes it stale -> False -> the sender zero-sends. A
    non-return row is not fenced (True); an unreadable source is fail-closed (False) — a round we
    cannot re-verify is never delivered.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
        markers_from_source,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
        decode_review_return_payload,
        review_return_is_current,
    )

    def _fence(row: CallbackOutboxRow) -> bool:
        if not is_review_return_route(str(getattr(row, "callback_route", "") or "")):
            return True
        issue = str(getattr(row, "issue", "") or "").strip()
        review_journal = str(getattr(row, "journal", "") or "").strip()
        if not issue or not review_journal:
            return False
        try:
            source = source_fn()
        except Exception:  # noqa: BLE001 - an unresolvable source is a fail-closed stale round
            return False
        if source is None:
            return False
        try:
            markers = markers_from_source(source, issue)
        except Exception:  # noqa: BLE001 - an unreadable source is a fail-closed stale round
            return False
        request_journal = decode_review_return_payload(str(getattr(row, "payload", "") or ""))
        return review_return_is_current(markers, issue, review_journal, request_journal)

    return _fence


__all__ = (
    "coordinator_target_tuple",
    "owning_lane_binding",
    "owning_lane_generation_reader",
    "review_round_send_fence",
)
