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
    ACTION_DISPATCH_NEXT,
    ACTION_WORKFLOW_STEP,
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
    REASON_WORKSPACE_UNRESOLVED,
    TARGET_AMBIGUOUS,
    TARGET_LOCATOR_MISSING,
    TARGET_MISSING,
    TARGET_OK,
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
        self.assertEqual(PROXY_ACTIONS, (ACTION_DISPATCH_NEXT, ACTION_WORKFLOW_STEP))

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

    def test_both_actions_deliver_when_every_other_link_is_ok(self):
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
            (dict(anchor=ANCHOR_UNVERIFIED), REASON_ANCHOR_UNVERIFIED),
            (dict(anchor=ANCHOR_SUPERSEDED), REASON_ANCHOR_SUPERSEDED),
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
    def test_exactly_one_addressable_agent_is_a_target(self):
        self.assertEqual(target_status_from_cardinality(1, 1), TARGET_OK)

    def test_zero_is_missing(self):
        self.assertEqual(target_status_from_cardinality(0, 0), TARGET_MISSING)

    def test_duplicates_are_ambiguity_not_a_pick(self):
        self.assertEqual(target_status_from_cardinality(2, 2), TARGET_AMBIGUOUS)
        self.assertEqual(target_status_from_cardinality(2, 1), TARGET_AMBIGUOUS)
        self.assertEqual(target_status_from_cardinality(5, 0), TARGET_AMBIGUOUS)

    def test_one_agent_without_a_locator_is_unaddressable(self):
        self.assertEqual(target_status_from_cardinality(1, 0), TARGET_LOCATOR_MISSING)


class AnchorStatusTest(unittest.TestCase):
    def test_the_current_gate_journal_verifies(self):
        self.assertEqual(
            anchor_status_for("89736", ("89688", "89712", "89736"), latest="89736"),
            ANCHOR_VERIFIED,
        )

    def test_an_earlier_gate_journal_is_superseded(self):
        self.assertEqual(
            anchor_status_for("89688", ("89688", "89712", "89736"), latest="89736"),
            ANCHOR_SUPERSEDED,
        )

    def test_a_journal_that_is_not_a_gate_marker_never_verifies(self):
        self.assertEqual(
            anchor_status_for("99999", ("89688", "89736"), latest="89736"), ANCHOR_UNVERIFIED
        )

    def test_an_unreachable_redmine_never_verifies(self):
        # The live read failing yields no markers; that must not read as a verified anchor.
        self.assertEqual(anchor_status_for("89736", (), latest=""), ANCHOR_UNVERIFIED)

    def test_an_empty_journal_never_verifies(self):
        self.assertEqual(anchor_status_for("", ("89736",), latest="89736"), ANCHOR_UNVERIFIED)

    def test_a_single_marker_that_is_also_the_latest_verifies(self):
        self.assertEqual(anchor_status_for("89688", ("89688",), latest="89688"), ANCHOR_VERIFIED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
