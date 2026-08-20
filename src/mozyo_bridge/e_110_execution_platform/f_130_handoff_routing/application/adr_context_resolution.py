"""Send-side ADR context resolution from the repo-local ADR directory (#15722).

The pure pointer type
(:mod:`mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.adr_context`)
stays IO-free, the same boundary the role-profile resolver keeps: this seam owns
the single filesystem read. It mirrors
:mod:`...application.role_profile_field_resolution` — a **fixed** repo-relative
path read from the caller-supplied ``repo_root``, never a cwd / worktree path
walk and never a directory search.

Resolution contract:

- ``<repo_root>/vibes/docs/adr/README.md`` is the index anchor. When the ADR
  directory or that index is absent the function returns ``None`` — the explicit
  "no ADR context resolved" fallback. That is what an adopting repo without ADR
  practice gets, so adding this to the handoff cannot break it (Redmine #15722
  AC3); the sender never invents an index that does not exist.
- Every ``adr-NNNN-<slug>.md`` beside the index becomes a ref, ordered by
  filename so a payload is deterministic.
- The status comes from the ADR's own ``- status: <token>`` line and is passed
  through :func:`normalize_adr_status`, so a file with a missing, malformed, or
  unreadable status is carried as ``unknown`` — visible, and never binding. This
  seam has no path that turns an unreadable file into an ``active`` rule.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.adr_context import (
    STATUS_UNKNOWN,
    AdrContextPointer,
    AdrRef,
    make_adr_ref,
    resolvable_paths_for,
)

#: Fixed repo-relative ADR location (``vibes/docs/`` is the project docs
#: namespace declared by the repo router). Resolved against the caller's
#: ``repo_root``; never searched for.
ADR_DIRECTORY = "vibes/docs/adr"
ADR_INDEX_BASENAME = "README.md"

#: ``adr-NNNN-<slug>.md`` — the filename shape ``vibes/docs/adr/README.md``
#: ``## 書式`` fixes. The capture group is the stable ADR id.
ADR_FILENAME_RE = re.compile(r"^(adr-\d{4})-[a-z0-9][a-z0-9-]*\.md$")

#: ``- status: <token>`` as the ADR format prescribes, at the head of the file.
_STATUS_LINE_RE = re.compile(r"^-\s*status\s*:\s*(.+)$", re.MULTILINE)

#: Only the front matter is scanned for the status line, so a body that quotes
#: "- status: active" cannot override the declared header.
_STATUS_SCAN_BYTES = 4096


def _declared_status(path: Path) -> str:
    """The ADR's declared status token, or ``unknown`` when it cannot be read.

    An unreadable / status-less ADR is reported as :data:`STATUS_UNKNOWN` rather
    than dropped or guessed: dropping would hide the ADR from the receiver, and
    guessing would launder it into a binding rule.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:_STATUS_SCAN_BYTES]
    except OSError:
        return STATUS_UNKNOWN
    match = _STATUS_LINE_RE.search(head)
    if match is None:
        return STATUS_UNKNOWN
    return match.group(1)


def resolve_adr_context(repo_root: Path) -> Optional[AdrContextPointer]:
    """Resolve the repo-local ADR pointer set, or ``None`` when there is none.

    ``None`` is the explicit fallback (no ADR directory / no index), not an
    error: the handoff simply carries no ADR context, exactly as it did before
    this mechanism existed.
    """
    adr_dir = Path(repo_root) / ADR_DIRECTORY
    index_path = adr_dir / ADR_INDEX_BASENAME
    if not adr_dir.is_dir() or not index_path.is_file():
        return None

    refs: list[AdrRef] = []
    for entry in sorted(adr_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        match = ADR_FILENAME_RE.match(entry.name)
        if match is None:
            continue
        refs.append(
            make_adr_ref(
                match.group(1),
                f"{ADR_DIRECTORY}/{entry.name}",
                _declared_status(entry),
            )
        )

    index_canonical = f"{ADR_DIRECTORY}/{ADR_INDEX_BASENAME}"
    return AdrContextPointer(
        index_canonical_path=index_canonical,
        index_resolvable_paths=resolvable_paths_for(index_canonical),
        refs=tuple(refs),
    )


__all__ = (
    "ADR_DIRECTORY",
    "ADR_INDEX_BASENAME",
    "ADR_FILENAME_RE",
    "resolve_adr_context",
)
