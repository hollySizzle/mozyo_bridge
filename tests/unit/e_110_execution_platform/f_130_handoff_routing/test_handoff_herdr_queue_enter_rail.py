"""Direct fake-port tests for the Herdr queue-enter causal rail (#15242).

The common handoff rail owns the one body injection.  These tests exercise only
``HerdrQueueEnterSession`` and its narrow ``HerdrQueueEnterOps`` port: generation
pinning, wait/Enter ordering, strict resend decisions, deadline accounting, and
failure classification all run without a live Herdr server, tmux, or Redmine.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_herdr_queue_enter_rail import (
    HerdrQueueEnterOps,
    HerdrQueueEnterSession,
    LiveHerdrQueueEnterOpsMixin,
    QueueEnterResendGate,
    enforce_active_queue_enter_effect_fence,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    QueueEnterRetryPolicy,
    resolve_queue_enter_retry_policy,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    WAIT_ABSENT,
    WAIT_CHANGED,
    WAIT_ERROR,
    WAIT_TIMEOUT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_resend_gate import (
    RESEND_SKIP_BODY_ABSENT,
    RESEND_SKIP_BUDGET_EXHAUSTED,
    RESEND_SKIP_DISABLED,
    RESEND_SKIP_IDENTITY_DRIFT,
    RESEND_SKIP_IDENTITY_UNCONFIRMED,
    RESEND_SKIP_NONE,
    RESEND_SKIP_RECEIVER_BLOCKED,
    RESEND_SKIP_STATE_UNREADABLE,
    RESEND_SKIP_WAIT_UNARMED,
)


TARGET = "wT:pT"
TEXT = "[[mk-15242]] body typed exactly once by the common rail"
RECEIVER = "codex"
_ASSIGNED_NAME = "mzb1_ws_codex_lane"


def _process_generation(terminal_id: str, revision: str) -> str:
    return (
        f"{len(_ASSIGNED_NAME)}:{_ASSIGNED_NAME}:"
        f"{len(terminal_id)}:{terminal_id}:"
        f"{len(TARGET)}:{TARGET}:r{revision}"
    )

_GENERATION_A: dict[str, str] = {
    "provider": RECEIVER,
    "assigned_name": _ASSIGNED_NAME,
    "locator": TARGET,
    "terminal_id": "terminal-generation-a",
    "row_revision": "7",
    "process_generation": _process_generation("terminal-generation-a", "7"),
    "attestation_observed_at": "2026-08-10T00:00:00+00:00",
    "startup_action_id": "startup-generation-a",
}
_GENERATION_B: dict[str, str] = {
    **_GENERATION_A,
    "terminal_id": "terminal-generation-b",
    "process_generation": _process_generation("terminal-generation-b", "7"),
}
_GENERATION_A_REV8: dict[str, str] = {
    **_GENERATION_A,
    "row_revision": "8",
    "process_generation": _process_generation("terminal-generation-a", "8"),
}


@dataclass
class _ManualClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeOps:
    """Typed fake with no body-injection primitive in reach."""

    def __init__(
        self,
        clock: _ManualClock,
        *,
        bindings: Optional[list[Optional[dict[str, str]]]] = None,
        states: Optional[list[Optional[str]]] = None,
        wait_kinds: Optional[list[object]] = None,
        collect_advances: Optional[list[float]] = None,
        arm_advances: Optional[list[float]] = None,
        state_advances: Optional[list[float]] = None,
        binding_advances: Optional[list[float]] = None,
        binding_settles_wait: Optional[list[bool]] = None,
        gate_advances: Optional[list[float]] = None,
        pending_advances: Optional[list[float]] = None,
        pending_binding_updates: Optional[list[Optional[dict[str, str]]]] = None,
        gates: Optional[list[object]] = None,
        arm_results: Optional[list[bool]] = None,
        pending_results: Optional[list[object]] = None,
    ) -> None:
        self.clock = clock
        self._bindings = list(bindings or [_GENERATION_A])
        self._last_binding = self._bindings[-1] if self._bindings else None
        self._states = list(states or ["turn_ended"])
        self._last_state = self._states[-1] if self._states else None
        self._wait_kinds = list(wait_kinds or [WAIT_CHANGED])
        self._collect_advances = list(collect_advances or [])
        self._arm_advances = list(arm_advances or [])
        self._state_advances = list(state_advances or [])
        self._binding_advances = list(binding_advances or [])
        self._binding_settles_wait = list(binding_settles_wait or [])
        self._gate_advances = list(gate_advances or [])
        self._pending_advances = list(pending_advances or [])
        self._pending_binding_updates = list(pending_binding_updates or [])
        self._gates = list(gates or [])
        self._arm_results = list(arm_results or [])
        self._pending_results = list(pending_results or [])

        self.events: list[str] = []
        self.arm_calls: list[tuple[str, int]] = []
        self.arm_tokens: list[object] = []
        self.live_tokens: set[object] = set()
        self.collect_calls: list[object] = []
        self.cancel_calls: list[object] = []
        self.gate_calls: list[
            tuple[str, str, str, Optional[dict[str, str]]]
        ] = []
        self.sleep_calls: list[float] = []

    def observe_queue_enter_runtime_state(self, target: str) -> Optional[str]:
        self.events.append("state")
        if self._states:
            self._last_state = self._states.pop(0)
        if self._state_advances:
            self.clock.advance(self._state_advances.pop(0))
        return self._last_state

    def observe_queue_enter_gateway_binding(
        self, target: str
    ) -> Optional[dict[str, str]]:
        self.events.append("binding")
        if self._bindings:
            self._last_binding = self._bindings.pop(0)
        if self._binding_advances:
            self.clock.advance(self._binding_advances.pop(0))
        if self._binding_settles_wait and self._binding_settles_wait.pop(0):
            self.live_tokens.clear()
        return self._last_binding

    def arm_queue_enter_turn_wait(
        self, target: str, *, timeout_ms: int
    ) -> Optional[object]:
        self.events.append("arm")
        self.arm_calls.append((target, timeout_ms))
        if self._arm_advances:
            self.clock.advance(self._arm_advances.pop(0))
        allowed = self._arm_results.pop(0) if self._arm_results else True
        if not allowed:
            return None
        token = object()
        self.arm_tokens.append(token)
        self.live_tokens.add(token)
        return token

    def collect_queue_enter_turn_wait(self, armed: object) -> Optional[str]:
        self.events.append("collect")
        self.collect_calls.append(armed)
        self.live_tokens.discard(armed)
        value = self._wait_kinds.pop(0) if self._wait_kinds else WAIT_ERROR
        advance = self._collect_advances.pop(0) if self._collect_advances else 0.0
        self.clock.advance(advance)
        if isinstance(value, Exception):
            raise value
        return str(value)

    def cancel_queue_enter_turn_wait(self, armed: object) -> None:
        self.events.append("cancel")
        self.cancel_calls.append(armed)
        self.live_tokens.discard(armed)

    def queue_enter_turn_wait_pending(self, armed: object) -> bool:
        self.events.append("pending")
        if self._pending_advances:
            self.clock.advance(self._pending_advances.pop(0))
        if self._pending_binding_updates:
            self._last_binding = self._pending_binding_updates.pop(0)
        value = self._pending_results.pop(0) if self._pending_results else True
        if isinstance(value, Exception):
            raise value
        if not bool(value):
            self.live_tokens.discard(armed)
        return bool(value) and armed in self.live_tokens

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict[str, str]],
    ) -> QueueEnterResendGate:
        self.events.append("gate")
        self.gate_calls.append((target, text, receiver, baseline_binding))
        if self._gate_advances:
            self.clock.advance(self._gate_advances.pop(0))
        value = (
            self._gates.pop(0)
            if self._gates
            else QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")
        )
        if isinstance(value, Exception):
            raise value
        if not isinstance(value, QueueEnterResendGate):
            raise TypeError("fake gate value must be QueueEnterResendGate")
        return value

    def sleep(self, seconds: float) -> None:
        self.events.append("sleep")
        self.sleep_calls.append(seconds)
        self.clock.advance(seconds)


# Static structural-conformance gate: pyproject registers this test in the mypy
# island, so a fake/Protocol signature drift is a type-check failure.
_PORT_CONFORMS: HerdrQueueEnterOps = _FakeOps(_ManualClock())


@dataclass(frozen=True)
class _DriveResult:
    first_enter_sent: bool
    extra_enter_times: tuple[float, ...]


@dataclass(frozen=True)
class _Snapshot:
    runtime_state: str = "turn_ended"

    def to_telemetry_dict(self) -> dict[str, object]:
        return {
            "observation_kind": "post_choreography_snapshot",
            "source": "fake",
            "runtime_state": self.runtime_state,
            "read_ok": True,
            "read_reason": None,
            "poll_attempts": 1,
        }


def _session(
    ops: _FakeOps,
    clock: _ManualClock,
    *,
    policy: Optional[QueueEnterRetryPolicy] = None,
    expected_assigned_name: Optional[str] = _ASSIGNED_NAME,
    expected_process_generation: Optional[str] = _GENERATION_A["process_generation"],
) -> HerdrQueueEnterSession:
    return HerdrQueueEnterSession(
        ops=ops,
        target=TARGET,
        text=TEXT,
        receiver=RECEIVER,
        expected_assigned_name=expected_assigned_name,
        expected_process_generation=expected_process_generation,
        retry_policy=policy or resolve_queue_enter_retry_policy(),
        monotonic=clock,
    )


def _drive(session: HerdrQueueEnterSession, ops: _FakeOps) -> _DriveResult:
    """Model the common rail around the session without giving it body I/O."""
    if not session.capture_before_body():
        return _DriveResult(False, ())
    # The body event is deliberately external. _FakeOps has no send_text / inject_body
    # member, so the session can only request Enter through the callback below.
    ops.events.append("body:external")
    if not session.arm_before_first_enter():
        return _DriveResult(False, ())

    ops.events.append("enter:first")
    session.note_first_enter_sent()
    extra_enter_times: list[float] = []

    def _press_extra_enter() -> None:
        enforce_active_queue_enter_effect_fence()
        ops.events.append("enter:retry")
        extra_enter_times.append(ops.clock())

    session.complete_after_first_enter(press_extra_enter=_press_extra_enter)
    return _DriveResult(True, tuple(extra_enter_times))


class QueueEnterGenerationFenceTest(unittest.TestCase):
    def test_wait_settled_inside_first_enter_adapter_io_is_zero_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, states=["turn_ended"], wait_kinds=[WAIT_CHANGED])
        session = _session(ops, clock)
        sent: list[float] = []

        self.assertTrue(session.capture_before_body())
        self.assertTrue(session.arm_before_first_enter())
        with self.assertRaises(TerminalTransportError):
            with session.enter_effect_boundary(session.armed_wait):
                ops.live_tokens.clear()  # project verifier IO settles the wait
                enforce_active_queue_enter_effect_fence()
                sent.append(clock())
        session.cancel_before_failed_enter()

        self.assertEqual(sent, [])
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_WAIT_UNARMED)

    def test_deadline_expired_inside_first_enter_adapter_io_is_zero_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock)
        session = _session(ops, clock)
        sent: list[float] = []

        self.assertTrue(session.capture_before_body())
        self.assertTrue(session.arm_before_first_enter())
        with self.assertRaises(TerminalTransportError):
            with session.enter_effect_boundary(session.armed_wait):
                clock.advance(31.0)  # project verifier IO consumes the budget
                enforce_active_queue_enter_effect_fence()
                sent.append(clock())
        session.cancel_before_failed_enter()

        self.assertEqual(sent, [])
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(
            session.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED
        )

    def test_generation_is_pinned_before_body_and_stays_stable_through_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, wait_kinds=[WAIT_CHANGED])
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertLess(ops.events.index("binding"), ops.events.index("body:external"))
        self.assertLess(ops.events.index("arm"), ops.events.index("enter:first"))
        self.assertLess(ops.events.index("pending"), ops.events.index("enter:first"))
        self.assertEqual(session.enter_attempts, 1)

    def test_resolved_first_observer_refuses_the_first_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, pending_results=[False])
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_WAIT_UNARMED)
        self.assertEqual(len(ops.cancel_calls), 1)
        self.assertNotIn("enter:first", ops.events)

    def test_generation_flip_inside_final_pending_poll_refuses_first_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, pending_binding_updates=[_GENERATION_B])
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_IDENTITY_DRIFT)
        self.assertEqual(len(ops.cancel_calls), 1)
        self.assertNotIn("enter:first", ops.events)

    def test_wait_settling_during_final_binding_read_refuses_first_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            binding_settles_wait=[False, False, False, True],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_WAIT_UNARMED)
        self.assertNotIn("enter:first", ops.events)

    def test_revision_only_drift_after_body_keeps_same_terminal_enterable(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            bindings=[_GENERATION_A, _GENERATION_A_REV8],
            wait_kinds=[WAIT_CHANGED],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertTrue(session.causal_start_confirmed)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_NONE)

    def test_generation_drift_after_body_refuses_the_first_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            bindings=[_GENERATION_A, _GENERATION_B],
            wait_kinds=[WAIT_CHANGED],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_IDENTITY_DRIFT)
        self.assertNotIn("arm", ops.events)
        self.assertNotIn("enter:first", ops.events)

    def test_generation_drift_while_first_wait_arms_refuses_the_first_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            bindings=[_GENERATION_A, _GENERATION_A, _GENERATION_B],
            wait_kinds=[WAIT_TIMEOUT],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_IDENTITY_DRIFT)
        self.assertEqual(len(ops.arm_calls), 1)
        self.assertEqual(len(ops.collect_calls), 0)
        self.assertEqual(len(ops.cancel_calls), 1)
        self.assertNotIn("enter:first", ops.events)

    def test_missing_pre_body_generation_refuses_first_enter_even_if_it_appears(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            bindings=[None, _GENERATION_A],
            wait_kinds=[WAIT_CHANGED],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(
            session.resend_skipped_reason, RESEND_SKIP_IDENTITY_UNCONFIRMED
        )
        self.assertEqual(ops.events, ["binding"])
        self.assertNotIn("arm", ops.events)
        self.assertNotIn("enter:first", ops.events)

    def test_missing_pre_body_generation_refuses_first_enter_when_still_missing(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, bindings=[None, None], wait_kinds=[WAIT_CHANGED])
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(
            session.resend_skipped_reason, RESEND_SKIP_IDENTITY_UNCONFIRMED
        )
        self.assertEqual(ops.events, ["binding"])

    def test_provider_mismatch_refuses_before_body_or_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, bindings=[{**_GENERATION_A, "provider": "claude"}])
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_IDENTITY_DRIFT)
        self.assertEqual(ops.events, ["binding"])

    def test_missing_expected_assigned_name_refuses_before_body_or_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock)
        session = _session(ops, clock, expected_assigned_name=None)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(
            session.resend_skipped_reason, RESEND_SKIP_IDENTITY_UNCONFIRMED
        )
        self.assertEqual(ops.events, ["binding"])

    def test_missing_expected_process_generation_refuses_before_body_or_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock)
        session = _session(ops, clock, expected_process_generation=None)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(
            session.resend_skipped_reason, RESEND_SKIP_IDENTITY_UNCONFIRMED
        )
        self.assertEqual(ops.events, ["binding"])

    def test_resolution_generation_a_then_first_live_generation_b_is_zero_body(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, bindings=[_GENERATION_B])
        session = _session(
            ops,
            clock,
            expected_process_generation=_GENERATION_A["process_generation"],
        )

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_IDENTITY_DRIFT)
        self.assertEqual(ops.events, ["binding"])


class QueueEnterCausalLoopTest(unittest.TestCase):
    def test_retry_rechecks_full_gate_at_final_effect_boundary(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT, WAIT_CHANGED],
            collect_advances=[2.0, 0.0],
            gates=[
                QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
                QueueEnterResendGate(RESEND_SKIP_BODY_ABSENT),
            ],
        )
        session = _session(ops, clock)
        self.assertTrue(session.capture_before_body())
        self.assertTrue(session.arm_before_first_enter())
        session.note_first_enter_sent()
        sent: list[float] = []

        def guarded_send() -> None:
            enforce_active_queue_enter_effect_fence()
            sent.append(clock())

        session.complete_after_first_enter(press_extra_enter=guarded_send)

        self.assertEqual(sent, [])
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_BODY_ABSENT)
        self.assertEqual(len(ops.gate_calls), 2)

    def test_retry_uses_final_gate_state_as_causal_baseline(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT, WAIT_CHANGED],
            collect_advances=[2.0, 0.0],
            gates=[
                QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
                QueueEnterResendGate(RESEND_SKIP_NONE, "busy"),
            ],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertEqual(result.extra_enter_times, (2.0,))
        self.assertFalse(session.causal_start_confirmed)
        self.assertEqual(session.causal_state, "busy")

    def test_wait_settled_inside_retry_adapter_io_is_zero_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            states=["turn_ended"],
            wait_kinds=[WAIT_TIMEOUT, WAIT_CHANGED],
            collect_advances=[2.0, 0.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
        )
        session = _session(ops, clock)
        self.assertTrue(session.capture_before_body())
        self.assertTrue(session.arm_before_first_enter())
        session.note_first_enter_sent()
        sent: list[float] = []

        def guarded_send() -> None:
            ops.live_tokens.clear()  # project verifier IO settles the retry wait
            enforce_active_queue_enter_effect_fence()
            sent.append(clock())

        session.complete_after_first_enter(press_extra_enter=guarded_send)

        self.assertEqual(sent, [])
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_WAIT_UNARMED)

    def test_deadline_expired_inside_retry_adapter_io_is_zero_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            states=["turn_ended"],
            wait_kinds=[WAIT_TIMEOUT, WAIT_CHANGED],
            collect_advances=[2.0, 0.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
        )
        session = _session(ops, clock)
        self.assertTrue(session.capture_before_body())
        self.assertTrue(session.arm_before_first_enter())
        session.note_first_enter_sent()
        sent: list[float] = []

        def guarded_send() -> None:
            clock.advance(29.0)  # project verifier IO crosses t=30 deadline
            enforce_active_queue_enter_effect_fence()
            sent.append(clock())

        session.complete_after_first_enter(press_extra_enter=guarded_send)

        self.assertEqual(sent, [])
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(
            session.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED
        )

    def test_default_and_explicit_policies_allow_multiple_fresh_safe_retries(self) -> None:
        policies = (
            ("default", resolve_queue_enter_retry_policy()),
            ("explicit", resolve_queue_enter_retry_policy(6.0, 2.0)),
        )
        for label, policy in policies:
            with self.subTest(policy=label):
                clock = _ManualClock()
                ops = _FakeOps(
                    clock,
                    wait_kinds=[WAIT_TIMEOUT, WAIT_TIMEOUT, WAIT_CHANGED],
                    collect_advances=[2.0, 2.0, 0.0],
                    gates=[
                        QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
                        QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
                    ],
                )
                session = _session(ops, clock, policy=policy)

                result = _drive(session, ops)

                self.assertTrue(result.first_enter_sent)
                self.assertEqual(result.extra_enter_times, (2.0, 4.0))
                self.assertEqual(session.enter_attempts, 3)
                self.assertEqual(len(ops.gate_calls), 4)
                self.assertEqual(len(ops.arm_calls), 3)
                self.assertEqual(
                    [timeout for _target, timeout in ops.arm_calls],
                    [2000, 2000, 2000],
                )
                self.assertEqual(
                    [call[1:] for call in ops.gate_calls],
                    [(TEXT, RECEIVER, _GENERATION_A)] * 4,
                )
                self.assertFalse(hasattr(ops, "inject_body"))
                self.assertFalse(hasattr(ops, "send_text"))

    def test_changed_event_on_idle_generation_is_causal_success(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, states=["turn_ended"], wait_kinds=[WAIT_CHANGED])
        session = _session(ops, clock)

        result = _drive(session, ops)
        observation = session.observation(_Snapshot())

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(observation.get("event_wait_kind"), WAIT_CHANGED)
        self.assertEqual(observation.get("enter_attempts"), 1)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_NONE)

    def test_busy_changed_event_is_not_causal_confirmation(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, states=["busy"], wait_kinds=[WAIT_CHANGED])
        session = _session(
            ops,
            clock,
            policy=resolve_queue_enter_retry_policy(0.0, 2.0),
        )

        result = _drive(session, ops)
        observation = session.observation(_Snapshot(runtime_state="busy"))

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertNotIn("event_wait_kind", observation)
        self.assertEqual(session.failure_reason, "turn_start_unconfirmed")
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_DISABLED)

    def test_noncausal_changed_event_never_authorises_an_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            states=["busy"],
            wait_kinds=[WAIT_CHANGED, WAIT_CHANGED],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(session.final_wait_kind, WAIT_CHANGED)
        self.assertEqual(session.failure_reason, "turn_start_unconfirmed")
        self.assertEqual(ops.gate_calls, [])

    def test_busy_initial_state_uses_idle_retry_baseline_for_causal_success(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            states=["busy"],
            wait_kinds=[WAIT_TIMEOUT, WAIT_CHANGED],
            collect_advances=[2.0, 0.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)
        observation = session.observation(_Snapshot())

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, (2.0,))
        self.assertTrue(session.causal_start_confirmed)
        self.assertEqual(observation.get("baseline_runtime_state"), "turn_ended")
        self.assertEqual(observation.get("event_wait_kind"), WAIT_CHANGED)

    def test_busy_retry_baseline_never_confirms_a_changed_event(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            states=["turn_ended"],
            wait_kinds=[WAIT_TIMEOUT, WAIT_CHANGED],
            collect_advances=[2.0, 0.0],
            gates=[
                QueueEnterResendGate(RESEND_SKIP_NONE, "busy"),
                QueueEnterResendGate(RESEND_SKIP_NONE, "busy"),
            ],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)
        observation = session.observation(_Snapshot(runtime_state="busy"))

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, (2.0,))
        self.assertFalse(session.causal_start_confirmed)
        self.assertNotIn("event_wait_kind", observation)

    def test_noncoherent_changed_event_cannot_be_reclassified_after_aba(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            bindings=[
                _GENERATION_A,
                _GENERATION_A,
                _GENERATION_A,
                _GENERATION_A,
                _GENERATION_B,
                _GENERATION_A,
            ],
            wait_kinds=[WAIT_CHANGED],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)
        observation = session.observation(_Snapshot())

        self.assertTrue(result.first_enter_sent)
        self.assertFalse(session.causal_start_confirmed)
        self.assertNotIn("event_wait_kind", observation)

    def test_generation_flip_inside_retry_pending_poll_refuses_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT],
            collect_advances=[2.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
            pending_binding_updates=[
                _GENERATION_A,
                _GENERATION_A,
                _GENERATION_B,
            ],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_IDENTITY_DRIFT)
        self.assertEqual(len(ops.cancel_calls), 1)

    def test_wait_settling_during_retry_binding_read_refuses_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT],
            collect_advances=[2.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
            binding_settles_wait=[False, False, False, False, True],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_WAIT_UNARMED)

    def test_generation_drift_while_retry_wait_arms_refuses_the_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            bindings=[
                _GENERATION_A,
                _GENERATION_A,
                _GENERATION_A,
                _GENERATION_A,
                _GENERATION_B,
            ],
            wait_kinds=[WAIT_TIMEOUT, WAIT_TIMEOUT],
            collect_advances=[2.0, 0.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_IDENTITY_DRIFT)
        self.assertEqual(len(ops.arm_calls), 2)
        self.assertEqual(len(ops.collect_calls), 1)
        self.assertEqual(len(ops.cancel_calls), 1)

    def test_resolved_retry_observer_refuses_the_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT],
            collect_advances=[2.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
            pending_results=[True, True, False],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_WAIT_UNARMED)
        self.assertEqual(len(ops.cancel_calls), 1)
        self.assertNotIn("enter:retry", ops.events)

    def test_observer_error_stops_without_any_later_enter(self) -> None:
        cases = (
            ("error_first", [WAIT_ERROR, WAIT_CHANGED], [0.0, 0.0], 0),
            ("error_after_timeout", [WAIT_TIMEOUT, WAIT_ERROR], [2.0, 0.0], 1),
            (
                "late_error_after_two_timeouts",
                [WAIT_TIMEOUT, WAIT_TIMEOUT, WAIT_ERROR, WAIT_CHANGED],
                [2.0, 2.0, 0.0, 0.0],
                2,
            ),
        )
        for label, wait_kinds, advances, prior_timeout_retries in cases:
            with self.subTest(case=label):
                clock = _ManualClock()
                ops = _FakeOps(
                    clock,
                    wait_kinds=list(wait_kinds),
                    collect_advances=list(advances),
                    gates=[
                        QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")
                    ] * prior_timeout_retries,
                )
                session = _session(ops, clock)

                result = _drive(session, ops)

                self.assertEqual(
                    len(result.extra_enter_times), prior_timeout_retries
                )
                self.assertEqual(
                    session.enter_attempts, 1 + prior_timeout_retries
                )
                self.assertEqual(
                    len(ops.gate_calls), prior_timeout_retries * 2
                )
                self.assertEqual(session.final_wait_kind, WAIT_ERROR)
                self.assertEqual(
                    session.resend_skipped_reason, RESEND_SKIP_STATE_UNREADABLE
                )


class QueueEnterFailureMappingTest(unittest.TestCase):
    def test_absent_wait_maps_to_turn_start_absent(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, wait_kinds=[WAIT_ABSENT])
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.failure_reason, "turn_start_absent")

    def test_receiver_blocked_gate_maps_to_receiver_blocked(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT],
            collect_advances=[2.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_RECEIVER_BLOCKED, "blocked")],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.failure_reason, "receiver_blocked")
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_RECEIVER_BLOCKED)
        self.assertEqual(len(ops.cancel_calls), 1)

    def test_unarmed_first_wait_refuses_first_enter_and_maps_unconfirmed(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, arm_results=[False])
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_WAIT_UNARMED)
        self.assertEqual(session.failure_reason, "turn_start_unconfirmed")

    def test_generic_gate_failure_maps_to_unconfirmed_without_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT],
            collect_advances=[2.0],
            gates=[RuntimeError("gate unavailable")],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_STATE_UNREADABLE)
        self.assertEqual(session.failure_reason, "turn_start_unconfirmed")
        self.assertEqual(len(ops.cancel_calls), 1)

    def test_post_arm_body_loss_refuses_and_cancels_the_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT],
            collect_advances=[2.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_BODY_ABSENT)],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_BODY_ABSENT)
        arm_indexes = [
            index for index, event in enumerate(ops.events) if event == "arm"
        ]
        self.assertEqual(len(arm_indexes), 2)
        self.assertLess(arm_indexes[-1], ops.events.index("gate"))
        self.assertEqual(len(ops.cancel_calls), 1)

    def test_transport_gate_failure_propagates_to_common_typed_terminal(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT],
            collect_advances=[2.0],
            gates=[TerminalTransportError("fake read failure")],
        )
        session = _session(ops, clock)

        with self.assertRaises(TerminalTransportError):
            _drive(session, ops)
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(len(ops.cancel_calls), 1)


class QueueEnterDeadlineTest(unittest.TestCase):
    def test_first_arm_consuming_deadline_refuses_the_first_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            arm_advances=[31.0],
            wait_kinds=[WAIT_TIMEOUT],
        )
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(clock(), 31.0)
        self.assertEqual(len(ops.collect_calls), 0)
        self.assertEqual(len(ops.cancel_calls), 1)
        self.assertEqual(
            session.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED
        )

    def test_post_arm_state_read_consuming_deadline_refuses_first_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, state_advances=[30.0])
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED)
        self.assertEqual(len(ops.cancel_calls), 1)

    def test_final_pending_poll_consuming_deadline_refuses_first_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(clock, pending_advances=[30.0])
        session = _session(ops, clock)

        result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertEqual(session.enter_attempts, 0)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED)
        self.assertEqual(len(ops.cancel_calls), 1)

    def test_post_arm_retry_gate_consuming_deadline_refuses_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT],
            collect_advances=[2.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
            gate_advances=[3.0],
        )
        session = _session(
            ops,
            clock,
            policy=resolve_queue_enter_retry_policy(5.0, 2.0),
        )

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED)
        self.assertEqual(len(ops.cancel_calls), 1)

    def test_final_retry_pending_poll_consuming_deadline_refuses_extra_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT],
            collect_advances=[2.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
            pending_advances=[0.0, 0.0, 3.0],
        )
        session = _session(
            ops,
            clock,
            policy=resolve_queue_enter_retry_policy(5.0, 2.0),
        )

        result = _drive(session, ops)

        self.assertTrue(result.first_enter_sent)
        self.assertEqual(result.extra_enter_times, ())
        self.assertEqual(session.enter_attempts, 1)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED)
        self.assertEqual(len(ops.cancel_calls), 1)

    def test_instant_timeout_still_observes_the_minimum_retry_interval(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT, WAIT_CHANGED],
            collect_advances=[0.0, 0.0],
            gates=[QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended")],
        )
        session = _session(
            ops,
            clock,
            policy=resolve_queue_enter_retry_policy(6.0, 2.0),
        )

        result = _drive(session, ops)

        self.assertEqual(ops.sleep_calls, [2.0])
        self.assertEqual(result.extra_enter_times, (2.0,))
        self.assertEqual([timeout for _target, timeout in ops.arm_calls], [2000, 2000])

    def test_absolute_deadline_caps_each_wait_and_forbids_a_late_enter(self) -> None:
        clock = _ManualClock()
        ops = _FakeOps(
            clock,
            wait_kinds=[WAIT_TIMEOUT, WAIT_TIMEOUT, WAIT_TIMEOUT],
            collect_advances=[2.0, 2.0, 1.0],
            gates=[
                QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
                QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
            ],
        )
        session = _session(
            ops,
            clock,
            policy=resolve_queue_enter_retry_policy(5.0, 2.0),
        )

        result = _drive(session, ops)

        self.assertEqual(result.extra_enter_times, (2.0, 4.0))
        self.assertEqual([timeout for _target, timeout in ops.arm_calls], [2000, 2000, 1000])
        self.assertEqual(session.enter_attempts, 3)
        self.assertEqual(session.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED)
        self.assertEqual(clock(), 5.0)


class _BindingReader:
    def __init__(self, rows: list[list[dict[str, object]]]) -> None:
        self.rows = list(rows)

    def read_agent_state(self, target: str) -> object:
        return SimpleNamespace(ok=True, state="turn_ended")

    def read_agent_rows(self) -> list[dict[str, object]]:
        return self.rows.pop(0)


class _BindingRail:
    def __init__(self, rows: list[list[dict[str, object]]]) -> None:
        self.reader = _BindingReader(rows)

    def arm_turn_start_wait(self, target: str, *, timeout_ms: int) -> object:
        return object()

    def read_visible_pane(self, target: str) -> str:
        return "› composer"


class _LiveBindingFakeOps(LiveHerdrQueueEnterOpsMixin, _FakeOps):
    """Use the live binding read while retaining the choreography event log."""


class LiveQueueEnterBindingTest(unittest.TestCase):
    assigned_name = "mzb1_ws_codex_lane"

    @staticmethod
    def _row(**updates: object) -> dict[str, object]:
        row: dict[str, object] = {
            "name": LiveQueueEnterBindingTest.assigned_name,
            "pane_id": TARGET,
            "terminal_id": "terminal-a",
            "revision": 7,
            "agent": RECEIVER,
            "agent_status": "idle",
        }
        row.update(updates)
        return row

    @staticmethod
    def _record(terminal_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            verdict="present",
            workspace_id="ws",
            lane_id="lane",
            role=RECEIVER,
            assigned_name=LiveQueueEnterBindingTest.assigned_name,
            locator=TARGET,
            terminal_id=terminal_id,
            observed_at="2026-08-10T00:00:00+00:00",
        )

    def _observe(self, rail: _BindingRail) -> Optional[dict[str, str]]:
        from mozyo_bridge.application import commands as commands_mod

        ops = LiveHerdrQueueEnterOpsMixin()
        terminal_id = str(rail.reader.rows[0][0]["terminal_id"])
        with patch.object(
            commands_mod, "active_herdr_turn_start_rail", rail
        ), patch(
            "mozyo_bridge.core.state.herdr_identity_attestation."
            "HerdrIdentityAttestationStore.read",
            return_value=self._record(terminal_id),
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_launch_generation_binding."
            "verified_terminal_generation_token",
            return_value="startup-generation-a",
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "infrastructure.herdr_transport.resolve_herdr_binary"
        ) as ambient_resolver:
            binding = ops.observe_queue_enter_gateway_binding(TARGET)
        ambient_resolver.assert_not_called()
        return binding

    def test_binding_uses_bound_inventory_and_carries_terminal_generation(self) -> None:
        binding = self._observe(_BindingRail([[self._row()]]))

        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding["terminal_id"], "terminal-a")
        self.assertEqual(binding["row_revision"], "7")
        self.assertIn("terminal-a", binding["process_generation"])

    def test_terminal_replacement_changes_binding_at_same_name_and_locator(self) -> None:
        rail = _BindingRail(
            [[self._row(terminal_id="terminal-a")], [self._row(terminal_id="terminal-b")]]
        )

        before = self._observe(rail)
        after = self._observe(rail)

        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertNotEqual(before, after)

    def test_missing_or_malformed_revision_refuses_binding(self) -> None:
        cases = ({}, {"revision": True}, {"revision": "7"}, {"revision": -1})
        for updates in cases:
            with self.subTest(updates=updates):
                row = self._row(**updates)
                if not updates:
                    row.pop("revision")
                self.assertIsNone(self._observe(_BindingRail([[row]])))

    def test_shell_residue_refuses_binding(self) -> None:
        row = self._row(agent="", agent_status="unknown")

        self.assertIsNone(self._observe(_BindingRail([[row]])))

    def test_shell_residue_refuses_before_body_or_enter(self) -> None:
        from mozyo_bridge.application import commands as commands_mod

        clock = _ManualClock()
        ops = _LiveBindingFakeOps(clock)
        session = _session(ops, clock)
        row = self._row(agent="", agent_status="unknown")
        with patch.object(
            commands_mod, "active_herdr_turn_start_rail", _BindingRail([[row]])
        ):
            result = _drive(session, ops)

        self.assertFalse(result.first_enter_sent)
        self.assertNotIn("body:external", ops.events)
        self.assertNotIn("enter:first", ops.events)

    def test_missing_or_mismatched_detected_provider_refuses_binding(self) -> None:
        missing = self._row()
        missing.pop("agent")
        cases = (missing, self._row(agent="claude"))

        for row in cases:
            with self.subTest(agent=row.get("agent")):
                self.assertIsNone(self._observe(_BindingRail([[row]])))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
