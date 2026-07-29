"""Review-round state: which journals are rounds, and whether the newest one is unresolved.

Extracted from :mod:`.glance_journal_grammar` when that module hit the oversized-module gate for
the fourth time (Redmine #14695). Shaving comments had bought a few lines each of the previous
three times; a fourth means the module is structurally over-full, and the gate's own prescribed
remedy is to reduce rather than to allowlist.

This is a coherent unit to carve off: what counts as a review round and what the newest round's
outcome is are one question asked by several consumers (the grammar's F10 progression
suppression, the ``codex_direct_edit`` exemption's supersession, the #14695 waiver's
supersession, and the pending-review close fence).

WHICH gates are rounds (:data:`.sublane_admission.REVIEW_ROUND_GATES`) and whether a round is
OPEN (:func:`.sublane_admission.is_open_review_round`) are deliberately NOT decided here. Both
live beside the gate vocabulary they read and are re-exported from this module for the consumers
that already import them from here. Review j#94110 finding 1 is why: this module used to answer
the openness question a second time, by reducing a combined ``review_request + review`` heading
to ``review``, and the two answers disagreed — the predicate called the journal open while the
reduction handed the classifier an approved review. One question, one definition.

Callers pass their own recognized-journal objects; this module only requires ``journal_id``,
``gates_or_gate`` and ``review_conclusion``, so the gate VOCABULARY and gate RECOGNITION stay with
the grammar that owns them.

Boundary: pure.
"""

from __future__ import annotations

from typing import Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
    GATE_REVIEW,
    GATE_REVIEW_REQUEST,
    REVIEW_ROUND_GATES,
    is_open_review_round,
)


def review_round_state(recognized: Sequence[object]) -> Tuple[list, bool, str, str, bool]:
    """Review-round journal ids, and whether the NEWEST of them is unresolved (pure).

    Both answers come from one pass so they cannot disagree about which journals are rounds.
    EVERY recognized gate is read, not the max-precedence reduction: ``Review Request + Close``
    IS a round and reducing it to ``close`` hid it (#14539 review j#91577 F1).

    "Unresolved" is the newest round's own outcome — a request nothing answered, or a review that
    did not conclude approved. It is deliberately independent of which gate is LATEST, because a
    Close recorded afterwards is not a review resolution (#14695 review j#93879 F1).
    """
    rounds = [r for r in recognized if r.gates_or_gate & REVIEW_ROUND_GATES]
    newest = max(rounds, key=lambda r: r.journal_id, default=None)
    unresolved = newest is not None and is_open_review_round(
        newest.gates_or_gate, newest.review_conclusion
    )
    # The newest round's OWN gate and conclusion, not a boolean (#14695 review j#94005 F2).
    # Compressing them lost the distinction the lane state classes are built on: a
    # ``changes_requested`` round means the implementer is working (non-blocking), a pending audit
    # means a review is owed, and a blocker means blocked. Collapsing all three into "unresolved"
    # turned a working lane into a coordinator-blocking one and weakened an explicit blocker.
    #
    # The gate is reduced to ONE round-family member, and the reduction is subordinate to
    # ``unresolved`` rather than independent of it (#14695 review j#94110 finding 1). An earlier
    # version let ``review`` win over ``review_request`` unconditionally, "because a result
    # answers a request" — but :func:`is_open_review_round` says a request is open no matter what
    # accompanies it, so a combined ``review_request + review`` heading concluding 承認 was
    # emitted as ``unresolved=True`` WITH ``gate=review / conclusion=approved``. The close-family
    # branch replays that tuple through the classifier, so the still-open request was re-read as
    # an approved review and advanced to ``owner_waiting`` — the pending-review zero-close fence
    # this issue exists to hold. Two rules answering "is this round open" is the defect; the
    # identity now follows the predicate.
    #
    # So: while the round is OPEN, it is carried as the thing that is open. A request that is
    # part of an open round keeps request identity and carries NO conclusion, because the
    # conclusion on a combined journal describes the review half and would contradict the
    # request half. Only a round that is not open by way of a request reduces to ``review``,
    # which preserves the typed distinction the lane states need (``changes_requested`` ->
    # implementing, pending -> review_waiting, blocker -> blocked; j#94005 F2).
    gate = ""
    conclusion = ""
    if newest is not None:
        gates = newest.gates_or_gate
        if unresolved and GATE_REVIEW_REQUEST in gates:
            gate = GATE_REVIEW_REQUEST
        else:
            gate = GATE_REVIEW if GATE_REVIEW in gates else GATE_REVIEW_REQUEST
            conclusion = newest.review_conclusion
    return (
        [r.journal_id for r in rounds],
        unresolved,
        gate,
        conclusion,
        bool(getattr(newest, "blocker", False)) if newest is not None else False,
    )


__all__ = ("REVIEW_ROUND_GATES", "is_open_review_round", "review_round_state")
