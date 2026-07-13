"""Trusted resolution of an agent provider's executable (Redmine #13441 j#76725 Q1).

Which binary a managed agent pane actually runs is an **outer trust boundary**. The
pre-#13441 launch paths rendered a bare ``claude`` / ``codex`` as ``argv[0]``, which
leaves provider identity to the ambient ``PATH`` at exec time — a repo checkout, a
shell rc, or a stale shim can decide what runs. This module resolves a provider to a
*verified absolute realpath* **before** any pane / process side effect, exactly like
the trusted herdr-binary (#13496) and onboarding-provider (#13497) resolvers:

- an explicit override via the profile's own trusted-env variable
  (``TrustedExecutable.env_override``, e.g. ``MOZYO_AGENT_CLAUDE_BINARY``) must itself
  be **absolute** — a relative override is refused, never resolved against the cwd —
  and is verified as a regular executable;
- otherwise the profile's command **basename** is searched on the **trusted** ``PATH``
  only. A ``PATH`` with any empty or relative component is refused whole (a shell would
  resolve those against the cwd), and a bare name never falls back to an ambient
  ``PATH``;
- the executable bit is checked against the symlink-resolved ``realpath``, and that
  ``realpath`` is what is returned as ``argv[0]`` — provider identity is then
  deterministic across cwd and symlinks;
- the ``PATH`` search collects the **distinct** executable realpaths: zero -> missing,
  one -> resolved, two or more -> ambiguous fail-closed. First-match is not an
  ambiguity check.

**The compatibility carve-out this implements.** j#75397 asked for both (a) trusted
absolute-realpath execution and (b) byte-invariant built-in argv. Those cannot both
hold: resolving and then *re-emitting the bare name* would reintroduce the TOCTOU /
PATH-substitution hole the resolution exists to close. Design Answer j#76725 Q1 ruled
for the trust boundary and narrowed byte-invariance to **"every argv token except
argv[0]", plus default topology, provider order, CLI output, and behavior**. So the
built-in ``claude`` / ``codex`` launches now exec an absolute realpath and are
otherwise unchanged.

Every failure raises :class:`AgentProviderExecutableError` — a distinct, launch-layer
error (the onboarding resolver's ``ConversationProviderError`` is *not* reused: its
``conversation_provider_unavailable`` reason code belongs to the onboarding boundary,
per j#76715). The caller must then mutate nothing: no pane, no process, no tmux side
effect.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    AGENT_PROVIDER_PROFILES,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AgentCapability,
    AgentProviderProfile,
    AgentProviderProfileError,
    AgentProviderProfileRegistry,
    InteractionProtocol,
)

#: Interaction protocols this launch mechanism can actually drive. A profile whose
#: protocol is outside this set is data-expressible but NOT launchable — it needs
#: adapter code — and asking to launch it fails closed here, before a pane exists,
#: rather than starting a provider mozyo cannot drive (#13441 honest-limit clause).
LAUNCHABLE_PROTOCOLS: frozenset[InteractionProtocol] = frozenset(
    {InteractionProtocol.INTERACTIVE_CLI_TUI}
)


class AgentProviderExecutableError(AgentProviderProfileError):
    """A provider's executable cannot be resolved from the trusted environment.

    Subclasses :class:`AgentProviderProfileError` so a caller may catch the whole
    profile/launch failure family at one boundary, while still distinguishing an
    unresolvable *binary* from a malformed *profile*.
    """


def _verify_executable_realpath(candidate: str) -> Optional[str]:
    """The symlink-resolved ``realpath`` iff it is a regular executable, else ``None``.

    The executable bit is checked against the ``realpath`` (a dangling or
    non-executable symlink fails), and that same ``realpath`` is what becomes
    ``argv[0]`` — never a cwd- or symlink-dependent path.
    """
    real = os.path.realpath(candidate)
    if os.path.isfile(real) and os.access(real, os.X_OK):
        return real
    return None


def _trusted_path_dirs(env: Mapping[str, str], *, provider_id: str) -> list[str]:
    """The trusted ``PATH`` components, fail-closed on any unsafe one.

    No ``PATH`` (or an empty one) yields no search directory — a bare command simply
    does not resolve, never falling back to the ambient process ``PATH``. If any
    component is empty or relative, the whole ``PATH`` is refused rather than silently
    dropping that component (which would quietly rewrite the caller's ``PATH``
    semantics).
    """
    raw = env.get("PATH", "")
    if not isinstance(raw, str) or raw == "":
        return []
    components = raw.split(os.pathsep)
    unsafe = [comp for comp in components if comp == "" or not os.path.isabs(comp)]
    if unsafe:
        raise AgentProviderExecutableError(
            f"refusing to resolve agent provider {provider_id!r}: the trusted PATH "
            f"contains unsafe (empty or relative) component(s) {sorted(unsafe)!r}, "
            f"which a shell would resolve against the current directory"
        )
    dirs: list[str] = []
    for comp in components:
        if comp not in dirs:
            dirs.append(comp)
    return dirs


def require_launchable(profile: AgentProviderProfile) -> None:
    """Fail closed unless ``profile`` declares a protocol/capability mozyo can drive.

    Checked *before* resolution and therefore before any side effect: an unsupported
    interaction protocol, or a provider that does not declare
    :attr:`AgentCapability.INTERACTIVE_TUI`, must never reach a pane. This is the
    seam that keeps "a data profile absorbs same-protocol providers" honest — a
    different-protocol provider is rejected instead of mis-launched.
    """
    if profile.protocol not in LAUNCHABLE_PROTOCOLS:
        raise AgentProviderExecutableError(
            f"agent provider {profile.provider_id!r} declares interaction protocol "
            f"{profile.protocol.value!r}, which the launch mechanism cannot drive "
            f"(launchable: {sorted(p.value for p in LAUNCHABLE_PROTOCOLS)}). A provider "
            f"with a different interaction protocol needs adapter code, not a data "
            f"profile (Redmine #13441)."
        )
    if not profile.has_capability(AgentCapability.INTERACTIVE_TUI):
        raise AgentProviderExecutableError(
            f"agent provider {profile.provider_id!r} does not declare the "
            f"{AgentCapability.INTERACTIVE_TUI.value!r} capability; the managed launch "
            f"mechanism only starts interactive-TUI providers"
        )


def resolve_agent_executable(
    provider_id: str,
    env: Optional[Mapping[str, str]] = None,
    *,
    registry: Optional[AgentProviderProfileRegistry] = None,
) -> str:
    """Resolve ``provider_id`` to a verified absolute executable, or fail closed.

    Order: the profile's trusted-env override (verified absolute + executable), then
    the trusted ``PATH`` search for the profile's command basename. Raises
    :class:`AgentProviderExecutableError` on an unknown provider, an undrivable
    protocol / missing capability, an unsafe ``PATH``, a non-executable override, a
    missing binary, or an ambiguous one — in every case the caller must spawn nothing.

    ``registry`` is injectable so tests (and any future non-built-in composition) can
    resolve against a synthetic profile set without touching the packaged data.
    """
    env = os.environ if env is None else env
    profiles = AGENT_PROVIDER_PROFILES if registry is None else registry

    profile = profiles.require(provider_id)
    require_launchable(profile)

    override = env.get(profile.executable.env_override)
    if isinstance(override, str) and override.strip():
        value = override.strip()
        if not os.path.isabs(value):
            raise AgentProviderExecutableError(
                f"{profile.executable.env_override} must be an absolute path; refusing "
                f"to resolve a relative agent-provider override against the current "
                f"directory"
            )
        verified = _verify_executable_realpath(value)
        if verified is None:
            raise AgentProviderExecutableError(
                f"{profile.executable.env_override} does not point at a regular "
                f"executable file; refusing to launch agent provider {provider_id!r}"
            )
        return verified

    name = profile.executable.command
    realpaths: list[str] = []
    for directory in _trusted_path_dirs(env, provider_id=provider_id):
        verified = _verify_executable_realpath(os.path.join(directory, name))
        if verified is not None and verified not in realpaths:
            realpaths.append(verified)

    if not realpaths:
        raise AgentProviderExecutableError(
            f"the {name!r} executable for agent provider {provider_id!r} was not found "
            f"as a regular executable on the trusted PATH; set "
            f"{profile.executable.env_override} to an absolute path to pin it"
        )
    if len(realpaths) > 1:
        raise AgentProviderExecutableError(
            f"the {name!r} executable for agent provider {provider_id!r} resolves to "
            f"{len(realpaths)} distinct executables on the trusted PATH; refusing to "
            f"select ambiguously — set {profile.executable.env_override} to pin one"
        )
    return realpaths[0]


def resolve_agent_argv0(
    provider_id: str,
    env: Optional[Mapping[str, str]] = None,
    *,
    registry: Optional[AgentProviderProfileRegistry] = None,
) -> str:
    """The ``argv[0]`` a managed launch runs for ``provider_id`` (verified absolute).

    The launch-site accessor. Named for its role at the call sites so the intent is
    legible there: this is the one token j#76725 Q1 exempts from byte-invariance.
    """
    return resolve_agent_executable(provider_id, env, registry=registry)


@dataclass(frozen=True)
class ResolvedProviderLaunch:
    """Everything a launch needs for one provider, resolved BEFORE any side effect.

    All fields are *values* pinned from ONE registry snapshot, so the slot builder
    renders an argv performing no further profile/registry lookup — the thing that
    resolved is exactly the thing that runs, and the builder cannot fail (or read a
    since-changed global) after a sibling provider has already started (Redmine #13441
    review R1-F1 / R2-F1):

    - ``executable`` — the verified absolute ``argv[0]``.
    - ``managed_argv`` — the profile-spelled managed policy tokens (e.g.
      ``("--permission-mode", "auto")``) or ``()``.
    - ``tool_shell_env_overrides`` — whether the provider re-expresses its attested
      identity as tool-shell ``-c`` overrides (Codex, #13614). Pinned here so the
      builder never re-reads ``provider_has_capability`` (which would be a global
      lookup that RAISES for a provider present only in an injected registry — the
      R2-F1 registry split).
    """

    provider_id: str
    executable: str
    managed_argv: tuple[str, ...] = ()
    tool_shell_env_overrides: bool = False


def preflight_launch_providers(
    providers: Sequence[str],
    env: Optional[Mapping[str, str]] = None,
    *,
    permission_mode_default: Optional[str] = None,
    registry: Optional[AgentProviderProfileRegistry] = None,
) -> "dict[str, ResolvedProviderLaunch]":
    """Resolve EVERY launch provider up front, or raise having touched nothing.

    Redmine #13441 review R1-F1. Resolving lazily inside the per-slot builder meant a
    session that launches a pair created the workspace, created the tab, and started
    the FIRST provider before the SECOND provider's executable was found to be missing
    — leaving a partial lane with a live agent behind. Resolution is a pure question
    about the environment, so it belongs entirely before the first side effect.

    This resolves, for every provider in ``providers``: the profile (unknown -> raise),
    the interaction protocol and required capability (undrivable -> raise), the trusted
    executable (missing / ambiguous / unsafe PATH / bad override -> raise), and the
    managed permission policy (invalid mode -> raise). Only if ALL of them succeed does
    it return; the caller may then create a workspace, a tab, and agents knowing no
    provider can still fail resolution.

    The caller MUST invoke this before its first mutation and pass the results down,
    rather than re-resolving per slot.
    """
    # Imported here: the permission policy lives in the execution-platform context and
    # imports this package's registry, so a module-level import would be a cycle.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
        permission_mode_argv,
    )
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
        AGENT_PROVIDER_PROFILES,
    )

    # ONE snapshot for the whole preflight: the executable, the managed policy, and the
    # capabilities all resolve from the same registry (Redmine #13441 review R2-F1). The
    # first cut resolved only the executable from `registry` and let the managed policy /
    # capability fall through to the global `AGENT_PROVIDER_PROFILES`, so a provider
    # present only in an injected registry resolved an empty managed argv and then made
    # the "pure" builder RAISE `unknown agent provider`.
    profiles = AGENT_PROVIDER_PROFILES if registry is None else registry

    resolved: dict[str, ResolvedProviderLaunch] = {}
    for provider_id in providers:
        if provider_id in resolved:
            continue
        executable = resolve_agent_executable(provider_id, env, registry=registry)
        profile = profiles.require(provider_id)
        resolved[provider_id] = ResolvedProviderLaunch(
            provider_id=provider_id,
            executable=executable,
            managed_argv=permission_mode_argv(
                provider_id,
                policy_default=permission_mode_default,
                env=env,
                registry=registry,
            ),
            tool_shell_env_overrides=profile.has_capability(
                AgentCapability.TOOL_SHELL_ENV_OVERRIDES
            ),
        )
    return resolved


__all__ = (
    "LAUNCHABLE_PROTOCOLS",
    "AgentProviderExecutableError",
    "ResolvedProviderLaunch",
    "preflight_launch_providers",
    "require_launchable",
    "resolve_agent_argv0",
    "resolve_agent_executable",
)
