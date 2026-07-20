"""Pure workspace-supervision domain: mode selection, report fold, service contract (#13683 Phase A).

The workspace callback supervisor (application composition root in
:mod:`...application.workspace_callback_supervisor`) enumerates the workspace registry and, for
each leased workspace, supplies durable workflow events + drains the callback outbox. This module
holds the **pure** decisions and shapes that root composes, kept free of any registry / Redmine /
store / lease I/O so they are deterministically testable:

- **which issues a pass supervises** (:func:`select_supervised_issues`): the two wake modes the
  design answer (j#77065) pins —
  - ``bounded_reconciliation`` re-reads the workspace's **whole** active-lane roster, so an
    external / MCP-only Redmine update (one no mozyo command emitted a local wake for) is still
    recovered on the reconciliation interval;
  - ``local_wake`` supervises only the roster issues a mozyo-originated gate/handoff commit named
    (its local wake), so a best-effort wake does the minimum work — and a wake naming an issue
    **not** in the active roster is *ignored* (surfaced, never silently trusted), because the
    roster is the authority on what is active, not the wake.
- **the redaction-safe report shapes** (:class:`IssueSupervisionOutcome` /
  :class:`WorkspaceSupervisionOutcome` / :class:`SupervisorReport`) — counts + fixed-vocabulary
  reasons only (no pane id, credential, or path), the same public-safe posture the callback
  reports keep.
- **the service lifecycle contract** (:class:`SupervisorServiceDefinition` /
  :func:`build_service_definition`): the *declarative* definition of the supervisor daemon a host
  service manager would run, carrying **no secrets** (Redmine is the durable auth authority; a
  credential is never written into a service definition or its logs — j#77065). The command is the
  bounded ``--run-once`` sweep; ``keep_alive`` is ``False`` because the sweep exits and is re-run on
  the host interval (mapping a one-shot command onto KeepAlive would be a tight restart loop —
  j#78995). The concrete macOS LaunchAgent realization (``RunAtLoad`` + ``StartInterval``, no
  KeepAlive, no ``EnvironmentVariables``) lives in Phase B1's
  ``application/supervisor_launchd.py``; this module stays pure and secret-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Supervision modes (machine-readable; literal regardless of UI language).
# ---------------------------------------------------------------------------

#: Re-read the whole active-lane roster (recovers external / MCP-only Redmine updates). This is the
#: **ticket-provider reconciliation** leg (Redmine #14150): the low-frequency bounded fallback that
#: reaches the provider to recover a lost wake / external (MCP / UI) update / service restart.
SUPERVISION_BOUNDED_RECONCILIATION = "bounded_reconciliation"
#: Supervise only the issues a mozyo-originated gate/handoff commit named (its local wake). The
#: event-driven ingest path: a fresh gate names its issue, so this pass reads the provider for that
#: ONE issue (bounded, targeted) and drains it — reaching an exactly-once callback without waiting
#: for the periodic reconcile interval (#14150 close condition 2).
SUPERVISION_LOCAL_WAKE = "local_wake"
#: The **local outbox drain** leg (Redmine #14150): read LOCAL state only (the outbox + the local
#: lifecycle/lease authority) and deliver already-enqueued, locally-attestable pending rows. A
#: drain pass makes **zero ticket-provider calls** — an empty pass and a safe-pending pass both
#: reach the provider zero times (#14150 close condition 1). A row whose current generation / owner /
#: dispatch anchor / retirement cannot be safely attested from local state is NOT blind-sent: it is
#: left for the provider reconciliation leg (:func:`select_drain_delivery_route`).
SUPERVISION_LOCAL_DRAIN = "local_drain"

SUPERVISION_MODES = frozenset(
    {SUPERVISION_BOUNDED_RECONCILIATION, SUPERVISION_LOCAL_WAKE, SUPERVISION_LOCAL_DRAIN}
)

#: Portable default bounded-reconciliation interval (seconds). Coarser than the callback wake
#: cadence — external Redmine updates are the reconciliation target, not sub-minute latency. The
#: operator tunes the concrete cadence in their runtime policy; this is only the neutral default.
DEFAULT_RECONCILIATION_INTERVAL_SECONDS = 300
#: Portable default LOCAL-drain interval (seconds). The drain reads local state only (no provider
#: load), so it can run finer than the provider reconciliation cadence to deliver already-safe
#: pending rows promptly — but it is still a bounded one-shot, never an in-turn poll. Chosen as a
#: neutral sub-multiple of the reconciliation default (not a private runtime value); the operator
#: tunes the concrete cadence in their runtime policy. Its correctness never depends on the cadence:
#: a dropped drain tick loses nothing (the provider reconciliation leg and the next drain re-read the
#: outbox), so this is a latency knob, not a safety one.
DEFAULT_LOCAL_DRAIN_INTERVAL_SECONDS = 60

#: A workspace whole-skip reason (fixed vocabulary).
SKIP_LEASE_REFUSED = "lease_held_by_other"  # a live duplicate supervisor owns this workspace
SKIP_ROSTER_UNREADABLE = "active_lane_roster_unreadable"  # the roster read failed (fail-closed)
SKIP_NO_ACTIVE_ISSUES = "no_active_issues_to_supervise"  # roster read OK but nothing to do
#: The renew fence tripped mid-sweep: this workspace's lease was lost (taken over after expiry),
#: so the supervisor stopped before the next issue's side-effects (Redmine #13683 review R1-F1).
SKIP_LEASE_LOST = "lease_lost_midsweep"

#: A per-issue supply error token: the Redmine source could not be read for durable-event supply /
#: candidate discovery (fail-open per issue — the callback drain still ran).
ISSUE_SOURCE_UNREADABLE = "redmine_source_unreadable"
#: A per-issue error token: the whole issue pass raised (recorded, not fatal to the sweep).
ISSUE_PASS_ERROR = "issue_pass_error"
#: A per-issue error token: the send-boundary ownership fence tripped (a takeover during this
#: issue's source reads), so the outbox delivery was skipped — zero-send (Redmine #13683 R2-F1).
ISSUE_LEASE_LOST = "lease_lost_before_send"


@dataclass(frozen=True)
class IssueSelection:
    """Which roster issues a pass supervises, plus local-wake hints that were ignored.

    ``supervised`` is the ordered, de-duplicated set of issues to run this pass. ``ignored_wake``
    are ``local_wake`` hints that named an issue **not** present in the active-lane roster — kept
    for the report (auditable, never silently dropped) because a wake is a hint, not the authority.
    """

    supervised: tuple[str, ...]
    ignored_wake: tuple[str, ...]


#: A supervised issue dropped because this workspace is not its authoritative owner (#13968 F1).
DROP_NOT_AUTHORITATIVE = "not_authoritative_workspace"


def authoritative_workspace_by_issue(
    active_owners: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """Map each issue to its SOLE actively-owning workspace, else omit it (Redmine #13968 F1).

    ``active_owners`` is an iterable of ``(workspace_id, issue_id)`` pairs — one per durable active
    owning lane (the lifecycle authority's active-disposition rows carrying a bound issue). An
    issue owned by **exactly one** workspace maps to that workspace; an issue owned by **zero** or
    by **two-or-more** workspaces is OMITTED — fail-closed: with no unique authoritative owner, no
    workspace supervises it (zero-ingest/zero-deliver everywhere).

    This is the cross-workspace, issue-level uniqueness the workspace-local roster filter cannot
    provide: the callback outbox partitions its UNIQUE key by ``workspace_id`` (a legal separate
    row per workspace), so the same issue appearing in two workspaces' live rosters would
    otherwise ingest + deliver twice. The durable owning-lane authority — the registry-identity
    source of truth, not the project name or a shared issue list — is what selects the one
    authoritative workspace (Redmine #13968 acceptance 1/2/5).
    """
    owners: dict[str, set[str]] = {}
    for ws, issue in active_owners:
        w = str(ws or "").strip()
        i = str(issue or "").strip()
        if not w or not i:
            continue
        owners.setdefault(i, set()).add(w)
    return {issue: next(iter(wss)) for issue, wss in owners.items() if len(wss) == 1}


def partition_authoritative(
    supervised: Iterable[str],
    authoritative_by_issue: dict[str, str],
    workspace_id: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split supervised issues into ``(kept, dropped)`` by authoritative ownership (#13968 F1).

    ``kept`` are the issues this workspace uniquely owns
    (``authoritative_by_issue[issue] == workspace_id``); ``dropped`` are the rest — owned by
    another workspace, unowned, or ambiguous (absent from the map) — which this workspace does not
    supervise. Order-preserving.
    """
    ws = str(workspace_id or "").strip()
    kept = tuple(i for i in supervised if authoritative_by_issue.get(i) == ws)
    dropped = tuple(i for i in supervised if authoritative_by_issue.get(i) != ws)
    return kept, dropped


def _as_journal_int(value: object) -> int | None:
    """Parse a Redmine journal id to ``int`` for chronological compare (``None`` if non-numeric)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def fence_candidates_to_anchor(candidates, anchor_journal):
    """Keep only candidates on a journal >= the current dispatch anchor (Redmine #13968 F2).

    ``anchor_journal`` is the current dispatch entry journal id — the owning journal of the current
    ``implementation_request`` for the issue's owning lane + generation (Redmine journal ids are
    monotonic, so a numerically OLDER candidate journal is a previous-generation gate). A candidate
    at or after the anchor is current and kept; one older than the anchor is historical and dropped
    (0-send). A ``None`` / blank / non-numeric anchor fails closed — every candidate is dropped —
    so a resolver that cannot pin the current generation never lets historical (or any) markers
    through. A candidate whose own journal is non-numeric is likewise dropped (fail-closed). Pure
    and duck-typed on ``candidate.journal``. Returns ``(kept, dropped)``, order-preserving.
    """
    anchor = _as_journal_int(anchor_journal)
    if anchor is None:
        return (), tuple(candidates)
    kept: list = []
    dropped: list = []
    for candidate in candidates:
        journal = _as_journal_int(getattr(candidate, "journal", ""))
        if journal is not None and journal >= anchor:
            kept.append(candidate)
        else:
            dropped.append(candidate)
    return tuple(kept), tuple(dropped)


def make_send_edge_fence(anchor: object, coordinator_route: str):
    """Build a per-row send-edge fence for general coordinator rows (Redmine #13968 R2-F1).

    Returns ``send_fence_fn(row) -> (fence, reason)``. A row is fenced when it is a GENERAL
    coordinator callback (its ``callback_route`` equals ``coordinator_route``) AND
    :func:`fence_candidates_to_anchor` drops it against ``anchor`` — i.e. the row's journal is older
    than the current dispatch anchor, or the anchor is unresolvable (``None`` / blank). Correlated
    ``review_return:<lane>`` rows carry their OWN generation fence (#13684) and are exempt (route
    mismatch → never fenced here). The reason token is secret-safe (no pane id / path / credential)
    so a fenced zero-send is auditable. Pure and duck-typed on ``row.callback_route`` / ``.journal``.
    """
    anchor_blank = not str(anchor or "").strip()

    def _fence(row) -> tuple[bool, str]:
        if str(getattr(row, "callback_route", "") or "").strip() != coordinator_route:
            return (False, "")  # review_return / non-coordinator: own fence, exempt
        kept, _dropped = fence_candidates_to_anchor([row], anchor)
        if kept:
            return (False, "")
        reason = (
            "fenced: dispatch anchor unresolvable"
            if anchor_blank
            else "superseded: journal older than current dispatch anchor"
        )
        return (True, reason)

    return _fence


def compose_send_edge_fences(*fences):
    """Combine per-row send-edge fences into one; the first that fires wins (pure; Redmine #13974).

    Each ``fence`` is a ``send_fence_fn(row) -> (fenced, reason)`` (or ``None``, skipped). The
    supervisor composes the coordinator-route fence (:func:`make_send_edge_fence`) with the
    review_return-route fence (:func:`...review_return_route.make_review_return_send_edge_fence`) so a
    single ``send_fence_fn`` terminally fences BOTH a historical coordinator row and a
    previous-generation review_return row in the same deliver pass. Each route-specific fence is exempt
    on the other's rows, so at most one ever fires for a given row. Returns ``(False, "")`` when no
    fence fires.
    """
    active = tuple(f for f in fences if f is not None)

    def _fence(row) -> tuple[bool, str]:
        for fence in active:
            fenced, reason = fence(row)
            if fenced:
                return (True, reason)
        return (False, "")

    return _fence


def _receipt_state(item: object) -> str:
    """Read a delivery outcome's ACTUAL persisted callback-outbox state (dict or object; pure)."""
    if isinstance(item, dict):
        return str(item.get("resulting_state", "") or "").strip()
    return str(getattr(item, "resulting_state", "") or "").strip()


def partition_delivery_receipts(
    delivery_outcomes: Iterable[object], *, delivered_state: str
) -> tuple[int, int]:
    """Split a deliver pass's per-row outcomes into ``(delivered, blocked)`` by durable receipt.

    ``delivery_outcomes`` is a ``DeliveryReport.delivered`` sequence — one entry per CLAIMED row that
    reached the send edge (as a :class:`...callback_outbox_processor.DeliveryOutcome` object OR its
    ``as_payload()`` dict). Crucially this list is NOT "the rows that were delivered": it carries every
    claimed row's terminal outcome regardless of whether the receiver was woken. A row counts as
    ``delivered`` ONLY when its **actual persisted** ``resulting_state`` is ``delivered_state`` (the
    outbox ``CALLBACK_DELIVERED`` terminal — a positively-submitted send). Every other outcome — a
    busy / ambiguous / unavailable receiver held as a retryable (``retry`` / re-``pending``) row, a
    post-injection ``uncertain`` receipt, or a claim whose lease expired mid-send and was reconciled
    away (``ownership_lost``) — is a NON-delivery counted as ``blocked``: a receipt held, not a wake.

    This is the receipt-truth binding (Redmine #13683 R2): the supervisor's ``delivered`` projection
    must equal actual receiver wakes, never the count of send *attempts*. Counting ``len(delivered)``
    reported a busy-receiver zero-send as a delivery — the ``delivered`` counter diverging from the
    receiver's durable state (installed a16 j#82329). Pure and duck-typed; returns ``(delivered,
    blocked)`` with ``delivered + blocked == len(delivery_outcomes)``.
    """
    delivered = 0
    blocked = 0
    for item in delivery_outcomes or ():
        if _receipt_state(item) == delivered_state:
            delivered += 1
        else:
            blocked += 1
    return delivered, blocked


# ---------------------------------------------------------------------------
# Local outbox drain: locally-attestable route selection (Redmine #14150).
# ---------------------------------------------------------------------------

#: The coordinator callback route (a sublane callback wakes the coordinator lane). Mirrors
#: ``...application.callback_runtime.DEFAULT_CALLBACK_ROUTE`` — the pure layer keeps its own literal
#: so it never imports the application layer.
COORDINATOR_ROUTE = "coordinator"

#: The routes the LOCAL drain may deliver **without any ticket-provider read** (Redmine #14150). Only
#: the general coordinator route qualifies in R1: its safety fence is the dispatch-anchor generation
#: check, which reads the LOCAL lifecycle authority (``make_send_edge_fence``), and it carries no
#: generation-correlation that the delivery authority would have to re-verify against a live provider
#: round. The correlated ``review_return:<lane>`` route needs the send-edge review-round re-read (a
#: provider call), and the ``lane_gateway:<lane>`` route needs its live owning-lane generation, so
#: both are NOT locally attestable — a drain pass DEFERS them to the provider reconciliation leg
#: rather than blind-send (the issue's "blind sendせず … reconciliation要求へ倒す" contract).
LOCAL_DRAIN_ATTESTABLE_ROUTES = frozenset({COORDINATOR_ROUTE})

#: A drain-deferred row reason (fixed vocabulary): the row's route is not locally attestable, so the
#: local drain left it for the provider reconciliation leg (zero-send, never a blind send).
DRAIN_DEFER_NOT_ATTESTABLE = "deferred_not_locally_attestable"
#: A drain-deferred issue reason: the issue's current dispatch anchor could not be resolved from the
#: LOCAL lifecycle authority, so the drain delivered none of its rows (fail-closed, deferred to the
#: provider reconciliation leg) rather than deliver un-anchored (a possible previous-generation replay).
DRAIN_DEFER_ANCHOR_UNRESOLVED = "deferred_local_anchor_unresolved"


def is_locally_attestable_route(route: object) -> bool:
    """True iff ``route`` can be safely delivered by the local drain (Redmine #14150; pure).

    Only :data:`LOCAL_DRAIN_ATTESTABLE_ROUTES` qualify — every other route requires a live
    ticket-provider read to attest (review-round currency / live owning-lane generation) and is
    deferred to the provider reconciliation leg instead of blind-sent.
    """
    return str(route or "").strip() in LOCAL_DRAIN_ATTESTABLE_ROUTES


def select_drain_issues(pending_rows: Iterable[object], workspace_id: str) -> tuple[str, ...]:
    """The ordered, de-duplicated issues with a locally-attestable pending row (Redmine #14150; pure).

    ``pending_rows`` are the workspace's LOCAL callback-outbox rows (duck-typed on ``.issue`` /
    ``.callback_route`` / ``.workspace_id``). The drain visits only issues that own at least one
    locally-attestable pending row **in this workspace's partition**, so a pass with no drainable row
    visits nothing (and reads the provider zero times). Order-preserving on first appearance.
    """
    ws = str(workspace_id or "").strip()
    issues: list[str] = []
    seen: set[str] = set()
    for row in pending_rows or ():
        if str(getattr(row, "workspace_id", "") or "").strip() != ws:
            continue
        if not is_locally_attestable_route(getattr(row, "callback_route", "")):
            continue
        issue = str(getattr(row, "issue", "") or "").strip()
        if not issue or issue in seen:
            continue
        seen.add(issue)
        issues.append(issue)
    return tuple(issues)


def fence_candidates_after_cursor(candidates, cursor):
    """Bound candidate DISCOVERY to events newer than the durable event cursor (Redmine #14150 F3; pure).

    ``cursor`` is the highest source journal id already folded for the issue (blank / non-numeric ->
    first pass, keep everything). Returns ``(fresh, next_cursor)``: ``fresh`` is the candidates on a
    journal strictly newer than ``cursor`` (an incremental read after the stored cursor); a candidate
    with a non-numeric journal is kept (fail-open toward delivery — the generation fence + outbox
    UNIQUE key remain the correctness authority, not the cursor). ``next_cursor`` is the max of
    ``cursor`` and every candidate journal seen this pass, as a string, so the caller advances the
    durable cursor past all discovered events on a successful pass. Order-preserving. Because a newer
    gate always has a higher journal id, an over-advance never drops a future gate — the cursor only
    filters re-discovery of already-folded events.
    """
    cur = _as_journal_int(cursor)
    fresh: list = []
    max_seen = cur
    for candidate in candidates:
        journal = _as_journal_int(getattr(candidate, "journal", ""))
        if journal is None:
            fresh.append(candidate)  # non-numeric journal: cannot cursor-filter -> keep (fail-open)
            continue
        if max_seen is None or journal > max_seen:
            max_seen = journal
        if cur is None or journal > cur:
            fresh.append(candidate)
    next_cursor = str(max_seen) if max_seen is not None else str(cursor or "")
    return tuple(fresh), next_cursor


def select_supervised_issues(
    roster_issues: Iterable[str],
    *,
    mode: str,
    wake_issues: Iterable[str] = (),
) -> IssueSelection:
    """Decide which of a workspace's active-lane issues this pass supervises (pure, fail-closed).

    ``bounded_reconciliation`` returns the whole (de-duplicated, order-preserving) roster.
    ``local_wake`` returns the **intersection** of the roster and ``wake_issues`` — a wake for a
    non-active / retired / foreign issue is dropped into ``ignored_wake`` rather than trusted, so
    the roster stays the authority on what is active. An unrecognized ``mode`` falls back to
    bounded reconciliation (never silently supervises nothing).
    """
    roster = tuple(dict.fromkeys(str(i).strip() for i in roster_issues if str(i).strip()))
    if mode != SUPERVISION_LOCAL_WAKE:
        return IssueSelection(supervised=roster, ignored_wake=())
    roster_set = set(roster)
    wanted = tuple(dict.fromkeys(str(w).strip() for w in wake_issues if str(w).strip()))
    supervised = tuple(i for i in roster if i in set(wanted))
    ignored = tuple(w for w in wanted if w not in roster_set)
    return IssueSelection(supervised=supervised, ignored_wake=ignored)


# ---------------------------------------------------------------------------
# Report shapes (redaction-safe: counts + fixed-vocabulary reasons only).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueSupervisionOutcome:
    """One supervised issue's result: durable-event supply + callback-outbox drain counts.

    ``events_supplied`` is the number of normalized workflow events appended to the runtime store
    (the glance/resume durable-state supply — j#77065 acceptance 5). ``delivered`` / ``recovered``
    / ``pending`` / ``dead_letter`` are the callback-outbox pass counts. ``error`` is a
    fixed-vocabulary token when this issue's pass failed (fail-open per issue — one issue's Redmine
    read / store error never aborts the workspace or the sweep).
    """

    issue: str
    events_supplied: int = 0
    delivered: int = 0
    #: Claimed callback rows that reached the send edge but did NOT positively deliver (Redmine #13683
    #: R2): a busy / ambiguous / unavailable receiver held as a retryable / uncertain receipt, or a
    #: claim reconciled away mid-send. Counted separately so ``delivered`` equals actual receiver wakes
    #: and a non-wake is operator-visible, never silently folded into the delivered count.
    blocked: int = 0
    recovered: int = 0
    pending: int = 0
    dead_letter: int = 0
    #: General callback candidates dropped by the latest-generation dispatch-anchor fence (#13968
    #: F2): historical (previous-generation) gate journals that would otherwise re-enqueue and
    #: re-deliver. Surfaced so a fenced zero-send is operator-visible, not a silent drop.
    historical_fenced: int = 0
    #: Rows the LOCAL drain could not attest as current from local state and released back to pending
    #: for the provider reconciliation leg (Redmine #14150): a zero-send that is neither a blind send
    #: nor a terminal drop. Surfaced so a deferred-to-reconciliation outcome is operator-visible.
    deferred: int = 0
    #: Whether this issue's pass performed a ticket-provider (Redmine) read (Redmine #14150). The
    #: LOCAL drain never reads the provider, so ``provider_read`` is ``False`` for every drain issue —
    #: the report's ``provider_calls`` roll-up is then provably ``0`` for an empty / safe-pending drain
    #: pass (close condition 1). The provider reconciliation leg sets it ``True`` only for issues it
    #: actually read (a watermark-skipped issue stays ``False``), so the count is real provider work.
    provider_read: bool = False
    error: str = ""
    #: The fixed-vocabulary review_result-return refusal reasons for this issue (#13684 review R1-F3):
    #: why a correlated return was NOT reserved (missing / ambiguous owner, self-route, stale, blank
    #: generation, uncorrelated). Secret-safe reason tokens only (no pane id / path / credential), so
    #: an operator sees a fail-closed zero-send is a deliberate refusal, not a silent drop.
    review_return_refusals: tuple[str, ...] = ()
    #: The fixed-vocabulary same-lane-gateway routing refusal reasons for this issue (Redmine #13683 R2):
    #: why a worker's implementation_done / review_request was NOT routed to its owning-lane gateway
    #: (no active owner, ambiguous, coordinator self-route, no gateway, blank / previous generation).
    #: Secret-safe reason tokens only, so a fail-closed zero-send is an operator-visible deliberate
    #: refusal (design answer j#82367), not a silent drop.
    lane_gateway_refusals: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        return {
            "issue": self.issue,
            "events_supplied": self.events_supplied,
            "delivered": self.delivered,
            "blocked": self.blocked,
            "recovered": self.recovered,
            "pending": self.pending,
            "dead_letter": self.dead_letter,
            "historical_fenced": self.historical_fenced,
            "deferred": self.deferred,
            "provider_read": self.provider_read,
            "error": self.error,
            "review_return_refusals": list(self.review_return_refusals),
            "lane_gateway_refusals": list(self.lane_gateway_refusals),
        }


@dataclass(frozen=True)
class WorkspaceSupervisionOutcome:
    """One workspace's supervision result (lease decision + per-issue outcomes)."""

    workspace_id: str
    lease_acquired: bool
    lease_reason: str
    supervised_issues: tuple[str, ...] = ()
    ignored_wake_issues: tuple[str, ...] = ()
    #: Roster issues dropped because this workspace is not their authoritative owner (#13968 F1):
    #: another workspace owns the issue, or ownership is absent / ambiguous. Surfaced (not silently
    #: dropped) so a fail-closed zero-supervise is operator-visible in the report.
    non_authoritative_issues: tuple[str, ...] = ()
    issues: tuple[IssueSupervisionOutcome, ...] = ()
    skipped_reason: str = ""
    #: The ACTUAL ticket-provider read count for this workspace (Redmine #14150 review F2): the number
    #: of ``read_entries`` (one HTTP fetch each) the reconcile source served this pass — supply +
    #: discovery + dispatch-anchor + review-identity + review_return / lane_gateway discovery +
    #: own-workspace backlog drain. NOT the count of issues that touched the provider (which
    #: under-counted a multi-read issue as 1). A local drain / downgraded workspace reads 0.
    provider_calls: int = 0
    #: Own-workspace review_return backlog dispositions (Redmine #13974 R2): rows reserved for a
    #: now-hibernated / superseded lane whose issue is no longer in any active roster, drained under the
    #: lease. ``backlog_fenced`` terminally converged (zero-send); ``backlog_delivered`` is a REAL send
    #: side effect (review F4 — it must be rolled into ``delivered``, never reported as 0);
    #: ``backlog_recovered`` reconciled a stale crashed inflight (review F1); ``backlog_transient_skipped``
    #: was left pending because the provider was unreadable. All surfaced so no side effect is invisible.
    backlog_fenced: int = 0
    backlog_delivered: int = 0
    #: Backlog-drain claimed rows that reached the send edge but did NOT positively deliver (Redmine
    #: #13683 R2): the receipt-truth counterpart of ``backlog_delivered`` (busy / uncertain / reconciled
    #: mid-send), rolled into the workspace ``blocked`` so a non-wake in the backlog drain is visible.
    backlog_blocked: int = 0
    backlog_recovered: int = 0
    backlog_transient_skipped: int = 0

    @property
    def events_supplied(self) -> int:
        return sum(i.events_supplied for i in self.issues)

    @property
    def delivered(self) -> int:
        # F4: a backlog-drain send is a real delivery — roll it in so the report never under-counts it.
        return sum(i.delivered for i in self.issues) + self.backlog_delivered

    @property
    def blocked(self) -> int:
        # Receipt truth (Redmine #13683 R2): non-delivering claimed rows across the active-issue pass
        # AND the backlog drain, so ``delivered`` never absorbs a send that did not wake the receiver.
        return sum(i.blocked for i in self.issues) + self.backlog_blocked

    @property
    def deferred(self) -> int:
        # Redmine #14150: rows the local drain released back to pending for the reconciliation leg.
        return sum(i.deferred for i in self.issues)

    @property
    def provider_read_issues(self) -> int:
        # Redmine #14150: how many issues touched the provider this pass (observability alongside the
        # ACTUAL ``provider_calls`` count — an issue can make several reads, so these differ by design).
        return sum(1 for i in self.issues if i.provider_read)

    def as_payload(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "lease_acquired": self.lease_acquired,
            "lease_reason": self.lease_reason,
            "supervised_issues": list(self.supervised_issues),
            "ignored_wake_issues": list(self.ignored_wake_issues),
            "non_authoritative_issues": list(self.non_authoritative_issues),
            "skipped_reason": self.skipped_reason,
            "events_supplied": self.events_supplied,
            "delivered": self.delivered,
            "blocked": self.blocked,
            "deferred": self.deferred,
            "provider_calls": self.provider_calls,
            "provider_read_issues": self.provider_read_issues,
            "backlog_fenced": self.backlog_fenced,
            "backlog_delivered": self.backlog_delivered,
            "backlog_blocked": self.backlog_blocked,
            "backlog_recovered": self.backlog_recovered,
            "backlog_transient_skipped": self.backlog_transient_skipped,
            "issues": [i.as_payload() for i in self.issues],
        }


@dataclass(frozen=True)
class SupervisorReport:
    """A whole run-once supervised sweep: per-workspace outcomes + roll-up counts."""

    mode: str
    holder: str
    workspaces: tuple[WorkspaceSupervisionOutcome, ...] = field(default_factory=tuple)
    #: Wall-clock milliseconds the whole sweep took (Redmine #14150 observability). Set by the CLI
    #: entrypoint (a clock read is impure, so the pure fold defaults it to 0). For a provider
    #: reconciliation pass this is the reconcile duration the close condition asks be measurable.
    duration_ms: int = 0

    @property
    def workspaces_supervised(self) -> int:
        return sum(1 for w in self.workspaces if w.lease_acquired)

    @property
    def workspaces_skipped(self) -> int:
        return sum(1 for w in self.workspaces if not w.lease_acquired)

    @property
    def events_supplied(self) -> int:
        return sum(w.events_supplied for w in self.workspaces)

    @property
    def delivered(self) -> int:
        return sum(w.delivered for w in self.workspaces)

    @property
    def blocked(self) -> int:
        return sum(w.blocked for w in self.workspaces)

    @property
    def deferred(self) -> int:
        # Redmine #14150: rows the local drain deferred to the provider reconciliation leg.
        return sum(w.deferred for w in self.workspaces)

    @property
    def provider_calls(self) -> int:
        """Ticket-provider reads this whole sweep performed (Redmine #14150 close condition 1).

        Counted as the number of ACTUAL ``read_entries`` (one HTTP fetch each) the reconcile source
        served across all workspaces (Redmine #14150 review F2) — NOT the number of issues that touched
        the provider (a single issue makes several reads). A LOCAL drain never reads the provider, so an
        empty drain pass and a safe-pending drain pass both roll up to ``0`` here — the testable contract.
        """
        return sum(w.provider_calls for w in self.workspaces)

    @property
    def empty_pass(self) -> bool:
        """True iff this pass produced no delivery, no supply, and no provider read (Redmine #14150).

        The observability signal the issue asks for: an empty drain pass (nothing to deliver) is
        visible as ``empty_pass`` with ``provider_calls == 0``.
        """
        return (
            self.delivered == 0
            and self.blocked == 0
            and self.events_supplied == 0
            and self.provider_calls == 0
        )

    @property
    def backlog_fenced(self) -> int:
        return sum(w.backlog_fenced for w in self.workspaces)

    @property
    def backlog_recovered(self) -> int:
        return sum(w.backlog_recovered for w in self.workspaces)

    def as_payload(self) -> dict[str, object]:
        return {
            "action": "run-once",
            "mode": self.mode,
            "holder": self.holder,
            "duration_ms": self.duration_ms,
            "workspaces_total": len(self.workspaces),
            "workspaces_supervised": self.workspaces_supervised,
            "workspaces_skipped": self.workspaces_skipped,
            "events_supplied": self.events_supplied,
            "delivered": self.delivered,
            "blocked": self.blocked,
            "deferred": self.deferred,
            "provider_calls": self.provider_calls,
            "empty_pass": self.empty_pass,
            "backlog_fenced": self.backlog_fenced,
            "backlog_recovered": self.backlog_recovered,
            "workspaces": [w.as_payload() for w in self.workspaces],
        }


# ---------------------------------------------------------------------------
# Provider reconciliation cadence: watermark gate + jitter/backoff (Redmine #14150).
# ---------------------------------------------------------------------------


def _parse_iso(value: object) -> "datetime | None":
    """Parse an ISO-8601 timestamp to an aware UTC ``datetime`` (``None`` if unparseable; pure)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def reconcile_backoff_seconds(
    base_interval_seconds: int,
    consecutive_empty_passes: int,
    *,
    max_interval_seconds: int,
    jitter_unit: float = 0.0,
    jitter_fraction: float = 0.0,
) -> int:
    """The next provider-reconcile due delay (seconds), backed off + jittered (Redmine #14150; pure).

    The provider reconciliation leg must not re-read every workspace / every journal on a fixed tight
    cadence. When consecutive passes find nothing new, the due interval backs off exponentially from
    ``base_interval_seconds`` (doubling per empty pass) up to ``max_interval_seconds``, so an idle
    fleet quiesces toward the ceiling instead of polling the provider at the floor. ``jitter_unit`` is
    an injected value in ``[0, 1)`` (a seam — the caller supplies a deterministic value in tests and a
    real RNG draw in production, so this stays pure and reproducible); it spreads the due time by up to
    ``jitter_fraction`` of the backed-off interval so a fleet of workspaces does not thunder the
    provider in lockstep. Returns an int in ``[base, max]`` (jitter only ADDS, never below base).
    """
    base = max(1, int(base_interval_seconds))
    ceiling = max(base, int(max_interval_seconds))
    empties = max(0, int(consecutive_empty_passes))
    # Exponential backoff, capped — guard the shift so a large empty count never overflows.
    backed = min(ceiling, base * (2 ** min(empties, 30)))
    unit = min(max(float(jitter_unit), 0.0), 0.999999)
    fraction = min(max(float(jitter_fraction), 0.0), 1.0)
    jitter = int(backed * fraction * unit)
    return min(ceiling, backed + jitter)


def should_reconcile_source(
    last_reconciled_at: object,
    now: object,
    due_after_seconds: int,
) -> bool:
    """True iff the provider reconcile watermark for a source is DUE (Redmine #14150; pure).

    ``last_reconciled_at`` is the durable watermark of the last completed provider read for a source
    (blank / unparseable -> never reconciled -> due). ``now`` is the current ISO timestamp; the source
    is due when ``due_after_seconds`` have elapsed since the watermark. This is the differential-fetch
    gate: a drain-only tick never sets the watermark (it made no provider read), so it never suppresses
    a genuine reconcile; only a completed provider read advances it. An unparseable ``now`` fails toward
    reconciling (never silently skips the provider fallback).
    """
    now_dt = _parse_iso(now)
    if now_dt is None:
        return True
    last_dt = _parse_iso(last_reconciled_at)
    if last_dt is None:
        return True
    return (now_dt - last_dt).total_seconds() >= max(0, int(due_after_seconds))


# ---------------------------------------------------------------------------
# Service lifecycle contract (declarative; NO secrets).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorServiceDefinition:
    """The declarative definition of the supervisor daemon a host service manager would run.

    Deliberately carries **no credential / secret** (Redmine is the durable auth authority; the
    daemon resolves credentials from the daemon-trusted environment / home file at run time, never
    from a service definition or its log — j#77065). ``command`` is the argv the service runs on
    each reconciliation tick; ``reconciliation_interval_seconds`` is the bounded cadence;
    ``run_at_login`` / ``keep_alive`` are the residency knobs a Phase B installer maps onto the
    concrete host service (launchd / systemd), which Phase A does not touch.
    """

    label: str
    command: tuple[str, ...]
    reconciliation_interval_seconds: int
    run_at_login: bool
    #: Always ``False`` for this supervisor: the command is a bounded ``--run-once`` sweep that
    #: exits, scheduled by a host interval (launchd ``StartInterval`` in Phase B1), NOT kept alive.
    #: Mapping a one-shot command onto ``KeepAlive`` would be a tight restart loop (j#78995), so the
    #: declarative contract pins ``keep_alive=False`` and the rendered launchd plist omits KeepAlive.
    keep_alive: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "command": list(self.command),
            "reconciliation_interval_seconds": self.reconciliation_interval_seconds,
            "run_at_login": self.run_at_login,
            "keep_alive": self.keep_alive,
        }


#: The default service label (a reverse-DNS style id; not operator-private).
DEFAULT_SUPERVISOR_SERVICE_LABEL = "org.mozyo-bridge.callback-supervisor"
#: The default LOCAL-drain service label (Redmine #14150): the finer-cadence drain agent, distinct
#: from the coarse provider-reconciliation agent so an OS scheduler runs BOTH bounded one-shots.
DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL = "org.mozyo-bridge.callback-supervisor.drain"


def build_service_definition(
    *,
    command_prefix: Sequence[str] = ("mozyo-bridge", "workflow", "supervisor"),
    reconciliation_interval_seconds: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    run_at_login: bool = True,
    keep_alive: bool = False,
    label: str = DEFAULT_SUPERVISOR_SERVICE_LABEL,
    local_drain: bool = False,
) -> SupervisorServiceDefinition:
    """Build the supervisor daemon's declarative service definition (pure, no secrets).

    The command is ``<command_prefix> --run-once`` — the service manager invokes one bounded
    supervised sweep per tick (the run-once entrypoint), so residency lives in the host manager's
    scheduled interval, not an unbounded in-process poll (the wait/polling doctrine keeps the
    bounded cadence in the watcher/service layer, never an LLM turn). ``keep_alive`` defaults to
    **False**: the sweep exits and is re-run on the interval (launchd ``RunAtLoad`` + ``StartInterval``
    in Phase B1), so KeepAlive would only produce a tight restart loop (j#78995).

    ``local_drain`` (Redmine #14150) builds the LOCAL-drain variant instead: the command is
    ``<command_prefix> --drain-only`` (read local state only, zero ticket-provider reads) and
    ``reconciliation_interval_seconds`` carries the FINER drain cadence. The OS scheduler runs this
    bounded one-shot at the drain interval alongside the coarser reconciliation agent — the SAME
    bounded command adapter, never an in-turn sleep/poll. The default label switches to the distinct
    drain label unless the caller overrides ``label``.
    """
    interval = max(1, int(reconciliation_interval_seconds))
    action_flag = "--drain-only" if local_drain else "--run-once"
    command = tuple(str(p) for p in command_prefix) + (action_flag,)
    resolved_label = str(label)
    if local_drain and resolved_label == DEFAULT_SUPERVISOR_SERVICE_LABEL:
        resolved_label = DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL
    return SupervisorServiceDefinition(
        label=resolved_label,
        command=command,
        reconciliation_interval_seconds=interval,
        run_at_login=bool(run_at_login),
        keep_alive=bool(keep_alive),
    )


__all__ = (
    "SUPERVISION_BOUNDED_RECONCILIATION",
    "SUPERVISION_LOCAL_WAKE",
    "SUPERVISION_LOCAL_DRAIN",
    "SUPERVISION_MODES",
    "DEFAULT_RECONCILIATION_INTERVAL_SECONDS",
    "DEFAULT_LOCAL_DRAIN_INTERVAL_SECONDS",
    "SKIP_LEASE_REFUSED",
    "SKIP_ROSTER_UNREADABLE",
    "SKIP_NO_ACTIVE_ISSUES",
    "SKIP_LEASE_LOST",
    "ISSUE_SOURCE_UNREADABLE",
    "ISSUE_PASS_ERROR",
    "ISSUE_LEASE_LOST",
    "DROP_NOT_AUTHORITATIVE",
    "COORDINATOR_ROUTE",
    "LOCAL_DRAIN_ATTESTABLE_ROUTES",
    "DRAIN_DEFER_NOT_ATTESTABLE",
    "DRAIN_DEFER_ANCHOR_UNRESOLVED",
    "authoritative_workspace_by_issue",
    "partition_authoritative",
    "fence_candidates_to_anchor",
    "make_send_edge_fence",
    "compose_send_edge_fences",
    "partition_delivery_receipts",
    "is_locally_attestable_route",
    "select_drain_issues",
    "fence_candidates_after_cursor",
    "reconcile_backoff_seconds",
    "should_reconcile_source",
    "IssueSelection",
    "select_supervised_issues",
    "IssueSupervisionOutcome",
    "WorkspaceSupervisionOutcome",
    "SupervisorReport",
    "SupervisorServiceDefinition",
    "DEFAULT_SUPERVISOR_SERVICE_LABEL",
    "DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL",
    "build_service_definition",
)
