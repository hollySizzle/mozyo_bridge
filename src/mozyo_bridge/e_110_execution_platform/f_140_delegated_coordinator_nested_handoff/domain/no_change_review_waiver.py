"""Durable direct-owner review WAIVER for a no-change investigation (Redmine #14695).

#14613 was a characterization: it produced zero repository change and zero commits, the owner
said in as many words that no separate reviewer was owed, and the coordinator recorded that
waiver durably and closed the issue. The standard ``sublane retire`` then blocked with
``stale_review_generation``, because the only durable authority it reads for that fence is a
REVIEW GENERATION — and a lane that changed nothing has none. The measured reproduction is
#14613 j#93256 (the waiver + Close) and j#93262 (the blocked retire).

The two escapes that were correctly refused there are the reason this module exists:

- asserting ``--latest-generation-admissible`` ("the latest generation is approved and carries
  no unresolved blocking finding") about a review that never happened is a FALSE assert;
- a fabricated Review Gate would be exactly the "exemption を Review Gate approval または自己
  review と表現しない" the central preset forbids.

So the waiver is expressed as its own durable authority, and the retire re-verifies it at action
time — the same shape :mod:`.review_exemption` established for the ``codex_direct_edit``
exemption (#14539), and deliberately NOT a second dialect of it. The two authorities answer the
same downstream question ("is an independent review owed right now?") from different premises:

- ``codex_direct_edit`` — work WAS produced, and the auditor was promoted to implementer for the
  paths the gate names, so coverage is checked against the commit's changed paths;
- this waiver — NO work was produced, so there is nothing to review, nothing to integrate, and
  the carve-out is the mirror image: any declared change at all refuses it.

**Why this is not a relaxation of the review fence.** A waiver admits a lane only when the
durable record says the lane produced nothing AND the live repository still agrees. A lane that
produced commits — ordinary development, a guardrail change, a release — can never reach it,
because a single declared commit, changed path, change-bearing gate or integration disposition
refuses (:func:`fold_zero_change_record`). The ordinary review-generation fence and the #14539
exemption route are untouched; this is a third independent route to the SAME single fence, and
each route can only ever admit, never widen the others.

**Structured marker only, closed vocabulary, exact field set.** The gate is declared by a
canonical ``[mozyo:workflow-event:gate=no_change_review_waiver:...]`` marker, read through the
shared span-preserving scan and the shared closed-vocabulary strict reader. Prose is never
interpreted, a quoted marker is not a marker, and a body the canonical producer could not render
— an extra field, a repeated key, an empty value, a permuted field order — is refused whole
rather than partially honoured. This copies :mod:`.worker_refresh_approval` (#14661), which
hardened exactly this problem for a destructive owner approval, rather than inventing a second
approval dialect.

**Lane correlation reuses the ONE envelope grammar.** ``workspace`` / ``lane`` /
``lane_generation`` / ``head`` are the common :mod:`.hibernate_evidence_envelope` fields, parsed
by that module's parser. A waiver therefore cannot be reused across lanes or across a superseded
generation of its own lane, and its ``head`` is what makes post-waiver mutation detectable.

**Two axes, never conflated** (#14661 Design Answer j#92641): ``approval_source=direct_owner`` is
WHOSE decision the record reports; who was allowed to RECORD it is resolved separately from the
gate->role policy with a durable anchor (:mod:`.hibernate_evidence_authority`). A marker that
names an approval source proves nothing about its writer.

Boundary: pure. No IO, no Redmine, no git. The live repository half of the zero-change proof is
necessarily measured by the application layer and arrives here as parameters — see
:func:`evaluate_no_change_waiver_admissible` for why neither half is sufficient alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.canonical_note_scan import (  # noqa: E501
    canonical_marker_bodies,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_integration_disposition import (  # noqa: E501
    fold_integration_disposition,
    has_conflicting_disposition_declaration,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.no_change_carve_out import (  # noqa: E501
    CARVE_OUT_DECLARED,
    HardCarveOutFacts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    EnvelopeParseError,
    LaneEvidenceEnvelope,
    parse_lane_envelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
    MARKER_GATE_ALIASES,
    strict_marker_body_fields,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_exemption import (  # noqa: E501
    declares_any_commit,
    fold_declared_change_scope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (  # noqa: E501
    REASON_OK,
    AdmissionResult,
)

#: **THE gate: can this record system establish who WROTE a waiver?** It cannot, and until a
#: ruling supplies a mechanism that can, this route admits nothing (Redmine #14695 review j#93776
#: finding 1; the issue's own Acceptance sanctions this outcome — "未対応なら Close 前に typed
#: refusal する").
#:
#: Every role in this workspace posts under one source-system account (ruling #14219 j#86718), and
#: :func:`...hibernate_issuer_policy.resolve_journal_issuer` says in its own docstring that it is a
#: POLICY binding, not authentication — it takes no author parameter at all. So a lane worker can
#: write its own waiver: it knows its own ``workspace`` / ``lane`` / ``lane_generation`` / ``head``,
#: which makes the envelope a value it fills in rather than a signature it must forge.
#:
#: R4 tried to close this by requiring two declarations to agree (the marker's ``carve_out`` and
#: the governed ``carve_out_check``). Measured: a record carrying neither an author nor a receipt
#: still admitted with ``ok``. **Conjoining two self-declarations by the same unauthenticated actor
#: is one self-declaration.** Adding a third would be no different.
#:
#: This is deliberately ONE flag rather than a condition spread through the consumers: when a
#: writer/receipt authority bound to an actual coordinator action is ruled on, flipping this — and
#: wiring that authority in — is the whole change. Every fold below stays live and tested meanwhile,
#: so what lands then is an authority check, not a re-implementation.
WRITER_AUTHORITY_RESOLVABLE = False

#: The gate token this authority is declared under. Named per surface, like every other approval
#: gate in this context (``worker_refresh_owner_approval`` / ``composer_discard_approval``): a
#: waiver for one kind of work can never be read as a waiver for another.
NO_CHANGE_REVIEW_WAIVER_GATE = "no_change_review_waiver"

#: The schema version. A marker of an unknown version is REFUSED rather than interpreted under
#: today's field meanings — the next version may give an existing field a different weight.
#:
#: Bumped to ``2`` when the carve-out determination joined the marker (Redmine #14695 review
#: j#93704 finding 1). A ``version=1`` marker carries no ``carve_out`` field, so honouring it
#: would be honouring a waiver whose determination is unbound — exactly what the finding refuses.
#: Nothing in production carries a v1 waiver (#14613's record is not conformant), so this
#: invalidates no real authority.
WAIVER_VERSION = "2"
#: The only admissible decision. A record written to DECLINE a waiver carries a different token
#: and therefore cannot admit anything.
WAIVER_DECISION = "waived"
#: The only admissible scope. This authority exists for an investigation that produced no
#: repository change; a marker naming any other scope is not this authority.
WAIVER_SCOPE = "no_change_investigation"
#: Waiving an independent review is not routine development, so the central preset's
#: ``### Owner Close Approval Delegation`` standing delegation does not reach it: only a DIRECT
#: owner decision qualifies. This is the provenance axis, never the writer axis.
WAIVER_APPROVAL_SOURCE = "direct_owner"
#: The only admissible ``carve_out`` value. The marker states the coordinator's carve-out finding
#: INSIDE the lane envelope, so overwriting it requires forging the exact workspace / lane /
#: generation / head — not merely appending another note to the issue (review j#93704 finding 1).
#:
#: This does NOT make the marker self-certifying, which j#93412 §3 forbids: the separate governed
#: ``carve_out_check`` field must independently agree, and a carve-out surface named ANYWHERE in
#: the record still refuses. The marker binds the determination to a lane; the governed field is
#: the determination; a recognized fact overrides both.
WAIVER_CARVE_OUT_NONE = "none"

#: The COMPLETE, ORDERED field set a canonical waiver marker carries — no more, no less, in this
#: sequence. An unknown / missing / permuted field is a marker the canonical producer could not
#: have rendered, and tolerating one is how a future meaningful field gets ignored by an old
#: verifier (the #14661 j#92533 F2 / j#92601 F2 lesson).
WAIVER_FIELD_ORDER: Tuple[str, ...] = (
    "gate",
    "version",
    "approval_source",
    "decision",
    "scope",
    "carve_out",
    "issue",
    "workspace",
    "lane",
    "lane_generation",
    "head",
)

#: The governed heading that DECLARES this gate. It is deliberately NOT added to the glance
#: grammar's lifecycle-gate allowlist: like ``## Gate: codex_direct_edit`` this is an issue-wide
#: authority declaration, not a step in the lane's lifecycle.
_HEADING_RE = re.compile(
    r"^\s{0,3}#{2,}\s*Gate\s*[:：]\s*no[ _]change[ _]review[ _]waiver\b.*$",
    re.MULTILINE | re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# The closed waiver vocabulary.
# ---------------------------------------------------------------------------

#: No waiver gate is in the durable record at all — the ordinary review path applies.
WAIVER_NONE = "none"
#: A VALID waiver: one canonical marker, every constant field at its literal value, a parseable
#: lane envelope.
WAIVER_WAIVED = "waived_state"
#: A waiver gate is DECLARED but cannot be read as one. Fail-closed: treated exactly like
#: :data:`WAIVER_NONE` by every consumer, and it SUPERSEDES an older valid waiver.
WAIVER_INVALID = "invalid"

NO_CHANGE_WAIVER_STATES: frozenset[str] = frozenset(
    {WAIVER_NONE, WAIVER_WAIVED, WAIVER_INVALID}
)


@dataclass(frozen=True)
class NoChangeWaiverFacts:
    """The LATEST durable no-change review waiver for one issue.

    ``state`` is a closed :data:`NO_CHANGE_WAIVER_STATES` token; ``journal`` is where the gate was
    recorded. ``issue`` and ``envelope`` are projected from the canonical marker and are EMPTY /
    ``None`` unless the waiver is valid — never guessed from prose, and never completed from the
    lane's current lifecycle row (that is precisely how a superseded generation's evidence gets
    promoted onto the live one).
    """

    state: str = WAIVER_NONE
    journal: str = ""
    issue: str = ""
    envelope: Optional[LaneEvidenceEnvelope] = None

    @property
    def recorded(self) -> bool:
        """True when any waiver gate journal (valid or not) is in the durable record."""
        return self.state != WAIVER_NONE

    @property
    def in_force(self) -> bool:
        """True ONLY for a VALID waiver. :data:`WAIVER_INVALID` is False, like an absent one."""
        return self.state == WAIVER_WAIVED and self.envelope is not None

    @property
    def head(self) -> str:
        """The lane head the waiver was recorded against, or ``""``."""
        return self.envelope.head if self.envelope is not None else ""

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state if self.state in NO_CHANGE_WAIVER_STATES else WAIVER_INVALID,
            "journal": str(self.journal or "").strip(),
            "issue": str(self.issue or "").strip(),
            "envelope": self.envelope.as_payload() if self.envelope is not None else None,
        }


def _raw_body_declares_gate(body: str) -> bool:
    """Whether ONE marker body names this gate, however the rest of it parses (pure).

    The existence half of latest-wins (the #14539 j#92012 F1 rule): a declaration supersedes by
    EXISTING, not by being readable, so a newer malformed waiver must still SHADOW an older valid
    one. Reading the raw components — not the strict parse — is what makes that possible.
    """
    for component in str(body or "").split(":"):
        key, _, value = component.partition("=")
        if key.strip() in MARKER_GATE_ALIASES and value.strip() == NO_CHANGE_REVIEW_WAIVER_GATE:
            return True
    return False


def _journal_waiver(notes: str) -> Optional[NoChangeWaiverFacts]:
    """The waiver ONE journal declares, or ``None`` if it declares none (pure).

    Qualification happens before any field is read, from the governed heading or from a marker
    that NAMES the gate. A qualifying journal then either carries exactly one canonical marker
    satisfying every literal — yielding :data:`WAIVER_WAIVED` — or folds to
    :data:`WAIVER_INVALID`.

    Both questions are answered from a SINGLE scan of the note
    (:func:`...canonical_note_scan.canonical_marker_bodies`). A reader that locates markers twice
    — once to count them and once to re-find their text — holds two notions of "where the marker
    is", and the weaker one wins: re-finding a body with a plain string search knows nothing about
    quote / fence exclusion, so a QUOTED marker earlier in the note is substituted for the
    canonical one (#14661 review j#92601 F2, measured). Location, exclusion and parsing are one
    authority here.

    A note carrying a marker that names this gate but cannot be read as one poisons the note for
    this gate — the readable siblings are NOT returned. Returning them would let a note carrying
    one clean marker plus one forged marker read exactly like a clean note, which is the readable
    subset the central `### Hibernate Evidence Marker Contract` refuses twice over.
    """
    text = notes or ""
    declared = _HEADING_RE.search(text) is not None
    readable: list[dict[str, str]] = []
    for _channel, body in canonical_marker_bodies(
        text, channels=frozenset({MARKER_CHANNEL_WORKFLOW_EVENT})
    ):
        if not _raw_body_declares_gate(body):
            continue  # some other surface's marker; not this gate's business
        declared = True
        # The SHARED closed-vocabulary reader. Stricter than the generic one on exactly the axes
        # a closed field set allows: a key repeated at all, an empty value, and any field set
        # other than this one are all bodies no producer here can render.
        fields = strict_marker_body_fields(body, expected=WAIVER_FIELD_ORDER)
        if fields is None:
            return NoChangeWaiverFacts(state=WAIVER_INVALID)
        # Field ORDER too, not merely the set: the canonical producer emits this sequence, so a
        # permutation is a marker nothing in this repo can render. The strict reader already
        # refused every repeated key, so the parsed mapping's order IS the body's order.
        if tuple(fields) != WAIVER_FIELD_ORDER:
            return NoChangeWaiverFacts(state=WAIVER_INVALID)
        readable.append(fields)

    if not declared:
        return None
    # Zero: the journal declares the gate (by heading, or by a marker) without carrying one
    # canonical marker — a heading cannot mint, it can only shadow (#14539 j#92174 F2).
    # Two or more: a record declaring this gate twice cannot say which one is authoritative.
    if len(readable) != 1:
        return NoChangeWaiverFacts(state=WAIVER_INVALID)
    fields = readable[0]

    constants = {
        "gate": NO_CHANGE_REVIEW_WAIVER_GATE,
        "version": WAIVER_VERSION,
        "approval_source": WAIVER_APPROVAL_SOURCE,
        "decision": WAIVER_DECISION,
        "scope": WAIVER_SCOPE,
        # The lane-bound half of the carve-out determination (review j#93704 finding 1). Any
        # other value is not this authority; the governed ``carve_out_check`` field must agree
        # independently, and that conjunction is enforced by the caller.
        "carve_out": WAIVER_CARVE_OUT_NONE,
    }
    if any(fields.get(key) != value for key, value in constants.items()):
        return NoChangeWaiverFacts(state=WAIVER_INVALID)

    issue = str(fields.get("issue", "") or "").strip()
    if not issue:
        return NoChangeWaiverFacts(state=WAIVER_INVALID)

    # The ONE envelope grammar (never a second copy): non-empty workspace / lane, a POSITIVE
    # generation, and a full lowercase 40/64-hex head. ``require_head=True`` because the head is
    # what makes post-waiver mutation detectable — a waiver with no head pins nothing.
    envelope = parse_lane_envelope(fields, require_head=True)
    if isinstance(envelope, EnvelopeParseError):
        return NoChangeWaiverFacts(state=WAIVER_INVALID)

    return NoChangeWaiverFacts(state=WAIVER_WAIVED, issue=issue, envelope=envelope)


def _int_journal(journal_id: object) -> Optional[int]:
    try:
        return int(str(journal_id).strip())
    except (TypeError, ValueError):
        return None


def fold_no_change_review_waiver(
    journals: Sequence[Tuple[object, str]],
) -> NoChangeWaiverFacts:
    """The LATEST durable no-change review waiver across one issue's journals (pure).

    Latest-wins by journal id, and **a declaration supersedes by EXISTING, not by being valid** —
    the invariant :func:`...review_exemption.fold_review_exemption` and
    :func:`...glance_integration_disposition.fold_work_unit` both carry (#13490 j#85365 F1). The
    newest structurally-qualifying journal is authoritative and only THEN is its content judged,
    so a malformed newer waiver SHADOWS an older valid one instead of being skipped so the stale
    one stays "latest".
    """
    latest: Optional[Tuple[int, NoChangeWaiverFacts]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        facts = _journal_waiver(notes)
        if facts is None:
            continue
        if latest is None or jint > latest[0]:
            latest = (
                jint,
                NoChangeWaiverFacts(
                    state=facts.state,
                    journal=str(jint),
                    issue=facts.issue,
                    envelope=facts.envelope,
                ),
            )
    return latest[1] if latest is not None else NoChangeWaiverFacts()


def review_round_supersedes(journal: object, round_journal_ids: Sequence[int]) -> bool:
    """Whether a review round is recorded AFTER ``journal`` (pure).

    The supersession half BOTH no-review-owed authorities share — the ``codex_direct_edit``
    exemption (#14539) and this waiver (#14695). One definition rather than two, because two
    copies of "is a review round newer than this authority" eventually answer differently for the
    same durable record, which is the drift this codebase keeps paying for when a second authority
    arrives (#13952). An unparseable journal id answers True — a round supersedes — because the
    ordering that makes the authority safe could not be established.

    Takes plain journal ids rather than the glance grammar's recognized-journal objects so the
    gate vocabulary stays in the grammar, where it belongs: the CALLER decides which journals
    constitute a review round (including that a combined ``## Gate: Review Request + Close`` IS
    one — the max-precedence reduction hid exactly that, #14539 review j#91577 F1), and this
    decides only the ordering.
    """
    anchor = _int_journal(journal)
    if anchor is None:
        return True
    rounds = [r for r in (round_journal_ids or ()) if isinstance(r, int)]
    return bool(rounds) and max(rounds) > anchor


def waiver_unsuperseded(
    waiver: NoChangeWaiverFacts, round_journal_ids: Sequence[int]
) -> bool:
    """Whether a VALID waiver stands unsuperseded by any newer review round (pure).

    Exposed separately from :func:`waived_now` so each consumer can report the refusal it actually
    hit. Folding supersession and zero-change into one boolean and handing THAT to the admission
    made a change-bearing record refuse as "superseded by a newer review round" — a diagnosis that
    names the wrong defect and would send an operator looking for a review that does not exist.
    Typed refusals are only worth having if each one is true.
    """
    return waiver.in_force and not review_round_supersedes(waiver.journal, round_journal_ids)


def waived_now(
    waiver: NoChangeWaiverFacts,
    zero_change: "ZeroChangeFacts",
    round_journal_ids: Sequence[int],
) -> bool:
    """Whether a no-change waiver is in force AND the record still declares zero change (pure).

    Three conjuncts, all fail-closed:

    - the waiver is VALID (a canonical marker with every literal and a parseable lane envelope);
    - no review round was opened AFTER it — a review requested on top of a waiver re-owes the
      review, exactly as it re-owes an exemption;
    - the durable record declares NO repository change. This conjunct is not decoration: a waiver
      says "there was nothing to review", so a record that declares a commit, a change scope or an
      integration disposition CONTRADICTS its own premise. Leaving it out of the DERIVED fact
      would let the glance say "no review owed" while the terminal retire refused the identical
      record — the disagreement #14539 j#90137 F3 measured, and fixed by making both consumers
      read one fact.

    The LIVE half of the zero-change proof is deliberately absent here: the glance is a read-only
    projection over durable journals with no repository to probe. That asymmetry is safe in this
    direction only, because the retire adds the live conjuncts ON TOP of this one — the retire is
    strictly stricter than the glance, never looser.
    """
    # The SAME gate the retire admission ends on, so the glance cannot say "no review owed" about a
    # record the retire refuses (the one-authority-two-consumers rule this module has held since
    # R1). Leaving the glance projecting while the retire refuses would re-open exactly the
    # disagreement #14539 j#90137 F3 fixed.
    if not WRITER_AUTHORITY_RESOLVABLE:
        return False
    return waiver_unsuperseded(waiver, round_journal_ids) and zero_change.proven


# ---------------------------------------------------------------------------
# The zero-change carve-out: does the durable record declare ANY change?
# ---------------------------------------------------------------------------

#: The record declares nothing that would constitute repository change.
ZERO_CHANGE_PROVEN = "zero_change_declared"
#: A journal declares a commit (in any governed commit field).
ZERO_CHANGE_COMMIT_DECLARED = "commit_declared_in_record"
#: A journal declares a change scope (``changed_paths``, or a change-bearing gate naming a commit).
ZERO_CHANGE_SCOPE_DECLARED = "change_scope_declared_in_record"
#: An integration disposition is recorded — something reached, or was withheld from, the
#: integration branch, which only work that exists can be.
ZERO_CHANGE_INTEGRATION_DECLARED = "integration_disposition_recorded"
#: One journal declares two DIFFERENT integration dispositions. Not "no disposition": a record
#: that says two things has said something, and reading the ambiguity as absence is exactly the
#: "malformed / conflicting を『無い』と読み飛ばさない" condition the #14695 ruling adds.
ZERO_CHANGE_INTEGRATION_AMBIGUOUS = "integration_disposition_declaration_conflicts"

ZERO_CHANGE_REASONS: frozenset[str] = frozenset(
    {
        ZERO_CHANGE_PROVEN,
        ZERO_CHANGE_COMMIT_DECLARED,
        ZERO_CHANGE_SCOPE_DECLARED,
        ZERO_CHANGE_INTEGRATION_DECLARED,
        ZERO_CHANGE_INTEGRATION_AMBIGUOUS,
    }
)


@dataclass(frozen=True)
class ZeroChangeFacts:
    """Whether the durable record declares that this lane produced NO repository change.

    ``proven`` is the fail-closed predicate; ``reason`` names which declaration refused it, and
    ``journal`` where that declaration is, so the refusal is diagnosable rather than a bare False.
    """

    proven: bool = False
    reason: str = ZERO_CHANGE_PROVEN
    journal: str = ""


def fold_zero_change_record(
    journals: Sequence[Tuple[object, str]],
    *,
    change_bearing_journals: Sequence[object] = (),
) -> ZeroChangeFacts:
    """Whether the issue's durable record declares ZERO repository change (pure).

    This is the carve-out the acceptance names literally: ordinary development, a guardrail
    change and a release all leave declarations in the record, and any ONE of them refuses the
    waiver:

    1. **a commit** in any governed commit field, read with the SAME grammar the exemption module
       uses (:func:`...review_exemption.declares_any_commit`) so the two authorities cannot
       disagree about what declaring a commit looks like;
    2. **a change scope** — a ``changed_paths`` field, or a change-bearing gate
       (``implementation_done`` / ``review_request``) naming a commit. Reused from
       :func:`...review_exemption.fold_declared_change_scope`, which already owns that definition,
       including its "absence is not a declaration" boundary;
    3. **an integration disposition** of any kind. ``merge`` / ``patch_equivalent`` say work
       reached the integration branch; ``explicit_deferral`` / ``integration_blocked`` say work
       is waiting to. All four presuppose work that exists, so all four refuse — this is not
       asking whether integration SUCCEEDED, it is asking whether there was anything to integrate.
       An out-of-vocabulary disposition folds to ``unknown``, which is likewise RECORDED and
       likewise refuses.
    4. **a CONFLICTING integration declaration** — one journal naming two different dispositions.
       The lenient display fold resolves that by line order, so asking it alone would let the
       ambiguity read as whichever came first, and a conflict written in the "no disposition"
       direction would read as absence. #14695 j#93406 fixes this explicitly: a malformed /
       conflicting change candidate is INVALID, never skipped as "not there". This is the same
       parser/consumer split #14539 j#91696 F3 established for the exemption route.

    Deliberately checked in that order so the reported reason is the most concrete one available.

    **This is a NEGATIVE claim over the WHOLE record, which is why its input must be complete.**
    A positive authority (a gate exists) cannot be fabricated by handing the reader a subset of
    the journals; a negative one is satisfied by omission alone — drop the journal that declares
    the commit and the record "declares no change". The caller is therefore required to supply
    the authoritative full issue history, and the CLI route reads it from the credential-gated
    Redmine read rather than from a caller-supplied file (#14695 j#93406 condition 2). Nothing in
    this pure function can enforce that, which is precisely why it is stated here.

    ``change_bearing_journals`` is supplied by the caller that owns gate recognition, exactly as
    the exemption route supplies it, so the gate vocabulary stays in ONE place. Its default is
    empty, which leaves only the ``changed_paths`` half of (2) — the total, strictly-safe
    behaviour for a direct caller that cannot classify gates.
    """
    for journal_id, notes in journals or ():
        if _int_journal(journal_id) is None:
            continue
        if declares_any_commit(notes):
            return ZeroChangeFacts(
                proven=False,
                reason=ZERO_CHANGE_COMMIT_DECLARED,
                journal=str(journal_id).strip(),
            )

    scope = fold_declared_change_scope(
        journals, change_bearing_journals=change_bearing_journals
    )
    if scope.journal:
        return ZeroChangeFacts(
            proven=False, reason=ZERO_CHANGE_SCOPE_DECLARED, journal=scope.journal
        )

    integration = fold_integration_disposition(journals or ()).validated()
    if integration.recorded:
        return ZeroChangeFacts(
            proven=False,
            reason=ZERO_CHANGE_INTEGRATION_DECLARED,
            journal=integration.journal,
        )
    # Asked AFTER the fold, because a fold that reports a disposition has already refused above;
    # this catches the case the fold CANNOT report — a journal whose two declarations include the
    # empty / absent direction, where the lenient resolution can land on "none".
    if has_conflicting_disposition_declaration(journals or ()):
        return ZeroChangeFacts(proven=False, reason=ZERO_CHANGE_INTEGRATION_AMBIGUOUS)

    return ZeroChangeFacts(proven=True, reason=ZERO_CHANGE_PROVEN)


# ---------------------------------------------------------------------------
# The terminal-retire admissibility fence for a waived lane (#14695 acceptance 2).
# ---------------------------------------------------------------------------

#: No waiver gate is in the durable record at all.
REASON_NO_WAIVER_RECORDED = "no_change_review_waiver_not_recorded"
#: A waiver gate is declared but cannot be read as one.
REASON_WAIVER_INVALID = "invalid_no_change_review_waiver"
#: A review round was opened AFTER the waiver, so an independent review is owed again.
REASON_WAIVER_SUPERSEDED = "no_change_review_waiver_superseded_by_newer_review_round"
#: The waiver marker names a different issue than the one being retired.
REASON_WAIVER_ISSUE_MISMATCH = "no_change_review_waiver_names_another_issue"
#: The waiver's lane envelope is not the retire target's lane / workspace / generation.
REASON_WAIVER_LANE_MISMATCH = "no_change_review_waiver_names_another_lane_or_generation"
#: The issue is not durably closed, so there is no Close evidence to re-verify.
REASON_CLOSE_NOT_RECORDED = "close_not_recorded"
#: The durable record declares repository change, which is the carve-out this waiver may not
#: cross. The concrete :data:`ZERO_CHANGE_REASONS` token is carried in the detail.
REASON_CHANGE_DECLARED = "record_declares_repository_change"
#: The live lane head could not be measured, so post-waiver mutation could not be ruled out.
REASON_LANE_HEAD_UNMEASURED = "lane_head_not_measured"
#: The live lane head is not the head the waiver was recorded against.
REASON_POST_WAIVER_MUTATION = "lane_head_moved_after_the_waiver"
#: The lane carries commits the integration branch does not, so it did produce change.
REASON_LANE_COMMITS_PRESENT = "lane_carries_commits_over_the_integration_branch"
#: The lane worktree is dirty, or could not be read. Uncommitted change IS repository change the
#: waiver did not cover, and an unreadable checkout cannot testify that there is none
#: (#14695 j#93406 condition 3).
REASON_WORKTREE_NOT_CLEAN = "lane_worktree_not_proven_clean"
#: A coordinator callback is still owed, so the lane's own workflow has not converged.
REASON_CALLBACK_OWED = "coordinator_callback_still_owed"
#: Everything the record CAN establish checks out, but who wrote the waiver cannot be established
#: at all (:data:`WRITER_AUTHORITY_RESOLVABLE`). This is the typed refusal the issue's Acceptance
#: names, not a gap in the record: no input to this module can satisfy it today.
REASON_WRITER_AUTHORITY_UNRESOLVED = "waiver_writer_authority_unresolvable"
#: A recognized durable fact names a hard carve-out surface (release / production verification /
#: credential / destructive / migration / external effect). Direct owner provenance does not
#: override this (#14695 j#93412 §3).
REASON_HARD_CARVE_OUT = "hard_carve_out_surface_recorded"
#: The hard carve-out's non-applicability could not be PROVEN. Distinct from the above because
#: "we checked and it applies" and "we could not check" are different operational problems, and
#: the ruling refuses BOTH rather than defaulting the second one clear.
REASON_HARD_CARVE_OUT_UNRESOLVED = "hard_carve_out_not_proven_inapplicable"

NO_CHANGE_WAIVER_REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        REASON_NO_WAIVER_RECORDED,
        REASON_WAIVER_INVALID,
        REASON_WAIVER_SUPERSEDED,
        REASON_WAIVER_ISSUE_MISMATCH,
        REASON_WAIVER_LANE_MISMATCH,
        REASON_CLOSE_NOT_RECORDED,
        REASON_CHANGE_DECLARED,
        REASON_LANE_HEAD_UNMEASURED,
        REASON_POST_WAIVER_MUTATION,
        REASON_LANE_COMMITS_PRESENT,
        REASON_WORKTREE_NOT_CLEAN,
        REASON_CALLBACK_OWED,
        REASON_HARD_CARVE_OUT,
        REASON_HARD_CARVE_OUT_UNRESOLVED,
        REASON_WRITER_AUTHORITY_UNRESOLVED,
    }
)


def evaluate_no_change_waiver_admissible(
    waiver: NoChangeWaiverFacts,
    *,
    currently_in_force: bool,
    zero_change: ZeroChangeFacts,
    carve_out: HardCarveOutFacts,
    close_recorded: bool,
    target_issue: str,
    expected_workspace: str = "",
    expected_lane: str = "",
    expected_lane_generation: int = 0,
    live_head: str = "",
    live_commits_ahead: Optional[int] = None,
    worktree_clean: bool = False,
    callbacks_drained: bool = False,
) -> AdmissionResult:
    """Whether a WAIVED lane may pass the terminal retire's latest-generation fence (pure).

    A no-change lane has no review generation, so
    :func:`...review_generation.evaluate_integration_admissible` can only ever answer
    ``no_approval_for_latest_generation``. Rather than have the coordinator falsely assert
    ``--latest-generation-admissible`` about a review that never happened, the retire re-verifies
    at action time the facts that actually carry the same safety weight:

    1. a VALID waiver that is CURRENTLY in force — no review round opened on top of it;
    2. that waiver is about THIS issue and THIS exact lane generation;
    3. the issue is durably CLOSED (its own close contract was satisfied upstream);
    4. the durable record declares no repository change at all;
    4b. the HARD carve-out is provably not applicable — no recognized durable fact names a
       release / production-verification / credential / destructive / migration / external-effect
       surface, and the issue's classification actually RESOLVED. A waiver's own
       ``scope=no_change_investigation`` field is necessary but never sufficient for this, and
       ``approval_source=direct_owner`` is not an escape from it (#14695 j#93412 §3);
    5. the live repository still agrees: the lane head is exactly the head the waiver was
       recorded against, the lane carries no commit the integration branch lacks, and the lane
       worktree is proven CLEAN — uncommitted change is repository change the waiver never
       covered, and a checkout that cannot be read cannot testify that there is none;
    6. no coordinator callback is still owed, so the lane's own workflow has converged rather
       than being terminalized mid-flight.

    ``currently_in_force`` is the SUPERSESSION-aware fact the glance classifier consumes, not
    :attr:`NoChangeWaiverFacts.in_force`. #14539 review j#90137 F3 measured what reading the bare
    state costs: the retire admitted a lane whose authority a newer review round had already
    superseded, so the retire and the glance disagreed about the very same durable record. One
    authority, two consumers.

    **Why (4) and (5) are BOTH required, and why neither is sufficient.** Git alone cannot
    distinguish "this lane never produced a commit" from "this lane's commits are already merged
    into the integration branch" — both leave zero commits ahead. The record half is what excludes
    the merged case, because integrated work necessarily leaves a commit record and an integration
    disposition behind (the preset requires an origin-reachable commit hash on
    ``implementation_done`` / ``close``, and an integration disposition on the coordinator's
    integration). The live half is what excludes the opposite failure: a record that is silent
    about change while the working lane has in fact moved since the waiver was written. Claiming
    either half proves zero change on its own would be false, so both are conjoined and the
    boundary is stated rather than papered over.

    ``live_commits_ahead`` is the count of commits the lane branch carries over the integration
    branch, and ``live_head`` the lane branch's current head — both measured by the caller at
    action time. ``None`` / ``""`` mean NOT MEASURED, which is fail-closed: an unmeasurable
    repository cannot testify that nothing changed. ``worktree_clean`` and ``callbacks_drained``
    default to the UNSATISFIED value for the same reason every authority-bearing invariant in this
    context does: a caller that omits them is refused, never default-admitted.

    Every refusal is a typed :data:`NO_CHANGE_WAIVER_REFUSAL_REASONS` token. There is no input to
    this function that turns an unreadable, foreign, stale or change-bearing record into an
    admission.
    """
    if waiver.state == WAIVER_NONE:
        return AdmissionResult(False, REASON_NO_WAIVER_RECORDED)
    if waiver.state != WAIVER_WAIVED or waiver.envelope is None:
        return AdmissionResult(False, REASON_WAIVER_INVALID)
    if not currently_in_force:
        return AdmissionResult(False, REASON_WAIVER_SUPERSEDED)

    # The waiver must be ABOUT the issue being retired. Both sides must be present and equal as
    # literals — a blank on either side correlates to nothing, and durable evidence from another
    # issue must never unlock this fence (the #14539 F2 rule, which was measured to matter).
    issue = str(target_issue or "").strip()
    if not issue or waiver.issue != issue:
        return AdmissionResult(False, REASON_WAIVER_ISSUE_MISMATCH)

    # …and about this exact lane INCARNATION. The expectation is the caller's measurement of the
    # retire target's own lifecycle row, never a value the caller invented for this comparison
    # (#14539 review j#91797 F2): an identity the caller chooses fences nothing, because it can
    # simply be pointed at whatever the evidence happens to say. An unresolved expectation is
    # blank / non-positive here and refuses.
    envelope = waiver.envelope
    if (
        not str(expected_workspace or "").strip()
        or not str(expected_lane or "").strip()
        or not isinstance(expected_lane_generation, int)
        or isinstance(expected_lane_generation, bool)
        or expected_lane_generation <= 0
        or envelope.workspace != str(expected_workspace).strip()
        or envelope.lane != str(expected_lane).strip()
        or envelope.lane_generation != expected_lane_generation
    ):
        return AdmissionResult(False, REASON_WAIVER_LANE_MISMATCH)

    if not close_recorded:
        return AdmissionResult(False, REASON_CLOSE_NOT_RECORDED)

    if not callbacks_drained:
        return AdmissionResult(False, REASON_CALLBACK_OWED)

    if not zero_change.proven:
        return AdmissionResult(False, REASON_CHANGE_DECLARED)

    # The HARD carve-out, asked as its own conjunct and NOT satisfiable by the marker's own
    # ``scope`` field (#14695 j#93412 §3). Its two refusals stay distinguishable, and neither is
    # reachable by a ``direct_owner`` provenance claim — provenance says who decided, not what
    # surface the work touched.
    #
    # Note what the two halves each contribute (review j#93704 finding 1). The marker's
    # ``carve_out=none`` — validated above as part of the closed field set — binds a determination
    # to THIS lane envelope and head, so changing it requires forging that identity rather than
    # appending a note. ``carve_out`` here is the INDEPENDENT governed ``carve_out_check`` field
    # plus the recognized-fact detection. Requiring both is what keeps the marker from certifying
    # itself while still denying an arbitrary journal the power to write the clear.
    if not carve_out.clear:
        return AdmissionResult(
            False,
            REASON_HARD_CARVE_OUT
            if carve_out.reason == CARVE_OUT_DECLARED
            else REASON_HARD_CARVE_OUT_UNRESOLVED,
        )

    if not worktree_clean:
        return AdmissionResult(False, REASON_WORKTREE_NOT_CLEAN)

    head = str(live_head or "").strip().lower()
    if not head or live_commits_ahead is None:
        return AdmissionResult(False, REASON_LANE_HEAD_UNMEASURED)
    if head != envelope.head.strip().lower():
        return AdmissionResult(False, REASON_POST_WAIVER_MUTATION)
    if not isinstance(live_commits_ahead, int) or isinstance(live_commits_ahead, bool):
        return AdmissionResult(False, REASON_LANE_HEAD_UNMEASURED)
    if live_commits_ahead != 0:
        return AdmissionResult(False, REASON_LANE_COMMITS_PRESENT)

    # LAST, on purpose. Everything above still reports its own true cause, so a malformed or
    # change-bearing record is diagnosed precisely rather than being swallowed by this refusal.
    # What reaches here is a record in which every establishable fact holds — and the one fact
    # this system cannot establish is who wrote it (:data:`WRITER_AUTHORITY_RESOLVABLE`).
    if not WRITER_AUTHORITY_RESOLVABLE:
        return AdmissionResult(False, REASON_WRITER_AUTHORITY_UNRESOLVED)

    return AdmissionResult(True, REASON_OK)


def render_no_change_review_waiver_marker(
    *,
    issue: str,
    workspace: str,
    lane: str,
    lane_generation: object,
    head: str,
) -> str:
    """The exact marker a valid no-change review waiver must carry (pure).

    Rendered so the coordinator recording a waiver can see precisely what to write — an authority
    contract nobody can produce is an authority contract nobody will use. Field order is
    :data:`WAIVER_FIELD_ORDER`, so what this emits is what the strict reader accepts, by
    construction.

    A renderer that accepts what its own parser refuses is not a strict grammar: it produces
    durable records that read back as a typed zero, so the authority silently does not count. Every
    producer error therefore raises ``ValueError`` here rather than being written — the envelope's
    own :func:`...hibernate_evidence_envelope.render_lane_envelope` enforces the workspace / lane /
    generation / head rules and the marker-separator rejection, and this adds the issue.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
        reject_marker_separator,
        render_lane_envelope,
    )

    issue_s = str(issue or "").strip()
    if not issue_s:
        raise ValueError("a no-change review waiver requires a non-empty issue")
    reject_marker_separator(issue_s, field="issue")
    try:
        generation = int(lane_generation)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(
            f"a no-change review waiver requires an integer lane_generation, "
            f"got {lane_generation!r}"
        ) from None
    if isinstance(lane_generation, bool):
        raise ValueError("a no-change review waiver requires an integer lane_generation")
    # Rendered through the envelope's own renderer so the head / generation / separator rules have
    # exactly one definition; it raises on a non-positive generation or a non-full-SHA head.
    envelope_body = render_lane_envelope(
        LaneEvidenceEnvelope(
            workspace=str(workspace or "").strip(),
            lane=str(lane or "").strip(),
            lane_generation=generation,
            head=str(head or "").strip(),
        )
    )
    if not str(head or "").strip():
        raise ValueError("a no-change review waiver requires the lane head it was recorded at")
    body = ":".join(
        [
            f"gate={NO_CHANGE_REVIEW_WAIVER_GATE}",
            f"version={WAIVER_VERSION}",
            f"approval_source={WAIVER_APPROVAL_SOURCE}",
            f"decision={WAIVER_DECISION}",
            f"scope={WAIVER_SCOPE}",
            f"carve_out={WAIVER_CARVE_OUT_NONE}",
            f"issue={issue_s}",
            envelope_body,
        ]
    )
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"


__all__ = (
    "NO_CHANGE_REVIEW_WAIVER_GATE",
    "NO_CHANGE_WAIVER_REFUSAL_REASONS",
    "NO_CHANGE_WAIVER_STATES",
    "NoChangeWaiverFacts",
    "REASON_CALLBACK_OWED",
    "REASON_CHANGE_DECLARED",
    "REASON_CLOSE_NOT_RECORDED",
    "REASON_HARD_CARVE_OUT",
    "REASON_HARD_CARVE_OUT_UNRESOLVED",
    "REASON_LANE_COMMITS_PRESENT",
    "REASON_LANE_HEAD_UNMEASURED",
    "REASON_NO_WAIVER_RECORDED",
    "REASON_POST_WAIVER_MUTATION",
    "REASON_WAIVER_INVALID",
    "REASON_WAIVER_ISSUE_MISMATCH",
    "REASON_WAIVER_LANE_MISMATCH",
    "REASON_WAIVER_SUPERSEDED",
    "REASON_WRITER_AUTHORITY_UNRESOLVED",
    "REASON_WORKTREE_NOT_CLEAN",
    "WAIVER_APPROVAL_SOURCE",
    "WRITER_AUTHORITY_RESOLVABLE",
    "WAIVER_CARVE_OUT_NONE",
    "WAIVER_DECISION",
    "WAIVER_FIELD_ORDER",
    "WAIVER_INVALID",
    "WAIVER_NONE",
    "WAIVER_SCOPE",
    "WAIVER_VERSION",
    "WAIVER_WAIVED",
    "ZERO_CHANGE_COMMIT_DECLARED",
    "ZERO_CHANGE_INTEGRATION_AMBIGUOUS",
    "ZERO_CHANGE_INTEGRATION_DECLARED",
    "ZERO_CHANGE_PROVEN",
    "ZERO_CHANGE_REASONS",
    "ZERO_CHANGE_SCOPE_DECLARED",
    "ZeroChangeFacts",
    "evaluate_no_change_waiver_admissible",
    "fold_no_change_review_waiver",
    "fold_zero_change_record",
    "render_no_change_review_waiver_marker",
    "review_round_supersedes",
    "waived_now",
    "waiver_unsuperseded",
)
