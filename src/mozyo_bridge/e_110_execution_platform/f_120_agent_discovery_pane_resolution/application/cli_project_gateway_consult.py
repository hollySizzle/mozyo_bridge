"""CLI surface for ``project-gateway consult`` (Redmine #12740 forward no-anchor consultation).

Split out of :mod:`...application.cli_project_gateway` (Redmine #12751
modularization) so the forward consultation leg is its own bounded module with an
independent test target — the same extraction pattern used for
``cli_project_gateway_child_intake`` (its one-step-down sibling). This module owns
the grandparent (department-root) -> parent (project-gateway) forward consultation
leg: the handler (:func:`cmd_project_gateway_consult`) and its parser registration
(:func:`register_consult`). The sibling ``cli_project_gateway`` registrar calls
:func:`register_consult` so the whole ``project-gateway`` subcommand tree is still
assembled in one place.

The read-only resolution core (candidate discovery, route construction, and the
fail-closed renderer) is shared from :mod:`...application.cli_project_gateway_resolve`
so consult and the read-only ``resolve`` command never diverge on identity or the
operator-facing classification. A local :func:`_discover_candidates` keeps consult's
own resolution patchable in isolation (mirroring ``cli_project_gateway_child_intake``),
and the fail-closed render reuses the *pure* :func:`render_gateway_resolution` over the
already-computed resolution, so no second discovery scan runs on the no-deliver path.
"""

from __future__ import annotations

import argparse

from mozyo_bridge.application.commands import (
    _agents_target_candidates,
    orchestrate_handoff,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CODEX,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    STATUS_FOUND,
    resolve_project_gateway,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.cli_project_gateway_resolve import (
    _route_from_args,
    render_gateway_resolution,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff_ticketless import (
    _add_ticketless_delivery_options,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
    CALLBACK_METHODS,
    CONSULTATION_PROJECT_DOMAIN,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    require_tmux,
)
from mozyo_bridge.shared.errors import die


def _discover_candidates() -> list:
    """All classified target candidates across every session (no pre-filter).

    Mirrors ``cli_project_gateway_resolve._discover_candidates`` (kept local to
    avoid an import cycle with the registrar and to keep consult's own resolution
    patchable in isolation): discovery is intentionally unfiltered so the resolver
    applies the role / repo / project / session predicates itself and its
    near-miss reasons stay visible. Patched in tests.
    """
    return _agents_target_candidates(argparse.Namespace(agent=None, session=None))


def cmd_project_gateway_consult(args: argparse.Namespace) -> int:
    """Forward a no-anchor ticketless consultation to the project gateway (#12740).

    The forward (department-root -> project-gateway) counterpart of the return
    ``handoff ticketless-callback`` rail. It resolves the single project gateway by
    semantic identity (the same fail-closed ``--target-repo`` + ``--target-project``
    + ``--to codex`` resolution as ``project-gateway handoff``), then delivers the
    consultation through the gated :func:`orchestrate_handoff` **without a Redmine
    anchor and without fabricating one** — closing the GK3500 rerun blocker where
    the root coordinator had found exactly one gateway but the anchored
    ``handoff send --source redmine`` failed closed with ``invalid_anchor`` and raw
    pane typing was correctly refused.

    The Redmine-anchor gate for worker dispatch / implementation / domain probe is
    NOT relaxed: this rail forwards a *consultation* only (no anchor, no dispatch
    token), and the structured payload restates the invariant so the receiver
    gateway mints a real Redmine anchor before dispatching a worker. The receiver
    gets the transition role/action boundary (#12706) and workflow-contract refs
    (#12700) auto-injected on ``found``, plus the forward consultation's callback
    return contract (which role to return to, via which product primitives) so it
    can return a structured result via ``ticketless-callback`` / ``q-enter
    consultation_callback`` (#12703 / #12705 / #12737). Fails closed (no delivery,
    no payload injected) when no unique project gateway exists.
    """
    require_tmux()

    # Same boundary as `project-gateway handoff`: the gateway is a Codex unit. The
    # implementation worker (Claude) is reached only after the gateway mints a
    # Redmine anchor, so a direct project-Claude consultation send is forbidden.
    if args.to != AGENT_KIND_CODEX:
        die(
            "`project-gateway consult` delivers to the project gateway, which is a "
            f"Codex unit; `--to {args.to}` is not allowed. The implementation "
            "worker (Claude) is reached only after the gateway creates a Redmine "
            "anchor — use `--to codex`. Direct project-Claude send is forbidden by "
            "the ticketless project gateway contract."
        )

    if not args.target_repo or args.target_repo == "auto":
        die(
            "`project-gateway consult` resolves the pane semantically, so it needs "
            "a concrete `--target-repo <git-root>` (not `auto`, which requires an "
            "explicit %pane). Pass the workspace Git root."
        )
    if not args.target_project:
        die(
            "`project-gateway consult` requires `--target-project <project_scope>` "
            "to resolve the project gateway. To gate on the Git repo root only, use "
            "`handoff ticketless-callback` with an explicit `--target`."
        )
    if getattr(args, "target", None):
        die(
            "`project-gateway consult` selects the pane by semantic identity; do "
            "not pass `--target %pane`. The forward consultation never carries a "
            "pane id to the ticketless receiver."
        )

    route = _route_from_args(
        repo_root=args.target_repo,
        project_scope=args.target_project,
        role=args.to,
        session=getattr(args, "gateway_session", None),
    )
    resolution = resolve_project_gateway(_discover_candidates(), route)

    if resolution.status != STATUS_FOUND or resolution.selected is None:
        # Fail closed; do not deliver and inject no forward-consultation payload.
        # Reuse the shared pure renderer over the already-computed resolution for
        # the operator-facing classification (no second discovery scan).
        return render_gateway_resolution(
            resolution, route, as_json=getattr(args, "as_json", False)
        )

    # Inject the resolved pane and the forward-consultation payload, then delegate
    # to the gated no-anchor orchestrator. The repo + project gates in
    # orchestrate_handoff re-verify the resolved pane before any send.
    args.target = resolution.selected.pane_id
    # Redmine #12706 / #12700: this command IS the grandparent (department-root) ->
    # project-gateway transition, so auto-inject the grandparent_coordinator
    # transition boundary and workflow-contract bundle on `found` (the operator
    # never types the role payload), exactly like `project-gateway handoff`.
    args.transition_role = ROLE_GRANDPARENT_COORDINATOR
    args.workflow_contract = ROLE_GRANDPARENT_COORDINATOR
    # Redmine #12740: the forward consultation payload, built programmatically (not
    # operator-typed) so it is product evidence, not a hand-asserted role payload.
    # The root forwards a project-domain consultation, asks the gateway to return
    # the result to the grandparent_coordinator lane via either no-anchor return
    # primitive, and names the project_gateway role contract the gateway acts under.
    args.consultation_kind = CONSULTATION_PROJECT_DOMAIN
    args.callback_to_role = ROLE_GRANDPARENT_COORDINATOR
    args.callback_methods = list(CALLBACK_METHODS)
    args.read_contract = ROLE_PROJECT_GATEWAY
    return orchestrate_handoff(
        args,
        default_kind="design_consultation",
        ticketless=True,
        ticketless_consultation=True,
    )


def register_consult(gateway_sub) -> None:
    """Register the ``project-gateway consult`` subcommand onto ``gateway_sub``."""
    consult = gateway_sub.add_parser(
        "consult",
        help=(
            "Forward a no-anchor ticketless consultation to the project gateway "
            "(Redmine #12740). Resolves the gateway by semantic identity (no %%pane "
            "copy), then delivers WITHOUT a Redmine anchor and without fabricating "
            "one — the forward counterpart of `handoff ticketless-callback`. "
            "Requires --target-repo + --target-project and --to codex. The "
            "worker-dispatch / implementation / domain-probe Redmine-anchor gate is "
            "NOT relaxed (this rail forwards a consultation only). Fails closed (no "
            "delivery) on missing / ambiguous resolution. Use the anchored "
            "`project-gateway handoff` once a Redmine anchor exists for worker work."
        ),
    )
    # Reuse the ticketless delivery knobs (no --source / --issue / --journal anchor
    # flags, no --kind): the route's repo/project/role come from
    # --target-repo / --target-project / --to, --target is resolved (not typed),
    # and the forward consultation payload is injected programmatically on `found`.
    _add_ticketless_delivery_options(consult)
    consult.add_argument(
        "--gateway-session",
        dest="gateway_session",
        default=None,
        help=(
            "Optional session or cockpit group to narrow the gateway resolution to "
            "one candidate. Omit to resolve across separate windows/sessions."
        ),
    )
    consult.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="On a fail-closed resolution, emit the GatewayResolution payload as JSON.",
    )
    consult.set_defaults(func=cmd_project_gateway_consult)
