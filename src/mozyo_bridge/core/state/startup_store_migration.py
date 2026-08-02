"""Offline v1 -> v2 migration primitive for the startup transaction store (#14741).

Design Answer j#96936 items 3, 5 and 7. #14741 owns this PRIMITIVE and its regressions;
#14838 owns the orchestration around it — stop every consumer, back up, migrate the sibling
stores too, restart on an attested new binary, verify health, roll back. The real migration
of a shared home runs only under that rail with exact owner approval.

Nothing here is reachable from a normal startup. That is the whole point: a runtime that
quietly bumped a store version would be exactly the implicit migration the offline rail
exists to prevent, and it would strand every older peer that was still reading the home.

Carved out of :mod:`.startup_action_capability` so both stay under the module-health
ceiling, and because the migration is a separate concern with a separate owner.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from mozyo_bridge.core.state.startup_action_capability import (
    CAPABILITY_LEGACY,
    StartupTransactionError,
    _IDENTITY_MANIFEST_SQL,
    _manifest_table_state,
    action_capability,
)


# --- Offline v1 -> v2 migration primitive (Design Answer j#96936 items 3, 5, 7) ----------
#
# #14741 owns this PRIMITIVE and its regressions; #14838 owns the orchestration around it
# (stop every consumer, back up, migrate the sibling stores too, restart on an attested new
# binary, verify health, roll back). The real migration of a shared home runs only under
# that rail with exact owner approval — nothing here is invoked by a normal startup.

MIGRATION_OK = "migrated"
#: Already v2 and exactly the shape v2 requires. Idempotent replay of a completed rollout.
MIGRATION_ALREADY_V2 = "already_v2"
#: A consumer still holds the store. Migrating under a live peer is the one thing an
#: offline rollout must never do, so contention is a refusal and never a wait.
MIGRATION_LIVE_CONSUMER = "live_consumer"
#: The store already contains capability-tagged actions while claiming v1. Nothing this
#: build wrote could be in that state, so the store's history is not what it claims.
MIGRATION_TAGGED_ROWS_PRESENT = "tagged_rows_present"
#: The named sibling table exists in a shape this build did not create.
MIGRATION_FOREIGN_SIBLING = "foreign_sibling_schema"
#: The backup could not be produced. No backup, no migration.
MIGRATION_BACKUP_FAILED = "backup_failed"
#: The caller's migration plan is not the plan this store presents.
MIGRATION_PLAN_DRIFT = "plan_drift"


class StartupStoreMigrationRefused(StartupTransactionError):
    """An offline v1->v2 migration was refused. Carries a fixed reason; zero mutation."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def startup_store_migration_plan_digest(conn) -> str:
    """A digest of what the migration is ABOUT to act on, for the caller to pre-approve.

    Covers the facts a rollout plan is written against: the schema version, the action ids
    present, and whether the sibling table already exists. If any of them changed between
    the plan being approved and the migration running, the digest differs and the migration
    refuses — which is what "plan drift" means operationally.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    actions = [
        str(row[0])
        for row in conn.execute(
            "SELECT action_id FROM startup_actions ORDER BY action_id"
        ).fetchall()
    ]
    sibling = _manifest_table_state(conn)
    payload = json.dumps([version, actions, sibling], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _refuse_if_tagged_rows(conn) -> None:
    for row in conn.execute("SELECT action_id FROM startup_actions").fetchall():
        try:
            capability = action_capability(row[0])
        except StartupTransactionError:
            raise StartupStoreMigrationRefused(
                MIGRATION_TAGGED_ROWS_PRESENT,
                "the store holds an action id this build cannot classify",
            )
        if capability != CAPABILITY_LEGACY:
            raise StartupStoreMigrationRefused(
                MIGRATION_TAGGED_ROWS_PRESENT,
                "the store already holds capability-tagged actions while declaring v1",
            )


def migrate_startup_store_v1_to_v2(fence, *, backup_path, expected_plan_digest: str = "") -> str:
    """Take a v1 startup store to v2, offline and fail-closed. Returns a fixed token.

    Every refusal happens BEFORE any mutation, and the migration itself is one transaction:
    create the sibling table if absent, then set ``user_version = 2``. A store left
    half-migrated would be the worst outcome available here — an old runtime would still
    accept it while a new one thinks the capability contract holds — so there is no
    intermediate state to be interrupted in.
    """
    import shutil

    backup = Path(backup_path)
    try:
        holder = fence._hold()
    except Exception as exc:  # noqa: BLE001 - contention is a refusal, never a wait
        raise StartupStoreMigrationRefused(MIGRATION_LIVE_CONSUMER, str(exc)) from exc
    with holder:
        with fence._connection("rw") as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version == 2:
                # Idempotent replay of a completed rollout; the connection's own `_verify`
                # already proved the v2 shape, so there is nothing left to do.
                return MIGRATION_ALREADY_V2
            if version != 1:
                raise StartupStoreMigrationRefused(
                    MIGRATION_PLAN_DRIFT, f"store is v{version}, not v1"
                )
            # `_manifest_table_state` raises on a foreign/partial shape — surface it as the
            # migration's own typed refusal rather than a generic authority error.
            try:
                sibling = _manifest_table_state(conn)
            except StartupTransactionError as exc:
                raise StartupStoreMigrationRefused(
                    MIGRATION_FOREIGN_SIBLING, str(exc)
                ) from exc
            _refuse_if_tagged_rows(conn)
            actual_plan = startup_store_migration_plan_digest(conn)
            if expected_plan_digest and actual_plan != expected_plan_digest:
                raise StartupStoreMigrationRefused(
                    MIGRATION_PLAN_DRIFT,
                    "the store is not in the state the approved migration plan described",
                )
            try:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fence.path, backup)
                if not backup.exists() or backup.stat().st_size <= 0:
                    raise OSError("backup is absent or empty")
            except OSError as exc:
                raise StartupStoreMigrationRefused(
                    MIGRATION_BACKUP_FAILED, str(exc)
                ) from exc
            try:
                conn.execute("BEGIN IMMEDIATE")
                if sibling == "absent":
                    conn.execute(_IDENTITY_MANIFEST_SQL)
                conn.execute("PRAGMA user_version = 2")
                conn.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise StartupStoreMigrationRefused(
                    MIGRATION_PLAN_DRIFT, f"the migration write failed ({exc})"
                ) from exc
    return MIGRATION_OK


