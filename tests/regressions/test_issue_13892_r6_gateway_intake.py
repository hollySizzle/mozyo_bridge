"""Redmine #13892 R6-F1 — the canonical writer must have a gateway production caller.

`record_dispatch_disposition` had exactly zero callers outside its own definition and two
tests. So in live Redmine no `dispatch-disposition` marker could ever exist, every `delivered`
dispatch row stayed permanently `owed`, and the over-block j#80629 removed *in design* was
never removed *in production*. A writer only tests can reach is not a rail.

These pin the round trip the ruling demanded: production entry -> live source/transport seam ->
writer -> reader. The reader is the real `LiveSessionRetireOps._durable_disposition` running
over the real appended note, so the marker is not merely written — it is proven readable by the
consumer whose over-block it exists to lift.
"""

from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (  # noqa: E501
    ROLE_DELEGATED_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.gateway_disposition_intake import (  # noqa: E501
    LEG_ATTEMPTED,
    LEG_NOT_APPLICABLE,
    REASON_DISPATCH_AMBIGUOUS,
    REASON_NOT_GATEWAY_LANE,
    execute_gateway_disposition_leg,
    resolve_round_dispatch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
    build_dispatch_authorization_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)

ISSUE, WS, LANE = "13999", "wsabc", "dogfood13892"
NAME = "mzb1_wsabc_claude_dogfood13892"
ACTION = "act-1"
REVIEW_GATE = "[mozyo:workflow-event:gate=review_request]"


def _auth_note(action_id=ACTION, lane_id=LANE, workspace_id=WS):
    return build_dispatch_authorization_marker(
        action_id=action_id, source_gate="start", issue=ISSUE,
        workspace_id=workspace_id, lane_id=lane_id, target_assigned_name=NAME,
    )


def _entry(journal, notes):
    return RedmineJournalEntry(issue_id=ISSUE, journal_id=journal, notes=notes)


class _Identity:
    workspace_id = WS
    lane_id = LANE


class _Outcome:
    def __init__(self, role=ROLE_DELEGATED_COORDINATOR, journal="200"):
        self.caller_role = role
        self.durable_anchor = f"redmine:issue={ISSUE}:journal={journal}"


class RoundDispatchResolutionTest(unittest.TestCase):
    """Which dispatch round does THIS review_request terminate? Cardinality decides."""

    def _resolve(self, entries, terminal="200"):
        return resolve_round_dispatch(
            entries, workspace_id=WS, lane_id=LANE, terminal_journal=terminal
        )

    def test_the_single_open_round_resolves(self):
        auth = self._resolve([_entry("100", _auth_note()), _entry("200", REVIEW_GATE)])
        self.assertIsNotNone(auth)
        self.assertEqual(auth.action_id, ACTION)

    def test_a_prior_closed_round_is_not_reopened(self):
        """R1's AUTHORIZE belongs to R1's review_request, not R2's."""
        auth = self._resolve(
            [
                _entry("100", _auth_note(action_id="r1")),
                _entry("200", REVIEW_GATE),
                _entry("300", _auth_note(action_id="r2")),
                _entry("400", REVIEW_GATE),
            ],
            terminal="400",
        )
        self.assertIsNotNone(auth)
        self.assertEqual(auth.action_id, "r2", "the round this gate closes is the open one")

    def test_two_authorizes_in_one_round_are_ambiguous(self):
        self.assertIsNone(
            self._resolve(
                [
                    _entry("100", _auth_note(action_id="a")),
                    _entry("150", _auth_note(action_id="b")),
                    _entry("200", REVIEW_GATE),
                ]
            ),
            "two AUTHORIZE markers in one round: which action a discharge names is unknown",
        )

    def test_no_authorize_resolves_nothing(self):
        self.assertIsNone(self._resolve([_entry("200", REVIEW_GATE)]))

    def test_a_foreign_lane_authorize_is_not_this_lane_s_round(self):
        self.assertIsNone(
            self._resolve(
                [_entry("100", _auth_note(lane_id="other_lane")), _entry("200", REVIEW_GATE)]
            )
        )


class GatewayDispositionLegTest(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace()
        self.appended = []
        self.entries = [_entry("100", _auth_note()), _entry("200", REVIEW_GATE)]
        outer = self

        class Src:
            def read_entries(self, issue_id):
                return list(outer.entries)

        self.src = Src()

    def _append(self, issue, note):
        self.appended.append((issue, note))
        self.entries.append(_entry("300", note))

    def _run(self, outcome=None):
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.gateway_disposition_intake as mod  # noqa: E501

        real = mod._sender_identity
        mod._sender_identity = lambda args: _Identity()
        try:
            return execute_gateway_disposition_leg(
                self.args, outcome or _Outcome(),
                source=self.src, append_note=self._append,
            )
        finally:
            mod._sender_identity = real

    def test_the_gateway_records_the_disposition(self):
        result = self._run()
        self.assertEqual(result.state, LEG_ATTEMPTED)
        self.assertTrue(result.wrote, f"the leg must append: {result.reason} {result.detail}")
        self.assertEqual(len(self.appended), 1)

    def test_a_worker_lane_never_records(self):
        """Only the gateway attests a discharge — a worker's own claim is not one."""
        result = self._run(_Outcome(role="implementation_worker"))
        self.assertEqual(result.state, LEG_NOT_APPLICABLE)
        self.assertEqual(result.reason, REASON_NOT_GATEWAY_LANE)
        self.assertEqual(self.appended, [], "zero-write")

    def test_a_replay_is_idempotent_and_writes_once(self):
        self._run()
        second = self._run()
        self.assertEqual(len(self.appended), 1, "the same round must not be recorded twice")
        self.assertFalse(second.wrote)
        self.assertTrue(second.ok if hasattr(second, "ok") else True)

    def test_an_ambiguous_round_is_zero_write(self):
        self.entries.insert(1, _entry("150", _auth_note(action_id="b")))
        result = self._run()
        self.assertEqual(result.reason, REASON_DISPATCH_AMBIGUOUS)
        self.assertEqual(self.appended, [], "zero-write on ambiguity")

    def test_an_implementation_done_anchor_does_not_discharge(self):
        """A partial implementation_done is routine and truthful — it terminates nothing."""
        self.entries = [
            _entry("100", _auth_note()),
            _entry("200", "[mozyo:workflow-event:gate=implementation_done]"),
        ]
        result = self._run()
        self.assertEqual(self.appended, [], "zero-write: only review_request terminates")
        self.assertFalse(result.wrote)

    def test_the_recorded_marker_discharges_the_retirement_reader(self):
        """The round trip: what the gateway wrote is what the retirement reader accepts."""
        from pathlib import Path

        from mozyo_bridge.core.state.dispatch_outbox_fence import TargetObligation
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
            LiveSessionRetireOps,
        )

        self.assertTrue(self._run().wrote)

        ops = LiveSessionRetireOps(repo_root=Path("."))
        ops._redmine_source = lambda: self.src
        row = TargetObligation(
            target_assigned_name=NAME, state="delivered", issue=ISSUE, journal="100",
            action_id=ACTION, workspace_id=WS, lane_id=LANE,
        )
        self.assertIs(
            ops._durable_disposition(row), True,
            "the marker the gateway wrote must lift the reader's over-block — otherwise the "
            "writer and the reader do not share a contract and the rail is still broken",
        )


class ProductionEntryReachesTheLegTest(unittest.TestCase):
    """`mozyo-bridge workflow step` — the gateway's own surface — must reach the writer.

    Testing the leg alone would repeat R6-F1 one level up: a leg with no production caller is
    exactly as unreachable as a writer with no production caller. This drives the real
    `cmd_workflow_step`.
    """

    def _drive(self, *, dry_run=False):
        import contextlib
        import io

        from unittest.mock import patch

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            cli_workflow,
        )
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.gateway_disposition_intake as intake  # noqa: E501

        seen = []

        def _spy(args, outcome):
            seen.append(outcome)
            return None

        args = argparse.Namespace(
            dry_run=dry_run, as_json=False, session=None, issue=None, journal=None,
            callback=None, store_path=None,
        )
        out = io.StringIO()
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
            cli_workflow, "_herdr_step_preflight", lambda _a: None
        ), patch.object(
            cli_workflow, "current_pane", lambda: "%self"
        ), patch.object(
            cli_workflow, "_discover_candidates", return_value=[]
        ), patch.object(
            cli_workflow, "_load_store_action", return_value=(None, "store_absent")
        ), patch.object(
            intake, "execute_gateway_disposition_leg", _spy
        ), contextlib.redirect_stdout(out):
            cli_workflow.cmd_workflow_step(args)
        return seen

    def test_the_step_reaches_the_disposition_leg(self):
        self.assertEqual(
            len(self._drive()), 1,
            "workflow step must invoke the gateway disposition leg; without a production "
            "caller no marker can ever exist in live Redmine (R6-F1)",
        )

    def test_a_dry_run_never_reaches_the_writer(self):
        self.assertEqual(
            self._drive(dry_run=True), [],
            "a dry run that appended a durable marker would not be a dry run",
        )


if __name__ == "__main__":
    unittest.main()
