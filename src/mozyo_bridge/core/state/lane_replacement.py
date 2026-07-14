"""Receiver-replacement store facade over the shared lifecycle CAS row.

The facade keeps replacement callers from gaining disposition/release mutation
authority while deliberately delegating every write to :class:`LaneLifecycleStore`.
All three lifecycle axes therefore share one ``BEGIN IMMEDIATE`` revision fence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import (
    CasOutcome,
    DecisionPointer,
    LaneLifecycleKey,
    ReleasePin,
)
from mozyo_bridge.core.state.lane_replacement_model import LaneReplacementRecord


class LaneReplacementStore:
    """Narrow request/outcome/get interface for receiver replacement."""

    def __init__(self, *, home: Path | None = None, path: Path | None = None) -> None:
        self._lifecycle = LaneLifecycleStore(home=home, path=path)

    @property
    def path(self) -> Path:
        return self._lifecycle.path

    def get_replacement(
        self, key: LaneLifecycleKey
    ) -> Optional[LaneReplacementRecord]:
        row = self._lifecycle.get(key)
        return LaneReplacementRecord.from_lifecycle(row) if row is not None else None

    def request_replacement(
        self,
        key: LaneLifecycleKey,
        *,
        expected_revision: int,
        action_id: str,
        pins: Iterable[ReleasePin],
        decision: DecisionPointer,
        now: str | None = None,
    ) -> CasOutcome:
        return self._lifecycle.request_replacement(
            key,
            expected_revision=expected_revision,
            action_id=action_id,
            pins=pins,
            decision=decision,
            now=now,
        )

    def record_replacement_outcome(
        self,
        key: LaneLifecycleKey,
        *,
        action_id: str,
        expected_revision: int,
        target: str,
        now: str | None = None,
    ) -> CasOutcome:
        return self._lifecycle.record_replacement_outcome(
            key,
            action_id=action_id,
            expected_revision=expected_revision,
            target=target,
            now=now,
        )


__all__ = ("LaneReplacementStore",)
