"""Old/new filename compatibility resolution (Redmine #11920 / #11921).

Two workspace-local files are being renamed by responsibility
(`vibes/docs/logics/workspace-anchor-project-defaults-migration.md`):

- `.mozyo-bridge/workspace.json`         -> `.mozyo-bridge/workspace-anchor.json`
- `.mozyo-bridge/workspace-defaults.yaml` -> `.mozyo-bridge/project-defaults.yaml`

The rename ships with a compatibility window: the new name is the primary,
the old name stays readable as a fallback, new writes only ever create the
new name, and the *both-exist* state is never silently merged. This module
centralizes the file-presence detection so every reader / writer / diagnostic
applies the same priority and the same both-exist contract.

Two contracts share this resolver:

- **read paths** (session-name resolution, cockpit context, session naming)
  must never die — they prefer the new name, fall back to the old, and degrade
  to "neither" otherwise. The new name is authoritative, so a stray leftover
  old file next to a new one never breaks identity resolution; ``doctor`` is
  where the both-exist drift is surfaced;
- **mutating / explicit commands** (``workspace register``,
  ``workspace-defaults``) read :attr:`CompatResolution.both_exist` and fail
  closed, asking the operator to remove the superseded legacy file rather than
  guessing which copy is authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CompatResolution:
    """Presence of the new vs legacy name for one workspace-local file.

    ``new_path`` / ``old_path`` are the absolute candidate paths; the boolean
    fields record which exist on disk at resolution time.
    """

    new_path: Path
    old_path: Path
    new_exists: bool
    old_exists: bool

    @property
    def both_exist(self) -> bool:
        """Both names present — the ambiguous state callers must fail closed on."""
        return self.new_exists and self.old_exists

    @property
    def neither_exists(self) -> bool:
        return not self.new_exists and not self.old_exists

    @property
    def using_legacy(self) -> bool:
        """Only the legacy name is present (the deprecation-warning state)."""
        return self.old_exists and not self.new_exists

    @property
    def read_path(self) -> Path | None:
        """Path a read should consume: new wins, then legacy, else ``None``.

        The new name wins even when both exist: it is authoritative, so read
        paths stay correct in the presence of a leftover legacy file while
        ``doctor`` / mutating commands surface the both-exist drift separately.
        """
        if self.new_exists:
            return self.new_path
        if self.old_exists:
            return self.old_path
        return None


def resolve_compat_path(
    repo_root: Path, new_relative: Path, old_relative: Path
) -> CompatResolution:
    """Resolve the new/legacy presence for a repo-relative file pair."""
    new_path = repo_root / new_relative
    old_path = repo_root / old_relative
    return CompatResolution(
        new_path=new_path,
        old_path=old_path,
        new_exists=new_path.exists(),
        old_exists=old_path.exists(),
    )
