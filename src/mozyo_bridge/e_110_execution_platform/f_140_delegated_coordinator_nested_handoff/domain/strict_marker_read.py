"""Marker-body strict reading: one grammar decides what a canonical producer could render.

Split out of :mod:`.redmine_journal_source` (Redmine #14687) at the layer seam the
`logic-refactor-split-strategy` doc names. The functions here are not about *Redmine journals*
at all — they are about the ``[mozyo:<channel>:k=v:...]`` **marker body**: how it splits into
components, when those components are something the canonical producer could have rendered, and
which markers count as a given gate's evidence. Their sibling
:mod:`.canonical_note_scan` owns the layer below (the token regex and the QUOTE-AWARE scan that
decides which lines are a note's canonical text at all); this module owns the layer above it.

Keeping the two layers in one module with the Redmine journal source, the gate-marker renderer
and the dispatch-marker family pushed that module past the module-health line after the #14661
integration (1011 > 1000). The answer is this cohesive split at an existing boundary, not an
allowlist entry recording the growth.

**Move-only.** Every definition below is byte-identical to the one it replaced, and
:mod:`.redmine_journal_source` re-exports all of them, so the ~120 modules and tests that import
these names from there are unchanged. The reasoning each docstring carries — the #14539 review
rounds that hardened these readers one refusal at a time — is the contract, and rewording it in a
move commit would silently detach it from the rounds it cites.

The strict readers are the ones every AUTHORITY consumer shares. Two readers of one grammar with
two notions of "renderable" is a drift generator, and that drift is exactly what let a quoted
marker become gate authority (#14585) and what let a repeated key be erased by last-write-wins
before an authority consumer saw it (#14539).
"""

from __future__ import annotations

from typing import Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.canonical_note_scan import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
    MARKER_RE,
    RECOGNIZED_CHANNELS,
    canonical_marker_fields,
    canonical_note_lines,
)


def _parse_marker_components(body: str) -> tuple[tuple[str, str], ...]:
    """Every ``key=value`` component of a marker body, IN ORDER and uncollapsed (pure).

    A fragment carrying no ``=`` is reported with an empty key so a caller checking
    well-formedness can see it, and an EMPTY component is reported the same way rather than being
    skipped. Nothing is dropped and nothing is merged: this is the raw component list, and every
    policy decision about repetition belongs to the caller.

    Redmine #14539 review j#91847 finding 4: the first version of this function said exactly that
    and then dropped empty components, so ``…:lane_generation=1::head=…`` — a body no canonical
    producer can render — read as perfectly well-formed. The central `### Hibernate Evidence Marker
    Contract` requires the opposite: "空 component・``=`` を欠く fragment・空 key・whitespace 混入は
    canonical producer が描画し得ない marker であり fragment を捨てて残りを一致させず marker 全体を
    fail-closed とする".

    Nothing is normalized either: the components are returned exactly as the body splits, WITH
    whatever surrounding whitespace they carry (review j#91896 finding 3). Stripping here erased
    the one piece of evidence that says ``gate = integration_disposition`` is not a marker the
    canonical producer could have rendered, so the whitespace-contaminated body read as clean.
    Deciding what is well-formed belongs to :func:`strict_marker_fields`, not to the scanner.
    """
    components: list[tuple[str, str]] = []
    for token in body.split(":"):
        key, eq, value = token.partition("=")
        if not eq:
            # No ``=`` at all (an empty component included): report it with an empty key so a
            # caller checking well-formedness can see it, and keep the raw text as the value.
            components.append(("", token))
            continue
        components.append((key, value))
    return tuple(components)


#: The two spellings of the ONE logical field naming a marker's gate. Read as a set, never
#: first-non-empty: a second, different gate spelled in the other alias is a second authority
#: claim, not a fallback (Redmine #14539 reviews j#91847 F3 / j#91896 F2).
MARKER_GATE_ALIASES: tuple[str, ...] = ("gate", "kind")


def strict_marker_fields(
    components: Sequence[Tuple[str, str]],
    *,
    canonicalize=None,
) -> "dict[str, str] | None":
    """One marker's fields when its body is canonical-producer-renderable, else ``None`` (pure).

    THE strict reader every authority consumer shares (Redmine #14539 review j#91896 findings 2
    and 3). :func:`marker_fields_in_note` folds a body to a dict with last-write-wins, which is
    fine for display and routing and unusable for authority: a repeated key is erased before the
    consumer sees it, and surrounding whitespace is normalized into a clean-looking field.

    A marker is refused — ``None``, meaning "this marker declares nothing" — when ANY component
    is one the canonical producer could not render:

    - an empty component, or one carrying no ``=``;
    - an empty key;
    - whitespace anywhere around a key or a value.

    The central `### Hibernate Evidence Marker Contract` requires exactly that, and requires it of
    the WHOLE marker: "fragment を捨てて残りを一致させず marker 全体を fail-closed とする".

    A key repeated with the same value collapses to one declaration; repeated with DIFFERENT
    values the marker is refused. ``canonicalize(key, value) -> str`` lets a caller declare which
    keys have a governed vocabulary, so two spellings of one token (``merged`` / ``merge``) count
    as the same declaration; without it values compare literally, which is the fail-closed reading
    for a key with no canonical form.
    """
    fields: dict[str, str] = {}
    seen: dict[str, str] = {}
    for key, value in components or ():
        if not key or key != key.strip() or value != value.strip():
            return None
        canonical = canonicalize(key, value) if canonicalize else value
        if key in seen:
            if seen[key] != canonical:
                return None
            continue
        seen[key] = canonical
        fields[key] = value
    return fields




def strict_marker_body_fields(
    body: str,
    *,
    expected=None,
    canonicalize=None,
) -> "dict[str, str] | None":
    """One CLOSED-VOCABULARY marker body's fields, or ``None`` if unrenderable (pure).

    The shared entry point for readers that own their own channel regex — a recovery-delivery
    authorization, an R19 owner marker — and previously split the captured body themselves
    (Redmine #14539 review j#92174 finding 3). Those private grammars happened to be strict, but
    "happened to be" is the problem: the pin that is supposed to inventory hand-rolled parsers
    could not see them, so a later loosening would have reached an effect with the gate green.

    Stricter than :func:`strict_marker_fields` on the three axes a closed vocabulary allows:

    - a key repeated AT ALL is refused, not just one repeated with a different value — a closed
      field set is rendered once per key, so a second occurrence is already not producer output;
    - with ``expected``, the field set must be EXACTLY that set, so a missing or extra key is
      refused rather than being caught field-by-field downstream;
    - an EMPTY value is refused. The shared reader allows one (the central contract's list of
      producer-impossible bodies does not include it), but every closed-vocabulary producer here
      raises on a blank field, so ``lane_id=`` is not something any of them can render. Review
      j#92327 finding 1 is what surfaced this: routing the recovery channels here in R24 dropped
      their own ``not value`` refusal, which was a LOOSENING carried by a change whose commit
      message called it a tightening.

    Everything the shared reader refuses it refuses too, which is why routing the private parsers
    here TIGHTENS them: they stripped each component before judging it, so ``issue = 14539`` — a
    body the canonical producer cannot render — read as a clean ``issue`` field.
    """
    components = _parse_marker_components(body)
    fields = strict_marker_fields(components, canonicalize=canonicalize)
    if fields is None:
        return None
    if len(fields) != len(components):
        return None
    if any(not value for value in fields.values()):
        return None
    if expected is not None and frozenset(fields) != frozenset(expected):
        return None
    return fields


def strict_marker_fields_in_note(notes: str):
    """Every marker as ``(channel, fields)``, or ``None`` if ANY of them is unreadable (pure).

    The drop-in strict counterpart of :func:`marker_fields_in_note` for readers whose result
    reaches an EFFECT — a send, an actuation, an admission (Redmine #14539 review j#92060).
    Those readers cannot use the lenient fold: it collapses a repeated key by last-write-wins and
    normalizes whitespace, so a body the canonical producer could not render arrives looking clean
    and becomes a dispatch anchor, a recovery key, or a work anchor.

    ``None`` rather than "the readable subset" is deliberate, and is the contract's own wording:
    "fragment を捨てて残りを一致させず marker 全体を fail-closed とする". Dropping the unreadable
    marker and matching on the rest would let a note carrying one clean marker plus one forged one
    read exactly like a clean note — which is how an exact-anchor check gets weakened by a change
    meant to tighten it.
    """
    if not notes:
        return ()
    found = []
    for channel, components in marker_components_in_note(notes):
        fields = strict_marker_fields(components)
        if fields is None:
            return None
        found.append((channel, fields))
    return tuple(found)


def _raw_declares_gate(components, gate: str) -> bool:
    """Whether one marker's RAW components name ``gate`` in either alias (pure)."""
    return any(
        key.strip() in MARKER_GATE_ALIASES and value.strip() == gate
        for key, value in components or ()
    )


def declares_gate(notes: str, gate: str) -> bool:
    """Whether ``notes`` CLAIMS ``gate`` at all, however its marker parses (pure).

    The existence half of latest-wins (Redmine #14539 review j#92012 finding 1). A declaration
    supersedes by EXISTING, not by being readable: a newer journal whose marker is malformed must
    still shadow an older valid one, or strict parsing silently resurrects stale authority. So this
    reads the RAW components and asks only "is this gate named here", where
    :func:`strict_gate_markers` asks the separate question "and does its evidence parse".

    Both aliases count and the workflow-event channel is the only authority channel, matching the
    strict reader — the two must agree about WHICH journal is current even when they disagree about
    whether its evidence is usable.
    """
    for channel, components in marker_components_in_note(notes or ""):
        if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
            continue
        if _raw_declares_gate(components, gate):
            return True
    return False


def strict_gate_markers(notes: str, gate: str, *, canonicalize=None) -> tuple:
    """Every STRICTLY readable workflow-event marker in ``notes`` declaring exactly ``gate`` (pure).

    The one call every Hibernate / terminal authority consumer makes (Redmine #14539 review
    j#91943 finding 1), so "which markers count as this gate's evidence" has a single definition:
    the workflow-event channel only (the handoff channel is a delivery notification), a body the
    canonical producer could render, and a logical gate set that is exactly ``{gate}`` — a marker
    naming two gates proves neither, so it matches nothing.

    ``canonicalize`` is forwarded to :func:`strict_marker_fields` and MUST be the same hook the
    other consumers of that gate pass (review j#92012 finding 3). Dropping it here reintroduced
    the very inconsistency R15 fixed: ``disposition=merged:disposition=merge`` is one declaration
    written twice for the terminal reader and an unreadable body for this one, so a contract-valid
    integration evidence vanished on the Hibernate side alone.
    """
    found = []
    for channel, components in marker_components_in_note(notes or ""):
        if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
            continue
        fields = strict_marker_fields(components, canonicalize=canonicalize)
        if marker_logical_gates(fields) == {gate}:
            found.append(fields)
            continue
        # This marker is not this gate's evidence. If it NAMES this gate anyway, it is a same-gate
        # claim we cannot honour, and the whole note is fail-closed (Redmine #14539 review j#92106
        # finding 3, widened by j#92174 finding 1). Skipping it and returning the readable siblings
        # is the subset behaviour ``strict_marker_fields_in_note`` already refuses: a note carrying
        # one clean and one uncountable marker for the SAME gate would read exactly like a clean
        # note. The contract says it twice — "fragment を捨てて残りを一致させず marker 全体を
        # fail-closed とする" and "同一種別の読めない marker を読み飛ばして別の marker を採ることも
        # しない".
        #
        # "Uncountable" is deliberately wider than "unparseable": a marker whose body parses
        # cleanly but names TWO gates proves neither (ruling #14219 j#86718), so as this gate's
        # evidence it is exactly as unusable as a malformed one. Testing readability first — the
        # earlier shape here — let ``gate=implementation_request:kind=unknown_gate`` parse, fail
        # the ``== {gate}`` check, and be silently skipped, handing authority to its clean sibling.
        # A marker naming some OTHER gate is not this gate's business and is left alone.
        if _raw_declares_gate(components, gate):
            return ()
    return tuple(found)


def marker_logical_gates(fields: "dict[str, str] | None") -> frozenset:
    """Every gate token a marker's fields declare, across both aliases (pure).

    ``frozenset()`` for ``None`` (a refused marker declares nothing). More than one token means
    the marker claims two authority contracts at once, which by ruling #14219 j#86718 proves
    neither — including when the second token is one no contract recognizes, because an
    unrecognized claim is still a claim and must not be silently dropped (review j#91896 F2).
    """
    if not fields:
        return frozenset()
    return frozenset(
        token
        for token in (str(fields.get(alias, "") or "").strip() for alias in MARKER_GATE_ALIASES)
        if token
    )


def marker_fields_in_note(notes: str) -> tuple[tuple[str, dict[str, str]], ...]:
    """Every CANONICAL ``[mozyo:<channel>:...]`` marker as ``(channel, fields)``, in note order (pure).

    A thin name over the shared quote-aware scan (:func:`canonical_marker_fields`): it recognizes
    the token grammar and parses the field list, but applies **no** vocabulary policy — each reader
    decides which channel / kind it accepts.

    "Canonical" is the load-bearing word (Redmine #14585). A marker that appears only inside a
    fenced block, a blockquote, an indented code block, or a backtick span is someone **quoting**
    the grammar — a review discussing the contract, a callback record echoing the landing marker it
    observed — not this agent recording a decision. Such a marker is not returned at all, so it is
    neither authority nor an ambiguity poison. Prose is never inspected; a note with no canonical
    token yields ``()``.
    """
    return canonical_marker_fields(notes)


def marker_components_in_note(
    notes: str,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Every marker as ``(channel, components)`` with its field list UNCOLLAPSED (pure).

    The same scan as :func:`marker_fields_in_note`, except each marker keeps its raw ordered
    ``(key, value)`` components instead of being folded into a dict. Authority readers need this:
    the dict fold is last-write-wins, so a marker that declares the same key twice with different
    values reaches every consumer looking perfectly well-formed, and its meaning silently depends
    on which occurrence came last (Redmine #14539 review j#91797 finding 4). A malformed fragment
    (one carrying no ``=``) is preserved with an empty key rather than dropped, so "this marker
    body is not well-formed" is answerable too.

    Still policy-free: it reports what the token says, and the caller decides what to refuse.

    Scanned over the CANONICAL lines, per line, exactly as :func:`canonical_marker_fields` is
    (#14665 / #14585). A marker that appears only inside a quotation is not a marker here either —
    it is neither authority nor an ambiguity poison — and scanning the note as one string would let
    an unclosed ``[mozyo:`` close on a ``]`` further down. The two readers of this grammar must not
    hold two notions of "quoted": that drift is what let a quoted marker become gate authority.
    """
    if not notes:
        return ()
    found: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for line in canonical_note_lines(notes):
        if not line:
            continue
        for match in MARKER_RE.finditer(line):
            channel = match.group("channel")
            if channel not in RECOGNIZED_CHANNELS:
                continue
            found.append((channel, _parse_marker_components(match.group("body"))))
    return tuple(found)


__all__ = (
    "MARKER_GATE_ALIASES",
    "declares_gate",
    "marker_components_in_note",
    "marker_fields_in_note",
    "marker_logical_gates",
    "strict_gate_markers",
    "strict_marker_body_fields",
    "strict_marker_fields",
    "strict_marker_fields_in_note",
)
