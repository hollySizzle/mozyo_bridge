"""The governed parked-state record, and the callback outcome inside it (Redmine #14219 T2b).

Split out of :mod:`.hibernate_basis_producer` when that module reached the module-health ceiling.
It is the one place that reads a park declaration's PROSE-adjacent structure — the governed
``- <field>: <value>`` lines the skill's fixed field shape defines — while the producer proper
stays with markers and conjuncts.

The park marker asserts that a lane is parked. This module checks the record that assertion has to
sit in, and every rule here exists because a weaker version of it shipped and was caught:

* the COMPLETE governed field set, not a convenient subset (j#86443 R2-F4);
* the field VALUES, not merely their presence (j#86503 R3-F3);
* the anchor naming THIS declaration, not merely this issue (j#86525 R4-F1);
* each governed field declared exactly once, since a first-write-wins duplicate has no order
  authority behind it (j#86525 R4-F3);
* the callback outcome as a RECORD for all three outcomes, ``sent`` included (j#86548 R5-F1);
* the record read through the canonical template's own field names rather than invented aliases,
  which had inverted the check — refusing canonical records while accepting unrelated prose
  (j#86548 R5-F2);
* each field and each part carrying its OWN authority, rather than one cross-record search that
  let a pane id inside a retry command stand in for the candidate rows (j#86558 R6-F1/F2).
"""

from __future__ import annotations

import re
from typing import Optional

from .redmine_journal_source import marker_fields_in_note

#: The park marker is not accompanied by the governed fixed-field park journal.
GAP_PARK_JOURNAL_FIELDS_ABSENT = "park_journal_fields_absent"
#: journal — so it is some other record's anchor, not this park declaration's own.
GAP_PARK_ANCHOR_NOT_THIS_DECLARATION = "park_anchor_not_this_declaration"
#: alongside it (a reason, and for ``blocked`` a replayable retry command).
GAP_PARK_CALLBACK_DETAIL_ABSENT = "park_callback_detail_absent"
#: operator's next move differs.
GAP_PARK_JOURNAL_FIELDS_INVALID = "park_journal_fields_invalid"

#: Returned when one governed field is declared more than once with DIFFERING values.
_FIELD_CONFLICT = object()


def _field_pattern(*names: str) -> "re.Pattern[str]":
    """A line-anchored governed ``- <name>: <value>`` matcher accepting any of ``names`` (pure).

    The same shape the glance reads its disposition fields from — a list marker, emphasis,
    backticks and an ASCII or fullwidth colon are tolerated — and, like the glance, several
    spellings of the same field are accepted rather than one being imposed.
    """
    alternation = "|".join(re.escape(name) for name in names)
    return re.compile(
        r"^\s*[-*]?\s*\**\s*(?:" + alternation + r")\**\s*[:：]\s*(?P<value>.+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )


def governed_field(notes: str, *names: str):
    """The governed field's single value, ``""`` if absent, or :data:`_FIELD_CONFLICT` (pure).

    Reading only the FIRST match made a note self-contradictory-but-passing: appending
    ``callback_result: invented`` after ``callback_result: sent`` left the first one authoritative
    with nothing to justify that order (checkpoint j#86525 R4-F3). Every other layer of this
    surface folds duplicates the same way — identical repeats collapse, differing ones are a typed
    conflict — so the governed fields do too.
    """
    values = {
        match.group("value").strip().strip("`")
        for match in _field_pattern(*names).finditer(notes or "")
    }
    if not values:
        return ""
    if len(values) > 1:
        return _FIELD_CONFLICT
    return values.pop()


#: The governed fixed fields a parked-state journal records, per the skill's own fixed field shape
#: (``references/workflow.md`` ``## Sublane 完了 guardrail``): the parked state is a handoff-worthy
#: ``blocked`` state, so besides the dependency fields it carries the ``durable_anchor`` it is filed
#: against and the ``callback_result`` that makes the state complete.
#:
#: The COMPLETE set is required, not a convenient subset (checkpoint j#86443 R2-F4). Checking four
#: of the six let a note that never called back — the exact failure the guardrail was written for
#: (`progress_without_callback`) — read as an affirmative park basis. "The lane is parked" and "the
#: park was handed off" are one durable state in that contract, so the producer requires the whole
#: record rather than leaving half of it to an action-time obligation.
#: Fields whose presence is what the record needs (their content is free text by contract).
_PARK_FREE_FIELDS = (("blocked_by",), ("resume_condition",))
_PARK_STATE_BLOCKED = "blocked"
#: ``callback_result: sent | blocked | not-attempted`` — the skill fixes the vocabulary, and
#: "silence is not allowed" is the whole point of the field. An invented value is silence wearing a
#: token, so it is refused rather than counted as a callback.
_CALLBACK_SENT = "sent"
_CALLBACK_BLOCKED = "blocked"
_CALLBACK_NOT_ATTEMPTED = "not-attempted"
_PARK_CALLBACK_RESULTS = frozenset({_CALLBACK_SENT, _CALLBACK_BLOCKED, _CALLBACK_NOT_ATTEMPTED})
#: ``resume_owner: coordinator`` — the guardrail assigns re-dispatch to the coordinator by name; a
#: park that nominates anyone else has not handed resume ownership to who actually owns it.
_PARK_RESUME_OWNER = "coordinator"
#: ``durable_anchor: #<issue_id> j#<gate_journal_id>``.
_DURABLE_ANCHOR_RE = re.compile(r"^#(?P<issue>\d+)\s+j#(?P<journal>\d+)$")

#: The canonical callback-outcome record, verbatim from the skill's own template
#: (``references/workflow.md`` ``### Callback outcome journal テンプレート``):
#:
#:     - target: coordinator (`--target coordinator`) | <coordinator_codex_%pane>
#:     - result: sent | blocked | not-attempted
#:     - on sent: command + observed landing marker
#:     - on blocked: reason / candidates (`agents targets` rows) / retry command (`--target %pane
#:       --target-repo auto`)
#:     - on not-attempted: explicit reason
#:
#: R4 invented its own spellings for these because the fixed-field block does not list them — but
#: the template does, and not finding it is not the same as it not existing (checkpoint j#86548
#: R5-F2). The invented aliases REJECTED canonical records while ACCEPTING values that had nothing
#: to do with a callback, which is the meaning of the check inverted. The template is the contract;
#: only the parked-state fold-in spelling ``callback_result`` is accepted alongside ``result``
#: because the fixed-field shape itself uses it.
_CALLBACK_RESULT_FIELDS = ("result", "callback_result")
_CALLBACK_TARGET_FIELD = "target"
_CALLBACK_DETAIL_FIELDS = {
    _CALLBACK_SENT: ("on sent",),
    _CALLBACK_BLOCKED: ("on blocked",),
    _CALLBACK_NOT_ATTEMPTED: ("on not-attempted",),
}

#: A pane token as ``agents targets`` prints one: ``%14`` or ``w3F:p4``.
_PANE_TOKEN_RE = re.compile(r"(?:%\d+|\b\w+:p\w+\b)")
#: The coordinator's natural target (`--target coordinator`), the normal route.
_COORDINATOR_TARGET = "coordinator"
#: The command that actually delivers a callback, and the receiver it must name: the coordinator's
#: window actor is Codex, and a callback is never addressed to another lane's Claude.
_HANDOFF_SEND_RE = re.compile(r"handoff\s+send\b", re.IGNORECASE)
_RECEIVER_CODEX_RE = re.compile(r"--to\s+codex\b", re.IGNORECASE)
#: ``--target <value>`` where the value is NOT another flag — ``--target --target-repo auto`` must
#: not read the next flag as the target it claims to pin.
_TARGET_VALUE_RE = re.compile(r"--target\s+(?!--)(?P<value>\S+)")
_RETRY_REPO_RE = re.compile(r"--target-repo\s+auto\b")
#: The handoff channel a landing marker belongs to.
_HANDOFF_CHANNEL = "handoff"


def _target_token(value: str) -> str:
    """The canonical target a ``target:`` field names, or ``""`` (pure).

    Either the natural coordinator target or a resolved pane; anything else is not a shape the
    contract routes a callback to.
    """
    if _COORDINATOR_TARGET in value.lower():
        return _COORDINATOR_TARGET
    pane = _PANE_TOKEN_RE.search(value)
    return pane.group(0) if pane else ""


def _landing_marker_fields(detail: str) -> tuple:
    """The handoff-channel marker field maps in ``detail`` (pure).

    Parsed with the same scanner the watcher uses, so a ``[mozyo:handoff:x]`` that carries no
    fields yields an empty map and cannot pass for an observation.
    """
    return tuple(
        fields
        for channel, fields in marker_fields_in_note(detail)
        if channel == _HANDOFF_CHANNEL
    )


def _sent_detail_gap(detail: str, *, target: str, source_issue: str, journal: str) -> Optional[str]:
    """Whether a ``sent`` record is THIS callback's delivery evidence (pure).

    R5 closed "a command-shaped string and a bracketed token are present". That is not the same as
    "this callback was delivered" (checkpoint j#86558 R6-F1): a same-lane note to the lane's own
    Claude, carrying another issue's marker, satisfied it. The contract routes a coordinator
    callback to the coordinator's CODEX — never to another lane's Claude — and the landing
    observation is of the marker the send itself composed. So the three parts must agree:

    * the command delivers (``handoff send``) to ``--to codex``, and the ``--target`` it names is
      the same target the record declares;
    * the marker is a real handoff marker whose ``issue`` / ``journal`` are this issue and THIS
      park declaration, addressed ``to=codex``.
    """
    declared = _target_token(target)
    if not declared:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if not (_HANDOFF_SEND_RE.search(detail) and _RECEIVER_CODEX_RE.search(detail)):
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    commanded = _TARGET_VALUE_RE.search(detail)
    if commanded is None or _target_token(commanded.group("value")) != declared:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT

    for fields in _landing_marker_fields(detail):
        if (
            str(fields.get("issue", "")).strip() == str(source_issue).strip()
            and str(fields.get("journal", "")).strip() == str(journal).strip()
            and str(fields.get("to", "")).strip().lower() == "codex"
        ):
            return None
    return GAP_PARK_CALLBACK_DETAIL_ABSENT


def _blocked_detail_gap(detail: str) -> Optional[str]:
    """Whether a ``blocked`` record carries reason / candidates / retry command (pure).

    Read as the template writes it — EXACTLY three ``/``-separated parts, each with its own job.
    Two earlier attempts stopped short of that: the first scored the reason as "whatever text is
    left once the evidence is removed" (its own negative control walked through it), and the second
    split the parts but then searched ACROSS them, so a pane id inside the retry command satisfied
    the candidate requirement and a retry that pinned no pane at all still passed (checkpoint
    j#86558 R6-F2). Splitting a record into parts does nothing unless each part has to carry its
    own authority:

    * part 1 — the reason, which must be text and not evidence standing in for one;
    * part 2 — the candidate rows, which must actually name at least one pane;
    * part 3 — the retry command, which must pin ``--target <pane>`` at one of THOSE candidates
      (not a flag, not a natural name) and ``--target-repo auto``, which is what makes it
      replayable from the durable record.
    """
    parts = [part.strip() for part in detail.split("/")]
    parts = [part for part in parts if part]
    if len(parts) != 3:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    reason, candidates, retry = parts

    if _PANE_TOKEN_RE.search(reason) or _HANDOFF_SEND_RE.search(reason):
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    candidate_panes = set(_PANE_TOKEN_RE.findall(candidates))
    if not candidate_panes:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT

    target = _TARGET_VALUE_RE.search(retry)
    if target is None:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    # Membership in the candidate set is the whole check: it implies the pinned value is a pane
    # (the set only ever holds pane tokens), so a separate shape test would be unobservable. The
    # negative lookahead in :data:`_TARGET_VALUE_RE` is what keeps ``--target --target-repo auto``
    # from parsing its own next flag as the target — also subsumed here, but the parse is right.
    if target.group("value") not in candidate_panes:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    if not _RETRY_REPO_RE.search(retry):
        return GAP_PARK_CALLBACK_DETAIL_ABSENT
    return None


def park_journal_gap(
    notes: str, *, source_issue: str = "", declaration_journal: str = ""
) -> Optional[str]:
    """The typed reason the note is not a valid governed parked-state journal, else ``None`` (pure).

    Presence is not the contract (checkpoint j#86503 R3-F3): the skill's fixed field shape pins the
    VALUES too, and a record whose ``callback_result`` is an invented word, whose ``resume_owner``
    is not the coordinator, or whose ``durable_anchor`` points somewhere else is not the record the
    park basis rests on. Reading those as satisfied is the same class of defect as reading a
    marker's self-declared authority: the shape looked right, so the content went unchecked.

    Two things the value alone still does not settle (checkpoint j#86525):

    * **which record the anchor names** (R4-F1). ``#<issue> j#<journal>`` has to point at THIS park
      declaration, not merely at some journal of this issue — otherwise any older callback journal
      on the same issue can stand in for this park's own handoff.
    * **whether the outcome was actually recorded** (R4-F2). ``blocked`` and ``not-attempted`` are
      legitimate outcomes, but the guardrail's point is that they are legitimate WHEN RECORDED: a
      blocked callback carries its reason and a replayable retry command, a not-attempted one
      carries its reason. Accepting the bare token re-admits exactly the "parked and nobody was
      told" state the completion rule exists to prevent — the token would be the silence, not the
      record of it.

    ``source_issue`` / ``declaration_journal`` are the issue and journal these notes came from —
    the SCOPE of the read, not the candidate's lane or head, so the producer's no-target-binding
    invariant is untouched. Either being empty relaxes only that comparison.
    """
    state = governed_field(notes, "state")
    callback_result = governed_field(notes, *_CALLBACK_RESULT_FIELDS)
    resume_owner = governed_field(notes, "resume_owner")
    anchor_raw = governed_field(notes, "durable_anchor")
    free = [governed_field(notes, *names) for names in _PARK_FREE_FIELDS]

    fields = [state, callback_result, resume_owner, anchor_raw, *free]
    # A field declared twice with differing values has no order authority (R4-F3): refuse before
    # any of them is read as the value.
    if any(value is _FIELD_CONFLICT for value in fields):
        return GAP_PARK_JOURNAL_FIELDS_INVALID
    if not all(fields):
        return GAP_PARK_JOURNAL_FIELDS_ABSENT

    if state.lower() != _PARK_STATE_BLOCKED:
        return GAP_PARK_JOURNAL_FIELDS_INVALID
    outcome = callback_result.lower()
    if outcome not in _PARK_CALLBACK_RESULTS:
        return GAP_PARK_JOURNAL_FIELDS_INVALID
    if resume_owner.lower() != _PARK_RESUME_OWNER:
        return GAP_PARK_JOURNAL_FIELDS_INVALID

    detail_gap = _callback_outcome_gap(
        notes, outcome, source_issue=source_issue, journal=declaration_journal
    )
    if detail_gap is not None:
        return detail_gap

    anchor = _DURABLE_ANCHOR_RE.match(anchor_raw)
    if anchor is None:
        return GAP_PARK_JOURNAL_FIELDS_INVALID
    if source_issue and anchor.group("issue") != str(source_issue).strip():
        return GAP_PARK_JOURNAL_FIELDS_INVALID
    if declaration_journal and anchor.group("journal") != str(declaration_journal).strip():
        return GAP_PARK_ANCHOR_NOT_THIS_DECLARATION
    return None


def _callback_outcome_gap(
    notes: str, outcome: str, *, source_issue: str = "", journal: str = ""
) -> Optional[str]:
    """The reason the callback outcome is not a complete record, else ``None`` (pure).

    Every outcome — ``sent`` included — has to be a RECORD, not a token, and the record has to be
    about THIS callback. Each requirement is carried by one named place:

    ======================  ==================================================================
    field / part            what it must prove
    ======================  ==================================================================
    ``target``              the callback was routed to the coordinator (natural target or pane)
    ``on sent`` command     it was delivered, to the coordinator's Codex, at that same target
    ``on sent`` marker      the landing marker observed is THIS issue + THIS declaration, to codex
    ``on blocked`` part 1   the reason
    ``on blocked`` part 2   the candidate panes (``agents targets`` rows)
    ``on blocked`` part 3   a retry pinned to one of those panes, replayable
    ``on not-attempted``    the explicit reason
    ======================  ==================================================================
    """
    target = governed_field(notes, _CALLBACK_TARGET_FIELD)
    if target is _FIELD_CONFLICT or not target:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT

    detail = governed_field(notes, *_CALLBACK_DETAIL_FIELDS[outcome])
    if detail is _FIELD_CONFLICT or not detail:
        return GAP_PARK_CALLBACK_DETAIL_ABSENT

    if outcome == _CALLBACK_SENT:
        return _sent_detail_gap(
            detail, target=target, source_issue=source_issue, journal=journal
        )
    if outcome == _CALLBACK_BLOCKED:
        return _blocked_detail_gap(detail)
    # not-attempted: an explicit reason, which is the whole field.
    return None


__all__ = [
    "GAP_PARK_ANCHOR_NOT_THIS_DECLARATION",
    "GAP_PARK_CALLBACK_DETAIL_ABSENT",
    "GAP_PARK_JOURNAL_FIELDS_ABSENT",
    "GAP_PARK_JOURNAL_FIELDS_INVALID",
    "governed_field",
    "park_journal_gap",
]
