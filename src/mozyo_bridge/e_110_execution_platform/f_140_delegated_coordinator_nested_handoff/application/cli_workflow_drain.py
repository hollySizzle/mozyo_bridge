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


def _lane_from_mapping(raw: object) -> DrainLane | None:
    """Build a :class:`DrainLane` from a structured mapping (fail-closed)."""
    if not isinstance(raw, dict):
        return None
    issue = str(raw.get("issue", "") or "").strip()
    state_class = str(raw.get("state_class", "") or "").strip()
    if not issue or not state_class:
        return None
    return DrainLane(
        issue=issue,
        state_class=state_class,
        actionability=str(
            raw.get("actionability", ACTIONABILITY_COORDINATOR_ACTIONABLE) or ""
        ).strip()
        or ACTIONABILITY_COORDINATOR_ACTIONABLE,
        next_action_owner=str(raw.get("next_action_owner", "") or "").strip(),
        lane=str(raw.get("lane", "") or "").strip(),
        release_pending=bool(raw.get("release_pending", False)),
        reason=str(raw.get("reason", "") or "").strip(),
    )


def _lanes_from_snapshot(path: str) -> tuple[DrainLane, ...]:
    try:
        data = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--snapshot-json {path!r} could not be read as JSON: {exc}") from exc
    entries = data.get("lanes", data) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise SystemExit("--snapshot-json must carry a list of lanes (or a {'lanes': [...]})")
    lanes = [lane for raw in entries if (lane := _lane_from_mapping(raw)) is not None]
    return tuple(lanes)


def _lanes_from_glance(path: str) -> tuple[DrainLane, ...]:
    """Derive drain lanes from a ``workflow glance --json`` envelope."""
    try:
        data = _json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--from-glance {path!r} could not be read as JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("--from-glance must carry a workflow glance --json envelope")

    lanes: list[DrainLane] = []
    for row in data.get("rows", []) or []:
        if not isinstance(row, dict):
            continue
        issue = str(row.get("issue_id", "") or "").strip()
        if not issue:
            continue
        lanes.append(
            DrainLane(
                issue=issue,
                lane=str(row.get("lane", "") or "").strip(),
                state_class=str(row.get("workflow_state", "") or "").strip(),
                next_action_owner=str(row.get("next_owner", "") or "").strip(),
            )
        )
    for diag in data.get("lifecycle_diagnostic", []) or []:
        if not isinstance(diag, dict):
            continue
        if str(diag.get("process_release", "") or "").strip() not in _RELEASE_PENDING:
            continue
        issue = str(diag.get("issue", "") or "").strip()
        lanes.append(
            DrainLane(
                issue=issue,
                lane=str(diag.get("lane", "") or "").strip(),
                state_class=LANE_STATE_IDLE,
                actionability=ACTIONABILITY_NON_ACTIONABLE_WAIT,
                next_action_owner="external_condition",
                release_pending=True,
                reason="release_dogfood_delegated_to_release_issue",
            )
        )
    return tuple(lanes)


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

    lanes: list[DrainLane] = [
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
    for issue, lane, _disposition, process_release in diagnostic:
        if process_release not in _RELEASE_PENDING:
            continue
        lanes.append(
            DrainLane(
                issue=issue,
                lane=lane,
                state_class=LANE_STATE_IDLE,
                actionability=ACTIONABILITY_NON_ACTIONABLE_WAIT,
                next_action_owner="external_condition",
                release_pending=True,
                reason="release_dogfood_delegated_to_release_issue",
            )
        )
    return tuple(lanes), degraded, tuple(notes)


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
        lanes = _lanes_from_snapshot(snapshot)
    elif from_glance:
        lanes = _lanes_from_glance(from_glance)
    else:
        repo = getattr(args, "repo", None)
        repo_root = Path(repo).expanduser() if repo else Path.cwd()
        lanes, degraded, notes = _lanes_live(repo_root)

    projection = project_drain_queue(lanes)
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
