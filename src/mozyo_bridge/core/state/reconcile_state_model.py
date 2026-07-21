"""Reconcile-state — pure key / record / validation (Redmine #13758).

The typed half of the event-driven reconcile-state component, kept apart from the schema
guard (:mod:`...reconcile_state_schema`) and the CAS writes
(:mod:`...reconcile_state`). One durable row per ``(workspace_id, lane_id,
dispatch_anchor)`` holds the **derived** self-heal-ladder bookkeeping for one dispatch: the
expected gate / owner re-derivable from Redmine, and the one genuinely accumulated fact —
the edge-based ``reconcile_failure_count``. The ``CasOutcome`` vocabulary is the shared
:mod:`mozyo_bridge.core.state.lane_lifecycle_model`, re-exported so callers have one import
surface.

This is a ``rebuildable_cache`` component (``managed-state-model.md``): its loss degrades
to a fresh reconcile cycle (``failure_count`` 0, expected fields re-derived from Redmine +
the lane registry) — safe by construction (more self-heals, never a mis-send), satisfying
the acceptance criterion "local derived state を削除しても Redmine + outbox + lane registry
から安全に再構築できる". Redmine remains the sole workflow truth; this row never authorizes
a send on its own — the reconcile decision is the pure
:mod:`...domain.reconcile_state_machine` over an action-time Redmine re-read.
"""

from __future__ import annotations

from dataclasses import dataclass

from mozyo_bridge.core.state.lane_lifecycle_model import (
    CAS_APPLIED,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    CasOutcome,
)


def norm(value: object) -> str:
    """Trim to a stable comparison string (blank for ``None``)."""
    return str(value or "").strip()


@dataclass(frozen=True)
class ReconcileStateKey:
    """Identity of one reconcile row: ``(workspace_id, lane_id, dispatch_anchor)``.

    ``dispatch_anchor`` is the durable anchor of the dispatch this cycle reconciles
    (``<issue>:<journal>`` of the implementation / review request) and encodes the exact
    action / review generation, so a new dispatch is a new row (the failure counter starts
    fresh) and a stale generation never shares a record with a live one.
    """

    workspace_id: str
    lane_id: str
    dispatch_anchor: str

    def as_row(self) -> tuple[str, str, str]:
        return (
            norm(self.workspace_id),
            norm(self.lane_id),
            norm(self.dispatch_anchor),
        )

    @property
    def valid(self) -> bool:
        return all(self.as_row())


@dataclass(frozen=True)
class ReconcileStateRecord:
    """One persisted reconcile row (all columns of ``reconcile_state_records``)."""

    workspace_id: str
    lane_id: str
    dispatch_anchor: str
    lane_generation: int
    issue_id: str
    latest_journal_id: str
    expected_gate: str
    expected_next_owner: str
    phase: str
    reconcile_failure_count: int
    deadline: str
    last_disposition: str
    escalated: bool
    callback_outbox_state: str
    last_observed_runtime: str
    revision: int
    created_at: str
    updated_at: str

    @property
    def key(self) -> ReconcileStateKey:
        return ReconcileStateKey(
            workspace_id=self.workspace_id,
            lane_id=self.lane_id,
            dispatch_anchor=self.dispatch_anchor,
        )


__all__ = (
    "CAS_APPLIED",
    "CAS_NOT_FOUND",
    "CAS_STALE_REVISION",
    "CAS_UNEXPECTED_STATE",
    "CasOutcome",
    "norm",
    "ReconcileStateKey",
    "ReconcileStateRecord",
)
