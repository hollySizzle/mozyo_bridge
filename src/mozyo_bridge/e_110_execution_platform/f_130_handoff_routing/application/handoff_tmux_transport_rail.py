"""Common tmux transport rail execution (Redmine #13729 tranche 4).

The ``orchestrate_handoff`` transport tail in ``application/commands.py`` historically
carried the **common tmux transport rail** inline: after the envelope is resolved, the
receiver admitted, and the herdr event-driven ``--mode standard`` rail (Redmine #13729
tranche 3, :mod:`handoff_herdr_standard_rail`) has had its chance to own the send, the
remaining tmux choreography injects the body once and drives it to a terminal disposition:

- inject ``f"{marker} {body}"`` into the target pane (``send-keys -l``);
- ``--mode pending`` emits a ``pending_input`` outcome, persists, and returns ``0`` (the body
  is parked in the composer; the sender does not press Enter);
- otherwise wait for the landing marker. On a strict (non ``queue-enter``) send a marker miss
  issues a **C-u rollback**, emits a ``blocked`` / ``marker_timeout`` outcome, prints the
  recovery guidance, and ``die``\\ s WITHOUT pressing Enter — the one place a C-u rollback is
  allowed;
- press Enter once. tmux ``--mode queue-enter`` retains its marker-miss Enter-only retry.
  Herdr ``--mode queue-enter`` instead arms a working-transition wait before Enter and, when
  this send's turn start is not causally confirmed, may issue **at most one** additional Enter
  after re-proving the same launch generation, current composer body, clear screen, readable
  runtime state, and a re-armed wait. Neither path re-types marker+body;
- under ``--mode standard`` observe the receiver pane for post-Enter turn-start activity; an
  unconfirmed turn start emits a ``blocked`` / ``turn_start_unconfirmed`` outcome and ``die``\\ s
  with **no C-u rollback and no re-send** (the uncertain-delivery no-blind-retry boundary);
- assemble the final ``sent`` outcome (``ok``, or ``queue_enter`` when the relaxed rail did not
  pre-confirm landing), fold in the additive Enter-only retry telemetry, the herdr queue-enter
  turn-start snapshot, and the focus-restore activation, then emit, persist, ledger (herdr only),
  and return ``0``.

This module carves that one coherent slice into an OOP-first application use case under
#12638 / #13729, the direct tmux sibling of the herdr rail carved in tranche 3, **without
touching** the envelope planner (#13729 tranche 2), the target/admission resolution above it,
the herdr event rail (#13255), the turn-start observation domain
(:func:`observe_standard_turn_start` / :func:`observe_queue_enter_turn_start`), the delivery
record / ledger / persistence seams, or the retry-policy config boundary:

- :class:`TmuxTransportRailRequest` is the frozen typed input — everything the rail reads from an
  ``orchestrate_handoff`` local (the resolved envelope value objects + ticketless payloads, the
  record-format / duplicate-lane diagnostics, the mode / marker / body, the raw landing / submit /
  retry scalars it coerces, the opt-in persistence + q-enter submit scalars, the ``herdr_send``
  backend predicate, and the pre-resolved focus-restore activation).
- :class:`TmuxTransportRailOps` is the port for the *side-effecting* dependencies the slice needs
  from its environment (inject the body, wait for the marker, capture the pane, C-u rollback,
  press Enter, sleep, observe standard / queue-enter turn starts, emit / persist / ledger, restore
  the previously-active pane, emit the marker-timeout guidance, ``die``), so
  :meth:`TmuxTransportRailUseCase.execute` is exercisable with a synthetic fake port and no live
  tmux / herdr / Redmine.
- :class:`TmuxTransportRailUseCase` holds the slice body: the three retry / rollback policy
  conditions (uncertain-delivery no-blind-retry, C-u rollback allowed only on a strict marker
  miss, the unchanged tmux marker retry, and the Herdr causal at-most-once Enter fallback) live
  here as typed control flow over the injected effects.
- :class:`LiveTmuxTransportRailOps` routes every effect through the :mod:`commands` module *at
  call time* (``run_tmux`` / ``capture_pane`` / ``wait_for_text`` — which ``bind_runtime_transport``
  swaps for herdr shims and the tests monkeypatch — plus ``_observe_standard_turn_start`` /
  ``_observe_queue_enter_turn_start`` / ``_maybe_restore_previous_active`` /
  ``_emit_handoff_marker_timeout_guidance`` / ``_maybe_persist_delivery_record`` /
  ``_record_herdr_send_ledger`` / ``die`` and the stashed ``active_herdr_turn_start_rail``), so
  the existing transport-wiring swap and every ``commands.*`` monkeypatch seam keep intercepting
  the side effects unchanged and no import cycle is introduced (``commands`` imports this module at
  module load; this module imports ``commands`` only lazily inside the live adapter). The emit
  closure is the facade's per-call publishing emitter (``make_publishing_emitter``), injected
  through the constructor so publication stays a property of emitting (Redmine #13583 R3-F1).

The pure collaborators (:func:`make_outcome`, :func:`submit_lines_for`,
:func:`turn_start_record_lines`, :func:`queue_enter_turn_start_record_lines`,
:func:`resolve_turn_start_window`, :func:`resolve_queue_enter_retry_policy`,
:func:`marker_visible_in`) are imported and called directly — they take no environment and are
already unit-covered — so the port stays scoped to the genuine side effects. The #13729 carve was
a pure, behavior-preserving restructuring: the injected keys, the emitted outcomes, the ledger /
persisted records, the exit code, and both ``die`` messages were byte-identical to the original
inline block.

Redmine #14232 adds ONE behavioural terminal: every transport-touching step now runs inside
:meth:`TmuxTransportRailUseCase.execute`'s ``TerminalTransportError`` guard, so a raised herdr
primitive closes to a typed ``blocked`` / ``transport_error`` outcome (assembled by the
:mod:`handoff_transport_failure_gate` sibling) instead of escaping as an uncaught traceback. tmux
is unaffected — its failures are ``subprocess.CalledProcessError``, which the guard does not catch.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol

from mozyo_bridge.application.handoff_delivery_command import submit_lines_for
from mozyo_bridge.application.session_bootstrap_command import marker_visible_in
from mozyo_bridge.application.turn_start_observation import (
    QueueEnterTurnStartObservation,
    TurnStartObservation,
    queue_enter_turn_start_record_lines,
    resolve_turn_start_window,
    turn_start_record_lines,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    MODE_PENDING,
    MODE_QUEUE_ENTER,
    MODE_STANDARD,
    DeliveryOutcome,
    ExecutionRoot,
    NormalizedAnchor,
    QueueEnterRetryOutcome,
    TargetActivationOutcome,
    make_outcome,
    resolve_queue_enter_retry_policy,
)
# Redmine #14232: typed containment for a raised transport primitive + the fixed step vocabulary.
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_transport_failure_gate import (  # noqa: E501
    STEP_READ_PANE_LANDING_WAIT,
    STEP_READ_PANE_RETRY_PROBE,
    STEP_READ_PANE_TURN_START_BASELINE,
    STEP_READ_PANE_TURN_START_OBSERVE,
    STEP_SEND_KEYS_ENTER,
    STEP_SEND_KEYS_ENTER_RETRY,
    STEP_SEND_KEYS_ROLLBACK,
    STEP_SEND_TEXT_BODY,
    close_transport_failure,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    REASON_TRANSPORT_ERROR,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    RoleProfileResolution,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    RUNTIME_AWAITING_INPUT,
    RUNTIME_BUSY,
    RUNTIME_TURN_ENDED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    DEFAULT_WAIT_TIMEOUT_MS as HERDR_QUEUE_WAIT_TIMEOUT_MS,
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
    RESEND_SKIP_STARTUP_SCREEN,
    RESEND_SKIP_STATE_NOT_INJECTABLE,
    RESEND_SKIP_STATE_UNREADABLE,
    RESEND_SKIP_WAIT_UNARMED,
    RESEND_SKIP_REASONS,
    current_composer_retains_body,
    screen_guard_detects,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_callback import (
    TicketlessCallback,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
    TicketlessConsultation,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_work_intake import (
    TicketlessWorkIntake,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    TransitionRoleBoundary,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.workflow_contract import (
    WorkflowContractBundle,
)


#: The per-call publishing emitter injected by the facade (``make_publishing_emitter``):
#: ``emit(outcome, **emit_kwargs)`` — publishes then renders the delivery outcome.
PublishingEmitter = Callable[..., None]


@dataclass(frozen=True)
class QueueEnterResendGate:
    """One strict Herdr queue-enter resend decision (redaction-safe).

    ``skip_reason == ""`` authorises one additional Enter.  ``runtime_state`` is
    recorded only to decide whether a later ``changed`` event is attributable to
    that Enter: ``awaiting_input`` / ``turn_ended`` are causal baselines, while
    ``busy`` may permit queueing but can never by itself confirm this payload.
    """

    skip_reason: str = RESEND_SKIP_NONE
    runtime_state: Optional[str] = None

    def __post_init__(self) -> None:
        if self.skip_reason not in RESEND_SKIP_REASONS:
            raise ValueError(f"unknown queue-enter resend skip reason: {self.skip_reason!r}")

    @property
    def allowed(self) -> bool:
        return self.skip_reason == RESEND_SKIP_NONE


@dataclass(frozen=True)
class TmuxTransportRailRequest:
    """The typed input for the common tmux transport rail slice.

    Every field is the value the original inline block read from an ``orchestrate_handoff``
    local: ``target`` / ``marker`` / ``body`` drive the injected text (``f"{marker} {body}"``)
    and the marker gate; ``mode`` selects the rail; the envelope value objects + ticketless
    payloads are the terminal-outcome context; ``record_format`` / ``record_command`` /
    ``duplicate_lane_panes`` are the record diagnostics; ``submit_intent`` / ``submit_delivery_id``
    / ``persist_delivery`` are the q-enter + opt-in persistence scalars; ``herdr_send`` gates the
    #13300 ledger + the #13292 queue-enter snapshot; ``read_lines`` / ``landing_timeout`` /
    ``submit_delay`` / ``queue_enter_retry_window`` / ``queue_enter_retry_interval`` are the raw
    scalars the slice coerces exactly as before; ``target_activation`` /
    ``restore_previous_active`` carry the pre-resolved focus-restore state. Frozen: the slice
    never mutates its input.
    """

    target: str
    marker: str
    body: str
    receiver: str
    anchor: Optional[NormalizedAnchor]
    mode: str
    kind: Optional[str]
    execution_root: Optional[ExecutionRoot]
    role_profile_resolution: Optional[RoleProfileResolution]
    role_profile_contract: Optional[str]
    transition_role_boundary: Optional[TransitionRoleBoundary]
    workflow_contract_bundle: Optional[WorkflowContractBundle]
    ticketless_callback: Optional[TicketlessCallback]
    ticketless_consultation: Optional[TicketlessConsultation]
    ticketless_work_intake: Optional[TicketlessWorkIntake]
    record_format: str
    record_command: Optional[str]
    duplicate_lane_panes: List[str]
    submit_intent: Optional[str]
    submit_delivery_id: Optional[str]
    persist_delivery: bool
    herdr_send: bool
    read_lines: int
    landing_timeout: Optional[float]
    submit_delay: Optional[float]
    queue_enter_retry_window: Optional[float]
    queue_enter_retry_interval: Optional[float]
    target_activation: Optional[TargetActivationOutcome]
    restore_previous_active: bool


class TmuxTransportRailOps(Protocol):
    """Port: the side-effecting dependencies the common tmux transport rail slice needs.

    The pure collaborators (:func:`make_outcome`, :func:`submit_lines_for`,
    :func:`turn_start_record_lines`, :func:`queue_enter_turn_start_record_lines`,
    :func:`resolve_turn_start_window`, :func:`resolve_queue_enter_retry_policy`,
    :func:`marker_visible_in`) are NOT here — the use case calls them directly. Only the genuine
    side effects are ported so the slice is exercisable with a synthetic fake that records the
    calls.
    """

    def inject_body(self, target: str, text: str) -> None:
        """Type the ``marker+body`` literal into ``target`` (``send-keys -l``, no Enter)."""
        ...

    def wait_for_marker(
        self, target: str, marker: str, lines: int, timeout: float
    ) -> bool:
        """Poll ``target`` up to ``timeout`` for the landing ``marker``; True if observed."""
        ...

    def capture(self, target: str, lines: int) -> str:
        """Read the last ``lines`` of ``target`` pane text (pre-Enter baseline / retry probe)."""
        ...

    def rollback(self, target: str) -> None:
        """Issue a C-u rollback in ``target`` (clear the unsubmitted composer line)."""
        ...

    def press_enter(self, target: str) -> None:
        """Send a single Enter to ``target`` (never re-types the marker+body)."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds`` (submit delay / Enter-only retry interval)."""
        ...

    def observe_standard_turn_start(
        self, target: str, *, baseline_capture: str, window_seconds: float, lines: int
    ) -> TurnStartObservation:
        """Poll ``target`` after Enter for post-submit turn-start activity (read-only)."""
        ...

    def observe_queue_enter_turn_start(
        self, target: str
    ) -> Optional[QueueEnterTurnStartObservation]:
        """Read-only herdr queue-enter runtime snapshot, or ``None`` when no rail is installed."""
        ...

    def observe_queue_enter_runtime_state(self, target: str) -> Optional[str]:
        """A mechanically-successful pre-Enter Herdr state, otherwise ``None``."""
        ...

    def observe_queue_enter_gateway_binding(self, target: str) -> Optional[dict]:
        """The collision-free launch-generation binding for the current target."""
        ...

    def arm_queue_enter_turn_wait(self, target: str, *, timeout_ms: int):
        """Arm the bound Herdr working-transition wait before an Enter."""
        ...

    def collect_queue_enter_turn_wait(self, armed) -> Optional[str]:
        """Collect a queue-enter wait and return its closed kind, or ``None``."""
        ...

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict],
    ) -> QueueEnterResendGate:
        """Re-prove identity, generation, composer, screen, and state before retry."""
        ...

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
        """Emit (publish + render) the terminal delivery outcome."""
        ...

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
        """Opt-in ``--persist-delivery`` durable persistence for a terminal outcome."""
        ...

    def record_ledger(
        self,
        outcome: DeliveryOutcome,
        *,
        retry_outcome: Optional[QueueEnterRetryOutcome],
    ) -> None:
        """Persist the #13296 herdr delivery-ledger entry for a herdr queue-enter send (#13300)."""
        ...

    def restore_previous_active(
        self,
        activation: Optional[TargetActivationOutcome],
        *,
        restore_previous_active: bool,
    ) -> Optional[TargetActivationOutcome]:
        """Best-effort post-delivery focus restore (pane selection only, #12597)."""
        ...

    def emit_marker_timeout_guidance(self, receiver: str) -> None:
        """Print the strict-rail marker_timeout stderr recovery trailer for ``receiver``."""
        ...

    def die(self, message: str) -> None:
        """Terminate the send with a non-zero exit and ``message`` (raises)."""
        ...


class TmuxTransportRailUseCase:
    """The common tmux transport rail slice.

    Injects the marker+body once, then drives it to a terminal disposition depending on mode +
    landing: ``pending`` parks and returns; a strict marker miss C-u-rolls-back and dies; an
    backend-specific Enter-only retry nudges the ``queue-enter`` prompt; an unconfirmed
    ``standard`` turn start dies with no rollback and no re-send; and the final ``sent`` assembly
    emits + persists + ledgers (herdr only) and returns ``0``. Every path returns or dies without
    falling through (the caller returns this method's result).
    """

    def __init__(
        self,
        ops: TmuxTransportRailOps,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ops = ops
        self._monotonic = monotonic
        self._current_step = STEP_SEND_TEXT_BODY  # Redmine #14232: primitive in flight

    def _outcome(
        self,
        request: TmuxTransportRailRequest,
        *,
        status: str,
        reason: str,
        queue_enter_turn_start_observation: Optional[dict] = None,
    ) -> DeliveryOutcome:
        """Assemble a terminal :class:`DeliveryOutcome` from the request context.

        The context threading (receiver / target / anchor / mode / kind / marker / envelope
        value objects / ticketless payloads) is identical across the pending / marker_timeout /
        turn_start_unconfirmed / sent terminals; only ``status`` / ``reason`` and the additive
        herdr queue-enter snapshot differ. ``status`` / ``reason`` are the wire-literal constants
        the call sites pass, re-narrowed to the ``Status`` / ``Reason`` wire enums by
        :func:`make_outcome`'s signature.
        """
        return make_outcome(
            status=status,  # type: ignore[arg-type]
            reason=reason,  # type: ignore[arg-type]
            receiver=request.receiver,
            target=request.target,
            anchor=request.anchor,
            mode=request.mode,
            kind=request.kind,
            notification_marker=request.marker,
            execution_root=request.execution_root,
            role_profile=request.role_profile_resolution,
            transition_role=request.transition_role_boundary,
            workflow_contract=request.workflow_contract_bundle,
            ticketless_callback=request.ticketless_callback,
            ticketless_consultation=request.ticketless_consultation,
            ticketless_work_intake=request.ticketless_work_intake,
            queue_enter_turn_start_observation=queue_enter_turn_start_observation,
        )

    def _submit_lines(
        self, request: TmuxTransportRailRequest, outcome: DeliveryOutcome
    ) -> Optional[List[str]]:
        return submit_lines_for(
            outcome,
            submit_intent=request.submit_intent,
            submit_delivery_id=request.submit_delivery_id,
        )

    def _fail_transport(
        self, request: TmuxTransportRailRequest, primitive: str
    ) -> "None":
        """Close a raised transport primitive into a typed terminal outcome (Redmine #14232).

        Defect, classification, and secret-safety posture: ``handoff_transport_failure_gate``.
        """
        outcome = self._outcome(
            request, status="blocked", reason=REASON_TRANSPORT_ERROR
        )
        close_transport_failure(
            outcome=outcome, primitive=primitive,
            target=request.target, marker=request.marker,
            emit=self._ops.emit, die=self._ops.die,
            record_format=request.record_format, record_command=request.record_command,
            duplicate_lane_panes=request.duplicate_lane_panes,
            role_profile_contract=request.role_profile_contract,
            submit_lines=self._submit_lines(request, outcome),
        )
        raise AssertionError("unreachable")

    def execute(self, request: TmuxTransportRailRequest) -> int:
        # Redmine #14232 (see the module docstring): the transport-failure containment guard. The
        # failed primitive comes from `_step`, never from the exception's adapter-authored message.
        try:
            return self._execute(request)
        except TerminalTransportError:
            self._fail_transport(request, self._current_step)
            raise AssertionError("unreachable")

    @staticmethod
    def _herdr_retry_enabled(retry_policy) -> bool:
        """Whether the Herdr queue may issue its one causal Enter retry.

        Sub-millisecond values cannot be represented by the Herdr wait CLI without
        overshooting the public window, so they conservatively disable the extra
        Enter.  The initial Enter and observation still run.
        """
        return (
            retry_policy.window_seconds >= 0.001
            and retry_policy.interval_seconds >= 0.001
        )

    def _remaining_wait_ms(self, deadline: float) -> int:
        """Whole milliseconds left before ``deadline``, capped to the rail default."""
        remaining = deadline - self._monotonic()
        if remaining < 0.001:
            return 0
        return min(HERDR_QUEUE_WAIT_TIMEOUT_MS, int(remaining * 1000.0))

    def _arm_queue_wait(self, target: str, *, timeout_ms: int):
        arm = getattr(self._ops, "arm_queue_enter_turn_wait", None)
        if arm is None or timeout_ms <= 0:
            return None
        try:
            return arm(target, timeout_ms=timeout_ms)
        except TypeError:
            # Compatibility for a staged fake/adapter that predates the timeout
            # keyword. Production implements the keyword and is deadline-capped.
            try:
                return arm(target)
            except Exception:  # noqa: BLE001 - fail closed to an unarmed observation
                return None
        except Exception:  # noqa: BLE001 - fail closed to an unarmed observation
            return None

    def _collect_queue_wait(self, armed) -> Optional[str]:
        collect = getattr(self._ops, "collect_queue_enter_turn_wait", None)
        if armed is None or collect is None:
            return None
        try:
            kind = collect(armed)
        except Exception:  # noqa: BLE001 - observer failure is not send evidence
            return None
        kind = str(kind or "").strip()
        return kind or None


    def _execute(self, request: TmuxTransportRailRequest) -> int:
        ops = self._ops
        raw_retry_values = (
            request.queue_enter_retry_window,
            request.queue_enter_retry_interval,
        )
        retry_values_finite = all(
            value is None or math.isfinite(float(value))
            for value in raw_retry_values
        )
        retry_policy = resolve_queue_enter_retry_policy(
            request.queue_enter_retry_window,
            request.queue_enter_retry_interval,
        )
        if (
            request.herdr_send
            and request.mode == MODE_QUEUE_ENTER
            and not retry_values_finite
        ):
            # argparse accepts ``nan`` / ``inf`` as floats.  Neither can define a
            # finite send deadline, so reject before the one body injection.
            outcome = self._outcome(request, status="blocked", reason="invalid_args")
            ops.emit(
                outcome,
                record_format=request.record_format,
                command=request.record_command,
                duplicate_lane_panes=request.duplicate_lane_panes or None,
                role_profile_contract=request.role_profile_contract,
                submit_lines=self._submit_lines(request, outcome),
            )
            ops.die(
                "queue-enter retry window and interval must be finite numbers; "
                "nothing was typed and Enter was not pressed"
            )
            raise AssertionError("unreachable")
        # The common body injection: the marker+body is typed ONCE here. No later path re-types
        # it — the whole no-blind-retry / rollback contract rests on this single injection.
        self._current_step = STEP_SEND_TEXT_BODY
        injected_text = f"{request.marker} {request.body}"
        ops.inject_body(request.target, injected_text)

        if request.mode == MODE_PENDING:
            # `--mode pending`: the body is parked in the composer; the sender never presses
            # Enter. Emit the pending_input outcome, persist opt-in, return.
            outcome = self._outcome(request, status="pending_input", reason="ok")
            ops.emit(
                outcome,
                record_format=request.record_format,
                command=request.record_command,
                duplicate_lane_panes=request.duplicate_lane_panes or None,
                role_profile_contract=request.role_profile_contract,
                submit_lines=self._submit_lines(request, outcome),
            )
            ops.persist(
                outcome,
                persist_delivery=request.persist_delivery,
                duplicate_lane_panes=request.duplicate_lane_panes,
                record_format=request.record_format,
            )
            return 0

        landing_timeout = float(request.landing_timeout or 8.0)
        landing_lines = max(request.read_lines, 200)
        self._current_step = STEP_READ_PANE_LANDING_WAIT
        marker_observed = ops.wait_for_marker(
            request.target, request.marker, landing_lines, landing_timeout
        )

        if not marker_observed and request.mode != MODE_QUEUE_ENTER:
            # C-u rollback is allowed ONLY here: a strict (non queue-enter) send whose marker
            # never landed. Roll back the unsubmitted line, emit blocked/marker_timeout, print
            # the recovery guidance, and die WITHOUT pressing Enter.
            self._current_step = STEP_SEND_KEYS_ROLLBACK
            ops.rollback(request.target)
            outcome = self._outcome(request, status="blocked", reason="marker_timeout")
            ops.emit(
                outcome,
                record_format=request.record_format,
                command=request.record_command,
                duplicate_lane_panes=request.duplicate_lane_panes or None,
                role_profile_contract=request.role_profile_contract,
                submit_lines=self._submit_lines(request, outcome),
            )
            ops.emit_marker_timeout_guidance(request.receiver)
            ops.die(
                "handoff marker was not observed in target pane; a C-u rollback was issued and Enter was not pressed (the receiver composer state was not verified). "
                f"target={request.target} marker={request.marker}"
            )
            raise AssertionError("unreachable")

        submit_delay = max(0.0, float(request.submit_delay or 0.0))
        if submit_delay:
            ops.sleep(submit_delay)

        # Redmine #13166 / #13262: on the strict `--mode standard` rail, snapshot the receiver
        # pane immediately before Enter so the post-Enter turn-start observation has a pre-submit
        # baseline. The marker was already observed (a marker miss died above), so this baseline
        # holds the marker+body sitting in the composer. queue-enter does not take this capture
        # baseline; Herdr uses its separate runtime/event baseline below, while tmux is unchanged.
        standard_rail = request.mode == MODE_STANDARD
        turn_start_window = resolve_turn_start_window(
            request.landing_timeout, landing_timeout
        )
        self._current_step = STEP_READ_PANE_TURN_START_BASELINE
        turn_start_baseline = (
            ops.capture(request.target, landing_lines) if standard_rail else None
        )

        # Herdr queue-enter keeps its busy-receiver queue semantics, but the event wait is no
        # longer telemetry-only (#15242). Capture the collision-free launch generation and the
        # pre-Enter runtime state, then arm BEFORE Enter. A changed event is a submission proof
        # only when the state was idle/turn-ended and the generation remains coherent; a busy
        # baseline may still accept an Enter but can never turn an unrelated working transition
        # into a confirmed submission.
        herdr_queue_enter = request.herdr_send and request.mode == MODE_QUEUE_ENTER
        queue_enter_armed_wait = None
        queue_enter_pre_binding = None
        queue_enter_baseline_state = None
        queue_retry_enabled = herdr_queue_enter and self._herdr_retry_enabled(retry_policy)
        queue_retry_deadline = (
            self._monotonic() + retry_policy.window_seconds
            if queue_retry_enabled
            else None
        )
        if herdr_queue_enter:
            _bind = getattr(ops, "observe_queue_enter_gateway_binding", None)
            if _bind is not None:
                try:
                    queue_enter_pre_binding = _bind(request.target)
                except Exception:  # noqa: BLE001 - missing identity withholds retry/confirmation
                    queue_enter_pre_binding = None
            _state = getattr(ops, "observe_queue_enter_runtime_state", None)
            if _state is not None:
                try:
                    queue_enter_baseline_state = _state(request.target)
                except Exception:  # noqa: BLE001 - unreadable state cannot confirm causality
                    queue_enter_baseline_state = None
            initial_wait_ms = (
                self._remaining_wait_ms(queue_retry_deadline)
                if queue_retry_deadline is not None
                else HERDR_QUEUE_WAIT_TIMEOUT_MS
            )
            queue_enter_armed_wait = self._arm_queue_wait(
                request.target, timeout_ms=initial_wait_ms
            )

        self._current_step = STEP_SEND_KEYS_ENTER
        ops.press_enter(request.target)
        enter_attempts = 1
        first_enter_at = self._monotonic()

        # Enter-only retry (Redmine #12580 / #12581). Only the `queue-enter` rail, and only when
        # the landing marker was not observed: a busy / redrawing TUI can drop the first Enter
        # even though the marker+body landed cleanly. Re-issue Enter — and ONLY Enter; the
        # marker+body typed once above is never re-injected, and an empty Enter on an idle agent
        # composer is a no-op, so the payload cannot be duplicated — on the policy interval until
        # the marker is observed or the window elapses. The `standard` / `pending` rails never
        # reach this branch, so their semantics are untouched.
        retry_engaged = (
            not (request.herdr_send and request.mode == MODE_QUEUE_ENTER)
            and request.mode == MODE_QUEUE_ENTER
            and not marker_observed
            and retry_policy.enabled
        )
        if retry_engaged:
            for _ in range(retry_policy.max_retries):
                if retry_policy.interval_seconds:
                    ops.sleep(retry_policy.interval_seconds)
                self._current_step = STEP_READ_PANE_RETRY_PROBE
                if marker_visible_in(
                    ops.capture(request.target, landing_lines), request.marker
                ):
                    marker_observed = True
                    break
                self._current_step = STEP_SEND_KEYS_ENTER_RETRY
                ops.press_enter(request.target)
                enter_attempts += 1

        # #15242: Herdr queue-enter uses the armed event plus a strict, at-most-once
        # Enter fallback. It never runs the marker-only loop above. A retry is allowed
        # only while the original absolute window has time left, after the public
        # minimum interval, and after the live adapter re-proves generation, current
        # composer tail, startup screen, and runtime state. The body is never touched.
        queue_first_wait_kind: Optional[str] = None
        queue_final_wait_kind: Optional[str] = None
        queue_causal_state: Optional[str] = queue_enter_baseline_state
        queue_resend_skipped_reason = RESEND_SKIP_NONE
        if herdr_queue_enter:
            queue_first_wait_kind = (
                self._collect_queue_wait(queue_enter_armed_wait) or WAIT_ERROR
            )
            queue_final_wait_kind = queue_first_wait_kind

            _bind = getattr(ops, "observe_queue_enter_gateway_binding", None)
            queue_binding_after_first = None
            if _bind is not None:
                try:
                    queue_binding_after_first = _bind(request.target)
                except Exception:  # noqa: BLE001 - no generation authority
                    queue_binding_after_first = None
            first_generation_coherent = (
                queue_enter_pre_binding is not None
                and queue_binding_after_first is not None
                and queue_enter_pre_binding == queue_binding_after_first
            )
            first_causally_confirmed = (
                queue_first_wait_kind == WAIT_CHANGED
                and queue_enter_baseline_state
                in (RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED)
                and first_generation_coherent
            )
            retry_candidate = (
                not first_causally_confirmed
                and queue_first_wait_kind != WAIT_ABSENT
            )
            if retry_candidate:
                if not queue_retry_enabled:
                    queue_resend_skipped_reason = RESEND_SKIP_DISABLED
                elif queue_retry_deadline is None:
                    queue_resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
                else:
                    delay = max(
                        0.0,
                        first_enter_at
                        + retry_policy.interval_seconds
                        - self._monotonic(),
                    )
                    remaining = queue_retry_deadline - self._monotonic()
                    if remaining < 0.001 or delay >= remaining:
                        queue_resend_skipped_reason = RESEND_SKIP_BUDGET_EXHAUSTED
                    else:
                        if delay:
                            ops.sleep(delay)
                        gate_fn = getattr(ops, "evaluate_queue_enter_resend", None)
                        if gate_fn is None:
                            gate = QueueEnterResendGate(
                                skip_reason=RESEND_SKIP_STATE_UNREADABLE
                            )
                        else:
                            try:
                                gate = gate_fn(
                                    request.target,
                                    injected_text,
                                    request.receiver,
                                    queue_enter_pre_binding,
                                )
                            except Exception:  # noqa: BLE001 - a failed proof refuses Enter
                                gate = QueueEnterResendGate(
                                    skip_reason=RESEND_SKIP_STATE_UNREADABLE
                                )
                        if not isinstance(gate, QueueEnterResendGate):
                            gate = QueueEnterResendGate(
                                skip_reason=RESEND_SKIP_STATE_UNREADABLE
                            )
                        if not gate.allowed:
                            queue_resend_skipped_reason = gate.skip_reason
                        else:
                            retry_wait_ms = self._remaining_wait_ms(queue_retry_deadline)
                            rearmed = self._arm_queue_wait(
                                request.target, timeout_ms=retry_wait_ms
                            )
                            if rearmed is None:
                                queue_resend_skipped_reason = RESEND_SKIP_WAIT_UNARMED
                            else:
                                # Arming can itself consume the last milliseconds of the
                                # absolute budget. Do not actuate after that budget; collect
                                # only to reap the armed observer.
                                if self._remaining_wait_ms(queue_retry_deadline) <= 0:
                                    self._collect_queue_wait(rearmed)
                                    queue_resend_skipped_reason = (
                                        RESEND_SKIP_BUDGET_EXHAUSTED
                                    )
                                else:
                                    self._current_step = STEP_SEND_KEYS_ENTER_RETRY
                                    ops.press_enter(request.target)
                                    enter_attempts += 1
                                    retry_engaged = True
                                    queue_causal_state = gate.runtime_state
                                    queue_final_wait_kind = (
                                        self._collect_queue_wait(rearmed) or WAIT_ERROR
                                    )

        # Redmine #13166 / #13262: standard-rail turn-start verification. Marker observed + Enter
        # issued proves the sender pressed Enter, not that the receiver TUI submitted the prompt
        # and started a turn — a busy / redrawing composer can absorb the Enter and leave the
        # marker+body unsubmitted while the rail still reported `sent` / `ok` (the false-positive
        # delivery this fixes). Observe the receiver pane for post-Enter turn-start activity
        # (read-only; no re-typed marker+body, no re-issued Enter, no auto-resend). An unconfirmed
        # turn start dies with NO C-u rollback and NO re-send. queue-enter is handled by its
        # backend-specific branches above and never enters this standard-only terminal.
        turn_start_lines: Optional[List[str]] = None
        if standard_rail:
            self._current_step = STEP_READ_PANE_TURN_START_OBSERVE
            turn_start = ops.observe_standard_turn_start(
                request.target,
                baseline_capture=turn_start_baseline or "",
                window_seconds=turn_start_window,
                lines=landing_lines,
            )
            turn_start_lines = turn_start_record_lines(
                turn_start, rail_label=f"{request.receiver} standard-rail"
            )
            if not turn_start.confirmed:
                outcome = self._outcome(
                    request, status="blocked", reason="turn_start_unconfirmed"
                )
                ops.emit(
                    outcome,
                    record_format=request.record_format,
                    command=request.record_command,
                    duplicate_lane_panes=request.duplicate_lane_panes or None,
                    role_profile_contract=request.role_profile_contract,
                    submit_lines=self._submit_lines(request, outcome),
                    turn_start_lines=turn_start_lines,
                )
                ops.die(
                    "handoff landing marker was observed and Enter was pressed, but the "
                    f"{request.receiver} receiver pane showed no turn-start activity within the "
                    "observation window; the Enter may have been absorbed by a busy / "
                    "redrawing composer. No C-u rollback and no re-send were issued (the "
                    "marker+body was typed once). Read the receiver to confirm whether "
                    "the turn started before re-issuing under --mode standard. "
                    f"target={request.target} marker={request.marker}"
                )
                raise AssertionError("unreachable")

        # Queue-enter keeps its own telemetry/ledger rail.  The authoritative
        # ``event_wait_kind=changed`` field is now published only for an idle/turn-ended
        # arm whose collision-free launch generation remained coherent. Raw first/final
        # wait kinds and the one-Enter fallback decision remain additive diagnostics;
        # a busy baseline can therefore be nudged without being over-claimed as confirmed.
        queue_enter_observation: Optional[dict] = None
        if herdr_queue_enter:
            queue_enter_post_binding = None
            _bind = getattr(ops, "observe_queue_enter_gateway_binding", None)
            if _bind is not None:
                try:
                    queue_enter_post_binding = _bind(request.target)
                except Exception:  # noqa: BLE001 - observation-only
                    queue_enter_post_binding = None
            generation_coherent = (
                queue_enter_pre_binding is not None
                and queue_enter_post_binding is not None
                and queue_enter_pre_binding == queue_enter_post_binding
            )
            snapshot = ops.observe_queue_enter_turn_start(request.target)
            queue_enter_observation = (
                snapshot.to_telemetry_dict()
                if snapshot is not None
                else {
                    "observation_kind": "post_choreography_snapshot",
                    "source": "herdr_agent_get",
                    "runtime_state": "unknown",
                    "read_ok": False,
                    "read_reason": "transport_error",
                    "poll_attempts": 0,
                }
            )
            extra = {
                "enter_attempts": enter_attempts,
                "first_event_wait_kind": queue_first_wait_kind,
                "final_event_wait_kind": queue_final_wait_kind,
                "resend_skipped_reason": queue_resend_skipped_reason,
            }
            if queue_enter_baseline_state is not None:
                extra["baseline_runtime_state"] = queue_enter_baseline_state
            if generation_coherent:
                extra["gateway_binding"] = queue_enter_post_binding
                extra["observation_version"] = 2
                if (
                    queue_final_wait_kind == WAIT_CHANGED
                    and queue_causal_state
                    in (RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED)
                ):
                    # The existing injection-stage and recovery readers trust only
                    # this field + the canonical v2 binding. Do not publish it for a
                    # busy baseline or an incoherent/recycled gateway.
                    extra["event_wait_kind"] = WAIT_CHANGED
            queue_enter_observation = {**queue_enter_observation, **extra}
            if snapshot is not None:
                # Reuse the additive `turn_start_lines` record channel (appended, never overrides
                # `next_action`). The structured observation above owns causal confirmation;
                # this renderer remains the redaction-safe post-choreography snapshot wording.
                turn_start_lines = queue_enter_turn_start_record_lines(snapshot)

        # Wording-layer differentiation under the relaxed `queue-enter` rail: marker observed
        # (possibly via the Enter-only retry above) -> strict `sent`/`ok`; marker still unobserved
        # -> `sent`/`queue_enter` (sender did not pre-confirm landing). The receiver-side contract
        # and `next_action_owner` stay identical to strict `sent` per the contract.
        relaxed_unobserved = request.mode == MODE_QUEUE_ENTER and not marker_observed
        outcome = self._outcome(
            request,
            status="sent",
            reason="queue_enter" if relaxed_unobserved else "ok",
            queue_enter_turn_start_observation=queue_enter_observation,
        )
        # Durable retry telemetry (policy + attempted count + interval) is recorded only when the
        # Enter-only retry actually engaged. It is wording-layer only: it never reaches the wire
        # enums or the inspector projection.
        retry_record = (
            QueueEnterRetryOutcome(
                window_seconds=retry_policy.window_seconds,
                interval_seconds=retry_policy.interval_seconds,
                enter_attempts=enter_attempts,
                marker_observed=marker_observed,
            )
            if retry_engaged and not herdr_queue_enter
            else None
        )
        # Redmine #12597: if standard_target_admission activated an inactive split and the policy
        # asks to restore focus, re-select the previously-active pane after delivery. Pane
        # selection only, best-effort, and the restore fact is recorded.
        activation = ops.restore_previous_active(
            request.target_activation,
            restore_previous_active=request.restore_previous_active,
        )
        ops.emit(
            outcome,
            record_format=request.record_format,
            command=request.record_command,
            duplicate_lane_panes=request.duplicate_lane_panes or None,
            role_profile_contract=request.role_profile_contract,
            retry=retry_record,
            activation=activation,
            submit_lines=self._submit_lines(request, outcome),
            turn_start_lines=turn_start_lines,
        )
        ops.persist(
            outcome,
            persist_delivery=request.persist_delivery,
            duplicate_lane_panes=request.duplicate_lane_panes,
            record_format=request.record_format,
            retry=retry_record,
            activation=activation,
            turn_start_lines=turn_start_lines,
        )
        # Redmine #13300: persist the herdr queue-enter outcome to the #13296 ledger. This
        # terminal block is shared with tmux, so the emission is guarded on `herdr_send` (tmux
        # 経路不変); the Enter-only retry telemetry enriches the same entry.
        if request.herdr_send:
            ops.record_ledger(outcome, retry_outcome=retry_record)
        return 0


class LiveTmuxTransportRailOps:
    """Live :class:`TmuxTransportRailOps`.

    Every effect routes through the :mod:`commands` module *at call time*: ``run_tmux`` /
    ``capture_pane`` / ``wait_for_text`` (which ``bind_runtime_transport`` swaps for herdr shims
    and the tests monkeypatch), the ``_observe_standard_turn_start`` /
    ``_observe_queue_enter_turn_start`` / ``_maybe_restore_previous_active`` /
    ``_emit_handoff_marker_timeout_guidance`` / ``_maybe_persist_delivery_record`` /
    ``_record_herdr_send_ledger`` / ``die`` re-exports, and the stashed
    ``active_herdr_turn_start_rail``. Resolving them from ``commands`` keeps the transport-wiring
    swap and every monkeypatch seam in force and introduces no import cycle. The emit closure is
    the facade's per-call publishing emitter, injected at construction so publication stays a
    property of emitting (Redmine #13583 R3-F1).
    """

    def __init__(self, emit: PublishingEmitter) -> None:
        self._emit = emit

    def inject_body(self, target: str, text: str) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands.run_tmux("send-keys", "-t", target, "-l", "--", text)

    def wait_for_marker(
        self, target: str, marker: str, lines: int, timeout: float
    ) -> bool:
        from mozyo_bridge.application import commands as _commands

        return _commands.wait_for_text(target, marker, lines, timeout)

    def capture(self, target: str, lines: int) -> str:
        from mozyo_bridge.application import commands as _commands

        return _commands.capture_pane(target, lines)

    def rollback(self, target: str) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands.run_tmux("send-keys", "-t", target, "C-u")

    def press_enter(self, target: str) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands.run_tmux("send-keys", "-t", target, "Enter")

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)

    def observe_standard_turn_start(
        self, target: str, *, baseline_capture: str, window_seconds: float, lines: int
    ) -> TurnStartObservation:
        import time

        from mozyo_bridge.application import commands as _commands

        return _commands._observe_standard_turn_start(
            target,
            baseline_capture=baseline_capture,
            capture=_commands.capture_pane,
            sleep=time.sleep,
            window_seconds=window_seconds,
            lines=lines,
        )

    def observe_queue_enter_turn_start(
        self, target: str
    ) -> Optional[QueueEnterTurnStartObservation]:
        import time

        from mozyo_bridge.application import commands as _commands

        rail = _commands.active_herdr_turn_start_rail
        if rail is None:
            return None
        return _commands._observe_queue_enter_turn_start(
            target,
            read=rail.reader.read_agent_state,
            sleep=time.sleep,
        )

    def observe_queue_enter_runtime_state(self, target: str) -> Optional[str]:
        """Return a mechanically-successful state from the already-bound Herdr rail."""
        from mozyo_bridge.application import commands as _commands

        rail = _commands.active_herdr_turn_start_rail
        if rail is None:
            return None
        try:
            result = rail.reader.read_agent_state(target)
        except Exception:  # noqa: BLE001 - unreadable state cannot establish causality
            return None
        if not bool(getattr(result, "ok", False)):
            return None
        state = str(getattr(result, "state", "") or "").strip()
        return state or None

    # -- #14203 / #15242: armed wait + gateway process binding --------------------------

    def arm_queue_enter_turn_wait(self, target: str, *, timeout_ms: int):
        """Arm the active rail's bound Herdr wait before one queue Enter.

        Reusing the active rail is material: it guarantees the observer uses the
        same resolved binary/server/environment as the transport instead of
        independently resolving a second Herdr instance.
        """
        from mozyo_bridge.application import commands as _commands

        rail = _commands.active_herdr_turn_start_rail
        if rail is None:
            return None
        try:
            return rail.arm_turn_start_wait(target, timeout_ms=timeout_ms)
        except Exception:  # noqa: BLE001 - unarmed means no extra Enter may be sent
            return None

    def collect_queue_enter_turn_wait(self, armed) -> Optional[str]:
        """Collect the armed wait's closed result kind (``changed`` = working observed)."""
        if armed is None:
            return None
        try:
            kind = str(getattr(armed.collect(), "kind", "") or "").strip()
            return kind or None
        except Exception:  # noqa: BLE001 - observation-only
            return None

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict],
    ) -> QueueEnterResendGate:
        """Strictly prove the one Herdr queue-enter retry is safe (#15242)."""
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
        except Exception:  # noqa: BLE001 - unreadable/blank is never a clear composer
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
        except Exception:  # noqa: BLE001 - a failed state read refuses the retry
            return QueueEnterResendGate(RESEND_SKIP_STATE_UNREADABLE)
        if not bool(getattr(state_result, "ok", False)):
            return QueueEnterResendGate(RESEND_SKIP_STATE_UNREADABLE)
        state = str(getattr(state_result, "state", "") or "").strip()
        if state == "blocked":
            return QueueEnterResendGate(RESEND_SKIP_RECEIVER_BLOCKED, state)
        # Queue-enter deliberately permits ``busy``: that is the state in which
        # queueing is useful. The exact current-composer and generation checks above
        # are what distinguish a safe nudge from a blind keypress. Busy is never a
        # causal confirmation state; the use case records it only as gate telemetry.
        if state not in (RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED, RUNTIME_BUSY):
            return QueueEnterResendGate(RESEND_SKIP_STATE_NOT_INJECTABLE, state)
        return QueueEnterResendGate(RESEND_SKIP_NONE, state)

    def observe_queue_enter_gateway_binding(self, target: str) -> Optional[dict]:
        """The action-time gateway process GENERATION binding for ``target`` (j#87418 F2).

        A generation authority, not a loose annotation: it is emitted ONLY when a
        ``verdict=present`` startup self-attestation exists whose workspace / lane / role /
        assigned-name / locator ALL match the live inventory row's decoded identity + this
        target. Any missing / non-present / mismatched axis yields ``None`` (fail-closed) —
        so the recovery can trust ``attestation_observed_at`` as the exact generation pin.
        Redaction-safe tokens / timestamps only.
        """
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
            row for row in rows
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
        row_revision = _norm(str(revision_raw)) if not isinstance(revision_raw, bool) else ""
        # A generation-bound, verdict=present attestation whose EVERY identity axis matches the
        # decoded live-inventory identity AND this target locator — otherwise no binding.
        try:
            record = HerdrIdentityAttestationStore().read(name)
        except Exception:  # noqa: BLE001 - unreadable attestation => no binding
            return None
        if record is None:
            return None
        if not (
            _norm(getattr(record, "verdict", "")) == VERDICT_PRESENT
            and _norm(getattr(record, "workspace_id", "")) == _norm(identity.workspace_id)
            and _norm_lane(getattr(record, "lane_id", "")) == _norm_lane(identity.lane_id)
            and _norm(getattr(record, "role", "")) == _norm(identity.role)
            and _norm(getattr(record, "assigned_name", "")) == name
            and _norm(getattr(record, "locator", "")) == _norm(target)
        ):
            return None
        observed_at = _norm(str(getattr(record, "observed_at", "") or ""))
        # #14203 design j#87472: the COLLISION-FREE per-launch generation token is sourced
        # from the home-scoped launch-generation store, NOT the main attestation (whose
        # seconds-precision observed_at cannot separate two same-second launches, and which
        # must not carry a required token onto shared v1/v2 homes). The attestation above is
        # an INDEPENDENT health prerequisite; the token is the launch-generation authority's,
        # verified as an attested row for this exact identity whose startup transaction is a
        # terminally-successful participant of this gateway. A pending / superseded / tokenless
        # generation yields NO binding — recovery then fails closed rather than trusting a
        # non-unique timestamp.
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
        self._emit(
            outcome,
            record_format=record_format,
            command=command,
            duplicate_lane_panes=duplicate_lane_panes,
            role_profile_contract=role_profile_contract,
            retry=retry,
            activation=activation,
            submit_lines=submit_lines,
            turn_start_lines=turn_start_lines,
        )

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
        from mozyo_bridge.application import commands as _commands

        _commands._maybe_persist_delivery_record(
            outcome,
            persist_delivery=persist_delivery,
            duplicate_lane_panes=duplicate_lane_panes,
            record_format=record_format,
            retry=retry,
            activation=activation,
            turn_start_lines=turn_start_lines,
        )

    def record_ledger(
        self,
        outcome: DeliveryOutcome,
        *,
        retry_outcome: Optional[QueueEnterRetryOutcome],
    ) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands._record_herdr_send_ledger(outcome, retry_outcome=retry_outcome)

    def restore_previous_active(
        self,
        activation: Optional[TargetActivationOutcome],
        *,
        restore_previous_active: bool,
    ) -> Optional[TargetActivationOutcome]:
        from mozyo_bridge.application import commands as _commands

        return _commands._maybe_restore_previous_active(
            activation, restore_previous_active=restore_previous_active
        )

    def emit_marker_timeout_guidance(self, receiver: str) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands._emit_handoff_marker_timeout_guidance(receiver)

    def die(self, message: str) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands.die(message)


def run_tmux_transport_rail(
    request: TmuxTransportRailRequest, *, emit: PublishingEmitter
) -> int:
    """Live composition root: drive the common tmux transport rail for the handoff facade.

    Constructs :class:`TmuxTransportRailUseCase` over :class:`LiveTmuxTransportRailOps` (every
    effect routed through ``commands`` at call time) and runs the slice, exactly as the original
    inline block did. ``emit`` is the facade's per-call publishing emitter.
    """
    return TmuxTransportRailUseCase(LiveTmuxTransportRailOps(emit=emit)).execute(request)


__all__ = (
    "PublishingEmitter",
    "QueueEnterResendGate",
    "TmuxTransportRailRequest",
    "TmuxTransportRailOps",
    "TmuxTransportRailUseCase",
    "LiveTmuxTransportRailOps",
    "run_tmux_transport_rail",
)
