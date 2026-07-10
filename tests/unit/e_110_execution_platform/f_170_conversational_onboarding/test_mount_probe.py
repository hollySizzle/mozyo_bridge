"""Live mount-probe adapter (Redmine #13508), with mocked platform mount tables."""

from __future__ import annotations

import unittest
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application import (
    mount_probe as mp,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.path_safety import (
    MOUNT_LOCAL,
    MOUNT_NETWORK,
    MOUNT_UNAVAILABLE,
)

_MAC_MOUNT = (
    "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
    "devfs on /dev (devfs, local, nobrowse)\n"
    "/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled)\n"
    "map -hosts on /net (autofs, nosuid)\n"
    "server:/export on /Volumes/nfsshare (nfs, nodev, nosuid)\n"
)

_LINUX_MOUNTINFO = (
    "22 1 0:20 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "23 22 0:21 / /home rw shared:2 - xfs /dev/sda2 rw\n"
    "24 22 0:22 / /mnt/nas rw shared:3 - nfs4 1.2.3.4:/export rw\n"
    "25 22 0:23 / /tmp rw shared:4 - tmpfs tmpfs rw\n"
)


def _mac_parsed():
    return mp._parse_mac_mount(_MAC_MOUNT)


def _linux_parsed():
    return mp._read_linux_mountinfo(_LINUX_MOUNTINFO)


class MacMountParseTests(unittest.TestCase):
    def test_local_apfs_data_volume(self) -> None:
        entry = mp._longest_mount_prefix(
            Path("/System/Volumes/Data/Users/x/proj"), _mac_parsed()
        )
        self.assertIsNotNone(entry)
        facts = mp._classify_fstype(*entry)
        self.assertEqual(facts.state, MOUNT_LOCAL)

    def test_nfs_volume_is_network(self) -> None:
        entry = mp._longest_mount_prefix(Path("/Volumes/nfsshare/data"), _mac_parsed())
        facts = mp._classify_fstype(*entry)
        self.assertEqual(facts.state, MOUNT_NETWORK)


class LinuxMountParseTests(unittest.TestCase):
    def test_ext4_root_is_local(self) -> None:
        entry = mp._longest_mount_prefix(Path("/var/data"), _linux_parsed())
        self.assertEqual(mp._classify_fstype(*entry).state, MOUNT_LOCAL)

    def test_longest_prefix_picks_nfs_submount(self) -> None:
        entry = mp._longest_mount_prefix(Path("/mnt/nas/share"), _linux_parsed())
        self.assertEqual(mp._classify_fstype(*entry).state, MOUNT_NETWORK)

    def test_tmpfs_is_local(self) -> None:
        entry = mp._longest_mount_prefix(Path("/tmp/x"), _linux_parsed())
        self.assertEqual(mp._classify_fstype(*entry).state, MOUNT_LOCAL)


class ClassifyFstypeTests(unittest.TestCase):
    def test_unrecognised_fstype_is_unavailable(self) -> None:
        self.assertEqual(
            mp._classify_fstype("weirdfs", frozenset()).state, MOUNT_UNAVAILABLE
        )

    def test_local_opt_rescues_unknown_fstype(self) -> None:
        self.assertEqual(
            mp._classify_fstype("weirdfs", frozenset({"local"})).state, MOUNT_LOCAL
        )


class ProbeErrorTests(unittest.TestCase):
    def test_probe_error_is_unavailable(self) -> None:
        class Boom(mp.LiveMountProbe):
            def _read_mounts(self):
                raise OSError("cannot read mounts")

        facts = Boom().classify_mount(Path("/whatever"))
        self.assertEqual(facts.state, MOUNT_UNAVAILABLE)

    def test_no_covering_mount_is_unavailable(self) -> None:
        class Empty(mp.LiveMountProbe):
            def _read_mounts(self):
                return []

        facts = Empty().classify_mount(Path("/whatever"))
        self.assertEqual(facts.state, MOUNT_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
