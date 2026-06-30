"""CLI surface for `workflow watch` — Redmine journal -> pending action intake (#12672).

`mozyo-bridge workflow watch` is the event-watcher entrypoint the spine roadmap US #12672
(``vibes/docs/logics/coordinator-sublane-development-flow.md`` ``### ロードマップUS`` step 3)
asks for: it reads **structured** Redmine journal markers, turns them into durable workflow
events keyed by the ``redmine:<issue>:<journal>`` anchor, folds them into the DB-backed
runtime, and reports the resulting *pending workflow action* — with duplicate suppression,
fail-closed ambiguity handling, and no auto-send.

Where `workflow runtime` (#12857) takes an ad-hoc event log on the command line and
`workflow resume` (#12671) reads the persisted store, `watch` is the *intake* of new
journal markers into that same store. Each ``--marker ISSUE:JOURNAL:GATE[,key=value...]``
is a structured gate marker a journal sweep yielded (never a free-text parse). The command:

1. reads the already-recorded events / route identities / advisory inputs from the mozyo DB;
2. classifies each marker as accepted (new durable anchor) or suppressed (already recorded /
   repeated) — observable duplicate suppression;
3. re-folds the recorded + accepted events into ``workflow.state`` + the enriched
   ``workflow.next_action`` and classifies the result as a pending action (ready /
   needs_confirmation / failed);
4. unless ``--dry-run``, persists the newly accepted events to the store so `workflow resume`
   reproduces the same decision.

It is **advisory / explicit** and fail-closed: it discovers nothing live, sends nothing
(a missing / mismatched / ambiguous route is recorded as a ``failed`` pending action, never
delivered), never emits a pane id, and always returns exit 0 — the result is a record, not a
delivery. Auto-delivery (``workflow action run``) is a deliberate later step.
"""

from __future__ import annotations

import argparse
import dataclasses
import json as _json
from pathlib import Path

from mozyo_bridge.core.state.workflow_runtime_store import (
    META_CAPACITY,
    META_OWNER_OR_RELEASE_GATE,
    META_READY_INDEPENDENT,
    META_READY_OVERLAP,
    WorkflowRuntimeStore,
    workflow_runtime_store_path,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (
    RouteIdentity,
    RouteIdentityError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    EventIntakeOutcome,
    JournalMarker,
    JournalMarkerError,
    build_marker,
    evaluate_event_intake,
    render_intake_journal,
    render_intake_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_next_action import (
    RouteCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_runtime import (
    LaneEvent,
)


def _parse_bool(key: str, value: str) -> bool:
    """Parse a ``key=value`` boolean modifier (``0``/``1``/``true``/``false``)."""
    norm = value.strip().lower()
    if norm in ("1", "true", "yes", "y"):
        return True
    if norm in ("0", "false", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(
        f"--marker {key}= expects a boolean (0/1/true/false), got {value!r}"
    )


def _parse_marker(spec: str) -> JournalMarker:
    """Parse an ``ISSUE:JOURNAL:GATE[,key=value...]`` ``--marker`` spec (structured only).

    The first two ``:`` separate the issue id and journal id (the durable anchor); the rest
    is a comma list whose first element is the gate marker name and whose remaining
    ``key=value`` elements set the structured #12856 lane facts:

    - ``conclusion=`` pending|approved|changes_requested (a ``review`` / ``review_result``)
    - ``callback=`` none|due|delivery_failed
    - ``commit=`` 0|1, ``integrated=`` 0|1, ``open=`` 0|1 (default 1), ``blocker=`` 0|1

    The gate name accepts the journal-facing alias ``review_result`` (mapped to the runtime
    ``review`` gate). Every value is validated against the literal vocabulary by
    :func:`build_marker`; an unknown gate / conclusion / callback is rejected at parse time
    rather than guessed from prose.
    """
    raw = (spec or "").strip()
    issue, sep1, rest1 = raw.partition(":")
    journal, sep2, rest2 = rest1.partition(":")
    if not sep1 or not sep2:
        raise argparse.ArgumentTypeError(
            "--marker expects ISSUE:JOURNAL:GATE (e.g. 12672:68978:review_request), "
            f"got {spec!r}"
        )
    issue = issue.strip()
    journal = journal.strip()
    parts = [p.strip() for p in rest2.split(",") if p.strip()]
    if not issue or not journal or not parts:
        raise argparse.ArgumentTypeError(
            f"--marker expects a non-empty ISSUE, JOURNAL and GATE, got {spec!r}"
        )
    gate = parts[0]

    conclusion = "pending"
    callback = "none"
    commit_bearing = False
    integration_recorded = False
    issue_open = True
    blocker_recorded = False

    for modifier in parts[1:]:
        key, eq, value = modifier.partition("=")
        if not eq:
            raise argparse.ArgumentTypeError(
                f"--marker modifier expects key=value, got {modifier!r}"
            )
        key = key.strip()
        value = value.strip()
        if key == "conclusion":
            conclusion = value
        elif key == "callback":
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
                f"--marker unknown modifier {key!r} (expected conclusion / callback / "
                "commit / integrated / open / blocker)"
            )

    try:
        return build_marker(
            issue,
            journal,
            gate,
            review_conclusion=conclusion,
            callback_state=callback,
            commit_bearing=commit_bearing,
            integration_recorded=integration_recorded,
            issue_open=issue_open,
            blocker_recorded=blocker_recorded,
        )
    except (JournalMarkerError, ValueError) as exc:
        # Re-raise as an argparse error so a bad gate / conclusion fails at parse time.
        raise argparse.ArgumentTypeError(str(exc)) from exc


#: The ``--route-identity`` key aliases accepted on the CLI (alias -> store column).
#: Mirrors ``cli_workflow_runtime._ROUTE_KEY_ALIASES`` so the watcher and runtime accept the
#: same route spec; the pane id is recorded only as cache, never a routing key.
_ROUTE_KEY_ALIASES = {
    "route_id": "route_id",
    "route": "route_id",
    "issue": "issue",
    "ws": "workspace_id",
    "workspace_id": "workspace_id",
    "lane": "lane_id",
    "lane_id": "lane_id",
    "role": "role",
    "pane_name": "pane_name",
    "pane": "pane_name",
    "pane_id": "last_seen_pane_id",
    "last_seen_pane_id": "last_seen_pane_id",
    "observed": "observed_at",
    "observed_at": "observed_at",
}


def _parse_route_identity(spec: str) -> dict:
    """Parse a ``key=value,...`` ``--route-identity`` spec into a store route record.

    Required stable keys: ``route_id`` / ``issue`` / ``ws`` / ``role`` / ``pane_name``;
    ``lane`` defaults to ``default``; ``pane_id`` (cache) and ``observed`` are optional. A
    missing required key is rejected at parse time so a half-formed identity that could only
    be matched by pane id never persists.
    """
    record: dict[str, str] = {}
    for modifier in (p.strip() for p in (spec or "").split(",") if p.strip()):
        key, eq, value = modifier.partition("=")
        if not eq:
            raise argparse.ArgumentTypeError(
                f"--route-identity modifier expects key=value, got {modifier!r}"
            )
        column = _ROUTE_KEY_ALIASES.get(key.strip())
        if column is None:
            raise argparse.ArgumentTypeError(
                f"--route-identity unknown key {key.strip()!r} (expected "
                f"{sorted(set(_ROUTE_KEY_ALIASES))})"
            )
        record[column] = value.strip()
    missing = [
        k for k in ("route_id", "issue", "workspace_id", "role", "pane_name") if not record.get(k)
    ]
    if missing:
        raise argparse.ArgumentTypeError(
            f"--route-identity requires non-empty {missing} (a pane id is never the route "
            "authority)"
        )
    return record


def _store_from_args(args: argparse.Namespace) -> WorkflowRuntimeStore:
    """Build the live store from ``--store-path`` (test/debug) or the home default."""
    raw = (getattr(args, "store_path", None) or "").strip()
    path = Path(raw) if raw else workflow_runtime_store_path()
    return WorkflowRuntimeStore(path=path)


def _int_meta(meta, key: str) -> int:
    """Read a persisted integer advisory input, tolerating a missing / malformed value."""
    try:
        return int(str(meta.get(key, "0")).strip() or "0")
    except (TypeError, ValueError):
        return 0


def _bool_meta(meta, key: str) -> bool:
    """Read a persisted boolean advisory input, tolerating a missing value (-> False)."""
    return str(meta.get(key, "")).strip().lower() in ("1", "true", "yes", "y")


def _recorded_events(store: WorkflowRuntimeStore) -> tuple[LaneEvent, ...]:
    """The store's persisted events as #12857 lane events (apply order)."""
    return tuple(
        LaneEvent(
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
        for row in store.read_events()
    )


def _route_candidates(store: WorkflowRuntimeStore) -> dict[str, list[RouteCandidate]]:
    """The store's persisted routes as the issue -> candidate map (public-safe; no pane id)."""
    issue_routes: dict[str, list[RouteCandidate]] = {}
    for row in store.read_route_identities():
        try:
            identity = RouteIdentity.from_record(row.as_record())
        except (RouteIdentityError, ValueError):
            # A malformed persisted identity must not abort intake; skip it so its lane
            # stays unresolved (fails closed downstream) for that row only.
            continue
        issue_routes.setdefault(row.issue, []).append(
            RouteCandidate(provider_role=identity.role, pointer=identity.public_pointer())
        )
    return issue_routes


def _candidate_from_record(rec) -> RouteCandidate | None:
    """Convert one ``--route-identity`` record into a public-safe candidate (or None)."""
    try:
        identity = RouteIdentity.from_record(rec)
    except (RouteIdentityError, ValueError):
        return None
    return RouteCandidate(provider_role=identity.role, pointer=identity.public_pointer())


def evaluate_intake_from_store(
    store: WorkflowRuntimeStore, markers, *, extra_route_records=()
) -> EventIntakeOutcome:
    """Read persisted runtime state from ``store`` and fold the new markers in (pure read).

    Combines the store's recorded events / route identities / advisory inputs with the
    supplied markers via the pure :func:`evaluate_event_intake`. ``extra_route_records`` are
    the ``--route-identity`` specs supplied in *this* invocation — they are merged after the
    persisted routes (recorded order: persisted oldest, this-run newest) so a route supplied
    alongside a marker resolves that same marker's action, instead of only taking effect on
    the next run. Does **not** persist — the caller decides whether to write the accepted
    events (``--dry-run`` skips the write).
    """
    recorded = _recorded_events(store)
    meta = store.read_meta()
    issue_routes = _route_candidates(store)
    for rec in extra_route_records:
        candidate = _candidate_from_record(rec)
        if candidate is not None:
            issue_routes.setdefault(str(rec.get("issue", "")).strip(), []).append(
                candidate
            )
    return evaluate_event_intake(
        markers,
        recorded_events=recorded,
        known_event_ids=[e.event_id for e in recorded],
        issue_routes=issue_routes,
        ready_independent_work=_int_meta(meta, META_READY_INDEPENDENT),
        ready_overlapping_work=_int_meta(meta, META_READY_OVERLAP),
        capacity_remaining=_int_meta(meta, META_CAPACITY),
        owner_or_release_gate_active=_bool_meta(meta, META_OWNER_OR_RELEASE_GATE),
    )


def cmd_workflow_watch(args: argparse.Namespace) -> int:
    """Ingest structured journal markers into a pending workflow action (#12672).

    Reads the persisted store, classifies each ``--marker`` as accepted / suppressed, folds
    the recorded + accepted events into the enriched ``workflow.next_action``, classifies it
    as a pending action, and (unless ``--dry-run``) persists the newly accepted events plus
    any supplied ``--route-identity`` so `workflow resume` reproduces the decision. Emits one
    envelope: a text summary, the JSON intake outcome with ``--json``, or the durable record
    markdown with ``--journal``. Never sends; always returns 0 (the result is a record).
    """
    store = _store_from_args(args)
    markers = tuple(getattr(args, "marker", None) or ())
    routes = list(getattr(args, "route_identity", None) or ())
    outcome = evaluate_intake_from_store(store, markers, extra_route_records=routes)

    if not getattr(args, "dry_run", False):
        accepted = outcome.accepted_events
        if accepted:
            store.append_events(dataclasses.asdict(event) for event in accepted)
        if routes:
            store.put_route_identities(routes)

    if getattr(args, "as_journal", False):
        print(render_intake_journal(outcome))
    elif getattr(args, "as_json", False):
        print(
            _json.dumps(
                outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
    else:
        print(render_intake_text(outcome))
    return 0


def register_watch(workflow_sub) -> None:
    """Register ``workflow watch`` onto the ``workflow`` subparser (#12672)."""
    watch = workflow_sub.add_parser(
        "watch",
        description=(
            "Ingest structured Redmine journal markers into a pending workflow action "
            "(Redmine #12672). Reads the durable journal markers of each lane "
            "(--marker ISSUE:JOURNAL:GATE[,conclusion=,callback=,commit=,integrated=,"
            "open=,blocker=], repeatable; the gate accepts the alias review_result), keys "
            "each by the durable redmine:<issue>:<journal> anchor, and folds the recorded "
            "+ newly accepted events into workflow.state + the enriched "
            "workflow.next_action — reporting it as a pending action (ready / "
            "needs_confirmation / failed). Duplicate suppression is observable (a marker "
            "whose anchor is already recorded is suppressed); a missing / mismatched / "
            "ambiguous route is a fail-closed 'failed' pending action, never delivered. "
            "Structured markers only — it never parses the note prose. Unless --dry-run, "
            "the accepted events (and any --route-identity) are persisted so `workflow "
            "resume` reproduces the decision. Advisory / explicit: it discovers nothing "
            "live, sends nothing, never emits a pane id, and never blocks (exit 0). See "
            "vibes/docs/logics/coordinator-sublane-development-flow.md."
        ),
        help=(
            "Advisory: ingest structured Redmine journal markers (deduped by "
            "redmine:<issue>:<journal>) into a pending workflow action. Records a "
            "failed pending state for a missing/ambiguous route; never sends."
        ),
    )
    watch.add_argument(
        "--marker",
        action="append",
        type=_parse_marker,
        metavar="ISSUE:JOURNAL:GATE[,key=value...]",
        help=(
            "One structured journal marker as ISSUE:JOURNAL:GATE (repeatable, applied in "
            "order). GATE is a durable gate kind (none / start / progress / "
            "implementation_done / review_request / review / review_result / "
            "owner_close_approval / close / blocked). Optional comma modifiers: "
            "conclusion=pending|approved|changes_requested (for a review / review_result), "
            "callback=none|due|delivery_failed, commit=0|1, integrated=0|1, open=0|1 "
            "(default 1), blocker=0|1. The durable anchor redmine:<issue>:<journal> "
            "deduplicates re-observed journals."
        ),
    )
    watch.add_argument(
        "--route-identity",
        action="append",
        dest="route_identity",
        type=_parse_route_identity,
        metavar="route_id=...,issue=...,ws=...,role=...,pane_name=...[,lane=,pane_id=,observed=]",
        help=(
            "A lane's stable route identity to persist (repeatable). Required keys: "
            "route_id, issue, ws (workspace_id), role, pane_name; optional: lane (default "
            "'default'), pane_id (recorded as the cache/evidence last_seen_pane_id, never "
            "a routing key), observed. The pane id is never emitted in output."
        ),
    )
    watch.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help=(
            "Classify and report the pending action without persisting the accepted events "
            "/ routes to the mozyo DB (preview only)."
        ),
    )
    watch.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit exactly one structured intake outcome as JSON (per-marker intake "
            "disposition + pending_action + workflow.{state,next_action})."
        ),
    )
    watch.add_argument(
        "--journal",
        action="store_true",
        dest="as_journal",
        help=(
            "Emit the durable record markdown (intake summary + command-result record) for "
            "the Redmine journal (takes precedence over --json)."
        ),
    )
    watch.add_argument(
        "--store-path",
        dest="store_path",
        default=None,
        help=argparse.SUPPRESS,  # test/debug override; default is the home store
    )
    watch.set_defaults(func=cmd_workflow_watch)


__all__ = (
    "cmd_workflow_watch",
    "evaluate_intake_from_store",
    "register_watch",
)
