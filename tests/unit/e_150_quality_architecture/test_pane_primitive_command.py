"""Use-case specifications for the pane primitive command boundary (#12932).

These exercise the ``pane_primitive_command`` boundary directly with a fake port
— no real tmux, no subprocess, no stderr. They pin the ``PanePrimitiveUseCase``
that backs the six low-level ``mozyo-bridge`` pane/debug primitives (``id`` /
``resolve`` / ``read`` / ``type`` / ``message`` / ``keys``):

- the exact primitive call sequence + argument shapes each flow issues (the
  tmux availability guard, target resolution, read-marker gate, and the
  ``send-keys`` writes) so the behavior-preserving carve is byte-for-byte,
- the ``PanePrimitiveOutcome`` stdout shape — the ``read`` no-trailing-newline
  ``stdout_end=""`` vs the ``id`` / ``resolve`` default newline vs the
  stdout-silent writers (``stdout is None``),
- the strict-rail ``message`` submission (marker observed -> Enter; marker miss
  -> ``C-u`` rollback + read clear + guidance trailer + ``die``) and the
  ``require_read`` ``SystemExit`` -> guidance-then-re-raise gate.

The end-to-end behavior over the live ``commands.*`` primitives stays pinned by
the CLI integration tests in ``tests/integration/.../test_mozyo_bridge.py``; this
file pins the family in isolation, which is the OOP-first carve's payoff.
"""

from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.application.pane_primitive_command import (
    PanePrimitiveOutcome,
    PanePrimitiveUseCase,
)


class _FakePaneOps:
    """Recording fake :class:`PanePrimitiveOps`.

    Records an ordered call log and lets a test script ``require_read`` /
    ``wait_for_text`` / ``die`` behavior. ``die`` raises ``SystemExit`` like the
    live ``commands.die`` so the strict-rail failure path terminates faithfully.
    """

    def __init__(
        self,
        *,
        pane: str = "%1",
        resolved: str = "%2",
        captured: str = "captured-text",
        window_name: str | None = "codex",
        location: str = "agents:0.0",
        require_read_raises: bool = False,
        wait_ok: bool = True,
    ) -> None:
        self.calls: list[tuple] = []
        self._pane = pane
        self._resolved = resolved
        self._captured = captured
        self._window_name = window_name
        self._location = location
        self._require_read_raises = require_read_raises
        self._wait_ok = wait_ok

    def require_tmux(self) -> None:
        self.calls.append(("require_tmux",))

    def current_pane(self) -> str:
        self.calls.append(("current_pane",))
        return self._pane

    def resolve_target(self, target: str) -> str:
        self.calls.append(("resolve_target", target))
        return self._resolved

    def resolve_message_target(self, args: argparse.Namespace) -> str:
        self.calls.append(("resolve_message_target", args))
        return self._resolved

    def capture_pane(self, target: str, lines: int) -> str:
        self.calls.append(("capture_pane", target, lines))
        return self._captured

    def mark_read(self, target: str) -> None:
        self.calls.append(("mark_read", target))

    def require_read(self, target: str) -> None:
        self.calls.append(("require_read", target))
        if self._require_read_raises:
            raise SystemExit(2)

    def clear_read(self, target: str) -> None:
        self.calls.append(("clear_read", target))

    def run_tmux(self, *args: str):
        self.calls.append(("run_tmux", *args))
        return None

    def pane_window_name(self, pane: str) -> str | None:
        self.calls.append(("pane_window_name", pane))
        return self._window_name

    def pane_location(self, pane: str) -> str:
        self.calls.append(("pane_location", pane))
        return self._location

    def wait_for_text(self, target: str, text: str, lines: int, timeout: float) -> bool:
        self.calls.append(("wait_for_text", target, text, lines, timeout))
        return self._wait_ok

    def emit_message_gate_guidance(self, target: str, *, attempt, no_submit) -> None:
        self.calls.append(("emit_message_gate_guidance", target, attempt, no_submit))

    def die(self, message: str) -> None:
        self.calls.append(("die", message))
        raise SystemExit(2)

    def sleep(self, seconds: float) -> None:
        self.calls.append(("sleep", seconds))


class PanePrimitiveIdResolveReadTest(unittest.TestCase):
    def test_id_prints_current_pane_with_default_newline(self) -> None:
        ops = _FakePaneOps(pane="%7")
        outcome = PanePrimitiveUseCase(ops).id(argparse.Namespace())
        self.assertIsInstance(outcome, PanePrimitiveOutcome)
        self.assertEqual(0, outcome.exit_code)
        self.assertEqual("%7", outcome.stdout)
        self.assertEqual("\n", outcome.stdout_end)
        # `id` does not require tmux — it only reads the current pane.
        self.assertEqual([("current_pane",)], ops.calls)

    def test_resolve_guards_tmux_then_returns_resolved_target(self) -> None:
        ops = _FakePaneOps(resolved="%9")
        outcome = PanePrimitiveUseCase(ops).resolve(argparse.Namespace(target="codex"))
        self.assertEqual("%9", outcome.stdout)
        self.assertEqual("\n", outcome.stdout_end)
        self.assertEqual(
            [("require_tmux",), ("resolve_target", "codex")],
            ops.calls,
        )

    def test_read_captures_then_marks_read_with_no_trailing_newline(self) -> None:
        ops = _FakePaneOps(resolved="%2", captured="pane body")
        outcome = PanePrimitiveUseCase(ops).read(
            argparse.Namespace(target="codex", lines=40)
        )
        self.assertEqual("pane body", outcome.stdout)
        # `cmd_read` printed with ``end=""`` — preserved via stdout_end.
        self.assertEqual("", outcome.stdout_end)
        self.assertEqual(0, outcome.exit_code)
        self.assertEqual(
            [
                ("require_tmux",),
                ("resolve_target", "codex"),
                ("capture_pane", "%2", 40),
                ("mark_read", "%2"),
            ],
            ops.calls,
        )


class PanePrimitiveTypeKeysTest(unittest.TestCase):
    def test_type_sends_literal_text_gated_by_read_marker(self) -> None:
        ops = _FakePaneOps(resolved="%2")
        outcome = PanePrimitiveUseCase(ops).type_text(
            argparse.Namespace(target="codex", text="hello")
        )
        self.assertIsNone(outcome.stdout)
        self.assertEqual(0, outcome.exit_code)
        self.assertEqual(
            [
                ("require_tmux",),
                ("resolve_target", "codex"),
                ("require_read", "%2"),
                ("run_tmux", "send-keys", "-t", "%2", "-l", "--", "hello"),
                ("clear_read", "%2"),
            ],
            ops.calls,
        )

    def test_keys_forwards_raw_keys_gated_by_read_marker(self) -> None:
        ops = _FakePaneOps(resolved="%2")
        outcome = PanePrimitiveUseCase(ops).keys(
            argparse.Namespace(target="codex", keys=["C-u", "Enter"])
        )
        self.assertIsNone(outcome.stdout)
        self.assertEqual(
            [
                ("require_tmux",),
                ("resolve_target", "codex"),
                ("require_read", "%2"),
                ("run_tmux", "send-keys", "-t", "%2", "C-u", "Enter"),
                ("clear_read", "%2"),
            ],
            ops.calls,
        )


class PanePrimitiveMessageTest(unittest.TestCase):
    def _message_args(self, **overrides) -> argparse.Namespace:
        base = dict(text="hi", submit=True, submit_delay=0.0)
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_submit_path_marks_header_waits_then_presses_enter(self) -> None:
        ops = _FakePaneOps(pane="%1", resolved="%2", window_name="codex", location="agents:0.0")
        outcome = PanePrimitiveUseCase(ops).message(self._message_args())
        self.assertIsNone(outcome.stdout)
        self.assertEqual(0, outcome.exit_code)
        header = "[mozyo-bridge from:codex pane:%1 at:agents:0.0]"
        self.assertEqual(
            [
                ("require_tmux",),
                ("resolve_message_target", ops.calls[1][1]),
                ("require_read", "%2"),
                ("current_pane",),
                ("pane_window_name", "%1"),
                ("pane_location", "%1"),
                ("run_tmux", "send-keys", "-t", "%2", "-l", "--", f"{header} hi"),
                ("wait_for_text", "%2", header, 200, 8.0),
                ("run_tmux", "send-keys", "-t", "%2", "Enter"),
                ("clear_read", "%2"),
            ],
            ops.calls,
        )

    def test_sender_id_falls_back_to_pane_when_window_name_absent(self) -> None:
        ops = _FakePaneOps(pane="%5", resolved="%2", window_name=None, location="agents:1.0")
        PanePrimitiveUseCase(ops).message(self._message_args())
        header = "[mozyo-bridge from:%5 pane:%5 at:agents:1.0]"
        run_literal = [c for c in ops.calls if c[0] == "run_tmux" and "-l" in c]
        self.assertEqual(
            ("run_tmux", "send-keys", "-t", "%2", "-l", "--", f"{header} hi"),
            run_literal[0],
        )

    def test_marker_miss_rolls_back_clears_emits_guidance_and_dies(self) -> None:
        ops = _FakePaneOps(resolved="%2", wait_ok=False)
        with self.assertRaises(SystemExit):
            PanePrimitiveUseCase(ops).message(self._message_args(attempt=1))
        kinds = [c[0] for c in ops.calls]
        # Rollback + read clear + guidance precede die; Enter is never pressed.
        self.assertIn(("run_tmux", "send-keys", "-t", "%2", "C-u"), ops.calls)
        self.assertLess(kinds.index("emit_message_gate_guidance"), kinds.index("die"))
        self.assertNotIn(("run_tmux", "send-keys", "-t", "%2", "Enter"), ops.calls)
        # The guidance carries the attempt + no_submit (submit=True -> no_submit False).
        guidance = [c for c in ops.calls if c[0] == "emit_message_gate_guidance"][0]
        self.assertEqual(("emit_message_gate_guidance", "%2", 1, False), guidance)

    def test_require_read_systemexit_emits_guidance_then_reraises(self) -> None:
        ops = _FakePaneOps(resolved="%2", require_read_raises=True)
        with self.assertRaises(SystemExit):
            PanePrimitiveUseCase(ops).message(self._message_args(submit=False, attempt=2))
        # Guidance fires on the gate failure; nothing is sent afterwards.
        guidance = [c for c in ops.calls if c[0] == "emit_message_gate_guidance"][0]
        # submit=False -> no_submit True.
        self.assertEqual(("emit_message_gate_guidance", "%2", 2, True), guidance)
        self.assertFalse([c for c in ops.calls if c[0] == "run_tmux"])

    def test_no_submit_path_sends_literal_without_enter(self) -> None:
        ops = _FakePaneOps(resolved="%2")
        PanePrimitiveUseCase(ops).message(self._message_args(submit=False))
        # No wait, no Enter, no rollback — just the literal send + clear.
        self.assertFalse([c for c in ops.calls if c[0] == "wait_for_text"])
        self.assertNotIn(("run_tmux", "send-keys", "-t", "%2", "Enter"), ops.calls)
        self.assertEqual(("clear_read", "%2"), ops.calls[-1])

    def test_submit_delay_sleeps_before_enter(self) -> None:
        ops = _FakePaneOps(resolved="%2")
        PanePrimitiveUseCase(ops).message(self._message_args(submit_delay=0.5))
        self.assertIn(("sleep", 0.5), ops.calls)
        # Sleep immediately precedes the Enter send.
        enter_idx = ops.calls.index(("run_tmux", "send-keys", "-t", "%2", "Enter"))
        self.assertEqual(("sleep", 0.5), ops.calls[enter_idx - 1])


class PanePrimitiveBoundaryHygieneTest(unittest.TestCase):
    def test_module_does_not_import_commands_at_load(self) -> None:
        # The live adapter imports ``commands`` lazily at call time to preserve
        # the monkeypatch seams and avoid an import cycle; the module must not
        # import it at load.
        import ast

        import mozyo_bridge.application.pane_primitive_command as mod

        with open(mod.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        top_level_imports: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                top_level_imports.append(node.module)
            elif isinstance(node, ast.Import):
                top_level_imports.extend(alias.name for alias in node.names)
        self.assertFalse([m for m in top_level_imports if "application.commands" in m])


if __name__ == "__main__":
    unittest.main()
