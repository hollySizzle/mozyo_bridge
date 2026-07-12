"""End-to-end canonical gate-record -> callback loop (Redmine #13520 review F1a).

Proves the production path the re-audit required: a callback-required gate recorded through the
canonical writer (which posts a marker-bearing note) is discovered by the watcher and delivered
exactly once — producer -> Redmine journal -> poll/parse -> exact-journal classify -> outbox ->
one-send callback. The marker is produced by ``emit_gate_record`` (a fake transport captures the
posted note), NOT hand-authored in the test fixture — closing the F1a gap where only test
fixtures wrote markers.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_DELIVERED
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_gate_record import (
    emit_gate_record,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (
    discover_candidates,
    run_once,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
)


class _CapturingTransport:
    """A fake Redmine note transport that captures posted notes as durable journal entries."""

    def __init__(self):
        self.journals: dict = {}
        self._n = 90000

    def post_issue_note(self, issue_id, notes):
        self._n += 1
        self.journals.setdefault(str(issue_id), []).append(
            RedmineJournalEntry(issue_id=str(issue_id), journal_id=str(self._n), notes=notes)
        )
        return f"redmine:issue={issue_id}"


class _JournalSource:
    """A Redmine journal source backed by the transport's captured notes (the 'live poll')."""

    def __init__(self, transport):
        self._transport = transport

    def read_entries(self, issue_id):
        return list(self._transport.journals.get(str(issue_id), []))


class CanonicalGateRecordE2ETest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.outbox = CallbackOutbox(path=Path(self._tmp.name) / "wf.sqlite")

    def test_recorded_gate_is_discovered_and_delivered_once(self):
        transport = _CapturingTransport()
        # PRODUCER: record a real callback-required gate through the canonical writer (posts a
        # marker-bearing note to "Redmine"). No hand-written marker anywhere in this test.
        receipt = emit_gate_record("13518", "review_request", body="US review posted", transport=transport)
        self.assertTrue(receipt.recorded)

        # DISCOVER + CLASSIFY + OUTBOX + DELIVER-ONCE over the same source the note was posted to.
        source = _JournalSource(transport)
        proc = CallbackOutboxProcessor(self.outbox, source)
        candidates = discover_candidates(source, "13518")
        self.assertEqual([(c.journal, c.notification_kind) for c in candidates], [("90001", "review_request")])

        sent = []
        report = run_once(
            proc, lambda row: sent.append(row.journal) or SEND_DELIVERED,
            candidates=candidates, stale_seconds=0,
        )
        self.assertEqual([d["journal"] for d in report["deliver"]["delivered"]], ["90001"])
        self.assertEqual(sent, ["90001"])  # exactly one send
        row = self.outbox.read()[0]
        self.assertEqual((row.normalized_gate, row.state), ("review_request", CALLBACK_DELIVERED))

    def test_production_entrypoint_records_and_gate_is_discoverable(self):
        # #13520 review R2-F1: drive the PRODUCTION CLI entrypoint (`workflow callbacks --emit-gate`)
        # end-to-end — the note is posted through the governed writer's env-resolved transport (not
        # a direct emit_gate_record call), then discovered -> outbox -> one-send.
        import argparse
        from unittest.mock import patch
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            cli_workflow_callbacks as cli,
        )

        transport = _CapturingTransport()
        store = str(Path(self._tmp.name) / "wf.sqlite")
        with patch.object(cli, "_outbox_from_args", lambda args: self.outbox), patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure."
            "redmine_note_transport.redmine_delivery_transport_from_env",
            lambda: transport,
        ):
            rc = cli.cmd_workflow_callbacks(
                argparse.Namespace(
                    emit_gate=True, issue="13518", gate="implementation_done", body="done",
                    json=True, store_path=store, sweep=False, ingest=False, deliver=False,
                    run_once=False, watch=False, recovery_plan=False,
                )
            )
        self.assertEqual(rc, 0)  # recorded -> exit 0
        source = _JournalSource(transport)
        cands = discover_candidates(source, "13518")
        self.assertEqual([c.notification_kind for c in cands], ["implementation_done"])
        run_once(CallbackOutboxProcessor(self.outbox, source), lambda row: SEND_DELIVERED,
                 candidates=cands, stale_seconds=0)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)

    def test_production_entrypoint_fail_closed_exit_when_not_recorded(self):
        # #13520 review R2-F1: opt-in unset -> nothing written AND a non-zero exit (a caller reading
        # only the return code can never treat an un-written gate as recorded).
        import argparse
        from unittest.mock import patch
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            cli_workflow_callbacks as cli,
        )

        with patch.object(cli, "_outbox_from_args", lambda args: self.outbox), patch(
            "mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure."
            "redmine_note_transport.redmine_delivery_transport_from_env",
            lambda: None,  # opt-in unset
        ):
            rc = cli.cmd_workflow_callbacks(
                argparse.Namespace(
                    emit_gate=True, issue="13518", gate="implementation_done", body="",
                    json=True, store_path=str(Path(self._tmp.name) / "wf.sqlite"),
                    sweep=False, ingest=False, deliver=False, run_once=False, watch=False,
                    recovery_plan=False,
                )
            )
        self.assertEqual(rc, 1)  # NOT recorded -> fail-closed non-zero exit

    def test_owner_close_waiting_gate_records_and_delivers(self):
        transport = _CapturingTransport()
        emit_gate_record("13518", "owner_close_approval_waiting", transport=transport)
        source = _JournalSource(transport)
        proc = CallbackOutboxProcessor(self.outbox, source)
        cands = discover_candidates(source, "13518")
        run_once(proc, lambda row: SEND_DELIVERED, candidates=cands, stale_seconds=0)
        # F5 vocab + F1a producer: the marker-facing state maps onto the runtime owner_close_approval gate.
        self.assertEqual(self.outbox.read()[0].normalized_gate, "owner_close_approval")


if __name__ == "__main__":
    unittest.main()
