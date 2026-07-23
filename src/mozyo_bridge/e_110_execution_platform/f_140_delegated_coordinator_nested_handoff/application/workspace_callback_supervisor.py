"""Workspace callback supervisor composition root (Redmine #13683 Phase A).

The user-scoped single owner the issue asks for: it enumerates the **whole** workspace registry
(not the invoking repo's one workspace) and, for each workspace it can lease, drives two things per
active-lane issue:

1. **durable-event supply** — re-reads the issue's Redmine journal markers and folds them into the
   home-scoped workflow-runtime store (``append_events``), exactly as ``workflow watch`` does. This
   is what makes ``workflow glance`` / ``workflow resume`` show a real ``workflow_state`` instead of
   the ``15/15 unknown`` degrade (j#77065 acceptance 5): glance's advisory-store fallback and
   resume both read these persisted events, so source-absence is no longer treated as healthy.
2. **callback-outbox drain** — discovers fresh handoff-worthy gate candidates and runs one callback
   pass (ingest → deliver-once → sweep) **pinned to that workspace's partition**, so a shared home
   DB never lets one workspace's supervisor claim / deliver another's rows.

Everything is composed from existing single-workspace machinery — nothing about the outbox, the
processor, the wake, or the recovery reconciler changes. What is net-new is the **cross-workspace
fan-out** and the **duplicate-supervisor fence**: before touching a workspace the supervisor
acquires that workspace's durable lease (:class:`...core.state.supervisor_lease.SupervisorLeaseStore`),
so a second supervisor that loses the race skips the workspace and delivers nothing (acceptance 1).

Wake modes (:mod:`...domain.workspace_supervisor`): ``bounded_reconciliation`` re-reads every
workspace's whole active-lane roster (recovering external / MCP-only Redmine updates on the
reconciliation interval); ``local_wake`` supervises only the roster issues a mozyo-originated
gate/handoff commit named (its best-effort local wake). Both are one bounded pass — this root holds
no LLM turn and no unbounded poll; residency is the Phase B service manager's job.

All I/O is injected (the registry lister, roster resolver, Redmine source, store, outbox, sender,
lease store, clock), so the fan-out and the lease fence are deterministically testable without a
live registry, Redmine, or daemon. The production defaults are built by :func:`build_supervisor`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxRow
from mozyo_bridge.core.state.supervisor_lease import (
    SUPERVISOR_LEASE_TTL_SECONDS,
    SupervisorLeaseStore,
)
from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_DELIVERED, CALLBACK_INFLIGHT, CALLBACK_PENDING, WorkflowRuntimeStore  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    workspace_callback_drain as _drain,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    workspace_hibernate_leg as _hibernate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (
    DEFAULT_CALLBACK_ROUTE,
    discover_candidates,
    run_once,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_review_return import (
    # Redmine #13844 R7 / #13974 move-only split: the review-return owning-lane send authorities and
    # the generation-fenced discovery/send-edge helpers live in a sibling leaf so this composition
    # root stays under the module-health threshold. Re-exported here so every caller's import surface
    # (and ``__all__``) is unchanged.
    LANE_GATEWAY_OWNER_READ_ERROR,
    REVIEW_RETURN_OWNER_READ_ERROR,
    BacklogDrainOutcome,
    build_candidate_anchor_fn,
    build_supervisor_send_edge_fence,
    coordinator_target_tuple,
    discover_fenced_lane_gateway_sends,
    discover_fenced_review_returns,
    drain_review_return_backlog,
    owning_lane_binding,
    owning_lane_generation_reader,
    resolve_current_request_journal,
    resolve_current_review_identity,
    resolve_dispatch_anchor,
    resolve_lane_facts,
    review_round_send_fence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.supervisor_wiring import (
    _CountingSource,
    _NULL_SOURCE,
    _ProviderCallCounter,
    supply_events as _supply_events,
    SupervisedWorkspace,
    default_authoritative_map,
    default_background_transport,
    default_binding,
    default_lifecycle_store,
    default_redmine_source,
    default_roster,
    default_target_resolver,
    default_workspaces,
    workspace_live_inventory,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_gateway_route import (
    LANE_GATEWAY_GATES,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    OwningLaneBinding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    group_wake_hints,
    ISSUE_LEASE_LOST,
    ISSUE_PASS_ERROR,
    ISSUE_SOURCE_UNREADABLE,
    SKIP_LEASE_LOST,
    SKIP_LEASE_REFUSED,
    SKIP_NO_ACTIVE_ISSUES,
    SKIP_ROSTER_UNREADABLE,
    SUPERVISION_BOUNDED_RECONCILIATION,
    SUPERVISION_HIBERNATE,
    SUPERVISION_LOCAL_DRAIN,
    IssueSupervisionOutcome,
    SupervisorReport,
    WorkspaceSupervisionOutcome,
    fence_candidates_to_anchor,
    partition_authoritative,
    partition_delivery_receipts,
    select_supervised_issues,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ``_NullSource`` / ``_NULL_SOURCE`` (unconfigured-Redmine degrade) and ``_CountingSource`` (the
# #14150 review-F2 provider-read counter) live in the ``supervisor_wiring`` sibling and are imported
# above, so this composition root stays under the module-health threshold.
# The per-issue error tokens (``ISSUE_SOURCE_UNREADABLE`` / ``ISSUE_PASS_ERROR`` / ``ISSUE_LEASE_LOST``)
# moved to the pure domain module (#14150 module-health leaf split), imported + re-exported below so the
# public import surface (and ``__all__``) is unchanged; the drain sibling reads them from the domain too.
# ``REVIEW_RETURN_OWNER_READ_ERROR`` (#13684 R1-F3) / ``LANE_GATEWAY_OWNER_READ_ERROR`` (#13683 R2) now
# live in the sibling leaf; imported above and re-exported via ``__all__`` for a stable import surface.
# ``SupervisedWorkspace`` and the ``default_*`` production wiring live in the ``supervisor_wiring``
# sibling leaf (extracted R2 for module-health); imported + re-exported here.


class WorkspaceCallbackSupervisor:
    """Enumerate the workspace registry and, per leased workspace, supply events + drain callbacks."""

    def __init__(
        self,
        *,
        holder: str,
        lease_store: SupervisorLeaseStore,
        store: WorkflowRuntimeStore,
        outbox: CallbackOutbox,
        workspaces_fn: Callable[[], Sequence[SupervisedWorkspace]],
        roster_fn: Callable[[SupervisedWorkspace], tuple[tuple[str, ...], str]],
        redmine_source_fn: Callable[[SupervisedWorkspace], Optional[RedmineJournalSource]],
        sender_fn: Callable[[SupervisedWorkspace], Callable[[CallbackOutboxRow], str]],
        binding_fn: Optional[Callable[[SupervisedWorkspace], object]] = None,
        owner_binding_fn: Optional[
            Callable[[str, str, object], OwningLaneBinding]
        ] = None,
        wake_store: object = None,
        clock: Callable[[], str] = _utc_now_iso,
        lease_ttl_seconds: int = SUPERVISOR_LEASE_TTL_SECONDS,
        release_after: bool = True,
        callback_route: str = DEFAULT_CALLBACK_ROUTE,
        reconcile_leg_fn: Optional[Callable[[str, str, object], object]] = None,
        authoritative_fn: Optional[Callable[[], dict[str, str]]] = None,
        candidate_fence_fn: Optional[
            Callable[[str, str, Optional[RedmineJournalSource]], Optional[str]]
        ] = None,
        backlog_drain_fn: Optional[Callable[..., BacklogDrainOutcome]] = None,
        lane_generation_fn: Optional[Callable[[str, str], str]] = None,
        drain_sender_fn: Optional[
            Callable[[SupervisedWorkspace], Callable[[CallbackOutboxRow], str]]
        ] = None,
        reconcile_due_fn: Optional[Callable[[str], bool]] = None,
        reconcile_mark_fn: Optional[Callable[[str, bool], None]] = None,
        provider_counter_fn: Optional[Callable[[str], "_ProviderCallCounter"]] = None,
        reconcile_incremental_fn: Optional[
            Callable[[str, Sequence[str]], "tuple[tuple[str, ...], tuple[str, ...], Callable[[Sequence[str]], None]]"]
        ] = None,
        hibernate_leg_fn: Optional[Callable[[SupervisedWorkspace, Callable[[], bool]], object]] = None,
    ) -> None:
        holder = str(holder or "").strip()
        if not holder:
            raise ValueError(
                "WorkspaceCallbackSupervisor requires a non-empty holder identity: the lease "
                "fence is keyed on it, and a blank holder cannot fence a duplicate supervisor"
            )
        self._holder = holder
        self._lease_store = lease_store
        self._store = store
        self._outbox = outbox
        self._workspaces_fn = workspaces_fn
        self._roster_fn = roster_fn
        self._redmine_source_fn = redmine_source_fn
        self._sender_fn = sender_fn
        self._binding_fn = binding_fn
        self._owner_binding_fn = owner_binding_fn
        self._wake_store = wake_store
        self._clock = clock
        self._ttl = int(lease_ttl_seconds)
        self._release_after = bool(release_after)
        self._route = callback_route
        # The event-driven reconcile leg (Redmine #13758): run per issue after the callback
        # drain, on the same lease/wake path. Optional so the callback-only supervisor
        # (pre-#13758) is unchanged; the production leg is wired in build_supervisor.
        self._reconcile_leg_fn = reconcile_leg_fn
        # Redmine #14219 T2c: the auto-hibernate mode leg — one bounded pass per leased
        # workspace (`workspace_hibernate_leg`). Optional: unwired -> the mode fails closed.
        self._hibernate_leg_fn = hibernate_leg_fn
        # Redmine #13968 F1: the authoritative-workspace resolver — a home-global
        # ``{issue -> sole actively-owning workspace}`` map from the durable lifecycle authority.
        # When wired, each workspace supervises ONLY the issues it uniquely owns (owned-elsewhere /
        # unowned / ambiguous -> dropped, zero-ingest/zero-deliver). Optional (unit fakes unchanged).
        self._authoritative_fn = authoritative_fn
        # Redmine #13968 F2: the latest-generation dispatch-anchor resolver. Given
        # ``(workspace_id, issue, source)`` it returns the current dispatch entry journal id, or
        # ``None`` when it cannot be pinned. General callbacks on a journal OLDER than that anchor
        # are historical replay and are fenced (0-send) at both the ingest and send edges; ``None``
        # fails closed. Optional (unit fakes unchanged); the production resolver is in build_supervisor.
        self._candidate_fence_fn = candidate_fence_fn
        # Redmine #13974 R2: the own-workspace review_return backlog drain — after the active-issue pass,
        # each leased workspace converges ITS OWN pending partition (a hibernated / superseded owning lane
        # never re-supervised) through the action-time fence. Optional; production drainer in build_supervisor.
        self._backlog_drain_fn = backlog_drain_fn
        # Redmine #14150: the LOCAL owning-lane generation reader ``(workspace_id, issue) -> generation``.
        # Read from the LOCAL lifecycle authority (no provider call). Stamped on coordinator candidates at
        # ingest (so the drain can attest currency locally) and read again by the drain to fence stale
        # rows. Optional (unit fakes unchanged); the production reader is wired in build_supervisor.
        self._lane_generation_fn = lane_generation_fn
        # Redmine #14150: the provider-free sender factory for the LOCAL drain — a background-service
        # sender with NO round-fence (the only provider read in the send path), so a drain delivery makes
        # zero ticket-provider calls. Optional; when absent a drain pass delivers nothing (fail-safe).
        self._drain_sender_fn = drain_sender_fn
        # Redmine #14150 provider-reconciliation cadence: ``reconcile_due_fn(workspace_id)`` is the
        # durable watermark + jitter/backoff gate. When wired and it returns False for a workspace, the
        # bounded-reconciliation pass DOWNGRADES that workspace to a LOCAL drain (0 provider reads) —
        # so a full-roster / full-journal provider re-read is not the always-on default; a recently
        # reconciled workspace is only drained. ``reconcile_mark_fn(workspace_id, produced_new)`` advances
        # the watermark after a completed provider read (and feeds the empty-pass backoff). Both optional
        # (default None -> always due -> the pre-#14150 every-pass reconcile, unit-fake behaviour). A
        # due-check failure fails toward reconciling (never silently suppresses the provider fallback).
        self._reconcile_due_fn = reconcile_due_fn
        self._reconcile_mark_fn = reconcile_mark_fn
        # Redmine #14150 review F1: shared per-workspace provider-call counter resolver — the reconcile
        # source AND the sender's send-edge round-fence source share it, so ``provider_calls`` is the
        # ACTUAL whole-pass provider read count. Optional (None -> fresh per-pass main-source-only count).
        self._provider_counter_fn = provider_counter_fn
        # Redmine #14150 review F2: changed-work incremental-reconcile selector
        # ``(workspace_id, roster) -> (to_reconcile, skipped, commit)`` — the subset changed externally
        # (provider changed-work watermark) OR locally (per-issue snapshot) OR carrying un-accounted work
        # gets a provider read; the rest skip (drained locally); ``commit`` advances watermark + snapshots
        # on success. Bounded reconciliation only. Optional (None -> reconcile the whole roster); a failed
        # changed-work read fails OPEN in the selector (never suppresses the provider fallback).
        self._reconcile_incremental_fn = reconcile_incremental_fn

    # -- public entrypoint -------------------------------------------------

    def run_once(
        self,
        *,
        mode: str = SUPERVISION_BOUNDED_RECONCILIATION,
        wake_hints: Iterable[tuple[str, str]] = (),
    ) -> SupervisorReport:
        """Run one bounded supervised sweep across the whole workspace registry.

        For each workspace the supervisor acquires its lease (a refused lease -> the workspace is
        skipped with zero delivery — the duplicate-supervisor fence), resolves the active-lane
        roster, selects the issues for the ``mode`` (whole roster for ``bounded_reconciliation``;
        only wake-named roster issues for ``local_wake``), and per issue supplies durable events +
        drains the callback outbox partition. Returns a redaction-safe :class:`SupervisorReport`.

        Local-wake primacy (R1-F2 / R2-F2): each leased workspace drains ITS OWN durable wake
        queue **after** acquiring the lease (in :meth:`_supervise_workspace`), so only the lease
        owner ever consumes its workspace's wakes — a lease-refused duplicate supervisor never
        drains (and destroys) another owner's wakes. Explicit ``wake_hints`` are grouped by
        workspace and merged with the drained wakes. Bounded reconciliation (the whole-roster
        mode) is the loss recovery: a dropped wake is still caught because the roster is re-read.
        """
        # Redmine #14150: the LOCAL outbox drain is a distinct execution path — it reads local state
        # only (the outbox + the local lifecycle authority) and delivers already-enqueued, locally
        # attestable coordinator rows through a provider-free sender. It never resolves a Redmine
        # source, never supplies events, and never runs the reconcile leg, so an empty pass and a
        # safe-pending pass both reach the ticket provider ZERO times (close condition 1). The
        # duplicate-supervisor lease fence is unchanged (a live duplicate owner still skips).
        if mode == SUPERVISION_LOCAL_DRAIN:
            outcomes = [self._drain_workspace_locally(ws) for ws in self._workspaces_fn()]
            return SupervisorReport(
                mode=mode, holder=self._holder, workspaces=tuple(outcomes)
            )
        # Redmine #14219 T2c: the auto-hibernate leg — same early-return shape as local_drain,
        # same per-workspace lease fence, zero callback/outbox side effects.
        if mode == SUPERVISION_HIBERNATE:
            outcomes = [_hibernate.hibernate_workspace(self, ws) for ws in self._workspaces_fn()]
            return SupervisorReport(
                mode=mode, holder=self._holder, workspaces=tuple(outcomes)
            )
        wake_by_ws = group_wake_hints(wake_hints)
        # Redmine #13968 F1: resolve the authoritative-workspace map ONCE per sweep (a single
        # home-global lifecycle read), so every workspace's authoritative filter reads the same
        # durable owner snapshot. ``None`` (no resolver wired) disables the filter — the pre-#13968
        # / unit-fake behaviour. A resolver failure fails closed to an empty map (every issue then
        # has no authoritative owner -> supervised nowhere), never a crash of the sweep.
        authoritative: Optional[dict[str, str]] = None
        if self._authoritative_fn is not None:
            try:
                authoritative = dict(self._authoritative_fn() or {})
            except Exception:  # noqa: BLE001 - an owner-map read never breaks the sweep
                authoritative = {}
        outcomes: list[WorkspaceSupervisionOutcome] = []
        for ws in self._workspaces_fn():
            outcomes.append(
                self._supervise_workspace(
                    ws,
                    mode=mode,
                    wake_issues=wake_by_ws.get(ws.workspace_id, ()),
                    authoritative=authoritative,
                )
            )
        return SupervisorReport(mode=mode, holder=self._holder, workspaces=tuple(outcomes))

    def _drain_wake_for(self, workspace_id: str) -> tuple[str, ...]:
        """Consume THIS workspace's durable local wakes into issue ids (fail-open, lease-owned).

        Called only after the lease is acquired (R2-F2), so a non-owner never consumes another
        workspace's wakes. A wake-store read failure is swallowed (returns ``()``) — a lost wake is
        recovered by the bounded reconciliation pass, so the queue never breaks the sweep.
        """
        if self._wake_store is None:
            return ()
        try:
            return tuple(h.issue for h in self._wake_store.drain(workspace_id=workspace_id))
        except Exception:  # noqa: BLE001 - a wake read never breaks the sweep (reconciliation recovers)
            return ()

    # -- per-workspace -----------------------------------------------------

    def _supervise_workspace(
        self,
        ws: SupervisedWorkspace,
        *,
        mode: str,
        wake_issues: Sequence[str],
        authoritative: Optional[dict[str, str]] = None,
    ) -> WorkspaceSupervisionOutcome:
        wsid = str(ws.workspace_id or "").strip()
        # A FRESH clock per workspace (R1-F1): a single sweep-start clock would make a later
        # workspace's lease deadline stale (born-expired) after a slow earlier workspace, and its
        # takeover check compare against sweep-start time. Reading the clock at each acquire keeps
        # the lease deadline and the takeover comparison anchored to real time.
        lease = self._lease_store.acquire(
            wsid, self._holder, now=self._clock(), ttl_seconds=self._ttl
        )
        if not lease.acquired:
            # A live duplicate supervisor owns this workspace — skip it, deliver nothing.
            return WorkspaceSupervisionOutcome(
                workspace_id=wsid,
                lease_acquired=False,
                lease_reason=lease.reason,
                skipped_reason=SKIP_LEASE_REFUSED,
            )
        try:
            # R2-F2: drain this workspace's durable wakes ONLY now that we own the lease, so a
            # lease-refused duplicate can never consume another owner's wakes. Merge the
            # lease-owned drained wakes with any explicit hints for this workspace.
            wake_issues = tuple(wake_issues) + self._drain_wake_for(wsid)
            roster, roster_error = self._roster_fn(ws)
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
                self._backlog_drain_fn is not None and self._has_pending_backlog(wsid)
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
            if mode == SUPERVISION_BOUNDED_RECONCILIATION and self._reconcile_due_fn is not None:
                try:
                    reconcile_due = bool(self._reconcile_due_fn(wsid))
                except Exception:  # noqa: BLE001 - a due-check failure fails toward reconciling
                    reconcile_due = True
                if not reconcile_due:
                    drain_sender = (
                        self._drain_sender_fn(ws) if self._drain_sender_fn is not None else None
                    )
                    drain_outcomes, drain_lease_lost = self._drain_issues_from_outbox(
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
            raw_source = self._redmine_source_fn(ws)
            # Redmine #14150 review F1: count the ACTUAL provider reads via the shared counter (spans
            # the reconcile source + the sender's send-edge round-fence source). Reset per pass.
            counter = (
                self._provider_counter_fn(wsid)
                if self._provider_counter_fn is not None
                else _ProviderCallCounter()
            )
            counter.n = 0
            source = _CountingSource(raw_source, counter) if raw_source is not None else None
            sender = self._sender_fn(ws)
            binding = self._binding_fn(ws) if self._binding_fn is not None else None
            # Redmine #14150 review F2: changed-work incremental read — provider-reconcile only the
            # changed / locally-changed / has-work roster subset; skip the rest (drained locally). A
            # selector failure fails OPEN to the full roster. Bounded reconciliation only.
            reconcile_skipped: tuple[str, ...] = ()
            reconcile_commit: Optional[Callable[[Sequence[str]], None]] = None
            reconcile_targets: Sequence[str] = supervised
            if mode == SUPERVISION_BOUNDED_RECONCILIATION and self._reconcile_incremental_fn:
                try:
                    reconcile_targets, reconcile_skipped, reconcile_commit = (
                        self._reconcile_incremental_fn(wsid, supervised))
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
                if index > 0 and not self._lease_store.renew(
                    wsid, self._holder, now=self._clock(), ttl_seconds=self._ttl
                ):
                    lease_lost = True
                    break
                issue_outcome = self._supervise_issue(wsid, issue, source, sender, binding)
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
                and self._reconcile_mark_fn is not None
                and not lease_lost
            ):
                produced = any(o.events_supplied or o.delivered for o in issue_outcomes)
                try:
                    self._reconcile_mark_fn(wsid, produced)
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
            if not lease_lost and self._backlog_drain_fn is not None:
                try:
                    backlog = self._backlog_drain_fn(
                        wsid, source=source, sender=sender, skip_issues=frozenset(supervised),
                        lease_guard_fn=lambda: self._lease_store.renew(
                            wsid, self._holder, now=self._clock(), ttl_seconds=self._ttl
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
        finally:
            # A bounded run-once releases each workspace at the end of its sweep so the next
            # invocation (a fresh process, a different holder) can re-acquire; a long-lived daemon
            # passes release_after=False and renews instead. The release is token-conditional, so a
            # taken-over previous owner can never evict a new owner here.
            if self._release_after:
                self._lease_store.release(wsid, self._holder)

    def _has_pending_backlog(self, workspace_id: str) -> bool:
        """True iff THIS workspace's partition holds a drainable pending OR stale-inflight row (F1 gate)."""
        wsid = str(workspace_id or "").strip()
        try:
            return any(
                str(getattr(r, "workspace_id", "") or "").strip() == wsid
                for r in self._outbox.read(states=[CALLBACK_PENDING, CALLBACK_INFLIGHT])
            )
        except Exception:  # noqa: BLE001 - an unreadable outbox gates no drain (fail-open)
            return False

    # -- local outbox drain (Redmine #14150) -------------------------------
    # The drain leg lives in the ``workspace_callback_drain`` sibling leaf (module-health split); the
    # methods below are thin delegators so the public class surface and every call site are unchanged.

    def release_all_leases(self) -> tuple[str, ...]:
        return _drain.release_all_leases(self)

    def _drain_workspace_locally(self, ws: SupervisedWorkspace) -> WorkspaceSupervisionOutcome:
        return _drain.drain_workspace_locally(self, ws)

    def _drain_issues_from_outbox(
        self, workspace_id: str, sender: Optional[Callable[[CallbackOutboxRow], str]]
    ) -> tuple[list[IssueSupervisionOutcome], bool]:
        return _drain.drain_issues_from_outbox(self, workspace_id, sender)

    # -- per-issue ---------------------------------------------------------

    def _supervise_issue(
        self,
        workspace_id: str,
        issue: str,
        source: Optional[RedmineJournalSource],
        sender: Callable[[CallbackOutboxRow], str],
        binding: object,
    ) -> IssueSupervisionOutcome:
        """Supply durable events + drain the callback outbox for one issue (fail-open per issue).

        Any Redmine read / store error for one issue is caught and recorded as a token — one bad
        issue never aborts the workspace or the whole sweep (the outbox fence makes every pass
        idempotent, so a skipped issue loses nothing; the next sweep re-reads it).
        """
        events_supplied = 0
        error = ""
        candidates = ()
        historical_fenced = 0
        send_fence_fn = None
        review_return_refusals: tuple[str, ...] = ()
        lane_gateway_refusals: tuple[str, ...] = ()
        anchor: Optional[str] = None
        try:
            if source is not None:
                events_supplied = self._supply_events(issue, source, binding)
                # R4-F2: record the durable expected target tuple (binding-resolved coordinator
                # provider + lane) on each candidate so the delivery authority binds the live target
                # to the exact expected role, not just the anchor.
                # #13683 R2 (design answer j#82367 B): EXCLUDE the worker-produced implementation_done /
                # review_request gates from the coordinator route — they route ONLY to the same-lane
                # gateway (via discover_fenced_lane_gateway_sends below), so a coordinator candidate for
                # them would be a forbidden double wake. Conditional on the lane_gateway route being
                # active (``owner_binding_fn`` wired): without it (unit fakes / pre-#13683) the gates
                # keep their prior coordinator route, so no worker gate is silently dropped.
                lane_gateway_active = self._owner_binding_fn is not None
                target_lane, target_receiver = coordinator_target_tuple(binding, self._route)
                # Redmine #14150: stamp the coordinator candidates with the owning-lane generation read
                # from the LOCAL lifecycle authority, so the local outbox drain can later attest the row
                # is still current WITHOUT a provider read (a stale row is deferred, never blind-sent).
                enqueue_lane_gen = ""
                if self._lane_generation_fn is not None:
                    try:
                        enqueue_lane_gen = str(
                            self._lane_generation_fn(workspace_id, issue) or ""
                        ).strip()
                    except Exception:  # noqa: BLE001 - an unreadable local generation just leaves it blank
                        enqueue_lane_gen = ""
                candidates = tuple(
                    discover_candidates(
                        source, issue, route=self._route, workspace_id=workspace_id,
                        target_lane=target_lane, target_receiver=target_receiver,
                        enqueue_lane_generation=enqueue_lane_gen,
                        exclude_gates=LANE_GATEWAY_GATES if lane_gateway_active else (),
                    )
                )
                # Redmine #13968 F2: fence the GENERAL coordinator candidates to the issue's current
                # generation. ``discover_candidates`` yields a candidate for EVERY gate marker in the
                # journal, incl. historical gates from previous lane incarnations. A candidate on a
                # journal OLDER than the owning lane's current dispatch anchor is previous-generation
                # replay -> dropped (0-send); an unresolvable anchor fails closed (all dropped). The
                # review_return candidates below carry their OWN generation fence (#13684), appended
                # AFTER this fence, so they are unaffected.
                if self._candidate_fence_fn is not None:
                    anchor = self._candidate_fence_fn(workspace_id, issue, source)
                    candidates, fenced = fence_candidates_to_anchor(candidates, anchor)
                    historical_fenced = len(fenced)
                    # R2-F1 / #13974: the SAME anchor (+ the current review generation head j#81454 A
                    # AND live request j#81496 F1) also fences PRE-EXISTING pending / recovered backlog
                    # rows at the send edge — a historical coordinator row AND a previous-generation /
                    # head-drifted / req-drifted review_return row both reach a terminal disposition
                    # instead of retrying forever (the ingest fence only stops newly discovered rows).
                    review_head, review_request, review_conclusion = resolve_current_review_identity(
                        source, issue
                    )
                    # #14094: the current full-head review_request journal exempts a RESUMED-lane
                    # current-gate lane_gateway row from the unresolvable-anchor send-edge fence.
                    current_request_journal = resolve_current_request_journal(source, issue)
                    send_fence_fn = build_supervisor_send_edge_fence(
                        anchor, self._route, review_head, review_request, review_conclusion,
                        current_request_journal,
                    )
                # #13684/#13974: reserve the correlated review_result return to the issue's owning-lane
                # Codex gateway, generation-fenced. The sibling helper resolves the owning-lane binding,
                # threads the current dispatch anchor (a review round predating the current generation is
                # refused at discovery — 0-enqueue), and returns the candidates + the refusal reasons
                # (R1-F3: a fail-closed zero-send is operator-visible, not a silent drop). Fail-open per
                # issue: an owner-read failure returns nothing and leaves the coordinator candidates.
                if self._owner_binding_fn is not None:
                    return_candidates, review_return_refusals = discover_fenced_review_returns(
                        self._owner_binding_fn, source,
                        workspace_id=workspace_id, issue=issue, binding=binding,
                        fence_active=self._candidate_fence_fn is not None, anchor=anchor,
                    )
                    candidates = candidates + tuple(return_candidates)
                    # #13683 R2 (design answer j#82367): route the worker's implementation_done /
                    # review_request to its OWN owning-lane implementation_gateway, generation-fenced.
                    # Same owning-lane binding + dispatch anchor as the review_return path; a default /
                    # self / foreign / stale / no-owner / ambiguous / no-gateway / blank-generation gate
                    # is refused (0-enqueue) and surfaced. The send-edge fence built above already
                    # includes the lane_gateway route fence for pre-existing backlog rows.
                    lane_candidates, lane_gateway_refusals = discover_fenced_lane_gateway_sends(
                        self._owner_binding_fn, source,
                        workspace_id=workspace_id, issue=issue, binding=binding,
                        fence_active=self._candidate_fence_fn is not None, anchor=anchor,
                    )
                    candidates = candidates + tuple(lane_candidates)
            else:
                error = ISSUE_SOURCE_UNREADABLE
        except Exception:  # noqa: BLE001 - a source read failure degrades this issue, never the sweep
            error = ISSUE_SOURCE_UNREADABLE
            candidates = ()
            send_fence_fn = None

        # R2-F1 transient-source guard: fence active but source read failed -> the current dispatch
        # anchor could not be resolved. Skip delivery entirely rather than deliver un-fenced
        # (historical replay) or terminally fence on a transient blip (drops current rows); rows stay
        # pending for the next sweep. Only the fenced (production) supervisor is guarded.
        if self._candidate_fence_fn is not None and error:
            return IssueSupervisionOutcome(
                issue=issue, events_supplied=events_supplied, error=error,
                historical_fenced=historical_fenced,
                provider_read=source is not None,
                review_return_refusals=review_return_refusals,
                lane_gateway_refusals=lane_gateway_refusals,
            )

        # Send-boundary ownership fence (R2-F1): the source reads above can be slow enough to cross
        # the lease TTL; a takeover DURING them means we no longer own the workspace. Re-verify (and
        # extend) ownership immediately before the outbox delivery — the sole irreversible
        # side-effect (the send). If the lease was lost, skip the outbox pass entirely: zero-send,
        # and the row stays pending/claimable for the new owner (the event append above is
        # idempotent, so a late append before this fence is harmless).
        if not self._lease_store.renew(
            workspace_id, self._holder, now=self._clock(), ttl_seconds=self._ttl
        ):
            return IssueSupervisionOutcome(
                issue=issue, events_supplied=events_supplied, error=ISSUE_LEASE_LOST,
                historical_fenced=historical_fenced,
                provider_read=source is not None,
                review_return_refusals=review_return_refusals,
                lane_gateway_refusals=lane_gateway_refusals,
            )

        try:
            processor = CallbackOutboxProcessor(
                self._outbox, source or _NULL_SOURCE, workspace_id=workspace_id
            )
            # R3-F1: scope the deliver's recover + claim to THIS issue, so the issue-specific
            # dispatch-anchor fence is never applied to another issue's rows (each issue's
            # generation baseline is independent) and ``historical_fenced`` is attributed correctly.
            report = run_once(
                processor, sender, candidates=candidates, send_fence_fn=send_fence_fn, issue=issue
            )
        except Exception:  # noqa: BLE001 - a store / send failure is recorded, not fatal to the sweep
            return IssueSupervisionOutcome(
                issue=issue, events_supplied=events_supplied, error=error or ISSUE_PASS_ERROR,
                historical_fenced=historical_fenced,
                provider_read=source is not None,
                review_return_refusals=review_return_refusals,
                lane_gateway_refusals=lane_gateway_refusals,
            )

        # Event-driven reconcile leg (Redmine #13758): after the callback drain, on the same
        # lease/wake path, re-read the issue's structured gate + run one reconcile cycle
        # (turn-ended -> gate re-read -> deliver / self-heal / escalate). Fail-open — the leg
        # never aborts the sweep, and its durable effects (reconcile-state rows, outbox rows)
        # are observable via `workflow glance`. Disabled when no leg was wired.
        if self._reconcile_leg_fn is not None:
            try:
                self._reconcile_leg_fn(workspace_id, issue, source)
            except Exception:  # noqa: BLE001 - a reconcile failure never breaks the sweep
                pass

        deliver = report.get("deliver") or {}
        sweep = report.get("sweep") or {}
        # Total fenced this pass: ingest-dropped candidates + send-edge fenced backlog rows.
        historical_fenced += len(deliver.get("fenced") or [])
        # Receipt truth (Redmine #13683 R2): ``deliver["delivered"]`` is EVERY claimed row that reached
        # the send edge, NOT the rows that positively delivered. Count a row as delivered ONLY when its
        # durable ``resulting_state`` is CALLBACK_DELIVERED; a busy / ambiguous / unavailable receiver
        # held as a retryable / uncertain receipt (or a claim reconciled away mid-send) is ``blocked``,
        # so the ``delivered`` counter equals actual receiver wakes (installed a16 j#82329 divergence).
        delivered_count, blocked_count = partition_delivery_receipts(
            deliver.get("delivered") or [], delivered_state=CALLBACK_DELIVERED
        )
        return IssueSupervisionOutcome(
            issue=issue,
            events_supplied=events_supplied,
            delivered=delivered_count,
            blocked=blocked_count,
            recovered=len(deliver.get("recovered") or []),
            pending=len(sweep.get("pending") or []),
            dead_letter=len(sweep.get("dead_letter") or []),
            historical_fenced=historical_fenced,
            deferred=len(deliver.get("deferred") or []),
            provider_read=source is not None,
            error=error,
            review_return_refusals=review_return_refusals,
            lane_gateway_refusals=lane_gateway_refusals,
        )

    def _supply_events(self, issue: str, source: RedmineJournalSource, binding: object) -> int:
        """Fold one issue's Redmine markers into the runtime store — delegates to the wiring sibling."""
        return _supply_events(self._store, issue, source, binding)


def build_supervisor(
    *,
    holder: str,
    home: Optional[Path] = None,
    store_path: Optional[Path] = None,
    release_after: bool = True,
    lease_ttl_seconds: int = SUPERVISOR_LEASE_TTL_SECONDS,
    reconcile_interval_seconds: Optional[int] = None,
    reconcile_max_interval_seconds: Optional[int] = None,
) -> WorkspaceCallbackSupervisor:
    """Build the production supervisor over the home registry, shared store, and shared outbox.

    ``home`` scopes the registry / lease / store / outbox (tests pass a temp dir); ``store_path``
    overrides just the workflow-runtime DB (test/debug). The store and outbox are the shared
    home-scoped singletons — workspace partitioning happens on the outbox rows' ``workspace_id``,
    not by a per-workspace DB.
    """
    from mozyo_bridge.core.state.supervisor_lease import supervisor_lease_path
    from mozyo_bridge.core.state.supervisor_wake import SupervisorWakeStore, supervisor_wake_path
    from mozyo_bridge.core.state.workflow_runtime_store import workflow_runtime_store_path
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
        BackgroundServiceCallbackSender,
    )

    from mozyo_bridge.core.state.reconcile_state import ReconcileStateStore
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_live_source import (
        lane_worker_runtime,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_supervisor_leg import (
        build_reconcile_leg_fn,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
        markers_from_source,
    )

    resolved_store_path = store_path or workflow_runtime_store_path(home)
    lease_store = SupervisorLeaseStore(path=supervisor_lease_path(home))
    store = WorkflowRuntimeStore(path=resolved_store_path)
    outbox = CallbackOutbox(path=resolved_store_path)
    # #13758 reconcile state lives in the native state.sqlite component (home-scoped), NOT the
    # workflow-runtime DB; the reconcile leg reuses the shared outbox for delivery.
    reconcile_store = ReconcileStateStore(home=home)
    # #13684: the durable owning-lane binding authority (#13681/#13689). It supplies both the *expected*
    # generation stamped at ingest (via owner_binding_fn) and the independent *live* generation read at
    # delivery (via default_target_resolver's live_generation_fn) — the two-sided fence correction 1.
    lifecycle_store = default_lifecycle_store(home=home)

    # Redmine #14150 review F1: one shared provider-call counter per workspace — the reconcile source
    # (wrapped in the supervisor) AND the send-edge round-fence source (wrapped below) increment the
    # SAME counter, so ``provider_calls`` is the ACTUAL whole-pass provider read count. The supervisor
    # resets it at each workspace pass start.
    _ws_provider_counters: dict[str, _ProviderCallCounter] = {}

    def _counter_for(workspace_id: str) -> _ProviderCallCounter:
        return _ws_provider_counters.setdefault(str(workspace_id), _ProviderCallCounter())

    def _sender_fn(ws: SupervisedWorkspace):
        # Model A' (design answer j#77216): the supervisor delivers as a background_service
        # authority — lease + claim gated (claim re-verified against the outbox, R3-F4), target
        # re-resolved from the route ledger + live inventory (R3-F2) and bound to the row anchor
        # (R3-F3), transport origin separated from an agent send — NOT an agent handoff identity.
        # #13684: the resolver reads the independent live owning-lane generation for a return row.
        return BackgroundServiceCallbackSender(
            workspace_id=ws.workspace_id,
            holder=holder,
            lease_store=lease_store,
            target_resolver=default_target_resolver(ws, lifecycle_store=lifecycle_store),
            transport=default_background_transport(ws),
            outbox=outbox,
            # R1-F1: re-verify the review round at the send edge against the live Redmine markers.
            # home-scoped so the daemon reads credentials from the pinned mozyo home (j#79092 R2-F1).
            # #14150 review F1: the round-fence source shares the workspace provider-call counter, so
            # its send-edge journal re-reads are folded into ``provider_calls``.
            round_fence_fn=review_round_send_fence(
                lambda: _CountingSource(
                    default_redmine_source(ws, home=home), _counter_for(ws.workspace_id)
                )
            ),
        )

    def _drain_sender_fn(ws: SupervisedWorkspace):
        # Redmine #14150: the LOCAL-drain sender. Identical to _sender_fn EXCEPT it wires NO
        # round_fence_fn — the round fence (review_round_send_fence) is the only ticket-provider read in
        # the send path, so omitting it makes a drain delivery provider-free by construction. The lease /
        # claim / target-resolve / retirement gates it keeps are all LOCAL (lease store / outbox / pane
        # inventory / retire store), so a coordinator row is fully attested without any Redmine read.
        return BackgroundServiceCallbackSender(
            workspace_id=ws.workspace_id,
            holder=holder,
            lease_store=lease_store,
            target_resolver=default_target_resolver(ws, lifecycle_store=lifecycle_store),
            transport=default_background_transport(ws),
            outbox=outbox,
        )

    def _lane_generation_fn(workspace_id: str, issue: str) -> str:
        # Redmine #14150: the LOCAL owning-lane generation (no provider read). Blank when the lane is
        # unresolvable, so the drain defers the issue rather than deliver un-attested.
        lane_id, generation, _disposition = resolve_lane_facts(
            lifecycle_store, workspace_id, issue
        )
        return str(generation) if lane_id else ""

    def _owner_binding_fn(workspace_id: str, issue: str, binding: object) -> OwningLaneBinding:
        return owning_lane_binding(
            workspace_id, issue, binding, lifecycle_store=lifecycle_store
        )

    # #13758 reconcile leg / #13968 general-callback fence resolve the owning lane + generation + dispatch
    # anchor through ONE authority — the sibling-leaf resolvers (move-only from the former inline closures).
    def _lane_facts(workspace_id: str, issue: str) -> "tuple[str, int, str]":
        return resolve_lane_facts(lifecycle_store, workspace_id, issue)

    _candidate_fence_fn = build_candidate_anchor_fn(lifecycle_store)

    def _backlog_drain_fn(
        workspace_id: str, *, source, sender, skip_issues, lease_guard_fn
    ) -> BacklogDrainOutcome:
        # #13974 R2: drain the own-workspace backlog over the shared outbox + lifecycle authority.
        return drain_review_return_backlog(
            outbox, workspace_id, source=source, sender=sender,
            lifecycle_store=lifecycle_store, route=DEFAULT_CALLBACK_ROUTE,
            lease_guard_fn=lease_guard_fn, skip_issues=skip_issues,
        )

    reconcile_leg_fn = build_reconcile_leg_fn(
        reconcile_store=reconcile_store,
        outbox=outbox,
        lane_facts_fn=_lane_facts,
        markers_fn=markers_from_source,
        dispatch_anchor_fn=resolve_dispatch_anchor,
        runtime_fn=lane_worker_runtime,
    )

    # Redmine #14150 provider-reconciliation cadence: the durable per-workspace watermark + exponential
    # empty-pass backoff + jitter. A workspace inside its backoff window is downgraded to a local drain
    # (0 provider reads); a completed reconcile advances the watermark. A blank watermark reads as
    # "never reconciled -> due", so the FIRST pass over a workspace always reconciles (single run-once
    # is unchanged); only repeated passes within the window downgrade. The values are portable defaults
    # (measurement-based, not private): the reconcile interval is the coarse provider fallback cadence.
    import random as _random

    from mozyo_bridge.core.state.reconcile_cadence import ReconcileCadenceStore
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
        DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
        reconcile_backoff_seconds,
        should_reconcile_source,
    )

    base_interval = int(reconcile_interval_seconds or DEFAULT_RECONCILIATION_INTERVAL_SECONDS)
    max_interval = int(reconcile_max_interval_seconds or (base_interval * 4))
    cadence_store = ReconcileCadenceStore(home=home)

    def _reconcile_due_fn(workspace_id: str) -> bool:
        watermark = cadence_store.read(workspace_id)
        due_after = reconcile_backoff_seconds(
            base_interval, watermark.empty_passes, max_interval_seconds=max_interval,
            jitter_unit=_random.random(), jitter_fraction=0.2,
        )
        return should_reconcile_source(watermark.last_reconciled_at, _utc_now_iso(), due_after)

    def _reconcile_mark_fn(workspace_id: str, produced_new: bool) -> None:
        cadence_store.mark(workspace_id, now=_utc_now_iso(), produced=produced_new)

    # Redmine #14150 review F2: the changed-work incremental-reconcile selector (Redmine updated_on
    # adapter, fail-open, folded with local snapshots + un-accounted work) — one wiring factory.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_changed_work import (
        default_reconcile_incremental_fn,
    )

    _reconcile_incremental_fn = default_reconcile_incremental_fn(
        cadence_store=cadence_store, lifecycle_store=lifecycle_store, outbox=outbox,
        lane_facts_fn=_lane_facts,
        authoritative_map_fn=lambda: default_authoritative_map(lifecycle_store),
        home=home, now_fn=_utc_now_iso,
    )

    return WorkspaceCallbackSupervisor(
        holder=holder,
        lease_store=lease_store,
        store=store,
        outbox=outbox,
        workspaces_fn=lambda: default_workspaces(home=home),
        roster_fn=default_roster,
        redmine_source_fn=lambda ws: default_redmine_source(ws, home=home),
        sender_fn=_sender_fn,
        binding_fn=default_binding,
        owner_binding_fn=_owner_binding_fn,
        wake_store=SupervisorWakeStore(path=supervisor_wake_path(home)),
        release_after=release_after,
        lease_ttl_seconds=lease_ttl_seconds,
        reconcile_leg_fn=reconcile_leg_fn,
        authoritative_fn=lambda: default_authoritative_map(lifecycle_store),
        candidate_fence_fn=_candidate_fence_fn,
        backlog_drain_fn=_backlog_drain_fn,
        lane_generation_fn=_lane_generation_fn,
        drain_sender_fn=_drain_sender_fn,
        reconcile_due_fn=_reconcile_due_fn,
        reconcile_mark_fn=_reconcile_mark_fn,
        provider_counter_fn=lambda wsid: _counter_for(wsid),
        reconcile_incremental_fn=_reconcile_incremental_fn,
    )


__all__ = (
    "SupervisedWorkspace",
    "WorkspaceCallbackSupervisor",
    "ISSUE_SOURCE_UNREADABLE",
    "ISSUE_PASS_ERROR",
    "ISSUE_LEASE_LOST",
    "REVIEW_RETURN_OWNER_READ_ERROR",
    "review_round_send_fence",
    "default_workspaces",
    "default_roster",
    "default_authoritative_map",
    "default_redmine_source",
    "default_target_resolver",
    "default_background_transport",
    "default_lifecycle_store",
    "workspace_live_inventory",
    "coordinator_target_tuple",
    "owning_lane_binding",
    "owning_lane_generation_reader",
    "default_binding",
    "build_supervisor",
)
