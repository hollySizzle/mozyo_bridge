"""herdr forward send adapter tests (Redmine #13583 Increment 3 + R1-F1/F2/F3 correction).

The injected-send-count contract (j#76417 point 7 / j#76528): the adapter calls the single forward
send port EXACTLY ONCE on the positive path (usable store + open generation + ok target), ZERO on
every negative (unbootstrapped/lost store, an active generation, missing/ambiguous/locator-missing/
self target), and never re-sends a delivered generation until the correlated callback completes it —
after which the next call mints a NEW generation and sends once. Uses the real ForwardOutboxFence
over a temp home + real mzb1 encode/decode + a counting fake send port.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.forward_outbox_fence import ForwardOutboxFence, ForwardRouteKey
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    decode_assigned_name,
    encode_assigned_name,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    STATUS_RESOLVED,
    WorkflowRoleResolution,
    project_gateway_lane_id,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_forward_send import (
    SEND_DELIVERED,
    SEND_FAILED,
    ForwardSendOutcome,
    execute_herdr_forward,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
CODEX = "codex"


def _row(lane, *, provider=CODEX, ws=WS, locator="live-loc"):
    return {AGENT_KEY_NAME: encode_assigned_name(ws, provider, lane), "locator": locator}


def _locator_of(row):
    return row.get("locator", "")


class _CountingPort:
    """A fake ForwardSendPort recording every send + the action id it was handed."""

    def __init__(self, result=SEND_DELIVERED):
        self.calls = []
        self.result = result

    def send(self, plan, target, action_id, *, args):
        self.calls.append((plan.direction, target.assigned_name, action_id))
        return ForwardSendOutcome(result=self.result, rc=0, detail="fake send")


def _grandparent():
    return WorkflowRoleResolution(status=STATUS_RESOLVED, role=ROLE_GRANDPARENT_COORDINATOR)


def _gateway(scope):
    return WorkflowRoleResolution(
        status=STATUS_RESOLVED, role=ROLE_PROJECT_GATEWAY, project_scope=scope,
        lane_id=project_gateway_lane_id(scope),
    )


class HerdrForwardSendTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.fence = ForwardOutboxFence(home=self.home)
        self.fence.bootstrap()
        self.args = argparse.Namespace()

    def _run(self, resolution, *, sender_lane, gateway_lane_ids, rows, port, provider=CODEX):
        return execute_herdr_forward(
            resolution, args=self.args, workspace_id=WS, sender_lane_id=sender_lane,
            target_provider=provider, gateway_lane_ids=frozenset(gateway_lane_ids), rows=rows,
            decode=decode_assigned_name, locator_of=_locator_of, fence=self.fence, send_port=port,
        )

    # --- positive: single send + action id injected ----------------------

    def test_grandparent_single_live_gateway_sends_once_with_action_id(self):
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=[_row(gw)], port=port)
        self.assertTrue(res.sent)
        self.assertEqual(len(port.calls), 1)
        self.assertTrue(port.calls[0][2].startswith("fwd_"))  # the minted action id was injected

    # --- generation lifecycle via the adapter ----------------------------

    def test_delivered_generation_blocks_until_callback_completes(self):
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort()
        rows = [_row(gw)]
        first = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=rows, port=port)
        self.assertTrue(first.sent)
        # a repeat while delivered (callback pending) is a duplicate zero-send.
        second = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=rows, port=port)
        self.assertFalse(second.sent)
        self.assertEqual(second.reason, "herdr_forward_duplicate")
        self.assertEqual(len(port.calls), 1)

    def test_completed_generation_allows_next_send(self):
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort()
        rows = [_row(gw)]
        first = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=rows, port=port)
        self.assertTrue(first.sent)
        # complete the generation (as the correlated callback would).
        route = ForwardRouteKey(WS, "default", "grandparent_coordinator", "project_gateway", "")
        self.assertTrue(self.fence.complete(route, port.calls[0][2]))
        second = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=rows, port=port)
        self.assertTrue(second.sent)
        self.assertEqual(len(port.calls), 2)
        self.assertNotEqual(port.calls[0][2], port.calls[1][2])  # a NEW generation id

    # --- store loss / unbootstrapped -> zero-send (R1-F2) ----------------

    def test_unbootstrapped_store_is_zero_send(self):
        fence = ForwardOutboxFence(home=Path(tempfile.mkdtemp()))  # never bootstrapped
        port = _CountingPort()
        gw = project_gateway_lane_id("alpha")
        res = execute_herdr_forward(
            _grandparent(), args=self.args, workspace_id=WS, sender_lane_id="default",
            target_provider=CODEX, gateway_lane_ids=frozenset({gw}), rows=[_row(gw)],
            decode=decode_assigned_name, locator_of=_locator_of, fence=fence, send_port=port,
        )
        self.assertFalse(res.sent)
        self.assertEqual(res.reason, "herdr_forward_fence_unavailable")
        self.assertEqual(len(port.calls), 0)

    def test_total_loss_after_delivered_is_zero_send(self):
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort()
        rows = [_row(gw)]
        self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=rows, port=port)
        self.fence.path.unlink()
        self.fence.sidecar_path.unlink()  # total loss -> must fail closed, no resurrection
        res = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=rows, port=port)
        self.assertFalse(res.sent)
        self.assertEqual(res.reason, "herdr_forward_fence_unavailable")
        self.assertEqual(len(port.calls), 1)  # only the original send

    # --- target negatives (zero-send, no generation consumed) ------------

    def test_zero_live_gateways_is_zero_send_missing(self):
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=[], port=port)
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "missing")
        self.assertEqual(len(port.calls), 0)
        # no generation consumed -> a later resolvable target can still send.
        ok = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=[_row(gw)], port=port)
        self.assertTrue(ok.sent)

    def test_two_live_gateways_is_zero_send_ambiguous(self):
        a, b = project_gateway_lane_id("alpha"), project_gateway_lane_id("beta")
        port = _CountingPort()
        res = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={a, b}, rows=[_row(a), _row(b)], port=port)
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "ambiguous")
        self.assertEqual(len(port.calls), 0)

    def test_gateway_without_locator_is_zero_send(self):
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=[_row(gw, locator="")], port=port)
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "locator_missing")
        self.assertEqual(len(port.calls), 0)

    # --- gateway -> child with self-fence --------------------------------

    def test_gateway_single_child_sends_once(self):
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(_gateway("alpha"), sender_lane=gw, gateway_lane_ids={gw}, rows=[_row(gw), _row("issue_1234")], port=port)
        self.assertTrue(res.sent)
        self.assertEqual(len(port.calls), 1)
        self.assertIn("delegated_coordinator", port.calls[0][0])

    def test_gateway_child_equal_to_self_lane_is_self_fenced(self):
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(_gateway("alpha"), sender_lane="issue_self", gateway_lane_ids={gw}, rows=[_row("issue_self")], port=port)
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "self")
        self.assertEqual(len(port.calls), 0)

    def test_gateway_two_children_is_ambiguous_zero_send(self):
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(_gateway("alpha"), sender_lane=gw, gateway_lane_ids={gw}, rows=[_row("issue_1"), _row("issue_2")], port=port)
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "ambiguous")
        self.assertEqual(len(port.calls), 0)

    # --- provider passthrough (F3 target provider) + send outcome --------

    def test_target_provider_scopes_the_inventory_match(self):
        # A claude-provider row on the gateway lane is NOT a codex target -> missing.
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort()
        res = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=[_row(gw, provider="claude")], port=port, provider=CODEX)
        self.assertFalse(res.sent)
        self.assertEqual(res.target_status, "missing")
        # ... but if the target provider IS claude (a rebind), the same row resolves.
        ok = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=[_row(gw, provider="claude")], port=port, provider="claude")
        self.assertTrue(ok.sent)

    def test_failed_send_marks_uncertain_and_blocks_retry(self):
        gw = project_gateway_lane_id("alpha")
        port = _CountingPort(result=SEND_FAILED)
        rows = [_row(gw)]
        first = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=rows, port=port)
        self.assertTrue(first.sent)
        self.assertEqual(first.send.result, SEND_FAILED)
        # uncertain generation blocks the next attempt (no blind retry).
        second = self._run(_grandparent(), sender_lane="default", gateway_lane_ids={gw}, rows=rows, port=port)
        self.assertFalse(second.sent)
        self.assertEqual(len(port.calls), 1)


class _FakeSenderRes:
    def __init__(self, ws):
        self.ok = True

        class _Id:
            workspace_id = ws
        self.identity = _Id()


class CompletionHookTest(unittest.TestCase):
    """The correlated-callback completion hook (Redmine #13583 R1-F1): a positively-delivered
    callback echoing a forward_action_id completes the exact delivered generation, and only then may
    the next forward send; a failed / stale / drifted callback never advances."""

    def setUp(self):
        import os
        self.home = Path(tempfile.mkdtemp())
        os.environ["MOZYO_BRIDGE_HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.pop("MOZYO_BRIDGE_HOME", None))
        self.fence = ForwardOutboxFence(home=self.home)
        self.fence.bootstrap()
        self.route = ForwardRouteKey(WS, "default", "grandparent_coordinator", "project_gateway", "")
        r = self.fence.reserve(self.route)
        self.fence.mark_delivered(self.route, r.action_id)
        self.action_id = r.action_id

    def _call(self, *, action_id, read_contract="grandparent_coordinator", delivered=True):
        import argparse as _ap
        from unittest.mock import patch
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            herdr_workflow_step as hws,
        )
        args = _ap.Namespace(forward_action_id=action_id, read_contract=read_contract, repo=None)
        with patch.object(hws, "_anchor_workspace_id", return_value=WS), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain."
            "herdr_target_resolution.resolve_sender_identity",
            return_value=_FakeSenderRes(WS),
        ):
            return hws.complete_forward_generation_on_callback(args, delivered=delivered)

    def test_matching_callback_completes_and_next_send_allowed(self):
        self.assertTrue(self._call(action_id=self.action_id))
        # completed -> the next reserve mints a fresh generation.
        self.assertTrue(self.fence.reserve(self.route).won)

    def test_failed_delivery_does_not_complete(self):
        self.assertFalse(self._call(action_id=self.action_id, delivered=False))
        self.assertFalse(self.fence.reserve(self.route).won)  # still delivered / active

    def test_stale_action_id_does_not_complete(self):
        self.assertFalse(self._call(action_id="fwd_stale"))

    def test_drifted_read_contract_does_not_complete(self):
        self.assertFalse(self._call(action_id=self.action_id, read_contract="project_gateway"))

    def test_empty_action_id_no_ops(self):
        self.assertFalse(self._call(action_id=""))


class ForwardTargetProviderTest(unittest.TestCase):
    """R1-F3: the forward target provider is resolved from the action-time provider_binding for the
    plan's to_role (reboundable), not a hard-coded codex; an unbound / unmapped / broken binding
    fails closed to ``""`` (the leg then zero-sends)."""

    def _resolve(self, to_role, binding):
        import argparse as _ap
        from unittest.mock import patch
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            herdr_workflow_step as hws,
        )
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.workflow_binding_source.load_workflow_binding",
            return_value=(binding, []),
        ):
            return hws._forward_target_provider(_ap.Namespace(repo=None), ".", to_role)

    def _binding(self, overrides=None):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
            RoleProviderBinding,
        )
        if overrides:
            return RoleProviderBinding.from_overrides(overrides)
        return RoleProviderBinding.default()

    def test_project_gateway_defaults_to_codex(self):
        self.assertEqual(self._resolve("project_gateway", self._binding()), "codex")

    def test_delegated_coordinator_maps_to_coordinator_binding_codex(self):
        self.assertEqual(self._resolve("delegated_coordinator", self._binding()), "codex")

    def test_rebind_is_honored(self):
        # A provider_binding rebind of project_gateway -> claude is used for the target.
        self.assertEqual(
            self._resolve("project_gateway", self._binding({"project_gateway": "claude"})), "claude"
        )

    def test_unmapped_role_fails_closed(self):
        self.assertEqual(self._resolve("implementation_worker", self._binding()), "")

    def test_broken_binding_fails_closed(self):
        import argparse as _ap
        from unittest.mock import patch
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            herdr_workflow_step as hws,
        )
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.workflow_binding_source.load_workflow_binding",
            side_effect=RuntimeError("broken provider config"),
        ):
            self.assertEqual(hws._forward_target_provider(_ap.Namespace(repo=None), ".", "project_gateway"), "")


class ReceiverVisibleRoundtripTest(unittest.TestCase):
    """R2-F1: the forward generation id must reach the RECEIVER on the real delivery path.

    The no-anchor ticketless rail has no Redmine anchor and no `--persist-delivery`, so the
    structured dict is sender-side only — `build_notification_body` types `pointer_clause()` into
    the receiver's pane/agent. The id must ride that body (and the durable record), or the receiver
    can never echo it and the generation would never complete. This pins the full
    forward -> receiver-reads-body -> echo -> callback completes roundtrip on ONE id.
    """

    def _consultation(self, action_id):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
            CALLBACK_METHODS,
            CONSULTATION_PROJECT_DOMAIN,
            TicketlessConsultation,
        )
        return TicketlessConsultation(
            CONSULTATION_PROJECT_DOMAIN, "grandparent_coordinator", list(CALLBACK_METHODS),
            "project_gateway", forward_action_id=action_id,
        )

    def _body(self, payload):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            build_notification_body,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_anchors import (
            TicketlessConsultationAnchor,
        )
        anchor = TicketlessConsultationAnchor(
            consultation_kind=payload.consultation_kind, callback_to_role=payload.callback_to_role
        )
        return build_notification_body(
            anchor, "design_consultation", None, "codex", ticketless_consultation=payload
        )

    def test_consultation_body_carries_the_action_id(self):
        c = self._consultation("fwd_abc123")
        self.assertIn("fwd_abc123", c.pointer_clause())
        self.assertNotIn("\n", c.pointer_clause())  # the body is a single send-keys line
        self.assertIn("fwd_abc123", self._body(c))  # what the receiver actually reads
        self.assertTrue(any("fwd_abc123" in l for l in c.record_lines()))

    def test_work_intake_body_carries_the_action_id(self):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
            CALLBACK_METHODS,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_work_intake import (
            WORK_SHAPE_DOMAIN_DESIGN,
            TicketlessWorkIntake,
        )
        w = TicketlessWorkIntake(
            WORK_SHAPE_DOMAIN_DESIGN, "project_gateway", list(CALLBACK_METHODS),
            "delegated_coordinator", forward_action_id="fwd_child9",
        )
        self.assertIn("fwd_child9", w.pointer_clause())
        self.assertNotIn("\n", w.pointer_clause())
        self.assertTrue(any("fwd_child9" in l for l in w.record_lines()))

    def test_non_forward_payload_body_is_byte_invariant(self):
        c = self._consultation("")
        self.assertNotIn("forward_action_id", c.pointer_clause())
        self.assertFalse(any("Forward action id" in l for l in c.record_lines()))

    def test_end_to_end_id_roundtrip_completes_the_generation(self):
        import os
        import re
        import argparse as _ap
        from unittest.mock import patch
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            herdr_workflow_step as hws,
        )
        home = Path(tempfile.mkdtemp())
        os.environ["MOZYO_BRIDGE_HOME"] = str(home)
        self.addCleanup(lambda: os.environ.pop("MOZYO_BRIDGE_HOME", None))
        fence = ForwardOutboxFence(home=home)
        fence.bootstrap()
        route = ForwardRouteKey(WS, "default", "grandparent_coordinator", "project_gateway", "")
        minted = fence.reserve(route).action_id
        fence.mark_delivered(route, minted)

        # 1) the forward body the RECEIVER reads carries the minted id.
        body = self._body(self._consultation(minted))
        found = re.search(r"forward_action_id (fwd_[0-9a-f]+)", body)
        self.assertIsNotNone(found, "the receiver must be able to read the id off the body")
        echoed = found.group(1)
        self.assertEqual(echoed, minted)

        # 2) the receiver echoes exactly that id on its callback -> the generation completes.
        args = _ap.Namespace(
            forward_action_id=echoed, read_contract="grandparent_coordinator", repo=None
        )
        with patch.object(hws, "_anchor_workspace_id", return_value=WS), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain."
            "herdr_target_resolution.resolve_sender_identity",
            return_value=_FakeSenderRes(WS),
        ):
            self.assertTrue(hws.complete_forward_generation_on_callback(args, delivered=True))
        # 3) completed -> the caller may forward again.
        self.assertTrue(fence.reserve(route).won)


class CallbackTransportOutcomeBoundaryTest(unittest.TestCase):
    """R2-F2: completion is gated on the transport's structured POSITIVE delivery, not the CLI rc.

    `orchestrate_handoff` returns rc 0 for a `pending_input` (body typed, Enter never pressed) and
    for a marker-unobserved `queue_enter` (landing unconfirmed). Neither handed the message to the
    receiver, so neither may complete a forward generation.
    """

    def _mk_outcome(self, status, reason):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            make_outcome,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_anchors import (
            TicketlessConsultationAnchor,
        )
        return make_outcome(
            status=status, reason=reason, receiver="codex", target="%1",
            anchor=TicketlessConsultationAnchor(
                consultation_kind="project_domain_consultation",
                callback_to_role="grandparent_coordinator",
            ),
            mode="queue-enter", kind="design_consultation", notification_marker="m",
        )

    def _positive(self, status, reason):
        import argparse as _ap
        from mozyo_bridge.application.commands import delivery_was_positive
        args = _ap.Namespace()
        args.delivery_outcome = self._mk_outcome(status, reason)
        return delivery_was_positive(args)

    def test_observed_sent_is_positive(self):
        self.assertTrue(self._positive("sent", "ok"))

    def test_marker_unobserved_queue_enter_is_not_positive(self):
        self.assertFalse(self._positive("sent", "queue_enter"))

    def test_pending_input_is_not_positive(self):
        # pending_input carries reason="ok" -> the status MUST be checked too.
        self.assertFalse(self._positive("pending_input", "ok"))

    def test_absent_outcome_fails_closed(self):
        import argparse as _ap
        from mozyo_bridge.application.commands import delivery_was_positive
        self.assertFalse(delivery_was_positive(_ap.Namespace()))

    def test_callback_entry_completes_only_on_positive_delivery(self):
        """The `handoff ticketless-callback` entry gates the completion on the real outcome."""
        import argparse as _ap
        from unittest.mock import patch
        from mozyo_bridge.application import commands as cmds

        for status, reason, expect in (
            ("sent", "ok", True),
            ("sent", "queue_enter", False),
            ("pending_input", "ok", False),
        ):
            captured = {}

            def _fake_run(a, _s=status, _r=reason):
                a.delivery_outcome = self._mk_outcome(_s, _r)
                return 0  # rc 0 in ALL three cases -- the rc must NOT be the gate

            def _fake_complete(a, *, delivered):
                captured["delivered"] = delivered
                return delivered

            with patch(
                "mozyo_bridge.application.handoff_command.HandoffCommandUseCase."
                "run_ticketless_callback",
                side_effect=lambda a: _fake_run(a),
            ), patch(
                "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
                "application.herdr_workflow_step.complete_forward_generation_on_callback",
                side_effect=_fake_complete,
            ):
                rc = cmds.cmd_handoff_ticketless_callback(
                    _ap.Namespace(forward_action_id="fwd_x", read_contract="grandparent_coordinator")
                )
            self.assertEqual(rc, 0)
            self.assertEqual(
                captured.get("delivered"), expect, f"{status}/{reason} -> expected {expect}"
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
