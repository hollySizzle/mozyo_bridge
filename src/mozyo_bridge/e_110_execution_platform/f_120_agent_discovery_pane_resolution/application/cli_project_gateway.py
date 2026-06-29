"""CLI surface for the semantic project-gateway route (Redmine #12668).

Exposes the ``resolve_project_gateway`` / ``start_project_gateway`` /
``handoff_to_project_gateway`` swimlane functions from
``vibes/docs/logics/ticketless-project-gateway-runtime-ux.md`` as a concrete
command surface so the department-root -> project-gateway route is expressible
without an operator-copied ``%pane``:

- ``project-gateway resolve`` — read-only. Resolves the single project gateway
  target by ``--repo`` + ``--project`` + ``--role`` (+ optional ``--session``)
  and prints it, or a fail-closed ``gateway_missing`` / ``gateway_target_ambiguous``
  / ``selector_gap`` classification with the next safe action.
- ``project-gateway handoff`` — resolves the gateway the same way, then delivers
  a ticketless consultation through the existing gated ``orchestrate_handoff``
  with the resolved pane injected as ``--target``. The operator never types a
  pane id; the Git ``--target-repo`` + project ``--target-project`` gates still
  re-verify the resolved pane (defense in depth).

This module is the ``project-gateway`` **registrar**: it owns the ``adopt`` /
``route-plan`` / ``handoff`` handlers and assembles the whole subcommand tree.
The other subcommand families live in bounded sibling modules so each
subcommand's change impact is localized (Redmine #12751):

- ``resolve`` (and the shared read-only resolution core — candidate discovery,
  route construction, and the fail-closed renderer) lives in
  :mod:`...application.cli_project_gateway_resolve`;
- ``consult`` (forward no-anchor consultation, #12740) lives in
  :mod:`...application.cli_project_gateway_consult`;
- ``child-intake`` (forward no-anchor work-intake, #12748) lives in
  :mod:`...application.cli_project_gateway_child_intake`.

Discovery + delivery primitives are reused from the existing modules
(``_agents_target_candidates`` / ``orchestrate_handoff``) so this never grows a
divergent identity model. Direct ``%pane`` addressing stays a debug escape hatch
on ``handoff send``; it is not this command's normal route.
"""

from __future__ import annotations

import argparse
import json as _json

from mozyo_bridge.application.commands import (
    orchestrate_handoff,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CODEX,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway import (
    STATUS_FOUND,
    resolve_project_gateway,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (
    ACTION_ADOPT,
    ACTION_LAUNCH,
    GatewayLaneIdentity,
    gateway_lane_identity_from_scope,
    resolve_launch_or_adopt,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    RELATIVE_CALLER_ROLES,
    classify_startup_evidence,
    cockpit_visible_from_candidate,
    resolve_relative_route,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.cli_project_gateway_resolve import (
    _discover_candidates,
    _route_from_args,
    register_resolve,
    render_gateway_resolution,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.cli_project_gateway_consult import (
    register_consult,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application.cli_project_gateway_child_intake import (
    register_child_intake,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff import (
    configure_handoff_parser,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    require_tmux,
)
from mozyo_bridge.shared.errors import die


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


def _startup_evidence_for(decision):
    """Classify the startup evidence for a launch-or-adopt decision (#12699).

    An ``adopt`` is cockpit-visible green-path evidence only when the resolved lane
    is a cockpit pane (not a detached normal window); a ``launch`` / ``blocked`` has
    no live Unit yet, so it classifies to ``none`` until a cockpit-visible Unit is
    started. A detached ``--no-attach`` normal session is never green-path.
    """
    if decision.action == ACTION_ADOPT and decision.adopted is not None:
        return classify_startup_evidence(
            cockpit_visible=cockpit_visible_from_candidate(decision.adopted),
            session_present=True,
        )
    return classify_startup_evidence()


def _adopt_exit_code(decision, evidence) -> int:
    """Unified adopt exit code shared by JSON and text mode (#12699 review rev2).

    An ``adopt`` is success (rc 0) only when the resolved lane is cockpit-visible
    green-path evidence; adopting a detached normal-window lane is rc 1 (it is not
    the route). ``launch`` is rc 0 (forward), ``blocked`` is rc 1. JSON and text
    must not disagree on this.
    """
    if decision.action == ACTION_ADOPT:
        return 0 if evidence.is_green_path else 1
    return 0 if decision.ok else 1


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

    evidence = _startup_evidence_for(decision)

    if getattr(args, "as_json", False):
        payload = decision.as_payload()
        payload["startup_evidence"] = evidence.as_payload()
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return _adopt_exit_code(decision, evidence)

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
        # Redmine #12699: a live lane is only a green-path route once it is a
        # cockpit-visible Unit. A detached normal-window lane is real but is NOT
        # green-path route evidence; surface that distinction explicitly.
        print(
            f"startup_evidence: {evidence.mode} (green_path={evidence.is_green_path})"
        )
        if not evidence.is_green_path:
            print(f"  warning: {evidence.detail}")
            print(
                "  start a cockpit-visible Unit from the project workdir: "
                "cd <project workdir> && mozyo-bridge cockpit (NOT a detached "
                "--no-attach normal session, NOT a cockpit --json preview)"
            )
        # The normal, pane-id-free routes to deliver to the adopted gateway.
        # Redmine #12740: the no-anchor consultation phase uses `project-gateway
        # consult` (forward ticketless rail, no Redmine anchor); anchored worker
        # work uses `project-gateway handoff` once a real Redmine anchor exists.
        print(
            "next (no-anchor consultation): consult_project_gateway -> "
            f"mozyo-bridge project-gateway consult --to {identity.role} "
            f"--target-repo {identity.repo_root} --target-project {identity.project_scope}"
        )
        print(
            "next (anchored worker work): handoff_to_project_gateway -> "
            f"mozyo-bridge project-gateway handoff --to {identity.role} "
            f"--target-repo {identity.repo_root} --target-project {identity.project_scope} "
            "--source redmine --issue <id> --journal <id> --kind implementation_request"
        )
        return _adopt_exit_code(decision, evidence)

    if decision.action == ACTION_LAUNCH:
        # Name the cockpit-visible startup explicitly; the launch command itself
        # warns against the detached / preview anti-patterns (#12699).
        print("next: start_project_gateway (cockpit-visible Unit) ->")
        print(f"  {decision.launch_command}")
        print(
            "  note: a detached --no-attach normal session / cockpit --json preview "
            "is NOT green-path route evidence; verify the Unit is in mozyo-cockpit."
        )
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
        # Fail closed; do not deliver. Reuse the shared pure renderer over the
        # already-computed resolution for the operator-facing classification +
        # next action (no second discovery scan).
        return render_gateway_resolution(
            resolution, route, as_json=getattr(args, "as_json", False)
        )

    # Inject the resolved pane and delegate to the gated handoff orchestrator. The
    # repo + project gates in orchestrate_handoff re-verify the resolved pane.
    args.target = resolution.selected.pane_id
    # Redmine #12706: this command IS the grandparent (department-root) ->
    # project-gateway transition, so auto-inject the grandparent_coordinator
    # boundary onto the standard transition payload only now — after a successful
    # `found` resolution (a fail-closed resolution above returns without
    # delivering, so no boundary is injected on the no-deliver path). The receiver
    # gateway reads `current_role=grandparent_coordinator` /
    # `forbidden_actions=[project_domain_decision, parent_gateway_no_dispatch_decision, ...]`
    # and so owns the project-domain / no_dispatch decision the grandparent must
    # not pre-empt (the #12698 defect). The operator never types the role payload;
    # the standard payload carries it.
    args.transition_role = ROLE_GRANDPARENT_COORDINATOR
    # Redmine #12700: the same grandparent -> project-gateway transition must also
    # carry the workflow-contract reference bundle so the receiver gateway knows the
    # required workflow contract docs (ticketless gateway UX, delegated-coordinator
    # acceptance / smoke frame, sublane development flow) as a normal-operation
    # contract — with receiver-resolvable path forms — instead of discovering them
    # by luck (#12698) or failing to resolve sender-repo-relative paths in a GK3500
    # monorepo workspace (#12700 j#66929). Auto-injected programmatically only on a
    # successful `found` resolution; the operator never types it.
    args.workflow_contract = ROLE_GRANDPARENT_COORDINATOR
    return orchestrate_handoff(args)


def cmd_project_gateway_route_plan(args: argparse.Namespace) -> int:
    """Resolve the current-Unit relative delegation route one step down (#12699).

    The current Unit is the *relative anchor*: ``--from-role`` names this Unit's
    lane role, and the route resolves the one-step-down target (grandparent ->
    ``project_gateway``, parent -> ``delegated_coordinator``, child ->
    ``implementation_worker``) semantically — never a copied ``%pane`` or an
    absolute root. For the coordinator-class targets it reuses the launch-or-adopt
    decision and classifies whether the resolved lane is cockpit-visible green-path
    evidence; for the implementation worker it returns the anchor-gated dispatch
    contract (a worker is never launched as a cockpit gateway).
    """
    require_tmux()
    plan = resolve_relative_route(
        _discover_candidates(),
        caller_role=args.from_role,
        repo_root=args.repo,
        project_scope=args.project,
        session=getattr(args, "session", None),
    )

    if getattr(args, "as_json", False):
        print(_json.dumps(plan.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if plan.ok else 1

    step = plan.step
    print(
        "relative_route: "
        f"{step.caller_position}({step.caller_role}) -> "
        f"{step.target_position}({step.target_binding}) role={step.target_role}"
    )
    print(f"anchor_required: {plan.anchor_required}")
    print(
        f"startup_evidence: {plan.startup_evidence.mode} "
        f"(green_path={plan.green_path})"
    )
    if plan.detail:
        print(f"detail: {plan.detail}")
    print(f"next: {plan.next_action}")
    return 0 if plan.ok else 1


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

    # Redmine #12751: the read-only `resolve` subcommand + the shared resolution
    # core (discovery / route construction / fail-closed renderer) live in
    # `cli_project_gateway_resolve`; register it here so the whole tree is
    # assembled in one place.
    register_resolve(gateway_sub)

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

    route_plan = gateway_sub.add_parser(
        "route-plan",
        help=(
            "Read-only: resolve the current-Unit relative delegation route one step "
            "down (Redmine #12699). --from-role names this Unit's lane role; the "
            "route resolves the next-step-down project_gateway / "
            "delegated_coordinator / implementation_worker semantically and reports "
            "the launch-or-adopt action, the anchor requirement, and whether the "
            "resolved lane is cockpit-visible green-path evidence (a detached "
            "--no-attach normal session / cockpit --json preview is not)."
        ),
    )
    route_plan.add_argument(
        "--from-role",
        dest="from_role",
        required=True,
        choices=list(RELATIVE_CALLER_ROLES),
        help=(
            "The current Unit's lane role (the relative anchor). One of "
            f"{', '.join(RELATIVE_CALLER_ROLES)}; the one-step-down target is "
            "derived from it (a grandchild worker has no downward delegation)."
        ),
    )
    route_plan.add_argument(
        "--repo",
        required=True,
        help="Workspace Git worktree root (repo_root authority).",
    )
    route_plan.add_argument(
        "--project",
        required=True,
        help="Adopted project scope id (redmine_project) for the relative route.",
    )
    route_plan.add_argument(
        "--session",
        default=None,
        help=(
            "Optional session or cockpit group to narrow candidates. Omit to "
            "resolve across separate windows/sessions (the normal path)."
        ),
    )
    route_plan.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the structured RelativeRoutePlan payload as JSON.",
    )
    route_plan.set_defaults(func=cmd_project_gateway_route_plan)

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

    # Redmine #12740 / #12751: the forward no-anchor consultation leg. Its handler +
    # parser live in `cli_project_gateway_consult` (bounded extraction so consult's
    # change impact is localized and it has an independent test target); register it
    # here so the whole `project-gateway` subcommand tree is assembled in one place.
    register_consult(gateway_sub)

    # Redmine #12748: the parent -> child no-anchor work-intake leg. Its handler +
    # parser live in `cli_project_gateway_child_intake` (extracted to keep this
    # registrar module under the module-health line cap, like the
    # `cli_handoff_ticketless` / `cli_handoff_q_enter` splits); register it here so
    # the whole `project-gateway` subcommand tree is assembled in one place.
    register_child_intake(gateway_sub)
