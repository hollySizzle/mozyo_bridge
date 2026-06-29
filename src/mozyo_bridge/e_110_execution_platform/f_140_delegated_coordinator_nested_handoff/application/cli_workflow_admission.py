"""CLI surface for the Redmine-aware `workflow admission` preflight (Redmine #12856).

`mozyo-bridge workflow admission` is the Redmine-aware companion to `workflow
fill-decision` (#12855). Where `fill-decision` takes *already-classified* lanes
(``--lane ISSUE:STATE``), `admission` takes the durable-record facts of each lane
(``--lane-signal ISSUE:GATE[,key=value...]``), classifies them with the pure
:func:`classify_lane_state` policy, and then runs the same Post-Dispatch Fill Loop
decision — so a coordinator/auditor who has the journal facts but is unsure which
:data:`LANE_STATE_*` class they imply can ask the command instead of mis-classifying by
hand.

The command is **advisory only** (issue #12856 j#68548 MVP boundary):

- it discovers nothing — every lane signal, the ready-work counts, the remaining
  soft-profile capacity, and the owner/release-gate flag are supplied by the caller
  from the durable record (live Redmine discovery / persistence is #12857);
- it never selects / creates a Redmine issue and never creates / adopts a lane;
- it always returns exit code 0 — the output is informational and is not meant to
  hard-block a handoff. The value is the classification + the fixed admission/fill
  vocabulary it produces, ready to paste into the Bandwidth Record Template.

It makes the "an active ``implementing`` lane is not a stop reason" invariant observable
end-to-end: pass only ``--lane-signal <id>:start`` lanes with ready independent work and
capacity and the decision is ``dispatch_sublane`` / ``dispatch_next``.
"""

from __future__ import annotations

import argparse
import json as _json

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    CALLBACK_STATES,
    GATE_KINDS,
    REVIEW_CONCLUSIONS,
    REVIEW_PENDING,
    CALLBACK_NONE,
    LaneSignal,
    SublaneAdmissionInputs,
    SublaneAdmissionOutcome,
    evaluate_sublane_admission,
    render_admission_journal,
)


def _parse_bool(key: str, value: str) -> bool:
    """Parse a ``key=value`` boolean modifier (``0``/``1``/``true``/``false``)."""
    norm = value.strip().lower()
    if norm in ("1", "true", "yes", "y"):
        return True
    if norm in ("0", "false", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(
        f"--lane-signal {key}= expects a boolean (0/1/true/false), got {value!r}"
    )


def _parse_lane_signal(spec: str) -> LaneSignal:
    """Parse a ``ISSUE:GATE[,key=value...]`` ``--lane-signal`` spec into a LaneSignal.

    The first ``:`` separates the issue id from the rest; the rest is a comma list whose
    first element is the gate kind and whose remaining ``key=value`` elements set the
    optional signal facts:

    - ``conclusion=`` pending|approved|changes_requested (only used for a ``review`` gate)
    - ``callback=`` none|due|delivery_failed
    - ``commit=`` 0|1 (commit-bearing work)
    - ``integrated=`` 0|1 (merge / push / patch-equivalent / explicit deferral recorded)
    - ``open=`` 0|1 (Redmine issue still open; default 1)
    - ``blocker=`` 0|1 (a blocker / failed handoff / unresolved dependency is recorded)

    The gate / conclusion / callback values are validated against the literal
    vocabularies so a typo is rejected at parse time rather than silently classifying to
    ``blocked``.
    """
    raw = (spec or "").strip()
    issue, sep, rest = raw.partition(":")
    if not sep:
        raise argparse.ArgumentTypeError(
            f"--lane-signal expects ISSUE:GATE (e.g. 12856:review_request), got {spec!r}"
        )
    issue = issue.strip()
    parts = [p.strip() for p in rest.split(",") if p.strip()]
    if not issue or not parts:
        raise argparse.ArgumentTypeError(
            f"--lane-signal expects a non-empty ISSUE and GATE, got {spec!r}"
        )
    gate = parts[0]
    if gate not in GATE_KINDS:
        raise argparse.ArgumentTypeError(
            f"--lane-signal gate must be one of {sorted(GATE_KINDS)}, got {gate!r}"
        )

    conclusion = REVIEW_PENDING
    callback = CALLBACK_NONE
    commit_bearing = False
    integration_recorded = False
    issue_open = True
    blocker_recorded = False

    for modifier in parts[1:]:
        key, eq, value = modifier.partition("=")
        if not eq:
            raise argparse.ArgumentTypeError(
                f"--lane-signal modifier expects key=value, got {modifier!r}"
            )
        key = key.strip()
        value = value.strip()
        if key == "conclusion":
            if value not in REVIEW_CONCLUSIONS:
                raise argparse.ArgumentTypeError(
                    f"--lane-signal conclusion= must be one of "
                    f"{sorted(REVIEW_CONCLUSIONS)}, got {value!r}"
                )
            conclusion = value
        elif key == "callback":
            if value not in CALLBACK_STATES:
                raise argparse.ArgumentTypeError(
                    f"--lane-signal callback= must be one of "
                    f"{sorted(CALLBACK_STATES)}, got {value!r}"
                )
            callback = value
        elif key == "commit":
            commit_bearing = _parse_bool(key, value)
        elif key == "integrated":
            integration_recorded = _parse_bool(key, value)
        elif key == "open":
            issue_open = _parse_bool(key, value)
        elif key == "blocker":
            blocker_recorded = _parse_bool(key, value)
        else:
            raise argparse.ArgumentTypeError(
                f"--lane-signal unknown modifier {key!r} (expected conclusion / callback "
                "/ commit / integrated / open / blocker)"
            )

    return LaneSignal(
        issue=issue,
        latest_gate=gate,
        review_conclusion=conclusion,
        callback_state=callback,
        commit_bearing=commit_bearing,
        integration_recorded=integration_recorded,
        issue_open=issue_open,
        blocker_recorded=blocker_recorded,
    )


def _inputs_from_args(args: argparse.Namespace) -> SublaneAdmissionInputs:
    signals = tuple(getattr(args, "lane_signal", None) or ())
    return SublaneAdmissionInputs(
        lane_signals=signals,
        ready_independent_work=int(getattr(args, "ready_independent", 0) or 0),
        ready_overlapping_work=int(getattr(args, "ready_overlap", 0) or 0),
        capacity_remaining=int(getattr(args, "capacity", 0) or 0),
        owner_or_release_gate_active=bool(getattr(args, "owner_or_release_gate", False)),
    )


def _print_outcome_text(outcome: SublaneAdmissionOutcome) -> None:
    print(f"admission_decision: {outcome.admission_decision}")
    print(f"fill_decision: {outcome.fill_decision}")
    print(f"advisory: {str(outcome.advisory).lower()}")
    print(f"next_drain_action: {outcome.next_drain_action}")
    if outcome.classified_lanes:
        for lane in outcome.classified_lanes:
            print(f"lane: {lane.issue} -> {lane.state_class}")
    else:
        print("lane: <none>")
    print(
        "lanes: "
        f"active_implementing={list(outcome.fill.active_implementing) or '<none>'} "
        f"coordinator_blocking={list(outcome.fill.coordinator_blocking) or '<none>'}"
    )
    print(
        "capacity: "
        f"ready_independent_work={outcome.fill.ready_independent_work} "
        f"capacity_remaining={outcome.fill.capacity_remaining}"
    )
    print(f"reason: {outcome.reason}")


def cmd_workflow_admission(args: argparse.Namespace) -> int:
    """Resolve and report the advisory Redmine-aware admission/fill preflight (#12856).

    Builds :class:`SublaneAdmissionInputs` from the supplied advisory flags, classifies
    each lane signal and evaluates the pure policy, and emits exactly one structured
    envelope: a text summary, one JSON object with ``--json``, or the Bandwidth Record
    Template markdown with ``--journal``. Always returns 0: the result is advisory and
    never blocks.
    """
    outcome = evaluate_sublane_admission(_inputs_from_args(args))
    if getattr(args, "as_journal", False):
        print(render_admission_journal(outcome))
    elif getattr(args, "as_json", False):
        print(
            _json.dumps(
                outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        _print_outcome_text(outcome)
    return 0


def register_admission(workflow_sub) -> None:
    """Register ``workflow admission`` onto the ``workflow`` subparser (#12856)."""
    admission = workflow_sub.add_parser(
        "admission",
        description=(
            "Resolve the advisory Redmine-aware sublane admission/fill preflight "
            "(Redmine #12856). Given the durable-record facts of each active lane "
            "(--lane-signal ISSUE:GATE[,conclusion=,callback=,commit=,integrated=,"
            "open=,blocker=], repeatable), the count of ready independent / overlapping "
            "implementation work, the remaining local soft-profile capacity, and "
            "whether an owner/release gate is active, it classifies each lane into a "
            "lane state class (implementing / callback_due / callback_delivery_failed / "
            "review_waiting / owner_waiting / integration_waiting / close_waiting / "
            "blocked / retire_ready / idle) and returns one admission decision "
            "(dispatch_sublane / stop_and_drain) plus the concrete fill_decision token "
            "from #12855. An active 'implementing' lane is not a stop reason. Advisory "
            "only: it discovers nothing, never selects/creates an issue or lane, and "
            "never blocks (exit 0). See "
            "vibes/docs/logics/coordinator-sublane-development-flow.md."
        ),
        help=(
            "Advisory: classify each lane from its durable-record facts, then report "
            "whether to dispatch another sublane or stop and drain (with the concrete "
            "fill reason). Discovers nothing, never blocks."
        ),
    )
    admission.add_argument(
        "--lane-signal",
        action="append",
        type=_parse_lane_signal,
        metavar="ISSUE:GATE[,key=value...]",
        help=(
            "One active lane's durable-record facts as ISSUE:GATE (repeatable). GATE is "
            "a durable gate kind (none / start / progress / implementation_done / "
            "review_request / review / owner_close_approval / close / blocked). Optional "
            "comma modifiers: conclusion=pending|approved|changes_requested (for a "
            "'review' gate), callback=none|due|delivery_failed, commit=0|1, "
            "integrated=0|1, open=0|1 (default 1), blocker=0|1. An 'implementing' lane "
            "(start/progress, or review returning changes) is not a stop reason."
        ),
    )
    admission.add_argument(
        "--ready-independent",
        dest="ready_independent",
        type=int,
        default=0,
        help="Count of ready implementation work items that do not overlap an active lane.",
    )
    admission.add_argument(
        "--ready-overlap",
        dest="ready_overlap",
        type=int,
        default=0,
        help=(
            "Count of ready implementation work items that overlap an active lane "
            "(file / invariant / merge order)."
        ),
    )
    admission.add_argument(
        "--capacity",
        dest="capacity",
        type=int,
        default=0,
        help="Remaining slots within the local soft profile for another active sublane.",
    )
    admission.add_argument(
        "--owner-or-release-gate",
        dest="owner_or_release_gate",
        action="store_true",
        help=(
            "An owner-decision / release / credential / destructive-operation gate is "
            "active (forces stop_owner_or_release_gate)."
        ),
    )
    admission.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit exactly one structured SublaneAdmissionOutcome envelope as JSON.",
    )
    admission.add_argument(
        "--journal",
        action="store_true",
        dest="as_journal",
        help=(
            "Emit the Bandwidth Record Template markdown for the Redmine dispatch-"
            "decision journal (takes precedence over --json)."
        ),
    )
    admission.set_defaults(func=cmd_workflow_admission)


__all__ = ("cmd_workflow_admission", "register_admission")
