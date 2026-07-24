"""The supervisor's callback/outbox delivery + reconcile legs (Redmine #14150; #14219 T3 R1-F1).

Extracted from ``workspace_callback_supervisor`` (review j#87154 R1-F1) so the folded auto-hibernate
after-leg can run under the workspace's HELD lease, BEFORE the supervisor releases it — and to keep
both modules under the module-health line ceiling. This is a pure leaf over the supervisor object:
:func:`deliver_under_lease` takes the live supervisor as ``sup`` and reads its ports/collaborators
(``sup._roster_fn``, ``sup._supervise_issue`` ...) exactly as the method did. It ACQUIRES nothing,
RELEASES nothing, and drains no wakes — the caller owns the lease lifecycle and the wake drain.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_wiring import (  # noqa: E501
    SupervisedWorkspace,
    _CountingSource,
    _ProviderCallCounter,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_review_return import (  # noqa: E501
    BacklogDrainOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    ISSUE_LEASE_LOST,
    IssueSupervisionOutcome,
    SKIP_LEASE_LOST,
    SKIP_NO_ACTIVE_ISSUES,
    SKIP_ROSTER_UNREADABLE,
    SUPERVISION_BOUNDED_RECONCILIATION,
    WorkspaceSupervisionOutcome,
    partition_authoritative,
    select_supervised_issues,
)

def deliver_under_lease(
    sup,
    ws: SupervisedWorkspace,
    wsid: str,
    lease,
    *,
    mode: str,
    wake_issues: Sequence[str],
    authoritative: Optional[dict[str, str]] = None,
) -> WorkspaceSupervisionOutcome:
    """The callback/outbox delivery + reconcile legs under the caller's HELD lease.

    Extracted (review j#87154 R1-F1) so the folded hibernate after-leg can run before the
    lease is released. No acquire, no release, no wake drain — the caller owns all three.
    """
    roster, roster_error = sup._roster_fn(ws)
    if roster_error:
        # A roster read that failed is degraded, not "nothing active" — fail closed on the
        # workspace (supervise nothing) rather than guess an empty active set.
        return WorkspaceSupervisionOutcome(
            workspace_id=wsid,
            lease_acquired=True,
            lease_reason=lease.reason,
            skipped_reason=SKIP_ROSTER_UNREADABLE,
        )
    selection = select_supervised_issues(roster, mode=mode, wake_issues=wake_issues)
    # Redmine #13968 F1: keep only the issues THIS workspace uniquely owns per the durable
    # lifecycle authority. An issue owned by another workspace, unowned, or ambiguously
    # owned is dropped and surfaced (``non_authoritative_issues``) — so the same issue is
    # never supervised (ingested / delivered) from two workspaces. When no authoritative
    # resolver is wired the roster is unchanged (pre-#13968 / unit-fake behaviour).
    if authoritative is not None:
        supervised, non_authoritative = partition_authoritative(
            selection.supervised, authoritative, wsid
        )
    else:
        supervised, non_authoritative = selection.supervised, ()
    # #13974 R2: a workspace with NO active issues but own-partition backlog (every owning lane
    # hibernated) must STILL drain it — empty roster short-circuits ONLY when nothing is drainable.
    if not supervised and not (
        sup._backlog_drain_fn is not None and sup._has_pending_backlog(wsid)
    ):
        return WorkspaceSupervisionOutcome(
            workspace_id=wsid,
            lease_acquired=True,
            lease_reason=lease.reason,
            ignored_wake_issues=selection.ignored_wake,
            non_authoritative_issues=non_authoritative,
            skipped_reason=SKIP_NO_ACTIVE_ISSUES,
        )
    # Redmine #14150 provider-reconciliation cadence: if this workspace is NOT due for a
    # provider reconcile (its durable watermark is still inside the backoff window), DOWNGRADE
    # it to a LOCAL drain this pass — zero provider reads — instead of re-reading every journal.
    # This is what stops "全workspace・全journal再読を常時の既定にする". The gate applies ONLY to
    # bounded reconciliation (local_wake / drain modes never reach here) and only when a due-fn
    # is wired; a due-check failure fails toward reconciling, never suppressing the fallback.
    if mode == SUPERVISION_BOUNDED_RECONCILIATION and sup._reconcile_due_fn is not None:
        try:
            reconcile_due = bool(sup._reconcile_due_fn(wsid))
        except Exception:  # noqa: BLE001 - a due-check failure fails toward reconciling
            reconcile_due = True
        if not reconcile_due:
            drain_sender = (
                sup._drain_sender_fn(ws) if sup._drain_sender_fn is not None else None
            )
            drain_outcomes, drain_lease_lost = sup._drain_issues_from_outbox(
                wsid, drain_sender
            )
            return WorkspaceSupervisionOutcome(
                workspace_id=wsid,
                lease_acquired=True,
                lease_reason=lease.reason,
                supervised_issues=supervised,
                ignored_wake_issues=selection.ignored_wake,
                non_authoritative_issues=non_authoritative,
                issues=tuple(drain_outcomes),
                skipped_reason=SKIP_LEASE_LOST if drain_lease_lost else "",
            )
    raw_source = sup._redmine_source_fn(ws)
    # Redmine #14150 review F1: count the ACTUAL provider reads via the shared counter (spans
    # the reconcile source + the sender's send-edge round-fence source). Reset per pass.
    counter = (
        sup._provider_counter_fn(wsid)
        if sup._provider_counter_fn is not None
        else _ProviderCallCounter()
    )
    counter.n = 0
    source = _CountingSource(raw_source, counter) if raw_source is not None else None
    sender = sup._sender_fn(ws)
    binding = sup._binding_fn(ws) if sup._binding_fn is not None else None
    # Redmine #14150 review F2: changed-work incremental read — provider-reconcile only the
    # changed / locally-changed / has-work roster subset; skip the rest (drained locally). A
    # selector failure fails OPEN to the full roster. Bounded reconciliation only.
    reconcile_skipped: tuple[str, ...] = ()
    reconcile_commit: Optional[Callable[[Sequence[str]], None]] = None
    reconcile_targets: Sequence[str] = supervised
    if mode == SUPERVISION_BOUNDED_RECONCILIATION and sup._reconcile_incremental_fn:
        try:
            reconcile_targets, reconcile_skipped, reconcile_commit = (
                sup._reconcile_incremental_fn(wsid, supervised))
        except Exception:  # noqa: BLE001 - a selector failure fails open to the full roster
            reconcile_targets, reconcile_skipped, reconcile_commit = supervised, (), None
    issue_outcomes: list[IssueSupervisionOutcome] = []
    lease_lost = False
    for index, issue in enumerate(reconcile_targets):
        # Issue-boundary renew fence (R1-F1): before each issue's side-effects (after the
        # first, which the acquire above already fenced) re-establish lease ownership with a
        # FRESH clock. renew() is holder-conditional: it returns False iff another supervisor
        # took the lease over after expiry — stop before the next issue so a stale holder
        # never delivers past a takeover. A live owner's renew also extends the deadline, so
        # a slow multi-issue sweep does not spuriously expire its own lease.
        if index > 0 and not sup._lease_store.renew(
            wsid, sup._holder, now=sup._clock(), ttl_seconds=sup._ttl
        ):
            lease_lost = True
            break
        issue_outcome = sup._supervise_issue(wsid, issue, source, sender, binding)
        issue_outcomes.append(issue_outcome)
        if issue_outcome.error == ISSUE_LEASE_LOST:
            # The send-boundary fence tripped mid-issue (a takeover during this issue's
            # source reads): the lease is gone, so stop before any further workspace work.
            lease_lost = True
            break
    # Redmine #14150: a completed provider reconcile advances this workspace's durable
    # watermark (and feeds the empty-pass backoff), so the next passes within the backoff
    # window downgrade to a local drain. ``produced`` = this pass supplied an event or
    # delivered a callback (non-empty), which resets the backoff toward the floor.
    if (
        mode == SUPERVISION_BOUNDED_RECONCILIATION
        and sup._reconcile_mark_fn is not None
        and not lease_lost
    ):
        produced = any(o.events_supplied or o.delivered for o in issue_outcomes)
        try:
            sup._reconcile_mark_fn(wsid, produced)
        except Exception:  # noqa: BLE001 - a watermark write never breaks the sweep
            pass
    # Redmine #14150 review F2: commit the changed-work cursor ONLY on a successful pass, for
    # the issues that reconciled without a source error — a transient provider failure never
    # advances the watermark past an un-read issue (recovery contract).
    if not lease_lost and reconcile_commit is not None:
        try:
            reconcile_commit([o.issue for o in issue_outcomes if not o.error])
        except Exception:  # noqa: BLE001 - a cursor commit never breaks the sweep
            pass
    # Redmine #13974 R2: while we still hold the lease, drain THIS workspace's own pending +
    # stale-inflight backlog (issues NOT in the active roster) through the same action-time fence
    # (renew guard stops before a send on takeover; own-partition only; skip supervised issues).
    # Fail-open. F4: capture ALL dispositions (delivered is a real send the report must not zero).
    backlog: Optional[BacklogDrainOutcome] = None
    if not lease_lost and sup._backlog_drain_fn is not None:
        try:
            backlog = sup._backlog_drain_fn(
                wsid, source=source, sender=sender, skip_issues=frozenset(supervised),
                lease_guard_fn=lambda: sup._lease_store.renew(
                    wsid, sup._holder, now=sup._clock(), ttl_seconds=sup._ttl
                ),
            )
            lease_lost = lease_lost or backlog.lease_lost
        except Exception:  # noqa: BLE001 - a backlog drain never breaks the sweep
            backlog = None
    return WorkspaceSupervisionOutcome(
        workspace_id=wsid,
        lease_acquired=True,
        lease_reason=lease.reason,
        supervised_issues=supervised,
        ignored_wake_issues=selection.ignored_wake,
        non_authoritative_issues=non_authoritative,
        reconcile_skipped_issues=tuple(reconcile_skipped),
        issues=tuple(issue_outcomes),
        skipped_reason=SKIP_LEASE_LOST if lease_lost else "",
        provider_calls=counter.n,
        backlog_fenced=backlog.fenced if backlog else 0,
        backlog_delivered=backlog.delivered if backlog else 0,
        backlog_blocked=backlog.blocked if backlog else 0,
        backlog_recovered=backlog.recovered if backlog else 0,
        backlog_transient_skipped=backlog.transient_skipped if backlog else 0,
    )


__all__ = ("deliver_under_lease",)
