"""Herdr queue-enter causal turn-start fallback (Redmine #15242).

The common handoff transport owns the one marker+body injection and the first Enter.
This module owns only the Herdr-specific observation and, when every live proof still
holds, one additional Enter.  It deliberately does not reuse the standard rail's
``drive_turn_start`` because queue-enter must continue to accept a busy receiver.

Safety invariants:

- the body is never in reach of a send-text primitive here;
- each Enter is preceded by an armed working-transition wait;
- an additional Enter is issued at most once and only after launch-generation,
  current-composer, startup-screen, and runtime-state checks;
- the public retry window is one absolute deadline across both waits and the interval;
- a busy receiver can be nudged, but busy-only evidence never confirms this delivery;
- telemetry remains on ``queue_enter_turn_start_observation`` so the delivery ledger
  continues to classify the outcome as the queue-enter rail.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    QueueEnterRetryPolicy,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    RUNTIME_AWAITING_INPUT,
    RUNTIME_BUSY,
    RUNTIME_TURN_ENDED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    DEFAULT_WAIT_TIMEOUT_MS,
    WAIT_ABSENT,
    WAIT_CHANGED,
    WAIT_ERROR,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_resend_gate import (
    RESEND_SKIP_BODY_ABSENT,
    RESEND_SKIP_BUDGET_EXHAUSTED,
    RESEND_SKIP_DISABLED,
    RESEND_SKIP_IDENTITY_DRIFT,
    RESEND_SKIP_IDENTITY_UNCONFIRMED,
    RESEND_SKIP_NONE,
    RESEND_SKIP_PANE_UNREADABLE,
    RESEND_SKIP_RECEIVER_BLOCKED,
    RESEND_SKIP_REASONS,
    RESEND_SKIP_STARTUP_SCREEN,
    RESEND_SKIP_STATE_NOT_INJECTABLE,
    RESEND_SKIP_STATE_UNREADABLE,
    RESEND_SKIP_WAIT_UNARMED,
    current_composer_retains_body,
    screen_guard_detects,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
)


@dataclass(frozen=True)
class QueueEnterResendGate:
    """One redaction-safe decision about the single additional Enter."""

    skip_reason: str = RESEND_SKIP_NONE
    runtime_state: Optional[str] = None

    def __post_init__(self) -> None:
        if self.skip_reason not in RESEND_SKIP_REASONS:
            raise ValueError(
                f"unknown queue-enter resend skip reason: {self.skip_reason!r}"
            )

    @property
    def allowed(self) -> bool:
        return self.skip_reason == RESEND_SKIP_NONE


class HerdrQueueEnterOps(Protocol):
    """Effects required by :class:`HerdrQueueEnterSession`."""

    def observe_queue_enter_runtime_state(self, target: str) -> Optional[str]: ...

    def observe_queue_enter_gateway_binding(self, target: str) -> Optional[dict]: ...

    def arm_queue_enter_turn_wait(self, target: str, *, timeout_ms: int): ...

    def collect_queue_enter_turn_wait(self, armed) -> Optional[str]: ...

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict],
    ) -> QueueEnterResendGate: ...

    def sleep(self, seconds: float) -> None: ...


def retry_values_are_finite(*values: object) -> bool:
    """Whether optional public retry scalars define finite numbers (total)."""
    try:
        return all(value is None or math.isfinite(float(value)) for value in values)
    except (TypeError, ValueError, OverflowError):
        return False


@dataclass
class HerdrQueueEnterSession:
    """One armed queue-enter attempt and its optional one-Enter fallback."""

    ops: HerdrQueueEnterOps
    target: str
    text: str
    receiver: str
    retry_policy: QueueEnterRetryPolicy
    monotonic: Callable[[], float]

    pre_binding: Optional[dict] = None
    baseline_state: Optional[str] = None
    armed_wait: Any = None
    retry_deadline: Optional[float] = None
    first_enter_at: Optional[float] = None
    first_wait_kind: Optional[str] = None
    final_wait_kind: Optional[str] = None
    causal_state: Optional[str] = None
    resend_skipped_reason: str = RESEND_SKIP_NONE
    enter_attempts: int = 1
    retry_engaged: bool = False

    @property
    def retry_enabled(self) -> bool:
        # Herdr CLI timeouts are integer milliseconds. Do not round a smaller
        # actuation budget up and silently widen it.
        return (
            self.retry_policy.window_seconds >= 0.001
            and self.retry_policy.interval_seconds >= 0.001
        )

    def _remaining_wait_ms(self) -> int:
        if self.retry_deadline is None:
            return DEFAULT_WAIT_TIMEOUT_MS
        remaining = self.retry_deadline - self.monotonic()
        if remaining < 0.001:
            return 0
        return min(DEFAULT_WAIT_TIMEOUT_MS, int(remaining * 1000.0))

    def _observe_binding(self) -> Optional[dict]:
        try:
            return self.ops.observe_queue_enter_gateway_binding(self.target)
        except Exception:  # noqa: BLE001 - missing identity withholds authority
            return None

    def _observe_state(self) -> Optional[str]:
        try:
            return self.ops.observe_queue_enter_runtime_state(self.target)
        except Exception:  # noqa: BLE001 - unreadable state cannot prove causality
            return None

    def _arm(self, timeout_ms: int):
        if timeout_ms <= 0:
            return None
        try:
            return self.ops.arm_queue_enter_turn_wait(
                self.target, timeout_ms=timeout_ms
            )
        except TypeError:
            # A staged test adapter may predate the timeout keyword. Production
            # implements it and therefore stays deadline-capped.
            try:
                return self.ops.arm_queue_enter_turn_wait(self.target)  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001 - unarmed is not send evidence
                return None
        except Exception:  # noqa: BLE001 - unarmed is not send evidence
            return None

    def _collect(self, armed) -> Optional[str]:
        if armed is None:
            return None
        try:
            kind = self.ops.collect_queue_enter_turn_wait(armed)
        except Exception:  # noqa: BLE001 - observer failure is not send evidence
            return None
        kind = str(kind or "").strip()
        return kind or None

    def arm_before_first_enter(self) -> None:
        """Capture the causal baseline and arm before the common rail presses Enter."""
        if self.retry_enabled:
            self.retry_deadline = (
                self.monotonic() + self.retry_policy.window_seconds
            )
        self.pre_binding = self._observe_binding()
        self.baseline_state = self._observe_state()
        self.causal_state = self.baseline_state
        self.armed_wait = self._arm(self._remaining_wait_ms())

    def note_first_enter_sent(self) -> None:
        self.first_enter_at = self.monotonic()

    def complete_after_first_enter(
        self, *, press_extra_enter: Callable[[], None]
    ) -> None:
        """Collect the first wait and, if safe, issue exactly one extra Enter."""
        self.first_wait_kind = self._collect(self.armed_wait) or WAIT_ERROR
        self.final_wait_kind = self.first_wait_kind

        binding_after_first = self._observe_binding()
        first_generation_coherent = (
            self.pre_binding is not None
            and binding_after_first is not None
            and self.pre_binding == binding_after_first
        )
        first_confirmed = (
            self.first_wait_kind == WAIT_CHANGED
            and self.baseline_state in (RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED)
            and first_generation_coherent
        )
        if first_confirmed or self.first_wait_kind == WAIT_ABSENT:
            return
        if not self.retry_enabled:
            self.resend_skipped_reason = RESEND_SKIP_DISABLED
            return
        if self.retry_deadline is None or self.first_enter_at is None:
            self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
            return

        delay = max(
            0.0,
            self.first_enter_at
            + self.retry_policy.interval_seconds
            - self.monotonic(),
        )
        remaining = self.retry_deadline - self.monotonic()
        if remaining < 0.001 or delay >= remaining:
            self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
            return
        if delay:
            self.ops.sleep(delay)

        try:
            gate = self.ops.evaluate_queue_enter_resend(
                self.target,
                self.text,
                self.receiver,
                self.pre_binding,
            )
        except TerminalTransportError:
            # This is not merely missing resend evidence: a bound transport
            # primitive failed after the body and first Enter were issued. Preserve
            # the common rail's typed ``blocked / transport_error`` containment
            # instead of reporting the uncertain partial delivery as a healthy send.
            raise
        except Exception:  # noqa: BLE001 - a failed proof refuses Enter
            gate = QueueEnterResendGate(RESEND_SKIP_STATE_UNREADABLE)
        if not isinstance(gate, QueueEnterResendGate):
            gate = QueueEnterResendGate(RESEND_SKIP_STATE_UNREADABLE)
        if not gate.allowed:
            self.resend_skipped_reason = gate.skip_reason
            return

        retry_wait_ms = self._remaining_wait_ms()
        if retry_wait_ms <= 0:
            self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
            return
        rearmed = self._arm(retry_wait_ms)
        if rearmed is None:
            self.resend_skipped_reason = RESEND_SKIP_WAIT_UNARMED
            return
        # Arming can consume the final milliseconds. Reap without actuation if
        # the absolute budget expired during that operation.
        if self._remaining_wait_ms() <= 0:
            self._collect(rearmed)
            self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
            return

        press_extra_enter()
        self.enter_attempts += 1
        self.retry_engaged = True
        self.causal_state = gate.runtime_state
        self.final_wait_kind = self._collect(rearmed) or WAIT_ERROR

    def observation(self, snapshot: object) -> dict:
        """Build queue-specific telemetry without creating a standard outcome."""
        post_binding = self._observe_binding()
        generation_coherent = (
            self.pre_binding is not None
            and post_binding is not None
            and self.pre_binding == post_binding
        )
        if snapshot is not None:
            telemetry = snapshot.to_telemetry_dict()  # type: ignore[attr-defined]
        else:
            telemetry = {
                "observation_kind": "post_choreography_snapshot",
                "source": "herdr_agent_get",
                "runtime_state": "unknown",
                "read_ok": False,
                "read_reason": "transport_error",
                "poll_attempts": 0,
            }
        extra = {
            "enter_attempts": self.enter_attempts,
            "first_event_wait_kind": self.first_wait_kind,
            "final_event_wait_kind": self.final_wait_kind,
            "resend_skipped_reason": self.resend_skipped_reason,
        }
        if self.baseline_state is not None:
            extra["baseline_runtime_state"] = self.baseline_state
        if generation_coherent:
            extra["gateway_binding"] = post_binding
            extra["observation_version"] = 2
            if (
                self.final_wait_kind == WAIT_CHANGED
                and self.causal_state
                in (RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED)
            ):
                extra["event_wait_kind"] = WAIT_CHANGED
        return {**telemetry, **extra}


class LiveHerdrQueueEnterOpsMixin:
    """Live queue effects mixed into the common transport adapter."""

    def observe_queue_enter_runtime_state(self, target: str) -> Optional[str]:
        from mozyo_bridge.application import commands as _commands

        rail = _commands.active_herdr_turn_start_rail
        if rail is None:
            return None
        try:
            result = rail.reader.read_agent_state(target)
        except Exception:  # noqa: BLE001 - unreadable cannot establish causality
            return None
        if not bool(getattr(result, "ok", False)):
            return None
        state = str(getattr(result, "state", "") or "").strip()
        return state or None

    def arm_queue_enter_turn_wait(self, target: str, *, timeout_ms: int):
        from mozyo_bridge.application import commands as _commands

        rail = _commands.active_herdr_turn_start_rail
        if rail is None:
            return None
        try:
            return rail.arm_turn_start_wait(target, timeout_ms=timeout_ms)
        except Exception:  # noqa: BLE001 - unarmed forbids the extra Enter
            return None

    def collect_queue_enter_turn_wait(self, armed) -> Optional[str]:
        if armed is None:
            return None
        try:
            kind = str(getattr(armed.collect(), "kind", "") or "").strip()
            return kind or None
        except Exception:  # noqa: BLE001 - observer failure is not evidence
            return None

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict],
    ) -> QueueEnterResendGate:
        from mozyo_bridge.application import commands as _commands
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (  # noqa: E501
            make_resend_screen_guard,
        )

        if baseline_binding is None:
            return QueueEnterResendGate(RESEND_SKIP_IDENTITY_UNCONFIRMED)
        current_binding = self.observe_queue_enter_gateway_binding(target)
        if current_binding is None:
            return QueueEnterResendGate(RESEND_SKIP_IDENTITY_UNCONFIRMED)
        if current_binding != baseline_binding:
            return QueueEnterResendGate(RESEND_SKIP_IDENTITY_DRIFT)

        rail = _commands.active_herdr_turn_start_rail
        if rail is None:
            return QueueEnterResendGate(RESEND_SKIP_STATE_UNREADABLE)
        try:
            content = rail.read_visible_pane(target)
        except TerminalTransportError:
            # The outer handoff rail owns the stable, redacted transport-failure
            # terminal. Do not collapse a real adapter failure into a benign gate
            # refusal after the first Enter has already been sent.
            raise
        except Exception:  # noqa: BLE001 - unreadable is never a clear composer
            return QueueEnterResendGate(RESEND_SKIP_PANE_UNREADABLE)
        if not isinstance(content, str) or not content.strip():
            return QueueEnterResendGate(RESEND_SKIP_PANE_UNREADABLE)
        guard = make_resend_screen_guard(receiver)
        if screen_guard_detects(guard, content):
            return QueueEnterResendGate(RESEND_SKIP_STARTUP_SCREEN)
        if not current_composer_retains_body(content, text):
            return QueueEnterResendGate(RESEND_SKIP_BODY_ABSENT)
        try:
            state_result = rail.reader.read_agent_state(target)
        except TerminalTransportError:
            raise
        except Exception:  # noqa: BLE001 - a failed state read refuses retry
            return QueueEnterResendGate(RESEND_SKIP_STATE_UNREADABLE)
        if not bool(getattr(state_result, "ok", False)):
            return QueueEnterResendGate(RESEND_SKIP_STATE_UNREADABLE)
        state = str(getattr(state_result, "state", "") or "").strip()
        if state == "blocked":
            return QueueEnterResendGate(RESEND_SKIP_RECEIVER_BLOCKED, state)
        if state not in (RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED, RUNTIME_BUSY):
            return QueueEnterResendGate(RESEND_SKIP_STATE_NOT_INJECTABLE, state)
        return QueueEnterResendGate(RESEND_SKIP_NONE, state)

    def observe_queue_enter_gateway_binding(self, target: str) -> Optional[dict]:
        """Return one attested, collision-free live launch-generation binding."""
        import os

        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
            VERDICT_PRESENT,
        )
        from mozyo_bridge.core.state.herdr_launch_generation import (
            verified_generation_token,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            AGENT_KEY_NAME,
            _agent_locator,
            _norm,
            _norm_lane,
            decode_assigned_name,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (  # noqa: E501
            HerdrCliAgentLister,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
            resolve_herdr_binary,
        )

        try:
            resolution = resolve_herdr_binary(os.environ)
            rows = HerdrCliAgentLister(resolution.path).list_agent_rows()
        except Exception:  # noqa: BLE001 - unreadable inventory => no binding
            return None
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and _agent_locator(row) == _norm(target)
        ]
        if len(matches) != 1:
            return None
        row = matches[0]
        name = _norm(row.get(AGENT_KEY_NAME))
        decoded = decode_assigned_name(name)
        if not decoded.ok or decoded.identity is None:
            return None
        identity = decoded.identity
        revision_raw = row.get("revision")
        row_revision = (
            _norm(str(revision_raw)) if not isinstance(revision_raw, bool) else ""
        )
        try:
            record = HerdrIdentityAttestationStore().read(name)
        except Exception:  # noqa: BLE001 - unreadable attestation => no binding
            return None
        if record is None:
            return None
        if not (
            _norm(getattr(record, "verdict", "")) == VERDICT_PRESENT
            and _norm(getattr(record, "workspace_id", ""))
            == _norm(identity.workspace_id)
            and _norm_lane(getattr(record, "lane_id", ""))
            == _norm_lane(identity.lane_id)
            and _norm(getattr(record, "role", "")) == _norm(identity.role)
            and _norm(getattr(record, "assigned_name", "")) == name
            and _norm(getattr(record, "locator", "")) == _norm(target)
        ):
            return None
        observed_at = _norm(str(getattr(record, "observed_at", "") or ""))
        startup_action_id = verified_generation_token(
            None,
            assigned_name=name,
            workspace_id=identity.workspace_id,
            role=identity.role,
            lane_id=identity.lane_id,
            locator=target,
            norm=_norm,
            norm_lane=_norm_lane,
        )
        if not observed_at or not startup_action_id:
            return None
        return {
            "provider": identity.role,
            "assigned_name": name,
            "locator": _norm(target),
            "row_revision": row_revision,
            "attestation_observed_at": observed_at,
            "startup_action_id": startup_action_id,
        }


__all__ = (
    "HerdrQueueEnterOps",
    "HerdrQueueEnterSession",
    "LiveHerdrQueueEnterOpsMixin",
    "QueueEnterResendGate",
    "retry_values_are_finite",
)
