"""Durable causal-key fold for the fleet rehydrate rail (Redmine #15745).

The replay fence of :mod:`.fleet_rehydrate`, kept as its own leaf so the question "may this
exact anchored send be issued now?" has one implementation that the read-only plan and the
``--execute`` actuation both call.

It owns **no new judgement**. Whether a recorded attempt reached the receiver is decided by
the shared injection-stage authority
(:mod:`...f_130_handoff_routing.domain.injection_stage`), and the causal key is built by the
canonical producer (:func:`...f_130_handoff_routing.domain.handoff.build_marker`). This
module only (a) reconstructs the key, (b) selects the ledger entries that byte-match it, and
(c) folds their stages into one closed :data:`...fleet_rehydrate.DISPATCH_STATES` token.

Three decisions carry the safety:

- **The marker is constructed and compared, never parsed.** A record is admitted only when
  the canonical producer, run over that record's own anchor / kind / receiver, renders a
  marker byte-identical to the one the record carries. A record whose stored marker differs
  from what the producer would render is not this key's evidence and is dropped, so no
  hand-written or drifted marker can stand in for a delivery.
- **The rail context is carried, not guessed.** ``sent`` + ``ok`` means different things on
  the standard and queue-enter rails, and :func:`injection_stage_for` needs the mode to tell
  them apart. The ledger stores the *rail* rather than the mode, and its own vocabulary
  fixes the correspondence: :data:`...herdr_delivery_ledger.RAIL_QUEUE_ENTER` is set only by
  the queue-enter rail (``_rail_for`` derives it from the presence of that rail's
  telemetry). Projecting that one token to :data:`MODE_QUEUE_ENTER` is therefore a faithful
  restatement of the record, not an inference — and it is the SAFE direction besides: it can
  only demote an unproven ``ok`` to ``uncertain_partial``, never promote anything.
- **Evidence is attributed to the receiver that would be sent to NOW** (Redmine #15745
  review j#108920 then j#108953 ``finding_generationfence``; verdicts j#108926 / j#108968).
  The marker carries no lane and no generation and the ledger has no generation column, so
  keying on the marker alone let an OLD generation's confirmed delivery answer for a fresh
  one: a relaunched pair — or a supersede successor — read as ``delivered`` and never
  received the pointer.

  The same-generation proof is **not** re-invented here and is **not** a row revision. The
  first attempt at this fence compared the inventory ``row_revision``, which
  :func:`...herdr_identity.process_generation_of_locator` documents is *not* a
  process-generation id (it mirrors ``terminal.revision`` and advances on presentation
  changes such as a title update). Treating a revision drift as "a different generation"
  dropped a confirmed record and re-promoted the key to ``owed`` — i.e. it would have
  **re-sent the body to the same live receiver**, against ADR-0002's exactly-once body and
  the issue's no-blind-replay condition. The proof is now the shared #15227 authority
  :func:`...herdr_live_attestation_time.fresh_attestation_identity` /
  :meth:`FreshGenerationBoundary.matches_delivery`, which joins one fresh inventory
  snapshot, the terminal-verified attestation, and the verified startup-action generation
  token against the ledger row's own ``gateway_binding`` on all six axes.

  Attribution is three-valued and **failure is always towards not-sending**: only a
  positively verified same generation is evidence, only a locator that is positively absent
  from a readable snapshot is ruled out, and everything else — including any axis this rail
  cannot read — is :data:`ATTRIB_UNKNOWN`, which blocks the lane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from mozyo_bridge.core.state.herdr_delivery_ledger import (
    ENTRY_DELIVERY_OUTCOME,
    RAIL_QUEUE_ENTER,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    RedmineAnchor,
    build_marker,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_send_semantics import (  # noqa: E501
    MODE_QUEUE_ENTER,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (  # noqa: E501
    STAGE_NOT_SENT,
    STAGE_SUBMITTED_CONFIRMED,
    STAGE_UNCERTAIN_PARTIAL,
    injection_stage_for,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate import (  # noqa: E501
    DISPATCH_ATTRIBUTION_UNKNOWN,
    DISPATCH_DELIVERED,
    DISPATCH_NOT_APPLICABLE,
    DISPATCH_OWED,
    DISPATCH_UNCERTAIN,
    DISPATCH_UNREADABLE,
    LaneDispatchFact,
)

#: The handoff kind the create-side rail dispatches to a lane gateway.
KIND_IMPLEMENTATION_REQUEST = "implementation_request"
#: The handoff kind a resume pointer rides (the #14203 / #14661 shape: an existing anchor is
#: re-pointed at, never re-generated).
KIND_REPLY = "reply"

# -- receiver attribution (closed) -------------------------------------------

#: The record was delivered to the receiver process this lane would send to now. Its stage
#: is evidence about the CURRENT generation.
ATTRIB_CURRENT = "current_receiver"
#: The record was delivered to a receiver that is no longer there (a different locator, or
#: no live slot at all). It says nothing about a receiver that does not yet exist, so it is
#: not evidence — and re-issuing to a fresh process cannot duplicate what a dead one held.
ATTRIB_RETIRED = "retired_receiver"
#: The record cannot be placed on either side. Fail-closed: the lane blocks.
ATTRIB_UNKNOWN = "unknown_receiver"


@dataclass(frozen=True)
class ReceiverGeneration:
    """What this rail can prove about the receiver a lane would send to now (pure value).

    Deliberately NOT a set of identity fields to compare here: the same-generation proof is
    the shared #15227 authority, injected as :attr:`matches` (normally
    :meth:`...herdr_live_attestation_time.FreshGenerationBoundary.matches_delivery`), so
    this module owns no second opinion about what a generation is. ``matches`` is ``None``
    when no boundary could be resolved at all — an honest "cannot prove", never a "no".

    ``live_locators`` is every locator the fresh snapshot showed, used ONLY for the positive
    absence direction: a locator no row claims belongs to a receiver that is gone.
    ``inventory_readable`` False means the snapshot itself could not be read, and then
    nothing can be placed on either side.
    """

    inventory_readable: bool = False
    live_locators: frozenset = frozenset()
    matches: Optional[Callable[[Any], bool]] = None

    @classmethod
    def unreadable(cls) -> "ReceiverGeneration":
        """The live inventory could not be read: no record can be placed."""
        return cls(inventory_readable=False)

    @classmethod
    def absent(cls) -> "ReceiverGeneration":
        """A readable snapshot with no live slot for this lane — the post-restart case."""
        return cls(inventory_readable=True)

    def proves_current(self, record: Any) -> bool:
        if self.matches is None:
            return False
        try:
            return bool(self.matches(record))
        except Exception:  # noqa: BLE001 - an authority that raises proves nothing
            return False

    def as_payload(self) -> dict:
        return {
            "inventory_readable": self.inventory_readable,
            "live_locator_count": len(self.live_locators),
            "generation_proof_available": self.matches is not None,
        }


def redmine_marker(issue: str, journal: str, kind: str, receiver: str) -> str:
    """The canonical landing marker for one anchored send (pure).

    A thin, single-purpose wrapper over the canonical producer so every caller in this rail
    builds the key the same way and none of them assembles marker text by hand.
    """
    return build_marker(RedmineAnchor(issue=str(issue), journal=str(journal)), kind, receiver)


def _record_marker_is_canonical(record: Any, kind: str, receiver: str) -> bool:
    """Does the record's stored marker byte-match what the producer would render for it?"""
    stored = getattr(record, "notification_marker", None)
    if not isinstance(stored, str) or not stored:
        return False
    issue = getattr(record, "issue_id", None)
    journal = getattr(record, "journal_id", None)
    if not isinstance(issue, str) or not isinstance(journal, str):
        return False
    if not issue or not journal:
        return False
    if getattr(record, "source", None) != "redmine":
        return False
    if getattr(record, "receiver", None) != receiver:
        return False
    return stored == redmine_marker(issue, journal, kind, receiver)


def _stage_of(record: Any) -> str:
    """The shared injection-stage classification of one ledger entry (pure)."""
    rail = getattr(record, "rail", None)
    return injection_stage_for(
        getattr(record, "status", None),
        getattr(record, "reason", None),
        mode=MODE_QUEUE_ENTER if rail == RAIL_QUEUE_ENTER else None,
        queue_enter_turn_start_observation=getattr(
            record, "queue_enter_observation", None
        ),
        turn_start_outcome=getattr(record, "turn_start_outcome", None),
    )


def attribute_record(record: Any, generation: ReceiverGeneration) -> str:
    """Place one recorded attempt relative to the CURRENT receiver (pure, three-valued).

    The ordering IS the safety property (review j#108953): the only path to
    :data:`ATTRIB_CURRENT` is a positively verified same generation, and the only path to
    :data:`ATTRIB_RETIRED` is a locator positively absent from a readable snapshot. Every
    other shape — an unreadable snapshot, a missing target, a live locator whose generation
    cannot be proven — is :data:`ATTRIB_UNKNOWN`. Nothing about a record's *fields* can
    license a resend on its own.
    """
    if not generation.inventory_readable:
        return ATTRIB_UNKNOWN
    if generation.proves_current(record):
        return ATTRIB_CURRENT
    target = getattr(record, "target", None)
    target = target.strip() if isinstance(target, str) else ""
    if not target:
        return ATTRIB_UNKNOWN
    if target not in generation.live_locators:
        # Positive absence over a readable snapshot: whoever received this is gone, so it
        # is not evidence about the receiver we are about to stand up, and re-issuing to a
        # fresh process cannot duplicate what a dead one held.
        return ATTRIB_RETIRED
    # The locator is live but the same generation could not be verified. That is exactly
    # the shape a re-launch under a recycled locator produces, and also the shape an
    # unreadable attestation produces; neither may license a resend.
    return ATTRIB_UNKNOWN


def select_key_records(
    records: Sequence[Any], *, marker: str, kind: str, receiver: str
) -> tuple[Any, ...]:
    """The delivery-outcome entries that are evidence for exactly this causal key (pure).

    Only :data:`ENTRY_DELIVERY_OUTCOME` entries are folded: a ``retry`` / ``disposition``
    entry is an audit chain link on the same marker, not an independent send outcome, and
    counting one as a second attempt would let a recorded reconcile note read as a delivery.
    """
    selected = []
    for record in records:
        if getattr(record, "entry_kind", None) != ENTRY_DELIVERY_OUTCOME:
            continue
        if getattr(record, "notification_marker", None) != marker:
            continue
        if not _record_marker_is_canonical(record, kind, receiver):
            continue
        selected.append(record)
    return tuple(selected)


def fold_dispatch_state(
    records: Sequence[Any],
    *,
    marker: str,
    kind: str,
    receiver: str,
    generation: Optional[ReceiverGeneration] = None,
) -> str:
    """Fold one causal key's recorded attempts into a closed dispatch state (pure).

    Attribution runs FIRST (review j#108920 ``finding_generationfence``): an attempt that
    cannot be placed relative to the current receiver yields
    :data:`DISPATCH_ATTRIBUTION_UNKNOWN` for the whole key, and an attempt aimed at a
    receiver that is gone is dropped rather than folded — it is not evidence about a process
    that does not exist yet, and re-issuing to a fresh one cannot duplicate what a dead one
    held. ``generation=None`` is read as an unreadable snapshot — the fail-closed default,
    so a caller that forgets to supply one can never get a send licensed by omission.

    Then, over the CURRENT-receiver attempts only, fail-closed by ordering: a single
    ``submitted_confirmed`` makes the key delivered, a single ``uncertain_partial`` makes it
    un-replayable, and only a history that is *entirely* ``not_sent`` — or empty — is owed.
    That mirrors :func:`...injection_stage.blind_retry_prohibited`, which admits a retry for
    :data:`STAGE_NOT_SENT` alone.
    """
    generation = generation if generation is not None else ReceiverGeneration.unreadable()
    selected = select_key_records(records, marker=marker, kind=kind, receiver=receiver)
    attributions = [attribute_record(record, generation) for record in selected]
    if ATTRIB_UNKNOWN in attributions:
        return DISPATCH_ATTRIBUTION_UNKNOWN
    stages = {
        _stage_of(record)
        for record, where in zip(selected, attributions)
        if where == ATTRIB_CURRENT
    }
    if STAGE_SUBMITTED_CONFIRMED in stages:
        return DISPATCH_DELIVERED
    if STAGE_UNCERTAIN_PARTIAL in stages:
        return DISPATCH_UNCERTAIN
    if not stages or stages == {STAGE_NOT_SENT}:
        return DISPATCH_OWED
    # Unreachable while INJECTION_STAGES stays a 3-token vocabulary; kept so a widened
    # vocabulary fails closed instead of falling through to "owed".
    return DISPATCH_UNCERTAIN


def dispatch_fact(
    records: Optional[Sequence[Any]],
    *,
    issue: str,
    journal: str,
    kind: str,
    receiver: str,
    generation: Optional[ReceiverGeneration] = None,
    unreadable: bool = False,
    detail: str = "",
) -> LaneDispatchFact:
    """Build the typed :class:`LaneDispatchFact` for one causal key (pure).

    ``unreadable`` (or ``records is None``) yields :data:`DISPATCH_UNREADABLE`: not observing
    a delivery is never the same as there being none. An absent ``journal`` yields
    :data:`DISPATCH_NOT_APPLICABLE` — this lane owes no such send because no anchor binds
    one — which the planner distinguishes from an owed-but-unanchored send.
    """
    issue_s = str(issue or "").strip()
    journal_s = str(journal or "").strip()
    if unreadable or records is None:
        return LaneDispatchFact(
            state=DISPATCH_UNREADABLE,
            anchor_issue=issue_s,
            anchor_journal=journal_s,
            detail=detail or "the durable delivery record could not be read",
        )
    if not issue_s or not journal_s:
        return LaneDispatchFact(
            state=DISPATCH_NOT_APPLICABLE,
            anchor_issue=issue_s,
            anchor_journal=journal_s,
            detail=detail,
        )
    marker = redmine_marker(issue_s, journal_s, kind, receiver)
    selected = select_key_records(records, marker=marker, kind=kind, receiver=receiver)
    state = fold_dispatch_state(
        records, marker=marker, kind=kind, receiver=receiver, generation=generation
    )
    resolved_detail = detail
    if state == DISPATCH_ATTRIBUTION_UNKNOWN and not resolved_detail:
        resolved_detail = (
            "a recorded attempt could not be placed relative to the receiver this lane "
            "would send to now: the live inventory was unreadable, the record names no "
            "target, or the locator is live but the terminal-verified same-generation "
            "proof did not hold"
        )
    return LaneDispatchFact(
        state=state,
        anchor_issue=issue_s,
        anchor_journal=journal_s,
        marker=marker,
        attempts=len(selected),
        detail=resolved_detail,
    )


def latest_anchor_journal(
    records: Sequence[Any], *, issue: str, kind: str, receiver: str
) -> str:
    """The journal of the newest canonical send of ``kind`` recorded for ``issue`` (pure).

    Used to bind the rehydrate to the anchor the fleet ACTUALLY dispatched under, rather
    than to whatever anchor the lifecycle row happens to carry now (a later disposition CAS
    moves ``decision_journal``, and re-issuing under a moved anchor would be a different
    send under a re-used name). Empty when no canonical record exists — the caller then
    falls back to the lifecycle anchor, which is the correct source for a lane that was
    never dispatched at all.
    """
    best_id = -1
    best_journal = ""
    for record in records:
        if getattr(record, "entry_kind", None) != ENTRY_DELIVERY_OUTCOME:
            continue
        if getattr(record, "issue_id", None) != str(issue):
            continue
        if not _record_marker_is_canonical(record, kind, receiver):
            continue
        entry_id = getattr(record, "entry_id", None)
        if not isinstance(entry_id, int):
            continue
        if entry_id > best_id:
            best_id = entry_id
            best_journal = str(getattr(record, "journal_id", "") or "")
    return best_journal


__all__ = (
    "ATTRIB_CURRENT",
    "ATTRIB_RETIRED",
    "ATTRIB_UNKNOWN",
    "KIND_IMPLEMENTATION_REQUEST",
    "KIND_REPLY",
    "ReceiverGeneration",
    "attribute_record",
    "dispatch_fact",
    "fold_dispatch_state",
    "latest_anchor_journal",
    "redmine_marker",
    "select_key_records",
)
