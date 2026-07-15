"""Role-based wiring for the #12441 main-lane implementation guard (Redmine #13174).

The pure predicate
:func:`...f_130_handoff_routing.domain.handoff.main_lane_implementation_request_blocked`
decides whether an ``implementation_request`` addressed to the cockpit main lane must
fail closed. Before #13174 the ``orchestrate_handoff`` call site hard-coded ``claude``
as both the governed receiver and the pane-binding role, baking the implementer role's
*runtime provider* into the guard. #13174 rebinds that decision onto the workflow
**implementer role**: the implementer's provider is resolved from the repo-local
:class:`~...domain.role_provider_binding.RoleProviderBinding` (#12673 seam, #13157
config), so under the default binding the guard resolves the implementer to ``claude``
(byte-identical) and under a rebind it follows the binding instead of the literal.

This thin application gate owns the *IO* of that resolution — loading the repo-local
binding via :func:`...application.workflow_binding_source.load_workflow_binding` and
threading the resolved implementer provider into the pure predicate — so
``application/commands.py`` (an oversized, line-capped module under the module-health
gate) keeps only a single call and does not grow with the binding wiring. It lives in
f_140 (not f_130 beside the predicate) because the binding it consumes lives in f_140
and f_140 already depends on f_130's handoff domain; importing the binding from f_130
would invert that dependency.

Fail-closed on a broken config: a malformed ``.mozyo-bridge/config.yaml`` (unknown role /
empty provider / bad schema) raises ``RepoLocalConfigError`` out of the loader, per the
#13157 contract, rather than silently defaulting.
"""

from __future__ import annotations

import argparse
from typing import Any, Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    VIEW_KIND_COCKPIT_PANE,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    main_lane_implementation_request_blocked,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (
    resolve_gateway_provider,
    resolve_worker_provider,
)


def resolve_implementer_provider(repo_root: Optional[str] = None) -> str:
    """The runtime provider bound to the implementer role for ``repo_root`` (fail-closed).

    Loads the repo-local role->provider binding (a missing config / block is the
    behavior-preserving default codex/claude map) and returns the provider bound to the
    implementer role. Under the default binding this is ``claude``. A broken config fails
    closed through the loader's ``RepoLocalConfigError``; a genuinely unbound implementer
    raises :class:`~...workflow_provider_resolution.WorkflowProviderUnresolved` rather than
    silently defaulting to a literal (Redmine #13569 j#76969 correction 4).
    """
    return resolve_worker_provider(repo_root)


def resolve_coordinator_provider(repo_root: Optional[str] = None) -> str:
    """The runtime provider bound to the coordinator role for ``repo_root`` (fail-closed).

    The `coordinator` pseudo-target / callback resolution counterpart of
    :func:`resolve_implementer_provider` (Redmine #13174 j#72023). Under the default
    binding this is ``codex`` — byte-identical to the pre-#13174 resolution. A broken
    config fails closed through the loader's ``RepoLocalConfigError``; a genuinely unbound
    coordinator raises :class:`~...workflow_provider_resolution.WorkflowProviderUnresolved`
    rather than silently defaulting to a literal (Redmine #13569 j#76969 correction 4).
    """
    return resolve_gateway_provider(repo_root)


def main_lane_guard_blocked(
    args: argparse.Namespace,
    *,
    receiver: str,
    kind: Optional[str],
    preflight_target: Any,
) -> bool:
    """Apply the #12441 main-lane guard with the implementer role resolved by binding.

    Resolves the implementer provider from the repo-local binding (default: ``claude``)
    and runs the pure predicate against the resolved target's cockpit/lane/role facts.
    Returns ``True`` when ``orchestrate_handoff`` must fail closed (the caller emits the
    structured outcome and ``die``s); ``False`` when the send may proceed.
    """
    implementer_provider = resolve_implementer_provider()
    return main_lane_implementation_request_blocked(
        receiver=receiver,
        kind=kind,
        target_lane_id=preflight_target.lane_id,
        target_is_cockpit_pane=(preflight_target.view_kind == VIEW_KIND_COCKPIT_PANE),
        target_binds_implementer=preflight_target.binds_receiver(implementer_provider),
        implementer_provider=implementer_provider,
        has_main_lane_exception=bool(getattr(args, "main_lane_exception", None)),
    )


__all__ = (
    "main_lane_guard_blocked",
    "resolve_coordinator_provider",
    "resolve_implementer_provider",
)
