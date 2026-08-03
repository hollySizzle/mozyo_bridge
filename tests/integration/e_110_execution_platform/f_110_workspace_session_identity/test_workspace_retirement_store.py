from __future__ import annotations

import sqlite3
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.workspace_registry import (
    load_workspace_by_id,
    register_workspace,
    registry_path,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.workspace_retirement import (
    WorkspaceRetirementAuthorityError,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.infrastructure.workspace_retirement_registry import (
    SQLiteWorkspaceRetirementRegistry,
)


class WorkspaceRetirementStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.repo = root / "missing-later"
        self.repo.mkdir()
        self.record = register_workspace(self.repo, home=self.home).record
        shutil.rmtree(self.repo)
        self.store = SQLiteWorkspaceRetirementRegistry(home=self.home)

    def test_backup_first_delete_cascade_and_replay(self) -> None:
        observation = self.store.observe(self.record.workspace_id)
        self.assertEqual(observation.path_state, "missing")
        plan_digest = "b" * 64
        outcome = self.store.retire(
            workspace_id=self.record.workspace_id,
            expected_record_digest=observation.record_digest,
            plan_digest=plan_digest,
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.backup_receipt, plan_digest)
        backup_path = (
            self.home / "workspace-registry-backups" / f"{plan_digest}.sqlite"
        )
        self.assertEqual(stat.S_IMODE(backup_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
        self.assertIsNone(load_workspace_by_id(self.record.workspace_id, home=self.home))
        backup = self.store.observe_retired(self.record.workspace_id, plan_digest)
        self.assertEqual(backup.record_digest, observation.record_digest)

        conn = sqlite3.connect(registry_path(self.home))
        try:
            activity = conn.execute(
                "SELECT 1 FROM workspace_activity WHERE workspace_id = ?",
                (self.record.workspace_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(activity)
        replay = self.store.retire(
            workspace_id=self.record.workspace_id,
            expected_record_digest=observation.record_digest,
            plan_digest=plan_digest,
        )
        self.assertTrue(replay.ok)

    def test_wrong_record_digest_is_zero_delete(self) -> None:
        outcome = self.store.retire(
            workspace_id=self.record.workspace_id,
            expected_record_digest="f" * 64,
            plan_digest="c" * 64,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "record_drift")
        self.assertIsNotNone(load_workspace_by_id(self.record.workspace_id, home=self.home))

    def test_path_reappearing_after_plan_is_zero_delete(self) -> None:
        observation = self.store.observe(self.record.workspace_id)
        self.repo.mkdir()
        outcome = self.store.retire(
            workspace_id=self.record.workspace_id,
            expected_record_digest=observation.record_digest,
            plan_digest="e" * 64,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "workspace_path_reappeared")
        self.assertIsNotNone(load_workspace_by_id(self.record.workspace_id, home=self.home))

    def test_corrupt_existing_backup_is_fail_closed(self) -> None:
        observation = self.store.observe(self.record.workspace_id)
        plan_digest = "d" * 64
        backup = self.home / "workspace-registry-backups" / f"{plan_digest}.sqlite"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"not sqlite")
        outcome = self.store.retire(
            workspace_id=self.record.workspace_id,
            expected_record_digest=observation.record_digest,
            plan_digest=plan_digest,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "backup_failed")
        self.assertIsNotNone(load_workspace_by_id(self.record.workspace_id, home=self.home))

    def test_corrupt_replay_backup_is_unreadable_not_absent(self) -> None:
        plan_digest = "a" * 64
        backup = self.home / "workspace-registry-backups" / f"{plan_digest}.sqlite"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"not sqlite")
        backup.chmod(0o600)
        with self.assertRaisesRegex(
            WorkspaceRetirementAuthorityError, "backup_not_readable"
        ):
            self.store.observe_retired(self.record.workspace_id, plan_digest)


if __name__ == "__main__":
    unittest.main()
