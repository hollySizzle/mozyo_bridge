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

import dataclasses
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxRow
from mozyo_bridge.core.state.supervisor_lease import (
    SUPERVISOR_LEASE_TTL_SECONDS,
    SupervisorLeaseStore,
)
from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (
    DEFAULT_CALLBACK_ROUTE,
    discover_candidates,
    discover_review_returns,
    run_once,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    OWNER_RESOLVED,
    OWNER_UNKNOWN,
    OwningLaneBinding,
    is_review_return_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    SKIP_LEASE_LOST,
    SKIP_LEASE_REFUSED,
    SKIP_NO_ACTIVE_ISSUES,
    SKIP_ROSTER_UNREADABLE,
    SUPERVISION_BOUNDED_RECONCILIATION,
    IssueSupervisionOutcome,
    SupervisorReport,
    WorkspaceSupervisionOutcome,
    select_supervised_issues,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _int_field(obj: object, name: str, default: int = 0) -> int:
    """Read an integer attribute, tolerant of blank / non-numeric (fail-closed to default)."""
    try:
        return int(getattr(obj, name, default) or default)
    except (TypeError, ValueError):
        return default


@dataclasses.dataclass(frozen=True)
class SupervisedWorkspace:
    """The minimal workspace facts the supervisor needs (id + canonical checkout path).

    A thin projection of :class:`...core.state.workspace_registry.WorkspaceRecord` so the roster
    resolver / source factory receive exactly what they need and nothing runtime-adjacent.
    """

    workspace_id: str
    canonical_path: str


class _NullSource:
    """A source yielding no journal entries — used when no Redmine source is configured.

    The callback drain (recover / deliver-once / sweep) still runs against it, so an unconfigured
    Redmine degrades to "drain the existing outbox" rather than skipping the workspace entirely.
    """

    def read_entries(self, issue_id: str):
        return []


_NULL_SOURCE = _NullSource()

#: A per-issue supply error token: the Redmine source could not be read for durable-event supply /
#: candidate discovery (fail-open per issue — the callback drain still ran).
ISSUE_SOURCE_UNREADABLE = "redmine_source_unreadable"
#: A per-issue error token: the whole issue pass raised (recorded, not fatal to the sweep).
ISSUE_PASS_ERROR = "issue_pass_error"
#: A per-issue error token: the send-boundary ownership fence tripped (a takeover during this
#: issue's source reads), so the outbox delivery was skipped — zero-send (Redmine #13683 R2-F1).
ISSUE_LEASE_LOST = "lease_lost_before_send"

#: A review-return refusal token (#13684 review R1-F3): resolving the owning-lane binding raised, so
#: no correlated return was reserved. Surfaced in the issue outcome so the zero-send is
#: operator-visible (secret-safe; carries no pane id / path / credential).
REVIEW_RETURN_OWNER_READ_ERROR = "owner_binding_read_error"

#: The launch-time lane-identity env a background supervisor must NOT inherit (Redmine #13683
#: R2-F3). A supervisor is not a lane agent, so carrying another lane's role / lane / workspace id
#: into a target workspace's send would misroute on a foreign identity (or, in a login service,
#: present a stale identity). These are scrubbed from the send env and only ``MOZYO_WORKSPACE_ID``
#: is re-set to the target workspace — so a herdr send that needs an attested lane-sender identity
#: fails **closed** (``missing_sender_env``) rather than misrouting on a stale ambient identity. The
#: sanctioned background system-actor sender-identity contract (a supervisor is not a claude/codex
#: lane provider, so :func:`...herdr_target_resolution.resolve_sender_identity` has no slot for it)
#: is a design-consultation seam, not resolved by ambient env.
_SCRUBBED_LANE_IDENTITY_ENV = ("MOZYO_AGENT_ROLE", "MOZYO_LANE_ID", "MOZYO_WORKSPACE_ID")


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
        wake_by_ws = _group_wake_hints(wake_hints)
        outcomes: list[WorkspaceSupervisionOutcome] = []
        for ws in self._workspaces_fn():
            outcomes.append(
                self._supervise_workspace(
                    ws, mode=mode, wake_issues=wake_by_ws.get(ws.workspace_id, ())
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
        self, ws: SupervisedWorkspace, *, mode: str, wake_issues: Sequence[str]
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
            if not selection.supervised:
                return WorkspaceSupervisionOutcome(
                    workspace_id=wsid,
                    lease_acquired=True,
                    lease_reason=lease.reason,
                    ignored_wake_issues=selection.ignored_wake,
                    skipped_reason=SKIP_NO_ACTIVE_ISSUES,
                )
            source = self._redmine_source_fn(ws)
            sender = self._sender_fn(ws)
            binding = self._binding_fn(ws) if self._binding_fn is not None else None
            issue_outcomes: list[IssueSupervisionOutcome] = []
            lease_lost = False
            for index, issue in enumerate(selection.supervised):
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
            return WorkspaceSupervisionOutcome(
                workspace_id=wsid,
                lease_acquired=True,
                lease_reason=lease.reason,
                supervised_issues=selection.supervised,
                ignored_wake_issues=selection.ignored_wake,
                issues=tuple(issue_outcomes),
                skipped_reason=SKIP_LEASE_LOST if lease_lost else "",
            )
        finally:
            # A bounded run-once releases each workspace at the end of its sweep so the next
            # invocation (a fresh process, a different holder) can re-acquire; a long-lived daemon
            # passes release_after=False and renews instead. The release is token-conditional, so a
            # taken-over previous owner can never evict a new owner here.
            if self._release_after:
                self._lease_store.release(wsid, self._holder)

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
        review_return_refusals: tuple[str, ...] = ()
        try:
            if source is not None:
                events_supplied = self._supply_events(issue, source, binding)
                # R4-F2: record the durable expected target tuple (binding-resolved coordinator
                # provider + lane) on each candidate so the delivery authority binds the live target
                # to the exact expected role, not just the anchor.
                target_lane, target_receiver = coordinator_target_tuple(binding, self._route)
                candidates = tuple(
                    discover_candidates(
                        source, issue, route=self._route, workspace_id=workspace_id,
                        target_lane=target_lane, target_receiver=target_receiver,
                    )
                )
                # #13684: reserve the correlated review_result return to the issue's owning-lane
                # Codex gateway. The pure policy (:func:`...review_return_route.plan_review_returns`)
                # consults the durable owning-lane binding + the latest-review fence; only a returnable
                # review_result becomes a candidate carrying the ``review_return:<lane>`` route (a
                # distinct outbox key) + the expected owning-lane generation. Fail-open per issue: an
                # owner-read failure leaves the coordinator candidates intact and returns nothing.
                if self._owner_binding_fn is not None:
                    try:
                        owner = self._owner_binding_fn(workspace_id, issue, binding)
                        return_candidates, plans = discover_review_returns(
                            source, issue, owner, workspace_id=workspace_id
                        )
                        candidates = candidates + tuple(return_candidates)
                        # R1-F3: surface WHY a return was refused (secret-safe reason tokens) so a
                        # fail-closed zero-send is operator-visible in the supervisor report, not a
                        # silent drop.
                        review_return_refusals = tuple(
                            p.reason for p in plans if not p.emit
                        )
                    except Exception:  # noqa: BLE001 - an owner-read failure never aborts the issue pass
                        review_return_refusals = (REVIEW_RETURN_OWNER_READ_ERROR,)
            else:
                error = ISSUE_SOURCE_UNREADABLE
        except Exception:  # noqa: BLE001 - a source read failure degrades this issue, never the sweep
            error = ISSUE_SOURCE_UNREADABLE
            candidates = ()

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
                review_return_refusals=review_return_refusals,
            )

        try:
            processor = CallbackOutboxProcessor(
                self._outbox, source or _NULL_SOURCE, workspace_id=workspace_id
            )
            report = run_once(processor, sender, candidates=candidates)
        except Exception:  # noqa: BLE001 - a store / send failure is recorded, not fatal to the sweep
            return IssueSupervisionOutcome(
                issue=issue, events_supplied=events_supplied, error=error or ISSUE_PASS_ERROR,
                review_return_refusals=review_return_refusals,
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
        return IssueSupervisionOutcome(
            issue=issue,
            events_supplied=events_supplied,
            delivered=len(deliver.get("delivered") or []),
            recovered=len(deliver.get("recovered") or []),
            pending=len(sweep.get("pending") or []),
            dead_letter=len(sweep.get("dead_letter") or []),
            error=error,
            review_return_refusals=review_return_refusals,
        )

    def _supply_events(self, issue: str, source: RedmineJournalSource, binding: object) -> int:
        """Fold one issue's Redmine journal markers into the runtime store (glance/resume supply).

        Reuses the exact ``workflow watch`` intake (:func:`evaluate_intake_from_store`) so the
        persisted events are byte-identical to a manual watch, then appends the newly accepted
        events. Idempotent: a re-read of the same journals accepts no new event (the durable
        ``redmine:<issue>:<journal>`` anchor deduplicates), so a repeated sweep supplies 0.
        Returns the number of events newly appended.
        """
        # Lazy import: the intake helper lives in the sibling watch CLI module; importing it here
        # (application -> application, same bounded context) keeps the supply identical to
        # `workflow watch` without duplicating the marker -> LaneEvent fold.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_watch import (
            evaluate_intake_from_store,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
            markers_from_source,
        )

        markers = markers_from_source(source, issue)
        if not markers:
            return 0
        outcome = evaluate_intake_from_store(self._store, markers, binding=binding)
        accepted = list(outcome.accepted_events)
        if accepted:
            self._store.append_events(dataclasses.asdict(event) for event in accepted)
        return len(accepted)


def _group_wake_hints(wake_hints: Iterable[tuple[str, str]]) -> dict[str, tuple[str, ...]]:
    """Group ``(workspace_id, issue)`` wake hints by workspace (order-preserving, de-duplicated)."""
    grouped: dict[str, list[str]] = {}
    for hint in wake_hints or ():
        try:
            wsid, issue = str(hint[0]).strip(), str(hint[1]).strip()
        except (IndexError, TypeError):
            continue
        if not wsid or not issue:
            continue
        bucket = grouped.setdefault(wsid, [])
        if issue not in bucket:
            bucket.append(issue)
    return {wsid: tuple(issues) for wsid, issues in grouped.items()}


# ---------------------------------------------------------------------------
# Production default wiring (built lazily so tests inject fakes without live adapters).
# ---------------------------------------------------------------------------


def default_workspaces(*, home: Optional[Path] = None) -> list[SupervisedWorkspace]:
    """Enumerate the home workspace registry into supervised-workspace projections."""
    from mozyo_bridge.core.state.workspace_registry import list_workspaces

    return [
        SupervisedWorkspace(
            workspace_id=str(rec.workspace_id), canonical_path=str(rec.canonical_path)
        )
        for rec in list_workspaces(home=home)
        if str(rec.workspace_id or "").strip()
    ]


def default_roster(ws: SupervisedWorkspace) -> tuple[tuple[str, ...], str]:
    """Resolve a workspace's active-lane issue set via the sublane read model (``(issues, error)``)."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (
        enumerate_active_lanes,
    )

    roster, error = enumerate_active_lanes(Path(ws.canonical_path))
    issues = tuple(
        dict.fromkeys(str(issue).strip() for issue, _lane in roster if str(issue).strip())
    )
    return issues, (error or "")


def default_redmine_source(
    ws: SupervisedWorkspace, *, home: Optional[Path] = None
) -> Optional[RedmineJournalSource]:
    """Build the live credential-gated Redmine journal source, or ``None`` when unconfigured.

    ``home`` scopes the credential root exactly like the registry / store / lease, so the launchd
    daemon (started with the ``--home`` the installer pinned) reads its Redmine credentials from the
    same mozyo home the install preflight validated — not whatever ``mozyo_bridge_home()`` a
    launchd process with no ``MOZYO_BRIDGE_HOME`` would re-derive (Redmine #13683 review j#79092
    R2-F1).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
        LiveRedmineJournalError,
        LiveRedmineJournalSource,
    )

    try:
        return LiveRedmineJournalSource.from_environment(home=home)
    except LiveRedmineJournalError:
        return None


def background_transport_env(workspace_id: str) -> dict:
    """The deterministic env for a background-service delivery subprocess (design answer j#77216).

    Model A' delivers as a ``background_service`` origin, NOT an agent: the inherited lane identity
    (``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID`` / ``MOZYO_WORKSPACE_ID``) is scrubbed so no foreign
    lane identity carries over (boundary 1), the target workspace id is pinned, and the delivery
    origin is stamped ``MOZYO_DELIVERY_ORIGIN=background_service`` so the transport is separated from
    an agent send (boundary 5). The lease + claim authority (not this env) gates the delivery.
    """
    import os

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.background_service_delivery import (
        BACKGROUND_SERVICE_ORIGIN,
    )

    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_LANE_IDENTITY_ENV}
    env["MOZYO_WORKSPACE_ID"] = str(workspace_id or "")
    env["MOZYO_DELIVERY_ORIGIN"] = BACKGROUND_SERVICE_ORIGIN
    return env


def workspace_live_inventory(ws: SupervisedWorkspace) -> "tuple[list, str]":
    """Best-effort ``(raw_inventory, backend)`` for this workspace (the live-inventory seam, R5-F1).

    Returns the workspace's **raw** backend inventory + its backend token so the resolver delegates
    the stable-key match to the one backend-neutral route authority (``resolve_route_neutral``),
    which normalizes and matches it. Herdr yields the live ``agent list`` rows + ``"herdr"``; an
    unresolved / unsupported backend yields ``([], "")`` so the resolver fail-closes (never a
    partial-key match on an unadapted inventory). Live running agents are the Phase B dogfood surface
    (#13490 / #13492); tests inject fixed ``(rows, backend)``.
    """
    try:
        import os

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
            list_herdr_agent_rows,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_entrypoint_preflight import (
            herdr_backend_active,
        )

        if herdr_backend_active(str(Path(ws.canonical_path))):
            return list(list_herdr_agent_rows(os.environ)), "herdr"
        # tmux / other backend live-inventory adaptation is the Phase B dogfood surface: the
        # resolver's unsupported-backend branch fail-closes on the empty backend token.
        return [], ""
    except Exception:  # noqa: BLE001 - inventory unavailable -> fail-closed empty
        return [], ""


#: The coordinator's durable lane in the route model (the coordinator runs in the default lane).
_COORDINATOR_LANE = "default"


def coordinator_target_tuple(binding: object, route: str) -> "tuple[str, str]":
    """The durable expected ``(lane, receiver)`` for a callback route, from the provider binding (R4-F2).

    For the coordinator route the receiver is the **binding-resolved coordinator provider**
    (``claude`` / ``codex``) and the lane is the coordinator default lane — recorded on the outbox
    row so the delivery authority binds the live target to the exact expected role (not just a lane).
    An unresolved binding (or a non-coordinator route) yields ``("", "")`` so the row's expected
    receiver is blank and the delivery fails closed (R4-F1) rather than routing to a wrong role.
    """
    if str(route or "").strip() != DEFAULT_CALLBACK_ROUTE:
        return "", ""
    provider = ""
    if binding is not None:
        try:
            provider = str(binding.provider_for("coordinator") or "").strip()
        except Exception:  # noqa: BLE001 - an unresolvable binding -> blank -> fail-closed delivery
            provider = ""
    return (_COORDINATOR_LANE, provider) if provider else ("", "")


#: The workflow role whose provider is the target-lane Codex gateway (the review_result return
#: receiver). ``project_gateway`` binds to codex in the default binding, so a return never lands on a
#: cross-lane Claude worker (design answer j#77892: coordinator -> target-lane Codex gateway only).
_GATEWAY_ROLE = "project_gateway"


def owning_lane_binding(
    workspace_id: str, issue: str, binding: object, *, lifecycle_store: object
) -> OwningLaneBinding:
    """Resolve an issue's durable owning-lane binding + generation + gateway receiver (#13684).

    The correlated-return target authority is the #13681/#13689 owning-lane binding, never a pane
    locator or an issue-id scan: :meth:`...core.state.lane_lifecycle.LaneLifecycleStore.resolve_owner`
    yields the single active owning lane (fail-closed on absent / ambiguous), and that lane's durable
    ``revision`` is the *expected* generation the outbox row records at ingest. The gateway receiver is
    the binding-resolved ``project_gateway`` provider (codex). Any store / read failure fails closed to
    :data:`OWNER_UNKNOWN` (no return), never a guess.
    """
    from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey

    wsid = str(workspace_id or "").strip()
    issue_s = str(issue or "").strip()
    try:
        owner = lifecycle_store.resolve_owner(wsid, issue_s)
    except Exception:  # noqa: BLE001 - an unreadable lifecycle store is a fail-closed unknown owner
        return OwningLaneBinding(status=OWNER_UNKNOWN)
    if not getattr(owner, "resolved", False):
        return OwningLaneBinding(status=str(getattr(owner, "status", OWNER_UNKNOWN)))
    lane = str(getattr(owner, "lane_id", "") or "").strip()
    generation = ""
    try:
        record = lifecycle_store.get(LaneLifecycleKey(wsid, lane))
        if record is not None:
            generation = str(record.revision)
    except Exception:  # noqa: BLE001 - a broken key / read is a fail-closed blank generation
        generation = ""
    gateway_receiver = ""
    if binding is not None:
        try:
            gateway_receiver = str(binding.provider_for(_GATEWAY_ROLE) or "").strip()
        except Exception:  # noqa: BLE001 - an unresolvable binding -> blank -> RETURN_NO_GATEWAY
            gateway_receiver = ""
    return OwningLaneBinding(
        status=OWNER_RESOLVED,
        lane_id=lane,
        generation=generation,
        gateway_receiver=gateway_receiver,
    )


def owning_lane_generation_reader(
    workspace_id: str, *, lifecycle_store: object
) -> "Callable[[CallbackOutboxRow], str]":
    """The independent send-time live-generation authority for a review_result return row (#13684).

    Correction 1: the delivery authority must read the live generation from an authority independent of
    the row, never copy the row's expected value. This returns the owning lane's **current** durable
    revision ONLY when (a) the row is a ``review_return:<lane>`` route and (b) the issue's current active
    owner is still exactly the row's recorded ``target_lane``. A supersession that switched the owner to
    a different lane (owner mismatch) or an absent / ambiguous / unreadable owner yields ``""`` -> a
    generation mismatch at :func:`authorize_background_delivery` -> zero-send the stale row. A same-lane
    revision bump likewise yields a live revision that differs from the row's expected -> zero-send. Any
    other route (coordinator) returns ``""`` so its Phase A fail-closed-disabled delivery is unchanged.
    """
    from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey

    wsid = str(workspace_id or "").strip()

    def _read(row: CallbackOutboxRow) -> str:
        if not is_review_return_route(str(getattr(row, "callback_route", "") or "")):
            return ""
        issue = str(getattr(row, "issue", "") or "").strip()
        target_lane = str(getattr(row, "target_lane", "") or "").strip()
        if not issue or not target_lane:
            return ""
        try:
            owner = lifecycle_store.resolve_owner(wsid, issue)
        except Exception:  # noqa: BLE001 - an unreadable owner is a blank -> fail-closed
            return ""
        if not getattr(owner, "resolved", False):
            return ""
        if str(getattr(owner, "lane_id", "") or "").strip() != target_lane:
            return ""  # a supersession switched the owner -> zero-send the stale-lane row
        try:
            record = lifecycle_store.get(LaneLifecycleKey(wsid, target_lane))
        except Exception:  # noqa: BLE001 - a broken read is a blank -> fail-closed
            return ""
        return str(record.revision) if record is not None else ""

    return _read


def review_round_send_fence(
    source_fn: Callable[[], Optional[RedmineJournalSource]]
) -> "Callable[[CallbackOutboxRow], bool]":
    """The action-time review-round fence for a review_result return row (#13684 review R1-F1).

    At the send edge the sender calls this to re-verify a correlated ``review_return`` row is STILL
    the current review round: it re-reads the issue's live structured markers through ``source_fn``
    (the ticket-provider boundary — Redmine is the authority, never a notification kind), decodes the
    row's correlated ``review_request_journal`` from its payload, and delegates to the pure
    :func:`...review_return_route.review_return_is_current`. A newer review_request / review_result /
    correction landing after the row was reserved makes it stale -> False -> the sender zero-sends. A
    non-return row is not fenced (True); an unreadable source is fail-closed (False) — a round we
    cannot re-verify is never delivered.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
        markers_from_source,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
        decode_review_return_payload,
        review_return_is_current,
    )

    def _fence(row: CallbackOutboxRow) -> bool:
        if not is_review_return_route(str(getattr(row, "callback_route", "") or "")):
            return True
        issue = str(getattr(row, "issue", "") or "").strip()
        review_journal = str(getattr(row, "journal", "") or "").strip()
        if not issue or not review_journal:
            return False
        try:
            source = source_fn()
        except Exception:  # noqa: BLE001 - an unresolvable source is a fail-closed stale round
            return False
        if source is None:
            return False
        try:
            markers = markers_from_source(source, issue)
        except Exception:  # noqa: BLE001 - an unreadable source is a fail-closed stale round
            return False
        request_journal = decode_review_return_payload(str(getattr(row, "payload", "") or ""))
        return review_return_is_current(markers, issue, review_journal, request_journal)

    return _fence


def default_lifecycle_store(*, home: Optional[Path] = None):
    """Build the home-scoped lane lifecycle store (the #13681/#13689 owning-lane binding authority)."""
    from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore

    return LaneLifecycleStore(home=home)


def default_target_resolver(ws: SupervisedWorkspace, *, lifecycle_store: object = None):
    """Build the production backend-neutral route target resolver for a workspace (R5-F1 / #13684).

    Delegates the stable-key match (``(workspace_id, lane_id, role, pane_name)``) to the ledger's
    :func:`...domain.backend_neutral_resolver.resolve_route_neutral` authority over the workspace's
    live ``(rows, backend)`` inventory (:func:`workspace_live_inventory`) — never a cached locator or
    a partial hand-rolled filter. The live running-agent surface is the Phase B dogfood (#13490).

    ``lifecycle_store`` (when supplied) wires the independent live-generation authority
    (:func:`owning_lane_generation_reader`) so the correlated review_result return route delivers under
    the generation fence; without it the resolver supplies no live generation (unchanged Phase A
    fail-closed-disabled delivery).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
        BackendNeutralTargetResolver,
    )

    live_generation_fn = None
    if lifecycle_store is not None:
        live_generation_fn = owning_lane_generation_reader(
            ws.workspace_id, lifecycle_store=lifecycle_store
        )
    return BackendNeutralTargetResolver(
        workspace_id=ws.workspace_id,
        inventory=lambda: workspace_live_inventory(ws),
        live_generation_fn=live_generation_fn,
    )


def default_background_transport(ws: SupervisedWorkspace):
    """Build the production background-service delivery transport for a workspace (boundary 5).

    Shares the handoff rail's outcome vocabulary but under a **separated origin class**: it fires
    ``mozyo-bridge handoff send`` to the **re-resolved explicit target** (never a role label) from
    the target workspace's canonical root, with the scrubbed background-service env
    (:func:`background_transport_env`). Delivery safety is the lease + claim authority (verified by
    the sender before this transport is ever called) + the outbox one-send fence, not this env. The
    subprocess runner is injectable (tests inject a fake; the live wire is the Phase B dogfood).
    """
    import subprocess

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_send_port import (
        _parse_outcome,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.handoff_callback_sender import (
        HandoffDeliveryResult,
    )

    canonical = str(ws.canonical_path)
    env = background_transport_env(ws.workspace_id)

    class _HandoffBackgroundTransport:
        def deliver(self, row, target) -> "HandoffDeliveryResult":  # noqa: F821
            argv = [
                "mozyo-bridge", "handoff", "send",
                "--to", str(target.receiver or "codex"),
                "--target", str(target.locator),  # the re-resolved explicit locator, never a label
                "--target-repo", canonical,
                "--source", "redmine",
                "--issue", str(target.issue),
                "--journal", str(target.journal),
                "--kind", "reply",
                "--mode", "standard",
                "--record-format", "json",
            ]
            try:
                proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; sanctioned handoff CLI
                    argv, capture_output=True, text=True, check=False, cwd=canonical or None, env=env
                )
            except Exception:  # noqa: BLE001 - a runner blow-up is fail-safe uncertain
                return HandoffDeliveryResult("blocked", "inject_failed")
            parsed = _parse_outcome(proc.stdout or "")
            if parsed is not None:
                return HandoffDeliveryResult(parsed[0], parsed[1])
            return HandoffDeliveryResult("blocked", "turn_start_unconfirmed")

    return _HandoffBackgroundTransport()


def default_binding(ws: SupervisedWorkspace) -> object:
    """Resolve the repo-local role->provider binding for the event-intake fold (best-effort)."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_binding_source import (
        load_workflow_binding,
    )

    try:
        binding, _warnings = load_workflow_binding(ws.canonical_path)
        return binding
    except Exception:  # noqa: BLE001 - a broken binding config folds the compatibility default
        return None


def build_supervisor(
    *,
    holder: str,
    home: Optional[Path] = None,
    store_path: Optional[Path] = None,
    release_after: bool = True,
    lease_ttl_seconds: int = SUPERVISOR_LEASE_TTL_SECONDS,
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

    from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleKey
    from mozyo_bridge.core.state.reconcile_state import ReconcileStateStore
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_live_source import (
        lane_worker_runtime,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_supervisor_leg import (
        build_reconcile_leg_fn,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
        latest_dispatch_journal,
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
            round_fence_fn=review_round_send_fence(lambda: default_redmine_source(ws, home=home)),
        )

    def _owner_binding_fn(workspace_id: str, issue: str, binding: object) -> OwningLaneBinding:
        return owning_lane_binding(
            workspace_id, issue, binding, lifecycle_store=lifecycle_store
        )

    def _lane_facts(workspace_id: str, issue: str) -> "tuple[str, int, str]":
        """Resolve ``(lane_id, live_generation, lifecycle_disposition)`` for the reconcile leg.

        The owning-lane authority (#13681/#13689) resolves the active lane; the lifecycle row's
        ``lane_generation`` (#13810 incarnation) is the reconcile generation and its
        ``lane_disposition`` gates a terminal close. An unresolved / unreadable owner is a
        fail-closed blank lane (the leg then skips). The exact dispatch anchor (review R4-F3)
        and the live runtime (review R4-F1) are read by the separate source / inventory seams.
        """
        wsid, issue_s = str(workspace_id).strip(), str(issue).strip()
        owner = lifecycle_store.resolve_owner(wsid, issue_s)
        if not getattr(owner, "resolved", False):
            return "", 0, ""
        lane_id = str(getattr(owner, "lane_id", "") or "").strip()
        record = lifecycle_store.get(LaneLifecycleKey(wsid, lane_id))
        if record is None:
            return lane_id, 0, ""
        generation = _int_field(record, "lane_generation")
        disposition = str(getattr(record, "lane_disposition", "") or "").strip()
        return lane_id, generation, disposition

    def _dispatch_anchor_fn(source: object, issue: str) -> str:
        """The EXACT workflow dispatch anchor: the latest implementation_request handoff journal.

        Read from the issue's raw handoff markers (review R4-F3) — the gate reader filters
        implementation_request out, and the lifecycle decision journal is the lane's lifecycle
        decision, not each dispatch. ``None`` / unreadable source -> blank (fail-safe baseline).
        """
        if source is None:
            return ""
        try:
            return latest_dispatch_journal(source, str(issue).strip())
        except Exception:  # noqa: BLE001 - an unreadable dispatch anchor baselines fail-safe
            return ""

    reconcile_leg_fn = build_reconcile_leg_fn(
        reconcile_store=reconcile_store,
        outbox=outbox,
        lane_facts_fn=_lane_facts,
        markers_fn=markers_from_source,
        dispatch_anchor_fn=_dispatch_anchor_fn,
        runtime_fn=lane_worker_runtime,
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
    "default_redmine_source",
    "default_target_resolver",
    "default_background_transport",
    "default_lifecycle_store",
    "workspace_live_inventory",
    "coordinator_target_tuple",
    "owning_lane_binding",
    "owning_lane_generation_reader",
    "background_transport_env",
    "default_binding",
    "build_supervisor",
)
