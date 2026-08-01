"""Composition of the update-authority resolver for the LAUNCH path (Redmine #14741).

The launch-side sibling of
:mod:`...e_110_execution_platform.f_130_handoff_routing.application.startup_admission_composition`,
and it exists for the same ruling: arming the authority fence is a **composition**
decision, never something the provider-neutral resolver defaults into (Design Answer D2
j#96288 item 3). ``preflight_launch_providers`` therefore stays a pure resolver that
evaluates authority only when a caller hands it one, and the production launch path
(``herdr_session_start``) asks this module what to hand it.

Scope is the same as the send side (D2 item 1): a provider with a trusted built-in updater
binding is armed; a provider without one is left unarmed, which reaches the classifier as
``not_evaluated`` — this ticket's gate does not apply to it. Promoting an unbound provider
to ``unknown`` is what refused every Claude send on every host in R3 (j#96202).

Returning ``None`` when no provider in the plan is bound keeps a launch that involves no
supported provider byte-invariant with the pre-#14741 path, including its cost: nothing
is resolved and no package manager is consulted.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
    ResolvedProviderLaunch,
    preflight_launch_providers as _preflight_unarmed,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.infrastructure.update_manager_adapter import (  # noqa: E501
    builtin_updater_target_probe,
    is_supported_provider,
)


#: A launch that nobody has tied to a provider update. The overwhelming majority, and the
#: pre-#14741 behavior: the authority gate does not apply, no package manager is consulted.
LAUNCH_CAUSE_GENERIC_FRESH = "generic_fresh"
#: A launch caused by a provider update — a verified update/install startup-blocker
#: observation, or an update outcome bound to this same lane generation. ONLY this cause
#: arms the strict authority gate (Design Answer j#96374 items 1-2).
LAUNCH_CAUSE_UPDATE_RELAUNCH = "update_relaunch"

LAUNCH_CAUSES: frozenset = frozenset(
    {LAUNCH_CAUSE_GENERIC_FRESH, LAUNCH_CAUSE_UPDATE_RELAUNCH}
)


class LaunchCauseError(ValueError):
    """An unrecognised launch cause. Fail closed rather than guess which one was meant."""


def launch_updater_target_resolver(
    providers: Sequence[str],
) -> Optional[Callable[[str], Any]]:
    """The typed resolver to arm a launch plan with, or ``None`` to leave it unarmed.

    ``None`` means no provider in this plan carries a built-in updater binding, so the
    fence has nothing to say about it. It never means "assume these are fine": the
    classifier records ``not_evaluated``, which is an absence of claim, not a pass.
    """
    if not any(is_supported_provider(provider) for provider in providers or ()):
        return None
    probe = builtin_updater_target_probe()

    def _resolver(provider_id: str):
        # Per-provider scope inside a MIXED plan. Returning ``None`` — not an empty
        # resolution — is the whole point: an empty-but-resolved answer classifies as
        # ``unknown`` and would fence an unbound provider just because a bound sibling
        # armed the plan, which is exactly the D2 item 1 failure. ``None`` means "not
        # evaluated for this one".
        return probe(provider_id) if is_supported_provider(provider_id) else None

    return _resolver


def preflight_launch_providers(
    providers: Sequence[str],
    env: Optional[Any] = None,
    *,
    launch_cause: str = LAUNCH_CAUSE_GENERIC_FRESH,
    **kwargs: Any,
) -> "dict[str, ResolvedProviderLaunch]":
    """The launch preflight, with the update-authority fence armed **by cause**.

    Signature-compatible with the unarmed resolver, so the production launch path
    (``herdr_session_start``) imports this name instead and its call is unchanged.

    **Only an update-derived launch is armed** (Design Answer j#96374 items 1-2). The
    previous cut armed every launch that involved a bound provider, which refused any
    launch whose env had no resolvable package manager and regressed 210 tests (j#96364):
    a fence that reads host state, armed on a path that never asked for it. A generic
    fresh launch is now byte-invariant with the pre-#14741 behavior and consults no
    package manager at all.

    ``launch_cause`` is a typed, provider-neutral token supplied by the composition root
    from a *verified* signal — an update/install startup-blocker observation, or an update
    outcome bound to this lane generation. It is deliberately NOT derivable here: a clean
    exit alone does not make a relaunch update-derived, and neither this module nor its
    callers re-guess it from pane text or ambient host state.

    An explicit ``updater_targets`` from the caller wins; this only supplies the default.
    Providers with no trusted built-in updater binding stay unarmed (``not_evaluated``)
    even under ``update_relaunch``, per D2 j#96288 item 1.
    """
    if launch_cause not in LAUNCH_CAUSES:
        raise LaunchCauseError(
            f"launch cause {launch_cause!r} is not recognised; allowed: "
            f"{sorted(LAUNCH_CAUSES)}. The cause decides whether the update-authority "
            f"gate is armed, so an unrecognised one is never treated as the harmless case."
        )
    if launch_cause == LAUNCH_CAUSE_UPDATE_RELAUNCH:
        kwargs.setdefault("updater_targets", launch_updater_target_resolver(providers))
    return _preflight_unarmed(providers, env, **kwargs)


__all__ = (
    "LAUNCH_CAUSES",
    "LAUNCH_CAUSE_GENERIC_FRESH",
    "LAUNCH_CAUSE_UPDATE_RELAUNCH",
    "LaunchCauseError",
    "ResolvedProviderLaunch",
    "launch_updater_target_resolver",
    "preflight_launch_providers",
)
