"""herdr managed-launch argv assembly — the cohesive sibling of session-start.

The `agent start` argv a managed launch runs is a cohesive block with several
overlays (self-identity `--env`, the trusted `MOZYO_HERDR_BINARY` injection, the
managed Claude `--permission-mode`, config-driven launch tokens, the Codex
tool-shell `-c` overrides, the lane `--tab` / `--split` placement, and the #13637
startup self-attestation wrapper). Homing it here — instead of inlining it in
:mod:`...herdr_session_start` — keeps the session-start composition root focused on
classification / placement / reclaim (the scheduled module-health reduction, see
``module_health.yaml``), and gives the argv assembly a single pure, directly-testable
function.

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
symlinked ``claude``) also carries the alias on a ``MOZYO_PROVIDER_ARGV0`` ``--env``
flag, and the wrapper (:mod:`herdr_agent_attest`) execs the realpath while handing the
process ``argv[0]=<alias>`` — the exec target stays the realpath, only the invocation
identity is the alias.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence

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
#: var, injected via herdr ``--env`` and read from the wrapper's ``os.environ``, tells
#: the wrapper to hand the process ``argv[0]=<alias>`` instead — decoupling the exec
#: target (realpath, the trust boundary) from the invocation identity (the trusted
#: symlink alias Claude requires to stay resident). Emitted ONLY when wrapping AND the
#: alias actually differs from the realpath, so an unsymlinked provider stays
#: byte-invariant. A launcher predating this contract simply does not read the var and
#: execs the realpath on both — the honest unwrapped-equivalent fallback that never
#: weakens the exec-target trust boundary (a herdr ``--env`` key an older wrapper does
#: not know is inherited, not an argparse error, so no launch dies of version skew).
MOZYO_PROVIDER_ARGV0_ENV = "MOZYO_PROVIDER_ARGV0"

#: The launch-env key carrying the reserved startup-transaction ``action_id`` to the
#: #13637 wrapper (Redmine #14231, Design Consultation Answer j#84724). Unlike
#: ``--replacement-action-id`` (a CLI flag emitted only on the rare replacement-recovery
#: path), this value is non-empty on EVERY managed launch, so it rides an ``--env`` key —
#: exactly the :data:`MOZYO_PROVIDER_ARGV0_ENV` precedent (docstring above): a herdr
#: ``--env`` key an older wrapper does not read is silently inherited, not an argparse
#: error, so a version-skewed wrapper (a different install resolved for `attest_launcher`)
#: degrades to "no execution-event evidence" rather than a hard launch failure. Always
#: injected (never conditional on non-empty) because, unlike the replacement id, this one
#: always has a value once ``reserve()`` has run.
MOZYO_STARTUP_ACTION_ID_ENV = "MOZYO_STARTUP_ACTION_ID"

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
    any distinct trusted alias out-of-band on a ``--env`` flag, so this builder holds no
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
    provider: str,
    repo_root: Path,
    workspace_id: str,
    lane: str,
    target_workspace: str,
    target_tab: str,
    split: str,
    focus: bool,
    binary: str,
    attest_launcher: str,
    store_home: str,
    resolved: ResolvedProviderLaunch,
    launch_argv_extra: Sequence[str],
    replacement_action_id: str = "",
    action_id: str = "",
) -> list[str]:
    """Assemble the full ``herdr agent start`` argv for one launched slot (pure).

    The durable ``assigned_name`` is applied at start (positional), so no separate
    ``agent rename``. ``--workspace`` pins placement (Redmine #13330) so herdr never
    auto-creates a second workspace; the self-identity triplet + the trusted
    ``MOZYO_HERDR_BINARY`` ride on ``--env`` flags (the server-spawned agent does not
    inherit the client env). ``--no-focus`` keeps the operator's focus.

    Startup self-attestation wrap (Redmine #13637, Design Answer j#76462): when
    ``attest_launcher`` resolves, the provider is run THROUGH ``mozyo-bridge herdr
    agent-attest`` so the agent's own process records a generation-bound
    self-attestation of its injected identity env before ``exec``ing the provider.
    When it does not resolve the run command is the bare provider (byte-invariant
    pre-#13637 launch) — a launch is never risked on a dead pane; the absent record
    then makes the adopt / doctor read side fail closed.

    ``store_home`` is the launcher's resolved mozyo-bridge home; it rides on
    ``--env MOZYO_BRIDGE_HOME=<home>`` (review j#76492 Finding 1). The wrapper writes
    the self-attestation to whatever home IT resolves, and a herdr-spawned process
    does NOT inherit the launching client's ``MOZYO_BRIDGE_HOME`` — so without this
    the wrapper would write to a different store than the launcher / adopt / doctor
    read, and a fresh launch's record would read as permanently ``absent``. Injecting
    the launcher's home pins writer and reader to one store. It is always injected
    (harmless when it equals the wrapper's default home).

    Provider argv[0] alias (Redmine #14017): a wrapped launch of a provider whose trusted
    absolute alias differs from its exec-target realpath additionally injects ``--env
    MOZYO_PROVIDER_ARGV0=<alias>``; the wrapper reads it and execs the realpath while
    handing the process ``argv[0]=<alias>``. It rides ``--env`` (not the provider command)
    so the exec target stays the realpath and an older wrapper that does not read it simply
    execs the realpath on both. Emitted only when wrapping AND the alias differs, so an
    unwrapped or unsymlinked launch is byte-invariant.

    Lane=tab placement (Redmine #13411) + config-driven split (Redmine #13646): a
    non-default lane's ``--tab`` is inserted right after ``--workspace``, and ``--split
    <dir>`` is appended for a slot that splits beside an occupant. ``split`` is the
    resolved direction the caller already decided (``""`` = emit no ``--split``): the
    session-start composition root resolves it from the lane class + ``lane_placement``
    config, so this pure builder never reads config and only renders the placement flags.
    A ``sublane`` slot that historically split gets ``split="right"`` (byte-for-byte the
    pre-#13646 literal) unless configured otherwise; the ``default`` lane passes
    ``split=""`` unless it is explicitly configured, and passes no ``target_tab`` either,
    so its unconfigured shape stays byte-for-byte the pre-#13411 command. ``--split`` is
    rendered independently of ``--tab`` (herdr 0.7.1 accepts them as independent optional
    flags, live ``--help`` j#76559), which is what lets the tab-less default pair split.

    ``focus`` selects ``--focus`` over the default ``--no-focus`` (Redmine #13646 review
    R1-F1 j#76613, Design Answer R1 j#76616). **herdr splits the container's ACTIVE pane —
    ``agent start`` has no pane-target flag** — so with every launch ``--no-focus`` the
    container's empty root pane stays active and the second slot's ``--split <dir>`` splits
    *the root*, not the first agent. Reclaiming that root (after all launches, #13330) then
    collapses the nested split away and leaves only the outer default ``right`` split the
    first agent implicitly created — i.e. the configured direction silently never applied
    (live-measured on both the tab-less default pair and the lane tab). Focusing the FIRST
    launch pins the container's split target to that agent, so the second slot splits the
    agent and the direction survives the reclaim (live-measured ``direction: down``). The
    caller fires this narrowly — fresh container, a full pair, explicit placement — so an
    unset / single-provider / heal / mixed-adopt launch keeps ``--no-focus`` and is
    byte-invariant, and a live pane is never focused / moved / swapped.
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
        run_cmd += ["--", *provider_cmd]
    else:
        run_cmd = provider_cmd
    env_flags = [
        "--env",
        f"{MOZYO_WORKSPACE_ID_ENV}={workspace_id}",
        "--env",
        f"{MOZYO_AGENT_ROLE_ENV}={provider}",
        "--env",
        f"{MOZYO_LANE_ID_ENV}={lane}",
        "--env",
        f"{HERDR_BINARY_ENV}={binary}",
    ]
    # MOZYO_BRIDGE_HOME rides along ONLY when wrapping (review j#76492 Finding 1): the
    # wrapper writes its self-attestation to the home it resolves, so it must resolve
    # the launcher's home — but a herdr-spawned process does not inherit the client's
    # MOZYO_BRIDGE_HOME. The unwrapped fallback writes no record, so it stays
    # byte-for-byte the pre-#13637 env set (no extra --env).
    if attest_launcher:
        env_flags += ["--env", f"MOZYO_BRIDGE_HOME={store_home}"]
    # Reserved startup-transaction action_id (Redmine #14231, Design Answer j#84724):
    # rides along ONLY when wrapping, same reasoning as MOZYO_BRIDGE_HOME above — only the
    # wrapper reads it (to append typed execution-stage events), so an unwrapped launch
    # stays byte-for-byte the pre-#14231 env set. Emitted whenever wrapping AND a caller
    # supplied one (empty on a caller that has no transaction, e.g. some test harnesses);
    # never gates or fails the launch either way.
    if attest_launcher and (action_id or "").strip():
        env_flags += ["--env", f"{MOZYO_STARTUP_ACTION_ID_ENV}={action_id.strip()}"]
    # Provider argv[0] alias (Redmine #14017): the provider command keeps the verified
    # realpath as its exec target (argv[0] token after `--`), but a symlinked provider
    # whose stable trusted alias differs from that realpath is handed argv[0]=<alias> by
    # the wrapper. Rides an `--env` flag so it survives herdr's non-inheriting spawn and
    # is read from the wrapper's own os.environ. Emitted ONLY when wrapping AND the alias
    # differs, so an unwrapped launch or an unsymlinked provider stays byte-invariant and
    # the exec target is never the alias.
    argv0_alias = resolved.argv0 or resolved.executable
    if attest_launcher and argv0_alias != resolved.executable:
        env_flags += ["--env", f"{MOZYO_PROVIDER_ARGV0_ENV}={argv0_alias}"]
    launch_argv = [
        "agent",
        "start",
        assigned_name,
        "--cwd",
        str(repo_root),
        "--workspace",
        target_workspace,
        *env_flags,
        "--focus" if focus else "--no-focus",
        "--",
        *run_cmd,
    ]
    placement_flags: list[str] = []
    if target_tab:
        placement_flags += ["--tab", target_tab]
    if split:
        placement_flags += ["--split", split]
    if placement_flags:
        insert_at = launch_argv.index("--workspace") + 2
        launch_argv[insert_at:insert_at] = placement_flags
    return launch_argv


__all__ = (
    "ATTEST_CAPABILITY_MARKER",
    "ATTEST_WRAPPER_SUBCOMMAND",
    "MOZYO_BRIDGE_LAUNCHER_ENV",
    "MOZYO_PROVIDER_ARGV0_ENV",
    "build_agent_start_argv",
    "build_attest_capability_probe_argv",
    "resolve_attest_launcher",
)
