"""Regression: foreign-sender dispatch-worker preflight before the outbox fence (#14192).

The dogfood failure (#13729 j#83970 / blocker j#83986): running public ``sublane
dispatch-worker`` from a *coordinator* shell returned ``admission_decision=healthy`` on
``--dry-run`` and, on ``--execute``, RESERVED the exactly-once outbox fence and only THEN
hit the inner ``handoff send`` gateway-route enforcement (``gateway_route_blocked`` — text /
Enter 0, a proven known-not-sent). The adapter folded that non-zero into ``uncertain``,
poisoning the exact key so every subsequent dispatch from the *correct* same-lane gateway
zero-sent with ``exact send key already uncertain; prior injection is never replayed`` — the
worker was undispatchable without minting a fresh generation.

This pins the fix end-to-end through the real :class:`WorkerDispatchUseCase` +
:class:`HerdrWorkerDispatchOps` + real :class:`DispatchOutboxFence`:

- a foreign / cross-lane sender fails closed BEFORE any fence write, on BOTH dry-run and
  execute, with an identical verdict (Acceptance #1 / #2), and
- the correct same-lane gateway then reserves the SAME pristine key and delivers, WITHOUT a
  fresh generation (Acceptance #5).

Hermetic: no live herdr, no tmux, no Redmine — the inner send + turn-start + lane read-back +
worker admission are injected, while the sender preflight and the outbox fence run for real.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    FENCE_ABSENT,
    FENCE_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatch_herdr_ops import (  # noqa: E501
    HerdrWorkerDispatchOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher import (  # noqa: E501
    WorkerDispatchUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (  # noqa: E501
    ADMISSION_HEALTHY,
    REASON_FOREIGN_SENDER,
    WorkerDispatchAdmission,
    WorkerDispatchAdmissionFacts,
    WorkerDispatchRequest,
)

WS = "ws"
LANE = "issue_14192_lane"
ISSUE = "14192"
JOURNAL = "84026"


def _healthy_admission() -> WorkerDispatchAdmission:
    return WorkerDispatchAdmission(
        ADMISSION_HEALTHY,
        "healthy",
        WorkerDispatchAdmissionFacts(
            True,
            True,
            True,
            True,
            "live",
            True,
            "awaiting_input",
            generation_binding_current=True,
            lane_generation=1,
            worker_assigned_name="mzb1_ws_claude_lane",
            workspace_id=WS,
            lane_id=LANE,
            action_id="lane_generation_1",
        ),
    )


def _lane() -> SimpleNamespace:
    return SimpleNamespace(
        workspace_id=WS,
        lane_id=LANE,
        lane_label=LANE,
        worker_pane="w2X:p2R",
        gateway_pane="w2X:p2Q",
        issue=ISSUE,
    )


class ForeignSenderPreflightRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.request = WorkerDispatchRequest(ISSUE, LANE, "/repo", JOURNAL)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *, sender_lane, execute, send_rc=0, send_known_not_sent=False):
        """Drive `dispatch-worker` end-to-end with the given sender lane identity."""
        env = {
            "MOZYO_WORKSPACE_ID": WS,
            "MOZYO_AGENT_ROLE": "codex",
            "MOZYO_LANE_ID": sender_lane,
            "MOZYO_BRIDGE_HOME": self.home,
        }
        ops = HerdrWorkerDispatchOps(Path("/repo"), LANE, ISSUE, env=env)
        use_case = WorkerDispatchUseCase(ops, worker_ready_probes=0)
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": self.home}, clear=False)
            )
            stack.enter_context(
                patch.object(ops, "read_lane", return_value=_lane())
            )
            stack.enter_context(
                patch.object(
                    ops,
                    "observe_worker_dispatch_admission",
                    return_value=_healthy_admission(),
                )
            )
            stack.enter_context(
                patch.object(ops, "worker_provider", return_value="claude")
            )
            stack.enter_context(
                patch.object(ops, "_observe_worker_turn_start", return_value="started")
            )
            # Resolve the sender workspace anchor to WS so the same-lane gateway env
            # attests; the legacy fallback is inert here.
            stack.enter_context(
                patch(
                    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology.herdr_workspace_segment",
                    return_value=WS,
                )
            )
            stack.enter_context(
                patch(
                    "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_send_entry._legacy_lane_token",
                    return_value="",
                )
            )
            stack.enter_context(
                patch(
                    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution.resolve_gateway_provider",
                    return_value="codex",
                )
            )
            stack.enter_context(
                patch(
                    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_dispatch_execution.target_is_retiring",
                    return_value=(False, ""),
                )
            )
            # The inner send is injected: `(rc, known_not_sent)`. The gateway leg delivers.
            stack.enter_context(
                patch(
                    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher._drive_worker_send_argv",
                    return_value=(send_rc, send_known_not_sent),
                )
            )
            return use_case.run(self.request, execute=execute)

    def _fence_state(self):
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": self.home}, clear=False):
            key = HerdrWorkerDispatchOps._fence_key(_healthy_admission(), self.request)
            return DispatchOutboxFence().state_of(key)

    def test_foreign_sender_dry_run_and_execute_block_with_zero_write(self):
        # The coordinator's lane ("default") differs from the target sublane.
        dry = self._run(sender_lane="default", execute=False)
        ex = self._run(sender_lane="default", execute=True)
        # Acceptance #1 / #2: identical fail-closed verdict on dry-run and execute.
        self.assertEqual(dry.status, ACTUATE_BLOCKED)
        self.assertEqual(ex.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_FOREIGN_SENDER, dry.blocked_reasons)
        self.assertIn(REASON_FOREIGN_SENDER, ex.blocked_reasons)
        # Acceptance #1: zero fence write — the exact key was never reserved / poisoned.
        self.assertEqual(self._fence_state(), FENCE_ABSENT)

    def test_correct_gateway_succeeds_after_foreign_attempts_no_fresh_generation(self):
        # 1. The foreign sender's mistaken dry-run + execute (zero write).
        self._run(sender_lane="default", execute=False)
        self._run(sender_lane="default", execute=True)
        self.assertEqual(self._fence_state(), FENCE_ABSENT)

        # 2. The CORRECT same-lane gateway (its own lane == the target lane) now dispatches
        #    the SAME (issue, journal, action, target) — no fresh generation minted.
        outcome = self._run(sender_lane=LANE, execute=True)
        self.assertEqual(outcome.status, ACTUATE_EXECUTED)
        self.assertTrue(outcome.worker_dispatch_confirmed)
        # The exact key reserved fresh and delivered — never blocked as "already uncertain".
        self.assertEqual(self._fence_state(), FENCE_DELIVERED)

    def test_same_lane_gateway_proven_not_sent_cancels_not_uncertain(self):
        # Acceptance #3 end-to-end: a legitimate same-lane gateway passes the preflight and
        # reserves, but the inner rail PROVES a pre-injection zero-send (`known_not_sent`).
        # The exact key is CANCELLED (an honest never-replay terminal), NOT poisoned to the
        # reconcile-only `uncertain` — and a same-key retry stays never-send (exactly-once).
        from mozyo_bridge.core.state.dispatch_outbox_fence import (
            FENCE_CANCELLED,
            FENCE_UNCERTAIN,
        )

        outcome = self._run(
            sender_lane=LANE, execute=True, send_rc=2, send_known_not_sent=True
        )
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        state = self._fence_state()
        self.assertEqual(state, FENCE_CANCELLED)
        self.assertNotEqual(state, FENCE_UNCERTAIN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
