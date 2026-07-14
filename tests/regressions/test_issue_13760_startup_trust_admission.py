"""Redmine #13760 — a receiver on a provider startup screen must consume ZERO bytes.

The defect, exactly as it happened live (#13582 j#77917 / j#77937 / j#77939): a fresh
worktree's managed Claude worker was sitting on the trust confirmation. Every signal the
dispatch path had said "ready" — ``sublane readiness --json`` reported ``status=ok``, the
herdr agent status read ``unknown`` before AND after the screen, and the pane was live
with non-blank content. So the same-lane gateway's high-level ``handoff send --kind
implementation_request --mode queue-enter`` typed the Implementation Request into a
screen that has no composer, and the Enter retry was absorbed as the dialog's **default
Yes**. The request body was destroyed. The transport recorded ``sent / queue_enter``, and
the coordinator's durable record therefore projected a dispatch the worker never saw.

This file pins the whole chain, not one convenient half of it (the recurring trap: a
guard that is real at one layer and fictional at the next). Three properties, in the
order the failure travelled:

1. the shared herdr send boundary refuses BEFORE injection — zero ``send_text``, zero
   ``send_keys``, no marker, no ACK, in queue-enter (the live mode) and in standard;
2. the refusal is a NONZERO exit — because that is the only thing ``sublane
   dispatch-worker`` measures;
3. fed that real exit code, the worker dispatcher projects ``gateway_notified`` /
   ``worker_dispatch_confirmed=false`` — so a startup-blocked lane can never look
   dispatched (j#77947 invariant 6).

Anything that makes a startup screen deliverable again — deleting the profile blockers,
moving the gate off the shared boundary, letting an unreadable pane decay to "clear" —
fails here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatcher import (  # noqa: E501
    WorkerDispatchUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_BLOCKED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (  # noqa: E501
    WORKER_DISPATCH_DELIVERY_FAILED,
)

# Imported as MODULES, not as names: pulling a `TestCase` subclass into this namespace
# would make the loader collect and re-run the whole imported suite here.
from tests.integration.e_110_execution_platform.f_130_handoff_routing import (  # noqa: E501
    test_herdr_transport_wiring as _wiring,
)
from tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff import (  # noqa: E501
    test_sublane_worker_dispatcher as _dispatch,
)

IDLE_COMPOSER = _wiring.IDLE_COMPOSER
TRUST_SCREEN = _wiring.TRUST_SCREEN


class Issue13760StartupTrustAdmissionTest(unittest.TestCase):
    """The j#77917 dispatch, replayed end to end against a receiver on the trust screen."""

    # The real `handoff send` path (orchestrate_handoff, real gates, real rails), faked
    # only at the herdr CLI boundary.
    _run = _wiring.PureHerdrEndToEndTest._run

    def _send_to_receiver_showing(self, screen, *, mode="queue-enter"):
        herdr = _wiring._FakeHerdr(
            [], get_states=["idle"], wait_results=[(0, "")], pane_content=screen
        )
        result, herdr, _ws, out, err = self._run(
            agent_rows_fn=_wiring._same_lane_rows(), herdr=herdr, mode=mode
        )
        return result, herdr, _wiring._outcome_from(out), out, err

    def test_j77917_queue_enter_into_a_trust_screen_types_nothing(self) -> None:
        result, herdr, outcome, out, err = self._send_to_receiver_showing(TRUST_SCREEN)

        # (1) Zero bytes. This is the assertion that would have saved the request: the
        # pre-#13760 rail typed the body here and then pressed Enter into the dialog.
        typed = [op for op in herdr.sends if op[0] in ("send_text", "send_keys")]
        self.assertEqual(typed, [], msg=f"the receiver was written to: {herdr.sends}")

        # No marker was ever built for a send that did not happen.
        self.assertIsNone(outcome.get("notification_marker"), msg=out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(
            outcome.get("reason"), "receiver_startup_interaction_required", msg=out
        )

        # (2) Nonzero exit — the only thing the outer dispatcher can measure.
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")

    def test_the_refusal_never_reports_a_delivery(self) -> None:
        _result, _herdr, outcome, out, _err = self._send_to_receiver_showing(TRUST_SCREEN)
        # `sent` is the token every downstream projection (glance, supervisor, the
        # coordinator's own record) reads as "the worker has it". A startup refusal must
        # never produce it, and must never be a positive queue-enter either.
        self.assertNotEqual(outcome.get("status"), "sent", msg=out)
        self.assertNotEqual(outcome.get("reason"), "queue_enter", msg=out)
        self.assertEqual(outcome.get("next_action_owner"), "operator", msg=out)

    def test_startup_blocked_send_projects_worker_dispatch_unconfirmed(self) -> None:
        # (3) The chain: feed the dispatcher the EXIT CODE the real gate just produced,
        # not a hand-picked `1`. A guard that fails closed at the transport but promotes
        # to `worker_dispatched` at the lane is not a guard.
        result, _herdr, _outcome, out, err = self._send_to_receiver_showing(TRUST_SCREEN)
        # The gate fails closed through `die()` == SystemExit, which is what the composed
        # inner CLI hands the dispatcher. Take its REAL code — a rewrite that made the
        # refusal exit 0 must break this test, not be papered over with a literal 1.
        rc = result.code if isinstance(result, SystemExit) else result
        self.assertIsInstance(rc, int, msg=f"out={out}\nerr={err}")
        self.assertNotEqual(rc, 0, msg=f"out={out}\nerr={err}")

        ops = _dispatch.FakeWorkerDispatchOps(lane=_dispatch._lane(), dispatch_rc=rc)
        dispatch = WorkerDispatchUseCase(ops).run(_dispatch._req(), execute=True)

        self.assertEqual(dispatch.status, ACTUATE_BLOCKED)
        self.assertEqual(dispatch.dispatch_result, WORKER_DISPATCH_DELIVERY_FAILED)
        self.assertFalse(dispatch.worker_dispatch_confirmed)
        # The lane's recorded state stays where it was: the gateway was notified, the
        # worker was not. j#77951 recorded exactly this shape by hand; now it is derived.
        self.assertIn("gateway_notified", dispatch.reason)

    def test_a_started_up_receiver_still_gets_the_same_anchor(self) -> None:
        # The other half of a real guard: it must not brick the happy path. The same
        # command, the same anchor, a receiver that has finished starting up — delivered,
        # with the body typed exactly once.
        result, herdr, outcome, out, err = self._send_to_receiver_showing(IDLE_COMPOSER)
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertIsNone(outcome.get("startup_admission"), msg=out)
        send_texts = [op for op in herdr.sends if op[0] == "send_text"]
        self.assertEqual(len(send_texts), 1, msg=herdr.sends)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
