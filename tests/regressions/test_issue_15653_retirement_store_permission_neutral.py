"""#15653: the scratch retirement store's mode/uid is not a runtime health condition.

Owner decision (2026-08-18): the retirement authority is not a secret file; isolation and
permission boundaries are the harness's responsibility, not the running agent's. A non-0600
mode (or a foreign-looking owner) alone must never make the store unhealthy / unreadable /
blocked, nothing may chmod an existing store, and no surface may steer an LLM or operator
toward mode checks or permission repair. Non-permission integrity (file type, symlink, seal
identity, schema, artifact completeness, inode pins) stays fail-closed.
"""

from __future__ import annotations

import argparse
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.core.state.scratch_retirement_fence import (
    RetirementUnit,
    ScratchRetirementFence,
    ScratchRetirementFenceError,
    slot_digest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_retirement_store import (  # noqa: E501
    cmd_herdr_retirement_store_status,
)


class RetirementStorePermissionNeutralTest(unittest.TestCase):
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
        self.unit = RetirementUnit("ws", "lane", slot_digest(["mzb1_a", "mzb1_b"]))

    def _bootstrap(self, *, mode: int) -> None:
        with self.fence.transaction(self.unit, live_pair_present=True):
            pass
        os.chmod(self.fence.path, mode)
        os.chmod(self.fence.seal_path, mode)

    def _modes(self) -> tuple[int, int]:
        return (
            self.fence.path.stat().st_mode & 0o777,
            self.fence.seal_path.stat().st_mode & 0o777,
        )

    def test_non_0600_store_serves_reads_and_writes_without_a_rechmod(self):
        self._bootstrap(mode=0o644)
        self.assertIsNone(self.fence.peek(self.unit))
        status = self.fence.status()
        self.assertEqual(status["store_state"], "present")
        self.assertTrue(status["readable"])
        with self.fence.transaction(self.unit, live_pair_present=True) as txn:
            attempt = txn.reserve(pinned=())
            txn.mark_completed(attempt_id=attempt.attempt_id, closed=())
        self.assertTrue(self.fence.peek(self.unit).completed)
        # The store was read and written throughout, and its mode never changed.
        self.assertEqual(self._modes(), (0o644, 0o644))

    def test_world_readable_store_is_not_a_health_failure(self):
        self._bootstrap(mode=0o666)
        self.assertIsNone(self.fence.peek(self.unit))
        self.assertTrue(self.fence.status()["readable"])
        self.assertEqual(self._modes(), (0o666, 0o666))

    def test_foreign_owner_observation_does_not_block(self):
        self._bootstrap(mode=0o644)
        real_euid = os.geteuid()
        with patch("os.geteuid", return_value=real_euid + 12345):
            self.assertIsNone(self.fence.peek(self.unit))
            self.assertTrue(self.fence.status()["readable"])

    def test_status_output_carries_no_permission_repair_guidance(self):
        self._bootstrap(mode=0o666)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cmd_herdr_retirement_store_status(
                argparse.Namespace(json=False, repo=None)
            )
        self.assertEqual(code, 0)
        text = output.getvalue().lower()
        for forbidden in ("chmod", "permission", "0600", "mode", "owner"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(self._modes(), (0o666, 0o666))

    def test_non_permission_integrity_still_fails_closed_on_a_non_0600_store(self):
        self._bootstrap(mode=0o644)
        # Symlinked primary: file-type integrity is untouched by #15653.
        moved = self.home / "moved.sqlite3"
        self.fence.path.rename(moved)
        self.fence.path.symlink_to(moved)
        with self.assertRaises(ScratchRetirementFenceError):
            self.fence.peek(self.unit)
        self.fence.path.unlink()
        moved.rename(self.fence.path)
        # Seal identity mismatch: still a fail-closed store replacement signal.
        self.fence.seal_path.write_text("different-nonce", encoding="utf-8")
        os.chmod(self.fence.seal_path, 0o644)
        with self.assertRaises(ScratchRetirementFenceError):
            self.fence.peek(self.unit)

    def test_migration_leaves_a_loose_mode_primary_untouched(self):
        from mozyo_bridge.core.state.scratch_retirement_fence import (
            _META_TABLE_SQL,
            _TABLE_SQL,
        )

        conn = sqlite3.connect(self.fence.path)
        conn.execute(_TABLE_SQL)
        conn.execute(_META_TABLE_SQL)
        conn.execute("INSERT INTO store_meta VALUES ('store_nonce','nonce')")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        self.fence.seal_path.write_text("nonce", encoding="utf-8")
        os.chmod(self.fence.path, 0o664)
        os.chmod(self.fence.seal_path, 0o664)
        self.assertEqual(self.fence.migrate_v1_backup_first(), 2)
        self.assertEqual(self._modes(), (0o664, 0o664))
        self.assertEqual(self.fence.store_shape().state, "present")
        self.assertTrue(self.fence.status()["readable"])


if __name__ == "__main__":
    unittest.main()
