"""The bounded pass's ONE external-mutation budget (Redmine #14219 T3 Final Disposition j#87188 = B).

A ``run_once`` bounded pass performs AT MOST ONE external mutation total across ALL workspaces — one
callback delivery (a receiver wake) OR a reconcile provider side-effect OR a hibernate
lifecycle/process actuation. Delivery holds first priority for that single slot; a deterministic
zero-send does NOT consume it; an UNCERTAIN external effect consumes it (no blind continuation).
Supervisor-internal lease / claim / reservation / idempotency records and provider READS are NOT
external mutations and never count (Final Disposition j#87188 boundary 1).

The budget is a shared mutable dict — the same ``{"mutated", "uncertain", "reads"}`` the folded
hibernate leg threads — so once delivery spends it the folded hibernate leg also defers, and vice
versa. It is enforced WITHOUT breaking the row-level exactly-once contract: a budget-deferred
callback row is stopped at the PRE-SEND edge by :func:`external_budget_defer_fence` (fed to the
outbox processor's ``defer_fence_fn``, which RELEASES the claim back to ``pending`` — no
``send_attempted``, no attempt bump, no dead-letter), so the very next event-wake / timer pass
delivers it. :func:`budgeted_sender` spends the budget the instant a real wake (or an uncertain
send) lands, so every later row / issue / workspace in the same pass then defers.
"""

from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (  # noqa: E501
    SEND_DELIVERED,
    SEND_NOT_SENT,
    normalize_send_result,
)

#: The redaction-safe defer reason a budget-deferred row carries in the delivery report.
PASS_BUDGET_SPENT_DEFER = "pass_external_mutation_budget_spent"


def budget_spent(pass_budget) -> bool:
    """True once this pass has performed (or become uncertain about) its one external mutation."""
    return bool(pass_budget.get("mutated") or pass_budget.get("uncertain"))


def external_budget_defer_fence(pass_budget):
    """A per-row pre-send defer fence: every row is DEFERRED (released to pending) once the pass
    has spent its one external-mutation budget, so it is delivered next pass — never dropped."""

    def fence(row):
        if budget_spent(pass_budget):
            return (True, PASS_BUDGET_SPENT_DEFER)
        return (False, "")

    return fence


def compose_defer_fences(*fences):
    """Compose defer fences (short-circuit on the first that defers); ``None`` entries are ignored."""
    active = [f for f in fences if f is not None]
    if not active:
        return None

    def fence(row):
        for f in active:
            deferred, reason = f(row)
            if deferred:
                return (True, reason)
        return (False, "")

    return fence


def budgeted_sender(inner_sender, pass_budget):
    """Wrap a delivery sender so the FIRST real receiver wake (``delivered``) OR an UNCERTAIN send
    spends the pass's one external-mutation budget; a deterministic ``not_sent`` never does."""

    def send(row):
        result = inner_sender(row)
        outcome = normalize_send_result(result).outcome
        if outcome == SEND_DELIVERED:
            pass_budget["mutated"] = True
        elif outcome != SEND_NOT_SENT:
            pass_budget["uncertain"] = True
        return result

    return send


__all__ = (
    "PASS_BUDGET_SPENT_DEFER",
    "budget_spent",
    "external_budget_defer_fence",
    "compose_defer_fences",
    "budgeted_sender",
)
