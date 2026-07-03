"""Pure sublane actuation VO / journal-renderer tests (Redmine #12973).

Covers the pure domain surface with no IO: the outcome payload shape and the durable-
record renderer for the executed / dry-run / blocked variants.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    ACTUATE_READY,
    DISPATCH_GATEWAY_NOTIFIED,
    DISPATCH_SKIPPED,
    DISPATCH_WORKER_DISPATCHED,
    REASON_HANDOFF_FAILED,
    ActuationStep,
    SublaneActuationOutcome,
    render_actuation_journal,
)


def _outcome(**kw):
    base = dict(
        status=ACTUATE_EXECUTED,
        execute=True,
        reason="ok",
        issue="12973",
        lane_label="issue_12973_x",
        branch="b",
        worktree_path="/wt/12973",
        launch_action="create_worktree",
        gateway_pane="%120",
        worker_pane="%121",
        dispatch_target="%120",
        dispatch_result=DISPATCH_GATEWAY_NOTIFIED,
        durable_anchor="70159",
    )
    base.update(kw)
    return SublaneActuationOutcome(**base)


class OutcomePayloadTests(unittest.TestCase):
    def test_executed_payload_round_trips_fields(self):
        outcome = _outcome(
            steps=(ActuationStep(1, "create worktree", "executed", "done"),)
        )
        payload = outcome.as_payload()
        self.assertEqual(payload["status"], ACTUATE_EXECUTED)
        self.assertTrue(payload["execute"])
        self.assertEqual(payload["gateway_pane"], "%120")
        self.assertEqual(payload["worker_pane"], "%121")
        self.assertEqual(payload["dispatch_result"], "gateway_notified")
        # #12986: a gateway-notified lane is NOT worker-confirmed.
        self.assertFalse(payload["worker_dispatch_confirmed"])
        self.assertEqual(payload["steps"][0]["status"], "executed")
        self.assertEqual(payload["blocked_reasons"], [])

    def test_executed_property_flags(self):
        self.assertTrue(_outcome().executed)
        self.assertFalse(_outcome().is_blocked)
        self.assertTrue(_outcome(status=ACTUATE_BLOCKED).is_blocked)

    def test_worker_dispatch_confirmed_only_when_worker_dispatched(self):
        # #12986: gateway_notified / skipped are NOT worker-confirmed; only the
        # explicit worker_dispatched token is.
        self.assertFalse(_outcome().worker_dispatch_confirmed)
        self.assertFalse(
            _outcome(dispatch_result=DISPATCH_SKIPPED).worker_dispatch_confirmed
        )
        self.assertTrue(
            _outcome(
                dispatch_result=DISPATCH_WORKER_DISPATCHED
            ).worker_dispatch_confirmed
        )


class JournalRenderTests(unittest.TestCase):
    def test_executed_journal_lists_panes_and_next_action(self):
        text = render_actuation_journal(_outcome())
        self.assertIn("## sublane actuated", text)
        self.assertIn("- gateway_pane: %120", text)
        self.assertIn("- worker_pane: %121", text)
        self.assertIn("- dispatch_result: gateway_notified", text)
        self.assertIn("- worker_dispatch_confirmed: false", text)
        self.assertIn("- durable_anchor: 70159", text)
        self.assertIn("sublane list --json", text)

    def test_gateway_notified_next_action_is_honest_about_worker(self):
        # #12986: the record must NOT claim the gateway already routed to the
        # worker; it must flag worker dispatch as unconfirmed and point at the
        # callback-recovery classifier. #12988: it also points at the ack drive
        # that can actually promote the state.
        text = render_actuation_journal(_outcome())
        self.assertIn("worker dispatch NOT yet confirmed", text)
        self.assertIn("no_progress_after_handoff", text)
        self.assertIn("callback-recovery", text)
        self.assertIn("sublane dispatch-worker --execute", text)

    def test_worker_dispatched_next_action_awaits_callback(self):
        text = render_actuation_journal(
            _outcome(dispatch_result=DISPATCH_WORKER_DISPATCHED)
        )
        self.assertIn("- worker_dispatch_confirmed: true", text)
        self.assertIn("worker dispatch confirmed", text)

    def test_dry_run_journal_heading_and_hint(self):
        text = render_actuation_journal(
            _outcome(status=ACTUATE_READY, execute=False, dispatch_target=None)
        )
        self.assertIn("## sublane actuation plan (dry-run)", text)
        self.assertIn("re-run with --execute", text)

    def test_blocked_journal_records_reasons_and_callback(self):
        text = render_actuation_journal(
            _outcome(
                status=ACTUATE_BLOCKED,
                blocked_reasons=(REASON_HANDOFF_FAILED,),
            )
        )
        self.assertIn("## sublane actuation blocked", text)
        self.assertIn("- blocked_reasons: handoff_failed", text)
        self.assertIn("coordinator callback", text)


if __name__ == "__main__":
    unittest.main()
