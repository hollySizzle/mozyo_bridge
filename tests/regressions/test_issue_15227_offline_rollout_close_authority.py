"""Offline rollout must use the shared terminal/generation close license (#15227)."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support.current_launch_authority import (
    seed_completed_current_launch_authority,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_close_authority import (  # noqa: E501
    OfflineRolloutCloseAuthorityError,
    decode_close_authority,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    HerdrLaunchGenerationStore,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
    HerdrObservedAgent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_close import (  # noqa: E501
    OfflineRolloutCloseExecutor,
    capture_close_authority,
    original_pin_positive_absence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_executor import (  # noqa: E501
    LiveOfflineRolloutExecutionPort,
)


_NAME = "mzb1_ws_codex_default"
_TERMINAL = "terminal-offline-current"
_LOCATOR = "w1:p1"


def _agent(
    *,
    name: str = _NAME,
    locator: str = _LOCATOR,
    terminal_id: str = _TERMINAL,
    workspace_id: str = "ws",
    lane_id: str = "default",
    role: str = "codex",
) -> HerdrObservedAgent:
    return HerdrObservedAgent(
        name=name,
        managed=True,
        workspace_id=workspace_id,
        lane_id=lane_id,
        role=role,
        runtime_state="awaiting_input",
        locator=locator,
        terminal_id=terminal_id,
    )


def _view(*agents: HerdrObservedAgent) -> HerdrInventoryView:
    return HerdrInventoryView(
        backend_selected=True,
        ok=True,
        workspace_segment="ws",
        agents=agents,
        raw_row_count=len(agents),
        invalid_row_count=0,
    )


def _pane_rows(view: HerdrInventoryView) -> tuple[dict, ...]:
    return tuple(
        {
            "pane_id": agent.locator,
            "workspace_id": agent.locator.split(":", 1)[0],
            "tab_id": f"{agent.locator.split(':', 1)[0]}:t1",
            "terminal_id": agent.terminal_id,
            "agent": agent.role,
        }
        for agent in view.agents
    )


def _plan() -> dict:
    return {
        "agents": [
            {
                "assigned_name": _NAME,
                "workspace_id": "ws",
                "lane_id": "default",
                "provider": "codex",
            }
        ],
        "phase_order": [
            {"phase": "non_top_workspace_stop", "assigned_names": []},
            {"phase": "top_workspace_stop", "assigned_names": [_NAME]},
        ],
    }


class OfflineRolloutCloseAuthorityRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.plan = _plan()
        self.action_id = seed_completed_current_launch_authority(
            self.home,
            workspace_id="ws",
            lane_id="default",
            role="codex",
            assigned_name=_NAME,
            locator=_LOCATOR,
            terminal_id=_TERMINAL,
            target_workspace="w1",
            target_tab="w1:t1",
        )
        captured = capture_close_authority(
            view=_view(_agent()), plan=self.plan, home=self.home
        )
        self.assertTrue(captured.ok, captured)
        self.private = {
            "workspace_paths": {"ws": str(Path(self.temp.name) / "repo")},
            "agents": [
                {
                    "assigned_name": _NAME,
                    "workspace_id": "ws",
                    "lane_id": "default",
                    "provider": "codex",
                }
            ],
            "close_authority": captured.receipt["close_authority"],
        }

    def _action(self, *, completed=()) -> dict:
        return {
            "plan": self.plan,
            "private_bindings": self.private,
            "completed_phases": list(completed),
        }

    def _executor(self, state) -> OfflineRolloutCloseExecutor:
        return OfflineRolloutCloseExecutor(
            home=self.home,
            env={},
            inventory_reader=lambda: state["view"],
            pane_inventory_reader=lambda: state.get(
                "panes", _pane_rows(state["view"])
            ),
            workspace_paths=self.private["workspace_paths"],
            settle_timeout=0,
            poll_interval=0,
        )

    def test_capture_persists_token_not_raw_terminal(self) -> None:
        encoded = json.dumps(self.private, sort_keys=True)
        self.assertNotIn(_TERMINAL, encoded)
        self.assertIn(self.action_id, encoded)
        self.assertEqual(
            set(self.private["agents"][0]),
            {"assigned_name", "workspace_id", "lane_id", "provider"},
        )
        decoded = decode_close_authority(self.private, plan=self.plan)
        self.assertEqual(decoded.pins[0].startup_action_id, self.action_id)
        self.assertNotIn(_LOCATOR, repr(decoded))
        self.assertNotIn(self.action_id, repr(decoded))

    def test_capture_refuses_missing_v4_or_completed_generation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = capture_close_authority(
                view=_view(_agent()), plan=self.plan, home=Path(raw)
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "close_authority_generation_unverified")

        record = HerdrIdentityAttestationStore(home=self.home).read(_NAME)
        with patch.object(
            HerdrIdentityAttestationStore,
            "read",
            return_value=replace(record, schema_version=3),
        ):
            legacy = capture_close_authority(
                view=_view(_agent()), plan=self.plan, home=self.home
            )
        self.assertFalse(legacy.ok)
        self.assertEqual(legacy.reason, "close_authority_generation_unverified")

    def test_closed_shape_rejects_tokenless_or_extra_fields(self) -> None:
        pin = dict(self.private["close_authority"]["pins"][0])
        for raw in (
            {"version": 2, "pins": [{key: value for key, value in pin.items()
                                       if key != "startup_action_id"}]},
            {"version": 2, "pins": [{**pin, "terminal_id": _TERMINAL}]},
            {"version": 2, "pins": [{**pin, "startup_action_id": ""}]},
        ):
            with self.subTest(raw=raw), self.assertRaises(
                OfflineRolloutCloseAuthorityError
            ):
                decode_close_authority(
                    {"close_authority": raw}, plan=self.plan
                )

    def test_first_non_replay_absence_is_zero_close(self) -> None:
        state = {"view": _view()}
        closer = self._executor(state)
        target = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "sublane_herdr_retire.execute_herdr_retire_close"
        )
        with patch(target) as close:
            result = closer.close_names(
                action=self._action(), names=(_NAME,), replaying=False
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "close_authority_target_absent")
        close.assert_not_called()

        settled = closer.wait_for_settled(
            action=self._action(), names=(_NAME,), replaying=False
        )
        self.assertFalse(settled.ok)
        self.assertEqual(settled.reason, "close_authority_target_absent")

    def test_active_phase_replay_accepts_only_positive_absence(self) -> None:
        state = {"view": _view()}
        closer = self._executor(state)
        target = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "sublane_herdr_retire.execute_herdr_retire_close"
        )
        with patch(target) as close:
            result = closer.close_names(
                action=self._action(), names=(_NAME,), replaying=True
            )
        self.assertTrue(result.ok, result)
        close.assert_not_called()

        state["view"] = _view(
            _agent(name="mzb1_ws_codex_other", locator="w1:p2")
        )
        with patch(target) as close:
            reclaimed = closer.close_names(
                action=self._action(), names=(_NAME,), replaying=True
            )
        self.assertFalse(reclaimed.ok)
        close.assert_not_called()

        state["view"] = _view()
        with (
            patch.object(HerdrIdentityAttestationStore, "read", side_effect=OSError),
            patch(target) as close,
        ):
            unreadable = closer.close_names(
                action=self._action(), names=(_NAME,), replaying=True
            )
        self.assertFalse(unreadable.ok)
        self.assertEqual(unreadable.reason, "close_authority_absence_unverified")
        close.assert_not_called()

    def test_shell_only_old_terminal_is_not_positive_absence(self) -> None:
        state = {
            "view": _view(),
            "panes": (
                {
                    "pane_id": _LOCATOR,
                    "workspace_id": "w1",
                    "tab_id": "w1:t1",
                    "terminal_id": _TERMINAL,
                    "agent": "",
                },
            ),
        }
        target = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "sublane_herdr_retire.execute_herdr_retire_close"
        )
        with patch(target) as close:
            result = self._executor(state).close_names(
                action=self._action(), names=(_NAME,), replaying=True
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "close_authority_absence_unverified")
        close.assert_not_called()

    def test_post_rebuild_absence_uses_original_completed_pane_receipt(self) -> None:
        pin = decode_close_authority(self.private, plan=self.plan).pins[0]
        with patch.object(HerdrLaunchGenerationStore, "read", return_value=None):
            self.assertTrue(
                original_pin_positive_absence(
                    home=self.home, view=_view(), pane_rows=(), pin=pin
                )
            )
            reclaimed = _view(
                _agent(
                    name="mzb1_foreign_codex_default",
                    locator="w9:p9",
                    terminal_id=_TERMINAL,
                    workspace_id="foreign",
                )
            )
            self.assertFalse(
                original_pin_positive_absence(
                    home=self.home,
                    view=reclaimed,
                    pane_rows=_pane_rows(reclaimed),
                    pin=pin,
                )
            )

    def test_saved_startup_token_mismatch_is_zero_close(self) -> None:
        state = {"view": _view(_agent())}
        closer = self._executor(state)
        private = json.loads(json.dumps(self.private))
        private["close_authority"]["pins"][0][
            "startup_action_id"
        ] = "startup-" + "e" * 64
        action = {**self._action(), "private_bindings": private}
        target = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "sublane_herdr_retire.execute_herdr_retire_close"
        )
        with patch(target) as close:
            result = closer.close_names(
                action=action, names=(_NAME,), replaying=False
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "close_authority_generation_drift")
        close.assert_not_called()

    def test_present_target_requires_exact_two_way_agent_pane_join(self) -> None:
        view = _view(_agent())
        exact = _pane_rows(view)[0]
        cases = {
            "same_locator_terminal_drift": (
                {**exact, "terminal_id": "terminal-offline-drift"},
            ),
            "role_drift": ({**exact, "agent": "claude"},),
            "missing_agent_marker": ({**exact, "agent": ""},),
            "missing_agent_bearing_pane": (),
            "extra_torn_agent_mapping": (
                exact,
                {
                    "pane_id": "w2:p2",
                    "workspace_id": "w2",
                    "tab_id": "w2:t1",
                    "terminal_id": "terminal-offline-extra",
                    "agent": "claude",
                },
            ),
            "malformed_pane": (
                {
                    key: value
                    for key, value in exact.items()
                    if key != "terminal_id"
                },
            ),
            "duplicate_pane": (exact, exact),
        }
        target = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "sublane_herdr_retire.execute_herdr_retire_close"
        )
        for case, panes in cases.items():
            with self.subTest(case=case), patch(target) as close:
                result = self._executor(
                    {"view": view, "panes": panes}
                ).close_names(
                    action=self._action(), names=(_NAME,), replaying=False
                )
            self.assertFalse(result.ok)
            self.assertEqual(
                result.reason, "close_authority_inventory_unreadable"
            )
            close.assert_not_called()

    def test_unreadable_agent_or_pane_inventory_is_zero_close(self) -> None:
        def unreadable():
            raise OSError("unreadable")

        target = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "sublane_herdr_retire.execute_herdr_retire_close"
        )
        readers = {
            "agent_inventory": (unreadable, lambda: _pane_rows(_view(_agent()))),
            "pane_inventory": (lambda: _view(_agent()), unreadable),
        }
        for case, (inventory_reader, pane_inventory_reader) in readers.items():
            closer = OfflineRolloutCloseExecutor(
                home=self.home,
                env={},
                inventory_reader=inventory_reader,
                pane_inventory_reader=pane_inventory_reader,
                workspace_paths=self.private["workspace_paths"],
                settle_timeout=0,
                poll_interval=0,
            )
            with self.subTest(case=case), patch(target) as close:
                result = closer.close_names(
                    action=self._action(), names=(_NAME,), replaying=False
                )
            self.assertFalse(result.ok)
            self.assertEqual(
                result.reason, "close_authority_inventory_unreadable"
            )
            close.assert_not_called()

    def test_present_target_exact_agent_pane_join_allows_close(self) -> None:
        view = _view(_agent())
        state = {"view": view, "panes": _pane_rows(view)}
        target = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "sublane_herdr_retire.execute_herdr_retire_close"
        )

        def close_once(*_args, **_kwargs):
            state["view"] = _view()
            state["panes"] = ()
            return SimpleNamespace(failed=False)

        with patch(target, side_effect=close_once) as close:
            result = self._executor(state).close_names(
                action=self._action(), names=(_NAME,), replaying=False
            )
        self.assertTrue(result.ok, result)
        close.assert_called_once()

    def test_completed_stop_phase_allows_only_its_positive_absence(self) -> None:
        state = {"view": _view()}
        closer = self._executor(state)
        plan = {
            **self.plan,
            "phase_order": [
                {
                    "phase": "non_top_workspace_stop",
                    "assigned_names": [_NAME],
                },
                {"phase": "top_workspace_stop", "assigned_names": []},
            ],
        }
        action = {
            "plan": plan,
            "private_bindings": self.private,
            "completed_phases": ["non_top_workspace_stop"],
        }
        result = closer.close_names(action=action, names=(), replaying=False)
        self.assertTrue(result.ok, result)

    def test_live_port_threads_replay_state_into_both_stop_phases(self) -> None:
        port = LiveOfflineRolloutExecutionPort(home=self.home, env={})
        for phase_name in ("non_top_workspace_stop", "top_workspace_stop"):
            with self.subTest(phase=phase_name), patch.object(
                port,
                "_stop_agents",
                return_value=SimpleNamespace(ok=True),
            ) as stop:
                result = port.execute_phase(
                    phase={"phase": phase_name},
                    action={},
                    action_directory=self.home / "action",
                    replaying=True,
                )
            self.assertTrue(result.ok)
            self.assertTrue(stop.call_args.kwargs["replaying"])

    def test_each_close_rejoins_then_requires_positive_absence(self) -> None:
        state = {"view": _view(_agent())}
        closer = self._executor(state)
        target = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "sublane_herdr_retire.execute_herdr_retire_close"
        )

        def close_once(*_args, **_kwargs):
            state["view"] = _view()
            return SimpleNamespace(failed=False)

        with patch(target, side_effect=close_once) as close:
            result = closer.close_names(
                action=self._action(), names=(_NAME,), replaying=False
            )
        self.assertTrue(result.ok, result)
        close.assert_called_once()

        state["view"] = _view(_agent(locator="w1:reclaimed"))
        with patch(target) as close:
            drifted = closer.close_names(
                action=self._action(), names=(_NAME,), replaying=False
            )
        self.assertFalse(drifted.ok)
        self.assertEqual(drifted.reason, "close_authority_generation_drift")
        close.assert_not_called()

        state["view"] = _view(_agent())
        with patch(target, return_value=SimpleNamespace(failed=False)) as close:
            unverified = closer.close_names(
                action=self._action(), names=(_NAME,), replaying=False
            )
        self.assertFalse(unverified.ok)
        self.assertEqual(unverified.reason, "agent_stop_unverified")
        close.assert_called_once()

    def test_multi_workspace_targets_rejoin_from_a_fresh_full_snapshot(self) -> None:
        second_name = "mzb1_other_claude_default"
        second_locator = "w2:p2"
        second_terminal = "terminal-offline-second"
        seed_completed_current_launch_authority(
            self.home,
            workspace_id="other",
            lane_id="default",
            role="claude",
            assigned_name=second_name,
            locator=second_locator,
            terminal_id=second_terminal,
            target_workspace="w2",
            target_tab="w2:t1",
        )
        second = _agent(
            name=second_name,
            locator=second_locator,
            terminal_id=second_terminal,
            workspace_id="other",
            role="claude",
        )
        plan = {
            "agents": [
                *self.plan["agents"],
                {
                    "assigned_name": second_name,
                    "workspace_id": "other",
                    "lane_id": "default",
                    "provider": "claude",
                },
            ],
            "phase_order": [
                {
                    "phase": "non_top_workspace_stop",
                    "assigned_names": [second_name],
                },
                {"phase": "top_workspace_stop", "assigned_names": [_NAME]},
            ],
        }
        first = _agent()
        state = {"view": _view(first, second), "reads": 0}
        captured = capture_close_authority(
            view=state["view"], plan=plan, home=self.home
        )
        self.assertTrue(captured.ok, captured)
        private = {
            "workspace_paths": {"ws": "/repo/ws", "other": "/repo/other"},
            "agents": plan["agents"],
            "close_authority": captured.receipt["close_authority"],
        }

        def inventory():
            state["reads"] += 1
            return state["view"]

        closer = OfflineRolloutCloseExecutor(
            home=self.home,
            env={},
            inventory_reader=inventory,
            pane_inventory_reader=lambda: _pane_rows(state["view"]),
            workspace_paths=private["workspace_paths"],
            settle_timeout=0,
            poll_interval=0,
        )

        def close_one(close_plan, **_kwargs):
            locator = close_plan.close_targets[0][1]
            state["view"] = _view(
                *(agent for agent in state["view"].agents
                  if agent.locator != locator)
            )
            return SimpleNamespace(failed=False)

        target = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "sublane_herdr_retire.execute_herdr_retire_close"
        )
        action = {
            "plan": plan,
            "private_bindings": private,
            "completed_phases": [],
        }
        with patch(target, side_effect=close_one) as close:
            result = closer.close_names(
                action=action,
                names=(_NAME, second_name),
                replaying=False,
            )
        self.assertTrue(result.ok, result)
        self.assertEqual(close.call_count, 2)
        self.assertGreaterEqual(state["reads"], 5)
        self.assertEqual(
            [call.args[0].workspace_id for call in close.call_args_list],
            ["ws", "other"],
        )


if __name__ == "__main__":
    unittest.main()
