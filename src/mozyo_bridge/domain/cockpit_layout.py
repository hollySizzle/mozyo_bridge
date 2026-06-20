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


def pane_identity_commands(
    *,
    pane_token: str,
    workspace_id: str,
    role: str,
    lane_id: str,
    lane_label: Optional[str] = None,
    title: Optional[str] = None,
) -> list["CockpitCommand"]:
    """Title (human-facing) + workspace/role/lane tmux user options (machine-readable).

    The single source of truth for stamping a cockpit pane's identity. Every
    cockpit construction (initial layout, append, adopt move, and the #12133
    peer-adopt of a role-less pane) routes through here so the option set —
    ``@mozyo_workspace_id`` / ``@mozyo_agent_role`` / ``@mozyo_lane_id`` (+ the
    optional human-facing ``@mozyo_lane_label``) — can never drift between
    construction paths. The lane id always lands (normalized to ``default``) so
    duplicate detection / Unit grouping can read ``workspace_id + lane_id`` off
    every cockpit pane; the human-facing lane label and the pane title are only
    stamped when provided.
    """
    commands: list[CockpitCommand] = []
    if title is not None:
        commands.append(
            CockpitCommand(
                argv=("select-pane", "-t", pane_token, "-T", title),
                captures=None,
                purpose=f"title {workspace_id} {role}",
            )
        )
    commands.extend(
        [
            CockpitCommand(
                argv=(
                    "set-option", "-p", "-t", pane_token,
                    WORKSPACE_OPTION, workspace_id,
                ),
                captures=None,
                purpose=f"mark workspace {workspace_id} ({role})",
            ),
            CockpitCommand(
                argv=("set-option", "-p", "-t", pane_token, ROLE_OPTION, role),
                captures=None,
                purpose=f"mark role {role} ({workspace_id})",
            ),
            CockpitCommand(
                argv=(
                    "set-option", "-p", "-t", pane_token,
                    LANE_OPTION, normalize_lane(lane_id),
                ),
                captures=None,
                purpose=f"mark lane {normalize_lane(lane_id)} ({workspace_id})",
            ),
        ]
    )
    if lane_label:
        commands.append(
            CockpitCommand(
                argv=(
                    "set-option", "-p", "-t", pane_token,
                    LANE_LABEL_OPTION, lane_label,
                ),
                captures=None,
                purpose=f"label lane {lane_label} ({workspace_id})",
            )
        )
    return commands


def _pane_identity_commands(pane: "CockpitPane") -> list["CockpitCommand"]:
    """Identity stamp for a planned :class:`CockpitPane` (delegates to the shared builder)."""
    return pane_identity_commands(
        pane_token=pane.token,
        workspace_id=pane.workspace_id,
        role=pane.role,
        lane_id=pane.lane_id,
        lane_label=pane.lane_label,
        title=pane.title,
    )


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
    window: str = COCKPIT_WINDOW,
    launch: Optional[Callable[[str, CockpitWorkspace], Optional[str]]] = None,
) -> CockpitPlan:
    """Plan appending ONE new column beside an existing cockpit column (Redmine #11803).

    ``anchor_pane`` is the real ``%pane`` id of the rightmost existing column's
    Codex pane; the new column is split to its right and widths re-equalized.
    ``column_index`` is the 0-based position of the new column (used only for
    the logical token names so they cannot collide with existing panes). The
    split targets ``anchor_pane`` directly, so the new column lands in whatever
    window holds the anchor — the shared `cockpit` window by default, or a
    Project Group window (#12330) when the anchor is a group-window pane;
    ``window`` is a display-only label for the returned plan. Pure.
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
        window=window,
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


# --- Per-Project-Group tmux window (Redmine #12330) ---------------------------
#
# Faithful execution of the `project_group_tmux_window` desired presentation
# (#12290 / #12302 visible-degrade follow-up): a Project Group can be laid out in
# its OWN tmux window in the cockpit session instead of a column in the shared
# `cockpit` window. The window is a *display* surface only — its NAME is never
# identity. Two distinct kinds of marker keep this faithful:
#
# - UNIT identity stays on the PANE options every cockpit construction stamps
#   (`@mozyo_workspace_id` / `@mozyo_agent_role` / `@mozyo_lane_id`), unchanged by
#   this feature. Duplicate detection / focus / target resolution read
#   `workspace_id + lane_id` off the pane options regardless of which window holds
#   the pane (the application layer scans every managed window). Daemon /
#   `agents targets` / pane-identity gate semantics are therefore unchanged —
#   the window is display metadata only.
# - GROUP membership of a window rides on a *window-level* `@mozyo_group_id`
#   option that the create plan stamps deterministically from the resolved
#   group id. The launcher locates a group's existing window by this mozyo-written
#   marker — NOT by the window name (names are never trusted as identity) and not
#   by a pane identity option (the group is display grouping, not Unit identity).
#
# The window / iTerm tab / OS window is never routing / approval / review / close
# authority (`unit-target-model.md` `#### Project Group tmux-window presentation`).

# Window-level marker naming the Project Group a managed cockpit window was
# created for. Mozyo-written and deterministic, so the launcher may locate a
# group's window by it; it is NOT Unit identity (that stays on pane options) and
# NOT the window name (never trusted).
GROUP_WINDOW_OPTION = "@mozyo_group_id"

# tmux window names are a flat display string; a name carrying control / quoting
# characters (newline, tab, `:` window-target separator, `.` pane separator,
# quotes) can confuse `list-windows -F` parsing and target resolution. Group
# labels come from a closed display-only config, but sanitize defensively so a
# window name can never inject a target separator.
_WINDOW_NAME_UNSAFE = set("\r\n\t:.\"'`$")


def sanitize_group_window_name(name: Optional[str]) -> str:
    """A public-safe tmux window name for a Project Group window (#12330, pure).

    Collapses whitespace, strips characters that could break a `session:window`
    target or `list-windows -F` parsing, and falls back to ``group`` when the
    result is empty. Display only — the name is never identity: discovery lists
    and reads windows by their tmux ``#{window_id}`` (see
    :func:`mozyo_bridge.application.commands._read_managed_cockpit_windows`), so a
    collision between two groups' sanitized names is harmless — two windows named
    the same stay distinct by id and each keeps its own ``@mozyo_group_id`` marker
    (#12330 review j#62380).
    """
    raw = (name or "").strip()
    cleaned = "".join(
        (" " if ch.isspace() else ch) for ch in raw if ch not in _WINDOW_NAME_UNSAFE
    )
    cleaned = " ".join(cleaned.split())  # collapse internal whitespace runs
    return cleaned or "group"


def build_group_window_create_plan(
    workspace: CockpitWorkspace,
    *,
    group_id: Optional[str],
    window_name: str,
    codex_ratio: int = DEFAULT_CODEX_RATIO,
    session: str = COCKPIT_SESSION_DEFAULT,
    launch: Optional[Callable[[str, CockpitWorkspace], Optional[str]]] = None,
) -> CockpitPlan:
    """Plan a NEW per-Project-Group tmux window seeded with ``workspace`` (#12330).

    Adds one window (named ``window_name``, a display-only label) to the existing
    cockpit ``session`` and lays the workspace's Codex-top / Claude-bottom pair in
    it, with the identical identity stamping the shared cockpit uses. The caller
    must have confirmed the cockpit ``session`` already exists (group windows are
    additive to a live session; session bootstrap stays the behavior-preserving
    `cockpit` window). Pure: returns a :class:`CockpitPlan`, runs no tmux.

    Rollback is the caller's `execute_cockpit_plan(..., cleanup_captured=True)`:
    killing every captured pane empties the fresh window, and tmux drops a window
    with no panes — so a mid-create failure never orphans a half-built window.
    """
    if not window_name:
        raise ValueError("group window create needs a window name")

    codex_ratio = normalize_ratio(codex_ratio)
    claude_ratio = 100 - codex_ratio
    codex_token = "@grp_codex"
    claude_token = "@grp_claude"
    commands: list[CockpitCommand] = []

    def _launch(role: str) -> Optional[str]:
        return launch(role, workspace) if launch is not None else None

    codex_argv = ["new-window", "-t", session, "-n", window_name]
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
            purpose=f"create group window {window_name!r} codex ({workspace.label})",
        )
    )
    claude_argv = ["split-window", "-v", "-t", codex_token, "-l", f"{claude_ratio}%"]
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
            purpose=f"create group window {window_name!r} claude ({workspace.label})",
        )
    )

    panes = (
        CockpitPane(
            token=codex_token, column=0, role=ROLE_CODEX,
            workspace_id=workspace.workspace_id, label=workspace.label,
            repo_root=workspace.repo_root,
            title=_pane_title(workspace.label, ROLE_CODEX, workspace.codex_anchor),
            height_pct=codex_ratio, anchor=workspace.codex_anchor,
            lane_id=workspace.lane_id, lane_label=workspace.lane_label,
        ),
        CockpitPane(
            token=claude_token, column=0, role=ROLE_CLAUDE,
            workspace_id=workspace.workspace_id, label=workspace.label,
            repo_root=workspace.repo_root,
            title=_pane_title(workspace.label, ROLE_CLAUDE, workspace.claude_anchor),
            height_pct=claude_ratio, anchor=workspace.claude_anchor,
            lane_id=workspace.lane_id, lane_label=workspace.lane_label,
        ),
    )
    for pane in panes:
        commands.extend(_pane_identity_commands(pane))

    # Display-only window hint (group id). Stamped on the codex pane's WINDOW, not
    # the pane identity option set — discovery never trusts it. Skipped when there
    # is no group id (the implicit per-repo default group).
    if group_id:
        commands.append(
            CockpitCommand(
                argv=("set-option", "-w", "-t", codex_token, GROUP_WINDOW_OPTION, group_id),
                captures=None,
                purpose=f"hint group window {window_name!r} -> group {group_id}",
            )
        )

    return CockpitPlan(
        session=session,
        window=window_name,
        codex_ratio=codex_ratio,
        claude_ratio=claude_ratio,
        columns=1,
        panes=panes,
        commands=tuple(commands),
    )


def build_group_window_focus_plan(
    target_pane: str, *, session: str = COCKPIT_SESSION_DEFAULT
) -> CockpitPlan:
    """Plan focusing an already-present pane in ANY cockpit-session window (#12330).

    Unlike :func:`build_cockpit_focus_plan` (which selects the fixed `cockpit`
    window), this selects the window that *contains* ``target_pane`` — a pane id
    is an unambiguous tmux target that resolves to its own window — so a duplicate
    already laid out in a Project Group window is focused there instead of being
    re-appended. No panes are created. ``session`` is accepted for parity / display
    but the pane id alone targets the right window.
    """
    if not target_pane:
        raise ValueError("focus needs the target pane id")
    commands = (
        CockpitCommand(
            argv=("select-window", "-t", target_pane),
            captures=None,
            purpose=f"focus window containing pane {target_pane}",
        ),
        CockpitCommand(
            argv=("select-pane", "-t", target_pane),
            captures=None,
            purpose=f"focus existing pane {target_pane}",
        ),
    )
    return CockpitPlan(
        session=session,
        window="",
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
    with a **known-empty** attached-client set is :pyattr:`resettable`; every other
    state (``foreign`` / ``unmanaged`` / an attached managed cockpit / a managed
    cockpit whose client state could not be read) carries a ``blocked_reason`` and
    is left untouched (fail-closed). ``absent`` is a benign no-op (nothing to
    reset), not a block. ``attached_clients_known`` is ``False`` when the client
    read failed — an *unknown* client state is fail-closed for a destructive
    teardown, never treated as "no client attached" (Redmine #11814 review j#57928).
    ``windows`` / ``managed_panes`` / ``unmanaged_panes`` / ``attached_clients``
    are the inventory the preview shows so the operator sees exactly what a
    confirmed kill would destroy.
    """

    session: str
    status: str
    session_present: bool
    has_cockpit_window: bool
    windows: tuple[str, ...]
    managed_panes: tuple[CockpitPaneIdentity, ...]
    unmanaged_panes: tuple[CockpitPaneIdentity, ...]
    attached_clients: tuple[str, ...]
    attached_clients_known: bool
    blocked_reason: Optional[str]

    @property
    def mozyo_identified(self) -> bool:
        """The session is a proven mozyo-managed cockpit (a kill plan is buildable)."""
        return self.status == COCKPIT_RESET_MANAGED

    @property
    def resettable(self) -> bool:
        """Safe to run the destructive teardown: mozyo-identified, with a
        known-empty client set (an unreadable client state is fail-closed)."""
        return (
            self.status == COCKPIT_RESET_MANAGED
            and self.attached_clients_known
            and not self.attached_clients
        )

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
            "attached_clients_known": self.attached_clients_known,
            "blocked_reason": self.blocked_reason,
        }


def assess_cockpit_reset(
    *,
    session: str,
    session_present: bool,
    columns: Optional[Sequence[Mapping[str, object]]],
    attached_clients: Sequence[str] = (),
    attached_clients_known: bool = True,
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
      :data:`COCKPIT_RESET_MANAGED`; it is resettable only when the client state was
      read successfully **and** is empty. ``attached_clients_known=False`` means the
      client read failed — an *unknown* client state is fail-closed for a destructive
      teardown (it is never treated as "no client attached"), so a managed cockpit
      with an unreadable client state blocks rather than killing (Redmine #11814
      review j#57928). An attached client likewise blocks — detach first.
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
                session, COCKPIT_RESET_FOREIGN, True, False, wins, (), (),
                clients, attached_clients_known, reason,
            )
        reason = f"no cockpit session {session!r} exists — nothing to reset."
        return CockpitResetTarget(
            session, COCKPIT_RESET_ABSENT, False, False, wins, (), (),
            clients, attached_clients_known, reason,
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
            (), tuple(unmanaged), clients, attached_clients_known, reason,
        )

    # Managed cockpit: the destructive gate. An *unknown* client state (the read
    # failed) is fail-closed first — never silently treated as "no client" — then
    # an actually-attached client blocks. Only a known-empty client set is
    # resettable (Redmine #11814 review j#57928).
    blocked: Optional[str] = None
    if not attached_clients_known:
        blocked = (
            f"cannot determine whether a client is attached to cockpit session "
            f"{session!r} (tmux `list-clients` could not be read); refusing to "
            f"tear it down while the client state is unknown (fail-closed). Retry, "
            f"or detach the cockpit and re-run."
        )
    elif clients:
        blocked = (
            f"cockpit session {session!r} has attached client(s) "
            f"({', '.join(clients)}); detach it first (close the iTerm2 -CC "
            f"window, or `tmux detach -s {session}`) so reset never tears it down "
            f"under a live client (fail-closed)."
        )
    return CockpitResetTarget(
        session, COCKPIT_RESET_MANAGED, True, True, wins,
        tuple(managed), tuple(unmanaged), clients, attached_clients_known, blocked,
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




# --- Cockpit column width rebalance (Redmine #12135) --------------------------
#
# An existing cockpit accumulates column-width skew: a manual `resize-pane`, an
# append from before the #11854 fair-share sizing landed, or an external tmux
# integration can leave one column ballooned while others starve (the live
# `56 / 50 / 99 / 69` skew US #12135 targets, #11854 j#57547 residual gap).
# `mozyo cockpit rebalance` restores the live columns toward an EQUAL fair-share
# width with a preview-first, confirm-gated plan of `resize-pane -x` commands.
#
# The column model is the tmux *window layout tree's top-level cells* — NOT an
# x-overlap geometry cluster. The live `doctor-geometry` clustering can read a
# structurally-drifted cockpit (a 2x2 grid where two Units share one tmux cell)
# as if it were clean columns; resizing such a cluster's pane only moves an inner
# sub-split boundary and corrupts the layout. The layout tree is the single source
# of truth for which boundaries are resizable, so rebalance parses it and fails
# closed when a top-level column is not a clean, full-width-resizable cell
# (deferring that structural repair to `mozyo cockpit reconcile` / #12136, never
# half-resizing it).
#
# Boundaries (`pane-centric-cockpit-semantics.md`):
# - Observed geometry is the ONLY thing rebalance reads and writes: it equalizes
#   the live top-level cell widths and never re-decides Unit / role / lane
#   identity, so it emits NO `set-option` — identity pane options stay exactly as
#   they are.
# - It changes column *width* only via `resize-pane -x` and never runs
#   `select-layout even-horizontal`, so each column keeps its vertical
#   Codex/Claude split (which even-horizontal would flatten — the #11807
#   regression :func:`build_cockpit_append_plan` also avoids).
# - Equal fair-share is the interim target: per-column ``width_weight`` from the
#   desired-presentation table (`unit-presentation-state-db.md`) is future scope;
#   until it lands, rebalance mirrors :func:`even_column_share`'s equal policy
#   that #11854 already sizes appended columns to.

# A column is "off" — worth a resize — only when its width deviates from its
# fair-share target by MORE than this many cells; within tolerance a column is
# treated as already balanced (a 1-cell rounding difference is not skew).
DEFAULT_REBALANCE_TOLERANCE = 1


def fair_share_widths(total_content: int, columns: int) -> list[int]:
    """Split ``total_content`` cells across ``columns`` as evenly as possible (pure).

    The base share is ``total_content // columns``; the leftover cells
    (``total_content % columns``) are handed to the leftmost columns one each, so
    the resulting widths differ by at most one cell and sum back to
    ``total_content`` exactly. The inter-column borders are not part of a pane
    width and a `resize-pane` never moves them, so conserving the content total is
    what keeps the window width unchanged while the columns are equalized.
    """
    n = max(1, int(columns))
    total = max(0, int(total_content))
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


@dataclass(frozen=True)
class LayoutCell:
    """One node of a parsed tmux ``window_layout`` tree (observed geometry, pure).

    A leaf carries a ``pane_id`` (``%``-prefixed) and no children; a split carries
    ``orient`` (``"h"`` for a left/right ``{...}`` split, ``"v"`` for a top/bottom
    ``[...]`` split) and ``children``. ``width`` / ``height`` / ``x`` / ``y`` are
    the observed cell rectangle in tmux cells.
    """

    width: int
    height: int
    x: int
    y: int
    pane_id: Optional[str]
    orient: Optional[str]
    children: tuple["LayoutCell", ...]

    @property
    def is_leaf(self) -> bool:
        return self.pane_id is not None

    def leaves(self) -> tuple["LayoutCell", ...]:
        if self.is_leaf:
            return (self,)
        out: list[LayoutCell] = []
        for child in self.children:
            out.extend(child.leaves())
        return tuple(out)


@dataclass(frozen=True)
class LayoutColumn:
    """One top-level cockpit column projected from the tmux layout tree (#12135).

    ``target_pane`` is the resize target — the column's first (top-left) leaf
    pane. ``clean`` is the load-bearing field: a column is cleanly resizable only
    when that leaf spans the FULL column width (a single pane, or a vertical
    Codex/Claude split whose panes share the column x-range). A column whose first
    leaf is narrower than the column carries a horizontal sub-split (a
    structural layout-tree drift; #12136 `cockpit reconcile` scope), so resizing
    that leaf would move an inner boundary, not the column — rebalance fails closed
    on it.
    """

    index: int
    width: int
    x: int
    target_pane: str
    pane_ids: tuple[str, ...]
    clean: bool

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "width": self.width,
            "x": self.x,
            "target_pane": self.target_pane,
            "pane_ids": list(self.pane_ids),
            "clean": self.clean,
        }


def _read_uint(text: str, i: int) -> tuple[int, int]:
    start = i
    while i < len(text) and text[i].isdigit():
        i += 1
    return int(text[start:i]), i


def _parse_layout_cell(text: str, i: int) -> tuple[LayoutCell, int]:
    """Parse one ``WxH,X,Y[,pane|{...}|[...]]`` node at ``text[i:]`` (pure)."""
    width, i = _read_uint(text, i)
    if i >= len(text) or text[i] != "x":
        raise ValueError(f"bad layout cell at {i}: expected 'x'")
    i += 1
    height, i = _read_uint(text, i)
    if i >= len(text) or text[i] != ",":
        raise ValueError(f"bad layout cell at {i}: expected ',' after height")
    x, i = _read_uint(text, i + 1)
    if i >= len(text) or text[i] != ",":
        raise ValueError(f"bad layout cell at {i}: expected ',' after x")
    y, i = _read_uint(text, i + 1)

    if i < len(text) and text[i] in "{[":
        opener = text[i]
        orient = "h" if opener == "{" else "v"
        closer = "}" if opener == "{" else "]"
        i += 1
        children: list[LayoutCell] = []
        while True:
            child, i = _parse_layout_cell(text, i)
            children.append(child)
            if i < len(text) and text[i] == ",":
                i += 1
                continue
            if i < len(text) and text[i] == closer:
                i += 1
                break
            break
        return (
            LayoutCell(width, height, x, y, None, orient, tuple(children)),
            i,
        )

    # Leaf: ``,<pane_number>`` — tmux writes a bare number; the ``%`` id is
    # ``%`` + that number.
    if i >= len(text) or text[i] != ",":
        raise ValueError(f"bad layout leaf at {i}: expected ',' before pane id")
    pane_num, i = _read_uint(text, i + 1)
    return (
        LayoutCell(width, height, x, y, f"%{pane_num}", None, ()),
        i,
    )


def parse_window_layout(layout: str) -> Optional[LayoutCell]:
    """Parse a tmux ``window_layout`` string into a :class:`LayoutCell` tree (pure).

    Strips the optional leading ``<checksum>,`` tmux prefixes, then parses the
    nested ``WxH,X,Y`` grammar (``{...}`` = left/right split, ``[...]`` =
    top/bottom split, ``,<n>`` = a leaf pane). Returns ``None`` for an empty or
    unparseable string so the caller degrades to "nothing to rebalance" rather
    than raising on a malformed read.
    """
    text = (layout or "").strip()
    if not text:
        return None
    # Drop a leading hex checksum (`a3cd,`) if present — the body starts at the
    # first `WxH` group.
    head, sep, rest = text.partition(",")
    if sep and head and all(c in "0123456789abcdefABCDEF" for c in head) and (
        "x" in rest.partition(",")[0]
    ):
        text = rest
    try:
        root, _ = _parse_layout_cell(text, 0)
    except (ValueError, IndexError):
        return None
    return root


def top_level_columns(root: Optional[LayoutCell]) -> tuple[LayoutColumn, ...]:
    """Project a parsed layout tree into its left-to-right top-level columns (#12135).

    When the root is a left/right (``{...}``) split, its direct children are the
    columns; otherwise the whole window is a single column. Each column's
    ``target_pane`` is its first (top-left) leaf and ``clean`` records whether
    that leaf spans the full column width (so a `resize-pane -x` on it moves the
    column boundary rather than an inner sub-split).
    """
    if root is None:
        return ()
    cells = root.children if root.orient == "h" else (root,)
    columns: list[LayoutColumn] = []
    for index, cell in enumerate(cells):
        leaves = cell.leaves()
        first = leaves[0] if leaves else None
        columns.append(
            LayoutColumn(
                index=index,
                width=cell.width,
                x=cell.x,
                target_pane=first.pane_id if first else "",
                pane_ids=tuple(leaf.pane_id for leaf in leaves if leaf.pane_id),
                clean=bool(first and first.width == cell.width),
            )
        )
    return tuple(columns)


@dataclass(frozen=True)
class RebalanceColumn:
    """One top-level column with its observed and fair-share target width (#12135)."""

    index: int
    target_pane: str
    pane_ids: tuple[str, ...]
    current_width: int
    target_width: int
    clean: bool

    @property
    def delta(self) -> int:
        return self.target_width - self.current_width

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "target_pane": self.target_pane,
            "pane_ids": list(self.pane_ids),
            "current_width": self.current_width,
            "target_width": self.target_width,
            "delta": self.delta,
            "clean": self.clean,
        }


@dataclass(frozen=True)
class CockpitRebalancePlan:
    """Plan to equalize the live cockpit's top-level column widths (#12135, pure).

    ``drift`` is ``True`` when a top-level column is not cleanly resizable (a
    structural layout-tree drift; #12136 `cockpit reconcile` scope); then
    ``blocked_reason`` is set, ``commands`` is empty, and the confirm path refuses
    to mutate. ``balanced`` is ``True``
    when every column is already within ``tolerance`` of its fair share (or there
    are fewer than two columns) — also an empty-``commands`` no-op. Otherwise
    ``commands`` is the ordered `resize-pane -x` sequence, left to right, sizing
    every column EXCEPT the last to its fair-share width; the rightmost absorbs
    the remainder, so N columns need only N-1 commands. No identity option is
    touched and no layout is flattened.
    """

    session: str
    window: str
    columns: tuple[RebalanceColumn, ...]
    total_content_width: int
    tolerance: int
    balanced: bool
    drift: bool
    blocked_reason: Optional[str]
    commands: tuple[CockpitCommand, ...]

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def as_dict(self) -> dict:
        return {
            "session": self.session,
            "window": self.window,
            "column_count": self.column_count,
            "total_content_width": self.total_content_width,
            "tolerance": self.tolerance,
            "balanced": self.balanced,
            "drift": self.drift,
            "blocked_reason": self.blocked_reason,
            "columns": [c.as_dict() for c in self.columns],
            "commands": [c.as_dict() for c in self.commands],
        }


def build_cockpit_rebalance_plan(
    columns: Sequence[LayoutColumn],
    *,
    session: str = COCKPIT_SESSION_DEFAULT,
    tolerance: int = DEFAULT_REBALANCE_TOLERANCE,
) -> CockpitRebalancePlan:
    """Plan an equal fair-share width rebalance of the cockpit's columns (#12135).

    ``columns`` is the top-level column projection :func:`top_level_columns`
    derives from the live ``window_layout`` tree. Pure: returns a
    :class:`CockpitRebalancePlan`, runs no tmux.

    Fail-closed on structural drift: if ANY column is not cleanly resizable
    (``LayoutColumn.clean`` is ``False`` — a horizontal sub-split crams two Units
    into one tmux cell), the plan is :pyattr:`CockpitRebalancePlan.drift` with a
    ``blocked_reason`` and NO commands; that structural repair is `mozyo cockpit
    reconcile` / #12136 scope, and half-resizing it would corrupt the layout. The
    fair share otherwise keeps the total column *content* width constant (borders
    are not pane width and a resize never moves them), so the columns are
    redistributed evenly without resizing the window; columns are resized left to
    right and the rightmost absorbs the remainder.
    """
    ordered = sorted(columns, key=lambda c: (c.x, c.index))
    n = len(ordered)
    total_content = sum(c.width for c in ordered)

    dirty = [c for c in ordered if not c.clean]
    if dirty:
        names = ", ".join(
            f"column {c.index} (pane {c.target_pane or '?'})" for c in dirty
        )
        reason = (
            f"cockpit {session!r} has a structurally drifted column that is not a "
            f"clean full-width split ({names}); its tmux cell carries a horizontal "
            f"sub-split (a 2x2 / mixed-Unit layout-tree drift), so a width "
            f"resize would move an inner boundary and corrupt the layout. Resolve "
            f"the structure first with `mozyo cockpit reconcile` (#12136), "
            f"then rebalance. (rebalance only equalizes width; it does not repair "
            f"structure.)"
        )
        rebalance_cols = tuple(
            RebalanceColumn(
                index=c.index,
                target_pane=c.target_pane,
                pane_ids=c.pane_ids,
                current_width=c.width,
                target_width=c.width,
                clean=c.clean,
            )
            for c in ordered
        )
        return CockpitRebalancePlan(
            session=session,
            window=COCKPIT_WINDOW,
            columns=rebalance_cols,
            total_content_width=total_content,
            tolerance=tolerance,
            balanced=False,
            drift=True,
            blocked_reason=reason,
            commands=(),
        )

    targets = fair_share_widths(total_content, n) if n else []
    rebalance_cols = []
    balanced = True
    for i, col in enumerate(ordered):
        target = targets[i] if i < len(targets) else col.width
        rebalance_cols.append(
            RebalanceColumn(
                index=col.index,
                target_pane=col.target_pane,
                pane_ids=col.pane_ids,
                current_width=col.width,
                target_width=target,
                clean=col.clean,
            )
        )
        if abs(target - col.width) > tolerance:
            balanced = False
    if n < 2:
        balanced = True

    commands: list[CockpitCommand] = []
    if not balanced:
        # Resize every column except the rightmost; the last absorbs the
        # remainder. `resize-pane -x` sets an absolute cell width and moves only
        # the column border, so the vertical Codex/Claude split is preserved and
        # no identity option is touched.
        for col in rebalance_cols[:-1]:
            if not col.target_pane:
                continue
            commands.append(
                CockpitCommand(
                    argv=(
                        "resize-pane", "-t", col.target_pane,
                        "-x", str(col.target_width),
                    ),
                    captures=None,
                    purpose=(
                        f"rebalance column {col.index} to {col.target_width} cells"
                    ),
                )
            )

    return CockpitRebalancePlan(
        session=session,
        window=COCKPIT_WINDOW,
        columns=tuple(rebalance_cols),
        total_content_width=total_content,
        tolerance=tolerance,
        balanced=balanced,
        drift=False,
        blocked_reason=None,
        commands=tuple(commands),
    )


# --- Cockpit structural reconcile (Redmine #12136) ----------------------------
#
# After #12133 peer-adopt resolved the missing/role-less identity, the live
# cockpit can still carry a *structural* layout-tree drift: two Unit columns
# nested inside ONE tmux top-level cell as a 2x2 grid (the live
# `[ {%1104|%953}, {%1106|%954} ]` case). `doctor-geometry`'s x-cluster diagnosis
# reports `ok` for it, but #12135 rebalance correctly fails closed because a width
# resize would move an inner sub-split boundary. `mozyo cockpit reconcile` repairs
# that structure so every Unit becomes its own clean top-level column and #12135
# rebalance can then run.
#
# Mechanism (verified in scratch tmux, #12136 j#59853/j#59857): order-preserving
# `swap-pane` + a checksum-valid `select-layout`. `select-layout` assigns the
# window's CURRENT panes to the target layout's leaves in pane order, ignoring the
# ids written in the layout string (scratch-confirmed). So reconcile first emits
# `swap-pane` commands that sort the live pane order into the desired column-major
# order (Unit0 codex, Unit0 claude, Unit1 codex, ...), then one `select-layout`
# with a hand-built, checksum-valid layout that lays every Unit out as a clean
# `[codex/claude]` top-level column in the SAME left-to-right order the operator
# already sees. No pane is killed and identity options ride with the panes.
#
# Boundaries (`pane-centric-cockpit-semantics.md`, #12136 acceptance):
# - Unit identity is read from pane options, NEVER inferred from geometry;
#   geometry (`pane_left`) only ORDERS Units left-to-right for display.
# - No pane kill. `swap-pane` / `select-layout` move/relayout live panes; identity
#   rides with them (no re-stamp).
# - Fails closed on an unidentified (role-less) pane (#12133 identity-adoption
#   scope) or a Unit split across more than one top-level cell (a different drift).
# - Column widths are set to an even fair share (a valid `select-layout` must
#   specify widths); #12135 rebalance / future `width_weight` refine width.


def layout_checksum(body: str) -> int:
    """tmux ``window_layout`` checksum of a layout ``body`` (the part after ``csum,``).

    Mirrors tmux's ``layout_checksum`` (layout-custom.c): a 16-bit rotate-add over
    the body bytes. ``select-layout`` rejects a layout whose ``%04x`` checksum
    prefix does not match, so reconcile computes it here (pure) to build a valid
    custom layout string.
    """
    csum = 0
    for ch in body:
        csum = ((csum >> 1) + ((csum & 1) << 15)) & 0xFFFF
        csum = (csum + ord(ch)) & 0xFFFF
    return csum


def format_custom_layout(body: str) -> str:
    """Prefix a layout ``body`` with its tmux checksum: ``<csum>,<body>`` (pure)."""
    return f"{layout_checksum(body):04x},{body}"


def _pane_num(pane_id: str) -> str:
    """tmux layout strings carry the bare pane number (no leading ``%``)."""
    return pane_id[1:] if pane_id.startswith("%") else pane_id


@dataclass(frozen=True)
class ReconcileUnit:
    """One Unit projected for reconcile, with its panes by role (#12136).

    Identity (``workspace_id`` / ``lane_id``) is from pane options; ``codex_pane``
    / ``claude_pane`` are ``""`` when that role is absent (a missing peer, #12133
    scope, kept as a single-pane column rather than adopted). ``min_x`` is the
    leftmost observed x, used only to order Units left-to-right.
    """

    workspace_id: str
    lane_id: str
    codex_pane: str
    claude_pane: str
    pane_ids: tuple[str, ...]
    min_x: int
    cell_index: int

    @property
    def ordered_panes(self) -> tuple[str, ...]:
        """The Unit's panes top-to-bottom: codex over claude (present roles only)."""
        out = []
        if self.codex_pane:
            out.append(self.codex_pane)
        if self.claude_pane:
            out.append(self.claude_pane)
        return tuple(out)

    def as_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "codex_pane": self.codex_pane,
            "claude_pane": self.claude_pane,
            "pane_ids": list(self.pane_ids),
            "min_x": self.min_x,
            "cell_index": self.cell_index,
        }


@dataclass(frozen=True)
class ReconcileCell:
    """One top-level tmux cell projected for reconcile (#12136).

    ``tangled`` (more than one Unit in the cell) is the structural drift reconcile
    repairs. ``unidentified_panes`` are leaves with no Unit identity; their
    presence blocks reconcile (identity adoption is the `cockpit adopt` flow, not
    structural reconcile).
    """

    index: int
    x: int
    width: int
    pane_ids: tuple[str, ...]
    unit_keys: tuple[tuple[str, str], ...]
    unidentified_panes: tuple[str, ...]

    @property
    def tangled(self) -> bool:
        return len(self.unit_keys) > 1

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "x": self.x,
            "width": self.width,
            "pane_ids": list(self.pane_ids),
            "unit_keys": [list(k) for k in self.unit_keys],
            "unidentified_panes": list(self.unidentified_panes),
            "tangled": self.tangled,
        }


@dataclass(frozen=True)
class CockpitReconcilePlan:
    """Plan to flatten nested top-level cells into per-Unit columns (#12136, pure).

    ``drift`` is ``True`` when a top-level cell is tangled (more than one Unit).
    ``blocked_reason`` is set (and ``commands`` empty) when reconcile cannot safely
    proceed — an unidentified pane (identity-adoption / `cockpit adopt` scope), a
    duplicate same-role pane, or a Unit split across more than
    one top-level cell. ``clean`` is ``True`` when every top-level cell already
    maps to exactly one Unit (a benign no-op). When ``drift``, ``swap_commands``
    sort the live pane order and ``layout_command`` applies the checksum-valid
    ``target_layout`` (one clean ``[codex/claude]`` column per Unit, original
    left-to-right order). No command kills a pane.
    """

    session: str
    window: str
    codex_ratio: int
    claude_ratio: int
    cells: tuple[ReconcileCell, ...]
    units_in_order: tuple[tuple[str, str], ...]
    drift: bool
    clean: bool
    blocked_reason: Optional[str]
    swap_commands: tuple[CockpitCommand, ...]
    layout_command: Optional[CockpitCommand]
    target_layout: Optional[str]

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    @property
    def commands(self) -> tuple[CockpitCommand, ...]:
        """All planned commands in execution order: swaps, then the relayout."""
        if self.layout_command is None:
            return self.swap_commands
        return self.swap_commands + (self.layout_command,)

    def as_dict(self) -> dict:
        return {
            "session": self.session,
            "window": self.window,
            "codex_ratio": self.codex_ratio,
            "claude_ratio": self.claude_ratio,
            "cell_count": self.cell_count,
            "units_in_order": [list(k) for k in self.units_in_order],
            "drift": self.drift,
            "clean": self.clean,
            "blocked_reason": self.blocked_reason,
            "target_layout": self.target_layout,
            "swap_commands": [c.as_dict() for c in self.swap_commands],
            "layout_command": (
                self.layout_command.as_dict()
                if self.layout_command is not None
                else None
            ),
            "commands": [c.as_dict() for c in self.commands],
        }


def _collect_reconcile_units(
    root: Optional[LayoutCell],
    identity: Mapping[str, Mapping[str, object]],
) -> tuple[list[ReconcileCell], list[ReconcileUnit], list[str], list[str]]:
    """Project top-level cells + their Units from the layout tree + pane identity.

    Returns ``(cells, units, unidentified_panes, duplicate_role_panes)``.
    """
    if root is None:
        return [], [], [], []
    cells_in = root.children if root.orient == "h" else (root,)
    cells: list[ReconcileCell] = []
    units: list[ReconcileUnit] = []
    all_unidentified: list[str] = []
    duplicates: list[str] = []
    for index, cell in enumerate(cells_in):
        leaves = cell.leaves()
        by_unit: dict[tuple[str, str], dict] = {}
        order: list[tuple[str, str]] = []
        unidentified: list[str] = []
        for leaf in leaves:
            pane_id = leaf.pane_id or ""
            info = identity.get(pane_id) or {}
            ws = str(info.get("workspace_id") or "")
            role = str(info.get("role") or "")
            lane = normalize_lane(info.get("lane_id"))  # type: ignore[arg-type]
            if not ws or role not in ROLES:
                unidentified.append(pane_id)
                all_unidentified.append(pane_id)
                continue
            key = (ws, lane)
            bucket = by_unit.get(key)
            if bucket is None:
                bucket = {"codex": "", "claude": "", "panes": [], "min_x": leaf.x}
                by_unit[key] = bucket
                order.append(key)
            if bucket[role]:
                # A Unit must have at most one pane per role; a second codex (or
                # second claude) is ambiguous — record it so reconcile blocks
                # rather than silently dropping a pane.
                duplicates.append(pane_id)
            else:
                bucket[role] = pane_id
            bucket["panes"].append(pane_id)
            bucket["min_x"] = min(bucket["min_x"], leaf.x)
        order.sort(key=lambda k: (by_unit[k]["min_x"], k[0], k[1]))
        cells.append(
            ReconcileCell(
                index=index,
                x=cell.x,
                width=cell.width,
                pane_ids=tuple(leaf.pane_id for leaf in leaves if leaf.pane_id),
                unit_keys=tuple(order),
                unidentified_panes=tuple(unidentified),
            )
        )
        for key in order:
            units.append(
                ReconcileUnit(
                    workspace_id=key[0],
                    lane_id=key[1],
                    codex_pane=by_unit[key]["codex"],
                    claude_pane=by_unit[key]["claude"],
                    pane_ids=tuple(by_unit[key]["panes"]),
                    min_x=by_unit[key]["min_x"],
                    cell_index=index,
                )
            )
    return cells, units, all_unidentified, duplicates


def plan_pane_swaps(
    current_order: Sequence[str], desired_order: Sequence[str]
) -> list[tuple[str, str]]:
    """Selection-sort ``current_order`` into ``desired_order`` as ``swap-pane`` pairs.

    Each returned ``(a, b)`` is a ``swap-pane -s a -t b`` that exchanges the two
    panes' positions; applying them in order transforms the live pane order into
    ``desired_order`` (pure — the caller emits the tmux commands). Assumes both
    sequences are permutations of the same pane set.
    """
    cur = list(current_order)
    swaps: list[tuple[str, str]] = []
    for i, want in enumerate(desired_order):
        if cur[i] == want:
            continue
        j = cur.index(want, i + 1)
        swaps.append((cur[i], cur[j]))
        cur[i], cur[j] = cur[j], cur[i]
    return swaps


def build_unit_columns_layout(
    units_in_order: Sequence[ReconcileUnit],
    *,
    window_width: int,
    window_height: int,
    codex_ratio: int = DEFAULT_CODEX_RATIO,
) -> tuple[str, list[str]]:
    """Build a checksum-less layout body of one clean column per Unit (pure).

    Returns ``(body, desired_leaf_order)``: a tmux layout body laying each Unit
    out left-to-right as a full-height column (a ``[codex/claude]`` vertical split,
    or a single leaf for a missing-peer Unit) at an even fair-share width, plus the
    column-major pane-id order the live panes must be swapped into before
    ``select-layout`` (it assigns panes by order). Widths/heights are kept
    internally consistent so tmux accepts the layout.
    """
    codex_ratio = normalize_ratio(codex_ratio)
    n = len(units_in_order)
    w = max(1, int(window_width))
    h = max(2, int(window_height))
    content_w = max(n, w - (n - 1)) if n else w
    widths = fair_share_widths(content_w, n) if n else []

    cols: list[str] = []
    leaf_order: list[str] = []
    x = 0
    for i, unit in enumerate(units_in_order):
        cw = widths[i]
        panes = unit.ordered_panes
        if len(panes) >= 2:
            codex_h = max(1, min(h - 2, round((h - 1) * codex_ratio / 100)))
            claude_h = (h - 1) - codex_h
            col = (
                f"{cw}x{h},{x},0["
                f"{cw}x{codex_h},{x},0,{_pane_num(panes[0])},"
                f"{cw}x{claude_h},{x},{codex_h + 1},{_pane_num(panes[1])}]"
            )
            leaf_order.extend([panes[0], panes[1]])
        else:
            col = f"{cw}x{h},{x},0,{_pane_num(panes[0])}"
            leaf_order.append(panes[0])
        cols.append(col)
        x += cw + 1

    body = f"{w}x{h},0,0{{" + ",".join(cols) + "}"
    return body, leaf_order


def build_cockpit_reconcile_plan(
    root: Optional[LayoutCell],
    identity: Mapping[str, Mapping[str, object]],
    *,
    session: str = COCKPIT_SESSION_DEFAULT,
    codex_ratio: int = DEFAULT_CODEX_RATIO,
) -> CockpitReconcilePlan:
    """Plan flattening nested top-level cells into per-Unit columns (#12136, pure).

    ``root`` is the parsed ``window_layout`` tree (:func:`parse_window_layout`);
    ``identity`` maps each ``pane_id`` to its ``{workspace_id, lane_id, role}`` from
    pane options. Pure: returns a :class:`CockpitReconcilePlan`, runs no tmux.

    Fail-closed (``blocked_reason``, no commands) when a cockpit pane is
    unidentified (#12133 identity-adoption scope), a Unit has a duplicate same-role
    pane, or a Unit spans more than one top-level cell. When a top-level cell is
    tangled, the plan is the
    order-preserving `swap-pane` sequence (sort the live pane order into
    column-major Unit order) followed by one checksum-valid `select-layout` that
    lays every Unit out as a clean column in its existing left-to-right order.
    """
    codex_ratio = normalize_ratio(codex_ratio)
    claude_ratio = 100 - codex_ratio
    cells, units, unidentified, duplicates = _collect_reconcile_units(root, identity)

    # Global left-to-right Unit order: cells are left-to-right, and units within a
    # cell are already min_x-sorted, so concatenating preserves visual order.
    units_in_order = tuple((u.workspace_id, u.lane_id) for u in units)

    cell_of_unit: dict[tuple[str, str], set[int]] = {}
    for u in units:
        cell_of_unit.setdefault((u.workspace_id, u.lane_id), set()).add(u.cell_index)
    split_units = sorted(k for k, idx in cell_of_unit.items() if len(idx) > 1)

    blocked_reason: Optional[str] = None
    if unidentified:
        blocked_reason = (
            f"cockpit {session!r} has unidentified pane(s) "
            f"({', '.join(sorted(set(unidentified)))}) with no @mozyo_workspace_id "
            f"/ @mozyo_agent_role; structural reconcile needs Unit identity from "
            f"pane options. Adopt identity with the `mozyo cockpit adopt` flow "
            f"(diagnose first with `mozyo cockpit doctor-geometry`); resolve "
            f"identity, then reconcile."
        )
    elif duplicates:
        blocked_reason = (
            f"cockpit {session!r} has a Unit with more than one pane of the same "
            f"role (duplicate pane(s) {', '.join(sorted(set(duplicates)))}); a clean "
            f"column needs exactly one codex and/or one claude per Unit. Refusing "
            f"to reconcile (fail-closed) — resolve the duplicate role first."
        )
    elif split_units:
        names = ", ".join(f"{ws}/{lane}" for ws, lane in split_units)
        blocked_reason = (
            f"cockpit {session!r} has Unit(s) split across more than one top-level "
            f"cell ({names}); this reconcile flattens nested cells but does not "
            f"re-merge a split Unit. Resolve manually or via a future reconcile mode."
        )

    tangled = any(c.tangled for c in cells)
    drift = tangled and blocked_reason is None
    clean = blocked_reason is None and not drift

    swap_commands: list[CockpitCommand] = []
    layout_command: Optional[CockpitCommand] = None
    target_layout: Optional[str] = None

    if drift and root is not None:
        current_order = [leaf.pane_id for leaf in root.leaves() if leaf.pane_id]
        body, desired_order = build_unit_columns_layout(
            units,
            window_width=root.width,
            window_height=root.height,
            codex_ratio=codex_ratio,
        )
        if sorted(current_order) != sorted(desired_order):
            # The leaf set and the identified-Unit pane set disagree (e.g. a pane
            # appeared/vanished mid-read): refuse rather than emit a layout that
            # would not cover every pane.
            blocked_reason = (
                f"cockpit {session!r} live pane set does not match the identified "
                f"Unit panes; refusing to relayout. Re-read and retry."
            )
            drift = False
            clean = False
        else:
            for src, dst in plan_pane_swaps(current_order, desired_order):
                swap_commands.append(
                    CockpitCommand(
                        argv=("swap-pane", "-s", src, "-t", dst),
                        captures=None,
                        purpose=f"reconcile: order pane {src} before {dst}",
                    )
                )
            target_layout = format_custom_layout(body)
            layout_command = CockpitCommand(
                argv=(
                    "select-layout", "-t", f"{session}:{COCKPIT_WINDOW}",
                    target_layout,
                ),
                captures=None,
                purpose="reconcile: apply per-Unit top-level columns",
            )

    return CockpitReconcilePlan(
        session=session,
        window=COCKPIT_WINDOW,
        codex_ratio=codex_ratio,
        claude_ratio=claude_ratio,
        cells=tuple(cells),
        units_in_order=units_in_order,
        drift=drift,
        clean=clean,
        blocked_reason=blocked_reason,
        swap_commands=tuple(swap_commands),
        layout_command=layout_command,
        target_layout=target_layout,
    )
