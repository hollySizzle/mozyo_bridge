"""Recovery and closing verification for project-column reflow.

The launch-time choreography remains in :mod:`herdr_project_column_reflow`.
This sibling owns the bounded recovery/verification family so the orchestration
module stays below the module-health ceiling without hiding its I/O seams.
``ProjectColumnReflowPorts`` is assembled by the facade at call time; patching
the established facade imports therefore continues to affect recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from mozyo_bridge.core.state.herdr_launch_generation import verified_generation_token
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_managed_column_scope import (  # noqa: E501
    ManagedColumnScope,
    managed_column_scope_matches,
    managed_external_boundary_matches,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    LayoutSnapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (  # noqa: E501
    CoordinatorPane,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_internal_ratio import (  # noqa: E501
    ColumnInternalRatio,
    effective_internal_ratios_match,
    internal_ratios_match,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
)


ProjectGroups = Mapping[tuple[str, str], tuple[CoordinatorPane, ...]]


class ColumnAttachLike(Protocol):
    """Structural part of ``ColumnAttach`` needed by recovery accounting."""

    pane: str


@dataclass(frozen=True)
class ProjectColumnReflowPorts:
    """Facade-owned I/O and verdict seams consumed by recovery."""

    attach_pane: Callable[..., str]
    identity_map: Callable[
        [Sequence[Mapping[str, object]], str], dict[str, str]
    ]
    list_rows: Callable[..., Sequence[Mapping[str, object]]]
    read_pane_layout: Callable[..., LayoutSnapshot | None]
    balance_project_columns: Callable[..., tuple[bool, str]]
    balanced_column_verdict: Callable[
        [LayoutSnapshot, ProjectGroups], tuple[bool, str]
    ]
    columnar_verdict: Callable[
        [LayoutSnapshot, ProjectGroups], tuple[bool, str]
    ]
    max_equal_project_columns: int
    failed_outcome: str
    prepared_outcome: str
    applied_outcome: str
    matched_outcome: str


def generation_authority_fingerprint(
    groups: ProjectGroups,
    home: Path,
) -> tuple[tuple[tuple[str, str], tuple[tuple[str, str, str, str], ...]], ...] | None:
    """Exact nonsecret fingerprint of each pane's completed generation."""
    entries: list[
        tuple[tuple[str, str], tuple[tuple[str, str, str, str], ...]]
    ] = []
    for key, members in sorted(groups.items()):
        slots: list[tuple[str, str, str, str]] = []
        for pane in sorted(members, key=lambda item: item.role):
            token = verified_generation_token(
                home,
                assigned_name=pane.assigned_name,
                workspace_id=pane.workspace_id,
                role=pane.role,
                lane_id=pane.lane_id,
                locator=pane.locator,
                live_terminal_id=pane.terminal_id,
                norm=_norm,
                norm_lane=_norm_lane,
            )
            if not token:
                return None
            slots.append((pane.role, pane.assigned_name, pane.locator, token))
        entries.append((key, tuple(slots)))
    return tuple(entries)


def phase_internal_ratios_match(
    layout: LayoutSnapshot,
    expected: Sequence[ColumnInternalRatio],
    present_managed_ids: Sequence[str] | set[str] | frozenset[str],
) -> bool:
    """Verify every complete Unit that remains in the current main-tab phase."""
    present = frozenset(present_managed_ids)
    current: list[ColumnInternalRatio] = []
    for item in expected:
        top_present = item.top in present
        lower_present = item.lower in present
        if lower_present and not top_present:
            return False
        if top_present and lower_present:
            current.append(item)
    matched, _detail = effective_internal_ratios_match(layout, current)
    return matched


def restore_detached(
    detached: Sequence[str],
    tab_id: str,
    planned: Sequence[ColumnAttachLike],
    *,
    before: Mapping[str, str],
    target_workspace: str,
    anchor: str,
    internal_ratios: Sequence[ColumnInternalRatio],
    managed_scope: ManagedColumnScope,
    ports: ProjectColumnReflowPorts,
    authority_check: Callable[[], bool] | None = None,
    binary: str,
    runner,
    timeout: float,
    env,
) -> tuple[tuple[str, ...], str]:
    """Best-effort return, then verify inventory, tab and ratios afresh."""
    stranded: set[str] = set()
    pending = set(detached)
    guard_refusal = ""
    for attach in planned:
        if attach.pane not in pending:
            continue
        boundary = ports.read_pane_layout(
            anchor, binary=binary, runner=runner, timeout=timeout, env=env
        )
        present = managed_scope.pane_ids.difference(pending)
        if (
            boundary is None
            or not managed_external_boundary_matches(
                boundary,
                managed_scope,
                present_managed_ids=present,
            )
            or not phase_internal_ratios_match(
                boundary,
                internal_ratios,
                present,
            )
            or (authority_check is not None and not authority_check())
        ):
            stranded.add(attach.pane)
            guard_refusal = (
                "the recovery generation or managed/external boundary changed "
                "before a return move"
            )
            continue
        refusal = ports.attach_pane(
            attach,
            tab_id,
            binary=binary,
            runner=runner,
            timeout=timeout,
            env=env,
        )
        if refusal:
            stranded.add(attach.pane)
            continue
        pending.remove(attach.pane)
    stranded.update(pending)
    if authority_check is not None and not authority_check():
        return tuple(sorted(stranded)), (
            "the recovery generation authority changed before final verification"
        )
    try:
        after = ports.identity_map(
            ports.list_rows(binary, runner, timeout), target_workspace
        )
    except HerdrSessionStartError:
        return tuple(sorted(stranded)), (
            "the shared workspace inventory could not be read during recovery"
        )
    if after != before:
        lost = sorted(set(before) - set(after))
        changed = sorted(
            locator
            for locator in set(before) & set(after)
            if before[locator] != after[locator]
        )
        return tuple(sorted(stranded)), (
            "the shared workspace inventory changed during recovery "
            f"(missing: {lost!r}, renamed: {changed!r})"
        )
    layout = ports.read_pane_layout(
        anchor, binary=binary, runner=runner, timeout=timeout, env=env
    )
    if layout is None:
        return tuple(sorted(stranded)), (
            "the recovery pane layout could not be read or parsed"
        )
    if not managed_column_scope_matches(layout, managed_scope):
        return tuple(sorted(stranded)), (
            "the recovery changed the managed/external geometry boundary"
        )
    if _norm(layout.tab_id) != tab_id:
        return tuple(sorted(stranded)), (
            f"the recovery layout reports tab {layout.tab_id!r}, not {tab_id!r}"
        )
    ratios_ok, ratio_detail = effective_internal_ratios_match(
        layout, internal_ratios
    )
    if not ratios_ok:
        return tuple(sorted(stranded)), (
            "a Unit's internal ratio changed during recovery: "
            f"{ratio_detail}"
        )
    if guard_refusal:
        return tuple(sorted(stranded)), guard_refusal
    return (), ""


def stranded_detail(
    refusal: str,
    stranded: Sequence[str],
    recovery_refusal: str,
) -> str:
    """Build a failure detail that claims recovery only after observation."""
    if not recovery_refusal:
        return (
            f"{refusal}; every detached pane was returned to the shared tab, "
            "and identities and internal ratios were verified"
        )
    if not stranded:
        return f"{refusal}; recovery could not be verified: {recovery_refusal}"
    return (
        f"{refusal}; pane(s) {sorted(stranded)!r} are NOT in the shared "
        "project-coordinator tab or their return could not be verified "
        f"({recovery_refusal}); the live-relayout runbook is required"
    )


def verify_reflow(
    before: Mapping[str, str],
    groups: ProjectGroups,
    target_workspace: str,
    tab_id: str,
    *,
    anchor: str,
    geometry_changed: bool,
    internal_ratios: Sequence[ColumnInternalRatio],
    managed_scope: ManagedColumnScope,
    ports: ProjectColumnReflowPorts,
    authority_check: Callable[[], bool] | None = None,
    binary: str,
    runner,
    timeout: float,
    env,
) -> tuple[str, str]:
    """Measure and equalise produced columns: identity, then geometry."""
    if authority_check is not None and not authority_check():
        return ports.failed_outcome, (
            "managed generation authority changed before verification"
        )
    after = ports.identity_map(
        ports.list_rows(binary, runner, timeout), target_workspace
    )
    if after != before:
        lost = sorted(set(before) - set(after))
        changed = sorted(
            locator
            for locator in set(before) & set(after)
            if before[locator] != after[locator]
        )
        return ports.failed_outcome, (
            "the shared workspace inventory changed across the reflow "
            f"(missing: {lost!r}, renamed: {changed!r}); the geometry is not claimed"
        )
    layout = ports.read_pane_layout(
        anchor, binary=binary, runner=runner, timeout=timeout, env=env
    )
    if layout is None:
        return ports.failed_outcome, (
            "the closing pane layout could not be read or parsed"
        )
    if not managed_column_scope_matches(layout, managed_scope):
        return ports.failed_outcome, (
            "the managed column scope or its external boundary changed"
        )
    if _norm(layout.tab_id) != tab_id:
        return ports.failed_outcome, (
            f"the closing layout reports tab {layout.tab_id!r}, not {tab_id!r}"
        )
    columnar, reason = ports.columnar_verdict(layout, groups)
    if not columnar:
        return ports.failed_outcome, (
            f"the reflowed tab is still not columnar: {reason}"
        )
    ratios_ok, ratio_detail = internal_ratios_match(layout, internal_ratios)
    if not ratios_ok:
        return ports.failed_outcome, (
            "a Unit's internal ratio changed across project-column reflow: "
            f"{ratio_detail}"
        )
    if len(groups) > ports.max_equal_project_columns:
        action = "now own" if geometry_changed else "already own"
        return ports.prepared_outcome, (
            f"{len(groups)} project pair(s) {action} full-height columns; "
            "configured placement must establish their final relative widths"
        )

    def same_complete_unit_topology(fresh: LayoutSnapshot) -> bool:
        fresh_columnar, _fresh_reason = ports.columnar_verdict(fresh, groups)
        fresh_ratios, _fresh_ratio_detail = internal_ratios_match(
            fresh,
            internal_ratios,
        )
        return fresh_columnar and fresh_ratios

    resized, refusal = ports.balance_project_columns(
        layout,
        groups,
        binary=binary,
        runner=runner,
        timeout=timeout,
        env=env,
        authority_check=authority_check,
        layout_check=same_complete_unit_topology,
    )
    if refusal:
        return ports.failed_outcome, refusal
    closing = ports.read_pane_layout(
        anchor, binary=binary, runner=runner, timeout=timeout, env=env
    )
    if closing is None:
        return ports.failed_outcome, (
            "the balanced pane layout could not be read or parsed"
        )
    balanced, reason = ports.balanced_column_verdict(closing, groups)
    if not balanced:
        return ports.failed_outcome, (
            f"the project columns are still not balanced: {reason}"
        )
    ratios_ok, ratio_detail = internal_ratios_match(closing, internal_ratios)
    if not ratios_ok:
        return ports.failed_outcome, (
            "a Unit's internal ratio changed during project-column balancing: "
            f"{ratio_detail}"
        )
    final_inventory = ports.identity_map(
        ports.list_rows(binary, runner, timeout), target_workspace
    )
    if final_inventory != before:
        return ports.failed_outcome, (
            "the shared workspace inventory changed during project-column balancing"
        )
    if authority_check is not None and not authority_check():
        return ports.failed_outcome, (
            "managed generation authority changed before success"
        )
    outcome = (
        ports.applied_outcome
        if geometry_changed or resized
        else ports.matched_outcome
    )
    action = "now own" if outcome == ports.applied_outcome else "already own"
    return outcome, (
        f"{len(groups)} project pair(s) {action} equal-width full-height columns "
        f"in tab {tab_id}"
    )


__all__ = (
    "ProjectColumnReflowPorts",
    "generation_authority_fingerprint",
    "phase_internal_ratios_match",
    "restore_detached",
    "stranded_detail",
    "verify_reflow",
)
