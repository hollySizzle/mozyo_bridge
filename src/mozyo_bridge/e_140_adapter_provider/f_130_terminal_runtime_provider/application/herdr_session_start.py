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

Empty base-pane reclaim (Redmine #13330)
-----------------------------------------
A herdr workspace is *born with a ``root_pane``* — an empty base shell (measured:
``workspace create`` returns ``result.root_pane.pane_id`` on a fresh ``pane_count: 1``
workspace). On a cold start the first ``agent start`` used to auto-create the
workspace implicitly, leaving that root pane as an unused, agent-less shell beside the
launched agent panes. To reclaim it deterministically, a pure cold start now:

1. classifies every requested slot (adopt vs launch) *before* launching;
2. if any slot must launch and none of the adopted agents pin an existing workspace,
   **explicitly** ``herdr workspace create --no-focus`` and captures the returned
   ``workspace_id`` + ``root_pane.pane_id``;
3. launches every slot with ``agent start --workspace <workspace_id>`` (so herdr never
   auto-creates a second workspace); and
4. after **every** launch succeeds, ``herdr pane close <root_pane_id>`` — closing only
   that exact captured handle.

Fail-closed guarantees: only the root pane *this run created* is ever a reclaim target
(never a scanned-for shell, so a user's own shell can't be mis-closed); a launch
failure raises before any reclaim (residue left, treated as an implementation
failure); a ``pane close`` failure is recorded non-fatally (the agents are already
live and an empty base pane is only cosmetic). All-adopt and launches into an
already-existing workspace create no new base pane, so they stay byte-invariant. The
tmux path (:mod:`mozyo_bridge.application.launch_command`) is untouched.

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
and needs no retire-side disposal: herdr auto-closes a workspace with its last
pane (live-measured), so a lane-zero host vanishes by itself.

The command is explicit opt-in and is **not** coupled to the ``terminal_transport``
backend flag: you may prepare herdr identities without selecting the herdr transport,
and vice versa (documented in ``vibes/docs/specs/herdr-native-identity.md``). In pure
herdr operation you run both.

Live-measured launch contract (herdr 0.7.1, coordinator pre-smoke)
-----------------------------------------------------------------
The ``agent start`` shape is no longer a staged assumption — it was validated against
a running herdr 0.7.1::

    herdr agent start <NAME> [--cwd PATH] [--env KEY=VALUE]... [--no-focus] -- <argv...>

- ``<NAME>`` is a required positional and is applied directly at start (probe:
  ``result.agent.name == <NAME>``), so mozyo mints the durable ``mzb1_...`` name here
  and does **not** issue a separate ``agent rename``.
- the self-identity vars ride on repeated ``--env`` flags, **not** the client process
  env: the server-spawned agent does not inherit the launching client's environment
  (coordinator-measured), so ``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` /
  ``MOZYO_LANE_ID`` are passed as ``--env KEY=VALUE``.
- ``--no-focus`` avoids stealing the operator's focus. The one exception is the first
  launch of a fresh, explicitly-placed pair, which must own the container's split target
  (Redmine #13646 R1-F1 — see ``herdr_lane_topology.resolve_focus_first_launch``).
- output is a single JSON object on stdout; the transient locator for rebind/read is
  ``result.agent.pane_id`` under a ``result.type == "agent_started"`` envelope
  (:func:`herdr_lane_topology._parse_started_agent`, fail-closed).

Tests exercise the argv + JSON parsing through an injected subprocess ``runner`` (no
live herdr binary); the end-to-end live smoke stays the coordinator's post-review step.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Optional, Sequence

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
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E501
    LAUNCH_CAUSE_GENERIC_FRESH,
)
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    AttestationStoreLockBusy,
    attestation_store_lock,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
    STORE_MAINTENANCE_IN_PROGRESS,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    evaluate_attestation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_identity import (  # noqa: E501
    _lane_id_from_metadata,
    _resolve_workspace_id_readonly,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    launch_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    finalize_session_launch_generations,
    open_startup_transaction_and_reserve_generations,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
    _extract_list_rows,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    build_agent_start_argv,
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
    require_registered_top_workspace,
    resolve_project_coordinator_container_plan,
    resolve_project_coordinator_workspace,
    resolve_role_grouped_implementation_target,
    validate_role_grouped_inventory,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    _create_tab,
    _create_workspace,
    _invoke,
    _list_rows,
    _list_workspace_labels,
    HerdrLauncherIncompatibleError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    HerdrSessionStartError,
    SHARED_COORDINATOR_WORKSPACE_LABEL,
    _host_workspace_label,
    _lane_live_slot_tabs,
    _launch_target_for_lane,
    _parse_started_agent,
    _parse_tab_created,
    _parse_workspace_created,
    _shared_coordinator_own_target,
    _shared_coordinator_target,
    _tab_target_for_lane,
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


def prepare_session(
    *,
    repo_root: Path,
    providers: Sequence[str],
    lane_id: str,
    env: Mapping[str, str],
    pair_order: Optional[Sequence[str]] = None,
    runner: Optional[Runner] = None,
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
) -> SessionStartResult:
    """Managed-launch admission under the store's shared lock (Redmine #13882 j#80190).

    Boundary 1 of the three-boundary lock protocol, and the one that lets the other two be
    safe. R7-F1 showed that a stale operation cannot pin a *generation* by path: the probe
    approved one store, a peer rotated it, a fresh one appeared at the same path, and the
    stale run then destroyed that fresh, valid store. The exclusion is what removes the
    window; this end holds it **shared**, from before the first attestation read through
    the last actuation, so a launch and an exclusive maintenance can never interleave.

    Non-blocking on purpose (j#80190): if maintenance holds the store exclusively, the
    launch fails closed **at acquisition** — before any workspace / tab / agent exists —
    rather than queueing and actuating into a store being rebuilt underneath it. That is
    the same zero-side-effect boundary the #13748 / #13847 / #13882 preflights already
    honor. Conversely, because this end is held for the whole run, maintenance cannot
    overtake an in-flight launch: its own acquisition is what fails.

    A dry run takes no lock: it plans, actuates nothing, and creating a fail-closed path
    for a read-only report would only make diagnosis harder during maintenance.

    ``pair_order`` is the lane's STABLE managed pair order, for a caller that shrank
    ``providers`` to a subset (contract: :func:`...validate_pair_order`, #14569).
    """
    # The signature is spelled out rather than `**kwargs` (review j#80305 R8-F2): the
    # explicit keyword-only contract is public (introspection / typing / IDE / wrapping
    # callers), and Python's argument binding at THIS entry is what rejects a malformed
    # call *before* any side effect. Collapsing it to `**kwargs` let a bad call create the
    # lock file first and only then raise from the inner function — a side effect ahead of
    # validation, which is exactly what the rest of this component refuses to do.
    call = dict(
        repo_root=repo_root,
        providers=providers,
        lane_id=lane_id,
        env=env,
        pair_order=pair_order,
        runner=runner,
        timeout=timeout,
        dry_run=dry_run,
        claude_permission_mode_default=claude_permission_mode_default,
        agent_launch=agent_launch,
        lane_placement=lane_placement,
        launch_context=launch_context,
        coordinator_placement_mode=coordinator_placement_mode,
        coordinator_top_workspace_id=coordinator_top_workspace_id,
        attestation_reader=attestation_reader,
        replacement_action_id=replacement_action_id,
        probe=probe,
        startup_fence=startup_fence,
        action_nonce=action_nonce,
        # In the dict too: signature-only left the public rail unarmed (audit j#97177 F1).
        launch_cause=launch_cause,
    )
    # Before the lock: acquiring it creates the home dir and a lock file, and a malformed
    # ratio authority must cost neither (#14569 j#91331 R4-F1). The locked entry re-checks.
    validate_pair_order(pair_order, providers, error_type=HerdrSessionStartError)
    validate_coordinator_placement_request(
        coordinator_placement_mode, coordinator_top_workspace_id,
        error_type=HerdrSessionStartError,
    )
    if coordinator_placement_mode == ROLE_GROUPED_SPACE:
        require_registered_top_workspace(
            coordinator_top_workspace_id, home=mozyo_bridge_home()
        )
    if dry_run:
        return _prepare_session_locked(**call)
    try:
        with attestation_store_lock(mozyo_bridge_home(), exclusive=False, blocking=False):
            return _prepare_session_locked(**call)
    except AttestationStoreLockBusy as exc:
        raise HerdrLauncherIncompatibleError(
            f"managed-launch admission refused: the selected attestation store is being "
            f"maintained right now ({exc}), so this launch would attest into a store that "
            f"is being rebuilt underneath it. No workspace / tab / agent was created. "
            f"Re-run once the maintenance command finishes.",
            reason=STORE_MAINTENANCE_IN_PROGRESS,
    ) from exc
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
) -> SessionStartResult:
    """Mint (or adopt) durable herdr identities for ``providers`` (fail-closed).

    Pure orchestration over the injected ``runner`` + ``env`` (no ambient I/O beyond
    ``register_workspace`` / ``read_anchor``). Raises :class:`HerdrSessionStartError`
    on any fail-closed condition (unknown provider, unconfigured binary, duplicate
    assigned name, a launch that yields no usable locator).

    ``dry_run`` is side-effect free by contract (Redmine #13595): it resolves the
    workspace identity read-only (:func:`_resolve_workspace_id_readonly` — never
    ``register_workspace``), classifies each slot as ``planned`` (or adopts /
    surfaces a live / stale slot read-only), and issues no ``herdr`` workspace /
    tab / agent write. A workspace with no durable identity yet fails closed with
    actionable guidance rather than being silently registered.

    ``agent_launch`` (Redmine #13425) is the repo-local launch-argv override the launch
    site resolved from ``.mozyo-bridge/config.yaml``. When provided, each launched slot's
    ``-- {provider}`` argv is extended with
    ``agent_launch.resolve_launch_argv(provider, lane_class)`` — the config's per-agent x
    lane-class tokens (model, reasoning-effort flag, …) appended verbatim (mozyo hardcodes
    no provider flag spec). ``lane_class`` is derived from the resolved lane: ``default``
    for the coordinator pair (no-lane session), ``sublane`` for a lane worker / gateway.
    ``None`` (the default) appends nothing — byte-for-byte the pre-#13425 launch, so the
    ``sublane_claude_model`` regression fix is opt-in on the launch site passing a config.

    ``lane_placement`` (Redmine #13646, Design Answer j#76564) is the repo-local herdr
    pane-pair placement policy the launch site resolved from ``.mozyo-bridge/config.yaml``.
    It reorders the requested ``providers`` (the resolved primary launches first and
    occupies; the rest split beside it) and supplies each splitting launch's ``--split
    <dir>`` — including the tab-less ``default`` pair, which before #13646 was left to the
    herdr server default. ``order`` never adds an unrequested peer; a configured primary that can only
    split beside a live sibling is reported ``order_deferred_until_full_relaunch`` rather
    than silently claimed (no swap / bounce — Non-goal: no live relayout). ``None`` — and
    an undeclared lane class — resolve to the PRODUCT default (Redmine #14568): both pairs
    split ``down``, and the coordinator pair launches ``(codex, claude)`` so the coordinator
    takes the upper pane. A workspace rolls a class back with ``split: right``.

    ``coordinator_placement_mode`` is the operator-home placement policy (#14139,
    #14996): per-project, all coordinators shared, or top dedicated with project
    coordinators shared. ``coordinator_top_workspace_id`` names that stable top;
    the policy acts only at launch/adopt and never moves live panes.

    ``claude_permission_mode_default`` is the launch-context policy default for the
    managed Claude permission mode (Redmine #11925 / #13360 / #13397): sublane lane
    creation passes ``auto`` so lane workers are reproducibly auto (tmux parity), and
    the bare ``mozyo`` coordinator-pair launch (``herdr_launch_command``) also passes
    ``auto`` so the coordinator Claude has the same headless-capable posture as its lane
    workers (Redmine #13397 finding 3 — the pre-#13397 flagless coordinator booted
    prompt-gated in an external project). A caller that passes ``None`` still gets the
    historical flagless bare ``claude`` launch. The ``MOZYO_CLAUDE_PERMISSION_MODE``
    env override rail wins over the default either way (resolved from ``env``).
    """
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
    # The mozyo-bridge launcher the #13637 self-check wraps the provider through
    # (resolved once, shared by every launched slot; "" disables wrapping).
    attest_launcher = resolve_attest_launcher(env)
    # The self-attestation store home, injected onto the wrapper (`--env
    # MOZYO_BRIDGE_HOME`) so its write lands in the SAME store the adopt reader /
    # doctor read — a herdr-spawned wrapper does not inherit the client's home
    # (review j#76492 Finding 1). Resolved via `mozyo_bridge_home()` to match the reader.
    store_home = str(mozyo_bridge_home())

    # Redmine #13377 (design j#73613, Opt3 — shared project workspace): the mzb1
    # `workspace` segment. A linked git worktree (a sublane lane checkout) inherits the
    # main checkout's registry identity (#13152), and its slots are launched INTO the
    # project workspace as `mzb1_<project-ws>_<role>_<lane>` — the lane segment, not a
    # per-lane workspace, is the discriminant (supersedes the #13331 j#73357 `wt_<hash>`
    # per-lane workspace; that token survives only as the legacy/compat + metadata key).
    # A standalone / main checkout is registered and named by its registry workspace_id
    # (byte-for-byte the prior path, incl. the fail-closed-on-empty guard). Both use the
    # single shared resolver so mint here and resolve at send/retire/projection agree.
    resolved_root = Path(repo_root).expanduser().resolve()
    lane = _norm(lane_id)
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
        register_workspace(repo_root)
        anchor = read_anchor(repo_root)
        workspace_id = _norm(anchor.get("workspace_id")) if isinstance(anchor, dict) else ""
        if not workspace_id:
            raise HerdrSessionStartError(
                "workspace has no resolvable workspace_id after registration"
            )

    result = SessionStartResult(
        workspace_id=workspace_id, lane_id=lane or "default", dry_run=dry_run
    )

    # Redmine #14242 F3 — the ORDER half of the launch / terminalize exclusion. Placement is
    # load-bearing (see the leaf's docstring): here it runs under the caller-held shared lock on
    # BOTH entry paths. A dry run actuates nothing and consults no durable state. That same
    # boundary read resolves this launch's lane-kind (Redmine #13647 T1b): caller context
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

    runner = runner or subprocess.run
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
                plans.append(
                    _SlotPlan(provider, assigned_name, "stale", live_locator)
                )
            else:
                # Startup self-attestation gate (Redmine #13637, Design Answer j#76462):
                # adopt a live name-match ONLY when a `present` self-attestation is
                # generation-bound to THIS live locator; absent / stale / missing /
                # conflicting -> surfaced read-only as `unattested` (never blind-adopted
                # or auto-repaired — herdr cannot read or mutate a live process env).
                join = evaluate_attestation(
                    attestation_read(assigned_name),
                    live_locator=live_locator,
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

    # Managed-launch compatibility boundary — the LAST fail-closed point before any herdr
    # write. The whole conjunction, its gating and its derived flags live beside it in
    # `herdr_launch_preflight.preflight_managed_launch`, shared with the `sublane create`
    # pre-worktree gate so a conjunct can never be present at only one of the two boundaries.
    preflight_managed_launch(
        attest_launcher, runner, timeout, env,
        repo_root=repo_root,
        store_home=Path(store_home),
        workspace_id=workspace_id,
        lane_id=result.lane_id,
        replacement_action_id=replacement_action_id,
        launch_planned=bool(launch_plans),
    )
    if role_grouped_project_coordinator:
        preflight_project_coordinator_label_authority(
            workspace_id=workspace_id, binary=binary, runner=runner, timeout=timeout,
            acquire_lock=not dry_run)
    # Reserve startup identities only after preflight; later effects belong to this action.
    transaction = open_startup_transaction_and_reserve_generations(
        workspace_id=workspace_id, lane_id=result.lane_id, providers=providers,
        dry_run=dry_run, home=Path(store_home), fence=startup_fence, nonce=action_nonce,
        launch_plans=launch_plans, attest_launcher=attest_launcher, env=env, resolved=resolved_launches,
    )
    result.action_id = transaction.action_id if transaction else ""

    # Resolve workspace; role_grouped_space separates all three roles (#14996).
    launch_plans = [p for p in plans if p.kind == "launch"]
    target_workspace = ""
    if launch_plans:
        shared_coordinator_space = (
            coordinator_placement_mode == SHARED_SPACE
            and result.lane_id == DEFAULT_LANE
        )
        adopt_locators = [p.locator for p in plans if p.kind == "adopt"]
        if shared_coordinator_space:
            # Legacy shared_space: own identity pin first, exact label otherwise (#14139).
            target_workspace = _shared_coordinator_own_target(
                rows, workspace_id, adopt_locators
            )
            if not target_workspace:
                # Serialize list→resolve→create; release errors may leave a labelled husk.
                try:
                    with coordinator_shared_create_lock(mozyo_bridge_home()):
                        workspace_labels = _list_workspace_labels(binary, runner, timeout)
                        target_workspace = _shared_coordinator_target(
                            rows,
                            workspace_id,
                            adopt_locators,
                            workspace_labels,
                            SHARED_COORDINATOR_WORKSPACE_LABEL,
                        )
                        if not target_workspace:
                            target_workspace, base_pane_id = _create_workspace(
                                binary,
                                repo_root,
                                runner,
                                timeout,
                                env,
                                label=SHARED_COORDINATOR_WORKSPACE_LABEL,
                            )
                            result.base_pane_id = base_pane_id
                except CoordinatorSharedCreateReleaseError as exc:
                    # Release runs AFTER the body: the shared workspace was already
                    # resolved (created on a clean slate, or adopted), and the
                    # coordinator agents were NOT started. A labelled `coordinators`
                    # workspace may exist as an empty husk; a re-run adopts it
                    # idempotently (no duplicate is created).
                    raise HerdrSessionStartError(
                        "managed-launch admission resolved the shared coordinators "
                        f"workspace but could not release the single-flight lock ({exc}); "
                        "the coordinator agents were NOT started. A labelled "
                        "'coordinators' workspace may have been created and remain as an "
                        "empty husk — re-run to adopt it idempotently (no duplicate is "
                        "created)."
                    ) from exc
                except CoordinatorSharedCreateLockUnavailable as exc:
                    raise HerdrSessionStartError(
                        "managed-launch admission could not acquire the shared "
                        f"coordinators single-flight lock ({exc}); no workspace / tab / "
                        "agent was created. Re-run once the home lock is reachable."
                    ) from exc
        elif role_grouped_project_coordinator:
            shared = resolve_project_coordinator_workspace(
                rows=rows,
                workspace_id=workspace_id,
                lane_id=result.lane_id,
                adopted_locators=adopt_locators,
                binary=binary,
                repo_root=repo_root,
                runner=runner,
                timeout=timeout,
                env=env,
            )
            target_workspace = shared.workspace_id
            result.base_pane_id = shared.base_pane_id
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
                create_label = (
                    _host_workspace_label(resolved_root)
                    if result.lane_id != DEFAULT_LANE
                    else ""
                )
                target_workspace, base_pane_id = _create_workspace(
                    binary,
                    repo_root,
                    runner,
                    timeout,
                    env,
                    label=create_label,
                )
                result.base_pane_id = base_pane_id
        result.herdr_workspace_id = target_workspace

    # Non-default implementation lanes keep the #13411 lane=tab path. Project
    # coordinators are intentionally loose pairs in their shared overview workspace.
    target_tab = ""
    lane_slot_tabs: list = []
    if (
        launch_plans
        and result.lane_id != DEFAULT_LANE
        and not role_grouped_project_coordinator
    ):
        lane_slot_tabs = _lane_live_slot_tabs(
            rows, workspace_id, target_workspace, result.lane_id
        )
        target_tab = _tab_target_for_lane(
            rows, workspace_id, target_workspace, result.lane_id
        )
        if not target_tab and not lane_slot_tabs:
            target_tab, tab_pane_id = _create_tab(
                binary, target_workspace, runner, timeout, env, label=result.lane_id
            )
            result.tab_pane_id = tab_pane_id
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

    # Pass 2 — execute each slot's decision (adopt row, dry-run plan, or launch into the
    # resolved target workspace/tab). A launch failure raises here, before reclaim.
    # `occupancy` grows per launch so the first launched slot occupies and the rest split.
    for plan in plans:
        # Config-driven launch argv (Redmine #13425): per-slot `-- {provider}` extras from
        # the single-source resolver; `None` config yields `[]`, so an unconfigured launch
        # is byte-for-byte the pre-#13425 command. `lane_class` is resolved once above.
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
            )
        )
        if plan.kind == "launch":
            occupancy += 1
            if transaction is not None:
                transaction.record_launch(
                    result.slots[-1],
                    receipt=launch_receipt(
                        target_workspace=target_workspace, target_tab=target_tab
                    ),
                )

    # Pass 3 — observe what we started (Redmine #13948, Answer j#80989). `agent start`
    # returning a well-formed, correctly-located locator is the LAUNCHER's claim; it says
    # nothing about the process. This bounded read-only probe turns "accepted" into
    # "live there, screen-clear, self-attested", per role, after every launch so the
    # providers boot concurrently. A dry run started nothing, so it observes nothing.
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
    # Finish the container — reclaim, column, ratio; the callee owns that order and why it
    # must follow pass 3 (#14996 R3, live finding j#100135). Records rather than raises.
    finalize_container_geometry(
        result, config_split=config_split, config_order=config_order,
        pair_order=pair_order, requested=providers, config_ratio=config_ratio,
        launched=len(launch_plans), initial_occupancy=plan_of_container.occupancy,
        dry_run=dry_run, binary=binary, runner=runner, timeout=timeout, env=env,
        project_coordinator=role_grouped_project_coordinator, store_home=store_home,
        top_workspace_id=coordinator_top_workspace_id,
    )
    # Finalize each launched slot's generation to `attested` — best-effort, once startup
    # health gathered the composite evidence (#14203 j#87472; see the binding module).
    finalize_session_launch_generations(
        store_home=Path(store_home), transaction=transaction, slots=result.slots,
        workspace_id=workspace_id, lane_id=result.lane_id, attestation_read=attestation_read,
        attest_launcher=attest_launcher, launch_plans=launch_plans, dry_run=dry_run,
    )
    if transaction is not None:
        # Record the debt, never discharge it: closing what this run started is the
        # explicit public rollback rail's authority alone (Answer j#80991). The debt is
        # scoped to what THIS run freshly launched, not the pair aggregate (Redmine #13933
        # R13, j#82038): a healthy fresh launch that adopted a non-green sibling owes no
        # rollback, so the transaction must not strand it at `rollback_owed` — the v1
        # replacement bind reads that phase and stalled the whole a14 convergence on it.
        transaction.settle(owed=result.owes_rollback, launched=bool(launch_plans))
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
