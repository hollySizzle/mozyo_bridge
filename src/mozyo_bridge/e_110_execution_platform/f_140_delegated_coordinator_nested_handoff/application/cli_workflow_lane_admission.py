"""CLI surface for the risk-based `workflow lane-admission` decision (Redmine #12921).

`mozyo-bridge workflow lane-admission` answers, for one concrete candidate lane, whether
to dispatch it in parallel, serialize it, hold it as blocked, or escalate it to the owner
— and for which concrete engineering / workflow risk. It is the per-candidate companion to
`workflow fill-decision` (#12855, aggregate fill) and `workflow admission` (#12856,
Redmine-aware classify-then-fill).

The command makes the owner correction observable (Redmine #12670 j#69283): coordinator
convenience is not a valid lane-reduction reason. Pass only the convenience flags
(``--callback-miss-concern`` / ``--coordinator-management-load`` / ``--broad-bucket``) and
the decision is ``allow_dispatch`` with those flags named under ``rejected_nonreasons``.

It is **advisory only** (issue #12921 non-goals):

- it discovers nothing — every fact (the candidate, the active lane signals, the overlap /
  dependency / gate flags) is supplied by the caller from the durable record;
- it never selects / creates a Redmine issue and never creates / adopts a lane;
- it always returns exit code 0 — the output is informational and not meant to hard-block
  a handoff. The value is the structured ``allow_dispatch`` / ``serialize`` / ``blocked`` /
  ``needs_owner_decision`` decision plus the concrete risk reasons, ready to paste into the
  Redmine dispatch-decision journal.
"""

from __future__ import annotations

import argparse
import json as _json

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_admission import (
    _parse_lane_signal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_admission_risk import (
    LaneAdmissionInputs,
    LaneAdmissionOutcome,
    evaluate_lane_admission,
    render_lane_admission_journal,
)


def _inputs_from_args(args: argparse.Namespace) -> LaneAdmissionInputs:
    return LaneAdmissionInputs(
        candidate_issue=(getattr(args, "candidate", None) or "").strip(),
        active_lane_signals=tuple(getattr(args, "lane_signal", None) or ()),
        file_overlap_lanes=tuple(getattr(args, "file_overlap", None) or ()),
        invariant_overlap_lanes=tuple(getattr(args, "invariant_overlap", None) or ()),
        merge_order_conflict_lanes=tuple(
            getattr(args, "merge_order_conflict", None) or ()
        ),
        dependency_lanes=tuple(getattr(args, "dependency", None) or ()),
        unresolved_design_decision=bool(getattr(args, "unresolved_design", False)),
        release_publish_gate_active=bool(getattr(args, "release_publish_gate", False)),
        credential_destructive_external_gate_active=bool(
            getattr(args, "credential_destructive_external_gate", False)
        ),
        callback_miss_concern=bool(getattr(args, "callback_miss_concern", False)),
        coordinator_management_load=bool(
            getattr(args, "coordinator_management_load", False)
        ),
        broad_bucket_only=bool(getattr(args, "broad_bucket", False)),
    )


def _print_outcome_text(outcome: LaneAdmissionOutcome) -> None:
    print(f"candidate_issue: {outcome.candidate_issue}")
    print(f"admission_decision: {outcome.decision}")
    print(f"should_dispatch: {str(outcome.should_dispatch).lower()}")
    print(f"advisory: {str(outcome.advisory).lower()}")
    if outcome.classified_lanes:
        for lane in outcome.classified_lanes:
            print(f"lane: {lane.issue} -> {lane.state_class}")
    else:
        print("lane: <none>")
    if outcome.risks:
        for risk in outcome.risks:
            lanes = ", ".join(risk.lanes) if risk.lanes else "<none>"
            print(f"risk: {risk.reason} ({risk.decision}) lanes={lanes}")
    else:
        print("risk: <none>")
    rejected = ", ".join(outcome.rejected_nonreasons) or "<none>"
    print(f"rejected_nonreasons: {rejected}")
    print(f"next_safe_action: {outcome.next_safe_action}")


def cmd_workflow_lane_admission(args: argparse.Namespace) -> int:
    """Resolve and report the advisory risk-based lane admission decision (#12921).

    Builds :class:`LaneAdmissionInputs` from the supplied advisory flags, evaluates the
    pure policy, and emits exactly one envelope: a text summary, one JSON object with
    ``--json``, or the journal narrative with ``--journal``. Always returns 0: the result
    is advisory and never blocks.
    """
    outcome = evaluate_lane_admission(_inputs_from_args(args))
    if getattr(args, "as_journal", False):
        print(render_lane_admission_journal(outcome))
    elif getattr(args, "as_json", False):
        print(
            _json.dumps(
                outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        _print_outcome_text(outcome)
    return 0


def register_lane_admission(workflow_sub) -> None:
    """Register ``workflow lane-admission`` onto the ``workflow`` subparser (#12921)."""
    lane_admission = workflow_sub.add_parser(
        "lane-admission",
        description=(
            "Resolve the advisory risk-based lane admission decision for one candidate "
            "lane (Redmine #12921). Given the candidate issue, the durable-record facts "
            "of each active lane (--lane-signal ISSUE:GATE[,...], repeatable, classified "
            "as in `workflow admission`), and the concrete risk facts (file / invariant "
            "overlap, merge-order conflict, dependency on a blocked / queued lane, "
            "unresolved design, release/tag/publish gate, credential/destructive/external "
            "gate), it returns one decision: allow_dispatch / serialize / blocked / "
            "needs_owner_decision, plus the concrete risk reasons and next safe action. "
            "Coordinator convenience is NOT a valid serialization reason: "
            "--callback-miss-concern / --coordinator-management-load / --broad-bucket are "
            "recorded under rejected_nonreasons and never move the decision off "
            "allow_dispatch on their own. Advisory only: it discovers nothing, never "
            "selects/creates an issue or lane, and never blocks (exit 0). See "
            "vibes/docs/logics/coordinator-sublane-development-flow.md."
        ),
        help=(
            "Advisory: decide whether one candidate lane may be dispatched in parallel, "
            "must be serialized, is blocked, or needs an owner decision — based on "
            "concrete engineering/workflow risk, not coordinator convenience. Discovers "
            "nothing, never blocks."
        ),
    )
    lane_admission.add_argument(
        "--candidate",
        required=True,
        help="The Redmine issue id of the candidate lane being considered for dispatch.",
    )
    lane_admission.add_argument(
        "--lane-signal",
        action="append",
        type=_parse_lane_signal,
        metavar="ISSUE:GATE[,key=value...]",
        help=(
            "One active lane's durable-record facts as ISSUE:GATE (repeatable; same "
            "format as `workflow admission`). Used to classify any --dependency lane: a "
            "blocked / callback_delivery_failed (or unreadable) dependency blocks the "
            "candidate; a review/owner/integration/close/callback_due dependency "
            "serializes it."
        ),
    )
    lane_admission.add_argument(
        "--file-overlap",
        action="append",
        metavar="ISSUE",
        help="Active lane issue id whose file set overlaps the candidate (repeatable).",
    )
    lane_admission.add_argument(
        "--invariant-overlap",
        action="append",
        metavar="ISSUE",
        help=(
            "Active lane issue id whose invariant / behavioral surface overlaps the "
            "candidate (repeatable)."
        ),
    )
    lane_admission.add_argument(
        "--merge-order-conflict",
        action="append",
        metavar="ISSUE",
        help=(
            "Active lane issue id with a known merge-order conflict against the candidate "
            "(repeatable)."
        ),
    )
    lane_admission.add_argument(
        "--dependency",
        action="append",
        metavar="ISSUE",
        help=(
            "Active lane issue id whose completion / queue the candidate genuinely "
            "depends on (repeatable). Classified from its --lane-signal."
        ),
    )
    lane_admission.add_argument(
        "--unresolved-design",
        action="store_true",
        dest="unresolved_design",
        help="An unresolved design decision must be settled before dispatch.",
    )
    lane_admission.add_argument(
        "--release-publish-gate",
        action="store_true",
        dest="release_publish_gate",
        help="A release / tag / publish gate is active.",
    )
    lane_admission.add_argument(
        "--credential-destructive-external-gate",
        action="store_true",
        dest="credential_destructive_external_gate",
        help="A credential / destructive / external-operation gate is active.",
    )
    lane_admission.add_argument(
        "--callback-miss-concern",
        action="store_true",
        dest="callback_miss_concern",
        help=(
            "REJECTED non-reason (「callback を取りこぼしそう」): a speculative worry "
            "about missing a first callback. Recorded, never decisive."
        ),
    )
    lane_admission.add_argument(
        "--coordinator-management-load",
        action="store_true",
        dest="coordinator_management_load",
        help=(
            "REJECTED non-reason (「管理が大変」): coordinator callback / review "
            "management burden. Recorded, never decisive."
        ),
    )
    lane_admission.add_argument(
        "--broad-bucket",
        action="store_true",
        dest="broad_bucket",
        help=(
            "REJECTED non-reason (「broad bucket だから」): the bucket / version is broad "
            "with no concrete overlap. Recorded, never decisive."
        ),
    )
    lane_admission.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit exactly one structured LaneAdmissionOutcome envelope as JSON.",
    )
    lane_admission.add_argument(
        "--journal",
        action="store_true",
        dest="as_journal",
        help=(
            "Emit the lane-admission-decision markdown for the Redmine dispatch-decision "
            "journal (takes precedence over --json)."
        ),
    )
    lane_admission.set_defaults(func=cmd_workflow_lane_admission)


__all__ = ("cmd_workflow_lane_admission", "register_lane_admission")
