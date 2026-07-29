"""Action-time update-authority preflight (Redmine #14741).

Binds the pure classifiers in
:mod:`...f_160_provider_registry.domain.agent_provider_update_authority` to the trusted
environment, and does it at **action time** — the same placement ruling the #13760
startup-admission gate carries (Design Answer j#77947 Q2). A readiness-probe-time answer
is a different moment than the launch/send it is supposed to protect, and the #14741
loop lived exactly in that gap: the lane was probed ready, the provider's update prompt
consumed the Enter, the process exited 0, and the self-heal re-launched the same pinned
older binary a few seconds later.

What this evaluates, and what it refuses to do
----------------------------------------------
- It resolves where the managed launch points (the profile's trusted override, else the
  trusted PATH search) and where the provider's own updater reaches (the trusted PATH,
  always — an updater shells out to a package manager and does not honor mozyo's pin).
  A disagreement is :data:`...AUTHORITY_SPLIT`.
- It re-verifies a lane's exact executable binding when the caller supplies one, so a
  re-launch after an update cannot inherit a pin that no longer describes what is there.
- It **never repairs**: no PATH first-match relaxation, no override rewrite, no update
  invoked, no update prompt answered. Those are the #14741 guardrails, and the whole
  incident is what happens when something downstream "helpfully" proceeds.
- It never raises for an undecidable environment. Every failure to establish a fact
  becomes a typed ``unknown``, which does not admit a launch. The launch resolver's own
  fail-closed raises are untouched; this is a classifier layered beside it, not a
  replacement for it.

Nothing about the host leaves: the returned :class:`UpdateAuthority` carries fixed
tokens and a small count, never a path, a version, or an env value.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
    AgentProviderExecutableError,
    resolve_agent_launch,
    trusted_path_exec_targets,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
    AgentProviderProfileError,
    AgentProviderProfileRegistry,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_update_authority import (  # noqa: E501
    AUTHORITY_UNKNOWN,
    BINDING_NOT_EVALUATED,
    UpdateAuthority,
    classify_executable_binding,
    classify_update_authority,
)


def executable_identity(exec_target: str, version: str) -> str:
    """The exact binding token for one executable: its realpath AND its version.

    Both halves are required, and that is the #14741 lesson in one line. Pinning the
    path alone cannot see an in-place package-manager rewrite (same path, new binary);
    pinning the version alone cannot see the authority split (right version, wrong
    install). A same-version reinstall is correctly a *match* — nothing about what this
    lane runs changed — which is why the reinstall case is not a drift and must not be
    reported as one.

    Returns ``""`` when either half is missing: an incomplete identity is not a weaker
    binding, it is no binding, and :func:`classify_executable_binding` maps that to an
    explicit token rather than to a silent pass.
    """
    target = exec_target.strip() if isinstance(exec_target, str) else ""
    ver = version.strip() if isinstance(version, str) else ""
    if not target or not ver:
        return ""
    return f"{target}@{ver}"


def evaluate_update_authority(
    provider_id: str,
    env: Optional[Mapping[str, str]] = None,
    *,
    registry: Optional[AgentProviderProfileRegistry] = None,
    bound_identity: str = "",
    observed_identity: str = "",
) -> UpdateAuthority:
    """Evaluate both update-authority axes for ``provider_id`` (never raises).

    ``bound_identity`` / ``observed_identity`` are :func:`executable_identity` tokens.
    Leaving ``bound_identity`` empty leaves the binding axis
    :data:`...BINDING_NOT_EVALUATED`, so a caller that does not pin an identity keeps
    its pre-#14741 behavior exactly.

    A provider whose profile is unknown, whose protocol is undrivable, or whose
    executable cannot be resolved yields :data:`...AUTHORITY_UNKNOWN` rather than an
    exception: this runs *beside* the launch resolver, which already fails closed by
    raising, and a classifier that raises cannot be consulted on the very environments
    it exists to describe.
    """
    env = os.environ if env is None else env

    try:
        launch = resolve_agent_launch(provider_id, env, registry=registry)
        exec_target = launch.exec_target
    except AgentProviderExecutableError:
        # The managed side is undecidable, so the comparison is too. Still report the
        # updater's reach when it is readable: `reachable_installs` is what tells an
        # operator whether the fix is "pin one" or "install one".
        path_targets, readable = trusted_path_exec_targets(
            provider_id, env, registry=registry
        )
        return UpdateAuthority(
            provider=provider_id,
            authority=AUTHORITY_UNKNOWN,
            binding=classify_executable_binding(
                bound_identity=bound_identity, observed_identity=observed_identity
            ),
            reachable_installs=len(path_targets) if readable else 0,
        )
    except AgentProviderProfileError:
        # An unknown provider / malformed profile. Caught explicitly (and NOT as a bare
        # `Exception`) so a genuine defect in this module still surfaces as a crash
        # instead of being laundered into a plausible-looking `unknown` verdict. Note the
        # ordering: `AgentProviderExecutableError` subclasses this, so the resolution
        # failure above is handled by its own, more specific branch.
        return UpdateAuthority(
            provider=provider_id,
            authority=AUTHORITY_UNKNOWN,
            binding=BINDING_NOT_EVALUATED,
        )

    path_targets, readable = trusted_path_exec_targets(
        provider_id, env, registry=registry
    )
    return UpdateAuthority(
        provider=provider_id,
        authority=classify_update_authority(
            exec_target=exec_target,
            path_exec_targets=path_targets,
            path_readable=readable,
        ),
        binding=classify_executable_binding(
            bound_identity=bound_identity, observed_identity=observed_identity
        ),
        reachable_installs=len(path_targets),
    )


__all__ = (
    "evaluate_update_authority",
    "executable_identity",
)
