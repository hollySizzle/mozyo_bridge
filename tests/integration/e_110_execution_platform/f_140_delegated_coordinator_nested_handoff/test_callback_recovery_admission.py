"""Receiver-side recovery admission, driven by the REAL producer (#13910 j#80984 / j#80986).

Every admission here resolves its key from a record the **real** :func:`build_recovery_recorder`
wrote. That is deliberate and load-bearing: a hand-written marker only proves the reader can parse
what the test author typed, not that the producer emits a value the reader accepts. (#13933's
review found exactly that failure — a hand-built fixture hid a live mismatch through five
adversarial rounds.) So the producer writes, the reader reads, and the round trip is the test.

Covers j#80984's required verification list:

- same-key concurrent admission -> effect count 1
- claim-before-effect crash -> same key admits 0; an explicit NEW anchor admits 1
- generation / route / receiver / workspace drift -> zero-actuation
- unreadable / torn authority -> fail-closed
- the #13889 read->send window (a gate landing around the send) -> absorbed here
- a non-recovery record is not admissible, and the action marker does not regress the sweep
"""

import tempfile
import threading
import unittest
from pathlib import Path

from mozyo_bridge.core.state.callback_publication_fence import CallbackPublicationFence
from mozyo_bridge.core.state.callback_recovery_receipt import (
    CallbackRecoveryReceipt,
    RECEIPT_ABSENT,
    RECEIPT_CLAIMED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_recovery_admission import (
    ADMIT_ADMITTED,
    ADMIT_CONFLICT,
    ADMIT_DUPLICATE,
    ADMIT_SUPERSEDED,
    ADMIT_UNREADABLE,
    admit_recovery,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_recovery_record import (
    build_recovery_recorder,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_sweep_watermark import (
    SWEEP_RECOVERY_RECEIVER,
    resolve_watermark,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    dispatch_generations,
    render_dispatch_note,
    render_gate_note,
    resolve_dispatch_entry_journal,
)

WS = "ws-1"
LANE = "issue_13910_lane"
ISSUE = "13910"
GEN = 1
TARGET = "claude-worker-1"
RECEIVER = SWEEP_RECOVERY_RECEIVER
ANCHOR = "79990"

STALL = "no_progress_after_handoff"


def entry(jid, notes):
    return RedmineJournalEntry(issue_id=ISSUE, journal_id=str(jid), notes=notes)


def ir(jid, generation=GEN):
    return entry(
        jid, render_dispatch_note("## Implementation Request", lane=LANE, lane_generation=generation)
    )


class Src:
    """A live-shaped durable source: re-reads per call, and grows as notes are posted.

    ``fresh_read = True`` mirrors the live Redmine adapter. The admission rail refuses a source
    that does not declare it, so a snapshot can never authorize an effect.
    """

    fresh_read = True

    def __init__(self, entries=()):
        self.entries = list(entries) or [ir(ANCHOR)]
        self._next = 80500

    def read_entries(self, issue):
        return list(self.entries)

    def post(self, _issue, note):
        self.entries.append(entry(str(self._next), note))
        self._next += 1


class _AdmissionBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.pubfence = CallbackPublicationFence(home=self.home)
        self.pubfence.bootstrap()
        self.receipt = CallbackRecoveryReceipt(home=self.home)
        self.receipt.bootstrap()
        self.src = Src()

    def produce(self, *, anchor=ANCHOR, generation=GEN, route=TARGET, receiver=RECEIVER, src=None):
        """Run the REAL recorder and return the journal id of the record it wrote."""
        source = src if src is not None else self.src
        rec = build_recovery_recorder(
            source=source,
            issue=ISSUE,
            lane=LANE,
            lane_generation=generation,
            post_note=source.post,
            grant_is_live=lambda: True,
            publication_fence=self.pubfence,
            workspace_id=WS,
            route_identity=route,
            receiver_identity=receiver,
        )
        wm = resolve_watermark(
            source.read_entries(ISSUE),
            dispatch_journal=anchor,
            lane=LANE,
            lane_generation=generation,
            latest_generation=generation,
        )
        jid = rec({"state": STALL, "dispatch_journal": anchor}, wm)
        self.assertTrue(jid, "the real recorder did not resolve a record journal")
        return jid

    def admit(self, jid, *, route=TARGET, receiver=RECEIVER, workspace=WS, src=None, receipt=None):
        return admit_recovery(
            source=src if src is not None else self.src,
            issue=ISSUE,
            recovery_action_journal=jid,
            workspace_id=workspace,
            route_identity=route,
            receiver_identity=receiver,
            receipt=receipt if receipt is not None else self.receipt,
        )


class AdmitOnceTests(_AdmissionBase):
    def test_first_admission_wins_and_replay_is_a_durable_no_op(self):
        """Acceptance 2: the same key admits exactly once; every replay is a durable no-op."""
        jid = self.produce()
        first = self.admit(jid)
        self.assertEqual(first.outcome, ADMIT_ADMITTED, first.detail)
        self.assertTrue(first.may_actuate)

        for _ in range(3):
            again = self.admit(jid)
            self.assertEqual(again.outcome, ADMIT_DUPLICATE, again.detail)
            self.assertFalse(again.may_actuate)
            self.assertEqual(again.key_digest, first.key_digest)

    def test_only_admitted_ever_authorizes_an_effect(self):
        """``may_actuate`` is not a reader's inference: exactly one outcome carries it."""
        jid = self.produce()
        self.assertTrue(self.admit(jid).may_actuate)
        self.assertFalse(self.admit(jid).may_actuate)

    def test_claim_is_recorded_durably(self):
        jid = self.produce()
        out = self.admit(jid)
        self.assertEqual(out.outcome, ADMIT_ADMITTED)
        # A NEW handle on the same store still sees the claim: the admission is durable, not
        # in-process state.
        fresh = CallbackRecoveryReceipt(home=self.home)
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_recovery_key import (  # noqa: E501
            resolve_recovery_action_key,
        )

        key = resolve_recovery_action_key(
            self.src.read_entries(ISSUE), recovery_action_journal=jid
        ).key
        self.assertEqual(fresh.peek(key), RECEIPT_CLAIMED)

    def test_concurrent_admission_of_one_key_yields_exactly_one_effect(self):
        """Required: same-key concurrent admission -> effect count 1."""
        jid = self.produce()
        effects = []
        lock = threading.Lock()
        # Sized to the racing threads ONLY. The main thread must not be a party: it would be an
        # extra waiter on an already-satisfied barrier and block forever.
        racers = 6
        barrier = threading.Barrier(racers)

        def run():
            barrier.wait(timeout=10)
            out = admit_recovery(
                source=Src(self.src.entries),
                issue=ISSUE,
                recovery_action_journal=jid,
                workspace_id=WS,
                route_identity=TARGET,
                receiver_identity=RECEIVER,
                receipt=CallbackRecoveryReceipt(home=self.home),
            )
            if out.may_actuate:
                with lock:
                    effects.append(out.key_digest)

        threads = [threading.Thread(target=run) for _ in range(racers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "an admission thread hung")
        self.assertEqual(
            len(effects), 1, f"exactly one receiver may actuate; {len(effects)} were authorized"
        )


class CrashAndRetryTests(_AdmissionBase):
    def test_crash_before_effect_is_never_readmitted_but_a_new_anchor_is(self):
        """Required: crash-before-effect -> same key 0, explicit new-anchor retry -> 1.

        j#80984 Disposition 4 (safety-first). The claim is NOT reclaimed on a timer or a
        presumed-dead check, so the crashed round is genuinely lost — and that liveness cost is
        paid back by an explicit coordinator act (a NEW recovery action anchor), never by this
        store quietly re-opening the key.
        """
        effects = 0
        jid = self.produce()

        admitted = self.admit(jid)
        self.assertTrue(admitted.may_actuate)
        # ...the receiver crashes HERE, before its first effect. `effects` stays 0.

        # The same delivery replays against a brand-new process (new store handle, new source).
        replay = self.admit(jid, src=Src(self.src.entries), receipt=CallbackRecoveryReceipt(home=self.home))
        self.assertEqual(replay.outcome, ADMIT_DUPLICATE, replay.detail)
        if replay.may_actuate:
            effects += 1
        self.assertEqual(effects, 0, "a crashed claim must not be re-admitted (no TTL reclaim)")

        # The coordinator explicitly issues a NEW recovery action anchor (a new dispatch round ->
        # a new record -> a new journal id -> a NEW key).
        self.src.entries.append(ir("81000", generation=2))
        new_jid = self.produce(anchor="81000", generation=2)
        self.assertNotEqual(new_jid, jid)

        retried = self.admit(new_jid)
        self.assertEqual(retried.outcome, ADMIT_ADMITTED, retried.detail)
        self.assertNotEqual(retried.key_digest, admitted.key_digest)
        if retried.may_actuate:
            effects += 1
        self.assertEqual(effects, 1, "the explicit new-anchor retry actuates exactly once")


class SupersedeTests(_AdmissionBase):
    def test_gate_landing_after_the_send_is_absorbed_at_the_receiver(self):
        """Acceptance 3 / the #13889 R5-F3 window, closed on the receiving side.

        The record was true when written, and the delivery is in flight. A qualifying gate then
        lands — the exact race that produced ``sent=True`` with the gate already durable. The
        sender cannot see it (its read is already done); the receiver must, and must not actuate.
        """
        jid = self.produce()
        # The gate lands between the send and the admission. `review_result` is the exact kind the
        # #13889 R5-F3 probe durably landed while `sent=True` was reported, so it is rendered
        # through its own canonical writer rather than approximated with a progress note.
        self.src.entries.append(
            entry(
                "80900",
                # Un-scoped, because that is what the canonical gate writer emits: a gate marker
                # carries no lane/generation, and `_entry_progress_kinds` accepts it for exactly
                # that reason. Scoping it here would test a note the producer never writes.
                render_gate_note("review_result", body="## Gate: review_result"),
            )
        )
        out = self.admit(jid)
        self.assertEqual(out.outcome, ADMIT_SUPERSEDED, out.detail)
        self.assertFalse(out.may_actuate)
        self.assertIn("80900", out.detail)

    def test_superseded_round_is_not_actuated(self):
        """A newer dispatch round opened: the recovery describes a round that is over."""
        jid = self.produce()
        self.src.entries.append(ir("81000", generation=2))
        out = self.admit(jid)
        self.assertEqual(out.outcome, ADMIT_SUPERSEDED, out.detail)
        self.assertFalse(out.may_actuate)

    def test_opaque_post_anchor_journal_refuses_actuation(self):
        """A stall that is no longer PROVABLE is not actuated: unprovable is not proven."""
        jid = self.produce()
        self.src.entries.append(entry("80900", "## Gate: review\n\n(prose body, no marker)"))
        out = self.admit(jid)
        self.assertEqual(out.outcome, ADMIT_SUPERSEDED, out.detail)
        self.assertFalse(out.may_actuate)

    def test_superseded_does_not_consume_the_key(self):
        """A refusal must not burn the key: nothing was actuated, so nothing was admitted.

        Claims are never reclaimed, so a key burned on a superseded delivery could never be
        admitted afterwards — the refusal would permanently destroy a legitimate action.
        """
        jid = self.produce()
        self.src.entries.append(ir("81000", generation=2))
        self.assertEqual(self.admit(jid).outcome, ADMIT_SUPERSEDED)

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.callback_recovery_key import (  # noqa: E501
            resolve_recovery_action_key,
        )

        key = resolve_recovery_action_key(
            self.src.read_entries(ISSUE), recovery_action_journal=jid
        ).key
        self.assertEqual(self.receipt.peek(key), RECEIPT_ABSENT)


class DriftTests(_AdmissionBase):
    def test_route_drift_is_a_conflict(self):
        """Required: a delivery addressed to another route is never admitted here."""
        jid = self.produce()
        out = self.admit(jid, route="someone-else-worker-9")
        self.assertEqual(out.outcome, ADMIT_CONFLICT, out.detail)
        self.assertFalse(out.may_actuate)
        self.assertIn("route_identity", out.detail)

    def test_receiver_drift_is_a_conflict(self):
        jid = self.produce()
        out = self.admit(jid, receiver="claude")
        self.assertEqual(out.outcome, ADMIT_CONFLICT, out.detail)
        self.assertIn("receiver_identity", out.detail)

    def test_workspace_drift_is_a_conflict(self):
        """The key is workspace-partitioned: a foreign workspace must not admit this action."""
        jid = self.produce()
        out = self.admit(jid, workspace="ws-2")
        self.assertEqual(out.outcome, ADMIT_CONFLICT, out.detail)
        self.assertIn("workspace_id", out.detail)

    def test_drift_does_not_consume_the_key(self):
        """A misrouted delivery must not destroy the real receiver's ability to admit."""
        jid = self.produce()
        self.assertEqual(self.admit(jid, route="wrong-worker").outcome, ADMIT_CONFLICT)
        self.assertEqual(self.admit(jid).outcome, ADMIT_ADMITTED)


class FailClosedTests(_AdmissionBase):
    def test_snapshot_source_cannot_admit(self):
        """A frozen read cannot show a gate that landed after it: it must not authorize an effect."""

        class Snapshot:
            def __init__(self, entries):
                self._e = list(entries)

            def read_entries(self, issue):
                return list(self._e)

        jid = self.produce()
        out = self.admit(jid, src=Snapshot(self.src.entries))
        self.assertEqual(out.outcome, ADMIT_UNREADABLE, out.detail)
        self.assertIn("fresh_read", out.detail)

    def test_unreadable_source_is_fail_closed(self):
        class Broken:
            fresh_read = True

            def read_entries(self, issue):
                raise RuntimeError("redmine down")

        out = self.admit("80500", src=Broken())
        self.assertEqual(out.outcome, ADMIT_UNREADABLE, out.detail)
        self.assertFalse(out.may_actuate)

    def test_torn_authority_is_fail_closed_not_unclaimed(self):
        """Required: a lost store is "I cannot tell you", never "nothing was claimed"."""
        jid = self.produce()
        self.assertEqual(self.admit(jid).outcome, ADMIT_ADMITTED)
        # The DB is lost while its identity sidecar survives (a replacement / deletion).
        self.receipt.path.unlink()
        out = self.admit(jid, receipt=CallbackRecoveryReceipt(home=self.home))
        self.assertEqual(out.outcome, ADMIT_UNREADABLE, out.detail)
        self.assertFalse(
            out.may_actuate, "a lost store must never re-admit an already-actuated recovery"
        )

    def test_never_bootstrapped_authority_is_fail_closed(self):
        jid = self.produce()
        empty = Path(tempfile.mkdtemp())
        out = self.admit(jid, receipt=CallbackRecoveryReceipt(home=empty))
        self.assertEqual(out.outcome, ADMIT_UNREADABLE, out.detail)

    def test_unknown_journal_is_fail_closed(self):
        out = self.admit("99999")
        self.assertEqual(out.outcome, ADMIT_UNREADABLE, out.detail)
        self.assertFalse(out.may_actuate)


class NonRecoveryRecordTests(_AdmissionBase):
    def test_a_zero_send_resolution_record_is_not_admissible(self):
        """Required: a non-recovery record carries no action, so there is nothing to actuate.

        The recorder writes a record for zero-send resolutions too. Those must NOT carry an
        admission key — a receiver able to admit one would actuate a recovery that was deliberately
        never sent.
        """
        rec = build_recovery_recorder(
            source=self.src, issue=ISSUE, lane=LANE, lane_generation=GEN,
            post_note=self.src.post, grant_is_live=lambda: True,
            publication_fence=self.pubfence, workspace_id=WS,
            route_identity=TARGET, receiver_identity=RECEIVER,
        )
        wm = resolve_watermark(
            self.src.read_entries(ISSUE), dispatch_journal=ANCHOR, lane=LANE,
            lane_generation=GEN, latest_generation=GEN,
        )
        jid = rec({"state": "progress_without_callback", "dispatch_journal": ANCHOR}, wm)
        self.assertTrue(jid)

        out = self.admit(jid)
        self.assertEqual(out.outcome, ADMIT_UNREADABLE, out.detail)
        self.assertFalse(out.may_actuate)

    def test_the_action_marker_does_not_make_the_sweep_record_opaque(self):
        """Non-regression for #13889: the sweep must not silence itself with its own record.

        The record now carries a SECOND marker. If that made the entry unrecognized, every later
        sweep would read its own record as opaque prose and abstain forever
        (``callback_sweep_watermark`` names this exact failure). It must be classified, and it must
        still not count as progress.
        """
        jid = self.produce()
        entries = self.src.read_entries(ISSUE)
        anchor = resolve_dispatch_entry_journal(entries, lane=LANE, lane_generation=GEN)
        wm = resolve_watermark(
            entries,
            dispatch_journal=anchor,
            lane=LANE,
            lane_generation=GEN,
            latest_generation=(dispatch_generations(entries, lane=LANE) or (0,))[-1],
        )
        self.assertNotIn(jid, wm.opaque, "the recovery record must not read as opaque prose")
        self.assertEqual(wm.progress, (), "the coordinator's own record is never worker progress")
        self.assertTrue(wm.stall_provable, "the sweep must not be silenced by its own record")


if __name__ == "__main__":
    unittest.main()
