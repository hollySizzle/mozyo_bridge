"""Typed embedded startup projection tests (Redmine #13948 R2)."""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_startup_projection import (  # noqa: E501
    project_sublane_startup,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E501
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    COMPENSATION_NOT_NEEDED,
    COMPENSATION_ROLLBACK_OWED,
    HEALTH_HEALTHY,
    HEALTH_PROVIDER_EXITED,
    HEALTH_RECEIVER_UNREADABLE,
)


class SublaneActuatorStartupProjectionTest(unittest.TestCase):
    @staticmethod
    def _result(*slots, action_id="act_projection"):
        return SessionStartResult(
            workspace_id="ws",
            lane_id="issue_13948_projection",
            slots=list(slots),
            action_id=action_id,
        )

    @staticmethod
    def _slot(provider, *, outcome=SLOT_LAUNCHED, health=HEALTH_HEALTHY,
              compensation=COMPENSATION_NOT_NEEDED, detail=""):
        return SlotResult(
            provider=provider,
            assigned_name=f"mzb1_ws_{provider}_issue_13948_projection",
            outcome=outcome,
            locator="private:locator",
            detail="raw launch detail must not propagate",
            health=health,
            compensation=compensation,
            health_detail=detail,
        )

    def test_healthy_result_maps_without_backend_locator_or_raw_detail(self):
        projection = project_sublane_startup(
            self._result(self._slot("codex"), self._slot("claude"))
        )

        self.assertTrue(projection.ok)
        self.assertEqual(projection.action_id, "act_projection")
        self.assertFalse(projection.rollback_owed)
        payload = projection.as_payload()
        self.assertEqual([role["provider"] for role in payload["roles"]], ["codex", "claude"])
        self.assertNotIn("locator", str(payload))
        self.assertNotIn("raw launch detail", str(payload))

    def test_mixed_adopted_and_fresh_failure_owes_only_fresh_rollback(self):
        projection = project_sublane_startup(
            self._result(
                self._slot("codex", outcome=SLOT_ADOPTED),
                self._slot(
                    "claude",
                    health=HEALTH_PROVIDER_EXITED,
                    compensation=COMPENSATION_ROLLBACK_OWED,
                ),
            )
        )

        self.assertFalse(projection.ok)
        self.assertTrue(projection.rollback_owed)
        self.assertEqual(projection.roles[0].disposition, "adopted")
        self.assertEqual(projection.roles[0].compensation, COMPENSATION_NOT_NEEDED)
        self.assertEqual(projection.roles[1].compensation, COMPENSATION_ROLLBACK_OWED)

    def test_uncertain_result_without_debt_stays_nonpositive(self):
        projection = project_sublane_startup(
            self._result(
                self._slot("claude", health=HEALTH_RECEIVER_UNREADABLE),
                action_id="act_uncertain",
            )
        )

        self.assertFalse(projection.ok)
        self.assertFalse(projection.rollback_owed)
        self.assertEqual(projection.roles[0].health, HEALTH_RECEIVER_UNREADABLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
