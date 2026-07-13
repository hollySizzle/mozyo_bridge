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

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import pane_lines
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    AGENT_PROVIDER_PROFILES,
    agent_discovery_aliases,
    agent_provider_ids,
)
from mozyo_bridge.shared.paths import REPO_ROOT_MARKERS


AGENT_KIND_CLAUDE = "claude"
AGENT_KIND_CODEX = "codex"
AGENT_KIND_UNKNOWN = "unknown"
# The known agent kinds are the registered provider ids (Redmine #13441) plus the
# core-owned `unknown` sentinel, which is a *resolver outcome*, not a provider — a
# profile can never register it (`FORBIDDEN_PROFILE_TOKENS` guards the authority
# axes; `unknown` is simply never a provider id in the packaged data).
_PROVIDER_IDS = agent_provider_ids()
AGENT_KINDS = _PROVIDER_IDS | {AGENT_KIND_UNKNOWN}
# `{window/pane alias -> provider id}` for name-based classification.
_DISCOVERY_ALIASES = agent_discovery_aliases()

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

# Projection kinds for the canonical `TargetRecord` view (Redmine #11907,
# unit-target-model.md). A `Target` is identity; how it is shown is a
# projection. A managed / cockpit pane carries `@mozyo_agent_role` (role_source
# == pane_option) and lives under the cockpit window — its window name is a
# layout attribute, so it projects as `cockpit_pane`. A normal-`mozyo` pane
# resolves its role from the `claude` / `codex` window name (or an inferred
# process) and projects as the compatibility `normal_window`. View kind is a
# projection attribute, never a routing identity.
VIEW_KIND_COCKPIT_PANE = "cockpit_pane"
VIEW_KIND_NORMAL_WINDOW = "normal_window"

# Foreground process basenames that *weakly* hint a role, derived from each
# provider profile's declared `process_names` (Redmine #13441). `node` / versioned
# native binaries are receiver-agnostic (both CLIs are node-based), so they are
# deliberately NOT here — a weak hint must still name the role to be usable, and
# automatic handoff never targets on a weak hint regardless.
_PROCESS_ROLE_HINTS = {
    process: profile.provider_id
    for profile in AGENT_PROVIDER_PROFILES
    for process in profile.process_names
}


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
    # Checkout-local lane facts (Redmine #11820), read from the pane's
    # `@mozyo_lane_id` / `@mozyo_lane_label` options when present (cockpit panes
    # carry them; normal-`mozyo` panes leave them empty -> the `default` lane).
    # Carried here so compact target discovery (#11811) can distinguish
    # same-workspace / different-lane panes without parsing titles.
    lane_id: str = ""
    lane_label: str | None = None
    # Delegated-coordinator-tree display breadcrumb (Redmine #12466), read from
    # the pane's `@mozyo_lane_kind` / `@mozyo_delegation_parent` options when
    # present. A projection cache for the cockpit display only: depth / root are
    # re-derived from the parent chain by the #12465 `delegation_projection`
    # foundation, and none of these carry routing / handoff / approval authority.
    # Empty for panes outside a delegation tree.
    lane_kind: str = ""
    delegation_parent: str = ""
    # Project-scoped cockpit identity (Redmine #12658), read from the pane's
    # `@mozyo_project_scope` / `@mozyo_project_path` / `@mozyo_project_label`
    # options. A monorepo project subdir is a routing / presentation scope *under*
    # the workspace; these are projection metadata, never a routing identity (the
    # `--target-repo` gate stays Git-repo-root-anchored). Empty for panes outside
    # any adopted project scope -> single-repo display is unchanged.
    project_scope: str = ""
    project_path: str = ""
    project_label: str = ""
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
            "lane_id": self.lane_id,
            "lane_label": self.lane_label,
            "lane_kind": self.lane_kind,
            "delegation_parent": self.delegation_parent,
            "project_scope": self.project_scope,
            "project_path": self.project_path,
            "project_label": self.project_label,
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
    """The provider a window name names, or ``unknown`` (Redmine #13441).

    Resolves through the profiles' declared discovery aliases, so a new
    same-protocol provider is recognized by adding a profile entry rather than a
    branch here. An unaliased name stays ``unknown`` exactly as before.
    """
    return _DISCOVERY_ALIASES.get(window_name, AGENT_KIND_UNKNOWN)


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
    """Map a raw role-ish string to a registered provider id, else ``unknown``.

    The pane option carries a *provider* token, so this checks the registered
    provider vocabulary (Redmine #13441) rather than a hard-coded pair. An
    unregistered token stays ``unknown``, so an unrecognized role never routes.
    """
    text = (value or "").strip()
    return text if text in _PROVIDER_IDS else AGENT_KIND_UNKNOWN


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

    The pane option is **authoritative when present** (Redmine #11822 audit,
    journal #57116): a pane carrying ``@mozyo_agent_role`` is a cockpit / managed
    pane, where the window name is a *layout / view* attribute — tmux auto-naming
    or existing cockpit state can leave a Claude-role pane in a window observed
    as ``codex``. Treating that layout name as a conflicting role signal would
    flag the pane ambiguous and make it unreachable, re-creating the very
    window/pane mismatch this US removes. So an explicit marker resolves
    strong / non-ambiguous regardless of the window name; the window name is only
    a role signal when no pane option is set (the normal-``mozyo`` rail, where
    panes carry no option). Live tmux state remains the liveness / preflight
    source of truth (#11698) — this resolver decides *identity*, never liveness.
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
        # Explicit marker wins; the window name is layout, not a rival signal.
        return RoleResolution(
            role=option_role,
            role_source=ROLE_SOURCE_PANE_OPTION,
            confidence=CONFIDENCE_STRONG,
            ambiguous=False,
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


def parse_location(location: str) -> tuple[str, str, str]:
    """Split a ``session:window_index.pane_index`` pane location (pure).

    The single location vocabulary every pane-inventory consumer shares: the
    ``agents list`` / ``agents targets`` discovery records parse window identity
    here, and the sublane inventory projection (#13086) imports this same helper
    so the two surfaces can never disagree on what window a pane lives in.
    """
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

    The duplicate-window check only applies when the window name is the
    pane's **role-identity authority** (the legacy ``window_name`` rail). A
    pane whose role resolves from the ``@mozyo_agent_role`` option (Redmine
    #11822 / #57116) is identified by pane id + option + lane, and its window
    name is a display-only layout attribute. Project Group windows
    deliberately share one display name per group (Redmine #12330), so two
    same-named group windows holding strong pane-option panes must stay
    non-ambiguous (Redmine #12336) — collapsing them by display name would
    re-create the window-name-as-identity coupling those issues remove.
    """
    raw = list(panes) if panes is not None else pane_lines()
    window_indexes: dict[tuple[str, str], set[str]] = {}
    parsed: list[tuple[dict[str, str], str, str, str]] = []
    for pane in raw:
        location = pane.get("location") or ""
        session, window_index, pane_index = parse_location(location)
        window_name = pane.get("window_name") or ""
        if window_name:
            window_indexes.setdefault((session, window_name), set()).add(window_index)
        parsed.append((pane, session, window_index, pane_index))
    records: list[AgentRecord] = []
    for pane, session, window_index, pane_index in parsed:
        window_name = pane.get("window_name") or ""
        cwd = pane.get("cwd") or ""
        # Role identity comes from the resolver, not the window name alone, so a
        # cockpit pane (role on `@mozyo_agent_role`, window `cockpit` / a layout
        # name) classifies like a normal-`mozyo` pane (role on the window name).
        # The resolver also reports which signal won (`role_source`), which the
        # duplicate-window check below reads so a display-only name never
        # invalidates a strong pane-option identity (see #57116, #12336).
        resolution = resolve_agent_role(
            pane_option_role=pane.get("agent_role"),
            window_name=window_name,
            process=pane.get("command"),
        )
        # Duplicate `(session, window_name)` only makes a pane ambiguous when the
        # window name *is* the role-identity authority (the legacy `window_name`
        # rail). When the role resolves from the pane option, the window name is
        # display-only: Project Group windows share one name per group (#12330),
        # and pane id + option + lane still identify the target unambiguously
        # (#12336). The pane's duplicate-window ambiguity is OR'd with the
        # resolver's own `ambiguous` so either one means "do not auto-target".
        ambig_windows = window_indexes.get((session, window_name), set())
        window_ambiguous = (
            resolution.role_source == ROLE_SOURCE_WINDOW_NAME
            and bool(window_name)
            and len(ambig_windows) > 1
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
        # Authoritative Git worktree root (Redmine #12658 j#66513). A cockpit pane
        # may carry a stamped `@mozyo_repo_root`; prefer it so a project-scoped
        # pane (whose cwd is the project workdir) keeps its parent workspace
        # identity instead of collapsing onto the project subdir. An unstamped pane
        # (normal `mozyo`, pre-#12658) falls back to cwd-derived inference, so
        # existing behavior is unchanged. ``cwd`` stays the real pane cwd for the
        # project-scope gate.
        stamped_repo_root = (pane.get("repo_root_stamp") or "").strip()
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
                repo_root=stamped_repo_root or infer_repo_root(cwd),
                agent_kind=agent_kind,
                ambiguous=window_ambiguous or resolution.ambiguous,
                role_source=resolution.role_source,
                confidence=resolution.confidence,
                lane_id=pane.get("lane_id") or "",
                lane_label=(pane.get("lane_label") or "").strip() or None,
                lane_kind=(pane.get("lane_kind") or "").strip(),
                delegation_parent=(pane.get("delegation_parent") or "").strip(),
                project_scope=(pane.get("project_scope") or "").strip(),
                project_path=(pane.get("project_path") or "").strip(),
                project_label=(pane.get("project_label") or "").strip(),
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


# --- Compact target discovery (Redmine #11811) --------------------------------

# Sanitized host placeholder. Host-aware display (local vs remote SSH) is
# #11817 scope; until then every candidate reports a generic `local` origin so
# no private hostname leaks into compact output or any durable record.
HOST_LOCAL = "local"

# Project-scope provenance vocabulary (Redmine #12985): names HOW a candidate's
# project scope was obtained so an operator/LLM reading `agents targets` can
# tell a stamped cockpit pane from a scope derived by the bounded live scan,
# and both from a pane with no scope at all. Display / diagnostic metadata
# only — never a routing key. `stamped` = the cockpit-stamped
# `@mozyo_project_scope` pane option was authoritative; `live_scan` = the scope
# was derived from the pane cwd via the injected bounded project-scope
# discovery resolver; `unresolved` = no project scope applies (single-repo
# workspace pane) or no resolver could bind one. The vocabulary may grow (e.g.
# a cross-process `cache` source) without breaking consumers — treat unknown
# values as diagnostic text.
PROJECT_SCOPE_SOURCE_STAMPED = "stamped"
PROJECT_SCOPE_SOURCE_LIVE_SCAN = "live_scan"
PROJECT_SCOPE_SOURCE_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class TargetCandidate:
    """One handoff target the LLM/operator can choose, with disambiguators.

    A compact, privacy-aware projection of a folded :class:`AgentRecord` plus
    resolved workspace identity and checkout lane (Redmine #11811). It exposes
    exactly the fields needed to pick an explicit ``pane_id`` without parsing
    pane titles, and reuses the #11822 role resolver's ``role_source`` /
    ``confidence`` / ``ambiguous`` so an unsafe or ambiguous target is visible
    rather than silently selected. ``repo_short`` is the checkout basename for
    compact text; the absolute ``repo_root`` / ``cwd`` ride in JSON only (the
    same exposure ``agents list`` already allows).

    The ``view_kind`` (Redmine #11907) names how this target is projected —
    ``cockpit_pane`` for a managed cockpit pane, ``normal_window`` for the
    compatibility ``mozyo`` rail — so normal local and cockpit share one target
    vocabulary while staying a *projection* attribute, never a routing identity.
    ``branch`` is the checkout's current git branch (best-effort, ``None`` for a
    non-git / detached checkout). :meth:`to_dict` renders the nested canonical
    ``TargetRecord`` projection (host / runtime / identity / repo / view) defined
    in ``vibes/docs/logics/unit-target-model.md``; it is a CLI/API projection,
    not a persisted record.
    """

    pane_id: str
    role: str
    role_source: str
    confidence: str
    ambiguous: bool
    session: str
    window_name: str
    window_index: str
    pane_index: str
    active: bool
    workspace_id: str | None
    workspace_label: str | None
    lane_id: str
    lane_label: str | None
    repo_short: str | None
    repo_root: str | None
    cwd: str
    host: str
    view_kind: str
    branch: str | None
    # Delegated-coordinator-tree display breadcrumb (Redmine #12466). Raw
    # projection-cache values read from the pane's `@mozyo_lane_kind` /
    # `@mozyo_delegation_parent` options; the derived KIND / DEPTH / PARENT /
    # ROOT projection is computed by :func:`derive_targets_delegation` (it walks
    # the parent chain across all candidates), not stored per-pane. Defaulted so
    # existing constructors stay valid. Never a routing identity: these are not
    # part of the canonical ``TargetRecord`` :meth:`to_dict` projection used for
    # handoff, exactly like the additive attention projection (#11952).
    lane_kind: str = ""
    delegation_parent: str = ""
    # Project-scoped cockpit identity (Redmine #12658). A monorepo project subdir
    # is a routing / presentation scope *under* the workspace identity; carried
    # here so ``agents targets`` can show ``workspace_label`` and ``project_scope``
    # simultaneously. Projection metadata only — never a routing key (the
    # ``--target-repo`` gate stays Git-repo-root-anchored). Defaulted so existing
    # constructors stay valid; empty for a single-repo workspace pane.
    project_scope: str = ""
    project_path: str = ""
    project_label: str = ""
    # Provenance of the project scope above (Redmine #12985): one of the
    # PROJECT_SCOPE_SOURCE_* values. Defaulted (like the #12658 fields) so
    # existing constructors stay valid; an unset value reads as `unresolved` in
    # the projection. Diagnostic metadata only — never a routing key.
    project_scope_source: str = ""

    def to_dict(self) -> dict[str, object]:
        """Nested canonical ``TargetRecord`` projection (Redmine #11907).

        Groups the flat fields into the ``host`` / ``runtime`` / ``identity`` /
        ``repo`` / ``view`` shape from ``unit-target-model.md`` so cockpit and
        normal local targets read with one vocabulary. JSON is a projection, not
        a saved file — callers must not persist this per target.
        """
        is_cockpit = self.view_kind == VIEW_KIND_COCKPIT_PANE
        return {
            "host": {"id": self.host, "label": self.host, "kind": "local"},
            "runtime": {
                "provider": "tmux",
                "session": self.session,
                "window": self.window_name,
                "window_index": self.window_index,
                "pane_index": self.pane_index,
                "pane_id": self.pane_id,
                "cwd": self.cwd,
            },
            "identity": {
                "workspace_id": self.workspace_id,
                "workspace_label": self.workspace_label,
                "lane_id": self.lane_id,
                "lane_label": self.lane_label,
                "role": self.role,
                "role_source": self.role_source,
                "confidence": self.confidence,
                "ambiguous": self.ambiguous,
                # Project scope rides under workspace identity (Redmine #12658):
                # null for a single-repo workspace pane, so existing consumers see
                # an additive key, never a changed one.
                "project_scope": self.project_scope or None,
                "project_path": self.project_path or None,
                "project_label": self.project_label or None,
                # Additive provenance for the project scope (Redmine #12985):
                # always a string ("stamped" / "live_scan" / "unresolved") so a
                # consumer can distinguish a stamped cockpit scope from a
                # live-scan-derived one without re-deriving. Diagnostic only.
                "project_scope_source": self.project_scope_source
                or PROJECT_SCOPE_SOURCE_UNRESOLVED,
            },
            "repo": {
                "label": self.repo_short,
                "root": self.repo_root,
                "branch": self.branch,
            },
            "view": {
                "kind": self.view_kind,
                # The cockpit session doubles as the display group; a normal
                # window has no cross-workspace group.
                "group": self.session if is_cockpit else None,
                "active": self.active,
            },
        }


def _normalize_lane_display(value: str | None) -> str:
    """Empty / missing lane -> the backward-compatible ``default`` lane (#11820)."""
    return (value or "").strip() or "default"


def _derive_view_kind(role_source: str) -> str:
    """Projection kind for a target from its role resolver provenance (#11907).

    A pane whose role resolved from the ``@mozyo_agent_role`` option is a
    managed / cockpit pane (Redmine #11822, journal #57116): its window name is a
    layout attribute, so it projects as ``cockpit_pane``. Every other source
    (``window_name`` legacy rail or an ``inferred`` process) is the compatibility
    ``normal_window`` projection. This reads provenance, never the cockpit
    session name, so a renamed cockpit session still classifies correctly and the
    window name never becomes a primary identity.
    """
    if role_source == ROLE_SOURCE_PANE_OPTION:
        return VIEW_KIND_COCKPIT_PANE
    return VIEW_KIND_NORMAL_WINDOW


def build_target_candidates(
    records: Iterable[AgentRecord],
    *,
    resolve_workspace: Callable[[str], tuple[str | None, str | None]] | None = None,
    resolve_branch: Callable[[str], str | None] | None = None,
    resolve_project: Callable[[str | None, str], tuple[str, str, str] | None] | None = None,
    host: str = HOST_LOCAL,
) -> list[TargetCandidate]:
    """Project folded agent records into canonical target candidates (#11811, #11907).

    ``records`` are already folded by ``pane_id`` (one row per agent). Only
    classified agents (``claude`` / ``codex``) are emitted — an ``unknown`` pane
    is not a handoff target. ``resolve_workspace(repo_root)`` returns
    ``(workspace_id, workspace_label)`` for the checkout (registry → anchor →
    derivation, same chain as the inventory) and ``resolve_branch(repo_root)``
    returns the current git branch (``None`` when unknown); each is invoked once
    per distinct repo root. The lane is read from the pane's ``@mozyo_lane_id``
    option (``default`` when absent), and ``view_kind`` is derived from the role
    resolver provenance so cockpit and normal panes share one vocabulary. This is
    pure: no tmux, no git, no registry I/O of its own — all I/O rides on the
    injected resolvers.

    Listing is deliberately non-selecting — same-role candidates stay
    distinguishable by workspace / lane / pane_id and the caller must choose an
    explicit pane, so a natural name can never auto-cross a safety boundary.
    """
    workspace_cache: dict[str, tuple[str | None, str | None]] = {}
    branch_cache: dict[str, str | None] = {}

    def workspace_for(repo_root: str | None) -> tuple[str | None, str | None]:
        if resolve_workspace is None or not repo_root:
            return (None, None)
        if repo_root not in workspace_cache:
            workspace_cache[repo_root] = resolve_workspace(repo_root)
        return workspace_cache[repo_root]

    def branch_for(repo_root: str | None) -> str | None:
        if resolve_branch is None or not repo_root:
            return None
        if repo_root not in branch_cache:
            branch_cache[repo_root] = resolve_branch(repo_root)
        return branch_cache[repo_root]

    def project_for(record: AgentRecord) -> tuple[str, str, str, str]:
        # A stamped pane option (cockpit-managed pane) is authoritative; an
        # un-stamped pane (normal `mozyo`) derives its scope from the cwd via the
        # injected resolver so a pane running inside a project subdir still
        # projects its scope. Empty scope when none applies — the single-repo
        # workspace stays unchanged. The fourth element names the provenance
        # (#12985) so the projection can say WHICH of these branches bound the
        # scope without the consumer re-deriving it.
        if record.project_scope:
            return (
                record.project_scope,
                record.project_path,
                record.project_label,
                PROJECT_SCOPE_SOURCE_STAMPED,
            )
        if resolve_project is not None and record.cwd:
            derived = resolve_project(record.repo_root, record.cwd)
            if derived is not None:
                return (*derived, PROJECT_SCOPE_SOURCE_LIVE_SCAN)
        return ("", "", "", PROJECT_SCOPE_SOURCE_UNRESOLVED)

    candidates: list[TargetCandidate] = []
    for record in records:
        if record.agent_kind == AGENT_KIND_UNKNOWN:
            continue
        workspace_id, workspace_label = workspace_for(record.repo_root)
        repo_short = Path(record.repo_root).name if record.repo_root else None
        project_scope, project_path, project_label, project_scope_source = (
            project_for(record)
        )
        candidates.append(
            TargetCandidate(
                pane_id=record.pane_id,
                role=record.agent_kind,
                role_source=record.role_source,
                confidence=record.confidence,
                ambiguous=record.ambiguous,
                session=record.session,
                window_name=record.window_name,
                window_index=record.window_index,
                pane_index=record.pane_index,
                active=record.pane_active,
                workspace_id=workspace_id,
                workspace_label=workspace_label,
                lane_id=_normalize_lane_display(record.lane_id),
                lane_label=record.lane_label,
                repo_short=repo_short,
                repo_root=record.repo_root,
                cwd=record.cwd,
                host=host,
                view_kind=_derive_view_kind(record.role_source),
                branch=branch_for(record.repo_root),
                lane_kind=record.lane_kind,
                delegation_parent=record.delegation_parent,
                project_scope=project_scope,
                project_path=project_path,
                project_label=project_label,
                project_scope_source=project_scope_source,
            )
        )
    return candidates


# --- Explicit-pane handoff preflight projection (Redmine #11908) --------------


@dataclass(frozen=True)
class PreflightTarget:
    """Canonical ``TargetRecord``-shaped identity view of one resolved handoff pane.

    The handoff explicit-pane preflight (Redmine #11908,
    ``vibes/docs/logics/unit-target-model.md`` "Resolver priority") projects the
    single pane it resolved onto the **same** role / view vocabulary
    ``agents targets`` uses (:func:`build_target_candidates`, #11907) instead of
    re-deriving role from raw window-name fields. So normal local and cockpit
    panes share one resolver rather than growing two.

    The pane option (``@mozyo_agent_role`` / ``@mozyo_workspace_id`` /
    ``@mozyo_lane_id``) is the primary identity; the window name is a
    compatibility fallback tagged ``role_source == window_name``; an ambiguous or
    ``unknown`` role is surfaced so the caller fails closed. ``view_kind`` is a
    projection attribute (``cockpit_pane`` vs ``normal_window``), never a routing
    identity — a renamed cockpit session still classifies because the kind is
    derived from role provenance, not the session/window name.
    """

    pane_id: str
    role: str
    role_source: str
    confidence: str
    ambiguous: bool
    view_kind: str
    workspace_id: str | None
    lane_id: str
    window_name: str
    pane_option_role: str

    def binds_receiver(self, receiver: str) -> bool:
        """True when this target *strongly, non-ambiguously* resolves to ``receiver``.

        The single role-binding predicate the queue-enter explicit-pane gate
        relies on. A weak (process-inferred) signal, an ambiguous resolution, or
        a role that does not equal ``receiver`` is never bound — so a marker-miss
        Enter under the relaxed rail can not land in the wrong receiver's pane.
        Identical semantics to the inline check it replaces; the safety preflight
        is not weakened, only routed through the canonical projection.
        """
        return (
            self.role == receiver
            and self.confidence == CONFIDENCE_STRONG
            and not self.ambiguous
        )


def project_preflight_target(pane: dict[str, str]) -> PreflightTarget:
    """Project a resolved pane dict onto the canonical preflight ``TargetRecord``.

    Pure over the fields :func:`mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.pane_lines`
    emits. Reuses the #11822 role resolver (pane-option primary, window-name
    fallback, process weak) and the #11907 :func:`_derive_view_kind` so the
    handoff preflight and ``agents targets`` never grow two divergent resolvers.

    Unlike :func:`build_target_candidates`, an ``unknown``-role pane is **kept**
    (not dropped) so the explicit-pane preflight can fail closed with full
    provenance (``role`` / ``role_source`` / ``confidence`` / ``ambiguous`` /
    ``view_kind``) instead of silently losing the target.
    """
    resolution = resolve_agent_role(
        pane_option_role=pane.get("agent_role"),
        window_name=pane.get("window_name"),
        process=pane.get("command"),
    )
    return PreflightTarget(
        pane_id=pane.get("id") or "",
        role=resolution.role,
        role_source=resolution.role_source,
        confidence=resolution.confidence,
        ambiguous=resolution.ambiguous,
        view_kind=_derive_view_kind(resolution.role_source),
        workspace_id=(pane.get("workspace_id") or "").strip() or None,
        lane_id=_normalize_lane_display(pane.get("lane_id")),
        window_name=pane.get("window_name") or "",
        pane_option_role=(pane.get("agent_role") or "").strip(),
    )
