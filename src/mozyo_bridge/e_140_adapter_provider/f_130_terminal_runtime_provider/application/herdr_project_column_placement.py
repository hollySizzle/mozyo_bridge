"""Generation-fenced configured placement for shared coordinator columns.
Freeze config/authority/generation/layout/target; move only healthy full pairs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.herdr_launch_generation import verified_generation_token
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_command_effect import (  # noqa: E501
    EFFECT_CHANGED,
    EFFECT_UNCHANGED,
    EFFECT_UNKNOWN,
    parse_changed_effect,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    MAX_RESIZE_PASSES,
    find_pair_split,
    governing_split,
    ratio_verdict,
    resize_step,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _invoke,
    _list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (  # noqa: E501
    CoordinatorPane,
    OwnSlot,
    ProjectColumnAuthority,
    project_column_authority,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_balance import (  # noqa: E501
    columnar_verdict,
    read_pane_layout,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_adjustment import (  # noqa: E501
    COLUMN_MOVE_LEFT,
    COLUMN_MOVE_RIGHT,
    COLUMN_WIDTH_DECREASE,
    COLUMN_WIDTH_INCREASE,
    ProjectColumnAdjustmentMixin,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_plan import (  # noqa: E501
    ObservedUnitColumn,
    ProjectColumnPlan,
    UnitColumnKey,
    resolve_project_column_plan,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_placement_model import (  # noqa: E501
    ColumnSlot,
    LiveUnitColumn,
    PLACEMENT_APPLIED,
    PLACEMENT_DEFERRED,
    PLACEMENT_MATCHED,
    PLACEMENT_PARTIAL,
    PLACEMENT_REFUSED,
    PREVIEW_DEFERRED,
    PREVIEW_MATCHED,
    PREVIEW_READY,
    PREVIEW_REFUSED,
    ProjectColumnPlacementEvidence,
    ProjectColumnPlacementPreview,
    ProjectColumnPlacementResult,
    REASON_AUTHORITY_UNVERIFIED,
    REASON_ADJUSTMENT_INVALID,
    REASON_ADJUSTMENT_UNREPRESENTABLE,
    REASON_COMMAND_UNPROVEN,
    REASON_CONFIG_UNRESOLVED,
    REASON_EDGE_REACHED,
    REASON_FULL_PAIR_REQUIRED,
    REASON_GENERATION_UNVERIFIED,
    REASON_GEOMETRY_UNSUPPORTED,
    REASON_INVENTORY_UNAVAILABLE,
    REASON_LAYOUT_UNAVAILABLE,
    REASON_OK,
    REASON_POSTCONDITION_FAILED,
    REASON_STALE_PREVIEW,
    column_resize_actuator_key,
    deferred_preview,
    internal_pair_matches,
    outer_ratio,
    placement_failure_result,
    refused_preview,
    row_by_locator,
    runtime_revision,
    singleton_temporary_tab,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_reflow import (  # noqa: E501
    _move_result,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DEFAULT_LANE,
    _norm,
    _norm_lane,
)


GenerationResolver = Callable[[CoordinatorPane], str]
PlanResolver = Callable[..., ProjectColumnPlan]

class HerdrProjectColumnPlacement(ProjectColumnAdjustmentMixin):
    """Preview and apply configured shared coordinator column placement."""

    def __init__(
        self,
        *,
        home: Path,
        target_workspace: str,
        top_workspace_id: str,
        binary: str,
        runner,
        timeout: float,
        env=None,
        authority: Optional[ProjectColumnAuthority] = None,
        own_slots: Sequence[OwnSlot] = (),
        expected_own_key: Optional[tuple[str, str]] = None,
        generation_resolver: Optional[GenerationResolver] = None,
        plan_resolver: PlanResolver = resolve_project_column_plan,
    ) -> None:
        self.home = Path(home)
        self.target_workspace = _norm(target_workspace)
        self.top_workspace_id = _norm(top_workspace_id)
        self.binary = binary
        self.runner = runner
        self.timeout = timeout
        self.env = env
        self.authority = authority or project_column_authority(self.home)
        self.own_slots = tuple(own_slots)
        self.expected_own_key = expected_own_key
        self.plan_resolver = plan_resolver
        self.generation_resolver = generation_resolver or self._generation_token

    def _generation_token(self, pane: CoordinatorPane) -> str:
        return verified_generation_token(
            self.home,
            assigned_name=pane.assigned_name,
            workspace_id=pane.workspace_id,
            role=pane.role,
            lane_id=pane.lane_id,
            locator=pane.locator,
            norm=_norm,
            norm_lane=_norm_lane,
        )

    def _read_rows(self) -> Optional[tuple[Mapping[str, object], ...]]:
        try:
            return tuple(_list_rows(self.binary, self.runner, self.timeout))
        except Exception:  # noqa: BLE001 - inventory is an external read boundary
            return None

    def _read_layout(self, pane_id: str):
        return read_pane_layout(
            pane_id,
            binary=self.binary,
            runner=self.runner,
            timeout=self.timeout,
            env=self.env,
        )

    def _observe_authority(
        self,
    ) -> tuple[
        Optional[tuple[Mapping[str, object], ...]],
        Optional[Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]],
        Optional[tuple[object, ...]],
        str,
    ]:
        rows = self._read_rows()
        if rows is None:
            return None, None, None, REASON_INVENTORY_UNAVAILABLE
        try:
            decision = self.authority.resolve(
                rows,
                target_workspace=self.target_workspace,
                own_slots=self.own_slots,
                expected_own_key=self.expected_own_key,
                top_workspace_id=self.top_workspace_id,
            )
        except Exception:  # noqa: BLE001 - durable authority read fails closed
            return rows, None, None, REASON_AUTHORITY_UNVERIFIED
        if not decision.ok or not decision.groups:
            return rows, None, None, REASON_AUTHORITY_UNVERIFIED

        fingerprint = []
        for key, members in sorted(decision.groups.items()):
            if len(members) != 2:
                return rows, decision.groups, None, REASON_FULL_PAIR_REQUIRED
            slots = []
            for pane in sorted(members, key=lambda member: member.role):
                row = row_by_locator(rows, pane.locator)
                revision = runtime_revision(row) if row is not None else None
                if row is None or revision is None:
                    return rows, None, None, REASON_AUTHORITY_UNVERIFIED
                try:
                    generation = self.generation_resolver(pane)
                except Exception:  # noqa: BLE001 - injected/read boundary
                    generation = ""
                if (
                    not isinstance(generation, str)
                    or not generation
                    or generation != generation.strip()
                ):
                    return rows, None, None, REASON_GENERATION_UNVERIFIED
                slots.append(
                    (
                        pane.role,
                        pane.assigned_name,
                        pane.locator,
                        generation,
                        revision,
                    )
                )
            fingerprint.append((key[0], key[1], "local", tuple(slots)))
        return rows, decision.groups, tuple(fingerprint), REASON_OK

    def _observe(self) -> ProjectColumnPlacementPreview:
        if not self.target_workspace:
            return refused_preview(
                REASON_AUTHORITY_UNVERIFIED,
                "the shared coordinator workspace is unresolved",
            )
        rows, groups, authority_fingerprint, authority_reason = self._observe_authority()
        if authority_reason == REASON_FULL_PAIR_REQUIRED:
            return deferred_preview(
                REASON_FULL_PAIR_REQUIRED,
                "configured placement waits until every Unit has both coordinator panes",
            )
        if groups is None or rows is None or authority_fingerprint is None:
            return refused_preview(
                authority_reason,
                "the complete live coordinator authority could not be verified",
            )

        pane_ids = {
            pane.locator for members in groups.values() for pane in members
        }
        first_pane = next(iter(pane_ids), "")
        layout = self._read_layout(first_pane)
        if (
            layout is None
            or not layout.tab_id
            or set(layout.panes) != pane_ids
        ):
            return refused_preview(
                REASON_LAYOUT_UNAVAILABLE,
                "the coordinator panes do not resolve to one complete readable tab",
            )
        columnar, _reason = columnar_verdict(layout, groups)
        if not columnar:
            return refused_preview(
                REASON_GEOMETRY_UNSUPPORTED,
                "the coordinator panes are not a complete left-to-right column layout",
            )

        generation_by_pane: dict[str, tuple[str, str]] = {}
        for _workspace, _lane, _host, slots in authority_fingerprint:
            for _provider, _name, pane, generation, revision in slots:
                generation_by_pane[pane] = (generation, revision)

        columns = []
        for (workspace_id, lane_id), members in groups.items():
            ordered = sorted(
                members,
                key=lambda member: (
                    layout.panes[member.locator].y,
                    member.locator,
                ),
            )
            top_pane, lower_pane = ordered
            top_rect = layout.panes[top_pane.locator]
            lower_rect = layout.panes[lower_pane.locator]
            internal = find_pair_split(layout, top_rect, lower_rect, "down")
            governing = governing_split(layout, top_rect, "down")
            if internal is None or governing != internal:
                return refused_preview(
                    REASON_GEOMETRY_UNSUPPORTED,
                    "a Unit does not have one unambiguous vertical internal divider",
                )

            def slot_of(pane: CoordinatorPane) -> ColumnSlot:
                generation, revision = generation_by_pane[pane.locator]
                return ColumnSlot(
                    pane.role,
                    pane.assigned_name,
                    pane.locator,
                    generation,
                    revision,
                )

            columns.append(
                LiveUnitColumn(
                    UnitColumnKey(workspace_id, lane_id or DEFAULT_LANE),
                    slot_of(top_pane),
                    slot_of(lower_pane),
                    internal.ratio,
                    top_rect.x,
                )
            )
        columns.sort(key=lambda column: (column.x, column.key))
        if len({column.x for column in columns}) != len(columns):
            return refused_preview(
                REASON_GEOMETRY_UNSUPPORTED,
                "two Unit columns report the same horizontal position",
            )

        observed = tuple(
            ObservedUnitColumn(column.key, index)
            for index, column in enumerate(columns)
        )
        try:
            plan = self.plan_resolver(observed, home=self.home)
        except Exception:  # noqa: BLE001 - config/registry boundary
            return refused_preview(
                REASON_CONFIG_UNRESOLVED,
                "the configured Unit order and relative widths could not be resolved",
            )
        live_keys = {column.key for column in columns}
        if (
            not isinstance(plan, ProjectColumnPlan)
            or not plan.executable
            or len(plan.desired_order) != len(columns)
            or len(set(plan.desired_order)) != len(plan.desired_order)
            or set(plan.desired_order) != live_keys
            or tuple(target.left_unit for target in plan.ratio_targets)
            != plan.desired_order[:-1]
        ):
            return refused_preview(
                REASON_CONFIG_UNRESOLVED,
                "the configured Unit placement is incomplete or not executable",
            )

        evidence = ProjectColumnPlacementEvidence(
            self.target_workspace,
            layout.tab_id,
            tuple(columns),
            layout,
            plan,
        )
        ratio_by_key = {target.left_unit: target.ratio for target in plan.ratio_targets}
        ratios_match = all(
            outer_ratio(evidence, key, ratio)[0]
            for key, ratio in ratio_by_key.items()
        )
        order_matches = evidence.current_order == plan.desired_order
        if order_matches and ratios_match:
            return ProjectColumnPlacementPreview(
                PREVIEW_MATCHED,
                REASON_OK,
                "live Unit order and relative widths already match the configuration",
                evidence.current_order,
                plan.desired_order,
                (),
                evidence,
            )
        operations = []
        if not order_matches:
            operations.append("reorder_unit_columns")
        if not ratios_match:
            operations.append("resize_unit_columns")
        return ProjectColumnPlacementPreview(
            PREVIEW_READY,
            REASON_OK,
            "configured Unit placement is ready to apply",
            evidence.current_order,
            plan.desired_order,
            tuple(operations),
            evidence,
        )

    def preview(self) -> ProjectColumnPlacementPreview:
        return self._observe()

    def _move(
        self,
        tail: Sequence[str],
        *,
        expected_pane: str,
        expected_tab: str = "",
    ) -> tuple[str, str]:
        try:
            completed = _invoke(
                self.binary,
                tail,
                self.runner,
                self.timeout,
                env=self.env,
            )
        except HerdrSessionStartError:
            return EFFECT_UNKNOWN, ""
        landed = _move_result(completed.stdout)
        if landed is not None:
            if landed[0] != expected_pane:
                return EFFECT_UNKNOWN, landed[1]
            if expected_tab and landed[1] != expected_tab:
                return EFFECT_UNKNOWN, landed[1]
            return EFFECT_CHANGED, landed[1]
        try:
            changed = json.loads(completed.stdout)["result"]["move_result"]["changed"]
        except (KeyError, TypeError, ValueError):
            return EFFECT_UNKNOWN, ""
        return (EFFECT_UNCHANGED, "") if changed is False else (EFFECT_UNKNOWN, "")

    def _swap(self, first: str, second: str) -> str:
        try:
            completed = _invoke(
                self.binary,
                (
                    "pane",
                    "swap",
                    "--source-pane",
                    first,
                    "--target-pane",
                    second,
                ),
                self.runner,
                self.timeout,
                env=self.env,
            )
        except HerdrSessionStartError:
            return EFFECT_UNKNOWN
        return parse_changed_effect(
            completed.stdout,
            result_type="pane_swap",
            envelope="swap",
        )

    def _same_authority(
        self, opening: ProjectColumnPlacementEvidence
    ) -> bool:
        _rows, _groups, fingerprint, reason = self._observe_authority()
        return reason == REASON_OK and fingerprint == opening.authority_fingerprint

    def _same_source_target(
        self,
        opening: ProjectColumnPlacementEvidence,
        current_order: Sequence[UnitColumnKey],
    ) -> bool:
        observed = tuple(
            ObservedUnitColumn(key, index)
            for index, key in enumerate(current_order)
        )
        try:
            plan = self.plan_resolver(observed, home=self.home)
        except Exception:  # noqa: BLE001 - config/registry boundary
            return False
        return bool(
            isinstance(plan, ProjectColumnPlan)
            and plan.executable
            and plan.source_fingerprint == opening.source_fingerprint
            and plan.desired_order == opening.plan.desired_order
            and plan.ratio_targets == opening.plan.ratio_targets
        )

    def _phase_layouts(
        self,
        *,
        main_anchor: str,
        detached: Mapping[str, str],
        expected_main: set[str],
        tab_id: str,
        top_order: Sequence[str],
        attached: Optional[Mapping[str, tuple[str, float]]] = None,
    ) -> bool:
        main = self._read_layout(main_anchor)
        if main is None or main.tab_id != tab_id or set(main.panes) != expected_main:
            return False
        if (
            tuple(
                pane
                for _x, pane in sorted(
                    (main.panes[pane].x, pane) for pane in top_order
                )
            )
            != tuple(top_order)
        ):
            return False
        if any(
            not internal_pair_matches(
                main,
                top=top,
                lower=lower,
                target_ratio=ratio,
            )
            for lower, (top, ratio) in (attached or {}).items()
        ):
            return False
        for pane_id, temp_tab in detached.items():
            temporary = self._read_layout(pane_id)
            if (
                temporary is None
                or temporary.tab_id != temp_tab
                or set(temporary.panes) != {pane_id}
                or temporary.splits
            ):
                return False
        return True

    def _recover(
        self,
        opening: ProjectColumnPlacementEvidence,
        detached: Mapping[str, tuple[str, float, str]],
    ) -> int:
        """One best-effort return for each still-proven detached lower pane."""

        stranded = 0
        for lower, (top, ratio, temp_tab) in detached.items():
            if not self._same_authority(opening):
                stranded += 1
                continue
            layout = self._read_layout(lower)
            if layout is None:
                stranded += 1
                continue
            if layout.tab_id == opening.tab_id:
                if not internal_pair_matches(
                    layout,
                    top=top,
                    lower=lower,
                    target_ratio=ratio,
                ):
                    stranded += 1
                continue
            if layout.tab_id != temp_tab or set(layout.panes) != {lower} or layout.splits:
                stranded += 1
                continue
            effect, landed = self._move(
                (
                    "pane",
                    "move",
                    lower,
                    "--tab",
                    opening.tab_id,
                    "--split",
                    "down",
                    "--ratio",
                    f"{ratio:.9g}",
                    "--target-pane",
                    top,
                    "--no-focus",
                ),
                expected_pane=lower,
                expected_tab=opening.tab_id,
            )
            if effect != EFFECT_CHANGED or landed != opening.tab_id:
                closing = self._read_layout(lower)
                if (
                    closing is None
                    or closing.tab_id != opening.tab_id
                    or not internal_pair_matches(
                        closing,
                        top=top,
                        lower=lower,
                        target_ratio=ratio,
                    )
                ):
                    stranded += 1
        return stranded

    def _failure(
        self,
        before: ProjectColumnPlacementPreview,
        *,
        changed: bool,
        detail: str,
        reason: str,
        stranded: int = 0,
        recovery_attempted: bool = False,
    ) -> ProjectColumnPlacementResult:
        return placement_failure_result(
            before, self._observe(), changed=changed, detail=detail, reason=reason,
            stranded=stranded, recovery_attempted=recovery_attempted,
        )

    def _swap_adjacent(
        self,
        before: ProjectColumnPlacementPreview,
        opening: ProjectColumnPlacementEvidence,
        left: LiveUnitColumn,
        right: LiveUnitColumn,
    ) -> Optional[ProjectColumnPlacementResult]:
        detached: dict[str, tuple[str, float, str]] = {}
        attached: dict[str, tuple[str, float]] = {}
        expected_main = set(opening.layout.panes)
        main_anchor = left.top.pane_id
        top_order = [column.top.pane_id for column in opening.columns]
        key_order = [column.key for column in opening.columns]
        left_index = key_order.index(left.key)
        right_index = key_order.index(right.key)
        if right_index != left_index + 1:
            return self._failure(
                before,
                changed=False,
                reason=REASON_POSTCONDITION_FAILED,
                detail="the requested Unit columns are no longer adjacent",
            )
        for column in (left, right):
            effect, temp_tab = self._move(
                (
                    "pane",
                    "move",
                    column.lower.pane_id,
                    "--new-tab",
                    "--no-focus",
                ),
                expected_pane=column.lower.pane_id,
            )
            if effect != EFFECT_CHANGED or not temp_tab:
                measured_tab = singleton_temporary_tab(
                    self._read_layout(column.lower.pane_id),
                    pane_id=column.lower.pane_id,
                    opening_tab=opening.tab_id,
                )
                if measured_tab:
                    detached[column.lower.pane_id] = (
                        column.top.pane_id,
                        column.internal_ratio,
                        measured_tab,
                    )
                stranded = self._recover(opening, detached)
                if effect != EFFECT_UNCHANGED and not measured_tab:
                    stranded += 1
                return self._failure(
                    before,
                    changed=effect != EFFECT_UNCHANGED or bool(detached),
                    reason=REASON_COMMAND_UNPROVEN,
                    detail="Herdr did not prove a temporary lower-pane move",
                    stranded=stranded,
                    recovery_attempted=True,
                )
            detached[column.lower.pane_id] = (
                column.top.pane_id,
                column.internal_ratio,
                temp_tab,
            )
            expected_main.remove(column.lower.pane_id)
            if (
                not self._same_authority(opening)
                or not self._same_source_target(opening, key_order)
                or not self._phase_layouts(
                    main_anchor=main_anchor,
                    detached={pane: value[2] for pane, value in detached.items()},
                    expected_main=expected_main,
                    tab_id=opening.tab_id,
                    top_order=top_order,
                )
            ):
                stranded = self._recover(opening, detached)
                return self._failure(
                    before,
                    changed=True,
                    reason=REASON_POSTCONDITION_FAILED,
                    detail="live authority changed after a temporary lower-pane move",
                    stranded=stranded,
                    recovery_attempted=True,
                )
        top_order[left_index], top_order[right_index] = (
            top_order[right_index],
            top_order[left_index],
        )
        key_order[left_index], key_order[right_index] = (
            key_order[right_index],
            key_order[left_index],
        )
        effect = self._swap(left.top.pane_id, right.top.pane_id)
        if (
            effect != EFFECT_CHANGED
            or not self._same_authority(opening)
            or not self._same_source_target(opening, key_order)
            or not self._phase_layouts(
                main_anchor=main_anchor,
                detached={pane: value[2] for pane, value in detached.items()},
                expected_main=expected_main,
                tab_id=opening.tab_id,
                top_order=top_order,
            )
        ):
            stranded = self._recover(opening, detached)
            return self._failure(
                before,
                changed=True,
                reason=(
                    REASON_COMMAND_UNPROVEN
                    if effect != EFFECT_CHANGED
                    else REASON_POSTCONDITION_FAILED
                ),
                detail="Herdr did not prove the adjacent Unit-column swap",
                stranded=stranded,
                recovery_attempted=True,
            )

        for column in (left, right):
            effect, landed = self._move(
                (
                    "pane",
                    "move",
                    column.lower.pane_id,
                    "--tab",
                    opening.tab_id,
                    "--split",
                    "down",
                    "--ratio",
                    f"{column.internal_ratio:.9g}",
                    "--target-pane",
                    column.top.pane_id,
                    "--no-focus",
                ),
                expected_pane=column.lower.pane_id,
                expected_tab=opening.tab_id,
            )
            if effect != EFFECT_CHANGED or landed != opening.tab_id:
                stranded = self._recover(opening, detached)
                return self._failure(
                    before,
                    changed=True,
                    reason=REASON_COMMAND_UNPROVEN,
                    detail="Herdr did not prove a lower pane returned to its Unit",
                    stranded=stranded,
                    recovery_attempted=True,
                )
            detached.pop(column.lower.pane_id, None)
            attached[column.lower.pane_id] = (
                column.top.pane_id,
                column.internal_ratio,
            )
            expected_main.add(column.lower.pane_id)
            if (
                not self._same_authority(opening)
                or not self._same_source_target(opening, key_order)
                or not self._phase_layouts(
                    main_anchor=main_anchor,
                    detached={pane: value[2] for pane, value in detached.items()},
                    expected_main=expected_main,
                    tab_id=opening.tab_id,
                    top_order=top_order,
                    attached=attached,
                )
            ):
                stranded = self._recover(opening, detached)
                return self._failure(
                    before,
                    changed=True,
                    reason=REASON_POSTCONDITION_FAILED,
                    detail="live authority changed while restoring a Unit column",
                    stranded=stranded,
                    recovery_attempted=True,
                )
        return None

    def _resize_columns(
        self,
        before: ProjectColumnPlacementPreview,
        opening: ProjectColumnPlacementEvidence,
        *,
        changed_before: bool,
    ) -> Optional[ProjectColumnPlacementResult]:
        changed = changed_before
        for target in opening.plan.ratio_targets:
            previous_distance: Optional[float] = None
            for pass_index in range(MAX_RESIZE_PASSES + 1):
                current = self._observe()
                evidence = current.evidence
                if (
                    evidence is None
                    or evidence.authority_fingerprint != opening.authority_fingerprint
                    or evidence.source_fingerprint != opening.source_fingerprint
                    or evidence.target_fingerprint != opening.target_fingerprint
                    or evidence.current_order != opening.plan.desired_order
                ):
                    return self._failure(
                        before,
                        changed=changed,
                        reason=REASON_POSTCONDITION_FAILED,
                        detail="live authority or configured target changed before a width adjustment",
                    )
                matched, split = outer_ratio(
                    evidence,
                    target.left_unit,
                    target.ratio,
                )
                if matched:
                    break
                if split is None or pass_index >= MAX_RESIZE_PASSES:
                    return self._failure(
                        before,
                        changed=changed,
                        reason=REASON_POSTCONDITION_FAILED,
                        detail="a Unit divider could not reach its configured relative width",
                    )
                distance = abs(split.ratio - target.ratio)
                if previous_distance is not None and distance >= previous_distance:
                    return self._failure(
                        before,
                        changed=changed,
                        reason=REASON_POSTCONDITION_FAILED,
                        detail="Herdr stopped moving a Unit divider toward its configured width",
                    )
                direction, amount = resize_step(split.ratio, target.ratio, "right")
                actuator_key = column_resize_actuator_key(
                    evidence.current_order, target.left_unit, direction
                )
                if actuator_key is None:
                    return self._failure(
                        before,
                        changed=changed,
                        reason=REASON_POSTCONDITION_FAILED,
                        detail="a Unit divider has no right-side resize actuator",
                    )
                column = evidence.by_key[actuator_key]
                try:
                    completed = _invoke(
                        self.binary,
                        (
                            "pane",
                            "resize",
                            "--pane",
                            column.top.pane_id,
                            "--direction",
                            direction,
                            "--amount",
                            f"{amount:.6f}",
                        ),
                        self.runner,
                        self.timeout,
                        env=self.env,
                    )
                except HerdrSessionStartError:
                    return self._failure(
                        before,
                        # A command error carries no typed effect; it may have changed.
                        changed=True,
                        reason=REASON_COMMAND_UNPROVEN,
                        detail="Herdr did not prove a Unit width adjustment",
                    )
                effect = parse_changed_effect(
                    completed.stdout,
                    result_type="pane_resize",
                    envelope="resize",
                )
                if effect != EFFECT_CHANGED:
                    return self._failure(
                        before,
                        changed=changed or effect == EFFECT_UNKNOWN,
                        reason=REASON_COMMAND_UNPROVEN,
                        detail="Herdr did not prove a Unit width adjustment",
                    )
                changed = True
                previous_distance = distance
        return None

    def apply(
        self, preview: ProjectColumnPlacementPreview
    ) -> ProjectColumnPlacementResult:
        if preview.status == PREVIEW_MATCHED:
            return ProjectColumnPlacementResult(
                PLACEMENT_MATCHED,
                REASON_OK,
                "live Unit placement already matches",
                preview,
                preview,
            )
        if preview.status == PREVIEW_DEFERRED:
            return ProjectColumnPlacementResult(
                PLACEMENT_DEFERRED,
                preview.reason,
                preview.detail,
                preview,
                preview,
            )
        if not preview.can_apply or preview.evidence is None:
            return ProjectColumnPlacementResult(
                PLACEMENT_REFUSED,
                preview.reason,
                preview.detail,
                preview,
                preview,
                "Resolve the refusal and preview again.",
            )

        opening = preview.evidence
        fresh = self._observe()
        current = fresh.evidence
        if (
            not fresh.can_apply
            or current is None
            or current.source_fingerprint != opening.source_fingerprint
            or current.authority_fingerprint != opening.authority_fingerprint
            or current.layout_fingerprint != opening.layout_fingerprint
            or current.target_fingerprint != opening.target_fingerprint
        ):
            return ProjectColumnPlacementResult(
                PLACEMENT_REFUSED,
                REASON_STALE_PREVIEW,
                "configuration, live authority, or tab layout changed before apply",
                preview,
                fresh,
                "Preview the current state again before applying.",
            )

        working = list(current.current_order)
        columns_changed = False
        for destination, desired in enumerate(current.plan.desired_order):
            source = working.index(desired)
            while source > destination:
                observed = self._observe()
                evidence = observed.evidence
                if (
                    evidence is None
                    or evidence.authority_fingerprint != opening.authority_fingerprint
                    or evidence.source_fingerprint != opening.source_fingerprint
                    or evidence.target_fingerprint != opening.target_fingerprint
                    or evidence.current_order != tuple(working)
                ):
                    return self._failure(
                        preview,
                        changed=columns_changed,
                        reason=REASON_POSTCONDITION_FAILED,
                        detail="live authority changed between Unit-column operations",
                    )
                left_key, right_key = working[source - 1], working[source]
                failure = self._swap_adjacent(
                    preview,
                    evidence,
                    evidence.by_key[left_key],
                    evidence.by_key[right_key],
                )
                if failure is not None:
                    return failure
                columns_changed = True
                working[source - 1], working[source] = (
                    working[source],
                    working[source - 1],
                )
                source -= 1

        ordered = self._observe()
        ordered_evidence = ordered.evidence
        if (
            ordered_evidence is None
            or ordered_evidence.current_order != opening.plan.desired_order
            or ordered_evidence.authority_fingerprint != opening.authority_fingerprint
            or ordered_evidence.target_fingerprint != opening.target_fingerprint
        ):
            return self._failure(
                preview,
                changed=columns_changed,
                reason=REASON_POSTCONDITION_FAILED,
                detail="the measured Unit order does not match the configured order",
            )
        failure = self._resize_columns(
            preview,
            ordered_evidence,
            changed_before=columns_changed,
        )
        if failure is not None:
            return failure
        final = self._observe()
        final_evidence = final.evidence
        if (
            final.status != PREVIEW_MATCHED
            or final_evidence is None
            or final_evidence.authority_fingerprint != opening.authority_fingerprint
            or final_evidence.target_fingerprint != opening.target_fingerprint
        ):
            return self._failure(
                preview,
                changed=True,
                reason=REASON_POSTCONDITION_FAILED,
                detail="the final measured Unit placement does not match the configuration",
            )
        return ProjectColumnPlacementResult(
            PLACEMENT_APPLIED,
            REASON_OK,
            "configured Unit order and relative widths were measured after apply",
            preview,
            final,
        )

    def converge(self) -> ProjectColumnPlacementResult:
        return self.apply(self.preview())


__all__ = (
    "COLUMN_MOVE_LEFT", "COLUMN_MOVE_RIGHT", "COLUMN_WIDTH_DECREASE",
    "COLUMN_WIDTH_INCREASE", "HerdrProjectColumnPlacement",
    "PLACEMENT_APPLIED", "PLACEMENT_DEFERRED",
    "PLACEMENT_MATCHED", "PLACEMENT_PARTIAL", "PLACEMENT_REFUSED",
    "PREVIEW_DEFERRED", "PREVIEW_MATCHED", "PREVIEW_READY", "PREVIEW_REFUSED",
    "ProjectColumnPlacementPreview", "ProjectColumnPlacementResult",
    "REASON_ADJUSTMENT_INVALID", "REASON_ADJUSTMENT_UNREPRESENTABLE",
    "REASON_EDGE_REACHED",
)
