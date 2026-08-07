from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.startup_transaction_fence import (
    StartupTransactionBusy,
    StartupTransactionError,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (
    RECORD_LAUNCH_BUSY_RETRY_SLEEP_SECONDS,
    StartupTransaction,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Fence:
    def __init__(self, outcomes) -> None:
        self.action = SimpleNamespace(action_id="startup-action")
        self.outcomes = list(outcomes)
        self.record_calls = []

    def reserve(self, _unit, _nonce):
        return self.action

    def record_participant(self, action_id, participant):
        self.record_calls.append((action_id, participant))
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "busy":
            raise StartupTransactionBusy("wrapper event append owns the lock")
        if isinstance(outcome, Exception):
            raise outcome
        return self.action


def _slot():
    return SimpleNamespace(
        provider="codex",
        assigned_name="mzb1_workspace_codex_default",
        locator="w1:p2",
    )


class StartupTransactionRecordLaunchTest(unittest.TestCase):
    def _transaction(self, fence, clock, *, budget=1.0):
        transaction = StartupTransaction(
            fence=fence,
            unit=object(),
            nonce="nonce",
            busy_retry_budget_seconds=budget,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        transaction.reserve()
        return transaction

    def test_wrapper_lock_contention_is_retried_then_recorded(self):
        fence = _Fence(["busy", "busy", "ok"])
        clock = _Clock()
        transaction = self._transaction(fence, clock)

        transaction.record_launch(_slot(), receipt="workspace=w1")

        self.assertEqual(len(fence.record_calls), 3)
        self.assertEqual(
            clock.sleeps,
            [
                RECORD_LAUNCH_BUSY_RETRY_SLEEP_SECONDS,
                RECORD_LAUNCH_BUSY_RETRY_SLEEP_SECONDS,
            ],
        )
        action_id, participant = fence.record_calls[-1]
        self.assertEqual(action_id, "startup-action")
        self.assertEqual(participant.role, "codex")
        self.assertEqual(participant.assigned_name, "mzb1_workspace_codex_default")
        self.assertEqual(participant.locator, "w1:p2")
        self.assertEqual(participant.receipt, "workspace=w1")

    def test_contention_still_fails_when_the_bounded_budget_is_exhausted(self):
        fence = _Fence(["busy", "busy", "busy", "ok"])
        clock = _Clock()
        transaction = self._transaction(fence, clock, budget=0.003)

        with self.assertRaises(StartupTransactionBusy):
            transaction.record_launch(_slot())

        self.assertEqual(len(fence.record_calls), 3)
        self.assertAlmostEqual(sum(clock.sleeps), 0.003)

    def test_non_contention_authority_error_is_not_retried(self):
        fence = _Fence([StartupTransactionError("store damaged"), "ok"])
        clock = _Clock()
        transaction = self._transaction(fence, clock)

        with self.assertRaisesRegex(StartupTransactionError, "store damaged"):
            transaction.record_launch(_slot())

        self.assertEqual(len(fence.record_calls), 1)
        self.assertEqual(clock.sleeps, [])

    def test_real_flock_held_by_wrapper_writer_is_waited_out(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fence = StartupTransactionFence(home=home)
            transaction = StartupTransaction(
                fence=fence,
                unit=StartupUnit(
                    workspace_id="workspace",
                    lane_id="default",
                    providers=("codex",),
                ),
                nonce="nonce",
                busy_retry_budget_seconds=0.5,
            )
            transaction.reserve()
            holder = StartupTransactionFence(home=home)
            acquired = threading.Event()

            def hold_like_wrapper_event_append():
                with holder._hold():
                    acquired.set()
                    time.sleep(0.03)

            thread = threading.Thread(target=hold_like_wrapper_event_append)
            thread.start()
            self.assertTrue(acquired.wait(timeout=0.5))

            transaction.record_launch(_slot())

            thread.join(timeout=0.5)
            self.assertFalse(thread.is_alive())
            recorded = fence.read(transaction.action_id)
            self.assertEqual(recorded.participant_for("codex").locator, "w1:p2")


if __name__ == "__main__":
    unittest.main()
