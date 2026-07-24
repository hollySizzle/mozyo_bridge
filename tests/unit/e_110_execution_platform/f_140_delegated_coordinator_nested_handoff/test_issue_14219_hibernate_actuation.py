"""Pure actuation-planning tests (Redmine #14219, tranche T2a).

Pins the bridge from an approved candidate to a single public hibernate invocation:

- **basis flags come only from the candidate** — an early-hibernate candidate sets exactly the five
  early flags; a dependency-park candidate sets exactly ``explicitly_parked``; neither leaks into
  the other, and nothing is re-asserted;
- **obligation flags come only from the action-time observation** — they pass through verbatim and
  default fail-closed;
- **the CAS is pinned** — ``expected_lane_generation`` / ``expected_revision`` are the anchor's, so
  a raced generation/revision refuses at execute time;
- **the journal is required** — an empty basis-event journal is a typed no-actuation, never a guess;
- **at most one mutation per pass** — ``plan_pass`` chooses one candidate deterministically and
  defers the rest (never drops them).
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    hibernate_actuation as ha,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    hibernate_candidate as hc,
)


def _candidate(issue="14219", lane="lane-abc", ws="ws-1", gen=3, rev=7, basis=hc.BASIS_EARLY_HIBERNATE):
    anchor = hc.LifecycleAnchor(
        issue_id=issue, repo_workspace_id=ws, lane_id=lane, lane_generation=gen, revision=rev
    )
    return hc.HibernateCandidate(
        issue_id=issue,
        anchor=anchor,
        head=hc.BoundField(value="a" * 40, provenance=hc.PROVENANCE_GIT_REMOTE),
        basis=basis,
        conjuncts=(),
    )


def _obligations(**over):
    base = dict(
        callbacks_drained=True, no_review_pending=True, no_owner_approval_pending=True,
        no_integration_pending=True, no_pending_prompt=True, not_working=True,
        worktree_clean=True, boundary_recorded=False,
    )
    base.update(over)
    return ha.ActionTimeObligations(**base)


class DeriveActuationRequestTests(unittest.TestCase):
    def test_early_hibernate_sets_exactly_the_five_early_flags(self):
        got = ha.derive_actuation_request(_candidate(), _obligations(), decision_journal="85508")
        self.assertIsInstance(got, ha.ActuationRequestFields)
        f = got.assertion_flags
        for k in ("review_approved", "staging_integrated", "required_ci_green",
                  "dogfood_delegated", "commits_pushed"):
            self.assertTrue(f[k], k)
        self.assertFalse(f["explicitly_parked"])

    def test_dependency_park_sets_exactly_explicitly_parked(self):
        got = ha.derive_actuation_request(
            _candidate(basis=hc.BASIS_DEPENDENCY_PARK), _obligations(), decision_journal="84508"
        )
        f = got.assertion_flags
        self.assertTrue(f["explicitly_parked"])
        for k in ("review_approved", "staging_integrated", "required_ci_green",
                  "dogfood_delegated", "commits_pushed"):
            self.assertFalse(f[k], k)

    def test_obligation_flags_pass_through_verbatim(self):
        obl = _obligations(worktree_clean=False, boundary_recorded=True, not_working=False)
        got = ha.derive_actuation_request(_candidate(), obl, decision_journal="85508")
        f = got.assertion_flags
        self.assertFalse(f["worktree_clean"])
        self.assertTrue(f["boundary_recorded"])
        self.assertFalse(f["not_working"])
        self.assertTrue(f["callbacks_drained"])

    def test_the_cas_is_pinned_to_the_anchor_generation_and_revision(self):
        got = ha.derive_actuation_request(
            _candidate(gen=9, rev=4), _obligations(), decision_journal="85508"
        )
        self.assertEqual(got.expected_lane_generation, "9")
        self.assertEqual(got.expected_revision, "4")
        self.assertEqual(got.issue, "14219")
        self.assertEqual(got.lane, "lane-abc")
        self.assertEqual(got.journal, "85508")

    def test_an_empty_journal_is_a_typed_no_actuation(self):
        got = ha.derive_actuation_request(_candidate(), _obligations(), decision_journal="   ")
        self.assertEqual(got, ha.NO_ACTUATION_MISSING_JOURNAL)

    def test_the_flag_set_matches_the_public_assertions_dataclass(self):
        # A derived flag map must be exactly the HibernateAssertions kwargs -- no missing/extra key,
        # so `HibernateAssertions(**assertion_flags)` never raises or silently drops.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_assertions import (  # noqa: E501
            HibernateAssertions,
        )
        import dataclasses

        got = ha.derive_actuation_request(_candidate(), _obligations(), decision_journal="85508")
        field_names = {f.name for f in dataclasses.fields(HibernateAssertions)}
        self.assertEqual(set(got.assertion_flags), field_names)
        HibernateAssertions(**got.assertion_flags)  # must not raise


class OrderCandidatesTests(unittest.TestCase):
    def test_empty_is_empty(self):
        self.assertEqual(ha.order_candidates([]), ())

    def test_deterministic_order_by_issue_then_lane(self):
        a = _candidate(issue="14200", lane="lane-a")
        b = _candidate(issue="14219", lane="lane-b")
        c = _candidate(issue="14219", lane="lane-a")
        self.assertEqual(ha.order_candidates([b, c, a]), (a, c, b))

    def test_order_is_stable_across_input_order(self):
        a = _candidate(issue="14219", lane="lane-a")
        b = _candidate(issue="14219", lane="lane-b")
        self.assertEqual(ha.order_candidates([a, b]), ha.order_candidates([b, a]))

    def test_no_candidate_is_dropped(self):
        cs = [_candidate(issue="14219", lane=f"lane-{i}") for i in range(4)]
        self.assertEqual(set(ha.order_candidates(cs)), set(cs))


if __name__ == "__main__":
    unittest.main()
