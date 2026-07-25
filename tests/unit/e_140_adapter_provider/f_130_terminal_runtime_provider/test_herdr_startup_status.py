"""`herdr startup-status` read-only startup evidence surface (Redmine #14231 step 3).

Pins the j#84724 public-surface contract: an action-scoped, diagnostic-only report that
can describe a generation ``doctor`` cannot (one that already vanished from the live
inventory), that never claims more than the evidence supports, and that emits no path /
env value / pane body / stderr text.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.startup_execution_events import (  # noqa: E402
    STAGE_ATTESTATION_WRITE_FAILED,
    STAGE_PROVIDER_EXEC_CALL_REACHED,
    STAGE_PROVIDER_EXEC_REJECTED,
    STAGE_SELF_LOOKUP_SUCCEEDED,
    STAGE_SELF_LOOKUP_TIMED_OUT,
    STAGE_WRAPPER_ENTERED,
    append_execution_event,
)
from mozyo_bridge.core.state.startup_transaction_fence import (  # noqa: E402
    Participant,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_status import (  # noqa: E402,E501
    STATUS_ACTION_UNKNOWN,
    STATUS_OK,
    _render_text,
    build_startup_status,
)

WS = "ws1"
LANE = "lane-1"
CLAUDE_NAME = "mzb1_ws1_claude_lane-1"
CLAUDE_LOCATOR = "wY:p2"
CODEX_NAME = "mzb1_ws1_codex_lane-1"
CODEX_LOCATOR = "wY:p3"


class StartupStatusReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fence = StartupTransactionFence(home=Path(self._tmp.name))
        self.unit = StartupUnit(workspace_id=WS, lane_id=LANE, providers=("claude",))

    def _reserve_with_participant(self, nonce="n1"):
        action = self.fence.reserve(self.unit, nonce)
        self.fence.record_participant(
            action.action_id,
            Participant(
                role="claude",
                assigned_name=CLAUDE_NAME,
                locator=CLAUDE_LOCATOR,
                receipt="workspace=wY",
            ),
        )
        return action.action_id

    def test_unknown_action_is_reported_as_unknown_not_as_no_evidence(self) -> None:
        report = build_startup_status(
            action_id="startup-does-not-exist", fence=self.fence, live_locators=[]
        )
        self.assertEqual(report.status, STATUS_ACTION_UNKNOWN)
        self.assertFalse(report.ok)

    def test_exec_reached_and_locator_live_is_confirmed(self) -> None:
        action_id = self._reserve_with_participant()
        append_execution_event(self.fence, action_id, STAGE_WRAPPER_ENTERED)
        append_execution_event(self.fence, action_id, STAGE_PROVIDER_EXEC_CALL_REACHED)
        report = build_startup_status(
            action_id=action_id, fence=self.fence, live_locators=[CLAUDE_LOCATOR]
        )
        self.assertEqual(report.status, STATUS_OK)
        (participant,) = report.participants
        self.assertEqual(participant.inventory_join, "provider_live_confirmed")
        self.assertFalse(participant.evidence_gap)
        self.assertIn("no recovery is needed", participant.next_action)

    def test_vanished_generation_is_readable_after_the_locator_is_gone(self) -> None:
        # The whole point of the surface: doctor cannot describe this action because its
        # row is not in the live inventory, but the evidence still reads.
        action_id = self._reserve_with_participant()
        append_execution_event(self.fence, action_id, STAGE_WRAPPER_ENTERED)
        append_execution_event(self.fence, action_id, STAGE_PROVIDER_EXEC_CALL_REACHED)
        report = build_startup_status(
            action_id=action_id, fence=self.fence, live_locators=[]
        )
        (participant,) = report.participants
        self.assertEqual(participant.inventory_join, "post_exec_locator_absent")
        self.assertEqual(participant.assigned_name, CLAUDE_NAME)
        self.assertEqual(participant.last_stage, STAGE_PROVIDER_EXEC_CALL_REACHED)
        self.assertIn("session-rollback", participant.next_action)

    def test_unreadable_inventory_is_not_reported_as_locator_absent(self) -> None:
        action_id = self._reserve_with_participant()
        append_execution_event(self.fence, action_id, STAGE_PROVIDER_EXEC_CALL_REACHED)
        report = build_startup_status(
            action_id=action_id, fence=self.fence, live_locators=None
        )
        (participant,) = report.participants
        self.assertEqual(participant.inventory_join, "inventory_unreadable")
        self.assertIn("NOT absent", participant.next_action)

    def test_stopped_before_exec_carries_the_stage_and_bounded_reason(self) -> None:
        action_id = self._reserve_with_participant()
        append_execution_event(self.fence, action_id, STAGE_WRAPPER_ENTERED)
        append_execution_event(
            self.fence, action_id, STAGE_SELF_LOOKUP_TIMED_OUT, bounded_reason="row_absent"
        )
        append_execution_event(
            self.fence,
            action_id,
            STAGE_ATTESTATION_WRITE_FAILED,
            bounded_reason="locator_unavailable",
        )
        report = build_startup_status(
            action_id=action_id, fence=self.fence, live_locators=[]
        )
        (participant,) = report.participants
        self.assertEqual(participant.last_stage, STAGE_ATTESTATION_WRITE_FAILED)
        self.assertEqual(participant.bounded_reason, "locator_unavailable")
        # No liveness conclusion is drawn -- the wrapper never reached the exec call.
        self.assertEqual(participant.inventory_join, "not_applicable")
        self.assertIn("no liveness conclusion applies", participant.next_action)

    def test_absent_evidence_is_a_reported_gap_not_a_wrapper_never_ran_claim(self) -> None:
        # A launch that predates the projection: participants exist, evidence does not.
        action_id = self._reserve_with_participant()
        report = build_startup_status(
            action_id=action_id, fence=self.fence, live_locators=[]
        )
        (participant,) = report.participants
        self.assertEqual(participant.last_stage, "no_evidence")
        self.assertTrue(participant.evidence_gap)
        self.assertIn("NOT proof the wrapper never ran", participant.next_action)

    def test_payload_carries_no_path_or_secret_shaped_content(self) -> None:
        import json

        action_id = self._reserve_with_participant()
        append_execution_event(self.fence, action_id, STAGE_PROVIDER_EXEC_CALL_REACHED)
        raw = json.dumps(
            build_startup_status(
                action_id=action_id, fence=self.fence, live_locators=[]
            ).as_payload()
        ).lower()
        for banned in ("token", "secret", "password", "credential", "/users/", "/private/"):
            self.assertNotIn(banned, raw)

    def test_report_is_read_only_action_row_is_unchanged(self) -> None:
        action_id = self._reserve_with_participant()
        before = self.fence.read(action_id)
        build_startup_status(action_id=action_id, fence=self.fence, live_locators=[])
        self.assertEqual(self.fence.read(action_id), before)


class LiveProviderProjectionTest(unittest.TestCase):
    """Redmine #14456 — a live provider must never project as a pre-exec stop.

    Reproduces the shape measured on the real pair launches: the wrapper's post-lookup
    appends lost the lock race, so `provider_exec_call_reached` is absent from the
    participant's timeline while the provider is running at the locator the action
    launched. The public read-only status is the whole surface under test — no raw
    herdr / tmux / log / SQLite access is needed to see the stage.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fence = StartupTransactionFence(home=Path(self._tmp.name))

    def _pair_action(self):
        action = self.fence.reserve(
            StartupUnit(workspace_id=WS, lane_id=LANE, providers=("codex", "claude")),
            "n-14456",
        )
        for role, name, locator in (
            ("codex", CODEX_NAME, CODEX_LOCATOR),
            ("claude", CLAUDE_NAME, CLAUDE_LOCATOR),
        ):
            self.fence.record_participant(
                action.action_id,
                Participant(
                    role=role, assigned_name=name, locator=locator, receipt="workspace=wY"
                ),
            )
        return action.action_id

    def _seed_full(self, action_id, name):
        for stage in (STAGE_WRAPPER_ENTERED, STAGE_PROVIDER_EXEC_CALL_REACHED):
            append_execution_event(self.fence, action_id, stage, participant=name)

    def _seed_truncated(self, action_id, name):
        """The dropped-append shape: the timeline stops at the last row that landed."""
        for stage in (STAGE_WRAPPER_ENTERED, STAGE_SELF_LOOKUP_SUCCEEDED):
            append_execution_event(self.fence, action_id, stage, participant=name)

    def test_live_pair_with_a_dropped_exec_row_is_a_terminal_success(self) -> None:
        action_id = self._pair_action()
        self._seed_full(action_id, CODEX_NAME)
        self._seed_truncated(action_id, CLAUDE_NAME)
        report = build_startup_status(
            action_id=action_id,
            fence=self.fence,
            live_locators=[CODEX_LOCATOR, CLAUDE_LOCATOR],
        )
        by_role = {p.role: p for p in report.participants}
        claude = by_role["claude"]
        # The defect: this read `not_applicable` / "stopped before the provider exec
        # call" about a demonstrably running provider.
        self.assertEqual(claude.inventory_join, "provider_live_exec_unrecorded")
        self.assertTrue(claude.live_success)
        self.assertIn("the provider is live", claude.next_action)
        self.assertNotIn("stopped before the provider exec call", claude.next_action)
        # The gap is still declared, and no unrecorded stage is invented.
        self.assertTrue(claude.evidence_gap)
        self.assertEqual(claude.last_stage, STAGE_SELF_LOOKUP_SUCCEEDED)
        # The fully-evidenced sibling keeps its #14231 verdict exactly.
        self.assertEqual(by_role["codex"].inventory_join, "provider_live_confirmed")
        self.assertFalse(by_role["codex"].evidence_gap)
        # And the action rolls up as the successful live pair it is.
        self.assertEqual(report.provider_liveness, "all_live")

    def test_partial_pair_is_typed_apart_from_success_and_from_none_live(self) -> None:
        action_id = self._pair_action()
        self._seed_full(action_id, CODEX_NAME)
        self._seed_truncated(action_id, CLAUDE_NAME)
        report = build_startup_status(
            action_id=action_id, fence=self.fence, live_locators=[CODEX_LOCATOR]
        )
        by_role = {p.role: p for p in report.participants}
        self.assertEqual(report.provider_liveness, "partial")
        self.assertTrue(by_role["codex"].live_success)
        # Claude is not live, so the truncated timeline draws no liveness conclusion.
        self.assertFalse(by_role["claude"].live_success)
        self.assertEqual(by_role["claude"].inventory_join, "not_applicable")

    def test_vanished_pair_is_none_live(self) -> None:
        action_id = self._pair_action()
        self._seed_full(action_id, CODEX_NAME)
        self._seed_full(action_id, CLAUDE_NAME)
        report = build_startup_status(
            action_id=action_id, fence=self.fence, live_locators=[]
        )
        self.assertEqual(report.provider_liveness, "none_live")
        for participant in report.participants:
            self.assertEqual(participant.inventory_join, "post_exec_locator_absent")
            self.assertFalse(participant.live_success)

    def test_unreadable_inventory_rolls_up_indeterminate_never_none_live(self) -> None:
        action_id = self._pair_action()
        self._seed_full(action_id, CODEX_NAME)
        self._seed_truncated(action_id, CLAUDE_NAME)
        report = build_startup_status(
            action_id=action_id, fence=self.fence, live_locators=None
        )
        self.assertEqual(report.provider_liveness, "indeterminate")
        for participant in report.participants:
            self.assertFalse(participant.live_success)

    def test_an_evidenced_exec_rejection_is_not_promoted_by_a_live_locator(self) -> None:
        action_id = self._pair_action()
        append_execution_event(
            self.fence, action_id, STAGE_WRAPPER_ENTERED, participant=CLAUDE_NAME
        )
        append_execution_event(
            self.fence,
            action_id,
            STAGE_PROVIDER_EXEC_REJECTED,
            bounded_reason="argv0_alias_unbound",
            participant=CLAUDE_NAME,
        )
        report = build_startup_status(
            action_id=action_id, fence=self.fence, live_locators=[CLAUDE_LOCATOR]
        )
        claude = {p.role: p for p in report.participants}["claude"]
        self.assertEqual(claude.inventory_join, "exec_stopped_locator_live")
        self.assertFalse(claude.live_success)
        self.assertEqual(claude.bounded_reason, "argv0_alias_unbound")
        self.assertIn("reused", claude.next_action)
        # A reused locator must not make the action look successful.
        self.assertNotEqual(report.provider_liveness, "all_live")

    def test_report_stays_value_free_and_read_only(self) -> None:
        import json

        action_id = self._pair_action()
        self._seed_truncated(action_id, CLAUDE_NAME)
        before = self.fence.read(action_id)
        report = build_startup_status(
            action_id=action_id, fence=self.fence, live_locators=[CLAUDE_LOCATOR]
        )
        self.assertEqual(self.fence.read(action_id), before)
        raw = json.dumps(report.as_payload()).lower()
        for banned in ("token", "secret", "password", "credential", "/users/", "/private/"):
            self.assertNotIn(banned, raw)
        # The new roll-up and per-participant verdict are both on the public payload.
        self.assertIn("provider_liveness", report.as_payload())
        self.assertIn("live_success", report.as_payload()["participants"][0])

    def test_text_render_states_the_liveness_without_raw_access(self) -> None:
        action_id = self._pair_action()
        self._seed_truncated(action_id, CLAUDE_NAME)
        self._seed_full(action_id, CODEX_NAME)
        text = _render_text(
            build_startup_status(
                action_id=action_id,
                fence=self.fence,
                live_locators=[CLAUDE_LOCATOR, CODEX_LOCATOR],
            )
        )
        self.assertIn("provider liveness: all_live", text)
        self.assertIn("live=yes", text)
        self.assertIn("timeline is incomplete", text)


class StartupStatusCliRegistrationTest(unittest.TestCase):
    def test_command_is_registered_on_the_real_parser(self) -> None:
        import mozyo_bridge.application.cli as cli

        args = cli.build_parser().parse_args(
            ["herdr", "startup-status", "--action-id", "startup-x", "--json"]
        )
        self.assertEqual(args.func.__name__, "cmd_herdr_startup_status")
        self.assertEqual(args.action_id, "startup-x")
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
