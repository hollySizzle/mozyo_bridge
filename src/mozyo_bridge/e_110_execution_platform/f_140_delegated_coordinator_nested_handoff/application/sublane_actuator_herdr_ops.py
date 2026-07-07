"""herdr-backend creation-side actuation IO for ``sublane start --execute`` (Redmine #13331).

The tmux :class:`~...application.sublane_actuator_ops.LiveSublaneActuatorOps` stands a lane
up as a *cockpit column* (two tmux panes) in the shared tmux server, so a lane is a
``(workspace_id, lane_id)`` slice of one workspace. Redmine #13331 (design consultation
answer j#73314, **option A**) migrates the lane onto herdr as its own **per-lane herdr
workspace**: the lane worktree is a fresh workspace whose two managed agents are launched
as ``mzb1_<lane-ws>_codex_default`` / ``mzb1_<lane-ws>_claude_default`` by the #13330
:func:`~...terminal_runtime_provider.application.herdr_session_start.prepare_session`
(explicit ``workspace create`` + ``agent start --workspace`` + root-pane reclaim), so the
hybrid (herdr coordinator, tmux lanes) is dissolved.

:class:`HerdrSublaneActuatorOps` implements the SAME
:class:`~...application.sublane_actuator_ops.SublaneActuatorOps` port the tmux adapter
does, so the pure fail-closed :class:`~...application.sublane_actuator_use_case.SublaneActuateUseCase`
choreography is unchanged — only the side effects differ:

* ``create_worktree`` — the identical additive #12604 git op (worktree add is backend-agnostic
  and already inside ``worktree-lifecycle-boundary.md``);
* ``append_lane_column`` — instead of a cockpit append, :func:`prepare_session` on the lane
  worktree, creating the lane's own herdr workspace with a codex gateway + claude worker;
* ``read_lane`` — resolves the lane from the **live herdr inventory** (``agent list``
  ``mzb1`` decode, #13247) filtered to the worktree's own mozyo workspace, not a tmux pane
  snapshot. The lane identity is the worktree→workspace mapping (collision-free: one
  worktree is one workspace), so it carries the request's ``lane_label`` / ``issue``;
* ``probe_gateway_ready`` — a non-fatal live-presence check of the gateway agent (the herdr
  send rail's turn-start observation + Enter-resend self-healing, #13322, is the landing
  net, so readiness is just "is the agent live");
* ``dispatch_implementation_request`` — the governed ``handoff send`` to the gateway. The
  gateway lives in the LANE workspace, so the coordinator→gateway leg crosses a workspace
  boundary: the dispatch names the lane worktree with an explicit ``--target-repo`` (the
  #13331 cross-workspace route authority resolves the receiver there) and a non-``%pane``
  herdr target so the send rides the herdr rail (#13320 effective-backend predicate).

Boundary (identical to the tmux adapter): creation-side / additive only — there is no
remove / kill / delete / merge method here; the destructive retire half stays gated
(``worktree-lifecycle-boundary.md``). The herdr binary is resolved ONLY from the trusted
environment (never a repo-local binary), exactly like every other herdr path.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (
    LiveSublaneGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    GATEWAY_ROLE,
    WORKER_ROLE,
    SublaneLaneView,
    _lane_state,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    HerdrSessionStartError,
    _list_rows,
    _resolve_binary_or_die,
    herdr_workspace_segment,
    prepare_session,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
    Runner,
)

#: The two provider slots a herdr lane workspace is launched with (gateway + worker).
HERDR_LANE_PROVIDERS: tuple[str, ...] = (GATEWAY_ROLE, WORKER_ROLE)


@dataclass
class HerdrSublaneActuatorOps:
    """Live herdr adapter composing the per-lane-workspace primitives for a lane.

    ``repo_root`` is the *coordinator's* checkout (the actuation is driven from the
    coordinator). ``lane_label`` / ``issue`` are the requested lane identity, echoed into
    the :class:`SublaneLaneView` on read-back: under option A the lane identity is the
    worktree→workspace mapping (each lane worktree is a distinct mozyo workspace, so there
    is no repo-root / basename collision to disambiguate — the ``read_lane`` anchor lookup
    is already the collision-free target), so the view carries the requested label with the
    live gateway / worker locators the inventory decode recovers.

    ``env`` / ``runner`` are injected so tests drive a fake herdr; the binary is resolved
    from ``env`` (trusted-environment only). ``quiet_stdout`` mirrors the tmux adapter:
    in ``--json`` mode it confines the composed ``handoff send`` progress text to stderr.
    """

    repo_root: Path
    lane_label: str
    issue: str
    branch: str = ""
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    providers: tuple[str, ...] = HERDR_LANE_PROVIDERS
    quiet_stdout: bool = False
    timeout: float = COMMAND_TIMEOUT_SECONDS

    # -- git probes / additive worktree add (backend-agnostic, reused verbatim) -----

    def _git(self) -> LiveSublaneGitOperations:
        return LiveSublaneGitOperations(repo_root=self.repo_root)

    def is_git_workspace(self) -> bool:
        return self._git().is_git_workspace()

    def worktree_exists(self, branch: str) -> bool:
        return self._git().worktree_exists(branch)

    def create_worktree(
        self, *, branch: str, worktree_path: str, base_ref: Optional[str] = None
    ) -> None:
        self._git().create_worktree(
            branch=branch, worktree_path=worktree_path, base_ref=base_ref
        )

    # -- lane column = per-lane herdr workspace -------------------------------------

    def append_lane_argv(self, worktree_path: str) -> list[str]:
        """The representative command for the dry-run preview (no side effect).

        The live path calls :func:`prepare_session` directly on the worktree; the nearest
        operator-facing equivalent is ``herdr session-start`` for the two lane slots.
        """
        argv = ["herdr", "session-start", "--repo", worktree_path]
        for provider in self.providers:
            argv += ["--agent", provider]
        return argv

    def append_lane_column(self, worktree_path: str) -> None:
        """Stand the lane's own herdr workspace up: launch the gateway + worker slots.

        Delegates to the #13330 :func:`prepare_session` on the LANE worktree (its own repo
        root → its own mozyo workspace + herdr workspace), launching
        ``mzb1_<lane-ws>_<provider>_default`` for each provider and reclaiming the cold-start
        empty base pane. :class:`HerdrSessionStartError` (unconfigured binary, a launch that
        lands in the wrong workspace, an unusable locator) propagates so the use case fails
        closed exactly as it does on a cockpit-append failure.
        """
        try:
            prepare_session(
                repo_root=Path(worktree_path),
                providers=list(self.providers),
                lane_id="",
                env=self.env,
                runner=self.runner,
                timeout=self.timeout,
            )
        except HerdrSessionStartError as exc:
            raise RuntimeError(f"herdr lane workspace creation failed: {exc}") from exc
        # Best-effort lane metadata upsert (Redmine #13356 j#73386 Q2): record the
        # token↔(lane_label / issue / branch / worktree) display join at the create
        # command boundary, so `sublane list` / dispatch-worker / the cockpit web UI
        # can resolve the lane's human identity from the wt_<hash> token. A metadata
        # write failure never breaks the actuation — the projections fail open to
        # the raw token (`lane_record_missing`).
        self._record_lane_metadata(worktree_path)

    def _record_lane_metadata(self, worktree_path: str) -> None:
        """Upsert the lane's display-metadata record (best-effort, never raises)."""
        from mozyo_bridge.core.state.lane_metadata import record_lane_created

        try:
            token = herdr_workspace_segment(Path(worktree_path))
        except (OSError, ValueError):
            return
        if not token:
            return
        try:
            repo_workspace_id = herdr_workspace_segment(self.repo_root)
        except (OSError, ValueError):
            repo_workspace_id = ""
        record_lane_created(
            lane_workspace_token=token,
            repo_workspace_id=repo_workspace_id,
            issue_id=self.issue or "",
            lane_label=self.lane_label or "",
            branch=self.branch or "",
            worktree_path=str(worktree_path),
        )

    def _live_rows(self) -> Sequence[Mapping[str, object]]:
        binary = _resolve_binary_or_die(self.env)
        runner = self.runner
        if runner is None:
            import subprocess

            runner = subprocess.run
        return _list_rows(binary, runner, self.timeout)

    def _lane_slots(
        self, workspace_id: str, rows: Sequence[Mapping[str, object]]
    ) -> dict[str, str]:
        """Map ``{role: locator}`` for this workspace's default-lane managed slots.

        Decodes each ``agent list`` row's ``mzb1`` name (#13247); a row is a managed lane
        slot iff it decodes to ``(workspace_id, default lane, role)``. Undecodable / foreign
        rows are skipped. A row that decodes to a managed slot but carries no live locator is
        skipped (a blank target is never a resolved pane), so the use case reads it as a
        missing pane and fails closed rather than adopting a locator-less lane.
        """
        slots: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
            if not decode.ok or decode.identity is None:
                continue
            identity = decode.identity
            if identity.workspace_id != workspace_id:
                continue
            if _norm_lane(identity.lane_id) != DEFAULT_LANE:
                continue
            if identity.role not in (GATEWAY_ROLE, WORKER_ROLE):
                continue
            locator = _agent_locator(row)
            if not locator:
                continue
            # First live locator wins; a duplicate managed name is a session-start
            # fail-closed condition, not this read's to resolve.
            slots.setdefault(identity.role, locator)
        return slots

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]:
        """Resolve the lane from the live herdr inventory for this worktree (fail-safe).

        Resolves the worktree's herdr workspace segment through the shared resolver
        (:func:`~...herdr_session_start.herdr_workspace_segment` — a lane token for a linked
        git worktree, #13331 j#73357), lists the live herdr agents, and folds the
        default-lane codex / claude managed slots into a :class:`SublaneLaneView`. Returns
        ``None`` when the worktree has no resolvable segment or when neither managed slot is
        live (a fresh worktree before :meth:`append_lane_column`, whose agents are not yet
        launched) — so the use case treats a not-yet-created lane as absent and creates it.
        The view carries the requested ``lane_label`` / ``issue`` (the worktree→workspace
        mapping is the collision-free lane identity).
        """
        try:
            workspace_id = herdr_workspace_segment(Path(worktree_path))
        except (OSError, ValueError):
            return None
        if not workspace_id:
            return None
        try:
            rows = self._live_rows()
        except HerdrSessionStartError:
            # Inventory unavailable (binary unset / herdr down). Fail-safe to None (the use
            # case reads that as "lane not visible" and fails closed on the read-back)
            # rather than fabricating a partial view — a genuinely down herdr also fails
            # closed on the append step, so this never adopts a lane it cannot see.
            return None
        slots = self._lane_slots(workspace_id, rows)
        gateway = slots.get(GATEWAY_ROLE)
        worker = slots.get(WORKER_ROLE)
        if not gateway and not worker:
            return None
        return SublaneLaneView(
            workspace_id=workspace_id,
            lane_id=DEFAULT_LANE,
            lane_label=self.lane_label,
            issue=self.issue or None,
            branch=None,
            repo_root=str(worktree_path),
            gateway_pane=gateway,
            worker_pane=worker,
            state=_lane_state(gateway, worker),
        )

    def probe_gateway_ready(self, gateway_pane: str) -> bool:
        """Non-fatal live-presence check of the gateway agent (#13293 parity).

        The tmux probe waits for a Codex TUI to boot + render before a queue-enter
        dispatch; on herdr the agent is server-spawned and the send rail self-heals
        (turn-start observation + Enter-resend, #13322), so readiness is simply "the
        gateway locator is live in the inventory now." Any read failure returns ``False``
        (never fatal) — the caller polls this on a bounded window.
        """
        want = _norm(gateway_pane)
        if not want:
            return False
        try:
            rows = self._live_rows()
        except Exception:  # noqa: BLE001 — a probe never fails the actuation.
            return False
        for row in rows:
            if isinstance(row, Mapping) and _agent_locator(row) == want:
                return True
        return False

    # -- governed dispatch (cross-workspace herdr send) -----------------------------

    def dispatch_argv(
        self,
        *,
        issue: str,
        journal: str,
        gateway_pane: str,
        lane_label: str,
        upstream_coordinator: Optional[str],
        target_repo: str,
    ) -> list[str]:
        """The governed ``handoff send`` argv for the coordinator→lane-gateway leg.

        The gateway lives in the LANE workspace, so ``--target-repo <lane-worktree>`` names
        it: the #13331 cross-workspace route authority resolves the codex gateway there. The
        ``--target`` is the gateway's live herdr locator — a non-``%pane`` value, so the
        send rides the herdr rail (#13320 effective-backend predicate) rather than the tmux
        rail. Same governed shape as the tmux dispatch (queue-enter, implementation_gateway
        role profile, lane / upstream_coordinator profile fields).
        """
        argv = [
            "handoff",
            "send",
            "--to",
            "codex",
            "--source",
            "redmine",
            "--issue",
            issue,
            "--journal",
            journal,
            "--kind",
            "implementation_request",
            "--target",
            gateway_pane,
            "--target-repo",
            target_repo,
            "--mode",
            "queue-enter",
            "--role-profile",
            "implementation_gateway",
            "--profile-field",
            f"lane={lane_label}",
        ]
        if upstream_coordinator:
            argv += ["--profile-field", f"upstream_coordinator={upstream_coordinator}"]
        return argv

    def _drive_cli(self, argv: list[str]) -> int:
        """Parse ``argv`` with the composed CLI parser and run its handler (live).

        Mirrors the tmux adapter's ``_drive_cli`` so a herdr dispatch is byte-for-byte the
        Namespace an operator's ``mozyo-bridge handoff send ...`` would build (same
        defaults, same herdr send rail, same outcome emission). Imported lazily so the pure
        use case / tests never require the CLI infrastructure.
        """
        from mozyo_bridge.application.cli import build_parser, normalize_paths

        args = build_parser().parse_args(argv)
        args = normalize_paths(args)
        if self.quiet_stdout:
            with contextlib.redirect_stdout(sys.stderr):
                return int(args.func(args))
        return int(args.func(args))

    def dispatch_implementation_request(
        self,
        *,
        issue: str,
        journal: str,
        gateway_pane: str,
        lane_label: str,
        upstream_coordinator: Optional[str],
        target_repo: str,
    ) -> int:
        return self._drive_cli(
            self.dispatch_argv(
                issue=issue,
                journal=journal,
                gateway_pane=gateway_pane,
                lane_label=lane_label,
                upstream_coordinator=upstream_coordinator,
                target_repo=target_repo,
            )
        )


__all__ = (
    "HERDR_LANE_PROVIDERS",
    "HerdrSublaneActuatorOps",
)
