"""Typed value and decision model for one live Herdr pair placement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

from mozyo_bridge.core.state.lane_lifecycle import ProcessGenerationPin
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (  # noqa: E501
    safe_text,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_managed_column_scope import (  # noqa: E501
    ManagedColumnScope,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    PaneRect,
    SplitInfo,
    ratio_verdict,
)


PLAN_READY = "ready"
PLAN_MATCHED = "matched"
PLAN_REFUSED = "refused"

APPLY_APPLIED = "applied"
APPLY_MATCHED = "matched"
APPLY_REFUSED = "refused"
APPLY_FAILED = "failed"
APPLY_PARTIAL = "partial_failure"

REASON_OK = "ok"
REASON_INVENTORY_UNAVAILABLE = "inventory_unavailable"
REASON_WORKSPACE_UNKNOWN = "workspace_unknown"
REASON_CONFIG_INVALID = "config_invalid"
REASON_PAIR_INVALID = "pair_invalid"
REASON_GENERATION_UNVERIFIED = "generation_unverified"
REASON_LAYOUT_UNAVAILABLE = "layout_unavailable"
REASON_NOT_DEDICATED_PAIR = "not_dedicated_pair"
REASON_GEOMETRY_UNSUPPORTED = "geometry_unsupported"
REASON_STALE = "stale_before_apply"
REASON_COMMAND_FAILED = "herdr_command_failed"
REASON_POSTCONDITION_FAILED = "postcondition_failed"

MOVE_CHANGED = "changed"
MOVE_UNCHANGED = "unchanged"
MOVE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlacementTarget:
    split: str
    order: tuple[str, str]
    ratio: float
    declared_pins: tuple[ProcessGenerationPin, ...] = field(
        default=(), repr=False
    )

    def as_payload(self) -> dict[str, object]:
        return {
            "split": safe_text(self.split),
            "order": [safe_text(provider) for provider in self.order],
            "ratio": self.ratio,
        }


@dataclass(frozen=True)
class LiveSlot:
    provider: str
    assigned_name: str = field(repr=False)
    pane_id: str = field(repr=False)
    generation: str = field(repr=False)
    runtime_revision: str = field(default="", repr=False)

    @property
    def fingerprint(self) -> tuple[str, str, str, str, str]:
        return (
            self.provider,
            self.assigned_name,
            self.pane_id,
            self.generation,
            self.runtime_revision,
        )


@dataclass(frozen=True)
class PairEvidence:
    workspace_id: str
    lane_id: str
    tab_id: str = field(repr=False)
    slots: tuple[LiveSlot, LiveSlot] = field(repr=False)
    split: SplitInfo = field(repr=False)
    rects: tuple[tuple[str, PaneRect], tuple[str, PaneRect]] = field(
        repr=False
    )
    current_order: tuple[str, str]
    managed_scope: ManagedColumnScope = field(repr=False)

    @property
    def by_provider(self) -> Mapping[str, LiveSlot]:
        return {slot.provider: slot for slot in self.slots}

    @property
    def fingerprint(self) -> tuple[object, ...]:
        rect = self.split.rect
        return (
            self.workspace_id,
            self.lane_id,
            self.tab_id,
            tuple(slot.fingerprint for slot in self.slots),
            self.split.direction,
            self.split.ratio,
            (rect.x, rect.y, rect.width, rect.height),
            tuple(
                (provider, pane.x, pane.y, pane.width, pane.height)
                for provider, pane in self.rects
            ),
            self.current_order,
            self.managed_scope.fingerprint,
        )

    @property
    def authority_fingerprint(self) -> tuple[object, ...]:
        return (
            self.workspace_id,
            self.lane_id,
            self.tab_id,
            tuple(slot.fingerprint for slot in self.slots),
        )

    @property
    def rect_by_provider(self) -> Mapping[str, PaneRect]:
        return dict(self.rects)


@dataclass(frozen=True)
class PlacementPlan:
    status: str
    reason: str
    detail: str
    workspace_id: str
    lane_id: str
    target: Optional[PlacementTarget] = None
    current_split: str = ""
    current_order: tuple[str, ...] = ()
    current_ratio: Optional[float] = None
    operations: tuple[str, ...] = ()
    evidence: Optional[PairEvidence] = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.status in {PLAN_READY, PLAN_MATCHED}

    @property
    def can_apply(self) -> bool:
        return self.status == PLAN_READY

    def as_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": safe_text(self.status),
            "reason": safe_text(self.reason),
            "detail": safe_text(self.detail),
            "unit": {
                "workspace_id": safe_text(self.workspace_id),
                "lane_id": safe_text(self.lane_id),
            },
            "current": {
                "split": safe_text(self.current_split) if self.current_split else None,
                "order": [safe_text(provider) for provider in self.current_order],
                "ratio": self.current_ratio,
            },
            "target": self.target.as_payload() if self.target else None,
            "operations": list(self.operations),
        }


@dataclass(frozen=True)
class PlacementApplyResult:
    status: str
    reason: str
    detail: str
    before: PlacementPlan
    after: PlacementPlan
    recovery: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {APPLY_APPLIED, APPLY_MATCHED}

    def as_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": safe_text(self.status),
            "reason": safe_text(self.reason),
            "detail": safe_text(self.detail),
            "recovery": safe_text(self.recovery) if self.recovery else None,
            "retryable": self.status == APPLY_FAILED,
            "before": self.before.as_payload(),
            "after": self.after.as_payload(),
        }


def refused_plan(
    workspace_id: str, lane_id: str, reason: str, detail: str
) -> PlacementPlan:
    return PlacementPlan(PLAN_REFUSED, reason, detail, workspace_id, lane_id)


RatioVerdict = Callable[[SplitInfo, PaneRect, float], tuple[bool, str]]


def decide_plan(
    *,
    workspace_id: str,
    lane_id: str,
    target: PlacementTarget,
    evidence: PairEvidence,
    ratio_evaluator: RatioVerdict = ratio_verdict,
) -> PlacementPlan:
    """Derive the deterministic operations needed for one admitted live pair."""

    operations: list[str] = []
    if evidence.split.direction != target.split:
        operations.append("change_split")
    elif evidence.current_order != target.order:
        operations.append("swap_order")
    first_rect = evidence.rect_by_provider[evidence.current_order[0]]
    matches_ratio, _ = ratio_evaluator(evidence.split, first_rect, target.ratio)
    if not matches_ratio:
        operations.append("resize_ratio")
    status = PLAN_READY if operations else PLAN_MATCHED
    return PlacementPlan(
        status=status,
        reason=REASON_OK,
        detail=(
            "placement changes are ready"
            if operations
            else "live placement already matches"
        ),
        workspace_id=workspace_id,
        lane_id=lane_id,
        target=target,
        current_split=evidence.split.direction,
        current_order=evidence.current_order,
        current_ratio=evidence.split.ratio,
        operations=tuple(operations),
        evidence=evidence,
    )


__all__ = (
    "APPLY_APPLIED",
    "APPLY_FAILED",
    "APPLY_MATCHED",
    "APPLY_PARTIAL",
    "APPLY_REFUSED",
    "LiveSlot",
    "PairEvidence",
    "PlacementApplyResult",
    "PlacementPlan",
    "PlacementTarget",
    "decide_plan",
)
