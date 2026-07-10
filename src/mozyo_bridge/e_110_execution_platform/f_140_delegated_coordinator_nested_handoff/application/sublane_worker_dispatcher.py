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

Backend selection (Redmine #13357): under ``terminal_transport.backend: herdr``
(#13331 option A — a lane is its own per-lane herdr workspace) the handler picks
the herdr adapter
:class:`~...application.sublane_worker_dispatch_herdr_ops.HerdrWorkerDispatchOps`
through :func:`_resolve_worker_dispatch_ops` (the same
:func:`~...application.sublane_herdr_projection.repo_backend_is_herdr` selector
``sublane create --execute`` uses), so the measured-ACK contract above holds on
the herdr rail too. Anything else keeps the tmux :class:`LiveWorkerDispatchOps`,
byte-for-byte.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

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
    redact_worktree_paths,
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
# Worker readiness wait tuning (#13301). Mirrors the #13293 gateway readiness wait
# but targets the same-lane Claude worker pane: before the queue-enter forward, the
# drive polls the worker pane up to ``DEFAULT_WORKER_READY_PROBES`` times at
# ``DEFAULT_WORKER_READY_INTERVAL_SECONDS`` apart (≈ a 10s window by default) so a
# freshly-launched worker TUI has time to boot before the forward — the second-wave
# 3/4-lane failure mode (j#72860/72861/72862) was "forward typed into a still-booting
# worker composer", the worker-side analog of the gateway dispatch-loss the #13293
# wait closed on the gateway pane. The wait NEVER hard-blocks the queue-enter rail:
# an unconfirmed readiness degrades to a recorded ``worker_ready=false`` and forwards
# anyway (the handoff Enter-only retry is the landing safety net).
# ---------------------------------------------------------------------------

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
            info = pane_info(worker_pane)
            if not is_receiver_agent_process(info.get("command", ""), "claude"):
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
        )
        return _drive_worker_send_argv(argv)


def _drive_worker_send_argv(argv: list[str]) -> int:
    """Run the composed same-lane ``handoff send`` argv, fail-closed (shared).

    Review j#71597: the inner `handoff send` fails closed through
    `die()` == `raise SystemExit`, which `except Exception` never
    catches, and it emits its own delivery record to stdout. Both must
    be contained here so the outer WorkerDispatchOutcome stays the
    single fail-closed, machine-readable surface: run the composed
    primitive under stdout capture and convert any SystemExit
    (including an argparse usage error) to its exit code. The captured
    inner record is surfaced to stderr on failure so the blocked send
    stays diagnosable without polluting the outer `--json` stdout.

    Shared by the tmux :class:`LiveWorkerDispatchOps` and the herdr
    :class:`~...application.sublane_worker_dispatch_herdr_ops.HerdrWorkerDispatchOps`
    (#13357), so both backends measure the delivery ACK with the identical
    containment; extracting it changes no tmux behaviour.
    """
    from mozyo_bridge.application.cli import build_parser, normalize_paths

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
    allow_direct_worker: bool = False,
    repo_root: Optional[str] = None,
    target_lane: Optional[str] = None,
) -> list[str]:
    """The same-lane worker forward as the gateway would type it (pure).

    ``--role-profile implementation_worker`` binds the worker's effective
    instruction set; ``gateway_callback_target`` (the lane's own gateway pane)
    is the worker's same-lane callback address. The queue-enter rail keeps the
    dispatch submit-complete (#12207) with standard_target_admission covering
    the usually-inactive worker split (#12597).

    ``allow_direct_worker`` (#13301) threads the explicit ``--allow-direct-worker``
    gateway-route exception (#12918) onto the send so a drive from a pane whose lane
    Unit differs from the worker's (e.g. a coordinator stall-drive) is admitted and
    recorded distinctly as a ``gateway_route_exception`` instead of failing closed.
    The same-lane gateway drive leaves it off (default), so the #12988 contract is
    byte-for-byte back-compatible.

    ``repo_root`` (Redmine #13397) pins the top-level ``--repo`` so the inner
    ``handoff send`` resolves its *effective backend* from the SAME repo the outer
    ``sublane dispatch-worker`` already selected — not from the driving process's
    cwd. Under ``terminal_transport.backend: herdr`` the send-path backend predicate
    (``herdr_effective_backend_selected`` → ``load_repo_local_config(repo_root_from_args(args))``)
    reads the config at ``repo_root_from_args``, which defaults to a marker walk from
    cwd. In-project (``mozyo_bridge``) the herdr selection is a *committed* config, so
    every checkout / lane worktree carries it and cwd resolution happens to agree; in
    an **external** adopted project the herdr selection lives only at the adopted root,
    so a drive whose cwd resolves elsewhere re-derived ``backend: tmux`` and validated
    the herdr worker locator (``wS:p3``) as an invalid tmux target, failing closed with
    ``target_unavailable`` (#13379 j#73722). Pinning ``--repo`` to the outer-resolved
    root makes the inner backend match the outer selection. ``None`` (the tmux
    :class:`LiveWorkerDispatchOps` default) omits the flag, so the tmux argv stays
    byte-for-byte the pre-#13397 shape.

    ``target_lane`` (Redmine #13485) pins the explicit ``--target-lane`` so the inner
    herdr send resolves the worker by its **stable ``(workspace_id, lane_id, role)``
    identity** — the ``lane_label`` the ``read_lane`` inventory decode confirmed — and
    NOT by re-deriving the lane from the *sender's* identity (``derive_target_lane``
    tier-2 sender-same-lane). Without the pin the herdr rail discards the resolved
    worker locator and re-resolves ``(sender.workspace_id, sender.lane_id, claude)``:
    when the sender's launch-time lane attestation diverges from the worker's lane —
    a coordinator / cross-lane stall-drive, or a legacy / mis-attested gateway — that
    derives a DIFFERENT (or stale) ``claude`` slot, so the send delivery-ACKs (exit 0)
    on the wrong agent while the real lane worker stays idle (the #13483 j#74570 live
    divergence). Pinning the lane makes the ACK measure submit-completion to the
    intended worker, exactly as the coordinator→gateway leg already pins
    ``--target-lane`` (``sublane_actuator_herdr_ops.dispatch_argv``). ``None`` (the
    tmux :class:`LiveWorkerDispatchOps` default) omits the flag: the tmux worker
    addresses an explicit ``%pane`` and never rides the lane-derivation rail, so the
    tmux argv stays byte-for-byte the pre-#13485 shape.
    """
    argv: list[str] = []
    if repo_root:
        # Top-level flag: it MUST precede the ``handoff`` subcommand.
        argv += ["--repo", repo_root]
    argv += [
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
    ]
    if target_lane:
        # Redmine #13485: explicit lane authority (mirrors the gateway dispatch's
        # `--target-lane`). Placed with the other target coordinates, before `--mode`.
        argv += ["--target-lane", target_lane]
    argv += [
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
    if allow_direct_worker:
        argv.append("--allow-direct-worker")
    return argv


def _replayable_command(
    *,
    issue: str,
    journal: Optional[str],
    worker_pane: Optional[str],
    lane_label: str,
    gateway_callback_target: Optional[str],
    target_repo: str,
    allow_direct_worker: bool = False,
    target_lane: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> str:
    """The replayable retry command surfaced on the outcome / durable journal.

    Redmine #13485 review F1: the outcome's ``command`` is documented as a *replayable
    retry command* (:func:`render_worker_dispatch_journal`), so on the herdr rail it MUST
    carry the same stable-lane authority the actual dispatch pins — ``--target-lane`` (the
    lane the ``read_lane`` decode confirmed) and the #13397 ``--repo`` backend pin — or
    replaying the printed command would drop back to the sender-lane re-derivation this US
    fixes (a cross-lane replay would false-positive ACK on the wrong lane again). The pins
    ride the same ``target_lane`` / ``repo_root`` params ``_worker_dispatch_argv`` uses, so
    the printed command is byte-identical to the argv the herdr adapter actually drove. The
    tmux path passes neither (both ``None``), so its command stays byte-for-byte the prior
    shape.
    """
    return "mozyo-bridge " + " ".join(
        _worker_dispatch_argv(
            issue=issue,
            journal=journal or "<journal>",
            worker_pane=worker_pane or "<worker-pane>",
            lane_label=lane_label,
            gateway_callback_target=gateway_callback_target,
            target_repo=target_repo,
            allow_direct_worker=allow_direct_worker,
            target_lane=target_lane,
            repo_root=repo_root,
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
    # #13301 pre-dispatch worker readiness wait (injectable for tests). ``probes<=0``
    # disables the wait (back-compat immediate forward; ``worker_ready`` stays None).
    worker_ready_probes: int = DEFAULT_WORKER_READY_PROBES
    worker_ready_interval_seconds: float = DEFAULT_WORKER_READY_INTERVAL_SECONDS
    sleep: Callable[[float], None] = field(default=time.sleep)

    def _wait_worker_ready(self, worker_pane: Optional[str]) -> Optional[bool]:
        """Bounded, non-fatal pre-forward worker readiness wait (#13301).

        Polls :meth:`WorkerDispatchOps.probe_worker_ready` up to
        ``worker_ready_probes`` times, ``worker_ready_interval_seconds`` apart, so a
        freshly-launched worker TUI has time to boot before the queue-enter forward.
        Returns ``None`` when the wait is disabled (``probes<=0``) or no worker pane
        resolved — nothing was probed. Otherwise ``True`` on the first ready
        observation or ``False`` when the window elapses unconfirmed. It NEVER raises
        and NEVER blocks the forward: an unconfirmed ``False`` degrades to a recorded
        observation and the caller forwards anyway (mirrors the #13293 gateway wait).
        """
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
        """Backend-specific replay-authority pins for the durable / dry-run command.

        Redmine #13485 review F1: the outcome ``command`` is a replayable retry command,
        so the herdr adapter supplies the ``--target-lane`` / ``--repo`` authority its
        actual dispatch pins (:meth:`HerdrWorkerDispatchOps.command_authority_pins`) and
        the command reproduces the true argv. An optional port capability read through
        ``getattr`` — the tmux :class:`LiveWorkerDispatchOps` does not define it, so its
        command stays byte-for-byte the pre-#13485 shape (no pins).
        """
        getter = getattr(self.ops, "command_authority_pins", None)
        return getter() if callable(getter) else {}

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
            )

        # 6b. Pre-forward worker readiness wait (#13301, execute path only): give a
        # freshly-launched worker TUI time to boot before the queue-enter forward, so
        # the anchored implementation_request lands on a live composer instead of
        # vanishing into a still-booting one (the worker-side analog of the #13293
        # gateway wait). Bounded + non-fatal — an unconfirmed readiness degrades to a
        # recorded worker_ready=false and forwards anyway (the queue-enter rail never
        # hard-blocks; the handoff Enter-only retry is the landing safety net).
        worker_ready = self._wait_worker_ready(lane.worker_pane)

        # 7. Live drive: the send's exit code is the delivery-ACK measurement.
        try:
            rc = self.ops.dispatch_to_worker(
                issue=request.issue,
                journal=anchor,
                worker_pane=lane.worker_pane,
                lane_label=request.lane_label,
                gateway_callback_target=lane.gateway_pane,
                target_repo=target_repo,
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
                worker_ready=worker_ready,
                allow_direct_worker=allow_direct_worker,
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
            worker_ready=worker_ready,
            allow_direct_worker=allow_direct_worker,
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
                **{
                    k: v
                    for k, v in self._command_pins().items()
                    if k in ("target_lane", "repo_root")
                },
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
