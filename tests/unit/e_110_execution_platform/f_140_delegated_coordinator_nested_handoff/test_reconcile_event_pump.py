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
    multiplex_wait,
    pump_targets_from,
    run_event_pump,
)


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
        passes, results = self._run(targets=[], wakes=[], max_iterations=3)
        self.assertEqual(len(results), 3)
        self.assertEqual(len(passes), 3)

    def test_woken_target_threads_local_wake_hint_into_next_pass(self):
        woke = (WakeSignal(kind=WAKE_WOKE), _t("ws1", "13758", "la", "tA"))
        passes, results = self._run(
            targets=[_t("ws1", "13758", "la", "tA")], wakes=[woke], max_iterations=2
        )
        # iteration 1: bounded reconciliation (no prior hint); iteration 2: local_wake with hint.
        self.assertEqual(passes[0][0], "bounded_reconciliation")
        self.assertEqual(passes[0][1], ())
        self.assertEqual(passes[1][0], "local_wake")
        self.assertEqual(passes[1][1], (("ws1", "13758"),))

    def test_timeout_runs_bounded_reconciliation(self):
        passes, results = self._run(
            targets=[_t("ws1", "1", "la", "tA")],
            wakes=[(WakeSignal(kind=WAKE_TIMED_OUT), None)],
            max_iterations=2,
        )
        self.assertEqual(passes[1][0], "bounded_reconciliation")  # no hint -> whole-roster
        self.assertEqual(passes[1][1], ())

    def test_failed_pass_does_not_kill_the_pump(self):
        calls = {"n": 0}

        def supervisor_pass(mode, hints):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return {"ok": True}

        results = run_event_pump(
            supervisor_pass=supervisor_pass,
            targets_fn=lambda: [],
            wait_multiplex_fn=lambda t: (WakeSignal(kind=WAKE_TIMED_OUT), None),
            max_iterations=2,
        )
        self.assertEqual(len(results), 2)  # survived the first pass's exception
        self.assertFalse(results[0]["pass_ok"])
        self.assertTrue(results[1]["pass_ok"])


if __name__ == "__main__":
    unittest.main()
