"""Value model for generation-fenced shared coordinator column placement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (  # noqa: E501
    safe_text,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    LayoutSnapshot,
    PaneRect,
    SplitInfo,
    find_pair_split,
    governing_split,
    ratio_verdict,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_plan import (  # noqa: E501
    ProjectColumnPlan,
    UnitColumnKey,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_LOCATOR,
    AGENT_KEY_LOCATOR_ALIAS,
    AGENT_KEY_LOCATOR_ALIAS_2,
)


PREVIEW_READY = "ready"
PREVIEW_MATCHED = "matched"
PREVIEW_DEFERRED = "deferred"
PREVIEW_REFUSED = "refused"

PLACEMENT_APPLIED = "applied"
PLACEMENT_MATCHED = "matched"
PLACEMENT_DEFERRED = "deferred"
PLACEMENT_REFUSED = "refused"
PLACEMENT_PARTIAL = "partial_failure"

REASON_OK = "ok"
REASON_INVENTORY_UNAVAILABLE = "inventory_unavailable"
REASON_AUTHORITY_UNVERIFIED = "authority_unverified"
REASON_GENERATION_UNVERIFIED = "generation_unverified"
REASON_FULL_PAIR_REQUIRED = "full_pair_required"
REASON_LAYOUT_UNAVAILABLE = "layout_unavailable"
REASON_GEOMETRY_UNSUPPORTED = "geometry_unsupported"
REASON_CONFIG_UNRESOLVED = "config_unresolved"
REASON_STALE_PREVIEW = "stale_preview"
REASON_COMMAND_UNPROVEN = "command_unproven"
REASON_POSTCONDITION_FAILED = "postcondition_failed"


def row_by_locator(
    rows: Sequence[Mapping[str, object]], pane_id: str
) -> Optional[Mapping[str, object]]:
    matched = []
    for row in rows:
        stated = {
            value
            for key in (
                AGENT_KEY_LOCATOR,
                AGENT_KEY_LOCATOR_ALIAS,
                AGENT_KEY_LOCATOR_ALIAS_2,
            )
            if isinstance((value := row.get(key)), str) and value
        }
        if pane_id in stated:
            matched.append(row)
    return matched[0] if len(matched) == 1 else None


def runtime_revision(row: Mapping[str, object]) -> Optional[str]:
    raw = row.get("runtime_revision")
    if raw is None:
        return ""
    if not isinstance(raw, str) or raw != raw.strip():
        return None
    return raw


def tab_bounds(layout: LayoutSnapshot) -> Optional[tuple[int, int, int, int]]:
    panes = tuple(layout.panes.values())
    if not panes:
        return None
    return (
        min(pane.x for pane in panes),
        min(pane.y for pane in panes),
        max(pane.x + pane.width for pane in panes),
        max(pane.y + pane.height for pane in panes),
    )


def refused_preview(reason: str, detail: str) -> "ProjectColumnPlacementPreview":
    return ProjectColumnPlacementPreview(PREVIEW_REFUSED, reason, detail)


def deferred_preview(
    reason: str,
    detail: str,
    *,
    current_order: Sequence[UnitColumnKey] = (),
) -> "ProjectColumnPlacementPreview":
    return ProjectColumnPlacementPreview(
        PREVIEW_DEFERRED,
        reason,
        detail,
        current_order=tuple(current_order),
    )


def internal_pair_matches(
    layout: LayoutSnapshot,
    *,
    top: str,
    lower: str,
    target_ratio: float,
) -> bool:
    top_rect = layout.panes.get(top)
    lower_rect = layout.panes.get(lower)
    if top_rect is None or lower_rect is None:
        return False
    split = find_pair_split(layout, top_rect, lower_rect, "down")
    if split is None or governing_split(layout, top_rect, "down") != split:
        return False
    matched, _detail = ratio_verdict(split, top_rect, target_ratio)
    return matched


def singleton_temporary_tab(
    layout: Optional[LayoutSnapshot], *, pane_id: str, opening_tab: str
) -> str:
    """Return a measured temporary tab id, never one inferred from a response."""

    if (
        layout is None
        or not layout.tab_id
        or layout.tab_id == opening_tab
        or set(layout.panes) != {pane_id}
        or layout.splits
    ):
        return ""
    return layout.tab_id


def outer_ratio(
    evidence: "ProjectColumnPlacementEvidence",
    key: UnitColumnKey,
    target: float,
) -> tuple[bool, Optional[SplitInfo]]:
    column = evidence.by_key.get(key)
    bounds = tab_bounds(evidence.layout)
    if column is None or bounds is None:
        return False, None
    rect = evidence.layout.panes.get(column.top.pane_id)
    if rect is None:
        return False, None
    x0, y0, x1, y1 = bounds
    split = governing_split(evidence.layout, rect, "right")
    expected = PaneRect(rect.x, y0, x1 - rect.x, y1 - y0)
    if split is None or split.rect != expected or rect.x < x0:
        return False, None
    matched, _detail = ratio_verdict(split, rect, target)
    return matched, split


@dataclass(frozen=True)
class ColumnSlot:
    """One admitted live slot. Runtime handles stay private to the result."""

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
class LiveUnitColumn:
    """One full two-pane Unit column, top-to-bottom."""

    key: UnitColumnKey
    top: ColumnSlot = field(repr=False)
    lower: ColumnSlot = field(repr=False)
    internal_ratio: float = field(repr=False)
    x: int = field(repr=False)

    @property
    def slots(self) -> tuple[ColumnSlot, ColumnSlot]:
        return (self.top, self.lower)


@dataclass(frozen=True)
class ProjectColumnPlacementEvidence:
    """Private proof bundle frozen by preview."""

    target_workspace: str = field(repr=False)
    tab_id: str = field(repr=False)
    columns: tuple[LiveUnitColumn, ...] = field(repr=False)
    layout: LayoutSnapshot = field(repr=False)
    plan: ProjectColumnPlan = field(repr=False)

    @property
    def current_order(self) -> tuple[UnitColumnKey, ...]:
        return tuple(column.key for column in self.columns)

    @property
    def authority_fingerprint(self) -> tuple[object, ...]:
        return tuple(
            sorted(
                (
                    (
                        column.key.workspace_id,
                        column.key.lane_id,
                        column.key.host_id,
                        tuple(
                            sorted(
                                (slot.fingerprint for slot in column.slots),
                                key=lambda slot: slot[0],
                            )
                        ),
                    )
                    for column in self.columns
                ),
                key=lambda item: (item[0], item[1], item[2]),
            )
        )

    @property
    def layout_fingerprint(self) -> tuple[object, ...]:
        return _layout_fingerprint(self.layout)

    @property
    def source_fingerprint(self) -> str:
        return self.plan.source_fingerprint or ""

    @property
    def target_fingerprint(self) -> tuple[object, ...]:
        return (
            self.plan.desired_order,
            tuple((target.left_unit, target.ratio) for target in self.plan.ratio_targets),
            self.source_fingerprint,
        )

    @property
    def by_key(self) -> Mapping[UnitColumnKey, LiveUnitColumn]:
        return {column.key: column for column in self.columns}


@dataclass(frozen=True)
class ProjectColumnPlacementPreview:
    status: str
    reason: str
    detail: str
    current_order: tuple[UnitColumnKey, ...] = ()
    desired_order: tuple[UnitColumnKey, ...] = ()
    operations: tuple[str, ...] = ()
    evidence: Optional[ProjectColumnPlacementEvidence] = field(
        default=None, repr=False, compare=False
    )

    @property
    def can_apply(self) -> bool:
        return self.status == PREVIEW_READY and self.evidence is not None

    def as_payload(self) -> dict[str, object]:
        def public_key(key: UnitColumnKey) -> dict[str, str]:
            return {
                "workspace_id": safe_text(key.workspace_id),
                "lane_id": safe_text(key.lane_id),
                "host_id": safe_text(key.host_id),
            }

        return {
            "status": safe_text(self.status),
            "reason": safe_text(self.reason),
            "detail": safe_text(self.detail),
            "current_order": [public_key(key) for key in self.current_order],
            "desired_order": [public_key(key) for key in self.desired_order],
            "operations": [safe_text(operation) for operation in self.operations],
        }


@dataclass(frozen=True)
class ProjectColumnPlacementResult:
    status: str
    reason: str
    detail: str
    before: ProjectColumnPlacementPreview
    after: Optional[ProjectColumnPlacementPreview] = None
    recovery: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {
            PLACEMENT_APPLIED,
            PLACEMENT_MATCHED,
            PLACEMENT_DEFERRED,
        }

    def as_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": safe_text(self.status),
            "reason": safe_text(self.reason),
            "detail": safe_text(self.detail),
            "before": self.before.as_payload(),
            "after": self.after.as_payload() if self.after is not None else None,
            "recovery": safe_text(self.recovery) if self.recovery else None,
        }


def _layout_fingerprint(layout: LayoutSnapshot) -> tuple[object, ...]:
    return (
        layout.tab_id,
        tuple(
            sorted(
                (pane, rect.x, rect.y, rect.width, rect.height)
                for pane, rect in layout.panes.items()
            )
        ),
        tuple(
            sorted(
                (
                    split.split_id,
                    split.direction,
                    split.ratio,
                    split.rect.x,
                    split.rect.y,
                    split.rect.width,
                    split.rect.height,
                )
                for split in layout.splits
            )
        ),
    )


__all__ = (
    "ColumnSlot",
    "LiveUnitColumn",
    "PLACEMENT_APPLIED",
    "PLACEMENT_DEFERRED",
    "PLACEMENT_MATCHED",
    "PLACEMENT_PARTIAL",
    "PLACEMENT_REFUSED",
    "PREVIEW_DEFERRED",
    "PREVIEW_MATCHED",
    "PREVIEW_READY",
    "PREVIEW_REFUSED",
    "ProjectColumnPlacementEvidence",
    "ProjectColumnPlacementPreview",
    "ProjectColumnPlacementResult",
    "REASON_AUTHORITY_UNVERIFIED",
    "REASON_COMMAND_UNPROVEN",
    "REASON_CONFIG_UNRESOLVED",
    "REASON_FULL_PAIR_REQUIRED",
    "REASON_GENERATION_UNVERIFIED",
    "REASON_GEOMETRY_UNSUPPORTED",
    "REASON_INVENTORY_UNAVAILABLE",
    "REASON_LAYOUT_UNAVAILABLE",
    "REASON_OK",
    "REASON_POSTCONDITION_FAILED",
    "REASON_STALE_PREVIEW",
)
