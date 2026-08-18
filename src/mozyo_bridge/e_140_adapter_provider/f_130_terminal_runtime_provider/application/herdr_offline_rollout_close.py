"""Generation-bound capture and close execution for offline rollout (#15227).

Every destructive close is licensed by the same fresh full-inventory + current
attestation + completed generation-v2 join used by current runtime retire rails.
The server-owned terminal id remains in memory and in its canonical stores; the
sealed offline action persists only the non-secret startup action token.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    evaluate_attestation,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
)
from mozyo_bridge.core.state.herdr_inventory_identity import (
    terminal_inventory_complete,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    HerdrLaunchGenerationStore,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_close_authority import (  # noqa: E501
    CLOSE_AUTHORITY_VERSION,
    OfflineRolloutCloseAuthority,
    OfflineRolloutClosePin,
    decode_close_authority,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    verified_terminal_generation_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_inventory import (  # noqa: E501
    agent_pane_rows_exact,
)


STATE_PRESENT = "present"
STATE_ABSENT = "absent"


def _ok(**receipt) -> PhaseExecutionResult:
    return PhaseExecutionResult(True, receipt=receipt)


def _fail(reason: str, detail: str = "") -> PhaseExecutionResult:
    return PhaseExecutionResult(False, reason=reason, detail=detail[:1000])


def _identity_exact(agent, pin: OfflineRolloutClosePin) -> bool:
    return bool(
        agent.name == pin.assigned_name
        and agent.workspace_id == pin.workspace_id
        and agent.lane_id == pin.lane_id
        and agent.role == pin.role
        and agent.locator == pin.locator
    )


def _attestation_is_current(record: object) -> bool:
    return bool(
        record is not None
        and type(getattr(record, "schema_version", None)) is int
        and getattr(record, "schema_version")
        == HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
    )


def _present_pin_join(
    *,
    home: Path,
    agent,
    pin: OfflineRolloutClosePin,
) -> bool:
    if not _identity_exact(agent, pin):
        return False
    terminal_id = getattr(agent, "terminal_id", "")
    try:
        attestation = HerdrIdentityAttestationStore(home=home).read(
            pin.assigned_name
        )
    except Exception:  # noqa: BLE001 - unreadable authority is never close authority
        return False
    joined = evaluate_attestation(
        attestation,
        live_locator=pin.locator,
        live_terminal_id=terminal_id,
        expected_workspace_id=pin.workspace_id,
        expected_role=pin.role,
        expected_lane=pin.lane_id,
    )
    token = verified_terminal_generation_token(
        home,
        assigned_name=pin.assigned_name,
        workspace_id=pin.workspace_id,
        role=pin.role,
        lane_id=pin.lane_id,
        locator=pin.locator,
        terminal_id=terminal_id,
    )
    return bool(
        _attestation_is_current(attestation)
        and joined.ok
        and token == pin.startup_action_id
    )


def _positive_absence(
    *,
    home: Path,
    view,
    pane_rows,
    pin: OfflineRolloutClosePin,
    require_assigned_name_absent: bool = True,
) -> bool:
    """Prove the terminal licensed by ``pin`` is absent from one fresh snapshot."""

    try:
        generation = HerdrLaunchGenerationStore(home=home).read(pin.assigned_name)
        attestation = HerdrIdentityAttestationStore(home=home).read(
            pin.assigned_name
        )
    except Exception:  # noqa: BLE001 - missing/unreadable stores grant no absence
        return False
    terminal_id = getattr(generation, "terminal_id", "")
    token = ""
    pinned_action_exact = False
    if generation is not None:
        token = verified_terminal_generation_token(
            home,
            assigned_name=pin.assigned_name,
            workspace_id=pin.workspace_id,
            role=pin.role,
            lane_id=pin.lane_id,
            locator=pin.locator,
            terminal_id=terminal_id,
        )
    if token != pin.startup_action_id:
        # The rollout deliberately rebuilds the launch-generation cache before restore.
        # Once that cache is an exact empty v2 store, the immutable startup transaction
        # remains the only durable source for the original terminal id.  Read its strict
        # pane-bound receipt in memory; the terminal id is never copied into the action.
        try:
            from mozyo_bridge.core.state.herdr_native_identity_binding import (
                native_name_for,
            )
            from mozyo_bridge.core.state.startup_transaction_fence import (
                PHASE_COMPLETED_SUCCESS,
                StartupTransactionFence,
            )
            from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
                parse_pane_bound_receipt,
            )

            action = StartupTransactionFence(home=home).read(pin.startup_action_id)
            participant = action.participant_for(pin.role) if action is not None else None
            receipt = parse_pane_bound_receipt(
                getattr(participant, "receipt", "") if participant is not None else ""
            )
            unit = getattr(action, "unit", None)
            exact_action = bool(
                action is not None
                and getattr(action, "phase", "") == PHASE_COMPLETED_SUCCESS
                and unit is not None
                and getattr(unit, "workspace_id", "") == pin.workspace_id
                and getattr(unit, "lane_id", "") == pin.lane_id
                and pin.role in tuple(getattr(unit, "providers", ()) or ())
                and participant is not None
                and getattr(participant, "closed", True) is False
                and getattr(participant, "assigned_name", "") == pin.assigned_name
                and getattr(participant, "locator", "") == pin.locator
                and receipt is not None
                and receipt.native_name == native_name_for(pin.assigned_name)
                and bool(receipt.terminal_id)
            )
            if not exact_action:
                return False
            terminal_id = receipt.terminal_id
            token = pin.startup_action_id
            pinned_action_exact = True
        except Exception:  # noqa: BLE001 - malformed/unreadable receipt grants no absence
            return False
    joined = evaluate_attestation(
        attestation,
        live_locator=pin.locator,
        live_terminal_id=terminal_id,
        expected_workspace_id=pin.workspace_id,
        expected_role=pin.role,
        expected_lane=pin.lane_id,
    )
    return bool(
        (
            (_attestation_is_current(attestation) and joined.ok)
            or (not require_assigned_name_absent and pinned_action_exact)
        )
        and token == pin.startup_action_id
        and not any(
            (require_assigned_name_absent and agent.name == pin.assigned_name)
            or agent.locator == pin.locator
            or agent.terminal_id == terminal_id
            for agent in view.agents
        )
        and not any(
            row.get("pane_id") == pin.locator
            or row.get("terminal_id") == terminal_id
            for row in pane_rows
        )
    )


def original_pin_positive_absence(
    *, home: Path, view, pane_rows, pin: OfflineRolloutClosePin,
    require_assigned_name_absent: bool = True,
) -> bool:
    """Public sibling seam for fresh pre-/mid-restore absence fencing."""

    return _positive_absence(
        home=home,
        view=view,
        pane_rows=pane_rows,
        pin=pin,
        require_assigned_name_absent=require_assigned_name_absent,
    )


def capture_close_authority(
    *,
    view,
    plan: Mapping[str, object],
    home: Path,
) -> PhaseExecutionResult:
    """Capture v2 pins for every live plan target from one full inventory."""

    if not terminal_inventory_complete(view) or view.unmanaged_agents:
        return _fail("close_authority_inventory_unreadable")
    planned = {
        row.get("assigned_name"): row
        for row in plan.get("agents", ())
        if isinstance(row, Mapping)
    }
    if (
        len(planned) != len(plan.get("agents", ()))
        or {agent.name for agent in view.managed_agents} != set(planned)
    ):
        return _fail("close_authority_agent_set_drift")
    pins = []
    for agent in sorted(view.managed_agents, key=lambda item: item.name):
        expected = planned.get(agent.name)
        if expected is None:
            return _fail("close_authority_agent_set_drift")
        identity_matches = all(
            (
                agent.workspace_id == expected.get("workspace_id"),
                agent.lane_id == expected.get("lane_id"),
                agent.role == expected.get("provider"),
            )
        )
        if not identity_matches:
            return _fail("close_authority_agent_set_drift", agent.name)
        try:
            attestation = HerdrIdentityAttestationStore(home=home).read(agent.name)
        except Exception:  # noqa: BLE001 - old/unreadable store is intentional zero-close
            return _fail("close_authority_generation_unverified", agent.name)
        joined = evaluate_attestation(
            attestation,
            live_locator=agent.locator,
            live_terminal_id=agent.terminal_id,
            expected_workspace_id=agent.workspace_id,
            expected_role=agent.role,
            expected_lane=agent.lane_id,
        )
        token = verified_terminal_generation_token(
            home,
            assigned_name=agent.name,
            workspace_id=agent.workspace_id,
            role=agent.role,
            lane_id=agent.lane_id,
            locator=agent.locator,
            terminal_id=agent.terminal_id,
        )
        if not (_attestation_is_current(attestation) and joined.ok and token):
            return _fail("close_authority_generation_unverified", agent.name)
        pins.append(
            OfflineRolloutClosePin(
                workspace_id=agent.workspace_id,
                lane_id=agent.lane_id,
                role=agent.role,
                assigned_name=agent.name,
                locator=agent.locator,
                startup_action_id=token,
            )
        )
    authority = OfflineRolloutCloseAuthority(tuple(pins))
    return _ok(close_authority=authority.as_payload())


@dataclass(frozen=True)
class _Snapshot:
    states: Mapping[str, str] = field(repr=False)
    agents: Mapping[str, object] = field(repr=False)


class OfflineRolloutCloseExecutor:
    """Execute stop phases against exact private pins, never locator identity alone."""

    def __init__(
        self,
        *,
        home: Path,
        env: Mapping[str, str],
        inventory_reader: Callable[[], object],
        pane_inventory_reader: Callable[[], object],
        workspace_paths: Mapping[str, str],
        settle_timeout: float = 600.0,
        poll_interval: float = 2.0,
    ) -> None:
        self.home = Path(home)
        self.env = dict(env)
        self.inventory_reader = inventory_reader
        self.pane_inventory_reader = pane_inventory_reader
        self.workspace_paths = dict(workspace_paths)
        self.settle_timeout = settle_timeout
        self.poll_interval = poll_interval

    @staticmethod
    def authority(action: Mapping[str, object]) -> OfflineRolloutCloseAuthority:
        return decode_close_authority(
            action.get("private_bindings"), plan=action.get("plan", {})
        )

    @staticmethod
    def _completed_stop_names(action: Mapping[str, object]) -> set[str]:
        completed = set(action.get("completed_phases", ()))
        return {
            name
            for phase in action.get("plan", {}).get("phase_order", ())
            if phase.get("phase") in completed
            and phase.get("phase")
            in {"non_top_workspace_stop", "top_workspace_stop"}
            for name in phase.get("assigned_names", ())
        }

    def _snapshot(
        self,
        authority: OfflineRolloutCloseAuthority,
        *,
        allowed_absent: set[str],
    ) -> PhaseExecutionResult | _Snapshot:
        try:
            view = self.inventory_reader()
            pane_rows = tuple(self.pane_inventory_reader())
        except Exception:  # noqa: BLE001 - unreadable inventory is never authority
            return _fail("close_authority_inventory_unreadable")
        if (
            not terminal_inventory_complete(view)
            or view.unmanaged_agents
            or not agent_pane_rows_exact(view.agents, pane_rows)
        ):
            return _fail("close_authority_inventory_unreadable")
        pins = authority.by_name()
        live = {agent.name: agent for agent in view.managed_agents}
        if len(live) != len(view.managed_agents) or not set(live).issubset(pins):
            return _fail("close_authority_agent_set_drift")
        states: dict[str, str] = {}
        for name, pin in pins.items():
            agent = live.get(name)
            if agent is not None:
                if not _present_pin_join(home=self.home, agent=agent, pin=pin):
                    return _fail("close_authority_generation_drift", name)
                states[name] = STATE_PRESENT
                continue
            if not _positive_absence(
                home=self.home, view=view, pane_rows=pane_rows, pin=pin
            ):
                return _fail("close_authority_absence_unverified", name)
            if name not in allowed_absent:
                return _fail("close_authority_target_absent", name)
            states[name] = STATE_ABSENT
        return _Snapshot(states=states, agents=live)

    def wait_for_settled(
        self,
        *,
        action: Mapping[str, object],
        names: tuple[str, ...],
        replaying: bool,
    ) -> PhaseExecutionResult:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
            LiveSessionRetireOps,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
            RUNTIME_AWAITING_INPUT,
            RUNTIME_TURN_ENDED,
        )

        authority = self.authority(action)
        pins = authority.by_name()
        if any(name not in pins for name in names):
            return _fail("close_authority_phase_target_mismatch")
        allowed = self._completed_stop_names(action)
        if replaying:
            allowed.update(names)
        deadline = time.monotonic() + self.settle_timeout
        while True:
            snapshot = self._snapshot(authority, allowed_absent=allowed)
            if isinstance(snapshot, PhaseExecutionResult):
                return snapshot
            pending = []
            for name in names:
                if snapshot.states[name] == STATE_ABSENT:
                    continue
                agent = snapshot.agents[name]
                if agent.runtime_state not in (
                    RUNTIME_AWAITING_INPUT,
                    RUNTIME_TURN_ENDED,
                ):
                    pending.append(name)
                    continue
                pin = pins[name]
                repo = self.workspace_paths.get(pin.workspace_id)
                if not repo:
                    return _fail("close_authority_workspace_unreadable", name)
                observer = LiveSessionRetireOps(repo_root=Path(repo), env=self.env)
                readable, has_pending = observer.observe_composer(pin.locator)
                if not readable or has_pending is not False:
                    pending.append(name)
            if not pending:
                return _ok(targets_settled=True)
            if time.monotonic() >= deadline:
                return _fail("agents_not_settled", ",".join(sorted(pending)))
            time.sleep(self.poll_interval)

    def close_names(
        self,
        *,
        action: Mapping[str, object],
        names: tuple[str, ...],
        replaying: bool,
    ) -> PhaseExecutionResult:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
            HerdrRetireClosePlan,
            execute_herdr_retire_close,
        )

        authority = self.authority(action)
        pins = authority.by_name()
        if any(name not in pins for name in names):
            return _fail("close_authority_phase_target_mismatch")
        allowed = self._completed_stop_names(action)
        if replaying:
            allowed.update(names)
        closed_now: set[str] = set()
        for name in names:
            snapshot = self._snapshot(
                authority, allowed_absent=allowed | closed_now
            )
            if isinstance(snapshot, PhaseExecutionResult):
                return snapshot
            if snapshot.states[name] == STATE_ABSENT:
                continue
            pin = pins[name]
            result = execute_herdr_retire_close(
                HerdrRetireClosePlan(
                    workspace_id=pin.workspace_id,
                    lane_id=pin.lane_id,
                    close_targets=((pin.role, pin.locator),),
                    foreign_names=(),
                ),
                env=self.env,
            )
            if result.failed:
                return _fail("agent_close_failed", name)
            closed_now.add(name)
            verified = self._snapshot(
                authority, allowed_absent=allowed | closed_now
            )
            if isinstance(verified, PhaseExecutionResult):
                return verified
            if verified.states[name] != STATE_ABSENT:
                return _fail("agent_stop_unverified", name)
        final = self._snapshot(
            authority, allowed_absent=allowed | set(names)
        )
        if isinstance(final, PhaseExecutionResult):
            return final
        if any(final.states[name] != STATE_ABSENT for name in names):
            return _fail("agent_stop_unverified")
        return _ok(stopped_assigned_names=sorted(names))


__all__ = (
    "CLOSE_AUTHORITY_VERSION",
    "OfflineRolloutCloseExecutor",
    "STATE_ABSENT",
    "STATE_PRESENT",
    "capture_close_authority",
    "original_pin_positive_absence",
)
