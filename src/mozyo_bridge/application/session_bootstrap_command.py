"""Bare ``mozyo`` / repo session bootstrap adapter boundary (#12975).

The bare ``mozyo`` entry and its repo-session startup path historically leaned on
a tail of procedural helpers in :mod:`mozyo_bridge.application.commands`, each
mixing a *side effect* (a tmux read / write, a pane-process poll, a session
window create) with a small *pure projection* (a window-name line parse, the
TUI-wrap marker normalization). This module carves that tail into an OOP-first
boundary under #12638:

- The module-level ``project_*`` / ``marker_visible_in`` / ``pane_command_basename``
  helpers are the pure projection: window-name parsing, the three TUI-wrap marker
  normalizations, and the pane foreground-command basename extraction, all with no
  tmux dependency (exercisable on a raw string / dict).
- :class:`SessionBootstrapOps` is the port over the tmux / pane / session
  primitives the use case needs from its environment, and
  :class:`LiveSessionBootstrapOps` the live adapter. The adapter resolves every
  primitive *through the* :mod:`commands` *module at call time*, so the
  characterization tests that patch ``mozyo_bridge.application.commands.run_tmux``
  / ``commands.list_session_windows`` / ``commands.wait_for_agent_terminal_pane``
  / ``commands.wait_for_text`` / ``commands.rollback_unsubmitted_input`` /
  ``commands.ensure_repo_session_windows`` (and the window-create seams they build
  on) keep intercepting unchanged, and this module never imports :mod:`commands`
  at module scope (no import cycle).
- :class:`SessionBootstrapUseCase` composes the port and the projection into the
  five behavior-preserving flows (``list_session_windows`` /
  ``wait_for_agent_terminal_pane`` / ``wait_for_text`` /
  ``rollback_unsubmitted_input`` / ``ensure_repo_session_windows``). The thin
  wrappers in :mod:`commands` build the live ops and run the use case.

Behavior-preserving: the read tolerance (a failed ``list-windows`` read degrades
to ``[]``), the pane-startup poll + timeout ``die`` wording, the ``wait_for_text``
poll interval / TUI-wrap normalizations / fail-closed ``False``, the ``C-u``
rollback send, and the ``ensure_repo_session_windows`` window-model orchestration
(create-missing, config load ordering, per-agent target + ready-wait + subtle
style) are unchanged from the original command bodies. ``cmd_mozyo`` itself is
already a thin composition root over the ``launch_command`` boundary (#12933) and
stays there.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


# --- Pure projection: line parsing, marker normalization, command basename. ---


def project_session_window_names(stdout: str) -> list[str]:
    """Parse ``list-windows -F '#{window_name}'`` output into window names.

    One trimmed name per non-blank line; blank lines are dropped. Kept pure so it
    is exercisable on a raw stdout string with no tmux dependency.
    """

    return [name.strip() for name in (stdout or "").splitlines() if name.strip()]


# Receiver TUIs (codex CLI, Claude Code) wrap long input at the visible pane
# width, emitting a literal newline + continuation indent inside the captured
# text. tmux ``capture-pane -J`` only rejoins lines tmux itself wrapped, so a raw
# substring search would miss a marker split by the TUI wrap even though it landed
# cleanly on the wire.
_WRAP_INDENT = re.compile(r"\n\s+")


def marker_visible_in(captured: str, text: str) -> bool:
    """One-shot check whether ``text`` is visible in ``captured`` pane text.

    Two wrap shapes are observed in practice and require different normalize
    functions:

    1. word-boundary wrap (``mozyo-bridge message`` markers like
       ``[mozyo-bridge from:claude pane:%110 at:mozyo_bridge:2.0]``, ~60 chars,
       contain whitespace) — the TUI wraps at a space, so collapsing ``\\n\\s+``
       into a single ``" "`` reconstructs the original.
    2. character-wrap (``mozyo-bridge handoff`` markers like
       ``[mozyo:handoff:source=asana:task=...:comment=...:kind=...:to=...]``,
       100+ chars, contain no whitespace) — the TUI wraps at an arbitrary
       character boundary, so the only normalize that reconstructs the original is
       collapsing ``\\n\\s+`` to the empty string.

    Try the raw match first (cheap, scrollback-safe), then both wrap normalizes
    before declaring the marker absent. All three paths still return ``False``
    when the marker is genuinely missing, preserving the fail-closed rollback
    contract.
    """

    if text in captured:
        return True
    if text in _WRAP_INDENT.sub(" ", captured):
        return True
    if text in _WRAP_INDENT.sub("", captured):
        return True
    return False


def pane_command_basename(info: dict) -> str:
    """The basename of a pane's foreground command from a ``pane_info`` dict.

    ``Path(...).name`` of the (possibly absolute) ``command`` field; a missing /
    empty command reads as ``""`` so :meth:`SessionBootstrapUseCase.wait_for_agent_terminal_pane`
    can test agent readiness without raising.
    """

    return Path(info.get("command") or "").name


# --- Port + live adapter over the ``commands`` seams. -------------------------


@runtime_checkable
class SessionBootstrapOps(Protocol):
    """Port: the primitives the session bootstrap use case needs from its environment.

    The live adapter routes every method through the :mod:`commands` module at
    call time so the monkeypatched characterization tests still intercept, and so
    this module never imports :mod:`commands` at module scope (no import cycle).
    ``list_session_windows`` / ``wait_for_agent_terminal_pane`` are deliberately
    routed through the *thin ``commands`` wrappers* (not re-implemented here) so a
    test that patches ``commands.list_session_windows`` /
    ``commands.wait_for_agent_terminal_pane`` still steers the
    ``ensure_repo_session_windows`` flow.
    """

    def run_tmux(self, *args: Any, **kwargs: Any) -> Any: ...

    def pane_info(self, target: str) -> dict: ...

    def is_agent_process(self, command: str) -> bool: ...

    def capture_pane(self, target: str, lines: int) -> str: ...

    def run_keys(self, target: str, keys: list[str]) -> None: ...

    def die(self, message: str) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...

    def require_tmux(self) -> None: ...

    def session_exists(self, session: str) -> bool: ...

    def load_tmux_conf_for(self, args: argparse.Namespace) -> bool: ...

    def new_agent_session_window(
        self, agent: str, session: str, cwd: str | None
    ) -> str: ...

    def new_agent_window(self, agent: str, session: str, cwd: str | None) -> str: ...

    def find_agent_window(self, agent: str, session: str) -> dict | None: ...

    def ensure_agent_target(
        self, pane: dict, expected_agent: str, force: bool
    ) -> None: ...

    def apply_window_subtle_style(self, session: str, window: str) -> bool: ...

    def list_session_windows(self, session: str) -> list[str]: ...

    def wait_for_agent_terminal_pane(
        self, pane_id: str, agent: str, timeout: float
    ) -> None: ...


class LiveSessionBootstrapOps:
    """Live :class:`SessionBootstrapOps` over the real ``commands`` seams.

    Each method resolves its target *through the* :mod:`commands` *module at call
    time* rather than binding it at import time, so the tests that patch the
    ``commands.*`` names (direct reads, the window-create helpers, and the
    ``commands.list_session_windows`` / ``commands.wait_for_agent_terminal_pane``
    thin wrappers the bootstrap flow composes over) keep intercepting the live
    behavior. The lazy import also avoids an import cycle with ``commands``.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def run_tmux(self, *args: Any, **kwargs: Any) -> Any:
        return self._commands().run_tmux(*args, **kwargs)

    def pane_info(self, target: str) -> dict:
        return self._commands().pane_info(target)

    def is_agent_process(self, command: str) -> bool:
        return self._commands().is_agent_process(command)

    def capture_pane(self, target: str, lines: int) -> str:
        return self._commands().capture_pane(target, lines)

    def run_keys(self, target: str, keys: list[str]) -> None:
        self._commands().cmd_keys(argparse.Namespace(target=target, keys=list(keys)))

    def die(self, message: str) -> None:
        self._commands().die(message)

    def sleep(self, seconds: float) -> None:
        self._commands().time.sleep(seconds)

    def monotonic(self) -> float:
        return self._commands().time.monotonic()

    def require_tmux(self) -> None:
        self._commands().require_tmux()

    def session_exists(self, session: str) -> bool:
        return self._commands().session_exists(session)

    def load_tmux_conf_for(self, args: argparse.Namespace) -> bool:
        return self._commands().load_tmux_conf_for(args)

    def new_agent_session_window(
        self, agent: str, session: str, cwd: str | None
    ) -> str:
        return self._commands().new_agent_session_window(agent, session, cwd=cwd)

    def new_agent_window(self, agent: str, session: str, cwd: str | None) -> str:
        return self._commands().new_agent_window(agent, session, cwd=cwd)

    def find_agent_window(self, agent: str, session: str) -> dict | None:
        return self._commands().find_agent_window(agent, session)

    def ensure_agent_target(
        self, pane: dict, expected_agent: str, force: bool
    ) -> None:
        self._commands().ensure_agent_target(pane, expected_agent, force=force)

    def apply_window_subtle_style(self, session: str, window: str) -> bool:
        return self._commands().apply_window_subtle_style(session, window)

    def list_session_windows(self, session: str) -> list[str]:
        # Route through the thin ``commands`` wrapper (not our own use-case
        # method) so a test that patches ``commands.list_session_windows`` still
        # steers the ``ensure_repo_session_windows`` window-model discovery.
        return self._commands().list_session_windows(session)

    def wait_for_agent_terminal_pane(
        self, pane_id: str, agent: str, timeout: float
    ) -> None:
        # Route through the thin ``commands`` wrapper so a test that patches
        # ``commands.wait_for_agent_terminal_pane`` still intercepts the
        # per-agent ready-wait inside ``ensure_repo_session_windows``.
        self._commands().wait_for_agent_terminal_pane(pane_id, agent, timeout)


# --- Use case: compose the port + projection into the caller-facing flows. -----


class SessionBootstrapUseCase:
    """The bare ``mozyo`` / repo session bootstrap flows over the injected port.

    Every flow is behavior-preserving with the original ``commands`` bodies: the
    tolerant ``[]`` on a failed window read, the pane-startup poll + timeout
    ``die``, the ``wait_for_text`` poll interval + fail-closed ``False``, the
    ``C-u`` rollback, and the ``ensure_repo_session_windows`` window-model
    orchestration. The port is injected so the thin ``commands`` wrappers supply
    :class:`LiveSessionBootstrapOps` (preserving the ``commands.*`` monkeypatch
    seams) while unit tests supply a fake port.
    """

    def __init__(self, ops: SessionBootstrapOps) -> None:
        self._ops = ops

    def list_session_windows(self, session: str) -> list[str]:
        result = self._ops.run_tmux(
            "list-windows", "-t", session, "-F", "#{window_name}", check=False
        )
        if result.returncode != 0:
            return []
        return project_session_window_names(getattr(result, "stdout", "") or "")

    def wait_for_agent_terminal_pane(
        self, pane_id: str, agent: str, timeout: float
    ) -> None:
        deadline = self._ops.monotonic() + timeout
        while self._ops.monotonic() < deadline:
            info = self._ops.pane_info(pane_id)
            command = pane_command_basename(info)
            if self._ops.is_agent_process(command):
                return
            self._ops.sleep(0.2)
        self._ops.die(f"timed out waiting for {agent} pane startup: {pane_id}")

    def wait_for_text(
        self, target: str, text: str, lines: int, timeout: float
    ) -> bool:
        deadline = self._ops.monotonic() + timeout
        while self._ops.monotonic() < deadline:
            if marker_visible_in(self._ops.capture_pane(target, lines), text):
                return True
            self._ops.sleep(0.2)
        return False

    def rollback_unsubmitted_input(self, target: str) -> None:
        self._ops.run_keys(target, ["C-u"])

    def ensure_repo_session_windows(self, args: argparse.Namespace) -> list[str]:
        """Ensure ``args.session`` exists with one window per agent (claude, codex).

        Each agent runs in its own tmux window in a single repo-scoped session.
        The window-model guarantee is gated on tmux window names; missing agent
        windows are created. Pre-existing non-agent windows (zsh, custom names)
        are left untouched and stay reachable through their indices — they just
        are not agent targets. Returns the list of newly created
        ``agent:pane_id`` entries.
        """

        self._ops.require_tmux()
        config_loaded = False
        if args.config and self._ops.session_exists(args.session):
            self._ops.load_tmux_conf_for(args)
            config_loaded = True
        created: list[str] = []
        if not self._ops.session_exists(args.session):
            claude_pane = self._ops.new_agent_session_window(
                "claude", args.session, args.cwd
            )
            created.append(f"claude:{claude_pane}")
        if args.config and not config_loaded:
            self._ops.load_tmux_conf_for(args)
        windows = self._ops.list_session_windows(args.session)
        for agent in ("claude", "codex"):
            if agent in windows:
                continue
            pane_id = self._ops.new_agent_window(agent, args.session, args.cwd)
            created.append(f"{agent}:{pane_id}")
        for agent in ("claude", "codex"):
            pane = self._ops.find_agent_window(agent, args.session)
            if pane:
                self._ops.ensure_agent_target(pane, agent, args.force)
                if args.ready_timeout:
                    self._ops.wait_for_agent_terminal_pane(
                        pane["id"], agent, args.ready_timeout
                    )
                # Apply the subtle per-window status-bar tint after the window
                # exists, the user's `.tmux.conf` has been sourced (above), and
                # the agent pane is settled. Window-scoped — only the agent
                # windows we manage are tinted; legacy windows in the same
                # session stay at the user's global style.
                self._ops.apply_window_subtle_style(args.session, agent)
        return created
