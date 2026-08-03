from __future__ import annotations

import unittest
from dataclasses import replace

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.workspace_retirement import (
    WorkspaceRetirementAuthorityError,
    WorkspaceRetirementStoreOutcome,
    WorkspaceRetirementUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_retirement import (
    PATH_MISSING,
    PATH_PRESENT,
    STATE_ALREADY_RETIRED,
    STATE_RETIRED,
    WorkspaceRetirementInventory,
    WorkspaceRetirementObservation,
    build_workspace_retirement_plan,
)


TARGET = "target-workspace"
CURRENT = "current-workspace"


def _observation(**changes) -> WorkspaceRetirementObservation:
    values = {
        "workspace_id": TARGET,
        "project_name": "retired-smoke",
        "updated_at": "2026-08-03T00:00:00+00:00",
        "record_digest": "a" * 64,
        "path_state": PATH_MISSING,
    }
    values.update(changes)
    return WorkspaceRetirementObservation(**values)


def _inventory(**changes) -> WorkspaceRetirementInventory:
    values = {
        "readable": True,
        "projection_complete": True,
        "live_agent_count": 0,
        "target_agent_set_digest": "b" * 64,
    }
    values.update(changes)
    return WorkspaceRetirementInventory(**values)


class WorkspaceRetirementPlanTests(unittest.TestCase):
    def test_missing_path_and_zero_live_agents_produce_a_redacted_plan(self) -> None:
        result = build_workspace_retirement_plan(
            observation=_observation(),
            inventory=_inventory(),
            current_workspace_id=CURRENT,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.state, "planned")
        self.assertEqual(len(result.plan_digest), 64)
        self.assertEqual(result.plan["target"]["workspace_id"], TARGET)
        self.assertEqual(result.plan["liveness"]["authority"], "herdr_global_inventory")
        self.assertNotIn("path", str(result.plan).lower().replace("path_state", ""))

    def test_current_workspace_path_or_live_agents_refuse(self) -> None:
        cases = (
            (
                _observation(),
                _inventory(),
                TARGET,
                "target_is_current_workspace",
            ),
            (
                _observation(path_state=PATH_PRESENT),
                _inventory(),
                CURRENT,
                "workspace_path_present",
            ),
            (
                _observation(),
                _inventory(live_agent_count=1),
                CURRENT,
                "live_agents_present",
            ),
            (
                _observation(),
                _inventory(readable=False),
                CURRENT,
                "inventory_unreadable",
            ),
            (
                _observation(),
                _inventory(projection_complete=False),
                CURRENT,
                "inventory_unreadable",
            ),
        )
        for observation, inventory, current, reason in cases:
            with self.subTest(reason=reason):
                result = build_workspace_retirement_plan(
                    observation=observation,
                    inventory=inventory,
                    current_workspace_id=current,
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, reason)
                self.assertIsNone(result.plan)

    def test_execute_requires_the_exact_fresh_plan_digest(self) -> None:
        preview = build_workspace_retirement_plan(
            observation=_observation(),
            inventory=_inventory(),
            current_workspace_id=CURRENT,
        )
        missing = build_workspace_retirement_plan(
            observation=_observation(),
            inventory=_inventory(),
            current_workspace_id=CURRENT,
            execute=True,
        )
        wrong = build_workspace_retirement_plan(
            observation=_observation(),
            inventory=_inventory(),
            current_workspace_id=CURRENT,
            execute=True,
            expected_plan_digest="f" * 64,
        )
        admitted = build_workspace_retirement_plan(
            observation=_observation(),
            inventory=_inventory(),
            current_workspace_id=CURRENT,
            execute=True,
            expected_plan_digest=preview.plan_digest,
        )
        self.assertEqual(missing.reason, "execute_plan_digest_required")
        self.assertEqual(wrong.reason, "plan_digest_mismatch")
        self.assertTrue(admitted.ok)

    def test_every_authority_change_changes_the_plan_digest(self) -> None:
        first = build_workspace_retirement_plan(
            observation=_observation(),
            inventory=_inventory(),
            current_workspace_id=CURRENT,
        )
        changed = (
            build_workspace_retirement_plan(
                observation=_observation(project_name="other"),
                inventory=_inventory(),
                current_workspace_id=CURRENT,
            ),
            build_workspace_retirement_plan(
                observation=_observation(record_digest="c" * 64),
                inventory=_inventory(),
                current_workspace_id=CURRENT,
            ),
            build_workspace_retirement_plan(
                observation=_observation(updated_at="2026-08-04T00:00:00+00:00"),
                inventory=_inventory(),
                current_workspace_id=CURRENT,
            ),
            build_workspace_retirement_plan(
                observation=_observation(),
                inventory=_inventory(target_agent_set_digest="d" * 64),
                current_workspace_id=CURRENT,
            ),
            build_workspace_retirement_plan(
                observation=_observation(),
                inventory=_inventory(),
                current_workspace_id="another-current",
            ),
        )
        self.assertTrue(all(result.ok for result in changed))
        self.assertTrue(all(result.plan_digest != first.plan_digest for result in changed))

    def test_returned_plan_is_fresh_and_cannot_mutate_digest_authority(self) -> None:
        result = build_workspace_retirement_plan(
            observation=_observation(),
            inventory=_inventory(),
            current_workspace_id=CURRENT,
        )
        payload = result.plan
        payload["target"]["record_digest"] = "f" * 64
        self.assertEqual(result.plan["target"]["record_digest"], "a" * 64)


class _Registry:
    def __init__(self, observations, *, retired=None, store_outcome=None):
        self.observations = list(observations)
        self.retired = retired
        self.store_outcome = store_outcome or WorkspaceRetirementStoreOutcome(
            True, backup_receipt="receipt"
        )
        self.writes = []

    def observe(self, workspace_id):
        if not self.observations:
            return None
        return self.observations.pop(0)

    def observe_retired(self, workspace_id, plan_digest):
        return self.retired

    def retire(self, **kwargs):
        self.writes.append(kwargs)
        return self.store_outcome


class _UnreadableRegistry(_Registry):
    def observe(self, workspace_id):
        raise WorkspaceRetirementAuthorityError("unreadable")


class _InvalidReplayRegistry(_Registry):
    def observe_retired(self, workspace_id, plan_digest):
        raise WorkspaceRetirementAuthorityError("replay_activity_present")


class _Inventory:
    def __init__(self, observations):
        self.observations = list(observations)

    def observe(self, workspace_id):
        return self.observations.pop(0)


class WorkspaceRetirementUseCaseTests(unittest.TestCase):
    def _preview(self):
        return build_workspace_retirement_plan(
            observation=_observation(),
            inventory=_inventory(),
            current_workspace_id=CURRENT,
        )

    def test_execute_rechecks_both_authorities_before_one_write(self) -> None:
        preview = self._preview()
        registry = _Registry([_observation(), _observation()])
        use_case = WorkspaceRetirementUseCase(
            registry=registry,
            inventory=_Inventory([_inventory(), _inventory()]),
        )
        result = use_case.run(
            workspace_id=TARGET,
            current_workspace_id=CURRENT,
            execute=True,
            expected_plan_digest=preview.plan_digest,
        )
        self.assertEqual(result.state, STATE_RETIRED)
        self.assertEqual(len(registry.writes), 1)
        self.assertEqual(registry.writes[0]["plan_digest"], preview.plan_digest)

    def test_action_time_drift_is_zero_write(self) -> None:
        preview = self._preview()
        registry = _Registry(
            [_observation(), _observation(record_digest="c" * 64)]
        )
        use_case = WorkspaceRetirementUseCase(
            registry=registry,
            inventory=_Inventory([_inventory(), _inventory()]),
        )
        result = use_case.run(
            workspace_id=TARGET,
            current_workspace_id=CURRENT,
            execute=True,
            expected_plan_digest=preview.plan_digest,
        )
        self.assertEqual(result.reason, "action_time_drift")
        self.assertEqual(registry.writes, [])

    def test_verified_backup_makes_replay_idempotent(self) -> None:
        preview = self._preview()
        registry = _Registry([None], retired=_observation())
        result = WorkspaceRetirementUseCase(
            registry=registry,
            inventory=_Inventory([_inventory()]),
        ).run(
            workspace_id=TARGET,
            current_workspace_id=CURRENT,
            execute=True,
            expected_plan_digest=preview.plan_digest,
        )
        self.assertEqual(result.state, STATE_ALREADY_RETIRED)
        self.assertEqual(registry.writes, [])

    def test_unreadable_registry_is_not_misreported_as_an_unknown_target(self) -> None:
        result = WorkspaceRetirementUseCase(
            registry=_UnreadableRegistry([]),
            inventory=_Inventory([_inventory()]),
        ).run(
            workspace_id=TARGET,
            current_workspace_id=CURRENT,
        )
        self.assertEqual(result.reason, "registry_unreadable")

    def test_unproven_replay_is_not_reported_as_already_retired(self) -> None:
        result = WorkspaceRetirementUseCase(
            registry=_InvalidReplayRegistry([None]),
            inventory=_Inventory([_inventory()]),
        ).run(
            workspace_id=TARGET,
            current_workspace_id=CURRENT,
            execute=True,
            expected_plan_digest="a" * 64,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "retirement_failed")
        self.assertEqual(result.detail, "retirement_replay_not_proven")

    def test_invalid_digest_is_rejected_before_backup_lookup(self) -> None:
        registry = _Registry([None], retired=_observation())
        result = WorkspaceRetirementUseCase(
            registry=registry,
            inventory=_Inventory([_inventory()]),
        ).run(
            workspace_id=TARGET,
            current_workspace_id=CURRENT,
            execute=True,
            expected_plan_digest="../outside",
        )
        self.assertEqual(result.reason, "invalid_observation")
        self.assertEqual(registry.writes, [])


if __name__ == "__main__":
    unittest.main()
