"""The two CORRELATED records a superseded failure round is terminalized against (#14755).

:mod:`.superseded_failure_terminal` owns the declaration a lane writes about ITSELF. This module
owns the two facts that declaration may not certify on its own, because they live in records the
declaration does not control:

- :func:`fold_finding_verdicts` — the implementer's ``review_finding_verdict`` gate on the SAME
  issue. The central preset makes a per-finding verdict mandatory once a review carries findings
  (``### Review Finding Verdict Obligation``), so "the findings were received and accepted" is
  already a governed durable fact and does not need a new authority invented for it. What the
  preset does NOT give it is a finding set to be complete against, so the verdicts are checked
  for COVERAGE against :func:`read_round_findings` — the round's own enumeration, in the round's
  own journal (#14755 review j#99057 finding_1: without it, a verdict journal answering one of
  two findings satisfied "every verdict present is accepted" and opened the terminal);
- :func:`fold_successor_acknowledgement` — the SUCCESSOR issue's own acknowledgement that it
  supersedes this one. A source-side declaration alone can name any issue in the tracker as its
  successor; requiring the named issue to say so too is what makes the pairing a correlation
  rather than a claim.

**What this does and does not establish, stated rather than implied.** Both records are written
through the same source system as the declaration, and this workspace has no way to authenticate
a journal's writer (ruling #14219 j#86718 — every role posts under one account). So neither fold
proves WHO wrote anything. What they do prove is that the terminal disposition is corroborated by
two records the declaration did not author: a verdict that names the exact failed review journal,
and a successor whose own record names this issue and this review journal back. That is
correlation strength, not authentication, and :mod:`.superseded_failure_terminal` states in one
place what the whole route rests on.

Boundary: pure. A total function over ``(journal_id, notes)`` pairs; no IO, no Redmine, no git.
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
    strict_marker_body_fields,
)


def _int_journal(journal_id: object) -> Optional[int]:
    try:
        return int(str(journal_id).strip())
    except (TypeError, ValueError):
        return None


def journal_ref(value: object) -> str:
    """One journal reference in comparable form: the bare decimal id, or ``""`` (pure).

    Governed prose writes a journal as ``j#93648``; a canonical marker carries the bare ``93648``
    because ``#`` reads fine in a marker body but the two spellings must compare equal or the
    correlation would depend on which surface a value came from. Anything that is not a positive
    decimal after the optional ``j#`` / ``#`` prefix is not a journal reference at all and yields
    ``""``, which every consumer treats as unresolved.
    """
    text = str(value or "").strip()
    if text[:2].lower() == "j#":
        text = text[2:]
    elif text[:1] == "#":
        text = text[1:]
    text = text.strip()
    if not text.isdigit():
        return ""
    return str(int(text))


#: Decoration a governed field value carries around the real token (list markers, emphasis,
#: backticks). The same normalization :mod:`.no_change_carve_out` applies to ``carve_out_check``.
_FIELD_DECORATION_RE = re.compile(r"^[`*\s\"']+|[`*\s\"']+$")


def _clean_field_value(value: object) -> str:
    return _FIELD_DECORATION_RE.sub("", str(value or "").strip()).strip()


#: A governed ``## Gate: <token>`` heading line, read for its RAW token. Kept here rather than
#: imported from :mod:`.glance_journal_grammar` for the reason its siblings already are: that
#: module imports this bounded context's authority folds, so the import would be a cycle.
_GATE_HEADING_LINE_RE = re.compile(
    r"^\s{0,3}#{2,}\s*Gate\s*[:：]\s*(?P<body>.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
#: Separators a combined governed heading uses between gate names.
_HEADING_PART_SPLIT_RE = re.compile(r"[+/,、]")
#: A trailing parenthetical qualifier governed authors append to a heading part.
_TRAILING_PAREN_RE = re.compile(r"\s*[（(][^）)]*[）)]\s*$")
#: The bounded dash qualifier the canonical Gate heading grammar allows AFTER the gate token
#: (``## Gate: review_finding_verdict — R3``). A sanctioned canonical spelling, not a deviation:
#: the central preset's ``### Gate Heading Canonical Literal`` invites it. Review #14695 j#94110
#: finding 2 measured what happens when a reader strips only the parenthetical form — the dash
#: form silently stopped matching, and a fence opened for a spelling the 正本 recommends.
_BOUNDED_QUALIFIER_RE = re.compile(r"\s+[—–]\s+")


def _normalize_gate_token(value: object) -> str:
    """One gate name in comparable form: lowercased, spaces / hyphens folded to ``_`` (pure)."""
    text = " ".join(str(value or "").strip().lower().split())
    return re.sub(r"[\s-]+", "_", text)


def heading_gate_tokens(notes: str) -> frozenset[str]:
    """Every gate token a note's CANONICAL ``## Gate:`` headings name (pure).

    Quote-aware: a heading that exists only inside a fenced block, a blockquote or an inline code
    span is not a declaration, because a note transcribing a past record or quoting the contract
    would otherwise qualify itself. Combined headings are split and BOTH the whole part and its
    bounded-qualifier head are emitted, so a qualified spelling qualifies exactly like a bare one.
    """
    tokens: set[str] = set()
    for line in canonical_note_lines(notes or ""):
        match = _GATE_HEADING_LINE_RE.match(line or "")
        if match is None:
            continue
        for raw_part in _HEADING_PART_SPLIT_RE.split(match.group("body")):
            part = _TRAILING_PAREN_RE.sub("", raw_part)
            candidates = [part]
            head = _BOUNDED_QUALIFIER_RE.split(part, maxsplit=1)[0]
            if head != part:
                candidates.append(_TRAILING_PAREN_RE.sub("", head))
            for candidate in candidates:
                token = _normalize_gate_token(candidate)
                if token:
                    tokens.add(token)
    return frozenset(tokens)


def marker_declares_gate(body: str, gate: str) -> bool:
    """Whether ONE marker body names ``gate``, however the rest of it parses (pure).

    The existence half of latest-wins (#14539 j#92012 F1): a declaration supersedes by EXISTING,
    not by being readable, so a newer malformed record must still SHADOW an older valid one.
    Reading the raw components rather than the strict parse is what makes that possible.
    """
    for component in str(body or "").split(":"):
        key, _, value = component.partition("=")
        if key.strip() in MARKER_GATE_ALIASES and value.strip() == gate:
            return True
    return False


def one_canonical_marker(
    notes: str, *, gate: str, field_order: Tuple[str, ...]
) -> "Tuple[bool, Optional[dict[str, str]]]":
    """``(declared, fields)`` for the ONE canonical marker of ``gate`` in a note (pure).

    ``declared`` is whether the note names the gate at all — by a canonical heading or by any
    marker, readable or not. ``fields`` is the single strictly-readable marker's field mapping, or
    ``None`` when the note declares the gate without carrying exactly one renderable marker.

    Both answers come from ONE scan of the note. A reader that locates markers twice holds two
    notions of "where the marker is" and the weaker one wins — re-finding a body by plain string
    search knows nothing about quote / fence exclusion, so a QUOTED marker earlier in the note gets
    substituted for the canonical one (#14661 review j#92601 F2, measured).

    A marker that names the gate but cannot be read as one poisons the note for that gate: the
    readable siblings are NOT returned. Returning them would let a note carrying one clean marker
    plus one forged marker read exactly like a clean note — the readable-subset the central
    ``### Hibernate Evidence Marker Contract`` refuses twice over. Field ORDER is required too,
    not merely the set: the canonical producer emits one sequence, so a permutation is a marker
    nothing in this repo can render.
    """
    text = notes or ""
    declared = gate in heading_gate_tokens(text)
    readable: list[dict[str, str]] = []
    for _channel, body in canonical_marker_bodies(
        text, channels=frozenset({MARKER_CHANNEL_WORKFLOW_EVENT})
    ):
        if not marker_declares_gate(body, gate):
            continue
        declared = True
        fields = strict_marker_body_fields(body, expected=field_order)
        if fields is None or tuple(fields) != field_order:
            return True, None
        readable.append(fields)
    if not declared:
        return False, None
    # Zero: the note declares the gate (heading, or a marker that named it) without carrying one
    # canonical marker — a heading can shadow but never mint (#14539 j#92174 F2). Two or more: a
    # record declaring the gate twice cannot say which one is authoritative.
    return True, readable[0] if len(readable) == 1 else None


# ---------------------------------------------------------------------------
# 1. The implementer's per-finding verdicts on the failed review round.
# ---------------------------------------------------------------------------

#: The governed gate whose template carries the per-finding verdicts (central preset
#: ``## Gate: review_finding_verdict``). It is READ here, never minted: this module adds no new
#: authority for a record the preset already requires and already defines the shape of.
FINDING_VERDICT_GATE_TOKEN = "review_finding_verdict"

#: The only verdict value that means the finding was received and accepted. ``disputed`` is an
#: open escalation and ``blocked`` is a verdict waiting on a record — both mean the round's
#: findings are still owed, which is precisely the "未受領 finding" the acceptance refuses.
VERDICT_ACCEPTED = "accepted"

#: The governed ``- 対象review_journal: j#<id>`` field. The ASCII spelling is accepted beside the
#: canonical Japanese one because the preset's own machine-readable-surface rule keeps identifiers
#: literal while leaving narrative language to the workspace; a record written under an English
#: preference is the same declaration.
_VERDICT_TARGET_FIELD_RE = re.compile(
    r"^\s*[-*]?\s*\**\s*(?:対象review_journal|target_review_journal)\**\s*[:：]\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
#: The governed ``- verdict: accepted | disputed | blocked`` field, one per finding.
_VERDICT_FIELD_RE = re.compile(
    r"^\s*[-*]?\s*\**\s*verdict\**\s*[:：]\s*(?P<value>.+?)\s*$", re.IGNORECASE
)
#: The governed ``- finding_<id>: <要約>`` line that OPENS one finding's verdict block (central
#: preset ``## Gate: review_finding_verdict``). Accepted as a heading too (``### finding_1 — …``),
#: because that is the form the real records are written in (#14577 j#93656) and both surfaces
#: mean the same declaration. The delimiter set is the same reason: the list form writes ``:`` and
#: the heading form an em / en dash. What is NOT widened is the token itself — it must be the
#: template's literal ``finding_<id>``, so a review's own ``F1`` / ``Finding 1`` prose is not
#: silently read as this contract's identity (that normalization would be a second definition of
#: finding identity, and this module refuses to mint one).
_FINDING_DECL_RE = re.compile(
    r"^\s*(?:[-*]\s*|#{1,6}\s*)?\**\s*finding_(?P<id>[A-Za-z0-9]+)\**\s*[:：—–-]",
    re.IGNORECASE,
)

#: Every finding on the named review round has an ``accepted`` verdict.
VERDICT_ALL_ACCEPTED = "review_findings_all_accepted"
#: No ``review_finding_verdict`` gate is in the durable record at all.
VERDICT_NOT_RECORDED = "review_finding_verdict_not_recorded"
#: The latest verdict gate is about a DIFFERENT review journal than the one being terminalized.
VERDICT_TARGET_MISMATCH = "review_finding_verdict_names_another_review_round"
#: At least one finding is not accepted — disputed, blocked, or an unfilled template line.
VERDICT_NOT_ACCEPTED = "review_finding_not_accepted"
#: The verdict gate is declared but its target / verdicts could not be read at all.
VERDICT_UNRESOLVED = "review_finding_verdict_unresolved"
#: The verdict gate's findings and verdicts do not pair one-to-one — a finding opened with no
#: verdict under it, a verdict with no finding above it, or the same finding id twice.
VERDICT_PAIRING_UNREADABLE = "review_finding_verdict_findings_do_not_pair"
#: The failed round declares no readable finding ENUMERATION, so which findings it raised — and
#: therefore whether the verdicts cover them — is not established by any record (#14755 review
#: j#99057 finding_1).
VERDICT_ROUND_FINDINGS_UNDECLARED = "review_round_findings_not_declared"
#: The verdicts do not cover exactly the findings the round enumerated: one is missing, or one is
#: answered that the round never raised.
VERDICT_COVERAGE_MISMATCH = "review_findings_not_all_given_a_verdict"

FINDING_VERDICT_REASONS: frozenset[str] = frozenset(
    {
        VERDICT_ALL_ACCEPTED,
        VERDICT_NOT_RECORDED,
        VERDICT_TARGET_MISMATCH,
        VERDICT_NOT_ACCEPTED,
        VERDICT_UNRESOLVED,
        VERDICT_PAIRING_UNREADABLE,
        VERDICT_ROUND_FINDINGS_UNDECLARED,
        VERDICT_COVERAGE_MISMATCH,
    }
)


# ---------------------------------------------------------------------------
# 1a. The failed round's OWN enumeration of the findings it raised.
# ---------------------------------------------------------------------------

#: The gate a REVIEW ROUND journal declares to enumerate the findings that round raised.
#:
#: #14755 review j#99057 finding_1 measured why this record has to exist. Without it the verdict
#: fold could only ask "is every verdict PRESENT an ``accepted``", which a verdict journal listing
#: one of two findings satisfies — so a partial disposition opened the terminal. Coverage cannot
#: be checked against a set nobody states, and no such set exists today: the preset's ``## Gate:
#: review`` template enumerates no per-finding identity, and the real records spell theirs three
#: different ways (``#### F1`` in #14577 j#93648, ``### Finding 1`` in #14755 j#99057, ``###
#: finding_1`` in the verdict j#93656). Inferring one identity from those three would be this
#: module inventing a normalization the contract never defined.
#:
#: So the round states its own finding set, in the round's own journal, under this module's token.
#: That keeps the enumeration with the authority that raised the findings (the reviewer) rather
#: than with the declaration writer who benefits from under-counting them, and it changes NOTHING
#: about the central review gate: this is an additive marker read only by this opt-in terminal
#: route, exactly like :data:`SUCCESSOR_ACK_GATE`. A review that does not carry one simply cannot
#: be terminalized this way — which is the fail-closed direction.
#:
#: It does NOT establish who wrote the enumeration; nothing in this workspace can (ruling #14219
#: j#86718). What it establishes is that the verdicts answer a finding set stated in a DIFFERENT
#: record than the verdict journal itself.
ROUND_FINDINGS_GATE = "superseded_failure_round_findings"
#: The schema version. An unknown version is refused rather than read under today's meanings.
ROUND_FINDINGS_VERSION = "1"
#: The COMPLETE, ORDERED field set, no more and no less, in this sequence.
ROUND_FINDINGS_FIELD_ORDER: Tuple[str, ...] = (
    "gate",
    "version",
    "review_journal",
    "findings",
)
#: The separator between finding ids inside the single ``findings`` field. Not a marker separator,
#: so it cannot split the field into two.
ROUND_FINDINGS_SEPARATOR = ","

#: The round declares no enumeration at all.
ROUND_FINDINGS_NONE = "none"
#: A valid enumeration: one canonical marker, contracted literals, a non-empty duplicate-free id set.
ROUND_FINDINGS_DECLARED = "declared"
#: An enumeration is DECLARED but cannot be read as one. Fail-closed: treated exactly like
#: :data:`ROUND_FINDINGS_NONE`, and it SUPERSEDES an older valid one.
ROUND_FINDINGS_INVALID = "invalid"

ROUND_FINDINGS_STATES: frozenset[str] = frozenset(
    {ROUND_FINDINGS_NONE, ROUND_FINDINGS_DECLARED, ROUND_FINDINGS_INVALID}
)


@dataclass(frozen=True)
class RoundFindingsFacts:
    """The finding set ONE review-round journal says that round raised.

    ``findings`` is EMPTY unless the enumeration is valid — never guessed from the round's prose,
    and never completed from what the verdicts happened to answer.
    """

    state: str = ROUND_FINDINGS_NONE
    review_journal: str = ""
    findings: Tuple[str, ...] = ()

    @property
    def in_force(self) -> bool:
        """True ONLY for a VALID enumeration. :data:`ROUND_FINDINGS_INVALID` is False."""
        return self.state == ROUND_FINDINGS_DECLARED


def read_round_findings(notes: str) -> RoundFindingsFacts:
    """The finding enumeration ONE review-round journal declares (pure).

    Read from the round's own journal rather than folded latest-wins across the issue: the
    enumeration is a property OF that round, so a later round's enumeration must not be able to
    stand in for it. Which journal is the round is decided upstream, by the same glance grammar
    every other consumer uses.
    """
    declared, fields = one_canonical_marker(
        notes, gate=ROUND_FINDINGS_GATE, field_order=ROUND_FINDINGS_FIELD_ORDER
    )
    if not declared:
        return RoundFindingsFacts()
    if fields is None:
        return RoundFindingsFacts(state=ROUND_FINDINGS_INVALID)
    constants = {"gate": ROUND_FINDINGS_GATE, "version": ROUND_FINDINGS_VERSION}
    if any(fields.get(key) != value for key, value in constants.items()):
        return RoundFindingsFacts(state=ROUND_FINDINGS_INVALID)
    review = journal_ref(fields.get("review_journal", ""))
    if not review:
        return RoundFindingsFacts(state=ROUND_FINDINGS_INVALID)
    raw = str(fields.get("findings", "") or "")
    parts = [part.strip().lower() for part in raw.split(ROUND_FINDINGS_SEPARATOR)]
    if not parts or any(not part for part in parts):
        # An empty id — from an empty field, a trailing separator, or two adjacent ones — means the
        # producer could not render this list, so the list says nothing rather than saying less.
        return RoundFindingsFacts(state=ROUND_FINDINGS_INVALID, review_journal=review)
    if len(set(parts)) != len(parts):
        # The same finding named twice cannot be covered "once each" by any verdict set.
        return RoundFindingsFacts(state=ROUND_FINDINGS_INVALID, review_journal=review)
    return RoundFindingsFacts(
        state=ROUND_FINDINGS_DECLARED, review_journal=review, findings=tuple(parts)
    )


def render_round_findings_marker(*, review_journal: object, findings: Sequence[str]) -> str:
    """The exact marker a valid round enumeration must carry (pure).

    Every producer error raises ``ValueError`` rather than being written, for the reason
    :func:`render_successor_acknowledgement_marker` states: a renderer that emits what its own
    parser refuses produces durable records that read back as a typed zero.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
        reject_marker_separator,
    )

    review = journal_ref(review_journal)
    if not review:
        raise ValueError("review_journal must be a journal reference")
    ids = [str(value or "").strip().lower() for value in findings or ()]
    if not ids or any(not value for value in ids):
        raise ValueError("findings must be a non-empty list of non-empty ids")
    if len(set(ids)) != len(ids):
        raise ValueError("findings must not repeat an id")
    if any(ROUND_FINDINGS_SEPARATOR in value for value in ids):
        raise ValueError("a finding id must not contain the list separator")
    for value in ids:
        reject_marker_separator(value, field="findings")
    fields = {
        "gate": ROUND_FINDINGS_GATE,
        "version": ROUND_FINDINGS_VERSION,
        "review_journal": review,
        "findings": ROUND_FINDINGS_SEPARATOR.join(ids),
    }
    body = ":".join(f"{key}={fields[key]}" for key in ROUND_FINDINGS_FIELD_ORDER)
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"


@dataclass(frozen=True)
class FindingVerdictFacts:
    """Whether the failed round's findings were received and ACCEPTED, per the durable record.

    ``accepted`` is the fail-closed predicate; ``reason`` names which condition refused and
    ``journal`` where the deciding declaration is, so a refusal is diagnosable rather than a bare
    False. ``review_journal`` is the round the deciding declaration says it is about.
    """

    accepted: bool = False
    reason: str = VERDICT_NOT_RECORDED
    journal: str = ""
    review_journal: str = ""


def fold_finding_verdicts(
    journals: Sequence[Tuple[object, str]], *, review_journal: str
) -> FindingVerdictFacts:
    """Whether the LATEST verdict gate accepts every finding of ``review_journal`` (pure).

    Latest-wins by journal id, and **supersede-by-EXISTING**: the newest journal that
    structurally declares the verdict gate is authoritative, and only THEN is its content judged.
    A newer malformed or differently-targeted verdict therefore SHADOWS an older clean one instead
    of being skipped so the stale one stays "latest" — the invariant every issue-wide authority
    fact in this bounded context carries (#13490 j#85365 F1).

    That latest-wins is also what makes this conjunct fence a SECOND failed round. If the lane
    went back to work, failed again and recorded verdicts for the newer round, the latest verdict
    gate names that newer review journal — so a declaration still pointing at the first round
    refuses with :data:`VERDICT_TARGET_MISMATCH` rather than admitting on stale corroboration.

    A qualifying journal must state its target once (two DIFFERENT targets have stated neither)
    and carry at least one verdict. Every verdict must be the literal :data:`VERDICT_ACCEPTED`;
    an unfilled template line (``accepted | disputed | blocked`` copied verbatim) is not that
    literal and refuses, which is correct — a template nobody filled in has decided nothing.

    Fields are read from the QUOTE-AWARE canonical lines, the same surface the gate qualification
    is read from. #14695 review j#93704 finding 2 measured the cost of the asymmetry: with the
    declaration quote-aware and the value read from the raw note, a field appearing only inside a
    fenced code block resolved a determination and the admission returned ``ok``.
    """
    wanted = journal_ref(review_journal)
    # The round's OWN enumeration, read from the round's journal. Located by the id the
    # declaration named, so the finding set is a property of that round and a later round's
    # enumeration cannot stand in for it.
    round_findings = RoundFindingsFacts()
    for journal_id, notes in journals or ():
        if wanted and journal_ref(journal_id) == wanted:
            round_findings = read_round_findings(notes or "")
            break
    latest: Optional[Tuple[int, FindingVerdictFacts]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        text = notes or ""
        if not _declares_verdict_gate(text):
            continue
        facts = _journal_verdicts(text, wanted=wanted, round_findings=round_findings)
        if latest is None or jint > latest[0]:
            latest = (
                jint,
                FindingVerdictFacts(
                    accepted=facts.accepted,
                    reason=facts.reason,
                    journal=str(jint),
                    review_journal=facts.review_journal,
                ),
            )
    return latest[1] if latest is not None else FindingVerdictFacts()


def _declares_verdict_gate(notes: str) -> bool:
    """Whether ONE journal structurally declares the verdict gate, on EITHER surface (pure).

    Both surfaces, for the shadowing rule rather than for detection breadth. This fold is
    latest-wins by DECLARATION, so a newer verdict gate must be able to shadow an older clean one
    — and a newer gate declared only by a marker would otherwise be invisible here, leaving the
    stale clean one "latest". Widening qualification is the fail-closed direction for exactly that
    reason: a journal that qualifies but cannot be read resolves to
    :data:`VERDICT_UNRESOLVED`, which refuses.

    The heading is the form this gate is actually written in (its template is a heading and it has
    no marker producer in this repo — the #14695 j#93576 F1 lesson), so the heading alone would be
    enough for detection; it is not enough for shadowing.
    """
    text = notes or ""
    if FINDING_VERDICT_GATE_TOKEN in heading_gate_tokens(text):
        return True
    return any(
        marker_declares_gate(body, FINDING_VERDICT_GATE_TOKEN)
        for _channel, body in canonical_marker_bodies(
            text, channels=frozenset({MARKER_CHANNEL_WORKFLOW_EVENT})
        )
    )


def _paired_verdicts(notes: str) -> "Optional[dict[str, str]]":
    """``{finding_id: verdict}`` for one verdict journal, or ``None`` if it does not pair (pure).

    One pass, in document order: a ``finding_<id>`` line opens a block and the next ``verdict:``
    line closes it. ``None`` — not a partial mapping — whenever the two do not pair one-to-one:

    - a finding opened with no verdict beneath it (the preset's "一括 verdict は invalid": a
      finding listed and left unanswered has decided nothing);
    - a verdict with no finding above it (a bare verdict answers something unnamed, so it cannot
      be counted toward covering any particular finding);
    - the same finding id twice (which of the two verdicts is that finding's is unordered).

    Returning ``None`` rather than the readable subset is the same rule
    :func:`one_canonical_marker` applies to markers: a record that cannot be read as this shape
    must not read exactly like a smaller record that can.
    """
    paired: "dict[str, str]" = {}
    open_id = ""
    for line in canonical_note_lines(notes or ""):
        text = line or ""
        decl = _FINDING_DECL_RE.match(text)
        if decl is not None:
            if open_id:
                return None  # the previous finding was never answered
            finding_id = decl.group("id").strip().lower()
            if not finding_id or finding_id in paired:
                return None
            open_id = finding_id
            continue
        verdict = _VERDICT_FIELD_RE.match(text)
        if verdict is None:
            continue
        if not open_id:
            return None  # a verdict answering nothing named
        paired[open_id] = _clean_field_value(verdict.group("value")).lower()
        open_id = ""
    if open_id:
        return None
    return paired or None


def _journal_verdicts(
    notes: str, *, wanted: str, round_findings: RoundFindingsFacts
) -> FindingVerdictFacts:
    """What ONE verdict-gate journal says about ``wanted`` (pure)."""
    targets = {
        journal_ref(_clean_field_value(match.group("value")))
        for line in canonical_note_lines(notes or "")
        for match in (_VERDICT_TARGET_FIELD_RE.match(line or ""),)
        if match is not None
    }
    targets.discard("")
    if len(targets) != 1:
        # Zero: the gate is declared with no readable target. Two: it has named two rounds and
        # therefore decided about neither.
        return FindingVerdictFacts(reason=VERDICT_UNRESOLVED)
    target = next(iter(targets))
    paired = _paired_verdicts(notes)
    if paired is None:
        return FindingVerdictFacts(
            reason=VERDICT_PAIRING_UNREADABLE, review_journal=target
        )
    if not wanted or target != wanted:
        # Reported BEFORE the verdict values are judged: "these verdicts are about another round"
        # is a different operational problem from "a finding was disputed", and collapsing them
        # would point an operator at the wrong record.
        return FindingVerdictFacts(reason=VERDICT_TARGET_MISMATCH, review_journal=target)
    # COVERAGE before content (#14755 review j#99057 finding_1). "every verdict present is
    # accepted" is satisfied by a verdict journal that lists one of the round's two findings, so
    # the set the verdicts answer must equal the set the ROUND said it raised — checked against
    # the round's own enumeration, a record this journal did not write.
    if not round_findings.in_force:
        return FindingVerdictFacts(
            reason=VERDICT_ROUND_FINDINGS_UNDECLARED, review_journal=target
        )
    if round_findings.review_journal != target:
        # The enumeration read from the round names a different round than these verdicts do.
        return FindingVerdictFacts(reason=VERDICT_TARGET_MISMATCH, review_journal=target)
    if set(paired) != set(round_findings.findings):
        return FindingVerdictFacts(
            reason=VERDICT_COVERAGE_MISMATCH, review_journal=target
        )
    if any(value != VERDICT_ACCEPTED for value in paired.values()):
        return FindingVerdictFacts(reason=VERDICT_NOT_ACCEPTED, review_journal=target)
    return FindingVerdictFacts(
        accepted=True, reason=VERDICT_ALL_ACCEPTED, review_journal=target
    )


# ---------------------------------------------------------------------------
# 2. The successor issue's own acknowledgement.
# ---------------------------------------------------------------------------

#: The gate token the SUCCESSOR issue declares to acknowledge that it supersedes another issue's
#: failed round. Named per surface, like every other approval / authority gate in this context: an
#: acknowledgement for one supersession can never be read as one for another.
SUCCESSOR_ACK_GATE = "superseded_failure_successor"
#: The schema version. An unknown version is REFUSED rather than interpreted under today's field
#: meanings, because the next version may give an existing field a different weight.
SUCCESSOR_ACK_VERSION = "1"
#: The only admissible decision. A record written to DECLINE the pairing carries a different token
#: and therefore cannot corroborate anything.
SUCCESSOR_ACK_DECISION = "supersedes"

#: The COMPLETE, ORDERED field set a canonical acknowledgement carries — no more, no less, in this
#: sequence. There is deliberately NO lane envelope: the acknowledgement is about the SUCCESSOR's
#: work, whose lane is not the retire target's, so an envelope here could not be exact-matched
#: against anything the retire measures and would be a field that looks like a fence but is not.
#: The correlation this record carries is issue-and-journal identity, and that is what it states.
SUCCESSOR_ACK_FIELD_ORDER: Tuple[str, ...] = (
    "gate",
    "version",
    "decision",
    "issue",
    "superseded_issue",
    "superseded_review_journal",
    "review_journal",
)

#: No acknowledgement is in the successor's durable record at all.
ACK_NONE = "none"
#: A VALID acknowledgement: one canonical marker with every literal at its contracted value.
ACK_ACKNOWLEDGED = "acknowledged"
#: An acknowledgement is DECLARED but cannot be read as one. Fail-closed: treated exactly like
#: :data:`ACK_NONE` by every consumer, and it SUPERSEDES an older valid one.
ACK_INVALID = "invalid"

SUCCESSOR_ACK_STATES: frozenset[str] = frozenset({ACK_NONE, ACK_ACKNOWLEDGED, ACK_INVALID})


@dataclass(frozen=True)
class SuccessorAcknowledgementFacts:
    """The LATEST durable supersession acknowledgement in one SUCCESSOR issue's journals.

    ``state`` is a closed :data:`SUCCESSOR_ACK_STATES` token; ``journal`` is where it was
    recorded. The identity fields are projected from the canonical marker and are EMPTY unless the
    acknowledgement is valid — never guessed from prose, and never completed from whatever the
    caller happened to ask about.
    """

    state: str = ACK_NONE
    journal: str = ""
    issue: str = ""
    superseded_issue: str = ""
    superseded_review_journal: str = ""
    review_journal: str = ""

    @property
    def recorded(self) -> bool:
        """True when any acknowledgement (valid or not) is in the successor's record."""
        return self.state != ACK_NONE

    @property
    def in_force(self) -> bool:
        """True ONLY for a VALID acknowledgement. :data:`ACK_INVALID` is False, like an absent one."""
        return self.state == ACK_ACKNOWLEDGED


def fold_successor_acknowledgement(
    journals: Sequence[Tuple[object, str]],
) -> SuccessorAcknowledgementFacts:
    """The LATEST supersession acknowledgement across the successor's journals (pure).

    Latest-wins by journal id, supersede-by-EXISTING. A successor that later withdrew the pairing
    by recording a malformed or superseding acknowledgement therefore shadows the older valid one,
    rather than being skipped so the stale one keeps corroborating.
    """
    latest: Optional[Tuple[int, SuccessorAcknowledgementFacts]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        facts = _journal_acknowledgement(notes or "")
        if facts is None:
            continue
        if latest is None or jint > latest[0]:
            latest = (
                jint,
                SuccessorAcknowledgementFacts(
                    state=facts.state,
                    journal=str(jint),
                    issue=facts.issue,
                    superseded_issue=facts.superseded_issue,
                    superseded_review_journal=facts.superseded_review_journal,
                    review_journal=facts.review_journal,
                ),
            )
    return latest[1] if latest is not None else SuccessorAcknowledgementFacts()


def _journal_acknowledgement(notes: str) -> Optional[SuccessorAcknowledgementFacts]:
    """The acknowledgement ONE journal declares, or ``None`` if it declares none (pure)."""
    declared, fields = one_canonical_marker(
        notes, gate=SUCCESSOR_ACK_GATE, field_order=SUCCESSOR_ACK_FIELD_ORDER
    )
    if not declared:
        return None
    if fields is None:
        return SuccessorAcknowledgementFacts(state=ACK_INVALID)
    constants = {
        "gate": SUCCESSOR_ACK_GATE,
        "version": SUCCESSOR_ACK_VERSION,
        "decision": SUCCESSOR_ACK_DECISION,
    }
    if any(fields.get(key) != value for key, value in constants.items()):
        return SuccessorAcknowledgementFacts(state=ACK_INVALID)
    issue = str(fields.get("issue", "") or "").strip()
    superseded_issue = str(fields.get("superseded_issue", "") or "").strip()
    superseded_review = journal_ref(fields.get("superseded_review_journal", ""))
    review = journal_ref(fields.get("review_journal", ""))
    if not issue or not superseded_issue or not superseded_review or not review:
        return SuccessorAcknowledgementFacts(state=ACK_INVALID)
    if issue == superseded_issue:
        # An issue cannot acknowledge that it supersedes itself. #14695 review j#94260 measured the
        # same shape one level down — an authority superseding a round recorded in its own journal
        # — and the answer is the same: a self-referential supersession orders nothing.
        return SuccessorAcknowledgementFacts(state=ACK_INVALID)
    return SuccessorAcknowledgementFacts(
        state=ACK_ACKNOWLEDGED,
        issue=issue,
        superseded_issue=superseded_issue,
        superseded_review_journal=superseded_review,
        review_journal=review,
    )


def render_successor_acknowledgement_marker(
    *,
    issue: str,
    superseded_issue: str,
    superseded_review_journal: object,
    review_journal: object,
) -> str:
    """The exact marker a valid successor acknowledgement must carry (pure).

    Rendered so the coordinator recording one can see precisely what to write — an authority
    contract nobody can produce is an authority contract nobody will use. Field order is
    :data:`SUCCESSOR_ACK_FIELD_ORDER`, so what this emits is what the strict reader accepts, by
    construction.

    Every producer error raises ``ValueError`` rather than being written. A renderer that emits
    what its own parser refuses is not a strict grammar: it produces durable records that read
    back as a typed zero, so the corroboration silently does not count.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
        reject_marker_separator,
    )

    issue_s = str(issue or "").strip()
    superseded_s = str(superseded_issue or "").strip()
    if not issue_s or not superseded_s:
        raise ValueError(
            "a successor acknowledgement requires a non-empty issue and superseded_issue"
        )
    if issue_s == superseded_s:
        raise ValueError("an issue cannot acknowledge that it supersedes itself")
    reject_marker_separator(issue_s, field="issue")
    reject_marker_separator(superseded_s, field="superseded_issue")
    superseded_review = journal_ref(superseded_review_journal)
    review = journal_ref(review_journal)
    if not superseded_review:
        raise ValueError(
            "a successor acknowledgement requires the superseded review journal id, got "
            f"{superseded_review_journal!r}"
        )
    if not review:
        raise ValueError(
            "a successor acknowledgement requires its own approved review journal id, got "
            f"{review_journal!r}"
        )
    body = ":".join(
        [
            f"gate={SUCCESSOR_ACK_GATE}",
            f"version={SUCCESSOR_ACK_VERSION}",
            f"decision={SUCCESSOR_ACK_DECISION}",
            f"issue={issue_s}",
            f"superseded_issue={superseded_s}",
            f"superseded_review_journal={superseded_review}",
            f"review_journal={review}",
        ]
    )
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"


__all__ = (
    "ACK_ACKNOWLEDGED",
    "ACK_INVALID",
    "ACK_NONE",
    "FINDING_VERDICT_GATE_TOKEN",
    "FINDING_VERDICT_REASONS",
    "FindingVerdictFacts",
    "ROUND_FINDINGS_DECLARED",
    "ROUND_FINDINGS_FIELD_ORDER",
    "ROUND_FINDINGS_GATE",
    "ROUND_FINDINGS_INVALID",
    "ROUND_FINDINGS_NONE",
    "ROUND_FINDINGS_SEPARATOR",
    "ROUND_FINDINGS_STATES",
    "ROUND_FINDINGS_VERSION",
    "RoundFindingsFacts",
    "SUCCESSOR_ACK_DECISION",
    "SUCCESSOR_ACK_FIELD_ORDER",
    "SUCCESSOR_ACK_GATE",
    "SUCCESSOR_ACK_STATES",
    "SUCCESSOR_ACK_VERSION",
    "SuccessorAcknowledgementFacts",
    "VERDICT_ACCEPTED",
    "VERDICT_ALL_ACCEPTED",
    "VERDICT_COVERAGE_MISMATCH",
    "VERDICT_NOT_ACCEPTED",
    "VERDICT_NOT_RECORDED",
    "VERDICT_PAIRING_UNREADABLE",
    "VERDICT_ROUND_FINDINGS_UNDECLARED",
    "VERDICT_TARGET_MISMATCH",
    "VERDICT_UNRESOLVED",
    "fold_finding_verdicts",
    "fold_successor_acknowledgement",
    "heading_gate_tokens",
    "journal_ref",
    "marker_declares_gate",
    "one_canonical_marker",
    "read_round_findings",
    "render_round_findings_marker",
    "render_successor_acknowledgement_marker",
)
