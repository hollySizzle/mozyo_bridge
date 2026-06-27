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

    def test_dispatched_outcome_points_at_transport(self) -> None:
        outcome = SubmitOutcome(
            intent="consultation_callback",
            resolved_rail=RAIL_TICKETLESS_CALLBACK,
            anchor_required=False,
            ticketless=True,
            delivery_id="qe-abc",
            dispatched=True,
            blocked=False,
        )
        self.assertTrue(outcome.to_dict()["dispatched"])
        self.assertIn("transport outcome", "\n".join(outcome.record_lines()))


if __name__ == "__main__":
    unittest.main()
