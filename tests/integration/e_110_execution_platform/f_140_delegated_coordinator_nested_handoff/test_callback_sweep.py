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
    SWEEP_STATE_STALL_UNPROVABLE,
    ZERO_SEND_DISPATCH_ROUND_CHANGED,
    ZERO_SEND_FENCE_HELD,
    ZERO_SEND_FENCE_UNAVAILABLE,
    ZERO_SEND_PROGRESS_LANDED,
    ZERO_SEND_STALL_UNPROVABLE,
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
    CALLBACK_SAME_LANE_ONLY,
    STATE_NO_PROGRESS_AFTER_HANDOFF,
    STATE_PROGRESS_WITHOUT_CALLBACK,
)

WS = "ws-1"
LANE = "issue_13883_lane"
ISSUE = "13883"
GEN = 1
TARGET = "claude-worker-1"


def entry(jid, notes):
    return RedmineJournalEntry(issue_id=ISSUE, journal_id=str(jid), notes=notes)


def ir(jid, generation=GEN):
    return entry(jid, render_dispatch_note("## Implementation Request", lane=LANE, lane_generation=generation))


def gate(jid, kind, **fields):
    return entry(jid, render_gate_note(kind, body="## Gate", **fields))


def progress(jid, kind, generation=GEN):
    return entry(
        jid, render_progress_note(kind, lane=LANE, lane_generation=generation, body="## Gate")
    )


def prose(jid, heading):
    """A journal in the REAL #13883 shape: a gate heading with no structured marker."""
    return entry(jid, f"{heading}\n\n(prose body)")


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

    def test_prose_only_gate_landing_in_the_window_is_zero_send(self):
        # Review j#80105 F2 / verdict j#80112: the REAL #13883 j#80002 carries no marker. A
        # marker-only reader sees nothing there, and the re-read is equally blind — so before this
        # correction the sweep sent the stale replay exactly once and the fence never saw it. The
        # sweep must ABSTAIN on an unreadable record instead of asserting a stall it cannot prove.
        source = RaceSource(
            [ir("79990")],
            lands_on_read=1,
            land=prose("80002", "## Gate: review_finding_verdict"),
        )
        result = self.sweep(source)
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_STALL_UNPROVABLE)
        self.assertEqual(result["state"], SWEEP_STATE_STALL_UNPROVABLE)
        self.assertFalse(result["is_stall"])
        self.assertEqual(result["opaque_journals"], ["80002"])
        self.assertEqual(self.sends, [])
        self.assertEqual(self.fence.state_of(self.anchor_key()), "absent")

    def test_prose_only_gate_present_from_the_start_is_never_a_stall_verdict(self):
        # The same real shape, already on the record at the decision read (the j#79995 case).
        result = self.sweep(RaceSource([ir("79990"), prose("79995", "## Gate: review — changes_requested")], ))
        self.assertFalse(result["sent"])
        self.assertEqual(result["state"], SWEEP_STATE_STALL_UNPROVABLE)
        self.assertEqual(self.sends, [])

    def test_a_new_dispatch_round_landing_in_the_window_is_zero_send(self):
        # Review F3, through the REAL read path: read_watermark resolves the round authority from
        # the source, so a generation-2 IR landing mid-sweep supersedes the generation-1 verdict.
        # The previous anchor-vs-anchor check could never fire here — both reads resolve the same
        # caller-fixed generation and so always agreed.
        source = RaceSource([ir("100", generation=1)], lands_on_read=1, land=ir("200", generation=2))
        result = self.sweep(source)
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_DISPATCH_ROUND_CHANGED)
        self.assertEqual(self.sends, [])

    def test_progress_from_a_newer_round_does_not_clear_an_older_rounds_stall(self):
        # The inverse fail-open: generation 2's progress must not make generation 1 look alive.
        source = RaceSource([ir("100", generation=1), ir("200", generation=2),
                             progress("201", "progress_log", generation=2)])
        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=2,
            source=source, fence=self.fence, target_assigned_name=TARGET, send_fn=self.send,
            callback=CALLBACK_SAME_LANE_ONLY,
        )
        # Sweeping generation 2 itself: its own progress is visible, so it is not a stall.
        self.assertEqual(result["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertFalse(result["sent"])
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


class ProducerWiringTest(unittest.TestCase):
    """Review F2: the progress marker needs a PRODUCTION writer, or the reader finds nothing real.

    This is the #13520 F1a gap repeated: a reader whose markers nothing in production writes sees
    an empty issue. The writer closes producer -> Redmine journal -> sweep classify end-to-end.
    """

    def setUp(self):
        self.posted = []

    class _FakeTransport:
        def __init__(self, sink):
            self.sink = sink

        def post_issue_note(self, issue_id, notes):
            self.sink.append((issue_id, notes))
            return f"redmine:issue={issue_id}"

    def test_emitted_progress_record_is_discoverable_by_the_sweep(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_gate_record import (
            emit_progress_record,
        )

        receipt = emit_progress_record(
            ISSUE, "review_finding_verdict", lane=LANE, lane_generation=GEN,
            body="## Gate: review_finding_verdict",
            transport=self._FakeTransport(self.posted),
        )
        self.assertTrue(receipt.recorded)
        # The producer's own output, read back by the consumer: the loop closes.
        written = entry("80002", self.posted[0][1])
        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=RaceSource([ir("80000"), written]),
            fence=DispatchOutboxFence(home=Path(tempfile.mkdtemp())),
            target_assigned_name=TARGET, send_fn=None, callback=CALLBACK_SAME_LANE_ONLY,
        )
        self.assertEqual(result["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertEqual(result["progress_journals"], [{"journal": "80002", "kind": "review_finding_verdict"}])

    def test_writer_is_fail_closed_when_the_optin_is_unset(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_gate_record import (
            GATE_RECORD_WRITE_OPTIN_UNSET,
            emit_progress_record,
        )

        receipt = emit_progress_record(
            ISSUE, "progress_log", lane=LANE, lane_generation=GEN, transport=None
        )
        self.assertFalse(receipt.recorded)
        self.assertEqual(receipt.reason, GATE_RECORD_WRITE_OPTIN_UNSET)


class RecoverySenderTest(unittest.TestCase):
    """Review F1: the production send_fn, verified through its injected runner (no external send)."""

    def test_sender_issues_one_handoff_send_naming_the_durable_anchor(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_sweep import (
            build_recovery_sender,
        )

        calls = []
        send = build_recovery_sender(
            issue=ISSUE, journal="79990", target="w1:p2",
            runner=lambda argv: (calls.append(argv), (0, ""))[1],
        )
        send()
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[1:3], ["handoff", "send"])
        # The notification is a pointer: it must name the durable anchor, not carry the content.
        self.assertIn("--issue", argv)
        self.assertEqual(argv[argv.index("--issue") + 1], ISSUE)
        self.assertEqual(argv[argv.index("--journal") + 1], "79990")
        self.assertEqual(argv[argv.index("--target") + 1], "w1:p2")

    def test_a_failing_send_raises_so_the_fence_records_uncertain(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_sweep import (
            RecoverySendError,
            build_recovery_sender,
        )

        send = build_recovery_sender(
            issue=ISSUE, journal="79990", target="w1:p2",
            runner=lambda argv: (3, "target unresolved"),
        )
        with self.assertRaises(RecoverySendError):
            send()

    def test_the_fenced_path_delivers_exactly_one_send_through_the_real_sender(self):
        # F1 end-to-end: sweep_once + a real fence + the production sender (injected runner).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_sweep import (
            build_recovery_sender,
        )

        home = Path(tempfile.mkdtemp())
        fence = DispatchOutboxFence(home=home)
        fence.bootstrap()
        calls = []
        sender = build_recovery_sender(
            issue=ISSUE, journal="79990", target=TARGET,
            runner=lambda argv: (calls.append(argv), (0, ""))[1],
        )
        for _ in range(3):  # repeated sweeps of the same still-silent round
            sweep_once(
                workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
                source=RaceSource([ir("79990")]), fence=fence,
                target_assigned_name=TARGET, send_fn=sender,
            )
        self.assertEqual(len(calls), 1)  # at most once per dispatch anchor


if __name__ == "__main__":
    unittest.main()
