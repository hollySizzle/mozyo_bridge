"""`mozyo-bridge sublane dispatch-worker` ack-drive boundary (Redmine #12988).

#12986 left ``sublane create --execute`` honestly stopping at
``gateway_notified`` / ``worker_dispatch_confirmed=false``: a gateway send
exiting 0 proves gateway notification only. This module adds the follow-up
**worker-dispatch ack drive**: the lane's Codex gateway (the only sanctioned
same-lane forwarding actor) runs ``sublane dispatch-worker --execute`` to
forward the anchored ``implementation_request`` to its same-lane Claude worker
over the *existing* governed ``handoff send --to claude`` rail, and the drive
records the measured delivery ACK as :data:`DISPATCH_WORKER_DISPATCHED` /
``worker_dispatch_confirmed=true`` — or fails closed, keeping the lane's
recorded ``gateway_notified`` state untouched.

What this drive deliberately does NOT do:

- it never weakens a routing gate: the send is composed through the real CLI
  parser (the same fully-defaulted path the gateway would type by hand), so the
  #12918 gateway-route gate, the cross-session / cross-lane blocks, the
  ``--target-repo`` identity gate, and the queue-enter submit-complete rail all
  still apply, unhidden;
- it never claims more than a **delivery ACK**: ``worker_dispatched`` means the
  worker runtime received the anchored input, never that the worker progressed
  or completed (``vibes/docs/logics/ack-completion-receiver-state.md``); no
  completion detector is introduced;
- it never dispatches unanchored: a live send without a durable-anchor journal
  id fails closed (:data:`REASON_ANCHOR_REQUIRED`), mirroring the #12973
  actuator's dispatch step;
- it never targets a lane whose identity does not match the request
  (:func:`lane_identity_matches`, the j#70250 misdelivery guard).

OOP-first boundary (mirrors #12973 ``sublane_actuator``):
:class:`WorkerDispatchUseCase` holds the fail-closed decision flow and never
touches IO; the :class:`WorkerDispatchOps` port owns every side effect;
:class:`LiveWorkerDispatchOps` composes the real primitives (lane read-back via
the #12973 actuator's proven inventory fold + the ``handoff send`` CLI
contract); the typed :class:`WorkerDispatchOutcome` carries the machine-readable
payload; and the thin ``cmd_sublane_dispatch_worker`` handler owns stdout and
the exit code. The default UX is a side-effect-free dry-run preview; ``--execute``
is the opt-in live drive, exactly like the actuator surface.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (
    LiveSublaneActuatorOps,
    resolve_dispatch_admission_args,
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
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (
    REASON_LANE_NOT_RESOLVED,
    REASON_LANE_PANE_MISSING,
    REASON_WORKER_DISPATCH_FAILED,
    WORKER_DISPATCH_DELIVERY_FAILED,
    WorkerDispatchOutcome,
    WorkerDispatchRequest,
    lane_identity_matches,
    render_worker_dispatch_journal,
)


# ---------------------------------------------------------------------------
# Injected drive operations port.
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkerDispatchOps(Protocol):
    """Every side effect the ack drive needs, injected so tests drive fakes.

    ``read_lane`` resolves the lane's :class:`SublaneLaneView` from the live
    pane inventory (the same read-back the #12973 actuator confirms lanes
    with). ``dispatch_to_worker`` routes the governed same-lane
    ``implementation_request`` to the worker pane and returns its exit code —
    the delivery-ACK measurement. There is intentionally no pane mutation, no
    retire / kill method, and no Redmine IO here.
    """

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]: ...

    def dispatch_to_worker(
        self,
        *,
        issue: str,
        journal: str,
        worker_pane: str,
        lane_label: str,
        gateway_callback_target: Optional[str],
        target_repo: str,
    ) -> int: ...


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

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]:
        return self._actuator_ops().read_lane(worktree_path)

    def dispatch_to_worker(
        self,
        *,
        issue: str,
        journal: str,
        worker_pane: str,
        lane_label: str,
        gateway_callback_target: Optional[str],
        target_repo: str,
    ) -> int:
        argv = _worker_dispatch_argv(
            issue=issue,
            journal=journal,
            worker_pane=worker_pane,
            lane_label=lane_label,
            gateway_callback_target=gateway_callback_target,
            target_repo=target_repo,
        )
        from mozyo_bridge.application.cli import build_parser, normalize_paths

        # Review j#71597: the inner `handoff send` fails closed through
        # `die()` == `raise SystemExit`, which `except Exception` never
        # catches, and it emits its own delivery record to stdout. Both must
        # be contained here so the outer WorkerDispatchOutcome stays the
        # single fail-closed, machine-readable surface: run the composed
        # primitive under stdout capture and convert any SystemExit
        # (including an argparse usage error) to its exit code. The captured
        # inner record is surfaced to stderr on failure so the blocked send
        # stays diagnosable without polluting the outer `--json` stdout.
        inner_out = io.StringIO()
        try:
            with contextlib.redirect_stdout(inner_out):
                args = build_parser().parse_args(argv)
                args = normalize_paths(args)
                rc = int(args.func(args) or 0)
        except SystemExit as exc:
            # A SystemExit is always the *fail-closed* leg here (`die()` /
            # argparse usage error); the success leg returns an int. A
            # non-int / None exit code is never treated as a delivery ACK —
            # an ambiguous exit must not promote to `worker_dispatched`.
            code = exc.code
            rc = code if isinstance(code, int) and code != 0 else 1
        if rc != 0:
            captured = inner_out.getvalue().strip()
            if captured:
                print(
                    "worker handoff send (inner delivery record):\n" + captured,
                    file=sys.stderr,
                )
        return rc


def _worker_dispatch_argv(
    *,
    issue: str,
    journal: str,
    worker_pane: str,
    lane_label: str,
    gateway_callback_target: Optional[str],
    target_repo: str,
) -> list[str]:
    """The same-lane worker forward as the gateway would type it (pure).

    ``--role-profile implementation_worker`` binds the worker's effective
    instruction set; ``gateway_callback_target`` (the lane's own gateway pane)
    is the worker's same-lane callback address. The queue-enter rail keeps the
    dispatch submit-complete (#12207) with standard_target_admission covering
    the usually-inactive worker split (#12597).
    """
    argv = [
        "handoff",
        "send",
        "--to",
        "claude",
        "--source",
        "redmine",
        "--issue",
        issue,
        "--journal",
        journal,
        "--kind",
        "implementation_request",
        "--target",
        worker_pane,
        "--target-repo",
        target_repo,
        "--mode",
        "queue-enter",
        "--role-profile",
        "implementation_worker",
        "--profile-field",
        f"lane={lane_label}",
    ]
    if gateway_callback_target:
        argv += [
            "--profile-field",
            f"gateway_callback_target={gateway_callback_target}",
        ]
    return argv


def _replayable_command(
    *,
    issue: str,
    journal: Optional[str],
    worker_pane: Optional[str],
    lane_label: str,
    gateway_callback_target: Optional[str],
    target_repo: str,
) -> str:
    return "mozyo-bridge " + " ".join(
        _worker_dispatch_argv(
            issue=issue,
            journal=journal or "<journal>",
            worker_pane=worker_pane or "<worker-pane>",
            lane_label=lane_label,
            gateway_callback_target=gateway_callback_target,
            target_repo=target_repo,
        )
    )


# ---------------------------------------------------------------------------
# Use case: fail-closed ack drive over the injected port.
# ---------------------------------------------------------------------------


@dataclass
class WorkerDispatchUseCase:
    """Drive the fail-closed same-lane worker transfer over :class:`WorkerDispatchOps`.

    The flow stops at the first failure and reports the partial state: missing
    identity, missing durable anchor, an unresolved lane, a lane-identity
    mismatch (j#70250 guard), or a missing worker / gateway pane all block
    before any send; a non-zero / raised send is recorded as
    ``delivery_failed``, never as a confirmed dispatch. Only a measured
    delivery ACK (send exit 0 on the submit-complete rail) yields
    :data:`DISPATCH_WORKER_DISPATCHED` / ``worker_dispatch_confirmed=true``.
    """

    ops: WorkerDispatchOps

    def run(
        self,
        request: WorkerDispatchRequest,
        *,
        execute: bool,
        target_repo: str = "auto",
        fill_inputs: Optional[FillDecisionInputs] = None,
        override_fill_stop: Optional[str] = None,
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

        command = _replayable_command(
            issue=request.issue,
            journal=request.journal,
            worker_pane=lane.worker_pane,
            lane_label=request.lane_label,
            gateway_callback_target=lane.gateway_pane,
            target_repo=target_repo,
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
            )

        # 7. Live drive: the send's exit code is the delivery-ACK measurement.
        try:
            rc = self.ops.dispatch_to_worker(
                issue=request.issue,
                journal=anchor,
                worker_pane=lane.worker_pane,
                lane_label=request.lane_label,
                gateway_callback_target=lane.gateway_pane,
                target_repo=target_repo,
            )
        except SystemExit as exc:
            # Review j#71597: the composed handoff CLI fails closed through
            # `die()` == SystemExit, which `except Exception` never catches.
            # A port implementation that leaks it must still become a
            # `delivery_failed` outcome, never a process exit that skips the
            # durable fail-closed record.
            code = exc.code
            rc = code if isinstance(code, int) and code != 0 else 1
            detail = f"worker handoff send exited: SystemExit({exc.code})"
        except Exception as exc:  # noqa: BLE001 — fail-closed on any send failure.
            rc = 1
            detail = f"worker handoff send raised: {exc}"
        else:
            detail = f"worker handoff send to {lane.worker_pane} exit={rc}"
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
            )
        override_suffix = (
            f" — fill-decision stop overridden (reason: {fill_override_reason})"
            if fill_override_reason
            else ""
        )
        return WorkerDispatchOutcome(
            status=ACTUATE_EXECUTED,
            execute=True,
            reason="same-lane worker transfer delivery-acked "
            f"({detail}); worker_dispatch_confirmed=true (delivery ACK only — "
            "not worker progress or completion)" + override_suffix,
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
            ),
            blocked_reasons=reasons,
            fill_decision=fill_decision,
            fill_override_reason=fill_override_reason,
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
    if outcome.is_blocked:
        lines.append("  -> blocked: " + ", ".join(outcome.blocked_reasons))
    if outcome.command:
        lines.append(f"  $ {outcome.command}")
    lines.append("  durable record:")
    for jline in render_worker_dispatch_journal(outcome).splitlines():
        lines.append(f"    {jline}")
    return "\n".join(lines)


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
    use_case = WorkerDispatchUseCase(LiveWorkerDispatchOps(repo_root=repo_root))
    outcome = use_case.run(
        request,
        execute=execute,
        target_repo=getattr(args, "target_repo", None) or "auto",
        fill_inputs=fill_inputs,
        override_fill_stop=override_fill_stop,
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
    "WorkerDispatchOps",
    "LiveWorkerDispatchOps",
    "WorkerDispatchUseCase",
    "format_worker_dispatch_text",
    "cmd_sublane_dispatch_worker",
)
