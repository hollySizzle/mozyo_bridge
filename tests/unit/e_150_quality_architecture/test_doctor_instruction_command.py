"""Use-case specifications for the doctor/instruction command boundary (#12930).

These exercise the ``doctor_instruction_command`` boundary directly — no real
diagnostic run, no filesystem. They pin the shared ``InstructionCommandUseCase``
that backs both ``cmd_doctor_instruction`` (``doctor instruction`` runbook) and
``cmd_instruction_doctor`` (``runtime-config check``):

- the json/text rendering decision (``args.json`` true -> sorted, indented
  ``json.dumps``; false -> the injected text renderer) byte-for-byte,
- the ``result["ok"]`` -> exit-code mapping (ok -> 0, not ok -> 1) for both
  rendering branches,
- that the runner and the renderer are *injected* callables the use case calls
  (so the thin adapters can hand it the ``doctor_instruction`` /
  ``instruction_doctor`` module functions resolved at call time, preserving those
  modules' monkeypatch seams),
- the relocated ``cmd_doctor_instruction`` / ``cmd_instruction_doctor`` adapters
  (#13104): re-exported from ``commands`` as the same objects, resolving their
  diagnostic modules lazily at call time, printing the outcome's stdout once,
  and returning its exit code.

The end-to-end behavior over the real ``run_doctor_instruction`` /
``run_instruction_doctor`` diagnostics stays pinned by the CLI tests in
``tests/test_runtime_config_instruction.py``; this file pins the command tail in
isolation, which is the OOP-first carve's payoff.
"""

from __future__ import annotations

import argparse
import json
import unittest

from mozyo_bridge.application.doctor_instruction_command import (
    InstructionCommandOutcome,
    InstructionCommandUseCase,
)


def _result(ok: bool) -> dict:
    return {"ok": ok, "checks": [{"name": "example", "status": "ok" if ok else "fail"}]}


class InstructionCommandUseCaseTest(unittest.TestCase):
    def test_text_branch_uses_injected_renderer_and_ok_exit_code(self) -> None:
        result = _result(ok=True)
        calls: list[object] = []

        def fake_runner(args: argparse.Namespace) -> dict:
            calls.append(args)
            return result

        def fake_render(payload: dict) -> str:
            self.assertIs(payload, result)
            return "RENDERED TEXT"

        args = argparse.Namespace(json=False)
        outcome = InstructionCommandUseCase(fake_runner, fake_render).execute(args)

        self.assertIsInstance(outcome, InstructionCommandOutcome)
        self.assertEqual("RENDERED TEXT", outcome.stdout)
        self.assertEqual(0, outcome.exit_code)
        # The runner is the injected callable, called once with the args.
        self.assertEqual([args], calls)

    def test_not_ok_result_maps_to_exit_code_one(self) -> None:
        args = argparse.Namespace(json=False)
        outcome = InstructionCommandUseCase(
            lambda _a: _result(ok=False),
            lambda _r: "text",
        ).execute(args)
        self.assertEqual(1, outcome.exit_code)

    def test_json_branch_serializes_result_sorted_and_indented(self) -> None:
        result = _result(ok=True)
        rendered: list[dict] = []

        def fake_render(payload: dict) -> str:
            rendered.append(payload)
            return "should-not-be-used"

        args = argparse.Namespace(json=True)
        outcome = InstructionCommandUseCase(lambda _a: result, fake_render).execute(args)

        self.assertEqual(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            outcome.stdout,
        )
        self.assertEqual(0, outcome.exit_code)
        # The text renderer is not consulted on the json path.
        self.assertEqual([], rendered)

    def test_json_branch_exit_code_follows_result_ok(self) -> None:
        args = argparse.Namespace(json=True)
        outcome = InstructionCommandUseCase(
            lambda _a: _result(ok=False),
            lambda _r: "unused",
        ).execute(args)
        self.assertEqual(1, outcome.exit_code)

    def test_missing_json_attr_defaults_to_text_branch(self) -> None:
        # ``getattr(args, "json", False)`` -> a namespace without ``json`` renders text.
        args = argparse.Namespace()
        outcome = InstructionCommandUseCase(
            lambda _a: _result(ok=True),
            lambda _r: "text-default",
        ).execute(args)
        self.assertEqual("text-default", outcome.stdout)

    def test_use_case_never_imports_the_doctor_command_boundary(self) -> None:
        # The two tails share this boundary but must not couple to the sibling
        # ``doctor_command`` boundary (#12930 requires no cycle between them) —
        # not even lazily. The diagnostic modules (``doctor_instruction`` /
        # ``instruction_doctor``) may be imported *only* lazily inside the
        # relocated adapter bodies (#13104), never at module import time, so
        # importing this boundary stays cycle-free and side-effect free.
        import ast

        import mozyo_bridge.application.doctor_instruction_command as mod

        with open(mod.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        def modules_of(nodes: list[ast.stmt]) -> list[str]:
            found: list[str] = []
            for node in nodes:
                if isinstance(node, ast.ImportFrom) and node.module:
                    found.append(node.module)
                elif isinstance(node, ast.Import):
                    found.extend(alias.name for alias in node.names)
            return found

        top_level = modules_of(tree.body)
        self.assertFalse([m for m in top_level if m.endswith("doctor_instruction")])
        self.assertFalse([m for m in top_level if m.endswith("instruction_doctor")])

        everywhere = modules_of(list(ast.walk(tree)))
        self.assertFalse([m for m in everywhere if m.endswith("doctor_command")])


class RelocatedAdapterTest(unittest.TestCase):
    """Pin the #13104 move: the adapters live here, ``commands`` re-exports them."""

    def test_commands_re_exports_are_same_objects(self) -> None:
        from mozyo_bridge.application import commands, doctor_instruction_command

        self.assertIs(
            commands.cmd_doctor_instruction,
            doctor_instruction_command.cmd_doctor_instruction,
        )
        self.assertIs(
            commands.cmd_instruction_doctor,
            doctor_instruction_command.cmd_instruction_doctor,
        )

    def test_cmd_doctor_instruction_resolves_seams_at_call_time(self) -> None:
        import contextlib
        import io
        from unittest.mock import patch

        from mozyo_bridge.application.doctor_instruction_command import (
            cmd_doctor_instruction,
        )

        args = argparse.Namespace(json=False)
        with patch(
            "mozyo_bridge.application.doctor_instruction.run_doctor_instruction",
            return_value=_result(ok=False),
        ), patch(
            "mozyo_bridge.application.doctor_instruction.format_doctor_instruction_text",
            return_value="patched instruction text",
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            exit_code = cmd_doctor_instruction(args)

        self.assertEqual(1, exit_code)
        self.assertEqual("patched instruction text\n", stdout.getvalue())

    def test_cmd_instruction_doctor_resolves_seams_at_call_time(self) -> None:
        import contextlib
        import io
        from unittest.mock import patch

        from mozyo_bridge.application.doctor_instruction_command import (
            cmd_instruction_doctor,
        )

        args = argparse.Namespace(json=False)
        with patch(
            "mozyo_bridge.application.instruction_doctor.run_instruction_doctor",
            return_value=_result(ok=True),
        ), patch(
            "mozyo_bridge.application.instruction_doctor.format_instruction_doctor_text",
            return_value="patched runtime-config text",
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            exit_code = cmd_instruction_doctor(args)

        self.assertEqual(0, exit_code)
        self.assertEqual("patched runtime-config text\n", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
