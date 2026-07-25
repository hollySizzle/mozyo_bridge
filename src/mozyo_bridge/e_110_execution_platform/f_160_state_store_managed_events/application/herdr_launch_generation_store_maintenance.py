"""Public launch-generation-store maintenance use case (Redmine #14203 review j#87479 F2).

The launch-generation store (:mod:`mozyo_bridge.core.state.herdr_launch_generation`) declares
recovery policy ``rebuildable_cache``: a lost / corrupt store must **degrade** to fail-closed
(binding and recovery refuse) and be re-derivable, never brick the home. Without a public
recovery surface a corrupt ``herdr-launch-generation.sqlite`` would stop every future managed
launch on that home with no path back but raw file surgery — exactly what the store policy
forbids. This module is that surface, mirroring the #13882 attestation-store rail:

- ``status`` — read-only: what shape the store is and what it admits (creates nothing);
- ``rebuild`` — rotate a CORRUPT store into ``backups/`` and remove it so the next managed
  launch re-creates it fresh. Legitimate only because the store is a ``rebuildable_cache``:
  the next launch's reserve/finalize re-derives the current generation, and until then every
  read degrades fail-closed rather than to a stale generation. Backup-first (a backup failure
  aborts with the store byte-unchanged) and idempotent; no implicit repair ever.

**Active-consumer safety.** ``rebuild`` refuses while a proven consumer is live — a managed
agent that is live AND carries a generation row here — because discarding a live agent's
generation would leave its gateway recovery unable to bind until it relaunches. An
**unreadable** inventory, or a corrupt store whose rows cannot be enumerated while agents are
live, refuses just as hard (a corrupt store is not an empty one). The scope is cross-workspace
(the store is home-shared), never repo-scoped.

Actuation boundary: this module never closes, sends to, or launches a process — it copies a
file and removes one, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    quarantine_attestation_store_artifacts,
    remove_attestation_store_artifacts,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    GENERATION_STORE_ABSENT,
    GENERATION_STORE_CORRUPT,
    GENERATION_STORE_HEALTHY,
    HERDR_LAUNCH_GENERATION_SCHEMA_VERSION,
    HerdrLaunchGenerationStore,
    LaunchGenerationStoreLockBusy,
    LaunchGenerationStoreLockReleaseError,
    LaunchGenerationStoreLockUnavailable,
    herdr_launch_generation_path,
    launch_generation_store_lock,
    probe_launch_generation_store,
)
from mozyo_bridge.core.state.state_store import StateStoreError

# --- Outcome vocabulary (fail-closed; only *_OK / *_PLANNED admit an action). ---------
STATUS_REPORTED = "status_reported"
PLANNED = "planned"
APPLIED = "applied"
#: Verified no-op — there is no corrupt store to rebuild (absent or healthy).
ALREADY_CURRENT = "already_current"
#: Refused: a healthy store holds live generations; rebuild would discard them.
BLOCKED_STORE_HEALTHY = "blocked_store_healthy"
#: Refused: managed agents are live and hold generations here.
BLOCKED_CONSUMERS_LIVE = "blocked_consumers_live"
#: Refused: liveness could not be measured (an unreadable inventory is not an empty one).
BLOCKED_INVENTORY_UNREADABLE = "blocked_inventory_unreadable"
#: Refused: agents are live but the store's rows cannot be enumerated (it is corrupt), so it
#: cannot be proven none of them consume it.
BLOCKED_CONSUMERS_UNMEASURABLE = "blocked_consumers_unmeasurable"
#: Refused: the backup/removal itself failed (the store is left untouched).
BLOCKED_FAILED = "blocked_failed"
#: NOT a success: the rebuild body completed (its side effects stand as reported) but the
#: store lock could not be released, so it may still be held — a subsequent managed write
#: could stall. Distinct from ``BLOCKED_FAILED`` (which is "nothing removed") because the
#: rotation truth is preserved, and distinct from ``APPLIED`` because the operation is not
#: cleanly complete (review j#87512 F1).
BLOCKED_RELEASE_UNVERIFIED = "blocked_release_unverified"

_OK_STATES = frozenset({STATUS_REPORTED, PLANNED, APPLIED, ALREADY_CURRENT})

_CONSUMERS_NONE = "none"
_CONSUMERS_PRESENT = "present"
_CONSUMERS_UNMEASURABLE = "unmeasurable"


@dataclass(frozen=True)
class LaunchGenerationStoreMaintenanceResult:
    """The auditable result of one maintenance intent (structured, not only stderr)."""

    intent: str
    state: str
    store_state: str = ""
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
            "store_state": self.store_state,
            "target_version": HERDR_LAUNCH_GENERATION_SCHEMA_VERSION,
            "detail": self.detail,
            # Operator-facing evidence. A pasteable durable record redacts absolute paths;
            # that is the caller's boundary, not this payload's.
            "backup_dir": str(self.backup_dir) if self.backup_dir else None,
            "live_consumers": list(self.live_consumers),
            "notes": list(self.notes),
        }


def _measure_consumers(view, home: Path) -> tuple:
    """Measure this store's live consumers -> ``(state, names)``. Never guesses.

    A consumer is an agent that is live AND carries a generation row here. An empty fleet is
    proof of no consumers whatever the store's state (checked before the store is read); only
    when agents ARE live does readability matter, and a store whose rows cannot be enumerated
    is :data:`_CONSUMERS_UNMEASURABLE`, never "none".
    """
    if not view.backend_selected:
        return _CONSUMERS_NONE, ()
    if not view.ok:
        return _CONSUMERS_UNMEASURABLE, ()
    live = {agent.name for agent in view.managed_agents}
    if not live:
        return _CONSUMERS_NONE, ()
    held = HerdrLaunchGenerationStore(home=home).assigned_names()
    if held is None:
        return _CONSUMERS_UNMEASURABLE, tuple(sorted(live))
    matched = tuple(sorted(live & held))
    return (_CONSUMERS_PRESENT, matched) if matched else (_CONSUMERS_NONE, ())


def _consumer_gate(
    view, home: Path, store_state: str
) -> Optional[LaunchGenerationStoreMaintenanceResult]:
    """Refuse a rebuild while consumers are live / unmeasurable, else ``None``."""
    state, names = _measure_consumers(view, home)
    if state == _CONSUMERS_NONE:
        return None
    if state == _CONSUMERS_PRESENT:
        return LaunchGenerationStoreMaintenanceResult(
            intent="rebuild",
            state=BLOCKED_CONSUMERS_LIVE,
            store_state=store_state,
            detail=(
                f"{len(names)} live managed agent(s) hold a launch generation in this store "
                f"({', '.join(names)}); rebuilding would discard their generation and block "
                f"their gateway recovery until they relaunch. Retire / close them first, "
                f"then re-run"
            ),
            live_consumers=names,
        )
    if not view.ok:
        return LaunchGenerationStoreMaintenanceResult(
            intent="rebuild",
            state=BLOCKED_INVENTORY_UNREADABLE,
            store_state=store_state,
            detail=(
                f"the live herdr inventory is unreadable ({view.reason}: {view.detail}); "
                f"liveness cannot be measured, and an unreadable inventory is not an empty "
                f"one. Refusing to touch the store"
            ),
        )
    return LaunchGenerationStoreMaintenanceResult(
        intent="rebuild",
        state=BLOCKED_CONSUMERS_UNMEASURABLE,
        store_state=store_state,
        detail=(
            f"{len(names)} managed agent(s) are live but this store's rows cannot be "
            f"enumerated (it is corrupt), so it cannot be proven that none of them hold a "
            f"generation here ({', '.join(names)}). Retire / close the live agent(s) first — "
            f"with an empty fleet nothing can be consuming this store, and the rebuild is "
            f"provably safe"
        ),
        live_consumers=names,
    )


def run_launch_generation_store_status(
    *, home: Path
) -> LaunchGenerationStoreMaintenanceResult:
    """Read-only report of the selected store's shape (creates nothing)."""
    state, detail = probe_launch_generation_store(herdr_launch_generation_path(home))
    notes: list[str] = []
    if state == GENERATION_STORE_ABSENT:
        notes.append(
            "no store yet; the next managed launch reserves it at "
            f"v{HERDR_LAUNCH_GENERATION_SCHEMA_VERSION}"
        )
    elif state == GENERATION_STORE_HEALTHY:
        notes.append("current shape; reserve / finalize / binding / recovery all operate")
    else:
        notes.append(
            "the store is corrupt / partial / foreign; reads fail closed (recovery "
            "degrades, never a stale generation). Run `rebuild --write` to rotate it aside "
            "so the next managed launch re-creates it"
        )
    return LaunchGenerationStoreMaintenanceResult(
        intent="status",
        state=STATUS_REPORTED,
        store_state=state,
        detail=f"launch-generation store is {state}",
        notes=tuple(notes),
    )


def _blocked_by_lock(exc: Exception) -> LaunchGenerationStoreMaintenanceResult:
    """Report a rebuild that never started because the lock could not be taken.

    Two typed acquisition outcomes, rendered as a structured public refusal (never a raw
    traceback across the recovery boundary, review j#87496 F1): the store is in use (a live
    managed-launch write holds it), or the lock itself is unavailable (an unwritable home / a
    filesystem without advisory locks). In BOTH cases nothing was published or removed.
    """
    if isinstance(exc, LaunchGenerationStoreLockBusy):
        detail = (
            f"the launch-generation store is in use and this rebuild did not start ({exc}). "
            f"Nothing was published and nothing was removed. Maintenance never queues ahead "
            f"of an in-flight managed launch write; re-run once it finishes"
        )
    else:
        detail = (
            f"the launch-generation store lock could not be acquired and this rebuild did "
            f"not start ({exc}). Nothing was published and nothing was removed. Check the "
            f"home's permissions / filesystem, then re-run"
        )
    return LaunchGenerationStoreMaintenanceResult(
        intent="rebuild", state=BLOCKED_FAILED, detail=detail
    )


def _run_rebuild_locked(
    *, home: Path, view, write: bool
) -> LaunchGenerationStoreMaintenanceResult:
    """Rotate a CORRUPT store into ``backups/`` and remove it (backup-first, consumer-gated).

    On the ``write`` path this runs UNDER the exclusive store lock, and it re-probes HERE (not
    before the lock) so the generation it acts on cannot be replaced underneath it — the path
    ABA that let a stale rebuild quarantine a peer's fresh, healthy store (review j#87488 P1).
    Only a corrupt store is a rebuild target: an absent store is already the rebuilt state, and
    a healthy store holds live generations that rebuild would discard (refused).
    """
    path = herdr_launch_generation_path(home)
    state, detail = probe_launch_generation_store(path)

    if state == GENERATION_STORE_ABSENT:
        return LaunchGenerationStoreMaintenanceResult(
            intent="rebuild",
            state=ALREADY_CURRENT,
            store_state=state,
            detail=(
                "no store exists; nothing to rebuild (the next managed launch creates it). "
                "Creating one here would fabricate an empty pointer no launch asked for"
            ),
        )
    if state == GENERATION_STORE_HEALTHY:
        return LaunchGenerationStoreMaintenanceResult(
            intent="rebuild",
            state=BLOCKED_STORE_HEALTHY,
            store_state=state,
            detail=(
                "the store is healthy and may hold live generations; rebuild discards them "
                "and is refused. There is nothing corrupt to recover"
            ),
        )

    # A corrupt store, and agents may be running against it. Gate on live consumers BEFORE
    # the idempotent success so a replay never reports success while consumers are live.
    blocked = _consumer_gate(view, home, state)
    if blocked is not None:
        return blocked

    if not write:
        return LaunchGenerationStoreMaintenanceResult(
            intent="rebuild",
            state=PLANNED,
            store_state=state,
            detail=(
                "would back the corrupt store up under backups/ then remove it; the next "
                "managed launch re-creates it. Re-run with --write to perform it"
            ),
            notes=(f"probe: {detail}",),
        )

    # Raw quarantine of the whole artifact set — the store is proven corrupt above, so its
    # bytes ARE the evidence and there is nothing to snapshot logically.
    try:
        backup_dir = quarantine_attestation_store_artifacts(path)
    except (StateStoreError, OSError) as exc:
        # Nothing was removed yet, so "untouched" is a true statement HERE and only here.
        return LaunchGenerationStoreMaintenanceResult(
            intent="rebuild",
            state=BLOCKED_FAILED,
            store_state=state,
            detail=(
                f"rebuild aborted before any removal: {exc}. The store is untouched and "
                f"nothing was lost; free space / permissions, then re-run"
            ),
        )
    if backup_dir is None:
        # The probe above proved a store was here, so ``None`` means it vanished between the
        # probe and the quarantine (a peer rotated it, or something outside this rail removed
        # it) — backup-first could NOT be proven. Reporting APPLIED would fabricate a backup
        # that never existed. Re-running converges (a subsequent probe sees ABSENT ->
        # ALREADY_CURRENT).
        return LaunchGenerationStoreMaintenanceResult(
            intent="rebuild",
            state=BLOCKED_FAILED,
            store_state=state,
            detail=(
                "the store disappeared after this rebuild's probe and before it could be "
                "preserved, so backup-first cannot be proven. Nothing was published and "
                "nothing was removed by this run. Re-run to converge"
            ),
        )
    try:
        remove_attestation_store_artifacts(path)
    except OSError as exc:
        # The rotation began, so the store is NOT untouched and must not be reported as such.
        # Sidecars are removed before the main file, so an interruption leaves the main file
        # present, the store still probes as existing, and re-running finishes the rotation.
        return LaunchGenerationStoreMaintenanceResult(
            intent="rebuild",
            state=BLOCKED_FAILED,
            store_state=state,
            detail=(
                f"rebuild was interrupted partway through rotating the store away ({exc}); "
                f"the backup is complete and published, but some artifacts may already be "
                f"removed — the store is NOT untouched. Re-run this same command to finish"
            ),
            backup_dir=backup_dir,
            executed=True,
        )
    return LaunchGenerationStoreMaintenanceResult(
        intent="rebuild",
        state=APPLIED,
        store_state=state,
        detail=(
            "corrupt store rotated into backups/ and removed; the next managed launch "
            "re-creates a fresh generation store"
        ),
        backup_dir=backup_dir,
        executed=True,
    )


def _release_failed_result(
    body_result: Optional[LaunchGenerationStoreMaintenanceResult], exc: Exception
) -> LaunchGenerationStoreMaintenanceResult:
    """Render a rebuild whose BODY completed but whose lock could not be released.

    Phase-aware (review j#87512 F1): the body's side-effect truth is preserved — a completed
    rotation keeps its ``backup_dir`` / ``executed`` / ``store_state`` — while the release
    failure is surfaced as a NON-success with a process-exit / retry action. It is neither
    ``applied`` (the operation is not cleanly complete) nor ``blocked_failed``'s
    "nothing removed" (that would erase a real rotation).
    """
    executed = bool(getattr(body_result, "executed", False))
    backup_dir = getattr(body_result, "backup_dir", None)
    store_state = getattr(body_result, "store_state", "")
    prior = getattr(body_result, "detail", "") or ""
    side = (
        f"the rebuild's side effects stand as reported ({prior})"
        if executed
        else f"no store artifact was removed ({prior or 'nothing was rotated'})"
    )
    return LaunchGenerationStoreMaintenanceResult(
        intent="rebuild",
        state=BLOCKED_RELEASE_UNVERIFIED,
        store_state=store_state,
        detail=(
            f"the rebuild completed but the store lock could NOT be released ({exc}); {side}, "
            f"yet the lock may still be held, so a subsequent managed write could stall. "
            f"Restart this process (or investigate the home's filesystem), then re-run"
        ),
        backup_dir=backup_dir,
        executed=executed,
    )


def run_launch_generation_store_rebuild(
    *, home: Path, view, write: bool = False
) -> LaunchGenerationStoreMaintenanceResult:
    """Rotate a CORRUPT store into ``backups/`` and remove it (backup-first, consumer-gated).

    A read-only plan mutates nothing and takes no lock. The ``--write`` path holds the store
    lock EXCLUSIVE (non-blocking) across the whole run — acquired BEFORE the probe — so the
    generation cannot be replaced underneath it and a live managed-launch write can never be
    clobbered (review j#87488 P1). Contention reports blocked with nothing removed; a lock
    that cannot be RELEASED after the body is a non-success that preserves the body's
    side-effect truth (review j#87512 F1).
    """
    if not write:
        return _run_rebuild_locked(home=home, view=view, write=False)
    body_result: Optional[LaunchGenerationStoreMaintenanceResult] = None
    try:
        with launch_generation_store_lock(home, exclusive=True, blocking=False):
            body_result = _run_rebuild_locked(home=home, view=view, write=True)
    except LaunchGenerationStoreLockReleaseError as exc:
        # The body completed (``body_result`` is set) but the lock could not be released.
        return _release_failed_result(body_result, exc)
    except (LaunchGenerationStoreLockBusy, LaunchGenerationStoreLockUnavailable) as exc:
        return _blocked_by_lock(exc)
    return body_result


def format_maintenance_text(result: LaunchGenerationStoreMaintenanceResult) -> str:
    """A compact human rendering (the CLI's non-JSON output)."""
    lines = [
        f"launch-generation store {result.intent}: {result.state}",
        f"  store: {result.store_state}",
        f"  {result.detail}",
    ]
    if result.backup_dir:
        lines.append(f"  backup: {result.backup_dir}")
    if result.live_consumers:
        lines.append(f"  live consumers: {', '.join(result.live_consumers)}")
    for note in result.notes:
        lines.append(f"  - {note}")
    return "\n".join(lines)


__all__ = (
    "ALREADY_CURRENT",
    "APPLIED",
    "BLOCKED_CONSUMERS_LIVE",
    "BLOCKED_CONSUMERS_UNMEASURABLE",
    "BLOCKED_FAILED",
    "BLOCKED_INVENTORY_UNREADABLE",
    "BLOCKED_RELEASE_UNVERIFIED",
    "BLOCKED_STORE_HEALTHY",
    "PLANNED",
    "STATUS_REPORTED",
    "LaunchGenerationStoreMaintenanceResult",
    "format_maintenance_text",
    "run_launch_generation_store_rebuild",
    "run_launch_generation_store_status",
)
