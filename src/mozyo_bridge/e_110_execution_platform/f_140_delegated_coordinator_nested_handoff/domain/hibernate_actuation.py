"""Pure actuation planning for the auto-hibernate runner (Redmine #14219, tranche T2a).

T1 produced a typed :class:`HibernateCandidate` bound to an exact lane and head. T2 turns an
approved candidate into exactly one public ``sublane hibernate --execute`` per bounded pass. This
module is the PURE bridge between the two, and it owns two safety-critical decisions:

  * **Candidate → request derivation.** :func:`derive_actuation_request` maps a candidate's proven
    basis to the :class:`HibernateAssertions` basis flags (an early-hibernate candidate sets the
    five early flags; a dependency-park candidate sets ``explicitly_parked``), binds
    ``expected_lane_generation`` / ``expected_revision`` from the anchor so the disposition CAS is
    pinned, and folds in the separately-sourced action-time obligation flags. The basis flags come
    ONLY from the candidate (never re-asserted here); the obligation flags come ONLY from the
    action-time observation. This module never fabricates a flag.
  * **At most one mutation per pass.** :func:`plan_pass` selects at most one actuatable candidate
    from the pass's candidates, deterministically, and defers the rest with a typed reason — the
    bounded-reconcile contract (one safe lifecycle mutation per pass).

There is no I/O and no actuation here. Driving the real ``SublaneHibernateUseCase`` (the preflight,
the T0/T1/T2 TOCTOU fence, the CAS) is the application leg; sourcing the obligation flags and the
basis-event journal from durable producers is T2b.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .hibernate_candidate import (
    BASIS_DEPENDENCY_PARK,
    BASIS_EARLY_HIBERNATE,
    HibernateCandidate,
)

# --------------------------------------------------------------------------------------------------
# Action-time obligations — the eight caller-asserted gates the public preflight requires that are
# NOT part of the hibernate basis (Redmine #13682 / #13967). They are durable/live facts observed at
# action time (T2b sources them); the candidate never proves them. Every field defaults to the
# unsatisfied (fail-closed) value, so an unsourced obligation blocks rather than passes.
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionTimeObligations:
    callbacks_drained: bool = False
    no_review_pending: bool = False
    no_owner_approval_pending: bool = False
    no_integration_pending: bool = False
    no_pending_prompt: bool = False
    not_working: bool = False
    worktree_clean: bool = False
    boundary_recorded: bool = False


# The basis flags each declared basis proves. The candidate's basis is the ONLY source of these.
_BASIS_FLAGS = {
    BASIS_EARLY_HIBERNATE: {
        "review_approved": True,
        "staging_integrated": True,
        "required_ci_green": True,
        "dogfood_delegated": True,
        "commits_pushed": True,
    },
    BASIS_DEPENDENCY_PARK: {
        "explicitly_parked": True,
    },
}

# The reason a candidate cannot be actuated this pass, or that a pass actuates nothing.
NO_ACTUATION_NO_CANDIDATE = "no_actuatable_candidate"
NO_ACTUATION_DEFERRED_ONE_PER_PASS = "deferred_one_mutation_per_pass"
NO_ACTUATION_MISSING_JOURNAL = "basis_event_journal_absent"


def order_candidates(candidates: Sequence[HibernateCandidate]) -> tuple[HibernateCandidate, ...]:
    """Deterministic pass order by ``(issue_id, lane_id)``.

    Determinism matters for crash / duplicate-wake idempotency: two passes over the same candidate
    set try the lanes in the same order, so a re-drive re-attempts the same lane rather than racing
    a different one. The application leg iterates this order and actuates at most one lifecycle
    mutation per pass (a blocked candidate does not starve the rest — the leg moves on, but only one
    mutation ever lands).
    """
    return tuple(sorted(candidates, key=lambda c: (c.issue_id, c.anchor.lane_id)))


@dataclass(frozen=True)
class ActuationRequestFields:
    """The exact inputs for one ``sublane hibernate`` invocation, derived from an approved candidate.

    ``assertion_flags`` is the full :class:`HibernateAssertions` kwargs mapping (basis flags from the
    candidate, obligation flags from the action-time observation). The application leg turns this
    into a ``HibernateRequest`` and drives the real preflight + ``--execute``; the CAS is pinned to
    ``expected_lane_generation`` / ``expected_revision`` so a raced generation/revision refuses.
    """

    issue: str
    lane: str
    journal: str
    expected_lane_generation: str
    expected_revision: str
    assertion_flags: dict


def derive_actuation_request(
    candidate: HibernateCandidate,
    obligations: ActionTimeObligations,
    *,
    decision_journal: str,
) -> "ActuationRequestFields | str":
    """Derive the exact hibernate request for an approved candidate, or a typed no-actuation reason.

    ``decision_journal`` is the durable basis-event journal that authorises the hibernate (the park
    declaration or the record that established early-hibernate qualification). It is part of the
    action intent's anchor (issue / lane / workspace / generation / revision / journal / head); an
    empty journal is a fail-closed :data:`NO_ACTUATION_MISSING_JOURNAL`, never a guessed anchor.

    The basis flags come only from ``candidate.basis``; the obligation flags come only from
    ``obligations``. Nothing is re-asserted or fabricated here — the public preflight and the
    T0/T1/T2 TOCTOU fence remain the action-time safety net.
    """
    if not decision_journal.strip():
        return NO_ACTUATION_MISSING_JOURNAL

    flags = {
        "explicitly_parked": False,
        "review_approved": False,
        "staging_integrated": False,
        "required_ci_green": False,
        "dogfood_delegated": False,
        "commits_pushed": False,
        "callbacks_drained": obligations.callbacks_drained,
        "no_review_pending": obligations.no_review_pending,
        "no_owner_approval_pending": obligations.no_owner_approval_pending,
        "no_integration_pending": obligations.no_integration_pending,
        "no_pending_prompt": obligations.no_pending_prompt,
        "not_working": obligations.not_working,
        "worktree_clean": obligations.worktree_clean,
        "boundary_recorded": obligations.boundary_recorded,
    }
    flags.update(_BASIS_FLAGS[candidate.basis])

    return ActuationRequestFields(
        issue=candidate.issue_id,
        lane=candidate.anchor.lane_id,
        journal=decision_journal.strip(),
        expected_lane_generation=str(candidate.anchor.lane_generation),
        expected_revision=str(candidate.anchor.revision),
        assertion_flags=flags,
    )
