"""Production callback runtime tests (Redmine #13520 review F1).

`run_once` ties ingest -> deliver-once -> sweep; `watch` runs one pass per Herdr-event wake
(unconditionally re-reading Redmine), bounded so it never becomes an unbounded poll.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox, CALLBACK_DELIVERED
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_outbox_processor import (
    CallbackCandidate,
    CallbackOutboxProcessor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_runtime import (
    discover_candidates,
    run_once,
    watch,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    render_workflow_event_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_delivery import (
    SEND_DELIVERED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
)


class _FakeSource:
    def __init__(self, entries):
        self._entries = entries

    def read_entries(self, issue_id):
        return self._entries.get(str(issue_id), [])


def _entry(issue, journal, gate):
    return RedmineJournalEntry(issue, journal, f"[mozyo:workflow-event:gate={gate}]")


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.outbox = CallbackOutbox(path=Path(self._tmp.name) / "wf.sqlite")
        self.source = _FakeSource({"13518": [_entry("13518", "75094", "implementation_done")]})
        self.proc = CallbackOutboxProcessor(self.outbox, self.source)

    def test_run_once_ingests_delivers_and_sweeps_in_one_pass(self):
        report = run_once(
            self.proc,
            lambda row: SEND_DELIVERED,
            candidates=[CallbackCandidate("13518", "75094", "coordinator", "implementation_done")],
            stale_seconds=0,
        )
        self.assertEqual(report["ingest"]["enqueued"], 1)
        self.assertEqual(len(report["deliver"]["delivered"]), 1)
        self.assertEqual(self.outbox.read()[0].state, CALLBACK_DELIVERED)
        # Nothing left pending after a successful delivery.
        self.assertEqual(report["sweep"]["pending"], [])

    def test_run_once_drain_only_without_candidates(self):
        # Enqueue out-of-band, then a candidate-less pass just delivers + sweeps.
        self.proc.ingest([CallbackCandidate("13518", "75094", "coordinator", "implementation_done")])
        report = run_once(self.proc, lambda row: SEND_DELIVERED, stale_seconds=0)
        self.assertNotIn("ingest", report)
        self.assertEqual(len(report["deliver"]["delivered"]), 1)


class DiscoverCandidatesTest(unittest.TestCase):
    """F1-R1 production discovery: real gate-marker journals become callback candidates."""

    def test_gate_markers_become_coordinator_candidates(self):
        # Real gate journals whose notes carry the producer's marker (render_workflow_event_marker).
        source = _FakeSource(
            {
                "13543": [
                    RedmineJournalEntry("13543", "75212", f"review {render_workflow_event_marker('review_request')}"),
                    RedmineJournalEntry("13543", "75094", f"done {render_workflow_event_marker('implementation_done')}"),
                    RedmineJournalEntry("13543", "75300", "just prose, no marker"),
                ]
            }
        )
        cands = discover_candidates(source, "13543")
        self.assertEqual(
            [(c.journal, c.callback_route, c.notification_kind) for c in cands],
            [("75212", "coordinator", "review_request"), ("75094", "coordinator", "implementation_done")],
        )

    def test_no_gate_marker_discovers_nothing(self):
        source = _FakeSource({"13543": [RedmineJournalEntry("13543", "75300", "prose only")]})
        self.assertEqual(discover_candidates(source, "13543"), [])


class ProductionPipelineTest(unittest.TestCase):
    """F1-R1 end-to-end: a real gate journal -> discover -> classify -> deliver once."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.outbox = CallbackOutbox(path=Path(self._tmp.name) / "wf.sqlite")

    def test_gate_journal_with_marker_delivers_a_coordinator_callback(self):
        source = _FakeSource(
            {"13543": [RedmineJournalEntry("13543", "75212", f"review {render_workflow_event_marker('review_request')}")]}
        )
        proc = CallbackOutboxProcessor(self.outbox, source)
        cands = discover_candidates(source, "13543")
        report = run_once(proc, lambda row: SEND_DELIVERED, candidates=cands, stale_seconds=0)
        self.assertEqual([d["journal"] for d in report["deliver"]["delivered"]], ["75212"])
        row = self.outbox.read()[0]
        self.assertEqual(
            (row.normalized_gate, row.callback_route, row.state),
            ("review_request", "coordinator", CALLBACK_DELIVERED),
        )

    def test_rediscovery_is_idempotent(self):
        source = _FakeSource(
            {"13543": [RedmineJournalEntry("13543", "75212", f"review {render_workflow_event_marker('review_request')}")]}
        )
        proc = CallbackOutboxProcessor(self.outbox, source)
        cands = discover_candidates(source, "13543")
        run_once(proc, lambda row: SEND_DELIVERED, candidates=cands, stale_seconds=0)
        # Re-discovering the same gate on a later pass enqueues no new row and re-sends nothing.
        report2 = run_once(proc, lambda row: SEND_DELIVERED, candidates=discover_candidates(source, "13543"), stale_seconds=0)
        self.assertEqual(report2["ingest"]["enqueued"], 0)
        self.assertEqual(report2["deliver"]["delivered"], [])
        self.assertEqual(len(self.outbox.read()), 1)


class WatchTest(unittest.TestCase):
    def test_watch_runs_one_pass_per_wake_bounded(self):
        passes = []
        result = watch(lambda: True, lambda: passes.append(1) or {"deliver": {"delivered": []}}, max_passes=3)
        self.assertEqual(len(passes), 3)  # bounded to max_passes
        self.assertEqual([r["wake"] for r in result], ["woke", "woke", "woke"])

    def test_watch_runs_a_pass_even_on_wake_timeout_or_error(self):
        # A herdr timeout (falsy) and a wait error both still run a pass (Redmine is authority).
        seen = []
        watch(lambda: False, lambda: seen.append("timeout-pass") or {}, max_passes=1)

        def boom():
            raise RuntimeError("cli event stream dropped")

        watch(boom, lambda: seen.append("error-pass") or {}, max_passes=1)
        self.assertEqual(seen, ["timeout-pass", "error-pass"])

    def test_watch_survives_a_raising_pass(self):
        # #13520 review F1b (background lifecycle): a pass that raises (transient Redmine/store
        # error) is caught and recorded, and the watcher survives to its next bounded wake.
        seen = []

        def flaky_pass():
            seen.append(1)
            if len(seen) == 1:
                raise RuntimeError("transient redmine read error")
            return {"deliver": {"delivered": []}}

        result = watch(lambda: True, flaky_pass, max_passes=2)
        self.assertEqual(len(seen), 2)  # the loop did NOT crash on the first raising pass
        self.assertEqual(result[0]["pass"], {"error": "RuntimeError"})
        self.assertEqual(result[1]["pass"], {"deliver": {"delivered": []}})

    def test_watch_zero_passes_is_noop(self):
        ran = []
        watch(lambda: True, lambda: ran.append(1) or {}, max_passes=0)
        self.assertEqual(ran, [])


if __name__ == "__main__":
    unittest.main()
