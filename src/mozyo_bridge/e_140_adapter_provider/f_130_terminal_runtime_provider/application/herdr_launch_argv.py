"""Herdr 0.8 managed-launch command and pane-environment assembly.

Herdr 0.8 separates placement from process launch. ``build_pane_launch_env`` renders
the identity, trusted binary, trusted source PATH and wrapper state injected by
``pane split``; ``build_agent_start_argv`` then starts the canonical provider kind in
that exact pane.
Keeping both renderers here gives session-start one directly testable launch contract.

Pure: :func:`build_agent_start_argv` is a total string-list transform (no I/O), and
:func:`resolve_attest_launcher` reads only the passed ``env`` mapping.

The provider command's argv[0] is the provider's verified absolute exec-target realpath
and the managed policy tokens are profile-spelled, but neither is resolved HERE: both
arrive pre-resolved on the :class:`ResolvedProviderLaunch` that
``preflight_launch_providers`` produced before the caller's first side effect (Redmine
#13441, review R1-F1). Keeping this builder pure is what guarantees it cannot fail after
a sibling provider has already been started — the partial-lane residue the lazy per-slot
resolution used to leave behind.

Redmine #14017: the provider command always keeps that realpath as its exec target, but
a wrapped launch of a provider whose trusted alias differs from the realpath (a
symlinked ``claude``) also carries the alias in the pane's
``MOZYO_PROVIDER_ARGV0`` environment, and the wrapper (:mod:`herdr_agent_attest`)
execs the realpath while handing the
process ``argv[0]=<alias>`` — the exec target stays the realpath, only the invocation
identity is the alias.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence

from mozyo_bridge.core.state.lane_epoch import MOZYO_LANE_EPOCH_ENV
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.codex_shell_identity import (
    CodexShellIdentity,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    _norm,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
    MOZYO_AGENT_ROLE_ENV,
    MOZYO_LANE_ID_ENV,
    MOZYO_WORKSPACE_ID_ENV,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    HERDR_BINARY_ENV,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (
    ResolvedProviderLaunch,
)

#: Optional launch-env override naming the absolute mozyo-bridge launcher used to
#: wrap the provider in the #13637 startup self-attestation self-check. When unset
#: the launcher is resolved from the trusted PATH (``shutil.which``); either way an
#: unresolvable / non-absolute value disables wrapping (byte-invariant fallback).
MOZYO_BRIDGE_LAUNCHER_ENV = "MOZYO_BRIDGE_LAUNCHER"

#: The launch-env key carrying the provider's trusted ``argv[0]`` alias to the #13637
#: self-attestation wrapper (Redmine #14017). The provider command after ``--`` keeps
#: the verified realpath as its first token (the exec target the wrapper runs); this
#: var, injected by ``pane split --env`` and read from the wrapper's ``os.environ``, tells
#: the wrapper to hand the process ``argv[0]=<alias>`` instead — decoupling the exec
#: target (realpath, the trust boundary) from the invocation identity (the trusted
#: symlink alias Claude requires to stay resident). Emitted ONLY when wrapping AND the
#: alias actually differs from the realpath, so an unsymlinked provider stays
#: byte-invariant. A launcher predating this contract simply does not read the var and
#: execs the realpath on both — the honest unwrapped-equivalent fallback that never
#: weakens the exec-target trust boundary (a pane environment key an older wrapper does
#: not know is inherited, not an argparse error, so no launch dies of version skew).
MOZYO_PROVIDER_ARGV0_ENV = "MOZYO_PROVIDER_ARGV0"

#: The launch-env key carrying the reserved startup-transaction ``action_id`` to the
#: #13637 wrapper (Redmine #14231, Design Consultation Answer j#84724). Unlike
#: ``--replacement-action-id`` (a CLI flag emitted only on the rare replacement-recovery
#: path), this value is non-empty on EVERY managed launch, so it rides a pane env key —
#: exactly the :data:`MOZYO_PROVIDER_ARGV0_ENV` precedent (docstring above): a herdr
#: environment key an older wrapper does not read is silently inherited, not an argparse
#: error, so a version-skewed wrapper (a different install resolved for `attest_launcher`)
#: degrades to "no execution-event evidence" rather than a hard launch failure. Always
#: injected (never conditional on non-empty) because, unlike the replacement id, this one
#: always has a value once ``reserve()`` has run.
MOZYO_STARTUP_ACTION_ID_ENV = "MOZYO_STARTUP_ACTION_ID"

#: Herdr 0.8's short process-local name.  The long ``mzb1`` identity remains the
#: routing and attestation authority; the wrapper uses this value only to find its
#: own raw ``agent list`` row before recording the long logical identity.
MOZYO_HERDR_NATIVE_NAME_ENV = "MOZYO_HERDR_NATIVE_NAME"

#: The wrapper subcommand every managed launch execs the provider THROUGH (Redmine
#: #13637): ``<launcher> herdr agent-attest ...``. Named once so the wrapper argv
#: (:func:`build_agent_start_argv`) and the capability probe
#: (:func:`build_attest_capability_probe_argv`, Redmine #13748) stay in lockstep — a
#: probe that verified a different subcommand than the wrapper actually runs would be
#: a false parity check.
ATTEST_WRAPPER_SUBCOMMAND: tuple[str, ...] = ("herdr", "agent-attest")

#: The stable marker the launcher's ``herdr agent-attest --help`` output MUST contain
#: for the capability probe to trust it (Redmine #13748 review R1). A bare exit-0 is
#: insufficient: a success-exit non-launcher (e.g. ``/usr/bin/true``) ignores the
#: probe args and exits 0 *without* the subcommand, so the real launch — which runs
#: the SAME launcher as ``argv[0]`` of the wrapper — would still exit before ``exec``ing
#: the provider, reproducing the vanishing lane #13748 closes. This marker is the first
#: flag the wrapper actually passes (:func:`build_agent_start_argv`), so its presence in
#: the help proves the launcher carries THIS ``agent-attest`` contract rather than merely
#: returning 0. Kept as the shared literal the wrapper renders so probe and wrapper stay
#: in lockstep.
ATTEST_CAPABILITY_MARKER = "--assigned-name"


def _is_absolute_executable(candidate: str) -> bool:
    """True iff ``candidate`` is an absolute path to an existing executable file.

    The same posture the herdr-binary resolver uses (``herdr_transport
    ._verify_executable``): absolute, a regular file after ``realpath`` (symlink
    resolved), and ``os.X_OK``. A non-absolute / missing / directory / non-executable
    value is rejected so a launcher can never be a repo-local or unrunnable path.
    """
    if not candidate or not os.path.isabs(candidate):
        return False
    real = os.path.realpath(candidate)
    return os.path.isfile(real) and os.access(real, os.X_OK)


def resolve_attest_launcher(env: Mapping[str, str]) -> str:
    """The absolute mozyo-bridge launcher to wrap the provider through, or ``""``.

    The #13637 managed launch execs the provider THROUGH ``mozyo-bridge herdr
    agent-attest`` so the agent self-attests its injected identity env before
    ``exec``ing the provider. This resolves that launcher from the trusted
    environment — an explicit :data:`MOZYO_BRIDGE_LAUNCHER_ENV`, else ``mozyo-bridge``
    on the passed env's PATH — and BOTH branches require an absolute path to an
    existing executable (never a repo-local / relative path, and never
    ``shutil.which``'s ambient ``os.environ`` fallback, so resolution is hermetic).
    An override that does not resolve to a runnable executable (e.g. a config typo)
    is rejected exactly like an unresolvable PATH (Redmine #13637 review j#76492
    Finding 2): returning ``""`` disables the wrapper so the launch falls back to the
    byte-invariant direct provider command rather than start an unrunnable wrapper
    (a dead pane), and the missing self-attestation record makes the adopt / doctor
    read side fail closed (the safe degradation, Design Answer j#76462).
    """
    override = _norm(env.get(MOZYO_BRIDGE_LAUNCHER_ENV))
    if override:
        return override if _is_absolute_executable(override) else ""
    import shutil

    path = _norm(env.get("PATH"))
    if not path:
        return ""
    found = shutil.which("mozyo-bridge", path=path)
    return found if found and _is_absolute_executable(found) else ""


def build_attest_capability_probe_argv(launcher: str) -> list[str]:
    """The argv that probes whether ``launcher`` can run the wrapper subcommand (pure).

    Redmine #13748: :func:`resolve_attest_launcher` proves the launcher is an *executable*
    but not that its CLI still carries ``herdr agent-attest`` — an installed launcher can
    lag unreleased source (measured: installed ``mozyo-bridge 0.10.0`` answers
    ``herdr agent-attest --help`` with argparse ``invalid choice`` / exit 2 while the source
    tree succeeds). ``--help`` is the actuation-free discriminant: argparse dispatches the
    subcommand and short-circuits on the help action BEFORE the wrapper's required
    ``--assigned-name`` / provider exec — without recording an attestation, spawning a
    provider, or touching a pane.

    The caller does NOT trust the exit code alone (review R1): a success-exit non-launcher
    (e.g. ``/usr/bin/true``) ignores these args and exits 0 without the subcommand, so it
    must additionally require :data:`ATTEST_CAPABILITY_MARKER` in the probe output — the
    positive signal that the launcher really carries this contract. The subcommand tokens
    are shared with the real wrapper (:data:`ATTEST_WRAPPER_SUBCOMMAND`) so the probe can
    never verify a path the launch would not take.
    """
    return [launcher, *ATTEST_WRAPPER_SUBCOMMAND, "--help"]


#: The read-only subcommand the launcher preflight runs to make a candidate launcher parse
#: a target repo's config with its OWN grammar (Redmine #14258 review j#87752 R4). Named
#: here, beside the wrapper subcommand, for the same lockstep reason: the argv the preflight
#: builds and the token the source advertises must never drift from the command the CLI
#: actually registers.
CONFIG_PARSE_SUBCOMMAND: tuple[str, ...] = ("config", "check-parse")

#: Exit code that subcommand returns when it REJECTS the document (as opposed to ``0`` for
#: "parses" or anything else for a mechanical failure). Mirrors
#: ``cli_config.CONFIG_CHECK_PARSE_REJECTED``; a drift guard pins the two together.
CONFIG_PARSE_REJECTED_EXIT = 2


def build_config_parse_probe_argv(launcher: str, config_path: str) -> list[str]:
    """The argv that makes ``launcher`` parse ``config_path`` with its own grammar (pure).

    Redmine #14258 R4. Every *summary* of the config grammar is a proxy, and a proxy was
    measured insufficient: commit ``d28e59e2`` added the nested ``lane_placement.by_lane_kind``
    key while leaving the supported version set and the top-level key set untouched, so a
    launcher predating it advertises an identical contract and still rejects the config as an
    unknown nested key. The only authority that can answer "can THIS launcher read THAT
    config" is the launcher's own parser, so the preflight hands it the exact target bytes
    and reads the exit code (:data:`CONFIG_PARSE_REJECTED_EXIT` = rejected).

    Read-only on both sides: the subcommand parses and returns without writing, and the path
    is one the caller owns (for a lane this run will create, the committed blob at the pinned
    base commit materialized to a temporary file) — never the launcher's own repo.
    """
    return [launcher, *CONFIG_PARSE_SUBCOMMAND, "--file", config_path]


def _provider_command(
    *,
    workspace_id: str,
    lane: str,
    resolved: ResolvedProviderLaunch,
    launch_argv_extra: Sequence[str],
) -> list[str]:
    """The provider command the herdr pane runs (`<abs executable> [flags...]`).

    Provider knowledge is *data* (Redmine #13441): ``resolved`` carries the profile's
    verified absolute argv[0] and its profile-spelled managed policy tokens, and the
    tool-shell behavior is a declared capability — so this builder holds no ``claude`` /
    ``codex`` branch and a new same-protocol provider needs no edit here.

    ``resolved`` is produced by ``preflight_launch_providers`` BEFORE the caller creates
    a workspace, a tab, or any agent (review R1-F1). This builder therefore performs
    **no** profile / registry / environment lookup of its own — argv[0], the managed
    tokens, AND the tool-shell capability all come off ``resolved`` — so it cannot fail,
    and so it cannot fail *after* a sibling provider has already been started and left a
    partial lane behind. Reading the capability live (via a global ``provider_has_capability``)
    was the R2-F1 registry split: it RAISED for a provider present only in an injected
    registry, and re-read a possibly-since-changed global inside the "pure" builder.

    The provider command's argv[0] here is the **verified absolute exec-target realpath**
    (Design Answer j#76725 Q1), not the bare provider name: leaving argv[0] bare would let
    the exec-time ``PATH`` decide which binary runs. It is the one token exempted from
    byte-invariance; every remaining token, and the render order, are unchanged. Redmine
    #14017 keeps the realpath here (it is what the wrapper actually ``exec``s) and carries
    any distinct trusted alias out-of-band in the pane environment, so this builder holds no
    provider branch and the exec target is never the alias.

    Reproducible permission mode for managed agents (Redmine #11925 / #13360): without the
    managed tokens here every herdr lane worker boots prompt-gated and stalls on its first
    gated command. Config-driven launch tokens (Redmine #13425) are appended AFTER the
    managed tokens (answer j#73949 Q4 render order) so the managed posture keeps its
    position. A provider that pinned ``tool_shell_env_overrides`` applies its own tool-shell
    env policy, so the attested identity is re-expressed as ``-c`` overrides appended last
    (Codex, #13614) — repo-local extras can never replace the attested tuple.
    """
    cmd = [resolved.executable]
    cmd.extend(resolved.managed_argv)
    if launch_argv_extra:
        cmd.extend(launch_argv_extra)
    if resolved.tool_shell_env_overrides:
        cmd.extend(
            CodexShellIdentity(workspace_id=workspace_id, lane_id=lane).launch_argv()
        )
    return cmd


def build_agent_start_argv(
    *,
    assigned_name: str,
    native_name: str,
    pane_locator: str,
    provider: str,
    workspace_id: str,
    lane: str,
    attest_launcher: str,
    resolved: ResolvedProviderLaunch,
    launch_argv_extra: Sequence[str],
    replacement_action_id: str = "",
    action_id: str = "",
    lane_epoch: str = "",
) -> list[str]:
    """Assemble the Herdr 0.8 pane-bound ``agent start`` argv (pure).

    ``native_name`` is the collision-checked 32-character Herdr identity; the longer
    ``assigned_name`` stays inside the self-attestation wrapper as mozyo's routing
    authority. Placement and environment injection are deliberately absent here: the
    caller has already created one exact pane with ``pane split`` and passes that locator
    through ``--pane``.

    When ``attest_launcher`` is present, the pane's canonical provider name resolves to
    that launcher through an action-private pane-local shell function. Consequently the
    tokens after Herdr's ``--`` begin at ``herdr agent-attest`` and the wrapper eventually
    execs the verified provider command. Without the wrapper, the function resolves
    directly to the verified provider executable and only its arguments follow ``--``.
    This function performs no filesystem or provider lookup.
    """
    provider_cmd = _provider_command(
        workspace_id=workspace_id,
        lane=lane,
        resolved=resolved,
        launch_argv_extra=launch_argv_extra,
    )
    if attest_launcher:
        run_cmd = [
            attest_launcher,
            *ATTEST_WRAPPER_SUBCOMMAND,
            ATTEST_CAPABILITY_MARKER,
            assigned_name,
            "--workspace-id",
            workspace_id,
            "--role",
            provider,
            "--lane",
            lane,
        ]
        # Redmine #13806 tranche D R2-F2: a REPLACEMENT launch carries the exact transaction
        # action_id into the fresh process's startup self-attestation. Emitted ONLY when
        # non-empty, so a normal (non-replacement) launch stays byte-invariant.
        if (replacement_action_id or "").strip():
            run_cmd += ["--replacement-action-id", replacement_action_id.strip()]
        # Redmine #14756: the lane epoch the lifecycle authority had minted when this launch
        # was planned, passed as the launcher-EXPECTED value. The wrapper compares it against
        # what actually landed in its own env (the injected `MOZYO_LANE_EPOCH` below), which
        # is the same "the launcher can prove it PASSED the value, only the process can prove
        # it ARRIVED" split #13637 established for the identity triplet. Emitted ONLY when
        # non-empty, so a lane with no minted epoch — and every pre-#14756 launch — stays
        # byte-invariant.
        if (lane_epoch or "").strip():
            run_cmd += ["--lane-epoch", lane_epoch.strip()]
        run_cmd += ["--", *provider_cmd]
    else:
        run_cmd = provider_cmd
    # Herdr 0.8 launches the canonical executable selected by ``--kind``.  When the
    # pane-local shell function points that canonical name at our attestation launcher,
    # the executable token itself must not be repeated: the args begin at the mozyo
    # subcommand.  The unwrapped function points at the verified provider executable, so
    # its args likewise exclude argv[0].
    agent_args = run_cmd[1:] if attest_launcher else provider_cmd[1:]
    return [
        "agent",
        "start",
        native_name,
        "--kind",
        provider,
        "--pane",
        pane_locator,
        "--",
        *agent_args,
    ]


def build_pane_launch_env(
    *,
    provider: str,
    native_name: str,
    workspace_id: str,
    lane: str,
    binary: str,
    source_path: str,
    attest_launcher: str,
    store_home: str,
    resolved: ResolvedProviderLaunch,
    action_id: str = "",
    lane_epoch: str = "",
) -> list[str]:
    """Return ``KEY=VALUE`` entries injected when the 0.8 pane is created."""
    if not source_path:
        raise ValueError("managed Herdr pane launch requires a non-empty trusted PATH")
    entries = [
        f"{MOZYO_WORKSPACE_ID_ENV}={workspace_id}",
        f"{MOZYO_AGENT_ROLE_ENV}={provider}",
        f"{MOZYO_LANE_ID_ENV}={lane}",
        f"{HERDR_BINARY_ENV}={binary}",
        # Do not prepend the shim here. macOS login shells can replace this value during
        # startup, which made PATH an unreliable launch authority. The exact pane gets a
        # shell-local canonical provider function before `agent start`; retaining only the
        # caller's trusted PATH here avoids a fallback through the shim if that preparation
        # is ever rejected.
        f"PATH={source_path}",
    ]
    if attest_launcher:
        entries.append(f"{MOZYO_HERDR_NATIVE_NAME_ENV}={native_name}")
        entries.append(f"MOZYO_BRIDGE_HOME={store_home}")
    if attest_launcher and (action_id or "").strip():
        entries.append(f"{MOZYO_STARTUP_ACTION_ID_ENV}={action_id.strip()}")
    if attest_launcher and (lane_epoch or "").strip():
        entries.append(f"{MOZYO_LANE_EPOCH_ENV}={lane_epoch.strip()}")
    argv0_alias = resolved.argv0 or resolved.executable
    if attest_launcher and argv0_alias != resolved.executable:
        entries.append(f"{MOZYO_PROVIDER_ARGV0_ENV}={argv0_alias}")
    return entries


__all__ = (
    "ATTEST_CAPABILITY_MARKER",
    "ATTEST_WRAPPER_SUBCOMMAND",
    "MOZYO_BRIDGE_LAUNCHER_ENV",
    "MOZYO_LANE_EPOCH_ENV",
    "MOZYO_HERDR_NATIVE_NAME_ENV",
    "MOZYO_PROVIDER_ARGV0_ENV",
    "build_agent_start_argv",
    "build_pane_launch_env",
    "CONFIG_PARSE_REJECTED_EXIT",
    "CONFIG_PARSE_SUBCOMMAND",
    "build_attest_capability_probe_argv",
    "build_config_parse_probe_argv",
    "resolve_attest_launcher",
)
