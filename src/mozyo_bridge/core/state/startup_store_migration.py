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
import shutil
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
    artifact_path: str = ""
    artifact_digest: str = ""
    seal_nonce_verified: bool = False
    schema_version: int = 0
    action_count: int = 0
    content_digest: str = ""


def _store_identity(conn) -> str:
    """The store's OWN identity — its ``store_meta.store_nonce``, never its path.

    A path is a name two different stores can wear in sequence; the nonce is the thing the
    fence's own seal check is about.
    """
    row = conn.execute(
        "SELECT value FROM store_meta WHERE key = ?", ("store_nonce",)
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


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


#: Names inside the artifact directory. Fixed, so a restore is a copy of two known files.
ARTIFACT_DB_NAME = "startup-transaction-fence.sqlite"
ARTIFACT_SEAL_NAME = ARTIFACT_DB_NAME + ".seal"


def stage_recovery_artifact(fence, conn, staging_dir: Path) -> tuple:
    """Build the WHOLE recovery artifact in a staging directory and prove it restores.

    Audit j#96976 C17 + C19. Two separate failures made the previous version's success
    claim unearned:

    - publishing the DB and the seal as two renames left a real window (and a real
      injected-fault outcome) where the final namespace held a DB with no seal — a
      partial artifact that looks restorable and is not;
    - the "verification" compared the copied seal against the LIVE seal and never opened
      the backup's own ``store_meta.store_nonce``. Corrupting only the backup nonce still
      returned ``seal_nonce_verified=True`` while the restored authority was unusable.

    So the pair is assembled here, in a directory nobody else can see, and then actually
    RESTORED: a fresh :class:`StartupTransactionFence` is opened on the staged DB, which
    forces the real contract — schema version, table shape, seal present, and the seal's
    nonce byte-matching the DB's own. Its rows and content are then required to equal the
    source's. Only a pair that passes all of that is published, and publication is ONE
    atomic directory rename.
    """
    from mozyo_bridge.core.state.startup_transaction_fence import StartupTransactionFence

    seal_source = Path(fence.seal_path)
    if not seal_source.is_file():
        raise OSError("the store has no readable identity seal to snapshot")

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)
    staged_db = staging_dir / ARTIFACT_DB_NAME
    staged_seal = staging_dir / ARTIFACT_SEAL_NAME

    source_facts = _store_facts(conn)
    dest = sqlite3.connect(staged_db)
    try:
        conn.backup(dest)
        dest.commit()
    finally:
        dest.close()
    staged_seal.write_bytes(seal_source.read_bytes())

    # The actual restore test. `_verify` inside this read is what proves the DB nonce and
    # the seal agree — the binding C19 says must be checked on the ARTIFACT, not on the
    # live store it was copied from.
    restored = StartupTransactionFence(staged_db)
    with restored._connection("ro") as verify_conn:
        artifact_facts = _store_facts(verify_conn)
    if artifact_facts != source_facts:
        raise OSError(
            f"the staged artifact does not read back as the source "
            f"(source={source_facts}, artifact={artifact_facts})"
        )
    artifact_digest = hashlib.sha256(
        json.dumps(
            [list(artifact_facts), staged_seal.read_bytes().decode("utf-8", "replace")],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return (source_facts, artifact_digest)


def publish_recovery_artifact(staging_dir: Path, artifact_dir: Path) -> None:
    """Publish the verified artifact in ONE atomic rename (audit j#96976 C17).

    Refuses an existing destination rather than merging into it: a rename over a populated
    directory is neither atomic nor obviously correct, and silently reusing someone else's
    artifact path is exactly the ambiguity this whole primitive is trying to remove.
    """
    if artifact_dir.exists():
        raise OSError(
            f"the recovery artifact path {artifact_dir.name!r} already exists; refusing to "
            "publish over it"
        )
    artifact_dir.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging_dir, artifact_dir)


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


#: The receipt protocol this build accepts. An unknown one is refused, never coerced.
COMPLETION_RECEIPT_PROTOCOL = "mzb-startup-migration-1"
#: The only phase that means "this rollout finished".
COMPLETION_PHASE_TERMINAL = "completed"


@dataclass(frozen=True)
class MigrationCompletionReceipt:
    """#14838's durable record that THIS action completed THIS migration.

    A typed value, not a mapping (audit j#96966 C18). The previous check accepted any
    ``Mapping`` carrying three coercively-``str()``-compared keys, so a three-key dict
    literal proved completion — and my own regression pinned exactly that as success, which
    made the test part of the defect. A receipt has to identify the action, the plan, the
    store, and the artifact, or it identifies nothing.

    Every field is compared for exact string equality. Nothing here is normalised: a padded
    or wrongly-typed field is a different receipt, not a forgiving one.
    """

    action_id: str
    plan_digest: str
    #: Where the completed rollout published its recovery artifact. Re-verified on replay:
    #: the digest below is checked against THIS directory, not against itself.
    artifact_path: str
    #: The target store's own identity — its ``store_meta.store_nonce``, not its path. A
    #: path is a name; two different stores can wear it in sequence.
    store_identity: str
    artifact_digest: str
    protocol: str = COMPLETION_RECEIPT_PROTOCOL
    phase: str = COMPLETION_PHASE_TERMINAL
    revision: int = 1


def artifact_digest_of(artifact_dir) -> str:
    """Recompute an existing artifact's digest by actually opening it (never raises=False).

    Independent verification, which is the whole point: the digest is derived from the
    artifact on disk — through a fresh fence, so the seal/nonce binding is proved again —
    rather than taken from the receipt that claims it. Comparing a receipt field against
    itself proves nothing, which is what the first cut of the replay check did.
    """
    from mozyo_bridge.core.state.startup_transaction_fence import StartupTransactionFence

    directory = Path(artifact_dir)
    db = directory / ARTIFACT_DB_NAME
    seal = directory / ARTIFACT_SEAL_NAME
    if not db.is_file() or not seal.is_file():
        return ""
    try:
        with StartupTransactionFence(db)._connection("ro") as conn:
            facts = _store_facts(conn)
    except Exception:  # noqa: BLE001 - an unopenable artifact simply does not verify
        return ""
    return hashlib.sha256(
        json.dumps(
            [list(facts), seal.read_bytes().decode("utf-8", "replace")],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _receipt_proves_completion(
    receipt, *, plan_digest: str, store_identity: str
) -> bool:
    """True iff ``receipt`` is the typed completion record for THIS plan, store and artifact.

    Strict by construction: the caller must hand over a
    :class:`MigrationCompletionReceipt` read back from #14838's private action store. Any
    other object — including a mapping that happens to have the right keys — proves nothing.
    """
    if not isinstance(receipt, MigrationCompletionReceipt):
        return False
    if receipt.protocol != COMPLETION_RECEIPT_PROTOCOL:
        return False
    if receipt.phase != COMPLETION_PHASE_TERMINAL or not isinstance(receipt.revision, int):
        return False
    if not isinstance(receipt.action_id, str) or not receipt.action_id.strip():
        return False
    if receipt.plan_digest != plan_digest or receipt.store_identity != store_identity:
        return False
    # The artifact the receipt names must still verify, and its recomputed digest must be
    # the one the receipt recorded.
    return bool(receipt.artifact_digest) and artifact_digest_of(
        receipt.artifact_path
    ) == receipt.artifact_digest


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
                identity = _store_identity(conn)
                if not _receipt_proves_completion(
                    completion_receipt,
                    plan_digest=digest_token,
                    store_identity=identity,
                ):
                    raise StartupStoreMigrationRefused(
                        MIGRATION_ALREADY_V2_UNVERIFIED,
                        "the store is already v2, but no typed action completion receipt "
                        "from the rollout's own action store proves this run is that same "
                        "completed rollout; refusing to report an unverified success",
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
            artifact_dir = Path(backup_path)
            staging_dir = artifact_dir.with_name(
                artifact_dir.name + _BACKUP_STAGING_SUFFIX
            )
            try:
                source_facts, artifact_digest = stage_recovery_artifact(
                    fence, conn, staging_dir
                )
                publish_recovery_artifact(staging_dir, artifact_dir)
            except (OSError, sqlite3.DatabaseError, ValueError, StartupTransactionError) as exc:
                # Zero in the final namespace: the staging directory is removed whole, and
                # nothing was ever renamed into place.
                try:
                    if staging_dir.exists():
                        shutil.rmtree(staging_dir)
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
        backup_path=str(artifact_dir / ARTIFACT_DB_NAME),
        backup_seal_path=str(artifact_dir / ARTIFACT_SEAL_NAME),
        artifact_path=str(artifact_dir),
        artifact_digest=artifact_digest,
        seal_nonce_verified=True,
        schema_version=post[0],
        action_count=post[1],
        content_digest=post[2],
    )
