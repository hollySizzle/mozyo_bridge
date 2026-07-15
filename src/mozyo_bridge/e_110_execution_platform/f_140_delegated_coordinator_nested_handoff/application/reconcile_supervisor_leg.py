"""Reconcile supervisor leg — production wiring (Redmine #13758 review F1).

Wires the event-driven reconcile cycle into the existing workspace callback supervisor: for
one active-lane issue, it reads the issue's structured gate markers (the workflow truth),
resolves the lane's live generation + lifecycle disposition, builds the fail-closed
:class:`...reconcile_state_machine.ReconcileObservation`, and runs one
:func:`...reconcile_runtime.reconcile_once` against the shared reconcile store + the shared
callback outbox. This is the composition root the review required — the reconciler now
runs on the supervisor's lease/wake/cursor path, not only from tests.

The observation is built from the exact structured Redmine journal + the live lane
generation (never a runtime signal): a turn-ended wake drives the supervisor, and this leg
re-reads Redmine and decides. Raw Herdr turn-edge detection and the live installed-artifact
dogfood are the #13492 surface; the leg's inputs (markers, generation, disposition, outbox
states) are all injected, so every branch is test-pinned without a live registry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from mozyo_bridge.core.state.callback_outbox import (
    CALLBACK_DELIVERED,
    CALLBACK_UNCERTAIN,
    CallbackOutbox,
)
from mozyo_bridge.core.state.reconcile_state import (
    ReconcileStateKey,
    ReconcileStateRecord,
    ReconcileStateStore,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_runtime import (
    ReconcileCycleInput,
    outbox_key_for,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_runtime import (
    reconcile_once as _reconcile_once,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_delivery_route import (
    provider_for_role,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_gate_chain import (
    expected_next,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_state_machine import (
    GEN_MATCH,
    GEN_MISMATCH,
    GEN_UNKNOWN,
    RECONCILE_ACTION_DELIVER,
    RECONCILE_CLOSED,
    RECONCILE_NOTIFIED,
    RECONCILE_SELF_HEAL_1,
    RECONCILE_SELF_HEAL_2,
    ROUTE_RESOLVED,
    ReconcileObservation,
    ReconcileDecision,
    is_turn_end_edge,
)

#: Lifecycle dispositions that terminally close a reconcile (no callback owed — §5 end).
_TERMINAL_DISPOSITIONS = frozenset({"hibernated", "retired", "superseded"})
#: Reconcile phases at which a record owes no further action for its await.
_TERMINAL_PHASES = frozenset({RECONCILE_NOTIFIED, RECONCILE_CLOSED})


def _int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _latest_gate_marker(markers: Iterable[object]) -> Optional[object]:
    """The most recent gate-bearing marker (max journal), or ``None`` for a gate-free lane."""
    best = None
    best_j = -1
    for m in markers or ():
        j = _int(getattr(m, "journal", ""), default=-1)
        if j > best_j:
            best_j, best = j, m
    return best


def _marker_with_gate_after(
    markers: Iterable[object], gate: str, after_journal: int
) -> Optional[object]:
    """The first marker whose gate is ``gate`` and journal > ``after_journal`` (or ``None``)."""
    for m in markers or ():
        if str(getattr(m, "gate", "")).strip() == gate and _int(
            getattr(m, "journal", ""), default=-1
        ) > after_journal:
            return m
    return None


def _gen_status(record: Optional[ReconcileStateRecord], live_generation: int) -> str:
    """Correlate the record's generation against the live lane generation (fail-closed)."""
    if live_generation <= 0:
        return GEN_UNKNOWN
    if record is not None and int(record.lane_generation) > 0:
        return GEN_MATCH if int(record.lane_generation) == live_generation else GEN_MISMATCH
    return GEN_MATCH


def _self_heal_gate_for_phase(phase: str) -> str:
    """The outbox normalized_gate token a prior self-heal at ``phase`` would have used."""
    if phase == RECONCILE_SELF_HEAL_1:
        return RECONCILE_SELF_HEAL_1
    if phase == RECONCILE_SELF_HEAL_2:
        return RECONCILE_SELF_HEAL_2
    return ""


def _second_latest_journal(markers: Iterable[object], below: int, *, fallback: str = "0") -> str:
    """The highest gate journal strictly below ``below`` (the dispatch position before it).

    When there is no earlier gate (the latest is the FIRST gate), returns ``fallback`` — the
    exact dispatch anchor — so the landed-await anchor matches the initial self-heal anchor,
    which also baselined on the dispatch anchor (review R3-F2).
    """
    best = -1
    for m in markers or ():
        j = _int(getattr(m, "journal", ""), default=-1)
        if best < j < below:
            best = j
    return str(best) if best >= 0 else str(fallback or "0")


def _anchor(issue: str, generation: int, baseline_journal: str, gate: str) -> str:
    """The reconcile record anchor — issue + lane generation + dispatch baseline + awaited gate.

    Includes the lane ``generation`` (the #13810 incarnation — a recycled lane is a fresh
    record) AND the dispatch/review-round baseline journal (the position the await opened
    from), so a ``changes_requested -> correction -> re-review`` loop that awaits the SAME gate
    (implementation_done again) in a NEW round gets a distinct durable record instead of
    reusing the prior round's terminal one (Redmine #13758 review R2-F3): the acceptance key is
    ``workspace + lane + issue + exact action/review generation``.
    """
    return f"{issue}:g{int(generation)}:from{baseline_journal}:await:{gate}"


def reconcile_leg_once(
    *,
    issue: str,
    workspace_id: str,
    lane_id: str,
    markers: Iterable[object],
    live_generation: int,
    lifecycle_disposition: str,
    outbox: CallbackOutbox,
    reconcile_store: ReconcileStateStore,
    runtime_state: str = "",
    dispatch_anchor: str = "",
    redmine_readable: bool = True,
    deadline: str = "",
    now: Optional[str] = None,
    now_epoch: Optional[float] = None,
) -> Optional[dict]:
    """Run one reconcile cycle for an active-lane issue (production leg). Returns a report or ``None``.

    Derives the awaited ``(expected_gate, expected_next_owner)`` from the lane's latest gate
    position (:func:`...reconcile_gate_chain.expected_next`); a position not attributable to a
    same-lane owner yields ``None`` — no reconcile (acceptance §6, fail-safe). Otherwise builds
    the fail-closed observation and runs :func:`reconcile_once`, then persists the observed
    runtime for the next cycle's edge detection.

    ``runtime_state`` is the lane worker's CURRENT observed runtime (``busy`` / ``turn_ended`` /
    ...) from the live inventory; the genuine ``busy -> turn_ended`` edge is derived by
    comparing it to the record's persisted ``last_observed_runtime`` (review R3-F1: a local
    wake is NOT a turn edge — the wake carries no runtime transition). A blank / unobservable
    runtime yields no edge (fail-closed: no self-heal without a real turn end). ``dispatch_anchor``
    is the exact durable dispatch identity (the lifecycle decision journal) used as the initial
    await baseline so the freshly-dispatched anchor is not a generic ``from0`` (review R3-F2).
    """
    wsid, laneid, issue_s = (
        str(workspace_id).strip(),
        str(lane_id).strip(),
        str(issue).strip(),
    )
    generation = int(live_generation) if live_generation > 0 else 0
    dispatch = str(dispatch_anchor or "").strip() or "0"
    latest = _latest_gate_marker(markers)
    latest_gate = str(getattr(latest, "gate", "")).strip() if latest is not None else ""
    latest_journal = _int(getattr(latest, "journal", ""), default=0) if latest is not None else 0
    conclusion = (
        str(getattr(latest, "review_conclusion", "")).strip() if latest is not None else ""
    )
    terminal = str(lifecycle_disposition or "").strip() in _TERMINAL_DISPOSITIONS

    # (1) Deliver a just-landed await first: if the current latest gate has an OPEN (non-terminal)
    #     reconcile record — anchored on the gate BEFORE it (its dispatch baseline) — that await's
    #     gate has landed, so reconcile THAT record to deliver the coordinator callback and close,
    #     before opening the next await (the leg's deliver branch is reachable — F1 — keyed
    #     byte-identically to discovery — F3). The gate BEFORE the latest is the second-latest
    #     gate journal, or the exact dispatch anchor when the latest gate is the first one.
    if latest_gate:
        landed_baseline = _second_latest_journal(markers, latest_journal, fallback=dispatch)
        landed_anchor = _anchor(issue_s, generation, landed_baseline, latest_gate)
        landed_key = ReconcileStateKey(wsid, laneid, landed_anchor)
        landed_record = reconcile_store.get(landed_key)
        if landed_record is not None and landed_record.phase not in _TERMINAL_PHASES:
            return _run_cycle(
                key=landed_key,
                issue=issue_s,
                lane_id=laneid,
                expected_gate=latest_gate,
                expected_owner=str(landed_record.expected_next_owner or "").strip(),
                baseline_journal=str(landed_record.latest_journal_id or landed_baseline),
                markers=markers,
                live_generation=generation,
                terminal=terminal,
                runtime_state=runtime_state,
                outbox=outbox,
                reconcile_store=reconcile_store,
                redmine_readable=bool(redmine_readable),
                deadline=deadline,
                now=now,
                now_epoch=now_epoch,
            )

    # (2) Otherwise open / continue the next await (the self-heal ladder). The initial await
    #     (no gate produced yet) baselines on the exact dispatch anchor (the lifecycle decision
    #     journal), NOT a generic ``0`` — so a re-dispatch is a distinct record (review R3-F2).
    exp = expected_next(latest_gate, review_conclusion=conclusion)
    if exp is None:
        return None  # no same-lane-owed next gate -> nothing to reconcile
    expected_gate, expected_owner = exp
    baseline_journal = str(latest_journal) if latest_journal > 0 else dispatch
    return _run_cycle(
        key=ReconcileStateKey(
            wsid, laneid, _anchor(issue_s, generation, baseline_journal, expected_gate)
        ),
        issue=issue_s,
        lane_id=laneid,
        expected_gate=expected_gate,
        expected_owner=expected_owner,
        baseline_journal=baseline_journal,
        markers=markers,
        live_generation=generation,
        terminal=terminal,
        runtime_state=runtime_state,
        outbox=outbox,
        reconcile_store=reconcile_store,
        redmine_readable=bool(redmine_readable),
        deadline=deadline,
        now=now,
        now_epoch=now_epoch,
    )


def _run_cycle(
    *,
    key: ReconcileStateKey,
    issue: str,
    lane_id: str,
    expected_gate: str,
    expected_owner: str,
    baseline_journal: str,
    markers: Iterable[object],
    live_generation: int,
    terminal: bool,
    runtime_state: str,
    outbox: CallbackOutbox,
    reconcile_store: ReconcileStateStore,
    redmine_readable: bool,
    deadline: str,
    now: Optional[str],
    now_epoch: Optional[float],
) -> Optional[dict]:
    """Build the observation for one anchored await and run :func:`reconcile_once`.

    The turn-end edge is derived from the observed runtime vs the record's persisted
    ``last_observed_runtime`` (review R3-F1). After the cycle, the observed runtime is persisted
    (without a revision bump) so the NEXT cycle can detect a fresh busy->turn_ended transition —
    including a busy->turn_ended after a prior turn_ended (a genuine new turn), which a
    stored-only-on-advance model would miss.
    """
    observed_runtime = str(runtime_state or "").strip()
    cycle = ReconcileCycleInput(
        key=key,
        issue_id=issue,
        dispatch_journal=baseline_journal,
        expected_gate=expected_gate,
        expected_next_owner=expected_owner,
        # The resolver-matchable delivery target: the owner's provider (worker -> claude), NOT
        # the role token (review R2-F2). The background-service resolver re-matches this against
        # the live inventory to reach the same-lane pane.
        target_receiver=provider_for_role(expected_owner),
        lane_generation=int(live_generation) if live_generation > 0 else 0,
        deadline=deadline,
        target_lane=lane_id,
    )

    def observe(c: ReconcileCycleInput, record: ReconcileStateRecord) -> ReconcileObservation:
        awaited = record.expected_gate or expected_gate
        baseline = _int(record.latest_journal_id or baseline_journal, default=0)
        advanced = _marker_with_gate_after(markers, awaited, baseline)
        gate_advanced = advanced is not None
        advanced_journal = (
            str(getattr(advanced, "journal", "")).strip() if gate_advanced else ""
        )
        callback_delivered = False
        if gate_advanced:
            deliver_decision = ReconcileDecision(
                action=RECONCILE_ACTION_DELIVER, reason="", next_phase="",
                next_failure_count=0, route="coordinator",
            )
            deliver_key = outbox_key_for(
                c, deliver_decision,
                advanced_gate=awaited, advanced_gate_journal=advanced_journal,
            )
            callback_delivered = outbox.state_of(deliver_key) == CALLBACK_DELIVERED
        prior_uncertain = False
        heal_gate = _self_heal_gate_for_phase(record.phase)
        if heal_gate:
            heal_decision = ReconcileDecision(
                action="self_heal", reason="", next_phase=heal_gate,
                next_failure_count=0, route=expected_owner,
            )
            heal_key = outbox_key_for(c, heal_decision)
            prior_uncertain = outbox.state_of(heal_key) == CALLBACK_UNCERTAIN
        deadline_exceeded = _deadline_exceeded(record.deadline, now_epoch)
        # The genuine busy->turn_ended edge: the record's last-observed runtime was not
        # turn_ended and the current observation is (review R3-F1). A blank observed runtime is
        # not turn_ended -> no edge (fail-closed: no self-heal without a real turn end).
        edge = is_turn_end_edge(record.last_observed_runtime, observed_runtime)
        return ReconcileObservation(
            redmine_readable=bool(redmine_readable),
            generation_status=_gen_status(record, int(live_generation)),
            gate_advanced=gate_advanced,
            advanced_gate=awaited if gate_advanced else "",
            advanced_gate_journal=advanced_journal,
            callback_delivered=callback_delivered,
            has_outstanding_gate=True,
            terminal_disposition=terminal,
            deadline_exceeded=deadline_exceeded,
            prior_send_uncertain=prior_uncertain,
            route_status=ROUTE_RESOLVED,
            expected_next_owner=expected_owner,
            is_edge=edge,
        )

    report = _reconcile_once(
        cycle, observe=observe, outbox=outbox, store=reconcile_store, now=now
    )
    # Persist the observed runtime for the next cycle's edge detection (best-effort, no
    # revision bump) — always, even on a non-edge no-op, so a later busy->turn_ended after a
    # prior turn_ended is detected (review R3-F1).
    if observed_runtime:
        reconcile_store.touch_runtime(key, observed_runtime, now=now)
    return report


def _deadline_exceeded(deadline: str, now_epoch: Optional[float]) -> bool:
    """True when a set ISO deadline is in the past relative to ``now_epoch`` (best-effort)."""
    deadline = str(deadline or "").strip()
    if not deadline or now_epoch is None:
        return False
    try:
        parsed = datetime.fromisoformat(deadline)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() < float(now_epoch)
    except (TypeError, ValueError):
        return False


def build_reconcile_leg_fn(
    *,
    reconcile_store: ReconcileStateStore,
    outbox: CallbackOutbox,
    lane_facts_fn: Callable[[str, str], "tuple[str, int, str]"],
    markers_fn: Callable[[object, str], Iterable[object]],
) -> Callable[[str, str, object], Optional[dict]]:
    """Build the supervisor's ``reconcile_leg_fn`` closure.

    ``lane_facts_fn(workspace_id, issue) -> (lane_id, live_generation, lifecycle_disposition,
    dispatch_anchor, runtime_state)`` resolves the lane identity + generation + disposition +
    the exact durable dispatch anchor (review R3-F2) + the live worker runtime (review R3-F1)
    from the lifecycle authority + live inventory; ``markers_fn(source, issue)`` reads the
    issue's structured gate markers (the ``workflow watch`` intake). Returns a callable the
    supervisor invokes per issue **after** the callback drain:
    ``reconcile_leg_fn(workspace_id, issue, source) -> report | None``. Any resolution / read
    failure returns ``None`` (fail-open per issue — the reconcile is a bounded add-on and never
    aborts the supervisor sweep).
    """

    def _leg(workspace_id: str, issue: str, source: object) -> Optional[dict]:
        try:
            facts = lane_facts_fn(workspace_id, issue)
            lane_id, live_generation, disposition, dispatch_anchor, runtime_state = facts
        except Exception:  # noqa: BLE001 - an unresolved lane is a fail-open skip (no reconcile)
            return None
        if not str(lane_id or "").strip():
            return None
        try:
            markers = list(markers_fn(source, issue))
        except Exception:  # noqa: BLE001 - an unreadable source fails closed via redmine_readable
            markers = []
            readable = False
        else:
            readable = True
        try:
            return reconcile_leg_once(
                issue=issue,
                workspace_id=workspace_id,
                lane_id=lane_id,
                markers=markers,
                live_generation=int(live_generation),
                lifecycle_disposition=disposition,
                outbox=outbox,
                reconcile_store=reconcile_store,
                runtime_state=str(runtime_state or ""),
                dispatch_anchor=str(dispatch_anchor or ""),
                redmine_readable=readable,
            )
        except Exception:  # noqa: BLE001 - a reconcile failure never breaks the supervisor sweep
            return None

    return _leg


__all__ = (
    "reconcile_leg_once",
    "build_reconcile_leg_fn",
)
