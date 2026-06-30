"""CLI parser registration for the grandchild realization commands (Redmine #12473).

Split out of ``application/cli_handoff.py`` so that module stays under the
module-health line threshold (Redmine #12473 j#64190): the #12473 grandchild
realization surface (the ``delegate-grandchild-stamp`` actuator and the
``delegate-grandchild-gate`` realize-or-blocked gate) is a cohesive parser
family, registered here and invoked from ``cli_handoff.register()`` via
:func:`register_grandchild_realization`. Behavior-preserving move; help / choices
/ defaults / ``func`` bindings are unchanged (same precedent as the
``cli_agents`` / ``cli_handoff`` extraction, Redmine #12153).
"""
from __future__ import annotations

import argparse

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.grandchild_stamp import (
    cmd_handoff_grandchild_gate,
    cmd_handoff_grandchild_stamp,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import REALIZATIONS


def register_grandchild_realization(handoff_sub) -> None:
    """Register the grandchild realization subcommands onto ``handoff_sub``."""
    _register_grandchild_stamp(handoff_sub)
    _register_grandchild_gate(handoff_sub)


def _register_grandchild_stamp(handoff_sub) -> None:
    """Register `handoff delegate-grandchild-stamp` (Redmine #12473).

    The side-effecting actuator that connects the #12458 grandchild dispatch
    decision (or a same-lane worker handoff) to live delegation metadata
    stamping. It takes the declared delegation chain (governance truth from the
    durable Redmine record, never inferred from pane proximity), validates the
    tree + the grandchild acceptance shape (a depth-2 `implementation` lane), and
    stamps the `@mozyo_lane_kind` / `@mozyo_delegation_parent` projection-cache
    options the discovery read path consumes so `agents targets` shows
    `KIND=implementation` / `DEPTH=2` / `PARENT=<delegated coordinator lane>`. It
    closes the #12460 `PARTIAL-display` gap: a decision record / same-lane worker
    only is not a full display PASS. Safe by default (preview unless `--apply`;
    `--dry-run` wins); display/audit breadcrumb only, never routing authority,
    never a direct grandchild Claude send, never a hidden subagent.
    """
    parser = handoff_sub.add_parser(
        "delegate-grandchild-stamp",
        help=(
            "Stamp live delegation metadata (`@mozyo_lane_kind` / "
            "`@mozyo_delegation_parent`) for a realized grandchild lane so "
            "`agents targets` shows KIND/DEPTH/PARENT (Redmine #12473)"
        ),
        description=(
            "Side-effecting actuator for the delegated coordinator -> grandchild "
            "realization (Redmine #12473, US #12454). It connects the #12458 "
            "grandchild dispatch decision to the live delegation metadata "
            "stamping #12460 found missing: given the DECLARED delegation chain "
            "(read from the durable Redmine record via repeatable `--lane` "
            "specs, never inferred from pane proximity) and which lane is the "
            "realized grandchild (`--grandchild-unit`), it validates the tree "
            "through the closed #12465 projection foundation (fail-closed on a "
            "cycle / unknown parent / depth > 2 / off-contract kind), asserts the "
            "grandchild derives to a depth-2 `implementation` lane, and stamps "
            "the `@mozyo_lane_kind` / `@mozyo_delegation_parent` options the "
            "discovery read path consumes onto each declared pane. `DEPTH` / "
            "`ROOT` are derived from the parent chain by the read model, never "
            "read from a pane option, so they are not stamped. Safe by default: "
            "preview (no tmux mutation) unless `--apply`; `--dry-run` wins. The "
            "stamped options are a re-derivable display / audit breadcrumb, never "
            "routing authority; this command sends nothing and the grandchild "
            "lane is a declared durable-anchored cockpit lane, never a hidden "
            "subagent. It prints the replayable `## Grandchild lane realization` "
            "record for the Redmine journal."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--lane",
        dest="lane",
        action="append",
        required=True,
        metavar="kind=..,unit=..,parent=..,pane=..",
        help=(
            "Repeatable declared-chain lane spec (comma-separated KEY=VALUE): "
            "`kind=<coordinator|delegated_coordinator|implementation>,"
            "unit=<workspace_id/lane_id>,parent=<workspace_id/lane_id|->,"
            "pane=%%N[,pane=%%M]`. `parent=-` (or none/root) marks the tree root; "
            "`pane` may repeat. Declare the full chain (parent coordinator -> "
            "delegated coordinator -> grandchild) so the grandchild depth derives; "
            "a lane with no `pane=` is declared for derivation only and is not "
            "stamped."
        ),
    )
    parser.add_argument(
        "--grandchild-unit",
        dest="grandchild_unit",
        required=True,
        metavar="workspace_id/lane_id",
        help=(
            "The declared lane that is the realized grandchild. It must derive to "
            "a depth-2 `implementation` lane, else the stamp fails closed (a "
            "decision / same-lane-worker-only route is not a full display PASS)."
        ),
    )
    parser.add_argument(
        "--realization",
        dest="realization",
        required=True,
        choices=sorted(REALIZATIONS),
        help=(
            "Whether the grandchild lane was newly created (`launch`) or an "
            "existing lane was explicitly adopted (`adopt`). `adopt` requires "
            "`--adopt-reason`."
        ),
    )
    parser.add_argument(
        "--adopt-reason",
        dest="adopt_reason",
        help=(
            "Replayable reason an existing lane was adopted as the grandchild "
            "(required for `--realization adopt`; rejected for `launch`)."
        ),
    )
    parser.add_argument(
        "--parent-issue",
        dest="parent_issue",
        help="Parent issue / US id recorded in the realization record.",
    )
    parser.add_argument(
        "--child-issue",
        dest="child_issue",
        help="Child (grandchild-target) issue id recorded in the realization record.",
    )
    parser.add_argument(
        "--delegated-coordinator",
        dest="delegated_coordinator",
        help="Delegated coordinator lane pointer recorded in the realization record.",
    )
    parser.add_argument(
        "--dispatch-anchor",
        dest="dispatch_anchor",
        help="Durable dispatch anchor pointer recorded in the realization record.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the `set-option` writes (best-effort). Default previews only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Force preview (no tmux mutation); wins over `--apply`.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the plan, derived projections, and realization record as JSON.",
    )
    parser.set_defaults(func=cmd_handoff_grandchild_stamp)


def _register_grandchild_gate(handoff_sub) -> None:
    """Register `handoff delegate-grandchild-gate` (Redmine #12473 j#64151).

    The realize-or-blocked gate that closes the #12474 runtime-path hole: a
    delegated coordinator could resolve a grandchild dispatch decision and then
    silently fall through to a same-lane worker handoff, leaving the grandchild
    unrealized and KIND/DEPTH/PARENT blank. This gate reads `agents targets`
    discovery, looks for a route-bound depth-2 `implementation` grandchild lane
    under the delegated coordinator unit, and returns `realized` /
    `same_lane_ok` / `blocked`. `blocked` (grandchild required but none realized)
    exits non-zero with a replayable record so the runtime records blocked
    instead of treating a same-lane worker handoff as display acceptance. It
    sends nothing, holds no routing authority, and never promotes
    window/session/title/proximity into a route.
    """
    parser = handoff_sub.add_parser(
        "delegate-grandchild-gate",
        help=(
            "Gate a delegated-coordinator worker handoff on grandchild "
            "realization: realized / same_lane_ok / blocked (Redmine #12473 "
            "j#64151)"
        ),
        description=(
            "Realize-or-blocked gate for the delegated coordinator -> grandchild "
            "runtime path (Redmine #12473 j#64151, #12474 QA). A grandchild "
            "dispatch decision that requires a separate grandchild lane "
            "(dispatch_launch / dispatch_adopt) must not silently fall through to "
            "a same-lane worker handoff. This command reads `agents targets` "
            "discovery, derives each lane's delegation breadcrumb (the same "
            "read-only #12466 projection), and checks for a route-bound depth-2 "
            "`implementation` grandchild lane whose parent is "
            "`--delegated-coordinator-unit`. Verdict: `realized` (proceed), "
            "`same_lane_ok` (`--no-require-grandchild`, the dispatch was "
            "no_dispatch), or `blocked` (a grandchild is required but none is "
            "realized/stamped) — `blocked` exits non-zero with a replayable "
            "`## Grandchild realization gate` record so the runtime records "
            "blocked instead of treating a same-lane worker handoff as a display "
            "PASS. Read-only over discovery; sends nothing; holds no routing "
            "authority; never promotes window/session/title/proximity into a "
            "route. Remediation for `blocked`: create/adopt a grandchild "
            "lane/window and run `handoff delegate-grandchild-stamp`, then re-run "
            "this gate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--delegated-coordinator-unit",
        dest="delegated_coordinator_unit",
        required=True,
        metavar="workspace_id/lane_id",
        help=(
            "The delegated coordinator lane unit; a realized grandchild is a "
            "depth-2 `implementation` lane whose `delegation_parent` is this unit."
        ),
    )
    parser.add_argument(
        "--no-require-grandchild",
        dest="require_grandchild",
        action="store_false",
        default=True,
        help=(
            "Declare the dispatch decision did NOT require a grandchild lane "
            "(a no_dispatch / low-context same-lane outcome), so a same-lane "
            "worker is the legitimate verdict. Default (fail-closed) assumes a "
            "grandchild IS required, so an unrealized grandchild blocks."
        ),
    )
    parser.add_argument(
        "--parent-issue",
        dest="parent_issue",
        help="Parent issue / US id recorded in the gate record.",
    )
    parser.add_argument(
        "--child-issue",
        dest="child_issue",
        help="Child (grandchild-target) issue id recorded in the gate record.",
    )
    parser.add_argument(
        "--session",
        help="Restrict candidate discovery to this tmux session (read-only filter).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the gate verdict, realized grandchild unit, and record as JSON.",
    )
    parser.set_defaults(func=cmd_handoff_grandchild_gate)


__all__ = ("register_grandchild_realization",)
