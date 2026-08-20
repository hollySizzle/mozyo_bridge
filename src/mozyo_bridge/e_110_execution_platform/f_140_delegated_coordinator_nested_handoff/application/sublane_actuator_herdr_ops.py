"""Herdr creation-side IO for ``sublane start --execute`` (#13377).

The adapter implements the same injected port as tmux while projecting each lane as
two managed agents in the shared project identity and dedicated lane-host placement.
Inventory reads, readiness probes and explicit-lane handoff dispatches are Herdr-native;
worktree creation is shared.  The boundary is additive only, and the binary resolves
only from the trusted environment.  Gateway dispatch also exposes a typed injection
stage so #15242 uncertainty cannot become a blind self-heal replay.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from mozyo_bridge.core.state.startup_transaction_fence import StartupTransactionFence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_ops import (
    GATEWAY_READY_CAPTURE_LINES,
    SublaneDispatchAttempt,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    injection_stage_for_outcome,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
    LAUNCH_CAUSE_GENERIC_FRESH,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_startup_projection import (
    project_sublane_startup,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_v1_replacement import (  # noqa: E501
    V1ReplacementDriver,
    V1ReplacementRequest,
    require_replacement_target_healthy,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_integration import (
    LiveSublaneGitOperations,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
    GATEWAY_ROLE,
    WORKER_ROLE,
    SublaneLaneView,
    _lane_state,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E501
    RuntimePlacementFingerprint,
    enforce_heal_postcondition,
    evaluate_heal_runtime_fence,
    production_placement_fingerprint,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    HerdrLauncherIncompatibleError,
    _list_rows,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    SublaneStartupObservation,
    SublaneLauncherIncompatibleError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_identity_binding import (  # noqa: E501
    finalize_lane_receipts_from_inventory,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_relaunch_authority_fence import (  # noqa: E501
    fence_update_relaunch_or_die,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    HerdrSessionStartError,
    _resolve_binary_or_die,
    herdr_workspace_segment,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (  # noqa: E501
    live_visible_reader,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_v1_replacement_binding import (  # noqa: E501
    prepare_actuator_lane_session,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_lane_resolution import (  # noqa: E501
    lane_slots,
    pair_colocation as _pair_colocation,
    resolve_lane_slots,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home
from mozyo_bridge.core.state.herdr_session_start_gate import session_start_gate
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E501
    evaluate_launcher_compatibility,
    evaluate_runtime_placement,
    run_dispatch_sender_preflight,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
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
    #: The durable-record journal that authorizes this lane's owner binding (Redmine
    #: #13681 W1). It travels the same ``--journal`` anchor the dispatch leg uses; a
    #: create with no journal is owner-unbound and writes no lifecycle row.
    journal: str = ""
    #: The delegation-geometry kind (``coordinator`` / ``delegated_coordinator`` /
    #: ``implementation``) the CREATING caller resolved from durable governance (Redmine
    #: #13647 T1b). Stored generation-bound on the lifecycle authority row at create, so a
    #: later heal resolves this lane's pane placement offline. Empty (every pre-#13647
    #: caller) records no kind, so the lane places by ``lane_class`` — which since Redmine
    #: #14568 resolves an undeclared class to the product default (``split: down``).
    lane_kind: str = ""
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
    #: Legacy diagnostic participant fields retained for reading old replacement debt.
    #: They never authorize a current launch; v4 direct action + generation-v2 does.
    replacement_assigned_name: str = ""
    replacement_old_locator: str = ""
    #: The typed launch cause this actuator's relaunch carries (Redmine #14741 j#97171).
    #: Defaults to the UNARMED cause, so every existing create / heal / recover caller that
    #: omits it is byte-invariant and queries no updater.
    replacement_launch_cause: str = LAUNCH_CAUSE_GENERIC_FRESH
    #: Historical target-only vocabulary retained for decoding old recovery intent.
    #: Current launch admission still requires v4/v2 terminal-bound authority.
    replacement_target_only: bool = False
    #: The VERIFIED delegated-parent lane stashed by the sender preflight (#15706) —
    #: the ONLY source the child's lifecycle declaration binds a parent from.
    verified_parent_lane_id: str = ""
    #: The typed delegated-sender refusal token stashed by the sender preflight
    #: (j#108076); empty on success and on every legacy refusal (byte-invariant).
    dispatch_sender_refusal_reason: str = ""

    # -- git probes / additive worktree add (backend-agnostic, reused verbatim) -----

    def _git(self) -> LiveSublaneGitOperations:
        return LiveSublaneGitOperations(repo_root=self.repo_root)

    def _resolve_runner(self) -> Runner:
        """The subprocess runner this adapter drives — the approved default when uninjected.

        ``runner`` is the tests' injection seam, so in production it is simply absent. Every
        consumer below is typed ``Runner`` (non-Optional) and *calls* what it is handed, so
        the ``None`` must be resolved to the approved ``subprocess.run`` here, at the one
        boundary that owns the seam. Resolving it per call site instead is what produced
        Redmine #14457 j#87901: the launcher-compatibility preflight was the one site that
        passed ``self.runner`` straight through, and the installed CLI's first
        ``sublane create --execute`` died with ``'NoneType' object is not callable`` inside
        the pre-mutation gate — a crash where a typed refusal or an admit belongs.
        """
        if self.runner is None:
            import subprocess

            return subprocess.run
        return self.runner

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

    # -- action-time pre-mutation preflights (bodies: `sublane_actuator_herdr_preflight`) --
    #
    # Each is a thin delegate to the cohesive sibling module that owns the answer. They are
    # one kind of thing — "what must be true before the first mutation" — and keeping the
    # bodies there is the reduction this adapter's module-health entry deferred (Redmine
    # #14258): with them extracted, this adapter is back under the threshold.

    def preflight_dispatch_sender(self) -> tuple[bool, str]:
        """Verify the command-shell sender before any lane mutation (#13613 / #15706)."""
        return run_dispatch_sender_preflight(self)

    def preflight_runtime_placement_gate(self) -> tuple[bool, str]:
        """Action-time runtime fingerprint gate — the mutation front door (#13705 R1-F1)."""
        return evaluate_runtime_placement(
            self.repo_root, self.runtime_fingerprint_reader
        )

    def preflight_launcher_compatibility(
        self, *, base_commit: str, lane_runtime_root: str, from_base_ref: bool
    ) -> tuple[bool, str, str]:
        """Verify the managed-launch launcher BEFORE the first worktree / process (#14258)."""
        return evaluate_launcher_compatibility(
            env=self.env,
            runner=self._resolve_runner(),
            timeout=self.timeout,
            repo_root=self.repo_root,
            store_home=mozyo_bridge_home(),
            replacement_action_id=self.replacement_action_id,
            committed_blob=self._git().committed_blob,
            base_commit=base_commit,
            lane_runtime_root=lane_runtime_root,
            from_base_ref=from_base_ref,
        )

    def resolve_base_commit(self, ref: str) -> str:
        """The lane base as a single immutable commit SHA, or ``""`` (Redmine #14258 R1)."""
        return self._git().resolve_commit(ref)

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

    def append_lane_column(self, worktree_path: str) -> SublaneStartupObservation:
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
        with session_start_gate(mozyo_bridge_home(), exclusive=False) as lease:
            result = self._prepare_lane_session(worktree_path, session_gate_lease=lease)
            return project_sublane_startup(result)

    def _prepare_lane_session(
        self,
        worktree_path: str,
        *,
        replacement_action_id: "str | None" = None,
        action_nonce: str = "",
        startup_fence: "StartupTransactionFence | None" = None,
        admission_lock_held: bool = False,
        providers: "Sequence[str] | None" = None,
        pair_order: "Sequence[str] | None" = None,
        session_gate_lease: object = None,
    ):
        """Run the production session composition and return its durable launch result.

        The raw result remains private because the v1 replacement adapter requires its
        exact action participant receipt. The public append boundary projects it into the
        actuator's typed startup observation; an adopted slot is never action-bound.
        """
        try:
            result = prepare_actuator_lane_session(
                worktree_path=worktree_path,
                config_repo_root=self.repo_root,
                providers=(
                    self._launch_providers()
                    if providers is None
                    else tuple(providers)
                ),
                # Redmine #14569 R2-F1: when the caller shrank `providers` to one leg, the
                # lane's stable `(gateway, worker)` order is the only thing that can still
                # say which role the declared ratio's share belongs to. It is passed in
                # already resolved — never re-resolved here, which would add a failure mode
                # to a launch whose caller has proven the binding once already.
                pair_order=tuple(pair_order) if pair_order else None,
                lane_id=self.lane_label,
                env=self.env,
                runner=self.runner,
                timeout=self.timeout,
                replacement_action_id=(
                    self.replacement_action_id
                    if replacement_action_id is None
                    else replacement_action_id
                ),
                action_nonce=action_nonce,
                startup_fence=startup_fence,
                admission_lock_held=admission_lock_held,
                launch_cause=self.replacement_launch_cause,
                # Redmine #13647 T1b (review j#85848 F1): the lane's delegation-geometry
                # kind must reach the FIRST launch. The lifecycle row this lane heals from
                # is declared only after this call returns, so a fresh create has no stored
                # kind yet — the caller's context is the only authority that exists at the
                # moment the panes are actually created.
                launch_context=self._launch_context(),
                session_gate_lease=session_gate_lease,
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
        # Redmine #14741 bracket 3 (j#96899 / C13): only HERE does the lane's lifecycle
        # row exist, so only here is there an actual generation/revision to bind to —
        # `prepare_session`'s own finalize runs strictly earlier (measured, j#97001).
        finalize_lane_receipts_from_inventory(store_home=mozyo_bridge_home(), result=result, env=self.env)
        return result

    def _launch_context(self):
        """This lane's caller-supplied :class:`LaneLaunchContext`, or ``None`` (#13647 T1b).

        Built from the create-time governance fact the coordinator asserted
        (``sublane create --lane-kind``). No kind -> ``None``, so the launch resolves its
        geometry through the ``lane_class`` layer exactly as it did pre-#13647 — byte-for-byte
        on THIS axis, but not a pre-#13646 launch: since Redmine #14568 an undeclared class
        lands on the product default (``split: down``); a heal of a lane whose row already
        records a kind resolves it from that row instead (and a disagreement between the two
        is refused at the launch admission, never silently resolved).
        """
        if self.lane_kind == "":
            return None
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_context import (  # noqa: E501
            LaneLaunchContext,
        )

        # UNTRIMMED (review j#85852 F1): the context's closed-vocabulary check is the
        # boundary; a padded token is a caller error to surface, never to repair here.
        return LaneLaunchContext(lane_kind=self.lane_kind)

    def _record_lane_metadata(self, worktree_path: str) -> None:
        """Upsert the lane's display-metadata record (best-effort, never raises)."""
        from mozyo_bridge.core.state.lane_metadata import record_lane_created
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
            declared_worktree_identity,
        )

        # The stable per-worktree metadata key (also the legacy pre-#13377 workspace
        # segment) — NOT the mzb1 workspace segment, which is the project identity now.
        # #13392: a non-git (directory scaffold) lane has no worktree — the use case runs it
        # in the shared workspace root itself, so the path-only ``wt_`` token would collide
        # across every lane on that root and one lane's record would overwrite the next; such
        # a lane is keyed by ``(root, lane_id)`` instead. The discriminant is the KIND of the
        # runtime root, probed on that root, and it is resolved ONCE for both declaration
        # writers by ``declared_worktree_identity`` — never re-derived here from
        # ``resolved == self.repo_root`` (Redmine #13933 j#81046 Decision 1 / #14478). That
        # proxy is a fact about the caller's ``--repo``: a create/adopt anchored at the lane's
        # own worktree collapsed it and minted a ``dl_`` token for a linked git worktree,
        # which the recovery reader (deriving ``wt_`` from the same root) can never match.
        token = declared_worktree_identity(worktree_path, self.lane_label or "")
        if token is None:
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
        """Declare this lane's owner binding via the create-declaration leaf (#13681 W1).

        The body lives in :func:`...sublane_create_lifecycle_declaration.
        declare_created_lane_lifecycle_for` (module-health leaf extraction, #13647 T1b /
        #15706), which reads the create-time governance facts — ``lane_kind`` and the
        verdict-verified ``verified_parent_lane_id`` — off this adapter.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_create_lifecycle_declaration import (  # noqa: E501
            declare_created_lane_lifecycle_for,
        )

        declare_created_lane_lifecycle_for(
            self, repo_workspace_id, worktree_identity=worktree_identity
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
            worktree_path=worktree_path,
            workspace_id=workspace_id,
            lane_id=lane_id,
            providers=providers,
            rows=rows,
            # #15774: the --lane-kind assertion reaches the adopt declaration too, so a
            # supersede-minted kind-empty row regains its kind via a governed re-create.
            lane_kind=self.lane_kind or "",
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

    def heal_lane_column(self, worktree_path: str, *, target_provider: "str | None" = None) -> None:
        with session_start_gate(mozyo_bridge_home(), exclusive=False) as lease:
            return self._heal_lane_column_under_gate(
                worktree_path,
                target_provider=target_provider,
                session_gate_lease=lease,
            )

    def _heal_lane_column_under_gate(self, worktree_path: str, *,
        target_provider: "str | None" = None, session_gate_lease: object) -> None:
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

        ``target_provider`` (Redmine #13933 R11 j#81429) scopes that postcondition to one
        owed participant so a single-leg convergence launch converges an approved partial
        pair without bypassing a live split; the pure, target-aware contract (raising the
        typed :class:`SublaneHealError`) is :func:`enforce_heal_postcondition`, and default
        ``None`` keeps the full-pair postcondition byte-identical.
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
            raise RuntimeError(f"lane heal fenced ({verdict.reason}): {verdict.detail}")

        # Update-authority fence: still inside the zero-relaunch pre-effect window.
        fence_update_relaunch_or_die(
            managed_pair,
            self.env,
            slots=existing,
            # A factory: with no live slot to read, the transport is never even resolved,
            # so a heal that needs no observation keeps its pre-#14741 dependencies.
            read_visible_factory=lambda: live_visible_reader(
                _resolve_binary_or_die(self.env), self._resolve_runner(), self.timeout
            ),
        )

        used_v1_binding = V1ReplacementDriver(home=mozyo_bridge_home()).run(
            V1ReplacementRequest(
                action_id=self.replacement_action_id,
                assigned_name=self.replacement_assigned_name,
                old_locator=self.replacement_old_locator,
                target_provider=target_provider,
                workspace_id=_ws,
                lane_id=self.lane_label,
                managed_pair=managed_pair,
                rows=rows,
                existing=existing,
                launch=lambda nonce, startup_fence: self._prepare_lane_session(
                    worktree_path,
                    replacement_action_id="",
                    action_nonce=nonce,
                    startup_fence=startup_fence,
                    admission_lock_held=True,
                    providers=(
                        (target_provider,)
                        if self.replacement_target_only and target_provider
                        else None
                    ),
                    pair_order=managed_pair,
                    session_gate_lease=session_gate_lease,
                ),
                target_only=self.replacement_target_only,
            )
        )
        if not used_v1_binding:
            result = self._prepare_lane_session(
                worktree_path,
                providers=(
                    (target_provider,)
                    if self.replacement_target_only and target_provider
                    else None
                ),
                pair_order=managed_pair,
                session_gate_lease=session_gate_lease,
            )
            require_replacement_target_healthy(result, self.replacement_action_id, target_provider, self.replacement_assigned_name)

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
        # ``target_provider=None`` keeps the full-pair postcondition; a scoped launch
        # converges an approved partial pair without bypassing a live split (#13933 R11).
        enforce_heal_postcondition(healed, managed_pair, target_provider=target_provider)

    def _live_rows(self) -> Sequence[Mapping[str, object]]:
        binary = _resolve_binary_or_die(self.env)
        return _list_rows(binary, self._resolve_runner(), self.timeout)

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
        """Compatibility port for the extracted shared lane-slot resolver."""
        return lane_slots(workspace_id, lane_id, rows, managed)

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
        return resolve_lane_slots(worktree_path, self.lane_label, rows, managed)

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
            transport = HerdrCliTransport(
                binary, runner=self._resolve_runner(), timeout=self.timeout
            )
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

        ``--target-lane`` names the stable slot, ``--target-repo`` remains the cwd
        gate, and the non-``%pane`` target selects Herdr rather than tmux.
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

    def _drive_cli_result(self, argv: list[str]) -> SublaneDispatchAttempt:
        """Run the composed CLI and retain its published injection stage."""
        from mozyo_bridge.application.cli import build_parser, normalize_paths

        args = None
        try:
            args = normalize_paths(build_parser().parse_args(argv))
            if self.quiet_stdout:
                with contextlib.redirect_stdout(sys.stderr):
                    rc = args.func(args)
            else:
                rc = args.func(args)
        except SystemExit as exc:
            code = exc.code
            rc = code if type(code) is int and code != 0 else 1
        return SublaneDispatchAttempt.typed(
            rc,
            injection_stage_for_outcome(
                getattr(args, "delivery_outcome", None)
            ),
        )

    def _drive_cli(self, argv: list[str]) -> int:
        """Legacy int result retained for existing direct callers."""
        return self._drive_cli_result(argv).exit_code

    def dispatch_implementation_request_result(
        self,
        **kwargs,
    ) -> SublaneDispatchAttempt:
        """Typed gateway dispatch used by the sublane actuator."""
        return self._drive_cli_result(self.dispatch_argv(**kwargs))

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
        return self.dispatch_implementation_request_result(
            issue=issue,
            journal=journal,
            gateway_pane=gateway_pane,
            lane_label=lane_label,
            upstream_coordinator=upstream_coordinator,
            target_repo=target_repo,
        ).exit_code

__all__ = (
    "HERDR_LANE_PROVIDERS",
    "HerdrSublaneActuatorOps",
)
