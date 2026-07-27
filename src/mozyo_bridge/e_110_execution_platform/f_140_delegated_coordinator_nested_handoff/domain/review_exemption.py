"""Durable ``codex_direct_edit`` review exemption, folded from governed journals (#14539).

The central preset's `### Codex Direct Edit Gate` promotes Codex to the *implementation
subject* for the scope a valid gate names, and its ``follow_up_review`` field says whether an
independent review is still owed:

- ``follow_up_review: false`` (the policy default) — the direct edit IS the review exemption.
  No separate auditor Review Request / Review Gate is required, and the implementing actor must
  NOT write a self-approval to simulate one.
- ``follow_up_review: true`` — the owner explicitly asked for an independent review of this
  scope, so every existing review / generation fence stays exactly as it was.

Until #14539 that policy lived only in prose. The runtime read-models did not know the field
existed, so two projections stayed wrong after a valid exemption (policy 正本: integration head
``f6763eb1f8b71dac42d2cb156c8131711f6e9f0d``, Redmine #14530 j#89545):

1. ``workflow glance`` kept folding a superseded, pre-exemption ``review_request`` into
   ``review_waiting`` — an audit that policy says is not owed;
2. the terminal retire's latest-generation fence blocked with ``stale_review_generation``,
   because an exempt lane has no review generation to be "latest" — which pushed the coordinator
   toward asserting ``--latest-generation-admissible`` (literally "the latest generation is
   approved with no unresolved blocking finding") about a review that never happened. A false
   assert is not an acceptable way to pass a safety fence.

This module is the pure, read-only authority fact behind both fixes. It mirrors the shape
:mod:`...domain.glance_integration_disposition` already established for the integration
disposition and the work unit: a structured, issue-wide, latest-wins declaration folded from
``(journal_id, notes)`` pairs.

**Structured fields only; the marker alone is never authority.** A journal QUALIFIES as a gate
journal structurally — via the governed ``## Gate: codex_direct_edit`` heading or a
``gate=codex_direct_edit`` workflow-event marker — and only then are the gate's REQUIRED fields
read, from governed ``key: value`` field lines. Prose is never interpreted. This is what makes
the implementation request's safety clause literal: a bare exemption marker carries no
``role`` / ``direct_edit`` / ``allowed_paths`` / ``reason`` / ``follow_up_review`` field lines,
so it folds to :data:`EXEMPTION_INVALID` — review still owed — never to an exemption.

**Fail-closed in one direction only.** Every unreadable / incomplete / out-of-vocabulary gate
folds to :data:`EXEMPTION_INVALID`, which is treated exactly like "no exemption": the review
stays owed and the generation fence stays armed. There is no input to this module that turns an
unreadable record into an exemption.

**Latest wins, and a declaration supersedes by EXISTING, not by being valid.** The same
invariant :func:`...glance_integration_disposition.fold_work_unit` carries from #13490 checkpoint
review j#85365 F1 (and #13952 F3 before it): the newest structurally-qualifying gate journal is
authoritative, and only THEN is its content judged. Skipping a malformed newer gate would let a
STALE older ``follow_up_review: false`` keep exempting work the current record no longer covers.

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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (
    REASON_OK,
    AdmissionResult,
)

#: The marker gate that declares a journal to BE a ``codex_direct_edit`` gate. Read through the
#: policy-free :func:`marker_fields_in_note` scanner (never by widening
#: ``redmine_journal_source.GATE_BEARING_KINDS``) for the same reason the integration disposition
#: is: that set is the *callback-required* gate vocabulary, and a direct-edit gate must not become
#: a callback-bearing gate.
MARKER_GATE_CODEX_DIRECT_EDIT = "codex_direct_edit"

# ---------------------------------------------------------------------------
# The closed exemption vocabulary.
# ---------------------------------------------------------------------------

#: No ``codex_direct_edit`` gate journal exists at all — the ordinary review path applies.
EXEMPTION_NONE = "none"
#: A VALID gate declaring ``follow_up_review: false`` — no independent review is owed.
EXEMPTION_EXEMPT = "exempt"
#: A VALID gate declaring ``follow_up_review: true`` — the owner asked for an independent review,
#: so every existing review / generation fence applies unchanged.
EXEMPTION_REVIEW_REQUIRED = "review_required"
#: A gate journal exists but does not satisfy the gate's required fields. Fail-closed: treated
#: exactly like :data:`EXEMPTION_NONE` by every consumer, and it SUPERSEDES an older valid gate.
EXEMPTION_INVALID = "invalid"

REVIEW_EXEMPTION_STATES: frozenset[str] = frozenset(
    {EXEMPTION_NONE, EXEMPTION_EXEMPT, EXEMPTION_REVIEW_REQUIRED, EXEMPTION_INVALID}
)

#: The literal ``role`` value the gate schema requires (central preset `### Codex Direct Edit
#: Gate`: ``必須: [role:実装者, direct_edit:true, allowed_paths, reason, follow_up_review]``).
#: Deliberately a single literal and NOT an alias set: a role spelling the preset does not mandate
#: folds to :data:`EXEMPTION_INVALID` (review owed), which is the safe direction. Widening this
#: is a preset change first, not a reader change (the #13952 two-forked-allowlist lesson).
CANONICAL_DIRECT_EDIT_ROLE = "実装者"

#: The closed boolean vocabulary for ``direct_edit`` / ``follow_up_review``. Anything else — an
#: unfilled template line such as ``false (既定) | true (…)``, prose, or a blank — is NOT a
#: boolean and fails the gate closed. There is deliberately no "missing means false" rule: the
#: preset lists ``follow_up_review`` as a REQUIRED field, so an absent field is an incomplete
#: gate, not a defaulted one.
_BOOLEAN_TOKENS: dict[str, bool] = {"true": True, "false": False}

# ---------------------------------------------------------------------------
# Structural qualification + governed field lines.
#
# The field-line shape mirrors :mod:`...domain.glance_integration_disposition` (list marker /
# Markdown emphasis / backticks / ASCII-or-fullwidth colon tolerated). The two modules keep
# separate copies deliberately for now — this issue does not refactor the disposition module —
# so any future change must be made in both; see the module docstring of the sibling.
# ---------------------------------------------------------------------------

#: The governed gate heading (`## Journal Templates` -> ``## Gate: codex_direct_edit``). The
#: ``Gate:`` label is REQUIRED — unlike the integration disposition, no coordinator writes this
#: gate under a bare ``## codex_direct_edit`` heading, and requiring the label keeps a passing
#: prose mention from qualifying a journal. Trailing narrative after the token is allowed and is
#: never parsed for a value.
_HEADING_RE = re.compile(
    r"^\s{0,3}#{2,}\s*Gate\s*[:：]\s*codex[ _]direct[ _]edit\b.*$",
    re.MULTILINE | re.IGNORECASE,
)


def _field_re(*names: str) -> "re.Pattern[str]":
    """A line-anchored governed ``- <name>: <value>`` field matcher (pure)."""
    alternation = "|".join(re.escape(n) for n in names)
    return re.compile(
        r"^\s*[-*]?\s*\**\s*(?:" + alternation + r")\**\s*[:：]\s*(?P<value>.+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )


_ROLE_FIELD_RE = _field_re("role")
_DIRECT_EDIT_FIELD_RE = _field_re("direct_edit", "direct edit")
_ALLOWED_PATHS_FIELD_RE = _field_re("allowed_paths", "allowed paths")
_REASON_FIELD_RE = _field_re("reason")
_FOLLOW_UP_REVIEW_FIELD_RE = _field_re("follow_up_review", "follow up review")

#: Decorations a governed field value carries around the real token.
_DECORATION_RE = re.compile(r"^[`*\s\"']+|[`*\s\"']+$")
#: The same, MINUS ``*`` — a path glob's ``**`` is semantic, not Markdown emphasis. Stripping it
#: the generic way turned ``vibes/docs/rules/**`` into ``vibes/docs/rules/``, silently narrowing
#: the scope the gate declared it covers.
_PATH_DECORATION_RE = re.compile(r"^[`\s\"']+|[`\s\"']+$")
#: A trailing parenthetical qualifier — governed authors append rationale in ``（…）`` / ``(…)``.
_TRAILING_PAREN_RE = re.compile(r"\s*[（(][^）)]*[）)]\s*$")
#: Separators inside an inline ``allowed_paths`` value.
_PATH_SPLIT_RE = re.compile(r"[,、\s]+")


def _clean(value: object) -> str:
    """Strip list/emphasis decoration and one trailing parenthetical off a field value (pure)."""
    text = str(value or "").strip()
    text = _TRAILING_PAREN_RE.sub("", text)
    return _DECORATION_RE.sub("", text).strip()


def _boolean(value: object) -> Optional[bool]:
    """Classify a governed field value against :data:`_BOOLEAN_TOKENS`, or ``None`` (pure)."""
    return _BOOLEAN_TOKENS.get(_clean(value).lower())


def _field(pattern: "re.Pattern[str]", notes: str) -> str:
    match = pattern.search(notes or "")
    return _clean(match.group("value")) if match else ""


def _allowed_paths(notes: str) -> Tuple[str, ...]:
    """The inline ``allowed_paths`` value as a tuple of path globs (pure).

    Only the INLINE form (``- allowed_paths: src/**, tests/**``) is recognized. A gate whose
    paths are written as a following bullet sub-list yields an empty tuple, which fails the gate
    closed to :data:`EXEMPTION_INVALID` — review owed — rather than admitting an exemption whose
    covered scope this reader could not actually determine. Widening the shape is a change to
    make against a real durable record, not a guess.

    Decoration is stripped with :data:`_PATH_DECORATION_RE`, NOT the generic :data:`_DECORATION_RE`:
    a path's trailing ``**`` is a glob, and treating it as Markdown emphasis would silently
    narrow ``vibes/docs/rules/**`` to ``vibes/docs/rules/``.
    """
    match = _ALLOWED_PATHS_FIELD_RE.search(notes or "")
    if match is None:
        return ()
    raw = _TRAILING_PAREN_RE.sub("", str(match.group("value") or "").strip())
    parts = [_PATH_DECORATION_RE.sub("", p).strip() for p in _PATH_SPLIT_RE.split(raw)]
    return tuple(p for p in parts if p)


@dataclass(frozen=True)
class ReviewExemptionFacts:
    """The latest durable ``codex_direct_edit`` review exemption for one issue.

    ``state`` is the closed :data:`REVIEW_EXEMPTION_STATES` token. ``journal`` is the journal the
    gate was recorded at. ``allowed_paths`` / ``reason`` are projected from governed structured
    field lines and are EMPTY when the record does not carry them — never guessed from prose.
    """

    state: str = EXEMPTION_NONE
    journal: str = ""
    allowed_paths: Tuple[str, ...] = ()
    reason: str = ""

    @property
    def recorded(self) -> bool:
        """True when any gate journal (valid or not) is in the durable record."""
        return self.state != EXEMPTION_NONE

    @property
    def in_force(self) -> bool:
        """True ONLY for a valid gate declaring ``follow_up_review: false``.

        Every other state — including :data:`EXEMPTION_INVALID` — is False, so an unreadable
        gate can never exempt a review.
        """
        return self.state == EXEMPTION_EXEMPT

    def validated(self) -> "ReviewExemptionFacts":
        state = str(self.state or "").strip()
        if state not in REVIEW_EXEMPTION_STATES:
            state = EXEMPTION_INVALID
        return ReviewExemptionFacts(
            state=state,
            journal=str(self.journal or "").strip(),
            allowed_paths=tuple(str(p).strip() for p in self.allowed_paths if str(p).strip()),
            reason=str(self.reason or "").strip(),
        )

    def as_payload(self) -> dict[str, object]:
        v = self.validated()
        return {
            "state": v.state,
            "journal": v.journal,
            "allowed_paths": list(v.allowed_paths),
            "reason": v.reason,
        }


def _journal_exemption(notes: str) -> Optional[ReviewExemptionFacts]:
    """The exemption one journal declares, or ``None`` if it is not a gate journal (pure).

    Structural qualification (heading or ``gate=codex_direct_edit`` marker) happens BEFORE any
    field is read, so a stray ``follow_up_review:`` line in an unrelated note never contributes.
    A qualifying journal then either satisfies every required field — yielding
    :data:`EXEMPTION_EXEMPT` / :data:`EXEMPTION_REVIEW_REQUIRED` — or folds to
    :data:`EXEMPTION_INVALID`.
    """
    text = notes or ""
    qualifies = _HEADING_RE.search(text) is not None
    if not qualifies:
        for channel, fields in marker_fields_in_note(text):
            if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
                continue
            gate = (fields.get("gate") or fields.get("kind") or "").strip()
            if gate == MARKER_GATE_CODEX_DIRECT_EDIT:
                qualifies = True
                break
    if not qualifies:
        return None

    allowed_paths = _allowed_paths(text)
    reason = _field(_REASON_FIELD_RE, text)
    invalid = ReviewExemptionFacts(
        state=EXEMPTION_INVALID, allowed_paths=allowed_paths, reason=reason
    )

    # Every required field of `### Codex Direct Edit Gate`, each fail-closed.
    if _field(_ROLE_FIELD_RE, text) != CANONICAL_DIRECT_EDIT_ROLE:
        return invalid
    if _boolean(_field(_DIRECT_EDIT_FIELD_RE, text)) is not True:
        return invalid
    if not allowed_paths:
        return invalid
    if not reason:
        return invalid
    follow_up = _boolean(_field(_FOLLOW_UP_REVIEW_FIELD_RE, text))
    if follow_up is None:
        return invalid

    return ReviewExemptionFacts(
        state=EXEMPTION_REVIEW_REQUIRED if follow_up else EXEMPTION_EXEMPT,
        allowed_paths=allowed_paths,
        reason=reason,
    )


def _int_journal(journal_id: object) -> Optional[int]:
    try:
        return int(str(journal_id).strip())
    except (TypeError, ValueError):
        return None


def fold_review_exemption(
    journals: Sequence[Tuple[object, str]],
) -> ReviewExemptionFacts:
    """The LATEST durable ``codex_direct_edit`` exemption across one issue's journals (pure).

    Latest-wins by journal id, and a gate journal supersedes by EXISTING rather than by being
    valid (see the module docstring): the newest structurally-qualifying journal is authoritative,
    and a malformed newer gate therefore SHADOWS an older valid one instead of being skipped so
    the stale one stays "latest". Returns the :data:`EXEMPTION_NONE` facts when no journal
    declares a gate.
    """
    latest: Optional[Tuple[int, ReviewExemptionFacts]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        facts = _journal_exemption(notes)
        if facts is None:
            continue
        if latest is None or jint > latest[0]:
            latest = (
                jint,
                ReviewExemptionFacts(
                    state=facts.state,
                    journal=str(jint),
                    allowed_paths=facts.allowed_paths,
                    reason=facts.reason,
                ),
            )
    if latest is None:
        return ReviewExemptionFacts()
    return latest[1]


# ---------------------------------------------------------------------------
# The terminal-retire admissibility fence for an exempt lane (#14539 acceptance 2/3).
# ---------------------------------------------------------------------------

#: No ``codex_direct_edit`` gate is in the durable record at all.
REASON_NO_EXEMPTION_RECORDED = "no_review_exemption_recorded"
#: A gate journal exists but does not satisfy the gate's required fields.
REASON_EXEMPTION_INVALID = "invalid_review_exemption_gate"
#: The gate declares ``follow_up_review: true`` — the owner required an independent review, so
#: the ordinary latest-generation fence (never this exemption route) decides admissibility.
REASON_FOLLOW_UP_REVIEW_REQUIRED = "owner_required_follow_up_review"
#: The issue is not durably closed, so there is no Close evidence to re-verify.
REASON_CLOSE_NOT_RECORDED = "close_not_recorded"
#: No integration disposition means the work reached the integration branch.
REASON_INTEGRATION_NOT_COMPLETE = "integration_not_complete"


def evaluate_exemption_integration_admissible(
    exemption: ReviewExemptionFacts,
    *,
    close_recorded: bool,
    integration_complete: bool,
) -> AdmissionResult:
    """Whether an EXEMPT lane may pass the terminal retire's latest-generation fence (pure).

    An exempt lane has no review generation, so :func:`...review_generation
    .evaluate_integration_admissible` can only ever answer ``no_approval_for_latest_generation``.
    Rather than have the coordinator falsely assert ``--latest-generation-admissible`` about a
    review that never happened, the retire re-verifies the three durable facts that actually
    carry the same safety weight, at action time (#14539 acceptance 2/3):

    1. a VALID ``codex_direct_edit`` gate declaring ``follow_up_review: false`` — review is not
       owed *by policy*, not by an operator's say-so;
    2. the issue is durably CLOSED (its own close contract was satisfied upstream — the owner
       close approval a US / standalone issue needs is enforced at close time);
    3. the integration disposition says the work actually reached the integration branch.

    All three are read from the SAME durable record and the SAME folds the glance projection uses,
    so this is a re-verification, not a second grammar. Any missing fact is fail-closed, and an
    ``invalid`` gate or an owner-required follow-up review never reaches this route at all.
    """
    facts = exemption.validated()
    if facts.state == EXEMPTION_NONE:
        return AdmissionResult(False, REASON_NO_EXEMPTION_RECORDED)
    if facts.state == EXEMPTION_INVALID:
        return AdmissionResult(False, REASON_EXEMPTION_INVALID)
    if facts.state == EXEMPTION_REVIEW_REQUIRED:
        return AdmissionResult(False, REASON_FOLLOW_UP_REVIEW_REQUIRED)
    if not close_recorded:
        return AdmissionResult(False, REASON_CLOSE_NOT_RECORDED)
    if not integration_complete:
        return AdmissionResult(False, REASON_INTEGRATION_NOT_COMPLETE)
    return AdmissionResult(True, REASON_OK)


__all__ = (
    "CANONICAL_DIRECT_EDIT_ROLE",
    "EXEMPTION_EXEMPT",
    "EXEMPTION_INVALID",
    "EXEMPTION_NONE",
    "EXEMPTION_REVIEW_REQUIRED",
    "MARKER_GATE_CODEX_DIRECT_EDIT",
    "REASON_CLOSE_NOT_RECORDED",
    "REASON_EXEMPTION_INVALID",
    "REASON_FOLLOW_UP_REVIEW_REQUIRED",
    "REASON_INTEGRATION_NOT_COMPLETE",
    "REASON_NO_EXEMPTION_RECORDED",
    "REVIEW_EXEMPTION_STATES",
    "ReviewExemptionFacts",
    "evaluate_exemption_integration_admissible",
    "fold_review_exemption",
)
