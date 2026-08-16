"""Scope -> project-gateway route provider resolution (Redmine #15414).

The Herdr project-gateway inventory always verified the requested receiver
against the scope's ``provider_binding`` (``provider_binding_mismatch``), while
every CLI sibling REQUESTED a hard-coded ``codex``. This module owns the single
durable-authority resolution BOTH sides read — the inventory gate through the
thin :class:`...ProjectGatewayBackendInventoryUseCase` delegates, and the CLI
family through :func:`resolve_scope_route_provider` — so the requested receiver
and the verifying gate cannot drift apart.

Split out of :mod:`.project_gateway_backend_inventory` (module-health size
boundary): this is pure durable-config resolution — workflow role bindings +
provider_binding — with no live inventory, no pane scan, and no backend
support registry. The typed refusal vocabulary and the selector constants stay
owned by the inventory module; this module imports them lazily to avoid a
top-level cycle with its delegating caller.
"""

from __future__ import annotations

from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    PROVIDER_CODEX,
    ROLE_COORDINATOR as PROVIDER_ROLE_COORDINATOR,
    ROLE_PROJECT_GATEWAY as PROVIDER_ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    ROLE_COORDINATOR,
)

#: The durable role-binding roles that may own a project scope's gateway lane.
GATEWAY_AUTHORITY_ROLES: frozenset = frozenset({ROLE_PROJECT_GATEWAY, ROLE_COORDINATOR})


def _inventory():
    # Lazy: the inventory module delegates its binding resolution here, so a
    # top-level mutual import would cycle. Only the typed error/refusal surface
    # and the selector constants are read back.
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (  # noqa: E501
        project_gateway_backend_inventory as inv,
    )

    return inv


def parsed_scope_role_bindings(ops, repo_root: Path, *, backend: str):
    """The durable workflow-role bindings, or the inventory's typed refusals."""
    inv = _inventory()
    try:
        parsed = ops.parsed_role_bindings(repo_root)
    except Exception as exc:  # noqa: BLE001 - durable authority unavailable
        raise inv.ProjectGatewayInventoryError(
            "workflow_role_binding_unavailable",
            "the durable workflow-role binding could not be read",
            backend=backend,
        ) from exc
    if not getattr(parsed, "ok", False):
        raise inv.ProjectGatewayInventoryError(
            "workflow_role_binding_invalid",
            "the durable workflow-role binding is malformed",
            backend=backend,
        )
    return parsed


def scope_route_providers(
    ops, repo_root: Path, scope: str, *, parsed, backend: str, selector: str
):
    """The scope's durable gateway authority and its bound route providers.

    The single provider_binding resolution both the inventory gate and the
    CLI-side receiver derivation (:func:`resolve_scope_route_provider`) read.
    Returns ``(authority, gateway_provider, child_provider)``.
    """
    inv = _inventory()
    authority_matches = [
        binding
        for binding in parsed.bindings
        if binding.role in GATEWAY_AUTHORITY_ROLES and binding.project_scope == scope
    ]
    if len(authority_matches) != 1:
        raise inv.ProjectGatewayInventoryError(
            "project_scope_binding_ambiguous"
            if authority_matches
            else "project_scope_binding_missing",
            "the project scope must have exactly one durable coordinator/project-gateway binding",
            backend=backend,
        )
    authority = authority_matches[0]

    try:
        role_binding = ops.provider_binding(repo_root)
    except Exception as exc:  # noqa: BLE001 - provider authority unavailable
        raise inv.ProjectGatewayInventoryError(
            "provider_binding_unavailable",
            "the workflow provider binding could not be read",
            backend=backend,
        ) from exc
    gateway_provider_key = (
        PROVIDER_ROLE_COORDINATOR
        if authority.role == ROLE_COORDINATOR
        else PROVIDER_ROLE_PROJECT_GATEWAY
    )
    gateway_provider = _norm(role_binding.provider_for(gateway_provider_key))
    child_provider = _norm(role_binding.provider_for(PROVIDER_ROLE_COORDINATOR))
    required_providers = (
        (gateway_provider,)
        if selector == inv.SELECT_GATEWAY
        else (child_provider,)
        if selector == inv.SELECT_CHILD_ROUTE
        else (gateway_provider, child_provider)
    )
    if any(not item for item in required_providers):
        raise inv.ProjectGatewayInventoryError(
            "provider_binding_unresolved",
            "provider_binding resolves no provider for the requested project-gateway route",
            backend=backend,
        )
    return authority, gateway_provider, child_provider


def resolve_scope_route_provider(
    *,
    repo_root: str,
    project_scope: str,
    selector: str | None = None,
    ops: object | None = None,
) -> str:
    """The provider_binding-bound receiver provider for a scope's gateway route.

    Redmine #15414: the ``project-gateway`` CLI family derives its requested
    receiver from the SAME durable authority the Herdr inventory gate later
    re-verifies, instead of a hard-coded provider — a workspace whose
    coordinator/project-gateway role is bound to a non-codex provider (e.g.
    ``claude``, #13157 / #15255 j#104520) resolves its gateway instead of
    failing closed on a constant. Durable-config read only: no live inventory,
    no pane scan.

    - The Herdr inventory's fail-closed ``provider_binding_mismatch`` gate on
      the explicitly requested provider is unchanged; this helper is how a
      caller requests the provider that gate verifies.
    - Non-Herdr backends keep the historical ``codex`` default: the durable
      scope-binding machinery this resolution reads is a Herdr route contract,
      and the tmux route never had the binding gate.
    - Raises the same typed ``ProjectGatewayInventoryError`` refusals the
      inventory raises (``backend_config_unavailable`` /
      ``workflow_role_binding_unavailable`` / ``workflow_role_binding_invalid``
      / ``project_scope_binding_missing`` / ``project_scope_binding_ambiguous``
      / ``provider_binding_unavailable`` / ``provider_binding_unresolved``),
      so callers render them identically.
    """
    inv = _inventory()
    if selector is None:
        selector = inv.SELECT_GATEWAY
    scope = _norm(project_scope)
    if (
        selector not in inv.INVENTORY_SELECTORS
        or selector == inv.SELECT_NONE
        or not scope
    ):
        raise inv.ProjectGatewayInventoryError(
            "selector_gap",
            "route provider resolution requires a project scope and a routed selector",
            backend="",
        )
    use_ops = ops if ops is not None else inv.LiveProjectGatewayInventoryOps()
    root = Path(repo_root).expanduser().resolve()
    try:
        backend = use_ops.backend(root)
    except Exception as exc:  # noqa: BLE001 - config unreadable is typed refusal
        raise inv.ProjectGatewayInventoryError(
            "backend_config_unavailable",
            "the repo-local terminal backend configuration is unreadable",
        ) from exc
    if backend != inv.BACKEND_HERDR:
        return PROVIDER_CODEX
    parsed = parsed_scope_role_bindings(use_ops, root, backend=backend)
    _authority, gateway_provider, child_provider = scope_route_providers(
        use_ops, root, scope, parsed=parsed, backend=backend, selector=selector
    )
    return gateway_provider if selector == inv.SELECT_GATEWAY else child_provider


def _norm(value: object) -> str:
    return str(value or "").strip()


__all__ = (
    "GATEWAY_AUTHORITY_ROLES",
    "parsed_scope_role_bindings",
    "resolve_scope_route_provider",
    "scope_route_providers",
)
