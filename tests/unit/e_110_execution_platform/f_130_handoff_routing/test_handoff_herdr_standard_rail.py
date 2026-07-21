"""Fake-port truth table for the herdr event-driven ``--mode standard`` rail (Redmine #13729 t3).

Exercises :class:`HerdrStandardRailUseCase` with a synthetic fake port + a fake rail — no live
herdr / tmux / Redmine — pinning the slice truth table:

- the rail's six closed outcomes project onto the handoff ``(status, reason)`` wire, and only a
  confirmed ``sent`` turn start persists the opt-in durable record and returns ``0``; every other
  outcome emits + ledgers then ``die``\\ s with **no C-u rollback and no re-send**;
- a missing rail (defensive) ``die``\\ s before driving anything;
- the side-effect ordering is emit -> ledger -> (persist | die);
- the resolved anchor (Redmine / Asana) + the ticketless / envelope context thread verbatim onto
  the emitted :class:`DeliveryOutcome`, and the q-enter submit + opt-in persistence scalars reach
  the submit-line / persistence calls.

The live composition (``run_herdr_standard_rail`` over ``LiveHerdrStandardRailOps``, driving the
real :class:`HerdrTurnStartRail` and the ``commands`` ledger / persistence / ``die`` seams) is
covered end-to-end by ``tests/integration/.../test_herdr_transport_wiring.py``.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import List, Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_herdr_standard_rail import (
    HerdrStandardRailOps,
    HerdrStandardRailRequest,
    HerdrStandardRailUseCase,
    TurnStartRailPort,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    AsanaAnchor,
    DeliveryOutcome,
    NormalizedAnchor,
    RedmineAnchor,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    OUTCOME_ABSENT,
    OUTCOME_BLOCKED,
    OUTCOME_DELIVERED_NOT_STARTED,
    OUTCOME_INJECT_FAILED,
    OUTCOME_PRECONDITION_NOT_IDLE,
    OUTCOME_STARTED,
    TurnStartResult,
)


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


@dataclass
class _PersistCall:
    outcome: DeliveryOutcome
    persist_delivery: bool
    duplicate_lane_panes: Optional[List[str]]
    record_format: str
    turn_start_lines: Optional[List[str]]


class _FakeOps:
    """A typed fake :class:`HerdrStandardRailOps` that records the side-effect calls in order."""

    def __init__(self) -> None:
        self.events: List[str] = []
        self.emitted: List[_EmitCall] = []
        self.ledgered: List[DeliveryOutcome] = []
        self.persisted: List[_PersistCall] = []
        self.died: List[str] = []

    def emit(
        self,
        outcome: DeliveryOutcome,
        *,
        record_format: str,
        command: Optional[str],
        duplicate_lane_panes: Optional[List[str]],
        role_profile_contract: Optional[str],
        submit_lines: Optional[List[str]],
        turn_start_lines: Optional[List[str]],
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
            )
        )

    def record_ledger(self, outcome: DeliveryOutcome) -> None:
        self.events.append("ledger")
        self.ledgered.append(outcome)

    def persist_delivery(
        self,
        outcome: DeliveryOutcome,
        *,
        persist_delivery: bool,
        duplicate_lane_panes: Optional[List[str]],
        record_format: str,
        turn_start_lines: Optional[List[str]],
    ) -> None:
        self.events.append("persist")
        self.persisted.append(
            _PersistCall(
                outcome=outcome,
                persist_delivery=persist_delivery,
                duplicate_lane_panes=duplicate_lane_panes,
                record_format=record_format,
                turn_start_lines=turn_start_lines,
            )
        )

    def die(self, message: str) -> None:
        self.events.append("die")
        self.died.append(message)
        raise _FakeDie(message)


class _FakeRail:
    """A fake :class:`TurnStartRailPort` returning a caller-chosen :class:`TurnStartResult`."""

    def __init__(self, result: TurnStartResult) -> None:
        self._result = result
        self.driven: List[tuple[str, str]] = []

    def drive_turn_start(self, target: str, text: str) -> TurnStartResult:
        self.driven.append((target, text))
        return self._result


# Structural-conformance gates (mypy island, review j#79040 F1' precedent): assigning the fakes
# to the port types makes any fake signature drift a STATIC error, not a silent runtime-only skip.
_PORT_CONFORMS: HerdrStandardRailOps = _FakeOps()
_RAIL_CONFORMS: TurnStartRailPort = _FakeRail(TurnStartResult(outcome=OUTCOME_STARTED))


def _request(
    *,
    anchor: Optional[NormalizedAnchor] = None,
    submit_intent: Optional[str] = None,
    submit_delivery_id: Optional[str] = None,
    persist_delivery: bool = False,
    duplicate_lane_panes: Optional[List[str]] = None,
) -> HerdrStandardRailRequest:
    """Build a request; the envelope value objects are ``None`` (the slice only threads them)."""
    return HerdrStandardRailRequest(
        target="%pT",
        marker="[[mk-1]]",
        body="hello body",
        receiver="claude",
        anchor=anchor,
        mode="standard",
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
    )


class HerdrStandardRailTruthTableTest(unittest.TestCase):
    def _run(
        self, outcome: str, request: HerdrStandardRailRequest
    ) -> tuple[_FakeOps, _FakeRail, Optional[int], Optional[_FakeDie]]:
        ops = _FakeOps()
        rail = _FakeRail(TurnStartResult(outcome=outcome))
        code: Optional[int] = None
        died: Optional[_FakeDie] = None
        try:
            code = HerdrStandardRailUseCase(ops).execute(rail, request)
        except _FakeDie as exc:
            died = exc
        return ops, rail, code, died

    # --- sent -------------------------------------------------------------------------------- #

    def test_started_projects_sent_ok_persists_and_returns_zero(self) -> None:
        ops, rail, code, died = self._run(OUTCOME_STARTED, _request(persist_delivery=True))
        self.assertIsNone(died)
        self.assertEqual(code, 0)
        # Confirmed turn start: emit -> ledger -> persist, then success. No die.
        self.assertEqual(ops.events, ["emit", "ledger", "persist"])
        self.assertEqual(ops.emitted[0].outcome.status, "sent")
        self.assertEqual(ops.emitted[0].outcome.reason, "ok")
        # The persistence call carries the opt-in flag + the same record format.
        self.assertTrue(ops.persisted[0].persist_delivery)
        self.assertEqual(ops.persisted[0].record_format, "both")
        # The rail was driven with exactly marker+body, once, against the target.
        self.assertEqual(rail.driven, [("%pT", "[[mk-1]] hello body")])
        # Structured turn-start telemetry rides the outcome (auditor replay, #13255 j#72695).
        self.assertIsInstance(ops.emitted[0].outcome.turn_start_outcome, dict)
        self.assertEqual(
            (ops.emitted[0].outcome.turn_start_outcome or {}).get("outcome"), "started"
        )

    # --- uncertain / blocked terminals ------------------------------------------------------ #

    def test_delivered_not_started_blocks_ledgers_and_dies_without_persist(self) -> None:
        ops, rail, code, died = self._run(
            OUTCOME_DELIVERED_NOT_STARTED, _request(persist_delivery=True)
        )
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        assert died is not None  # narrow for the type checker
        # Emitted + ledgered, then died. The opt-in persistence is NEVER reached on a non-sent.
        self.assertEqual(ops.events, ["emit", "ledger", "die"])
        self.assertEqual(ops.persisted, [])
        self.assertEqual(ops.emitted[0].outcome.status, "blocked")
        self.assertEqual(ops.emitted[0].outcome.reason, "turn_start_unconfirmed")
        # The rollback boundary is the whole point: the marker+body was typed once and only
        # Enter was sent — no C-u rollback and no blind re-send.
        self.assertIn("no C-u rollback, no", died.message)
        self.assertIn("typed at most once", died.message)
        self.assertIn("rail outcome delivered_not_started", died.message)
        self.assertIn("target=%pT", died.message)
        self.assertIn("marker=[[mk-1]]", died.message)

    def test_blocked_outcome_projects_receiver_blocked_and_dies(self) -> None:
        ops, _rail, code, died = self._run(OUTCOME_BLOCKED, _request())
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        self.assertEqual(ops.events, ["emit", "ledger", "die"])
        self.assertEqual(ops.emitted[0].outcome.reason, "receiver_blocked")
        self.assertEqual(ops.persisted, [])

    def test_absent_outcome_projects_turn_start_absent_and_dies(self) -> None:
        ops, _rail, code, died = self._run(OUTCOME_ABSENT, _request())
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        self.assertEqual(ops.emitted[0].outcome.reason, "turn_start_absent")
        self.assertEqual(ops.persisted, [])

    def test_precondition_not_idle_projects_reason_and_still_drove_the_rail(self) -> None:
        # The rail itself refused to inject (its own fail-close); from the slice's view the
        # drive was still invoked and returned the precondition_not_idle outcome.
        ops, rail, code, died = self._run(OUTCOME_PRECONDITION_NOT_IDLE, _request())
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        self.assertEqual(ops.emitted[0].outcome.reason, "precondition_not_idle")
        self.assertEqual(len(rail.driven), 1)
        self.assertEqual(ops.persisted, [])

    def test_inject_failed_projects_reason_and_dies(self) -> None:
        ops, _rail, code, died = self._run(OUTCOME_INJECT_FAILED, _request())
        self.assertIsNone(code)
        self.assertIsNotNone(died)
        self.assertEqual(ops.emitted[0].outcome.reason, "inject_failed")
        self.assertEqual(ops.persisted, [])

    # --- defensive: no rail installed ------------------------------------------------------- #

    def test_missing_rail_dies_before_driving_anything(self) -> None:
        ops = _FakeOps()
        died: Optional[_FakeDie] = None
        try:
            HerdrStandardRailUseCase(ops).execute(None, _request())
        except _FakeDie as exc:
            died = exc
        self.assertIsNotNone(died)
        assert died is not None
        # Nothing emitted / ledgered / persisted: the rail was never driven.
        self.assertEqual(ops.events, ["die"])
        self.assertEqual(ops.emitted, [])
        self.assertEqual(ops.ledgered, [])
        self.assertEqual(ops.persisted, [])
        self.assertIn("no turn-start rail was installed", died.message)
        self.assertIn("target=%pT", died.message)

    # --- context threading (Redmine / Asana, submit, duplicate lanes) ----------------------- #

    def test_redmine_anchor_threads_onto_the_outcome(self) -> None:
        ops, _rail, _code, _died = self._run(
            OUTCOME_STARTED, _request(anchor=RedmineAnchor(issue="9", journal="12"))
        )
        anchor = ops.emitted[0].outcome.anchor
        self.assertIsInstance(anchor, dict)
        self.assertEqual((anchor or {}).get("source"), "redmine")

    def test_asana_anchor_threads_onto_the_outcome(self) -> None:
        ops, _rail, _code, _died = self._run(
            OUTCOME_STARTED, _request(anchor=AsanaAnchor(task_id="T1", comment_id="C1"))
        )
        anchor = ops.emitted[0].outcome.anchor
        self.assertIsInstance(anchor, dict)
        self.assertEqual((anchor or {}).get("source"), "asana")

    def test_submit_intent_produces_submit_lines_only_when_set(self) -> None:
        with_intent, _rail, _code, _died = self._run(
            OUTCOME_STARTED,
            _request(submit_intent="submit_complete", submit_delivery_id="d-1"),
        )
        self.assertIsNotNone(with_intent.emitted[0].submit_lines)
        without_intent, _rail2, _code2, _died2 = self._run(OUTCOME_STARTED, _request())
        self.assertIsNone(without_intent.emitted[0].submit_lines)

    def test_duplicate_lane_panes_empty_is_none_on_emit_but_raw_on_persist(self) -> None:
        # Emit collapses an empty duplicate-lane list to None (diagnostic only); persistence
        # receives the raw list, exactly as the original inline block threaded them.
        ops, _rail, _code, _died = self._run(
            OUTCOME_STARTED, _request(persist_delivery=True, duplicate_lane_panes=[])
        )
        self.assertIsNone(ops.emitted[0].duplicate_lane_panes)
        self.assertEqual(ops.persisted[0].duplicate_lane_panes, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
