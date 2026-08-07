from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_ROLLED_BACK,
    PHASE_ROLLBACK_OWED,
    Participant,
    StartupTransactionBusy,
    StartupTransactionError,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
    PREPARED_PANE_ABSENT,
    PREPARED_PANE_PRESENT,
    REASON_INCOMPLETE,
    ROLLBACK_PREPARED_PANE_UNVERIFIABLE,
    ROLLBACK_PREPARED_RECEIPT_INVALID,
    PreparedPaneObservation,
    run_session_rollback,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback_ops import (  # noqa: E501
    LiveStartupRollbackOps,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    RECORD_LAUNCH_BUSY_RETRY_SLEEP_SECONDS,
    PaneBoundReceiptError,
    StartupTransaction,
    pane_bound_receipt,
    parse_pane_bound_receipt,
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


class _PreparedRollbackOps:
    def __init__(self, observation, *, remove_after_close=True) -> None:
        self.observation = observation
        self.remove_after_close = remove_after_close
        self.prepared_calls = []
        self.prepared_close_calls = []
        self.agent_close_calls = []

    def agent_rows(self):
        return []

    def runtime_state(self, _locator):
        raise AssertionError("an absent agent row has no runtime state")

    def observe_composer(self, _locator):
        raise AssertionError("an absent agent row has no composer")

    def startup_blocker(self, _provider, _locator):
        raise AssertionError("an absent agent row has no startup UI")

    def open_obligations(self, _workspace_id, _assigned_names):
        return []

    def close(self, workspace_id, lane_id, targets):
        self.agent_close_calls.append((workspace_id, lane_id, tuple(targets)))
        return SimpleNamespace(failed=())

    def prepared_pane(self, *, locator, workspace_id, tab_id):
        self.prepared_calls.append((locator, workspace_id, tab_id))
        return self.observation

    def close_prepared_pane(self, *, locator, workspace_id, tab_id):
        self.prepared_close_calls.append((locator, workspace_id, tab_id))
        if self.remove_after_close:
            self.observation = PreparedPaneObservation(state=PREPARED_PANE_ABSENT)
        return True, ""


def _rollback_action(home: Path, receipt: str):
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(
        StartupUnit(
            workspace_id="logical-workspace",
            lane_id="default",
            providers=("codex",),
        ),
        "prepared-pane-test",
    )
    action = fence.record_participant(
        action.action_id,
        Participant(
            role="codex",
            assigned_name="mzb1_logical_workspace_codex_default",
            locator="w1:p2",
            receipt=receipt,
        ),
    )
    fence.set_phase(action.action_id, PHASE_ROLLBACK_OWED)
    return fence, action.action_id


class PaneBoundReceiptTest(unittest.TestCase):
    def test_exact_v1_receipt_round_trips(self):
        native_name = native_name_for("mzb1_logical_workspace_codex_default")
        encoded = pane_bound_receipt(
            target_workspace="w1",
            target_tab="w1:t1",
            native_name=native_name,
        )

        decoded = parse_pane_bound_receipt(encoded)

        self.assertEqual(decoded.workspace_id, "w1")
        self.assertEqual(decoded.tab_id, "w1:t1")
        self.assertEqual(decoded.native_name, native_name)
        self.assertIsNone(parse_pane_bound_receipt("workspace=w1 tab=w1:t1"))

    def test_claimed_pane_receipt_is_not_normalized_or_downgraded(self):
        native_name = native_name_for("mzb1_logical_workspace_codex_default")
        malformed = (
            f"pane_bound_v1 tab=w1:t1 workspace=w1 native={native_name}"
        )
        with self.assertRaises(PaneBoundReceiptError):
            parse_pane_bound_receipt(malformed)
        with self.assertRaises(PaneBoundReceiptError):
            parse_pane_bound_receipt(
                f"pane_bound_v2 workspace=w1 tab=w1:t1 native={native_name}"
            )
        with self.assertRaises(PaneBoundReceiptError):
            parse_pane_bound_receipt(
                f"pane_bound_v1 workspace=w1 tab=w2:t1 native={native_name}"
            )
        with self.assertRaises(PaneBoundReceiptError):
            parse_pane_bound_receipt(
                f" pane_bound_v1 workspace=w1 tab=w1:t1 native={native_name}"
            )


class LivePreparedPaneObservationTest(unittest.TestCase):
    def test_herdr_080_shell_facts_do_not_infer_empty_input_from_rendered_text(self):
        pane_list = {
            "id": "cli:pane:list",
            "result": {
                "type": "pane_list",
                "panes": [
                    {
                        "pane_id": "w1:p2",
                        "workspace_id": "w1",
                        "tab_id": "w1:t1",
                        "agent_status": "unknown",
                    }
                ],
            },
        }
        process_info = {
            "id": "cli:pane:process_info",
            "result": {
                "type": "pane_process_info",
                "process_info": {
                    "pane_id": "w1:p2",
                    "shell_pid": 42,
                    "foreground_process_group_id": 42,
                    "foreground_processes": [
                        {"pid": 42, "argv0": "zsh", "name": "zsh"}
                    ],
                },
            },
        }
        replies = iter((pane_list, process_info))
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(next(replies)),
                stderr="",
            )

        ops = LiveStartupRollbackOps(repo_root=ROOT, env={})
        ops._retire_ops = SimpleNamespace(_binary=lambda: "/usr/bin/herdr")
        with patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_session_rollback_ops.subprocess.run",
            runner,
        ):
            observation = ops.prepared_pane(
                locator="w1:p2", workspace_id="w1", tab_id="w1:t1"
            )

        self.assertEqual(observation.state, PREPARED_PANE_PRESENT)
        self.assertTrue(observation.agent_absent)
        self.assertTrue(observation.shell_only)
        self.assertIsNone(observation.input_empty)
        self.assertEqual(
            calls,
            [
                ["/usr/bin/herdr", "pane", "list"],
                [
                    "/usr/bin/herdr",
                    "pane",
                    "process-info",
                    "--pane",
                    "w1:p2",
                ],
            ],
        )


class PreparedPaneRollbackTest(unittest.TestCase):
    def _receipt(self):
        return pane_bound_receipt(
            target_workspace="w1",
            target_tab="w1:t1",
            native_name=native_name_for("mzb1_logical_workspace_codex_default"),
        )

    def _present(self, *, input_empty):
        return PreparedPaneObservation(
            state=PREPARED_PANE_PRESENT,
            locator="w1:p2",
            workspace_id="w1",
            tab_id="w1:t1",
            agent_absent=True,
            shell_only=True,
            input_empty=input_empty,
            detail=(
                "Herdr does not expose an authoritative empty-input fact"
                if input_empty is None
                else ""
            ),
        )

    def test_absent_agent_is_not_absent_when_prepared_pane_input_is_unobservable(self):
        with tempfile.TemporaryDirectory() as directory:
            fence, action_id = _rollback_action(Path(directory), self._receipt())
            ops = _PreparedRollbackOps(self._present(input_empty=None))

            result = run_session_rollback(
                action_id=action_id, ops=ops, fence=fence, execute=True
            )

            self.assertEqual(result.state, "blocked")
            self.assertEqual(
                result.participants[0].verdict,
                ROLLBACK_PREPARED_PANE_UNVERIFIABLE,
            )
            self.assertEqual(ops.prepared_close_calls, [])
            self.assertEqual(ops.agent_close_calls, [])
            self.assertEqual(fence.read(action_id).phase, PHASE_ROLLBACK_OWED)

    def test_positive_future_input_fact_allows_exact_close_and_absence_recheck(self):
        with tempfile.TemporaryDirectory() as directory:
            fence, action_id = _rollback_action(Path(directory), self._receipt())
            ops = _PreparedRollbackOps(self._present(input_empty=True))

            result = run_session_rollback(
                action_id=action_id, ops=ops, fence=fence, execute=True
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.state, "completed")
            self.assertEqual(
                ops.prepared_close_calls, [("w1:p2", "w1", "w1:t1")]
            )
            self.assertEqual(ops.agent_close_calls, [])
            self.assertGreaterEqual(len(ops.prepared_calls), 2)
            self.assertEqual(
                fence.read(action_id).phase, PHASE_COMPLETED_ROLLED_BACK
            )

    def test_close_success_is_not_enough_when_exact_pane_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            fence, action_id = _rollback_action(Path(directory), self._receipt())
            ops = _PreparedRollbackOps(
                self._present(input_empty=True), remove_after_close=False
            )

            result = run_session_rollback(
                action_id=action_id, ops=ops, fence=fence, execute=True
            )

            self.assertEqual(result.state, "incomplete")
            self.assertEqual(result.reason, REASON_INCOMPLETE)
            self.assertFalse(result.participants[0].closed)
            self.assertEqual(fence.read(action_id).phase, PHASE_ROLLBACK_OWED)

    def test_malformed_pane_receipt_blocks_without_any_close(self):
        with tempfile.TemporaryDirectory() as directory:
            fence, action_id = _rollback_action(
                Path(directory),
                "pane_bound_v1 workspace=w1 tab=w1:t1 native=not-canonical",
            )
            ops = _PreparedRollbackOps(self._present(input_empty=True))

            result = run_session_rollback(
                action_id=action_id, ops=ops, fence=fence, execute=True
            )

            self.assertEqual(result.state, "blocked")
            self.assertEqual(
                result.participants[0].verdict,
                ROLLBACK_PREPARED_RECEIPT_INVALID,
            )
            self.assertEqual(ops.prepared_calls, [])
            self.assertEqual(ops.prepared_close_calls, [])

    def test_legacy_absent_agent_path_does_not_use_prepared_pane_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            fence, action_id = _rollback_action(Path(directory), "workspace=w1")
            ops = _PreparedRollbackOps(self._present(input_empty=True))

            result = run_session_rollback(
                action_id=action_id, ops=ops, fence=fence, execute=True
            )

            self.assertTrue(result.ok)
            self.assertEqual(ops.prepared_calls, [])
            self.assertEqual(ops.prepared_close_calls, [])


if __name__ == "__main__":
    unittest.main()
