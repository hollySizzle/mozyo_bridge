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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    gateway_generation_authority as gen_mod,
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
                    with patch.object(
                        self.ops, "_governed_sender_resolves", return_value=True
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

    def test_f1_rail_ready_uses_the_real_resolver_never_env_presence(self):
        # j#87370 F1 adversarial: a NON-EMPTY but foreign-workspace triad must NOT read as
        # governed-rail capable — the check is the SAME resolver the real send uses.
        foreign = LiveGatewayRecoveryOps(
            repo_root=self.repo, request=_request(),
            env={
                "MOZYO_WORKSPACE_ID": "foreign_workspace", "MOZYO_AGENT_ROLE": "claude",
                "MOZYO_LANE_ID": "issue_x_lane",
            },
        )
        with patch.object(live_mod, "repo_scope_workspace_id", return_value="real_ws"):
            self.assertFalse(foreign._governed_sender_resolves())
            # …and rail readiness then depends ONLY on the operator-capable one-shot rail.
            with patch.object(
                foreign,
                "_recovery_delivery_service",
                return_value=type("S", (), {"ready": lambda self: False})(),
            ):
                self.assertFalse(foreign.resume_rail_ready(foreign.request))
            with patch.object(
                foreign,
                "_recovery_delivery_service",
                return_value=type("S", (), {"ready": lambda self: True})(),
            ):
                self.assertTrue(foreign.resume_rail_ready(foreign.request))
        # A matching-workspace attested context resolves through the same resolver.
        attested = LiveGatewayRecoveryOps(
            repo_root=self.repo, request=_request(),
            env={
                "MOZYO_WORKSPACE_ID": "real_ws", "MOZYO_AGENT_ROLE": "claude",
                "MOZYO_LANE_ID": "issue_x_lane",
            },
        )
        with patch.object(live_mod, "repo_scope_workspace_id", return_value="real_ws"):
            self.assertTrue(attested._governed_sender_resolves())

    def test_f1_oneshot_transport_unwraps_the_production_resolution_path(self):
        # j#87378 F1: the resolver returns a REAL HerdrBinaryResolution; the transport must
        # receive its ``.path`` — pinned with the production type, not a fake shape.
        import os
        import stat
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_anchor_delivery_live import (  # noqa: E501
            LiveRecoveryAnchorDeliveryService,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
            HerdrCliTransport,
        )

        fake_bin = self.repo / "herdr"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(fake_bin, os.stat(fake_bin).st_mode | stat.S_IXUSR)
        ops = LiveGatewayRecoveryOps(
            repo_root=self.repo, request=_request(),
            env={"MOZYO_HERDR_BINARY": str(fake_bin)},
        )
        service = LiveRecoveryAnchorDeliveryService(self.repo, ops.env)
        rail = service._build_rail()
        self.assertIsInstance(rail._transport, HerdrCliTransport)
        self.assertEqual(rail._transport.binary, str(fake_bin))

    def test_f1_the_oneshot_rail_builds_from_the_exact_resolved_transport(self):
        # j#87384 F1: with a resolvable herdr binary the rail constructs (non-None) from the
        # SAME transport authority; with none it is None — and rail readiness judges the
        # SAME capability. Never resolved via the default (tmux) config path.
        import os
        import stat
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (  # noqa: E501
            HerdrTurnStartRail,
        )

        fake_bin = self.repo / "herdr"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(fake_bin, os.stat(fake_bin).st_mode | stat.S_IXUSR)
        ops = LiveGatewayRecoveryOps(
            repo_root=self.repo, request=_request(),
            env={"MOZYO_HERDR_BINARY": str(fake_bin)},
        )
        rail = ops._recovery_delivery_service()._build_rail()
        self.assertIsInstance(rail, HerdrTurnStartRail)
        with patch.object(ops, "_governed_sender_resolves", return_value=False):
            self.assertTrue(ops.resume_rail_ready(ops.request))

    def test_f2_a_clean_env_without_herdr_is_rail_unavailable_and_green(self):
        # j#87384 F2: with MOZYO_HERDR_BINARY unset and no herdr on PATH, the rail is
        # unavailable (None) and readiness is False — asserted hermetically so the suite
        # never depends on the host's herdr installation.
        ops = LiveGatewayRecoveryOps(
            repo_root=self.repo, request=_request(),
            env={"PATH": str(self.repo / "empty-path")},
        )
        service = ops._recovery_delivery_service()
        self.assertIsNone(service._build_rail())
        self.assertFalse(service.ready())
        with patch.object(ops, "_governed_sender_resolves", return_value=False):
            self.assertFalse(ops.resume_rail_ready(ops.request))

    def test_f2_oneshot_requires_action_bound_attestation_and_generation(self):
        # The wrapper carries the exact revision + refresh action into the shared service;
        # the service regressions own the identity/attestation adversarial matrix.
        ops = LiveGatewayRecoveryOps(
            repo_root=self.repo,
            request=_request(
                assigned_name="gw", action_id="refresh-gateway:a:r4"
            ),
        )
        row = {"name": "gw", "pane_id": "w:9", "status": "done", "revision": "2"}
        seen = []
        service = type(
            "S",
            (),
            {
                "deliver": lambda self, request: (
                    seen.append(request)
                    or type("O", (), {"started": True})()
                )
            },
        )()
        continuation = ContinuationPointer(
            source="redmine",
            issue_id="14203",
            journal_id="87251",
            expected_gate="implementation_request",
            next_semantic_action="callback_recovery_once",
        )
        with patch.object(live_mod, "repo_scope_workspace_id", return_value="ws"), \
                patch.object(ops, "_rows", return_value=[row]), \
                patch.object(ops, "_recovery_delivery_service", return_value=service):
            self.assertEqual(
                ops._oneshot_resume(continuation, "w:9", "codex"),
                DRAIN_SEND_OK,
            )
        self.assertEqual(seen[0].target_revision, "2")
        self.assertEqual(seen[0].target_action_id, "refresh-gateway:a:r4")

    def test_f2_oneshot_records_ok_only_on_an_observed_turn_start(self):
        # The wrapper promotes only the shared service's typed ``started`` result.
        continuation = ContinuationPointer(
            source="redmine", issue_id="14203", journal_id="87251",
            expected_gate="implementation_request",
            next_semantic_action="callback_recovery_once",
        )
        ops = LiveGatewayRecoveryOps(
            repo_root=self.repo,
            request=_request(assigned_name="gw", action_id="refresh-gateway:a:r4"),
        )
        row = {"name": "gw", "pane_id": "w:9", "status": "done", "revision": "2"}
        def _run(started):
            with patch.object(live_mod, "repo_scope_workspace_id", return_value="ws"):
                with patch.object(ops, "_rows", return_value=[row]):
                    service = type(
                        "S",
                        (),
                        {
                            "deliver": lambda self, request: type(
                                "O", (), {"started": started}
                            )()
                        },
                    )()
                    with patch.object(
                        ops, "_recovery_delivery_service", return_value=service
                    ):
                        return ops._oneshot_resume(
                            continuation, "w:9", "codex"
                        )

        self.assertEqual(_run(True), DRAIN_SEND_OK)
        self.assertEqual(_run(False), DRAIN_SEND_ERROR)

    def test_f3_an_implementation_request_anchor_lands_on_the_worker_forward_record(self):
        # j#87370 F3: the IR's causal result is the exact-anchor gateway→worker forward in
        # the REAL ledger — readable-but-empty confirms absence; a record for a DIFFERENT
        # anchor never lands.
        from types import SimpleNamespace

        req = _request(resume_gate="implementation_request")
        ops = LiveGatewayRecoveryOps(repo_root=self.repo, request=req)
        marker_hits: dict = {}

        class _Ledger:
            def records_for_marker(self, marker):
                return marker_hits.get(marker, [])

        ops.ledger = _Ledger()
        with patch.object(ops, "_providers", return_value=("claude", "codex")), \
                patch.object(ops, "_same_lane_worker_locator", return_value="w:4"):
            # Readable, no forward record -> absence positively confirmed.
            self.assertEqual(ops._expected_gate_facts(req), (False, True, True))
            # The exact-anchor worker-forward record lands.
            from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
                RedmineAnchor,
                build_marker,
            )

            marker = build_marker(
                RedmineAnchor(issue="14203", journal="87251"),
                "implementation_request", "claude",
            )
            # j#87378 F3 adversarial: a record for the SAME anchor sent to a FOREIGN
            # target never lands — only the current same-lane worker's exact locator.
            marker_hits[marker] = [SimpleNamespace(
                notification_marker=marker, source="redmine", issue_id="14203",
                journal_id="87251", receiver="claude", backend="herdr",
                target="w:FOREIGN", status="sent", reason="ok",
            )]
            self.assertEqual(ops._expected_gate_facts(req), (False, True, True))
            marker_hits[marker] = [SimpleNamespace(
                notification_marker=marker, source="redmine", issue_id="14203",
                journal_id="87251", receiver="claude", backend="herdr",
                target="w:4", status="sent", reason="ok",
            )]
            self.assertEqual(ops._expected_gate_facts(req), (True, False, True))
        # An unresolvable same-lane worker is UNOBSERVABLE, never a guess.
        with patch.object(ops, "_providers", return_value=("claude", "codex")), \
                patch.object(ops, "_same_lane_worker_locator", return_value=""):
            self.assertEqual(ops._expected_gate_facts(req), (False, False, False))


class RowRevisionCloseBoundaryTests(unittest.TestCase):
    """j#87370 F2: the pinned row revision is re-verified at the CLOSE boundary."""

    def test_a_row_revision_drift_blocks_the_close_boundary(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            sublane_stale_worker_recovery_live as stale_live,
        )
        from mozyo_bridge.core.state.replacement_transaction import (
            ParticipantPin,
            ReplacementTransactionKey,
            ReplacementTransactionStore,
        )

        repo = Path(tempfile.mkdtemp())
        store = ReplacementTransactionStore(home=repo)
        # Approval pinned at row revision 1; the live row was recycled to revision 2 with the
        # SAME name + locator. The close boundary must block (identity_matches=False) with the
        # drift axis named — never close the new generation under the old approval.
        request = port_pin_request(_request(gateway_revision="1"))
        port = stale_live.LiveRecoveryActuatorPort(
            repo_root=repo, request=request, store=store,
            key=ReplacementTransactionKey("ws", "refresh-gateway:t"),
        )
        pin = ParticipantPin(
            lane_id="issue_x_lane", role="codex", provider="codex",
            assigned_name="gw", old_locator="w:3", is_self=False,
            lane_revision="5", lane_generation="2",
        )
        drifted_row = {"name": "gw", "pane_id": "w:3", "status": "done", "revision": "2"}
        with patch.object(
            port, "_exact_and_matches", return_value=([drifted_row], [drifted_row], [drifted_row])
        ):
            obs = port.observe_preservation(pin)
        self.assertFalse(obs.identity_matches)
        self.assertIn("row_revision_drift", obs.detail)
        self.assertIn("pinned=1", obs.detail)
        self.assertIn("live=2", obs.detail)
        # The exact pinned revision passes this axis (later axes evaluate normally).
        matching_row = {"name": "gw", "pane_id": "w:3", "status": "done", "revision": "1"}
        with patch.object(
            port, "_exact_and_matches",
            return_value=([matching_row], [matching_row], [matching_row]),
        ):
            obs2 = port.observe_preservation(pin)
        self.assertNotIn("row_revision_drift", obs2.detail or "")


class TurnStartAuthorityTests(unittest.TestCase):
    """j#87397: the turn-start authority is the ANCHOR delivery record's own rail telemetry."""

    #: The default legitimate live observed_at (a diagnostic field only; generations are
    #: compared by the collision-free per-launch token, NEVER this timestamp).
    live_attestation_observed_at = "2026-07-24T17:00:00+00:00"
    WS = "ws"

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.attn_home = Path(tempfile.mkdtemp())
        self.ops = LiveGatewayRecoveryOps(
            repo_root=self.repo, request=_request(gateway_revision="4"),
            attestation_home=self.attn_home,
        )
        # A REAL launch-generation store + startup-transaction fence (design j#87472: the
        # generation authority is an ATTESTED row whose token names a completed-success
        # startup transaction with this exact participant — not a mocked timestamp, and not
        # the main attestation). Seed the legitimate current generation for the pinned
        # gateway; ``live_generation_token`` is the fence-derived action id it produces.
        self.live_generation_token = self._seed_fence_success(nonce="A")
        self._seed_generation(self.live_generation_token)

    def _seed_fence_success(
        self, *, nonce, assigned_name="gw", role="codex", lane_id="issue_x_lane",
        locator="w:3", workspace_id=None, closed=False, terminal_success=True,
    ):
        """Reserve a startup transaction, record this gateway as its participant, and drive
        it to ``completed_success`` (or a non-terminal phase). Returns the fence action id —
        the generation token every managed launch injects and binds on."""
        from mozyo_bridge.core.state.startup_transaction_fence import (
            PHASE_COMPLETED_SUCCESS,
            PHASE_HEALTH_CHECK,
            Participant,
            StartupTransactionFence,
            StartupUnit,
        )

        fence = StartupTransactionFence(home=self.attn_home)
        unit = StartupUnit(
            workspace_id=workspace_id or self.WS, lane_id=lane_id, providers=(role,)
        )
        action = fence.reserve(unit, f"nonce-{nonce}")
        token = action.action_id
        fence.record_participant(
            token,
            Participant(
                role=role, assigned_name=assigned_name, locator=locator,
                receipt="rcpt", closed=closed,
            ),
        )
        fence.set_phase(
            token, PHASE_COMPLETED_SUCCESS if terminal_success else PHASE_HEALTH_CHECK
        )
        return token

    def _seed_generation(
        self, token, *, assigned_name="gw", role="codex", lane_id="issue_x_lane",
        locator="w:3", workspace_id=None, verdict=None,
    ):
        """Reserve + finalize the current-generation row for ``assigned_name`` at ``token``
        (INSERT OR REPLACE supersedes any prior generation, so a recycle re-seeds cleanly)."""
        from mozyo_bridge.core.state.herdr_identity_attestation import VERDICT_PRESENT
        from mozyo_bridge.core.state.herdr_launch_generation import (
            HerdrLaunchGenerationStore,
        )

        ws = workspace_id or self.WS
        store = HerdrLaunchGenerationStore(home=self.attn_home)
        store.reserve_pending(
            assigned_name=assigned_name, startup_action_id=token,
            workspace_id=ws, role=role, lane_id=lane_id,
        )
        store.finalize(
            assigned_name=assigned_name, startup_action_id=token,
            workspace_id=ws, role=role, lane_id=lane_id, locator=locator,
            verdict=verdict or VERDICT_PRESENT,
            observed_at=self.live_attestation_observed_at,
        )

    def _rec(self, **overrides):
        from types import SimpleNamespace
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E501
            RedmineAnchor,
            build_marker,
        )

        marker = build_marker(
            RedmineAnchor(issue="14203", journal="87251"), "review_request", "codex",
        )
        base = dict(
            notification_marker=marker, source="redmine", issue_id="14203",
            journal_id="87251", receiver="codex", target="w:3", status="sent",
            reason="ok", recorded_at="2026-07-24T17:14:34+00:00",
            turn_start_outcome=None, queue_enter_observation=None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def _facts_with(self, records):
        # The recovery-time attestation identity join runs for REAL against the seeded store;
        # only the repo workspace resolution (a separate concern) is fixed to self.WS.
        class _Ledger:
            def records_for_marker(self, marker):
                return records

        self.ops.ledger = _Ledger()
        with patch.object(self.ops, "_providers", return_value=("claude", "codex")), \
                patch.object(gen_mod, "repo_scope_workspace_id", return_value=self.WS):
            obs_record = self.ops._anchor_delivery_record("codex")
            started = (
                obs_record is not None
                and self.ops._record_observed_turn_start(obs_record)
            )
        return obs_record is not None, started

    def test_only_the_exact_anchor_record_confirms_delivery(self):
        confirmed, _ = self._facts_with([self._rec()])
        self.assertTrue(confirmed)
        # A foreign target / different journal record never confirms.
        confirmed, _ = self._facts_with([self._rec(target="w:FOREIGN")])
        self.assertFalse(confirmed)
        confirmed, _ = self._facts_with([self._rec(journal_id="99999")])
        self.assertFalse(confirmed)

    def _binding(self, **overrides):
        base = dict(
            provider="codex", assigned_name="gw", locator="w:3", row_revision="4",
            attestation_observed_at="2026-07-24T17:00:00+00:00",
            startup_action_id=self.live_generation_token,
        )
        base.update(overrides)
        return base

    def test_started_requires_an_observed_start_and_the_generation_binding(self):
        # Design j#87409 + review j#87418: BOTH an observed start on the v2 QUEUE-ENTER
        # observation AND a generation-coherent binding (pins + provider + the binding's
        # attestation_observed_at == the live current-generation attestation, verified against
        # the REAL seeded attestation store) are required.
        # Unobserved telemetry => never started.
        _, started = self._facts_with([self._rec()])
        self.assertFalse(started)
        # v2 fast-turn: pre-Enter armed wait collected ``changed`` while the snapshot settled
        # — started WITH a generation-coherent binding.
        _, started = self._facts_with([self._rec(queue_enter_observation={
            "read_ok": True, "runtime_state": "turn_ended",
            "event_wait_kind": "changed", "observation_version": 2,
            "gateway_binding": self._binding(),
        })])
        self.assertTrue(started)
        # The same observed start WITHOUT a binding (legacy record) never starts.
        _, started = self._facts_with([self._rec(queue_enter_observation={
            "read_ok": True, "runtime_state": "turn_ended",
            "event_wait_kind": "changed",
        })])
        self.assertFalse(started)
        # F3: the strict event-rail turn_start_outcome branch is UNREACHABLE / removed — a
        # standard-rail record (turn_start_outcome only, no queue-enter binding) never starts,
        # even with a binding grafted on (it is not a v2 queue-enter observed start).
        _, started = self._facts_with([self._rec(
            turn_start_outcome={"outcome": "started"},
            queue_enter_observation={"gateway_binding": self._binding()},
        )])
        self.assertFalse(started)
        # F2: a recycled generation — same locator/name but a DIFFERENT row revision — never
        # binds.
        _, started = self._facts_with([self._rec(queue_enter_observation={
            "event_wait_kind": "changed",
            "gateway_binding": self._binding(row_revision="9"),
        })])
        self.assertFalse(started)
        # F2: a FOREIGN provider in the binding never binds (provider is now an axis).
        _, started = self._facts_with([self._rec(queue_enter_observation={
            "event_wait_kind": "changed",
            "gateway_binding": self._binding(provider="claude"),
        })])
        self.assertFalse(started)
        # j#87445: a DIFFERENT generation token (a distinct launch) never binds — this is
        # the collision-free authority (a shared observed_at is no longer enough).
        _, started = self._facts_with([self._rec(queue_enter_observation={
            "event_wait_kind": "changed",
            "gateway_binding": self._binding(startup_action_id="startup-GEN-B"),
        })])
        self.assertFalse(started)
        # j#87445: a blank generation token never binds (tokenless / legacy record).
        _, started = self._facts_with([self._rec(queue_enter_observation={
            "event_wait_kind": "changed",
            "gateway_binding": self._binding(startup_action_id=""),
        })])
        self.assertFalse(started)
        # F1 / j#87445 / j#87472 (recovery side): a recycle BETWEEN delivery and recovery
        # re-seeds the LIVE current generation with a NEW token (a fresh launch, even at the
        # same observed_at) — the reservation supersedes the old attested pointer, so the
        # same binding no longer equates, fail-closed (the ABA / same-second recycle case).
        recycled = self._seed_fence_success(nonce="B")
        self._seed_generation(recycled)
        _, started = self._facts_with([self._rec(queue_enter_observation={
            "event_wait_kind": "changed", "gateway_binding": self._binding(),
        })])
        self.assertFalse(started)
        self._seed_generation(self.live_generation_token)  # restore the legitimate current gen
        # Timeout / settled snapshot / unread snapshot never start even with a binding.
        for qe in (
            {"event_wait_kind": "timeout", "gateway_binding": self._binding()},
            {"read_ok": True, "runtime_state": "turn_ended",
             "gateway_binding": self._binding()},
            {"read_ok": False, "runtime_state": "busy",
             "gateway_binding": self._binding()},
        ):
            _, started = self._facts_with([self._rec(queue_enter_observation=qe)])
            self.assertFalse(started, qe)

    def test_the_j87393_shape_legacy_record_stays_unconfirmed(self):
        # The actual dogfood record shape (entry 3033): sent/ok, no event-rail outcome,
        # post-hoc snapshot read_ok/turn_ended, NO v2 fields — pinned turn-unconfirmed.
        confirmed, started = self._facts_with([self._rec(queue_enter_observation={
            "observation_kind": "post_choreography_snapshot",
            "source": "herdr_agent_get",
            "runtime_state": "turn_ended", "read_ok": True, "read_reason": None,
            "poll_attempts": 1,
        })])
        self.assertTrue(confirmed)   # the delivery itself is confirmed
        self.assertFalse(started)    # but the turn stays unconfirmed (fail-closed)

    def test_j87424_a_foreign_identity_attestation_never_binds_the_generation(self):
        # Review j#87424 F1: the recovery-time attestation identity join must verify EVERY
        # axis against a REAL store record — a record sharing the assigned-name key / locator /
        # observed_at but a FOREIGN workspace / lane / role never establishes the generation
        # authority for THIS request (which is codex / ws / issue_x_lane). A mocked timestamp
        # would have hidden this; the store is real.
        observed = {
            "event_wait_kind": "changed",
            "gateway_binding": self._binding(),  # observed_at matches the seeded timestamp
        }
        # Baseline: the legitimate record (seeded in setUp) binds.
        _, started = self._facts_with([self._rec(queue_enter_observation=observed)])
        self.assertTrue(started)
        # Foreign identity: the current-generation ROW keeps the same token but a FOREIGN
        # workspace / lane / role — the reader compares each axis to the request pins, so any
        # foreign axis yields no token and never binds.
        for override in (
            dict(workspace_id="foreign-workspace"),
            dict(lane_id="foreign-lane"),
            dict(role="claude"),  # a claude generation for a codex gateway request
        ):
            with self.subTest(override=override):
                self._seed_generation(self.live_generation_token, **override)
                _, started = self._facts_with(
                    [self._rec(queue_enter_observation=observed)]
                )
                self.assertFalse(started)
                self._seed_generation(self.live_generation_token)  # restore legitimate gen

    def test_j87445_same_second_launches_never_join_by_the_timestamp(self):
        # Review j#87445 / design j#87472: two launches with the SAME name/workspace/lane/
        # role/locator/row_revision AND the SAME observed_at second, differing ONLY in the
        # collision-free per-launch token, must never be treated as one generation. Each
        # launch reserves a distinct fence action id; the current-generation row holds exactly
        # one, and the delivery-time binding must equal THAT — the shared second is irrelevant.
        # Generation A (seeded in setUp) is live; the binding carries token A -> binds.
        observed_A = {
            "event_wait_kind": "changed", "gateway_binding": self._binding(),
        }
        _, started = self._facts_with([self._rec(queue_enter_observation=observed_A)])
        self.assertTrue(started)
        # A same-second RECYCLE to generation B (a new fence action id) is now the current row.
        # The delivery-time generation-A binding must NOT join the current generation B.
        self._seed_generation(self._seed_fence_success(nonce="B2"))
        _, started = self._facts_with([self._rec(queue_enter_observation=observed_A)])
        self.assertFalse(started)
        # ABA: a THIRD launch (A') at the same second reuses generation A's shape but a fresh
        # token — an old generation-A binding still never joins A' (each launch's nonce differs).
        self._seed_generation(self._seed_fence_success(nonce="Aprime"))
        _, started = self._facts_with([self._rec(queue_enter_observation=observed_A)])
        self.assertFalse(started)

    def test_j87424_no_attestation_or_absent_verdict_never_binds(self):
        observed = {
            "event_wait_kind": "changed", "gateway_binding": self._binding(),
        }
        # A verdict=missing generation row (real store) never binds.
        self._seed_generation(self.live_generation_token, verdict="missing")
        _, started = self._facts_with([self._rec(queue_enter_observation=observed)])
        self.assertFalse(started)
        # A completely EMPTY home (no generation store, no fence) never binds.
        self.ops.attestation_home = Path(tempfile.mkdtemp())
        _, started = self._facts_with([self._rec(queue_enter_observation=observed)])
        self.assertFalse(started)

    def test_a_later_unrelated_record_never_supplies_the_start(self):
        # j#87397 F2: an unrelated later delivery (different marker/journal) with an observed
        # start must NOT leak into this anchor's classification.
        unrelated = self._rec(
            journal_id="99999", turn_start_outcome={"outcome": "started"},
        )
        confirmed, started = self._facts_with([unrelated])
        self.assertFalse(confirmed)
        self.assertFalse(started)

    def test_the_otel_timeline_authority_is_gone(self):
        self.assertFalse(hasattr(self.ops, "_turn_started_after"))
        self.assertFalse(hasattr(self.ops, "otel_store"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
