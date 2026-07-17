"""Bounded, deterministic ``## Gate:`` journal-template grammar for `workflow glance`.

Redmine #13435 review j#74295 Finding 1 asked the glance projection to fold the workflow
state of every active lane from the durable Redmine record, and the coordinator's design
answer (j#74307) fixed *how*: a **glance-only, read-only** grammar that reads the canonical
governed journal template — never free-form prose — to a workflow gate.

The boundary (j#74307):

- **This is not the watcher intake seam.** The #12672 ``redmine_journal_source`` contract
  (structured ``[mozyo:...]`` markers only; a gate is *never* inferred from prose) is left
  exactly as-is; ``workflow watch`` still ingests markers only. This module is a separate
  read-model adapter that interprets the *governed journal template* for display, and it
  produces no watcher events and mutates nothing.
- **Only line-anchored ``## Gate: <kind>`` headings are read**, normalized (case /
  surrounding whitespace / a trailing ``(...)`` qualifier / a bounded dash qualifier whose
  left-hand lifecycle token is an exact allowlist match) and **exact-matched** against a fixed
  allowlist. Natural-language body text, ambiguous substrings, and pane scrollback are never
  consulted. A ``##`` heading without the ``Gate:`` label (a
  ``Progress Log`` / ``Handoff Delivery Record`` / ``Correction`` note) is structurally
  ignored — it is not a gate.
- **Combined headings carry several explicit gate facts.** ``## Gate: Implementation Done +
  Review Request`` splits on ``+`` into two recognized gates in one journal.
- **Collisions are excluded, not guessed.** ``Review Finding Verdict(s)`` is the
  implementer's verdict, not an audit ``review``; ``Design Consultation Answer`` is not a
  review result. Because the match is exact against the allowlist, these headings simply
  contribute no gate (an unrecognized template → the caller marks the lane ``unknown``,
  never a misclassified state). Misclassification is worse than non-classification.
- **A review conclusion is read only from an audit ``review`` journal that carries an
  explicit ``結論:`` field** (``承認`` -> approved, ``要修正`` -> changes requested). It is
  never inferred from body sentiment.

The output is a :class:`GateFacts` (or ``None`` when no canonical gate was recognized), which
:func:`lane_signal_from_gate_facts` turns into the same
:class:`...domain.sublane_admission.LaneSignal` the admission preflight and the glance fold
already consume — so the glance does not invent a second state machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    CALLBACK_NONE,
    GATE_BLOCKED,
    GATE_CLOSE,
    GATE_IMPLEMENTATION_DONE,
    GATE_NONE,
    GATE_OWNER_CLOSE_APPROVAL,
    GATE_REVIEW,
    GATE_REVIEW_REQUEST,
    GATE_START,
    LaneSignal,
    REVIEW_APPROVED,
    REVIEW_CHANGES_REQUESTED,
    REVIEW_PENDING,
)

# ---------------------------------------------------------------------------
# The allowlist: normalized ``## Gate: <kind>`` heading -> workflow gate.
#
# Keys are the heading text AFTER normalization (see :func:`_gate_heading_parts`):
# lower-cased, a trailing ``(...)`` qualifier removed, whitespace collapsed. Dispatch
# decisions fold to ``start`` (the lane is dispatched / implementing — a positive pipeline
# occupancy, never a stop reason). The list is the governed template's lifecycle gates
# (j#74307 point 5): dispatch, implementation_done, review_request, audit review,
# owner_close_approval, blocked, close/retire. The integration disposition is read separately
# from a ``## Integration disposition: <value>`` line (see :func:`_integration_disposition`).
# ---------------------------------------------------------------------------

_HEADING_GATE: dict[str, str] = {
    "start": GATE_START,
    "implementation request": GATE_START,
    "implementation request dispatch": GATE_START,
    "wave rebalance dispatch decision": GATE_START,
    "dispatch": GATE_START,
    "implementation done": GATE_IMPLEMENTATION_DONE,
    "implementation_done": GATE_IMPLEMENTATION_DONE,
    "review request": GATE_REVIEW_REQUEST,
    "review_request": GATE_REVIEW_REQUEST,
    "review": GATE_REVIEW,
    "owner close approval": GATE_OWNER_CLOSE_APPROVAL,
    "owner_close_approval": GATE_OWNER_CLOSE_APPROVAL,
    "blocked": GATE_BLOCKED,
    "close": GATE_CLOSE,
    "task close": GATE_CLOSE,
    "task_close": GATE_CLOSE,
    "retire": GATE_CLOSE,
    "retirement": GATE_CLOSE,
}

#: Integration-disposition values (the ``<value>`` of a ``## Integration disposition: <value>``
#: heading — the canonical governed form, e.g. #13446 j#74290 ``explicit_deferral``). Only a
#: *completion* disposition marks the work integrated (``integration_recorded=True``); a
#: *deferral* explicitly does NOT — the lane stays integration_waiting until it is actually
#: merged (Redmine #13435 re-audit j#74323 Finding 1). An unrecognized value asserts neither.
_INTEGRATION_COMPLETE_VALUES: frozenset[str] = frozenset(
    {
        "merged",
        "integrated",
        "integration_complete",
        "ff_push",
        "ff_pushed",
        "pushed",
        "complete",
        "completed",
        "no_commit",
        "no_commits",
        "patch_equivalent",
        "cherry_picked",
    }
)
_INTEGRATION_DEFERRAL_VALUES: frozenset[str] = frozenset(
    {"explicit_deferral", "deferral", "deferred", "defer", "deferred_disposition"}
)

#: Collision-prone canonical headings that are **explicitly not** the gate they resemble
#: (j#74307 point 3). Exact-matching already keeps them out of :data:`_HEADING_GATE`; this
#: set documents the intent and is asserted in tests so a future allowlist edit cannot make
#: e.g. ``Review Finding Verdicts`` classify as an audit ``review``.
_EXCLUDED_HEADINGS: frozenset[str] = frozenset(
    {
        "review finding verdict",
        "review finding verdicts",
        "design consultation",
        "design consultation answer",
    }
)

# Precedence within a single combined journal: pick the most-advanced gate. Across journals
# the *latest journal id* wins regardless of precedence (a later durable gate is
# authoritative); this order only breaks ties inside one journal's ``+``-combined heading.
_GATE_PRECEDENCE: dict[str, int] = {
    GATE_START: 1,
    GATE_IMPLEMENTATION_DONE: 2,
    GATE_REVIEW_REQUEST: 3,
    GATE_REVIEW: 4,
    GATE_OWNER_CLOSE_APPROVAL: 5,
    GATE_CLOSE: 6,
    GATE_BLOCKED: 7,
}

#: The gates whose journal may carry a commit hash (so the lane is commit-bearing and can be
#: ``integration_waiting`` until merged). ``commit_bearing`` is sticky once seen.
_COMMIT_BEARING_GATES: frozenset[str] = frozenset(
    {GATE_IMPLEMENTATION_DONE, GATE_REVIEW_REQUEST, GATE_OWNER_CLOSE_APPROVAL, GATE_CLOSE}
)

# A line-anchored ``## Gate: <heading>`` (two or more ``#``, the ``Gate:`` label required so a
# non-gate ``##`` section is structurally ignored). The ``:`` may be an ASCII or fullwidth
# colon (governed journals are authored in a mixed JA/EN workspace).
_GATE_HEADING_RE = re.compile(r"^\s{0,3}#{2,}\s*Gate\s*[:：]\s*(?P<title>.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_WS_RE = re.compile(r"\s+")
_SPLIT_PLUS_RE = re.compile(r"\s*\+\s*")
_BOUNDED_QUALIFIER_RE = re.compile(r"\s+[—–]\s+")

# An explicit ``結論:`` (conclusion) field line inside an audit review journal. ASCII or
# fullwidth colon; a leading list marker (``-`` / ``*``) is tolerated.
_CONCLUSION_RE = re.compile(r"^\s*[-*]?\s*結論\s*[:：]\s*(?P<value>.+?)\s*$", re.MULTILINE)

# A line-anchored ``## Integration disposition: <value>`` heading (the canonical governed
# form; a ``## Gate: Integration disposition:`` variant is also accepted). ``<value>`` is the
# first identifier token; classified against the completion / deferral vocabularies above.
_INTEGRATION_DISPOSITION_RE = re.compile(
    r"^\s{0,3}#{2,}\s*(?:Gate\s*[:：]\s*)?Integration[ _]disposition\s*[:：]\s*(?P<value>[A-Za-z_]+)",
    re.MULTILINE | re.IGNORECASE,
)

# An explicit commit-hash field on a gate journal (``commit`` / ``commit_or_diff`` /
# ``commit_hash`` / ``target_commit`` … : <hex>). Markdown emphasis / list markers tolerated.
_COMMIT_FIELD_RE = re.compile(
    r"(?im)^\s*[-*]?\s*\**\s*(?:commit|commit_or_diff|commit_hash|target_commit(?:_or_diff)?)\**\s*[:：]\s*\**`?\s*[0-9a-f]{7,40}"
)


def _normalize_heading(title: str) -> str:
    """Normalize one ``## Gate:`` heading title: drop a trailing ``(...)``, lower, collapse ws."""
    title = _TRAILING_PAREN_RE.sub("", title).strip()
    return _WS_RE.sub(" ", title).strip().lower()


def _strip_bounded_qualifier(part: str) -> str:
    """Drop a spaced em/en-dash suffix only after an exact governed lifecycle token.

    The left side must already be a complete allowlist entry (or an explicit collision
    exclusion).  This intentionally does not perform prefix matching: near-matches remain
    unknown instead of being promoted to workflow truth.
    """

    match = _BOUNDED_QUALIFIER_RE.search(part)
    if not match:
        return part
    left = part[: match.start()].strip()
    if left in _HEADING_GATE or left in _EXCLUDED_HEADINGS:
        return left
    return part


def _gate_heading_parts(notes: str) -> Tuple[str, ...]:
    """Every normalized ``## Gate:`` heading part in one journal note (``+``-split; pure)."""
    parts: list[str] = []
    for match in _GATE_HEADING_RE.finditer(notes or ""):
        normalized = _normalize_heading(match.group("title"))
        for part in _SPLIT_PLUS_RE.split(normalized):
            part = _strip_bounded_qualifier(part.strip())
            if part:
                parts.append(part)
    return tuple(parts)


def _review_conclusion(notes: str) -> str:
    """Read the explicit ``結論:`` field of an audit review journal (never body sentiment)."""
    match = _CONCLUSION_RE.search(notes or "")
    if not match:
        return REVIEW_PENDING
    value = match.group("value")
    if "承認" in value or "approve" in value.lower():
        return REVIEW_APPROVED
    if "要修正" in value or "changes" in value.lower() or "needs" in value.lower():
        return REVIEW_CHANGES_REQUESTED
    return REVIEW_PENDING


def _int_journal(journal_id) -> Optional[int]:
    try:
        return int(str(journal_id).strip())
    except (TypeError, ValueError):
        return None


def _integration_disposition(notes: str) -> Optional[bool]:
    """Classify a ``## Integration disposition:`` line: True=complete, False=deferral, None=absent.

    A *completion* disposition (merged / ff-pushed / no-commit …) marks the work integrated; a
    *deferral* (``explicit_deferral`` …) explicitly does not; an absent or unrecognized-value
    line asserts neither (returns None). Redmine #13435 re-audit j#74323 Finding 1.
    """
    match = _INTEGRATION_DISPOSITION_RE.search(notes or "")
    if not match:
        return None
    value = match.group("value").strip().lower()
    if value in _INTEGRATION_COMPLETE_VALUES:
        return True
    if value in _INTEGRATION_DEFERRAL_VALUES:
        return False
    return None


@dataclass(frozen=True)
class GateFacts:
    """The durable gate facts folded from one issue's canonical ``## Gate:`` journals.

    ``latest_gate`` is the most-recent recognized gate (the max journal id; ties inside a
    combined heading broken by :data:`_GATE_PRECEDENCE`). ``latest_gate_journal`` is that
    journal id. ``review_conclusion`` is meaningful only when ``latest_gate`` is
    :data:`GATE_REVIEW`. ``commit_bearing`` / ``integration_recorded`` are sticky facts
    accumulated across the recognized gate journals; ``blocker_recorded`` is true when the
    latest gate is :data:`GATE_BLOCKED`.
    """

    latest_gate: str
    latest_gate_journal: str
    review_conclusion: str = REVIEW_PENDING
    commit_bearing: bool = False
    integration_recorded: bool = False
    blocker_recorded: bool = False


@dataclass(frozen=True)
class _RecognizedJournal:
    journal_id: int
    gate: str  # max-precedence gate of this journal (GATE_NONE if integration-only)
    review_conclusion: str
    commit_bearing: bool


def fold_issue_gate_facts(journals: Sequence[Tuple[object, str]]) -> Optional[GateFacts]:
    """Fold one issue's journals into :class:`GateFacts`, or ``None`` if no gate recognized.

    ``journals`` is an ordered sequence of ``(journal_id, notes)`` — the raw Redmine journal
    id and note body (no prose is interpreted beyond the ``## Gate:`` grammar). Pure.

    A journal contributes a gate only when it carries at least one allowlisted, line-anchored
    ``## Gate: <kind>`` heading; an integration-disposition heading contributes
    ``integration_recorded`` but is not itself a lifecycle gate. When nothing is recognized
    the result is ``None`` so the caller surfaces the lane as ``unknown`` (an unrecognized
    template) rather than a fabricated state.
    """
    recognized: list[_RecognizedJournal] = []
    integration_recorded = False

    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        # An integration disposition can stand in its own journal (no gate heading): a
        # *completion* disposition marks the work integrated; a *deferral* explicitly does
        # not (the lane stays integration_waiting) — never conflate the two.
        if _integration_disposition(notes) is True:
            integration_recorded = True
        gates: set[str] = set()
        for part in _gate_heading_parts(notes):
            if part in _EXCLUDED_HEADINGS:
                continue
            gate = _HEADING_GATE.get(part)
            if gate is not None:
                gates.add(gate)
        if not gates:
            continue
        top_gate = max(gates, key=lambda g: _GATE_PRECEDENCE.get(g, 0))
        conclusion = _review_conclusion(notes) if GATE_REVIEW in gates else REVIEW_PENDING
        commit_bearing = bool(gates & _COMMIT_BEARING_GATES) and bool(_COMMIT_FIELD_RE.search(notes or ""))
        recognized.append(
            _RecognizedJournal(
                journal_id=jint,
                gate=top_gate,
                review_conclusion=conclusion,
                commit_bearing=commit_bearing,
            )
        )

    if not recognized:
        return None

    latest = max(recognized, key=lambda r: r.journal_id)
    return GateFacts(
        latest_gate=latest.gate,
        latest_gate_journal=str(latest.journal_id),
        review_conclusion=latest.review_conclusion if latest.gate == GATE_REVIEW else REVIEW_PENDING,
        commit_bearing=any(r.commit_bearing for r in recognized),
        integration_recorded=integration_recorded,
        blocker_recorded=(latest.gate == GATE_BLOCKED),
    )


def lane_signal_from_gate_facts(
    issue: str, facts: GateFacts, *, issue_open: bool = True
) -> LaneSignal:
    """Build the :class:`LaneSignal` the glance fold consumes from folded gate facts (pure).

    ``issue_open`` (from the Redmine issue status) is passed through so the classifier applies
    it exactly where it already does (the close / owner-close family). The closed status is
    **never** fabricated into a :data:`GATE_CLOSE` gate here: doing so previously projected a
    closed issue with unread commit facts onto ``retire_ready`` (an unsafe "safe to retire"
    claim). Retirement is only reached through the classifier's real close/owner-close path,
    which keeps commit-bearing-but-unmerged work in ``integration_waiting`` (Redmine #13435
    re-audit j#74323 Finding 3). A closed issue whose gate/commit facts are unresolved is
    surfaced as ``unknown`` (degraded) by the caller, never retired.
    """
    return LaneSignal(
        issue=issue,
        latest_gate=facts.latest_gate,
        review_conclusion=facts.review_conclusion,
        callback_state=CALLBACK_NONE,
        commit_bearing=facts.commit_bearing,
        integration_recorded=facts.integration_recorded,
        issue_open=issue_open,
        blocker_recorded=facts.blocker_recorded,
    )


__all__ = (
    "GateFacts",
    "fold_issue_gate_facts",
    "lane_signal_from_gate_facts",
)
