"""Fake-port truth table for the common tmux transport rail (Redmine #13729 tranche 4).

Exercises :class:`TmuxTransportRailUseCase` with a synthetic fake port — no live tmux / herdr /
Redmine — pinning the slice's ``mode`` x ``outcome`` truth table and, in particular, the three
retry / rollback policy conditions the tranche 4 request separates:

- **C-u rollback is allowed on exactly one cell**: a strict (``standard`` / ``pending``-not-taken,
  i.e. non ``queue-enter``) send whose landing marker was never observed. It rolls back, emits
  ``blocked`` / ``marker_timeout``, prints the guidance, and dies WITHOUT pressing Enter. A
  ``queue-enter`` marker miss never rolls back;
- **uncertain delivery is no-blind-retry**: a ``standard`` send whose post-Enter turn start is not
  confirmed emits ``blocked`` / ``turn_start_unconfirmed`` and dies with no C-u rollback and no
  re-send (the marker+body was typed once);
- **Enter-only retry engages on exactly one cell**: ``queue-enter`` + marker-unobserved +
  policy-enabled re-issues ONLY Enter on the interval until the marker lands or the window
  elapses; the marker+body is never re-injected.

The ``pending`` / ``sent`` terminals, the herdr ``queue-enter`` #13292 snapshot + #13300 ledger,
and the envelope / anchor / submit / duplicate-lane context threading are pinned alongside. The
live composition (``run_tmux_transport_rail`` over ``LiveTmuxTransportRailOps``, routing every
effect through the ``commands`` module) is covered end-to-end by the ``orchestrate_handoff``
handoff-routing integration tests.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import List, Optional
from unittest.mock import patch

from mozyo_bridge.application.turn_start_observation import (
    QueueEnterTurnStartObservation,
    TurnStartObservation,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_tmux_transport_rail import (
    LiveTmuxTransportRailOps,
    QueueEnterResendGate,
    TmuxTransportRailOps,
    TmuxTransportRailRequest,
    TmuxTransportRailUseCase,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_resend_gate import (
    RESEND_SKIP_BODY_ABSENT,
    RESEND_SKIP_BUDGET_EXHAUSTED,
    RESEND_SKIP_IDENTITY_DRIFT,
    RESEND_SKIP_NONE,
    RESEND_SKIP_RECEIVER_BLOCKED,
    RESEND_SKIP_STARTUP_SCREEN,
    RESEND_SKIP_STATE_NOT_INJECTABLE,
    RESEND_SKIP_STATE_UNREADABLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    AgentStateResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    AsanaAnchor,
    DeliveryOutcome,
    NormalizedAnchor,
    QueueEnterRetryOutcome,
    RedmineAnchor,
    TargetActivationOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    STAGE_SUBMITTED_CONFIRMED,
    STAGE_UNCERTAIN_PARTIAL,
)

_MODE_QUEUE_ENTER = "queue-enter"
_MODE_STANDARD = "standard"
_MODE_PENDING = "pending"


class _FakeDie(Exception):
    """Stand-in for ``commands.die`` — raises so the use case's control flow terminates."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class _EmitCall:
    outcome: DeliveryOutcome
    record_format: str
    command: Optional[str]
    duplicate_lane_panes: Optional[List[str]]
    role_profile_contract: Optional[str]
    submit_lines: Optional[List[str]]
    turn_start_lines: Optional[List[str]]
    retry: Optional[QueueEnterRetryOutcome]
    activation: Optional[TargetActivationOutcome]


@dataclass
class _PersistCall:
    outcome: DeliveryOutcome
    persist_delivery: bool
    duplicate_lane_panes: Optional[List[str]]
    record_format: str
    turn_start_lines: Optional[List[str]]
    retry: Optional[QueueEnterRetryOutcome]
    activation: Optional[TargetActivationOutcome]


@dataclass
class _FakeOps:
    """A typed fake :class:`TmuxTransportRailOps` recording the side-effect calls in order.

    The result-shaping inputs (marker observation, the retry-probe captures, the standard /
    queue-enter observations, the restore passthrough) are set by the caller so a single fake
    drives every truth-table cell without live tmux / herdr.
    """

    marker_observed: bool = True
    #: FIFO captures returned by ``capture`` — the pre-Enter standard baseline, then the
    #: Enter-only retry marker-visibility probes. Empty -> "".
    captures: List[str] = field(default_factory=list)
    standard_confirmed: bool = True
    queue_enter_snapshot: Optional[QueueEnterTurnStartObservation] = None
    restore_result: Optional[TargetActivationOutcome] = None

    events: List[str] = field(default_factory=list)
    injected: List[tuple] = field(default_factory=list)
    enter_presses: int = 0
    emitted: List[_EmitCall] = field(default_factory=list)
    persisted: List[_PersistCall] = field(default_factory=list)
    ledgered: List[tuple] = field(default_factory=list)
    guidance: List[str] = field(default_factory=list)
    died: List[str] = field(default_factory=list)

    def inject_body(self, target: str, text: str) -> None:
        self.events.append("inject")
        self.injected.append((target, text))

    def wait_for_marker(
        self, target: str, marker: str, lines: int, timeout: float
    ) -> bool:
        self.events.append("wait")
        return self.marker_observed

    def capture(self, target: str, lines: int) -> str:
        self.events.append("capture")
        return self.captures.pop(0) if self.captures else ""

    def rollback(self, target: str) -> None:
        self.events.append("rollback")

    def press_enter(self, target: str) -> None:
        self.events.append("enter")
        self.enter_presses += 1

    def sleep(self, seconds: float) -> None:
        self.events.append("sleep")

    def observe_standard_turn_start(
        self, target: str, *, baseline_capture: str, window_seconds: float, lines: int
    ) -> TurnStartObservation:
        self.events.append("observe_std")
        return TurnStartObservation(
            confirmed=self.standard_confirmed,
            polls=1,
            window_seconds=window_seconds,
            interval_seconds=1.0,
        )

    def observe_queue_enter_turn_start(
        self, target: str
    ) -> Optional[QueueEnterTurnStartObservation]:
        self.events.append("observe_qe")
        return self.queue_enter_snapshot

    def observe_queue_enter_runtime_state(self, target: str) -> Optional[str]:
        self.events.append("state_qe")
        return None

    def observe_queue_enter_gateway_binding(
        self, target: str
    ) -> Optional[dict[str, str]]:
        self.events.append("bind_qe")
        return None

    def arm_queue_enter_turn_wait(
        self, target: str, *, timeout_ms: int
    ) -> Optional[object]:
        self.events.append(f"arm_qe_wait:{timeout_ms}")
        return None

    def collect_queue_enter_turn_wait(self, armed: object) -> Optional[str]:
        self.events.append("collect_qe_wait")
        return None

    def cancel_queue_enter_turn_wait(self, armed: object) -> None:
        self.events.append("cancel_qe_wait")

    def queue_enter_turn_wait_pending(self, armed: object) -> bool:
        self.events.append("pending_qe_wait")
        return False

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict[str, str]],
    ) -> QueueEnterResendGate:
        self.events.append("gate_qe")
        return QueueEnterResendGate(RESEND_SKIP_STATE_UNREADABLE)

    def emit(
        self,
        outcome: DeliveryOutcome,
        *,
        record_format: str,
        command: Optional[str],
        duplicate_lane_panes: Optional[List[str]],
        role_profile_contract: Optional[str],
        submit_lines: Optional[List[str]],
        turn_start_lines: Optional[List[str]] = None,
        retry: Optional[QueueEnterRetryOutcome] = None,
        activation: Optional[TargetActivationOutcome] = None,
    ) -> None:
        self.events.append("emit")
        self.emitted.append(
            _EmitCall(
                outcome=outcome,
                record_format=record_format,
                command=command,
                duplicate_lane_panes=duplicate_lane_panes,
                role_profile_contract=role_profile_contract,
                submit_lines=submit_lines,
                turn_start_lines=turn_start_lines,
                retry=retry,
                activation=activation,
            )
        )

    def persist(
        self,
        outcome: DeliveryOutcome,
        *,
        persist_delivery: bool,
        duplicate_lane_panes: Optional[List[str]],
        record_format: str,
        turn_start_lines: Optional[List[str]] = None,
        retry: Optional[QueueEnterRetryOutcome] = None,
        activation: Optional[TargetActivationOutcome] = None,
    ) -> None:
        self.events.append("persist")
        self.persisted.append(
            _PersistCall(
                outcome=outcome,
                persist_delivery=persist_delivery,
                duplicate_lane_panes=duplicate_lane_panes,
                record_format=record_format,
                turn_start_lines=turn_start_lines,
                retry=retry,
                activation=activation,
            )
        )

    def record_ledger(
        self,
        outcome: DeliveryOutcome,
        *,
        retry_outcome: Optional[QueueEnterRetryOutcome],
        backend: Optional[str] = None,
        rail: Optional[str] = None,
        disposition: Optional[str] = None,
    ) -> None:
        self.events.append("ledger")
        self.ledgered.append(
            (outcome, retry_outcome, backend, rail, disposition)
        )

    def restore_previous_active(
        self,
        activation: Optional[TargetActivationOutcome],
        *,
        restore_previous_active: bool,
    ) -> Optional[TargetActivationOutcome]:
        self.events.append("restore")
        return self.restore_result

    def emit_marker_timeout_guidance(self, receiver: str) -> None:
        self.events.append("guidance")
        self.guidance.append(receiver)

    def die(self, message: str) -> None:
        self.events.append("die")
        self.died.append(message)
        raise _FakeDie(message)


# Structural-conformance gate (mypy island, review j#79040 F1' precedent): assigning the fake to
# the port type makes any fake signature drift a STATIC error, not a silent runtime-only skip.
_PORT_CONFORMS: TmuxTransportRailOps = _FakeOps()


def _request(
    *,
    mode: str,
    herdr_send: bool = False,
    persist_delivery: bool = False,
    anchor: Optional[NormalizedAnchor] = None,
    submit_intent: Optional[str] = None,
    submit_delivery_id: Optional[str] = None,
    duplicate_lane_panes: Optional[List[str]] = None,
    queue_enter_retry_window: Optional[float] = None,
    queue_enter_retry_interval: Optional[float] = None,
    target_activation: Optional[TargetActivationOutcome] = None,
    restore_previous_active: bool = False,
    submit_delay: Optional[float] = None,
    herdr_process_generation: Optional[str] = None,
) -> TmuxTransportRailRequest:
    """Build a request; the envelope value objects are ``None`` (the slice only threads them)."""
    return TmuxTransportRailRequest(
        target="%pT",
        marker="[[mk-1]]",
        body="hello body",
        receiver="claude",
        anchor=anchor,
        mode=mode,
        kind="implementation_request",
        execution_root=None,
        role_profile_resolution=None,
        role_profile_contract=None,
        transition_role_boundary=None,
        workflow_contract_bundle=None,
        ticketless_callback=None,
        ticketless_consultation=None,
        ticketless_work_intake=None,
        record_format="both",
        record_command=None,
        duplicate_lane_panes=[] if duplicate_lane_panes is None else duplicate_lane_panes,
        submit_intent=submit_intent,
        submit_delivery_id=submit_delivery_id,
        persist_delivery=persist_delivery,
        herdr_send=herdr_send,
        herdr_assigned_name=("mzb1_ws_claude_lane" if herdr_send else None),
        herdr_process_generation=(
            herdr_process_generation
            or QueueEnterObservationOnlyWaitTests._binding()["process_generation"]
            if herdr_send
            else None
        ),
        read_lines=50,
        landing_timeout=None,
        submit_delay=submit_delay,
        queue_enter_retry_window=queue_enter_retry_window,
        queue_enter_retry_interval=queue_enter_retry_interval,
        target_activation=target_activation,
        restore_previous_active=restore_previous_active,
    )


def _run(
    ops: _FakeOps, request: TmuxTransportRailRequest
) -> tuple[Optional[int], Optional[_FakeDie]]:
    code: Optional[int] = None
    died: Optional[_FakeDie] = None
    try:
        code = TmuxTransportRailUseCase(ops).execute(request)
    except _FakeDie as exc:
        died = exc
    return code, died


class TmuxTransportRailPendingTest(unittest.TestCase):
    def test_pending_injects_emits_persists_and_returns_without_enter(self) -> None:
        ops = _FakeOps()
        code, died = _run(ops, _request(mode=_MODE_PENDING, persist_delivery=True))
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        # The body is parked: injected once, emit -> persist, no marker wait / Enter / rollback.
        self.assertEqual(ops.events, ["inject", "emit", "persist"])
        self.assertEqual(ops.injected, [("%pT", "[[mk-1]] hello body")])
        self.assertEqual(ops.enter_presses, 0)
        self.assertEqual(ops.emitted[0].outcome.status, "pending_input")
        self.assertEqual(ops.emitted[0].outcome.reason, "ok")
        self.assertTrue(ops.persisted[0].persist_delivery)
        # Pending never threads retry / activation / turn-start lines.
        self.assertIsNone(ops.emitted[0].retry)
        self.assertIsNone(ops.emitted[0].turn_start_lines)


class TmuxTransportRailRollbackTest(unittest.TestCase):
    def test_standard_marker_miss_rolls_back_and_dies_without_enter(self) -> None:
        # The one C-u rollback cell: strict send, marker never observed.
        ops = _FakeOps(marker_observed=False)
        code, died = _run(ops, _request(mode=_MODE_STANDARD))
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        assert died is not None
        self.assertEqual(
            ops.events, ["inject", "wait", "rollback", "emit", "guidance", "die"]
        )
        # Enter was NOT pressed on a rolled-back marker miss.
        self.assertEqual(ops.enter_presses, 0)
        self.assertEqual(ops.emitted[0].outcome.status, "blocked")
        self.assertEqual(ops.emitted[0].outcome.reason, "marker_timeout")
        self.assertEqual(ops.persisted, [])
        self.assertEqual(ops.guidance, ["claude"])
        self.assertIn("C-u rollback was issued and Enter was not pressed", died.message)
        self.assertIn("target=%pT", died.message)
        self.assertIn("marker=[[mk-1]]", died.message)

    def test_queue_enter_marker_miss_does_not_roll_back(self) -> None:
        # A queue-enter marker miss is NOT a rollback cell: it presses Enter, never rolls back,
        # and lands on the relaxed sent/queue_enter terminal. Retry is disabled here (window=0)
        # to isolate the rollback question from the Enter-only retry loop.
        ops = _FakeOps(marker_observed=False)
        code, died = _run(
            ops, _request(mode=_MODE_QUEUE_ENTER, queue_enter_retry_window=0.0)
        )
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        self.assertNotIn("rollback", ops.events)
        self.assertEqual(ops.enter_presses, 1)
        self.assertEqual(ops.emitted[0].outcome.status, "sent")
        self.assertEqual(ops.emitted[0].outcome.reason, "queue_enter")


class TmuxTransportRailStandardConfirmTest(unittest.TestCase):
    def test_standard_confirmed_turn_start_sends_and_persists(self) -> None:
        ops = _FakeOps(marker_observed=True, standard_confirmed=True)
        code, died = _run(ops, _request(mode=_MODE_STANDARD, persist_delivery=True))
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        # Baseline capture (pre-Enter) -> Enter -> standard observation -> restore -> emit -> persist.
        self.assertEqual(
            ops.events,
            ["inject", "wait", "capture", "enter", "observe_std", "restore", "emit", "persist"],
        )
        self.assertEqual(ops.enter_presses, 1)
        self.assertEqual(ops.emitted[0].outcome.status, "sent")
        self.assertEqual(ops.emitted[0].outcome.reason, "ok")
        # A confirmed standard turn start carries the additive turn-start record lines.
        self.assertIsNotNone(ops.emitted[0].turn_start_lines)
        self.assertEqual(ops.persisted[0].turn_start_lines, ops.emitted[0].turn_start_lines)
        # No retry engaged on a standard rail; no ledger without herdr.
        self.assertIsNone(ops.emitted[0].retry)
        self.assertEqual(ops.ledgered, [])

    def test_standard_unconfirmed_turn_start_blocks_and_dies_no_rollback_no_resend(self) -> None:
        # The uncertain-delivery no-blind-retry cell.
        ops = _FakeOps(marker_observed=True, standard_confirmed=False)
        code, died = _run(ops, _request(mode=_MODE_STANDARD, persist_delivery=True))
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        assert died is not None
        self.assertEqual(
            ops.events, ["inject", "wait", "capture", "enter", "observe_std", "emit", "die"]
        )
        # Enter was pressed exactly once and never re-sent; no rollback.
        self.assertEqual(ops.enter_presses, 1)
        self.assertNotIn("rollback", ops.events)
        self.assertEqual(ops.emitted[0].outcome.status, "blocked")
        self.assertEqual(ops.emitted[0].outcome.reason, "turn_start_unconfirmed")
        # The opt-in persistence is NEVER reached on the uncertain terminal.
        self.assertEqual(ops.persisted, [])
        self.assertIn("No C-u rollback and no re-send were issued", died.message)
        self.assertIn("marker+body was typed once", died.message)
        self.assertIn("--mode standard", died.message)


class TmuxTransportRailQueueEnterTest(unittest.TestCase):
    def test_submit_delay_sleeps_before_the_enter_press(self) -> None:
        # checkpoint #14219 j#86687 R21-F2: the choreography is inject -> delay sleep -> Enter.
        # A positive-infinite delay therefore never reaches Enter, which is why the shared
        # send-semantics authority refuses it before anything is typed.
        ops = _FakeOps(marker_observed=True)
        code, died = _run(ops, _request(mode=_MODE_QUEUE_ENTER, submit_delay=0.5))
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        self.assertIn("sleep", ops.events)
        self.assertLess(ops.events.index("sleep"), ops.events.index("enter"))

    def test_queue_enter_marker_observed_sends_ok_without_retry(self) -> None:
        ops = _FakeOps(marker_observed=True)
        code, died = _run(ops, _request(mode=_MODE_QUEUE_ENTER))
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        # No baseline capture (not standard), Enter once, no standard observation, no retry.
        self.assertEqual(ops.events, ["inject", "wait", "enter", "restore", "emit", "persist"])
        self.assertEqual(ops.enter_presses, 1)
        self.assertEqual(ops.emitted[0].outcome.reason, "ok")
        self.assertIsNone(ops.emitted[0].retry)

    def test_enter_only_retry_engages_and_marker_lands_mid_retry(self) -> None:
        # queue-enter + marker-unobserved + policy-enabled: re-issue Enter until the marker lands.
        # window=6 / interval=2 -> max_retries=3. The 2nd retry probe sees the marker.
        ops = _FakeOps(
            marker_observed=False,
            captures=["", "[[mk-1]]"],  # 1st probe misses, 2nd probe sees the marker
        )
        code, died = _run(
            ops,
            _request(
                mode=_MODE_QUEUE_ENTER,
                queue_enter_retry_window=6.0,
                queue_enter_retry_interval=2.0,
            ),
        )
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        # Initial Enter + one retry Enter (the 2nd probe observed the marker and broke).
        self.assertEqual(ops.enter_presses, 2)
        # Marker observed via retry -> strict sent/ok (not the relaxed queue_enter).
        self.assertEqual(ops.emitted[0].outcome.reason, "ok")
        retry = ops.emitted[0].retry
        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry.enter_attempts, 2)
        self.assertTrue(retry.marker_observed)
        # The retry telemetry is threaded to persistence too.
        self.assertEqual(ops.persisted[0].retry, retry)

    def test_enter_only_retry_exhausts_window_stays_relaxed(self) -> None:
        ops = _FakeOps(marker_observed=False, captures=["", "", ""])  # never sees the marker
        code, died = _run(
            ops,
            _request(
                mode=_MODE_QUEUE_ENTER,
                queue_enter_retry_window=6.0,
                queue_enter_retry_interval=2.0,
            ),
        )
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        # Initial Enter + 3 retry Enters (max_retries = 6 // 2).
        self.assertEqual(ops.enter_presses, 4)
        self.assertEqual(ops.emitted[0].outcome.reason, "queue_enter")
        retry = ops.emitted[0].retry
        assert retry is not None
        self.assertEqual(retry.enter_attempts, 4)
        self.assertFalse(retry.marker_observed)

    def test_tmux_keeps_small_positive_retry_values(self) -> None:
        # Redmine #15242: Herdr rounds wait budgets to integer milliseconds,
        # but tmux's established marker rail accepts positive float values.
        # The Herdr-only minimum must not erase tmux's additional Enter.
        ops = _FakeOps(marker_observed=False, captures=["", "[[mk-1]]"])
        code, died = _run(
            ops,
            _request(
                mode=_MODE_QUEUE_ENTER,
                queue_enter_retry_window=0.002,
                queue_enter_retry_interval=0.0005,
            ),
        )
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        self.assertEqual(ops.enter_presses, 2)
        self.assertEqual(ops.emitted[0].outcome.reason, "ok")

    def test_retry_disabled_when_marker_observed(self) -> None:
        # Even with a policy window, an observed marker never engages the retry loop.
        ops = _FakeOps(marker_observed=True)
        code, _died = _run(
            ops,
            _request(
                mode=_MODE_QUEUE_ENTER,
                queue_enter_retry_window=6.0,
                queue_enter_retry_interval=2.0,
            ),
        )
        self.assertEqual(code, 0)
        self.assertEqual(ops.enter_presses, 1)
        self.assertIsNone(ops.emitted[0].retry)

    def test_herdr_queue_enter_threads_snapshot_and_ledgers(self) -> None:
        snapshot = QueueEnterTurnStartObservation(
            runtime_state="busy", read_ok=True, read_reason=None, poll_attempts=1
        )
        ops = _V2FakeOps(
            marker_observed=True,
            queue_enter_snapshot=snapshot,
            wait_kind="changed",
            binding=QueueEnterObservationOnlyWaitTests._binding(),
            runtime_state="turn_ended",
        )
        code, died = _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=True))
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        self.assertNotIn(
            "wait", ops.events, "Herdr must not run the tmux landing-marker poll"
        )
        # herdr queue-enter: the #13292 snapshot is observed and the #13300 ledger is recorded.
        self.assertIn("observe_qe", ops.events)
        self.assertIn("ledger", ops.events)
        obs = ops.emitted[0].outcome.queue_enter_turn_start_observation
        self.assertIsInstance(obs, dict)
        self.assertEqual((obs or {}).get("runtime_state"), "busy")
        # The queue-enter observation record lines ride the additive turn-start channel.
        self.assertIsNotNone(ops.emitted[0].turn_start_lines)
        # The ledger receives the same outcome; retry_outcome is None (no retry engaged).
        self.assertEqual(ops.ledgered[0][0], ops.emitted[0].outcome)
        self.assertIsNone(ops.ledgered[0][1])

    def test_tmux_queue_enter_records_no_ledger(self) -> None:
        # tmux 経路不変: a non-herdr send never records the herdr ledger and never snapshots.
        ops = _FakeOps(marker_observed=True)
        code, _died = _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=False))
        self.assertEqual(code, 0)
        self.assertIn("wait", ops.events, "the tmux compatibility rail is unchanged")
        self.assertNotIn("ledger", ops.events)
        self.assertNotIn("observe_qe", ops.events)
        self.assertIsNone(ops.emitted[0].outcome.queue_enter_turn_start_observation)


class TmuxTransportRailContextThreadingTest(unittest.TestCase):
    def test_redmine_anchor_threads_onto_the_outcome(self) -> None:
        ops = _FakeOps(marker_observed=True)
        _run(
            ops,
            _request(mode=_MODE_QUEUE_ENTER, anchor=RedmineAnchor(issue="9", journal="12")),
        )
        anchor = ops.emitted[0].outcome.anchor
        self.assertIsInstance(anchor, dict)
        self.assertEqual((anchor or {}).get("source"), "redmine")

    def test_asana_anchor_threads_onto_the_outcome(self) -> None:
        ops = _FakeOps(marker_observed=True)
        _run(
            ops,
            _request(mode=_MODE_QUEUE_ENTER, anchor=AsanaAnchor(task_id="T1", comment_id="C1")),
        )
        anchor = ops.emitted[0].outcome.anchor
        self.assertIsInstance(anchor, dict)
        self.assertEqual((anchor or {}).get("source"), "asana")

    def test_submit_intent_produces_submit_lines_only_when_set(self) -> None:
        with_intent = _FakeOps(marker_observed=True)
        _run(
            with_intent,
            _request(
                mode=_MODE_QUEUE_ENTER,
                submit_intent="submit_complete",
                submit_delivery_id="d-1",
            ),
        )
        self.assertIsNotNone(with_intent.emitted[0].submit_lines)
        without_intent = _FakeOps(marker_observed=True)
        _run(without_intent, _request(mode=_MODE_QUEUE_ENTER))
        self.assertIsNone(without_intent.emitted[0].submit_lines)

    def test_duplicate_lane_panes_empty_is_none_on_emit_but_raw_on_persist(self) -> None:
        ops = _FakeOps(marker_observed=True)
        _run(
            ops,
            _request(mode=_MODE_QUEUE_ENTER, persist_delivery=True, duplicate_lane_panes=[]),
        )
        self.assertIsNone(ops.emitted[0].duplicate_lane_panes)
        self.assertEqual(ops.persisted[0].duplicate_lane_panes, [])

    def test_focus_restore_activation_threads_to_emit_and_persist(self) -> None:
        restored = TargetActivationOutcome(
            activated=True, target_pane="%pT", previous_active_pane="%prev", restored=True
        )
        ops = _FakeOps(marker_observed=True, restore_result=restored)
        _run(ops, _request(mode=_MODE_QUEUE_ENTER, persist_delivery=True))
        self.assertEqual(ops.emitted[0].activation, restored)
        self.assertEqual(ops.persisted[0].activation, restored)


class _V2FakeOps(_FakeOps):
    """A fake carrying the #14203 j#87409 observation-only seams (arm / collect / binding)."""

    def __init__(
        self,
        *args,
        wait_kind="changed",
        wait_kinds=None,
        binding=None,
        runtime_state="turn_ended",
        resend_gate=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.wait_kind = wait_kind
        self.wait_kinds = list(wait_kinds or [])
        self.binding = binding
        self.runtime_state = runtime_state
        self.resend_gate = resend_gate or QueueEnterResendGate(RESEND_SKIP_BODY_ABSENT)
        self.armed_targets: List[str] = []
        self.armed_timeouts: List[int] = []
        self.collected: List[object] = []
        self.live_waits: set[object] = set()

    def arm_queue_enter_turn_wait(
        self, target: str, *, timeout_ms: int
    ) -> Optional[object]:
        self.events.append("arm_qe_wait")
        self.armed_targets.append(target)
        self.armed_timeouts.append(timeout_ms)
        armed = object()
        self.live_waits.add(armed)
        return armed

    def collect_queue_enter_turn_wait(self, armed: object) -> Optional[str]:
        self.events.append("collect_qe_wait")
        self.collected.append(armed)
        self.live_waits.discard(armed)
        return self.wait_kinds.pop(0) if self.wait_kinds else self.wait_kind

    def cancel_queue_enter_turn_wait(self, armed: object) -> None:
        self.events.append("cancel_qe_wait")
        self.live_waits.discard(armed)

    def queue_enter_turn_wait_pending(self, armed: object) -> bool:
        self.events.append("pending_qe_wait")
        return armed in self.live_waits

    def observe_queue_enter_gateway_binding(
        self, target: str
    ) -> Optional[dict[str, str]]:
        self.events.append("bind_qe")
        return self.binding

    def observe_queue_enter_runtime_state(self, target: str) -> Optional[str]:
        self.events.append("state_qe")
        return self.runtime_state

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict[str, str]],
    ) -> QueueEnterResendGate:
        self.events.append("gate_qe")
        return self.resend_gate


class QueueEnterObservationOnlyWaitTests(unittest.TestCase):
    """#14203 j#87409 (B-constrained): the pre-Enter observation-only wait + process binding."""

    def _snapshot(self, runtime_state="turn_ended"):
        return QueueEnterTurnStartObservation(
            runtime_state=runtime_state, read_ok=True, read_reason=None, poll_attempts=1
        )

    @staticmethod
    def _binding():
        assigned_name = "mzb1_ws_claude_lane"
        terminal_id = "terminal-test"
        locator = "%pT"
        revision = "4"
        return {
            "provider": "claude",
            "assigned_name": assigned_name,
            "locator": locator,
            "terminal_id": terminal_id,
            "row_revision": revision,
            "process_generation": (
                f"{len(assigned_name)}:{assigned_name}:"
                f"{len(terminal_id)}:{terminal_id}:"
                f"{len(locator)}:{locator}:r{revision}"
            ),
            "attestation_observed_at": "2026-07-24T17:00:00+00:00",
            "startup_action_id": "startup-GEN-A",
        }

    def test_the_wait_is_armed_before_the_first_enter(self) -> None:
        ops = _V2FakeOps(
            marker_observed=True,
            queue_enter_snapshot=self._snapshot(),
            binding=self._binding(),
        )
        code, died = _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=True))
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        self.assertIn("arm_qe_wait", ops.events)
        self.assertLess(ops.events.index("arm_qe_wait"), ops.events.index("enter"))
        self.assertEqual(ops.enter_presses, 1)  # the choreography is untouched (no double input)

    def test_a_fast_turn_persists_the_observed_start_despite_a_settled_snapshot(self) -> None:
        # The immediate start->turn_ended shape: the armed wait collected ``changed`` while
        # the post-choreography snapshot only ever saw the settled state. The persisted
        # observation carries BOTH + the action-time process binding, versioned additively.
        binding = self._binding()
        ops = _V2FakeOps(
            marker_observed=True, queue_enter_snapshot=self._snapshot("turn_ended"),
            wait_kind="changed", binding=binding,
        )
        code, _died = _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=True))
        self.assertEqual(code, 0)
        obs = ops.emitted[0].outcome.queue_enter_turn_start_observation
        self.assertEqual(obs.get("runtime_state"), "turn_ended")
        self.assertEqual(obs.get("event_wait_kind"), "changed")
        self.assertEqual(obs.get("gateway_binding"), binding)
        self.assertEqual(obs.get("observation_version"), 2)

    def test_a_wait_timeout_is_persisted_as_uncertain_non_success(self) -> None:
        binding = self._binding()
        ops = _V2FakeOps(
            marker_observed=True, queue_enter_snapshot=self._snapshot(),
            wait_kind="timeout", binding=binding,
        )
        code, died = _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=True))
        self.assertIsNotNone(died)
        self.assertIsNone(code)
        self.assertEqual(
            (ops.emitted[0].outcome.status, ops.emitted[0].outcome.reason),
            ("blocked", "turn_start_unconfirmed"),
        )
        obs = ops.emitted[0].outcome.queue_enter_turn_start_observation
        # Only a causal ``changed`` is published under the authoritative field.
        # Timeout remains explicit diagnostic telemetry and the body-absent gate
        # prevents the extra Enter.
        self.assertNotIn("event_wait_kind", obs)
        self.assertEqual(obs.get("first_event_wait_kind"), "timeout")
        self.assertEqual(obs.get("final_event_wait_kind"), "timeout")
        self.assertEqual(obs.get("resend_skipped_reason"), RESEND_SKIP_BODY_ABSENT)
        self.assertEqual(obs.get("enter_attempts"), 1)
        self.assertEqual(obs.get("gateway_binding"), binding)

    def test_timeout_rechecks_then_stops_after_the_confirming_retry(self) -> None:
        binding = self._binding()
        ops = _V2FakeOps(
            marker_observed=True,
            queue_enter_snapshot=self._snapshot(),
            wait_kinds=["timeout", "changed"],
            binding=binding,
            runtime_state="turn_ended",
            resend_gate=QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
        )
        code, died = _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=True))
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        self.assertEqual(ops.injected, [("%pT", "[[mk-1]] hello body")])
        self.assertEqual(ops.enter_presses, 2)
        self.assertEqual(len(ops.armed_targets), 2)
        self.assertLess(ops.events.index("arm_qe_wait"), ops.events.index("enter"))
        obs = ops.emitted[0].outcome.queue_enter_turn_start_observation
        self.assertEqual(obs.get("first_event_wait_kind"), "timeout")
        self.assertEqual(obs.get("final_event_wait_kind"), "changed")
        self.assertEqual(obs.get("event_wait_kind"), "changed")
        self.assertEqual(obs.get("enter_attempts"), 2)
        self.assertEqual(
            ops.emitted[0].outcome.injection_stage["stage"],
            STAGE_SUBMITTED_CONFIRMED,
        )

    def test_busy_baseline_can_be_nudged_but_never_reports_success(self) -> None:
        binding = self._binding()
        ops = _V2FakeOps(
            marker_observed=True,
            queue_enter_snapshot=self._snapshot("busy"),
            # A timeout can authorise one freshly gated nudge while busy. The
            # following changed event is not attributable to this send because
            # the pre-Enter state was busy, so it must stop rather than authorise
            # another Enter.
            wait_kinds=["timeout", "changed"],
            binding=binding,
            runtime_state="busy",
            resend_gate=QueueEnterResendGate(RESEND_SKIP_NONE, "busy"),
        )
        code, died = _run(
            ops,
            _request(
                mode=_MODE_QUEUE_ENTER,
                herdr_send=True,
                queue_enter_retry_window=4.0,
                queue_enter_retry_interval=2.0,
            ),
        )
        self.assertIsNotNone(died)
        self.assertIsNone(code)
        self.assertEqual(ops.enter_presses, 2)
        self.assertEqual(ops.events.count("gate_qe"), 1)
        obs = ops.emitted[0].outcome.queue_enter_turn_start_observation
        self.assertNotIn("event_wait_kind", obs)
        self.assertEqual(obs.get("baseline_runtime_state"), "busy")
        self.assertEqual(obs.get("first_event_wait_kind"), "timeout")
        self.assertEqual(obs.get("final_event_wait_kind"), "changed")
        self.assertEqual(obs.get("enter_attempts"), 2)
        self.assertEqual(
            ops.emitted[0].outcome.injection_stage["stage"],
            STAGE_UNCERTAIN_PARTIAL,
        )
        self.assertEqual(ops.emitted[0].outcome.status, "blocked")

    def test_generation_drift_refuses_the_extra_enter(self) -> None:
        ops = _V2FakeOps(
            marker_observed=True,
            queue_enter_snapshot=self._snapshot(),
            wait_kind="timeout",
            binding=self._binding(),
            resend_gate=QueueEnterResendGate(RESEND_SKIP_IDENTITY_DRIFT),
        )
        _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=True))
        self.assertEqual(ops.enter_presses, 1)
        obs = ops.emitted[0].outcome.queue_enter_turn_start_observation
        self.assertEqual(obs.get("resend_skipped_reason"), RESEND_SKIP_IDENTITY_DRIFT)

    def test_transport_failure_during_resend_gate_closes_to_typed_block(self) -> None:
        class _TransportFailingGateOps(_V2FakeOps):
            def evaluate_queue_enter_resend(
                self,
                target: str,
                text: str,
                receiver: str,
                baseline_binding: Optional[dict[str, str]],
            ) -> QueueEnterResendGate:
                self.events.append("gate_qe")
                raise TerminalTransportError("adapter-private failure detail")

        ops = _TransportFailingGateOps(
            marker_observed=True,
            queue_enter_snapshot=self._snapshot(),
            wait_kind="timeout",
            binding=self._binding(),
            runtime_state="turn_ended",
        )
        code, died = _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=True))

        self.assertIsNone(code)
        self.assertIsNotNone(died)
        self.assertEqual(ops.enter_presses, 1)
        self.assertEqual(ops.injected, [("%pT", "[[mk-1]] hello body")])
        outcome = ops.emitted[0].outcome
        self.assertEqual((outcome.status, outcome.reason), ("blocked", "transport_error"))
        self.assertEqual(outcome.injection_stage["stage"], STAGE_UNCERTAIN_PARTIAL)
        self.assertEqual(
            outcome.transport_failure,
            {"primitive": "send_keys(enter) (submit)"},
        )
        self.assertEqual(len(ops.ledgered), 1)
        self.assertEqual(ops.ledgered[0][0], outcome)
        self.assertIsNone(ops.ledgered[0][1])
        self.assertEqual(ops.ledgered[0][2], "herdr")
        self.assertEqual(ops.ledgered[0][3], "queue_enter_rail")
        self.assertEqual(ops.ledgered[0][4], "send_keys(enter) (submit)")
        self.assertLess(ops.events.index("ledger"), ops.events.index("emit"))
        self.assertNotIn("adapter-private", died.message)

    def test_zero_window_disables_the_extra_enter_but_keeps_the_initial_observation(self) -> None:
        ops = _V2FakeOps(
            marker_observed=True,
            queue_enter_snapshot=self._snapshot(),
            wait_kind="timeout",
            binding=self._binding(),
            resend_gate=QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
        )
        _run(
            ops,
            _request(
                mode=_MODE_QUEUE_ENTER,
                herdr_send=True,
                queue_enter_retry_window=0.0,
            ),
        )
        self.assertEqual(ops.enter_presses, 1)
        self.assertEqual(len(ops.armed_targets), 1)
        obs = ops.emitted[0].outcome.queue_enter_turn_start_observation
        self.assertEqual(obs.get("resend_skipped_reason"), "resend_disabled")

    def test_unsupported_retry_policy_refuses_before_body_or_enter(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf"), 1e306, 1e308):
            with self.subTest(value=value):
                ops = _V2FakeOps(marker_observed=True)
                code, died = _run(
                    ops,
                    _request(
                        mode=_MODE_QUEUE_ENTER,
                        herdr_send=True,
                        queue_enter_retry_window=value,
                    ),
                )
                self.assertIsNone(code)
                self.assertIsNotNone(died)
                self.assertEqual(ops.injected, [])
                self.assertEqual(ops.enter_presses, 0)
                self.assertEqual(ops.emitted[0].outcome.reason, "invalid_args")

    def test_sub_millisecond_policy_never_overflows_and_disables_extra_enter(self) -> None:
        ops = _V2FakeOps(
            marker_observed=True,
            queue_enter_snapshot=self._snapshot(),
            binding=self._binding(),
            wait_kind="changed",
        )

        code, died = _run(
            ops,
            _request(
                mode=_MODE_QUEUE_ENTER,
                herdr_send=True,
                queue_enter_retry_window=0.0001,
                queue_enter_retry_interval=5e-324,
            ),
        )

        self.assertIsNone(died)
        self.assertEqual(code, 0)
        self.assertEqual(ops.enter_presses, 1)
        self.assertEqual(ops.armed_timeouts, [8000])

    def test_a_generation_change_across_the_window_drops_the_v2_authority(self) -> None:
        # j#87418 F1: the pre-arm and post-collect generations differ (a same-name/-locator
        # recycle mid-window) -> the observed start + binding are NOT persisted as one
        # authority; the record keeps only the settled snapshot.
        class _RecycleOps(_V2FakeOps):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._bind_calls = 0

            def observe_queue_enter_gateway_binding(self, target):
                self.events.append("bind_qe")
                self._bind_calls += 1
                assigned_name = "mzb1_ws_claude_lane"
                terminal_id = "terminal-test"
                revision = str(self._bind_calls)
                return {
                    "provider": "claude",
                    "assigned_name": assigned_name,
                    "locator": "%pT",
                    "terminal_id": terminal_id,
                    "row_revision": revision,
                    "process_generation": (
                        f"{len(assigned_name)}:{assigned_name}:"
                        f"{len(terminal_id)}:{terminal_id}:3:%pT:r{revision}"
                    ),
                    "attestation_observed_at": f"2026-07-24T17:0{self._bind_calls}:00+00:00",
                    "startup_action_id": f"startup-GEN-{self._bind_calls}",  # DIFFERENT token
                }

        ops = _RecycleOps(
            marker_observed=True, queue_enter_snapshot=self._snapshot(), wait_kind="changed",
        )
        initial_name = "mzb1_ws_claude_lane"
        initial_terminal = "terminal-test"
        initial_locator = "%pT"
        initial_generation = (
            f"{len(initial_name)}:{initial_name}:"
            f"{len(initial_terminal)}:{initial_terminal}:"
            f"{len(initial_locator)}:{initial_locator}:r1"
        )
        code, died = _run(
            ops,
            _request(
                mode=_MODE_QUEUE_ENTER,
                herdr_send=True,
                herdr_process_generation=initial_generation,
            ),
        )
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        self.assertEqual(ops.enter_presses, 0)
        obs = ops.emitted[0].outcome.queue_enter_turn_start_observation
        self.assertNotIn("event_wait_kind", obs)
        self.assertNotIn("gateway_binding", obs)

    def test_an_ops_without_an_armed_wait_fails_closed_and_unversioned(self) -> None:
        ops = _FakeOps(marker_observed=True, queue_enter_snapshot=self._snapshot())
        code, died = _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=True))
        self.assertIsNotNone(died)
        self.assertIsNone(code)
        self.assertEqual(ops.enter_presses, 0)
        self.assertEqual(ops.injected, [])
        self.assertEqual(ops.emitted[0].outcome.reason, "target_unavailable")
        self.assertIsNone(
            ops.emitted[0].outcome.queue_enter_turn_start_observation
        )

    def test_the_tmux_and_standard_paths_never_arm(self) -> None:
        ops = _V2FakeOps(marker_observed=True)
        _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=False))
        self.assertNotIn("arm_qe_wait", ops.events)
        ops2 = _V2FakeOps(marker_observed=True, standard_confirmed=True)
        _run(ops2, _request(mode="standard", herdr_send=True))
        self.assertNotIn("arm_qe_wait", ops2.events)


class _ManualMonotonicClock:
    """Deterministic monotonic source for absolute retry-deadline tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _DeadlineFakeOps(_V2FakeOps):
    """Advance a manual clock when waits and interval sleeps consume budget."""

    def __init__(self, clock, *, collect_advances, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock
        self.collect_advances = list(collect_advances)
        self.arm_records: List[tuple[float, int]] = []

    def arm_queue_enter_turn_wait(
        self, target: str, *, timeout_ms: int
    ) -> Optional[object]:
        self.arm_records.append((self.clock(), timeout_ms))
        return super().arm_queue_enter_turn_wait(target, timeout_ms=timeout_ms)

    def collect_queue_enter_turn_wait(self, armed: object) -> Optional[str]:
        kind = super().collect_queue_enter_turn_wait(armed)
        if self.collect_advances:
            self.clock.advance(self.collect_advances.pop(0))
        return kind

    def sleep(self, seconds: float) -> None:
        self.events.append("sleep")
        self.clock.advance(seconds)


class QueueEnterAbsoluteRetryDeadlineTests(unittest.TestCase):
    """Pin every Herdr wait and retry to one absolute public window."""

    @staticmethod
    def _binding():
        return QueueEnterObservationOnlyWaitTests._binding()

    @staticmethod
    def _snapshot():
        return QueueEnterTurnStartObservation(
            runtime_state="turn_ended",
            read_ok=True,
            read_reason=None,
            poll_attempts=1,
        )

    def _execute(self, ops, clock, *, window: float, interval: float):
        try:
            code = TmuxTransportRailUseCase(ops, monotonic=clock).execute(
                _request(
                    mode=_MODE_QUEUE_ENTER,
                    herdr_send=True,
                    queue_enter_retry_window=window,
                    queue_enter_retry_interval=interval,
                )
            )
        except _FakeDie as exc:
            return None, exc
        return code, None

    def test_initial_wait_consuming_the_window_refuses_an_extra_enter(self) -> None:
        clock = _ManualMonotonicClock()
        ops = _DeadlineFakeOps(
            clock,
            collect_advances=[2.0],
            marker_observed=True,
            queue_enter_snapshot=self._snapshot(),
            wait_kinds=["timeout"],
            binding=self._binding(),
            runtime_state="turn_ended",
            resend_gate=QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
        )

        code, died = self._execute(ops, clock, window=2.0, interval=0.25)
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        self.assertEqual(ops.enter_presses, 1)
        self.assertEqual(ops.arm_records, [(0.0, 250)])
        self.assertNotIn("gate_qe", ops.events)
        observation = ops.emitted[0].outcome.queue_enter_turn_start_observation
        self.assertEqual(
            observation.get("resend_skipped_reason"),
            RESEND_SKIP_BUDGET_EXHAUSTED,
        )

    def test_interval_larger_than_remaining_window_does_not_sleep_or_resend(self) -> None:
        clock = _ManualMonotonicClock()
        ops = _DeadlineFakeOps(
            clock,
            collect_advances=[0.25],
            marker_observed=True,
            queue_enter_snapshot=self._snapshot(),
            wait_kinds=["timeout"],
            binding=self._binding(),
            runtime_state="turn_ended",
            resend_gate=QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
        )

        code, died = self._execute(ops, clock, window=1.0, interval=2.0)
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        self.assertEqual(ops.enter_presses, 1)
        self.assertEqual(ops.arm_records, [(0.0, 1000)])
        self.assertNotIn("sleep", ops.events)
        self.assertNotIn("gate_qe", ops.events)

    def test_consecutive_timeout_and_error_never_trigger_a_third_enter(self) -> None:
        clock = _ManualMonotonicClock()
        window = 2.0
        ops = _DeadlineFakeOps(
            clock,
            collect_advances=[0.1, 0.5],
            marker_observed=True,
            queue_enter_snapshot=self._snapshot(),
            wait_kinds=["timeout", "error", "changed"],
            binding=self._binding(),
            runtime_state="turn_ended",
            resend_gate=QueueEnterResendGate(RESEND_SKIP_NONE, "turn_ended"),
        )

        code, died = self._execute(ops, clock, window=window, interval=0.4)
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        self.assertEqual(ops.enter_presses, 2)
        self.assertEqual(len(ops.arm_records), 2)
        self.assertEqual(ops.wait_kinds, ["changed"])
        self.assertEqual(len(ops.collected), 2)
        for armed_at, timeout_ms in ops.arm_records:
            remaining_ms = int(max(0.0, window - armed_at) * 1000.0)
            self.assertGreater(timeout_ms, 0)
            self.assertLessEqual(timeout_ms, remaining_ms)
        observation = ops.emitted[0].outcome.queue_enter_turn_start_observation
        self.assertEqual(observation.get("enter_attempts"), 2)
        self.assertEqual(observation.get("final_event_wait_kind"), "error")


class _LiveGateReader:
    def __init__(self, result: AgentStateResult) -> None:
        self.result = result
        self.targets: List[str] = []

    def read_agent_state(self, target: str) -> AgentStateResult:
        self.targets.append(target)
        return self.result


class _LiveGateRail:
    def __init__(self, *, pane: str, state: str = "turn_ended", ok: bool = True) -> None:
        self.pane = pane
        self.reader = _LiveGateReader(
            AgentStateResult(
                ok=ok,
                state=state if ok else "unknown",
                reason=None if ok else "transport_error",
            )
        )
        self.arm_calls: List[tuple[str, int]] = []
        self.armed = object()

    def read_visible_pane(self, target: str) -> str:
        return self.pane

    def arm_turn_start_wait(self, target: str, *, timeout_ms: int):
        self.arm_calls.append((target, timeout_ms))
        return self.armed


class LiveQueueEnterResendGateTests(unittest.TestCase):
    """Production adapter reuses one active rail and fails closed before extra Enter."""

    binding = QueueEnterObservationOnlyWaitTests._binding()
    text = "[[mk-1]] hello body"

    @staticmethod
    def _ops() -> LiveTmuxTransportRailOps:
        return LiveTmuxTransportRailOps(emit=lambda *_args, **_kwargs: None)

    def _evaluate(
        self,
        rail: _LiveGateRail,
        *,
        current_binding=None,
        blocker=None,
    ) -> QueueEnterResendGate:
        from mozyo_bridge.application import commands as commands_mod

        ops = self._ops()
        binding = self.binding if current_binding is None else current_binding
        with patch.object(commands_mod, "active_herdr_turn_start_rail", rail), patch.object(
            ops, "observe_queue_enter_gateway_binding", return_value=binding
        ), patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_startup_admission.make_resend_screen_guard",
            return_value=lambda _content: blocker,
        ):
            return ops.evaluate_queue_enter_resend(
                "%pT", self.text, "claude", self.binding
            )

    def test_arm_reuses_the_exact_active_rail_and_timeout(self) -> None:
        from mozyo_bridge.application import commands as commands_mod

        rail = _LiveGateRail(pane=f"› {self.text}")
        ops = self._ops()
        with patch.object(commands_mod, "active_herdr_turn_start_rail", rail):
            armed = ops.arm_queue_enter_turn_wait("%pT", timeout_ms=731)
        self.assertIs(armed, rail.armed)
        self.assertEqual(rail.arm_calls, [("%pT", 731)])

    def test_exact_current_composer_and_readable_idle_state_allow_one_nudge(self) -> None:
        gate = self._evaluate(_LiveGateRail(pane=f"› {self.text}\n  ? for shortcuts"))
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.runtime_state, "turn_ended")

    def test_historical_prompt_with_busy_output_is_not_a_current_composer(self) -> None:
        rail = _LiveGateRail(
            pane=f"› {self.text}\n• Existing turn is still running",
            state="busy",
        )
        self.assertEqual(
            self._evaluate(rail).skip_reason,
            RESEND_SKIP_BODY_ABSENT,
        )

    def test_generation_drift_and_startup_screen_refuse(self) -> None:
        rail = _LiveGateRail(pane=f"› {self.text}")
        self.assertEqual(
            self._evaluate(rail, current_binding={**self.binding, "row_revision": "9"}).skip_reason,
            RESEND_SKIP_IDENTITY_DRIFT,
        )
        self.assertEqual(
            self._evaluate(rail, blocker="workspace_trust").skip_reason,
            RESEND_SKIP_STARTUP_SCREEN,
        )

    def test_blocked_unknown_and_failed_state_reads_refuse(self) -> None:
        cases = (
            ("blocked", True, RESEND_SKIP_RECEIVER_BLOCKED),
            ("unknown", True, RESEND_SKIP_STATE_NOT_INJECTABLE),
            ("unknown", False, RESEND_SKIP_STATE_UNREADABLE),
        )
        for state, ok, reason in cases:
            with self.subTest(state=state, ok=ok):
                rail = _LiveGateRail(pane=f"› {self.text}", state=state, ok=ok)
                self.assertEqual(self._evaluate(rail).skip_reason, reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
