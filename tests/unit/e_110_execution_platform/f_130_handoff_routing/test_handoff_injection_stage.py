"""Contract + drift guard for the shared injection-stage authority (Redmine #14232).

``domain/injection_stage.py`` answers exactly one question for the whole handoff surface:
**may a blind retry duplicate this payload?** #14232 j#84877 recorded that three readers
answered it separately and inconsistently, so the value of the module is only as good as two
properties, both pinned here:

- the ``(status, reason)`` -> stage mapping itself (the truth table), and
- **exhaustiveness**: the blocked-reason partition covers the whole ``Reason`` wire vocabulary,
  so a newly added reason cannot be *forgotten* into a bucket by default. That guard is the
  point — the two reasons the old private table had silently omitted
  (``reader_upgrade_required`` / ``execution_root_outside_target_repo``) were both added by
  later issues that had no reason to know a second table existed.

These are module-contract assertions, not recurrence pins, so they live in ``tests/unit``; the
#14232 recurrence pins are in
``tests/regressions/test_issue_14232_handoff_partial_delivery_outcome.py``.
"""
from __future__ import annotations

import typing
import unittest

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    Reason,
    make_outcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    INJECTION_STAGES,
    NON_BLOCKED_REASONS,
    POST_INJECTION_BLOCKED_REASONS,
    PRE_INJECTION_BLOCKED_REASONS,
    REASON_TRANSPORT_ERROR,
    STAGE_NOT_SENT,
    STAGE_SUBMITTED_CONFIRMED,
    STAGE_UNCERTAIN_PARTIAL,
    blind_retry_prohibited,
    injection_stage_for,
    injection_stage_record_lines,
    injection_stage_telemetry,
    stage_from_telemetry,
    stage_guidance,
)


class ReasonVocabularyPartitionTest(unittest.TestCase):
    """The partition is an exhaustive, disjoint split of the ``Reason`` wire vocabulary."""

    def setUp(self) -> None:
        self.reasons = frozenset(typing.get_args(Reason))

    def test_partition_covers_every_reason(self):
        classified = (
            NON_BLOCKED_REASONS
            | PRE_INJECTION_BLOCKED_REASONS
            | POST_INJECTION_BLOCKED_REASONS
        )
        self.assertEqual(
            self.reasons - classified,
            frozenset(),
            "a handoff Reason is unclassified: add it to exactly one of "
            "NON_BLOCKED_REASONS / PRE_INJECTION_BLOCKED_REASONS / "
            "POST_INJECTION_BLOCKED_REASONS after deciding whether a blind retry could "
            "duplicate it",
        )

    def test_partition_names_no_reason_outside_the_wire_vocabulary(self):
        classified = (
            NON_BLOCKED_REASONS
            | PRE_INJECTION_BLOCKED_REASONS
            | POST_INJECTION_BLOCKED_REASONS
        )
        self.assertEqual(
            classified - self.reasons,
            frozenset(),
            "the partition classifies a token the handoff wire cannot emit (renamed / removed?)",
        )

    def test_the_three_buckets_are_pairwise_disjoint(self):
        for left, right in (
            (NON_BLOCKED_REASONS, PRE_INJECTION_BLOCKED_REASONS),
            (NON_BLOCKED_REASONS, POST_INJECTION_BLOCKED_REASONS),
            (PRE_INJECTION_BLOCKED_REASONS, POST_INJECTION_BLOCKED_REASONS),
        ):
            with self.subTest(left=sorted(left)[:1], right=sorted(right)[:1]):
                self.assertEqual(left & right, frozenset())

    def test_transport_error_is_a_wire_reason_and_post_injection(self):
        self.assertIn(REASON_TRANSPORT_ERROR, self.reasons)
        self.assertIn(REASON_TRANSPORT_ERROR, POST_INJECTION_BLOCKED_REASONS)


class InjectionStageTruthTableTest(unittest.TestCase):
    """The ``(status, reason)`` -> stage mapping."""

    def test_only_sent_ok_is_a_confirmed_submission(self):
        self.assertEqual(
            injection_stage_for("sent", "ok"), STAGE_SUBMITTED_CONFIRMED
        )
        # Every other cell must NOT be confirmed, including the relaxed rail's `sent`.
        for status, reason in (
            ("sent", "queue_enter"),
            ("pending_input", "ok"),
            ("blocked", "marker_timeout"),
            ("blocked", "invalid_args"),
        ):
            with self.subTest(status=status, reason=reason):
                self.assertNotEqual(
                    injection_stage_for(status, reason), STAGE_SUBMITTED_CONFIRMED
                )

    def test_pre_injection_blocked_reasons_are_not_sent(self):
        for reason in sorted(PRE_INJECTION_BLOCKED_REASONS):
            with self.subTest(reason=reason):
                self.assertEqual(
                    injection_stage_for("blocked", reason), STAGE_NOT_SENT
                )

    def test_post_injection_blocked_reasons_are_uncertain_partial(self):
        for reason in sorted(POST_INJECTION_BLOCKED_REASONS):
            with self.subTest(reason=reason):
                self.assertEqual(
                    injection_stage_for("blocked", reason), STAGE_UNCERTAIN_PARTIAL
                )

    def test_pending_input_and_marker_unobserved_queue_enter_are_uncertain_partial(self):
        self.assertEqual(
            injection_stage_for("pending_input", "ok"), STAGE_UNCERTAIN_PARTIAL
        )
        self.assertEqual(
            injection_stage_for("sent", "queue_enter"), STAGE_UNCERTAIN_PARTIAL
        )

    def test_a_pre_injection_reason_on_a_non_blocked_status_is_not_not_sent(self):
        """The mapping keys on ``(status, reason)``, not on the reason alone.

        ``ok`` is carried by both ``sent`` (confirmed) and ``pending_input`` (parked), so a
        reason-only classifier would collapse them. A pre-injection reason arriving on a
        non-``blocked`` status is incoherent input and must fail closed, not claim zero-send.
        """
        self.assertEqual(
            injection_stage_for("sent", "invalid_args"), STAGE_UNCERTAIN_PARTIAL
        )

    def test_unrecognised_input_fails_closed_to_uncertain_partial(self):
        for status, reason in (
            ("weird", "weird"),
            ("blocked", "a_reason_that_does_not_exist"),
            (None, None),
            ("", ""),
            (object(), object()),
        ):
            with self.subTest(status=status, reason=reason):
                self.assertEqual(
                    injection_stage_for(status, reason), STAGE_UNCERTAIN_PARTIAL
                )

    def test_whitespace_is_stripped_before_classification(self):
        self.assertEqual(injection_stage_for(" sent ", " ok "), STAGE_SUBMITTED_CONFIRMED)


class BlindRetryPredicateTest(unittest.TestCase):
    def test_only_not_sent_permits_a_blind_retry(self):
        self.assertFalse(blind_retry_prohibited(STAGE_NOT_SENT))
        self.assertTrue(blind_retry_prohibited(STAGE_UNCERTAIN_PARTIAL))
        self.assertTrue(
            blind_retry_prohibited(STAGE_SUBMITTED_CONFIRMED),
            "re-issuing a confirmed submission duplicates it; the predicate answers "
            "'may I resend without re-reading?', not 'is there work left'",
        )

    def test_an_unknown_stage_token_prohibits_a_blind_retry(self):
        for token in ("", None, "not_a_stage", object()):
            with self.subTest(token=token):
                self.assertTrue(blind_retry_prohibited(token))


class StageTelemetryTest(unittest.TestCase):
    def test_every_stage_has_guidance(self):
        for stage in INJECTION_STAGES:
            with self.subTest(stage=stage):
                self.assertTrue(stage_guidance(stage))

    def test_unknown_stage_has_no_guidance(self):
        self.assertEqual(stage_guidance("not_a_stage"), "")

    def test_telemetry_carries_stage_flag_and_guidance(self):
        telemetry = injection_stage_telemetry("blocked", REASON_TRANSPORT_ERROR)
        self.assertEqual(telemetry["stage"], STAGE_UNCERTAIN_PARTIAL)
        self.assertTrue(telemetry["blind_retry_prohibited"])
        self.assertTrue(telemetry["next_action"])
        self.assertEqual(
            set(telemetry), {"stage", "blind_retry_prohibited", "next_action"}
        )

    def test_stage_round_trips_through_the_telemetry_mapping(self):
        for status, reason in (("sent", "ok"), ("blocked", "invalid_args"), ("x", "y")):
            with self.subTest(status=status, reason=reason):
                telemetry = injection_stage_telemetry(status, reason)
                self.assertEqual(
                    stage_from_telemetry(telemetry), injection_stage_for(status, reason)
                )

    def test_stage_from_telemetry_is_none_for_absent_or_bogus_input(self):
        for value in (None, {}, {"stage": "not_a_stage"}, {"stage": 3}, "sent"):
            with self.subTest(value=value):
                self.assertIsNone(stage_from_telemetry(value))

    def test_record_lines_render_for_a_known_stage_and_are_empty_otherwise(self):
        lines = injection_stage_record_lines(
            injection_stage_telemetry("blocked", "turn_start_unconfirmed")
        )
        self.assertEqual(len(lines), 1)
        self.assertIn(STAGE_UNCERTAIN_PARTIAL, lines[0])
        self.assertIn("blind retry PROHIBITED", lines[0])
        self.assertEqual(injection_stage_record_lines(None), [])
        self.assertEqual(injection_stage_record_lines({"stage": "bogus"}), [])

    def test_not_sent_record_line_says_retry_is_safe(self):
        lines = injection_stage_record_lines(
            injection_stage_telemetry("blocked", "invalid_args")
        )
        self.assertIn("retry safe", lines[0])


class MakeOutcomeCarriesTheStageTest(unittest.TestCase):
    """``make_outcome`` derives the projection for EVERY terminal path.

    Deriving it in the one factory (rather than at each terminal ``emit``) is what makes it
    impossible for a newly added terminal path to ship a delivery record whose retry safety a
    reader has to re-derive — the same posture the #13583 delivery-outcome gate adopted after
    hand-picked publish sites were missed.
    """

    def _built(self, status: str, reason: str):
        return make_outcome(
            status=status,
            reason=reason,
            receiver="codex",
            target="%7",
            anchor=None,
            mode="queue-enter",
            kind="reply",
            notification_marker=None,
            source="redmine",
        )

    def test_stage_is_present_on_sent_pending_and_blocked_outcomes(self):
        for status, reason, expected in (
            ("sent", "ok", STAGE_SUBMITTED_CONFIRMED),
            ("sent", "queue_enter", STAGE_UNCERTAIN_PARTIAL),
            ("pending_input", "ok", STAGE_UNCERTAIN_PARTIAL),
            ("blocked", "invalid_args", STAGE_NOT_SENT),
            ("blocked", REASON_TRANSPORT_ERROR, STAGE_UNCERTAIN_PARTIAL),
        ):
            with self.subTest(status=status, reason=reason):
                outcome = self._built(status, reason)
                self.assertEqual(outcome.injection_stage["stage"], expected)
                self.assertEqual(
                    outcome.injection_stage["blind_retry_prohibited"],
                    blind_retry_prohibited(expected),
                )

    def test_stage_survives_the_json_projection(self):
        payload = self._built("blocked", REASON_TRANSPORT_ERROR).to_dict()
        self.assertEqual(
            payload["injection_stage"]["stage"], STAGE_UNCERTAIN_PARTIAL
        )


class TransportErrorWordingTest(unittest.TestCase):
    """The additive ``transport_error`` reason renders complete, secret-safe wording."""

    def _built(self):
        return make_outcome(
            status="blocked",
            reason=REASON_TRANSPORT_ERROR,
            receiver="codex",
            target="%7",
            anchor=None,
            mode="queue-enter",
            kind="reply",
            notification_marker="[m]",
            source="redmine",
        )

    def test_next_action_is_owned_by_the_sender_and_forbids_a_blind_resend(self):
        outcome = self._built()
        self.assertEqual(outcome.next_action_owner, "sender")
        self.assertIn("blind", outcome.next_action.lower())

    def test_wording_is_not_the_generic_fallback(self):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            _outcome_narrative,
            next_action_for,
        )

        self.assertNotEqual(
            next_action_for("blocked", REASON_TRANSPORT_ERROR, "codex")[1],
            "inspect handoff failure and decide the next step",
        )
        self.assertNotEqual(
            _outcome_narrative("blocked", REASON_TRANSPORT_ERROR, "queue-enter", "codex"),
            "Handoff did not deliver; see structured outcome for details.",
        )

    def test_receiver_contract_names_the_receiver(self):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            _receiver_contract_line,
        )

        line = _receiver_contract_line("blocked", REASON_TRANSPORT_ERROR, "codex")
        self.assertIsNotNone(line)
        self.assertIn("codex", line)


if __name__ == "__main__":  # pragma: no cover - manual runner parity
    unittest.main()
