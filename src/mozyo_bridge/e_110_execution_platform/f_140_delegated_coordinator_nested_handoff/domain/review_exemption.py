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
    declares_gate,
    strict_gate_markers,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_generation import (
    REASON_OK,
    AdmissionResult,
)

#: The marker gate that declares a journal to BE a ``codex_direct_edit`` gate. Read through the
#: policy-free shared marker scanner (never by widening
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
#: The gate's own fields are well-formed, but its ``allowed_paths`` could NOT be shown to cover
#: the durable record's declared changed paths for the target commit (Redmine #14539 review
#: j#90137 F1). The central preset's ``close.review_exemption`` requires a gate covering *the
#: whole changed-path set of the target commit*, so a gate whose coverage is unproven — no
#: declared change scope to check against, or a changed path no glob matches — is NOT an
#: exemption. Distinct from :data:`EXEMPTION_INVALID` only for diagnosis; every consumer treats
#: it the same fail-closed way.
EXEMPTION_PATH_COVERAGE_UNPROVEN = "path_coverage_unproven"

REVIEW_EXEMPTION_STATES: frozenset[str] = frozenset(
    {
        EXEMPTION_NONE,
        EXEMPTION_EXEMPT,
        EXEMPTION_REVIEW_REQUIRED,
        EXEMPTION_INVALID,
        EXEMPTION_PATH_COVERAGE_UNPROVEN,
    }
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


def _field_label_re(*names: str) -> "re.Pattern[str]":
    """A line-anchored governed field LABEL matcher whose inline value may be empty (pure).

    The governed templates write list-valued fields with an empty inline value and the items on
    following indented list lines (``- allowed_paths:`` / ``- changed_paths:`` then ``  - src/**``),
    so a matcher that requires a non-empty inline value cannot see them at all.
    """
    alternation = "|".join(re.escape(n) for n in names)
    return re.compile(
        r"^(?P<indent>[ \t]*)[-*]?[ \t]*\**[ \t]*(?:" + alternation + r")\**[ \t]*[:：][ \t]*"
        r"(?P<value>.*?)[ \t]*$",
        re.MULTILINE | re.IGNORECASE,
    )


_ROLE_FIELD_RE = _field_re("role")
_DIRECT_EDIT_FIELD_RE = _field_re("direct_edit", "direct edit")
_ALLOWED_PATHS_FIELD_RE = _field_label_re("allowed_paths", "allowed paths")
_CHANGED_PATHS_FIELD_RE = _field_label_re("changed_paths", "changed paths")
_REASON_FIELD_RE = _field_re("reason")
_FOLLOW_UP_REVIEW_FIELD_RE = _field_re("follow_up_review", "follow up review")

#: An explicit commit-hash field on a governed journal (the same shapes the glance grammar's
#: ``_COMMIT_FIELD_RE`` accepts). Used to tie a declared change scope to the commit it describes.
_COMMIT_FIELD_RE = re.compile(
    r"(?im)^[ \t]*[-*]?[ \t]*\**[ \t]*"
    r"(?:commit|commit_or_diff|commit_hash|target_commit(?:_or_diff)?)\**[ \t]*[:：][ \t]*"
    r"\**`?[ \t]*(?P<value>[0-9a-f]{7,64})"
)

#: A following list item line (``  - `src/**` (新規)``) under a list-valued field label.
_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)[-*][ \t]+(?P<value>.+?)[ \t]*$")

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


def _unique_field(pattern: "re.Pattern[str]", notes: str) -> Tuple[str, bool]:
    """A governed scalar field's single agreed value, plus whether it CONFLICTS (pure).

    Returns ``(value, conflicted)``. Reading a governed authority field with ``search()`` takes
    the FIRST occurrence and silently ignores every later one, so a record carrying both
    ``follow_up_review: false`` and ``follow_up_review: true`` folded to an exemption while the
    durable text says the owner required a review (Redmine #14539 review j#91577 finding 3).

    The rule, stated because the reviewer left the equal-duplicate case to the implementation:

    - zero occurrences -> ``("", False)`` — absent, judged by the caller's required-field check;
    - occurrences that all clean to the SAME value -> that value, not a conflict. A duplicated
      identical line is a transcription artifact, not two different declarations;
    - occurrences that clean to DIFFERENT values -> ``("", True)``. Such a record is not
      incomplete, it is not uniquely interpretable, and an authority gate that cannot be read
      one way must fail closed rather than pick a winner by position.
    """
    values = {_clean(m.group("value")) for m in pattern.finditer(notes or "")}
    if len(values) > 1:
        return "", True
    return (values.pop() if values else ""), False


def _clean_path(value: object) -> str:
    """Strip decoration and one trailing parenthetical off ONE path entry (pure).

    Uses :data:`_PATH_DECORATION_RE`, NOT the generic :data:`_DECORATION_RE`: a path glob's
    trailing ``**`` is semantic, and treating it as Markdown emphasis silently narrowed
    ``vibes/docs/rules/**`` to ``vibes/docs/rules/``. The trailing parenthetical strip is what
    lets a governed author annotate an entry (`` `src/x.py` (新規)``).
    """
    text = _TRAILING_PAREN_RE.sub("", str(value or "").strip())
    return _PATH_DECORATION_RE.sub("", text).strip()


def _path_field(pattern: "re.Pattern[str]", notes: str) -> Tuple[str, ...]:
    """A governed list-valued path field as a tuple of entries (pure).

    Reads BOTH governed shapes, because the templates produce both:

    - inline — ``- allowed_paths: src/**, tests/**`` (comma / whitespace separated);
    - continuation list — the label with an empty inline value followed by MORE-indented list
      items, which is exactly what the ``## Gate: codex_direct_edit`` /
      ``## Gate: review_request`` templates invite (they ship ``- allowed_paths:`` /
      ``- changed_paths:`` with nothing after the colon).

    Redmine #14539 review j#90137: the first implementation read the inline form only. That was
    fail-closed, but the continuation form is the shape real governed journals actually use
    (this issue's own review_request j#89842 writes ``changed_paths`` that way), so reading only
    the inline form would have made every real record unverifiable rather than merely strict.

    Scanning stops at the first line that is not a more-indented list item, so a following
    sibling field or a new section never leaks entries into the list.
    """
    values = _path_field_values(pattern, notes)
    return values[0] if values else ()


def _path_field_values(
    pattern: "re.Pattern[str]", notes: str
) -> list[Tuple[str, ...]]:
    """Every occurrence of a governed list-valued path field, in order (pure)."""
    return [_path_entries_at(match, notes or "") for match in pattern.finditer(notes or "")]


def _unique_path_field(
    pattern: "re.Pattern[str]", notes: str
) -> Tuple[Tuple[str, ...], bool]:
    """A governed list field's single agreed value, plus whether it CONFLICTS (pure).

    The list-valued counterpart of :func:`_unique_field`, with the same rule and the same
    reason: a second ``allowed_paths`` label declaring a different set makes the gate's path
    authority ambiguous, and ``search()`` resolved that ambiguity by position (Redmine #14539
    review j#91577 finding 3).
    """
    values = _path_field_values(pattern, notes)
    distinct = set(values)
    if len(distinct) > 1:
        return (), True
    return (distinct.pop() if distinct else ()), False


def _path_entries_at(match: "re.Match[str]", notes: str) -> Tuple[str, ...]:
    """The entries of ONE governed list-field occurrence (pure). See :func:`_path_field`."""
    entries: list[str] = []
    inline = _TRAILING_PAREN_RE.sub("", str(match.group("value") or "").strip())
    if inline:
        entries.extend(_PATH_SPLIT_RE.split(inline))
    label_indent = len(str(match.group("indent") or "").expandtabs(4))
    # ``match`` ends at the label line's end-of-line, so the slice begins with that line's
    # newline; ``split("\n")[1:]`` drops it and yields the FOLLOWING lines. (``splitlines()``
    # here would yield a leading "" and stop the scan before the first item.)
    for line in (notes or "")[match.end() :].split("\n")[1:]:
        if not line.strip():
            break
        item = _LIST_ITEM_RE.match(line)
        if item is None:
            break
        if len(str(item.group("indent") or "").expandtabs(4)) <= label_indent:
            break
        entries.append(item.group("value"))
    return tuple(p for p in (_clean_path(e) for e in entries) if p)


# ---------------------------------------------------------------------------
# Path coverage: does the gate's ``allowed_paths`` cover the declared changed paths?
# ---------------------------------------------------------------------------


#: Segments a repository-relative canonical path can never contain. An empty segment is a leading
#: ``/`` or a ``//``; ``.`` / ``..`` are traversal, not a canonical name.
_NON_CANONICAL_SEGMENTS: frozenset[str] = frozenset({"", ".", ".."})


def is_canonical_relative_path(value: object) -> bool:
    """Whether ``value`` is a repository-relative canonical POSIX path (pure).

    Redmine #14539 review j#91577 finding 4. The matcher used to *normalize* its inputs — it
    dropped empty segments — so ``/src/a.py`` (absolute) and ``src//**`` (a double slash) were
    silently rewritten into the canonical forms and then matched. Coverage of a review exemption
    must not be granted to an input the record never actually declared in the governed form, so
    a non-canonical path is now unusable in either role: as a changed path it stays uncovered,
    and as an ``allowed_paths`` glob it matches nothing. Both directions withhold the exemption,
    which is the fail-closed side.

    Rejected: an empty string, a leading ``/``, any ``//``, a trailing ``/``, a ``\\`` separator,
    and any ``.`` / ``..`` segment. Glob metacharacters are NOT paths characters and are judged
    by :func:`_segment_match`, not here — ``src/**`` is canonical, ``src//**`` is not.
    """
    text = str(value or "").strip()
    if not text or "\\" in text:
        return False
    return all(seg not in _NON_CANONICAL_SEGMENTS for seg in text.split("/"))


def _segment_match(segment: str, pattern: str) -> bool:
    """Match ONE path segment against one pattern segment (pure).

    The vocabulary is CLOSED and is exactly what :func:`_glob_match` documents: ``*`` matches any
    run of characters within the segment, ``?`` matches one, and every other character — ``[``
    and ``]`` included — is a literal.

    This no longer delegates to ``fnmatchcase``. Applying it per segment did keep ``*`` from
    crossing ``/``, but it also brought fnmatch's ``[...]`` character classes in, so
    ``allowed_paths: src/[ab].py`` covered ``src/a.py`` — a path authority accepting a grammar
    its own contract does not declare (Redmine #14539 review j#91577 finding 4).
    """
    translated = "".join(
        "[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in pattern
    )
    return re.fullmatch(translated, segment) is not None


def _glob_match(path: str, pattern: str) -> bool:
    """Whether one POSIX-ish path matches one governed ``allowed_paths`` glob (pure).

    ``**`` matches zero or more whole segments; ``*`` / ``?`` match within a single segment;
    anything else is an exact segment — character classes and every other glob dialect are
    literals, because that is the whole vocabulary this contract declares. Deliberately a small
    explicit matcher rather than a ``fnmatch`` of the whole string (whose ``*`` crosses ``/``, so
    ``src/*`` would match ``src/a/b/c.py`` and over-grant coverage) and rather than
    ``PurePath.full_match`` (3.13+).

    Both sides must be repository-relative canonical paths
    (:func:`is_canonical_relative_path`); anything else matches nothing rather than being
    normalized into a form that would.
    """
    if not is_canonical_relative_path(path) or not is_canonical_relative_path(pattern):
        return False
    segs = str(path or "").strip().split("/")
    pats = str(pattern or "").strip().split("/")
    if not segs or not pats:
        return False

    def walk(i: int, j: int) -> bool:
        if j == len(pats):
            return i == len(segs)
        if pats[j] == "**":
            return any(walk(k, j + 1) for k in range(i, len(segs) + 1))
        if i == len(segs) or not _segment_match(segs[i], pats[j]):
            return False
        return walk(i + 1, j + 1)

    return walk(0, 0)


def uncovered_paths(
    changed_paths: Sequence[str], allowed_paths: Sequence[str]
) -> Tuple[str, ...]:
    """The declared changed paths NO ``allowed_paths`` glob covers (pure).

    Empty iff every changed path is covered. Callers treat a non-empty result as "this gate does
    not cover the commit", which is the central preset's actual exemption condition.
    """
    allowed = [p for p in (str(a).strip() for a in allowed_paths or ()) if p]
    if not allowed:
        return tuple(str(p).strip() for p in changed_paths or () if str(p).strip())
    return tuple(
        p
        for p in (str(c).strip() for c in changed_paths or ())
        if p and not any(_glob_match(p, a) for a in allowed)
    )


@dataclass(frozen=True)
class DeclaredChangeScope:
    """The change scope the durable record declares: a target commit and its changed paths.

    Both come from ONE journal — the latest that carries a governed ``changed_paths`` field — so
    the paths are tied to the commit that journal declares, rather than being stitched together
    across unrelated records (Redmine #14539 review j#90137 F1: "対象 commit と changed paths を
    durable evidence から相関し").

    ``proven`` is the fail-closed predicate: without BOTH a commit and a non-empty path set there
    is nothing to check an exemption's coverage against, and "we could not check" must never read
    as "covered".
    """

    commit: str = ""
    paths: Tuple[str, ...] = ()
    journal: str = ""

    @property
    def proven(self) -> bool:
        return bool(self.commit and self.paths)


@dataclass(frozen=True)
class ReviewExemptionFacts:
    """The latest durable ``codex_direct_edit`` review exemption for one issue.

    ``state`` is the closed :data:`REVIEW_EXEMPTION_STATES` token. ``journal`` is the journal the
    gate was recorded at. ``allowed_paths`` / ``reason`` are projected from governed structured
    field lines and are EMPTY when the record does not carry them — never guessed from prose.

    ``covered_commit`` is the target commit the coverage check ran against (empty when no change
    scope was declared), and ``uncovered`` names the declared changed paths no ``allowed_paths``
    glob matched. Both are diagnosis for :data:`EXEMPTION_PATH_COVERAGE_UNPROVEN`.

    ``covered_scope_journal`` is the journal that declared that scope. It is what lets a consumer
    ask whether OTHER evidence about the same lane is newer than the scope it claims to be about
    (Redmine #14539 review j#91577 finding 2) — a merged disposition recorded before the current
    target commit was ever declared says nothing about that commit.
    """

    state: str = EXEMPTION_NONE
    journal: str = ""
    allowed_paths: Tuple[str, ...] = ()
    reason: str = ""
    covered_commit: str = ""
    uncovered: Tuple[str, ...] = ()
    covered_scope_journal: str = ""

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
            covered_commit=str(self.covered_commit or "").strip(),
            uncovered=tuple(str(p).strip() for p in self.uncovered if str(p).strip()),
            covered_scope_journal=str(self.covered_scope_journal or "").strip(),
        )

    def as_payload(self) -> dict[str, object]:
        v = self.validated()
        return {
            "state": v.state,
            "journal": v.journal,
            "allowed_paths": list(v.allowed_paths),
            "reason": v.reason,
            "covered_commit": v.covered_commit,
            "uncovered": list(v.uncovered),
            "covered_scope_journal": v.covered_scope_journal,
        }


def _journal_exemption(notes: str) -> Optional[ReviewExemptionFacts]:
    """The exemption one journal declares, or ``None`` if it is not a gate journal (pure).

    Structural qualification (heading or ``gate=codex_direct_edit`` marker) happens BEFORE any
    field is read, so a stray ``follow_up_review:`` line in an unrelated note never contributes.
    A qualifying journal then either satisfies every required field — yielding
    :data:`EXEMPTION_EXEMPT` / :data:`EXEMPTION_REVIEW_REQUIRED` — or folds to
    :data:`EXEMPTION_INVALID`.

    Every governed field is read with the exactly-one rule (:func:`_unique_field`): a record
    declaring the SAME field twice with DIFFERENT values is not uniquely interpretable and folds
    to :data:`EXEMPTION_INVALID` before any value is judged. Reading the first occurrence let a
    gate carrying both ``follow_up_review: false`` and ``follow_up_review: true`` — or a second,
    narrower ``allowed_paths`` — still fold to an exemption (Redmine #14539 review j#91577
    finding 3).
    """
    text = notes or ""
    # DECLARATION is raw: a journal naming this gate qualifies however its marker parses, so a
    # malformed newer gate still SHADOWS an older valid one (the supersede-by-existing rule this
    # module has run on since R2-F1). Readability is the separate question below.
    declared_by_marker = declares_gate(text, MARKER_GATE_CODEX_DIRECT_EDIT)
    qualifies = _HEADING_RE.search(text) is not None or declared_by_marker
    if not qualifies:
        return None

    # …and a journal that NAMES this gate in a marker must carry a marker the canonical producer
    # could actually render (Redmine #14539 review j#92106 finding 1). The allowlist entry that
    # called this "structural qualification only" was wrong on the effect chain: this qualification
    # decides whether the fields below are read as authority, and a valid read MINTS an exemption
    # that reaches the glance projection and the terminal retire admission. So an unreadable marker
    # qualifies the journal (it shadows) and yields EXEMPTION_INVALID (review still owed) rather
    # than an exemption.
    #
    # A heading does NOT rescue it (review j#92174 finding 2). Restricting this to marker-only
    # journals conflated two different questions: a heading is enough to DECLARE the gate, but it
    # cannot make a same-gate marker readable, and a note carrying one is a note whose evidence
    # this reader cannot count. Honouring the heading and ignoring the marker beside it is the
    # readable-subset behaviour ``strict_gate_markers`` refuses one layer down, so the note is
    # fail-closed here too — it still shadows, it just does not mint.
    if declared_by_marker and not strict_gate_markers(text, MARKER_GATE_CODEX_DIRECT_EDIT):
        return ReviewExemptionFacts(state=EXEMPTION_INVALID)

    role, role_conflict = _unique_field(_ROLE_FIELD_RE, text)
    direct_edit_raw, direct_edit_conflict = _unique_field(_DIRECT_EDIT_FIELD_RE, text)
    allowed_paths, allowed_paths_conflict = _unique_path_field(_ALLOWED_PATHS_FIELD_RE, text)
    reason, reason_conflict = _unique_field(_REASON_FIELD_RE, text)
    follow_up_raw, follow_up_conflict = _unique_field(_FOLLOW_UP_REVIEW_FIELD_RE, text)

    invalid = ReviewExemptionFacts(
        state=EXEMPTION_INVALID, allowed_paths=allowed_paths, reason=reason
    )

    # A field declared twice with conflicting values makes the whole gate ambiguous, so this is
    # checked BEFORE any single value is judged — otherwise the first-occurrence value decides.
    if (
        role_conflict
        or direct_edit_conflict
        or allowed_paths_conflict
        or reason_conflict
        or follow_up_conflict
    ):
        return invalid

    # Every required field of `### Codex Direct Edit Gate`, each fail-closed.
    if role != CANONICAL_DIRECT_EDIT_ROLE:
        return invalid
    if _boolean(direct_edit_raw) is not True:
        return invalid
    if not allowed_paths:
        return invalid
    if not reason:
        return invalid
    follow_up = _boolean(follow_up_raw)
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


def fold_declared_change_scope(
    journals: Sequence[Tuple[object, str]],
    *,
    change_bearing_journals: Sequence[object] = (),
) -> DeclaredChangeScope:
    """The LATEST durable ``(commit, changed_paths)`` declaration across one issue (pure).

    The governed ``implementation_done`` / ``review_request`` journals declare the target commit
    and the paths it changed; this reads the newest journal that DECLARES a change scope and takes
    the commit from that SAME journal, so the two are correlated rather than stitched across
    unrelated records (Redmine #14539 review j#90137 F1).

    A journal that declares changed paths but no commit — or a commit but no paths — yields an
    unproven scope, and an unproven scope can never satisfy a coverage check.

    **A declaration supersedes by EXISTING, not by being valid** (review j#90244 R2-F1; the same
    invariant this module applies to the gate itself, and that
    :func:`...glance_integration_disposition.fold_work_unit` carries from #13490 j#85365 F1). The
    latest DECLARING journal wins, and only THEN is its content judged — a newer empty or
    commit-less declaration shadows an older proven scope instead of being skipped so the stale
    one stays "latest".

    **What counts as declaring a change scope** (review j#90289 R3-F1). Either:

    1. the journal carries a ``changed_paths`` field — whatever it contains; or
    2. the journal is a CHANGE-BEARING gate (its id is in ``change_bearing_journals``, i.e. its
       recognized gate is ``implementation_done`` / ``review_request``) AND it declares a commit.

    Rule 2 is what R3 was missing. Keying only on the field's presence meant a NEW
    ``## Gate: Implementation Done`` naming a new target commit but omitting ``changed_paths``
    altogether declared nothing, so the previous commit's scope stayed authoritative and the gate
    kept being checked against it. Announcing a new target commit as an implementation result IS
    a change-scope declaration; if it does not say which paths changed, that scope is unproven.

    ``change_bearing_journals`` is supplied by the caller that owns gate recognition
    (:func:`...glance_journal_grammar.fold_issue_gate_facts`) rather than re-derived here, so the
    gate vocabulary stays in ONE place — the drift this codebase keeps paying for otherwise
    (#13952). Its default is empty, which leaves only rule 1 — the total, fail-open-to-strictness
    behaviour for a direct caller that cannot classify gates.

    Non-change-bearing journals (``close`` / an integration disposition / an ordinary note) still
    declare nothing and leave the scope standing: **absence is not a declaration**. Otherwise a
    Close gate — which also carries a commit — would erase the scope of the work it closes.

    Boundary: this reads what the durable record DECLARES. It cannot re-derive the true changed
    set (that needs git, which this pure domain has no access to). The preset already requires
    the record to be replayable; what this adds is that a gate must be checked against the
    declared scope instead of being trusted on the strength of a non-empty field.
    """
    change_bearing = {
        str(j).strip() for j in (change_bearing_journals or ()) if str(j).strip()
    }
    latest: Optional[Tuple[int, DeclaredChangeScope]] = None
    for journal_id, notes in journals or ():
        jint = _int_journal(journal_id)
        if jint is None:
            continue
        text = notes or ""
        # Same exactly-one rule the gate's own fields use (review j#91577 finding 3): a journal
        # naming two different target commits, or declaring two different changed sets, does not
        # uniquely declare either. It still DECLARES — so it supersedes — but as an unproven
        # scope, which is the fail-closed resolution of the ambiguity.
        commits = {m.group("value").strip().lower() for m in _COMMIT_FIELD_RE.finditer(text)}
        # ``next(iter(...))``, never ``.pop()`` — popping empties the set that the ``declares``
        # test below still has to read.
        commit = next(iter(commits)) if len(commits) == 1 else ""
        # A change-bearing journal naming ANY commit announces a new target, so two conflicting
        # ones still declare (and shadow) — as an unproven scope, since neither is the identity.
        declares = _CHANGED_PATHS_FIELD_RE.search(text) is not None or (
            str(jint) in change_bearing and bool(commits)
        )
        if not declares:
            continue  # this journal says nothing about the change scope
        # A declaration IS present, so it supersedes regardless of what it says. A missing /
        # empty path list or a missing commit resolves to an UNPROVEN scope, which shadows an
        # older proven one instead of leaving it standing.
        paths, paths_conflict = _unique_path_field(_CHANGED_PATHS_FIELD_RE, text)
        if paths_conflict:
            paths = ()
        if latest is None or jint > latest[0]:
            latest = (jint, DeclaredChangeScope(commit=commit, paths=paths, journal=str(jint)))
    if latest is None:
        return DeclaredChangeScope()
    return latest[1]


def fold_review_exemption(
    journals: Sequence[Tuple[object, str]],
    *,
    change_bearing_journals: Sequence[object] = (),
) -> ReviewExemptionFacts:
    """The LATEST durable ``codex_direct_edit`` exemption across one issue's journals (pure).

    Latest-wins by journal id, and a gate journal supersedes by EXISTING rather than by being
    valid (see the module docstring): the newest structurally-qualifying journal is authoritative,
    and a malformed newer gate therefore SHADOWS an older valid one instead of being skipped so
    the stale one stays "latest". Returns the :data:`EXEMPTION_NONE` facts when no journal
    declares a gate.

    An otherwise-valid ``follow_up_review: false`` gate is then checked for PATH COVERAGE against
    the durable record's declared change scope (Redmine #14539 review j#90137 F1). The central
    preset's ``close.review_exemption`` requires "対象 commit の全 changed path を覆う有効な gate",
    so a gate whose coverage cannot be shown — nothing declared to check against, or a changed
    path no glob matches — folds to :data:`EXEMPTION_PATH_COVERAGE_UNPROVEN`, not to an exemption.

    ``change_bearing_journals`` is forwarded to :func:`fold_declared_change_scope`; see its
    docstring for what makes a journal declare a change scope (review j#90289 R3-F1).
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
    resolved = latest[1]
    if resolved.state != EXEMPTION_EXEMPT:
        return resolved

    scope = fold_declared_change_scope(
        journals, change_bearing_journals=change_bearing_journals
    )
    if not scope.proven:
        # Nothing to check the gate against. "We could not verify coverage" must never read as
        # "covered": the exemption is withheld until the record declares its change scope.
        return ReviewExemptionFacts(
            state=EXEMPTION_PATH_COVERAGE_UNPROVEN,
            journal=resolved.journal,
            allowed_paths=resolved.allowed_paths,
            reason=resolved.reason,
        )
    missing = uncovered_paths(scope.paths, resolved.allowed_paths)
    if missing:
        return ReviewExemptionFacts(
            state=EXEMPTION_PATH_COVERAGE_UNPROVEN,
            journal=resolved.journal,
            allowed_paths=resolved.allowed_paths,
            reason=resolved.reason,
            covered_commit=scope.commit,
            uncovered=missing,
            covered_scope_journal=scope.journal,
        )
    return ReviewExemptionFacts(
        state=EXEMPTION_EXEMPT,
        journal=resolved.journal,
        allowed_paths=resolved.allowed_paths,
        reason=resolved.reason,
        covered_commit=scope.commit,
        covered_scope_journal=scope.journal,
    )


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
#: The gate's ``allowed_paths`` could not be shown to cover the declared changed paths.
REASON_PATH_COVERAGE_UNPROVEN = "review_exemption_path_coverage_unproven"
#: A valid exemption exists but a NEWER review round supersedes it, so a review is owed again.
REASON_EXEMPTION_SUPERSEDED = "review_exemption_superseded_by_newer_review_round"
#: The issue is not durably closed, so there is no Close evidence to re-verify.
REASON_CLOSE_NOT_RECORDED = "close_not_recorded"
#: No integration disposition means the work reached the integration branch.
REASON_INTEGRATION_NOT_COMPLETE = "integration_not_complete"
#: The Close gate names a DIFFERENT commit than the one the exemption's coverage was proven for
#: (or names none at all), so the three durable facts are not about the same work.
REASON_CLOSE_COMMIT_MISMATCH = "close_commit_is_not_the_covered_commit"
#: The integration disposition predates the change-scope declaration it would have to be about,
#: so it is evidence for an EARLIER commit than the one being retired.
REASON_INTEGRATION_EVIDENCE_STALE = "integration_evidence_predates_the_change_scope"
#: The durable record carries no STRICT (lane-enveloped, head-bearing) integration evidence, or the
#: evidence it carries is malformed / conflicting. The lenient display fold is not authority.
REASON_INTEGRATION_EVIDENCE_NOT_STRICT = "no_strict_integration_evidence"
#: Strict integration evidence exists, but its reviewed SOURCE head is not the commit the
#: exemption's coverage was proven for — it proves the integration of different work.
REASON_INTEGRATION_HEAD_MISMATCH = "integration_source_head_is_not_the_covered_commit"


def evaluate_exemption_integration_admissible(
    exemption: ReviewExemptionFacts,
    *,
    currently_in_force: bool,
    close_recorded: bool,
    integration_complete: bool,
    close_commit: str = "",
    integration_journal: str = "",
    integration_source_head: str = "",
) -> AdmissionResult:
    """Whether an EXEMPT lane may pass the terminal retire's latest-generation fence (pure).

    An exempt lane has no review generation, so :func:`...review_generation
    .evaluate_integration_admissible` can only ever answer ``no_approval_for_latest_generation``.
    Rather than have the coordinator falsely assert ``--latest-generation-admissible`` about a
    review that never happened, the retire re-verifies the three durable facts that actually
    carry the same safety weight, at action time (#14539 acceptance 2/3):

    1. a VALID ``codex_direct_edit`` gate declaring ``follow_up_review: false``, whose
       ``allowed_paths`` cover the declared changed paths, and which is CURRENTLY in force —
       review is not owed *by policy*, not by an operator's say-so;
    2. the issue is durably CLOSED (its own close contract was satisfied upstream — the owner
       close approval a US / standalone issue needs is enforced at close time);
    3. the integration disposition says the work actually reached the integration branch.

    All three are read from the SAME durable record and the SAME folds the glance projection uses,
    so this is a re-verification, not a second grammar. Any missing fact is fail-closed, and an
    ``invalid`` gate or an owner-required follow-up review never reaches this route at all.

    ``currently_in_force`` is the SUPERSESSION-aware fact
    (:attr:`...glance_journal_grammar.GateFacts.review_exempt`) — the same one the glance
    classifier consumes — NOT :attr:`ReviewExemptionFacts.in_force`. Redmine #14539 review j#90137
    F3: reading the bare gate state here let the retire admit a lane whose exemption a NEWER review
    round had already superseded, so the retire and the glance disagreed about the very same
    durable record. One authority, two consumers.

    **The three facts must be about the SAME work** (Redmine #14539 review j#91577 finding 2).
    Conjoining three booleans only says each fact exists somewhere in the record, not that they
    share a subject, so a lane could retire on a Close and a merge that both belong to an EARLIER
    commit while the current target commit was never integrated at all. Two bindings close that:

    - ``close_commit`` — the commit the Close gate declares — must literal-equal
      :attr:`ReviewExemptionFacts.covered_commit`, the commit the path coverage was proven for.
      Literal equality is deliberate: an abbreviated hash is not resolved against a full one here,
      because this is a safety fence and it has no repository to resolve against, so a
      length-mismatched pair fails closed rather than being guessed equal.
    - ``integration_journal`` must be no OLDER than
      :attr:`ReviewExemptionFacts.covered_scope_journal`. A disposition recorded before the
      current target commit was declared cannot be evidence about it.

    An unparseable / absent journal id on either side of that comparison fails closed.

    **The integration evidence must NAME the commit, not merely follow it** (review j#91696
    finding 2). The ordering test above was justified in R5 as "the strongest correlation the
    durable grammar supports, because the governed disposition record carries no commit field".
    That premise was wrong: the same bounded context already ships a STRICT integration-evidence
    grammar (:mod:`...domain.hibernate_evidence_integration`, #14219 T2b) whose lane-enveloped
    marker separates ``head`` — the reviewed SOURCE head — from ``integration_head``, the commit
    that proved integration on the branch. Journal ordering is not a substitute for identity: a
    marker naming a different source head, recorded after the scope, passed the ordering test
    while proving the integration of entirely different work.

    ``integration_source_head`` is therefore the strict evidence's reviewed head, and it must
    literal-equal ``covered_commit`` on the same terms as ``close_commit``. It is ``""`` when the
    caller found no strict evidence, or found it malformed / lane-unbound / conflicting — all of
    which fail closed here, because the lenient display fold is not authority. The ordering test
    is KEPT alongside it: it costs nothing and independently rules out a disposition recorded
    before the work it claims to integrate existed.
    """
    facts = exemption.validated()
    if facts.state == EXEMPTION_NONE:
        return AdmissionResult(False, REASON_NO_EXEMPTION_RECORDED)
    if facts.state == EXEMPTION_INVALID:
        return AdmissionResult(False, REASON_EXEMPTION_INVALID)
    if facts.state == EXEMPTION_PATH_COVERAGE_UNPROVEN:
        return AdmissionResult(False, REASON_PATH_COVERAGE_UNPROVEN)
    if facts.state == EXEMPTION_REVIEW_REQUIRED:
        return AdmissionResult(False, REASON_FOLLOW_UP_REVIEW_REQUIRED)
    if not currently_in_force:
        return AdmissionResult(False, REASON_EXEMPTION_SUPERSEDED)
    if not close_recorded:
        return AdmissionResult(False, REASON_CLOSE_NOT_RECORDED)
    if not integration_complete:
        return AdmissionResult(False, REASON_INTEGRATION_NOT_COMPLETE)

    # The three facts above each exist. Now: are they about the same commit?
    covered = facts.covered_commit.lower()
    if not covered or str(close_commit or "").strip().lower() != covered:
        return AdmissionResult(False, REASON_CLOSE_COMMIT_MISMATCH)
    scope_journal = _int_journal(facts.covered_scope_journal)
    integration_at = _int_journal(integration_journal)
    if scope_journal is None or integration_at is None or integration_at < scope_journal:
        return AdmissionResult(False, REASON_INTEGRATION_EVIDENCE_STALE)

    # …and does the integration evidence NAME that commit? Ordering is not identity.
    source_head = str(integration_source_head or "").strip().lower()
    if not source_head:
        return AdmissionResult(False, REASON_INTEGRATION_EVIDENCE_NOT_STRICT)
    if source_head != covered:
        return AdmissionResult(False, REASON_INTEGRATION_HEAD_MISMATCH)
    return AdmissionResult(True, REASON_OK)


__all__ = (
    "CANONICAL_DIRECT_EDIT_ROLE",
    "EXEMPTION_EXEMPT",
    "EXEMPTION_INVALID",
    "EXEMPTION_NONE",
    "EXEMPTION_PATH_COVERAGE_UNPROVEN",
    "EXEMPTION_REVIEW_REQUIRED",
    "MARKER_GATE_CODEX_DIRECT_EDIT",
    "REASON_CLOSE_COMMIT_MISMATCH",
    "REASON_CLOSE_NOT_RECORDED",
    "REASON_EXEMPTION_INVALID",
    "REASON_EXEMPTION_SUPERSEDED",
    "REASON_FOLLOW_UP_REVIEW_REQUIRED",
    "REASON_INTEGRATION_EVIDENCE_NOT_STRICT",
    "REASON_INTEGRATION_EVIDENCE_STALE",
    "REASON_INTEGRATION_HEAD_MISMATCH",
    "REASON_INTEGRATION_NOT_COMPLETE",
    "REASON_NO_EXEMPTION_RECORDED",
    "REASON_PATH_COVERAGE_UNPROVEN",
    "REVIEW_EXEMPTION_STATES",
    "DeclaredChangeScope",
    "ReviewExemptionFacts",
    "evaluate_exemption_integration_admissible",
    "fold_declared_change_scope",
    "fold_review_exemption",
    "is_canonical_relative_path",
    "uncovered_paths",
)
