"""Fail-closed workflow-role -> runtime-provider resolution (Redmine #13569 Increment 2B).

Increment 2B rebinds the *runtime provider* the delegated-route / gateway / sublane
actuation sites key on from a hard-coded ``claude`` / ``codex`` literal onto the
workflow role's binding — the implementer / worker role's provider, the coordinator /
gateway role's provider — resolved from the repo-local
:class:`~..domain.role_provider_binding.RoleProviderBinding` (#12673 / #13157 config).

The one rule this module enforces (Coordinator Answer j#76969 correction 4): a caller
must NOT write ``binding.provider_for(role) or PROVIDER_CLAUDE`` and silently fall back
to a literal. The *default* is already carried by ``RoleProviderBinding.default()`` —
:func:`...workflow_binding_source.load_workflow_binding` returns a default-merged binding,
so ``provider_for`` returns the compatibility provider (``claude`` / ``codex``) under the
default. A ``None`` here therefore means a **genuinely unbound** role (a custom binding
that does not bind it) — an actuation-time fail-closed condition, never a reason to guess
a provider. The resolvers raise :class:`WorkflowProviderUnresolved` so an actuation caller
fails closed BEFORE any tmux / herdr send / retire side effect (zero-send), exactly like an
unknown / unlaunchable / mismatched provider.

This keeps the *workflow invariant* (gateway-via, worker-never-cross-boundary-direct)
intact while the provider it is expressed in follows the binding: renaming the worker's
provider does not weaken the gate, and a provider the binding does not assign never
becomes an actuation target.
"""

from __future__ import annotations

from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_binding_source import (
    load_workflow_binding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    RoleProviderBinding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ROLE_COORDINATOR,
    ROLE_IMPLEMENTER,
)


class WorkflowProviderUnresolved(ValueError):
    """A workflow role resolves to no runtime provider — the actuation must zero-send.

    Raised instead of silently defaulting to a literal provider (Coordinator Answer
    j#76969 correction 4). The default binding binds every workflow role, so this is a
    genuinely unbound custom binding; the caller fails closed before any side effect.
    """


def _binding(repo_root: Optional[str], binding: Optional[RoleProviderBinding]):
    """The binding to resolve against — an injected one, else the repo-local default-merge.

    ``load_workflow_binding`` fails closed on a malformed ``.mozyo-bridge/config.yaml``
    (the #13157 contract), so a broken config raises out of here rather than resolving a
    guessed provider.
    """
    if binding is not None:
        return binding
    resolved, _warnings = load_workflow_binding(repo_root)
    return resolved


def resolve_role_provider(
    role: str,
    repo_root: Optional[str] = None,
    *,
    binding: Optional[RoleProviderBinding] = None,
) -> str:
    """The runtime provider bound to ``role``, fail-closed (never a silent literal default).

    Returns the bound provider (``claude`` / ``codex`` under the default binding,
    byte-identical to the pre-#13569 literals). Raises :class:`WorkflowProviderUnresolved`
    if the role is unbound — the caller must then perform no side effect.
    """
    provider = _binding(repo_root, binding).provider_for(role)
    if not provider:
        raise WorkflowProviderUnresolved(
            f"workflow role {role!r} is not bound to any runtime provider; refusing to "
            f"guess a provider (Redmine #13569 j#76969 correction 4) — actuation must "
            f"zero-send"
        )
    return provider


def resolve_worker_provider(
    repo_root: Optional[str] = None,
    *,
    binding: Optional[RoleProviderBinding] = None,
) -> str:
    """The runtime provider bound to the implementer (worker) role, fail-closed.

    The same-lane worker / implementer the gateway forwards an ``implementation_request``
    to. Default: ``claude``. Rebinding the implementer role moves this without any literal
    edit at the actuation sites.
    """
    return resolve_role_provider(ROLE_IMPLEMENTER, repo_root, binding=binding)


def resolve_gateway_provider(
    repo_root: Optional[str] = None,
    *,
    binding: Optional[RoleProviderBinding] = None,
) -> str:
    """The runtime provider bound to the coordinator (gateway) role, fail-closed.

    The lane gateway / coordinator pane's provider. Default: ``codex``.
    """
    return resolve_role_provider(ROLE_COORDINATOR, repo_root, binding=binding)


__all__ = (
    "WorkflowProviderUnresolved",
    "resolve_role_provider",
    "resolve_worker_provider",
    "resolve_gateway_provider",
)
