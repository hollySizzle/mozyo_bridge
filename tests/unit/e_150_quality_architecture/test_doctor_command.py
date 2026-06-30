"""Use-case / pure-renderer specifications for the doctor command boundary (#12927).

These exercise the ``doctor_command`` boundary directly — no real doctor run, no
tmux server, no filesystem. They pin:

- the use case's json/text rendering decision (``args.json`` true -> sorted,
  indented ``json.dumps``; false -> the injected text renderer) byte-for-byte,
- the ``result["ok"]`` -> exit-code mapping (ok -> 0, not ok -> 1) for both
  rendering branches,
- that the runner and the renderer are *injected* callables the use case calls
  (so the thin ``cmd_doctor`` adapter can hand it the ``commands.*`` globals
  resolved at call time, preserving the existing monkeypatch surface),
- the relocated pure ``format_doctor_text`` still renders the same legacy text
  and is re-exported from ``doctor`` for backward-compatible importers.

The end-to-end behavior over the real ``run_doctor`` / section collectors stays
pinned by the ``cmd_doctor`` characterization tests in
``tests/integration/.../test_mozyo_bridge.py``; this file pins the command tail
in isolation, which is the OOP-first carve's payoff.
"""

from __future__ import annotations

import argparse
import json
import unittest

from mozyo_bridge.application.doctor_command import (
    DoctorCommandOutcome,
    DoctorCommandUseCase,
    format_doctor_text,
)


def _minimal_result(ok: bool) -> dict:
    """A minimal ``run_doctor`` result shape that ``format_doctor_text`` accepts."""
    return {
        "ok": ok,
        "sections": {
            "tmux": {"status": "ok" if ok else "warning", "next_action": []},
        },
    }


class DoctorCommandUseCaseTest(unittest.TestCase):
    def test_text_branch_uses_injected_renderer_and_ok_exit_code(self) -> None:
        result = _minimal_result(ok=True)
        calls: list[object] = []

        def fake_run_doctor(args: argparse.Namespace) -> dict:
            calls.append(args)
            return result

        def fake_render(payload: dict) -> str:
            self.assertIs(payload, result)
            return "RENDERED TEXT"

        args = argparse.Namespace(json=False)
        outcome = DoctorCommandUseCase(fake_run_doctor, fake_render).execute(args)

        self.assertIsInstance(outcome, DoctorCommandOutcome)
        self.assertEqual("RENDERED TEXT", outcome.stdout)
        self.assertEqual(0, outcome.exit_code)
        # The runner is the injected callable, called once with the args.
        self.assertEqual([args], calls)

    def test_not_ok_result_maps_to_exit_code_one(self) -> None:
        args = argparse.Namespace(json=False)
        outcome = DoctorCommandUseCase(
            lambda _a: _minimal_result(ok=False),
            lambda _r: "text",
        ).execute(args)
        self.assertEqual(1, outcome.exit_code)

    def test_json_branch_serializes_result_sorted_and_indented(self) -> None:
        result = _minimal_result(ok=True)
        rendered: list[dict] = []

        def fake_render(payload: dict) -> str:
            rendered.append(payload)
            return "should-not-be-used"

        args = argparse.Namespace(json=True)
        outcome = DoctorCommandUseCase(lambda _a: result, fake_render).execute(args)

        self.assertEqual(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            outcome.stdout,
        )
        self.assertEqual(0, outcome.exit_code)
        # The text renderer is not consulted on the json path.
        self.assertEqual([], rendered)

    def test_json_branch_exit_code_follows_result_ok(self) -> None:
        args = argparse.Namespace(json=True)
        outcome = DoctorCommandUseCase(
            lambda _a: _minimal_result(ok=False),
            format_doctor_text,
        ).execute(args)
        self.assertEqual(1, outcome.exit_code)

    def test_missing_json_attr_defaults_to_text_branch(self) -> None:
        # ``getattr(args, "json", False)`` -> a namespace without ``json`` renders text.
        args = argparse.Namespace()
        outcome = DoctorCommandUseCase(
            lambda _a: _minimal_result(ok=True),
            lambda _r: "text-default",
        ).execute(args)
        self.assertEqual("text-default", outcome.stdout)

    def test_default_renderer_is_format_doctor_text(self) -> None:
        # The renderer defaults to the bounded module's pure ``format_doctor_text``.
        result = _minimal_result(ok=True)
        args = argparse.Namespace(json=False)
        outcome = DoctorCommandUseCase(lambda _a: result).execute(args)
        self.assertEqual(format_doctor_text(result), outcome.stdout)


class FormatDoctorTextTest(unittest.TestCase):
    def test_relocated_renderer_emits_legacy_result_line(self) -> None:
        text_ok = format_doctor_text(_minimal_result(ok=True))
        self.assertTrue(text_ok.endswith("result: ok"))
        self.assertIn("tmux: ok", text_ok)

        text_bad = format_doctor_text(_minimal_result(ok=False))
        self.assertTrue(text_bad.endswith("result: needs attention"))

    def test_re_exported_from_doctor_module_is_same_object(self) -> None:
        from mozyo_bridge.application import doctor

        self.assertIs(doctor.format_doctor_text, format_doctor_text)


if __name__ == "__main__":
    unittest.main()
