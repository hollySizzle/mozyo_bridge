"""The HARD carve-out a direct-owner no-change waiver may never cross (Redmine #14695 j#93412 §3).

Split out of :mod:`.no_change_review_waiver` when the R2 hardening pushed that module past the
oversized-module gate. It is a genuinely separate question and reads a disjoint set of surfaces:
the waiver module asks "is this waiver authentic and is the record change-free", this one asks
"is the WORK this issue did the kind of work a waiver may cover at all". Splitting rather than
allowlisting is the gate's own prescribed remedy.

The ruling states the direction twice, and both halves are here:

- a recognized durable fact naming a carve-out surface REFUSES;
- an UNRESOLVED classification also refuses — "既定 clear にせず typed refusal とする".

Review j#93576 finding 1 measured what the first implementation got wrong on both halves, so
each is now anchored to something real rather than to something invented:

- **detection read markers only.** These gates have no marker producer anywhere in the repo
  (``render_workflow_event_marker`` refuses every token outside the callback-bearing
  ``GATE_BEARING_KINDS``), so the detector was searching for a form nothing can emit while
  ``## Gate: production_verification`` — the form they ARE written in — folded to ``clear=True``
  and admitted. Both structured surfaces are read now;
- **the token vocabulary was invented here.** It is now taken from the words the central preset's
  ``### Owner Close Approval Delegation`` carve-out list actually uses, with a drift test binding
  the two;
- **the resolution half did not read the thing it claimed to decide.** R1 took a caller flag the
  retire route always passed as ``True``, derived from "some lifecycle gate parsed"; R2 replaced
  it with the issue's ``work_unit`` declaration, whose canonical purpose is REVIEW AUTHORITY
  ROUTING, not impact classification. Review j#93638 finding 1 measured that second version: a
  record carrying ``work_unit: leaf_issue`` AND an explicit
  ``carve_out_check: production_verification`` folded to ``clear=True`` and admitted — the record
  stated the carve-out in the governed field and the reader never looked at it. It is now the
  canonical ``carve_out_check`` field itself (``## Gate: owner_close_approval`` template).

``direct_owner`` provenance is explicitly NOT an escape from any of this: provenance says who
decided, not what surface the work touched.

Boundary: pure. A total function over ``(journal_id, notes)`` pairs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.canonical_note_scan import (  # noqa: E501
    canonical_marker_bodies,
    canonical_note_lines,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
    MARKER_GATE_ALIASES,
)


def _int_journal(journal_id: object) -> Optional[int]:
    try:
        return int(str(journal_id).strip())
    except (TypeError, ValueError):
        return None


#: Structured gate tokens that INDICATE a hard carve-out surface. Closed, matched as WHOLE gate
#: tokens — never as substrings and never against prose, because "prose keyword scan は authority
#: にしない" (j#93412 §3).
#:
#: **Every token here is a word the central preset's ``### Owner Close Approval Delegation``
#: carve-out list actually uses**, and a drift test binds this set to that section's text. Review
#: j#93576 finding 1 is why: the first version was a vocabulary invented here, and a set nothing
#: in the repo produces or documents cannot detect anything. Anchoring it to the 正本 means a
#: carve-out the preset adds shows up as a failing drift test rather than as a silent hole.
#:
#: Guardrail / preset / router / skill / scaffold changes are deliberately ABSENT, and their
#: absence is not a gap: any such change is repository change, so it is already refused one
#: conjunct earlier by :func:`fold_zero_change_record`. Listing them here would imply this set is
#: what protects them, and a later edit to that assumption would silently open a hole.
HARD_CARVE_OUT_GATE_TOKENS: frozenset[str] = frozenset(
    {
        "release",
        "publish",
        "tag",
        "package_distribution",
        "production_verification",
        "credential",
        "auth",
        "permission",
        "billing",
        "destructive_operation",
        "data_deletion",
        "migration",
        "external_effect",
        "legal",
        "compliance",
        "security",
    }
)

#: A governed ``## Gate: <token>`` heading, read for its RAW token — the surface carve-out gates
#: are actually written on. Review j#93576 finding 1 measured the cost of reading markers only:
#: a record declaring ``## Gate: production_verification`` folded to ``clear=True`` and admitted,
#: because none of these gates has a marker producer at all (``render_workflow_event_marker``
#: refuses every token outside the callback-bearing ``GATE_BEARING_KINDS``, so a marker-only
#: detector was looking for something nothing in this repo can emit).
#:
#: Deliberately narrower in purpose than the glance's heading grammar, and deliberately GENEROUS
#: in splitting: this feeds a REFUSAL, so over-detection is the fail-closed direction. Combined
#: headings are split on ``+`` / ``/`` / ``,`` and every part is tested, which means
#: ``## Gate: Implementation Done + Release`` is caught. It is kept here rather than imported from
#: :mod:`.glance_journal_grammar` because that module imports THIS one; the sibling
#: :mod:`.review_exemption` carries its own heading regex for the same reason.
_GATE_HEADING_LINE_RE = re.compile(
    r"^\s{0,3}#{2,}\s*Gate\s*[:：]\s*(?P<body>.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
#: Separators a combined governed heading uses between gate names.
_HEADING_PART_SPLIT_RE = re.compile(r"[+/,、]")

#: The gate whose template owns the canonical carve-out determination. Its ``carve_out_check``
#: field is read ONLY from a journal structurally declaring this gate (qualify, then read).
OWNER_CLOSE_APPROVAL_GATE_TOKEN = "owner_close_approval"
#: The only value that means "no carve-out applies". Anything else non-empty is a stated reason,
#: which by the preset's own grammar (``none | <該当理由>``) means one DOES apply.
CARVE_OUT_CHECK_NONE = "none"

#: The governed ``- carve_out_check: <value>`` field line. Read with ``finditer`` (never
#: ``search``) so the exactly-one rule can see a second, conflicting declaration instead of
#: silently taking whichever came first.
_CARVE_OUT_CHECK_FIELD_RE = re.compile(
    r"^\s*[-*]?\s*\**\s*(?:carve_out_check|carve out check)\**\s*[:：]\s*(?P<value>.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
#: Decoration a governed field value carries around the real token.
_FIELD_DECORATION_RE = re.compile(r"^[`*\s\"']+|[`*\s\"']+$")


def _clean_field_value(value: object) -> str:
    """Strip list / emphasis decoration off a governed field value (pure)."""
    return _FIELD_DECORATION_RE.sub("", str(value or "").strip()).strip()


#: A trailing parenthetical qualifier governed authors append to a heading part.
_TRAILING_PAREN_RE = re.compile(r"\s*[（(][^）)]*[）)]\s*$")

#: The carve-out surface is provably not applicable.
CARVE_OUT_CLEAR = "hard_carve_out_not_applicable"
#: A recognized durable fact names a carve-out surface.
CARVE_OUT_DECLARED = "hard_carve_out_fact_recorded"
#: The classification could not be resolved, so non-applicability could not be PROVEN.
CARVE_OUT_UNRESOLVED = "hard_carve_out_classification_unresolved"

HARD_CARVE_OUT_REASONS: frozenset[str] = frozenset(
    {CARVE_OUT_CLEAR, CARVE_OUT_DECLARED, CARVE_OUT_UNRESOLVED}
)


@dataclass(frozen=True)
class HardCarveOutFacts:
    """Whether the hard carve-out is provably not applicable to this waiver.

    ``clear`` is the fail-closed predicate: it is True only when the classification RESOLVED and
    no recognized carve-out fact was found. ``reason`` distinguishes "we checked and it is clear"
    from "we could not check" — operationally different problems that must not collapse into one
    boolean, and ``detail`` names the token or journal that refused.
    """

    clear: bool = False
    reason: str = CARVE_OUT_UNRESOLVED
    detail: str = ""


def _normalize_gate_token(value: object) -> str:
    """One gate name in comparable form: lowercased, spaces / hyphens folded to ``_`` (pure)."""
    text = " ".join(str(value or "").strip().lower().split())
    return re.sub(r"[\s-]+", "_", text)


def _structured_gate_tokens(notes: str) -> frozenset[str]:
    """Every gate token this note NAMES, across both structured surfaces (pure).

    Structured only — canonical ``## Gate:`` headings and canonical workflow-event markers. Prose
    is never inspected and a quoted marker/heading is not one (both surfaces are read through the
    quote-aware canonical scan), because a review discussing a release or a callback quoting one
    would trip a keyword scan while proving nothing.

    BOTH surfaces, not just markers (review j#93576 finding 1). The carve-out gates have no marker
    producer in this repo at all, so a marker-only detector could never fire: the heading is the
    form these are actually written in, and reading only markers let a record declaring
    ``## Gate: production_verification`` admit.

    Read from RAW components / raw heading text rather than a strict parse, because the question
    is "does this record NAME a carve-out surface". A malformed marker naming ``gate=release``
    still names it, and refusing to see it because its body is unrenderable would make a broken
    record safer than a well-formed one — backwards for a refusal trigger.
    """
    tokens: set[str] = set()
    for _channel, body in canonical_marker_bodies(
        notes or "", channels=frozenset({MARKER_CHANNEL_WORKFLOW_EVENT})
    ):
        for component in str(body or "").split(":"):
            key, _, value = component.partition("=")
            if key.strip() in MARKER_GATE_ALIASES and value.strip():
                tokens.add(_normalize_gate_token(value))
    # Headings, over the CANONICAL lines so a quoted heading is not a declaration.
    for line in canonical_note_lines(notes or ""):
        match = _GATE_HEADING_LINE_RE.match(line or "")
        if match is None:
            continue
        for part in _HEADING_PART_SPLIT_RE.split(match.group("body")):
            token = _normalize_gate_token(_TRAILING_PAREN_RE.sub("", part))
            if token:
                tokens.add(token)
    return frozenset(tokens)


def fold_hard_carve_out(
    journals: Sequence[Tuple[object, str]],
) -> HardCarveOutFacts:
    """Whether the hard carve-out is provably NOT applicable to this waiver (pure).

    Redmine #14695 j#93412 §3 makes this a distinct conjunct from zero-change, and states its
    direction twice: a recognized durable fact naming a carve-out surface REFUSES, and an
    unresolvable classification ALSO refuses — "既定 clear にせず typed refusal とする". Direct
    owner provenance is explicitly not an escape from it.

    Two independent halves, both required:

    1. **Detection.** No journal may NAME a token in :data:`HARD_CARVE_OUT_GATE_TOKENS`, on either
       structured surface — a canonical ``## Gate:`` heading or a canonical workflow-event marker
       (:func:`_structured_gate_tokens`). Reading markers alone was the R1 defect: these gates have
       no marker producer at all, so the detector was looking for something nothing in this repo
       emits, while ``## Gate: production_verification`` — the form they ARE written in — sailed
       through (review j#93576 finding 1, reproduced to a full ``ok`` admission).
    2. **Resolution — the CANONICAL field, not a proxy.** The central preset already defines the
       durable field that records this exact determination: the ``## Gate: owner_close_approval``
       template's ``carve_out_check: none | <該当理由>``. That field IS the coordinator's
       carve-out finding, so it is read directly:

       - ``none`` -> :data:`CARVE_OUT_CLEAR`;
       - any other non-empty value (a stated reason) -> :data:`CARVE_OUT_DECLARED`. The record
         says a carve-out applies; nothing else in this module needs to agree with it;
       - absent, blank, or self-conflicting -> :data:`CARVE_OUT_UNRESOLVED`.

       An unfilled template line (``none | <該当理由>`` copied verbatim) is not the literal
       ``none``, so it lands in DECLARED — the fail-closed side, which is correct: a template
       nobody filled in has determined nothing.

       R2 used the issue's ``work_unit`` declaration as a stand-in, and review j#93638 finding 1
       measured the cost: ``fold_work_unit``'s canonical purpose is REVIEW AUTHORITY ROUTING
       (``leaf_issue`` / ``user_story``), not impact classification, so a record carrying
       ``work_unit: leaf_issue`` AND an explicit ``carve_out_check: production_verification``
       folded to ``clear=True`` and admitted — the record stated the carve-out in the governed
       field and the reader never looked at it. A proxy is not the fact.

       Before that, R1 took a ``gates_resolved`` boolean the retire route always passed as True.
       Both failures are the same shape: a conjunct that does not read the thing it claims to
       decide.

    The honest boundary, stated rather than hidden: this proves "no recognized durable fact names
    a carve-out surface, and the coordinator's own carve-out determination says none applies". It
    cannot prove the absence of an external effect that was never recorded anywhere — nothing
    reading a durable record can. That is why it is one conjunct among several and never the whole
    admission.
    """
    for journal_id, notes in journals or ():
        if _int_journal(journal_id) is None:
            continue
        named = _structured_gate_tokens(notes) & HARD_CARVE_OUT_GATE_TOKENS
        if named:
            return HardCarveOutFacts(
                clear=False,
                reason=CARVE_OUT_DECLARED,
                detail=f"j#{str(journal_id).strip()}:{sorted(named)[0]}",
            )

    return _fold_carve_out_check(journals)


def _fold_carve_out_check(
    journals: Sequence[Tuple[object, str]],
) -> HardCarveOutFacts:
    """The LATEST governed ``carve_out_check`` determination across the issue (pure).

    Read only from a journal that STRUCTURALLY declares the ``owner_close_approval`` gate, so a
    stray ``carve_out_check:`` line in an unrelated note never becomes the determination — the
    same qualify-then-read order :mod:`.review_exemption` uses for its gate fields.

    **Latest wins, and a determination supersedes by EXISTING, not by being valid** — the
    invariant this bounded context applies to every issue-wide authority fact (#13490 j#85365 F1).
    A newer owner-close-approval journal that omits the field, or states it twice with different
    values, SHADOWS an older clean ``none`` rather than being skipped so the stale one stays
    latest. Both resolve to :data:`CARVE_OUT_UNRESOLVED`, which refuses.
    """
    latest: Optional[Tuple[int, str, bool]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        text = notes or ""
        if OWNER_CLOSE_APPROVAL_GATE_TOKEN not in _structured_gate_tokens(text):
            continue
        values = {
            _clean_field_value(m.group("value"))
            for m in _CARVE_OUT_CHECK_FIELD_RE.finditer(text)
        }
        # Exactly-one: a journal stating two DIFFERENT determinations has determined neither.
        conflicted = len(values) > 1
        value = "" if conflicted or not values else next(iter(values))
        if latest is None or jint > latest[0]:
            latest = (jint, value, conflicted)

    if latest is None:
        return HardCarveOutFacts(clear=False, reason=CARVE_OUT_UNRESOLVED)
    _, value, conflicted = latest
    if conflicted or not value:
        return HardCarveOutFacts(clear=False, reason=CARVE_OUT_UNRESOLVED, detail=value)
    if value.strip().lower() != CARVE_OUT_CHECK_NONE:
        return HardCarveOutFacts(clear=False, reason=CARVE_OUT_DECLARED, detail=value)
    return HardCarveOutFacts(clear=True, reason=CARVE_OUT_CLEAR, detail=value)


__all__ = (
    "CARVE_OUT_CHECK_NONE",
    "CARVE_OUT_CLEAR",
    "CARVE_OUT_DECLARED",
    "CARVE_OUT_UNRESOLVED",
    "HARD_CARVE_OUT_GATE_TOKENS",
    "HARD_CARVE_OUT_REASONS",
    "HardCarveOutFacts",
    "OWNER_CLOSE_APPROVAL_GATE_TOKEN",
    "fold_hard_carve_out",
)
