"""Pure hibernate-candidate classifier tests (Redmine #14219, tranche T1; R1-F1/F2 corrected).

Pins the safety core of the auto-hibernate candidate model:

- **anchor binding is fail-closed AND exact** — ``None`` records → unreadable, no active record →
  absent, more than one active record → ambiguous, and the single active record must match the
  enumeration's selected lane on workspace / lane / generation / revision or it is a typed stale
  zero-actuation (R1-F1);
- **the head is never the lifecycle record** — a head must be bound from a non-lifecycle authority;
- **each conjunct is bound to the candidate's exact head / issue** — head-anchored evidence at a
  different head, and issue-anchored evidence for a different issue, are rejected, so a genuine
  proof of a *different* generation can never be synthesised into a candidate (R1-F2);
- **`releasable` is not a proxy** — each basis conjunct must carry its OWN durable authority; there
  is no drain-queue provenance token, and a required conjunct that is missing / wrong-authority /
  off-anchor / false each folds to a distinct typed reason with never an implicit fallback.

All pure; no store, no I/O. The read-only lifecycle binding end-to-end is exercised in
``tests/regressions/test_issue_14219_hibernate_candidate_binding.py``.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from mozyo_bridge.core.state.lane_lifecycle_model import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    hibernate_candidate as hc,
)

ISSUE = "14219"
WS = "ws-1"
LANE = "lane-abc"
GEN = 3
REV = 7
HEAD = "a" * 40


@dataclass(frozen=True)
class _Rec:
    """A minimal stand-in for the fields ``bind_lifecycle_anchor`` reads off a lifecycle record."""

    issue_id: str = ISSUE
    repo_workspace_id: str = WS
    lane_id: str = LANE
    lane_generation: int = GEN
    revision: int = REV
    lane_disposition: str = DISPOSITION_ACTIVE


def _selected(**over) -> hc.SelectedLane:
    base = dict(
        issue_id=ISSUE, repo_workspace_id=WS, lane_id=LANE, lane_generation=GEN, revision=REV
    )
    base.update(over)
    return hc.SelectedLane(**base)


def _head(value: str = HEAD, provenance: str = hc.PROVENANCE_GIT_REMOTE) -> hc.BoundField:
    return hc.BoundField(value=value, provenance=provenance)


# A correctly-sourced, on-anchor, satisfied conjunct: bound to the candidate lane identity, and to
# the candidate head when the conjunct is head-bearing. Any of the bound_* kwargs can be overridden
# to drive a negative case.
def _conj(key, provenance, *, satisfied=True, bound_workspace=WS, bound_lane=LANE,
          bound_generation=GEN, bound_head=None):
    if bound_head is None:
        bound_head = HEAD if key in hc._CONJUNCT_REQUIRES_HEAD else ""
    return hc.BasisConjunct(
        key=key, satisfied=satisfied, provenance=provenance,
        bound_workspace=bound_workspace, bound_lane=bound_lane,
        bound_generation=bound_generation, bound_head=bound_head,
    )


def _early_conjuncts(**overrides) -> tuple[hc.BasisConjunct, ...]:
    spec = {
        hc.CONJUNCT_REVIEW_APPROVED: hc.PROVENANCE_REVIEW_RECORD,
        hc.CONJUNCT_STAGING_INTEGRATED: hc.PROVENANCE_INTEGRATION_RECORD,
        hc.CONJUNCT_REQUIRED_CI_GREEN: hc.PROVENANCE_CI_RECORD,
        hc.CONJUNCT_DOGFOOD_DELEGATED: hc.PROVENANCE_DELEGATION_RECORD,
        hc.CONJUNCT_COMMITS_PUSHED: hc.PROVENANCE_GIT_REMOTE,
    }
    out = []
    for key, prov in spec.items():
        override = overrides.get(key)
        if override == "drop":
            continue
        if isinstance(override, hc.BasisConjunct):
            out.append(override)
        else:
            out.append(_conj(key, prov))
    return tuple(out)


def _classify(records, **kw):
    defaults = dict(
        selected=_selected(),
        declared_basis=hc.BASIS_EARLY_HIBERNATE,
        records=records,
        head=_head(),
        conjuncts=_early_conjuncts(),
    )
    defaults.update(kw)
    return hc.classify_hibernate_candidate(**defaults)


class BindLifecycleAnchorTests(unittest.TestCase):
    def test_none_records_is_unreadable_not_absent(self):
        got = hc.bind_lifecycle_anchor(None, selected=_selected())
        self.assertIsInstance(got, hc.HibernateNonCandidate)
        self.assertEqual(got.reason, hc.NON_CANDIDATE_LIFECYCLE_UNREADABLE)

    def test_empty_store_is_absent_not_unreadable(self):
        got = hc.bind_lifecycle_anchor((), selected=_selected())
        self.assertEqual(got.reason, hc.NON_CANDIDATE_LIFECYCLE_ABSENT)

    def test_no_active_record_for_issue_is_absent(self):
        recs = (_Rec(lane_disposition=DISPOSITION_HIBERNATED),)
        got = hc.bind_lifecycle_anchor(recs, selected=_selected())
        self.assertEqual(got.reason, hc.NON_CANDIDATE_LIFECYCLE_ABSENT)

    def test_two_active_records_is_ambiguous(self):
        recs = (_Rec(lane_id="lane-a"), _Rec(lane_id="lane-b"))
        got = hc.bind_lifecycle_anchor(recs, selected=_selected())
        self.assertEqual(got.reason, hc.NON_CANDIDATE_LANE_AMBIGUOUS)

    def test_exactly_one_matching_record_binds_the_exact_anchor(self):
        recs = (_Rec(issue_id="99999", lane_id="other"), _Rec())  # different issue ignored
        got = hc.bind_lifecycle_anchor(recs, selected=_selected())
        self.assertIsInstance(got, hc.LifecycleAnchor)
        self.assertEqual((got.repo_workspace_id, got.lane_id), (WS, LANE))
        self.assertEqual((got.lane_generation, got.revision), (GEN, REV))
        self.assertEqual(got.disposition, DISPOSITION_ACTIVE)

    # -- R1-F1: exact selected-lane binding ----------------------------------------------------
    def test_single_active_row_for_a_different_workspace_is_rejected(self):
        # The only active row for the issue is a lane the enumeration did NOT select.
        recs = (_Rec(repo_workspace_id="ws-OTHER"),)
        got = hc.bind_lifecycle_anchor(recs, selected=_selected())
        self.assertEqual(got.reason, hc.NON_CANDIDATE_WORKSPACE_MISMATCH)

    def test_single_active_row_for_a_different_lane_is_rejected(self):
        recs = (_Rec(lane_id="lane-OTHER"),)
        got = hc.bind_lifecycle_anchor(recs, selected=_selected())
        self.assertEqual(got.reason, hc.NON_CANDIDATE_LANE_IDENTITY_MISMATCH)

    def test_generation_drift_is_stale(self):
        recs = (_Rec(lane_generation=99),)
        got = hc.bind_lifecycle_anchor(recs, selected=_selected())
        self.assertEqual(got.reason, hc.NON_CANDIDATE_GENERATION_MISMATCH)

    def test_revision_drift_is_stale(self):
        recs = (_Rec(revision=99),)
        got = hc.bind_lifecycle_anchor(recs, selected=_selected())
        self.assertEqual(got.reason, hc.NON_CANDIDATE_REVISION_MISMATCH)


class HeadAuthorityTests(unittest.TestCase):
    def test_missing_head_is_rejected(self):
        self.assertEqual(_classify((_Rec(),), head=None).reason, hc.NON_CANDIDATE_HEAD_UNBOUND)

    def test_empty_head_value_is_rejected(self):
        got = _classify((_Rec(),), head=_head(value="   "))
        self.assertEqual(got.reason, hc.NON_CANDIDATE_HEAD_UNBOUND)

    def test_head_bound_from_lifecycle_is_rejected(self):
        got = _classify((_Rec(),), head=_head(provenance=hc.PROVENANCE_LIFECYCLE_READONLY))
        self.assertEqual(got.reason, hc.NON_CANDIDATE_HEAD_UNBOUND)

    def test_head_from_review_record_is_accepted(self):
        got = _classify((_Rec(),), head=_head(provenance=hc.PROVENANCE_REVIEW_RECORD))
        self.assertIsInstance(got, hc.HibernateCandidate)

    def test_lifecycle_provenance_is_not_a_head_authority(self):
        self.assertNotIn(hc.PROVENANCE_LIFECYCLE_READONLY, hc.HEAD_AUTHORITIES)


class ConjunctAnchorTests(unittest.TestCase):
    """R1-F2 + R2-F1: each conjunct's evidence must be about the candidate's exact lane identity
    (workspace + lane + generation) and, if head-bearing, its exact head."""

    def _one(self, key, provenance, **bound):
        return _classify((_Rec(),), conjuncts=_early_conjuncts(**{
            key: _conj(key, provenance, **bound)
        }))

    # -- head drift on head-bearing conjuncts -------------------------------------------------
    def test_review_evidence_at_a_different_head_is_rejected(self):
        got = self._one(hc.CONJUNCT_REVIEW_APPROVED, hc.PROVENANCE_REVIEW_RECORD, bound_head="b" * 40)
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_head_bearing_conjunct_with_empty_head_is_rejected(self):
        got = self._one(hc.CONJUNCT_COMMITS_PUSHED, hc.PROVENANCE_GIT_REMOTE, bound_head="")
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_ci_evidence_head_drift_is_rejected(self):
        got = self._one(hc.CONJUNCT_REQUIRED_CI_GREEN, hc.PROVENANCE_CI_RECORD, bound_head="c" * 40)
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_integration_evidence_at_a_different_head_is_rejected(self):
        # R2-F1: integration must name the candidate's integrated commit, not merely the issue.
        got = self._one(
            hc.CONJUNCT_STAGING_INTEGRATED, hc.PROVENANCE_INTEGRATION_RECORD, bound_head="d" * 40
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_dogfood_delegation_at_a_different_head_is_rejected(self):
        # R2-F1: a dogfood delegation carries the exact SHA; a delegation of another SHA is stale.
        got = self._one(
            hc.CONJUNCT_DOGFOOD_DELEGATED, hc.PROVENANCE_DELEGATION_RECORD, bound_head="e" * 40
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    # -- lane-identity drift on every conjunct (R2-F1) ----------------------------------------
    def test_evidence_from_an_old_lane_generation_is_rejected(self):
        # The same issue's superseded generation must not count after a re-hydrate.
        got = self._one(
            hc.CONJUNCT_STAGING_INTEGRATED, hc.PROVENANCE_INTEGRATION_RECORD, bound_generation=GEN - 1
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_evidence_for_a_different_lane_is_rejected(self):
        got = self._one(
            hc.CONJUNCT_REVIEW_APPROVED, hc.PROVENANCE_REVIEW_RECORD, bound_lane="lane-OTHER"
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_evidence_for_a_different_workspace_is_rejected(self):
        got = self._one(
            hc.CONJUNCT_COMMITS_PUSHED, hc.PROVENANCE_GIT_REMOTE, bound_workspace="ws-OTHER"
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_conjunct_with_unbound_lane_identity_is_rejected(self):
        # Empty workspace/lane and generation 0 (the default "unbound") never match.
        got = self._one(
            hc.CONJUNCT_REVIEW_APPROVED, hc.PROVENANCE_REVIEW_RECORD,
            bound_workspace="", bound_lane="", bound_generation=0,
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_on_anchor_evidence_is_accepted(self):
        self.assertIsInstance(_classify((_Rec(),)), hc.HibernateCandidate)


class BasisTests(unittest.TestCase):
    def test_full_early_hibernate_is_a_candidate(self):
        got = _classify((_Rec(),))
        self.assertIsInstance(got, hc.HibernateCandidate)
        self.assertEqual(got.basis, hc.BASIS_EARLY_HIBERNATE)
        self.assertEqual(len(got.conjuncts), len(hc.EARLY_HIBERNATE_CONJUNCTS))

    def test_declared_basis_must_be_real(self):
        got = _classify((_Rec(),), declared_basis="whatever")
        self.assertEqual(got.reason, hc.NON_CANDIDATE_DECLARED_BASIS_INVALID)

    def test_a_missing_required_conjunct_is_partially_unknown_not_a_fallback(self):
        got = _classify(
            (_Rec(),),
            conjuncts=_early_conjuncts(**{hc.CONJUNCT_REQUIRED_CI_GREEN: "drop"}),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN)

    def test_a_false_conjunct_is_unsatisfied(self):
        got = _classify(
            (_Rec(),),
            conjuncts=_early_conjuncts(**{
                hc.CONJUNCT_COMMITS_PUSHED: _conj(
                    hc.CONJUNCT_COMMITS_PUSHED, hc.PROVENANCE_GIT_REMOTE, satisfied=False
                )
            }),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_BASIS_UNSATISFIED)

    def test_a_conjunct_from_the_wrong_authority_is_rejected(self):
        got = _classify(
            (_Rec(),),
            conjuncts=_early_conjuncts(**{
                hc.CONJUNCT_REVIEW_APPROVED: _conj(
                    hc.CONJUNCT_REVIEW_APPROVED, hc.PROVENANCE_INTEGRATION_RECORD
                )
            }),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_AUTHORITY_MISMATCH)

    def test_no_drain_queue_provenance_can_satisfy_any_conjunct(self):
        drain_verdict_tokens = {"releasable", "hold", "process_retention", "drain_queue"}
        self.assertEqual(drain_verdict_tokens & hc.PROVENANCES, set())
        for authority in hc._CONJUNCT_AUTHORITY.values():
            self.assertIn(authority, hc.PROVENANCES)
            self.assertNotIn(authority, drain_verdict_tokens)

    def test_authority_mismatch_outranks_partial_and_unsatisfied(self):
        conjuncts = _early_conjuncts(**{
            hc.CONJUNCT_REVIEW_APPROVED: _conj(
                hc.CONJUNCT_REVIEW_APPROVED, hc.PROVENANCE_INTEGRATION_RECORD  # wrong authority
            ),
            hc.CONJUNCT_DOGFOOD_DELEGATED: "drop",  # also missing
        })
        got = _classify((_Rec(),), conjuncts=conjuncts)
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_AUTHORITY_MISMATCH)


class DependencyParkTests(unittest.TestCase):
    def _park(self, **over):
        c = _conj(hc.CONJUNCT_PARK_DECLARED, hc.PROVENANCE_PARK_DECLARATION, **over)
        return hc.classify_hibernate_candidate(
            selected=_selected(), declared_basis=hc.BASIS_DEPENDENCY_PARK,
            records=(_Rec(),), head=_head(), conjuncts=(c,),
        )

    def test_dependency_park_needs_only_the_park_declaration(self):
        got = self._park()
        self.assertIsInstance(got, hc.HibernateCandidate)
        self.assertEqual(got.basis, hc.BASIS_DEPENDENCY_PARK)

    def test_dependency_park_rejects_a_park_from_the_wrong_authority(self):
        got = hc.classify_hibernate_candidate(
            selected=_selected(), declared_basis=hc.BASIS_DEPENDENCY_PARK, records=(_Rec(),),
            head=_head(),
            conjuncts=(hc.BasisConjunct(
                key=hc.CONJUNCT_PARK_DECLARED, satisfied=True,
                provenance=hc.PROVENANCE_REVIEW_RECORD,
                bound_workspace=WS, bound_lane=LANE, bound_generation=GEN,
            ),),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_AUTHORITY_MISMATCH)

    def test_dependency_park_rejects_a_declaration_for_an_old_generation(self):
        # R2-F1: park binds to the selected lane generation; a superseded declaration is stale.
        got = self._park(bound_generation=GEN - 1)
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_dependency_park_rejects_a_declaration_for_a_different_lane(self):
        got = self._park(bound_lane="lane-OTHER")
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_ANCHOR_MISMATCH)

    def test_dependency_park_does_not_require_a_head(self):
        # Park is lane-anchored only; an empty bound_head must NOT sink it.
        got = self._park(bound_head="")
        self.assertIsInstance(got, hc.HibernateCandidate)

    def test_early_hibernate_conjuncts_do_not_satisfy_a_declared_park(self):
        got = hc.classify_hibernate_candidate(
            selected=_selected(), declared_basis=hc.BASIS_DEPENDENCY_PARK, records=(_Rec(),),
            head=_head(), conjuncts=_early_conjuncts(),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN)


class PayloadSecretSafetyTests(unittest.TestCase):
    def test_candidate_payload_is_ids_and_tokens_only(self):
        import json

        got = _classify((_Rec(),))
        assert isinstance(got, hc.HibernateCandidate)
        text = json.dumps(got.as_payload())
        for banned in ("token", "password", "secret", "credential", "/users", "/home"):
            self.assertNotIn(banned, text.lower())

    def test_every_non_candidate_reason_is_in_the_closed_vocabulary(self):
        cases = [
            _classify(None),  # unreadable
            _classify(()),  # absent
            _classify((_Rec(), _Rec(lane_id="b"))),  # ambiguous
            _classify((_Rec(repo_workspace_id="x"),)),  # workspace mismatch
            _classify((_Rec(lane_id="x"),)),  # lane mismatch
            _classify((_Rec(lane_generation=99),)),  # generation mismatch
            _classify((_Rec(revision=99),)),  # revision mismatch
            _classify((_Rec(),), head=None),  # head unbound
            _classify((_Rec(),), declared_basis="nope"),  # basis invalid
        ]
        for got in cases:
            self.assertIsInstance(got, hc.HibernateNonCandidate)
            self.assertIn(got.reason, hc.HIBERNATE_NON_CANDIDATE_REASONS)


if __name__ == "__main__":
    unittest.main()
