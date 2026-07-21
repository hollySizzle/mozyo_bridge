"""Pure hibernate-candidate classifier tests (Redmine #14219, tranche T1).

Pins the safety core of the auto-hibernate candidate model:

- **anchor binding is fail-closed** — ``None`` records → unreadable, no active record → absent,
  more than one active record → ambiguous, exactly one → the exact anchor (matching the
  ``authority_execution_index`` ``len(recs) != 1 -> drop`` guard);
- **the head is never the lifecycle record** — a head must be bound from a non-lifecycle authority;
  a missing head or a ``lifecycle_readonly`` head is refused;
- **currency** — a generation / revision drift versus the enumeration's snapshot is stale → zero-op;
- **`releasable` is not a proxy** — each basis conjunct must carry its OWN durable authority; there
  is no drain-queue provenance token, and a required conjunct that is missing / wrong-authority /
  false each folds to a distinct typed reason with never an implicit fallback between bases.

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


@dataclass(frozen=True)
class _Rec:
    """A minimal stand-in for the fields ``bind_lifecycle_anchor`` reads off a lifecycle record."""

    issue_id: str
    repo_workspace_id: str = "ws-1"
    lane_id: str = "lane-abc"
    lane_generation: int = 3
    revision: int = 7
    lane_disposition: str = DISPOSITION_ACTIVE


def _head(value: str = "a" * 40, provenance: str = hc.PROVENANCE_GIT_REMOTE) -> hc.BoundField:
    return hc.BoundField(value=value, provenance=provenance)


def _early_conjuncts(**overrides) -> tuple[hc.BasisConjunct, ...]:
    """The five correctly-sourced, satisfied early-hibernate conjuncts, before any override."""
    base = {
        hc.CONJUNCT_REVIEW_APPROVED: hc.PROVENANCE_REVIEW_RECORD,
        hc.CONJUNCT_STAGING_INTEGRATED: hc.PROVENANCE_INTEGRATION_RECORD,
        hc.CONJUNCT_REQUIRED_CI_GREEN: hc.PROVENANCE_CI_RECORD,
        hc.CONJUNCT_DOGFOOD_DELEGATED: hc.PROVENANCE_DELEGATION_RECORD,
        hc.CONJUNCT_COMMITS_PUSHED: hc.PROVENANCE_GIT_REMOTE,
    }
    out = []
    for key, prov in base.items():
        if overrides.get(key) == "drop":
            continue
        out.append(hc.BasisConjunct(key=key, satisfied=True, provenance=prov))
    for key, mutated in overrides.items():
        if mutated == "drop":
            continue
        out.append(mutated)
    return tuple(out)


def _classify(records, **kw):
    defaults = dict(
        issue_id="14219",
        declared_basis=hc.BASIS_EARLY_HIBERNATE,
        records=records,
        head=_head(),
        conjuncts=_early_conjuncts(),
    )
    defaults.update(kw)
    return hc.classify_hibernate_candidate(**defaults)


class BindLifecycleAnchorTests(unittest.TestCase):
    def test_none_records_is_unreadable_not_absent(self):
        got = hc.bind_lifecycle_anchor(None, issue_id="14219")
        self.assertIsInstance(got, hc.HibernateNonCandidate)
        self.assertEqual(got.reason, hc.NON_CANDIDATE_LIFECYCLE_UNREADABLE)

    def test_empty_store_is_absent_not_unreadable(self):
        got = hc.bind_lifecycle_anchor((), issue_id="14219")
        self.assertEqual(got.reason, hc.NON_CANDIDATE_LIFECYCLE_ABSENT)

    def test_no_active_record_for_issue_is_absent(self):
        recs = (_Rec(issue_id="14219", lane_disposition=DISPOSITION_HIBERNATED),)
        got = hc.bind_lifecycle_anchor(recs, issue_id="14219")
        self.assertEqual(got.reason, hc.NON_CANDIDATE_LIFECYCLE_ABSENT)

    def test_two_active_records_is_ambiguous(self):
        recs = (
            _Rec(issue_id="14219", lane_id="lane-a"),
            _Rec(issue_id="14219", lane_id="lane-b"),
        )
        got = hc.bind_lifecycle_anchor(recs, issue_id="14219")
        self.assertEqual(got.reason, hc.NON_CANDIDATE_LANE_AMBIGUOUS)

    def test_exactly_one_active_record_binds_the_exact_anchor(self):
        recs = (
            _Rec(issue_id="99999", lane_id="other"),  # a different issue is ignored
            _Rec(issue_id="14219", lane_id="lane-abc", lane_generation=3, revision=7),
        )
        got = hc.bind_lifecycle_anchor(recs, issue_id="14219")
        self.assertIsInstance(got, hc.LifecycleAnchor)
        self.assertEqual(got.repo_workspace_id, "ws-1")
        self.assertEqual(got.lane_id, "lane-abc")
        self.assertEqual(got.lane_generation, 3)
        self.assertEqual(got.revision, 7)
        self.assertEqual(got.disposition, DISPOSITION_ACTIVE)


class HeadAuthorityTests(unittest.TestCase):
    def test_missing_head_is_rejected(self):
        got = _classify((_Rec(issue_id="14219"),), head=None)
        self.assertEqual(got.reason, hc.NON_CANDIDATE_HEAD_UNBOUND)

    def test_empty_head_value_is_rejected(self):
        got = _classify((_Rec(issue_id="14219"),), head=_head(value="   "))
        self.assertEqual(got.reason, hc.NON_CANDIDATE_HEAD_UNBOUND)

    def test_head_bound_from_lifecycle_is_rejected(self):
        # The ruling: the head is NEVER inferred from the lifecycle record.
        got = _classify(
            (_Rec(issue_id="14219"),),
            head=_head(provenance=hc.PROVENANCE_LIFECYCLE_READONLY),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_HEAD_UNBOUND)

    def test_head_from_review_record_is_accepted(self):
        got = _classify(
            (_Rec(issue_id="14219"),), head=_head(provenance=hc.PROVENANCE_REVIEW_RECORD)
        )
        self.assertIsInstance(got, hc.HibernateCandidate)

    def test_lifecycle_provenance_is_not_a_head_authority(self):
        self.assertNotIn(hc.PROVENANCE_LIFECYCLE_READONLY, hc.HEAD_AUTHORITIES)


class CurrencyTests(unittest.TestCase):
    def test_generation_drift_is_stale(self):
        got = _classify((_Rec(issue_id="14219", lane_generation=3),), observed_generation=2)
        self.assertEqual(got.reason, hc.NON_CANDIDATE_GENERATION_MISMATCH)

    def test_revision_drift_is_stale(self):
        got = _classify((_Rec(issue_id="14219", revision=7),), observed_revision=6)
        self.assertEqual(got.reason, hc.NON_CANDIDATE_REVISION_MISMATCH)

    def test_matching_generation_and_revision_pass(self):
        got = _classify(
            (_Rec(issue_id="14219", lane_generation=3, revision=7),),
            observed_generation=3,
            observed_revision=7,
        )
        self.assertIsInstance(got, hc.HibernateCandidate)


class BasisTests(unittest.TestCase):
    def test_full_early_hibernate_is_a_candidate(self):
        got = _classify((_Rec(issue_id="14219"),))
        self.assertIsInstance(got, hc.HibernateCandidate)
        self.assertEqual(got.basis, hc.BASIS_EARLY_HIBERNATE)
        self.assertEqual(len(got.conjuncts), len(hc.EARLY_HIBERNATE_CONJUNCTS))

    def test_declared_basis_must_be_real(self):
        got = _classify((_Rec(issue_id="14219"),), declared_basis="whatever")
        self.assertEqual(got.reason, hc.NON_CANDIDATE_DECLARED_BASIS_INVALID)

    def test_a_missing_required_conjunct_is_partially_unknown_not_a_fallback(self):
        # Drop CI green entirely -> partial. It must NOT silently fall back to dependency-park.
        got = _classify(
            (_Rec(issue_id="14219"),),
            conjuncts=_early_conjuncts(**{hc.CONJUNCT_REQUIRED_CI_GREEN: "drop"}),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN)

    def test_a_false_conjunct_is_unsatisfied(self):
        got = _classify(
            (_Rec(issue_id="14219"),),
            conjuncts=_early_conjuncts(**{
                hc.CONJUNCT_COMMITS_PUSHED: hc.BasisConjunct(
                    key=hc.CONJUNCT_COMMITS_PUSHED,
                    satisfied=False,
                    provenance=hc.PROVENANCE_GIT_REMOTE,
                )
            }),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_BASIS_UNSATISFIED)

    def test_a_conjunct_from_the_wrong_authority_is_rejected(self):
        # review_approved "proven" by the integration authority is a structural proxy.
        got = _classify(
            (_Rec(issue_id="14219"),),
            conjuncts=_early_conjuncts(**{
                hc.CONJUNCT_REVIEW_APPROVED: hc.BasisConjunct(
                    key=hc.CONJUNCT_REVIEW_APPROVED,
                    satisfied=True,
                    provenance=hc.PROVENANCE_INTEGRATION_RECORD,
                )
            }),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_AUTHORITY_MISMATCH)

    def test_no_drain_queue_provenance_can_satisfy_any_conjunct(self):
        # `releasable` is a drain verdict; there is deliberately no provenance token for it, so it
        # cannot even be named as a conjunct's authority. Every legitimate authority is a
        # first-class durable record.
        drain_verdict_tokens = {"releasable", "hold", "process_retention", "drain_queue"}
        self.assertEqual(drain_verdict_tokens & hc.PROVENANCES, set())
        for key, authority in hc._CONJUNCT_AUTHORITY.items():
            self.assertIn(authority, hc.PROVENANCES)
            self.assertNotIn(authority, drain_verdict_tokens)

    def test_authority_mismatch_outranks_partial_and_unsatisfied(self):
        # A proxy attempt is the loudest signal even when another conjunct is also missing.
        conjuncts = _early_conjuncts(**{
            hc.CONJUNCT_REVIEW_APPROVED: hc.BasisConjunct(
                key=hc.CONJUNCT_REVIEW_APPROVED,
                satisfied=True,
                provenance=hc.PROVENANCE_INTEGRATION_RECORD,  # wrong authority
            ),
            hc.CONJUNCT_DOGFOOD_DELEGATED: "drop",  # also missing
        })
        got = _classify((_Rec(issue_id="14219"),), conjuncts=conjuncts)
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_AUTHORITY_MISMATCH)


class DependencyParkTests(unittest.TestCase):
    def test_dependency_park_needs_only_the_park_declaration(self):
        got = _classify(
            (_Rec(issue_id="14219"),),
            declared_basis=hc.BASIS_DEPENDENCY_PARK,
            conjuncts=(
                hc.BasisConjunct(
                    key=hc.CONJUNCT_PARK_DECLARED,
                    satisfied=True,
                    provenance=hc.PROVENANCE_PARK_DECLARATION,
                ),
            ),
        )
        self.assertIsInstance(got, hc.HibernateCandidate)
        self.assertEqual(got.basis, hc.BASIS_DEPENDENCY_PARK)

    def test_dependency_park_rejects_the_blocked_gate_as_a_park_proxy(self):
        # A `blocked` gate is not a park declaration; only the durable park authority counts.
        got = _classify(
            (_Rec(issue_id="14219"),),
            declared_basis=hc.BASIS_DEPENDENCY_PARK,
            conjuncts=(
                hc.BasisConjunct(
                    key=hc.CONJUNCT_PARK_DECLARED,
                    satisfied=True,
                    provenance=hc.PROVENANCE_REVIEW_RECORD,  # anything but the park authority
                ),
            ),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_CONJUNCT_AUTHORITY_MISMATCH)

    def test_early_hibernate_conjuncts_do_not_satisfy_a_declared_park(self):
        # No implicit basis fallback: five early conjuncts, but the declared basis is park.
        got = _classify(
            (_Rec(issue_id="14219"),),
            declared_basis=hc.BASIS_DEPENDENCY_PARK,
            conjuncts=_early_conjuncts(),
        )
        self.assertEqual(got.reason, hc.NON_CANDIDATE_BASIS_PARTIALLY_UNKNOWN)


class PayloadSecretSafetyTests(unittest.TestCase):
    def test_candidate_payload_is_ids_and_tokens_only(self):
        import json

        got = _classify((_Rec(issue_id="14219"),))
        assert isinstance(got, hc.HibernateCandidate)
        text = json.dumps(got.as_payload())
        for banned in ("token", "password", "secret", "credential", "/users", "/home"):
            self.assertNotIn(banned, text.lower())

    def test_every_non_candidate_reason_is_in_the_closed_vocabulary(self):
        # Exercise one representative of each reason and assert it is a member of the closed set.
        cases = [
            _classify(None),  # unreadable
            _classify(()),  # absent
            _classify((_Rec(issue_id="14219"), _Rec(issue_id="14219", lane_id="b"))),  # ambiguous
            _classify((_Rec(issue_id="14219", lane_generation=9),), observed_generation=1),
            _classify((_Rec(issue_id="14219", revision=9),), observed_revision=1),
            _classify((_Rec(issue_id="14219"),), head=None),
            _classify((_Rec(issue_id="14219"),), declared_basis="nope"),
        ]
        for got in cases:
            self.assertIsInstance(got, hc.HibernateNonCandidate)
            self.assertIn(got.reason, hc.HIBERNATE_NON_CANDIDATE_REASONS)


if __name__ == "__main__":
    unittest.main()
