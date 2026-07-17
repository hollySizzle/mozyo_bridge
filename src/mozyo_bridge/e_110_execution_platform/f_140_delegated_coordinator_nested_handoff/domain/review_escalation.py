"""Deterministic late-finding -> full-surface adversarial review escalation (Redmine #13967 item 3).

When the same subsystem keeps producing *late authority findings* across review rounds —
a blocking, authority-bearing defect that a per-finding re-review missed and that only
surfaced in a later round — continuing to re-review one finding at a time is a whack-a-mole
that repeatedly ships regressions (the pattern the memory names: "同じ authority fail-closed
面を 6+ review round 差分修正で追った → 不変条件を 1 構造で強制に切替"). The owner decision
(Redmine #13967) standardizes the escape: once late authority findings **repeat** on one
subsystem, the next round is promoted from per-finding re-review to a **full-surface
adversarial sweep** of that subsystem.

This module is the pure, deterministic **trigger + projection**. Given the per-subsystem
review-round history (which rounds produced a late authority finding), it returns, per
subsystem, whether the next round must escalate and to which review mode. It never weakens
review / close authority — escalation only *adds* review scope (Redmine #13967 acceptance:
既存 review/close authority を緩めない).

Invariants:

- **only late AND authority-bearing findings count.** A non-authority finding (taste /
  style) or a finding caught in the first round is not a late authority finding and never
  drives escalation. A round is counted **once** no matter how many qualifying findings it
  carries (distinct ``round_index``).
- **the trigger is deterministic.** ``escalate`` iff the count of distinct rounds with a
  late authority finding on the subsystem ``>= threshold`` (default
  :data:`DEFAULT_ESCALATION_THRESHOLD` = 2 — "repeated"). No wall-clock, no heuristic.
- **fail toward more review, never less.** A subsystem whose round history is supplied as
  unreadable is surfaced as :data:`REASON_HISTORY_UNREADABLE` and escalated (the safe
  direction — a full-surface sweep is stricter, never a bypass). Escalation is never
  fabricated from *no* qualifying findings, only from an explicitly unreadable history.

The application layer (``cli_workflow_review_escalation``) reads the durable Redmine review
journals into these value objects; this module guesses nothing. The round / late-finding
substrate is :mod:`...domain.review_generation` (unique generations + newer-unresolved-
blocking-finding detection); this module counts *repetition across* those generations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Review mode vocabulary (machine-readable; literal regardless of UI language).
# ---------------------------------------------------------------------------

MODE_PER_FINDING_REREVIEW = "per_finding_rereview"
MODE_FULL_SURFACE_ADVERSARIAL = "full_surface_adversarial"

REVIEW_MODES = frozenset({MODE_PER_FINDING_REREVIEW, MODE_FULL_SURFACE_ADVERSARIAL})

# "Repeated" = at least two rounds with a late authority finding on the same subsystem.
DEFAULT_ESCALATION_THRESHOLD = 2

# ---------------------------------------------------------------------------
# Reason codes (closed vocab).
# ---------------------------------------------------------------------------

REASON_NO_LATE_AUTHORITY_FINDING = "no_late_authority_findings"
REASON_BELOW_THRESHOLD = "late_authority_findings_below_threshold"
REASON_REPEATED_LATE_AUTHORITY = "repeated_late_authority_findings"
REASON_HISTORY_UNREADABLE = "subsystem_review_history_unreadable"


# ---------------------------------------------------------------------------
# Inputs / outputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubsystemFinding:
    """One review finding read from the durable record (fail-closed defaults).

    - ``subsystem`` — the surface the finding is against (a module / authority face).
    - ``round_index`` — the review round the finding was recorded in (1-based; a larger
      index is a later round).
    - ``authority_bearing`` — the finding touches a workflow / routing / approval / send-
      safety / fail-closed **authority**, not taste or style. Only authority-bearing
      findings drive escalation.
    - ``late`` — the finding surfaced *after* a prior round on the same subsystem (a
      per-finding re-review missed it). The caller derives this from the durable record
      (e.g. a blocking finding in round N>1 on a previously-reviewed subsystem, or the
      :func:`...review_generation.evaluate_approval_admissible` newer-unresolved-blocking
      signal).
    - ``finding_id`` — display / audit pointer (advisory).
    """

    subsystem: str
    round_index: int
    authority_bearing: bool = False
    late: bool = False
    finding_id: str = ""

    @property
    def counts_toward_escalation(self) -> bool:
        return self.authority_bearing and self.late


@dataclass(frozen=True)
class SubsystemEscalationVerdict:
    """The deterministic escalation verdict for one subsystem (pure)."""

    subsystem: str
    late_authority_rounds: tuple[int, ...]
    late_authority_round_count: int
    threshold: int
    escalate: bool
    next_round_mode: str
    reason: str

    def as_payload(self) -> dict[str, object]:
        return {
            "subsystem": self.subsystem,
            "late_authority_rounds": list(self.late_authority_rounds),
            "late_authority_round_count": self.late_authority_round_count,
            "threshold": self.threshold,
            "escalate": self.escalate,
            "next_round_mode": self.next_round_mode,
            "reason": self.reason,
        }


def evaluate_subsystem_escalation(
    subsystem: str,
    findings: Iterable[SubsystemFinding],
    *,
    threshold: int = DEFAULT_ESCALATION_THRESHOLD,
    history_readable: bool = True,
) -> SubsystemEscalationVerdict:
    """Decide whether ``subsystem``'s next review round must escalate (pure, deterministic).

    ``findings`` are this subsystem's findings across rounds. The count is the number of
    **distinct rounds** carrying a late authority finding; ``escalate`` iff that count
    ``>= threshold``. ``history_readable=False`` fails toward escalation (the stricter
    direction), never toward a bypass.
    """
    thr = max(1, int(threshold))
    if not history_readable:
        return SubsystemEscalationVerdict(
            subsystem=subsystem,
            late_authority_rounds=(),
            late_authority_round_count=0,
            threshold=thr,
            escalate=True,
            next_round_mode=MODE_FULL_SURFACE_ADVERSARIAL,
            reason=REASON_HISTORY_UNREADABLE,
        )

    rounds = sorted(
        {f.round_index for f in findings if f.subsystem == subsystem and f.counts_toward_escalation}
    )
    count = len(rounds)
    escalate = count >= thr
    if escalate:
        reason = REASON_REPEATED_LATE_AUTHORITY
    elif count == 0:
        reason = REASON_NO_LATE_AUTHORITY_FINDING
    else:
        reason = REASON_BELOW_THRESHOLD
    return SubsystemEscalationVerdict(
        subsystem=subsystem,
        late_authority_rounds=tuple(rounds),
        late_authority_round_count=count,
        threshold=thr,
        escalate=escalate,
        next_round_mode=(
            MODE_FULL_SURFACE_ADVERSARIAL if escalate else MODE_PER_FINDING_REREVIEW
        ),
        reason=reason,
    )


@dataclass(frozen=True)
class ReviewEscalationProjection:
    """Per-subsystem escalation verdicts + the roll-up of subsystems that must escalate."""

    verdicts: tuple[SubsystemEscalationVerdict, ...]
    threshold: int

    @property
    def escalating_subsystems(self) -> tuple[str, ...]:
        return tuple(v.subsystem for v in self.verdicts if v.escalate)

    @property
    def any_escalation(self) -> bool:
        return bool(self.escalating_subsystems)

    def as_payload(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
            "any_escalation": self.any_escalation,
            "escalating_subsystems": list(self.escalating_subsystems),
            "verdicts": [v.as_payload() for v in self.verdicts],
        }


def project_review_escalation(
    findings: Iterable[SubsystemFinding],
    *,
    threshold: int = DEFAULT_ESCALATION_THRESHOLD,
    unreadable_subsystems: Iterable[str] = (),
) -> ReviewEscalationProjection:
    """Project every subsystem's escalation verdict (pure, order-stable).

    Subsystems are taken from the findings' ``subsystem`` values plus any explicitly
    ``unreadable_subsystems`` (each escalated, fail-closed). The output is ordered by
    subsystem name so the projection is a stable contract.
    """
    findings = tuple(findings)
    unreadable = {s for s in unreadable_subsystems if s}
    subsystems = sorted({f.subsystem for f in findings} | unreadable)
    verdicts = tuple(
        evaluate_subsystem_escalation(
            subsystem,
            findings,
            threshold=threshold,
            history_readable=subsystem not in unreadable,
        )
        for subsystem in subsystems
    )
    return ReviewEscalationProjection(verdicts=verdicts, threshold=max(1, int(threshold)))


# ---------------------------------------------------------------------------
# Rendering (pure).
# ---------------------------------------------------------------------------


def render_review_escalation_table(projection: ReviewEscalationProjection) -> str:
    """A fixed-width human table of the escalation projection (pure)."""
    if not projection.verdicts:
        return "no subsystem review history to evaluate"
    headers = ("SUBSYSTEM", "LATE_AUTH_ROUNDS", "COUNT", "THRESHOLD", "ESCALATE", "NEXT_MODE")
    cells = [
        (
            v.subsystem,
            ",".join(str(r) for r in v.late_authority_rounds) or "-",
            str(v.late_authority_round_count),
            str(v.threshold),
            "yes" if v.escalate else "-",
            v.next_round_mode,
        )
        for v in projection.verdicts
    ]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _line(row) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()

    lines = [_line(headers), _line(tuple("-" * w for w in widths))]
    lines.extend(_line(row) for row in cells)
    return "\n".join(lines)


__all__ = (
    "MODE_PER_FINDING_REREVIEW",
    "MODE_FULL_SURFACE_ADVERSARIAL",
    "REVIEW_MODES",
    "DEFAULT_ESCALATION_THRESHOLD",
    "REASON_NO_LATE_AUTHORITY_FINDING",
    "REASON_BELOW_THRESHOLD",
    "REASON_REPEATED_LATE_AUTHORITY",
    "REASON_HISTORY_UNREADABLE",
    "SubsystemFinding",
    "SubsystemEscalationVerdict",
    "ReviewEscalationProjection",
    "evaluate_subsystem_escalation",
    "project_review_escalation",
    "render_review_escalation_table",
)
