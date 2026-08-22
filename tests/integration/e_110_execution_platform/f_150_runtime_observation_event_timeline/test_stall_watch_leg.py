"""The stall-watch leg, end to end (Redmine #15855).

Every real collaborator this feature owns wired together — policy, cadence watermark,
discovery join, the #15843 sensor and classifier, the escalation gate, the durable store,
the note renderer, and the canonical-gate-write readback fence — driven by fake screens
across a sequence of passes. Only the three seams that leave the process are fakes: the
herdr screen read, the gate-record POST, and the wake enqueue.

The properties this file exists for are the ones no unit test can hold:

- a stall on a watched lane produces exactly one Redmine journal and one wake, in that
  order, and only after the journal id was read back;
- a firing whose journal already landed is never written twice, even when the local store
  believes it is unrecorded (the crash-after-POST case);
- the leg costs nothing on a tick inside the cadence — no screen is read at all;
- a watcher that cannot read does **not** mark its cadence, so it retries rather than
  silently skipping a window.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.stall_escalation import StallEscalationStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_leg import (  # noqa: E501
    LEG_DISABLED,
    LEG_NOTHING_TO_WATCH,
    LEG_NOT_DUE,
    LEG_NO_READER,
    LEG_RAN,
    build_journal_writer,
    journal_id_carrying_key,
    run_stall_watch_leg,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_body_marker import (  # noqa: E501
    resolve_body_marker,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_phase import (  # noqa: E501
    stall_watch_status,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_wiring import (  # noqa: E501
    CONFIG_UNREADABLE_DETAIL,
    POLICY_DETAIL_LIMIT,
    build_stall_watch_leg_fn,
    lane_facts_snapshot,
    resolve_stall_watch_policy,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_escalation_note import (  # noqa: E501
    STALL_ESCALATION_GATE,
    STALL_ESCALATION_REASON,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_watch_policy import (  # noqa: E501
    POLICY_ABSENT,
    POLICY_CONFIG_UNREADABLE,
    POLICY_INVALID,
    StallWatchPolicy,
)

WS = "wsA"
LANE = "lane_a"
LOCATOR = "w1V:pK"
T0 = datetime(2026, 8, 22, 9, 0, 0, tzinfo=timezone.utc)

#: A screen carrying a declared content-refusal signature would need the packaged registry;
#: a frozen, unmatched screen is enough here and lands on the patient indeterminate class,
#: which is an escalating class. What matters to this file is the wiring, not the verdict.
FROZEN_SCREEN = "some rendered output that never changes\n> \n"


class _Source:
    """In-memory Redmine journal source: what a readback actually sees."""

    def __init__(self) -> None:
        self.entries: list[RedmineJournalEntry] = []
        self.reads = 0

    def read_entries(self, issue_id):
        self.reads += 1
        return [e for e in self.entries if e.issue_id == str(issue_id)]

    def append(self, issue, notes, journal_id):
        self.entries.append(
            RedmineJournalEntry(issue_id=str(issue), journal_id=str(journal_id), notes=notes)
        )


class _Emit:
    """Fake canonical gate writer that lands the note in the fake source."""

    def __init__(self, source, *, recorded=True, reason="ok", land=True) -> None:
        self.source = source
        self.calls = []
        self.recorded = recorded
        self.reason = reason
        self.land = land
        self._next = 110130

    def __call__(self, issue, gate, *, body="", transport=None, marker_fields=None,
                 review_findings=None):
        self.calls.append({"issue": issue, "gate": gate, "body": body})
        if self.recorded and self.land:
            self.source.append(issue, body, self._next)
            self._next += 1

        class _Receipt:
            recorded = self.recorded
            reason = self.reason

        return _Receipt()


class _Wake:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, workspace_id, issue):
        self.calls.append((workspace_id, issue))
        return True


class LegBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = StallEscalationStore(path=self.dir / "stall-escalation.sqlite")
        self.policy = StallWatchPolicy.from_record({"lanes": [LANE]})
        self.source = _Source()
        self.emit = _Emit(self.source)
        self.wake = _Wake()
        self.reads = []
        self.clock_value = 0.0
        self.moment = T0

    def _rows(self):
        return [{"name": encode_assigned_name(WS, "claude", LANE), "pane_id": LOCATOR}]

    def _read(self, target):
        self.reads.append(target)
        return True, FROZEN_SCREEN

    def _tick(self, seconds=0.0):
        self.clock_value += seconds or 1.0
        return self.clock_value

    def run_leg(self, *, budget=None, read=None, at=None, **kwargs):
        writer = kwargs.pop(
            "write_journal",
            build_journal_writer(
                policy=self.policy,
                transport=object(),
                source=self.source,
                emit=self.emit,
            ),
        )
        return run_stall_watch_leg(
            workspace_id=WS,
            store=self.store,
            policy=kwargs.pop("policy", self.policy),
            inventory_rows=kwargs.pop("inventory_rows", self._rows),
            read_screen=self._read if read is None else read,
            write_journal=writer,
            wake=kwargs.pop("wake", self.wake),
            generation_for=kwargs.pop("generation_for", lambda lane: "g1"),
            issue_for=kwargs.pop("issue_for", lambda lane: "15855"),
            body_marker_for=kwargs.pop("body_marker_for", None),
            budget=budget,
            clock=self._tick,
            sleep=lambda _s: None,
            now=lambda: at or self.moment,
            sample_interval_seconds=0.0,
            **kwargs,
        )


class CadenceGateTest(LegBase):
    def test_a_disabled_policy_reads_no_screen(self) -> None:
        outcome = self.run_leg(policy=StallWatchPolicy.default())
        self.assertEqual(outcome.reason, LEG_DISABLED)
        self.assertEqual(self.reads, [])

    def test_a_tick_inside_the_cadence_reads_no_screen(self) -> None:
        self.run_leg()
        self.reads.clear()
        outcome = self.run_leg(at=T0 + timedelta(seconds=60))
        self.assertEqual(outcome.reason, LEG_NOT_DUE)
        self.assertEqual(self.reads, [])

    def test_the_next_tick_past_the_cadence_observes_again(self) -> None:
        self.run_leg()
        self.reads.clear()
        outcome = self.run_leg(at=T0 + timedelta(seconds=301))
        self.assertEqual(outcome.reason, LEG_RAN)
        self.assertTrue(self.reads)

    def test_nothing_in_scope_still_marks_the_cadence(self) -> None:
        # There was nothing to look at; that window WAS observed.
        outcome = self.run_leg(inventory_rows=lambda: [])
        self.assertEqual(outcome.reason, LEG_NOTHING_TO_WATCH)
        self.assertTrue(self.store.last_pass_at(WS))

    def test_a_blocked_reader_does_not_mark_the_cadence(self) -> None:
        # A watcher that cannot read is blocked, not quiet: the next tick must try again
        # rather than pretend this window was observed.
        outcome = self.run_leg(read=None if False else None, **{})
        self.assertEqual(outcome.reason, LEG_RAN)  # sanity: the normal path does run
        store2 = StallEscalationStore(path=self.dir / "other.sqlite")
        outcome = run_stall_watch_leg(
            workspace_id=WS,
            store=store2,
            policy=self.policy,
            inventory_rows=self._rows,
            read_screen=None,
            generation_for=lambda lane: "g1",
            issue_for=lambda lane: "15855",
            now=lambda: T0,
        )
        self.assertEqual(outcome.reason, LEG_NO_READER)
        self.assertEqual(store2.last_pass_at(WS), "")

    def test_a_blocked_reader_still_settles_the_backlog(self) -> None:
        # Writing an already-fired escalation needs a transport, not a screen. Letting a
        # broken reader also stop the durable write would strand exactly the reports this
        # rail exists to deliver, at the moment the cockpit is least healthy.
        self.run_leg()
        spent = {"reads": 0, "mutated": True, "uncertain": False}
        self.run_leg(at=T0 + timedelta(seconds=301), budget=spent)
        self.assertEqual(len(self.store.unrecorded_pending(WS)), 1)
        outcome = run_stall_watch_leg(
            workspace_id=WS,
            store=self.store,
            policy=self.policy,
            inventory_rows=self._rows,
            read_screen=None,
            write_journal=build_journal_writer(
                policy=self.policy, transport=object(), source=self.source, emit=self.emit
            ),
            wake=self.wake,
            generation_for=lambda lane: "g1",
            issue_for=lambda lane: "15855",
            now=lambda: T0 + timedelta(seconds=700),
        )
        self.assertEqual(outcome.reason, LEG_NO_READER)
        self.assertEqual(len(self.emit.calls), 1)
        self.assertEqual(self.store.unrecorded_pending(WS), ())


class EndToEndTest(LegBase):
    def test_two_frozen_passes_produce_one_journal_and_one_wake(self) -> None:
        first = self.run_leg()
        self.assertEqual(first.reason, LEG_RAN)
        self.assertEqual(first.observed.escalated, 0)
        self.assertEqual(self.emit.calls, [])

        second = self.run_leg(at=T0 + timedelta(seconds=301))
        self.assertEqual(second.observed.escalated, 1)
        self.assertEqual(len(self.emit.calls), 1)
        self.assertEqual(self.wake.calls, [(WS, "15855")])
        self.assertEqual(self.store.open_pending(WS), ())

    def test_the_journal_is_a_canonical_blocked_gate_note(self) -> None:
        self.run_leg()
        self.run_leg(at=T0 + timedelta(seconds=301))
        (call,) = self.emit.calls
        self.assertEqual(call["gate"], STALL_ESCALATION_GATE)
        self.assertEqual(call["issue"], "15855")
        body = call["body"]
        self.assertIn(f"## Gate: {STALL_ESCALATION_GATE}", body)
        self.assertIn(f"- reason: {STALL_ESCALATION_REASON}", body)
        self.assertIn(f"- slot: {WS}/{LANE}/claude", body)
        self.assertIn("- generation: g1", body)
        self.assertIn("- consecutive_detections: 2", body)
        self.assertIn("- policy: cadence=300s;threshold=2;source=repo_local_config", body)

    def test_the_note_carries_no_screen_text(self) -> None:
        self.run_leg()
        self.run_leg(at=T0 + timedelta(seconds=301))
        body = self.emit.calls[0]["body"]
        self.assertNotIn("rendered output", body)
        self.assertIn("No pane content is carried in this record", body)

    def test_a_persisting_stall_writes_exactly_one_journal(self) -> None:
        for index in range(10):
            self.run_leg(at=T0 + timedelta(seconds=301 * index))
        self.assertEqual(len(self.emit.calls), 1)
        self.assertEqual(len(self.wake.calls), 1)

    def test_the_operator_threshold_is_honoured_end_to_end(self) -> None:
        self.policy = StallWatchPolicy.from_record({"lanes": [LANE], "threshold": 4})
        for index in range(3):
            self.run_leg(at=T0 + timedelta(seconds=301 * index))
        self.assertEqual(self.emit.calls, [])
        self.run_leg(at=T0 + timedelta(seconds=301 * 3))
        self.assertEqual(len(self.emit.calls), 1)


class BudgetTest(LegBase):
    def test_a_spent_budget_defers_the_journal_to_a_later_pass(self) -> None:
        self.run_leg()
        spent = {"reads": 0, "mutated": True, "uncertain": False}
        second = self.run_leg(at=T0 + timedelta(seconds=301), budget=spent)
        self.assertEqual(second.observed.escalated, 1)
        self.assertEqual(self.emit.calls, [])
        self.assertEqual(len(self.store.unrecorded_pending(WS)), 1)

        free = {"reads": 0, "mutated": False, "uncertain": False}
        self.run_leg(at=T0 + timedelta(seconds=700), budget=free)
        self.assertEqual(len(self.emit.calls), 1)
        self.assertTrue(free["mutated"])

    def test_writing_the_journal_spends_the_budget(self) -> None:
        self.run_leg()
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        self.run_leg(at=T0 + timedelta(seconds=301), budget=budget)
        self.assertTrue(budget["mutated"])


class ReadbackFenceTest(LegBase):
    def test_a_journal_that_already_landed_is_bound_not_rewritten(self) -> None:
        # The crash-after-POST case: the durable record holds the firing, the local store
        # does not know it. A second write here would duplicate a coordinator-facing note.
        self.run_leg()
        self.run_leg(at=T0 + timedelta(seconds=301))
        (pending_key,) = [c["body"] for c in self.emit.calls]
        self.assertEqual(len(self.source.entries), 1)

        # Force the local store back to "unrecorded" and run the writer again.
        import sqlite3

        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute("UPDATE stall_escalation_pending SET journal_id='', woke_at=''")
            conn.commit()
        finally:
            conn.close()
        self.run_leg(at=T0 + timedelta(seconds=700))
        self.assertEqual(len(self.emit.calls), 1)  # no second POST
        self.assertEqual(len(self.source.entries), 1)
        (pending,) = self.store.unwoken_pending(WS) or self.store.open_pending(WS) or (None,)
        self.assertIsNone(pending)

    def test_a_posted_but_unverifiable_write_does_not_wake_anyone(self) -> None:
        # "The POST did not raise" is not "a journal exists".
        self.emit = _Emit(self.source, land=False)
        self.run_leg()
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        self.run_leg(at=T0 + timedelta(seconds=301), budget=budget)
        self.assertEqual(len(self.emit.calls), 1)
        self.assertEqual(self.wake.calls, [])
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.last_reason, "readback_unverified")
        # ...and the possibly-landed POST is charged to the shared pass budget, so no later
        # workspace performs a second external mutation behind it (j#110132 finding_1).
        self.assertTrue(budget["uncertain"])
        self.assertFalse(budget["mutated"])

    def test_a_transport_error_is_uncertain_not_a_deterministic_refusal(self) -> None:
        # The refusal allowlist is what separates "never reached Redmine" from "may have".
        # A transport error is outside it, so it must spend the budget as uncertain.
        self.emit = _Emit(self.source, recorded=False, reason="transport_error")
        self.run_leg()
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        self.run_leg(at=T0 + timedelta(seconds=301), budget=budget)
        self.assertTrue(budget["uncertain"])
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.last_reason, "transport_error")

    def test_an_unset_write_optin_is_a_deterministic_refusal(self) -> None:
        # The other side of the same allowlist: nothing was attempted, so the pass keeps
        # its remaining mutation slot.
        self.emit = _Emit(self.source, recorded=False, reason="write_optin_unset")
        self.run_leg()
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        self.run_leg(at=T0 + timedelta(seconds=301), budget=budget)
        self.assertFalse(budget["uncertain"])
        self.assertFalse(budget["mutated"])

    def test_a_refused_write_records_the_reason_and_wakes_nobody(self) -> None:
        self.emit = _Emit(self.source, recorded=False, reason="write_optin_unset")
        self.run_leg()
        self.run_leg(at=T0 + timedelta(seconds=301))
        self.assertEqual(self.wake.calls, [])
        (pending,) = self.store.unrecorded_pending(WS)
        self.assertEqual(pending.last_reason, "write_optin_unset")

    def test_journal_id_carrying_key_matches_only_the_exact_key(self) -> None:
        self.source.append("15855", "- idempotency_key: stallesc1_aaa\n", "1")
        self.source.append("15855", "- idempotency_key: stallesc1_bbb\n", "2")
        self.assertEqual(
            journal_id_carrying_key(self.source, "15855", "stallesc1_bbb"), "2"
        )
        self.assertEqual(journal_id_carrying_key(self.source, "15855", "nope"), "")

    def test_an_unreadable_source_reports_not_recorded(self) -> None:
        class _Broken:
            def read_entries(self, issue_id):
                raise RuntimeError("provider down")

        self.assertEqual(journal_id_carrying_key(_Broken(), "15855", "k"), "")


class NoiseTest(LegBase):
    def test_a_progressing_screen_never_escalates(self) -> None:
        screens = iter([f"line {i}\n" * (i + 1) for i in range(40)])

        def _read(target):
            return True, next(screens)

        for index in range(6):
            self.run_leg(at=T0 + timedelta(seconds=301 * index), read=_read)
        self.assertEqual(self.emit.calls, [])
        self.assertEqual(self.store.read_streaks(WS), {})

    def test_an_unreadable_screen_never_escalates(self) -> None:
        for index in range(8):
            self.run_leg(
                at=T0 + timedelta(seconds=301 * index),
                read=lambda target: (False, ""),
            )
        self.assertEqual(self.emit.calls, [])

    def test_a_lane_outside_the_declared_scope_is_never_read(self) -> None:
        policy = StallWatchPolicy.from_record({"lanes": ["some_other_lane"]})
        outcome = self.run_leg(policy=policy)
        self.assertEqual(outcome.reason, LEG_NOTHING_TO_WATCH)
        self.assertEqual(self.reads, [])



MARKER = "[mozyo:handoff:source=redmine:issue=15855:journal=110127:kind=review_request:to=claude]"
#: A body sitting UNSENT in the live composer: the provider banner, then the composer
#: prompt carrying the dispatched marker. `current_composer_retains_body` finds the last
#: rendered prompt and looks below it, so the marker must follow the prompt -- a marker
#: ABOVE it is transcript (already submitted) and correctly does not match.
COMPOSER_SCREEN = f"claude 2.1.220\n\n> {MARKER}\n"


class _LedgerRecord:
    def __init__(self, marker=MARKER, receiver="claude", target=LOCATOR) -> None:
        self.notification_marker = marker
        self.receiver = receiver
        self.target = target


class _Ledger:
    def __init__(self, records, *, raises=False) -> None:
        self._records = records
        self.raises = raises

    def records_for_issue(self, issue_id):
        if self.raises:
            raise RuntimeError("ledger unreadable")
        return list(self._records)


class BodyMarkerJoinTest(unittest.TestCase):
    """The join that makes ``unsent_composer`` reachable (review j#110132 finding_2)."""

    def test_the_most_recent_matching_dispatch_wins(self) -> None:
        older = "[mozyo:handoff:source=redmine:issue=15855:journal=1:kind=reply:to=claude]"
        ledger = _Ledger([_LedgerRecord(marker=older), _LedgerRecord()])
        self.assertEqual(
            resolve_body_marker(ledger, issue="15855", role="claude", locator=LOCATOR),
            MARKER,
        )

    def test_a_different_role_is_not_matched(self) -> None:
        ledger = _Ledger([_LedgerRecord(receiver="codex")])
        self.assertEqual(
            resolve_body_marker(ledger, issue="15855", role="claude", locator=LOCATOR), ""
        )

    def test_a_different_locator_is_not_matched(self) -> None:
        # The send went to a pane that is not the one being observed now.
        ledger = _Ledger([_LedgerRecord(target="w9Q:pZ")])
        self.assertEqual(
            resolve_body_marker(ledger, issue="15855", role="claude", locator=LOCATOR), ""
        )

    def test_non_handoff_text_is_never_matched(self) -> None:
        # Matching arbitrary recorded text would reintroduce the whole-screen substring
        # guess `ack-completion-receiver-state.md` forbids.
        ledger = _Ledger([_LedgerRecord(marker="some free-form note about the pane")])
        self.assertEqual(
            resolve_body_marker(ledger, issue="15855", role="claude", locator=LOCATOR), ""
        )

    def test_every_missing_input_yields_no_marker(self) -> None:
        ledger = _Ledger([_LedgerRecord()])
        for kwargs in (
            {"issue": "", "role": "claude", "locator": LOCATOR},
            {"issue": "15855", "role": "", "locator": LOCATOR},
            {"issue": "15855", "role": "claude", "locator": ""},
        ):
            with self.subTest(**kwargs):
                self.assertEqual(resolve_body_marker(ledger, **kwargs), "")
        self.assertEqual(
            resolve_body_marker(None, issue="15855", role="claude", locator=LOCATOR), ""
        )

    def test_an_unreadable_ledger_yields_no_marker(self) -> None:
        ledger = _Ledger([], raises=True)
        self.assertEqual(
            resolve_body_marker(ledger, issue="15855", role="claude", locator=LOCATOR), ""
        )


class UnsentComposerReachabilityTest(LegBase):
    """The whole point of finding_2: the class must be reachable in the PRODUCTION path."""

    def _run(self, *, with_marker, at=None):
        ledger = _Ledger([_LedgerRecord()]) if with_marker else _Ledger([])

        def _marker(issue, role, locator):
            return resolve_body_marker(
                ledger, issue=issue, role=role, locator=locator
            )

        return self.run_leg(
            read=lambda target: (True, COMPOSER_SCREEN),
            body_marker_for=_marker,
            at=at,
        )

    def _fire(self, *, with_marker):
        self._run(with_marker=with_marker)
        return self._run(with_marker=with_marker, at=T0 + timedelta(seconds=301))

    def test_a_retained_body_is_classified_unsent_composer(self) -> None:
        outcome = self._fire(with_marker=True)
        self.assertEqual(outcome.observed.escalated, 1)
        (call,) = self.emit.calls
        self.assertIn("- stall_class: unsent_composer", call["body"])
        # ADR-0002's bounded Enter-only budget, not patient waiting.
        self.assertIn("- prescription: enter_only_retry", call["body"])

    def test_without_the_join_the_same_screen_reports_the_wrong_class(self) -> None:
        # The pre-fix behaviour, kept as the discriminator: the IDENTICAL screen falls
        # through to the patient indeterminate class when no marker is supplied, so the
        # durable record would name the wrong remedy.
        outcome = self._fire(with_marker=False)
        self.assertEqual(outcome.observed.escalated, 1)
        (call,) = self.emit.calls
        self.assertIn("- stall_class: unresponsive_indeterminate", call["body"])
        self.assertIn("- prescription: patient_wait_then_retry", call["body"])

    def test_the_class_reaches_the_note_but_the_screen_text_does_not(self) -> None:
        self._fire(with_marker=True)
        (call,) = self.emit.calls
        self.assertIn("- stall_class: unsent_composer", call["body"])
        # The marker is evidence the classifier consumed; it is NOT pane content and it is
        # NOT reproduced in the durable record.
        self.assertNotIn(MARKER, call["body"])
        self.assertNotIn("claude 2.1.220", call["body"])


class StatusTest(LegBase):
    def test_status_reports_the_effective_policy_and_the_cadence(self) -> None:
        self.run_leg()
        payload = stall_watch_status(
            workspace_id=WS, store=self.store, policy=self.policy, now=T0
        )
        self.assertTrue(payload["policy"]["enabled"])
        self.assertEqual(payload["policy"]["lanes"], [LANE])
        self.assertEqual(payload["cadence"]["cadence_seconds"], 300)
        self.assertTrue(payload["cadence"]["last_pass_at"])

    def test_status_reports_the_pending_backlog_age(self) -> None:
        # The starvation-visibility requirement: a queue that is not moving must say so.
        self.run_leg()
        spent = {"reads": 0, "mutated": True, "uncertain": False}
        self.run_leg(at=T0 + timedelta(seconds=301), budget=spent)
        payload = stall_watch_status(
            workspace_id=WS,
            store=self.store,
            policy=self.policy,
            now=T0 + timedelta(seconds=900),
        )
        self.assertEqual(payload["pending"]["unrecorded"], 1)
        self.assertIsNotNone(payload["pending"]["oldest_unrecorded_age_seconds"])
        self.assertGreaterEqual(payload["pending"]["oldest_unrecorded_age_seconds"], 0)

    def test_status_says_why_an_unconfigured_watcher_is_off(self) -> None:
        payload = stall_watch_status(
            workspace_id=WS,
            store=self.store,
            policy=StallWatchPolicy.default(),
            now=T0,
        )
        self.assertFalse(payload["policy"]["enabled"])
        self.assertIn("observes nothing", payload["note"])

    def test_status_says_why_an_invalid_config_is_off(self) -> None:
        payload = stall_watch_status(
            workspace_id=WS,
            store=self.store,
            policy=StallWatchPolicy.resolve({"cadence_seconds": "soon"}),
            now=T0,
        )
        self.assertIn("cadence_seconds", payload["note"])


class _LaneRecord:
    def __init__(self, ws=WS, lane=LANE, issue="15855", generation="7",
                 disposition="active") -> None:
        self.repo_workspace_id = ws
        self.lane_id = lane
        self.issue_id = issue
        self.lane_generation = generation
        self.lane_disposition = disposition


class _LifecycleStore:
    def __init__(self, records, *, raises=False) -> None:
        self._records = records
        self.raises = raises
        self.reads = 0

    def records(self):
        self.reads += 1
        if self.raises:
            raise RuntimeError("lifecycle store unreadable")
        return self._records


class LaneSnapshotTest(unittest.TestCase):
    def test_only_this_workspace_s_active_lanes_contribute(self) -> None:
        store = _LifecycleStore(
            [
                _LaneRecord(lane="lane_a"),
                _LaneRecord(lane="lane_b", disposition="retired"),
                _LaneRecord(lane="lane_c", ws="wsB"),
            ]
        )
        self.assertEqual(
            lane_facts_snapshot(store, WS), {"lane_a": ("7", "15855")}
        )

    def test_a_row_missing_either_anchor_is_absent_from_the_map(self) -> None:
        # Absent from the map => the discovery join drops the unit: the fail-closed
        # direction, rather than a half-resolved target.
        store = _LifecycleStore(
            [
                _LaneRecord(lane="no_issue", issue=""),
                _LaneRecord(lane="no_generation", generation=""),
                _LaneRecord(lane="good"),
            ]
        )
        self.assertEqual(set(lane_facts_snapshot(store, WS)), {"good"})

    def test_both_anchors_come_from_one_snapshot(self) -> None:
        # Reading them separately would let a lane's generation and its issue anchor come
        # from different instants -- how a stall gets escalated onto a stale issue.
        store = _LifecycleStore([_LaneRecord()])
        lane_facts_snapshot(store, WS)
        self.assertEqual(store.reads, 1)

    def test_an_unreadable_lifecycle_store_watches_nothing(self) -> None:
        self.assertEqual(lane_facts_snapshot(_LifecycleStore([], raises=True), WS), {})


class PolicyResolutionTest(unittest.TestCase):
    def test_a_repo_with_no_config_watches_nothing(self) -> None:
        self.assertFalse(
            resolve_stall_watch_policy(tempfile.mkdtemp()).enabled
        )

    def _repo(self, body):
        root = Path(tempfile.mkdtemp())
        (root / ".mozyo-bridge").mkdir()
        (root / ".mozyo-bridge" / "config.yaml").write_text("version: 2\n" + body)
        return root

    def test_a_malformed_block_is_typed_invalid_not_absent(self) -> None:
        # j#110121-2 asks for a TYPED no-op. Collapsing malformed into `absent` told an
        # operator who mistyped a cadence that they had never configured anything
        # (review j#110132 finding_4).
        policy = resolve_stall_watch_policy(
            self._repo("stall_watch:\n  cadence_seconds: soon\n")
        )
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.reason, POLICY_INVALID)
        self.assertIn("cadence_seconds", policy.detail)
        self.assertEqual(policy.source, POLICY_INVALID)

    def test_the_detail_never_carries_the_raw_exception(self) -> None:
        # `detail` reaches --status --json. A YAML failure's message carries the absolute
        # config path AND a fragment of the file's own content, so truncating it is not
        # redaction (review j#110146 finding_2).
        root = self._repo("  : : bad yaml [\n")
        policy = resolve_stall_watch_policy(root)
        self.assertEqual(policy.reason, POLICY_CONFIG_UNREADABLE)
        self.assertNotIn(str(root), policy.detail)
        self.assertNotIn("bad yaml", policy.detail)
        self.assertNotIn("block end", policy.detail)
        self.assertIn(CONFIG_UNREADABLE_DETAIL, policy.detail)
        # ...and the same holds through the surface it actually reaches.
        self.assertNotIn(str(root), json.dumps(policy.telemetry()))

    def test_a_private_path_never_reaches_the_detail(self) -> None:
        root = self._repo("stall_watch:\n  lanes: [ok]\n  cadence_seconds: [1,2]\n")
        policy = resolve_stall_watch_policy(root)
        self.assertNotIn(str(root), policy.detail)
        self.assertNotIn("private", policy.detail)

    def test_a_sibling_key_is_not_misfiled_as_this_block_being_malformed(self) -> None:
        # `stall_watch_extra` CONTAINS the substring `stall_watch`; a substring test
        # reported it as this block being invalid and sent the operator to the wrong line.
        policy = resolve_stall_watch_policy(
            self._repo("stall_watch_extra:\n  x: 1\n")
        )
        self.assertEqual(policy.reason, POLICY_CONFIG_UNREADABLE)
        self.assertNotIn("stall_watch_extra", policy.detail)

    def test_classification_is_a_type_decision_not_a_string_decision(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_wiring import (  # noqa: E501
            own_validator_error,
        )
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_watch_policy import (  # noqa: E501
            StallWatchPolicyError,
        )

        own = StallWatchPolicyError("stall_watch.threshold must be at least 1")
        wrapped = RuntimeError("loader failed")
        wrapped.__cause__ = own
        self.assertIs(own_validator_error(wrapped), own)
        # A message that merely MENTIONS the block is not this block's error.
        self.assertIsNone(own_validator_error(RuntimeError("stall_watch is fine")))

    def test_the_cause_walk_is_bounded_against_a_cyclic_chain(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_wiring import (  # noqa: E501
            own_validator_error,
        )

        a, b = RuntimeError("a"), RuntimeError("b")
        a.__cause__, b.__cause__ = b, a
        self.assertIsNone(own_validator_error(a))

    def test_a_key_that_only_appears_as_a_SUBSTRING_is_not_named(self) -> None:
        # The discriminator between exact-token and substring matching. `lanes` is a
        # substring of the VALUE below but is not a token in it; a substring matcher would
        # sort `lanes` ahead of the real key and send the operator to the wrong setting.
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_wiring import (  # noqa: E501
            redacted_detail,
        )
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_watch_policy import (  # noqa: E501
            StallWatchPolicyError,
        )

        own = StallWatchPolicyError(
            "stall_watch.roles entries must be non-empty strings; got 'my_lanes_backup'"
        )
        self.assertEqual(
            redacted_detail(own, own=own), "StallWatchPolicyError: stall_watch.roles"
        )

    def test_the_detail_bound_holds_for_an_oversized_exception_type(self) -> None:
        # The closed vocabulary keeps the detail short in every case the parser produces,
        # so the bound is not observable through `from_record`. It is exercised directly
        # instead of deleted: it is what still holds if a future caller passes an exception
        # this module did not construct.
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_wiring import (  # noqa: E501
            POLICY_DETAIL_LIMIT,
            redacted_detail,
        )

        huge = type("E" * (POLICY_DETAIL_LIMIT * 3), (RuntimeError,), {})
        detail = redacted_detail(huge("boom"), own=None)
        self.assertEqual(len(detail), POLICY_DETAIL_LIMIT)

    def test_the_named_key_comes_from_the_declared_set(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_wiring import (  # noqa: E501
            UNIDENTIFIED_KEY,
            redacted_detail,
        )
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_watch_policy import (  # noqa: E501
            StallWatchPolicyError,
        )

        own = StallWatchPolicyError("stall_watch.threshold must be at least 1; got 0")
        self.assertEqual(
            redacted_detail(own, own=own), "StallWatchPolicyError: stall_watch.threshold"
        )
        vague = StallWatchPolicyError("something went wrong at /home/alice/secret")
        detail = redacted_detail(vague, own=vague)
        self.assertIn(UNIDENTIFIED_KEY, detail)
        self.assertNotIn("/home/alice", detail)

    def test_the_three_off_states_are_distinguishable(self) -> None:
        absent = resolve_stall_watch_policy(tempfile.mkdtemp())
        invalid = resolve_stall_watch_policy(
            self._repo("stall_watch:\n  threshold: 0\n")
        )
        unreadable = resolve_stall_watch_policy(self._repo("  : : bad yaml [\n"))
        reasons = {absent.reason, invalid.reason, unreadable.reason}
        self.assertEqual(
            reasons, {POLICY_ABSENT, POLICY_INVALID, POLICY_CONFIG_UNREADABLE}
        )
        for policy in (absent, invalid, unreadable):
            with self.subTest(reason=policy.reason):
                self.assertFalse(policy.enabled)

    def test_a_sibling_block_error_is_not_blamed_on_stall_watch(self) -> None:
        # The loader raises one error type for ANY invalid block; reporting a work_unit
        # mistake as "your stall_watch block is malformed" would send the operator to the
        # wrong line.
        policy = resolve_stall_watch_policy(
            self._repo("work_unit:\n  granularity: nonsense\n")
        )
        self.assertEqual(policy.reason, POLICY_CONFIG_UNREADABLE)

    def test_the_detail_is_bounded(self) -> None:
        policy = resolve_stall_watch_policy(
            self._repo("stall_watch:\n  lanes: [%s]\n" % ", ".join(["x"] * 400))
        )
        self.assertLessEqual(len(policy.detail), POLICY_DETAIL_LIMIT)

    def test_a_declared_block_is_read(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / ".mozyo-bridge").mkdir()
        (root / ".mozyo-bridge" / "config.yaml").write_text(
            "version: 2\nstall_watch:\n  lanes: [lane_a]\n  threshold: 5\n"
        )
        policy = resolve_stall_watch_policy(root)
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.threshold, 5)


class WiringTest(unittest.TestCase):
    class _WS:
        workspace_id = WS

        def __init__(self, path) -> None:
            self.canonical_path = path

    def _repo(self, body=""):
        root = Path(tempfile.mkdtemp())
        (root / ".mozyo-bridge").mkdir()
        (root / ".mozyo-bridge" / "config.yaml").write_text("version: 2\n" + body)
        return root

    def test_an_unconfigured_workspace_reads_nothing_at_all(self) -> None:
        # The property that makes it safe to wire the leg unconditionally: a host that
        # never configured it runs a sweep indistinguishable from the pre-#15855 one.
        lifecycle = _LifecycleStore([_LaneRecord()])
        inventory_calls = []
        reader_calls = []
        leg = build_stall_watch_leg_fn(
            home=Path(tempfile.mkdtemp()),
            lifecycle_store=lifecycle,
            inventory_rows=lambda: inventory_calls.append(1) or [],
            screen_reader=lambda: reader_calls.append(1) or None,
            note_transport=lambda: None,
        )
        outcome = leg(self._WS(self._repo()), pass_budget=None)
        self.assertEqual(outcome.reason, LEG_DISABLED)
        self.assertEqual(lifecycle.reads, 0)
        self.assertEqual(inventory_calls, [])
        self.assertEqual(reader_calls, [])

    def test_a_configured_workspace_runs_the_leg(self) -> None:
        lifecycle = _LifecycleStore([_LaneRecord()])
        leg = build_stall_watch_leg_fn(
            home=Path(tempfile.mkdtemp()),
            lifecycle_store=lifecycle,
            inventory_rows=lambda: [
                {"name": encode_assigned_name(WS, "claude", LANE), "pane_id": LOCATOR}
            ],
            screen_reader=lambda: (lambda target: (True, FROZEN_SCREEN)),
            note_transport=lambda: None,
            sleep=lambda _s: None,
            sample_interval_seconds=0.0,
        )
        outcome = leg(
            self._WS(self._repo("stall_watch:\n  lanes: [%s]\n" % LANE)),
            pass_budget={"reads": 0, "mutated": False, "uncertain": False},
        )
        self.assertEqual(outcome.reason, LEG_RAN)
        self.assertEqual(outcome.discovery.watched, 1)
        self.assertEqual(lifecycle.reads, 1)

    def test_an_unset_write_optin_keeps_the_firing_visible(self) -> None:
        # No transport => the canonical writer refuses with write_optin_unset, and the
        # firing stays in the local pending queue instead of being silently dropped.
        from mozyo_bridge.core.state.stall_escalation import StallEscalationStore

        home = Path(tempfile.mkdtemp())
        store = StallEscalationStore(home=home)
        repo = self._repo("stall_watch:\n  lanes: [%s]\n" % LANE)
        leg = build_stall_watch_leg_fn(
            home=home,
            lifecycle_store=_LifecycleStore([_LaneRecord()]),
            inventory_rows=lambda: [
                {"name": encode_assigned_name(WS, "claude", LANE), "pane_id": LOCATOR}
            ],
            screen_reader=lambda: (lambda target: (True, FROZEN_SCREEN)),
            note_transport=lambda: None,
            store=store,
            sleep=lambda _s: None,
            sample_interval_seconds=0.0,
        )
        ws = self._WS(repo)
        leg(ws, pass_budget=None)
        # Force the cadence open for the second pass rather than waiting five minutes.
        store.mark_pass(WS, now="2020-01-01T00:00:00+00:00")
        leg(ws, pass_budget=None)
        pending = store.unrecorded_pending(WS)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].last_reason, "write_optin_unset")




class DiscoveryCoverageTest(LegBase):
    """The leg must leave its coverage behind, so `--status` never has to read a pane."""

    def test_a_pass_records_its_coverage(self) -> None:
        self.run_leg()
        recorded = self.store.last_discovery(WS)
        self.assertEqual(recorded["watched"], 1)
        self.assertEqual(recorded["candidates"], 1)
        self.assertEqual(recorded["out_of_reach"], 0)
        self.assertTrue(recorded["observed_at"])

    def test_a_pass_that_watches_nothing_still_records_why(self) -> None:
        # The early return is exactly when an operator most wants the answer.
        outcome = self.run_leg(issue_for=lambda lane: "")
        self.assertEqual(outcome.reason, LEG_NOTHING_TO_WATCH)
        recorded = self.store.last_discovery(WS)
        self.assertEqual(recorded["watched"], 0)
        self.assertEqual(recorded["out_of_reach"], 1)
        self.assertEqual(recorded["dropped"], {"issue_anchor_unresolved": 1})

    def test_coverage_is_absent_before_any_pass(self) -> None:
        self.assertIsNone(self.store.last_discovery(WS))

    def test_the_recorded_coverage_carries_counts_only(self) -> None:
        # Identities would make the row unsafe to render; only counts and fixed reason
        # tokens are stored.
        self.run_leg(issue_for=lambda lane: "")
        blob = json.dumps(self.store.last_discovery(WS))
        self.assertNotIn(LOCATOR, blob)
        self.assertNotIn(LANE, blob)
        self.assertNotIn(FROZEN_SCREEN.strip(), blob)


class OperatorStatusSurfaceTest(unittest.TestCase):
    """`workflow supervisor --status` must answer "is the watcher running, and is it stuck".

    An earlier version implemented the projection but wired no production caller, so the
    last/next due (j#110121-2) and the pending age (j#110121-6) were readable only from
    tests — review j#110132 finding_3. These run the REAL CLI entrypoint against a temp
    home, so a future regression that unwires it fails here rather than passing silently.
    """

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / ".mozyo-bridge").mkdir(parents=True)

    def _register(self, config_body=""):
        from mozyo_bridge.core.state.workspace_registry import register_workspace

        (self.repo / ".mozyo-bridge" / "config.yaml").write_text(
            "version: 2\n" + config_body
        )
        return register_workspace(self.repo, home=self.home)

    def _status(self, as_json=False):
        from mozyo_bridge.application.cli import main

        argv = ["workflow", "supervisor", "--status", "--home", str(self.home)]
        if as_json:
            argv.append("--json")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = main(argv)
        self.assertEqual(rc, 0)
        return buffer.getvalue()

    def test_an_unconfigured_workspace_is_reported_as_off_with_its_reason(self) -> None:
        self._register()
        text = self._status()
        self.assertIn("stall_watch:", text)
        self.assertIn(f"off ({POLICY_ABSENT})", text)

    def test_a_malformed_block_is_reported_as_invalid_not_absent(self) -> None:
        self._register("stall_watch:\n  cadence_seconds: soon\n")
        self.assertIn(f"off ({POLICY_INVALID})", self._status())

    def test_a_configured_workspace_reports_cadence_and_due_instants(self) -> None:
        self._register("stall_watch:\n  lanes: [lane_a]\n  threshold: 3\n")
        text = self._status()
        self.assertIn("on (cadence=300s threshold=3)", text)
        self.assertIn("next_due>=", text)
        self.assertIn("pending:", text)

    def test_the_pending_backlog_age_is_visible(self) -> None:
        from mozyo_bridge.core.state.stall_escalation import (
            PendingEscalation,
            StallEscalationStore,
        )

        result = self._register("stall_watch:\n  lanes: [lane_a]\n")
        workspace_id = result.record.workspace_id
        StallEscalationStore(home=self.home).enqueue_pending(
            PendingEscalation(
                idempotency_key="k1",
                workspace_id=workspace_id,
                lane_id="lane_a",
                role="claude",
                stall_class="content_refusal",
                prescription="context_reset_reinjection",
                consecutive=2,
                first_observed_at="2026-08-22T09:00:00+00:00",
                escalated_at="2026-08-22T09:00:00+00:00",
                issue="15855",
            )
        )
        text = self._status()
        self.assertIn("unrecorded=1", text)
        self.assertIn("oldest_age=", text)
        self.assertNotIn("oldest_age=-", text)

    def test_coverage_is_absent_until_a_pass_has_run(self) -> None:
        # "never ran" and "ran and watched nothing" are different operator situations.
        self._register("stall_watch:\n  lanes: [lane_a]\n")
        self.assertIn("coverage: no pass recorded yet", self._status())
        payload = json.loads(self._status(as_json=True))
        self.assertIsNone(payload["stall_watch"][0]["discovery"])

    def test_out_of_reach_is_visible_in_text_and_json(self) -> None:
        # The whole of finding_1: an operator asking "is the watcher covering my cockpit"
        # must get an answer from --status at ANY instant, not only just after a sweep.
        from mozyo_bridge.core.state.stall_escalation import StallEscalationStore

        result = self._register("stall_watch:\n  lanes: [lane_a]\n")
        StallEscalationStore(home=self.home).record_discovery(
            result.record.workspace_id,
            candidates=5,
            watched=2,
            out_of_reach=2,
            dropped={"issue_anchor_unresolved": 2, "foreign_workspace": 1},
            now="2026-08-22T09:00:00+00:00",
        )
        text = self._status()
        self.assertIn("out_of_reach=2", text)
        self.assertIn("watched=2", text)
        self.assertIn("candidates=5", text)
        self.assertIn("issue_anchor_unresolved=2", text)

        discovery = json.loads(self._status(as_json=True))["stall_watch"][0]["discovery"]
        self.assertEqual(discovery["out_of_reach"], 2)
        self.assertEqual(discovery["watched"], 2)
        self.assertEqual(discovery["candidates"], 5)
        self.assertEqual(discovery["observed_at"], "2026-08-22T09:00:00+00:00")
        self.assertEqual(discovery["dropped"]["issue_anchor_unresolved"], 2)

    def test_a_corrupt_coverage_row_renders_a_token_not_its_contents(self) -> None:
        # The operator-surface half of j#110169: a stored row that fails the store's own
        # contract must reach `--status` as a TOKEN, never as the strings it contains.
        import sqlite3

        from mozyo_bridge.core.state.stall_escalation import (
            StallEscalationStore,
            stall_escalation_path,
        )

        result = self._register("stall_watch:\n  lanes: [lane_a]\n")
        StallEscalationStore(home=self.home).record_discovery(
            result.record.workspace_id,
            candidates=2, watched=1, out_of_reach=1,
            dropped={"issue_anchor_unresolved": 1}, now="2026-08-22T09:00:00+00:00",
        )
        conn = sqlite3.connect(stall_escalation_path(self.home))
        try:
            conn.execute(
                "UPDATE stall_watch_discovery SET dropped=?", ('{"/etc/shadow": 1}',)
            )
            conn.commit()
        finally:
            conn.close()

        text = self._status()
        self.assertIn("coverage: unreadable (off_vocabulary_reason)", text)
        self.assertNotIn("shadow", text)

        row = json.loads(self._status(as_json=True))["stall_watch"][0]["discovery"]
        self.assertEqual(row["unreadable"], "off_vocabulary_reason")
        self.assertNotIn("shadow", json.dumps(row))
        self.assertEqual(row["dropped"], {})

    def test_the_json_surface_carries_the_full_projection(self) -> None:
        self._register("stall_watch:\n  lanes: [lane_a]\n")
        payload = json.loads(self._status(as_json=True))
        (row,) = payload["stall_watch"]
        self.assertTrue(row["policy"]["enabled"])
        self.assertEqual(row["policy"]["lanes"], ["lane_a"])
        self.assertIn("next_due_at", row["cadence"])
        self.assertTrue(row["cadence"]["next_due_is_a_threshold_not_a_schedule"])
        for key in (
            "unrecorded",
            "anchorless",
            "recorded_but_unwoken",
            "oldest_unrecorded_age_seconds",
        ):
            with self.subTest(key=key):
                self.assertIn(key, row["pending"])

    def test_status_survives_a_workspace_whose_policy_cannot_be_read(self) -> None:
        self._register()
        (self.repo / ".mozyo-bridge" / "config.yaml").write_text("  : : bad yaml [\n")
        text = self._status()
        self.assertIn("stall_watch:", text)
        self.assertIn("off (", text)


class SupervisorReportTelemetryTest(unittest.TestCase):
    def test_a_raising_leg_is_reported_rather_than_swallowed(self) -> None:
        # "The watcher blew up" and "the watcher had nothing to do" must not read the same.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_stall_watch_capture import (  # noqa: E501
            STALL_WATCH_LEG_ERROR,
            capture_stall_watch,
        )

        class _WS:
            workspace_id = WS

        def _boom(ws, *, pass_budget=None):
            raise RuntimeError("watcher exploded")

        captured = capture_stall_watch(_boom, _WS(), pass_budget=None)
        self.assertEqual(captured["stall_watch_reason"], STALL_WATCH_LEG_ERROR)
        self.assertEqual(captured["error"], "RuntimeError")
        # The TYPE only -- a message could quote a path, a config value or a screen.
        self.assertNotIn("exploded", json.dumps(captured))

    def test_no_leg_wired_reports_nothing(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_stall_watch_capture import (  # noqa: E501
            capture_stall_watch,
        )

        self.assertIsNone(capture_stall_watch(None, object()))

    def test_the_report_payload_carries_the_leg_telemetry(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
            build_supervisor,
        )

        supervisor = build_supervisor(holder="probe", home=Path(tempfile.mkdtemp()))
        payload = supervisor.run_once().as_payload()
        self.assertIn("stall_watch", payload)
        self.assertEqual(payload["stall_watch"], [])


class SupervisorHookTest(unittest.TestCase):
    def test_the_supervisor_accepts_an_injected_stall_watch_leg(self) -> None:
        # The leg is injected, not imported, so the composition root keeps no dependency on
        # the observation feature.
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
            WorkspaceCallbackSupervisor,
        )

        params = inspect.signature(WorkspaceCallbackSupervisor.__init__).parameters
        self.assertIn("stall_watch_leg_fn", params)
        self.assertIsNone(params["stall_watch_leg_fn"].default)

    def test_the_production_build_actually_wires_it(self) -> None:
        # The whole point of #15855: without this, the sensor still has nobody running it.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
            build_supervisor,
        )

        supervisor = build_supervisor(holder="probe", home=Path(tempfile.mkdtemp()))
        self.assertIsNotNone(supervisor._stall_watch_leg_fn)

    def test_a_raising_leg_never_breaks_a_sweep(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
            build_supervisor,
        )

        supervisor = build_supervisor(holder="probe", home=Path(tempfile.mkdtemp()))

        def _boom(ws, *, pass_budget=None):
            raise RuntimeError("watcher exploded")

        supervisor._stall_watch_leg_fn = _boom
        report = supervisor.run_once()
        self.assertIsNotNone(report)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
