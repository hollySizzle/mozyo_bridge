"""Pure receiver-replacement generation model (Redmine #13763 j#78052)."""

from __future__ import annotations

from dataclasses import dataclass

from mozyo_bridge.core.state.lane_lifecycle_model import (
    DISPOSITION_ACTIVE,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleRecord,
    ReleasePin,
    norm,
)


def quarantine_action_id(*, lane_id: str, role: str, locator: str) -> str:
    """The deterministic idempotency key for one approved old receiver."""
    values = tuple(norm(value) for value in (lane_id, role, locator))
    if not all(values):
        raise ValueError("quarantine action id requires lane, role, and locator")
    return f"quarantine:{values[0]}:{values[1]}:{values[2]}"


@dataclass(frozen=True)
class LaneReplacementRecord:
    """The replacement projection of one shared lane-lifecycle row."""

    key: LaneLifecycleKey
    issue_id: str
    state: str
    action_id: str
    pins: tuple[ReleasePin, ...]
    revision: int
    lane_active: bool
    updated_at: str
    decision: DecisionPointer | None

    @classmethod
    def from_lifecycle(cls, row: LaneLifecycleRecord) -> "LaneReplacementRecord":
        return cls(
            key=row.key,
            issue_id=row.issue_id,
            state=row.replacement_state,
            action_id=row.replacement_action_id,
            pins=row.replacement_slots,
            revision=row.revision,
            lane_active=row.lane_disposition == DISPOSITION_ACTIVE,
            updated_at=row.updated_at,
            decision=row.decision,
        )


__all__ = ("LaneReplacementRecord", "quarantine_action_id")
