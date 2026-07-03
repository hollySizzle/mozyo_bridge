"""Cockpit dispatcher boundary: sub-action routing + create/append/focus flow (#13011).

The ``mozyo cockpit`` command body historically lived as one procedural
dispatcher — ``cmd_cockpit`` — in :mod:`mozyo_bridge.application.commands`.
Its sub-action handlers were carved into their own OOP-first boundaries over
the #12638 tranche, but the dispatcher residual still mixed the routing
decision, the side-effect reads driving the create/append/focus flow, the pure
decisions (duplicate detection + action resolution), and the presentation
(the ``--json`` / ``--dry-run`` projections and the real-run prints + attach).

This module carves that into an OOP-first boundary under #13011, following the
#12987 / #12989 cockpit carves:

- :func:`resolve_cockpit_route` is the pure sub-action routing decision,
  preserving the original short-circuit order (the whole-cockpit sub-actions
  route before workspace resolution; adopt and reset/rebuild route after the
  column read because they need the duplicate-detection context).
- :func:`find_same_unit_column` / :func:`resolve_shared_cockpit_action` are the
  pure decisions: the #11820/#12739 ``workspace_id + lane_id + project_scope``
  duplicate detection, and the shared-column create / focus / append resolution
  (fail-closed on a stale cockpit) with the ``rightmost_codex_anchor`` geometry
  pick injected as a callable (mirroring the #12982 group-window carve).
- :func:`build_cockpit_json_payload` / :func:`render_cockpit_dry_run_lines` are
  the pure ``--json`` / ``--dry-run`` projections.
- :class:`CockpitSubactionRoutes` is the port over the eight already-carved
  sub-action handlers (routed through the ``commands._handle_cockpit_*`` thin
  wrappers at call time); :class:`CockpitLaunchFlowOps` is the port for the
  launcher flow's environment (workspace/lane/scope resolution, cockpit reads,
  presentation-config load, group-window action, tmux gate, #11803 plan
  executor, fail-closed abort, stdout line sink). The live adapters resolve
  every target *through the* :mod:`commands` *module at call time* (never at
  import) — and ``repo_local_config_loader.load_repo_local_config`` through its
  module — so the decision / presentation characterization tests that patch
  those seams keep intercepting and no import cycle is introduced.
- :class:`CockpitDispatchUseCase` composes the two ports and the pure decisions
  into :meth:`run`, rendering through the port's ``emit`` sink and returning a
  :class:`CockpitDispatchOutcome` whose ``attach_session`` hands the terminal
  ``os.execvp`` attach back to the thin ``cmd_cockpit`` wrapper — the process
  replacement stays in :mod:`commands`, preserving the ``commands.os.execvp``
  patch seam.

Behavior-preserving: the sub-action short-circuit order, the read-only
``--dry-run`` / ``--json`` contract, the duplicate-detection focus priority,
the fail-closed stale-cockpit / invalid-presentation aborts, the group-window
execution and its rollback boundary, the create/focus/append prints, the
adopt-advisory and degraded-presentation notices, and the attach /
``--no-attach`` tail are unchanged from the original command body. This module
reuses the #12977 plan executor via the port; it never reimplements executor
semantics.

Redmine #13015 adds the sublane separate-window placement on top: the opt-in
``delegation_window_policy: separate`` gives a sublane whose repo faithfully
executes ``project_group_tmux_window`` its own tmux window via the group-window
action machinery; every fallback is recorded machine-readably on the
``sublane_window`` payload field (never a silent reroute). Redmine #13085 makes
``shared`` the default: a sublane reuses the single project/common host window.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from mozyo_bridge.application.cockpit_group_window_command import (
    GROUP_ACTION_CREATE,
    GROUP_ACTION_FOCUS,
    GROUP_ACTIONS,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    COCKPIT_SESSION_DEFAULT,
    CockpitWorkspace,
    build_cockpit_append_plan,
    build_cockpit_focus_plan,
    build_cockpit_plan,
    normalize_lane,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping import (
    GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW,
    LaunchContext,
    resolve_group_window_placement,
    resolve_launch_placement,
    resolve_sublane_window_placement,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfigError,
)


# --- Pure routing decision: the `mozyo cockpit` sub-action vocabulary. ---------

# Whole-cockpit sub-actions that short-circuit BEFORE workspace resolution: they
# inspect the live cockpit (or an explicit pane/unit selection), never the
# current workspace, so they must not gate on the workspace context (#12131 /
# #12341 / #12133 / #12135 / #12136).
ROUTE_DOCTOR_GEOMETRY = "doctor-geometry"
ROUTE_LIST = "list"
ROUTE_STATUS = "status"
ROUTE_PEER_ADOPT = "peer-adopt"
ROUTE_REBALANCE = "rebalance"
ROUTE_RECONCILE = "reconcile"
# Sub-actions that route AFTER the workspace + column read: adopt needs the
# duplicate-detection context ("already a cockpit column" means nothing to
# adopt, #11897) and reset/rebuild needs the workspace + launch closure for the
# rebuild create (#11814).
ROUTE_ADOPT = "adopt"
ROUTE_RESET = "reset"
# The default create/append/focus launcher flow (#11803).
ROUTE_LAUNCH = "launch"

_PRE_WORKSPACE_ROUTES = (
    ROUTE_DOCTOR_GEOMETRY,
    ROUTE_LIST,
    ROUTE_STATUS,
    ROUTE_PEER_ADOPT,
    ROUTE_REBALANCE,
    ROUTE_RECONCILE,
)


def resolve_cockpit_route(action: Optional[str]) -> str:
    """Map ``args.action`` to its dispatch route key (pure, #13011).

    ``reset`` and ``rebuild`` share :data:`ROUTE_RESET` (rebuild is the
    reset-then-create variant of the same confirm-gated handler, #11814); any
    other / absent action falls through to the default :data:`ROUTE_LAUNCH`
    create/append/focus flow.
    """
    if action in _PRE_WORKSPACE_ROUTES:
        return action
    if action == "adopt":
        return ROUTE_ADOPT
    if action in ("reset", "rebuild"):
        return ROUTE_RESET
    return ROUTE_LAUNCH


# --- Pure decisions: duplicate detection + shared-column action resolution. ----


def find_same_unit_column(columns: Any, workspace: Any) -> tuple[list, Optional[dict]]:
    """``(existing_codex, same)`` — the #11820/#12739 duplicate detection (pure).

    Duplicate detection compares ``workspace_id + lane_id + project_scope``:
    same workspace + same lane + same project scope focuses the existing column;
    a different lane (a worktree / clone / relocated checkout) OR a different
    project scope falls through to append as its own column. The project scope
    is the #12658 stamp, so a department-root pane (empty ``project_scope``) and
    a project-scoped gateway pane that share the same Git root and lane are
    distinct Units that can coexist (#12739). A pre-#11820 pane carries no lane
    id and normalizes to ``default``; a pre-#12658 pane carries no project scope
    and normalizes to empty, matching a root launch.
    """
    existing_codex = [c for c in (columns or []) if c.get("role") == "codex"]
    target_lane = normalize_lane(workspace.lane_id)
    target_scope = (workspace.project_scope or "").strip()
    same = next(
        (
            c
            for c in existing_codex
            if c.get("workspace_id") == workspace.workspace_id
            and normalize_lane(c.get("lane_id")) == target_lane
            and (c.get("project_scope") or "").strip() == target_scope
        ),
        None,
    )
    return existing_codex, same


def resolve_shared_cockpit_action(
    workspace: Any,
    session: str,
    *,
    columns: Any,
    session_present: bool,
    same: Optional[dict],
    existing_codex: list,
    codex_ratio: int,
    launch: Callable[[str, Any], str],
    rightmost_codex_anchor: Callable[[Any], Optional[str]],
) -> tuple[str, Any, Optional[str]]:
    """Resolve the shared-column ``(action, plan, blocked_reason)`` (pure, #11803).

    ``plan is None`` marks a blocked action (stale cockpit) — fail-closed on a
    real run, reported (not aborted) under ``--dry-run`` / ``--json``. The
    ``rightmost_codex_anchor`` geometry pick is injected as a callable so this
    decision stays pure over the already-read columns (#11849: anchor on the
    visually rightmost column by geometry, not list-panes order — a
    middle-column anchor would let the full-height split crush an existing
    column's width).
    """
    if columns is None and session_present:
        # The session exists but has no usable cockpit window. Treating this as
        # "create" would run `new-session` against an existing session (failing)
        # and the cleanup could then kill that pre-existing session — so fail
        # closed with a recovery action instead (#11803 review).
        return (
            "create",
            None,
            (
                f"session {session!r} already exists but has no cockpit window to "
                f"add a column to. Rebuild it with `mozyo layout apply cockpit`, or "
                f"remove it (`tmux kill-session -t {session}`) and re-run "
                "`mozyo cockpit`."
            ),
        )
    if columns is None:
        return (
            "create",
            build_cockpit_plan(
                [workspace], codex_ratio=codex_ratio, session=session, launch=launch
            ),
            None,
        )
    if same is not None:
        return (
            "focus",
            build_cockpit_focus_plan(same["pane_id"], session=session),
            None,
        )
    anchor = rightmost_codex_anchor(existing_codex)
    if anchor:
        return (
            "append",
            build_cockpit_append_plan(
                workspace,
                anchor_pane=anchor,
                column_index=len(existing_codex),
                codex_ratio=codex_ratio,
                session=session,
                launch=launch,
            ),
            None,
        )
    return (
        "append",
        None,
        (
            f"cockpit session {session!r} exists but carries no "
            "mozyo-identified codex column to append beside; rebuild it "
            "with `mozyo layout apply cockpit` or remove the stale session."
        ),
    )


# --- Pure presentation: the `--json` payload and `--dry-run` plan text. --------


def build_cockpit_json_payload(
    *,
    plan: Any,
    action: str,
    workspace: Any,
    session: str,
    blocked_reason: Optional[str],
    adopt_advisory: Any,
    presentation_decision: Any,
    presentation_blocked: Optional[str],
    group_window: Optional[str],
    sublane_decision: Any = None,
) -> dict:
    """The ``mozyo cockpit --json`` payload (pure projection, #11803/#12739)."""
    payload = plan.as_dict() if plan is not None else {}
    payload["action"] = action
    payload["workspace_id"] = workspace.workspace_id
    payload["lane_id"] = normalize_lane(workspace.lane_id)
    payload["lane_label"] = workspace.lane_label
    # Project scope rides the projection (Redmine #12739) so `cockpit --json`
    # can show that a project-scoped launch appends a distinct Unit instead
    # of focusing the department-root column. Empty for a root / single-repo
    # workspace.
    payload["project_scope"] = (workspace.project_scope or "").strip()
    payload["session"] = session
    payload["blocked"] = blocked_reason
    payload["adopt_advisory"] = (
        adopt_advisory.as_dict() if adopt_advisory is not None else None
    )
    payload["presentation"] = (
        presentation_decision.as_dict()
        if presentation_decision is not None
        else None
    )
    payload["presentation_blocked"] = presentation_blocked
    payload["group_window"] = group_window
    # #13015: sublane window decision + machine-readable degraded fallback.
    payload["sublane_window"] = (
        sublane_decision.as_dict() if sublane_decision is not None else None
    )
    return payload


def render_cockpit_dry_run_lines(
    *,
    plan: Any,
    action: str,
    workspace: Any,
    session: str,
    blocked_reason: Optional[str],
    adopt_advisory: Any,
    presentation_decision: Any,
    presentation_blocked: Optional[str],
    group_window: Optional[str],
    sublane_decision: Any = None,
) -> list[str]:
    """The ``mozyo cockpit --dry-run`` plan text (pure rendering, #11803)."""
    lines = [
        f"cockpit plan: action={action} session={session} "
        f"workspace={workspace.workspace_id} ({workspace.label}) "
        f"lane={normalize_lane(workspace.lane_id)}"
    ]
    if plan is None:
        lines.append(f"  (blocked: {blocked_reason})")
    else:
        for cmd in plan.commands:
            lines.append(
                "  tmux " + " ".join(shlex.quote(token) for token in cmd.argv)
            )
    if presentation_blocked:
        lines.append(f"  (presentation blocked: {presentation_blocked})")
    elif presentation_decision is not None and presentation_decision.degraded:
        lines.append(f"  presentation: {presentation_decision.diagnostic}")
    if group_window is not None:
        if sublane_decision is not None and getattr(
            sublane_decision, "separated", False
        ):
            # #13015: the plan targets the lane's own sublane window.
            lines.append(
                f"  presentation: delegation_window_policy=separate -> sublane "
                f"window {group_window!r} (tmux window requested, never "
                "guaranteed; display only)"
            )
        else:
            lines.append(
                f"  presentation: project_group_tmux_window -> Project Group window "
                f"{group_window!r} (tmux window requested, never guaranteed; "
                "display only)"
            )
    elif sublane_decision is not None and getattr(sublane_decision, "degraded", False):
        # #13015: explicit sublane-window fallback, never silent.
        lines.append(f"  presentation: {sublane_decision.diagnostic}")
    if adopt_advisory is not None and adopt_advisory.message:
        lines.append(f"  {adopt_advisory.message}")
    return lines


# --- Outcome: exit code + the terminal attach handed back to the wrapper. ------


@dataclass(frozen=True)
class CockpitDispatchOutcome:
    """Result of the cockpit dispatch flow.

    ``exit_code`` is the process exit status. ``attach_session`` is the tmux
    session the thin ``cmd_cockpit`` wrapper must ``os.execvp``-attach to (the
    fresh-create path without ``--no-attach``); ``None`` means no attach — the
    process replacement stays in :mod:`commands` so the ``commands.os.execvp``
    patch seam is unchanged.
    """

    exit_code: int
    attach_session: Optional[str] = None


# --- Ports + live adapters over the ``commands`` seams. ------------------------


@runtime_checkable
class CockpitSubactionRoutes(Protocol):
    """Port: the eight already-carved ``mozyo cockpit`` sub-action handlers.

    Each method mirrors one ``commands._handle_cockpit_*`` thin wrapper (their
    own OOP boundaries live behind those wrappers); the live adapter routes each
    through the :mod:`commands` module at call time so the wrappers' patch seams
    and boundary wiring stay authoritative.
    """

    def doctor_geometry(self, session: str, *, json_output: bool) -> int: ...

    def membership_list(self, session: str, *, json_output: bool) -> int: ...

    def membership_status(self, args: Any, session: str, *, json_output: bool) -> int: ...

    def peer_adopt(
        self, session: str, args: Any, *, json_output: bool, dry_run: bool
    ) -> int: ...

    def rebalance(
        self, session: str, *, confirm: bool, json_output: bool, dry_run: bool
    ) -> int: ...

    def reconcile(
        self, session: str, *, confirm: bool, json_output: bool, dry_run: bool,
        codex_ratio: int,
    ) -> int: ...

    def adopt(
        self, args: Any, workspace: Any, session: str, *, columns: Any,
        session_present: bool, already_in_cockpit: bool, existing_codex: list,
    ) -> int: ...

    def reset(
        self, args: Any, workspace: Any, session: str, *, columns: Any,
        session_present: bool, rebuild: bool, launch: Callable[[str, Any], str],
        codex_ratio: int,
    ) -> int: ...


@runtime_checkable
class CockpitLaunchFlowOps(Protocol):
    """Port: the environment the create/append/focus launcher flow composes over.

    The reads (project scope, canonical session, lane, columns, session
    presence, adopt advisory, presentation grouping), the #12982 group-window
    action, the #11849 anchor pick, the tmux availability gate, the #12977 plan
    executor, the fail-closed abort, and the stdout line sink (injected so a
    fake captures the rendering).
    """

    def resolve_project_scope_fields(
        self, cwd: Optional[str], repo_root: Optional[str]
    ) -> Any: ...

    def resolve_canonical_session(self, repo_root: str) -> Any: ...

    def resolve_workspace_lane(self, repo_root: str, workspace_id: Any) -> Any: ...

    def agent_launch_command(self, role: str, session: str, repo_root: str) -> str: ...

    def require_tmux(self) -> None: ...

    def read_cockpit_columns(self, session: str) -> Any: ...

    def cockpit_session_present(self, session: str) -> bool: ...

    def adopt_advisory(self, workspace: Any, session: str) -> Any: ...

    def load_presentation_grouping(self, repo_root: str) -> Any: ...

    def group_window_action(
        self, workspace: Any, session: str, *, decision: Any, codex_ratio: int,
        launch: Callable[[str, Any], str],
    ) -> Any: ...

    def rightmost_codex_anchor(self, codex_columns: Any) -> Optional[str]: ...

    def execute_plan(self, plan: Any, *, cleanup_captured: bool = False) -> Any: ...

    def die(self, message: str) -> None: ...

    def emit(self, text: str) -> None: ...


class LiveCockpitSubactionRoutes:
    """Live :class:`CockpitSubactionRoutes` over the ``commands._handle_cockpit_*`` wrappers.

    Each method resolves its handler *through the* :mod:`commands` *module at
    call time* rather than binding it at import time, so the sub-action
    boundaries (and any test that patches a handler wrapper) stay authoritative
    and no import cycle is introduced.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def doctor_geometry(self, session: str, *, json_output: bool) -> int:
        return self._commands()._handle_cockpit_doctor_geometry(
            session, json_output=json_output
        )

    def membership_list(self, session: str, *, json_output: bool) -> int:
        return self._commands()._handle_cockpit_list(session, json_output=json_output)

    def membership_status(self, args: Any, session: str, *, json_output: bool) -> int:
        return self._commands()._handle_cockpit_status(
            args, session, json_output=json_output
        )

    def peer_adopt(
        self, session: str, args: Any, *, json_output: bool, dry_run: bool
    ) -> int:
        return self._commands()._handle_cockpit_peer_adopt(
            session, args, json_output=json_output, dry_run=dry_run
        )

    def rebalance(
        self, session: str, *, confirm: bool, json_output: bool, dry_run: bool
    ) -> int:
        return self._commands()._handle_cockpit_rebalance(
            session, confirm=confirm, json_output=json_output, dry_run=dry_run
        )

    def reconcile(
        self, session: str, *, confirm: bool, json_output: bool, dry_run: bool,
        codex_ratio: int,
    ) -> int:
        return self._commands()._handle_cockpit_reconcile(
            session,
            confirm=confirm,
            json_output=json_output,
            dry_run=dry_run,
            codex_ratio=codex_ratio,
        )

    def adopt(
        self, args: Any, workspace: Any, session: str, *, columns: Any,
        session_present: bool, already_in_cockpit: bool, existing_codex: list,
    ) -> int:
        return self._commands()._handle_cockpit_adopt(
            args,
            workspace,
            session,
            columns=columns,
            session_present=session_present,
            already_in_cockpit=already_in_cockpit,
            existing_codex=existing_codex,
        )

    def reset(
        self, args: Any, workspace: Any, session: str, *, columns: Any,
        session_present: bool, rebuild: bool, launch: Callable[[str, Any], str],
        codex_ratio: int,
    ) -> int:
        return self._commands()._handle_cockpit_reset(
            args,
            workspace,
            session,
            columns=columns,
            session_present=session_present,
            rebuild=rebuild,
            launch=launch,
            codex_ratio=codex_ratio,
        )


class LiveCockpitLaunchFlowOps:
    """Live :class:`CockpitLaunchFlowOps` over the real ``commands`` seams.

    Each method resolves its target *through the* :mod:`commands` *module at
    call time* rather than binding it at import time, so the decision /
    presentation characterization tests that patch the ``commands.*`` seams
    (and ``repo_local_config_loader.load_repo_local_config``) keep
    intercepting, and this module never imports :mod:`commands` at module scope
    (no import cycle).
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def resolve_project_scope_fields(
        self, cwd: Optional[str], repo_root: Optional[str]
    ) -> Any:
        return self._commands()._resolve_project_scope_fields(cwd, repo_root)

    def resolve_canonical_session(self, repo_root: str) -> Any:
        return self._commands().resolve_canonical_session(repo_root)

    def resolve_workspace_lane(self, repo_root: str, workspace_id: Any) -> Any:
        return self._commands()._resolve_workspace_lane(repo_root, workspace_id)

    def agent_launch_command(self, role: str, session: str, repo_root: str) -> str:
        # Cockpit / sublane append: same reproducible auto policy (#11925).
        return self._commands()._agent_launch_command(
            role,
            session,
            repo_root,
            permission_mode_default=COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
        )

    def require_tmux(self) -> None:
        self._commands().require_tmux()

    def read_cockpit_columns(self, session: str) -> Any:
        return self._commands()._read_cockpit_columns(session)

    def cockpit_session_present(self, session: str) -> bool:
        return self._commands()._cockpit_session_present(session)

    def adopt_advisory(self, workspace: Any, session: str) -> Any:
        return self._commands()._cockpit_adopt_advisory(workspace, session)

    def load_presentation_grouping(self, repo_root: str) -> Any:
        from mozyo_bridge.application import repo_local_config_loader

        return repo_local_config_loader.load_repo_local_config(
            repo_root
        ).presentation.grouping

    def group_window_action(
        self, workspace: Any, session: str, *, decision: Any, codex_ratio: int,
        launch: Callable[[str, Any], str],
    ) -> Any:
        return self._commands()._cockpit_group_window_action(
            workspace,
            session,
            decision=decision,
            codex_ratio=codex_ratio,
            launch=launch,
        )

    def rightmost_codex_anchor(self, codex_columns: Any) -> Optional[str]:
        return self._commands()._rightmost_codex_anchor(codex_columns)

    def execute_plan(self, plan: Any, *, cleanup_captured: bool = False) -> Any:
        commands = self._commands()
        return commands.execute_cockpit_plan(
            plan, commands.run_tmux, cleanup_captured=cleanup_captured
        )

    def die(self, message: str) -> None:
        self._commands().die(message)

    def emit(self, text: str) -> None:
        print(text)


# --- Use case: route the sub-action, else run the create/append/focus flow. ----


class CockpitDispatchUseCase:
    """The ``mozyo cockpit`` dispatcher over the injected ports (#13011).

    :meth:`run` routes the sub-action short-circuits in the original order,
    then composes the create/append/focus launcher flow: workspace resolution,
    duplicate detection, the presentation placement, the plan resolution, the
    read-only ``--json`` / ``--dry-run`` projections, and the confirm-free
    real-run execution. The fresh-create attach is returned as
    :attr:`CockpitDispatchOutcome.attach_session` for the thin wrapper.
    """

    def __init__(
        self, routes: CockpitSubactionRoutes, ops: CockpitLaunchFlowOps
    ) -> None:
        self._routes = routes
        self._ops = ops

    def run(self, args: Any) -> CockpitDispatchOutcome:
        session = getattr(args, "cockpit_session", None) or COCKPIT_SESSION_DEFAULT
        codex_ratio = int(getattr(args, "codex_ratio", 70) or 70)
        json_output = bool(getattr(args, "json_output", False))
        dry_run = bool(getattr(args, "dry_run", False))
        action_arg = getattr(args, "action", None)
        route = resolve_cockpit_route(action_arg)

        # Whole-cockpit sub-actions short-circuit before workspace resolution:
        # they inspect the live cockpit (or an explicit pane/unit selection),
        # never the current workspace, and their preview paths never gate on
        # tmux being mutable (#12131 / #12341 / #12133 / #12135 / #12136).
        if route == ROUTE_DOCTOR_GEOMETRY:
            return CockpitDispatchOutcome(
                self._routes.doctor_geometry(session, json_output=json_output)
            )
        if route == ROUTE_LIST:
            return CockpitDispatchOutcome(
                self._routes.membership_list(session, json_output=json_output)
            )
        if route == ROUTE_STATUS:
            return CockpitDispatchOutcome(
                self._routes.membership_status(args, session, json_output=json_output)
            )
        if route == ROUTE_PEER_ADOPT:
            return CockpitDispatchOutcome(
                self._routes.peer_adopt(
                    session, args, json_output=json_output, dry_run=dry_run
                )
            )
        if route == ROUTE_REBALANCE:
            return CockpitDispatchOutcome(
                self._routes.rebalance(
                    session,
                    confirm=bool(getattr(args, "confirm", False)),
                    json_output=json_output,
                    dry_run=dry_run,
                )
            )
        if route == ROUTE_RECONCILE:
            return CockpitDispatchOutcome(
                self._routes.reconcile(
                    session,
                    confirm=bool(getattr(args, "confirm", False)),
                    json_output=json_output,
                    dry_run=dry_run,
                    codex_ratio=codex_ratio,
                )
            )

        # adopt (#11897) and reset/rebuild (#11814) are their own confirm-gated
        # sub-actions whose default paths are non-mutating previews, so they do
        # not gate on tmux being mutable up front — but they need the workspace
        # + column read below (adopt's "already a cockpit column" duplicate
        # context; reset's rebuild create), so they route after it.
        adopt_mode = route == ROUTE_ADOPT
        reset_mode = route == ROUTE_RESET
        inspect_only = dry_run or json_output
        no_attach = bool(getattr(args, "no_attach", False))

        repo = getattr(args, "repo", None) or getattr(args, "cwd", None) or os.getcwd()
        cwd_root = str(Path(repo).expanduser().resolve())
        # Project-scoped identity (#12658): when the cockpit is summoned from
        # inside an adopted monorepo project, the workspace identity is the Git
        # repo root and the project scope rides separately, so the dry-run JSON
        # keeps repo_root and the project path distinct. A single-repo workspace
        # keeps repo_root == cwd_root.
        repo_root, (p_scope, p_path, p_label), p_launch = (
            self._ops.resolve_project_scope_fields(cwd_root, cwd_root)
        )
        canon = self._ops.resolve_canonical_session(repo_root)
        lane = self._ops.resolve_workspace_lane(
            repo_root, getattr(canon, "workspace_id", None)
        )
        workspace = CockpitWorkspace(
            workspace_id=getattr(canon, "workspace_id", None) or canon.name,
            label=canon.name,
            repo_root=repo_root,
            lane_id=lane.lane_id,
            lane_label=lane.lane_label,
            project_scope=p_scope,
            project_path=p_path,
            project_label=p_label,
            launch_cwd=p_launch,
        )

        def launch(role: str, ws: Any) -> str:
            return self._ops.agent_launch_command(role, session, ws.repo_root)

        # Read-only state read drives the create/append/focus decision.
        # `--dry-run` / `--json` only read (never mutate) — the column read /
        # session-presence read are tolerant so a missing/stale cockpit degrades
        # gracefully instead of aborting.
        if not inspect_only and not adopt_mode and not reset_mode:
            self._ops.require_tmux()
        columns = self._ops.read_cockpit_columns(session)
        session_present = self._ops.cockpit_session_present(session)

        existing_codex, same = find_same_unit_column(columns, workspace)

        # `mozyo cockpit adopt` short-circuits to its own create/append/focus-free
        # path (#11897 detect / #11898 confirm-gated move): it never spawns fresh
        # columns. `same is not None` means the workspace+lane is already a
        # cockpit column (focus priority, j#57823), so there is nothing to adopt.
        if adopt_mode:
            return CockpitDispatchOutcome(
                self._routes.adopt(
                    args,
                    workspace,
                    session,
                    columns=columns,
                    session_present=session_present,
                    already_in_cockpit=same is not None,
                    existing_codex=existing_codex,
                )
            )

        # `mozyo cockpit reset` / `rebuild` (#11814) is a confirm-gated,
        # mozyo-identity-gated teardown of a stale/broken cockpit; it never
        # spawns the normal append/focus column and never silently adopts.
        if reset_mode:
            return CockpitDispatchOutcome(
                self._routes.reset(
                    args,
                    workspace,
                    session,
                    columns=columns,
                    session_present=session_present,
                    rebuild=(action_arg == "rebuild"),
                    launch=launch,
                    codex_ratio=codex_ratio,
                )
            )

        # Adopt advisory rides the normal create/append flow as a NON-mutating
        # notice (#11897): a co-existing normal `mozyo` session for this
        # workspace+lane is an adopt candidate the operator may prefer over a
        # fresh column. Skipped on the focus path (`same is not None`), where
        # the cockpit already shows it.
        adopt_advisory = (
            self._ops.adopt_advisory(workspace, session) if same is None else None
        )

        # Desired Project-Group presentation placement (#12302 / #12330), read
        # from `.mozyo-bridge/config.yaml` for THIS workspace.
        # `same_cockpit_column` (the default / a missing config) preserves
        # current behavior exactly; `project_group_tmux_window` faithfully
        # executes (`execute_group_window=True`); `normal_window` visibly
        # degrades to the shared column. An invalid placement config fails
        # closed (reported under --json/--dry-run, fatal on a real run) — never
        # a silent reroute. Display-only: never a routing / approval authority.
        presentation_decision = None
        presentation_blocked = None
        grouping = None
        try:
            grouping = self._ops.load_presentation_grouping(repo_root)
            placement = resolve_launch_placement(
                grouping,
                LaunchContext(
                    workspace_id=workspace.workspace_id,
                    lane_id=normalize_lane(workspace.lane_id),
                    repo_label=workspace.label,
                ),
            )
            presentation_decision = resolve_group_window_placement(
                grouping.project_group_presentation,
                placement,
                execute_group_window=True,
            )
        except RepoLocalConfigError as exc:
            presentation_blocked = (
                f"invalid .mozyo-bridge/config.yaml presentation config: {exc}"
            )

        # Faithful per-Project-Group tmux window (#12330): when the config opts
        # into `project_group_tmux_window` AND the cockpit session already has
        # its `cockpit` home window, route to the group-window action instead of
        # the shared-column flow; session bootstrap (no cockpit window yet)
        # stays behavior-preserving below.
        faithful_group = (
            presentation_decision is not None
            and presentation_blocked is None
            and presentation_decision.executed_surface
            == GROUP_WINDOW_SURFACE_GROUP_TMUX_WINDOW
        )

        # Sublane window placement (#13015 / #13085): the opt-in `separate`
        # gives a launching sublane its OWN tmux window via the faithful
        # group-window flow (#12330); `shared` (the default) and every fallback
        # keep the single project/common host window (any degrade recorded on
        # the decision, never a silent reroute).
        sublane_decision = None
        if grouping is not None and presentation_blocked is None:
            sublane_decision = resolve_sublane_window_placement(
                grouping.delegation_window_policy,
                workspace_id=workspace.workspace_id, lane_id=workspace.lane_id,
                lane_label=workspace.lane_label,
                group_window_executing=faithful_group,
                cockpit_window_present=columns is not None,
            )
        sublane_separate = sublane_decision is not None and sublane_decision.separated

        # The sublane's own window (when separated) wins over the project-group
        # window; both run through the same group-window action machinery.
        window_decision = None
        if sublane_separate:
            window_decision = sublane_decision
        elif faithful_group and columns is not None:
            window_decision = presentation_decision

        group_window = None
        if window_decision is not None:
            action, plan, blocked_reason, group_window = self._ops.group_window_action(
                workspace, session, decision=window_decision,
                codex_ratio=codex_ratio, launch=launch,
            )
        else:
            action, plan, blocked_reason = resolve_shared_cockpit_action(
                workspace,
                session,
                columns=columns,
                session_present=session_present,
                same=same,
                existing_codex=existing_codex,
                codex_ratio=codex_ratio,
                launch=launch,
                rightmost_codex_anchor=self._ops.rightmost_codex_anchor,
            )

        if json_output:
            payload = build_cockpit_json_payload(
                plan=plan,
                action=action,
                workspace=workspace,
                session=session,
                blocked_reason=blocked_reason,
                adopt_advisory=adopt_advisory,
                presentation_decision=presentation_decision,
                presentation_blocked=presentation_blocked,
                group_window=group_window,
                sublane_decision=sublane_decision,
            )
            self._ops.emit(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            )
            return CockpitDispatchOutcome(exit_code=0)
        if dry_run:
            for line in render_cockpit_dry_run_lines(
                plan=plan,
                action=action,
                workspace=workspace,
                session=session,
                blocked_reason=blocked_reason,
                adopt_advisory=adopt_advisory,
                presentation_decision=presentation_decision,
                presentation_blocked=presentation_blocked,
                group_window=group_window,
                sublane_decision=sublane_decision,
            ):
                self._ops.emit(line)
            return CockpitDispatchOutcome(exit_code=0)

        if presentation_blocked:
            # Fail closed on a real run: an invalid presentation config never
            # silently changes (or silently keeps) the placement.
            self._ops.die(presentation_blocked)

        if blocked_reason:
            self._ops.die(blocked_reason)

        if action in GROUP_ACTIONS:
            # Faithful per-Project-Group tmux window (#12330). All three actions
            # mutate the LIVE cockpit session and never spawn a fresh -CC attach
            # (the operator switches tmux windows). create / append use
            # cleanup_captured so a mid-build failure kills only the panes this
            # attempt created — and because tmux drops a window with no panes, a
            # failed group-window create leaves no orphan window (rollback
            # boundary, acceptance #12330).
            if action == GROUP_ACTION_FOCUS:
                self._ops.execute_plan(plan)
                self._ops.emit(
                    f"workspace {workspace.label!r} already in cockpit {session!r} "
                    f"(window {group_window!r}); focused it."
                )
                return CockpitDispatchOutcome(exit_code=0)
            self._ops.execute_plan(plan, cleanup_captured=True)
            window_noun = (
                "sublane window" if sublane_separate else "Project Group window"
            )
            if action == GROUP_ACTION_CREATE:
                self._ops.emit(
                    f"created {window_noun} {group_window!r} in cockpit "
                    f"{session!r} for {workspace.label!r}; switch to it with your tmux "
                    "window keys (no new iTerm window opened)."
                )
            else:
                self._ops.emit(
                    f"appended {workspace.label!r} as a new column to {window_noun} "
                    f"{group_window!r} in cockpit {session!r}; switch to it with "
                    "your tmux window keys (no new iTerm window opened)."
                )
            if adopt_advisory is not None and adopt_advisory.message:
                self._ops.emit(f"  {adopt_advisory.message}")
            return CockpitDispatchOutcome(exit_code=0)

        if action == "create":
            # cleanup_captured kills only the panes THIS attempt created (closing
            # the freshly-created session) — never a blanket `kill-session` that
            # could destroy a pre-existing `mozyo-cockpit` we did not create. If
            # `new-session` itself fails, nothing was captured and nothing is
            # killed, so an existing session is left intact (#11803 review).
            self._ops.execute_plan(plan, cleanup_captured=True)
            self._ops.emit(
                f"cockpit created: session={session} workspace={workspace.label}"
            )
            if adopt_advisory is not None and adopt_advisory.message:
                self._ops.emit(f"  {adopt_advisory.message}")
            if presentation_decision is not None and presentation_decision.degraded:
                self._ops.emit(f"  presentation: {presentation_decision.diagnostic}")
            if sublane_decision is not None and sublane_decision.degraded:
                self._ops.emit(f"  presentation: {sublane_decision.diagnostic}")
            if no_attach:
                self._ops.emit(f"attach: tmux -CC attach -t {session}")
                return CockpitDispatchOutcome(exit_code=0)
            return CockpitDispatchOutcome(exit_code=0, attach_session=session)

        if action == "focus":
            # Existing cockpit already shows this workspace — select it, never a
            # duplicate column, and never a second attach/iTerm window.
            self._ops.execute_plan(plan)
            self._ops.emit(
                f"workspace {workspace.label!r} already in cockpit {session!r}; "
                f"focused pane {same['pane_id']}"
            )
            return CockpitDispatchOutcome(exit_code=0)

        # append: add a column to the live cockpit without a new iTerm window. On
        # a mid-append failure the newly-created panes are cleaned up
        # (cleanup_captured) so a failed append never orphans panes in the shared
        # cockpit; the other workspaces' columns are left untouched (#11803
        # review).
        self._ops.execute_plan(plan, cleanup_captured=True)
        self._ops.emit(
            f"appended {workspace.label!r} as a new column to cockpit {session!r}; "
            "switch to your cockpit window to see it (no new iTerm window opened)"
        )
        if adopt_advisory is not None and adopt_advisory.message:
            self._ops.emit(f"  {adopt_advisory.message}")
        if presentation_decision is not None and presentation_decision.degraded:
            self._ops.emit(f"  presentation: {presentation_decision.diagnostic}")
        if sublane_decision is not None and sublane_decision.degraded:
            self._ops.emit(f"  presentation: {sublane_decision.diagnostic}")
        return CockpitDispatchOutcome(exit_code=0)
