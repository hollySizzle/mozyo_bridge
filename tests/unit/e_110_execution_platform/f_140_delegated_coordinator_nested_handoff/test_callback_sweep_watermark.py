"""Unit tests for the dispatch-anchored callback-sweep watermark (Redmine #13889).

Pins the #13883 evidence as deterministic fixtures (acceptance 6) using the journals' **real
recorded shape**. Both evidence gates are PROSE-ONLY — j#79995 (``## Gate: review —
changes_requested``) and j#80002 (``## Gate: review_finding_verdict``) carry no ``[mozyo:...]``
marker (verified against the live #13883 record; review j#80105 F2, verdict j#80112). An earlier
revision of these tests injected markers those journals never had, so they passed while the real
defect survived. They now assert the two shapes separately:

- **as recorded today** (prose) the sweep cannot classify the gate, so it must ABSTAIN — declaring
  a stall here is the stale replay itself, and the fence would not stop the first one;
- **as recorded through the canonical writer** (marker-bearing) the sweep sees the gate first-pass
  and resolves ``progress_without_callback``.

Ordering is by durable journal id throughout, never wall-clock — the 8-second gap in the evidence
is precisely what a clock cutoff cannot resolve.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (
    PROGRESS_BEARING_KINDS,
    SEND_RESERVED,
    SWEEP_STATE_ANCHOR_MISSING,
    SWEEP_STATE_STALL_UNPROVABLE,
    ZERO_SEND_DISPATCH_ROUND_CHANGED,
    ZERO_SEND_NOT_A_STALL,
    ZERO_SEND_PROGRESS_LANDED,
    ZERO_SEND_STALL_UNPROVABLE,
    classify_sweep,
    decide_recovery,
    opaque_entries_after,
    progress_entries_after,
    render_progress_note,
    resolve_watermark,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    GATE_BEARING_KINDS,
    RedmineJournalEntry,
    dispatch_generations,
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


def entry(jid, notes):
    return RedmineJournalEntry(issue_id="13883", journal_id=str(jid), notes=notes)


def ir(jid, *, lane=LANE, generation=GEN):
    """The canonical Implementation Request journal (the dispatch anchor)."""
    return entry(jid, render_dispatch_note("## IR", lane=lane, lane_generation=generation))


def gate(jid, kind, **fields):
    """A callback-required gate journal recorded through the canonical marker-bearing writer."""
    return entry(jid, render_gate_note(kind, body=f"## Gate: {kind}", **fields))


def progress(jid, kind, *, lane=LANE, generation=GEN):
    """A worker-side progress journal recorded through the canonical marker-bearing writer."""
    return entry(
        jid,
        render_progress_note(kind, lane=lane, lane_generation=generation, body=f"## Gate: {kind}"),
    )


def wm(entries, anchor, *, lane=LANE, generation=GEN, latest=GEN):
    return resolve_watermark(
        entries, dispatch_journal=anchor, lane=lane, lane_generation=generation,
        latest_generation=latest,
    )


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
            render_progress_note("review_request", lane=LANE, lane_generation=GEN)

    def test_an_unscoped_progress_marker_cannot_be_rendered(self):
        # Review F3: an unscoped marker cannot be attributed to a dispatch round, so the producer
        # refuses to emit one rather than emit an ambiguous token.
        with self.assertRaises(ValueError):
            render_progress_note("progress_log", lane="", lane_generation=GEN)
        with self.assertRaises(ValueError):
            render_progress_note("progress_log", lane=LANE, lane_generation="")


class OrderedJournalIdTest(unittest.TestCase):
    """Acceptance 1/2: the before/after test is an ordered durable journal id, never a clock."""

    def test_progress_is_measured_strictly_after_the_dispatch_anchor(self):
        entries = [
            gate("79000", "implementation_done"),  # a PRIOR round's gate: before the anchor
            ir("79990"),
            progress("79995", "review_finding_verdict"),
        ]
        self.assertEqual(
            progress_entries_after(entries, after_journal="79990", lane=LANE, lane_generation=GEN),
            (("79995", "review_finding_verdict"),),
        )

    def test_a_gate_at_the_anchor_itself_is_not_progress(self):
        self.assertEqual(
            progress_entries_after([ir("79990")], after_journal="79990", lane=LANE, lane_generation=GEN),
            (),
        )

    def test_ordering_is_numeric_not_lexicographic(self):
        # "9999" < "10000" numerically but sorts AFTER lexicographically; an id compare must not
        # silently degrade to string ordering.
        entries = [ir("9999"), progress("10000", "progress_log")]
        self.assertEqual(
            progress_entries_after(entries, after_journal="9999", lane=LANE, lane_generation=GEN),
            (("10000", "progress_log"),),
        )

    def test_blank_anchor_yields_an_unanchored_watermark_not_no_progress(self):
        # Fail-closed: an unanchored sweep must abstain, never baseline on a fabricated 0 (which
        # would make every journal on the issue look like post-dispatch progress).
        w = wm([progress("79995", "progress_log")], "")
        self.assertFalse(w.anchored)
        self.assertFalse(w.has_progress)
        self.assertEqual(classify_sweep(watermark=w)["state"], SWEEP_STATE_ANCHOR_MISSING)
        self.assertFalse(classify_sweep(watermark=w)["is_stall"])


class ChannelScopingTest(unittest.TestCase):
    """A progress kind only counts on the workflow-event channel, never as a handoff pointer.

    The self-counting hazard: the coordinator's own notification journals are newer entries on the
    same issue, authored by the SAME Redmine user as the worker (every #13889 evidence journal is
    author id 5), so identity cannot separate them. A handoff marker is a *pointer to* a gate, not
    the gate landing.
    """

    def test_a_handoff_channel_progress_kind_is_not_counted(self):
        pointer = entry(
            "79995",
            "[mozyo:handoff:source=redmine:issue=13883:journal=79990:"
            "kind=review_finding_verdict:to=codex]",
        )
        self.assertEqual(
            progress_entries_after([ir("79990"), pointer], after_journal="79990", lane=LANE, lane_generation=GEN),
            (),
        )

    def test_a_recognized_pointer_is_not_opaque_either(self):
        # It is understood (just not progress), so it must not force an abstention.
        pointer = entry("79995", "[mozyo:handoff:source=redmine:issue=13883:journal=79990:kind=reply:to=codex]")
        self.assertEqual(opaque_entries_after([ir("79990"), pointer], after_journal="79990"), ())


class RoundScopingTest(unittest.TestCase):
    """Review F3: progress and anchors are scoped to a dispatch round, and a supersede is detectable."""

    def test_progress_from_a_newer_generation_does_not_count_for_the_older_round(self):
        entries = [ir("100", generation=1), ir("200", generation=2), progress("201", "progress_log", generation=2)]
        self.assertEqual(
            progress_entries_after(entries, after_journal="100", lane=LANE, lane_generation=1), ()
        )
        self.assertEqual(
            progress_entries_after(entries, after_journal="200", lane=LANE, lane_generation=2),
            (("201", "progress_log"),),
        )

    def test_dispatch_generations_reports_every_round_without_fixing_one(self):
        # The round authority: resolve_dispatch_entry_journal fixes a generation and so can never
        # reveal that a newer round opened.
        entries = [ir("100", generation=1), ir("200", generation=2)]
        self.assertEqual(dispatch_generations(entries, lane=LANE), (1, 2))
        self.assertEqual(resolve_dispatch_entry_journal(entries, lane=LANE, lane_generation=1), "100")

    def test_a_newer_generation_marks_the_watermark_superseded(self):
        w = wm([ir("100", generation=1), ir("200", generation=2)], "100", generation=1, latest=2)
        self.assertTrue(w.superseded)


class Evidence79995Test(unittest.TestCase):
    """#13883 evidence 1: the review gate landed 8 SECONDS before the sweep. It is PROSE-ONLY."""

    def real_entries(self):
        # j#79995 2026-07-16T09:28:53Z — verbatim shape: a heading, no marker.
        return [ir("79990"), entry("79995", "## Gate: review — changes_requested\n\n(prose body)")]

    def test_as_recorded_the_gate_is_opaque_so_the_sweep_abstains(self):
        # The real record: the sweep cannot see the gate, so it must NOT declare a stall. Declaring
        # one is what produced j#79996 (and the recovery mutation after it).
        w = wm(self.real_entries(), "79990")
        self.assertFalse(w.has_progress)
        self.assertEqual(w.opaque, ("79995",))
        self.assertFalse(w.stall_provable)
        verdict = classify_sweep(watermark=w, callback=CALLBACK_SAME_LANE_ONLY)
        self.assertEqual(verdict["state"], SWEEP_STATE_STALL_UNPROVABLE)
        self.assertFalse(verdict["is_stall"])
        self.assertNotEqual(verdict["state"], STATE_NO_PROGRESS_AFTER_HANDOFF)

    def test_a_stale_verdict_that_races_the_prose_gate_is_zero_send(self):
        # The TOCTOU window on the REAL shape: the recovery mutation must not fire.
        decided = wm([ir("79990")], "79990")
        rechecked = wm(self.real_entries(), "79990")
        decision = decide_recovery(
            decided=decided, rechecked=rechecked, decided_state=STATE_NO_PROGRESS_AFTER_HANDOFF
        )
        self.assertTrue(decision.zero_send)
        self.assertEqual(decision.reason, ZERO_SEND_STALL_UNPROVABLE)
        self.assertIn("79995", decision.detail)

    def test_recorded_through_the_canonical_writer_it_resolves_first_pass(self):
        # Once the producer is wired, the same landing is classified — no correction journal.
        entries = [ir("79990"), gate("79995", "review_result", conclusion="changes_requested")]
        verdict = classify_sweep(watermark=wm(entries, "79990"), callback=CALLBACK_SAME_LANE_ONLY)
        self.assertEqual(verdict["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertEqual(verdict["progress_journals"], [{"journal": "79995", "kind": "review_result"}])


class Evidence80002Test(unittest.TestCase):
    """#13883 evidence 2: the worker verdict gate landed, then the review was replayed. PROSE-ONLY."""

    def real_entries(self):
        # j#80002 2026-07-16T09:32:20Z — verbatim shape: a heading, no marker.
        return [ir("80000"), entry("80002", "## Gate: review_finding_verdict\n\n(prose body)")]

    def test_as_recorded_the_verdict_gate_is_opaque_so_no_replay_fires(self):
        # j#80005 declared a forward stall and j#80006 replayed the review to the worker. On the
        # real shape the sweep abstains, so the replay never happens.
        w = wm(self.real_entries(), "80000")
        self.assertEqual(w.opaque, ("80002",))
        verdict = classify_sweep(watermark=w, callback=CALLBACK_SAME_LANE_ONLY)
        self.assertEqual(verdict["state"], SWEEP_STATE_STALL_UNPROVABLE)
        decision = decide_recovery(
            decided=w, rechecked=w, decided_state=verdict["state"]
        )
        self.assertTrue(decision.zero_send)

    def test_recorded_through_the_canonical_writer_it_counts_as_progress(self):
        entries = [ir("80000"), progress("80002", "review_finding_verdict")]
        w = wm(entries, "80000")
        self.assertTrue(w.has_progress)
        self.assertEqual(w.latest_progress_journal, "80002")
        verdict = classify_sweep(watermark=w, callback=CALLBACK_SAME_LANE_ONLY)
        self.assertEqual(verdict["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertTrue(
            decide_recovery(decided=w, rechecked=w, decided_state=verdict["state"]).zero_send
        )


class GenuineStallTest(unittest.TestCase):
    """The guard must not fail OPEN: a provably silent lane still classifies and still sends."""

    def test_a_genuinely_silent_lane_is_still_a_no_progress_stall(self):
        w = wm([ir("79990")], "79990")
        self.assertTrue(w.stall_provable)
        verdict = classify_sweep(watermark=w, callback=CALLBACK_ABSENT)
        self.assertEqual(verdict["state"], STATE_NO_PROGRESS_AFTER_HANDOFF)
        self.assertTrue(verdict["is_stall"])

    def test_a_still_silent_lane_clears_the_pre_mutation_recheck(self):
        w = wm([ir("79990")], "79990")
        decision = decide_recovery(
            decided=w, rechecked=w, decided_state=STATE_NO_PROGRESS_AFTER_HANDOFF
        )
        self.assertTrue(decision.send)
        self.assertEqual(decision.reason, SEND_RESERVED)

    def test_a_landed_marker_bearing_gate_zero_sends_as_not_a_stall(self):
        w = wm([ir("79990"), gate("79995", "implementation_done")], "79990")
        verdict = classify_sweep(watermark=w, callback=CALLBACK_SAME_LANE_ONLY)
        decision = decide_recovery(decided=w, rechecked=w, decided_state=verdict["state"])
        self.assertTrue(decision.zero_send)
        self.assertEqual(decision.reason, ZERO_SEND_NOT_A_STALL)

    def test_progress_landing_in_the_window_is_zero_send(self):
        decided = wm([ir("79990")], "79990")
        rechecked = wm([ir("79990"), progress("79995", "progress_log")], "79990")
        decision = decide_recovery(
            decided=decided, rechecked=rechecked, decided_state=STATE_NO_PROGRESS_AFTER_HANDOFF
        )
        self.assertTrue(decision.zero_send)
        self.assertEqual(decision.reason, ZERO_SEND_PROGRESS_LANDED)

    def test_a_superseded_dispatch_round_is_zero_send(self):
        decided = wm([ir("100")], "100", generation=1, latest=1)
        rechecked = wm([ir("100"), ir("200", generation=2)], "100", generation=1, latest=2)
        decision = decide_recovery(
            decided=decided, rechecked=rechecked, decided_state=STATE_NO_PROGRESS_AFTER_HANDOFF
        )
        self.assertTrue(decision.zero_send)
        self.assertEqual(decision.reason, ZERO_SEND_DISPATCH_ROUND_CHANGED)


if __name__ == "__main__":
    unittest.main()
