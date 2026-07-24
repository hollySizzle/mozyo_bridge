"""Scenario: 即時 turn_ended → guarded gateway refresh → 既存 work 継続 (Redmine #14203).

The deterministic end-to-end shape of the #14203 acceptance: a same-lane
implementation_gateway ends its provider turn seconds after a confirmed callback delivery
with NO expected durable gate landed (the five-lane dogfood reproduction), the guarded
refresh closes ONLY that gateway generation and relaunches the same durable slot, and the
fresh gateway resumes the EXISTING durable anchor exactly once — never a regenerated
Implementation Request / Review Request, never a blind resend, and never a close on the
#14219-shaped false negative (an unconfirmed delivery that actually landed). Fakes only.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    PHASE_COMPLETED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery import (  # noqa: E402,E501
    GatewayRefreshRequest,
    GatewayRefreshUseCase,
    REFRESH_STATUS_COMPLETED,
    REFRESH_STATUS_REFUSED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E402,E501
    REFRESH_ACTIONABLE,
    REFRESH_BLOCK_TURN_NOT_FAILED,
    TURN_CLASS_FAILED,
    TURN_CLASS_PRODUCTIVE,
    TURN_CLASS_UNCONFIRMED,
)

# The regression module carries the shared fakes (the scenario composes them 1:1).
from tests.regressions.test_issue_14203_gateway_refresh import (  # noqa: E402
    ACTION_ID,
    FakeActuatorPort,
    FakeGatewayOps,
    GATEWAY,
    GEN,
    _target,
    _turn,
)

FIXED = "2026-07-24T15:00:00+00:00"


class GatewayRefreshResumeScenario(unittest.TestCase):
    """The #14203 acceptance flow, deterministic and process-free."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)
        self.port = FakeActuatorPort()

    def _request(self) -> GatewayRefreshRequest:
        return GatewayRefreshRequest(
            issue="14203", lane=GATEWAY["lane_id"], role=GATEWAY["role"],
            provider=GATEWAY["provider"], assigned_name=GATEWAY["assigned_name"],
            locator=GATEWAY["old_locator"], journal="84223", action_id=ACTION_ID,
            action_generation=GEN, lane_revision="5", lane_generation="2",
            resume_anchor_journal="83755", resume_gate="review_request",
        )

    def _use_case(self, ops) -> GatewayRefreshUseCase:
        return GatewayRefreshUseCase(
            self.store, self.port, ops, workspace_id="ws", clock=lambda: FIXED,
        )

    def test_immediate_turn_end_guarded_refresh_existing_work_resumes(self):
        # 1. The reproduction: callback delivery `sent`, turn `started`, the gateway
        #    settled back to turn_ended seconds later, and a FRESH anchored durable re-read
        #    confirms the expected gate never landed. Classification: turn failed.
        ops = FakeGatewayOps(turn=_turn(), target=_target())
        preflight = self._use_case(ops).run(self._request(), execute=False)
        self.assertEqual(preflight.turn_class, TURN_CLASS_FAILED)
        self.assertEqual(preflight.verdict, REFRESH_ACTIONABLE)
        self.assertEqual(self.port.closed, [])  # a preflight closes nothing

        # 2. The owner-approved guarded refresh: ONLY the exact pinned gateway generation
        #    is closed, the same durable slot is relaunched action-bound + attested, and
        #    the EXISTING review_request anchor is resumed exactly once.
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_COMPLETED)
        gateway_identity = (
            GATEWAY["lane_id"], GATEWAY["role"], GATEWAY["provider"],
            GATEWAY["assigned_name"],
        )
        self.assertEqual(self.port.closed, [gateway_identity])   # nothing else was closed
        self.assertEqual(len(ops.resumes), 1)
        self.assertEqual(ops.resumes[0].journal_id, "83755")     # the EXISTING anchor
        self.assertEqual(ops.resumes[0].expected_gate, "review_request")

        # 3. Existing work continues: the durable transaction is completed and a replay is
        #    idempotent — no second close, no duplicate delivery of the anchor.
        rec = self.store.get(ReplacementTransactionKey("ws", ACTION_ID))
        self.assertEqual(rec.phase, PHASE_COMPLETED)
        replay_ops = FakeGatewayOps(turn=_turn(), target=_target(), already_landed=True)
        replay = self._use_case(replay_ops).run(self._request(), execute=True)
        self.assertEqual(replay.status, REFRESH_STATUS_COMPLETED)
        self.assertEqual(self.port.closed, [gateway_identity])   # still exactly one close
        self.assertEqual(replay_ops.resumes, [])                 # zero duplicate delivery

    def test_the_14219_false_negative_shape_never_closes_the_gateway(self):
        # The same lane, but the delivery confirmation is missing (the wait timed out —
        # the shape that twice turned out to be a REAL landing in the #14219 dogfood): the
        # classification is unconfirmed and the refresh refuses, zero close.
        ops = FakeGatewayOps(turn=_turn(delivery_confirmed=False), target=_target())
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.turn_class, TURN_CLASS_UNCONFIRMED)
        self.assertEqual(outcome.verdict, REFRESH_BLOCK_TURN_NOT_FAILED)
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)
        self.assertEqual(self.port.closed, [])
        self.assertEqual(ops.resumes, [])

    def test_a_landed_gate_is_productive_and_never_refreshed(self):
        # The durable journal is the authority: once the expected gate lands, the turn is
        # productive — no refresh regardless of how brief the runtime turn looked.
        ops = FakeGatewayOps(
            turn=_turn(expected_gate_landed=True, expected_gate_absent=False),
            target=_target(),
        )
        outcome = self._use_case(ops).run(self._request(), execute=True)
        self.assertEqual(outcome.turn_class, TURN_CLASS_PRODUCTIVE)
        self.assertEqual(outcome.status, REFRESH_STATUS_REFUSED)
        self.assertEqual(self.port.closed, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
