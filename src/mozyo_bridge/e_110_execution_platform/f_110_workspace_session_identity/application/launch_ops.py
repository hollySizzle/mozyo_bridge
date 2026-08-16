"""Live :class:`~mozyo_bridge.application.launch_command.LaunchOps` adapter.

Split out of ``launch_command`` (Redmine #15526) when that module sat exactly at the
module-health threshold and a diagnostics fix could not be added without crossing it.
The adapter is the natural seam: it is the only part of the launch boundary that is
pure I/O wiring, and it depends on nothing else in ``launch_command`` — the port
protocol and the use cases stay together in the readable composition file, which
re-exports this class so every importer and monkeypatch seam is unchanged.

Placement (review j#105978 finding_1): session launch is the Workspace・Session識別
feature's concern, so the adapter lives in
``f_110_workspace_session_identity/application`` rather than as a new flat
``application/`` module, which the source-layout migration contract does not admit.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, NoReturn


class LiveLaunchOps:
    """Live :class:`LaunchOps` over the real ``commands`` helpers.

    Every method resolves its helper *through the* :mod:`commands` *module at
    call time* rather than binding it at import time, so the ``cmd_mozyo`` /
    ``cmd_layout_apply`` characterization tests that patch
    ``mozyo_bridge.application.commands.<fn>`` keep intercepting the live side
    effects. ``attach`` calls :func:`os.execvp` — the tests patch
    ``commands.os.execvp``, which is the same ``os`` module object, so this call
    is intercepted too.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def require_tmux(self) -> None:
        self._commands().require_tmux()

    def repo_root(self, args: argparse.Namespace) -> Path:
        return self._commands().repo_root_from_args(args)

    def adoption_marker(self, repo_root: Path) -> str | None:
        from mozyo_bridge.shared.paths import workspace_adoption_marker

        return workspace_adoption_marker(repo_root)

    def nested_adoption_marker(self, repo_root: Path) -> tuple[Path, str] | None:
        # Searched from the CWD, which is the directory the operator actually ran
        # `mozyo` in — the resolution this refusal has to explain (Redmine #15526).
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_adoption import (  # noqa: E501
            nested_adoption_marker,
        )

        return nested_adoption_marker(Path.cwd(), repo_root)

    def canonical_session_name(self, repo_root: Path) -> str:
        return self._commands().resolve_canonical_session(repo_root).name

    def session_exists(self, session: str) -> bool:
        return self._commands().session_exists(session)

    def session_cwd_mismatch(self, session: str, repo_root: Path) -> list[str]:
        return self._commands().session_cwd_mismatch(session, repo_root)

    def legacy_notice(self, repo_root: Path, session: str) -> str | None:
        return self._commands().legacy_basename_session_notice(repo_root, session)

    def default_tmux_conf(self, repo_root: Path) -> Any:
        return self._commands().default_tmux_conf(repo_root)

    def ensure_windows(self, setup_args: argparse.Namespace) -> list[str]:
        return self._commands().ensure_repo_session_windows(setup_args)

    def run_tmux(self, *args: Any, **kwargs: Any) -> Any:
        return self._commands().run_tmux(*args, **kwargs)

    def attach(self, argv: list[str]) -> NoReturn:
        os.execvp("tmux", argv)
        raise AssertionError("unreachable")  # pragma: no cover - execvp replaces process

    def emit(self, text: str, end: str = "\n") -> None:
        print(text, end=end)

    def die(self, message: str) -> NoReturn:
        self._commands().die(message)
        raise AssertionError("unreachable")  # pragma: no cover - die raises SystemExit

    def resolve_cockpit_workspaces(self, args: argparse.Namespace) -> list:
        return self._commands()._resolve_cockpit_workspaces(args)

    def agent_launch_command(
        self, role: str, session: str, repo_root: str, *, permission_mode_default: Any
    ) -> str:
        return self._commands()._agent_launch_command(
            role, session, repo_root, permission_mode_default=permission_mode_default
        )

    def execute_cockpit_plan(self, plan, *, cleanup_captured: bool = False) -> Any:
        commands = self._commands()
        return commands.execute_cockpit_plan(
            plan, commands.run_tmux, cleanup_captured=cleanup_captured
        )
