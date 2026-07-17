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


class EveryLegOutcomeIsClassifiedTest(unittest.TestCase):
    """The full early-return table: for EVERY exit, (who sees it, zero-write, visible).

    R7 surfaced the refusals I happened to be thinking about and left two exits unexamined:
    a sender-identity failure went out mislabelled AND invisible (R8-F1), and the dry-run
    branch answered before applicability so it leaked onto worker steps (R8-F3). Both were
    paths my own tests never drove. So this drives every exit rather than the interesting ones.
    """

    def _leg(self, *, role=ROLE_DELEGATED_COORDINATOR, anchor=f"redmine:issue={ISSUE}:journal=200",
             source=None, append=lambda i, n: None, sender=_Identity()):
        outcome = type("O", (), {"caller_role": role, "durable_anchor": anchor})()
        with patch.object(intake, "_sender_identity", lambda args: sender):
            return intake.execute_gateway_disposition_leg(
                argparse.Namespace(), outcome, source=source, append_note=append
            )

    def _ok_source(self):
        entries = [
            RedmineJournalEntry(
                issue_id=ISSUE, journal_id="100",
                notes=build_dispatch_authorization_marker(
                    action_id=ACTION, source_gate="start", issue=ISSUE, workspace_id=WS,
                    lane_id=LANE, target_assigned_name=NAME,
                ),
            ),
            RedmineJournalEntry(issue_id=ISSUE, journal_id="200", notes=REVIEW_GATE),
        ]
        return type("S", (), {"read_entries": lambda self, i: list(entries)})()

    # --- rows that are legitimately SILENT: not a gateway round at all ---------------

    def test_a_worker_lane_is_silent(self):
        r = self._leg(role="implementation_worker")
        self.assertFalse(r.applicable)
        self.assertEqual(r.reason, intake.REASON_NOT_GATEWAY_LANE)
        self.assertEqual(intake.disposition_payload_fields(r), {})

    def test_a_gateway_without_a_verified_anchor_is_silent(self):
        r = self._leg(anchor="none")
        self.assertFalse(r.applicable)
        self.assertEqual(r.reason, intake.REASON_NO_VERIFIED_ANCHOR)
        self.assertEqual(intake.disposition_payload_fields(r), {})

    # --- every other row: applicable, zero-write, VISIBLE ----------------------------

    def test_a_sender_identity_failure_is_named_and_visible(self):
        """R8-F1: the anchor IS verified; what failed is the sender identity."""
        r = self._leg(source=self._ok_source(), sender=None)
        self.assertEqual(r.reason, intake.REASON_SENDER_UNRESOLVED)
        self.assertNotEqual(
            r.reason, intake.REASON_NO_VERIFIED_ANCHOR, "that names the wrong cause"
        )
        self.assertTrue(r.applicable, "a verified gateway round must never fail silently")
        self.assertFalse(r.wrote)
        self.assertFalse(r.ok)
        self.assertTrue(intake.disposition_payload_fields(r))
        self.assertTrue(intake.disposition_text_lines(r))

    def test_no_source_is_visible(self):
        with patch.object(intake, "_live_source", lambda: None):
            r = self._leg(source=None)
        self.assertEqual(r.reason, intake.REASON_SOURCE_UNAVAILABLE)
        self.assertTrue(r.applicable)
        self.assertTrue(intake.disposition_text_lines(r))

    def test_no_write_opt_in_is_visible(self):
        with patch.object(intake, "_live_append_note", lambda: None):
            r = self._leg(source=self._ok_source(), append=None)
        self.assertEqual(r.reason, intake.REASON_NO_WRITE_OPT_IN)
        self.assertTrue(r.applicable)
        self.assertTrue(intake.disposition_text_lines(r))

    def test_every_applicable_refusal_reports_zero_write_and_not_ok(self):
        """No applicable refusal may claim `ok` or `wrote`, whatever its cause."""
        for label, kwargs in (
            ("sender", dict(source=self._ok_source(), sender=None)),
            ("unreadable", dict(source=type("B", (), {
                "read_entries": lambda self, i: (_ for _ in ()).throw(RuntimeError("x"))})())),
        ):
            with self.subTest(label):
                r = self._leg(**kwargs)
                self.assertTrue(r.applicable)
                self.assertFalse(r.wrote)
                self.assertFalse(r.ok)
                self.assertEqual(r.state, intake.LEG_REFUSED)


class DryRunAppliesOnlyToGatewayRoundsTest(unittest.TestCase):
    """R8-F3: applicability is decided from role/anchor, never from how the step was invoked."""

    def _drive(self, *, role, as_json=True):
        args = argparse.Namespace(
            dry_run=True, as_json=as_json, session=None, issue=None, journal=None,
            callback=None, store_path=None,
        )
        outcome = WorkflowStepOutcome(
            state=STATE_LANE_UNRESOLVED, next_action="hold", execution=EXECUTION_BLOCKED,
            reason="fixture", next_owner=OWNER_OPERATOR, primitive=PRIMITIVE_NONE,
            durable_anchor=f"redmine:issue={ISSUE}:journal=200",
            caller_role=role, repo_root=".",
        )
        out = io.StringIO()
        with patch.object(cli_workflow, "require_tmux", lambda: None), patch.object(
            cli_workflow, "_herdr_step_preflight", lambda _a: outcome
        ), patch.object(
            cli_workflow, "_load_store_action", return_value=(None, "store_absent")
        ), contextlib.redirect_stdout(out):
            cli_workflow.cmd_workflow_step(args)
        return out.getvalue()

    def test_a_worker_dry_run_carries_no_disposition_field(self):
        """The additive contract: a step that concerns no disposition reports as before."""
        self.assertNotIn(
            "dispatch_disposition", json.loads(self._drive(role="implementation_worker"))
        )

    def test_a_worker_dry_run_prints_no_disposition_line(self):
        self.assertNotIn(
            "dispatch disposition", self._drive(role="implementation_worker", as_json=False)
        )

    def test_a_gateway_dry_run_is_surfaced_as_zero_write(self):
        d = json.loads(self._drive(role=ROLE_DELEGATED_COORDINATOR))["dispatch_disposition"]
        self.assertEqual(d["state"], intake.LEG_DRY_RUN)
        self.assertFalse(d["wrote"])

    def test_a_gateway_dry_run_text_is_not_an_alarming_refusal(self):
        text = self._drive(role=ROLE_DELEGATED_COORDINATOR, as_json=False)
        self.assertIn("dry run", text)
        self.assertNotIn("NOT recorded", text)


class IdempotentReplayIsASuccessTest(unittest.TestCase):
    """R8-F2: the writer calls a same-payload replay a success; the envelope must agree."""

    def setUp(self):
        self.appended = []
        self.entries = [
            RedmineJournalEntry(
                issue_id=ISSUE, journal_id="100",
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

    def _step(self, *, as_json):
        args = argparse.Namespace(
            dry_run=False, as_json=as_json, session=None, issue=None, journal=None,
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
            intake, "_live_source", lambda: self.src
        ), patch.object(
            intake, "_live_append_note", lambda: self._append
        ), contextlib.redirect_stdout(out):
            cli_workflow.cmd_workflow_step(args)
        return out.getvalue()

    def test_a_replayed_step_reports_success_in_json(self):
        first = json.loads(self._step(as_json=True))["dispatch_disposition"]
        second = json.loads(self._step(as_json=True))["dispatch_disposition"]
        self.assertEqual(len(self.appended), 1, "write count stays 1")
        self.assertEqual(first["state"], intake.LEG_RECORDED)
        self.assertTrue(first["ok"])
        self.assertEqual(second["state"], intake.LEG_ALREADY_RECORDED)
        self.assertTrue(second["ok"], "an idempotent replay is a success, not a refusal")
        self.assertFalse(second["wrote"], "nothing new was appended")

    def test_a_replayed_step_text_does_not_read_as_a_refusal(self):
        self._step(as_json=False)
        text = self._step(as_json=False)
        self.assertEqual(len(self.appended), 1)
        self.assertIn("already recorded", text)
        self.assertNotIn(
            "NOT recorded", text, "the writer's contract calls this a success"
        )
