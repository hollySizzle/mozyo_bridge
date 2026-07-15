"""Creation-side actuation IO layer for ``sublane start --execute`` (Redmine #13299).

Byte-preserving carve of the injected side-effect boundary out of the #12973
``sublane_actuator`` facade (module-health decomposition of the at-ceiling use case;
the facade re-exports these names, so the public import surface is unchanged):

* :class:`SublaneActuatorOps` — the port declaring every side effect the live actuator
  needs (git probes / additive ``git worktree add`` / cockpit ``append`` / lane read-back
  / gateway readiness probe / governed ``handoff send`` dispatch), injected so tests drive
  fakes; there is intentionally no remove / kill / delete / merge method — the destructive
  half is gated and coordinator-owned;
* :class:`LiveSublaneActuatorOps` — the adapter composing the real creation-side primitives
  (the #12604 git ops + the existing ``cockpit append`` / ``handoff send`` CLI contract the
  coordinator already drives by hand) for a ``repo_root``;
* the gateway-readiness tuning constants (#13293) and the ``_normalize_path`` repo-root
  matcher shared by the read-back.

The pure fail-closed decision flow lives in the sibling
:mod:`...application.sublane_actuator_use_case` (:class:`SublaneActuateUseCase`), which
drives this port and never touches IO.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_append_argv import resolve_append_lane_argv  # noqa: E501
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (
    LiveSublaneGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    LaunchPreflight,
    SublaneIntegrationPolicy,
    WorktreeLaunchDecision,
    decide_worktree_launch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    SublaneCreateRequest,
    SublaneLaneView,
    project_sublanes,
)


# ---------------------------------------------------------------------------
# Gateway readiness wait tuning (#13293). The pre-dispatch wait polls the gateway
# pane up to ``DEFAULT_GATEWAY_READY_PROBES`` times at
# ``DEFAULT_GATEWAY_READY_INTERVAL_SECONDS`` apart (≈ a 10s window by default) so a
# freshly-launched Codex TUI has time to boot before the queue-enter dispatch — the
# j#72677 / 5-example dispatch-loss failure mode was 100% "dispatch typed into a still-
# booting composer". The wait NEVER hard-blocks the queue-enter rail: an unconfirmed
# readiness degrades to a recorded ``gateway_ready=false`` and dispatches anyway.
# ---------------------------------------------------------------------------

DEFAULT_GATEWAY_READY_PROBES = 20
DEFAULT_GATEWAY_READY_INTERVAL_SECONDS = 0.5
#: How many rendered lines the live readiness probe captures from the gateway pane.
GATEWAY_READY_CAPTURE_LINES = 40


# ---------------------------------------------------------------------------
# Injected actuation operations port.
# ---------------------------------------------------------------------------


@runtime_checkable
class SublaneActuatorOps(Protocol):
    """Every side effect the live actuator needs, injected so tests drive fakes.

    Read probes (``is_git_workspace`` / ``worktree_exists``) mirror the #12604 git port.
    ``create_worktree`` is the single additive git mutation. ``append_lane_column`` stands
    up the cockpit-visible gateway + worker column for a worktree (binding the identity
    stamps). ``read_lane`` resolves the lane's :class:`SublaneLaneView` from the live pane
    inventory (used to adopt an existing lane and to confirm the created one on read-back).
    ``dispatch_implementation_request`` routes the governed handoff to the gateway pane and
    returns its exit code. There is intentionally no remove / kill / delete / merge method —
    the destructive half is gated and coordinator-owned.

    Optional capability (Redmine #13392): an adapter MAY additionally provide
    ``canonical_workspace_root() -> str`` — the registered workspace root the actuation is
    driven from. The use case reads it (via ``getattr``) to resolve the *lane runtime root*
    of a non-git (``LAUNCH_SKIP_NO_GIT``) lane, which has no worktree and so runs in the
    workspace root itself rather than the phantom ``--worktree`` path. Discovered via
    ``getattr`` and deliberately NOT part of this protocol so existing adapters / test fakes
    that only ever drive the Git path stay conformant (they fall back to the worktree path).

    Optional capability (Redmine #13378): an adapter MAY additionally provide
    ``heal_lane_column(worktree_path) -> None`` — a *creation-side* relaunch of the lane's
    missing managed slot(s) the use case invokes at most once when a dispatch fails and the
    gateway slot is gone on read-back (the measured vanish mode: an idle pre-session gateway
    agent killed by a host-level event such as an agent-CLI update). It is discovered via
    ``getattr`` and deliberately NOT part of this protocol, so existing adapters and test
    fakes stay conformant; the herdr adapter provides it (its session preparation is
    adopt-or-launch idempotent), the tmux adapter deliberately does not (a repeated
    ``cockpit append`` would duplicate the column, not adopt it). Still additive-only —
    a heal launches panes, it never closes one.
    """

    def is_git_workspace(self) -> bool: ...

    def worktree_exists(self, branch: str) -> bool: ...

    def create_worktree(
        self, *, branch: str, worktree_path: str, base_ref: Optional[str] = None
    ) -> None: ...

    def append_lane_column(self, worktree_path: str) -> None: ...

    def append_lane_argv(self, worktree_path: str) -> list[str]: ...

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]: ...

    def declare_adopted_lane_lifecycle(
        self, worktree_path: str, *, adopted: bool
    ) -> str:
        """Backfill an ADOPTED lane's lifecycle owner binding (Redmine #13809).

        A live-adopt (``adopted`` True) records the lane's owner row through the common
        declaration service, fail-closed and idempotent — closing the gap where a lane
        adopted onto a live pair skipped :meth:`append_lane_column` and stayed
        owner-rowless. A create (``adopted`` False) already declared via the append.

        Returns an outcome status from
        :mod:`...sublane_adopt_declaration` (``ADOPT_DECL_*``): only a value in
        ``ADOPT_DECL_PROCEED`` (a fresh / idempotent ``declared``, or the non-gated
        ``not_adopted``) authorizes the caller to proceed to dispatch; any other value
        leaves the lane owner-unbound and the caller must fail closed (R3-F3). An adapter
        whose backend does not manage adopt owner rows returns ``ADOPT_DECL_NOT_ADOPTED``.
        """
        ...

    def probe_gateway_ready(self, gateway_pane: str) -> bool:
        """One non-fatal readiness snapshot of the gateway pane (#13293).

        ``True`` when the gateway TUI is observed booted and rendered (its Codex
        foreground process is up and the pane has drawn content), so a queue-enter
        dispatch will land on a live composer rather than vanish into a still-booting
        one. Any read failure (pane gone, not-yet-agent process, blank capture) returns
        ``False`` — the caller polls this until ready or the bounded window elapses and
        never treats a probe failure as fatal.
        """
        ...

    def dispatch_implementation_request(
        self,
        *,
        issue: str,
        journal: str,
        gateway_pane: str,
        lane_label: str,
        upstream_coordinator: Optional[str],
        target_repo: str,
    ) -> int: ...


@dataclass(frozen=True)
class LiveSublaneActuatorOps:
    """Live adapter composing the real creation-side primitives for ``repo_root``.

    Git probes / additive ``git worktree add`` delegate to the #12604
    :class:`LiveSublaneGitOperations`. The cockpit append and the gateway dispatch drive the
    *existing CLI contract* the coordinator already runs by hand (``cockpit append
    --repo <worktree> --no-attach`` / ``handoff send ...``) through the composed argument
    parser, so this adapter reuses the proven, fully-defaulted code path instead of
    reconstructing a fragile Namespace. ``read_lane`` folds the live tmux pane inventory and
    matches the lane by repo-root.

    ``quiet_stdout`` (#13293): when set, the composed sub-CLI drives (cockpit append /
    handoff send) have their stdout redirected to *stderr* so the actuator's own stdout
    stays a single machine-readable JSON envelope — the coordinator's ``--json`` parse
    broke on the interleaved delivery-progress text (j#72677 evidence 2). Off by default,
    so the human-readable path keeps emitting the inner records inline on stdout.
    """

    repo_root: Path
    quiet_stdout: bool = False

    def _git(self) -> LiveSublaneGitOperations:
        return LiveSublaneGitOperations(repo_root=self.repo_root)

    def gateway_provider(self) -> str:
        """The runtime provider bound to the coordinator (gateway) role (Redmine #13569).

        The coordinator -> lane-gateway dispatch and the gateway readiness probe key on
        this, resolved from the repo-local ``RoleProviderBinding`` (default ``codex``,
        byte-identical) so a rebound gateway provider moves the ``--to`` receiver and the
        readiness check with no source edit. An unbound coordinator role raises
        :class:`WorkflowProviderUnresolved`, which the actuation turns into a fail-closed
        zero-dispatch.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            resolve_gateway_provider,
        )

        return resolve_gateway_provider(str(self.repo_root))

    def canonical_workspace_root(self) -> str:
        # #13392: the workspace root the actuation is driven from — the lane runtime root
        # of a non-git (skip_no_git) lane, which has no worktree and runs here.
        return str(self.repo_root)

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

    def _drive_cli(self, argv: list[str]) -> int:
        """Parse ``argv`` with the composed CLI parser and run its handler (live).

        Mirrors :func:`mozyo_bridge.application.cli.main`'s dispatch (parse + normalize
        paths + ``func``) so an appended pane / dispatch is byte-for-byte what the operator
        would get from the shell command — the same fully-defaulted Namespace, the same
        ``require_tmux`` gate, the same outcome emission. Imported lazily so the pure use
        case / tests never require the CLI infrastructure.
        """
        from mozyo_bridge.application.cli import build_parser, normalize_paths

        args = build_parser().parse_args(argv)
        args = normalize_paths(args)
        # #13293: in ``--json`` mode confine the inner CLI's delivery-progress text to
        # stderr so the actuator's stdout is a single JSON envelope; the record is still
        # visible for diagnosis, just off the machine-readable channel.
        if self.quiet_stdout:
            with contextlib.redirect_stdout(sys.stderr):
                return int(args.func(args))
        return int(args.func(args))

    def append_lane_argv(self, worktree_path: str) -> list[str]:
        # #13155: one resolver for live drive + preview; config from THIS repo (j#71880).
        return resolve_append_lane_argv(worktree_path, config_root=self.repo_root)

    def append_lane_column(self, worktree_path: str) -> None:
        rc = self._drive_cli(self.append_lane_argv(worktree_path))
        if rc != 0:
            raise RuntimeError(
                f"cockpit append failed for worktree {worktree_path!r} (exit {rc})"
            )

    def read_lane(self, worktree_path: str) -> Optional[SublaneLaneView]:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
            try_pane_lines,
        )

        rows = try_pane_lines() or []
        target = _normalize_path(worktree_path)
        target_base = Path(worktree_path).name
        lanes = [lane for lane in project_sublanes(rows) if lane.repo_root]
        # Prefer an exact repo-root match; the returned lane's *identity* is still
        # validated against the request by the use case, so this only narrows the
        # candidate.
        for lane in lanes:
            if _normalize_path(lane.repo_root) == target:
                return lane
        # Basename fallback only when it is unambiguous — a single lane shares the
        # worktree basename. Returning an arbitrary basename collision would hand a
        # different repo's lane to the identity check (or, worse, pass it); require
        # uniqueness here and still let the use case validate identity.
        basename_matches = [
            lane for lane in lanes if Path(lane.repo_root).name == target_base
        ]
        if len(basename_matches) == 1:
            return basename_matches[0]
        return None

    def declare_adopted_lane_lifecycle(
        self, worktree_path: str, *, adopted: bool
    ) -> str:
        # The tmux/cockpit backend does not gate adopt owner rows: the standard herdr
        # sublane live-adopt owner-row gap (Redmine #13809) is the herdr adapter's. The
        # cockpit lane's owner binding is declared through the cockpit-append CLI this
        # adapter drives; ``not_adopted`` signals the use case not to fail closed here.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
            ADOPT_DECL_NOT_ADOPTED,
        )

        return ADOPT_DECL_NOT_ADOPTED

    def probe_gateway_ready(self, gateway_pane: str) -> bool:
        # #13293: one non-fatal readiness snapshot — the gateway's foreground process is
        # the Codex TUI (strong per-receiver identity, the same check the queue-enter
        # rail uses) AND the pane has rendered content (a booted TUI has drawn its UI;
        # a blank capture is a pane still coming up). Any read failure (pane resolve /
        # capture raising, incl. the pane_resolver `die()` == SystemExit) is treated as
        # "not ready yet", never fatal — the caller polls this on a bounded window.
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (  # noqa: E501
            is_receiver_agent_process,
            pane_info,
        )
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (  # noqa: E501
            capture_pane,
        )

        try:
            gateway_provider = self.gateway_provider()
            info = pane_info(gateway_pane)
            if not is_receiver_agent_process(info.get("command", ""), gateway_provider):
                return False
            rendered = capture_pane(gateway_pane, GATEWAY_READY_CAPTURE_LINES)
        except (SystemExit, Exception):  # noqa: BLE001 — a probe never fails the actuation.
            return False
        return bool(rendered.strip())

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
        return self._drive_cli(argv)


def resolve_lane_runtime_root(
    ops: object, worktree_path: str, *, skip_no_git: bool
) -> str:
    """The filesystem / cwd / target-repo root the lane actually runs in (Redmine #13392).

    A non-git sublane skips ``git worktree add`` (``LAUNCH_SKIP_NO_GIT``) — the requested
    ``--worktree`` path is never created and carries no herdr identity segment, so the lane
    runs in the **workspace root** itself (the tmux-era directory-scaffold-lane semantics).
    When the launch skipped the worktree for that reason AND the ops adapter exposes the
    optional ``canonical_workspace_root()`` capability, this returns that root; every other
    case (a Git worktree lane, or an adapter without the capability) returns the requested
    ``worktree_path`` unchanged, so the Git path stays byte-for-byte the prior behaviour.
    """
    if skip_no_git:
        getter = getattr(ops, "canonical_workspace_root", None)
        if callable(getter):
            root = (getter() or "").strip()
            if root:
                return root
    return worktree_path or ""


def default_nongit_worktree_request(
    ops: object, request: SublaneCreateRequest, is_git: bool
) -> SublaneCreateRequest:
    """Default a non-git lane's omitted ``--worktree`` to the workspace root (Redmine #13432).

    In a non-git (directory-scaffold) workspace the lane has no worktree — it runs in the
    workspace root itself (#13392 論点1) — so an omitted ``--worktree`` collapses to the lane
    runtime root. Returns ``request`` unchanged for a Git workspace, when a worktree is
    already supplied, or when the ops adapter exposes no resolvable workspace root (the
    non-git plan does not require the field either way).
    """
    if is_git or (request.worktree_path or "").strip():
        return request
    root = resolve_lane_runtime_root(ops, "", skip_no_git=True)
    if not root:
        return request
    return replace(request, worktree_path=root)


def decide_create_launch(
    ops: object, request: SublaneCreateRequest, policy: SublaneIntegrationPolicy
) -> WorktreeLaunchDecision:
    """The #12604 worktree launch decision for a create request over the ops git probes.

    Shared by the plan-only (:class:`SublaneCreateUseCase`) and actuator
    (:class:`SublaneActuateUseCase`) create paths: probe git, resolve the identity /
    worktree-exists preflight facts, and ask the pure :func:`decide_worktree_launch`. A
    non-git workspace resolves to ``LAUNCH_SKIP_NO_GIT`` before the identity gate, so a
    #13432 non-git lane (optional ``--branch`` / ``--worktree``) is never blocked here.
    """
    is_git = ops.is_git_workspace()
    identity_known = bool(request.branch) and bool(request.worktree_path)
    worktree_exists = (
        ops.worktree_exists(request.branch) if is_git and identity_known else False
    )
    preflight = LaunchPreflight(
        is_git_workspace=is_git,
        worktree_exists=worktree_exists,
        branch_resolved=bool(request.branch),
        target_identity_known=identity_known,
    )
    return decide_worktree_launch(policy, preflight)


def resolve_create_identity(
    ops: object, request: SublaneCreateRequest
) -> tuple[SublaneCreateRequest, tuple[str, ...]]:
    """Resolve the create-request identity + its fail-closed ``missing_fields`` (Redmine #13432).

    The shared #13432 identity contract for both the plan-only and the actuator create use
    cases: a blank field under the Git-strict contract triggers one git probe; in a non-git
    workspace ``--branch`` / ``--worktree`` are optional and the omitted ``--worktree``
    defaults to the workspace root. A fully-supplied request never probes (so a later gate —
    work-unit / anchor — still short-circuits before any probe). Returns the (possibly
    worktree-defaulted) request and the remaining required-field gap.
    """
    missing = request.missing_fields()
    if not missing:
        return request, missing
    is_git = ops.is_git_workspace()
    request = default_nongit_worktree_request(ops, request, is_git)
    return request, request.missing_fields(is_git=is_git)


def _normalize_path(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return path.strip().rstrip("/")


__all__ = (
    "DEFAULT_GATEWAY_READY_PROBES",
    "DEFAULT_GATEWAY_READY_INTERVAL_SECONDS",
    "GATEWAY_READY_CAPTURE_LINES",
    "SublaneActuatorOps",
    "LiveSublaneActuatorOps",
    "resolve_lane_runtime_root",
    "decide_create_launch",
    "default_nongit_worktree_request",
    "resolve_create_identity",
)
