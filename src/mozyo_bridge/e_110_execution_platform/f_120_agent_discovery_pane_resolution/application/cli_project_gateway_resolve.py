"""CLI surface + shared core for ``project-gateway resolve`` (Redmine #12668 / #12751).

Split out of :mod:`...application.cli_project_gateway` so the read-only semantic
resolution is its own bounded module (Redmine #12751 modularization), the same
extraction pattern used for ``cli_project_gateway_child_intake``. This module owns
the read-only resolution *core* that the rest of the ``project-gateway`` family
reuses:

- :func:`_discover_candidates` / :func:`_route_from_args` — the unfiltered
  candidate discovery + route-construction helpers (the resolver applies the
  role / repo / project / session predicates itself, so discovery is intentionally
  unfiltered and its near-miss reasons stay visible). Patched in tests.
- :func:`render_gateway_resolution` — the pure operator-facing renderer for a
  *computed* :class:`GatewayResolution` (text or JSON, with the fail-closed next
  action). Taking the already-computed resolution keeps the delivering commands
  (``handoff`` / ``consult``) from re-discovering on their fail-closed path.
- :func:`cmd_project_gateway_resolve` — the read-only ``resolve`` handler.
- :func:`register_resolve` — the ``resolve`` parser registration.

The sibling ``cli_project_gateway`` registrar imports these so the read-only route
and the delivering commands share one identity model + one renderer, and calls
:func:`register_resolve` so the whole ``project-gateway`` subcommand tree is still
assembled in one place.
"""

from __future__ import annotations

import argparse
import json as _json

from mozyo_bridge.application.commands import (
    _agents_target_candidates,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CODEX,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    ProjectGatewayRoute,
    resolve_project_gateway,
    start_project_gateway_command,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.project_gateway_backend_inventory import (
    ProjectGatewayInventoryError,
    SELECT_GATEWAY,
    discover_project_gateway_inventory,
    render_inventory_error,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.project_gateway_route_binding import (
    resolve_scope_route_provider,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    require_tmux,
)


def _discover_candidates(
    *,
    repo_root: str | None = None,
    project_scope: str | None = None,
    provider: str = AGENT_KIND_CODEX,
    session: str | None = None,
    selector: str = SELECT_GATEWAY,
) -> list:
    """All classified target candidates across every session (no pre-filter).

    Discovery is intentionally unfiltered: the resolver applies the
    role / repo / project / session predicates itself so its near-miss reasons
    stay visible (a session pre-filter would hide cross-session gateways, which
    are the normal separate-window/session path). Patched in tests.
    """
    if repo_root is None or project_scope is None:
        # Compatibility seam for direct callers/tests predating backend-aware
        # discovery. Command handlers always supply the semantic request below.
        return _agents_target_candidates(argparse.Namespace(agent=None, session=None))
    return discover_project_gateway_inventory(
        repo_root=repo_root,
        project_scope=project_scope,
        provider=provider,
        session=session,
        selector=selector,
    )


def _route_from_args(
    *, repo_root: str, project_scope: str, role: str, session: str | None
) -> ProjectGatewayRoute:
    return ProjectGatewayRoute(
        repo_root=repo_root,
        project_scope=project_scope,
        role=role,
        session=session,
    )


def render_gateway_resolution(resolution, route, *, as_json: bool) -> int:
    """Render a *computed* gateway resolution for the operator (text or JSON).

    Pure with respect to discovery: it takes the already-resolved
    ``GatewayResolution`` and ``ProjectGatewayRoute`` and never re-discovers, so
    the read-only ``resolve`` handler and the delivering commands (``handoff`` /
    ``consult``) share one renderer without a second candidate scan on the
    fail-closed path. Returns the resolve-command exit code (0 on ``found``, 1 on
    any fail-closed classification).
    """
    if as_json:
        print(_json.dumps(resolution.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if resolution.ok else 1

    print(f"status: {resolution.status}")
    print(
        "route: "
        f"role={route.role} repo_root={route.repo_root} "
        f"project_scope={route.project_scope} "
        f"session={route.session or '<any>'} target_kind={route.target_kind}"
    )
    if resolution.detail:
        print(f"detail: {resolution.detail}")

    if resolution.ok and resolution.selected is not None:
        sel = resolution.selected
        print(
            "gateway: "
            f"pane_id={sel.pane_id} session={sel.session} "
            f"window={sel.window_name} repo={sel.repo_short} "
            f"project_scope={sel.project_scope}"
        )
        # The normal, pane-id-free routes to deliver to the resolved gateway.
        # Redmine #12740: the no-anchor consultation phase uses `project-gateway
        # consult` (forward ticketless rail, no Redmine anchor); anchored worker
        # work uses `project-gateway handoff` once a real Redmine anchor exists.
        print(
            "next (no-anchor consultation): consult_project_gateway -> "
            f"mozyo-bridge project-gateway consult --to {route.role} "
            f"--target-repo {route.repo_root} --target-project {route.project_scope}"
        )
        print(
            "next (anchored worker work): handoff_to_project_gateway -> "
            f"mozyo-bridge project-gateway handoff --to {route.role} "
            f"--target-repo {route.repo_root} --target-project {route.project_scope} "
            "--source redmine --issue <id> --journal <id> --kind implementation_request"
        )
        return 0

    if resolution.matched:
        print("matched (ambiguous — refuse to auto-select):")
        for cand in resolution.matched:
            print(f"  - pane_id={cand.pane_id} session={cand.session} window={cand.window_name}")
        print("resolve by adding --session <session-or-cockpit-group> to narrow to one.")
        return 1

    # gateway_missing / selector_gap: name the concrete start action + near misses.
    # Redmine #12699: the start action is a cockpit-visible Unit, not a detached
    # --no-attach normal session and not a cockpit --json preview.
    print("next: start_project_gateway (cockpit-visible Unit) ->")
    print(f"  {start_project_gateway_command(route)}")
    if resolution.near_misses:
        print("near misses (why each pane was not the gateway):")
        for near in resolution.near_misses:
            cand = near.candidate
            print(
                f"  - pane_id={cand.pane_id} role={cand.role} "
                f"repo={cand.repo_short} project_scope={cand.project_scope or '<none>'} "
                f"reason={near.reason}"
            )
    return 1


def _route_provider(
    *, repo_root: str, project_scope: str, selector: str = SELECT_GATEWAY
) -> str:
    """The provider_binding-bound receiver for this scope's route. Patched in tests.

    Redmine #15414: derived from the same durable authority the Herdr inventory
    gate re-verifies (``resolve_scope_route_provider``), never a hard-coded
    provider constant.
    """
    return resolve_scope_route_provider(
        repo_root=repo_root, project_scope=project_scope, selector=selector
    )


def cmd_project_gateway_resolve(args: argparse.Namespace) -> int:
    """Resolve (read-only) the project gateway target by semantic identity.

    The resolved receiver is the GATEWAY ROLE's bound provider — historically the
    hard-coded ``role="codex"`` route (Redmine #12668 review j#66626 blocker 2),
    now the workspace's ``provider_binding`` resolution for the scope's
    coordinator/project-gateway role (Redmine #15414 — a coordinator bound to
    ``claude`` resolves its gateway instead of failing closed on the constant).
    The role-canonical invariant is unchanged: this command resolves the gateway
    route only, never the implementation-worker route; the worker is reached only
    after the gateway decides implementation is needed and a Redmine anchor
    exists.
    """
    try:
        provider = _route_provider(
            repo_root=args.repo, project_scope=args.project
        )
    except ProjectGatewayInventoryError as exc:
        return render_inventory_error(
            exc, as_json=getattr(args, "as_json", False)
        )
    route = _route_from_args(
        repo_root=args.repo,
        project_scope=args.project,
        role=provider,
        session=getattr(args, "session", None),
    )
    try:
        inventory = _discover_candidates(
            repo_root=args.repo,
            project_scope=args.project,
            provider=provider,
            session=getattr(args, "session", None),
        )
    except ProjectGatewayInventoryError as exc:
        return render_inventory_error(
            exc, as_json=getattr(args, "as_json", False)
        )
    resolution = resolve_project_gateway(inventory, route)
    return render_gateway_resolution(
        resolution, route, as_json=getattr(args, "as_json", False)
    )


def register_resolve(gateway_sub) -> None:
    """Register the ``project-gateway resolve`` subcommand onto ``gateway_sub``."""
    resolve = gateway_sub.add_parser(
        "resolve",
        help=(
            "Read-only: resolve the single project gateway target by semantic "
            "identity, or return a fail-closed gateway_missing / "
            "gateway_target_ambiguous / selector_gap with the next safe action. "
            "Never selects by active pane."
        ),
    )
    resolve.add_argument(
        "--repo",
        required=True,
        help="Workspace Git worktree root (repo_root authority).",
    )
    resolve.add_argument(
        "--project",
        required=True,
        help="Adopted project scope id (redmine_project) to resolve the gateway for.",
    )
    # No --role: the receiver is always the GATEWAY role, whose provider comes
    # from the workspace's provider_binding (Redmine #15414; historically the
    # fixed codex route). Resolving the implementation-worker route is
    # off-contract and removed (review j#66626).
    resolve.add_argument(
        "--session",
        default=None,
        help=(
            "Optional session or cockpit group to narrow candidates. Omit to "
            "resolve across separate windows/sessions (the normal path)."
        ),
    )
    resolve.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the structured GatewayResolution payload as JSON.",
    )
    resolve.set_defaults(func=cmd_project_gateway_resolve)
