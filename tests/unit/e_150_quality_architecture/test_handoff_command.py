"""Fake-port use-case specifications for the handoff command boundary (#12936).

These exercise the ``handoff_command`` boundary directly through a synthetic
:class:`mozyo_bridge.application.handoff_command.HandoffCommandOps` fake — no real
tmux server, no ``orchestrate_handoff``, no semantic target selection. They pin,
in isolation, the per-command entry policy carried by each ``run_*`` method:

- :meth:`HandoffCommandUseCase.run_send`: apply the semantic target selection
  before orchestrating, and orchestrate with no default kind / flags;
- :meth:`HandoffCommandUseCase.run_reply`: orchestrate with ``default_kind="reply"``;
- :meth:`HandoffCommandUseCase.run_ticketless_callback`: orchestrate with
  ``default_kind="reply"`` and ``ticketless=True``;
- :meth:`HandoffCommandUseCase.run_cross_workspace_consult`: pin ``args.to`` to
  ``codex``, default ``--kind`` to ``design_consultation`` only when unset, and
  orchestrate with ``require_receiver_binding=True``.

The end-to-end behavior over the real ``commands.*`` helpers +
``orchestrate_handoff`` stays pinned by the ``handoff`` CLI characterization
tests under ``tests/integration/.../f_130_handoff_routing/`` (``test_handoff_q_enter_cli``,
``test_handoff_ticketless_callback_cli``, ``test_handoff_orchestrator``); this
file pins the extracted entry bodies in isolation, which is the OOP-first carve's
payoff.
"""

from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.application.handoff_command import (
    CONSULT_DEFAULT_KIND,
    HandoffCommandUseCase,
)


class _FakeHandoffCommandOps:
    """A synthetic :class:`HandoffCommandOps` that records calls / scripts a rc."""

    def __init__(self, *, orchestrate_rc: int = 0) -> None:
        self._orchestrate_rc = orchestrate_rc
        self.selected: list[argparse.Namespace] = []
        self.orchestrated: argparse.Namespace | None = None
        self.orchestrate_kwargs: dict | None = None

    def apply_handoff_selection(self, args) -> None:
        self.selected.append(args)

    def orchestrate_handoff(
        self,
        args,
        *,
        default_kind=None,
        require_receiver_binding=False,
        ticketless=False,
    ) -> int:
        self.orchestrated = args
        self.orchestrate_kwargs = {
            "default_kind": default_kind,
            "require_receiver_binding": require_receiver_binding,
            "ticketless": ticketless,
        }
        return self._orchestrate_rc


class RunSendTest(unittest.TestCase):
    def test_applies_selection_then_orchestrates_with_defaults(self) -> None:
        ops = _FakeHandoffCommandOps(orchestrate_rc=0)
        args = argparse.Namespace(to="codex")

        rc = HandoffCommandUseCase(ops).run_send(args)

        self.assertEqual(0, rc)
        # Selection runs before orchestration, on the same namespace.
        self.assertEqual([args], ops.selected)
        self.assertIs(args, ops.orchestrated)
        self.assertEqual(
            {"default_kind": None, "require_receiver_binding": False, "ticketless": False},
            ops.orchestrate_kwargs,
        )

    def test_propagates_orchestrate_rc(self) -> None:
        ops = _FakeHandoffCommandOps(orchestrate_rc=7)
        rc = HandoffCommandUseCase(ops).run_send(argparse.Namespace())
        self.assertEqual(7, rc)


class RunReplyTest(unittest.TestCase):
    def test_orchestrates_with_reply_default_kind(self) -> None:
        ops = _FakeHandoffCommandOps(orchestrate_rc=0)
        args = argparse.Namespace()

        rc = HandoffCommandUseCase(ops).run_reply(args)

        self.assertEqual(0, rc)
        self.assertEqual([], ops.selected)  # reply does NOT apply selection
        self.assertIs(args, ops.orchestrated)
        self.assertEqual("reply", ops.orchestrate_kwargs["default_kind"])
        self.assertFalse(ops.orchestrate_kwargs["ticketless"])
        self.assertFalse(ops.orchestrate_kwargs["require_receiver_binding"])


class RunTicketlessCallbackTest(unittest.TestCase):
    def test_orchestrates_reply_kind_ticketless(self) -> None:
        ops = _FakeHandoffCommandOps(orchestrate_rc=0)
        args = argparse.Namespace()

        rc = HandoffCommandUseCase(ops).run_ticketless_callback(args)

        self.assertEqual(0, rc)
        self.assertEqual([], ops.selected)
        self.assertIs(args, ops.orchestrated)
        self.assertEqual("reply", ops.orchestrate_kwargs["default_kind"])
        self.assertTrue(ops.orchestrate_kwargs["ticketless"])
        self.assertFalse(ops.orchestrate_kwargs["require_receiver_binding"])


class RunCrossWorkspaceConsultTest(unittest.TestCase):
    def test_pins_codex_and_defaults_kind_and_requires_binding(self) -> None:
        ops = _FakeHandoffCommandOps(orchestrate_rc=0)
        args = argparse.Namespace(to="claude", kind=None)

        rc = HandoffCommandUseCase(ops).run_cross_workspace_consult(args)

        self.assertEqual(0, rc)
        # Receiver forced to codex; unset kind defaults to design_consultation.
        self.assertEqual("codex", args.to)
        self.assertEqual(CONSULT_DEFAULT_KIND, args.kind)
        self.assertEqual("design_consultation", args.kind)
        self.assertEqual([], ops.selected)
        self.assertTrue(ops.orchestrate_kwargs["require_receiver_binding"])
        self.assertIsNone(ops.orchestrate_kwargs["default_kind"])
        self.assertFalse(ops.orchestrate_kwargs["ticketless"])

    def test_explicit_kind_is_preserved(self) -> None:
        ops = _FakeHandoffCommandOps(orchestrate_rc=0)
        args = argparse.Namespace(to="claude", kind="review_request")

        HandoffCommandUseCase(ops).run_cross_workspace_consult(args)

        # Receiver still pinned, but the explicit kind is NOT overridden.
        self.assertEqual("codex", args.to)
        self.assertEqual("review_request", args.kind)

    def test_missing_kind_attr_defaults_to_consult_kind(self) -> None:
        ops = _FakeHandoffCommandOps(orchestrate_rc=0)
        args = argparse.Namespace(to="claude")  # no kind attribute at all

        HandoffCommandUseCase(ops).run_cross_workspace_consult(args)

        self.assertEqual(CONSULT_DEFAULT_KIND, args.kind)


if __name__ == "__main__":
    unittest.main()
