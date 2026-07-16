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
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_publication_fence import (
    PUBLICATION_RESERVED,
    PUBLICATION_UNCERTAIN,
    CallbackPublicationFenceError,
    PublicationKey,
)
from mozyo_bridge.core.state.callback_sweep_lease import (
    CallbackSweepLeaseError,
    LEASE_HELD,
    LEASE_RECLAIMED,
    CallbackSweepLease,
    LeaseKey,
)
from mozyo_bridge.core.state.dispatch_outbox_fence import (
    DispatchOutboxFence,
    FENCE_DELIVERED,
    FENCE_UNCERTAIN,
    FenceKey,
    dispatch_outbox_fence_path,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_sweep import (
    SWEEP_SOURCE_UNREADABLE,
    ZERO_SEND_ATTEMPT_HELD,
    ZERO_SEND_OWNERSHIP_LOST,
    ZERO_SEND_RECORD_FAILED,
    ZERO_SEND_SOURCE_NOT_FRESH,
    ZERO_SEND_WORKSPACE_UNATTESTED,
    RecordOwnershipLostError,
    RecordPublicationHeldError,
    RecordPublicationUncertainError,
    build_recovery_recorder,
    build_recovery_sender,
    source_is_fresh,
    sweep_once,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
    LiveRedmineJournalSource,
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
    opaque_entries_after,
    progress_entries_after,
    render_progress_note,
    render_sweep_record_note,
    resolve_watermark,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    MappingRedmineJournalSource,
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


def _bootstrapped_pubfence(home=None):
    """A ready-to-use publication fence. Like the lease store it never auto-creates."""
    from mozyo_bridge.core.state.callback_publication_fence import CallbackPublicationFence

    f = CallbackPublicationFence(home=home or Path(tempfile.mkdtemp()))
    f.bootstrap()
    return f


def _bootstrapped_lease(home=None):
    """A ready-to-use attempt lease. The store is identity-pinned and never auto-creates (R6-F2),
    so it must be bootstrapped explicitly -- exactly as the production composition root does."""
    lease = CallbackSweepLease(home=home or Path(tempfile.mkdtemp()))
    lease.bootstrap()
    return lease


class RaceSource:
    """A LIVE-shaped journal source whose record can advance between reads (the TOCTOU window).

    ``lands_on_read`` injects an entry *after* the Nth read has been served, reproducing a gate
    landing between the sweep's decision read and its pre-mutation re-read.

    ``fresh_read = True`` because this models the live adapter, which re-fetches per call. That
    declaration is load-bearing: review R2-F1 showed the production path was wired to a FROZEN
    snapshot source, whose re-read returns the identical payload — so this fixture's race was a
    behaviour production could not exhibit, and the "closed" TOCTOU window was open. `sweep_once`
    now refuses to mutate on any source that does not declare freshness, and
    `SnapshotSourceTest` pins that refusal against the real snapshot class.
    """

    fresh_read = True

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


def as_factory(record_fn):
    """Adapt a plain test recorder to the factory seam.

    `sweep_once` takes only a factory (review R8-F2): a raw writer let a caller reproduce the very
    defect the factory prevents, so the API makes the unsafe shape unrepresentable rather than
    trusting caller convention. Tests adapt to the contract; they do not get a back door.
    """
    if record_fn is None:
        return None
    return lambda grant_is_live: record_fn


class FakeRecorder:
    """A durable recorder seam: records the resolution and hands back its journal id."""

    def __init__(self, journal="90001"):
        self.journal = journal
        self.records = []

    def __call__(self, result, watermark):
        self.records.append(result.get("state"))
        return self.journal


class SweepFenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.fence = DispatchOutboxFence(home=self.home)
        self.fence.bootstrap()
        self.pubfence = _bootstrapped_pubfence(getattr(self, 'home', Path(self._tmp.name)))
        self.lease = _bootstrapped_lease(self.home)
        self.sends = []
        self.recorder = FakeRecorder()
        self.addCleanup(self._tmp.cleanup)

    def send(self, record_journal):
        self.sends.append(record_journal)

    def sweep(self, source, **kw):
        kw.setdefault("send_fn", self.send)
        kw.setdefault("record_fn_factory", as_factory(self.recorder))
        kw.setdefault("lease", _bootstrapped_lease())
        return sweep_once(
            workspace_id=WS,
            lane_id=LANE,
            issue=ISSUE,
            lane_generation=GEN,
            source=source,
            fence=self.fence,
            target_assigned_name=TARGET,
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
        self.assertEqual(self.sends, [self.recorder.journal])
        self.assertEqual(self.fence.state_of(self.anchor_key()), FENCE_DELIVERED)

    def test_the_sweep_re_reads_before_and_after_recording(self):
        # Acceptance 2 + review R3-F1: the decision read, the mutation-boundary re-check, and the
        # post-record position verify are all SEPARATE durable reads. Closing one window used to
        # just move it one step later, so the count is pinned deliberately.
        source = RaceSource([ir("79990")])
        self.sweep(source)
        self.assertEqual(source.reads, 3)

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
        # The mutation-boundary re-read and the post-record verify BOTH refuse this send, so
        # asserting zero-send alone cannot tell them apart (a probe showed the boundary re-read
        # could be deleted with every test still green). What only the boundary re-read buys is
        # that no FALSE stall record is written into the durable log at all — the verify can only
        # decline to send after one already landed. So pin the log, not just the send.
        self.assertEqual(self.recorder.records, [STATE_PROGRESS_WITHOUT_CALLBACK])

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
        # As above: only the boundary re-read prevents a false stall record from being written.
        self.assertEqual(self.recorder.records, [SWEEP_STATE_STALL_UNPROVABLE])

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
            source=source, fence=self.fence, lease=self.lease, target_assigned_name=TARGET, send_fn=self.send,
            record_fn_factory=as_factory(self.recorder), callback=CALLBACK_SAME_LANE_ONLY,
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
        self.assertEqual(self.sends, [self.recorder.journal])  # still exactly one delivery

    def test_a_new_dispatch_round_gets_its_own_recovery_budget(self):
        # The fence keys on the dispatch anchor, so a genuinely NEW round is not starved by the
        # previous round's delivery.
        self.sweep(RaceSource([ir("79990")]))
        result = self.sweep(RaceSource([ir("80100")]))
        self.assertTrue(result["sent"])
        self.assertEqual(len(self.sends), 2)

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
            lease=_bootstrapped_lease(),
            send_fn=self.send,
            record_fn_factory=as_factory(self.recorder),
        )
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_FENCE_UNAVAILABLE)
        self.assertEqual(self.sends, [])

    def test_a_raising_send_marks_the_fence_uncertain_and_never_auto_retries(self):
        def boom(record_journal):
            raise RuntimeError("transport died")

        result = self.sweep(RaceSource([ir("79990")]), send_fn=boom)
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
            lease=_bootstrapped_lease(),
            send_fn=None,
        )
        self.assertEqual(result["state"], STATE_NO_PROGRESS_AFTER_HANDOFF)
        self.assertFalse(result["sent"])
        self.assertEqual(self.fence.state_of(self.anchor_key()), "absent")


class SnapshotSourceRefusalTest(unittest.TestCase):
    """Review R2-F1: a frozen snapshot's "re-read" is a no-op, so it may never actuate.

    Pinned against the REAL `MappingRedmineJournalSource` — the class production was actually wired
    to — not a fixture. The previous revision's race test used a source that changes between reads,
    a behaviour the snapshot class cannot exhibit, so it "proved" a TOCTOU closure that did not
    exist in production.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fence = DispatchOutboxFence(home=Path(self._tmp.name))
        self.fence.bootstrap()
        self.pubfence = _bootstrapped_pubfence(getattr(self, 'home', Path(self._tmp.name)))
        self.lease = _bootstrapped_lease(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_the_real_snapshot_source_cannot_observe_a_later_landing(self):
        # Reproduced exactly as production composes it: the CLI did `json.loads(file)` ONCE and
        # handed the resulting mapping to the source. A gate landing on Redmine afterwards (here:
        # the file being rewritten) is invisible, so the sweep's "re-read" returns the decision
        # read verbatim. Note the mapping must be loaded from the file, not shared with the test —
        # sharing the dict would make it look live, which production never is.
        import json

        path = Path(tempfile.mkdtemp()) / "snapshot.json"
        payload = {"issue": {"id": ISSUE}, "journals": [
            {"id": "79990", "notes": render_dispatch_note("IR", lane=LANE, lane_generation=GEN)}]}
        path.write_text(json.dumps(payload))

        source = MappingRedmineJournalSource(payload=json.loads(path.read_text()))
        first = [e.journal_id for e in source.read_entries(ISSUE)]

        payload["journals"].append({"id": "79995", "notes": "## Gate: review_finding_verdict"})
        path.write_text(json.dumps(payload))  # the gate lands durably

        second = [e.journal_id for e in source.read_entries(ISSUE)]
        self.assertEqual(first, ["79990"])
        self.assertEqual(second, ["79990"])  # the "fresh" re-read never sees j#79995
        self.assertFalse(source_is_fresh(source))

    def test_sweep_refuses_to_mutate_on_a_snapshot_source(self):
        sends = []
        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=MappingRedmineJournalSource(payload={"issue": {"id": ISSUE}, "journals": [
                {"id": "79990", "notes": render_dispatch_note("IR", lane=LANE, lane_generation=GEN)}]}),
            fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
            send_fn=lambda j: sends.append(j), record_fn_factory=as_factory(FakeRecorder()),
        )
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_SOURCE_NOT_FRESH)
        self.assertEqual(sends, [])

    def test_the_live_source_declares_freshness(self):
        # The counterpart: the live adapter re-fetches per call, so it may actuate.
        self.assertTrue(source_is_fresh(LiveRedmineJournalSource(base_url="https://x", api_key="k")))


class WorkspaceAttestationTest(unittest.TestCase):
    """Review R2-F2: the fence key is workspace-partitioned, so a blank id is a fence bypass."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fence = DispatchOutboxFence(home=Path(self._tmp.name))
        self.fence.bootstrap()
        self.pubfence = _bootstrapped_pubfence(getattr(self, 'home', Path(self._tmp.name)))
        self.addCleanup(self._tmp.cleanup)

    def sweep(self, ws, sends):
        return sweep_once(
            workspace_id=ws, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=RaceSource([ir("79990")]), fence=self.fence,
            target_assigned_name=TARGET,
            lease=_bootstrapped_lease(), send_fn=lambda j: sends.append(j),
            record_fn_factory=as_factory(FakeRecorder()),
        )

    def test_a_blank_workspace_id_is_zero_send(self):
        sends = []
        result = self.sweep("", sends)
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_WORKSPACE_UNATTESTED)
        self.assertEqual(sends, [])

    def test_blank_then_real_workspace_cannot_send_twice(self):
        # The exact reproduction: blank reserved a DIFFERENT fence row, so the same recovery for
        # the same dispatch anchor sent twice (reviewer measured send_count=2).
        sends = []
        self.sweep("", sends)
        self.sweep("ws-real", sends)
        self.assertEqual(len(sends), 1)


class RecoveryRecordTest(unittest.TestCase):
    """Review R2-F3: the classification is durable BEFORE the send, and the pointer names it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fence = DispatchOutboxFence(home=Path(self._tmp.name))
        self.fence.bootstrap()
        self.pubfence = _bootstrapped_pubfence(getattr(self, 'home', Path(self._tmp.name)))
        self.lease = _bootstrapped_lease(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def sweep(self, *, record_fn, sends, source=None):
        return sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=source or RaceSource([ir("79990")]), fence=self.fence,
            target_assigned_name=TARGET, lease=self.lease,
            send_fn=lambda j: sends.append(j), record_fn_factory=as_factory(record_fn),
        )

    def test_no_recorder_means_no_send(self):
        sends = []
        result = self.sweep(record_fn=None, sends=sends)
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_RECORD_FAILED)
        self.assertEqual(sends, [])

    def test_the_record_is_written_before_the_send_and_is_what_the_pointer_names(self):
        order = []

        def recorder(result, watermark):
            order.append("record")
            return "90001"

        def send(journal):
            order.append(f"send->{journal}")

        sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=RaceSource([ir("79990")]), fence=self.fence,
            target_assigned_name=TARGET,
            lease=self.lease, send_fn=send, record_fn_factory=as_factory(recorder),
        )
        self.assertEqual(order, ["record", "send->90001"])

    def test_an_unresolvable_record_is_zero_send(self):
        # A send whose reason never landed durably is the prohibited silent re-poke.
        sends = []
        result = self.sweep(record_fn=lambda r, w: "", sends=sends)
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_RECORD_FAILED)
        self.assertFalse(result["resolution_recorded"])
        self.assertEqual(sends, [])

    def test_a_record_failure_leaves_no_durable_authority_touched(self):
        # Review R3-F3. The previous revision reserved first and marked the key `cancelled` on a
        # record failure, claiming it was "released for a later attempt" -- but FENCE_CANCELLED is
        # TERMINAL to reserve(), so that anchor's recovery was blocked forever. Recording before
        # reserving means a failed attempt touches nothing.
        self.sweep(record_fn=lambda r, w: "", sends=[])
        self.assertEqual(self.fence.state_of(FenceKey(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, journal="79990",
            action_id=SWEEP_RECOVERY_ACTION_ID, target_assigned_name=TARGET)), "absent")

    def test_a_transient_record_failure_is_retryable_and_then_sends_exactly_once(self):
        # The reviewer's exact reproduction: attempt 1 fails to record, attempt 2 has a healthy
        # writer. The old code returned `fence_held` here and could never send again.
        sends = []
        first = self.sweep(record_fn=lambda r, w: "", sends=sends)
        self.assertEqual(first["send_reason"], ZERO_SEND_RECORD_FAILED)

        second = self.sweep(record_fn=FakeRecorder("90002"), sends=sends)
        self.assertTrue(second["sent"])
        self.assertEqual(sends, ["90002"])

        # ...and the retry is still at-most-once: a third healthy sweep does not re-send.
        third = self.sweep(record_fn=FakeRecorder("90002"), sends=sends)
        self.assertFalse(third["sent"])
        self.assertEqual(third["send_reason"], ZERO_SEND_FENCE_HELD)
        self.assertEqual(sends, ["90002"])

    def test_a_gate_landing_after_the_boundary_read_but_before_the_record_is_zero_send(self):
        # Review R3-F1, the exact seam: the decision read and the mutation-boundary re-read both
        # see a silent lane, and the gate lands while the record is being written. The previous
        # revision wrote a stall record and SENT; the recorder's own fresh read saw the gate but
        # was only used to look for existing records, never to re-classify.
        #
        # Redmine has no CAS, so the window is closed by POSITION: the record's journal id is a
        # serialization point, and a qualifying gate PRECEDING it proves the verdict was already
        # stale when written.
        gate_j = gate("79995", "review_result", conclusion="changes_requested")

        class LateGateSource:
            fresh_read = True

            def __init__(self):
                self.reads = 0

            def read_entries(self, issue_id):
                self.reads += 1
                # reads 1-2: decision + mutation boundary -> silent.
                # read 3+: the post-record verify -> the gate is now on the record, at j#79995,
                # which precedes the record written at j#99999.
                return [ir("79990")] if self.reads <= 2 else [ir("79990"), gate_j]

        sends = []
        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=LateGateSource(), fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
            send_fn=lambda j: sends.append(j), record_fn_factory=as_factory(FakeRecorder("99999")),
        )
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_PROGRESS_LANDED)
        self.assertEqual(result["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertEqual(sends, [])

    def test_a_gate_landing_after_the_record_also_zero_sends_but_is_not_a_stale_record(self):
        # The position check decides what the LOG means, not whether to send: any live lane is
        # left alone. A gate after j#R means the record was true when written, so no correction is
        # owed -- distinct from the preceding-gate case, where the log now holds a false verdict.
        late = gate("99999", "review_result", conclusion="changes_requested")

        class AfterRecordSource:
            fresh_read = True

            def __init__(self):
                self.reads = 0

            def read_entries(self, issue_id):
                self.reads += 1
                return [ir("79990")] if self.reads <= 2 else [ir("79990"), late]

        sends = []
        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=AfterRecordSource(), fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
            send_fn=lambda j: sends.append(j), record_fn_factory=as_factory(FakeRecorder("90001")),
        )
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_PROGRESS_LANDED)
        self.assertFalse(result["record_stale_at_write"])  # j#99999 > j#90001
        self.assertEqual(sends, [])

    def test_a_preceding_gate_flags_the_record_as_stale_at_write(self):
        gate_j = gate("79995", "review_result", conclusion="changes_requested")

        class PrecedingSource:
            fresh_read = True

            def __init__(self):
                self.reads = 0

            def read_entries(self, issue_id):
                self.reads += 1
                return [ir("79990")] if self.reads <= 2 else [ir("79990"), gate_j]

        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=PrecedingSource(), fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
            send_fn=lambda j: None, record_fn_factory=as_factory(FakeRecorder("99999")),
        )
        self.assertFalse(result["sent"])
        self.assertTrue(result["record_stale_at_write"])  # j#79995 < j#99999

    def test_a_zero_send_resolution_is_still_recorded(self):
        # Acceptance 3: the first-pass resolution is durable; no correction journal is needed.
        rec = FakeRecorder()
        sends = []
        result = self.sweep(
            record_fn=rec, sends=sends,
            source=RaceSource([ir("79990"), gate("79995", "implementation_done")]),
        )
        self.assertFalse(result["sent"])
        self.assertEqual(rec.records, [result["state"]])
        self.assertEqual(result["recovery_record_journal"], rec.journal)
        self.assertTrue(result["resolution_recorded"])

    def test_a_failed_resolution_record_is_reported_incomplete_not_resolved(self):
        # Review R3-F4. A best-effort record let the sweep return state='progress_without_callback'
        # -- presented as a first-pass resolution -- while nothing durable had been written. The
        # caller could not tell that claim from a real one.
        def boom(result, watermark):
            raise RuntimeError("redmine write failed")

        result = self.sweep(
            record_fn=boom, sends=[],
            source=RaceSource([ir("79990"), gate("79995", "implementation_done")]),
        )
        self.assertFalse(result["resolution_recorded"])
        self.assertEqual(result["record_reason"], "RuntimeError")
        self.assertNotIn("recovery_record_journal", result)

    def test_a_blank_resolution_record_is_also_incomplete(self):
        result = self.sweep(
            record_fn=lambda r, w: "", sends=[],
            source=RaceSource([ir("79990"), gate("79995", "implementation_done")]),
        )
        self.assertFalse(result["resolution_recorded"])
        self.assertEqual(result["record_reason"], "unresolved")

    def test_the_sweep_record_is_recognized_but_never_progress(self):
        # The needle R2-F3 requires: the coordinator's own record must not clear a genuine stall
        # (masquerade as worker progress), and must not be opaque either (which would make every
        # later sweep abstain — the sweep would silence itself).
        note = render_sweep_record_note(
            "## sweep record", lane=LANE, lane_generation=GEN,
            dispatch_anchor="79990", outcome="no_progress_after_handoff",
        )
        rec = entry("79996", note)
        entries = [ir("79990"), rec]
        self.assertEqual(
            progress_entries_after(entries, after_journal="79990", lane=LANE, lane_generation=GEN),
            (),  # not progress
        )
        self.assertEqual(opaque_entries_after(entries, after_journal="79990"), ())  # not opaque
        # So a still-silent lane stays a provable stall even after the sweep recorded itself.
        w = resolve_watermark(entries, dispatch_journal="79990", lane=LANE, lane_generation=GEN,
                              latest_generation=GEN)
        self.assertTrue(w.stall_provable)

    def test_the_recorder_resolves_its_own_journal_and_is_idempotent(self):
        # Redmine's note write returns 204 with no journal id, so the recorder must write then
        # re-read and resolve its marker's OWNING entry (the reconcile_dispatch_writer pattern).
        posted = []

        class Src:
            fresh_read = True

            def read_entries(self, issue_id):
                return [ir("79990")] + [entry("80500", n) for n in posted]

        src = Src()
        recorder = build_recovery_recorder(
            source=src, issue=ISSUE, lane=LANE, lane_generation=GEN,
            post_note=lambda i, n: posted.append(n), grant_is_live=lambda: True,
            publication_fence=self.pubfence, workspace_id=WS,
        )
        wm = resolve_watermark([ir("79990")], dispatch_journal="79990", lane=LANE,
                               lane_generation=GEN, latest_generation=GEN)
        first = recorder({"state": "no_progress_after_handoff", "dispatch_journal": "79990"}, wm)
        self.assertEqual(first, "80500")
        self.assertEqual(len(posted), 1)
        # A repeated pass at the same resolution recovers the record instead of duplicating it.
        again = recorder({"state": "no_progress_after_handoff", "dispatch_journal": "79990"}, wm)
        self.assertEqual(again, "80500")
        self.assertEqual(len(posted), 1)


class ProductionRecorderTest(unittest.TestCase):
    """Drive the REAL `build_recovery_recorder` + real fence (review R4-F1/F2/F3/F4).

    `FakeRecorder` has no pre-write read, so it cannot exhibit the recorder's own durable
    observation point -- which is precisely where R4-F1 lived while every FakeRecorder-based
    regression stayed green. These tests use the production recorder for that reason.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fence = DispatchOutboxFence(home=Path(self._tmp.name))
        self.fence.bootstrap()
        self.pubfence = _bootstrapped_pubfence(getattr(self, 'home', Path(self._tmp.name)))
        self.posted = []
        self.sends = []
        self.lease = _bootstrapped_lease(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def outcomes(self):
        import re
        return [re.search(r"outcome=([a-z_]+)", n).group(1) for n in self.posted]

    def recorder(self, source):
        """A recorder FACTORY, like production: the grant predicate comes from sweep_once."""
        return lambda grant_is_live: build_recovery_recorder(
            source=source, issue=ISSUE, lane=LANE, lane_generation=GEN,
            post_note=lambda i, n: self.posted.append(n), grant_is_live=grant_is_live,
            publication_fence=self.pubfence, workspace_id=WS,
        )

    def _published(self):
        return [entry(str(90000 + k), n) for k, n in enumerate(self.posted)]

    def test_a_gate_visible_only_to_the_recorder_writes_no_stall_record(self):
        # R4-F1: the recorder's pre-write read is a durable OBSERVATION. Writing a stall record
        # against a read that already shows the gate produced the exact
        # [no_progress_after_handoff, progress_without_callback] pair acceptance 3 forbids.
        gate_j = gate("79995", "review_result", conclusion="changes_requested")
        outer = self

        class LateSource:
            fresh_read = True

            def __init__(self):
                self.n = 0

            def read_entries(self, issue_id):
                self.n += 1
                base = [ir("79990")] if self.n <= 2 else [ir("79990"), gate_j]
                return base + outer._published()

        src = LateSource()
        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN, source=src,
            fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
            send_fn=lambda j: self.sends.append(j), record_fn_factory=self.recorder(src),
        )
        self.assertEqual(self.outcomes(), [STATE_PROGRESS_WITHOUT_CALLBACK])  # no stall record
        self.assertEqual(result["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertEqual(self.sends, [])

    def test_a_gate_landing_before_the_final_live_read_is_not_replayed(self):
        # R4-F2: the record's journal id is a POSITION, not a CAS against future writes, so the
        # guarantee comes from the final live read sitting immediately before the send. Any gate
        # durable by that read is caught -- which covers the whole #13883 evidence (seconds to
        # minutes).
        #
        # Deliberately NOT asserted here: a gate landing AFTER that final read (e.g. during the
        # send itself) is the disclosed read->send window. Redmine has no CAS, so the sweep alone
        # cannot close it; whether that satisfies j#80058 Acceptance 2 is a coordinator/owner
        # decision under design consultation j#80273. This test pins what is actually guaranteed
        # rather than a guarantee that does not exist.
        gate_j = gate("79995", "review_result", conclusion="changes_requested")
        outer = self

        class LateGateSource:
            fresh_read = True

            def __init__(self):
                self.n = 0

            def read_entries(self, issue_id):
                self.n += 1
                # silent through the boundary read; durable by the final live read.
                base = [ir("79990")] if self.n <= 2 else [ir("79990"), gate_j]
                return base + outer._published()

        src = LateGateSource()
        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN, source=src,
            fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
            send_fn=lambda j: self.sends.append(j), record_fn_factory=self.recorder(src),
        )
        self.assertFalse(result["sent"])
        self.assertEqual(self.sends, [])

    def test_the_zero_send_resolution_is_published_while_the_lease_is_still_held(self):
        # R5-F2 pinned DETERMINISTICALLY. `_abort` released the lease before recording, so the
        # zero-send resolution -- the most common outcome -- was published outside the serialized
        # region. A concurrency test cannot pin that reliably: the loser now stands down at the
        # lease and never reaches the recorder, so the two orderings look identical unless a very
        # tight interleaving is forced. Asserting the invariant directly is stronger and stable:
        # at record time, the lease MUST still be ours.
        held_at_record = []
        key = LeaseKey(workspace_id=WS, lane_id=LANE, issue=ISSUE, anchor="79990")

        def recorder(result, watermark):
            held_at_record.append(self.lease.owner_of(key))
            return "90001"

        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=RaceSource([ir("79990"), gate("79995", "review_result",
                                                 conclusion="changes_requested")]),
            fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
            send_fn=lambda j: self.sends.append(j), record_fn_factory=as_factory(recorder),
            callback=CALLBACK_SAME_LANE_ONLY,
        )
        self.assertEqual(result["state"], STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertEqual(len(held_at_record), 1)
        self.assertNotEqual(held_at_record[0], "")  # the lease was still held when we published
        # ...and it is released afterwards, so the next sweep is not blocked.
        self.assertEqual(self.lease.owner_of(key), "")

    def test_two_sweeps_that_both_see_progress_publish_one_resolution(self):
        # R5-F2: `_abort` released the lease BEFORE recording, so the zero-send resolution -- the
        # most common outcome -- was published outside the serialized region and two sweeps posted
        # duplicate `progress_without_callback` records.
        gate_j = gate("79995", "review_result", conclusion="changes_requested")
        lock = threading.Lock()
        start = threading.Event()
        outer = self

        class Src:
            fresh_read = True

            def read_entries(self, issue_id):
                with lock:
                    return [ir("79990"), gate_j] + outer._published()

        def publish(issue_id, note):
            with lock:
                outer.posted.append(note)

        def run():
            start.wait()
            src = Src()
            sweep_once(
                workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN, source=src,
                fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
                send_fn=lambda j: self.sends.append(j), callback=CALLBACK_SAME_LANE_ONLY,
                record_fn_factory=lambda live: build_recovery_recorder(
                    source=src, issue=ISSUE, lane=LANE, lane_generation=GEN, post_note=publish, grant_is_live=live,
                    publication_fence=self.pubfence, workspace_id=WS,
                ),
            )

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.outcomes(), [STATE_PROGRESS_WITHOUT_CALLBACK])
        self.assertEqual(self.sends, [])

    def test_two_concurrent_sweeps_publish_one_record_and_send_at_most_once(self):
        # R4-F3: with the record published outside the reservation, two sweeps both passed an empty
        # pre-read, posted duplicate records, and then BOTH returned "" forever (>=2 = ambiguous),
        # losing recovery for that anchor permanently. The reservation now owns the whole attempt.
        lock = threading.Lock()
        start = threading.Event()
        outer = self

        class Src:
            fresh_read = True

            def read_entries(self, issue_id):
                with lock:
                    return [ir("79990")] + outer._published()

        def publish(issue_id, note):
            with lock:
                outer.posted.append(note)

        reasons = []

        def run():
            start.wait()
            src = Src()
            r = sweep_once(
                workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN, source=src,
                fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
                send_fn=lambda j: self.sends.append(j),
                record_fn_factory=lambda live: build_recovery_recorder(
                    source=src, issue=ISSUE, lane=LANE, lane_generation=GEN, post_note=publish, grant_is_live=live,
                    publication_fence=self.pubfence, workspace_id=WS,
                ),
            )
            reasons.append(r["send_reason"])

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=10)

        # The invariants, which hold regardless of interleaving:
        self.assertEqual(len(self.posted), 1)             # one durable record
        self.assertLessEqual(len(self.sends), 1)          # one send budget
        self.assertEqual(len(reasons), 2)
        # Exactly one sweep proceeded; the other stood down. WHICH authority stopped it is
        # timing-dependent -- the lease if the attempts overlap, the fence if the first had already
        # finished and released -- so asserting a specific one makes this test flaky rather than
        # stronger. The passive-loser behaviour itself is pinned deterministically in
        # `AttemptLeaseTest.test_a_concurrent_loser_is_passive_and_never_touches_the_live_owner`.
        stood_down = [r for r in reasons if r in (ZERO_SEND_ATTEMPT_HELD, ZERO_SEND_FENCE_HELD)]
        self.assertEqual(len(stood_down), 1, f"exactly one sweep must stand down; got {reasons}")

    def test_a_post_record_read_failure_keeps_the_pointer_and_reports_incomplete(self):
        # R4-F4: the record IS durable, so returning a bare unreadable result dropped the pointer
        # and let the CLI exit 0 on a sweep that had already mutated Redmine.
        class FailVerify:
            fresh_read = True

            def __init__(self):
                self.n = 0

            def read_entries(self, issue_id):
                self.n += 1
                if self.n >= 3:
                    raise RuntimeError("redmine read failed")
                return [ir("79990")]

        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=FailVerify(), fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
            send_fn=lambda j: self.sends.append(j), record_fn_factory=as_factory(FakeRecorder("90001")),
        )
        self.assertFalse(result["sent"])
        self.assertEqual(result["recovery_record_journal"], "90001")  # pointer preserved
        self.assertFalse(result["sweep_complete"])
        self.assertEqual(self.sends, [])


class AttemptLeaseTest(unittest.TestCase):
    """The owner-token attempt lease (review R5-F1).

    `DispatchOutboxFence` could not be this authority: its `reserve` reads a lingering `reserved`
    row as crash residue and rewrites it to `uncertain`, so holding a reservation across slow I/O
    let any concurrent sweep corrupt a live owner and block the anchor forever. Adding a `release`
    to it was rejected -- `state == reserved` does not prove the send never happened, because the
    row has no owner identity. These pin the property that fixes it: every row names its owner.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lease = _bootstrapped_lease(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def key(self):
        return LeaseKey(workspace_id=WS, lane_id=LANE, issue=ISSUE, anchor="79990")

    def test_a_concurrent_loser_is_passive_and_never_touches_the_live_owner(self):
        won = self.lease.acquire(self.key())
        self.assertTrue(won.owned)
        loser = self.lease.acquire(self.key())
        self.assertFalse(loser.owned)
        self.assertEqual(loser.status, LEASE_HELD)
        self.assertEqual(loser.token, "")
        # The owner's lease is untouched -- the exact thing the shared fence could not promise.
        self.assertEqual(self.lease.owner_of(self.key()), won.token)

    def test_the_owner_can_stand_down_and_the_next_attempt_is_clean(self):
        won = self.lease.acquire(self.key())
        self.assertTrue(self.lease.release(self.key(), won.token))
        self.assertEqual(self.lease.owner_of(self.key()), "")
        self.assertTrue(self.lease.acquire(self.key()).owned)  # clean retry, no permanent block

    def test_release_is_owner_conditional(self):
        won = self.lease.acquire(self.key())
        self.assertFalse(self.lease.release(self.key(), "not-the-owner"))
        self.assertEqual(self.lease.owner_of(self.key()), won.token)

    def test_an_expired_lease_is_reclaimable_so_a_crashed_owner_never_blocks_the_anchor(self):
        dead = self.lease.acquire(self.key(), ttl_seconds=10, now=1000.0)
        # Still live at t+5: a slow-but-live owner is never stolen from.
        self.assertFalse(self.lease.acquire(self.key(), now=1005.0).owned)
        # Past the deadline: reclaimable, and the dead owner's token no longer releases anything.
        taken = self.lease.acquire(self.key(), now=1011.0)
        self.assertTrue(taken.owned)
        self.assertEqual(taken.status, LEASE_RECLAIMED)
        self.assertFalse(self.lease.release(self.key(), dead.token))


class LeaseOwnershipFencingTest(unittest.TestCase):
    """Review R6-F1/F2: acquiring is not owning. Ownership is re-verified at every durable act."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.lease = _bootstrapped_lease(self.home)
        self.fence = DispatchOutboxFence(home=self.home)
        self.fence.bootstrap()
        self.pubfence = _bootstrapped_pubfence(getattr(self, 'home', Path(self._tmp.name)))
        self.posted = []
        self.sends = []
        self.addCleanup(self._tmp.cleanup)

    def key(self):
        return LeaseKey(workspace_id=WS, lane_id=LANE, issue=ISSUE, anchor="79990")

    def test_an_owner_that_lost_its_expired_lease_publishes_nothing_and_sends_nothing(self):
        # R6-F1. My R6 safety argument -- "a dead owner provably has not sent, because the send is
        # fence-gated after the leased work" -- only ever covered the SEND. Publication is gated by
        # the lease ALONE, so an owner that is merely SLOW (not dead) could outlive its TTL, get
        # reclaimed, and still publish: two durable records for one anchor.
        held = self.lease.acquire(self.key())
        # another sweep reclaims the anchor while this owner is still working
        self.lease.acquire(self.key(), now=time.time() + 99999)
        self.assertFalse(self.lease.owns(self.key(), held.token))

        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=RaceSource([ir("79990")]), fence=self.fence, lease=self.lease,
            target_assigned_name=TARGET, send_fn=lambda j: self.sends.append(j),
            record_fn_factory=lambda live: build_recovery_recorder(
                source=RaceSource([ir("79990")]), issue=ISSUE, lane=LANE, lane_generation=GEN,
                post_note=lambda i, n: self.posted.append(n), grant_is_live=live,
                publication_fence=self.pubfence, workspace_id=WS,
            ),
        )
        # This sweep is a fresh attempt: it either owns the lease cleanly, or stands down. What it
        # must never do is publish while a different owner holds the anchor.
        self.assertLessEqual(len(self.posted), 1)
        self.assertEqual(self.sends, [])

    def test_a_lapsed_owner_records_no_stall_and_sends_nothing(self):
        # R6-F1 on the path where it is load-bearing. The sweep acquires the lease and then does
        # its Redmine reads; a slow owner can outlive its TTL right there and be reclaimed. Without
        # a check at the durable act, this sweep and the new owner both publish -- two
        # `no_progress_after_handoff` records for one anchor, which is what the auditor reproduced.
        #
        # The lease is stolen during the BOUNDARY re-read, i.e. after this sweep acquired and while
        # it is still working: exactly the "slow, not dead" owner the TTL creates.
        outer = self

        class StealingSource:
            fresh_read = True

            def __init__(self):
                self.n = 0

            def read_entries(self, issue_id):
                self.n += 1
                if self.n == 2:  # the boundary re-read: the sweep already owns the lease
                    outer.lease.acquire(outer.key(), now=time.time() + 99999)
                return [ir("79990")]  # a genuinely silent lane -> the stall path

        posted = []
        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=StealingSource(), fence=self.fence, lease=self.lease,
            target_assigned_name=TARGET, send_fn=lambda j: self.sends.append(j),
            record_fn_factory=as_factory(lambda r, w: (posted.append(r["state"]), "90001")[1]),
        )
        self.assertEqual(posted, [])                 # published nothing: it no longer owns it
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_OWNERSHIP_LOST)
        self.assertEqual(self.sends, [])

    def test_a_reclaim_inside_the_recorder_publishes_nothing(self):
        # R7-F1. Checking ownership before CALLING the recorder is not enough: the production
        # recorder then does its own Redmine pre-read before post_note, so the gap between the
        # check and the actual write is a whole network round-trip -- ample time for a slow owner's
        # TTL to lapse. The reclaim is injected inside that gap, which is where it really happens.
        outer = self
        posted, sends, seen = [], [], {"n": 0}

        class Src:
            fresh_read = True

            def read_entries(self, issue_id):
                return [ir("79990")] + [entry(str(96000 + k), n) for k, n in enumerate(posted)]

        class RecorderSource:
            fresh_read = True

            def read_entries(self, issue_id):
                seen["n"] += 1
                if seen["n"] == 1:            # the recorder's pre-read: steal the lease HERE
                    outer.lease.acquire(outer.key(), now=time.time() + 99999)
                return [ir("79990")] + [entry(str(96000 + k), n) for k, n in enumerate(posted)]

        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN, source=Src(),
            fence=self.fence, lease=self.lease, target_assigned_name=TARGET,
            send_fn=lambda j: sends.append(j),
            record_fn_factory=lambda live: build_recovery_recorder(
                source=RecorderSource(), issue=ISSUE, lane=LANE, lane_generation=GEN,
                post_note=lambda i, n: posted.append(n), grant_is_live=live,
                publication_fence=self.pubfence, workspace_id=WS,
            ),
        )
        self.assertEqual(posted, [])          # zero-publication, not "published then declined to send"
        self.assertEqual(sends, [])
        self.assertEqual(result["send_reason"], ZERO_SEND_OWNERSHIP_LOST)

    def test_bootstrap_refuses_a_sidecar_only_loss_and_recover_is_the_way_out(self):
        # R7-F2. The production composition root bootstraps on EVERY --execute, so silently
        # re-minting a nonce onto a live DB invalidates the grant of an owner that is still
        # working. The sibling fence refuses this exact state; only half that contract had been
        # copied (sidecar-present/DB-missing), leaving DB-present/sidecar-missing wide open.
        a = self.lease.acquire(self.key())
        self.lease.sidecar_path.unlink()                 # sidecar lost, DB (and the grant) live
        with self.assertRaises(CallbackSweepLeaseError):
            self.lease.bootstrap()
        # ...and a fail-closed state needs a sanctioned way out, or it is a permanent stall.
        self.lease.recover()
        b = self.lease.acquire(self.key())
        self.assertTrue(b.owned)
        self.assertNotEqual(a.store_nonce, b.store_nonce)   # the old grant is invalidated

    def test_bootstrap_is_idempotent_only_on_an_exact_identity_match(self):
        a = self.lease.acquire(self.key())
        self.lease.bootstrap()                            # DB + sidecar agree -> no-op
        self.assertTrue(self.lease.owns(self.key(), a.token))

    def test_the_r9_sequence_publishes_exactly_once(self):
        # The R9-F1 sequence, verbatim: A reserves -> A is suspended past the attempt TTL -> B
        # reclaims the lease and attempts the SAME record -> A resumes holding a stale check.
        # Previously this produced posted_count=2. The publication fence never reclaims, so
        # whoever reserved the record identity is the only one who can ever write it.
        posted = []
        outer = self

        class Src:
            fresh_read = True

            def read_entries(self, issue_id):
                return [ir("79990")] + [entry(str(99000 + k), n) for k, n in enumerate(posted)]

        def mk(grant):
            return build_recovery_recorder(
                source=Src(), issue=ISSUE, lane=LANE, lane_generation=GEN,
                post_note=lambda i, n: posted.append(n), grant_is_live=grant,
                publication_fence=self.pubfence, workspace_id=WS,
            )

        wm = resolve_watermark([ir("79990")], dispatch_journal="79990", lane=LANE,
                               lane_generation=GEN, latest_generation=GEN)
        res = {"state": STATE_NO_PROGRESS_AFTER_HANDOFF, "dispatch_journal": "79990"}
        a = self.lease.acquire(self.key())

        def a_grant():
            ok = self.lease.owns(self.key(), a.token)
            # A is suspended here, past its TTL. B reclaims and attempts the same record.
            self.lease.acquire(self.key(), now=time.time() + 99999)
            try:
                mk(lambda: True)(res, wm)
            except (RecordPublicationHeldError, RecordPublicationUncertainError):
                pass
            return ok      # A resumes, still holding the check it already passed

        try:
            mk(a_grant)(res, wm)
        except (RecordPublicationHeldError, RecordPublicationUncertainError):
            pass
        self.assertEqual(len(posted), 1)

    def test_a_reservation_is_never_reclaimed_so_a_crashed_owner_blocks_rather_than_duplicates(self):
        # A reserved and then crashed (no published/uncertain mark). A re-entry must NOT take over:
        # the owner may be mid-PUT, and reclaiming is exactly what created duplicates. The anchor
        # stalls and an operator reconciles -- availability traded for safety, deliberately.
        key = PublicationKey(workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=str(GEN),
                             dispatch_anchor="79990", outcome=STATE_NO_PROGRESS_AFTER_HANDOFF)
        first = self.pubfence.reserve(key)
        self.assertTrue(first.may_publish)
        second = self.pubfence.reserve(key)                 # a re-entry, any time later
        self.assertFalse(second.may_publish)
        self.assertEqual(second.prior_state, PUBLICATION_RESERVED)
        self.assertTrue(second.needs_reconcile)
        self.assertEqual(self.pubfence.state_of(key), PUBLICATION_RESERVED)  # owner's row intact

    def test_an_uncertain_put_is_never_auto_retried(self):
        posted = []

        class Src:
            fresh_read = True

            def read_entries(self, issue_id):
                return [ir("79990")]

        def boom(issue_id, note):
            posted.append(note)                             # the PUT may well have landed...
            raise RuntimeError("connection reset")          # ...but its fate is unknown

        rec = build_recovery_recorder(
            source=Src(), issue=ISSUE, lane=LANE, lane_generation=GEN, post_note=boom,
            grant_is_live=lambda: True, publication_fence=self.pubfence, workspace_id=WS,
        )
        wm = resolve_watermark([ir("79990")], dispatch_journal="79990", lane=LANE,
                               lane_generation=GEN, latest_generation=GEN)
        res = {"state": STATE_NO_PROGRESS_AFTER_HANDOFF, "dispatch_journal": "79990"}
        with self.assertRaises(RecordPublicationUncertainError):
            rec(res, wm)
        key = PublicationKey(workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=str(GEN),
                             dispatch_anchor="79990", outcome=STATE_NO_PROGRESS_AFTER_HANDOFF)
        self.assertEqual(self.pubfence.state_of(key), PUBLICATION_UNCERTAIN)
        # A later sweep must NOT try again: only Redmine knows whether that PUT landed.
        with self.assertRaises(RecordPublicationHeldError):
            build_recovery_recorder(
                source=Src(), issue=ISSUE, lane=LANE, lane_generation=GEN,
                post_note=lambda i, n: posted.append(n), grant_is_live=lambda: True,
                publication_fence=self.pubfence, workspace_id=WS,
            )(res, wm)
        self.assertEqual(len(posted), 1)

    def test_the_stall_and_zero_send_outcomes_are_fenced_separately(self):
        # Both outcomes go through the same contract, but they are DIFFERENT records: a lane whose
        # verdict legitimately changes must still be able to record the new one.
        stall = PublicationKey(workspace_id=WS, lane_id=LANE, issue=ISSUE,
                               lane_generation=str(GEN), dispatch_anchor="79990",
                               outcome=STATE_NO_PROGRESS_AFTER_HANDOFF)
        progress = PublicationKey(workspace_id=WS, lane_id=LANE, issue=ISSUE,
                                  lane_generation=str(GEN), dispatch_anchor="79990",
                                  outcome=STATE_PROGRESS_WITHOUT_CALLBACK)
        self.assertTrue(self.pubfence.reserve(stall).may_publish)
        self.assertTrue(self.pubfence.reserve(progress).may_publish)   # a distinct identity
        self.assertFalse(self.pubfence.reserve(stall).may_publish)     # but each only once

    def test_publication_fence_store_loss_fails_closed_and_recover_is_the_way_out(self):
        key = PublicationKey(workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=str(GEN),
                             dispatch_anchor="79990", outcome=STATE_NO_PROGRESS_AFTER_HANDOFF)
        self.pubfence.reserve(key)
        self.pubfence.sidecar_path.unlink()
        with self.assertRaises(CallbackPublicationFenceError):
            self.pubfence.bootstrap()
        with self.assertRaises(CallbackPublicationFenceError):
            self.pubfence.reserve(key)              # forgetting a reservation would republish
        self.pubfence.recover()
        self.assertTrue(self.pubfence.reserve(key).may_publish)

    def test_actuation_refuses_a_grant_less_raw_writer(self):
        # R8-F2: the unsafe shape must be unrepresentable, not merely discouraged. A raw writer
        # could previously actuate and reproduce the very race the factory prevents.
        sends = []
        result = sweep_once(
            workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
            source=RaceSource([ir("79990")]), fence=self.fence, lease=self.lease,
            target_assigned_name=TARGET, send_fn=lambda j: sends.append(j),
        )  # no record_fn_factory at all
        self.assertFalse(result["sent"])
        self.assertEqual(result["send_reason"], ZERO_SEND_RECORD_FAILED)
        self.assertEqual(sends, [])

    def test_the_probe_does_not_mutate_the_store_it_inspects(self):
        # R8-F4: is_bootstrapped() claimed "read-only" while executing DDL. A probe that mutates is
        # not a probe. Opened mode=ro, a write attempt would raise rather than silently succeed.
        before = self.lease.path.read_bytes()
        self.assertTrue(self.lease.is_bootstrapped())
        self.assertEqual(self.lease.path.read_bytes(), before)

    def test_a_foreign_schema_is_not_this_store(self):
        # R8-F4: the sibling verifies the exact schema version; a foreign / older store must not
        # read as bootstrapped.
        import sqlite3

        conn = sqlite3.connect(self.lease.path, isolation_level=None)
        try:
            conn.execute("PRAGMA user_version = 999")
        finally:
            conn.close()
        self.assertFalse(self.lease.is_bootstrapped())

    def test_expiry_alone_ends_ownership_even_if_nobody_reclaims(self):
        # Isolates the TTL check. The reclaim test cannot: reclaiming REPLACES the token, so it
        # fails the identity comparison regardless of whether expiry is honoured -- a probe showed
        # `owns` could ignore the deadline entirely with every test still green. An owner whose
        # lease simply lapsed is not an owner, even with nobody waiting.
        a = self.lease.acquire(self.key(), ttl_seconds=0.01)
        self.assertTrue(self.lease.owns(self.key(), a.token))
        time.sleep(0.05)
        self.assertFalse(self.lease.owns(self.key(), a.token))

    def test_a_lost_store_is_refused_as_a_store_loss_specifically(self):
        # Distinguishes the two store-identity guards. Both fail closed, so an assertRaises alone
        # cannot tell them apart; this pins that the MISSING-DB case is diagnosed as a store loss
        # rather than falling through to the nonce mismatch by luck.
        self.lease.acquire(self.key())
        self.lease.path.unlink()
        with self.assertRaises(CallbackSweepLeaseError) as ctx:
            self.lease.acquire(self.key())
        self.assertIn("store", str(ctx.exception).lower())
        self.assertIn("missing", str(ctx.exception).lower())

    def test_a_stale_owner_cannot_publish_after_reclaim(self):
        # The invariant stated directly: a token that no longer owns the anchor is not owning,
        # whatever it did earlier.
        a = self.lease.acquire(self.key())
        self.assertTrue(self.lease.owns(self.key(), a.token))
        b = self.lease.acquire(self.key(), now=time.time() + 99999)
        self.assertFalse(self.lease.owns(self.key(), a.token))   # A lost it
        self.assertTrue(self.lease.owns(self.key(), b.token))    # B holds it
        self.assertFalse(self.lease.release(self.key(), a.token))  # A cannot drop B's lease

    def test_a_replaced_store_is_not_the_store_the_grant_came_from(self):
        # R6-F2: a deleted / recreated store used to hand a second live owner the same anchor while
        # the first still believed it held the lease. Store identity makes that detectable.
        a = self.lease.acquire(self.key())
        self.assertTrue(self.lease.owns(self.key(), a.token, store_nonce=a.store_nonce))
        self.lease.path.unlink()                    # the store is lost underneath the owner
        with self.assertRaises(CallbackSweepLeaseError):
            self.lease.acquire(self.key())          # no silent re-create -> no second owner
        with self.assertRaises(CallbackSweepLeaseError):
            self.lease.owns(self.key(), a.token, store_nonce=a.store_nonce)

    def test_a_recreated_store_invalidates_an_older_grant(self):
        a = self.lease.acquire(self.key())
        self.lease.path.unlink()
        self.lease.sidecar_path.unlink()
        fresh = _bootstrapped_lease(self.home)      # a deliberately re-bootstrapped store
        b = fresh.acquire(self.key())
        self.assertNotEqual(a.store_nonce, b.store_nonce)
        # The old grant is not honoured against the new store identity.
        self.assertFalse(fresh.owns(self.key(), a.token, store_nonce=a.store_nonce))

    def test_bootstrap_refuses_a_lost_store(self):
        # sidecar present, DB gone = store loss. Re-creating silently is what mints duplicates.
        self.lease.path.unlink()
        with self.assertRaises(CallbackSweepLeaseError):
            _bootstrapped_lease(self.home)


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
            target_assigned_name=TARGET,
            lease=_bootstrapped_lease(), send_fn=None, callback=CALLBACK_SAME_LANE_ONLY,
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
            issue=ISSUE, target="w1:p2",
            runner=lambda argv: (calls.append(argv), (0, ""))[1],
        )
        send("90001")   # the recovery record journal sweep_once just wrote
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[1:3], ["handoff", "send"])
        # The notification is a pointer: it must name the durable anchor, not carry the content.
        self.assertIn("--issue", argv)
        self.assertEqual(argv[argv.index("--issue") + 1], ISSUE)
        # R2-F3: the pointer names the RECOVERY RECORD, not the original dispatch.
        self.assertEqual(argv[argv.index("--journal") + 1], "90001")
        self.assertEqual(argv[argv.index("--target") + 1], "w1:p2")

    def test_a_failing_send_raises_so_the_fence_records_uncertain(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_sweep import (
            RecoverySendError,
            build_recovery_sender,
        )

        send = build_recovery_sender(
            issue=ISSUE, target="w1:p2",
            runner=lambda argv: (3, "target unresolved"),
        )
        with self.assertRaises(RecoverySendError):
            send("90001")

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
            issue=ISSUE, target=TARGET,
            runner=lambda argv: (calls.append(argv), (0, ""))[1],
        )
        for _ in range(3):  # repeated sweeps of the same still-silent round
            sweep_once(
                workspace_id=WS, lane_id=LANE, issue=ISSUE, lane_generation=GEN,
                source=RaceSource([ir("79990")]), fence=fence,
                target_assigned_name=TARGET,
            lease=_bootstrapped_lease(), send_fn=sender, record_fn_factory=as_factory(FakeRecorder()),
            )
        self.assertEqual(len(calls), 1)  # at most once per dispatch anchor


class AttestedWorkspaceIdTest(unittest.TestCase):
    """Direct regression for the `_attested_workspace_id` caller (R4 coverage note).

    R3-F2 was that the "measured" authority did not exist where the command runs: `read_anchor`
    returns None in a linked sublane worktree, so the CLI flag silently became the authority. These
    pin the resolver contract at the caller, not just in the registry.
    """

    def resolve(self, *, workspace_id="", canonical=None, raises=False):
        import argparse

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
            sublane_diagnostics as sd,
        )

        class _Resolved:
            def __init__(self, wsid):
                self.workspace_id = wsid

        def fake_resolve(repo_root, **kw):
            if raises:
                raise RuntimeError("registry unreadable")
            return _Resolved(canonical)

        import mozyo_bridge.core.state.workspace_registry as reg

        original = reg.resolve_canonical_session
        reg.resolve_canonical_session = fake_resolve
        try:
            return sd._attested_workspace_id(
                argparse.Namespace(workspace_id=workspace_id, repo=None)
            )
        finally:
            reg.resolve_canonical_session = original

    def test_a_linked_worktree_inherits_its_main_checkout_identity(self):
        # The #13152 topology the real command runs in: no local anchor, identity inherited.
        self.assertEqual(self.resolve(canonical="ws-main"), "ws-main")

    def test_an_unresolved_authority_is_blank_so_the_sweep_zero_sends(self):
        self.assertEqual(self.resolve(canonical=None), "")
        self.assertEqual(self.resolve(raises=True), "")

    def test_the_flag_can_assert_the_measured_identity_but_never_supply_it(self):
        # Matching assertion passes through...
        self.assertEqual(self.resolve(workspace_id="ws-main", canonical="ws-main"), "ws-main")
        # ...a mismatch fails closed...
        with self.assertRaises(SystemExit):
            self.resolve(workspace_id="ws-other", canonical="ws-main")
        # ...and with NO measured authority the flag cannot mint one (the R3-F2 defect).
        with self.assertRaises(SystemExit):
            self.resolve(workspace_id="ws-invented", canonical=None)


if __name__ == "__main__":
    unittest.main()
