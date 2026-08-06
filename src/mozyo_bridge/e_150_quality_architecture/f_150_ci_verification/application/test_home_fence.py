"""I/O side of test-process home isolation (Redmine #14757).

Three responsibilities, all of them shared by ``tests run`` / ``tests profile``
/ ``tests parallel`` so the three entry points cannot drift apart:

- :func:`isolated_env` — materialise the task-specific root and return the child
  env (pins from the pure domain module, applied to an inherited env copy).
- :func:`snapshot_home` — a transactionally consistent, value-free logical
  snapshot of a mozyo-bridge home.
- :func:`reexec_isolated` — re-run *this* CLI invocation inside a fenced child.

The snapshot never opens a live database with ``immutable=1`` (#14757 acceptance
5). ``immutable=1`` tells SQLite the file cannot change, which is false for the
operator's home while a cockpit is running, and a torn read of a live WAL
database would be reported as a schema change. Instead each store is opened
read-only (``mode=ro``) and copied with the online backup API into an in-memory
database; the backup runs inside a read transaction, so the copy is a consistent
point-in-time image even while another process is writing. A store that cannot
be opened or copied is recorded as ``unreadable`` and fails the guard rather
than being skipped.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (
    OPERATOR_DEFAULT_HOME,
    HomeSnapshot,
    IsolationLayout,
    apply_isolation,
    digest,
    isolation_env,
)
from mozyo_bridge.shared.paths import (
    HOME_FENCE_DENY_ENV,
    HOME_FENCE_DENY_SEPARATOR,
    HOME_FENCE_ROOT_ENV,
)

#: Filename suffixes treated as home SQLite stores for the schema / identity
#: tiers. The home also holds ``.sqlite3`` stores (auto-integration ledger).
_STORE_SUFFIXES = (".sqlite", ".sqlite3")

#: SQLite's own sidecar files, excluded from the entry-name tier. They appear and
#: disappear as *any* process opens a transaction — including this snapshot's own
#: read — so comparing their presence would report the operator's running cockpit
#: (or the guard itself) as a test-process write. What they carry is committed
#: state, and that is what the schema / identity tiers read through a consistent
#: copy.
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

#: Sub-directory of home holding pre-write migration backups. Mirrors
#: ``core.state.state_store.BACKUPS_DIRNAME`` without importing it — this module
#: must stay usable when the state package is not importable.
_BACKUPS_DIRNAME = "backups"

#: The registry table whose *identity set* (not contents) is guarded: an
#: inserted temp workspace is exactly the #14741 producer shape.
_REGISTRY_IDENTITY = ("registry.sqlite", "workspaces", "workspace_id")


def ambient_homes(env: dict[str, str] | os._Environ | None = None) -> tuple[Path, ...]:
    """The shared homes a fenced test process must never resolve onto.

    Captured from the *real* environment, before any pin is applied: the operator
    default (``~/.mozyo_bridge``) plus whatever ``MOZYO_BRIDGE_HOME`` currently
    names. Both matter — a lane that already points the env at a shared home
    would otherwise be denied only by its default spelling.
    """
    source = os.environ if env is None else env
    homes = {Path(OPERATOR_DEFAULT_HOME).expanduser().resolve()}
    explicit = source.get("MOZYO_BRIDGE_HOME")
    if explicit:
        homes.add(Path(explicit).expanduser().resolve())
    return tuple(sorted(homes))


def effective_home(env: dict[str, str] | os._Environ | None = None) -> Path:
    """The one shared home *this* invocation would have used, unfenced.

    The guarded target. :func:`ambient_homes` is deliberately broader — it denies
    both spellings so a lane-pinned env cannot slip past the default one — but
    only one of them is the state actually at risk from this run, and that is the
    one the before/after guard snapshots.
    """
    source = os.environ if env is None else env
    return (
        Path(source.get("MOZYO_BRIDGE_HOME") or OPERATOR_DEFAULT_HOME)
        .expanduser()
        .resolve()
    )


def isolated_env(
    root: Path,
    *,
    denied_homes: tuple[Path, ...],
    base_env: dict[str, str] | None = None,
) -> tuple[IsolationLayout, dict[str, str]]:
    """Create the task-specific root and return ``(layout, child_env)``.

    ``HOME`` is inherited untouched (#14757 acceptance 1), so the fenced child
    keeps user site-packages and the operator's git identity without the
    ``PYTHONUSERBASE`` / synthetic-committer repairs a repurposed ``HOME``
    forces.
    """
    layout = IsolationLayout(root=Path(root).resolve())
    for directory in layout.directories:
        directory.mkdir(parents=True, exist_ok=True)
    pins = isolation_env(
        layout,
        denied_homes=denied_homes,
        deny_separator=HOME_FENCE_DENY_SEPARATOR,
        fence_root_key=HOME_FENCE_ROOT_ENV,
        fence_deny_key=HOME_FENCE_DENY_ENV,
    )
    env = apply_isolation(dict(os.environ if base_env is None else base_env), pins)
    return layout, env


# --------------------------------------------------------------------------- #
# Snapshot                                                                    #
# --------------------------------------------------------------------------- #

def _consistent_copy(store: Path) -> sqlite3.Connection:
    """An in-memory, point-in-time copy of ``store`` (read-only source).

    ``mode=ro`` refuses to create or write the file, and the online backup API
    runs inside a read transaction, so the copy is consistent even against a
    database another process is actively writing. Raises `sqlite3.Error` /
    `OSError` on an unreadable or corrupt store — the caller records that as
    ``unreadable`` rather than treating it as "no change".
    """
    uri = f"file:{store.as_posix()}?mode=ro"
    source = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        copy = sqlite3.connect(":memory:")
        try:
            source.backup(copy)
        except BaseException:
            # A corrupt / non-database file fails at `backup`, not at `connect`
            # (which is lazy), so the destination must be closed here or it leaks
            # for every unreadable store the guard walks past.
            copy.close()
            raise
        return copy
    finally:
        source.close()


def _schema_parts(conn: sqlite3.Connection, label: str) -> tuple[str, ...]:
    """``user_version`` + the schema-object set of one store (the #14477 tier)."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    objects = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    parts = [f"{label}\tuser_version\t{version}"]
    parts += [f"{label}\tobject\t{kind}\t{name}" for kind, name in objects]
    return tuple(parts)


def _identity_parts(conn: sqlite3.Connection, store: Path) -> tuple[str, ...]:
    """Per-table row counts, plus the registry's workspace identity set.

    Row *counts* rather than row contents: the operator's own cockpit updates
    ``last_seen`` / ``updated_at`` on existing rows continuously, and a content
    digest would flag that as a test write. An inserted row — the #14741
    producer — moves the count.
    """
    label = store.name
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    parts: list[str] = []
    for table in tables:
        # Table names come from this store's own sqlite_master, not from user
        # input, and cannot be bound as parameters in a FROM clause.
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        parts.append(f"{label}\trows\t{table}\t{count}")
    registry_store, registry_table, registry_column = _REGISTRY_IDENTITY
    if label == registry_store and registry_table in tables:
        ids = conn.execute(
            f'SELECT "{registry_column}" FROM "{registry_table}" '
            f'ORDER BY "{registry_column}"'
        ).fetchall()
        parts += [f"{label}\tid\t{value[0]}" for value in ids]
    return tuple(parts)


def _relative_names(root: Path) -> tuple[str, ...]:
    """Sorted relative paths under ``root`` (empty when absent)."""
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
        )
    )


def snapshot_home(home: Path) -> HomeSnapshot:
    """A value-free logical snapshot of ``home`` (see module docstring)."""
    home = Path(home).expanduser()
    resolved = home.resolve() if home.exists() else home
    label = str(resolved)
    if not home.is_dir():
        return HomeSnapshot(home=label, missing=True)

    entries: list[str] = []
    stores: list[Path] = []
    for child in sorted(home.iterdir(), key=lambda path: path.name):
        if child.name.endswith(_SQLITE_SIDECAR_SUFFIXES):
            continue
        entries.append(f"{'dir' if child.is_dir() else 'file'}\t{child.name}")
        if child.is_file() and child.suffix in _STORE_SUFFIXES:
            stores.append(child)

    schema_parts: list[str] = []
    identity_parts: list[str] = []
    unreadable: list[str] = []
    for store in stores:
        try:
            copy = _consistent_copy(store)
        except (sqlite3.Error, OSError) as exc:
            # Recorded, never skipped: "could not look" must not read as
            # "nothing changed", and immutable=1 is not an allowed fallback.
            unreadable.append(f"{store.name} ({type(exc).__name__})")
            continue
        try:
            schema_parts.extend(_schema_parts(copy, store.name))
            identity_parts.extend(_identity_parts(copy, store))
        except sqlite3.Error as exc:
            unreadable.append(f"{store.name} ({type(exc).__name__})")
        finally:
            copy.close()

    backups = _relative_names(home / _BACKUPS_DIRNAME)
    return HomeSnapshot(
        home=label,
        entry_count=len(entries),
        entry_digest=digest(tuple(entries)),
        schema_digest=digest(tuple(schema_parts)),
        identity_digest=digest(tuple(identity_parts)),
        backup_count=len(backups),
        backup_digest=digest(backups),
        store_count=len(stores),
        unreadable=tuple(sorted(unreadable)),
    )


# --------------------------------------------------------------------------- #
# Re-exec into a fenced child                                                 #
# --------------------------------------------------------------------------- #

# The child is launched with `python -c <bootstrap>` rather than `python -m
# mozyo_bridge` + PYTHONPATH: `sys.path` is process-local, so the parent's
# runtime reaches the child WITHOUT being inherited by anything the test bodies
# spawn. Injecting it via PYTHONPATH instead leaked `src/` into nested
# subprocesses and broke verdict parity (Redmine #13735 j#78390 F1).
_BOOTSTRAP = (
    "import sys\n"
    "sys.path.insert(0, {runtime!r})\n"
    "from mozyo_bridge.application.cli import main\n"
    "raise SystemExit(main(sys.argv[1:]))\n"
)


def runtime_root() -> str:
    """Absolute dir that makes the *parent's own* ``mozyo_bridge`` importable."""
    import mozyo_bridge  # local import: avoid a package-load cycle at import time

    return str(Path(mozyo_bridge.__file__).resolve().parent.parent)


def reexec_isolated(
    argv: list[str],
    *,
    repo_root: Path,
    env: dict[str, str],
    capture_stdout: dict | None = None,
) -> int:
    """Re-run this CLI invocation in a fenced child, returning its exit code.

    ``argv`` is the parent's own recorded argv plus the already-isolated marker,
    so the child runs the identical command with identical flags — the parent
    never has to reconstruct a namespace back into arguments.

    ``capture_stdout``, when given, receives the child's stdout under the
    ``"stdout"`` key instead of it going straight to the terminal. The caller
    needs that only to merge a structured document; stderr is always inherited so
    a failing child's diagnostics stay visible in real time either way.
    """
    command = [sys.executable, "-c", _BOOTSTRAP.format(runtime=runtime_root()), *argv]
    if capture_stdout is None:
        return subprocess.run(command, cwd=str(repo_root), env=env).returncode
    proc = subprocess.run(
        command, cwd=str(repo_root), env=env, stdout=subprocess.PIPE, text=True
    )
    capture_stdout["stdout"] = proc.stdout or ""
    return proc.returncode


__all__ = (
    "ambient_homes",
    "effective_home",
    "isolated_env",
    "reexec_isolated",
    "runtime_root",
    "snapshot_home",
)
