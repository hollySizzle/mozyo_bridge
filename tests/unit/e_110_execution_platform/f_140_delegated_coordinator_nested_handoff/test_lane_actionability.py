"""Lane actionability / ownership resolution tests (Redmine #13756).

Pins the axis that lets a delegated review stop being a coordinator stop reason — and,
more importantly, every way that claim is refused. Each guard is probed on its own,
because a delegation check that verifies only *some* of its preconditions still passes
the happy path:

- the delivery must have landed (``not_attempted`` / ``delivery_failed`` both refuse);
- a durable callback must be expected (an ACK is not completion);
- the callback must not be overdue (a stalled delegation is coordinator debt again);
- the owner must be nameable and must actually be a delegate;
- the lane must be a verified managed sublane;
- main-owned states and unreadable state classes are never delegable.

Each refusal resolves the *effective* actionability back to ``coordinator_actionable`` —
never to the claimed value — so a refused claim can never be narrated as if it verified.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_actionability import (
    ACTIONABILITIES,
    ACTIONABILITY_COORDINATOR_ACTIONABLE,
    ACTIONABILITY_DELEGATED_IN_FLIGHT,
    ACTIONABILITY_NON_ACTIONABLE_WAIT,
    DELEGATED_OWNERS,
    DELIVERY_FAILED,
    DELIVERY_NOT_ATTEMPTED,
    DELIVERY_SENT,
    NEXT_ACTION_OWNERS,
    OWNER_DEDICATED_GATEWAY,
    OWNER_DEDICATED_WORKER,
    OWNER_EXTERNAL_CONDITION,
    OWNER_MAIN_COORDINATOR,
    OWNER_OWNER,
    OWNER_UNKNOWN,
    REASON_CALLBACK_OVERDUE,
    REASON_COORDINATOR_OWNED,
    REASON_DELEGATED_VERIFIED,
    REASON_DELIVERY_FAILED,
    REASON_DELIVERY_NOT_CONFIRMED,
    REASON_MAIN_OWNED_STATE,
    REASON_NO_CALLBACK_EXPECTATION,
    REASON_NO_UNBLOCK_CONDITION,
    REASON_OWNER_NOT_DELEGATED,
    REASON_STATE_NOT_BLOCKING,
    REASON_SURFACE_NOT_VERIFIED,
    REASON_UNKNOWN_ACTIONABILITY,
    REASON_WAIT_OWNER_NOT_EXTERNAL,
    REASON_WAIT_STALLED,
    REASON_WAIT_VERIFIED,
    ActionabilityClaim,
    resolve_actionability,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_execution_surface import (
    DISPATCH_ACK_WORKER_CONFIRMED,
    SURFACE_INTERNAL_TASK_AGENT,
    SURFACE_MANAGED_SUBLANE,
    LaneProvenance,
)

VERIFIED_SUBLANE = LaneProvenance(
    execution_surface=SURFACE_MANAGED_SUBLANE,
    workspace="w19",
    lane="issue_13441_provider_registry",
    issue_generation="1",
    lifecycle_revision="3",
    durable_anchor="13441#77503",
    gateway_identity="w19:p1",
    worker_identity="w19:p2",
    dispatch_ack=DISPATCH_ACK_WORKER_CONFIRMED,
)


def _delegated(**overrides) -> ActionabilityClaim:
    base = dict(
        actionability=ACTIONABILITY_DELEGATED_IN_FLIGHT,
        next_action_owner=OWNER_DEDICATED_GATEWAY,
        delivery_state=DELIVERY_SENT,
        callback_expected=True,
        callback_overdue=False,
    )
    base.update(overrides)
    return ActionabilityClaim(**base)


def _wait(**overrides) -> ActionabilityClaim:
    base = dict(
        actionability=ACTIONABILITY_NON_ACTIONABLE_WAIT,
        next_action_owner=OWNER_EXTERNAL_CONDITION,
        unblock_condition="predecessor lane idle",
    )
    base.update(overrides)
    return ActionabilityClaim(**base)


def _resolve(claim, provenance=VERIFIED_SUBLANE, *, blocking=True, main_owned=False):
    return resolve_actionability(
        claim,
        provenance,
        state_is_coordinator_blocking=blocking,
        state_is_main_owned=main_owned,
    )


class VocabularyTest(unittest.TestCase):
    def test_delegated_owners_exclude_the_main_coordinator_and_the_owner(self):
        # Owner debt is the coordinator's to aggregate; it is not a delegation.
        self.assertNotIn(OWNER_MAIN_COORDINATOR, DELEGATED_OWNERS)
        self.assertNotIn(OWNER_OWNER, DELEGATED_OWNERS)
        self.assertNotIn(OWNER_UNKNOWN, DELEGATED_OWNERS)
        self.assertEqual(
            DELEGATED_OWNERS, {OWNER_DEDICATED_GATEWAY, OWNER_DEDICATED_WORKER}
        )

    def test_vocabularies_are_closed(self):
        self.assertEqual(len(ACTIONABILITIES), 3)
        self.assertLessEqual(DELEGATED_OWNERS, NEXT_ACTION_OWNERS)


class DefaultsTest(unittest.TestCase):
    def test_default_claim_is_the_fail_closed_one(self):
        claim = ActionabilityClaim()
        self.assertEqual(claim.actionability, ACTIONABILITY_COORDINATOR_ACTIONABLE)
        self.assertEqual(claim.next_action_owner, OWNER_MAIN_COORDINATOR)
        self.assertEqual(claim.delivery_state, DELIVERY_NOT_ATTEMPTED)
        self.assertFalse(claim.callback_expected)

    def test_default_claim_blocks_on_a_blocking_state(self):
        verdict = _resolve(ActionabilityClaim())
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_COORDINATOR_OWNED)

    def test_default_claim_does_not_block_on_a_non_blocking_state(self):
        verdict = _resolve(ActionabilityClaim(), blocking=False)
        self.assertFalse(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_STATE_NOT_BLOCKING)


class DelegatedInFlightTest(unittest.TestCase):
    def test_verified_delegation_does_not_block(self):
        verdict = _resolve(_delegated())
        self.assertFalse(verdict.coordinator_blocking)
        self.assertTrue(verdict.delegated_in_flight)
        self.assertEqual(verdict.reason, REASON_DELEGATED_VERIFIED)

    def test_a_dedicated_worker_can_also_own_the_next_action(self):
        verdict = _resolve(_delegated(next_action_owner=OWNER_DEDICATED_WORKER))
        self.assertFalse(verdict.coordinator_blocking)

    def test_undelivered_request_refuses(self):
        verdict = _resolve(_delegated(delivery_state=DELIVERY_NOT_ATTEMPTED))
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_DELIVERY_NOT_CONFIRMED)
        self.assertEqual(
            verdict.actionability, ACTIONABILITY_COORDINATOR_ACTIONABLE
        )

    def test_failed_delivery_refuses(self):
        verdict = _resolve(_delegated(delivery_state=DELIVERY_FAILED))
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_DELIVERY_FAILED)

    def test_unknown_delivery_token_refuses(self):
        verdict = _resolve(_delegated(delivery_state="probably_landed"))
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_DELIVERY_NOT_CONFIRMED)

    def test_missing_callback_expectation_refuses(self):
        # The delivery landed, but nothing is expected back: an ACK is not completion.
        verdict = _resolve(_delegated(callback_expected=False))
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_NO_CALLBACK_EXPECTATION)

    def test_overdue_callback_refuses(self):
        verdict = _resolve(_delegated(callback_overdue=True))
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_CALLBACK_OVERDUE)

    def test_non_delegate_owner_refuses(self):
        for owner in (OWNER_MAIN_COORDINATOR, OWNER_OWNER, OWNER_UNKNOWN, "gateway?"):
            with self.subTest(owner=owner):
                verdict = _resolve(_delegated(next_action_owner=owner))
                self.assertTrue(verdict.coordinator_blocking)
                self.assertEqual(verdict.reason, REASON_OWNER_NOT_DELEGATED)

    def test_unverified_surface_refuses(self):
        verdict = _resolve(
            _delegated(),
            LaneProvenance(execution_surface=SURFACE_INTERNAL_TASK_AGENT),
        )
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_SURFACE_NOT_VERIFIED)

    def test_legacy_lane_with_no_surface_claim_refuses(self):
        # The `--lane ISSUE:STATE` fallback: no surface, so no delegation.
        verdict = _resolve(_delegated(), LaneProvenance())
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_SURFACE_NOT_VERIFIED)


class NonActionableWaitTest(unittest.TestCase):
    def test_verified_external_wait_does_not_block(self):
        verdict = _resolve(_wait())
        self.assertFalse(verdict.coordinator_blocking)
        self.assertTrue(verdict.non_actionable_wait)
        self.assertEqual(verdict.reason, REASON_WAIT_VERIFIED)

    def test_wait_on_a_non_external_owner_refuses(self):
        for owner in (
            OWNER_MAIN_COORDINATOR,
            OWNER_OWNER,
            OWNER_DEDICATED_GATEWAY,
            OWNER_UNKNOWN,
        ):
            with self.subTest(owner=owner):
                verdict = _resolve(_wait(next_action_owner=owner))
                self.assertTrue(verdict.coordinator_blocking)
                self.assertEqual(verdict.reason, REASON_WAIT_OWNER_NOT_EXTERNAL)

    def test_wait_without_a_durable_unblock_condition_refuses(self):
        for condition in ("", "   "):
            with self.subTest(condition=condition):
                verdict = _resolve(_wait(unblock_condition=condition))
                self.assertTrue(verdict.coordinator_blocking)
                self.assertEqual(verdict.reason, REASON_NO_UNBLOCK_CONDITION)

    def test_stalled_wait_refuses(self):
        verdict = _resolve(_wait(callback_overdue=True))
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_WAIT_STALLED)

    def test_wait_from_an_unverified_surface_refuses(self):
        verdict = _resolve(_wait(), LaneProvenance())
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_SURFACE_NOT_VERIFIED)


class FailClosedTest(unittest.TestCase):
    def test_main_owned_state_refuses_every_claim(self):
        for claim in (_delegated(), _wait()):
            with self.subTest(claim=claim.actionability):
                verdict = _resolve(claim, main_owned=True)
                self.assertTrue(verdict.coordinator_blocking)
                self.assertEqual(verdict.reason, REASON_MAIN_OWNED_STATE)
                self.assertEqual(
                    verdict.actionability, ACTIONABILITY_COORDINATOR_ACTIONABLE
                )

    def test_unknown_actionability_token_refuses(self):
        verdict = _resolve(
            ActionabilityClaim(
                actionability="in_flight_probably",
                next_action_owner=OWNER_DEDICATED_GATEWAY,
                delivery_state=DELIVERY_SENT,
                callback_expected=True,
            )
        )
        self.assertTrue(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_UNKNOWN_ACTIONABILITY)

    def test_a_refused_claim_never_keeps_its_claimed_label(self):
        # The projection must not be able to show a refused delegation as delegated.
        for claim in (
            _delegated(delivery_state=DELIVERY_FAILED),
            _delegated(callback_overdue=True),
            _wait(unblock_condition=""),
        ):
            with self.subTest(claim=claim):
                self.assertEqual(
                    _resolve(claim).actionability,
                    ACTIONABILITY_COORDINATOR_ACTIONABLE,
                )

    def test_a_misdeclared_claim_on_a_non_blocking_state_invents_no_stop(self):
        # An `implementing` lane with a broken delegation claim degrades its label, but
        # it must not become a stop reason — that would be a new false stop.
        verdict = _resolve(_delegated(delivery_state=DELIVERY_FAILED), blocking=False)
        self.assertFalse(verdict.coordinator_blocking)
        self.assertEqual(verdict.reason, REASON_DELIVERY_FAILED)


if __name__ == "__main__":
    unittest.main()
