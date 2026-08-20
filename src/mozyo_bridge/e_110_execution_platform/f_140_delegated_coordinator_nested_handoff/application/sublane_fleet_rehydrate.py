"""``sublane rehydrate-fleet``: the governed post-restart fleet rehydrate rail (Redmine #15745).

A host restart expires every pane's terminal attestation. ``herdr session-start`` restores
the default coordinator pair and nothing else, so re-forming the three-tier fleet was a
manual chain of otherwise-governed rails (#15631 j#108474 / j#108484 recorded the measured
sequence). This surface is the missing decision layer over those rails: from an attested
coordinator it reads what the manifest calls active, joins the durable authorities, and says
per lane which undelivered action — pair heal, dispatch restore, delegated-coordinator
resume brief — that lane owes.

Two stages, exactly as the issue's acceptance 1 requires:

- **plan (default, read-only)** — enumerates every lane of this repo's workspace with its
  exact issue / lane / generation / branch / worktree binding, the actions it owes, and the
  typed skip / block reason for the rest. It opens no transaction, reserves no fence, sends
  nothing, and writes no row; a regression pins that effect budget at zero.
- **``--execute``** — re-reads each lane's identity fresh at action time and composes the
  EXISTING primitives (:class:`...sublane_actuator_use_case.SublaneActuateUseCase` for the
  adopt-or-launch heal; the canonical handoff rail for a restore / brief). It introduces no
  raw Herdr / tmux call, no environment injection, no provider-UI answer, no direct store
  write, no worktree clobber and no branch rewrite.

This module owns the live fact join, the rendering, and the thin CLI handler. The pure
decision is :func:`...domain.fleet_rehydrate.plan_lane_rehydrate`; the actuation port and its
live adapter are :mod:`.sublane_fleet_rehydrate_ops`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mozyo_bridge.core.state.lane_lifecycle_model import LaneLifecycleRecord

from mozyo_bridge.core.state.lane_kind import LANE_KIND_DELEGATED_COORDINATOR
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate import (  # noqa: E501
    ACTION_HEAL_PAIR,
    ACTION_RESTORE_DISPATCH,
    ACTION_RESUME_BRIEF,
    DELEGATED_COORDINATOR_BRIEF_FIELDS,
    DISPATCH_NOT_APPLICABLE,
    FleetLaneFacts,
    FleetLanePlan,
    SKIP,
    SKIP_FILTERED,
    plan_lane_rehydrate,
    summarize_rehydrate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.fleet_rehydrate_dispatch_fold import (  # noqa: E501
    KIND_IMPLEMENTATION_REQUEST,
    KIND_REPLY,
    dispatch_fact,
    latest_anchor_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.reconcile_state_machine import (  # noqa: E501
    COORDINATOR_ROUTE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_reboot_audit import (  # noqa: E501
    RebootAuditUnavailable,
    gather_reboot_facts,
)


class FleetRehydrateUnavailable(RuntimeError):
    """An AUTHORITY could not be read, so no plan exists (Redmine #15745).

    Distinct from "this repo owns no lanes", which is a legitimate empty plan. An
    unresolvable workspace identity or an unreadable lifecycle store means the rail does not
    know what exists — and a fleet rail that cannot see is not one that found nothing. The
    command surfaces this as a non-zero exit so a caller can never read it as "rehydrated,
    nothing to do". Per-lane unreadability is a typed ``blocked`` lane instead, because there
    the rail DOES know the lane exists.
    """


#: The callback route token a lane whose parent is the workspace coordinator briefs back to.
#: Re-exported from the reconcile state machine so this rail names the same route the
#: callback rails do rather than minting a second spelling.
PARENT_ROUTE_COORDINATOR = COORDINATOR_ROUTE


def parent_callback_route(parent_lane_id: str) -> str:
    """The durable callback route a delegated-coordinator lane briefs back to (pure).

    A lane created by the default-lane coordinator carries an empty ``parent_lane_id`` (the
    v12 column's documented "no delegated parent" fact), and its parent IS the workspace
    coordinator, so the stable :data:`PARENT_ROUTE_COORDINATOR` token is the route — not a
    guess but the same token the callback outbox resolves. A lane created UNDER another lane
    names that lane's gateway route explicitly.
    """
    parent = (parent_lane_id or "").strip()
    return f"lane_gateway:{parent}" if parent else PARENT_ROUTE_COORDINATOR


@dataclass(frozen=True)
class ResumeBriefInput:
    """The coordinator-supplied half of a delegated-coordinator resume brief (#15745 acc 4).

    Two of the fixed role profile's four placeholders are durable facts this rail resolves
    itself (``parent_issue`` from the parent lane's lifecycle row, ``parent_callback_target``
    from :func:`parent_callback_route`). The remaining two — which parent and child *project*
    the delegation spans — are a governance assertion no per-lane store holds, so they are
    supplied here or the lane blocks. They are never inferred from a lane label, a worktree
    name, or a pane.

    ``anchor_journal`` is the lane's CURRENT durable resume anchor. It is what makes the
    brief's causal key move between restarts: a fresh anchor is a fresh key (so the brief is
    owed), and re-running the rail against the SAME anchor re-uses a delivered key (so it is
    not). Empty means "resolve from the lifecycle row's decision anchor".
    """

    anchor_journal: str = ""
    fields: tuple[tuple[str, str], ...] = ()

    def field_map(self) -> dict[str, str]:
        return {k: v for k, v in self.fields}


def parse_resume_anchor(value: str) -> tuple[str, str]:
    """Parse a ``--resume-anchor LANE=JOURNAL`` pair, failing closed on a malformed one."""
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            f"--resume-anchor must be LANE=JOURNAL; got {value!r}"
        )
    lane, journal = value.split("=", 1)
    lane, journal = lane.strip(), journal.strip()
    if not lane or not journal:
        raise argparse.ArgumentTypeError(
            f"--resume-anchor must be LANE=JOURNAL with both parts non-empty; got {value!r}"
        )
    return lane, journal


def parse_resume_profile_field(value: str) -> tuple[str, str, str]:
    """Parse a ``--resume-profile-field LANE:KEY=VALUE`` triple, failing closed."""
    if ":" not in value or "=" not in value:
        raise argparse.ArgumentTypeError(
            f"--resume-profile-field must be LANE:KEY=VALUE; got {value!r}"
        )
    lane, rest = value.split(":", 1)
    key, field_value = rest.split("=", 1)
    lane, key, field_value = lane.strip(), key.strip(), field_value.strip()
    if not lane or not key or not field_value:
        raise argparse.ArgumentTypeError(
            "--resume-profile-field must be LANE:KEY=VALUE with every part non-empty; "
            f"got {value!r}"
        )
    if key not in DELEGATED_COORDINATOR_BRIEF_FIELDS:
        raise argparse.ArgumentTypeError(
            f"--resume-profile-field key must be one of "
            f"{list(DELEGATED_COORDINATOR_BRIEF_FIELDS)}; got {key!r}"
        )
    return lane, key, field_value


def resume_inputs_from_args(args: argparse.Namespace) -> dict[str, ResumeBriefInput]:
    """Fold the repeatable resume flags into one per-lane input map."""
    anchors: dict[str, str] = {}
    for lane, journal in getattr(args, "resume_anchor", None) or ():
        anchors[lane] = journal
    fields: dict[str, dict[str, str]] = {}
    for lane, key, value in getattr(args, "resume_profile_field", None) or ():
        fields.setdefault(lane, {})[key] = value
    lanes = set(anchors) | set(fields)
    return {
        lane: ResumeBriefInput(
            anchor_journal=anchors.get(lane, ""),
            fields=tuple(sorted(fields.get(lane, {}).items())),
        )
        for lane in lanes
    }


def _ledger_records(issue: str, *, home: Optional[Path]) -> tuple[Optional[list], str]:
    """Strictly read the durable delivery record for one issue.

    Returns ``(records, detail)``; ``records is None`` means the authority could not be read
    (which the planner turns into a typed block), never that nothing was delivered.
    """
    from mozyo_bridge.core.state.herdr_delivery_ledger import (
        HerdrDeliveryLedger,
        HerdrDeliveryLedgerError,
    )

    try:
        return HerdrDeliveryLedger(home=home).records_for_issue_strict(issue), ""
    except (HerdrDeliveryLedgerError, OSError) as exc:
        return None, f"delivery ledger unreadable ({type(exc).__name__})"


def _lifecycle_anchor(record: "LaneLifecycleRecord") -> str:
    """The lane's own durable decision journal, when it belongs to the lane's issue.

    A lane's ``decision_*`` triple names the record that put it in its current state. It is
    usable as a send anchor only when it is an anchor *for this lane's issue*: a disposition
    CAS taken under a different issue's journal is a valid decision record but not a valid
    implementation-request anchor, and re-issuing under it would be a different send wearing
    this lane's name.
    """
    if (record.decision_source or "").strip() != "redmine":
        return ""
    if (record.decision_issue_id or "").strip() != (record.issue_id or "").strip():
        return ""
    return (record.decision_journal or "").strip()


def _resume_profile_fields(
    record: "LaneLifecycleRecord",
    *,
    supplied: ResumeBriefInput,
    parent_issue: str,
) -> tuple[tuple[str, str], ...]:
    """Resolve the four fixed delegated-coordinator placeholders (durable + supplied).

    Explicit values always win; the two derivable placeholders fall back to their durable
    source; anything still unresolved is carried as an EMPTY value so the planner blocks on
    it by name rather than shipping a half-substituted delegation contract.
    """
    explicit = supplied.field_map()
    resolved = {
        "parent_issue": explicit.get("parent_issue", "") or parent_issue,
        "parent_callback_target": explicit.get("parent_callback_target", "")
        or parent_callback_route(record.parent_lane_id),
        "parent_project": explicit.get("parent_project", ""),
        "child_project": explicit.get("child_project", ""),
    }
    return tuple((name, resolved.get(name, "")) for name in DELEGATED_COORDINATOR_BRIEF_FIELDS)


def gather_fleet_facts(
    repo_root: Path,
    *,
    home: Optional[Path] = None,
    integration_branch: str = "",
    resume_inputs: Optional[Mapping[str, ResumeBriefInput]] = None,
    lifecycle_rows: Optional[Sequence["LaneLifecycleRecord"]] = None,
    rows: Optional[Sequence[Mapping[str, object]]] = None,
    issue_states: Optional[Mapping[str, Optional[bool]]] = None,
    ledger_by_issue: Optional[Mapping[str, Optional[Sequence[Any]]]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[FleetLaneFacts, ...]:
    """Join every authority the rehydrate decision reads, one :class:`FleetLaneFacts` per lane.

    The four-authority join (Redmine open/closed, git, the lifecycle row, the live
    assigned-name inventory) is delegated verbatim to :func:`gather_reboot_facts` so this
    rail describes exactly the lanes and exactly the slot scoping ``sublane reboot-audit``
    does. What is added here is the axes the rehydrate decision needs and a convergence audit
    does not: delegation geometry, the expected managed roles, and the durable delivery fold
    of each causal key the lane could owe.

    Every collaborator is injectable so the join can be exercised without a network, a live
    herdr, or the shared home.
    """
    from mozyo_bridge.core.state.lane_lifecycle_readonly import (
        load_lane_lifecycle_readonly,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
        repo_scope_workspace_id,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        WorkflowProviderUnresolved,
        resolve_gateway_provider,
        resolve_worker_provider,
    )

    environ = environ if environ is not None else os.environ
    resume_inputs = dict(resume_inputs or {})
    workspace_id = repo_scope_workspace_id(repo_root, home=home)
    if not workspace_id:
        raise FleetRehydrateUnavailable(
            "the repo's workspace identity could not be resolved, so the lanes this repo "
            "owns cannot be determined. This is an unreadable authority, not an empty one"
        )
    records = (
        tuple(lifecycle_rows)
        if lifecycle_rows is not None
        else load_lane_lifecycle_readonly(home=home)
    )
    if records is None:
        raise FleetRehydrateUnavailable(
            "the lane lifecycle store could not be read (unreadable, or a newer / malformed "
            "component schema). No fleet plan can be produced; this is NOT the same as the "
            "store having no rows"
        )
    try:
        # The SAME four-authority join `sublane reboot-audit` runs, over the SAME rows, so a
        # lane cannot be described one way by the audit and another by the rehydrate.
        reboot_facts = gather_reboot_facts(
            repo_root,
            home=home,
            integration_branch=integration_branch,
            issue_states=issue_states,
            lifecycle_rows=records,
            rows=rows,
            environ=environ,
        )
    except RebootAuditUnavailable as exc:
        raise FleetRehydrateUnavailable(str(exc)) from exc

    by_lane = {
        (r.repo_workspace_id, r.lane_id): r
        for r in records
        if r.repo_workspace_id == workspace_id
    }
    try:
        managed_roles = (
            resolve_gateway_provider(str(repo_root)),
            resolve_worker_provider(str(repo_root)),
        )
        gateway_receiver = managed_roles[0]
    except WorkflowProviderUnresolved:
        # An unresolvable binding is a per-lane block (`inventory_unreadable` with the
        # role detail), not a whole-rail failure: the rail still knows which lanes exist.
        managed_roles = ()
        gateway_receiver = ""

    ledger_cache: dict[str, tuple[Optional[list], str]] = {}

    def _records_for(issue: str) -> tuple[Optional[Sequence[Any]], str]:
        if ledger_by_issue is not None:
            if issue in ledger_by_issue:
                supplied = ledger_by_issue[issue]
                return supplied, (
                    "" if supplied is not None else "delivery ledger unreadable"
                )
            return (), ""
        if issue not in ledger_cache:
            ledger_cache[issue] = _ledger_records(issue, home=home)
        return ledger_cache[issue]

    facts: list[FleetLaneFacts] = []
    for reboot in reboot_facts:
        record = by_lane.get((reboot.workspace_id, reboot.lane_id))
        if record is None:  # pragma: no cover - the two reads share one row set
            continue
        issue = (reboot.issue_id or "").strip()
        ledger, ledger_detail = _records_for(issue) if issue else ((), "")
        unreadable = ledger is None

        dispatch_anchor = ""
        if issue and not unreadable and gateway_receiver:
            dispatch_anchor = latest_anchor_journal(
                ledger or (),
                issue=issue,
                kind=KIND_IMPLEMENTATION_REQUEST,
                receiver=gateway_receiver,
            )
        if not dispatch_anchor:
            dispatch_anchor = _lifecycle_anchor(record)
        dispatch = dispatch_fact(
            ledger,
            issue=issue,
            journal=dispatch_anchor,
            kind=KIND_IMPLEMENTATION_REQUEST,
            receiver=gateway_receiver,
            unreadable=unreadable,
            detail=ledger_detail,
        )

        supplied = resume_inputs.get(reboot.lane_id, ResumeBriefInput())
        brief = dispatch_fact(
            None if unreadable else (ledger or ()),
            issue=issue,
            journal=(supplied.anchor_journal or _lifecycle_anchor(record)),
            kind=KIND_REPLY,
            receiver=gateway_receiver,
            unreadable=unreadable,
            detail=ledger_detail,
        )
        if record.lane_kind != LANE_KIND_DELEGATED_COORDINATOR:
            # A non-delegated lane owes no brief BY CONSTRUCTION. Reporting its key as
            # `owed` would make an absent obligation read as a pending one.
            brief = dispatch_fact(
                (),
                issue=issue,
                journal="",
                kind=KIND_REPLY,
                receiver=gateway_receiver,
            )
            profile_fields: tuple[tuple[str, str], ...] = ()
        else:
            parent = by_lane.get((reboot.workspace_id, record.parent_lane_id))
            profile_fields = _resume_profile_fields(
                record,
                supplied=supplied,
                parent_issue=(parent.issue_id if parent is not None else ""),
            )

        facts.append(
            FleetLaneFacts(
                reboot=reboot,
                lane_kind=record.lane_kind,
                parent_lane_id=record.parent_lane_id,
                replacement_state=record.replacement_state,
                managed_roles=managed_roles,
                dispatch=dispatch,
                resume_brief=brief,
                resume_profile_fields=profile_fields,
                startup_interaction_pending=False,
            )
        )
    return tuple(facts)


def plan_fleet(
    facts: Sequence[FleetLaneFacts], *, lane_filter: str = ""
) -> tuple[FleetLanePlan, ...]:
    """The per-lane plans, honouring an optional lane filter as a TYPED skip.

    A filtered-out lane is reported as :data:`SKIP_FILTERED` rather than dropped, so the
    plan's lane set always equals the manifest's lane set for this workspace and a reader
    can never mistake "excluded by the caller" for "the manifest does not have it".
    """
    only = (lane_filter or "").strip()
    plans = []
    for fact in facts:
        if only and fact.lane_id != only:
            plans.append(
                FleetLanePlan(
                    workspace_id=fact.workspace_id,
                    lane_id=fact.lane_id,
                    issue_id=fact.issue_id,
                    disposition=SKIP,
                    reason=SKIP_FILTERED,
                    lane_kind=fact.lane_kind,
                    lane_generation=fact.lane_generation,
                    revision=fact.revision,
                    dispatch_state=DISPATCH_NOT_APPLICABLE,
                    resume_brief_state=DISPATCH_NOT_APPLICABLE,
                )
            )
            continue
        plans.append(plan_lane_rehydrate(fact))
    return tuple(plans)


# ---------------------------------------------------------------------------
# Rendering (pure).
# ---------------------------------------------------------------------------


def rehydrate_payload(
    facts: Sequence[FleetLaneFacts],
    plans: Sequence[FleetLanePlan],
    *,
    execute: bool,
    outcomes: Sequence[Mapping[str, object]] = (),
) -> dict:
    """The structured plan / result envelope (path-free, pasteable into a journal)."""
    by_lane = {str(o.get("lane_id", "")): o for o in outcomes}
    return {
        "state": "executed" if execute else "plan",
        "execute": execute,
        "lanes": [
            {
                "facts": f.as_payload(),
                "plan": p.as_payload(),
                **({"outcome": by_lane[p.lane_id]} if p.lane_id in by_lane else {}),
            }
            for f, p in zip(facts, plans)
        ],
        "summary": summarize_rehydrate(plans),
    }


_ACTION_LABEL = {
    ACTION_HEAL_PAIR: "adopt-or-launch the gateway/worker pair",
    ACTION_RESTORE_DISPATCH: "re-issue the anchored implementation_request",
    ACTION_RESUME_BRIEF: "re-deliver the delegated-coordinator resume brief",
}


def format_rehydrate_text(
    facts: Sequence[FleetLaneFacts],
    plans: Sequence[FleetLanePlan],
    *,
    execute: bool,
    outcomes: Sequence[Mapping[str, object]] = (),
) -> str:
    header = "sublane rehydrate-fleet" + ("" if execute else " (plan, read-only)")
    if not plans:
        return f"{header}: no lane rows for this repo workspace"
    by_lane = {str(o.get("lane_id", "")): o for o in outcomes}
    lines = [f"{header}: {len(plans)} lane(s)"]
    for fact, plan in zip(facts, plans):
        lines.append(
            f"  {plan.lane_id} issue={plan.issue_id or '-'} "
            f"gen={plan.lane_generation} rev={plan.revision} "
            f"kind={plan.lane_kind or '-'} -> {plan.disposition}"
            + (f" ({plan.reason})" if plan.reason else "")
        )
        lines.append(
            f"    branch={fact.reboot.branch or '-'} "
            f"pair_whole={str(plan.pair_whole).lower()} "
            f"live_roles={','.join(plan.live_roles) or '-'}"
        )
        lines.append(
            f"    dispatch={plan.dispatch_state}"
            + (f"@j#{plan.dispatch_anchor_journal}" if plan.dispatch_anchor_journal else "")
            + f" resume_brief={plan.resume_brief_state}"
            + (f"@j#{plan.resume_anchor_journal}" if plan.resume_anchor_journal else "")
        )
        for action in plan.actions:
            lines.append(f"    + {action}: {_ACTION_LABEL.get(action, action)}")
        if plan.detail:
            lines.append(f"    detail: {plan.detail}")
        outcome = by_lane.get(plan.lane_id)
        if outcome is not None:
            lines.append(
                f"    outcome: {outcome.get('status')} "
                f"applied={','.join(outcome.get('applied') or ()) or '-'} "
                f"reason={outcome.get('reason') or '-'}"
            )
    lines.append("  summary: " + json.dumps(summarize_rehydrate(plans), sort_keys=True))
    if not execute:
        lines.append(
            "  note: read-only. No worktree, branch, process, store, ticket or send was "
            "touched. Re-run with --execute to actuate exactly the actions listed above."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Thin CLI handler.
# ---------------------------------------------------------------------------


def cmd_sublane_rehydrate_fleet(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo", None)
    repo_root = Path(repo).expanduser() if repo else Path.cwd()
    json_mode = bool(getattr(args, "json", False))
    execute = bool(getattr(args, "execute", False))
    try:
        facts = gather_fleet_facts(
            repo_root,
            integration_branch=getattr(args, "integration_branch", "") or "",
            resume_inputs=resume_inputs_from_args(args),
        )
    except FleetRehydrateUnavailable as exc:
        payload = {
            "state": "unavailable",
            "execute": execute,
            "detail": str(exc),
            "lanes": [],
            "summary": summarize_rehydrate(()),
        }
        if json_mode:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                f"sublane rehydrate-fleet: unavailable\n  detail: {exc}", file=sys.stderr
            )
        return 1
    plans = plan_fleet(facts, lane_filter=getattr(args, "lane_label", "") or "")

    outcomes: tuple[Mapping[str, object], ...] = ()
    if execute:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_fleet_rehydrate_ops import (  # noqa: E501
            FleetRehydrateUseCase,
            LiveFleetRehydrateOps,
        )

        use_case = FleetRehydrateUseCase(
            LiveFleetRehydrateOps(repo_root=repo_root, quiet_stdout=json_mode)
        )
        outcomes = use_case.run(facts, plans)

    if json_mode:
        print(
            json.dumps(
                rehydrate_payload(facts, plans, execute=execute, outcomes=outcomes),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            format_rehydrate_text(facts, plans, execute=execute, outcomes=outcomes),
            file=sys.stdout,
        )
    if execute:
        # A lane that could not be actuated is a command failure on the execute leg: the
        # caller asked for the fleet to be rehydrated and part of it was not.
        return 1 if any(o.get("status") == "blocked" for o in outcomes) else 0
    # Read-only: a lane needing work — or one that is blocked — is a FINDING, not a command
    # failure. A non-zero exit here would make the plan unusable in the loop that consumes it.
    return 0


def register_sublane_rehydrate_fleet_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "rehydrate-fleet",
        help=(
            "Redmine #15745: rehydrate the three-tier fleet after a restart. READ-ONLY "
            "PLAN by default — for every lane the manifest calls active, join the durable "
            "authorities (lifecycle row, git worktree/branch, Redmine open/closed, live "
            "assigned-name inventory, durable delivery record) and name the exact actions "
            "that lane owes (heal_pair / restore_dispatch / resume_brief) or the typed skip "
            "/ block reason. --execute actuates ONLY those actions, re-reading each lane's "
            "identity fresh at action time and composing the existing `sublane create` "
            "adopt-or-launch and canonical handoff primitives. Never replays a delivered or "
            "uncertain send, never answers a provider UI, never closes a pane, and never "
            "touches a worktree or branch."
        ),
    )
    parser.add_argument(
        "--lane-label",
        dest="lane_label",
        default="",
        help="Restrict the actioned set to one lane label (other lanes are reported as a "
        "typed `filtered` skip, never dropped)",
    )
    parser.add_argument(
        "--integration-branch",
        dest="integration_branch",
        default="",
        help="Branch the lanes' heads are checked for reachability against (read-only)",
    )
    parser.add_argument(
        "--resume-anchor",
        dest="resume_anchor",
        action="append",
        metavar="LANE=JOURNAL",
        type=parse_resume_anchor,
        help="The CURRENT durable resume anchor for a delegated_coordinator lane "
        "(repeatable). Defaults to the lane's lifecycle decision journal. The anchor IS the "
        "brief's causal key: a fresh anchor is owed, an already-delivered one is never "
        "replayed.",
    )
    parser.add_argument(
        "--resume-profile-field",
        dest="resume_profile_field",
        action="append",
        metavar="LANE:KEY=VALUE",
        type=parse_resume_profile_field,
        help="A fixed delegated_coordinator role-profile field this rail cannot derive "
        "durably (repeatable). `parent_project` / `child_project` must be supplied; "
        "`parent_issue` / `parent_callback_target` default to the durable lifecycle facts "
        "and may be overridden. A lane missing any field blocks rather than sending a "
        "half-resolved delegation contract.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actuate the planned actions. Default: plan only, zero side effects.",
    )
    from mozyo_bridge.application.cli_common import add_repo_option

    add_repo_option(parser)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.set_defaults(func=cmd_sublane_rehydrate_fleet)


__all__ = (
    "FleetRehydrateUnavailable",
    "PARENT_ROUTE_COORDINATOR",
    "ResumeBriefInput",
    "cmd_sublane_rehydrate_fleet",
    "format_rehydrate_text",
    "gather_fleet_facts",
    "parent_callback_route",
    "parse_resume_anchor",
    "parse_resume_profile_field",
    "plan_fleet",
    "register_sublane_rehydrate_fleet_parser",
    "rehydrate_payload",
    "resume_inputs_from_args",
)
