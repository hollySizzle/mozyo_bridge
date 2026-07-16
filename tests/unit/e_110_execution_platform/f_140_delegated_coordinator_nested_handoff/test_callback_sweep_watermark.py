"""Unit tests for the dispatch-anchored callback-sweep watermark (Redmine #13889).

Pins the #13883 evidence as deterministic fixtures (acceptance 6): the near-simultaneous landings
that made the sweep record ``no_progress_after_handoff`` 8 seconds after a durable gate
(j#79995 -> j#79996) and replay a review result over a landed worker verdict
(j#80002 -> j#80005 -> j#80006). Both are reproduced by durable journal ORDER, not wall-clock — the
ordering the sweep is now required to use.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (
    PROGRESS_BEARING_KINDS,
    SWEEP_STATE_ANCHOR_MISSING,
    ZERO_SEND_DISPATCH_ROUND_CHANGED,
    ZERO_SEND_NOT_A_STALL,
    ZERO_SEND_PROGRESS_LANDED,
    SEND_RESERVED,
    classify_sweep,
    decide_recovery,
    progress_entries_after,
    render_progress_note,
    resolve_watermark,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    GATE_BEARING_KINDS,
    RedmineJournalEntry,
    render_dispatch_note,
    render_gate_note,
    resolve_dispatch_entry_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_callback import (
    CALLBACK_ABSENT,
    CALLBACK_SAME_LANE_ONLY,
    STATE_NO_PROGRESS_AFTER_HANDOFF,
    STATE_PROGRESS_WITHOUT_CALLBACK,
)

LANE = "issue_13883_lane"
GEN = 1


def ir(jid):
    """The canonical Implementation Request journal (the dispatch anchor)."""
    return RedmineJournalEntry(
        issue_id="13883",
        journal_id=str(jid),
        notes=render_dispatch_note(
            "## Implementation Request", lane=LANE, lane_generation=GEN
        ),
    )


def gate(jid, kind, **fields):
    """A callback-required gate journal (implementation_done / review_request / review_result)."""
    return RedmineJournalEntry(
        issue_id="13883",
        journal_id=str(jid),
        notes=render_gate_note(kind, body=f"## Gate: {kind}", **fields),
    )


def progress(jid, kind):
    """A worker-side progress journal (review_finding_verdict / progress_log / ...)."""
    return RedmineJournalEntry(
        issue_id="13883",
        journal_id=str(jid),
        notes=render_progress_note(kind, body=f"## Gate: {kind}"),
    )


def prose(jid, text):
    """A prose-only journal: no structured token, so it is never counted (never guessed)."""
    return RedmineJournalEntry(issue_id="13883", journal_id=str(jid), notes=text)


class VocabularyBoundaryTest(unittest.TestCase):
    """The progress vocabulary is SEPARATE from the callback-required one (#13758 R5-F3 precedent)."""

    def test_progress_kinds_do_not_widen_the_callback_required_gate_vocabulary(self):
        # Widening GATE_BEARING_KINDS would turn every worker verdict into a coordinator wake —
        # the duplicate-notification failure this issue is about.
        self.assertEqual(PROGRESS_BEARING_KINDS & GATE_BEARING_KINDS, frozenset())

    def test_review_finding_verdict_is_progress_but_owes_no_callback(self):
        self.assertIn("review_finding_verdict", PROGRESS_BEARING_KINDS)
        self.assertNotIn("review_finding_verdict", GATE_BEARING_KINDS)

    def test_render_progress_marker_rejects_a_callback_required_gate(self):
        with self.assertRaises(ValueError):
            render_progress_note("review_request")


class OrderedJournalIdTest(unittest.TestCase):
    """Acceptance 1/2: the before/after test is an ordered durable journal id, never a clock."""

    def test_progress_is_measured_strictly_after_the_dispatch_anchor(self):
        entries = [
            gate("79000", "implementation_done"),  # a PRIOR round's gate: before the anchor
            ir("79990"),
            progress("79995", "review_finding_verdict"),
        ]
        found = progress_entries_after(entries, after_journal="79990")
        self.assertEqual(found, (("79995", "review_finding_verdict"),))

    def test_a_gate_at_the_anchor_itself_is_not_progress(self):
        # Strictly-after: the dispatch journal is the baseline, not progress past it.
        self.assertEqual(progress_entries_after([ir("79990")], after_journal="79990"), ())

    def test_prose_only_journal_is_never_counted_as_progress(self):
        entries = [ir("79990"), prose("79995", "## Gate: review — changes_requested")]
        self.assertEqual(progress_entries_after(entries, after_journal="79990"), ())

    def test_ordering_is_numeric_not_lexicographic(self):
        # "9999" < "10000" numerically but sorts AFTER lexicographically; an id compare must not
        # silently degrade to string ordering.
        entries = [ir("9999"), progress("10000", "progress_log")]
        self.assertEqual(
            progress_entries_after(entries, after_journal="9999"),
            (("10000", "progress_log"),),
        )

    def test_blank_anchor_yields_an_unanchored_watermark_not_no_progress(self):
        # Fail-closed: an unanchored sweep must abstain, never baseline on a fabricated 0 (which
        # would make every journal on the issue look like post-dispatch progress).
        wm = resolve_watermark([progress("79995", "progress_log")], dispatch_journal="")
        self.assertFalse(wm.anchored)
        self.assertFalse(wm.has_progress)
        self.assertEqual(
            classify_sweep(watermark=wm)["state"], SWEEP_STATE_ANCHOR_MISSING
        )
        self.assertFalse(classify_sweep(watermark=wm)["is_stall"])


class ChannelScopingTest(unittest.TestCase):
    """A progress kind only counts on the workflow-event channel, never as a handoff pointer.

    The self-counting hazard this module exists to avoid: the coordinator's own notification
    journals are newer entries on the same issue, authored by the SAME Redmine user as the worker
    (every #13889 evidence journal is author id 5), so identity cannot separate them. A handoff
    marker is a *pointer to* a gate, not the gate landing — counting one would let the sweep read
    its own dispatch notification as the lane's progress and clear a genuine stall.
    """

    def test_a_handoff_channel_progress_kind_is_not_counted(self):
        pointer = RedmineJournalEntry(
            issue_id="13883",
            journal_id="79995",
            notes=(
                "[mozyo:handoff:source=redmine:issue=13883:journal=79990:"
                "kind=review_finding_verdict:to=codex]"
            ),
        )
        self.assertEqual(
            progress_entries_after([ir("79990"), pointer], after_journal="79990"), ()
        )

    def test_a_workflow_event_channel_progress_kind_is_counted(self):
        self.assertEqual(
            progress_entries_after(
                [ir("79990"), progress("79995", "review_finding_verdict")],
                after_journal="79990",
            ),
            (("79995", "review_finding_verdict"),),
        )


class Evidence79995Test(unittest.TestCase):
    """#13883 evidence 1: the review gate landed 8 SECONDS before the sweep and was missed."""

    def entries(self):
        return [
            ir("79990"),
            # j#79995 2026-07-16T09:28:53Z — the durable review result LANDED.
            gate("79995", "review_result", conclusion="changes_requested"),
        ]

    def test_first_pass_is_progress_without_callback_not_no_progress(self):
        # The defect: j#79996 recorded no_progress_after_handoff 8s after j#79995 landed, then
        # j#79999 corrected it to progress_without_callback after the fact. The derived watermark
        # sees the gate on the FIRST pass, so no correction journal exists in the design.
        anchor = resolve_dispatch_entry_journal(
            self.entries(), lane=LANE, lane_generation=GEN
        )
        self.assertEqual(anchor, "79990")
        wm = resolve_watermark(self.entries(), dispatch_journal=anchor)
        verdict = classify_sweep(watermark=wm, callback=CALLBACK_SAME_LANE_ONLY)
        self.assertTrue(wm.has_progress)
        self.assertEqual(verdict["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertNotEqual(verdict["state"], STATE_NO_PROGRESS_AFTER_HANDOFF)
        self.assertEqual(verdict["progress_journals"], [{"journal": "79995", "kind": "review_result"}])

    def test_a_no_progress_verdict_that_races_a_landing_gate_is_zero_send(self):
        # The TOCTOU window itself: the decision read saw nothing, the gate landed, the re-read
        # sees it. The recovery mutation (which produced the duplicate replay) must not fire.
        decided = resolve_watermark([ir("79990")], dispatch_journal="79990")
        rechecked = resolve_watermark(self.entries(), dispatch_journal="79990")
        decision = decide_recovery(
            decided=decided,
            rechecked=rechecked,
            decided_state=STATE_NO_PROGRESS_AFTER_HANDOFF,
        )
        self.assertTrue(decision.zero_send)
        self.assertEqual(decision.reason, ZERO_SEND_PROGRESS_LANDED)
        self.assertIn("79995", decision.detail)


class Evidence80002Test(unittest.TestCase):
    """#13883 evidence 2: the worker verdict gate landed, the sweep replayed the review anyway."""

    def entries(self):
        return [
            ir("80000"),
            # j#80002 2026-07-16T09:32:20Z — `Gate: review_finding_verdict` LANDED (worker side).
            progress("80002", "review_finding_verdict"),
        ]

    def test_worker_verdict_gate_counts_as_progress(self):
        # The root of evidence 2: review_finding_verdict is in NO marker vocabulary before this
        # change, so anchoring alone could never have seen it. It must count as progress.
        wm = resolve_watermark(self.entries(), dispatch_journal="80000")
        self.assertTrue(wm.has_progress)
        self.assertEqual(wm.latest_progress_journal, "80002")

    def test_no_duplicate_replay_after_the_verdict_landed(self):
        # j#80005 declared a forward stall and j#80006 replayed the review result to the worker;
        # j#80013 corrected it after the fact. The verdict is progress_without_callback first-pass.
        wm = resolve_watermark(self.entries(), dispatch_journal="80000")
        verdict = classify_sweep(watermark=wm, callback=CALLBACK_SAME_LANE_ONLY)
        self.assertEqual(verdict["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        decision = decide_recovery(
            decided=wm, rechecked=wm, decided_state=verdict["state"]
        )
        self.assertTrue(decision.zero_send)
        self.assertEqual(decision.reason, ZERO_SEND_NOT_A_STALL)


class GenuineStallTest(unittest.TestCase):
    """The guard must not fail OPEN: a real stall still classifies and still sends."""

    def test_a_genuinely_silent_lane_is_still_a_no_progress_stall(self):
        wm = resolve_watermark([ir("79990")], dispatch_journal="79990")
        verdict = classify_sweep(watermark=wm, callback=CALLBACK_ABSENT)
        self.assertEqual(verdict["state"], STATE_NO_PROGRESS_AFTER_HANDOFF)
        self.assertTrue(verdict["is_stall"])

    def test_a_still_silent_lane_clears_the_pre_mutation_recheck(self):
        wm = resolve_watermark([ir("79990")], dispatch_journal="79990")
        decision = decide_recovery(
            decided=wm, rechecked=wm, decided_state=STATE_NO_PROGRESS_AFTER_HANDOFF
        )
        self.assertTrue(decision.send)
        self.assertEqual(decision.reason, SEND_RESERVED)

    def test_a_superseded_dispatch_round_is_zero_send(self):
        decided = resolve_watermark([ir("79990")], dispatch_journal="79990")
        rechecked = resolve_watermark([ir("80100")], dispatch_journal="80100")
        decision = decide_recovery(
            decided=decided,
            rechecked=rechecked,
            decided_state=STATE_NO_PROGRESS_AFTER_HANDOFF,
        )
        self.assertTrue(decision.zero_send)
        self.assertEqual(decision.reason, ZERO_SEND_DISPATCH_ROUND_CHANGED)


if __name__ == "__main__":
    unittest.main()
