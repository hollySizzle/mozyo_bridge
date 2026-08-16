"""Unit tests for the pure q-enter front-door brain (Redmine #12705).

Pins the three properties the issue requires of the LLM-facing submit primitive:
the anchor requirement is owned by the CLI (fail-closed, not LLM judgment), the
composer residue is one unambiguous state, and the delivery id is deterministic for
duplicate prevention.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain import (
    q_enter,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.q_enter import (
    RAIL_ANCHORED_REPLY,
    RAIL_ANCHORED_SEND,
    RAIL_TICKETLESS_CALLBACK,
    RESIDUE_CLEARED,
    RESIDUE_NOT_TYPED,
    RESIDUE_TYPED_BUT_PENDING,
    RESIDUE_UNSAFE_REQUIRES_FRESH_RECEIVER,
    SubmitOutcome,
    SubmitPlanError,
    classify_composer_residue,
    derive_delivery_id,
    resolve_submit_plan,
    submit_record_lines,
)


def _outcome(status, reason, *, mode="queue-enter", **extra):
    """A real ``DeliveryOutcome`` for the front-door derivation (review j#95333 F1).

    ``from_transport`` reads the whole outcome now, so these tests build one through
    ``make_outcome`` — the same producer production uses — instead of passing two tokens the
    authority cannot classify on their own.
    """
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
        make_outcome,
    )

    return make_outcome(
        status=status, reason=reason, receiver="codex", target="%2", anchor=None,
        mode=mode, kind="reply", notification_marker="[m]", source="redmine", **extra
    )


def _canonical_binding(*, action_id: str, observed_at: str) -> dict[str, str]:
    assigned_name = "mzb1_ws_codex_lane"
    terminal_id = "terminal-q-enter"
    locator = "%2"
    revision = "1"
    return {
        "provider": "codex",
        "assigned_name": assigned_name,
        "locator": locator,
        "row_revision": revision,
        "attestation_observed_at": observed_at,
        "startup_action_id": action_id,
    }


class ResolveSubmitPlanTest(unittest.TestCase):
    def test_consultation_callback_resolves_no_anchor_ticketless_rail(self) -> None:
        plan = resolve_submit_plan("consultation_callback")
        self.assertEqual(RAIL_TICKETLESS_CALLBACK, plan.rail)
        self.assertFalse(plan.anchor_required)
        self.assertTrue(plan.ticketless)
        self.assertIsNone(plan.source)

    def test_consultation_callback_rejects_stray_source(self) -> None:
        # A ticketless callback never carries a source/anchor; offering one is a
        # category error the front door names instead of silently ignoring.
        with self.assertRaises(SubmitPlanError):
            resolve_submit_plan("consultation_callback", source="redmine")

    def test_consultation_callback_rejects_each_stray_anchor_field(self) -> None:
        # review j#67184: failing closed only on --source let an LLM pass
        # --issue/--journal/--task-id without --source, which the ticketless rail
        # then silently ignored. Every anchor field must fail closed.
        for field in ("issue", "journal", "task", "comment", "anchor_url"):
            with self.subTest(field=field):
                with self.assertRaises(SubmitPlanError) as ctx:
                    resolve_submit_plan(
                        "consultation_callback", **{field: True}
                    )
                # The error names the stray flag so the next action is unambiguous.
                self.assertIn("no ticket anchor", str(ctx.exception).lower())

    def test_consultation_callback_rejects_issue_without_source(self) -> None:
        # The exact reviewer reproduction: --issue/--journal with no --source.
        with self.assertRaises(SubmitPlanError):
            resolve_submit_plan(
                "consultation_callback", issue=True, journal=True
            )

    def test_consultation_callback_clean_still_resolves(self) -> None:
        # No anchor field at all still resolves to the ticketless rail.
        plan = resolve_submit_plan("consultation_callback")
        self.assertEqual(RAIL_TICKETLESS_CALLBACK, plan.rail)

    def test_worker_dispatch_with_redmine_anchor_resolves_anchored_send(self) -> None:
        plan = resolve_submit_plan(
            "worker_dispatch",
            source="redmine",
            issue=True,
            journal=True,
            kind="implementation_request",
        )
        self.assertEqual(RAIL_ANCHORED_SEND, plan.rail)
        self.assertTrue(plan.anchor_required)
        self.assertFalse(plan.ticketless)
        self.assertEqual("implementation_request", plan.default_kind)

    def test_worker_dispatch_without_anchor_fails_closed(self) -> None:
        # The Redmine-governed worker-dispatch anchor requirement is not relaxed.
        with self.assertRaises(SubmitPlanError) as ctx:
            resolve_submit_plan("worker_dispatch", source="redmine", issue=True)
        # The error points the LLM at the no-anchor rail instead of leaving it to
        # rediscover invalid_anchor by trial.
        self.assertIn("consultation_callback", str(ctx.exception))

    def test_reply_without_source_fails_closed(self) -> None:
        with self.assertRaises(SubmitPlanError):
            resolve_submit_plan("reply")

    def test_reply_with_anchor_defaults_kind_reply(self) -> None:
        plan = resolve_submit_plan(
            "reply", source="redmine", issue=True, journal=True
        )
        self.assertEqual(RAIL_ANCHORED_REPLY, plan.rail)
        self.assertEqual("reply", plan.default_kind)

    def test_asana_worker_dispatch_accepts_task_plus_comment(self) -> None:
        plan = resolve_submit_plan(
            "worker_dispatch",
            source="asana",
            task=True,
            comment=True,
            kind="implementation_request",
        )
        self.assertEqual(RAIL_ANCHORED_SEND, plan.rail)

    def test_asana_worker_dispatch_without_comment_or_url_fails_closed(self) -> None:
        with self.assertRaises(SubmitPlanError):
            resolve_submit_plan(
                "worker_dispatch", source="asana", task=True, kind="x"
            )

    def test_unknown_intent_fails_closed(self) -> None:
        with self.assertRaises(SubmitPlanError):
            resolve_submit_plan("submit_everything")


class ComposerResidueTest(unittest.TestCase):
    def test_sent_ok_is_cleared(self) -> None:
        self.assertEqual(RESIDUE_CLEARED, classify_composer_residue("sent", "ok"))

    def test_sent_queue_enter_is_typed_but_pending(self) -> None:
        self.assertEqual(
            RESIDUE_TYPED_BUT_PENDING,
            classify_composer_residue("sent", "queue_enter"),
        )

    def test_pending_input_is_typed_but_pending(self) -> None:
        self.assertEqual(
            RESIDUE_TYPED_BUT_PENDING,
            classify_composer_residue("pending_input", "ok"),
        )

    def test_marker_timeout_is_unsafe_state(self) -> None:
        # j#66977: after a C-u rollback whose effect is not verifiable from tmux,
        # the only safe read is a fresh receiver is required.
        self.assertEqual(
            RESIDUE_UNSAFE_REQUIRES_FRESH_RECEIVER,
            classify_composer_residue("blocked", "marker_timeout"),
        )

    def test_blocked_before_typing_is_not_typed(self) -> None:
        for reason in ("invalid_anchor", "invalid_args", "target_unavailable"):
            self.assertEqual(
                RESIDUE_NOT_TYPED, classify_composer_residue("blocked", reason)
            )

    def test_every_classification_is_a_known_state(self) -> None:
        self.assertIn(
            classify_composer_residue("sent", "ok"),
            q_enter.COMPOSER_RESIDUE_STATES,
        )


class DeliveryIdTest(unittest.TestCase):
    def test_same_payload_yields_same_id(self) -> None:
        kwargs = dict(
            intent="reply",
            receiver="codex",
            source="redmine",
            issue="12705",
            journal="67162",
        )
        self.assertEqual(derive_delivery_id(**kwargs), derive_delivery_id(**kwargs))

    def test_different_payload_yields_different_id(self) -> None:
        a = derive_delivery_id(intent="reply", receiver="codex", issue="1")
        b = derive_delivery_id(intent="reply", receiver="codex", issue="2")
        self.assertNotEqual(a, b)

    def test_id_is_prefixed_and_stable_shape(self) -> None:
        did = derive_delivery_id(intent="consultation_callback", receiver="codex")
        self.assertTrue(did.startswith("qe-"))
        self.assertEqual(len("qe-") + 16, len(did))


class SubmitRecordLinesTest(unittest.TestCase):
    def test_record_lines_carry_residue_and_delivery_id(self) -> None:
        lines = submit_record_lines(
            status="sent", reason="queue_enter", intent="reply", delivery_id="qe-abc"
        )
        blob = "\n".join(lines)
        self.assertIn("typed_but_pending", blob)
        self.assertIn("qe-abc", blob)
        self.assertIn("Duplicate prevention", blob)


class SubmitOutcomeTest(unittest.TestCase):
    def test_blocked_outcome_serializes_guidance(self) -> None:
        outcome = SubmitOutcome(
            intent="reply",
            resolved_rail=None,
            anchor_required=True,
            ticketless=False,
            delivery_id="qe-abc",
            dispatched=False,
            blocked=True,
            blocked_reason="anchor_required",
            guidance="provide --issue and --journal",
        )
        data = outcome.to_dict()
        self.assertTrue(data["q_enter"])
        self.assertTrue(data["blocked"])
        self.assertFalse(data["dispatched"])
        self.assertEqual("anchor_required", data["blocked_reason"])
        self.assertIn("anchor_required", "\n".join(outcome.record_lines()))

    def test_blocked_outcome_reports_nothing_was_attempted(self) -> None:
        # Redmine #14232: the front door's OWN fail-closed path never resolved a rail, which is
        # a stronger statement than a transport `not_sent` — no injection stage exists at all.
        outcome = SubmitOutcome(
            intent="reply",
            resolved_rail=None,
            anchor_required=True,
            ticketless=False,
            delivery_id="qe-abc",
            dispatched=False,
            blocked=True,
            blocked_reason="anchor_required",
        )
        data = outcome.to_dict()
        self.assertFalse(data["resolved"])
        self.assertIsNone(data["injection_stage"])
        self.assertFalse(data["blind_retry_prohibited"])

    def test_confirmed_delivery_reports_resolved_and_dispatched(self) -> None:
        # Redmine #14232: built via `from_transport`, the only path that may set `dispatched` —
        # it is derived from the transport outcome, not from plan success. Review j#95333 F1:
        # it takes the whole outcome, because `sent`/`ok` alone cannot say which rail verified
        # what; this one is a `standard` send, which really did confirm a turn start.
        outcome = SubmitOutcome.from_transport(
            _outcome("sent", "ok", mode="standard"),
            plan_intent="consultation_callback",
            rail=RAIL_TICKETLESS_CALLBACK,
            anchor_required=False,
            ticketless=True,
            delivery_id="qe-abc",
        )
        data = outcome.to_dict()
        self.assertTrue(data["resolved"])
        self.assertTrue(data["dispatched"])
        self.assertFalse(data["blocked"])
        self.assertEqual("submitted_confirmed", data["injection_stage"])
        self.assertIn("transport outcome", "\n".join(outcome.record_lines()))

    def test_unconfirmed_transport_reports_resolved_but_not_dispatched(self) -> None:
        """The #14232 defect shape: plan success must not read as delivery success."""
        for status, reason, stage in (
            ("sent", "queue_enter", "uncertain_partial"),
            ("blocked", "turn_start_unconfirmed", "uncertain_partial"),
            ("blocked", "transport_error", "uncertain_partial"),
            ("blocked", "invalid_args", "not_sent"),
        ):
            with self.subTest(status=status, reason=reason):
                outcome = SubmitOutcome.from_transport(
                    _outcome(status, reason),
                    plan_intent="worker_dispatch",
                    rail=RAIL_ANCHORED_SEND,
                    anchor_required=True,
                    ticketless=False,
                    delivery_id="qe-abc",
                )
                self.assertTrue(outcome.resolved)
                self.assertFalse(outcome.dispatched)
                self.assertTrue(outcome.blocked)
                self.assertEqual(stage, outcome.injection_stage)
                self.assertEqual(reason, outcome.blocked_reason)
                self.assertEqual(
                    stage != "not_sent", outcome.blind_retry_prohibited
                )

    def test_a_deliberate_pending_park_is_not_blocked(self) -> None:
        """Review j#95333 F2: `--mode pending` asked the rail NOT to submit.

        Getting exactly what you asked for is not a block — and because the front-door exit
        code is now derived from `blocked`, calling it one would make a documented operator
        path exit non-zero. It is still not `dispatched`, and a blind resend is still
        prohibited: the body IS parked in the receiver's composer.
        """
        outcome = SubmitOutcome.from_transport(
            _outcome("pending_input", "ok", mode="pending"),
            plan_intent="worker_dispatch",
            rail=RAIL_ANCHORED_SEND,
            anchor_required=True,
            ticketless=False,
            delivery_id="qe-abc",
        )
        self.assertTrue(outcome.resolved)
        self.assertFalse(outcome.blocked)
        self.assertFalse(outcome.dispatched)
        self.assertIsNone(outcome.blocked_reason)
        self.assertEqual("uncertain_partial", outcome.injection_stage)
        self.assertTrue(outcome.blind_retry_prohibited)

    def test_marker_observed_queue_enter_is_confirmed_only_by_a_causal_start(self) -> None:
        """Review j#95333 F1 / j#95601: `queue-enter` + `ok` is not proof of submit.

        j#95601 corrected which signal proves it: the armed working-transition wait that fired
        under a coherent generation, NOT the post-hoc `runtime_state` poll. So `busy` alone no
        longer confirms, and a causal start confirms whatever the later poll happens to read.
        """
        binding = _canonical_binding(
            action_id="startup-abc",
            observed_at="2026-07-29T20:10:01+00:00",
        )
        for runtime_state, causal, expect_dispatched in (
            ("busy", False, False),            # non-causal poll: not a confirmation
            ("awaiting_input", False, False),
            ("turn_ended", False, False),
            ("busy", True, True),              # armed wait fired, coherent generation
            ("turn_ended", True, True),        # a fast turn that already finished
        ):
            with self.subTest(runtime_state=runtime_state, causal=causal):
                observation = {
                    "runtime_state": runtime_state, "read_ok": True,
                    "read_reason": None, "poll_attempts": 2,
                    "observation_kind": "post_choreography_snapshot",
                    "source": "herdr_agent_get",
                }
                if causal:
                    observation.update(
                        event_wait_kind="changed", gateway_binding=binding,
                        baseline_runtime_state="turn_ended",
                        observation_version=2,
                    )
                outcome = SubmitOutcome.from_transport(
                    _outcome("sent", "ok", queue_enter_turn_start_observation=observation),
                    plan_intent="worker_dispatch",
                    rail=RAIL_ANCHORED_SEND,
                    anchor_required=True,
                    ticketless=False,
                    delivery_id="qe-abc",
                )
                self.assertEqual(expect_dispatched, outcome.dispatched)

    def test_absent_transport_outcome_fails_closed_to_uncertain(self) -> None:
        """A rail that returned no structured outcome must not read as delivered."""
        outcome = SubmitOutcome.from_transport(
            None,
            plan_intent="reply",
            rail=RAIL_ANCHORED_REPLY,
            anchor_required=True,
            ticketless=False,
            delivery_id="qe-abc",
        )
        self.assertFalse(outcome.dispatched)
        self.assertEqual("uncertain_partial", outcome.injection_stage)
        self.assertTrue(outcome.blind_retry_prohibited)


class QueueEnterUnconfirmedWordingTest(unittest.TestCase):
    """Queue-enter uncertainty must not inherit the standard/tmux narrative."""

    def test_make_outcome_threads_mode_into_all_uncertain_wording(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
            build_delivery_record,
        )

        for reason, enter_attempts in (
            ("turn_start_unconfirmed", 0),
            ("receiver_blocked", 1),
            ("turn_start_absent", 1),
        ):
            with self.subTest(reason=reason):
                outcome = _outcome(
                    "blocked",
                    reason,
                    mode="queue-enter",
                    queue_enter_turn_start_observation={
                        "observation_kind": "post_choreography_snapshot",
                        "source": "herdr_agent_get",
                        "runtime_state": "awaiting_input",
                        "read_ok": True,
                        "read_reason": None,
                        "poll_attempts": 1,
                        "enter_attempts": enter_attempts,
                        "first_event_wait_kind": None,
                        "final_event_wait_kind": None,
                        "resend_skipped_reason": "identity_unconfirmed",
                    },
                )
                record = build_delivery_record(outcome)
                lowered = record.lower()

                self.assertIn(
                    f"delivery uncertain (queue-enter {reason})", lowered
                )
                self.assertIn(f"ended with `{reason}`", lowered)
                self.assertIn("marker+body was typed at most once", lowered)
                self.assertIn("enter was pressed zero or more times", lowered)
                self.assertIn("every actual enter", lowered)
                self.assertIn("no c-u rollback", lowered)
                self.assertIn("partial delivery remains uncertain", lowered)
                self.assertIn("blind retry is prohibited", lowered)
                self.assertIn("read the codex pane", outcome.next_action.lower())

                for forbidden in (
                    "standard rail",
                    "nothing was submitted",
                    "not delivered",
                    "telemetry-only",
                    "never blocks",
                    "armed a working-transition wait before enter",
                    "no re-send were issued",
                ):
                    self.assertNotIn(forbidden, lowered)

    def test_queue_transport_error_wording_is_backend_neutral(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
            build_delivery_record,
        )

        outcome = _outcome("blocked", "transport_error", mode="queue-enter")
        lowered = build_delivery_record(outcome).lower()

        self.assertIn("delivery uncertain (transport_error)", lowered)
        self.assertIn("enter may have been pressed zero or more times", lowered)
        self.assertIn("marker+body was typed at most once", lowered)
        self.assertNotIn("herdr", lowered)
        self.assertNotIn("armed", lowered)
        self.assertNotIn("no re-send were issued", lowered)

    def test_causal_queue_success_does_not_claim_marker_observation(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
            build_delivery_record,
        )

        binding = _canonical_binding(
            action_id="startup-causal",
            observed_at="2026-08-10T00:00:00+00:00",
        )
        outcome = _outcome(
            "sent",
            "ok",
            mode="queue-enter",
            queue_enter_turn_start_observation={
                "observation_version": 2,
                "event_wait_kind": "changed",
                "baseline_runtime_state": "turn_ended",
                "gateway_binding": binding,
            },
        )
        record = build_delivery_record(outcome)

        self.assertIn("causal turn start confirmed", record)
        self.assertIn("same-generation causal turn start", record)
        self.assertNotIn("Landing marker observed", record)

    def test_tmux_queue_success_keeps_marker_observed_wording(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
            build_delivery_record,
        )

        record = build_delivery_record(
            _outcome("sent", "ok", mode="queue-enter")
        )

        self.assertIn("sent (queue-enter, marker observed)", record)
        self.assertIn("Landing marker observed", record)

    def test_standard_unconfirmed_wording_is_preserved(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
            build_delivery_record,
        )

        outcome = _outcome(
            "blocked", "turn_start_unconfirmed", mode="standard"
        )
        record = build_delivery_record(outcome)
        self.assertIn("codex standard rail", record)
        self.assertIn("No C-u rollback and no re-send were issued", record)
        self.assertIn("tmux capture", record)


if __name__ == "__main__":
    unittest.main()
class BusyQueuedSubmissionRecordTest(unittest.TestCase):
    """Review j#106482: the durable record must not deny the busy outcome.

    ADR-0002 / #15537: a busy-baseline queued submission is proven by the
    composer clearing behind the wait-free full effect fence. The renderer used
    to map every `sent` / `queue_enter` to the tmux marker-unobserved wording
    ("the sender did not verify submission"), which is the OPPOSITE of what the
    busy rail verified; failures likewise claimed "every actual Enter had a
    working-transition wait armed first", denying the waived pending observer.
    """

    @staticmethod
    def _observation(**extra):
        base = {
            "observation_kind": "post_choreography_snapshot",
            "source": "herdr_agent_get",
            "runtime_state": "busy",
            "read_ok": True,
            "read_reason": None,
            "poll_attempts": 1,
            "enter_attempts": 1,
            "first_event_wait_kind": None,
            "final_event_wait_kind": None,
            "resend_skipped_reason": "",
            "baseline_runtime_state": "busy",
        }
        base.update(extra)
        return base

    def _record(self, status, reason, **extra):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
            build_delivery_record,
        )

        outcome = _outcome(
            status,
            reason,
            mode="queue-enter",
            queue_enter_turn_start_observation=self._observation(**extra),
        )
        return build_delivery_record(outcome)

    def test_busy_queued_submission_record_states_composer_clear_not_tmux_wording(
        self,
    ) -> None:
        record = self._record(
            "sent",
            "queue_enter",
            busy_queue_path=True,
            queued_submission_confirmed=True,
        )
        self.assertIn("busy queued submission, composer cleared", record)
        self.assertIn("noncausal queued submission", record)
        self.assertIn("#15537", record)
        self.assertNotIn("marker unobserved", record)
        self.assertNotIn("did not verify submission", record)

    def test_tmux_queue_enter_record_keeps_marker_unobserved_wording(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
            build_delivery_record,
        )

        record = build_delivery_record(_outcome("sent", "queue_enter"))
        self.assertIn("marker unobserved", record)
        self.assertIn("did not verify submission", record)
        self.assertNotIn("busy queued submission", record)

    def test_busy_uncertain_record_does_not_claim_an_armed_wait(self) -> None:
        record = self._record(
            "blocked",
            "turn_start_unconfirmed",
            busy_queue_path=True,
            queued_submission_confirmed=False,
        )
        self.assertIn("busy queue path", record)
        self.assertIn("wait-free full effect fence", record)
        self.assertIn("waived", record)
        self.assertNotIn("armed first", record)

    def test_idle_uncertain_record_scopes_the_armed_wait_claim_to_its_series(
        self,
    ) -> None:
        record = self._record("blocked", "turn_start_unconfirmed")
        self.assertIn("idle/turn-ended series", record)
        self.assertIn("working-transition wait armed first", record)
        self.assertNotIn("busy queue path", record)

    def test_malformed_busy_proof_shapes_fail_closed_to_non_busy_wording(
        self,
    ) -> None:
        """Review j#106486: only the producer's exact ``True`` mints busy proof.

        The queue rail writes exact bools. A truthiness reader would promote
        wire-shaped values it never writes (the string ``"false"``, ints,
        lists) into a fabricated "sender verified the composer cleared" record.
        Every non-canonical shape must fall back to the rail-appropriate
        non-busy wording on both the success and the uncertain path.
        """
        shapes = (
            {"busy_queue_path": "false", "queued_submission_confirmed": "false"},
            {"busy_queue_path": 1, "queued_submission_confirmed": 1},
            {"busy_queue_path": [True], "queued_submission_confirmed": [True]},
            {"busy_queue_path": True, "queued_submission_confirmed": "true"},
            {"busy_queue_path": "true", "queued_submission_confirmed": True},
            {"busy_queue_path": True, "queued_submission_confirmed": False},
            {"busy_queue_path": False, "queued_submission_confirmed": True},
            {},
        )
        for shape in shapes:
            with self.subTest(shape=shape, path="sent"):
                record = self._record("sent", "queue_enter", **shape)
                self.assertNotIn("composer cleared", record)
                self.assertNotIn("sender verified", record)
                self.assertIn("marker unobserved", record)
            with self.subTest(shape=shape, path="uncertain"):
                record = self._record("blocked", "turn_start_unconfirmed", **shape)
                if shape.get("busy_queue_path") is True:
                    continue
                self.assertNotIn("busy queue path", record)
                self.assertIn("working-transition wait armed first", record)
class BusyQueuedSubmissionProjectionTest(unittest.TestCase):
    """Review j#106497 (finding_busyprojection): derivative projections must not
    downgrade the exact busy queued-submission proof back to failure.

    The injection stage stays ``uncertain_partial`` (blind-retry axis unchanged),
    but the positive-delivery axis — the delivery gate, the q-enter front door,
    the composer residue, the callback disposition, and the carried guidance —
    must all report the proven queued submission as delivered. tmux / legacy
    ``sent`` / ``queue_enter`` (no busy observation) and malformed shapes stay
    fail-closed.
    """

    BUSY_PROOF = {
        "observation_kind": "post_choreography_snapshot",
        "source": "herdr_agent_get",
        "runtime_state": "busy",
        "read_ok": True,
        "read_reason": None,
        "poll_attempts": 1,
        "enter_attempts": 1,
        "baseline_runtime_state": "busy",
        "busy_queue_path": True,
        "queued_submission_confirmed": True,
    }
    MALFORMED = {
        **BUSY_PROOF,
        "busy_queue_path": "false",
        "queued_submission_confirmed": "false",
    }

    def _busy_outcome(self):
        return _outcome(
            "sent", "queue_enter",
            queue_enter_turn_start_observation=dict(self.BUSY_PROOF),
        )

    def test_delivery_gate_reports_busy_queued_submission_positive(self) -> None:
        import argparse

        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.delivery_outcome_gate import (  # noqa: E501
            delivery_was_positive,
            publish_delivery_outcome,
        )

        for observation, expected in (
            (dict(self.BUSY_PROOF), True),
            (None, False),                      # tmux / legacy queue_enter
            (dict(self.MALFORMED), False),      # malformed wire shape
        ):
            with self.subTest(observation=observation):
                args = argparse.Namespace()
                publish_delivery_outcome(
                    args,
                    _outcome(
                        "sent", "queue_enter",
                        queue_enter_turn_start_observation=observation,
                    ),
                )
                self.assertIs(expected, delivery_was_positive(args))

    def test_front_door_dispatches_busy_queued_submission(self) -> None:
        outcome = self._busy_outcome()
        front = SubmitOutcome.from_transport(
            outcome, plan_intent="reply", rail=RAIL_ANCHORED_REPLY,
            anchor_required=True, ticketless=False, delivery_id="qe-busy",
        )
        self.assertTrue(front.dispatched)
        self.assertFalse(front.blocked)
        self.assertIsNone(front.blocked_reason)
        # blind-retry axis unchanged: stage stays uncertain_partial, retry prohibited
        self.assertEqual("uncertain_partial", front.injection_stage)
        self.assertTrue(front.blind_retry_prohibited)
        # guidance tells the truth instead of "submission is NOT confirmed"
        self.assertIn("queued submission proven", front.guidance)
        self.assertNotIn("NOT confirmed", front.guidance)

    def test_front_door_keeps_tmux_and_malformed_queue_enter_blocked(self) -> None:
        for observation in (None, dict(self.MALFORMED)):
            with self.subTest(observation=observation):
                front = SubmitOutcome.from_transport(
                    _outcome(
                        "sent", "queue_enter",
                        queue_enter_turn_start_observation=observation,
                    ),
                    plan_intent="reply", rail=RAIL_ANCHORED_REPLY,
                    anchor_required=True, ticketless=False, delivery_id="qe-x",
                )
                self.assertFalse(front.dispatched)
                self.assertTrue(front.blocked)
                self.assertNotIn("queued submission proven", front.guidance)

    def test_composer_residue_is_cleared_only_on_the_exact_busy_proof(self) -> None:
        for observation, expected in (
            (dict(self.BUSY_PROOF), "cleared"),
            (None, "typed_but_pending"),
            (dict(self.MALFORMED), "typed_but_pending"),
        ):
            with self.subTest(observation=observation):
                self.assertEqual(
                    expected,
                    classify_composer_residue(
                        "sent", "queue_enter", mode="queue-enter",
                        queue_enter_turn_start_observation=observation,
                    ),
                )

    def test_callback_disposition_delivers_only_on_the_exact_busy_proof(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (  # noqa: E501
            SEND_DELIVERED,
            SEND_UNCERTAIN,
            send_outcome_for_delivery,
        )

        self.assertEqual(
            SEND_DELIVERED,
            send_outcome_for_delivery(
                "sent", "queue_enter",
                injection_stage="uncertain_partial",
                queue_enter_turn_start_observation=dict(self.BUSY_PROOF),
            ),
        )
        for observation in (None, dict(self.MALFORMED)):
            with self.subTest(observation=observation):
                self.assertEqual(
                    SEND_UNCERTAIN,
                    send_outcome_for_delivery(
                        "sent", "queue_enter",
                        injection_stage="uncertain_partial",
                        queue_enter_turn_start_observation=observation,
                    ),
                )

    def test_carried_next_action_is_truthful_on_the_busy_proof(self) -> None:
        outcome = self._busy_outcome()
        telemetry = outcome.injection_stage
        self.assertEqual("uncertain_partial", telemetry["stage"])
        self.assertTrue(telemetry["blind_retry_prohibited"])
        self.assertIn("queued submission proven", telemetry["next_action"])
        self.assertNotIn("NOT confirmed", telemetry["next_action"])
        generic = _outcome(
            "sent", "queue_enter",
            queue_enter_turn_start_observation=dict(self.MALFORMED),
        ).injection_stage
        self.assertIn("NOT confirmed", generic["next_action"])
