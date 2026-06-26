from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.handoff import (
    AnchorError,
    AsanaAnchor,
    KIND_LABELS,
    LastInputProjection,
    MODE_PENDING,
    MODE_QUEUE_ENTER,
    MODE_STANDARD,
    QUEUE_ENTER_RETRY_INTERVAL_SECONDS,
    QUEUE_ENTER_RETRY_WINDOW_SECONDS,
    QueueEnterRetryOutcome,
    RedmineAnchor,
    build_delivery_record,
    build_marker,
    build_notification_body,
    make_outcome,
    next_action_for,
    normalize_anchor,
    project_last_input,
    resolve_queue_enter_retry_policy,
)

class HandoffDomainTest(unittest.TestCase):
    def test_normalize_anchor_builds_asana_with_comment_id(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")

        self.assertIsInstance(anchor, AsanaAnchor)
        self.assertEqual("T1", anchor.task_id)
        self.assertEqual("C1", anchor.comment_id)
        self.assertIsNone(anchor.anchor_url)

    def test_normalize_anchor_builds_asana_with_anchor_url(self) -> None:
        anchor = normalize_anchor(
            "asana", task_id="T1", anchor_url="https://app.asana.com/0/0/T1#2026-05"
        )

        self.assertIsInstance(anchor, AsanaAnchor)
        self.assertEqual("https://app.asana.com/0/0/T1#2026-05", anchor.anchor_url)
        self.assertIsNone(anchor.comment_id)

    def test_normalize_anchor_rejects_asana_with_both_comment_and_url(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor(
                "asana", task_id="T1", comment_id="C1", anchor_url="https://example/x"
            )

    def test_normalize_anchor_rejects_asana_with_neither_comment_nor_url(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor("asana", task_id="T1")

    def test_normalize_anchor_rejects_asana_without_task_id(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor("asana", comment_id="C1")

    def test_normalize_anchor_builds_redmine(self) -> None:
        anchor = normalize_anchor("redmine", issue="9020", journal="46005")

        self.assertIsInstance(anchor, RedmineAnchor)
        self.assertEqual("9020", anchor.issue)
        self.assertEqual("46005", anchor.journal)

    def test_normalize_anchor_rejects_redmine_with_asana_fields(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor(
                "redmine", issue="9020", journal="46005", task_id="T1"
            )

    def test_normalize_anchor_rejects_asana_with_redmine_fields(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor(
                "asana", task_id="T1", comment_id="C1", journal="46005"
            )

    def test_normalize_anchor_rejects_unknown_source(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor("jira", task_id="T1")

    def test_build_marker_for_asana_with_comment(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")

        self.assertEqual(
            "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=reply:to=claude]",
            build_marker(anchor, "reply", "claude"),
        )

    def test_build_marker_for_asana_with_anchor_url(self) -> None:
        anchor = normalize_anchor(
            "asana", task_id="T1", anchor_url="https://example/x"
        )

        self.assertEqual(
            "[mozyo:handoff:source=asana:task=T1:anchor=https://example/x:kind=review_result:to=codex]",
            build_marker(anchor, "review_result", "codex"),
        )

    def test_build_marker_for_redmine(self) -> None:
        anchor = normalize_anchor("redmine", issue="9020", journal="46005")

        self.assertEqual(
            "[mozyo:handoff:source=redmine:issue=9020:journal=46005:kind=review_request:to=codex]",
            build_marker(anchor, "review_request", "codex"),
        )

    def test_build_notification_body_requires_summary_for_custom_kind(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")

        with self.assertRaises(AnchorError):
            build_notification_body(anchor, "custom", None, "claude")

    def test_build_notification_body_appends_durable_pointer(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")

        body = build_notification_body(anchor, "implementation_request", None, "claude")

        self.assertIn("implementation request ready for claude", body)
        self.assertIn("Asana task T1", body)
        self.assertIn("comment C1", body)
        self.assertIn("durable anchor", body)

    def test_build_notification_body_uses_summary_when_provided(self) -> None:
        anchor = normalize_anchor("redmine", issue="9020", journal="46005")

        body = build_notification_body(
            anchor, "custom", "ship hotfix to staging", "codex"
        )

        self.assertIn("ship hotfix to staging", body)
        self.assertIn("Redmine #9020", body)
        self.assertIn("journal #46005", body)

    def test_build_notification_body_rejects_unknown_kind(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")

        with self.assertRaises(AnchorError):
            build_notification_body(anchor, "shipping_notice", None, "claude")

    def test_make_outcome_sent_attributes_action_to_receiver(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker="[marker]",
        )

        self.assertEqual("receiver", outcome.next_action_owner)
        self.assertIn("durable anchor", outcome.next_action)
        payload = json.loads(outcome.to_json())
        self.assertEqual("sent", payload["status"])
        self.assertEqual("[marker]", payload["notification_marker"])
        self.assertEqual({"source": "asana", "task_id": "T1", "comment_id": "C1"}, payload["anchor"])

    def test_make_outcome_preserves_source_even_when_anchor_is_none(self) -> None:
        # Failure paths like invalid_anchor / invalid_args call make_outcome
        # without a normalized anchor. The structured contract still requires
        # `source`, so downstream durable-record integration (task
        # 1214760547941073) does not need to recover it out of band.
        outcome = make_outcome(
            status="blocked",
            reason="invalid_anchor",
            receiver="claude",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker=None,
            source="asana",
        )

        self.assertEqual("asana", outcome.source)
        payload = json.loads(outcome.to_json())
        self.assertEqual("asana", payload["source"])
        self.assertIsNone(payload["anchor"])

    def test_make_outcome_pending_attributes_action_to_operator(self) -> None:
        anchor = normalize_anchor("redmine", issue="9020", journal="46005")
        outcome = make_outcome(
            status="pending_input",
            reason="ok",
            receiver="codex",
            target="%2",
            anchor=anchor,
            mode=MODE_PENDING,
            kind="reply",
            notification_marker="[marker]",
        )

        self.assertEqual("operator", outcome.next_action_owner)
        self.assertIn("pending prompt", outcome.next_action)

    def test_next_action_for_marker_timeout_attributes_to_sender(self) -> None:
        owner, action = next_action_for("blocked", "marker_timeout", "claude")

        self.assertEqual("sender", owner)
        # The terminal escalation label is preserved so audit tooling and the
        # preset's `Notification fails` branch keep grepping the same word,
        # but the action now spells out the retry budget that must precede it
        # (Asana task 1214779823377861).
        self.assertIn("un-notified", action)
        self.assertIn("mozyo-bridge read claude", action)
        self.assertIn("--no-submit", action)
        self.assertIn("3", action)
        self.assertIn("next-action verb", action)

    def test_kind_labels_contract_is_stable(self) -> None:
        self.assertEqual(
            {
                "implementation_request",
                "design_consultation",
                "review_request",
                "review_result",
                "implementation_done",
                "reply",
                "custom",
            },
            set(KIND_LABELS),
        )


class ProjectLastInputTest(unittest.TestCase):
    """Cover the inspector ``last_input`` projection helper.

    The mapping table is fixed by the receiver-state inspector contract at
    ``mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md``
    section "Receiver Inspector and Existing DeliveryOutcome" and the upstream
    transport-agnostic ACK contract section "Existing DeliveryOutcome との対応".
    Both prohibit translating ACK terminal states (``blocked + *``) into
    runtime/process state, and tmux-path outcomes never claim ``acknowledged``.
    """

    def _build_outcome(
        self,
        *,
        status,
        reason,
        receiver: str = "claude",
        mode: str = MODE_STANDARD,
    ):
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        return make_outcome(
            status=status,
            reason=reason,
            receiver=receiver,
            target="%2",
            anchor=anchor,
            mode=mode,
            kind="implementation_request",
            notification_marker="[marker]",
        )

    def test_sent_ok_projects_submitted_ack_status(self) -> None:
        outcome = self._build_outcome(status="sent", reason="ok")

        projection = project_last_input(
            outcome,
            submitted_at="2026-05-13T13:20:28Z",
            input_kind="prompt",
            prompt_turn_id="turn-1",
            input_id="input-1",
        )

        self.assertEqual(
            LastInputProjection(
                submitted_at="2026-05-13T13:20:28Z",
                acknowledged_at=None,
                ack_status="submitted",
                input_kind="prompt",
                prompt_turn_id="turn-1",
                input_id="input-1",
            ),
            projection,
        )

    def test_sent_ok_does_not_claim_acknowledged_on_tmux_path(self) -> None:
        # The tmux compatibility layer cannot observe runtime.input.ack; the
        # helper must not synthesize an `acknowledged` claim from a `sent`
        # outcome even when the caller forgets to pass `submitted_at`.
        outcome = self._build_outcome(status="sent", reason="ok")

        projection = project_last_input(outcome)

        assert projection is not None
        self.assertEqual("submitted", projection.ack_status)
        self.assertIsNone(projection.acknowledged_at)
        self.assertIsNone(projection.submitted_at)

    def test_pending_input_ok_projects_unobserved_ack_status(self) -> None:
        # Per the inspector contract, `pending_input/ok` carries input staged
        # at the prompt but the receiver runtime has not received the turn.
        # `submitted_at` stays null and `ack_status` is `unobserved`.
        outcome = self._build_outcome(
            status="pending_input", reason="ok", mode=MODE_PENDING
        )

        projection = project_last_input(
            outcome,
            submitted_at="2026-05-13T13:20:28Z",
            input_kind="prompt",
            prompt_turn_id="turn-7",
        )

        assert projection is not None
        self.assertIsNone(projection.submitted_at)
        self.assertIsNone(projection.acknowledged_at)
        self.assertEqual("unobserved", projection.ack_status)
        self.assertEqual("prompt", projection.input_kind)
        self.assertEqual("turn-7", projection.prompt_turn_id)

    def test_blocked_marker_timeout_yields_no_projection(self) -> None:
        # `marker_timeout` is an ACK terminal state, not a receiver-runtime
        # fact. Refusing to project it prevents callers from inferring
        # `process.exited` or any runtime_phase value from a rollback.
        outcome = self._build_outcome(status="blocked", reason="marker_timeout")

        self.assertIsNone(project_last_input(outcome))

    def test_blocked_target_unavailable_yields_no_projection(self) -> None:
        outcome = self._build_outcome(status="blocked", reason="target_unavailable")

        self.assertIsNone(project_last_input(outcome))

    def test_blocked_target_not_agent_yields_no_projection(self) -> None:
        outcome = self._build_outcome(status="blocked", reason="target_not_agent")

        self.assertIsNone(project_last_input(outcome))

    def test_blocked_invalid_anchor_yields_no_projection(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="invalid_anchor",
            receiver="claude",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker=None,
            source="asana",
        )

        self.assertIsNone(project_last_input(outcome))

    def test_blocked_invalid_args_yields_no_projection(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="invalid_args",
            receiver="claude",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker=None,
            source="asana",
        )

        self.assertIsNone(project_last_input(outcome))

    def test_outcome_method_matches_projection_helper(self) -> None:
        outcome = self._build_outcome(status="sent", reason="ok")

        via_method = outcome.to_last_input_projection(
            submitted_at="2026-05-13T13:20:28Z",
            input_kind="prompt",
            prompt_turn_id="turn-1",
        )
        via_helper = project_last_input(
            outcome,
            submitted_at="2026-05-13T13:20:28Z",
            input_kind="prompt",
            prompt_turn_id="turn-1",
        )

        self.assertEqual(via_helper, via_method)

    def test_projection_dataclass_is_serialisable_to_dict(self) -> None:
        # Inspector consumers serialise projections into the ReceiverState
        # snapshot; ensure the dataclass exposes all the expected fields.
        outcome = self._build_outcome(status="sent", reason="ok")

        projection = project_last_input(
            outcome, submitted_at="2026-05-13T13:20:28Z"
        )
        assert projection is not None

        self.assertEqual(
            {
                "submitted_at": "2026-05-13T13:20:28Z",
                "acknowledged_at": None,
                "ack_status": "submitted",
                "input_kind": None,
                "prompt_turn_id": None,
                "input_id": None,
            },
            projection.to_dict(),
        )


class QueueEnterRetryPolicyTest(unittest.TestCase):
    def test_defaults_resolve_to_module_constants(self) -> None:
        policy = resolve_queue_enter_retry_policy()
        self.assertEqual(QUEUE_ENTER_RETRY_WINDOW_SECONDS, policy.window_seconds)
        self.assertEqual(QUEUE_ENTER_RETRY_INTERVAL_SECONDS, policy.interval_seconds)
        # 30s / 2s -> 15 additional Enter presses.
        self.assertEqual(15, policy.max_retries)
        self.assertTrue(policy.enabled)

    def test_explicit_overrides_are_clamped_non_negative(self) -> None:
        policy = resolve_queue_enter_retry_policy(10, 2)
        self.assertEqual(5, policy.max_retries)
        # Negative values clamp to 0 (disabled) rather than producing a
        # nonsensical negative count.
        self.assertFalse(resolve_queue_enter_retry_policy(-5, 2).enabled)
        self.assertEqual(0, resolve_queue_enter_retry_policy(-5, 2).max_retries)

    def test_zero_window_or_interval_disables_retry(self) -> None:
        self.assertEqual(0, resolve_queue_enter_retry_policy(0, 2).max_retries)
        self.assertEqual(0, resolve_queue_enter_retry_policy(30, 0).max_retries)
        self.assertFalse(resolve_queue_enter_retry_policy(0, 2).enabled)

    def test_none_falls_back_per_field(self) -> None:
        # Only the interval is overridden; the window keeps the default.
        policy = resolve_queue_enter_retry_policy(None, 5)
        self.assertEqual(QUEUE_ENTER_RETRY_WINDOW_SECONDS, policy.window_seconds)
        self.assertEqual(5.0, policy.interval_seconds)
        self.assertEqual(6, policy.max_retries)


class DeliveryRecordRetryLineTest(unittest.TestCase):
    def _queue_enter_outcome(self, reason: str):
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        return make_outcome(
            status="sent",
            reason=reason,
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="implementation_request",
            notification_marker=build_marker(anchor, "implementation_request", "claude"),
        )

    def test_retry_line_rendered_for_unobserved_pass(self) -> None:
        record = build_delivery_record(
            self._queue_enter_outcome("queue_enter"),
            retry=QueueEnterRetryOutcome(
                window_seconds=30.0,
                interval_seconds=2.0,
                enter_attempts=16,
                marker_observed=False,
            ),
        )
        self.assertIn(
            "- Retry: queue-enter Enter-only retry (window 30s / interval 2s)",
            record,
        )
        self.assertIn("re-issued Enter 15 time(s)", record)
        self.assertIn("16 Enter press(es) total", record)
        self.assertIn("marker+body typed once and never re-injected", record)
        self.assertIn("still unobserved after the retry window", record)

    def test_retry_line_reports_observed_after_retry(self) -> None:
        record = build_delivery_record(
            self._queue_enter_outcome("ok"),
            retry=QueueEnterRetryOutcome(
                window_seconds=30.0,
                interval_seconds=2.0,
                enter_attempts=3,
                marker_observed=True,
            ),
        )
        self.assertIn("re-issued Enter 2 time(s)", record)
        self.assertIn("landing marker observed after retry", record)

    def test_no_retry_line_without_retry_record(self) -> None:
        # Default callers (no retry pass) get a byte-identical record: the retry
        # telemetry is opt-in and never reaches the wire outcome.
        record = build_delivery_record(self._queue_enter_outcome("ok"))
        self.assertNotIn("Enter-only retry", record)
        self.assertNotIn("retry", self._queue_enter_outcome("ok").to_dict())


if __name__ == "__main__":
    unittest.main()
