"""Positive-delivery gate over the handoff transport outcome (Redmine #13583 R2-F2).

``orchestrate_handoff`` returns the CLI exit code, and **``rc == 0`` is not proof that the message
reached the receiver**:

- ``--mode pending`` types the body but never presses Enter and still returns ``0``
  (``status="pending_input"``, ``reason="ok"`` — note the ``ok`` *reason*, so the **status** must be
  checked too);
- a ``queue-enter`` send whose landing marker was never observed returns ``0`` as well
  (``status="sent"``, ``reason="queue_enter"`` — the sender did not pre-confirm the landing).

Any caller that may only act on a *delivered* message must therefore read the transport's structured
outcome, not the rc. The #13583 forward-generation completion hook is exactly such a caller:
completing a forward generation on a callback that never landed would let the caller forward again
while the previous consultation is still unanswered.

:func:`publish_delivery_outcome` is called by ``orchestrate_handoff`` at each terminal delivery path
to expose the outcome on the caller's ``args``; :func:`delivery_was_positive` is the fail-closed
predicate the callers read.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

#: The only transport result that counts as a positive delivery: the send landed AND the landing
#: marker was observed.
POSITIVE_STATUS = "sent"
POSITIVE_REASON = "ok"

#: The attribute ``orchestrate_handoff`` publishes the terminal outcome onto.
DELIVERY_OUTCOME_ATTR = "delivery_outcome"


def publish_delivery_outcome(args: argparse.Namespace, outcome) -> None:
    """Expose the REAL transport outcome to the caller (called at each terminal delivery path)."""
    setattr(args, DELIVERY_OUTCOME_ATTR, outcome)


def delivery_was_positive(args: argparse.Namespace) -> bool:
    """True only when the last ``orchestrate_handoff`` on ``args`` **positively delivered**.

    Positive delivery is the structured ``status="sent"`` **and** ``reason="ok"`` (landing marker
    observed). ``pending_input`` (body typed, Enter never pressed — it carries ``reason="ok"``, so
    the status is what disqualifies it), a marker-unobserved ``queue_enter``, a blocked outcome, and
    an **absent** outcome (an early return, or a caller that never sent) are all ``False``.
    """
    outcome = getattr(args, DELIVERY_OUTCOME_ATTR, None)
    if outcome is None:
        return False
    return (
        str(getattr(outcome, "status", "")) == POSITIVE_STATUS
        and str(getattr(outcome, "reason", "")) == POSITIVE_REASON
    )


def make_publishing_emitter(publish: Callable[[Any], None], emit):
    """Wrap ``emit`` so every emitted outcome is published first (Redmine #13583 R3-F1).

    ``orchestrate_handoff`` has many terminal paths (blocked / invalid-args / pending / the
    tmux+queue-enter final / the **herdr event-driven turn-start rail**). Publishing at hand-picked
    ``return`` sites is fragile: the event rail emitted its outcome and returned ``0`` on a ``sent``
    projection while never publishing, so on the normal herdr route ``delivery_was_positive`` was
    always ``False`` and a correlated forward generation could never complete (a fail-safe stuck
    lifecycle). Routing every emit through this wrapper makes publication a property of *emitting*,
    so a newly added terminal path cannot silently miss it.

    Redmine #13729: takes a ``publish`` callback instead of the ``argparse.Namespace``.
    The facade — which owns the Namespace as its caller's return channel — passes
    ``lambda outcome: publish_delivery_outcome(args, outcome)``, so this wrapper (and
    every deep handoff helper) is Namespace-free while the delivery-outcome hand-back
    stays byte-identical.
    """

    def _emit(outcome, **emit_kwargs):
        publish(outcome)
        emit(outcome, **emit_kwargs)

    return _emit


__all__ = (
    "POSITIVE_STATUS",
    "POSITIVE_REASON",
    "DELIVERY_OUTCOME_ATTR",
    "publish_delivery_outcome",
    "delivery_was_positive",
    "make_publishing_emitter",
)
