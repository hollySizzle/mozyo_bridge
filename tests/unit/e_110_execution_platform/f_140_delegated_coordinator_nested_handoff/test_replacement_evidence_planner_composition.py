"""The planner's LIVE composition (Redmine #14741 j#97093 decision 1).

The planner's own contract is pinned without any store next door; this file pins the other
half: that the ports are bound to the real authorities, that the expected launch cause is
the registry's canonical token rather than a second literal in e_110, and that a refusal
arrives as a typed reason instead of an exception at a caller that is about to close a pane.

Every store here is built under a temp home. Nothing reads the operator's shared home.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_launch_generation import (  # noqa: E402
    HerdrLaunchGenerationStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    ParticipantPin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_evidence_planner_composition import (  # noqa: E402,E501
    EvidencePlanning,
    build_evidence_planner,
    plan_participants_with_evidence,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E402,E501
    LAUNCH_CAUSE_UPDATE_RELAUNCH,
)
from tests.support.current_launch_authority import seed_completed_current_generation

WORKSPACE = "ws"
LANE = "issue_14741"
ASSIGNED = "mzb1_ws_codex_lane"
LEGACY_ACTION = "startup-" + "b" * 64


def _pin(**kw) -> ParticipantPin:
    base = dict(
        lane_id=LANE,
        role="gateway",
        provider="codex",
        assigned_name=ASSIGNED,
        old_locator="ws:p1",
        lane_revision="7",
        lane_generation="lane-gen-1",
    )
    base.update(kw)
    return ParticipantPin(**base)


class CompositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())

    def _plan(self, participants=None):
        return plan_participants_with_evidence(
            participants if participants is not None else [_pin()],
            home=self.home,
            workspace_id=WORKSPACE,
            lane_id=LANE,
            live_rows=(),
        )

    def test_the_expected_cause_is_the_registry_token(self) -> None:
        """Not re-spelled in e_110: the same object the provider registry exports."""
        self.assertEqual(LAUNCH_CAUSE_UPDATE_RELAUNCH, "update_relaunch")
        planner = build_evidence_planner(self.home, live_rows=())
        self.assertEqual(
            planner._update_cause("codex", "update_prompt_available"),
            LAUNCH_CAUSE_UPDATE_RELAUNCH,
        )

    def test_a_non_update_blocker_is_not_a_cause(self) -> None:
        """A trust or login screen is not an update, and neither is an unknown provider."""
        planner = build_evidence_planner(self.home, live_rows=())
        for provider, blocker in (
            ("codex", "trust_prompt"),
            ("codex", ""),
            ("no_such_provider", "update_prompt_available"),
        ):
            with self.subTest(provider=provider, blocker=blocker):
                self.assertEqual(planner._update_cause(provider, blocker), "")

    def test_the_ports_read_the_temp_home_and_not_the_shared_one(self) -> None:
        """The generation port is bound to THIS home: seeding it changes the answer."""
        live = [{"name": ASSIGNED, "pane_id": "ws:p1",
                 "terminal_id": "terminal:ws:p1"}]
        planner = build_evidence_planner(self.home, live_rows=live)
        self.assertIsNone(planner._generations(ASSIGNED))
        action = seed_completed_current_generation(
            self.home, assigned_name=ASSIGNED, workspace_id=WORKSPACE,
            lane_id=LANE, role="gateway", locator="ws:p1",
        )
        found = planner._generations(ASSIGNED)
        self.assertIsNotNone(found)
        self.assertEqual(found.startup_action_id, action)

    def test_an_unreadable_authority_is_a_typed_reason_not_an_exception(self) -> None:
        """A caller about to close a live pane must never receive a raw store error."""
        answer = self._plan()
        self.assertIsInstance(answer, EvidencePlanning)
        self.assertTrue(answer.refused)
        self.assertEqual(answer.participants, ())

    def test_an_empty_home_refuses_because_no_generation_is_recorded(self) -> None:
        """MEASURED, and reported to j#97093: this is why the five sites are not wired yet.

        A home with no launch-generation row cannot say which startup action a participant
        belongs to, so the planner refuses ``generation_unavailable``. Every legacy lane is
        in exactly that state, so wiring the five paths on top of this contract turns their
        execute leg into a refusal. The interaction is a ruling, not a thing to paper over
        here, and this test states the current behaviour rather than a preferred one.
        """
        self.assertEqual(self._plan().refusal, "generation_unavailable")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
