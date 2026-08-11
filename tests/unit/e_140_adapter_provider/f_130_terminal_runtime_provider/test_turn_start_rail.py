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
    DEFAULT_ERROR_RESEND_WAIT_TIMEOUT_MS,
    DEFAULT_WAIT_TIMEOUT_MS,
    OUTCOME_ABSENT,
    OUTCOME_BLOCKED,
    OUTCOME_DELIVERED_NOT_STARTED,
    OUTCOME_INJECT_FAILED,
    OUTCOME_PRECONDITION_NOT_IDLE,
    MAX_ERROR_RESEND_WAIT_TIMEOUT_MS,
    OUTCOME_STARTED,
    RESEND_SKIP_BODY_ABSENT,
    RESEND_SKIP_BUDGET_EXHAUSTED,
    RESEND_SKIP_DISABLED,
    RESEND_SKIP_ENTER_SEND_FAILED,
    RESEND_SKIP_IDENTITY_DRIFT,
    RESEND_SKIP_IDENTITY_PROBE_UNBOUND,
    RESEND_SKIP_IDENTITY_UNCONFIRMED,
    RESEND_SKIP_NONE,
    RESEND_SKIP_PANE_UNREADABLE,
    RESEND_SKIP_RECEIVER_BLOCKED,
    RESEND_SKIP_SCREEN_GUARD_UNBOUND,
    RESEND_SKIP_STARTUP_SCREEN,
    RESEND_SKIP_STATE_NOT_INJECTABLE,
    RESEND_SKIP_STATE_UNREADABLE,
    WAIT_CHANGED,
    WAIT_ERROR,
    WAIT_TIMEOUT,
    HerdrTurnStartRail,
    TurnStartRailError,
    TurnStartResult,
    WaitResult,
    composer_retains_body,
    turn_start_rail_record_lines,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_resend_gate import (
    current_composer_retains_body,
)

TARGET = "w1:p1"
BODY = "Refs: Redmine #13248 please start the turn"
#: The durable assigned name a live identity probe reports for :data:`TARGET`.
IDENTITY = "mzb1_ws_claude_lane"
TERMINAL_A = "term_a"
TERMINAL_B = "term_b"


def _live_target_token(terminal_id: str, revision: int) -> str:
    return (
        f"{len(IDENTITY)}:{IDENTITY}:{len(terminal_id)}:{terminal_id}:"
        f"{len(TARGET)}:{TARGET}:r{revision}"
    )


FINGERPRINT_A_41 = _live_target_token(TERMINAL_A, 41)
FINGERPRINT_A_42 = _live_target_token(TERMINAL_A, 42)
FINGERPRINT_B_41 = _live_target_token(TERMINAL_B, 41)

#: The five machine keys `to_telemetry_dict` carried before Redmine #15202 added two.
TELEMETRY_KEYS_BEFORE_15202 = (
    "outcome",
    "snapshot_state",
    "wait_kind",
    "enter_resends",
    "reclassified_blocked",
)


def _clear_screen(_content: str):
    """A screen guard that finds no startup screen (the pane is a real composer)."""
    return None


def _trust_screen(_content: str) -> str:
    """A screen guard that matches a declared startup screen (a trust confirmation)."""
    return "workspace_trust_confirmation"


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
        self._resolved = False

    def collect(self) -> WaitResult:
        self._resolved = True
        self._events.append(("collect", self._index))
        return self._result

    def cancel(self) -> None:
        self._resolved = True
        self._events.append(("cancel", self._index))

    def pending(self) -> bool:
        return not self._resolved


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


class FakeIdentityProbe:
    """A scripted target-identity probe. Pops tokens in order; repeats the last.

    ``None`` in the script means "could not establish an identity" — the shape a live
    probe reports for an unreadable listing or an ambiguous locator.
    """

    def __init__(self, *tokens):
        self._tokens = list(tokens) if tokens else [IDENTITY]
        self.calls: list[str] = []

    def __call__(self, target: str):
        self.calls.append(target)
        if len(self._tokens) > 1:
            return self._tokens.pop(0)
        return self._tokens[0]


def _rail(reader, transport, wait, events=None, **kwargs) -> HerdrTurnStartRail:
    # A stable identity probe is the DEFAULT here so each error-path test states only
    # the condition it is about; the identity gate itself is pinned by the dedicated
    # unbound / unconfirmed / drift tests below, which pass `identity_probe=` explicitly.
    kwargs.setdefault("identity_probe", FakeIdentityProbe())
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

    def test_precondition_not_idle_for_each_non_injectable_state(self) -> None:
        # busy / blocked / unknown stay fail-closed (#13255 invariant, kept by
        # #13319). turn_ended is now injectable and has its own success path below.
        for state in (RUNTIME_BUSY, RUNTIME_BLOCKED, RUNTIME_UNKNOWN):
            with self.subTest(state=state):
                reader = FakeReader(AgentStateResult.observed(state))
                transport = FakeTransport()
                wait = FakeWait(WaitResult.changed())
                result = _rail(reader, transport, wait).drive_turn_start(TARGET, BODY)
                self.assertEqual(result.outcome, OUTCOME_PRECONDITION_NOT_IDLE)
                # Never injected and never armed a wait.
                self.assertEqual(transport.send_text_calls, [])
                self.assertEqual(wait.arm_count, 0)

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
# turn_ended (herdr `done`) is an injectable pre-injection state (Redmine #13319).
# ---------------------------------------------------------------------------
class TurnEndedInjectableTests(unittest.TestCase):
    """#13319 / design j#73077: a `done` (turn_ended) agent accepts a follow-up send.

    herdr holds `done` until the next input (measured 60s+), so `awaiting_input`
    would never arrive and every 2nd+ send used to fail closed with
    `precondition_not_idle`. `turn_ended` is now an injectable static state: the
    prior turn is over, so the wait armed before injection attributes the next
    `working` transition to this send. The `agent_state` mapping is unchanged and
    the snapshot stays `turn_ended` through the outcome + telemetry.
    """

    def test_turn_ended_injects_and_starts(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_TURN_ENDED))
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
        # The snapshot is preserved as turn_ended (never collapsed to awaiting_input).
        self.assertEqual(result.snapshot_state, RUNTIME_TURN_ENDED)
        self.assertEqual(result.to_telemetry_dict()["snapshot_state"], RUNTIME_TURN_ENDED)
        # It really injected: wait armed, body typed, Enter sent (check-then-wait).
        self.assertEqual(wait.arm_count, 1)
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 1)
        kinds = [e[0] for e in events]
        self.assertLess(kinds.index("arm"), kinds.index("send_text"))

    def test_turn_ended_wait_armed_before_injection(self) -> None:
        # Same check-then-wait ordering as awaiting_input: arm the wait first so the
        # working transition cannot land in the snapshot->wait race window.
        reader = FakeReader(AgentStateResult.observed(RUNTIME_TURN_ENDED))
        events: list = []
        transport = FakeTransport(events=events)
        wait = FakeWait(WaitResult.changed())
        wait.events = events
        _rail(reader, transport, wait, events=events).drive_turn_start(TARGET, BODY)
        self.assertEqual([e[0] for e in events][0], "arm")

    def test_turn_ended_timeout_delivered_not_started(self) -> None:
        # Injected from turn_ended but no turn started; re-snapshot still turn_ended
        # (not blocked) -> delivered_not_started. snapshot_state stays turn_ended.
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_TURN_ENDED),
            AgentStateResult.observed(RUNTIME_TURN_ENDED),
        )
        transport = FakeTransport()
        wait = FakeWait(WaitResult.timeout())
        result = _rail(
            reader, transport, wait, max_enter_resends=0
        ).drive_turn_start(TARGET, BODY)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.snapshot_state, RUNTIME_TURN_ENDED)
        self.assertFalse(result.reclassified_blocked)


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

    def test_timeout_path_ignores_the_screen_guard(self) -> None:
        # Redmine #15202 requirement 5: the E14 timeout rail is untouched. Even a guard
        # that matches a startup screen does not enter the timeout gate — that gate asks
        # its original two questions only, so a rail carrying a guard still resends on a
        # timeout exactly as it did before.
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.timeout(), WaitResult.changed())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_trust_screen
        )
        self.assertEqual(result.outcome, OUTCOME_STARTED)
        self.assertEqual(result.enter_resends, 1)
        self.assertEqual(result.first_wait_kind, WAIT_TIMEOUT)
        # The 8s landing window, not the 15s error-resend window.
        self.assertEqual(wait.timeouts, [DEFAULT_WAIT_TIMEOUT_MS, DEFAULT_WAIT_TIMEOUT_MS])
        self.assertEqual(len(transport.send_text_calls), 1)


# ---------------------------------------------------------------------------
# WAIT_ERROR Enter-resend rail (Redmine #15202).
# ---------------------------------------------------------------------------
class WaitErrorEnterResendTests(unittest.TestCase):
    """The #15199 shape: body typed, Enter sent, and the *observation* failed.

    Before #15202 a first wait that resolved ``error`` returned
    ``delivered_not_started`` with ``enter_resends=0`` and the request sat in the
    composer forever. These pin the recovery, its gates, and the record it leaves.
    """

    def test_error_resend_recovers_started(self) -> None:
        # 受け入れ条件: WAIT_ERROR の後に Enter が 1 回だけ再送され、開始が確認できれば成功。
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error("herdr wait failed to spawn"), WaitResult.changed())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_STARTED)
        self.assertEqual(result.enter_resends, 1)
        # 本文は1回だけ。追加されたのは Enter だけ。
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 2)
        self.assertEqual(wait.arm_count, 2)

    def test_recovered_start_still_records_the_first_error(self) -> None:
        # 実装要件 4: 最終結果だけで最初のエラーを上書きしない。
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error(), WaitResult.changed())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.wait_kind, WAIT_CHANGED)
        self.assertEqual(result.first_wait_kind, WAIT_ERROR)
        telemetry = result.to_telemetry_dict()
        self.assertEqual(telemetry["first_wait_kind"], WAIT_ERROR)
        self.assertEqual(telemetry["enter_resends"], 1)
        self.assertEqual(telemetry["wait_kind"], WAIT_CHANGED)
        # The human-readable record must not read as a clean first-try start either.
        self.assertIn("first wait error", turn_start_rail_record_lines(result)[0])

    def test_error_resend_re_waits_on_the_longer_window(self) -> None:
        # 実装要件 2: 再待機は最大15秒。The FIRST wait keeps the 8s landing window.
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error(), WaitResult.changed())
        _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(
            wait.timeouts, [DEFAULT_WAIT_TIMEOUT_MS, DEFAULT_ERROR_RESEND_WAIT_TIMEOUT_MS]
        )
        self.assertEqual(DEFAULT_ERROR_RESEND_WAIT_TIMEOUT_MS, 15000)

    def test_error_resend_window_is_configurable_below_the_maximum(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error(), WaitResult.changed())
        _rail(
            reader,
            transport,
            wait,
            max_enter_resends=1,
            error_resend_wait_timeout_ms=11000,
        ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
        self.assertEqual(wait.timeouts, [DEFAULT_WAIT_TIMEOUT_MS, 11000])

    def test_error_resend_window_above_fifteen_seconds_is_rejected(self) -> None:
        # 実装要件 2 の「再待機は最大15秒」は *maximum* であって default ではない。
        # positive チェックだけでは 21 秒を構成できてしまい要件に反する
        # (audit j#102755 finding 2)。clamp ではなく construction で拒否する。
        reader = FakeReader()
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        self.assertEqual(MAX_ERROR_RESEND_WAIT_TIMEOUT_MS, 15000)
        self.assertEqual(
            DEFAULT_ERROR_RESEND_WAIT_TIMEOUT_MS, MAX_ERROR_RESEND_WAIT_TIMEOUT_MS
        )
        for over_limit in (15001, 21000, 60000):
            with self.assertRaises(TurnStartRailError):
                HerdrTurnStartRail(
                    transport=transport,
                    reader=reader,
                    wait=wait,
                    error_resend_wait_timeout_ms=over_limit,
                )
        # The maximum itself is admissible (a boundary, not an exclusive bound).
        HerdrTurnStartRail(
            transport=transport,
            reader=reader,
            wait=wait,
            error_resend_wait_timeout_ms=MAX_ERROR_RESEND_WAIT_TIMEOUT_MS,
        )

    def test_still_unconfirmed_after_resend_is_delivered_not_started(self) -> None:
        # 受け入れ条件: 再送後に確認できなければ開始未確認として記録する。
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error(), WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertFalse(result.started)
        self.assertTrue(result.delivered)
        self.assertEqual(result.enter_resends, 1)
        self.assertEqual(result.first_wait_kind, WAIT_ERROR)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED)

    def test_one_extra_enter_is_the_cap_across_both_arming_kinds(self) -> None:
        # 実装要件 2 / 6: 追加 Enter は 1 回上限。A timeout that spends the budget leaves
        # nothing for a following error, so a mixed sequence cannot press Enter twice.
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.timeout(), WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.enter_resends, 1)
        self.assertEqual(result.first_wait_kind, WAIT_TIMEOUT)
        self.assertEqual(result.wait_kind, WAIT_ERROR)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED)
        # Body once, Enter twice total (the initial one plus the single resend).
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 2)

    def test_error_hard_cap_overrides_a_larger_configured_budget(self) -> None:
        # Audit j#102980: max_enter_resends is public and may exceed its default.
        # WAIT_ERROR still permits at most one extra Enter in the whole sequence.
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error(), WaitResult.error(), WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=2).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.enter_resends, 1)
        self.assertEqual(result.first_wait_kind, WAIT_ERROR)
        self.assertEqual(result.wait_kind, WAIT_ERROR)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED)
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 2)
        self.assertEqual(
            wait.timeouts,
            [DEFAULT_WAIT_TIMEOUT_MS, DEFAULT_ERROR_RESEND_WAIT_TIMEOUT_MS],
        )

    def test_timeout_then_error_cannot_spend_a_larger_budget_twice(self) -> None:
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.timeout(), WaitResult.error(), WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=2).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.enter_resends, 1)
        self.assertEqual(result.first_wait_kind, WAIT_TIMEOUT)
        self.assertEqual(result.wait_kind, WAIT_ERROR)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED)
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 2)

    def test_error_then_timeout_stays_at_the_error_hard_cap(self) -> None:
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error(), WaitResult.timeout(), WaitResult.timeout())
        result = _rail(reader, transport, wait, max_enter_resends=2).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.enter_resends, 1)
        self.assertEqual(result.first_wait_kind, WAIT_ERROR)
        self.assertEqual(result.wait_kind, WAIT_TIMEOUT)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_BUDGET_EXHAUSTED)
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 2)

    def test_timeout_only_sequence_keeps_the_configured_larger_budget(self) -> None:
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
        )
        transport = FakeTransport(
            read_pane=[PaneReadResult.success(BODY), PaneReadResult.success(BODY)]
        )
        wait = FakeWait(WaitResult.timeout(), WaitResult.timeout(), WaitResult.changed())
        result = _rail(reader, transport, wait, max_enter_resends=2).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_STARTED)
        self.assertEqual(result.enter_resends, 2)
        self.assertEqual(result.first_wait_kind, WAIT_TIMEOUT)
        self.assertEqual(result.wait_kind, WAIT_CHANGED)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_NONE)
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 3)
        self.assertEqual(
            wait.timeouts,
            [
                DEFAULT_WAIT_TIMEOUT_MS,
                DEFAULT_WAIT_TIMEOUT_MS,
                DEFAULT_WAIT_TIMEOUT_MS,
            ],
        )

    def test_no_error_resend_when_a_startup_screen_is_on_the_pane(self) -> None:
        # 実装要件 3 / 除外条件: workspace trust・権限確認・選択画面を検出したら再送しない。
        # #13760: a blind Enter into one accepts its default and destroys the request.
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_trust_screen
        )
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_STARTUP_SCREEN)
        # Zero extra keys: only the original Enter was ever pressed.
        self.assertEqual(len(transport.send_keys_calls), 1)
        self.assertEqual(wait.arm_count, 1)

    def test_no_error_resend_without_a_bound_screen_guard(self) -> None:
        # Fail-closed: with no classifier the rail cannot rule a startup screen out, so
        # it withholds the resend rather than press Enter into an uncharacterised pane.
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY
        )
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_SCREEN_GUARD_UNBOUND)
        # An unbound guard is refused BEFORE any read — a zero-cost refusal.
        self.assertEqual(transport.read_pane_calls, [])
        self.assertEqual(len(transport.send_keys_calls), 1)

    def test_a_raising_screen_guard_is_read_as_screen_detected(self) -> None:
        def _explodes(_content: str):
            raise RuntimeError("provider registry fault")

        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_explodes
        )
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_STARTUP_SCREEN)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(len(transport.send_keys_calls), 1)

    def test_no_error_resend_when_the_pane_read_fails(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(
            read_pane=[PaneReadResult.failure(REASON_TRANSPORT_ERROR)]
        )
        wait = FakeWait(WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_PANE_UNREADABLE)
        self.assertEqual(len(transport.send_keys_calls), 1)

    def test_no_error_resend_on_a_blank_pane(self) -> None:
        # A blank read is never evidence of a clear composer (#13760's live lane saw an
        # empty pane AFTER a dialog ate the body).
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success("   \n  ")])
        wait = FakeWait(WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_PANE_UNREADABLE)
        self.assertEqual(result.enter_resends, 0)

    def test_no_error_resend_when_the_composer_lost_the_body(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success("empty composer")])
        wait = FakeWait(WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_BODY_ABSENT)
        self.assertEqual(len(transport.send_keys_calls), 1)

    def test_no_error_resend_when_a_runtime_permission_prompt_is_up(self) -> None:
        # 除外条件「権限確認」: a runtime block is not a startup screen (the profile does
        # not declare it), so the gate's re-snapshot is what catches it. Enter here would
        # answer the prompt instead of submitting the turn.
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_BLOCKED),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_RECEIVER_BLOCKED)
        self.assertEqual(len(transport.send_keys_calls), 1)

    def test_no_error_resend_when_the_re_snapshot_read_fails(self) -> None:
        # 除外条件「read失敗」 (audit j#102755 finding 1). `AgentStateResult` forces
        # `state=unknown` on a mechanical failure, so a bare `!= blocked` test would
        # press Enter on the strength of a read that never happened — fail-OPEN.
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.failure(REASON_TRANSPORT_ERROR),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_STATE_UNREADABLE)
        self.assertEqual(len(transport.send_keys_calls), 1)

    def test_no_error_resend_on_an_observed_unknown_or_busy_receiver(self) -> None:
        # A read can SUCCEED and still carry `unknown` (an unrecognised status), and
        # `busy` means a turn is already running. Neither is a confirmed idle receiver,
        # so the gate demands positive membership of the injectable set rather than the
        # absence of `blocked`.
        for state in (RUNTIME_UNKNOWN, RUNTIME_BUSY):
            with self.subTest(state=state):
                reader = FakeReader(
                    AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
                    AgentStateResult.observed(state),
                )
                transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
                wait = FakeWait(WaitResult.error())
                result = _rail(
                    reader, transport, wait, max_enter_resends=1
                ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
                self.assertEqual(result.enter_resends, 0)
                self.assertEqual(
                    result.resend_skipped_reason, RESEND_SKIP_STATE_NOT_INJECTABLE
                )
                self.assertEqual(len(transport.send_keys_calls), 1)

    def test_turn_ended_re_snapshot_admits_the_resend(self) -> None:
        # The injectable set is the SAME one the pre-injection precondition uses, so
        # `turn_ended` (herdr `done`, a static state) admits the resend just as it
        # admits the original injection — the gate is strict, not arbitrary.
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_TURN_ENDED),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error(), WaitResult.changed())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_STARTED)
        self.assertEqual(result.enter_resends, 1)

    # --- target identity revalidation (audit j#102755 finding 3) ------------------
    def test_same_name_locator_and_revision_admits_one_bounded_enter(self) -> None:
        # The production token joins assigned name + terminal id + locator + row
        # revision. An unchanged conservative fingerprint permits one bounded Enter.
        probe = FakeIdentityProbe(FINGERPRINT_A_41, FINGERPRINT_A_41)
        reader = FakeReader(
            AgentStateResult.observed(RUNTIME_AWAITING_INPUT),
            AgentStateResult.observed(RUNTIME_TURN_ENDED),
        )
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error(), WaitResult.changed())
        result = _rail(
            reader, transport, wait, max_enter_resends=1, identity_probe=probe
        ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
        self.assertEqual(result.outcome, OUTCOME_STARTED)
        self.assertEqual(result.enter_resends, 1)
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 2)
        self.assertEqual(probe.calls, [TARGET, TARGET])

    def test_same_name_and_locator_revision_drift_withholds_the_enter(self) -> None:
        # Revision is not process identity, but it is a conservative mutation fence:
        # any drift withholds the extra Enter rather than guessing that it is benign.
        probe = FakeIdentityProbe(FINGERPRINT_A_41, FINGERPRINT_A_42)
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(
            reader, transport, wait, max_enter_resends=1, identity_probe=probe
        ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_IDENTITY_DRIFT)
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 1)
        self.assertEqual(transport.read_pane_calls, [])

    def test_different_terminal_same_pane_and_revision_withholds_the_enter(self) -> None:
        # A pane id and terminal revision can both be reused by another terminal. The
        # stable terminal id is therefore required in the live-target fingerprint.
        probe = FakeIdentityProbe(FINGERPRINT_A_41, FINGERPRINT_B_41)
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(
            reader, transport, wait, max_enter_resends=1, identity_probe=probe
        ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_IDENTITY_DRIFT)
        self.assertEqual(len(transport.send_text_calls), 1)
        self.assertEqual(len(transport.send_keys_calls), 1)
        self.assertEqual(transport.read_pane_calls, [])

    def test_no_error_resend_when_the_target_identity_drifted(self) -> None:
        # 除外条件「対象の識別情報変更」. A locator is transient: the pane can be killed
        # and its id reused, or the lane relaunched, inside the 8–15s wait window. The
        # outer identity gates all ran BEFORE the drive and none re-runs mid-drive, so
        # without this check the extra Enter can land on an agent that never got a body.
        probe = FakeIdentityProbe(IDENTITY, "mzb1_ws_claude_otherlane")
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(
            reader, transport, wait, max_enter_resends=1, identity_probe=probe
        ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_IDENTITY_DRIFT)
        self.assertEqual(len(transport.send_keys_calls), 1)
        # Probed once before injection and once before the resend — never memoised.
        self.assertEqual(probe.calls, [TARGET, TARGET])

    def test_no_error_resend_without_a_bound_identity_probe(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(
            reader, transport, wait, max_enter_resends=1, identity_probe=None
        ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(
            result.resend_skipped_reason, RESEND_SKIP_IDENTITY_PROBE_UNBOUND
        )
        # Refused before any pane read — an unbound probe costs nothing.
        self.assertEqual(transport.read_pane_calls, [])
        self.assertEqual(len(transport.send_keys_calls), 1)

    def test_no_error_resend_when_the_identity_cannot_be_established(self) -> None:
        # Either end unresolvable is a refusal: an unreadable listing before injection
        # leaves nothing to compare against, and one at resend time cannot rule drift out.
        for tokens in ((None, IDENTITY), (IDENTITY, None), (None, None)):
            with self.subTest(tokens=tokens):
                reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
                transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
                wait = FakeWait(WaitResult.error())
                result = _rail(
                    reader,
                    transport,
                    wait,
                    max_enter_resends=1,
                    identity_probe=FakeIdentityProbe(*tokens),
                ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
                self.assertEqual(result.enter_resends, 0)
                self.assertEqual(
                    result.resend_skipped_reason, RESEND_SKIP_IDENTITY_UNCONFIRMED
                )
                self.assertEqual(len(transport.send_keys_calls), 1)

    def test_a_raising_identity_probe_refuses_the_resend(self) -> None:
        def _explodes(_target):
            raise RuntimeError("agent list failed")

        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(
            reader, transport, wait, max_enter_resends=1, identity_probe=_explodes
        ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(
            result.resend_skipped_reason, RESEND_SKIP_IDENTITY_UNCONFIRMED
        )

    def test_blank_identity_tokens_never_compare_equal(self) -> None:
        # Two unresolvable answers must not satisfy the drift check by both being "".
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(
            reader,
            transport,
            wait,
            max_enter_resends=1,
            identity_probe=FakeIdentityProbe("   ", "   "),
        ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(
            result.resend_skipped_reason, RESEND_SKIP_IDENTITY_UNCONFIRMED
        )

    def test_timeout_path_does_not_consult_the_identity_probe(self) -> None:
        # 実装要件 5: the timeout gate keeps its original two checks. The probe is still
        # sampled once before injection (the baseline), but the timeout gate never asks
        # it, so a drifted identity does not change that path's behaviour.
        probe = FakeIdentityProbe(IDENTITY, "mzb1_ws_claude_otherlane")
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.timeout(), WaitResult.changed())
        result = _rail(
            reader, transport, wait, max_enter_resends=1, identity_probe=probe
        ).drive_turn_start(TARGET, BODY, screen_guard=_trust_screen)
        self.assertEqual(result.outcome, OUTCOME_STARTED)
        self.assertEqual(result.enter_resends, 1)
        self.assertEqual(probe.calls, [TARGET])

    def test_error_resend_enter_send_failure_stops_the_rail(self) -> None:
        events: list = []
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(
            send_keys=[
                TransportResult.success(),
                TransportResult.failure(REASON_TRANSPORT_ERROR),
            ],
            read_pane=[PaneReadResult.success(BODY)],
            events=events,
        )
        wait = FakeWait(WaitResult.error(), WaitResult.changed())
        wait.events = events
        result = _rail(
            reader, transport, wait, max_enter_resends=1, events=events
        ).drive_turn_start(TARGET, BODY, screen_guard=_clear_screen)
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_ENTER_SEND_FAILED)
        # The re-armed wait was cancelled, never collected — no phantom `started`.
        self.assertIn(("cancel", 1), events)
        self.assertNotIn(("collect", 1), events)

    def test_error_resend_disabled_when_cap_zero(self) -> None:
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=0).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_DELIVERED_NOT_STARTED)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_DISABLED)
        # A disabled rail never even reads the pane.
        self.assertEqual(transport.read_pane_calls, [])

    def test_absent_pane_never_resends(self) -> None:
        # WAIT_ABSENT is terminal: there is no pane to press Enter into.
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(read_pane=[PaneReadResult.success(BODY)])
        wait = FakeWait(WaitResult.absent())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_ABSENT)
        self.assertEqual(result.enter_resends, 0)
        self.assertEqual(result.resend_skipped_reason, RESEND_SKIP_NONE)
        self.assertEqual(transport.read_pane_calls, [])

    def test_inject_failure_still_precedes_any_resend(self) -> None:
        # 実装要件 5: a failed body injection is unchanged — no wait, no Enter resend.
        reader = FakeReader(AgentStateResult.observed(RUNTIME_AWAITING_INPUT))
        transport = FakeTransport(
            send_text=TransportResult.failure(REASON_TRANSPORT_ERROR)
        )
        wait = FakeWait(WaitResult.error())
        result = _rail(reader, transport, wait, max_enter_resends=1).drive_turn_start(
            TARGET, BODY, screen_guard=_clear_screen
        )
        self.assertEqual(result.outcome, OUTCOME_INJECT_FAILED)
        self.assertEqual(result.enter_resends, 0)
        self.assertIsNone(result.first_wait_kind)
        self.assertEqual(transport.send_keys_calls, [])


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

    def test_record_lines_name_a_withheld_resend(self) -> None:
        # Redmine #15202: `0 Enter re-send(s)` alone cannot tell "none was needed" from
        # "one was wanted and refused"; the record must say which, in tokens only.
        result = TurnStartResult(
            outcome=OUTCOME_DELIVERED_NOT_STARTED,
            snapshot_state=RUNTIME_AWAITING_INPUT,
            wait_kind=WAIT_ERROR,
            first_wait_kind=WAIT_ERROR,
            resend_skipped_reason=RESEND_SKIP_STARTUP_SCREEN,
        )
        line = turn_start_rail_record_lines(result)[0]
        self.assertIn(RESEND_SKIP_STARTUP_SCREEN, line)
        self.assertNotIn("\n", line)
        # The first wait matched the final one, so it is not restated.
        self.assertNotIn("first wait", line)

    def test_record_lines_stay_quiet_when_nothing_was_withheld(self) -> None:
        result = TurnStartResult(
            outcome=OUTCOME_STARTED,
            snapshot_state=RUNTIME_AWAITING_INPUT,
            wait_kind=WAIT_CHANGED,
            first_wait_kind=WAIT_CHANGED,
        )
        line = turn_start_rail_record_lines(result)[0]
        self.assertNotIn("withheld", line)
        self.assertNotIn("first wait", line)

    def test_record_lines_for_not_armed_outcome(self) -> None:
        result = TurnStartResult(
            outcome=OUTCOME_PRECONDITION_NOT_IDLE, snapshot_state=RUNTIME_BUSY
        )
        line = turn_start_rail_record_lines(result)[0]
        self.assertIn("not-armed", line)

    def test_to_telemetry_dict_carries_the_machine_fields(self) -> None:
        # Redmine #13255 j#72695: the structured telemetry the delivery outcome
        # carries (`DeliveryOutcome.turn_start_outcome`). Tokens + numbers only, the
        # five fields j#72602 decision 4 named plus the two Redmine #15202 added, and
        # no bounded-text `detail`.
        result = TurnStartResult(
            outcome=OUTCOME_BLOCKED,
            detail="wait timed out and a re-snapshot found a runtime block",
            snapshot_state=RUNTIME_AWAITING_INPUT,
            wait_kind=WAIT_TIMEOUT,
            enter_resends=2,
            reclassified_blocked=True,
            first_wait_kind=WAIT_ERROR,
            resend_skipped_reason=RESEND_SKIP_BUDGET_EXHAUSTED,
        )
        self.assertEqual(
            {
                "outcome": OUTCOME_BLOCKED,
                "snapshot_state": RUNTIME_AWAITING_INPUT,
                "wait_kind": WAIT_TIMEOUT,
                "enter_resends": 2,
                "reclassified_blocked": True,
                "first_wait_kind": WAIT_ERROR,
                "resend_skipped_reason": RESEND_SKIP_BUDGET_EXHAUSTED,
            },
            result.to_telemetry_dict(),
        )
        self.assertNotIn("detail", result.to_telemetry_dict())

    def test_to_telemetry_dict_keeps_the_original_five_unchanged(self) -> None:
        # Redmine #15202 is ADDITIVE: an older reader of the j#72602 five keys must see
        # the same values it always did, so the new keys can never be a silent migration.
        result = TurnStartResult(
            outcome=OUTCOME_STARTED,
            snapshot_state=RUNTIME_AWAITING_INPUT,
            wait_kind=WAIT_CHANGED,
        )
        telemetry = result.to_telemetry_dict()
        self.assertEqual(
            {
                "outcome": OUTCOME_STARTED,
                "snapshot_state": RUNTIME_AWAITING_INPUT,
                "wait_kind": WAIT_CHANGED,
                "enter_resends": 0,
                "reclassified_blocked": False,
            },
            {key: telemetry[key] for key in TELEMETRY_KEYS_BEFORE_15202},
        )

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

    def test_bad_first_wait_kind_rejected(self) -> None:
        with self.assertRaises(TurnStartRailError):
            TurnStartResult(outcome=OUTCOME_STARTED, first_wait_kind="done")

    def test_bad_resend_skipped_reason_rejected(self) -> None:
        # A closed vocabulary: a novel token can never reach a durable record.
        with self.assertRaises(TurnStartRailError):
            TurnStartResult(outcome=OUTCOME_STARTED, resend_skipped_reason="because")

    def test_rail_rejects_non_positive_error_resend_timeout(self) -> None:
        reader = FakeReader()
        transport = FakeTransport()
        wait = FakeWait(WaitResult.changed())
        with self.assertRaises(TurnStartRailError):
            HerdrTurnStartRail(
                transport=transport,
                reader=reader,
                wait=wait,
                error_resend_wait_timeout_ms=0,
            )


class ComposerRetainsBodyTests(unittest.TestCase):
    def test_retained(self) -> None:
        self.assertTrue(composer_retains_body("... " + BODY + " ...", BODY))

    def test_soft_wrap_tolerated(self) -> None:
        # A rendered composer soft-wraps the body across lines at a word boundary;
        # the whitespace-insensitive match keeps it.
        wrapped = "Refs: Redmine #13248 please\nstart   the turn"
        self.assertTrue(composer_retains_body(wrapped, BODY))

    def test_mid_token_wrap_tolerated(self) -> None:
        # Redmine #13322: the composer hard-wraps a long unbroken token mid-token
        # (the handoff marker), inserting a newline + indent inside the token. A
        # whitespace-COLLAPSE would leave a spurious space and miss it; the
        # whitespace-INSENSITIVE match must still recognise the retained body.
        marker = "[mozyo:handoff:source=redmine:issue=13322:journal=73136:kind=review_request:to=codex]"
        body = f"{marker} please start the turn"
        rendered = (
            "› [mozyo:handoff:source=redmine:issue=13322:journal=7313\n"
            "  6:kind=review_request:to=codex] please start the\n"
            "  turn"
        )
        self.assertTrue(composer_retains_body(rendered, body))

    def test_not_retained(self) -> None:
        self.assertFalse(composer_retains_body("empty composer", BODY))

    def test_empty_body_is_false(self) -> None:
        self.assertFalse(composer_retains_body("anything", "   "))

    def test_non_string_is_false(self) -> None:
        self.assertFalse(composer_retains_body(None, BODY))
        self.assertFalse(composer_retains_body("x", None))

    def test_current_composer_match_ignores_same_body_in_transcript(self) -> None:
        content = f"› {BODY}\nassistant: completed\n› "
        self.assertFalse(current_composer_retains_body(content, BODY))

    def test_current_composer_match_rejects_old_prompt_when_busy_output_follows(self) -> None:
        # While a receiver is busy it may render no fresh composer at all. The last
        # prompt is then the already-submitted request in transcript; unindented
        # receiver output below it proves that prompt is historical, not retained.
        content = f"› {BODY}\n• Working on the request\nmore output"
        self.assertFalse(current_composer_retains_body(content, BODY))

    def test_current_composer_match_accepts_wrapped_last_prompt_only(self) -> None:
        content = "assistant: old output\n› Refs: Redmine #15242 please\n  start the turn"
        self.assertTrue(
            current_composer_retains_body(
                content, "Refs: Redmine #15242 please start the turn"
            )
        )

    def test_current_composer_match_tolerates_indented_tui_footer(self) -> None:
        content = "› Refs: Redmine #15242 please\n  start the turn\n\n  ? for shortcuts"
        self.assertTrue(
            current_composer_retains_body(
                content, "Refs: Redmine #15242 please start the turn"
            )
        )

    def test_current_composer_requires_a_non_blank_last_prompt(self) -> None:
        self.assertFalse(current_composer_retains_body("transcript only " + BODY, BODY))
        self.assertFalse(current_composer_retains_body("›   ", BODY))


if __name__ == "__main__":
    unittest.main()
