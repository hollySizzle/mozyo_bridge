"""Public attestation-store maintenance use case (Redmine #13882 acceptance 3).

The #13882 write policy deliberately leaves a v1 shared home **at v1** so older installed
launchers keep working (``herdr_identity_attestation_schema``). That makes forward
migration an explicit operator act rather than a launch side effect — and this module is
that act's only public rail. It exists because the alternative the incident actually left
operators with was hand-editing ``herdr-identity-attestation.sqlite`` with raw SQLite,
which the issue rules out as a non-goal.

Three intents, each a read-only plan by default:

- ``status`` — what shape the selected store is, and what that admits;
- ``migrate`` — additive v1 -> v2, backup-first, idempotent;
- ``rebuild`` — rotate an unmigratable (corrupt / foreign / newer) store aside into
  ``backups/`` and start a fresh one. Legitimate *only* because this projection is a
  ``rebuildable_cache``: the next launch's self-attestation re-derives it, and until then
  every read degrades to fail-closed rather than to a false attestation. Rebuild is not
  offered as a shortcut around migrate — migrate preserves the rows, rebuild discards
  them, so the command refuses to rebuild a store that migrate could handle.

**Active-consumer safety.** Both mutating intents refuse while a **proven consumer of this
store** is live — a managed agent that is live AND carries a record here (see
:func:`_live_consumer_names` for why a stored row is the only available evidence, herdr
having no surface that reveals a live process's injected home). Changing the shape under
them would change what a concurrent older-runtime reader sees of their records; rebuilding
would additionally orphan the attestations of processes still running, turning verified
slots unverifiable. The scope is cross-workspace, never repo-scoped, because the store is
shared. An **unreadable** inventory is never folded into "no consumers" (the #13682 R1-F1
/ #13754 anti-pattern): it refuses just as hard.

Actuation boundary: this module never closes, sends to, or launches a process. It copies
a file and runs additive DDL, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    herdr_identity_attestation_path,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    MIGRATION_APPLIED,
    RECOGNIZED_SCHEMA_VERSIONS,
    STORE_ABSENT,
    STORE_RECOGNIZED,
    AttestationMigrationOutcome,
    HerdrIdentityAttestationSchemaError,
    probe_store_schema,
    quarantine_attestation_store_artifacts,
    remove_attestation_store_artifacts,
)
from mozyo_bridge.core.state.state_store import StateStoreError

# --- Outcome vocabulary (fail-closed; only *_OK / *_PLANNED admit an action). ---------
#: The read-only report intent always succeeds if the store could be probed.
STATUS_REPORTED = "status_reported"
#: A dry-run plan: the action is admissible and would run under ``--write``.
PLANNED = "planned"
#: The mutation ran.
APPLIED = "applied"
#: Verified idempotent no-op — the store is already at the target shape.
ALREADY_CURRENT = "already_current"
#: Refused: managed agents are live in this home.
BLOCKED_CONSUMERS_LIVE = "blocked_consumers_live"
#: Refused: liveness could not be measured (an unreadable inventory is not an empty one).
BLOCKED_INVENTORY_UNREADABLE = "blocked_inventory_unreadable"
#: Refused: agents are live but the store's rows cannot be enumerated, so it cannot be
#: proven none of them consume it (review j#80000 finding 2).
BLOCKED_CONSUMERS_UNMEASURABLE = "blocked_consumers_unmeasurable"
#: Refused: the store's shape is not one this intent can act on.
BLOCKED_STORE_UNSUPPORTED = "blocked_store_unsupported"
#: Refused: rebuild was asked for a store that ``migrate`` can handle without data loss.
BLOCKED_MIGRATE_INSTEAD = "blocked_migrate_instead"
#: Refused: the mutation itself failed (the store is left untouched / backed up).
BLOCKED_FAILED = "blocked_failed"

_OK_STATES = frozenset({STATUS_REPORTED, PLANNED, APPLIED, ALREADY_CURRENT})


@dataclass(frozen=True)
class AttestationStoreMaintenanceResult:
    """The auditable result of one maintenance intent (structured, not only stderr)."""

    intent: str
    state: str
    store_version: Optional[int] = None
    store_state: str = ""
    target_version: int = HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
    detail: str = ""
    backup_dir: Optional[Path] = None
    live_consumers: tuple = ()
    executed: bool = False
    notes: Sequence[str] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.state in _OK_STATES

    def as_payload(self) -> dict:
        return {
            "intent": self.intent,
            "state": self.state,
            "ok": self.ok,
            "executed": self.executed,
            "store_version": self.store_version,
            "store_state": self.store_state,
            "target_version": self.target_version,
            "detail": self.detail,
            # Operator-facing evidence. A pasteable durable record redacts absolute paths
            # (Redmine #12098 / #13368); that is the caller's boundary, not this payload's.
            "backup_dir": str(self.backup_dir) if self.backup_dir else None,
            "live_consumers": list(self.live_consumers),
            "notes": list(self.notes),
        }


#: Consumer measurement outcomes. Tri-state on purpose: "proven none" and "cannot tell"
#: are different facts, and collapsing them is how a destructive path fails open.
_CONSUMERS_NONE = "no_consumers"
_CONSUMERS_PRESENT = "consumers"
_CONSUMERS_UNMEASURABLE = "unmeasurable"


def _measure_consumers(view, home: Path) -> tuple:
    """Measure this store's live consumers -> ``(state, names)``. Never guesses.

    Scoped by evidence. herdr exposes no surface returning a launched process's
    environment, so which home a live agent was launched against is unobservable from
    outside (the constraint the whole self-attestation design exists to work around). A
    **stored row** is the only proof tying a live agent to *this* store, so a consumer is
    an agent that is both live AND carries a record here — cross-workspace, never
    repo-scoped, since the store is shared.

    A live agent with no row here is correctly excluded, and that is not a fail-open:
    attestation is a **one-shot write at boot** (``perform_self_attestation`` is the sole
    production writer and ``exec``s immediately after), so a live agent has already
    completed its only write. Either it uses a different home, or it already failed to
    attest — in both cases this store's shape cannot degrade it further.

    The precedence below is what closes review j#80000 finding 2. An **empty fleet** is
    proof of no consumers whatever the store's state — nothing can be consuming a store
    when nothing is running — so it is checked *before* the store is read. Only when
    agents ARE live does the store's readability matter: if its rows cannot be enumerated,
    the intersection is unknown, and the honest answer is :data:`_CONSUMERS_UNMEASURABLE`,
    never "none". Previously this folded to an empty set and admitted a destructive
    ``rebuild`` against an unreadable store while agents were live — fail-open on the one
    path whose entire target set is unreadable stores.
    """
    if not view.backend_selected:
        # No herdr backend: no managed consumers of this store by construction.
        return _CONSUMERS_NONE, ()
    if not view.ok:
        return _CONSUMERS_UNMEASURABLE, ()
    live = {agent.name for agent in view.managed_agents}
    if not live:
        return _CONSUMERS_NONE, ()
    attested = HerdrIdentityAttestationStore(home=home).assigned_names()
    if attested is None:
        return _CONSUMERS_UNMEASURABLE, tuple(sorted(live))
    matched = tuple(sorted(live & attested))
    return (_CONSUMERS_PRESENT, matched) if matched else (_CONSUMERS_NONE, ())


def _consumer_gate(view, intent: str, home: Path) -> Optional[AttestationStoreMaintenanceResult]:
    """Refuse a mutation while consumers are live / unmeasurable, else ``None``."""
    state, names = _measure_consumers(view, home)
    if state == _CONSUMERS_NONE:
        return None
    if state == _CONSUMERS_PRESENT:
        return AttestationStoreMaintenanceResult(
            intent=intent,
            state=BLOCKED_CONSUMERS_LIVE,
            detail=(
                f"{len(names)} live managed agent(s) hold a startup self-attestation in "
                f"this store ({', '.join(names)}); changing its shape would change what a "
                f"concurrent older-runtime reader sees of their records. Retire / close "
                f"them first, then re-run"
            ),
            live_consumers=names,
        )
    if not view.ok:
        return AttestationStoreMaintenanceResult(
            intent=intent,
            state=BLOCKED_INVENTORY_UNREADABLE,
            detail=(
                f"the live herdr inventory is unreadable ({view.reason}: {view.detail}); "
                f"liveness cannot be measured, and an unreadable inventory is not an "
                f"empty one. Refusing to touch the store"
            ),
        )
    return AttestationStoreMaintenanceResult(
        intent=intent,
        state=BLOCKED_CONSUMERS_UNMEASURABLE,
        detail=(
            f"{len(names)} managed agent(s) are live but this store's rows cannot be "
            f"enumerated (it is unreadable / unsupported), so it cannot be proven that "
            f"none of them attested into it ({', '.join(names)}). An unreadable store is "
            f"not an empty one. Retire / close the live agent(s) first — with an empty "
            f"fleet nothing can be consuming this store, and the operation is provably safe"
        ),
        live_consumers=names,
    )


def run_attestation_store_status(*, home: Path) -> AttestationStoreMaintenanceResult:
    """Read-only report of the selected store's shape (creates nothing)."""
    store = probe_store_schema(herdr_identity_attestation_path(home))
    notes: list[str] = []
    if store.state == STORE_ABSENT:
        notes.append(
            f"no store yet; the first self-attestation creates it at "
            f"v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION}"
        )
    elif store.state == STORE_RECOGNIZED:
        if store.version == HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION:
            notes.append("current shape; normal and replacement launches are admitted")
        else:
            notes.append(
                f"v{store.version} is read-compatible: reads project up to "
                f"v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION} and normal launches write "
                f"it v{store.version}-shaped, so older installed launchers keep working"
            )
            notes.append(
                "replacement launches are REFUSED at this shape (no "
                "`replacement_action_id` column); run `migrate --write` to admit them"
            )
    elif store.upgrade_required:
        notes.append("the store is newer than this runtime; use a newer runtime")
    else:
        notes.append(
            "the store's recorded version and on-disk shape disagree (partial / corrupt "
            "/ foreign); restore a backup, or `rebuild --write` to rotate it aside"
        )
    return AttestationStoreMaintenanceResult(
        intent="status",
        state=STATUS_REPORTED,
        store_version=store.version,
        store_state=store.state,
        detail=f"attestation store is {store.state}",
        notes=tuple(notes),
    )


def run_attestation_store_migrate(
    *, home: Path, view, write: bool = False
) -> AttestationStoreMaintenanceResult:
    """Additive v1 -> v2 migration: backup-first, idempotent, consumer-gated."""
    path = herdr_identity_attestation_path(home)
    store = probe_store_schema(path)
    if store.state not in (STORE_ABSENT, STORE_RECOGNIZED):
        return AttestationStoreMaintenanceResult(
            intent="migrate",
            state=BLOCKED_STORE_UNSUPPORTED,
            store_version=store.version,
            store_state=store.state,
            detail=(
                "the store is newer than this runtime; use a newer runtime"
                if store.upgrade_required
                else "the store's recorded version and on-disk shape disagree (partial / "
                "corrupt / foreign); migration would have to guess a shape. Restore a "
                "backup, or `rebuild --write` to rotate it aside"
            ),
        )
    # The live-zero read runs BEFORE the idempotent already-current success, so a replay
    # can never report success while consumers are live (Redmine #13841 review j#79150
    # finding 2).
    blocked = _consumer_gate(view, "migrate", home)
    if blocked is not None:
        return AttestationStoreMaintenanceResult(
            intent=blocked.intent,
            state=blocked.state,
            store_version=store.version,
            store_state=store.state,
            detail=blocked.detail,
            live_consumers=blocked.live_consumers,
        )
    if store.state == STORE_ABSENT:
        return AttestationStoreMaintenanceResult(
            intent="migrate",
            state=ALREADY_CURRENT,
            store_version=None,
            store_state=store.state,
            detail=(
                f"no store exists yet; nothing to migrate (the first self-attestation "
                f"creates it at v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION}). Creating "
                f"one here would fabricate an empty projection no launch asked for"
            ),
        )
    if store.version == HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION:
        return AttestationStoreMaintenanceResult(
            intent="migrate",
            state=ALREADY_CURRENT,
            store_version=store.version,
            store_state=store.state,
            detail=f"already at v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION}; no DDL ran",
        )
    if not write:
        return AttestationStoreMaintenanceResult(
            intent="migrate",
            state=PLANNED,
            store_version=store.version,
            store_state=store.state,
            detail=(
                f"would migrate v{store.version} -> "
                f"v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION} (additive, backup-first). "
                f"Re-run with --write to perform it"
            ),
            notes=(
                "after migrating, launchers that write only v"
                f"{store.version} can no longer attest into this home — they will be "
                "refused visibly at the managed-launch preflight, not silently dropped",
            ),
        )
    try:
        outcome: AttestationMigrationOutcome = _migrate(path)
    except (HerdrIdentityAttestationSchemaError, StateStoreError) as exc:
        return AttestationStoreMaintenanceResult(
            intent="migrate",
            state=BLOCKED_FAILED,
            store_version=store.version,
            store_state=store.state,
            detail=str(exc),
        )
    return AttestationStoreMaintenanceResult(
        intent="migrate",
        state=APPLIED if outcome.outcome == MIGRATION_APPLIED else ALREADY_CURRENT,
        store_version=outcome.from_version,
        store_state=store.state,
        detail=(
            f"migrated v{outcome.from_version} -> v{outcome.to_version} "
            f"(additive, backup-first)"
        ),
        backup_dir=outcome.backup_dir,
        executed=True,
    )


def _migrate(path: Path) -> AttestationMigrationOutcome:
    """Seam for the schema-module migration (kept injectable for tests)."""
    from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
        migrate_attestation_store,
    )

    return migrate_attestation_store(path)


def run_attestation_store_rebuild(
    *, home: Path, view, write: bool = False
) -> AttestationStoreMaintenanceResult:
    """Rotate an unmigratable store aside into ``backups/`` and start fresh.

    Only for a store ``migrate`` cannot handle. A recognized older store is refused here
    (:data:`BLOCKED_MIGRATE_INSTEAD`) because rebuild **discards** its rows while migrate
    preserves them — offering rebuild as the easier path would quietly destroy real
    attestations.
    """
    path = herdr_identity_attestation_path(home)
    store = probe_store_schema(path)
    if store.state == STORE_ABSENT:
        return AttestationStoreMaintenanceResult(
            intent="rebuild",
            state=ALREADY_CURRENT,
            store_version=None,
            store_state=store.state,
            detail="no store exists; nothing to rebuild",
        )
    if store.state == STORE_RECOGNIZED:
        return AttestationStoreMaintenanceResult(
            intent="rebuild",
            state=BLOCKED_MIGRATE_INSTEAD,
            store_version=store.version,
            store_state=store.state,
            detail=(
                f"the store is a recognized v{store.version} and is not corrupt; rebuild "
                f"would discard its rows. Use `migrate --write` (additive, preserves "
                f"them), or restore a backup if you intend to lose them"
            ),
        )
    if store.upgrade_required:
        return AttestationStoreMaintenanceResult(
            intent="rebuild",
            state=BLOCKED_STORE_UNSUPPORTED,
            store_version=store.version,
            store_state=store.state,
            detail=(
                "the store is newer than this runtime understands. Rebuilding it would "
                "destroy a newer runtime's authority; use a newer runtime instead"
            ),
        )
    blocked = _consumer_gate(view, "rebuild", home)
    if blocked is not None:
        return AttestationStoreMaintenanceResult(
            intent=blocked.intent,
            state=blocked.state,
            store_version=store.version,
            store_state=store.state,
            detail=blocked.detail,
            live_consumers=blocked.live_consumers,
        )
    if not write:
        return AttestationStoreMaintenanceResult(
            intent="rebuild",
            state=PLANNED,
            store_version=store.version,
            store_state=store.state,
            detail=(
                f"would rotate the unreadable / unsupported store into backups/ and start "
                f"a fresh v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION} store. Re-run with "
                f"--write to perform it"
            ),
            notes=(
                "rebuilt attestations are re-derived by each slot's next launch; until "
                "then every read degrades to fail-closed (adopt refuses, doctor "
                "non-green), never to a false attestation",
            ),
        )
    try:
        # Raw quarantine, not a logical snapshot (review j#80029 R2-F1): the store is
        # already proven unreadable above, so there is nothing to snapshot logically and
        # its bytes ARE the evidence. Whole artifact set — a stranded `-wal` would both
        # lose forensic evidence and let a later open resurrect a partial store.
        backup_dir = quarantine_attestation_store_artifacts(path)
        remove_attestation_store_artifacts(path)
    except (StateStoreError, OSError) as exc:
        return AttestationStoreMaintenanceResult(
            intent="rebuild",
            state=BLOCKED_FAILED,
            store_version=store.version,
            store_state=store.state,
            detail=f"rebuild aborted: {exc} (the store is left untouched)",
        )
    return AttestationStoreMaintenanceResult(
        intent="rebuild",
        state=APPLIED,
        store_version=store.version,
        store_state=store.state,
        detail=(
            f"rotated the unsupported store into backups/ and removed it; the next "
            f"self-attestation creates a fresh v{HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION} "
            f"store"
        ),
        backup_dir=backup_dir,
        executed=True,
    )


def format_maintenance_text(result: AttestationStoreMaintenanceResult) -> str:
    """Human-readable rendering (the JSON payload is the machine surface)."""
    lines = [
        f"herdr attestation-store {result.intent}: {result.state}",
        f"  store: {result.store_state}"
        + (f" (v{result.store_version})" if result.store_version is not None else "")
        + f" target: v{result.target_version}",
    ]
    if result.detail:
        lines.append(f"  {result.detail}")
    for note in result.notes:
        lines.append(f"  note: {note}")
    if result.live_consumers:
        lines.append(f"  live consumers: {', '.join(result.live_consumers)}")
    if result.backup_dir:
        lines.append(f"  backup: {result.backup_dir}")
    if not result.executed and result.state == PLANNED:
        lines.append("  (plan only; re-run with --write to perform it)")
    return "\n".join(lines)


_SUPPORTED = sorted(RECOGNIZED_SCHEMA_VERSIONS)

__all__ = (
    "ALREADY_CURRENT",
    "APPLIED",
    "BLOCKED_CONSUMERS_LIVE",
    "BLOCKED_CONSUMERS_UNMEASURABLE",
    "BLOCKED_FAILED",
    "BLOCKED_INVENTORY_UNREADABLE",
    "BLOCKED_MIGRATE_INSTEAD",
    "BLOCKED_STORE_UNSUPPORTED",
    "PLANNED",
    "STATUS_REPORTED",
    "AttestationStoreMaintenanceResult",
    "format_maintenance_text",
    "run_attestation_store_migrate",
    "run_attestation_store_rebuild",
    "run_attestation_store_status",
)
