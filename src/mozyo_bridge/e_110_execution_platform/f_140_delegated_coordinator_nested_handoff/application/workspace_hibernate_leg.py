"""The supervisor's auto-hibernate phase (Redmine #14219 T2c leg; T3 fold, ruling j#87108).

The hibernate leg is **one leg of the existing ``run_once`` bounded pass**, folded into the
``local_wake`` (event-wake primary) and ``bounded_reconciliation`` (timer/restart fallback)
paths AFTER the callback/outbox delivery + reconcile legs, sharing the pass's ONE external
mutation budget — never a second supervisor, a second queue, or a third scheduler cadence
(Design Consultation Answer j#87108, ruling j#85459 §3).

Two entry shapes delegate to the SAME :func:`run_hibernate_phase` primitive, so the folded and
the standalone paths can never diverge:

* :func:`run_hibernate_phase` — run the wired leg under an ALREADY-HELD lease, handed a ``renew``
  callable bound to THIS workspace/holder and the pass-wide shared ``budget``. It acquires and
  releases NOTHING (the caller owns the lease); it only runs the leg and returns a typed result.
  This is what the folded ``_supervise_workspace`` calls under the lease it already holds.
* :func:`hibernate_workspace` / :func:`hibernate_sweep` — the standalone
  ``SUPERVISION_HIBERNATE`` mode: acquire the lease (the duplicate-supervisor fence), delegate to
  :func:`run_hibernate_phase`, finally release. T3 keeps this as a **production-unreachable
  internal / focused-test compatibility seam** (no CLI / event-pump / scheduler surface selects
  it — pinned by test); it delegates to the same primitive so it stays equivalent to the folded
  path.

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
    SKIP_HIBERNATE_LEG_ERROR,
    SKIP_HIBERNATE_UNWIRED,
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


def maybe_fold_hibernate(sup, mode, base_outcomes):
    """Fold the auto-hibernate after-stage into ``run_once`` for the folded modes only (j#87108).

    A no-op (returns the outcomes unchanged) when the mode is not a folded mode or no hibernate leg
    is wired; otherwise runs :func:`fold_hibernate_stage`.
    """
    if mode not in _FOLDED_MODES or sup._hibernate_leg_fn is None:
        return base_outcomes
    return fold_hibernate_stage(sup, base_outcomes)

#: The delivery-stage skip tokens that mean the pass was UNCERTAIN / fail-closed for a workspace
#: (a lost lease mid-delivery, an unreadable roster). Hibernate NEVER actuates behind one — no
#: blind continuation after an uncertain prior action (Design Consultation Answer j#87108 §2).
_UNCERTAIN_DELIVERY_SKIPS = frozenset({SKIP_LEASE_LOST, SKIP_ROSTER_UNREADABLE})


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


def run_hibernate_phase(sup, ws, *, budget, renew) -> HibernatePhaseResult:
    """Run one workspace's hibernate leg under an ALREADY-HELD lease (no acquire / no release).

    The single shared primitive both the folded pass and the standalone seam use. ``renew`` is the
    caller's lease-renew callable (bound to this workspace/holder); ``budget`` is the pass-wide
    shared dict (``{"reads", "mutated"}``) the T2a leg threads its provider-read cap and one-
    mutation fence through. An unwired leg fails closed; a raised leg is the typed uncertain status
    that consumes the budget (review j#86739 R3-F1).
    """
    if sup._hibernate_leg_fn is None:
        return HibernatePhaseResult(skipped_reason=SKIP_HIBERNATE_UNWIRED)
    try:
        if budget is not None and _leg_accepts_budget(sup._hibernate_leg_fn):
            result = sup._hibernate_leg_fn(ws, renew, budget)
        else:
            result = sup._hibernate_leg_fn(ws, renew)
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


def fold_hibernate_stage(sup, base_outcomes):
    """The AFTER-stage hibernate leg of one bounded ``run_once`` pass (Design Answer j#87108).

    Folded into the ``local_wake`` / ``bounded_reconciliation`` pass AFTER the callback/outbox
    delivery + reconcile legs, over the SAME leased workspaces, sharing ONE pass-wide external
    mutation budget — never a separate mode / cadence / queue. Deterministic (workspace-id order)
    so a re-driven pass re-attempts the same workspace. Per workspace, hibernate is a typed
    zero-actuation DEFER (it leaves the delivery outcome unchanged, ``hibernate_ran=False``) when:

    * the workspace had no lease (a duplicate-supervisor skip) — nothing to actuate under;
    * the delivery stage was UNCERTAIN (:data:`_UNCERTAIN_DELIVERY_SKIPS`) — no blind actuation
      behind a lost lease / unreadable roster;
    * a PRIOR leg mutated this pass — either this workspace's own delivery/reconcile produced a
      side effect (delivered / blocked / supplied an event: "callback/outbox delivery ... の
      優先度を先に保つ"), or an earlier workspace's hibernate already spent the pass's one
      lifecycle mutation (``budget["mutated"]``).

    Otherwise it runs the SAME :func:`hibernate_workspace` primitive under the (re-acquired,
    idempotent same-holder) lease, threading the shared ``budget``; a mutation OR an uncertain
    leg (raised) consumes the pass budget so total lifecycle mutations across ALL workspaces /
    candidates never exceed one. Returns the outcomes in their original (delivery) order with the
    hibernate fields merged in.
    """
    budget: dict = {"reads": 0, "mutated": False}
    by_id = {o.workspace_id: o for o in base_outcomes}
    merged = list(base_outcomes)
    index_of = {id(o): i for i, o in enumerate(base_outcomes)}
    for ws in sorted(sup._workspaces_fn(), key=lambda w: str(w.workspace_id or "")):
        wsid = str(ws.workspace_id or "").strip()
        base = by_id.get(wsid)
        if base is None or not base.lease_acquired:
            continue  # a lease-refused / unknown workspace never actuates
        if base.skipped_reason in _UNCERTAIN_DELIVERY_SKIPS:
            continue  # uncertain delivery -> hibernate defers (no blind continuation)
        if budget["mutated"]:
            continue  # the pass already spent its one lifecycle mutation
        if base.delivered > 0 or base.blocked > 0 or base.events_supplied > 0:
            continue  # a prior leg mutated this workspace this pass -> hibernate defers
        hib = hibernate_workspace(sup, ws, budget=budget)
        if hib.hibernate_mutations > 0 or hib.skipped_reason == SKIP_HIBERNATE_LEG_ERROR:
            # A mutation OR an uncertain (raised) leg consumes the pass-wide one-mutation budget.
            budget["mutated"] = True
        if not (hib.hibernate_ran or hib.hibernate_mutations):
            # An unwired leg / a refused re-acquire actuated nothing: leave the delivery outcome
            # untouched rather than stamping a spurious hibernate skip over it.
            continue
        merged[index_of[id(base)]] = replace(
            base,
            hibernate_ran=hib.hibernate_ran,
            hibernate_mutations=hib.hibernate_mutations,
            hibernate_attempts=hib.hibernate_attempts,
        )
    return merged


def _leg_accepts_budget(leg) -> bool:
    try:
        return len(inspect.signature(leg).parameters) >= 3
    except (TypeError, ValueError):  # pragma: no cover - exotic callables fail closed
        return False


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
    "fold_hibernate_stage",
    "maybe_fold_hibernate",
    "hibernate_sweep",
    "hibernate_workspace",
)
