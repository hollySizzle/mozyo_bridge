"""Fold automatic finished-lane retirement into the existing supervisor pass (#15066).

This leg runs after callback/backlog delivery and before auto-hibernate while the workspace lease
is still held.  It shares the pass-wide one-external-mutation budget: a prior mutation or uncertain
effect defers retirement, and a retirement mutation/uncertainty defers hibernate and later
workspaces.  It neither owns a scheduler nor acquires a second lease.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_LEASE_LOST,
    SKIP_RETIRE_BUDGET_DEFERRED,
    SKIP_RETIRE_DELIVERY_UNCERTAIN,
    SKIP_RETIRE_LEG_ERROR,
    SKIP_RETIRE_WAKE_UNBOUND,
    SKIP_ROSTER_UNREADABLE,
    SUPERVISION_BOUNDED_RECONCILIATION,
    SUPERVISION_LOCAL_WAKE,
)

_FOLDED_MODES = frozenset({SUPERVISION_LOCAL_WAKE, SUPERVISION_BOUNDED_RECONCILIATION})
_UNCERTAIN_DELIVERY_SKIPS = frozenset({SKIP_LEASE_LOST, SKIP_ROSTER_UNREADABLE})


@dataclass(frozen=True)
class RetireAttempt:
    """One redaction-safe automatic-retire attempt."""

    issue: str
    lane: str
    lane_generation: int
    revision: int
    state: str
    reason: str = ""
    mutated: bool = False
    uncertain: bool = False
    cleanup_state: str = "not_started"
    cleanup_reason: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "lane_generation": self.lane_generation,
            "revision": self.revision,
            "state": self.state,
            "reason": self.reason,
            "mutated": self.mutated,
            "uncertain": self.uncertain,
            "cleanup_state": self.cleanup_state,
            "cleanup_reason": self.cleanup_reason,
        }


@dataclass(frozen=True)
class RetirePassResult:
    """Bounded result from the production/injected retire leg."""

    attempts: tuple[RetireAttempt, ...] = ()

    @property
    def mutations(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.mutated)

    @property
    def uncertain(self) -> bool:
        return any(attempt.uncertain for attempt in self.attempts)


def run_folded_retire(
    sup, ws, base_outcome, *, mode, pass_budget, bound_issues, renew
):
    """Run the retire after-leg under an already-held workspace lease."""
    if mode not in _FOLDED_MODES or sup._retire_leg_fn is None:
        return base_outcome
    if pass_budget.get("mutated"):
        return replace(base_outcome, retire_disposition=SKIP_RETIRE_BUDGET_DEFERRED)
    if pass_budget.get("uncertain"):
        return replace(base_outcome, retire_disposition=SKIP_RETIRE_DELIVERY_UNCERTAIN)
    if base_outcome.skipped_reason in _UNCERTAIN_DELIVERY_SKIPS:
        return replace(base_outcome, retire_disposition=SKIP_RETIRE_DELIVERY_UNCERTAIN)
    if base_outcome.delivered > 0:
        return replace(base_outcome, retire_disposition=SKIP_RETIRE_BUDGET_DEFERRED)

    restrict = None
    if mode == SUPERVISION_LOCAL_WAKE:
        restrict = frozenset(
            str(issue).strip() for issue in (bound_issues or ()) if str(issue).strip()
        )
        if not restrict:
            return replace(base_outcome, retire_disposition=SKIP_RETIRE_WAKE_UNBOUND)
    try:
        result = sup._retire_leg_fn(
            ws, renew, pass_budget, restrict_issues=restrict
        )
    except Exception:  # noqa: BLE001 - may be after an external effect
        return replace(base_outcome, retire_disposition=SKIP_RETIRE_LEG_ERROR)

    attempts = tuple(attempt.as_payload() for attempt in result.attempts)
    if result.mutations > 1:
        # A production leg violating the one-mutation contract is an uncertain pass; never allow
        # hibernate or another workspace to actuate behind it.
        return replace(
            base_outcome,
            retire_ran=True,
            retire_mutations=result.mutations,
            retire_attempts=attempts,
            retire_disposition=SKIP_RETIRE_LEG_ERROR,
        )
    return replace(
        base_outcome,
        retire_ran=True,
        retire_mutations=result.mutations,
        retire_attempts=attempts,
        retire_disposition=(SKIP_RETIRE_LEG_ERROR if result.uncertain else ""),
    )


def mark_pass_budget(pass_budget, outcome) -> None:
    """Spend the shared budget on a confirmed or uncertain retirement effect."""
    if outcome.retire_mutations > 0:
        pass_budget["mutated"] = True
    if outcome.retire_disposition == SKIP_RETIRE_LEG_ERROR:
        pass_budget["uncertain"] = True


__all__ = (
    "RetireAttempt",
    "RetirePassResult",
    "run_folded_retire",
    "mark_pass_budget",
)
