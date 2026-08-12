"""Legacy recovery absence is sealed separately from close authority (#15227)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.herdr_launch_generation import (
    HerdrLaunchGenerationStore,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    PHASE_HEALTH_CHECK,
    Participant,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_legacy_absence_authority import (  # noqa: E501
    OfflineRolloutLegacyAbsenceAuthorityError,
    decode_legacy_absence_authority,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_pane_intent import (  # noqa: E501
    build_pane_intent,
)
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_restore_intent import (  # noqa: E501
    build_restore_intent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
    HerdrObservedAgent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_close import (  # noqa: E501
    OfflineRolloutCloseExecutor,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_legacy_absence import (  # noqa: E501
    capture_legacy_absence_authority,
    legacy_pin_positive_absence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_phase_fence import (  # noqa: E501
    OfflineRolloutPhaseFence,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_restore import (  # noqa: E501
    capture_restore_container_intent,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    pane_bound_receipt,
)
from tests.support.current_launch_authority import seed_current_generation


_CLAUDE = "mzb1_ws__claude__lane_1"
_CODEX = "mzb1_ws__codex__lane_1"
_ROOT_TERMINAL = "terminal:legacy:root"
_OLD = {
    _CLAUDE: ("claude", "w7:p1", "terminal:legacy:claude"),
    _CODEX: ("codex", "w7:p2", "terminal:legacy:codex"),
}


def _plan() -> dict:
    return {
        "agents": [],
        "stores": {
            "launch_generation": {
                "state": "recognized",
                "version": 2,
                "target_version": 2,
                "upgrade_required": False,
            }
        },
        "legacy_recoveries": [
            {
                "issue_id": "15227",
                "workspace_id": "ws",
                "lane_id": "lane-1",
                "agents": [
                    {"provider": provider, "assigned_name": name}
                    for name, (provider, _locator, _terminal) in sorted(_OLD.items())
                ],
            }
        ],
        "phase_order": [
            {
                "phase": "top_restore_action_bootstrap",
                "assigned_names": sorted(_OLD),
            },
            {"phase": "remaining_workspace_restore", "assigned_names": []},
        ],
    }


def _view(*agents: HerdrObservedAgent) -> HerdrInventoryView:
    return HerdrInventoryView(
        backend_selected=True,
        ok=True,
        workspace_segment="ws",
        agents=agents,
        raw_row_count=len(agents),
        invalid_row_count=0,
    )


def _root(*, extra=()) -> tuple[dict, ...]:
    return (
        {
            "pane_id": "w7:p0",
            "workspace_id": "w7",
            "tab_id": "w7:t1",
            "terminal_id": _ROOT_TERMINAL,
            "agent": "",
        },
        *extra,
    )


def _agent(name: str, provider: str, locator: str, terminal: str):
    return HerdrObservedAgent(
        name=name,
        managed=True,
        workspace_id="ws",
        lane_id="lane-1",
        role=provider,
        runtime_state="awaiting_input",
        locator=locator,
        terminal_id=terminal,
    )


class OfflineRolloutLegacyAbsenceRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.plan = _plan()
        self.restore = build_restore_intent(
            self.plan, nonce_factory=lambda: "1" * 32
        )

    def _seed_old_pair(self, *, terminal_bound: bool = True, split_tab=False) -> str:
        fence = StartupTransactionFence(home=self.home)
        action = fence.reserve(
            StartupUnit("ws", "lane-1", ("claude", "codex")), "old-pair"
        )
        for name, (provider, locator, terminal) in sorted(_OLD.items()):
            tab = "w7:t2" if split_tab and provider == "codex" else "w7:t1"
            receipt = (
                pane_bound_receipt(
                    target_workspace="w7",
                    target_tab=tab,
                    native_name=native_name_for(name),
                    terminal_id=terminal,
                )
                if terminal_bound
                else (
                    f"pane_bound_v1 workspace=w7 tab={tab} "
                    f"native={native_name_for(name)}"
                )
            )
            fence.record_participant(
                action.action_id,
                Participant(
                    role=provider,
                    assigned_name=name,
                    locator=locator,
                    receipt=receipt,
                ),
            )
        fence.set_phase(action.action_id, PHASE_HEALTH_CHECK)
        fence.set_phase(action.action_id, PHASE_COMPLETED_SUCCESS)
        for name, (provider, locator, terminal) in _OLD.items():
            seed_current_generation(
                self.home,
                workspace_id="ws",
                lane_id="lane-1",
                role=provider,
                assigned_name=name,
                locator=locator,
                action_id=action.action_id,
                terminal_id=terminal,
            )
            HerdrIdentityAttestationStore(home=self.home).upsert(
                IdentityAttestationRecord(
                    assigned_name=name,
                    workspace_id="ws",
                    role=provider,
                    lane_id="lane-1",
                    locator=locator,
                    terminal_id=terminal,
                    verdict="present",
                    observed_at="2026-08-12T00:00:00+00:00",
                )
            )
        return action.action_id

    def _capture(self):
        return capture_legacy_absence_authority(
            home=self.home,
            plan=self.plan,
            restore_intent=self.restore,
            view=_view(),
            pane_rows=_root(),
        )

    def _private(self) -> dict:
        captured = self._capture()
        self.assertTrue(captured.ok, captured)
        panes = build_pane_intent(_root(), agents=())
        private = {
            "close_authority": {"version": 2, "pins": []},
            "legacy_absence_authority": captured.receipt[
                "legacy_absence_authority"
            ],
            "restore_intent": self.restore.as_payload(),
            "passive_pane_intent": panes.as_payload(),
        }
        container = capture_restore_container_intent(
            home=self.home, plan=self.plan, private_bindings=private
        )
        private["restore_container_intent"] = container.as_payload()
        return private

    def test_terminal_bound_absent_pair_captures_and_seals_one_container(self) -> None:
        action_id = self._seed_old_pair()
        private = self._private()
        authority = decode_legacy_absence_authority(
            private, plan=self.plan, restore_intent=self.restore
        )
        self.assertEqual(
            {pin.startup_action_id for pin in authority.pins}, {action_id}
        )
        self.assertEqual(private["close_authority"]["pins"], [])
        group = private["restore_container_intent"]["groups"][0]
        self.assertEqual(
            (group["workspace_id"], group["tab_id"], group["pane_locator"]),
            ("w7", "w7:t1", "w7:p0"),
        )
        encoded = json.dumps(private, sort_keys=True)
        for _provider, locator, terminal in _OLD.values():
            self.assertNotIn(terminal, encoded)
            self.assertNotIn(locator, repr(authority))
        self.assertNotIn(action_id, repr(authority))

    def test_preterminal_or_split_container_evidence_is_zero_capture(self) -> None:
        self._seed_old_pair(terminal_bound=False)
        preterminal = self._capture()
        self.assertFalse(preterminal.ok)
        self.assertEqual(
            preterminal.reason, "legacy_absence_authority_unverified"
        )

        with tempfile.TemporaryDirectory() as raw:
            self.home = Path(raw) / "home"
            self._seed_old_pair(split_tab=True)
            captured = self._capture()
            self.assertTrue(captured.ok, captured)
            panes = build_pane_intent(_root(), agents=())
            with self.assertRaisesRegex(ValueError, "restore_container_binding_invalid"):
                capture_restore_container_intent(
                    home=self.home,
                    plan=self.plan,
                    private_bindings={
                        "close_authority": {"version": 2, "pins": []},
                        "legacy_absence_authority": captured.receipt[
                            "legacy_absence_authority"
                        ],
                        "restore_intent": self.restore.as_payload(),
                        "passive_pane_intent": panes.as_payload(),
                    },
                )

    def test_each_evidence_reclaim_and_rebuild_fallback_fail_closed(self) -> None:
        self._seed_old_pair()
        private = self._private()
        pin = decode_legacy_absence_authority(
            private, plan=self.plan, restore_intent=self.restore
        ).pins[0]
        group = self.restore.groups[0]
        exact = dict(
            home=self.home,
            view=_view(),
            pane_rows=_root(),
            pin=pin,
            expected_providers=group.providers,
            require_current_generation=True,
        )
        self.assertTrue(legacy_pin_positive_absence(**exact))
        with patch.object(HerdrLaunchGenerationStore, "read", return_value=None):
            self.assertFalse(legacy_pin_positive_absence(**exact))
            self.assertFalse(
                legacy_pin_positive_absence(
                    **{**exact, "require_current_generation": False}
                )
            )
        expected_replacement = SimpleNamespace(
            phase="pending",
            startup_action_id=group.expected_startup_action_id,
            workspace_id=pin.workspace_id,
            role=pin.provider,
            lane_id=pin.lane_id,
            locator="",
            terminal_id="",
        )
        with patch.object(
            HerdrLaunchGenerationStore,
            "read",
            return_value=expected_replacement,
        ):
            self.assertTrue(
                legacy_pin_positive_absence(
                    **{
                        **exact,
                        "require_current_generation": False,
                        "allowed_replacement_action_id": (
                            group.expected_startup_action_id
                        ),
                    }
                )
            )
        drifted_generation = SimpleNamespace(
            phase="attested",
            startup_action_id="startup-" + "f" * 64,
            workspace_id=pin.workspace_id,
            role=pin.provider,
            lane_id=pin.lane_id,
            locator="w8:p8",
            terminal_id="terminal:foreign",
        )
        with patch.object(
            HerdrLaunchGenerationStore,
            "read",
            return_value=drifted_generation,
        ):
            self.assertFalse(
                legacy_pin_positive_absence(
                    **{**exact, "require_current_generation": False}
                )
            )
        with patch.object(HerdrIdentityAttestationStore, "read", return_value=None):
            self.assertFalse(legacy_pin_positive_absence(**exact))

        provider, locator, terminal = _OLD[pin.assigned_name]
        foreign = _agent(
            pin.assigned_name, provider, "w8:p8", "terminal:replacement"
        )
        self.assertFalse(
            legacy_pin_positive_absence(
                **{
                    **exact,
                    "view": _view(foreign),
                    "pane_rows": _root(
                        extra=(
                            {
                                "pane_id": foreign.locator,
                                "workspace_id": "w8",
                                "tab_id": "w8:t1",
                                "terminal_id": foreign.terminal_id,
                                "agent": provider,
                            },
                        )
                    ),
                }
            )
        )
        for row in (
            {
                "pane_id": locator,
                "workspace_id": "w7",
                "tab_id": "w7:t1",
                "terminal_id": "terminal:other",
                "agent": "",
            },
            {
                "pane_id": "w8:p8",
                "workspace_id": "w8",
                "tab_id": "w8:t1",
                "terminal_id": terminal,
                "agent": "",
            },
        ):
            with self.subTest(row=row):
                self.assertFalse(
                    legacy_pin_positive_absence(
                        **{**exact, "pane_rows": _root(extra=(row,))}
                    )
                )

    def test_phase_fence_accepts_nonempty_root_then_blocks_edge_drift(self) -> None:
        self._seed_old_pair()
        private = self._private()
        state = {"view": _view(), "panes": _root()}
        fence = OfflineRolloutPhaseFence(
            home=self.home,
            inventory_reader=lambda: state["view"],
            pane_inventory_reader=lambda: state["panes"],
            supervisor_stopped_reader=lambda: True,
        )
        action = {
            "plan": self.plan,
            "private_bindings": private,
            "completed_phases": [],
        }
        self.assertTrue(fence.require_pre_restore(action).ok)
        action["completed_phases"] = ["rebuild_launch_generation"]
        with patch.object(HerdrLaunchGenerationStore, "read", return_value=None):
            self.assertFalse(fence.require_pre_restore(action).ok)
        state["panes"] = _root(
            extra=(
                {
                    "pane_id": "w8:p8",
                    "workspace_id": "w8",
                    "tab_id": "w8:t1",
                    "terminal_id": _OLD[_CODEX][2],
                    "agent": "",
                },
            )
        )
        blocked = fence.require_pre_restore(action)
        self.assertFalse(blocked.ok)

    def test_closed_schema_and_close_target_separation(self) -> None:
        self._seed_old_pair()
        private = self._private()
        malformed = json.loads(json.dumps(private))
        malformed["legacy_absence_authority"]["pins"][0][
            "terminal_id"
        ] = _OLD[_CLAUDE][2]
        with self.assertRaises(OfflineRolloutLegacyAbsenceAuthorityError):
            decode_legacy_absence_authority(
                malformed, plan=self.plan, restore_intent=self.restore
            )

        closer = OfflineRolloutCloseExecutor(
            home=self.home,
            env={},
            inventory_reader=lambda: (_ for _ in ()).throw(
                AssertionError("legacy close must not read inventory")
            ),
            pane_inventory_reader=lambda: (),
            workspace_paths={},
        )
        target = (
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "sublane_herdr_retire.execute_herdr_retire_close"
        )
        with patch(target) as close:
            result = closer.close_names(
                action={"plan": self.plan, "private_bindings": private},
                names=(_CLAUDE,),
                replaying=False,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "close_authority_phase_target_mismatch")
        close.assert_not_called()

    def test_nonempty_authority_requires_v2_plan_and_rebuild_is_non_destructive(self) -> None:
        self._seed_old_pair()
        private = self._private()
        for generation in (
            {
                "state": "recognized",
                "version": 1,
                "target_version": 2,
                "upgrade_required": True,
            },
            {
                "state": "absent",
                "version": None,
                "target_version": 2,
                "upgrade_required": False,
            },
        ):
            with self.subTest(generation=generation):
                plan = json.loads(json.dumps(self.plan))
                plan["stores"]["launch_generation"] = generation
                with self.assertRaisesRegex(
                    OfflineRolloutLegacyAbsenceAuthorityError,
                    "legacy_absence_authority_generation_plan_invalid",
                ):
                    decode_legacy_absence_authority(
                        private, plan=plan, restore_intent=self.restore
                    )

        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_generation_rebuild import (  # noqa: E501
            rebuild_launch_generation,
        )

        before = {
            name: HerdrLaunchGenerationStore(home=self.home).read(name)
            for name in _OLD
        }
        planned = {
            "state": "recognized",
            "version": 2,
            "target_version": 2,
            "upgrade_required": False,
        }
        rebuilt = rebuild_launch_generation(
            home=self.home,
            backup_root=Path(self.temp.name) / "backups",
            planned=planned,
            observe=lambda: planned,
            backup_receipt={},
            replaying=False,
        )
        self.assertTrue(rebuilt.ok, rebuilt)
        self.assertEqual(rebuilt.receipt["outcome"], "already_current")
        self.assertEqual(
            {
                name: HerdrLaunchGenerationStore(home=self.home).read(name)
                for name in _OLD
            },
            before,
        )


if __name__ == "__main__":
    unittest.main()
