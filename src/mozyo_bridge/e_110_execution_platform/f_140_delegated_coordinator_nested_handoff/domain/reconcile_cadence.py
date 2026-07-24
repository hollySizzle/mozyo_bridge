"""Provider reconciliation cadence: watermark gate + exponential backoff + jitter (Redmine #14150).

Pure domain helpers extracted from ``workspace_supervisor`` (Redmine #14219 T3 review j#87154 R1
module-health leaf split) so that module stays under the line ceiling. No behaviour change — these
are the same functions, re-exported from ``workspace_supervisor`` so every caller's import surface is
unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

def _parse_iso(value: object) -> "datetime | None":
    """Parse an ISO-8601 timestamp to an aware UTC ``datetime`` (``None`` if unparseable; pure)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def reconcile_backoff_seconds(
    base_interval_seconds: int,
    consecutive_empty_passes: int,
    *,
    max_interval_seconds: int,
    jitter_unit: float = 0.0,
    jitter_fraction: float = 0.0,
) -> int:
    """The next provider-reconcile due delay (seconds), backed off + jittered (Redmine #14150; pure).

    The provider reconciliation leg must not re-read every workspace / every journal on a fixed tight
    cadence. When consecutive passes find nothing new, the due interval backs off exponentially from
    ``base_interval_seconds`` (doubling per empty pass) up to ``max_interval_seconds``, so an idle
    fleet quiesces toward the ceiling instead of polling the provider at the floor. ``jitter_unit`` is
    an injected value in ``[0, 1)`` (a seam — the caller supplies a deterministic value in tests and a
    real RNG draw in production, so this stays pure and reproducible); it spreads the due time by up to
    ``jitter_fraction`` of the backed-off interval so a fleet of workspaces does not thunder the
    provider in lockstep. Returns an int in ``[base, max]`` (jitter only ADDS, never below base).
    """
    base = max(1, int(base_interval_seconds))
    ceiling = max(base, int(max_interval_seconds))
    empties = max(0, int(consecutive_empty_passes))
    # Exponential backoff, capped — guard the shift so a large empty count never overflows.
    backed = min(ceiling, base * (2 ** min(empties, 30)))
    unit = min(max(float(jitter_unit), 0.0), 0.999999)
    fraction = min(max(float(jitter_fraction), 0.0), 1.0)
    jitter = int(backed * fraction * unit)
    return min(ceiling, backed + jitter)


def should_reconcile_source(
    last_reconciled_at: object,
    now: object,
    due_after_seconds: int,
) -> bool:
    """True iff the provider reconcile watermark for a source is DUE (Redmine #14150; pure).

    ``last_reconciled_at`` is the durable watermark of the last completed provider read for a source
    (blank / unparseable -> never reconciled -> due). ``now`` is the current ISO timestamp; the source
    is due when ``due_after_seconds`` have elapsed since the watermark. This is the differential-fetch
    gate: a drain-only tick never sets the watermark (it made no provider read), so it never suppresses
    a genuine reconcile; only a completed provider read advances it. An unparseable ``now`` fails toward
    reconciling (never silently skips the provider fallback).
    """
    now_dt = _parse_iso(now)
    if now_dt is None:
        return True
    last_dt = _parse_iso(last_reconciled_at)
    if last_dt is None:
        return True
    return (now_dt - last_dt).total_seconds() >= max(0, int(due_after_seconds))


__all__ = ("reconcile_backoff_seconds", "should_reconcile_source")
