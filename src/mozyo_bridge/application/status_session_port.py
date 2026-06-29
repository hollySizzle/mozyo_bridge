"""Port boundary over the tmux session-status reads (Redmine #12785 / #12638 / #12749).

Fourth OOP-first port for the ``commands.py`` decomposition (after
``tmux_control_port``, ``tmux_option_port``, and ``agent_discovery_port``). The
``status`` command describes a normal agent session: it asks whether the session
exists, enumerates its windows, and — only when agent windows are present —
captures the session's panes. The procedural ``cmd_status`` issued those three
reads as module-level calls (``session_exists`` / ``list_session_windows`` /
``run_tmux("list-panes", ...)``) and its tests patched
``mozyo_bridge.application.commands.session_exists`` /
``commands.list_session_windows`` / ``commands.run_tmux`` to drive them — a
function-monkeypatch seam that mixed the read boundary with the present/missing
agent-window logic and the stdout rendering.

This module defines :class:`StatusSessionPort` — the three read operations the
:class:`~mozyo_bridge.application.commands_status.ResolveSessionStatusUseCase`
depends on — with a live adapter (:class:`LiveStatusSession`). The use case
takes the port by injection so its specification test drives a fake port (no
real tmux, no function patch).

Compatibility bridge (transitional): the live adapter reaches the three reads
through the ``commands`` module *at call time* (``commands.session_exists`` /
``commands.list_session_windows`` / ``commands.run_tmux``) so the existing
``status`` tests that patch those ``commands.*`` names keep working unchanged
while the use case gains its port seam. Relocating those leaf reads out of
``commands`` is residual carried to #12638. This is a read-only status boundary;
it issues no send-keys / paste-buffer routing
(``tmux-send-safety-contract``) — the port exposes no key-send operation.
"""

from __future__ import annotations

from typing import List, Protocol, Tuple, runtime_checkable

# The ``list-panes -F`` format ``cmd_status`` historically used: window index,
# window name, pane id, active flag, foreground command, and cwd, tab-separated.
PANES_FORMAT = (
    "#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_active}\t"
    "#{pane_current_command}\t#{pane_current_path}"
)


@runtime_checkable
class StatusSessionPort(Protocol):
    """The session-status reads the status use case depends on (read-only)."""

    def session_exists(self, session: str) -> bool:
        """Whether the named tmux session exists."""
        ...

    def list_windows(self, session: str) -> List[str]:
        """The session's window names, in order (empty on any read failure)."""
        ...

    def capture_panes(self, session: str) -> Tuple[bool, str]:
        """Capture the session's panes; return ``(ok, text)``.

        ``ok`` is ``False`` (and ``text`` empty) when the underlying tmux read
        fails, so the caller renders the header without pane rows rather than
        raising.
        """
        ...


class LiveStatusSession:
    """Live adapter for the status session reads.

    Routes through the ``commands`` module at call time (see the module
    docstring's compatibility-bridge note) so the residual ``commands``-owned
    session enumeration — and the tests that patch it — stay intact during the
    migration.
    """

    def session_exists(self, session: str) -> bool:
        from mozyo_bridge.application import commands as _commands

        return _commands.session_exists(session)

    def list_windows(self, session: str) -> List[str]:
        from mozyo_bridge.application import commands as _commands

        return _commands.list_session_windows(session)

    def capture_panes(self, session: str) -> Tuple[bool, str]:
        from mozyo_bridge.application import commands as _commands

        result = _commands.run_tmux(
            "list-panes", "-s", "-t", session, "-F", PANES_FORMAT, check=False
        )
        return (result.returncode == 0, result.stdout)
