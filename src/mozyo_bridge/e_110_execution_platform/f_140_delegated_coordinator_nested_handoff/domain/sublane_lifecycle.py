"""Pure sublane lifecycle projection / planning core (Redmine #12955).

The MVP lifecycle surface under ``mozyo-bridge sublane`` (``create`` / ``start`` /
``list`` / ``status`` / ``retire``) removes the hand-assembled choreography a
coordinator otherwise repeats for every max-5 sublane: derive the worktree / branch /
lane identity, stand up a cockpit-visible gateway + worker pane, and — at end of life —
retire the lane's panes / worktree / local branch safely. This module is the **pure
decision + projection core** of that surface; it holds no IO and discovers nothing.

Three concerns, each pure over caller-supplied facts:

- :func:`project_sublanes` folds a tmux pane inventory (the ``pane_lines`` row dicts) into
  one :class:`SublaneLaneView` per non-default lane — issue id (parsed from the lane
  label), worktree / repo root, the gateway (``codex``) pane, the worker (``claude``)
  pane, branch (from a caller-resolved lookup), and a coarse :data:`SUBLANE_STATE_*`.
  This is the read-only ``list`` / ``status`` projection. Since #13086 the projection
  also carries the lane's **host window identity** (session / window index / window
  name, parsed with the same :func:`...agent_discovery.parse_location` helper the
  ``agents list`` / ``agents targets`` records use, so the two surfaces can never
  contradict each other) and machine-readable **stale / retire hints**
  (:data:`STALE_HINT_*`) — decision *material* for a human / coordinator retire call,
  never an auto-retire trigger. Window identity and hints are display / diagnosis
  projections only; routing and callback target resolution never read them.

- :func:`plan_sublane_create` composes the already-decided #12604 worktree launch action
  (:func:`...sublane_integration_policy.decide_worktree_launch`) with the pane / role /
  dispatch steps into a replayable :class:`SublaneCreatePlan`. It **fails closed**: a
  missing identity field or a blocked launch decision yields a ``blocked`` plan with no
  steps, never a partial one. It emits the plan; it never actuates it.

- :func:`preflight_sublane_retire` composes the #12604 retire decision
  (:func:`...sublane_integration_policy.decide_retire_integration`) into a
  :class:`SublaneRetirePreflight` carrying the fail-closed verdict, the durable-record
  journal, and the retirement runbook. On :data:`INTEGRATION_BLOCKED` the runbook is
  empty (the lane is *not* retired); on ``retire_ok`` it lists the destructive commands
  the coordinator runs by hand.

Boundary (``vibes/docs/logics/worktree-lifecycle-boundary.md`` — *scope 境界 / Design
Consultation triggers*): the destructive / actuating half of the lifecycle
(``git worktree add/remove`` as a core CLI actuator, pane kill, local branch delete) is
gated behind a separate Design Consultation and is **not** performed here. This module
plans and explains only; it is squarely on the identity / discovery / safety /
planning side of that boundary. It never self-authorizes a close, a carve-out, or an
owner decision, and it never emits private paths or pane ids into a durable journal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Collection, Iterable, Mapping, Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    parse_location,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    LAUNCH_BLOCKED,
    LAUNCH_CREATE_WORKTREE,
    LAUNCH_REUSE_WORKTREE,
    RetireDecision,
    WorktreeLaunchDecision,
    render_integration_decision_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.work_unit_granularity import (
    DEFAULT_WORK_UNIT_GRANULARITY,
    WorkUnitDispatchDecision,
    decide_work_unit_dispatch,
)

# ---------------------------------------------------------------------------
# Roles + lane identity (literal; machine-readable regardless of UI language).
# ---------------------------------------------------------------------------

#: The sublane gateway role — the Codex pane the coordinator routes governed kinds to.
GATEWAY_ROLE = "codex"
#: The sublane worker role — the same-lane Claude implementer.
WORKER_ROLE = "claude"

#: The reserved non-sublane lane id (cockpit / unmanaged panes carry this).
DEFAULT_LANE = "default"

#: The main / coordinator lane's label. The coordinator lane is *not* a sublane, but its
#: panes do not always carry ``lane_id == "default"``: in a live cockpit the main lane is
#: stamped with a hashed workspace lane id (e.g. ``lane-124611ffed3c``) and only the
#: label / kind reads ``main``. Excluding it by label / kind — not just the literal
#: default lane id — keeps ``list`` / ``status`` reporting real sublanes only.
MAIN_LANE_LABEL = "main"

#: Lane-kind values (``@mozyo_lane_kind``) that mark the coordinator / default lane.
_NON_SUBLANE_KINDS = frozenset({"main", "default"})

#: ``issue_<id>_<slug>`` lane-label convention (the existing dogfood naming). Only the
#: numeric issue id is extracted; the slug is display-only and never forced-generated
#: here (issue-number -> path/branch generation stays operator judgment per the boundary
#: doc runbook).
_ISSUE_LABEL_RE = re.compile(r"issue[_-](\d+)")


def parse_issue_from_lane_label(lane_label: str) -> Optional[str]:
    """Extract the numeric issue id from an ``issue_<id>_...`` lane label (pure).

    Returns ``None`` when the label carries no ``issue_<digits>`` token, so a lane whose
    label does not follow the convention simply shows no issue rather than a guessed one.
    """
    match = _ISSUE_LABEL_RE.search(lane_label or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Record-path redaction (Redmine #13368; privacy系統対処).
#
# A lane ``worktree_path`` is a host-local absolute path and is **private state**:
# ``src/mozyo_bridge/core/state/lane_metadata.py`` marks it "local/private state
# only; do not copy to a Redmine journal / pasteable durable record", and
# ``vibes/docs/rules/public-private-boundary.md`` forbids personal home / private
# project absolute paths in a public record. j#73454 (#13358 review finding 2) was
# exactly such a leak: a gateway dispatch outcome carried the absolute worktree
# path into the Redmine journal. These helpers make every *pasteable human-readable*
# record redact the absolute path to its portable **sibling basename** — the lane
# worktree directory name (e.g. ``mozyo_bridge_issue_13368_record_path_redaction``),
# which carries no home prefix and still identifies the lane. The absolute path
# stays only in the structured JSON outcome / local state, mirroring the #12098
# ``ExecutionRoot`` doctrine (``workdir`` absolute in the machine surface, portable
# pointer in the pasteable text).
# ---------------------------------------------------------------------------

#: A leading Windows drive designator (``C:`` / ``d:``). Its presence — like a
#: backslash separator — marks a Windows-shaped path whose basename POSIX flavor
#: cannot extract (Redmine #13368 review j#73538 finding 1).
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _portable_basename(text: str) -> str:
    """Final path component of ``text`` for either a POSIX or a Windows path (pure).

    Redmine #13368 review j#73538 (finding 1): a lane ``worktree_path`` may be a
    Windows-shaped host-local path (``C:\\Users\\<user>\\lane``). ``PurePosixPath``
    does not treat ``\\`` as a separator, so its ``.name`` returns the whole string
    and the private prefix survives redaction. Detect a Windows shape (a backslash
    separator or a leading drive designator) and use the Windows flavor for it;
    otherwise the POSIX flavor. Falls back to the raw string when no component can
    be derived (e.g. a bare drive), never an empty result.
    """
    if "\\" in text or _WINDOWS_DRIVE_RE.match(text):
        return PureWindowsPath(text).name or text
    return PurePosixPath(text).name or text


def portable_worktree_label(worktree_path: Optional[str]) -> str:
    """Pasteable-safe label for a lane worktree: its sibling basename (pure, #13368).

    Returns the worktree directory basename (no personal home / private-project
    absolute prefix), so it is safe to render into a Redmine journal / pasteable
    durable record. Handles both POSIX and Windows-shaped paths (:func:`_portable_basename`).
    Empty input renders as ``-`` (matching the existing ``or '-'`` field convention
    in the record renderers).
    """
    text = (worktree_path or "").strip()
    if not text:
        return "-"
    return _portable_basename(text)


def redact_worktree_paths(text: str, *worktree_paths: Optional[str]) -> str:
    """Redact known host-local worktree absolute paths in composed record text (pure).

    Replaces each supplied absolute worktree path with its portable sibling basename
    (:func:`portable_worktree_label`) wherever it appears in ``text`` — e.g. inside a
    replayable ``git worktree add <abs>`` / ``cockpit append --repo <abs>`` command
    line — so a pasteable text record carries no private path while the exact command
    (with the absolute path) is still preserved in the structured JSON outcome
    (#13368). Replacement is by the exact known string, never by guessing a home
    prefix, so it cannot mangle unrelated text.
    """
    out = text
    for raw in worktree_paths:
        candidate = (raw or "").strip()
        if candidate:
            out = out.replace(candidate, _portable_basename(candidate))
    return out


# ---------------------------------------------------------------------------
# list / status: sublane inventory projection.
# ---------------------------------------------------------------------------

#: Both a gateway and a worker pane are live for the lane.
SUBLANE_STATE_ACTIVE = "active"
#: Only the gateway (Codex) pane is live — the worker was lost / not yet dispatched.
SUBLANE_STATE_GATEWAY_ONLY = "gateway_only"
#: Only the worker (Claude) pane is live — the gateway is missing.
SUBLANE_STATE_WORKER_ONLY = "worker_only"
#: Neither a gateway nor a worker pane is live (only other/unknown-role panes).
SUBLANE_STATE_DETACHED = "detached"

SUBLANE_STATES = frozenset(
    {
        SUBLANE_STATE_ACTIVE,
        SUBLANE_STATE_GATEWAY_ONLY,
        SUBLANE_STATE_WORKER_ONLY,
        SUBLANE_STATE_DETACHED,
    }
)

# ---------------------------------------------------------------------------
# Stale / retire hints (#13086): machine-readable retire *decision material*.
#
# Each hint names one observed inconsistency between the lane's identity and
# the live inventory (or the caller-probed git facts). Hints are advisory
# diagnosis output for a human / coordinator retire decision — they never
# trigger a destructive retire, and routing / callback target resolution never
# reads them. Detail-bearing hints append ``:<detail>`` after the literal
# prefix (the ``missing_field:<name>`` convention the create plan already
# uses).
# ---------------------------------------------------------------------------

#: The lane has no live ``codex`` gateway pane (dispatch / callback rail lost).
STALE_HINT_GATEWAY_PANE_MISSING = "gateway_pane_missing"
#: The lane has no live ``claude`` worker pane (implementer lost / never adopted).
STALE_HINT_WORKER_PANE_MISSING = "worker_pane_missing"
#: The lane's panes span more than one tmux window (`windows` lists them all) —
#: the durable record expects one host window per lane (#13085 shared default).
STALE_HINT_WINDOW_SPLIT = "window_split"
#: Prefix: another live lane carries the same issue id (rendered as
#: ``duplicate_issue_lane:<peer lane label or id>`` per duplicate peer) — one of
#: them is likely superseded.
STALE_HINT_DUPLICATE_ISSUE_LANE = "duplicate_issue_lane"
#: The lane records a worktree / repo root, but it no longer resolves to a live
#: git checkout (removed, moved, or never created).
STALE_HINT_WORKTREE_UNRESOLVED = "worktree_unresolved"
#: Prefix: the lane's branch is already reachable from the integration branch
#: (rendered as ``branch_integrated:<integration branch>``) — no commit on the
#: lane branch is unmerged, so retiring loses no work. Note the literal meaning:
#: a freshly dispatched lane that has not committed yet also qualifies (its
#: branch still points at an integrated commit) — combine with the issue /
#: journal state before reading this as "the lane's work shipped".
STALE_HINT_BRANCH_INTEGRATED = "branch_integrated"

STALE_HINTS = frozenset(
    {
        STALE_HINT_GATEWAY_PANE_MISSING,
        STALE_HINT_WORKER_PANE_MISSING,
        STALE_HINT_WINDOW_SPLIT,
        STALE_HINT_DUPLICATE_ISSUE_LANE,
        STALE_HINT_WORKTREE_UNRESOLVED,
        STALE_HINT_BRANCH_INTEGRATED,
    }
)


@dataclass(frozen=True)
class SublanePane:
    """One pane belonging to a sublane (a projection of a pane-inventory row).

    ``session`` / ``window_index`` / ``window_name`` are the pane's host-window
    identity (#13086), parsed from the row's ``location`` / ``window_name``
    fields with the same :func:`parse_location` vocabulary the ``agents list`` /
    ``agents targets`` records use — display / diagnosis metadata, never a
    routing key.
    """

    pane_id: str
    role: str
    active: bool
    command: str
    cwd: str
    session: str = ""
    window_index: str = ""
    window_name: str = ""

    @property
    def window(self) -> str:
        """The pane's ``session:window_index`` window address ('' when unknown)."""
        if not self.session and not self.window_index:
            return ""
        return f"{self.session}:{self.window_index}"

    def as_payload(self) -> dict[str, object]:
        return {
            "pane_id": self.pane_id,
            "role": self.role,
            "active": self.active,
            "command": self.command,
            "cwd": self.cwd,
            "session": self.session,
            "window_index": self.window_index,
            "window_name": self.window_name,
            "window": self.window,
        }


@dataclass(frozen=True)
class SublaneLaneView:
    """The ``list`` / ``status`` projection of a single sublane lane.

    ``host_window`` / ``host_window_name`` name the single tmux window hosting
    every pane of the lane (#13086); when the lane's panes span multiple windows
    both are ``None``, ``windows`` lists every distinct window address, and the
    :data:`STALE_HINT_WINDOW_SPLIT` hint is raised. ``stale_hints`` is the
    machine-readable retire decision material — advisory only, never an
    auto-retire trigger and never a routing input.
    """

    workspace_id: str
    lane_id: str
    lane_label: str
    issue: Optional[str]
    branch: Optional[str]
    repo_root: Optional[str]
    gateway_pane: Optional[str]
    worker_pane: Optional[str]
    state: str
    panes: Tuple[SublanePane, ...] = ()
    host_window: Optional[str] = None
    host_window_name: Optional[str] = None
    windows: Tuple[str, ...] = ()
    stale_hints: Tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "lane_label": self.lane_label,
            "issue": self.issue,
            "branch": self.branch,
            "repo_root": self.repo_root,
            "gateway_pane": self.gateway_pane,
            "worker_pane": self.worker_pane,
            "state": self.state,
            "panes": [p.as_payload() for p in self.panes],
            "host_window": self.host_window,
            "host_window_name": self.host_window_name,
            "windows": list(self.windows),
            "stale_hints": list(self.stale_hints),
        }


def _is_non_sublane_lane(lane_id: str, lane_label: str, lane_kind: str) -> bool:
    """True for the coordinator / default lane, which ``list`` / ``status`` must exclude.

    A lane is *not* a sublane when any of its identity signals mark it as the main /
    default coordinator lane: the reserved :data:`DEFAULT_LANE` id (or an empty id, which
    normalizes to it), the :data:`MAIN_LANE_LABEL`, or a main / default lane kind. Relying
    on the literal default lane id alone is insufficient — the live main lane carries a
    hashed lane id and only its label / kind reads ``main`` (Redmine #12955 j#69954).
    """
    if (lane_id or "").strip() in ("", DEFAULT_LANE):
        return True
    if (lane_label or "").strip().casefold() == MAIN_LANE_LABEL:
        return True
    if (lane_kind or "").strip().casefold() in _NON_SUBLANE_KINDS:
        return True
    return False


def _lane_state(gateway_pane: Optional[str], worker_pane: Optional[str]) -> str:
    if gateway_pane and worker_pane:
        return SUBLANE_STATE_ACTIVE
    if gateway_pane:
        return SUBLANE_STATE_GATEWAY_ONLY
    if worker_pane:
        return SUBLANE_STATE_WORKER_ONLY
    return SUBLANE_STATE_DETACHED


def _lane_windows(
    panes: Tuple[SublanePane, ...],
) -> Tuple[Optional[str], Optional[str], Tuple[str, ...]]:
    """Derive ``(host_window, host_window_name, windows)`` from a lane's panes (pure).

    ``windows`` is every distinct known window address, sorted. ``host_window``
    (and its name) is set only when exactly one window hosts every
    window-resolvable pane; a split lane or an all-unknown lane yields ``None``
    so a caller never mistakes a guess for the host.
    """
    addresses = sorted({p.window for p in panes if p.window})
    if len(addresses) != 1:
        return None, None, tuple(addresses)
    host = addresses[0]
    name = next(
        (p.window_name for p in panes if p.window == host and p.window_name), None
    )
    return host, name, (host,)


def _lane_stale_hints(
    *,
    gateway_pane: Optional[str],
    worker_pane: Optional[str],
    windows: Tuple[str, ...],
    duplicate_peers: Tuple[str, ...],
    worktree_unresolved: bool,
    integrated_into: Optional[str],
) -> Tuple[str, ...]:
    """Assemble one lane's machine-readable stale / retire hints (pure, advisory)."""
    hints: list[str] = []
    if not gateway_pane:
        hints.append(STALE_HINT_GATEWAY_PANE_MISSING)
    if not worker_pane:
        hints.append(STALE_HINT_WORKER_PANE_MISSING)
    if len(windows) > 1:
        hints.append(STALE_HINT_WINDOW_SPLIT)
    for peer in duplicate_peers:
        hints.append(f"{STALE_HINT_DUPLICATE_ISSUE_LANE}:{peer}")
    if worktree_unresolved:
        hints.append(STALE_HINT_WORKTREE_UNRESOLVED)
    if integrated_into:
        hints.append(f"{STALE_HINT_BRANCH_INTEGRATED}:{integrated_into}")
    return tuple(hints)


def project_sublanes(
    pane_rows: Iterable[Mapping[str, str]],
    *,
    branches: Optional[Mapping[str, str]] = None,
    unresolved_worktrees: Optional[Collection[str]] = None,
    integrated_branches: Optional[Mapping[str, str]] = None,
) -> list[SublaneLaneView]:
    """Fold a tmux pane inventory into one :class:`SublaneLaneView` per sublane (pure).

    ``pane_rows`` are the ``pane_lines`` row dicts (keys ``id`` / ``agent_role`` /
    ``workspace_id`` / ``lane_id`` / ``lane_label`` / ``lane_kind`` / ``cwd`` /
    ``command`` / ``pane_active`` / ``location`` / ``window_name`` /
    ``repo_root_stamp`` …). Rows are grouped by ``(workspace_id, lane_id)``; the
    coordinator / default lane is skipped (:func:`_is_non_sublane_lane` — by default
    lane id, ``main`` label, or main / default kind) so only real sublanes appear.
    Within a lane the first ``codex`` pane is the gateway and the first ``claude``
    pane the worker; extra same-role panes are still listed under ``panes`` but never
    silently promoted. Lanes are returned sorted by ``(workspace_id, lane_id)`` for a
    stable display.

    The caller-resolved lookups keep the domain IO-free (it never runs git):

    - ``branches`` — ``lane_id -> branch``; an absent entry leaves ``branch`` ``None``.
    - ``unresolved_worktrees`` — lane ids whose recorded worktree / repo root no
      longer resolves to a live git checkout -> :data:`STALE_HINT_WORKTREE_UNRESOLVED`.
    - ``integrated_branches`` — ``lane_id -> integration branch`` the lane's branch is
      already reachable from -> :data:`STALE_HINT_BRANCH_INTEGRATED`.

    An absent lookup entry means *unknown*, and unknown never fabricates a hint —
    hints are advisory retire decision material, so the projection stays quiet
    rather than guessing (#13086).
    """
    branches = branches or {}
    unresolved_worktrees = frozenset(unresolved_worktrees or ())
    integrated_branches = integrated_branches or {}
    grouped: dict[Tuple[str, str], list[SublanePane]] = {}
    labels: dict[Tuple[str, str], str] = {}
    repo_roots: dict[Tuple[str, str], str] = {}

    for row in pane_rows:
        lane_id = (row.get("lane_id") or "").strip() or DEFAULT_LANE
        lane_label_raw = (row.get("lane_label") or "").strip()
        lane_kind_raw = (row.get("lane_kind") or "").strip()
        # Exclude the coordinator / default lane by any of its identity signals — the
        # live main lane carries a hashed lane id, so a literal default-id check alone
        # would emit it as a sublane (Redmine #12955 j#69954).
        if _is_non_sublane_lane(lane_id, lane_label_raw, lane_kind_raw):
            continue
        workspace_id = (row.get("workspace_id") or "").strip()
        key = (workspace_id, lane_id)
        session, window_index, _pane_index = parse_location(
            (row.get("location") or "").strip()
        )
        pane = SublanePane(
            pane_id=(row.get("id") or "").strip(),
            role=(row.get("agent_role") or "").strip(),
            active=(row.get("pane_active") or "").strip() == "1",
            command=(row.get("command") or "").strip(),
            cwd=(row.get("cwd") or "").strip(),
            session=session,
            window_index=window_index,
            window_name=(row.get("window_name") or "").strip(),
        )
        grouped.setdefault(key, []).append(pane)
        # Keep the first non-empty lane label seen for the lane.
        if not labels.get(key):
            labels[key] = (row.get("lane_label") or "").strip()
        # Prefer an explicit repo-root stamp; fall back to the pane cwd.
        if key not in repo_roots or not repo_roots[key]:
            repo_roots[key] = (
                (row.get("repo_root_stamp") or "").strip() or pane.cwd
            )

    # Duplicate-issue detection needs the whole lane set: map each issue id to the
    # lanes carrying it, so every duplicate lane can name its peers.
    lanes_by_issue: dict[str, list[Tuple[str, str]]] = {}
    for key in grouped:
        issue = parse_issue_from_lane_label(labels.get(key, ""))
        if issue:
            lanes_by_issue.setdefault(issue, []).append(key)

    views: list[SublaneLaneView] = []
    for key in sorted(grouped):
        workspace_id, lane_id = key
        panes = tuple(grouped[key])
        gateway = next((p.pane_id for p in panes if p.role == GATEWAY_ROLE), None)
        worker = next((p.pane_id for p in panes if p.role == WORKER_ROLE), None)
        lane_label = labels.get(key, "")
        issue = parse_issue_from_lane_label(lane_label)
        host_window, host_window_name, windows = _lane_windows(panes)
        duplicate_peers = tuple(
            labels.get(peer) or peer[1]
            for peer in sorted(lanes_by_issue.get(issue, []))
            if issue and peer != key
        )
        views.append(
            SublaneLaneView(
                workspace_id=workspace_id,
                lane_id=lane_id,
                lane_label=lane_label,
                issue=issue,
                branch=branches.get(lane_id),
                repo_root=repo_roots.get(key) or None,
                gateway_pane=gateway,
                worker_pane=worker,
                state=_lane_state(gateway, worker),
                panes=panes,
                host_window=host_window,
                host_window_name=host_window_name,
                windows=windows,
                stale_hints=_lane_stale_hints(
                    gateway_pane=gateway,
                    worker_pane=worker,
                    windows=windows,
                    duplicate_peers=duplicate_peers,
                    worktree_unresolved=lane_id in unresolved_worktrees,
                    integrated_into=integrated_branches.get(lane_id),
                ),
            )
        )
    return views


# ---------------------------------------------------------------------------
# create / start: fail-closed launch plan.
# ---------------------------------------------------------------------------

#: The plan is complete and replayable.
CREATE_PLANNED = "planned"
#: Fail-closed: a required identity field is missing, or the launch decision refused; no
#: steps are emitted.
CREATE_BLOCKED = "blocked"

CREATE_STATES = frozenset({CREATE_PLANNED, CREATE_BLOCKED})


@dataclass(frozen=True)
class SublaneCreateRequest:
    """The operator-supplied identity for a ``sublane create`` (never forced-generated).

    Every field is caller-supplied so the domain never fabricates a worktree path or
    branch from the issue number (the boundary doc keeps issue-number -> path/branch
    generation an operator decision). ``journal`` is the durable anchor the dispatch
    steps point at. ``upstream_coordinator`` is the coordinator pane the gateway calls
    back to; ``None`` renders a placeholder in the dispatch step.

    ``work_unit`` declares the governed granularity of the dispatched unit (#13002):
    the caller-asserted kind of ``issue`` (``user_story`` standard default /
    ``leaf_issue`` exception / ``epic`` / ``feature``), a fact no probe can infer —
    like the retire assertions, it is supplied from the durable Redmine record.
    ``work_unit_decision_anchor`` is the durable owner / operator decision journal
    an ``epic`` / ``feature`` dispatch must carry; without it the plan fails closed.
    """

    issue: str
    lane_label: str
    branch: str
    worktree_path: str
    journal: Optional[str] = None
    upstream_coordinator: Optional[str] = None
    gateway_role: str = GATEWAY_ROLE
    worker_role: str = WORKER_ROLE
    work_unit: str = DEFAULT_WORK_UNIT_GRANULARITY
    work_unit_decision_anchor: Optional[str] = None
    # #13293: the explicit base ref the lane worktree is cut from. ``None`` keeps the
    # historical ``git worktree add <path> -b <branch>`` behavior (branch off the main
    # checkout's current HEAD); a supplied ref pins the base so a stale checkout can
    # never silently cut a lane from an unintended commit (the j#72677 base trap). It
    # is not part of the lane *identity* (adopt / read-back match on label / issue), so
    # it never participates in the identity guard.
    base_ref: Optional[str] = None

    def work_unit_decision(self) -> WorkUnitDispatchDecision:
        """The fail-closed #13002 work-unit dispatch decision for this request."""
        return decide_work_unit_dispatch(
            self.work_unit,
            explicit_decision_anchor=self.work_unit_decision_anchor,
        )

    def missing_fields(self) -> Tuple[str, ...]:
        """The required identity fields left blank (fail-closed trigger)."""
        missing = []
        if not (self.issue or "").strip():
            missing.append("issue")
        if not (self.lane_label or "").strip():
            missing.append("lane_label")
        if not (self.branch or "").strip():
            missing.append("branch")
        if not (self.worktree_path or "").strip():
            missing.append("worktree_path")
        return tuple(missing)


@dataclass(frozen=True)
class SublaneStep:
    """One ordered, replayable step of a create plan.

    ``command`` is the concrete shell command when the step is directly replayable
    (``git worktree add`` / ``handoff send``); ``None`` when the step is a runbook
    pointer (adopt the pane's role via ``init``) whose exact form is operator / cockpit
    dependent.
    """

    order: int
    title: str
    detail: str
    command: Optional[str] = None

    def as_payload(self) -> dict[str, object]:
        return {
            "order": self.order,
            "title": self.title,
            "detail": self.detail,
            "command": self.command,
        }


@dataclass(frozen=True)
class SublaneCreatePlan:
    """The result of :func:`plan_sublane_create`."""

    status: str
    reason: str
    launch_action: Optional[str] = None
    steps: Tuple[SublaneStep, ...] = ()
    blocked_reasons: Tuple[str, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return self.status == CREATE_BLOCKED

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "launch_action": self.launch_action,
            "steps": [s.as_payload() for s in self.steps],
            "blocked_reasons": list(self.blocked_reasons),
        }


def plan_sublane_create(
    request: SublaneCreateRequest, launch: WorktreeLaunchDecision
) -> SublaneCreatePlan:
    """Compose the #12604 launch decision with the pane / dispatch steps (pure).

    Fail-closed precedence:

    1. any required identity field is blank -> :data:`CREATE_BLOCKED` (``missing_field``
       reasons), no steps;
    2. the #13002 work-unit granularity gate refuses -> :data:`CREATE_BLOCKED` carrying
       the ``work_unit_*`` diagnostic, no steps (an ``epic`` / ``feature`` unit is never
       planned without an explicit owner / operator decision anchor);
    3. the launch decision is :data:`LAUNCH_BLOCKED` -> :data:`CREATE_BLOCKED` carrying the
       decision reason, no steps (never plan against an unverified target);
    4. otherwise -> :data:`CREATE_PLANNED` with the ordered worktree + gateway + worker +
       dispatch steps. A :data:`LAUNCH_REUSE_WORKTREE` action renders the worktree step as
       a no-op reuse note rather than a ``git worktree add``.

    The steps are a *plan*: this function actuates nothing.
    """
    missing = request.missing_fields()
    if missing:
        return SublaneCreatePlan(
            status=CREATE_BLOCKED,
            reason="required sublane identity fields are missing; refusing to plan a "
            "sublane against an incomplete target",
            launch_action=launch.action,
            blocked_reasons=tuple(f"missing_field:{name}" for name in missing),
        )
    unit_decision = request.work_unit_decision()
    if not unit_decision.is_allowed:
        return SublaneCreatePlan(
            status=CREATE_BLOCKED,
            reason=unit_decision.reason,
            launch_action=launch.action,
            blocked_reasons=(unit_decision.diagnostic,),
        )
    if launch.action == LAUNCH_BLOCKED:
        return SublaneCreatePlan(
            status=CREATE_BLOCKED,
            reason=launch.reason,
            launch_action=launch.action,
            blocked_reasons=(LAUNCH_BLOCKED,),
        )

    if launch.action == LAUNCH_CREATE_WORKTREE:
        # #13293: reflect an explicit --base-ref in the planned recipe so the plan-only
        # command and the --execute actuation cut the lane from the same base.
        _base = (request.base_ref or "").strip()
        _wt_command = f"git worktree add {request.worktree_path} -b {request.branch}"
        worktree_step = SublaneStep(
            order=1,
            title="create worktree",
            detail="create the lane worktree / branch with plain git (operator recipe; "
            "not actuated by this command)",
            command=f"{_wt_command} {_base}" if _base else _wt_command,
        )
    elif launch.action == LAUNCH_REUSE_WORKTREE:
        worktree_step = SublaneStep(
            order=1,
            title="reuse worktree",
            detail=f"a worktree for branch {request.branch!r} already exists; reuse it "
            "(never clobbered)",
            command=None,
        )
    else:
        # skip_no_git / skip_disabled: the sublane runs without a worktree.
        worktree_step = SublaneStep(
            order=1,
            title="skip worktree",
            detail=launch.reason,
            command=None,
        )

    steps = (
        worktree_step,
        SublaneStep(
            order=2,
            title="append gateway pane",
            detail=f"append a cockpit-visible {request.gateway_role} gateway pane for "
            f"lane {request.lane_label!r} and bind its role / workspace / lane / "
            "repo-root stamps",
            command=None,
        ),
        SublaneStep(
            order=3,
            title="append worker pane",
            detail=f"append a cockpit-visible {request.worker_role} worker pane for "
            f"lane {request.lane_label!r} and bind its role / workspace / lane / "
            "repo-root stamps",
            command=None,
        ),
        SublaneStep(
            order=4,
            title="dispatch implementation_request",
            detail="route the governed implementation_request to the gateway "
            "(coordinator -> sublane Codex gateway -> same-lane Claude worker); the "
            "durable Redmine journal is the anchor, the pane message a pointer",
            command=_dispatch_command(request),
        ),
    )
    return SublaneCreatePlan(
        status=CREATE_PLANNED,
        reason="sublane identity resolved; launch action "
        f"{launch.action!r}: {launch.reason}",
        launch_action=launch.action,
        steps=steps,
    )


def _dispatch_command(request: SublaneCreateRequest) -> str:
    """The replayable gateway ``handoff send`` command for the create plan (pure)."""
    journal = request.journal or "<journal>"
    coordinator = request.upstream_coordinator or "<coordinator-pane>"
    return (
        "mozyo-bridge handoff send --to codex --source redmine "
        f"--issue {request.issue} --journal {journal} "
        "--kind implementation_request --target <gateway-pane> --target-repo auto "
        "--mode queue-enter --role-profile implementation_gateway "
        f"--profile-field lane={request.lane_label} "
        f"--profile-field upstream_coordinator={coordinator}"
    )


# ---------------------------------------------------------------------------
# retire: fail-closed preflight + runbook.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SublaneRetirePreflight:
    """The result of :func:`preflight_sublane_retire`.

    ``decision`` is the #12604 :class:`RetireDecision` (authority). ``journal`` is the
    durable-record text (fail-closed record on block; integration-decision record on ok)
    rendered by :func:`render_integration_decision_journal`. ``runbook`` is the ordered
    destructive command list the coordinator runs *by hand* — empty on a blocked
    preflight (the lane is not retired) and on a non-Git lane's Git-specific steps.
    """

    decision: RetireDecision
    journal: str
    runbook: Tuple[SublaneStep, ...] = field(default=())

    @property
    def may_retire(self) -> bool:
        return self.decision.may_retire

    def as_payload(self) -> dict[str, object]:
        return {
            "decision": self.decision.as_payload(),
            "journal": self.journal,
            "runbook": [s.as_payload() for s in self.runbook],
        }


def preflight_sublane_retire(
    decision: RetireDecision,
    *,
    issue: str,
    lane_label: str,
    worktree_path: Optional[str] = None,
    branch: Optional[str] = None,
    integration_branch: Optional[str] = None,
    is_git_workspace: bool = True,
) -> SublaneRetirePreflight:
    """Compose the #12604 retire decision into a fail-closed preflight + runbook (pure).

    On :data:`INTEGRATION_BLOCKED` the runbook is empty — the lane is *not* retired and the
    coordinator is called back with the fail-closed ``journal``. On ``retire_ok`` the
    runbook lists the destructive commands (pane kill / ``git worktree remove`` / local
    branch delete) the coordinator executes by hand under the Sublane Retirement Drain;
    this command never actuates them (the destructive core-CLI actuator is gated behind a
    Design Consultation per ``worktree-lifecycle-boundary.md``). Remote branch deletion is
    never emitted.
    """
    journal = render_integration_decision_journal(
        decision, issue=issue, integration_branch=integration_branch
    )
    if not decision.may_retire:
        return SublaneRetirePreflight(decision=decision, journal=journal, runbook=())

    runbook: list[SublaneStep] = [
        SublaneStep(
            order=1,
            title="confirm clean worktree",
            detail="verify no in-scope dirty / untracked changes remain before removing "
            "the worktree",
            command="git status --short",
        ),
        SublaneStep(
            order=2,
            title="kill lane panes",
            detail=f"guarded-kill the gateway + worker panes for lane {lane_label!r} "
            "(coordinator authority; never a hidden kill)",
            command=None,
        ),
    ]
    if is_git_workspace and worktree_path:
        runbook.append(
            SublaneStep(
                order=3,
                title="remove worktree",
                detail="remove the lane worktree with plain git (operator recipe)",
                command=f"git worktree remove {worktree_path}",
            )
        )
    if is_git_workspace and branch:
        runbook.append(
            SublaneStep(
                order=len(runbook) + 1,
                title="delete local branch",
                detail="delete the merged local branch only; remote branches are never "
                "deleted",
                command=f"git branch -d {branch}",
            )
        )
    return SublaneRetirePreflight(
        decision=decision, journal=journal, runbook=tuple(runbook)
    )


__all__ = (
    "GATEWAY_ROLE",
    "WORKER_ROLE",
    "DEFAULT_LANE",
    "MAIN_LANE_LABEL",
    "parse_issue_from_lane_label",
    "portable_worktree_label",
    "redact_worktree_paths",
    "SUBLANE_STATE_ACTIVE",
    "SUBLANE_STATE_GATEWAY_ONLY",
    "SUBLANE_STATE_WORKER_ONLY",
    "SUBLANE_STATE_DETACHED",
    "SUBLANE_STATES",
    "STALE_HINT_GATEWAY_PANE_MISSING",
    "STALE_HINT_WORKER_PANE_MISSING",
    "STALE_HINT_WINDOW_SPLIT",
    "STALE_HINT_DUPLICATE_ISSUE_LANE",
    "STALE_HINT_WORKTREE_UNRESOLVED",
    "STALE_HINT_BRANCH_INTEGRATED",
    "STALE_HINTS",
    "SublanePane",
    "SublaneLaneView",
    "project_sublanes",
    "CREATE_PLANNED",
    "CREATE_BLOCKED",
    "CREATE_STATES",
    "SublaneCreateRequest",
    "SublaneStep",
    "SublaneCreatePlan",
    "plan_sublane_create",
    "SublaneRetirePreflight",
    "preflight_sublane_retire",
)
