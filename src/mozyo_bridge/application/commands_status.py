"""status command family — OOP-first session-read boundary (Redmine #12785 / #12638 / #12749).

First conversion tranche under #12785 (continuing the #12749 series). The
``status`` command's three external session reads — session existence, window
enumeration, and the agent-pane capture — are pulled behind
:class:`~mozyo_bridge.application.status_session_port.StatusSessionPort`, and the
present/missing agent-window logic that the procedural ``cmd_status`` inlined
between those reads moves into :class:`ResolveSessionStatusUseCase`, which
returns a frozen :class:`SessionStatusView` value object. The thin command
handler in ``commands.py`` builds a :class:`StatusQuery`, drives the use case
with a :class:`~mozyo_bridge.application.status_session_port.LiveStatusSession`,
and renders the view; it no longer threads ``argparse.Namespace`` into the read
logic.

Scope (deliberately bounded per #12749 j#68175 / #12785 j#68236): only the
session-read boundary of ``status``. The cockpit-membership projection
(``_status_repo_cockpit_membership``) and the ``return cmd_doctor(args)`` tail
stay in ``commands.py`` and remain residual to #12638 — moving the broad
cockpit / doctor modules is not in this tranche.
"""

from __future__ import annotations

from dataclasses import dataclass

from mozyo_bridge.application.status_session_port import StatusSessionPort
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    AGENT_LABELS,
)


@dataclass(frozen=True)
class StatusQuery:
    """Typed input for the status session read: the session to describe."""

    session: str


@dataclass(frozen=True)
class SessionStatusView:
    """Resolved session-status facts the status handler renders.

    ``present`` is ``False`` for a missing session (the only meaningful field
    then). When present, ``agent_windows`` lists the session's agent-named
    windows in window order; ``has_agent_windows`` gates the pane table;
    ``panes_ok`` / ``panes_text`` carry the capture result; and
    ``missing_agents`` is the sorted set of agent labels with no window.
    """

    session: str
    present: bool
    agent_windows: tuple = ()
    has_agent_windows: bool = False
    panes_ok: bool = False
    panes_text: str = ""
    missing_agents: tuple = ()


class ResolveSessionStatusUseCase:
    """Resolve a session's present/missing agent-window status over a port.

    Owns the read-orchestration + classification ``cmd_status`` previously
    inlined: existence → window enumeration → (only when agent windows exist)
    pane capture, plus the missing-agent computation. Decoupled from live tmux
    via the injected :class:`StatusSessionPort`, so it is unit-testable with a
    fake port (no ``commands.*`` monkeypatch). Behavior-preserving: panes are
    captured only when agent windows are present, exactly as the procedural
    handler did (so a session whose agent windows are all missing issues no
    ``list-panes`` read).
    """

    def __init__(self, sessions: StatusSessionPort) -> None:
        self._sessions = sessions

    def resolve(self, query: StatusQuery) -> SessionStatusView:
        session = query.session
        if not self._sessions.session_exists(session):
            return SessionStatusView(session=session, present=False)

        windows = self._sessions.list_windows(session)
        agent_windows = tuple(name for name in windows if name in AGENT_LABELS)
        if not agent_windows:
            return SessionStatusView(
                session=session,
                present=True,
                agent_windows=(),
                has_agent_windows=False,
            )

        panes_ok, panes_text = self._sessions.capture_panes(session)
        missing = tuple(
            sorted(agent for agent in AGENT_LABELS if agent not in agent_windows)
        )
        return SessionStatusView(
            session=session,
            present=True,
            agent_windows=agent_windows,
            has_agent_windows=True,
            panes_ok=panes_ok,
            panes_text=panes_text,
            missing_agents=missing,
        )
