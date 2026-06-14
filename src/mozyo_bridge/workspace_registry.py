"""Home-registry-first workspace registration (Redmine #11429).

Before this module, a workspace's tmux session identity was re-derived from
its path on every invocation (`domain.session_naming.derive_session_name`).
That works until the derivation inputs move underneath the name: the operator
adds / edits `.mozyo-bridge/workspace-defaults.yaml`, the workspace is
relocated, or a non-git / dev-container workspace cannot keep stable
derivation inputs at all. The home registry makes the *first* derived name a
durable identity instead of a per-call computation:

- the **home registry** (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/registry.sqlite`)
  is the source of truth for workspace identity: workspace id, canonical /
  display path, readable project name, canonical session name, preset /
  rules version. Live tmux window / pane / process state is deliberately NOT
  stored here — the only runtime-adjacent field is ``last_seen``, kept in a
  separate cache table so identity rows never carry runtime state;
- the **workspace-local anchor** (`<repo>/.mozyo-bridge/workspace.json`) is a
  minimal recovery record. If the home registry disappears (ephemeral home,
  reinstall), re-running ``mozyo-bridge workspace register`` inside the
  workspace restores the same workspace id and canonical session name from
  the anchor. The anchor intentionally does NOT store the workspace path —
  its on-disk location *is* the path, so a copied / moved workspace can never
  present a stale path;
- **resolution order** for the session name is registry → anchor → path
  derivation. Path derivation (`derive_session_name`) remains the
  first-registration and fallback behavior, so a workspace that never
  registered behaves exactly as before (Redmine #10796 semantics).

Reads are strictly read-only: resolving a session name never creates the
registry, never writes ``last_seen``, and never touches the anchor. All
writes go through :func:`register_workspace`, which has two callers: the
explicit ``mozyo-bridge workspace register`` CLI (manual, idempotent) and
smart ``mozyo-bridge init`` (Redmine #11427), which registers an
unregistered workspace as part of a guarded adoption — after its
fail-closed preflight checks and before any tmux/vscode mutation. The
session resolution step inside ``init`` is itself read-only; the
registration is a separate, explicit write.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mozyo_bridge.domain.session_naming import derive_session_name
from mozyo_bridge.shared.errors import die
from mozyo_bridge.shared.paths import mozyo_bridge_home

REGISTRY_FILENAME = "registry.sqlite"
REGISTRY_SCHEMA_VERSION = 1

ANCHOR_RELATIVE = Path(".mozyo-bridge/workspace.json")
ANCHOR_SCHEMA_VERSION = 1

# Scaffold manifest consulted (best-effort) for preset / rules version.
SCAFFOLD_MANIFEST_RELATIVE = Path(".mozyo-bridge/scaffold.json")

# Resolution-source markers, extending the derivation markers in
# `domain.session_naming` so `session name --json` consumers can tell which
# layer produced the name.
SOURCE_HOME_REGISTRY = "home-registry"
SOURCE_WORKSPACE_ANCHOR = "workspace-anchor"

# Registration outcome markers.
REGISTER_CREATED = "created"
REGISTER_UPDATED = "updated"
REGISTER_RESTORED = "restored-from-anchor"

# Read-only registry-health markers, used by `doctor` (Redmine #11426). These
# classify the home registry without ever creating, writing, or `die()`-ing —
# the opposite contract from `_connect_rw`, which is the write path.
REGISTRY_HEALTH_MISSING = "missing"
REGISTRY_HEALTH_OK = "ok"
REGISTRY_HEALTH_INVALID_SCHEMA = "invalid-schema"
REGISTRY_HEALTH_UNREADABLE = "unreadable"

# A canonical session name read back from the registry / anchor is operator
# editable in principle, so it is validated before being handed to tmux.
# tmux treats `:` as a window separator and `.` as a pane separator; we gate
# down to the same conservative alphabet `derive_session_name` produces.
_SAFE_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_WORKSPACES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    display_path TEXT NOT NULL,
    project_name TEXT NOT NULL,
    canonical_session TEXT NOT NULL,
    preset TEXT,
    preset_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

# `last_seen` lives in its own table: the design constraint (#11425) is that
# the registry separates identity (source of truth) from cache-style runtime
# adjacency. Dropping this table loses nothing but freshness hints.
_ACTIVITY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workspace_activity (
    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id)
        ON DELETE CASCADE,
    last_seen TEXT NOT NULL
)
"""

_SELECT_COLUMNS = (
    "w.workspace_id, w.canonical_path, w.display_path, w.project_name, "
    "w.canonical_session, w.preset, w.preset_version, w.created_at, "
    "w.updated_at, a.last_seen"
)
_SELECT_FROM = (
    "FROM workspaces w LEFT JOIN workspace_activity a "
    "ON a.workspace_id = w.workspace_id"
)


@dataclass(frozen=True)
class WorkspaceRecord:
    """One registered workspace as stored in the home registry."""

    workspace_id: str
    canonical_path: str
    display_path: str
    project_name: str
    canonical_session: str
    preset: str | None
    preset_version: str | None
    created_at: str
    updated_at: str
    last_seen: str | None

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "canonical_path": self.canonical_path,
            "display_path": self.display_path,
            "project_name": self.project_name,
            "canonical_session": self.canonical_session,
            "preset": self.preset,
            "preset_version": self.preset_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_seen": self.last_seen,
        }


@dataclass(frozen=True)
class RegisterResult:
    """Outcome of :func:`register_workspace`."""

    record: WorkspaceRecord
    outcome: str
    registry_path: Path
    anchor_path: Path
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedSession:
    """A session name plus which identity layer produced it.

    ``source`` is :data:`SOURCE_HOME_REGISTRY`, :data:`SOURCE_WORKSPACE_ANCHOR`,
    or one of the derivation markers from `domain.session_naming`.
    ``identifier`` carries the Redmine identifier only on the derivation
    branch (registry / anchor names are opaque identities, not re-derivable).
    """

    name: str
    source: str
    repo_root: Path
    workspace_id: str | None
    identifier: str | None


def registry_path(home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / REGISTRY_FILENAME


def anchor_path(repo_root: Path) -> Path:
    return repo_root / ANCHOR_RELATIVE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_safe_session_name(name: object) -> bool:
    return isinstance(name, str) and bool(_SAFE_SESSION_NAME_RE.match(name))


def _display_path(resolved: Path) -> str:
    """Contract ``$HOME`` to ``~`` for human-facing listings."""
    try:
        return "~/" + resolved.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(resolved)


def read_scaffold_preset(repo_root: Path) -> tuple[str | None, str | None]:
    """Best-effort ``(preset, preset_version)`` from the scaffold manifest.

    Missing / unreadable / non-JSON manifests yield ``(None, None)``;
    registration must work in never-scaffolded workspaces too.
    """
    manifest = repo_root / SCAFFOLD_MANIFEST_RELATIVE
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    preset = raw.get("preset")
    version = raw.get("preset_version")
    return (
        preset if isinstance(preset, str) and preset.strip() else None,
        version if isinstance(version, str) and version.strip() else None,
    )


# --- workspace-local anchor -------------------------------------------------


def read_anchor(repo_root: Path) -> dict | None:
    """Best-effort read of the workspace-local anchor.

    Returns the anchor mapping only when it is structurally valid (mapping,
    supported schema version, non-empty workspace id, tmux-safe canonical
    session). Anything else returns ``None`` — resolution and registration
    must degrade to derivation, never die, on a corrupt anchor.
    """
    path = anchor_path(repo_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != ANCHOR_SCHEMA_VERSION:
        return None
    workspace_id = raw.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        return None
    if not _is_safe_session_name(raw.get("canonical_session")):
        return None
    return raw


def write_anchor(repo_root: Path, record: WorkspaceRecord) -> Path:
    path = anchor_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": ANCHOR_SCHEMA_VERSION,
        "workspace_id": record.workspace_id,
        "canonical_session": record.canonical_session,
        "project_name": record.project_name,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


# --- home registry (SQLite) -------------------------------------------------


def _connect_rw(path: Path) -> sqlite3.Connection:
    """Open the registry for writing, creating file + schema when missing.

    A present-but-corrupt registry dies loudly instead of being silently
    recreated: it is the identity source of truth, and the recovery route
    (delete it, re-register from each workspace's anchor) must be an
    operator decision, not an implicit side effect.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.execute(_WORKSPACES_TABLE_SQL)
            conn.execute(_ACTIVITY_TABLE_SQL)
            conn.execute(f"PRAGMA user_version = {REGISTRY_SCHEMA_VERSION}")
            conn.commit()
        elif version != REGISTRY_SCHEMA_VERSION:
            conn.close()
            die(
                f"workspace registry {path} has schema version {version}, "
                f"but this mozyo-bridge supports version "
                f"{REGISTRY_SCHEMA_VERSION}. Upgrade mozyo-bridge, or move "
                "the registry aside and re-register workspaces from their "
                "anchors (`mozyo-bridge workspace register`)."
            )
        return conn
    except sqlite3.DatabaseError as exc:
        die(
            f"workspace registry {path} is unreadable ({exc}). Move the "
            "corrupt file aside and re-register each workspace from its "
            "local anchor with `mozyo-bridge workspace register`."
        )
    raise AssertionError("unreachable")


def _row_to_record(row: tuple) -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=row[0],
        canonical_path=row[1],
        display_path=row[2],
        project_name=row[3],
        canonical_session=row[4],
        preset=row[5],
        preset_version=row[6],
        created_at=row[7],
        updated_at=row[8],
        last_seen=row[9],
    )


def _read_rows(path: Path, where_sql: str = "", params: tuple = ()) -> list[WorkspaceRecord]:
    """Read registry rows without creating or mutating the database.

    Missing registry → empty list. A corrupt / unreadable registry also
    yields an empty list here: read paths (session-name resolution, status)
    must keep working from the anchor / derivation fallback even when the
    home registry is damaged. Write paths surface the corruption instead.
    """
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT {_SELECT_COLUMNS} {_SELECT_FROM} {where_sql}",
                params,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return []
    return [_row_to_record(row) for row in rows]


def load_workspace_by_path(
    repo_root: Path, *, home: Path | None = None
) -> WorkspaceRecord | None:
    resolved = Path(repo_root).expanduser().resolve()
    rows = _read_rows(
        registry_path(home), "WHERE w.canonical_path = ?", (str(resolved),)
    )
    return rows[0] if rows else None


def load_workspace_by_id(
    workspace_id: str, *, home: Path | None = None
) -> WorkspaceRecord | None:
    rows = _read_rows(
        registry_path(home), "WHERE w.workspace_id = ?", (workspace_id,)
    )
    return rows[0] if rows else None


def list_workspaces(*, home: Path | None = None) -> list[WorkspaceRecord]:
    return _read_rows(registry_path(home), "ORDER BY w.canonical_path")


def inspect_registry_health(home: Path | None = None) -> dict:
    """Read-only health probe of the home registry for ``doctor`` (#11426).

    Unlike :func:`_connect_rw` (the write path, which creates the file/schema
    and ``die()``-s on a present-but-corrupt registry), this never creates the
    registry, never writes, and never exits the process. It classifies the
    registry so ``doctor`` can report a safe error state without mutating
    anything:

    - ``missing`` — the file does not exist (a workspace that never registered
      is a normal, valid state; resolution falls back to derivation);
    - ``ok`` — opens read-only, ``user_version`` matches
      :data:`REGISTRY_SCHEMA_VERSION`, and the ``workspaces`` table is
      queryable;
    - ``invalid-schema`` — opens and is a real database, but ``user_version``
      differs (a future schema this CLI cannot read, or a past one). This is
      the read-side mirror of the write-side schema ``die()``;
    - ``unreadable`` — present but not a usable SQLite database (truncated,
      garbage, or missing the identity table).

    The ``workspaces`` probe query matters: a zero-byte or non-SQLite file can
    still ``connect`` (SQLite opens lazily), so the classification is driven by
    actually reading ``user_version`` and the identity table, not by the
    connect call succeeding.
    """
    path = registry_path(home)
    info: dict = {
        "path": str(path),
        "exists": path.exists(),
        "status": REGISTRY_HEALTH_MISSING,
        "schema_version": None,
        "expected_schema_version": REGISTRY_SCHEMA_VERSION,
    }
    if not path.exists():
        return info
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            # Force a query against the identity table so a file that "opens"
            # but is not a real registry (truncated / missing table) is caught
            # here as unreadable rather than masquerading as schema 0.
            conn.execute("SELECT 1 FROM workspaces LIMIT 1").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        info["status"] = REGISTRY_HEALTH_UNREADABLE
        info["error"] = str(exc)
        return info
    info["schema_version"] = version
    info["status"] = (
        REGISTRY_HEALTH_OK
        if version == REGISTRY_SCHEMA_VERSION
        else REGISTRY_HEALTH_INVALID_SCHEMA
    )
    return info


# --- registration -----------------------------------------------------------


def register_workspace(
    repo_root: Path,
    *,
    home: Path | None = None,
    project_name: str | None = None,
) -> RegisterResult:
    """Create or refresh this workspace's registry row and local anchor.

    Identity precedence on (re-)registration:

    1. an existing **anchor** pins workspace id + canonical session — this is
       what makes the registry recoverable after home loss, and what keeps a
       *moved* workspace the same workspace;
    2. else an existing **registry row** for this canonical path is refreshed
       in place (and its anchor rewritten);
    3. else a **new identity** is minted: random workspace id, canonical
       session from :func:`derive_session_name` (the only point where path
       derivation feeds the durable identity).

    ``project_name`` overrides the readable name; default is the directory
    basename (which may be non-ASCII — readability beats slug purity here).
    """
    resolved = Path(repo_root).expanduser().resolve()
    if not resolved.is_dir():
        die(f"workspace root is not a directory: {resolved}")

    db_path = registry_path(home)
    now = _utc_now()
    notes: list[str] = []

    anchor = read_anchor(resolved)
    existing_by_path = load_workspace_by_path(resolved, home=home)

    if anchor is not None:
        workspace_id = anchor["workspace_id"]
        canonical_session = anchor["canonical_session"]
        existing_by_id = load_workspace_by_id(workspace_id, home=home)
        outcome = REGISTER_UPDATED if existing_by_id is not None else REGISTER_RESTORED
    elif existing_by_path is not None:
        workspace_id = existing_by_path.workspace_id
        canonical_session = existing_by_path.canonical_session
        existing_by_id = existing_by_path
        outcome = REGISTER_UPDATED
        notes.append("anchor was missing; rewritten from the registry row")
    else:
        workspace_id = uuid.uuid4().hex
        canonical_session = derive_session_name(resolved).name
        existing_by_id = None
        outcome = REGISTER_CREATED

    preset, preset_version = read_scaffold_preset(resolved)
    display = _display_path(resolved)

    # Readable name precedence: explicit override > this identity's existing
    # registry row > anchor > directory basename (which may be non-ASCII —
    # readability beats slug purity for a display name).
    anchor_name = anchor.get("project_name") if anchor is not None else None
    if project_name and project_name.strip():
        name = project_name.strip()
    elif existing_by_id is not None and existing_by_id.project_name.strip():
        name = existing_by_id.project_name
    elif isinstance(anchor_name, str) and anchor_name.strip():
        name = anchor_name.strip()
    else:
        name = resolved.name

    conn = _connect_rw(db_path)
    try:
        with conn:
            # A row already holding this canonical path under a DIFFERENT
            # workspace id describes whatever workspace previously lived
            # here; two workspaces cannot occupy one path, so the stale row
            # yields to the anchored identity rather than blocking it.
            stale = conn.execute(
                "SELECT workspace_id FROM workspaces "
                "WHERE canonical_path = ? AND workspace_id != ?",
                (str(resolved), workspace_id),
            ).fetchone()
            if stale:
                conn.execute(
                    "DELETE FROM workspaces WHERE workspace_id = ?", (stale[0],)
                )
                notes.append(
                    f"replaced stale registry row {stale[0]} that previously "
                    "claimed this path"
                )
            previous = conn.execute(
                "SELECT canonical_path, created_at FROM workspaces "
                "WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            created_at = previous[1] if previous else now
            if previous and previous[0] != str(resolved):
                notes.append(
                    f"workspace moved: canonical path updated from "
                    f"{previous[0]} to {resolved}"
                )
            conn.execute(
                "INSERT INTO workspaces (workspace_id, canonical_path, "
                "display_path, project_name, canonical_session, preset, "
                "preset_version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET "
                "canonical_path = excluded.canonical_path, "
                "display_path = excluded.display_path, "
                "project_name = excluded.project_name, "
                "canonical_session = excluded.canonical_session, "
                "preset = excluded.preset, "
                "preset_version = excluded.preset_version, "
                "updated_at = excluded.updated_at",
                (
                    workspace_id,
                    str(resolved),
                    display,
                    name,
                    canonical_session,
                    preset,
                    preset_version,
                    created_at,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO workspace_activity (workspace_id, last_seen) "
                "VALUES (?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET "
                "last_seen = excluded.last_seen",
                (workspace_id, now),
            )
    finally:
        conn.close()

    record = WorkspaceRecord(
        workspace_id=workspace_id,
        canonical_path=str(resolved),
        display_path=display,
        project_name=name,
        canonical_session=canonical_session,
        preset=preset,
        preset_version=preset_version,
        created_at=created_at,
        updated_at=now,
        last_seen=now,
    )
    written_anchor = write_anchor(resolved, record)
    return RegisterResult(
        record=record,
        outcome=outcome,
        registry_path=db_path,
        anchor_path=written_anchor,
        notes=tuple(notes),
    )


# --- resolution -------------------------------------------------------------


def resolve_canonical_session(
    repo_root: Path | str, *, home: Path | None = None
) -> ResolvedSession:
    """Resolve the workspace's session name: registry → anchor → derivation.

    Read-only by contract. A registered canonical session name always wins
    over re-deriving from the path; path derivation is reached only for
    never-registered workspaces (and is then byte-identical to the
    pre-registry behavior of `derive_session_name`).
    """
    resolved = Path(repo_root).expanduser().resolve()

    record = load_workspace_by_path(resolved, home=home)
    if record is not None and _is_safe_session_name(record.canonical_session):
        return ResolvedSession(
            name=record.canonical_session,
            source=SOURCE_HOME_REGISTRY,
            repo_root=resolved,
            workspace_id=record.workspace_id,
            identifier=None,
        )

    anchor = read_anchor(resolved)
    if anchor is not None:
        return ResolvedSession(
            name=anchor["canonical_session"],
            source=SOURCE_WORKSPACE_ANCHOR,
            repo_root=resolved,
            workspace_id=anchor["workspace_id"],
            identifier=None,
        )

    derived = derive_session_name(resolved)
    return ResolvedSession(
        name=derived.name,
        source=derived.source,
        repo_root=resolved,
        workspace_id=None,
        identifier=derived.identifier,
    )
