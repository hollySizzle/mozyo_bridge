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


def _finding_from_mapping(raw: object) -> tuple[SubsystemFinding | None, str]:
    """``(finding, subsystem)``. finding is None when the entry is malformed; the subsystem
    (if extractable) is returned so the caller can fail it closed to escalation rather than
    silently dropping it (Redmine #13967 F4 / R2-F3).

    ``authority_bearing``, ``late`` and ``round_index`` are **required and exact-typed**: a
    missing key, or a value that is not an exact JSON bool (``"false"`` must not coerce to
    True) / exact int, makes the entry malformed. No coercion, no defaulting of a required
    authority field to a valid ``false``."""
    if not isinstance(raw, dict):
        return None, ""
    subsystem = str(raw.get("subsystem", "") or "").strip()
    if not subsystem:
        return None, ""
    ri = raw.get("round_index")
    if isinstance(ri, bool) or not isinstance(ri, int):
        return None, subsystem  # round_index required, exact int (not bool/str/float/missing)
    authority = raw.get("authority_bearing")
    late = raw.get("late")
    if not isinstance(authority, bool) or not isinstance(late, bool):
        # Required authority flags must be present exact bools — a missing key is NOT a
        # valid `false`, and a string is NOT coerced (Redmine #13967 R2-F3).
        return None, subsystem
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


# Tri-state escalation decision (Redmine #13967 R2-F3). `indeterminate` is distinct from
# `no_escalation` so an absent / unprovenanced / partially-malformed history can NEVER read
# as a confident "no escalation" verdict — only an evaluated history that genuinely produced
# no escalation is `no_escalation`.
DECISION_ESCALATE = "escalate"
DECISION_NO_ESCALATION = "no_escalation"
DECISION_INDETERMINATE = "indeterminate"


def cmd_workflow_review_escalation(args: argparse.Namespace) -> int:
    """Project the deterministic late-finding escalation decision. Read-only; always exits 0.

    Authority-bearing gate (Redmine #13967 R2-F3): the decision is only ``escalate`` /
    ``no_escalation`` when the history was **provided with a declared provenance** (a durable
    anchor / verified source) and no fatal indeterminacy was hit. Absent history, missing
    provenance, a non-list ``findings``, or a malformed entry with no nameable subsystem all
    resolve to ``indeterminate`` — never a confident ``no_escalation``.
    """
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

    provenance = ""
    indeterminate_reasons: list[str] = []
    if isinstance(data, dict):
        raw_entries = data.get("findings", [])
        snapshot_unreadable = data.get("unreadable_subsystems", []) or []
        snapshot_threshold = data.get("threshold")
        provenance = str(data.get("provenance", "") or "").strip()
    else:
        raw_entries = data
        snapshot_unreadable = []
        snapshot_threshold = None

    if not history_provided:
        indeterminate_reasons.append("no_history_provided")
    if history_provided and not provenance:
        # Authority-bearing projection requires a declared durable/verified provenance.
        indeterminate_reasons.append("no_snapshot_provenance")
    if history_provided and not isinstance(raw_entries, list):
        indeterminate_reasons.append("findings_not_a_list")
        raw_entries = []

    findings: list = []
    malformed_subsystems: list[str] = []
    for raw_f in raw_entries or []:
        f, subsystem = _finding_from_mapping(raw_f)
        if f is not None:
            findings.append(f)
        elif subsystem:
            # Malformed entry with a nameable subsystem -> that subsystem fails CLOSED to
            # escalation (never silently dropped).
            malformed_subsystems.append(subsystem)
        else:
            # Malformed entry we cannot even attribute to a subsystem makes the whole
            # projection indeterminate (we cannot claim we read the history).
            indeterminate_reasons.append("unattributable_malformed_entry")

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
    evaluated = not indeterminate_reasons
    if not evaluated:
        decision = DECISION_INDETERMINATE
    elif projection.any_escalation:
        decision = DECISION_ESCALATE
    else:
        decision = DECISION_NO_ESCALATION

    payload = projection.as_payload()
    payload["history_provided"] = history_provided
    payload["provenance"] = provenance
    payload["evaluated"] = evaluated
    payload["escalation_decision"] = decision
    payload["indeterminate_reasons"] = sorted(set(indeterminate_reasons))
    if getattr(args, "as_json", False):
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_review_escalation_table(projection))
        print("")
        if decision == DECISION_INDETERMINATE:
            print(
                "escalation_decision: indeterminate — the review history was not evaluable "
                "(" + ", ".join(sorted(set(indeterminate_reasons))) + "). This is NOT a "
                "verdict of 'no escalation'; supply a provenanced, well-formed history."
            )
        elif decision == DECISION_ESCALATE:
            print(
                "escalation_decision: escalate next round to full-surface adversarial "
                "sweep: " + ", ".join(projection.escalating_subsystems)
            )
        else:
            print("escalation_decision: no_escalation (evaluated with provenance)")
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
            "Read the per-subsystem review history: {\"provenance\": \"<durable anchor / "
            "verified source>\", \"findings\": [ {subsystem, round_index, authority_bearing, "
            "late, finding_id}, ... ], \"unreadable_subsystems\": [...], \"threshold\": N}. A "
            "non-empty `provenance` is REQUIRED for an evaluable verdict (without it, or with "
            "no snapshot, the decision is `indeterminate`, never `no_escalation`). "
            "authority_bearing / late / round_index are required exact-typed per finding. "
            "Structured facts only."
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
