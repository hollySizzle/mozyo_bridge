"""The auto-hibernate actuation leg (Redmine #14219, tranche T2a).

One bounded pass: given the pass's approved :class:`HibernateCandidate`s, drive the REAL public
``SublaneHibernateUseCase`` — its preflight, its #13843 T0/T1/T2 TOCTOU fence, and its disposition
CAS — and actuate **at most one** lifecycle hibernate mutation. The safety invariants the design
ruling (#14219 j#85459) and the T1 approval (j#85506) require:

  * **≤1 mutation per pass.** The candidates are tried in deterministic order; the leg stops
    attempting the moment one hibernate is applied. A blocked candidate does not starve the rest
    (the leg moves on), but only one mutation ever lands.
  * **Lease-gated.** The caller's ``lease_renew_fn`` is checked immediately BEFORE each execute
    (mirroring the callback send-boundary fence); a lost lease stops the pass with zero further
    mutation — a taken-over runner must not double-actuate.
  * **Uncertain / blocked → no blind retry.** A candidate whose preflight blocks, whose CAS loses a
    race, or whose release is withheld is recorded with its typed reason and NOT retried in this
    pass; the next pass re-observes and re-attempts. The classifier and the use case remain the
    authority — this leg never overrides a block.

The obligation flags and the basis-event journal are supplied by injected seams (``obligations_fn``
/ ``journal_fn``); wiring those to durable producers is T2b. This leg performs the actuation only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from ..domain.hibernate_actuation import (
    NO_ACTUATION_DEFERRED_ONE_PER_PASS,
    NO_ACTUATION_NO_CANDIDATE,
    ActionTimeObligations,
    ActuationRequestFields,
    derive_actuation_request,
    order_candidates,
)
from ..domain.hibernate_candidate import HibernateCandidate
from .sublane_hibernate import HibernateOutcome, HibernateRequest, SublaneHibernateUseCase
from .sublane_hibernate_assertions import HibernateAssertions

# Per-candidate attempt outcome kinds (closed vocabulary).
ATTEMPT_ACTUATED = "actuated"
ATTEMPT_PARTIAL = "actuated_release_incomplete"
ATTEMPT_BLOCKED = "blocked"
ATTEMPT_DEFERRED = "deferred"
ATTEMPT_LEASE_LOST = "lease_lost"
ATTEMPT_NO_JOURNAL = "no_basis_journal"
ATTEMPT_STALE = "stale_basis"
# Crash-redrive attempt kinds (review j#86757 R4-F2): finishing a prior pass's interrupted
# release on an already-hibernated row, via the public use case's own redrive path.
ATTEMPT_REDRIVEN = "redriven"
ATTEMPT_REDRIVE_WITHHELD = "redriven_success_withheld"
ATTEMPT_REDRIVE_BLOCKED = "redrive_blocked"
# Review j#86776 R5-F5: a hibernated row whose ``process_release`` is not a canonical release
# state token — an uncertain state the wiring refuses to hand the public rail (zero execute).
ATTEMPT_RELEASE_STATE_UNKNOWN = "release_state_unknown"

# Time-to-drain status (Redmine #14219 T3 review j#87196 R2-F2(a); ruling j#87182 / j#87181): a
# closed enum. ``completed`` = a successful fresh actuation OR a terminal successful redrive with a
# trusted start+end. ``pending`` = blocked / deferred / partial / success-withheld (not a drain
# completion). ``uncertain`` = a lost lease / raised leg (no trusted end). ``unavailable`` = a
# completed actuation whose drain-ready start / end timestamp is missing / malformed / clock-skewed
# (never a guessed 0). No blind completion is inferred.
TTD_COMPLETED = "completed"
TTD_PENDING = "pending"
TTD_UNCERTAIN = "uncertain"
TTD_UNAVAILABLE = "unavailable"

_TTD_COMPLETED_KINDS = frozenset({ATTEMPT_ACTUATED, ATTEMPT_REDRIVEN})
_TTD_UNCERTAIN_KINDS = frozenset({ATTEMPT_LEASE_LOST})


def _parse_iso(value: object) -> "Optional[datetime]":
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def drain_metrics(kind: str, drain_ready_at: str, completed_at: str):
    """The (status, time_to_drain_ms, time_to_disposition_ms) for one attempt — pure, secret-free.

    ``drain_ready_at`` is the basis decision journal's provider ``created_on``; ``completed_at`` is
    the injected supervisor clock read at the attempt's terminal disposition. A missing / malformed /
    skewed pair yields no trusted latency (``None``): a completed actuation then reports
    :data:`TTD_UNAVAILABLE`, never a guessed 0; a pending / uncertain attempt keeps its status with a
    null disposition latency.
    """
    if kind in _TTD_COMPLETED_KINDS:
        base = TTD_COMPLETED
    elif kind in _TTD_UNCERTAIN_KINDS:
        base = TTD_UNCERTAIN
    else:
        base = TTD_PENDING
    start = _parse_iso(drain_ready_at)
    end = _parse_iso(completed_at)
    if start is None or end is None or end < start:
        return (TTD_UNAVAILABLE if base == TTD_COMPLETED else base), None, None
    delta_ms = int((end - start).total_seconds() * 1000)
    if base == TTD_COMPLETED:
        return TTD_COMPLETED, delta_ms, delta_ms
    return base, None, delta_ms


def stamp_drain_metrics(attempt: "HibernateAttempt", drain_ready_at: str, completed_at: str) -> "HibernateAttempt":
    """Return ``attempt`` with its drain-latency status + durations bound (raw timestamps discarded)."""
    status, drain_ms, disp_ms = drain_metrics(attempt.kind, drain_ready_at, completed_at)
    return replace(
        attempt, time_to_drain_status=status, time_to_drain_ms=drain_ms,
        time_to_disposition_ms=disp_ms,
    )


# Fixed reason tokens the leg emits itself (secret-free; the use case's own reasons are already a
# closed vocabulary and are passed through verbatim).
LEG_REASON_LEASE_LOST = "supervisor_lease_lost"
LEG_REASON_SUCCESS_WITHHELD = "release_success_withheld"
LEG_REASON_NOT_ACTUATED = "not_actuated"
LEG_REASON_BASIS_STALE = "basis_stale_since_build"
LEG_REASON_WORKTREE_UNRESOLVED = "candidate_worktree_unresolved"
# Review j#86776 R5-F3: the redrive could not build an HONEST request — no durable intent
# records the basis this hibernated row was CAS'd under (a dependency-park / manual / pre-R5
# row), or the stored intent describes a different cycle than the row. Either is a typed
# zero-close: the redrive never fabricates the basis the row failed to record.
LEG_REASON_REDRIVE_INTENT_ABSENT = "redrive_intent_absent"
LEG_REASON_REDRIVE_INTENT_MISMATCH = "redrive_intent_mismatch"
LEG_REASON_REDRIVE_INTENT_UNREADABLE = "redrive_intent_unreadable"
# Review j#86928 R6-F1: the fresh actuation could NOT durably persist its redrive intent, so the
# irreversible CAS is refused — persisting the intent pre-CAS is what makes a post-CAS crash
# recoverable, and a hibernate that cannot be recovered would strand the live process forever.
LEG_REASON_INTENT_PERSIST_FAILED = "intent_persist_failed"


@dataclass(frozen=True)
class HibernateAttempt:
    """One candidate's outcome this pass. ``reason`` is a closed token; no secrets/paths.

    ``released`` (review j#87176 R2-F2) is the number of process slots this attempt ACTUALLY closed
    — ``len(ReleaseOutcome.closed)``. It is a count, not a status: a lane whose CAS applied but whose
    release was ``not_requested`` (no live slot / dead process) mutated the lane yet freed ZERO
    slots, so ``released == 0`` even though ``kind == actuated``. The report's released-capacity
    metric sums THIS, never the count of actuated attempts, so it never reports freed capacity that
    no process release produced.
    """

    issue: str
    lane: str
    kind: str
    reason: str = ""
    revision: int = 0
    released: int = 0
    #: The drain-latency observability for this candidate (Redmine #14219 T3 review j#87196 R2-F2(a)).
    #: ``time_to_drain_status`` is the closed enum :data:`TTD_COMPLETED` / :data:`TTD_PENDING` /
    #: :data:`TTD_UNCERTAIN` / :data:`TTD_UNAVAILABLE`. ``time_to_drain_ms`` is the drain-ready ->
    #: terminal-success latency in ms, set ONLY for a completed actuation/redrive with a trusted
    #: start+end; ``None`` otherwise. ``time_to_disposition_ms`` is the drain-ready -> typed-terminal
    #: (applied/blocked/uncertain) latency. Both are DERIVED durations, never raw timestamps — the
    #: payload is redaction-safe (no provider ``created_on``, no path).
    time_to_drain_status: str = ""
    time_to_drain_ms: Optional[int] = None
    time_to_disposition_ms: Optional[int] = None

    def as_payload(self) -> dict:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "kind": self.kind,
            "reason": self.reason,
            "revision": self.revision,
            "released": self.released,
            "time_to_drain_status": self.time_to_drain_status,
            "time_to_drain_ms": self.time_to_drain_ms,
            "time_to_disposition_ms": self.time_to_disposition_ms,
        }


@dataclass(frozen=True)
class HibernatePassResult:
    """The bounded pass result: every candidate's attempt, and the (0 or 1) mutation count."""

    attempts: tuple[HibernateAttempt, ...]
    mutations: int
    empty_pass: bool

    def as_payload(self) -> dict:
        return {
            "attempts": [a.as_payload() for a in self.attempts],
            "mutations": self.mutations,
            "empty_pass": self.empty_pass,
        }


def _to_request(fields: ActuationRequestFields) -> HibernateRequest:
    return HibernateRequest(
        issue=fields.issue,
        lane=fields.lane,
        journal=fields.journal,
        assertions=HibernateAssertions(**fields.assertion_flags),
        expected_lane_generation=fields.expected_lane_generation,
        expected_revision=fields.expected_revision,
    )


def _blocked_reason(outcome: HibernateOutcome) -> str:
    """A closed-vocabulary reason for a non-actuated outcome (secret-free).

    The use case's ``blocked_reasons`` are already a closed token set; a withheld success uses a
    fixed token (its ``recovery_detail`` is not surfaced raw). Never a free string.
    """
    if outcome.blocked_reasons:
        return ",".join(outcome.blocked_reasons)
    if outcome.success_withheld:
        return LEG_REASON_SUCCESS_WITHHELD
    return LEG_REASON_NOT_ACTUATED


def _released_count(outcome: HibernateOutcome) -> int:
    """The number of process slots this outcome ACTUALLY closed (review j#87176 R2-F2).

    ``len(ReleaseOutcome.closed)`` — the real freed capacity. A ``not_requested`` release (no live
    slot) closed nothing, so this is ``0`` even for an applied-lane success; a ``partial`` release
    is its actual closed count. ``0`` when no release ran.
    """
    release = getattr(outcome, "release", None)
    if release is None:
        return 0
    return len(getattr(release, "closed", ()) or ())


def run_hibernate_pass(
    candidates: Sequence[HibernateCandidate],
    *,
    refresh_fn: Callable[[HibernateCandidate], Optional[HibernateCandidate]],
    obligations_fn: Callable[[HibernateCandidate], ActionTimeObligations],
    journal_fn: Callable[[HibernateCandidate], str],
    use_case: Optional[SublaneHibernateUseCase] = None,
    lease_renew_fn: Callable[[], bool],
    use_case_fn: Optional[Callable[[HibernateCandidate], Optional[SublaneHibernateUseCase]]] = None,
    record_intent_fn: Optional[
        Callable[[HibernateCandidate, ActuationRequestFields], bool]
    ] = None,
    budget_consumed: bool = False,
    clock_fn: Optional[Callable[[], str]] = None,
) -> HibernatePassResult:
    """Run one bounded hibernate pass, actuating at most one lifecycle mutation.

    ``refresh_fn`` is the action-time revalidation (Redmine #14219 T2a R1-F3): it RE-PRODUCES the
    candidate from every durable authority afresh (lifecycle anchor + each basis conjunct + head)
    and the pass proceeds only if the fresh candidate is EXACTLY EQUAL to the built one. Lifecycle
    identity alone is not enough — a review supersession, an integration/CI/dogfood lapse, or an
    origin-reachability change between build and actuation must abort, even when the lifecycle row
    is unchanged. A ``None`` or non-equal refresh is a typed stale zero-actuation. (The concrete
    producers behind ``refresh_fn`` are T2b; the leg only requires the composite re-check.)

    ``obligations_fn`` / ``journal_fn`` source the action-time obligation flags and the durable
    basis-event journal (T2b seams). ``lease_renew_fn`` is the pre-run wrapper lease fence; the
    commit-point fence is the ``use_case``'s own injected ``lease_guard`` (R1-F2). The pass:

      * iterates candidates in :func:`order_candidates` order;
      * consumes the one-mutation budget on the AUTHORITATIVE mutation fact
        (``transition.applied``) — a CAS that applied but left an incomplete release still consumes
        it (R1-F1), so a partial hibernate never permits a second CAS in the pass;
      * re-validates (composite refresh) then renews the lease immediately before each ``execute``;
        a stale refresh, a lost wrapper lease, or a lease lost at the use case's commit boundary
        (``outcome.lease_lost``) actuates nothing;
      * records each candidate's typed outcome and never retries a block within the pass.
    """
    ordered = order_candidates(candidates)
    if not ordered:
        return HibernatePassResult(attempts=(), mutations=0, empty_pass=True)

    attempts: list[HibernateAttempt] = []
    # Review j#86757 R4-F2 condition 4: a caller that already spent the pass's one-mutation
    # budget on a crash redrive starts this fresh pass consumed — every candidate defers.
    mutated = budget_consumed
    stopped = False
    for candidate in ordered:
        issue, lane = candidate.issue_id, candidate.anchor.lane_id
        drain_ready = str(getattr(candidate, "drain_ready_at", "") or "")

        def _append(attempt, _dr=drain_ready):
            # Stamp each attempt at ITS terminal disposition (review j#87214 R4-F2/F3): START =
            # THIS candidate's exact basis drain_ready_at; END = the clock read now, not once per pass.
            attempts.append(stamp_drain_metrics(
                attempt, _dr, clock_fn() if clock_fn is not None else ""
            ))
        if mutated or stopped:
            _append(HibernateAttempt(
                issue, lane, ATTEMPT_DEFERRED, NO_ACTUATION_DEFERRED_ONE_PER_PASS
            ))
            continue

        # Action-time revalidation FIRST: a fresh re-production of the candidate must be EXACTLY
        # equal — lifecycle anchor AND every durable basis conjunct/head still current
        # (fail-closed). It precedes the request derivation so that a lane whose basis has lapsed
        # reports THAT (``stale_basis``), rather than the ``no_basis_journal`` its now-empty
        # decision journal would otherwise produce: both are zero-actuation, but only one of them
        # names what actually happened. Neither check mutates, so the order is free.
        if refresh_fn(candidate) != candidate:
            _append(HibernateAttempt(issue, lane, ATTEMPT_STALE, LEG_REASON_BASIS_STALE))
            continue

        fields = derive_actuation_request(
            candidate, obligations_fn(candidate), decision_journal=journal_fn(candidate)
        )
        if isinstance(fields, str):
            # A missing basis-event journal is fail-closed for THIS candidate only; others proceed.
            _append(HibernateAttempt(issue, lane, ATTEMPT_NO_JOURNAL, fields))
            continue

        # Wrapper lease fence: renew immediately before the mutation (auxiliary to the use case's
        # own commit-point lease_guard).
        if not lease_renew_fn():
            _append(HibernateAttempt(issue, lane, ATTEMPT_LEASE_LOST, LEG_REASON_LEASE_LOST))
            stopped = True
            continue

        # Per-candidate actuation binding (checkpoint j#86726 R1-F2): the public rail's
        # worktree fingerprint / lane-activity authority is the use case's own repo_root, so a
        # multi-lane workspace must bind each candidate to ITS canonical worktree. An
        # unresolvable binding (missing / foreign / ambiguous worktree) is a typed zero-call —
        # never a fallback to a shared root that could inspect a sibling lane.
        bound_use_case = use_case_fn(candidate) if use_case_fn is not None else use_case
        if bound_use_case is None:
            _append(HibernateAttempt(
                issue, lane, ATTEMPT_BLOCKED, LEG_REASON_WORKTREE_UNRESOLVED
            ))
            continue
        # Review j#86776 R5-F3 / review j#86928 R6-F1: persist the durable redrive intent
        # immediately BEFORE the irreversible CAS, and make that persist a PRECONDITION of the
        # CAS. If the CAS landed but the release then crashed, the next pass's redrive
        # reconstructs THIS actuation's proven basis from the intent; if the intent could NOT be
        # persisted (a write / unreadable / schema / validation failure) the CAS is refused with
        # a typed zero-transition / zero-close, leaving the lane active — a hibernate that could
        # never be recovered would strand the live process forever (R6-F1). ``record_intent_fn``
        # returns whether the intent is durably stored.
        if record_intent_fn is not None and not record_intent_fn(candidate, fields):
            _append(HibernateAttempt(
                issue, lane, ATTEMPT_BLOCKED, LEG_REASON_INTENT_PERSIST_FAILED
            ))
            continue
        outcome = bound_use_case.run(_to_request(fields), execute=True)

        # A lease lost at the use case's commit boundary committed nothing; stop the pass.
        if outcome.lease_lost:
            _append(HibernateAttempt(issue, lane, ATTEMPT_LEASE_LOST, LEG_REASON_LEASE_LOST))
            stopped = True
            continue

        # The one-mutation budget is keyed to the AUTHORITATIVE mutation fact (R1-F1): a CAS that
        # applied consumes it even if the release was incomplete / withheld.
        applied = outcome.transition is not None and outcome.transition.applied
        if applied:
            mutated = True
            revision = outcome.transition.revision
            released = _released_count(outcome)
            if outcome.is_success:
                _append(HibernateAttempt(
                    issue, lane, ATTEMPT_ACTUATED, "", revision=revision, released=released
                ))
            else:
                _append(HibernateAttempt(
                    issue, lane, ATTEMPT_PARTIAL, _blocked_reason(outcome),
                    revision=revision, released=released
                ))
        else:
            _append(HibernateAttempt(issue, lane, ATTEMPT_BLOCKED, _blocked_reason(outcome)))

    return HibernatePassResult(
        attempts=tuple(attempts),
        mutations=1 if mutated and not budget_consumed else 0,
        empty_pass=False,
    )


@dataclass(frozen=True)
class RedriveResult:
    """The crash-redrive prelude's outcome: its attempts, and whether it consumed the budget."""

    attempts: tuple[HibernateAttempt, ...]
    mutations: int
    stopped: bool


def run_hibernate_redrives(
    redrives: "Sequence[object]",
    *,
    use_case_fn: "Callable[[object], Optional[SublaneHibernateUseCase]]",
    request_fn: "Callable[[object], HibernateRequest | str]",
    lease_renew_fn: Callable[[], bool],
    clock_fn: Optional[Callable[[], str]] = None,
    drain_ready_fn: "Optional[Callable[[str], str]]" = None,
) -> RedriveResult:
    """Finish prior interrupted releases on already-hibernated rows (review j#86757 R4-F2).

    ``redrives`` are the lifecycle rows a caller enumerated as hibernated with an UNRESOLVED
    process release (requested / partial, or ``not_requested`` when the lane still has a live
    slot / an unreadable inventory — review j#86776 R5-F2; released, and ``not_requested`` with
    a confirmed-empty inventory, are terminal and never reach here). Each is driven through the
    SAME public use case, whose ``already_hibernated`` path resumes the row's STORED release
    action id / pins (the immutable action authority) — no ACTIVE-basis re-derivation, no rebind
    to another cycle's approval. Deterministic ``(issue, lane)`` order; the pass-wide
    one-mutation budget applies:

    * ``request_fn`` returning a REASON STRING instead of a request is a typed zero-close
      (review j#86776 R5-F3: no durable intent records this row's basis, or the intent describes
      a different cycle) — recorded as ``redrive_blocked`` with that reason, consumes nothing,
      never touches the use case;
    * an EXECUTED redrive (the release drive ran — settled or success-withheld) consumed the
      budget: its process-close / store-write side effects are a managed-environment mutation,
      so no fresh mutation may follow in the same pass;
    * a typed zero-close refusal (``redrive_blocked``: preservation gate unmet, unreadable
      inventory, boundary divergence) consumes nothing — the fresh pass may proceed;
    * a lease lost stops the pass (zero further actuation), mirroring the fresh path.
    """
    ordered = sorted(
        redrives,
        key=lambda row: (
            str(getattr(row, "issue_id", "")),
            str(getattr(row, "lane_id", "")),
        ),
    )
    attempts: list[HibernateAttempt] = []
    mutated = False
    stopped = False
    for row in ordered:
        issue = str(getattr(row, "issue_id", ""))
        lane = str(getattr(row, "lane_id", ""))
        drain_ready = str(drain_ready_fn(issue) if drain_ready_fn is not None else "")

        def _append(attempt, _dr=drain_ready):
            attempts.append(stamp_drain_metrics(
                attempt, _dr, clock_fn() if clock_fn is not None else ""
            ))
        if mutated or stopped:
            _append(HibernateAttempt(
                issue, lane, ATTEMPT_DEFERRED, NO_ACTUATION_DEFERRED_ONE_PER_PASS
            ))
            continue
        request = request_fn(row)
        if isinstance(request, str):
            # A typed zero-close: no honest request could be built (no / mismatched intent).
            # Zero use-case call, zero mutation — consumes nothing.
            _append(HibernateAttempt(
                issue, lane, ATTEMPT_REDRIVE_BLOCKED, request
            ))
            continue
        use_case = use_case_fn(row)
        if use_case is None:
            _append(HibernateAttempt(
                issue, lane, ATTEMPT_REDRIVE_BLOCKED, LEG_REASON_WORKTREE_UNRESOLVED
            ))
            continue
        if not lease_renew_fn():
            _append(HibernateAttempt(issue, lane, ATTEMPT_LEASE_LOST, LEG_REASON_LEASE_LOST))
            stopped = True
            continue
        outcome = use_case.run(request, execute=True)
        if outcome.lease_lost:
            _append(HibernateAttempt(issue, lane, ATTEMPT_LEASE_LOST, LEG_REASON_LEASE_LOST))
            stopped = True
            continue
        if outcome.release is not None:
            # The release drive RAN: store writes / process closes may have landed (even a
            # success-withheld one) — the authoritative side-effect fact consumes the budget.
            mutated = True
            released = _released_count(outcome)
            if outcome.success_withheld:
                _append(HibernateAttempt(
                    issue, lane, ATTEMPT_REDRIVE_WITHHELD, LEG_REASON_SUCCESS_WITHHELD,
                    released=released
                ))
            else:
                _append(HibernateAttempt(
                    issue, lane, ATTEMPT_REDRIVEN, "", released=released
                ))
            continue
        _append(HibernateAttempt(
            issue, lane, ATTEMPT_REDRIVE_BLOCKED, _blocked_reason(outcome)
        ))
    return RedriveResult(
        attempts=tuple(attempts), mutations=1 if mutated else 0, stopped=stopped
    )


__all__ = [
    "ATTEMPT_ACTUATED",
    "ATTEMPT_PARTIAL",
    "ATTEMPT_BLOCKED",
    "ATTEMPT_DEFERRED",
    "ATTEMPT_LEASE_LOST",
    "ATTEMPT_NO_JOURNAL",
    "ATTEMPT_STALE",
    "ATTEMPT_REDRIVEN",
    "ATTEMPT_REDRIVE_WITHHELD",
    "ATTEMPT_REDRIVE_BLOCKED",
    "ATTEMPT_RELEASE_STATE_UNKNOWN",
    "LEG_REASON_LEASE_LOST",
    "LEG_REASON_REDRIVE_INTENT_ABSENT",
    "LEG_REASON_REDRIVE_INTENT_MISMATCH",
    "LEG_REASON_REDRIVE_INTENT_UNREADABLE",
    "LEG_REASON_INTENT_PERSIST_FAILED",
    "RedriveResult",
    "run_hibernate_redrives",
    "NO_ACTUATION_NO_CANDIDATE",
    "HibernateAttempt",
    "HibernatePassResult",
    "run_hibernate_pass",
]
