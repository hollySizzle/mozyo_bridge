"""Herdr queue-enter causal turn-start fallback (Redmine #15242).

The common handoff transport owns the one marker+body injection and the first Enter.
This module owns only the Herdr-specific observation and, while every live proof still
holds, bounded Enter-only retries.  It deliberately does not reuse the standard rail's
``drive_turn_start`` because queue-enter must continue to accept a busy receiver.

Safety invariants:

- the body is never in reach of a send-text primitive here;
- each Enter is preceded by an armed working-transition wait;
- every additional Enter repeats launch-generation, current-composer,
  startup-screen, and runtime-state checks;
- the public retry window is one absolute deadline across all waits and intervals;
- timeout retries use the public policy cap; an observer error stops the
  sequence immediately without authorising another Enter;
- a busy receiver can be nudged, but busy-only evidence never confirms this delivery;
- telemetry remains on ``queue_enter_turn_start_observation`` so the delivery ledger
  continues to classify the outcome as the queue-enter rail.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Iterator, Optional, Protocol, cast

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    QUEUE_ENTER_RETRY_MAX_SECONDS,
    QueueEnterRetryPolicy,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    canonical_queue_enter_generation_binding,
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
    WAIT_TIMEOUT,
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

_STABLE_GENERATION_FIELDS = (
    "provider",
    "assigned_name",
    "locator",
    "terminal_id",
    "startup_action_id",
)

_ACTIVE_ENTER_EFFECT_FENCE: ContextVar[Optional[Callable[[], None]]] = ContextVar(
    "mozyo_queue_enter_effect_fence", default=None
)


class QueueEnterEffectFenceRefused(TerminalTransportError):
    """The queue's live wait/deadline proof expired before the Enter effect."""


@contextmanager
def queue_enter_effect_fence(check: Callable[[], None]) -> Iterator[None]:
    """Expose one queue Enter's last safety check to the actual send primitive."""
    token = _ACTIVE_ENTER_EFFECT_FENCE.set(check)
    try:
        yield
    finally:
        _ACTIVE_ENTER_EFFECT_FENCE.reset(token)


def enforce_active_queue_enter_effect_fence() -> None:
    """Run the active queue Enter fence, if this send belongs to that rail."""
    check = _ACTIVE_ENTER_EFFECT_FENCE.get()
    if check is not None:
        check()


def _same_terminal_generation(left: object, right: object) -> bool:
    """Compare stable v2 identity while allowing mutable terminal revision drift."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (
        canonical_queue_enter_generation_binding(left)
        and canonical_queue_enter_generation_binding(right)
        and all(left[field] == right[field] for field in _STABLE_GENERATION_FIELDS)
    )


@dataclass(frozen=True)
class QueueEnterResendGate:
    """One redaction-safe decision about the next additional Enter."""

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

    def observe_queue_enter_gateway_binding(
        self, target: str
    ) -> Optional[dict[str, str]]: ...

    def arm_queue_enter_turn_wait(
        self, target: str, *, timeout_ms: int
    ) -> Optional[object]: ...

    def collect_queue_enter_turn_wait(self, armed: object) -> Optional[str]: ...

    def cancel_queue_enter_turn_wait(self, armed: object) -> None: ...

    def queue_enter_turn_wait_pending(self, armed: object) -> bool: ...

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict[str, str]],
    ) -> QueueEnterResendGate: ...

    def sleep(self, seconds: float) -> None: ...


class QueueEnterSnapshot(Protocol):
    """Redaction-safe post-choreography snapshot accepted by the queue rail."""

    def to_telemetry_dict(self) -> dict[str, object]: ...


class _QueueEnterStateReader(Protocol):
    def read_agent_state(self, target: str) -> object: ...

    def read_agent_rows(self) -> Optional[Sequence[Mapping[str, object]]]: ...


class _ActiveHerdrQueueRail(Protocol):
    @property
    def reader(self) -> _QueueEnterStateReader: ...

    def arm_turn_start_wait(self, target: str, *, timeout_ms: int) -> object: ...

    def read_visible_pane(self, target: str) -> str: ...


def _active_queue_rail() -> Optional[_ActiveHerdrQueueRail]:
    """Narrow the dynamically installed command seam to this rail's live port."""
    from mozyo_bridge.application import commands as _commands

    raw: object = _commands.active_herdr_turn_start_rail
    if raw is None:
        return None
    try:
        reader = getattr(raw, "reader")
        methods = (
            getattr(reader, "read_agent_state"),
            getattr(raw, "arm_turn_start_wait"),
            getattr(raw, "read_visible_pane"),
        )
    except (AttributeError, TypeError):
        return None
    if not all(callable(method) for method in methods):
        return None
    return cast(_ActiveHerdrQueueRail, raw)


def retry_values_are_supported(*values: Optional[float]) -> bool:
    """Whether public retry scalars are finite and portably bounded."""
    return all(
        value is None
        or (math.isfinite(value) and value <= QUEUE_ENTER_RETRY_MAX_SECONDS)
        for value in values
    )


@dataclass
class HerdrQueueEnterSession:
    """One queue-enter send and its deadline-bounded Enter-only fallbacks."""

    ops: HerdrQueueEnterOps
    target: str
    text: str
    receiver: str
    expected_assigned_name: Optional[str]
    expected_process_generation: Optional[str]
    retry_policy: QueueEnterRetryPolicy
    monotonic: Callable[[], float]

    pre_binding: Optional[dict[str, str]] = None
    baseline_state: Optional[str] = None
    armed_wait: Optional[object] = None
    retry_deadline: Optional[float] = None
    first_enter_at: Optional[float] = None
    last_enter_at: Optional[float] = None
    first_wait_kind: Optional[str] = None
    final_wait_kind: Optional[str] = None
    causal_state: Optional[str] = None
    causal_baseline_state: Optional[str] = None
    causal_start_confirmed: bool = False
    resend_skipped_reason: str = RESEND_SKIP_NONE
    enter_attempts: int = 0
    retry_engaged: bool = False

    @property
    def retry_enabled(self) -> bool:
        # Herdr CLI timeouts are integer milliseconds. Do not round a smaller
        # actuation budget up and silently widen it.
        return (
            self.retry_policy.window_seconds >= 0.001
            and self.retry_policy.interval_seconds >= 0.001
            and self.retry_policy.enabled
        )

    @property
    def deadline_enabled(self) -> bool:
        return (
            math.isfinite(self.retry_policy.window_seconds)
            and 0.001 <= self.retry_policy.window_seconds
            <= QUEUE_ENTER_RETRY_MAX_SECONDS
        )

    def _remaining_wait_ms(self, *, interval_capped: bool = True) -> int:
        if self.retry_deadline is None:
            return DEFAULT_WAIT_TIMEOUT_MS
        remaining = self.retry_deadline - self.monotonic()
        if remaining < 0.001:
            return 0
        max_wait_seconds = DEFAULT_WAIT_TIMEOUT_MS / 1000.0
        caps = [DEFAULT_WAIT_TIMEOUT_MS, int(min(remaining, max_wait_seconds) * 1000.0)]
        if interval_capped:
            caps.append(
                int(min(self.retry_policy.interval_seconds, max_wait_seconds) * 1000.0)
            )
        return min(caps)

    def _observe_binding(self) -> Optional[dict[str, str]]:
        try:
            return self.ops.observe_queue_enter_gateway_binding(self.target)
        except Exception:  # noqa: BLE001 - missing identity withholds authority
            return None

    def _observe_state(self) -> Optional[str]:
        try:
            return self.ops.observe_queue_enter_runtime_state(self.target)
        except Exception:  # noqa: BLE001 - unreadable state cannot prove causality
            return None

    def _arm(self, timeout_ms: int) -> Optional[object]:
        if timeout_ms <= 0:
            return None
        try:
            return self.ops.arm_queue_enter_turn_wait(
                self.target, timeout_ms=timeout_ms
            )
        except Exception:  # noqa: BLE001 - unarmed is not send evidence
            return None

    def _collect(self, armed: Optional[object]) -> str:
        if armed is None:
            return WAIT_ERROR
        try:
            kind = self.ops.collect_queue_enter_turn_wait(armed)
        except Exception:  # noqa: BLE001 - observer failure is not send evidence
            return WAIT_ERROR
        kind = str(kind or "").strip()
        if kind not in (WAIT_CHANGED, WAIT_TIMEOUT, WAIT_ABSENT, WAIT_ERROR):
            return WAIT_ERROR
        return kind

    def _cancel(self, armed: Optional[object]) -> None:
        if armed is None:
            return
        cancel = getattr(self.ops, "cancel_queue_enter_turn_wait", None)
        if callable(cancel):
            try:
                cancel(armed)
                return
            except Exception:  # noqa: BLE001 - cleanup is best effort
                return
        # Compatibility for a narrow fake/adapter that predates the cancel seam.
        self._collect(armed)

    def _pending(self, armed: Optional[object]) -> bool:
        if armed is None:
            return False
        pending = getattr(self.ops, "queue_enter_turn_wait_pending", None)
        if not callable(pending):
            return False
        try:
            return bool(pending(armed))
        except Exception:  # noqa: BLE001 - liveness must be positively known
            return False

    def capture_before_body(self) -> bool:
        """Pin and validate the exact authorised target before body injection."""
        self.pre_binding = self._observe_binding()
        if not canonical_queue_enter_generation_binding(self.pre_binding):
            self.resend_skipped_reason = RESEND_SKIP_IDENTITY_UNCONFIRMED
            return False
        assert self.pre_binding is not None
        expected_name = str(self.expected_assigned_name or "").strip()
        expected_generation = str(
            self.expected_process_generation or ""
        ).strip()
        if not expected_name or not expected_generation:
            self.resend_skipped_reason = RESEND_SKIP_IDENTITY_UNCONFIRMED
            return False
        if (
            self.pre_binding["provider"] != self.receiver
            or self.pre_binding["assigned_name"] != expected_name
            or self.pre_binding["locator"] != self.target
            or self.pre_binding["process_generation"] != expected_generation
        ):
            self.resend_skipped_reason = RESEND_SKIP_IDENTITY_DRIFT
            return False
        return True

    def _binding_refusal(self) -> str:
        """Return the closed reason when the pinned generation is not current."""
        if self.pre_binding is None:
            return RESEND_SKIP_IDENTITY_UNCONFIRMED
        current_binding = self._observe_binding()
        if current_binding is None:
            return RESEND_SKIP_IDENTITY_UNCONFIRMED
        if not _same_terminal_generation(current_binding, self.pre_binding):
            return RESEND_SKIP_IDENTITY_DRIFT
        return RESEND_SKIP_NONE

    def _ready_for_immediate_enter(self, armed: Optional[object]) -> bool:
        """Make observer, deadline, and stable generation the final live checks."""
        reason = RESEND_SKIP_NONE
        if not self._pending(armed):
            reason = RESEND_SKIP_WAIT_UNARMED
        elif self.retry_deadline is not None and self.retry_deadline - self.monotonic() < 0.001:
            reason = RESEND_SKIP_BUDGET_EXHAUSTED
        else:
            reason = self._binding_refusal()
            if (
                reason == RESEND_SKIP_NONE
                and not self._pending(armed)
            ):
                reason = RESEND_SKIP_WAIT_UNARMED
            if (
                reason == RESEND_SKIP_NONE
                and self.retry_deadline is not None
                and self.retry_deadline - self.monotonic() < 0.001
            ):
                reason = RESEND_SKIP_BUDGET_EXHAUSTED
        if reason == RESEND_SKIP_NONE:
            return True
        self._cancel(armed)
        self.resend_skipped_reason = reason
        return False

    def _require_effect_boundary_ready(self, armed: Optional[object]) -> None:
        """Fail closed if the armed wait or budget expired inside adapter IO."""
        if self._ready_for_immediate_enter(armed):
            return
        raise QueueEnterEffectFenceRefused(
            "queue-enter safety evidence expired before the Enter send effect"
        )

    def _require_retry_effect_boundary_ready(
        self, armed: Optional[object]
    ) -> None:
        """Repeat the complete resend proof at the final retry effect boundary."""
        gate = self._evaluate_resend_gate()
        if not gate.allowed:
            self._cancel(armed)
            self.resend_skipped_reason = gate.skip_reason
            raise QueueEnterEffectFenceRefused(
                "queue-enter resend proof changed before the Enter send effect"
            )
        # Only the post-verifier state may be paired with this retry's event.
        self.causal_state = gate.runtime_state
        self._require_effect_boundary_ready(armed)

    @contextmanager
    def enter_effect_boundary(
        self, armed: Optional[object], *, retry: bool = False
    ) -> Iterator[None]:
        """Bind this Enter's final live fence around its transport call."""
        if retry:
            check = lambda: self._require_retry_effect_boundary_ready(armed)
        else:
            check = lambda: self._require_effect_boundary_ready(armed)
        with queue_enter_effect_fence(check):
            yield

    def arm_before_first_enter(self) -> bool:
        """Recheck generation, establish the deadline, and arm before first Enter."""
        binding_refusal = self._binding_refusal()
        if binding_refusal != RESEND_SKIP_NONE:
            self.resend_skipped_reason = binding_refusal
            return False
        if self.deadline_enabled:
            self.retry_deadline = (
                self.monotonic() + self.retry_policy.window_seconds
            )
        self.armed_wait = self._arm(
            self._remaining_wait_ms(interval_capped=self.retry_enabled)
        )
        if self.armed_wait is None:
            self.resend_skipped_reason = RESEND_SKIP_WAIT_UNARMED
            return False
        if (
            self.retry_deadline is not None
            and self.retry_deadline - self.monotonic() < 0.001
        ):
            # Arming may itself block until the absolute budget is gone. Reap
            # the observer, but never press the first Enter after the deadline.
            self._cancel(self.armed_wait)
            self.armed_wait = None
            self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
            return False
        # Snapshot after the wait is live: a transition that happened while
        # arming must not be mistaken for a consequence of the later Enter.
        self.baseline_state = self._observe_state()
        self.causal_state = self.baseline_state
        if (
            self.retry_deadline is not None
            and self.retry_deadline - self.monotonic() < 0.001
        ):
            self._cancel(self.armed_wait)
            self.armed_wait = None
            self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
            return False
        # Re-read the exact generation after every post-arm observation so a
        # target replaced during that operation never receives this Enter.
        binding_refusal = self._binding_refusal()
        if binding_refusal != RESEND_SKIP_NONE:
            self._cancel(self.armed_wait)
            self.armed_wait = None
            self.resend_skipped_reason = binding_refusal
            return False
        if (
            self.retry_deadline is not None
            and self.retry_deadline - self.monotonic() < 0.001
        ):
            self._cancel(self.armed_wait)
            self.armed_wait = None
            self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
            return False
        if not self._ready_for_immediate_enter(self.armed_wait):
            self.armed_wait = None
            return False
        return True

    def cancel_before_failed_enter(self) -> None:
        """Reap the first observer when the following Enter primitive fails."""
        self._cancel(self.armed_wait)
        self.armed_wait = None

    def note_first_enter_sent(self) -> None:
        sent_at = self.monotonic()
        self.first_enter_at = sent_at
        self.last_enter_at = sent_at
        self.enter_attempts = 1

    def _generation_coherent(self) -> bool:
        current = self._observe_binding()
        return (
            self.pre_binding is not None
            and current is not None
            and _same_terminal_generation(self.pre_binding, current)
        )

    def _causally_confirmed(self, wait_kind: str) -> bool:
        confirmed = (
            wait_kind == WAIT_CHANGED
            and self.causal_state in (RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED)
            and self._generation_coherent()
        )
        if confirmed:
            self.causal_start_confirmed = True
            self.causal_baseline_state = self.causal_state
        return confirmed

    def _evaluate_resend_gate(self) -> QueueEnterResendGate:
        try:
            gate = self.ops.evaluate_queue_enter_resend(
                self.target,
                self.text,
                self.receiver,
                self.pre_binding,
            )
        except TerminalTransportError:
            # A bound transport primitive failed after injection. Preserve the
            # common rail's typed ``blocked / transport_error`` terminal.
            raise
        except Exception:  # noqa: BLE001 - a failed proof refuses Enter
            return QueueEnterResendGate(RESEND_SKIP_STATE_UNREADABLE)
        if not isinstance(gate, QueueEnterResendGate):
            return QueueEnterResendGate(RESEND_SKIP_STATE_UNREADABLE)
        return gate

    def complete_after_first_enter(
        self, *, press_extra_enter: Callable[[], None]
    ) -> None:
        """Collect waits and issue only freshly-authorised, deadline-bounded Enters."""
        armed = self.armed_wait
        resends = 0
        while True:
            wait_kind = self._collect(armed)
            if self.first_wait_kind is None:
                self.first_wait_kind = wait_kind
            self.final_wait_kind = wait_kind

            if self._causally_confirmed(wait_kind) or wait_kind == WAIT_ABSENT:
                return
            if wait_kind == WAIT_ERROR:
                # A broken observer cannot authorise another Enter. A late
                # error cannot undo earlier timeout-authorised retries, so it
                # stops the sequence immediately and truthfully records the
                # error as the final wait kind.
                self.resend_skipped_reason = RESEND_SKIP_STATE_UNREADABLE
                return
            if not self.retry_enabled:
                self.resend_skipped_reason = RESEND_SKIP_DISABLED
                return
            if wait_kind != WAIT_TIMEOUT:
                # A changed event that cannot be attributed to this Enter is
                # not a timeout and therefore cannot authorise another Enter.
                # ``final_wait_kind`` plus the absent causal event telemetry
                # explains the unconfirmed terminal without inventing a gate
                # refusal that did not occur.
                return
            if self.retry_deadline is None or self.last_enter_at is None:
                self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
                return

            resend_cap = self.retry_policy.max_retries
            if resends >= resend_cap:
                self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
                return

            delay = max(
                0.0,
                self.last_enter_at
                + self.retry_policy.interval_seconds
                - self.monotonic(),
            )
            remaining = self.retry_deadline - self.monotonic()
            if remaining < 0.001 or delay >= remaining:
                self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
                return
            if delay:
                self.ops.sleep(delay)
            if self.retry_deadline - self.monotonic() < 0.001:
                self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
                return

            retry_wait_ms = self._remaining_wait_ms()
            if retry_wait_ms <= 0:
                self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
                return
            rearmed = self._arm(retry_wait_ms)
            if rearmed is None:
                self.resend_skipped_reason = RESEND_SKIP_WAIT_UNARMED
                return
            if self.retry_deadline - self.monotonic() < 0.001:
                self._cancel(rearmed)
                self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
                return
            # The safety gate is intentionally AFTER arm. Body retention,
            # modal/startup screen, runtime state, and identity can all change
            # while the wait process starts; stale pre-arm evidence cannot
            # authorise a keypress.
            try:
                gate = self._evaluate_resend_gate()
            except TerminalTransportError:
                self._cancel(rearmed)
                raise
            if not gate.allowed:
                self._cancel(rearmed)
                self.resend_skipped_reason = gate.skip_reason
                return
            if self.retry_deadline - self.monotonic() < 0.001:
                self._cancel(rearmed)
                self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
                return
            binding_refusal = self._binding_refusal()
            if binding_refusal != RESEND_SKIP_NONE:
                self._cancel(rearmed)
                self.resend_skipped_reason = binding_refusal
                return
            if self.retry_deadline - self.monotonic() < 0.001:
                self._cancel(rearmed)
                self.resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
                return
            if not self._ready_for_immediate_enter(rearmed):
                return

            try:
                with self.enter_effect_boundary(rearmed, retry=True):
                    press_extra_enter()
            except QueueEnterEffectFenceRefused:
                self._cancel(rearmed)
                return
            except TerminalTransportError:
                self._cancel(rearmed)
                raise
            self.enter_attempts += 1
            resends += 1
            self.retry_engaged = True
            self.last_enter_at = self.monotonic()
            armed = rearmed

    @property
    def failure_reason(self) -> str:
        """The existing wire reason that most precisely describes non-confirmation."""
        if self.final_wait_kind == WAIT_ABSENT:
            return "turn_start_absent"
        if self.resend_skipped_reason == RESEND_SKIP_RECEIVER_BLOCKED:
            return "receiver_blocked"
        return "turn_start_unconfirmed"

    def observation(
        self, snapshot: Optional[QueueEnterSnapshot]
    ) -> dict[str, Any]:
        """Build queue-specific telemetry without creating a standard outcome."""
        post_binding = self._observe_binding()
        generation_coherent = _same_terminal_generation(
            self.pre_binding, post_binding
        )
        if snapshot is not None:
            telemetry: dict[str, Any] = snapshot.to_telemetry_dict()
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
        causal_baseline = (
            self.causal_baseline_state
            if self.causal_start_confirmed
            else self.baseline_state
        )
        if causal_baseline is not None:
            extra["baseline_runtime_state"] = causal_baseline
        if generation_coherent:
            extra["gateway_binding"] = post_binding
            extra["observation_version"] = 2
            if (
                self.causal_start_confirmed
                and self.final_wait_kind == WAIT_CHANGED
            ):
                extra["event_wait_kind"] = WAIT_CHANGED
        return {**telemetry, **extra}


class LiveHerdrQueueEnterOpsMixin:
    """Live queue effects mixed into the common transport adapter."""

    def observe_queue_enter_runtime_state(self, target: str) -> Optional[str]:
        rail = _active_queue_rail()
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

    def arm_queue_enter_turn_wait(
        self, target: str, *, timeout_ms: int
    ) -> Optional[object]:
        rail = _active_queue_rail()
        if rail is None:
            return None
        try:
            armed: object = rail.arm_turn_start_wait(target, timeout_ms=timeout_ms)
            return armed
        except Exception:  # noqa: BLE001 - unarmed forbids the extra Enter
            return None

    def collect_queue_enter_turn_wait(self, armed: object) -> Optional[str]:
        collect = getattr(armed, "collect", None)
        if not callable(collect):
            return None
        try:
            kind = str(getattr(collect(), "kind", "") or "").strip()
            return kind or None
        except Exception:  # noqa: BLE001 - observer failure is not evidence
            return None

    def cancel_queue_enter_turn_wait(self, armed: object) -> None:
        cancel = getattr(armed, "cancel", None)
        if not callable(cancel):
            return
        try:
            cancel()
        except Exception:  # noqa: BLE001 - best-effort observer cleanup
            return

    def queue_enter_turn_wait_pending(self, armed: object) -> bool:
        pending = getattr(armed, "pending", None)
        if not callable(pending):
            return False
        try:
            return bool(pending())
        except Exception:  # noqa: BLE001 - unreadable liveness fails closed
            return False

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict[str, str]],
    ) -> QueueEnterResendGate:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_admission import (  # noqa: E501
            make_resend_screen_guard,
        )

        if baseline_binding is None:
            return QueueEnterResendGate(RESEND_SKIP_IDENTITY_UNCONFIRMED)
        current_binding = self.observe_queue_enter_gateway_binding(target)
        if current_binding is None:
            return QueueEnterResendGate(RESEND_SKIP_IDENTITY_UNCONFIRMED)
        if not _same_terminal_generation(current_binding, baseline_binding):
            return QueueEnterResendGate(RESEND_SKIP_IDENTITY_DRIFT)

        rail = _active_queue_rail()
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

    def observe_queue_enter_gateway_binding(
        self, target: str
    ) -> Optional[dict[str, str]]:
        """Return one attested, collision-free live launch-generation binding."""
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore,
            VERDICT_PRESENT,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
            verified_terminal_generation_token,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            AGENT_KEY_NAME,
            AGENT_KEY_REVISION,
            AGENT_KEY_TERMINAL_ID,
            _agent_locator,
            _norm,
            _norm_lane,
            decode_assigned_name,
            process_generation_of_locator,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
            SLOT_LIVE,
            classify_named_slot,
        )

        rail = _active_queue_rail()
        if rail is None:
            return None
        read_rows = getattr(rail.reader, "read_agent_rows", None)
        if not callable(read_rows):
            return None
        try:
            rows = read_rows()
        except Exception:  # noqa: BLE001 - unreadable inventory => no binding
            return None
        if rows is None:
            return None
        process_generation = process_generation_of_locator(target, rows)
        if process_generation is None:
            return None
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping) and _agent_locator(row) == _norm(target)
        ]
        if len(matches) != 1:
            return None
        row = matches[0]
        name = _norm(row.get(AGENT_KEY_NAME))
        decoded = decode_assigned_name(name)
        if not decoded.ok or decoded.identity is None:
            return None
        identity = decoded.identity
        if (
            classify_named_slot(row) != SLOT_LIVE
            or _norm(row.get("agent")) != _norm(identity.role)
        ):
            return None
        terminal_id = row.get(AGENT_KEY_TERMINAL_ID)
        revision_raw = row.get(AGENT_KEY_REVISION)
        # ``process_generation_of_locator`` already proved these exact JSON
        # types; keep the explicit narrowing local to the rendered binding.
        if type(terminal_id) is not str or type(revision_raw) is not int:
            return None
        row_revision = str(revision_raw)
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
        startup_action_id = verified_terminal_generation_token(
            None,
            assigned_name=name,
            workspace_id=identity.workspace_id,
            role=identity.role,
            lane_id=identity.lane_id,
            locator=target,
            terminal_id=terminal_id,
            norm=_norm,
            norm_lane=_norm_lane,
        )
        if not observed_at or not startup_action_id:
            return None
        return {
            "provider": identity.role,
            "assigned_name": name,
            "locator": _norm(target),
            "terminal_id": terminal_id,
            "row_revision": row_revision,
            "process_generation": process_generation,
            "attestation_observed_at": observed_at,
            "startup_action_id": startup_action_id,
        }


__all__ = (
    "HerdrQueueEnterOps",
    "HerdrQueueEnterSession",
    "LiveHerdrQueueEnterOpsMixin",
    "QueueEnterEffectFenceRefused",
    "QueueEnterSnapshot",
    "QueueEnterResendGate",
    "enforce_active_queue_enter_effect_fence",
    "queue_enter_effect_fence",
    "retry_values_are_supported",
)
