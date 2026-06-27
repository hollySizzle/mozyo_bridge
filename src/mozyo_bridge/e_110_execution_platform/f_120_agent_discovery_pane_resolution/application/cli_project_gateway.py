"""CLI surface for the semantic project-gateway route (Redmine #12668).

Exposes the ``resolve_project_gateway`` / ``start_project_gateway`` /
``handoff_to_project_gateway`` swimlane functions from
``vibes/docs/logics/ticketless-project-gateway-runtime-ux.md`` as a concrete
command surface so the department-root -> project-gateway route is expressible
without an operator-copied ``%pane``:

- ``project-gateway resolve`` — read-only. Resolves the single project gateway
  target by ``--repo`` + ``--project`` + ``--role`` (+ optional ``--session``)
  and prints it, or a fail-closed ``gateway_missing`` / ``gateway_target_ambiguous``
  / ``selector_gap`` classification with the next safe action (the concrete
  ``start_project_gateway`` command for a missing gateway, the matching
  candidates for an ambiguous one).
- ``project-gateway handoff`` — resolves the gateway the same way, then delivers
  a ticketless consultation through the existing gated ``orchestrate_handoff``
  with the resolved pane injected as ``--target``. The operator never types a
  pane id; the Git ``--target-repo`` + project ``--target-project`` gates still
  re-verify the resolved pane (defense in depth).

Discovery + delivery primitives are reused from the existing modules
(``_agents_target_candidates`` / ``orchestrate_handoff``) so this never grows a
divergent identity model. Direct ``%pane`` addressing stays a debug escape hatch
on ``handoff send``; it is not this command's normal route.
"""

from __future__ import annotations

import argparse
import json as _json

from mozyo_bridge.application.commands import (
    _agents_target_candidates,
    orchestrate_handoff,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CODEX,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    STATUS_FOUND,
    ProjectGatewayRoute,
    resolve_project_gateway,
    start_project_gateway_command,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff import (
    configure_handoff_parser,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    require_tmux,
)
from mozyo_bridge.shared.errors import die


def _discover_candidates() -> list:
    """All classified target candidates across every session (no pre-filter).

    Discovery is intentionally unfiltered: the resolver applies the
    role / repo / project / session predicates itself so its near-miss reasons
    stay visible (a session pre-filter would hide cross-session gateways, which
    are the normal separate-window/session path). Patched in tests.
    """
    return _agents_target_candidates(argparse.Namespace(agent=None, session=None))


def _route_from_args(
    *, repo_root: str, project_scope: str, role: str, session: str | None
) -> ProjectGatewayRoute:
    return ProjectGatewayRoute(
        repo_root=repo_root,
        project_scope=project_scope,
        role=role,
        session=session,
    )


def cmd_project_gateway_resolve(args: argparse.Namespace) -> int:
    """Resolve (read-only) the project gateway target by semantic identity."""
    require_tmux()
    route = _route_from_args(
        repo_root=args.repo,
        project_scope=args.project,
        role=args.role,
        session=getattr(args, "session", None),
    )
    resolution = resolve_project_gateway(_discover_candidates(), route)

    if getattr(args, "as_json", False):
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
        # The normal, pane-id-free route to deliver to the resolved gateway.
        print(
            "next: handoff_to_project_gateway -> "
            f"mozyo-bridge project-gateway handoff --to {route.role} "
            f"--target-repo {route.repo_root} --target-project {route.project_scope} "
            "--source redmine --issue <id> --journal <id> --kind ticketless_consultation"
        )
        return 0

    if resolution.matched:
        print("matched (ambiguous — refuse to auto-select):")
        for cand in resolution.matched:
            print(f"  - pane_id={cand.pane_id} session={cand.session} window={cand.window_name}")
        print("resolve by adding --session <session-or-cockpit-group> to narrow to one.")
        return 1

    # gateway_missing / selector_gap: name the concrete start action + near misses.
    print("next: start_project_gateway ->")
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


def cmd_project_gateway_handoff(args: argparse.Namespace) -> int:
    """Resolve the gateway semantically, then deliver through the gated orchestrator.

    Replaces the manual ``--target %pane`` of ``handoff send`` with a fail-closed
    semantic resolution by ``--target-repo`` + ``--target-project`` + ``--to`` role.
    On a non-``found`` resolution it refuses to deliver and reports the fail-closed
    classification. On ``found`` it injects the resolved pane and hands off through
    :func:`orchestrate_handoff`, where the Git repo + project-scope gates re-verify
    the pane before delivery.
    """
    require_tmux()

    if not args.target_repo or args.target_repo == "auto":
        die(
            "`project-gateway handoff` resolves the pane semantically, so it needs "
            "a concrete `--target-repo <git-root>` (not `auto`, which requires an "
            "explicit %pane). Pass the workspace Git root."
        )
    if not args.target_project:
        die(
            "`project-gateway handoff` requires `--target-project <project_scope>` "
            "to resolve the project gateway. To gate on the Git repo root only, use "
            "`handoff send` with an explicit `--target`."
        )
    if getattr(args, "target", None):
        die(
            "`project-gateway handoff` selects the pane by semantic identity; do "
            "not pass `--target %pane`. Use `handoff send` for explicit-pane "
            "delivery (the debug escape hatch)."
        )

    route = _route_from_args(
        repo_root=args.target_repo,
        project_scope=args.target_project,
        role=args.to,
        session=getattr(args, "gateway_session", None),
    )
    resolution = resolve_project_gateway(_discover_candidates(), route)

    if resolution.status != STATUS_FOUND or resolution.selected is None:
        # Fail closed; do not deliver. Reuse the read-only renderer for the
        # operator-facing classification + next action.
        resolve_args = argparse.Namespace(
            repo=route.repo_root,
            project=route.project_scope,
            role=route.role,
            session=route.session,
            as_json=getattr(args, "as_json", False),
        )
        return cmd_project_gateway_resolve(resolve_args)

    # Inject the resolved pane and delegate to the gated handoff orchestrator. The
    # repo + project gates in orchestrate_handoff re-verify the resolved pane.
    args.target = resolution.selected.pane_id
    return orchestrate_handoff(args)


def register(sub) -> None:
    """Register the ``project-gateway`` subcommand tree onto ``sub``."""
    gateway = sub.add_parser(
        "project-gateway",
        help=(
            "Semantic department-root -> project-gateway route (Redmine #12668). "
            "Discover / start / handoff a project-scoped gateway unit across "
            "separate window/session surfaces by identity (role + repo_root + "
            "project_scope + optional session/cockpit group), fail-closed on "
            "missing / ambiguous, without copying a volatile %%pane. See "
            "vibes/docs/logics/ticketless-project-gateway-runtime-ux.md."
        ),
    )
    gateway_sub = gateway.add_subparsers(dest="project_gateway_command", required=True)

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
    resolve.add_argument(
        "--role",
        default=AGENT_KIND_CODEX,
        choices=["codex", "claude"],
        help="Project gateway role (default codex).",
    )
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

    handoff = gateway_sub.add_parser(
        "handoff",
        help=(
            "Resolve the project gateway by semantic identity (no %%pane copy) and "
            "deliver a ticketless consultation through the gated handoff "
            "orchestrator. Requires --target-repo + --target-project; the role is "
            "--to. Fails closed (no delivery) on missing / ambiguous resolution."
        ),
    )
    # Reuse the full handoff argument set; the route's repo/project/role come from
    # --target-repo / --target-project / --to, and --target is resolved, not typed.
    configure_handoff_parser(
        handoff,
        kind_required=True,
        target_required=False,
        target_repo_required=True,
    )
    handoff.add_argument(
        "--gateway-session",
        dest="gateway_session",
        default=None,
        help=(
            "Optional session or cockpit group to narrow the gateway resolution to "
            "one candidate. Omit to resolve across separate windows/sessions."
        ),
    )
    handoff.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="On a fail-closed resolution, emit the GatewayResolution payload as JSON.",
    )
    handoff.set_defaults(func=cmd_project_gateway_handoff)
