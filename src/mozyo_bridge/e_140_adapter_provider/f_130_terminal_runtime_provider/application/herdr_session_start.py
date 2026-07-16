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
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
        AgentLaunchConfig,
        LanePlacementConfig,
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
from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    evaluate_attestation,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (
    SLOT_ADOPTED,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    build_agent_start_argv,
    resolve_attest_launcher,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (
    ResolvedProviderLaunch,
    preflight_launch_providers,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AgentProviderProfileError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    _close_base_pane,
    _create_tab,
    _create_workspace,
    _invoke,
    _list_rows,
    preflight_attest_launcher_capability,
    preflight_attest_store_schema,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (
    HerdrSessionStartError,
    _host_workspace_label,
    _lane_live_slot_tabs,
    _launch_target_for_lane,
    _parse_started_agent,
    _parse_tab_created,
    _parse_workspace_created,
    _tab_target_for_lane,
    _workspace_prefix,
    herdr_workspace_segment,
    resolve_container_plan,
    resolve_launch_order,
    resolve_placement_policy,
    slot_placement,
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


def _find_named_agent(
    rows: Sequence[Mapping[str, object]], assigned_name: str
) -> list:
    """Rows whose durable name equals ``assigned_name`` (fail-closed on duplicates)."""
    return [
        row
        for row in rows
        if isinstance(row, Mapping) and _norm(row.get(AGENT_KEY_NAME)) == assigned_name
    ]


def _lane_id_from_metadata(resolved_root: Path) -> str:
    """The recorded lane id for a lane worktree (``""`` when unrecorded).

    Shared project workspace model (Redmine #13377): a lane worktree's slots are
    ``mzb1_<project-ws>_<role>_<lane>``, so a relaunch from the worktree must
    recover the SAME lane segment ``sublane create`` launched with. The lane
    metadata record — keyed on the worktree's stable per-path token — carries it
    (``lane_id``, falling back to ``lane_label`` for a record written before the
    column existed). Read-only and fail-open to ``""`` (the caller fails closed:
    a lane slot is never minted with a guessed lane).
    """
    from mozyo_bridge.core.state.lane_metadata import load_lane_records

    token = derive_lane_workspace_token(str(resolved_root))
    record = load_lane_records().get(token)
    if record is None:
        return ""
    return _norm(getattr(record, "lane_id", "")) or _norm(
        getattr(record, "lane_label", "")
    )


@dataclass(frozen=True)
class _SlotPlan:
    """A per-provider decision (adopt / launch / dry-run plan) made before any launch.

    Classifying every slot up front lets the run pick a single launch-target
    workspace (and decide whether to create+reclaim a base pane) before it starts
    launching, so ``agent start`` can pass an explicit ``--workspace``.
    """

    provider: str
    assigned_name: str
    kind: str  # "adopt" | "launch" | "planned" | "stale" | "unattested"
    locator: str = ""  # adopted live locator (kind == "adopt") / stale residue pane (kind == "stale"); else ""
    detail: str = ""  # fail-closed reason for kind == "unattested" (Redmine #13637); else ""


def _resolve_workspace_id_readonly(resolved_root: Path) -> str:
    """Resolve a registered workspace's ``workspace_id`` for ``--dry-run``, read-only.

    The query-side mirror of :func:`register_workspace`'s identity precedence
    (Redmine #13595): an existing **anchor** pins the id, else an existing
    **registry row** for this canonical path — but purely read-only (never create
    the registry, write ``last_seen``, or touch the anchor; the exact defect this
    fixes called ``register_workspace`` before the dry-run branch). Fails closed
    rather than minting a fake assigned identity: both anchor names present is the
    same ambiguity the write path refuses (guess nothing), and no anchor + no
    registry row means no durable identity yet (register first). Linked worktrees
    never reach here — the :func:`prepare_session` inheritance branch
    (:func:`herdr_workspace_segment`) resolves them read-only.
    """
    if anchor_resolution(resolved_root).both_exist:
        raise HerdrSessionStartError(
            f"both {ANCHOR_RELATIVE.as_posix()} and "
            f"{ANCHOR_LEGACY_RELATIVE.as_posix()} exist in {resolved_root}; the new "
            "name is authoritative but a dry-run refuses to guess which identity a "
            f"real session-start would use — remove the legacy "
            f"{ANCHOR_LEGACY_RELATIVE.as_posix()} and re-run "
            "`mozyo-bridge workspace register`, then --dry-run"
        )
    anchor = read_anchor(resolved_root)
    if isinstance(anchor, dict):
        workspace_id = _norm(anchor.get("workspace_id"))
        if workspace_id:
            return workspace_id
    record = load_workspace_by_path(resolved_root)
    if record is not None:
        workspace_id = _norm(record.workspace_id)
        if workspace_id:
            return workspace_id
    raise HerdrSessionStartError(
        f"dry-run cannot resolve a durable workspace identity for {resolved_root} "
        "and refuses to register it (a dry-run has no side effect) or mint a fake "
        "one; run `mozyo-bridge workspace register` first, then re-run with --dry-run"
    )


def prepare_session(
    *,
    repo_root: Path,
    providers: Sequence[str],
    lane_id: str,
    env: Mapping[str, str],
    runner: Optional[Runner] = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    dry_run: bool = False,
    claude_permission_mode_default: Optional[str] = None,
    agent_launch: "Optional[AgentLaunchConfig]" = None,
    lane_placement: "Optional[LanePlacementConfig]" = None,
    attestation_reader: "Optional[Callable[[str], Optional[IdentityAttestationRecord]]]" = None,
    replacement_action_id: str = "",
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
    It reorders the requested ``providers`` (the configured provider launches first and
    occupies; the rest split beside it) and supplies each splitting launch's ``--split
    <dir>`` — including the tab-less ``default`` pair, previously left to the herdr server
    default. ``order`` never adds an unrequested peer; a configured primary that can only
    split beside a live sibling is reported ``order_deferred_until_full_relaunch`` rather
    than silently claimed (no swap / bounce — Non-goal: no live relayout). ``None`` keeps
    the requested order and the legacy split discipline (byte-for-byte pre-#13646).

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
    # Reject a duplicate (provider, lane) slot BEFORE any side effect (spec §5
    # slot-uniqueness). Every requested provider shares this run's lane, so a
    # repeated provider is a repeated slot: it would mint the SAME
    # `mzb1_<ws>_<role>_<lane>` name twice (two launches / two renames) — the read
    # side then fails closed with `multiple_matches`, leaving the session unusable.
    # Fail-closed rejection (not silent de-dup) matches the spec wording, so the CLI
    # can keep its repeatable `--agent` flag.
    seen_slots: set = set()
    lane_norm = _norm(lane_id)
    for provider in providers:
        slot = (provider, lane_norm)
        if slot in seen_slots:
            raise HerdrSessionStartError(
                f"duplicate requested slot for provider {provider!r} in lane "
                f"{lane_norm or 'default'!r}; each (provider, lane) may be prepared "
                "once — remove the duplicate `--agent` argument"
            )
        seen_slots.add(slot)
    # Validate the managed permission policy BEFORE any side effect (review j#73404):
    # the lane chokepoint requests (codex, claude), so a validation that only fires
    # inside the claude slot's launch would leave the codex gateway already started — a
    # partial lane — when the env override is invalid. Applicability is now data-driven
    # (#13441 R1-F2): every requested provider is asked, and one answers only if its
    # profile declares the managed permission concept. Validating here (rather than only
    # in the launch preflight below) keeps an invalid override fail-closed even on an
    # adopt-only run, exactly as before.
    for provider in providers:
        try:
            permission_mode_argv(
                provider, policy_default=claude_permission_mode_default, env=env
            )
        except InvalidPermissionMode as exc:
            raise HerdrSessionStartError(str(exc)) from exc
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

    result = SessionStartResult(workspace_id=workspace_id, lane_id=lane or "default")

    # Config-driven pane placement (Redmine #13646, Design Answer j#76564): resolve the
    # lane class's `(split, order)` ONCE, then reorder the requested providers so the
    # first-launched slot occupies the container. `lane_class` is the same axis
    # `agent_launch` keys on, resolved independently (no merge). An unset config yields
    # `(None, None)`, so every downstream decision stays byte-for-byte pre-#13646. The
    # decisions are pure (`herdr_lane_topology`).
    lane_class = "default" if result.lane_id == DEFAULT_LANE else "sublane"
    config_split, config_order = resolve_placement_policy(lane_placement, lane_class)
    providers = resolve_launch_order(providers, config_order)

    runner = runner or subprocess.run
    # Startup self-attestation reader (Redmine #13637): the adopt gate joins each live
    # name-match with its record. Injectable for tests; defaults to the store pinned to
    # the SAME `store_home` the wrapper writes to (j#76492 F1), fail-open None.
    attestation_read = (
        attestation_reader or HerdrIdentityAttestationStore(home=Path(store_home)).read
    )
    rows = _list_rows(binary, runner, timeout)

    # Pass 1 — classify every slot (adopt / launch / dry-run plan) before launching,
    # failing closed on a duplicate live name. Classifying up front is what lets us
    # pick ONE launch-target workspace (and decide whether to create+reclaim a base
    # pane) before the first `agent start`.
    plans: list = []
    for provider in providers:
        assigned_name = encode_assigned_name(workspace_id, provider, lane)
        existing = _find_named_agent(rows, assigned_name)
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

    # Launcher command-capability preflight (Redmine #13748) — the same fail-closed
    # boundary as the provider preflight above. The #13637 wrapper execs every launched
    # provider THROUGH `<attest_launcher> herdr agent-attest ...`; `resolve_attest_launcher`
    # proves the launcher is executable but NOT that its CLI still carries that subcommand.
    # An installed launcher lagging unreleased source answers the wrapper with argparse
    # exit 2, so every wrapped pane dies ~0.4s after start — the `sublane create` "live
    # locator then vanishes" failure. Verify it here, before any workspace/tab/agent write,
    # so a capability skew fails closed with recovery guidance and zero herdr actuation.
    # Gated on a resolved wrapper AND an actual launch plan: an unwrapped (`attest_launcher
    # == ""`) or adopt-only / dry-run run runs no wrapper, so it is never probed and stays
    # byte-invariant (Redmine #13637 fallback preserved).
    # The store-schema join (Redmine #13882) rides the same boundary: the probe above is
    # code-vs-code, so it cannot see that the SELECTED home holds an older shape on disk —
    # the live-but-unattested pair. Read-only; never migrates the shared home. See
    # `preflight_attest_store_schema`.
    if attest_launcher and launch_plans:
        observation = preflight_attest_launcher_capability(
            attest_launcher, runner, timeout, env
        )
        preflight_attest_store_schema(
            observation,
            store_home=Path(store_home),
            replacement_launch=bool((replacement_action_id or "").strip()),
        )

    # Resolve the launch-target workspace (Redmine #13330 / #13377 / #13380). Nothing
    # to launch (all adopt / dry-run) means no workspace create and no reclaim —
    # byte-invariant. Placement is lane-aware (#13380 dedicated sublane host): a
    # lane's own live/adopted slots pin the target first (a heal never splits a
    # pair); otherwise a lane slot joins the sublane host workspace the other lane
    # slots occupy (never the coordinator's), and the default lane joins only its
    # own pins — one mozyo workspace thus occupies a constant "project 1 + host 1"
    # herdr workspaces. When nothing pins a target the workspace is created
    # explicitly (labelled for a lane slot) so its empty root pane is a known
    # handle to reclaim, not one we scan for.
    launch_plans = [p for p in plans if p.kind == "launch"]
    target_workspace = ""
    if launch_plans:
        target_workspace = _launch_target_for_lane(
            rows,
            workspace_id,
            result.lane_id,
            [p.locator for p in plans if p.kind == "adopt"],
        )
        if not target_workspace:
            target_workspace, base_pane_id = _create_workspace(
                binary,
                repo_root,
                runner,
                timeout,
                env,
                label=(
                    _host_workspace_label(resolved_root)
                    if result.lane_id != DEFAULT_LANE
                    else ""
                ),
            )
            result.base_pane_id = base_pane_id
        result.herdr_workspace_id = target_workspace

    # Resolve the launch-target tab within the host workspace (Redmine #13411,
    # lane=tab). Only a non-default lane subdivides: its gateway + worker live in
    # ONE dedicated tab, so a host with N lanes shows N tabs instead of 2N loose
    # panes. The lane's own live/adopted slots pin their tab (a heal rejoins the
    # SAME tab). When nothing pins a tab, mint one explicitly ONLY for a FRESH lane
    # (no own live/adopted slots) — labelled with the lane key (cosmetic) so its
    # empty root pane is a known handle to reclaim. A heal of a legacy pre-#13411
    # lane whose live slots are LOOSE panes (own slots present, no tab pinned)
    # launches loose too, keeping the pair together (it migrates to a tab on a full
    # relaunch, the #13380 cohabiting precedent). The default lane never uses a tab,
    # so the coordinator path stays byte-invariant.
    #
    # The fresh-vs-loose decision keys on the lane's WHOLE live inventory in the
    # target workspace (`_lane_live_slot_tabs`), NOT this run's requested `plans`
    # (review j#74433 finding 1): a single-provider heal requests only one provider,
    # so the lane's OTHER live slot is in the inventory but never in `plans` —
    # counting requested adopts alone would mint a fresh tab for a live loose sibling
    # (splitting the pair).
    target_tab = ""
    lane_slot_tabs: list = []
    if launch_plans and result.lane_id != DEFAULT_LANE:
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
    plan_of_container = resolve_container_plan(
        rows,
        workspace_id,
        target_workspace,
        result.lane_id,
        lane_class=lane_class,
        target_tab=target_tab,
        lane_slot_tabs=lane_slot_tabs,
        config_split=config_split,
        config_order=config_order,
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
            )
        )
        if plan.kind == "launch":
            occupancy += 1

    # Reclaim the empty root panes we created — only after EVERY launch succeeded
    # (a launch failure raised above, so reaching here means all agents are live and
    # the workspace/tab is safe to keep with just its agent panes). Close only the
    # exact root pane ids we captured; a close failure is non-fatal cosmetic residue.
    # The workspace base pane (#13330) and the lane tab root pane (#13411) are
    # distinct handles — reclaim each independently, never one guessed for the other.
    if result.base_pane_id:
        reclaimed, detail = _close_base_pane(
            binary, result.base_pane_id, runner, timeout, env
        )
        result.base_pane_reclaimed = reclaimed
        result.base_pane_detail = detail
    if result.tab_pane_id:
        reclaimed, detail = _close_base_pane(
            binary, result.tab_pane_id, runner, timeout, env
        )
        result.tab_pane_reclaimed = reclaimed
        result.tab_pane_detail = detail
    return result


def _execute_slot(
    plan: _SlotPlan,
    *,
    repo_root: Path,
    workspace_id: str,
    lane: str,
    target_workspace: str,
    target_tab: str = "",
    split: str = "",
    focus: bool = False,
    binary: str,
    attest_launcher: str = "",
    store_home: str = "",
    env: Mapping[str, str],
    runner: Runner,
    timeout: float,
    resolved: Optional[ResolvedProviderLaunch] = None,
    launch_argv_extra: Sequence[str] = (),
    order_deferred: bool = False,
    replacement_action_id: str = "",
) -> SlotResult:
    if plan.kind == "adopt":
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_ADOPTED,
            locator=plan.locator,
            detail="live agent already carries the durable name; adopted",
        )
    if plan.kind == "planned":
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_PLANNED,
            detail="would launch (dry-run)",
        )
    if plan.kind == "stale":
        # A host-restart shell-residue slot (Redmine #13518 j#75329): surfaced read-only with
        # its residue locator so an owner-approved recovery (j#75331) can close that exact pane
        # and relaunch the same slot — this run performs no destructive side effect.
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_STALE,
            locator=plan.locator,
            detail=(
                "durable name held by a shell-residue pane with no live agent; requires an "
                "owner-approved close + same-slot relaunch (dirty worktree preserved)"
            ),
        )
    if plan.kind == "unattested":
        # A live slot whose startup self-attestation is absent / stale / missing /
        # conflicting (Redmine #13637): surfaced read-only with the exact fail-closed
        # reason and its live locator. herdr cannot read or repair a running process's
        # env, so recovery is an OWNER-approved close + same-slot relaunch (which re-runs
        # the self-check and writes a fresh present record) — never an automatic
        # destructive repair here.
        return SlotResult(
            provider=plan.provider,
            assigned_name=plan.assigned_name,
            outcome=SLOT_UNATTESTED,
            locator=plan.locator,
            detail=(
                f"{plan.detail}; requires an owner-approved close + same-slot relaunch "
                "(the relaunch re-runs the startup self-attestation self-check)"
            ),
        )
    # Launch with the durable name at start; the full `agent start` argv (self-identity
    # `--env`, `MOZYO_HERDR_BINARY`, `--permission-mode`, config tokens, Codex `-c`
    # overrides, lane `--tab`, and the #13637 self-attestation wrap) is assembled by the
    # cohesive sibling `herdr_launch_argv.build_agent_start_argv`.
    #
    # `env` is threaded in so argv[0] resolves to the provider's verified absolute
    # executable from the SAME trusted environment the launch itself runs under
    # (Redmine #13441): resolving against a different env than the one handed to
    # `_invoke` would verify one binary and exec another. An unresolvable / ambiguous
    # provider binary raises here — before `agent start` runs — so a failed resolution
    # never leaves a live pane behind.
    launch_argv = build_agent_start_argv(
        assigned_name=plan.assigned_name,
        provider=plan.provider,
        repo_root=repo_root,
        workspace_id=workspace_id,
        lane=lane,
        target_workspace=target_workspace,
        target_tab=target_tab,
        split=split,
        focus=focus,
        binary=binary,
        attest_launcher=attest_launcher,
        store_home=store_home,
        resolved=resolved,
        launch_argv_extra=launch_argv_extra,
        replacement_action_id=replacement_action_id,
    )
    started = _invoke(
        binary,
        launch_argv,
        runner,
        timeout,
        env=dict(env),
    )
    started_agent = _parse_started_agent(started.stdout)
    if started_agent is None:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} returned no usable live locator "
            "(expected result.agent.pane_id in an agent_started payload); refuse to "
            "return a blank handle"
        )
    locator, landed_tab = started_agent
    if not valid_target(locator):
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} returned an invalid live locator "
            f"{locator!r}; refuse to return a malformed handle"
        )
    # Verify the launch actually landed in the requested workspace (Redmine #13330
    # review j#73231). Passing `--workspace` is what keeps herdr from auto-creating a
    # second workspace (with its own empty base pane); if the returned locator is in a
    # DIFFERENT workspace (herdr ignored the flag / spec drift), trusting it would let
    # us close our created root pane while an auto-created base pane survives elsewhere,
    # unseen — exactly the failure this US must prevent. Fail closed instead (before
    # any reclaim), so the mislocated launch is surfaced rather than papered over.
    landed = _workspace_prefix(locator)
    if landed != target_workspace:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} landed in workspace "
            f"{landed or '<none>'!r} but --workspace {target_workspace!r} was requested; "
            "refuse to trust a mislocated launch (herdr may have auto-created another "
            "workspace with its own base pane)"
        )
    # Verify the launch actually landed in the requested TAB (Redmine #13411 review
    # j#74434 finding 2) — the tab-axis analogue of the workspace guard above. When
    # a lane tab is requested (`--tab`, non-default lane), the `agent_started`
    # envelope returns the landed `tab_id` (live probe #13411 j#74434); if herdr
    # ignored / misplaced `--tab` and landed in a DIFFERENT tab of the same
    # workspace, trusting it would leave the pair split and let us reclaim this run's
    # tab root pane against a mislocated launch. A missing tab id is equally
    # unverifiable, so both fail closed before any reclaim. The default lane passes
    # no `target_tab`, so this guard is skipped and its behaviour is byte-invariant.
    if target_tab and landed_tab != target_tab:
        raise HerdrSessionStartError(
            f"herdr agent start for {plan.provider!r} landed in tab "
            f"{landed_tab or '<none>'!r} but --tab {target_tab!r} was requested; "
            "refuse to trust a mislocated launch (the gateway/worker pair must "
            "share one dedicated lane tab)"
        )
    # `order_deferred` (see `slot_placement`): the configured primary could only be placed
    # as a split beside an already-live sibling, so the physical order waits for a full
    # relaunch. Say so rather than silently claim the order was applied.
    detail = "launched with the durable name and self-identity env (--env) at start"
    if order_deferred:
        detail += "; order_deferred_until_full_relaunch (no swap/bounce)"
    return SlotResult(
        provider=plan.provider,
        assigned_name=plan.assigned_name,
        outcome=SLOT_LAUNCHED,
        locator=locator,
        detail=detail,
    )


def _render_text(result: SessionStartResult) -> str:
    lines = [
        f"herdr session-start: workspace={result.workspace_id} lane={result.lane_id}"
    ]
    if result.herdr_tab_id:
        lines[0] += f" tab={result.herdr_tab_id}"
    for slot in result.slots:
        lines.append(
            f"  - {slot.provider}: {slot.outcome} name={slot.assigned_name}"
            + (f" locator={slot.locator}" if slot.locator else "")
        )
    if result.base_pane_id:
        state = (
            "reclaimed"
            if result.base_pane_reclaimed
            else f"reclaim-failed ({result.base_pane_detail})"
        )
        lines.append(f"base pane {result.base_pane_id}: {state}")
    if result.tab_pane_id:
        state = (
            "reclaimed"
            if result.tab_pane_reclaimed
            else f"reclaim-failed ({result.tab_pane_detail})"
        )
        lines.append(f"tab root pane {result.tab_pane_id}: {state}")
    return "\n".join(lines)


def cmd_herdr_session_start(args: argparse.Namespace) -> int:
    """CLI entry: prepare durable herdr identities for the workspace's agents."""
    from mozyo_bridge.application.commands_common import repo_root_from_args

    from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
        RepoLocalConfigError,
    )

    repo_root = repo_root_from_args(args)
    agents = getattr(args, "agent", None) or [PROVIDER_CLAUDE, PROVIDER_CODEX]
    lane_id = getattr(args, "lane", None) or ""
    dry_run = bool(getattr(args, "dry_run", False))
    # Config-driven launch argv (Redmine #13425) + pane placement (Redmine #13646):
    # resolved from the repo the command runs in. lane_class is derived inside
    # `prepare_session` from the resolved lane. One load serves both surfaces.
    try:
        repo_config = load_repo_local_config(repo_root)
    except RepoLocalConfigError as exc:
        die(f"herdr session-start failed: invalid repo-local config: {exc}")
        raise AssertionError("unreachable")
    agent_launch = repo_config.agent_launch
    lane_placement = repo_config.lane_placement
    try:
        result = prepare_session(
            repo_root=repo_root,
            providers=list(agents),
            lane_id=lane_id,
            env=os.environ,
            dry_run=dry_run,
            claude_permission_mode_default=COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
            agent_launch=agent_launch,
            lane_placement=lane_placement,
        )
    except HerdrSessionStartError as exc:
        die(f"herdr session-start failed: {exc}")
        raise AssertionError("unreachable")
    if getattr(args, "json", False):
        print(json.dumps(result.as_payload(), ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(result))
    return 0


__all__ = (
    "SLOT_ADOPTED",
    "SLOT_LAUNCHED",
    "SLOT_PLANNED",
    "SLOT_STALE",
    "SLOT_UNATTESTED",
    "HerdrSessionStartError",
    "SessionStartResult",
    "SlotResult",
    "cmd_herdr_session_start",
    "herdr_workspace_segment",
    "prepare_session",
)
