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
import sys
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_binding_source import (
    _repo_root_from_args,
    load_workflow_binding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    RoleProviderBinding,
)
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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
    LiveRedmineJournalError,
    LiveRedmineJournalSource,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
    markers_from_source,
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


def _markers_from_redmine_json(args: argparse.Namespace) -> tuple[JournalMarker, ...]:
    """Read structured gate markers from a fetched Redmine issue-detail JSON snapshot.

    ``--redmine-json`` points at the ``/issues/<id>.json?include=journals`` (or MCP
    ``get_issue_detail``) payload an operator / MCP already fetched — the Redmine event
    source. Both real shapes are accepted: the Redmine REST shape that nests journals under
    ``issue.journals``, and the MCP / export wrapper shape with a top-level ``journals``
    list. The :class:`MappingRedmineJournalSource` reads its journal entries and
    :func:`markers_from_source` extracts the structured ``[mozyo:...]`` gate markers (never
    prose) into :class:`JournalMarker` inputs, so a Redmine-recorded review_request /
    review_result / implementation_done becomes a pending action. Absent the flag, returns
    ``()`` and the watcher ingests only explicit ``--marker`` specs.
    """
    raw = (getattr(args, "redmine_json", None) or "").strip()
    if not raw:
        return ()
    payload = _json.loads(Path(raw).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(
            f"--redmine-json {raw!r} must contain a Redmine issue-detail object "
            "(an issues.json / get_issue_detail payload), not a "
            f"{type(payload).__name__}"
        )
    source = MappingRedmineJournalSource(payload=payload)
    issue_id = (getattr(args, "source_issue", None) or "").strip()
    return markers_from_source(source, issue_id)


def _live_journal_source(args: argparse.Namespace) -> LiveRedmineJournalSource:
    """Build the live poll source from daemon-trusted credentials (patchable test seam).

    Isolated so a hermetic CLI test can monkeypatch it to return a source over a fake
    transport, without touching the real environment or the network. Credentials come only
    from env / the home credential file (never a repo-local file); an unconfigured environment
    raises :class:`LiveRedmineJournalError`.
    """
    since = (getattr(args, "since", None) or "").strip() or None
    return LiveRedmineJournalSource.from_environment(since=since)


def _markers_from_live_poll(args: argparse.Namespace) -> tuple[JournalMarker, ...]:
    """Read structured gate markers by polling Redmine live (opt-in ``--poll``).

    Absent ``--poll`` returns ``()`` so the default path is the non-destructive snapshot /
    ``--marker`` intake. With ``--poll`` the live adapter fetches ``--source-issue``'s journals
    over the network (credentials from env / the home file, never repo-local) and
    :func:`markers_from_source` extracts the structured ``[mozyo:...]`` gate markers — the same
    read/extract boundary the snapshot path uses. A missing issue id, unconfigured credentials,
    or a transport failure is surfaced as a ``SystemExit`` with a redacted message (never the
    key or URL), consistent with the ``--redmine-json`` payload guard.
    """
    if not getattr(args, "poll", False):
        return ()
    issue_id = (getattr(args, "source_issue", None) or "").strip()
    if not issue_id:
        raise SystemExit("--poll requires --source-issue ISSUE_ID (the Redmine issue to poll)")
    try:
        source = _live_journal_source(args)
        for warning in source.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        return markers_from_source(source, issue_id)
    except LiveRedmineJournalError as exc:
        raise SystemExit(str(exc)) from exc


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
    store: WorkflowRuntimeStore,
    markers,
    *,
    extra_route_records=(),
    binding: RoleProviderBinding | None = None,
) -> EventIntakeOutcome:
    """Read persisted runtime state from ``store`` and fold the new markers in (pure read).

    Combines the store's recorded events / route identities / advisory inputs with the
    supplied markers via the pure :func:`evaluate_event_intake`. ``extra_route_records`` are
    the ``--route-identity`` specs supplied in *this* invocation — they are merged after the
    persisted routes (recorded order: persisted oldest, this-run newest) so a route supplied
    alongside a marker resolves that same marker's action, instead of only taking effect on
    the next run. ``binding`` is the #12673 role->provider binding (the #13157 config
    override, or the compatibility default when ``None``); it is threaded into both the
    enrichment and the watcher's stricter ambiguity check. Does **not** persist — the caller
    decides whether to write the accepted events (``--dry-run`` skips the write).
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
        binding=binding,
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
    binding, warnings = load_workflow_binding(_repo_root_from_args(args))
    # Advisory (non-blocking) binding warnings to stderr so the single structured envelope
    # on stdout stays clean (#13157).
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    # Redmine-sourced structured markers (the event source) first — a fetched snapshot
    # (--redmine-json) and/or a live network poll (--poll --source-issue) — then explicit
    # --marker specs (debug / supplemental); all feed the same intake. Duplicate anchors across
    # the sources are deduplicated by the intake's redmine:<issue>:<journal> suppression.
    markers = (
        _markers_from_redmine_json(args)
        + _markers_from_live_poll(args)
        + tuple(getattr(args, "marker", None) or ())
    )
    routes = list(getattr(args, "route_identity", None) or ())
    outcome = evaluate_intake_from_store(
        store, markers, extra_route_records=routes, binding=binding
    )

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
            "(Redmine #12672 / #13289). Reads the Redmine event source three ways: "
            "--redmine-json scans a fetched issue-detail snapshot's journal entries for "
            "structured [mozyo:...] gate markers (the durable history), --poll fetches the "
            "same markers live over the network from --source-issue (opt-in, credentialed), "
            "and --marker supplies an "
            "explicit/debug ISSUE:JOURNAL:GATE[,conclusion=,callback=,commit=,integrated=,"
            "open=,blocker=] spec (repeatable; the gate accepts the alias review_result); "
            "all feed the same intake. The watcher keys "
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
        "--redmine-json",
        dest="redmine_json",
        default=None,
        metavar="PATH",
        help=(
            "Read the Redmine event source: a fetched issue-detail JSON snapshot "
            "(issues.json?include=journals / get_issue_detail shape) whose journal entries "
            "are scanned for structured [mozyo:handoff|workflow-event:...] gate markers and "
            "converted into pending actions. Structured markers only — note prose is never "
            "parsed; a journal with no recognized marker yields nothing. Combine with "
            "--source-issue to set the issue id when the payload omits it. The live "
            "credentialed auto-poll adapter is a follow-up; this reads a supplied snapshot."
        ),
    )
    watch.add_argument(
        "--source-issue",
        dest="source_issue",
        default=None,
        metavar="ISSUE_ID",
        help=(
            "The Redmine issue id: for --redmine-json, used when the payload's issue.id is "
            "absent; for --poll, the required issue whose journals are fetched live (the "
            "journal entry's own id is always the dedup anchor)."
        ),
    )
    watch.add_argument(
        "--poll",
        action="store_true",
        dest="poll",
        help=(
            "Opt-in: read the Redmine event source live over the network instead of a "
            "hand-fetched --redmine-json snapshot. Fetches --source-issue's journals via a "
            "read-only issues/<id>.json?include=journals GET and extracts the same structured "
            "[mozyo:...] gate markers. Requires --source-issue. Credentials come only from the "
            "daemon-trusted env (MOZYO_REDMINE_API_KEY / MOZYO_REDMINE_URL) or the home-scoped "
            "redmine-credentials.yaml — never a repo-local file; the API key is never echoed. "
            "An unconfigured environment or a fetch failure fails closed with a redacted error."
        ),
    )
    watch.add_argument(
        "--since",
        dest="since",
        default=None,
        metavar="UPDATED_ON",
        help=(
            "Cursor for --poll: an ISO updated_on timestamp (e.g. 2026-07-05T08:00:00Z). Only "
            "journals created strictly after it are ingested; a journal without a timestamp is "
            "kept. Optional — the durable redmine:<issue>:<journal> anchor deduplicates a "
            "re-poll regardless, so the cursor is an efficiency filter, not a correctness gate."
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
        "--repo",
        dest="repo",
        default=None,
        metavar="PATH",
        help=(
            "Repo root whose .mozyo-bridge/config.yaml provides the role->provider binding "
            "override (Redmine #13157). A missing file / provider_binding block threads the "
            "compatibility default (codex/claude). Defaults to the resolved repo root."
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
