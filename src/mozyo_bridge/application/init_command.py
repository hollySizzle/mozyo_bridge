"""Init command handler boundary (#12926).

The ``mozyo-bridge init`` handler historically lived as one procedural body in
``application/commands.py``: it mixed the *fail-closed adoption policy* (which
refusals fire, in what order, with which messages, and what the smart-adoption
plan is) with every *side effect* it drives (tmux pane resolution, the workspace
registry write, the VS Code settings merge, the session / window renames, the
status-bar style, and the pane-option markers). This module carves that body
into an OOP-first boundary under #12638:

- :class:`InitRequest` is the request value object built from the parsed
  argparse namespace; the CLI handler ``cmd_init`` stays thin (argparse ->
  request -> use case -> stdout / exit code).
- :class:`InitWorkspaceOps` is the port for everything the use case needs from
  its environment, and :class:`LiveInitWorkspaceOps` the live adapter. The
  adapter resolves every helper *through the* :mod:`commands` *module at call
  time*, so the existing characterization tests that patch
  ``mozyo_bridge.application.commands.<fn>`` (``run_tmux`` / ``pane_lines`` /
  ``rename_session`` / ...) keep intercepting the real side effects unchanged.
- The module-level ``*_message`` helpers are the pure fail-closed policy: each
  returns the exact ``die`` message string the legacy body produced, so stderr
  wording is byte-for-byte preserved.
- :class:`InitUseCase` composes the port and the policy. It walks the same read
  -> check -> mutate sequence the legacy body did (all abortable preflight
  checks run before any mutation, so a guard stop never half-adopts a pane) and
  returns an :class:`InitOutcome` describing either a refusal message or the
  completed adoption's stdout (success line + notes + warnings). ``cmd_init``
  renders that outcome.

Behavior-preserving: this is a pure restructuring. The refusal ordering, the
message text, the registry / vscode / tmux side effects, and the printed output
are all unchanged from the original ``cmd_init``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class InitRequest:
    """Parsed intent of one ``mozyo-bridge init`` invocation.

    ``target_arg`` preserves the *explicit* ``--target`` value (``None`` when the
    current pane is used) so escape-hatch ``--window-only`` suggestions can echo
    it back instead of silently dropping it (Redmine #11367 review #54498).
    """

    agent: str
    target_arg: str | None
    window_only: bool
    no_vscode_settings: bool

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "InitRequest":
        return cls(
            agent=args.agent,
            target_arg=args.target,
            window_only=bool(getattr(args, "window_only", False)),
            no_vscode_settings=bool(getattr(args, "no_vscode_settings", False)),
        )


@dataclass(frozen=True)
class InitOutcome:
    """Result of running :class:`InitUseCase` — a refusal or a completed adoption.

    A refusal carries the bare ``die`` message (the handler prepends ``error:``
    and exits non-zero). A completion carries the stdout the handler prints: the
    headline ``success_line``, the per-step ``notes`` (each rendered ``  - ...``),
    and any non-fatal ``warnings`` (rendered to stderr).
    """

    refused_message: str | None = None
    success_line: str | None = None
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def refused(cls, message: str) -> "InitOutcome":
        return cls(refused_message=message)

    @classmethod
    def completed(
        cls,
        success_line: str,
        notes: list[str] | tuple[str, ...] = (),
        warnings: list[str] | tuple[str, ...] = (),
    ) -> "InitOutcome":
        return cls(
            success_line=success_line,
            notes=tuple(notes),
            warnings=tuple(warnings),
        )

    @property
    def is_refused(self) -> bool:
        return self.refused_message is not None


@runtime_checkable
class InitWorkspaceOps(Protocol):
    """Port: everything the init use case needs from its environment.

    Implementations own every tmux query / mutation, the workspace-registry
    read and write, the VS Code settings merge, and the pure pane-conflict /
    fallback / root helpers. The use case depends only on this protocol, so it is
    exercisable with a synthetic fake. The live adapter routes each call through
    the :mod:`commands` module so monkeypatched test doubles still intercept.
    """

    def require_tmux(self) -> None: ...

    def current_pane(self) -> str: ...

    def is_tmux_target(self, raw_target: str) -> bool: ...

    def resolve_pane_id(self, raw_target: str) -> str | None: ...

    def pane_location(self, target: str) -> str: ...

    def pane_lines(self) -> list[dict]: ...

    def agent_window_conflict(
        self, panes: list[dict], session: str, skip_window_index: str, agent: str
    ) -> list[str]: ...

    def confident_root(self, cwd: str) -> Path | None: ...

    def canonical_session(self, root: Path) -> Any: ...

    def is_fallback_session(self, name: str) -> bool: ...

    def session_exists(self, name: str) -> bool: ...

    def register_workspace(self, root: Path) -> Any: ...

    def write_vscode_session_name(
        self, root: Path, session_name: str
    ) -> tuple[Path, bool, str | None]: ...

    def rename_session(self, old: str, new: str) -> None: ...

    def rename_window(self, window_target: str, name: str) -> None: ...

    def apply_window_subtle_style(self, session: str, agent: str) -> None: ...

    def bind_agent_pane_markers(
        self, target: str, agent: str, workspace_id: str | None, notes: list[str]
    ) -> None: ...


class LiveInitWorkspaceOps:
    """Live :class:`InitWorkspaceOps` over the real ``commands`` helpers.

    Every method resolves its helper *through the* :mod:`commands` *module at
    call time* rather than binding it at import time. That is deliberate: the
    ``cmd_init`` characterization tests patch ``mozyo_bridge.application.commands
    .<fn>`` (``run_tmux`` / ``pane_location`` / ``pane_lines`` / ``session_exists``
    / ``rename_session`` / ``rename_window`` / ``apply_window_subtle_style`` /
    ...) and expect those doubles to intercept the side effects. Late resolution
    keeps that contract and avoids an import cycle (``commands`` imports this
    module only lazily inside ``cmd_init``).
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def require_tmux(self) -> None:
        self._commands().require_tmux()

    def current_pane(self) -> str:
        return self._commands().current_pane()

    def is_tmux_target(self, raw_target: str) -> bool:
        return self._commands().is_tmux_target(raw_target)

    def resolve_pane_id(self, raw_target: str) -> str | None:
        commands = self._commands()
        resolved = commands.run_tmux(
            "display-message", "-t", raw_target, "-p", "#{pane_id}", check=False
        )
        if resolved.returncode != 0 or not resolved.stdout.strip():
            return None
        return resolved.stdout.strip()

    def pane_location(self, target: str) -> str:
        return self._commands().pane_location(target)

    def pane_lines(self) -> list[dict]:
        return self._commands().pane_lines()

    def agent_window_conflict(
        self, panes: list[dict], session: str, skip_window_index: str, agent: str
    ) -> list[str]:
        # Pure window-conflict decision; owned by this boundary (#12979).
        return _agent_window_conflict(panes, session, skip_window_index, agent)

    def confident_root(self, cwd: str) -> Path | None:
        # Workspace-root discovery; owned by this boundary (#12979).
        return _confident_workspace_root(cwd)

    def canonical_session(self, root: Path) -> Any:
        return self._commands().resolve_canonical_session(root)

    def is_fallback_session(self, name: str) -> bool:
        # Pure fallback-name decision; owned by this boundary (#12979).
        return _is_fallback_session_name(name)

    def session_exists(self, name: str) -> bool:
        return self._commands().session_exists(name)

    def register_workspace(self, root: Path) -> Any:
        from mozyo_bridge.workspace_registry import register_workspace

        return register_workspace(root)

    def write_vscode_session_name(
        self, root: Path, session_name: str
    ) -> tuple[Path, bool, str | None]:
        # Workspace-local settings write; owned by this boundary (#12979).
        return _write_vscode_session_name(root, session_name)

    def rename_session(self, old: str, new: str) -> None:
        self._commands().rename_session(old, new)

    def rename_window(self, window_target: str, name: str) -> None:
        self._commands().rename_window(window_target, name)

    def apply_window_subtle_style(self, session: str, agent: str) -> None:
        self._commands().apply_window_subtle_style(session, agent)

    def bind_agent_pane_markers(
        self, target: str, agent: str, workspace_id: str | None, notes: list[str]
    ) -> None:
        self._commands()._bind_agent_pane_markers(target, agent, workspace_id, notes)


# --- Pure fail-closed policy: exact legacy `die` messages. --------------------


def _target_suffix(target_arg: str | None) -> str:
    """Render an explicit target for a suggested re-run command.

    Mirrors the legacy ``_init_target_suffix``: an explicit ``--target`` is
    preserved so escape-hatch suggestions do not silently rename the current
    pane instead (Redmine #11367 review #54498).
    """

    return f" {target_arg}" if target_arg else ""


def agent_window_conflict_message(
    conflicts: list[str], session: str, agent: str, target: str
) -> str | None:
    """Refusal when another window in ``session`` already carries ``agent``.

    Returns ``None`` when there is no conflict. The resolver keys agents on the
    window name, so a second window named ``agent`` in the same session is
    ambiguous even though tmux tolerates it.
    """

    if not conflicts:
        return None
    existing = ", ".join(sorted(set(conflicts)))
    return (
        f"session '{session}' already has a window named '{agent}' at "
        f"{existing}. Rename or kill that window before running "
        f"`mozyo-bridge init {agent}` on {target}; tmux tolerates duplicate "
        "window names but the resolver does not."
    )


def unconfident_root_message(
    target: str, target_cwd: str, agent: str, target_arg: str | None
) -> str:
    suffix = _target_suffix(target_arg)
    return (
        f"refusing to init {target}: cannot confidently determine this pane's "
        f"workspace root from its cwd ({target_cwd or 'unknown'}). Smart `init` "
        "adopts the pane into the workspace's derived session, which needs a "
        "confident root (a `.git` / `.tmux.conf` / `pyproject.toml` / "
        "`.mozyo-bridge/scaffold.json` ancestor).\n"
        "Next actions:\n"
        f"  - cd into the workspace root and re-run `mozyo-bridge init {agent}`.\n"
        f"  - Or rename only this window in place: "
        f"`mozyo-bridge init {agent}{suffix} --window-only`."
    )


def foreign_session_message(
    target: str,
    target_session: str,
    expected_name: str,
    agent: str,
    target_arg: str | None,
) -> str:
    suffix = _target_suffix(target_arg)
    return (
        f"refusing to init {target}: it is in tmux session "
        f"'{target_session}', which has a meaningful name that differs from "
        f"this workspace's expected session '{expected_name}'. Smart `init` "
        "only adopts low-information tmux-integrated fallback sessions "
        "(all-underscore names); it will not rename a named session.\n"
        f"  current session:  {target_session}\n"
        f"  expected session: {expected_name}\n"
        "Next actions:\n"
        "  - Start the workspace session: `mozyo` (attach) or "
        "`mozyo --no-attach`.\n"
        f"  - Rename only this window in the current session: "
        f"`mozyo-bridge init {agent}{suffix} --window-only`."
    )


def expected_session_exists_message(
    target: str,
    target_session: str,
    expected_name: str,
    agent: str,
    target_arg: str | None,
) -> str:
    suffix = _target_suffix(target_arg)
    return (
        f"refusing to init {target}: the expected session '{expected_name}' "
        f"already exists as a separate tmux session, so the fallback session "
        f"'{target_session}' cannot be renamed into it without colliding.\n"
        "Next actions:\n"
        f"  - Attach the existing session and run agents there: "
        f"`tmux attach -t {expected_name}`.\n"
        f"  - Or rename only this window in place: "
        f"`mozyo-bridge init {agent}{suffix} --window-only`."
    )


# --- Use case: read -> check -> mutate, preserving the legacy sequence. -------


@dataclass
class InitUseCase:
    """Orchestrate ``mozyo-bridge init`` over the :class:`InitWorkspaceOps` port.

    The walk mirrors the legacy ``cmd_init`` body exactly: resolve the target,
    branch on ``--window-only``, then run every abortable preflight check before
    any mutation, then mutate (registry -> vscode -> session rename -> window
    rename -> style -> markers) and report.
    """

    ops: InitWorkspaceOps

    def run(self, request: InitRequest) -> InitOutcome:
        ops = self.ops
        ops.require_tmux()

        raw_target = request.target_arg or ops.current_pane()
        if not ops.is_tmux_target(raw_target):
            return InitOutcome.refused(
                f"init target must be a tmux pane id or location, not a label: "
                f"{raw_target}"
            )
        target = ops.resolve_pane_id(raw_target)
        if not target:
            return InitOutcome.refused(f"invalid tmux target: {raw_target}")

        location = ops.pane_location(target)
        target_session, _, rest = location.partition(":")
        if not target_session or not rest:
            return InitOutcome.refused(
                f"could not parse tmux location for {target}: {location!r}"
            )
        target_window_index = rest.split(".", 1)[0]

        panes = ops.pane_lines()
        agent = request.agent
        conflicts = ops.agent_window_conflict(
            panes, target_session, target_window_index, agent
        )

        # --- Legacy window-only path: rename the window in place, nothing else.
        if request.window_only:
            refusal = agent_window_conflict_message(
                conflicts, target_session, agent, target
            )
            if refusal:
                return InitOutcome.refused(refusal)
            ops.rename_window(f"{target_session}:{target_window_index}", agent)
            ops.apply_window_subtle_style(target_session, agent)
            return InitOutcome.completed(
                f"initialized {target} as {agent} (renamed window "
                f"{target_session}:{target_window_index} -> {agent}; window-only)"
            )

        # --- Smart adoption path: all fail-closed checks before any mutation. --
        target_cwd = ""
        for pane in panes:
            if pane.get("id") == target:
                target_cwd = pane.get("cwd") or ""
                break
        root = ops.confident_root(target_cwd)
        if root is None:
            return InitOutcome.refused(
                unconfident_root_message(
                    target, target_cwd, agent, request.target_arg
                )
            )
        expected = ops.canonical_session(root)
        expected_name = expected.name

        refusal = agent_window_conflict_message(
            conflicts, target_session, agent, target
        )
        if refusal:
            return InitOutcome.refused(refusal)

        need_session_rename = target_session != expected_name
        if need_session_rename:
            if not ops.is_fallback_session(target_session):
                return InitOutcome.refused(
                    foreign_session_message(
                        target,
                        target_session,
                        expected_name,
                        agent,
                        request.target_arg,
                    )
                )
            if ops.session_exists(expected_name):
                return InitOutcome.refused(
                    expected_session_exists_message(
                        target,
                        target_session,
                        expected_name,
                        agent,
                        request.target_arg,
                    )
                )

        notes: list[str] = []
        warnings: list[str] = []

        # --- Registry-aware identity (Redmine #11427): make the workspace
        # identity durable after every abortable preflight check but before any
        # tmux / vscode mutation. A workspace already resolved from the home
        # registry keeps its identity untouched; one resolved via path derivation
        # or an anchor-only state is registered now. ``register_workspace`` is
        # idempotent and reuses the anchor identity, so the canonical session name
        # is byte-identical to what ``canonical_session`` returned above.
        workspace_id = expected.workspace_id
        if expected.source != _source_home_registry():
            registration = ops.register_workspace(root)
            expected_name = registration.record.canonical_session
            workspace_id = registration.record.workspace_id
            notes.append(
                f"registered workspace '{registration.record.project_name}' "
                f"({registration.outcome}; session '{expected_name}')"
            )

        # --- Mutations: vscode settings -> session rename -> window -> style. ---
        if not request.no_vscode_settings:
            settings_path, written, warning = ops.write_vscode_session_name(
                root, expected_name
            )
            if warning:
                warnings.append(warning)
            elif written:
                notes.append(
                    f'pinned tmux-integrated.sessionName="{expected_name}" '
                    f"in {settings_path}"
                )

        final_session = target_session
        if need_session_rename:
            ops.rename_session(target_session, expected_name)
            final_session = expected_name
            notes.append(
                f"renamed session '{target_session}' -> '{expected_name}'"
            )

        ops.rename_window(f"{final_session}:{target_window_index}", agent)
        # `init` is the second entry point that promotes an existing pane into the
        # agent-window rail (the first being bare `mozyo`). Apply the same subtle
        # status-bar tint so adopted panes look identical to `mozyo`-created ones.
        ops.apply_window_subtle_style(final_session, agent)

        # Stabilize role binding with the same pane-option markers cockpit panes
        # carry (Redmine #11427 / #11822): the markers make the resolver report a
        # strong `pane_option` role that survives a later window rename. The marker
        # write is best-effort and appends its own notes; a failed marker never
        # undoes the completed adoption.
        ops.bind_agent_pane_markers(target, agent, workspace_id, notes)

        return InitOutcome.completed(
            f"adopted {target} into session '{final_session}' as {agent}",
            notes,
            warnings,
        )


def _source_home_registry() -> str:
    from mozyo_bridge.workspace_registry import SOURCE_HOME_REGISTRY

    return SOURCE_HOME_REGISTRY


# --- Workspace / session config helpers (#12979). ----------------------------
# The pure fallback / conflict decisions and the workspace-root discovery /
# VS Code settings write these methods drive used to live in
# ``application/commands.py``. They are co-located with the boundary that
# consumes them (:class:`LiveInitWorkspaceOps`) as part of the #12638 OOP-first
# carve; ``commands.py`` re-exports the legacy ``_``-prefixed names so existing
# imports (``commands._confident_workspace_root`` /
# ``commands._is_fallback_session_name``) resolve byte-for-byte.


def _confident_workspace_root(cwd: str) -> Path | None:
    """Return the workspace root for ``cwd`` only when its identity is confident.

    Walks up from ``cwd`` using the same markers bare ``mozyo`` uses and returns
    the root only when that root actually bears a repo / workspace marker
    (``.git`` / ``.tmux.conf`` / ``pyproject.toml`` / ``.mozyo-bridge/scaffold.json``).
    Returns ``None`` when ``cwd`` is empty or the walk fell through to a
    marker-less directory, so smart ``init`` fails closed rather than adopting an
    unidentifiable cwd into a derived session. Uses ``find_repo_root`` (a pure
    cwd walk-up) rather than ``resolve_repo_root`` so the root reflects where the
    pane actually is, not a ``MOZYO_REPO`` override.
    """
    from mozyo_bridge.shared.paths import REPO_ROOT_MARKERS, find_repo_root

    if not cwd:
        return None
    root = find_repo_root(Path(cwd))
    if any((root / marker).exists() for marker in REPO_ROOT_MARKERS):
        return root
    return None


def _is_fallback_session_name(name: str) -> bool:
    """True for a low-information tmux-integrated fallback session name.

    The VS Code ``tmux-integrated`` extension sanitizes a non-ASCII workspace
    basename down to underscores, so a fully Japanese basename becomes an
    all-underscore session like ``___________``. Such a name carries no
    workspace identity and is safe for smart ``init`` to rename into the derived
    session. A name with any non-underscore character is treated as meaningful
    and is never renamed without an explicit ``--window-only`` opt-in.
    """
    return bool(name) and all(ch == "_" for ch in name)


def _agent_window_conflict(
    panes: list[dict], session: str, skip_window_index: str, agent: str
) -> list[str]:
    """Return `session:idx(pane)` for other windows in ``session`` named ``agent``.

    The resolver keys agents on the window name, so a second window named
    ``agent`` in the same session is ambiguous even though tmux tolerates it.
    The target window itself (``skip_window_index``) is excluded.
    """
    conflicts = []
    for pane in panes:
        pane_location_value = pane.get("location") or ""
        pane_session, _, pane_rest = pane_location_value.partition(":")
        if pane_session != session:
            continue
        pane_window_index = pane_rest.split(".", 1)[0]
        if pane_window_index == skip_window_index:
            continue
        if pane.get("window_name") == agent:
            conflicts.append(f"{pane_session}:{pane_window_index}({pane.get('id')})")
    return conflicts


def _write_vscode_session_name(root: Path, session_name: str) -> tuple[Path, bool, str | None]:
    """Merge ``tmux-integrated.sessionName`` into ``<root>/.vscode/settings.json``.

    Returns ``(path, written, warning)``. ``written`` is ``False`` with a
    non-``None`` ``warning`` when the existing file is JSONC / unparseable —
    smart ``init`` warns and continues rather than clobbering operator content
    or aborting the whole adoption. Only the workspace-local settings file is
    ever touched; user-global VS Code settings are never read or written.
    """
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import (
        VSCODE_SESSION_NAME_KEY,
        VSCODE_SETTINGS_RELATIVE,
        merge_vscode_session_name,
    )

    settings_path = root / VSCODE_SETTINGS_RELATIVE
    existing = (
        settings_path.read_text(encoding="utf-8") if settings_path.exists() else None
    )
    try:
        new_text = merge_vscode_session_name(existing, session_name)
    except ValueError as exc:
        return (
            settings_path,
            False,
            (
                f"{settings_path} is not plain JSON ({exc}); left unchanged. Add "
                f'"{VSCODE_SESSION_NAME_KEY}": "{session_name}" by hand, or re-run '
                "with --no-vscode-settings to silence this."
            ),
        )
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(new_text, encoding="utf-8")
    return settings_path, True, None
