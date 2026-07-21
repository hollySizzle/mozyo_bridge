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
    build_startup_status,
)

WS = "ws1"
LANE = "lane-1"
CLAUDE_NAME = "mzb1_ws1_claude_lane-1"
CLAUDE_LOCATOR = "wY:p2"


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
