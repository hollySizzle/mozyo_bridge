"""OOP-first boundary for the low-level pane primitive command wrappers (Redmine #12932).

This carves the residual ``cmd_id`` / ``cmd_resolve`` / ``cmd_read`` /
``cmd_type`` / ``cmd_message`` / ``cmd_keys`` command bodies out of the
orchestration module into one bounded command boundary. These are the
``mozyo-bridge`` low-level pane/debug primitives — print the current pane, resolve
a semantic target to a ``%pane`` id, capture a pane, and the three
read-marker-gated ``send-keys`` writers (``type`` / ``message`` / ``keys``).
They share a common shape — a tmux availability guard, a target resolution, an
optional read-marker gate, and a ``send-keys`` write — so a single use case
serves the family:

- :class:`PanePrimitiveOutcome`: the optional stdout payload + process exit code
  a primitive produces. ``stdout is None`` means the thin adapter prints nothing
  (the ``type`` / ``message`` / ``keys`` writers are stdout-silent); ``stdout_end``
  preserves the ``cmd_read`` ``print(..., end="")`` (no trailing newline) shape
  byte-for-byte.
- :class:`PanePrimitiveOps`: the port protocol over every tmux / pane / read-marker
  primitive the family touches (availability, target resolution, capture,
  read-marker gate, ``send-keys``, the landing-marker probe, the message-gate
  guidance trailer, ``die``, and ``sleep``).
- :class:`LivePanePrimitiveOps`: the live adapter that routes every primitive
  through the :mod:`mozyo_bridge.application.commands` module globals *at call
  time*. That keeps the existing monkeypatch seams (tests patch
  ``commands.require_tmux`` / ``commands.run_tmux`` / ``commands.wait_for_text`` /
  ``commands.capture_pane`` and drive the commands through ``args.func(args)``)
  driving the live or patched primitive unchanged.
- :class:`PanePrimitiveUseCase`: composes the port into the six behavior-preserving
  flows, leaving the ``cmd_*`` adapters thin composition roots that only print the
  outcome's stdout and return its exit code.
- ``cmd_id`` / ``cmd_resolve`` / ``cmd_read`` / ``cmd_type`` / ``cmd_message`` /
  ``cmd_keys`` and their ``_emit_pane_primitive_outcome`` print helper: those thin
  composition roots themselves, moved here from ``commands.py`` (Redmine #13121)
  and re-exported there so the ``commands.cmd_*`` import surface, the cli /
  cli_core / cli_handoff parser bindings (``func.__name__`` unchanged), and the
  tests that patch ``commands.cmd_read`` / ``.cmd_message`` / ``.cmd_keys`` keep
  working (the notify / session-bootstrap internal callers resolve the handlers
  through the ``commands`` module attributes at call time).

The strict-rail ``message`` submission (marker observed → Enter, else ``C-u``
rollback + ``marker_timeout`` die) and its stderr guidance trailer are preserved
byte-for-byte here; this module changes no wire behavior. It imports nothing from
:mod:`mozyo_bridge.application.commands` at module load (the live adapter imports
it lazily), so there is no import cycle.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    NO_SUBMIT_RETRY_BUDGET,
)


def emit_message_gate_guidance(
    target: str,
    *,
    attempt: int | None = None,
    no_submit: bool = False,
) -> None:
    """Print the stderr guidance trailer after a `mozyo-bridge message` gate failure.

    The base ``error: ...`` line already names the literal next-action verb
    ("read target again", "must read target before interacting", etc.). This
    trailer is the structural anti-shortcut required by Asana task
    1214779823377861: it spells out the retry path (``mozyo-bridge read``,
    then re-run) and the per-preset ``--no-submit`` retry budget so an agent
    cannot conflate the budget with the ``handoff send`` retry pool or jump
    straight to the preset's ``Notification fails`` branch after one transient
    failure.
    """
    cap = NO_SUBMIT_RETRY_BUDGET
    print(
        f"hint: retry path: `mozyo-bridge read {target}` to refresh the read "
        f"marker, then re-run the failed `mozyo-bridge message` command.",
        file=sys.stderr,
    )
    if not no_submit:
        return
    if attempt is not None:
        used = max(0, int(attempt))
        remaining = max(0, cap - used)
        print(
            f"hint: --no-submit retry budget: attempt {used}/{cap} just "
            f"failed; {remaining}/{cap} attempts remaining per preset "
            "contract. Do not borrow from the `mozyo-bridge handoff send` "
            "retry pool — they are separate budgets.",
            file=sys.stderr,
        )
    else:
        print(
            f"hint: --no-submit retry budget: up to {cap} attempts per "
            "preset contract; pass `--attempt N` on each retry to track "
            "the remaining budget. Do not borrow from the `mozyo-bridge "
            "handoff send` retry pool — they are separate budgets.",
            file=sys.stderr,
        )


@dataclass(frozen=True)
class PanePrimitiveOutcome:
    """The optional stdout payload and process exit code of a pane primitive command.

    ``stdout`` is the single block the thin adapter hands to one ``print(...)``
    call, or ``None`` for the stdout-silent writers (``type`` / ``message`` /
    ``keys``). ``stdout_end`` is the ``print`` terminator so the ``cmd_read``
    ``print(..., end="")`` (no trailing newline) shape is preserved byte-for-byte
    while ``cmd_id`` / ``cmd_resolve`` keep the default newline. ``exit_code`` is
    ``0`` on the success paths; the strict-rail ``message`` failure raises
    ``SystemExit`` via :meth:`PanePrimitiveOps.die` instead of returning.
    """

    exit_code: int
    stdout: str | None = None
    stdout_end: str = "\n"


class PanePrimitiveOps(Protocol):
    """Port over every tmux / pane / read-marker primitive the family touches."""

    def require_tmux(self) -> None: ...

    def current_pane(self) -> str: ...

    def resolve_target(self, target: str) -> str: ...

    def resolve_message_target(self, args: argparse.Namespace) -> str: ...

    def capture_pane(self, target: str, lines: int) -> str: ...

    def mark_read(self, target: str) -> None: ...

    def require_read(self, target: str) -> None: ...

    def clear_read(self, target: str) -> None: ...

    def run_tmux(self, *args: str) -> Any: ...

    def pane_window_name(self, pane: str) -> str | None: ...

    def pane_location(self, pane: str) -> str: ...

    def wait_for_text(self, target: str, text: str, lines: int, timeout: float) -> bool: ...

    def emit_message_gate_guidance(
        self, target: str, *, attempt: int | None, no_submit: bool
    ) -> None: ...

    def die(self, message: str) -> None: ...

    def sleep(self, seconds: float) -> None: ...


class LivePanePrimitiveOps:
    """Live :class:`PanePrimitiveOps` adapter routing through ``commands.*`` at call time.

    Every primitive is resolved through the :mod:`mozyo_bridge.application.commands`
    module at call time so the existing ``commands.*`` monkeypatch seams (tests
    that patch ``commands.require_tmux`` / ``commands.run_tmux`` /
    ``commands.wait_for_text`` / ``commands.capture_pane`` and drive the commands
    through ``args.func(args)``) keep intercepting the live behavior unchanged.
    The lazy import also avoids an import cycle with ``commands``.
    """

    def require_tmux(self) -> None:
        from mozyo_bridge.application import commands

        commands.require_tmux()

    def current_pane(self) -> str:
        from mozyo_bridge.application import commands

        return commands.current_pane()

    def resolve_target(self, target: str) -> str:
        from mozyo_bridge.application import commands

        return commands.resolve_target(target)

    def resolve_message_target(self, args: argparse.Namespace) -> str:
        # Semantic target selection (Redmine #12663): `--select-role` resolves the
        # target pane by role + repo (+ session / project) instead of a `%pane` id.
        from mozyo_bridge.application.commands_target_select import resolve_message_target

        return resolve_message_target(args)

    def capture_pane(self, target: str, lines: int) -> str:
        from mozyo_bridge.application import commands

        return commands.capture_pane(target, lines)

    def mark_read(self, target: str) -> None:
        from mozyo_bridge.application import commands

        commands.mark_read(target)

    def require_read(self, target: str) -> None:
        from mozyo_bridge.application import commands

        commands.require_read(target)

    def clear_read(self, target: str) -> None:
        from mozyo_bridge.application import commands

        commands.clear_read(target)

    def run_tmux(self, *args: str) -> Any:
        from mozyo_bridge.application import commands

        return commands.run_tmux(*args)

    def pane_window_name(self, pane: str) -> str | None:
        from mozyo_bridge.application import commands

        return commands.pane_window_name(pane)

    def pane_location(self, pane: str) -> str:
        from mozyo_bridge.application import commands

        return commands.pane_location(pane)

    def wait_for_text(self, target: str, text: str, lines: int, timeout: float) -> bool:
        from mozyo_bridge.application import commands

        return commands.wait_for_text(target, text, lines, timeout)

    def emit_message_gate_guidance(
        self, target: str, *, attempt: int | None, no_submit: bool
    ) -> None:
        emit_message_gate_guidance(target, attempt=attempt, no_submit=no_submit)

    def die(self, message: str) -> None:
        from mozyo_bridge.application import commands

        commands.die(message)

    def sleep(self, seconds: float) -> None:
        from mozyo_bridge.application import commands

        commands.time.sleep(seconds)


class PanePrimitiveUseCase:
    """Compose the pane primitive port into the six behavior-preserving command flows.

    The port is injected so the thin ``cmd_*`` adapters can supply
    :class:`LivePanePrimitiveOps` (preserving the ``commands.*`` monkeypatch
    seams) while unit tests supply a fake port. Each method returns a
    :class:`PanePrimitiveOutcome` the adapter prints; the strict-rail ``message``
    failure raises ``SystemExit`` via ``die`` instead of returning.
    """

    def __init__(self, ops: PanePrimitiveOps) -> None:
        self._ops = ops

    def id(self, args: argparse.Namespace) -> PanePrimitiveOutcome:
        return PanePrimitiveOutcome(exit_code=0, stdout=self._ops.current_pane())

    def resolve(self, args: argparse.Namespace) -> PanePrimitiveOutcome:
        self._ops.require_tmux()
        return PanePrimitiveOutcome(exit_code=0, stdout=self._ops.resolve_target(args.target))

    def read(self, args: argparse.Namespace) -> PanePrimitiveOutcome:
        self._ops.require_tmux()
        target = self._ops.resolve_target(args.target)
        captured = self._ops.capture_pane(target, args.lines)
        self._ops.mark_read(target)
        return PanePrimitiveOutcome(exit_code=0, stdout=captured, stdout_end="")

    def type_text(self, args: argparse.Namespace) -> PanePrimitiveOutcome:
        self._ops.require_tmux()
        target = self._ops.resolve_target(args.target)
        self._ops.require_read(target)
        self._ops.run_tmux("send-keys", "-t", target, "-l", "--", args.text)
        self._ops.clear_read(target)
        return PanePrimitiveOutcome(exit_code=0)

    def message(self, args: argparse.Namespace) -> PanePrimitiveOutcome:
        self._ops.require_tmux()
        target = self._ops.resolve_message_target(args)
        attempt = getattr(args, "attempt", None)
        no_submit = not getattr(args, "submit", True)
        try:
            self._ops.require_read(target)
        except SystemExit:
            # `require_read` dies before returning; intercept so the structural
            # guidance trailer lands on stderr right after the base `error:` line.
            # Re-raise to preserve the SystemExit exit code.
            self._ops.emit_message_gate_guidance(target, attempt=attempt, no_submit=no_submit)
            raise
        sender = self._ops.current_pane()
        sender_id = self._ops.pane_window_name(sender) or sender
        header = f"[mozyo-bridge from:{sender_id} pane:{sender} at:{self._ops.pane_location(sender)}]"
        self._ops.run_tmux("send-keys", "-t", target, "-l", "--", f"{header} {args.text}")
        if getattr(args, "submit", True):
            landing_timeout = float(getattr(args, "landing_timeout", 8.0) or 8.0)
            read_lines = int(getattr(args, "read_lines", 50) or 50)
            landing_lines = max(read_lines, 200)
            if not self._ops.wait_for_text(target, header, landing_lines, landing_timeout):
                self._ops.run_tmux("send-keys", "-t", target, "C-u")
                self._ops.clear_read(target)
                self._ops.emit_message_gate_guidance(target, attempt=attempt, no_submit=no_submit)
                self._ops.die(
                    "message marker was not observed in target pane; a C-u rollback was issued and Enter was not pressed (the receiver composer state was not verified). "
                    f"target={target} marker={header}"
                )
            submit_delay = max(0.0, float(getattr(args, "submit_delay", 0.2) or 0.0))
            if submit_delay:
                self._ops.sleep(submit_delay)
            self._ops.run_tmux("send-keys", "-t", target, "Enter")
        self._ops.clear_read(target)
        return PanePrimitiveOutcome(exit_code=0)

    def keys(self, args: argparse.Namespace) -> PanePrimitiveOutcome:
        self._ops.require_tmux()
        target = self._ops.resolve_target(args.target)
        self._ops.require_read(target)
        self._ops.run_tmux("send-keys", "-t", target, *args.keys)
        self._ops.clear_read(target)
        return PanePrimitiveOutcome(exit_code=0)


# Thin composition roots (moved from ``commands.py``, Redmine #13121): print the
# outcome's stdout and return its exit code, nothing else. Parser-bound through
# the ``commands`` re-export so ``func.__name__`` and the ``commands.cmd_*``
# monkeypatch seams are unchanged; the live port keeps resolving every primitive
# through the ``commands`` module globals at call time (#12932).
def _emit_pane_primitive_outcome(outcome: PanePrimitiveOutcome) -> int:
    if outcome.stdout is not None:
        print(outcome.stdout, end=outcome.stdout_end)
    return outcome.exit_code


def cmd_id(_: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).id(_))


def cmd_resolve(args: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).resolve(args))


def cmd_read(args: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).read(args))


def cmd_type(args: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).type_text(args))


def cmd_message(args: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).message(args))


def cmd_keys(args: argparse.Namespace) -> int:
    return _emit_pane_primitive_outcome(PanePrimitiveUseCase(LivePanePrimitiveOps()).keys(args))
