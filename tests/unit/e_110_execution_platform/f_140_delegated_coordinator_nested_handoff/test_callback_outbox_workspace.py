"""Workspace-scoped callback outbox tests (Redmine #13520 review R2-F5).

A shared home DB must partition callback rows / claims by workspace: two workspaces' watchers never
claim each other's rows, the same (source, issue, journal, gate, route) coexists across workspaces
(widened UNIQUE), and the v2 -> v3 migration preserves existing rows (workspace_id='').
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CallbackOutboxKey
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DELIVERED,
    CALLBACK_PENDING,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackCandidate,
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    render_workflow_event_marker,
)


def _key(ws: str) -> CallbackOutboxKey:
    return CallbackOutboxKey(
        source="redmine", issue="13518", journal="75094",
        normalized_gate="implementation_done", callback_route="coordinator", workspace_id=ws,
    )


class OutboxPartitionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.outbox = CallbackOutbox(path=Path(self._tmp.name) / "wf.sqlite")

    def test_same_subkey_coexists_across_workspaces(self):
        # The SAME (source, issue, journal, gate, route) in two workspaces is two distinct rows —
        # neither workspace's callback is dropped by the other's (the loss the finding warns of).
        self.assertTrue(self.outbox.enqueue(_key("A")).inserted)
        self.assertTrue(self.outbox.enqueue(_key("B")).inserted)
        self.assertEqual(len(self.outbox.read()), 2)

    def test_claim_is_partitioned_by_workspace(self):
        self.outbox.enqueue(_key("A"))
        self.outbox.enqueue(_key("B"))
        claimed_a = self.outbox.claim_pending(workspace_id="A")
        self.assertEqual([r.workspace_id for r in claimed_a], ["A"])  # only A's row
        # B's row is untouched (still pending) — A's watcher never claimed it.
        pending = self.outbox.read(states=[CALLBACK_PENDING])
        self.assertEqual([r.workspace_id for r in pending], ["B"])

    def test_unpartitioned_claim_is_backcompat(self):
        self.outbox.enqueue(_key(""))
        self.assertEqual(len(self.outbox.claim_pending()), 1)  # workspace_id=None -> any (legacy)


class TwoWorkspaceProcessorTest(unittest.TestCase):
    """Two watchers on one shared home DB: each delivers ONLY its own workspace's row."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.outbox = CallbackOutbox(path=Path(self._tmp.name) / "wf.sqlite")
        entry = RedmineJournalEntry("13518", "75094", f"done {render_workflow_event_marker('implementation_done')}")
        self.source = _FakeSource({"13518": [entry]})

    def _proc(self, ws):
        return CallbackOutboxProcessor(self.outbox, self.source, workspace_id=ws)

    def test_each_workspace_delivers_only_its_own_row(self):
        # Both workspaces ingest the same gate; two rows exist (partitioned).
        self._proc("A").ingest([CallbackCandidate("13518", "75094", "coordinator")])
        self._proc("B").ingest([CallbackCandidate("13518", "75094", "coordinator")])
        self.assertEqual(len(self.outbox.read()), 2)

        # Workspace A's watcher delivers ONLY A's row; B's row is never sent by A.
        sent_a = []
        self._proc("A").deliver(lambda row: sent_a.append(row.workspace_id) or SEND_DELIVERED)
        self.assertEqual(sent_a, ["A"])  # single winner, correct workspace
        by_ws = {r.workspace_id: r.state for r in self.outbox.read()}
        self.assertEqual(by_ws["A"], CALLBACK_DELIVERED)
        self.assertEqual(by_ws["B"], CALLBACK_PENDING)  # B untouched (wrong-workspace zero-send)

        # Workspace B's watcher then delivers ONLY B's row.
        sent_b = []
        self._proc("B").deliver(lambda row: sent_b.append(row.workspace_id) or SEND_DELIVERED)
        self.assertEqual(sent_b, ["B"])


class V2ToV3MigrationTest(unittest.TestCase):
    """The v2 -> v3 migration adds workspace_id + widens the UNIQUE, preserving existing rows."""

    def test_v2_callback_rows_are_preserved_with_blank_workspace(self):
        path = Path(tempfile.mkdtemp()) / "wf.sqlite"
        # Build a v2-shaped callback_outbox (no workspace_id, old UNIQUE) with one row.
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE callback_outbox (
                source TEXT NOT NULL, issue TEXT NOT NULL, journal TEXT NOT NULL,
                normalized_gate TEXT NOT NULL, callback_route TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
                send_attempted INTEGER NOT NULL DEFAULT 0, claim_token TEXT NOT NULL DEFAULT '',
                claimed_at TEXT NOT NULL DEFAULT '', notification_kind TEXT NOT NULL DEFAULT '',
                notification_summary TEXT NOT NULL DEFAULT '', gate_mismatch INTEGER NOT NULL DEFAULT 0,
                detail TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '',
                seq INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(source, issue, journal, normalized_gate, callback_route)
            );
            """
        )
        conn.execute(
            "INSERT INTO callback_outbox (source, issue, journal, normalized_gate, callback_route, "
            "state, seq, created_at, updated_at) VALUES "
            "('redmine','13518','75094','implementation_done','coordinator','pending',0,'t','t')"
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()

        # Opening through CallbackOutbox migrates to v3; the row is preserved with workspace_id=''.
        outbox = CallbackOutbox(path=path)
        rows = outbox.read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].workspace_id, "")
        self.assertEqual((rows[0].issue, rows[0].state), ("13518", "pending"))
        # The widened UNIQUE now admits the same sub-key in a different workspace.
        self.assertTrue(outbox.enqueue(_key("A")).inserted)


class _FakeSource:
    def __init__(self, entries):
        self._entries = entries

    def read_entries(self, issue_id):
        return list(self._entries.get(str(issue_id), []))


if __name__ == "__main__":
    unittest.main()
