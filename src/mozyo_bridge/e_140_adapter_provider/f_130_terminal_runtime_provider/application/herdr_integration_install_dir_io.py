"""Config-dir transaction IO for the opt-in herdr integration-hook installer (#13249).

The installer brackets herdr's own ``integration install`` with a snapshot / backup /
diff / rollback transaction (:mod:`...herdr_integration_install_ops`). This module owns
the filesystem half of that transaction, and it owns one rule:

    **a read of a config dir is only usable together with the proof that it was
    complete, and a write into a config dir is only allowed into the dir it was staged
    against.**

Both halves of that rule are structural rather than advisory. Every producer returns
its data *with* its completeness (:class:`DirScan`, :class:`DirRead`,
:class:`DirBackup`) — there is deliberately no way to obtain a snapshot and forget to
ask whether it was whole — because a file that could not be listed disappears from
every downstream set at once and reads as *absent*, which no per-file check can catch
(Redmine #13249 reviews j#91688 finding 2 / j#91762 finding 1). And
:func:`config_dir_drift`, the target-identity guard, is applied inside
:func:`rollback_dir` itself rather than only at its call sites, because that is where
the bytes are written: restoring a backup into a dir that has since been re-pointed
would push operator content outside home (review j#91762 finding 1).

Credential-shaped files are never read, hashed, copied, diffed, or restored by anything
here (:func:`~...domain.herdr_integration_install.is_credential_shaped`).
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_integration_install import (
    REASON_CONFIG_DIR_MISSING,
    REASON_UNSAFE_CONFIG_PATH,
    DirSnapshot,
    is_credential_shaped,
    is_safe_config_dir,
)


def config_dir_drift(
    config_dir: Path, home: Path, *, expected_real: Optional[str] = None
) -> "Optional[tuple[str, str]]":
    """Re-validate a target config dir **now**; return ``(reason, detail)`` or ``None``.

    The plan gate answers "was this dir present, a directory, and safely inside home
    *when we looked*". That is a statement about the past, and every mutation the
    installer makes happens later — so the same questions are asked again at each
    action time (before reading / backing up, before each herdr invocation, before any
    rollback write). Without that, a dir removed or re-pointed after the gate makes the
    documented ``config_dir_missing`` / ``unsafe_config_path`` guarantees unenforceable
    and can put herdr's write outside home entirely (Redmine #13249 review j#91762
    finding 1).

    ``expected_real`` pins the identity observed at preflight: the dir must still
    resolve to the *same* realpath, not merely to *some* safe path under home.
    """
    display = str(config_dir)
    try:
        present = config_dir.exists()
    except OSError as exc:
        return (
            REASON_UNSAFE_CONFIG_PATH,
            f"config path {display} could not be examined ({exc.__class__.__name__})",
        )
    if not present:
        return (
            REASON_CONFIG_DIR_MISSING,
            f"config dir {display} is no longer present; it existed when the plan "
            f"gate ran, so it changed underneath this operation",
        )
    home_real = os.path.realpath(home)
    config_real = os.path.realpath(config_dir)
    if not os.path.isdir(config_real) or not is_safe_config_dir(
        resolved=config_real, home_resolved=home_real
    ):
        return (
            REASON_UNSAFE_CONFIG_PATH,
            f"config path {display} now resolves to {config_real}, which is outside "
            f"home or is not a directory (symlink / traversal); refusing to touch it",
        )
    if expected_real is not None and config_real != expected_real:
        return (
            REASON_UNSAFE_CONFIG_PATH,
            f"config path {display} now resolves to {config_real} but this operation "
            f"was staged against {expected_real}; the target changed identity",
        )
    return None


@dataclass(frozen=True)
class DirScan:
    """The non-credential files under a dir **plus the proof the listing was complete**.

    ``files`` are the ``(relpath, abspath)`` pairs that were successfully enumerated;
    ``unenumerable`` names every subtree / entry whose ``scandir`` or ``lstat`` failed.
    The two travel together on purpose: an enumeration error makes a file vanish from
    *every* downstream set at once (snapshot and backup alike), so a per-file
    "unreadable" check can never see it — it reads as *absent*, and every completeness
    comparison built from those sets silently agrees (Redmine #13249 review j#91688
    finding 2). Only :attr:`complete` licenses treating the file list as exhaustive.
    """

    files: "tuple[tuple[str, Path], ...]" = ()
    unenumerable: "tuple[str, ...]" = ()

    @property
    def complete(self) -> bool:
        return not self.unenumerable


def scan_dir(root: Path) -> DirScan:
    """Enumerate ``root``'s non-credential regular files, recording listing failures.

    Credential-shaped files and dirs (by any path component) are pruned so they are
    never read. Symlinked files are skipped too — the installer only tracks real hook
    files, and following a symlink out of the dir would read arbitrary content.

    Every way the listing can come up short is captured rather than swallowed:
    ``os.walk``'s ``onerror`` collects a subtree whose ``scandir`` failed (without it
    ``os.walk`` drops the subtree *silently*), and the entry type is taken from an
    explicit :func:`os.lstat` instead of :meth:`Path.is_file`, which answers ``False``
    for a stat error and would make an un-stattable entry indistinguishable from a
    directory.
    """
    files: "list[tuple[str, Path]]" = []
    failed: "list[str]" = []

    def _relative(target: object) -> str:
        if not isinstance(target, (str, os.PathLike)):
            return "."
        try:
            return os.path.relpath(os.fspath(target), root)
        except (OSError, ValueError):
            return str(target)

    def _onerror(exc: OSError) -> None:
        failed.append(_relative(getattr(exc, "filename", None) or root))

    # A root that is absent or not a directory is NOT "an empty dir, completely read".
    # Every caller here only ever scans a root the gates already required to exist, so
    # its disappearance means the dir drifted under us — the same "could not look"
    # reading as "nothing is there" that j#91688 finding 2 closed for subtrees, which
    # this reports at the root too (Redmine #13249 review j#91762 finding 1).
    try:
        if not root.is_dir():
            return DirScan(unenumerable=(".",))
    except OSError:
        return DirScan(unenumerable=(".",))
    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerror):
        # Prune credential-shaped subdirs so we never descend into them.
        dirnames[:] = [d for d in dirnames if not is_credential_shaped(d)]
        for name in filenames:
            if is_credential_shaped(name):
                continue
            abspath = Path(dirpath) / name
            rel = os.path.relpath(abspath, root)
            if any(is_credential_shaped(part) for part in Path(rel).parts):
                continue
            try:
                mode = os.lstat(abspath).st_mode
            except OSError:
                failed.append(rel)
                continue
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                continue
            files.append((rel, abspath))
    return DirScan(files=tuple(files), unenumerable=tuple(sorted(set(failed))))


#: The snapshot digest for a file that could not be read. It is intentionally NOT a
#: 64-char sha256 hexdigest, so it never collides with a real content hash. Crucially,
#: two of these sentinels comparing *equal* is NOT proof the bytes match (they were
#: never read) — :func:`has_unreadable` and :func:`rollback_dir` treat any snapshot
#: carrying it as unverifiable (Redmine #13249 review j#83674 finding 1).
UNREADABLE_SENTINEL = "\x00unreadable\x00"


@dataclass(frozen=True)
class DirRead:
    """A dir snapshot **inseparable from the proof that the whole dir was read**.

    A bare :class:`DirSnapshot` cannot answer "did I see everything?", so every
    producer here returns this instead — the completeness verdict is part of the type
    rather than something a caller must remember to re-derive afterwards (a post-hoc
    check is exactly what the success path forgot in Redmine #13249 review j#91688
    finding 3). ``unreadable`` names files whose bytes could not be read;
    ``unenumerable`` names subtrees / entries the listing itself could not cover.
    """

    snapshot: DirSnapshot = field(default_factory=DirSnapshot)
    unreadable: "tuple[str, ...]" = ()
    unenumerable: "tuple[str, ...]" = ()

    @property
    def complete(self) -> bool:
        return not (self.unreadable or self.unenumerable)

    @property
    def gap_detail(self) -> str:
        """Bounded, operator-readable description of what could not be read."""
        parts = []
        if self.unenumerable:
            parts.append(f"un-enumerable: {', '.join(self.unenumerable[:5])}")
        if self.unreadable:
            parts.append(f"unreadable: {', '.join(self.unreadable[:5])}")
        return "; ".join(parts) or "none"


def read_dir(root: Path) -> DirRead:
    """Content manifest (relpath -> sha256) of ``root``'s non-credential files.

    An unreadable file is recorded with :data:`UNREADABLE_SENTINEL` (never a real
    hash) so its presence is still detected, but its bytes never enter the snapshot —
    and a snapshot carrying the sentinel can never be used as restoration *proof*.
    The returned :class:`DirRead` also carries the listing failures from
    :func:`scan_dir`, so a caller cannot obtain a snapshot without its completeness.
    """
    scan = scan_dir(root)
    manifest: "dict[str, str]" = {}
    unreadable: "list[str]" = []
    for rel, abspath in scan.files:
        try:
            digest = hashlib.sha256(abspath.read_bytes()).hexdigest()
        except OSError:
            digest = UNREADABLE_SENTINEL
            unreadable.append(rel)
        manifest[rel] = digest
    return DirRead(
        snapshot=DirSnapshot.of(manifest),
        unreadable=tuple(sorted(unreadable)),
        unenumerable=scan.unenumerable,
    )


def has_unreadable(snapshot: DirSnapshot) -> bool:
    """True iff ``snapshot`` carries an unreadable non-credential file.

    Such a file could not be hashed or backed up, so no snapshot equality involving
    it is a byte-level proof — an apply must refuse to start (rollback unprovable) and
    a rollback must never report itself verified.
    """
    return any(digest == UNREADABLE_SENTINEL for _rel, digest in snapshot.entries)


@dataclass(frozen=True)
class DirBackup:
    """The bytes captured for a rollback, **with the proof the capture was complete**.

    ``files`` maps each successfully-read non-credential file to its bytes;
    ``unreadable`` lists files whose read failed and ``unenumerable`` the subtrees the
    listing could not cover. A backup read can fail even when the snapshot read
    succeeded (a transient error, or a file that turned unreadable between the two
    passes), and an incomplete backup means a rollback of that dir could never be
    restored — so the caller MUST refuse the apply before mutating rather than silently
    proceeding with a partial backup (Redmine #13249 review j#83737 finding 1).
    """

    files: "dict[str, bytes]" = field(default_factory=dict)
    unreadable: "tuple[str, ...]" = ()
    unenumerable: "tuple[str, ...]" = ()

    @property
    def complete(self) -> bool:
        return not (self.unreadable or self.unenumerable)

    @property
    def gap_detail(self) -> str:
        parts = []
        if self.unenumerable:
            parts.append(f"un-enumerable: {', '.join(self.unenumerable[:5])}")
        if self.unreadable:
            parts.append(f"unreadable: {', '.join(self.unreadable[:5])}")
        return "; ".join(parts) or "none"


def backup_dir(root: Path) -> DirBackup:
    """Capture the bytes of ``root``'s non-credential files for rollback."""
    scan = scan_dir(root)
    files: "dict[str, bytes]" = {}
    unreadable: "list[str]" = []
    for rel, abspath in scan.files:
        try:
            files[rel] = abspath.read_bytes()
        except OSError:
            unreadable.append(rel)
    return DirBackup(
        files=files,
        unreadable=tuple(sorted(unreadable)),
        unenumerable=scan.unenumerable,
    )


def rollback_dir(
    root: Path,
    backup: "dict[str, bytes]",
    before: DirSnapshot,
    *,
    home: Path,
    expected_real: str,
) -> bool:
    """Restore ``root``'s non-credential files to their pre-apply state, and **verify** it.

    This function is where the installer *writes*, so the target-identity guard lives
    here rather than only at its call sites: if ``root`` no longer resolves to the dir
    this rollback was staged against, restoring the backup would push operator bytes
    into some other location — outside home, in the symlink case. A drifted root is
    therefore reported unrestorable **before any write**, never repaired blindly
    (Redmine #13249 review j#91762 finding 1).

    Removes any non-credential file herdr added (present now, absent in ``before``),
    then rewrites every backed-up file's original bytes (restoring changed / removed
    files). Credential files are never touched (they are absent from both the backup
    and the snapshot). Best-effort per file: a rollback IO error on one file does not
    abort the rest — but the restoration is then **re-read and compared to the
    pre-apply snapshot**, and the boolean it returns is ``True`` only when the dir's
    non-credential content is byte-identical to how it was found. A swallowed
    remove/restore error (a read-only file, a permission loss) that leaves residue
    therefore makes this return ``False`` (Redmine #13249 review j#83613 finding 1),
    so a caller can never claim ``home left as found`` on an unproven rollback.
    """
    if config_dir_drift(root, home, expected_real=expected_real) is not None:
        return False
    before_paths = before.paths
    for rel, abspath in scan_dir(root).files:
        if rel not in before_paths:
            try:
                abspath.unlink()
            except OSError:
                pass
    for rel, data in backup.items():
        target = root / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError:
            pass
    # Prove the restoration: the post-rollback read must match the pre-apply snapshot,
    # cover the whole dir, AND carry no unreadable file — a sentinel that only *equals*
    # another sentinel is not byte proof, and a subtree that could not be listed is not
    # evidence of absence, so either gap reports the dir unverified rather than
    # "restored" (Redmine #13249 reviews j#83674 finding 1 / j#91688 finding 2).
    after = read_dir(root)
    if not after.complete or has_unreadable(before):
        return False
    return after.snapshot == before


__all__ = (
    "UNREADABLE_SENTINEL",
    "DirBackup",
    "DirRead",
    "DirScan",
    "backup_dir",
    "config_dir_drift",
    "has_unreadable",
    "read_dir",
    "rollback_dir",
    "scan_dir",
)
