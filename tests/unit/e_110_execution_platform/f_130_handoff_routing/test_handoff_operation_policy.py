"""The core-owned handoff operation vocabulary + entry policy (Redmine #15149 / #15156).

#15149 extracted the four high-level handoff operations' *entry policy* out of the
CLI entry bodies into ``domain/handoff_operation.py`` so the CLI entry and the typed
application API (the boundary a local MCP server calls) read one table instead of
restating it. These pin that the table says what the CLI entries have always done,
that the vocabulary is closed, and that the API's normalization of a request cannot
be used to reach a rail the requested operation does not have.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.handoff_command import (  # noqa: E402
    CONSULT_DEFAULT_KIND,
    HandoffCommandUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_application_service import (  # noqa: E402,E501
    apply_entry_policy,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_command_input import (  # noqa: E402,E501
    HandoffCommandInput,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_operation import (  # noqa: E402,E501
    HANDOFF_OPERATIONS,
    OP_CROSS_WORKSPACE_CONSULT,
    OP_REPLY,
    OP_SEND,
    OP_TICKETLESS_CALLBACK,
    UnknownHandoffOperation,
    entry_policy_for,
)


class _FakeOps:
    """Records what the CLI use case handed the orchestration."""

    def __init__(self) -> None:
        self.selected: list = []
        self.kwargs: dict | None = None

    def apply_handoff_selection(self, args) -> None:
        self.selected.append(args)

    def orchestrate_handoff(
        self, args, *, default_kind=None, require_receiver_binding=False, ticketless=False
    ) -> int:
        self.kwargs = {
            "default_kind": default_kind,
            "require_receiver_binding": require_receiver_binding,
            "ticketless": ticketless,
        }
        return 0


class VocabularyTest(unittest.TestCase):
    def test_the_vocabulary_is_closed_and_names_the_four_operations(self) -> None:
        self.assertEqual(
            sorted(HANDOFF_OPERATIONS),
            sorted(
                [OP_SEND, OP_REPLY, OP_TICKETLESS_CALLBACK, OP_CROSS_WORKSPACE_CONSULT]
            ),
        )

    def test_an_unknown_operation_fails_closed(self) -> None:
        with self.assertRaises(UnknownHandoffOperation):
            entry_policy_for("send_but_skip_the_gates")

    def test_the_policies_are_frozen(self) -> None:
        policy = entry_policy_for(OP_SEND)
        with self.assertRaises(Exception):
            policy.ticketless = True  # type: ignore[misc]


class PolicyMatchesTheCliEntryTest(unittest.TestCase):
    """The table must say exactly what each CLI entry body does — or it has drifted."""

    def test_send(self) -> None:
        policy = entry_policy_for(OP_SEND)
        ops = _FakeOps()
        args = argparse.Namespace(to="codex")
        HandoffCommandUseCase(ops).run_send(args)

        self.assertTrue(policy.semantic_selection)
        self.assertEqual([args], ops.selected)
        self.assertEqual(
            {
                "default_kind": policy.default_kind,
                "require_receiver_binding": policy.require_receiver_binding,
                "ticketless": policy.ticketless,
            },
            ops.kwargs,
        )
        self.assertEqual(
            {"default_kind": None, "require_receiver_binding": False, "ticketless": False},
            ops.kwargs,
        )

    def test_reply(self) -> None:
        policy = entry_policy_for(OP_REPLY)
        ops = _FakeOps()
        HandoffCommandUseCase(ops).run_reply(argparse.Namespace())

        self.assertFalse(policy.semantic_selection)
        self.assertEqual([], ops.selected)
        self.assertEqual("reply", policy.default_kind)
        self.assertEqual("reply", ops.kwargs["default_kind"])
        self.assertFalse(ops.kwargs["ticketless"])

    def test_ticketless_callback(self) -> None:
        policy = entry_policy_for(OP_TICKETLESS_CALLBACK)
        ops = _FakeOps()
        HandoffCommandUseCase(ops).run_ticketless_callback(argparse.Namespace())

        self.assertEqual("reply", policy.default_kind)
        self.assertTrue(policy.ticketless)
        self.assertEqual("reply", ops.kwargs["default_kind"])
        self.assertTrue(ops.kwargs["ticketless"])
        self.assertFalse(ops.kwargs["require_receiver_binding"])

    def test_cross_workspace_consult(self) -> None:
        policy = entry_policy_for(OP_CROSS_WORKSPACE_CONSULT)
        ops = _FakeOps()
        args = argparse.Namespace(to="claude", kind=None)
        HandoffCommandUseCase(ops).run_cross_workspace_consult(args)

        self.assertEqual("codex", policy.pinned_receiver)
        self.assertEqual(CONSULT_DEFAULT_KIND, policy.pinned_kind)
        self.assertTrue(policy.require_receiver_binding)
        # ... and the entry applied exactly that.
        self.assertEqual("codex", args.to)
        self.assertEqual(CONSULT_DEFAULT_KIND, args.kind)
        self.assertTrue(ops.kwargs["require_receiver_binding"])
        self.assertIsNone(ops.kwargs["default_kind"])


class ApplyEntryPolicyTest(unittest.TestCase):
    """The API normalizes a typed input the same way, and fails closed on smuggling."""

    def test_reply_gets_the_reply_default_kind(self) -> None:
        out = apply_entry_policy(
            HandoffCommandInput(to="codex"), entry_policy_for(OP_REPLY)
        )
        self.assertEqual("reply", out.default_kind)
        self.assertFalse(out.ticketless)

    def test_consult_pins_the_receiver_and_defaults_the_kind(self) -> None:
        out = apply_entry_policy(
            HandoffCommandInput(to="claude"),
            entry_policy_for(OP_CROSS_WORKSPACE_CONSULT),
        )
        self.assertEqual("codex", out.to)
        self.assertEqual(CONSULT_DEFAULT_KIND, out.kind)
        self.assertTrue(out.require_receiver_binding)

    def test_consult_preserves_an_explicit_kind(self) -> None:
        out = apply_entry_policy(
            HandoffCommandInput(to="claude", kind="review_request"),
            entry_policy_for(OP_CROSS_WORKSPACE_CONSULT),
        )
        self.assertEqual("review_request", out.kind)
        self.assertEqual("codex", out.to)

    def test_a_request_cannot_smuggle_in_a_rail_its_operation_lacks(self) -> None:
        """The anchorless ticketless rails are the operation's, never the caller's.

        ``ticketless`` skips the anchor requirement and ``ticketless_consultation`` /
        ``ticketless_work_intake`` are the project-gateway rails. A caller asking for
        ``send`` while setting them would otherwise reach a rail the send operation
        does not have, so every entry-policy field is reset from the policy.
        """
        smuggled = HandoffCommandInput(
            to="claude",
            ticketless=True,
            ticketless_consultation=True,
            ticketless_work_intake=True,
            require_receiver_binding=True,
            default_kind="implementation_request",
        )
        out = apply_entry_policy(smuggled, entry_policy_for(OP_SEND))

        self.assertFalse(out.ticketless)
        self.assertFalse(out.ticketless_consultation)
        self.assertFalse(out.ticketless_work_intake)
        self.assertFalse(out.require_receiver_binding)
        self.assertIsNone(out.default_kind)

    def test_normalization_does_not_mutate_the_caller_s_input(self) -> None:
        original = HandoffCommandInput(to="claude")
        apply_entry_policy(original, entry_policy_for(OP_CROSS_WORKSPACE_CONSULT))
        self.assertEqual("claude", original.to)
        self.assertIsNone(original.kind)


if __name__ == "__main__":
    unittest.main()
