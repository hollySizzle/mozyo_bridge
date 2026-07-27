"""Legacy project skill partial-mirror check / sync service (Redmine #14580).

Observes the tree for the rules in :mod:`..domain.legacy_mirror_contract` and,
in sync mode, replaces each pinned mirror entry. Every judgement about *what is
a violation* and *which recovery converges* lives in the domain module; this
layer only reports what it saw.

Directory descriptors are the I/O authority
-------------------------------------------
Review j#90418 R6-F1 measured that ``lstat`` preflight plus path-based I/O is
not enough: the path is re-resolved on every subsequent call, so swapping the
tree between the audit and the write reached outside the mirror four different
ways — a mirror entry re-pointed at an external file passed as clean because the
content read followed it; an aliased *parent* of either side let a write land
outside while ``O_NOFOLLOW`` on the leaf saw nothing wrong; and re-binding this
run's own temp path to a victim symlink let a path-based ``chmod`` change the
victim's mode and then installed the symlink as a pinned entry.

So the walk opens every component with ``O_DIRECTORY | O_NOFOLLOW`` and keeps
the resulting descriptor. Afterwards **nothing resolves a multi-component path
again**: entries are stat'd, read, created, renamed and unlinked relative to a
bound descriptor, with ``O_NOFOLLOW`` on the leaf. A component swapped after the
walk no longer affects where the I/O lands — the descriptor still refers to the
directory that was validated.

Consequences worth stating, because they are easy to undo by accident:

- content parity reads through the bound descriptors and re-validates on the
  fd with ``fstat``; a plain ``Path.read_bytes()`` would follow a symlink
  installed after rule E ran;
- the staging file is created with ``O_CREAT | O_EXCL | O_NOFOLLOW`` on the
  mirror descriptor, so this run owns it; the mode is set with ``fchmod`` on
  that fd, never ``chmod`` on a path that could have been re-bound;
- the swap is ``os.replace`` with ``src_dir_fd`` / ``dst_dir_fd``;
- cleanup unlinks this run's exact name relative to the same descriptor.

Where the host cannot provide these primitives the service fails closed
(:data:`~..domain.legacy_mirror_contract.PLATFORM_UNSUPPORTED`) rather than
degrading to path-based I/O.

Unreadable state is a typed violation, not an exception: a mode-000 canonical
file used to escape the audit as a traceback, which left `release check drift`
advising the operator to follow a disposition that was never printed
(j#90418 R6-F3).

Residue from an interrupted run is a plain unpinned entry: it blocks and asks
for a reviewed disposition. This service never deletes it, because it cannot
distinguish its own crash residue from a file someone meant to keep.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..domain.legacy_mirror_contract import (
    CONTENT_DRIFT,
    ENTRY_MISSING,
    ENTRY_NOT_REGULAR,
    ENTRY_SYMLINK,
    ENTRY_UNREADABLE,
    MIRROR_RELATIVE,
    MIRRORED_REFERENCES,
    PATH_COMPONENT_MISSING,
    PATH_COMPONENT_NOT_DIRECTORY,
    PATH_COMPONENT_SYMLINK,
    PATH_UNREADABLE,
    PLATFORM_UNSUPPORTED,
    RULE_CONTENT_PARITY,
    RULE_DEST_ENTRY_SET,
    RULE_DEST_ENTRY_TYPES,
    RULE_DEST_TOPOLOGY,
    RULE_PLATFORM,
    RULE_SOURCE_ENTRIES,
    RULE_SOURCE_TOPOLOGY,
    SOURCE_MISSING,
    SOURCE_NOT_REGULAR,
    SOURCE_RELATIVE,
    SOURCE_SWAPPED_DURING_SYNC,
    SOURCE_SYMLINK,
    SOURCE_UNREADABLE,
    UNPINNED_ENTRY,
    MirrorAudit,
    Violation,
    describe_name,
)

#: Staging-file prefix. Cosmetic only — it makes residue recognisable to a human
#: reading the blocker. Ownership comes from the exclusive create, never from
#: the name (j#90397 R5-F2).
_TEMP_PREFIX = ".mozyo-legacy-mirror."

#: Points a test may observe to exercise an interleaving deterministically.
HOOK_TEMP_CREATED = "temp_created"

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
#: Leaf reads: no-follow AND non-blocking. `O_NONBLOCK` is what keeps a FIFO
#: swapped in after the type audit from blocking the open itself; the `fstat`
#: on the returned fd then rejects it (j#90450 R7-F2).
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


#: Every platform-dependent primitive this module actually calls, paired with
#: the capability probe for it. Review j#90450 R7-F4: the manifest listed
#: `os.stat`, which nothing here calls, and omitted `os.lstat(dir_fd=)`, which
#: every type decision goes through — so a host without it passed the preflight
#: and then raised `NotImplementedError` straight past the fail-closed path. The
#: manifest is the call surface, not a plausible-looking sample of it.
_REQUIRED_DIR_FD_CALLS: tuple[tuple[str, object], ...] = (
    ("open(dir_fd=)", os.open),
    ("lstat(dir_fd=)", os.lstat),
    ("unlink(dir_fd=)", os.unlink),
    ("mkdir(dir_fd=)", os.mkdir),
    # `os.replace` shares `os.rename`'s implementation; the capability set is
    # keyed on `os.rename` even though both accept the arguments (measured).
    ("rename(src_dir_fd=, dst_dir_fd=)", os.rename),
)


def missing_platform_capabilities() -> tuple[str, ...]:
    """Primitives this service refuses to run without."""
    missing: list[str] = []
    for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"):
        if not hasattr(os, flag):
            missing.append(flag)
    for label, function in _REQUIRED_DIR_FD_CALLS:
        if function not in os.supports_dir_fd:
            missing.append(label)
    if os.scandir not in os.supports_fd:
        missing.append("scandir(fd)")
    return tuple(missing)


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
        #: Seam for deterministic interleaving tests. Production callers leave
        #: it unset; behaviour does not depend on it.
        self._progress_hook = progress_hook

    def _notify(self, event: str) -> None:
        if self._progress_hook is not None:
            self._progress_hook(event)

    # --- bound-descriptor plumbing -----------------------------------------

    def _classify_component(
        self, parent_fd: int, part: str, walked: str, rule: str
    ) -> tuple[Violation | None, bool]:
        """Say *why* a component could not be opened as a real directory.

        The open is the authority; this only produces the message. Errno alone
        cannot tell a symlink from a plain non-directory — on macOS both give
        ENOTDIR under ``O_DIRECTORY | O_NOFOLLOW`` — so the classification uses
        a no-follow ``lstat`` through the same bound parent.
        """
        try:
            info = os.lstat(part, dir_fd=parent_fd)
        except FileNotFoundError:
            return None, True
        except OSError:
            return (
                Violation(rule, PATH_UNREADABLE, walked, "could not be inspected"),
                False,
            )
        if stat.S_ISLNK(info.st_mode):
            return (
                Violation(
                    rule,
                    PATH_COMPONENT_SYMLINK,
                    walked,
                    "path components must be real directories, not symlinks",
                ),
                False,
            )
        if not stat.S_ISDIR(info.st_mode):
            return (
                Violation(rule, PATH_COMPONENT_NOT_DIRECTORY, walked, "exists but is not a directory"),
                False,
            )
        return (
            Violation(rule, PATH_UNREADABLE, walked, "directory could not be opened"),
            False,
        )

    def _open_bound(
        self, relative: str, rule: str, *, create: bool = False
    ) -> tuple[int | None, tuple[Violation, ...], bool]:
        """Open each component no-follow, returning a descriptor for the leaf.

        Returns ``(fd, violations, missing)``. The repo root itself is opened
        without ``O_NOFOLLOW``: it is the anchor the operator invoked us with,
        and a checkout legitimately reached through a symlinked parent was
        accepted as out of scope (j#90378).
        """
        try:
            parent = os.open(self.repo_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return (
                None,
                (Violation(rule, PATH_UNREADABLE, ".", "repository root is not an accessible directory"),),
                False,
            )

        # Ownership of `parent` belongs to this frame until it is handed to the
        # caller. Every exit closes it exactly once: the early returns used to
        # leak it, and `except BaseException` does not fire on `return` — an
        # audit of a tree with no source and no mirror leaked two descriptors
        # per call, 50 over 25 calls (j#90450 R7-F1).
        handed_over = False
        walked = ""
        try:
            for part in relative.split("/"):
                walked = f"{walked}/{part}" if walked else part
                try:
                    child = os.open(part, _DIR_FLAGS, dir_fd=parent)
                except OSError:
                    violation, missing = self._classify_component(parent, part, walked, rule)
                    if missing and create:
                        try:
                            os.mkdir(part, 0o755, dir_fd=parent)
                            child = os.open(part, _DIR_FLAGS, dir_fd=parent)
                        except OSError:
                            return (
                                None,
                                (Violation(rule, PATH_UNREADABLE, walked, "could not be created"),),
                                False,
                            )
                    elif missing:
                        return None, (), True
                    else:
                        assert violation is not None
                        return None, (violation,), False
                os.close(parent)
                parent = child
            handed_over = True
            return parent, (), False
        finally:
            if not handed_over:
                os.close(parent)

    @contextmanager
    def _bound(self, relative: str, rule: str, *, create: bool = False) -> Iterator[
        tuple[int | None, tuple[Violation, ...], bool]
    ]:
        fd, violations, missing = self._open_bound(relative, rule, create=create)
        try:
            yield fd, violations, missing
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _read_bound(dir_fd: int, name: str) -> tuple[bytes | None, str | None]:
        """Read an entry through a bound descriptor, re-validating on the fd.

        Returns ``(payload, failure_kind)``. The ``fstat`` is what makes this
        safe after the type audit: the descriptor cannot be re-pointed, so a
        symlink or FIFO installed in the meantime is refused here.

        ``O_NONBLOCK`` matters as much as ``O_NOFOLLOW``. Validating *after* the
        open is too late for a FIFO: the open itself blocks waiting for a
        writer, so an entry swapped to a FIFO right after the type audit hung
        `check()` indefinitely (j#90450 R7-F2 — a probe was still alive after
        four seconds and had to be killed). ``O_NONBLOCK`` makes the open return
        so the ``fstat`` can reject it; on a regular file it does not change
        read semantics.
        """
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
        except OSError:
            return None, ENTRY_UNREADABLE
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None, ENTRY_NOT_REGULAR
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks), None
        except OSError:
            return None, ENTRY_UNREADABLE
        finally:
            os.close(fd)

    # --- rules --------------------------------------------------------------

    def _audit_source_entries(self, source_fd: int) -> tuple[Violation, ...]:
        found: list[Violation] = []
        for name in MIRRORED_REFERENCES:
            subject = f"{SOURCE_RELATIVE}/{name}"
            try:
                info = os.lstat(name, dir_fd=source_fd)
            except FileNotFoundError:
                found.append(
                    Violation(RULE_SOURCE_ENTRIES, SOURCE_MISSING, subject, "pinned canonical reference is missing")
                )
                continue
            except OSError:
                found.append(
                    Violation(RULE_SOURCE_ENTRIES, SOURCE_UNREADABLE, subject, "could not be inspected")
                )
                continue
            if stat.S_ISLNK(info.st_mode):
                found.append(
                    Violation(
                        RULE_SOURCE_ENTRIES,
                        SOURCE_SYMLINK,
                        subject,
                        "canonical references must be regular files, not symlinks",
                    )
                )
            elif not stat.S_ISREG(info.st_mode):
                found.append(
                    Violation(RULE_SOURCE_ENTRIES, SOURCE_NOT_REGULAR, subject, "is not a regular file")
                )
        return tuple(found)

    def _audit_dest_entries(self, mirror_fd: int) -> tuple[Violation, ...]:
        found: list[Violation] = []
        try:
            names = sorted(entry.name for entry in os.scandir(mirror_fd))
        except OSError:
            return (
                Violation(RULE_DEST_ENTRY_SET, PATH_UNREADABLE, MIRROR_RELATIVE, "directory could not be listed"),
            )

        pinned = set(MIRRORED_REFERENCES)
        for name in names:
            if name not in pinned:
                found.append(
                    Violation(
                        RULE_DEST_ENTRY_SET,
                        UNPINNED_ENTRY,
                        f"{MIRROR_RELATIVE}/{describe_name(name)}",
                        "not in the pinned partial mirror set",
                    )
                )

        for name in MIRRORED_REFERENCES:
            subject = f"{MIRROR_RELATIVE}/{describe_name(name)}"
            try:
                info = os.lstat(name, dir_fd=mirror_fd)
            except FileNotFoundError:
                continue  # rule F reports the absence
            except OSError:
                found.append(Violation(RULE_DEST_ENTRY_TYPES, ENTRY_UNREADABLE, subject, "could not be inspected"))
                continue
            if stat.S_ISLNK(info.st_mode):
                found.append(
                    Violation(
                        RULE_DEST_ENTRY_TYPES,
                        ENTRY_SYMLINK,
                        subject,
                        "mirror references must be regular files, not symlinks",
                    )
                )
            elif not stat.S_ISREG(info.st_mode):
                found.append(
                    Violation(RULE_DEST_ENTRY_TYPES, ENTRY_NOT_REGULAR, subject, "is not a regular file")
                )
        return tuple(found)

    def _audit_content(
        self, source_fd: int, mirror_fd: int, dest_violations: tuple[Violation, ...]
    ) -> tuple[Violation, ...]:
        unusable = {v.subject for v in dest_violations if v.rule == RULE_DEST_ENTRY_TYPES}
        found: list[Violation] = []
        for name in MIRRORED_REFERENCES:
            subject = f"{MIRROR_RELATIVE}/{describe_name(name)}"
            if subject in unusable:
                continue  # rule E already reported it
            try:
                os.lstat(name, dir_fd=mirror_fd)
            except FileNotFoundError:
                found.append(Violation(RULE_CONTENT_PARITY, ENTRY_MISSING, subject, "mirrored reference is absent"))
                continue
            except OSError:
                found.append(Violation(RULE_CONTENT_PARITY, ENTRY_UNREADABLE, subject, "could not be inspected"))
                continue

            source_payload, source_failure = self._read_bound(source_fd, name)
            if source_failure is not None:
                found.append(
                    Violation(
                        RULE_SOURCE_ENTRIES,
                        SOURCE_UNREADABLE if source_failure == ENTRY_UNREADABLE else source_failure,
                        f"{SOURCE_RELATIVE}/{name}",
                        "could not be read as a regular file",
                    )
                )
                continue
            mirror_payload, mirror_failure = self._read_bound(mirror_fd, name)
            if mirror_failure is not None:
                # A type failure discovered HERE is the same defect rule E
                # reports, just found a moment later — so it must carry rule
                # E's weight. Filing it under rule F left it out of the
                # write-blocking set, and the recovery then said "resync" while
                # the sync's own preflight refused the identical tree
                # (j#90450 R7-F2).
                rule = (
                    RULE_DEST_ENTRY_TYPES
                    if mirror_failure == ENTRY_NOT_REGULAR
                    else RULE_CONTENT_PARITY
                )
                found.append(Violation(rule, mirror_failure, subject, "could not be read as a regular file"))
                continue
            if source_payload != mirror_payload:
                found.append(Violation(RULE_CONTENT_PARITY, CONTENT_DRIFT, subject, "differs from canonical"))
        return tuple(found)

    def audit(self) -> MirrorAudit:
        """Evaluate rules A-F over the current tree."""
        missing_capabilities = missing_platform_capabilities()
        if missing_capabilities:
            return MirrorAudit(
                violations=(
                    Violation(
                        RULE_PLATFORM,
                        PLATFORM_UNSUPPORTED,
                        "host",
                        "missing: " + ", ".join(missing_capabilities),
                    ),
                )
            )

        with self._bound(SOURCE_RELATIVE, RULE_SOURCE_TOPOLOGY) as (source_fd, source_topology, source_missing):
            source: tuple[Violation, ...] = source_topology
            if source_missing:
                source = (
                    Violation(
                        RULE_SOURCE_TOPOLOGY,
                        PATH_COMPONENT_MISSING,
                        SOURCE_RELATIVE,
                        "canonical body is not present",
                    ),
                )
            elif source_fd is not None:
                source = self._audit_source_entries(source_fd)

            with self._bound(MIRROR_RELATIVE, RULE_DEST_TOPOLOGY) as (mirror_fd, dest_topology, dest_missing):
                dest: tuple[Violation, ...] = dest_topology
                if mirror_fd is not None:
                    dest = self._audit_dest_entries(mirror_fd)

                skipped: list[str] = []
                content: tuple[Violation, ...] = ()
                if source or dest_missing or source_fd is None or mirror_fd is None:
                    # A/B failed, or one side is absent: parity against a broken
                    # source would report a drift the sync cannot resolve, and the
                    # composite then advertised a resync that refuses (R5-F3).
                    skipped.append(RULE_CONTENT_PARITY)
                else:
                    content = self._audit_content(source_fd, mirror_fd, dest)

                return MirrorAudit(
                    violations=source + dest + content,
                    dest_missing=dest_missing,
                    skipped_rules=tuple(skipped),
                )

    # --- writing -----------------------------------------------------------

    def _replace_one(self, source_fd: int, mirror_fd: int, name: str) -> Violation | None:
        """Copy one pinned reference into place, entirely through bound fds."""
        payload, failure = self._read_bound(source_fd, name)
        if failure is not None:
            return Violation(
                RULE_SOURCE_ENTRIES,
                SOURCE_SWAPPED_DURING_SYNC,
                f"{SOURCE_RELATIVE}/{name}",
                "could not be read as a regular file at write time",
            )

        temp_name = f"{_TEMP_PREFIX}{os.urandom(8).hex()}.tmp"
        try:
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=mirror_fd,
            )
        except OSError:
            return Violation(
                RULE_DEST_ENTRY_SET, PATH_UNREADABLE, MIRROR_RELATIVE, "staging file could not be created"
            )

        owned = temp_name
        try:
            try:
                # A single `os.write` may write fewer bytes than asked; loop
                # until the payload is out (j#90450 R7-F3).
                view = memoryview(payload or b"")
                while view:
                    view = view[os.write(temp_fd, view) :]
                # `fchmod` on our own descriptor: a path-based `chmod` here
                # changed a victim's mode when the temp name was re-bound to a
                # symlink between create and chmod (j#90418 R6-F1 case 4).
                os.fchmod(temp_fd, 0o644)
                created = os.fstat(temp_fd)
            except OSError:
                return Violation(
                    RULE_DEST_ENTRY_SET,
                    ENTRY_UNREADABLE,
                    f"{MIRROR_RELATIVE}/{describe_name(temp_name)}",
                    "staging file could not be written",
                )
            finally:
                os.close(temp_fd)

            self._notify(HOOK_TEMP_CREATED)

            # `os.replace` renames whatever the NAME refers to, and it does not
            # follow symlinks — so a name re-bound between create and swap gets
            # the foreign entry installed as a pinned reference (measured while
            # correcting R6-F1 case 4: `fchmod` on our fd protected the victim's
            # mode, and the symlink still landed in the mirror). Confirm the
            # name still resolves to the inode we created before swapping it in.
            try:
                verify_fd = os.open(temp_name, _FILE_FLAGS, dir_fd=mirror_fd)
            except OSError:
                owned = ""  # not ours any more; do not unlink someone else's entry
                return Violation(
                    RULE_DEST_ENTRY_SET,
                    ENTRY_UNREADABLE,
                    f"{MIRROR_RELATIVE}/{describe_name(temp_name)}",
                    "staging entry was replaced while the sync held it",
                )
            try:
                current = os.fstat(verify_fd)
            except OSError:
                owned = ""
                return Violation(
                    RULE_DEST_ENTRY_SET,
                    ENTRY_UNREADABLE,
                    f"{MIRROR_RELATIVE}/{describe_name(temp_name)}",
                    "staging entry could not be re-validated",
                )
            finally:
                os.close(verify_fd)
            if (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino):
                owned = ""
                return Violation(
                    RULE_DEST_ENTRY_SET,
                    ENTRY_UNREADABLE,
                    f"{MIRROR_RELATIVE}/{describe_name(temp_name)}",
                    "staging entry was rebound while the sync held it",
                )

            try:
                os.replace(temp_name, name, src_dir_fd=mirror_fd, dst_dir_fd=mirror_fd)
            except OSError:
                # e.g. the destination became a directory after the preflight —
                # `IsADirectoryError` used to escape as a traceback through the
                # CLI and the release gate (j#90450 R7-F3). The staging file is
                # still ours, so the `finally` below removes it.
                return Violation(
                    RULE_DEST_ENTRY_TYPES,
                    ENTRY_NOT_REGULAR,
                    f"{MIRROR_RELATIVE}/{describe_name(name)}",
                    "could not be replaced; it is no longer a regular file",
                )
            owned = ""
        finally:
            if owned:
                # Only ever this run's exact name, relative to the bound mirror.
                try:
                    os.unlink(owned, dir_fd=mirror_fd)
                except OSError:
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

        with self._bound(SOURCE_RELATIVE, RULE_SOURCE_TOPOLOGY) as (source_fd, source_violations, source_missing):
            if source_fd is None:
                return 1, (), (
                    "refusing to sync the legacy project skill mirror; nothing was written.",
                    *MirrorAudit(violations=source_violations or ()).report_lines(),
                )
            with self._bound(MIRROR_RELATIVE, RULE_DEST_TOPOLOGY, create=True) as (
                mirror_fd,
                mirror_violations,
                _mirror_missing,
            ):
                if mirror_fd is None:
                    return 1, (), (
                        "refusing to sync the legacy project skill mirror; nothing was written.",
                        *MirrorAudit(violations=mirror_violations or ()).report_lines(),
                    )
                for name in MIRRORED_REFERENCES:
                    swapped = self._replace_one(source_fd, mirror_fd, name)
                    if swapped is not None:
                        return 1, (), (
                            "aborted the legacy project skill mirror sync.",
                            swapped.message(),
                            "",
                            "The tree changed underneath the sync, or the write could not",
                            "complete. Nothing outside the mirror was modified. Re-run once the",
                            "tracked paths are stable.",
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
