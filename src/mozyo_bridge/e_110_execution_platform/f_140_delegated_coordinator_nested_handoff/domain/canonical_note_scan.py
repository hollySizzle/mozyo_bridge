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
- **B. blockquote** — ``>`` as the first non-space character, nesting (``> >``) included.
- **C. indented code** — four or more spaces (or a tab) of indent, Markdown's other verbatim block.
- **D. inline code** — a backtick span inside an otherwise canonical line.

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

An **unmatched** backtick string is refused rather than ignored: the rest of the line is blanked.
CommonMark renders it as literal text, but a line whose quoting is unbalanced is exactly the line
whose authorship this scan cannot establish, and this is the recoverable direction (below).

Two properties of the scan are load-bearing rather than incidental:

- **Quoted regions are blanked, not deleted.** The line structure survives, so a marker can never be
  spliced together out of fragments that sat on either side of a quotation.
- **The scan is per line.** The marker body grammar is ``[^\\]]*``, which spans newlines, so scanning
  the blanked note as one string would let an unclosed ``[mozyo:`` on one line close on a ``]``
  further down and parse as a marker that no single line contains.

The cost is that a canonical marker must be written at top level and on one line: a marker indented
four spaces under a list bullet, split across lines, or sharing its line with an unbalanced backtick
run, is refused. Both canonical producers
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
#: A blockquote line: `>` is the first non-space character (nesting is still a leading `>`).
_BLOCKQUOTE = re.compile(r"^ {0,3}>")
#: An indented code block line: four or more spaces (or a tab) of indent.
_INDENTED_CODE = re.compile(r"^(?: {4}|\t)")
#: A backtick string: a MAXIMAL run of backticks. Code-span delimiters are matched by run length.
_BACKTICK_RUN = re.compile(r"`+")
#: Only whitespace — what a fence closer is allowed to carry after its run.
_ONLY_SPACE = re.compile(r"^\s*$")


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


def _blank_inline_code(line: str) -> str:
    """``line`` with every inline code span blanked, preserving column positions (pure).

    A span runs from a backtick string to the next backtick string of EXACTLY the same length
    (CommonMark 0.31.2 §6.1); runs of other lengths in between are span content. A backtick string
    with no match refuses the rest of the line rather than being ignored — see the module docstring.
    """
    runs = list(_BACKTICK_RUN.finditer(line))
    if not runs:
        return line
    chars = list(line)
    index = 0
    while index < len(runs):
        opener = runs[index]
        width = opener.end() - opener.start()
        closer = next(
            (n for n in range(index + 1, len(runs)) if runs[n].end() - runs[n].start() == width),
            None,
        )
        if closer is None:  # unmatched backtick string: refuse to the end of the line
            start, end, index = opener.start(), len(line), len(runs)
        else:
            start, end, index = opener.start(), runs[closer].end(), closer + 1
        for position in range(start, end):
            chars[position] = " "
    return "".join(chars)


def canonical_note_lines(notes: str) -> tuple[str, ...]:
    """The note's lines with every QUOTED region blanked, in order (pure).

    The authority for "which text is the writer's own voice". Rules A–D are applied uniformly to
    every line (see the module docstring); a quoted line becomes ``""`` rather than disappearing, so
    the caller can scan line by line and a marker can never be spliced across a quotation.

    The open fence is carried as ``(char, length)`` rather than a boolean, because only a matching
    delimiter ends a quotation — every line until then is content, whatever it looks like.
    """
    canonical: list[str] = []
    fence: "tuple[str, int] | None" = None
    for line in str(notes or "").split("\n"):
        if fence is not None:  # A: inside a fence, only its own closer gets out
            if _closes_fence(line, *fence):
                fence = None
            canonical.append("")
            continue
        opener = _fence_opener(line)
        if opener is not None:  # A: the opener line is part of the quotation too
            fence = opener
            canonical.append("")
            continue
        if _BLOCKQUOTE.match(line) or _INDENTED_CODE.match(line):  # B / C
            canonical.append("")
            continue
        canonical.append(_blank_inline_code(line))  # D
    return tuple(canonical)


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
