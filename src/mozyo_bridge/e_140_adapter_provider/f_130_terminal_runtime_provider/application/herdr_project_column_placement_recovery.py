"""Recovery and phase-boundary checks for project-column placement."""

from __future__ import annotations

from typing import Mapping, Optional, Sequence, TYPE_CHECKING

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_command_effect import (  # noqa: E501
    EFFECT_CHANGED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_managed_column_scope import (  # noqa: E501
    ManagedColumnScope,
    managed_external_boundary_matches,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    LayoutSnapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_placement_model import (  # noqa: E501
    LiveUnitColumn,
    ProjectColumnPlacementEvidence,
    internal_pair_matches,
)


class ProjectColumnPlacementRecoveryMixin:
    """Keep temporary detachment recovery behind a typed placement boundary."""

    if TYPE_CHECKING:

        def _read_layout(self, pane_id: str) -> Optional[LayoutSnapshot]: ...

        def _same_authority(
            self, opening: ProjectColumnPlacementEvidence
        ) -> bool: ...

        def _move(
            self,
            tail: Sequence[str],
            *,
            expected_pane: str,
            expected_tab: str = "",
        ) -> tuple[str, str]: ...

    def _phase_layouts(
        self,
        *,
        main_anchor: str,
        detached: Mapping[str, str],
        expected_main: set[str],
        tab_id: str,
        top_order: Sequence[str],
        columns: Sequence[LiveUnitColumn],
        attached: Optional[Mapping[str, tuple[str, float]]] = None,
        managed_scope: Optional[ManagedColumnScope] = None,
    ) -> bool:
        main = self._read_layout(main_anchor)
        if main is None or main.tab_id != tab_id or set(main.panes) != expected_main:
            return False
        if managed_scope is not None and not managed_external_boundary_matches(
            main,
            managed_scope,
            present_managed_ids=managed_scope.pane_ids.intersection(expected_main),
        ):
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
        # Every complete Unit that was not detached must retain its exact
        # top/lower membership and opening divider ratio.
        for column in columns:
            top = column.top.pane_id
            lower = column.lower.pane_id
            top_present = top in expected_main
            lower_present = lower in expected_main
            if not top_present or (not lower_present and lower not in detached):
                return False
            if lower_present and (
                lower in detached
                or not internal_pair_matches(
                    main,
                    top=top,
                    lower=lower,
                    target_ratio=column.internal_ratio,
                )
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
        pending = set(detached)
        for lower, (top, ratio, temp_tab) in detached.items():
            if not self._same_authority(opening):
                stranded += 1
                continue
            main = self._read_layout(top)
            present = opening.managed_scope.pane_ids.difference(pending)
            if (
                main is None
                or not managed_external_boundary_matches(
                    main,
                    opening.managed_scope,
                    present_managed_ids=present,
                )
                or any(
                    candidate.top.pane_id in present
                    and candidate.lower.pane_id in present
                    and not internal_pair_matches(
                        main,
                        top=candidate.top.pane_id,
                        lower=candidate.lower.pane_id,
                        target_ratio=candidate.internal_ratio,
                    )
                    for candidate in opening.columns
                )
            ):
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
                else:
                    pending.discard(lower)
                continue
            if layout.tab_id != temp_tab or set(layout.panes) != {lower} or layout.splits:
                stranded += 1
                continue
            if not self._same_authority(opening):
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
                    continue
            pending.discard(lower)
            closing = self._read_layout(top)
            if (
                not self._same_authority(opening)
                or closing is None
                or not managed_external_boundary_matches(
                    closing,
                    opening.managed_scope,
                    present_managed_ids=opening.managed_scope.pane_ids.difference(
                        pending
                    ),
                )
                or not internal_pair_matches(
                    closing,
                    top=top,
                    lower=lower,
                    target_ratio=ratio,
                )
            ):
                stranded += 1
        if not self._same_authority(opening):
            stranded += len(pending) or 1
        return stranded


__all__ = ("ProjectColumnPlacementRecoveryMixin",)
