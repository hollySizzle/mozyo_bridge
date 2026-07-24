"""Local outbox drain leg for the workspace callback supervisor (Redmine #14150).

The provider-free drain path, extracted from
:mod:`...application.workspace_callback_supervisor` as a module-health leaf (the composition root
must stay under the size threshold). These are free functions taking the supervisor ``sup`` as their
first argument and reading its already-wired collaborators (outbox / lease store / clock / drain
sender / local generation reader) — the class keeps thin delegating methods so the public surface is
unchanged.

Every function here reads LOCAL state only (the outbox + the local lifecycle authority through the
injected ``lane_generation_fn`` + the lease store) and delivers through a provider-free sender, so a
drain pass makes **zero** ticket-provider calls. A row that cannot be attested as current from local
state is DEFERRED (released back to pending) for the provider reconciliation leg — never blind-sent
and never terminally dropped.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    pass_external_budget as _pxb,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    COORDINATOR_ROUTE,
    DRAIN_DEFER_ANCHOR_UNRESOLVED,
    DRAIN_DEFER_NOT_ATTESTABLE,
    ISSUE_LEASE_LOST,
    ISSUE_PASS_ERROR,
    SKIP_LEASE_LOST,
    SKIP_LEASE_REFUSED,
    SKIP_NO_ACTIVE_ISSUES,
    SKIP_PASS_BUDGET_SPENT,
    IssueSupervisionOutcome,
    WorkspaceSupervisionOutcome,
    partition_delivery_receipts,
    select_drain_issues,
)


class _NullSource:
    """A source yielding no journal entries — the drain never reads a provider, so its processor
    is constructed against this null source (any incidental read yields nothing, never a call)."""

    def read_entries(self, issue_id: str):
        return []


_NULL_SOURCE = _NullSource()


def release_all_leases(sup) -> tuple[str, ...]:
    """Release every workspace lease ``sup``'s holder holds (Redmine #14150; token-conditional).

    The lease-lifecycle fix the live evidence (j#83437 / j#83443) requires: a bounded ``--watch`` run
    holds leases across iterations (``release_after=False``) so it keeps ownership between wakes, but
    when it TERMINATES — normal end, an exception, or a ``wake=error`` — those leases must be released,
    or the fallback ``--run-once`` starves every workspace as ``lease_held_by_other`` until the
    ~5-minute TTL. The release is token-conditional (a workspace taken over by a NEW live owner is
    never evicted), so the active-duplicate-owner fence is preserved — only THIS terminated holder's
    own leases drop. Returns the workspace ids released (fail-open per workspace).
    """
    released: list[str] = []
    try:
        workspaces = list(sup._workspaces_fn())
    except Exception:  # noqa: BLE001 - an unreadable registry releases nothing (fail-open)
        return ()
    for ws in workspaces:
        wsid = str(getattr(ws, "workspace_id", "") or "").strip()
        if not wsid:
            continue
        try:
            if sup._lease_store.release(wsid, sup._holder):
                released.append(wsid)
        except Exception:  # noqa: BLE001 - one lease release never breaks the shutdown sweep
            continue
    return tuple(released)


def drain_workspace_locally(sup, ws, *, pass_budget=None) -> WorkspaceSupervisionOutcome:
    """Drain one workspace's locally-attestable pending rows — ZERO provider reads (#14150).

    Acquires the workspace lease (the same duplicate-supervisor fence: a live duplicate owner is
    skipped, delivers nothing), reads the LOCAL outbox partition, and delivers the coordinator rows it
    can attest as current from local state. It never resolves a Redmine source, so an empty pass and a
    safe-pending pass both read the provider zero times.

    ``pass_budget`` (Final Design Disposition j#87188 = B; review j#87204 R3-F1) is the whole
    LOCAL_DRAIN pass's ONE external-mutation budget: a workspace whose pass already spent it performs
    NO send (its rows stay pending for the next pass), and within a workspace the budget-wrapped
    sender + defer fence cap the delivery at the pass's one external mutation total.
    """
    wsid = str(ws.workspace_id or "").strip()
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
        if pass_budget is not None and _pxb.budget_spent(pass_budget):
            # An earlier workspace already used the pass's one external mutation — deliver nothing
            # (this workspace's rows stay pending, delivered next pass).
            return WorkspaceSupervisionOutcome(
                workspace_id=wsid,
                lease_acquired=True,
                lease_reason=lease.reason,
                skipped_reason=SKIP_PASS_BUDGET_SPENT,
            )
        try:
            pending = sup._outbox.read(states=[CALLBACK_PENDING])
        except Exception:  # noqa: BLE001 - an unreadable outbox drains nothing (fail-open)
            pending = ()
        drain_issues = select_drain_issues(pending, wsid)
        if not drain_issues:
            return WorkspaceSupervisionOutcome(
                workspace_id=wsid,
                lease_acquired=True,
                lease_reason=lease.reason,
                skipped_reason=SKIP_NO_ACTIVE_ISSUES,
            )
        sender = sup._drain_sender_fn(ws) if sup._drain_sender_fn is not None else None
        budget_defer = None
        if pass_budget is not None:
            budget_defer = _pxb.external_budget_defer_fence(pass_budget)
            if sender is not None:
                sender = _pxb.budgeted_sender(sender, pass_budget)
        issue_outcomes, lease_lost = drain_issues_under_held_lease(
            sup, wsid, drain_issues, sender, defer_fence_fn=budget_defer
        )
        outcome = WorkspaceSupervisionOutcome(
            workspace_id=wsid,
            lease_acquired=True,
            lease_reason=lease.reason,
            supervised_issues=drain_issues,
            issues=tuple(issue_outcomes),
            skipped_reason=SKIP_LEASE_LOST if lease_lost else "",
        )
        if pass_budget is not None:
            # The budgeted sender already spent the budget on a real send; fold in a lost-lease
            # uncertain so a later workspace never continues behind an unknown external effect.
            if outcome.skipped_reason == SKIP_LEASE_LOST:
                pass_budget["uncertain"] = True
        return outcome
    finally:
        if sup._release_after:
            sup._lease_store.release(wsid, sup._holder)


def drain_issues_under_held_lease(
    sup,
    workspace_id: str,
    drain_issues: Sequence[str],
    sender: Optional[Callable[[CallbackOutboxRow], str]],
    *,
    defer_fence_fn: "Optional[Callable[[CallbackOutboxRow], tuple[bool, str]]]" = None,
) -> tuple[list[IssueSupervisionOutcome], bool]:
    """Drain the given issues under an ALREADY-HELD lease — shared by the drain mode and the
    bounded-reconciliation watermark downgrade (Redmine #14150). Returns ``(outcomes, lease_lost)``;
    a takeover between issues stops before the next send (parity with the reconciliation loop).
    ``defer_fence_fn`` (Final Design Disposition j#87188 = B) is composed with the local-attestation
    fence so a pass that already spent its one external mutation defers the rest to the next pass.
    """
    issue_outcomes: list[IssueSupervisionOutcome] = []
    lease_lost = False
    for index, issue in enumerate(drain_issues):
        if index > 0 and not sup._lease_store.renew(
            workspace_id, sup._holder, now=sup._clock(), ttl_seconds=sup._ttl
        ):
            lease_lost = True
            break
        issue_outcome = drain_issue_locally(
            sup, workspace_id, issue, sender, defer_fence_fn=defer_fence_fn
        )
        issue_outcomes.append(issue_outcome)
        if issue_outcome.error == ISSUE_LEASE_LOST:
            lease_lost = True
            break
    return issue_outcomes, lease_lost


def drain_issues_from_outbox(
    sup, workspace_id: str, sender: Optional[Callable[[CallbackOutboxRow], str]],
    *, defer_fence_fn: "Optional[Callable[[CallbackOutboxRow], tuple[bool, str]]]" = None,
) -> tuple[list[IssueSupervisionOutcome], bool]:
    """Read the LOCAL outbox and drain this workspace's attestable issues under a held lease."""
    try:
        pending = sup._outbox.read(states=[CALLBACK_PENDING])
    except Exception:  # noqa: BLE001 - an unreadable outbox drains nothing (fail-open)
        pending = ()
    drain_issues = select_drain_issues(pending, workspace_id)
    return drain_issues_under_held_lease(
        sup, workspace_id, drain_issues, sender, defer_fence_fn=defer_fence_fn
    )


def drain_issue_locally(
    sup,
    workspace_id: str,
    issue: str,
    sender: Optional[Callable[[CallbackOutboxRow], str]],
    *,
    defer_fence_fn: "Optional[Callable[[CallbackOutboxRow], tuple[bool, str]]]" = None,
) -> IssueSupervisionOutcome:
    """Deliver one issue's locally-attestable coordinator rows — ZERO provider reads (#14150).

    Reads the current owning-lane generation from the LOCAL lifecycle authority and delivers only the
    coordinator rows whose ``enqueue_lane_generation`` matches it (still-current, provider-attested-at-
    ingest and not since superseded). Every other coordinator row — a blank / mismatched generation, or
    the whole issue when the local generation is unresolvable, or when no provider-free drain sender is
    wired — is DEFERRED (released back to pending), never blind-sent and never terminally dropped: the
    provider reconciliation leg re-decides it against the durable authority.
    """
    current_gen = ""
    if sup._lane_generation_fn is not None:
        try:
            current_gen = str(sup._lane_generation_fn(workspace_id, issue) or "").strip()
        except Exception:  # noqa: BLE001 - an unreadable local generation defers the whole issue
            current_gen = ""

    def _defer_fence(row: CallbackOutboxRow) -> tuple[bool, str]:
        if sender is None:
            # No provider-free drain sender wired: defer everything (fail-safe, never blind-send).
            return (True, DRAIN_DEFER_NOT_ATTESTABLE)
        if not current_gen:
            # The current owning-lane generation could not be attested locally -> defer the issue.
            return (True, DRAIN_DEFER_ANCHOR_UNRESOLVED)
        if str(getattr(row, "enqueue_lane_generation", "") or "").strip() != current_gen:
            # Enqueued under a different (previous / blank) generation -> not locally attestable.
            return (True, DRAIN_DEFER_NOT_ATTESTABLE)
        # Final Design Disposition j#87188 = B: once the pass spent its one external mutation, defer
        # every further row (released to pending) so the next pass delivers it — never blind-sent.
        if defer_fence_fn is not None:
            deferred, reason = defer_fence_fn(row)
            if deferred:
                return (True, reason)
        return (False, "")

    # Send-boundary ownership fence (parity with _supervise_issue): re-verify + extend the lease
    # immediately before any claim / send. A takeover means zero-send; the rows stay pending.
    if not sup._lease_store.renew(
        workspace_id, sup._holder, now=sup._clock(), ttl_seconds=sup._ttl
    ):
        return IssueSupervisionOutcome(issue=issue, error=ISSUE_LEASE_LOST)

    # A sender is only ever invoked for a row _defer_fence admits; when no drain sender is wired every
    # row defers, so this fallback is never called (defensive against a future bug).
    call_sender = sender if sender is not None else (lambda row: "uncertain")
    try:
        processor = CallbackOutboxProcessor(
            sup._outbox, _NULL_SOURCE, workspace_id=workspace_id
        )
        report = processor.deliver(
            call_sender, issue=issue, route=COORDINATOR_ROUTE, defer_fence_fn=_defer_fence
        )
    except Exception:  # noqa: BLE001 - a store / send failure is recorded, not fatal to the sweep
        return IssueSupervisionOutcome(issue=issue, error=ISSUE_PASS_ERROR)

    delivered_count, blocked_count = partition_delivery_receipts(
        report.delivered, delivered_state=CALLBACK_DELIVERED
    )
    return IssueSupervisionOutcome(
        issue=issue,
        delivered=delivered_count,
        blocked=blocked_count,
        recovered=len(report.recovered),
        deferred=len(report.deferred),
        provider_read=False,
    )


__all__ = (
    "release_all_leases",
    "drain_workspace_locally",
    "drain_issues_under_held_lease",
    "drain_issues_from_outbox",
    "drain_issue_locally",
)
