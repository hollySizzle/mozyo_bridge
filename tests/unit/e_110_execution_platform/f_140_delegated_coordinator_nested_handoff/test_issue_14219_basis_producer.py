"""Durable evidence → basis conjunct producer tests (Redmine #14219 T2b, step 4).

Pins the three producer invariants:

- **no target-binding** — the producer transcribes the identity the EVIDENCE declares, so a
  cross-lane / old-generation / head-drifted record still reaches T1 and is rejected THERE
  (``conjunct_anchor_mismatch``) instead of being absorbed into a matching-looking conjunct;
- **latest declaration wins by existing** — a newer ``changes_requested`` shadows an older
  ``approved``, and a newer legacy (lane-unbound) marker shadows an older enveloped one;
- **negative vs unreadable stay distinct** — an explicit non-approval / unreachable head is an
  unsatisfied conjunct, while a deferral / non-success CI / absent record is a typed gap that keeps
  its own reason.

Plus the end-to-end shape: a fully-evidenced early-hibernate lane classifies as a candidate, and
each single defect knocks it back to a typed non-candidate.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from mozyo_bridge.core.state.lane_lifecycle_model import DISPOSITION_ACTIVE

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    hibernate_basis_producer as bp,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_candidate import (  # noqa: E501
    BASIS_DEPENDENCY_PARK,
    BASIS_EARLY_HIBERNATE,
    CONJUNCT_COMMITS_PUSHED,
    CONJUNCT_DOGFOOD_DELEGATED,
    CONJUNCT_PARK_DECLARED,
    CONJUNCT_REQUIRED_CI_GREEN,
    CONJUNCT_REVIEW_APPROVED,
    CONJUNCT_STAGING_INTEGRATED,
    NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN,
    NON_CANDIDATE_BASIS_UNSATISFIED,
    NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH,
    PROVENANCE_REVIEW_RECORD,
    BoundField,
    HibernateCandidate,
    HibernateNonCandidate,
    SelectedLane,
    classify_hibernate_candidate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    ISSUER_COORDINATOR,
    ISSUER_LANE_WORKER,
    ISSUER_REVIEW_GATEWAY,
    EvidenceJournal,
    ResolvedIssuer,
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
REV = 7
WS = "ws-1"
LANE = "lane-abc"
GEN = 4
HEAD = "a" * 40
STAGING_HEAD = "b" * 40
REQ_JOURNAL = "85390"
RELEASE_ISSUE = "14184"
ACCEPTANCE = "85431"


@dataclass(frozen=True)
class _Rec:
    """A minimal stand-in for the fields ``bind_lifecycle_anchor`` reads off a lifecycle record."""

    issue_id: str = ISSUE
    repo_workspace_id: str = WS
    lane_id: str = LANE
    lane_generation: int = GEN
    revision: int = REV
    lane_disposition: str = DISPOSITION_ACTIVE


def _env(workspace=WS, lane=LANE, gen=GEN, head=HEAD):
    return LaneEvidenceEnvelope(workspace=workspace, lane=lane, lane_generation=gen, head=head)


def _review_note(*, conclusion="approved", head=HEAD, enveloped=True, gen=GEN, lane=LANE):
    kwargs = dict(target_head=head, review_request_journal=REQ_JOURNAL, conclusion=conclusion)
    if enveloped:
        kwargs.update(
            evidence_workspace=WS, evidence_lane=lane, evidence_lane_generation=gen
        )
    return "review\n" + render_workflow_event_marker("review_result", **kwargs)


def _integration_note(**overrides):
    kwargs = dict(
        envelope=_env(),
        integration_head=STAGING_HEAD,
        integration_branch="main-next",
        disposition="merge",
    )
    kwargs.update(overrides)
    return "integration\n" + render_integration_evidence(**kwargs)


def _ci_note(**overrides):
    return "ci\n" + render_hibernate_evidence(
        EVIDENCE_REQUIRED_CI_GREEN,
        envelope=overrides.pop("envelope", _env()),
        workflow=overrides.pop("workflow", "test.yml"),
        run="299",
        **overrides,
    )


def _dogfood_note(**overrides):
    return "dogfood\n" + render_hibernate_evidence(
        EVIDENCE_DOGFOOD_DELEGATED,
        envelope=overrides.pop("envelope", _env()),
        release_issue=overrides.pop("release_issue", RELEASE_ISSUE),
        acceptance=ACCEPTANCE,
    )


def _receipts(**overrides):
    """The release issue's own receipt for the delegation (the corroborating record)."""
    base = dict(release_issue=RELEASE_ISSUE, source_issue=ISSUE, head=HEAD)
    base.update(overrides)
    return {base["release_issue"]: bp.DogfoodReceipt(**base)}


#: The governed fixed-field park journal a park declaration must sit in.
PARK_FIELDS = (
    "- state: blocked\n"
    "- durable_anchor: #14219 j#85500\n"
    "- callback_result: sent\n"
    "- blocked_by: 14150\n"
    "- resume_condition: #14150 の callback outcome journal 到達\n"
    "- resume_owner: coordinator\n"
)


def _review_note_answering(req, *, head=HEAD):
    """A review_result naming an explicit request journal (for the ordering / mixed-round cases)."""
    return "review\n" + render_workflow_event_marker(
        "review_result",
        target_head=head,
        review_request_journal=req,
        conclusion="approved",
        evidence_workspace=WS,
        evidence_lane=LANE,
        evidence_lane_generation=GEN,
    )


def _request_note(head=HEAD):
    return "review request\n" + render_workflow_event_marker(
        "review_request", target_head=head
    )


def _park_note(**overrides):
    fields = overrides.pop("park_fields", PARK_FIELDS)
    return "park\n" + fields + render_hibernate_evidence(
        EVIDENCE_PARK_DECLARED, envelope=overrides.pop("envelope", _env(head=""))
    )


def _push(reachable=True, head=HEAD, lane=LANE, gen=GEN):
    return bp.PushObservation(
        workspace=WS, lane=lane, lane_generation=gen, head=head, reachable=reachable
    )


def _issuer(role, *, workspace=WS, lane=LANE, gen=GEN, anchor="j#85300"):
    """A port-resolved issuer: the role AND the lane that writer holds it over."""
    return ResolvedIssuer(
        role=role, workspace=workspace, lane=lane, lane_generation=gen, authority_anchor=anchor
    )


def _journal(journal_id, notes, role, **issuer_over):
    return EvidenceJournal(
        journal_id=journal_id, notes=notes, issuer=_issuer(role, **issuer_over)
    )


def _review_only_journals():
    """A request + its approval, and nothing else — the other conjuncts are simply absent."""
    return [
        _journal(REQ_JOURNAL, _request_note(), ISSUER_LANE_WORKER),
        _journal("85400", _review_note(), ISSUER_REVIEW_GATEWAY),
    ]


def _park_journals(**overrides):
    return [_journal("85500", overrides.get("park", _park_note()), ISSUER_LANE_WORKER)]


def _early_journals(*, review_issuer=None, **overrides):
    """The durable evidence journals an early-hibernate lane rests on, with their writers.

    ``review_issuer`` overrides the review record's resolved writer — needed whenever the review
    itself is about another lane / generation, because that lane's gateway (not this one's) is the
    actor who would have written it.
    """
    return [
        _journal(REQ_JOURNAL, overrides.get("request", _request_note()), ISSUER_LANE_WORKER),
        EvidenceJournal(
            "85400",
            overrides.get("review", _review_note()),
            review_issuer or _issuer(ISSUER_REVIEW_GATEWAY),
        ),
        _journal("85410", overrides.get("integration", _integration_note()), ISSUER_COORDINATOR),
        _journal("85420", overrides.get("ci", _ci_note()), ISSUER_COORDINATOR),
        _journal("85430", overrides.get("dogfood", _dogfood_note()), ISSUER_COORDINATOR),
    ]


def _produce(journals=None, *, basis=BASIS_EARLY_HIBERNATE, push=None, receipts=None):
    return bp.produce_basis_conjuncts(
        journals if journals is not None else _early_journals(),
        basis=basis,
        source_issue=ISSUE,
        push=push if push is not None else _push(),
        dogfood_receipts=_receipts() if receipts is None else receipts,
    )


def _by_key(produced):
    return {c.key: c for c in produced.conjuncts}


def _gaps(produced):
    return {g.key: g.reason for g in produced.gaps}


class FullyEvidencedProductionTests(unittest.TestCase):
    def test_all_five_early_conjuncts_are_produced_and_satisfied(self):
        produced = _produce()
        self.assertEqual(produced.gaps, ())
        keys = _by_key(produced)
        self.assertEqual(set(keys), {
            CONJUNCT_REVIEW_APPROVED,
            CONJUNCT_STAGING_INTEGRATED,
            CONJUNCT_REQUIRED_CI_GREEN,
            CONJUNCT_DOGFOOD_DELEGATED,
            CONJUNCT_COMMITS_PUSHED,
        })
        for key, conjunct in keys.items():
            self.assertTrue(conjunct.satisfied, key)
            self.assertEqual(
                (conjunct.bound_workspace, conjunct.bound_lane, conjunct.bound_generation),
                (WS, LANE, GEN),
                key,
            )
            self.assertEqual(conjunct.bound_head, HEAD, key)

    def test_staging_conjunct_binds_the_source_head_not_the_integration_head(self):
        produced = _produce()
        self.assertEqual(_by_key(produced)[CONJUNCT_STAGING_INTEGRATED].bound_head, HEAD)

    def test_decision_journal_is_the_newest_evidence_journal(self):
        self.assertEqual(_produce().decision_journal, "85430")

    def test_park_basis_produces_the_lane_anchored_conjunct(self):
        produced = _produce(_park_journals(), basis=BASIS_DEPENDENCY_PARK)
        self.assertEqual(produced.gaps, ())
        conjunct = _by_key(produced)[CONJUNCT_PARK_DECLARED]
        self.assertTrue(conjunct.satisfied)
        self.assertEqual(conjunct.bound_head, "")
        self.assertEqual(conjunct.bound_generation, GEN)

    def test_park_basis_does_not_require_the_early_conjuncts(self):
        produced = _produce(_park_journals(), basis=BASIS_DEPENDENCY_PARK, push=_push())
        self.assertEqual({c.key for c in produced.conjuncts}, {CONJUNCT_PARK_DECLARED})


class LatestDeclarationWinsTests(unittest.TestCase):
    def test_newer_changes_requested_shadows_older_approval(self):
        journals = _early_journals()
        journals.append(_journal("85440", _review_note(conclusion="changes_requested"), ISSUER_REVIEW_GATEWAY))
        produced = _produce(journals)
        self.assertFalse(_by_key(produced)[CONJUNCT_REVIEW_APPROVED].satisfied)

    def test_older_approval_does_not_resurface_when_newer_is_legacy(self):
        # The newer marker carries no envelope: it supersedes by EXISTING, so the conjunct is a gap
        # rather than the stale enveloped approval.
        journals = _early_journals()
        journals.append(_journal("85440", _review_note(enveloped=False), ISSUER_REVIEW_GATEWAY))
        produced = _produce(journals)
        self.assertNotIn(CONJUNCT_REVIEW_APPROVED, _by_key(produced))
        self.assertIn(CONJUNCT_REVIEW_APPROVED, _gaps(produced))

    def test_newer_deferral_shadows_older_merge(self):
        journals = _early_journals()
        journals.append(_journal(
            "85450",
            "## Integration disposition: explicit_deferral\n\n- next_owner: coordinator\n",
            ISSUER_COORDINATOR,
        ))
        produced = _produce(journals)
        self.assertNotIn(CONJUNCT_STAGING_INTEGRATED, _by_key(produced))
        self.assertIn(CONJUNCT_STAGING_INTEGRATED, _gaps(produced))

    def test_an_older_journal_never_overrides_a_newer_one(self):
        journals = list(reversed(_early_journals()))
        journals.append(_journal("85300", _review_note(conclusion="changes_requested"), ISSUER_REVIEW_GATEWAY))
        self.assertTrue(_by_key(_produce(journals))[CONJUNCT_REVIEW_APPROVED].satisfied)


class NegativeVsUnreadableTests(unittest.TestCase):
    def test_explicit_non_approval_is_an_unsatisfied_conjunct(self):
        produced = _produce(
            _early_journals(review=_review_note(conclusion="changes_requested"))
        )
        self.assertFalse(_by_key(produced)[CONJUNCT_REVIEW_APPROVED].satisfied)
        self.assertEqual(_gaps(produced), {})

    def test_unreachable_head_is_an_unsatisfied_conjunct(self):
        produced = _produce(push=_push(reachable=False))
        self.assertFalse(_by_key(produced)[CONJUNCT_COMMITS_PUSHED].satisfied)

    def test_absent_evidence_is_a_gap(self):
        produced = _produce(_review_only_journals())
        self.assertEqual(
            _gaps(produced),
            {
                CONJUNCT_STAGING_INTEGRATED: bp.GAP_EVIDENCE_ABSENT,
                CONJUNCT_REQUIRED_CI_GREEN: bp.GAP_EVIDENCE_ABSENT,
                CONJUNCT_DOGFOOD_DELEGATED: bp.GAP_EVIDENCE_ABSENT,
            },
        )

    def test_absent_push_observation_is_a_gap(self):
        produced = bp.produce_basis_conjuncts(_early_journals(), basis=BASIS_EARLY_HIBERNATE)
        self.assertEqual(_gaps(produced)[CONJUNCT_COMMITS_PUSHED], bp.GAP_PUSH_OBSERVATION_ABSENT)

    def test_deferral_gap_keeps_its_own_reason(self):
        journals = _early_journals()
        journals.append(_journal(
            "85450", "## Integration disposition: explicit_deferral\n", ISSUER_COORDINATOR
        ))
        self.assertNotEqual(
            _gaps(_produce(journals))[CONJUNCT_STAGING_INTEGRATED], bp.GAP_EVIDENCE_ABSENT
        )

    def test_missing_review_conclusion_is_a_gap_not_an_approval(self):
        marker = "[mozyo:workflow-event:gate=review_result:head={h}:req=1:workspace={w}:lane={l}:lane_generation={g}]".format(  # noqa: E501
            h=HEAD, w=WS, l=LANE, g=GEN
        )
        produced = _produce(_early_journals(review=marker))
        self.assertEqual(
            _gaps(produced)[CONJUNCT_REVIEW_APPROVED], bp.GAP_REVIEW_MISSING_CONCLUSION
        )

    def test_ci_not_success_keeps_its_own_reason(self):
        marker = (
            "[mozyo:workflow-event:gate=required_ci_green:workspace={w}:lane={l}:"
            "lane_generation={g}:head={h}:workflow=test.yml:run=299:conclusion=failure]"
        ).format(w=WS, l=LANE, g=GEN, h=HEAD)
        produced = _produce(_early_journals(ci=marker))
        self.assertEqual(_gaps(produced)[CONJUNCT_REQUIRED_CI_GREEN], "evidence_ci_not_success")


class ProducerNeverBindsToTheTargetTests(unittest.TestCase):
    """A drifted record must reach T1 with the identity IT declares, not the candidate's."""

    def test_cross_lane_review_is_transcribed_as_the_other_lane(self):
        # Written by the OTHER lane's gateway about the other lane — a genuine record, correctly
        # issued, simply not about this candidate. It reaches T1 carrying the lane it declares.
        produced = _produce(_early_journals(
            review=_review_note(lane="other-lane"),
            review_issuer=_issuer(ISSUER_REVIEW_GATEWAY, lane="other-lane"),
        ))
        self.assertEqual(_by_key(produced)[CONJUNCT_REVIEW_APPROVED].bound_lane, "other-lane")

    def test_old_generation_review_is_transcribed_as_the_old_generation(self):
        produced = _produce(_early_journals(
            review=_review_note(gen=GEN - 1),
            review_issuer=_issuer(ISSUER_REVIEW_GATEWAY, gen=GEN - 1),
        ))
        self.assertEqual(_by_key(produced)[CONJUNCT_REVIEW_APPROVED].bound_generation, GEN - 1)

    def test_drifted_head_is_transcribed_as_the_drifted_head(self):
        # A review that is internally consistent (its request asked about the SAME other head) —
        # so it survives the request correlation and reaches T1 carrying the head it declares,
        # which is what T1 must reject. The producer never quietly re-points it at the target.
        other = "c" * 40
        produced = _produce(_early_journals(
            request=_request_note(head=other), review=_review_note(head=other)
        ))
        self.assertEqual(_by_key(produced)[CONJUNCT_REVIEW_APPROVED].bound_head, other)


class ReviewRequestCorrelationTests(unittest.TestCase):
    """Checkpoint review j#86389 F1: an approval is evidence only of the review it answers.

    Review Generation Marker Contract v2 makes ``req`` mandatory precisely so a conclusion can be
    tied to one review generation. Without the correlation, any enveloped ``approved`` — hand
    written, superseded, or about a different question — satisfied the conjunct.
    """

    def _review_gap(self, journals):
        return _gaps(_produce(journals)).get(CONJUNCT_REVIEW_APPROVED)

    def test_an_approval_without_req_is_a_gap(self):
        marker = (
            "[mozyo:workflow-event:gate=review_result:conclusion=approved:head={h}:"
            "workspace={w}:lane={l}:lane_generation={g}]"
        ).format(h=HEAD, w=WS, l=LANE, g=GEN)
        self.assertEqual(
            self._review_gap(_early_journals(review="review\n" + marker)),
            bp.GAP_REVIEW_MISSING_REQ,
        )

    def test_an_approval_with_no_review_request_at_all_is_a_gap(self):
        journals = [j for j in _early_journals() if j.journal_id != REQ_JOURNAL]
        self.assertEqual(self._review_gap(journals), bp.GAP_REVIEW_REQUEST_ABSENT)

    def test_an_approval_written_before_the_request_it_names_is_a_gap(self):
        # Checkpoint j#86443 R2-F1: a result at journal 100 naming `req=200`, with the request
        # arriving later at 200, previously became a satisfied conjunct — a pre-written approval
        # activated retroactively by a future request. A result answers the request BEFORE it.
        result = _journal("100", _review_note(), ISSUER_REVIEW_GATEWAY)
        later_request = _journal("200", _request_note(), ISSUER_LANE_WORKER)
        journals = [result, later_request]
        gaps = _gaps(_produce(journals))
        self.assertIn(gaps.get(CONJUNCT_REVIEW_APPROVED), {
            bp.GAP_REVIEW_REQUEST_SUPERSEDED, bp.GAP_REVIEW_REQUEST_UNCORRELATED
        })

    def test_a_request_in_the_same_journal_as_the_result_does_not_correlate(self):
        # Redmine ids are monotonic: an answer cannot share a record with its own question. The
        # result NAMES its own journal, so only the strictly-before rule refuses it — a rule that
        # accepted `>=` would find a matching request and call the approval correlated.
        review = "review\n" + render_workflow_event_marker(
            "review_result",
            target_head=HEAD,
            review_request_journal="300",
            conclusion="approved",
            evidence_workspace=WS,
            evidence_lane=LANE,
            evidence_lane_generation=GEN,
        )
        note = _request_note() + "\n" + review
        self.assertEqual(
            _gaps(_produce([_journal("300", note, ISSUER_REVIEW_GATEWAY)])).get(
                CONJUNCT_REVIEW_APPROVED
            ),
            bp.GAP_REVIEW_JOURNAL_CONTRADICTORY,
        )

    def test_a_result_that_also_opens_a_fresh_round_is_contradictory(self):
        # Checkpoint j#86503 R3-F1: the mixed-round shape — this journal ANSWERS the round opened
        # at 100 and OPENS a new one in the same record. The strictly-before correlation looks only
        # before this journal and the supersession check only after it, so the two rules meet
        # exactly here and neither saw it. Refused on its own terms, as the glance already does.
        journals = [
            _journal("100", _request_note(), ISSUER_LANE_WORKER),
            _journal(
                "200",
                _review_note_answering("100") + "\n" + _request_note(),
                ISSUER_REVIEW_GATEWAY,
            ),
        ]
        self.assertEqual(
            _gaps(_produce(journals)).get(CONJUNCT_REVIEW_APPROVED),
            bp.GAP_REVIEW_JOURNAL_CONTRADICTORY,
        )

    def test_the_same_result_without_the_fresh_request_still_correlates(self):
        # Negative control: it is the CO-PRESENCE that is contradictory, not the shape of a result
        # answering an earlier journal.
        journals = [
            _journal("100", _request_note(), ISSUER_LANE_WORKER),
            _journal("200", _review_note_answering("100"), ISSUER_REVIEW_GATEWAY),
        ]
        produced = _produce(journals)
        self.assertNotIn(CONJUNCT_REVIEW_APPROVED, _gaps(produced))
        self.assertTrue(_by_key(produced)[CONJUNCT_REVIEW_APPROVED].satisfied)

    def test_the_answered_request_is_the_nearest_preceding_one(self):
        # Two rounds: the result must correlate with round 2's request, not round 1's.
        journals = [
            _journal("100", _request_note(), ISSUER_LANE_WORKER),
            _journal("200", _request_note(), ISSUER_LANE_WORKER),
            _journal("300", _review_note(), ISSUER_REVIEW_GATEWAY),
        ]
        # `_review_note` names REQ_JOURNAL, which is neither: uncorrelated.
        self.assertEqual(
            _gaps(_produce(journals)).get(CONJUNCT_REVIEW_APPROVED),
            bp.GAP_REVIEW_REQUEST_UNCORRELATED,
        )

    def test_an_approval_answering_a_superseded_request_is_a_gap(self):
        # A newer review_request opens a new review generation; the old approval answers the old
        # question and must not carry over to the new one.
        journals = _early_journals()
        journals.append(_journal("85460", _request_note(), ISSUER_LANE_WORKER))
        self.assertEqual(self._review_gap(journals), bp.GAP_REVIEW_REQUEST_SUPERSEDED)

    def test_an_approval_disagreeing_with_its_request_about_the_head_is_a_gap(self):
        journals = _early_journals(request=_request_note(head="c" * 40))
        self.assertEqual(self._review_gap(journals), bp.GAP_REVIEW_REQUEST_HEAD_MISMATCH)

    def test_a_correlated_approval_still_satisfies(self):
        # Negative control: the correlation rejects the uncorrelated, not every approval.
        self.assertTrue(_by_key(_produce())[CONJUNCT_REVIEW_APPROVED].satisfied)


class IssuerAuthorityTests(unittest.TestCase):
    """Checkpoint review j#86389 F2: the marker cannot confer the authority it claims.

    Each conjunct's provenance says the evidence came from a specific authority. That has to be a
    fact about the WRITER — otherwise any actor's coordinator-shaped record reads as the
    coordinator's.
    """

    def _with_role(self, key, notes_key, role):
        journals = _early_journals()
        replaced = []
        for journal in journals:
            if notes_key in journal.notes.split("\n", 1)[0]:
                replaced.append(_journal(journal.journal_id, journal.notes, role))
            else:
                replaced.append(journal)
        return _gaps(_produce(replaced)).get(key)

    def test_a_worker_written_ci_record_is_not_the_coordinators(self):
        self.assertEqual(
            self._with_role(CONJUNCT_REQUIRED_CI_GREEN, "ci", ISSUER_LANE_WORKER),
            "evidence_issuer_mismatch",
        )

    def test_a_coordinator_written_review_result_is_not_the_gateways(self):
        self.assertEqual(
            self._with_role(CONJUNCT_REVIEW_APPROVED, "review", ISSUER_COORDINATOR),
            "evidence_issuer_mismatch",
        )

    def test_an_unresolved_writer_is_typed_and_distinct_from_a_mismatch(self):
        # An unattributed record blocks — but it says WHY: the port could not resolve the author,
        # which is an operational problem, not a wrong actor.
        journals = tuple(
            EvidenceJournal(journal_id=j.journal_id, notes=j.notes) for j in _early_journals()
        )
        gaps = _gaps(_produce(list(journals)))
        self.assertEqual(gaps.get(CONJUNCT_REVIEW_APPROVED), "evidence_issuer_unresolved")

    def test_a_foreign_lanes_worker_cannot_declare_this_lane_parked(self):
        # Checkpoint j#86443 R2-F2: role equality alone let ANY lane's worker declare THIS lane
        # parked, simply by writing this lane's envelope. The writer's own lane must be the lane
        # the evidence is about.
        journals = [EvidenceJournal(
            "85500", _park_note(), _issuer(ISSUER_LANE_WORKER, lane="other-lane")
        )]
        self.assertEqual(
            _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(CONJUNCT_PARK_DECLARED),
            "evidence_issuer_mismatch",
        )

    def test_a_superseded_generations_worker_cannot_declare_this_lane_parked(self):
        journals = [EvidenceJournal(
            "85500", _park_note(), _issuer(ISSUER_LANE_WORKER, gen=GEN - 1)
        )]
        self.assertEqual(
            _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(CONJUNCT_PARK_DECLARED),
            "evidence_issuer_mismatch",
        )

    def test_a_foreign_lanes_gateway_cannot_approve_this_lanes_review(self):
        journals = _early_journals(
            review_issuer=_issuer(ISSUER_REVIEW_GATEWAY, lane="other-lane")
        )
        self.assertEqual(
            _gaps(_produce(journals)).get(CONJUNCT_REVIEW_APPROVED), "evidence_issuer_mismatch"
        )

    def test_an_issuer_without_an_authority_anchor_is_unresolved(self):
        # The port must name the durable record it resolved the lane-role binding FROM: in this
        # workspace every governed journal shares one source-system author, so an unanchored
        # "this is the lane worker" is not a resolution.
        journals = [EvidenceJournal(
            "85500", _park_note(), _issuer(ISSUER_LANE_WORKER, anchor="")
        )]
        self.assertEqual(
            _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(CONJUNCT_PARK_DECLARED),
            "evidence_issuer_unresolved",
        )

    def test_an_unanchored_coordinator_is_unresolved_on_every_gate_it_owns(self):
        # Checkpoint j#86503 R3-F2: the anchor requirement reached only the lane-scoped roles, so a
        # bare ResolvedIssuer(role="coordinator") — no workspace, no lane, no anchor — satisfied
        # integration, CI and dogfood. The coordinator's authority is workspace-level, but that is
        # about SCOPE, not about whether the writer was identified at all.
        bare = ResolvedIssuer(role=ISSUER_COORDINATOR)
        for journal_id, key in (
            ("85410", CONJUNCT_STAGING_INTEGRATED),
            ("85420", CONJUNCT_REQUIRED_CI_GREEN),
            ("85430", CONJUNCT_DOGFOOD_DELEGATED),
        ):
            with self.subTest(conjunct=key):
                journals = [
                    EvidenceJournal(j.journal_id, j.notes, bare)
                    if j.journal_id == journal_id
                    else j
                    for j in _early_journals()
                ]
                self.assertEqual(
                    _gaps(_produce(journals)).get(key), "evidence_issuer_unresolved"
                )

    def test_the_coordinators_authority_is_not_lane_scoped(self):
        # Negative control for the lane comparison: the coordinator writes integration / CI /
        # dogfood records that are not the lane's own claims about itself, so requiring a lane
        # identity there would block every legitimate coordinator record.
        journals = _early_journals()
        produced = _produce(journals)
        self.assertEqual(produced.gaps, ())
        self.assertTrue(_by_key(produced)[CONJUNCT_REQUIRED_CI_GREEN].satisfied)

    def test_the_issuer_of_the_current_declaration_is_the_one_judged(self):
        # A newer CI record written by the wrong actor is not rescued by the older well-written one.
        journals = _early_journals()
        journals.append(_journal("85470", _ci_note(), ISSUER_LANE_WORKER))
        self.assertEqual(
            _gaps(_produce(journals)).get(CONJUNCT_REQUIRED_CI_GREEN), "evidence_issuer_mismatch"
        )


class CorroborationTests(unittest.TestCase):
    """Checkpoint review j#86389 F3: a claim the issuer alone controls is not corroboration."""

    def test_a_delegation_without_the_release_issues_receipt_is_a_gap(self):
        self.assertEqual(
            _gaps(_produce(receipts={})).get(CONJUNCT_DOGFOOD_DELEGATED),
            bp.GAP_DOGFOOD_RECEIPT_ABSENT,
        )

    def test_a_receipt_for_another_head_is_a_gap(self):
        self.assertEqual(
            _gaps(_produce(receipts=_receipts(head="c" * 40))).get(CONJUNCT_DOGFOOD_DELEGATED),
            bp.GAP_DOGFOOD_RECEIPT_MISMATCH,
        )

    def test_a_receipt_for_another_source_issue_is_a_gap(self):
        self.assertEqual(
            _gaps(_produce(receipts=_receipts(source_issue="99999"))).get(
                CONJUNCT_DOGFOOD_DELEGATED
            ),
            bp.GAP_DOGFOOD_RECEIPT_MISMATCH,
        )

    def test_a_park_marker_without_the_governed_park_journal_is_a_gap(self):
        journals = _park_journals(park=_park_note(park_fields=""))
        self.assertEqual(
            _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(CONJUNCT_PARK_DECLARED),
            bp.GAP_PARK_JOURNAL_FIELDS_ABSENT,
        )

    def test_a_park_journal_missing_one_governed_field_is_a_gap(self):
        partial = "- state: blocked\n- blocked_by: 14150\n- resume_owner: coordinator\n"
        journals = _park_journals(park=_park_note(park_fields=partial))
        self.assertEqual(
            _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(CONJUNCT_PARK_DECLARED),
            bp.GAP_PARK_JOURNAL_FIELDS_ABSENT,
        )

    def test_a_park_journal_that_never_called_back_is_a_gap(self):
        # Checkpoint j#86443 R2-F4: the parked state is a handoff-worthy `blocked` state, so the
        # governed record includes `callback_result` (and the `durable_anchor` it is filed
        # against). Checking only the dependency fields let exactly the failure the guardrail was
        # written for -- a park nobody was told about -- read as an affirmative basis.
        for missing in ("callback_result", "durable_anchor"):
            with self.subTest(missing=missing):
                fields = "".join(
                    line + "\n"
                    for line in PARK_FIELDS.strip().split("\n")
                    if not line.startswith(f"- {missing}:")
                )
                journals = _park_journals(park=_park_note(park_fields=fields))
                self.assertEqual(
                    _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(
                        CONJUNCT_PARK_DECLARED
                    ),
                    bp.GAP_PARK_JOURNAL_FIELDS_ABSENT,
                )

    def test_a_park_journal_with_an_invented_field_value_is_a_gap(self):
        # Checkpoint j#86503 R3-F3: presence is not the contract. The skill's fixed field shape
        # pins the VALUES — `callback_result: sent | blocked | not-attempted`,
        # `resume_owner: coordinator`, `durable_anchor: #<issue> j#<journal>` — and a record that
        # merely has the field names is the same class of defect as a marker asserting its own
        # authority: the shape looked right, so nobody read the content.
        for field, bad in (
            ("callback_result", "invented"),
            ("callback_result", "SENT-ish"),
            ("resume_owner", "worker"),
            ("durable_anchor", "totally unrelated text"),
            ("durable_anchor", "#99999 j#85500"),   # a real shape, but another issue's record
        ):
            with self.subTest(field=field, value=bad):
                fields = "".join(
                    (f"- {field}: {bad}\n" if line.startswith(f"- {field}:") else line + "\n")
                    for line in PARK_FIELDS.strip().split("\n")
                )
                journals = _park_journals(park=_park_note(park_fields=fields))
                self.assertEqual(
                    _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(
                        CONJUNCT_PARK_DECLARED
                    ),
                    bp.GAP_PARK_JOURNAL_FIELDS_INVALID,
                )

    def test_every_governed_callback_result_value_is_accepted(self):
        # Negative control for the closed vocabulary: it refuses inventions, not the real values.
        # `blocked` / `not-attempted` are legitimate outcomes — the guardrail requires a RECORD of
        # the callback, not a successful one.
        for value in ("sent", "blocked", "not-attempted"):
            with self.subTest(callback_result=value):
                fields = PARK_FIELDS.replace("- callback_result: sent", f"- callback_result: {value}")
                journals = _park_journals(park=_park_note(park_fields=fields))
                produced = _produce(journals, basis=BASIS_DEPENDENCY_PARK)
                self.assertEqual(produced.gaps, ())
                self.assertTrue(_by_key(produced)[CONJUNCT_PARK_DECLARED].satisfied)

    def test_a_fully_recorded_park_still_satisfies(self):
        produced = _produce(_park_journals(), basis=BASIS_DEPENDENCY_PARK)
        self.assertEqual(produced.gaps, ())
        self.assertTrue(_by_key(produced)[CONJUNCT_PARK_DECLARED].satisfied)


class EndToEndClassificationTests(unittest.TestCase):
    """The produced conjuncts drive the real T1 classifier."""

    def _classify(self, produced, *, head=HEAD):
        return classify_hibernate_candidate(
            selected=SelectedLane(
                issue_id=ISSUE, repo_workspace_id=WS, lane_id=LANE, lane_generation=GEN, revision=REV
            ),
            declared_basis=produced.basis,
            records=(_Rec(),),
            head=BoundField(value=head, provenance=PROVENANCE_REVIEW_RECORD),
            conjuncts=produced.conjuncts,
        )

    def test_fully_evidenced_lane_is_a_candidate(self):
        got = self._classify(_produce())
        self.assertIsInstance(got, HibernateCandidate)
        self.assertEqual(got.basis, BASIS_EARLY_HIBERNATE)

    def test_missing_evidence_is_partially_unknown(self):
        got = self._classify(_produce(_review_only_journals()))
        self.assertIsInstance(got, HibernateNonCandidate)
        self.assertEqual(got.reason, NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN)

    def test_explicit_non_approval_is_unsatisfied_not_unknown(self):
        got = self._classify(_produce(_early_journals(review=_review_note(conclusion="changes_requested"))))
        self.assertEqual(got.reason, NON_CANDIDATE_BASIS_UNSATISFIED)

    def test_cross_lane_evidence_is_an_anchor_mismatch(self):
        got = self._classify(_produce(_early_journals(
            review=_review_note(lane="other-lane"),
            review_issuer=_issuer(ISSUER_REVIEW_GATEWAY, lane="other-lane"),
        )))
        self.assertEqual(got.reason, NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_old_generation_evidence_is_an_anchor_mismatch(self):
        got = self._classify(_produce(_early_journals(
            review=_review_note(gen=GEN - 1),
            review_issuer=_issuer(ISSUER_REVIEW_GATEWAY, gen=GEN - 1),
        )))
        self.assertEqual(got.reason, NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_head_drifted_evidence_is_an_anchor_mismatch(self):
        other = "c" * 40
        got = self._classify(_produce(_early_journals(
            request=_request_note(head=other), review=_review_note(head=other)
        )))
        self.assertEqual(got.reason, NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
