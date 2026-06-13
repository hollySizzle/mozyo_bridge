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

# Machine-readable identity stamped on every cockpit pane as tmux user options
# (Redmine #11803): the pane title is human-facing, but duplicate detection /
# focus / append must read identity reliably, so the workspace id and agent
# role go on `@mozyo_workspace_id` / `@mozyo_agent_role` user options instead of
# parsing the title string.
WORKSPACE_OPTION = "@mozyo_workspace_id"
ROLE_OPTION = "@mozyo_agent_role"

# Additive lane / checkout identity (Redmine #11820). The same `workspace_id`
# can appear in several checkouts (git worktree / clone / devcontainer) when a
# tracked `.mozyo-bridge/workspace.json` is duplicated; the lane id distinguishes
# them so the cockpit treats same-workspace-different-lane as a separate column
# instead of a focus target. It NEVER redefines `workspace_id` — it rides on its
# own `@mozyo_lane_id` user option, with an optional human-facing
# `@mozyo_lane_label`.
LANE_OPTION = "@mozyo_lane_id"
LANE_LABEL_OPTION = "@mozyo_lane_label"

# A checkout that is not a distinct lane — the primary worktree, the registered
# canonical checkout, or a non-git workspace — belongs to the "default" lane.
# Pre-#11820 cockpit panes carry no `@mozyo_lane_id` and normalize to this same
# value, so an upgraded cockpit keeps focusing them rather than appending a
# duplicate column.
DEFAULT_LANE = "default"


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
    # Checkout-local lane identity (Redmine #11820). Defaults keep every
    # existing construction (and non-lane environments) on the backward-compatible
    # "default" lane.
    lane_id: str = DEFAULT_LANE
    lane_label: Optional[str] = None


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
    lane_id: str = DEFAULT_LANE
    lane_label: Optional[str] = None


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
                    "lane_id": p.lane_id,
                    "lane_label": p.lane_label,
                }
                for p in self.panes
            ],
            "commands": [c.as_dict() for c in self.commands],
        }


def _pane_title(label: str, role: str, anchor: Optional[str]) -> str:
    base = f"{label} · {role}"
    return f"{base} · {anchor}" if anchor else base


def _pane_identity_commands(pane: "CockpitPane") -> list["CockpitCommand"]:
    """Title (human-facing) + workspace/role/lane tmux user options (machine-readable).

    The lane id always lands (normalized to ``default``) so duplicate detection
    can read ``workspace_id + lane_id`` off every cockpit pane; the human-facing
    lane label is only stamped when present.
    """
    commands = [
        CockpitCommand(
            argv=("select-pane", "-t", pane.token, "-T", pane.title),
            captures=None,
            purpose=f"title {pane.workspace_id} {pane.role}",
        ),
        CockpitCommand(
            argv=(
                "set-option", "-p", "-t", pane.token,
                WORKSPACE_OPTION, pane.workspace_id,
            ),
            captures=None,
            purpose=f"mark workspace {pane.workspace_id} ({pane.role})",
        ),
        CockpitCommand(
            argv=("set-option", "-p", "-t", pane.token, ROLE_OPTION, pane.role),
            captures=None,
            purpose=f"mark role {pane.role} ({pane.workspace_id})",
        ),
        CockpitCommand(
            argv=(
                "set-option", "-p", "-t", pane.token,
                LANE_OPTION, normalize_lane(pane.lane_id),
            ),
            captures=None,
            purpose=f"mark lane {normalize_lane(pane.lane_id)} ({pane.workspace_id})",
        ),
    ]
    if pane.lane_label:
        commands.append(
            CockpitCommand(
                argv=(
                    "set-option", "-p", "-t", pane.token,
                    LANE_LABEL_OPTION, pane.lane_label,
                ),
                captures=None,
                purpose=f"label lane {pane.lane_label} ({pane.workspace_id})",
            )
        )
    return commands


def normalize_ratio(codex_ratio: int) -> int:
    """Clamp the Codex share to a sane, splittable 10..90 range."""
    return max(10, min(90, int(codex_ratio)))


def even_column_share(total_columns: int) -> int:
    """Percent width of one column when ``total_columns`` share the window evenly.

    Used to size an appended full-height column (Redmine #11854). A bare
    ``split-window -h -f`` gives the new full-height column ~50% of the *whole*
    window on every append, so each append halves the existing columns and the
    last-added lane balloons while older lanes starve (#11850 j#57317). Sizing
    the split to ``-l {even_column_share(N)}%`` instead makes the new column take
    only its fair ``1/N`` of the window; with ``-f`` the percentage is of the
    full window width, so tmux scales the existing (already-equal) columns into
    the remaining space and every column stays equal — while each column keeps
    its vertical Codex/Claude split (which ``select-layout even-horizontal``
    would have flattened, see :func:`build_cockpit_append_plan`).

    Clamped to a splittable 1..99 so a degenerate ``total_columns`` can never
    emit a 0% or 100% split that tmux would reject.
    """
    n = max(2, int(total_columns))
    return max(1, min(99, round(100 / n)))


def normalize_lane(value: Optional[str]) -> str:
    """Empty / missing lane id -> the backward-compatible ``default`` lane."""
    text = (value or "").strip()
    return text or DEFAULT_LANE


@dataclass(frozen=True)
class LaneIdentity:
    """A checkout-local lane identity (Redmine #11820).

    ``lane_id`` distinguishes multiple checkouts (git worktree / clone /
    relocated copy) of the *same* ``workspace_id`` so the cockpit treats them as
    separate columns. It is additive — it never redefines ``workspace_id``. The
    primary checkout maps to :data:`DEFAULT_LANE`; a distinct checkout maps to a
    deterministic, privacy-safe ``lane-<hash>`` derived from git facts (never a
    raw absolute path). ``lane_label`` is a human-facing hint (branch name or
    checkout basename), display-only.
    """

    lane_id: str
    lane_label: Optional[str]


def _norm_path(path: Optional[str]) -> Optional[str]:
    """Trailing-slash-insensitive comparison key (display-only normalization)."""
    if not path:
        return path
    stripped = path.rstrip("/")
    return stripped or "/"


def _basename(path: Optional[str]) -> str:
    if not path:
        return ""
    return path.rstrip("/").rsplit("/", 1)[-1]


def _lane_hash(seed: str) -> str:
    """Deterministic, privacy-safe lane id: a truncated digest, never a path."""
    import hashlib

    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"lane-{digest[:12]}"


def resolve_lane_identity(
    *,
    repo_root: str,
    canonical_path: Optional[str] = None,
    git_dir: Optional[str] = None,
    git_common_dir: Optional[str] = None,
    branch: Optional[str] = None,
) -> LaneIdentity:
    """Derive the lane identity for a checkout from observed facts (pure).

    A checkout is a *distinct* lane when either:

    - it is a linked git worktree — ``git_dir`` differs from ``git_common_dir``
      (the main worktree has them equal, a linked worktree points its git dir at
      ``.../.git/worktrees/<name>`` while the common dir stays the source), or
    - it is a relocated checkout — its ``repo_root`` differs from the workspace's
      registered ``canonical_path`` (a clone / copy that shares the same
      ``workspace_id`` via a duplicated ``.mozyo-bridge/workspace.json``).

    Otherwise it is the primary checkout and maps to :data:`DEFAULT_LANE`. The
    lane id is hashed from ``git_dir`` (unique per worktree and per clone) or, if
    absent, the checkout path — so durable / displayed surfaces carry a stable
    token, never a raw absolute path.
    """
    git_dir_n = _norm_path(git_dir)
    common_n = _norm_path(git_common_dir)
    branch_label = (branch or "").strip() or None

    is_linked_worktree = bool(git_dir_n and common_n and git_dir_n != common_n)
    repo_n = _norm_path(repo_root)
    canon_n = _norm_path(canonical_path)
    is_relocated = bool(canon_n and repo_n and repo_n != canon_n)

    if is_linked_worktree or is_relocated:
        seed = git_dir_n or repo_n or repo_root
        label = branch_label or _basename(repo_root) or None
        return LaneIdentity(lane_id=_lane_hash(seed), lane_label=label)
    return LaneIdentity(lane_id=DEFAULT_LANE, lane_label=branch_label)


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
                lane_id=ws.lane_id,
                lane_label=ws.lane_label,
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
                lane_id=ws.lane_id,
                lane_label=ws.lane_label,
            )
        )

    # --- Pane identity: human title + machine-readable workspace/role options. ---
    for pane in panes:
        commands.extend(_pane_identity_commands(pane))

    return CockpitPlan(
        session=session,
        window=COCKPIT_WINDOW,
        codex_ratio=codex_ratio,
        claude_ratio=claude_ratio,
        columns=len(workspaces),
        panes=tuple(panes),
        commands=tuple(commands),
    )


def build_cockpit_append_plan(
    workspace: CockpitWorkspace,
    *,
    anchor_pane: str,
    column_index: int,
    codex_ratio: int = DEFAULT_CODEX_RATIO,
    session: str = COCKPIT_SESSION_DEFAULT,
    launch: Optional[Callable[[str, CockpitWorkspace], Optional[str]]] = None,
) -> CockpitPlan:
    """Plan appending ONE new column to an existing cockpit (Redmine #11803).

    ``anchor_pane`` is the real ``%pane`` id of the rightmost existing column's
    Codex pane; the new column is split to its right and widths re-equalized.
    ``column_index`` is the 0-based position of the new column (used only for
    the logical token names so they cannot collide with existing panes). Pure.
    """
    if not anchor_pane:
        raise ValueError("append needs the anchor pane id of an existing column")

    codex_ratio = normalize_ratio(codex_ratio)
    claude_ratio = 100 - codex_ratio
    codex_token = f"@col{column_index}_codex"
    claude_token = f"@col{column_index}_claude"
    commands: list[CockpitCommand] = []

    def _launch(role: str) -> Optional[str]:
        return launch(role, workspace) if launch is not None else None

    # New column: a FULL-HEIGHT horizontal split (`-h -f`) to the right of the
    # existing layout (Redmine #11807). A plain `-h` would split only the
    # anchor Codex pane's cell, and a follow-up `select-layout even-horizontal`
    # would flatten every existing column's vertical Codex/Claude split into
    # separate left/right panes. `-f` makes the split span the whole window
    # height — a true new column — and we deliberately do NOT re-run
    # `even-horizontal`, so each existing workspace keeps its Codex-top /
    # Claude-bottom pair intact.
    #
    # Size the split to the new column's fair `1/N` share of the window
    # (Redmine #11854). Without `-l`, the full-height split takes ~50% of the
    # whole window each time, so the newest lane balloons and the existing lanes
    # starve (#11850 j#57317). With `-f` the `-l N%` is a percentage of the full
    # window width, so tmux scales the existing (equal) columns into the rest and
    # every column stays equal — re-equalizing widths without an `even-horizontal`
    # that would flatten the vertical splits.
    even_share = even_column_share(column_index + 1)
    codex_argv = [
        "split-window", "-h", "-f", "-l", f"{even_share}%", "-t", anchor_pane,
    ]
    if workspace.repo_root:
        codex_argv += ["-c", workspace.repo_root]
    codex_argv += ["-P", "-F", "#{pane_id}"]
    cmd = _launch(ROLE_CODEX)
    if cmd:
        codex_argv.append(cmd)
    commands.append(
        CockpitCommand(
            argv=tuple(codex_argv),
            captures=codex_token,
            purpose=f"append column {column_index} codex ({workspace.label})",
        )
    )
    claude_argv = [
        "split-window", "-v", "-t", codex_token, "-l", f"{claude_ratio}%",
    ]
    if workspace.repo_root:
        claude_argv += ["-c", workspace.repo_root]
    claude_argv += ["-P", "-F", "#{pane_id}"]
    cmd = _launch(ROLE_CLAUDE)
    if cmd:
        claude_argv.append(cmd)
    commands.append(
        CockpitCommand(
            argv=tuple(claude_argv),
            captures=claude_token,
            purpose=f"append column {column_index} claude ({workspace.label})",
        )
    )

    panes = (
        CockpitPane(
            token=codex_token, column=column_index, role=ROLE_CODEX,
            workspace_id=workspace.workspace_id, label=workspace.label,
            repo_root=workspace.repo_root,
            title=_pane_title(workspace.label, ROLE_CODEX, workspace.codex_anchor),
            height_pct=codex_ratio, anchor=workspace.codex_anchor,
            lane_id=workspace.lane_id, lane_label=workspace.lane_label,
        ),
        CockpitPane(
            token=claude_token, column=column_index, role=ROLE_CLAUDE,
            workspace_id=workspace.workspace_id, label=workspace.label,
            repo_root=workspace.repo_root,
            title=_pane_title(workspace.label, ROLE_CLAUDE, workspace.claude_anchor),
            height_pct=claude_ratio, anchor=workspace.claude_anchor,
            lane_id=workspace.lane_id, lane_label=workspace.lane_label,
        ),
    )
    for pane in panes:
        commands.extend(_pane_identity_commands(pane))

    return CockpitPlan(
        session=session,
        window=COCKPIT_WINDOW,
        codex_ratio=codex_ratio,
        claude_ratio=claude_ratio,
        columns=1,
        panes=panes,
        commands=tuple(commands),
    )


def build_cockpit_focus_plan(
    target_pane: str, *, session: str = COCKPIT_SESSION_DEFAULT
) -> CockpitPlan:
    """Plan focusing an already-present cockpit pane (Redmine #11803).

    No panes are created — a duplicate workspace is selected, not re-appended.
    """
    if not target_pane:
        raise ValueError("focus needs the target pane id")
    commands = (
        CockpitCommand(
            argv=("select-window", "-t", f"{session}:{COCKPIT_WINDOW}"),
            captures=None,
            purpose="focus cockpit window",
        ),
        CockpitCommand(
            argv=("select-pane", "-t", target_pane),
            captures=None,
            purpose=f"focus existing pane {target_pane}",
        ),
    )
    return CockpitPlan(
        session=session,
        window=COCKPIT_WINDOW,
        codex_ratio=0,
        claude_ratio=0,
        columns=0,
        panes=(),
        commands=commands,
    )
