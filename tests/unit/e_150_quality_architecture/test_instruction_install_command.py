"""Use-case specifications for the instruction-install command boundary (#12935).

These exercise the ``instruction_install_command`` boundary directly — no real
install run, no filesystem. They pin the ``InstructionInstallUseCase`` that backs
``cmd_instruction_install`` (``runtime-config install`` / deprecated ``instruction
install``):

- the json/text rendering decision (``args.json`` true -> sorted, indented
  ``json.dumps``; false -> the injected text renderer) byte-for-byte,
- the ``result["ok"]`` -> exit-code mapping (ok -> 0, not ok -> 1) for both
  rendering branches,
- that the runner and the renderer are *injected* callables the use case calls
  (so the thin adapter can hand it the ``instruction_install`` module functions
  resolved at call time, preserving that module's monkeypatch seams),
- the relocated ``cmd_instruction_install`` adapter (#13104): re-exported from
  ``commands`` as the same object, resolving the install module lazily at call
  time, printing the outcome's stdout once, and returning its exit code.

The end-to-end behavior over the real ``run_instruction_install`` /
``format_instruction_install_text`` install stays pinned by the CLI tests in
``tests/test_runtime_config_instruction.py``; this file pins the command entry in
isolation, which is the OOP-first carve's payoff.
"""

from __future__ import annotations

import argparse
import json
import unittest

from mozyo_bridge.application.instruction_install_command import (
    InstructionInstallOutcome,
    InstructionInstallUseCase,
)


def _result(ok: bool) -> dict:
    return {
        "ok": ok,
        "profile": "redmine-codex",
        "action": "up-to-date" if ok else "conflict",
        "target": "/repo",
        "messages": ["example"],
    }


class InstructionInstallUseCaseTest(unittest.TestCase):
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
        outcome = InstructionInstallUseCase(fake_runner, fake_render).execute(args)

        self.assertIsInstance(outcome, InstructionInstallOutcome)
        self.assertEqual("RENDERED TEXT", outcome.stdout)
        self.assertEqual(0, outcome.exit_code)
        # The runner is the injected callable, called once with the args.
        self.assertEqual([args], calls)

    def test_not_ok_result_maps_to_exit_code_one(self) -> None:
        args = argparse.Namespace(json=False)
        outcome = InstructionInstallUseCase(
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
        outcome = InstructionInstallUseCase(lambda _a: result, fake_render).execute(args)

        self.assertEqual(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            outcome.stdout,
        )
        self.assertEqual(0, outcome.exit_code)
        # The text renderer is not consulted on the json path.
        self.assertEqual([], rendered)

    def test_json_branch_exit_code_follows_result_ok(self) -> None:
        args = argparse.Namespace(json=True)
        outcome = InstructionInstallUseCase(
            lambda _a: _result(ok=False),
            lambda _r: "unused",
        ).execute(args)
        self.assertEqual(1, outcome.exit_code)

    def test_missing_json_attr_defaults_to_text_branch(self) -> None:
        # ``getattr(args, "json", False)`` -> a namespace without ``json`` renders text.
        args = argparse.Namespace()
        outcome = InstructionInstallUseCase(
            lambda _a: _result(ok=True),
            lambda _r: "text-default",
        ).execute(args)
        self.assertEqual("text-default", outcome.stdout)

    def test_use_case_never_imports_the_install_or_diagnostic_modules(self) -> None:
        # The write-side entry keeps its own boundary; it must not couple to the
        # #12930 diagnostic boundary (this tranche does not touch that surface),
        # not even lazily. The install module may be imported *only* lazily
        # inside the relocated ``cmd_instruction_install`` adapter body (#13104),
        # never at module import time, so importing this boundary stays
        # cycle-free and side-effect free.
        import ast

        import mozyo_bridge.application.instruction_install_command as mod

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
        self.assertFalse([m for m in top_level if m.endswith("instruction_install")])

        everywhere = modules_of(list(ast.walk(tree)))
        self.assertFalse(
            [m for m in everywhere if m.endswith("doctor_instruction_command")]
        )


class RelocatedAdapterTest(unittest.TestCase):
    """Pin the #13104 move: the adapter lives here, ``commands`` re-exports it."""

    def test_commands_re_export_is_same_object(self) -> None:
        from mozyo_bridge.application import commands, instruction_install_command

        self.assertIs(
            commands.cmd_instruction_install,
            instruction_install_command.cmd_instruction_install,
        )

    def test_cmd_instruction_install_resolves_seams_at_call_time(self) -> None:
        import contextlib
        import io
        from unittest.mock import patch

        from mozyo_bridge.application.instruction_install_command import (
            cmd_instruction_install,
        )

        args = argparse.Namespace(json=False)
        with patch(
            "mozyo_bridge.application.instruction_install.run_instruction_install",
            return_value=_result(ok=False),
        ), patch(
            "mozyo_bridge.application.instruction_install.format_instruction_install_text",
            return_value="patched install text",
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            exit_code = cmd_instruction_install(args)

        self.assertEqual(1, exit_code)
        self.assertEqual("patched install text\n", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
