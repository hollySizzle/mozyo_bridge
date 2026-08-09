"""Live-only one-step adjustments for verified Herdr Unit columns.

This mixin keeps the interaction-specific plan derivation separate from the
larger shared-column actuator.  The host class remains responsible for every
authority, generation, tab, geometry, command-effect, and postcondition check.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_plan import (  # noqa: E501
    ObservedUnitColumn,
    ProjectColumnPlan,
    UnitColumnPreference,
    UnitColumnKey,
    plan_project_columns,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_placement_model import (  # noqa: E501
    PLACEMENT_APPLIED,
    PLACEMENT_REFUSED,
    PREVIEW_MATCHED,
    PREVIEW_READY,
    ProjectColumnPlacementEvidence,
    ProjectColumnPlacementPreview,
    ProjectColumnPlacementResult,
    REASON_ADJUSTMENT_INVALID,
    REASON_ADJUSTMENT_UNREPRESENTABLE,
    REASON_EDGE_REACHED,
    REASON_STALE_PREVIEW,
    refused_preview,
)


COLUMN_MOVE_LEFT = "move_unit_left"
COLUMN_MOVE_RIGHT = "move_unit_right"
COLUMN_WIDTH_DECREASE = "decrease_unit_width"
COLUMN_WIDTH_INCREASE = "increase_unit_width"
COLUMN_ADJUSTMENTS = frozenset(
    {
        COLUMN_MOVE_LEFT,
        COLUMN_MOVE_RIGHT,
        COLUMN_WIDTH_DECREASE,
        COLUMN_WIDTH_INCREASE,
    }
)
WIDTH_ADJUSTMENT_FACTOR = 1.25


class ProjectColumnAdjustmentMixin:
    """Add preview-first Unit-column moves to a verified placement service."""

    def _service_with_plan_resolver(self, plan_resolver):
        return type(self)(
            home=self.home,
            target_workspace=self.target_workspace,
            top_workspace_id=self.top_workspace_id,
            binary=self.binary,
            runner=self.runner,
            timeout=self.timeout,
            env=self.env,
            authority=self.authority,
            own_slots=self.own_slots,
            expected_own_key=self.expected_own_key,
            generation_resolver=self.generation_resolver,
            plan_resolver=plan_resolver,
        )

    def _fixed_plan_service(self, plan: ProjectColumnPlan):
        """Reuse the verified actuator with one immutable, preview-owned target."""

        return self._service_with_plan_resolver(
            lambda _observed, *, home=None: plan
        )

    @staticmethod
    def _live_baseline_plan(observed, *, home=None) -> ProjectColumnPlan:
        """Build an observation-only plan without consulting repo configuration."""

        del home
        ordered = tuple(sorted(observed, key=lambda item: item.current_index))
        encoded = json.dumps(
            {
                "domain": "herdr-live-unit-column-baseline-v1",
                "units": [
                    (
                        item.key.host_id,
                        item.key.workspace_id,
                        item.key.lane_id,
                        item.current_index,
                    )
                    for item in ordered
                ],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return plan_project_columns(
            tuple(
                UnitColumnPreference(
                    observed=item,
                    position=item.current_index,
                    relative_width=1.0,
                )
                for item in ordered
            ),
            source_fingerprint=hashlib.sha256(encoded).hexdigest(),
        )

    @staticmethod
    def _adjustment_fingerprint(
        opening: ProjectColumnPlacementEvidence,
        target: UnitColumnKey,
        adjustment: str,
    ) -> str:
        encoded = json.dumps(
            {
                "version": 1,
                "base_source": opening.source_fingerprint,
                "authority": opening.authority_fingerprint,
                "layout": opening.layout_fingerprint,
                "target": (target.host_id, target.workspace_id, target.lane_id),
                "adjustment": adjustment,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _adjusted_plan(
        self,
        opening: ProjectColumnPlacementEvidence,
        target: UnitColumnKey,
        adjustment: str,
    ) -> ProjectColumnPlan | None:
        current_order = list(opening.current_order)
        if target not in current_order:
            return None
        desired_order = list(current_order)
        target_index = desired_order.index(target)
        if adjustment == COLUMN_MOVE_LEFT:
            if target_index == 0:
                return None
            desired_order[target_index - 1], desired_order[target_index] = (
                desired_order[target_index],
                desired_order[target_index - 1],
            )
        elif adjustment == COLUMN_MOVE_RIGHT:
            if target_index == len(desired_order) - 1:
                return None
            desired_order[target_index], desired_order[target_index + 1] = (
                desired_order[target_index + 1],
                desired_order[target_index],
            )

        widths: dict[UnitColumnKey, float] = {}
        for column in opening.columns:
            rect = opening.layout.panes.get(column.top.pane_id)
            if rect is None or rect.width <= 0:
                return None
            widths[column.key] = float(rect.width)
        if adjustment == COLUMN_WIDTH_INCREASE:
            widths[target] *= WIDTH_ADJUSTMENT_FACTOR
        elif adjustment == COLUMN_WIDTH_DECREASE:
            widths[target] /= WIDTH_ADJUSTMENT_FACTOR

        desired_indexes = {key: index for index, key in enumerate(desired_order)}
        preferences = tuple(
            UnitColumnPreference(
                observed=ObservedUnitColumn(key, index),
                position=desired_indexes[key],
                relative_width=widths[key],
            )
            for index, key in enumerate(current_order)
        )
        return plan_project_columns(
            preferences,
            source_fingerprint=self._adjustment_fingerprint(
                opening, target, adjustment
            ),
        )

    @staticmethod
    def _selected_projection(
        opening: ProjectColumnPlacementEvidence,
        target: UnitColumnKey,
        adjustment: str,
        plan: ProjectColumnPlan,
    ) -> dict[str, int | float] | None:
        widths: dict[UnitColumnKey, float] = {}
        for column in opening.columns:
            rect = opening.layout.panes.get(column.top.pane_id)
            if rect is None or rect.width <= 0:
                return None
            widths[column.key] = float(rect.width)
        target_widths = dict(widths)
        if adjustment == COLUMN_WIDTH_INCREASE:
            target_widths[target] *= WIDTH_ADJUSTMENT_FACTOR
        elif adjustment == COLUMN_WIDTH_DECREASE:
            target_widths[target] /= WIDTH_ADJUSTMENT_FACTOR
        current_total = sum(widths.values())
        target_total = sum(target_widths.values())
        if current_total <= 0 or target_total <= 0:
            return None
        return {
            "selected_current_position": opening.current_order.index(target) + 1,
            "selected_target_position": plan.desired_order.index(target) + 1,
            "selected_current_width_share": widths[target] / current_total,
            "selected_target_width_share": target_widths[target] / target_total,
        }

    def preview_adjustment(
        self, target: UnitColumnKey, adjustment: str
    ) -> ProjectColumnPlacementPreview:
        """Preview one live-only Unit-column move or width step."""

        if adjustment not in COLUMN_ADJUSTMENTS or not isinstance(
            target, UnitColumnKey
        ):
            return refused_preview(
                REASON_ADJUSTMENT_INVALID,
                "the requested Unit-column adjustment is not supported",
            )
        baseline = self._service_with_plan_resolver(
            self._live_baseline_plan
        ).preview()
        opening = baseline.evidence
        if opening is None:
            return baseline
        if target not in opening.by_key:
            return refused_preview(
                REASON_ADJUSTMENT_INVALID,
                "the selected Unit is not present in the verified shared tab",
            )
        current_index = opening.current_order.index(target)
        at_edge = (
            adjustment == COLUMN_MOVE_LEFT and current_index == 0
        ) or (
            adjustment == COLUMN_MOVE_RIGHT
            and current_index == len(opening.current_order) - 1
        )
        if at_edge:
            preview = ProjectColumnPlacementPreview(
                PREVIEW_MATCHED,
                REASON_EDGE_REACHED,
                "the selected Unit is already at the requested edge",
                opening.current_order,
                opening.current_order,
                (),
                opening,
            )
            projection = self._selected_projection(
                opening, target, adjustment, opening.plan
            )
            return replace(preview, **(projection or {}))
        plan = self._adjusted_plan(opening, target, adjustment)
        if plan is None or not plan.executable:
            return refused_preview(
                REASON_ADJUSTMENT_UNREPRESENTABLE,
                "the requested Unit-column adjustment is not representable by Herdr",
            )
        preview = self._fixed_plan_service(plan).preview()
        current = preview.evidence
        if current is not None and (
            current.target_workspace != opening.target_workspace
            or current.tab_id != opening.tab_id
            or current.authority_fingerprint != opening.authority_fingerprint
            or current.layout_fingerprint != opening.layout_fingerprint
            or current.current_order != opening.current_order
        ):
            return refused_preview(
                REASON_STALE_PREVIEW,
                "live Unit authority or layout changed while previewing the adjustment",
            )
        detail = (
            "selected Unit-column adjustment is ready to apply"
            if preview.status == PREVIEW_READY
            else "the selected Unit-column adjustment has no measurable effect"
            if preview.status == PREVIEW_MATCHED
            else preview.detail
        )
        projection = self._selected_projection(
            opening, target, adjustment, plan
        )
        return replace(
            preview,
            detail=detail,
            operations=(adjustment,) + tuple(preview.operations),
            **(projection or {}),
        )

    def apply_adjustment(
        self, preview: ProjectColumnPlacementPreview
    ) -> ProjectColumnPlacementResult:
        """Apply only a fresh preview produced by :meth:`preview_adjustment`."""

        adjustment_tokens = set(preview.operations) & COLUMN_ADJUSTMENTS
        if (
            len(adjustment_tokens) != 1
            or not preview.can_apply
            or preview.evidence is None
        ):
            return ProjectColumnPlacementResult(
                PLACEMENT_REFUSED,
                REASON_ADJUSTMENT_INVALID,
                "a fresh applicable Unit-column adjustment preview is required",
                preview,
                preview,
                "Preview the selected Unit-column adjustment again.",
            )
        result = self._fixed_plan_service(preview.evidence.plan).apply(preview)
        if result.status == PLACEMENT_APPLIED:
            return replace(
                result,
                detail=(
                    "live Unit-column adjustment was measured after apply; "
                    "no saved configuration was changed"
                ),
            )
        return result


__all__ = (
    "COLUMN_MOVE_LEFT",
    "COLUMN_MOVE_RIGHT",
    "COLUMN_WIDTH_DECREASE",
    "COLUMN_WIDTH_INCREASE",
    "ProjectColumnAdjustmentMixin",
)
