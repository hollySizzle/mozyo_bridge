"""Redaction of private filesystem paths out of launcher-probe evidence (Redmine #14258).

The launcher compatibility probe runs a CANDIDATE launcher against a real config document and
puts what it printed into a public refusal. That text is not ours to control, so the close
condition — no private absolute path in public evidence — has to be enforced here, on
arbitrary third-party output, rather than assumed from any format.

Carved out of ``herdr_pane_lifecycle`` (module-health leaf extraction, not an allowlist bump).
It depends only on ``re`` and ``Path``, which is what makes it a leaf.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

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
_ABS_ROOT_RE = re.compile(r"\\\\|(?<![A-Za-z0-9_.\-])[A-Za-z]:[\\/]|/")

#: A quoted run, escape-aware: the closing quote is the first UNescaped one. The naive
#: same-quote rule closed at the first escaped quote and left the rest of the path behind
#: (review j#87824 R20), so precision here requires understanding the escape.
_QUOTED_RUNS = (
    re.compile(r"'((?:[^'\\]|\\.)*)'"),
    re.compile(r'"((?:[^"\\]|\\.)*)"'),
)

#: Positive proof #1: the candidate is the ``//`` of a ``scheme://`` URL. A URL is a
#: documentation pointer, not a private location, so it is preserved — but only when the
#: scheme syntax is actually there, never merely because of what precedes it.
_URL_PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:$")

#: Positive proof #2: the ``/`` sits INSIDE a token (``relative/path.yaml``, ``down/right``),
#: i.e. the preceding character continues a word. ``:`` deliberately does not qualify —
#: ``config:/Users/…`` is a labelled absolute path, and treating the label as proof of
#: relativity is exactly the fail-open the allowlist had.
_RELATIVE_CONTINUATION_RE = re.compile(r"[A-Za-z0-9_.\-]$")


def _keeps_absolute_root(line: str, start: int, root: str) -> bool:
    """True iff this root occurrence is provably NOT a private absolute path (#14258 R20).

    The predicate is deliberately inverted from the earlier design. That one asked "does a
    known-safe character precede the root?" and, for anything it had not enumerated, concluded
    "not a path" — so a colon label, a backtick, a brace or a pipe let a full private path
    through untouched (measured). The candidate launcher's stderr format is not ours to
    control, so an allowlist of shapes can never be the safe side.

    Only two things are preserved, each by positive proof: a ``scheme://`` URL, and a ``/``
    that continues a word and is therefore inside a relative token. A drive or UNC root is
    never exempt — neither can occur inside a relative path.
    """
    if root != "/":
        return False
    before = line[:start]
    if line.startswith("//", start) and _URL_PREFIX_RE.search(before):
        return True
    return bool(_RELATIVE_CONTINUATION_RE.search(before))


def _token_end(line: str, start: int) -> int:
    """The index just past the whitespace-delimited token containing ``start``."""
    end = line.find(" ", start)
    return len(line) if end == -1 else end


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
    # Quoted runs first: an escape-aware closing quote is a PROVEN terminator, so a path
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
    # Then every remaining absolute-root candidate. Nothing after it is kept: an unquoted path
    # has no proven terminator, and the close condition forbids a private absolute path in
    # public evidence outright. Text BEFORE the root survives — that is where the error class
    # and the parse reason sit.
    lines = []
    for line in text.splitlines() or [""]:
        # `guard` skips the remainder of a token already proven safe. A URL contains further
        # slashes (`https://host/path`), and judging each one in isolation would condemn the
        # second — proving the token safe has to protect the whole token, not one character.
        guard = 0
        for match in _ABS_ROOT_RE.finditer(line):
            if match.start() < guard:
                continue
            if _keeps_absolute_root(line, match.start(), match.group(0)):
                guard = _token_end(line, match.start())
                continue
            line = line[: match.start()] + REDACTED_PROBE_PATH
            break
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "REDACTED_PROBE_PATH",
    "redact_probe_paths",
]


#: Public name for the module boundary; the underscore spelling stays for in-module callers.
redact_probe_paths = _redact_probe_paths
