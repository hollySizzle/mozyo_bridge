"""herdr agent-status -> mozyo runtime receiver-state mapping tests (Redmine #13246).

Pins the pure, fail-closed mapping and its result records with no herdr binary:
every recognised herdr status maps to its runtime state, and every unknown /
unrecognised / non-string / parse-derived value fails closed to ``unknown``
without raising. The load-bearing doctrine boundary is pinned explicitly —
herdr ``done`` maps to ``turn_ended`` (an assistant-turn signal), never to a
close / task ``done`` — so a later caller cannot silently promote it to workflow
truth. No network / tmux / herdr is touched here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    HERDR_AGENT_STATUSES,
    HERDR_STATUS_BLOCKED,
    HERDR_STATUS_DONE,
    HERDR_STATUS_IDLE,
    HERDR_STATUS_UNKNOWN,
    HERDR_STATUS_WORKING,
    RUNTIME_AWAITING_INPUT,
    RUNTIME_BLOCKED,
    RUNTIME_BUSY,
    RUNTIME_RECEIVER_STATES,
    RUNTIME_TURN_ENDED,
    RUNTIME_UNKNOWN,
    AgentStateError,
    AgentStateListResult,
    AgentStateResult,
    map_agent_status,
    normalize_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    REASON_BINARY_NOT_FOUND,
    REASON_TRANSPORT_ERROR,
    TerminalTransportError,
)

# Attention-state vocabulary the runtime mapping must NOT collide with: this
# import pins that the mozyo runtime states are a *different* vocabulary from the
# derived cockpit attention states (so a runtime signal is never mistaken for
# workflow truth).
from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention import (
    STATE_DONE as ATTENTION_STATE_DONE,
)


class VocabularyTest(unittest.TestCase):
    def test_herdr_statuses_closed(self) -> None:
        self.assertEqual(
            HERDR_AGENT_STATUSES,
            {
                HERDR_STATUS_WORKING,
                HERDR_STATUS_BLOCKED,
                HERDR_STATUS_IDLE,
                HERDR_STATUS_DONE,
                HERDR_STATUS_UNKNOWN,
            },
        )

    def test_runtime_states_closed(self) -> None:
        self.assertEqual(
            RUNTIME_RECEIVER_STATES,
            {
                RUNTIME_BUSY,
                RUNTIME_BLOCKED,
                RUNTIME_AWAITING_INPUT,
                RUNTIME_TURN_ENDED,
                RUNTIME_UNKNOWN,
            },
        )

    def test_runtime_turn_ended_is_not_attention_done(self) -> None:
        # Doctrine boundary: herdr ``done`` -> ``turn_ended`` must be a distinct
        # token from the derived attention ``done`` (close_gate_satisfied), so it
        # can never be silently promoted to workflow truth.
        self.assertNotEqual(RUNTIME_TURN_ENDED, ATTENTION_STATE_DONE)
        self.assertNotIn(ATTENTION_STATE_DONE, RUNTIME_RECEIVER_STATES)


class MapAgentStatusTest(unittest.TestCase):
    def test_recognised_statuses(self) -> None:
        self.assertEqual(map_agent_status(HERDR_STATUS_WORKING), RUNTIME_BUSY)
        self.assertEqual(map_agent_status(HERDR_STATUS_BLOCKED), RUNTIME_BLOCKED)
        self.assertEqual(map_agent_status(HERDR_STATUS_IDLE), RUNTIME_AWAITING_INPUT)
        self.assertEqual(map_agent_status(HERDR_STATUS_DONE), RUNTIME_TURN_ENDED)
        self.assertEqual(map_agent_status(HERDR_STATUS_UNKNOWN), RUNTIME_UNKNOWN)

    def test_done_never_maps_to_a_completion_state(self) -> None:
        # The single most important fail-closed rule: a finished turn is not a
        # finished task.
        mapped = map_agent_status("done")
        self.assertEqual(mapped, RUNTIME_TURN_ENDED)
        self.assertNotEqual(mapped, "done")

    def test_case_and_whitespace_insensitive(self) -> None:
        self.assertEqual(map_agent_status("  WORKING "), RUNTIME_BUSY)
        self.assertEqual(map_agent_status("Blocked"), RUNTIME_BLOCKED)

    def test_unrecognised_token_fails_closed(self) -> None:
        for bad in ("starting", "running", "busy", "", "   ", "done "):
            with self.subTest(bad=bad):
                # ``done `` (trailing space) is stripped, so it maps; the rest
                # are unrecognised. Only assert the unrecognised ones fail closed.
                if bad.strip().lower() in HERDR_AGENT_STATUSES:
                    continue
                self.assertEqual(map_agent_status(bad), RUNTIME_UNKNOWN)

    def test_non_string_fails_closed_without_raising(self) -> None:
        for bad in (None, 5, True, [], {}, ("working",), object()):
            with self.subTest(bad=bad):
                self.assertEqual(map_agent_status(bad), RUNTIME_UNKNOWN)

    def test_always_returns_a_valid_runtime_state(self) -> None:
        for value in ("working", "nope", None, 5, "IDLE"):
            self.assertIn(map_agent_status(value), RUNTIME_RECEIVER_STATES)


class NormalizeStatusTest(unittest.TestCase):
    def test_normalises_recognised(self) -> None:
        self.assertEqual(normalize_status(" Working "), HERDR_STATUS_WORKING)

    def test_none_for_unrecognised_or_non_string(self) -> None:
        for bad in ("nope", "", None, 5, []):
            self.assertIsNone(normalize_status(bad))


class AgentStateResultTest(unittest.TestCase):
    def test_observed_success(self) -> None:
        result = AgentStateResult.observed(RUNTIME_BUSY, raw_status="working")
        self.assertTrue(result.ok)
        self.assertEqual(result.state, RUNTIME_BUSY)
        self.assertIsNone(result.reason)
        self.assertEqual(result.raw_status, "working")

    def test_observed_unknown_is_a_success(self) -> None:
        # A read that ran but saw an unrecognised status is a successful
        # observation of ``unknown``, distinct from a mechanical failure.
        result = AgentStateResult.observed(RUNTIME_UNKNOWN, raw_status="frobnicate")
        self.assertTrue(result.ok)
        self.assertEqual(result.state, RUNTIME_UNKNOWN)

    def test_failure_degrades_to_unknown(self) -> None:
        result = AgentStateResult.failure(REASON_TRANSPORT_ERROR, "boom")
        self.assertFalse(result.ok)
        self.assertEqual(result.state, RUNTIME_UNKNOWN)
        self.assertEqual(result.reason, REASON_TRANSPORT_ERROR)

    def test_success_with_reason_rejected(self) -> None:
        with self.assertRaises(AgentStateError):
            AgentStateResult(ok=True, state=RUNTIME_BUSY, reason=REASON_TRANSPORT_ERROR)

    def test_failure_without_reason_rejected(self) -> None:
        with self.assertRaises(AgentStateError):
            AgentStateResult(ok=False, state=RUNTIME_UNKNOWN, reason=None)

    def test_failure_with_confident_state_rejected(self) -> None:
        # A failed read may never assert a confident state.
        with self.assertRaises(AgentStateError):
            AgentStateResult(ok=False, state=RUNTIME_BUSY, reason=REASON_TRANSPORT_ERROR)

    def test_unknown_state_token_rejected(self) -> None:
        with self.assertRaises(AgentStateError):
            AgentStateResult(ok=True, state="mystery")

    def test_failure_with_bad_reason_rejected(self) -> None:
        with self.assertRaises(AgentStateError):
            AgentStateResult(ok=False, state=RUNTIME_UNKNOWN, reason="not_a_reason")

    def test_error_is_a_transport_error(self) -> None:
        # One fail-closed error base for the whole terminal-runtime seam.
        self.assertTrue(issubclass(AgentStateError, TerminalTransportError))


class AgentStateListResultTest(unittest.TestCase):
    def test_observed_pairs(self) -> None:
        result = AgentStateListResult.observed(
            (("poc_claude", RUNTIME_BUSY), ("poc_codex", RUNTIME_AWAITING_INPUT))
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.states), 2)
        self.assertIsNone(result.reason)

    def test_failure_has_no_states(self) -> None:
        result = AgentStateListResult.failure(REASON_BINARY_NOT_FOUND, "gone")
        self.assertFalse(result.ok)
        self.assertEqual(result.states, ())
        self.assertEqual(result.reason, REASON_BINARY_NOT_FOUND)

    def test_bad_entry_rejected(self) -> None:
        with self.assertRaises(AgentStateError):
            AgentStateListResult(ok=True, states=(("a", "not_a_state"),))

    def test_malformed_entry_shape_rejected(self) -> None:
        with self.assertRaises(AgentStateError):
            AgentStateListResult(ok=True, states=(("only_one",),))  # type: ignore[arg-type]

    def test_failure_with_states_rejected(self) -> None:
        with self.assertRaises(AgentStateError):
            AgentStateListResult(
                ok=False, states=(("a", RUNTIME_BUSY),), reason=REASON_TRANSPORT_ERROR
            )


if __name__ == "__main__":
    unittest.main()
