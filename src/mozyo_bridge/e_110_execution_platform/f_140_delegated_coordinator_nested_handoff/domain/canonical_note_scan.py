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
- **E. raw HTML** — from the first unescaped markup start (``<`` + a letter / ``!`` / ``?`` / ``/``)
  left standing by A–D, everything to the END OF THE NOTE. This rule does not tokenize: it has no
  tag set, no nesting depth, no attribute or comment handling. Three rounds tried to model raw HTML
  piece by piece — a tag whitelist, then nesting, then attributes and comments — and each shipped
  with the next token type still open (#14584 j#91406 F3, j#91593 F2/F3). A partial HTML parser
  deciding authority IS the defect, so this asks only whether markup begins, never where it ends.
  A marker inside a comment or an attribute is not even a quotation: it renders as **nothing**, and
  an invisible string was becoming a durable gate event.
- **F. link syntax** — from a destination and title ``](…)``, a reference label ``][…]``, a
  reference definition's tail ``]: …`` or an image's alt text ``![…]``, everything to the end of the
  PARAGRAPH. A marker written in one of them renders as a URL or an attribute, never as prose. Like
  E this does not tokenize: the version that tried closed a definition at the physical line end,
  counted every parenthesis into one depth, and knew nothing of quoted titles or angle-bracket
  destinations — three shortcuts, three separate escapes (#14584 j#91682 F1). §4.7 lets a
  definition's destination and title begin on the NEXT line, so no line-scoped rule can be right.
  The refusal stops at the paragraph rather than the note only because refusing further was
  measured against live journals and cost seven real gate events (``[P1][documented_rule …]`` is
  ordinary review prose). **The scope of a refusal and the way it is implemented are independent —
  wanting a bounded one is not a reason to start parsing.**
- **Backslash escapes (§2.4)** — an escaped delimiter is a literal. Counting an escaped backtick as
  a run pairs it with the REAL opener after it, so the span those two delimiters actually formed
  stops being blanked; ``\\<`` starts no markup.

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

**A pass may not hide what a WIDER pass has not read yet.** E refuses to the end of the note; the
tail refusals (an unmatched backtick run, link syntax) and the hanging-indent blanking refuse only
to the end of a paragraph or a line. Running a narrow one first lets it erase the very ``<code>``
that E would have refused the rest of the note for — reported for the link tail, and equally true of
the other three (#14584 j#91735). So the order is: hide what the renderer hides (A–C, and CLOSED
code spans), **read E**, then hide what this module hides beyond the renderer, then apply E.

**But E must not read a link's own angle brackets as markup either.** Reading it early means the
destinations are still there, and ``[docs](<https://example.com>)`` refused the rest of the note and
erased live gate events (#14584 j#91761). The fix is NOT to mask what looks like a link region: an
attempt at that masked from every *lexical* ``](`` / ``][`` / ``]:`` / ``![``, and where such a
trigger is not a link at all the true hidden region is empty, so any mask was too large and hid a
real ``<script>`` opener (#14584 j#91792). "A smaller approximation only costs an over-blank" holds
only while the approximation is a SUBSET of the real region, and a false trigger has no real region
to be a subset of.

- **G. autolink** — ``<scheme:…>`` and ``<user@host>``, blanked in full. These are **links, not raw
  HTML** (§6.5), so E must not refuse a note for one; and their content is **a URL, not prose**, so a
  marker inside one is no more a declaration than a marker in ``[text](URL)``, which F already
  refuses. Excluding them from E while leaving them visible to the marker scan let
  ``<https://example.test/[mozyo:…]>`` become a gate and an exact work anchor (#14584 j#91839) —
  "not markup" and "not authority" are two decisions, and fixing the first said nothing about the
  second. **Matching the grammar exactly is still not enough**: CommonMark settles blocks before
  inlines (§3), an email local part may hold ``!``, ``?`` and ``-``, and so ``<!--a@b>`` satisfies
  the autolink grammar *and* opens an HTML block when it starts a line — where the block wins.
  Blanking one hid the opener from E and a marker in an unrendered raw-HTML block became a gate
  (#14584 j#91863). A match that could start a block is therefore left for E; the same bytes
  mid-paragraph really are an autolink. **Matching a grammar is not being parsed by it.**

What remains is an over-blank this module accepts on purpose: a tag-shaped **title**
(``[text](url "<code>")``) still starts a refusal. The renderer hides it, but nothing short of
parsing the link proves that, and the direction that cannot be recovered is the other one.

Three properties of the scan are load-bearing rather than incidental:

- **Quoted regions are blanked, not deleted.** The line structure survives, so a marker can never be
  spliced together out of fragments that sat on either side of a quotation.
- **The MARKER scan is per line.** The marker body grammar is ``[^\\]]*``, which spans newlines, so
  scanning the blanked note as one string would let an unclosed ``[mozyo:`` on one line close on a
  ``]`` further down and parse as a marker that no single line contains. (Rule D joins a paragraph's
  lines to find span delimiters, but it only ever *blanks*; it never hands a joined string to
  :func:`canonical_marker_fields`, which still reads one line at a time.)

The cost is that a canonical marker must be written at top level and on one line: a marker indented
four columns under a list bullet, split across lines, lazily continuing a blockquote, sharing its
paragraph with an unbalanced backtick run, or standing anywhere below a line that starts markup, is
refused. Both canonical producers (:func:`...redmine_journal_source.render_gate_note` /
:func:`...redmine_journal_source.render_dispatch_note`) already render at column 0 on one line, and
that direction of failure is recoverable — the writer re-records at column 0, or backticks the tag
they were talking about. The other direction, handing authority to a quotation, is not.
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
    # A list item interrupts a paragraph, but an ORDERED one only when it starts at 1
    # (CommonMark 0.31.2 §5.2). Accepting any number made `prose\n2. …` a new block and cost
    # the gate below it (#14584 j#91954 F2).
    re.compile(r"^ {0,3}(?:[-+*]|1[.)])(?:[ \t]|$)"),  # list item
)

#: Anything that starts raw HTML: a tag, a closing tag, a comment / declaration / CDATA (``<!``) or
#: a processing instruction (``<?``). This module does NOT tokenize what follows — it refuses from
#: here to the end of the note (see :func:`_first_hidden_construct`).
_HIDDEN_CONSTRUCT_START = re.compile(r"<[A-Za-z!?/]")
#: An AUTOLINK, which is a link and not raw HTML at all (CommonMark 0.31.2 §6.5): an absolute URI or
#: an email address in angle brackets. Excluding it from the rule above is renderer-faithful
#: wherever it appears, so it needs no reasoning about surrounding link syntax.
#: The HTML block openers that OVERLAP the autolink grammar (CommonMark 0.31.2 §4.6): type 2 is
#: exactly ``<!--``, type 3 is ``<?``, type 4 is ``<!`` and an ASCII letter. Nothing wider — ``<!``
#: followed by anything else opens no block, and treating it as one erased live gate events.
_HTML_BLOCK_OPENER = re.compile(r"<!--|<\?|<![A-Za-z]")
#: A list marker, which opens a container (CommonMark §5.2). What may follow it is decided in
#: COLUMNS by :func:`_is_block_start_column`, not by counting characters.
_LIST_MARKER = re.compile(r"[-+*]|\d{1,9}[.)]")
_AUTOLINK = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\x00-\x20]*"
    r"|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)>"
)
#: Where Markdown's link syntax starts hiding text: a destination and title ``](…)``, a reference
#: label ``][…]``, a reference definition's tail ``]: …``, and an image's alt text ``![…]``. A marker
#: written in any of them renders as a URL or an attribute — never as prose.
_LINK_HIDDEN_PART = re.compile(r"\]\(|\]\[|\]:|!\[")


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
        if quoted_paragraph and not any(p.match(line) for p in _INTERRUPTS_PARAGRAPH):
            classified.append(("", None, False))  # B: lazy continuation of the quote (§5.1)
            continue
        quoted_paragraph = False
        if paragraph is None or any(p.match(line) for p in _INTERRUPTS_PARAGRAPH):
            counter += 1  # a block that interrupts a paragraph also starts a new one
            paragraph = counter
        classified.append((line, paragraph, False))
    return classified


def _is_escaped(text: str, index: int) -> bool:
    """True if ``text[index]`` is preceded by an ODD number of backslashes (pure, CommonMark §2.4)."""
    backslashes = 0
    while index - backslashes - 1 >= 0 and text[index - backslashes - 1] == "\\":
        backslashes += 1
    return backslashes % 2 == 1


def _first_hidden_construct(line: str) -> int:
    """The offset where a construct with hidden content starts on ``line``, or ``-1`` (pure).

    Deliberately does not say WHICH construct it is, and never asks where it ends. Three rounds of
    modelling raw HTML piece by piece — a tag whitelist, then nesting, then attributes and comments
    — each shipped with the next token type still unhandled (#14584 j#91406 F3, j#91593 F2/F3). A
    partial parser deciding authority is the defect, so this asks only "does one start here" and the
    caller refuses everything from here to the end of the note.

    Markdown's link syntax hides content too, and is refused the same way — see
    :func:`_blank_from_link_syntax`. The only difference is how far the refusal runs.

    Autolinks never reach this: :func:`_blank_autolinks` has already removed them, because they are
    links rather than raw HTML (§6.5) AND their URL is not prose. Stating that once, as a blank,
    rather than twice — a skip here and a blank there — is deliberate; this module has been bitten
    repeatedly by one rule living in two places.
    """
    for match in _HIDDEN_CONSTRUCT_START.finditer(line):
        if not _is_escaped(line, match.start()):
            return match.start()
    return -1


def _blank_autolinks(text: str) -> str:
    """``text`` with every autolink blanked, preserving character positions (pure).

    An autolink is a link whose URI is both destination and label (§6.5). Two things follow, and
    both were learned the hard way:

    - it is **not raw HTML**, so rule E must not refuse the rest of the note for it — reading
      ``[docs](<https://example.com>)`` as markup erased live gate events (#14584 j#91761);
    - its content is **a URL, not prose**, so a marker written inside it is no more a declaration
      than one written in ``[text](URL)``, which rule F already refuses. Excluding it from E while
      leaving it visible to the marker scan let ``<https://example.test/[mozyo:…]>`` become a gate
      and an exact work anchor (#14584 j#91839).

    Matching the autolink grammar exactly is NOT enough on its own, because CommonMark settles block
    structure before inlines (§3). An email local part may contain ``!``, ``?`` and ``-``, so
    ``<!--a@b>``, ``<?a@b>`` and ``<!A@b>`` satisfy this grammar *and* start HTML blocks of type
    2/3/4 when they begin a line — and the block wins. Blanking one hid the opener from rule E and
    a marker inside an unrendered raw-HTML block became a gate (#14584 j#91863). Matching a grammar
    is not the same as being parsed by it.

    So a match that could START an html block is left alone; the same bytes mid-paragraph really are
    an autolink and are still blanked.
    """
    chars = list(text)
    for match in _AUTOLINK.finditer(text):
        if _is_escaped(text, match.start()):
            continue
        if _starts_html_block(text, match.start(), first_line_only=True):
            continue
        for index in range(match.start(), match.end()):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _starts_html_block(text: str, offset: int, *, first_line_only: bool = False) -> bool:
    """True if ``text[offset:]`` begins an HTML block rather than an inline construct (pure).

    An HTML block starts at the head of a line after at most three spaces (CommonMark 0.31.2 §4.6).
    Of the types that can open one, only three overlap the autolink grammar at all: 2 (exactly
    ``<!--``), 3 (``<?``) and 4 (``<!`` followed by an ASCII letter). Type 5 needs ``<![CDATA[`` and
    an email local part admits no ``[``; the tag-shaped types need a tag name where the autolink
    grammar needs a scheme colon or an ``@``.

    Testing ``<!`` alone was too wide, and cost real authority: ``<!@b>``, ``<!1@b>`` and ``<!-@b>``
    are ordinary email autolinks, and refusing them erased the gate recorded below (#14584 j#91898).
    This boundary has now been wrong in both directions, so the tests pin real blocks and real
    autolinks side by side rather than one at a time.

    WHERE a block may begin is a second question, and getting the token set right said nothing about
    it. A list marker is a container prefix rather than prose (§3.2 / §5.2), so ``- <!--a@b>`` opens
    the same block ``<!--a@b>`` does; reading "block start" as "physical line head" blanked that
    opener as an autolink and a marker inside an unrendered block became a gate (#14584 j#91918).
    """
    line_start = text.rfind("\n", 0, offset) + 1
    prefix = text[line_start:offset]
    if not _is_block_start_column(prefix):
        return False
    if first_line_only and line_start != 0 and _LIST_MARKER.search(prefix):
        # An HTML block CAN interrupt a paragraph, so a later line is still a block start when its
        # prefix is only indentation. A list marker there is different: had that list been able to
        # interrupt, the line would already have begun a paragraph of its own. It did not, so the
        # marker is prose — `prose\n2. <!--a@b>` is exactly that case (#14584 j#91954 F2).
        return False
    return bool(_HTML_BLOCK_OPENER.match(text, offset))


def _is_block_start_column(prefix: str) -> bool:
    """True if ``prefix`` is only indentation and list markers, so a block may begin after it (pure).

    Measured in COLUMNS, like every other indentation decision in this module. A list marker admits
    one to four columns of whitespace after it (§5.2 rule 1); at five the content is no longer at the
    item's column but an indented code block inside it (§5.2 rule 2 with §4.4), and code is not a
    place where a raw-HTML block opens. Accepting whitespace by the character instead refused
    ``-     <!--a@b>`` — visible code in the rendering — and erased the gate below it (#14584
    j#91938). Four leading columns do the same thing at the top level, which is why the indent
    allowance is three.
    """
    index = column = 0
    while index < len(prefix) and prefix[index] in " \t":
        # The boundary holds after the step, not before it: a tab at column 0, 1 or 2 lands on
        # column 4, and checking beforehand consumed it as indentation anyway (#14584 j#91954 F1).
        step = 1 if prefix[index] == " " else _TAB_STOP - (column % _TAB_STOP)
        if column + step > 3:
            break
        column += step
        index += 1
    while index < len(prefix):
        marker = _LIST_MARKER.match(prefix, index)
        if marker is None:
            return False
        column += marker.end() - marker.start()
        index = marker.end()
        width = 0
        while index < len(prefix) and prefix[index] in " \t":
            step = 1 if prefix[index] == " " else _TAB_STOP - (column % _TAB_STOP)
            width += step
            column += step
            index += 1
        if not 1 <= width <= 4:
            return False
    return True


def _blank_from_link_syntax(text: str) -> str:
    """``text`` blanked from where link syntax starts hiding text, to the end of the paragraph (pure).

    A marker written as ``[text](THIS)``, ``[text][THIS]``, ``[label]: THIS`` or ``![THIS](img)``
    renders as a URL or an attribute — never as prose.

    This does not ask where the region ends, for the same reason :func:`_first_hidden_construct`
    does not. The previous version tried: it closed a reference definition at the physical line end,
    counted every parenthesis into one depth, and knew nothing of quoted titles or angle-bracket
    destinations — and each of those three shortcuts released a marker on its own (#14584 j#91682
    F1). CommonMark 0.31.2 §4.7 lets a definition's destination and title start on the NEXT line and
    the title span several, so no line-scoped rule can be right here.

    The refusal stops at the paragraph rather than the note because refusing further was measured
    against live journals and cost seven real gate events: ``[P1][documented_rule …]`` is ordinary
    review prose. Bounding by paragraph needs no tokenizer — it is the block structure already
    decided above.
    """
    for match in _LINK_HIDDEN_PART.finditer(text):
        if _is_escaped(text, match.start()):
            continue
        chars = list(text)
        for index in range(match.start(), len(text)):
            if chars[index] != "\n":
                chars[index] = " "
        return "".join(chars)
    return text


def _backtick_runs(text: str) -> "list[tuple[int, int]]":
    """The delimiter backtick strings in ``text`` as ``(start, end)`` (pure).

    A backslash-escaped backtick is a literal, not a delimiter (§2.4): counting it would pair a run
    that never opened a span with the real opener after it, releasing the span's content (#14584
    j#91593 F1). Such a run is shortened by its escaped first character, and disappears entirely if
    that was all of it.
    """
    runs = [
        (match.start() + 1, match.end()) if _is_escaped(text, match.start()) else
        (match.start(), match.end())
        for match in _BACKTICK_RUN.finditer(text)
    ]
    return [(start, end) for start, end in runs if end > start]


def _blank_closed_code_spans(text: str) -> str:
    """``text`` with every CLOSED inline code span blanked, character positions preserved (pure).

    A span runs from a backtick string to the next backtick string of EXACTLY the same length
    (CommonMark 0.31.2 §6.1); runs of other lengths in between are span content. Newlines survive so
    the caller can split back into lines: a span's line endings are span content, which is why this
    works on a whole paragraph rather than a line (#14584 j#91194 F1).

    A run with no match is left ALONE here and refused later by :func:`_blank_paragraph_tail`. The
    split matters: this pass hides exactly what the renderer hides, so rule E may be read on its
    output, while the tail pass hides more than the renderer does and must not (#14584 j#91735).
    """
    runs = _backtick_runs(text)
    chars = list(text)
    index = 0
    while index < len(runs):
        opener_start, opener_end = runs[index]
        width = opener_end - opener_start
        closer = next(
            (n for n in range(index + 1, len(runs)) if runs[n][1] - runs[n][0] == width),
            None,
        )
        if closer is None:
            index += 1
            continue
        for position in range(opener_start, runs[closer][1]):
            if chars[position] != "\n":
                chars[position] = " "
        index = closer + 1
    return "".join(chars)


def _blank_paragraph_tail(text: str) -> str:
    """``text`` blanked from the first construct that refuses the rest of the paragraph (pure).

    Two of them, for the same reason: an unmatched backtick string leaves a paragraph whose quoting
    does not balance, and link syntax opens a region whose end this module refuses to compute. Both
    hide MORE than the renderer does, which is why they run after rule E has been read.
    """
    unmatched = [
        start
        for index, (start, end) in enumerate(_backtick_runs(text))
        if not any(
            other[1] - other[0] == end - start for other in _backtick_runs(text)[index + 1:]
        )
    ]
    starts = [match.start() for match in _LINK_HIDDEN_PART.finditer(text)
              if not _is_escaped(text, match.start())]
    candidates = unmatched + starts
    if not candidates:
        return text
    chars = list(text)
    for index in range(min(candidates), len(text)):
        if chars[index] != "\n":
            chars[index] = " "
    return "".join(chars)


def canonical_note_lines(notes: str) -> tuple[str, ...]:
    """The note's lines with every QUOTED region blanked, in order (pure).

    The authority for "which text is the writer's own voice". Block structure is decided first
    (:func:`_classify_block_structure`), then rule D is applied to each surviving run of lines —
    the paragraphs — because a code span's delimiters may sit on different lines of one paragraph.

    A quoted line becomes ``""`` rather than disappearing, so the caller can scan line by line and a
    marker can never be spliced across a quotation.

    **A pass may not hide what a WIDER pass has not read yet.** Rule E refuses to the end of the
    note; the tail passes and the hanging-indent blanking refuse only to the end of a paragraph or a
    line. Running the narrow ones first let them erase the very ``<code>`` that would have refused
    the rest of the note, and markers below it came back as authority (#14584 j#91735 — reported for
    the link tail, and true of the unmatched-backtick tail, the image tail and hanging indent too).
    So E is read on the output of the passes that hide exactly what the renderer hides, and applied
    after the ones that hide more.
    """
    # Line endings are normalized first: Redmine returns CRLF, and the block rules below admit only
    # U+0020 / U+0009 as whitespace, so a stray "\r" would keep every fence closer from closing.
    text = str(notes or "")
    lines_in = _LINE_ENDING.split(text)
    if _BARE_CARRIAGE_RETURN.search(text) is not None:
        return tuple("" for _line in lines_in)  # renderer-dependent structure: refuse the note
    classified = _classify_block_structure(lines_in)
    lines = [text for text, _paragraph, _blanked in classified]
    paragraphs = _paragraph_runs(classified)
    for start, end in paragraphs:  # renderer-faithful: these hide exactly what the renderer hides
        joined = _blank_closed_code_spans("\n".join(lines[start:end]))
        lines[start:end] = _blank_autolinks(joined).split("\n")
    # E is READ here, before anything that hides more than the renderer does.
    cutoff = next(
        (index for index, line in enumerate(lines) if _first_hidden_construct(line) >= 0), None
    )
    for start, end in paragraphs:  # refuses more than the renderer: tails
        lines[start:end] = _blank_paragraph_tail("\n".join(lines[start:end])).split("\n")
    lines = ["" if blanked else line for line, (_t, _p, blanked) in zip(lines, classified)]
    if cutoff is not None:  # E is APPLIED last, on the position it observed for itself
        return tuple(lines[:cutoff] + [""] * (len(lines) - cutoff))
    return tuple(lines)


def _paragraph_runs(classified: "list[tuple[str, int | None, bool]]") -> "list[tuple[int, int]]":
    """The ``(start, end)`` line ranges of each paragraph in ``classified`` (pure)."""
    runs: list[tuple[int, int]] = []
    start = 0
    while start < len(classified):
        paragraph = classified[start][1]
        if paragraph is None:
            start += 1
            continue
        end = start
        while end < len(classified) and classified[end][1] == paragraph:
            end += 1
        runs.append((start, end))
        start = end
    return runs


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
