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

Redmine #13756 added the second lane form. ``--lane ISSUE:STATE`` is unchanged and
**fail-closed**: it declares no owner and no execution surface, so every blocking state
still blocks exactly as before. To tell the policy that a lane's next action is *not*
the main coordinator's — a review already delivered to a dedicated same-lane gateway, or
a lane waiting on a durable external condition — the caller must say so explicitly with
``--lane-spec``, and must present the provenance that makes the claim checkable. An
unverifiable claim is not an error: it degrades to coordinator-blocking, which is the
safe direction.
"""

from __future__ import annotations

import argparse
import json as _json

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_actionability import (
    ActionabilityClaim,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_execution_surface import (
    LaneProvenance,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    FillDecisionInputs,
    FillDecisionOutcome,
    LaneState,
    evaluate_fill_decision,
)


def _parse_lane(spec: str) -> LaneState:
    """Parse a legacy ``ISSUE:STATE`` ``--lane`` spec into a :class:`LaneState`.

    The ``STATE`` is kept literal — an unrecognized state class is not rejected here;
    the pure policy treats an unknown lane state conservatively as coordinator-blocking
    (fail toward stopping rather than over-dispatching on a misread class).

    The lane's #13756 axes are left at their fail-closed defaults (``coordinator_actionable``
    / ``main_coordinator`` / ``unspecified`` surface), so this form's behaviour is
    identical to pre-#13756.
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


# `--lane-spec` keys -> (LaneState axis, constructor field). Every key is explicit: an
# unrecognized key is rejected rather than ignored, because silently dropping (say) a
# misspelled `callback_overdue=true` would turn a stalled delegation into a healthy one.
_CLAIM_KEYS = {
    "actionability": "actionability",
    "owner": "next_action_owner",
    "delivery": "delivery_state",
    "callback_expected": "callback_expected",
    "callback_overdue": "callback_overdue",
    "unblock_condition": "unblock_condition",
}
_PROVENANCE_KEYS = {
    "surface": "execution_surface",
    "workspace": "workspace",
    "lane": "lane",
    "generation": "issue_generation",
    "revision": "lifecycle_revision",
    "anchor": "durable_anchor",
    "gateway": "gateway_identity",
    "worker": "worker_identity",
    "ack": "dispatch_ack",
}
_BOOL_KEYS = frozenset({"callback_expected", "callback_overdue"})


def _parse_bool(key: str, value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError(
        f"--lane-spec {key} expects true or false, got {value!r}"
    )


def _parse_lane_spec(spec: str) -> LaneState:
    """Parse the explicit ``key=value,...`` ``--lane-spec`` form into a :class:`LaneState`.

    ``issue`` and ``state`` are required; every other key is optional and defaults to the
    fail-closed value. Vocabulary values are **not** validated here on purpose: an
    unrecognized ``actionability`` / ``owner`` / ``surface`` / ``ack`` token reaches the
    pure policy, which fails it closed to coordinator-blocking. Rejecting it at the CLI
    would turn a safe degradation into a hard error and tempt the caller to drop the
    field entirely.
    """
    claim: dict[str, object] = {}
    provenance: dict[str, str] = {}
    issue = ""
    state = ""

    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if not sep or not key:
            raise argparse.ArgumentTypeError(
                f"--lane-spec expects key=value pairs separated by commas, got {part!r}"
            )
        if key == "issue":
            issue = value
        elif key == "state":
            state = value
        elif key in _CLAIM_KEYS:
            claim[_CLAIM_KEYS[key]] = (
                _parse_bool(key, value) if key in _BOOL_KEYS else value
            )
        elif key in _PROVENANCE_KEYS:
            provenance[_PROVENANCE_KEYS[key]] = value
        else:
            known = ", ".join(
                sorted({"issue", "state", *_CLAIM_KEYS, *_PROVENANCE_KEYS})
            )
            raise argparse.ArgumentTypeError(
                f"--lane-spec got unknown key {key!r}; known keys: {known}"
            )

    if not issue or not state:
        raise argparse.ArgumentTypeError(
            "--lane-spec requires issue=<id> and state=<lane state class>, e.g. "
            "issue=13441,state=review_waiting,actionability=delegated_in_flight,"
            "owner=dedicated_gateway,delivery=sent,callback_expected=true,"
            "surface=managed_sublane,workspace=w19,lane=issue_13441,revision=3,"
            "anchor=13441#77503,gateway=w19:p1,ack=gateway_acked"
        )

    return LaneState(
        issue=issue,
        state_class=state,
        claim=ActionabilityClaim(**claim),
        provenance=LaneProvenance(**provenance),
    )


def _inputs_from_args(args: argparse.Namespace) -> FillDecisionInputs:
    # Legacy `--lane` and explicit `--lane-spec` lanes describe one lane set together;
    # order is legacy-first, which only affects display order in the echoed lists.
    lanes = tuple(getattr(args, "lane", None) or ()) + tuple(
        getattr(args, "lane_spec", None) or ()
    )
    hard_cap = getattr(args, "sublane_hard_cap", None)
    return FillDecisionInputs(
        lanes=lanes,
        ready_independent_work=int(getattr(args, "ready_independent", 0) or 0),
        ready_overlapping_work=int(getattr(args, "ready_overlap", 0) or 0),
        capacity_remaining=int(getattr(args, "capacity", 0) or 0),
        owner_or_release_gate_active=bool(getattr(args, "owner_or_release_gate", False)),
        managed_sublane_actuation_available=not bool(
            getattr(args, "actuation_unavailable", False)
        ),
        sublane_hard_cap=None if hard_cap is None else int(hard_cap),
    )


def _print_outcome_text(outcome: FillDecisionOutcome) -> None:
    projection = outcome.capacity_projection
    print(f"fill_decision: {outcome.fill_decision}")
    print(f"advisory: {str(outcome.advisory).lower()}")
    print(f"next_drain_action: {outcome.next_drain_action}")
    print(
        "lanes: "
        f"active_implementing={list(outcome.active_implementing) or '<none>'} "
        f"coordinator_blocking={list(outcome.coordinator_blocking) or '<none>'} "
        f"delegated_in_flight={list(outcome.delegated_in_flight) or '<none>'} "
        f"non_actionable_wait={list(outcome.non_actionable_wait) or '<none>'}"
    )
    # The verified projection — the only honest source for a narrated lane count.
    print(
        "sublanes: "
        f"resident={projection.resident_managed_sublanes} "
        f"gateway_dispatched={projection.gateway_dispatched_sublanes} "
        f"worker_confirmed_productive={projection.worker_confirmed_productive_sublanes} "
        f"blocked_or_undispatched={projection.blocked_or_undispatched_sublanes}"
    )
    print(
        "non_sublane_surfaces: "
        f"internal_task_agents={projection.internal_task_agents} "
        f"unverified={projection.unverified_surface} "
        f"other={projection.other_surface}"
    )
    print(
        "capacity: "
        f"ready_independent_work={outcome.ready_independent_work} "
        f"capacity_remaining={outcome.capacity_remaining}"
    )
    # Per-lane provenance record, so the durable decision can be replayed line by line.
    for record in outcome.lanes:
        print(
            "lane: "
            f"issue={record['issue']} state={record['state_class']} "
            f"actionability={record['actionability']}({record['actionability_reason']}) "
            f"surface={record['execution_surface_resolved']} "
            f"owner={record['next_action_owner']} ack={record['dispatch_ack']} "
            f"workspace={record['workspace'] or '<none>'} "
            f"lane_label={record['lane'] or '<none>'} "
            f"generation={record['issue_generation'] or '<none>'} "
            f"revision={record['lifecycle_revision'] or '<none>'} "
            f"anchor={record['durable_anchor'] or '<none>'}"
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
            "stop_soft_profile_full / stop_owner_or_release_gate / "
            "stop_actuation_unavailable). An active 'implementing' lane is not a stop "
            "reason, and neither is a lane whose next action is verifiably owned by a "
            "dedicated gateway / worker or by a durable external condition (--lane-spec). "
            "Advisory only: it discovers nothing, never selects/creates an issue or lane, "
            "and never blocks (exit 0). See "
            "vibes/docs/logics/coordinator-sublane-development-flow.md."
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
        "--lane-spec",
        action="append",
        type=_parse_lane_spec,
        metavar="KEY=VALUE,...",
        help=(
            "An active lane in explicit form (repeatable). Requires issue=<id> and "
            "state=<lane state class>. Optional actionability axis: "
            "actionability=(coordinator_actionable|delegated_in_flight|non_actionable_wait), "
            "owner=(main_coordinator|dedicated_gateway|dedicated_worker|owner|"
            "external_condition|unknown), delivery=(not_attempted|sent|delivery_failed), "
            "callback_expected=(true|false), callback_overdue=(true|false), "
            "unblock_condition=<durable condition>. Optional execution-surface axis: "
            "surface=(managed_sublane|internal_task_agent|coordinator_local|"
            "detached_worktree), workspace=, lane=, generation=, revision=, anchor=, "
            "gateway=, worker=, ack=(none|gateway_acked|worker_confirmed). A "
            "non-blocking claim is honoured ONLY from a verified managed sublane with a "
            "confirmed delivery, a durable callback expectation, and no overdue "
            "callback; anything unverifiable fails closed to coordinator-blocking."
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
        "--sublane-hard-cap",
        dest="sublane_hard_cap",
        type=int,
        default=None,
        help=(
            "Hard cap on concurrent managed sublanes (the local soft profile's "
            "lane_count <= 10). Lowers --capacity to what the cap still allows, counting "
            "VERIFIED managed sublanes only: internal task agents neither consume the cap "
            "nor fill it."
        ),
    )
    fill.add_argument(
        "--actuation-unavailable",
        dest="actuation_unavailable",
        action="store_true",
        help=(
            "The high-level managed-sublane actuation rail is unavailable (forces the "
            "fixed stop_actuation_unavailable result). Report zero productive sublanes "
            "rather than substituting task agents / direct edits / main-lane work / bare "
            "worktrees for sublanes that cannot be opened."
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
