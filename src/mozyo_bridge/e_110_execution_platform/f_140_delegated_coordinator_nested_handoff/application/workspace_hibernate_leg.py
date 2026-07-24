"""The supervisor's auto-hibernate mode leg (Redmine #14219 T2c).

One bounded hibernate pass per leased workspace, as a distinct ``run_once`` early-return leg —
the ``local_drain`` shape (Redmine #14150), never a second supervisor. The choreography per
workspace is exactly the lease contract the drain leg pins:

* **acquire** the workspace lease first — a refused lease (live duplicate supervisor) skips the
  workspace with ZERO actuation, the same duplicate-supervisor fence every other leg uses;
* **try**: run the wired leg function under the held lease, handing it a ``renew`` callable bound
  to THIS workspace/holder — the T2a pass renews immediately before each execute, and the use
  case's own commit-point ``lease_guard`` re-checks at the irreversible line;
* **finally release** (when the supervisor releases after passes), so a crashed pass never wedges
  the workspace: the next supervisor run re-acquires and the pass's own zero-actuation fences
  (fresh candidate re-assembly, CAS revision pins, the one-mutation budget) make the redrive safe.

An UNWIRED leg fails closed: nothing is acquired, nothing actuates, and the report says why
(``SKIP_HIBERNATE_UNWIRED``) instead of silently no-opping. A leg that RAISES is fail-open per
workspace (the sweep continues; ``SKIP_HIBERNATE_LEG_ERROR``) — parity with the per-issue pass
error token; the leg's own budget bounds any partial effect to at most the one audited mutation.
"""

from __future__ import annotations

import inspect

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_HIBERNATE_BUDGET_DEFERRED,
    SKIP_HIBERNATE_LEG_ERROR,
    SKIP_HIBERNATE_UNWIRED,
    SKIP_LEASE_REFUSED,
    WorkspaceSupervisionOutcome,
)


def hibernate_sweep(sup) -> "list[WorkspaceSupervisionOutcome]":
    """The supervisor pass's budgeted hibernate sweep (review j#86734 R2-F2/R2-F3).

    The ONE-MUTATION budget and the provider-read budget belong to the whole ``run_once`` pass,
    not to a workspace: workspaces run in DETERMINISTIC order (workspace id), every leg shares
    one read counter, and once any workspace's pass applies a mutation the remaining workspaces
    are typed-deferred WITHOUT running their legs (zero reads, zero actuation) — the next pass
    picks them up. Total external mutations per supervisor pass therefore never exceed one.
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
        if outcome.hibernate_mutations > 0:
            budget["mutated"] = True
        outcomes.append(outcome)
    return outcomes


def _leg_accepts_budget(leg) -> bool:
    try:
        return len(inspect.signature(leg).parameters) >= 3
    except (TypeError, ValueError):  # pragma: no cover - exotic callables fail closed
        return False


def hibernate_workspace(sup, ws, budget=None) -> WorkspaceSupervisionOutcome:
    """Run one workspace's bounded hibernate pass under its lease (acquire -> try -> finally)."""
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

        try:
            if budget is not None and _leg_accepts_budget(sup._hibernate_leg_fn):
                result = sup._hibernate_leg_fn(ws, renew, budget)
            else:
                result = sup._hibernate_leg_fn(ws, renew)
        except Exception:  # noqa: BLE001 - one workspace's leg error never aborts the sweep
            return WorkspaceSupervisionOutcome(
                workspace_id=wsid,
                lease_acquired=True,
                lease_reason=lease.reason,
                skipped_reason=SKIP_HIBERNATE_LEG_ERROR,
            )
        return WorkspaceSupervisionOutcome(
            workspace_id=wsid,
            lease_acquired=True,
            lease_reason=lease.reason,
            hibernate_ran=True,
            hibernate_mutations=int(result.mutations),
            hibernate_attempts=tuple(
                attempt.as_payload() for attempt in result.attempts
            ),
        )
    finally:
        if sup._release_after:
            sup._lease_store.release(wsid, sup._holder)


__all__ = ("hibernate_sweep", "hibernate_workspace")
