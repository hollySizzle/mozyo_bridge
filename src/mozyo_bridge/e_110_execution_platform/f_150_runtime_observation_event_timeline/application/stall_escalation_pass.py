"""The stall escalation gate, in two phases (Redmine #15855).

The seam between the #15843 sensor and the #15855 wiring, split along the line #15855
j#110121 drew: **observing is free, recording is not.**

- :func:`apply_escalation_gate` folds one pass's observations into the stored runs and
  turns crossed thresholds into *pending* firings. It makes **zero external mutations** —
  it reads screens' verdicts (already gathered), writes local SQLite, and stops.
- :func:`settle_pending_escalations` is the part that costs something: it writes **at most
  one** Redmine ``## Gate: blocked`` journal per pass through the canonical gate writer,
  and only when the pass-wide external-mutation budget is still unspent.

Why the split is not cosmetic
-----------------------------
``workspace_callback_supervisor`` allows **one external mutation per bounded pass**, with
callback delivery holding first priority (#14219 T3 / Final Disposition j#87188). j#110121-6
rejected the idea that an escalation could sidestep that budget: the durable escalation
record is a Redmine journal, a Redmine journal append *is* an external mutation, and
pretending otherwise would let this rail quietly double the per-pass mutation rate the
supervisor is built around. So the firing and the recording are separated in time, and the
gap between them is a durable, inspectable pending row rather than a retry loop.

Ordering, and the fence that makes it safe
------------------------------------------
Per firing the sequence is **pending → journal → readback → wake**, and each arrow is
guarded:

- the pending row is keyed by :func:`escalation_idempotency_key`, derived from the run's
  identity rather than the firing pass's clock, so a crash-and-retry collides instead of
  producing a second journal;
- ``mark_recorded`` refuses a blank journal id, so an "it probably wrote" never counts as
  written — the firing stays unrecorded and is retried, which is the correct direction when
  the alternative is a stall nobody is told about;
- ``mark_woken`` refuses a firing with no journal id **in SQL**, so a coordinator is never
  woken to read a journal that does not exist. That ordering is enforced by the store
  rather than trusted to call order, because it is the one inversion this rail exists to
  prevent.

Fairness, stated as an assumption rather than assumed
-----------------------------------------------------
Delivery-first is preserved: this rail takes the mutation slot only on a pass where nothing
else has spent it. Pending firings are settled **oldest first**, so a newer stall cannot
repeatedly overtake an older one, and the pending row carries ``attempts`` /
``last_reason`` so a refused write is distinguishable from one nobody has reached yet.

The non-starvation property therefore rests on one assumption, named here because it is not
provable inside this module: **callback delivery eventually idles.** That is the same
assumption the outbox drain already rests on — a permanently non-empty outbox is a
pathology in its own right — but if it fails, escalations queue rather than being lost, and
the oldest pending age is what makes that visible instead of silent. Reversing
delivery-first to bound the age would be a change to a recorded decision, not an
implementation detail, and is deliberately not done here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from mozyo_bridge.core.state.stall_escalation import (
    PendingEscalation,
    StallEscalationStore,
    StreakRow,
    escalation_idempotency_key,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_pass import (  # noqa: E501
    StallObservation,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_escalation_policy import (  # noqa: E501
    DEFAULT_ESCALATION_THRESHOLD,
    STREAK_ADVANCE,
    STREAK_HOLD,
    STREAK_RESET,
    StreakState,
    WatchIdentity,
    fold_observation,
)

#: What a journal write did to the world, in the vocabulary the shared pass budget speaks.
#:
#: The three-way split exists because "no journal id came back" is NOT one situation. A
#: refused write never touched Redmine; a POST that returned but could not be read back MAY
#: have created a journal. Collapsing them (as this module first did) leaves an external
#: mutation unaccounted for, and the pass budget is shared across every workspace, so the
#: next workspace then performs a SECOND external mutation in the same bounded pass —
#: exactly what ``pass_external_budget`` exists to prevent ("an UNCERTAIN external effect
#: consumes it (no blind continuation)").
#: A deterministic zero-send. Nothing reached Redmine; the budget is untouched.
WRITE_NOT_SENT = "not_sent"
#: A journal exists and its id was read back. Spends the budget as ``mutated``.
WRITE_RECORDED = "recorded"
#: The write MAY have landed (POST returned but unverifiable, transport error, raise).
#: Spends the budget as ``uncertain``.
WRITE_UNCERTAIN = "uncertain"

WRITE_OUTCOMES: frozenset[str] = frozenset(
    {WRITE_NOT_SENT, WRITE_RECORDED, WRITE_UNCERTAIN}
)


@dataclass(frozen=True)
class JournalWriteResult:
    """One journal write attempt, classified for the shared external-mutation budget."""

    outcome: str
    journal_id: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in WRITE_OUTCOMES:
            raise ValueError(f"unknown journal write outcome {self.outcome!r}")
        if self.outcome == WRITE_RECORDED and not self.journal_id:
            raise ValueError("a recorded write must carry the journal id it read back")


#: Writes one escalation journal and classifies what it did. It must not raise — a writer
#: that blows up is wrapped by the caller, which treats the failure as UNCERTAIN because a
#: raise after the POST is indistinguishable from a landed write.
JournalWriter = Callable[[PendingEscalation], JournalWriteResult]

#: Enqueues a coordinator wake for ``(workspace_id, issue)``; returns whether it stuck.
WakeEnqueue = Callable[[str, str], bool]

#: Settle reason: the firing has no authoritative active issue anchor, so there is nowhere
#: to write it. j#110121-5 forbids guessing an issue, so it stays local and visible.
SETTLE_ANCHOR_UNRESOLVED = "issue_anchor_unresolved"
#: Settle reason: some other leg of this pass already spent the one external mutation.
SETTLE_BUDGET_SPENT = "external_mutation_budget_spent"
#: Settle reason: nothing was waiting.
SETTLE_NOTHING_PENDING = "nothing_pending"
#: Settle reason: a journal was written and read back.
SETTLE_RECORDED = "recorded"
#: Settle reason: the writer deterministically sent nothing; the firing stays retryable.
SETTLE_WRITE_REFUSED = "write_refused"
#: Settle reason: the write MAY have landed. The firing stays unrecorded (so a later pass
#: re-checks and binds it if a journal exists) AND the pass budget is spent as uncertain, so
#: no later workspace mutates behind an unknown partial effect.
SETTLE_WRITE_UNCERTAIN = "write_uncertain"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ObservedUnit:
    """One classified unit, joined to the durable identity the discovery layer resolved.

    ``issue`` is the authoritative active issue anchor. Its absence is a normal outcome, not
    a degraded mode: the firing is still recorded locally, it simply cannot be written to a
    Redmine issue, because guessing one is forbidden (j#110121-5).
    """

    identity: WatchIdentity
    observation: StallObservation
    issue: str = ""
    evidence_tier: str = ""


@dataclass(frozen=True)
class EscalationPassOutcome:
    """What the gate did with one pass's observations. Carries no pane content."""

    workspace_id: str
    observed: int = 0
    advanced: int = 0
    reset: int = 0
    held: int = 0
    forgotten: int = 0
    fired: tuple[PendingEscalation, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def escalated(self) -> int:
        return len(self.fired)

    def telemetry(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "observed": self.observed,
            "advanced": self.advanced,
            "reset": self.reset,
            "held": self.held,
            "forgotten": self.forgotten,
            "escalated": self.escalated,
            "fired": [pending.telemetry() for pending in self.fired],
        }
        if self.errors:
            payload["errors"] = list(self.errors)
        return payload


@dataclass(frozen=True)
class SettleOutcome:
    """What one pass did about the pending queue."""

    workspace_id: str
    reason: str = SETTLE_NOTHING_PENDING
    recorded: Optional[PendingEscalation] = None
    woke: tuple[str, ...] = ()
    unrecorded: int = 0
    anchorless: int = 0
    oldest_unrecorded_at: str = ""
    errors: tuple[str, ...] = ()

    @property
    def spent_budget(self) -> bool:
        """Whether this settle consumed the pass's one external mutation."""
        return self.recorded is not None

    def telemetry(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "settle_reason": self.reason,
            "unrecorded_pending": self.unrecorded,
            "anchorless_pending": self.anchorless,
            "woke": list(self.woke),
            "spent_budget": self.spent_budget,
        }
        if self.oldest_unrecorded_at:
            payload["oldest_unrecorded_at"] = self.oldest_unrecorded_at
        if self.recorded is not None:
            payload["recorded"] = self.recorded.telemetry()
        if self.errors:
            payload["errors"] = list(self.errors)
        return payload


# --------------------------------------------------------------------------------------
# Phase 1: observe -> fold -> persist -> enqueue pending (zero external mutation)
# --------------------------------------------------------------------------------------


def _to_state(row: StreakRow) -> Optional[StreakState]:
    """Lift a stored row into the policy's state, or ``None`` when it is not usable.

    A row whose ``stall_class`` this build no longer declares (a downgrade, a hand-edited
    DB) is dropped rather than raising: the pure layer would reject it, and a watcher tick
    must not die on one bad row. Dropping it restarts that slot's run from this pass, which
    is the conservative direction — it delays an escalation, never invents one.
    """
    try:
        return StreakState(
            identity=WatchIdentity(
                workspace_id=row.workspace_id,
                lane_id=row.lane_id,
                role=row.role,
                generation=row.generation,
                target=row.target,
            ),
            stall_class=row.stall_class,
            consecutive=int(row.consecutive),
            first_observed_at=row.first_observed_at,
            last_observed_at=row.last_observed_at,
            escalated_at=row.escalated_at,
        )
    except Exception:  # noqa: BLE001 - an unreadable stored row is not a crash
        return None


def _to_row(state: StreakState) -> StreakRow:
    return StreakRow(
        workspace_id=state.identity.workspace_id,
        lane_id=state.identity.lane_id,
        role=state.identity.role,
        generation=state.identity.generation,
        target=state.identity.target,
        stall_class=state.stall_class,
        consecutive=state.consecutive,
        first_observed_at=state.first_observed_at,
        last_observed_at=state.last_observed_at,
        escalated_at=state.escalated_at,
    )


def apply_escalation_gate(
    observed: Sequence[ObservedUnit],
    *,
    workspace_id: str,
    store: StallEscalationStore,
    threshold: int = DEFAULT_ESCALATION_THRESHOLD,
    now: Callable[[], str] = _utc_now_iso,
    forget_absent: bool = True,
) -> EscalationPassOutcome:
    """Fold ``observed`` into the stored runs and enqueue whatever crosses the threshold.

    Makes no external mutation of any kind: crossed thresholds become **pending** rows, and
    :func:`settle_pending_escalations` decides when one of them may become a Redmine
    journal.

    ``forget_absent`` drops runs for slots absent from *this* pass's observation set. It is
    correct only when the caller observed the whole managed inventory; a caller watching a
    hand-picked subset must pass ``False``, or every unobserved slot's run would be erased
    on every tick and no stall could ever reach the threshold.

    One unit's failure never aborts the pass: a store error is captured into
    :attr:`EscalationPassOutcome.errors` and the remaining units are still processed. A
    watcher that dies on the first wedged unit is blind to the rest of the cockpit, which
    is the property :func:`run_stall_watch_pass` already protects for reads.
    """
    ws = str(workspace_id or "").strip()
    if not ws:
        return EscalationPassOutcome(
            workspace_id="", errors=("workspace_id is required to scope stall streaks",)
        )

    try:
        stored = store.read_streaks(ws)
    except Exception as exc:  # noqa: BLE001 - an unreadable store is reported, not raised
        return EscalationPassOutcome(
            workspace_id=ws, errors=(f"streak read failed: {type(exc).__name__}",)
        )

    advanced = reset = held = 0
    fired: list[PendingEscalation] = []
    errors: list[str] = []

    for unit in observed:
        identity = unit.identity
        obs = unit.observation
        if identity.workspace_id != ws:
            errors.append(
                f"{identity.slot_label}: belongs to another workspace; skipped"
            )
            continue

        stamp = now()
        previous_row = stored.get(identity.key())
        previous = _to_state(previous_row) if previous_row is not None else None

        try:
            decision = fold_observation(
                previous,
                identity=identity,
                stall_class=obs.stall_class,
                observed_at=stamp,
                threshold=threshold,
            )
        except Exception as exc:  # noqa: BLE001 - one unclassifiable unit is not a crash
            errors.append(f"{identity.slot_label}: fold failed ({type(exc).__name__})")
            continue

        if decision.effect == STREAK_RESET:
            reset += 1
        elif decision.effect == STREAK_HOLD:
            held += 1
        elif decision.effect == STREAK_ADVANCE:
            advanced += 1

        pending: Optional[PendingEscalation] = None
        if decision.escalates and decision.next_state is not None:
            state = decision.next_state
            pending = PendingEscalation(
                idempotency_key=escalation_idempotency_key(
                    workspace_id=identity.workspace_id,
                    lane_id=identity.lane_id,
                    role=identity.role,
                    generation=identity.generation,
                    stall_class=state.stall_class,
                    first_observed_at=state.first_observed_at,
                    # The write TARGET is sealed into the key, so a row whose issue is
                    # altered later no longer derives its own key and is detected on read
                    # (review j#110192 finding_1).
                    issue=str(unit.issue or ""),
                ),
                workspace_id=identity.workspace_id,
                lane_id=identity.lane_id,
                role=identity.role,
                generation=identity.generation,
                target=identity.target,
                issue=str(unit.issue or ""),
                stall_class=state.stall_class,
                prescription=obs.prescription.action,
                matched_id=obs.matched_id,
                evidence_tier=str(unit.evidence_tier or ""),
                consecutive=state.consecutive,
                first_observed_at=state.first_observed_at,
                escalated_at=stamp,
            )

        try:
            # Pending BEFORE the latch: a crash in between re-fires and collides on the
            # idempotency key, whereas latching first would lose the firing outright.
            if pending is not None:
                store.enqueue_pending(pending)
            if decision.next_state is None:
                store.clear_streak(identity.key())
            elif decision.effect != STREAK_HOLD:
                store.write_streak(_to_row(decision.next_state))
        except Exception as exc:  # noqa: BLE001 - persist failure is reported per unit
            errors.append(
                f"{identity.slot_label}: streak persist failed ({type(exc).__name__})"
            )
            continue

        if pending is not None:
            fired.append(pending)

    forgotten = 0
    if forget_absent:
        try:
            forgotten = store.forget_absent_slots(
                ws, [unit.identity.key() for unit in observed]
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"absent-slot sweep failed: {type(exc).__name__}")

    return EscalationPassOutcome(
        workspace_id=ws,
        observed=len(observed),
        advanced=advanced,
        reset=reset,
        held=held,
        forgotten=forgotten,
        fired=tuple(fired),
        errors=tuple(errors),
    )


# --------------------------------------------------------------------------------------
# Phase 2: settle -> at most ONE Redmine journal, then the wake (budget-gated)
# --------------------------------------------------------------------------------------


def settle_pending_escalations(
    *,
    workspace_id: str,
    store: StallEscalationStore,
    budget: Optional[dict] = None,
    write_journal: Optional[JournalWriter] = None,
    wake: Optional[WakeEnqueue] = None,
    now: Callable[[], str] = _utc_now_iso,
) -> SettleOutcome:
    """Advance the pending queue by at most one external mutation.

    ``budget`` is the supervisor's pass-wide budget dict (``{"reads", "mutated",
    "uncertain"}``). A budget already carrying ``mutated`` means callback delivery — which
    holds first priority — has spent this pass's slot, so nothing is written and the
    pending queue is reported instead. When this function does write, it sets ``mutated``
    so no later leg spends the slot again.

    Wakes for firings that were recorded on an **earlier** pass are enqueued first and cost
    nothing: the wake queue is local SQLite, so it is not an external mutation. This is what
    lets a journal written on a budget-starved pass still reach a coordinator promptly.
    """
    ws = str(workspace_id or "").strip()
    if not ws:
        return SettleOutcome(
            workspace_id="", errors=("workspace_id is required to settle escalations",)
        )

    errors: list[str] = []
    woke: list[str] = []

    # 1. Local, free, and first: wake coordinators for anything already recorded.
    if wake is not None:
        try:
            unwoken = store.unwoken_pending(ws)
        except Exception as exc:  # noqa: BLE001
            unwoken = ()
            errors.append(f"pending read failed: {type(exc).__name__}")
        for pending in unwoken:
            if not pending.issue:
                continue
            try:
                if wake(ws, pending.issue) and store.mark_woken(
                    pending.idempotency_key, now=now()
                ):
                    woke.append(pending.idempotency_key)
            except Exception:  # noqa: BLE001 - a wake loss is recoverable; the row stays
                continue

    try:
        unrecorded = store.unrecorded_pending(ws)
    except Exception as exc:  # noqa: BLE001
        return SettleOutcome(
            workspace_id=ws,
            reason=SETTLE_NOTHING_PENDING,
            woke=tuple(woke),
            errors=tuple(errors + [f"pending read failed: {type(exc).__name__}"]),
        )

    anchorless = sum(1 for pending in unrecorded if not pending.issue)
    oldest = unrecorded[0].escalated_at if unrecorded else ""
    common = dict(
        workspace_id=ws,
        woke=tuple(woke),
        unrecorded=len(unrecorded),
        anchorless=anchorless,
        oldest_unrecorded_at=oldest,
        errors=tuple(errors),
    )

    writable = [pending for pending in unrecorded if pending.issue]
    if not writable:
        reason = SETTLE_ANCHOR_UNRESOLVED if anchorless else SETTLE_NOTHING_PENDING
        return SettleOutcome(reason=reason, **common)  # type: ignore[arg-type]

    # The canonical predicate, NOT a re-derivation. ``budget_spent`` is
    # ``mutated OR uncertain`` (``pass_external_budget``), and every sibling leg
    # (``workspace_retire_leg`` / ``workspace_hibernate_leg`` /
    # ``retire_supervisor_wiring``) defers on both. An earlier version of this module
    # checked ``mutated`` alone, which let this rail write behind another leg's UNCERTAIN
    # partial effect — review j#110132 finding_1.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.pass_external_budget import (  # noqa: E501
        budget_spent,
    )

    if budget is not None and budget_spent(budget):
        return SettleOutcome(reason=SETTLE_BUDGET_SPENT, **common)  # type: ignore[arg-type]
    if write_journal is None:
        return SettleOutcome(reason=SETTLE_WRITE_REFUSED, **common)  # type: ignore[arg-type]

    # Oldest first: a newer stall must not repeatedly overtake an older one.
    target = writable[0]
    try:
        result = write_journal(target)
    except Exception as exc:  # noqa: BLE001 - a writer must never abort a supervisor pass
        # A raise is indistinguishable from a landed POST, so it is UNCERTAIN — the same
        # reading ``budgeted_sender`` applies to a sender that raises after the send edge.
        result = JournalWriteResult(
            outcome=WRITE_UNCERTAIN, reason=f"writer_raised_{type(exc).__name__}"
        )

    # Spend the budget BEFORE any further store work. A store error below must not be able
    # to leave a possibly-landed external mutation unaccounted for, because the next
    # workspace in this pass reads this same dict.
    if budget is not None:
        if result.outcome == WRITE_RECORDED:
            budget["mutated"] = True
        elif result.outcome == WRITE_UNCERTAIN:
            budget["uncertain"] = True

    if result.outcome != WRITE_RECORDED:
        settle_reason = (
            SETTLE_WRITE_UNCERTAIN
            if result.outcome == WRITE_UNCERTAIN
            else SETTLE_WRITE_REFUSED
        )
        try:
            store.record_attempt(
                target.idempotency_key, result.reason or settle_reason, now=now()
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"attempt record failed: {type(exc).__name__}")
        return SettleOutcome(
            workspace_id=ws,
            reason=settle_reason,
            woke=tuple(woke),
            unrecorded=len(unrecorded),
            anchorless=anchorless,
            oldest_unrecorded_at=oldest,
            errors=tuple(errors),
        )

    journal_id = result.journal_id
    try:
        store.mark_recorded(target.idempotency_key, journal_id, now=now())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"record mark failed: {type(exc).__name__}")

    recorded = PendingEscalation(
        **{**target.__dict__, "journal_id": journal_id, "written_at": now()}
    )

    # Only now may a coordinator be woken: the journal it will be told to read exists.
    if wake is not None and target.issue:
        try:
            if wake(ws, target.issue) and store.mark_woken(
                target.idempotency_key, now=now()
            ):
                woke.append(target.idempotency_key)
        except Exception:  # noqa: BLE001 - the journal is durable; the wake retries later
            pass

    return SettleOutcome(
        workspace_id=ws,
        reason=SETTLE_RECORDED,
        recorded=recorded,
        woke=tuple(woke),
        unrecorded=len(unrecorded) - 1,
        anchorless=anchorless,
        oldest_unrecorded_at=oldest,
        errors=tuple(errors),
    )


__all__ = (
    "SETTLE_ANCHOR_UNRESOLVED",
    "SETTLE_BUDGET_SPENT",
    "SETTLE_NOTHING_PENDING",
    "SETTLE_RECORDED",
    "SETTLE_WRITE_REFUSED",
    "SETTLE_WRITE_UNCERTAIN",
    "WRITE_NOT_SENT",
    "WRITE_OUTCOMES",
    "WRITE_RECORDED",
    "WRITE_UNCERTAIN",
    "EscalationPassOutcome",
    "JournalWriteResult",
    "JournalWriter",
    "ObservedUnit",
    "SettleOutcome",
    "WakeEnqueue",
    "apply_escalation_gate",
    "settle_pending_escalations",
)
