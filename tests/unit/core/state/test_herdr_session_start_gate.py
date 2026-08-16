from __future__ import annotations

import fcntl
import os
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_session_start_gate import (
    SessionStartGateError,
    _gate_lock_names,
    acquire_session_start_gate,
    release_session_start_gate,
    require_session_start_gate,
)


class HerdrSessionStartGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()

    def test_acquire_bootstraps_one_owned_private_home(self) -> None:
        fresh = Path(self.temp.name) / "fresh" / "home"
        lease = acquire_session_start_gate(fresh, exclusive=False)
        try:
            self.assertTrue(fresh.is_dir())
            self.assertEqual(fresh.stat().st_mode & 0o777, 0o700)
            self.assertEqual(lease.gate_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(len(lease.lock_paths), 2)
            for path in lease.lock_paths:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                prefix, digest = path.stem.split("-", 1)
                self.assertIn(prefix, {"inode", "path"})
                self.assertEqual(len(digest), 64)
                self.assertTrue(
                    all(ch in "0123456789abcdef" for ch in digest)
                )
        finally:
            release_session_start_gate(lease)

    def test_unsafe_or_symlink_home_is_never_bootstrapped_as_authority(self) -> None:
        self.home.chmod(0o777)
        with self.assertRaisesRegex(SessionStartGateError, "home_unsafe"):
            acquire_session_start_gate(self.home, exclusive=False)

        target = Path(self.temp.name) / "target"
        target.mkdir()
        link = Path(self.temp.name) / "linked-home"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(SessionStartGateError, "home_unsafe"):
            acquire_session_start_gate(link, exclusive=False)

    def test_unicode_equivalent_home_spellings_share_one_lock_key(self) -> None:
        name = "動画ドライブ"
        nfc = Path(self.temp.name) / unicodedata.normalize("NFC", name)
        nfd = Path(self.temp.name) / unicodedata.normalize("NFD", name)
        self.assertNotEqual(str(nfc), str(nfd))
        self.assertEqual(
            _gate_lock_names(nfc, (101, 202)),
            _gate_lock_names(nfd, (101, 202)),
        )
        if sys.platform != "darwin":
            return
        nfc.mkdir()
        lease = acquire_session_start_gate(nfc, exclusive=True)
        try:
            with self.assertRaisesRegex(SessionStartGateError, "gate_busy"):
                acquire_session_start_gate(nfd, exclusive=False)
        finally:
            release_session_start_gate(lease)

    def test_path_and_inode_keys_cover_case_alias_and_replacement(self) -> None:
        upper = Path(self.temp.name) / "Workspace"
        lower = Path(self.temp.name) / "workspace"
        upper_names = _gate_lock_names(upper, (11, 22))
        lower_names = _gate_lock_names(lower, (11, 22))
        self.assertEqual(upper_names, lower_names)

        alias_names = _gate_lock_names(Path("/different/alias"), (11, 22))
        self.assertEqual(upper_names[0], alias_names[0])
        self.assertNotEqual(upper_names[1], alias_names[1])

        replacement_names = _gate_lock_names(upper, (33, 44))
        self.assertNotEqual(upper_names[0], replacement_names[0])
        self.assertEqual(upper_names[1], replacement_names[1])

        upper.mkdir()
        lease = acquire_session_start_gate(upper, exclusive=True)
        try:
            with self.assertRaisesRegex(SessionStartGateError, "gate_busy"):
                acquire_session_start_gate(lower, exclusive=False)
        finally:
            release_session_start_gate(lease)

    def test_anchor_parent_owner_trust_is_a_closed_table(self) -> None:
        # #15227 R-correction. Under the canonical runner's sandbox `/` is a namespace-private
        # tmpfs owned by an UNMAPPED uid (surfaced as the kernel overflow uid): no process in
        # the namespace can hold that uid, so with group/other write bits clear no subject can
        # rename `/tmp` there, and the gate must hold. On a real host the init namespace maps
        # every uid, `_unmapped_overflow_uid()` returns None, and the strict owner rule stays:
        # an overflow-OWNED parent is then an ordinary untrusted owner and refuses.
        from types import SimpleNamespace

        from mozyo_bridge.core.state import herdr_session_start_gate as gate

        euid = os.geteuid()

        def _dir_stat(uid: int, mode: int) -> SimpleNamespace:
            return SimpleNamespace(
                st_mode=0o040000 | mode, st_uid=uid, st_dev=7, st_ino=9
            )

        def _anchor(parent_uid: int, parent_mode: int) -> SimpleNamespace:
            anchor_stat = _dir_stat(euid, 0o1777)
            parent = SimpleNamespace(stat=lambda: _dir_stat(parent_uid, parent_mode))
            return SimpleNamespace(
                stat=lambda: anchor_stat, lstat=lambda: anchor_stat, parent=parent
            )

        cases = (
            # (parent_uid, parent_mode, unmapped_overflow, expected, why)
            (0, 0o755, None, True, "root-owned parent: the ordinary host case"),
            (euid, 0o755, None, True, "self-owned parent"),
            (65534, 0o755, 65534, True, "unmapped-overflow parent, unwritable: sandbox"),
            (65534, 0o755, None, False, "overflow parent on a host that MAPS that uid"),
            (65534, 0o775, 65534, False, "unmapped-overflow parent but group-writable"),
            (1234, 0o755, 65534, False, "an ordinary foreign owner is never trusted"),
        )
        for parent_uid, parent_mode, overflow, expected, why in cases:
            with patch.object(
                gate, "_unmapped_overflow_uid", return_value=overflow
            ):
                self.assertEqual(
                    gate._common_anchor_matches(
                        _anchor(parent_uid, parent_mode), (7, 9)
                    ),
                    expected,
                    why,
                )

    def test_unmapped_overflow_uid_is_none_when_the_map_covers_it(self) -> None:
        # Parsing contract on the live host: whatever namespace this test runs in, the
        # helper must answer None exactly when some uid_map range covers the overflow uid.
        from mozyo_bridge.core.state import herdr_session_start_gate as gate

        overflow = int(Path("/proc/sys/kernel/overflowuid").read_text().strip())
        covered = any(
            int(line.split()[0]) <= overflow < int(line.split()[0]) + int(line.split()[2])
            for line in Path("/proc/self/uid_map").read_text().splitlines()
        )
        result = gate._unmapped_overflow_uid()
        if covered:
            self.assertIsNone(result)
        else:
            self.assertEqual(result, overflow)

    def test_writable_nonsticky_common_anchor_is_refused(self) -> None:
        unsafe = Path(self.temp.name) / "unsafe-anchor"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        with patch(
            "mozyo_bridge.core.state.herdr_session_start_gate."
            "HERDR_SESSION_START_GATE_ANCHOR",
            unsafe,
        ):
            with self.assertRaisesRegex(SessionStartGateError, "anchor_unsafe"):
                acquire_session_start_gate(self.home, exclusive=False)

    def test_shared_and_exclusive_conflict_both_directions(self) -> None:
        # The selected home is group-writable under the test runner's umask.  The
        # stable gate root must therefore live under its private, entry-stable parent.
        self.home.chmod(0o775)
        shared = acquire_session_start_gate(self.home, exclusive=False)
        try:
            self.assertNotEqual(shared.gate_root.parent, self.home)
            with self.assertRaisesRegex(SessionStartGateError, "gate_busy"):
                acquire_session_start_gate(self.home, exclusive=True)
        finally:
            release_session_start_gate(shared)

        exclusive = acquire_session_start_gate(self.home, exclusive=True)
        try:
            with self.assertRaisesRegex(SessionStartGateError, "gate_busy"):
                acquire_session_start_gate(self.home, exclusive=False)
            with self.assertRaisesRegex(SessionStartGateError, "gate_busy"):
                acquire_session_start_gate(self.home, exclusive=True)
            self.assertIs(
                require_session_start_gate(
                    exclusive, home=self.home, exclusive=False
                ),
                exclusive,
            )
            self.assertIs(
                require_session_start_gate(
                    exclusive, home=self.home, exclusive=True
                ),
                exclusive,
            )
        finally:
            release_session_start_gate(exclusive)

    def test_lease_is_active_same_home_same_process_only(self) -> None:
        other = Path(self.temp.name) / "other"
        other.mkdir()
        lease = acquire_session_start_gate(self.home, exclusive=True)
        with self.assertRaisesRegex(SessionStartGateError, "lease_invalid"):
            require_session_start_gate(lease, home=other, exclusive=True)
        with patch("os.getpid", return_value=os.getpid() + 1000):
            with self.assertRaisesRegex(SessionStartGateError, "lease_invalid"):
                require_session_start_gate(
                    lease, home=self.home, exclusive=True
                )
        release_session_start_gate(lease)
        with self.assertRaisesRegex(SessionStartGateError, "lease_invalid"):
            require_session_start_gate(lease, home=self.home, exclusive=True)

    def test_path_swap_invalidates_use_and_release_still_closes_fd(self) -> None:
        lease = acquire_session_start_gate(self.home, exclusive=True)
        path = lease.lock_paths[0]
        path.unlink()
        path.write_text("replacement", encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaisesRegex(SessionStartGateError, "lease_invalid"):
            require_session_start_gate(lease, home=self.home, exclusive=True)
        with self.assertRaisesRegex(SessionStartGateError, "release_unverified"):
            release_session_start_gate(lease)
        for fd in lease._fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_home_replacement_cannot_fork_the_home_scoped_lock_inode(self) -> None:
        self.home.chmod(0o775)
        lease = acquire_session_start_gate(self.home, exclusive=True)
        displaced = Path(self.temp.name) / "displaced-home"
        self.home.rename(displaced)
        self.home.mkdir()
        self.home.chmod(0o775)
        with self.assertRaisesRegex(SessionStartGateError, "gate_busy"):
            acquire_session_start_gate(self.home, exclusive=False)
        with self.assertRaisesRegex(SessionStartGateError, "lease_invalid"):
            require_session_start_gate(lease, home=self.home, exclusive=True)
        with self.assertRaisesRegex(SessionStartGateError, "release_unverified"):
            release_session_start_gate(lease)
        for fd in lease._fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_second_key_failure_unwinds_the_first_lock_and_all_fds(self) -> None:
        real_flock = fcntl.flock
        acquired = []

        def fail_second(fd, operation):
            if operation & fcntl.LOCK_UN:
                return real_flock(fd, operation)
            acquired.append(fd)
            if len(acquired) == 2:
                raise BlockingIOError("injected second-key contention")
            return real_flock(fd, operation)

        with patch(
            "mozyo_bridge.core.state.herdr_session_start_gate.fcntl.flock",
            side_effect=fail_second,
        ):
            with self.assertRaisesRegex(SessionStartGateError, "gate_busy"):
                acquire_session_start_gate(self.home, exclusive=True)
        self.assertEqual(len(acquired), 2)
        for fd in acquired:
            with self.assertRaises(OSError):
                os.fstat(fd)

        lease = acquire_session_start_gate(self.home, exclusive=True)
        release_session_start_gate(lease)

    def test_unlock_failure_still_closes_fd(self) -> None:
        lease = acquire_session_start_gate(self.home, exclusive=True)
        with patch(
            "mozyo_bridge.core.state.herdr_session_start_gate.fcntl.flock",
            side_effect=OSError("injected"),
        ):
            with self.assertRaisesRegex(SessionStartGateError, "release_unverified"):
                release_session_start_gate(lease)
        for fd in lease._fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_close_failure_is_reported_after_unlock_and_lease_is_invalidated(self) -> None:
        lease = acquire_session_start_gate(self.home, exclusive=True)
        real_close = os.close

        def close_then_fail(fd):
            real_close(fd)
            raise OSError("injected close uncertainty")

        with patch(
            "mozyo_bridge.core.state.herdr_session_start_gate.os.close",
            side_effect=close_then_fail,
        ):
            with self.assertRaisesRegex(
                SessionStartGateError, "release_unverified"
            ):
                release_session_start_gate(lease)
        with self.assertRaisesRegex(SessionStartGateError, "lease_invalid"):
            require_session_start_gate(
                lease, home=self.home, exclusive=True
            )


if __name__ == "__main__":
    unittest.main()
