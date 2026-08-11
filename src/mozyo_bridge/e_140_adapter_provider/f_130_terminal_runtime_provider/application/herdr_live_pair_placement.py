"""Preview-first live placement for one dedicated Herdr agent pair (#14608).

The durable target is ``(workspace_id, lane_id)``.  Pane ids are recovered from
the exact managed assigned names on every observation and are never accepted as
operator input.  Mutation is deliberately narrow: the two managed panes must be
the only panes in their tab and must exactly tile one unambiguous divider.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.core.state.herdr_launch_generation import (
    HerdrLaunchGenerationStore,
    verified_generation_token,
)
from mozyo_bridge.core.state.lane_kind import LANE_KIND_COORDINATOR
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    LaneLifecycleKey,
    ProcessGenerationPin,
)
from mozyo_bridge.core.state.lane_lifecycle_readonly import LaneLifecycleReader
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair
from mozyo_bridge.core.state.workspace_registry import WorkspaceRecord, load_workspace_by_id
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    safe_text,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (
    MAX_RESIZE_PASSES,
    RATIO_APPLIED,
    RATIO_FAILED,
    RATIO_MATCHED,
    RESIZE_CHANGED,
    RESIZE_UNCHANGED,
    PaneRect,
    SplitInfo,
    _read_layout,
    _resize,
    find_pair_split,
    governing_split,
    order_pair,
    ratio_verdict,
    resize_step,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_command_effect import (
    parse_changed_effect,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    _invoke,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_reflow import (
    _move_result,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_project_column_authority import (
    IdentityWorkspaceResolver,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_LOCATOR,
    AGENT_KEY_LOCATOR_ALIAS,
    AGENT_KEY_LOCATOR_ALIAS_2,
    _norm,
    _norm_lane,
    decode_assigned_name,
    encode_assigned_name,
    rebind_by_name,
    terminal_identity_of_live_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (
    SLOT_LIVE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (
    HerdrCliAgentLister,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
    Runner,
    resolve_herdr_binary,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


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


def _refused(workspace_id: str, lane_id: str, reason: str, detail: str) -> PlacementPlan:
    return PlacementPlan(PLAN_REFUSED, reason, detail, workspace_id, lane_id)


def _target_for(record: WorkspaceRecord, lane_id: str) -> PlacementTarget:
    config = load_repo_local_config(Path(record.canonical_path))
    lane_class = "default" if lane_id == "default" else "sublane"
    lane_kind: Optional[str] = LANE_KIND_COORDINATOR if lane_class == "default" else None
    lifecycle = None
    declared_pins: tuple[ProcessGenerationPin, ...] = ()
    if lane_class == "sublane":
        lifecycle = LaneLifecycleReader().get(LaneLifecycleKey(record.workspace_id, lane_id))
        if (
            lifecycle is None
            or getattr(lifecycle, "lane_disposition", None) != DISPOSITION_ACTIVE
        ):
            raise ValueError("sublane placement requires an active lifecycle")
        lane_kind = (lifecycle.lane_kind or None) if lifecycle is not None else None
    resolved = config.lane_placement.resolve_effective(lane_class, lane_kind)
    if lane_class == "default":
        order = resolved.order
    else:
        declared = read_declared_pin_pair(lifecycle) if lifecycle is not None else None
        if declared is None or not declared.ok:
            raise ValueError("sublane placement requires one declared live pair")
        if not isinstance(declared.gateway, ProcessGenerationPin) or not isinstance(
            declared.worker, ProcessGenerationPin
        ):
            raise ValueError("sublane placement requires typed generation pins")
        declared_pins = (declared.gateway, declared.worker)
        pair_order = (
            getattr(declared.gateway, "provider", ""),
            getattr(declared.worker, "provider", ""),
        )
        order = resolved.order or pair_order
    if (
        resolved.split not in {"down", "right"}
        or resolved.ratio is None
        or len(order or ()) != 2
        or not all(isinstance(value, str) and value for value in order or ())
        or len(set(order or ())) != 2
        or (
            declared_pins
            and set(order or ()) != {pin.provider for pin in declared_pins}
        )
    ):
        raise ValueError("effective lane placement is not one exact two-provider target")
    return PlacementTarget(
        resolved.split,
        tuple(order),  # type: ignore[arg-type]
        float(resolved.ratio),
        declared_pins,
    )


def _current_split(layout, pane_to_provider: Mapping[str, str]):
    if len(layout.splits) != 1:
        return None
    pane_ids = tuple(pane_to_provider)
    candidates = []
    for direction in ("down", "right"):
        one, two = layout.panes[pane_ids[0]], layout.panes[pane_ids[1]]
        if order_pair(one, two, direction):
            first_id, second_id = pane_ids
            first, second = one, two
        else:
            first_id, second_id = pane_ids[1], pane_ids[0]
            first, second = two, one
        split = find_pair_split(layout, first, second, direction)
        if split is None:
            continue
        governing = governing_split(layout, first, direction)
        if governing is not None and governing.rect == split.rect:
            candidates.append((split, first, pane_to_provider[first_id], pane_to_provider[second_id]))
    return candidates[0] if len(candidates) == 1 else None


def _row_locator_claims(row: Mapping[str, object]) -> frozenset[str]:
    """Return every non-empty locator alias one inventory row claims."""
    claims: set[str] = set()
    for key in (AGENT_KEY_LOCATOR, AGENT_KEY_LOCATOR_ALIAS, AGENT_KEY_LOCATOR_ALIAS_2):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            claims.add(value.strip())
    return frozenset(claims)


def decide_plan(
    *,
    workspace_id: str,
    lane_id: str,
    target: PlacementTarget,
    evidence: PairEvidence,
) -> PlacementPlan:
    operations: list[str] = []
    if evidence.split.direction != target.split:
        operations.append("change_split")
    elif evidence.current_order != target.order:
        operations.append("swap_order")
    first_rect = evidence.rect_by_provider[evidence.current_order[0]]
    matches_ratio, _ = ratio_verdict(evidence.split, first_rect, target.ratio)
    if not matches_ratio:
        operations.append("resize_ratio")
    status = PLAN_READY if operations else PLAN_MATCHED
    return PlacementPlan(
        status=status,
        reason=REASON_OK,
        detail=("placement changes are ready" if operations else "live placement already matches"),
        workspace_id=workspace_id,
        lane_id=lane_id,
        target=target,
        current_split=evidence.split.direction,
        current_order=evidence.current_order,
        current_ratio=evidence.split.ratio,
        operations=tuple(operations),
        evidence=evidence,
    )


class HerdrLivePairPlacement:
    """Read and mutate one exact managed pair through injectable Herdr IO."""

    def __init__(
        self,
        binary: str,
        *,
        runner: Optional[Runner] = None,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        lister: Optional[HerdrCliAgentLister] = None,
        generation_store: Optional[HerdrLaunchGenerationStore] = None,
        generation_verifier: Optional[Callable[..., str]] = None,
        workspace_loader: Callable[[str], Optional[WorkspaceRecord]] = (
            load_workspace_by_id
        ),
        workspace_resolver: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.binary = binary
        self.runner: Runner = runner or subprocess.run
        self.timeout = timeout
        self.lister = lister or HerdrCliAgentLister(binary, runner=self.runner, timeout=timeout)
        self.generations = generation_store or HerdrLaunchGenerationStore()
        self.generation_verifier = generation_verifier or verified_generation_token
        self.workspace_loader = workspace_loader
        self.workspace_of = workspace_resolver or IdentityWorkspaceResolver(
            mozyo_bridge_home()
        ).workspace_of

    def _resolve_slots(
        self,
        *,
        workspace_id: str,
        lane_id: str,
        target: PlacementTarget,
        rows: Sequence[Mapping[str, object]],
    ) -> tuple[Optional[tuple[LiveSlot, LiveSlot]], str, str]:
        """Join exact live providers to their current launch generations."""
        if any(not isinstance(row, Mapping) for row in rows):
            return None, REASON_PAIR_INVALID, "the live inventory contains an unreadable row"

        exact_rows: list[Mapping[str, object]] = []
        for row in rows:
            decoded = decode_assigned_name(row.get("name"))
            if decoded.ok and decoded.identity is not None:
                identity = decoded.identity
                if identity.workspace_id == workspace_id and identity.lane_id == lane_id:
                    exact_rows.append(row)
        if len(exact_rows) != 2:
            return (
                None,
                REASON_PAIR_INVALID,
                "the unit does not have exactly two managed live agents",
            )

        slots: list[LiveSlot] = []
        locators: set[str] = set()
        pin_by_provider: dict[str, ProcessGenerationPin] = {}
        for pin in target.declared_pins:
            if not isinstance(pin, ProcessGenerationPin) or pin.provider in pin_by_provider:
                return (
                    None,
                    REASON_PAIR_INVALID,
                    "the declared provider generation is ambiguous",
                )
            pin_by_provider[pin.provider] = pin
        if pin_by_provider and set(pin_by_provider) != set(target.order):
            return (
                None,
                REASON_PAIR_INVALID,
                "the declared provider pair does not match the placement target",
            )
        for provider in target.order:
            name = encode_assigned_name(workspace_id, provider, lane_id)
            matching = [row for row in exact_rows if row.get("name") == name]
            if len(matching) != 1:
                return (
                    None,
                    REASON_PAIR_INVALID,
                    "a managed provider slot is missing or duplicated",
                )
            row = matching[0]
            rebound = rebind_by_name(name, rows)
            owners = [
                candidate
                for candidate in rows
                if rebound.locator and rebound.locator in _row_locator_claims(candidate)
            ]
            if (
                not rebound.is_rebound
                or not valid_target(rebound.locator)
                or rebound.locator in locators
                or _row_locator_claims(row) != {rebound.locator}
                or len(owners) != 1
                or owners[0] is not row
            ):
                return (
                    None,
                    REASON_PAIR_INVALID,
                    "a managed provider locator is missing, duplicated, or invalid",
                )
            locators.add(rebound.locator)

            raw_runtime_revision = row.get("runtime_revision")
            if raw_runtime_revision is None:
                runtime_revision = ""
            elif isinstance(raw_runtime_revision, str):
                runtime_revision = raw_runtime_revision.strip()
            else:
                return (
                    None,
                    REASON_PAIR_INVALID,
                    "a managed provider has malformed runtime revision evidence",
                )
            declared_pin = pin_by_provider.get(provider)
            if declared_pin is not None:
                live_pin = ProcessGenerationPin(
                    role=declared_pin.role,
                    provider=provider,
                    assigned_name=name,
                    locator=rebound.locator,
                    runtime_revision=runtime_revision,
                )
                if not declared_pin.binds_same_generation(live_pin):
                    return (
                        None,
                        REASON_PAIR_INVALID,
                        "a managed provider does not match its declared generation",
                    )
            if row.get("agent") != provider or classify_named_slot(row) != SLOT_LIVE:
                return (
                    None,
                    REASON_PAIR_INVALID,
                    "a managed provider is not positively live in its assigned slot",
                )
            cwd = row.get("cwd")
            if not isinstance(cwd, str) or not cwd.strip():
                return (
                    None,
                    REASON_PAIR_INVALID,
                    "a managed provider has no stable workspace directory",
                )
            try:
                resolved_workspace = self.workspace_of(cwd)
            except (OSError, RuntimeError, ValueError):
                resolved_workspace = ""
            if resolved_workspace != workspace_id:
                return (
                    None,
                    REASON_PAIR_INVALID,
                    "a managed provider directory does not resolve to the unit workspace",
                )
            foreground = row.get("foreground_cwd")
            if foreground is not None and not isinstance(foreground, str):
                return (
                    None,
                    REASON_PAIR_INVALID,
                    "a managed provider has malformed foreground directory evidence",
                )
            if isinstance(foreground, str) and foreground.strip() and foreground != cwd:
                try:
                    foreground_workspace = self.workspace_of(foreground)
                except (OSError, RuntimeError, ValueError):
                    foreground_workspace = ""
                if foreground_workspace and foreground_workspace != workspace_id:
                    return (
                        None,
                        REASON_PAIR_INVALID,
                        "a managed provider foreground process resolves to another workspace",
                    )

            terminal_id = terminal_identity_of_live_slot(name, rebound.locator, rows)
            generation_token = self.generation_verifier(
                None, assigned_name=name, workspace_id=workspace_id, role=provider,
                lane_id=lane_id, locator=rebound.locator,
                live_terminal_id=terminal_id, norm=_norm, norm_lane=_norm_lane,
            )
            if not generation_token:
                return (
                    None,
                    REASON_GENERATION_UNVERIFIED,
                    "a managed provider generation is not currently attested",
                )
            slots.append(
                LiveSlot(
                    provider, name,
                    rebound.locator,
                    generation_token,
                    runtime_revision,
                )
            )
        return tuple(slots), REASON_OK, ""  # type: ignore[return-value]

    def _observe(self, workspace_id: str, lane_id: str) -> PlacementPlan:
        record = self.workspace_loader(workspace_id)
        if record is None:
            return _refused(workspace_id, lane_id, REASON_WORKSPACE_UNKNOWN, "workspace is not registered")
        try:
            target = _target_for(record, lane_id)
        except Exception:
            return _refused(workspace_id, lane_id, REASON_CONFIG_INVALID, "effective lane placement could not be resolved")
        try:
            rows = tuple(self.lister.list_agent_rows())
        except Exception:
            return _refused(workspace_id, lane_id, REASON_INVENTORY_UNAVAILABLE, "live Herdr agent inventory could not be read")

        slots, reason, detail = self._resolve_slots(
            workspace_id=workspace_id,
            lane_id=lane_id,
            target=target,
            rows=rows,
        )
        if slots is None:
            return _refused(workspace_id, lane_id, reason, detail)

        layout = _read_layout(slots[0].pane_id, binary=self.binary, runner=self.runner, timeout=self.timeout, env=None)
        if layout is None or not valid_target(layout.tab_id):
            return _refused(workspace_id, lane_id, REASON_LAYOUT_UNAVAILABLE, "the live pane layout could not be read")
        expected_panes = {slot.pane_id for slot in slots}
        if set(layout.panes) != expected_panes:
            return _refused(workspace_id, lane_id, REASON_NOT_DEDICATED_PAIR, "the unit does not exclusively occupy a two-pane tab")
        pane_to_provider = {slot.pane_id: slot.provider for slot in slots}
        current = _current_split(layout, pane_to_provider)
        if current is None:
            return _refused(workspace_id, lane_id, REASON_GEOMETRY_UNSUPPORTED, "the two panes do not form one unambiguous supported divider")
        split, first_rect, first_provider, second_provider = current
        rects = tuple(
            (slot.provider, layout.panes[slot.pane_id]) for slot in slots
        )
        evidence = PairEvidence(
            workspace_id,
            lane_id,
            layout.tab_id,
            slots,
            split,
            rects,
            (first_provider, second_provider),
        )
        return decide_plan(
            workspace_id=workspace_id,
            lane_id=lane_id,
            target=target,
            evidence=evidence,
        )

    def preview(self, workspace_id: str, lane_id: str = "default") -> PlacementPlan:
        if not isinstance(lane_id, str) or not lane_id.strip():
            return _refused(workspace_id, "", REASON_CONFIG_INVALID, "lane identity must not be empty")
        return self._observe(workspace_id, lane_id)

    def _swap(self, tail: Sequence[str]) -> str:
        """Return a typed swap effect; process exit zero is not enough."""
        try:
            completed = _invoke(
                self.binary, tail, self.runner, self.timeout, env=None
            )
        except HerdrSessionStartError:
            return MOVE_UNKNOWN
        return parse_changed_effect(
            completed.stdout, result_type="pane_swap", envelope="swap"
        )

    def _move(
        self,
        tail: Sequence[str],
        *,
        expected_pane: str,
        expected_tab: str = "",
    ) -> tuple[str, str]:
        """Return typed effect and landed tab; exit zero alone is not success."""
        try:
            completed = _invoke(
                self.binary, tail, self.runner, self.timeout, env=None
            )
        except HerdrSessionStartError:
            return MOVE_UNKNOWN, ""
        landed = _move_result(completed.stdout)
        if landed is not None:
            if landed[0] != expected_pane:
                return MOVE_UNKNOWN, landed[1]
            if not valid_target(landed[1]):
                return MOVE_CHANGED, ""
            if expected_tab and landed[1] != expected_tab:
                return MOVE_CHANGED, landed[1]
            return MOVE_CHANGED, landed[1]
        try:
            payload = json.loads(completed.stdout)
            changed = payload["result"]["move_result"]["changed"]
        except (KeyError, TypeError, ValueError):
            return MOVE_UNKNOWN, ""
        return (MOVE_UNCHANGED, "") if changed is False else (MOVE_UNKNOWN, "")

    def _refresh_slots(
        self, evidence: PairEvidence, target: PlacementTarget
    ) -> Optional[tuple[LiveSlot, LiveSlot]]:
        record = self.workspace_loader(evidence.workspace_id)
        if record is None:
            return None
        try:
            current_target = _target_for(record, evidence.lane_id)
            rows = tuple(self.lister.list_agent_rows())
        except Exception:
            return None
        if current_target != target:
            return None
        slots, _, _ = self._resolve_slots(
            workspace_id=evidence.workspace_id,
            lane_id=evidence.lane_id,
            target=target,
            rows=rows,
        )
        return slots

    def _same_generations(
        self, evidence: PairEvidence, target: PlacementTarget
    ) -> bool:
        slots = self._refresh_slots(evidence, target)
        return bool(
            slots is not None
            and tuple(slot.fingerprint for slot in slots)
            == tuple(slot.fingerprint for slot in evidence.slots)
        )

    def _detached_state_is_safe(
        self,
        evidence: PairEvidence,
        target: PlacementTarget,
        *,
        temp_tab: str,
    ) -> bool:
        if not temp_tab or not self._same_generations(evidence, target):
            return False
        slots = evidence.by_provider
        staying = slots[target.order[0]]
        moving = slots[target.order[1]]
        original = _read_layout(
            staying.pane_id,
            binary=self.binary,
            runner=self.runner,
            timeout=self.timeout,
            env=None,
        )
        temporary = _read_layout(
            moving.pane_id,
            binary=self.binary,
            runner=self.runner,
            timeout=self.timeout,
            env=None,
        )
        return bool(
            original is not None
            and temporary is not None
            and original.tab_id == evidence.tab_id
            and temporary.tab_id == temp_tab
            and set(original.panes) == {staying.pane_id}
            and set(temporary.panes) == {moving.pane_id}
            and not original.splits
            and not temporary.splits
        )

    @staticmethod
    def _same_opening_authority(
        opening: PairEvidence, current: PairEvidence
    ) -> bool:
        return current.authority_fingerprint == opening.authority_fingerprint

    def _apply_ratio_guarded(
        self,
        *,
        opening: PairEvidence,
        target: PlacementTarget,
    ) -> tuple[str, str, str]:
        """Resize with a fresh authority fence and retain the proven effect."""
        effect = MOVE_UNCHANGED
        previous_distance: Optional[float] = None
        detail = "the divider was not measured"
        for pass_index in range(MAX_RESIZE_PASSES + 1):
            current = self._observe(opening.workspace_id, opening.lane_id)
            if (
                current.status == PLAN_REFUSED
                or current.evidence is None
                or current.target != target
                or not self._same_opening_authority(opening, current.evidence)
                or current.evidence.split.direction != target.split
                or current.evidence.current_order != target.order
            ):
                return RATIO_FAILED, "pair authority changed before ratio actuation", effect
            first = current.evidence.rect_by_provider[target.order[0]]
            matched, detail = ratio_verdict(current.evidence.split, first, target.ratio)
            if matched:
                outcome = RATIO_APPLIED if effect == MOVE_CHANGED else RATIO_MATCHED
                return outcome, detail, effect
            distance = abs(current.evidence.split.ratio - target.ratio)
            if previous_distance is not None and distance >= previous_distance:
                detail = f"Herdr stopped moving the divider toward the target; {detail}"
                return RATIO_FAILED, detail, effect
            if pass_index >= MAX_RESIZE_PASSES:
                break
            token, amount = resize_step(
                current.evidence.split.ratio, target.ratio, target.split
            )
            previous_distance = distance
            resize_effect = _resize(
                current.evidence.by_provider[target.order[token != target.split]].pane_id,
                token,
                amount,
                binary=self.binary,
                runner=self.runner,
                timeout=self.timeout,
                env=None,
            )
            if resize_effect == RESIZE_CHANGED:
                effect = MOVE_CHANGED
                continue
            if resize_effect == RESIZE_UNCHANGED:
                detail = "Herdr reported that the ratio adjustment changed nothing"
                return RATIO_FAILED, detail, effect
            unknown_effect = MOVE_CHANGED if effect == MOVE_CHANGED else MOVE_UNKNOWN
            return RATIO_FAILED, "Herdr did not prove the ratio adjustment effect", unknown_effect
        detail = f"the divider did not reach the target; {detail}"
        return RATIO_FAILED, detail, effect

    def _result_after_failure(
        self,
        before: PlacementPlan,
        *,
        effect: str,
        detail: str,
        reason: str = REASON_COMMAND_FAILED,
    ) -> PlacementApplyResult:
        """Report typed failure without pretending an unknown command had no effect."""
        after = self._observe(before.workspace_id, before.lane_id)
        status = APPLY_FAILED if effect == MOVE_UNCHANGED else APPLY_PARTIAL
        recovery = (
            "Do not retry blindly; inspect the Unit, follow the runbook, then preview again."
            if status == APPLY_PARTIAL
            else "Resolve the reported refusal and run preview again before apply."
        )
        return PlacementApplyResult(status, reason, detail, before, after, recovery)

    def apply(self, workspace_id: str, lane_id: str = "default") -> PlacementApplyResult:
        before = self.preview(workspace_id, lane_id)
        if before.status == PLAN_MATCHED:
            return PlacementApplyResult(APPLY_MATCHED, REASON_OK, "live placement already matches", before, before)
        if not before.can_apply or before.evidence is None or before.target is None:
            return PlacementApplyResult(APPLY_REFUSED, before.reason, before.detail, before, before)

        fresh = self._observe(workspace_id, lane_id)
        if (
            not fresh.can_apply
            or fresh.evidence is None
            or fresh.target != before.target
            or fresh.evidence.fingerprint != before.evidence.fingerprint
        ):
            return PlacementApplyResult(APPLY_REFUSED, REASON_STALE, "identity, generation, target, or geometry changed before apply", before, fresh)
        evidence = fresh.evidence
        target = fresh.target
        slots = evidence.by_provider
        changed = False

        if evidence.split.direction != target.split:
            staying = slots[target.order[0]]
            moving = slots[target.order[1]]
            first_effect, temp_tab = self._move(
                ("pane", "move", moving.pane_id, "--new-tab", "--no-focus"),
                expected_pane=moving.pane_id,
            )
            if first_effect != MOVE_CHANGED:
                return self._result_after_failure(
                    before,
                    effect=first_effect,
                    detail="Herdr did not prove the temporary pane move",
                )
            changed = True
            if not self._detached_state_is_safe(
                evidence, target, temp_tab=temp_tab
            ):
                return self._result_after_failure(
                    before,
                    effect=MOVE_CHANGED,
                    detail="the detached pair authority changed before the return move",
                    reason=REASON_POSTCONDITION_FAILED,
                )
            second_effect, landed_tab = self._move(
                (
                    "pane", "move", moving.pane_id,
                    "--tab", evidence.tab_id,
                    "--split", target.split,
                    "--target-pane", staying.pane_id,
                    "--no-focus",
                ),
                expected_pane=moving.pane_id,
                expected_tab=evidence.tab_id,
            )
            if second_effect != MOVE_CHANGED or landed_tab != evidence.tab_id:
                return self._result_after_failure(
                    before,
                    effect=(
                        MOVE_CHANGED
                        if second_effect == MOVE_CHANGED
                        else MOVE_UNKNOWN
                    ),
                    detail="Herdr did not prove the pane returned to the original tab",
                )
        elif evidence.current_order != target.order:
            first = slots[evidence.current_order[0]]
            second = slots[evidence.current_order[1]]
            swap_effect = self._swap(
                (
                    "pane",
                    "swap",
                    "--source-pane",
                    first.pane_id,
                    "--target-pane",
                    second.pane_id,
                )
            )
            if swap_effect != MOVE_CHANGED:
                return self._result_after_failure(
                    before,
                    effect=swap_effect,
                    detail="Herdr did not prove the pane swap effect",
                )
            changed = True

        after_order = self._observe(workspace_id, lane_id)
        if (
            after_order.status == PLAN_REFUSED
            or after_order.evidence is None
            or after_order.target != target
            or not self._same_opening_authority(evidence, after_order.evidence)
        ):
            return self._result_after_failure(
                before,
                effect=MOVE_CHANGED if changed else MOVE_UNCHANGED,
                detail="the pair authority could not be re-established after the operation",
                reason=REASON_POSTCONDITION_FAILED,
            )
        if (
            after_order.evidence.split.direction != target.split
            or after_order.evidence.current_order != target.order
        ):
            return self._result_after_failure(
                before,
                effect=MOVE_CHANGED if changed else MOVE_UNCHANGED,
                detail="the measured split or order does not match the target",
                reason=REASON_POSTCONDITION_FAILED,
            )

        if "resize_ratio" in after_order.operations:
            outcome, detail, ratio_effect = self._apply_ratio_guarded(
                opening=evidence,
                target=target,
            )
            if outcome not in {RATIO_MATCHED, RATIO_APPLIED}:
                return self._result_after_failure(
                    before,
                    effect=(
                        MOVE_CHANGED
                        if changed or ratio_effect == MOVE_CHANGED
                        else ratio_effect
                    ),
                    detail=detail,
                )
            changed = changed or ratio_effect == MOVE_CHANGED

        final = self._observe(workspace_id, lane_id)
        if (
            final.status != PLAN_MATCHED
            or final.evidence is None
            or final.target != target
            or not self._same_opening_authority(evidence, final.evidence)
        ):
            return self._result_after_failure(
                before,
                effect=MOVE_CHANGED if changed else MOVE_UNCHANGED,
                detail="final live measurement does not match the effective placement",
                reason=REASON_POSTCONDITION_FAILED,
            )
        return PlacementApplyResult(APPLY_APPLIED if changed else APPLY_MATCHED, REASON_OK, "live placement was measured after apply", before, final)


def production_live_pair_placement() -> HerdrLivePairPlacement:
    resolution = resolve_herdr_binary(os.environ)
    return HerdrLivePairPlacement(resolution.path)


__all__ = (
    "APPLY_APPLIED",
    "APPLY_FAILED",
    "APPLY_MATCHED",
    "APPLY_PARTIAL",
    "APPLY_REFUSED",
    "HerdrLivePairPlacement",
    "PairEvidence",
    "PlacementApplyResult",
    "PlacementPlan",
    "PlacementTarget",
    "decide_plan",
    "production_live_pair_placement",
)
