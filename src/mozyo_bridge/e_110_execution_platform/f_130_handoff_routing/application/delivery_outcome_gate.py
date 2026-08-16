"""Positive-delivery gate over the handoff transport outcome (Redmine #13583 R2-F2).

``orchestrate_handoff`` returns the CLI exit code, and **``rc == 0`` is not proof of a causally
confirmed receiver turn**:

- ``--mode pending`` types the body but never presses Enter and still returns ``0``
  (``status="pending_input"``, ``reason="ok"`` — note the ``ok`` *reason*, so the **status** must be
  checked too);
- the tmux-compatibility ``queue-enter`` rail can return ``0`` without observing its landing
  marker (``status="sent"``, ``reason="queue_enter"``).  That is a practical queued submission,
  not causal turn-start evidence.  Herdr instead withholds success until its causal rail confirms
  the turn.

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

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    STAGE_SUBMITTED_CONFIRMED,
    injection_stage_for_outcome,
)

#: Historical wire tokens for the positive cell.  They are insufficient by themselves: the
#: shared full-outcome authority below also checks mode-specific causal evidence.
#:
#: Redmine #14232: these two constants are now the *statement* of one cell of the shared
#: injection-stage authority (``injection_stage.injection_stage_for`` maps exactly
#: ``sent`` + ``ok`` to :data:`...injection_stage.STAGE_SUBMITTED_CONFIRMED`), not a second
#: private definition. :func:`delivery_was_positive` evaluates that authority so this gate and
#: the callback / outbox retry authority can no longer drift apart — the divergence #14232
#: j#84877 recorded, where a marker-unobserved ``queue_enter`` was non-positive here and
#: *delivered* there. They are kept exported because callers assert against them by name.
POSITIVE_STATUS = "sent"
POSITIVE_REASON = "ok"

#: The attribute ``orchestrate_handoff`` publishes the terminal outcome onto.
DELIVERY_OUTCOME_ATTR = "delivery_outcome"


def publish_delivery_outcome(args: argparse.Namespace, outcome) -> None:
    """Expose the REAL transport outcome to the caller (called at each terminal delivery path)."""
    setattr(args, DELIVERY_OUTCOME_ATTR, outcome)


def delivery_was_positive(args: argparse.Namespace) -> bool:
    """True only when the last ``orchestrate_handoff`` on ``args`` **positively delivered**.

    Positive delivery requires a structured outcome classified as submitted and confirmed.
    ``pending_input`` (body typed, Enter never pressed), a tmux marker-unobserved
    ``queue_enter``, a blocked outcome, and an **absent** outcome are all ``False``.
    """
    outcome = getattr(args, DELIVERY_OUTCOME_ATTR, None)
    if outcome is None:
        return False
    # Redmine #14232: evaluate the SHARED injection-stage authority rather than re-testing the
    # two tokens locally, so this gate and the callback / outbox retry authority can no longer
    # answer "was it delivered?" differently.
    #
    # Review j#95333 F1: read the WHOLE outcome, not the two tokens. A tmux, legacy, or
    # synthetic ``queue-enter`` outcome can report ``sent`` / ``ok`` without causal
    # turn-start evidence. The Herdr rail now supplies that evidence and fails closed when
    # it cannot. The shared classifier keeps both cases safe.
    return injection_stage_for_outcome(outcome) == STAGE_SUBMITTED_CONFIRMED


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
