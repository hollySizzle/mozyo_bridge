"""The glance family's reconcile-state index (Redmine #15747).

Companion leaf to :mod:`glance_snapshot_source` (module-health 1000-line ceiling —
the same extraction shape as #15707 / #15712): the issue -> reconcile-facts index the
active-lane projection joins onto. The read is the store's NON-CREATING
``records_readonly`` — the previous ``records()`` ran the read-write schema ensure, so
a glance in a fresh home minted ``state.sqlite`` as a side effect of a pure read
(#15711 j#108206 measured this exact call path). An absent store reads as the typed
empty index; only the reconcile write side (lane declaration / reconcile supervisor)
may create the store.
"""

from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_authority_projection import (  # noqa: E501
    ReconcileFacts,
    reconcile_facts_from_record,
)

#: Reconcile phases that are terminal — a record here owes no further reconcile action, so
#: an active (non-terminal) record for the same issue is preferred when projecting.
_RECONCILE_TERMINAL_PHASES = frozenset({"notified", "closed"})


def _reconcile_index(reconcile_store) -> dict[str, ReconcileFacts]:
    """Index the reconcile-state store by issue -> the most relevant record's facts. (fail-open)

    A store read failure or an unreadable component degrades to an empty index (every lane
    projects the fail-closed empty reconcile facts — never a fabricated attempt count). Among
    an issue's records, a non-terminal (active reconcile) record wins over a terminal one, and
    among equals the most recently updated; so the projection surfaces the live self-heal
    ladder rather than a stale closed cycle.
    """
    if reconcile_store is None:
        return {}
    try:
        records = reconcile_store.records_readonly()
    except Exception:  # noqa: BLE001 - the reconcile store is a rebuildable_cache; degrade
        return {}
    if records is None:  # unsupported / unreadable component: fail closed to no join
        return {}
    best: dict[str, object] = {}
    for rec in records:
        issue = str(getattr(rec, "issue_id", "") or "").strip()
        if not issue:
            continue
        current = best.get(issue)
        if current is None or _reconcile_more_relevant(rec, current):
            best[issue] = rec
    return {issue: reconcile_facts_from_record(rec) for issue, rec in best.items()}


def _reconcile_more_relevant(candidate, incumbent) -> bool:
    """Is ``candidate`` a better projection than ``incumbent`` for the same issue? (pure)"""
    cand_active = str(getattr(candidate, "phase", "")).strip() not in _RECONCILE_TERMINAL_PHASES
    inc_active = str(getattr(incumbent, "phase", "")).strip() not in _RECONCILE_TERMINAL_PHASES
    if cand_active != inc_active:
        return cand_active  # a live reconcile wins over a terminal one
    return str(getattr(candidate, "updated_at", "")) > str(getattr(incumbent, "updated_at", ""))


__all__ = (
    "_RECONCILE_TERMINAL_PHASES",
    "_reconcile_index",
    "_reconcile_more_relevant",
)
