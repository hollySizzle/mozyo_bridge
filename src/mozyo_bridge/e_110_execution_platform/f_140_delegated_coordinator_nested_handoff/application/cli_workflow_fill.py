"""CLI surface for the advisory `workflow fill-decision` command (Redmine #12855).

`mozyo-bridge workflow fill-decision` exposes the documented Post-Dispatch Fill Loop
decision (``vibes/docs/logics/coordinator-sublane-development-flow.md`` `### Post-
Dispatch Fill Loop`) as a machine-readable result, so a coordinator / auditor who did
not read (or forgot) the spine can still ask the command "given this lane set, do I
dispatch another sublane, or stop — and for which concrete reason?".

The command is **advisory only** (issue #12855 j#68506 MVP boundary):

- it discovers nothing — the active lane set, ready-work counts, remaining soft-profile
  capacity, and owner/release-gate flag are all supplied by the caller as simple
  advisory inputs (Redmine-aware preflight is #12856; DB-backed runtime is #12857);
- it never selects / creates a Redmine issue and never creates / adopts a lane;
- it always returns exit code 0 — the output is informational and is not meant to
  hard-block a handoff. The fixed :data:`FILL_*` vocabulary is the value: it names the
  exact stop reason (or ``dispatch_next``) the coordinator records in the Bandwidth
  Record Template.

It deliberately makes the "an active ``implementing`` lane is not a stop reason"
invariant observable from the command line: pass only ``--lane <id>:implementing``
lanes with ready independent work and capacity and the decision is ``dispatch_next``.
"""

from __future__ import annotations

import argparse
import json as _json

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    FillDecisionInputs,
    FillDecisionOutcome,
    LaneState,
    evaluate_fill_decision,
)


def _parse_lane(spec: str) -> LaneState:
    """Parse a ``ISSUE:STATE`` ``--lane`` spec into a :class:`LaneState`.

    The ``STATE`` is kept literal — an unrecognized state class is not rejected here;
    the pure policy treats an unknown lane state conservatively as coordinator-blocking
    (fail toward stopping rather than over-dispatching on a misread class).
    """
    raw = (spec or "").strip()
    issue, sep, state = raw.partition(":")
    if not sep:
        raise argparse.ArgumentTypeError(
            f"--lane expects ISSUE:STATE (e.g. 12855:implementing), got {spec!r}"
        )
    issue = issue.strip()
    state = state.strip()
    if not issue or not state:
        raise argparse.ArgumentTypeError(
            f"--lane expects a non-empty ISSUE and STATE, got {spec!r}"
        )
    return LaneState(issue=issue, state_class=state)


def _inputs_from_args(args: argparse.Namespace) -> FillDecisionInputs:
    lanes = tuple(getattr(args, "lane", None) or ())
    return FillDecisionInputs(
        lanes=lanes,
        ready_independent_work=int(getattr(args, "ready_independent", 0) or 0),
        ready_overlapping_work=int(getattr(args, "ready_overlap", 0) or 0),
        capacity_remaining=int(getattr(args, "capacity", 0) or 0),
        owner_or_release_gate_active=bool(getattr(args, "owner_or_release_gate", False)),
    )


def _print_outcome_text(outcome: FillDecisionOutcome) -> None:
    print(f"fill_decision: {outcome.fill_decision}")
    print(f"advisory: {str(outcome.advisory).lower()}")
    print(f"next_drain_action: {outcome.next_drain_action}")
    print(
        "lanes: "
        f"active_implementing={list(outcome.active_implementing) or '<none>'} "
        f"coordinator_blocking={list(outcome.coordinator_blocking) or '<none>'}"
    )
    print(
        "capacity: "
        f"ready_independent_work={outcome.ready_independent_work} "
        f"capacity_remaining={outcome.capacity_remaining}"
    )
    print(f"reason: {outcome.reason}")


def cmd_workflow_fill_decision(args: argparse.Namespace) -> int:
    """Resolve and report the advisory Post-Dispatch Fill Loop decision (#12855).

    Builds :class:`FillDecisionInputs` from the supplied advisory flags, evaluates the
    pure policy, and emits exactly one structured envelope (text, or one JSON object
    with ``--json``). Always returns 0: the result is advisory and never blocks.
    """
    outcome = evaluate_fill_decision(_inputs_from_args(args))
    if getattr(args, "as_json", False):
        print(_json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_outcome_text(outcome)
    return 0


def register_fill_decision(workflow_sub) -> None:
    """Register ``workflow fill-decision`` onto the ``workflow`` subparser (#12855)."""
    fill = workflow_sub.add_parser(
        "fill-decision",
        description=(
            "Resolve the advisory Post-Dispatch Fill Loop decision for the current "
            "lane set (Redmine #12855). Given a caller-supplied summary of the active "
            "lanes (--lane ISSUE:STATE, repeatable), the count of ready independent / "
            "overlapping implementation work, the remaining local soft-profile "
            "capacity, and whether an owner/release gate is active, it returns one "
            "fixed decision token: dispatch_next, or a concrete stop reason "
            "(stop_no_ready_work / stop_overlap / stop_coordinator_blocking / "
            "stop_soft_profile_full / stop_owner_or_release_gate). An active "
            "'implementing' lane is not a stop reason. Advisory only: it discovers "
            "nothing, never selects/creates an issue or lane, and never blocks (exit "
            "0). See vibes/docs/logics/coordinator-sublane-development-flow.md."
        ),
        help=(
            "Advisory: report whether to dispatch another sublane or stop (and the "
            "concrete stop reason) for a caller-supplied lane set. Discovers nothing, "
            "never blocks."
        ),
    )
    fill.add_argument(
        "--lane",
        action="append",
        type=_parse_lane,
        metavar="ISSUE:STATE",
        help=(
            "An active lane as ISSUE:STATE (repeatable). STATE is a lane state class "
            "(implementing / callback_due / review_waiting / owner_waiting / "
            "integration_waiting / close_waiting / blocked / retire_ready / idle). An "
            "'implementing' lane is positive pipeline occupancy, not a stop reason."
        ),
    )
    fill.add_argument(
        "--ready-independent",
        dest="ready_independent",
        type=int,
        default=0,
        help="Count of ready implementation work items that do not overlap an active lane.",
    )
    fill.add_argument(
        "--ready-overlap",
        dest="ready_overlap",
        type=int,
        default=0,
        help=(
            "Count of ready implementation work items that overlap an active lane "
            "(file / invariant / merge order)."
        ),
    )
    fill.add_argument(
        "--capacity",
        dest="capacity",
        type=int,
        default=0,
        help="Remaining slots within the local soft profile for another active sublane.",
    )
    fill.add_argument(
        "--owner-or-release-gate",
        dest="owner_or_release_gate",
        action="store_true",
        help=(
            "An owner-decision / release / credential / destructive-operation gate is "
            "active (forces stop_owner_or_release_gate)."
        ),
    )
    fill.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit exactly one structured FillDecisionOutcome envelope as JSON.",
    )
    fill.set_defaults(func=cmd_workflow_fill_decision)


__all__ = ("cmd_workflow_fill_decision", "register_fill_decision")
