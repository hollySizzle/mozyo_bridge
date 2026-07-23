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

# Fixed reason tokens the leg emits itself (secret-free; the use case's own reasons are already a
# closed vocabulary and are passed through verbatim).
LEG_REASON_LEASE_LOST = "supervisor_lease_lost"
LEG_REASON_SUCCESS_WITHHELD = "release_success_withheld"
LEG_REASON_NOT_ACTUATED = "not_actuated"
LEG_REASON_BASIS_STALE = "basis_stale_since_build"
LEG_REASON_WORKTREE_UNRESOLVED = "candidate_worktree_unresolved"


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
    refresh_fn: Callable[[HibernateCandidate], Optional[HibernateCandidate]],
    obligations_fn: Callable[[HibernateCandidate], ActionTimeObligations],
    journal_fn: Callable[[HibernateCandidate], str],
    use_case: Optional[SublaneHibernateUseCase] = None,
    lease_renew_fn: Callable[[], bool],
    use_case_fn: Optional[Callable[[HibernateCandidate], Optional[SublaneHibernateUseCase]]] = None,
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
    mutated = False
    stopped = False
    for candidate in ordered:
        issue, lane = candidate.issue_id, candidate.anchor.lane_id
        if mutated or stopped:
            attempts.append(HibernateAttempt(
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
            attempts.append(HibernateAttempt(issue, lane, ATTEMPT_STALE, LEG_REASON_BASIS_STALE))
            continue

        fields = derive_actuation_request(
            candidate, obligations_fn(candidate), decision_journal=journal_fn(candidate)
        )
        if isinstance(fields, str):
            # A missing basis-event journal is fail-closed for THIS candidate only; others proceed.
            attempts.append(HibernateAttempt(issue, lane, ATTEMPT_NO_JOURNAL, fields))
            continue

        # Wrapper lease fence: renew immediately before the mutation (auxiliary to the use case's
        # own commit-point lease_guard).
        if not lease_renew_fn():
            attempts.append(HibernateAttempt(issue, lane, ATTEMPT_LEASE_LOST, LEG_REASON_LEASE_LOST))
            stopped = True
            continue

        # Per-candidate actuation binding (checkpoint j#86726 R1-F2): the public rail's
        # worktree fingerprint / lane-activity authority is the use case's own repo_root, so a
        # multi-lane workspace must bind each candidate to ITS canonical worktree. An
        # unresolvable binding (missing / foreign / ambiguous worktree) is a typed zero-call —
        # never a fallback to a shared root that could inspect a sibling lane.
        bound_use_case = use_case_fn(candidate) if use_case_fn is not None else use_case
        if bound_use_case is None:
            attempts.append(HibernateAttempt(
                issue, lane, ATTEMPT_BLOCKED, LEG_REASON_WORKTREE_UNRESOLVED
            ))
            continue
        outcome = bound_use_case.run(_to_request(fields), execute=True)

        # A lease lost at the use case's commit boundary committed nothing; stop the pass.
        if outcome.lease_lost:
            attempts.append(HibernateAttempt(issue, lane, ATTEMPT_LEASE_LOST, LEG_REASON_LEASE_LOST))
            stopped = True
            continue

        # The one-mutation budget is keyed to the AUTHORITATIVE mutation fact (R1-F1): a CAS that
        # applied consumes it even if the release was incomplete / withheld.
        applied = outcome.transition is not None and outcome.transition.applied
        if applied:
            mutated = True
            revision = outcome.transition.revision
            if outcome.is_success:
                attempts.append(
                    HibernateAttempt(issue, lane, ATTEMPT_ACTUATED, "", revision=revision)
                )
            else:
                attempts.append(HibernateAttempt(
                    issue, lane, ATTEMPT_PARTIAL, _blocked_reason(outcome), revision=revision
                ))
        else:
            attempts.append(HibernateAttempt(issue, lane, ATTEMPT_BLOCKED, _blocked_reason(outcome)))

    return HibernatePassResult(
        attempts=tuple(attempts), mutations=1 if mutated else 0, empty_pass=False
    )


__all__ = [
    "ATTEMPT_ACTUATED",
    "ATTEMPT_PARTIAL",
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
