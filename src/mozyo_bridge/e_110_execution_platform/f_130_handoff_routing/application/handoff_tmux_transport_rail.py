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
- press Enter once. tmux ``--mode queue-enter`` retains its marker-miss retry; Herdr instead
  arms a working-transition wait for an idle/turn-ended baseline, while a BUSY baseline takes
  the ADR-0002 queued-submission path (#15537): no observer is required, the Enter passes a
  wait-free full effect fence, and the composer releasing the body reports ``sent`` /
  ``queue_enter``. Either flavour may issue deadline-bounded additional Enters after freshly
  re-proving launch generation, current composer, clear screen, and readable state (plus a
  re-armed wait on the causal flavour). Neither path re-types marker+body;
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
  miss, the unchanged tmux marker retry, and the Herdr causal deadline-bounded Enter-only
  fallback) live here as typed control flow over the injected effects.
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
already unit-covered — so the port stays scoped to genuine side effects. The #13729 carve kept
injected keys, outcomes, ledger/persistence, exit code, and both ``die`` messages byte-identical.

Redmine #14232 adds ONE behavioural terminal: every transport-touching step now runs inside
:meth:`TmuxTransportRailUseCase.execute`'s ``TerminalTransportError`` guard, so a raised herdr
primitive closes to a typed ``blocked`` / ``transport_error`` outcome (assembled by the
:mod:`handoff_transport_failure_gate` sibling) instead of escaping as an uncaught traceback. tmux
is unaffected — its failures are ``subprocess.CalledProcessError``, which the guard does not catch.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Protocol

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
    transport_failure_telemetry,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.handoff_herdr_queue_enter_rail import (
    HerdrQueueEnterSession,
    LiveHerdrQueueEnterOpsMixin,
    QueueEnterEffectFenceRefused,
    QueueEnterRetryProbeFailed,
    QueueEnterResendGate,
    retry_values_are_supported,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    REASON_TRANSPORT_ERROR,
    turn_start_positively_observed,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    RoleProfileResolution,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
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
    herdr_assigned_name: Optional[str]
    herdr_process_generation: Optional[str] = field(repr=False)
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

    def observe_queue_enter_gateway_binding(
        self, target: str
    ) -> Optional[dict[str, str]]:
        """The collision-free launch-generation binding for the current target."""
        ...

    def arm_queue_enter_turn_wait(
        self, target: str, *, timeout_ms: int
    ) -> Optional[object]:
        """Arm the bound Herdr working-transition wait before an Enter."""
        ...

    def collect_queue_enter_turn_wait(self, armed: object) -> Optional[str]:
        """Collect a queue-enter wait and return its closed kind, or ``None``."""
        ...

    def cancel_queue_enter_turn_wait(self, armed: object) -> None:
        """Cancel a queue-enter wait when no following Enter is authorised."""
        ...

    def queue_enter_turn_wait_pending(self, armed: object) -> bool:
        """Whether the wait is still live immediately before Enter."""
        ...

    def evaluate_queue_enter_resend(
        self,
        target: str,
        text: str,
        receiver: str,
        baseline_binding: Optional[dict[str, str]],
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
        self, outcome: DeliveryOutcome, *,
        retry_outcome: Optional[QueueEnterRetryOutcome],
        backend: Optional[str] = None,
        rail: Optional[str] = None,
        disposition: Optional[str] = None,
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
        queue_enter_turn_start_observation: Optional[dict[str, Any]] = None,
        transport_failure: Optional[dict[str, Any]] = None,
    ) -> DeliveryOutcome:
        """Assemble a terminal :class:`DeliveryOutcome` from the request context.

        Context threading stays identical across every terminal; only the
        terminal fields and additive Herdr telemetry differ.
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
            transport_failure=transport_failure,
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
        """Close a raised transport primitive into a typed terminal outcome (#14232)."""
        outcome = self._outcome(
            request,
            status="blocked",
            reason=REASON_TRANSPORT_ERROR,
            transport_failure=transport_failure_telemetry(primitive),
        )
        if request.herdr_send and request.mode == MODE_QUEUE_ENTER:
            self._ops.record_ledger(
                outcome, retry_outcome=None, backend="herdr", rail="queue_enter_rail",
                disposition=primitive,
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
        except TerminalTransportError as exc:
            if isinstance(exc, QueueEnterRetryProbeFailed):
                self._current_step = STEP_READ_PANE_RETRY_PROBE
            self._fail_transport(request, self._current_step)
            raise AssertionError("unreachable")

    def _execute(self, request: TmuxTransportRailRequest) -> int:
        ops = self._ops
        raw_retry_values = (
            request.queue_enter_retry_window,
            request.queue_enter_retry_interval,
        )
        retry_values_valid = retry_values_are_supported(*raw_retry_values)
        retry_policy = resolve_queue_enter_retry_policy(
            request.queue_enter_retry_window,
            request.queue_enter_retry_interval,
        )
        if (
            request.herdr_send
            and request.mode == MODE_QUEUE_ENTER
            and not retry_values_valid
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
                "queue-enter retry window and interval must be finite numbers no "
                "greater than 3600 seconds; "
                "nothing was typed and Enter was not pressed"
            )
            raise AssertionError("unreachable")
        herdr_queue_enter = request.herdr_send and request.mode == MODE_QUEUE_ENTER
        injected_text = f"{request.marker} {request.body}"
        queue_session: Optional[HerdrQueueEnterSession] = None
        if herdr_queue_enter:
            queue_session = HerdrQueueEnterSession(
                ops=ops,
                target=request.target,
                text=injected_text,
                receiver=request.receiver,
                expected_assigned_name=request.herdr_assigned_name,
                expected_process_generation=request.herdr_process_generation,
                retry_policy=retry_policy,
                monotonic=self._monotonic,
            )
            # Pin the collision-free process generation BEFORE the body reaches
            # the composer. A locator recycled between body injection and Enter
            # is then detected and never receives the Enter.
            if not queue_session.capture_before_body():
                outcome = self._outcome(
                    request, status="blocked", reason="target_unavailable"
                )
                ops.emit(
                    outcome,
                    record_format=request.record_format,
                    command=request.record_command,
                    duplicate_lane_panes=request.duplicate_lane_panes or None,
                    role_profile_contract=request.role_profile_contract,
                    submit_lines=self._submit_lines(request, outcome),
                )
                ops.die(
                    "Herdr queue-enter could not prove that the current target is "
                    "the exact authorised receiver terminal generation; nothing was "
                    "typed and Enter was not pressed. Resolve the target again before "
                    "retrying. Re-running session-start only adopts a live legacy pair "
                    "and cannot create missing launch proof. A record-less non-default "
                    "scratch pair can use session-retire preflight/execute followed by a "
                    "fresh session-start. Lifecycle-managed and default pairs have no "
                    "general receipt-refresh rail in this build; keep them fail-closed "
                    "and follow the terminal-bound receipt upgrade runbook. A database "
                    f"retry or migration cannot repair this. target={request.target}"
                )
                raise AssertionError("unreachable")

        # The common body injection: the marker+body is typed ONCE here. No later path re-types
        # it — the whole no-blind-retry / rollback contract rests on this single injection.
        self._current_step = STEP_SEND_TEXT_BODY
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
        marker_observed = False
        if not herdr_queue_enter:
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
        first_enter_armed = (
            queue_session.arm_before_first_enter()
            if queue_session is not None
            else True
        )
        enter_attempts = 0
        if first_enter_armed:
            self._current_step = STEP_SEND_KEYS_ENTER
            try:
                if queue_session is None:
                    ops.press_enter(request.target)
                else:
                    with queue_session.enter_effect_boundary(queue_session.armed_wait):
                        ops.press_enter(request.target)
            except QueueEnterEffectFenceRefused:
                if queue_session is not None:
                    queue_session.cancel_before_failed_enter()
                first_enter_armed = False
            except TerminalTransportError:
                if queue_session is not None:
                    queue_session.cancel_before_failed_enter()
                raise
            if first_enter_armed:
                enter_attempts = 1
        if queue_session is not None and first_enter_armed:
            queue_session.note_first_enter_sent()

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

        # Herdr queue-enter skips the tmux marker loop; this session owns waits and deadline.
        if queue_session is not None and first_enter_armed:
            def _press_extra_enter() -> None:
                self._current_step = STEP_SEND_KEYS_ENTER_RETRY
                ops.press_enter(request.target)
            self._current_step = STEP_READ_PANE_RETRY_PROBE  # retry authorization reads
            if queue_session.busy_queue_path:
                # ADR-0002 (#15537): a busy receiver cannot yield a causal turn start,
                # so completion proves queued submission (composer cleared) instead.
                queue_session.complete_after_busy_enter(
                    press_extra_enter=_press_extra_enter
                )
            else:
                queue_session.complete_after_first_enter(
                    press_extra_enter=_press_extra_enter
                )
            enter_attempts = queue_session.enter_attempts
            retry_engaged = queue_session.retry_engaged

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
        # wait kinds and the bounded fallback decisions remain additive diagnostics;
        # a busy baseline can therefore be nudged without being over-claimed as confirmed.
        queue_enter_observation: Optional[dict[str, Any]] = None
        if queue_session is not None:
            snapshot = ops.observe_queue_enter_turn_start(request.target)
            queue_enter_observation = queue_session.observation(snapshot)
            if snapshot is not None:
                # Reuse the additive `turn_start_lines` record channel (appended, never overrides
                # `next_action`). The structured observation above owns causal confirmation;
                # this renderer remains the redaction-safe post-choreography snapshot wording.
                turn_start_lines = queue_enter_turn_start_record_lines(snapshot)

        # tmux keeps the legacy marker-based queue result. Herdr now has a causal
        # authority: only an armed working transition on the same launch generation
        # is a successful submit. Missing evidence is an uncertain partial delivery,
        # never rc=0 / sent merely because Enter was issued (#15242 acceptance).
        relaxed_unobserved = request.mode == MODE_QUEUE_ENTER and not marker_observed
        # ADR-0002 (#15537): a busy receiver whose composer verifiably released the
        # injected body after an Enter is a QUEUED submission — the receiver CLI holds
        # the message and processes it when its current turn ends. That is the same
        # practical promise the tmux rail reports as ``sent / queue_enter``, so the
        # existing vocabulary is reused; a causal turn start is still never claimed.
        herdr_queued_submission = (
            queue_session is not None
            and queue_session.queued_submission_confirmed
            and not turn_start_positively_observed(queue_enter_observation)
        )
        herdr_queue_unconfirmed = queue_session is not None and not (
            turn_start_positively_observed(queue_enter_observation)
            or queue_session.queued_submission_confirmed
        )
        outcome = self._outcome(
            request,
            status="blocked" if herdr_queue_unconfirmed else "sent",
            reason=(
                queue_session.failure_reason
                if herdr_queue_unconfirmed
                else "queue_enter"
                if herdr_queued_submission
                else "ok"
                if queue_session is not None
                else "queue_enter"
                if relaxed_unobserved
                else "ok"
            ),
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
        if herdr_queue_unconfirmed:
            ops.die(
                "Herdr queue-enter typed the marker+body once and pressed Enter "
                f"{enter_attempts} time(s), but no causally attributable turn start "
                "was confirmed on the same launch generation. The uncertain partial "
                f"delivery was recorded as blocked / {outcome.reason}; no body "
                "resend or rollback was issued, and blind retry is prohibited. Read "
                "the receiver and durable anchor before any explicit recovery. "
                f"target={request.target} marker={request.marker}"
            )
            raise AssertionError("unreachable")
        return 0


class LiveTmuxTransportRailOps(LiveHerdrQueueEnterOpsMixin):
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
        self, outcome: DeliveryOutcome, *,
        retry_outcome: Optional[QueueEnterRetryOutcome],
        backend: Optional[str] = None,
        rail: Optional[str] = None,
        disposition: Optional[str] = None,
    ) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands._record_herdr_send_ledger(
            outcome, retry_outcome=retry_outcome, backend=backend, rail=rail,
            disposition=disposition,
        )

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
