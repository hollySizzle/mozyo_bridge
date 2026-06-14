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
from typing import Callable, Iterable, Mapping, Optional, Sequence

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


# --- Cockpit adopt detection (Redmine #11897, Phase 1: detect + advisory) -----
#
# A normal `mozyo` session and a cockpit column can co-exist for the same
# workspace+lane (Redmine #11816 j#57823): the daily `cd <ws> && mozyo` rail puts
# each agent in its own tmux *window* (role on the window name), while the cockpit
# puts them in one `cockpit` window (role on `@mozyo_agent_role`). Phase 1 is
# strictly NON-DESTRUCTIVE — it only *detects* a co-existing normal session and
# advises that it is an adopt candidate. It never plans or runs a `join-pane`
# transfer; the explicit, confirm-gated pane move is Phase 2 (Redmine #11898).

# Adopt advisory grading (fail-closed by design, j#57823):
ADOPT_STATUS_NONE = "none"  # no co-existing normal session for this workspace+lane
ADOPT_STATUS_CANDIDATE = "candidate"  # exactly one normal session with BOTH agents
ADOPT_STATUS_PARTIAL = "partial"  # one normal session but only one role present
ADOPT_STATUS_AMBIGUOUS = "ambiguous"  # more than one matching normal session

_PHASE2_POINTER = (
    "Explicit pane adoption is `mozyo cockpit adopt --confirm` (Redmine #11898); "
    "without --confirm adopt only previews and moves no panes."
)


@dataclass(frozen=True)
class NormalSessionObservation:
    """One normal-`mozyo` agent pane projected for adopt detection (#11897).

    A privacy-aware, pure-input projection of a discovered agent pane: the tmux
    ``session`` it lives in, the resolved ``workspace_id`` + ``lane_id`` of its
    checkout, its ``role`` (``claude`` / ``codex``), and ``pane_id`` for operator
    reference. The application layer builds these from the session inventory; the
    detector below stays pure and testable without tmux.
    """

    session: str
    workspace_id: str
    lane_id: str
    role: str
    pane_id: str = ""


@dataclass(frozen=True)
class CoexistingNormalSession:
    """A normal `mozyo` session co-existing with the cockpit for one workspace+lane."""

    session: str
    roles: tuple[str, ...]  # sorted distinct agent roles present (claude/codex)
    pane_ids: tuple[str, ...]  # sorted pane ids, operator reference only
    # Sorted ``(role, pane_id)`` pairs (Redmine #11898 / Phase 2). ``roles`` and
    # ``pane_ids`` above are sorted independently and so lose the role->pane
    # mapping; the confirm-gated adopt move (codex top, claude bottom) needs to
    # know *which* pane is which role, so the mapping is carried explicitly here.
    agent_panes: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict:
        return {
            "session": self.session,
            "roles": list(self.roles),
            "pane_ids": list(self.pane_ids),
            "agent_panes": [list(pair) for pair in self.agent_panes],
        }


@dataclass(frozen=True)
class AdoptAdvisory:
    """The read-only result of cockpit-adopt detection (#11897, Phase 1).

    ``status`` grades adoptability (see ``ADOPT_STATUS_*``); only a
    ``candidate`` is :pyattr:`adoptable`. ``message`` is the human advisory line
    (``None`` only for :data:`ADOPT_STATUS_NONE`, where there is nothing to say).
    This describes what an advisory should report — it carries no plan and moves
    no panes.
    """

    workspace_id: str
    lane_id: str
    status: str
    candidates: tuple[CoexistingNormalSession, ...]
    message: Optional[str]

    @property
    def adoptable(self) -> bool:
        return self.status == ADOPT_STATUS_CANDIDATE

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)

    def as_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "status": self.status,
            "adoptable": self.adoptable,
            "candidates": [c.as_dict() for c in self.candidates],
            "message": self.message,
        }


def detect_adopt_candidates(
    *,
    workspace_id: str,
    lane_id: str,
    observations: Iterable[NormalSessionObservation],
    cockpit_session: str = COCKPIT_SESSION_DEFAULT,
) -> AdoptAdvisory:
    """Detect co-existing normal `mozyo` sessions for one workspace+lane (#11897).

    Pure and read-only: it classifies what a non-destructive cockpit-adopt
    advisory should say and never plans or runs a pane transfer. Only
    observations matching this exact ``workspace_id`` + ``lane_id`` (lane
    normalized, so a pre-#11820 empty lane matches ``default``) and carrying a
    real agent role are considered; the cockpit's own session is excluded so its
    columns never look like an adopt source.

    Fail-closed grading (Redmine #11816 j#57823): a single normal session
    carrying BOTH agents is an adopt :data:`ADOPT_STATUS_CANDIDATE`; one role
    only is :data:`ADOPT_STATUS_PARTIAL`; more than one matching normal session
    is :data:`ADOPT_STATUS_AMBIGUOUS`; none is :data:`ADOPT_STATUS_NONE`. Only a
    candidate is adoptable — every other state is advisory-only.
    """
    target_lane = normalize_lane(lane_id)
    by_session: dict[str, dict] = {}
    order: list[str] = []
    for obs in observations:
        if obs.role not in ROLES:
            continue
        if obs.session == cockpit_session:
            continue
        if obs.workspace_id != workspace_id:
            continue
        if normalize_lane(obs.lane_id) != target_lane:
            continue
        bucket = by_session.get(obs.session)
        if bucket is None:
            bucket = {"roles": set(), "pane_ids": set(), "pairs": set()}
            by_session[obs.session] = bucket
            order.append(obs.session)
        bucket["roles"].add(obs.role)
        if obs.pane_id:
            bucket["pane_ids"].add(obs.pane_id)
            bucket["pairs"].add((obs.role, obs.pane_id))

    candidates = tuple(
        CoexistingNormalSession(
            session=name,
            roles=tuple(sorted(by_session[name]["roles"])),
            pane_ids=tuple(sorted(by_session[name]["pane_ids"])),
            agent_panes=tuple(sorted(by_session[name]["pairs"])),
        )
        for name in order
    )

    if not candidates:
        return AdoptAdvisory(workspace_id, target_lane, ADOPT_STATUS_NONE, (), None)

    where = f"workspace {workspace_id!r} lane {target_lane!r}"
    if len(candidates) > 1:
        names = ", ".join(repr(c.session) for c in candidates)
        message = (
            f"notice: {len(candidates)} co-existing normal `mozyo` sessions "
            f"({names}) match {where}; cockpit adopt is ambiguous and fails "
            f"closed — resolve to a single session before adopting. {_PHASE2_POINTER}"
        )
        return AdoptAdvisory(
            workspace_id, target_lane, ADOPT_STATUS_AMBIGUOUS, candidates, message
        )

    only = candidates[0]
    if set(only.roles) >= set(ROLES):
        message = (
            f"notice: co-existing normal `mozyo` session {only.session!r} "
            f"(claude+codex) is running for {where} and is not in the cockpit. "
            f"It is an adopt candidate — inspect it with `mozyo cockpit adopt`. "
            f"{_PHASE2_POINTER}"
        )
        return AdoptAdvisory(
            workspace_id, target_lane, ADOPT_STATUS_CANDIDATE, candidates, message
        )

    present = ", ".join(only.roles)
    message = (
        f"notice: co-existing normal `mozyo` session {only.session!r} for {where} "
        f"carries only {present}; both claude and codex are required to adopt, so "
        f"it fails closed. {_PHASE2_POINTER}"
    )
    return AdoptAdvisory(
        workspace_id, target_lane, ADOPT_STATUS_PARTIAL, candidates, message
    )


# --- Cockpit adopt move (Redmine #11898, Phase 2: confirm-gated pane move) -----
#
# Phase 1 (#11897) only *detects* a co-existing normal `mozyo` session and advises
# that it is an adopt candidate. Phase 2 plans the explicit, confirm-gated move of
# that session's live codex/claude panes into the cockpit as a new column, using
# `join-pane` (which *moves* a live pane, preserving its `%pane` id and the agent
# running in it) rather than `split-window` (which would spawn fresh agents). The
# plan is pure and inspectable; the application-layer executor runs it atomically
# with best-effort rollback. Scope (j#57880 risk posture): this US adopts into an
# *existing* cockpit only — bootstrapping a brand-new cockpit session out of a
# moved pair is deliberately out of scope to avoid a fragile partial live move;
# the operator creates the cockpit first (`mozyo cockpit`) and then adopts.


def adopt_pane_pair(candidate: "CoexistingNormalSession") -> Optional[tuple[str, str]]:
    """The ``(codex_pane, claude_pane)`` ids to move, or ``None`` if ambiguous.

    Fail-closed on role ambiguity (Redmine #11898 acceptance): a clean adopt
    needs *exactly one* codex pane and *exactly one* claude pane. If a role maps
    to zero or more than one pane the pair is unknown, so the move must not be
    planned (the caller treats ``None`` as a fail-closed block).
    """
    codex = [pane for (role, pane) in candidate.agent_panes if role == ROLE_CODEX]
    claude = [pane for (role, pane) in candidate.agent_panes if role == ROLE_CLAUDE]
    if len(codex) == 1 and len(claude) == 1:
        return codex[0], claude[0]
    return None


@dataclass(frozen=True)
class CockpitAdoptPlan:
    """Plan for the confirm-gated move of one normal session into the cockpit (#11898).

    ``join_commands`` are the two **atomic** ``join-pane`` moves (codex column,
    then claude below it); ``stamp_commands`` re-apply the machine-readable
    identity (`@mozyo_workspace_id` / `@mozyo_agent_role` / `@mozyo_lane_id`) on
    the moved panes *after* both joins land. The executor treats the joins as a
    transaction (rollback the first if the second fails) and the stamps as
    best-effort (the pair is already adopted once both joins succeed). The
    ``source_*`` fields drive that rollback and the explicit (never implicit)
    source-session cleanup reporting.
    """

    session: str
    window: str
    codex_ratio: int
    claude_ratio: int
    column_index: int
    workspace_id: str
    lane_id: str
    lane_label: Optional[str]
    source_session: str
    source_codex_pane: str
    source_claude_pane: str
    anchor_pane: str
    panes: tuple[CockpitPane, ...]
    join_commands: tuple[CockpitCommand, ...]
    stamp_commands: tuple[CockpitCommand, ...]

    @property
    def commands(self) -> tuple[CockpitCommand, ...]:
        """All planned commands in execution order (for dry-run / JSON preview)."""
        return self.join_commands + self.stamp_commands

    def as_dict(self) -> dict:
        return {
            "session": self.session,
            "window": self.window,
            "codex_ratio": self.codex_ratio,
            "claude_ratio": self.claude_ratio,
            "column_index": self.column_index,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "lane_label": self.lane_label,
            "source_session": self.source_session,
            "source_codex_pane": self.source_codex_pane,
            "source_claude_pane": self.source_claude_pane,
            "anchor_pane": self.anchor_pane,
            "panes": [
                {
                    "token": p.token,
                    "column": p.column,
                    "role": p.role,
                    "workspace_id": p.workspace_id,
                    "lane_id": p.lane_id,
                    "lane_label": p.lane_label,
                    "title": p.title,
                    "height_pct": p.height_pct,
                }
                for p in self.panes
            ],
            "join_commands": [c.as_dict() for c in self.join_commands],
            "stamp_commands": [c.as_dict() for c in self.stamp_commands],
        }


def build_cockpit_adopt_plan(
    workspace: CockpitWorkspace,
    *,
    source_session: str,
    source_codex_pane: str,
    source_claude_pane: str,
    anchor_pane: str,
    column_index: int,
    codex_ratio: int = DEFAULT_CODEX_RATIO,
    session: str = COCKPIT_SESSION_DEFAULT,
) -> CockpitAdoptPlan:
    """Plan moving one normal session's codex/claude panes into the cockpit (#11898).

    ``source_codex_pane`` / ``source_claude_pane`` are the real ``%pane`` ids of
    the co-existing normal session (resolved by detection). ``anchor_pane`` is the
    real ``%pane`` id of the cockpit's visually rightmost codex column — the new
    column is joined to its right, mirroring :func:`build_cockpit_append_plan`'s
    full-height ``-h -f -l N%`` geometry so existing columns keep their vertical
    Codex/Claude split. ``join-pane`` preserves pane ids, so the moved panes are
    referenced by their original ids throughout (no token capture needed) and the
    identity re-stamp targets those same ids. Pure: returns a
    :class:`CockpitAdoptPlan`, runs no tmux.
    """
    if not source_codex_pane or not source_claude_pane:
        raise ValueError("adopt needs both source codex and claude pane ids")
    if source_codex_pane == source_claude_pane:
        raise ValueError("adopt source codex and claude panes must differ")
    if not anchor_pane:
        raise ValueError("adopt needs the cockpit anchor codex pane id")

    codex_ratio = normalize_ratio(codex_ratio)
    claude_ratio = 100 - codex_ratio
    even_share = even_column_share(column_index + 1)

    # Atomic joins: codex becomes a new full-height column to the right of the
    # cockpit, then claude is split in below it. join-pane MOVES the live pane
    # (agent intact) and keeps its %id, so later commands reference the original
    # ids directly.
    join_commands = (
        CockpitCommand(
            argv=(
                "join-pane", "-h", "-f", "-l", f"{even_share}%",
                "-s", source_codex_pane, "-t", anchor_pane,
            ),
            captures=None,
            purpose=f"adopt codex column {column_index} ({workspace.label})",
        ),
        CockpitCommand(
            argv=(
                "join-pane", "-v", "-l", f"{claude_ratio}%",
                "-s", source_claude_pane, "-t", source_codex_pane,
            ),
            captures=None,
            purpose=f"adopt claude under column {column_index} ({workspace.label})",
        ),
    )

    panes = (
        CockpitPane(
            token=source_codex_pane, column=column_index, role=ROLE_CODEX,
            workspace_id=workspace.workspace_id, label=workspace.label,
            repo_root=workspace.repo_root,
            title=_pane_title(workspace.label, ROLE_CODEX, workspace.codex_anchor),
            height_pct=codex_ratio, anchor=workspace.codex_anchor,
            lane_id=workspace.lane_id, lane_label=workspace.lane_label,
        ),
        CockpitPane(
            token=source_claude_pane, column=column_index, role=ROLE_CLAUDE,
            workspace_id=workspace.workspace_id, label=workspace.label,
            repo_root=workspace.repo_root,
            title=_pane_title(workspace.label, ROLE_CLAUDE, workspace.claude_anchor),
            height_pct=claude_ratio, anchor=workspace.claude_anchor,
            lane_id=workspace.lane_id, lane_label=workspace.lane_label,
        ),
    )
    stamp_commands: list[CockpitCommand] = []
    for pane in panes:
        stamp_commands.extend(_pane_identity_commands(pane))

    return CockpitAdoptPlan(
        session=session,
        window=COCKPIT_WINDOW,
        codex_ratio=codex_ratio,
        claude_ratio=claude_ratio,
        column_index=column_index,
        workspace_id=workspace.workspace_id,
        lane_id=normalize_lane(workspace.lane_id),
        lane_label=workspace.lane_label,
        source_session=source_session,
        source_codex_pane=source_codex_pane,
        source_claude_pane=source_claude_pane,
        anchor_pane=anchor_pane,
        panes=panes,
        join_commands=join_commands,
        stamp_commands=tuple(stamp_commands),
    )


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


# --- Cockpit reset / rebuild (Redmine #11814) ---------------------------------
#
# A stale or broken `mozyo-cockpit` session (the #11807 append-flatten regression
# left one that could not self-heal; the operator fell back to a manual
# `tmux kill-session`) needs a safe, first-class teardown UX so nobody reaches for
# raw `tmux kill-session` again. The hard rule (US #11814 safety boundary) is that
# the destructive `kill-session` only runs against a cockpit that is *proven* to be
# mozyo-managed — never decided by session name alone. Identity is read off the
# same machine-readable markers append/adopt already stamp (`@mozyo_workspace_id`
# on the cockpit-window panes); a same-named session that carries no such marker,
# or a session with no `cockpit` window at all, fails closed and is left untouched.
# This module stays pure: :func:`assess_cockpit_reset` grades a runtime snapshot
# and :func:`build_cockpit_reset_plan` emits the (single) kill command; the
# application layer reads tmux, previews, and — only with explicit confirm —
# executes. `reset` never adopts and never silently rebuilds; `rebuild` is reset
# composed with the normal create flow, decided in the application layer.

COCKPIT_RESET_ABSENT = "absent"  # no cockpit session at all — nothing to reset
COCKPIT_RESET_FOREIGN = "foreign"  # session present but no `cockpit` window — unconfirmable
COCKPIT_RESET_UNMANAGED = "unmanaged"  # cockpit window present but no mozyo-identified pane
COCKPIT_RESET_MANAGED = "managed"  # mozyo-identified cockpit — safe to reset


@dataclass(frozen=True)
class CockpitPaneIdentity:
    """One cockpit-window pane projected for the reset preview (#11814).

    ``managed`` is the load-bearing field: a pane is mozyo-managed only when it
    carries a non-empty ``@mozyo_workspace_id`` marker. The reset gate keys off
    the presence of *any* managed pane, never the session name — a stray shell
    pane an operator split into the cockpit window is reported (``managed`` False)
    but does not by itself make the session resettable or block it.
    """

    pane_id: str
    workspace_id: str
    role: str
    lane_id: str
    managed: bool

    def as_dict(self) -> dict:
        return {
            "pane_id": self.pane_id,
            "workspace_id": self.workspace_id,
            "role": self.role,
            "lane_id": self.lane_id,
            "managed": self.managed,
        }


@dataclass(frozen=True)
class CockpitResetTarget:
    """The graded reset target — what a reset/rebuild may (or may not) tear down (#11814).

    ``status`` is one of the ``COCKPIT_RESET_*`` grades. Only :data:`COCKPIT_RESET_MANAGED`
    with no attached client is :pyattr:`resettable`; every other state
    (``foreign`` / ``unmanaged`` / an attached managed cockpit) carries a
    ``blocked_reason`` and is left untouched (fail-closed). ``absent`` is a benign
    no-op (nothing to reset), not a block. ``windows`` / ``managed_panes`` /
    ``unmanaged_panes`` / ``attached_clients`` are the inventory the preview shows
    so the operator sees exactly what a confirmed kill would destroy.
    """

    session: str
    status: str
    session_present: bool
    has_cockpit_window: bool
    windows: tuple[str, ...]
    managed_panes: tuple[CockpitPaneIdentity, ...]
    unmanaged_panes: tuple[CockpitPaneIdentity, ...]
    attached_clients: tuple[str, ...]
    blocked_reason: Optional[str]

    @property
    def mozyo_identified(self) -> bool:
        """The session is a proven mozyo-managed cockpit (a kill plan is buildable)."""
        return self.status == COCKPIT_RESET_MANAGED

    @property
    def resettable(self) -> bool:
        """Safe to run the destructive teardown: mozyo-identified and not attached."""
        return self.status == COCKPIT_RESET_MANAGED and not self.attached_clients

    @property
    def absent(self) -> bool:
        return self.status == COCKPIT_RESET_ABSENT

    def as_dict(self) -> dict:
        return {
            "session": self.session,
            "status": self.status,
            "session_present": self.session_present,
            "has_cockpit_window": self.has_cockpit_window,
            "mozyo_identified": self.mozyo_identified,
            "resettable": self.resettable,
            "windows": list(self.windows),
            "managed_panes": [p.as_dict() for p in self.managed_panes],
            "unmanaged_panes": [p.as_dict() for p in self.unmanaged_panes],
            "attached_clients": list(self.attached_clients),
            "blocked_reason": self.blocked_reason,
        }


def assess_cockpit_reset(
    *,
    session: str,
    session_present: bool,
    columns: Optional[Sequence[Mapping[str, object]]],
    attached_clients: Sequence[str] = (),
    windows: Sequence[str] = (),
) -> CockpitResetTarget:
    """Grade a cockpit session for reset/rebuild from a runtime snapshot (#11814, pure).

    ``columns`` is the cockpit window's pane list (each a mapping with
    ``pane_id`` / ``workspace_id`` / ``role`` / ``lane_id`` — the shape
    :func:`mozyo_bridge.application.commands._read_cockpit_columns` returns), or
    ``None`` when the ``cockpit`` window does not exist. Grading is fail-closed:

    - ``columns is None``: the session either does not exist (:data:`COCKPIT_RESET_ABSENT`,
      a benign no-op) or exists without a cockpit window (:data:`COCKPIT_RESET_FOREIGN`)
      — in the latter case ownership cannot be confirmed from a window/marker, so the
      same-named session is left untouched rather than killed by name.
    - a cockpit window with **no** marker-carrying pane is :data:`COCKPIT_RESET_UNMANAGED`
      and left untouched.
    - a cockpit window with at least one ``@mozyo_workspace_id`` pane is
      :data:`COCKPIT_RESET_MANAGED`; it is resettable only when no client is attached
      (moving the rug out from a live client is fail-closed — detach first).
    """
    clients = tuple(c for c in attached_clients if c)
    wins = tuple(w for w in windows if w)

    if columns is None:
        if session_present:
            reason = (
                f"session {session!r} exists but has no `cockpit` window, so it "
                f"cannot be confirmed as the mozyo-managed cockpit. Refusing to "
                f"kill a session by name alone (fail-closed). Inspect it with "
                f"`tmux list-windows -t {session}` and, only if you are sure it is "
                f"stale, remove it manually with `tmux kill-session -t {session}`."
            )
            return CockpitResetTarget(
                session, COCKPIT_RESET_FOREIGN, True, False, wins, (), (), clients, reason
            )
        reason = f"no cockpit session {session!r} exists — nothing to reset."
        return CockpitResetTarget(
            session, COCKPIT_RESET_ABSENT, False, False, wins, (), (), clients, reason
        )

    managed: list[CockpitPaneIdentity] = []
    unmanaged: list[CockpitPaneIdentity] = []
    for col in columns:
        workspace_id = str(col.get("workspace_id") or "")
        ident = CockpitPaneIdentity(
            pane_id=str(col.get("pane_id") or ""),
            workspace_id=workspace_id,
            role=str(col.get("role") or ""),
            lane_id=normalize_lane(col.get("lane_id")),  # type: ignore[arg-type]
            managed=bool(workspace_id),
        )
        (managed if ident.managed else unmanaged).append(ident)

    if not managed:
        reason = (
            f"session {session!r} has a `cockpit` window but no pane carries the "
            f"mozyo identity marker (`@mozyo_workspace_id`), so it is not a "
            f"mozyo-managed cockpit. Refusing to reset it (fail-closed); inspect "
            f"it manually if you believe it is stale."
        )
        return CockpitResetTarget(
            session, COCKPIT_RESET_UNMANAGED, True, True, wins,
            (), tuple(unmanaged), clients, reason,
        )

    blocked: Optional[str] = None
    if clients:
        blocked = (
            f"cockpit session {session!r} has attached client(s) "
            f"({', '.join(clients)}); detach it first (close the iTerm2 -CC "
            f"window, or `tmux detach -s {session}`) so reset never tears it down "
            f"under a live client (fail-closed)."
        )
    return CockpitResetTarget(
        session, COCKPIT_RESET_MANAGED, True, True, wins,
        tuple(managed), tuple(unmanaged), clients, blocked,
    )


@dataclass(frozen=True)
class CockpitResetPlan:
    """The destructive teardown plan for a mozyo-managed cockpit (#11814).

    A single ``kill-session`` against the proven-managed cockpit session. Built
    only after :func:`assess_cockpit_reset` grades the target
    :data:`COCKPIT_RESET_MANAGED`, so the plan can never name a session whose
    ownership was not confirmed from a marker-carrying cockpit pane.
    """

    session: str
    commands: tuple[CockpitCommand, ...]

    def as_dict(self) -> dict:
        return {
            "session": self.session,
            "commands": [c.as_dict() for c in self.commands],
        }


def build_cockpit_reset_plan(
    session: str = COCKPIT_SESSION_DEFAULT,
) -> CockpitResetPlan:
    """Plan the teardown of the mozyo cockpit ``session`` (#11814, pure).

    Emits the single ``kill-session`` the confirm-gated executor runs. The caller
    must have graded the target :data:`COCKPIT_RESET_MANAGED` first; this builder
    does not re-check identity (it has no runtime to read).
    """
    if not session:
        raise ValueError("cockpit reset needs the cockpit session name")
    return CockpitResetPlan(
        session=session,
        commands=(
            CockpitCommand(
                argv=("kill-session", "-t", session),
                captures=None,
                purpose=f"reset cockpit session {session}",
            ),
        ),
    )
