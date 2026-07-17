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
import re
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_escalation import (
    DEFAULT_ESCALATION_THRESHOLD,
    SubsystemFinding,
    project_review_escalation,
    render_review_escalation_table,
)

# A declared provenance must LOOK like a durable anchor that references an id, not any
# free-form non-empty string (Redmine #13967 R3-F3). It must be a string that names a
# ticket / journal reference: a `#<id>`, `j#<id>`, a `<scheme>:<...>:<digits>` anchor, or a
# recognized source word plus a digit. This is a shape gate, not proof the anchor resolves.
_PROVENANCE_ANCHOR = re.compile(
    r"(#\s*\d+|j#\s*\d+|\b(redmine|asana)\b[^\n]*\d+|:\s*\d+)", re.IGNORECASE
)


def _valid_provenance(value: object) -> bool:
    """True when ``value`` is a non-empty string shaped like a durable anchor (R3-F3)."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and bool(_PROVENANCE_ANCHOR.search(text))


def _finding_from_mapping(raw: object) -> tuple[SubsystemFinding | None, str]:
    """``(finding, subsystem)``. finding is None when the entry is malformed; the subsystem
    (if extractable) is returned so the caller can fail it closed to escalation rather than
    silently dropping it (Redmine #13967 F4 / R2-F3 / R3-F3).

    ``authority_bearing``, ``late`` and ``round_index`` are **required and exact-typed**: a
    missing key, or a value that is not an exact JSON bool (``"false"`` must not coerce to
    True) / an exact int ``>= 1`` (rounds are 1-based; ``0`` / negative is invalid), makes
    the entry malformed. No coercion, no defaulting of a required authority field to a valid
    ``false``."""
    if not isinstance(raw, dict):
        return None, ""
    subsystem = str(raw.get("subsystem", "") or "").strip()
    if not subsystem:
        return None, ""
    ri = raw.get("round_index")
    if isinstance(ri, bool) or not isinstance(ri, int) or ri < 1:
        return None, subsystem  # round_index required, exact int >= 1 (1-based rounds)
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

    provenance_raw: object = ""
    indeterminate_reasons: list[str] = []
    snapshot_threshold: object = None
    raw_entries: object = []
    snapshot_unreadable: object = []
    if isinstance(data, dict):
        raw_entries = data.get("findings", [])
        snapshot_unreadable = data.get("unreadable_subsystems", [])
        snapshot_threshold = data.get("threshold")
        provenance_raw = data.get("provenance", "")
    else:
        raw_entries = data
    provenance = provenance_raw.strip() if isinstance(provenance_raw, str) else ""

    if not history_provided:
        indeterminate_reasons.append("no_history_provided")
    if history_provided and not _valid_provenance(provenance_raw):
        # Authority-bearing projection requires a declared durable-anchor-shaped provenance
        # — a free-form or non-string value is not a verified source (Redmine #13967 R3-F3).
        indeterminate_reasons.append("no_or_invalid_provenance")
    if history_provided and not isinstance(raw_entries, list):
        indeterminate_reasons.append("findings_not_a_list")
        raw_entries = []
    # `unreadable_subsystems` must be a list; a non-list is a malformed envelope, reported
    # as indeterminate rather than crashing on `list(...)` (Redmine #13967 R3-F3d).
    if snapshot_unreadable in (None, ""):
        snapshot_unreadable = []
    if not isinstance(snapshot_unreadable, list):
        indeterminate_reasons.append("unreadable_subsystems_not_a_list")
        snapshot_unreadable = []

    findings: list = []
    malformed_subsystems: list[str] = []
    for raw_f in (raw_entries if isinstance(raw_entries, list) else []):
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

    # Threshold must be an exact int (a snapshot `"100"` string is NOT coerced — R3-F3c).
    cli_threshold = getattr(args, "threshold", None)  # argparse already enforces int
    threshold = DEFAULT_ESCALATION_THRESHOLD
    if cli_threshold is not None:
        threshold = int(cli_threshold)
    elif snapshot_threshold is not None:
        if isinstance(snapshot_threshold, bool) or not isinstance(snapshot_threshold, int):
            indeterminate_reasons.append("threshold_not_an_int")
        else:
            threshold = snapshot_threshold
    if threshold < 1:
        indeterminate_reasons.append("threshold_below_one")
        threshold = DEFAULT_ESCALATION_THRESHOLD

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
    # Redefine the boolean verdict so a legacy consumer keying on `any_escalation` can NEVER
    # read a confident False from an indeterminate history — it is True for BOTH `escalate`
    # and `indeterminate` (fail-closed toward review), only False for an evaluated
    # `no_escalation` (Redmine #13967 R3-F3a).
    payload["any_escalation"] = decision != DECISION_NO_ESCALATION
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
