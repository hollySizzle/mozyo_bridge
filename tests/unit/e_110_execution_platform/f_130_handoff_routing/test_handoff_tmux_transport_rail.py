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

from mozyo_bridge.application.turn_start_observation import (
    QueueEnterTurnStartObservation,
    TurnStartObservation,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_tmux_transport_rail import (
    TmuxTransportRailOps,
    TmuxTransportRailRequest,
    TmuxTransportRailUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    AsanaAnchor,
    DeliveryOutcome,
    NormalizedAnchor,
    QueueEnterRetryOutcome,
    RedmineAnchor,
    TargetActivationOutcome,
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
    ) -> None:
        self.events.append("ledger")
        self.ledgered.append((outcome, retry_outcome))

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
        ops = _FakeOps(marker_observed=True, queue_enter_snapshot=snapshot)
        code, died = _run(ops, _request(mode=_MODE_QUEUE_ENTER, herdr_send=True))
        self.assertIsNone(died)
        self.assertEqual(code, 0)
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
