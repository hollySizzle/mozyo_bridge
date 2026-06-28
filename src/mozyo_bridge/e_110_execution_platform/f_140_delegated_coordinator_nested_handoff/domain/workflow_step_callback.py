"""Determined-callback rail-field derivation for `workflow step` (Redmine #12755).

Split out of :mod:`...domain.workflow_step` so that module stays under the
module-health line cap (the same extraction pattern as ``cli_handoff_ticketless`` /
``cli_project_gateway_child_intake``). This leaf owns the pure mapping from a
lane's *already-determined* callback ``classification`` to the structured fields
the ``handoff ticketless-callback`` rail requires (#12703).

The pending callback returns the lane's determined result to the caller over the
no-anchor callback rail. The rail requires five structured fields; they are a
fixed function of the determined ``classification`` (the lane decided the
classification — never a domain/design answer — and the rest follow). The
``read_contract`` is supplied separately from the caller lane role.
"""

from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_callback import (
    CLASSIFICATION_ANCHOR_REQUIRED,
    CLASSIFICATION_BLOCKED,
    CLASSIFICATION_CONSULTATION_RESULT,
    CLASSIFICATION_NO_DISPATCH,
    CLASSIFICATIONS,
    DISPATCH_ANCHOR_REQUIRED_BEFORE_WORKER,
    DISPATCH_HAND_BACK_TO_CALLER,
    DISPATCH_NO_DISPATCH,
    OWNER_CALLER as CB_OWNER_CALLER,
    REASON_ANCHOR_REQUIRED_FOR_WORKER,
    REASON_BLOCKED_PENDING_DECISION,
    REASON_CONSULTATION_CLASSIFIED,
    REASON_NO_DISPATCH_DECIDED,
)


class WorkflowStepError(ValueError):
    """A determined-callback classification is not one the ticketless rail carries."""


# classification -> (dispatch_decision, callback_reason). The ticketless callback
# rail's no-anchor-safe decisions only; an actual worker dispatch needs a real
# Redmine anchor via `handoff send`, so it is deliberately not expressible here.
_CALLBACK_FIELD_BY_CLASSIFICATION: dict[str, tuple[str, str]] = {
    CLASSIFICATION_BLOCKED: (DISPATCH_HAND_BACK_TO_CALLER, REASON_BLOCKED_PENDING_DECISION),
    CLASSIFICATION_ANCHOR_REQUIRED: (
        DISPATCH_ANCHOR_REQUIRED_BEFORE_WORKER,
        REASON_ANCHOR_REQUIRED_FOR_WORKER,
    ),
    CLASSIFICATION_NO_DISPATCH: (DISPATCH_NO_DISPATCH, REASON_NO_DISPATCH_DECIDED),
    CLASSIFICATION_CONSULTATION_RESULT: (
        DISPATCH_HAND_BACK_TO_CALLER,
        REASON_CONSULTATION_CLASSIFIED,
    ),
}


def callback_rail_fields(classification: str) -> dict[str, str]:
    """Map a determined callback ``classification`` to the ticketless rail fields.

    Fail-closed: a classification the ticketless callback rail does not carry (e.g.
    ``review_ready``, which is an anchored review path, not a no-anchor callback)
    raises :class:`WorkflowStepError` rather than fabricating fields. The workflow
    next-owner is always the caller (the callback returns *up*).
    """
    mapped = _CALLBACK_FIELD_BY_CLASSIFICATION.get(classification)
    if mapped is None:
        raise WorkflowStepError(
            f"callback classification {classification!r} is not carried by the "
            f"ticketless no-anchor callback rail; expected one of {list(CLASSIFICATIONS)}"
        )
    dispatch_decision, callback_reason = mapped
    return {
        "classification": classification,
        "dispatch_decision": dispatch_decision,
        "callback_reason": callback_reason,
        "workflow_next_owner": CB_OWNER_CALLER,
    }


__all__ = ("WorkflowStepError", "callback_rail_fields")
