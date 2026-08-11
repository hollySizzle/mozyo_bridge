"""Shared scheduler lifecycle lock and effect-boundary regressions (#15192 r17)."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    supervisor_launchd as launchd,
    supervisor_launchd_fs as launchd_fs,
    supervisor_scheduler_lifecycle_lock as lifecycle,
    supervisor_systemd as systemd,
    supervisor_systemd_fs as systemd_fs,
)


def _result(returncode=0, stdout=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": "redacted"})()


class LifecycleLockIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _dir_fd(self):
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)

    def _acquire(self):
        fd = self._dir_fd()
        try:
            return lifecycle.SchedulerLifecycleLock.acquire(fd)
        finally:
            os.close(fd)

    def test_two_writer_barrier_refuses_second_and_recovers_after_release(self):
        held = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def owner():
            with self._acquire():
                held.set()
                release.wait(timeout=5)
            finished.set()

        thread = threading.Thread(target=owner)
        thread.start()
        self.assertTrue(held.wait(timeout=5))
        with self.assertRaises(lifecycle.SchedulerLifecycleLockBusy):
            self._acquire()
        release.set()
        self.assertTrue(finished.wait(timeout=5))
        thread.join(timeout=5)
        with self._acquire():
            pass
        lock_path = self.root / lifecycle.LIFECYCLE_LOCK_NAME
        self.assertTrue(lock_path.is_file())
        self.assertEqual(0o600, stat.S_IMODE(lock_path.stat().st_mode))

    def test_symlink_hardlink_and_loose_mode_are_unsafe(self):
        lock_path = self.root / lifecycle.LIFECYCLE_LOCK_NAME
        victim = self.root / "victim"
        victim.write_text("sentinel", encoding="utf-8")
        for kind in ("symlink", "hardlink", "mode"):
            with self.subTest(kind=kind):
                if os.path.lexists(lock_path):
                    lock_path.unlink()
                if kind == "symlink":
                    lock_path.symlink_to(victim)
                else:
                    lock_path.write_text("", encoding="utf-8")
                    os.chmod(lock_path, 0o600)
                    if kind == "hardlink":
                        os.link(lock_path, self.root / "other-name")
                    else:
                        os.chmod(lock_path, 0o644)
                try:
                    with self.assertRaises(lifecycle.SchedulerLifecycleLockUnsafe):
                        self._acquire()
                finally:
                    other = self.root / "other-name"
                    if other.exists():
                        other.unlink()

    def test_new_lock_mode_is_exact_even_under_a_restrictive_umask(self):
        previous = os.umask(0o777)
        try:
            with self._acquire():
                pass
        finally:
            os.umask(previous)
        mode = stat.S_IMODE((self.root / lifecycle.LIFECYCLE_LOCK_NAME).stat().st_mode)
        self.assertEqual(lifecycle.LIFECYCLE_LOCK_MODE, mode)


class AdapterLifecycleFenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mozyo = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.mozyo.cleanup)
        self.os_home = Path(self.tmp.name)
        self.mozyo_home = Path(self.mozyo.name)

    def test_launchd_busy_is_typed_and_has_no_manager_effect(self):
        calls = []
        with launchd_fs.acquire_lifecycle_lock(self.os_home):
            with patch.object(launchd, "_running_on_darwin", return_value=True):
                result = launchd.install(
                    os_home=self.os_home, mozyo_home=self.mozyo_home,
                    runner=lambda argv: calls.append(list(argv)),
                    which=lambda _name: "/opt/bin/mozyo-bridge",
                )
        self.assertEqual(launchd.REASON_LIFECYCLE_BUSY, result["reason"])
        self.assertEqual(launchd.EFFECT_NONE, result["effect_state"])
        self.assertEqual([], calls)

    def test_systemd_busy_allows_only_read_only_capability_checks(self):
        calls = []

        def runner(raw):
            argv = list(raw)
            calls.append(argv)
            if argv[0] == "systemctl":
                return _result()
            return _result(stdout=json.dumps({"type": "s", "data": "test-systemd"}))

        with systemd_fs.acquire_lifecycle_lock(self.os_home):
            with patch.object(systemd, "_running_on_linux", return_value=True):
                result = systemd.install(
                    os_home=self.os_home, mozyo_home=self.mozyo_home, runner=runner,
                    which=lambda _name: "/opt/bin/mozyo-bridge",
                )
        self.assertEqual(systemd.REASON_LIFECYCLE_BUSY, result["reason"])
        self.assertEqual(systemd.EFFECT_NONE, result["effect_state"])
        self.assertFalse(any(c[0] == "systemctl" and c[2] != "show" for c in calls))


if __name__ == "__main__":
    unittest.main()
