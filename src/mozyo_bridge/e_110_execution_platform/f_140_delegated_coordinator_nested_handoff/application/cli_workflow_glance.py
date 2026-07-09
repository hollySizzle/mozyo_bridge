"""CLI surface for `workflow glance` — coordinator pipeline projection (Redmine #13435).

`mozyo-bridge workflow glance` is the single **read-only** command a coordinator runs
to see, for every active lane/US at once: its workflow state (folded from the durable
Redmine record), the next action + owner, and — critically — whether it is stuck in the
delivery layer. It exists because the motivating session's "the whole pipeline looks
stopped" was really "the work is done but a turn-start submit failed / a callback
self-looped", and the coordinator only found that by hand-correlating
``mozyo-bridge status`` + each Redmine journal + a ``herdr agent read`` pane.

Boundary with the neighbouring surfaces (design j#74172):

- ``status`` is the repo/session/tmux/doctor runtime snapshot; ``observe`` re-fetches
  runtime observation / freshness; ``workflow step`` executes exactly one safe workflow
  *mutation*. ``glance`` is none of these — it is a multi-lane **display** of the
  durable ``workflow_state + next_action + delivery_anomaly`` read model, and it mutates
  nothing (no Redmine write, no herdr send, no ``workflow step``).

Sources (design j#74172 split step 2):

- default / ``--active-lanes``: enumerate the active lanes from the persisted
  workflow-runtime store (the events ``workflow watch`` recorded) and join the herdr
  delivery ledger for the transport dimension (fail-open);
- ``--snapshot-json PATH``: read an already-composed structured snapshot (a coordinator
  / MCP sweep, or a postmortem fixture) — structured facts only, never parsed prose;
- ``--issue ID`` (repeatable): narrow the projection to specific issues (postmortem /
  test use).

The workflow state is folded from the durable record only, so a delivery anomaly can
never demote it — a "done but not delivered" lane still reads as review_waiting, flagged
with the anomaly and re-owned to the coordinator (the visible stall). A ``runtime_state``
sourced from a live pane read is tagged ``delivery_source=runtime_observation`` so the
reader knows it is a supplementary signal, not a durable gate.
"""

from __future__ import annotations

import argparse
import json as _json
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (
    MappingGlanceSnapshotSource,
    store_active_lane_snapshots,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (
    fold_glance_rows,
    glance_payload,
    render_glance_table,
)


def _snapshot_json_payload(args: argparse.Namespace):
    """Read the ``--snapshot-json`` structured payload, or None when the flag is absent."""
    raw = (getattr(args, "snapshot_json", None) or "").strip()
    if not raw:
        return None
    try:
        return _json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--snapshot-json {raw!r} could not be read as JSON: {exc}") from exc


def _store_from_args(args: argparse.Namespace):
    """Build the workflow-runtime store from ``--store-path`` (test/debug) or the home default."""
    # Lazy import so the glance CLI does not pull the store module in until an active-lane
    # enumeration actually needs it (the --snapshot-json path stays store-free).
    from mozyo_bridge.core.state.workflow_runtime_store import (
        WorkflowRuntimeStore,
        workflow_runtime_store_path,
    )

    raw = (getattr(args, "store_path", None) or "").strip()
    path = Path(raw) if raw else workflow_runtime_store_path()
    return WorkflowRuntimeStore(path=path)


def _ledger_from_args(args: argparse.Namespace):
    """Build the herdr delivery ledger, or None when disabled / unavailable (fail-open)."""
    if getattr(args, "no_ledger", False):
        return None
    from mozyo_bridge.core.state.herdr_delivery_ledger import (
        HerdrDeliveryLedger,
        herdr_delivery_ledger_path,
    )

    raw = (getattr(args, "ledger_path", None) or "").strip()
    try:
        path = Path(raw) if raw else herdr_delivery_ledger_path()
        return HerdrDeliveryLedger(path=path)
    except Exception:  # noqa: BLE001 - a missing/unreadable ledger degrades to no join
        return None


def _collect_snapshots(args: argparse.Namespace):
    """Collect issue snapshots from the requested sources (snapshot JSON and/or store).

    ``--snapshot-json`` supplies structured snapshots directly; the store enumeration is
    the default source (and is added explicitly when ``--active-lanes`` is combined with a
    snapshot file). Snapshot-JSON entries win over store entries for the same issue (the
    caller composed them deliberately). Every source read is fail-open.
    """
    payload = _snapshot_json_payload(args)
    active_lanes = bool(getattr(args, "active_lanes", False))

    snaps: list = []
    seen: set[str] = set()

    def _extend(candidates) -> None:
        for snap in candidates:
            if snap.issue_id in seen:
                continue
            seen.add(snap.issue_id)
            snaps.append(snap)

    if payload is not None:
        _extend(MappingGlanceSnapshotSource(payload).snapshots())

    # The store enumeration is the default source (no snapshot file), and is also included
    # when --active-lanes is asked for alongside a snapshot file.
    if payload is None or active_lanes:
        try:
            store = _store_from_args(args)
            ledger = _ledger_from_args(args)
            _extend(store_active_lane_snapshots(store, ledger=ledger))
        except Exception:  # noqa: BLE001 - enumeration is best-effort; never break the glance
            pass

    wanted = {i.strip() for i in (getattr(args, "issue", None) or []) if i.strip()}
    if wanted:
        snaps = [s for s in snaps if s.issue_id in wanted]
    return snaps


def cmd_workflow_glance(args: argparse.Namespace) -> int:
    """Project every active lane/US into workflow state + next action + delivery anomaly.

    Read-only: collects snapshots from the requested sources, folds each into a
    :class:`...domain.workflow_glance.WorkflowGlanceRow`, and emits one envelope (a
    fixed-width table, or the structured ``--json`` object). Mutates nothing and always
    returns 0 — the output is a projection, not a delivery.
    """
    rows = fold_glance_rows(_collect_snapshots(args))
    if getattr(args, "as_json", False):
        print(_json.dumps(glance_payload(rows), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_glance_table(rows))
    return 0


def register_glance(workflow_sub) -> None:
    """Register ``workflow glance`` onto the ``workflow`` subparser (Redmine #13435)."""
    glance = workflow_sub.add_parser(
        "glance",
        description=(
            "Project every active lane/US into a single read-only view: workflow_state "
            "(folded from the durable Redmine record) + next_action + next_owner + "
            "delivery_anomaly (Redmine #13435). It exists so a coordinator can spot at a "
            "glance the 'looks stopped but is really delivery-stuck' lanes (a done lane "
            "whose turn-start submit failed / whose callback self-looped) without "
            "hand-correlating status + each journal + a herdr pane read. Sources: the "
            "default / --active-lanes enumerates the persisted workflow-runtime store and "
            "joins the herdr delivery ledger; --snapshot-json reads an already-composed "
            "structured snapshot (never parsed prose); --issue narrows to specific issues. "
            "The workflow state comes from the durable record only, so a delivery anomaly "
            "never rolls it back; a runtime-observed signal is tagged "
            "delivery_source=runtime_observation. Read-only: no Redmine write, no herdr "
            "send, no workflow step; always exits 0."
        ),
        help=(
            "Read-only projection of every active lane/US: workflow_state + next_action + "
            "delivery_anomaly, so a 'done but delivery-stuck' lane reads as a visible "
            "stall. Mutates nothing; never blocks."
        ),
    )
    glance.add_argument(
        "--active-lanes",
        action="store_true",
        dest="active_lanes",
        help=(
            "Enumerate the active lanes from the persisted workflow-runtime store and join "
            "the herdr delivery ledger (the default source when no --snapshot-json is "
            "given; combine with --snapshot-json to include both)."
        ),
    )
    glance.add_argument(
        "--issue",
        action="append",
        dest="issue",
        metavar="ISSUE_ID",
        help=(
            "Narrow the projection to this Redmine issue id (repeatable). Postmortem / "
            "test use; omit to project every enumerated lane."
        ),
    )
    glance.add_argument(
        "--snapshot-json",
        dest="snapshot_json",
        default=None,
        metavar="PATH",
        help=(
            "Read an already-composed structured glance snapshot: {\"issues\": [ {issue, "
            "subject, lane, latest_gate, latest_gate_journal, review_conclusion, "
            "callback_state, commit_bearing, integration_recorded, issue_open, "
            "blocker_recorded, delivery: {anomaly, source, observed_journal, runtime_state, "
            "receive_method}}, ... ]} (a bare list is also accepted). Structured facts "
            "only — no note prose is parsed."
        ),
    )
    glance.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit exactly one structured envelope as JSON (per-row workflow_state / "
            "next_action / delivery_anomaly + a summary of the live-anomaly issues)."
        ),
    )
    glance.add_argument(
        "--no-ledger",
        action="store_true",
        dest="no_ledger",
        help="Skip the herdr delivery-ledger join (durable workflow state only).",
    )
    glance.add_argument(
        "--store-path",
        dest="store_path",
        default=None,
        help=argparse.SUPPRESS,  # test/debug override; default is the home store
    )
    glance.add_argument(
        "--ledger-path",
        dest="ledger_path",
        default=None,
        help=argparse.SUPPRESS,  # test/debug override; default is the home ledger
    )
    glance.set_defaults(func=cmd_workflow_glance)


__all__ = ("cmd_workflow_glance", "register_glance")
