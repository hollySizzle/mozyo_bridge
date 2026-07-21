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

from dataclasses import dataclass
from typing import Callable, Sequence

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
ATTEMPT_BLOCKED = "blocked"
ATTEMPT_DEFERRED = "deferred"
ATTEMPT_LEASE_LOST = "lease_lost"
ATTEMPT_NO_JOURNAL = "no_basis_journal"
ATTEMPT_STALE = "stale_anchor"

# Fixed reason tokens the leg emits itself (secret-free; the use case's own reasons are already a
# closed vocabulary and are passed through verbatim).
LEG_REASON_LEASE_LOST = "supervisor_lease_lost"
LEG_REASON_SUCCESS_WITHHELD = "release_success_withheld"
LEG_REASON_NOT_ACTUATED = "not_actuated"
LEG_REASON_ANCHOR_DRIFTED = "anchor_drifted_since_build"


@dataclass(frozen=True)
class HibernateAttempt:
    """One candidate's outcome this pass. ``reason`` is a closed token; no secrets/paths."""

    issue: str
    lane: str
    kind: str
    reason: str = ""
    revision: int = 0

    def as_payload(self) -> dict:
        return {
            "issue": self.issue,
            "lane": self.lane,
            "kind": self.kind,
            "reason": self.reason,
            "revision": self.revision,
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


def run_hibernate_pass(
    candidates: Sequence[HibernateCandidate],
    *,
    revalidate_fn: Callable[[HibernateCandidate], bool],
    obligations_fn: Callable[[HibernateCandidate], ActionTimeObligations],
    journal_fn: Callable[[HibernateCandidate], str],
    use_case: SublaneHibernateUseCase,
    lease_renew_fn: Callable[[], bool],
) -> HibernatePassResult:
    """Run one bounded hibernate pass, actuating at most one lifecycle mutation.

    ``revalidate_fn`` re-confirms a candidate's exact anchor is still current at action time (the
    public CAS pins to its own fresh read, so a lane that drifted since build must not be
    hibernated — see :func:`hibernate_candidate_source.still_current`). ``obligations_fn`` /
    ``journal_fn`` source the action-time obligation flags and the durable basis-event journal for a
    candidate (T2b seams). ``lease_renew_fn`` renews the supervisor lease and returns ``False`` if it
    was taken over. The pass:

      * iterates candidates in :func:`order_candidates` order;
      * once one hibernate is applied, defers every remaining candidate (one mutation per pass);
      * re-validates the anchor, then renews the lease, immediately before each ``execute`` — a
        drifted anchor or a lost lease actuates nothing;
      * records each candidate's typed outcome and never retries a block within the pass.
    """
    ordered = order_candidates(candidates)
    if not ordered:
        return HibernatePassResult(attempts=(), mutations=0, empty_pass=True)

    attempts: list[HibernateAttempt] = []
    mutated = False
    stopped = False
    for candidate in ordered:
        issue, lane = candidate.issue_id, candidate.anchor.lane_id
        if mutated or stopped:
            attempts.append(HibernateAttempt(
                issue, lane, ATTEMPT_DEFERRED, NO_ACTUATION_DEFERRED_ONE_PER_PASS
            ))
            continue

        fields = derive_actuation_request(
            candidate, obligations_fn(candidate), decision_journal=journal_fn(candidate)
        )
        if isinstance(fields, str):
            # A missing basis-event journal is fail-closed for THIS candidate only; others proceed.
            attempts.append(HibernateAttempt(issue, lane, ATTEMPT_NO_JOURNAL, fields))
            continue

        # Action-time revalidation: the exact anchor must still be current (fail-closed on drift).
        if not revalidate_fn(candidate):
            attempts.append(HibernateAttempt(issue, lane, ATTEMPT_STALE, LEG_REASON_ANCHOR_DRIFTED))
            continue

        # Lease-boundary fence: renew immediately before the sole irreversible mutation.
        if not lease_renew_fn():
            attempts.append(HibernateAttempt(issue, lane, ATTEMPT_LEASE_LOST, LEG_REASON_LEASE_LOST))
            stopped = True
            continue

        outcome = use_case.run(_to_request(fields), execute=True)
        if outcome.is_success:
            mutated = True
            revision = outcome.transition.revision if outcome.transition is not None else 0
            attempts.append(HibernateAttempt(issue, lane, ATTEMPT_ACTUATED, "", revision=revision))
        else:
            attempts.append(HibernateAttempt(issue, lane, ATTEMPT_BLOCKED, _blocked_reason(outcome)))

    return HibernatePassResult(
        attempts=tuple(attempts), mutations=1 if mutated else 0, empty_pass=False
    )


__all__ = [
    "ATTEMPT_ACTUATED",
    "ATTEMPT_BLOCKED",
    "ATTEMPT_DEFERRED",
    "ATTEMPT_LEASE_LOST",
    "ATTEMPT_NO_JOURNAL",
    "ATTEMPT_STALE",
    "NO_ACTUATION_NO_CANDIDATE",
    "HibernateAttempt",
    "HibernatePassResult",
    "run_hibernate_pass",
]
