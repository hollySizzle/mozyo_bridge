"""Fail-closed ``sublane dispatch-worker`` drive (#12988, hardened by #13846).

IO stays behind :class:`WorkerDispatchOps`; the use case requires current worker
authority plus a causally-bound turn-start and never equates transport ACK with
worker uptake.  It does not close, relaunch, or infer task completion.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (
    LiveSublaneActuatorOps,
    resolve_dispatch_admission_args,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (
    WorkflowProviderUnresolved,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (
    ACTUATE_BLOCKED,
    ACTUATE_EXECUTED,
    ACTUATE_READY,
    DISPATCH_NOT_ATTEMPTED,
    DISPATCH_WORKER_DISPATCHED,
    REASON_ANCHOR_REQUIRED,
    REASON_FILL_STOP,
    REASON_LANE_MISMATCH,
    REASON_MISSING_IDENTITY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_dispatch_admission import (
    evaluate_dispatch_admission,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    FillDecisionInputs,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    SublaneLaneView,
    redact_worktree_paths,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (
    ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,
    REASON_FOREIGN_SENDER,
    REASON_LANE_NOT_RESOLVED,
    REASON_LANE_PANE_MISSING,
    REASON_WORKER_DISPATCH_FAILED,
    REASON_WORKER_TURN_START_UNCONFIRMED,
    TURN_START_STARTED,
    WORKER_DISPATCH_DELIVERY_FAILED,
    WORKER_DISPATCH_TURN_START_UNCONFIRMED,
    SenderDispatchAdmission,
    WorkerDispatchAdmission,
    WorkerDispatchAdmissionFacts,
    WorkerDispatchOutcome,
    WorkerDispatchRequest,
    lane_identity_matches,
    render_worker_dispatch_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatch_send import (  # noqa: E501,F401
    # Re-exported so `_worker_dispatcher._drive_worker_send_argv` / `._worker_dispatch_argv`
    # / `._replayable_command` stay the established call + monkeypatch seams after the
    # Redmine #14192 leaf carve (line-cap). Callers reference these module globals.
    _drive_worker_send_argv,
    _replayable_command,
    _worker_dispatch_argv,
)


# Worker readiness wait tuning (#13301); #13846 now treats loss as a hard fence.

DEFAULT_WORKER_READY_PROBES = 20
DEFAULT_WORKER_READY_INTERVAL_SECONDS = 0.5
#: How many rendered lines the live readiness probe captures from the worker pane.
WORKER_READY_CAPTURE_LINES = 40


# ---------------------------------------------------------------------------
# Injected drive operations port.
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkerDispatchOps(Protocol):
    """Every side effect the ack drive needs, injected so tests drive fakes.

    ``read_lane`` resolves the lane's :class:`SublaneLaneView` from the live
    pane inventory (the same read-back the #12973 actuator confirms lanes
    with). ``probe_worker_ready`` is the #13301 non-fatal readiness snapshot of
    the same-lane worker pane. ``dispatch_to_worker`` routes the governed
    same-lane ``implementation_request`` to the worker pane and returns its exit
    code — the delivery-ACK measurement. There is intentionally no pane mutation,
    no retire / kill method, and no Redmine IO here.
    """

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]: ...

    def observe_worker_dispatch_admission(
        self, *, lane: SublaneLaneView, request: WorkerDispatchRequest
    ) -> WorkerDispatchAdmission: ...

    def probe_worker_ready(self, worker_pane: str) -> bool:
        """One non-fatal readiness snapshot of the same-lane worker pane (#13301).

        ``True`` when the worker TUI is observed booted and rendered (its Claude
        foreground process is up and the pane has drawn content), so a queue-enter
        forward lands on a live composer rather than vanishing into a still-booting
        one. Any read failure (pane gone, not-yet-agent process, blank capture)
        returns ``False`` — the caller polls this until ready or the bounded window
        elapses and never treats a probe failure as fatal.
        """
        ...

    def dispatch_to_worker(
        self,
        *,
        issue: str,
        journal: str,
        worker_pane: str,
        lane_label: str,
        gateway_callback_target: Optional[str],
        target_repo: str,
        allow_direct_worker: bool = False,
    ) -> int: ...

    def dispatch_to_worker_turn_start(
        self,
        *,
        issue: str,
        journal: str,
        worker_pane: str,
        lane_label: str,
        gateway_callback_target: Optional[str],
        target_repo: str,
        worker_assigned_name: str,
        allow_direct_worker: bool = False,
    ) -> tuple[int, str, bool]:
        """``(delivery_ack_rc, turn_start_token, known_not_sent)`` (Redmine #14192).

        ``known_not_sent`` is ``True`` only when a non-zero send's inner rail PROVED a
        pre-injection zero-send (a ``gateway_route_blocked`` / ``reader_upgrade_required``
        gate), so the caller cancels the exact fence key instead of poisoning it to the
        reconcile-only ``uncertain`` terminal. Every other non-zero outcome is ``False``.
        """
        ...

    def reserve_worker_dispatch(
        self, *, admission: WorkerDispatchAdmission, request: WorkerDispatchRequest
    ) -> tuple[bool, str]: ...

    def complete_worker_dispatch(
        self,
        *,
        admission: WorkerDispatchAdmission,
        request: WorkerDispatchRequest,
        delivered: bool,
        detail: str,
        known_not_sent: bool = False,
    ) -> bool: ...


@dataclass(frozen=True)
class LiveWorkerDispatchOps:
    """Live adapter composing the real same-lane forwarding primitives.

    Lane read-back delegates to the #12973 :class:`LiveSublaneActuatorOps`
    inventory fold (exact repo-root match, unambiguous-basename fallback, with
    identity still validated by the use case). The worker send drives the
    *existing CLI contract* the gateway already runs by hand — ``handoff send
    --to claude ... --mode queue-enter`` — through the composed argument parser,
    so every routing gate (gateway-route, cross-lane, ``--target-repo``
    identity) runs exactly as if typed.
    """

    repo_root: Path

    def _actuator_ops(self) -> LiveSublaneActuatorOps:
        return LiveSublaneActuatorOps(repo_root=self.repo_root)

    def worker_provider(self) -> str:
        """The runtime provider bound to the implementer (worker) role (Redmine #13569).

        Resolved from the repo-local ``RoleProviderBinding`` (default ``claude``,
        byte-identical) so the forward keys on the role, not a literal — a rebound worker
        provider moves the ``--to`` receiver and the readiness probe with no source edit.
        An unbound worker role raises :class:`WorkflowProviderUnresolved`, which the drive
        turns into a fail-closed zero-send.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            resolve_worker_provider,
        )

        return resolve_worker_provider(str(self.repo_root))

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]:
        return self._actuator_ops().read_lane(worktree_path)

    def observe_worker_dispatch_admission(
        self, *, lane: SublaneLaneView, request: WorkerDispatchRequest
    ) -> WorkerDispatchAdmission:
        # tmux has no lifecycle-generation + startup-attestation join equivalent.
        # Locator/process presence alone is not authority to inject (#13846).
        facts = WorkerDispatchAdmissionFacts(
            lifecycle_current=False,
            anchor_current=False,
            identity_attested=False,
            action_binding_current=False,
            slot_state="unknown",
            locator_present=bool(lane.worker_pane),
            receiver_state="unknown",
            generation_binding_current=False,
            worker_locator=lane.worker_pane,
        )
        return WorkerDispatchAdmission(
            ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,
            "tmux receiver lacks the generation-bound authority required for dispatch",
            facts,
        )

    def probe_worker_ready(self, worker_pane: str) -> bool:
        # #13301: one non-fatal readiness snapshot — the worker's foreground process
        # is the Claude TUI (strong per-receiver identity, the same check the
        # queue-enter rail uses) AND the pane has rendered content (a booted TUI has
        # drawn its UI; a blank capture is a pane still coming up). Any read failure
        # (pane resolve / capture raising, incl. the pane_resolver `die()` ==
        # SystemExit) is treated as "not ready yet", never fatal — the caller polls
        # this on a bounded window. Mirrors LiveSublaneActuatorOps.probe_gateway_ready
        # (#13293) with the worker's `claude` provider.
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (  # noqa: E501
            is_receiver_agent_process,
            pane_info,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (  # noqa: E501
            capture_pane,
        )

        try:
            worker_provider = self.worker_provider()
            info = pane_info(worker_pane)
            if not is_receiver_agent_process(info.get("command", ""), worker_provider):
                return False
            rendered = capture_pane(worker_pane, WORKER_READY_CAPTURE_LINES)
        except (SystemExit, Exception):  # noqa: BLE001 — a probe never fails the drive.
            return False
        return bool(rendered.strip())

    def dispatch_to_worker(
        self,
        *,
        issue: str,
        journal: str,
        worker_pane: str,
        lane_label: str,
        gateway_callback_target: Optional[str],
        target_repo: str,
        allow_direct_worker: bool = False,
    ) -> int:
        argv = _worker_dispatch_argv(
            issue=issue,
            journal=journal,
            worker_pane=worker_pane,
            lane_label=lane_label,
            gateway_callback_target=gateway_callback_target,
            target_repo=target_repo,
            allow_direct_worker=allow_direct_worker,
            worker_provider=self.worker_provider(),
        )
        rc, _known_not_sent = _drive_worker_send_argv(argv)
        return rc

    def dispatch_to_worker_turn_start(self, **kwargs) -> tuple[int, str, bool]:
        worker_assigned_name = kwargs.pop("worker_assigned_name", None)
        del worker_assigned_name
        # tmux never reserves the generation-bound outbox fence (its
        # `complete_worker_dispatch` is a no-op), so `known_not_sent` is inert here.
        return self.dispatch_to_worker(**kwargs), "unknown", False

    def reserve_worker_dispatch(self, **kwargs) -> tuple[bool, str]:
        return False, "tmux dispatch has no generation-bound outbox authority"

    def complete_worker_dispatch(self, **kwargs) -> bool:
        return False


# ---------------------------------------------------------------------------
# Use case: fail-closed ack drive over the injected port.
# ---------------------------------------------------------------------------


@dataclass
class WorkerDispatchUseCase:
    """Drive the generation-bound, exactly-once same-lane worker transfer."""

    ops: WorkerDispatchOps
    # #13301 pre-dispatch worker readiness wait (injectable for tests). ``probes<=0``
    # disables the wait (back-compat immediate forward; ``worker_ready`` stays None).
    worker_ready_probes: int = DEFAULT_WORKER_READY_PROBES
    worker_ready_interval_seconds: float = DEFAULT_WORKER_READY_INTERVAL_SECONDS
    sleep: Callable[[float], None] = field(default=time.sleep)

    def _wait_worker_ready(self, worker_pane: Optional[str]) -> Optional[bool]:
        """Poll a bounded readiness window; ``False`` is a #13846 send fence."""
        probes = self.worker_ready_probes
        if probes <= 0 or not worker_pane:
            return None
        for attempt in range(probes):
            if self.ops.probe_worker_ready(worker_pane):
                return True
            if attempt + 1 < probes:
                self.sleep(self.worker_ready_interval_seconds)
        return False

    def _command_pins(self) -> dict:
        """Backend-specific replay-authority pins."""
        getter = getattr(self.ops, "command_authority_pins", None)
        return getter() if callable(getter) else {}

    def _display_worker_provider(self) -> str:
        """Binding-resolved provider for the display/replay command."""
        getter = getattr(self.ops, "worker_provider", None)
        if not callable(getter):
            return "claude"
        try:
            return getter()
        except WorkflowProviderUnresolved:
            return "claude"

    def _sender_admission(
        self,
        lane: SublaneLaneView,
        request: WorkerDispatchRequest,
        *,
        allow_direct_worker: bool,
    ) -> Optional[SenderDispatchAdmission]:
        """Consult the backend's pre-reserve sender-identity preflight (#14192).

        An OPTIONAL port capability (resolved by name, like ``command_authority_pins``):
        the herdr adapter verifies the sender IS this lane's current same-lane gateway
        before any outbox reserve, so a coordinator / foreign / cross-lane sender fails
        closed with zero write and zero send. ``None`` (the tmux adapter, which does not
        provide it) skips the preflight and keeps the pre-#14192 flow byte-for-byte — the
        tmux inner send still runs its own gateway-route gate. An unexpected raise fails
        closed (a not-admitted verdict), never silently admits.
        """
        gate = getattr(self.ops, "observe_sender_admission", None)
        if not callable(gate):
            return None
        try:
            return gate(
                lane=lane, request=request, allow_direct_worker=allow_direct_worker
            )
        except Exception as exc:  # noqa: BLE001 - unreadable sender authority fails closed
            return SenderDispatchAdmission(
                admitted=False,
                reason=f"sender dispatch authority could not be observed: {exc}",
                detail_token="sender_authority_unreadable",
            )

    def _observe_admission(
        self, lane: SublaneLaneView, request: WorkerDispatchRequest
    ) -> WorkerDispatchAdmission:
        try:
            return self.ops.observe_worker_dispatch_admission(
                lane=lane, request=request
            )
        except Exception as exc:  # noqa: BLE001 - unreadable authority is conflict
            facts = WorkerDispatchAdmissionFacts(
                False, False, False, False, "unknown", bool(lane.worker_pane), "unknown"
            )
            return WorkerDispatchAdmission(
                ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,
                f"worker dispatch authority could not be observed: {exc}",
                facts,
            )

    def run(
        self,
        request: WorkerDispatchRequest,
        *,
        execute: bool,
        target_repo: str = "auto",
        fill_inputs: Optional[FillDecisionInputs] = None,
        override_fill_stop: Optional[str] = None,
        allow_direct_worker: bool = False,
    ) -> WorkerDispatchOutcome:
        # 1. Fail closed on missing identity before any probe.
        missing = request.missing_fields()
        if missing:
            return self._blocked(
                request,
                reason="required worker-dispatch identity fields are missing; "
                "refusing to drive a transfer against an incomplete target",
                reasons=(REASON_MISSING_IDENTITY,)
                + tuple(f"missing_field:{name}" for name in missing),
                target_repo=target_repo,
                execute=execute,
            )

        # 2. Anchor requirement: a live worker send needs a durable journal id
        # (worker dispatch is never unanchored — same contract as the #12973
        # actuator's dispatch step).
        anchor = (request.journal or "").strip()
        if execute and not anchor:
            return self._blocked(
                request,
                reason="a live worker dispatch requires a durable-anchor "
                "journal id (--journal); refusing to forward an unanchored "
                "implementation_request",
                reasons=(REASON_ANCHOR_REQUIRED,),
                target_repo=target_repo,
                execute=execute,
            )

        # 2b. Dispatch admission gate (#13290, execute path only): consult the
        # caller-supplied fill decision (the single #12855 authority) and fail closed
        # on a concrete stop unless an explicit override reason is supplied. When no
        # fill context is supplied the gate is not armed and this is a no-op, keeping
        # the #12988 ack-drive contract byte-for-byte back-compatible. A dry-run never
        # consults the gate (it performs no send to gate).
        fill_decision_token: Optional[str] = None
        fill_override_reason: Optional[str] = None
        if execute:
            admission = evaluate_dispatch_admission(
                fill_inputs, override_reason=override_fill_stop
            )
            if admission.is_blocked:
                return self._blocked(
                    request,
                    reason=admission.reason,
                    reasons=(REASON_FILL_STOP,)
                    + ((admission.fill_decision,) if admission.fill_decision else ()),
                    target_repo=target_repo,
                    execute=execute,
                    fill_decision=admission.fill_decision,
                )
            fill_decision_token = admission.fill_decision
            fill_override_reason = admission.override_reason

        # 3. Resolve the live lane for the worktree; no lane -> fail closed.
        lane = self.ops.read_lane(request.worktree_path)
        if lane is None:
            return self._blocked(
                request,
                reason="no live lane resolves for this worktree in the pane "
                "inventory; nothing to dispatch to (create/adopt the lane "
                "first with `sublane create --execute`)",
                reasons=(REASON_LANE_NOT_RESOLVED,),
                target_repo=target_repo,
                execute=execute,
                fill_decision=fill_decision_token,
                fill_override_reason=fill_override_reason,
            )

        # 4. Identity guard (j#70250): never forward #<issue> to a lane whose
        # label / issue does not match the request.
        if not lane_identity_matches(
            lane, issue=request.issue, lane_label=request.lane_label
        ):
            return self._blocked(
                request,
                reason=f"resolved lane identity (label={lane.lane_label!r} "
                f"issue={lane.issue!r}) does not match the requested lane "
                f"(label={request.lane_label!r} issue={request.issue!r}); "
                "fail-closed before any worker send",
                reasons=(REASON_LANE_MISMATCH,),
                target_repo=target_repo,
                execute=execute,
                gateway_pane=lane.gateway_pane,
                worker_pane=lane.worker_pane,
                fill_decision=fill_decision_token,
                fill_override_reason=fill_override_reason,
            )

        # #13846: classify the current lifecycle generation, startup attestation,
        # receiver state and prior exact delivery before requiring a locator.  This
        # preserves authoritative terminal absence as a typed stale-recovery result.
        dispatch_admission = self._observe_admission(lane, request)
        if not dispatch_admission.is_healthy:
            return self._blocked(
                request,
                reason=dispatch_admission.reason,
                reasons=(dispatch_admission.decision,),
                target_repo=target_repo,
                execute=execute,
                gateway_pane=lane.gateway_pane,
                worker_pane=lane.worker_pane,
                fill_decision=fill_decision_token,
                fill_override_reason=fill_override_reason,
                dispatch_admission=dispatch_admission,
            )

        # 5. Both panes must be live: the worker is the transfer target and the
        # gateway is the worker's recorded same-lane callback address.
        if not lane.worker_pane or not lane.gateway_pane:
            missing_pane = "worker" if not lane.worker_pane else "gateway"
            return self._blocked(
                request,
                reason=f"the resolved lane has no live {missing_pane} pane; "
                "refusing to drive a transfer the lane cannot receive / "
                "call back on",
                reasons=(REASON_LANE_PANE_MISSING,),
                target_repo=target_repo,
                execute=execute,
                gateway_pane=lane.gateway_pane,
                worker_pane=lane.worker_pane,
                fill_decision=fill_decision_token,
                fill_override_reason=fill_override_reason,
            )

        # 5b. Sender-identity preflight (#14192): verify the sender IS this lane's
        # current same-lane gateway BEFORE any outbox reserve, and on BOTH dry-run and
        # execute so their verdicts match (Acceptance #1/#2). A coordinator / foreign /
        # cross-lane sender fails closed here with zero fence write and zero send, so the
        # inner rail's `gateway_route_blocked` can never reserve-then-poison the exact
        # key (Acceptance #5). The tmux adapter omits the capability -> preflight skipped,
        # byte-for-byte the pre-#14192 flow (the tmux inner send keeps its own route gate).
        sender_admission = self._sender_admission(
            lane, request, allow_direct_worker=allow_direct_worker
        )
        if sender_admission is not None and not sender_admission.admitted:
            return self._blocked(
                request,
                reason=sender_admission.reason,
                reasons=(REASON_FOREIGN_SENDER,)
                + (
                    (sender_admission.detail_token,)
                    if sender_admission.detail_token
                    else ()
                ),
                target_repo=target_repo,
                execute=execute,
                gateway_pane=lane.gateway_pane,
                worker_pane=lane.worker_pane,
                fill_decision=fill_decision_token,
                fill_override_reason=fill_override_reason,
            )

        pins = self._command_pins()
        command = _replayable_command(
            issue=request.issue,
            journal=request.journal,
            worker_pane=lane.worker_pane,
            lane_label=request.lane_label,
            gateway_callback_target=lane.gateway_pane,
            target_repo=target_repo,
            allow_direct_worker=allow_direct_worker,
            target_lane=pins.get("target_lane"),
            repo_root=pins.get("repo_root"),
            worker_provider=self._display_worker_provider(),
        )

        # 6. Dry-run: preview the resolved transfer; perform nothing.
        if not execute:
            return WorkerDispatchOutcome(
                status=ACTUATE_READY,
                execute=False,
                reason="lane resolved and identity-confirmed; the same-lane "
                "worker transfer would be driven (dry-run; nothing sent)",
                issue=request.issue,
                lane_label=request.lane_label,
                worktree_path=request.worktree_path,
                gateway_pane=lane.gateway_pane,
                worker_pane=lane.worker_pane,
                dispatch_target=lane.worker_pane,
                dispatch_result=DISPATCH_NOT_ATTEMPTED,
                durable_anchor=(request.journal or None),
                command=command,
                allow_direct_worker=allow_direct_worker,
                admission_decision=dispatch_admission.decision,
                admission_reason=dispatch_admission.reason,
                lane_generation=dispatch_admission.facts.lane_generation,
                worker_assigned_name=dispatch_admission.facts.worker_assigned_name,
                receiver_state=dispatch_admission.facts.receiver_state,
            )

        # 6b. Bounded readiness observation; an unconfirmed receiver is a zero-send.
        worker_ready = self._wait_worker_ready(lane.worker_pane)

        # Re-observe immediately before injection.  A process/action completion
        # between probe and act invalidates the first observation; never send on it.
        action_admission = self._observe_admission(lane, request)
        if worker_ready is False or action_admission != dispatch_admission:
            return self._blocked(
                request,
                reason="worker authority changed or readiness disappeared before injection",
                reasons=(ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,),
                target_repo=target_repo,
                execute=True,
                gateway_pane=lane.gateway_pane,
                worker_pane=lane.worker_pane,
                fill_decision=fill_decision_token,
                fill_override_reason=fill_override_reason,
                dispatch_admission=action_admission,
                worker_ready=worker_ready,
            )

        reserved, reserve_detail = self.ops.reserve_worker_dispatch(
            admission=action_admission, request=request
        )
        if not reserved:
            conflict = WorkerDispatchAdmission(
                ADMISSION_WORKER_LIVENESS_AUTHORITY_CONFLICT,
                f"dispatch outbox reservation refused: {reserve_detail}",
                action_admission.facts,
            )
            return self._blocked(
                request,
                reason=conflict.reason,
                reasons=(conflict.decision,),
                target_repo=target_repo,
                execute=True,
                gateway_pane=lane.gateway_pane,
                worker_pane=lane.worker_pane,
                dispatch_admission=conflict,
                worker_ready=worker_ready,
            )

        # 7. Live drive: retain transport ACK and turn-start as separate facts.
        # ``known_not_sent`` (#14192) defaults False so the except legs — whose fate is
        # unknown — stay uncertain; only a returned proven pre-injection zero-send sets it.
        known_not_sent = False
        try:
            rc, turn_start, known_not_sent = self.ops.dispatch_to_worker_turn_start(
                issue=request.issue,
                journal=anchor,
                worker_pane=lane.worker_pane,
                lane_label=request.lane_label,
                gateway_callback_target=lane.gateway_pane,
                target_repo=target_repo,
                worker_assigned_name=(
                    dispatch_admission.facts.worker_assigned_name or ""
                ),
                allow_direct_worker=allow_direct_worker,
            )
        except SystemExit as exc:
            # Review j#71597: the composed handoff CLI fails closed through
            # `die()` == SystemExit, which `except Exception` never catches.
            # A port implementation that leaks it must still become a
            # `delivery_failed` outcome, never a process exit that skips the
            # durable fail-closed record.
            code = exc.code
            rc = code if isinstance(code, int) and code != 0 else 1
            turn_start = "not_started"
            detail = f"worker handoff send exited: SystemExit({exc.code})"
        except Exception as exc:  # noqa: BLE001 — fail-closed on any send failure.
            rc = 1
            turn_start = "not_started"
            detail = f"worker handoff send raised: {exc}"
        else:
            detail = f"worker handoff send to {lane.worker_pane} exit={rc}"
        # #14192: a proven pre-injection zero-send (`known_not_sent`) cancels the exact
        # fence key (never replayed) instead of poisoning it to the reconcile-only
        # `uncertain` terminal; every other non-delivered outcome stays uncertain.
        outcome_durable = self.ops.complete_worker_dispatch(
            admission=dispatch_admission,
            request=request,
            delivered=(rc == 0 and turn_start == TURN_START_STARTED),
            detail=detail,
            known_not_sent=known_not_sent,
        )
        if not outcome_durable:
            turn_start = "unknown"
        if rc != 0:
            return WorkerDispatchOutcome(
                status=ACTUATE_BLOCKED,
                execute=True,
                reason="same-lane worker dispatch did not ack "
                f"({detail}); the lane's recorded dispatch state stays "
                "`gateway_notified` (fail-closed)",
                issue=request.issue,
                lane_label=request.lane_label,
                worktree_path=request.worktree_path,
                gateway_pane=lane.gateway_pane,
                worker_pane=lane.worker_pane,
                dispatch_target=lane.worker_pane,
                dispatch_result=WORKER_DISPATCH_DELIVERY_FAILED,
                durable_anchor=(request.journal or None),
                command=command,
                blocked_reasons=(REASON_WORKER_DISPATCH_FAILED,),
                fill_decision=fill_decision_token,
                fill_override_reason=fill_override_reason,
                worker_ready=worker_ready,
                allow_direct_worker=allow_direct_worker,
                admission_decision=dispatch_admission.decision,
                admission_reason=dispatch_admission.reason,
                lane_generation=dispatch_admission.facts.lane_generation,
                worker_assigned_name=dispatch_admission.facts.worker_assigned_name,
                receiver_state=dispatch_admission.facts.receiver_state,
                turn_start_outcome=turn_start,
            )
        if turn_start != TURN_START_STARTED:
            return WorkerDispatchOutcome(
                status=ACTUATE_BLOCKED,
                execute=True,
                reason="transport acknowledged queue entry, but no causally-bound "
                "worker turn-start was observed; refusing ACK-only promotion",
                issue=request.issue,
                lane_label=request.lane_label,
                worktree_path=request.worktree_path,
                gateway_pane=lane.gateway_pane,
                worker_pane=lane.worker_pane,
                dispatch_target=lane.worker_pane,
                dispatch_result=WORKER_DISPATCH_TURN_START_UNCONFIRMED,
                durable_anchor=(request.journal or None),
                command=command,
                blocked_reasons=(REASON_WORKER_TURN_START_UNCONFIRMED,),
                fill_decision=fill_decision_token,
                fill_override_reason=fill_override_reason,
                worker_ready=worker_ready,
                allow_direct_worker=allow_direct_worker,
                admission_decision=dispatch_admission.decision,
                admission_reason=dispatch_admission.reason,
                lane_generation=dispatch_admission.facts.lane_generation,
                worker_assigned_name=dispatch_admission.facts.worker_assigned_name,
                receiver_state=dispatch_admission.facts.receiver_state,
                turn_start_outcome=turn_start,
            )
        override_suffix = (
            f" — fill-decision stop overridden (reason: {fill_override_reason})"
            if fill_override_reason
            else ""
        )
        return WorkerDispatchOutcome(
            status=ACTUATE_EXECUTED,
            execute=True,
            reason="same-lane worker transfer delivery-acked and turn-start observed "
            f"({detail}); worker_dispatch_confirmed=true (not worker progress or "
            "completion)" + override_suffix,
            issue=request.issue,
            lane_label=request.lane_label,
            worktree_path=request.worktree_path,
            gateway_pane=lane.gateway_pane,
            worker_pane=lane.worker_pane,
            dispatch_target=lane.worker_pane,
            dispatch_result=DISPATCH_WORKER_DISPATCHED,
            durable_anchor=(request.journal or None),
            command=command,
            fill_decision=fill_decision_token,
            fill_override_reason=fill_override_reason,
            worker_ready=worker_ready,
            allow_direct_worker=allow_direct_worker,
            admission_decision=dispatch_admission.decision,
            admission_reason=dispatch_admission.reason,
            lane_generation=dispatch_admission.facts.lane_generation,
            worker_assigned_name=dispatch_admission.facts.worker_assigned_name,
            receiver_state=dispatch_admission.facts.receiver_state,
            turn_start_outcome=turn_start,
        )

    # -- helpers ------------------------------------------------------------

    def _blocked(
        self,
        request: WorkerDispatchRequest,
        *,
        reason: str,
        reasons: tuple[str, ...],
        target_repo: str,
        execute: bool,
        gateway_pane: Optional[str] = None,
        worker_pane: Optional[str] = None,
        fill_decision: Optional[str] = None,
        fill_override_reason: Optional[str] = None,
        dispatch_admission: Optional[WorkerDispatchAdmission] = None,
        worker_ready: Optional[bool] = None,
    ) -> WorkerDispatchOutcome:
        return WorkerDispatchOutcome(
            status=ACTUATE_BLOCKED,
            execute=execute,
            reason=reason,
            issue=request.issue,
            lane_label=request.lane_label,
            worktree_path=request.worktree_path or None,
            gateway_pane=gateway_pane,
            worker_pane=worker_pane,
            dispatch_target=None,
            dispatch_result=DISPATCH_NOT_ATTEMPTED,
            durable_anchor=(request.journal or None),
            command=_replayable_command(
                issue=request.issue,
                journal=request.journal,
                worker_pane=worker_pane,
                lane_label=request.lane_label,
                gateway_callback_target=gateway_pane,
                target_repo=target_repo,
                worker_provider=self._display_worker_provider(),
                **{
                    k: v
                    for k, v in self._command_pins().items()
                    if k in ("target_lane", "repo_root")
                },
            ),
            blocked_reasons=reasons,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
            worker_ready=worker_ready,
            admission_decision=(
                dispatch_admission.decision if dispatch_admission else None
            ),
            admission_reason=(dispatch_admission.reason if dispatch_admission else None),
            lane_generation=(
                dispatch_admission.facts.lane_generation if dispatch_admission else None
            ),
            worker_assigned_name=(
                dispatch_admission.facts.worker_assigned_name
                if dispatch_admission
                else None
            ),
            receiver_state=(
                dispatch_admission.facts.receiver_state if dispatch_admission else None
            ),
            retry_allowed=(dispatch_admission.retry_allowed if dispatch_admission else False),
        )


# ---------------------------------------------------------------------------
# Text rendering (pure) + thin CLI handler.
# ---------------------------------------------------------------------------


def format_worker_dispatch_text(outcome: WorkerDispatchOutcome) -> str:
    header = "sublane dispatch-worker"
    if not outcome.execute:
        header += " (dry-run)"
    lines = [f"{header}: {outcome.status}", f"  reason: {outcome.reason}"]
    if outcome.gateway_pane or outcome.worker_pane:
        lines.append(
            f"  lane: gateway={outcome.gateway_pane or '-'} "
            f"worker={outcome.worker_pane or '-'}"
        )
    lines.append(
        f"  dispatch: {outcome.dispatch_result} "
        f"(worker_dispatch_confirmed={str(outcome.worker_dispatch_confirmed).lower()})"
    )
    if outcome.worker_ready is not None:
        lines.append(f"  worker_ready: {str(outcome.worker_ready).lower()}")
    if outcome.allow_direct_worker:
        lines.append("  route: --allow-direct-worker (gateway_route_exception)")
    if outcome.is_blocked:
        lines.append("  -> blocked: " + ", ".join(outcome.blocked_reasons))
    if outcome.command:
        lines.append(f"  $ {outcome.command}")
    lines.append("  durable record:")
    for jline in render_worker_dispatch_journal(outcome).splitlines():
        lines.append(f"    {jline}")
    # #13368: keep the pasteable text free of the host-local absolute worktree path
    # (the machine `worktree_path` stays in the `--json` payload). The same-lane send
    # command uses `--target-repo auto`, so this is defence-in-depth against a future
    # command shape carrying the path.
    return redact_worktree_paths("\n".join(lines), outcome.worktree_path)


def _resolve_worker_dispatch_ops(
    *, repo_root: Path, request: WorkerDispatchRequest
) -> WorkerDispatchOps:
    """Pick the worker-dispatch ack-drive adapter for the configured terminal backend.

    Redmine #13357 (the worker-dispatch leg of the #13331 option A migration):
    ``backend: herdr`` → the per-lane-workspace
    :class:`~...application.sublane_worker_dispatch_herdr_ops.HerdrWorkerDispatchOps`,
    carrying the requested lane identity so its inventory read-back projects the lane
    (the same selector shape as ``sublane create``'s ``_resolve_sublane_ops``). Anything
    else — including an absent / broken repo-local config — keeps the tmux
    :class:`LiveWorkerDispatchOps`, byte-for-byte (#13320 posture).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_backend_is_herdr,
    )

    if repo_backend_is_herdr(repo_root):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_dispatch_herdr_ops import (  # noqa: E501
            HerdrWorkerDispatchOps,
        )

        return HerdrWorkerDispatchOps(
            repo_root=repo_root,
            lane_label=request.lane_label,
            issue=request.issue,
        )
    return LiveWorkerDispatchOps(repo_root=repo_root)


def cmd_sublane_dispatch_worker(args: argparse.Namespace) -> int:
    """``sublane dispatch-worker`` handler: dry-run preview by default; ``--execute``
    drives the live same-lane worker transfer (``--dry-run`` wins when both are
    given, mirroring ``sublane create``)."""
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    try:
        # The durable record should carry the resolved worktree path, not a
        # relative "."; resolution failure falls back to the given path.
        repo_root = repo_root.resolve()
    except (OSError, RuntimeError):
        pass
    execute = bool(getattr(args, "execute", False)) and not bool(
        getattr(args, "dry_run", False)
    )
    request = WorkerDispatchRequest(
        issue=getattr(args, "issue", "") or "",
        lane_label=getattr(args, "lane_label", "") or "",
        worktree_path=str(repo_root),
        journal=getattr(args, "journal", None),
    )
    fill_inputs, override_fill_stop = resolve_dispatch_admission_args(args)
    # #13301: convert the --worker-ready-timeout window into a bounded probe count
    # (<=0 disables the pre-forward readiness wait for back-compat / non-tmux runs),
    # mirroring the #13293 gateway --gateway-ready-timeout conversion.
    ready_timeout = float(getattr(args, "worker_ready_timeout", 10.0) or 0.0)
    interval = DEFAULT_WORKER_READY_INTERVAL_SECONDS
    ready_probes = 0 if ready_timeout <= 0 else max(1, round(ready_timeout / interval))
    use_case = WorkerDispatchUseCase(
        _resolve_worker_dispatch_ops(repo_root=repo_root, request=request),
        worker_ready_probes=ready_probes,
        worker_ready_interval_seconds=interval,
    )
    outcome = use_case.run(
        request,
        execute=execute,
        target_repo=getattr(args, "target_repo", None) or "auto",
        fill_inputs=fill_inputs,
        override_fill_stop=override_fill_stop,
        allow_direct_worker=bool(getattr(args, "allow_direct_worker", False)),
    )
    if getattr(args, "json", False):
        print(
            json.dumps(
                outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        print(format_worker_dispatch_text(outcome))
    return 1 if outcome.is_blocked else 0


__all__ = (
    "DEFAULT_WORKER_READY_PROBES",
    "DEFAULT_WORKER_READY_INTERVAL_SECONDS",
    "WORKER_READY_CAPTURE_LINES",
    "WorkerDispatchOps",
    "LiveWorkerDispatchOps",
    "WorkerDispatchUseCase",
    "format_worker_dispatch_text",
    "cmd_sublane_dispatch_worker",
)
