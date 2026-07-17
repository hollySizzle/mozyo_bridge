"""CLI surface for `workflow drain-queue` — coordinator dependency drain queue (Redmine #13967).

`mozyo-bridge workflow drain-queue` is the read-only projection a coordinator runs to
decide **whether it still needs to keep an active process resident** — the early-hibernate
question (Redmine #13967 item 1). It buckets the active lane set into the fixed drain-queue
vocabulary (``callback / review / owner / integration / close / blocked / retirement /
release-dogfood``, the spine `### Drain Order` plus the delegated dogfood bucket), tags each
bucket with its actionable-vs-non-actionable ownership split, and emits one
``process_retention`` verdict (:data:`...drain_queue.PROCESS_HOLD` /
:data:`...drain_queue.PROCESS_RELEASABLE`).

Boundary with the neighbouring surfaces:

- ``workflow glance`` projects **per-lane** workflow state + next action + delivery anomaly.
  ``workflow fill-decision`` answers "dispatch the next sublane or stop?". ``drain-queue`` is
  the **aggregate** view: it groups those same lanes into the drain buckets and answers "can
  the coordinator process release, or must it hold?". It reuses the glance / fill-decision
  read model — it does not invent a second state machine — and mutates nothing.

Sources (fail-closed to a visible ``unknown`` bucket, never a silent empty):

- ``--snapshot-json PATH``: an already-composed structured lane list
  (``{"lanes": [ {issue, state_class, actionability, next_action_owner, lane,
  release_pending, reason}, ... ]}``; a bare list is also accepted). The deterministic
  contract surface — structured facts only, no prose parsed.
- ``--from-glance PATH``: a ``workflow glance --json`` envelope. Each active row folds to a
  drain lane (its ``workflow_state`` is the state class); each ``lifecycle_diagnostic`` row
  whose release axis is ``requested`` / ``partial`` folds to a delegated ``release_dogfood``
  lane. The richest live path (the glance fold already read the durable Redmine record).
- default (no source flag): a best-effort live enumeration of the active-lane roster + the
  lifecycle diagnostic, folded through the same glance read model. Without the durable Redmine
  fold a lane may read ``unknown`` (degraded, surfaced) — prefer ``--from-glance`` for the
  full durable-record classification.

Actionability defaults to ``coordinator_actionable`` (the fail-closed blocking sink) unless a
structured lane supplies an earned non-blocking claim — a live projection never fabricates a
``delegated_in_flight`` / ``non_actionable_wait`` it cannot substantiate.
"""

from __future__ import annotations

import argparse
import dataclasses
import json as _json
from pathlib import Path

from mozyo_bridge.core.state.lane_lifecycle_model import (
    DISPOSITION_ACTIVE,
    DISPOSITIONS,
    RELEASE_PARTIAL,
    RELEASE_REQUESTED,
    RELEASE_STATES,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.drain_queue import (
    DrainLane,
    drain_queue_payload,
    project_drain_queue,
    render_drain_queue_table,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_actionability import (
    ACTIONABILITY_COORDINATOR_ACTIONABLE,
    ACTIONABILITY_NON_ACTIONABLE_WAIT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    LANE_STATE_IDLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (
    ANOMALY_NONE,
    DELIVERY_ANOMALIES,
)

# The release-axis values that mean a centralized TestPyPI / installed dogfood is still
# owed on the dedicated release issue (Redmine #13967 item 2).
_RELEASE_PENDING = frozenset({RELEASE_REQUESTED, RELEASE_PARTIAL})

# The lifecycle-diagnostic roster is, by contract, non-active lanes only (an active lane is
# in the active roster, not the diagnostic). So a diagnostic row whose lane_disposition is
# `active` — or an unknown token — is a malformed/contradictory canonical row (Redmine
# #13967 R7-F2).
_NON_ACTIVE_DISPOSITIONS = frozenset(DISPOSITIONS - {DISPOSITION_ACTIVE})


# Sentinel distinguishing a KEY-ABSENT field from a KEY-PRESENT null / non-string value
# (Redmine #13967 R5-F1). ``dict.get(k)`` returns None for both, conflating "the caller
# omitted the field" (take the default) with "the caller gave an explicit null" (malformed).
_MISSING = object()


def _exact_str(value: object, *, required: bool = False) -> str | None:
    """Return the stripped string ONLY when ``value`` is an exact ``str`` (Redmine #13967 R4/R5-F).

    Pass ``value`` via ``raw.get(key, _MISSING)`` so an absent key is distinguishable from a
    present ``null``:

    - ``_MISSING`` (key absent) -> ``None`` when ``required`` else ``""`` (the optional default);
    - a present ``None`` (explicit JSON null) or any non-string -> ``None`` (malformed; NEVER
      ``str(...)``-coerced, and NEVER folded into the absent-default — R5-F1);
    - a present ``str`` -> its stripped value (``None`` when ``required`` and it is empty).
    """
    if value is _MISSING:
        return None if required else ""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if required and not text:
        return None
    return text


def _lane_from_mapping(raw: object) -> DrainLane | None:
    """Build a :class:`DrainLane` from a structured mapping (fail-closed, exact-type).

    Returns None when the row is malformed: a non-dict; a missing / non-string / empty
    ``issue`` or ``state_class``; a non-string ``lane`` / ``actionability`` /
    ``next_action_owner`` / ``reason`` (identity and classification fields are NEVER
    ``str(...)``-coerced — Redmine #13967 R4-F2); or a ``release_pending`` that is present
    but not an exact JSON bool (a string ``"false"`` must not coerce to True — R2-F2). The
    caller marks a snapshot with any malformed row durable-incomplete rather than dropping it.
    """
    if not isinstance(raw, dict):
        return None
    issue = _exact_str(raw.get("issue", _MISSING), required=True)
    state_class = _exact_str(raw.get("state_class", _MISSING), required=True)
    lane = _exact_str(raw.get("lane", _MISSING))
    next_owner = _exact_str(raw.get("next_action_owner", _MISSING))
    reason = _exact_str(raw.get("reason", _MISSING))
    if issue is None or state_class is None or lane is None or next_owner is None or reason is None:
        return None
    rp = raw.get("release_pending", False)
    if not isinstance(rp, bool):
        return None  # exact-type: a non-bool release_pending is malformed, never coerced
    actionability = raw.get("actionability", ACTIONABILITY_COORDINATOR_ACTIONABLE)
    if not isinstance(actionability, str):
        return None
    actionability = actionability.strip() or ACTIONABILITY_COORDINATOR_ACTIONABLE
    return DrainLane(
        issue=issue,
        state_class=state_class,
        actionability=actionability,
        next_action_owner=next_owner,
        lane=lane,
        release_pending=rp,
        reason=reason,
    )


def _lanes_from_snapshot(path: str) -> tuple[tuple[DrainLane, ...], bool]:
    """``(lanes, complete)``. ``complete`` is False when any row was malformed (an invalid
    row is NOT silently dropped — it makes the snapshot durable-incomplete so the retention
    verdict fails closed to hold, Redmine #13967 R2-F2)."""
    try:
        data = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--snapshot-json {path!r} could not be read as JSON: {exc}") from exc
    entries = data.get("lanes", data) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        # A non-list lanes is a malformed envelope -> durable-incomplete (hold), not a crash
        # and not a silent empty (Redmine #13967 R4-F2).
        return (), False
    lanes: list[DrainLane] = []
    complete = True
    for raw in entries:
        lane = _lane_from_mapping(raw)
        if lane is None:
            complete = False  # malformed row -> durable-incomplete, not dropped
        else:
            lanes.append(lane)
    return tuple(lanes), complete


def _release_dogfood_lane(issue: str, lane: str) -> DrainLane:
    return DrainLane(
        issue=issue,
        lane=lane,
        state_class=LANE_STATE_IDLE,
        actionability=ACTIONABILITY_NON_ACTIONABLE_WAIT,
        next_action_owner="external_condition",
        release_pending=True,
        reason="release_dogfood_delegated_to_release_issue",
    )


def _merge_release_pending(
    active: list[DrainLane], release_rows: list[tuple[str, str]]
) -> tuple[DrainLane, ...]:
    """Merge release-pending flags into the active lanes by (issue, lane) identity.

    A lane already present in ``active`` gets its ``release_pending`` flag set on the SAME
    row (so it is counted once and, per :func:`...drain_queue.bucket_for_state`, a
    coordinator-blocking base bucket still wins over release_dogfood — a delegated dogfood
    never hides live drain). A release-pending identity with no active row is appended as a
    fresh release_dogfood lane. This keeps ``lane_count`` correct and enforces the "one
    lane, one bucket" invariant against composed/malformed inputs (Redmine #13967 F3).
    """
    by_key: dict[tuple[str, str], int] = {
        (l.issue, l.lane): i for i, l in enumerate(active)
    }
    merged = list(active)
    for issue, lane in release_rows:
        key = (issue, lane)
        if key in by_key:
            idx = by_key[key]
            merged[idx] = dataclasses.replace(merged[idx], release_pending=True)
        else:
            by_key[key] = len(merged)
            merged.append(_release_dogfood_lane(issue, lane))
    return tuple(merged)


def _lanes_from_glance(path: str) -> tuple[tuple[DrainLane, ...], bool]:
    """Derive drain lanes from a ``workflow glance --json`` envelope. ``(lanes, complete)``.

    The canonical ``workflow glance --json`` producer ALWAYS emits ``rows`` and an exact-bool
    ``degraded`` (``domain/workflow_glance.py::glance_payload``) and always appends
    ``lifecycle_diagnostic`` (``cli_workflow_glance.py``). So this reader treats those three
    keys as REQUIRED with exact types (Redmine #13967 R6-F2): a missing / present-null /
    wrong-type ``rows`` / ``lifecycle_diagnostic`` (must be a list) or ``degraded`` (must be an
    exact bool), or a ``degraded=True`` envelope, makes the projection durable-incomplete
    (-> hold). Only the canonical empty envelope (both lists present + ``degraded: false``)
    with no unreadable row is ``complete``. An unreadable row (non-dict / non-string identity
    or state) also makes it incomplete rather than being silently dropped (R2-F2).

    Beyond identity / state, the reader validates the canonical row's **delivery-anomaly**
    dimension and the envelope's **cardinality / active-issue ownership** (Redmine #13967
    R9): each row's ``delivery_anomaly`` (closed :data:`DELIVERY_ANOMALIES` vocab),
    ``delivery_anomaly_stale`` (exact bool) and ``has_active_anomaly`` (exact bool, must equal
    ``anomaly != none and not stale``); the envelope ``count`` (exact int == ``len(rows)``) and
    ``active_anomaly_issues`` (a duplicate-free string list whose set equals the row-derived
    active-anomaly issues); and active-issue uniqueness (one active row per ISSUE). Any breach
    is a contradictory canonical envelope -> durable-incomplete (hold). A live (non-stale)
    anomaly flags its lane ``delivery_anomaly_active`` (which forces a hold) WITHOUT rewinding
    the durable ``state_class`` (the glance non-rollback invariant)."""
    try:
        data = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--from-glance {path!r} could not be read as JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("--from-glance must carry a workflow glance --json envelope")

    complete = True
    # `rows` / `lifecycle_diagnostic` are REQUIRED lists (a missing key, present null, or
    # non-list is a partially-read glance envelope -> durable-incomplete, R6-F2).
    rows = data.get("rows", _MISSING)
    if not isinstance(rows, list):
        complete = False
        rows = []
    diagnostics = data.get("lifecycle_diagnostic", _MISSING)
    if not isinstance(diagnostics, list):
        complete = False
        diagnostics = []
    # `degraded` is a REQUIRED exact bool. A missing / present-null / falsey-non-bool value
    # (e.g. `0`), or an explicit `degraded: true`, means the glance source was not confirmed
    # healthy -> durable-incomplete (R6-F2). Only an exact `degraded: false` clears it.
    degraded = data.get("degraded", _MISSING)
    if not isinstance(degraded, bool) or degraded is True:
        complete = False

    active: list[DrainLane] = []
    active_keys: set[tuple[str, str]] = set()
    active_issues: set[str] = set()
    derived_anomaly_issues: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            complete = False
            continue
        issue = _exact_str(row.get("issue_id", _MISSING), required=True)
        state = _exact_str(row.get("workflow_state", _MISSING), required=True)
        # The canonical WorkflowGlanceRow emits both `workflow_state` AND `state_class`, and
        # its contract is that they are EQUAL. Validate the paired field too: a missing /
        # null / non-string / MISMATCHED state_class is a contradictory canonical row, so the
        # projection is durable-incomplete rather than trusting one side (Redmine #13967 R7-F1).
        state_class = _exact_str(row.get("state_class", _MISSING), required=True)
        lane = _exact_str(row.get("lane", _MISSING))
        next_owner = _exact_str(row.get("next_owner", _MISSING))
        if (
            issue is None
            or state is None
            or state_class is None
            or state_class != state
            or lane is None
            or next_owner is None
        ):
            # Non-string / missing identity or state, or a workflow_state/state_class conflict,
            # is malformed (never str-coerced) -> hold.
            complete = False
            continue
        # Delivery-anomaly dimension (Redmine #13967 R9-F1): the canonical WorkflowGlanceRow
        # always emits `delivery_anomaly` (a closed DELIVERY_ANOMALIES token), an exact-bool
        # `delivery_anomaly_stale`, and a derived exact-bool `has_active_anomaly` whose contract
        # is `has_active_anomaly == (delivery_anomaly != none and not stale)`. Validate all
        # three (exact type + closed vocab + mutual consistency); any breach is a contradictory
        # canonical row -> durable-incomplete (hold), never a silently dropped anomaly.
        anomaly = row.get("delivery_anomaly", _MISSING)
        stale = row.get("delivery_anomaly_stale", _MISSING)
        active_flag = row.get("has_active_anomaly", _MISSING)
        if (
            not isinstance(anomaly, str)
            or anomaly not in DELIVERY_ANOMALIES
            or not isinstance(stale, bool)
            or not isinstance(active_flag, bool)
            or active_flag != (anomaly != ANOMALY_NONE and not stale)
        ):
            complete = False
            continue
        # A live (non-stale) delivery anomaly re-owns the lane to the coordinator as a
        # transport-repair obligation. It NEVER rewinds the durable workflow_state (the glance
        # non-rollback invariant): the lane keeps its state_class and is additionally flagged
        # anomaly-active, which the projection folds into a fail-closed hold.
        key = (issue, lane)
        if key in active_keys:
            # A duplicated (issue, lane) active identity is a contradictory roster -> hold (R8-F1).
            complete = False
        if issue in active_issues:
            # The canonical active roster is one row per ISSUE (`active_lane_snapshots` dedups
            # to a single active row per issue). Two active rows for the same issue is ambiguous
            # ownership -> hold (Redmine #13967 R9-F2), even when their lanes differ.
            complete = False
        active_keys.add(key)
        active_issues.add(issue)
        if active_flag:
            derived_anomaly_issues.add(issue)
        active.append(
            DrainLane(
                issue=issue,
                lane=lane,
                state_class=state,
                next_action_owner=next_owner,
                delivery_anomaly_active=active_flag,
            )
        )
    # Envelope cardinality (Redmine #13967 R9-F2): the canonical producer always emits
    # `count == len(rows)`. A missing / present-null / non-int (or bool) / mismatched `count`
    # means the envelope disagrees with its own row roster -> durable-incomplete (hold).
    count = data.get("count", _MISSING)
    if isinstance(count, bool) or not isinstance(count, int) or count != len(rows):
        complete = False
    # Envelope `active_anomaly_issues` (Redmine #13967 R9-F1): the producer builds it as
    # `[r.issue_id for r in rows if r.has_active_anomaly]`. Validate it is a list of exact,
    # duplicate-free strings whose set equals the row-derived active-anomaly issue set — a
    # missing / non-list / non-string-member / duplicated / disagreeing summary is a
    # contradictory canonical envelope -> hold.
    env_anomalies = data.get("active_anomaly_issues", _MISSING)
    if (
        not isinstance(env_anomalies, list)
        or not all(isinstance(x, str) for x in env_anomalies)
        or len(env_anomalies) != len(set(env_anomalies))
        or set(env_anomalies) != derived_anomaly_issues
    ):
        complete = False
    release_rows: list[tuple[str, str]] = []
    diag_keys: set[tuple[str, str]] = set()
    for diag in diagnostics:
        if not isinstance(diag, dict):
            complete = False
            continue
        # The canonical producer emits issue / lane / lane_disposition / process_release on
        # EVERY diagnostic row (non-active lanes only). Validate all four BEFORE deciding
        # whether the row is a pending release: an unknown / missing / null / non-string
        # process_release, an out-of-vocabulary lane_disposition (or an `active` one — the
        # diagnostic roster is non-active by contract), or missing identity is a malformed /
        # contradictory canonical row -> durable-incomplete (hold), never read as "no release
        # debt" (Redmine #13967 R7-F2). Only a fully-valid requested/partial row folds as a
        # pending release lane.
        d_issue = _exact_str(diag.get("issue", _MISSING), required=True)
        d_lane = _exact_str(diag.get("lane", _MISSING), required=True)
        d_disposition = _exact_str(diag.get("lane_disposition", _MISSING), required=True)
        pr = _exact_str(diag.get("process_release", _MISSING), required=True)
        if (
            d_issue is None
            or d_lane is None
            or d_disposition is None
            or d_disposition not in _NON_ACTIVE_DISPOSITIONS
            or pr is None
            or pr not in RELEASE_STATES
        ):
            complete = False
            continue
        key = (d_issue, d_lane)
        if key in diag_keys:
            # The same lane appearing twice in the diagnostic (with possibly conflicting
            # disposition / release) is contradictory durable state -> hold (R8-F1).
            complete = False
        diag_keys.add(key)
        if pr in _RELEASE_PENDING:
            release_rows.append(key)
    # Collection-level identity invariant (Redmine #13967 R8-F1): the active roster and the
    # non-active lifecycle-diagnostic roster are disjoint by contract — a lane is active XOR
    # non-active, never both. A `(issue, lane)` in BOTH sets is a contradictory / unreadable
    # durable state, so the projection is durable-incomplete (-> hold), never releasable.
    if active_keys & diag_keys:
        complete = False
    return _merge_release_pending(active, release_rows), complete


def _lanes_live(repo_root: Path) -> tuple[tuple[DrainLane, ...], bool, tuple[str, ...]]:
    """Best-effort live enumeration folded through the glance read model.

    Returns ``(lanes, degraded, notes)``. Fail-open: an unreadable roster / diagnostic is
    reported as degraded (never a silent empty). Without the durable Redmine fold a lane may
    read ``unknown`` — ``--from-glance`` (which folds the Redmine record) is the richer path.
    """
    from mozyo_bridge.core.state.workflow_runtime_store import (
        WorkflowRuntimeStore,
        workflow_runtime_store_path,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (
        active_lane_snapshots,
        enumerate_active_lanes,
        enumerate_lifecycle_diagnostic,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (
        fold_glance_rows,
    )

    notes: list[str] = []
    degraded = False

    roster, roster_error = enumerate_active_lanes(repo_root)
    if roster_error:
        degraded = True
        notes.append(roster_error)
    store = WorkflowRuntimeStore(path=workflow_runtime_store_path())
    collection = active_lane_snapshots(
        roster,
        redmine_source=None,
        store=store,
        ledger=None,
        reconcile_store=None,
        authority_index={},
    )
    notes.extend(collection.notes)
    degraded = degraded or collection.degraded
    rows = fold_glance_rows(collection.snapshots)

    active: list[DrainLane] = [
        DrainLane(
            issue=r.issue_id,
            lane=r.lane,
            state_class=r.workflow_state,
            next_action_owner=r.next_owner,
            # A live (non-stale) delivery anomaly holds the process here too (Redmine #13967
            # R9-F1) — same read model, same non-rollback transport-repair signal.
            delivery_anomaly_active=r.has_active_anomaly,
        )
        for r in rows
    ]

    diagnostic, diag_error = enumerate_lifecycle_diagnostic(repo_root)
    if diag_error:
        degraded = True
        notes.append(diag_error)
    release_rows = [
        (issue, lane)
        for issue, lane, _disposition, process_release in diagnostic
        if process_release in _RELEASE_PENDING
    ]
    return _merge_release_pending(active, release_rows), degraded, tuple(notes)


def cmd_workflow_drain_queue(args: argparse.Namespace) -> int:
    """Project the active lane set into the bucketed drain queue + retention verdict.

    Read-only: mutates nothing and always returns 0 — the output is a projection, not a
    delivery. A degraded source (live mode) is reported, never silently read as "nothing to
    drain".
    """
    snapshot = (getattr(args, "snapshot_json", None) or "").strip()
    from_glance = (getattr(args, "from_glance", None) or "").strip()
    degraded = False
    notes: tuple[str, ...] = ()

    if snapshot:
        lanes, complete = _lanes_from_snapshot(snapshot)
        degraded = not complete
    elif from_glance:
        # `_lanes_from_glance` fully validates the envelope (required rows / lifecycle_
        # diagnostic lists + an exact-bool `degraded: false`, and every row's exact-type
        # identity), so `complete` already reflects source degradation and any unreadable
        # row — do not release from a partially-read glance envelope (Redmine #13967 R6-F2).
        lanes, complete = _lanes_from_glance(from_glance)
        degraded = not complete
    else:
        repo = getattr(args, "repo", None)
        repo_root = Path(repo).expanduser() if repo else Path.cwd()
        lanes, degraded, notes = _lanes_live(repo_root)

    # Fail-closed: a live/glance source that could not be fully read holds the process
    # (durable_complete=False), so the retention verdict never says `releasable` from state
    # it could not read. A caller-supplied --snapshot-json is treated as complete.
    projection = project_drain_queue(lanes, durable_complete=not degraded)
    if getattr(args, "as_json", False):
        payload = drain_queue_payload(projection)
        payload["degraded"] = bool(degraded)
        payload["notes"] = list(notes)
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_drain_queue_table(projection))
        if degraded:
            print("")
            print("degraded: some lane sources were unavailable/unrecognized:")
            for note in notes:
                print(f"  - {note}")
    return 0


def register_drain_queue(workflow_sub) -> None:
    """Register ``workflow drain-queue`` onto the ``workflow`` subparser (Redmine #13967)."""
    drain = workflow_sub.add_parser(
        "drain-queue",
        description=(
            "Read-only projection of the coordinator dependency drain queue (Redmine "
            "#13967): bucket every active lane into callback / review / owner / integration "
            "/ close / blocked / retirement / release-dogfood (the spine ### Drain Order + "
            "the delegated dogfood bucket), tag each bucket with its "
            "coordinator_actionable / delegated_in_flight / non_actionable_wait ownership "
            "split, and emit one process_retention verdict (hold | releasable) so a "
            "review-approved + integrated lane can decide whether to keep an active process "
            "or hibernate early. It reuses the workflow glance / fill-decision read model "
            "(no second state machine) and mutates nothing. Sources: --snapshot-json (a "
            "structured lane list), --from-glance (a workflow glance --json envelope), or "
            "the default best-effort live enumeration. Actionability fails closed to "
            "coordinator_actionable unless a structured lane supplies an earned claim. "
            "Always exits 0."
        ),
        help=(
            "Read-only drain-queue projection: buckets + actionable/non-actionable "
            "ownership + a hold|releasable process-retention verdict for the "
            "early-hibernate decision. Mutates nothing; never blocks."
        ),
    )
    drain.add_argument(
        "--snapshot-json",
        dest="snapshot_json",
        default=None,
        metavar="PATH",
        help=(
            "Read a structured lane list: {\"lanes\": [ {issue, state_class, actionability, "
            "next_action_owner, lane, release_pending, reason}, ... ]} (a bare list is also "
            "accepted). The deterministic contract surface — structured facts only."
        ),
    )
    drain.add_argument(
        "--from-glance",
        dest="from_glance",
        default=None,
        metavar="PATH",
        help=(
            "Derive drain lanes from a `workflow glance --json` envelope (the richest live "
            "path: the glance fold already read the durable Redmine record). Each active row "
            "folds to a drain lane; each lifecycle_diagnostic row whose release axis is "
            "requested/partial folds to a delegated release_dogfood lane."
        ),
    )
    drain.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit exactly one structured envelope as JSON (per-bucket ownership split + the "
            "process_retention verdict + hold_buckets)."
        ),
    )
    drain.set_defaults(func=cmd_workflow_drain_queue)


__all__ = ("cmd_workflow_drain_queue", "register_drain_queue")
