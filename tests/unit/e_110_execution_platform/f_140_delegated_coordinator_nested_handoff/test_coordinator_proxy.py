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
    ACTION_DISPATCH_NEXT,
    ANCHOR_ACTION_MISMATCH,
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
        self.assertEqual(PROXY_ACTIONS, (ACTION_DISPATCH_NEXT,))

    def test_every_action_names_the_decision_that_authorizes_it(self):
        # Review j#89878 finding 1: an action with no decision token is an action whose anchor
        # cannot be verified. The map must cover the vocabulary exactly — no delegable action
        # without a decision, and no decision entry for an action that cannot be requested.
        self.assertEqual(sorted(ACTION_DECISION_TOKENS), sorted(PROXY_ACTIONS))
        for action, tokens in ACTION_DECISION_TOKENS.items():
            self.assertTrue(tokens, action)
            self.assertTrue(all(t.strip() for t in tokens), action)

    def test_dispatch_next_is_authorized_by_the_dispatch_decision(self):
        self.assertEqual(ACTION_DECISION_TOKENS[ACTION_DISPATCH_NEXT], ("implementation_request",))

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
    """The (action, journal) PAIR is the unit of authority (review j#89878 finding 1)."""

    DECISIONS = {
        "implementation_request": ("89688",),
        "implementation_done": ("89873",),
        "start": ("89754",),
    }

    def _status(self, journal, decisions=None, action=ACTION_DISPATCH_NEXT):
        return anchor_status_for(
            journal,
            action=action,
            decision_journals=self.DECISIONS if decisions is None else decisions,
        )

    def test_the_action_s_own_current_decision_verifies(self):
        self.assertEqual(self._status("89688"), ANCHOR_VERIFIED)

    def test_a_real_journal_carrying_another_decision_does_not_authorize_this_action(self):
        # The exact defect: an implementation_done must never authorize a dispatch_next.
        self.assertEqual(self._status("89873"), ANCHOR_ACTION_MISMATCH)
        self.assertEqual(self._status("89754"), ANCHOR_ACTION_MISMATCH)

    def test_a_journal_with_no_marker_never_verifies(self):
        self.assertEqual(self._status("99999"), ANCHOR_UNVERIFIED)

    def test_an_unreachable_redmine_never_verifies(self):
        # The live read failing yields no decisions at all; that must not read as verified.
        self.assertEqual(self._status("89688", decisions={}), ANCHOR_UNVERIFIED)

    def test_an_empty_journal_never_verifies(self):
        self.assertEqual(self._status(""), ANCHOR_UNVERIFIED)

    def test_a_newer_decision_of_the_same_kind_supersedes(self):
        decisions = {"implementation_request": ("89688", "89900")}
        self.assertEqual(self._status("89688", decisions=decisions), ANCHOR_SUPERSEDED)
        self.assertEqual(self._status("89900", decisions=decisions), ANCHOR_VERIFIED)

    def test_a_later_unrelated_gate_does_not_supersede_the_authorization(self):
        # Supersession is judged WITHIN the action's own decision series. An implementation_done
        # recorded after the dispatch decision does not make that decision stale.
        decisions = {
            "implementation_request": ("89688",),
            "implementation_done": ("89873",),  # a later journal, a different kind
        }
        self.assertEqual(self._status("89688", decisions=decisions), ANCHOR_VERIFIED)

    def test_an_unknown_action_can_authorize_nothing(self):
        self.assertEqual(self._status("89688", action="not_an_action"), ANCHOR_ACTION_MISMATCH)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
