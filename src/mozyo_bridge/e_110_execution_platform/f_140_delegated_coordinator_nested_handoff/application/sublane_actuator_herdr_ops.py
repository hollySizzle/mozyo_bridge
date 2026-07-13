"""herdr-backend creation-side actuation IO for ``sublane start --execute`` (Redmine #13377).

The tmux :class:`~...application.sublane_actuator_ops.LiveSublaneActuatorOps` stands a lane
up as a *cockpit column* (two tmux panes) in the shared tmux server, so a lane is a
``(workspace_id, lane_id)`` slice of one workspace. Redmine #13377 (design consultation
answer j#73613, **Opt3 — shared project workspace**) gives herdr the same identity shape:
the lane worktree stays a linked git worktree and its two managed agents are launched as
``mzb1_<project-ws>_codex_<lane>`` / ``mzb1_<project-ws>_claude_<lane>`` by the #13330
:func:`~...terminal_runtime_provider.application.herdr_session_start.prepare_session`
(join-or-create workspace + ``agent start --workspace --cwd <lane-worktree>`` + root-pane
reclaim). Placement refined by Redmine #13380 (dedicated sublane host workspace): lane
slots land in a single sublane host workspace separate from the coordinator pair's
project workspace, so the herdr workspace count is a constant "project 1 + host 1" —
still never scaling with the lane count. This supersedes the #13331 j#73314 per-lane
``wt_<hash>`` workspace (option A), which survives read-side as legacy compatibility
only.

:class:`HerdrSublaneActuatorOps` implements the SAME
:class:`~...application.sublane_actuator_ops.SublaneActuatorOps` port the tmux adapter
does, so the pure fail-closed :class:`~...application.sublane_actuator_use_case.SublaneActuateUseCase`
choreography is unchanged — only the side effects differ:

* ``create_worktree`` — the identical additive #12604 git op (worktree add is backend-agnostic
  and already inside ``worktree-lifecycle-boundary.md``);
* ``append_lane_column`` — instead of a cockpit append, :func:`prepare_session` on the lane
  worktree with ``lane_id=lane_label``, launching the codex gateway + claude worker as lane
  slots of the project identity, placed in the dedicated sublane host workspace (#13380);
* ``read_lane`` — resolves the lane from the **live herdr inventory** (``agent list``
  ``mzb1`` decode, #13247) filtered to the lane's unit ``(project workspace, lane_label)``,
  not a tmux pane snapshot; a pre-#13377 lane still resolves through its legacy
  ``wt_<hash>`` default-lane slots (compatibility read, so a live legacy lane is never
  double-created);
* ``probe_gateway_ready`` — a non-fatal boot-readiness check of the gateway agent: live in
  the inventory AND rendered (``agent read`` returns non-blank text, #13378 — the same
  booted-and-rendered gate as the tmux probe; the send rail's turn-start observation +
  Enter-resend, #13322, stays the landing net);
* ``dispatch_implementation_request`` — the governed ``handoff send`` to the gateway. The
  gateway is a lane slot of the SAME *mozyo* workspace identity (its herdr placement —
  the sublane host workspace, #13380 — is irrelevant to routing, which matches on the
  mzb1 decode), so the coordinator→gateway leg is an **explicit-lane** send: the
  dispatch passes ``--target-lane
  <lane_label>`` (the j#73613 explicit lane field — never an all-lane scan) plus
  ``--target-repo <lane-worktree>`` as the repo/cwd gate (a gate, not a workspace
  selector) and a non-``%pane`` herdr target so the send rides the herdr rail (#13320
  effective-backend predicate).

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

from mozyo_bridge.core.state.workspace_registry import read_anchor
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (
    GATEWAY_READY_CAPTURE_LINES,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (
    LiveSublaneGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    GATEWAY_ROLE,
    WORKER_ROLE,
    SublaneLaneView,
    _lane_state,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    _list_rows,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    HerdrSessionStartError,
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
    derive_directory_lane_token,
    derive_lane_workspace_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    resolve_sender_identity,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
    HerdrCliTransport,
    Runner,
)

#: The two provider slots a herdr lane workspace is launched with (gateway + worker).
HERDR_LANE_PROVIDERS: tuple[str, ...] = (GATEWAY_ROLE, WORKER_ROLE)


@dataclass
class HerdrSublaneActuatorOps:
    """Live herdr adapter composing the shared-project-workspace primitives for a lane.

    ``repo_root`` is the *coordinator's* checkout (the actuation is driven from the
    coordinator). ``lane_label`` / ``issue`` are the requested lane identity: under the
    #13377 shared model ``lane_label`` IS the mzb1 lane segment, so the lane unit
    ``(project workspace, lane_label)`` is the collision-free target the ``read_lane``
    inventory decode resolves, and the view carries the requested label with the live
    gateway / worker locators.

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

    def gateway_provider(self) -> str:
        """The coordinator (gateway) role's runtime provider from the binding (Redmine #13569).

        Default ``codex`` (byte-identical); a rebound gateway provider moves the herdr
        coordinator -> lane-gateway ``--to`` receiver with no source edit. Unbound ->
        fail-closed zero-dispatch.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            resolve_gateway_provider,
        )

        return resolve_gateway_provider(str(self.repo_root))

    def _launch_providers(self) -> tuple[str, str]:
        """The (gateway, worker) provider slots this lane launches (Redmine #13569 R1-F2).

        The sublane's runtime provider slots are injected from the repo-local binding —
        the gateway (coordinator) provider then the worker (implementer) provider — so a
        rebound binding launches ITS providers rather than the fixed ``(codex, claude)``
        pair, and the coordinator -> gateway dispatch reaches a real pane. Default
        ``(codex, claude)`` in launch order, byte-identical. This is the sublane's
        provider-slot injection, NOT the fenced *default-lane* topology (which stays a
        literal contract in ``herdr_launch_command`` / ``session_bootstrap``). An unbound
        role raises :class:`WorkflowProviderUnresolved`, failing the launch closed.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            resolve_worker_provider,
        )

        return (self.gateway_provider(), resolve_worker_provider(str(self.repo_root)))

    def canonical_workspace_root(self) -> str:
        # #13392: the coordinator's workspace root — the lane runtime root of a non-git
        # (skip_no_git) lane, which has no worktree and runs in the workspace root itself.
        return str(self.repo_root)

    def is_git_workspace(self) -> bool:
        return self._git().is_git_workspace()

    def worktree_exists(self, branch: str) -> bool:
        return self._git().worktree_exists(branch)

    def preflight_dispatch_sender(self) -> tuple[bool, str]:
        """Verify the command-shell sender before any lane mutation (#13613)."""

        try:
            anchor = read_anchor(self.repo_root)
        except Exception as exc:  # fail closed at the external read boundary
            return False, f"workspace anchor unreadable ({exc})"
        anchor_workspace_id = _norm(
            anchor.get("workspace_id") if isinstance(anchor, Mapping) else ""
        )
        result = resolve_sender_identity(
            self.env,
            anchor_workspace_id=anchor_workspace_id or None,
        )
        if not result.ok or result.identity is None:
            return False, f"{result.reason}: {result.detail}"
        try:
            from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (  # noqa: E501
                resolve_coordinator_provider,
            )

            coordinator_provider = resolve_coordinator_provider(str(self.repo_root))
        except Exception as exc:  # config/binding IO is fail-closed here
            return False, f"coordinator provider binding is unreadable ({exc})"
        if result.identity.role != coordinator_provider:
            return False, (
                f"sender provider {result.identity.role!r} is not the configured "
                f"coordinator provider {coordinator_provider!r}"
            )
        if result.identity.lane_id != DEFAULT_LANE:
            return False, (
                f"sender lane {result.identity.lane_id!r} is not the coordinator "
                f"default lane {DEFAULT_LANE!r}"
            )
        return True, "sender identity matches the coordinator binding and default lane"

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
        for provider in self._launch_providers():
            argv += ["--agent", provider]
        return argv

    def append_lane_column(self, worktree_path: str) -> None:
        """Stand the lane's slots up inside the dedicated sublane host workspace.

        Delegates to the #13330 :func:`prepare_session` on the LANE worktree with
        ``lane_id=lane_label`` (Redmine #13377 Opt3): the worktree inherits the main
        checkout's workspace identity, so the slots launch as
        ``mzb1_<project-ws>_<provider>_<lane_label>`` INTO the sublane host workspace
        (Redmine #13380 lane-aware join: the lane's own live slots pin the target
        first, then the host the other lane slots occupy — never the coordinator
        pair's project workspace — and a labelled host is minted on demand; a lane
        never creates a per-lane workspace). :class:`HerdrSessionStartError`
        (unconfigured binary, a launch that lands in the wrong workspace, an unusable
        locator) propagates so the use case fails closed exactly as it does on a
        cockpit-append failure.
        """
        # Config-driven launch argv (Redmine #13425): the lane's slots are the `sublane`
        # lane_class, so the config's `launch_argv.{provider}.sublane` tokens (the folded
        # `sublane_claude_model` claude `--model`, codex sublane reasoning-effort flag, …)
        # are appended at the launch chokepoint — the herdr-side fix for the #13155
        # regression. The config is read from the SOURCE checkout (`self.repo_root`, the
        # coordinator's), never `worktree_path`: the committed config is identical after
        # creation and this keeps one config source (the same rule the tmux
        # `resolve_append_lane_argv` follows). An unconfigured repo appends nothing.
        from mozyo_bridge.application.repo_local_config_loader import (
            load_repo_local_config,
        )

        # Config-driven pane placement (Redmine #13646): the lane's slots are the `sublane`
        # lane_class, so the config's `lane_placement.sublane` split / order decides the
        # gateway/worker pair's geometry inside the lane tab and which provider occupies it
        # first. Read from the SAME source checkout as `agent_launch` (one config source).
        # An unconfigured repo keeps the legacy `--split right` and the requested order.
        repo_config = load_repo_local_config(self.repo_root)
        agent_launch = repo_config.agent_launch
        try:
            prepare_session(
                repo_root=Path(worktree_path),
                providers=list(self._launch_providers()),
                lane_id=self.lane_label,
                env=self.env,
                runner=self.runner,
                timeout=self.timeout,
                lane_placement=repo_config.lane_placement,
                # Lane creation is a managed-pane chokepoint: pass the cockpit /
                # sublane policy default (#11925) so lane Claude workers launch
                # reproducibly auto, exactly like the tmux `cockpit append` path
                # (#13360 — without this every herdr lane worker stalls on its
                # first permission prompt).
                claude_permission_mode_default=COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
                agent_launch=agent_launch,
            )
        except HerdrSessionStartError as exc:
            raise RuntimeError(f"herdr lane slot creation failed: {exc}") from exc
        # Best-effort lane metadata upsert (Redmine #13356 j#73386 Q2 / #13377): record
        # the (lane unit)↔(lane_label / issue / branch / worktree) display join at the
        # create command boundary, so `sublane list` / dispatch-worker / the cockpit web
        # UI can resolve the lane's human identity. The record key stays the worktree's
        # stable path token; `repo_workspace_id` + `lane_id` carry the live unit the
        # shared-model projections join on. A metadata write failure never breaks the
        # actuation — the projections fail open (`lane_record_missing`).
        self._record_lane_metadata(worktree_path)

    def _record_lane_metadata(self, worktree_path: str) -> None:
        """Upsert the lane's display-metadata record (best-effort, never raises)."""
        from mozyo_bridge.core.state.lane_metadata import record_lane_created

        try:
            resolved = Path(worktree_path).expanduser().resolve()
        except OSError:
            return
        # The stable per-worktree metadata key (also the legacy pre-#13377 workspace
        # segment) — NOT the mzb1 workspace segment, which is the project identity now.
        # #13392: a non-git (directory scaffold) lane has no worktree — the use case
        # collapses its runtime root to the shared workspace root (``== self.repo_root``),
        # so the path-only ``wt_`` token would collide across every lane on that root and
        # one lane's record would overwrite the next. Detect that collapse (runtime root IS
        # the workspace root) and scope the key by ``(workspace root, lane_id)`` instead so
        # two lanes on one non-git root keep distinct records (live unit stays
        # ``(project ws, lane_id)``). A Git lane's distinct worktree keeps the ``wt_`` token.
        try:
            is_workspace_root = resolved == self.repo_root.expanduser().resolve()
        except OSError:
            is_workspace_root = False
        if is_workspace_root:
            token = derive_directory_lane_token(str(resolved), self.lane_label or "")
        else:
            token = derive_lane_workspace_token(str(resolved))
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
            lane_id=self.lane_label or "",
        )

    def heal_lane_column(self, worktree_path: str) -> None:
        """Relaunch the lane's missing managed slot(s) (self-heal, Redmine #13378).

        A lane gateway can die between its launch and the first dispatch for reasons
        entirely outside mozyo (measured: a host-level ``npm install -g @openai/codex``
        cleanly exits every idle, pre-session codex TUI — #13378 j#73606). The heal is
        simply :meth:`append_lane_column` again: :func:`prepare_session` is
        adopt-or-launch idempotent per slot, so the surviving slot is *adopted* and
        only the dead slot is relaunched. Under the #13380 lane-aware join the
        surviving slot pins the relaunch target (a heal never splits the pair —
        even a legacy lane still cohabiting the coordinator's workspace heals in
        place), and a lane whose BOTH slots died heals into the sublane host the
        other lane slots occupy, or re-mints it (j#73619 alignment: a heal never
        resurrects a per-lane workspace). Any
        :class:`HerdrSessionStartError` propagates as ``RuntimeError`` so the use case
        stays fail-closed. Exposed as an *optional* port capability (the use case
        discovers it via ``getattr``): the tmux adapter deliberately does not provide it
        — a repeated tmux ``cockpit append`` would append a duplicate column, not adopt.
        """
        self.append_lane_column(worktree_path)

    def _live_rows(self) -> Sequence[Mapping[str, object]]:
        binary = _resolve_binary_or_die(self.env)
        runner = self.runner
        if runner is None:
            import subprocess

            runner = subprocess.run
        return _list_rows(binary, runner, self.timeout)

    def _lane_slots(
        self,
        workspace_id: str,
        lane_id: str,
        rows: Sequence[Mapping[str, object]],
    ) -> dict[str, str]:
        """Map ``{role: locator}`` for the lane unit's managed slots.

        Decodes each ``agent list`` row's ``mzb1`` name (#13247); a row is a managed lane
        slot iff it decodes to ``(workspace_id, lane_id, role)``. Undecodable / foreign
        rows are skipped. A row that decodes to a managed slot but carries no live locator is
        skipped (a blank target is never a resolved pane), so the use case reads it as a
        missing pane and fails closed rather than adopting a locator-less lane.
        """
        want_lane = _norm_lane(lane_id)
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
            if _norm_lane(identity.lane_id) != want_lane:
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

        Shared project workspace model (Redmine #13377): the lane unit is
        ``(project workspace segment, lane_label)`` — the worktree resolves to the main
        checkout's workspace identity through the shared resolver and the requested
        ``lane_label`` is the lane discriminant. A pre-#13377 lane whose agents still
        carry the legacy per-lane ``wt_<hash>`` default-lane names resolves through the
        compatibility read (so a live legacy lane is adopted, never double-created).
        Returns ``None`` when no segment resolves or neither managed slot is live (a
        fresh worktree before :meth:`append_lane_column`) — the use case then treats the
        lane as absent and creates it.
        """
        try:
            resolved = Path(worktree_path).expanduser().resolve()
            workspace_id = herdr_workspace_segment(resolved)
        except (OSError, ValueError):
            return None
        try:
            rows = self._live_rows()
        except HerdrSessionStartError:
            # Inventory unavailable (binary unset / herdr down). Fail-safe to None (the use
            # case reads that as "lane not visible" and fails closed on the read-back)
            # rather than fabricating a partial view — a genuinely down herdr also fails
            # closed on the append step, so this never adopts a lane it cannot see.
            return None
        lane_id = _norm_lane(self.lane_label)
        slots: dict[str, str] = {}
        if workspace_id:
            slots = self._lane_slots(workspace_id, lane_id, rows)
        if not slots:
            # Legacy compatibility (pre-#13377): the lane's agents live in their own
            # `wt_<hash>` workspace under the default lane.
            legacy_ws = derive_lane_workspace_token(str(resolved))
            legacy_slots = self._lane_slots(legacy_ws, DEFAULT_LANE, rows)
            if legacy_slots:
                workspace_id, lane_id, slots = legacy_ws, DEFAULT_LANE, legacy_slots
        gateway = slots.get(GATEWAY_ROLE)
        worker = slots.get(WORKER_ROLE)
        if not gateway and not worker:
            return None
        return SublaneLaneView(
            workspace_id=workspace_id,
            lane_id=lane_id,
            lane_label=self.lane_label,
            issue=self.issue or None,
            branch=None,
            repo_root=str(worktree_path),
            gateway_pane=gateway,
            worker_pane=worker,
            state=_lane_state(gateway, worker),
        )

    def probe_gateway_ready(self, gateway_pane: str) -> bool:
        """Non-fatal boot-readiness check of the gateway agent (#13293 parity).

        Redmine #13378: readiness is "the gateway locator is live in the inventory
        AND its pane has rendered content" (``agent read`` returns non-blank text) —
        the same booted-and-rendered gate the tmux probe applies. The prior
        liveness-only probe returned ``True`` the instant ``agent start`` completed,
        so the in-create dispatch fired into a still-booting codex TUI and vanished
        (the measured reason the #13366 runbook fell back to a two-step
        ``--no-dispatch`` flow). Any read failure returns ``False`` (never fatal) —
        the caller polls this on a bounded window and the queue-enter rail's
        turn-start observation + Enter-resend (#13322) stays the landing net.
        """
        want = _norm(gateway_pane)
        if not want:
            return False
        try:
            rows = self._live_rows()
        except Exception:  # noqa: BLE001 — a probe never fails the actuation.
            return False
        if not any(
            isinstance(row, Mapping) and _agent_locator(row) == want for row in rows
        ):
            return False
        try:
            binary = _resolve_binary_or_die(self.env)
            runner = self.runner
            if runner is None:
                import subprocess

                runner = subprocess.run
            transport = HerdrCliTransport(binary, runner=runner, timeout=self.timeout)
            read = transport.read_pane(want, lines=GATEWAY_READY_CAPTURE_LINES)
        except Exception:  # noqa: BLE001 — a probe never fails the actuation.
            return False
        if not read.ok:
            return False
        return bool((read.content or "").strip())

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

        The gateway is a lane slot of the SAME project workspace (Redmine #13377), so the
        dispatch is an explicit-lane, same-workspace herdr send: ``--target-lane
        <lane_label>`` names the slot (j#73613 — an explicit lane field, never an all-lane
        scan) and ``--target-repo <lane-worktree>`` stays the repo/cwd gate (a gate, not a
        workspace selector). The ``--target`` is the gateway's live herdr locator — a
        non-``%pane`` value, so the send rides the herdr rail (#13320 effective-backend
        predicate) rather than the tmux rail. Same governed shape as the tmux dispatch
        (queue-enter, implementation_gateway role profile, lane / upstream_coordinator
        profile fields).
        """
        argv = [
            "handoff",
            "send",
            "--to",
            self.gateway_provider(),
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
            "--target-lane",
            lane_label,
            "--mode",
            "queue-enter",
            "--role-profile",
            "implementation_gateway",
            "--profile-field",
            f"lane={lane_label}",
        ]
        # The live callers resolve the #13476 stable-route default
        # (SublaneCreateRequest.resolved_upstream_coordinator), so this is always
        # non-empty in normal flow; the guard stays as null-safety for direct callers.
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
