"""Unit tests for the supervisor event pump (Redmine #13758 Q1 / j#79507).

Pins the event-driven activation logic with injected seams (no live Herdr / supervisor):
multiplex first-woke-wins (a single target never blocks the others), the woken target threads
a local-wake hint into the next pass, the pump is bounded, a timeout / error runs the bounded
whole-roster reconciliation, and a failed pass never kills the pump.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_wake import (
    WAKE_ERROR,
    WAKE_TIMED_OUT,
    WAKE_WOKE,
    WakeSignal,
)
from dataclasses import dataclass

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_event_pump import (
    EventPumpTarget,
    HERDR_STATUS_TURN_ENDED,
    build_event_pump_seams,
    multiplex_wait,
    pump_targets_from,
    run_event_pump,
)


class _StubSupervisor:
    def __init__(self):
        self.calls = []

    def run_once(self, *, mode, wake_hints):
        self.calls.append((mode, tuple(wake_hints)))
        return {"mode": mode}


class BuildEventPumpSeamsTest(unittest.TestCase):
    """review R6-F1: the wait seam spawns the resolved herdr `wait agent-status`, not mozyo-bridge."""

    def test_wait_argv_uses_resolved_herdr_binary_and_turn_ended_status(self):
        captured = {}

        def fake_runner(argv):
            captured["argv"] = list(argv)
            return 0, ""  # rc 0 -> woke

        _pass, _targets_fn, wait_multiplex_fn = build_event_pump_seams(
            supervisor=_StubSupervisor(),
            targets_fn=lambda: [],
            wait_binary="/trusted/bin/herdr",
            timeout_ms=1234,
            wait_runner=fake_runner,
        )
        signal, woken = wait_multiplex_fn([_t("ws1", "13758", "la", "mzb1_ws1_claude_la")])
        self.assertEqual(signal.kind, WAKE_WOKE)
        self.assertEqual(
            captured["argv"],
            [
                "/trusted/bin/herdr", "wait", "agent-status", "mzb1_ws1_claude_la",
                "--status", HERDR_STATUS_TURN_ENDED, "--timeout", "1234",
            ],
        )
        self.assertEqual(HERDR_STATUS_TURN_ENDED, "done")  # the turn_ended raw status, not `working`

    def test_empty_binary_degrades_to_timeout_only_wait(self):
        # No trusted herdr binary -> the seam must NOT spawn anything; it times out (still re-reads).
        def fake_runner(argv):  # pragma: no cover - must never be called
            raise AssertionError("no subprocess must be spawned without a resolved binary")

        _pass, _targets_fn, wait_multiplex_fn = build_event_pump_seams(
            supervisor=_StubSupervisor(),
            targets_fn=lambda: [],
            wait_binary="",
            timeout_ms=1000,
            wait_runner=fake_runner,
        )
        signal, woken = wait_multiplex_fn([_t("ws1", "13758", "la", "mzb1_ws1_claude_la")])
        self.assertEqual(signal.kind, WAKE_TIMED_OUT)
        self.assertIsNone(woken)


@dataclass(frozen=True)
class _ObsAgent:
    name: str
    managed: bool
    workspace_id: str = ""
    lane_id: str = ""


class PumpTargetsTest(unittest.TestCase):
    def test_managed_active_agent_becomes_a_target(self):
        agents = [
            _ObsAgent("mzb1_ws1_claude_la", True, "ws1", "la"),
            _ObsAgent("foreign", False, "ws1", "lb"),  # unmanaged -> skipped
        ]
        active = {("ws1", "la"): "13758"}
        targets = pump_targets_from(agents, lambda ws, lane: active.get((ws, lane), ""))
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].target, "mzb1_ws1_claude_la")
        self.assertEqual(targets[0].issue, "13758")

    def test_inactive_or_unresolved_lane_is_skipped(self):
        agents = [_ObsAgent("mzb1_ws1_claude_lz", True, "ws1", "lz")]
        targets = pump_targets_from(agents, lambda ws, lane: "")  # no active issue
        self.assertEqual(targets, [])


def _t(ws, issue, lane, target):
    return EventPumpTarget(workspace_id=ws, issue=issue, lane_id=lane, target=target)


class MultiplexWaitTest(unittest.TestCase):
    def test_first_woke_target_wins(self):
        # target A times out, target B wakes -> B is returned (A did not block B).
        outcomes = {"tA": False, "tB": True}  # False->timeout, True->woke

        def wait_builder(t):
            return lambda: outcomes[t.target]

        targets = [_t("ws1", "1", "la", "tA"), _t("ws1", "2", "lb", "tB")]
        signal, woken = multiplex_wait(targets, wait_builder=wait_builder)
        self.assertEqual(signal.kind, WAKE_WOKE)
        self.assertEqual(woken.target, "tB")

    def test_all_timeout_returns_timeout_no_target(self):
        def wait_builder(t):
            return lambda: False

        targets = [_t("ws1", "1", "la", "tA"), _t("ws1", "2", "lb", "tB")]
        signal, woken = multiplex_wait(targets, wait_builder=wait_builder)
        self.assertEqual(signal.kind, WAKE_TIMED_OUT)
        self.assertIsNone(woken)

    def test_empty_targets_is_benign_timeout(self):
        signal, woken = multiplex_wait([], wait_builder=lambda t: (lambda: True))
        self.assertEqual(signal.kind, WAKE_TIMED_OUT)
        self.assertIsNone(woken)

    def test_wait_error_is_surfaced_when_no_woke(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_wake import (
            HerdrWaitError,
        )

        def wait_builder(t):
            def _w():
                raise HerdrWaitError("boom")
            return _w

        signal, woken = multiplex_wait([_t("ws1", "1", "la", "tA")], wait_builder=wait_builder)
        self.assertEqual(signal.kind, WAKE_ERROR)
        self.assertIsNone(woken)

    def test_blocking_first_target_does_not_block_a_second_wake(self):
        # review R6-F2: the waits are armed CONCURRENTLY. Target A blocks on its whole bounded
        # window; target B wakes immediately. A truly-multiplex wait returns B promptly instead of
        # serializing behind A's block (a serial loop would deadlock this test on A).
        import threading
        import time

        release = threading.Event()
        self.addCleanup(release.set)  # never leak the blocked worker thread

        def wait_builder(t):
            if t.target == "tA":
                return lambda: release.wait(5.0)  # blocks up to 5s -> falsy (timeout) if not woken
            return lambda: True  # tB wakes at once

        targets = [_t("ws1", "1", "la", "tA"), _t("ws1", "2", "lb", "tB")]
        started = time.monotonic()
        signal, woken = multiplex_wait(targets, wait_builder=wait_builder)
        elapsed = time.monotonic() - started
        self.assertEqual(signal.kind, WAKE_WOKE)
        self.assertEqual(woken.target, "tB")
        self.assertLess(elapsed, 2.0)  # did NOT wait out A's 5s block (would be ~5s if serial)


class RunEventPumpTest(unittest.TestCase):
    def _run(self, *, targets, wakes, max_iterations=3):
        # wakes: list of (WakeSignal, woken_target) per iteration.
        passes = []

        def supervisor_pass(mode, hints):
            passes.append((mode, tuple(hints)))
            return {"mode": mode}

        seq = list(wakes)

        def wait_multiplex_fn(_targets):
            return seq.pop(0) if seq else (WakeSignal(kind=WAKE_TIMED_OUT), None)

        results = run_event_pump(
            supervisor_pass=supervisor_pass,
            targets_fn=lambda: targets,
            wait_multiplex_fn=wait_multiplex_fn,
            max_iterations=max_iterations,
        )
        return passes, results

    def test_bounded_iterations(self):
        # review R6-F3: a startup bootstrap pass + one pass per (wait -> consume) iteration.
        passes, results = self._run(targets=[], wakes=[], max_iterations=3)
        self.assertEqual(len(results), 1 + 3)  # bootstrap + 3
        self.assertEqual(len(passes), 1 + 3)
        self.assertEqual(passes[0][0], "bounded_reconciliation")  # bootstrap first, no hint
        self.assertEqual(results[0]["wake"], "bootstrap")

    def test_zero_iterations_runs_nothing(self):
        passes, results = self._run(targets=[], wakes=[], max_iterations=0)
        self.assertEqual(results, [])
        self.assertEqual(passes, [])

    def test_observed_wake_is_consumed_in_the_same_invocation(self):
        # review R6-F3: an observed edge drives a local_wake reconcile pass WITHIN the bounded
        # invocation (even at the CLI default of one iteration), not a deferred "next" pass.
        woke = (WakeSignal(kind=WAKE_WOKE), _t("ws1", "13758", "la", "tA"))
        passes, results = self._run(
            targets=[_t("ws1", "13758", "la", "tA")], wakes=[woke], max_iterations=1
        )
        # bootstrap (bounded, no hint), then the single iteration consumes the wake as local_wake.
        self.assertEqual(passes[0][0], "bounded_reconciliation")
        self.assertEqual(passes[0][1], ())
        self.assertEqual(passes[1][0], "local_wake")
        self.assertEqual(passes[1][1], (("ws1", "13758"),))
        self.assertEqual(results[1]["wake"], WAKE_WOKE)
        self.assertEqual(results[1]["woke_target"], "tA")

    def test_timeout_runs_bounded_reconciliation(self):
        passes, results = self._run(
            targets=[_t("ws1", "1", "la", "tA")],
            wakes=[(WakeSignal(kind=WAKE_TIMED_OUT), None)],
            max_iterations=1,
        )
        # iteration 1 (after bootstrap): a timeout -> whole-roster bounded reconciliation, no hint.
        self.assertEqual(passes[1][0], "bounded_reconciliation")
        self.assertEqual(passes[1][1], ())

    def test_failed_pass_does_not_kill_the_pump(self):
        calls = {"n": 0}

        def supervisor_pass(mode, hints):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")  # the bootstrap pass raises
            return {"ok": True}

        results = run_event_pump(
            supervisor_pass=supervisor_pass,
            targets_fn=lambda: [],
            wait_multiplex_fn=lambda t: (WakeSignal(kind=WAKE_TIMED_OUT), None),
            max_iterations=2,
        )
        self.assertEqual(len(results), 1 + 2)  # bootstrap + 2, survived the first pass's exception
        self.assertFalse(results[0]["pass_ok"])
        self.assertTrue(results[1]["pass_ok"])
        self.assertTrue(results[2]["pass_ok"])


if __name__ == "__main__":
    unittest.main()
