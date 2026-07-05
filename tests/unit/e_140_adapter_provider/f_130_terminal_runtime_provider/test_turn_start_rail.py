"""Pure check-then-wait turn-start rail harness (Redmine #13248).

The **formal 4-case harness** for the herdr turn-start rail, driven entirely by
in-memory fakes (scripted transport / state reader / wait primitive) — no live
herdr binary. It pins:

- the four post-injection outcomes (``started`` / ``delivered_not_started`` /
  ``blocked`` / ``absent``);
- the two pre-injection fail-closed outcomes (``precondition_not_idle`` /
  ``inject_failed``);
- the check-then-wait *ordering* (snapshot -> arm wait -> inject -> collect), the
  E9 constraint that makes the rail correct;
- the Codex Enter-resend rail (E14): first wait timeout -> body still in composer
  -> re-send Enter (only Enter) -> started; and the resend-cap / skip paths;
- the E14 subscribe-time fail-safe (an immediate ``changed`` is accepted as
  started);
- the pure helpers and the redaction-safe record renderer, and the result
  invariants.

Live verification of the wait surface is out of scope (staged seam, #13254).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    AgentStateResult,
    RUNTIME_AWAITING_INPUT,
    RUNTIME_BLOCKED,
    RUNTIME_BUSY,
    RUNTIME_TURN_ENDED,
    RUNTIME_UNKNOWN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    REASON_INVALID_TARGET,
    REASON_TRANSPORT_ERROR,
    PaneReadResult,
    TransportResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    DEFAULT_WAIT_TIMEOUT_MS,
    OUTCOME_ABSENT,
    OUTCOME_BLOCKED,
    OUTCOME_DELIVERED_NOT_STARTED,
    OUTCOME_INJECT_FAILED,
    OUTCOME_PRECONDITION_NOT_IDLE,
    OUTCOME_STARTED,
    WAIT_CHANGED,
    WAIT_TIMEOUT,
    HerdrTurnStartRail,
    TurnStartRailError,
    TurnStartResult,
    WaitResult,
    composer_retains_body,
    turn_start_rail_record_lines,
)

TARGET = "w1:p1"
BODY = "Refs: Redmine #13248 please start the turn"


class FakeReader:
    """A scripted #13246 state reader. Pops states in order; repeats the last."""

    def __init__(self, *states: AgentStateResult):
        self._states = list(states) or [
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT)
        ]
        self.calls: list[str] = []

    def read_agent_state(self, target: str) -> AgentStateResult:
        self.calls.append(target)
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]


class FakeArmedWait:
    """One armed wait: replays a scripted :class:`WaitResult`, records collect/cancel."""

    def __init__(self, result: WaitResult, events: list, index: int):
        self._result = result
        self._events = events
        self._index = index

    def collect(self) -> WaitResult:
        self._events.append(("collect", self._index))
        return self._result

    def cancel(self) -> None:
        self._events.append(("cancel", self._index))


class FakeWait:
    """A scripted wait primitive. Each arm hands out the next scripted result."""

    def __init__(self, *results: WaitResult):
        self._results = list(results)
        self._armed = 0
        self.events: list = []
        self.timeouts: list[int] = []

    def arm(self, target: str, *, timeout_ms: int):
        self.events.append(("arm", target, timeout_ms))
        self.timeouts.append(timeout_ms)
        result = self._results[min(self._armed, len(self._results) - 1)]
        armed = FakeArmedWait(result, self.events, self._armed)
        self._armed += 1
        return armed

    @property
    def arm_count(self) -> int:
        return self._armed


class FakeTransport:
    """A scripted transport port. Records send/read calls in a shared event log."""

    backend = "herdr"

    def __init__(
        self,
        *,
        send_text=None,
        send_keys=None,
        read_pane=None,
        events: list = None,
    ):
        self._send_text = send_text or TransportResult.success()
        self._send_keys = list(send_keys) if send_keys else [TransportResult.success()]
        self._read_pane = list(read_pane) if read_pane else [
            PaneReadResult.success(BODY)
        ]
        self.events = events if events is not None else []
        self.send_text_calls: list = []
        self.send_keys_calls: list = []
        self.read_pane_calls: list = []

    def send_text(self, target: str, text: str) -> TransportResult:
        self.events.append(("send_text", target))
        self.send_text_calls.append((target, text))
        return self._send_text

    def send_keys(self, target: str, keys: str) -> TransportResult:
        self.events.append(("send_keys", target))
        self.send_keys_calls.append((target, keys))
        idx = min(len(self.send_keys_calls) - 1, len(self._send_keys) - 1)
        return self._send_keys[idx]

    def read_pane(self, target: str, **kwargs) -> PaneReadResult:
        self.events.append(("read_pane", target))
        self.read_pane_calls.append(target)
        idx = min(len(self.read_pane_calls) - 1, len(self._read_pane) - 1)
        return self._read_pane[idx]


def _rail(reader, transport, wait, events=None, **kwargs) -> HerdrTurnStartRail:
    return HerdrTurnStartRail(
        transport=transport, reader=reader, wait=wait, **kwargs
    )


# ---------------------------------------------------------------------------
# The four post-injection outcomes.
# ---------------------------------------------------------------------------
class FourCaseHarnessTests(unittest.TestCase):
    def test_started(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        events: list = []
        transport = FakeTransport(events=events)
        wait = FakeWait(WaitResult.changed())
        wait.events = events
        result = _rail(reader, transport, wait, events=events).drive_turn_start(
            TARGET, BODY
        )
        self.assertEqual(result.outcome, OUTCOME_STARTED)
        self.assertTrue(result.started)
        self.assertTrue(result.delivered)
        self.assertEqual(result.wait_kind, WAIT_CHANGED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.snapshot_state, RUNTIME_AWAITING_INPUT)

    def test_delivered_not_started(self) -> None:
        # snapshot idle -> wait timeout -> re-snapshot idle (not blocked) -> DNS.
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        transport = FakeTransport()
        wait = FakeWait(WaitResult.timeout())
        result = _rail(
            reader, transport, wait, max_enter_resends=0
        ).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertTrue(result.delivered)
        self.assertFalse(result.started)
        self.assertEqual(result.wait_kind, WAIT_TIMEOUT)
        self.assertFalse(result.reclassified_blocked)

    def test_blocked_reclassified_on_timeout(self) -> None:
        # snapshot idle -> wait timeout -> re-snapshot blocked -> BLOCKED.
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_BLOCKED),
        )
        transport = FakeTransport()
        wait = FakeWait(WaitResult.timeout())
        result = _rail(
            reader, transport, wait, max_enter_resends=0
        ).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_BLOCKED)
        self.assertTrue(result.reclassified_blocked)
        self.assertEqual(result.wait_kind, WAIT_TIMEOUT)

    def test_absent(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport()
        wait = FakeWait(WaitResult.absent())
        result = _rail(reader, transport, wait).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_ABSENT)
        self.assertTrue(result.delivered)

    def test_wait_error_fails_closed_to_delivered_not_started(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport()
        wait = FakeWait(WaitResult.error("spawn boom"))
        result = _rail(reader, transport, wait).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)


# ---------------------------------------------------------------------------
# The two pre-injection fail-closed outcomes.
# ---------------------------------------------------------------------------
class PreconditionTests(unittest.TestCase):
    def test_precondition_not_idle_when_busy_never_injects(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_BUSY))
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        result = _rail(reader, transport, wait).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_PRECONDITION_NOT_IDLE)
        self.assertFalse(result.delivered)
        # Never injected and never armed a wait.
        self.assertEqual(transport.send_text_calls, [])
        self.assertEqual(transport.send_keys_calls, [])
        self.assertEqual(wait.arm_count, 0)
        self.assertIsNone(result.wait_kind)
        self.assertEqual(result.snapshot_state, RUNTIME_BUSY)

    def test_precondition_not_idle_for_each_non_idle_state(self) -> None:
        for state in (RUNTIME_BUSY, RUNTIME_BLOCKED, RUNTIME_TURN_ENDED, RUNTIME_UNKNOWN):
            with self.subTest(state=state):
                reader = FakeReader(AgentStateResult.observed(state))
                transport = FakeTransport()
                wait = FakeWait(WaitResult.changed())
                result = _rail(reader, transport, wait).drive_turn_start(TARGET, BODY)
                self.assertEqual(result.outcome, OUTCOME_PRECONDITION_NOT_IDLE)

    def test_unreadable_snapshot_fails_closed_to_precondition(self) -> None:
        # A mechanically failed read degrades to state=unknown -> not idle.
        reader = FakeReader(AgentStateResult.failure(REASON_TRANSPORT_ERROR))
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        result = _rail(reader, transport, wait).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_PRECONDITION_NOT_IDLE)
        self.assertEqual(result.snapshot_state, RUNTIME_UNKNOWN)
        self.assertEqual(wait.arm_count, 0)

    def test_inject_failed_on_send_text_cancels_wait(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        events: list = []
        transport = FakeTransport(
            send_text=TransportResult.failure(REASON_TRANSPORT_ERROR), events=events
        )
        wait = FakeWait(WaitResult.changed())
        wait.events = events
        result = _rail(reader, transport, wait, events=events).drive_turn_start(
            TARGET, BODY
        )
        self.assertEqual(result.outcome, OUTCOME_INJECT_FAILED)
        self.assertFalse(result.delivered)
        # The armed wait was cancelled, never collected; Enter was never sent.
        self.assertIn(("cancel", 0), events)
        self.assertNotIn(("collect", 0), events)
        self.assertEqual(transport.send_keys_calls, [])

    def test_inject_failed_on_send_keys_cancels_wait(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        events: list = []
        transport = FakeTransport(
            send_keys=[TransportResult.failure(REASON_TRANSPORT_ERROR)], events=events
        )
        wait = FakeWait(WaitResult.changed())
        wait.events = events
        result = _rail(reader, transport, wait, events=events).drive_turn_start(
            TARGET, BODY
        )
        self.assertEqual(result.outcome, OUTCOME_INJECT_FAILED)
        self.assertIn(("cancel", 0), events)
        self.assertNotIn(("collect", 0), events)


# ---------------------------------------------------------------------------
# Check-then-wait ordering — the correctness invariant (E9 / E12).
# ---------------------------------------------------------------------------
class OrderingTests(unittest.TestCase):
    def test_wait_armed_before_injection(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        events: list = []
        transport = FakeTransport(events=events)
        wait = FakeWait(WaitResult.changed())
        wait.events = events
        _rail(reader, transport, wait, events=events).drive_turn_start(TARGET, BODY)
        # The first event is the arm; send_text/send_keys come after; collect last.
        kinds = [e[0] for e in events]
        self.assertEqual(kinds[0], "arm")
        self.assertLess(kinds.index("arm"), kinds.index("send_text"))
        self.assertLess(kinds.index("send_text"), kinds.index("send_keys"))
        self.assertLess(kinds.index("send_keys"), kinds.index("collect"))

    def test_wait_timeout_ms_passed_through(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        _rail(reader, transport, wait, wait_timeout_ms=12345).drive_turn_start(
            TARGET, BODY
        )
        self.assertEqual(wait.timeouts, [12345])

    def test_default_wait_timeout(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        _rail(reader, transport, wait).drive_turn_start(TARGET, BODY)
        self.assertEqual(wait.timeouts, [DEFAULT_WAIT_TIMEOUT_MS])

    def test_reader_property_exposes_injected_reader(self) -> None:
        # Redmine #13292: the queue-enter telemetry-only observation borrows the
        # resolved rail's state reader for a read-only snapshot (no drive_turn_start).
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        self.assertIs(_rail(reader, transport, wait).reader, reader)


# ---------------------------------------------------------------------------
# Codex Enter-resend rail (E14).
# ---------------------------------------------------------------------------
class EnterResendTests(unittest.TestCase):
    def test_resend_recovers_started(self) -> None:
        # first wait timeout, body still in composer -> resend Enter -> started.
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.timeout(), WaitResult.changed())
        result = _rail(
            reader, transport, wait, max_enter_resends=1
        ).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_STARTED)
        self.assertEqual(result.enter_resends, 1)
        # Body typed once; Enter sent twice (initial + one resend); wait armed twice.
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 2)
        self.assertEqual(wait.arm_count, 2)
        self.assertEqual(len(transport.read_pane_calls), 1)

    def test_resend_cap_exhausted_is_delivered_not_started(self) -> None:
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.timeout(), WaitResult.timeout())
        result = _rail(
            reader, transport, wait, max_enter_resends=1
        ).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 1)
        # Only one resend attempted (cap=1); Enter sent twice total.
        self.assertEqual(len(transport.send_keys_calls), 2)
        self.assertEqual(wait.arm_count, 2)

    def test_no_resend_when_composer_cleared(self) -> None:
        # first wait timeout, but body NOT in composer -> no resend.
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success("empty composer")])
        wait = FakeWait(WaitResult.timeout())
        result = _rail(
            reader, transport, wait, max_enter_resends=1
        ).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(len(transport.send_keys_calls), 1)
        self.assertEqual(wait.arm_count, 1)

    def test_no_resend_when_pane_read_fails(self) -> None:
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        transport = FakeTransport(
            read_pane=[PaneReadResult.failure(REASON_TRANSPORT_ERROR)]
        )
        wait = FakeWait(WaitResult.timeout())
        result = _rail(
            reader, transport, wait, max_enter_resends=1
        ).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(len(transport.send_keys_calls), 1)

    def test_resend_disabled_when_cap_zero(self) -> None:
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.timeout())
        result = _rail(
            reader, transport, wait, max_enter_resends=0
        ).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        # The resend rail never even read the pane.
        self.assertEqual(transport.read_pane_calls, [])

    def test_resend_send_keys_failure_stops_rail(self) -> None:
        # resend Enter fails -> cancel rearmed wait, stop, classify last timeout.
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        events: list = []
        transport = FakeTransport(
            send_keys=[
                TransportResult.success(),
                TransportResult.failure(REASON_TRANSPORT_ERROR),
            ],
            read_pane=[PaneReadResult.success(BODY)],
            events=events,
        )
        wait = FakeWait(WaitResult.timeout(), WaitResult.timeout())
        wait.events = events
        result = _rail(
            reader, transport, wait, max_enter_resends=1, events=events
        ).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        # The rearmed (second) wait was cancelled, never collected.
        self.assertIn(("cancel", 1), events)
        self.assertNotIn(("collect", 1), events)

    def test_immediate_changed_accepted_as_started(self) -> None:
        # E14 subscribe-time fail-safe: an immediate changed is a real start.
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed("event returned in ~11ms"))
        result = _rail(reader, transport, wait).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_STARTED)
        self.assertEqual(result.enter_resends, 0)


# ---------------------------------------------------------------------------
# Injected clock (settle) and record renderer + invariants.
# ---------------------------------------------------------------------------
class ClockAndRecordTests(unittest.TestCase):
    def test_settle_sleep_called_between_text_and_enter(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        slept: list = []
        _rail(
            reader,
            transport,
            wait,
            sleep=slept.append,
            inject_settle_seconds=0.25,
        ).drive_turn_start(TARGET, BODY)
        self.assertEqual(slept, [0.25])

    def test_no_settle_by_default(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        slept: list = []
        _rail(reader, transport, wait, sleep=slept.append).drive_turn_start(
            TARGET, BODY
        )
        self.assertEqual(slept, [])

    def test_record_lines_are_redaction_safe(self) -> None:
        result = TurnStartResult(
            outcome=OUTCOME_BLOCKED,
            snapshot_state=RUNTIME_AWAITING_INPUT,
            wait_kind=WAIT_TIMEOUT,
            enter_resends=1,
            reclassified_blocked=True,
        )
        lines = turn_start_rail_record_lines(result)
        self.assertEqual(len(lines), 1)
        joined = lines[0]
        self.assertIn(OUTCOME_BLOCKED, joined)
        self.assertIn("1 Enter re-send", joined)
        # Redaction-safe: no absolute path, single line.
        self.assertNotIn("/Users/", joined)
        self.assertNotIn("\n", joined)

    def test_record_lines_for_not_armed_outcome(self) -> None:
        result = TurnStartResult(
            outcome=OUTCOME_PRECONDITION_NOT_IDLE, snapshot_state=RUNTIME_BUSY
        )
        line = turn_start_rail_record_lines(result)[0]
        self.assertIn("not-armed", line)

    def test_to_telemetry_dict_carries_the_five_machine_fields(self) -> None:
        # Redmine #13255 j#72695: the structured telemetry the delivery outcome
        # carries (`DeliveryOutcome.turn_start_outcome`). Tokens + numbers only, the
        # exact five fields j#72602 decision 4 named, and no bounded-text `detail`.
        result = TurnStartResult(
            outcome=OUTCOME_BLOCKED,
            detail="wait timed out and a re-snapshot found a runtime block",
            snapshot_state=RUNTIME_AWAITING_INPUT,
            wait_kind=WAIT_TIMEOUT,
            enter_resends=2,
            reclassified_blocked=True,
        )
        self.assertEqual(
            {
                "outcome": OUTCOME_BLOCKED,
                "snapshot_state": RUNTIME_AWAITING_INPUT,
                "wait_kind": WAIT_TIMEOUT,
                "enter_resends": 2,
                "reclassified_blocked": True,
            },
            result.to_telemetry_dict(),
        )
        self.assertNotIn("detail", result.to_telemetry_dict())

    def test_to_telemetry_dict_not_armed_wait_kind_is_none(self) -> None:
        result = TurnStartResult(
            outcome=OUTCOME_PRECONDITION_NOT_IDLE, snapshot_state=RUNTIME_BUSY
        )
        self.assertIsNone(result.to_telemetry_dict()["wait_kind"])
        self.assertEqual(0, result.to_telemetry_dict()["enter_resends"])


class ResultInvariantTests(unittest.TestCase):
    def test_bad_outcome_rejected(self) -> None:
        with self.assertRaises(TurnStartRailError):
            TurnStartResult(outcome="nope")

    def test_bad_snapshot_state_rejected(self) -> None:
        with self.assertRaises(TurnStartRailError):
            TurnStartResult(outcome=OUTCOME_STARTED, snapshot_state="idle")

    def test_bad_wait_kind_rejected(self) -> None:
        with self.assertRaises(TurnStartRailError):
            TurnStartResult(outcome=OUTCOME_STARTED, wait_kind="done")

    def test_negative_enter_resends_rejected(self) -> None:
        with self.assertRaises(TurnStartRailError):
            TurnStartResult(outcome=OUTCOME_STARTED, enter_resends=-1)

    def test_bad_wait_result_kind_rejected(self) -> None:
        with self.assertRaises(TurnStartRailError):
            WaitResult(kind="pending")

    def test_rail_rejects_non_positive_timeout(self) -> None:
        reader = FakeReader()
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        with self.assertRaises(TurnStartRailError):
            HerdrTurnStartRail(
                transport=transport, reader=reader, wait=wait, wait_timeout_ms=0
            )

    def test_rail_rejects_negative_resends(self) -> None:
        reader = FakeReader()
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        with self.assertRaises(TurnStartRailError):
            HerdrTurnStartRail(
                transport=transport, reader=reader, wait=wait, max_enter_resends=-1
            )


class ComposerRetainsBodyTests(unittest.TestCase):
    def test_retained(self) -> None:
        self.assertTrue(composer_retains_body("... " + BODY + " ...", BODY))

    def test_soft_wrap_tolerated(self) -> None:
        # A rendered composer soft-wraps the body across lines; whitespace collapse
        # keeps the match.
        wrapped = "Refs: Redmine #13248 please\nstart   the turn"
        self.assertTrue(composer_retains_body(wrapped, BODY))

    def test_not_retained(self) -> None:
        self.assertFalse(composer_retains_body("empty composer", BODY))

    def test_empty_body_is_false(self) -> None:
        self.assertFalse(composer_retains_body("anything", "   "))

    def test_non_string_is_false(self) -> None:
        self.assertFalse(composer_retains_body(None, BODY))
        self.assertFalse(composer_retains_body("x", None))


if __name__ == "__main__":
    unittest.main()
