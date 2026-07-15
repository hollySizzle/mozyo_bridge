"""Pure dispatch-authority decision tests (Redmine #13489 increment 2).

Only a valid, non-superseded authorization whose exact target is a single ``awaiting_input``
slot yields AUTHORIZE; every other combination is zero send (MONITOR or BLOCKED).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (
    DispatchAuthorization,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authority import (
    AUTHORIZE,
    BLOCKED,
    MONITOR,
    REASON_AUTHORIZATION_INVALID,
    REASON_AUTHORIZATION_SUPERSEDED,
    REASON_NO_AUTHORIZATION,
    REASON_RUNTIME_NOT_READY,
    REASON_RUNTIME_UNAVAILABLE,
    REASON_RUNTIME_UNKNOWN,
    REASON_TARGET_ABSENT,
    REASON_TARGET_AMBIGUOUS,
    TARGET_ABSENT,
    TARGET_AMBIGUOUS,
    TARGET_AWAITING_INPUT,
    TARGET_BLOCKED,
    TARGET_BUSY,
    TARGET_TURN_ENDED,
    TARGET_UNAVAILABLE,
    TARGET_UNKNOWN,
    decide_dispatch_authority,
)


def _auth(**over) -> DispatchAuthorization:
    fields = dict(
        action_id="act-1",
        source_gate="74999",
        issue="13489",
        workspace_id="ws1",
        lane_id="issue_13489",
        target_role="implementation_worker",
        target_assigned_name="mzb1_ws1_claude_issue_13489",
        action="dispatch_worker",
        conclusion="authorized",
        authorized_by_role="coordinator",
        journal="75010",
    )
    fields.update(over)
    return DispatchAuthorization(**fields)


class DecideTest(unittest.TestCase):
    def test_authorize_when_valid_and_awaiting_input(self):
        d = decide_dispatch_authority(
            authorization=_auth(), superseded=False, target_runtime=TARGET_AWAITING_INPUT
        )
        self.assertEqual(d.decision, AUTHORIZE)
        self.assertTrue(d.authorized)
        self.assertIsNotNone(d.authorization)

    def test_no_authorization_is_monitor(self):
        d = decide_dispatch_authority(
            authorization=None, superseded=False, target_runtime=TARGET_AWAITING_INPUT
        )
        self.assertEqual(d.decision, MONITOR)
        self.assertEqual(d.reason, REASON_NO_AUTHORIZATION)
        self.assertIsNone(d.authorization)

    def test_invalid_authorization_is_blocked(self):
        d = decide_dispatch_authority(
            authorization=_auth(action="retire"),
            superseded=False,
            target_runtime=TARGET_AWAITING_INPUT,
        )
        self.assertEqual(d.decision, BLOCKED)
        self.assertEqual(d.reason, REASON_AUTHORIZATION_INVALID)

    def test_superseded_is_monitor(self):
        d = decide_dispatch_authority(
            authorization=_auth(), superseded=True, target_runtime=TARGET_AWAITING_INPUT
        )
        self.assertEqual(d.decision, MONITOR)
        self.assertEqual(d.reason, REASON_AUTHORIZATION_SUPERSEDED)

    def test_busy_blocked_turn_ended_are_monitor(self):
        for rt in (TARGET_BUSY, TARGET_BLOCKED, TARGET_TURN_ENDED):
            d = decide_dispatch_authority(
                authorization=_auth(), superseded=False, target_runtime=rt
            )
            self.assertEqual(d.decision, MONITOR, rt)
            self.assertEqual(d.reason, REASON_RUNTIME_NOT_READY, rt)

    def test_absent_target_is_blocked(self):
        d = decide_dispatch_authority(
            authorization=_auth(), superseded=False, target_runtime=TARGET_ABSENT
        )
        self.assertEqual(d.decision, BLOCKED)
        self.assertEqual(d.reason, REASON_TARGET_ABSENT)

    def test_ambiguous_target_is_blocked(self):
        d = decide_dispatch_authority(
            authorization=_auth(), superseded=False, target_runtime=TARGET_AMBIGUOUS
        )
        self.assertEqual(d.decision, BLOCKED)
        self.assertEqual(d.reason, REASON_TARGET_AMBIGUOUS)

    def test_unavailable_inventory_is_blocked(self):
        d = decide_dispatch_authority(
            authorization=_auth(), superseded=False, target_runtime=TARGET_UNAVAILABLE
        )
        self.assertEqual(d.decision, BLOCKED)
        self.assertEqual(d.reason, REASON_RUNTIME_UNAVAILABLE)

    def test_unknown_runtime_is_blocked(self):
        d = decide_dispatch_authority(
            authorization=_auth(), superseded=False, target_runtime=TARGET_UNKNOWN
        )
        self.assertEqual(d.decision, BLOCKED)
        self.assertEqual(d.reason, REASON_RUNTIME_UNKNOWN)

    def test_only_awaiting_input_authorizes(self):
        # Exhaustive: no non-awaiting_input token yields AUTHORIZE (zero-send guarantee).
        for rt in (
            TARGET_BUSY,
            TARGET_BLOCKED,
            TARGET_TURN_ENDED,
            TARGET_UNKNOWN,
            TARGET_ABSENT,
            TARGET_AMBIGUOUS,
            TARGET_UNAVAILABLE,
        ):
            d = decide_dispatch_authority(
                authorization=_auth(), superseded=False, target_runtime=rt
            )
            self.assertNotEqual(d.decision, AUTHORIZE, rt)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
