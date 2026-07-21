"""Typed hibernate-candidate model + pure classifier (Redmine #14219, tranche T1).

The event-driven auto-hibernate runner (#14219) must turn a drain-ready lane into a
``mozyo-bridge sublane hibernate --execute`` actuation *without* a human sweep. The public
hibernate rail (#13843 / #13967) gates on :class:`HibernateAssertions` — six operator-asserted
booleans, every one defaulting ``False`` (fail-closed) and, today, wired straight from CLI flags.
Nothing binds them to durable authority. This module is the typed, PURE core that closes that gap
*safely*, per the design ruling on #14219 j#85459:

  * **Fork 1 (anchor binding).** The lane's exact identity — ``repo_workspace_id`` / ``lane_id`` /
    ``lane_generation`` / ``revision`` / ``disposition`` — is re-bound from the read-only lifecycle
    store (:func:`bind_lifecycle_anchor` folds the records; the impure read lives in the
    application layer). The lifecycle record is **NOT** the git-head authority: it carries no
    commit field at all. So the candidate's ``head`` may be bound only from a non-lifecycle
    authority (:data:`HEAD_AUTHORITIES`); a head tagged ``lifecycle_readonly`` is refused.
  * **Fork 2 (basis, `releasable`-is-not-a-proxy).** The drain-queue ``process_retention=releasable``
    verdict means only "no coordinator-actionable holding drain remains" — it is NOT proof of the
    five early-hibernate conjuncts. Each conjunct is a :class:`BasisConjunct` that MUST carry its
    own durable authority; :data:`_CONJUNCT_AUTHORITY` pins one legitimate provenance per key, and
    there is deliberately **no drain-queue provenance token**, so ``releasable`` cannot be named as
    any conjunct's authority. A required conjunct that is missing → ``basis_partially_unknown``
    (no-op, never an implicit fallback to another basis); wrong provenance →
    ``conjunct_authority_mismatch``; present-but-false → ``basis_unsatisfied``.

Everything here is a pure total function over supplied facts. There is no actuation, no I/O, and no
mutation — T1 is ``actuation 0``. The classifier's basis conjuncts are typed *inputs*; wiring each
to its real durable source (review / integration / CI / dogfood-delegation / git-remote / park
declaration) and driving the public preflight is tranche T2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_ACTIVE

# --------------------------------------------------------------------------------------------------
# Authority provenance — where a bound fact came from. Each anchor field and each basis conjunct
# carries one of these, so a reviewer (and the code) can see the authority behind every value.
# There is NO drain-queue / read-model-verdict provenance: a candidate can never be justified by a
# derived verdict, only by a first-class durable authority.
# --------------------------------------------------------------------------------------------------
PROVENANCE_LIFECYCLE_READONLY = "lifecycle_readonly"
PROVENANCE_REVIEW_RECORD = "durable_review_record"
PROVENANCE_INTEGRATION_RECORD = "durable_integration_record"
PROVENANCE_CI_RECORD = "durable_ci_record"
PROVENANCE_DELEGATION_RECORD = "durable_delegation_record"
PROVENANCE_GIT_REMOTE = "git_remote_evidence"
PROVENANCE_PARK_DECLARATION = "durable_park_declaration"

PROVENANCES = frozenset({
    PROVENANCE_LIFECYCLE_READONLY,
    PROVENANCE_REVIEW_RECORD,
    PROVENANCE_INTEGRATION_RECORD,
    PROVENANCE_CI_RECORD,
    PROVENANCE_DELEGATION_RECORD,
    PROVENANCE_GIT_REMOTE,
    PROVENANCE_PARK_DECLARATION,
})

#: The only authorities that may bind the git head. The ruling is explicit: the head is NEVER
#: inferred from the lifecycle record (which has no commit field). It comes from the durable Review
#: record (whose canonical ``review_result`` marker carries the full head) or from git-remote
#: evidence.
HEAD_AUTHORITIES = frozenset({PROVENANCE_REVIEW_RECORD, PROVENANCE_GIT_REMOTE})

# --------------------------------------------------------------------------------------------------
# Basis vocabulary. A candidate qualifies under exactly one DECLARED basis (a typed basis event);
# the classifier never infers or falls back between bases.
# --------------------------------------------------------------------------------------------------
BASIS_EARLY_HIBERNATE = "early_hibernate"
BASIS_DEPENDENCY_PARK = "dependency_park"

DECLARABLE_BASES = frozenset({BASIS_EARLY_HIBERNATE, BASIS_DEPENDENCY_PARK})

# Conjunct keys.
CONJUNCT_REVIEW_APPROVED = "review_approved"
CONJUNCT_STAGING_INTEGRATED = "staging_integrated"
CONJUNCT_REQUIRED_CI_GREEN = "required_ci_green"
CONJUNCT_DOGFOOD_DELEGATED = "dogfood_delegated"
CONJUNCT_COMMITS_PUSHED = "commits_pushed"
CONJUNCT_PARK_DECLARED = "park_declared"

#: The five conjuncts an early-hibernate candidate must satisfy, each from its own authority.
EARLY_HIBERNATE_CONJUNCTS = (
    CONJUNCT_REVIEW_APPROVED,
    CONJUNCT_STAGING_INTEGRATED,
    CONJUNCT_REQUIRED_CI_GREEN,
    CONJUNCT_DOGFOOD_DELEGATED,
    CONJUNCT_COMMITS_PUSHED,
)

#: The conjuncts a dependency-park candidate must satisfy.
DEPENDENCY_PARK_CONJUNCTS = (CONJUNCT_PARK_DECLARED,)

#: The one legitimate provenance for each conjunct. A conjunct whose provenance is not this exact
#: authority is a structural error (an attempted proxy) — never silently accepted. `releasable` /
#: any drain verdict is absent from every value here, so it can never satisfy a conjunct.
_CONJUNCT_AUTHORITY = {
    CONJUNCT_REVIEW_APPROVED: PROVENANCE_REVIEW_RECORD,
    CONJUNCT_STAGING_INTEGRATED: PROVENANCE_INTEGRATION_RECORD,
    CONJUNCT_REQUIRED_CI_GREEN: PROVENANCE_CI_RECORD,
    CONJUNCT_DOGFOOD_DELEGATED: PROVENANCE_DELEGATION_RECORD,
    CONJUNCT_COMMITS_PUSHED: PROVENANCE_GIT_REMOTE,
    CONJUNCT_PARK_DECLARED: PROVENANCE_PARK_DECLARATION,
}

_REQUIRED_CONJUNCTS = {
    BASIS_EARLY_HIBERNATE: EARLY_HIBERNATE_CONJUNCTS,
    BASIS_DEPENDENCY_PARK: DEPENDENCY_PARK_CONJUNCTS,
}

# Per-conjunct evidence anchor (Redmine #14219 R1-F2 + R2-F1). A conjunct's provenance proves WHERE
# the evidence came from; its anchor proves WHAT lane, generation, and commit the evidence is about.
# An issue-only anchor is NOT enough (R2-F1): a durable record for the same issue but a superseded
# lane generation, a different lane, or a different head could otherwise be synthesised into the
# current candidate. So EVERY conjunct must bind to the candidate's exact lane identity
# (workspace + lane + generation); a durable record from an old generation or another lane cannot
# count.
#
# Head-bearing conjuncts additionally bind to the candidate head — the evidence names a specific
# commit: a review_result marker carries the full head; a CI run is about one commit; an
# integration disposition names the integrated commit (skill ``references/workflow.md``); a dogfood
# delegation carries the exact SHA (skill ``references/release.md``); a push-reachability check is
# of one head. The dependency-park declaration is not about a commit, so it binds to the lane
# identity only.
_CONJUNCT_REQUIRES_HEAD = frozenset({
    CONJUNCT_REVIEW_APPROVED,
    CONJUNCT_STAGING_INTEGRATED,
    CONJUNCT_REQUIRED_CI_GREEN,
    CONJUNCT_DOGFOOD_DELEGATED,
    CONJUNCT_COMMITS_PUSHED,
})

# --------------------------------------------------------------------------------------------------
# Non-candidate reasons — the closed vocabulary emitted when a lane is NOT a hibernate candidate.
# Every "unknown / stale / ambiguous / uncertain" resolves to one of these (a typed zero-actuation),
# never to a silently-dropped lane.
# --------------------------------------------------------------------------------------------------
NON_CANDIDATE_LIFECYCLE_UNREADABLE = "lifecycle_store_unreadable"
NON_CANDIDATE_LIFECYCLE_ABSENT = "active_lifecycle_record_absent"
NON_CANDIDATE_LANE_AMBIGUOUS = "active_lane_ambiguous"
NON_CANDIDATE_WORKSPACE_MISMATCH = "selected_workspace_mismatch"
NON_CANDIDATE_LANE_IDENTITY_MISMATCH = "selected_lane_mismatch"
NON_CANDIDATE_GENERATION_MISMATCH = "lane_generation_mismatch"
NON_CANDIDATE_REVISION_MISMATCH = "lifecycle_revision_mismatch"
NON_CANDIDATE_HEAD_UNBOUND = "head_authority_absent"
NON_CANDIDATE_DECLARED_BASIS_INVALID = "declared_basis_invalid"
NON_CANDIDATE_CONJUNCT_AUTHORITY_MISMATCH = "conjunct_authority_mismatch"
NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH = "conjunct_anchor_mismatch"
NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN = "basis_partially_unknown"
NON_CANDIDATE_BASIS_UNSATISFIED = "basis_unsatisfied"

HIBERNATE_NON_CANDIDATE_REASONS = frozenset({
    NON_CANDIDATE_LIFECYCLE_UNREADABLE,
    NON_CANDIDATE_LIFECYCLE_ABSENT,
    NON_CANDIDATE_LANE_AMBIGUOUS,
    NON_CANDIDATE_WORKSPACE_MISMATCH,
    NON_CANDIDATE_LANE_IDENTITY_MISMATCH,
    NON_CANDIDATE_GENERATION_MISMATCH,
    NON_CANDIDATE_REVISION_MISMATCH,
    NON_CANDIDATE_HEAD_UNBOUND,
    NON_CANDIDATE_DECLARED_BASIS_INVALID,
    NON_CANDIDATE_CONJUNCT_AUTHORITY_MISMATCH,
    NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH,
    NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN,
    NON_CANDIDATE_BASIS_UNSATISFIED,
})


@dataclass(frozen=True)
class BoundField:
    """One anchor value plus the authority it was bound from.

    ``value`` is always an id / token / short hex — never a filesystem path or a secret — so a
    payload built from these is secret-safe by construction.
    """

    value: str
    provenance: str


@dataclass(frozen=True)
class LifecycleAnchor:
    """The lane's exact identity, re-bound from the read-only lifecycle store.

    Every field here is, by construction, ``provenance=lifecycle_readonly``. The git head is
    deliberately absent — the lifecycle record carries no commit, and the ruling forbids inferring
    the head from it.
    """

    issue_id: str
    repo_workspace_id: str
    lane_id: str
    lane_generation: int
    revision: int
    disposition: str = DISPOSITION_ACTIVE

    def as_payload(self) -> dict:
        return {
            "issue_id": self.issue_id,
            "repo_workspace_id": self.repo_workspace_id,
            "lane_id": self.lane_id,
            "lane_generation": self.lane_generation,
            "revision": self.revision,
            "disposition": self.disposition,
            "provenance": PROVENANCE_LIFECYCLE_READONLY,
        }


@dataclass(frozen=True)
class SelectedLane:
    """The EXACT lane identity the enumeration selected (Redmine #14219 R1-F1).

    A hibernate candidate must re-bind to *this* lane, not merely to "the single active lane for the
    issue". The classifier confirms the freshly-read lifecycle record matches all four dimensions
    (workspace / lane / generation / revision); any drift is a stale selection → zero-actuation.
    """

    issue_id: str
    repo_workspace_id: str
    lane_id: str
    lane_generation: int
    revision: int


@dataclass(frozen=True)
class BasisConjunct:
    """One durable precondition, bound from its OWN authority AND to the candidate's exact anchor.

    ``key`` is a ``CONJUNCT_*`` token; ``provenance`` must equal :data:`_CONJUNCT_AUTHORITY` for
    that key (WHERE the evidence came from). The anchor (WHAT lane / commit the evidence is about)
    must line up with the candidate too (Redmine #14219 R1-F2 + R2-F1): every conjunct binds to the
    candidate's exact lane identity via ``bound_workspace`` / ``bound_lane`` / ``bound_generation``
    (so an old-generation or cross-lane record cannot count), and a head-bearing conjunct
    (:data:`_CONJUNCT_REQUIRES_HEAD`) additionally carries ``bound_head`` equal to the candidate
    head. ``bound_generation`` of 0 and empty strings mean "unbound" and never match.
    """

    key: str
    satisfied: bool
    provenance: str
    bound_workspace: str = ""
    bound_lane: str = ""
    bound_generation: int = 0
    bound_head: str = ""
    detail: str = ""

    def as_payload(self) -> dict:
        return {
            "key": self.key,
            "satisfied": self.satisfied,
            "provenance": self.provenance,
            "bound_workspace": self.bound_workspace,
            "bound_lane": self.bound_lane,
            "bound_generation": self.bound_generation,
            "bound_head": self.bound_head,
        }


@dataclass(frozen=True)
class HibernateCandidate:
    """A lane proven eligible to hibernate, bound to its exact durable anchor.

    This is a *candidate*, not an actuation: producing it performs no mutation. Tranche T2 claims a
    candidate, re-runs the public ``sublane hibernate`` preflight + T0/T1/T2 TOCTOU fence at action
    time, and only then ``--execute``s.
    """

    issue_id: str
    anchor: LifecycleAnchor
    head: BoundField
    basis: str
    conjuncts: tuple[BasisConjunct, ...]

    def as_payload(self) -> dict:
        return {
            "kind": "hibernate_candidate",
            "issue_id": self.issue_id,
            "basis": self.basis,
            "anchor": self.anchor.as_payload(),
            "head": {"value": self.head.value, "provenance": self.head.provenance},
            "conjuncts": [c.as_payload() for c in self.conjuncts],
        }


@dataclass(frozen=True)
class HibernateNonCandidate:
    """A typed zero-actuation verdict: this lane is not a candidate, and why."""

    issue_id: str
    reason: str
    detail: str = ""

    def as_payload(self) -> dict:
        return {
            "kind": "hibernate_non_candidate",
            "issue_id": self.issue_id,
            "reason": self.reason,
            "detail": self.detail,
        }


def _issue(rec: object) -> str:
    return str(getattr(rec, "issue_id", "") or "").strip()


def _disposition(rec: object) -> str:
    return str(getattr(rec, "lane_disposition", "") or "").strip()


def bind_lifecycle_anchor(
    records: Optional[Sequence[object]],
    *,
    selected: SelectedLane,
) -> "LifecycleAnchor | HibernateNonCandidate":
    """Re-bind the EXACT selected lane from the read-only lifecycle rows (Redmine #14219 R1-F1).

    Two stages, both fail-closed:

    1. Ambiguity guard (matching ``authority_execution_index``'s ``len(recs) != 1 -> drop``):

       * ``records is None`` (store unknown / newer / malformed / partial — the readonly
         downgrade guard) → :data:`NON_CANDIDATE_LIFECYCLE_UNREADABLE`.
       * no active record for the issue → :data:`NON_CANDIDATE_LIFECYCLE_ABSENT`.
       * more than one active record (original/recovery or cross-workspace) →
         :data:`NON_CANDIDATE_LANE_AMBIGUOUS`.

    2. Exact-identity confirmation — the single active record must match the enumeration's
       ``selected`` lane on ALL four dimensions, or the selection is stale / points at a different
       lane (workspace / lane / generation / revision mismatch). Only then is a
       :class:`LifecycleAnchor` returned. This is what stops "the single active row for the issue"
       from silently standing in for the lane the enumeration actually chose.

    ``records is ()`` (an absent store, nothing created) is distinct from ``None`` and folds to
    ``absent``, not ``unreadable``.
    """
    issue_id = selected.issue_id.strip()
    if records is None:
        return HibernateNonCandidate(issue_id, NON_CANDIDATE_LIFECYCLE_UNREADABLE)
    active = [
        rec for rec in records
        if _issue(rec) == issue_id and _disposition(rec) == DISPOSITION_ACTIVE
    ]
    if not active:
        return HibernateNonCandidate(issue_id, NON_CANDIDATE_LIFECYCLE_ABSENT)
    if len(active) != 1:
        return HibernateNonCandidate(
            issue_id, NON_CANDIDATE_LANE_AMBIGUOUS, detail=f"{len(active)} active records"
        )
    rec = active[0]
    ws = str(getattr(rec, "repo_workspace_id", "") or "")
    lane = str(getattr(rec, "lane_id", "") or "")
    generation = int(getattr(rec, "lane_generation", 0) or 0)
    revision = int(getattr(rec, "revision", 0) or 0)

    if ws != selected.repo_workspace_id:
        return HibernateNonCandidate(
            issue_id, NON_CANDIDATE_WORKSPACE_MISMATCH,
            detail=f"selected={selected.repo_workspace_id} bound={ws}",
        )
    if lane != selected.lane_id:
        return HibernateNonCandidate(
            issue_id, NON_CANDIDATE_LANE_IDENTITY_MISMATCH,
            detail=f"selected={selected.lane_id} bound={lane}",
        )
    if generation != selected.lane_generation:
        return HibernateNonCandidate(
            issue_id, NON_CANDIDATE_GENERATION_MISMATCH,
            detail=f"selected={selected.lane_generation} bound={generation}",
        )
    if revision != selected.revision:
        return HibernateNonCandidate(
            issue_id, NON_CANDIDATE_REVISION_MISMATCH,
            detail=f"selected={selected.revision} bound={revision}",
        )
    return LifecycleAnchor(
        issue_id=issue_id,
        repo_workspace_id=ws,
        lane_id=lane,
        lane_generation=generation,
        revision=revision,
        disposition=DISPOSITION_ACTIVE,
    )


def _anchor_matches(conjunct: BasisConjunct, *, anchor: "LifecycleAnchor", candidate_head: str) -> bool:
    """Whether the conjunct's evidence is bound to the candidate's exact lane and (if head-bearing)
    commit.

    Every conjunct must name the candidate's exact lane identity — workspace, lane, and generation
    (Redmine #14219 R2-F1) — so a durable record from a superseded generation or a different lane
    cannot count. A head-bearing conjunct must additionally carry ``bound_head`` equal to the
    candidate head. An empty or drifted anchor means the evidence — however genuine — is about a
    different lane / commit and does not apply.
    """
    if not (
        conjunct.bound_workspace and conjunct.bound_workspace == anchor.repo_workspace_id
        and conjunct.bound_lane and conjunct.bound_lane == anchor.lane_id
        and conjunct.bound_generation and conjunct.bound_generation == anchor.lane_generation
    ):
        return False
    if conjunct.key in _CONJUNCT_REQUIRES_HEAD:
        return bool(conjunct.bound_head) and conjunct.bound_head == candidate_head
    return True


def _evaluate_basis(
    declared_basis: str,
    conjuncts: Sequence[BasisConjunct],
    *,
    anchor: "LifecycleAnchor",
    candidate_head: str,
) -> Optional[str]:
    """Return a non-candidate reason if the declared basis is not satisfied, else ``None``.

    Only the conjuncts REQUIRED by the declared basis are considered — a candidate never falls back
    to another basis. Reason precedence, most structural first: authority mismatch (wrong source) →
    partially unknown (a required conjunct is missing) → anchor mismatch (right source, but the
    evidence is about a different lane / generation / head) → unsatisfied (evidence about this
    candidate says no).
    """
    required = _REQUIRED_CONJUNCTS[declared_basis]
    by_key = {c.key: c for c in conjuncts}

    # 1. Authority mismatch: any supplied REQUIRED conjunct whose provenance is not its one
    #    legitimate authority. Loudest — it means someone tried to pass a proxy.
    for key in required:
        c = by_key.get(key)
        if c is not None and c.provenance != _CONJUNCT_AUTHORITY[key]:
            return NON_CANDIDATE_CONJUNCT_AUTHORITY_MISMATCH
    # 2. Partially unknown: a required conjunct was not supplied at all → no-op, never a guess.
    for key in required:
        if key not in by_key:
            return NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN
    # 3. Anchor mismatch: a required conjunct's evidence is about a different lane / generation /
    #    head than the candidate — a genuine proof, but not of THIS lane at THIS head.
    for key in required:
        if not _anchor_matches(by_key[key], anchor=anchor, candidate_head=candidate_head):
            return NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH
    # 4. Unsatisfied: a required conjunct is present, correctly-sourced, on-target, but false.
    for key in required:
        if not by_key[key].satisfied:
            return NON_CANDIDATE_BASIS_UNSATISFIED
    return None


def classify_hibernate_candidate(
    *,
    selected: SelectedLane,
    declared_basis: str,
    records: Optional[Sequence[object]],
    head: Optional[BoundField],
    conjuncts: Sequence[BasisConjunct],
) -> "HibernateCandidate | HibernateNonCandidate":
    """Decide whether a lane is a hibernate candidate, fail-closed. PURE.

    Order of gates (each a typed zero-actuation on failure):

      1. ``declared_basis`` must be a real basis (a typed basis event, never inferred).
      2. Re-bind and confirm the EXACT ``selected`` lane from the read-only lifecycle records —
         workspace / lane / generation / revision must all match (Redmine #14219 R1-F1); any drift
         (stale selection, wrong lane) is a typed zero-actuation.
      3. ``head`` must be present and bound from a :data:`HEAD_AUTHORITIES` authority — never the
         lifecycle record.
      4. The declared basis's required conjuncts must each be satisfied from their own authority AND
         be bound to this candidate's exact lane identity — and, if head-bearing, its exact head
         (Redmine #14219 R1-F2 + R2-F1).
    """
    issue_id = selected.issue_id.strip()
    if declared_basis not in DECLARABLE_BASES:
        return HibernateNonCandidate(
            issue_id, NON_CANDIDATE_DECLARED_BASIS_INVALID, detail=str(declared_basis)
        )

    bound = bind_lifecycle_anchor(records, selected=selected)
    if isinstance(bound, HibernateNonCandidate):
        return bound
    anchor = bound

    if head is None or not head.value.strip() or head.provenance not in HEAD_AUTHORITIES:
        return HibernateNonCandidate(issue_id, NON_CANDIDATE_HEAD_UNBOUND)

    basis_reason = _evaluate_basis(
        declared_basis, conjuncts, anchor=anchor, candidate_head=head.value
    )
    if basis_reason is not None:
        return HibernateNonCandidate(issue_id, basis_reason)

    required = _REQUIRED_CONJUNCTS[declared_basis]
    by_key = {c.key: c for c in conjuncts}
    proven = tuple(by_key[key] for key in required)
    return HibernateCandidate(
        issue_id=issue_id,
        anchor=anchor,
        head=head,
        basis=declared_basis,
        conjuncts=proven,
    )
