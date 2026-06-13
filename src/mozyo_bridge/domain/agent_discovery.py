"""Cross-workspace agent discovery surface (Redmine #10332, #11628).

Read-only discovery of every tmux pane structured by ``session`` /
``window_index`` / ``window_name`` / ``pane_id`` / ``process`` / ``cwd`` /
``repo_root`` / ``agent_kind``. Used by ``mozyo-bridge agents list`` and as
the building block for cross-workspace handoff targeting.

Agent identity is keyed by ``pane_id`` (Redmine #11628, owner agreement
2026-06-11): in tmux session groups the same pane belongs to several
sessions, so a per-(session, pane) row double-counts agents. The raw
:func:`discover_agents` enumeration still yields one record per tmux pane
*line* (one per session membership); :func:`fold_agents_by_pane` collapses
those memberships into one record per pane whose ``views`` carry every
membership and whose top-level session / window fields describe the
canonical view. This assumes a single tmux server — a multi-server
deployment would need a ``(socket, pane_id)`` composite key.

The legacy ``find_agent_window`` resolver in ``pane_resolver`` only addresses
**same-session** routing; it intentionally fails closed on cross-session
fallback. This module adds the structured enumeration the sender needs in
order to *name* a cross-workspace target explicitly, which is the only path
the cross-workspace handoff gate accepts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from mozyo_bridge.infrastructure.tmux_client import pane_lines
from mozyo_bridge.shared.paths import REPO_ROOT_MARKERS


AGENT_KIND_CLAUDE = "claude"
AGENT_KIND_CODEX = "codex"
AGENT_KIND_UNKNOWN = "unknown"
AGENT_KINDS = frozenset({AGENT_KIND_CLAUDE, AGENT_KIND_CODEX, AGENT_KIND_UNKNOWN})

# Role identity model (Redmine #11822). Agent role is not the window name nor a
# single pane option — it is the *output of a resolver* over the pane's runtime
# facts, so cockpit panes (role on `@mozyo_agent_role`, window named `cockpit`)
# and normal-`mozyo` panes (role on the window name) classify uniformly. The
# resolver always exposes which signal won (``role_source``) and how strong it
# is (``confidence``) so a mis-route can be traced and automatic targeting can
# fail closed on weak / ambiguous signals.
ROLE_SOURCE_PANE_OPTION = "pane_option"
ROLE_SOURCE_WINDOW_NAME = "window_name"
ROLE_SOURCE_INFERRED = "inferred"
ROLE_SOURCE_UNKNOWN = "unknown"

CONFIDENCE_STRONG = "strong"
CONFIDENCE_WEAK = "weak"
CONFIDENCE_NONE = "none"

# Foreground process basenames that *weakly* hint a role. `node` / versioned
# native binaries are receiver-agnostic (both CLIs are node-based), so they are
# deliberately NOT here — a weak hint must still name the role to be usable, and
# automatic handoff never targets on a weak hint regardless.
_PROCESS_ROLE_HINTS = {AGENT_KIND_CLAUDE: AGENT_KIND_CLAUDE, AGENT_KIND_CODEX: AGENT_KIND_CODEX}


@dataclass(frozen=True)
class PaneView:
    """One (session, window, pane-index) membership of a pane.

    Grouped tmux sessions expose the same pane through several sessions;
    each membership is a view. ``canonical`` marks the view whose session
    matches the workspace's resolved canonical session name.
    """

    session: str
    window_index: str
    window_name: str
    pane_index: str
    pane_active: bool
    canonical: bool

    def as_payload(self) -> dict:
        return {
            "session": self.session,
            "window_index": self.window_index,
            "window_name": self.window_name,
            "pane_index": self.pane_index,
            "pane_active": self.pane_active,
            "canonical": self.canonical,
        }


@dataclass(frozen=True)
class AgentRecord:
    """One discovered pane; after folding, one agent identity.

    ``views`` is empty on the raw per-line records from
    :func:`discover_agents` and populated by :func:`fold_agents_by_pane`,
    where the top-level session / window / pane fields describe the
    canonical view and ``views`` carries every membership.
    """

    pane_id: str
    session: str
    window_index: str
    window_name: str
    pane_index: str
    pane_active: bool
    process: str
    cwd: str
    repo_root: str | None
    agent_kind: str
    ambiguous: bool
    role_source: str = ROLE_SOURCE_UNKNOWN
    confidence: str = CONFIDENCE_NONE
    views: tuple[PaneView, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "pane_id": self.pane_id,
            "session": self.session,
            "window_index": self.window_index,
            "window_name": self.window_name,
            "pane_index": self.pane_index,
            "pane_active": self.pane_active,
            "process": self.process,
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "agent_kind": self.agent_kind,
            "ambiguous": self.ambiguous,
            "role_source": self.role_source,
            "confidence": self.confidence,
            "views": [view.as_payload() for view in self.views],
        }


def infer_repo_root(cwd: str) -> str | None:
    """Walk up from ``cwd`` until a REPO_ROOT_MARKERS-bearing directory is found.

    Recognizes both git-style project markers (``.git`` / ``.tmux.conf`` /
    ``pyproject.toml``) and scaffolded mozyo workspace markers
    (``.mozyo-bridge/scaffold.json``), so a non-git scaffolded workspace
    reports its own root instead of leaking up to the home directory
    (Redmine #11301).

    Returns the absolute path as a string, or ``None`` when no marker is
    reachable (filesystem root, unreadable path, etc.). The resolver is
    permissive on errors because discovery is read-only — a missing
    repo_root in the output is informational, not fatal.
    """
    if not cwd:
        return None
    try:
        current = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        if any((path / marker).exists() for marker in REPO_ROOT_MARKERS):
            return str(path)
    return None


def classify_agent_kind(window_name: str) -> str:
    if window_name == AGENT_KIND_CLAUDE:
        return AGENT_KIND_CLAUDE
    if window_name == AGENT_KIND_CODEX:
        return AGENT_KIND_CODEX
    return AGENT_KIND_UNKNOWN


@dataclass(frozen=True)
class RoleResolution:
    """The resolved agent role for a pane plus the signal that decided it.

    ``role`` is ``claude`` / ``codex`` / ``unknown``. ``role_source`` names the
    winning signal (``pane_option`` / ``window_name`` / ``inferred`` /
    ``unknown``) and ``confidence`` grades it (``strong`` / ``weak`` /
    ``none``). ``ambiguous`` is set when two signals name *different* roles
    (e.g. ``@mozyo_agent_role=claude`` in a ``codex`` window) — a state callers
    must treat as fail-closed for automatic targeting. ``evidence`` carries the
    minimal raw facts for debugging.
    """

    role: str
    role_source: str
    confidence: str
    ambiguous: bool
    evidence: tuple[str, ...] = ()


def _normalize_role(value: str | None) -> str:
    """Map a raw role-ish string to a known agent kind, else ``unknown``."""
    text = (value or "").strip()
    if text == AGENT_KIND_CLAUDE:
        return AGENT_KIND_CLAUDE
    if text == AGENT_KIND_CODEX:
        return AGENT_KIND_CODEX
    return AGENT_KIND_UNKNOWN


def resolve_agent_role(
    *,
    pane_option_role: str | None = None,
    window_name: str | None = None,
    process: str | None = None,
) -> RoleResolution:
    """Resolve a pane's agent role from its runtime facts (pure, Redmine #11822).

    Signal priority:

    1. explicit pane option ``@mozyo_agent_role`` -> ``pane_option`` / strong
    2. ``window_name == claude|codex`` -> ``window_name`` / strong (legacy rail)
    3. foreground process basename ``claude`` / ``codex`` -> ``inferred`` / weak

    When BOTH the pane option and the window name name an agent role and they
    disagree, the result is ``ambiguous`` (fail-closed); the pane option is
    reported as the role (it is the more explicit, intentional marker) but
    callers must not auto-target an ambiguous pane. Live tmux state remains the
    liveness / preflight source of truth (#11698) — this resolver decides
    *identity*, never liveness.
    """
    option_role = _normalize_role(pane_option_role)
    window_role = _normalize_role(window_name)
    process_role = _PROCESS_ROLE_HINTS.get((process or "").strip(), AGENT_KIND_UNKNOWN)
    evidence = (
        f"option={(pane_option_role or '').strip() or '-'}",
        f"window={(window_name or '').strip() or '-'}",
        f"process={(process or '').strip() or '-'}",
    )

    if option_role != AGENT_KIND_UNKNOWN:
        ambiguous = (
            window_role != AGENT_KIND_UNKNOWN and window_role != option_role
        )
        return RoleResolution(
            role=option_role,
            role_source=ROLE_SOURCE_PANE_OPTION,
            confidence=CONFIDENCE_STRONG,
            ambiguous=ambiguous,
            evidence=evidence,
        )
    if window_role != AGENT_KIND_UNKNOWN:
        return RoleResolution(
            role=window_role,
            role_source=ROLE_SOURCE_WINDOW_NAME,
            confidence=CONFIDENCE_STRONG,
            ambiguous=False,
            evidence=evidence,
        )
    if process_role != AGENT_KIND_UNKNOWN:
        return RoleResolution(
            role=process_role,
            role_source=ROLE_SOURCE_INFERRED,
            confidence=CONFIDENCE_WEAK,
            ambiguous=False,
            evidence=evidence,
        )
    return RoleResolution(
        role=AGENT_KIND_UNKNOWN,
        role_source=ROLE_SOURCE_UNKNOWN,
        confidence=CONFIDENCE_NONE,
        ambiguous=False,
        evidence=evidence,
    )


def _parse_location(location: str) -> tuple[str, str, str]:
    session, _, rest = location.partition(":")
    window_index, _, pane_index = rest.partition(".")
    return session, window_index, pane_index


def discover_agents(panes: Iterable[dict[str, str]] | None = None) -> list[AgentRecord]:
    """Enumerate every tmux pane and classify by window-name agent rail.

    ``ambiguous`` flags panes whose ``(session, window_name)`` pair spans
    more than one distinct window index in the same session — the same
    fail-closed surface ``find_agent_window`` already raises on within a
    single session. Discovery does not raise on the ambiguity; it surfaces
    the flag so callers can decide whether to disambiguate before acting.
    """
    raw = list(panes) if panes is not None else pane_lines()
    window_indexes: dict[tuple[str, str], set[str]] = {}
    parsed: list[tuple[dict[str, str], str, str, str]] = []
    for pane in raw:
        location = pane.get("location") or ""
        session, window_index, pane_index = _parse_location(location)
        window_name = pane.get("window_name") or ""
        if window_name:
            window_indexes.setdefault((session, window_name), set()).add(window_index)
        parsed.append((pane, session, window_index, pane_index))
    records: list[AgentRecord] = []
    for pane, session, window_index, pane_index in parsed:
        window_name = pane.get("window_name") or ""
        ambig_windows = window_indexes.get((session, window_name), set())
        window_ambiguous = bool(window_name) and len(ambig_windows) > 1
        cwd = pane.get("cwd") or ""
        # Role identity comes from the resolver, not the window name alone, so a
        # cockpit pane (role on `@mozyo_agent_role`, window `cockpit`) classifies
        # like a normal-`mozyo` pane (role on the window name). The pane's own
        # duplicate-window ambiguity is OR'd with the resolver's role-signal
        # conflict — either one means "do not auto-target without disambiguation".
        resolution = resolve_agent_role(
            pane_option_role=pane.get("agent_role"),
            window_name=window_name,
            process=pane.get("command"),
        )
        # Only a STRONG signal sets the authoritative `agent_kind` (and thus
        # what `agents list` / handoff target on). A weak process hint is still
        # surfaced via `role_source` / `confidence` for debugging and #11811
        # discovery, but never promotes an `unknown` pane to a real agent kind —
        # that preserves the pre-#11822 window-name classification exactly while
        # adding the pane-option rail.
        agent_kind = (
            resolution.role
            if resolution.confidence == CONFIDENCE_STRONG
            else AGENT_KIND_UNKNOWN
        )
        records.append(
            AgentRecord(
                pane_id=pane.get("id") or "",
                session=session,
                window_index=window_index,
                window_name=window_name,
                pane_index=pane_index,
                pane_active=(pane.get("pane_active") == "1"),
                process=pane.get("command") or "",
                cwd=cwd,
                repo_root=infer_repo_root(cwd),
                agent_kind=agent_kind,
                ambiguous=window_ambiguous or resolution.ambiguous,
                role_source=resolution.role_source,
                confidence=resolution.confidence,
            )
        )
    return records


def fold_agent_kind(window_names: Iterable[str]) -> str:
    """Classify a pane from the window names of all its views.

    Windows are shared across grouped sessions so the names normally agree;
    when they somehow disagree on two different agent kinds, the pane is
    ``unknown`` rather than arbitrarily one of them.
    """
    kinds = {
        kind
        for kind in (classify_agent_kind(name) for name in window_names)
        if kind != AGENT_KIND_UNKNOWN
    }
    return kinds.pop() if len(kinds) == 1 else AGENT_KIND_UNKNOWN


def _fold_resolved_kind(kinds: Iterable[str]) -> str:
    """Fold already-resolved agent kinds across a pane's grouped views.

    Views of one pane share window name and pane options so their resolved
    kinds normally agree; a genuine disagreement folds to ``unknown`` rather
    than arbitrarily picking one (same fail-closed spirit as
    :func:`fold_agent_kind`).
    """
    distinct = {kind for kind in kinds if kind != AGENT_KIND_UNKNOWN}
    return distinct.pop() if len(distinct) == 1 else AGENT_KIND_UNKNOWN


def fold_agents_by_pane(
    records: Iterable[AgentRecord],
    *,
    resolve_canonical: Callable[[str], str | None] | None = None,
) -> list[AgentRecord]:
    """Collapse per-line records into one record per ``pane_id`` (#11628).

    Each grouped-session membership becomes a :class:`PaneView`; the
    canonical view is the one whose session equals
    ``resolve_canonical(repo_root)`` (the workspace's canonical session
    name), else the first view by (session, window, pane) sort order so the
    choice stays deterministic. The folded record's ``ambiguous`` is the OR
    of its views' flags, and ``agent_kind`` is folded across all views'
    window names. Records without a ``pane_id`` cannot carry a stable
    identity and are dropped. ``resolve_canonical`` is invoked once per
    distinct ``repo_root``.
    """
    grouped: dict[str, list[AgentRecord]] = {}
    order: list[str] = []
    for record in records:
        if not record.pane_id:
            continue
        if record.pane_id not in grouped:
            grouped[record.pane_id] = []
            order.append(record.pane_id)
        grouped[record.pane_id].append(record)

    canonical_cache: dict[str, str | None] = {}

    def canonical_session_for(repo_root: str | None) -> str | None:
        if resolve_canonical is None or not repo_root:
            return None
        if repo_root not in canonical_cache:
            canonical_cache[repo_root] = resolve_canonical(repo_root)
        return canonical_cache[repo_root]

    folded: list[AgentRecord] = []
    for pane_id in order:
        members = sorted(
            grouped[pane_id],
            key=lambda r: (r.session, r.window_index, r.pane_index),
        )
        first = grouped[pane_id][0]
        canonical_name = canonical_session_for(first.repo_root)
        canonical_index = 0
        if canonical_name is not None:
            for index, member in enumerate(members):
                if member.session == canonical_name:
                    canonical_index = index
                    break
        views = tuple(
            PaneView(
                session=member.session,
                window_index=member.window_index,
                window_name=member.window_name,
                pane_index=member.pane_index,
                pane_active=member.pane_active,
                canonical=(index == canonical_index),
            )
            for index, member in enumerate(members)
        )
        canonical = members[canonical_index]
        folded_kind = _fold_resolved_kind(member.agent_kind for member in members)
        # Keep the canonical view's resolver provenance when the fold agrees with
        # it; a cross-view disagreement (folded to unknown) resets provenance so
        # the record never claims a strong source for an unknown role.
        if folded_kind == canonical.agent_kind:
            role_source, confidence = canonical.role_source, canonical.confidence
        else:
            role_source, confidence = ROLE_SOURCE_UNKNOWN, CONFIDENCE_NONE
        folded.append(
            replace(
                canonical,
                agent_kind=folded_kind,
                ambiguous=any(member.ambiguous for member in members),
                role_source=role_source,
                confidence=confidence,
                views=views,
            )
        )
    return folded


def filter_agents(
    records: Iterable[AgentRecord],
    *,
    session: str | None = None,
    agent_kind: str | None = None,
) -> list[AgentRecord]:
    """Filter records by session membership and/or agent kind.

    The session filter matches the canonical session OR any grouped view's
    session (a folded pane is a member of every session it appears in);
    unfolded records carry no views, so this stays an exact-name match for
    them.
    """
    out: list[AgentRecord] = []
    for record in records:
        if session is not None:
            in_views = any(view.session == session for view in record.views)
            if record.session != session and not in_views:
                continue
        if agent_kind is not None and record.agent_kind != agent_kind:
            continue
        out.append(record)
    return out


def codex_gateway_candidates(
    target_session: str,
    panes: Iterable[dict[str, str]] | None = None,
) -> list[AgentRecord]:
    """Codex-classified panes in ``target_session`` for gateway diagnostics.

    Read-only diagnostic helper (Redmine #11776). Composes the existing
    discovery pipeline — :func:`discover_agents` -> :func:`fold_agents_by_pane`
    -> :func:`filter_agents` (``agent_kind=codex``) — so a blocked
    cross-session handoff can name the safe Codex gateway pane(s) with the
    concrete ``pane_id`` / ``window_name`` / ``cwd`` / ``repo_root`` an operator
    needs to build a working ``--to codex --target <pane> --target-repo <root>``
    command. It performs no send and widens no admission gate; callers pass the
    already-fetched ``panes`` snapshot so this never reaches tmux on its own.
    """
    if not target_session:
        return []
    folded = fold_agents_by_pane(discover_agents(panes))
    return filter_agents(folded, session=target_session, agent_kind=AGENT_KIND_CODEX)
