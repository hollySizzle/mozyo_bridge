"""Cockpit layout planner / composition boundary (#12977).

Three composition helpers on the ``mozyo layout apply cockpit`` path historically
lived as procedural bodies in :mod:`mozyo_bridge.application.commands`:

- ``_resolve_project_scope_fields`` — resolve a cockpit column's Git repo root +
  project scope (Redmine #12658), fail-soft against the live filesystem.
- ``_resolve_cockpit_workspaces`` — resolve the active workspace columns to summon
  into the cockpit, either from explicit ``--repo`` columns or the live session
  inventory (#11788 / #11820).
- ``execute_cockpit_plan`` — run a :class:`CockpitPlan`'s tmux commands, resolving
  logical pane tokens and failing fast / cleaning up captured panes (#11788 /
  #11803).

The ``cmd_layout_apply`` entry itself already lives behind the ``launch_command``
boundary (#12933); this module carves the remaining planner / composition tail
into the same OOP-first shape under #12638:

- :func:`resolve_project_scope_fields` stays a fail-soft compatibility leaf (it
  runs its own project-discovery reads and never blocks the cockpit). The
  ``commands`` wrapper delegates to it so ``cmd_cockpit`` / the workspace resolver
  keep the same ``commands._resolve_project_scope_fields`` patch seam.
- :class:`CockpitWorkspacesOps` is the port for the reads the workspace resolver
  needs, and :class:`LiveCockpitWorkspacesOps` the live adapter routing each read
  *through the* :mod:`commands` *module at call time* — so the characterization
  tests that patch ``commands._resolve_workspace_lane`` /
  ``commands.resolve_canonical_session`` (and the source
  ``mozyo_bridge.session_inventory.take_inventory``) keep intercepting.
- :class:`CockpitPlanExecutorOps` is the port for the plan executor's ``run`` +
  ``die``; :class:`LiveCockpitPlanExecutorOps` binds the caller's ``run`` and
  routes ``commands.die`` at call time, so the tests that call the executor
  directly with a fake runner — and any that patch ``commands.die`` — keep
  intercepting.
- :class:`CockpitWorkspacesUseCase` / :class:`CockpitPlanExecutorUseCase` compose
  the ports and own the resolution / execution flow.

Behavior-preserving: the column identity, the dedupe keying, the fail-fast /
cleanup wording, and the returned shapes are unchanged from the original bodies.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
)


# --- Project-scope resolution leaf (fail-soft discovery). --------------------


def resolve_project_scope_fields(
    cwd: str | None, repo_root: str | None
) -> tuple[str | None, tuple[str | None, str | None, str | None], str | None]:
    """Resolve a cockpit column's Git repo root + project scope (Redmine #12658).

    When ``cwd`` resolves inside an adopted monorepo project, the workspace
    identity is the *Git repository root* (the umbrella/department workspace) and
    the project scope (the project's ``redmine_project`` id) is carried
    separately — so the cockpit / dry-run JSON keeps ``repo_root`` and the project
    path distinct. Returns ``(effective_repo_root, (scope, path, label),
    launch_cwd)`` where ``launch_cwd`` is the absolute project workdir a launched
    pane should start in (Redmine #12658 j#66505) so its cwd is under the project
    path and a ``--target-project`` handoff gate can pass.

    Fail-soft and compatibility-preserving: when no adopted project contains the
    cwd (a single-repo workspace, an un-scanned root, or any discovery error) the
    original ``repo_root`` is returned unchanged with an empty project triple and
    a ``None`` launch_cwd, so existing single-repo cockpit behavior is identical.
    """
    none_triple: tuple[str | None, str | None, str | None] = (None, None, None)
    if not cwd:
        return repo_root, none_triple, None
    try:
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
            project_scope_for_cwd,
            resolve_workspace_root,
        )

        # Prefer the real Git worktree root over a nested project-local scaffold
        # marker (Redmine #12658 j#66499): a monorepo project subdir may carry its
        # own `.mozyo-bridge/scaffold.json`, at which `infer_repo_root` would stop
        # and collapse the workspace onto the project. The Git root is the
        # workspace; the scaffold marker is the fallback only when there is no Git
        # root above (non-git scaffolded workspace, #11301).
        git_root = resolve_workspace_root(cwd) or repo_root
        if not git_root:
            return repo_root, none_triple, None
        scope = project_scope_for_cwd(cwd, git_root)
    except Exception:  # noqa: BLE001 - project scope is additive; never block cockpit
        return repo_root, none_triple, None
    if scope is None:
        return repo_root, none_triple, None
    # Launch the pane at the project workdir (repo-relative ``scope.workdir``
    # resolved against the Git root) so the pane cwd is under the project path.
    launch_cwd = str(Path(git_root) / scope.workdir)
    return git_root, (scope.scope, scope.path, scope.label), launch_cwd


# --- Workspace-column resolution: port + adapter + use case. -----------------


@runtime_checkable
class CockpitWorkspacesOps(Protocol):
    """Port: the reads the cockpit workspace-column resolver needs.

    The live adapter routes every read *through the* :mod:`commands` *module* (or
    the ``session_inventory`` source) at call time so the monkeypatched
    characterization tests still intercept, and so this module never imports
    :mod:`commands` at module scope (no import cycle).
    """

    def resolve_project_scope_fields(
        self, cwd: str | None, repo_root: str | None
    ) -> tuple[str | None, tuple[str | None, str | None, str | None], str | None]: ...

    def resolve_canonical_session(self, repo_root: str | None) -> Any: ...

    def resolve_workspace_lane(self, repo_root: str, workspace_id: Any) -> Any: ...

    def take_inventory(self) -> Any: ...


class LiveCockpitWorkspacesOps:
    """Live :class:`CockpitWorkspacesOps` over the real ``commands`` helpers.

    Each read resolves *through the* :mod:`commands` *module at call time* rather
    than binding at import time, so the ``_resolve_cockpit_workspaces``
    characterization tests that patch ``commands._resolve_workspace_lane`` /
    ``commands.resolve_canonical_session`` — and the ones that patch the source
    ``mozyo_bridge.session_inventory.take_inventory`` — keep intercepting. Project
    scope routes through ``commands._resolve_project_scope_fields`` so a patched
    scope resolution is honored on the workspace path too.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def resolve_project_scope_fields(
        self, cwd: str | None, repo_root: str | None
    ) -> tuple[str | None, tuple[str | None, str | None, str | None], str | None]:
        return self._commands()._resolve_project_scope_fields(cwd, repo_root)

    def resolve_canonical_session(self, repo_root: str | None) -> Any:
        return self._commands().resolve_canonical_session(repo_root)

    def resolve_workspace_lane(self, repo_root: str, workspace_id: Any) -> Any:
        return self._commands()._resolve_workspace_lane(repo_root, workspace_id)

    def take_inventory(self) -> Any:
        from mozyo_bridge.session_inventory import take_inventory

        return take_inventory()


@dataclass
class CockpitWorkspacesUseCase:
    """Resolve the active cockpit workspace columns over :class:`CockpitWorkspacesOps`.

    Mirrors the legacy ``_resolve_cockpit_workspaces`` body exactly: explicit
    ``--repo`` columns win in deterministic order; otherwise the active mozyo
    workspaces are discovered from the live session inventory — one column per
    distinct workspace+lane that currently carries a codex/claude agent pane.
    """

    ops: CockpitWorkspacesOps

    def resolve(self, args: argparse.Namespace) -> list:
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            CockpitWorkspace,
            normalize_lane,
        )

        ops = self.ops
        repos = getattr(args, "layout_repos", None)
        out: list = []
        if repos:
            for repo in repos:
                resolved = str(Path(repo).expanduser().resolve())
                # Project-scoped identity (#12658): re-root to the Git repo root
                # and carry the project scope separately when this column is an
                # adopted monorepo project; single-repo columns are unchanged.
                effective_root, (p_scope, p_path, p_label), p_launch = (
                    ops.resolve_project_scope_fields(resolved, resolved)
                )
                canon = ops.resolve_canonical_session(effective_root)
                wsid = getattr(canon, "workspace_id", None) or canon.name
                lane = ops.resolve_workspace_lane(
                    effective_root, getattr(canon, "workspace_id", None)
                )
                out.append(
                    CockpitWorkspace(
                        workspace_id=wsid,
                        label=canon.name,
                        repo_root=effective_root,
                        lane_id=lane.lane_id,
                        lane_label=lane.lane_label,
                        project_scope=p_scope,
                        project_path=p_path,
                        project_label=p_label,
                        launch_cwd=p_launch,
                    )
                )
            return out

        snapshot = ops.take_inventory()
        # One column per distinct workspace+lane (Redmine #11820). Keying by
        # `workspace_id` alone would collapse same-workspace-different-lane
        # checkouts (e.g. a main worktree and a linked worktree) into a single
        # column, which contradicts the append-as-separate-column contract this
        # US adds — so the dedupe key carries the normalized lane id too.
        by_lane: dict[tuple, object] = {}
        for rec in snapshot.records:
            if rec.agent_kind not in (AGENT_KIND_CODEX, AGENT_KIND_CLAUDE):
                continue
            wsid = (
                (rec.workspace.workspace_id if rec.workspace else None)
                or rec.repo_root
                or rec.session
            )
            lane = ops.resolve_workspace_lane(
                rec.repo_root or "",
                rec.workspace.workspace_id if rec.workspace else None,
            )
            # Project-scoped identity (#12658): a discovered pane carries its own
            # cwd, so resolve the project scope from it; the Git repo_root is kept
            # as the workspace authority.
            _eff_root, (p_scope, p_path, p_label), p_launch = (
                ops.resolve_project_scope_fields(rec.cwd, rec.repo_root)
            )
            key = (wsid, normalize_lane(lane.lane_id), p_scope or "")
            if key not in by_lane:
                by_lane[key] = CockpitWorkspace(
                    workspace_id=wsid,
                    label=rec.session,
                    repo_root=rec.repo_root,
                    lane_id=lane.lane_id,
                    lane_label=lane.lane_label,
                    project_scope=p_scope,
                    project_path=p_path,
                    project_label=p_label,
                    launch_cwd=p_launch,
                )
        return list(by_lane.values())


# --- Plan execution: port + adapter + use case. ------------------------------


@runtime_checkable
class CockpitPlanExecutorOps(Protocol):
    """Port: the ``run`` + ``die`` the cockpit plan executor drives.

    The live adapter binds the caller's ``run`` (the module-level ``run_tmux`` or
    a test's fake) and routes ``die`` through :mod:`commands` at call time.
    """

    def run(self, *args: Any, **kwargs: Any) -> Any: ...

    def die(self, message: str) -> NoReturn: ...


class LiveCockpitPlanExecutorOps:
    """Live :class:`CockpitPlanExecutorOps` binding the caller's ``run``.

    ``run`` is bound at construction (each ``execute_cockpit_plan`` caller passes
    its own ``run_tmux`` / fake), and ``die`` resolves through the :mod:`commands`
    module at call time so the executors that patch ``commands.die`` still
    intercept the fatal abort.
    """

    def __init__(self, run: Any) -> None:
        self._run = run

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self._run(*args, **kwargs)

    def die(self, message: str) -> NoReturn:
        from mozyo_bridge.application import commands

        commands.die(message)
        raise AssertionError("unreachable")  # pragma: no cover - die raises SystemExit


@dataclass
class CockpitPlanExecutorUseCase:
    """Run a :class:`CockpitPlan`'s tmux commands over :class:`CockpitPlanExecutorOps`.

    Mirrors the legacy ``execute_cockpit_plan`` body exactly. Each command's
    logical pane tokens (``@colN_role``) are substituted with the real ``%pane``
    id captured from an earlier ``-P -F '#{pane_id}'`` command. Returns the
    token -> pane id map.

    Fail-fast (Redmine #11788 review): a tmux step that exits non-zero, or a
    capturing step that does not return a ``%pane`` id, is fatal. Continuing would
    run later steps against an empty / wrong target and present a broken
    half-built layout as if it succeeded, so the layout — whose source of truth is
    tmux state — must abort instead.

    ``cleanup_captured`` (Redmine #11803 review): when appending into an existing
    shared cockpit the session must not be killed, so a mid-append failure would
    otherwise orphan the new panes already created. With this flag, every pane
    captured so far is ``kill-pane``'d (best-effort) before aborting, leaving the
    shared cockpit's other columns intact.
    """

    ops: CockpitPlanExecutorOps

    def execute(self, plan, *, cleanup_captured: bool = False) -> dict:
        ops = self.ops
        ids: dict[str, str] = {}

        def _abort(message: str):
            if cleanup_captured:
                for pane_id in ids.values():
                    ops.run("kill-pane", "-t", pane_id, check=False)
            ops.die(message)

        for cmd in plan.commands:
            argv = [ids.get(token, token) for token in cmd.argv]
            result = ops.run(*argv, check=False)
            if getattr(result, "returncode", 0) != 0:
                detail = (getattr(result, "stderr", "") or "").strip() or (
                    getattr(result, "stdout", "") or ""
                ).strip()
                _abort(
                    f"cockpit layout step failed ({cmd.purpose}): "
                    f"`tmux {' '.join(argv)}` -> {detail or 'nonzero exit'}"
                )
            if cmd.captures:
                pane_id = (getattr(result, "stdout", "") or "").strip()
                if not pane_id.startswith("%"):
                    _abort(
                        f"cockpit layout step did not return a pane id "
                        f"({cmd.purpose}): got {pane_id!r}"
                    )
                ids[cmd.captures] = pane_id
        return ids
