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
    RELEASE_PARTIAL,
    RELEASE_REQUESTED,
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

# The release-axis values that mean a centralized TestPyPI / installed dogfood is still
# owed on the dedicated release issue (Redmine #13967 item 2).
_RELEASE_PENDING = frozenset({RELEASE_REQUESTED, RELEASE_PARTIAL})


def _exact_str(value: object, *, required: bool = False) -> str | None:
    """Return the stripped string ONLY when ``value`` is an exact ``str`` (Redmine #13967 R4-F2).

    A non-string (dict / list / number / None) is NEVER coerced via ``str(...)`` — it
    returns None so the caller can treat the field as malformed. When ``required`` a
    present-but-empty string also returns None. An absent/optional field returns "".
    """
    if value is None:
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
    issue = _exact_str(raw.get("issue"), required=True)
    state_class = _exact_str(raw.get("state_class"), required=True)
    lane = _exact_str(raw.get("lane"))
    next_owner = _exact_str(raw.get("next_action_owner"))
    reason = _exact_str(raw.get("reason"))
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
    """Derive drain lanes from a ``workflow glance --json`` envelope. ``(lanes, complete)``:
    ``complete`` is False when a row could not be read (a non-dict, a missing issue_id, or a
    row with no workflow_state) — an unreadable row makes the projection durable-incomplete
    rather than being silently dropped (Redmine #13967 R2-F2)."""
    try:
        data = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--from-glance {path!r} could not be read as JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("--from-glance must carry a workflow glance --json envelope")

    complete = True
    # A non-list `rows` / `lifecycle_diagnostic` is a malformed envelope: it makes the
    # projection durable-incomplete (-> hold) instead of crashing on iteration (R4-F2).
    rows = data.get("rows", [])
    if not isinstance(rows, list):
        complete = False
        rows = []
    diagnostics = data.get("lifecycle_diagnostic", [])
    if not isinstance(diagnostics, list):
        complete = False
        diagnostics = []

    active: list[DrainLane] = []
    for row in rows:
        if not isinstance(row, dict):
            complete = False
            continue
        issue = _exact_str(row.get("issue_id"), required=True)
        state = _exact_str(row.get("workflow_state"), required=True)
        lane = _exact_str(row.get("lane"))
        next_owner = _exact_str(row.get("next_owner"))
        if issue is None or state is None or lane is None or next_owner is None:
            # Non-string / missing identity or state is malformed (never str-coerced) -> hold.
            complete = False
            continue
        active.append(
            DrainLane(
                issue=issue, lane=lane, state_class=state, next_action_owner=next_owner
            )
        )
    release_rows: list[tuple[str, str]] = []
    for diag in diagnostics:
        if not isinstance(diag, dict):
            complete = False
            continue
        pr = _exact_str(diag.get("process_release"))
        if pr is None or pr not in _RELEASE_PENDING:
            # A non-string process_release, or one not in the pending set, is not a release
            # row (a non-string is malformed -> hold; a known non-pending value is skipped).
            if pr is None:
                complete = False
            continue
        d_issue = _exact_str(diag.get("issue"), required=True)
        d_lane = _exact_str(diag.get("lane"), required=True)
        if d_issue is None or d_lane is None:
            # A release-pending diagnostic row with no exact-string durable identity must NOT
            # become a phantom release_dogfood lane that reads `releasable` — it makes the
            # projection durable-incomplete (-> hold) instead (Redmine #13967 R3-F2 / R4-F2).
            complete = False
            continue
        release_rows.append((d_issue, d_lane))
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
        lanes, complete = _lanes_from_glance(from_glance)
        # A glance envelope that reports its own source degradation, OR that carried an
        # unreadable row, makes the drain projection durable-incomplete too (Redmine #13967
        # F2 / R2-F2 — do not release from a partially-read durable record).
        degraded = not complete
        try:
            glance_env = _json.loads(Path(from_glance).read_text(encoding="utf-8"))
            degraded = degraded or bool(
                isinstance(glance_env, dict) and glance_env.get("degraded")
            )
        except (OSError, ValueError):
            degraded = True
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
