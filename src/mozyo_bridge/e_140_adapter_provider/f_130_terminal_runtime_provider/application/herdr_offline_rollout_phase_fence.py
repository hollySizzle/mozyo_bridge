"""Fresh three-state effect fence for offline rollout resume (#15227).

The durable phase prefix says what was attempted; it is not a reusable observation of
the host.  Every post-``consumer_zero`` effect re-reads the full Herdr inventory and
joins it to the original close pins and the pre-sealed restore intent.  A restore group
has exactly three outcomes: absent and launchable, completed by its expected startup
action and foldable, or blocked residual.  There is no repair/backfill branch here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_inventory_identity import terminal_inventory_complete
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.herdr_launch_generation import (
    GENERATION_ATTESTED,
    HerdrLaunchGenerationStore,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    PHASE_HEALTH_CHECK,
    PHASE_LAUNCHING,
    PHASE_PLANNED,
    PHASE_SUCCESS_OWED,
    StartupTransactionFence,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_close_authority import (  # noqa: E501
    decode_close_authority,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_legacy_absence_authority import (  # noqa: E501
    decode_legacy_absence_authority,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_restore_intent import (  # noqa: E501
    REMAINING_RESTORE_PHASE,
    RESTORE_PHASES,
    TOP_RESTORE_PHASE,
    OfflineRolloutRestoreGroup,
    decode_restore_intent,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_pane_intent import (  # noqa: E501
    decode_pane_intent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_restore_verification import (  # noqa: E501
    verify_restored_names,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_close import (  # noqa: E501
    original_pin_positive_absence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_legacy_absence import (  # noqa: E501
    legacy_pin_positive_absence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_inventory import (  # noqa: E501
    agent_pane_rows_exact,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    parse_pane_bound_receipt,
)


GROUP_LAUNCH = "launch"
GROUP_FOLDED = "folded"
_STATE_ABSENT = "absent"
_STATE_RESTORED = "restored"
_STATE_RESIDUAL = "residual"

PRE_RESTORE_EFFECT_PHASES = frozenset(
    {
        "verified_backup",
        "migrate_attestation",
        "migrate_lane_lifecycle",
        "migrate_startup_transaction",
        "rebuild_launch_generation",
        "exact_runtime_install",
        "legacy_lane_epoch_adoption",
    }
)
POST_RESTORE_EFFECT_PHASES = frozenset(
    {"supervisor_pair_install", "supervisor_pair_readback", "final_verify"}
)


def supervisor_positive_stopped(*, home: Path, action: Mapping[str, object]) -> bool:
    """Fresh exact scheduler absence for every pre-restore mutation edge."""

    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
        supervisor_service_backend,
    )

    planned = action.get("plan", {}).get("supervisors", ())
    if not isinstance(planned, list) or len(planned) != 1:
        return False
    expected = planned[0]
    if not isinstance(expected, Mapping):
        return False
    status = supervisor_service_backend.service_status(mozyo_home=home)
    if not isinstance(status, Mapping) or status.get("backend") != expected.get(
        "backend"
    ):
        return False
    rows = status.get("agents")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        return False
    row = rows[0]
    if not (
        row.get("label") == expected.get("label")
        and row.get("installed") is False
        and row.get("loaded") is False
        and row.get("probe_state") == "confirmed_absent"
    ):
        return False
    if expected.get("backend") == "launchd":
        return row.get("plist_state") == "absent" and row.get("legacy_drain") == "absent"
    return (
        expected.get("backend") == "systemd_user"
        and row.get("service_unit_exists") is False
        and row.get("timer_unit_exists") is False
    )


def _ok(**receipt) -> PhaseExecutionResult:
    return PhaseExecutionResult(True, receipt=receipt)


def _fail(reason: str) -> PhaseExecutionResult:
    return PhaseExecutionResult(False, reason=reason)


@dataclass(frozen=True)
class RestoreGroupAdmission:
    ok: bool
    disposition: str = ""
    reason: str = ""


class OfflineRolloutPhaseFence:
    """Rejoin one action to a fresh exact inventory immediately before effects."""

    def __init__(
        self,
        *,
        home: Path,
        inventory_reader: Callable[[], object],
        pane_inventory_reader: Callable[[], object],
        supervisor_stopped_reader: Callable[[], bool] | None = None,
    ) -> None:
        self.home = Path(home)
        self.inventory_reader = inventory_reader
        self.pane_inventory_reader = pane_inventory_reader
        self.supervisor_stopped_reader = supervisor_stopped_reader

    def _snapshot(self):
        try:
            view = self.inventory_reader()
            panes = tuple(self.pane_inventory_reader())
        except Exception:  # noqa: BLE001 - unreadable is a typed zero-effect refusal
            return None
        if not terminal_inventory_complete(view) or tuple(view.unmanaged_agents):
            return None
        if not agent_pane_rows_exact(view.agents, panes):
            return None
        return view, panes

    def restore_pane_snapshot(self) -> dict[str, tuple[str, str, str]]:
        """Return one strict private pane identity set for an in-process restore guard."""
        snapshot = self._snapshot()
        if snapshot is None:
            raise ValueError("restore_partition_inventory_unreadable")
        _view, panes = snapshot
        return {
            row["pane_id"]: (
                row["workspace_id"],
                row["tab_id"],
                row["terminal_id"],
            )
            for row in panes
        }

    def observe_transient_pane(self, locator: str, terminal_id: str = "") -> str:
        """Pin a just-returned shell pane to its fresh full-inventory terminal."""
        snapshot = self._snapshot()
        if snapshot is None:
            raise ValueError("restore_partition_inventory_unreadable")
        _view, panes = snapshot
        matches = [row for row in panes if row["pane_id"] == locator]
        if len(matches) != 1:
            raise ValueError("restore_transient_pane_unreadable")
        row = matches[0]
        observed = row["terminal_id"]
        if terminal_id and terminal_id != observed:
            raise ValueError("restore_transient_pane_identity_changed")
        if row.get("agent") not in (None, ""):
            raise ValueError("restore_transient_pane_not_shell_only")
        return observed

    @staticmethod
    def _intent(action: Mapping[str, object]):
        return decode_restore_intent(
            action.get("private_bindings"), plan=action.get("plan", {})
        )

    @staticmethod
    def _authority(action: Mapping[str, object]):
        return decode_close_authority(
            action.get("private_bindings"), plan=action.get("plan", {})
        )

    @staticmethod
    def _legacy_authority(action: Mapping[str, object], intent):
        return decode_legacy_absence_authority(
            action.get("private_bindings"),
            plan=action.get("plan", {}),
            restore_intent=intent,
        )

    @staticmethod
    def _passive_panes(action: Mapping[str, object]):
        intent = decode_pane_intent(action.get("private_bindings"))
        return {
            row.locator: (row.workspace_id, row.tab_id, row.terminal_id)
            for row in intent.panes
        }

    @staticmethod
    def _foreign_overlap(actions, group: OfflineRolloutRestoreGroup) -> bool:
        return any(
            not startup.terminal
            and startup.action_id != group.expected_startup_action_id
            for startup in actions
        )

    def _original_absence_exact(
        self,
        *,
        action: Mapping[str, object],
        intent,
        view,
        panes,
        live_names: set[str],
        replacement_tokens: Mapping[str, str] | None = None,
    ) -> str:
        """Recheck destructive and legacy pins against one fresh full snapshot."""

        for pin in self._authority(action).pins:
            if not original_pin_positive_absence(
                home=self.home,
                view=view,
                pane_rows=panes,
                pin=pin,
                require_assigned_name_absent=pin.assigned_name not in live_names,
            ):
                return "original_close_pin_absence_unverified"
        group_by_name = {
            name: group
            for group in intent.groups
            for name in group.assigned_names
        }
        require_current = "rebuild_launch_generation" not in set(
            action.get("completed_phases", ())
        )
        allowed_tokens = replacement_tokens or {}
        for pin in self._legacy_authority(action, intent).pins:
            group = group_by_name.get(pin.assigned_name)
            if group is None or not legacy_pin_positive_absence(
                home=self.home,
                view=view,
                pane_rows=panes,
                pin=pin,
                expected_providers=group.providers,
                require_current_generation=require_current,
                require_assigned_name_absent=pin.assigned_name not in live_names,
                allowed_replacement_action_id=allowed_tokens.get(
                    pin.assigned_name, ""
                ),
            ):
                return "legacy_absence_authority_unverified"
        return ""

    def _group_state(
        self, view, group: OfflineRolloutRestoreGroup, actions
    ) -> str:
        live = {agent.name: agent for agent in view.managed_agents}
        group_live = [live.get(name) for name in group.assigned_names]
        startup = next(
            (
                row
                for row in actions
                if row.action_id == group.expected_startup_action_id
            ),
            None,
        )
        if startup is None:
            return _STATE_ABSENT if all(agent is None for agent in group_live) else _STATE_RESIDUAL
        unit = getattr(startup, "unit", None)
        participants = tuple(getattr(startup, "participants", ()) or ())
        if not (
            getattr(startup, "action_id", "") == group.expected_startup_action_id
            and getattr(startup, "phase", "") == PHASE_COMPLETED_SUCCESS
            and unit is not None
            and getattr(unit, "workspace_id", "") == group.workspace_id
            and getattr(unit, "lane_id", "") == group.lane_id
            and tuple(getattr(unit, "providers", ()) or ()) == tuple(sorted(group.providers))
            and len(participants) == len(group.agents)
            and {getattr(row, "role", "") for row in participants}
            == set(group.providers)
            and all(agent is not None for agent in group_live)
        ):
            return _STATE_RESIDUAL
        for expected in group.agents:
            agent = live.get(expected.assigned_name)
            participant = startup.participant_for(expected.provider)
            if agent is None or participant is None:
                return _STATE_RESIDUAL
            try:
                receipt = parse_pane_bound_receipt(
                    getattr(participant, "receipt", "")
                )
            except Exception:  # noqa: BLE001 - malformed receipt is residual
                return _STATE_RESIDUAL
            if not (
                (agent.workspace_id, agent.lane_id, agent.role, agent.name)
                == (
                    expected.workspace_id,
                    expected.lane_id,
                    expected.provider,
                    expected.assigned_name,
                )
                and getattr(participant, "closed", True) is False
                and getattr(participant, "assigned_name", "") == expected.assigned_name
                and getattr(participant, "locator", "") == agent.locator
                and receipt is not None
                and receipt.native_name == native_name_for(expected.assigned_name)
                and receipt.terminal_id == agent.terminal_id
            ):
                return _STATE_RESIDUAL
        expected_tokens = {
            name: group.expected_startup_action_id for name in group.assigned_names
        }
        identities = {agent.assigned_name: agent for agent in group.agents}
        try:
            verified, _name = verify_restored_names(
                view=view,
                names=group.assigned_names,
                home=self.home,
                expected_identities=identities,
                expected_tokens=expected_tokens,
            )
        except Exception:  # noqa: BLE001 - every store read is fail-closed here
            verified = False
        return _STATE_RESTORED if verified else _STATE_RESIDUAL

    def _partition(
        self,
        action: Mapping[str, object],
        *,
        restored_groups: set[int],
        absent_groups: set[int],
        flexible_group: int | None = None,
    ) -> tuple[PhaseExecutionResult, str]:
        snapshot = self._snapshot()
        if snapshot is None:
            return _fail("restore_partition_inventory_unreadable"), ""
        view, panes = snapshot
        try:
            actions = StartupTransactionFence(home=self.home).read_snapshot()
        except Exception:  # noqa: BLE001 - unreadable startup authority is residual
            return _fail("restore_action_residual"), ""
        if any(not startup.terminal for startup in actions):
            return _fail("restore_action_residual"), ""
        intent = self._intent(action)
        if restored_groups | absent_groups | ({flexible_group} if flexible_group is not None else set()) != set(range(len(intent.groups))):
            return _fail("restore_partition_contract_invalid"), ""
        if restored_groups & absent_groups:
            return _fail("restore_partition_contract_invalid"), ""
        states = [self._group_state(view, group, actions) for group in intent.groups]
        if any(states[index] != _STATE_RESTORED for index in restored_groups):
            return _fail("restore_partition_drift"), ""
        if any(states[index] != _STATE_ABSENT for index in absent_groups):
            return _fail("restore_action_residual"), ""
        flexible_state = states[flexible_group] if flexible_group is not None else ""
        if flexible_group is not None and flexible_state not in {_STATE_ABSENT, _STATE_RESTORED}:
            return _fail("restore_action_residual"), ""
        effective_restored = set(restored_groups)
        if flexible_group is not None and flexible_state == _STATE_RESTORED:
            effective_restored.add(flexible_group)
        expected_live = {
            name
            for index in effective_restored
            for name in intent.groups[index].assigned_names
        }
        if {agent.name for agent in view.managed_agents} != expected_live:
            return _fail("restore_partition_drift"), ""
        by_locator = {row["pane_id"]: row for row in panes}
        expected_panes = {
            (locator, *identity)
            for locator, identity in self._passive_panes(action).items()
        } | {
            (
                agent.locator,
                by_locator[agent.locator]["workspace_id"],
                by_locator[agent.locator]["tab_id"],
                agent.terminal_id,
            )
            for agent in view.managed_agents
        }
        if {
            (
                row["pane_id"],
                row["workspace_id"],
                row["tab_id"],
                row["terminal_id"],
            )
            for row in panes
        } != expected_panes:
            return _fail("restore_partition_drift"), ""

        absence_error = self._original_absence_exact(
            action=action,
            intent=intent,
            view=view,
            panes=panes,
            live_names=expected_live,
            replacement_tokens={
                name: intent.groups[index].expected_startup_action_id
                for index in effective_restored
                for name in intent.groups[index].assigned_names
            },
        )
        if absence_error:
            return _fail(absence_error), ""
        disposition = (
            GROUP_FOLDED if flexible_state == _STATE_RESTORED else GROUP_LAUNCH
        ) if flexible_group is not None else ""
        return _ok(restore_partition_exact=True), disposition

    def require_pre_restore(self, action: Mapping[str, object]) -> PhaseExecutionResult:
        intent = self._intent(action)
        result, _ = self._partition(
            action,
            restored_groups=set(),
            absent_groups=set(range(len(intent.groups))),
        )
        if not result.ok:
            return result
        stopped = self._require_supervisor_stopped()
        return result if stopped.ok else stopped

    def _require_supervisor_stopped(self) -> PhaseExecutionResult:
        if self.supervisor_stopped_reader is None:
            return _ok(supervisor_fence_not_configured=True)
        try:
            stopped = self.supervisor_stopped_reader()
        except Exception:  # noqa: BLE001
            stopped = False
        return (
            _ok(supervisor_stopped=True)
            if stopped is True
            else _fail("supervisor_stop_drift")
        )

    def require_post_restore(self, action: Mapping[str, object]) -> PhaseExecutionResult:
        intent = self._intent(action)
        result, _ = self._partition(
            action,
            restored_groups=set(range(len(intent.groups))),
            absent_groups=set(),
        )
        return result

    def before_effect(
        self, action: Mapping[str, object], phase_name: str
    ) -> PhaseExecutionResult:
        if phase_name in PRE_RESTORE_EFFECT_PHASES:
            return self.require_pre_restore(action)
        if phase_name in POST_RESTORE_EFFECT_PHASES:
            restored = self.require_post_restore(action)
            if not restored.ok or phase_name != "supervisor_pair_install":
                return restored
            return self._require_supervisor_stopped()
        return _ok(effect_fence_not_applicable=True)

    def before_restore_group(
        self,
        action: Mapping[str, object],
        *,
        phase_name: str,
        group_index: int,
    ) -> RestoreGroupAdmission:
        intent = self._intent(action)
        if phase_name not in RESTORE_PHASES or not 0 <= group_index < len(intent.groups):
            return RestoreGroupAdmission(False, reason="restore_group_selector_invalid")
        selected = intent.groups[group_index]
        if selected.phase != phase_name:
            return RestoreGroupAdmission(False, reason="restore_group_selector_invalid")
        phase_order = RESTORE_PHASES.index(phase_name)
        restored = {
            index
            for index, group in enumerate(intent.groups)
            if RESTORE_PHASES.index(group.phase) < phase_order
            or (group.phase == phase_name and index < group_index)
        }
        absent = set(range(len(intent.groups))) - restored - {group_index}
        result, disposition = self._partition(
            action,
            restored_groups=restored,
            absent_groups=absent,
            flexible_group=group_index,
        )
        if not result.ok:
            return RestoreGroupAdmission(False, reason=result.reason)
        stopped = self._require_supervisor_stopped()
        return RestoreGroupAdmission(
            stopped.ok,
            disposition=disposition if stopped.ok else "",
            reason=stopped.reason,
        )

    def require_restore_effect(
        self,
        action: Mapping[str, object],
        *,
        phase_name: str,
        group_index: int,
        baseline_panes: Mapping[str, tuple[str, str, str]],
        transient_panes: Mapping[str, tuple[str, str, str]],
    ) -> PhaseExecutionResult:
        """Admit one in-process restore effect without accepting a foreign action.

        Before reserve the selected group must be wholly absent.  After this invocation's
        sealed reserve, only its exact pending action and pane-bound participants may be in
        flight.  A prepared shell pane is accepted solely when its locator and terminal are
        the immutable participant receipt; every other pane is residual.
        """
        intent = self._intent(action)
        if phase_name not in RESTORE_PHASES or not 0 <= group_index < len(intent.groups):
            return _fail("restore_group_selector_invalid")
        selected = intent.groups[group_index]
        if selected.phase != phase_name:
            return _fail("restore_group_selector_invalid")
        snapshot = self._snapshot()
        if snapshot is None:
            return _fail("restore_partition_inventory_unreadable")
        view, panes = snapshot
        try:
            actions = StartupTransactionFence(home=self.home).read_snapshot()
        except Exception:  # noqa: BLE001 - malformed authority is a zero-effect refusal
            return _fail("restore_action_residual")
        startup = next(
            (row for row in actions if row.action_id == selected.expected_startup_action_id),
            None,
        )
        passive_panes = self._passive_panes(action)
        if startup is None:
            admission = self.before_restore_group(
                action, phase_name=phase_name, group_index=group_index
            )
            return (
                _ok(restore_effect_exact=True)
                if admission.ok
                else _fail(admission.reason)
            )
        if self._foreign_overlap(actions, selected):
            return _fail("restore_action_residual")
        allowed_phases = {
            PHASE_PLANNED,
            PHASE_LAUNCHING,
            PHASE_HEALTH_CHECK,
            PHASE_SUCCESS_OWED,
            PHASE_COMPLETED_SUCCESS,
        }
        unit = startup.unit
        if not (
            startup.phase in allowed_phases
            and unit.workspace_id == selected.workspace_id
            and unit.lane_id == selected.lane_id
            and unit.providers == tuple(sorted(selected.providers))
        ):
            return _fail("restore_action_residual")
        phase_order = RESTORE_PHASES.index(phase_name)
        restored = {
            index
            for index, group in enumerate(intent.groups)
            if RESTORE_PHASES.index(group.phase) < phase_order
            or (group.phase == phase_name and index < group_index)
        }
        absent = set(range(len(intent.groups))) - restored - {group_index}
        states = [self._group_state(view, group, actions) for group in intent.groups]
        if any(states[index] != _STATE_RESTORED for index in restored):
            return _fail("restore_partition_drift")
        if any(states[index] != _STATE_ABSENT for index in absent):
            return _fail("restore_action_residual")

        participants = tuple(startup.participants)
        if (
            (startup.phase == PHASE_PLANNED and participants)
            or (startup.phase == PHASE_LAUNCHING and not participants)
            or (
                startup.phase
                in {PHASE_HEALTH_CHECK, PHASE_SUCCESS_OWED, PHASE_COMPLETED_SUCCESS}
                and len(participants) != len(selected.agents)
            )
        ):
            return _fail("restore_action_residual")
        expected_by_role = {row.provider: row for row in selected.agents}
        if (
            len(expected_by_role) != len(selected.agents)
            or len({row.role for row in participants}) != len(participants)
            or any(row.role not in expected_by_role for row in participants)
        ):
            return _fail("restore_action_residual")
        live = {agent.name: agent for agent in view.managed_agents}
        pane_by_locator = {row["pane_id"]: row for row in panes}
        participant_panes: set[tuple[str, str, str, str]] = set()
        selected_live: set[str] = set()
        for participant in participants:
            expected = expected_by_role[participant.role]
            try:
                receipt = parse_pane_bound_receipt(participant.receipt)
            except Exception:  # noqa: BLE001 - malformed private receipt is residual
                return _fail("restore_action_residual")
            pane = pane_by_locator.get(participant.locator)
            if not (
                participant.closed is False
                and participant.assigned_name == expected.assigned_name
                and receipt is not None
                and receipt.native_name == native_name_for(expected.assigned_name)
                and bool(receipt.terminal_id)
                and pane is not None
                and pane["terminal_id"] == receipt.terminal_id
            ):
                return _fail("restore_action_residual")
            participant_panes.add(
                (
                    participant.locator,
                    pane["workspace_id"],
                    pane["tab_id"],
                    receipt.terminal_id,
                )
            )
            agent = live.get(expected.assigned_name)
            if agent is None:
                if (
                    pane.get("agent") not in (None, "")
                    or pane["workspace_id"] != receipt.workspace_id
                    or pane["tab_id"] != receipt.tab_id
                ):
                    return _fail("restore_action_residual")
                continue
            if not (
                (agent.workspace_id, agent.lane_id, agent.role, agent.locator, agent.terminal_id)
                == (
                    expected.workspace_id,
                    expected.lane_id,
                    expected.provider,
                    participant.locator,
                    receipt.terminal_id,
                )
            ):
                return _fail("restore_action_residual")
            if startup.phase != PHASE_COMPLETED_SUCCESS and (
                pane["workspace_id"] != receipt.workspace_id
                or pane["tab_id"] != receipt.tab_id
            ):
                return _fail("restore_action_residual")
            selected_live.add(agent.name)

        restored_names = {
            name for index in restored for name in intent.groups[index].assigned_names
        }
        if set(live) != restored_names | selected_live:
            return _fail("restore_partition_drift")
        participant_and_restored_panes = {
            (
                agent.locator,
                pane_by_locator[agent.locator]["workspace_id"],
                pane_by_locator[agent.locator]["tab_id"],
                agent.terminal_id,
            )
            for name, agent in live.items()
            if name in restored_names
        } | participant_panes
        allowed_panes = {
            (locator, *identity) for locator, identity in baseline_panes.items()
        } | {(locator, *identity) for locator, identity in transient_panes.items()}
        if set(baseline_panes) & set(transient_panes):
            return _fail("restore_partition_contract_invalid")
        if {
            (
                row["pane_id"],
                row["workspace_id"],
                row["tab_id"],
                row["terminal_id"],
            )
            for row in panes
        } != allowed_panes | participant_and_restored_panes:
            return _fail("restore_partition_drift")
        expected_baseline = {
            (locator, *identity) for locator, identity in passive_panes.items()
        } | {
            (
                agent.locator,
                pane_by_locator[agent.locator]["workspace_id"],
                pane_by_locator[agent.locator]["tab_id"],
                agent.terminal_id,
            )
            for name, agent in live.items()
            if name in restored_names
        }
        if {
            (locator, *identity) for locator, identity in baseline_panes.items()
        } != expected_baseline:
            return _fail("restore_partition_drift")
        absence_error = self._original_absence_exact(
            action=action,
            intent=intent,
            view=view,
            panes=panes,
            live_names=restored_names | selected_live,
            replacement_tokens={
                name: group.expected_startup_action_id
                for index, group in enumerate(intent.groups)
                if index in restored | {group_index}
                for name in group.assigned_names
            },
        )
        if absence_error:
            return _fail(absence_error)
        if startup.phase == PHASE_COMPLETED_SUCCESS and states[group_index] != _STATE_RESTORED:
            return _fail("restore_action_residual")
        stopped = self._require_supervisor_stopped()
        return _ok(restore_effect_exact=True) if stopped.ok else stopped

    def require_restore_completion(
        self,
        action: Mapping[str, object],
        *,
        phase_name: str,
        group_index: int,
        baseline_panes: Mapping[str, tuple[str, str, str]],
        transient_panes: Mapping[str, tuple[str, str, str]],
    ) -> PhaseExecutionResult:
        """Require live participants and finalized generation-v2 before success."""

        effect = self.require_restore_effect(
            action,
            phase_name=phase_name,
            group_index=group_index,
            baseline_panes=baseline_panes,
            transient_panes=transient_panes,
        )
        if not effect.ok or transient_panes:
            return effect if not effect.ok else _fail("restore_action_residual")
        intent = self._intent(action)
        group = intent.groups[group_index]
        snapshot = self._snapshot()
        if snapshot is None:
            return _fail("restore_partition_inventory_unreadable")
        view, _panes = snapshot
        live = {agent.name: agent for agent in view.managed_agents}
        try:
            generations = HerdrLaunchGenerationStore(home=self.home)
            attestations = HerdrIdentityAttestationStore(home=self.home)
            for expected in group.agents:
                observed = live.get(expected.assigned_name)
                generation = generations.read(expected.assigned_name)
                attestation = attestations.read(expected.assigned_name)
                joined = (
                    evaluate_attestation(
                        attestation,
                        live_locator=observed.locator,
                        live_terminal_id=observed.terminal_id,
                        expected_workspace_id=expected.workspace_id,
                        expected_role=expected.provider,
                        expected_lane=expected.lane_id,
                    )
                    if observed is not None
                    else None
                )
                if (
                    observed is None
                    or attestation is None
                    or attestation.schema_version
                    != HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
                    or joined is None
                    or not joined.ok
                ):
                    return _fail("restore_attestation_unverified")
                if generation is None or not (
                    generation.phase == GENERATION_ATTESTED
                    and generation.startup_action_id
                    == group.expected_startup_action_id
                    and generation.assigned_name == expected.assigned_name
                    and generation.workspace_id == expected.workspace_id
                    and generation.role == expected.provider
                    and generation.lane_id == expected.lane_id
                    and generation.locator == observed.locator
                    and generation.terminal_id == observed.terminal_id
                    and generation.verdict == "present"
                ):
                    return _fail("restore_generation_unfinalized")
        except Exception:  # noqa: BLE001 - unreadable generation never completes
            return _fail("restore_generation_unfinalized")
        return _ok(restore_completion_exact=True)

    def after_restore_group(
        self,
        action: Mapping[str, object],
        *,
        phase_name: str,
        group_index: int,
    ) -> PhaseExecutionResult:
        intent = self._intent(action)
        phase_order = RESTORE_PHASES.index(phase_name)
        restored = {
            index
            for index, group in enumerate(intent.groups)
            if RESTORE_PHASES.index(group.phase) < phase_order
            or (group.phase == phase_name and index <= group_index)
        }
        absent = set(range(len(intent.groups))) - restored
        result, _ = self._partition(
            action, restored_groups=restored, absent_groups=absent
        )
        if not result.ok:
            return result
        return self._require_supervisor_stopped()

    def require_restore_phase_entry(
        self, action: Mapping[str, object], *, phase_name: str
    ) -> PhaseExecutionResult:
        """Verify a zero-group restore phase, or the partition before its first group."""

        intent = self._intent(action)
        indexes = [
            index for index, group in enumerate(intent.groups) if group.phase == phase_name
        ]
        if indexes:
            admission = self.before_restore_group(
                action, phase_name=phase_name, group_index=indexes[0]
            )
            return _ok(restore_partition_exact=True) if admission.ok else _fail(admission.reason)
        phase_order = RESTORE_PHASES.index(phase_name)
        restored = {
            index
            for index, group in enumerate(intent.groups)
            if RESTORE_PHASES.index(group.phase) < phase_order
        }
        absent = set(range(len(intent.groups))) - restored
        result, _ = self._partition(
            action, restored_groups=restored, absent_groups=absent
        )
        return result


__all__ = (
    "GROUP_FOLDED",
    "GROUP_LAUNCH",
    "OfflineRolloutPhaseFence",
    "POST_RESTORE_EFFECT_PHASES",
    "PRE_RESTORE_EFFECT_PHASES",
    "RestoreGroupAdmission",
    "supervisor_positive_stopped",
)
