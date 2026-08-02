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
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
#: The approved plan digest was absent or not a canonical digest. Required, never defaulted.
MIGRATION_PLAN_DIGEST_REQUIRED = "plan_digest_required"
#: The store is already v2, but nothing proves this build's run is the SAME completed
#: rollout. "It is already done" is not the same statement as "I already did it".
MIGRATION_ALREADY_V2_UNVERIFIED = "already_v2_unverified"
#: The seal could not be snapshotted or read back with the DB, so the artifact could not
#: restore the authority it claims to back up.
MIGRATION_SEAL_UNAVAILABLE = "seal_unavailable"
#: The caller's migration plan is not the plan this store presents.
MIGRATION_PLAN_DRIFT = "plan_drift"


_PLAN_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
#: Suffix for the staged snapshot. A backup is published by rename, so a reader never sees
#: a half-written one.
_BACKUP_STAGING_SUFFIX = ".staging"


@dataclass(frozen=True)
class StartupStoreMigrationResult:
    """What a migration did, and the evidence it verified before doing it.

    ``backup_path`` alone is NOT a recovery point (audit j#96966 C11): the fence's
    ``store_shape`` requires an external ``.seal`` whose nonce byte-matches the DB's, so
    restoring the database on its own yields a *damaged* authority that refuses every read.
    ``backup_seal_path`` is the seal captured with it, and ``seal_nonce_verified`` records
    that the published pair was read back and agreed. A caller must restore BOTH.
    """

    outcome: str
    backup_path: str = ""
    backup_seal_path: str = ""
    seal_nonce_verified: bool = False
    schema_version: int = 0
    action_count: int = 0
    content_digest: str = ""


def _store_facts(conn) -> tuple:
    """``(user_version, action count, content digest)`` — the readback identity."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    rows = conn.execute(
        "SELECT action_id, workspace_id, lane_id, providers, phase, revision,"
        " participants, reserved_at, updated_at FROM startup_actions ORDER BY action_id"
    ).fetchall()
    payload = json.dumps(
        [[str(cell) for cell in row] for row in rows],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return (version, len(rows), hashlib.sha256(payload.encode("utf-8")).hexdigest())


def staged_seal_backup(fence, backup_path: Path) -> tuple:
    """Stage the fence's identity seal beside the DB snapshot (audit j#96966 C11).

    The seal is a separate file the fence requires and byte-matches against the DB's own
    stored nonce. A snapshot without it restores to a store that fails `store_shape` — so
    the "backup" would be a file that cannot be used as one. Captured and published under
    the same staging/rename discipline as the DB.
    """
    seal_source = Path(fence.seal_path)
    seal_target = backup_path.with_name(backup_path.name + ".seal")
    staging = seal_target.with_name(seal_target.name + _BACKUP_STAGING_SUFFIX)
    if not seal_source.is_file():
        raise OSError("the store has no readable identity seal to snapshot")
    if staging.exists():
        staging.unlink()
    staging.write_bytes(seal_source.read_bytes())
    if staging.read_bytes() != seal_source.read_bytes():
        raise OSError("the seal snapshot does not read back as the source seal")
    return (staging, seal_target)


def staged_backup(conn, backup_path: Path) -> tuple:
    """Snapshot the LIVE store with SQLite's backup API, then verify it by readback.

    Audit j#96959 C8. ``shutil.copy2`` copies the main database file and nothing else, so a
    store in WAL mode with ``wal_autocheckpoint=0`` loses every committed row still sitting
    in the write-ahead log — measured: 1 of 2 actions survived a raw copy while the backup
    API preserved both. A recovery point that silently drops committed actions is worse than
    no backup, because the rollback would look like it worked.

    The snapshot is written to a STAGING path and read back as a fresh connection: version,
    action count, and a content digest must match the source. Only then is it published by
    rename, so a reader never sees a partial backup and a failed backup publishes nothing.
    """
    staging = backup_path.with_name(backup_path.name + _BACKUP_STAGING_SUFFIX)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if staging.exists():
        staging.unlink()
    source_facts = _store_facts(conn)
    dest = sqlite3.connect(staging)
    try:
        conn.backup(dest)
        dest.commit()
    finally:
        dest.close()
    verify = sqlite3.connect(f"file:{staging}?mode=ro", uri=True)
    try:
        snapshot_facts = _store_facts(verify)
    finally:
        verify.close()
    if snapshot_facts != source_facts:
        raise OSError(
            f"the snapshot does not read back as the source "
            f"(source={source_facts}, snapshot={snapshot_facts})"
        )
    return (staging, source_facts)


def publish_backup(staging: Path, backup_path: Path) -> None:
    """Atomically publish a verified staging snapshot. Rename, never copy."""
    os.replace(staging, backup_path)


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


def _receipt_proves_completion(receipt, plan_digest: str, target_path) -> bool:
    """True iff ``receipt`` is #14838's completion record for THIS plan and THIS store.

    Deliberately strict and deliberately dumb: exact plan digest, exact target path. It is
    not this primitive's job to interpret the rollout's bookkeeping — only to refuse to
    call an unproven state a success.
    """
    if not isinstance(receipt, Mapping):
        return False
    return (
        str(receipt.get("plan_digest") or "") == plan_digest
        and str(receipt.get("target_path") or "") == str(target_path)
        and str(receipt.get("outcome") or "") == MIGRATION_OK
    )


def migrate_startup_store_v1_to_v2(
    fence, *, backup_path, expected_plan_digest: str, completion_receipt=None
):
    """Take a v1 startup store to v2, offline and fail-closed. Returns a fixed token.

    Every refusal happens BEFORE any mutation, and the migration itself is one transaction:
    create the sibling table if absent, then set ``user_version = 2``. A store left
    half-migrated would be the worst outcome available here — an old runtime would still
    accept it while a new one thinks the capability contract holds — so there is no
    intermediate state to be interrupted in.
    """
    backup = Path(backup_path)
    # Audit j#96959 C9: the approved plan digest is REQUIRED. Defaulting it to "" and
    # guarding with a truthy test meant a caller that simply omitted it silently disabled
    # drift detection — the one check that ties this run to the plan an operator approved.
    digest_token = expected_plan_digest if isinstance(expected_plan_digest, str) else ""
    if not _PLAN_DIGEST_RE.fullmatch(digest_token):
        raise StartupStoreMigrationRefused(
            MIGRATION_PLAN_DIGEST_REQUIRED,
            "an offline migration requires the exact canonical digest of the approved "
            "plan; missing, padded or malformed is refused before any mutation",
        )
    try:
        holder = fence._hold()
    except Exception as exc:  # noqa: BLE001 - contention is a refusal, never a wait
        raise StartupStoreMigrationRefused(MIGRATION_LIVE_CONSUMER, str(exc)) from exc
    with holder:
        with fence._connection("rw") as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version == 2:
                # Audit j#96966 C10. "The store is already v2" is NOT "I already did this
                # migration". The previous cut returned success here before even looking at
                # the approved plan, so any well-formed digest — including a store migrated
                # by some other run, against some other plan — read as an idempotent
                # completion. Replay succeeds only when the caller presents the completion
                # receipt for THIS action from the external action store (#14838) and it
                # names this plan and this target.
                facts = _store_facts(conn)
                if not _receipt_proves_completion(
                    completion_receipt, digest_token, fence.path
                ):
                    raise StartupStoreMigrationRefused(
                        MIGRATION_ALREADY_V2_UNVERIFIED,
                        "the store is already v2, but no external action completion "
                        "receipt proves this run is that same completed rollout; refusing "
                        "to report an unverified success",
                    )
                return StartupStoreMigrationResult(
                    MIGRATION_ALREADY_V2,
                    schema_version=facts[0],
                    action_count=facts[1],
                    content_digest=facts[2],
                )
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
            if actual_plan != digest_token:
                raise StartupStoreMigrationRefused(
                    MIGRATION_PLAN_DRIFT,
                    "the store is not in the state the approved migration plan described",
                )
            staging = seal_staging = None
            seal_target = None
            try:
                staging, source_facts = staged_backup(conn, backup)
                seal_staging, seal_target = staged_seal_backup(fence, backup)
                # Publish the PAIR, DB first: a reader that finds the DB must find the seal
                # that goes with it, and a crash between the two leaves a DB whose missing
                # seal makes it obviously unusable rather than silently wrong.
                publish_backup(staging, backup)
                staging = None
                publish_backup(seal_staging, seal_target)
                seal_staging = None
                if backup.read_bytes()[:16] and Path(seal_target).read_bytes() != Path(
                    fence.seal_path
                ).read_bytes():
                    raise OSError("the published seal does not match the live store's seal")
            except (OSError, sqlite3.DatabaseError, ValueError) as exc:
                for leftover in (staging, seal_staging):
                    if leftover is not None:
                        try:
                            Path(leftover).unlink(missing_ok=True)
                        except OSError:
                            pass
                reason = (
                    MIGRATION_SEAL_UNAVAILABLE
                    if "seal" in str(exc)
                    else MIGRATION_BACKUP_FAILED
                )
                raise StartupStoreMigrationRefused(reason, str(exc)) from exc
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
            post = _store_facts(conn)
    return StartupStoreMigrationResult(
        MIGRATION_OK,
        backup_path=str(backup),
        backup_seal_path=str(seal_target),
        seal_nonce_verified=True,
        schema_version=post[0],
        action_count=post[1],
        content_digest=post[2],
    )


