"""CLI for `workflow review-escalation` — late-finding sweep trigger (Redmine #13967 item 3).

`mozyo-bridge workflow review-escalation` is the read-only, deterministic projection an
auditor / coordinator runs to decide whether a subsystem's **next** review round must be
promoted from per-finding re-review to a full-surface adversarial sweep. It reads the
per-subsystem review-round history (which rounds carried a *late authority finding*) and
emits, per subsystem, the escalation verdict + the next review mode.

It mutates nothing and computes purely from structured facts the caller extracted from the
durable Redmine review journals (structured facts only — no prose is parsed). This never
weakens review / close authority: escalation only *adds* review scope.

Source: ``--snapshot-json PATH`` — ``{"findings": [ {subsystem, round_index,
authority_bearing, late, finding_id}, ... ], "unreadable_subsystems": [ ... ],
"threshold": N}`` (a bare findings list is also accepted; ``--threshold`` /
``--unreadable-subsystem`` override / extend it).
"""

from __future__ import annotations

import argparse
import json as _json
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_escalation import (
    DEFAULT_ESCALATION_THRESHOLD,
    SubsystemFinding,
    project_review_escalation,
    render_review_escalation_table,
)


def _exact_bool(value: object, default: bool = False) -> bool | None:
    """Return the value only when it is an EXACT JSON bool (Redmine #13967 F4).

    A missing key takes ``default``; a present-but-non-bool value (e.g. the string
    ``"false"``, which ``bool(...)`` would coerce to True) returns None so the caller can
    treat the entry as malformed rather than silently coercing it. No coercion.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return None


def _finding_from_mapping(raw: object) -> tuple[SubsystemFinding | None, str]:
    """``(finding, subsystem)``. finding is None when the entry is malformed; the subsystem
    (if extractable) is returned so the caller can fail it closed to escalation rather than
    silently dropping it (Redmine #13967 F4)."""
    if not isinstance(raw, dict):
        return None, ""
    subsystem = str(raw.get("subsystem", "") or "").strip()
    if not subsystem:
        return None, ""
    ri = raw.get("round_index", 0)
    if isinstance(ri, bool) or not isinstance(ri, int):
        return None, subsystem  # round_index must be an exact int (not bool/str/float)
    authority = _exact_bool(raw.get("authority_bearing"))
    late = _exact_bool(raw.get("late"))
    if authority is None or late is None:
        return None, subsystem  # non-bool flag -> malformed, fail closed
    return (
        SubsystemFinding(
            subsystem=subsystem,
            round_index=ri,
            authority_bearing=authority,
            late=late,
            finding_id=str(raw.get("finding_id", "") or "").strip(),
        ),
        subsystem,
    )


def cmd_workflow_review_escalation(args: argparse.Namespace) -> int:
    """Project the deterministic late-finding escalation verdict. Read-only; always exits 0."""
    raw = (getattr(args, "snapshot_json", None) or "").strip()
    history_provided = bool(raw)
    data: object = {}
    if raw:
        try:
            data = _json.loads(Path(raw).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(
                f"--snapshot-json {raw!r} could not be read as JSON: {exc}"
            ) from exc

    if isinstance(data, dict):
        entries = data.get("findings", [])
        snapshot_unreadable = data.get("unreadable_subsystems", []) or []
        snapshot_threshold = data.get("threshold")
    else:
        entries = data
        snapshot_unreadable = []
        snapshot_threshold = None

    findings: list = []
    malformed_subsystems: list[str] = []
    for raw_f in entries or []:
        f, subsystem = _finding_from_mapping(raw_f)
        if f is not None:
            findings.append(f)
        elif subsystem:
            # A malformed entry with a nameable subsystem fails that subsystem CLOSED to
            # escalation rather than being silently dropped (Redmine #13967 F4).
            malformed_subsystems.append(subsystem)

    cli_threshold = getattr(args, "threshold", None)
    threshold = (
        int(cli_threshold)
        if cli_threshold is not None
        else (int(snapshot_threshold) if snapshot_threshold is not None else DEFAULT_ESCALATION_THRESHOLD)
    )

    unreadable = (
        list(snapshot_unreadable)
        + list(getattr(args, "unreadable_subsystem", None) or [])
        + malformed_subsystems
    )
    unreadable = [str(s).strip() for s in unreadable if str(s).strip()]

    projection = project_review_escalation(
        findings, threshold=threshold, unreadable_subsystems=unreadable
    )
    payload = projection.as_payload()
    payload["history_provided"] = history_provided
    if getattr(args, "as_json", False):
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_review_escalation_table(projection))
        if not history_provided:
            # An authority-bearing projection must not read as a confident "no escalation"
            # when no review history was supplied at all (Redmine #13967 F4).
            print("")
            print(
                "no readable review history provided (--snapshot-json absent); this is not "
                "a verdict of 'no escalation'. Supply a durable/verified history to evaluate."
            )
        elif projection.any_escalation:
            print("")
            print(
                "escalate next round to full-surface adversarial sweep: "
                + ", ".join(projection.escalating_subsystems)
            )
    return 0


def register_review_escalation(workflow_sub) -> None:
    """Register ``workflow review-escalation`` onto the ``workflow`` subparser (Redmine #13967)."""
    esc = workflow_sub.add_parser(
        "review-escalation",
        description=(
            "Read-only deterministic trigger (Redmine #13967 item 3): given the per-subsystem "
            "review-round history (which rounds carried a late authority finding), decide "
            "whether the next round must escalate from per-finding re-review to a full-surface "
            "adversarial sweep. escalate iff the count of distinct rounds with a late "
            "authority finding >= --threshold (default 2 = 'repeated'). Only late AND "
            "authority-bearing findings count; a round counts once. An unreadable subsystem "
            "history fails toward escalation (stricter, never a bypass). Never weakens "
            "review / close authority. Read-only; always exits 0."
        ),
        help=(
            "Deterministic late-finding -> full-surface adversarial review escalation "
            "trigger/projection. Read-only; never blocks."
        ),
    )
    esc.add_argument(
        "--snapshot-json",
        dest="snapshot_json",
        default=None,
        metavar="PATH",
        help=(
            "Read the per-subsystem review history: {\"findings\": [ {subsystem, round_index, "
            "authority_bearing, late, finding_id}, ... ], \"unreadable_subsystems\": [...], "
            "\"threshold\": N} (a bare findings list is also accepted). Structured facts only."
        ),
    )
    esc.add_argument(
        "--threshold",
        dest="threshold",
        type=int,
        default=None,
        help=(
            "Number of distinct late-authority-finding rounds that triggers escalation "
            "(default 2). Overrides a threshold in the snapshot."
        ),
    )
    esc.add_argument(
        "--unreadable-subsystem",
        dest="unreadable_subsystem",
        action="append",
        metavar="SUBSYSTEM",
        help=(
            "Mark a subsystem whose review history could not be read; it fails toward "
            "escalation (repeatable)."
        ),
    )
    esc.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit exactly one structured envelope as JSON.",
    )
    esc.set_defaults(func=cmd_workflow_review_escalation)


__all__ = ("cmd_workflow_review_escalation", "register_review_escalation")
