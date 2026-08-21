"""#15842: queue-enter must not report ``submitted_confirmed`` on a swallowed Enter.

Reproduced from the live incident recorded in #15842 j#109739 (the #15841 dispatch that
stalled for over an hour). ``sublane create`` launched a fresh Codex gateway and
dispatched into it in one operation. The provider TUI was still starting, so the Enter
was absorbed by its startup UI and the marker+body stayed parked in the composer with
``Context 0% used`` — nothing had run. The rail nevertheless reported
``submitted_confirmed``, the dispatcher yielded on that ACK, and the lane sat silent
until an operator pressed Enter by hand. One Enter recovered it: the body was in the
composer all along and only the submit was missing.

The mechanism, and what these pins fix in place:

- a freshly launched pane reads ``awaiting_input`` while its banner is up, so the rail's
  pre-Enter idle baseline check passed;
- the armed working-transition wait then fired on the provider's own **startup** work,
  which the rail could not distinguish from "this turn started";
- with the generation still coherent, all three legs of the causal claim were satisfied
  by an Enter that submitted nothing.

The fix makes the causal series prove submission the way the busy series already did
(ADR-0002 / #15537): a ``changed`` event confirms only once the injected body has
verifiably left the current composer. The pins below cover the false-positive shape, the
unchanged behaviour of a genuine landing, the bounded Enter-only recovery ADR-0002
requires for the recoverable case, the live composer read against a rendered pane rather
than a fake verdict, the closed classification, and the tmux backend's separate posture.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_herdr_queue_enter_rail import (  # noqa: E501
    HerdrQueueEnterSession,
    LiveHerdrQueueEnterOpsMixin,
    QueueEnterResendGate,
    enforce_active_queue_enter_effect_fence,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.queue_enter_submit_proof import (  # noqa: E501
    SUBMIT_PROOF_BODY_RETAINED,
    SUBMIT_PROOF_COMPOSER_CLEARED,
    SUBMIT_PROOF_UNEVALUATED,
    SUBMIT_PROOF_UNPROVEN,
    classify_submit_proof,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    resolve_queue_enter_retry_policy,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_send_semantics import (  # noqa: E501
    MODE_QUEUE_ENTER,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (  # noqa: E501
    STAGE_SUBMITTED_CONFIRMED,
    STAGE_UNCERTAIN_PARTIAL,
    injection_stage_for,
    turn_start_positively_observed,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (  # noqa: E501
    WAIT_CHANGED,
    WAIT_TIMEOUT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_resend_gate import (  # noqa: E501
    RESEND_SKIP_BODY_ABSENT,
    RESEND_SKIP_IDENTITY_DRIFT,
    RESEND_SKIP_NONE,
    RESEND_SKIP_PANE_UNREADABLE,
    RESEND_SKIP_STARTUP_SCREEN,
)


TARGET = "w1V:pT"
RECEIVER = "codex"
ASSIGNED_NAME = "mzb1_ws_codex_lane"
MARKER = "[mozyo:handoff:source=redmine:issue=15841:journal=109727:kind=implementation_request:to=codex]"  # noqa: E501
TEXT = f"{MARKER} implementation request ready for codex."


def _process_generation(terminal_id: str, revision: str) -> str:
    return (
        f"{len(ASSIGNED_NAME)}:{ASSIGNED_NAME}:"
        f"{len(terminal_id)}:{terminal_id}:"
        f"{len(TARGET)}:{TARGET}:r{revision}"
    )


GENERATION: dict[str, str] = {
    "provider": RECEIVER,
    "assigned_name": ASSIGNED_NAME,
    "locator": TARGET,
    "terminal_id": "terminal-a",
    "row_revision": "7",
    "process_generation": _process_generation("terminal-a", "7"),
    "attestation_observed_at": "2026-08-21T11:11:00+00:00",
    "startup_action_id": "startup-324749ec",
}


@dataclass
class _Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Ops:
    """Narrow queue-enter port fake; no body-injection primitive is in reach."""

    def __init__(
        self,
        clock: _Clock,
        *,
        wait_kinds: Optional[list[str]] = None,
        gates: Optional[list[QueueEnterResendGate]] = None,
        baseline_state: str = "awaiting_input",
    ) -> None:
        self.clock = clock
        self._wait_kinds = list(wait_kinds or [WAIT_CHANGED])
        self._gates = list(gates or [])
        self._baseline_state = baseline_state
        self.gate_calls = 0
        self.enter_times: list[float] = []

    # -- observation -----------------------------------------------------
    def observe_queue_enter_runtime_state(self, target: str) -> Optional[str]:
        return self._baseline_state

    def observe_queue_enter_gateway_binding(
        self, target: str
    ) -> Optional[dict[str, str]]:
        return dict(GENERATION)

    def arm_queue_enter_turn_wait(
        self, target: str, *, timeout_ms: int
    ) -> Optional[object]:
        return object()

    def collect_queue_enter_turn_wait(self, armed: object) -> Optional[str]:
        return self._wait_kinds.pop(0) if self._wait_kinds else WAIT_TIMEOUT

    def cancel_queue_enter_turn_wait(self, armed: object) -> None:
        return None

    def queue_enter_turn_wait_pending(self, armed: object) -> bool:
        return True

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict[str, str]],
    ) -> QueueEnterResendGate:
        self.gate_calls += 1
        if self._gates:
            return self._gates.pop(0)
        # Default models the #15841 pane: body still parked in the composer.
        return QueueEnterResendGate(RESEND_SKIP_NONE, "awaiting_input")

    def sleep(self, seconds: float) -> None:
        self.clock.advance(seconds)


@dataclass(frozen=True)
class _Snapshot:
    runtime_state: str = "busy"

    def to_telemetry_dict(self) -> dict[str, object]:
        return {
            "observation_kind": "post_choreography_snapshot",
            "source": "fake",
            "runtime_state": self.runtime_state,
            "read_ok": True,
            "read_reason": None,
            "poll_attempts": 1,
        }


def _drive(ops: _Ops, clock: _Clock, *, window: float = 6.0, interval: float = 2.0):
    """Run the session exactly as the common transport rail drives it."""
    session = HerdrQueueEnterSession(
        ops=ops,
        target=TARGET,
        text=TEXT,
        receiver=RECEIVER,
        expected_assigned_name=ASSIGNED_NAME,
        expected_process_generation=GENERATION["process_generation"],
        retry_policy=resolve_queue_enter_retry_policy(window, interval),
        monotonic=clock,
    )
    assert session.capture_before_body()
    # The marker+body is typed exactly once, by the common rail, right here.
    assert session.arm_before_first_enter()
    session.note_first_enter_sent()

    def _press_extra_enter() -> None:
        enforce_active_queue_enter_effect_fence()
        ops.enter_times.append(clock())

    session.complete_after_first_enter(press_extra_enter=_press_extra_enter)
    return session


def _stage(observation: dict) -> str:
    """Classify exactly as ``make_outcome`` does for a queue-enter ``sent`` / ``ok``."""
    return injection_stage_for(
        "sent",
        "ok",
        mode=MODE_QUEUE_ENTER,
        queue_enter_turn_start_observation=observation,
    )


class FreshPaneSwallowedEnterTest(unittest.TestCase):
    """The #15841 shape: startup activity supplied the transition, nothing submitted."""

    def test_startup_transition_with_body_still_in_composer_is_not_confirmed(
        self,
    ) -> None:
        clock = _Clock()
        # A fresh pane reads idle behind its banner; the armed wait then fires on the
        # provider's own startup work while the composer still holds the body.
        ops = _Ops(clock, wait_kinds=[WAIT_CHANGED], baseline_state="awaiting_input")

        session = _drive(ops, clock)
        observation = session.observation(_Snapshot())

        self.assertFalse(session.causal_start_confirmed)
        self.assertNotIn("event_wait_kind", observation)
        self.assertFalse(turn_start_positively_observed(observation))
        self.assertEqual(_stage(observation), STAGE_UNCERTAIN_PARTIAL)
        self.assertNotEqual(_stage(observation), STAGE_SUBMITTED_CONFIRMED)
        self.assertEqual(session.failure_reason, "turn_start_unconfirmed")

    def test_unconfirmed_shape_names_the_retained_body_in_telemetry(self) -> None:
        clock = _Clock()
        ops = _Ops(clock, wait_kinds=[WAIT_CHANGED])

        session = _drive(ops, clock)
        observation = session.observation(_Snapshot())

        self.assertEqual(observation.get("submit_proof"), SUBMIT_PROOF_BODY_RETAINED)
        self.assertNotIn("submit_proof_refusal", observation)

    def test_body_is_never_retyped_while_only_enter_is_re_pressed(self) -> None:
        clock = _Clock()
        ops = _Ops(clock, wait_kinds=[WAIT_CHANGED])

        _drive(ops, clock)

        # ADR-0002: the recoverable case spends the bounded Enter-only budget. The
        # port has no body-injection primitive at all, so exactly-once is structural.
        self.assertTrue(ops.enter_times)
        self.assertFalse(hasattr(ops, "inject_body"))
        self.assertFalse(hasattr(ops, "send_text"))

    def test_enter_only_recovery_confirms_once_the_composer_releases_the_body(
        self,
    ) -> None:
        clock = _Clock()
        # First changed event: swallowed Enter (body retained). The re-pressed Enter
        # lands, and the next changed event finds the composer cleared. This is the
        # automated form of the single manual Enter that recovered #15841.
        ops = _Ops(
            clock,
            wait_kinds=[WAIT_CHANGED, WAIT_CHANGED],
            gates=[
                QueueEnterResendGate(RESEND_SKIP_NONE, "awaiting_input"),
                QueueEnterResendGate(RESEND_SKIP_NONE, "awaiting_input"),
                QueueEnterResendGate(RESEND_SKIP_NONE, "awaiting_input"),
                QueueEnterResendGate(RESEND_SKIP_BODY_ABSENT),
            ],
        )

        session = _drive(ops, clock)
        observation = session.observation(_Snapshot())

        self.assertEqual(len(ops.enter_times), 1)
        self.assertEqual(session.enter_attempts, 2)
        self.assertTrue(session.causal_start_confirmed)
        self.assertEqual(_stage(observation), STAGE_SUBMITTED_CONFIRMED)
        self.assertEqual(
            observation.get("submit_proof"), SUBMIT_PROOF_COMPOSER_CLEARED
        )

    def test_retry_authorisation_never_outlives_the_observation_behind_it(self) -> None:
        clock = _Clock()
        # First changed event: body retained on an idle baseline, so one Enter-only
        # retry is authorised. The retry's own effect-boundary gate re-reads the
        # receiver as BUSY, which makes the second changed event unattributable — the
        # rail short-circuits before any proof runs. The earlier authorisation must
        # not carry over into it and buy a second Enter.
        ops = _Ops(
            clock,
            wait_kinds=[WAIT_CHANGED, WAIT_CHANGED],
            gates=[
                QueueEnterResendGate(RESEND_SKIP_NONE, "awaiting_input"),
                QueueEnterResendGate(RESEND_SKIP_NONE, "awaiting_input"),
                QueueEnterResendGate(RESEND_SKIP_NONE, "busy"),
            ],
        )

        session = _drive(ops, clock)
        observation = session.observation(_Snapshot())

        self.assertEqual(len(ops.enter_times), 1)
        self.assertEqual(session.enter_attempts, 2)
        self.assertFalse(session.submit_retry_authorised)
        self.assertFalse(session.causal_start_confirmed)
        # The verdict itself stays on the record so the durable note still says WHY
        # the send ended unconfirmed; only the authorisation is per-iteration.
        self.assertEqual(observation.get("submit_proof"), SUBMIT_PROOF_BODY_RETAINED)
        self.assertEqual(_stage(observation), STAGE_UNCERTAIN_PARTIAL)

    def test_startup_screen_refuses_both_confirmation_and_an_extra_enter(self) -> None:
        clock = _Clock()
        # The declared startup screen is up: neither fact was established, so the rail
        # fails closed in BOTH directions rather than pressing Enter into a modal.
        ops = _Ops(
            clock,
            wait_kinds=[WAIT_CHANGED],
            gates=[QueueEnterResendGate(RESEND_SKIP_STARTUP_SCREEN)],
        )

        session = _drive(ops, clock)
        observation = session.observation(_Snapshot())

        self.assertFalse(session.causal_start_confirmed)
        self.assertEqual(ops.enter_times, [])
        self.assertEqual(observation.get("submit_proof"), SUBMIT_PROOF_UNPROVEN)
        self.assertEqual(
            observation.get("submit_proof_refusal"), RESEND_SKIP_STARTUP_SCREEN
        )
        self.assertEqual(_stage(observation), STAGE_UNCERTAIN_PARTIAL)

    def test_unreadable_pane_never_promotes_to_a_confirmed_submission(self) -> None:
        clock = _Clock()
        ops = _Ops(
            clock,
            wait_kinds=[WAIT_CHANGED],
            gates=[QueueEnterResendGate(RESEND_SKIP_PANE_UNREADABLE)],
        )

        session = _drive(ops, clock)
        observation = session.observation(_Snapshot())

        self.assertFalse(session.causal_start_confirmed)
        self.assertEqual(ops.enter_times, [])
        self.assertEqual(_stage(observation), STAGE_UNCERTAIN_PARTIAL)


class EstablishedPaneInvariantTest(unittest.TestCase):
    """A dispatch that really lands keeps reporting ``submitted_confirmed``."""

    def test_landed_enter_on_an_established_pane_is_still_confirmed(self) -> None:
        clock = _Clock()
        # A real submission clears the composer BEFORE the working transition, so the
        # proof is already true by the time the armed wait fires. No extra Enter.
        ops = _Ops(
            clock,
            wait_kinds=[WAIT_CHANGED],
            baseline_state="turn_ended",
            gates=[QueueEnterResendGate(RESEND_SKIP_BODY_ABSENT)],
        )

        session = _drive(ops, clock)
        observation = session.observation(_Snapshot())

        self.assertTrue(session.causal_start_confirmed)
        self.assertEqual(ops.enter_times, [])
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(observation.get("event_wait_kind"), WAIT_CHANGED)
        self.assertEqual(observation.get("baseline_runtime_state"), "turn_ended")
        self.assertTrue(turn_start_positively_observed(observation))
        self.assertEqual(_stage(observation), STAGE_SUBMITTED_CONFIRMED)
        self.assertEqual(
            observation.get("submit_proof"), SUBMIT_PROOF_COMPOSER_CLEARED
        )

    def test_proof_costs_exactly_one_gate_read_on_the_landed_path(self) -> None:
        clock = _Clock()
        ops = _Ops(
            clock,
            wait_kinds=[WAIT_CHANGED],
            baseline_state="turn_ended",
            gates=[QueueEnterResendGate(RESEND_SKIP_BODY_ABSENT)],
        )

        _drive(ops, clock)

        self.assertEqual(ops.gate_calls, 1)

    def test_no_causal_event_leaves_the_proof_unevaluated(self) -> None:
        clock = _Clock()
        # A busy baseline can never yield a causal start, so the causal series never
        # runs a submit proof and the telemetry stays byte-identical to the old shape.
        ops = _Ops(clock, wait_kinds=[WAIT_CHANGED], baseline_state="busy")
        session = HerdrQueueEnterSession(
            ops=ops,
            target=TARGET,
            text=TEXT,
            receiver=RECEIVER,
            expected_assigned_name=ASSIGNED_NAME,
            expected_process_generation=GENERATION["process_generation"],
            retry_policy=resolve_queue_enter_retry_policy(0.0, 2.0),
            monotonic=clock,
        )
        self.assertTrue(session.capture_before_body())
        self.assertTrue(session.arm_before_first_enter())
        session.note_first_enter_sent()
        session.complete_after_first_enter(press_extra_enter=lambda: None)
        observation = session.observation(_Snapshot())

        self.assertEqual(session.submit_proof.kind, SUBMIT_PROOF_UNEVALUATED)
        self.assertNotIn("submit_proof", observation)
        self.assertEqual(ops.gate_calls, 0)
        self.assertEqual(_stage(observation), STAGE_UNCERTAIN_PARTIAL)


class _Reader:
    def __init__(self, rows: list[dict[str, object]], state: str) -> None:
        self.rows = rows
        self.state = state

    def read_agent_state(self, target: str) -> object:
        return SimpleNamespace(ok=True, state=self.state)

    def read_agent_rows(self) -> list[dict[str, object]]:
        return self.rows


class _Rail:
    def __init__(self, rows: list[dict[str, object]], pane: str, state: str) -> None:
        self.reader = _Reader(rows, state)
        self._pane = pane

    def arm_turn_start_wait(self, target: str, *, timeout_ms: int) -> object:
        return object()

    def read_visible_pane(self, target: str) -> str:
        return self._pane


#: The #15841 pane, verbatim in shape: the Codex startup banner above a composer that
#: still holds the marker+body, hard-wrapped mid-token the way the live TUI folds it.
STARTUP_PANE_RETAINING_BODY = (
    ">_ OpenAI Codex (v0.148.0)\n"
    "\n"
    "  To get started, describe a task or try one of these commands:\n"
    "\n"
    f"› {MARKER[:60]}\n"
    f"  {MARKER[60:]} implementation request ready for codex.\n"
    "\n"
    "  ⏎ send   ⇧⏎ newline                            Context 0% used\n"
)

#: The same pane after the Enter actually submitted: the prompt is now scrollback with
#: unindented receiver output beneath it, so the CURRENT composer no longer holds it.
LANDED_PANE = (
    f"› {MARKER} implementation request ready for codex.\n"
    "\n"
    "thinking\n"
    "Reading the durable anchor before acting.\n"
    "\n"
    "› \n"
)


class LiveComposerProofTest(unittest.TestCase):
    """Drive the LIVE gate against rendered panes, not a pre-decided verdict.

    A fake that returns the skip reason directly would never exercise
    ``current_composer_retains_body``, the screen guard, or the ordering between them —
    the failure mode #15745 j#109007 recorded (a port replaced by a fake leaves the
    live adapter unrun). These two pins read real pane text through the real predicates.
    """

    def _row(self) -> dict[str, object]:
        return {
            "name": ASSIGNED_NAME,
            "pane_id": TARGET,
            "terminal_id": "terminal-a",
            "revision": 7,
            "agent": RECEIVER,
            "agent_status": "idle",
        }

    def _evaluate(self, pane: str, *, state: str) -> QueueEnterResendGate:
        from mozyo_bridge.application import commands as commands_mod

        ops = LiveHerdrQueueEnterOpsMixin()
        rail = _Rail([self._row()], pane, state)
        record = SimpleNamespace(
            verdict="present",
            workspace_id="ws",
            lane_id="lane",
            role=RECEIVER,
            assigned_name=ASSIGNED_NAME,
            locator=TARGET,
            terminal_id="terminal-a",
            observed_at=GENERATION["attestation_observed_at"],
        )
        with patch.object(
            commands_mod, "active_herdr_turn_start_rail", rail
        ), patch(
            "mozyo_bridge.core.state.herdr_identity_attestation."
            "HerdrIdentityAttestationStore.read",
            return_value=record,
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_launch_generation_binding."
            "verified_terminal_generation_token",
            return_value=GENERATION["startup_action_id"],
        ):
            return ops.evaluate_queue_enter_resend(
                TARGET, TEXT, RECEIVER, dict(GENERATION)
            )

    def test_live_read_of_the_incident_pane_never_reports_the_body_absent(self) -> None:
        gate = self._evaluate(STARTUP_PANE_RETAINING_BODY, state="awaiting_input")

        self.assertNotEqual(gate.skip_reason, RESEND_SKIP_BODY_ABSENT)
        self.assertFalse(classify_submit_proof(gate.skip_reason).submitted)

    def test_live_read_of_a_landed_pane_reports_the_body_absent(self) -> None:
        gate = self._evaluate(LANDED_PANE, state="busy")

        self.assertEqual(gate.skip_reason, RESEND_SKIP_BODY_ABSENT)
        self.assertTrue(classify_submit_proof(gate.skip_reason).submitted)


class SubmitProofClassificationTest(unittest.TestCase):
    """The three-way partition is closed and fails closed on anything else."""

    def test_body_absent_is_the_only_submitted_verdict(self) -> None:
        proof = classify_submit_proof(RESEND_SKIP_BODY_ABSENT)

        self.assertTrue(proof.submitted)
        self.assertFalse(proof.enter_retryable)
        self.assertEqual(proof.telemetry(), {"submit_proof": SUBMIT_PROOF_COMPOSER_CLEARED})

    def test_allowed_gate_is_retryable_but_never_submitted(self) -> None:
        proof = classify_submit_proof(RESEND_SKIP_NONE)

        self.assertFalse(proof.submitted)
        self.assertTrue(proof.enter_retryable)

    def test_every_other_token_is_neither_submitted_nor_retryable(self) -> None:
        for token in (
            RESEND_SKIP_STARTUP_SCREEN,
            RESEND_SKIP_PANE_UNREADABLE,
            RESEND_SKIP_IDENTITY_DRIFT,
            "a_reason_token_that_does_not_exist_yet",
            None,
            2,
            ["body_absent"],
        ):
            with self.subTest(token=token):
                proof = classify_submit_proof(token)

                self.assertFalse(proof.submitted)
                self.assertFalse(proof.enter_retryable)
                self.assertEqual(proof.kind, SUBMIT_PROOF_UNPROVEN)


class TmuxBackendPostureTest(unittest.TestCase):
    """The other observation surface has no equivalent hole (IR step 4)."""

    def test_tmux_queue_enter_never_reaches_a_confirmed_submission(self) -> None:
        # The tmux rail runs no causal observation at all, so it publishes no
        # queue-enter observation and no turn-start outcome. The carve-out in
        # ``injection_stage_for`` therefore already demotes every tmux queue-enter
        # ``ok`` — there is nothing for a startup race to over-claim.
        self.assertFalse(turn_start_positively_observed(None, None))
        self.assertEqual(
            injection_stage_for("sent", "ok", mode=MODE_QUEUE_ENTER),
            STAGE_UNCERTAIN_PARTIAL,
        )
        self.assertEqual(
            injection_stage_for("sent", "queue_enter", mode=MODE_QUEUE_ENTER),
            STAGE_UNCERTAIN_PARTIAL,
        )


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
