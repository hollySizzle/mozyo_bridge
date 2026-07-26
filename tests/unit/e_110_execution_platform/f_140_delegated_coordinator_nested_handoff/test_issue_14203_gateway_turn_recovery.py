"""Unit: gateway provider-turn classification + refresh decision domain (Redmine #14203).

Pins the pure half of ``sublane recover-gateway``: the closed turn-classification vocabulary
(the durable journal is the authority; an unconfirmed delivery / turn start is NEVER a
failure), the secret-safe fail-closed reason normalization, the ordered fail-closed refresh
gates (the worker / default coordinator / foreign slot are protected), and the exact action
id. No process, no DB, no I/O.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    GatewayRefreshObservation,
    GatewayTurnObservation,
    REFRESH_ACTIONABLE,
    REFRESH_BLOCK_AUTHORITY_CONFLICT,
    REFRESH_BLOCK_NO_RESUME_ANCHOR,
    REFRESH_BLOCK_NON_GATEWAY,
    REFRESH_BLOCK_NOT_SETTLED,
    REFRESH_BLOCK_PENDING_COMPOSER,
    REFRESH_BLOCK_STALE_GENERATION,
    REFRESH_BLOCK_TURN_NOT_FAILED,
    REFRESH_BLOCK_UNKNOWN,
    REFRESH_BLOCK_WORKER_NOT_DISTINGUISHED,
    REFRESH_BLOCK_WRONG_ISSUE_LANE,
    REFRESH_BLOCKERS,
    REFRESH_VERDICTS,
    RESUMABLE_GATES,
    TURN_CLASS_FAILED,
    TURN_CLASS_NOT_SETTLED,
    TURN_CLASS_PRODUCTIVE,
    TURN_CLASS_UNCONFIRMED,
    TURN_CLASS_UNOBSERVABLE,
    TURN_CLASSES,
    TURN_FAILURE_REASONS,
    TURN_REASON_AUTH,
    TURN_REASON_RATE_LIMIT,
    TURN_REASON_SESSION_STALE,
    TURN_REASON_UNKNOWN,
    classify_gateway_turn,
    decide_gateway_refresh,
    gateway_refresh_action_id,
    is_refresh_actionable,
    normalize_turn_failure_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
    stale_worker_recovery_action_id,
)


def _turn(**overrides) -> GatewayTurnObservation:
    facts = dict(
        delivery_confirmed=True,
        turn_started=True,
        settled_turn_ended=True,
        expected_gate_landed=False,
        expected_gate_absent=True,
        durable_source_fresh=True,
    )
    facts.update(overrides)
    return GatewayTurnObservation(**facts)


def _refresh(**overrides) -> GatewayRefreshObservation:
    facts = dict(
        identity_resolved=True,
        is_lane_implementation_gateway=True,
        issue_lane_matches=True,
        generation_matches=True,
        settled_idle=True,
        composer_clear=True,
        resume_anchor_present=True,
        worker_distinct_preserved=True,
        no_authority_conflict=True,
    )
    facts.update(overrides)
    return GatewayRefreshObservation(**facts)


class TurnClassificationTests(unittest.TestCase):
    def test_all_defaults_fail_closed_to_unobservable(self):
        # Every field defaults to the unsafe side: a wholly-missing observation can never
        # classify as a failure (which would justify a destructive refresh).
        self.assertEqual(
            classify_gateway_turn(GatewayTurnObservation()), TURN_CLASS_UNOBSERVABLE
        )

    def test_a_landed_gate_is_productive_regardless_of_runtime_appearance(self):
        # The durable journal is the authority: even with NOTHING else confirmed (no
        # delivery confirmation, no turn start, unsettled runtime), a landed gate is
        # productive — never a failure, never unobservable.
        obs = GatewayTurnObservation(expected_gate_landed=True)
        self.assertEqual(classify_gateway_turn(obs), TURN_CLASS_PRODUCTIVE)

    def test_a_landed_and_absent_contradiction_is_unobservable(self):
        obs = _turn(expected_gate_landed=True, expected_gate_absent=True)
        self.assertEqual(classify_gateway_turn(obs), TURN_CLASS_UNOBSERVABLE)

    def test_unconfirmed_absence_is_unobservable_not_absent(self):
        # A source that could not positively confirm absence leaves expected_gate_absent
        # False — that is UNOBSERVABLE, never treated as "no gate landed".
        obs = _turn(expected_gate_absent=False)
        self.assertEqual(classify_gateway_turn(obs), TURN_CLASS_UNOBSERVABLE)

    def test_a_snapshot_source_cannot_assert_absence(self):
        # #13889: a frozen snapshot's re-read is a no-op guard. Absence asserted from a
        # non-fresh source is unobservable.
        obs = _turn(durable_source_fresh=False)
        self.assertEqual(classify_gateway_turn(obs), TURN_CLASS_UNOBSERVABLE)

    def test_an_unconfirmed_delivery_is_never_a_failure(self):
        # #14219 dogfood: two consecutive delivered_not_started reports were BOTH real
        # landings. An unconfirmed callback outcome must classify unconfirmed, not failed.
        obs = _turn(delivery_confirmed=False)
        self.assertEqual(classify_gateway_turn(obs), TURN_CLASS_UNCONFIRMED)

    def test_an_unconfirmed_turn_start_is_never_a_failure(self):
        obs = _turn(turn_started=False)
        self.assertEqual(classify_gateway_turn(obs), TURN_CLASS_UNCONFIRMED)

    def test_an_unsettled_runtime_is_not_settled_not_failed(self):
        obs = _turn(settled_turn_ended=False)
        self.assertEqual(classify_gateway_turn(obs), TURN_CLASS_NOT_SETTLED)

    def test_the_fully_confirmed_remainder_is_failed(self):
        self.assertEqual(classify_gateway_turn(_turn()), TURN_CLASS_FAILED)

    def test_the_class_vocabulary_is_closed(self):
        self.assertEqual(
            TURN_CLASSES,
            {
                TURN_CLASS_PRODUCTIVE, TURN_CLASS_FAILED, TURN_CLASS_UNCONFIRMED,
                TURN_CLASS_NOT_SETTLED, TURN_CLASS_UNOBSERVABLE,
            },
        )


class TurnReasonTests(unittest.TestCase):
    def test_exact_members_pass_through(self):
        for token in (TURN_REASON_RATE_LIMIT, TURN_REASON_AUTH, TURN_REASON_SESSION_STALE):
            self.assertEqual(normalize_turn_failure_reason(token), token)

    def test_everything_else_collapses_to_unknown_fail_closed(self):
        # Free text, raw provider error strings, casing variants, and empty all collapse —
        # a secret-bearing evidence string can never pass through this normalization.
        for raw in (
            "", "RATE_LIMIT", "Rate_Limit", "429 Too Many Requests",
            "auth failed: token sk-abc123", "session expired", "rate limit",
        ):
            self.assertEqual(
                normalize_turn_failure_reason(raw), TURN_REASON_UNKNOWN, raw
            )

    def test_the_reason_vocabulary_is_closed_and_contains_unknown(self):
        self.assertIn(TURN_REASON_UNKNOWN, TURN_FAILURE_REASONS)
        self.assertEqual(len(TURN_FAILURE_REASONS), 4)

    def test_payload_carries_the_normalized_reason_never_the_raw_token(self):
        obs = GatewayTurnObservation(reason_token="429 Too Many Requests sk-secret")
        payload = obs.as_payload()
        self.assertEqual(payload["reason"], TURN_REASON_UNKNOWN)
        self.assertNotIn("reason_token", payload)
        self.assertNotIn("sk-secret", str(payload))


class RefreshDecisionTests(unittest.TestCase):
    def test_all_positive_with_a_failed_turn_is_actionable(self):
        verdict = decide_gateway_refresh(_refresh(), TURN_CLASS_FAILED)
        self.assertEqual(verdict, REFRESH_ACTIONABLE)
        self.assertTrue(is_refresh_actionable(verdict))

    def test_all_defaults_fail_closed_to_identity_unknown(self):
        self.assertEqual(
            decide_gateway_refresh(GatewayRefreshObservation(), TURN_CLASS_FAILED),
            REFRESH_BLOCK_UNKNOWN,
        )

    def test_each_single_fact_off_names_its_exact_blocker(self):
        cases = {
            "identity_resolved": REFRESH_BLOCK_UNKNOWN,
            "is_lane_implementation_gateway": REFRESH_BLOCK_NON_GATEWAY,
            "issue_lane_matches": REFRESH_BLOCK_WRONG_ISSUE_LANE,
            "generation_matches": REFRESH_BLOCK_STALE_GENERATION,
            "settled_idle": REFRESH_BLOCK_NOT_SETTLED,
            "composer_clear": REFRESH_BLOCK_PENDING_COMPOSER,
            "resume_anchor_present": REFRESH_BLOCK_NO_RESUME_ANCHOR,
            "worker_distinct_preserved": REFRESH_BLOCK_WORKER_NOT_DISTINGUISHED,
            "no_authority_conflict": REFRESH_BLOCK_AUTHORITY_CONFLICT,
        }
        for field, blocker in cases.items():
            with self.subTest(field=field):
                obs = _refresh(**{field: False})
                self.assertEqual(
                    decide_gateway_refresh(obs, TURN_CLASS_FAILED), blocker
                )

    def test_every_non_failed_turn_class_blocks_the_refresh(self):
        # A productive / unconfirmed / unsettled / unobservable turn NEVER justifies a
        # close — even with every slot fact positive.
        for turn_class in (
            TURN_CLASS_PRODUCTIVE, TURN_CLASS_UNCONFIRMED,
            TURN_CLASS_NOT_SETTLED, TURN_CLASS_UNOBSERVABLE, "", "bogus",
        ):
            with self.subTest(turn_class=turn_class):
                self.assertEqual(
                    decide_gateway_refresh(_refresh(), turn_class),
                    REFRESH_BLOCK_TURN_NOT_FAILED,
                )

    def test_the_worker_protection_gate_fires_before_the_runtime_gates(self):
        # A worker-shaped slot (not the gateway) with every later fact ALSO off must name
        # the protection blocker, not a later one — the ordered most-fundamental-first rule.
        obs = _refresh(
            is_lane_implementation_gateway=False, settled_idle=False, composer_clear=False,
        )
        self.assertEqual(
            decide_gateway_refresh(obs, TURN_CLASS_FAILED), REFRESH_BLOCK_NON_GATEWAY
        )

    def test_the_verdict_vocabulary_is_closed(self):
        self.assertEqual(len(REFRESH_VERDICTS), 11)
        self.assertEqual(REFRESH_BLOCKERS, REFRESH_VERDICTS - {REFRESH_ACTIONABLE})


class ActionIdTests(unittest.TestCase):
    def test_the_exact_id_shape_pins_the_row_revision(self):
        # Review j#87364 F5: the live inventory row revision is a REQUIRED authority
        # component — a recycled generation derives a DIFFERENT transaction key.
        self.assertEqual(
            gateway_refresh_action_id(
                lane_id="l", role="codex", provider="codex", assigned_name="gw",
                locator="w:3", revision="4",
            ),
            "refresh-gateway:l:codex:codex:gw:w:3:r4",
        )
        self.assertNotEqual(
            gateway_refresh_action_id(
                lane_id="l", role="codex", provider="codex", assigned_name="gw",
                locator="w:3", revision="4",
            ),
            gateway_refresh_action_id(
                lane_id="l", role="codex", provider="codex", assigned_name="gw",
                locator="w:3", revision="5",
            ),
        )

    def test_a_missing_component_raises(self):
        for missing in (
            "lane_id", "role", "provider", "assigned_name", "locator", "revision",
        ):
            parts = dict(
                lane_id="l", role="codex", provider="codex", assigned_name="gw",
                locator="w:3", revision="4",
            )
            parts[missing] = ""
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError):
                    gateway_refresh_action_id(**parts)

    def test_never_collides_with_a_worker_recovery_key(self):
        # The same slot-shape must yield DIFFERENT transaction action ids for a gateway
        # refresh vs a stale-worker recovery — the two authorities never share a key.
        self.assertNotEqual(
            gateway_refresh_action_id(
                lane_id="l", role="codex", provider="codex", assigned_name="gw",
                locator="w:3", revision="4",
            ),
            stale_worker_recovery_action_id(
                lane_id="l", role="codex", provider="codex", assigned_name="gw",
                locator="w:3",
            ),
        )


class ResumeVocabularyTests(unittest.TestCase):
    def test_resumable_gates_are_the_governed_handoff_kinds(self):
        self.assertEqual(
            RESUMABLE_GATES,
            {
                "custom", "design_consultation", "implementation_done",
                "implementation_request", "reply", "review_request", "review_result",
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
