"""#15530: macOS must resume an exact interrupted retirement-store migration.

The pre-fix implementation opened ``/dev/fd/<n>`` as a SQLite destination.  macOS
returns ``SQLITE_CANTOPEN`` after the durable control marker and private staging have
already been published, leaving managed dispatch fail-closed with no public recovery rail.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.core.state.scratch_retirement_fence import (
    ScratchRetirementFence,
    ScratchRetirementFenceError,
    _META_TABLE_SQL,
    _TABLE_SQL,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_retirement_store import (  # noqa: E501
    cmd_herdr_retirement_store_recover,
    register_herdr_retirement_store_parser,
)


class RetirementStoreRecoveryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        home_patch = patch(
            "mozyo_bridge.core.state.scratch_retirement_fence.mozyo_bridge_home",
            return_value=self.home,
        )
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.fence = ScratchRetirementFence(home=self.home)

    def _v1(self) -> None:
        conn = sqlite3.connect(self.fence.path)
        conn.execute(_TABLE_SQL)
        conn.execute(_META_TABLE_SQL)
        conn.execute("INSERT INTO store_meta VALUES ('store_nonce','nonce')")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        self.fence.seal_path.write_text("nonce", encoding="utf-8")
        os.chmod(self.fence.path, 0o644)
        os.chmod(self.fence.seal_path, 0o644)

    def _interrupt_after_staging_publish(self) -> tuple[Path, Path]:
        from mozyo_bridge.core.state import scratch_retirement_migration as migration

        with patch.object(
            migration,
            "_copy_sqlite",
            side_effect=OSError("simulated macOS pinned destination refusal"),
        ):
            with self.assertRaisesRegex(
                ScratchRetirementFenceError, "could not be migrated"
            ):
                self.fence.migrate_v1_backup_first()
        backup = self.fence.path.with_name(self.fence.path.name + ".v1.backup")
        control = backup.with_name(backup.name + ".migration")
        payload = json.loads(control.read_text(encoding="utf-8"))
        return control, control.parent / payload["staging_name"]

    def _recover(self, *, write: bool = True) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cmd_herdr_retirement_store_recover(
                argparse.Namespace(write=write, repo=None)
            )
        return code, output.getvalue()

    def test_public_recovery_resumes_pinned_staging_on_macos(self):
        self._v1()
        control, staging = self._interrupt_after_staging_publish()
        self.assertEqual(self.fence.store_shape().state, "damaged")
        self.assertEqual(self._recover(), (0, "retirement authority schema: 2\n"))
        self.assertEqual(self.fence.status()["store_state"], "present")
        self.assertTrue(self.fence.status()["readable"])
        # #15653: recovery never chmods the existing primary DB or seal.
        self.assertEqual(self.fence.path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.fence.seal_path.stat().st_mode & 0o777, 0o644)
        self.assertTrue(control.exists())
        self.assertTrue(staging.exists())
        conn = sqlite3.connect(self.fence.path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            conn.close()

    def test_recovery_refuses_healthy_v1_without_creating_migration_evidence(self):
        self._v1()
        before = (self.fence.path.read_bytes(), self.fence.seal_path.read_bytes())
        code, output = self._recover()
        self.assertEqual(code, 1)
        self.assertIn("recovery refused", output)
        self.assertEqual(
            (self.fence.path.read_bytes(), self.fence.seal_path.read_bytes()), before
        )
        self.assertFalse(self.fence.lock_path.exists())
        self.assertFalse(
            self.fence.path.with_name(
                self.fence.path.name + ".v1.backup.migration"
            ).exists()
        )

    def test_drifted_control_is_zero_write_refused(self):
        self._v1()
        control, staging = self._interrupt_after_staging_publish()
        control.write_text("{}", encoding="utf-8")
        observed = {
            path: path.read_bytes()
            for path in (
                self.fence.path,
                self.fence.seal_path,
                control,
                staging / "store.sqlite3",
                staging / "store.seal",
            )
        }
        code, output = self._recover()
        self.assertEqual(code, 1)
        self.assertIn("recovery refused", output)
        self.assertEqual({path: path.read_bytes() for path in observed}, observed)
        conn = sqlite3.connect(self.fence.path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            conn.close()

    def test_recovery_requires_explicit_write_and_is_registered(self):
        self.assertEqual(
            self._recover(write=False),
            (2, "retirement-store recover requires --write\n"),
        )
        parser = argparse.ArgumentParser()
        herdr = parser.add_subparsers(dest="herdr", required=True)
        register_herdr_retirement_store_parser(herdr)
        args = parser.parse_args(["retirement-store", "recover", "--write"])
        self.assertIs(args.func, cmd_herdr_retirement_store_recover)

    def test_private_image_validation_failure_does_not_truncate_staging(self):
        from mozyo_bridge.core.state import scratch_retirement_migration as migration

        self._v1()
        staging = self.home / "pinned-staging.sqlite3"
        staging.write_bytes(b"existing-staging-evidence")
        os.chmod(staging, 0o600)
        staging_info = staging.stat()
        pin = (staging_info.st_dev, staging_info.st_ino)
        wanted = migration._logical_digest(self.fence.path)

        with patch.object(
            migration,
            "_logical_digest_connection",
            side_effect=(
                wanted,
                migration.ScratchRetirementMigrationError(
                    "simulated private image validation failure"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                migration.ScratchRetirementMigrationError,
                "private image validation failure",
            ):
                migration._copy_sqlite(self.fence.path, staging, pin)

        self.assertEqual(staging.read_bytes(), b"existing-staging-evidence")
        self.assertEqual((staging.stat().st_dev, staging.stat().st_ino), pin)


if __name__ == "__main__":
    unittest.main()
