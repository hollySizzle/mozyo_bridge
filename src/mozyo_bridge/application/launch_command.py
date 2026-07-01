"""Session-launch command boundary: bare ``mozyo`` and ``layout apply cockpit`` (#12933).

The two attach-launching command entries historically lived as procedural bodies
in :mod:`mozyo_bridge.application.commands`:

- ``cmd_mozyo`` — the bare ``mozyo`` entry that ensures a repo-scoped session with
  one window per agent, then attaches (or emits the plan under ``--json`` /
  ``--no-attach``).
- ``cmd_layout_apply`` — ``mozyo layout apply cockpit`` that builds / focuses the
  shared cockpit layout, then attaches.

Both mix a *pure* decision + rendering surface (session-name resolution guards,
the attach-command form, the JSON payload, the dry-run text) with the *side
effects* they drive (tmux queries / mutations and the terminal ``os.execvp``
attach). This module carves that into an OOP-first boundary under #12638:

- The module-level ``attach_*`` / ``build_*`` / ``render_*`` helpers are the pure
  policy: they own the exact attach-command wording, the JSON payload shape, and
  the dry-run text byte-for-byte.
- :class:`LaunchOps` is the port for everything the use cases need from their
  environment, and :class:`LiveLaunchOps` the live adapter. The adapter resolves
  every helper *through the* :mod:`commands` *module at call time*, so the
  existing characterization tests that patch
  ``mozyo_bridge.application.commands.<fn>`` (``require_tmux`` /
  ``session_exists`` / ``ensure_repo_session_windows`` / ``run_tmux`` /
  ``_resolve_cockpit_workspaces`` / ``_agent_launch_command`` /
  ``execute_cockpit_plan`` and ``commands.os.execvp``) keep intercepting the
  real side effects unchanged.
- :class:`MozyoLaunchUseCase` / :class:`CockpitLayoutUseCase` compose the port
  and the policy and return a typed :class:`MozyoLaunchOutcome` /
  :class:`LayoutLaunchOutcome`. The thin ``cmd_mozyo`` / ``cmd_layout_apply``
  handlers render that outcome (print, ``die``, attach) — they own stdout and the
  terminal attach so the use cases stay exercisable with a synthetic fake.

Behavior-preserving: the refusal wording, the stdout/stderr text, the tmux side
effects, and the exit codes are unchanged from the original command bodies.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
    AGENT_LABELS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
)


# --- Pure policy: attach form, window parsing, payload / text rendering. -------


def attach_command_line(session: str, control_mode: bool) -> str:
    """The ``tmux [-CC] attach -t <session>`` hint string.

    ``--cc`` (iTerm2 control mode, Redmine #11729) swaps the plain
    ``tmux attach`` for ``tmux -CC attach``; it only changes the *attach* form.
    """

    return (
        f"tmux -CC attach -t {session}"
        if control_mode
        else f"tmux attach -t {session}"
    )


def attach_argv(session: str, control_mode: bool) -> list[str]:
    """The ``os.execvp`` argv for attaching to ``session`` (``-CC`` under ``--cc``)."""

    if control_mode:
        return ["tmux", "-CC", "attach", "-t", session]
    return ["tmux", "attach", "-t", session]


def _parse_mozyo_window_rows(table: str) -> list[dict]:
    """Parse ``list-windows`` rows (``index<TAB>name<TAB>process``) into dicts.

    Mirrors the human ``INDEX/NAME/PROCESS`` table emitted by bare ``mozyo`` so
    the ``--json`` payload exposes the same window facts to external launchers.
    ``index`` is an int when numeric (tmux window indices always are); a
    missing/blank process becomes ``None`` rather than an empty string.
    """
    windows: list[dict] = []
    for line in table.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        index = parts[0] if parts else ""
        name = parts[1] if len(parts) > 1 else ""
        process = parts[2] if len(parts) > 2 else ""
        windows.append(
            {
                "index": int(index) if index.isdigit() else index,
                "name": name,
                "process": process or None,
            }
        )
    return windows


def build_mozyo_json_payload(
    *,
    session: str,
    repo_root: str,
    cwd: str,
    created: list[str],
    windows: list[dict],
    attach_command: str,
    control_mode: bool,
    raw_no_attach: bool,
    notice: str | None,
) -> dict[str, Any]:
    """The ``mozyo --json`` payload (Redmine #11313).

    ``--json`` always returns without attaching, so the reported ``no_attach`` is
    the *effective* value (raw flag OR json) — which is always ``True`` on this
    JSON path — matching what actually happened rather than the raw flag
    (review #54111). ``ready`` is whether both agent windows are present.
    """

    present = {window["name"] for window in windows}
    # This builder is only reached under ``--json``, where json_output is True, so
    # ``raw_no_attach or json_output`` collapses to True.
    no_attach_effective = bool(raw_no_attach) or True
    return {
        "session": session,
        "repo_root": repo_root,
        "cwd": cwd,
        "created": list(created),
        "windows": windows,
        "ready": AGENT_LABELS.issubset(present),
        "attach": attach_command,
        "attach_target": session,
        "attached": False,
        "control_mode": control_mode,
        "no_attach": no_attach_effective,
        "legacy_session_notice": notice,
    }


def render_cockpit_layout_dry_run(plan, session: str, attach_command: str) -> str:
    """The ``layout apply cockpit --dry-run`` text block (planned tmux commands)."""

    lines = [
        f"cockpit plan: session={session} columns={plan.columns} "
        f"codex={plan.codex_ratio}% claude={plan.claude_ratio}%"
    ]
    for cmd in plan.commands:
        rendered = " ".join(shlex.quote(token) for token in cmd.argv)
        lines.append(f"  tmux {rendered}")
    lines.append(f"attach: {attach_command}")
    return "\n".join(lines)


def build_cockpit_layout_json_payload(plan, attach_command: str, control_mode: bool) -> dict[str, Any]:
    """The ``layout apply cockpit --json`` payload (plan + attach form)."""

    payload = plan.as_dict()
    payload["attach"] = attach_command
    payload["control_mode"] = control_mode
    return payload


# --- Port + live adapter (routes through ``commands`` at call time). ----------


@runtime_checkable
class LaunchOps(Protocol):
    """Port: everything the launch use cases need from their environment.

    The live adapter routes each call through the :mod:`commands` module so the
    monkeypatched characterization tests still intercept, and so this module
    never imports :mod:`commands` at module scope (no import cycle).
    """

    def require_tmux(self) -> None: ...

    def repo_root(self, args: argparse.Namespace) -> Path: ...

    def canonical_session_name(self, repo_root: Path) -> str: ...

    def session_exists(self, session: str) -> bool: ...

    def session_cwd_mismatch(self, session: str, repo_root: Path) -> list[str]: ...

    def legacy_notice(self, repo_root: Path, session: str) -> str | None: ...

    def default_tmux_conf(self, repo_root: Path) -> Any: ...

    def ensure_windows(self, setup_args: argparse.Namespace) -> list[str]: ...

    def run_tmux(self, *args: Any, **kwargs: Any) -> Any: ...

    def attach(self, argv: list[str]) -> NoReturn: ...

    def resolve_cockpit_workspaces(self, args: argparse.Namespace) -> list: ...

    def agent_launch_command(
        self, role: str, session: str, repo_root: str, *, permission_mode_default: Any
    ) -> str: ...

    def execute_cockpit_plan(self, plan, *, cleanup_captured: bool = False) -> Any: ...


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


# --- Outcomes ----------------------------------------------------------------


@dataclass(frozen=True)
class MozyoLaunchOutcome:
    """Result of :class:`MozyoLaunchUseCase` — a refusal, JSON, or an attach plan.

    ``error_message`` is the bare ``die`` message (the handler exits non-zero).
    ``notice`` is the non-JSON legacy-session notice printed *before* the session
    line (or before a late ``die``); it is ``None`` in JSON mode, where the notice
    rides ``json_stdout`` instead. ``json_stdout`` is the single ``--json`` block.
    On the text success path the handler prints the session line + window table,
    then attaches unless ``no_attach``. ``windows_table`` is ``None`` when the
    ``list-windows`` probe failed (nothing is printed for the table), matching the
    legacy ``if result.returncode == 0`` guard.
    """

    error_message: str | None = None
    notice: str | None = None
    json_stdout: str | None = None
    session: str | None = None
    created: tuple[str, ...] = ()
    windows_table: str | None = None
    attach_command: str | None = None
    attach_argv: tuple[str, ...] = ()
    no_attach: bool = False


@dataclass(frozen=True)
class LayoutLaunchOutcome:
    """Result of :class:`CockpitLayoutUseCase` — a refusal, JSON, dry-run, or attach.

    ``error_message`` is the bare ``die`` message. ``json_stdout`` /
    ``dry_run_stdout`` are the single non-mutating output blocks. On the execute
    path ``pre_attach_lines`` are printed (the reuse or the built message) before
    the handler attaches unless ``no_attach``.
    """

    error_message: str | None = None
    json_stdout: str | None = None
    dry_run_stdout: str | None = None
    pre_attach_lines: tuple[str, ...] = ()
    attach_command: str | None = None
    attach_argv: tuple[str, ...] = ()
    no_attach: bool = False


# --- Use cases ---------------------------------------------------------------


@dataclass
class MozyoLaunchUseCase:
    """Bare ``mozyo`` launch over the :class:`LaunchOps` port.

    Mirrors the legacy ``cmd_mozyo`` body exactly: resolve the repo root and
    session name (failing closed on an underivable name or a cwd-mismatched
    existing session), compute the legacy notice, ensure the repo session
    windows, then decide JSON vs text vs attach. The ``os.execvp`` attach and the
    stdout stay in the thin handler that renders the returned outcome.
    """

    ops: LaunchOps

    def run(self, args: argparse.Namespace) -> MozyoLaunchOutcome:
        ops = self.ops
        ops.require_tmux()
        repo_root = ops.repo_root(args)
        derived = ops.canonical_session_name(repo_root)
        if not derived:
            return MozyoLaunchOutcome(
                error_message=(
                    "could not derive a session name from repo root; cd into a "
                    "project directory or pass a subcommand explicitly"
                )
            )
        user_session = getattr(args, "session", None)
        session = user_session or derived
        cwd = getattr(args, "cwd", None) or str(repo_root)
        if not user_session and ops.session_exists(session):
            offending = ops.session_cwd_mismatch(session, repo_root)
            if offending:
                return MozyoLaunchOutcome(
                    error_message=(
                        f"session '{session}' already exists but its panes are "
                        f"outside repo root {repo_root} (cwds: "
                        f"{', '.join(offending)}). "
                        "Re-run from the matching repo root, or pass an explicit "
                        "`--session NAME` to bare `mozyo` to disambiguate."
                    )
                )
        json_output = bool(getattr(args, "json_output", False))
        notice = None
        if not user_session:
            notice = ops.legacy_notice(repo_root, session)
        config_path = getattr(args, "config_path", None)
        config_path_was_default = config_path is None
        resolved_config_path = config_path or str(ops.default_tmux_conf(repo_root))
        setup_args = argparse.Namespace(
            session=session,
            cwd=cwd,
            config=True,
            config_path=resolved_config_path,
            config_path_was_default=config_path_was_default,
            ready_timeout=float(getattr(args, "ready_timeout", 10.0) or 0.0),
            force=bool(getattr(args, "force", False)),
        )
        created = ops.ensure_windows(setup_args)
        # The non-JSON legacy notice is printed before the session line — and
        # before a late select-window failure — so carry it on the outcome for
        # the text path; JSON mode folds it into the payload instead.
        text_notice = notice if (notice and not json_output) else None

        select = ops.run_tmux("select-window", "-t", f"{session}:claude", check=False)
        if select.returncode != 0:
            return MozyoLaunchOutcome(
                notice=text_notice,
                error_message=(
                    f"failed to select `claude` window in session '{session}'. "
                    "The window-model guarantee did not hold. "
                    f"stderr={select.stderr.strip() or select.stdout.strip()}"
                ),
            )
        result = ops.run_tmux(
            "list-windows",
            "-t",
            session,
            "-F",
            "#{window_index}\t#{window_name}\t#{pane_current_command}",
            check=False,
        )
        control_mode = bool(getattr(args, "cc", False))
        attach_command = attach_command_line(session, control_mode)
        if json_output:
            windows = _parse_mozyo_window_rows(
                result.stdout if result.returncode == 0 else ""
            )
            payload = build_mozyo_json_payload(
                session=session,
                repo_root=str(repo_root),
                cwd=cwd,
                created=list(created),
                windows=windows,
                attach_command=attach_command,
                control_mode=control_mode,
                raw_no_attach=bool(getattr(args, "no_attach", False)),
                notice=notice,
            )
            return MozyoLaunchOutcome(
                json_stdout=json.dumps(
                    payload, ensure_ascii=False, indent=2, sort_keys=True
                )
            )
        return MozyoLaunchOutcome(
            notice=text_notice,
            session=session,
            created=tuple(created),
            windows_table=result.stdout if result.returncode == 0 else None,
            attach_command=attach_command,
            attach_argv=tuple(attach_argv(session, control_mode)),
            no_attach=bool(getattr(args, "no_attach", False)),
        )


@dataclass
class CockpitLayoutUseCase:
    """``mozyo layout apply cockpit`` over the :class:`LaunchOps` port.

    Mirrors the legacy ``cmd_layout_apply`` body: validate the preset, resolve
    the workspaces (failing closed on none), build the cockpit plan, then decide
    JSON vs dry-run vs execute+attach. On the execute path it reuses an existing
    cockpit session or builds a fresh one (tearing down a partial build on a
    mid-step failure), leaving the terminal attach + stdout to the handler.
    """

    ops: LaunchOps

    def run(self, args: argparse.Namespace) -> LayoutLaunchOutcome:
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            COCKPIT_SESSION_DEFAULT,
            build_cockpit_plan,
        )

        ops = self.ops
        preset = getattr(args, "preset", "cockpit")
        if preset != "cockpit":
            return LayoutLaunchOutcome(
                error_message=f"unsupported layout preset: {preset!r}"
            )
        session = getattr(args, "cockpit_session", None) or COCKPIT_SESSION_DEFAULT
        codex_ratio = int(getattr(args, "codex_ratio", 70) or 70)

        workspaces = ops.resolve_cockpit_workspaces(args)
        if not workspaces:
            return LayoutLaunchOutcome(
                error_message=(
                    "no active workspace to summon into the cockpit. Pass explicit "
                    "`--repo <root>` columns, or start at least one mozyo session "
                    "(`mozyo`) so the inventory has a codex/claude pane to discover."
                )
            )

        def launch(role: str, ws) -> str:
            # Cockpit managed Claude panes launch auto reproducibly (#11925);
            # env var still overrides. Codex is unaffected (Claude-only flag).
            return ops.agent_launch_command(
                role,
                session,
                ws.repo_root,
                permission_mode_default=COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
            )

        plan = build_cockpit_plan(
            workspaces, codex_ratio=codex_ratio, session=session, launch=launch
        )

        json_output = bool(getattr(args, "json_output", False))
        dry_run = bool(getattr(args, "dry_run", False))
        control_mode = bool(getattr(args, "cc", False))
        no_attach = bool(getattr(args, "no_attach", False))
        attach_command = attach_command_line(session, control_mode)

        if json_output:
            payload = build_cockpit_layout_json_payload(
                plan, attach_command, control_mode
            )
            return LayoutLaunchOutcome(
                json_stdout=json.dumps(
                    payload, ensure_ascii=False, indent=2, sort_keys=True
                )
            )

        if dry_run:
            return LayoutLaunchOutcome(
                dry_run_stdout=render_cockpit_layout_dry_run(
                    plan, session, attach_command
                )
            )

        ops.require_tmux()
        # Reuse over duplication (Redmine #11788): when the cockpit session
        # already exists, focus/attach it instead of rebuilding a second copy of
        # the panes.
        if ops.session_exists(session):
            pre_attach = (
                f"cockpit session {session!r} already exists; attaching without "
                "rebuild (reuse over duplicate panes)",
            )
        else:
            try:
                ops.execute_cockpit_plan(plan)
            except SystemExit:
                # A layout step failed mid-build (Redmine #11788 review). Tear
                # down the partial cockpit session best-effort so a retry rebuilds
                # cleanly instead of the reuse path adopting a broken half layout.
                ops.run_tmux("kill-session", "-t", session, check=False)
                raise
            pre_attach = (
                f"cockpit built: session={session} columns={plan.columns} "
                f"codex={plan.codex_ratio}% claude={plan.claude_ratio}%",
            )
        return LayoutLaunchOutcome(
            pre_attach_lines=pre_attach,
            attach_command=attach_command,
            attach_argv=tuple(attach_argv(session, control_mode)),
            no_attach=no_attach,
        )
