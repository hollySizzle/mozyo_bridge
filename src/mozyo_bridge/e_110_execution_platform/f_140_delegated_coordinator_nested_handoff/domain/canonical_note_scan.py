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
#: Only Markdown's block whitespace — what a fence closer may carry, and what a blank line is.
#: NOT ``\s``: Python's ``\s`` matches every Unicode space, while CommonMark 0.31.2 §2.1 admits only
#: U+0020 and U+0009 here, so ``\s`` reads ``` ```<U+00A0> ``` as a fence closer and a non-breaking
#: space as a blank line — both of which release the quoted text below them (#14584 j#91406 F1).
_ONLY_SPACE = re.compile(r"^[ \t]*$")
#: A line ending, per CommonMark 0.31.2 §2.1. Redmine returns journal bodies with CRLF, so this is
#: what makes the strict space class above safe: without it every ``\r`` would sit on the end of a
#: fence closer and stop it closing, and the scan would swallow the whole note.
_LINE_ENDING = re.compile(r"\r\n|\r|\n")
#: A carriage return that is NOT part of a CRLF. The spec calls it a line ending; pandoc does not
#: split on it. Both readings have shapes the other refuses, so where they disagree the text's block
#: structure — and therefore its authorship — is renderer-dependent, and the note is refused whole.
_BARE_CARRIAGE_RETURN = re.compile(r"\r(?!\n)")
#: Markdown's tab stop. Indentation is measured in COLUMNS, not characters (CommonMark 0.31.2 §2.2).
_TAB_STOP = 4
#: The blocks that can interrupt a paragraph, so a blockquote's paragraph cannot lazily continue into
#: them. Fences and blockquotes interrupt too, but are recognized before this is consulted. The
#: delimiters take ``[ \t]`` for the same reason as :data:`_ONLY_SPACE`: an interrupter recognized
#: too eagerly ends the quotation early, which releases the line after it.
_INTERRUPTS_PARAGRAPH = (
    re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)"),  # ATX heading
    re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"),  # thematic break
    re.compile(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$)"),  # list item
)

#: Raw-HTML tags that put what follows them inside a verbatim or quoted element. Their block runs to
#: their own closing tag — NOT to the next blank line: an unclosed ``<code>`` leaves every later line
#: inside that element in the rendered document, which is exactly the state a blank line does not
#: leave. This is wider than CommonMark §4.6's type-1 list on purpose; the extra tags are the ones
#: the quotation contract names.
_HTML_QUOTING_TAGS = ("pre", "script", "style", "textarea", "code", "blockquote")
#: An HTML block start: any tag at the head of a line. Recognizing this broadly is deliberate — an
#: unmodelled HTML construct must fall to "quoted", never to "the writer's own voice".
_HTML_BLOCK_START = re.compile(r"^ {0,3}</?[A-Za-z][A-Za-z0-9-]*", re.IGNORECASE)
#: Inline raw HTML that renders its content verbatim or as a quotation. The scan blanks from such an
#: opening tag to its closing tag, and to the end of the paragraph if it never closes.
_HTML_INLINE_OPEN = re.compile(r"<(?P<tag>code|pre|blockquote|script|style|textarea)\b[^>]*>", re.I)


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
    classified: list[tuple[str, "int | None", bool]] = []
    fence: "tuple[str, int] | None" = None
    html_closers: "tuple[str, ...] | None" = None
    quoted_paragraph = False
    paragraph: "int | None" = None
    counter = 0
    for line in lines:
        if fence is not None:  # A: inside a fence, only its own closer gets out
            if _closes_fence(line, *fence):
                fence = None
            classified.append(("", None, False))
            paragraph = None
            continue
        if html_closers is not None:  # E: inside a raw-HTML block
            if html_closers and any(closer in line.lower() for closer in html_closers):
                html_closers = None  # §4.6 type 1: ends on the line carrying its closing tag
            elif not html_closers and _ONLY_SPACE.match(line):
                html_closers = None  # every other type: ends at the first blank line
            classified.append(("", None, False))
            paragraph = None
            continue
        if _ONLY_SPACE.match(line):  # a blank line ends every paragraph, lazy or not
            quoted_paragraph, paragraph = False, None
            classified.append(("", None, False))
            continue
        if _indent_columns(line) >= _TAB_STOP:
            # C: four columns starts an indented code block — but only where a block CAN start. In
            # an open paragraph this is hanging indent, which cannot interrupt it (§4.4), so the
            # line stays in the paragraph: a span's delimiters may be on it, and cutting the
            # paragraph here is what released the marker in between (#14584 j#91406 F2). It is
            # still blanked afterwards, which keeps this module's long-standing refusal of a marker
            # written under a deep indent.
            classified.append((line, paragraph, True) if paragraph is not None else ("", None, False))
            continue
        opener = _fence_opener(line)
        if opener is not None:  # A: the opener line is part of the quotation too
            fence, quoted_paragraph, paragraph = opener, False, None
            classified.append(("", None, False))
            continue
        if line.lstrip(" \t").startswith(">"):  # B: the explicit blockquote marker
            quoted_paragraph, paragraph = True, None
            classified.append(("", None, False))
            continue
        html = _html_block_closers(line)
        if html is not None:  # E: a raw-HTML block starts here
            html_closers, quoted_paragraph, paragraph = html, False, None
            classified.append(("", None, False))
            continue
        if quoted_paragraph and not any(p.match(line) for p in _INTERRUPTS_PARAGRAPH):
            classified.append(("", None, False))  # B: lazy continuation of the quote (§5.1)
            continue
        quoted_paragraph = False
        if paragraph is None or any(p.match(line) for p in _INTERRUPTS_PARAGRAPH):
            counter += 1  # a block that interrupts a paragraph also starts a new one
            paragraph = counter
        classified.append((line, paragraph, False))
    return classified


def _html_block_closers(line: str) -> "tuple[str, ...] | None":
    """The closing tags that end the raw-HTML block ``line`` starts, ``()`` for blank-line-ended,
    ``None`` if it starts none (pure).

    Raw HTML reaches the renderer as markup, so ``<pre>`` / ``<code>`` / ``<blockquote>`` around a
    marker are quotation by exactly the contract this module states — and the scan did not model
    them at all (#14584 j#91406 F3). Any tag at the head of a line is treated as a block start: an
    HTML construct this module does not model must fall to "quoted", never to "the writer's voice".
    """
    if _HTML_BLOCK_START.match(line) is None:
        return None
    name = line.lstrip(" \t").lstrip("<").lstrip("/").split(">")[0].split()[0].lower()
    if name in _HTML_QUOTING_TAGS:
        return (f"</{name}>",)
    return ()


def _blank_inline_html(text: str) -> str:
    """``text`` with every inline raw-HTML verbatim / quotation region blanked (pure).

    From an opening :data:`_HTML_INLINE_OPEN` tag to its closing tag, or to the end of the paragraph
    if it never closes — the same fail-closed rule an unmatched backtick string gets.
    """
    chars = list(text)
    position = 0
    while True:
        opening = _HTML_INLINE_OPEN.search(text, position)
        if opening is None:
            return "".join(chars)
        closing = re.compile(rf"</{opening.group('tag')}[ \t]*>", re.I).search(text, opening.end())
        position = closing.end() if closing is not None else len(text)
        for index in range(opening.start(), position):
            if chars[index] != "\n":
                chars[index] = " "


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
    # Line endings are normalized first: Redmine returns CRLF, and the block rules below admit only
    # U+0020 / U+0009 as whitespace, so a stray "\r" would keep every fence closer from closing.
    text = str(notes or "")
    lines_in = _LINE_ENDING.split(text)
    if _BARE_CARRIAGE_RETURN.search(text) is not None:
        return tuple("" for _line in lines_in)  # renderer-dependent structure: refuse the note
    classified = _classify_block_structure(lines_in)
    lines = [text for text, _paragraph, _blanked in classified]
    start = 0
    while start < len(classified):
        paragraph = classified[start][1]
        if paragraph is None:
            start += 1
            continue
        end = start
        while end < len(classified) and classified[end][1] == paragraph:
            end += 1
        joined = _blank_inline_html("\n".join(lines[start:end]))
        lines[start:end] = _blank_code_spans(joined).split("\n")
        start = end
    return tuple(
        "" if blanked else line
        for line, (_text, _paragraph, blanked) in zip(lines, classified)
    )


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
