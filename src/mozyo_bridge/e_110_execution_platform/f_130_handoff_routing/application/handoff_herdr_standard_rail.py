"""Herdr event-driven ``--mode standard`` turn-start rail execution (Redmine #13729 tranche 3).

The ``orchestrate_handoff`` transport tail in ``application/commands.py`` historically
carried the herdr event-driven ``--mode standard`` rail inline (Redmine #13255): under
``terminal_transport.backend: herdr`` AND ``--mode standard`` the strict rail is driven by
the event-driven :class:`HerdrTurnStartRail` INSTEAD OF the common tmux body injection +
landing-marker gate + capture-based standard turn-start observation. The rail OWNS injection
(snapshot -> arm wait -> ``send_text(marker+body)`` -> ``send_keys(enter)`` -> collect
through the herdr transport port) and returns / dies without ever falling through to the
common tmux choreography below it.

This module carves that one coherent slice into an OOP-first application use case under
#12638 / #13729, aligning with the existing ``handoff_envelope_planner.py`` (#13729 tranche 2)
and the ``handoff_delivery_command.py`` delivery-rendering boundary, **without touching** the
rail domain (:class:`HerdrTurnStartRail`), the ``(status, reason)`` projection
(:func:`project_herdr_turn_start`), the delivery-record / ledger / persistence seams, the tmux
common rail, or the queue-enter rail (all out of this slice's scope):

- :class:`HerdrStandardRailRequest` is the frozen typed input — everything the rail's terminal
  outcome carries (the resolved envelope value objects, the ticketless payloads, the
  record-format / duplicate-lane diagnostics, the opt-in persistence + q-enter submit scalars).
- :class:`HerdrStandardRailOps` is the port for the *side-effecting* dependencies the slice
  needs from its environment (emit an outcome, record the #13296 ledger, persist the opt-in
  durable record, ``die``), so :meth:`HerdrStandardRailUseCase.execute` is exercisable with a
  synthetic fake port + a fake rail and no live herdr / tmux / Redmine.
- :class:`HerdrStandardRailUseCase` holds the slice body: drive the rail, project the wire
  outcome, assemble + emit + ledger the terminal outcome, and either persist + succeed (``sent``)
  or ``die`` with no C-u rollback and no re-send (every other rail outcome).
- :class:`LiveHerdrStandardRailOps` routes the ledger / persistence / ``die`` seams through the
  :mod:`commands` module *at call time* (the ``_record_herdr_send_ledger`` /
  ``_maybe_persist_delivery_record`` / ``die`` re-exports), so the existing herdr transport
  integration tests (which drive ``orchestrate_handoff`` for real through
  ``bind_runtime_transport`` + a fake herdr runner and patch the low-level ``commands.*`` seams)
  keep intercepting the side effects unchanged and no import cycle is introduced (``commands``
  imports this module at module load; this module imports ``commands`` only lazily inside the
  live adapter). The emit closure is the facade's per-call publishing emitter
  (``make_publishing_emitter``), injected through the constructor so publication stays a
  property of emitting (Redmine #13583 R3-F1).

The pure collaborators (:func:`make_outcome`, :func:`project_herdr_turn_start`,
:func:`turn_start_rail_record_lines`, :func:`submit_lines_for`) are imported and called
directly — they take no environment and are already unit-covered — so the port stays scoped to
the genuine side effects. This is a pure, behavior-preserving restructuring: the emitted
outcome, the ledger record, the persisted record, the exit code, and both ``die`` messages are
byte-identical to the original inline block.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, cast

from mozyo_bridge.application.handoff_delivery_command import submit_lines_for
from mozyo_bridge.application.turn_start_observation import project_herdr_turn_start
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    DeliveryOutcome,
    ExecutionRoot,
    NormalizedAnchor,
    Reason,
    Status,
    make_outcome,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    TurnStartResult,
    turn_start_rail_record_lines,
)


class TurnStartRailPort(Protocol):
    """Port: the herdr event-driven turn-start rail the slice drives.

    The live implementation is
    :class:`~...f_130_terminal_runtime_provider.domain.turn_start_rail.HerdrTurnStartRail`
    (stashed on ``commands.active_herdr_turn_start_rail`` by ``bind_runtime_transport``); it
    structurally satisfies this narrow port (its ``drive_turn_start`` also accepts an optional
    ``enter_keys`` keyword, which this slice never passes). Depending on the port instead of the
    concrete rail keeps the slice exercisable with a synthetic fake rail.
    """

    def drive_turn_start(self, target: str, text: str) -> TurnStartResult:
        """Inject ``text`` into ``target`` and confirm a turn started; never raises."""
        ...


#: The per-call publishing emitter injected by the facade (``make_publishing_emitter``):
#: ``emit(outcome, **emit_kwargs)`` — publishes then renders the delivery outcome.
PublishingEmitter = Callable[..., None]

#: The wire status the projection assigns to a confirmed turn start; the sole
#: outcome that persists the opt-in durable record and returns ``0``. Every other
#: projected status is a ``blocked`` terminal that emits + ledgers + dies.
_SENT = "sent"


@dataclass(frozen=True)
class HerdrStandardRailRequest:
    """The typed input for the herdr event-driven ``--mode standard`` rail slice.

    Every field is the value the original inline block read from an
    ``orchestrate_handoff`` local: ``target`` / ``marker`` / ``body`` drive the rail and the
    injected text (``f"{marker} {body}"``); the remaining fields are the terminal outcome
    context (the resolved envelope value objects + ticketless payloads), the record-format /
    duplicate-lane diagnostics, and the opt-in persistence + q-enter submit scalars. Frozen:
    the slice never mutates its input.
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


class HerdrStandardRailOps(Protocol):
    """Port: the side-effecting dependencies the herdr standard-rail slice needs.

    The pure collaborators (:func:`make_outcome`, :func:`project_herdr_turn_start`,
    :func:`turn_start_rail_record_lines`, :func:`submit_lines_for`) are NOT here — the use case
    calls them directly. Only the genuine side effects are ported so the slice is exercisable
    with a synthetic fake that records the calls.
    """

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
        """Emit (publish + render) the terminal delivery outcome."""
        ...

    def record_ledger(self, outcome: DeliveryOutcome) -> None:
        """Persist the #13296 herdr delivery-ledger entry for this outcome (#13300)."""
        ...

    def persist_delivery(
        self,
        outcome: DeliveryOutcome,
        *,
        persist_delivery: bool,
        duplicate_lane_panes: Optional[List[str]],
        record_format: str,
        turn_start_lines: Optional[List[str]],
    ) -> None:
        """Opt-in ``--persist-delivery`` durable persistence for a ``sent`` outcome."""
        ...

    def die(self, message: str) -> None:
        """Terminate the send with a non-zero exit and ``message`` (raises)."""
        ...


class HerdrStandardRailUseCase:
    """The herdr event-driven ``--mode standard`` rail slice.

    Drives the injected :class:`HerdrTurnStartRail`, projects the ``(status, reason)`` wire,
    assembles + emits + ledgers the terminal outcome, and either persists + returns ``0`` (a
    confirmed ``sent`` turn start) or dies with the marker+body typed at most once and only
    Enter sent — **no C-u rollback and no re-send** — on every other rail outcome. The control
    flow returns / dies without ever falling through (the caller returns this method's result).
    """

    def __init__(self, ops: HerdrStandardRailOps) -> None:
        self._ops = ops

    def execute(
        self, rail: Optional[TurnStartRailPort], request: HerdrStandardRailRequest
    ) -> int:
        if rail is None:  # defensive: the decorator always stashes it under herdr
            self._ops.die(
                "herdr backend selected for a --mode standard send but no turn-start "
                "rail was installed; refusing to fall back to the capture rail. "
                f"target={request.target}"
            )
            raise AssertionError("unreachable")
        turn_start = rail.drive_turn_start(request.target, f"{request.marker} {request.body}")
        status, reason = project_herdr_turn_start(turn_start)
        # Machine-readable turn-start telemetry (turn_start_outcome / snapshot_state /
        # wait_kind / enter_resends / reclassified_blocked) for EVERY rail outcome,
        # rendered redaction-safe by the rail's own record renderer and persisted on
        # the delivery record (auditor j#72602 decision 4).
        turn_start_lines = turn_start_rail_record_lines(turn_start)
        # Redmine #13255 j#72695: carry the SAME telemetry as a structured field on
        # the outcome so it lands in the JSON / persisted record (an auditor / the
        # future #12656 ledger reads this, not the reused `(status, reason)` wire
        # alone) AND so the delivery-record wording discriminates a herdr
        # `delivered_not_started` from the tmux/capture standard rail's
        # `turn_start_unconfirmed`.
        turn_start_telemetry = turn_start.to_telemetry_dict()
        # ``project_herdr_turn_start`` returns plain ``str`` from a total lookup over the rail's
        # closed vocabulary; every value is a valid ``Status`` / ``Reason`` member. The casts are
        # static-only (runtime no-op) — they re-narrow the projection to the wire Literals without
        # changing any value, exactly as the original inline block passed them positionally.
        outcome = make_outcome(
            status=cast(Status, status),
            reason=cast(Reason, reason),
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
            turn_start_outcome=turn_start_telemetry,
        )
        self._ops.emit(
            outcome,
            record_format=request.record_format,
            command=request.record_command,
            duplicate_lane_panes=request.duplicate_lane_panes or None,
            role_profile_contract=request.role_profile_contract,
            submit_lines=submit_lines_for(
                outcome,
                submit_intent=request.submit_intent,
                submit_delivery_id=request.submit_delivery_id,
            ),
            turn_start_lines=turn_start_lines,
        )
        # Redmine #13300: persist every event-rail outcome (incl. delivered-not-started)
        # before the sent/die branch, so no terminal event-rail path escapes the ledger.
        self._ops.record_ledger(outcome)
        if status == _SENT:
            self._ops.persist_delivery(
                outcome,
                persist_delivery=request.persist_delivery,
                duplicate_lane_panes=request.duplicate_lane_panes,
                record_format=request.record_format,
                turn_start_lines=turn_start_lines,
            )
            return 0
        self._ops.die(
            "handoff was routed through the herdr event-driven turn-start rail but "
            f"no turn start was confirmed (rail outcome {turn_start.outcome}); the "
            f"{request.receiver} receiver was not observed starting a turn. The marker+body "
            "was typed at most once and only Enter was sent (no C-u rollback, no "
            f"re-send). Read the receiver before re-issuing. target={request.target} "
            f"marker={request.marker}"
        )
        raise AssertionError("unreachable")


class LiveHerdrStandardRailOps:
    """Live :class:`HerdrStandardRailOps`.

    The ledger / persistence / ``die`` seams route through the :mod:`commands` module *at call
    time* (the historical ``_record_herdr_send_ledger`` / ``_maybe_persist_delivery_record`` /
    ``die`` re-exports), so a monkeypatched ``commands.*`` seam still intercepts and no import
    cycle is introduced. The emit closure is the facade's per-call publishing emitter, injected
    at construction so publication stays a property of emitting (Redmine #13583 R3-F1).
    """

    def __init__(self, emit: PublishingEmitter) -> None:
        self._emit = emit

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
        self._emit(
            outcome,
            record_format=record_format,
            command=command,
            duplicate_lane_panes=duplicate_lane_panes,
            role_profile_contract=role_profile_contract,
            submit_lines=submit_lines,
            turn_start_lines=turn_start_lines,
        )

    def record_ledger(self, outcome: DeliveryOutcome) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands._record_herdr_send_ledger(outcome)

    def persist_delivery(
        self,
        outcome: DeliveryOutcome,
        *,
        persist_delivery: bool,
        duplicate_lane_panes: Optional[List[str]],
        record_format: str,
        turn_start_lines: Optional[List[str]],
    ) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands._maybe_persist_delivery_record(
            outcome,
            persist_delivery=persist_delivery,
            duplicate_lane_panes=duplicate_lane_panes,
            record_format=record_format,
            turn_start_lines=turn_start_lines,
        )

    def die(self, message: str) -> None:
        from mozyo_bridge.application import commands as _commands

        _commands.die(message)


def run_herdr_standard_rail(
    rail: Optional[TurnStartRailPort],
    request: HerdrStandardRailRequest,
    *,
    emit: PublishingEmitter,
) -> int:
    """Live composition root: drive the herdr standard rail for the handoff facade.

    Constructs :class:`HerdrStandardRailUseCase` over :class:`LiveHerdrStandardRailOps` (the
    ledger / persistence / ``die`` seams routed through ``commands`` at call time) and runs the
    slice, exactly as the original inline block did. ``emit`` is the facade's per-call
    publishing emitter.
    """
    return HerdrStandardRailUseCase(LiveHerdrStandardRailOps(emit=emit)).execute(rail, request)


__all__ = (
    "TurnStartRailPort",
    "HerdrStandardRailRequest",
    "HerdrStandardRailOps",
    "HerdrStandardRailUseCase",
    "LiveHerdrStandardRailOps",
    "run_herdr_standard_rail",
)
