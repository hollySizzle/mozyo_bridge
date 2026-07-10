"""herdr-backend worker-dispatch ack-drive IO for ``sublane dispatch-worker`` (Redmine #13357).

The tmux :class:`~...application.sublane_worker_dispatcher.LiveWorkerDispatchOps` reads the
lane from the tmux pane inventory and forwards the anchored ``implementation_request`` to
the same-lane Claude worker pane over the queue-enter tmux rail. Under
``terminal_transport.backend: herdr`` (Redmine #13331, option A) a lane is its own per-lane
herdr workspace whose managed agents are ``mzb1_<lane-ws>_codex_default`` (gateway) /
``mzb1_<lane-ws>_claude_default`` (worker) — there is no tmux pane to read or type into, so
the #12988 measured-ACK drive had no herdr leg (gap audit #13331 j#73370, loss #1).

:class:`HerdrWorkerDispatchOps` implements the SAME
:class:`~...application.sublane_worker_dispatcher.WorkerDispatchOps` port the tmux adapter
does, so the pure fail-closed
:class:`~...application.sublane_worker_dispatcher.WorkerDispatchUseCase` choreography — and
with it the #12988 contract (only a measured delivery ACK promotes to
``worker_dispatched`` / ``worker_dispatch_confirmed=true``; every failure keeps the lane's
recorded ``gateway_notified`` state, fail-closed) — is unchanged. Only the side effects
differ:

* ``read_lane`` — resolves the lane from the **live herdr inventory**, delegating to the
  #13331 :class:`~...application.sublane_actuator_herdr_ops.HerdrSublaneActuatorOps` fold
  (worktree → workspace segment via the shared resolver, ``agent list`` ``mzb1`` decode);
* ``probe_worker_ready`` — the #13301 pre-forward readiness wait's herdr form: a non-fatal
  live-presence check of the worker locator. A herdr agent is server-spawned (no TUI boot /
  render race to wait out) and the herdr send rail self-heals the landing (turn-start
  observation + Enter-resend, #13322), so readiness is simply "the worker locator is live
  in the inventory now" — the same rationale as the #13331 gateway probe;
* ``dispatch_to_worker`` — the identical governed ``handoff send --to claude ... --mode
  queue-enter`` CLI contract, composed through the shared argv builder and driven with the
  shared j#71597 SystemExit / stdout containment. The worker locator is a non-``%pane``
  target, so the send rides the **herdr rail** (#13320 effective-backend predicate): the
  same-workspace route authority resolves the worker slot (#13305) — pinned to the lane's
  **stable ``(workspace, lane_label, claude)`` identity** by an explicit ``--target-lane``
  (Redmine #13485) so it is never re-derived from the sender's own lane — the queue-enter
  rail injects + submits with the #13322 Enter-resend self-healing, and the delivery is
  recorded to the #13296 herdr ledger with the queue-enter turn-start observation (#13292).
  Exit 0 is the submit-complete delivery-ACK measurement to that stable worker target —
  nothing more (the ``ack-completion-receiver-state.md`` separation holds: no completion
  detector; the turn-start observation is separate telemetry, never conflated with the ACK).

The tmux path is untouched: this adapter is only selected by
``_resolve_worker_dispatch_ops`` when the repo-local config selects ``backend: herdr``
(:func:`~...application.sublane_herdr_projection.repo_backend_is_herdr`); any other /
absent / broken config keeps :class:`LiveWorkerDispatchOps` byte-for-byte.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_worker_dispatcher as _worker_dispatcher,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
    SublaneLaneView,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)


@dataclass
class HerdrWorkerDispatchOps:
    """Live herdr adapter composing the same-lane worker-forward primitives (#13357).

    ``repo_root`` is the lane worktree the drive runs in (the gateway's own checkout —
    the same value the request's ``worktree_path`` carries). ``lane_label`` / ``issue``
    are the requested lane identity, echoed by the inventory read-back exactly like the
    #13331 actuator adapter: under option A the lane identity is the worktree→workspace
    mapping, so the j#70250 ``lane_identity_matches`` guard validates against the
    request's own coordinates rather than a tmux label parse.

    ``env`` / ``runner`` are injected so tests drive a fake herdr; the binary is resolved
    from ``env`` (trusted-environment only), exactly like every other herdr path.
    """

    repo_root: Path
    lane_label: str
    issue: str
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    timeout: float = COMMAND_TIMEOUT_SECONDS

    def _actuator_ops(self) -> HerdrSublaneActuatorOps:
        return HerdrSublaneActuatorOps(
            repo_root=self.repo_root,
            lane_label=self.lane_label,
            issue=self.issue,
            env=self.env,
            runner=self.runner,
            timeout=self.timeout,
        )

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]:
        """The #13331 live-inventory lane read-back (worktree → workspace → slots)."""
        return self._actuator_ops().read_lane(worktree_path)

    def probe_worker_ready(self, worker_pane: str) -> bool:
        """One non-fatal live-presence snapshot of the worker locator (#13301 herdr form).

        Delegates to the #13331 presence probe — a role-agnostic "is this locator live in
        the inventory now" check — because a server-spawned herdr agent has no TUI
        boot/render race to observe and the send rail's #13322 self-healing is the landing
        net. Any read failure returns ``False`` (never fatal); the use case polls this on
        its bounded window exactly as it does the tmux probe.
        """
        return self._actuator_ops().probe_gateway_ready(worker_pane)

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
        """Drive the governed same-lane worker forward on the herdr rail (measured ACK).

        The argv the tmux adapter composes, plus two herdr-only pins (``--repo`` /
        ``--target-lane``): ``worker_pane`` is a live herdr locator (never ``%N``), so the
        #13320 effective-backend predicate routes the send onto the herdr rail, where
        ``--target-repo auto`` resolves to the sender's own repo root (#13331 j#73312 #2 —
        the same-workspace worker's repo) and the queue-enter rail submit-completes with
        the #13322 turn-start observation + Enter-resend self-healing. The exit code —
        contained by the shared j#71597 helper — is the delivery-ACK measurement the use
        case promotes (0) or fails closed on (non-0, ``gateway_notified`` kept). Calls
        resolve through the dispatcher module attribute so its established monkeypatch
        seams keep working.

        Redmine #13485: the herdr rail re-resolves its target through the #13305
        backend-neutral route authority, which discards the ``worker_pane`` locator and
        derives the target lane. Passing ``target_lane=lane_label`` pins that lane to the
        stable ``(workspace, lane_label, claude)`` identity the ``read_lane`` inventory
        decode already confirmed, so the ACK measures submit-completion to the intended
        worker even when the SENDER's launch-time lane attestation diverges (a coordinator
        / cross-lane stall-drive, or a legacy gateway) — the send no longer silently
        ACKs on a different / stale ``claude`` while the real lane worker stays idle
        (#13483 j#74570). This mirrors the coordinator→gateway leg, which already pins
        ``--target-lane`` (:meth:`HerdrSublaneActuatorOps.dispatch_argv`).
        """
        argv = _worker_dispatcher._worker_dispatch_argv(
            issue=issue,
            journal=journal,
            worker_pane=worker_pane,
            lane_label=lane_label,
            gateway_callback_target=gateway_callback_target,
            target_repo=target_repo,
            allow_direct_worker=allow_direct_worker,
            # Redmine #13485: pin the worker's stable lane identity so the herdr
            # route authority resolves `(workspace, lane_label, claude)` explicitly,
            # not the sender-derived lane (tier-2). The tmux adapter omits this
            # (default None) and its `%pane` target never rides the lane rail.
            target_lane=lane_label,
            # Redmine #13397: pin the inner send's effective-backend resolution to the
            # SAME repo the outer `sublane dispatch-worker` selected herdr on
            # (`self.repo_root` — the value `repo_backend_is_herdr` returned True for),
            # not the driving process's cwd. Without this, an external adopted project
            # (whose `backend: herdr` selection lives only at the adopted root, not a
            # committed config every checkout carries) re-derives `backend: tmux` from a
            # divergent cwd and validates the herdr worker locator (`worker_pane`, a
            # non-`%pane` handle) as an invalid tmux target — the #13379 j#73722 blocker.
            repo_root=str(self.repo_root),
        )
        return _worker_dispatcher._drive_worker_send_argv(argv)


__all__ = ("HerdrWorkerDispatchOps",)
