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

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_HIBERNATE_LEG_ERROR,
    SKIP_HIBERNATE_UNWIRED,
    SKIP_LEASE_REFUSED,
    WorkspaceSupervisionOutcome,
)


def hibernate_workspace(sup, ws) -> WorkspaceSupervisionOutcome:
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


__all__ = ("hibernate_workspace",)
