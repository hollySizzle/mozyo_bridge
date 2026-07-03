"""OOP-first boundary for the instruction-install command entry (Redmine #12935).

This carves the residual ``cmd_instruction_install`` command body — the
``runtime-config install`` / deprecated ``instruction install`` entry that
projects the verified Redmine default into ``<repo>/.codex/config.toml`` — out of
the orchestration module into one bounded command boundary:

- :class:`InstructionInstallOutcome`: the rendered stdout payload + process exit
  code the command produces.
- :class:`InstructionInstallUseCase`: composes an injected install runner with
  the json/text rendering decision and the ``result["ok"]`` -> exit-code mapping,
  leaving the ``cmd_instruction_install`` adapter a thin composition root that
  only prints the outcome's stdout and returns its exit code.
- :func:`cmd_instruction_install`: that thin composition root itself, moved here
  from the orchestration module in the #13104 wrapper facade cleanup.
  :mod:`mozyo_bridge.application.commands` re-exports it so the parser binding
  and the ``commands.cmd_instruction_install`` import / monkeypatch surface are
  unchanged.

The runner and text renderer are injected callables so the thin adapter can hand
the use case the :mod:`mozyo_bridge.application.instruction_install` module
functions resolved lazily *at call time* (imported inside the adapter body).
That keeps the existing monkeypatch seams (tests patch / import
``instruction_install.run_instruction_install`` /
``.format_instruction_install_text`` and drive the command through
``args.func(args)``) driving the live or patched install unchanged. The use case
never reads the filesystem and never owns stdout itself; rendering stays
side-effect free, and the one ``print(...)`` lives in the adapter.

The sibling doctor/instruction boundary
(:mod:`mozyo_bridge.application.doctor_instruction_command`, Redmine #12930)
shares the identical run -> json/text render -> ``result["ok"]`` exit-code shape,
but it is documented and named for read-only *diagnostics*. ``instruction
install`` is the write side (it renders/merges managed tables into the runtime
config), so it keeps its own bounded boundary here rather than coupling the
write-side entry to #12930's diagnostic module. Both boundaries stay independent
so this tranche does not touch the #12930 surface.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class InstructionInstallOutcome:
    """The rendered stdout payload and process exit code of ``instruction install``.

    ``stdout`` is the single block the thin adapter hands to one ``print(...)``
    call (matching the legacy ``print(format_instruction_install_text(...))`` /
    ``print(json.dumps(...))`` behaviour byte-for-byte, trailing newline
    included). ``exit_code`` is ``0`` when the install result is healthy, ``1``
    otherwise.
    """

    stdout: str
    exit_code: int


class InstructionInstallUseCase:
    """Compose the install run, the json/text rendering decision, and the exit code.

    The install runner and the text renderer are injected callables so the thin
    ``cmd_instruction_install`` adapter can supply the
    :mod:`mozyo_bridge.application.instruction_install` module functions resolved
    at call time (preserving that module's monkeypatch seams). The use case owns
    no stdout: it returns an :class:`InstructionInstallOutcome` the adapter prints.
    """

    def __init__(
        self,
        runner: Callable[[argparse.Namespace], dict[str, Any]],
        render_text: Callable[[dict[str, Any]], str],
    ) -> None:
        self._runner = runner
        self._render_text = render_text

    def execute(self, args: argparse.Namespace) -> InstructionInstallOutcome:
        result = self._runner(args)
        if getattr(args, "json", False):
            stdout = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        else:
            stdout = self._render_text(result)
        return InstructionInstallOutcome(
            stdout=stdout,
            exit_code=0 if result["ok"] else 1,
        )


def cmd_instruction_install(args: argparse.Namespace) -> int:
    # Thin handler over ``InstructionInstallUseCase`` above (#12935). Lazy
    # imports preserve the ``instruction_install`` monkeypatch seams
    # (``commands`` re-exports this adapter, #13104).
    from mozyo_bridge.application.instruction_install import (
        format_instruction_install_text,
        run_instruction_install,
    )

    outcome = InstructionInstallUseCase(
        run_instruction_install, format_instruction_install_text
    ).execute(args)
    print(outcome.stdout)
    return outcome.exit_code
