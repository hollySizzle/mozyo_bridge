"""Reconcile gate-chain expectation — pure (Redmine #13758 review F1).

Derives, from an active lane's LATEST structured gate marker, which gate is expected next
and which same-lane owner owes it — the ``expected_gate`` / ``expected_next_owner`` the
reconcile state machine reasons over. The dispatch itself (``implementation_request`` /
``review_request`` handoff) is not a gate-bearing marker
(``redmine_journal_source.GATE_BEARING_KINDS``), so the expectation is read from the lane's
workflow POSITION (its latest gate), not from a dispatch marker.

Scope: the primary worker-owed / gateway-owed steps the acceptance criteria exercise. A
position the reconciler does not model (an approved review awaiting owner close, an
``owner_close_approval_waiting``, a bare ``blocked``) returns ``None`` — the reconciler
does NOT self-heal a coordinator / owner-owed state (fail-safe: it never re-notifies a lane
for a gate it cannot attribute to a same-lane owner). This module is pure — a total function
over the latest gate token + review conclusion — so every branch is test-pinned.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Same-lane owners the reconciler routes a self-heal to (fixed role tokens).
# ---------------------------------------------------------------------------
OWNER_WORKER = "implementation_worker"
OWNER_GATEWAY = "implementation_gateway"

# ---------------------------------------------------------------------------
# The gate tokens (marker-facing names; the ``## Gate:`` grammar).
# ---------------------------------------------------------------------------
GATE_IMPLEMENTATION_DONE = "implementation_done"
GATE_REVIEW_REQUEST = "review_request"
GATE_REVIEW_RESULT = "review_result"
GATE_BLOCKED = "blocked"
GATE_OWNER_CLOSE_WAITING = "owner_close_approval_waiting"

#: The review-result conclusion that returns work to the worker (a re-implementation is owed).
_REVIEW_CHANGES_REQUESTED = "changes_requested"

#: The satisfying gate a dispatch position expects, plus who owes it. ``None`` (no marker yet)
#: is the freshly-dispatched worker owing its first implementation_done.
_CHAIN: dict[Optional[str], tuple[str, str]] = {
    None: (GATE_IMPLEMENTATION_DONE, OWNER_WORKER),
    # implementation_done recorded, but the worker has not yet requested review -> it owes
    # the review_request (a same-lane worker gate).
    GATE_IMPLEMENTATION_DONE: (GATE_REVIEW_REQUEST, OWNER_WORKER),
    # review_request recorded -> the same-lane gateway owes the review_result.
    GATE_REVIEW_REQUEST: (GATE_REVIEW_RESULT, OWNER_GATEWAY),
}


def expected_next(
    latest_gate: object, *, review_conclusion: object = ""
) -> Optional[tuple[str, str]]:
    """The ``(expected_gate, expected_next_owner)`` for a lane's latest gate position. (pure)

    ``latest_gate`` is the lane's most recent gate-bearing marker token (or a blank / ``None``
    when the lane has produced no gate yet — the freshly-dispatched worker owing its first
    implementation_done). Returns ``None`` for a position the reconciler does not attribute to
    a same-lane owner:

    - ``review_result`` with ``changes_requested`` -> the worker owes the re-implementation
      (``implementation_done``); an approved / other review_result -> ``None`` (owner-owed close);
    - ``blocked`` / ``owner_close_approval_waiting`` / any unknown token -> ``None``.
    """
    token = str(latest_gate or "").strip()
    if not token:
        return _CHAIN[None]
    if token == GATE_REVIEW_RESULT:
        if str(review_conclusion or "").strip() == _REVIEW_CHANGES_REQUESTED:
            return (GATE_IMPLEMENTATION_DONE, OWNER_WORKER)
        return None  # approved / other -> owner-owed close, not a same-lane self-heal
    return _CHAIN.get(token)


__all__ = (
    "OWNER_WORKER",
    "OWNER_GATEWAY",
    "GATE_IMPLEMENTATION_DONE",
    "GATE_REVIEW_REQUEST",
    "GATE_REVIEW_RESULT",
    "GATE_BLOCKED",
    "GATE_OWNER_CLOSE_WAITING",
    "expected_next",
)
