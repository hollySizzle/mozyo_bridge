"""Recurrence pins for Redmine #14232 handoff partial-delivery typed outcome.

Every test here detects the return of one of the **five** defects this issue fixed: the three
residual defects #14232 j#84877 recorded against the daily-default handoff rails (1-3, each
reproduced non-passing on the lane base ``a83587a3``), plus the ones each same-lane review
found in the preceding fix: j#95333 -> 4-5 (non-passing on ``0426e915``), j#95601 -> 6
(non-passing on ``3c1f724d``), j#95827 -> 7 (non-passing on ``3322a343``).

None of them asserts a general module contract. The injection-stage vocabulary's own contract
— including the guards that the 4-5 fixes are not *over*-corrections, which are green on both
heads — lives in
``tests/unit/e_110_execution_platform/f_130_handoff_routing/test_handoff_injection_stage.py``,
because the tests-placement policy's R3-b is a file-unit rule.

The seven symptoms, and why each is a #14232 defect rather than a design preference:

1. **A transport exception escaped the high-level handoff boundary.** Under
   ``terminal_transport.backend: herdr`` the shim
   (``transport_binding._HerdrTmuxShim``) raises ``TransportBindingError`` when a mapped
   primitive fails — e.g. ``herdr send_keys(enter) failed (reason=transport_error): herdr
   command timed out``, the live evidence in the issue description. The common tmux
   transport rail (``TmuxTransportRailUseCase`` — the ``queue-enter`` default and
   ``pending``) called ``inject_body`` / ``wait_for_marker`` / ``press_enter`` / ``capture``
   / ``rollback`` with no guard, so that exception propagated out of ``orchestrate_handoff``
   and the CLI exited 1 with a stack trace and **no** structured ``status`` / ``reason`` /
   ``next_action``. Only the herdr event-driven ``--mode standard`` rail closed to a typed
   outcome (j#84870), which is why the improvement recorded there did not cover the default
   rail.

2. **The q-enter front door claimed delivery before the transport ran.**
   ``cmd_handoff_q_enter`` emitted ``SubmitOutcome(dispatched=True, blocked=False)`` and
   *then* called ``orchestrate_handoff``, so a subsequent ``blocked`` /
   ``turn_start_unconfirmed`` never corrected the front-door record (j#84877 required
   correction 1).

3. **A post-injection outcome was classified as "nothing was typed".**
   ``classify_composer_residue`` folded every non-``marker_timeout`` ``blocked`` onto
   ``not_typed``, so the herdr ``delivered_not_started`` projection (``blocked`` /
   ``turn_start_unconfirmed`` — body **and** Enter injected, then an event-wait timeout) read
   as "not typed at all" (j#84877 required correction 2), and the callback / outbox retry
   authority disagreed with the handoff positive-delivery gate on ``sent`` / ``queue_enter``
   (j#84877 required correction 3 / acceptance 4).

4. **A marker-observed ``queue-enter`` send was reported as a confirmed submission.** The
   first fix removed the *reason*-level optimism (``sent``/``queue_enter``) but kept a
   *rail*-level one: on ``queue-enter`` a landed marker resolves to ``sent``/``ok``, and that
   rail runs **no turn-start gate** — ``ok`` there means only "the marker landed and Enter was
   pressed". Classifying from ``(status, reason)`` alone therefore claimed
   ``submitted_confirmed`` / ``dispatched=true`` even when the outcome's own snapshot said
   ``awaiting_input`` ("delivered, but a turn start was not observed") or ``turn_ended`` — the
   exact post snapshot j#84870 recorded as the residual defect.

5. **A ``blocked`` front-door terminal still exited 0.** j#94407's acceptance names *front
   door / delivery record / exit code / callback retry authority* as the four surfaces that
   must converge on one classification; the first fix converged three. A record reading
   ``blocked=True dispatched=False`` alongside shell success is a false success for every
   automated caller. (The same fix narrowed a defect the first fix introduced: it derived
   ``blocked`` from ``not delivered``, which swept an explicitly requested ``--mode pending``
   park in with the failures.)

6. **The confirmation rested on a non-causal signal.** The fix for 4 required "positive
   turn-start evidence" and read the post-choreography ``runtime_state`` poll — which that
   field's own source contract forbids being read as an event-observed turn start. The
   queue-enter rail runs no idle precondition gate, so an already-busy receiver reads ``busy``
   identically, reintroducing the false confirmation in a new cell; symmetrically it dropped
   the rail's real causal signal, so a fast turn read as unconfirmed.

7. **The generation binding was accepted on truthiness alone.** The fix for 6 required a
   ``gateway_binding`` alongside the fired wait but never checked its shape, so a string, a
   list, an int, a partial dict, an empty required field, or a legacy/versionless record all
   promoted to confirmed — the inference "these fields are present, therefore the record came
   from the coherence gate" assumed what it needed to establish.
"""
from __future__ import annotations

import argparse
import unittest
from dataclasses import dataclass, field
from typing import List, Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_tmux_transport_rail import (
    TmuxTransportRailRequest,
    TmuxTransportRailUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    DeliveryOutcome,
    QueueEnterRetryOutcome,
    RedmineAnchor,
    TargetActivationOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    STAGE_NOT_SENT,
    STAGE_SUBMITTED_CONFIRMED,
    STAGE_UNCERTAIN_PARTIAL,
    injection_stage_for,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.delivery_outcome_gate import (
    delivery_was_positive,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.q_enter import (
    RESIDUE_NOT_TYPED,
    classify_composer_residue,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
    TransportBindingError,
)

_MODE_QUEUE_ENTER = "queue-enter"
_MODE_STANDARD = "standard"

#: Sentinel: "do not put a `gateway_binding` key on the observation at all", distinct from
#: putting one whose value is `None` / empty (the rail can do neither, but a reader must fail
#: closed on both).
_MISSING = object()

#: A generation-coherent gateway binding, in the shape the rail persists when the pre-arm and
#: post-collect generations match.
_GATEWAY_BINDING = {
    "provider": "codex", "assigned_name": "mzb1_ws_codex_lane", "locator": "w4B:p4T",
    "row_revision": "1", "attestation_observed_at": "2026-07-29T20:10:01+00:00",
    "startup_action_id": "startup-abc",
}


class _FakeDie(Exception):
    """Stand-in for ``commands.die``: raises so the rail's control flow terminates."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class _RaisingOps:
    """A rail port whose one named transport step raises a herdr ``TransportBindingError``.

    ``raise_on`` names the single step that fails (``inject`` / ``wait`` / ``enter`` /
    ``capture``); every other step succeeds so the failure is isolated to that step. This is
    the fake shape of the live evidence: the herdr shim's ``_require_ok`` /
    ``capture_pane`` raise ``TransportBindingError`` when a mapped primitive times out.
    """

    raise_on: str
    marker_observed: bool = True
    captures: List[str] = field(default_factory=list)

    events: List[str] = field(default_factory=list)
    emitted: List[DeliveryOutcome] = field(default_factory=list)
    persisted: List[DeliveryOutcome] = field(default_factory=list)
    ledgered: List[DeliveryOutcome] = field(default_factory=list)
    enter_presses: int = 0
    injected: List[tuple] = field(default_factory=list)
    rollbacks: int = 0
    guidance: List[str] = field(default_factory=list)
    died: List[str] = field(default_factory=list)

    def _boom(self, primitive: str) -> None:
        raise TransportBindingError(
            f"herdr {primitive} failed (reason=transport_error): herdr command timed out"
        )

    def inject_body(self, target: str, text: str) -> None:
        self.events.append("inject")
        if self.raise_on == "inject":
            self._boom("send_text")
        self.injected.append((target, text))

    def wait_for_marker(
        self, target: str, marker: str, lines: int, timeout: float
    ) -> bool:
        self.events.append("wait")
        if self.raise_on == "wait":
            self._boom("read_pane")
        return self.marker_observed

    def capture(self, target: str, lines: int) -> str:
        self.events.append("capture")
        if self.raise_on == "capture":
            self._boom("read_pane")
        return self.captures.pop(0) if self.captures else ""

    def rollback(self, target: str) -> None:
        self.events.append("rollback")
        self.rollbacks += 1

    def press_enter(self, target: str) -> None:
        self.events.append("enter")
        if self.raise_on == "enter":
            self._boom("send_keys(enter)")
        self.enter_presses += 1

    def sleep(self, seconds: float) -> None:
        self.events.append("sleep")

    def observe_standard_turn_start(
        self, target: str, *, baseline_capture: str, window_seconds: float, lines: int
    ):
        raise AssertionError("no standard turn-start observation is expected here")

    def observe_queue_enter_turn_start(self, target: str):
        return None

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
        self.emitted.append(outcome)

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
        self.persisted.append(outcome)

    def record_ledger(
        self, outcome: DeliveryOutcome, *, retry_outcome: Optional[QueueEnterRetryOutcome]
    ) -> None:
        self.events.append("ledger")
        self.ledgered.append(outcome)

    def restore_previous_active(
        self,
        activation: Optional[TargetActivationOutcome],
        *,
        restore_previous_active: bool,
    ) -> Optional[TargetActivationOutcome]:
        return activation

    def emit_marker_timeout_guidance(self, receiver: str) -> None:
        self.events.append("guidance")
        self.guidance.append(receiver)

    def die(self, message: str) -> None:
        self.events.append("die")
        self.died.append(message)
        raise _FakeDie(message)


def _request(mode: str = _MODE_QUEUE_ENTER, **overrides) -> TmuxTransportRailRequest:
    base = dict(
        target="%7",
        marker="[mozyo:handoff:source=redmine:issue=14232:journal=94407:kind=reply:to=codex]",
        body="reply ready for codex.",
        receiver="codex",
        anchor=RedmineAnchor(issue="14232", journal="94407"),
        mode=mode,
        kind="reply",
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
        duplicate_lane_panes=[],
        submit_intent=None,
        submit_delivery_id=None,
        persist_delivery=False,
        herdr_send=True,
        read_lines=50,
        landing_timeout=8.0,
        submit_delay=None,
        queue_enter_retry_window=0.0,
        queue_enter_retry_interval=0.0,
        target_activation=None,
        restore_previous_active=False,
    )
    base.update(overrides)
    return TmuxTransportRailRequest(**base)


class HerdrTransportExceptionClosesToTypedOutcomeTest(unittest.TestCase):
    """Defect 1: a herdr transport exception escaped the high-level handoff boundary.

    On the lane base every case below propagated ``TransportBindingError`` out of
    ``TmuxTransportRailUseCase.execute`` — no ``emit``, no ``die``, so the CLI printed a
    stack trace and exited 1 with no structured outcome (the issue's live evidence).
    """

    def _assert_typed_transport_block(self, ops: _RaisingOps) -> DeliveryOutcome:
        self.assertEqual(
            len(ops.emitted), 1, "exactly one terminal outcome must be emitted"
        )
        outcome = ops.emitted[0]
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.reason, "transport_error")
        self.assertTrue(ops.died, "the send must terminate through die(), not a traceback")
        # Secret-safe: the typed outcome carries fixed tokens, never the adapter's raw text.
        self.assertNotIn("herdr command timed out", ops.died[0])
        return outcome

    def test_send_keys_enter_timeout_closes_to_typed_uncertain_partial(self):
        """The exact live evidence: ``send_keys(enter)`` timed out after the body landed."""
        ops = _RaisingOps(raise_on="enter")
        with self.assertRaises(_FakeDie):
            TmuxTransportRailUseCase(ops).execute(_request())
        outcome = self._assert_typed_transport_block(ops)
        # Body typed, Enter's fate unknown -> a blind retry can duplicate.
        self.assertEqual(
            outcome.injection_stage["stage"], STAGE_UNCERTAIN_PARTIAL
        )
        self.assertTrue(outcome.injection_stage["blind_retry_prohibited"])

    def test_send_text_timeout_closes_to_typed_outcome(self):
        """``send_text`` (the single body injection) timed out."""
        ops = _RaisingOps(raise_on="inject")
        with self.assertRaises(_FakeDie):
            TmuxTransportRailUseCase(ops).execute(_request())
        outcome = self._assert_typed_transport_block(ops)
        self.assertEqual(outcome.injection_stage["stage"], STAGE_UNCERTAIN_PARTIAL)
        self.assertEqual(ops.enter_presses, 0, "no Enter after a failed injection")

    def test_landing_wait_read_timeout_closes_to_typed_outcome(self):
        """The landing-marker wait's ``read_pane`` timed out (turn-start read/wait)."""
        ops = _RaisingOps(raise_on="wait")
        with self.assertRaises(_FakeDie):
            TmuxTransportRailUseCase(ops).execute(_request())
        self._assert_typed_transport_block(ops)
        self.assertEqual(ops.enter_presses, 0)

    def test_enter_only_retry_probe_read_timeout_closes_to_typed_outcome(self):
        """The queue-enter Enter-only retry's marker probe (``capture``) timed out."""
        ops = _RaisingOps(raise_on="capture", marker_observed=False)
        with self.assertRaises(_FakeDie):
            TmuxTransportRailUseCase(ops).execute(
                _request(queue_enter_retry_window=4.0, queue_enter_retry_interval=2.0)
            )
        self._assert_typed_transport_block(ops)

    def test_transport_exception_never_rolls_back_or_resends(self):
        """A transport exception must not add a C-u rollback or a second injection.

        The no-blind-retry / marker+body-typed-once boundary (issue Non-goals) holds on the
        exception path too: the rail must not "repair" an unknown partial delivery.
        """
        ops = _RaisingOps(raise_on="enter")
        with self.assertRaises(_FakeDie):
            TmuxTransportRailUseCase(ops).execute(_request())
        self.assertEqual(ops.rollbacks, 0)
        self.assertEqual(len(ops.injected), 1)


class QEnterFrontDoorDerivesDispatchedFromTransportTest(unittest.TestCase):
    """Defect 2: the front door emitted ``dispatched=true`` before the transport ran.

    On the lane base ``cmd_handoff_q_enter`` emitted its ``SubmitOutcome`` *before*
    ``orchestrate_handoff``, so a transport block never corrected it: the recorded front-door
    result read ``dispatched`` even when the delivery was ``blocked`` /
    ``turn_start_unconfirmed`` (j#84877 required correction 1).
    """

    def _run(self, delivery_status: str, delivery_reason: str, rc: int = 0):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import (
            cli_handoff_q_enter as mod,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            make_outcome,
        )

        emitted: List[object] = []
        order: List[str] = []

        def _fake_emit(outcome, *, record_format):
            order.append("front_door")
            emitted.append(outcome)

        def _fake_orchestrate(args, **kwargs):
            order.append("transport")
            args.delivery_outcome = make_outcome(
                status=delivery_status,
                reason=delivery_reason,
                receiver="claude",
                target="%7",
                anchor=RedmineAnchor(issue="14232", journal="94407"),
                mode=_MODE_STANDARD,
                kind="implementation_request",
                notification_marker="[m]",
            )
            return rc

        args = argparse.Namespace(
            intent="worker_dispatch",
            source="redmine",
            issue="14232",
            journal="94407",
            task_id=None,
            comment_id=None,
            anchor_url=None,
            kind="implementation_request",
            to="claude",
            classification=None,
            record_format="both",
        )
        original_emit = mod._emit_submit_outcome
        original_orchestrate = mod.orchestrate_handoff
        mod._emit_submit_outcome = _fake_emit
        mod.orchestrate_handoff = _fake_orchestrate
        try:
            code = mod.cmd_handoff_q_enter(args)
        finally:
            mod._emit_submit_outcome = original_emit
            mod.orchestrate_handoff = original_orchestrate
        return code, emitted, order

    def test_front_door_record_is_emitted_after_the_transport(self):
        _code, emitted, order = self._run("sent", "ok")
        self.assertEqual(
            order,
            ["transport", "front_door"],
            "the front-door result must be derived after the transport outcome is known",
        )
        self.assertEqual(len(emitted), 1)

    def test_blocked_transport_does_not_leave_a_dispatched_front_door_record(self):
        _code, emitted, _order = self._run("blocked", "turn_start_unconfirmed")
        outcome = emitted[0]
        self.assertFalse(
            outcome.dispatched,
            "a blocked transport must not be recorded as a dispatched front door",
        )
        self.assertTrue(outcome.resolved, "the rail was still resolved (planning succeeded)")
        self.assertEqual(outcome.injection_stage, STAGE_UNCERTAIN_PARTIAL)
        self.assertTrue(outcome.blind_retry_prohibited)

    def test_marker_unobserved_queue_enter_is_not_reported_as_dispatched(self):
        """``sent`` / ``queue_enter`` never pre-confirmed landing, so it is not a delivery."""
        _code, emitted, _order = self._run("sent", "queue_enter")
        outcome = emitted[0]
        self.assertFalse(outcome.dispatched)
        self.assertEqual(outcome.injection_stage, STAGE_UNCERTAIN_PARTIAL)

    def test_confirmed_delivery_is_reported_as_dispatched(self):
        """The guard is not vacuous: a confirmed transport still reports ``dispatched``.

        Without this cell every assertion above would also hold if the derivation had been
        broken into always reporting "not dispatched".
        """
        _code, emitted, _order = self._run("sent", "ok")
        outcome = emitted[0]
        self.assertTrue(outcome.dispatched)
        self.assertFalse(outcome.blocked)
        self.assertEqual(outcome.injection_stage, STAGE_SUBMITTED_CONFIRMED)


class PostInjectionOutcomeIsNotClassifiedAsNotTypedTest(unittest.TestCase):
    """Defect 3: post-injection outcomes were classified as "nothing was typed".

    On the lane base ``classify_composer_residue("blocked", "turn_start_unconfirmed")``
    returned ``not_typed``, and the callback retry authority
    (``send_outcome_for_delivery``) disagreed with the handoff positive-delivery gate
    (``delivery_was_positive``) on ``sent`` / ``queue_enter``.
    """

    def test_herdr_delivered_not_started_is_not_classified_not_typed(self):
        """j#84877: ``delivered_not_started`` injected body **and** Enter before the timeout."""
        self.assertNotEqual(
            classify_composer_residue("blocked", "turn_start_unconfirmed"),
            RESIDUE_NOT_TYPED,
        )

    def test_transport_error_residue_is_not_classified_not_typed(self):
        self.assertNotEqual(
            classify_composer_residue("blocked", "transport_error"), RESIDUE_NOT_TYPED
        )

    def test_callback_retry_authority_reuses_the_handoff_injection_stage(self):
        """Acceptance 4: one classification, so an unknown partial is never auto-resent."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
            SEND_DELIVERED,
            SEND_NOT_SENT,
            SEND_UNCERTAIN,
            send_outcome_for_delivery,
        )

        expected = {
            STAGE_SUBMITTED_CONFIRMED: SEND_DELIVERED,
            STAGE_NOT_SENT: SEND_NOT_SENT,
            STAGE_UNCERTAIN_PARTIAL: SEND_UNCERTAIN,
        }
        for status, reason in (
            ("sent", "ok"),
            ("sent", "queue_enter"),
            ("pending_input", "ok"),
            ("blocked", "transport_error"),
            ("blocked", "turn_start_unconfirmed"),
            ("blocked", "marker_timeout"),
            ("blocked", "inject_failed"),
            ("blocked", "invalid_args"),
            ("blocked", "precondition_not_idle"),
            ("blocked", "execution_root_outside_target_repo"),
            ("blocked", "reader_upgrade_required"),
        ):
            with self.subTest(status=status, reason=reason):
                self.assertEqual(
                    send_outcome_for_delivery(status, reason),
                    expected[injection_stage_for(status, reason)],
                )

    def test_queue_enter_is_not_optimistically_reported_delivered(self):
        """The issue's Non-goal: no optimistic delivered-ization of an unconfirmed landing.

        ``delivery_was_positive`` has always refused ``sent`` / ``queue_enter``; the callback
        retry authority reported it ``delivered`` and closed the row. Converged, both refuse.
        """
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.delivery_outcome_gate import (
            delivery_was_positive,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
            SEND_DELIVERED,
            send_outcome_for_delivery,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            make_outcome,
        )

        args = argparse.Namespace()
        args.delivery_outcome = make_outcome(
            status="sent",
            reason="queue_enter",
            receiver="codex",
            target="%7",
            anchor=RedmineAnchor(issue="14232", journal="94407"),
            mode=_MODE_QUEUE_ENTER,
            kind="reply",
            notification_marker="[m]",
        )
        self.assertFalse(delivery_was_positive(args))
        self.assertNotEqual(
            send_outcome_for_delivery("sent", "queue_enter"), SEND_DELIVERED
        )


class MarkerObservedQueueEnterIsNotAConfirmedSubmissionTest(unittest.TestCase):
    """Defect 4 (review j#95333 finding 1): a second optimistic delivered-ization.

    R1 fixed the *reason*-level optimism (`sent`/`queue_enter`) but left the *rail*-level one:
    a ``queue-enter`` send whose landing marker WAS observed reports ``sent``/``ok``, and R1
    read that as ``submitted_confirmed`` / ``dispatched=true``. But the queue-enter rail runs
    no turn-start gate at all — ``ok`` there means only "the marker landed and Enter was
    pressed". Measured on `0426e915`, all four runtime snapshots (including ``awaiting_input``,
    which the observation module documents as *"delivered, but a turn start was not
    observed"*, and ``turn_ended``, the exact post snapshot j#84870 recorded as the residual
    defect) produced ``stage=submitted_confirmed dispatched=True delivery_was_positive=True``.
    """

    def _built(
        self, runtime_state=None, *, mode=_MODE_QUEUE_ENTER,
        event_wait_kind=None, binding=_MISSING,
    ):
        observation = None
        if runtime_state is not None:
            observation = {
                "observation_kind": "post_choreography_snapshot",
                "source": "herdr_agent_get",
                "runtime_state": runtime_state,
                "read_ok": True,
                "read_reason": None,
                "poll_attempts": 3,
            }
            extra = {}
            if event_wait_kind is not None:
                extra["event_wait_kind"] = event_wait_kind
            if binding is not _MISSING:
                extra["gateway_binding"] = binding
            if extra:
                extra["observation_version"] = 2
                observation.update(extra)
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            make_outcome,
        )

        return make_outcome(
            status="sent", reason="ok", receiver="codex", target="w4B:p4T",
            anchor=RedmineAnchor(issue="14232", journal="94508"), mode=mode,
            kind="review_request", notification_marker="[m]", source="redmine",
            queue_enter_turn_start_observation=observation,
        )

    def _causal(self, runtime_state="busy"):
        return self._built(
            runtime_state, event_wait_kind="changed", binding=_GATEWAY_BINDING
        )

    def test_a_receiver_that_never_started_a_turn_is_not_confirmed(self):
        for runtime_state in ("awaiting_input", "turn_ended"):
            with self.subTest(runtime_state=runtime_state):
                outcome = self._built(runtime_state)  # no causal signal at all
                self.assertEqual(
                    outcome.injection_stage["stage"], STAGE_UNCERTAIN_PARTIAL
                )
                args = argparse.Namespace()
                args.delivery_outcome = outcome
                self.assertFalse(delivery_was_positive(args))

    def test_the_front_door_does_not_report_it_dispatched(self):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.q_enter import (
            RAIL_ANCHORED_SEND,
            SubmitOutcome,
        )

        front = SubmitOutcome.from_transport(
            self._built("awaiting_input"),
            plan_intent="worker_dispatch", rail=RAIL_ANCHORED_SEND,
            anchor_required=True, ticketless=False, delivery_id="qe-x",
        )
        self.assertFalse(front.dispatched)
        self.assertEqual(front.injection_stage, STAGE_UNCERTAIN_PARTIAL)

    def test_the_composer_residue_is_not_reported_cleared(self):
        """The same misreading on the residue axis: an absorbed Enter leaves the body typed."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.q_enter import (
            RESIDUE_CLEARED,
        )

        outcome = self._built("awaiting_input")
        self.assertNotEqual(
            classify_composer_residue(
                outcome.status, outcome.reason, mode=outcome.mode,
                queue_enter_turn_start_observation=(
                    outcome.queue_enter_turn_start_observation
                ),
            ),
            RESIDUE_CLEARED,
        )


class NonCausalSnapshotIsNotAConfirmationTest(unittest.TestCase):
    """Defect 6 (review j#95601): the confirmation rested on a non-causal signal.

    R2 fixed defect 4 by requiring "positive turn-start evidence" for a marker-observed
    ``queue-enter`` send — but chose the wrong signal. It read the post-choreography
    ``runtime_state`` poll, whose own source contract
    (``DeliveryOutcome.queue_enter_turn_start_observation``) says a post-hoc snapshot *"does
    not prove causality the way an armed ``wait agent-status`` transition does, so it must not
    be read as an event-observed turn start"*. The queue-enter rail runs no idle precondition
    gate, so a receiver that was already busy before the send — or a recycled process running
    someone else's turn — reads ``busy`` identically, which reintroduced the very false
    confirmation defect 4 had just removed. Symmetrically it dropped the rail's *real* causal
    signal, so a fast turn that had already finished by snapshot time read as unconfirmed.

    Measured on ``3c1f724d``: ``busy`` + ``event_wait_kind=timeout`` and ``busy`` with no event
    at all both returned ``submitted_confirmed``; ``turn_ended``/``awaiting_input`` WITH a
    coherent ``changed`` wait both returned ``uncertain_partial``.
    """

    _built = MarkerObservedQueueEnterIsNotAConfirmedSubmissionTest._built
    _causal = MarkerObservedQueueEnterIsNotAConfirmedSubmissionTest._causal

    def test_a_busy_snapshot_without_a_fired_wait_is_not_confirmed(self):
        """(a) busy + timeout / absent event -> uncertain."""
        for event_wait_kind in (None, "timeout", "absent"):
            with self.subTest(event_wait_kind=event_wait_kind):
                outcome = self._built(
                    "busy", event_wait_kind=event_wait_kind,
                    binding=_GATEWAY_BINDING if event_wait_kind else _MISSING,
                )
                self.assertEqual(
                    outcome.injection_stage["stage"], STAGE_UNCERTAIN_PARTIAL
                )

    def test_a_fired_wait_without_a_coherent_binding_is_not_confirmed(self):
        """(b) busy + missing / incoherent binding -> uncertain.

        The rail writes ``event_wait_kind`` and ``gateway_binding`` together, and drops BOTH
        when the pre-arm and post-collect generations disagree. A record carrying one without
        the other did not come from that gate.
        """
        for binding in (_MISSING, None, {}):
            with self.subTest(binding=binding):
                outcome = self._built("busy", event_wait_kind="changed", binding=binding)
                self.assertEqual(
                    outcome.injection_stage["stage"], STAGE_UNCERTAIN_PARTIAL
                )

    def test_a_fast_turn_that_already_finished_is_still_confirmed(self):
        """(c) changed + coherent binding + final `turn_ended` -> confirmed.

        The armed wait was set up BEFORE this send's Enter, so the transition it observed
        belongs to this send; the later poll finding the turn already over does not retract it.
        """
        outcome = self._causal("turn_ended")
        self.assertEqual(
            outcome.injection_stage["stage"], STAGE_SUBMITTED_CONFIRMED
        )
        args = argparse.Namespace()
        args.delivery_outcome = outcome
        self.assertTrue(delivery_was_positive(args))

    def test_the_front_door_reports_the_causal_confirmation(self):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.q_enter import (
            RAIL_ANCHORED_SEND,
            SubmitOutcome,
        )

        front = SubmitOutcome.from_transport(
            self._causal("turn_ended"),
            plan_intent="worker_dispatch", rail=RAIL_ANCHORED_SEND,
            anchor_required=True, ticketless=False, delivery_id="qe-x",
        )
        self.assertTrue(front.dispatched)
        self.assertFalse(front.blocked)


class MalformedGenerationBindingIsNotCausalEvidenceTest(unittest.TestCase):
    """Defect 7 (review j#95827): the binding was accepted on truthiness alone.

    R3 required a ``gateway_binding`` alongside the fired wait, reasoning that the rail writes
    the two together only under a coherent generation — so their presence *is* the coherence
    guarantee. But it checked only that the value was truthy, which makes the inference circular:
    it assumes the record came from that producer instead of establishing it. #14203 j#87418 had
    already ruled that the mere non-emptiness of a binding field is not a generation authority,
    and R3's own docstring promised an "unrecognised shape" would fail closed.

    Measured on ``3322a343``: a string, a list, an int, a partial dict, a dict with an empty
    required field, a non-``str`` field, and a full dict on a legacy or absent
    ``observation_version`` ALL returned ``submitted_confirmed``. R3's regression covered only
    ``_MISSING`` / ``None`` / ``{}`` — the three falsy shapes — so every truthy malformation
    walked straight through into ``dispatched``, ``delivery_was_positive`` and callback
    completion at once.
    """

    def _observation(self, binding, *, version=2):
        observation = {
            "observation_kind": "post_choreography_snapshot",
            "source": "herdr_agent_get", "runtime_state": "busy", "read_ok": True,
            "read_reason": None, "poll_attempts": 3, "event_wait_kind": "changed",
        }
        if binding is not _MISSING:
            observation["gateway_binding"] = binding
        if version is not None:
            observation["observation_version"] = version
        return observation

    def _stage(self, binding, *, version=2):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            make_outcome,
        )

        outcome = make_outcome(
            status="sent", reason="ok", receiver="codex", target="w4B:p4T",
            anchor=RedmineAnchor(issue="14232", journal="95816"), mode=_MODE_QUEUE_ENTER,
            kind="review_request", notification_marker="[m]", source="redmine",
            queue_enter_turn_start_observation=self._observation(binding, version=version),
        )
        return outcome.injection_stage["stage"]

    def test_a_non_mapping_binding_is_not_evidence(self):
        for binding in ("not-a-binding", ["not-a-binding"], 1, 3.5, True):
            with self.subTest(binding=binding):
                self.assertEqual(self._stage(binding), STAGE_UNCERTAIN_PARTIAL)

    def test_a_partial_binding_is_not_evidence(self):
        """Every canonical generation field must be present — a subset proves nothing."""
        for drop in (
            "provider", "assigned_name", "locator", "row_revision",
            "attestation_observed_at", "startup_action_id",
        ):
            with self.subTest(missing=drop):
                partial = {k: v for k, v in _GATEWAY_BINDING.items() if k != drop}
                self.assertEqual(self._stage(partial), STAGE_UNCERTAIN_PARTIAL)

    def test_an_empty_required_field_is_not_evidence(self):
        """The producer guarantees these non-empty; an empty one did not come from it."""
        for field in (
            "provider", "assigned_name", "locator",
            "attestation_observed_at", "startup_action_id",
        ):
            with self.subTest(empty=field):
                self.assertEqual(
                    self._stage({**_GATEWAY_BINDING, field: ""}), STAGE_UNCERTAIN_PARTIAL
                )

    def test_a_non_string_field_is_not_evidence(self):
        for value in (1, None, [], {}):
            with self.subTest(value=value):
                self.assertEqual(
                    self._stage({**_GATEWAY_BINDING, "row_revision": value}),
                    STAGE_UNCERTAIN_PARTIAL,
                )

    def test_a_legacy_or_versionless_observation_is_not_evidence(self):
        """The rail stamps ``observation_version=2`` exactly when it publishes these fields."""
        for version in (None, 1, "2", 3):
            with self.subTest(observation_version=version):
                self.assertEqual(
                    self._stage(_GATEWAY_BINDING, version=version), STAGE_UNCERTAIN_PARTIAL
                )


class FrontDoorBlockedTerminalDoesNotExitZeroTest(unittest.TestCase):
    """Defect 5 (review j#95333 finding 2): the exit code never joined the convergence.

    j#94407's acceptance names *front door / delivery record / exit code / callback retry
    authority* as the four surfaces that must converge on one classification. R1 converged
    three: measured on `0426e915`, a front-door record reading ``blocked=True
    dispatched=False stage=uncertain_partial`` still exited **0**, so every automated caller
    saw shell success for an unconfirmed delivery.
    """

    def _run(self, status, reason, *, mode=_MODE_QUEUE_ENTER, rail_rc=0):
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import (
            cli_handoff_q_enter as mod,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
            make_outcome,
        )

        emitted: List[object] = []

        def _fake_orchestrate(args, **kwargs):
            args.delivery_outcome = make_outcome(
                status=status, reason=reason, receiver="claude", target="%7",
                anchor=RedmineAnchor(issue="14232", journal="94508"), mode=mode,
                kind="implementation_request", notification_marker="[m]", source="redmine",
            )
            return rail_rc

        original_emit, original_orchestrate = (
            mod._emit_submit_outcome, mod.orchestrate_handoff
        )
        mod._emit_submit_outcome = lambda o, *, record_format: emitted.append(o)
        mod.orchestrate_handoff = _fake_orchestrate
        try:
            rc = mod.cmd_handoff_q_enter(argparse.Namespace(
                intent="worker_dispatch", source="redmine", issue="14232",
                journal="94508", task_id=None, comment_id=None, anchor_url=None,
                kind="implementation_request", to="claude", classification=None,
                record_format="both",
            ))
        finally:
            mod._emit_submit_outcome = original_emit
            mod.orchestrate_handoff = original_orchestrate
        return rc, emitted[0]

    def test_an_unconfirmed_queue_enter_delivery_does_not_exit_zero(self):
        for reason in ("ok", "queue_enter"):
            with self.subTest(reason=reason):
                rc, front = self._run("sent", reason)
                self.assertTrue(front.blocked)
                self.assertNotEqual(
                    rc, 0, "a blocked front-door terminal must not report shell success"
                )

    def test_a_deliberate_pending_park_still_exits_zero(self):
        """`--mode pending` asked the rail not to submit; that is not a blocked terminal."""
        rc, front = self._run("pending_input", "ok", mode="pending")
        self.assertFalse(front.blocked)
        self.assertFalse(front.dispatched)
        self.assertEqual(rc, 0)


if __name__ == "__main__":  # pragma: no cover - manual runner parity
    unittest.main()
