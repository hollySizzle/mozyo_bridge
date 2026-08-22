"""The stall-watch leg of one bounded supervisor pass (Redmine #15855).

#15855 j#110121-1 settled that the watcher gets **no OS registration of its own**: #15192
fixed "exactly one owned registration per host" and ships a one-way migration that removes
any second one, so a stall-watch timer would have to reverse a live decision. Instead the
watcher folds into the bounded sweep the single owned unit already runs, exactly as the
retire and hibernate legs do — and the ~5-minute period comes from this module's own
watermark rather than from a second timer.

The leg is deliberately shallow. Everything it decides lives elsewhere and is separately
testable:

- **whether to run** — :func:`stall_watch_due` against the operator's cadence;
- **what to look at** — :func:`discover_watch_units`, the four-filter join;
- **what the screens mean** — the #15843 sensor and classifier, unchanged;
- **whether that is an escalation** — :func:`apply_escalation_gate`;
- **whether to write it down** — :func:`settle_pending_escalations`, budget-gated.

What is left here is the composition and one genuinely local concern: turning a canonical
gate write into a **journal id**.

Why the write needs a readback, not a boolean
---------------------------------------------
:func:`emit_gate_record` reports ``recorded: True`` and a redacted ``redmine:issue=<id>``
pointer — it does not return the journal it created. j#110121-6 requires the wake to happen
only after a journal **id** is read back, and for a good reason: "the POST did not raise" is
not the same as "a journal exists", and a pass that treated it as such would, after an
uncertain write, write a second journal on the next pass.

What the pass costs while it runs
---------------------------------
The sensor samples each screen twice around one interval, and that interval is a real
``sleep`` — ``DEFAULT_SAMPLE_INTERVAL_SECONDS`` (50s) — slept **once per pass**, not once
per unit (:func:`run_stall_watch_pass`). So a due tick blocks the workspace's sweep for
about that long while holding its lease. Two facts make that safe rather than incidental:

- the lease TTL is ``SUPERVISOR_LEASE_TTL_SECONDS`` (300s), six times the interval, so the
  sample cannot age a lease out from under the leg and hand the workspace to a duplicate
  supervisor mid-pass;
- the leg is due roughly every 300s against a 180s tick, so the blocking pass is a minority
  of ticks and the ones in between return immediately from the cadence check.

The interval is deliberately NOT shortened for this caller. It is #15843's calibrated
value, and the whole classification — what counts as chrome movement versus a frozen screen
— is tuned against it; re-picking it here would re-litigate that calibration in a module
that does not own it.

:func:`journal_id_carrying_key` closes that by making the escalation note *self-identifying*:
the body carries ``idempotency_key: <key>`` (rendered by
:func:`render_escalation_body`), so the id can be recovered by scanning the issue's journals
for that key. The scan runs **before** the write as well as after it, which is what makes
the whole rail idempotent across a crash: a firing whose journal already landed is bound to
it and never written again, whatever the local store believes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from mozyo_bridge.core.state.stall_escalation import (
    PendingEscalation,
    StallEscalationStore,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_escalation_pass import (  # noqa: E501
    ObservedUnit,
    apply_escalation_gate,
    settle_pending_escalations,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_pass import (  # noqa: E501
    StallWatchTarget,
    run_stall_watch_pass,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_phase import (  # noqa: E501
    WatchUnit,
    discover_watch_units,
    stall_watch_due,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_escalation_note import (  # noqa: E501
    STALL_ESCALATION_GATE,
    render_escalation_body,
    render_policy_id,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_watch_policy import (  # noqa: E501
    StallWatchPolicy,
)

#: Leg outcomes (fixed vocabulary; a status surface branches on these, not on prose).
LEG_DISABLED = "policy_disabled"
LEG_NOT_DUE = "within_cadence"
LEG_NOTHING_TO_WATCH = "no_units_in_scope"
LEG_NO_READER = "no_screen_reader"
LEG_RAN = "ran"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StallWatchLegOutcome:
    """What one pass's stall-watch leg did. Carries no pane content."""

    workspace_id: str
    reason: str
    discovery: Optional[object] = None
    observed: Optional[object] = None
    settled: Optional[object] = None

    @property
    def spent_budget(self) -> bool:
        return bool(getattr(self.settled, "spent_budget", False))

    def telemetry(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "stall_watch_reason": self.reason,
            "spent_budget": self.spent_budget,
        }
        for key, value in (
            ("discovery", self.discovery),
            ("observed", self.observed),
            ("settled", self.settled),
        ):
            if value is not None and hasattr(value, "telemetry"):
                payload[key] = value.telemetry()
        return payload


def journal_id_carrying_key(source: object, issue: str, key: str) -> str:
    """The id of the issue journal whose note carries ``key``, or ``""``.

    The readback half of the write fence. Matching on the rendered
    ``idempotency_key: <key>`` line rather than on prose or on a timestamp is what makes it
    exact: the key is derived from the run's identity, so exactly one journal can ever
    carry it, and finding it is proof the durable record holds this firing.

    Fail-soft by design — an unreadable source returns ``""``, which reports the firing as
    *not yet recorded*. That direction retries a write that may be redundant; the opposite
    direction would bind a firing to a journal that does not exist and wake a coordinator to
    read nothing.
    """
    if source is None or not key:
        return ""
    try:
        entries = source.read_entries(str(issue))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - an unreadable issue is "not recorded", never a crash
        return ""
    needle = f"idempotency_key: {key}"
    best = ""
    for entry in entries or ():
        notes = str(getattr(entry, "notes", "") or "")
        if needle in notes:
            candidate = str(getattr(entry, "journal_id", "") or "")
            # Last match wins: a duplicate can only arise from a pre-fence write, and the
            # later journal is the one a reader following the issue will act on.
            if candidate:
                best = candidate
    return best


def build_journal_writer(
    *,
    policy: StallWatchPolicy,
    transport: object,
    source: object,
    emit: Optional[Callable[..., object]] = None,
) -> Callable[[PendingEscalation], "tuple[str, str]"]:
    """Bind the canonical gate writer into the ``(journal_id, reason)`` seam.

    ``transport`` is the credential-gated, opt-in note transport every durable write in this
    repo already uses; ``None`` means the opt-in is unset and the writer refuses with
    ``write_optin_unset`` — never a silent success. ``source`` is the same workspace's
    Redmine journal source, used for the readback.
    """
    if emit is None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_gate_record import (  # noqa: E501
            emit_gate_record,
        )

        emit = emit_gate_record

    policy_id = render_policy_id(
        cadence_seconds=policy.cadence_seconds,
        threshold=policy.threshold,
        source=policy.source,
    )

    def _write(pending: PendingEscalation) -> "tuple[str, str]":
        # Readback FIRST: a firing whose journal already landed (an uncertain write, a
        # crash after the POST) is bound to it and never written twice.
        existing = journal_id_carrying_key(source, pending.issue, pending.idempotency_key)
        if existing:
            return existing, "already_recorded"

        body = render_escalation_body(
            issue=pending.issue,
            slot_label=pending.slot_label,
            generation=pending.generation,
            target=pending.target,
            provider_id=pending.role,
            stall_class=pending.stall_class,
            prescription=pending.prescription,
            consecutive=pending.consecutive,
            first_observed_at=pending.first_observed_at,
            last_observed_at=pending.escalated_at,
            policy_id=policy_id,
            idempotency_key=pending.idempotency_key,
            matched_id=pending.matched_id,
            evidence_tier=pending.evidence_tier,
        )
        try:
            receipt = emit(
                pending.issue,
                STALL_ESCALATION_GATE,
                body=body,
                transport=transport,
                marker_fields={"blocker_recorded": True},
            )
        except Exception as exc:  # noqa: BLE001 - a writer never aborts a supervisor pass
            return "", f"writer_raised_{type(exc).__name__}"

        if not getattr(receipt, "recorded", False):
            return "", str(getattr(receipt, "reason", "write_refused"))

        # The POST returned; now prove a journal exists and learn its id.
        journal_id = journal_id_carrying_key(
            source, pending.issue, pending.idempotency_key
        )
        if not journal_id:
            # Posted but unverifiable. Reported as NOT recorded so the wake does not fire;
            # the next pass's readback binds it without a second write.
            return "", "readback_unverified"
        return journal_id, "recorded"

    return _write


def run_stall_watch_leg(
    *,
    workspace_id: str,
    store: StallEscalationStore,
    policy: StallWatchPolicy,
    inventory_rows: Callable[[], Sequence[object]],
    read_screen: Optional[Callable[[str], "tuple[bool, str]"]],
    write_journal: Optional[Callable[[PendingEscalation], "tuple[str, str]"]] = None,
    wake: Optional[Callable[[str, str], bool]] = None,
    generation_for: Optional[Callable[[str], str]] = None,
    issue_for: Optional[Callable[[str], str]] = None,
    provider_for: Optional[Callable[[str], str]] = None,
    budget: Optional[dict] = None,
    signatures: object = None,
    clock: Callable[[], float] = None,
    sleep: Callable[[float], None] = None,
    now: Callable[[], datetime] = _utc_now,
    sample_interval_seconds: Optional[float] = None,
) -> StallWatchLegOutcome:
    """Run one stall-watch leg inside an already-leased workspace sweep.

    Returns without touching anything when the policy is disabled or the cadence has not
    elapsed — the common case on most ticks, and the reason folding into the existing unit
    costs almost nothing.

    Note the ordering of the two settle-relevant facts: the watermark is marked **after**
    the observation, so a crash mid-pass re-observes rather than skipping a cadence window.
    Re-observing is harmless (the fold is idempotent for an unchanged screen); skipping is
    a stall nobody looked at.
    """
    ws = str(workspace_id or "").strip()
    # ONE instant for the whole pass. Every stamp this leg writes -- the cadence watermark,
    # each fold's observed_at, the firing's escalated_at -- comes from here, so a pass is a
    # single point in time in the durable record rather than a smear across however long the
    # screen sampling took. It is also what makes the cadence testable against an injected
    # clock instead of the wall clock.
    moment = now()
    stamp = moment.isoformat(timespec="seconds")
    verdict = stall_watch_due(
        policy=policy, last_pass_at=store.last_pass_at(ws) if ws else "", now=moment
    )
    if not verdict.due:
        return StallWatchLegOutcome(
            workspace_id=ws,
            reason=LEG_DISABLED if not policy.enabled else LEG_NOT_DUE,
        )

    try:
        rows = list(inventory_rows() or ())
    except Exception:  # noqa: BLE001 - an unreadable inventory watches nothing this pass
        rows = []
    discovery = discover_watch_units(
        rows,
        workspace_id=ws,
        policy=policy,
        generation_for=generation_for,
        issue_for=issue_for,
        provider_for=provider_for,
    )
    if not discovery.units:
        store.mark_pass(ws, now=stamp)
        return StallWatchLegOutcome(
            workspace_id=ws, reason=LEG_NOTHING_TO_WATCH, discovery=discovery
        )
    if read_screen is None:
        # A watcher that cannot read is blocked, not quiet: the cadence is NOT marked, so
        # the next tick tries again instead of pretending this window was observed.
        #
        # The backlog is still settled. Writing an already-fired escalation needs a Redmine
        # transport, not a screen — so letting a broken screen reader also stop the durable
        # record from being written would strand exactly the reports this rail exists to
        # deliver, at exactly the moment the cockpit is least healthy.
        return StallWatchLegOutcome(
            workspace_id=ws,
            reason=LEG_NO_READER,
            discovery=discovery,
            settled=settle_pending_escalations(
                workspace_id=ws,
                store=store,
                budget=budget,
                write_journal=write_journal,
                wake=wake,
                now=lambda: stamp,
            ),
        )

    import time as _time

    if signatures is None:
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_pass import (  # noqa: E501
            load_default_signatures,
        )

        signatures = load_default_signatures()

    kwargs = {}
    if sample_interval_seconds is not None:
        kwargs["interval_seconds"] = float(sample_interval_seconds)
    observations = run_stall_watch_pass(
        tuple(
            StallWatchTarget(target=unit.locator, provider_id=unit.provider_id)
            for unit in discovery.units
        ),
        read_screen=read_screen,
        clock=clock or _time.monotonic,
        sleep=sleep or _time.sleep,
        signatures=signatures,
        **kwargs,
    )

    by_locator = {unit.locator: unit for unit in discovery.units}
    observed_units = []
    for observation in observations:
        unit: Optional[WatchUnit] = by_locator.get(observation.target)
        if unit is None:
            continue
        observed_units.append(
            ObservedUnit(
                identity=unit.identity,
                observation=observation,
                issue=unit.issue,
            )
        )

    observed = apply_escalation_gate(
        observed_units,
        workspace_id=ws,
        store=store,
        threshold=policy.threshold,
        now=lambda: stamp,
        # The join above observed every unit the policy admits, so a slot missing from this
        # pass is genuinely gone rather than merely unwatched.
        forget_absent=True,
    )
    store.mark_pass(ws, now=stamp)

    settled = settle_pending_escalations(
        workspace_id=ws,
        store=store,
        budget=budget,
        write_journal=write_journal,
        wake=wake,
        now=lambda: stamp,
    )
    return StallWatchLegOutcome(
        workspace_id=ws,
        reason=LEG_RAN,
        discovery=discovery,
        observed=observed,
        settled=settled,
    )


__all__ = (
    "LEG_DISABLED",
    "LEG_NOTHING_TO_WATCH",
    "LEG_NOT_DUE",
    "LEG_NO_READER",
    "LEG_RAN",
    "StallWatchLegOutcome",
    "build_journal_writer",
    "journal_id_carrying_key",
    "run_stall_watch_leg",
)
