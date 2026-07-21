"""Typed integration disposition + work-unit authority, folded from durable journals (#14213).

Two *authority* facts the glance previously could not read, both of which made it project a
lane onto the wrong next action:

1. **Integration disposition.** The glance collapsed the whole coordinator vocabulary
   (``merge`` / ``patch_equivalent`` / ``explicit_deferral`` / ``integration_blocked``) into
   one boolean "integration_recorded", set ONLY by a completion value. A recorded
   ``explicit_deferral`` therefore left no trace at all, so an approved-review lane whose work
   was durably NOT on the integration branch projected as "coordinator: collect owner close
   approval" — it steered a main-unmerged issue toward close (dogfood: #14192 j#84323,
   #14150 j#84424). This module folds the disposition as a CLOSED TYPED token plus its
   structured reason / unlock / next-owner fields.

2. **Work unit.** ``review_waiting`` unconditionally projected "auditor review owed (US-level
   audit)". For a leaf issue the Review Gate is owed by the same-lane ``implementation_gateway``,
   which is exactly what the reconciler's own ``expected_owner`` said — so the row contradicted
   itself (dogfood: #14150 j#84320). This module reads the governed ``work_unit:`` field the
   dispatch/gate journals already carry (``leaf_issue`` / ``user_story``).

**Structured fields only.** Every value here comes from a marker field or a governed
``key: value`` field line. Prose is NEVER interpreted — a deferral whose reason sits in a
free-text section yields an EMPTY reason, not a guessed one (acceptance 3). A disposition
journal that is present but unreadable folds to :data:`INTEGRATION_UNKNOWN`, which the policy
treats as *pending* — "we could not read it" must never project as "integration is done".

**Latest wins.** Dispositions supersede: #14150 recorded ``explicit_deferral`` (j#84424) and
later ``merged`` (j#84605). Only the highest-journal-id disposition is authoritative, so a
resolved deferral does not pin the lane in integration_waiting forever.

Boundary: pure. No IO, no Redmine, no git. A total function over ``(journal_id, notes)`` pairs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MARKER_CHANNEL_WORKFLOW_EVENT,
    marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    INTEGRATION_BLOCKED,
    INTEGRATION_COMPLETE_DISPOSITIONS,
    INTEGRATION_DISPOSITIONS,
    INTEGRATION_EXPLICIT_DEFERRAL,
    INTEGRATION_MERGE,
    INTEGRATION_NONE,
    INTEGRATION_PATCH_EQUIVALENT,
    INTEGRATION_UNKNOWN,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.work_unit_granularity import (
    WORK_UNIT_GRANULARITIES,
)

#: The marker gate that declares a journal to BE an integration disposition. Deliberately read
#: through the policy-free :func:`marker_fields_in_note` scanner rather than by widening
#: ``redmine_journal_source.GATE_BEARING_KINDS``: that set is the *callback-required* gate
#: vocabulary, and an integration disposition must not become a callback-bearing gate.
MARKER_GATE_INTEGRATION_DISPOSITION = "integration_disposition"

#: Durable spellings the coordinator actually writes -> the canonical closed token. The
#: canonical tokens are the acceptance vocabulary (``merge`` / ``patch_equivalent`` /
#: ``explicit_deferral`` / ``integration_blocked``); the aliases are the forms observed in real
#: governed journals (``merged`` in #14150 j#84605 / #14192 j#84608) and the pre-#14213 glance
#: vocabulary, kept so historical records keep folding.
_DISPOSITION_ALIASES: dict[str, str] = {
    "merge": INTEGRATION_MERGE,
    "merged": INTEGRATION_MERGE,
    "integrated": INTEGRATION_MERGE,
    "integration_complete": INTEGRATION_MERGE,
    "ff_push": INTEGRATION_MERGE,
    "ff_pushed": INTEGRATION_MERGE,
    "pushed": INTEGRATION_MERGE,
    "complete": INTEGRATION_MERGE,
    "completed": INTEGRATION_MERGE,
    "no_commit": INTEGRATION_MERGE,
    "no_commits": INTEGRATION_MERGE,
    "patch_equivalent": INTEGRATION_PATCH_EQUIVALENT,
    "cherry_picked": INTEGRATION_PATCH_EQUIVALENT,
    "explicit_deferral": INTEGRATION_EXPLICIT_DEFERRAL,
    "deferral": INTEGRATION_EXPLICIT_DEFERRAL,
    "deferred": INTEGRATION_EXPLICIT_DEFERRAL,
    "defer": INTEGRATION_EXPLICIT_DEFERRAL,
    "deferred_disposition": INTEGRATION_EXPLICIT_DEFERRAL,
    "integration_blocked": INTEGRATION_BLOCKED,
    "blocked": INTEGRATION_BLOCKED,
}

#: A line-anchored integration-disposition HEADING. Accepts the governed inline-value form
#: (``## Integration disposition: explicit_deferral``), the ``## Gate:``-prefixed variant, and
#: the real coordinator shapes observed in durable records — an optional ``Coordinator`` prefix
#: and any trailing narrative (#14192 j#84323 ``## Coordinator Integration Disposition —
#: pre-config candidateへ包含 …``, #14150 j#84605 ``## Integration Disposition — canonical
#: staging merged``). Trailing narrative is never parsed for a value; it is prose. Only the
#: inline ``: <identifier>`` form yields a heading value.
_HEADING_RE = re.compile(
    r"^\s{0,3}#{2,}\s*(?:Gate\s*[:：]\s*)?(?:Coordinator\s+)?Integration[ _]disposition\b"
    r"(?:\s*[:：]\s*(?P<value>[A-Za-z_]+))?",
    re.MULTILINE | re.IGNORECASE,
)


def _field_re(*names: str) -> "re.Pattern[str]":
    """A line-anchored governed ``- <name>: <value>`` field matcher (pure).

    Tolerates a list marker, Markdown emphasis, backticks and an ASCII or fullwidth colon —
    the shapes the governed journal templates actually produce.
    """
    alternation = "|".join(re.escape(n) for n in names)
    return re.compile(
        r"^\s*[-*]?\s*\**\s*(?:" + alternation + r")\**\s*[:：]\s*(?P<value>.+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )


_DISPOSITION_FIELD_RE = _field_re("disposition", "integration_disposition", "integration disposition")
_REASON_FIELD_RE = _field_re("defer_reason", "deferral_reason", "reason", "保留理由")
_UNLOCK_FIELD_RE = _field_re("unlock", "unlock_condition", "解除条件")
_NEXT_OWNER_FIELD_RE = _field_re("next_owner", "next owner", "next_action_owner")
_WORK_UNIT_FIELD_RE = _field_re("work_unit", "work unit")

#: Decorations a governed field value carries around the real token.
_DECORATION_RE = re.compile(r"^[`*\s\"']+|[`*\s\"']+$")
#: A trailing parenthetical qualifier — governed authors append rationale in ``（…）`` / ``(…)``
#: after the token (``- disposition: `explicit_deferral`（canonical integration pending）``).
_TRAILING_PAREN_RE = re.compile(r"\s*[（(][^）)]*[）)]\s*$")


def _clean(value: object) -> str:
    """Strip list/emphasis decoration and one trailing parenthetical off a field value (pure)."""
    text = str(value or "").strip()
    text = _TRAILING_PAREN_RE.sub("", text)
    return _DECORATION_RE.sub("", text).strip()


def _token(value: object) -> str:
    """Normalize a field value to a lower-case identifier token, or '' (pure)."""
    cleaned = _clean(value)
    match = re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", cleaned)
    return cleaned.lower() if match else ""


def canonical_disposition(value: object) -> str:
    """Map a durable disposition spelling onto the closed vocabulary (pure).

    Returns a member of :data:`INTEGRATION_DISPOSITIONS`. An out-of-vocabulary value is
    :data:`INTEGRATION_UNKNOWN` (pending, fail-closed) — never :data:`INTEGRATION_NONE`, which
    means "no disposition was recorded at all".
    """
    token = _token(value)
    if not token:
        return INTEGRATION_UNKNOWN
    if token in _DISPOSITION_ALIASES:
        return _DISPOSITION_ALIASES[token]
    if token in INTEGRATION_DISPOSITIONS:
        return token
    return INTEGRATION_UNKNOWN


@dataclass(frozen=True)
class IntegrationDispositionFacts:
    """The latest durable integration disposition for one issue. All structured; no prose.

    ``disposition`` is the closed typed token. ``journal`` is the journal it was recorded at.
    ``reason`` / ``unlock`` / ``next_owner`` are projected from governed structured field lines
    and are EMPTY when the record does not carry them — the projection never invents them from
    surrounding prose (Redmine #14213 acceptance 3).
    """

    disposition: str = INTEGRATION_NONE
    journal: str = ""
    reason: str = ""
    unlock: str = ""
    next_owner: str = ""

    @property
    def recorded(self) -> bool:
        """True when any disposition (complete or pending) is in the durable record."""
        return self.disposition != INTEGRATION_NONE

    @property
    def complete(self) -> bool:
        """True when the disposition means the work reached the integration branch."""
        return self.disposition in INTEGRATION_COMPLETE_DISPOSITIONS

    def validated(self) -> "IntegrationDispositionFacts":
        disposition = str(self.disposition or "").strip()
        if disposition not in INTEGRATION_DISPOSITIONS:
            disposition = INTEGRATION_UNKNOWN
        return IntegrationDispositionFacts(
            disposition=disposition,
            journal=str(self.journal or "").strip(),
            reason=str(self.reason or "").strip(),
            unlock=str(self.unlock or "").strip(),
            next_owner=str(self.next_owner or "").strip(),
        )

    def as_payload(self) -> dict[str, object]:
        v = self.validated()
        return {
            "disposition": v.disposition,
            "journal": v.journal,
            "reason": v.reason,
            "unlock": v.unlock,
            "next_owner": v.next_owner,
        }


def _marker_disposition_value(notes: str) -> Optional[str]:
    """The ``disposition=`` value of an integration-disposition marker in ``notes`` (pure).

    Returns the raw value when the note carries a ``workflow-event`` marker whose gate is
    ``integration_disposition``, ``""`` when such a marker exists without a ``disposition``
    field, and ``None`` when there is no such marker at all.
    """
    found: Optional[str] = None
    for channel, fields in marker_fields_in_note(notes or ""):
        if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
            continue
        gate = (fields.get("gate") or fields.get("kind") or "").strip()
        if gate != MARKER_GATE_INTEGRATION_DISPOSITION:
            continue
        found = (fields.get("disposition") or "").strip()
        if found:
            return found
    return found


def _journal_disposition(notes: str) -> Optional[IntegrationDispositionFacts]:
    """The disposition one journal declares, or ``None`` if it is not a disposition journal (pure).

    A journal QUALIFIES structurally — via an integration-disposition heading or a
    ``gate=integration_disposition`` marker — before any field is read. A stray ``disposition:``
    line in an unrelated note therefore never contributes, so the fold cannot be steered by a
    passing mention.
    """
    text = notes or ""
    heading = _HEADING_RE.search(text)
    marker_value = _marker_disposition_value(text)
    if heading is None and marker_value is None:
        return None

    # Value precedence: the structured marker field, then the governed ``disposition:`` field
    # line, then the heading's inline ``: <value>`` form. Each is structured; none is prose.
    raw: object = ""
    if marker_value:
        raw = marker_value
    else:
        field = _DISPOSITION_FIELD_RE.search(text)
        if field is not None:
            raw = field.group("value")
        elif heading is not None and heading.group("value"):
            raw = heading.group("value")

    def _field(pattern: "re.Pattern[str]") -> str:
        match = pattern.search(text)
        return _clean(match.group("value")) if match else ""

    return IntegrationDispositionFacts(
        disposition=canonical_disposition(raw),
        reason=_field(_REASON_FIELD_RE),
        unlock=_field(_UNLOCK_FIELD_RE),
        next_owner=_field(_NEXT_OWNER_FIELD_RE),
    )


def _int_journal(journal_id: object) -> Optional[int]:
    try:
        return int(str(journal_id).strip())
    except (TypeError, ValueError):
        return None


def fold_integration_disposition(
    journals: Sequence[Tuple[object, str]],
) -> IntegrationDispositionFacts:
    """The LATEST durable integration disposition across one issue's journals (pure).

    Latest-wins by journal id, because dispositions supersede: #14150 recorded
    ``explicit_deferral`` (j#84424) and later ``merged`` (j#84605); ORing them (the pre-#14213
    behaviour) would either lose the deferral or pin the lane in integration_waiting after it
    was genuinely merged. Returns the :data:`INTEGRATION_NONE` facts when no journal declares a
    disposition.
    """
    latest: Optional[Tuple[int, IntegrationDispositionFacts]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        facts = _journal_disposition(notes)
        if facts is None:
            continue
        if latest is None or jint > latest[0]:
            latest = (jint, IntegrationDispositionFacts(
                disposition=facts.disposition,
                journal=str(jint),
                reason=facts.reason,
                unlock=facts.unlock,
                next_owner=facts.next_owner,
            ))
    if latest is None:
        return IntegrationDispositionFacts()
    return latest[1]


def fold_work_unit(journals: Sequence[Tuple[object, str]]) -> str:
    """The latest governed ``work_unit:`` declaration across one issue's journals (pure).

    Returns a :data:`...work_unit_granularity.WORK_UNIT_GRANULARITIES` member, or ``""`` when
    the durable record never declares one. ``""`` is meaningful: the projection must NOT claim
    US-level audit authority without evidence (Redmine #14213 acceptance 4), so "undeclared" is
    a distinct answer from ``user_story``.

    Unlike ``normalize_work_unit_granularity`` (the dispatch-time validator, which raises on a
    bad token) this is a read-only projection over records written by many past sessions, so an
    unrecognized token folds to ``""`` — undeclared — rather than raising or being coerced.

    **A declaration supersedes by EXISTING, not by being valid** (Redmine #13490 checkpoint
    review j#85365 F1). The latest journal that carries a governed ``work_unit:`` field wins,
    and only then is its value judged: a recognized token is the work unit, anything else folds
    to ``""``. Skipping an out-of-vocabulary declaration instead — the earlier behaviour — let a
    STALE older ``user_story`` survive a newer bad one, so a lane kept claiming US-level audit
    authority the current record no longer supports. That is the same invariant #13952 F3 fixed
    for review markers: a newer malformed record must shadow an older valid one, never be
    dropped so the old one stays "latest".
    """
    latest: Optional[Tuple[int, str]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        match = _WORK_UNIT_FIELD_RE.search(notes or "")
        if match is None:
            continue  # no declaration here — this journal says nothing about the work unit
        # A declaration IS present, so it supersedes regardless of what it says. An
        # unrecognized value resolves to "" (undeclared), which routes to the same-lane
        # implementation_gateway rather than to a US-level audit.
        token = _token(match.group("value"))
        resolved = token if token in WORK_UNIT_GRANULARITIES else ""
        if latest is None or jint > latest[0]:
            latest = (jint, resolved)
    return latest[1] if latest is not None else ""


__all__ = (
    "MARKER_GATE_INTEGRATION_DISPOSITION",
    "IntegrationDispositionFacts",
    "canonical_disposition",
    "fold_integration_disposition",
    "fold_work_unit",
)
