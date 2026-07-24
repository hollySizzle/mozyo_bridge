"""Unit: ``sublane recover-gateway`` live wiring (Redmine #14203, review j#87356 F1).

The CLI constructs the LIVE composition root and runs the real use case (never a staged
seam). These tests exercise the wiring hermetically: the herdr binary env points at a
nonexistent path and no Redmine credentials are set, so every live boundary fails CLOSED —
the preflight honestly reports ``turn_unobservable`` + ``identity_unknown`` with zero
process effect, and an ``--execute`` refuses. The live adapter's observation / resume logic
is pinned at the module seams with fakes (no live herdr, no live Redmine, no real process).
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_gateway_recovery_live as live_mod,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.fresh_coordinator_drain import (  # noqa: E501
    DRAIN_SEND_ERROR,
    DRAIN_SEND_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery import (  # noqa: E501
    GatewayRefreshRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery_cli import (  # noqa: E501
    SEAM_UNAVAILABLE_VERDICT,
    cmd_sublane_recover_gateway,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery_live import (  # noqa: E501
    LiveGatewayRecoveryOps,
    port_pin_request,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
    REFRESH_BLOCK_UNKNOWN,
    TURN_CLASS_UNOBSERVABLE,
)
from mozyo_bridge.core.state.replacement_transaction import ContinuationPointer


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        issue="14203", lane="issue_x_lane", role="codex", provider="codex",
        assigned_name="gw", locator="w:3", journal="", action_id="",
        action_generation=0, gateway_revision="", lane_revision="",
        lane_generation="", resume_anchor_journal="87251", resume_gate="review_request",
        reason_token="", execute=False, json=True, repo=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _request(**overrides) -> GatewayRefreshRequest:
    base = dict(
        issue="14203", lane="issue_x_lane", role="codex", provider="codex",
        assigned_name="gw", locator="w:3", resume_anchor_journal="87251",
        resume_gate="review_request",
    )
    base.update(overrides)
    return GatewayRefreshRequest(**base)


class _Entry:
    def __init__(self, journal_id, notes):
        self.journal_id = journal_id
        self.notes = notes


class LiveWiringFailClosedTests(unittest.TestCase):
    """Hermetic CLI runs: every live boundary unavailable => fail-closed, zero effect."""

    def _run(self, **overrides):
        out = io.StringIO()
        env = {
            "MOZYO_HERDR_BINARY": "/nonexistent/herdr-binary-for-tests",
            "PATH": "/nonexistent",
        }
        with patch.dict("os.environ", env, clear=False), redirect_stdout(out):
            code = cmd_sublane_recover_gateway(_args(**overrides))
        return code, json.loads(out.getvalue())

    def test_a_hermetic_preflight_fails_closed_with_zero_effect(self):
        with tempfile.TemporaryDirectory() as repo:
            code, payload = self._run(repo=repo, execute=False)
        # Every live boundary is unavailable: the turn is honestly unobservable and the
        # target unresolvable (or the workspace identity itself) — NEVER a fabricated
        # classification, never a process effect.
        self.assertEqual(payload["turn_class"], TURN_CLASS_UNOBSERVABLE)
        self.assertIn(
            payload["verdict"], (REFRESH_BLOCK_UNKNOWN, SEAM_UNAVAILABLE_VERDICT)
        )
        self.assertFalse(payload["closed_old_gateway"])
        self.assertFalse(payload["fresh_slot_attested"])
        if payload["verdict"] == SEAM_UNAVAILABLE_VERDICT:
            self.assertEqual(code, 1)
        else:
            self.assertEqual(code, 0)  # a preflight reporting a blocker is exit 0

    def test_a_hermetic_execute_refuses_with_zero_effect(self):
        with tempfile.TemporaryDirectory() as repo:
            code, payload = self._run(repo=repo, execute=True)
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "refused")
        self.assertTrue(payload["executed"])
        self.assertFalse(payload["closed_old_gateway"])
        self.assertFalse(payload["fresh_slot_attested"])


class LiveOpsObservationTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.ops = LiveGatewayRecoveryOps(repo_root=self.repo, request=_request())

    def _rows(self):
        return [
            {"name": "mzb1_ws_codex_lane", "pane_id": "w:3", "status": "done",
             "revision": "4", "cwd": str(self.repo)},
        ]

    def test_an_unreadable_inventory_is_identity_unknown(self):
        with patch.object(
            live_mod, "list_herdr_agent_rows", side_effect=RuntimeError("no herdr")
        ):
            obs = self.ops.observe_target(_request())
        self.assertFalse(obs.identity_resolved)

    def test_expected_gate_facts_require_a_fresh_reader(self):
        # No reader / a non-fresh reader NEVER asserts absence (turn_unobservable).
        self.assertEqual(self.ops._expected_gate_facts(_request()), (False, False, False))
        self.ops.journal_reader = lambda issue: []
        self.ops.journal_reader_fresh = False
        self.assertEqual(self.ops._expected_gate_facts(_request()), (False, False, False))

    def test_expected_gate_facts_are_anchored_and_ordered(self):
        marker = "[mozyo:workflow-event:gate=review_result:conclusion=approved:req=87251]"
        self.ops.journal_reader_fresh = True
        # A gate BEFORE/AT the anchor does not count; absence is positively confirmed.
        self.ops.journal_reader = lambda issue: [
            _Entry("87200", marker), _Entry("87251", marker),
        ]
        self.assertEqual(self.ops._expected_gate_facts(_request()), (False, True, True))
        # A gate STRICTLY after the anchor lands.
        self.ops.journal_reader = lambda issue: [_Entry("87300", marker)]
        self.assertEqual(self.ops._expected_gate_facts(_request()), (True, False, True))
        # Non-gate prose after the anchor is not a landing.
        self.ops.journal_reader = lambda issue: [_Entry("87300", "prose only")]
        self.assertEqual(self.ops._expected_gate_facts(_request()), (False, True, True))
        # An unreadable reader is unobservable, never "absent".
        def _boom(issue):
            raise RuntimeError("source down")
        self.ops.journal_reader = _boom
        self.assertEqual(self.ops._expected_gate_facts(_request()), (False, False, False))

    def test_resume_once_never_sends_without_a_distinct_fresh_gateway(self):
        continuation = ContinuationPointer(
            source="redmine", issue_id="14203", journal_id="87251",
            expected_gate="review_request", next_semantic_action="callback_recovery_once",
        )
        driven: list = []
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=self._rows()):
            with patch.object(
                self.ops, "_drive_cli", side_effect=lambda argv: driven.append(argv) or 0
            ):
                with patch.object(
                    self.ops, "_providers", return_value=("claude", "codex")
                ):
                    # The only row still carries the OLD locator -> never a blind send.
                    result = self.ops.resume_once(continuation)
        self.assertEqual(result, DRAIN_SEND_ERROR)
        self.assertEqual(driven, [])

    def test_resume_once_drives_the_governed_rail_with_the_existing_anchor(self):
        continuation = ContinuationPointer(
            source="redmine", issue_id="14203", journal_id="87251",
            expected_gate="review_request", next_semantic_action="callback_recovery_once",
        )
        fresh_rows = [
            {"name": "gw", "pane_id": "w:9", "status": "idle"},
        ]
        driven: list = []
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=fresh_rows):
            with patch.object(
                self.ops, "_drive_cli", side_effect=lambda argv: driven.append(argv) or 0
            ):
                with patch.object(
                    self.ops, "_providers", return_value=("claude", "codex")
                ):
                    result = self.ops.resume_once(continuation)
        self.assertEqual(result, DRAIN_SEND_OK)
        self.assertEqual(len(driven), 1)
        argv = driven[0]
        # The governed handoff rail, carrying the EXISTING anchor + its immutable gate kind
        # (never a regenerated request) to the FRESH gateway locator, lane-pinned.
        self.assertEqual(argv[:2], ["handoff", "send"])
        self.assertIn("--journal", argv)
        self.assertEqual(argv[argv.index("--journal") + 1], "87251")
        self.assertEqual(argv[argv.index("--kind") + 1], "reply")  # the j#84223 pointer shape
        self.assertEqual(argv[argv.index("--target") + 1], "w:9")
        self.assertEqual(argv[argv.index("--target-lane") + 1], "issue_x_lane")

    def test_port_pin_request_maps_the_gateway_revision(self):
        pin = port_pin_request(_request(gateway_revision="7", lane_revision="5",
                                        lane_generation="2"))
        self.assertEqual(pin.worker_revision, "7")
        self.assertEqual(pin.lane_revision, "5")
        self.assertEqual(pin.lane_generation, "2")
        self.assertEqual(pin.assigned_name, "gw")


class ReviewR2AdversarialTests(unittest.TestCase):
    """The j#87364 F1-F5 adversarial shapes, pinned at the live-adapter seams."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.ops = LiveGatewayRecoveryOps(repo_root=self.repo, request=_request())

    def test_f3_lane_owning_issue_is_exact_parsed_never_prefix(self):
        lane = "issue_13490_single_entry_e2e_r1"
        self.assertEqual(live_mod._lane_owning_issue(lane), "13490")
        self.assertNotEqual(live_mod._lane_owning_issue(lane), "1349")  # prefix never matches
        self.assertEqual(live_mod._lane_owning_issue("not_a_lane"), "")  # unparsable => ""

    def test_f4_an_unrelated_gate_after_the_anchor_is_not_landed(self):
        # An owner_approval (or any non-causal gate) after a review_request anchor must NOT
        # read as the failed turn's response — absence stays positively confirmed.
        self.ops.journal_reader_fresh = True
        self.ops.journal_reader = lambda issue: [
            _Entry("87300", "[mozyo:workflow-event:gate=owner_approval]"),
        ]
        self.assertEqual(self.ops._expected_gate_facts(_request()), (False, True, True))
        # Only the causally-linked review_result (req=<anchor>) lands.
        self.ops.journal_reader = lambda issue: [
            _Entry("87300",
                   "[mozyo:workflow-event:gate=review_result:conclusion=approved:req=87251]"),
        ]
        self.assertEqual(self.ops._expected_gate_facts(_request()), (True, False, True))
        # A review_result correlated to a DIFFERENT request does not land.
        self.ops.journal_reader = lambda issue: [
            _Entry("87300",
                   "[mozyo:workflow-event:gate=review_result:conclusion=approved:req=99999]"),
        ]
        self.assertEqual(self.ops._expected_gate_facts(_request()), (False, True, True))

    def test_f4_an_uncorrelatable_anchor_kind_is_unobservable(self):
        self.ops.journal_reader_fresh = True
        self.ops.journal_reader = lambda issue: []
        req = _request(resume_gate="reply")
        self.assertEqual(self.ops._expected_gate_facts(req), (False, False, False))

    def test_f1_the_anchor_issue_is_a_separate_authority(self):
        # The parent-lane/child-issue topology: lane owned by 13490, anchors on 14203.
        ops = LiveGatewayRecoveryOps(
            repo_root=self.repo,
            request=_request(issue="13490", anchor_issue="14203"),
        )
        seen: list = []
        ops.journal_reader_fresh = True
        ops.journal_reader = lambda issue: seen.append(issue) or []
        ops._expected_gate_facts(ops.request)
        self.assertEqual(seen, ["14203"])  # the durable read targets the ANCHOR issue
        self.assertEqual(ops._anchor_issue(), "14203")
        # Empty anchor_issue falls back to the lane-owning issue.
        self.assertEqual(
            LiveGatewayRecoveryOps(
                repo_root=self.repo, request=_request(issue="13490")
            )._anchor_issue(),
            "13490",
        )

    def test_f5_an_empty_pinned_revision_never_matches(self):
        rows = [{
            "name": "gw", "pane_id": "w:3", "status": "done", "revision": "4",
            "cwd": str(self.repo),
        }]
        with patch.object(live_mod, "list_herdr_agent_rows", return_value=rows):
            with patch.object(live_mod, "repo_scope_workspace_id", return_value="ws"):
                ops = LiveGatewayRecoveryOps(
                    repo_root=self.repo, request=_request(gateway_revision="")
                )
                with patch.object(
                    live_mod, "decode_assigned_name",
                    return_value=type("D", (), {
                        "ok": True,
                        "identity": type("I", (), {
                            "workspace_id": "ws", "lane_id": "issue_x_lane",
                            "role": "codex",
                        })(),
                    })(),
                ):
                    with patch.object(
                        ops, "_providers", return_value=("claude", "codex")
                    ):
                        with patch.object(ops, "_composer_clear", return_value=True):
                            obs = ops.observe_target(ops.request)
        self.assertFalse(obs.generation_matches)  # empty pin is NEVER a match

    def test_f2_resume_rail_ready_requires_the_attested_sender_env_triad(self):
        bare = LiveGatewayRecoveryOps(repo_root=self.repo, request=_request(), env={})
        self.assertFalse(bare.resume_rail_ready(bare.request))
        attested = LiveGatewayRecoveryOps(
            repo_root=self.repo, request=_request(),
            env={
                "MOZYO_WORKSPACE_ID": "ws", "MOZYO_AGENT_ROLE": "claude",
                "MOZYO_LANE_ID": "issue_x_lane",
            },
        )
        self.assertTrue(attested.resume_rail_ready(attested.request))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
