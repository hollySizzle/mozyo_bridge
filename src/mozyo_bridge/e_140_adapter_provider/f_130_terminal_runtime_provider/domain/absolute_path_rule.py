"""Where an absolute filesystem path begins — the repository's single rule.

Two surfaces in this feature must answer the same question and must not be able
to disagree about it:

- the launcher-probe redaction (Redmine #14258) strips private paths out of
  third-party probe output before it lands in a public refusal;
- the managed-lane plugin policy (Redmine #14619) refuses to construct or emit a
  record carrying one.

This module owns the rule for both. It belongs to **neither** of them, which is
the point: the previous arrangement had the generic #14258 redaction importing
#14619's plugin-identity module, so the older and more general surface depended
on the newer and more specific one. Coordinator ruling j#92243 permits converging
the two consumers only onto "one **neutral** authority with accurate dependency
docs", and a home named for the rule rather than for either consumer is what
makes that true.

The history is worth keeping, because the cost of getting it wrong is measured
rather than hypothetical:

- Redmine #14258 hardened this rule across twenty-one review rounds. Its shape —
  "every root occurrence is a path unless positively proven otherwise" — is
  inverted on purpose. An allowlist of shapes that "look safe" fails open on the
  first format nobody enumerated, and it did.
- Redmine #14619 then wrote a *second* rule ("a ``/``, a segment, another ``/``")
  rather than reusing this one. That copy read ``/etc``, ``/``, ``/秘密`` and
  ``/tmp-☃/secret`` as safe, and because the plugin policy's source check, its
  sink guard and its test oracle all shared that copy, the "two independent
  layers" failed together (review j#92194 F1).
- Sharing only the *patterns* and re-implementing the predicate left the same
  drift one level down (review j#92241 F3), so the predicate lives here too.

Pure: no IO, no dependencies beyond ``re``.
"""

from __future__ import annotations

import re

#: Where an absolute filesystem path can BEGIN: a UNC root, a drive root, or a
#: POSIX ``/``. Every occurrence is a candidate; :func:`keeps_absolute_root`
#: decides whether a particular one is proven not to be a path.
#:
#: The drive alternative requires its letter to stand alone, because a drive root
#: IS a single letter — so the ``s:/`` inside ``https://`` is not one. That is a
#: rule about the SHAPE of the root, not a safety allowlist, and it costs nothing:
#: the ``/`` alternative still finds that position.
ABSOLUTE_ROOT_RE = re.compile(r"\\\\|(?<![A-Za-z0-9_.\-])[A-Za-z]:[\\/]|/")

#: The only positive proof that a root occurrence is not an absolute path: the
#: ``/`` sits INSIDE a token (``relative/path.yaml``, ``github:owner/repo@sha``),
#: i.e. the preceding character continues a word. ``:`` deliberately does not
#: qualify — ``config:/Users/…`` is a labelled absolute path, and treating the
#: label as proof of relativity is exactly the fail-open the original allowlist
#: had (Redmine #14258 review j#87831 R21).
RELATIVE_CONTINUATION_RE = re.compile(r"[A-Za-z0-9_.\-]$")


def keeps_absolute_root(line: str, start: int, root: str) -> bool:
    """True iff this root occurrence is provably NOT an absolute path.

    Deliberately inverted: the question is not "does this look like a path?" but
    "is there positive proof that it is not one?". Exactly one thing proves it — a
    ``/`` that continues a word. A drive or UNC root is never exempt; neither can
    occur inside a relative path.

    The proof is judged per occurrence and covers nothing beyond itself. An
    earlier version let one proof exempt the rest of the surrounding token, which
    silently exempted every later root in it (Redmine #14258 review j#87831 R21).

    ``line`` must be a single line. ``$`` also matches just before a trailing
    newline, so on multi-line text the character ending the *previous* line would
    satisfy the proof and a path after a line break would read as safe — measured
    in Redmine #14619 review round 5. Callers split first; see
    :func:`contains_absolute_path`.
    """
    if root != "/":
        return False
    return bool(RELATIVE_CONTINUATION_RE.search(line[:start]))


def contains_absolute_path(text: str) -> bool:
    """Whether ``text`` carries an absolute filesystem path occurrence.

    Catches a single-component path (``/etc``), the root itself (``/``), a
    non-ASCII path (``/秘密``), a mixed-alphabet path (``/tmp-☃/secret``), a
    labelled path (``config:/Users/x``), a drive root and a UNC root. Does *not*
    flag a relative token (``relative/path.yaml``) or an identity spelling whose
    ``/`` continues a word (``github:owner/repo@sha``, ``install/enable``).

    A bare ``/`` used as prose punctuation (``HOME / XDG_CONFIG_HOME``) *is*
    flagged. Prose is rewritten to suit the boundary; the boundary is not widened
    to suit prose.

    Evaluated line by line — see :func:`keeps_absolute_root` for why that is part
    of the rule rather than an implementation detail.
    """
    return any(
        not keeps_absolute_root(line, match.start(), match.group(0))
        for line in text.splitlines() or [text]
        for match in ABSOLUTE_ROOT_RE.finditer(line)
    )


__all__ = (
    "ABSOLUTE_ROOT_RE",
    "RELATIVE_CONTINUATION_RE",
    "contains_absolute_path",
    "keeps_absolute_root",
)
