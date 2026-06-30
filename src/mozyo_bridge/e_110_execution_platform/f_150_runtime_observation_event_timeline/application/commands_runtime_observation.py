"""Command handler for runtime observation reload (Redmine #12224).

``mozyo-bridge observe reload`` re-captures runtime observation snapshots so an
operator can explicitly refresh a diagnostic / display view and see how old it
is. The semantics are the runtime observation snapshot contract codified in
:mod:`mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation` (closed under #12223, recorded in
``vibes/docs/logics/runtime-observability-boundary.md``):

- The reload refreshes diagnostic / display snapshots ONLY. It never updates
  workflow truth, approval, review, routing, close, or completion — those stay
  with the Redmine durable record and the governed workflow rules.
- A snapshot never implies action safety. Side-effecting commands keep doing
  their own action-time live preflight (#12226), regardless of any displayed
  snapshot age.
- Fail-closed: a stale / unreadable / contradictory source derives
  ``unknown`` / ``reload_required``, never ``healthy``. The process exit code is
  non-zero when any requested snapshot is in that fail-closed band, so a caller
  can never read a stale snapshot as healthy from the exit status alone (the
  facts are still printed, like ``doctor``).

The probes are read-only and best-effort. The tmux probe re-reads live tmux
runtime (degrading to the inventory cache when tmux is unavailable); the
otel/cache probe reads the local OTel event store file with no network call.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain import runtime_observation as ro

SOURCE_TMUX = "tmux"
SOURCE_OTEL = "otel"
SOURCE_ALL = "all"
SOURCE_CHOICES = (SOURCE_TMUX, SOURCE_OTEL, SOURCE_ALL)

DEFAULT_MAX_AGE_SECONDS = 30.0
DEFAULT_EXPIRED_AFTER_SECONDS = 300.0


def _tmux_snapshot(
    *,
    inv_source: str,
    collected_at: Optional[str],
    record_count: int,
    notes: Tuple[str, ...],
    now: datetime,
    max_age_seconds: float,
    expired_after_seconds: float,
) -> ro.RuntimeObservationSnapshot:
    """Map an inventory capture into a runtime observation snapshot.

    A live tmux runtime read is a strong runtime signal observed *now*; a
    degraded read served from the inventory cache is a projection whose
    ``observed_at`` is the cache's collection time (so it ages and fails
    closed), and an empty degraded read with no cache is unreadable.
    """
    from mozyo_bridge.session_inventory import SOURCE_RUNTIME

    if inv_source == SOURCE_RUNTIME:
        ref_at = collected_at or "?"
        return ro.make_snapshot(
            source=ro.SOURCE_TMUX,
            method=ro.METHOD_LIVE_QUERY,
            observed_at=collected_at,
            readability=ro.READABILITY_READABLE,
            strength=ro.STRENGTH_STRONG_RUNTIME_SIGNAL,
            now=now,
            max_age_seconds=max_age_seconds,
            expired_after_seconds=expired_after_seconds,
            source_refs=(f"tmux:runtime@{ref_at}",),
            notes=(f"live tmux runtime: {record_count} agent pane(s) observed",)
            + notes,
        )
    # Degraded: tmux unavailable, serving (or failing to serve) the cache.
    readable = (
        ro.READABILITY_PARTIAL if record_count > 0 else ro.READABILITY_UNREADABLE
    )
    ref_at = collected_at or "?"
    return ro.make_snapshot(
        source=ro.SOURCE_CACHE,
        method=ro.METHOD_PROJECTION_READ,
        observed_at=collected_at,
        readability=readable,
        strength=ro.STRENGTH_PROJECTION_ONLY,
        now=now,
        max_age_seconds=max_age_seconds,
        expired_after_seconds=expired_after_seconds,
        source_refs=(f"cache:inventory.sqlite@{ref_at}",),
        notes=("tmux unavailable; served inventory cache projection",) + notes,
    )


def _otel_snapshot(
    *,
    store_exists: bool,
    last_write: Optional[str],
    total: int,
    now: datetime,
    max_age_seconds: float,
    expired_after_seconds: float,
    error: Optional[str] = None,
) -> ro.RuntimeObservationSnapshot:
    """Map an OTel event store read into a runtime observation snapshot.

    The store is a best-effort cache, never the source of truth. A missing or
    unreadable store is unreadable; a present-but-empty store has no
    observation time and derives ``unknown``; otherwise freshness is the age of
    the store's ``last_write``.
    """
    if not store_exists or error is not None:
        note = (
            f"otel event store unreadable: {error}"
            if error is not None
            else "otel event store does not exist (receiver never ran / wrong path)"
        )
        return ro.make_snapshot(
            source=ro.SOURCE_CACHE,
            method=ro.METHOD_PROJECTION_READ,
            observed_at=None,
            readability=ro.READABILITY_UNREADABLE,
            strength=ro.STRENGTH_PROJECTION_ONLY,
            now=now,
            max_age_seconds=max_age_seconds,
            expired_after_seconds=expired_after_seconds,
            source_refs=("otel:events",),
            notes=(note,),
        )
    ref_at = last_write or "?"
    return ro.make_snapshot(
        source=ro.SOURCE_CACHE,
        method=ro.METHOD_PROJECTION_READ,
        observed_at=last_write,
        readability=ro.READABILITY_READABLE,
        strength=ro.STRENGTH_PROJECTION_ONLY,
        now=now,
        max_age_seconds=max_age_seconds,
        expired_after_seconds=expired_after_seconds,
        source_refs=(f"otel:events@{ref_at}",),
        notes=(f"otel event store cache: {total} event(s)",),
    )


def snapshot_from_inventory(
    snapshot,
    *,
    now: datetime,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    expired_after_seconds: float = DEFAULT_EXPIRED_AFTER_SECONDS,
) -> ro.RuntimeObservationSnapshot:
    """Map a session-inventory snapshot to a runtime observation envelope.

    The single inventory -> envelope mapping, shared by the ``observe reload``
    CLI (#12224, via :func:`_capture_tmux`) and the cockpit Web UI (#12225, via
    :func:`mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application.cockpit_ui.attach_observation`). Both faces
    derive ``observed_at`` / ``freshness`` / ``readability`` / ``display_state``
    from the same logic, so the CLI and the GUI never disagree about whether the
    displayed runtime view is fresh or must be reloaded. The caller supplies the
    already-captured ``snapshot`` so the envelope describes exactly the rows it
    is shown alongside.
    """
    return _tmux_snapshot(
        inv_source=snapshot.source,
        collected_at=snapshot.collected_at,
        record_count=len(snapshot.records),
        notes=tuple(snapshot.notes),
        now=now,
        max_age_seconds=max_age_seconds,
        expired_after_seconds=expired_after_seconds,
    )


def _capture_tmux(
    *, home: Optional[Path], now: datetime, max_age: float, expired_after: float
) -> ro.RuntimeObservationSnapshot:
    from mozyo_bridge.session_inventory import take_inventory

    snapshot = take_inventory(home=home, persist=True)
    return snapshot_from_inventory(
        snapshot,
        now=now,
        max_age_seconds=max_age,
        expired_after_seconds=expired_after,
    )


def _capture_otel(
    *,
    db: Optional[Path],
    home: Optional[Path],
    now: datetime,
    max_age: float,
    expired_after: float,
) -> ro.RuntimeObservationSnapshot:
    from mozyo_bridge.otel_store import OtelEventStore, OtelStoreError

    store = OtelEventStore(db, home=home)
    if not store.path.exists():
        return _otel_snapshot(
            store_exists=False,
            last_write=None,
            total=0,
            now=now,
            max_age_seconds=max_age,
            expired_after_seconds=expired_after,
        )
    try:
        counts = store.counts()
    except OtelStoreError as exc:
        return _otel_snapshot(
            store_exists=True,
            last_write=None,
            total=0,
            now=now,
            max_age_seconds=max_age,
            expired_after_seconds=expired_after,
            error=str(exc),
        )
    return _otel_snapshot(
        store_exists=True,
        last_write=counts.get("last_write"),
        total=int(counts.get("total") or 0),
        now=now,
        max_age_seconds=max_age,
        expired_after_seconds=expired_after,
    )


def _render(snapshots, *, as_json: bool, now_iso: str) -> None:
    if as_json:
        import json as _json

        payload = {
            "observed_now": now_iso,
            "note": ro.RELOAD_DIAGNOSTIC_ONLY_NOTE,
            "snapshots": [snap.as_payload() for snap in snapshots],
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(
        "SOURCE\tMETHOD\tOBSERVED_AT\tFRESHNESS\tREADABILITY\t"
        "DISPLAY_STATE\tSTALE_REASON\tSTRENGTH"
    )
    for snap in snapshots:
        print(
            "\t".join(
                [
                    snap.source,
                    snap.method,
                    snap.observed_at or "-",
                    snap.freshness,
                    snap.readability,
                    snap.display_state,
                    snap.stale_reason or "-",
                    snap.strength,
                ]
            )
        )
        if snap.source_refs:
            print(f"  source_refs: {', '.join(snap.source_refs)}")
        for note in snap.notes:
            print(f"  note: {note}")
    print(f"observed_now: {now_iso}")
    print(RELOAD_FOOTER)


RELOAD_FOOTER = (
    "reload is diagnostic/display only; it does not update workflow truth, "
    "approval, routing, close, or completion (read the Redmine durable record), "
    "and it does not authorize action (side-effecting commands run action-time "
    "live preflight)."
)


def cmd_observe_reload(args: argparse.Namespace) -> int:
    """Refresh runtime observation snapshots (Redmine #12224). Read-only.

    Re-captures display/diagnostic snapshots for the requested source(s) and
    prints their freshness / readability envelope. Exit code is non-zero when
    any requested snapshot is fail-closed (``unknown`` / ``reload_required``),
    so a stale snapshot is never reported as healthy via the exit status.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    source = getattr(args, "source", None) or SOURCE_ALL
    max_age = float(getattr(args, "max_age", None) or DEFAULT_MAX_AGE_SECONDS)
    expired_after = float(
        getattr(args, "expired_after", None) or DEFAULT_EXPIRED_AFTER_SECONDS
    )
    db = Path(args.db).expanduser() if getattr(args, "db", None) else None
    home = Path(args.home).expanduser() if getattr(args, "home", None) else None

    snapshots = []
    if source in (SOURCE_TMUX, SOURCE_ALL):
        snapshots.append(
            _capture_tmux(
                home=home, now=now, max_age=max_age, expired_after=expired_after
            )
        )
    if source in (SOURCE_OTEL, SOURCE_ALL):
        snapshots.append(
            _capture_otel(
                db=db,
                home=home,
                now=now,
                max_age=max_age,
                expired_after=expired_after,
            )
        )

    _render(snapshots, as_json=getattr(args, "as_json", False), now_iso=now_iso)
    return 1 if any(snap.needs_reload for snap in snapshots) else 0
