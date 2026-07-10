"""Live mount-metadata adapter for onboarding preflight (Redmine #13508).

The pure classifier (:mod:`...domain.path_safety`) consumes a closed
:class:`MountFacts`; this application-layer adapter is where the actual OS probe
happens (allowed here — the domain never runs an ambient OS command). It reads
the mount table (``/proc/self/mountinfo`` on Linux, ``/sbin/mount`` on macOS/BSD),
finds the mount covering the canonical path, and maps its filesystem type to a
closed classification.

It is **fail-closed**: any probe error, an unreadable mount table, or an
unrecognised filesystem type resolves to ``MOUNT_UNAVAILABLE`` (which the
classifier turns into ``ambiguous``), never a silent ``local``/``normal``. Cloud
File-Provider folders (Google Drive / Dropbox / OneDrive under
``~/Library/CloudStorage``) usually surface as an ordinary local volume at the
mount layer; the classifier's path-prefix / provider-name signals cover those,
so this adapter only needs to distinguish local vs network vs undeterminable.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from ..domain.path_safety import (
    MOUNT_LOCAL,
    MOUNT_NETWORK,
    MOUNT_SYNC_CLOUD,
    MOUNT_UNAVAILABLE,
    MountFacts,
)

__all__ = ("LiveMountProbe",)

# Filesystem types that are network/remote mounts.
_NETWORK_FSTYPES: frozenset[str] = frozenset(
    {
        "nfs",
        "nfs4",
        "smbfs",
        "smb",
        "cifs",
        "afpfs",
        "afp",
        "webdav",
        "ftp",
        "fuse.sshfs",
        "sshfs",
        "9p",
        "ncpfs",
    }
)
# Filesystem types that are ordinary local on-disk / in-memory mounts.
_LOCAL_FSTYPES: frozenset[str] = frozenset(
    {
        "apfs",
        "hfs",
        "hfsplus",
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "zfs",
        "tmpfs",
        "ramfs",
        "overlay",
        "overlayfs",
        "vfat",
        "exfat",
        "ntfs",
        "ntfs3",
        "msdos",
        "devfs",
        "devtmpfs",
        "fuseblk",
    }
)
# Filesystem types of known cloud/sync FUSE mounts (best effort).
_SYNC_FSTYPES: frozenset[str] = frozenset(
    {"dfsfuse_dfs", "fuse.dropbox", "fuse.googledrive"}
)

# macOS `mount` line: ``DEVICE on MOUNTPOINT (fstype, opt, opt, ...)``.
_MAC_MOUNT_RE = re.compile(r"^(?P<dev>.+?) on (?P<mp>.+?) \((?P<opts>[^)]*)\)\s*$")


class LiveMountProbe:
    """A :class:`MountProbe` that classifies a path's real mount, fail-closed."""

    def __init__(self, *, platform: str | None = None) -> None:
        self._platform = platform or sys.platform

    def classify_mount(self, path: Path) -> MountFacts:
        try:
            mounts = self._read_mounts()
            entry = _longest_mount_prefix(Path(path), mounts)
        except Exception as exc:  # noqa: BLE001 - any probe failure is unavailable
            return MountFacts(
                state=MOUNT_UNAVAILABLE,
                source="probe_error",
                detail=f"mount probe raised {type(exc).__name__}",
            )
        if entry is None:
            return MountFacts(
                state=MOUNT_UNAVAILABLE,
                source="no_mount",
                detail="no covering mount found for the path",
            )
        fstype, opts = entry
        return _classify_fstype(fstype, opts)

    # --- platform mount-table readers ----------------------------------------

    def _read_mounts(self) -> list[tuple[str, str, frozenset[str]]]:
        """Return ``(mountpoint, fstype, opts)`` for every mount, longest last-ok."""
        if self._platform.startswith("linux"):
            return _read_linux_mountinfo(Path("/proc/self/mountinfo").read_text(encoding="utf-8"))
        # macOS / BSD
        out = subprocess.run(
            ["/sbin/mount"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        return _parse_mac_mount(out)


def _read_linux_mountinfo(text: str) -> list[tuple[str, str, frozenset[str]]]:
    entries: list[tuple[str, str, frozenset[str]]] = []
    for line in text.splitlines():
        # mountinfo: ... 4=mountpoint ... " - " fstype source superopts
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or not right_fields:
            continue
        mountpoint = _unescape_mountinfo(left_fields[4])
        fstype = right_fields[0].lower()
        entries.append((mountpoint, fstype, frozenset()))
    return entries


def _unescape_mountinfo(field: str) -> str:
    # mountinfo octal-escapes space (\040), tab (\011), newline (\012), backslash (\134)
    return (
        field.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _parse_mac_mount(text: str) -> list[tuple[str, str, frozenset[str]]]:
    entries: list[tuple[str, str, frozenset[str]]] = []
    for line in text.splitlines():
        match = _MAC_MOUNT_RE.match(line)
        if not match:
            continue
        mountpoint = match.group("mp")
        opts = [o.strip() for o in match.group("opts").split(",") if o.strip()]
        if not opts:
            continue
        fstype = opts[0].lower()
        entries.append((mountpoint, fstype, frozenset(o.lower() for o in opts[1:])))
    return entries


def _longest_mount_prefix(
    path: Path, mounts: list[tuple[str, str, frozenset[str]]]
) -> tuple[str, frozenset[str]] | None:
    """The (fstype, opts) of the mount whose mountpoint is the longest prefix."""
    try:
        target = path.resolve()
    except (OSError, RuntimeError):
        target = path
    best: tuple[int, str, frozenset[str]] | None = None
    for mountpoint, fstype, opts in mounts:
        try:
            mp = Path(mountpoint).resolve()
        except (OSError, RuntimeError):
            mp = Path(mountpoint)
        if target == mp or mp in target.parents:
            depth = len(mp.parts)
            if best is None or depth > best[0]:
                best = (depth, fstype, opts)
    if best is None:
        return None
    return best[1], best[2]


def _classify_fstype(fstype: str, opts: frozenset[str]) -> MountFacts:
    fstype = fstype.lower()
    if fstype in _NETWORK_FSTYPES:
        return MountFacts(state=MOUNT_NETWORK, source="mount_table", detail=fstype)
    if fstype in _SYNC_FSTYPES:
        return MountFacts(state=MOUNT_SYNC_CLOUD, source="mount_table", detail=fstype)
    if fstype in _LOCAL_FSTYPES or "local" in opts:
        return MountFacts(state=MOUNT_LOCAL, source="mount_table", detail=fstype)
    # Probed successfully but the fstype is unrecognised — fail closed rather
    # than guess "local".
    return MountFacts(
        state=MOUNT_UNAVAILABLE,
        source="unrecognised_fstype",
        detail=fstype,
    )
