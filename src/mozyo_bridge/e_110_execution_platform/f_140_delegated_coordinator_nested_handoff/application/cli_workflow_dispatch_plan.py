"""CLI surface for the Version-bucket `workflow dispatch-plan` command (Redmine #12920).

`mozyo-bridge workflow dispatch-plan` reads a lane *bucket* — a Redmine Version snapshot an
operator / MCP already fetched — enumerates the bucket's open leaf issues, classifies each
as a dispatch candidate (`dispatchable` / `standby` / `blocked` / `needs_owner_decision`),
and projects the coordinator-owned queue (review / owner / integration waiting) the
candidates are admitted against. It replaces the throughput-losing "pick one issue / one
lane by hand" loop with a single read-only plan.

It composes the existing authorities and adds no policy of its own: the bucket / leaf rule
is #12919's :class:`RedmineFixedVersionLaneBucketProvider`, the per-candidate decision is
#12921's :func:`evaluate_lane_admission`, and the queue projection reuses #12856's
:func:`classify_lane_state` — all in the pure :func:`build_dispatch_plan` (#12920).

It is **read-only and advisory** (issue #12920 non-goals):

- it discovers nothing — the bucket snapshot (``--issues-json`` / ``--versions-json``), the
  active lane signals (``--lane-signal``), and the per-candidate risk facts
  (``--candidate-facts``) are all supplied by the caller; the command performs no network
  call and no Redmine read;
- it never mutates Redmine / tmux / worktree and never sends a handoff. ``--mode`` records
  dispatch intent (``dry-run`` default / ``execute``) but both modes are identical and
  side-effect-free: the plan only *emits* the governed route (coordinator Codex -> sublane
  Codex gateway -> same-lane Claude) a coordinator would then run through the existing,
  #12918-route-gated ``handoff send`` primitive. Unattended / automatic dispatch is an
  explicit non-goal, so this surface never auto-sends;
- it always returns exit code 0 — the output is the structured plan, ready to paste into
  the Redmine dispatch-decision journal.
"""

from __future__ import annotations

import argparse
import json as _json
from pathlib import Path
from typing import Mapping

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_admission import (
    _parse_lane_signal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_set_dispatch_plan import (
    MODE_DRY_RUN,
    MODE_EXECUTE,
    RECOMMENDED_ROUTE,
    CandidateDispatchFacts,
    LaneSetDispatchPlan,
    build_dispatch_plan,
    render_dispatch_plan_journal,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.fixed_version_lane_bucket_provider import (
    RedmineFixedVersionLaneBucketProvider,
)

# ``--mode`` accepts the hyphenated UI spelling; the plan vocabulary is the literal token.
_MODE_BY_FLAG = {"dry-run": MODE_DRY_RUN, "execute": MODE_EXECUTE}


def _load_json(path_text: str, flag: str) -> object:
    """Load a JSON snapshot file for a ``--*-json`` flag, failing closed on a bad path."""
    raw = (path_text or "").strip()
    try:
        return _json.loads(Path(raw).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"{flag} {raw!r}: file not found") from exc
    except OSError as exc:
        raise SystemExit(f"{flag} {raw!r}: {exc}") from exc
    except _json.JSONDecodeError as exc:
        raise SystemExit(f"{flag} {raw!r}: invalid JSON ({exc})") from exc


def _str_tuple(value: object) -> tuple[str, ...]:
    """Coerce a JSON list of ids into a string tuple (a bare scalar becomes one element)."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value).strip()
    return (text,) if text else ()


def _facts_from_mapping(payload: Mapping[str, object]) -> CandidateDispatchFacts:
    """Build one :class:`CandidateDispatchFacts` from a ``--candidate-facts`` entry."""
    return CandidateDispatchFacts(
        expected_changed_surface=str(payload.get("expected_changed_surface", "") or ""),
        file_overlap_lanes=_str_tuple(payload.get("file_overlap")),
        invariant_overlap_lanes=_str_tuple(payload.get("invariant_overlap")),
        merge_order_conflict_lanes=_str_tuple(payload.get("merge_order_conflict")),
        dependency_lanes=_str_tuple(payload.get("dependency")),
        unresolved_design_decision=bool(payload.get("unresolved_design", False)),
        release_publish_gate_active=bool(payload.get("release_publish_gate", False)),
        credential_destructive_external_gate_active=bool(
            payload.get("credential_destructive_external_gate", False)
        ),
        callback_miss_concern=bool(payload.get("callback_miss_concern", False)),
        coordinator_management_load=bool(payload.get("coordinator_management_load", False)),
        broad_bucket_only=bool(payload.get("broad_bucket", False)),
    )


def _candidate_facts_from_args(
    args: argparse.Namespace,
) -> dict[str, CandidateDispatchFacts]:
    """Read the optional per-candidate facts JSON into an issue-id -> facts map.

    The file is a JSON object keyed by issue id, each value an object with the optional
    risk fields (``expected_changed_surface``, ``file_overlap`` / ``invariant_overlap`` /
    ``merge_order_conflict`` / ``dependency`` lists, and the owner-gate /
    coordinator-convenience booleans). Absent the flag, every candidate has no facts (so
    no concrete risk fires and it is dispatchable).
    """
    raw = (getattr(args, "candidate_facts", None) or "").strip()
    if not raw:
        return {}
    payload = _load_json(raw, "--candidate-facts")
    if not isinstance(payload, Mapping):
        raise SystemExit(
            f"--candidate-facts {raw!r} must contain a JSON object keyed by issue id, "
            f"not a {type(payload).__name__}"
        )
    facts: dict[str, CandidateDispatchFacts] = {}
    for issue_id, entry in payload.items():
        if not isinstance(entry, Mapping):
            raise SystemExit(
                f"--candidate-facts entry for {issue_id!r} must be an object, "
                f"not a {type(entry).__name__}"
            )
        facts[str(issue_id).strip()] = _facts_from_mapping(entry)
    return facts


def _build_plan(args: argparse.Namespace) -> LaneSetDispatchPlan:
    issues_payload = _load_json(getattr(args, "issues_json", ""), "--issues-json")
    versions_text = (getattr(args, "versions_json", None) or "").strip()
    versions_payload = (
        _load_json(versions_text, "--versions-json") if versions_text else None
    )
    provider = RedmineFixedVersionLaneBucketProvider(
        issues_payload=issues_payload,
        versions_payload=versions_payload,
    )
    resolution = provider.resolve_bucket((getattr(args, "bucket_id", None) or "").strip())
    mode = _MODE_BY_FLAG[getattr(args, "mode", "dry-run")]
    return build_dispatch_plan(
        resolution,
        active_lane_signals=tuple(getattr(args, "lane_signal", None) or ()),
        candidate_facts=_candidate_facts_from_args(args),
        mode=mode,
    )


def _print_plan_text(plan: LaneSetDispatchPlan) -> None:
    print(f"bucket_id: {plan.bucket_id}")
    print(f"bucket_name: {plan.bucket_name or '<none>'}")
    print(f"resolved: {str(plan.resolved).lower()}")
    print(f"mode: {plan.mode}")
    print(f"recommended_route: {RECOMMENDED_ROUTE}")
    if not plan.resolved:
        skip = plan.bucket_skip or {}
        print(
            f"bucket_skip: {skip.get('reason', 'unknown')} "
            f"({skip.get('detail', '') or 'no detail'})"
        )
        return
    queue = plan.queue_state
    print(
        "queue: "
        f"active={queue.total_active} "
        f"review_waiting={list(queue.review_waiting) or '<none>'} "
        f"owner_waiting={list(queue.owner_waiting) or '<none>'} "
        f"integration_waiting={list(queue.integration_waiting) or '<none>'}"
    )
    counts = plan.counts_by_classification
    print(
        "counts: "
        f"dispatchable={counts['dispatchable']} standby={counts['standby']} "
        f"blocked={counts['blocked']} needs_owner_decision={counts['needs_owner_decision']}"
    )
    if plan.candidates:
        for candidate in plan.candidates:
            skip = candidate.skip_reason or "<none>"
            print(
                f"candidate: {candidate.issue_id} ({candidate.tracker or 'unknown'}, "
                f"parent={candidate.parent_id or 'none'}) -> {candidate.classification}; "
                f"skip_reason={skip}; surface={candidate.expected_changed_surface or '<none>'}"
            )
    else:
        print("candidate: <none>")
    if plan.skipped_issues:
        for skipped in plan.skipped_issues:
            print(f"skipped: {skipped.issue_id} -> {skipped.reason}")


def cmd_workflow_dispatch_plan(args: argparse.Namespace) -> int:
    """Build and report the read-only lane-set dispatch plan for a bucket (#12920).

    Reads the supplied Redmine snapshot, resolves the bucket via the #12919 provider,
    builds the plan via the pure :func:`build_dispatch_plan`, and emits exactly one
    envelope: a text summary, one JSON object with ``--json``, or the journal narrative
    with ``--journal``. Always returns 0: the result is advisory and never mutates or
    dispatches. ``--mode execute`` records intent only — it still emits the governed route
    rather than auto-sending (a #12920 non-goal).
    """
    plan = _build_plan(args)
    if getattr(args, "as_journal", False):
        print(render_dispatch_plan_journal(plan))
    elif getattr(args, "as_json", False):
        print(
            _json.dumps(plan.as_payload(), ensure_ascii=False, indent=2, sort_keys=True)
        )
    else:
        _print_plan_text(plan)
    return 0


def register_dispatch_plan(workflow_sub) -> None:
    """Register ``workflow dispatch-plan`` onto the ``workflow`` subparser (#12920)."""
    dispatch_plan = workflow_sub.add_parser(
        "dispatch-plan",
        description=(
            "Generate the read-only lane-set dispatch plan for a Redmine Version bucket "
            "(Redmine #12920). Reads a supplied issues snapshot (--issues-json, optionally "
            "--versions-json), resolves the bucket via the #12919 fixed_version provider, "
            "enumerates its open leaf issues, and classifies each candidate as "
            "dispatchable / standby / blocked / needs_owner_decision via the #12921 "
            "risk-based admission policy against the active lanes (--lane-signal "
            "ISSUE:GATE[,...], repeatable, same format as `workflow admission`) and the "
            "optional per-candidate risk facts (--candidate-facts JSON). Each candidate "
            "carries its issue id / tracker / parent / bucket / expected changed surface / "
            "skip reason / recommended route. It also projects the coordinator-owned queue "
            "(review / owner / integration waiting) the candidates are admitted against. "
            "Read-only and advisory: it discovers nothing, never selects/creates an issue "
            "or lane, never mutates Redmine/tmux/worktree, and never auto-dispatches "
            "(--mode execute records intent only and still emits the governed coordinator "
            "Codex -> sublane Codex gateway -> same-lane Claude route for manual handoff). "
            "Always exit 0. See vibes/docs/logics/coordinator-sublane-development-flow.md."
        ),
        help=(
            "Read-only: from a Redmine Version bucket snapshot, enumerate open leaf "
            "candidates and classify each as dispatchable / standby / blocked / "
            "needs_owner_decision, with the coordinator-owned queue state. Discovers "
            "nothing, never mutates, never auto-dispatches."
        ),
    )
    dispatch_plan.add_argument(
        "--bucket-id",
        required=True,
        dest="bucket_id",
        metavar="VERSION_ID",
        help="The Redmine Version id whose bucket to plan (the fixed_version id).",
    )
    dispatch_plan.add_argument(
        "--issues-json",
        required=True,
        dest="issues_json",
        metavar="PATH",
        help=(
            "Path to the fetched issues snapshot "
            "(/issues.json?fixed_version_id=<id>&status_id=* or an MCP/export wrapper). "
            "Read only; no network call is made."
        ),
    )
    dispatch_plan.add_argument(
        "--versions-json",
        dest="versions_json",
        metavar="PATH",
        help=(
            "Optional path to the fetched versions snapshot (/versions.json). When absent, "
            "Version status / name / dates are derived from the issues' embedded "
            "fixed_version and a closed/locked Version cannot be detected."
        ),
    )
    dispatch_plan.add_argument(
        "--lane-signal",
        action="append",
        type=_parse_lane_signal,
        metavar="ISSUE:GATE[,key=value...]",
        help=(
            "One active lane's durable-record facts as ISSUE:GATE (repeatable; same format "
            "as `workflow admission`). Classified for the coordinator-owned queue "
            "projection and as the active lane set every candidate is admitted against."
        ),
    )
    dispatch_plan.add_argument(
        "--candidate-facts",
        dest="candidate_facts",
        metavar="PATH",
        help=(
            "Optional path to a JSON object keyed by candidate issue id, each value an "
            "object with the per-candidate risk facts (expected_changed_surface; "
            "file_overlap / invariant_overlap / merge_order_conflict / dependency lists; "
            "unresolved_design / release_publish_gate / credential_destructive_external_"
            "gate / callback_miss_concern / coordinator_management_load / broad_bucket "
            "booleans). A candidate with no entry has no concrete risk (dispatchable)."
        ),
    )
    dispatch_plan.add_argument(
        "--mode",
        choices=("dry-run", "execute"),
        default="dry-run",
        help=(
            "Dispatch intent. Both modes are read-only and identical in effect: 'execute' "
            "records intent only and still emits the governed route rather than "
            "auto-dispatching (unattended dispatch is a #12920 non-goal)."
        ),
    )
    dispatch_plan.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit exactly one structured LaneSetDispatchPlan envelope as JSON.",
    )
    dispatch_plan.add_argument(
        "--journal",
        action="store_true",
        dest="as_journal",
        help=(
            "Emit the lane-set dispatch-plan markdown for the Redmine dispatch-decision "
            "journal (takes precedence over --json)."
        ),
    )
    dispatch_plan.set_defaults(func=cmd_workflow_dispatch_plan)


__all__ = ("cmd_workflow_dispatch_plan", "register_dispatch_plan")
