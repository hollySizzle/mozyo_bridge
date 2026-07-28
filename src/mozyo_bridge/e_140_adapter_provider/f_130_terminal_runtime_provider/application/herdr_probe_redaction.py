"""Redaction of private filesystem paths out of launcher-probe evidence (Redmine #14258).

The launcher compatibility probe runs a CANDIDATE launcher against a real config document and
puts what it printed into a public refusal. That text is not ours to control, so the close
condition — no private absolute path in public evidence — has to be enforced here, on
arbitrary third-party output, rather than assumed from any format.

Carved out of ``herdr_pane_lifecycle`` (module-health leaf extraction, not an allowlist bump).
Beyond ``os``, ``re`` and ``Path`` it imports exactly one thing: the repository's
absolute-path rule (``domain.absolute_path_rule``) — the root patterns and the
positive-proof predicate. That module belongs to neither consumer, so this surface and
the managed-lane plugin policy (Redmine #14619) cannot disagree about what an absolute
path is. The quoted-run and URL-collapse transformations below are NOT shared: they are
redaction behaviour, not part of the rule.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.absolute_path_rule import (
    ABSOLUTE_ROOT_RE as _canonical_abs_root_re,
    RELATIVE_CONTINUATION_RE as _canonical_relative_continuation_re,
    keeps_absolute_root as _canonical_keeps_absolute_root,
)

#: What a redacted filesystem path is rendered as in a public verdict detail.
REDACTED_PROBE_PATH = "<target config>"

#: Matches an absolute filesystem path anywhere in a message. Applied AFTER the exact scratch
#: paths are substituted, so it is the backstop rather than the primary rule: a parser can
#: print a path this code never chose (a realpath, an ``include`` target), and the issue's
#: close condition forbids a private absolute path in public evidence regardless of who wrote
#: it.
#:
#: Three root shapes, each pinned by a regression rather than asserted in prose (review
#: j#87786 R11: the previous alternative required a *doubled* backslash, so an ordinary
#: drive path with single separators passed straight through while the comment claimed Windows
#: was covered — the same "docstring stronger than the implementation" defect as R1 and R7, in
#: the same subject area): a UNC root, a drive root with either separator, and a POSIX root.
#:
#: A *relative* token is deliberately not matched. It carries no private location, and
#: redacting it would eat the parse reason the detail exists to convey.
#: The three absolute roots. A candidate occurrence of any of these is treated as a private
#: path unless something POSITIVELY proves otherwise (see :func:`_keeps_absolute_root`).
#:
#: The drive alternative requires its letter to stand alone: a drive root IS a single letter,
#: so ``s:/`` inside ``https://`` is not one. That is a rule about the SHAPE of the root, not
#: a safety allowlist — and it costs nothing, because the ``/`` alternative still finds that
#: position and the URL proof is what preserves it.
#: Imported rather than restated: the canonical rule lives in ``absolute_path_rule``,
#: a module owned by neither consumer. Redmine #14619 review j#92194 F1 measured what
#: two copies cost — the second copy silently read ``/etc`` and ``/`` as safe — and
#: j#92241 F3 that sharing only the patterns leaves the predicate duplicated. The
#: patterns are unchanged from the #14258 version this module hardened.
_ABS_ROOT_RE = _canonical_abs_root_re

#: A quoted run, escape-aware: the closing quote is the first UNescaped one. The naive
#: same-quote rule closed at the first escaped quote and left the rest of the path behind
#: (review j#87824 R20), so precision here requires understanding the escape.
_QUOTED_RUNS = (
    re.compile(r"'((?:[^'\\]|\\.)*)'"),
    re.compile(r'"((?:[^"\\]|\\.)*)"'),
)

#: What an external URL is rendered as. A ``scheme://`` token is NOT preserved byte for byte
#: (design consultation j#87837). A URL's path, query and fragment can carry a private path —
#: ``https://host/docs/Users/<name>/private.yaml``, ``?file=/Users/…`` — and nothing structural
#: separates those from an ordinary documentation path, so preserving the token means either
#: modelling its content or accepting a hole. The close condition allows neither, and a URL is
#: not a recovery authority, so the whole token is collapsed into this fixed string.
EXTERNAL_URL_PLACEHOLDER = "<external URL>"

#: Where a URL BEGINS. Deliberately not a rule for where one ends: nothing after a
#: ``scheme://`` on that line is kept, so the token's extent is never a privacy authority
#: (design consultation j#87841). Every candidate end rule failed on some real shape — a
#: whitespace terminator cut ``?file=C:\\Users\\Ada Smith\\private.yaml`` in half and left the
#: tail behind with its root gone (measured), and a "does it contain a slash" test missed that
#: same drive/UNC case entirely. Not needing an end rule removes the whole class.
_URL_OCCURRENCE_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")

#: The only positive proof left: the ``/`` sits INSIDE a token (``relative/path.yaml``, ``down/right``),
#: i.e. the preceding character continues a word. ``:`` deliberately does not qualify —
#: ``config:/Users/…`` is a labelled absolute path, and treating the label as proof of
#: relativity is exactly the fail-open the allowlist had.
_RELATIVE_CONTINUATION_RE = _canonical_relative_continuation_re


def _collapse_url_tokens(line: str) -> str:
    """Replace the first ``scheme://`` onward with the placeholder — unconditionally.

    Not "the URL token": everything from there to the end of the line, whatever it is. Two
    weaker rules were measured and rejected (j#87837 then j#87841). Ending the replacement at
    whitespace splits ``?file=C:\\Users\\Ada Smith\\private.yaml`` at the space and leaves
    ``Smith\\private.yaml`` behind, now unrecognizable to the scanner because its root went
    into the placeholder. Truncating only for path-bearing URLs misses that same case, since a
    drive or UNC path carries no forward slash at all.

    The cost is stated plainly: prose after a URL, and any second URL, are dropped too. A URL is
    not a recovery authority, and the close condition — no private absolute path in public
    evidence — is not negotiable against detail.
    """
    match = _URL_OCCURRENCE_RE.search(line)
    if match is None:
        return line
    return line[: match.start()] + EXTERNAL_URL_PLACEHOLDER


#: The positive-proof predicate is the canonical one, not a second implementation.
#: Review j#92241 F3: sharing only the regex OBJECTS left this rule written twice,
#: which is exactly the drift that produced the original path-detector defect. The
#: rule (and its long review history, Redmine #14258 j#87831 R21 included) now
#: lives in one place and both consumers call it.
_keeps_absolute_root = _canonical_keeps_absolute_root


def _redact_probe_paths(detail: str, *scratch: Path) -> str:
    """Strip filesystem paths out of a parser's message, keeping WHY it failed (#14258 R7).

    The config-parse measurement materializes the target document in a private temporary
    directory, so both parsers name that path in their errors — and the detail is then
    concatenated into a public, operator-facing refusal. Measured (review j#87766): the
    self-parser's message carried the full ``/var/folders/.../mozyo-config-parse-*/config.yaml``
    into the ``target_config_invalid`` error, which the issue's close condition forbids and
    which the surrounding docstring wrongly claimed could not happen.

    Each scratch path is substituted along with its ``realpath`` (on macOS ``/var`` resolves
    to ``/private/var``, so the launcher prints a spelling this process never constructed),
    and any remaining absolute path is replaced by the same placeholder. What survives is the
    part that helps: the error class and the parse reason — ``unknown key 'by_lane_kind'``,
    ``while parsing a flow sequence``.
    """
    text = detail or ""
    spellings = set()
    for path in scratch:
        for candidate in (path, Path(os.path.realpath(path))):
            spellings.add(str(candidate))
    for spelling in sorted(spellings, key=len, reverse=True):
        text = text.replace(spelling, REDACTED_PROBE_PATH)
    # Quoted runs next: an escape-aware closing quote is a PROVEN terminator, so a path
    # containing spaces (or escaped quotes) is replaced precisely and the text after the run
    # survives. Only runs that actually start at an absolute root are touched.
    for pattern in _QUOTED_RUNS:
        text = pattern.sub(
            lambda m: (
                REDACTED_PROBE_PATH
                if _ABS_ROOT_RE.match(m.group(1))
                else m.group(0)
            ),
            text,
        )
    # Then every remaining absolute-root candidate, each judged on its own. Nothing after an
    # unproven root is kept: an unquoted path has no proven terminator, and the close condition
    # forbids a private absolute path in public evidence outright. Text BEFORE the root
    # survives — that is where the error class and the parse reason sit.
    lines = []
    for line in text.splitlines() or [""]:
        # URLs go first and go WHOLE. Replacing them here (rather than proving them safe in the
        # scan below) is what makes the scan guard-free: no surviving token needs protecting
        # from its own later characters, so nothing can exempt a root that follows one.
        line = _collapse_url_tokens(line)
        for match in _ABS_ROOT_RE.finditer(line):
            if _keeps_absolute_root(line, match.start(), match.group(0)):
                continue
            line = line[: match.start()] + REDACTED_PROBE_PATH
            break
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "EXTERNAL_URL_PLACEHOLDER",
    "REDACTED_PROBE_PATH",
    "redact_probe_paths",
]


#: Public name for the module boundary; the underscore spelling stays for in-module callers.
redact_probe_paths = _redact_probe_paths
