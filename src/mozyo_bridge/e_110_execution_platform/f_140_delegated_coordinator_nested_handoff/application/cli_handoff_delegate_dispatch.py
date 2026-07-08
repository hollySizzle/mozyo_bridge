"""CLI registration for `handoff delegate-launch-adopt` / `delegate-grandchild-dispatch`.

Moved verbatim out of the oversized ``f_130_handoff_routing/application/cli_handoff.py``
(module-health gate, Redmine #13377 change window): these two registrations belong to the
delegated-coordinator feature package anyway (their handlers / vocabularies already live
here), following the ``cli_handoff_grandchild_realization`` precedent. Behavior-preserving:
help / choices / defaults / dest / ``func`` bindings are unchanged.
"""
from __future__ import annotations

import argparse

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    SOURCES,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.delegation_launch_adopt import (
    cmd_handoff_delegate_launch_adopt,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.grandchild_dispatch import (
    cmd_handoff_grandchild_dispatch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_launch_adopt import (
    LAUNCH_ADOPT_MODES,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_dispatch import (
    RECORD_POLICIES,
)


def register_delegate_launch_adopt(handoff_sub) -> None:
    """Register `handoff delegate-launch-adopt` (Redmine #12457).

    Read-only decision primitive for the parent -> delegated coordinator route.
    It resolves a fail-closed launch/adopt decision over `agents targets`
    candidate discovery and prints the decision + the durable parent-delegation
    record + (for an adopt outcome) the gated `handoff send --to codex` command
    the operator runs. It never sends and never targets a child Claude directly.
    """
    parser = handoff_sub.add_parser(
        "delegate-launch-adopt",
        help=(
            "Resolve a fail-closed delegated coordinator launch/adopt decision "
            "from durable policy + `agents targets` candidate discovery (Redmine "
            "#12457)"
        ),
        description=(
            "Decision primitive for the parent -> delegated coordinator route "
            "(Redmine #12457, US #12454). It is READ-ONLY and never sends: it "
            "runs the `agents targets` discovery pipeline, deterministically "
            "filters candidates by the Codex gateway role, the canonical child "
            "repo identity (`--target-repo`), lane state, and uniqueness, and "
            "resolves `--launch-adopt-mode` (disabled / adopt_existing / "
            "launch_new / launch_or_adopt) to an adopt / launch / fail_closed "
            "outcome. `agents targets` is candidate discovery only — selection "
            "fails closed on a disabled policy, a missing repo identity, a weak / "
            "ambiguous identity, zero candidates (unless the mode launches), or "
            "more than one match. For an adopt outcome it prints the gated "
            "`handoff send --to codex` command to run manually; it never targets "
            "a child Claude directly and uses no window / session / title / "
            "display proximity as routing authority. The durable anchor stays "
            "the Redmine issue / journal; this command prints a pointer + a "
            "pasteable parent delegation decision record."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--launch-adopt-mode",
        dest="launch_adopt_mode",
        required=True,
        choices=sorted(LAUNCH_ADOPT_MODES),
        help=(
            "Durable launch/adopt policy mode (read from the durable record, not "
            "pane proximity): `disabled` forms no route (fail-closed missing "
            "policy); `adopt_existing` adopts exactly one matching child Codex "
            "gateway; `launch_new` always launches a new lane; `launch_or_adopt` "
            "adopts a unique match else launches, and fails closed on more than "
            "one match."
        ),
    )
    parser.add_argument(
        "--target-repo",
        dest="target_repo",
        required=True,
        help=(
            "Mandatory canonical child repo identity gate: a candidate is "
            "adoptable only when its pane cwd resolves to this repo root. Without "
            "it the decision fails closed (selecting a pane from layout alone "
            "would recreate the #12455 missing-context violation). Pass the "
            "explicit canonical child repo root path."
        ),
    )
    parser.add_argument(
        "--parent-coordinator-route",
        dest="parent_coordinator_route",
        required=True,
        help=(
            "Durable route anchor of the parent coordinator (the mandatory "
            "`delegation_parent` callback target). The parent retains parent "
            "issue close / owner approval authority, so every route must be "
            "callbackable to it."
        ),
    )
    parser.add_argument(
        "--callback-target",
        dest="callback_target",
        action="append",
        metavar="PURPOSE=ROUTE",
        help=(
            "Repeatable additional callback target "
            "(`owning_us_coordinator=<route>` / `audit_coordinator=<route>`) for "
            "the child project's owning-US / audit coordinator when it is a "
            "different lane than the delegation parent."
        ),
    )
    parser.add_argument(
        "--child-project",
        dest="child_project",
        help="Child project identifier recorded in the delegation decision.",
    )
    parser.add_argument(
        "--parent-issue",
        dest="parent_issue",
        help="Parent issue / US id recorded in the delegation decision.",
    )
    parser.add_argument(
        "--child-issue",
        dest="child_issue",
        help=(
            "Child project issue id used in the recommended `handoff send` "
            "anchor (defaults to a placeholder in the printed command)."
        ),
    )
    parser.add_argument(
        "--parent-project",
        dest="parent_project",
        help="Parent project identifier for the role-profile `parent_project` field.",
    )
    parser.add_argument(
        "--source",
        choices=sorted(SOURCES),
        default="redmine",
        help="Durable record source system for the recommended command (default redmine).",
    )
    parser.add_argument(
        "--journal",
        help="Optional Redmine journal id for the recommended command anchor.",
    )
    parser.add_argument(
        "--excluded-lane",
        dest="excluded_lane",
        action="append",
        metavar="LANE_ID",
        help=(
            "Repeatable lane id to exclude from adoption (e.g. a retired or "
            "incompatible-active lane) so it never becomes a candidate."
        ),
    )
    parser.add_argument(
        "--session",
        help="Restrict candidate discovery to this tmux session (read-only filter).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the decision, callback targets, recommended command, and record as JSON.",
    )
    parser.set_defaults(func=cmd_handoff_delegate_launch_adopt)


def register_grandchild_dispatch(handoff_sub) -> None:
    """Register `handoff delegate-grandchild-dispatch` (Redmine #12458).

    Read-only decision primitive for the delegated coordinator -> grandchild
    implementation lane route (depth 2). It resolves the delegation policy gate
    (`enable_grandchild_dispatch` / `max_delegation_depth: 2` / master gate /
    active-lane capacity) and a fail-closed launch/adopt decision over `agents
    targets` candidate discovery, and prints the decision + the durable
    `## Delegated dispatch decision` (decision-records §2) + `## Delegated
    callback targets` (§4) records + (for an adopt outcome) the gated `handoff
    send --to codex` command the operator runs. It never sends, never targets a
    grandchild Claude directly, and the grandchild lane is always a declared
    durable-anchored cockpit lane, never a hidden subagent.
    """
    parser = handoff_sub.add_parser(
        "delegate-grandchild-dispatch",
        help=(
            "Resolve a fail-closed delegated coordinator -> grandchild dispatch "
            "decision from delegation policy + `agents targets` candidate "
            "discovery (Redmine #12458)"
        ),
        description=(
            "Decision primitive for the delegated coordinator -> grandchild "
            "implementation lane route (Redmine #12458, US #12454, depth 2). It "
            "is READ-ONLY and never sends. It first resolves the delegation "
            "policy gate — the `enable_delegated_coordinator` master gate, the "
            "`enable_grandchild_dispatch` depth-2 permission, the "
            "`max_delegation_depth` hop ceiling (hard ceiling 2), and the "
            "`max_active_child_lanes` capacity — and fails closed with an "
            "explicit reason if depth-2 dispatch is not permitted. When "
            "permitted it runs the `agents targets` discovery pipeline, "
            "deterministically filters candidates by the Codex gateway role, the "
            "canonical child repo identity (`--target-repo`), lane state, and "
            "uniqueness, and resolves `--launch-adopt-mode` to a dispatch_adopt / "
            "dispatch_launch / fail_closed outcome; `--no-dispatch REASON` records "
            "the `grandchild_dispatch: avoided` path instead. `agents targets` is "
            "candidate discovery only; selection never targets a grandchild Claude "
            "directly and uses no window / session / title / display proximity as "
            "routing authority. The grandchild lane it decides to launch / adopt "
            "is always a declared durable-anchored cockpit lane, never a hidden "
            "subagent. The durable anchor stays the Redmine issue / journal; this "
            "command prints a pointer + the §2 dispatch decision record and the "
            "§4 multi-coordinator callback targets record (the GK parent route "
            "and the mozyo_bridge coordinator route are both required and "
            "replayable)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--launch-adopt-mode",
        dest="launch_adopt_mode",
        default="launch_or_adopt",
        choices=sorted(LAUNCH_ADOPT_MODES),
        help=(
            "Durable launch/adopt policy mode for the grandchild lane (read from "
            "the durable record, not pane proximity): `disabled` forms no route; "
            "`adopt_existing` adopts exactly one matching grandchild Codex "
            "gateway; `launch_new` always launches a new lane; `launch_or_adopt` "
            "(default) adopts a unique match else launches, and fails closed on "
            "more than one match. Ignored for `--no-dispatch`."
        ),
    )
    parser.add_argument(
        "--target-repo",
        dest="target_repo",
        help=(
            "Mandatory canonical child repo identity gate for an adopt decision: "
            "a candidate is adoptable only when its pane cwd resolves to this "
            "repo root. Without it the dispatch fails closed (selecting a pane "
            "from layout alone would recreate the #12455 missing-context "
            "violation). Not required for `--no-dispatch`."
        ),
    )
    # --- delegation policy knobs (durable-record-derived; config loader is the
    #     #12390 follow-up). Out-of-range values clamp fail-closed in the domain.
    parser.add_argument(
        "--enable-delegated-coordinator",
        dest="enable_delegated_coordinator",
        action="store_true",
        help="Master gate: nested delegation is permitted (default off / safety-biased).",
    )
    parser.add_argument(
        "--enable-grandchild-dispatch",
        dest="enable_grandchild_dispatch",
        action="store_true",
        help="Permit depth-2 (grandchild) dispatch (default off; requires the master gate and depth>=2).",
    )
    parser.add_argument(
        "--max-delegation-depth",
        dest="max_delegation_depth",
        type=int,
        default=1,
        help="Root-relative delegation hop ceiling (0..2; hard ceiling 2). Grandchild needs >=2. Default 1.",
    )
    parser.add_argument(
        "--max-active-child-lanes",
        dest="max_active_child_lanes",
        type=int,
        default=1,
        help="Max concurrent child/grandchild lanes one delegated coordinator may hold (>=1). Default 1.",
    )
    parser.add_argument(
        "--decision-record-policy",
        dest="decision_record_policy",
        default="minimal",
        choices=sorted(RECORD_POLICIES),
        help="No-dispatch / context-neutral record granularity (`minimal` | `verbose`). Default minimal.",
    )
    parser.add_argument(
        "--current-depth",
        dest="current_depth",
        type=int,
        default=1,
        help="Depth of the dispatching delegated coordinator (default 1; grandchild lands at current+1).",
    )
    parser.add_argument(
        "--active-grandchild-lanes",
        dest="active_grandchild_lanes",
        type=int,
        default=0,
        help="Count of active grandchild lanes the delegated coordinator already holds (capacity check).",
    )
    parser.add_argument(
        "--no-dispatch",
        dest="no_dispatch",
        metavar="REASON",
        help=(
            "Record an explicit `grandchild_dispatch: avoided` no-dispatch "
            "decision with this reason (decision-records §3; e.g. "
            "`context_cost_low` / `single_pass_no_iteration` / "
            "`urgent_minimal_correction` or a borderline `<具体記述>`). Skips tmux "
            "discovery — the delegated coordinator keeps the work in its own lane."
        ),
    )
    # --- multi-coordinator callback coverage (decision-records §4.1) ----------
    parser.add_argument(
        "--parent-coordinator-route",
        dest="parent_coordinator_route",
        required=True,
        help=(
            "Durable route anchor of the GK parent coordinator (the mandatory "
            "`delegation_parent` callback target). The parent retains parent "
            "issue close / owner approval authority, so every route is "
            "callbackable to it."
        ),
    )
    parser.add_argument(
        "--owning-coordinator-route",
        dest="owning_coordinator_route",
        help=(
            "Durable route anchor of the mozyo_bridge owning-US / audit "
            "coordinator (the required `owning_us_coordinator` callback target) "
            "when it is a different lane than the GK parent. Both this route and "
            "the parent route must be replayable; supply this OR "
            "`--owning-same-as-parent`."
        ),
    )
    parser.add_argument(
        "--owning-same-as-parent",
        dest="owning_same_as_parent",
        action="store_true",
        help=(
            "Declare the owning-US / audit coordinator callback route is the same "
            "as the delegation parent route (explicit `same_as_delegation_parent` "
            "— coverage is never omitted by assumption)."
        ),
    )
    parser.add_argument(
        "--callback-target",
        dest="callback_target",
        action="append",
        metavar="PURPOSE=ROUTE",
        help=(
            "Repeatable additional callback target "
            "(`owning_us_coordinator=<route>` / `audit_coordinator=<route>`)."
        ),
    )
    parser.add_argument(
        "--child-project",
        dest="child_project",
        help="Child project identifier recorded in the dispatch decision.",
    )
    parser.add_argument(
        "--delegated-coordinator",
        dest="delegated_coordinator",
        help="Delegated coordinator lane pointer recorded in the dispatch decision.",
    )
    parser.add_argument(
        "--parent-issue",
        dest="parent_issue",
        help="Parent issue / US id recorded in the dispatch decision.",
    )
    parser.add_argument(
        "--child-issue",
        dest="child_issue",
        help="Child (grandchild-target) issue id recorded in the decision / recommended command.",
    )
    parser.add_argument(
        "--dispatch-anchor",
        dest="dispatch_anchor",
        help="Durable dispatch anchor pointer recorded in the dispatch decision.",
    )
    parser.add_argument(
        "--source",
        choices=sorted(SOURCES),
        default="redmine",
        help="Durable record source system for the recommended command (default redmine).",
    )
    parser.add_argument(
        "--journal",
        help="Optional Redmine journal id for the recommended command anchor.",
    )
    parser.add_argument(
        "--excluded-lane",
        dest="excluded_lane",
        action="append",
        metavar="LANE_ID",
        help="Repeatable lane id to exclude from adoption so it never becomes a candidate.",
    )
    parser.add_argument(
        "--session",
        help="Restrict candidate discovery to this tmux session (read-only filter).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the decision, policy gate, callback targets, recommended command, and records as JSON.",
    )
    parser.set_defaults(func=cmd_handoff_grandchild_dispatch)


__all__ = (
    "register_delegate_launch_adopt",
    "register_grandchild_dispatch",
)
