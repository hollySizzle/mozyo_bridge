"""The one quote-aware canonical marker scan every journal-note reader shares (Redmine #14585).

A structured ``[mozyo:<channel>:k=v:...]`` marker is how an agent records a durable decision in a
Redmine journal. A journal that *quotes* that grammar — a review discussing the contract, a callback
record echoing the landing marker it observed, a spec excerpt pasted into a note — is **not** that
agent recording a decision. Reading the two the same way is what let a quoted marker become gate
authority on a fresh lane (#14577 j#90416 F1).

That distinction was implemented once, inside the proxy rail's own reader (#14546
``coordinator_proxy_send.canonical_note_text``), while its sibling — the
:mod:`...domain.redmine_journal_source` reader that ``workflow watch`` / callback discovery / the
``workflow step`` anchor gate all go through — kept scanning the raw note. Two readers of the same
grammar with two different notions of "quoted" is a drift generator, so the rule set now lives here,
stated once, and both readers call it:

- **A. fenced code** — from a ```` ``` ```` / ``~~~`` opener to its closer, fences included. An
  unclosed fence swallows the rest of the note (a half-open quotation is still a quotation).
- **B. blockquote** — ``>`` as the first non-space character, nesting (``> >``) included, **and the
  paragraph that lazily continues out of one**: a line under ``> quoted`` with no blank line between
  them is still inside the blockquote (CommonMark 0.31.2 §5.1).
- **C. indented code** — four or more **columns** of indent, Markdown's other verbatim block.
- **D. inline code** — a backtick span, which may open on one line of a paragraph and close on
  another (§6.1 normalizes a span's line endings to spaces).

None of B, C or D is a property of a single line, which is why the scan decides block structure
first and applies D per paragraph afterwards. Every version of this module that asked a line about
itself leaked the shape it did not ask about: indentation counted in characters reads ``" \\t"`` as
two columns when Markdown reads four; a blockquote recognized only by its ``>`` releases the very
next line; a span closed at the line end releases everything between its delimiters (#14584 j#91194
F1–F3).

Rules A and D are about *delimiters*, and a delimiter is only a delimiter when it matches. Treating
a fence as a boolean toggle, or a code span as "the text between any two backticks", reads Markdown
that the renderer never produced: a ```` ``` ```` line inside a ```` ```` ```` block is content, not
a closer, and ``` `` ``` opens a span that a single backtick cannot close. Every such mismatch hands
a *verbatim* region back to the scan as the writer's own voice, which is the whole defect this
module exists to prevent (#14584 j#91152 F1). So the delimiter identity is carried, per CommonMark
0.31.2 §4.5 / §6.1:

- a fence closer must be the **same character** as its opener and **at least as long**, with nothing
  but whitespace after it — an "closer" bearing an info string is content;
- a backtick fence opener's info string may **not** contain a backtick (a ```` ```a`b ```` line is a
  paragraph, not an opener), which is what keeps a later real fence from being read as its closer;
- a code span runs from a backtick string to the **next backtick string of exactly that length**;
  runs of any other length in between are content.

An **unmatched** backtick string is refused rather than ignored: the rest of its paragraph is
blanked. CommonMark renders it as literal text, but a paragraph whose quoting is unbalanced is
exactly the text whose authorship this scan cannot establish, and this is the recoverable direction
(below).

Two properties of the scan are load-bearing rather than incidental:

- **Quoted regions are blanked, not deleted.** The line structure survives, so a marker can never be
  spliced together out of fragments that sat on either side of a quotation.
- **The MARKER scan is per line.** The marker body grammar is ``[^\\]]*``, which spans newlines, so
  scanning the blanked note as one string would let an unclosed ``[mozyo:`` on one line close on a
  ``]`` further down and parse as a marker that no single line contains. (Rule D joins a paragraph's
  lines to find span delimiters, but it only ever *blanks*; it never hands a joined string to
  :func:`canonical_marker_fields`, which still reads one line at a time.)

The cost is that a canonical marker must be written at top level and on one line: a marker indented
four columns under a list bullet, split across lines, lazily continuing a blockquote, or sharing its
paragraph with an unbalanced backtick run, is refused. Both canonical producers
(:func:`...redmine_journal_source.render_gate_note` /
:func:`...redmine_journal_source.render_dispatch_note`) already render at column 0 on one line, and
that direction of failure is recoverable — the writer re-records at column 0. The other direction,
handing authority to a quotation, is not.
"""

from __future__ import annotations

import re

#: The handoff marker channel (:func:`...domain.handoff.build_marker`). Its ``kind`` field carries
#: the gate; the source anchor is ``issue`` / ``journal``.
MARKER_CHANNEL_HANDOFF = "handoff"
#: The dedicated workflow-event channel a gate / dispatch journal embeds to carry the full
#: structured event. Its gate field is ``gate`` (``kind`` is also accepted as an alias).
MARKER_CHANNEL_WORKFLOW_EVENT = "workflow-event"

#: The channels a canonical scan recognizes. An unrecognized channel is dropped here so no reader
#: has to know the channel set.
RECOGNIZED_CHANNELS = frozenset({MARKER_CHANNEL_HANDOFF, MARKER_CHANNEL_WORKFLOW_EVENT})

#: ``[mozyo:<channel>:<body>]`` — the body is the ':'-separated key=value field list.
MARKER_RE = re.compile(r"\[mozyo:(?P<channel>[a-z0-9_-]+):(?P<body>[^\]]*)\]")

#: A fence line: a run of ``` or ~~~ after at most three spaces of indent, plus whatever follows it.
#: Whether the line actually OPENS or CLOSES a fence depends on the run's character and length, so
#: both are captured rather than matched away (see :func:`_fence_opener` / :func:`_closes_fence`).
_CODE_FENCE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<rest>.*)$")
#: A backtick string: a MAXIMAL run of backticks. Code-span delimiters are matched by run length.
_BACKTICK_RUN = re.compile(r"`+")
#: Only whitespace — what a fence closer is allowed to carry after its run, and what a blank line is.
_ONLY_SPACE = re.compile(r"^\s*$")
#: Markdown's tab stop. Indentation is measured in COLUMNS, not characters (CommonMark 0.31.2 §2.2).
_TAB_STOP = 4
#: The blocks that can interrupt a paragraph, so a blockquote's paragraph cannot lazily continue into
#: them. Fences and blockquotes interrupt too, but are recognized before this is consulted.
_INTERRUPTS_PARAGRAPH = (
    re.compile(r"^ {0,3}#{1,6}(?:\s|$)"),  # ATX heading
    re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"),  # thematic break
    re.compile(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])(?:\s|$)"),  # list item
)


def _indent_columns(line: str) -> int:
    """The line's leading indentation in COLUMNS, expanding tabs to 4-column stops (pure).

    ``" \\t"`` is four columns, not two characters: Markdown measures block structure in columns, so
    a scan that counts literal spaces reads an indented code block as a top-level paragraph
    (#14584 j#91194 F2).
    """
    column = 0
    for char in line:
        if char == " ":
            column += 1
        elif char == "\t":
            column += _TAB_STOP - (column % _TAB_STOP)
        else:
            break
    return column


def _fence_opener(line: str) -> "tuple[str, int] | None":
    """The ``(delimiter char, run length)`` this line opens a fenced block with, else ``None`` (pure).

    A backtick fence's info string may not contain a backtick (CommonMark 0.31.2 §4.5): without that
    rule ```` ```a`b ```` reads as an opener, and the *real* fence opener on a following line reads
    as its closer — handing the fenced content back as canonical text.
    """
    match = _CODE_FENCE.match(line)
    if match is None:
        return None
    fence = match.group("fence")
    if fence[0] == "`" and "`" in match.group("rest"):
        return None
    return fence[0], len(fence)


def _closes_fence(line: str, char: str, length: int) -> bool:
    """True if ``line`` closes a fence opened by ``length`` × ``char`` (pure).

    The closer must use the same character, be at least as long, and carry nothing but whitespace: a
    shorter run, a run of the other fence character, or a run bearing an info string is CONTENT.
    """
    match = _CODE_FENCE.match(line)
    if match is None:
        return False
    fence = match.group("fence")
    return fence[0] == char and len(fence) >= length and bool(_ONLY_SPACE.match(match.group("rest")))


def _classify_block_structure(lines: "list[str]") -> "list[str]":
    """Each line, or ``""`` where Markdown's BLOCK structure puts it inside a quotation (pure).

    Rules A (fenced code), B (blockquote, including the paragraph that lazily continues out of one)
    and C (indented code) are decided here, because all three are block-level state that one line
    cannot answer on its own. Rule D is inline and is applied afterwards, per paragraph.
    """
    canonical: list[str] = []
    fence: "tuple[str, int] | None" = None
    quoted_paragraph = False
    for line in lines:
        if fence is not None:  # A: inside a fence, only its own closer gets out
            if _closes_fence(line, *fence):
                fence = None
            canonical.append("")
            continue
        if _ONLY_SPACE.match(line):  # a blank line ends every paragraph, lazy or not
            quoted_paragraph = False
            canonical.append("")
            continue
        if _indent_columns(line) >= _TAB_STOP:  # C: measured in columns, so " \t" counts
            canonical.append("")
            continue
        opener = _fence_opener(line)
        if opener is not None:  # A: the opener line is part of the quotation too
            fence, quoted_paragraph = opener, False
            canonical.append("")
            continue
        if line.lstrip(" \t").startswith(">"):  # B: the explicit blockquote marker
            quoted_paragraph = True
            canonical.append("")
            continue
        if quoted_paragraph and not any(p.match(line) for p in _INTERRUPTS_PARAGRAPH):
            canonical.append("")  # B: lazy continuation — still the quoted paragraph (§5.1)
            continue
        quoted_paragraph = False
        canonical.append(line)
    return canonical


def _blank_code_spans(text: str) -> str:
    """``text`` with every inline code span blanked, preserving every character position (pure).

    A span runs from a backtick string to the next backtick string of EXACTLY the same length
    (CommonMark 0.31.2 §6.1); runs of other lengths in between are span content. Newlines survive so
    the caller can split back into lines: a span's line endings are span content, which is why this
    works on a whole paragraph rather than a line (#14584 j#91194 F1).

    A backtick string with no match refuses the rest of the PARAGRAPH rather than being ignored —
    see the module docstring.
    """
    runs = list(_BACKTICK_RUN.finditer(text))
    if not runs:
        return text
    chars = list(text)
    index = 0
    while index < len(runs):
        opener = runs[index]
        width = opener.end() - opener.start()
        closer = next(
            (n for n in range(index + 1, len(runs)) if runs[n].end() - runs[n].start() == width),
            None,
        )
        if closer is None:  # unmatched backtick string: refuse to the end of the paragraph
            start, end, index = opener.start(), len(text), len(runs)
        else:
            start, end, index = opener.start(), runs[closer].end(), closer + 1
        for position in range(start, end):
            if chars[position] != "\n":
                chars[position] = " "
    return "".join(chars)


def canonical_note_lines(notes: str) -> tuple[str, ...]:
    """The note's lines with every QUOTED region blanked, in order (pure).

    The authority for "which text is the writer's own voice". Block structure is decided first
    (:func:`_classify_block_structure`), then rule D is applied to each surviving run of lines —
    the paragraphs — because a code span's delimiters may sit on different lines of one paragraph.

    A quoted line becomes ``""`` rather than disappearing, so the caller can scan line by line and a
    marker can never be spliced across a quotation.
    """
    lines = _classify_block_structure(str(notes or "").split("\n"))
    start = 0
    while start < len(lines):
        if not lines[start]:
            start += 1
            continue
        end = start
        while end < len(lines) and lines[end]:
            end += 1
        lines[start:end] = _blank_code_spans("\n".join(lines[start:end])).split("\n")
        start = end
    return tuple(lines)


def canonical_note_text(notes: str) -> str:
    """:func:`canonical_note_lines` re-joined into one string (pure).

    Kept for readers that want the canonical text itself. A caller that goes on to look for markers
    must NOT scan this as one string — use :func:`canonical_marker_fields`, which scans per line.
    """
    return "\n".join(canonical_note_lines(notes))


def parse_marker_fields(body: str) -> dict[str, str]:
    """Parse a ``key=value:key=value`` marker body into a dict (pure; last write wins)."""
    fields: dict[str, str] = {}
    for token in body.split(":"):
        token = token.strip()
        if not token:
            continue
        key, eq, value = token.partition("=")
        if not eq:
            continue
        fields[key.strip()] = value.strip()
    return fields


def canonical_marker_fields(
    notes: str, *, channels: "frozenset[str] | set[str] | None" = None
) -> tuple[tuple[str, dict[str, str]], ...]:
    """Every CANONICAL ``[mozyo:<channel>:...]`` marker as ``(channel, fields)``, in note order (pure).

    The shared structured-token scan both marker readers are built on: it blanks quoted regions
    (:func:`canonical_note_lines`), scans **per canonical line**, recognizes the token grammar and
    parses the field list — but applies **no** vocabulary policy beyond the channel set, so each
    reader still decides which gate / kind it accepts. A marker that appears only inside a quotation
    yields nothing: it is neither authority nor an ambiguity poison, because it is not a marker at
    all as far as this scan is concerned.

    ``channels`` optionally restricts the result to a SUBSET of :data:`RECOGNIZED_CHANNELS` (the
    channel provenance a caller needs to keep the two channels apart); ``None`` keeps all recognized
    channels. Prose is never inspected; a note with no canonical token yields ``()``.
    """
    if not notes:
        return ()
    found: list[tuple[str, dict[str, str]]] = []
    for line in canonical_note_lines(notes):
        if not line:
            continue
        for match in MARKER_RE.finditer(line):
            channel = match.group("channel")
            if channel not in RECOGNIZED_CHANNELS:
                continue
            if channels is not None and channel not in channels:
                continue
            found.append((channel, parse_marker_fields(match.group("body"))))
    return tuple(found)


__all__ = (
    "MARKER_CHANNEL_HANDOFF",
    "MARKER_CHANNEL_WORKFLOW_EVENT",
    "RECOGNIZED_CHANNELS",
    "MARKER_RE",
    "canonical_note_lines",
    "canonical_note_text",
    "parse_marker_fields",
    "canonical_marker_fields",
)
