"""Redmine #13892 R7-F1 — a disposition refusal must be visible on the step envelope.

The leg returned a structured `DispositionLegResult`, and `cmd_workflow_step` threw it away:
no text, no JSON, no rc. In this gateway's own environment `MOZYO_REDMINE_DELIVERY_WRITE` is
unset, so `_live_append_note()` is None and a standard step silently records nothing. The
operator is never told, and once the review result posts the verified anchor moves past that
round — so nothing ever retries it, the delivered row is owed forever, and Acceptance 2/6's
over-block removal never happens in the normal flow. Fail-closed is only safe when someone is
told.

These drive the REAL `cmd_workflow_step` with a real gateway-shaped `WorkflowStepOutcome` and
read its actual stdout, in both text and JSON. Asserting the leg was *called* (R6's test) does
not prove the operator can see the answer.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (  # noqa: E501
    ROLE_DELEGATED_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    cli_workflow,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    gateway_disposition_intake as intake,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
    build_dispatch_authorization_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (  # noqa: E501
    EXECUTION_BLOCKED,
    OWNER_OPERATOR,
    PRIMITIVE_NONE,
    STATE_LANE_UNRESOLVED,
    WorkflowStepOutcome,
)

ISSUE, WS, LANE = "13999", "wsabc", "dogfood13892"
NAME = "mzb1_wsabc_claude_dogfood13892"
ACTION = "act-1"
REVIEW_GATE = "[mozyo:workflow-event:gate=review_request]"


class _Identity:
    workspace_id = WS
    lane_id = LANE


def _gateway_outcome():
    """A real `WorkflowStepOutcome` shaped like a gateway lane on a verified review_request."""
    return WorkflowStepOutcome(
        state=STATE_LANE_UNRESOLVED,
        next_action="hold",
        execution=EXECUTION_BLOCKED,
        reason="fixture",
        next_owner=OWNER_OPERATOR,
        primitive=PRIMITIVE_NONE,
        durable_anchor=f"redmine:issue={ISSUE}:journal=200",
        caller_role=ROLE_DELEGATED_COORDINATOR,
        repo_root=".",
    )


class DispositionSurfacedOnStepTest(unittest.TestCase):
    def setUp(self):
        self.appended = []
        self.entries = [
            RedmineJournalEntry(
                issue_id=ISSUE,
                journal_id="100",
                notes=build_dispatch_authorization_marker(
                    action_id=ACTION, source_gate="start", issue=ISSUE, workspace_id=WS,
                    lane_id=LANE, target_assigned_name=NAME,
                ),
            ),
            RedmineJournalEntry(issue_id=ISSUE, journal_id="200", notes=REVIEW_GATE),
        ]
        outer = self

        class Src:
            def read_entries(self, issue_id):
                return list(outer.entries)

        self.src = Src()

    def _append(self, issue, note):
        self.appended.append((issue, note))
        self.entries.append(RedmineJournalEntry(issue_id=ISSUE, journal_id="300", notes=note))

    def _step(self, *, as_json=False, dry_run=False, source=True, append=True, source_obj=None):
        """Drive the REAL `cmd_workflow_step`, returning its stdout."""
        args = argparse.Namespace(
            dry_run=dry_run, as_json=as_json, session=None, issue=None, journal=None,
            callback=None, store_path=None,
        )
        out = io.StringIO()
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
            cli_workflow, "_herdr_step_preflight", lambda _a: _gateway_outcome()
        ), patch.object(
            cli_workflow, "_load_store_action", return_value=(None, "store_absent")
        ), patch.object(
            intake, "_sender_identity", lambda args: _Identity()
        ), patch.object(
            intake, "_live_source", lambda: (source_obj or self.src) if source else None
        ), patch.object(
            intake, "_live_append_note", lambda: self._append if append else None
        ), contextlib.redirect_stdout(out):
            cli_workflow.cmd_workflow_step(args)
        return out.getvalue()

    # --- (a) success: production entry -> source/append -> writer -> reader --------

    def test_a_recorded_disposition_is_reported_and_readable_by_the_reader(self):
        text = self._step()
        self.assertEqual(len(self.appended), 1, "the standard step must append the marker")
        self.assertIn("dispatch disposition: recorded", text)

        # The round trip: the reader that over-blocks must accept what this step wrote.
        from pathlib import Path

        from mozyo_bridge.core.state.dispatch_outbox_fence import TargetObligation
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
            LiveSessionRetireOps,
        )

        ops = LiveSessionRetireOps(repo_root=Path("."))
        ops._redmine_source = lambda: self.src
        row = TargetObligation(
            target_assigned_name=NAME, state="delivered", issue=ISSUE, journal="100",
            action_id=ACTION, workspace_id=WS, lane_id=LANE,
        )
        self.assertIs(ops._durable_disposition(row), True)

    def test_the_json_envelope_carries_the_disposition(self):
        payload = json.loads(self._step(as_json=True))
        self.assertIn("dispatch_disposition", payload)
        self.assertTrue(payload["dispatch_disposition"]["wrote"])

    # --- (b) refusals: zero-write AND visible -------------------------------------

    def test_write_opt_in_unset_is_zero_write_and_visible_in_text(self):
        """This gateway's real environment. It must not fail silently."""
        text = self._step(append=False)
        self.assertEqual(self.appended, [], "zero-write")
        self.assertIn("NOT recorded", text)
        self.assertIn("write_opt_in_unset", text)

    def test_write_opt_in_unset_is_visible_in_json(self):
        payload = json.loads(self._step(as_json=True, append=False))
        d = payload["dispatch_disposition"]
        self.assertFalse(d["wrote"])
        self.assertEqual(d["reason"], "write_opt_in_unset")

    def test_an_unreadable_source_is_zero_write_and_visible(self):
        class Boom:
            def read_entries(self, issue_id):
                raise RuntimeError("credential failure")

        payload = json.loads(self._step(as_json=True, source_obj=Boom()))
        self.assertEqual(self.appended, [])
        self.assertEqual(payload["dispatch_disposition"]["reason"], "source_unreadable")

    def test_an_ambiguous_round_is_zero_write_and_visible(self):
        self.entries.insert(
            1,
            RedmineJournalEntry(
                issue_id=ISSUE,
                journal_id="150",
                notes=build_dispatch_authorization_marker(
                    action_id="act-2", source_gate="start", issue=ISSUE, workspace_id=WS,
                    lane_id=LANE, target_assigned_name=NAME,
                ),
            ),
        )
        payload = json.loads(self._step(as_json=True))
        self.assertEqual(self.appended, [], "zero-write on ambiguity")
        self.assertEqual(
            payload["dispatch_disposition"]["reason"], "dispatch_authorize_ambiguous"
        )

    def test_a_raising_leg_is_reported_not_swallowed(self):
        """An escaped exception must not look like 'nothing to record'."""

        def _boom(*a, **k):
            raise RuntimeError("unexpected")

        with patch.object(intake, "execute_gateway_disposition_leg", _boom):
            payload = json.loads(self._step(as_json=True))
        d = payload["dispatch_disposition"]
        self.assertEqual(d["state"], "error")
        self.assertEqual(d["reason"], "leg_raised")
        self.assertFalse(d["wrote"])

    # --- (c) dry-run ---------------------------------------------------------------

    def test_a_dry_run_is_zero_write(self):
        self._step(dry_run=True)
        self.assertEqual(self.appended, [], "a dry run that appended would not be a dry run")

    # --- ordinary steps stay unchanged ---------------------------------------------

    def test_a_worker_lane_step_carries_no_disposition_field(self):
        """Additive: a step that never concerned a disposition reports exactly as before."""
        args = argparse.Namespace(
            dry_run=False, as_json=True, session=None, issue=None, journal=None,
            callback=None, store_path=None,
        )
        worker = WorkflowStepOutcome(
            state=STATE_LANE_UNRESOLVED, next_action="hold", execution=EXECUTION_BLOCKED,
            reason="fixture", next_owner=OWNER_OPERATOR, primitive=PRIMITIVE_NONE,
            durable_anchor=f"redmine:issue={ISSUE}:journal=200",
            caller_role="implementation_worker", repo_root=".",
        )
        out = io.StringIO()
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
            cli_workflow, "_herdr_step_preflight", lambda _a: worker
        ), patch.object(
            cli_workflow, "_load_store_action", return_value=(None, "store_absent")
        ), contextlib.redirect_stdout(out):
            cli_workflow.cmd_workflow_step(args)
        self.assertNotIn("dispatch_disposition", json.loads(out.getvalue()))
        self.assertEqual(self.appended, [])


if __name__ == "__main__":
    unittest.main()
