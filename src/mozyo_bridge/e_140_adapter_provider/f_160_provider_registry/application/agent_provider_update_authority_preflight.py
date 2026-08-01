"""Action-time update-authority preflight (Redmine #14741).

Binds the pure classifiers in
:mod:`...f_160_provider_registry.domain.agent_provider_update_authority` to the trusted
environment, and does it at **action time** — the same placement ruling the #13760
startup-admission gate carries (Design Answer j#77947 Q2). A readiness-probe-time answer
is a different moment than the launch/send it is supposed to protect, and the #14741
loop lived exactly in that gap: the lane was probed ready, the provider's update prompt
consumed the Enter, the process exited 0, and the self-heal re-launched the same pinned
older binary a few seconds later.

Where the updater's write target comes from (review j#95741 F2)
--------------------------------------------------------------
It is **supplied**, never inferred. The first cut derived it from the distinct realpaths
the provider's *command* resolved to on the trusted PATH, and that is a proxy for a fact
it cannot stand in for: an update runs the package manager, which writes to *its* global
prefix. A host with a single matching ``codex`` on PATH but a PATH ``npm`` owning a
different prefix was classified ``aligned``; worse, before the update the second install
does not exist at all, so no enumeration of the provider command could ever have observed
the split it was supposed to detect.

So this module takes an optional ``updater_targets`` probe — a callable returning the
install roots the provider's updater writes to, plus whether it could establish them. It
ships **no default probe**: establishing a package manager's prefix means asking that
package manager, which is a code-execution decision this layer will not make on its own.
With no probe the verdict is :data:`...AUTHORITY_UNKNOWN`, which is the honest answer and
the one Acceptance 2 explicitly allows ("検出不能を typed unknown として fail-closed").

What it refuses to do
---------------------
- It **never repairs**: no PATH first-match relaxation, no override rewrite, no update
  invoked, no update prompt answered. Those are the #14741 guardrails, and the whole
  incident is what happens when something downstream "helpfully" proceeds.
- It never raises for an undecidable environment. Every failure to establish a fact
  becomes a typed ``unknown``. The launch resolver's own fail-closed raises are
  untouched; this is a classifier layered beside it, not a replacement for it.

Nothing about the host leaves: the returned :class:`UpdateAuthority` carries fixed
tokens and a small count, never a path, a version, or an env value.
"""

from __future__ import annotations

import os
from typing import Callable, Mapping, Optional, Sequence, Tuple

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
    AgentProviderExecutableError,
    resolve_agent_launch,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
    AgentProviderProfileError,
    AgentProviderProfileRegistry,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_update_authority import (  # noqa: E501
    AUTHORITY_NOT_EVALUATED,
    AUTHORITY_UNKNOWN,
    BINDING_NOT_EVALUATED,
    UpdateAuthority,
    classify_executable_binding,
    classify_update_authority,
)

#: A probe that answers "where does this provider's own updater write?".
#:
#: Returns ``(install_roots, resolved)``. ``resolved`` is False whenever the probe could
#: not establish the answer — that is a typed unknown, never an empty-therefore-fine.
#: Injected rather than defaulted: see the module docstring.
UpdaterTargetProbe = Callable[[str], Tuple[Sequence[str], bool]]


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


def _probe_updater_targets(
    provider_id: str, probe: Optional[UpdaterTargetProbe]
) -> Tuple[Tuple[str, ...], bool]:
    """Run ``probe`` fail-closed: any failure or malformed answer is "not resolved".

    ``None`` is NOT a failure — it means the caller did not arm this gate, and it is
    reported by :func:`evaluate_update_authority` as ``not_evaluated`` rather than
    ``unknown`` (Design Answer D2 j#96288 item 1). R3 conflated the two, so every provider
    without a built-in updater binding — Claude included — was promoted to ``unknown`` and
    zero-actuation, and 29 unrelated send tests died. "Nobody asked" and "we asked and
    could not tell" are different facts and now have different tokens.
    """
    if probe is None:
        return ((), False)
    try:
        roots, resolved = probe(provider_id)
    except Exception:  # noqa: BLE001 - an injected probe is foreign code; it may do anything
        # A probe that raises has not established anything. Fail closed rather than let a
        # third-party failure decide a trust question by exception.
        return ((), False)
    if not resolved:
        return ((), False)
    normalised: list[str] = []
    for root in roots or ():
        if isinstance(root, str) and root.strip():
            real = os.path.realpath(root.strip())
            if real not in normalised:
                normalised.append(real)
    return (tuple(normalised), True)


def evaluate_update_authority(
    provider_id: str,
    env: Optional[Mapping[str, str]] = None,
    *,
    registry: Optional[AgentProviderProfileRegistry] = None,
    updater_targets: Optional[UpdaterTargetProbe] = None,
    bound_identity: str = "",
    observed_identity: str = "",
) -> UpdateAuthority:
    """Evaluate both update-authority axes for ``provider_id`` (never raises).

    ``updater_targets`` is the injected probe described in the module docstring; with no
    probe the authority axis is :data:`...AUTHORITY_UNKNOWN`.

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

    if updater_targets is None:
        # Unarmed: byte-invariant with every pre-#14741 call site (D2 item 1). The binding
        # axis is still honoured, because a caller that pinned an identity DID arm that.
        return UpdateAuthority(
            provider=provider_id,
            authority=AUTHORITY_NOT_EVALUATED,
            binding=classify_executable_binding(
                bound_identity=bound_identity, observed_identity=observed_identity
            ),
        )

    try:
        exec_target = resolve_agent_launch(provider_id, env, registry=registry).exec_target
    except AgentProviderExecutableError:
        roots, resolved = _probe_updater_targets(provider_id, updater_targets)
        return UpdateAuthority(
            provider=provider_id,
            authority=AUTHORITY_UNKNOWN,
            binding=classify_executable_binding(
                bound_identity=bound_identity, observed_identity=observed_identity
            ),
            updater_targets=len(roots) if resolved else 0,
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

    roots, resolved = _probe_updater_targets(provider_id, updater_targets)
    return UpdateAuthority(
        provider=provider_id,
        authority=classify_update_authority(
            exec_target=exec_target,
            updater_write_roots=roots,
            updater_roots_readable=resolved,
        ),
        binding=classify_executable_binding(
            bound_identity=bound_identity, observed_identity=observed_identity
        ),
        updater_targets=len(roots),
    )


__all__ = (
    "UpdaterTargetProbe",
    "evaluate_update_authority",
    "executable_identity",
)
