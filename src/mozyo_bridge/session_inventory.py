"""Cross-workspace session inventory (Redmine #11422).

``mozyo-bridge session list`` enumerates every running mozyo session / agent
pane across workspaces so operators and external UIs can discover existing
sessions safely. Three design constraints from the parent UserStory
(Redmine #11421) shape this module:

- **SQLite is a durable cache / index, never the source of truth.** Live
  tmux session / window / pane / process state is collected from the tmux
  runtime on every listing; the snapshot in
  ``${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/inventory.sqlite`` only serves the
  degraded path (tmux unavailable) and is clearly marked ``stale`` there.
  The cache lives in its own database file — the workspace registry
  (``registry.sqlite``, Redmine #11429) is an identity source of truth whose
  contract forbids runtime tmux state, and this module must not weaken that
  separation. Losing ``inventory.sqlite`` loses nothing but the offline
  fallback; the next runtime listing rebuilds it.
- **Identity is keyed by ``pane_id``** (Redmine #11628, owner agreement
  2026-06-11). In tmux session groups the same pane belongs to several
  sessions, so a per-(session, pane) row would double-count agents. Each
  inventory record is one pane; its memberships are folded into ``views``,
  and the canonical view is the one whose session matches the workspace's
  resolved canonical session name. This assumes a single tmux server; a
  multi-server deployment would need a ``(socket, pane_id)`` composite key.
- **Path identity absorbs Unicode normalization differences** (Redmine
  #11625). macOS readdir yields NFD path bytes while document- or
  agent-supplied paths are NFC, so registry lookups here compare through
  the shared ``shared.paths.normalize_path_unicode`` helper instead of raw
  bytes — the same helper the session-name hash derivation uses.

Workspace identity per pane resolves registry → anchor → derivation, the
same layering as ``workspace_registry.resolve_canonical_session``; when the
home registry / inventory cache is lost, the runtime listing rebuilds the
same identities from each workspace's local anchor (or path derivation), so
an ephemeral home directory degrades gracefully instead of losing the
inventory.

This is a generic inventory surface for any external consumer; it is
deliberately not a backend for a specific VS Code extension or
tmux-integrated launcher.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from mozyo_bridge.domain.agent_discovery import (
    CONFIDENCE_NONE,
    PaneView,
    ROLE_SOURCE_UNKNOWN,
    discover_agents,
    fold_agents_by_pane,
)
from mozyo_bridge.domain.session_naming import (
    SOURCE_REPO_FALLBACK,
    derive_session_name,
    derive_session_name_without_defaults,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home, normalize_path_unicode
from mozyo_bridge.workspace_registry import (
    SOURCE_HOME_REGISTRY,
    SOURCE_WORKSPACE_ANCHOR,
    list_workspaces,
    read_anchor,
)

INVENTORY_FILENAME = "inventory.sqlite"
# v2 (Redmine #11822): adds role_source / confidence columns. The cache is a
# regenerable index, so an older v1 cache is dropped and rebuilt at v2 on the
# next write rather than migrated in place; a newer-than-current cache is left
# untouched so a downgraded CLI never destroys it.
INVENTORY_SCHEMA_VERSION = 2

# How the snapshot handed to the caller was produced.
SOURCE_RUNTIME = "runtime"
SOURCE_CACHE = "cache"

_PANES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS panes (
    pane_id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    window_index TEXT NOT NULL,
    window_name TEXT NOT NULL,
    pane_index TEXT NOT NULL,
    pane_active INTEGER NOT NULL,
    process TEXT NOT NULL,
    cwd TEXT NOT NULL,
    repo_root TEXT,
    agent_kind TEXT NOT NULL,
    workspace_id TEXT,
    canonical_session TEXT,
    project_name TEXT,
    identity_source TEXT,
    views_json TEXT NOT NULL,
    role_source TEXT,
    confidence TEXT
)
"""

_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS inventory_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_PANE_COLUMNS = (
    "pane_id, session, window_index, window_name, pane_index, pane_active, "
    "process, cwd, repo_root, agent_kind, workspace_id, canonical_session, "
    "project_name, identity_source, views_json, role_source, confidence"
)
_PANE_COLUMN_COUNT = 17


def inventory_path(home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / INVENTORY_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class WorkspaceIdentity:
    """The workspace identity a pane's repo root resolves to.

    ``source`` is ``home-registry`` / ``workspace-anchor`` or one of the
    derivation markers from ``domain.session_naming`` — the same layering
    (and the same fallback story) as the workspace registry: when the home
    registry is gone, anchors and derivation still reproduce the identity.
    """

    workspace_id: str | None
    canonical_session: str
    project_name: str | None
    source: str

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "canonical_session": self.canonical_session,
            "project_name": self.project_name,
            "source": self.source,
        }


@dataclass(frozen=True)
class InventoryRecord:
    """One agent pane in the inventory, keyed by ``pane_id`` (Redmine #11628).

    The top-level session / window / pane fields describe the canonical
    view; every membership (including the canonical one) is in ``views``.
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
    # Role resolver provenance (Redmine #11822): which signal classified the
    # agent kind and how strong it was. Defaults keep legacy construction
    # working and make a cache miss / older payload degrade to "unknown/none".
    role_source: str = ROLE_SOURCE_UNKNOWN
    confidence: str = CONFIDENCE_NONE
    workspace: WorkspaceIdentity | None = None
    views: tuple[PaneView, ...] = ()
    # OTel activity join (Redmine #11675). Computed at query time from the
    # otel event store, NEVER persisted in the inventory cache — the two
    # caches stay independent and the activity is always as fresh as the
    # store. None means "not yet attached"; the payload then reports the
    # unknown state, which by contract degrades to tmux liveness.
    activity: dict | None = None

    def as_payload(self) -> dict:
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
            "role_source": self.role_source,
            "confidence": self.confidence,
            "workspace": self.workspace.as_payload() if self.workspace else None,
            "views": [view.as_payload() for view in self.views],
            "activity": self.activity
            or {"state": "unknown", "last_event_at": None, "source": None},
        }


@dataclass(frozen=True)
class InventorySnapshot:
    """The inventory handed to the CLI: records plus provenance.

    ``source`` is :data:`SOURCE_RUNTIME` when collected live from tmux, or
    :data:`SOURCE_CACHE` when degraded to the SQLite snapshot; ``stale`` is
    true exactly on the cache branch. ``notes`` carries non-fatal
    degradations (cache unwritable, cache recreated, ...).
    """

    records: tuple[InventoryRecord, ...]
    collected_at: str | None
    source: str
    stale: bool
    inventory_path: Path
    notes: tuple[str, ...] = ()

    def as_payload(self) -> dict:
        return {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "collected_at": self.collected_at,
            "source": self.source,
            "stale": self.stale,
            "inventory_path": str(self.inventory_path),
            "notes": list(self.notes),
            "panes": [record.as_payload() for record in self.records],
        }


# --- workspace identity resolution -------------------------------------------


def _fallback_session_name_without_defaults(root: Path) -> str:
    """Path-derived session name without reading workspace-local defaults."""
    return derive_session_name_without_defaults(root).name


def _resolve_identity(
    repo_root: str, registry_by_path: dict[str, object], *, derive_unregistered: bool
) -> WorkspaceIdentity:
    """Resolve a pane's repo root to its workspace identity.

    Same registry → anchor → derivation layering as
    ``resolve_canonical_session``, with two inventory-specific differences:
    the registry lookup is NFC-normalized (Redmine #11625) and the registry
    rows are pre-indexed so a listing resolves each unique root once instead
    of re-opening SQLite per pane.
    """
    record = registry_by_path.get(normalize_path_unicode(repo_root))
    if record is not None:
        return WorkspaceIdentity(
            workspace_id=record.workspace_id,
            canonical_session=record.canonical_session,
            project_name=record.project_name,
            source=SOURCE_HOME_REGISTRY,
        )
    root = Path(repo_root)
    anchor = read_anchor(root)
    if anchor is not None:
        name = anchor.get("project_name")
        return WorkspaceIdentity(
            workspace_id=anchor["workspace_id"],
            canonical_session=anchor["canonical_session"],
            project_name=name if isinstance(name, str) and name.strip() else None,
            source=SOURCE_WORKSPACE_ANCHOR,
        )
    if not derive_unregistered:
        return WorkspaceIdentity(
            workspace_id=None,
            canonical_session=_fallback_session_name_without_defaults(root),
            project_name=None,
            source=SOURCE_REPO_FALLBACK,
        )
    derived = derive_session_name(root)
    return WorkspaceIdentity(
        workspace_id=None,
        canonical_session=derived.name,
        project_name=None,
        source=derived.source,
    )


def _registry_index(home: Path | None) -> dict[str, object]:
    return {
        normalize_path_unicode(record.canonical_path): record
        for record in list_workspaces(home=home)
    }


# --- runtime collection -------------------------------------------------------


def collect_runtime_inventory(
    panes: Iterable[dict[str, str]],
    *,
    home: Path | None = None,
    derive_unregistered: bool = True,
) -> list[InventoryRecord]:
    """Build inventory records from raw tmux pane lines.

    Discovery and pane_id folding are shared with ``agents list``
    (``domain.agent_discovery.fold_agents_by_pane``, Redmine #11628) so the
    two surfaces cannot drift; this function adds the workspace identity
    layer on top, resolving each distinct repo root once through the
    registry → anchor → derivation chain (Unicode-normalized, #11625).
    """
    registry_by_path = _registry_index(home)
    identity_cache: dict[str, WorkspaceIdentity] = {}

    def identity_for(repo_root: str) -> WorkspaceIdentity:
        key = normalize_path_unicode(repo_root)
        if key not in identity_cache:
            identity_cache[key] = _resolve_identity(
                repo_root,
                registry_by_path,
                derive_unregistered=derive_unregistered,
            )
        return identity_cache[key]

    folded = fold_agents_by_pane(
        discover_agents(list(panes)),
        resolve_canonical=lambda root: identity_for(root).canonical_session,
    )
    return [
        InventoryRecord(
            pane_id=agent.pane_id,
            session=agent.session,
            window_index=agent.window_index,
            window_name=agent.window_name,
            pane_index=agent.pane_index,
            pane_active=agent.pane_active,
            process=agent.process,
            cwd=agent.cwd,
            repo_root=agent.repo_root,
            agent_kind=agent.agent_kind,
            role_source=agent.role_source,
            confidence=agent.confidence,
            workspace=identity_for(agent.repo_root) if agent.repo_root else None,
            views=agent.views,
        )
        for agent in folded
    ]


# --- durable cache (SQLite) ----------------------------------------------------


def _record_to_row(record: InventoryRecord) -> tuple:
    return (
        record.pane_id,
        record.session,
        record.window_index,
        record.window_name,
        record.pane_index,
        1 if record.pane_active else 0,
        record.process,
        record.cwd,
        record.repo_root,
        record.agent_kind,
        record.workspace.workspace_id if record.workspace else None,
        record.workspace.canonical_session if record.workspace else None,
        record.workspace.project_name if record.workspace else None,
        record.workspace.source if record.workspace else None,
        json.dumps(
            [view.as_payload() for view in record.views], ensure_ascii=False
        ),
        record.role_source,
        record.confidence,
    )


def _row_to_record(row: tuple) -> InventoryRecord:
    views = tuple(
        PaneView(
            session=view.get("session", ""),
            window_index=view.get("window_index", ""),
            window_name=view.get("window_name", ""),
            pane_index=view.get("pane_index", ""),
            pane_active=bool(view.get("pane_active")),
            canonical=bool(view.get("canonical")),
        )
        for view in json.loads(row[14])
        if isinstance(view, dict)
    )
    workspace = None
    if row[11] is not None:
        workspace = WorkspaceIdentity(
            workspace_id=row[10],
            canonical_session=row[11],
            project_name=row[12],
            source=row[13] or "",
        )
    return InventoryRecord(
        pane_id=row[0],
        session=row[1],
        window_index=row[2],
        window_name=row[3],
        pane_index=row[4],
        pane_active=bool(row[5]),
        process=row[6],
        cwd=row[7],
        repo_root=row[8],
        agent_kind=row[9],
        role_source=row[15] or ROLE_SOURCE_UNKNOWN,
        confidence=row[16] or CONFIDENCE_NONE,
        workspace=workspace,
        views=views,
    )


def save_snapshot(
    records: Iterable[InventoryRecord],
    *,
    home: Path | None = None,
    now: str | None = None,
) -> tuple[Path, list[str]]:
    """Replace the cached snapshot with ``records``. Best-effort by contract.

    The inventory file is a regenerable cache, so unlike the workspace
    registry a corrupt file is moved out of the way (recreated) instead of
    dying — but a cache written by a *newer* schema is left untouched so a
    downgraded CLI never destroys it. All degradations are returned as
    notes, never raised: failing to persist the cache must not fail the
    runtime listing it was derived from.
    """
    path = inventory_path(home)
    notes: list[str] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in ("open", "recreate"):
        try:
            conn = sqlite3.connect(path)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > INVENTORY_SCHEMA_VERSION:
                    notes.append(
                        f"inventory cache {path} has schema version {version}; "
                        "left untouched (snapshot not persisted)"
                    )
                    return path, notes
                with conn:
                    if version != INVENTORY_SCHEMA_VERSION:
                        # Fresh (0) or an older known version: the cache is a
                        # regenerable index, so rebuild the table at the current
                        # schema rather than ALTER-migrate in place.
                        conn.execute("DROP TABLE IF EXISTS panes")
                        conn.execute(_PANES_TABLE_SQL)
                        conn.execute(_META_TABLE_SQL)
                        conn.execute(
                            f"PRAGMA user_version = {INVENTORY_SCHEMA_VERSION}"
                        )
                    conn.execute("DELETE FROM panes")
                    conn.executemany(
                        f"INSERT INTO panes ({_PANE_COLUMNS}) "
                        f"VALUES ({', '.join('?' * _PANE_COLUMN_COUNT)})",
                        [_record_to_row(record) for record in records],
                    )
                    conn.execute(
                        "INSERT INTO inventory_meta (key, value) "
                        "VALUES ('collected_at', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (now or _utc_now(),),
                    )
                return path, notes
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            if attempt == "open":
                notes.append(
                    f"inventory cache {path} was unreadable ({exc}); recreated"
                )
                try:
                    path.unlink(missing_ok=True)
                except OSError as unlink_exc:
                    notes.append(
                        f"could not remove corrupt inventory cache: {unlink_exc}"
                    )
                    return path, notes
            else:
                notes.append(f"inventory cache not persisted: {exc}")
    return path, notes


def load_snapshot(
    *, home: Path | None = None
) -> tuple[list[InventoryRecord], str | None] | None:
    """Read the cached snapshot. Returns ``None`` when no usable cache exists.

    Missing / corrupt / unknown-version caches all degrade to ``None`` —
    the cache is never authoritative, so there is nothing to repair here.
    """
    path = inventory_path(home)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != INVENTORY_SCHEMA_VERSION:
                return None
            rows = conn.execute(
                f"SELECT {_PANE_COLUMNS} FROM panes ORDER BY pane_id"
            ).fetchall()
            meta = conn.execute(
                "SELECT value FROM inventory_meta WHERE key = 'collected_at'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None
    try:
        records = [_row_to_record(row) for row in rows]
    except (ValueError, KeyError):
        return None
    return records, meta[0] if meta else None


# --- OTel activity join (Redmine #11675) ----------------------------------------


def attach_activity(
    records: list[InventoryRecord], *, home: Path | None = None
) -> list[InventoryRecord]:
    """Join OTel activity onto inventory records by bootstrap hints.

    Join key: an OTel source's bootstrap-injected (``mozyo.session``,
    ``mozyo.agent``) against each pane's view sessions plus its
    ``agent_kind`` (under the 1-agent-1-window model this pair is unique
    per workspace session; grouped sessions are already folded). Records
    without a matching source — and panes that share a (session, kind)
    pair with another pane, which cannot be disambiguated honestly — get
    the ``unknown`` state, which callers treat as "consult tmux liveness",
    never as death. Best-effort throughout: a missing / corrupt store
    yields unknown for everyone and never fails the listing.
    """
    from dataclasses import replace

    from mozyo_bridge.domain.agent_activity import summarize_activity
    from mozyo_bridge.otel_store import OtelEventStore

    # Multiple OTel sources can legitimately share one join key: the agent
    # CLI mints a new session.id on every restart while the tmux pane (and
    # therefore the bootstrap hints) stays the same. The newest event wins
    # deterministically — review #56160 caught the last-write-wins indexing
    # letting a stale pre-restart source override the live one.
    activity_by_key: dict[tuple[str, str], dict] = {}
    freshness: dict[tuple[str, str], float] = {}
    for activity in summarize_activity(OtelEventStore(home=home)):
        hints = activity.match_hints
        session = hints.get("session")
        agent = hints.get("agent")
        if not (isinstance(session, str) and isinstance(agent, str)):
            continue
        key = (session, agent)
        # seconds_since_event is computed uniformly for every record;
        # smaller = newer. A record with no parseable timestamp can never
        # displace one that has one.
        age = (
            activity.seconds_since_event
            if activity.seconds_since_event is not None
            else float("inf")
        )
        if key in freshness and age >= freshness[key]:
            continue
        freshness[key] = age
        activity_by_key[key] = {
            "state": activity.state,
            "last_event_at": activity.last_event_at,
            "last_event_name": activity.last_event_name,
            "source": "otel",
        }

    # A (session, agent_kind) pair claimed by more than one pane cannot be
    # attributed honestly — leave all claimants unknown.
    pair_counts: dict[tuple[str, str], int] = {}
    for record in records:
        for view in record.views:
            pair = (view.session, record.agent_kind)
            pair_counts[pair] = pair_counts.get(pair, 0) + 1

    out: list[InventoryRecord] = []
    for record in records:
        attached: dict | None = None
        for view in record.views:
            pair = (view.session, record.agent_kind)
            if pair_counts.get(pair, 0) == 1 and pair in activity_by_key:
                attached = activity_by_key[pair]
                break
        out.append(
            replace(
                record,
                activity=attached
                or {"state": "unknown", "last_event_at": None, "source": None},
            )
        )
    return out


# --- orchestration --------------------------------------------------------------


def take_inventory(
    *,
    home: Path | None = None,
    panes: Iterable[dict[str, str]] | None = None,
    derive_unregistered: bool = True,
    persist: bool = True,
) -> InventorySnapshot:
    """Collect the live inventory, refreshing the cache; degrade to cache.

    ``panes=None`` means "ask the tmux runtime". When tmux is unavailable
    (no binary, no server) the cached snapshot is returned with
    ``stale=True``; when there is no cache either, an empty stale snapshot
    is returned — the caller decides how loudly to surface that.
    """
    if panes is None:
        from mozyo_bridge.infrastructure.tmux_client import try_pane_lines

        panes = try_pane_lines()
    if panes is not None:
        now = _utc_now()
        records = collect_runtime_inventory(
            panes, home=home, derive_unregistered=derive_unregistered
        )
        path = inventory_path(home)
        notes: tuple[str, ...] = ()
        if persist:
            path, notes = save_snapshot(records, home=home, now=now)
        records = attach_activity(records, home=home)
        return InventorySnapshot(
            records=tuple(records),
            collected_at=now,
            source=SOURCE_RUNTIME,
            stale=False,
            inventory_path=path,
            notes=tuple(notes),
        )
    cached = load_snapshot(home=home)
    if cached is not None:
        records, collected_at = cached
        records = attach_activity(records, home=home)
        return InventorySnapshot(
            records=tuple(records),
            collected_at=collected_at,
            source=SOURCE_CACHE,
            stale=True,
            inventory_path=inventory_path(home),
            notes=("tmux unavailable; serving the last cached snapshot",),
        )
    return InventorySnapshot(
        records=(),
        collected_at=None,
        source=SOURCE_CACHE,
        stale=True,
        inventory_path=inventory_path(home),
        notes=("tmux unavailable and no cached snapshot exists",),
    )
