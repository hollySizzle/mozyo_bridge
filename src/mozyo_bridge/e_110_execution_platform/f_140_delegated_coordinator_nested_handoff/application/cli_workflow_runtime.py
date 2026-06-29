"""CLI surface for the stateful `workflow runtime` slice (Redmine #12857).

`mozyo-bridge workflow runtime` is the first command surface over the stateful workflow
runtime. Where `workflow admission` (#12856) takes the durable-record facts of a lane set
*as already-current*, `runtime` takes an ordered durable **event log** (`--event
ISSUE:GATE[,id=...,key=value...]`, repeatable), folds it with duplicate suppression into
per-lane state, and returns both the current ``workflow.state`` and the one overall
``workflow.next_action`` — the read model the spine
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` `### 設計思想`) wants every
workflow-aware command result to carry.

The command is **advisory only** (issue #12857 j#68572 first-slice boundary):

- it discovers nothing and persists nothing — every event and the ready-work / capacity /
  owner-gate inputs are supplied by the caller from the durable record (the live Redmine
  watcher is #12672; DB persistence is residual);
- it never selects / creates a Redmine issue and never creates / adopts a lane;
- it emits abstract workflow roles (auditor / implementer / coordinator / owner), never a
  runtime provider (codex / claude) — role↔provider binding is #12673;
- it always returns exit code 0 — the output is informational and never hard-blocks a
  handoff yet.

Duplicate suppression is observable: pass the same ``id=`` twice and the second event is
suppressed, so the folded state (and the next action) is unchanged — replaying the same
durable event log is idempotent.
"""

from __future__ import annotations

import argparse
import dataclasses
import json as _json

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    CALLBACK_NONE,
    CALLBACK_STATES,
    GATE_KINDS,
    REVIEW_CONCLUSIONS,
    REVIEW_PENDING,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    LaneEvent,
    WorkflowRuntimeState,
    evaluate_workflow_runtime,
    render_runtime_journal,
)


def _parse_bool(key: str, value: str) -> bool:
    """Parse a ``key=value`` boolean modifier (``0``/``1``/``true``/``false``)."""
    norm = value.strip().lower()
    if norm in ("1", "true", "yes", "y"):
        return True
    if norm in ("0", "false", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(
        f"--event {key}= expects a boolean (0/1/true/false), got {value!r}"
    )


def _parse_event(spec: str) -> LaneEvent:
    """Parse a ``ISSUE:GATE[,id=...,key=value...]`` ``--event`` spec into a LaneEvent.

    The first ``:`` separates the issue id from the rest; the rest is a comma list whose
    first element is the gate kind and whose remaining ``key=value`` elements set the
    optional event facts:

    - ``id=`` the durable anchor (journal pointer, e.g. ``12857:68572``) that makes
      replay idempotent. Omitted here, the event id is left empty and a *unique*
      synthetic id is assigned per supplied event later (:func:`_assign_event_ids`), so an
      ``id=``-less event is never falsely suppressed against another event that happens to
      share its issue / gate. Duplicate suppression therefore applies only across events
      that share an explicit ``id=`` durable anchor.
    - ``conclusion=`` pending|approved|changes_requested (only used for a ``review`` gate)
    - ``callback=`` none|due|delivery_failed
    - ``commit=`` 0|1 (commit-bearing work)
    - ``integrated=`` 0|1 (merge / push / patch-equivalent / explicit deferral recorded)
    - ``open=`` 0|1 (Redmine issue still open; default 1)
    - ``blocker=`` 0|1 (a blocker / failed handoff / unresolved dependency is recorded)

    The gate / conclusion / callback values are validated against the literal vocabularies
    so a typo is rejected at parse time rather than silently classifying to ``blocked``.
    """
    raw = (spec or "").strip()
    issue, sep, rest = raw.partition(":")
    if not sep:
        raise argparse.ArgumentTypeError(
            f"--event expects ISSUE:GATE (e.g. 12857:review_request), got {spec!r}"
        )
    issue = issue.strip()
    parts = [p.strip() for p in rest.split(",") if p.strip()]
    if not issue or not parts:
        raise argparse.ArgumentTypeError(
            f"--event expects a non-empty ISSUE and GATE, got {spec!r}"
        )
    gate = parts[0]
    if gate not in GATE_KINDS:
        raise argparse.ArgumentTypeError(
            f"--event gate must be one of {sorted(GATE_KINDS)}, got {gate!r}"
        )

    event_id = ""
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
                f"--event modifier expects key=value, got {modifier!r}"
            )
        key = key.strip()
        value = value.strip()
        if key == "id":
            if not value:
                raise argparse.ArgumentTypeError(
                    "--event id= expects a non-empty durable anchor"
                )
            event_id = value
        elif key == "conclusion":
            if value not in REVIEW_CONCLUSIONS:
                raise argparse.ArgumentTypeError(
                    f"--event conclusion= must be one of "
                    f"{sorted(REVIEW_CONCLUSIONS)}, got {value!r}"
                )
            conclusion = value
        elif key == "callback":
            if value not in CALLBACK_STATES:
                raise argparse.ArgumentTypeError(
                    f"--event callback= must be one of "
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
                f"--event unknown modifier {key!r} (expected id / conclusion / callback "
                "/ commit / integrated / open / blocker)"
            )

    # Leave the durable anchor empty when ``id=`` is omitted; a unique synthetic id is
    # assigned per supplied event by :func:`_assign_event_ids` so two distinct events that
    # share an issue / gate (e.g. a pending then an approved review) are NOT collapsed.
    return LaneEvent(
        event_id=event_id,
        issue=issue,
        gate=gate,
        review_conclusion=conclusion,
        callback_state=callback,
        commit_bearing=commit_bearing,
        integration_recorded=integration_recorded,
        issue_open=issue_open,
        blocker_recorded=blocker_recorded,
    )


def _assign_event_ids(events: tuple[LaneEvent, ...]) -> tuple[LaneEvent, ...]:
    """Give every ``id=``-less event a unique synthetic durable anchor (CLI layer).

    Events parsed with an explicit ``id=`` keep it verbatim — duplicate suppression across
    a shared explicit anchor is the intended feature. Events without one are each a
    *distinct* supplied event (we cannot know two of them are the same durable fact), so
    each gets a unique synthetic id (its supplied position) that cannot be confused with
    another event's. The synthetic id is guarded against an (unlikely) clash with an
    explicit anchor so it never accidentally suppresses, or is suppressed by, a real one.
    """
    used = {event.event_id for event in events if event.event_id}
    assigned: list[LaneEvent] = []
    for index, event in enumerate(events):
        if event.event_id:
            assigned.append(event)
            continue
        synthetic = f"#event-{index}"
        while synthetic in used:
            synthetic = f"#{synthetic}"
        used.add(synthetic)
        assigned.append(dataclasses.replace(event, event_id=synthetic))
    return tuple(assigned)


def _state_from_args(args: argparse.Namespace) -> WorkflowRuntimeState:
    events = _assign_event_ids(tuple(getattr(args, "event", None) or ()))
    return evaluate_workflow_runtime(
        events,
        ready_independent_work=int(getattr(args, "ready_independent", 0) or 0),
        ready_overlapping_work=int(getattr(args, "ready_overlap", 0) or 0),
        capacity_remaining=int(getattr(args, "capacity", 0) or 0),
        owner_or_release_gate_active=bool(getattr(args, "owner_or_release_gate", False)),
    )


def _print_state_text(state: WorkflowRuntimeState) -> None:
    nxt = state.next_action
    print(f"next_action: {nxt.action}")
    print(f"owner_role: {nxt.owner_role}")
    print(f"target_issue: {nxt.target_issue or '<none>'}")
    print(f"admission_decision: {state.admission_decision}")
    print(f"fill_decision: {state.fill_decision}")
    print(f"advisory: {str(state.advisory).lower()}")
    if state.lane_actions:
        for row in state.lane_actions:
            print(
                f"lane: {row.issue} -> {row.state_class} "
                f"=> {row.action} ({row.owner_role})"
            )
    else:
        print("lane: <none>")
    print(
        "events: "
        f"applied={list(state.applied_event_ids) or '<none>'} "
        f"suppressed={list(state.suppressed_event_ids) or '<none>'}"
    )
    print(f"reason: {nxt.reason}")


def cmd_workflow_runtime(args: argparse.Namespace) -> int:
    """Replay the supplied event log and report state + next_action (advisory; #12857).

    Builds the event log from ``--event`` specs, folds it with duplicate suppression,
    classifies the resulting lane state via the #12856 authority, and emits exactly one
    envelope: a text summary, one JSON object with ``--json`` (``workflow.state`` +
    ``workflow.next_action`` shape), or the durable record markdown with ``--journal``.
    Always returns 0: the result is advisory and never blocks.
    """
    state = _state_from_args(args)
    if getattr(args, "as_journal", False):
        print(render_runtime_journal(state))
    elif getattr(args, "as_json", False):
        print(
            _json.dumps(
                state.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        _print_state_text(state)
    return 0


def register_runtime(workflow_sub) -> None:
    """Register ``workflow runtime`` onto the ``workflow`` subparser (#12857)."""
    runtime = workflow_sub.add_parser(
        "runtime",
        description=(
            "Replay an ordered durable workflow event log into current lane state and "
            "the next action (Redmine #12857, first vertical slice). Given the durable "
            "events of each active lane (--event ISSUE:GATE[,id=,conclusion=,callback=,"
            "commit=,integrated=,open=,blocker=], repeatable, applied in order), the "
            "count of ready independent / overlapping implementation work, the remaining "
            "local soft-profile capacity, and whether an owner/release gate is active, it "
            "folds the events with duplicate suppression (same id= is suppressed so "
            "replay is idempotent), classifies each lane via the #12856 authority, and "
            "returns the workflow.state (per-lane state class + owed action + owner role) "
            "and one overall workflow.next_action (with owner role and target issue). An "
            "active 'implementing' lane is not a stop reason. Roles are abstract "
            "(auditor / implementer / coordinator / owner), never a runtime provider. "
            "Advisory only: it discovers nothing, persists nothing, never selects/creates "
            "an issue or lane, and never blocks (exit 0). See "
            "vibes/docs/logics/coordinator-sublane-development-flow.md."
        ),
        help=(
            "Advisory: replay a durable workflow event log (with duplicate suppression) "
            "into current lane state and the overall next action. Discovers nothing, "
            "never blocks."
        ),
    )
    runtime.add_argument(
        "--event",
        action="append",
        type=_parse_event,
        metavar="ISSUE:GATE[,id=...,key=value...]",
        help=(
            "One durable lane event as ISSUE:GATE (repeatable, applied in order). GATE is "
            "a durable gate kind (none / start / progress / implementation_done / "
            "review_request / review / owner_close_approval / close / blocked). Optional "
            "comma modifiers: id=<durable anchor> (e.g. 12857:68572; the same id is "
            "suppressed on replay; omit it and each supplied event is treated as a "
            "distinct event so it is never falsely suppressed), "
            "conclusion=pending|approved|changes_requested (for a 'review' gate), "
            "callback=none|due|delivery_failed, commit=0|1, integrated=0|1, open=0|1 "
            "(default 1), blocker=0|1. The last applied event per issue is its current "
            "state."
        ),
    )
    runtime.add_argument(
        "--ready-independent",
        dest="ready_independent",
        type=int,
        default=0,
        help="Count of ready implementation work items that do not overlap an active lane.",
    )
    runtime.add_argument(
        "--ready-overlap",
        dest="ready_overlap",
        type=int,
        default=0,
        help=(
            "Count of ready implementation work items that overlap an active lane "
            "(file / invariant / merge order)."
        ),
    )
    runtime.add_argument(
        "--capacity",
        dest="capacity",
        type=int,
        default=0,
        help="Remaining slots within the local soft profile for another active sublane.",
    )
    runtime.add_argument(
        "--owner-or-release-gate",
        dest="owner_or_release_gate",
        action="store_true",
        help=(
            "An owner-decision / release / credential / destructive-operation gate is "
            "active (forces the owner/release-gate stop and next action)."
        ),
    )
    runtime.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit exactly one structured WorkflowRuntimeState envelope as JSON "
            "(workflow.state + workflow.next_action)."
        ),
    )
    runtime.add_argument(
        "--journal",
        action="store_true",
        dest="as_journal",
        help=(
            "Emit the durable record markdown (Bandwidth Record Template + runtime next "
            "action) for the Redmine journal (takes precedence over --json)."
        ),
    )
    runtime.set_defaults(func=cmd_workflow_runtime)


__all__ = ("cmd_workflow_runtime", "register_runtime")
