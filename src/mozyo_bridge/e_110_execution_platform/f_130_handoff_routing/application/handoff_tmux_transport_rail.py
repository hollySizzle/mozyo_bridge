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
- press Enter once. Under ``--mode queue-enter`` (and only when the marker was not observed) an
  **Enter-only retry** re-issues Enter — and ONLY Enter, never the marker+body — on the policy
  interval until the marker lands or the window elapses;
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
  miss, Enter-only retry only under ``queue-enter`` + marker-unobserved) live here as typed
  control flow over the injected effects.
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
already unit-covered — so the port stays scoped to the genuine side effects. This is a pure,
behavior-preserving restructuring: the injected keys, the emitted outcomes, the ledger / persisted
records, the exit code, and both ``die`` messages are byte-identical to the original inline block.
"""
from __future__ import annotations

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
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    RoleProfileResolution,
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
    Enter-only retry nudges the ``queue-enter`` prompt; an unconfirmed ``standard`` turn start dies
    with no rollback and no re-send; and the final ``sent`` assembly emits + persists + ledgers
    (herdr only) and returns ``0``. Every path returns or dies without falling through (the caller
    returns this method's result).
    """

    def __init__(self, ops: TmuxTransportRailOps) -> None:
        self._ops = ops

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

    def execute(self, request: TmuxTransportRailRequest) -> int:
        ops = self._ops
        # The common body injection: the marker+body is typed ONCE here. No later path re-types
        # it — the whole no-blind-retry / rollback contract rests on this single injection.
        ops.inject_body(request.target, f"{request.marker} {request.body}")

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
        marker_observed = ops.wait_for_marker(
            request.target, request.marker, landing_lines, landing_timeout
        )

        if not marker_observed and request.mode != MODE_QUEUE_ENTER:
            # C-u rollback is allowed ONLY here: a strict (non queue-enter) send whose marker
            # never landed. Roll back the unsubmitted line, emit blocked/marker_timeout, print
            # the recovery guidance, and die WITHOUT pressing Enter.
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
        # holds the marker+body sitting in the composer. The queue-enter rail keeps its prior
        # behavior untouched (its marker-unobserved path stays `sent` / `queue_enter`).
        standard_rail = request.mode == MODE_STANDARD
        turn_start_window = resolve_turn_start_window(
            request.landing_timeout, landing_timeout
        )
        turn_start_baseline = (
            ops.capture(request.target, landing_lines) if standard_rail else None
        )

        ops.press_enter(request.target)
        enter_attempts = 1

        # Enter-only retry (Redmine #12580 / #12581). Only the `queue-enter` rail, and only when
        # the landing marker was not observed: a busy / redrawing TUI can drop the first Enter
        # even though the marker+body landed cleanly. Re-issue Enter — and ONLY Enter; the
        # marker+body typed once above is never re-injected, and an empty Enter on an idle agent
        # composer is a no-op, so the payload cannot be duplicated — on the policy interval until
        # the marker is observed or the window elapses. The `standard` / `pending` rails never
        # reach this branch, so their semantics are untouched.
        retry_policy = resolve_queue_enter_retry_policy(
            request.queue_enter_retry_window,
            request.queue_enter_retry_interval,
        )
        retry_engaged = (
            request.mode == MODE_QUEUE_ENTER
            and not marker_observed
            and retry_policy.enabled
        )
        if retry_engaged:
            for _ in range(retry_policy.max_retries):
                if retry_policy.interval_seconds:
                    ops.sleep(retry_policy.interval_seconds)
                if marker_visible_in(
                    ops.capture(request.target, landing_lines), request.marker
                ):
                    marker_observed = True
                    break
                ops.press_enter(request.target)
                enter_attempts += 1

        # Redmine #13166 / #13262: standard-rail turn-start verification. Marker observed + Enter
        # issued proves the sender pressed Enter, not that the receiver TUI submitted the prompt
        # and started a turn — a busy / redrawing composer can absorb the Enter and leave the
        # marker+body unsubmitted while the rail still reported `sent` / `ok` (the false-positive
        # delivery this fixes). Observe the receiver pane for post-Enter turn-start activity
        # (read-only; no re-typed marker+body, no re-issued Enter, no auto-resend). An unconfirmed
        # turn start dies with NO C-u rollback and NO re-send. The queue-enter rail is untouched.
        turn_start_lines: Optional[List[str]] = None
        if standard_rail:
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

        # Redmine #13292: additive, telemetry-only queue-enter turn-start observation under the
        # herdr backend. The queue-enter inject -> Enter -> Enter-only retry choreography above is
        # left BYTE-IDENTICAL; only AFTER it, and only for a herdr send, do we take a read-only
        # runtime-state snapshot. It never changes `status` / `reason` / `next_action_owner`
        # (they stay `sent` / `ok`|`queue_enter` / `receiver`) and never blocks the send. The tmux
        # backend and every non-queue-enter rail are untouched (`queue_enter_observation` stays
        # `None`).
        queue_enter_observation: Optional[dict] = None
        if request.herdr_send and request.mode == MODE_QUEUE_ENTER:
            snapshot = ops.observe_queue_enter_turn_start(request.target)
            if snapshot is not None:  # always installed under herdr; skip defensively if not
                queue_enter_observation = snapshot.to_telemetry_dict()
                # Reuse the additive `turn_start_lines` record channel (appended, never overrides
                # `next_action`); the queue-enter renderer labels itself telemetry-only.
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
            if retry_engaged
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
    "TmuxTransportRailRequest",
    "TmuxTransportRailOps",
    "TmuxTransportRailUseCase",
    "LiveTmuxTransportRailOps",
    "run_tmux_transport_rail",
)
