"""OOP-first boundary for the doctor/instruction command tails (Redmine #12930).

This carves the residual ``cmd_doctor_instruction`` (``doctor instruction``
runbook) and ``cmd_instruction_doctor`` (``runtime-config check`` /
``instruction doctor`` alias) command bodies out of the orchestration module
into one bounded command boundary. Both tails share an identical shape — run a
diagnostic, render it as json or text, and map ``result["ok"]`` to an exit
code — so a single use case serves both:

- :class:`InstructionCommandOutcome`: the rendered stdout payload + process exit
  code the command produces.
- :class:`InstructionCommandUseCase`: composes an injected diagnostic runner with
  the json/text rendering decision and the ``result["ok"]`` -> exit-code mapping,
  leaving the ``cmd_doctor_instruction`` / ``cmd_instruction_doctor`` adapters
  thin composition roots that only print the outcome's stdout and return its
  exit code.

The runner and text renderer are injected callables so the thin adapters can
hand the use case the ``doctor_instruction`` / ``instruction_doctor`` module
functions resolved lazily *at call time*. That keeps the existing monkeypatch
seams (tests patch ``doctor_instruction.run_doctor`` / ``.run_instruction_doctor``
/ ``.doctor_target`` and drive the commands through ``args.func(args)``) driving
the live or patched diagnostic unchanged. This module never reads the
filesystem, never imports the diagnostic modules or the sibling
:mod:`mozyo_bridge.application.doctor_command` boundary, and never owns stdout
itself; rendering stays side-effect free.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class InstructionCommandOutcome:
    """The rendered stdout payload and process exit code of a doctor/instruction command.

    ``stdout`` is the single block the thin adapter hands to one ``print(...)``
    call (matching the legacy ``print(format_*_text(...))`` /
    ``print(json.dumps(...))`` behaviour byte-for-byte, trailing newline
    included). ``exit_code`` is ``0`` when the diagnostic result is healthy,
    ``1`` otherwise.
    """

    stdout: str
    exit_code: int


class InstructionCommandUseCase:
    """Compose the diagnostic run, the json/text rendering decision, and the exit code.

    The diagnostic runner and the text renderer are injected callables so the
    thin ``cmd_doctor_instruction`` / ``cmd_instruction_doctor`` adapters can
    supply the ``doctor_instruction`` / ``instruction_doctor`` module functions
    resolved at call time (preserving those modules' monkeypatch seams). The use
    case owns no stdout: it returns an :class:`InstructionCommandOutcome` the
    adapter prints.
    """

    def __init__(
        self,
        runner: Callable[[argparse.Namespace], dict[str, Any]],
        render_text: Callable[[dict[str, Any]], str],
    ) -> None:
        self._runner = runner
        self._render_text = render_text

    def execute(self, args: argparse.Namespace) -> InstructionCommandOutcome:
        result = self._runner(args)
        if getattr(args, "json", False):
            stdout = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        else:
            stdout = self._render_text(result)
        return InstructionCommandOutcome(
            stdout=stdout,
            exit_code=0 if result["ok"] else 1,
        )
