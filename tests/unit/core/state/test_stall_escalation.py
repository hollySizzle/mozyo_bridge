"""Stall escalation store tests (Redmine #15855).

The local durability under the escalation gate — explicitly *not* the durable escalation
record, which j#110121-5 fixed as a Redmine ``## Gate: blocked`` journal. What is checked
here is what only this layer can guarantee: runs keyed by the durable slot rather than by a
recycled pane locator, an idempotency key derived from the run instead of the firing
clock, and the two SQL-level fences that keep the pending lifecycle honest (a blank journal
id is not "written"; a firing with no journal id cannot be "woken").

Mirrors the sibling home-scoped store test (``test_supervisor_wake.py``): a real temp-dir
SQLite file, no other collaborator.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.stall_escalation import (
    DISCOVERY_BAD_COUNT,
    DISCOVERY_BAD_REASON_TOKEN,
    DISCOVERY_BAD_TIMESTAMP,
    DISCOVERY_INCONSISTENT,
    DISCOVERY_MALFORMED,
    STALL_ESCALATION_SCHEMA_VERSION,
    TIMESTAMP_UNREADABLE,
    StallDiscoveryContractError,
    PendingEscalation,
    StallEscalationStore,
    StallEscalationStoreError,
    StreakRow,
    escalation_idempotency_key,
    stall_escalation_path,
)

#: Real tz-aware instants: the store now enforces an ISO-8601 timestamp grammar, so a
#: pseudo-stamp like "t1" is (correctly) refused. Order is preserved so every
#: ordering / fairness assertion keeps its meaning.
ISO = {f"t{i}": f"2026-08-22T09:0{i}:00+00:00" for i in range(10)}

WS = "wsA"
LANE = "issue_15855_stall_wiring"
ROLE = "claude"
SLOT = (WS, LANE, ROLE)


def _row(*, lane_id=LANE, role=ROLE, generation="g1", target="w1V:pK",
         stall_class="content_refusal", consecutive=1, escalated_at=""):
    return StreakRow(
        workspace_id=WS,
        lane_id=lane_id,
        role=role,
        generation=generation,
        target=target,
        stall_class=stall_class,
        consecutive=consecutive,
        first_observed_at="2026-08-22T09:01:00+00:00",
        last_observed_at="2026-08-22T09:01:00+00:00",
        escalated_at=escalated_at,
    )


def _key(**overrides):
    base = dict(
        workspace_id=WS,
        lane_id=LANE,
        role=ROLE,
        generation="g1",
        stall_class="content_refusal",
        first_observed_at="2026-08-22T09:01:00+00:00",
    )
    base.update(overrides)
    return escalation_idempotency_key(**base)


def _pending(*, idempotency_key=None, lane_id=LANE, issue="15855", escalated_at="2026-08-22T09:02:00+00:00",
             **overrides):
    base = dict(
        idempotency_key=(
            _key(lane_id=lane_id) if idempotency_key is None else idempotency_key
        ),
        workspace_id=WS,
        lane_id=lane_id,
        role=ROLE,
        generation="g1",
        target="w1V:pK",
        issue=issue,
        stall_class="content_refusal",
        prescription="context_reset_reinjection",
        consecutive=2,
        first_observed_at="2026-08-22T09:01:00+00:00",
        escalated_at=escalated_at,
    )
    base.update(overrides)
    return PendingEscalation(**base)


class StoreBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = StallEscalationStore(path=self.dir / "stall-escalation.sqlite")


class PathTest(unittest.TestCase):
    def test_path_is_home_scoped(self) -> None:
        home = Path("/tmp/some-home")
        self.assertEqual(stall_escalation_path(home), home / "stall-escalation.sqlite")


class IdempotencyKeyTest(unittest.TestCase):
    def test_the_same_run_yields_the_same_key(self) -> None:
        self.assertEqual(_key(), _key())

    def test_the_key_does_not_depend_on_the_firing_clock(self) -> None:
        # Deriving it from escalated_at would make every retry look like a new escalation
        # and write a duplicate journal -- the failure the readback fence exists to stop.
        self.assertEqual(_key(), _key())
        self.assertNotIn(ISO["t2"], _key())

    def test_a_different_run_yields_a_different_key(self) -> None:
        base = _key()
        for field, value in (
            ("workspace_id", "wsB"),
            ("lane_id", "issue_99999"),
            ("role", "codex"),
            ("generation", "g2"),
            ("stall_class", "unsent_composer"),
            ("first_observed_at", ISO["t9"]),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(base, _key(**{field: value}))

    def test_the_key_is_a_versioned_opaque_token(self) -> None:
        # Versioned so a future derivation change is distinguishable rather than colliding.
        self.assertTrue(_key().startswith("stallesc1_"))


class AbsentStoreTest(StoreBase):
    def test_construction_touches_no_filesystem(self) -> None:
        self.assertFalse(self.store.path.exists())

    def test_reads_on_an_absent_db_are_empty_not_errors(self) -> None:
        self.assertEqual(self.store.read_streaks(WS), {})
        self.assertEqual(self.store.open_pending(), ())
        self.assertEqual(self.store.unrecorded_pending(), ())
        self.assertEqual(self.store.unwoken_pending(), ())
        self.assertEqual(self.store.forget_absent_slots(WS, []), 0)
        self.assertFalse(self.store.mark_recorded(_key(), "9"))
        self.assertFalse(self.store.mark_woken(_key()))
        self.assertFalse(self.store.record_attempt(_key(), "x"))
        self.store.clear_streak(SLOT)
        self.assertFalse(self.store.path.exists())


class StreakTest(StoreBase):
    def test_write_then_read_round_trips(self) -> None:
        self.store.write_streak(_row(consecutive=3, escalated_at="2026-08-22T09:09:00+00:00"))
        streaks = self.store.read_streaks(WS)
        self.assertEqual(set(streaks), {SLOT})
        row = streaks[SLOT]
        self.assertEqual(row.stall_class, "content_refusal")
        self.assertEqual(row.consecutive, 3)
        self.assertEqual(row.escalated_at, "2026-08-22T09:09:00+00:00")
        self.assertEqual(row.generation, "g1")
        self.assertEqual(row.target, "w1V:pK")

    def test_the_locator_is_not_part_of_the_key(self) -> None:
        # A rebound pane must update the same run, not create a second one.
        self.store.write_streak(_row(target="w1V:pK", consecutive=1))
        self.store.write_streak(_row(target="w9Q:pZ", consecutive=2))
        streaks = self.store.read_streaks(WS)
        self.assertEqual(len(streaks), 1)
        self.assertEqual(streaks[SLOT].consecutive, 2)
        self.assertEqual(streaks[SLOT].target, "w9Q:pZ")

    def test_the_generation_is_not_part_of_the_key(self) -> None:
        # Keying on generation would strand a row per relaunch.
        self.store.write_streak(_row(generation="g1"))
        self.store.write_streak(_row(generation="g2", consecutive=1))
        streaks = self.store.read_streaks(WS)
        self.assertEqual(len(streaks), 1)
        self.assertEqual(streaks[SLOT].generation, "g2")

    def test_role_and_lane_are_part_of_the_key(self) -> None:
        self.store.write_streak(_row(role="claude"))
        self.store.write_streak(_row(role="codex"))
        self.store.write_streak(_row(lane_id="issue_99999"))
        self.assertEqual(len(self.store.read_streaks(WS)), 3)

    def test_clear_removes_only_the_named_slot(self) -> None:
        self.store.write_streak(_row(role="claude"))
        self.store.write_streak(_row(role="codex"))
        self.store.clear_streak((WS, LANE, "claude"))
        self.assertEqual(set(self.store.read_streaks(WS)), {(WS, LANE, "codex")})

    def test_blank_identity_is_a_no_op(self) -> None:
        self.store.write_streak(_row(lane_id=""))
        self.store.write_streak(_row(role=""))
        self.assertEqual(self.store.read_streaks(WS), {})


class ForgetAbsentTest(StoreBase):
    def test_slots_absent_from_the_live_inventory_are_dropped(self) -> None:
        self.store.write_streak(_row(role="claude"))
        self.store.write_streak(_row(role="codex"))
        dropped = self.store.forget_absent_slots(WS, [(WS, LANE, "claude")])
        self.assertEqual(dropped, 1)
        self.assertEqual(set(self.store.read_streaks(WS)), {(WS, LANE, "claude")})

    def test_other_workspaces_are_untouched(self) -> None:
        self.store.write_streak(_row())
        self.store.write_streak(
            StreakRow(
                workspace_id="wsB", lane_id=LANE, role=ROLE, stall_class="content_refusal",
                consecutive=1, first_observed_at="2026-08-22T09:01:00+00:00", last_observed_at="2026-08-22T09:01:00+00:00",
            )
        )
        self.store.forget_absent_slots(WS, [])
        self.assertEqual(len(self.store.read_streaks("wsB")), 1)

    def test_pending_escalations_survive_the_slot_going_away(self) -> None:
        # The stall happened; dropping the record of it because the pane was retired would
        # erase the very fact the watcher exists to preserve.
        self.store.enqueue_pending(_pending())
        self.store.write_streak(_row())
        self.store.forget_absent_slots(WS, [])
        self.assertEqual(self.store.read_streaks(WS), {})
        self.assertEqual(len(self.store.open_pending(WS)), 1)


class PendingLifecycleTest(StoreBase):
    def test_enqueue_then_read_round_trips_every_field(self) -> None:
        self.assertTrue(
            self.store.enqueue_pending(_pending(matched_id="sig-1", evidence_tier="rendered_confirmed"))
        )
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.lane_id, LANE)
        self.assertEqual(pending.role, ROLE)
        self.assertEqual(pending.generation, "g1")
        self.assertEqual(pending.target, "w1V:pK")
        self.assertEqual(pending.issue, "15855")
        self.assertEqual(pending.stall_class, "content_refusal")
        self.assertEqual(pending.prescription, "context_reset_reinjection")
        self.assertEqual(pending.matched_id, "sig-1")
        self.assertEqual(pending.evidence_tier, "rendered_confirmed")
        self.assertEqual(pending.consecutive, 2)
        self.assertFalse(pending.recorded)
        self.assertFalse(pending.settled)

    def test_the_same_firing_is_idempotent(self) -> None:
        self.assertTrue(self.store.enqueue_pending(_pending()))
        self.assertFalse(self.store.enqueue_pending(_pending(escalated_at="2026-08-22T09:59:00+00:00")))
        self.assertEqual(len(self.store.unrecorded_pending(WS)), 1)

    def test_a_firing_without_an_identity_is_refused(self) -> None:
        self.assertFalse(self.store.enqueue_pending(_pending(idempotency_key="")))
        self.assertFalse(self.store.enqueue_pending(_pending(workspace_id="")))

    def test_recording_binds_the_journal_and_moves_it_to_unwoken(self) -> None:
        self.store.enqueue_pending(_pending())
        self.assertTrue(self.store.mark_recorded(_key(), "110130", now="2026-08-22T09:05:00+00:00"))
        self.assertEqual(self.store.unrecorded_pending(WS), ())
        (pending,) = self.store.unwoken_pending(WS)
        self.assertEqual(pending.journal_id, "110130")
        self.assertEqual(pending.written_at, "2026-08-22T09:05:00+00:00")
        self.assertTrue(pending.recorded)
        self.assertFalse(pending.settled)

    def test_a_blank_journal_id_is_not_a_recording(self) -> None:
        # "It probably wrote" with no readback is exactly the uncertain state that would
        # let the next pass write a duplicate.
        self.store.enqueue_pending(_pending())
        self.assertFalse(self.store.mark_recorded(_key(), ""))
        self.assertFalse(self.store.mark_recorded(_key(), "   "))
        self.assertEqual(len(self.store.unrecorded_pending(WS)), 1)

    def test_recording_twice_reports_false(self) -> None:
        self.store.enqueue_pending(_pending())
        self.store.mark_recorded(_key(), "110130")
        self.assertFalse(self.store.mark_recorded(_key(), "110131"))
        (pending,) = self.store.unwoken_pending(WS)
        self.assertEqual(pending.journal_id, "110130")

    def test_waking_an_unrecorded_firing_is_refused_in_sql(self) -> None:
        # Waking a coordinator to read a journal that does not exist is the one inversion
        # this rail is built to prevent, so the store refuses it rather than trusting the
        # caller's ordering.
        self.store.enqueue_pending(_pending())
        self.assertFalse(self.store.mark_woken(_key()))
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.woke_at, "")

    def test_waking_a_recorded_firing_settles_it(self) -> None:
        self.store.enqueue_pending(_pending())
        self.store.mark_recorded(_key(), "110130")
        self.assertTrue(self.store.mark_woken(_key(), now="2026-08-22T09:06:00+00:00"))
        self.assertEqual(self.store.unwoken_pending(WS), ())
        self.assertEqual(self.store.open_pending(WS), ())

    def test_waking_twice_reports_false(self) -> None:
        self.store.enqueue_pending(_pending())
        self.store.mark_recorded(_key(), "110130")
        self.store.mark_woken(_key())
        self.assertFalse(self.store.mark_woken(_key()))


class AttemptVisibilityTest(StoreBase):
    def test_a_refused_write_is_counted_with_its_reason(self) -> None:
        # A repeatedly-refused write must not look like an escalation nobody reached yet.
        self.store.enqueue_pending(_pending())
        self.assertTrue(self.store.record_attempt(_key(), "write_optin_unset", now="2026-08-22T09:03:00+00:00"))
        self.assertTrue(self.store.record_attempt(_key(), "credential_missing", now="2026-08-22T09:04:00+00:00"))
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.attempts, 2)
        self.assertEqual(pending.last_reason, "credential_missing")
        self.assertEqual(pending.last_attempt_at, "2026-08-22T09:04:00+00:00")

    def test_attempts_are_not_counted_against_a_recorded_firing(self) -> None:
        self.store.enqueue_pending(_pending())
        self.store.mark_recorded(_key(), "110130")
        self.assertFalse(self.store.record_attempt(_key(), "late"))

    def test_recording_clears_the_stale_refusal_reason(self) -> None:
        self.store.enqueue_pending(_pending())
        self.store.record_attempt(_key(), "write_optin_unset")
        self.store.mark_recorded(_key(), "110130")
        (pending,) = self.store.unwoken_pending(WS)
        self.assertEqual(pending.last_reason, "")


class FairnessOrderTest(StoreBase):
    def test_unrecorded_pending_is_oldest_first(self) -> None:
        # Whichever escalation has waited longest takes the next write slot, so a newer
        # stall cannot repeatedly overtake an older one.
        self.store.enqueue_pending(_pending(lane_id="lane_c", escalated_at="2026-08-22T09:07:00+00:00"))
        self.store.enqueue_pending(_pending(lane_id="lane_a", escalated_at="2026-08-22T09:01:00+00:00"))
        self.store.enqueue_pending(_pending(lane_id="lane_b", escalated_at="2026-08-22T09:04:00+00:00"))
        order = [p.lane_id for p in self.store.unrecorded_pending(WS)]
        self.assertEqual(order, ["lane_a", "lane_b", "lane_c"])

    def test_pending_is_scoped_per_workspace_when_asked(self) -> None:
        self.store.enqueue_pending(_pending())
        self.store.enqueue_pending(
            _pending(idempotency_key="other", workspace_id="wsB")
        )
        self.assertEqual(len(self.store.unrecorded_pending()), 2)
        self.assertEqual(len(self.store.unrecorded_pending("wsB")), 1)


class DiscoveryCoverageTest(StoreBase):
    """Coverage counts, and the difference between "never ran" and "ran, saw nothing"."""

    def test_record_then_read_round_trips(self) -> None:
        # The counts satisfy the identity the store enforces: candidates == watched +
        # sum(dropped), and out_of_reach == sum(dropped) - foreign_workspace.
        self.store.record_discovery(
            "wsA", candidates=4, watched=2, out_of_reach=2,
            dropped={"issue_anchor_unresolved": 2}, now="2026-08-22T09:01:00+00:00",
        )
        self.assertEqual(
            self.store.last_discovery("wsA"),
            {
                "observed_at": "2026-08-22T09:01:00+00:00",
                "candidates": 4,
                "watched": 2,
                "out_of_reach": 2,
                "dropped": {"issue_anchor_unresolved": 2},
            },
        )

    def test_a_workspace_with_no_row_is_None_even_though_the_db_exists(self) -> None:
        # The discriminating case: another workspace HAS recorded, so the file and the
        # table are both present. "Never ran" must still be None rather than zeros --
        # zeros would claim this watcher ran and saw nothing.
        self.store.record_discovery("wsB", candidates=1, watched=1, out_of_reach=0)
        self.assertTrue(self.store.path.exists())
        self.assertIsNone(self.store.last_discovery("wsA"))

    def test_a_later_pass_replaces_the_earlier_summary(self) -> None:
        self.store.record_discovery("wsA", candidates=1, watched=1, out_of_reach=0, now="2026-08-22T09:01:00+00:00")
        self.store.record_discovery(
            "wsA", candidates=9, watched=3, out_of_reach=6,
            dropped={"issue_anchor_unresolved": 6}, now="2026-08-22T09:02:00+00:00",
        )
        recorded = self.store.last_discovery("wsA")
        self.assertEqual(recorded["observed_at"], "2026-08-22T09:02:00+00:00")
        self.assertEqual(recorded["out_of_reach"], 6)

    def test_a_blank_workspace_records_nothing(self) -> None:
        self.store.record_discovery("", candidates=1, watched=1, out_of_reach=0)
        self.assertIsNone(self.store.last_discovery(""))

class DiscoveryContractBase(StoreBase):
    """Shared fixtures for the two contract suites (no tests of its own)."""

    def _valid(self, **overrides):
        base = dict(
            candidates=4,
            watched=1,
            out_of_reach=2,
            dropped={
                "foreign_workspace": 1,
                "issue_anchor_unresolved": 1,
                "no_live_locator": 1,
            },
        )
        base.update(overrides)
        return base

    def _seed_valid(self):
        self.store.record_discovery("wsA", now="2026-08-22T09:01:00+00:00", **self._valid())

    def _corrupt(self, sql, *args):
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()


class DiscoveryWriteContractTest(DiscoveryContractBase):
    """The WRITE seam is public, so the closed contract is enforced there, not assumed.

    The module docstring declares that every stored value is a fixed classification token or
    a count, "which is what makes a row safe to render verbatim". Review j#110169 showed the
    declaration was enforced at neither boundary: an arbitrary path and a negative count
    passed straight through to `--status`.
    """

    def test_an_off_vocabulary_reason_is_refused(self) -> None:
        with self.assertRaises(StallDiscoveryContractError):
            self.store.record_discovery(
                "wsA",
                **self._valid(
                    candidates=10,
                    watched=1,
                    out_of_reach=9,
                    dropped={"/home/alice/private/secret.yml": 9},
                ),
            )
        self.assertIsNone(self.store.last_discovery("wsA"))

    def test_the_refusal_does_not_quote_the_offending_string(self) -> None:
        # Quoting it would put the very string this check exists to contain into a log.
        with self.assertRaises(StallDiscoveryContractError) as caught:
            self.store.record_discovery(
                "wsA",
                **self._valid(
                    candidates=10, watched=1, out_of_reach=9,
                    dropped={"/home/alice/private/secret.yml": 9},
                ),
            )
        self.assertNotIn("secret.yml", str(caught.exception))
        self.assertNotIn("/home/alice", str(caught.exception))

    def test_negative_counts_are_refused(self) -> None:
        for field in ("candidates", "watched", "out_of_reach"):
            with self.subTest(field=field):
                with self.assertRaises(StallDiscoveryContractError):
                    self.store.record_discovery("wsA", **self._valid(**{field: -3}))

    def test_a_negative_count_inside_dropped_is_refused(self) -> None:
        with self.assertRaises(StallDiscoveryContractError):
            self.store.record_discovery(
                "wsA",
                **self._valid(
                    candidates=1, watched=4, out_of_reach=-3,
                    dropped={"issue_anchor_unresolved": -3},
                ),
            )

    def test_a_boolean_is_not_a_count(self) -> None:
        # bool is an int subclass; `watched=True` is a mistake, not the number 1.
        #
        # The counts below are chosen so they would be CONSISTENT if the bool were silently
        # coerced (1 == 1 + 0). An earlier version of this test used counts that broke the
        # identity too, so it passed for the wrong reason and left the bool check untested.
        with self.assertRaises(StallDiscoveryContractError) as caught:
            self.store.record_discovery(
                "wsA", candidates=1, watched=True, out_of_reach=0, dropped={}
            )
        self.assertIn("bool", str(caught.exception))

    def test_a_boolean_inside_dropped_is_not_a_count(self) -> None:
        with self.assertRaises(StallDiscoveryContractError) as caught:
            self.store.record_discovery(
                "wsA", candidates=1, watched=0, out_of_reach=1,
                dropped={"no_live_locator": True},
            )
        self.assertIn("bool", str(caught.exception))

    def test_counts_that_disagree_are_refused(self) -> None:
        # candidates == watched + sum(dropped) holds in the producer by construction, so a
        # row that breaks it is not a row this rail wrote.
        with self.assertRaises(StallDiscoveryContractError):
            self.store.record_discovery("wsA", **self._valid(candidates=99))
        with self.assertRaises(StallDiscoveryContractError):
            self.store.record_discovery("wsA", **self._valid(out_of_reach=3))

    def test_a_non_mapping_dropped_is_refused(self) -> None:
        with self.assertRaises(StallDiscoveryContractError):
            self.store.record_discovery("wsA", **self._valid(dropped=["a"]))

    def test_a_valid_summary_still_round_trips(self) -> None:
        self._seed_valid()
        self.assertEqual(
            self.store.last_discovery("wsA"),
            {
                "observed_at": "2026-08-22T09:01:00+00:00",
                "candidates": 4,
                "watched": 1,
                "out_of_reach": 2,
                "dropped": {
                    "foreign_workspace": 1,
                    "issue_anchor_unresolved": 1,
                    "no_live_locator": 1,
                },
            },
        )


class TimestampContractTest(DiscoveryContractBase):
    """The fifth column. R4 validated counts and reasons and left this one unchecked.

    "It is a timestamp" was an assumption nothing enforced, so it was whatever the caller
    passed — review j#110183 reproduced ``/private/example/unsafe-observed-at`` reaching both
    operator surfaces. The grammar is now enforced at write AND read, for every timestamp
    this store renders, not only the one the finding named.
    """

    GOOD = "2026-08-22T13:00:00+00:00"

    def test_a_non_timestamp_write_is_refused(self) -> None:
        for label, now in (
            ("path sentinel", "/private/example/unsafe-observed-at"),
            ("free text", "hello"),
            ("naive instant", "2026-08-22T13:00:00"),
            ("empty string", ""),
            ("not a string", 12345),
        ):
            with self.subTest(label=label):
                with self.assertRaises(StallDiscoveryContractError):
                    self.store.record_discovery("wsA", now=now, **self._valid())
        self.assertIsNone(self.store.last_discovery("wsA"))

    def test_an_explicit_empty_string_is_refused_not_defaulted(self) -> None:
        # `now or default` would silently repair it — the same "repair instead of refuse"
        # pattern the config resolver was corrected on.
        with self.assertRaises(StallDiscoveryContractError):
            self.store.record_discovery("wsA", now="", **self._valid())

    def test_none_still_means_use_the_current_time(self) -> None:
        self.store.record_discovery("wsA", **self._valid())
        self.assertTrue(self.store.last_discovery("wsA")["observed_at"])

    def test_the_refusal_does_not_quote_the_supplied_value(self) -> None:
        with self.assertRaises(StallDiscoveryContractError) as caught:
            self.store.record_discovery(
                "wsA", now="/private/example/unsafe-observed-at", **self._valid()
            )
        self.assertNotIn("unsafe-observed-at", str(caught.exception))
        self.assertNotIn("/private", str(caught.exception))

    def test_an_accepted_timestamp_is_normalized(self) -> None:
        # "parses but is written oddly" cannot survive either: what comes back out is what
        # this module would have written itself.
        self.store.record_discovery(
            "wsA", now="2026-08-22T13:00:00.123456+00:00", **self._valid()
        )
        self.assertEqual(self.store.last_discovery("wsA")["observed_at"], self.GOOD)

    def test_a_corrupted_stored_timestamp_is_not_rendered(self) -> None:
        self.store.record_discovery("wsA", now=self.GOOD, **self._valid())
        self._corrupt("UPDATE stall_watch_discovery SET observed_at='/etc/shadow'")
        got = self.store.last_discovery("wsA")
        self.assertEqual(got["unreadable"], DISCOVERY_BAD_TIMESTAMP)
        self.assertNotIn("shadow", json.dumps(got))
        self.assertEqual(got["observed_at"], "")

    def test_pending_timestamps_are_refused_on_write(self) -> None:
        # Stronger exposure than the discovery row: these reach a Redmine journal BODY.
        for field in ("first_observed_at", "escalated_at"):
            with self.subTest(field=field):
                kwargs = {"first_observed_at": self.GOOD, "escalated_at": self.GOOD}
                kwargs[field] = "/home/alice/private/secret"
                with self.assertRaises(StallDiscoveryContractError):
                    self.store.enqueue_pending(_pending(**kwargs))

    def test_a_corrupted_pending_timestamp_is_replaced_not_dropped(self) -> None:
        # The row IS the escalation; dropping it would lose the stall report. The value is
        # replaced so nothing arbitrary reaches the note or the JSON status.
        self.store.enqueue_pending(
            _pending(first_observed_at=self.GOOD, escalated_at=self.GOOD)
        )
        self._corrupt(
            "UPDATE stall_escalation_pending SET first_observed_at='/etc/shadow'"
        )
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.first_observed_at, TIMESTAMP_UNREADABLE)
        self.assertNotIn("shadow", json.dumps(pending.telemetry()))

    def test_the_watermark_is_held_to_the_same_grammar(self) -> None:
        # `last_pass_at` reaches `--status` as `last=`, so it is the same class of column.
        with self.assertRaises(StallDiscoveryContractError):
            self.store.mark_pass("wsA", now="/private/x")
        self.store.mark_pass("wsA", now=self.GOOD)
        self.assertEqual(self.store.last_pass_at("wsA"), self.GOOD)
        self._corrupt("UPDATE stall_watch_watermark SET last_pass_at='/etc/shadow'")
        self.assertEqual(self.store.last_pass_at("wsA"), TIMESTAMP_UNREADABLE)


class DiscoveryReadContractTest(DiscoveryContractBase):
    """A store is not a trust boundary: the durable row is re-validated on the way OUT.

    An older build, a hand-edited DB or a partially-written row can all hold values this
    build's contract forbids, and the READ path is what feeds ``--status``.
    """

    def _unreadable(self):
        got = self.store.last_discovery("wsA")
        self.assertIsNotNone(got)
        return got

    def test_an_off_vocabulary_reason_in_the_db_is_not_rendered(self) -> None:
        self._seed_valid()
        self._corrupt(
            "UPDATE stall_watch_discovery SET dropped=?", '{"/etc/shadow": 1}'
        )
        got = self._unreadable()
        self.assertEqual(got["unreadable"], DISCOVERY_BAD_REASON_TOKEN)
        self.assertNotIn("shadow", json.dumps(got))

    def test_a_negative_count_in_the_db_is_not_rendered(self) -> None:
        self._seed_valid()
        self._corrupt("UPDATE stall_watch_discovery SET out_of_reach=-3")
        got = self._unreadable()
        self.assertEqual(got["unreadable"], DISCOVERY_BAD_COUNT)
        self.assertNotIn("-3", json.dumps(got))

    def test_counts_that_disagree_in_the_db_are_not_rendered(self) -> None:
        self._seed_valid()
        self._corrupt("UPDATE stall_watch_discovery SET candidates=99")
        got = self._unreadable()
        self.assertEqual(got["unreadable"], DISCOVERY_INCONSISTENT)
        self.assertNotIn("99", json.dumps(got))

    def test_malformed_json_in_the_db_is_not_rendered(self) -> None:
        self._seed_valid()
        self._corrupt("UPDATE stall_watch_discovery SET dropped='not json'")
        self.assertEqual(self._unreadable()["unreadable"], DISCOVERY_MALFORMED)

    def test_no_stored_value_survives_a_rejected_row(self) -> None:
        # Including the timestamp: a row whose reasons are untrusted has an untrusted
        # observed_at too.
        self._seed_valid()
        self._corrupt("UPDATE stall_watch_discovery SET dropped=?", '{"/etc/shadow": 1}')
        got = self._unreadable()
        self.assertEqual(got["observed_at"], "")
        self.assertEqual(got["candidates"], 0)
        self.assertEqual(got["watched"], 0)
        self.assertEqual(got["out_of_reach"], 0)
        self.assertEqual(got["dropped"], {})

    def test_unreadable_is_distinct_from_never_run(self) -> None:
        # Different operator actions: "fix the store" vs "wait for a pass".
        self._seed_valid()
        self._corrupt("UPDATE stall_watch_discovery SET dropped='not json'")
        self.assertIsNotNone(self.store.last_discovery("wsA"))
        self.assertIsNone(self.store.last_discovery("wsZ"))

    def test_the_reject_token_vocabulary_is_closed(self) -> None:
        from mozyo_bridge.core.state.stall_escalation import unreadable_discovery

        for token in (
            DISCOVERY_BAD_REASON_TOKEN,
            DISCOVERY_BAD_COUNT,
            DISCOVERY_INCONSISTENT,
            DISCOVERY_MALFORMED,
        ):
            with self.subTest(token=token):
                shape = unreadable_discovery(token)
                self.assertEqual(shape["unreadable"], token)
                self.assertEqual(
                    set(shape),
                    {
                        "observed_at",
                        "candidates",
                        "watched",
                        "out_of_reach",
                        "dropped",
                        "unreadable",
                    },
                )


class SchemaTest(StoreBase):
    def test_version_is_stamped_on_creation(self) -> None:
        self.store.write_streak(_row())
        conn = sqlite3.connect(self.store.path)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(version, STALL_ESCALATION_SCHEMA_VERSION)

    def test_an_unrecognized_version_fails_closed_on_write(self) -> None:
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute(f"PRAGMA user_version = {STALL_ESCALATION_SCHEMA_VERSION + 41}")
        finally:
            conn.close()
        with self.assertRaises(StallEscalationStoreError):
            self.store.write_streak(_row())

    def test_an_unrecognized_version_fails_closed_on_read(self) -> None:
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute(f"PRAGMA user_version = {STALL_ESCALATION_SCHEMA_VERSION + 41}")
        finally:
            conn.close()
        with self.assertRaises(StallEscalationStoreError):
            self.store.read_streaks(WS)

    def test_a_table_lost_under_a_valid_version_self_heals(self) -> None:
        self.store.write_streak(_row())
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute("DROP TABLE stall_watch_streak")
        finally:
            conn.close()
        self.assertEqual(self.store.read_streaks(WS), {})
        self.store.write_streak(_row())
        self.assertEqual(set(self.store.read_streaks(WS)), {SLOT})


class HygieneTest(StoreBase):
    def test_no_column_can_hold_pane_text(self) -> None:
        # The property that makes a row safe to render verbatim into a Redmine journal:
        # every stored value is a token, an identity, a count or a timestamp.
        self.store.write_streak(_row())
        conn = sqlite3.connect(self.store.path)
        try:
            streak_cols = {r[1] for r in conn.execute("PRAGMA table_info(stall_watch_streak)")}
            pending_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(stall_escalation_pending)")
            }
        finally:
            conn.close()
        self.assertEqual(
            streak_cols,
            {
                "workspace_id", "lane_id", "role", "generation", "target", "stall_class",
                "consecutive", "first_observed_at", "last_observed_at", "escalated_at",
            },
        )
        self.assertEqual(
            pending_cols,
            {
                "idempotency_key", "workspace_id", "lane_id", "role", "generation",
                "target", "issue", "stall_class", "prescription", "matched_id",
                "evidence_tier", "consecutive", "first_observed_at", "escalated_at",
                "journal_id", "written_at", "woke_at", "attempts", "last_attempt_at",
                "last_reason",
            },
        )

    def test_pending_telemetry_omits_empty_optionals(self) -> None:
        payload = _pending(issue="", matched_id="").telemetry()
        self.assertEqual(payload["stall_class"], "content_refusal")
        self.assertEqual(payload["slot"], f"{WS}/{LANE}/{ROLE}")
        self.assertNotIn("issue", payload)
        self.assertNotIn("matched_id", payload)
        self.assertFalse(payload["recorded"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
