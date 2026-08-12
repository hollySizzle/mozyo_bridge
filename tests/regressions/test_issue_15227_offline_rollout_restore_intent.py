"""Replay and effect-edge matrix for sealed offline restore authority (#15227)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    PHASE_HEALTH_CHECK,
    PHASE_ROLLBACK_OWED,
    Participant,
    StartupTransactionError,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_offline_rollout_action import (  # noqa: E501
    PhaseExecutionResult,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_restore_intent import (  # noqa: E501
    build_restore_intent,
    decode_restore_intent,
    restore_phase_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
    HerdrObservedAgent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_executor import (  # noqa: E501
    LiveOfflineRolloutExecutionPort,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_phase_fence import (  # noqa: E501
    POST_RESTORE_EFFECT_PHASES,
    PRE_RESTORE_EFFECT_PHASES,
    OfflineRolloutPhaseFence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_restore import (  # noqa: E501
    OfflineRolloutRestoreExecutor,
    _RestoreEffectGuard,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_runner import (  # noqa: E501
    RUNNER_ENV,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    StartupTransaction,
    pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_completion import (  # noqa: E501
    finalize_session_launch_authority,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_identity import (  # noqa: E501
    PrivateWorktreeBinding,
    private_workspace_effect_fence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    _create_tab,
    _create_workspace,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_bound_launch import (  # noqa: E501
    split_prepared_pane,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_role_grouped_space import (  # noqa: E501
    _resolve_project_coordinator_workspace_under_lock,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from tests.support.current_launch_authority import seed_current_generation


_TOP = "mzb1_ws_codex_default"
_OTHER = "mzb1_other_claude_default"


def _agent(
    name: str = _TOP,
    *,
    workspace: str = "ws",
    lane: str = "default",
    role: str = "codex",
    locator: str = "w1:p1",
    terminal: str = "terminal:top",
) -> HerdrObservedAgent:
    return HerdrObservedAgent(
        name=name,
        managed=True,
        workspace_id=workspace,
        lane_id=lane,
        role=role,
        runtime_state="awaiting_input",
        locator=locator,
        terminal_id=terminal,
    )


def _view(*agents: HerdrObservedAgent, invalid: int = 0) -> HerdrInventoryView:
    return HerdrInventoryView(
        backend_selected=True,
        ok=True,
        workspace_segment="ws",
        agents=agents,
        raw_row_count=len(agents),
        invalid_row_count=invalid,
    )


def _plan(*, two_groups: bool = False) -> dict:
    agents = [
        {
            "assigned_name": _TOP,
            "workspace_id": "ws",
            "lane_id": "default",
            "provider": "codex",
        }
    ]
    remaining = []
    if two_groups:
        agents.append(
            {
                "assigned_name": _OTHER,
                "workspace_id": "other",
                "lane_id": "default",
                "provider": "claude",
            }
        )
        remaining.append(_OTHER)
    return {
        "agents": agents,
        "legacy_recoveries": [],
        "phase_order": [
            {"phase": "top_restore_action_bootstrap", "assigned_names": [_TOP]},
            {
                "phase": "remaining_workspace_restore",
                "assigned_names": remaining,
            },
        ],
    }


def _private(plan: dict) -> dict:
    counter = iter(range(1, 100))
    intent = build_restore_intent(
        plan, nonce_factory=lambda: f"{next(counter):032x}"
    )
    passive = []
    containers = []
    for index, group in enumerate(intent.groups, start=1):
        workspace = f"w{index}"
        pane = f"{workspace}:p0"
        tab = f"{workspace}:t1"
        terminal = f"terminal:root:{index}"
        passive.append(
            {
                "locator": pane,
                "workspace_id": workspace,
                "tab_id": tab,
                "terminal_id": terminal,
            }
        )
        containers.append(
            {
                "expected_startup_action_id": group.expected_startup_action_id,
                "logical_workspace_id": group.workspace_id,
                "lane_id": group.lane_id,
                "workspace_id": workspace,
                "tab_id": tab,
                "pane_locator": pane,
                "terminal_id": terminal,
            }
        )
    return {
        "agents": [
            {
                "assigned_name": row["assigned_name"],
                "workspace_id": row["workspace_id"],
                "lane_id": row["lane_id"],
                "provider": row["provider"],
            }
            for row in plan["agents"]
        ],
        "close_authority": {
            "version": 2,
            "pins": [
                {
                    "workspace_id": row["workspace_id"],
                    "lane_id": row["lane_id"],
                    "role": row["provider"],
                    "assigned_name": row["assigned_name"],
                    "locator": f"old:{index}",
                    "startup_action_id": "startup-" + f"{index:064x}",
                }
                for index, row in enumerate(
                    sorted(plan["agents"], key=lambda item: item["assigned_name"]),
                    start=1,
                )
            ],
        },
        "legacy_absence_authority": {"version": 1, "pins": []},
        "restore_intent": intent.as_payload(),
        "passive_pane_intent": {"version": 1, "panes": passive},
        "restore_container_intent": {"version": 1, "groups": containers},
        "workspace_paths": {"ws": "/repo/ws", "other": "/repo/other"},
        "legacy_recovery_worktree_paths": {},
        "target_cli": "/installed/mozyo-bridge",
        "provider_executable_bindings": {},
    }


def _action(*, two_groups: bool = False) -> dict:
    plan = _plan(two_groups=two_groups)
    return {"plan": plan, "private_bindings": _private(plan)}


def _pane_rows(action: dict, *agents: HerdrObservedAgent) -> tuple[dict, ...]:
    roots = [
        {
            "pane_id": row["locator"],
            "workspace_id": row["workspace_id"],
            "tab_id": row["tab_id"],
            "terminal_id": row["terminal_id"],
            "agent": "",
        }
        for row in action["private_bindings"]["passive_pane_intent"]["panes"]
    ]
    return tuple(
        roots
        + [
            {
                "pane_id": agent.locator,
                "workspace_id": agent.locator.split(":", 1)[0],
                "tab_id": f"{agent.locator.split(':', 1)[0]}:t1",
                "terminal_id": agent.terminal_id,
                "agent": agent.role,
            }
            for agent in agents
        ]
    )


def _passive_baseline(action: dict) -> dict[str, tuple[str, str, str]]:
    return {
        row["locator"]: (
            row["workspace_id"],
            row["tab_id"],
            row["terminal_id"],
        )
        for row in action["private_bindings"]["passive_pane_intent"]["panes"]
    }


def _seed_expected_group(home: Path, group, observed: HerdrObservedAgent) -> str:
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(
        StartupUnit(group.workspace_id, group.lane_id, group.providers),
        group.action_nonce,
    )
    for expected in group.agents:
        if expected.assigned_name != observed.name:
            raise AssertionError("fixture supports one participant per restore group")
        target_workspace = observed.locator.split(":", 1)[0]
        fence.record_participant(
            action.action_id,
            Participant(
                role=expected.provider,
                assigned_name=expected.assigned_name,
                locator=observed.locator,
                receipt=pane_bound_receipt(
                    target_workspace=target_workspace,
                    target_tab=f"{target_workspace}:t1",
                    native_name=native_name_for(expected.assigned_name),
                    terminal_id=observed.terminal_id,
                ),
            ),
        )
    fence.set_phase(action.action_id, PHASE_HEALTH_CHECK)
    fence.set_phase(action.action_id, PHASE_COMPLETED_SUCCESS)
    seed_current_generation(
        home,
        workspace_id=group.workspace_id,
        lane_id=group.lane_id,
        role=group.providers[0],
        assigned_name=group.assigned_names[0],
        locator=observed.locator,
        action_id=action.action_id,
        terminal_id=observed.terminal_id,
    )
    HerdrIdentityAttestationStore(home=home).upsert(
        IdentityAttestationRecord(
            assigned_name=observed.name,
            workspace_id=observed.workspace_id,
            role=observed.role,
            lane_id=observed.lane_id,
            locator=observed.locator,
            terminal_id=observed.terminal_id,
            verdict="present",
            observed_at="2026-08-12T00:00:00+00:00",
        )
    )
    return action.action_id


class OfflineRolloutRestoreIntentRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"

    def test_every_post_consumer_zero_effect_calls_the_fence_first(self) -> None:
        class BlockedFence:
            @staticmethod
            def before_effect(_action, _phase):
                return PhaseExecutionResult(False, reason="effect_edge_fenced")

        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})
        all_effects = sorted(PRE_RESTORE_EFFECT_PHASES | POST_RESTORE_EFFECT_PHASES)
        with patch.object(port, "_phase_fence", return_value=BlockedFence()):
            for name in all_effects:
                with self.subTest(phase=name):
                    result = port.execute_phase(
                        phase={"phase": name},
                        action={},
                        action_directory=self.home / "action",
                    )
                    self.assertFalse(result.ok)
                    self.assertEqual(result.reason, "effect_edge_fenced")

    def test_pre_restore_reclaim_or_unreadable_inventory_is_zero_effect(self) -> None:
        action = _action()
        state = {"view": _view()}
        fence = OfflineRolloutPhaseFence(
            home=self.home,
            inventory_reader=lambda: state["view"],
            pane_inventory_reader=lambda: _pane_rows(
                action, *state["view"].agents
            ),
            supervisor_stopped_reader=lambda: True,
        )
        with (
            patch.object(StartupTransactionFence, "read_snapshot", return_value=()),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_offline_rollout_phase_fence."
                "original_pin_positive_absence",
                return_value=True,
            ),
        ):
            self.assertTrue(fence.require_pre_restore(action).ok)
            state["view"] = _view(
                _agent(locator="w9:reclaimed", terminal="terminal:foreign")
            )
            reclaimed = fence.require_pre_restore(action)
            self.assertFalse(reclaimed.ok)
            self.assertEqual(reclaimed.reason, "restore_action_residual")
            state["view"] = _view(invalid=1)
            unreadable = fence.require_pre_restore(action)
            self.assertFalse(unreadable.ok)
            self.assertEqual(
                unreadable.reason, "restore_partition_inventory_unreadable"
            )

    def test_supervisor_reappearance_blocks_every_adjacent_effect_edge(self) -> None:
        action = _action()
        fence = OfflineRolloutPhaseFence(
            home=self.home,
            inventory_reader=lambda: _view(),
            pane_inventory_reader=lambda: _pane_rows(action),
            supervisor_stopped_reader=lambda: False,
        )
        with (
            patch.object(StartupTransactionFence, "read_snapshot", return_value=()),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_offline_rollout_phase_fence."
                "original_pin_positive_absence",
                return_value=True,
            ),
        ):
            pre_restore = fence.require_pre_restore(action)
            restore_group = fence.before_restore_group(
                action,
                phase_name="top_restore_action_bootstrap",
                group_index=0,
            )

        self.assertFalse(pre_restore.ok)
        self.assertEqual(pre_restore.reason, "supervisor_stop_drift")
        self.assertFalse(restore_group.ok)
        self.assertEqual(restore_group.reason, "supervisor_stop_drift")

    def test_completed_exact_action_folds_without_a_second_launch(self) -> None:
        action = _action()
        intent = decode_restore_intent(
            action["private_bindings"], plan=action["plan"]
        )
        observed = _agent()
        self.assertEqual(
            _seed_expected_group(self.home, intent.groups[0], observed),
            intent.groups[0].expected_startup_action_id,
        )
        preparer = Mock(side_effect=AssertionError("fold must not relaunch"))
        phase_fence = OfflineRolloutPhaseFence(
            home=self.home,
            inventory_reader=lambda: _view(observed),
            pane_inventory_reader=lambda: _pane_rows(action, observed),
        )
        executor = OfflineRolloutRestoreExecutor(
            home=self.home,
            env={},
            phase_fence=phase_fence,
            session_preparer=preparer,
        )
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_offline_rollout_phase_fence."
            "original_pin_positive_absence",
            return_value=True,
        ):
            result = executor.execute(
                phase_name="top_restore_action_bootstrap",
                action=action,
                action_directory=self.home / "action",
            )
        self.assertTrue(result.ok, result)
        self.assertEqual(
            result.receipt,
            restore_phase_receipt(intent, "top_restore_action_bootstrap"),
        )
        preparer.assert_not_called()

    def test_planned_or_rollback_expected_action_blocks_without_launch(self) -> None:
        for terminal_phase in ("planned", PHASE_ROLLBACK_OWED):
            with self.subTest(phase=terminal_phase), tempfile.TemporaryDirectory() as raw:
                home = Path(raw)
                action = _action()
                group = decode_restore_intent(
                    action["private_bindings"], plan=action["plan"]
                ).groups[0]
                fence_store = StartupTransactionFence(home=home)
                reserved = fence_store.reserve(
                    StartupUnit(group.workspace_id, group.lane_id, group.providers),
                    group.action_nonce,
                )
                if terminal_phase == PHASE_ROLLBACK_OWED:
                    fence_store.set_phase(reserved.action_id, PHASE_HEALTH_CHECK)
                    fence_store.set_phase(reserved.action_id, PHASE_ROLLBACK_OWED)
                preparer = Mock()
                executor = OfflineRolloutRestoreExecutor(
                    home=home,
                    env={},
                    phase_fence=OfflineRolloutPhaseFence(
                        home=home,
                        inventory_reader=lambda: _view(),
                        pane_inventory_reader=lambda: _pane_rows(action),
                    ),
                    session_preparer=preparer,
                )
                result = executor.execute(
                    phase_name="top_restore_action_bootstrap",
                    action=action,
                    action_directory=home / "action",
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, "restore_action_residual")
                preparer.assert_not_called()

    def test_foreign_nonterminal_is_home_global_but_terminal_history_is_ignored(self) -> None:
        for phase, expected_ok in (
            ("planned", False),
            (PHASE_ROLLBACK_OWED, False),
            (PHASE_COMPLETED_SUCCESS, True),
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as raw:
                home = Path(raw)
                action = _action()
                startup = StartupTransactionFence(home=home)
                foreign = startup.reserve(
                    StartupUnit("unrelated", "other", ("claude",)),
                    "foreign-nonce",
                )
                if phase != "planned":
                    startup.set_phase(foreign.action_id, PHASE_HEALTH_CHECK)
                    startup.set_phase(foreign.action_id, phase)
                fence = OfflineRolloutPhaseFence(
                    home=home,
                    inventory_reader=lambda: _view(),
                    pane_inventory_reader=lambda: _pane_rows(action),
                )
                with patch(
                    "mozyo_bridge.e_140_adapter_provider."
                    "f_130_terminal_runtime_provider.application."
                    "herdr_offline_rollout_phase_fence."
                    "original_pin_positive_absence",
                    return_value=True,
                ):
                    result = fence.require_pre_restore(action)
                self.assertEqual(result.ok, expected_ok)
                if not expected_ok:
                    self.assertEqual(result.reason, "restore_action_residual")

    def test_multigroup_inflight_allows_only_the_selected_expected_action(self) -> None:
        action = _action(two_groups=True)
        selected = decode_restore_intent(
            action["private_bindings"], plan=action["plan"]
        ).groups[0]
        reserved = StartupTransactionFence(home=self.home).reserve(
            StartupUnit(
                selected.workspace_id, selected.lane_id, selected.providers
            ),
            selected.action_nonce,
        )
        self.assertEqual(reserved.action_id, selected.expected_startup_action_id)
        fence = OfflineRolloutPhaseFence(
            home=self.home,
            inventory_reader=lambda: _view(),
            pane_inventory_reader=lambda: _pane_rows(action),
        )
        with patch(
            "mozyo_bridge.e_140_adapter_provider."
            "f_130_terminal_runtime_provider.application."
            "herdr_offline_rollout_phase_fence."
            "original_pin_positive_absence",
            return_value=True,
        ):
            result = fence.require_restore_effect(
                action,
                phase_name="top_restore_action_bootstrap",
                group_index=0,
                baseline_panes=_passive_baseline(action),
                transient_panes={},
            )
        self.assertTrue(result.ok, result)

    def test_offline_reserve_atomically_rejects_every_foreign_nonterminal(self) -> None:
        foreign_units = (
            StartupUnit("target", "lane", ("codex",)),
            StartupUnit("target", "lane", ("claude", "codex")),
            StartupUnit("unrelated", "other", ("claude",)),
        )
        for foreign_unit in foreign_units:
            with self.subTest(foreign=foreign_unit), tempfile.TemporaryDirectory() as raw:
                store = StartupTransactionFence(home=Path(raw))
                foreign = store.reserve(foreign_unit, "foreign")
                with self.assertRaisesRegex(
                    StartupTransactionError, "foreign nonterminal"
                ):
                    store.reserve(
                        StartupUnit(
                            "target", "lane", ("claude", "codex")
                        ),
                        "selected",
                        refuse_nonterminal_slot_overlap=True,
                    )
                self.assertEqual(store.read_snapshot(), (foreign,))

                store.set_phase(foreign.action_id, PHASE_HEALTH_CHECK)
                store.set_phase(foreign.action_id, PHASE_COMPLETED_SUCCESS)
                selected = store.reserve(
                    StartupUnit("target", "lane", ("claude", "codex")),
                    "selected",
                    refuse_nonterminal_slot_overlap=True,
                )
                self.assertFalse(selected.terminal)

    def test_full_pane_snapshot_requires_bidirectional_agent_role_join(self) -> None:
        action = _action()
        observed = _agent()
        for pane_agent in ("", "claude"):
            with self.subTest(pane_agent=pane_agent):
                panes = list(_pane_rows(action, observed))
                panes[-1] = {**panes[-1], "agent": pane_agent}
                fence = OfflineRolloutPhaseFence(
                    home=self.home,
                    inventory_reader=lambda: _view(observed),
                    pane_inventory_reader=lambda: tuple(panes),
                )
                with self.assertRaisesRegex(
                    ValueError, "restore_partition_inventory_unreadable"
                ):
                    fence.restore_pane_snapshot()

    def test_each_sealed_passive_root_axis_drift_is_zero_launch(self) -> None:
        mutations = {
            "locator": lambda row: {**row, "pane_id": "w1:p9"},
            "workspace": lambda row: {**row, "workspace_id": "w9"},
            "tab": lambda row: {**row, "tab_id": "w1:t9"},
            "terminal": lambda row: {
                **row,
                "terminal_id": "terminal:root:drift",
            },
        }
        for axis, mutate in mutations.items():
            with self.subTest(axis=axis):
                action = _action()
                panes = list(_pane_rows(action))
                panes[0] = mutate(panes[0])
                preparer = Mock()
                executor = OfflineRolloutRestoreExecutor(
                    home=self.home,
                    env={},
                    phase_fence=OfflineRolloutPhaseFence(
                        home=self.home,
                        inventory_reader=lambda: _view(),
                        pane_inventory_reader=lambda: tuple(panes),
                    ),
                    session_preparer=preparer,
                )
                with patch(
                    "mozyo_bridge.e_140_adapter_provider."
                    "f_130_terminal_runtime_provider.application."
                    "herdr_offline_rollout_phase_fence."
                    "original_pin_positive_absence",
                    return_value=True,
                ):
                    result = executor.execute(
                        phase_name="top_restore_action_bootstrap",
                        action=action,
                        action_directory=self.home / "action",
                    )
                self.assertFalse(result.ok)
                self.assertIn(
                    result.reason,
                    {
                        "restore_partition_drift",
                        "restore_partition_inventory_unreadable",
                    },
                )
                preparer.assert_not_called()

    def test_unsealed_extra_shell_only_pane_is_zero_effect_and_zero_launch(self) -> None:
        action = _action()
        panes = _pane_rows(action) + (
            {
                "pane_id": "w9:p1",
                "workspace_id": "w9",
                "tab_id": "w9:t1",
                "terminal_id": "terminal:unsealed-shell",
                "agent": "",
            },
        )
        fence = OfflineRolloutPhaseFence(
            home=self.home,
            inventory_reader=lambda: _view(),
            pane_inventory_reader=lambda: panes,
        )
        preparer = Mock()
        executor = OfflineRolloutRestoreExecutor(
            home=self.home,
            env={},
            phase_fence=fence,
            session_preparer=preparer,
        )
        with patch(
            "mozyo_bridge.e_140_adapter_provider."
            "f_130_terminal_runtime_provider.application."
            "herdr_offline_rollout_phase_fence."
            "original_pin_positive_absence",
            return_value=True,
        ):
            pre_restore = fence.require_pre_restore(action)
            launch = executor.execute(
                phase_name="top_restore_action_bootstrap",
                action=action,
                action_directory=self.home / "action",
            )
        self.assertFalse(pre_restore.ok)
        self.assertEqual(pre_restore.reason, "restore_partition_drift")
        self.assertFalse(launch.ok)
        self.assertEqual(launch.reason, "restore_partition_drift")
        preparer.assert_not_called()

    def test_receipt_container_is_strict_until_completed_then_tab_move_is_allowed(self) -> None:
        action = _action()
        group = decode_restore_intent(
            action["private_bindings"], plan=action["plan"]
        ).groups[0]
        observed = _agent()

        inflight_store = StartupTransactionFence(home=self.home)
        inflight = inflight_store.reserve(
            StartupUnit(group.workspace_id, group.lane_id, group.providers),
            group.action_nonce,
        )
        inflight_store.record_participant(
            inflight.action_id,
            Participant(
                role="codex",
                assigned_name=_TOP,
                locator=observed.locator,
                receipt=pane_bound_receipt(
                    target_workspace="w1",
                    target_tab="w1:t1",
                    native_name=native_name_for(_TOP),
                    terminal_id=observed.terminal_id,
                ),
            ),
        )
        inflight_store.set_phase(inflight.action_id, PHASE_HEALTH_CHECK)
        moved = list(_pane_rows(action, observed))
        moved[-1] = {**moved[-1], "tab_id": "w1:t2"}
        fence = OfflineRolloutPhaseFence(
            home=self.home,
            inventory_reader=lambda: _view(observed),
            pane_inventory_reader=lambda: tuple(moved),
        )
        with patch(
            "mozyo_bridge.e_140_adapter_provider."
            "f_130_terminal_runtime_provider.application."
            "herdr_offline_rollout_phase_fence."
            "original_pin_positive_absence",
            return_value=True,
        ):
            precompletion = fence.require_restore_effect(
                action,
                phase_name="top_restore_action_bootstrap",
                group_index=0,
                baseline_panes=_passive_baseline(action),
                transient_panes={},
            )
        self.assertFalse(precompletion.ok)
        self.assertEqual(precompletion.reason, "restore_action_residual")

        with tempfile.TemporaryDirectory() as raw:
            completed_home = Path(raw)
            _seed_expected_group(completed_home, group, observed)
            completed_fence = OfflineRolloutPhaseFence(
                home=completed_home,
                inventory_reader=lambda: _view(observed),
                pane_inventory_reader=lambda: tuple(moved),
            )
            with patch(
                "mozyo_bridge.e_140_adapter_provider."
                "f_130_terminal_runtime_provider.application."
                "herdr_offline_rollout_phase_fence."
                "original_pin_positive_absence",
                return_value=True,
            ):
                completed = completed_fence.require_restore_effect(
                    action,
                    phase_name="top_restore_action_bootstrap",
                    group_index=0,
                    baseline_panes=_passive_baseline(action),
                    transient_panes={},
                )
            self.assertTrue(completed.ok, completed)

    def test_real_completion_fence_leaves_health_on_pending_generation_or_v4_drift(self) -> None:
        for failure in ("generation_pending", "attestation_drift"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                home = Path(raw)
                action = _action()
                group = decode_restore_intent(
                    action["private_bindings"], plan=action["plan"]
                ).groups[0]
                state = {
                    "view": _view(),
                    "panes": _pane_rows(action),
                }
                phase_fence = OfflineRolloutPhaseFence(
                    home=home,
                    inventory_reader=lambda: state["view"],
                    pane_inventory_reader=lambda: state["panes"],
                )
                guard = _RestoreEffectGuard(
                    fence=phase_fence,
                    action=action,
                    phase_name="top_restore_action_bootstrap",
                    group_index=0,
                )
                transaction = StartupTransaction(
                    fence=StartupTransactionFence(home=home),
                    unit=StartupUnit(
                        group.workspace_id, group.lane_id, group.providers
                    ),
                    nonce=group.action_nonce,
                    effect_fence=guard,
                    completion_fence=guard.before_completion,
                    refuse_nonterminal_slot_overlap=True,
                )
                new_locator = "w1:p2"
                new_terminal = "terminal:restored"
                receipt = pane_bound_receipt(
                    target_workspace="w1",
                    target_tab="w1:t1",
                    native_name=native_name_for(_TOP),
                    terminal_id=new_terminal,
                )
                with patch(
                    "mozyo_bridge.e_140_adapter_provider."
                    "f_130_terminal_runtime_provider.application."
                    "herdr_offline_rollout_phase_fence."
                    "original_pin_positive_absence",
                    return_value=True,
                ):
                    transaction.reserve()
                    guard.own_pane(
                        new_locator, "w1", "w1:t1", new_terminal
                    )
                    state["panes"] = state["panes"] + (
                        {
                            "pane_id": new_locator,
                            "workspace_id": "w1",
                            "tab_id": "w1:t1",
                            "terminal_id": new_terminal,
                            "agent": "",
                        },
                    )
                    transaction.record_prepared_pane(
                        role="codex",
                        assigned_name=_TOP,
                        locator=new_locator,
                        receipt=receipt,
                    )
                    guard.release_pane(new_locator)
                    restored = _agent(
                        locator=new_locator, terminal=new_terminal
                    )
                    state["view"] = _view(restored)
                    state["panes"] = _pane_rows(action, restored)
                    HerdrIdentityAttestationStore(home=home).upsert(
                        IdentityAttestationRecord(
                            assigned_name=_TOP,
                            workspace_id="ws",
                            role="codex",
                            lane_id="default",
                            locator=new_locator,
                            terminal_id=(
                                "terminal:drift"
                                if failure == "attestation_drift"
                                else new_terminal
                            ),
                            verdict="present",
                            observed_at="2026-08-12T00:00:00+00:00",
                        )
                    )
                    seed_current_generation(
                        home,
                        workspace_id="ws",
                        lane_id="default",
                        role="codex",
                        assigned_name=_TOP,
                        locator=new_locator,
                        action_id=group.expected_startup_action_id,
                        terminal_id=new_terminal,
                        attested=failure != "generation_pending",
                    )
                    expected_reason = (
                        "restore_generation_unfinalized"
                        if failure == "generation_pending"
                        else "restore_attestation_unverified"
                    )
                    with (
                        patch(
                            "mozyo_bridge.e_140_adapter_provider."
                            "f_130_terminal_runtime_provider.application."
                            "herdr_session_start_completion._list_rows",
                            return_value=(),
                        ),
                        self.assertRaisesRegex(
                            HerdrSessionStartError, expected_reason
                        ),
                    ):
                        finalize_session_launch_authority(
                            SimpleNamespace(
                                lane_id="default",
                                owes_rollback=False,
                                slots=[],
                            ),
                            store_home=home,
                            transaction=transaction,
                            workspace_id="ws",
                            attestation_read=lambda _name: None,
                            attest_launcher="",
                            launch_plans=[
                                SimpleNamespace(
                                    provider="codex", assigned_name=_TOP
                                )
                            ],
                            dry_run=False,
                            binary="/herdr",
                            runner=Mock(),
                            timeout=1.0,
                            effect_fence=guard,
                        )
                landed = StartupTransactionFence(home=home).read(
                    group.expected_startup_action_id
                )
                self.assertEqual(landed.phase, PHASE_HEALTH_CHECK)
                self.assertFalse(landed.terminal)

    def test_launch_uses_sealed_nonce_and_requires_exact_returned_action(self) -> None:
        action = _action()
        intent = decode_restore_intent(
            action["private_bindings"], plan=action["plan"]
        )
        group = intent.groups[0]
        state = {"view": _view()}
        phase_fence = OfflineRolloutPhaseFence(
            home=self.home,
            inventory_reader=lambda: state["view"],
            pane_inventory_reader=lambda: _pane_rows(
                action, *state["view"].agents
            ),
        )
        preparer = Mock(
            return_value=SimpleNamespace(
                ok=True, action_id="startup-" + "f" * 64
            )
        )
        executor = OfflineRolloutRestoreExecutor(
            home=self.home,
            env={},
            phase_fence=phase_fence,
            session_preparer=preparer,
        )
        with (
            patch.object(executor, "_candidate_provenance", return_value=True),
            patch.object(executor, "_launch_environment", return_value={}),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_offline_rollout_phase_fence."
                "original_pin_positive_absence",
                return_value=True,
            ),
        ):
            mismatch = executor.execute(
                phase_name="top_restore_action_bootstrap",
                action=action,
                action_directory=self.home / "action",
            )
        self.assertFalse(mismatch.ok)
        self.assertEqual(mismatch.reason, "restore_action_id_mismatch")
        self.assertEqual(preparer.call_args.kwargs["action_nonce"], group.action_nonce)
        self.assertEqual(
            preparer.call_args.kwargs["expected_workspace_id"], group.workspace_id
        )
        self.assertNotIn(group.action_nonce, repr(intent))
        self.assertNotIn(group.expected_startup_action_id, repr(intent))

    def test_cwd_primitives_rejoin_private_identity_immediately_before_invoke(self) -> None:
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        for invoke in (
            lambda runner, fence: _create_workspace(
                "herdr", repo, runner, 1.0, {}, effect_fence=fence
            ),
            lambda runner, fence: _create_tab(
                "herdr", "w1", runner, 1.0, {}, effect_fence=fence
            ),
            lambda runner, fence: split_prepared_pane(
                binary="herdr",
                anchor_locator="w1:p1",
                direction="right",
                repo_root=repo,
                env_entries=(),
                runner=runner,
                timeout=1.0,
                env={},
                effect_fence=fence,
            ),
        ):
            with self.subTest(primitive=invoke):
                runner = Mock()
                fence = Mock(
                    side_effect=HerdrSessionStartError("private cwd binding changed")
                )
                with self.assertRaises(HerdrSessionStartError):
                    invoke(runner, fence)
                fence.assert_called_once_with()
                runner.assert_not_called()

    def test_recovery_cwd_refuses_a_different_worktree_of_the_same_workspace(self) -> None:
        root = Path(self.temp.name)
        main, approved, foreign = root / "main", root / "approved", root / "foreign"
        for path in (main, approved, foreign):
            path.mkdir()
        binding = PrivateWorktreeBinding("ws", "lane-1", 7, "private-token")
        row = SimpleNamespace(
            repo_workspace_id="ws",
            lane_id="lane-1",
            lane_generation=7,
            worktree_identity="private-token",
        )
        reader = Mock()
        reader.records.return_value = [row]
        with (
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_session_start_identity._is_linked_worktree",
                return_value=True,
            ),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_session_start_identity.herdr_workspace_segment",
                return_value="ws",
            ),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_session_start_identity.load_workspace_by_id",
                return_value=SimpleNamespace(canonical_path=str(main)),
            ),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_session_start_identity.LaneLifecycleReader",
                return_value=reader,
            ),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_session_start_identity.bind_lane_worktree",
                return_value=(approved, "refs/heads/lane-1"),
            ),
        ):
            fence = private_workspace_effect_fence(
                foreign,
                expected_workspace_id="ws",
                expected_worktree=binding,
                home=self.home,
            )
            self.assertIsNotNone(fence)
            with self.assertRaises(HerdrSessionStartError):
                fence()
        self.assertNotIn("private-token", repr(binding))

    def test_recovery_worktree_fence_is_derived_from_the_sealed_plan(self) -> None:
        action = {
            "plan": {
                "legacy_recoveries": [
                    {
                        "issue_id": "15227",
                        "workspace_id": "ws",
                        "lane_id": "lane-1",
                        "lane_generation": 7,
                        "worktree": {"identity": "private-token"},
                    }
                ]
            }
        }
        binding = OfflineRolloutRestoreExecutor._worktree_binding(  # noqa: SLF001
            action,
            SimpleNamespace(
                recovery_issue_id="15227", workspace_id="ws", lane_id="lane-1"
            ),
        )
        self.assertEqual(
            (binding.workspace_id, binding.lane_id, binding.lane_generation),
            ("ws", "lane-1", 7),
        )
        self.assertNotIn("private-token", repr(binding))

    def test_role_grouped_workspace_create_uses_the_same_private_cwd_fence(self) -> None:
        repo = Path(self.temp.name) / "repo"
        repo.mkdir(exist_ok=True)
        runner = Mock()
        fence = Mock(side_effect=HerdrSessionStartError("private cwd binding changed"))
        with (
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_role_grouped_space._list_workspace_labels",
                return_value={},
            ),
            self.assertRaises(HerdrSessionStartError),
        ):
            _resolve_project_coordinator_workspace_under_lock(
                rows=(),
                workspace_id="ws",
                lane_id="lane-1",
                adopted_locators=(),
                binary="herdr",
                repo_root=repo,
                runner=runner,
                timeout=1.0,
                env={},
                effect_fence=fence,
            )
        fence.assert_called_once_with()
        runner.assert_not_called()

    def test_runtime_install_rechecks_partition_after_hash_before_effect(self) -> None:
        action = _action()
        wheel = Path(self.temp.name) / "candidate.whl"
        wheel.write_bytes(b"candidate")
        action["plan"]["candidate_artifact"] = {
            "wheel_sha256": "a" * 64,
            "version": "0.15.0a4",
        }
        action["private_bindings"].update(
            runner={"wheel": str(wheel)},
            pipx="/installed/pipx",
        )
        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})

        class DriftedFence:
            @staticmethod
            def before_effect(_action, _phase):
                return PhaseExecutionResult(False, reason="effect_edge_drift")

        with (
            patch.object(port, "_phase_fence", return_value=DriftedFence()),
            patch(
                "mozyo_bridge.e_140_adapter_provider."
                "f_130_terminal_runtime_provider.application."
                "herdr_offline_rollout_executor._sha256",
                return_value="a" * 64,
            ),
            patch(
                "mozyo_bridge.e_140_adapter_provider."
                "f_130_terminal_runtime_provider.application."
                "herdr_offline_rollout_executor._run"
            ) as install,
        ):
            result = port._exact_runtime_install(  # noqa: SLF001
                {}, action, self.home / "action"
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "effect_edge_drift")
        install.assert_not_called()

    def test_launch_completion_requires_fresh_exact_cumulative_roster(self) -> None:
        action = _action(two_groups=True)
        intent = decode_restore_intent(
            action["private_bindings"], plan=action["plan"]
        )
        state = {"view": _view()}
        top = _agent()

        def prepare_top(**kwargs):
            self.assertEqual(kwargs["action_nonce"], intent.groups[0].action_nonce)
            _seed_expected_group(self.home, intent.groups[0], top)
            state["view"] = _view(top)
            return SimpleNamespace(
                ok=True,
                action_id=intent.groups[0].expected_startup_action_id,
            )

        executor = OfflineRolloutRestoreExecutor(
            home=self.home,
            env={},
            phase_fence=OfflineRolloutPhaseFence(
                home=self.home,
                inventory_reader=lambda: state["view"],
                pane_inventory_reader=lambda: _pane_rows(
                    action, *state["view"].agents
                ),
            ),
            session_preparer=prepare_top,
        )
        with (
            patch.object(executor, "_candidate_provenance", return_value=True),
            patch.object(executor, "_launch_environment", return_value={}),
            patch(
                "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
                "application.herdr_offline_rollout_phase_fence."
                "original_pin_positive_absence",
                return_value=True,
            ),
        ):
            first = executor.execute(
                phase_name="top_restore_action_bootstrap",
                action=action,
                action_directory=self.home / "action",
            )
        self.assertTrue(first.ok, first)

        state["view"] = _view(
            top,
            _agent(
                "mzb1_foreign_codex_default",
                workspace="foreign",
                locator="w9:p9",
                terminal="terminal:foreign",
            ),
        )
        second_preparer = Mock()
        second = OfflineRolloutRestoreExecutor(
            home=self.home,
            env={},
            phase_fence=OfflineRolloutPhaseFence(
                home=self.home,
                inventory_reader=lambda: state["view"],
                pane_inventory_reader=lambda: _pane_rows(
                    action, *state["view"].agents
                ),
            ),
            session_preparer=second_preparer,
        ).execute(
            phase_name="remaining_workspace_restore",
            action=action,
            action_directory=self.home / "action",
        )
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, "restore_partition_drift")
        second_preparer.assert_not_called()

    def test_provider_environment_scrubs_runner_and_caller_identity(self) -> None:
        target = Path(self.temp.name) / "mozyo-bridge"
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o700)
        action = _action()
        action["private_bindings"]["target_cli"] = str(target)
        executor = OfflineRolloutRestoreExecutor(
            home=self.home,
            env={
                RUNNER_ENV: "private-runner-token",
                "MOZYO_WORKSPACE_ID": "foreign",
                "MOZYO_AGENT_ROLE": "codex",
                "MOZYO_LANE_ID": "foreign",
                "PATH": "/usr/bin",
            },
            phase_fence=OfflineRolloutPhaseFence(
                home=self.home,
                inventory_reader=lambda: _view(),
                pane_inventory_reader=lambda: _pane_rows(action),
            ),
        )
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_offline_rollout_restore."
            "validate_provider_launch_bindings",
            return_value={"MOZYO_AGENT_CODEX_BINARY": "/usr/bin/true"},
        ):
            env = executor._launch_environment(action)  # noqa: SLF001
        self.assertNotIn(RUNNER_ENV, env)
        self.assertNotIn("MOZYO_WORKSPACE_ID", env)
        self.assertNotIn("MOZYO_AGENT_ROLE", env)
        self.assertNotIn("MOZYO_LANE_ID", env)
        self.assertEqual(env["MOZYO_BRIDGE_LAUNCHER"], str(target))
        self.assertNotIn(
            decode_restore_intent(
                action["private_bindings"], plan=action["plan"]
            ).groups[0].action_nonce,
            repr(env),
        )


if __name__ == "__main__":
    unittest.main()
