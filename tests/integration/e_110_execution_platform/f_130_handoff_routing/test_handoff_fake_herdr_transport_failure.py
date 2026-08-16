"""Fake-Herdr transport-failure composition over the real shim + rail (Redmine #14232).

Issue acceptance 5: *reproduce ``send_text`` timeout / ``send_keys`` timeout / wait timeout /
adapter exception deterministically with a fake Herdr.* The recurrence pins in
``tests/regressions/test_issue_14232_handoff_partial_delivery_outcome.py`` drive the rail
through a **fake port object**, which pins the rail's own guard but replaces the very seam the
defect lived across. This module closes that gap: it composes the **real**
``resolve_runtime_transport_binding`` herdr shim (whose ``_require_ok`` / ``capture_pane`` raise
``TransportBindingError``) with the **real** ``TmuxTransportRailUseCase``, injecting only a fake
:class:`TerminalTransportPort` at the outermost boundary — the same seam the module's own
docstring names as the test seam ("tests inject ``port`` + ``list_agents`` +
``resolve_assigned_name``").

So the chain under test is the production one: *fake herdr primitive fails / times out ->
shim raises -> rail closes to a typed ``blocked`` / ``transport_error`` outcome with the shared
injection-stage classification*. That is the whole path the issue's live evidence traversed
(``herdr send_keys(enter) failed (reason=transport_error): herdr command timed out``), and the
part a rail-only fake cannot demonstrate.

No live herdr binary, no tmux, no subprocess: the fake port returns structured failures (or
raises, for the adapter-exception case), so every case is deterministic.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import List, Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_tmux_transport_rail import (
    TmuxTransportRailRequest,
    TmuxTransportRailUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_herdr_queue_enter_rail import (
    QueueEnterResendGate,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    DeliveryOutcome,
    QueueEnterRetryOutcome,
    RedmineAnchor,
    TargetActivationOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    REASON_TRANSPORT_ERROR,
    STAGE_UNCERTAIN_PARTIAL,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.transport_binding import (
    resolve_runtime_transport_binding,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    PaneReadResult,
    REASON_TRANSPORT_ERROR as TRANSPORT_REASON_TRANSPORT_ERROR,
    TerminalTransportConfig,
    TerminalTransportError,
    TransportResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_resend_gate import (
    RESEND_SKIP_BODY_ABSENT,
)

#: A herdr-valid live locator, so the shim's translator passes the target through unchanged and
#: the test exercises the primitive mapping rather than target translation.
_TARGET = "w4B:p4V"

#: The fake herdr timeout detail. Asserted ABSENT from the typed outcome / die text: the adapter
#: message can name a binary path or carry a raw backend status, and a delivery record is
#: pasteable into a durable journal.
_TIMEOUT_DETAIL = "herdr command timed out after 20s running /opt/private/bin/herdr"


class _FakeDie(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class _FakeHerdrPort:
    """A deterministic fake :class:`TerminalTransportPort`.

    ``fail_on`` names the primitive that fails (``send_text`` / ``send_keys_enter`` /
    ``read_pane``); ``raise_instead`` makes it *raise* an adapter exception rather than return a
    structured failure, covering the fourth case in acceptance 5 (an adapter exception, not a
    reported failure).
    """

    fail_on: str
    raise_instead: bool = False
    backend: str = BACKEND_HERDR
    calls: List[tuple] = field(default_factory=list)

    def _fail(self, primitive: str):
        if self.raise_instead:
            raise TerminalTransportError(f"{primitive}: {_TIMEOUT_DETAIL}")
        return TransportResult.failure(
            TRANSPORT_REASON_TRANSPORT_ERROR, _TIMEOUT_DETAIL
        )

    def send_text(self, target: str, text: str) -> TransportResult:
        self.calls.append(("send_text", target))
        if self.fail_on == "send_text":
            return self._fail("send_text")
        return TransportResult.success()

    def send_keys(self, target: str, keys: str) -> TransportResult:
        self.calls.append(("send_keys", target, keys))
        if self.fail_on == "send_keys_enter" and keys == "enter":
            return self._fail("send_keys(enter)")
        return TransportResult.success()

    def read_pane(
        self, target: str, *, source: str = "visible", lines: Optional[int] = None
    ) -> PaneReadResult:
        self.calls.append(("read_pane", target))
        if self.fail_on == "read_pane":
            if self.raise_instead:
                raise TerminalTransportError(f"read_pane: {_TIMEOUT_DETAIL}")
            return PaneReadResult.failure(
                TRANSPORT_REASON_TRANSPORT_ERROR, _TIMEOUT_DETAIL
            )
        return PaneReadResult.success("")


@dataclass
class _ShimBackedOps:
    """A rail port whose transport effects route through the REAL herdr shim binding.

    Only the record / ledger / die effects are faked; ``inject_body`` / ``wait_for_marker`` /
    ``capture`` / ``press_enter`` / ``rollback`` issue the exact tmux argv shapes the live
    ``LiveTmuxTransportRailOps`` issues, against the shim the production wiring installs.
    """

    run_tmux: object
    capture_pane: object

    emitted: List[DeliveryOutcome] = field(default_factory=list)
    persisted: List[DeliveryOutcome] = field(default_factory=list)
    ledgered: List[tuple] = field(default_factory=list)
    died: List[str] = field(default_factory=list)
    guidance: List[str] = field(default_factory=list)
    enter_presses: int = 0
    rollbacks: int = 0
    injections: int = 0
    queue_wait_kinds: List[str] = field(default_factory=lambda: ["changed"])
    live_waits: set[object] = field(default_factory=set)

    def inject_body(self, target: str, text: str) -> None:
        self.injections += 1
        self.run_tmux("send-keys", "-t", target, "-l", "--", text)

    def wait_for_marker(
        self, target: str, marker: str, lines: int, timeout: float
    ) -> bool:
        # Mirrors ``commands.wait_for_text``: it polls via ``capture_pane``, so a failed
        # herdr ``read_pane`` surfaces here exactly as it does in production.
        from mozyo_bridge.application.session_bootstrap_command import marker_visible_in

        return marker_visible_in(self.capture_pane(target, lines), marker)

    def capture(self, target: str, lines: int) -> str:
        return self.capture_pane(target, lines)

    def rollback(self, target: str) -> None:
        self.rollbacks += 1
        self.run_tmux("send-keys", "-t", target, "C-u")

    def press_enter(self, target: str) -> None:
        self.enter_presses += 1
        self.run_tmux("send-keys", "-t", target, "Enter")

    def sleep(self, seconds: float) -> None:
        pass

    def observe_standard_turn_start(self, target: str, **kwargs):
        raise AssertionError("the queue-enter rail must not observe a standard turn start")

    def observe_queue_enter_turn_start(self, target: str):
        return None

    def observe_queue_enter_runtime_state(self, target: str) -> str:
        return "turn_ended"

    def observe_queue_enter_gateway_binding(self, target: str) -> dict:
        assigned_name = "mzb1_ws_codex_lane"
        terminal_id = "terminal-test"
        revision = "1"
        return {
            "provider": "codex",
            "assigned_name": assigned_name,
            "locator": target,
            "terminal_id": terminal_id,
            "row_revision": revision,
            "process_generation": (
                f"{len(assigned_name)}:{assigned_name}:"
                f"{len(terminal_id)}:{terminal_id}:"
                f"{len(target)}:{target}:r{revision}"
            ),
            "attestation_observed_at": "2026-08-10T00:00:00+00:00",
            "startup_action_id": "startup-test",
        }

    def arm_queue_enter_turn_wait(self, target: str, *, timeout_ms: int):
        armed = object()
        self.live_waits.add(armed)
        return armed

    def collect_queue_enter_turn_wait(self, armed) -> str:
        self.live_waits.discard(armed)
        return self.queue_wait_kinds.pop(0) if self.queue_wait_kinds else "changed"

    def cancel_queue_enter_turn_wait(self, armed) -> None:
        self.live_waits.discard(armed)

    def queue_enter_turn_wait_pending(self, armed) -> bool:
        return armed in self.live_waits

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict],
    ) -> QueueEnterResendGate:
        # Exercise the real Herdr shim's pane read from the current #15242
        # strict gate, not the retired marker-only retry probe.
        self.capture(target, 200)
        return QueueEnterResendGate(RESEND_SKIP_BODY_ABSENT)

    def emit(self, outcome: DeliveryOutcome, **kwargs) -> None:
        self.emitted.append(outcome)

    def persist(self, outcome: DeliveryOutcome, **kwargs) -> None:
        self.persisted.append(outcome)

    def record_ledger(
        self,
        outcome: DeliveryOutcome,
        *,
        retry_outcome: Optional[QueueEnterRetryOutcome],
        backend: Optional[str] = None,
        rail: Optional[str] = None,
        disposition: Optional[str] = None,
    ) -> None:
        self.ledgered.append(
            (outcome, retry_outcome, backend, rail, disposition)
        )

    def restore_previous_active(
        self,
        activation: Optional[TargetActivationOutcome],
        *,
        restore_previous_active: bool,
    ) -> Optional[TargetActivationOutcome]:
        return activation

    def emit_marker_timeout_guidance(self, receiver: str) -> None:
        self.guidance.append(receiver)

    def die(self, message: str) -> None:
        self.died.append(message)
        raise _FakeDie(message)


def _request(**overrides) -> TmuxTransportRailRequest:
    base = dict(
        target=_TARGET,
        marker="[mozyo:handoff:source=redmine:issue=14232:journal=94407:kind=reply:to=codex]",
        body="reply ready for codex.",
        receiver="codex",
        anchor=RedmineAnchor(issue="14232", journal="94407"),
        mode="queue-enter",
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
        submit_intent="reply",
        submit_delivery_id="qe-0123456789abcdef",
        persist_delivery=False,
        herdr_send=True,
        herdr_assigned_name="mzb1_ws_codex_lane",
        herdr_process_generation=(
            f"{len('mzb1_ws_codex_lane')}:mzb1_ws_codex_lane:"
            f"{len('terminal-test')}:terminal-test:"
            f"{len(_TARGET)}:{_TARGET}:r1"
        ),
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


def _ops_over_fake_herdr(port: _FakeHerdrPort) -> _ShimBackedOps:
    """Build the rail port over the REAL herdr shim binding for ``port``."""

    def _unexpected_tmux(*args, **kwargs):  # pragma: no cover - a herdr send must not use tmux
        raise AssertionError(f"the herdr binding must not fall back to tmux: {args!r}")

    binding = resolve_runtime_transport_binding(
        TerminalTransportConfig(backend=BACKEND_HERDR),
        tmux_run_tmux=_unexpected_tmux,
        tmux_capture_pane=_unexpected_tmux,
        port=port,
        # A herdr-valid target passes the translator through, so neither collaborator is
        # reached; they are supplied so a regression that starts translating is visible.
        resolve_assigned_name=lambda target: (_ for _ in ()).throw(
            AssertionError("a herdr-valid locator must not be translated")
        ),
        list_agents=lambda: [],
    )
    assert binding.backend == BACKEND_HERDR
    return _ShimBackedOps(run_tmux=binding.run_tmux, capture_pane=binding.capture_pane)


class FakeHerdrTransportFailureClosesToTypedOutcomeTest(unittest.TestCase):
    """Each acceptance-5 failure mode closes to one typed, secret-safe outcome."""

    def _drive(
        self,
        port: _FakeHerdrPort,
        *,
        queue_wait_kinds: Optional[List[str]] = None,
        **request_overrides,
    ) -> _ShimBackedOps:
        ops = _ops_over_fake_herdr(port)
        if queue_wait_kinds is not None:
            ops.queue_wait_kinds = list(queue_wait_kinds)
        with self.assertRaises(_FakeDie):
            TmuxTransportRailUseCase(ops).execute(_request(**request_overrides))
        return ops

    def _assert_typed_and_redacted(self, ops: _ShimBackedOps) -> DeliveryOutcome:
        self.assertEqual(len(ops.emitted), 1)
        outcome = ops.emitted[0]
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.reason, REASON_TRANSPORT_ERROR)
        self.assertEqual(outcome.injection_stage["stage"], STAGE_UNCERTAIN_PARTIAL)
        self.assertTrue(outcome.injection_stage["blind_retry_prohibited"])
        self.assertEqual(outcome.next_action_owner, "sender")
        # The adapter's own message never reaches the durable surfaces.
        self.assertNotIn(_TIMEOUT_DETAIL, outcome.next_action)
        self.assertNotIn(_TIMEOUT_DETAIL, ops.died[0])
        self.assertNotIn("/opt/private/bin/herdr", outcome.to_json())
        self.assertEqual(len(ops.ledgered), 1)
        self.assertEqual(ops.ledgered[0][0], outcome)
        self.assertEqual(ops.ledgered[0][2:4], ("herdr", "queue_enter_rail"))
        self.assertEqual(
            ops.ledgered[0][4], outcome.transport_failure["primitive"]
        )
        return outcome

    def test_send_text_timeout(self):
        ops = self._drive(_FakeHerdrPort(fail_on="send_text"))
        self._assert_typed_and_redacted(ops)
        self.assertEqual(ops.enter_presses, 0, "no Enter after a failed body injection")

    def test_send_keys_enter_timeout(self):
        """The issue's live evidence: the body landed, then ``send_keys(enter)`` timed out."""
        ops = self._drive(_FakeHerdrPort(fail_on="send_keys_enter"))
        self._assert_typed_and_redacted(ops)
        self.assertEqual(ops.injections, 1, "the marker+body is typed exactly once")
        self.assertEqual(ops.rollbacks, 0, "no C-u rollback on an uncertain delivery")

    def test_herdr_queue_enter_skips_the_tmux_landing_read(self):
        port = _FakeHerdrPort(fail_on="read_pane")
        ops = _ops_over_fake_herdr(port)
        rc = TmuxTransportRailUseCase(ops).execute(_request())
        self.assertEqual(rc, 0)
        self.assertNotIn(("read_pane", _TARGET), port.calls)
        self.assertEqual(ops.enter_presses, 1)

    def test_adapter_exception_rather_than_a_reported_failure(self):
        """A primitive that *raises* (not one that reports ``ok=False``) is also contained."""
        for primitive in ("send_text", "send_keys_enter"):
            with self.subTest(primitive=primitive):
                ops = self._drive(
                    _FakeHerdrPort(fail_on=primitive, raise_instead=True)
                )
                self._assert_typed_and_redacted(ops)

    def test_enter_only_retry_probe_read_timeout(self):
        """The queue-enter strict resend gate's pane read fails after the first Enter."""

        @dataclass
        class _LateReadFailure(_FakeHerdrPort):
            reads: int = 0

            def read_pane(self, target, *, source="visible", lines=None):
                self.reads += 1
                # Herdr does not perform a landing read.  Its first pane read is
                # the strict resend gate after the initial Enter.
                return PaneReadResult.failure(
                    TRANSPORT_REASON_TRANSPORT_ERROR, _TIMEOUT_DETAIL
                )

        ops = self._drive(
            _LateReadFailure(fail_on="never"),
            queue_wait_kinds=["timeout"],
            queue_enter_retry_window=4.0,
            queue_enter_retry_interval=2.0,
        )
        self._assert_typed_and_redacted(ops)
        self.assertEqual(
            ops.enter_presses, 1, "the first Enter was issued before the probe failed"
        )
        self.assertEqual(ops.injections, 1, "the marker+body is never re-injected")

    def test_a_healthy_fake_herdr_send_still_reaches_a_sent_outcome(self):
        """The guard must not swallow the success path: an all-ok fake port delivers.

        Without this cell the four failure cells above would also pass if the rail had been
        broken into always blocking, which is the classic vacuous-guard failure.
        """
        port = _FakeHerdrPort(fail_on="never")
        ops = _ops_over_fake_herdr(port)
        # The marker is unobserved, but the pre-armed event fires on the same
        # generation, which is stronger causal evidence than rendered landing.
        rc = TmuxTransportRailUseCase(ops).execute(_request())
        self.assertEqual(rc, 0)
        self.assertEqual(ops.died, [])
        self.assertEqual(len(ops.emitted), 1)
        self.assertEqual(ops.emitted[0].status, "sent")
        self.assertEqual(ops.emitted[0].reason, "ok")
        self.assertEqual(ops.enter_presses, 1)


if __name__ == "__main__":  # pragma: no cover - manual runner parity
    unittest.main()
