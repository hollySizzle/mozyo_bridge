"""Real callback sender: fire one semantic handoff, map its outcome (Redmine #13520 / US #13518).

The one-send adapter the :class:`...application.callback_outbox_processor.CallbackOutboxProcessor`
injects into :meth:`deliver`. Design answer j#75098 Q2: a callback fires an **existing semantic
handoff once** (a coordinator new-turn trigger) and authorizes no downstream action; the
receiver reads the durable journal and decides.

The adapter is deliberately thin and testable: the actual live handoff (target resolution,
rail, credentials, transport) is an injected ``send_fn`` — a ``Callable[[CallbackOutboxRow],
HandoffDeliveryResult]`` that performs the one real send and returns its ``(status, reason)``.
The adapter maps that onto the closed :data:`...domain.callback_delivery.SEND_OUTCOMES`
vocabulary through the conservative pure
:func:`...domain.callback_delivery.send_outcome_for_delivery` (only a positively-confirmed
turn-start is ``delivered``; a deterministic pre-injection block is ``not_sent``; anything
ambiguous is ``uncertain``, never auto-retried). The processor has already claimed the row and
checkpointed the send edge, so ``send_fn`` is invoked **exactly once** per claimed row.

Splitting the live send behind ``send_fn`` keeps this module pure of transport / credential
concerns (tests inject a fake), and keeps the live send path — verified under the #13521
scenario / live harness with QA-only anchors — the one place transport happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    CallbackSendResult,
    SEND_UNCERTAIN,
    normalize_zero_send_reason,
    send_outcome_for_delivery,
)


@dataclass(frozen=True)
class HandoffDeliveryResult:
    """The ``(status, reason)`` a one-send ``send_fn`` reports, mirroring a handoff DeliveryOutcome.

    ``status`` is the handoff outcome status (``sent`` / ``pending_input`` / ``blocked``);
    ``reason`` is its reason token. The adapter maps this onto a closed send outcome; a
    ``send_fn`` that could not even produce a result (an exception) is treated as
    :data:`SEND_UNCERTAIN` by :class:`HandoffCallbackSender` (fail-safe, no auto-retry).

    ``persist_ok`` / ``persist_reason`` are **best-effort durable-receipt evidence** (#13520
    review F6): whether the sanctioned ``--persist-delivery`` path wrote a Redmine delivery
    receipt, and its reason token (``ok`` / ``write_optin_unset`` / ``transport_error`` …).
    They are *observability only* and DELIBERATELY do NOT affect the send outcome: the
    authoritative durable callback record is the home-scoped outbox row, not the Redmine
    receipt, so a confirmed turn-start is ``delivered`` even when the receipt did not persist
    (``persist_ok`` is None when no receipt was reported / parsed).
    """

    status: str
    reason: str
    persist_ok: "bool | None" = None
    persist_reason: str = ""


class HandoffCallbackSender:
    """Fires one semantic handoff per callback row and maps the outcome (fail-safe).

    ``send_fn`` performs the single real send for a claimed row and returns a
    :class:`HandoffDeliveryResult`. Any exception it raises is caught and mapped to
    :data:`SEND_UNCERTAIN` — a send that blew up mid-flight may or may not have injected, so it
    is never auto-retried (a duplicate delivery is the failure to avoid).

    Returns a :class:`...domain.callback_delivery.CallbackSendResult` carrying the mapped outcome
    plus the send's best-effort durable-receipt evidence (``persist_ok`` / ``persist_reason``) so a
    ``write_optin_unset`` / transport failure / persisted receipt is **observable** downstream
    (#13520 review R2-F6) — the outcome is unchanged by the evidence (the outbox is the authority).
    """

    def __init__(self, send_fn: Callable[[CallbackOutboxRow], HandoffDeliveryResult]) -> None:
        self._send_fn = send_fn

    def __call__(self, row: CallbackOutboxRow) -> CallbackSendResult:
        try:
            result = self._send_fn(row)
        except Exception:  # noqa: BLE001 - a mid-send failure is fail-safe uncertain, never a retry
            return CallbackSendResult(SEND_UNCERTAIN)
        outcome = send_outcome_for_delivery(result.status, result.reason)
        # Redmine #14248 review j#85410 F1: the send edge's own reason used to be consumed by
        # `send_outcome_for_delivery` and then DROPPED, so a downstream reader could see
        # `not_sent` but never whether it was an authorization refusal or a transport
        # precondition. Carry it forward, normalized through the closed allowlist so the raw
        # string never escapes. Observability only — `outcome` is unchanged.
        return CallbackSendResult(
            outcome,
            persist_ok=result.persist_ok,
            persist_reason=result.persist_reason,
            send_reason=normalize_zero_send_reason(result.reason),
        )


__all__ = (
    "HandoffDeliveryResult",
    "HandoffCallbackSender",
)
