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
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (
    ACTION_ADOPT,
    ACTION_BLOCKED,
    ACTION_LAUNCH,
    GatewayLaneIdentity,
    gateway_lane_identity_from_scope,
    resolve_launch_or_adopt,
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
    """Resolve (read-only) the project gateway target by semantic identity.

    The project gateway role is fixed to ``codex`` (the design doc's
    ``role="codex"`` route): a project gateway is a Codex coordinator unit, and
    the implementation worker (Claude) is reached only after the gateway decides
    implementation is needed. So this command never resolves a Claude target
    (Redmine #12668 review j#66626 blocker 2).
    """
    require_tmux()
    route = _route_from_args(
        repo_root=args.repo,
        project_scope=args.project,
        role=AGENT_KIND_CODEX,
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


def _gateway_identity(repo_root: str, project_scope: str) -> GatewayLaneIdentity:
    """Build the gateway lane identity for ``project_scope`` under ``repo_root``.

    Prefers the project's adopted metadata (#12658 ``adopted_scopes_for_repo``) so
    the launch action carries the real project path / label / parent workspace.
    Falls back to a metadata-thin identity from the flags when the project is not
    discoverable / not adopted (e.g. ``runtime_identity.enabled`` is off): the
    launch-or-adopt resolution still runs and fails closed honestly rather than
    pretending the scope is adopted.
    """
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
        adopted_scopes_for_repo,
    )

    for scope in adopted_scopes_for_repo(repo_root):
        if scope.scope == project_scope:
            return gateway_lane_identity_from_scope(scope, repo_root=repo_root)
    # Not adopted: derive a thin identity directly from the route inputs. The
    # project path is unknown, so the launch command names the project workdir
    # generically; this never invents an adoption that the metadata does not show.
    return GatewayLaneIdentity(
        project_scope=project_scope,
        project_label=project_scope,
        project_path="",
        repo_root=repo_root,
    )


def cmd_project_gateway_adopt(args: argparse.Namespace) -> int:
    """Resolve the launch-or-adopt decision for a project gateway lane (#12708).

    The grandparent (department root) -> parent (project gateway) transition entry
    point: classify a request onto ``--project``, then decide — purely by semantic
    identity, never a copied ``%pane`` — whether to *adopt* a live gateway lane,
    *launch* one (none exists), or fail *blocked* (ambiguous / under-specified).
    Read-only: it prints the decision and the concrete next action; the actual
    launch is the named ``start_project_gateway`` command (cockpit), and delivery
    to an adopted gateway stays ``project-gateway handoff``.
    """
    require_tmux()
    identity = _gateway_identity(args.repo, args.project)
    decision = resolve_launch_or_adopt(
        _discover_candidates(),
        identity,
        session=getattr(args, "session", None),
    )

    if getattr(args, "as_json", False):
        print(_json.dumps(decision.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if decision.ok else 1

    print(f"action: {decision.action}")
    print(
        "identity: "
        f"target_kind={identity.target_kind} role={identity.role} "
        f"lane_kind={identity.lane_kind} launch_policy={identity.launch_policy} "
        f"callback_to={identity.callback_to}"
    )
    print(
        "route: "
        f"repo_root={identity.repo_root} project_scope={identity.project_scope} "
        f"workspace={identity.workspace or '<unknown>'}"
    )
    if decision.detail:
        print(f"detail: {decision.detail}")

    if decision.action == ACTION_ADOPT and decision.adopted is not None:
        sel = decision.adopted
        print(
            "adopt: "
            f"pane_id={sel.pane_id} session={sel.session} window={sel.window_name} "
            f"repo={sel.repo_short} project_scope={sel.project_scope}"
        )
        # The normal, pane-id-free route to deliver to the adopted gateway.
        print(
            "next: handoff_to_project_gateway -> "
            f"mozyo-bridge project-gateway handoff --to {identity.role} "
            f"--target-repo {identity.repo_root} --target-project {identity.project_scope} "
            "--source redmine --issue <id> --journal <id> --kind ticketless_consultation"
        )
        return 0

    if decision.action == ACTION_LAUNCH:
        print("next: start_project_gateway ->")
        print(f"  {decision.launch_command}")
        return 0

    # ACTION_BLOCKED: fail closed; name the matched / near-miss candidates so the
    # operator can disambiguate or complete the route.
    resolution = decision.resolution
    if resolution.matched:
        print("matched (ambiguous — refuse to adopt or launch):")
        for cand in resolution.matched:
            print(f"  - pane_id={cand.pane_id} session={cand.session} window={cand.window_name}")
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

    # The project gateway role is codex (design doc `role="codex"` route). This
    # command must NOT direct-send to the project Claude worker: the root ->
    # project gateway -> implementation worker boundary requires the gateway
    # (Codex) to decide implementation need and create the Redmine anchor first.
    # Reject `--to claude` so the Redmine-anchor boundary cannot be bypassed
    # (Redmine #12668 review j#66626 blocker 2).
    if args.to != AGENT_KIND_CODEX:
        die(
            "`project-gateway handoff` delivers to the project gateway, which is a "
            f"Codex unit; `--to {args.to}` is not allowed. The implementation "
            "worker (Claude) is reached only after the gateway creates a Redmine "
            "anchor — use `--to codex`. Direct project-Claude send is forbidden by "
            "the ticketless project gateway contract."
        )

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
    # No --role: the project gateway role is fixed to codex (design doc route).
    # Resolving a Claude target is off-contract and removed (review j#66626).
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

    adopt = gateway_sub.add_parser(
        "adopt",
        help=(
            "Read-only: decide launch-or-adopt for the project gateway lane "
            "(Redmine #12708). Resolves the live gateway by semantic identity and "
            "returns adopt (reuse the live lane) / launch (start one in the "
            "project workdir) / blocked (ambiguous or under-specified). The "
            "grandparent -> parent project-gateway transition entry; never selects "
            "by active pane."
        ),
    )
    adopt.add_argument(
        "--repo",
        required=True,
        help="Workspace Git worktree root (repo_root authority).",
    )
    adopt.add_argument(
        "--project",
        required=True,
        help="Adopted project scope id (redmine_project) to launch-or-adopt the gateway for.",
    )
    adopt.add_argument(
        "--session",
        default=None,
        help=(
            "Optional session or cockpit group to narrow candidates. Omit to "
            "resolve across separate windows/sessions (the normal path)."
        ),
    )
    adopt.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the structured LaunchOrAdoptDecision payload as JSON.",
    )
    adopt.set_defaults(func=cmd_project_gateway_adopt)

    handoff = gateway_sub.add_parser(
        "handoff",
        help=(
            "Resolve the project gateway by semantic identity (no %%pane copy) and "
            "deliver a ticketless consultation through the gated handoff "
            "orchestrator. Requires --target-repo + --target-project and --to codex "
            "(the gateway is a Codex unit; --to claude is rejected so the project "
            "Claude worker is never direct-sent). Fails closed (no delivery) on "
            "missing / ambiguous resolution."
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
