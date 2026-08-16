"""Positive-delivery gate over the handoff transport outcome (Redmine #13583 R2-F2).

``orchestrate_handoff`` returns the CLI exit code, and **``rc == 0`` is not proof of a causally
confirmed receiver turn**:

- ``--mode pending`` types the body but never presses Enter and still returns ``0``
  (``status="pending_input"``, ``reason="ok"`` — note the ``ok`` *reason*, so the **status** must be
  checked too);
- the tmux-compatibility ``queue-enter`` rail can return ``0`` without observing its landing
  marker (``status="sent"``, ``reason="queue_enter"``).  That is a practical queued submission,
  not causal turn-start evidence.  Herdr withholds success until its causal rail confirms the
  turn on an idle / turn-ended baseline, or — on a busy baseline (ADR-0002 / #15537) — until
  the injected body clears the composer behind the wait-free full effect fence (the noncausal
  ``sent`` / ``queue_enter`` queued submission, which IS a positive delivery here even though
  its injection stage stays ``uncertain_partial``).

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
    delivery_positively_confirmed,
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

    Two proofs qualify (review j#106497): a structured outcome classified as submitted and
    confirmed (causal, idle / turn-ended), or the exact herdr busy queued submission —
    ``sent`` / ``queue_enter`` with producer-exact ``busy_queue_path`` /
    ``queued_submission_confirmed`` bools (composer cleared, ADR-0002 / #15537) — whose
    stage stays ``uncertain_partial``. ``pending_input`` (body typed, Enter never pressed),
    a tmux marker-unobserved ``queue_enter``, malformed observation shapes, a blocked
    outcome, and an **absent** outcome are all ``False``.
    """
    outcome = getattr(args, DELIVERY_OUTCOME_ATTR, None)
    if outcome is None:
        return False
    # Redmine #14232: evaluate the SHARED positive-delivery authority rather than re-testing
    # tokens locally, so this gate and the callback / outbox retry authority can no longer
    # answer "was it delivered?" differently.
    #
    # Review j#95333 F1: read the WHOLE outcome, not the two tokens. Review j#106497
    # (finding_busyprojection): the herdr busy queued submission (ADR-0002 / #15537) is the
    # second positive proof — exact ``busy_queue_path`` / ``queued_submission_confirmed``
    # bools over ``sent`` / ``queue_enter`` — while a tmux, legacy, or synthetic
    # ``queue_enter`` (no such observation) and malformed shapes stay non-positive.
    return delivery_positively_confirmed(outcome)


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
