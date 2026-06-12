"""Cockpit layout planner (Redmine #11788).

The cockpit view arranges *active* workspaces as horizontal columns, and within
each column the workspace's two agents as a vertical split — Codex on top,
Claude on the bottom — at a configurable ratio (default Codex 70 / Claude 30):

    workspace A          workspace B
    +---------------+    +---------------+
    | Codex   70%   |    | Codex   70%   |
    +---------------+    +---------------+
    | Claude  30%   |    | Claude  30%   |
    +---------------+    +---------------+

tmux state is the source of truth for the layout; iTerm2 control mode (`--cc`,
Redmine #11729) is only a display surface over that tmux state and carries no
layout semantics of its own.

This module is **pure**: :func:`build_cockpit_plan` turns a list of workspaces
into an inspectable :class:`CockpitPlan` — pane metadata plus an ordered list of
tmux commands that use *logical* pane tokens (``@col0_codex`` …). The executor
(in the application layer) resolves those tokens to real ``%pane`` ids as it
captures them, so the plan can be generated, JSON-dumped, dry-run printed, and
unit-tested without a live tmux server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

COCKPIT_SESSION_DEFAULT = "mozyo-cockpit"
COCKPIT_WINDOW = "cockpit"
DEFAULT_CODEX_RATIO = 70

ROLE_CODEX = "codex"
ROLE_CLAUDE = "claude"
ROLES = (ROLE_CODEX, ROLE_CLAUDE)


@dataclass(frozen=True)
class CockpitWorkspace:
    """One active workspace to summon into the cockpit as a column."""

    workspace_id: str
    label: str
    repo_root: Optional[str]
    # Redmine issue / journal pointer per role, when known. Display-only —
    # recorded in the pane title so the operator can see whose turn it is.
    codex_anchor: Optional[str] = None
    claude_anchor: Optional[str] = None


@dataclass(frozen=True)
class CockpitPane:
    """A planned pane: which workspace/role it holds and how tall it is."""

    token: str  # logical id, e.g. "@col0_claude"
    column: int
    role: str
    workspace_id: str
    label: str
    repo_root: Optional[str]
    title: str
    height_pct: int
    anchor: Optional[str]


@dataclass(frozen=True)
class CockpitCommand:
    """One tmux invocation in the plan.

    ``argv`` may contain logical pane tokens (``@colN_role``); the executor
    substitutes the captured real pane id before running tmux. When
    ``captures`` is set, the command is expected to print a ``%pane`` id
    (``-P -F '#{pane_id}'``) that the executor binds to that token.
    """

    argv: tuple[str, ...]
    captures: Optional[str]
    purpose: str

    def as_dict(self) -> dict:
        return {
            "argv": list(self.argv),
            "captures": self.captures,
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class CockpitPlan:
    session: str
    window: str
    codex_ratio: int
    claude_ratio: int
    columns: int
    panes: tuple[CockpitPane, ...]
    commands: tuple[CockpitCommand, ...]

    def as_dict(self) -> dict:
        return {
            "session": self.session,
            "window": self.window,
            "codex_ratio": self.codex_ratio,
            "claude_ratio": self.claude_ratio,
            "columns": self.columns,
            "panes": [
                {
                    "token": p.token,
                    "column": p.column,
                    "role": p.role,
                    "workspace_id": p.workspace_id,
                    "label": p.label,
                    "repo_root": p.repo_root,
                    "title": p.title,
                    "height_pct": p.height_pct,
                    "anchor": p.anchor,
                }
                for p in self.panes
            ],
            "commands": [c.as_dict() for c in self.commands],
        }


def _pane_title(label: str, role: str, anchor: Optional[str]) -> str:
    base = f"{label} · {role}"
    return f"{base} · {anchor}" if anchor else base


def normalize_ratio(codex_ratio: int) -> int:
    """Clamp the Codex share to a sane, splittable 10..90 range."""
    return max(10, min(90, int(codex_ratio)))


def build_cockpit_plan(
    workspaces: Sequence[CockpitWorkspace],
    *,
    codex_ratio: int = DEFAULT_CODEX_RATIO,
    session: str = COCKPIT_SESSION_DEFAULT,
    launch: Optional[Callable[[str, CockpitWorkspace], Optional[str]]] = None,
) -> CockpitPlan:
    """Plan the cockpit layout for ``workspaces`` (left-to-right columns).

    ``launch(role, workspace)`` returns the shell command a pane should start
    (e.g. the OTel-wrapped agent launch), or ``None`` to leave the pane at a
    shell. Pure: returns a :class:`CockpitPlan`, runs no tmux.
    """
    if not workspaces:
        raise ValueError("cockpit layout needs at least one active workspace")

    codex_ratio = normalize_ratio(codex_ratio)
    claude_ratio = 100 - codex_ratio
    target = f"{session}:{COCKPIT_WINDOW}"
    panes: list[CockpitPane] = []
    commands: list[CockpitCommand] = []

    def _launch(role: str, ws: CockpitWorkspace) -> Optional[str]:
        return launch(role, ws) if launch is not None else None

    # --- Columns: one Codex pane per workspace, left to right. ---
    prev_codex_token: Optional[str] = None
    for col, ws in enumerate(workspaces):
        codex_token = f"@col{col}_codex"
        if col == 0:
            argv = [
                "new-session", "-d", "-s", session, "-n", COCKPIT_WINDOW,
            ]
            if ws.repo_root:
                argv += ["-c", ws.repo_root]
            argv += ["-P", "-F", "#{pane_id}"]
        else:
            # Split the previous column's Codex pane horizontally so columns
            # land left-to-right; even-horizontal below equalizes widths.
            argv = ["split-window", "-h", "-t", prev_codex_token]
            if ws.repo_root:
                argv += ["-c", ws.repo_root]
            argv += ["-P", "-F", "#{pane_id}"]
        cmd = _launch(ROLE_CODEX, ws)
        if cmd:
            argv.append(cmd)
        commands.append(
            CockpitCommand(
                argv=tuple(argv),
                captures=codex_token,
                purpose=f"column {col} codex pane ({ws.label})",
            )
        )
        panes.append(
            CockpitPane(
                token=codex_token,
                column=col,
                role=ROLE_CODEX,
                workspace_id=ws.workspace_id,
                label=ws.label,
                repo_root=ws.repo_root,
                title=_pane_title(ws.label, ROLE_CODEX, ws.codex_anchor),
                height_pct=codex_ratio,
                anchor=ws.codex_anchor,
            )
        )
        prev_codex_token = codex_token

    if len(workspaces) > 1:
        commands.append(
            CockpitCommand(
                argv=("select-layout", "-t", target, "even-horizontal"),
                captures=None,
                purpose="equalize column widths",
            )
        )

    # --- Within each column: split Codex vertically to add Claude on bottom. ---
    for col, ws in enumerate(workspaces):
        codex_token = f"@col{col}_codex"
        claude_token = f"@col{col}_claude"
        argv = [
            "split-window", "-v", "-t", codex_token, "-l", f"{claude_ratio}%",
        ]
        if ws.repo_root:
            argv += ["-c", ws.repo_root]
        argv += ["-P", "-F", "#{pane_id}"]
        cmd = _launch(ROLE_CLAUDE, ws)
        if cmd:
            argv.append(cmd)
        commands.append(
            CockpitCommand(
                argv=tuple(argv),
                captures=claude_token,
                purpose=f"column {col} claude pane ({ws.label})",
            )
        )
        panes.append(
            CockpitPane(
                token=claude_token,
                column=col,
                role=ROLE_CLAUDE,
                workspace_id=ws.workspace_id,
                label=ws.label,
                repo_root=ws.repo_root,
                title=_pane_title(ws.label, ROLE_CLAUDE, ws.claude_anchor),
                height_pct=claude_ratio,
                anchor=ws.claude_anchor,
            )
        )

    # --- Pane titles: record workspace id + agent role for the operator. ---
    for pane in panes:
        commands.append(
            CockpitCommand(
                argv=(
                    "select-pane", "-t", pane.token, "-T", pane.title,
                ),
                captures=None,
                purpose=f"title {pane.workspace_id} {pane.role}",
            )
        )

    return CockpitPlan(
        session=session,
        window=COCKPIT_WINDOW,
        codex_ratio=codex_ratio,
        claude_ratio=claude_ratio,
        columns=len(workspaces),
        panes=tuple(panes),
        commands=tuple(commands),
    )
