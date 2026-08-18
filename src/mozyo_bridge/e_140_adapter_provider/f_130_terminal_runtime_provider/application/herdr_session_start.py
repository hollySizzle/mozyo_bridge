"""herdr session-start one-command — the durable-name write side (Redmine #13261).

`mozyo-bridge herdr session-start` is the opt-in helper that prepares a **pure herdr
session** for mozyo handoff routing. Nothing in the codebase ever *wrote* a durable
herdr name before this (the #13175 PoC did ``agent rename`` by hand); this command
mints them so the herdr-native target resolution (#13261 read side) has stable
identities to resolve against.

Flow (per requested provider agent, ``claude`` / ``codex``):

1. resolve the herdr binary from the **trusted environment** — the explicit
   ``MOZYO_HERDR_BINARY`` then an executable ``herdr`` on the trusted ``PATH``
   (Redmine #13496; absolute PATH components only, realpath / executable verified);
   unresolvable / ambiguous fails closed (never a repo-local or cwd binary);
2. ensure the workspace is registered (``register_workspace`` / anchor reuse) and take
   its ``workspace_id`` — the workspace_registry schema is unchanged (#11425);
3. mint the durable name ``encode_assigned_name(workspace_id, provider, lane)`` (#13247);
4. **idempotency + composite liveness:** if a *live* agent already carries that exact
   assigned name, *adopt* it (no launch). Liveness is a composite judgment, not a bare name
   match (Redmine #13518 j#75329): a host-restart shell residue (name survives, no detected
   agent) is classified :data:`SLOT_STALE` and surfaced read-only, never blind-adopted. A
   duplicated assigned name (more than one live agent) fails closed rather than corrupting;
5. otherwise launch the agent as a herdr-managed pane with the durable name applied
   **at start** and the self-identity vars injected via ``--env``.

Duplicate requested ``(provider, lane)`` slots fail closed **before any side effect**
(spec §5 slot-uniqueness) so the same durable name is never minted twice.

Pane-bound launch and root occupation (#13330, #15101, #15227, #15702, #15705)
-------------------------------------------------------------------------
Herdr 0.8 no longer places a process while starting it. A cold run mints the workspace
or tab FIRST-SLOT-PREPARED (born with the first launch slot's cwd / env — #15702 lane
tabs, #15705 default-lane workspaces) so its root IS the first prepared pane; later
slots split beside it. A root this run did not occupy stays preserved — no terminal
generation pin, never closed by locator (#15227). A failure retains typed startup debt
for the public rollback path; it never scans for or closes an unowned shell pane.

Placement: dedicated sublane host workspace (Redmine #13380)
------------------------------------------------------------
The mzb1 identity model is #13377's (design j#73613): a lane's slots are
``mzb1_<project-ws>_<role>_<lane>`` and the workspace segment stays the project
identity. The herdr *placement* however splits (#13380, owner intent #13377
j#73654 "サブレーン専用ウィンドウ"): the default lane (coordinator pair) lives in
the project workspace while every lane slot lands in a single dedicated sublane
host workspace — a constant "project 1 + host 1", never scaling with lanes. The
join rule lives in :func:`_launch_target_for_lane`; the host is minted on demand
with an operator-readable label (:func:`_host_workspace_label`, cosmetic only)
and is not retire authority. A lane-zero host may retain a cosmetic unbound root;
managed-slot retirement neither closes that root nor requires workspace disappearance.

The command is explicit opt-in and is **not** coupled to the ``terminal_transport``
backend flag: you may prepare herdr identities without selecting the herdr transport,
and vice versa (documented in ``vibes/docs/specs/herdr-native-identity.md``). In pure
herdr operation you run both.

Herdr 0.8 requires this preflighted prepared-pane launch surface::

    herdr pane split <ANCHOR> --direction <right|down> --cwd <PATH> [--env KEY=VALUE]... --no-focus
    herdr pane run <PANE_ID> '<provider>() { exec <ACTION_PRIVATE_SHIM> "$@"; }'
    herdr agent start <NATIVE_NAME> --kind <claude|codex> --pane <PANE_ID> -- <argv...>
``pane run`` binds the canonical provider name only after login-shell startup. Herdr's
short native name is collision-checked and bound to ``mzb1_...``; receipts fail closed.

Tests exercise the argv + JSON parsing through an injected subprocess ``runner`` (no
live herdr binary); the end-to-end live smoke stays the coordinator's post-review step.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from mozyo_bridge.core.state.startup_transaction_fence import StartupTransactionFence
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
        AgentLaunchConfig,
        LanePlacementConfig,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_context import (  # noqa: E501
        LaneLaunchContext,
    )

from mozyo_bridge.core.state.workspace_registry import (
    ANCHOR_LEGACY_RELATIVE,
    ANCHOR_RELATIVE,
    _is_linked_worktree,
    anchor_resolution,
    load_workspace_by_path,
    read_anchor,
    register_workspace,
)
from mozyo_bridge.core.state.herdr_session_start_gate import (
    SessionStartGateError,
    require_session_start_gate,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import attestation_store_lock
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
    LAUNCH_CAUSE_GENERIC_FRESH,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    evaluate_attestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_identity import (  # noqa: E501
    PrivateRestoreContainerBinding,
    PrivateWorktreeBinding,
    _lane_id_from_metadata,
    _resolve_workspace_id_readonly,
    private_workspace_effect_fence,
    resolve_workspace_id_if_registered,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
    InvalidPermissionMode,
    permission_mode_argv,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    derive_lane_workspace_token,
    encode_assigned_name,
    terminal_identity_of_live_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (
    SLOT_STALE as LIVENESS_STALE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_slot_execution import (  # noqa: E501
    _execute_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (
    StartupProbe,
    attach_startup_health,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    open_startup_transaction_and_reserve_generations,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_alias import apply_workspace_alias, require_alias_identity  # noqa: E501
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_completion import (  # noqa: E501
    complete_session_start,
    complete_session_start_container,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (
    SLOT_ADOPTED,
    _SlotPlan,
    SLOT_LAUNCHED,
    SLOT_PLANNED,
    SLOT_STALE,
    SLOT_UNATTESTED,
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    AGENT_PROVIDERS,
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    COMMAND_TIMEOUT_SECONDS,
    Runner,
    _bounded_detail,
    resolve_herdr_binary,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_preflight import (  # noqa: E501
    preflight_managed_launch,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_cli_capabilities import (  # noqa: E501
    require_herdr_cli_capabilities,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    resolve_attest_launcher,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (
    ResolvedProviderLaunch,
    preflight_launch_providers,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AgentProviderProfileError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import finalize_container_geometry  # noqa: E501
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_role_grouped_space import (  # noqa: E501
    classify_role_grouped_placement,
    preflight_project_coordinator_label_authority,
    _resolve_project_coordinator_workspace_under_lock,
    resolve_project_coordinator_container_plan,
    resolve_role_grouped_implementation_target,
    validate_role_grouped_inventory,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_tab_preparation import (  # noqa: E501
    resolve_lane_tab,
    workspace_root_minter,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    _create_workspace,
    _invoke,
    _list_rows,
    _list_workspace_labels,
    HerdrLauncherIncompatibleError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_bound_launch import (  # noqa: E501
    ActionPrivateLaunchShimSet,
    bind_native_launch_set,
    prepare_complete_launch_shims,
    prepared_pane_recorder,
    resolve_required_split_anchor,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    HerdrSessionStartError,
    SHARED_COORDINATOR_WORKSPACE_LABEL,
    _host_workspace_label,
    _launch_target_for_lane,
    _parse_started_agent,
    _shared_coordinator_own_target,
    _shared_coordinator_target,
    _workspace_prefix,
    herdr_workspace_segment,
    resolve_container_plan,
    resolve_launch_order,
    resolve_placement_policy_for_role,
    slot_placement,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.coordinator_placement_mode import (  # noqa: E501
    COORDINATOR_PLACEMENT_MODES,
    DEFAULT_COORDINATOR_PLACEMENT_MODE,
    ROLE_GROUPED_SPACE,
    SHARED_SPACE,
)
from mozyo_bridge.core.state.coordinator_placement_fence import (
    CoordinatorSharedCreateLockUnavailable,
    CoordinatorSharedCreateReleaseError,
    coordinator_shared_create_lock,
)
from mozyo_bridge.shared.errors import die

def _resolve_binary_or_die(env: Mapping[str, str]) -> str:
    """The absolute herdr binary this launch injects, via the shared resolver.

    Shares the single :func:`resolve_herdr_binary` trusted-environment order
    (``MOZYO_HERDR_BINARY`` → trusted-PATH ``herdr``, realpath / executable
    verified) so a launch never resolves a different binary than the send / read
    paths (Redmine #13496). The resolved absolute path is what rides on the
    launched agent's ``--env MOZYO_HERDR_BINARY=<path>`` (see :func:`_execute_slot`).
    A fail-closed resolution is re-raised as :class:`HerdrSessionStartError` so the
    session-start caller keeps its single error type.
    """
    try:
        return resolve_herdr_binary(env).path
    except TerminalTransportError as exc:
        raise HerdrSessionStartError(str(exc)) from exc


@contextmanager
def _coordinator_launch_lock(surface: str) -> Iterator[None]:
    """Hold one shared coordinator surface through the complete pair launch.

    Herdr 0.8 starts an agent only in an already prepared pane.  A labelled shared
    workspace is therefore not yet joinable while its creator has only made the root
    pane: a concurrent project needs one exact managed pane as its split anchor.  Keep
    the existing home-scoped fence until this session call completes, so a waiter
    re-observes the creator's live panes instead of racing the create/launch gap.
    """
    try:
        with coordinator_shared_create_lock(mozyo_bridge_home()):
            yield
    except CoordinatorSharedCreateReleaseError as exc:
        raise HerdrSessionStartError(
            f"managed launch completed its {surface} critical section but "
            f"could not release the single-flight lock ({exc}); workspace and agent "
            "creation may already have completed. Re-run to re-observe the exact live "
            "state before taking any recovery action."
        ) from exc
    except CoordinatorSharedCreateLockUnavailable as exc:
        raise HerdrSessionStartError(
            f"managed-launch admission could not acquire the {surface} "
            f"single-flight lock ({exc}); no workspace / tab / agent was created. "
            "Re-run once the home lock is reachable."
        ) from exc


def prepare_session(
    *,
    repo_root: Path,
    providers: Sequence[str],
    lane_id: str,
    env: Mapping[str, str],
    pair_order: Optional[Sequence[str]] = None,
    runner: Optional[Runner] = None,
    launcher_runner: Optional[Runner] = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    dry_run: bool = False,
    claude_permission_mode_default: Optional[str] = None,
    agent_launch: "Optional[AgentLaunchConfig]" = None,
    lane_placement: "Optional[LanePlacementConfig]" = None,
    launch_context: "Optional[LaneLaunchContext]" = None,
    coordinator_placement_mode: str = DEFAULT_COORDINATOR_PLACEMENT_MODE,
    coordinator_top_workspace_id: str = "",
    attestation_reader: "Optional[Callable[[str], Optional[IdentityAttestationRecord]]]" = None,
    replacement_action_id: str = "",
    probe: "Optional[StartupProbe]" = None,
    startup_fence: "Optional[StartupTransactionFence]" = None,
    action_nonce: str = "",
    expected_workspace_id: str = "",
    expected_worktree: "Optional[PrivateWorktreeBinding]" = None,
    launch_cause: str = LAUNCH_CAUSE_GENERIC_FRESH,
    _restore_effect_fence: "Optional[Callable[[], None]]" = None,
    _restore_startup_overlap_guard: bool = False,
    _restore_container: "Optional[PrivateRestoreContainerBinding]" = None,
    _restore_pane_owner=None,
    _session_gate_lease=None,
) -> SessionStartResult:
    """Validate through the typed entry sibling while keeping this stable signature."""
    arguments = locals()
    from .herdr_session_start_entry import prepare_session as entry

    return entry(**arguments)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_lifecycle_admission import admit_launch_against_lifecycle  # noqa: E501
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_preflight import validate_coordinator_placement_request, validate_pair_order, validate_session_request  # noqa: E501


def _prepare_session_locked(
    *,
    repo_root: Path,
    providers: Sequence[str],
    lane_id: str,
    env: Mapping[str, str],
    pair_order: Optional[Sequence[str]] = None,
    runner: Optional[Runner] = None,
    launcher_runner: Optional[Runner] = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    dry_run: bool = False,
    claude_permission_mode_default: Optional[str] = None,
    agent_launch: "Optional[AgentLaunchConfig]" = None,
    lane_placement: "Optional[LanePlacementConfig]" = None,
    launch_context: "Optional[LaneLaunchContext]" = None,
    coordinator_placement_mode: str = DEFAULT_COORDINATOR_PLACEMENT_MODE,
    coordinator_top_workspace_id: str = "",
    attestation_reader: "Optional[Callable[[str], Optional[IdentityAttestationRecord]]]" = None,
    replacement_action_id: str = "",
    probe: "Optional[StartupProbe]" = None,
    startup_fence: "Optional[StartupTransactionFence]" = None,
    action_nonce: str = "",
    launch_cause: str = LAUNCH_CAUSE_GENERIC_FRESH,
    alias_expected_workspace_id: str = "",
    workspace_effect_expected_id: str = "",
    expected_worktree: "Optional[PrivateWorktreeBinding]" = None,
    _launch_shims: "Optional[ActionPrivateLaunchShimSet]" = None,
    _capabilities_observed: bool = False,
    _restore_effect_fence: "Optional[Callable[[], None]]" = None,
    _restore_startup_overlap_guard: bool = False,
    _restore_container: "Optional[PrivateRestoreContainerBinding]" = None,
    _restore_pane_owner=None,
    _session_gate_lease=None,
) -> SessionStartResult:
    """Mint or adopt the requested durable Herdr identities, fail-closed.

    The cataloged native-identity spec owns the full contract. This private entry
    point validates the plan before writes; the public caller owns lock and rollback.

    Nested-workspace alias / launch-disable is re-evaluated HERE as well as in
    :func:`prepare_session` (review j#102107 Finding 4). This entry — not the
    public wrapper — is the boundary every launch actually reaches: the v1
    replacement driver calls it directly with ``admission_lock_held=True`` to
    avoid re-entering the lock wrapper, so a rail placed only on the public entry
    is bypassed by exactly the live replacement path. Re-applying is idempotent:
    a canonical root declares nothing, so an already-folded root resolves to
    itself unchanged. The public entry keeps its own call so a declined workspace
    is still refused *before* the home lock, binary resolution and the capability
    probe — the zero-side-effect ordering this rail promises.
    """
    repo_root, _own_id = apply_workspace_alias(repo_root)
    restore_axes = (
        _restore_effect_fence is not None,
        _restore_startup_overlap_guard is True,
        _restore_container is not None,
        _restore_pane_owner is not None,
    )
    if any(restore_axes) and (
        not all(restore_axes)
        or not workspace_effect_expected_id
        or not action_nonce
    ):
        raise HerdrSessionStartError("private restore authority is incomplete")
    if expected_worktree is not None and not all(restore_axes):
        raise HerdrSessionStartError("private restore authority is incomplete")
    if not dry_run:
        try:
            require_session_start_gate(
                _session_gate_lease,
                home=mozyo_bridge_home(),
                exclusive=False,
            )
        except SessionStartGateError as exc:
            raise HerdrSessionStartError(str(exc)) from exc
    _alias_expected_id = alias_expected_workspace_id or _own_id
    for provider in providers:
        if provider not in AGENT_PROVIDERS:
            raise HerdrSessionStartError(
                f"unknown provider {provider!r}; expected one of {sorted(AGENT_PROVIDERS)}"
            )
    # Argument-level fail-closed validation, BEFORE any side effect: unknown placement mode
    # (#14139), duplicate (provider, lane) slot (spec §5), invalid managed permission policy
    # (j#73404), whole-plan role/profile/argv resolution (#13647 T2). Leafed for module health.
    validate_session_request(
        providers=providers,
        lane_id=lane_id,
        coordinator_placement_mode=coordinator_placement_mode,
        coordinator_top_workspace_id=coordinator_top_workspace_id,
        claude_permission_mode_default=claude_permission_mode_default,
        env=env,
        error_type=HerdrSessionStartError,
        launch_context=launch_context,
        pair_order=pair_order,
    )
    binary = _resolve_binary_or_die(env)
    runner = runner or subprocess.run
    # The Herdr transport and the local mozyo-bridge launcher are distinct authority
    # surfaces.  Production uses subprocess.run for both, while endpoint-bound smoke
    # injects a Herdr-only runner that must never receive launcher probes.  Preserve the
    # historical single-runner test seam when no explicit launcher runner is supplied.
    launcher_runner = launcher_runner or runner
    if _restore_effect_fence is not None:
        unfenced_runner = runner

        def runner(*args, **kwargs):
            _restore_effect_fence()
            return unfenced_runner(*args, **kwargs)
    # Probe both Herdr 0.8 write surfaces before registration or any Herdr write.
    if not _capabilities_observed:
        require_herdr_cli_capabilities(
            binary,
            runner=runner,
            timeout=timeout,
            env=env,
            error_type=HerdrSessionStartError,
        )
    # The mozyo-bridge launcher the #13637 self-check wraps the provider through
    # (resolved once, shared by every launched slot; "" disables wrapping).
    attest_launcher = resolve_attest_launcher(env)
    # The self-attestation store home, injected onto the wrapper (`--env
    # MOZYO_BRIDGE_HOME`) so its write lands in the SAME store the adopt reader /
    # doctor read — a herdr-spawned wrapper does not inherit the client's home
    # (review j#76492 Finding 1). Resolved via `mozyo_bridge_home()` to match the reader.
    store_home = str(mozyo_bridge_home())

    # Linked worktrees inherit the main checkout's workspace identity; the lane segment
    # remains the slot discriminant. The shared resolver keeps every consumer aligned.
    resolved_root = Path(repo_root).expanduser().resolve()
    lane = _norm(lane_id)
    refresh_registration = False
    if _is_linked_worktree(resolved_root):
        workspace_id = herdr_workspace_segment(resolved_root)
        if not workspace_id:
            raise HerdrSessionStartError(
                "linked worktree's main checkout has no registered workspace "
                "identity to inherit; run `mozyo-bridge workspace register` from "
                "the main checkout first (Redmine #13152 / #13377)"
            )
        if not lane:
            # A lane worktree's slots carry a non-default lane segment. Recover the
            # recorded lane rather than minting the project workspace's DEFAULT
            # slots (those are the coordinator pair) from a lane checkout.
            lane = _lane_id_from_metadata(resolved_root)
            if not lane:
                raise HerdrSessionStartError(
                    "a linked worktree lane requires an explicit lane id (its slots "
                    "are `mzb1_<project-ws>_<role>_<lane>`); pass --lane or create "
                    "the lane via `sublane create` so its lane metadata record "
                    "carries the lane id (Redmine #13377)"
                )
    elif dry_run:
        # Query / command split (Redmine #13595): a dry-run resolves the durable
        # workspace identity WITHOUT any write. The prior code called
        # `register_workspace(repo_root)` here unconditionally, so a `--dry-run`
        # (documented "without any side effect") created the registry + anchor on an
        # unregistered repo and bumped `updated_at` / `last_seen` / anchor bytes on a
        # registered one. Resolve read-only; fail closed when no identity resolves.
        workspace_id = _resolve_workspace_id_readonly(resolved_root)
    else:
        existing_workspace = resolve_workspace_id_if_registered(resolved_root)
        if alias_expected_workspace_id:
            require_alias_identity(alias_expected_workspace_id, existing_workspace)
        if existing_workspace:
            workspace_id = existing_workspace
            refresh_registration = True
        else:
            preflight_managed_launch(attest_launcher, launcher_runner, timeout, env, repo_root=repo_root, store_home=Path(store_home), workspace_id="unregistered", lane_id=lane or "default", replacement_action_id=replacement_action_id, launch_planned=bool(providers))
            if _restore_effect_fence is not None:
                _restore_effect_fence()
            register_workspace(repo_root)
            anchor = read_anchor(repo_root)
            workspace_id = _norm(anchor.get("workspace_id")) if isinstance(anchor, dict) else ""
            if not workspace_id:
                raise HerdrSessionStartError("workspace has no resolvable workspace_id after registration")
    require_alias_identity(_alias_expected_id, workspace_id)
    workspace_effect_fence = private_workspace_effect_fence(
        repo_root,
        expected_workspace_id=workspace_effect_expected_id,
        expected_worktree=expected_worktree,
        home=Path(store_home),
    )
    result = SessionStartResult(
        workspace_id=workspace_id, lane_id=lane or "default", dry_run=dry_run
    )
    if _restore_container is not None and not (
        valid_target(_restore_container.workspace_id)
        and valid_target(_restore_container.tab_id)
        and valid_target(_restore_container.pane_locator)
        and _restore_container.pane_locator.startswith(
            f"{_restore_container.workspace_id}:"
        )
        and _restore_container.tab_id.startswith(
            f"{_restore_container.workspace_id}:t"
        )
        and bool(_restore_container.terminal_id)
    ):
        raise HerdrSessionStartError("private restore container authority is malformed")
    # This shared-lock boundary resolves the launch's lane-kind (Redmine #13647 T1b): caller context
    # (fresh launch) reconciled with the stored generation-bound kind (heal), fail-closed.
    lane_kind = admit_launch_against_lifecycle(
        workspace_id=workspace_id,
        lane_id=result.lane_id,
        store_home=store_home,
        launch_context=launch_context,
        dry_run=dry_run,
    )
    role_grouped_project_coordinator = False
    role_grouped_implementation = False
    if coordinator_placement_mode == ROLE_GROUPED_SPACE:
        role_grouped_project_coordinator, role_grouped_implementation = (
            classify_role_grouped_placement(
                lane_id=result.lane_id, lane_kind=lane_kind, workspace_id=workspace_id,
                top_workspace_id=coordinator_top_workspace_id,
            )
        )
    shared_space_project_coordinator = (
        coordinator_placement_mode == SHARED_SPACE and result.lane_id == DEFAULT_LANE
    )
    project_column_coordinator = role_grouped_project_coordinator or shared_space_project_coordinator

    # Config-driven pane placement (Redmine #13646, Design Answer j#76564): resolve the
    # lane's EFFECTIVE `(split, order)` ONCE, then reorder the requested providers so the
    # first-launched slot occupies the container. `lane_class` is the same axis
    # `agent_launch` keys on, resolved independently (no merge). The decisions are pure
    # (`herdr_lane_topology`).
    # Lane-role aware placement precedence (Redmine #13647, disposition j#85650): the
    # caller-supplied `launch_context` carries the durable `lane_kind` (親/子/孫) resolved from
    # governance at the create / heal boundary — never inferred from provider / pane / display
    # cache here. Precedence is `by_lane_kind[kind] > lane_class > product default`
    # (`resolve_placement_policy_for_role`); a `None` context / unresolved kind / a config with
    # no matching `by_lane_kind` entry all fall through to the lane-class resolution.
    # Fresh launch = that context, heal = the stored kind (reconciled above).
    # Redmine #14568: the bottom of that chain is now the PRODUCT default (`split: down` on
    # both lane classes, `order: (codex, claude)` on the default pair), so a workspace that
    # declares no `lane_placement` still lands its pairs vertically with the coordinator on
    # top. `split: right` on the lane class (or on a lane kind) is the rollback.
    # Redmine #14569 resolves the pair's split RATIO through the same one adapter; it is
    # consumed after the launches (`agent start` has no `--ratio`), not in the argv.
    lane_class = "default" if result.lane_id == DEFAULT_LANE else "sublane"
    config_split, config_order, config_ratio = resolve_placement_policy_for_role(
        lane_placement, lane_class, lane_kind
    )
    providers = resolve_launch_order(providers, config_order)

    # Startup self-attestation reader (Redmine #13637): the adopt gate joins each live
    # name-match with its record. Injectable for tests; defaults to the store pinned to
    # the SAME `store_home` the wrapper writes to (j#76492 F1), fail-open None.
    attestation_read = (
        attestation_reader or HerdrIdentityAttestationStore(home=Path(store_home)).read
    )
    rows = _list_rows(binary, runner, timeout)

    plans: list = []
    for provider in providers:
        assigned_name = encode_assigned_name(workspace_id, provider, lane)
        # Rows whose durable name equals assigned_name (fail-closed on duplicates below).
        existing = [
            row for row in rows
            if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME)) == assigned_name
        ]
        if len(existing) > 1:
            raise HerdrSessionStartError(
                f"{len(existing)} live agents already carry {assigned_name!r}; herdr "
                "names must be unique — refuse to launch / rename over a duplicate"
            )
        if len(existing) == 1:
            live_locator = _agent_locator(existing[0])
            if classify_named_slot(existing[0]) == LIVENESS_STALE:
                # Composite liveness (Redmine #13518 j#75329): a host-restart shell residue
                # (name matches, no detected agent) is stale and surfaced, never blind-adopted.
                plans.append(_SlotPlan(provider, assigned_name, "stale", live_locator))
            else:
                # Startup self-attestation gate (Redmine #13637, Design Answer j#76462):
                # adopt a live name-match ONLY when a `present` self-attestation is
                # generation-bound to THIS live locator; absent / stale / missing /
                # conflicting -> surfaced read-only as `unattested` (never blind-adopted
                # or auto-repaired — herdr cannot read or mutate a live process env).
                join = evaluate_attestation(
                    attestation_read(assigned_name),
                    live_locator=live_locator,
                    live_terminal_id=terminal_identity_of_live_slot(assigned_name, live_locator, rows),
                    expected_workspace_id=workspace_id,
                    expected_role=provider,
                    expected_lane=lane,
                )
                kind = "adopt" if join.ok else "unattested"
                plans.append(
                    _SlotPlan(
                        provider, assigned_name, kind, live_locator, detail=join.reason
                    )
                )
        elif dry_run:
            plans.append(_SlotPlan(provider, assigned_name, "planned"))
        else:
            plans.append(_SlotPlan(provider, assigned_name, "launch"))

    if coordinator_placement_mode == ROLE_GROUPED_SPACE:
        validate_role_grouped_inventory(
            rows, workspace_id, result.lane_id,
            [(p.kind, p.locator) for p in plans],
            shared_project_coordinator=role_grouped_project_coordinator,
        )
    # Whole-plan launch preflight — the LAST point before any herdr write (#13441 review
    # R1-F1). Every provider that will actually be launched has its profile, protocol,
    # capability, trusted executable, and managed policy resolved HERE, so a provider that
    # cannot be resolved aborts the run with zero `workspace create`, zero `tab create`,
    # and zero `agent start`. Resolving lazily inside each slot's builder (the pre-R1-F1
    # shape) meant a (codex, claude) pair created the workspace, created the tab, and
    # started codex before discovering that claude's binary was missing — leaving a live
    # agent in a partial lane. This is the same invariant the permission-mode validation
    # above already held (j#73404); executable resolution now holds it too.
    #
    # Only `launch` plans are preflighted: an adopt-only / dry-run session starts no
    # process, so it must not begin to require a resolvable provider binary that the
    # pre-#13441 code never needed (byte-invariant for adopt / dry-run).
    launch_plans = [plan for plan in plans if plan.kind == "launch"]
    try:
        resolved_launches = preflight_launch_providers(
            [plan.provider for plan in launch_plans],
            env,
            permission_mode_default=claude_permission_mode_default,
            # The cause the CALLER proved; naming none stays byte-invariant (j#97171).
            launch_cause=launch_cause,
        )
    except AgentProviderProfileError as exc:
        # Includes AgentProviderExecutableError (unknown / undrivable / missing /
        # ambiguous / unsafe-PATH). Re-raised on this module's fail-closed boundary.
        raise HerdrSessionStartError(str(exc)) from exc

    # Completeness / identity guard BEFORE the first side effect (#13441 review R2-F1
    # must-fix 4): every launch plan must have a matching resolved entry, and the
    # resolved entry's provider identity must match the plan. This fails closed HERE
    # (zero workspace / tab / agent) rather than deferring an identity mismatch to the
    # pure builder, which must never re-derive or re-check it after a sibling started.
    for plan in launch_plans:
        resolved = resolved_launches.get(plan.provider)
        if resolved is None or resolved.provider_id != plan.provider:
            raise HerdrSessionStartError(
                f"launch preflight did not resolve provider {plan.provider!r} "
                f"(resolved={resolved!r}); refusing to start a lane with an "
                f"unresolved or mismatched provider"
            )
    # Managed-launch compatibility boundary, shared with the pre-worktree gate.
    preflight_managed_launch(
        attest_launcher, launcher_runner, timeout, env,
        repo_root=repo_root,
        store_home=Path(store_home),
        workspace_id=workspace_id,
        lane_id=result.lane_id,
        replacement_action_id=replacement_action_id,
        launch_planned=bool(launch_plans),
    )
    if refresh_registration:
        if workspace_effect_fence is not None:
            workspace_effect_fence()
        if _restore_effect_fence is not None:
            _restore_effect_fence()
        register_workspace(repo_root)
    if _restore_effect_fence is not None:
        _restore_effect_fence()
    native_admission = bind_native_launch_set(
        tuple(plan.assigned_name for plan in launch_plans),
        store_home=Path(store_home),
        source_path=env.get("PATH"),
    )
    if role_grouped_project_coordinator:
        preflight_project_coordinator_label_authority(
            workspace_id=workspace_id, binary=binary, runner=runner, timeout=timeout,
            acquire_lock=not dry_run)
    # Reserve startup identities only after preflight; later effects belong to this action.
    if workspace_effect_fence is not None:
        workspace_effect_fence()
    if _restore_effect_fence is not None:
        _restore_effect_fence()
    transaction = open_startup_transaction_and_reserve_generations(
        workspace_id=workspace_id, lane_id=result.lane_id, providers=providers,
        dry_run=dry_run, home=Path(store_home), fence=startup_fence, nonce=action_nonce,
        launch_plans=launch_plans, attest_launcher=attest_launcher, env=env, resolved=resolved_launches,
        effect_fence=_restore_effect_fence,
        completion_fence=getattr(
            _restore_effect_fence, "before_completion", None
        ),
        refuse_nonterminal_slot_overlap=_restore_startup_overlap_guard,
    )
    result.action_id = transaction.action_id if transaction else ""
    if _restore_effect_fence is not None:
        _restore_effect_fence()
    launch_shim_dirs = prepare_complete_launch_shims(
        _launch_shims,
        providers=tuple(plan.provider for plan in launch_plans),
        resolved_launches=resolved_launches,
        attest_launcher=attest_launcher,
        store_home=Path(store_home),
        action_id=result.action_id,
    )
    # Resolve workspace; role_grouped_space separates all three roles (#14996).
    target_workspace, workspace_root_prepared = "", None
    if launch_plans:
        adopt_locators = [p.locator for p in plans if p.kind == "adopt"]
        # #15705: a fresh DEFAULT-lane workspace mint is first-slot-prepared —
        # occupation of its own root tab removes the empty root, never a close.
        mint_prepared = workspace_root_minter(
            first_launch_plan=launch_plans[0], native_bindings=native_admission.bindings,
            resolved_launches=resolved_launches, attest_launcher=attest_launcher,
            store_home=store_home, action_id=result.action_id, workspace_id=workspace_id,
            lane_id=result.lane_id, repo_root=repo_root, binary=binary, env=env,
            runner=runner, timeout=timeout, effect_fence=workspace_effect_fence,
        )
        if _restore_container is not None:
            target_workspace = _restore_container.workspace_id
        elif shared_space_project_coordinator:
            # Legacy shared_space: own identity pin first, exact label otherwise (#14139).
            target_workspace = _shared_coordinator_own_target(
                rows, workspace_id, adopt_locators
            )
            if not target_workspace:
                if _launch_shims is None:
                    raise HerdrSessionStartError(
                        "shared-space launch has no launch-scoped resource owner; "
                        "refusing to enter the single-flight section"
                    )
                # Herdr 0.8 needs a concrete managed pane as a split anchor.  Hold the
                # fence through this call's launch/finalization, then make a waiter
                # refresh inventory under the lock before resolving the shared target.
                _launch_shims.hold(_coordinator_launch_lock("shared coordinators"))
                rows = _list_rows(binary, runner, timeout)
                workspace_labels = _list_workspace_labels(binary, runner, timeout)
                target_workspace = _shared_coordinator_target(
                    rows,
                    workspace_id,
                    adopt_locators,
                    workspace_labels,
                    SHARED_COORDINATOR_WORKSPACE_LABEL,
                )
                if not target_workspace:
                    target_workspace, workspace_root_prepared = mint_prepared(
                        SHARED_COORDINATOR_WORKSPACE_LABEL
                    )
                    result.base_pane_id = workspace_root_prepared.locator
        elif role_grouped_project_coordinator:
            if _launch_shims is None:
                raise HerdrSessionStartError(
                    "role-grouped project-coordinator launch has no launch-scoped "
                    "resource owner; refusing to enter the single-flight section"
                )
            # The label alone is not a Herdr 0.8 split authority. Hold the shared
            # fence through live pane creation; a waiter then refreshes under that fence.
            _launch_shims.hold(_coordinator_launch_lock("project coordinators"))
            rows = _list_rows(binary, runner, timeout)
            shared = _resolve_project_coordinator_workspace_under_lock(
                rows=rows,
                workspace_id=workspace_id,
                lane_id=result.lane_id,
                adopted_locators=adopt_locators,
                binary=binary,
                repo_root=repo_root,
                runner=runner,
                timeout=timeout,
                env=env,
                effect_fence=workspace_effect_fence,
                prepared_minter=mint_prepared,
            )
            target_workspace = shared.workspace_id
            result.base_pane_id = shared.base_pane_id
            workspace_root_prepared = shared.base_prepared
        else:
            if role_grouped_implementation:
                target_workspace = resolve_role_grouped_implementation_target(
                    rows=rows,
                    workspace_id=workspace_id,
                    lane_id=result.lane_id,
                    adopted_locators=adopt_locators,
                    binary=binary,
                    runner=runner,
                    timeout=timeout,
                )
            else:
                target_workspace = _launch_target_for_lane(
                    rows, workspace_id, result.lane_id, adopt_locators
                )
            if not target_workspace:
                if result.lane_id == DEFAULT_LANE:
                    target_workspace, workspace_root_prepared = mint_prepared("")
                    result.base_pane_id = workspace_root_prepared.locator
                else:
                    # Host mint stays plain: first slot lands in its #15702 lane tab.
                    target_workspace, base_pane_id = _create_workspace(
                        binary, repo_root, runner, timeout, env,
                        label=_host_workspace_label(resolved_root),
                        effect_fence=workspace_effect_fence,
                    )
                    result.base_pane_id = base_pane_id
        result.herdr_workspace_id = target_workspace

    # Non-default implementation lanes keep the #13411 lane=tab path. Project
    # coordinators are intentionally loose pairs in their shared overview workspace.
    # A freshly minted lane tab is created first-slot-prepared (#15702): its born root
    # pane carries the first launch slot's cwd/env and terminal receipt, so that slot
    # occupies the root instead of splitting beside it and no empty shell pane remains.
    target_tab, root_prepared = "", workspace_root_prepared
    lane_slot_tabs: list = []
    if (
        launch_plans
        and result.lane_id != DEFAULT_LANE
        and not role_grouped_project_coordinator
    ):
        tab_resolution = resolve_lane_tab(
            rows=rows, workspace_id=workspace_id, target_workspace=target_workspace,
            lane_id=result.lane_id,
            restore_tab=(
                _restore_container.tab_id if _restore_container is not None else None
            ),
            first_launch_plan=launch_plans[0],
            native_bindings=native_admission.bindings,
            resolved_launches=resolved_launches, attest_launcher=attest_launcher,
            store_home=store_home, action_id=result.action_id, repo_root=repo_root,
            binary=binary, env=env, runner=runner, timeout=timeout,
            effect_fence=workspace_effect_fence,
        )
        target_tab = tab_resolution.target_tab
        lane_slot_tabs = tab_resolution.lane_slot_tabs
        if tab_resolution.occupy is not None:
            result.tab_pane_id = tab_resolution.occupy.locator
            root_prepared = tab_resolution.occupy
        result.herdr_tab_id = target_tab
    # Split placement (#13411 tab axis + #13646 direction / #13646-R1-F1 focus). The first
    # slot occupies the container; later launching slots split beside it. Pure decisions —
    # see `herdr_lane_topology.resolve_container_plan` for the full contract.
    if role_grouped_project_coordinator:
        plan_of_container = resolve_project_coordinator_container_plan(
            rows,
            workspace_id,
            target_workspace,
            result.lane_id,
            config_split=config_split,
            launch_count=len(launch_plans),
        )
    else:
        plan_of_container = resolve_container_plan(
            rows,
            workspace_id,
            target_workspace,
            result.lane_id,
            lane_class=lane_class,
            target_tab=target_tab,
            lane_slot_tabs=lane_slot_tabs,
            config_split=config_split,
            launch_count=len(launch_plans),
        )
    occupancy = plan_of_container.occupancy  # grows per launch (first occupies, rest split)

    split_anchor = resolve_required_split_anchor(
        rows,
        launch_required=bool(launch_plans),
        created_root=(
            _restore_container.pane_locator
            if _restore_container is not None
            else result.tab_pane_id or result.base_pane_id
        ),
        target_workspace=target_workspace,
        target_tab=target_tab,
        preferred_locator=next(
            (p.locator for p in plans if p.kind == "adopt" and p.locator), ""
        ),
    )
    record_prepared = prepared_pane_recorder(transaction)

    # Execute each adopt/plan/launch decision. Occupancy grows after every launch.
    for plan in plans:
        # Resolve provider-specific argv once per slot from the project config.
        launch_argv_extra = (
            agent_launch.resolve_launch_argv(plan.provider, lane_class)
            if agent_launch is not None
            else []
        )
        slot_split, slot_focus, order_deferred = slot_placement(
            plan.kind,
            plan.provider,
            split_direction=plan_of_container.split_direction,
            occupancy=occupancy,
            config_order=config_order,
            focus_first=plan_of_container.focus_first,
        )
        result.slots.append(
            _execute_slot(
                plan,
                repo_root=repo_root,
                workspace_id=workspace_id,
                lane=result.lane_id,
                target_workspace=target_workspace,
                target_tab=target_tab,
                split=slot_split,
                focus=slot_focus,
                anchor_locator=split_anchor,
                binary=binary,
                attest_launcher=attest_launcher,
                store_home=store_home,
                env=env,
                runner=runner,
                timeout=timeout,
                resolved=resolved_launches.get(plan.provider),
                launch_argv_extra=launch_argv_extra,
                order_deferred=order_deferred,
                replacement_action_id=replacement_action_id,
                action_id=transaction.action_id if transaction is not None else "",
                prepared_callback=record_prepared,
                native_binding=native_admission.bindings.get(plan.assigned_name),
                shim_dir=launch_shim_dirs.get(plan.provider, ""),
                workspace_effect_fence=workspace_effect_fence,
                pane_owner=_restore_pane_owner,
                occupy_prepared=root_prepared,
            )
        )
        if plan.kind == "launch":
            root_prepared = None
            split_anchor = result.slots[-1].locator
            occupancy += 1

    # Observe every fresh process after concurrent launch; dry-run has none to observe.
    if not dry_run:
        attach_startup_health(
            result, workspace_id=workspace_id, binary=binary, runner=runner,
            timeout=timeout, attestation_read=attestation_read, probe=probe,
            attested_launch=bool(attest_launcher),
            # j#85125 F2: a wrapped fresh launch must show its own attributed
            # execution-stage rows before a green — the reader is action-scoped,
            # so an unmanaged run (no transaction) composes the exact prior pipeline.
            action_id=transaction.action_id if transaction is not None else "",
        )
    # The completion leaf owns authority -> geometry -> configured placement order.
    complete_session_start_container(
        result, geometry_finalizer=finalize_container_geometry,
        configured_placement=complete_session_start, store_home=store_home,
        transaction=transaction, workspace_id=workspace_id,
        attestation_read=attestation_read, attest_launcher=attest_launcher,
        launch_plans=launch_plans, dry_run=dry_run, config_split=config_split,
        config_order=config_order, pair_order=pair_order, requested=providers,
        config_ratio=config_ratio, initial_occupancy=plan_of_container.occupancy,
        project_column_coordinator=project_column_coordinator,
        coordinator_top_workspace_id=coordinator_top_workspace_id, binary=binary,
        runner=runner, timeout=timeout, env=env, effect_fence=_restore_effect_fence,
    )
    return result


def __getattr__(name: str):
    """Lazy re-export of the relocated CLI handler (Redmine #13882 R8-F1 / R9-F1).

    The handler now lives in :mod:`.herdr_session_start_cli` (the split approved in
    j#80207), but this module's public import surface predates that move and the split was
    declared behavior-preserving, so ``from ...herdr_session_start import
    cmd_herdr_session_start`` must keep working.

    It is a module ``__getattr__`` (PEP 562) rather than a forwarding wrapper because the
    contract is **object identity**, not merely "a call that works": the first attempt
    defined a second function here, so ``old.cmd_herdr_session_start is
    new.cmd_herdr_session_start`` was False and callers comparing, patching or registering
    the handler saw two different objects (review j#80348 R9-F1). Resolving on attribute
    access returns the one real handler while still importing lazily, which is what keeps
    the CLI module free to import this one.
    """
    if name == "cmd_herdr_session_start":
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_cli import (  # noqa: E501
            cmd_herdr_session_start,
        )

        return cmd_herdr_session_start
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = (
    "cmd_herdr_session_start",
    "SLOT_ADOPTED",
    "SLOT_LAUNCHED",
    "SLOT_PLANNED",
    "SLOT_STALE",
    "SLOT_UNATTESTED",
    "HerdrSessionStartError",
    "SessionStartResult",
    "SlotResult",
    "herdr_workspace_segment",
    "prepare_session",
)
