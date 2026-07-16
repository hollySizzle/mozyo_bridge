"""Integration tests for the fenced callback sweep (Redmine #13889 acceptance 2/3/5).

Drives :func:`...application.callback_sweep.sweep_once` against a real
:class:`...dispatch_outbox_fence.DispatchOutboxFence` (a temp-home SQLite store) and a journal
source whose snapshot can CHANGE between the decision read and the pre-mutation re-read — the
exact #13883 race. Asserts the recovery send fires at most once per dispatch anchor and never at
all once a qualifying gate has landed.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    FENCE_DELIVERED,
    FENCE_UNCERTAIN,
    FenceKey,
    dispatch_outbox_fence_path,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_sweep import (
    SWEEP_SOURCE_UNREADABLE,
    sweep_once,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (
    SEND_RESERVED,
    SWEEP_RECOVERY_ACTION_ID,
    ZERO_SEND_FENCE_HELD,
    ZERO_SEND_FENCE_UNAVAILABLE,
    ZERO_SEND_PROGRESS_LANDED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (
    render_progress_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    render_dispatch_note,
    render_gate_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_callback import (
    STATE_NO_PROGRESS_AFTER_HANDOFF,
    STATE_PROGRESS_WITHOUT_CALLBACK,
)

WS = "ws-1"
LANE = "issue_13883_lane"
ISSUE = "13883"
GEN = 1
TARGET = "claude-worker-1"


def ir(jid):
    return RedmineJournalEntry(
        issue_id=ISSUE,
        journal_id=str(jid),
        notes=render_dispatch_note("## Implementation Request", lane=LANE, lane_generation=GEN),
    )


def gate(jid, kind, **fields):
    return RedmineJournalEntry(
        issue_id=ISSUE, journal_id=str(jid), notes=render_gate_note(kind, body="## Gate", **fields)
    )


def progress(jid, kind):
    return RedmineJournalEntry(
        issue_id=ISSUE, journal_id=str(jid), notes=render_progress_note(kind, body="## Gate")
    )


class RaceSource:
    """A journal source whose snapshot can advance between reads (the TOCTOU window).

    ``lands_on_read`` injects an entry *after* the Nth read has been served, reproducing a gate
    landing between the sweep's decision read and its pre-mutation re-read.
    """

    def __init__(self, entries, *, lands_on_read=None, land=None, raises_on_read=None):
        self._entries = list(entries)
        self._lands_on_read = lands_on_read
        self._land = land
        self._raises_on_read = raises_on_read
        self.reads = 0

    def read_entries(self, issue_id):
        self.reads += 1
        if self._raises_on_read == self.reads:
            raise RuntimeError("redmine read failed")
        served = list(self._entries)
        if self._lands_on_read == self.reads and self._land is not None:
            self._entries.append(self._land)
        return served


class SweepFenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.fence = DispatchOutboxFence(home=self.home)
        self.fence.bootstrap()
        self.sends = []
        self.addCleanup(self._tmp.cleanup)

    def send(self):
        self.sends.append("recovery")

    def sweep(self, source, **kw):
        return sweep_once(
            workspace_id=WS,
            lane_id=LANE,
            issue=ISSUE,
            lane_generation=GEN,
            source=source,
            fence=self.fence,
            target_assigned_name=TARGET,
            send_fn=self.send,
            **kw,
        )

    def anchor_key(self, journal="79990"):
        return FenceKey(
            workspace_id=WS,
            lane_id=LANE,
            issue=ISSUE,
            journal=journal,
            action_id=SWEEP_RECOVERY_ACTION_ID,
            target_assigned_name=TARGET,
        )

    def test_genuine_stall_sends_exactly_once_and_marks_the_fence_delivered(self):
        source = RaceSource([ir("79990")])
        result = self.sweep(source)
        self.assertEqual(result["state"], STATE_NO_PROGRESS_AFTER_HANDOFF)
        self.assertTrue(result["sent"])
        self.assertEqual(result["send_reason"], SEND_RESERVED)
        self.assertEqual(self.sends, ["recovery"])
        self.assertEqual(self.fence.state_of(self.anchor_key()), FENCE_DELIVERED)

    def test_the_sweep_re_reads_before_mutating(self):
        # Acceptance 2: the decision read and the pre-mutation re-check are SEPARATE durable reads.
        source = RaceSource([ir("79990")])
        self.sweep(source)
        self.assertEqual(source.reads, 2)

    def test_gate_landing_in_the_toctou_window_is_zero_send(self):
        # The #13883 evidence race: the decision read sees a silent lane; the review gate lands;
        # the re-read sees it. No recovery mutation, and the verdict is corrected FIRST-PASS.
        source = RaceSource(
            [ir("79990")],
            lands_on_read=1,
            land=gate("79995", "review_result", conclusion="changes_requested"),
        )
        result = self.sweep(source)
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_PROGRESS_LANDED)
        self.assertEqual(result["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertEqual(self.sends, [])
        # Nothing was reserved, so a later legitimate round is not fenced out by this one.
        self.assertEqual(self.fence.state_of(self.anchor_key()), "absent")

    def test_worker_verdict_landing_in_the_window_is_zero_send(self):
        # Evidence 2 end-to-end: j#80002 review_finding_verdict lands mid-sweep -> no replay.
        source = RaceSource(
            [ir("79990")],
            lands_on_read=1,
            land=progress("80002", "review_finding_verdict"),
        )
        result = self.sweep(source)
        self.assertFalse(result["sent"])
        self.assertEqual(result["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertEqual(result["progress_journals"], [{"journal": "80002", "kind": "review_finding_verdict"}])
        self.assertEqual(self.sends, [])

    def test_recovery_is_at_most_once_per_gate_anchor(self):
        # Acceptance 5: a repeat sweep of the SAME still-silent lane must not replay.
        first = self.sweep(RaceSource([ir("79990")]))
        self.assertTrue(first["sent"])
        second = self.sweep(RaceSource([ir("79990")]))
        self.assertFalse(second["sent"])
        self.assertEqual(second["send_reason"], ZERO_SEND_FENCE_HELD)
        self.assertEqual(self.sends, ["recovery"])  # still exactly one delivery

    def test_a_new_dispatch_round_gets_its_own_recovery_budget(self):
        # The fence keys on the dispatch anchor, so a genuinely NEW round is not starved by the
        # previous round's delivery.
        self.sweep(RaceSource([ir("79990")]))
        result = self.sweep(RaceSource([ir("80100")]))
        self.assertTrue(result["sent"])
        self.assertEqual(self.sends, ["recovery", "recovery"])

    def test_unbootstrapped_fence_is_zero_send(self):
        # The idempotency authority is unavailable -> refuse to send rather than risk a duplicate.
        bare = DispatchOutboxFence(home=Path(tempfile.mkdtemp()))
        result = sweep_once(
            workspace_id=WS,
            lane_id=LANE,
            issue=ISSUE,
            lane_generation=GEN,
            source=RaceSource([ir("79990")]),
            fence=bare,
            target_assigned_name=TARGET,
            send_fn=self.send,
        )
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_FENCE_UNAVAILABLE)
        self.assertEqual(self.sends, [])

    def test_a_raising_send_marks_the_fence_uncertain_and_never_auto_retries(self):
        def boom():
            raise RuntimeError("transport died")

        result = sweep_once(
            workspace_id=WS,
            lane_id=LANE,
            issue=ISSUE,
            lane_generation=GEN,
            source=RaceSource([ir("79990")]),
            fence=self.fence,
            target_assigned_name=TARGET,
            send_fn=boom,
        )
        self.assertFalse(result["sent"])
        self.assertTrue(result["needs_reconcile"])
        self.assertEqual(self.fence.state_of(self.anchor_key()), FENCE_UNCERTAIN)
        # A follow-up sweep must NOT auto-retry an ambiguous send.
        again = self.sweep(RaceSource([ir("79990")]))
        self.assertFalse(again["sent"])
        self.assertEqual(again["send_reason"], ZERO_SEND_FENCE_HELD)

    def test_unreadable_source_abstains_without_mutating(self):
        result = self.sweep(RaceSource([ir("79990")], raises_on_read=1))
        self.assertEqual(result["state"], SWEEP_SOURCE_UNREADABLE)
        self.assertFalse(result["sent"])
        self.assertEqual(self.sends, [])

    def test_unreadable_recheck_abstains_without_mutating(self):
        # The decision read succeeded and said "stall", but the re-read failed: the premise cannot
        # be re-verified, so the mutation must not fire.
        result = self.sweep(RaceSource([ir("79990")], raises_on_read=2))
        self.assertEqual(result["state"], SWEEP_SOURCE_UNREADABLE)
        self.assertFalse(result["sent"])
        self.assertEqual(self.sends, [])

    def test_read_only_preview_never_reserves_the_fence(self):
        result = sweep_once(
            workspace_id=WS,
            lane_id=LANE,
            issue=ISSUE,
            lane_generation=GEN,
            source=RaceSource([ir("79990")]),
            fence=self.fence,
            target_assigned_name=TARGET,
            send_fn=None,
        )
        self.assertEqual(result["state"], STATE_NO_PROGRESS_AFTER_HANDOFF)
        self.assertFalse(result["sent"])
        self.assertEqual(self.fence.state_of(self.anchor_key()), "absent")


if __name__ == "__main__":
    unittest.main()
