"""Legacy project skill partial-mirror check / sync service (Redmine #14580).

Observes the tree for the rules defined in
:mod:`..domain.legacy_mirror_contract` and, in sync mode, replaces each pinned
mirror entry. Every judgement about *what is a violation* and *which recovery
converges* lives in the domain module; this layer only reports what it saw.

Filesystem discipline (design consultation answer, Redmine #14580 j#90402):

- **lossless enumeration** — :func:`os.scandir` entry names are compared
  exactly. Nothing is serialised through a shell word list, so a name
  containing a space, tab, newline or a literal ``*`` cannot be re-split or
  re-globbed (j#90397 R5-F1).
- **no-follow observation** — every type test uses :func:`os.lstat`. ``stat``,
  ``Path.is_file`` and shell ``-e``/``-d``/``-f`` all follow symlinks, which is
  how an aliased destination, an aliased canonical source and a dangling entry
  each passed an earlier gate.
- **owned temp** — the staging file comes from :func:`tempfile.mkstemp` in the
  destination directory, so this run owns an exclusive fd and an exact path. It
  removes that path and nothing else, ever. Prefix matching is not ownership
  (j#90397 R5-F2): it deleted an unrelated file that merely shared the prefix,
  and it deleted a *concurrent* run's in-flight temp.
- **entry replacement** — :func:`os.replace` swaps the directory entry, so a
  hardlinked or otherwise aliased destination inode is never opened or
  truncated (j#90342 R3-F1).
- **action-time recheck** — the source is opened with ``O_NOFOLLOW`` and
  re-validated on the fd, and the whole audit is re-run before success is
  reported, so an alias or type swap between preflight and write is
  fail-closed rather than silently mirrored.

Residue from an interrupted run is a plain unpinned entry: it blocks and asks
for a reviewed disposition. This service never deletes it, because it cannot
distinguish its own crash residue from a file someone meant to keep.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from ..domain.legacy_mirror_contract import (
    CONTENT_DRIFT,
    ENTRY_MISSING,
    ENTRY_NOT_REGULAR,
    ENTRY_SYMLINK,
    MIRROR_RELATIVE,
    MIRRORED_REFERENCES,
    PATH_COMPONENT_MISSING,
    PATH_COMPONENT_NOT_DIRECTORY,
    PATH_COMPONENT_SYMLINK,
    RULE_CONTENT_PARITY,
    RULE_DEST_ENTRY_SET,
    RULE_DEST_ENTRY_TYPES,
    RULE_DEST_TOPOLOGY,
    RULE_SOURCE_ENTRIES,
    RULE_SOURCE_TOPOLOGY,
    SOURCE_MISSING,
    SOURCE_NOT_REGULAR,
    SOURCE_RELATIVE,
    SOURCE_SWAPPED_DURING_SYNC,
    SOURCE_SYMLINK,
    UNPINNED_ENTRY,
    MirrorAudit,
    Violation,
    describe_name,
)

#: Staging-file prefix. Purely cosmetic — it makes residue recognisable to a
#: human reading the blocker. It is NOT used to decide what may be deleted;
#: :func:`tempfile.mkstemp` hands back the exact path this run owns.
_TEMP_PREFIX = ".mozyo-legacy-mirror."

#: Points a test may observe to exercise a race deterministically.
HOOK_TEMP_CREATED = "temp_created"


class LegacyProjectSkillMirrorSync:
    """Check or sync the legacy project skill partial mirror for one repo."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        progress_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.source_dir = self.repo_root / SOURCE_RELATIVE
        self.mirror_dir = self.repo_root / MIRROR_RELATIVE
        #: Seam for deterministic concurrency tests. Production callers leave
        #: it unset; the service's behaviour does not depend on it.
        self._progress_hook = progress_hook

    # --- observation -------------------------------------------------------

    def _notify(self, event: str) -> None:
        if self._progress_hook is not None:
            self._progress_hook(event)

    @staticmethod
    def _lstat(path: Path) -> os.stat_result | None:
        """``lstat`` without following, or ``None`` when nothing is there."""
        try:
            return os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None

    def _walk_components(
        self, relative: str, rule: str
    ) -> tuple[tuple[Violation, ...], bool]:
        """Check each component of a repo-relative path.

        Returns its violations plus whether the walk stopped because a
        component simply does not exist. That distinction is the whole point:
        an existing non-directory component and a missing one need opposite
        recoveries, and collapsing them produced the non-convergent "mirror
        missing, rerun the sync" advice for an ENOTDIR tree (j#90378 R4-F3).
        """
        probe = self.repo_root
        for part in relative.split("/"):
            probe = probe / part
            info = self._lstat(probe)
            display = str(probe.relative_to(self.repo_root))
            if info is None:
                return (), True
            if stat.S_ISLNK(info.st_mode):
                return (
                    Violation(
                        rule=rule,
                        kind=PATH_COMPONENT_SYMLINK,
                        subject=display,
                        note="path components must be real directories, not symlinks",
                    ),
                ), False
            if not stat.S_ISDIR(info.st_mode):
                return (
                    Violation(
                        rule=rule,
                        kind=PATH_COMPONENT_NOT_DIRECTORY,
                        subject=display,
                        note="exists but is not a directory",
                    ),
                ), False
        return (), False

    def _audit_source(self) -> tuple[Violation, ...]:
        violations, missing = self._walk_components(
            SOURCE_RELATIVE, RULE_SOURCE_TOPOLOGY
        )
        if violations:
            return violations
        if missing:
            return (
                Violation(
                    rule=RULE_SOURCE_TOPOLOGY,
                    kind=PATH_COMPONENT_MISSING,
                    subject=SOURCE_RELATIVE,
                    note="canonical body is not present",
                ),
            )

        found: list[Violation] = []
        for name in MIRRORED_REFERENCES:
            info = self._lstat(self.source_dir / name)
            subject = f"{SOURCE_RELATIVE}/{name}"
            if info is None:
                found.append(
                    Violation(
                        rule=RULE_SOURCE_ENTRIES,
                        kind=SOURCE_MISSING,
                        subject=subject,
                        note="pinned canonical reference is missing",
                    )
                )
            elif stat.S_ISLNK(info.st_mode):
                found.append(
                    Violation(
                        rule=RULE_SOURCE_ENTRIES,
                        kind=SOURCE_SYMLINK,
                        subject=subject,
                        note="canonical references must be regular files, not symlinks",
                    )
                )
            elif not stat.S_ISREG(info.st_mode):
                found.append(
                    Violation(
                        rule=RULE_SOURCE_ENTRIES,
                        kind=SOURCE_NOT_REGULAR,
                        subject=subject,
                        note="canonical reference is not a regular file",
                    )
                )
        return tuple(found)

    def _mirror_entry_names(self) -> tuple[str, ...]:
        """Every direct entry name, hidden and odd names included."""
        with os.scandir(self.mirror_dir) as entries:
            return tuple(entry.name for entry in entries)

    def _audit_dest(self) -> tuple[tuple[Violation, ...], bool]:
        topology, missing = self._walk_components(MIRROR_RELATIVE, RULE_DEST_TOPOLOGY)
        if topology:
            return topology, False
        if missing:
            return (), True

        found: list[Violation] = []
        pinned = set(MIRRORED_REFERENCES)
        for name in sorted(self._mirror_entry_names()):
            if name not in pinned:
                found.append(
                    Violation(
                        rule=RULE_DEST_ENTRY_SET,
                        kind=UNPINNED_ENTRY,
                        subject=f"{MIRROR_RELATIVE}/{describe_name(name)}",
                        note="not in the pinned partial mirror set",
                    )
                )

        for name in MIRRORED_REFERENCES:
            info = self._lstat(self.mirror_dir / name)
            if info is None:
                continue  # rule F reports the absence
            subject = f"{MIRROR_RELATIVE}/{describe_name(name)}"
            if stat.S_ISLNK(info.st_mode):
                found.append(
                    Violation(
                        rule=RULE_DEST_ENTRY_TYPES,
                        kind=ENTRY_SYMLINK,
                        subject=subject,
                        note="mirror references must be regular files, not symlinks",
                    )
                )
            elif not stat.S_ISREG(info.st_mode):
                found.append(
                    Violation(
                        rule=RULE_DEST_ENTRY_TYPES,
                        kind=ENTRY_NOT_REGULAR,
                        subject=subject,
                        note="mirror reference is not a regular file",
                    )
                )
        return tuple(found), False

    def _audit_content(self, dest_violations: tuple[Violation, ...]) -> tuple[Violation, ...]:
        unusable = {
            v.subject
            for v in dest_violations
            if v.rule == RULE_DEST_ENTRY_TYPES
        }
        found: list[Violation] = []
        for name in MIRRORED_REFERENCES:
            subject = f"{MIRROR_RELATIVE}/{describe_name(name)}"
            if subject in unusable:
                continue  # reading it would follow the very alias rule E rejected
            mirror_path = self.mirror_dir / name
            if self._lstat(mirror_path) is None:
                found.append(
                    Violation(
                        rule=RULE_CONTENT_PARITY,
                        kind=ENTRY_MISSING,
                        subject=subject,
                        note="mirrored reference is absent",
                    )
                )
                continue
            if (self.source_dir / name).read_bytes() != mirror_path.read_bytes():
                found.append(
                    Violation(
                        rule=RULE_CONTENT_PARITY,
                        kind=CONTENT_DRIFT,
                        subject=subject,
                        note="differs from canonical",
                    )
                )
        return tuple(found)

    def audit(self) -> MirrorAudit:
        """Evaluate rules A-F over the current tree."""
        source = self._audit_source()
        dest, dest_missing = self._audit_dest()

        skipped: list[str] = []
        content: tuple[Violation, ...] = ()
        if source:
            # A/B failed: content parity against a broken source would report a
            # drift the sync cannot resolve, and the composite then advertised a
            # resync that refuses (j#90397 R5-F3).
            skipped.append(RULE_CONTENT_PARITY)
        elif dest_missing:
            skipped.append(RULE_CONTENT_PARITY)
        else:
            content = self._audit_content(dest)

        return MirrorAudit(
            violations=source + dest + content,
            dest_missing=dest_missing,
            skipped_rules=tuple(skipped),
        )

    # --- writing -----------------------------------------------------------

    def _replace_one(self, name: str) -> Violation | None:
        """Copy one pinned reference into place via an owned temp + rename."""
        source_path = self.source_dir / name
        try:
            fd = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            # ELOOP here means the source became a symlink after preflight.
            return Violation(
                rule=RULE_SOURCE_ENTRIES,
                kind=SOURCE_SWAPPED_DURING_SYNC,
                subject=f"{SOURCE_RELATIVE}/{name}",
                note="canonical reference could not be opened as a regular file",
            )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return Violation(
                    rule=RULE_SOURCE_ENTRIES,
                    kind=SOURCE_SWAPPED_DURING_SYNC,
                    subject=f"{SOURCE_RELATIVE}/{name}",
                    note="canonical reference stopped being a regular file",
                )
            with os.fdopen(os.dup(fd), "rb") as handle:
                payload = handle.read()
        finally:
            os.close(fd)

        temp_fd, temp_path = tempfile.mkstemp(
            dir=self.mirror_dir, prefix=_TEMP_PREFIX, suffix=".tmp"
        )
        owned = temp_path
        try:
            with os.fdopen(temp_fd, "wb") as handle:
                handle.write(payload)
            self._notify(HOOK_TEMP_CREATED)
            # `mkstemp` creates 0600; canonical and mirror are tracked 0644.
            os.chmod(temp_path, 0o644)
            os.replace(temp_path, self.mirror_dir / name)
            owned = ""
        finally:
            if owned:
                # Only ever this run's exact path.
                try:
                    os.unlink(owned)
                except FileNotFoundError:
                    pass
        return None

    # --- entry points ------------------------------------------------------

    def check(self) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        """Read-only. Returns ``(exit_code, stdout_lines, stderr_lines)``."""
        result = self.audit()
        if result.ok:
            return 0, (
                "legacy project skill mirror is up to date",
                f"  source: {self.source_dir}",
                f"  destination: {self.mirror_dir}",
            ), ()
        return 1, (), result.report_lines()

    def sync(self) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        """Replace the pinned entries. Writes zero unless A-E all hold."""
        preflight = self.audit()
        if preflight.blocks_write:
            return 1, (), (
                "refusing to sync the legacy project skill mirror; nothing was written.",
                *preflight.report_lines(),
            )

        self.mirror_dir.mkdir(parents=True, exist_ok=True)
        for name in MIRRORED_REFERENCES:
            swapped = self._replace_one(name)
            if swapped is not None:
                return 1, (), (
                    "aborted the legacy project skill mirror sync.",
                    swapped.message(),
                    "",
                    "The canonical body changed underneath the sync. Re-run once the",
                    f"tracked {SOURCE_RELATIVE} path is stable.",
                )

        # Never announce success on an unverified tree: re-audit what we wrote.
        after = self.audit()
        if not after.ok:
            return 1, (), (
                "the legacy project skill mirror did not converge after syncing.",
                *after.report_lines(),
            )

        return 0, (
            "synced legacy project skill mirror",
            f"  source: {self.source_dir}",
            f"  destination: {self.mirror_dir}",
            f"  references: {' '.join(MIRRORED_REFERENCES)}",
            "  SKILL.md adapter stub left untouched (intentional divergence)",
        ), ()
