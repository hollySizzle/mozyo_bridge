"""Fake-port use-case specifications for the notify command boundary (#12931).

These exercise the ``notify_command`` boundary directly through a synthetic
:class:`mozyo_bridge.application.notify_command.NotifyOps` fake — no real tmux
server, no ``orchestrate_handoff``, no filesystem. They pin, in isolation:

- :class:`LegacyQueueNotifyUseCase`: the type-observe-marker-Enter TUI sequence,
  the max(read_lines, 200) landing-lines rule, the marker-miss rollback + die
  (no Enter), the submit-delay sleep gate, and the ``journal=`` / ``task=`` gate
  in the success line;
- :class:`StandardNotifyUseCase`: the legacy-``--type`` -> kind/summary mapping,
  the normalized ``orchestrate_handoff`` namespace, and the legacy success line
  that fires only on ``rc == 0`` (with ``target=-`` when ``pane_info`` dies).

The end-to-end behavior over the real ``commands.*`` helpers stays pinned by the
``notify`` characterization tests in
``tests/integration/.../f_130_handoff_routing/test_notify_contract.py`` and the
``notify_agent`` tests in ``tests/integration/.../test_mozyo_bridge.py``; this
file pins the extracted bodies in isolation, which is the OOP-first carve's
payoff.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest

from mozyo_bridge.application.notify_command import (
    LegacyQueueNotifyUseCase,
    StandardNotifyUseCase,
)


class _FakeNotifyOps:
    """A synthetic :class:`NotifyOps` that records calls and serves scripted values."""

    def __init__(
        self,
        *,
        task=None,
        pane_id: str = "%9",
        marker_seen: bool = True,
        orchestrate_rc: int = 0,
        pane_info_raises: bool = False,
    ) -> None:
        self._task = task
        self._pane_id = pane_id
        self._marker_seen = marker_seen
        self._orchestrate_rc = orchestrate_rc
        self._pane_info_raises = pane_info_raises
        self.calls: list[str] = []
        self.read_calls: list[argparse.Namespace] = []
        self.messages: list[argparse.Namespace] = []
        self.keys: list[argparse.Namespace] = []
        self.slept: list[float] = []
        self.wait_args: tuple | None = None
        self.rolled_back: list[str] = []
        self.orchestrated: argparse.Namespace | None = None

    def require_tmux(self) -> None:
        self.calls.append("require_tmux")

    def validate_notify_gate(self, args) -> None:
        self.calls.append("validate_notify_gate")

    def find_handoff_task(self, args, agent):
        self.calls.append("find_handoff_task")
        return self._task

    def load_tmux_conf_for(self, args) -> None:
        self.calls.append("load_tmux_conf_for")

    def pane_info(self, target_name):
        self.calls.append("pane_info")
        if self._pane_info_raises:
            raise SystemExit(2)
        return {"id": self._pane_id}

    def ensure_agent_target(self, target_info, agent, *, force) -> None:
        self.calls.append(f"ensure_agent_target:force={force}")

    def cmd_read(self, args):
        self.read_calls.append(args)

    def build_prompt(self, args, agent, task) -> str:
        return f"PROMPT[{agent}]"

    def cmd_message(self, args):
        self.messages.append(args)

    def landing_marker(self, args, task) -> str:
        return "[marker]"

    def wait_for_text(self, target, marker, lines, timeout) -> bool:
        self.wait_args = (target, marker, lines, timeout)
        return self._marker_seen

    def rollback_unsubmitted_input(self, target) -> None:
        self.rolled_back.append(target)

    def die(self, message):
        self.calls.append("die")
        raise SystemExit(message)

    def cmd_keys(self, args):
        self.keys.append(args)

    def sleep(self, seconds) -> None:
        self.slept.append(seconds)

    def orchestrate_handoff(self, args) -> int:
        self.orchestrated = args
        return self._orchestrate_rc


class LegacyQueueNotifyUseCaseTest(unittest.TestCase):
    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            journal="46005",
            target="%9",
            config=False,
            force=True,
            read_lines=20,
            landing_timeout=5.0,
            submit_delay=0.0,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_marker_seen_types_reads_and_presses_enter(self) -> None:
        ops = _FakeNotifyOps(marker_seen=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = LegacyQueueNotifyUseCase(ops).run(self._args(), "codex")

        self.assertEqual(0, rc)
        # read before and after typing the prompt.
        self.assertEqual(2, len(ops.read_calls))
        self.assertEqual("PROMPT[codex]", ops.messages[0].text)
        # landing lines = max(read_lines, 200); timeout threaded through.
        self.assertEqual(("%9", "[marker]", 200, 5.0), ops.wait_args)
        self.assertEqual([["Enter"]], [k.keys for k in ops.keys])
        self.assertEqual([], ops.rolled_back)
        self.assertIn(
            "notified codex: journal=46005 target=%9 read_lines=20",
            out.getvalue(),
        )

    def test_marker_missing_rolls_back_and_dies_without_enter(self) -> None:
        ops = _FakeNotifyOps(marker_seen=False)
        with self.assertRaises(SystemExit):
            LegacyQueueNotifyUseCase(ops).run(self._args(), "codex")

        self.assertEqual(["%9"], ops.rolled_back)
        self.assertEqual([], ops.keys)
        self.assertIn("die", ops.calls)

    def test_submit_delay_sleeps_before_enter(self) -> None:
        ops = _FakeNotifyOps(marker_seen=True)
        with contextlib.redirect_stdout(io.StringIO()):
            LegacyQueueNotifyUseCase(ops).run(self._args(submit_delay=0.3), "claude")

        self.assertEqual([0.3], ops.slept)

    def test_task_gate_used_in_success_line_when_no_journal(self) -> None:
        ops = _FakeNotifyOps(task={"id": "legacy-1"}, marker_seen=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            LegacyQueueNotifyUseCase(ops).run(
                self._args(journal=None), "claude"
            )

        self.assertIn("find_handoff_task", ops.calls)
        self.assertIn("notified claude: task=legacy-1", out.getvalue())


class StandardNotifyUseCaseTest(unittest.TestCase):
    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            issue="9020",
            journal="46005",
            target="%9",
            type=None,
            force=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_known_type_maps_to_kind_and_drops_summary(self) -> None:
        ops = _FakeNotifyOps(orchestrate_rc=0)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = StandardNotifyUseCase(ops).run(
                self._args(type="review_request"), "codex", default_kind="reply"
            )

        self.assertEqual(0, rc)
        self.assertIsNotNone(ops.orchestrated)
        self.assertEqual("review_request", ops.orchestrated.kind)
        self.assertIsNone(ops.orchestrated.summary)
        self.assertEqual("codex", ops.orchestrated.to)
        self.assertEqual("redmine", ops.orchestrated.source)

    def test_unknown_type_falls_back_to_default_kind_with_summary(self) -> None:
        ops = _FakeNotifyOps(orchestrate_rc=0)
        with contextlib.redirect_stdout(io.StringIO()):
            StandardNotifyUseCase(ops).run(
                self._args(type="odd"), "claude", default_kind="reply"
            )

        self.assertEqual("reply", ops.orchestrated.kind)
        self.assertEqual("legacy --type=odd", ops.orchestrated.summary)

    def test_success_line_prints_on_rc_zero(self) -> None:
        ops = _FakeNotifyOps(orchestrate_rc=0)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            StandardNotifyUseCase(ops).run(self._args(), "codex", default_kind="reply")

        self.assertIn("notified codex: journal=46005 target=%9", out.getvalue())

    def test_no_success_line_on_nonzero_rc(self) -> None:
        ops = _FakeNotifyOps(orchestrate_rc=3)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = StandardNotifyUseCase(ops).run(
                self._args(), "codex", default_kind="reply"
            )

        self.assertEqual(3, rc)
        self.assertEqual("", out.getvalue())

    def test_target_dash_when_pane_info_dies_on_success(self) -> None:
        ops = _FakeNotifyOps(orchestrate_rc=0, pane_info_raises=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            StandardNotifyUseCase(ops).run(self._args(), "codex", default_kind="reply")

        self.assertIn("target=-", out.getvalue())


if __name__ == "__main__":
    unittest.main()
