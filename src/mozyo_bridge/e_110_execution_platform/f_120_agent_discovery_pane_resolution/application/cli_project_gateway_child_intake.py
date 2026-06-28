"""CLI surface for `project-gateway child-intake` (Redmine #12748 parent -> child no-anchor work-intake).

Split out of :mod:`...application.cli_project_gateway` so the project-gateway
command family stays under the module-health line cap (the same extraction
pattern used for ``cli_handoff_ticketless`` / ``cli_handoff_q_enter``). This module
owns the parent -> child forward ticketless work-intake leg: the handler
(:func:`cmd_project_gateway_child_intake`) and its parser registration
(:func:`register_child_intake`). The sibling ``cli_project_gateway`` registrar
calls :func:`register_child_intake` so the whole ``project-gateway`` subcommand
tree is still assembled in one place.

The leg is the one-step-down sibling of ``project-gateway consult`` (#12740):
consult forwards a no-anchor consultation grandparent -> parent; this forwards a
no-anchor *work-intake* parent (``project_gateway``) -> child
(``delegated_coordinator``), with the same-lane guard that refuses to adopt the
parent as its own child. See the handler docstring for the contract boundaries.
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
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.child_intake_route import (
    STATUS_CHILD_RESOLVED,
    ChildIntakeRouteError,
    resolve_child_intake_route,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff_ticketless import (
    _add_ticketless_delivery_options,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
    CALLBACK_METHODS,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_work_intake import (
    ROLE_DELEGATED_COORDINATOR,
    WORK_SHAPES,
    WORK_SHAPE_DOMAIN_DESIGN,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_PROJECT_GATEWAY,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    require_tmux,
)
from mozyo_bridge.shared.errors import die


def _discover_candidates() -> list:
    """All classified target candidates across every session (no pre-filter).

    Mirrors ``cli_project_gateway._discover_candidates`` (kept local to avoid an
    import cycle with the registrar): discovery is intentionally unfiltered so the
    same-lane / child resolver applies the role / repo / project / session
    predicates itself and its near-miss reasons stay visible. Patched in tests.
    """
    return _agents_target_candidates(argparse.Namespace(agent=None, session=None))


def cmd_project_gateway_child_intake(args: argparse.Namespace) -> int:
    """Forward a no-anchor ticketless work-intake to the child coordinator (#12748).

    The one-step-down sibling of ``project-gateway consult`` (#12740): consult
    forwards a no-anchor consultation grandparent -> parent; this forwards a
    no-anchor *work-intake* parent (``project_gateway``) -> child
    (``delegated_coordinator``). The parent must NOT answer the domain/design
    consultation itself; it routes the work to the child as ticketless work-intake,
    and the CHILD owns the Redmine issue/journal create / select / blocked decision
    (the parent does not return ``anchor_required`` merely because no anchor exists).

    Resolves the child by semantic identity with the same-lane guard
    (:func:`resolve_child_intake_route`): the caller's own lane (``--from-pane``, a
    negative self-fence, never the target authority) is excluded so the child cannot
    resolve back to the parent's own lane. Fails closed (no delivery, no payload
    injected) on ``same_lane`` / ``child_missing`` / ``child_ambiguous``. On
    ``child_resolved`` it injects the resolved child pane and delivers the
    work-intake through the gated no-anchor :func:`orchestrate_handoff` WITHOUT a
    Redmine anchor and without fabricating one.

    Unlike ``consult`` / ``handoff`` this leg injects NO transition_role /
    workflow_contract boundary: the #12706 ``project_gateway`` boundary models the
    gateway as the domain-OWNER of the grandparent -> parent leg (it ALLOWS
    ``project_domain_decision`` and hands off to ``implementation_worker``), which is
    the opposite of the #12748 parent -> child contract (the parent must not answer
    domain; the child owns it). The ``TicketlessWorkIntake`` envelope itself carries
    this leg's role/ownership contract (``read_contract=delegated_coordinator``,
    ``parent_must_not_answer_domain``, ``child_owns_anchor_decision``,
    ``callback_to_role=project_gateway``). The worker-dispatch Redmine-anchor gate is
    NOT relaxed (this rail forwards a work-intake only; no anchor, no dispatch token).
    """
    require_tmux()

    # The child / implementation gateway is a Codex coordinator unit (the same live
    # identity as the project gateway). The grandchild worker (Claude) is reached
    # only after the child mints a Redmine anchor, so a direct project-Claude
    # work-intake send is forbidden.
    if args.to != AGENT_KIND_CODEX:
        die(
            "`project-gateway child-intake` delivers to the child coordinator, "
            f"which is a Codex unit; `--to {args.to}` is not allowed. The grandchild "
            "worker (Claude) is reached only after the child creates a Redmine "
            "anchor — use `--to codex`. Direct project-Claude send is forbidden by "
            "the ticketless project gateway contract."
        )

    if not args.target_repo or args.target_repo == "auto":
        die(
            "`project-gateway child-intake` resolves the child pane semantically, so "
            "it needs a concrete `--target-repo <git-root>` (not `auto`, which "
            "requires an explicit %pane). Pass the workspace Git root."
        )
    if not args.target_project:
        die(
            "`project-gateway child-intake` requires `--target-project "
            "<project_scope>` to resolve the child coordinator. To gate on the Git "
            "repo root only, use `handoff ticketless-callback` with an explicit "
            "`--target`."
        )
    if getattr(args, "target", None):
        die(
            "`project-gateway child-intake` selects the child pane by semantic "
            "identity; do not pass `--target %pane`. The forward work-intake never "
            "carries a pane id to the ticketless receiver."
        )

    caller_pane = (getattr(args, "from_pane", None) or "").strip()
    if not caller_pane:
        die(
            "`project-gateway child-intake` requires `--from-pane <the parent "
            "gateway's own %pane>`: the caller's own lane id is the same-lane "
            "self-fence so the child route cannot resolve back to the parent lane "
            "(the runtime-ux `親 -> 子` fail condition `route が parent 自身へ戻る`). "
            "It is never the target authority — the child is resolved by "
            "`--target-repo` + `--target-project` semantic identity."
        )

    try:
        route = resolve_child_intake_route(
            _discover_candidates(),
            repo_root=args.target_repo,
            project_scope=args.target_project,
            caller_pane=caller_pane,
            session=getattr(args, "gateway_session", None),
        )
    except ChildIntakeRouteError as exc:
        die(str(exc))
        raise AssertionError("unreachable")

    if route.status != STATUS_CHILD_RESOLVED or route.selected is None:
        # Fail closed; do not deliver and inject no work-intake payload. Surface the
        # classification (same_lane / child_missing / child_ambiguous) + next action.
        if getattr(args, "as_json", False):
            print(
                _json.dumps(
                    route.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
                )
            )
            return 1
        print(f"status: {route.status}")
        print(
            "route: "
            f"repo_root={route.repo_root} project_scope={route.project_scope} "
            f"caller_pane={route.caller_pane} self_is_gateway={route.self_is_gateway}"
        )
        print(f"detail: {route.detail}")
        resolution = route.decision.resolution
        if resolution.matched:
            print("matched (distinct child lanes — ambiguous, refuse to adopt):")
            for cand in resolution.matched:
                print(
                    f"  - pane_id={cand.pane_id} session={cand.session} "
                    f"window={cand.window_name}"
                )
        if resolution.near_misses:
            print("near misses (why each pane was not the child):")
            for near in resolution.near_misses:
                cand = near.candidate
                print(
                    f"  - pane_id={cand.pane_id} role={cand.role} "
                    f"repo={cand.repo_short} project_scope={cand.project_scope or '<none>'} "
                    f"reason={near.reason}"
                )
        return 1

    # Inject the resolved CHILD pane and the forward work-intake payload, then
    # delegate to the gated no-anchor orchestrator. The repo + project gates in
    # orchestrate_handoff re-verify the resolved pane before any send.
    args.target = route.selected.pane_id
    # Redmine #12748: the forward work-intake payload, built programmatically (not
    # operator-typed) so it is product evidence, not a hand-asserted role payload.
    # The parent forwards a work shape, names that the child (delegated_coordinator)
    # owns the anchor decision and acts under that role contract, and asks the child
    # to return the result to the project_gateway lane via either no-anchor return
    # primitive. No transition_role / workflow_contract is injected here (see the
    # docstring): the work-intake envelope carries this leg's contract.
    args.work_shape = getattr(args, "work_shape", None) or WORK_SHAPE_DOMAIN_DESIGN
    args.callback_to_role = ROLE_PROJECT_GATEWAY
    args.callback_methods = list(CALLBACK_METHODS)
    args.read_contract = ROLE_DELEGATED_COORDINATOR
    return orchestrate_handoff(
        args,
        default_kind="design_consultation",
        ticketless=True,
        ticketless_work_intake=True,
    )


def register_child_intake(gateway_sub) -> None:
    """Register the ``project-gateway child-intake`` subcommand onto ``gateway_sub``."""
    child_intake = gateway_sub.add_parser(
        "child-intake",
        help=(
            "Forward a no-anchor ticketless work-intake from the project gateway to "
            "the child / implementation coordinator (Redmine #12748). The "
            "one-step-down sibling of `consult`: the parent must NOT answer the "
            "domain/design itself; it routes the work to the child as ticketless "
            "work-intake and the CHILD owns the Redmine anchor create/select/blocked "
            "decision. Resolves the child by semantic identity with a same-lane guard "
            "(--from-pane is the caller's own lane, a self-fence so the child cannot "
            "resolve back to the parent lane — NOT the target authority). Requires "
            "--target-repo + --target-project and --to codex. Fails closed (no "
            "delivery) on same-lane / missing / ambiguous child route. Delivers "
            "WITHOUT a Redmine anchor and without fabricating one; the worker-dispatch "
            "/ implementation / domain-probe Redmine-anchor gate is NOT relaxed."
        ),
    )
    # Reuse the ticketless delivery knobs (no --source / --issue / --journal anchor
    # flags, no --kind): the route's repo/project/role come from
    # --target-repo / --target-project / --to, --target is resolved (not typed), and
    # the forward work-intake payload is injected programmatically on child_resolved.
    _add_ticketless_delivery_options(child_intake)
    child_intake.add_argument(
        "--from-pane",
        dest="from_pane",
        required=True,
        help=(
            "The parent project gateway's OWN lane id (%%pane). Used only as the "
            "same-lane self-fence — it is excluded from the child candidate set so "
            "the child route cannot resolve back to the parent lane. It is never the "
            "target authority: the child is resolved by --target-repo + "
            "--target-project semantic identity."
        ),
    )
    child_intake.add_argument(
        "--work-shape",
        dest="work_shape",
        choices=list(WORK_SHAPES),
        default=WORK_SHAPE_DOMAIN_DESIGN,
        help=(
            "The class of work forwarded to the child for triage "
            f"(default `{WORK_SHAPE_DOMAIN_DESIGN}`). One of {', '.join(WORK_SHAPES)}. "
            "None of these authorizes a worker dispatch (that stays anchor-gated and "
            "is the child's decision after it mints a Redmine anchor); the shape only "
            "tells the child what kind of work to triage into an anchor."
        ),
    )
    child_intake.add_argument(
        "--gateway-session",
        dest="gateway_session",
        default=None,
        help=(
            "Optional session or cockpit group to narrow the child resolution to one "
            "candidate. Omit to resolve across separate windows/sessions."
        ),
    )
    child_intake.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="On a fail-closed resolution, emit the ChildIntakeRoute payload as JSON.",
    )
    child_intake.set_defaults(func=cmd_project_gateway_child_intake)
