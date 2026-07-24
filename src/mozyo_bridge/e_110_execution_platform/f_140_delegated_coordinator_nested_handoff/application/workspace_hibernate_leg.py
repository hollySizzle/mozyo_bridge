"""The supervisor's auto-hibernate phase (Redmine #14219 T2c leg; T3 fold, ruling j#87108).

The hibernate leg is **one leg of the existing ``run_once`` bounded pass**, folded into the
``local_wake`` (event-wake primary) and ``bounded_reconciliation`` (timer/restart fallback)
paths AFTER the callback/outbox delivery + reconcile legs, sharing the pass's ONE external
mutation budget — never a second supervisor, a second queue, or a third scheduler cadence
(Design Consultation Answer j#87108, ruling j#85459 §3).

Two entry shapes delegate to the SAME :func:`run_hibernate_phase` primitive, so the folded and
the standalone paths can never diverge:

* :func:`run_folded_hibernate` — the folded after-leg, called by ``_supervise_workspace`` WHILE IT
  STILL HOLDS this workspace's lease (review j#87154 R1-F1: no release-then-re-acquire), handed the
  ONE pass-wide ``pass_budget`` the delivery/reconcile legs already threaded. It is a typed
  zero-actuation DEFER (leaving the delivery outcome unchanged, recording WHY in
  ``hibernate_disposition``) when an earlier workspace already spent the pass mutation or was
  uncertain, when THIS workspace's own delivery mutated / was uncertain, or (``local_wake`` only)
  when the pass has no wake binding; otherwise it runs the leg via :func:`run_hibernate_phase`.
* :func:`run_hibernate_phase` — run the wired leg under an ALREADY-HELD lease, handed a ``renew``
  callable bound to THIS workspace/holder, the pass-wide shared ``budget``, and (``local_wake``) a
  ``restrict_issues`` scope binding the candidate set to the woken issues (review j#87154 R1-F2). It
  acquires and releases NOTHING (the caller owns the lease); it only runs the leg and returns a
  typed result.
* :func:`hibernate_workspace` / :func:`hibernate_sweep` — the standalone ``SUPERVISION_HIBERNATE``
  mode: acquire the lease (the duplicate-supervisor fence), delegate to :func:`run_hibernate_phase`,
  finally release. T3 keeps this as a **production-unreachable internal / focused-test compatibility
  seam** (no CLI / event-pump / scheduler surface selects it — pinned by test); it delegates to the
  same primitive so it stays equivalent to the folded path.

An UNWIRED leg fails closed (``SKIP_HIBERNATE_UNWIRED``) instead of silently no-opping. A leg
that RAISES is an UNCERTAIN mutation status (``SKIP_HIBERNATE_LEG_ERROR``; review j#86739 R3-F1):
the exception may have fired after a side effect, so it consumes the pass-wide one-mutation
budget exactly like a success — the remaining candidates are the typed budget defer, never a
second actuation behind an unknown partial effect.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_HIBERNATE_BUDGET_DEFERRED,
    SKIP_HIBERNATE_DELIVERY_UNCERTAIN,
    SKIP_HIBERNATE_LEG_ERROR,
    SKIP_HIBERNATE_UNWIRED,
    SKIP_HIBERNATE_WAKE_UNBOUND,
    SKIP_LEASE_LOST,
    SKIP_LEASE_REFUSED,
    SKIP_ROSTER_UNREADABLE,
    SUPERVISION_BOUNDED_RECONCILIATION,
    SUPERVISION_LOCAL_WAKE,
    WorkspaceSupervisionOutcome,
)

#: The two folded-pass modes (Design Consultation Answer j#87108): the auto-hibernate leg rides
#: the event-wake (``local_wake``) and the timer/restart (``bounded_reconciliation``) passes only.
#: ``local_drain`` (#14150 provider-free) and the standalone ``SUPERVISION_HIBERNATE`` seam never
#: fold it.
_FOLDED_MODES = frozenset({SUPERVISION_LOCAL_WAKE, SUPERVISION_BOUNDED_RECONCILIATION})

#: The delivery-stage skip tokens that mean the pass was UNCERTAIN / fail-closed for a workspace
#: (a lost lease mid-delivery, an unreadable roster). Hibernate NEVER actuates behind one — no
#: blind continuation after an uncertain prior action (Design Consultation Answer j#87108 §2).
_UNCERTAIN_DELIVERY_SKIPS = frozenset({SKIP_LEASE_LOST, SKIP_ROSTER_UNREADABLE})


def run_folded_hibernate(sup, ws, base_outcome, *, mode, pass_budget, bound_issues, renew):
    """The folded hibernate after-leg for ONE workspace under its ALREADY-HELD lease (j#87154 R1).

    Called from ``_supervise_workspace`` while the caller still holds ``ws``'s lease and BEFORE it
    releases (R1-F1: never a release-then-re-acquire), sharing the ONE ``pass_budget`` the delivery
    legs already threaded. Returns ``base_outcome`` unchanged when the mode does not fold hibernate
    or no leg is wired; otherwise a :func:`dataclasses.replace` with the hibernate fields +
    ``hibernate_disposition`` merged in. Per workspace it is a typed zero-actuation DEFER (delivery
    outcome unchanged, ``hibernate_ran=False``, a redaction-safe disposition token) when:

    * an EARLIER workspace already spent the pass's one mutation (``pass_budget["mutated"]``) or was
      UNCERTAIN (``pass_budget["uncertain"]``) — the whole pass shares one external-mutation budget
      and never actuates behind an uncertain prior action;
    * THIS workspace's own delivery was uncertain (:data:`_UNCERTAIN_DELIVERY_SKIPS`) or already
      mutated (delivered / blocked / supplied an event — delivery keeps priority);
    * (``local_wake`` only, R1-F2) the pass has NO wake binding — a ``local_wake`` pass hibernates
      only the lanes of the exact woken issues; the whole-roster candidate scan is the timer /
      ``bounded_reconciliation`` fallback alone.

    It does NOT write ``pass_budget`` — the caller marks it from the merged outcome via
    :func:`mark_pass_budget` so a single authority updates the shared budget for later workspaces.
    """
    if mode not in _FOLDED_MODES or sup._hibernate_leg_fn is None:
        return base_outcome
    if pass_budget.get("mutated"):
        return replace(base_outcome, hibernate_disposition=SKIP_HIBERNATE_BUDGET_DEFERRED)
    if pass_budget.get("uncertain"):
        return replace(base_outcome, hibernate_disposition=SKIP_HIBERNATE_DELIVERY_UNCERTAIN)
    if base_outcome.skipped_reason in _UNCERTAIN_DELIVERY_SKIPS:
        return replace(base_outcome, hibernate_disposition=SKIP_HIBERNATE_DELIVERY_UNCERTAIN)
    if base_outcome.delivered > 0 or base_outcome.blocked > 0 or base_outcome.events_supplied > 0:
        return replace(base_outcome, hibernate_disposition=SKIP_HIBERNATE_BUDGET_DEFERRED)
    restrict = None
    if mode == SUPERVISION_LOCAL_WAKE:
        bound = frozenset(str(i).strip() for i in (bound_issues or ()) if str(i).strip())
        if not bound:
            return replace(base_outcome, hibernate_disposition=SKIP_HIBERNATE_WAKE_UNBOUND)
        restrict = bound
    phase = run_hibernate_phase(
        sup, ws, budget=pass_budget, renew=renew, restrict_issues=restrict
    )
    if phase.skipped_reason == SKIP_HIBERNATE_LEG_ERROR:
        # UNCERTAIN mutation status: surfaced (never an empty pass) AND consumes the pass budget.
        return replace(base_outcome, hibernate_disposition=SKIP_HIBERNATE_LEG_ERROR)
    if phase.skipped_reason == SKIP_HIBERNATE_UNWIRED:
        return replace(base_outcome, hibernate_disposition=SKIP_HIBERNATE_UNWIRED)
    return replace(
        base_outcome,
        hibernate_ran=phase.ran,
        hibernate_mutations=phase.mutations,
        hibernate_attempts=phase.attempts,
        hibernate_disposition="",
    )


def mark_pass_budget(pass_budget, outcome) -> None:
    """Update the ONE shared ``pass_budget`` from a finished workspace outcome (j#87154 R1-F1).

    The single authority that advances the pass-wide budget so LATER workspaces defer: an external
    mutation (a delivered / blocked / supplied-event delivery leg OR a hibernate mutation) spends the
    one-mutation budget; an uncertain delivery OR a RAISED (leg-error) hibernate marks the pass
    UNCERTAIN so no later workspace actuates behind an unknown partial effect.
    """
    if (
        outcome.delivered > 0
        or outcome.blocked > 0
        or outcome.events_supplied > 0
        or outcome.hibernate_mutations > 0
    ):
        pass_budget["mutated"] = True
    if (
        outcome.skipped_reason in _UNCERTAIN_DELIVERY_SKIPS
        or outcome.hibernate_disposition == SKIP_HIBERNATE_LEG_ERROR
    ):
        pass_budget["uncertain"] = True


@dataclass(frozen=True)
class HibernatePhaseResult:
    """The typed result of running the hibernate leg under an already-held lease.

    ``consumes_budget`` is ``True`` when this phase applied a mutation OR its status is UNCERTAIN
    (an unwired leg is neither — it never touched anything, so it does not consume the pass
    budget). ``skipped_reason`` is a fixed hibernate skip token when the leg did not run to a
    typed pass result (unwired / leg error), else ``""``.
    """

    ran: bool = False
    mutations: int = 0
    attempts: tuple[dict, ...] = ()
    consumes_budget: bool = False
    skipped_reason: str = ""


def run_hibernate_phase(
    sup, ws, *, budget, renew, restrict_issues=None
) -> HibernatePhaseResult:
    """Run one workspace's hibernate leg under an ALREADY-HELD lease (no acquire / no release).

    The single shared primitive both the folded pass and the standalone seam use. ``renew`` is the
    caller's lease-renew callable (bound to this workspace/holder); ``budget`` is the pass-wide
    shared dict (``{"reads", "mutated"}``) the T2a leg threads its provider-read cap and one-
    mutation fence through. ``restrict_issues`` (review j#87154 R1-F2), when not ``None``, binds the
    leg's candidate set to exactly those issue ids — the ``local_wake`` wake-target scope; ``None``
    is the whole-workspace candidate scan (``bounded_reconciliation`` / standalone). An unwired leg
    fails closed; a raised leg is the typed uncertain status that consumes the budget (j#86739 R3-F1).
    """
    if sup._hibernate_leg_fn is None:
        return HibernatePhaseResult(skipped_reason=SKIP_HIBERNATE_UNWIRED)
    leg = sup._hibernate_leg_fn
    params = _leg_params(leg)
    kwargs: dict = {}
    if restrict_issues is not None and "restrict_issues" in params:
        kwargs["restrict_issues"] = frozenset(restrict_issues)
    try:
        if budget is not None and len(params) >= 3:
            result = leg(ws, renew, budget, **kwargs)
        else:
            result = leg(ws, renew, **kwargs)
    except Exception:  # noqa: BLE001 - one workspace's leg error never aborts the pass
        # Uncertain mutation status: consumes the budget so no blind continuation follows.
        return HibernatePhaseResult(
            consumes_budget=True, skipped_reason=SKIP_HIBERNATE_LEG_ERROR
        )
    mutations = int(result.mutations)
    return HibernatePhaseResult(
        ran=True,
        mutations=mutations,
        attempts=tuple(attempt.as_payload() for attempt in result.attempts),
        consumes_budget=mutations > 0,
    )


def hibernate_sweep(sup) -> "list[WorkspaceSupervisionOutcome]":
    """The supervisor pass's budgeted hibernate sweep (review j#86734 R2-F2/R2-F3).

    The ONE-MUTATION budget and the provider-read budget belong to the whole ``run_once`` pass,
    not to a workspace: workspaces run in DETERMINISTIC order (workspace id), every leg shares
    one read counter, and once any workspace's pass applies a mutation the remaining workspaces
    are typed-deferred WITHOUT running their legs (zero reads, zero actuation) — the next pass
    picks them up. A leg that raises consumes the budget too (its mutation status is
    UNKNOWN — review j#86739 R3-F1), so total external mutations per pass never exceed one
    even when an exception hides one.
    """
    budget: dict = {"reads": 0, "mutated": False}
    outcomes: "list[WorkspaceSupervisionOutcome]" = []
    ordered = sorted(sup._workspaces_fn(), key=lambda ws: str(ws.workspace_id or ""))
    for ws in ordered:
        if budget["mutated"]:
            outcomes.append(
                WorkspaceSupervisionOutcome(
                    workspace_id=str(ws.workspace_id or "").strip(),
                    lease_acquired=False,
                    lease_reason="",
                    skipped_reason=SKIP_HIBERNATE_BUDGET_DEFERRED,
                )
            )
            continue
        outcome = hibernate_workspace(sup, ws, budget=budget)
        if outcome.hibernate_mutations > 0 or outcome.skipped_reason == SKIP_HIBERNATE_LEG_ERROR:
            # Review j#86739 R3-F1: a leg that RAISED cannot prove it mutated nothing — the
            # public use case has post-CAS work, so the exception may be post-mutation. An
            # uncertain outcome consumes the pass budget exactly like a confirmed mutation
            # (uncertain prior action -> no blind continuation), and the remaining workspaces
            # are typed-deferred to the next pass.
            budget["mutated"] = True
        outcomes.append(outcome)
    return outcomes


def _leg_params(leg) -> "frozenset[str]":
    try:
        return frozenset(inspect.signature(leg).parameters)
    except (TypeError, ValueError):  # pragma: no cover - exotic callables fail closed
        return frozenset()


def hibernate_workspace(sup, ws, budget=None) -> WorkspaceSupervisionOutcome:
    """The standalone-seam per-workspace pass under its lease (acquire -> phase -> finally release).

    Only the standalone ``SUPERVISION_HIBERNATE`` seam uses this (the folded pass calls
    :func:`run_hibernate_phase` under a lease it already holds). It acquires the lease — the
    duplicate-supervisor fence — delegates to the SAME :func:`run_hibernate_phase` primitive, and
    releases in ``finally``.
    """
    wsid = str(ws.workspace_id or "").strip()
    if sup._hibernate_leg_fn is None:
        return WorkspaceSupervisionOutcome(
            workspace_id=wsid,
            lease_acquired=False,
            lease_reason="",
            skipped_reason=SKIP_HIBERNATE_UNWIRED,
        )
    lease = sup._lease_store.acquire(
        wsid, sup._holder, now=sup._clock(), ttl_seconds=sup._ttl
    )
    if not lease.acquired:
        return WorkspaceSupervisionOutcome(
            workspace_id=wsid,
            lease_acquired=False,
            lease_reason=lease.reason,
            skipped_reason=SKIP_LEASE_REFUSED,
        )
    try:
        def renew() -> bool:
            return bool(
                sup._lease_store.renew(
                    wsid, sup._holder, now=sup._clock(), ttl_seconds=sup._ttl
                )
            )

        phase = run_hibernate_phase(sup, ws, budget=budget, renew=renew)
        return WorkspaceSupervisionOutcome(
            workspace_id=wsid,
            lease_acquired=True,
            lease_reason=lease.reason,
            hibernate_ran=phase.ran,
            hibernate_mutations=phase.mutations,
            hibernate_attempts=phase.attempts,
            skipped_reason=phase.skipped_reason,
        )
    finally:
        if sup._release_after:
            sup._lease_store.release(wsid, sup._holder)


__all__ = (
    "HibernatePhaseResult",
    "run_hibernate_phase",
    "run_folded_hibernate",
    "mark_pass_budget",
    "hibernate_sweep",
    "hibernate_workspace",
)
