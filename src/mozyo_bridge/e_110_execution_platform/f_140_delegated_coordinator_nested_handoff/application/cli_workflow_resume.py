"""CLI surface for `workflow resume` — explain current state + next action (Redmine #12671).

`mozyo-bridge workflow resume` is the explicit-execution entrypoint the spine roadmap US
#12671 (``vibes/docs/logics/coordinator-sublane-development-flow.md`` ``### ロードマップUS``
step 2: "`workflow resume` / `workflow action run` 相当の明示実行入口を持つ") asks for. Where
`workflow runtime` (#12857) takes an ordered event log *on the command line* and is purely
advisory in memory, `resume` reads the **persisted** workflow runtime state from the mozyo
DB (the #12671 :class:`...core.state.workflow_runtime_store.WorkflowRuntimeStore`) — the
durable lane event log, the issue-tagged route identities, and the advisory scalar inputs —
re-folds it through the #12857 runtime, and reports the current ``workflow.state`` plus the
enriched ``workflow.next_action`` (owner_role / route_identity / anchor / suggested_command
/ risk_level / requires_confirmation / blocked_reason).

The mozyo DB holds runtime state; Redmine stays the durable memory. So `resume` is the
"where am I and what is the next action?" command an agent runs without re-deriving it from
free text. It is **advisory / explicit**: it discovers nothing live, never auto-runs the
recommended action (that is a deliberate later step), and never blocks (exit 0). A pane id
is never emitted — ``route_identity`` is the public-safe stable pointer, and the persisted
``last_seen_pane_id`` is cache / evidence the read model intentionally drops.

The companion writer is `workflow runtime --persist` (in
:mod:`...application.cli_workflow_runtime`): it appends the supplied events / route
identities / advisory inputs to the same store, so `runtime --persist` then `resume`
reproduces the same decision from durable runtime state.
"""

from __future__ import annotations

import argparse
import json as _json
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence

from mozyo_bridge.core.state.workflow_runtime_store import (
    META_CAPACITY,
    META_OWNER_OR_RELEASE_GATE,
    META_READY_INDEPENDENT,
    META_READY_OVERLAP,
    WorkflowEventRow,
    WorkflowRouteRow,
    WorkflowRuntimeStore,
    workflow_runtime_store_path,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (
    RouteIdentity,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_next_action import (
    RouteCandidate,
    WorkflowCommandResult,
    derive_workflow_next_action,
    render_command_result_journal,
    render_command_result_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    LaneEvent,
    evaluate_workflow_runtime,
)


class WorkflowResumeStore(Protocol):
    """The read port `workflow resume` depends on (the live DB store satisfies it).

    Declared as a Protocol so the use case stays testable with an in-memory fake (no
    SQLite) — the live :class:`...core.state.workflow_runtime_store.WorkflowRuntimeStore`
    matches it structurally.
    """

    def read_events(self) -> Sequence[WorkflowEventRow]: ...

    def read_route_identities(self) -> Sequence[WorkflowRouteRow]: ...

    def read_meta(self) -> Mapping[str, str]: ...


def _event_from_row(row: WorkflowEventRow) -> LaneEvent:
    """Map a persisted event row onto the #12857 :class:`LaneEvent` it replays as."""
    return LaneEvent(
        event_id=row.event_id,
        issue=row.issue,
        gate=row.gate,
        review_conclusion=row.review_conclusion,
        callback_state=row.callback_state,
        commit_bearing=row.commit_bearing,
        integration_recorded=row.integration_recorded,
        issue_open=row.issue_open,
        blocker_recorded=row.blocker_recorded,
    )


def _int_meta(meta: Mapping[str, str], key: str) -> int:
    """Read an integer advisory input, tolerating a missing / malformed value (-> 0)."""
    try:
        return int(str(meta.get(key, "0")).strip() or "0")
    except (TypeError, ValueError):
        return 0


def _bool_meta(meta: Mapping[str, str], key: str) -> bool:
    """Read a boolean advisory input, tolerating a missing value (-> False)."""
    return str(meta.get(key, "")).strip().lower() in ("1", "true", "yes", "y")


def assemble_command_result(
    event_rows: Iterable[WorkflowEventRow],
    route_rows: Iterable[WorkflowRouteRow],
    meta: Mapping[str, str],
) -> WorkflowCommandResult:
    """Fold persisted runtime state into the enriched command result (pure given rows).

    - the event rows replay (with #12857 duplicate suppression) into per-lane state and
      the overall next action;
    - the route rows become the issue -> route-candidate map (provider role + **public-safe**
      pointer, in recorded order, no pane id); a route whose stable identity is malformed is
      skipped rather than failing the whole resume. Selection by owner_role + fail-closed on
      no provider match happens in :func:`derive_workflow_next_action`;
    - the latest persisted event id per issue is that lane's durable anchor;
    - the advisory meta reproduces the ready-work / capacity / owner-gate inputs.
    """
    events = [_event_from_row(row) for row in event_rows]

    # issue -> route candidates in recorded order (read_route_identities orders by
    # recorded_at). The next action selects among them by its owner_role.
    issue_routes: dict[str, list[RouteCandidate]] = {}
    for row in route_rows:
        try:
            identity = RouteIdentity.from_record(row.as_record())
        except ValueError:
            # A malformed persisted identity must not abort the whole resume; skip it so
            # the lane's route stays unresolved (fails closed downstream) for that row only.
            continue
        issue_routes.setdefault(row.issue, []).append(
            RouteCandidate(provider_role=identity.role, pointer=identity.public_pointer())
        )

    # Latest persisted event per issue (rows arrive in apply order) is the lane's anchor.
    issue_anchors: dict[str, str] = {}
    for event in events:
        issue_anchors[event.issue] = event.event_id

    state = evaluate_workflow_runtime(
        events,
        ready_independent_work=_int_meta(meta, META_READY_INDEPENDENT),
        ready_overlapping_work=_int_meta(meta, META_READY_OVERLAP),
        capacity_remaining=_int_meta(meta, META_CAPACITY),
        owner_or_release_gate_active=_bool_meta(meta, META_OWNER_OR_RELEASE_GATE),
    )
    next_action = derive_workflow_next_action(
        state,
        issue_routes=issue_routes,
        issue_anchors=issue_anchors,
    )
    return WorkflowCommandResult(state=state, next_action=next_action)


def resume_command_result(store: WorkflowResumeStore) -> WorkflowCommandResult:
    """Read the persisted runtime state from ``store`` and assemble the command result."""
    return assemble_command_result(
        store.read_events(), store.read_route_identities(), store.read_meta()
    )


def _store_from_args(args: argparse.Namespace) -> WorkflowRuntimeStore:
    """Build the live store from ``--store-path`` (test/debug) or the home default."""
    raw = (getattr(args, "store_path", None) or "").strip()
    path = Path(raw) if raw else workflow_runtime_store_path()
    return WorkflowRuntimeStore(path=path)


def cmd_workflow_resume(args: argparse.Namespace) -> int:
    """Read persisted runtime state and report state + enriched next_action (#12671).

    Loads the durable event log / route identities / advisory inputs from the mozyo DB,
    re-folds them, and emits exactly one envelope: a text summary, the nested
    ``workflow.{state,next_action}`` JSON with ``--json``, or the durable record markdown
    with ``--journal``. Always returns 0: the result is advisory and never blocks. A pane
    id is never emitted.
    """
    result = resume_command_result(_store_from_args(args))
    if getattr(args, "as_journal", False):
        print(render_command_result_journal(result))
    elif getattr(args, "as_json", False):
        print(
            _json.dumps(
                result.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        print(render_command_result_text(result))
    return 0


def register_resume(workflow_sub) -> None:
    """Register ``workflow resume`` onto the ``workflow`` subparser (#12671)."""
    resume = workflow_sub.add_parser(
        "resume",
        description=(
            "Explain the current workflow state and the next action from the persisted "
            "mozyo DB runtime state (Redmine #12671). Reads the durable lane event log, "
            "the issue-tagged route identities, and the advisory inputs that "
            "`workflow runtime --persist` wrote, re-folds them (with duplicate "
            "suppression) through the #12857 runtime, and reports the current "
            "workflow.state plus the enriched workflow.next_action (owner_role / "
            "route_identity / anchor / suggested_command / risk_level / "
            "requires_confirmation / blocked_reason). Redmine stays the durable memory; "
            "the mozyo DB holds runtime state. Advisory / explicit: it discovers nothing "
            "live, never auto-runs the recommended action, never emits a pane id, and "
            "never blocks (exit 0). See vibes/docs/logics/coordinator-sublane-development-flow.md."
        ),
        help=(
            "Explain the current workflow state and the next action from persisted mozyo "
            "DB runtime state (enriched next_action). Advisory; never blocks."
        ),
    )
    resume.add_argument(
        "--store-path",
        dest="store_path",
        default=None,
        help=argparse.SUPPRESS,  # test/debug override; default is the home store
    )
    resume.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit exactly one structured command-result envelope as JSON "
            "(workflow.state + enriched workflow.next_action)."
        ),
    )
    resume.add_argument(
        "--journal",
        action="store_true",
        dest="as_journal",
        help=(
            "Emit the durable record markdown (runtime record + enriched next action) for "
            "the Redmine journal (takes precedence over --json)."
        ),
    )
    resume.set_defaults(func=cmd_workflow_resume)


__all__ = (
    "WorkflowResumeStore",
    "assemble_command_result",
    "resume_command_result",
    "cmd_workflow_resume",
    "register_resume",
)
