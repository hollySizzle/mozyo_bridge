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


#: The callback-outcome detail lines, in the skill template's own shape.
#: The executable delivery command for THIS declaration — required flags and the anchor included.
#: R10-F1's most basic lesson: the previous fixture (`--to codex --target coordinator` only) was
#: itself a command the CLI refuses to run.
SEND_COMMAND = (
    "mozyo-bridge handoff send --to codex --source redmine --kind reply"
    " --issue 14219 --journal 85500 --target coordinator --mode standard"
)
CALLBACK_SENT_DETAIL = (
    "- target: coordinator (`--target coordinator`)\n"
    f"- on sent: {SEND_COMMAND}"
    " / observed landing marker"
    " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"
)
RETRY_COMMAND = (
    "mozyo-bridge handoff send --to codex --source redmine --kind reply"
    " --issue 14219 --journal 85500 --target %14 --target-repo auto"
)
CALLBACK_BLOCKED_DETAIL = (
    "- target: coordinator (`--target coordinator`)\n"
    "- on blocked: coordinator pane unresolved"
    " / candidates (`agents targets` rows): %14 codex w3F:p4"
    f" / retry command: {RETRY_COMMAND}\n"
)
CALLBACK_NOT_ATTEMPTED_DETAIL = (
    "- target: coordinator\n"
    "- on not-attempted: this lane IS the coordinator lane, no cross-lane hop applies\n"
)

#: The governed fixed-field park journal a park declaration must sit in — including the callback
#: outcome record the skill's template requires alongside the outcome token.
PARK_FIELDS = (
    "- state: blocked\n"
    "- durable_anchor: #14219 j#85500\n"
    "- callback_result: sent\n"
    "- blocked_by: 14150\n"
    "- resume_condition: #14150 の callback outcome journal 到達\n"
    "- resume_owner: coordinator\n"
) + CALLBACK_SENT_DETAIL


def _park_fields(*, result="sent", detail=None):
    """PARK_FIELDS with a different callback outcome and its record."""
    if detail is None:
        detail = CALLBACK_SENT_DETAIL
    base = PARK_FIELDS.replace(CALLBACK_SENT_DETAIL, "")
    return base.replace("- callback_result: sent", f"- callback_result: {result}") + detail


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

    def test_every_canonical_callback_outcome_record_is_accepted(self):
        # The skill's own template, in its own field names (checkpoint j#86548 R5-F2). R4 invented
        # spellings for these and so REJECTED the canonical records while accepting values that had
        # nothing to do with a callback — the check's meaning inverted.
        for value, detail in (
            ("sent", CALLBACK_SENT_DETAIL),
            ("blocked", CALLBACK_BLOCKED_DETAIL),
            ("not-attempted", CALLBACK_NOT_ATTEMPTED_DETAIL),
        ):
            with self.subTest(callback_result=value):
                journals = _park_journals(
                    park=_park_note(park_fields=_park_fields(result=value, detail=detail))
                )
                produced = _produce(journals, basis=BASIS_DEPENDENCY_PARK)
                self.assertEqual(produced.gaps, ())
                self.assertTrue(_by_key(produced)[CONJUNCT_PARK_DECLARED].satisfied)

    def test_an_identical_duplicate_landing_marker_collapses(self):
        # The marker/governed-field rule is collapse-identical / conflict-differing. R9 refused any
        # repeat outright, which is safe but not the contract: a record stating one fact twice
        # would never satisfy the basis (checkpoint j#86577 R9-F3). The differing case stays a gap
        # (see `test_an_outcome_without_its_record_is_a_gap`).
        marker = ("[mozyo:handoff:source=redmine:issue=14219:journal=85500"
                  ":kind=reply:to=codex]")
        fields = _park_fields(
            result="sent",
            detail=(
                "- target: coordinator\n"
                "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                f" / observed landing marker {marker} {marker}\n"
            ),
        )
        produced = _produce(
            _park_journals(park=_park_note(park_fields=fields)), basis=BASIS_DEPENDENCY_PARK
        )
        self.assertEqual(produced.gaps, ())

    def test_the_observation_part_may_restate_the_command(self):
        # The command component is cut at the separator, so text AFTER it belongs to the
        # observation and is not parsed as part of the invocation. Operators do quote the command
        # next to the marker; without the cut, that second mention reads as a second invocation.
        marker = ("[mozyo:handoff:source=redmine:issue=14219:journal=85500"
                  ":kind=reply:to=codex]")
        fields = _park_fields(
            result="sent",
            detail=(
                "- target: coordinator\n"
                "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                f" / observed landing marker {marker}"
                " (delivered via mozyo-bridge handoff send)\n"
            ),
        )
        produced = _produce(
            _park_journals(park=_park_note(park_fields=fields)), basis=BASIS_DEPENDENCY_PARK
        )
        self.assertEqual(produced.gaps, ())

    def test_a_quoted_summary_is_the_executed_command(self):
        # checkpoint j#86645 R11-F4: `--summary 'park callback delivered'` is ONE value after
        # shell lexing, and a naive split broke it into three tokens — so a correctly-recorded
        # executed command stopped satisfying the basis. The second case puts the separator
        # character inside the quotes, which the quote-aware boundary must not split on.
        for summary in ("'park callback delivered'", "'delivered / observed'"):
            with self.subTest(summary=summary):
                detail = (
                    "- target: coordinator\n"
                    f"- on sent: {SEND_COMMAND} --summary {summary}"
                    " / observed landing marker"
                    " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"
                )
                produced = _produce(
                    _park_journals(park=_park_note(park_fields=_park_fields(result="sent", detail=detail))),
                    basis=BASIS_DEPENDENCY_PARK,
                )
                self.assertEqual(produced.gaps, ())

    def test_a_custom_kind_with_its_summary_is_executable(self):
        # Positive control for the post-parse rule: custom + summary IS a delivery the CLI runs.
        detail = (
            "- target: coordinator\n"
            "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind custom"
            " --issue 14219 --journal 85500 --target coordinator --summary 'park callback'"
            " / observed landing marker"
            " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=custom:to=codex]\n"
        )
        produced = _produce(
            _park_journals(park=_park_note(park_fields=_park_fields(result="sent", detail=detail))),
            basis=BASIS_DEPENDENCY_PARK,
        )
        self.assertEqual(produced.gaps, ())

    def test_a_separator_valued_summary_is_not_a_boundary(self):
        # checkpoint j#86653 R13-F3: `--summary \\/` and `--summary '/'` both deliver the VALUE
        # `/`; only a bare `/` is template structure. Value-only tokens made the three
        # indistinguishable and refused correctly-recorded executed commands.
        for summary in ("\\/", "'/'"):
            with self.subTest(summary=summary):
                detail = (
                    "- target: coordinator\n"
                    f"- on sent: {SEND_COMMAND} --summary {summary}"
                    " / observed landing marker"
                    " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"
                )
                produced = _produce(
                    _park_journals(park=_park_note(park_fields=_park_fields(result="sent", detail=detail))),
                    basis=BASIS_DEPENDENCY_PARK,
                )
                self.assertEqual(produced.gaps, ())

    def test_a_separator_valued_summary_in_the_retry_is_not_a_boundary(self):
        fields = _park_fields(
            result="blocked",
            detail=(
                "- target: coordinator (`--target coordinator`)\n"
                "- on blocked: coordinator pane unresolved"
                " / candidates (`agents targets` rows): %14 codex w3F:p4"
                f" / retry command: {RETRY_COMMAND} --summary \\/\n"
            ),
        )
        produced = _produce(
            _park_journals(park=_park_note(park_fields=fields)), basis=BASIS_DEPENDENCY_PARK
        )
        self.assertEqual(produced.gaps, ())

    def test_an_empty_part_is_not_normalised_away(self):
        # checkpoint j#86653 R13-F4: dropping empty parts let a FOUR-part record pass as three.
        # The separator count and per-part non-emptiness are judged before any normalisation.
        for detail in (
            # double separator before the candidates
            ("- target: coordinator\n- on blocked: pane unresolved"
             " / / candidates (`agents targets` rows): %14"
             f" / retry command: {RETRY_COMMAND}\n"),
            # double separator before the retry
            ("- target: coordinator\n- on blocked: pane unresolved"
             " / candidates (`agents targets` rows): %14"
             f" / / retry command: {RETRY_COMMAND}\n"),
            # trailing separator
            ("- target: coordinator\n- on blocked: pane unresolved"
             " / candidates (`agents targets` rows): %14"
             f" / retry command: {RETRY_COMMAND} /\n"),
        ):
            with self.subTest(detail=detail[:60]):
                journals = _park_journals(
                    park=_park_note(park_fields=_park_fields(result="blocked", detail=detail))
                )
                self.assertEqual(
                    _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(
                        CONJUNCT_PARK_DECLARED
                    ),
                    bp.GAP_PARK_CALLBACK_DETAIL_ABSENT,
                )

    def test_a_mid_word_hash_is_literal_as_in_the_shell(self):
        # checkpoint j#86662 R15-F2: `park#callback` is ONE argv value to /bin/sh. Following
        # Python shlex's mid-word truncation rejected this command the shell actually delivers.
        detail = (
            "- target: coordinator\n"
            f"- on sent: {SEND_COMMAND} --summary park#callback"
            " / observed landing marker"
            " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"
        )
        produced = _produce(
            _park_journals(park=_park_note(park_fields=_park_fields(result="sent", detail=detail))),
            basis=BASIS_DEPENDENCY_PARK,
        )
        self.assertEqual(produced.gaps, ())

    def test_an_escaped_or_quoted_hash_is_a_value_not_a_comment(self):
        # Positive counterpart of the comment rule: `\\#` and `'#'` deliver the VALUE `#`.
        for summary in ("\\#", "'#'"):
            with self.subTest(summary=summary):
                detail = (
                    "- target: coordinator\n"
                    f"- on sent: {SEND_COMMAND} --summary {summary}"
                    " / observed landing marker"
                    " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"
                )
                produced = _produce(
                    _park_journals(park=_park_note(park_fields=_park_fields(result="sent", detail=detail))),
                    basis=BASIS_DEPENDENCY_PARK,
                )
                self.assertEqual(produced.gaps, ())

    def test_a_quoted_or_escaped_operator_valued_summary_is_content(self):
        # checkpoint j#86667 R16-F1: `--summary ';'` / `'|'` / `'&&'` / `'('` / `\\;` all deliver
        # ONE literal argv value to /bin/sh — control characters are operators only when their
        # provenance is bare. Judging plain values refused these commands the shell actually runs.
        for summary in ("';'", "'|'", "'&&'", "'('", "\\;"):
            with self.subTest(summary=summary):
                detail = (
                    "- target: coordinator\n"
                    f"- on sent: {SEND_COMMAND} --summary {summary}"
                    " / observed landing marker"
                    " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"
                )
                produced = _produce(
                    _park_journals(park=_park_note(park_fields=_park_fields(result="sent", detail=detail))),
                    basis=BASIS_DEPENDENCY_PARK,
                )
                self.assertEqual(produced.gaps, ())

    def test_a_quoted_or_escaped_operator_in_the_retry_is_content(self):
        # The blocked retry runs through the SAME single safety judgment: stripping the
        # provenance from the parts refused a replayable retry too (checkpoint j#86667 R16-F1).
        for summary in ("';'", "\\;"):
            with self.subTest(summary=summary):
                fields = _park_fields(
                    result="blocked",
                    detail=(
                        "- target: coordinator (`--target coordinator`)\n"
                        "- on blocked: coordinator pane unresolved"
                        " / candidates (`agents targets` rows): %14 codex w3F:p4"
                        f" / retry command: {RETRY_COMMAND} --summary {summary}\n"
                    ),
                )
                produced = _produce(
                    _park_journals(park=_park_note(park_fields=fields)),
                    basis=BASIS_DEPENDENCY_PARK,
                )
                self.assertEqual(produced.gaps, ())

    def test_posix_escapes_survive_the_boundary(self):
        # checkpoint j#86649 R12-F2: `--summary park\/callback` is ONE argv value (`park/callback`)
        # to the shell; the home-grown boundary FSM split it at the escaped slash. The second case
        # escapes a literal double quote before a separator inside the summary. Both are commands
        # the CLI runs, and both must satisfy the basis when correctly recorded.
        for summary in ("park\\/callback", '"park \\" / delivered"'):
            with self.subTest(summary=summary):
                detail = (
                    "- target: coordinator\n"
                    f"- on sent: {SEND_COMMAND} --summary {summary}"
                    " / observed landing marker"
                    " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"
                )
                produced = _produce(
                    _park_journals(park=_park_note(park_fields=_park_fields(result="sent", detail=detail))),
                    basis=BASIS_DEPENDENCY_PARK,
                )
                self.assertEqual(produced.gaps, ())

    def test_project_scope_with_its_repo_gate_is_still_a_delivery(self):
        # Negative control for the shared semantics: the project/repo rule refuses the LAYERING
        # violation, not the layered use.
        detail = (
            "- target: coordinator\n"
            f"- on sent: {SEND_COMMAND} --target-repo auto --target-project proj-a"
            " / observed landing marker"
            " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"
        )
        produced = _produce(
            _park_journals(park=_park_note(park_fields=_park_fields(result="sent", detail=detail))),
            basis=BASIS_DEPENDENCY_PARK,
        )
        self.assertEqual(produced.gaps, ())

    def test_a_quoted_separator_in_the_retry_survives_the_three_part_split(self):
        # The blocked boundary is a BARE `/` token too: a quoted separator inside the retry's own
        # summary is content, and a naive text split would cut the record into four parts.
        fields = _park_fields(
            result="blocked",
            detail=(
                "- target: coordinator (`--target coordinator`)\n"
                "- on blocked: coordinator pane unresolved"
                " / candidates (`agents targets` rows): %14 codex w3F:p4"
                f" / retry command: {RETRY_COMMAND} --summary 'retry / replay'\n"
            ),
        )
        produced = _produce(
            _park_journals(park=_park_note(park_fields=fields)), basis=BASIS_DEPENDENCY_PARK
        )
        self.assertEqual(produced.gaps, ())

    def test_the_equals_spelling_is_the_same_invocation(self):
        # checkpoint j#86626 R10-F3: canonical argparse accepts `--flag=value`, and the contract
        # is the flag's effective value, not its spelling.
        detail = (
            "- target: coordinator\n"
            "- on sent: mozyo-bridge handoff send --to=codex --source=redmine --kind=reply"
            " --issue=14219 --journal=85500 --target=coordinator"
            " / observed landing marker"
            " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"
        )
        produced = _produce(
            _park_journals(park=_park_note(park_fields=_park_fields(result="sent", detail=detail))),
            basis=BASIS_DEPENDENCY_PARK,
        )
        self.assertEqual(produced.gaps, ())

    def test_the_templates_coordinator_pane_fallback_is_accepted(self):
        # The template's second target form: `<coordinator_codex_%pane>`. It is a pane, and the
        # record says whose — which is what distinguishes it from any other pane on the cockpit.
        fields = _park_fields(
            result="sent",
            detail=(
                "- target: coordinator codex pane %14\n"
                "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply"
                " --issue 14219 --journal 85500 --target %14"
                " --target-repo auto / observed landing marker"
                " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"
            ),
        )
        produced = _produce(
            _park_journals(park=_park_note(park_fields=fields)), basis=BASIS_DEPENDENCY_PARK
        )
        self.assertEqual(produced.gaps, ())

    def test_the_template_spelling_result_is_accepted_too(self):
        # The callback template writes `result:`; the parked-state fixed-field block folds it in as
        # `callback_result:`. Both are the contract's own spellings, so both are read.
        fields = _park_fields().replace("- callback_result: sent", "- result: sent")
        produced = _produce(
            _park_journals(park=_park_note(park_fields=fields)), basis=BASIS_DEPENDENCY_PARK
        )
        self.assertEqual(produced.gaps, ())

    def test_an_outcome_without_its_record_is_a_gap(self):
        # Every outcome is a RECORD, not a token — `sent` included (R5-F1). A `sent` with no
        # target, no command and no observed marker is the same silence the field exists to forbid.
        for value, detail in (
            ("sent", ""),
            ("blocked", ""),
            ("not-attempted", ""),
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator\n"),
            ("sent", "- target: coordinator\n- on sent: [mozyo:handoff:issue=14219:kind=reply]\n"),
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / retry command: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target-repo auto\n"),
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates: %14 / retry command: nope\n"),
            ("blocked", "- target: coordinator\n- on blocked: %14"
                        " mozyo-bridge handoff send --target %14 --target-repo auto\n"),
            ("sent", "- on sent: mozyo-bridge handoff send / [mozyo:handoff:kind=reply]\n"),
            # `not-attempted` with its target but NO `on not-attempted` field: the outcome field
            # itself is the explicit reason the contract asks for, so its absence is the gap.
            # (Without this case the detail-field requirement is only load-bearing for the other
            # two outcomes, which the mutation probe caught.)
            ("not-attempted", "- target: coordinator\n"),
            # The R4 alias spelling supplying the reason for `not-attempted`: not the contract's
            # field, so it must not stand in for it.
            ("not-attempted", "- target: coordinator\n- reason: this lane is the coordinator\n"),
            # Three parts, but the candidate rows are missing and the retry targets a NAME rather
            # than a pane — so no part carries an `agents targets` row.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates: none found"
                        " / retry command: mozyo-bridge handoff send --target coordinator"
                        " --target-repo auto\n"),
            # The reason slot filled with evidence rather than a reason.
            ("blocked", "- target: coordinator\n- on blocked: %14 unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --target %14"
                        " --target-repo auto\n"),
            # Reason plus evidence, but the evidence is not separated into candidates AND retry —
            # two parts, so the record does not carry the three the template requires.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates %14 and retry mozyo-bridge handoff send --target %14"
                        " --target-repo auto\n"),
            # -- checkpoint j#86558 R6-F1: the record must be about THIS callback ---------------
            # A same-lane note to the lane's own Claude, carrying another issue's marker. Every
            # string the previous version looked for is present; none of it is this callback.
            ("sent", "- target: same-lane worker w3F:p3\n"
                     "- on sent: mozyo-bridge handoff send --to claude --target w3F:p3"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=99999:journal=1:kind=reply:to=claude]\n"),
            # Prose mentioning the command, and a bracketed token that carries no fields.
            ("sent", "- target: coordinator\n"
                     "- on sent: we did a handoff send at some point / [mozyo:handoff:x]\n"),
            # Delivered to the coordinator, but the marker names a different journal — so it is
            # not the landing observation for THIS park declaration.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=1:kind=reply:to=codex]\n"),
            # The command targets somewhere other than the target the record declares.
            ("sent", "- target: %14\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # -- checkpoint j#86558 R6-F2: each part carries its own authority -----------------
            # The candidate part names no pane; the pane inside the retry command used to stand
            # in for it.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates: none found"
                        " / retry command: mozyo-bridge handoff send --to codex --target %14"
                        " --target-repo auto\n"),
            # Candidates named, but the retry pins a natural name rather than one of them.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14 codex w3F:p4"
                        " / retry command: mozyo-bridge handoff send --to codex"
                        " --target coordinator --target-repo auto\n"),
            # `--target` immediately followed by the next flag: the flag is not the target.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to codex --target"
                        " --target-repo auto\n"),
            # Each of the next four isolates ONE rule: everything else about the record is
            # correct, so only the named rule can be what refuses it (the mutation probe caught
            # that the earlier cases each broke several rules at once and so proved none of them).
            # Delivered to the lane's own Claude rather than the coordinator's Codex.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to claude --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # The marker observed belongs to another issue.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=99999:journal=85500:kind=reply:to=codex]\n"),
            # The marker observed was addressed to a Claude, so it is not the coordinator callback.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=claude]\n"),
            # A target that is neither the natural coordinator target nor a resolved pane.
            ("sent", "- target: mainlane\n"
                     "- on sent: mozyo-bridge handoff send --to codex --target mainlane"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # -- checkpoint j#86562 R7-F1: the target rule is COMMON to all three outcomes -----
            # The rule was written in the contract for every outcome but wired inside the `sent`
            # branch, so these two named any target they liked.
            ("blocked", "- target: same-lane worker w3F:p3\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to codex --target %14"
                        " --target-repo auto\n"),
            ("not-attempted", "- target: same-lane worker w3F:p3\n"
                              "- on not-attempted: this lane IS the coordinator lane\n"),
            # `noncoordinator` contains `coordinator`; a substring test read it as the coordinator.
            ("sent", "- target: noncoordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --target noncoordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # -- checkpoint j#86562 R7-F2: the command is ONE invocation ------------------------
            # The effective receiver is claude; reading only the first `--to` said codex.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --to claude"
                     " --target coordinator / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # The effective target is the pane; reading only the first `--target` said coordinator.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " --target w3F:p3 / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # A marker missing the fields the canonical producer always emits (`source` / `kind`)
            # was never built by the sender, so it is not the landing observation.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / [mozyo:handoff:issue=14219:journal=85500:to=codex]\n"),
            # The retry is a command that will be REPLAYED — `echo` will not deliver anything.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: echo --target %14 --target-repo auto\n"),
            # A retry that pins two targets has no single meaning to replay.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to codex --target %14"
                        " --target %99 --target-repo auto\n"),
            # A retry that does not name the coordinator's Codex is not the callback's retry.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to claude --target %14"
                        " --target-repo auto\n"),
            # A retry command with no `--target-repo auto` is not replayable as specified.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target %14\n"),
            # A target field naming two panes: no single target to have delivered to.
            ("sent", "- target: coordinator pane %99 %14\n"
                     "- on sent: mozyo-bridge handoff send --to codex --target %14"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # One `--target`, but not the one the record declares.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --target %14"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # A retry naming the right receiver and flags, but not a delivery command.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: echo --to codex --target %14 --target-repo auto\n"),
            # -- checkpoint j#86569 R8-F1: the command is ONE INVOCATION, boundaries included ---
            # A wrapped command is precisely the one that did not run. The `handoff send` string
            # is present in all three — which is why the earlier check passed them.
            ("sent", "- target: coordinator\n"
                     "- on sent: echo mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            ("sent", "- target: coordinator\n"
                     "- on sent: false && mozyo-bridge handoff send --to codex"
                     " --target coordinator / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: echo mozyo-bridge handoff send --to codex --target %14"
                        " --target-repo auto\n"),
            # -- checkpoint j#86569 R8-F2: the landing marker is canonical and unambiguous -------
            # A key declared twice says two things and proves neither; the shared scanner's
            # last-write-wins quietly resolved both of these to the acceptable value.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / [mozyo:handoff:source=redmine:issue=14219:journal=1:journal=85500"
                     ":kind=reply:to=codex]\n"),
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply"
                     ":to=claude:to=codex]\n"),
            # A foreign marker riding along beside a valid one: two observations, no single one.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / [mozyo:handoff:source=redmine:issue=99999:journal=1:kind=reply:to=claude]"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # Combinations the canonical producer cannot emit: another source system carrying this
            # Redmine anchor, and a kind outside the handoff vocabulary.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / [mozyo:handoff:source=asana:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=banana"
                     ":to=codex]\n"),
            # Each of the next five isolates ONE invocation/marker rule: everything else about the
            # record is canonical, so only the named rule can refuse it.
            # A shell operator with no prefix word: the invocation is composed, not run alone.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " && true / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # A prefix that ends in a colon but is not a label.
            ("sent", "- target: coordinator\n"
                     "- on sent: sudo -u root cmd: mozyo-bridge handoff send --to codex"
                     " --target coordinator / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # The entry point is there, and so are the words — but not as `handoff send`.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge workflow callbacks handoff send --to codex"
                     " --target coordinator / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # Two invocations in one line: which one is the delivery?
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " mozyo-bridge handoff send --mode standard / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # The valid marker FIRST and a foreign one after it — "any one matches" would stop at
            # the first and never see the second.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]"
                     " [mozyo:handoff:source=redmine:issue=99999:journal=1:kind=reply:to=claude]\n"),
            # -- checkpoint j#86577 R9-F1: a wrapper is not a label ---------------------------
            # `echo command:` reads as a label to a pattern that only wants "words then a colon",
            # so the command that ran was `echo` — everything else here is canonical.
            ("sent", "- target: coordinator\n"
                     "- on sent: echo command: mozyo-bridge handoff send --to codex"
                     " --target coordinator / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / echo retry command: mozyo-bridge handoff send --to codex --target %14"
                        " --target-repo auto\n"),
            # -- checkpoint j#86577 R9-F2: the canonical vocabularies are lowercase literals ----
            # The producer cannot emit these, and the CLI refuses `--to CODEX` outright, so
            # case-folding the comparison accepted tokens no run could have produced.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / [mozyo:handoff:source=REDMINE:issue=14219:journal=85500:kind=reply"
                     ":to=codex]\n"),
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine --kind reply --issue 14219 --journal 85500 --target coordinator"
                     " / [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply"
                     ":to=CODEX]\n"),
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to CODEX --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # -- checkpoint j#86626 R10-F1: the command must be one the CLI would actually run --
            # The exact previous positive fixture: required `--source` / `--kind` are missing, so
            # the canonical argparse refuses it — a command that cannot run delivered nothing.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # Fully-flagged, plus one token the CLI would reject as unrecognized.
            ("sent", "- target: coordinator\n"
                     f"- on sent: {SEND_COMMAND} definitely-not-a-cli-arg"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # The retry side of the same rule.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to codex --target %14"
                        " --target-repo auto\n"),
            # -- checkpoint j#86626 R10-F2: the command must compose THIS marker ----------------
            # Executable commands whose canonical build_marker output is a DIFFERENT marker than
            # the one observed: another anchor, another kind, another source system.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine"
                     " --kind reply --issue 99999 --journal 1 --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine"
                     " --kind review_request --issue 14219 --journal 85500 --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source asana"
                     " --task-id 123 --comment-id 456 --kind reply --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # Passes argparse (both flags optional) but fails the canonical anchor semantics:
            # a redmine anchor requires BOTH --issue and --journal. The retry path is the
            # discriminating one — the sent path re-derives the anchor for the marker comparison,
            # so only here is `normalize_anchor` the sole refusing rule.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to codex --source redmine"
                        " --kind reply --issue 14219 --target %14 --target-repo auto\n"),
            # A conflicting duplicate ACROSS spellings whose argparse-effective (last) value is the
            # acceptable one: only the spelling-aware conflict scan can refuse it.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine"
                     " --kind reply --issue 14219 --journal 85500 --target w3F:p3"
                     " --target=coordinator / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # -- checkpoint j#86645 R11-F1: the retry replays THIS callback ---------------------
            # Executable, codex, candidate-pinned, replayable — and anchored at another ticket.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to codex --source redmine"
                        " --kind reply --issue 99999 --journal 85500 --target %14"
                        " --target-repo auto\n"),
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to codex --source redmine"
                        " --kind reply --issue 14219 --journal 1 --target %14"
                        " --target-repo auto\n"),
            # -- checkpoint j#86645 R11-F2: argparse acceptance is not a sent delivery ----------
            # `--mode pending` places the body without pressing Enter: the rail reports
            # pending_input, so nothing was sent.
            ("sent", "- target: coordinator\n"
                     f"- on sent: {SEND_COMMAND.replace('--mode standard', '--mode pending')}"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # `--kind custom` without `--summary` is refused by build_notification_body before
            # anything is sent — argparse alone cannot see it.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine"
                     " --kind custom --issue 14219 --journal 85500 --target coordinator"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=custom:to=codex]\n"),
            # A pending-mode retry would place the body without submitting on replay.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to codex --source redmine"
                        " --kind reply --issue 14219 --journal 85500 --target %14"
                        " --target-repo auto --mode pending\n"),
            # -- checkpoint j#86645 R11-F3: abbreviation would bypass the conflict rule ---------
            # `--ki` abbreviates `--kind` under argparse's default; two names, one logical option,
            # two values. The evidence grammar refuses abbreviation outright.
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine"
                     " --ki review_request --kind reply --issue 14219 --journal 85500"
                     " --target coordinator / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            ("sent", "- target: coordinator\n"
                     "- on sent: mozyo-bridge handoff send --to codex --source redmine"
                     " --kind reply --issu 99999 --issue 14219 --journal 85500"
                     " --target coordinator / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # -- checkpoint j#86645 R11-F4: an unclosed quote is not a lexable command ----------
            ("sent", "- target: coordinator\n"
                     f"- on sent: {SEND_COMMAND} --summary 'unclosed"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # A control operator in value position: the shell would have ended the command at
            # `;`, so `--summary ;` never carried a value — only the punctuation-token check can
            # refuse it (argparse itself would happily take ";" as the summary).
            ("sent", "- target: coordinator\n"
                     f"- on sent: {SEND_COMMAND} --summary ;"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # -- checkpoint j#86649 R12-F1: the shared send-semantics authority ----------------
            # `--select` is mutually exclusive with an explicit `--target`: the canonical
            # apply_handoff_selection dies before sending, so nothing was delivered.
            ("sent", "- target: coordinator\n"
                     f"- on sent: {SEND_COMMAND} --select"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # `--target-project` without `--target-repo`: the canonical admission pipeline refuses
            # invalid_args / zero-send.
            ("sent", "- target: coordinator\n"
                     f"- on sent: {SEND_COMMAND} --target-project proj-a"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # -- checkpoint j#86657 R14-F1: an unquoted `#` comments out the rest ---------------
            # The shell delivers `--summary` with NO value (parse refusal, zero send); reading the
            # `#` as a token made the record look like a delivery.
            ("sent", "- target: coordinator\n"
                     f"- on sent: {SEND_COMMAND} --summary #"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            # -- checkpoint j#86662 R15-F1: expansion rewrites argv before the CLI runs ---------
            # An unset variable deletes the summary value (unquoted) or empties it (double-quoted)
            # -> parse refusal / custom-summary refusal; a glob fans out into unknown arguments.
            # All three are zero-send; the record's literal text is not the argv that ran.
            ("sent", "- target: coordinator\n"
                     f"- on sent: {SEND_COMMAND} --summary $MOZYO_UNSET_VAR"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            ("sent", "- target: coordinator\n"
                     f'- on sent: {SEND_COMMAND} --summary "$MOZYO_UNSET_VAR"'
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            ("sent", "- target: coordinator\n"
                     f"- on sent: {SEND_COMMAND} --summary *"
                     " / observed landing marker"
                     " [mozyo:handoff:source=redmine:issue=14219:journal=85500:kind=reply:to=codex]\n"),
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        f" / retry command: {RETRY_COMMAND} --summary $MOZYO_UNSET_VAR\n"),
            # A BARE operator in the retry's value position (checkpoint j#86667 R16-F1's negative
            # counterpart): the shell ends the retry at `|`, so the replay never carried a value.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        f" / retry command: {RETRY_COMMAND} --summary |\n"),
            # A retry pinned at a pane that was never a candidate.
            ("blocked", "- target: coordinator\n- on blocked: pane unresolved"
                        " / candidates (`agents targets` rows): %14"
                        " / retry command: mozyo-bridge handoff send --to codex --target %99"
                        " --target-repo auto\n"),
        ):
            with self.subTest(callback_result=value, detail=detail[:40]):
                journals = _park_journals(
                    park=_park_note(park_fields=_park_fields(result=value, detail=detail))
                )
                self.assertEqual(
                    _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(
                        CONJUNCT_PARK_DECLARED
                    ),
                    bp.GAP_PARK_CALLBACK_DETAIL_ABSENT,
                )

    def test_the_r4_invented_aliases_are_no_longer_evidence(self):
        # The exact shapes R4 accepted: field names absent from the contract, carrying values with
        # nothing to do with a callback. They are not the record; they never were.
        for detail in (
            "- callback_reason: x\n- retry_command: nope\n",
            "- reason: dependency waiting\n- retry: later\n",
        ):
            with self.subTest(detail=detail.strip()):
                journals = _park_journals(
                    park=_park_note(park_fields=_park_fields(result="blocked", detail=detail))
                )
                self.assertEqual(
                    _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(
                        CONJUNCT_PARK_DECLARED
                    ),
                    bp.GAP_PARK_CALLBACK_DETAIL_ABSENT,
                )

    def test_an_anchor_naming_another_journal_of_this_issue_is_a_gap(self):
        # Checkpoint j#86525 R4-F1: the shape and the issue were checked, but not WHICH journal.
        # An older callback journal of the same issue is not this park declaration's own anchor.
        fields = PARK_FIELDS.replace("- durable_anchor: #14219 j#85500", "- durable_anchor: #14219 j#1")
        journals = _park_journals(park=_park_note(park_fields=fields))
        self.assertEqual(
            _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(CONJUNCT_PARK_DECLARED),
            bp.GAP_PARK_ANCHOR_NOT_THIS_DECLARATION,
        )

    def test_conflicting_duplicate_governed_fields_are_a_gap(self):
        # Checkpoint j#86525 R4-F3: first-write-wins had no order authority behind it. Every other
        # layer of this surface folds duplicates as collapse-or-conflict; the governed fields now
        # do too.
        for extra in (
            "- callback_result: invented\n",
            "- durable_anchor: #14219 j#1\n",
            "- resume_owner: worker\n",
        ):
            with self.subTest(duplicate=extra.strip()):
                journals = _park_journals(park=_park_note(park_fields=PARK_FIELDS + extra))
                self.assertEqual(
                    _gaps(_produce(journals, basis=BASIS_DEPENDENCY_PARK)).get(
                        CONJUNCT_PARK_DECLARED
                    ),
                    bp.GAP_PARK_JOURNAL_FIELDS_INVALID,
                )

    def test_an_identical_duplicate_field_collapses(self):
        # Negative control for the conflict rule: a re-stated identical field is not a conflict,
        # so the rule refuses contradiction rather than repetition.
        journals = _park_journals(
            park=_park_note(park_fields=PARK_FIELDS + "- callback_result: sent\n")
        )
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


class SendSemanticsSharedAuthorityTests(unittest.TestCase):
    """The canonical call sites and the evidence parser consult the SAME semantic authority.

    R11 re-enumerated individual send-time conditions in the evidence parser and drifted from the
    application semantics (j#86649 R12-F1). The rules now live in
    ``handoff_send_semantics.send_semantic_gap`` and these tests pin that the canonical consumers
    actually call it — so a rule added there reaches every consumer, including this evidence
    surface, at once.
    """

    def test_apply_handoff_selection_consults_the_authority(self):
        import inspect

        from mozyo_bridge.application.commands_target_select import apply_handoff_selection

        self.assertIn("send_semantic_gap", inspect.getsource(apply_handoff_selection))

    def test_admission_pipeline_consults_the_authority(self):
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import (
            handoff_admission_pipeline,
        )

        self.assertIn("send_semantic_gap", inspect.getsource(handoff_admission_pipeline))

    def test_build_notification_body_consults_the_authority(self):
        # Source-level wiring guard (checkpoint j#86653 R13-F1): behavioural equivalence alone
        # does not make one of two implementations THE authority. The line budget for this wiring
        # was funded by extracting default_body_for_kind out of the allowlisted module rather
        # than bumping its baseline.
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            build_notification_body,
        )

        self.assertIn("send_semantic_gap", inspect.getsource(build_notification_body))

    def test_build_notification_body_matches_the_authority(self):
        # Behavioural matrix kept alongside the wiring guard: the builder raises EXACTLY when the
        # authority reports the missing custom summary.
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            AnchorError,
            build_notification_body,
            normalize_anchor,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_send_semantics import (  # noqa: E501
            SEND_SEMANTIC_CUSTOM_SUMMARY,
            send_semantic_gap,
        )

        anchor = normalize_anchor("redmine", issue="14219", journal="85500")
        for kind, summary in (
            ("custom", None),
            ("custom", ""),
            ("custom", "a summary"),
            ("reply", None),
        ):
            with self.subTest(kind=kind, summary=summary):
                expected_gap = (
                    send_semantic_gap(kind=kind, summary=summary)
                    == SEND_SEMANTIC_CUSTOM_SUMMARY
                )
                try:
                    build_notification_body(
                        anchor=anchor, kind=kind, receiver="codex", summary=summary
                    )
                    raised = False
                except AnchorError:
                    raised = True
                self.assertEqual(raised, expected_gap)

    def test_the_evidence_parser_consults_the_authority(self):
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_park_record import (  # noqa: E501
            _send_invocation,
        )

        self.assertIn("send_semantic_gap", inspect.getsource(_send_invocation))


class SendGrammarDriftGuardTests(unittest.TestCase):
    """The park record's mirrored ``handoff send`` grammar matches the canonical parser.

    The domain must not import the application-layer parser builder, so the grammar lives as data
    (``_SEND_OPTIONS``) — and this test builds the REAL parser and asserts the mirror matches it
    action for action, the same drift-guard pattern ``callback_delivery`` uses. If this fails, the
    canonical CLI grammar changed and the mirror must be updated with it.
    """

    def test_mirrored_grammar_matches_the_canonical_parser(self):
        import argparse

        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff import (  # noqa: E501
            configure_handoff_parser,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff_select import (  # noqa: E501
            add_handoff_select_args,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_park_record import (  # noqa: E501
            _SEND_OPTIONS,
        )

        canonical = argparse.ArgumentParser(add_help=False)
        configure_handoff_parser(canonical, kind_required=True)
        add_handoff_select_args(canonical)

        def spec(action):
            if type(action).__name__ == "_StoreTrueAction":
                value = "flag"
            elif type(action).__name__ == "_AppendAction":
                value = "append"
            else:
                value = action.type or str
            choices = tuple(action.choices) if action.choices else None
            return (action.option_strings[0], bool(action.required), choices, value)

        canonical_specs = [spec(action) for action in canonical._actions]
        self.assertEqual(list(_SEND_OPTIONS), canonical_specs)


class ModuleDefinitionHygieneTests(unittest.TestCase):
    """No authority-bearing module carries duplicate top-level definitions.

    Checkpoint j#86653 R13-F2: slice-based edits twice left stale copies of parser definitions in
    ``hibernate_park_record``, and the LATER copy silently won at import time — so the checks that
    had just been written were effectively absent. A hand grep proved unreliable (it checked only
    the names just edited); this guard checks every top-level name mechanically.
    """

    _MODULES = (
        "hibernate_park_record",
        "hibernate_basis_producer",
        "hibernate_evidence_authority",
        "hibernate_evidence_marker",
        "hibernate_evidence_envelope",
        "hibernate_evidence_integration",
    )

    @staticmethod
    def _duplicate_top_level_names(source: str) -> list:
        """Every duplicated top-level name in ``source`` (defs, classes, plain AND annotated
        assignments to a Name; tuple-unpacking targets are out of scope and none exist here).

        Checkpoint j#86657 R14-F2: the first version collected ``ast.Assign`` only, and the very
        authority it existed to protect — ``_SEND_OPTIONS: tuple = ...`` — is an ``AnnAssign``,
        so an annotated duplicate sailed through the guard.
        """
        import ast

        seen: list = []
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                seen.append(node.name)
            elif isinstance(node, ast.Assign):
                seen.extend(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                seen.append(node.target.id)
        return sorted({item for item in seen if seen.count(item) > 1})

    def test_the_guard_detects_what_it_exists_to_detect(self):
        # The guard's own guard: synthetic duplicates of every node shape it claims to cover.
        # A guard whose detection power is never measured is the defect that created this test.
        cases = {
            "annotated": "X: tuple = ()\nX: tuple = (1,)",
            "plain": "Y = 1\nY = 2",
            "def": "def f():\n    pass\n\ndef f():\n    pass",
            "class": "class C:\n    pass\n\nclass C:\n    pass",
            "mixed": "Z = 1\nZ: int = 2",
        }
        for shape, source in cases.items():
            with self.subTest(shape=shape):
                self.assertNotEqual(self._duplicate_top_level_names(source), [])
        self.assertEqual(self._duplicate_top_level_names("A = 1\nB: int = 2\ndef c(): pass"), [])

    def test_no_duplicate_top_level_definitions(self):
        import importlib
        import inspect

        for name in self._MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(
                    "mozyo_bridge.e_110_execution_platform."
                    "f_140_delegated_coordinator_nested_handoff.domain." + name
                )
                self.assertEqual(
                    self._duplicate_top_level_names(inspect.getsource(module)), []
                )


class LexerDriftGuardTests(unittest.TestCase):
    """The provenance-preserving lexer's VALUES match ``shlex`` exactly.

    The lexer exists because ``shlex`` discards provenance (checkpoint j#86653 R13-F3); this guard
    is what keeps it from becoming a second, diverging value authority.
    """

    CORPUS = (
        "mozyo-bridge handoff send --to codex --summary 'park callback delivered'",
        "cmd --summary park\\/callback / observed",
        "cmd --summary '/' / observed",
        'a "b \\" / c" d',
        "plain / boundary + plus",
        "--flag=value --other 'x y'",
        "a;b | c && d",
        "  leading and   multiple   spaces ",
        "escaped\\ space and 'quoted part'",
    )

    #: Fragments whose pairwise combinations cover the quote / escape / punctuation / separator
    #: transitions (checkpoint j#86657 R14-F1: a curated corpus covers only what it curates, so
    #: this one is generated). Comment and expansion characters are NOT here: Python shlex is not
    #: the shell for either (checkpoint j#86662 R15-F2 — shlex comments mid-word where sh does
    #: not), so comment semantics are checked against /bin/sh itself below, and expansion-bearing
    #: tokens are policy-refused before lexing fidelity matters.
    FRAGMENTS = (
        "plain", "--flag=value", "'quoted part'", '"dq \\" part"', "\\ escaped",
        "/", "+", ";", "|", "(", "%14", "--summary",
        # Quoted / escaped operators deliver the operator CHARACTER as a value (checkpoint
        # j#86667 R16-F1) — the values still match shlex; only the provenance differs.
        "';'", "\\;",
    )

    @staticmethod
    def _reference(text: str):
        import shlex as shlex_module

        # ``commenters=""``: this oracle answers for TOKENIZATION only. Its default comment rule
        # is Python's, not POSIX's, and mirroring it rejected commands the shell delivers.
        lexer = shlex_module.shlex(text, posix=True, punctuation_chars=";|&<>()")
        lexer.commenters = ""
        lexer.whitespace_split = True
        try:
            return list(lexer)
        except ValueError:
            return None

    def test_values_match_shlex(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_park_record import (  # noqa: E501
            _lex_detail,
        )

        for text in self.CORPUS:
            with self.subTest(text=text):
                got = _lex_detail(text)
                self.assertEqual(
                    None if got is None else [token.value for token in got],
                    self._reference(text),
                )

    def test_values_match_shlex_over_the_fragment_cross_product(self):
        from itertools import product

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_park_record import (  # noqa: E501
            _lex_detail,
        )

        for left, right in product(self.FRAGMENTS, repeat=2):
            text = f"{left} {right}"
            with self.subTest(text=text):
                got = _lex_detail(text)
                self.assertEqual(
                    None if got is None else [token.value for token in got],
                    self._reference(text),
                    text,
                )

    def test_comment_boundaries_match_the_shell_itself(self):
        # The comment contract is POSIX sh's, not Python shlex's: `#` comments only at word start;
        # mid-word, escaped and quoted hashes are literal. The oracle here is /bin/sh's actual
        # argv, because following shlex's own default rejected commands the shell delivers
        # (checkpoint j#86662 R15-F2).
        import os
        import subprocess

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_park_record import (  # noqa: E501
            _lex_detail,
        )

        if not os.path.exists("/bin/sh"):  # pragma: no cover - POSIX hosts only
            self.skipTest("/bin/sh not available")

        cases = (
            "a # b",
            "a #b",
            "a park#callback",
            "a \\# b",
            "a '#' b",
            'a "#" b',
            "#lead",
        )
        for text in cases:
            with self.subTest(text=text):
                result = subprocess.run(
                    ["/bin/sh", "-c", f'set -- {text}\nfor a; do printf "%s\\0" "$a"; done'],
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0)
                out = result.stdout.decode()
                shell_argv = out.split("\0")[:-1] if out else []
                self.assertEqual(
                    [token.value for token in _lex_detail(text)], shell_argv
                )

    def test_unclosed_quote_and_trailing_escape_are_fail_closed(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_park_record import (  # noqa: E501
            _lex_detail,
        )

        self.assertIsNone(_lex_detail("a 'unclosed"))
        self.assertIsNone(_lex_detail('a "unclosed'))
        self.assertIsNone(_lex_detail("a trailing\\"))
        self.assertIsNone(_lex_detail("two\nlines"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
