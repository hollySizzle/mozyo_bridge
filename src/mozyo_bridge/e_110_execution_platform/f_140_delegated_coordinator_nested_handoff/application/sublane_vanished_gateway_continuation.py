"""Prepare the stored continuation of a recovered gateway (#14741 j#97220 B6b3-1).

Between "the gateway is live and attested" and "the original implementation request has
been delivered" there is one question worth isolating: WHICH request, exactly. This module
answers it and stops. It sends nothing, reads no delivery ledger, and never reports a
transaction as completed -- those are the next two tranches.

The answer comes only from the stored row. A caller's plan, its anchor, and anything it
would like the continuation to be are not consulted: the row was written when the recovery
was planned, and re-deriving the pointer from today's inputs is how a retry ends up
delivering something the transaction never agreed to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery import (  # noqa: E501
    RECOVERY_ACTION_GENERATION,
    stored_row_is_this_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_vanished_gateway_recovery_live import (  # noqa: E501
    RECOVERED_READY,
    RecoveryActuation,
    actuate_vanished_gateway_recovery,
    recovery_lease_holder,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.vanished_gateway_recovery import (  # noqa: E501
    REDISPATCH_GATEWAY_ONCE,
    RESUME_GATE,
    RequestAnchor,
)

#: The stored continuation is exact and this recovery is ready to deliver it. NOT delivered,
#: NOT confirmed, NOT completed -- naming it any of those would be a claim about a send that
#: has not happened.
CONTINUATION_READY = "continuation_ready"

#: The stored row is gone, unreadable, or is not this recovery any more.
STOPPED_TRANSACTION_UNAVAILABLE = "transaction_unavailable"
#: The stored continuation pointer is not the exact one this rail delivers.
STOPPED_CONTINUATION_INVALID = "continuation_invalid"


@dataclass(frozen=True)
class ContinuationPreparation:
    """The stored pointer to deliver, or the closed reason not to. (pure value)"""

    outcome: str = ""
    stopped: str = ""
    detail: str = ""
    action_id: str = ""
    holder: str = ""
    #: The STORED :class:`ContinuationPointer` itself, carried rather than re-assembled
    #: (audit j#97223). The next legs hand this straight to `drive_continuation_once`; a
    #: flattened copy would mean each of them rebuilding a pointer from raw columns again,
    #: which is the re-derivation this rail exists to avoid.
    pointer: Any = None

    @property
    def ready(self) -> bool:
        return self.outcome == CONTINUATION_READY


def _raw(value: object) -> str:
    """Plain exact text, or ``""``. No strip, no subclass, no coercion."""
    if type(value) is not str:
        return ""
    if not value or value != value.strip():
        return ""
    return value


def _stopped(reason: str, detail: str = "", action_id: str = "") -> ContinuationPreparation:
    return ContinuationPreparation(stopped=reason, detail=detail, action_id=action_id)


def prepare_vanished_gateway_continuation(
    *,
    plan: Any,
    anchor: RequestAnchor,
    store: Any,
    home: Any,
    workspace_id: str,
    actuation_port: Any,
    launch_authority: Any = None,
    store_admission: Any = None,
    clock: Optional[Any] = None,
) -> ContinuationPreparation:
    """Recover the gateway, then say exactly which request it still owes.

    Drives the B6b2 executor first: anything other than ``recovered_ready`` is that
    executor's own closed reason, returned unchanged and with the row not re-read -- there
    is nothing to prepare for a recovery that did not happen.
    """
    actuation = actuate_vanished_gateway_recovery(
        plan=plan,
        anchor=anchor,
        store=store,
        home=home,
        workspace_id=workspace_id,
        actuation_port=actuation_port,
        launch_authority=launch_authority,
        store_admission=store_admission,
        clock=clock,
    )
    if not isinstance(actuation, RecoveryActuation) or actuation.outcome != RECOVERED_READY:
        return _stopped(
            _raw(getattr(actuation, "stopped", "")) or "actuation_stopped",
            _raw(getattr(actuation, "detail", "")),
            _raw(getattr(actuation, "action_id", "")),
        )

    action_id = actuation.action_id
    from mozyo_bridge.core.state.replacement_transaction import (
        ReplacementTransactionKey,
    )

    try:
        key = ReplacementTransactionKey(_raw(workspace_id), action_id)
        stored = store.get(key)
        same = stored is not None and stored_row_is_this_recovery(
            stored, key=key, anchor=anchor, action_id=action_id
        )
    except Exception:  # noqa: BLE001 - KI / SystemExit / GeneratorExit propagate
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE,
            "the stored transaction could not be read",
            action_id,
        )
    if not same:
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE,
            "the stored row at this key is not this recovery",
            action_id,
        )

    # ONLY the stored pointer, and the stored OBJECT: the raw columns were already proved
    # exact by the same-action validator above, so re-flattening them here would just be a
    # second, drifting copy of the thing the next legs actually need.
    try:
        pointer = stored.continuation
    except Exception:  # noqa: BLE001 - a hostile record is input, not truth
        return _stopped(
            STOPPED_TRANSACTION_UNAVAILABLE,
            "the stored continuation could not be read",
            action_id,
        )
    if pointer is None:
        return _stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored continuation is not re-readable",
            action_id,
        )
    if (
        _raw(getattr(pointer, "source", None)) != anchor.source
        or _raw(getattr(pointer, "issue_id", None)) != anchor.issue_id
        or _raw(getattr(pointer, "journal_id", None)) != anchor.journal_id
        or _raw(getattr(pointer, "expected_gate", None)) != RESUME_GATE
        or _raw(getattr(pointer, "next_semantic_action", None)) != REDISPATCH_GATEWAY_ONCE
    ):
        return _stopped(
            STOPPED_CONTINUATION_INVALID,
            "the stored continuation is not this rail's exact pointer",
            action_id,
        )

    return ContinuationPreparation(
        outcome=CONTINUATION_READY,
        action_id=action_id,
        # Derived, never accepted: the delivery leg has to take the same lease this
        # recovery already holds (j#97190 F5).
        holder=recovery_lease_holder(action_id),
        pointer=pointer,
    )


__all__ = (
    "CONTINUATION_READY",
    "STOPPED_CONTINUATION_INVALID",
    "STOPPED_TRANSACTION_UNAVAILABLE",
    "ContinuationPreparation",
    "prepare_vanished_gateway_continuation",
)
