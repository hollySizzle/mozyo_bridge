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

:func:`read_journal_authority` closes that by making the escalation note *self-identifying*:
the body carries the firing's key in a canonical field (rendered by
:func:`render_escalation_body`), so the id can be recovered by scanning the issue's journals
for the journal that declares it. The scan runs **before** the write as well as after it,
which is what makes the whole rail idempotent across a crash: a firing whose journal already
landed is bound to it and never written again, whatever the local store believes.

What that scan can and cannot establish is worth stating exactly, because two rounds were
lost to overstating it. It establishes that a journal declaring this firing EXISTS. It does
not establish that this rail WROTE it — the key is public and deterministic, so anyone who
can comment on the issue can declare it too. Several claimants are therefore refused rather
than resolved, and the author is carried for the issuer binding whose source is still being
decided (j#110297).

A write is therefore classified three ways, not two (review j#110132 finding_1). "No journal
id" is not one situation: a refused write never reached Redmine, while a POST that returned
but could not be read back MAY have created a journal. The first leaves the shared pass
budget untouched; the second must spend it as UNCERTAIN, or the next workspace in the same
bounded pass performs a second external mutation behind an unknown partial effect. See
:data:`DETERMINISTIC_NO_SEND_REASONS` for why the deterministic set is an allowlist.

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

"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from mozyo_bridge.core.state.stall_escalation import (
    PendingEscalation,
    StallEscalationStore,
)
from mozyo_bridge.core.state.stall_pending_contract import canonical_idempotency_key
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_escalation_pass import (  # noqa: E501
    WRITE_NOT_SENT,
    WRITE_RECORDED,
    WRITE_UNCERTAIN,
    JournalWriteResult,
    JournalWriter,
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
    note_declares_key,
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


#: Exactly ONE journal carries this firing; ``journal_id`` holds its id.
READBACK_FOUND = "found"
#: The authority was asked and no journal carries this firing.
READBACK_ABSENT = "absent"
#: The authority was NOT asked: the pass has spent its provider-read budget.
READBACK_CAPPED = "read_cap_reached"
#: The authority was asked and could not answer (no source, or the read raised).
READBACK_UNREADABLE = "unreadable"
#: SEVERAL journals claim this firing. Nobody can say which is the record, so nothing may
#: be built on any of them (review j#110293 finding_authorityforgery).
READBACK_AMBIGUOUS = "ambiguous_authority"

READBACK_OUTCOMES: frozenset[str] = frozenset(
    {
        READBACK_FOUND, READBACK_ABSENT, READBACK_CAPPED, READBACK_UNREADABLE,
        READBACK_AMBIGUOUS,
    }
)


@dataclass(frozen=True)
class ReadbackResult:
    """What the external authority said, and whether it was asked at all.

    Five distinguishable answers, because collapsing any pair of them has already cost this
    issue a round. "No journal carries this" and "we never looked" are opposite facts that a
    boolean cannot tell apart, and only the first may ever authorise anything. "Several
    journals claim it" is a third fact again: it is not an absence, and it is certainly not
    a find.
    """

    outcome: str
    journal_id: str = ""
    #: The provider's opaque author id for the found journal (``""`` when not applicable).
    #: Carried because a note that merely SAYS it is the record proves nothing — anyone who
    #: can write a note can write its fields — while the author is a fact the provider
    #: authenticates (the reason ``RedmineJournalEntry.author_id`` exists, #14661 j#92494).
    author_id: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in READBACK_OUTCOMES:
            raise ValueError(f"unknown readback outcome {self.outcome!r}")
        if bool(self.journal_id) != (self.outcome == READBACK_FOUND):
            # An id on a result that did not find one is a claim with no answer behind it,
            # and an empty id on a FOUND result is a found nothing. Enforced in the type
            # rather than trusted at each call site: a caller-side `if result.found` guard
            # is unmeasurable while nothing can construct the shape it defends against.
            raise ValueError(
                f"a {self.outcome!r} readback must "
                f"{'carry' if self.outcome == READBACK_FOUND else 'not carry'} a journal id"
            )
        if self.author_id and self.outcome != READBACK_FOUND:
            raise ValueError(f"a {self.outcome!r} readback must not carry an author")

    @property
    def found(self) -> bool:
        return self.outcome == READBACK_FOUND

    @property
    def asked(self) -> bool:
        """Whether the authority was actually consulted.

        A cap and a missing source are not asks. An UNREADABLE answer is not an ask either:
        the request left, the provider refused it, and nothing came back. Reporting that as
        "asked and answered nothing" is what review j#110293 finding_unreadablecollapse
        found — a lie that costs nothing today and misleads the next caller.
        """
        return self.outcome in (READBACK_FOUND, READBACK_ABSENT, READBACK_AMBIGUOUS)


def read_journal_authority(source: object, issue: str, key: str) -> ReadbackResult:
    """Ask the external system which journal carries ``key``. The rail's ONLY authority.

    Matching is :func:`note_declares_key` — the canonical field parser that lives beside the
    renderer — compared for EQUALITY with the firing's own key. A non-canonical ``key``
    matches nothing: the request itself is refused rather than compared, so a caller cannot
    go fishing with a prefix.

    **Several claimants is a refusal, not a choice.** This used to take the last match, on
    the reasoning that a duplicate could only come from a pre-fence write. That reasoning
    omitted the attacker this rail's own threat model declares: anyone who can comment on
    the issue can post the same canonical field line, and last-match-wins handed them the
    binding and the coordinator's wake pointer (review j#110293 finding_authorityforgery).
    Nobody can say which of two claimants is the record, so nothing is built on either.

    **An unreadable provider is not an absence.** The exception used to be swallowed into
    ``""``, which the seam then reported as "asked, and no journal carries this" — the exact
    collapse the typed contract was introduced to prevent (finding_unreadablecollapse).

    What this still cannot do, stated plainly: it cannot tell OUR journal from a forgery
    that copies the field, because the idempotency key is public and deterministic. Only the
    author can, and the expected issuer is a pending design decision (j#110297). The author
    is carried here so that decision plugs in rather than rebuilds.
    """
    if source is None or not key or not canonical_idempotency_key(key):
        return ReadbackResult(outcome=READBACK_UNREADABLE)
    try:
        entries = source.read_entries(str(issue))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - typed, never swallowed into an absence
        return ReadbackResult(outcome=READBACK_UNREADABLE)
    claimants = [
        entry for entry in entries or ()
        if note_declares_key(getattr(entry, "notes", "")) == key
        and str(getattr(entry, "journal_id", "") or "")
    ]
    if not claimants:
        return ReadbackResult(outcome=READBACK_ABSENT)
    if len(claimants) > 1:
        return ReadbackResult(outcome=READBACK_AMBIGUOUS)
    (entry,) = claimants
    return ReadbackResult(
        outcome=READBACK_FOUND,
        journal_id=str(getattr(entry, "journal_id", "") or ""),
        author_id=str(getattr(entry, "author_id", "") or ""),
    )


def build_journal_readback(
    *,
    source: object,
    budget: Optional[dict] = None,
    read_cap: Optional[int] = None,
    expected_issuer: str = "",
) -> Callable[[str, str], ReadbackResult]:
    """The ONE counted, capped way this rail asks Redmine whether a journal exists.

    Every readback in the leg goes through this seam — the writer's pre-POST idempotency
    check, the writer's post-POST verification, and the wake admission's verifier. Before,
    only the verifier consulted ``budget["reads"]``; the writer called the underlying lookup
    directly, so a pass already at the cap still performed two real provider reads and the
    shared counter never moved (review j#110281 finding_readcap). A cap one caller honours
    and another walks past is not a cap, and the telemetry that reports it is fiction.

    The counter and the ceiling are the pass-wide ones the hibernate leg threads
    (``budget["reads"]`` / :data:`MAX_PROVIDER_READS_PER_PASS`) — imported rather than
    re-picked, so "how many provider reads may a bounded pass make" stays ONE decision.

    Positive answers are cached for the pass; nothing else is. A journal, once written, does
    not stop existing, so a hit is free to reuse — while caching a MISS would make the firing
    this pass is about to write unverifiable for the rest of the pass, because the miss would
    have been recorded before the write. An ambiguity or a provider failure is likewise not
    cached: neither is a fact about the world that stays true.

    ``expected_issuer`` is the provider author id this rail writes as. When it is known, a
    claimant authored by anyone else is not a claimant at all — that is what makes a forged
    note harmless rather than merely ambiguous. It is empty today: where the rail learns its
    own issuer is the design decision raised in j#110297, and until that is settled this
    parameter is the seam that decision plugs into. The residual while it is empty is
    recorded there and in the spec, not hidden here.
    """
    if read_cap is None:
        # Lazily, the way this rail already imports `budget_spent`: the read budget stays one
        # shared decision and f_150 takes no load-time dependency on an f_140 wiring module.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_supervisor_wiring import (  # noqa: E501
            MAX_PROVIDER_READS_PER_PASS,
        )

        read_cap = MAX_PROVIDER_READS_PER_PASS
    cache: dict[tuple[str, str], ReadbackResult] = {}

    def read(issue: str, key: str) -> ReadbackResult:
        issue, key = str(issue or ""), str(key or "")
        if not issue or not key:
            return ReadbackResult(outcome=READBACK_UNREADABLE)
        if (issue, key) in cache:
            return cache[(issue, key)]
        if source is None:
            return ReadbackResult(outcome=READBACK_UNREADABLE)
        if budget is not None and int(budget.get("reads", 0)) >= read_cap:
            return ReadbackResult(outcome=READBACK_CAPPED)
        if budget is not None:
            budget["reads"] = int(budget.get("reads", 0)) + 1
        result = read_journal_authority(source, issue, key)
        if result.found and expected_issuer and result.author_id != expected_issuer:
            # Authored by someone else: not this rail's record, so not a claimant. Refused
            # rather than reported as an absence, because "a journal claiming this firing
            # exists and we did not write it" is an operator-visible fact.
            result = ReadbackResult(outcome=READBACK_AMBIGUOUS)
        if result.found:
            cache[(issue, key)] = result
        return result

    return read


def build_journal_verifier(
    *, readback: Callable[[str, str], ReadbackResult]
) -> Callable[[object], str]:
    """The wake admission's authority: which journal Redmine says carries a firing.

    A thin adapter over :func:`build_journal_readback` — the same seam, the same counter and
    the same cap the writer uses. Anything that is not a confirmed FOUND answers ``""``,
    which :func:`admit_wake` reads as a refused wake: a firing waits for the next pass
    rather than being woken on a claim nobody checked, and the refusal is counted in the
    settle telemetry so a capped pass is visible rather than silent.

    The seam is a required argument rather than something this function can build for
    itself. A verifier that could quietly construct its own would be a SECOND seam in the
    same pass — its own cache, its own counter — which is finding_readcap's exact shape one
    level up. One pass, one seam, passed in.
    """

    def verify(pending: object) -> str:
        result = readback(
            str(getattr(pending, "issue", "") or ""),
            str(getattr(pending, "idempotency_key", "") or ""),
        )
        return result.journal_id if result.found else ""

    return verify


#: Refusal reasons that prove NOTHING reached Redmine. Every one of these is decided before
#: (or by) the server without creating a journal: the write opt-in is unset, the transport
#: has no base URL or credential, the server rejected the caller, or the sink had no anchor
#: to write to.
#:
#: The set is an ALLOWLIST on purpose. Anything not named here — a transport error, a
#: timeout, a reason a future transport invents — is treated as UNCERTAIN, because the cost
#: of the two mistakes is not symmetric: calling a landed write "refused" leaves a real
#: external mutation off the shared pass budget and lets a second one happen in the same
#: bounded pass (review j#110132 finding_1), while calling a refused write "uncertain" only
#: costs this pass its remaining mutation slot.
DETERMINISTIC_NO_SEND_REASONS: frozenset[str] = frozenset(
    {
        "write_optin_unset",
        "base_url_unset",
        "credential_missing",
        "unauthorized",
        "no_anchor",
        "disabled",
        "unsupported_source",
        # Not transport reasons: the writer's own refusals when the idempotency check could
        # not run (the pass spent its provider-read budget) or could not conclude (several
        # journals claim the firing). Both belong in this allowlist because they prove the
        # same thing every other member does — nothing reached Redmine (review j#110281
        # finding_readcap, j#110293 finding_authorityforgery).
        READBACK_CAPPED,
        READBACK_AMBIGUOUS,
    }
)


def classify_refusal(reason: str) -> str:
    """Map a gate-record refusal reason onto a budget outcome (fail-safe on unknowns)."""
    return (
        WRITE_NOT_SENT
        if str(reason or "") in DETERMINISTIC_NO_SEND_REASONS
        else WRITE_UNCERTAIN
    )


def build_journal_writer(
    *,
    policy: StallWatchPolicy,
    transport: object,
    readback: Callable[[str, str], ReadbackResult],
    emit: Optional[Callable[..., object]] = None,
) -> JournalWriter:
    """Bind the canonical gate writer into the :class:`JournalWriteResult` seam.

    ``transport`` is the credential-gated, opt-in note transport every durable write in this
    repo already uses; ``None`` means the opt-in is unset and the writer refuses with
    ``write_optin_unset`` — never a silent success.

    ``readback`` is the shared counted/capped authority seam
    (:func:`build_journal_readback`), and it is REQUIRED. Passing the same object the
    verifier uses is the whole point: one pass, one counter, one cache. Letting this
    function build its own from a bare source is precisely how the writer came to walk past
    a cap the verifier was honouring (review j#110281 finding_readcap).
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

    def _write(pending: PendingEscalation) -> JournalWriteResult:
        # Readback FIRST: a firing whose journal already landed (an uncertain write, a
        # crash after the POST) is bound to it and never written twice.
        existing = readback(pending.issue, pending.idempotency_key)
        if existing.found:
            return JournalWriteResult(
                outcome=WRITE_RECORDED,
                journal_id=existing.journal_id,
                reason="already_recorded",
            )
        if existing.outcome == READBACK_AMBIGUOUS:
            # Several journals claim this firing. Posting would add a third and binding to
            # any of them would be a guess, so the firing stays pending and visible. A
            # deterministic zero-send: nothing reached Redmine (review j#110293
            # finding_authorityforgery).
            return JournalWriteResult(
                outcome=WRITE_NOT_SENT, reason=READBACK_AMBIGUOUS
            )
        if existing.outcome == READBACK_CAPPED:
            # The pass has spent its provider-read budget, so the idempotency check could
            # not run. Posting anyway would risk a SECOND journal for one firing, and the
            # cost of waiting is nothing: the firing stays pending and the next pass writes
            # it. This is a deterministic zero-send — nothing reached Redmine — so the
            # external-mutation budget is untouched (review j#110281 finding_readcap).
            #
            # Note the asymmetry with an UNREADABLE source below, which is deliberate. A cap
            # is a deferral: another pass follows in minutes and the report is not lost. An
            # outage is not: refusing there would mean never reporting the stall, which is
            # the exact failure this whole issue exists to end.
            return JournalWriteResult(
                outcome=WRITE_NOT_SENT, reason=READBACK_CAPPED
            )

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
            # The request may already have left; treat it as a possible external mutation.
            return JournalWriteResult(
                outcome=WRITE_UNCERTAIN, reason=f"writer_raised_{type(exc).__name__}"
            )

        if not getattr(receipt, "recorded", False):
            reason = str(getattr(receipt, "reason", "write_refused"))
            return JournalWriteResult(outcome=classify_refusal(reason), reason=reason)

        # The POST returned; now prove a journal exists and learn its id.
        verified = readback(pending.issue, pending.idempotency_key)
        if not verified.found:
            # Posted but unverifiable. NOT recorded — so the wake does not fire and the next
            # pass's readback binds it without a second write — but UNCERTAIN rather than
            # refused, because the POST returned and a journal may well exist. Reporting it
            # as a plain refusal (the pre-j#110132 behaviour) left a possibly-landed
            # external mutation unaccounted for in the shared pass budget.
            #
            # A cap reached HERE is the same situation, not a deferral: the POST already
            # happened, so "we never looked" and "we looked and saw nothing" have identical
            # consequences for this pass — an external effect that cannot be accounted for
            # (review j#110281 finding_readcap).
            return JournalWriteResult(
                outcome=WRITE_UNCERTAIN, reason="readback_unverified"
            )
        return JournalWriteResult(
            outcome=WRITE_RECORDED, journal_id=verified.journal_id, reason="recorded"
        )

    return _write


def _record_discovery(store, workspace_id: str, discovery, stamp: str) -> None:
    """Persist the pass's coverage counts (best-effort; never fails a pass).

    Status must be answerable at any instant, not only just after a sweep, and it must not
    answer by reading panes — so the leg leaves the counts behind (review j#110146
    finding_1). A failure here loses observability, not correctness, so it is swallowed.
    """
    try:
        store.record_discovery(
            workspace_id,
            candidates=discovery.candidates,
            watched=discovery.watched,
            out_of_reach=discovery.out_of_reach,
            dropped=dict(discovery.dropped),
            now=stamp,
        )
    except Exception:  # noqa: BLE001 - observability loss never breaks the sweep
        pass


def run_stall_watch_leg(
    *,
    workspace_id: str,
    store: StallEscalationStore,
    policy: StallWatchPolicy,
    inventory_rows: Callable[[], Sequence[object]],
    read_screen: Optional[Callable[[str], "tuple[bool, str]"]],
    write_journal: Optional[JournalWriter] = None,
    wake: Optional[Callable[[str, str], bool]] = None,
    #: The wake admission's authority (:func:`build_journal_verifier`). Absent, no wake is
    #: admitted at all: an unverifiable claim is not a weaker reason to wake a coordinator,
    #: it is no reason (review j#110254 finding_stateauthority).
    verify_journal: Optional[Callable[[object], str]] = None,
    generation_for: Optional[Callable[[str], str]] = None,
    issue_for: Optional[Callable[[str], str]] = None,
    provider_for: Optional[Callable[[str], str]] = None,
    #: ``(issue, role, locator) -> marker``. Supplies the evidence the ``unsent_composer``
    #: classification needs; without it that class is unreachable (j#110132 finding_2).
    body_marker_for: Optional[Callable[[str, str, str], str]] = None,
    budget: Optional[dict] = None,
    signatures: object = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
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
    # Persist the coverage BEFORE the early return, so "watched nothing, and here is why"
    # is readable from `--status` even on a pass that found no units at all.
    _record_discovery(store, ws, discovery, stamp)

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
                verify_journal=verify_journal,
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
    def _marker(unit: WatchUnit) -> str:
        if body_marker_for is None:
            return ""
        try:
            return str(
                body_marker_for(unit.issue, unit.identity.role, unit.locator) or ""
            )
        except Exception:  # noqa: BLE001 - unresolved evidence is simply no evidence
            return ""

    observations = run_stall_watch_pass(
        tuple(
            StallWatchTarget(
                target=unit.locator,
                provider_id=unit.provider_id,
                # Without this the sensor's rule 5 can never match, so the periodic path
                # would report every swallowed-Enter stall as patient-wait indeterminate.
                pending_body_marker=_marker(unit),
            )
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
        verify_journal=verify_journal,
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
    "DETERMINISTIC_NO_SEND_REASONS",
    "classify_refusal",
    "LEG_DISABLED",
    "LEG_NOTHING_TO_WATCH",
    "LEG_NOT_DUE",
    "LEG_NO_READER",
    "LEG_RAN",
    "StallWatchLegOutcome",
    "READBACK_ABSENT",
    "READBACK_AMBIGUOUS",
    "READBACK_CAPPED",
    "READBACK_FOUND",
    "READBACK_OUTCOMES",
    "READBACK_UNREADABLE",
    "ReadbackResult",
    "build_journal_readback",
    "build_journal_verifier",
    "build_journal_writer",
    "read_journal_authority",
    "run_stall_watch_leg",
)
