"""Action-time candidate assembler tests (Redmine #14219 T2b, step 4b).

Pins the four assembler invariants:

- **the head comes from the git-remote observation, not from a marker** — a review_result marker
  naming a different head cannot pull the candidate onto itself; it is an anchor mismatch;
- **an unreadable journal source is its own typed zero** — never an empty evidence set, and the
  later reads do not even run;
- **one fresh observation per candidate per pass** — ``journal_fn`` and ``refresh_fn`` read the
  SAME re-assembly, so the actuation journal and the revalidated candidate cannot come from two
  different observations;
- **a non-candidate yields no decision journal** — a lapsed basis cannot hand the leg an anchor.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_ACTIVE

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_candidate_assembler import (  # noqa: E501
    DETAIL_JOURNAL_SOURCE_UNREADABLE,
    AssemblyRequest,
    HibernateCandidateAssembler,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
    GAP_PUSH_OBSERVATION_ABSENT,
    DogfoodReceipt,
    PushObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    ISSUER_COORDINATOR,
    ISSUER_LANE_WORKER,
    ISSUER_REVIEW_GATEWAY,
    EvidenceJournal,
    ResolvedIssuer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_candidate import (  # noqa: E501
    BASIS_DEPENDENCY_PARK,
    BASIS_EARLY_HIBERNATE,
    NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN,
    NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH,
    NON_CANDIDATE_HEAD_MALFORMED,
    NON_CANDIDATE_HEAD_UNBOUND,
    NON_CANDIDATE_LIFECYCLE_UNREADABLE,
    PROVENANCE_GIT_REMOTE,
    HibernateCandidate,
    HibernateNonCandidate,
    SelectedLane,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    LaneEvidenceEnvelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_integration import (  # noqa: E501
    render_integration_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_marker import (  # noqa: E501
    EVIDENCE_DOGFOOD_DELEGATED,
    EVIDENCE_PARK_DECLARED,
    EVIDENCE_REQUIRED_CI_GREEN,
    render_hibernate_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    render_workflow_event_marker,
)

ISSUE = "14219"
WS = "ws-1"
LANE = "lane-abc"
GEN = 4
REV = 7
HEAD = "a" * 40
OTHER_HEAD = "c" * 40
STAGING_HEAD = "b" * 40
REQ_JOURNAL = "85000"
RELEASE_ISSUE = "14184"


@dataclass(frozen=True)
class _Rec:
    """The fields ``bind_lifecycle_anchor`` reads off a lifecycle record."""

    issue_id: str = ISSUE
    repo_workspace_id: str = WS
    lane_id: str = LANE
    lane_generation: int = GEN
    revision: int = REV
    lane_disposition: str = DISPOSITION_ACTIVE


def _env(workspace=WS, lane=LANE, gen=GEN, head=HEAD) -> LaneEvidenceEnvelope:
    return LaneEvidenceEnvelope(workspace=workspace, lane=lane, lane_generation=gen, head=head)


def _request_note(head=HEAD) -> str:
    return "review request\n" + render_workflow_event_marker("review_request", target_head=head)


def _review_note(*, conclusion="approved", head=HEAD, gen=GEN, lane=LANE) -> str:
    return "review\n" + render_workflow_event_marker(
        "review_result",
        target_head=head,
        review_request_journal=REQ_JOURNAL,
        conclusion=conclusion,
        evidence_workspace=WS,
        evidence_lane=lane,
        evidence_lane_generation=gen,
    )


def _integration_note(**overrides) -> str:
    kwargs = dict(
        envelope=_env(),
        integration_head=STAGING_HEAD,
        integration_branch="main-next",
        disposition="merge",
    )
    kwargs.update(overrides)
    return "## Integration disposition\n" + render_integration_evidence(**kwargs)


def _ci_note(**overrides) -> str:
    return "ci\n" + render_hibernate_evidence(
        EVIDENCE_REQUIRED_CI_GREEN,
        envelope=overrides.pop("envelope", _env()),
        workflow="test.yml",
        run="299",
    )


def _dogfood_note(**overrides) -> str:
    return "dogfood\n" + render_hibernate_evidence(
        EVIDENCE_DOGFOOD_DELEGATED,
        envelope=overrides.pop("envelope", _env()),
        release_issue=RELEASE_ISSUE,
        acceptance="85431",
    )


#: A park marker must sit in the governed fixed-field park journal it claims.
PARK_FIELDS = (
    "- state: blocked\n- durable_anchor: #14219 j#85010\n- callback_result: sent\n"
    "- blocked_by: 14150\n"
    "- resume_condition: callback outcome journal\n- resume_owner: coordinator\n"
)


def _park_note(**overrides) -> str:
    return "park\n" + PARK_FIELDS + render_hibernate_evidence(
        EVIDENCE_PARK_DECLARED, envelope=overrides.pop("envelope", _env(head=""))
    )


def _issuer(role, *, lane=LANE, gen=GEN) -> ResolvedIssuer:
    """A port-resolved writer: the role AND the lane that writer holds it over."""
    return ResolvedIssuer(
        role=role, workspace=WS, lane=lane, lane_generation=gen, authority_anchor="j#84900"
    )


def _receipts(issue=ISSUE, head=HEAD) -> dict:
    return {
        RELEASE_ISSUE: DogfoodReceipt(
            release_issue=RELEASE_ISSUE, source_issue=issue, head=head
        )
    }


def _early_journals(**overrides) -> list:
    """A fully-evidenced early-hibernate issue; ``overrides`` replace one note by journal id."""
    journals = [
        (REQ_JOURNAL, _request_note(), ISSUER_LANE_WORKER),
        ("85001", _review_note(), ISSUER_REVIEW_GATEWAY),
        ("85002", _integration_note(), ISSUER_COORDINATOR),
        ("85003", _ci_note(), ISSUER_COORDINATOR),
        ("85004", _dogfood_note(), ISSUER_COORDINATOR),
    ]
    return [
        EvidenceJournal(jid, overrides.get(jid, note), _issuer(role))
        for jid, note, role in journals
    ]


def _selected(**over) -> SelectedLane:
    base = dict(
        issue_id=ISSUE, repo_workspace_id=WS, lane_id=LANE, lane_generation=GEN, revision=REV
    )
    base.update(over)
    return SelectedLane(**base)


def _push(head=HEAD, reachable=True) -> PushObservation:
    return PushObservation(
        workspace=WS, lane=LANE, lane_generation=GEN, head=head, reachable=reachable
    )


class _Ports:
    """Counting fakes for the three injected reads."""

    #: Distinguishes "caller did not override push" from "the observation is absent" — an
    #: ``is None`` default would have made the absent-observation test silently exercise the happy
    #: path (it did, until this sentinel).
    _DEFAULT = object()

    def __init__(self, *, journals=None, records=(_Rec(),), push=_DEFAULT, receipts=None):
        self.journals = journals
        self.records = records
        self.receipts = _receipts() if receipts is None else receipts
        self.push = _push() if push is self._DEFAULT else push
        self.journal_calls = 0
        self.push_calls = 0
        self.record_calls = 0

    def journals_fn(self, issue):
        self.journal_calls += 1
        return self.journals

    def push_fn(self, selected):
        self.push_calls += 1
        return self.push

    def records_fn(self):
        self.record_calls += 1
        return self.records

    def assembler(self, obligations_fn=lambda c: None) -> HibernateCandidateAssembler:
        return HibernateCandidateAssembler(
            records_fn=self.records_fn,
            journals_fn=self.journals_fn,
            push_fn=self.push_fn,
            obligations_fn=obligations_fn,
            dogfood_receipts_fn=lambda issue: self.receipts,
        )


class CandidateAssemblerTests(unittest.TestCase):
    def _assemble(self, *, basis=BASIS_EARLY_HIBERNATE, **port_kwargs):
        ports = _Ports(**port_kwargs)
        got = ports.assembler().assemble(AssemblyRequest(selected=_selected(), basis=basis))
        return got, ports

    def test_fully_evidenced_lane_assembles_a_candidate(self):
        got, _ = self._assemble(journals=_early_journals())
        self.assertIsInstance(got.verdict, HibernateCandidate)
        self.assertEqual(got.candidate.basis, BASIS_EARLY_HIBERNATE)
        # The decision journal is the newest durable record any produced conjunct rests on.
        self.assertEqual(got.decision_journal, "85004")
        self.assertEqual(
            {c.key for c in got.candidate.conjuncts},
            {
                "review_approved",
                "staging_integrated",
                "required_ci_green",
                "dogfood_delegated",
                "commits_pushed",
            },
        )

    def test_head_is_bound_from_the_git_remote_observation(self):
        got, _ = self._assemble(journals=_early_journals())
        self.assertEqual(got.candidate.head.provenance, PROVENANCE_GIT_REMOTE)
        self.assertEqual(got.candidate.head.value, HEAD)

    def test_a_review_marker_head_cannot_bind_the_candidate_head(self):
        # If the head were taken from the review marker, a lane whose observed head has moved on
        # would still "match" its own stale review. The observation is the head authority, so the
        # marker is compared against it -> anchor mismatch, zero actuation.
        got, _ = self._assemble(
            journals=_early_journals(**{
                REQ_JOURNAL: _request_note(head=OTHER_HEAD),
                "85001": _review_note(head=OTHER_HEAD),
            })
        )
        self.assertIsInstance(got.verdict, HibernateNonCandidate)
        self.assertEqual(got.verdict.reason, NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_evidence_that_agrees_with_itself_cannot_outvote_the_observation(self):
        # The discriminating case for the head authority: EVERY marker names the same head, and it
        # is not the head the lane is actually at. Bound from the markers, this lane would look
        # fully evidenced and hibernate at a head that no longer exists on the lane; bound from the
        # observation, it is an anchor mismatch. (A single disagreeing marker cannot tell the two
        # rules apart — the other conjuncts would mismatch either way.)
        # The delegation's receipt agrees with the markers too — every durable record is
        # internally consistent, and only the observation disagrees.
        stale = _early_journals(
            **{
                REQ_JOURNAL: _request_note(head=OTHER_HEAD),
                "85001": _review_note(head=OTHER_HEAD),
                "85002": _integration_note(envelope=_env(head=OTHER_HEAD)),
                "85003": _ci_note(envelope=_env(head=OTHER_HEAD)),
                "85004": _dogfood_note(envelope=_env(head=OTHER_HEAD)),
            }
        )
        got, _ = self._assemble(journals=stale, receipts=_receipts(head=OTHER_HEAD))
        self.assertIsInstance(got.verdict, HibernateNonCandidate)
        self.assertEqual(got.verdict.reason, NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_unreadable_journal_source_is_typed_and_short_circuits(self):
        got, ports = self._assemble(journals=None)
        self.assertIsInstance(got.verdict, HibernateNonCandidate)
        self.assertEqual(got.verdict.reason, NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN)
        self.assertEqual(got.verdict.detail, DETAIL_JOURNAL_SOURCE_UNREADABLE)
        self.assertIsNone(got.produced)
        # The evidence could not be read, so nothing downstream is observed or classified.
        self.assertEqual((ports.push_calls, ports.record_calls), (0, 0))

    def test_absent_push_observation_leaves_the_head_unbound(self):
        got, ports = self._assemble(journals=_early_journals(), push=None)
        self.assertIsInstance(got.verdict, HibernateNonCandidate)
        self.assertEqual(got.verdict.reason, NON_CANDIDATE_HEAD_UNBOUND)
        self.assertEqual(got.verdict.detail, GAP_PUSH_OBSERVATION_ABSENT)
        self.assertEqual(ports.record_calls, 0)

    def test_unreachable_head_is_an_unsatisfied_conjunct_not_a_bound_head(self):
        got, _ = self._assemble(journals=_early_journals(), push=_push(reachable=False))
        self.assertIsInstance(got.verdict, HibernateNonCandidate)
        self.assertEqual(got.verdict.reason, "basis_unsatisfied")

    def test_a_malformed_observed_head_is_refused_under_every_basis(self):
        # Checkpoint j#86525 R4-F4: under early hibernate a malformed head was rejected only as a
        # side effect (the head-bearing conjuncts carry full SHAs, so nothing matched it), but a
        # dependency park has no head-bearing conjunct and the same head sailed through. The head
        # contract cannot depend on which basis happens to be declared.
        for basis, journals in (
            (BASIS_DEPENDENCY_PARK, [EvidenceJournal("85010", _park_note(), _issuer(ISSUER_LANE_WORKER))]),
            (BASIS_EARLY_HIBERNATE, _early_journals()),
        ):
            for bad in ("not-a-full-sha", "a" * 39, ("A" * 40), "a" * 41):
                with self.subTest(basis=basis, head=bad):
                    ports = _Ports(journals=journals, push=_push(head=bad))
                    got = ports.assembler().assemble(
                        AssemblyRequest(selected=_selected(), basis=basis)
                    )
                    self.assertIsInstance(got.verdict, HibernateNonCandidate)
                    self.assertEqual(got.verdict.reason, NON_CANDIDATE_HEAD_MALFORMED)

    def test_a_sha256_head_is_still_accepted(self):
        # Negative control for the head shape: the repo's canonical predicate accepts 40 OR 64 hex,
        # so the guard must not narrow the contract to sha1 while closing the malformed case.
        ports = _Ports(
            journals=[EvidenceJournal("85010", _park_note(), _issuer(ISSUER_LANE_WORKER))],
            push=_push(head="b" * 64),
        )
        got = ports.assembler().assemble(
            AssemblyRequest(selected=_selected(), basis=BASIS_DEPENDENCY_PARK)
        )
        self.assertIsInstance(got.verdict, HibernateCandidate)

    def test_unreadable_lifecycle_store_is_typed(self):
        got, _ = self._assemble(journals=_early_journals(), records=None)
        self.assertIsInstance(got.verdict, HibernateNonCandidate)
        self.assertEqual(got.verdict.reason, NON_CANDIDATE_LIFECYCLE_UNREADABLE)

    def test_dependency_park_needs_only_its_own_declaration(self):
        got, _ = self._assemble(
            journals=[EvidenceJournal("85010", _park_note(), _issuer(ISSUER_LANE_WORKER))], basis=BASIS_DEPENDENCY_PARK
        )
        self.assertIsInstance(got.verdict, HibernateCandidate)
        self.assertEqual(got.decision_journal, "85010")

    def test_assemble_all_keeps_non_candidates(self):
        ports = _Ports(journals=[EvidenceJournal("85010", _park_note(), _issuer(ISSUER_LANE_WORKER))])
        got = ports.assembler().assemble_all([
            AssemblyRequest(selected=_selected(), basis=BASIS_DEPENDENCY_PARK),
            AssemblyRequest(selected=_selected(), basis=BASIS_EARLY_HIBERNATE),
        ])
        self.assertEqual(len(got), 2)
        self.assertIsInstance(got[0].verdict, HibernateCandidate)
        self.assertIsInstance(got[1].verdict, HibernateNonCandidate)
        self.assertEqual(got[1].verdict.reason, NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN)


class PassSeamTests(unittest.TestCase):
    def _seams_for(self, ports):
        assembler = ports.assembler(obligations_fn=lambda c: "obligations")
        candidate = assembler.assemble(
            AssemblyRequest(selected=_selected(), basis=BASIS_EARLY_HIBERNATE)
        ).candidate
        self.assertIsNotNone(candidate)
        return assembler, candidate

    def test_journal_and_refresh_share_one_observation_per_candidate(self):
        ports = _Ports(journals=_early_journals())
        assembler, candidate = self._seams_for(ports)
        before = ports.journal_calls
        seams = assembler.pass_seams()
        journal = seams.journal_fn(candidate)
        refreshed = seams.refresh_fn(candidate)
        # Exactly ONE re-read backs both seams: no second TOCTOU window between them.
        self.assertEqual(ports.journal_calls - before, 1)
        self.assertEqual(journal, "85004")
        self.assertEqual(refreshed, candidate)

    def test_a_new_pass_re_observes(self):
        ports = _Ports(journals=_early_journals())
        assembler, candidate = self._seams_for(ports)
        before = ports.journal_calls
        assembler.pass_seams().refresh_fn(candidate)
        assembler.pass_seams().refresh_fn(candidate)
        self.assertEqual(ports.journal_calls - before, 2)

    def test_refresh_is_none_and_journal_empty_once_the_basis_lapses(self):
        ports = _Ports(journals=_early_journals())
        assembler, candidate = self._seams_for(ports)
        # Between build and actuation the reviewer supersedes the approval.
        ports.journals = _early_journals() + [
            EvidenceJournal(
                "85009",
                _review_note(conclusion="changes_requested"),
                _issuer(ISSUER_REVIEW_GATEWAY),
            )
        ]
        seams = assembler.pass_seams()
        self.assertEqual(seams.journal_fn(candidate), "")
        self.assertIsNone(seams.refresh_fn(candidate))

    def test_obligations_are_passed_through_untouched(self):
        ports = _Ports(journals=_early_journals())
        assembler, candidate = self._seams_for(ports)
        self.assertEqual(assembler.pass_seams().obligations_fn(candidate), "obligations")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
