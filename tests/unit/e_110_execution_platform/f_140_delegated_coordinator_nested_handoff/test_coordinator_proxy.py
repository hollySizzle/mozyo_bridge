"""Pure single-step coordinator-proxy decision-matrix tests (Redmine #14546).

Pins the authority chain an external coordinator client's delegation must clear, and the fixed
reason each broken link produces. Two things are asserted deliberately rather than incidentally:

- the matrix is evaluated in **authority order**, so the reported reason is the *first* broken link.
  That ordering is not cosmetic: it also decides what work a refusal does, and the fence is last so
  a delegation refused for a bad target or a superseded anchor never consumes a generation;
- there is **exactly one** path to ``deliver``. The suite enumerates each link's non-OK values and
  asserts every one of them zero-sends, so a future link that is added but not checked cannot
  silently become optional.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
    ACTION_DECISION_TOKENS,
    ACTION_BOOTSTRAP_LANE,
    ACTION_DISPATCH_NEXT,
    ACTION_SCOPES,
    ANCHOR_ACTION_MISMATCH,
    ANCHOR_DECISION_AMBIGUOUS,
    ANCHOR_DECISION_UNREADABLE,
    DECISION_REFUSAL_STATUS,
    ANCHOR_DECISION_INCOMPLETE,
    ANCHOR_GENERATION_STALE,
    ANCHOR_LANE_UNRESOLVED,
    ANCHOR_SCOPE_MISMATCH,
    ANCHOR_SUPERSEDED,
    ANCHOR_UNVERIFIED,
    ANCHOR_VERIFIED,
    AUTHORITY_BLOCKED,
    AUTHORITY_MISSING,
    AUTHORITY_RESOLVED,
    DELIVER,
    FENCE_DUPLICATE,
    FENCE_OPEN,
    FENCE_RECONCILE,
    FENCE_STALE,
    FENCE_UNAVAILABLE,
    PROVIDER_RESOLVED,
    PROVIDER_UNRESOLVED,
    PROXY_ACTIONS,
    REASON_ACTION_UNKNOWN,
    REASON_ANCHOR_ACTION_MISMATCH,
    REASON_ANCHOR_DECISION_AMBIGUOUS,
    REASON_ANCHOR_DECISION_UNREADABLE,
    REASON_ANCHOR_DECISION_INCOMPLETE,
    REASON_ANCHOR_GENERATION_STALE,
    REASON_ANCHOR_LANE_UNRESOLVED,
    REASON_ANCHOR_SCOPE_MISMATCH,
    REASON_ANCHOR_SUPERSEDED,
    REASON_ANCHOR_UNVERIFIED,
    REASON_AUTHORITY_BLOCKED,
    REASON_AUTHORITY_MISSING,
    REASON_DUPLICATE,
    REASON_FENCE_RECONCILE,
    REASON_FENCE_UNAVAILABLE,
    REASON_PROVIDER_UNRESOLVED,
    REASON_STALE,
    REASON_TARGET_AMBIGUOUS,
    REASON_TARGET_LOCATOR_MISSING,
    REASON_TARGET_MISSING,
    REASON_TARGET_UNATTESTED,
    REASON_WORKSPACE_UNRESOLVED,
    TARGET_AMBIGUOUS,
    TARGET_LOCATOR_MISSING,
    TARGET_MISSING,
    TARGET_OK,
    TARGET_UNATTESTED,
    WORKSPACE_RESOLVED,
    WORKSPACE_UNRESOLVED,
    ZERO_SEND,
    DecisionRecord,
    IssueExpectation,
    LaneExpectation,
    SCOPE_ISSUE,
    SCOPE_LANE,
    ProxyLinks,
    anchor_status_for,
    decide_proxy_delegation,
    normalize_action,
    target_status_from_cardinality,
)


def _links(**overrides) -> ProxyLinks:
    base = dict(
        action=ACTION_DISPATCH_NEXT,
        workspace=WORKSPACE_RESOLVED,
        authority=AUTHORITY_RESOLVED,
        provider=PROVIDER_RESOLVED,
        target=TARGET_OK,
        anchor=ANCHOR_VERIFIED,
        fence=FENCE_OPEN,
    )
    base.update(overrides)
    return ProxyLinks(**base)


class ActionVocabularyTest(unittest.TestCase):
    def test_the_vocabulary_is_closed(self):
        self.assertEqual(PROXY_ACTIONS, (ACTION_BOOTSTRAP_LANE, ACTION_DISPATCH_NEXT))

    def test_every_action_declares_its_scope(self):
        self.assertEqual(sorted(ACTION_SCOPES), sorted(PROXY_ACTIONS))
        self.assertEqual(ACTION_SCOPES[ACTION_BOOTSTRAP_LANE], SCOPE_ISSUE)
        self.assertEqual(ACTION_SCOPES[ACTION_DISPATCH_NEXT], SCOPE_LANE)

    def test_every_action_names_the_decision_that_authorizes_it(self):
        # Review j#89878 finding 1: an action with no decision token is an action whose anchor
        # cannot be verified. The map must cover the vocabulary exactly — no delegable action
        # without a decision, and no decision entry for an action that cannot be requested.
        self.assertEqual(sorted(ACTION_DECISION_TOKENS), sorted(PROXY_ACTIONS))
        for action, tokens in ACTION_DECISION_TOKENS.items():
            self.assertTrue(tokens, action)
            self.assertTrue(all(t.strip() for t in tokens), action)

    def test_both_actions_are_authorized_by_the_dispatch_decision(self):
        for action in PROXY_ACTIONS:
            self.assertEqual(ACTION_DECISION_TOKENS[action], ("implementation_request",), action)

    def test_normalize_rejects_anything_outside_it(self):
        for value in ("", None, "sublane_retire", "close", "dispatch next", "DISPATCH_NEXT"):
            self.assertEqual(normalize_action(value), "", repr(value))

    def test_normalize_trims_a_valid_token(self):
        self.assertEqual(normalize_action("  dispatch_next "), ACTION_DISPATCH_NEXT)


class TheOnlyDeliverPathTest(unittest.TestCase):
    def test_all_links_ok_delivers(self):
        decision = decide_proxy_delegation(_links())
        self.assertTrue(decision.delivers)
        self.assertEqual(decision.decision, DELIVER)
        self.assertEqual(decision.reason, "")

    def test_every_action_delivers_when_every_other_link_is_ok(self):
        for action in PROXY_ACTIONS:
            self.assertTrue(decide_proxy_delegation(_links(action=action)).delivers, action)

    def test_every_broken_link_zero_sends_with_its_fixed_reason(self):
        cases = [
            (dict(action="retire_everything"), REASON_ACTION_UNKNOWN),
            (dict(workspace=WORKSPACE_UNRESOLVED), REASON_WORKSPACE_UNRESOLVED),
            (dict(authority=AUTHORITY_MISSING), REASON_AUTHORITY_MISSING),
            (dict(authority=AUTHORITY_BLOCKED), REASON_AUTHORITY_BLOCKED),
            (dict(provider=PROVIDER_UNRESOLVED), REASON_PROVIDER_UNRESOLVED),
            (dict(target=TARGET_MISSING), REASON_TARGET_MISSING),
            (dict(target=TARGET_AMBIGUOUS), REASON_TARGET_AMBIGUOUS),
            (dict(target=TARGET_LOCATOR_MISSING), REASON_TARGET_LOCATOR_MISSING),
            (dict(target=TARGET_UNATTESTED), REASON_TARGET_UNATTESTED),
            (dict(anchor=ANCHOR_UNVERIFIED), REASON_ANCHOR_UNVERIFIED),
            (dict(anchor=ANCHOR_SUPERSEDED), REASON_ANCHOR_SUPERSEDED),
            (dict(anchor=ANCHOR_ACTION_MISMATCH), REASON_ANCHOR_ACTION_MISMATCH),
            (dict(anchor=ANCHOR_DECISION_INCOMPLETE), REASON_ANCHOR_DECISION_INCOMPLETE),
            (dict(anchor=ANCHOR_GENERATION_STALE), REASON_ANCHOR_GENERATION_STALE),
            (dict(anchor=ANCHOR_LANE_UNRESOLVED), REASON_ANCHOR_LANE_UNRESOLVED),
            (dict(anchor=ANCHOR_SCOPE_MISMATCH), REASON_ANCHOR_SCOPE_MISMATCH),
            (dict(anchor=ANCHOR_DECISION_AMBIGUOUS), REASON_ANCHOR_DECISION_AMBIGUOUS),
            (dict(anchor=ANCHOR_DECISION_UNREADABLE), REASON_ANCHOR_DECISION_UNREADABLE),
            (dict(fence=FENCE_DUPLICATE), REASON_DUPLICATE),
            (dict(fence=FENCE_STALE), REASON_STALE),
            (dict(fence=FENCE_RECONCILE), REASON_FENCE_RECONCILE),
            (dict(fence=FENCE_UNAVAILABLE), REASON_FENCE_UNAVAILABLE),
        ]
        for overrides, reason in cases:
            decision = decide_proxy_delegation(_links(**overrides))
            self.assertEqual(decision.decision, ZERO_SEND, overrides)
            self.assertEqual(decision.reason, reason, overrides)

    def test_an_unknown_status_value_is_never_treated_as_ok(self):
        # A typo / a future token must fail closed, not fall through the equality checks.
        for field in ("workspace", "authority", "provider", "target", "anchor", "fence"):
            decision = decide_proxy_delegation(_links(**{field: "probably_fine"}))
            self.assertEqual(decision.decision, ZERO_SEND, field)
            self.assertTrue(decision.reason, field)


class AuthorityOrderingTest(unittest.TestCase):
    def test_the_first_broken_link_is_the_reported_one(self):
        # Everything broken at once: the reason must be the most fundamental link, not the last.
        decision = decide_proxy_delegation(
            _links(
                workspace=WORKSPACE_UNRESOLVED,
                authority=AUTHORITY_MISSING,
                provider=PROVIDER_UNRESOLVED,
                target=TARGET_MISSING,
                anchor=ANCHOR_UNVERIFIED,
                fence=FENCE_DUPLICATE,
            )
        )
        self.assertEqual(decision.reason, REASON_WORKSPACE_UNRESOLVED)

    def test_an_unknown_action_outranks_every_other_link(self):
        decision = decide_proxy_delegation(
            _links(action="", workspace=WORKSPACE_UNRESOLVED, fence=FENCE_DUPLICATE)
        )
        self.assertEqual(decision.reason, REASON_ACTION_UNKNOWN)

    def test_a_bad_target_outranks_the_fence(self):
        # Ordering with teeth: this is what keeps a doomed delegation from consuming a generation.
        decision = decide_proxy_delegation(
            _links(target=TARGET_AMBIGUOUS, fence=FENCE_DUPLICATE)
        )
        self.assertEqual(decision.reason, REASON_TARGET_AMBIGUOUS)

    def test_a_superseded_anchor_outranks_the_fence(self):
        decision = decide_proxy_delegation(
            _links(anchor=ANCHOR_SUPERSEDED, fence=FENCE_DUPLICATE)
        )
        self.assertEqual(decision.reason, REASON_ANCHOR_SUPERSEDED)


class TargetCardinalityTest(unittest.TestCase):
    def test_exactly_one_addressable_attested_agent_is_a_target(self):
        self.assertEqual(target_status_from_cardinality(1, 1, attested=True), TARGET_OK)

    def test_zero_is_missing(self):
        self.assertEqual(target_status_from_cardinality(0, 0, attested=True), TARGET_MISSING)

    def test_duplicates_are_ambiguity_not_a_pick(self):
        self.assertEqual(target_status_from_cardinality(2, 2, attested=True), TARGET_AMBIGUOUS)
        self.assertEqual(target_status_from_cardinality(2, 1, attested=True), TARGET_AMBIGUOUS)
        self.assertEqual(target_status_from_cardinality(5, 0, attested=True), TARGET_AMBIGUOUS)

    def test_one_agent_without_a_locator_is_unaddressable(self):
        self.assertEqual(target_status_from_cardinality(1, 0, attested=True), TARGET_LOCATOR_MISSING)

    def test_a_name_match_without_an_attestation_is_not_a_target(self):
        # Review j#89878 finding 2: the decoded assigned name is what the slot was launched to BE.
        self.assertEqual(target_status_from_cardinality(1, 1, attested=False), TARGET_UNATTESTED)

    def test_an_unperformed_attestation_join_fails_closed(self):
        # `None` = the caller could not join (unreadable store). That must not decay to a match.
        self.assertEqual(target_status_from_cardinality(1, 1, attested=None), TARGET_UNATTESTED)
        self.assertEqual(target_status_from_cardinality(1, 1), TARGET_UNATTESTED)


class AnchorStatusTest(unittest.TestCase):
    """The NAMED journal's canonical decision, matched against live facts (j#90329 contract 5)."""

    LANE = "lane_a"
    EXPECTED = LaneExpectation(lane=LANE, generation=1, decision_journal="89688")

    def _status(self, decision=..., expected=..., action=ACTION_DISPATCH_NEXT, refusal=""):
        if decision is ...:
            decision = DecisionRecord("89688", "implementation_request", self.LANE, "1")
        return anchor_status_for(
            action=action,
            decision=decision,
            decision_refusal=refusal,
            expected=self.EXPECTED if expected is ... else expected,
        )

    def test_the_named_journal_s_canonical_decision_verifies(self):
        self.assertEqual(self._status(), ANCHOR_VERIFIED)

    def test_every_canonical_read_refusal_maps_to_a_status(self):
        # The reader's fixed refusals are the ONLY way a decision is absent now — there is no
        # history to scan, so nothing elsewhere on the issue can produce or poison one.
        self.assertEqual(
            DECISION_REFUSAL_STATUS,
            {
                "no_canonical_decision": ANCHOR_UNVERIFIED,
                "duplicate_canonical_decision": ANCHOR_DECISION_AMBIGUOUS,
                # A same-kind claim in a body the canonical producer could not have rendered
                # (Redmine #14667). Its own status, because its remedy is its own: the journal
                # carries a decision, and it has to be re-recorded rather than added to.
                "unreadable_canonical_decision": ANCHOR_DECISION_UNREADABLE,
                "action_not_declared": ANCHOR_ACTION_MISMATCH,
            },
        )
        for refusal, status in DECISION_REFUSAL_STATUS.items():
            self.assertEqual(self._status(decision=None, refusal=refusal), status, refusal)

    def test_an_unknown_refusal_fails_closed_as_unverified(self):
        self.assertEqual(self._status(decision=None, refusal="something_new"), ANCHOR_UNVERIFIED)

    def test_a_decision_without_a_lane_authorizes_nothing(self):
        decision = DecisionRecord("89688", "implementation_request")
        self.assertEqual(self._status(decision=decision), ANCHOR_DECISION_INCOMPLETE)

    def test_a_decision_without_a_numeric_generation_authorizes_nothing(self):
        for generation in ("", "one", "1.0", "-1"):
            decision = DecisionRecord("89688", "implementation_request", self.LANE, generation)
            self.assertEqual(
                self._status(decision=decision), ANCHOR_DECISION_INCOMPLETE, generation
            )

    def test_a_lane_with_no_live_facts_authorizes_nothing(self):
        self.assertEqual(self._status(expected=None), ANCHOR_LANE_UNRESOLVED)

    def test_a_decision_naming_a_different_lane_is_a_scope_mismatch(self):
        expected = LaneExpectation(lane="lane_b", generation=1, decision_journal="89688")
        self.assertEqual(self._status(expected=expected), ANCHOR_SCOPE_MISMATCH)

    def test_a_real_lane_advance_stales_the_old_decision(self):
        expected = LaneExpectation(lane=self.LANE, generation=2, decision_journal="90000")
        self.assertEqual(self._status(expected=expected), ANCHOR_GENERATION_STALE)

    def test_an_unknown_action_can_authorize_nothing(self):
        self.assertEqual(self._status(action="not_an_action"), ANCHOR_ACTION_MISMATCH)


class BootstrapScopeTest(unittest.TestCase):
    """The rail must act from the state that has NO lane (review j#90068 finding 1)."""

    ISSUE = "14546"

    def _status(self, decision=..., expected=..., action=ACTION_BOOTSTRAP_LANE, refusal=""):
        if decision is ...:
            decision = DecisionRecord("89688", "implementation_request")
        if expected is ...:
            expected = IssueExpectation(
                issue=self.ISSUE, owns_active_lane=False, latest_decision_journal=""
            )
        return anchor_status_for(
            action=action, decision=decision, decision_refusal=refusal, expected=expected
        )

    def test_a_fresh_issue_with_no_lane_verifies(self):
        self.assertEqual(self._status(), ANCHOR_VERIFIED)

    def test_an_issue_that_already_owns_a_lane_is_past_the_bootstrap(self):
        expected = IssueExpectation(
            issue=self.ISSUE, owns_active_lane=True, latest_decision_journal=""
        )
        self.assertEqual(self._status(expected=expected), ANCHOR_SCOPE_MISMATCH)

    def test_a_lane_scoped_decision_is_not_a_bootstrap_decision(self):
        decision = DecisionRecord("89688", "implementation_request", "lane_a", "1")
        self.assertEqual(self._status(decision=decision), ANCHOR_SCOPE_MISMATCH)

    def test_an_unreadable_lifecycle_authority_fails_closed(self):
        self.assertEqual(self._status(expected=None), ANCHOR_LANE_UNRESOLVED)

    def test_a_lane_expectation_is_not_an_issue_expectation(self):
        expected = LaneExpectation(lane="lane_a", generation=1, decision_journal="89688")
        self.assertEqual(self._status(expected=expected), ANCHOR_SCOPE_MISMATCH)

    def test_a_quotation_is_neither_authority_nor_poison(self):
        # j#90329 contract 5: a quotation on ANOTHER journal is simply not read. A quotation in the
        # NAMED journal yields `no_canonical_decision` (the scanner strips code spans first), which
        # refuses this invocation without touching any other decision on the issue.
        self.assertEqual(
            self._status(decision=None, refusal="no_canonical_decision"), ANCHOR_UNVERIFIED
        )

    def test_the_lane_scoped_action_still_requires_a_lane(self):
        self.assertEqual(
            self._status(action=ACTION_DISPATCH_NEXT, expected=None), ANCHOR_DECISION_INCOMPLETE
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
