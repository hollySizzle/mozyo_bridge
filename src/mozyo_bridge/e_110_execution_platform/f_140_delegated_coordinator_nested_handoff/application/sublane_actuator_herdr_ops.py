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
slots land in a single sublane host workspace separate from the coordinator pair's project
workspace, so the herdr workspace count is a constant "project 1 + host 1" — never scaling
with the lane count. This supersedes the #13331 j#73314 per-lane ``wt_<hash>`` workspace
(option A), which survives read-side as legacy compatibility only.

:class:`HerdrSublaneActuatorOps` implements the SAME
:class:`~...application.sublane_actuator_ops.SublaneActuatorOps` port the tmux adapter
does, so the pure fail-closed :class:`~...application.sublane_actuator_use_case.SublaneActuateUseCase`
choreography is unchanged — only the side effects differ:

* ``create_worktree`` — the identical additive #12604 git op (backend-agnostic worktree add);
* ``append_lane_column`` — instead of a cockpit append, :func:`prepare_session` on the lane
  worktree with ``lane_id=lane_label``, launching the codex gateway + claude worker as lane
  slots of the project identity, placed in the dedicated sublane host workspace (#13380);
* ``read_lane`` — resolves the lane from the **live herdr inventory** (``agent list`` mzb1
  decode, #13247) filtered to ``(project workspace, lane_label)``, not a tmux snapshot; a
  pre-#13377 lane resolves through its legacy ``wt_<hash>`` slots (compatibility read, so a
  live legacy lane is never double-created);
* ``probe_gateway_ready`` — a non-fatal boot-readiness check of the gateway agent: live in
  the inventory AND rendered (``agent read`` returns non-blank text, #13378); the send
  rail's turn-start observation + Enter-resend (#13322) stays the landing net;
* ``dispatch_implementation_request`` — the governed ``handoff send`` to the gateway. The
  gateway is a lane slot of the SAME *mozyo* workspace identity (its #13380 host placement
  is irrelevant to routing, which matches on the mzb1 decode), so the coordinator→gateway
  leg is an **explicit-lane** send: ``--target-lane <lane_label>`` (the j#73613 explicit
  lane field — never an all-lane scan) plus ``--target-repo <lane-worktree>`` as the
  repo/cwd gate and a non-``%pane`` herdr target so the send rides the herdr rail (#13320).

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
from typing import Callable, Mapping, Optional, Sequence

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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E501
    RuntimePlacementFingerprint,
    evaluate_heal_runtime_fence,
    production_placement_fingerprint,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    _tab_id_of_row,
    _workspace_prefix,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    HerdrLauncherIncompatibleError,
    _list_rows,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneLauncherIncompatibleError,
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


def _pair_colocation(
    slots: Mapping[str, tuple[str, str]],
    pair: "tuple[str, str] | None" = None,
) -> Optional[bool]:
    """Whether a live gateway/worker pair shares one placement container (#13705).

    ``slots`` maps role -> ``(locator, placement_key)``. ``True`` when both slots are
    live and share one ``(herdr_workspace, tab_id)`` key (an operable same-tab pair),
    ``False`` when both are live but the keys differ (a ``pair_split``), and ``None``
    when fewer than two slots are live (the ordinary single-provider heal — not
    applicable, so the fence never blocks on it).

    ``pair`` is the (gateway, worker) provider pair to check (Redmine #13569 R2-F2): the
    heal fence passes the binding-resolved pair so a rebound lane's fence keys on its own
    providers, not the fixed ``codex/claude``. ``None`` uses the built-in pair
    (``GATEWAY_ROLE`` / ``WORKER_ROLE``), byte-identical.
    """
    gw_role, wk_role = pair if pair is not None else (GATEWAY_ROLE, WORKER_ROLE)
    gateway = slots.get(gw_role)
    worker = slots.get(wk_role)
    if gateway is None or worker is None:
        return None
    return gateway[1] == worker[1]


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
    #: The durable-record journal that authorizes this lane's owner binding (Redmine
    #: #13681 W1). It travels the same ``--journal`` anchor the dispatch leg uses; a
    #: create with no journal is owner-unbound and writes no lifecycle row.
    journal: str = ""
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    runner: Optional[Runner] = None
    providers: tuple[str, ...] = HERDR_LANE_PROVIDERS
    quiet_stdout: bool = False
    timeout: float = COMMAND_TIMEOUT_SECONDS
    #: The running build's placement provenance (Redmine #13705). The default is
    #: THIS runtime's real fingerprint (``__version__`` + advertised placement
    #: contracts); tests inject an older / incompatible one to exercise the
    #: mutating-heal runtime fence. Only the heal path reads it.
    runtime_placement_fingerprint: RuntimePlacementFingerprint = field(
        default_factory=production_placement_fingerprint
    )
    #: The action-time runtime fingerprint reader for the mutation front-door gate
    #: (Redmine #13705 R1-F1). Returns a :func:`run_runtime_fingerprint`-shaped dict
    #: (active-vs-repo-local-source drift). ``None`` builds the live reader from
    #: ``repo_root``; tests inject a reader returning a drift fingerprint to prove the
    #: front door goes zero-write on a source/installed skew.
    runtime_fingerprint_reader: Optional[Callable[[], dict]] = None
    #: The #13806 tranche D replacement ``action_id`` a worker-recovery relaunch carries into
    #: the fresh process's startup self-attestation (empty on a normal heal = byte-invariant).
    replacement_action_id: str = ""

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

    def preflight_runtime_placement_gate(self) -> tuple[bool, str]:
        """Action-time runtime fingerprint gate — the mutation front door (#13705 R1-F1).

        Verifies BEFORE any worktree / lane side effect that the action-time runtime is
        not a source/installed skew that would place the gateway/worker pair incorrectly.
        Reads a :func:`run_runtime_fingerprint` result (active loaded package vs
        repo-local source) and blocks ONLY when the active runtime is missing the
        same-tab placement behavior the source ships — the exact skew that split the
        #13441 lane (the pure :func:`evaluate_mutation_placement_gate` policy).

        This is the achievable authority the R1 review asked for: the OFFICIAL mutating
        front door goes zero-write on an installed/source fingerprint mismatch, detected
        by a REAL active-vs-source probe (not a hard-coded capability). It cannot stop a
        runtime that predates all fence code (no code we ship runs there); that residual
        is closed by the #13524 reinstall fingerprint gate. A run with no repo-local
        source to compare is unverifiable and allowed (again the reinstall gate covers it).
        Any read failure fails closed (a fingerprint that cannot be established must not
        greenlight a mutation).
        """
        from mozyo_bridge.application.doctor_runtime import (
            evaluate_mutation_placement_gate,
        )

        reader = self.runtime_fingerprint_reader
        if reader is None:
            import argparse

            from mozyo_bridge.application.doctor_runtime import run_runtime_fingerprint

            def reader() -> dict:
                return run_runtime_fingerprint(
                    argparse.Namespace(repo=str(self.repo_root))
                )

        try:
            fingerprint = reader()
        except Exception as exc:  # noqa: BLE001 — an unresolvable fingerprint fails closed.
            return False, (
                f"the action-time runtime fingerprint could not be established ({exc}); "
                "refuse to actuate a lane from a runtime of unverifiable provenance "
                "(Redmine #13705)"
            )
        return evaluate_mutation_placement_gate(fingerprint)

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
                # #13806 R2-F2: carry the recovery's action_id into the fresh startup attestation.
                replacement_action_id=self.replacement_action_id,
            )
        except HerdrLauncherIncompatibleError as exc:
            # Redmine #13847: typed launcher-compat error (not a generic pane-create failure),
            # so the use case reports `launcher_runtime_incompatible` with schema-upgrade recovery.
            raise SublaneLauncherIncompatibleError(str(exc), reason=exc.reason) from exc
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
        # Redmine #13681 W1: declare the lane's owner binding in the lifecycle
        # component, adjacent to (and keyed differently from) the display-metadata
        # upsert. Best-effort, exactly like the metadata write — but the two axes are
        # separate: metadata is a display join that *revives a tombstone*, while the
        # lifecycle row is CAS'd owner authority the roster (W4) and send gate (W3)
        # fail-closed against. The live lane unit `(project workspace, lane_label)` is
        # the same unit those projections join on. The `token` computed above is the
        # lane's canonical worktree identity — recorded in the fail-closed lifecycle row
        # (Redmine #13754) so `retire --execute` can prove the caller's `--worktree`
        # belongs to this lane before closing.
        self._declare_lane_lifecycle(repo_workspace_id, worktree_identity=token)

    def _declare_lane_lifecycle(
        self, repo_workspace_id: str, *, worktree_identity: str = ""
    ) -> None:
        """Declare this lane's owner binding (best-effort, never raises; Redmine #13681 W1).

        A declare needs both the lane unit identity `(repo_workspace_id, lane_label)`
        and the durable decision anchor `(--journal)`. A create with no journal — or an
        unresolved workspace segment / lane label — is **owner-unbound**: no lifecycle
        row is written, and the lane reads as owner-unbound at the roster and send gate.
        That is a fail-closed gap surfaced honestly downstream, never a guessed owner.

        ``worktree_identity`` (Redmine #13754) is the lane's canonical worktree token,
        recorded here so ``retire --execute`` can prove the caller's ``--worktree``
        belongs to this lane. It is the SAME token the display-metadata record is keyed
        on, computed once at the create boundary so writer and reader cannot drift.

        The write is best-effort like the metadata upsert: a store error never breaks the
        actuation. A re-run (self-heal, #13378) re-declares and is refused idempotently
        (`already_declared`); a create for an issue another lane still actively owns is
        refused (`owner_conflict`) and the recovery lane stays unbound until an explicit
        `sublane supersede` hands ownership over (W2) — both are correct, not errors.
        """
        journal = _norm(self.journal)
        issue = _norm(self.issue)
        lane = _norm(self.lane_label)
        workspace = _norm(repo_workspace_id)
        if not (journal and issue and lane and workspace):
            return
        from mozyo_bridge.core.state.lane_lifecycle import (
            DecisionPointer,
            DecisionPointerError,
            LaneLifecycleError,
            LaneLifecycleKey,
            LaneLifecycleStore,
        )

        try:
            key = LaneLifecycleKey(workspace, lane)
            decision = DecisionPointer(
                source="redmine", issue_id=issue, journal_id=journal
            )
        except (DecisionPointerError, ValueError):
            # A non-decimal issue / journal cannot anchor a re-readable decision — skip
            # rather than write an owner row no recovery could ever resolve.
            return
        try:
            LaneLifecycleStore().declare_active(
                key,
                decision=decision,
                issue_id=issue,
                worktree_identity=worktree_identity,
            )
        except (LaneLifecycleError, DecisionPointerError, OSError) as exc:
            print(
                f"warning: lane lifecycle declare skipped ({type(exc).__name__}); "
                "lane reads as owner-unbound",
                file=sys.stderr,
            )

    def declare_adopted_lane_lifecycle(
        self, worktree_path: str, *, adopted: bool
    ) -> str:
        """Backfill an ADOPTED lane's owner binding via the common service (Redmine #13809).

        The standard live-adopt path skips :meth:`append_lane_column`, so it never reached
        the create-path :meth:`_declare_lane_lifecycle` and the adopted lane stayed
        owner-rowless (the ``original_identity_unknown`` that blocks ``sublane hibernate``).
        Only an ``adopted`` lane needs this — the create path already declared via append.
        This resolves the live inventory unit and hands the RAW rows to
        :func:`...sublane_adopt_declaration.declare_adopted_owner_row`, which does the
        raw-multiplicity / liveness / startup-attestation gate + fail-closed idempotent
        declaration. Returns the outcome status the use case propagates (only
        ``ADOPT_DECL_DECLARED`` authorizes proceeding to dispatch, R3-F3); a store error is
        logged but never breaks the actuation.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
            ADOPT_DECL_DECLARE_ERROR,
            ADOPT_DECL_NOT_ADOPTED,
            ADOPT_DECL_UNREADABLE,
            declare_adopted_owner_row,
            owner_bound_or,
        )

        if not adopted:
            return ADOPT_DECL_NOT_ADOPTED
        try:
            rows = self._live_rows()
        except HerdrSessionStartError:
            # Inventory unreadable at declaration time (herdr down / unconfigured binary):
            # owner-unbound UNLESS the state DB (separate authority) confirms this lane
            # already owns the issue. Never proceed on inference (Redmine #13810 R4-F3).
            return self._adopt_unreadable_outcome()
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            WorkflowProviderUnresolved,
        )

        try:
            providers = self._launch_providers()
        except WorkflowProviderUnresolved:
            return self._adopt_unreadable_outcome()
        workspace_id, lane_id, _slots = self._resolve_lane_slots(
            worktree_path, rows, providers
        )
        outcome = declare_adopted_owner_row(
            journal=self.journal or "",
            issue=self.issue or "",
            lane_label=self.lane_label or "",
            repo_root=self.repo_root,
            worktree_path=worktree_path,
            workspace_id=workspace_id,
            lane_id=lane_id,
            providers=providers,
            rows=rows,
        )
        if outcome == ADOPT_DECL_DECLARE_ERROR:
            print(
                "warning: adopted lane lifecycle declare skipped (store error); "
                "lane reads as owner-unbound",
                file=sys.stderr,
            )
        return outcome

    def _adopt_unreadable_outcome(self) -> str:
        """The adopt outcome when the live inventory could not be read at declaration time.

        Redmine #13810 R4-F3: an unreadable inventory / unresolved provider pair leaves the
        lane owner-unbound and must block dispatch — UNLESS the state DB (a separate
        authority from the live herdr inventory) confirms this lane already owns the issue.
        The ownership read is keyed on the SAME ``herdr_workspace_segment(self.repo_root)`` /
        ``lane_label`` unit the create-path declaration used.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
            ADOPT_DECL_UNREADABLE,
            owner_bound_or,
        )

        try:
            workspace = herdr_workspace_segment(self.repo_root)
        except (OSError, ValueError):
            workspace = ""
        return owner_bound_or(
            ADOPT_DECL_UNREADABLE,
            workspace=workspace,
            issue=self.issue or "",
            lane=self.lane_label or "",
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

        Runtime / placement-contract fence (Redmine #13705). BEFORE any pane side
        effect the heal proves the running runtime can honour the lane's same-tab
        pair placement contract: an incompatible / unknown-provenance runtime (a
        source/installed skew — the measured incident healed a #13411 lane from an
        older 0.10.0 runtime lacking the contract, splitting the pair across tabs)
        fails closed here with zero ``workspace`` / ``tab`` / ``agent`` write. An
        already-split live pair is likewise refused (a heal cannot repair a live
        split). After a compatible relaunch a same-tab **postcondition** verifies
        both slots share one ``(herdr_workspace, tab_id)`` container, so a heal that
        nonetheless split the pair is surfaced rather than reported healed.
        """
        # Preflight fence — read-only, BEFORE any side effect. An unreadable inventory
        # is fail-closed (Redmine #13705 R1-F3): the pair invariant is unverifiable, so
        # a mutating heal refuses rather than proceeding on an unknown topology (never
        # `rows=()` → proceed).
        try:
            rows = self._live_rows()
        except HerdrSessionStartError as exc:
            raise RuntimeError(
                f"lane heal preflight fenced (inventory_unreadable): the live herdr "
                f"inventory could not be read ({exc}); refuse to mutate an existing "
                "lane without verifying its pair placement (Redmine #13705)"
            ) from exc
        # Resolve the lane's binding-launched (gateway, worker) provider pair ONCE and key
        # every heal path on it (Redmine #13569 R2-F2 invariant): the read-back, the
        # pre-condition fence, and the post-condition verify the SAME pair a rebound lane
        # launched, never the fixed `codex/claude`. An unresolved binding leaves the pair
        # placement unverifiable, so the mutating heal fails closed (Redmine #13705).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            WorkflowProviderUnresolved,
        )

        try:
            managed_pair = self._launch_providers()
        except WorkflowProviderUnresolved as exc:
            raise RuntimeError(
                f"lane heal preflight fenced (provider_unresolved): the lane's gateway/"
                f"worker provider binding could not be resolved ({exc}); refuse to mutate "
                "an existing lane without verifying its pair placement (Redmine #13705)"
            ) from exc
        _ws, _lane, existing = self._resolve_lane_slots(worktree_path, rows, managed_pair)
        verdict = evaluate_heal_runtime_fence(
            self.runtime_placement_fingerprint,
            existing_pair_colocated=_pair_colocation(existing, managed_pair),
        )
        if not verdict.ok:
            raise RuntimeError(
                f"lane heal fenced ({verdict.reason}): {verdict.detail}"
            )

        self.append_lane_column(worktree_path)

        # Same-tab postcondition (Redmine #13705 R1-F3): the compatible heal must have
        # restored the gateway/worker pair in ONE `(herdr_workspace, tab_id)` container.
        # Unknown is NOT success — an unreadable post-heal inventory, a missing slot, or
        # a split all fail closed. A legacy loose pair shares the KNOWN key `(wN, "")`
        # (co-located), so it is never conflated with an unreadable / unknown placement.
        try:
            post_rows = self._live_rows()
        except HerdrSessionStartError as exc:
            raise RuntimeError(
                "lane heal postcondition failed (inventory_unreadable): the live herdr "
                f"inventory could not be read after the relaunch ({exc}); the same-tab "
                "pair placement is unverified — fail-closed (Redmine #13705)"
            ) from exc
        _ws, _lane, healed = self._resolve_lane_slots(
            worktree_path, post_rows, managed_pair
        )
        if _pair_colocation(healed, managed_pair) is not True:
            gateway = healed.get(managed_pair[0])
            worker = healed.get(managed_pair[1])
            raise RuntimeError(
                "lane heal postcondition failed: the gateway "
                f"{gateway[0] if gateway else '<none>'} and worker "
                f"{worker[0] if worker else '<none>'} are not confirmed in one "
                f"placement container after the relaunch (gateway placement "
                f"{gateway[1] if gateway else None}, worker placement "
                f"{worker[1] if worker else None}); the pair is split or incomplete "
                "(Redmine #13705) — fail-closed"
            )

    def _live_rows(self) -> Sequence[Mapping[str, object]]:
        binary = _resolve_binary_or_die(self.env)
        runner = self.runner
        if runner is None:
            import subprocess

            runner = subprocess.run
        return _list_rows(binary, runner, self.timeout)

    def observe_pair_attestation(self, worktree_path: str):
        """Redmine #13847: observe both slots' post-launch self-attestation (read-only).

        The create post-launch gate / recovery verify decide on the pure ``(gateway,
        worker)`` SlotAttestation pair this returns; the store/inventory IO is the cohesive
        :func:`observe_lane_pair_attestation` helper (keyed on the binding-resolved pair).

        Returns ``None`` when the managed launch did NOT wrap the provider (no attest
        launcher resolved — the #13637 byte-invariant fallback): an unwrapped launch
        produces no self-attestation by design, so there is nothing to confirm and the
        create gate skips (the adopt / doctor read side stays the fail-closed net). With a
        resolved launcher the capability preflight (item 2) has already proven it
        schema-compatible, so a wrapped launch's attestation is meaningful to confirm.
        """
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
            resolve_attest_launcher,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_pair_attestation_ops import (  # noqa: E501
            observe_lane_pair_attestation,
        )

        if not resolve_attest_launcher(self.env):
            return None
        gateway_provider, worker_provider = self._launch_providers()
        return observe_lane_pair_attestation(
            worktree_path=worktree_path,
            gateway_provider=gateway_provider,
            worker_provider=worker_provider,
            list_rows=self._live_rows,
            resolve_slots=self._resolve_lane_slots,
        )

    def _lane_slots(
        self,
        workspace_id: str,
        lane_id: str,
        rows: Sequence[Mapping[str, object]],
        managed: "tuple[str, ...] | None" = None,
    ) -> dict[str, tuple[str, str]]:
        """Map ``{role: (locator, placement_key)}`` for the lane unit's managed slots.

        Decodes each ``agent list`` row's ``mzb1`` name (#13247); a row is a managed lane
        slot iff it decodes to ``(workspace_id, lane_id, role)``. Undecodable / foreign
        rows are skipped. A row that decodes to a managed slot but carries no live locator is
        skipped (a blank target is never a resolved pane), so the use case reads it as a
        missing pane and fails closed rather than adopting a locator-less lane.

        ``managed`` is the (gateway, worker) provider pair the lane is expected to run
        (Redmine #13569 R2-F2). It must match the pair :meth:`_launch_providers` launched,
        so a lane whose binding rebound its providers is READ BACK by its own providers
        rather than being judged "no lane" against a fixed ``codex/claude``. ``None`` uses
        the built-in pair, byte-identical.

        ``placement_key`` is the slot's ``(herdr_workspace, tab_id)`` container
        (Redmine #13705): two slots sharing one key are an operable same-tab pair; a
        differing key is the ``pair_split`` a runtime skew produced. Keeping both the
        locator and the placement key in the value lets the #13569 provider read-back and
        the #13705 pair-split fence resolve off one pass over the managed pair.
        """
        managed_pair = managed if managed is not None else (GATEWAY_ROLE, WORKER_ROLE)
        want_lane = _norm_lane(lane_id)
        slots: dict[str, tuple[str, str]] = {}
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
            if identity.role not in managed_pair:
                continue
            locator = _agent_locator(row)
            if not locator:
                continue
            # First live locator wins; a duplicate managed name is a session-start
            # fail-closed condition, not this read's to resolve.
            if identity.role not in slots:
                slots[identity.role] = (
                    locator,
                    (_workspace_prefix(locator), _tab_id_of_row(row)),
                )
        return slots

    def _resolve_lane_slots(
        self,
        worktree_path: str,
        rows: Sequence[Mapping[str, object]],
        managed: "tuple[str, ...] | None" = None,
    ) -> tuple[str, str, dict[str, tuple[str, str]]]:
        """``(workspace_id, lane_id, {role: (locator, placement_key)})`` for the lane.

        The shared resolution both :meth:`read_lane` and the #13705 heal fence use:
        the shared project-workspace unit ``(project ws, lane_label)`` first, then the
        legacy ``wt_<hash>`` default-lane compatibility unit. ``("", "", {})`` when the
        worktree resolves no segment (the caller treats that as an absent lane).

        ``managed`` is the binding-resolved (gateway, worker) provider pair (Redmine
        #13569 R2-F2), threaded to BOTH the shared and legacy read-back so every path — and
        the #13705 pair-split fence built on the resulting slots — keys on the same pair a
        rebound lane actually launched. ``None`` uses the built-in pair, byte-identical.
        """
        try:
            resolved = Path(worktree_path).expanduser().resolve()
            workspace_id = herdr_workspace_segment(resolved)
        except (OSError, ValueError):
            return "", "", {}
        lane_id = _norm_lane(self.lane_label)
        slots: dict[str, tuple[str, str]] = {}
        if workspace_id:
            slots = self._lane_slots(workspace_id, lane_id, rows, managed)
        if not slots:
            # Legacy compatibility (pre-#13377): the lane's agents live in their own
            # `wt_<hash>` workspace under the default lane.
            legacy_ws = derive_lane_workspace_token(str(resolved))
            legacy_slots = self._lane_slots(legacy_ws, DEFAULT_LANE, rows, managed)
            if legacy_slots:
                workspace_id, lane_id, slots = legacy_ws, DEFAULT_LANE, legacy_slots
        return workspace_id, lane_id, slots

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

        A live pair whose two slots do NOT share one ``(herdr_workspace, tab_id)``
        container reads as ``pair_split`` (Redmine #13705), not ``active``.
        """
        try:
            rows = self._live_rows()
        except HerdrSessionStartError:
            # Inventory unavailable (binary unset / herdr down). Fail-safe to None (the use
            # case reads that as "lane not visible" and fails closed on the read-back)
            # rather than fabricating a partial view — a genuinely down herdr also fails
            # closed on the append step, so this never adopts a lane it cannot see.
            return None
        # The read-back recognizes the SAME provider pair the launch created (Redmine
        # #13569 R2-F2), resolved from the binding — a rebound lane is read back by its own
        # providers, not judged "no lane" against a fixed pair. The one shared resolver
        # (#13705) threads that pair through BOTH the shared-workspace and legacy read-back,
        # so the provider read-back and the pair-split fence agree on the same pair. Fail-safe
        # to None if the binding cannot resolve (mirrors the inventory-unavailable path).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
            WorkflowProviderUnresolved,
        )

        try:
            gateway_provider, worker_provider = self._launch_providers()
        except WorkflowProviderUnresolved:
            return None
        managed_pair = (gateway_provider, worker_provider)
        workspace_id, lane_id, slots = self._resolve_lane_slots(
            worktree_path, rows, managed_pair
        )
        gateway = slots.get(gateway_provider)
        worker = slots.get(worker_provider)
        if not gateway and not worker:
            return None
        return SublaneLaneView(
            workspace_id=workspace_id,
            lane_id=lane_id,
            lane_label=self.lane_label,
            issue=self.issue or None,
            branch=None,
            repo_root=str(worktree_path),
            gateway_pane=gateway[0] if gateway else None,
            worker_pane=worker[0] if worker else None,
            state=_lane_state(
                gateway[0] if gateway else None,
                worker[0] if worker else None,
                gateway_placement=gateway[1] if gateway else None,
                worker_placement=worker[1] if worker else None,
            ),
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
