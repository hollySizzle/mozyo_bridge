"""Review-round state: which journals are rounds, and whether the newest one is unresolved.

Extracted from :mod:`.glance_journal_grammar` when that module hit the oversized-module gate for
the fourth time (Redmine #14695). Shaving comments had bought a few lines each of the previous
three times; a fourth means the module is structurally over-full, and the gate's own prescribed
remedy is to reduce rather than to allowlist.

This is a coherent unit to carve off: what counts as a review round, whether a round is open, and
what the newest round's outcome is are one question asked by several consumers (the grammar's F10
progression suppression, the ``codex_direct_edit`` exemption's supersession, the #14695 waiver's
supersession, and the pending-review close fence).

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
    REVIEW_APPROVED,
)


#: The recognized gates that constitute an OPEN review round. A round is an explicit request for
#: an independent review, so an exemption recorded BEFORE it does not close it (Redmine #14539).
#: ``implementation_done`` is deliberately absent: it states that implementation finished, not
#: that a review was requested — whether a review is owed on it is exactly what the exemption
#: policy decides, so its ordering against the exemption journal is irrelevant.
REVIEW_ROUND_GATES: frozenset[str] = frozenset({GATE_REVIEW_REQUEST, GATE_REVIEW})

def is_open_review_round(gates: "frozenset | set", conclusion: str) -> bool:
    """Whether this journal's gates constitute an OPEN review round (pure).

    A ``review_request`` is always open — it asks for a review that has not answered yet. A
    ``review`` is open unless it CONCLUDED approved; ``pending`` (an unreadable / absent 結論) and
    ``changes_requested`` both leave the round owed, and ``pending`` in particular must count as
    open because it is the fail-closed read of a review whose conclusion could not be established.
    """
    if GATE_REVIEW_REQUEST in gates:
        return True
    return GATE_REVIEW in gates and conclusion != REVIEW_APPROVED


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
    # The gate is reduced to the round-family member so a combined heading is carried as the round
    # it opened: ``review`` wins over ``review_request`` because a result answers a request.
    gate = ""
    if newest is not None:
        gates = newest.gates_or_gate
        gate = GATE_REVIEW if GATE_REVIEW in gates else GATE_REVIEW_REQUEST
    return (
        [r.journal_id for r in rounds],
        unresolved,
        gate,
        (newest.review_conclusion if newest is not None else ""),
        bool(getattr(newest, "blocker", False)) if newest is not None else False,
    )


__all__ = ("REVIEW_ROUND_GATES", "is_open_review_round", "review_round_state")
