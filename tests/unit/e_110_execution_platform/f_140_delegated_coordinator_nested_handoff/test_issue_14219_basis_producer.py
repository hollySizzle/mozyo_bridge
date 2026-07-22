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
    kwargs = dict(target_head=head, review_request_journal="85400", conclusion=conclusion)
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
        EVIDENCE_REQUIRED_CI_GREEN, envelope=overrides.pop("envelope", _env()), run="299", **overrides
    )


def _dogfood_note(**overrides):
    return "dogfood\n" + render_hibernate_evidence(
        EVIDENCE_DOGFOOD_DELEGATED,
        envelope=overrides.pop("envelope", _env()),
        release_issue="14184",
    )


def _park_note(**overrides):
    return "park\n" + render_hibernate_evidence(
        EVIDENCE_PARK_DECLARED, envelope=overrides.pop("envelope", _env(head=""))
    )


def _push(reachable=True, head=HEAD, lane=LANE, gen=GEN):
    return bp.PushObservation(
        workspace=WS, lane=lane, lane_generation=gen, head=head, reachable=reachable
    )


def _early_journals(**overrides):
    """The four durable evidence journals an early-hibernate lane rests on."""
    return [
        ("85400", overrides.get("review", _review_note())),
        ("85410", overrides.get("integration", _integration_note())),
        ("85420", overrides.get("ci", _ci_note())),
        ("85430", overrides.get("dogfood", _dogfood_note())),
    ]


def _produce(journals=None, *, basis=BASIS_EARLY_HIBERNATE, push=None):
    return bp.produce_basis_conjuncts(
        journals if journals is not None else _early_journals(),
        basis=basis,
        push=push if push is not None else _push(),
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
        produced = _produce([("85500", _park_note())], basis=BASIS_DEPENDENCY_PARK)
        self.assertEqual(produced.gaps, ())
        conjunct = _by_key(produced)[CONJUNCT_PARK_DECLARED]
        self.assertTrue(conjunct.satisfied)
        self.assertEqual(conjunct.bound_head, "")
        self.assertEqual(conjunct.bound_generation, GEN)

    def test_park_basis_does_not_require_the_early_conjuncts(self):
        produced = _produce([("85500", _park_note())], basis=BASIS_DEPENDENCY_PARK, push=_push())
        self.assertEqual({c.key for c in produced.conjuncts}, {CONJUNCT_PARK_DECLARED})


class LatestDeclarationWinsTests(unittest.TestCase):
    def test_newer_changes_requested_shadows_older_approval(self):
        journals = _early_journals()
        journals.append(("85440", _review_note(conclusion="changes_requested")))
        produced = _produce(journals)
        self.assertFalse(_by_key(produced)[CONJUNCT_REVIEW_APPROVED].satisfied)

    def test_older_approval_does_not_resurface_when_newer_is_legacy(self):
        # The newer marker carries no envelope: it supersedes by EXISTING, so the conjunct is a gap
        # rather than the stale enveloped approval.
        journals = _early_journals()
        journals.append(("85440", _review_note(enveloped=False)))
        produced = _produce(journals)
        self.assertNotIn(CONJUNCT_REVIEW_APPROVED, _by_key(produced))
        self.assertIn(CONJUNCT_REVIEW_APPROVED, _gaps(produced))

    def test_newer_deferral_shadows_older_merge(self):
        journals = _early_journals()
        journals.append(
            ("85450", "## Integration disposition: explicit_deferral\n\n- next_owner: coordinator\n")
        )
        produced = _produce(journals)
        self.assertNotIn(CONJUNCT_STAGING_INTEGRATED, _by_key(produced))
        self.assertIn(CONJUNCT_STAGING_INTEGRATED, _gaps(produced))

    def test_an_older_journal_never_overrides_a_newer_one(self):
        journals = list(reversed(_early_journals()))
        journals.append(("85300", _review_note(conclusion="changes_requested")))
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
        produced = _produce([("85400", _review_note())])
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
        journals.append(("85450", "## Integration disposition: explicit_deferral\n"))
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
            "lane_generation={g}:head={h}:run=299:conclusion=failure]"
        ).format(w=WS, l=LANE, g=GEN, h=HEAD)
        produced = _produce(_early_journals(ci=marker))
        self.assertEqual(_gaps(produced)[CONJUNCT_REQUIRED_CI_GREEN], "evidence_ci_not_success")


class ProducerNeverBindsToTheTargetTests(unittest.TestCase):
    """A drifted record must reach T1 with the identity IT declares, not the candidate's."""

    def test_cross_lane_review_is_transcribed_as_the_other_lane(self):
        produced = _produce(_early_journals(review=_review_note(lane="other-lane")))
        self.assertEqual(_by_key(produced)[CONJUNCT_REVIEW_APPROVED].bound_lane, "other-lane")

    def test_old_generation_review_is_transcribed_as_the_old_generation(self):
        produced = _produce(_early_journals(review=_review_note(gen=GEN - 1)))
        self.assertEqual(_by_key(produced)[CONJUNCT_REVIEW_APPROVED].bound_generation, GEN - 1)

    def test_drifted_head_is_transcribed_as_the_drifted_head(self):
        other = "c" * 40
        produced = _produce(_early_journals(review=_review_note(head=other)))
        self.assertEqual(_by_key(produced)[CONJUNCT_REVIEW_APPROVED].bound_head, other)


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
        got = self._classify(_produce([("85400", _review_note())]))
        self.assertIsInstance(got, HibernateNonCandidate)
        self.assertEqual(got.reason, NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN)

    def test_explicit_non_approval_is_unsatisfied_not_unknown(self):
        got = self._classify(_produce(_early_journals(review=_review_note(conclusion="changes_requested"))))
        self.assertEqual(got.reason, NON_CANDIDATE_BASIS_UNSATISFIED)

    def test_cross_lane_evidence_is_an_anchor_mismatch(self):
        got = self._classify(_produce(_early_journals(review=_review_note(lane="other-lane"))))
        self.assertEqual(got.reason, NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_old_generation_evidence_is_an_anchor_mismatch(self):
        got = self._classify(_produce(_early_journals(review=_review_note(gen=GEN - 1))))
        self.assertEqual(got.reason, NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_head_drifted_evidence_is_an_anchor_mismatch(self):
        got = self._classify(_produce(_early_journals(review=_review_note(head="c" * 40))))
        self.assertEqual(got.reason, NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
