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

:func:`resolve_attest_launcher` reads only the passed ``env`` mapping.

:func:`build_agent_start_argv` is a total string-list transform over its arguments with
ONE trusted-environment read (Redmine #13441): argv[0] is the provider's verified
absolute executable, resolved from the passed ``env`` (its ``PATH`` / the profile's
trusted override) by ``agent_provider_executable``. That read is deliberate — argv[0] can
no longer be a bare name decided by the exec-time ``PATH`` (Design Answer j#76725 Q1) —
and it fails closed *before* the caller creates a pane, so an unresolvable or ambiguous
binary never becomes a live process. Pass ``env`` explicitly to keep a call hermetic;
``None`` reads ``os.environ``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional, Sequence

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
    resolve_agent_argv0,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    managed_flag_for,
    provider_has_capability,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AgentCapability,
    ManagedFlagConcept,
)

#: Optional launch-env override naming the absolute mozyo-bridge launcher used to
#: wrap the provider in the #13637 startup self-attestation self-check. When unset
#: the launcher is resolved from the trusted PATH (``shutil.which``); either way an
#: unresolvable / non-absolute value disables wrapping (byte-invariant fallback).
MOZYO_BRIDGE_LAUNCHER_ENV = "MOZYO_BRIDGE_LAUNCHER"


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


def _provider_command(
    provider: str,
    *,
    workspace_id: str,
    lane: str,
    claude_permission_mode: Optional[str],
    launch_argv_extra: Sequence[str],
    env: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """The provider command the herdr pane runs (`<abs executable> [flags...]`).

    Provider knowledge is now *data* (Redmine #13441): the profile registry supplies
    argv[0], the managed-flag spelling, and the tool-shell capability, so this builder
    holds no ``claude`` / ``codex`` branch and a new same-protocol provider needs no
    edit here.

    argv[0] is the **verified absolute realpath** resolved from the trusted environment
    (Design Answer j#76725 Q1), not the bare provider name: leaving argv[0] bare would
    let the exec-time ``PATH`` decide which binary runs. This is the one token exempted
    from byte-invariance; every remaining token, and the render order, are unchanged.
    Resolution fails closed *before* the caller creates a pane.

    Reproducible permission mode for managed agents (Redmine #11925 / #13360): the tmux
    managed-pane chokepoint has always appended ``--permission-mode <mode>``; without the
    same suffix here every herdr lane worker boots prompt-gated and stalls on its first
    gated command. The mode arrives pre-resolved (and pre-validated) from
    ``prepare_session``, and the *flag spelling* now comes from the provider's profile.
    Config-driven launch tokens (Redmine #13425) are appended AFTER the managed flag
    (answer j#73949 Q4 render order) so the managed posture keeps its position. A provider
    declaring ``tool_shell_env_overrides`` applies its own tool-shell env policy, so the
    attested identity is re-expressed as ``-c`` overrides appended last (Codex, #13614) —
    repo-local extras can never replace the attested tuple.
    """
    cmd = [resolve_agent_argv0(provider, env)]
    permission_flag = managed_flag_for(provider, ManagedFlagConcept.PERMISSION_MODE)
    if permission_flag and claude_permission_mode:
        cmd.extend([permission_flag, claude_permission_mode])
    if launch_argv_extra:
        cmd.extend(launch_argv_extra)
    if provider_has_capability(provider, AgentCapability.TOOL_SHELL_ENV_OVERRIDES):
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
    claude_permission_mode: Optional[str],
    launch_argv_extra: Sequence[str],
    env: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """Assemble the full ``herdr agent start`` argv for one launched slot.

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
        provider,
        workspace_id=workspace_id,
        lane=lane,
        claude_permission_mode=claude_permission_mode,
        launch_argv_extra=launch_argv_extra,
        env=env,
    )
    if attest_launcher:
        run_cmd = [
            attest_launcher,
            "herdr",
            "agent-attest",
            "--assigned-name",
            assigned_name,
            "--workspace-id",
            workspace_id,
            "--role",
            provider,
            "--lane",
            lane,
            "--",
            *provider_cmd,
        ]
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
    "MOZYO_BRIDGE_LAUNCHER_ENV",
    "build_agent_start_argv",
    "resolve_attest_launcher",
)
