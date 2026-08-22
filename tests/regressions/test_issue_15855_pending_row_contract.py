"""A tampered pending row never actuates and never renders (Redmine #15855, j#110192 f1).

Review j#110192 finding_1 reproduced seven separate corruptions of one stored escalation
row. Six of them were rendered values; the seventh was not rendered at all — a direct-DB
rewrite of ``issue`` that redirected an EXTERNAL Redmine write to issue 99999.

The unit file (``tests/unit/core/state/test_stall_escalation.py``) pins the store's own
behaviour. This file pins the two properties that only hold end-to-end, and that are the
actual claims of the fix:

1. **Writer zero-call.** A row whose routing facts were altered is not merely reordered or
   flagged — the seam that reaches Redmine is never invoked for it. Asserted for both
   corruption routes: a direct call that builds the row, and a direct-DB rewrite after the
   row was legitimately stored.
2. **No sentinel on any surface.** The corrupted values do not appear in the rendered
   journal body, the ``--status`` text, or the ``--status --json`` payload.

Both routes are covered because they fail differently: the direct call is refused at the
write boundary, while the direct-DB rewrite gets past every write-side check by definition
and can only be caught on read.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.stall_escalation import (  # noqa: E402
    PendingEscalation,
    StallEscalationStore,
    escalation_idempotency_key,
)
from mozyo_bridge.core.state.stall_pending_contract import (  # noqa: E402
    EXTERNAL_REFERENCE_AUTHORITY,
    FIELD_CLASSES,
    FIELD_CLASS_EXTERNAL,
    FIELD_CLASS_IDENTITY,
    FIELD_CLASS_STATE,
    COUNT_MAX,
    IDENTITY_SEAL_FIELDS,
    NUMERIC_ID_MAX_LENGTH,
    PENDING_FIELD_CHECKERS,
    PENDING_FIELD_CLASSES,
    PENDING_OK,
    PENDING_ROUTING_MISMATCH,
    PENDING_STATE_MISMATCH,
    PENDING_UNRENDERABLE,
    PROJECTION_DERIVED_FIELDS,
    ROW_SEAL_FIELDS,
    pending_row_integrity,
    WAKE_ADMITTED,
    WAKE_JOURNAL_MISMATCH,
    WAKE_JOURNAL_NOT_CANONICAL,
    WAKE_JOURNAL_UNVERIFIED,
    WAKE_REFUSALS,
    WAKE_ROW_QUARANTINED,
    StallPendingContractError,
    admit_wake,
    canonical_journal_id,
    canonical_numeric_id_sql,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_escalation_pass import (  # noqa: E402,E501
    SETTLE_NOTHING_PENDING,
    WRITE_RECORDED,
    JournalWriteResult,
    settle_pending_escalations,
)
from mozyo_bridge.core.state.stall_pending_transition import (  # noqa: E402
    bumped_attempts,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_leg import (  # noqa: E402,E501
    build_journal_verifier,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_escalation_note import (  # noqa: E402,E501
    render_escalation_body,
)

WS = "wsA"
LANE = "lane_a"
ROLE = "claude"
REAL_ISSUE = "15855"
#: The issue the reproduction redirected the write to. Never a legitimate target here.
FOREIGN_ISSUE = "99999"
FIRST = "2026-08-22T09:01:00+00:00"


def _key(*, lane_id=LANE, issue=REAL_ISSUE, stall_class="content_refusal"):
    return escalation_idempotency_key(
        workspace_id=WS,
        lane_id=lane_id,
        role=ROLE,
        generation="g1",
        stall_class=stall_class,
        first_observed_at=FIRST,
        issue=issue,
    )


def _pending(**overrides):
    base = dict(
        idempotency_key=_key(),
        workspace_id=WS,
        lane_id=LANE,
        role=ROLE,
        generation="g1",
        target="w1V:pK",
        issue=REAL_ISSUE,
        stall_class="content_refusal",
        prescription="context_reset_reinjection",
        matched_id="m1",
        evidence_tier="rendered_confirmed",
        consecutive=2,
        first_observed_at=FIRST,
        escalated_at="2026-08-22T09:02:00+00:00",
    )
    base.update(overrides)
    return PendingEscalation(**base)


class _RecordingWriter:
    """Records every pending row it is asked to write, and which issue it was aimed at.

    Also remembers what it wrote WHERE, because the wake admission asks the external system
    the same question the writer's readback asked it. Keeping both faces on one fake is the
    point: a fake that let the writer report a journal the verifier could not find would be
    modelling an inconsistent Redmine, not a hostile one.
    """

    def __init__(self, journal_id: str = "110200") -> None:
        self.calls = []
        self.journal_id = journal_id
        #: ``(issue, idempotency_key) -> journal id``. The external system's own record.
        self.journals: dict = {}

    def __call__(self, pending):
        self.calls.append(pending)
        self.journals[(pending.issue, pending.idempotency_key)] = self.journal_id
        return JournalWriteResult(
            outcome=WRITE_RECORDED, journal_id=self.journal_id, reason="recorded"
        )

    def verify(self, pending) -> str:
        """The authority's answer: which journal carries this firing, if any."""
        return self.journals.get((pending.issue, pending.idempotency_key), "")

    @property
    def target_issues(self):
        return [p.issue for p in self.calls]


class _Wake:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, pending, journal_id):
        self.calls.append((pending, journal_id))
        return True


class RegressionBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = StallEscalationStore(path=self.dir / "stall-escalation.sqlite")
        self.writer = _RecordingWriter()
        self.wake = _Wake()

    def _corrupt(self, sql, *args):
        """Rewrite the stored row behind the store's back.

        This is the reproduction's actual route. Anything the WRITE boundary checks is by
        definition bypassed here, so whatever survives this is what the read boundary is
        worth.
        """
        conn = sqlite3.connect(self.store.path)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def _settle(self):
        return settle_pending_escalations(
            workspace_id=WS,
            store=self.store,
            now=lambda: "2026-08-22T09:05:00+00:00",
            budget={"reads": 0, "mutated": False, "uncertain": False},
            write_journal=self.writer,
            wake=self.wake,
            verify_journal=self.writer.verify,
        )


class WriterZeroCallTest(RegressionBase):
    """The seam that reaches Redmine is never invoked for a row that failed the contract."""

    def test_a_direct_db_issue_rewrite_produces_zero_writer_calls(self) -> None:
        self.store.enqueue_pending(_pending())
        self._corrupt("UPDATE stall_escalation_pending SET issue=?", FOREIGN_ISSUE)

        outcome = self._settle()

        self.assertEqual(self.writer.calls, [])
        self.assertNotIn(FOREIGN_ISSUE, self.writer.target_issues)
        self.assertEqual(self.wake.calls, [])
        # Reported as "nothing to write", and separately visible as a quarantine count --
        # the pass is not pretending the escalation never happened.
        self.assertEqual(outcome.reason, SETTLE_NOTHING_PENDING)
        self.assertEqual(len(self.store.quarantined_pending(WS)), 1)

    def test_an_untampered_row_still_reaches_the_writer_at_its_own_issue(self) -> None:
        # The control. A fix that stops every write would pass the test above trivially.
        self.store.enqueue_pending(_pending())

        self._settle()

        self.assertEqual(self.writer.target_issues, [REAL_ISSUE])
        self.assertEqual(len(self.wake.calls), 1)

    def test_a_direct_call_carrying_a_foreign_issue_is_refused_before_storage(self) -> None:
        # The other route: the row is built with a key that does not seal its own issue.
        with self.assertRaises(StallPendingContractError):
            self.store.enqueue_pending(
                _pending(issue=FOREIGN_ISSUE, idempotency_key="not-canonical")
            )
        self.assertEqual(self.store.open_pending(WS), ())

        self._settle()

        self.assertEqual(self.writer.calls, [])

    def test_a_row_whose_key_seals_a_different_issue_never_actuates(self) -> None:
        # Every field is individually well-formed; only the BINDING is wrong. This is the
        # case no per-field grammar can catch.
        self.store.enqueue_pending(
            _pending(issue=FOREIGN_ISSUE, idempotency_key=_key(issue=REAL_ISSUE))
        )

        self._settle()

        self.assertEqual(self.writer.calls, [])
        self.assertEqual(len(self.store.quarantined_pending(WS)), 1)


class NoSentinelOnAnySurfaceTest(RegressionBase):
    """The corrupted values do not reach a journal body, the status text, or the JSON."""

    #: The reproduction's values, one per rendered field.
    SENTINELS = (
        ("lane_id", "lane\n- injected: line", "injected"),
        ("stall_class", "not_a_class", "not_a_class"),
        ("prescription", "rm -rf /", "rm -rf"),
        ("last_reason", "/private/example/operator-unsafe-reason", "operator-unsafe"),
    )

    def test_none_of_the_reproduction_values_can_be_stored(self) -> None:
        for field, value, _ in self.SENTINELS:
            with self.subTest(field=field):
                with self.assertRaises(StallPendingContractError):
                    self.store.enqueue_pending(_pending(**{field: value}))
        self.assertEqual(self.store.open_pending(WS), ())

    def test_a_direct_db_sentinel_never_reaches_the_rendered_journal_body(self) -> None:
        for field, value, needle in self.SENTINELS:
            with self.subTest(field=field):
                self.store = StallEscalationStore(
                    path=self.dir / f"body-{field}.sqlite"
                )
                self.store.enqueue_pending(_pending())
                self._corrupt(
                    f"UPDATE stall_escalation_pending SET {field}=?", value  # noqa: S608
                )
                rows = self.store.open_pending(WS)
                self.assertEqual(len(rows), 1)
                # The row survives -- it is evidence a stall fired -- but it is withheld
                # from the writer, so no body is ever rendered from it in production.
                self.assertFalse(rows[0].externally_writable)
                self.assertEqual(self.store.unrecorded_pending(WS), ())
                # And even if something did render it, the value is not in the payload the
                # renderer would be handed.
                projected = json.dumps(rows[0].telemetry())
                self.assertNotIn(needle, projected)
                self.assertIn(PENDING_UNRENDERABLE, projected)

    def test_the_body_renderer_is_never_handed_a_quarantined_row(self) -> None:
        # The end-to-end shape of the injection: `lane_id` reaches a journal BODY through
        # `slot_label`, where a newline fabricates a line in a durable record.
        self.store.enqueue_pending(_pending())
        self._corrupt(
            "UPDATE stall_escalation_pending SET lane_id=?", "lane\n- injected: line"
        )
        # Nothing to render from: the supply side of the writer is empty.
        self.assertEqual(self.store.unrecorded_pending(WS), ())

        # And the row that IS still readable renders a slot the body renderer can hold: the
        # corrupted component came out as the unrenderable token, not as a new line.
        (row,) = self.store.open_pending(WS)
        body = render_escalation_body(
            issue=REAL_ISSUE,
            slot_label=str(row.telemetry()["slot"]),
            generation=row.generation,
            target=row.target,
            provider_id="claude",
            stall_class=row.stall_class,
            prescription=row.prescription,
            consecutive=row.consecutive,
            first_observed_at=row.first_observed_at,
            last_observed_at=row.escalated_at,
            policy_id="stall-watch/v1",
            idempotency_key=row.idempotency_key,
            matched_id=row.matched_id,
            evidence_tier=row.evidence_tier,
        )
        self.assertNotIn("- injected: line", body)
        self.assertIn(PENDING_UNRENDERABLE, body)
        self.assertIn("## Gate: blocked", body)


class PersistenceStateColumnTest(RegressionBase):
    """The state columns end-to-end (review j#110218 finding_pendingcontract).

    Round five closed identity / routing / stall and called the row closed; the five
    persistence-state columns were still open. These are the two properties that only hold
    end-to-end: a fake ``journal_id`` never settles a firing, and no read surface raises.
    """

    def test_a_fake_journal_id_does_not_settle_the_firing(self) -> None:
        self.store.enqueue_pending(_pending())
        self._corrupt(
            "UPDATE stall_escalation_pending SET journal_id=?, written_at=?",
            "not-a-journal",
            "whenever",
        )

        outcome = self._settle()

        # Not written, not woken, and still open: the escalation is preserved as unfinished
        # rather than closed out against a journal nobody read back.
        self.assertEqual(self.wake.calls, [])
        self.assertEqual(outcome.reason, SETTLE_NOTHING_PENDING)
        (row,) = self.store.open_pending(WS)
        self.assertFalse(row.recorded)
        self.assertFalse(row.settled)
        self.assertEqual(len(self.store.quarantined_pending(WS)), 1)

    def test_a_real_journal_id_still_settles(self) -> None:
        # The control. A fence that refuses every wake passes the test above for free.
        self.store.enqueue_pending(_pending())

        self._settle()

        self.assertEqual(len(self.wake.calls), 1)
        self.assertEqual(self.store.open_pending(WS), ())

    def test_no_read_surface_raises_on_a_non_numeric_count(self) -> None:
        """The visibility surface must be the LAST thing to break, not the first.

        `--status` swallows exceptions so a status command cannot crash. When a corrupted
        count raised out of `quarantined_pending`, that swallowing turned a tampered store
        into a QUIETER display than a healthy one.
        """
        for column in ("consecutive", "attempts"):
            with self.subTest(column=column):
                store = StallEscalationStore(path=self.dir / f"cnt-{column}.sqlite")
                store.enqueue_pending(_pending())
                conn = sqlite3.connect(store.path)
                try:
                    conn.execute(
                        f"UPDATE stall_escalation_pending SET {column}='not-an-int'"  # noqa: S608
                    )
                    conn.commit()
                finally:
                    conn.close()
                for surface in ("open_pending", "unrecorded_pending", "unwoken_pending",
                                "quarantined_pending"):
                    with self.subTest(surface=surface):
                        getattr(store, surface)(WS)  # must not raise
                self.assertEqual(len(store.quarantined_pending(WS)), 1)
                self.assertEqual(store.unrecorded_pending(WS), ())

    def test_no_state_column_value_reaches_the_projection(self) -> None:
        self.store.enqueue_pending(_pending())
        self._corrupt(
            "UPDATE stall_escalation_pending SET last_attempt_at=?, woke_at=?, attempts=-5",
            "/private/example/operator-unsafe-reason",
            "/etc/shadow",
        )
        (row,) = self.store.open_pending(WS)
        projected = json.dumps(row.telemetry())
        self.assertNotIn("operator-unsafe", projected)
        self.assertNotIn("shadow", projected)
        self.assertIn(PENDING_UNRENDERABLE, projected)
        self.assertEqual(self.store.unrecorded_pending(WS), ())


# ======================================================================================
# Review j#110254: grammar is not existence, and one table is not one rule
# ======================================================================================
#
# Two findings, one shape. `journal_id` carried a grammar that looked like rigour: a
# canonical-shaped `999999` settled a firing with Redmine never asked (finding_
# stateauthority), and the "single checker table" was re-implemented by hand on three more
# faces that each answered differently (finding_checkerdrift).
#
# The tests below are deliberately NOT "the table has the right keys". That test existed,
# passed, and proved nothing: a name in a table says nothing about whether any face reads
# it. What is pinned here is AGREEMENT between faces on the same input, and the mechanism
# each column's declared class promises.

#: Values chosen to separate the faces that drifted. The 13-digit id is the one the
#: 12-character bound refuses while a hand-written `isdigit()` accepts; the Arabic-Indic
#: digits are the ones `str.isdigit()` accepts and `[0-9]` does not; the trailing newline is
#: the one Python's `$` accepts and `fullmatch` does not.
JOURNAL_ID_CORPUS = (
    "", "0", "9", "110264", "000000000001", "123456789012", "1234567890123",
    "99999999999999999999", "not-a-journal", "110200x", "x110200", "11 0264",
    " 110264", "110264 ", "110264\n", "\n110264", "11\u20280264", "\u0661\u0662\u0663",
    "\u00b2", "1.0", "-1", "+1", "1e3", "0x10", "11\u0663", "\U0001d7cf",
)


class JournalIdFaceEquivalenceTest(unittest.TestCase):
    """Every face that decides "is this a journal id" gives the SAME answer.

    The faces are the ones review j#110254 found disagreeing, plus the write-result seam
    that was checking only for non-emptiness. They are enumerated here rather than argued
    about, and the assertion is agreement — not agreement with an oracle written in this
    file, which would just be a fourth implementation of the rule.
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def _sql_face(self, value: str) -> bool:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE t (journal_id TEXT NOT NULL)")
            conn.execute("INSERT INTO t (journal_id) VALUES (?)", (value,))
            (count,) = conn.execute(
                f"SELECT COUNT(*) FROM t WHERE {canonical_numeric_id_sql('journal_id')}"  # noqa: S608
            ).fetchone()
        finally:
            conn.close()
        return bool(count)

    def _table_face(self, value: str) -> bool:
        try:
            PENDING_FIELD_CHECKERS["journal_id"](value)
        except StallPendingContractError:
            return False
        return bool(value)

    def _store_face(self, value: str, index: int) -> bool:
        store = StallEscalationStore(path=self.dir / f"face-{index}.sqlite")
        store.enqueue_pending(_pending())
        return bool(store.mark_recorded(_key(), value))

    def _write_result_face(self, value: str) -> bool:
        try:
            JournalWriteResult(outcome=WRITE_RECORDED, journal_id=value)
        except ValueError:
            return False
        return True

    def _projection_face(self, value: str) -> bool:
        row = _pending(journal_id=value)
        rendered = row.telemetry().get("journal_id", "")
        return bool(rendered) and rendered != PENDING_UNRENDERABLE

    def test_every_face_agrees_on_every_value(self) -> None:
        for index, value in enumerate(JOURNAL_ID_CORPUS):
            with self.subTest(value=value):
                faces = {
                    "checker_table": self._table_face(value),
                    "canonical_journal_id": canonical_journal_id(value),
                    "recorded_property": _pending(journal_id=value).recorded,
                    "mark_recorded": self._store_face(value, index),
                    "mark_woken_sql": self._sql_face(value),
                    "journal_write_result": self._write_result_face(value),
                    "projection": self._projection_face(value),
                }
                self.assertEqual(
                    len(set(faces.values())), 1,
                    f"faces disagree on {value!r}: {faces}",
                )

    def test_the_corpus_separates_accept_from_refuse(self) -> None:
        # Without this, a rule that refused everything would satisfy the agreement test.
        self.assertTrue(canonical_journal_id("110264"))
        self.assertTrue(canonical_journal_id("123456789012"))  # exactly at the bound
        self.assertFalse(canonical_journal_id("1234567890123"))  # one past it
        self.assertFalse(canonical_journal_id("\u0661\u0662\u0663"))  # digits, not [0-9]

    def test_the_sql_predicate_is_generated_not_transcribed(self) -> None:
        # The bound in the SQL comes from the same constant the checker uses. Pinning the
        # linkage rather than the literal is what makes a future bound change safe.
        self.assertIn(str(NUMERIC_ID_MAX_LENGTH), canonical_numeric_id_sql("journal_id"))
        self.assertIn("journal_id", canonical_numeric_id_sql("journal_id"))


class WakeAuthorityTest(RegressionBase):
    """A coordinator is woken only for a journal the EXTERNAL system confirms."""

    def _forge(self, journal_id: str = "999999") -> None:
        """The reproduction: rewrite ONE row's persistence state. No key recomputation."""
        self._corrupt(
            "UPDATE stall_escalation_pending SET journal_id=?, written_at=?",
            journal_id, "2026-08-22T09:03:00+00:00",
        )

    def test_a_canonical_but_fabricated_journal_id_wakes_nobody(self) -> None:
        self.store.enqueue_pending(_pending())
        self._forge()

        self._settle()

        self.assertEqual(self.wake.calls, [])
        self.assertEqual(self.writer.calls, [])

    def test_the_fabricated_row_is_visible_rather_than_settled(self) -> None:
        # The half of the finding that mattered most: before the seal, a forged row was
        # `settled=True, integrity=ok` and appeared in NEITHER the open inventory nor the
        # quarantine surface. A stall report that vanishes is worse than one that fails.
        self.store.enqueue_pending(_pending())
        self._forge()

        self.assertEqual(len(self.store.quarantined_pending(WS)), 1)
        (row,) = self.store.quarantined_pending(WS)
        self.assertFalse(row.settled)
        self.assertFalse(row.externally_writable)

    def test_a_fully_forged_settled_row_is_still_visible(self) -> None:
        # Rewriting `woke_at` too satisfies every lifecycle predicate, so scoping the scan
        # to open rows would let the most complete forgery be the one nobody can see.
        self.store.enqueue_pending(_pending())
        self._corrupt(
            "UPDATE stall_escalation_pending SET journal_id=?, written_at=?, woke_at=?",
            "999999", "2026-08-22T09:03:00+00:00", "2026-08-22T09:04:00+00:00",
        )

        self.assertEqual(self.store.open_pending(WS), ())
        self.assertEqual(len(self.store.quarantined_pending(WS)), 1)

    def test_a_journal_the_authority_does_not_know_is_refused_and_counted(self) -> None:
        # A row the store itself wrote, naming a journal Redmine has never heard of. The
        # seal is intact, so only the authority can catch this one.
        self.store.enqueue_pending(_pending())
        self.assertTrue(self.store.mark_recorded(_key(), "110999"))

        outcome = self._settle()

        self.assertEqual(self.wake.calls, [])
        self.assertEqual(
            dict(outcome.wake_refusals), {WAKE_JOURNAL_UNVERIFIED: 1}
        )

    def test_a_journal_the_authority_names_differently_is_refused(self) -> None:
        self.store.enqueue_pending(_pending())
        self.assertTrue(self.store.mark_recorded(_key(), "110998"))
        self.writer.journals[(REAL_ISSUE, _key())] = "110264"  # Redmine says otherwise

        outcome = self._settle()

        self.assertEqual(self.wake.calls, [])
        self.assertEqual(dict(outcome.wake_refusals), {WAKE_JOURNAL_MISMATCH: 1})

    def test_no_verifier_wakes_nobody(self) -> None:
        # Fail-closed: an unverifiable claim is not a weaker reason to wake a coordinator,
        # it is no reason. A host with no readable Redmine source waits.
        self.store.enqueue_pending(_pending())
        self.assertTrue(self.store.mark_recorded(_key(), "110264"))

        outcome = settle_pending_escalations(
            workspace_id=WS,
            store=self.store,
            now=lambda: "2026-08-22T09:05:00+00:00",
            budget={"reads": 0, "mutated": False, "uncertain": False},
            write_journal=self.writer,
            wake=self.wake,
        )

        self.assertEqual(self.wake.calls, [])
        self.assertEqual(dict(outcome.wake_refusals), {WAKE_JOURNAL_UNVERIFIED: 1})

    def test_a_confirmed_journal_still_wakes(self) -> None:
        # The control. A fence that refuses every wake passes all of the above for free.
        self.store.enqueue_pending(_pending())
        self.assertTrue(self.store.mark_recorded(_key(), "110264"))
        self.writer.journals[(REAL_ISSUE, _key())] = "110264"

        outcome = self._settle()

        self.assertEqual(self.wake.calls, [(WS, REAL_ISSUE)])
        self.assertEqual(outcome.wake_refusals, ())
        self.assertEqual(self.store.open_pending(WS), ())

    def test_the_freshly_written_firing_is_verified_like_any_other(self) -> None:
        # The row recorded moments ago goes back through the same supply surface and the
        # same admission. Two code paths for one rule is the defect this round is about.
        self.store.enqueue_pending(_pending())

        outcome = self._settle()

        self.assertEqual(self.writer.target_issues, [REAL_ISSUE])
        self.assertEqual(self.wake.calls, [(WS, REAL_ISSUE)])
        self.assertEqual(outcome.wake_refusals, ())


class NoLaunderingTest(RegressionBase):
    """A tampered row is never re-sealed by an ordinary later transition."""

    def test_a_later_transition_does_not_relegitimise_a_forged_row(self) -> None:
        # Without this fence the store becomes the forger's accomplice: the next pass
        # recomputes a seal over the tampered values and the row reads `ok` again.
        self.store.enqueue_pending(_pending())
        self._corrupt("UPDATE stall_escalation_pending SET attempts=41")

        self.assertFalse(self.store.record_attempt(_key(), "transport_error"))
        self.assertFalse(self.store.mark_recorded(_key(), "110264"))
        self.assertFalse(self.store.mark_woken(_key()))

        (row,) = self.store.quarantined_pending(WS)
        self.assertEqual(row.integrity, PENDING_STATE_MISMATCH)
        self.assertEqual(row.attempts, 41)

    def test_an_untampered_row_still_transitions(self) -> None:
        self.store.enqueue_pending(_pending())
        self.assertTrue(self.store.record_attempt(_key(), "transport_error"))
        self.assertTrue(self.store.mark_recorded(_key(), "110264"))
        self.assertTrue(self.store.mark_woken(_key()))
        self.assertEqual(self.store.quarantined_pending(WS), ())


class FieldClassEnumerationTest(RegressionBase):
    """Every stored column declares a class, and the class's mechanism actually fires.

    This is the class generalisation the point fixes do not provide. `issue` lost a round,
    then `journal_id` lost a round for the same reason one field later; enumerating the
    columns and proving each declared mechanism on each column is what stops the third.
    """

    #: A DIFFERENT but perfectly legal value for each column. Legal matters: the whole
    #: finding is that a legal-looking value is what no grammar can catch.
    LEGAL_ALTERNATIVES = {
        "idempotency_key": _key(lane_id="lane_b"),
        "workspace_id": "wsB",
        "lane_id": "lane_b",
        "role": "codex",
        "generation": "g2",
        "stall_class": "unsent_composer",
        "first_observed_at": "2026-08-22T08:00:00+00:00",
        "issue": FOREIGN_ISSUE,
        "journal_id": "110264",
        "written_at": "2026-08-22T09:09:00+00:00",
        "woke_at": "2026-08-22T09:09:00+00:00",
        "attempts": 7,
        "last_attempt_at": "2026-08-22T09:09:00+00:00",
        "last_reason": "transport_error",
        "row_seal": "stallst1_" + "0" * 32,
        "target": "w1V:pZ",
        "prescription": "owner_escalation",
        "matched_id": "m2",
        "evidence_tier": "binary_read_unrendered",
        "consecutive": 9,
        "escalated_at": "2026-08-22T09:09:00+00:00",
    }

    def test_the_class_table_covers_exactly_the_stored_columns(self) -> None:
        stored = {f.name for f in fields(PendingEscalation)} - {"integrity"}
        self.assertEqual(set(PENDING_FIELD_CLASSES), stored)
        self.assertEqual(set(PENDING_FIELD_CHECKERS), stored)
        self.assertEqual(set(self.LEGAL_ALTERNATIVES), stored)
        self.assertLessEqual(set(PENDING_FIELD_CLASSES.values()), FIELD_CLASSES)

    def test_the_two_seals_PARTITION_the_stored_columns(self) -> None:
        # The statement rounds six, seven and eight each failed to make: not "the columns we
        # thought about are sealed" but "there is no third category". Every stored column is
        # in the identity key, in the row seal, or IS one of the two derived columns.
        stored = {f.name for f in fields(PendingEscalation)} - {"integrity"}
        identity, row = set(IDENTITY_SEAL_FIELDS), set(ROW_SEAL_FIELDS)
        self.assertEqual(identity & row, set())
        self.assertEqual(identity | row | {"idempotency_key", "row_seal"}, stored)

    def test_every_stored_column_detects_a_legal_rewrite(self) -> None:
        # Per column, against a value that passes every grammar. "Legal" is the whole point:
        # `999999` is a valid journal id, `99999` a valid issue id, and `owner_escalation` a
        # valid prescription — a grammar cannot tell any of them from the right value.
        for name in sorted(self.LEGAL_ALTERNATIVES):
            expected = (
                PENDING_ROUTING_MISMATCH
                if name in set(IDENTITY_SEAL_FIELDS) | {"idempotency_key"}
                else PENDING_STATE_MISMATCH
            )
            with self.subTest(column=name, expected=expected):
                store = StallEscalationStore(path=self.dir / f"cls-{name}.sqlite")
                store.enqueue_pending(_pending())
                conn = sqlite3.connect(store.path)
                try:
                    conn.execute(
                        f"UPDATE stall_escalation_pending SET {name}=?",  # noqa: S608
                        (self.LEGAL_ALTERNATIVES[name],),
                    )
                    conn.commit()
                finally:
                    conn.close()
                # Unscoped on purpose: rewriting `workspace_id` also RELOCATES the row, so a
                # workspace-scoped query would report "clean" for the one corruption that
                # moves the evidence. The whole-store inventory is where it cannot hide.
                (row,) = store.quarantined_pending()
                self.assertEqual(row.integrity, expected)
                self.assertFalse(row.externally_writable)

    def test_every_external_reference_declares_where_its_authority_is_asked(self) -> None:
        external = {
            name for name, klass in PENDING_FIELD_CLASSES.items()
            if klass == FIELD_CLASS_EXTERNAL
        }
        self.assertEqual(external, set(EXTERNAL_REFERENCE_AUTHORITY))
        self.assertTrue(external, "the class must not be empty or this test proves nothing")

    def test_the_issue_authority_site_refuses_a_rewritten_target(self) -> None:
        # `issue`'s declared site is the write admission: the writer is never reached.
        self.assertEqual(EXTERNAL_REFERENCE_AUTHORITY["issue"], "write_admission")
        self.store.enqueue_pending(_pending())
        self._corrupt("UPDATE stall_escalation_pending SET issue=?", FOREIGN_ISSUE)

        self._settle()

        self.assertEqual(self.writer.calls, [])

    def test_the_journal_authority_site_refuses_an_unconfirmed_id(self) -> None:
        # `journal_id`'s declared site is the wake admission, and it consults the authority
        # rather than the row's own claim.
        self.assertEqual(EXTERNAL_REFERENCE_AUTHORITY["journal_id"], "wake_admission")
        row = _pending(journal_id="999999")
        self.assertEqual(admit_wake(row, ""), WAKE_JOURNAL_UNVERIFIED)
        self.assertEqual(admit_wake(row, "110264"), WAKE_JOURNAL_MISMATCH)
        self.assertEqual(admit_wake(row, "999999"), WAKE_ADMITTED)

    def test_every_column_renders_through_the_table_or_is_declared_derived(self) -> None:
        # Face (c) of finding_checkerdrift: two instants were assigned straight from the
        # row in the same commit that introduced the table. This walks every column.
        unsafe = "/private/example/unsafe-value"
        for name in sorted(PENDING_FIELD_CLASSES):
            if name in PROJECTION_DERIVED_FIELDS:
                continue
            with self.subTest(column=name):
                row = _pending(**{name: unsafe})
                projected = json.dumps(row.telemetry())
                self.assertNotIn("unsafe-value", projected)
                self.assertIn(PENDING_UNRENDERABLE, projected)



class SealBindingTest(RegressionBase):
    """The state seal is bound to the row it seals, and to the values it covers."""

    def test_a_settled_row_s_state_cannot_be_lifted_onto_another_firing(self) -> None:
        # Without the key in the seal, the settled state of a firing that really did reach a
        # coordinator is copyable onto one that never did — and the copy reads as `ok`.
        settled_key = _key()
        other_key = _key(lane_id="lane_b")
        self.store.enqueue_pending(_pending())
        self.store.enqueue_pending(_pending(idempotency_key=other_key, lane_id="lane_b"))
        self.assertTrue(self.store.mark_recorded(settled_key, "110264"))
        # EVERY sealed column, not a subset. Copying only some of them would be caught by
        # the seal for a reason that has nothing to do with the key binding, and the test
        # would pass while proving nothing about it.
        columns = (*ROW_SEAL_FIELDS, "row_seal")
        assignments = ", ".join(
            f"{name}=(SELECT {name} FROM stall_escalation_pending WHERE idempotency_key=?)"
            for name in columns
        )
        self._corrupt(
            f"UPDATE stall_escalation_pending SET {assignments} WHERE idempotency_key=?",  # noqa: S608
            *([settled_key] * len(columns)), other_key,
        )

        quarantined = {row.idempotency_key for row in self.store.quarantined_pending()}
        self.assertEqual(quarantined, {other_key})

    def test_a_row_that_needed_normalising_is_not_born_tampered(self) -> None:
        # The seal must cover what is STORED, not what was passed in. `checked_timestamp`
        # folds instants to UTC, so sealing the caller's values and storing the normalized
        # ones would quarantine every row that arrived on a non-UTC offset — at the moment
        # it was written, by the store itself.
        self.store.enqueue_pending(
            _pending(
                written_at="2026-08-22T18:02:00+09:00",
                last_attempt_at="2026-08-22T18:02:00+09:00",
                attempts=1,
            )
        )

        self.assertEqual(self.store.quarantined_pending(), ())
        (row,) = self.store.open_pending(WS)
        self.assertEqual(row.integrity, PENDING_OK)
        self.assertEqual(row.written_at, "2026-08-22T09:02:00+00:00")

    def test_a_caller_supplied_seal_is_never_trusted(self) -> None:
        # `enqueue_pending` DERIVES the seal from the row. A seal a caller could supply
        # would seal nothing at all.
        forged = "stallst1_" + "0" * 32
        self.store.enqueue_pending(_pending(row_seal=forged))
        self.assertEqual(self.store.quarantined_pending(), ())
        (row,) = self.store.open_pending(WS)
        self.assertNotEqual(row.row_seal, forged)


class WakeAdmissionUnitTest(unittest.TestCase):
    """:func:`admit_wake` alone, including layers an upstream filter usually hides.

    ``unwoken_pending`` already excludes quarantined rows, so two of these branches are not
    reachable through the settle path today. That makes them provably equivalent under the
    upstream guard, NOT dead: the admission is a public rule and a caller arriving by
    another route must get the same answer. So the layer is tested where it CAN be
    measured — directly (the disposition #15844 recorded for exactly this shape).
    """

    def test_a_quarantined_row_is_refused_whatever_the_authority_says(self) -> None:
        # Stamped the way a stored row is stamped, by the same classifier: the verdict is
        # not a property of the value object, it is what the read boundary computed.
        raw = _pending(journal_id="110264", consecutive=-3)  # a grammar violation
        row = replace(raw, integrity=pending_row_integrity(raw))
        self.assertFalse(row.externally_writable)
        self.assertEqual(admit_wake(row, "110264"), WAKE_ROW_QUARANTINED)

    def test_a_non_canonical_stored_id_is_refused_before_the_authority(self) -> None:
        row = _pending(journal_id="not-a-journal")
        self.assertEqual(admit_wake(row, "not-a-journal"), WAKE_JOURNAL_NOT_CANONICAL)

    def test_every_verdict_is_a_declared_token(self) -> None:
        # The verdicts reach a status surface, so they are held to the same closed-vocabulary
        # rule as every other rendered token in this rail.
        row = _pending(journal_id="999999")
        for observed in ("", "110264", "999999"):
            with self.subTest(observed=observed):
                verdict = admit_wake(row, observed)
                self.assertTrue(verdict == WAKE_ADMITTED or verdict in WAKE_REFUSALS)


class TransitionInputTest(RegressionBase):
    """Values a CALLER supplies to a transition go through the table like any other."""

    def test_an_unusable_timestamp_refuses_the_write_rather_than_storing_it(self) -> None:
        # `now` is caller-supplied on every transition, so it is the one value in a plan
        # that does not arrive from an already-validated row.
        self.store.enqueue_pending(_pending())
        for method, args in (
            ("record_attempt", (_key(), "transport_error")),
            ("mark_recorded", (_key(), "110264")),
        ):
            with self.subTest(method=method):
                self.assertFalse(
                    getattr(self.store, method)(*args, now="/private/example/not-an-instant")
                )
        (row,) = self.store.open_pending(WS)
        self.assertEqual(row.attempts, 0)
        self.assertEqual(row.journal_id, "")
        self.assertEqual(row.integrity, PENDING_OK)

    def test_recording_against_no_journal_at_all_is_not_a_transition(self) -> None:
        # `""` is a legal stored `journal_id` (an unrecorded row has one), so the column
        # grammar admits it and only the transition can refuse it.
        self.store.enqueue_pending(_pending())
        self.assertFalse(self.store.mark_recorded(_key(), ""))
        (row,) = self.store.open_pending(WS)
        self.assertEqual(row.attempts, 0)
        self.assertEqual(row.written_at, "")

    def test_the_attempt_count_saturates_inside_its_own_grammar(self) -> None:
        # An unbounded increment eventually leaves the count grammar and would quarantine a
        # row for the crime of having been refused too many times.
        self.assertEqual(bumped_attempts(0), 1)
        self.assertEqual(bumped_attempts(COUNT_MAX - 1), COUNT_MAX)
        self.assertEqual(bumped_attempts(COUNT_MAX), COUNT_MAX)
        PENDING_FIELD_CHECKERS["attempts"](bumped_attempts(COUNT_MAX))  # must not raise


class _VerifierSource:
    """The minimum a Redmine journal source has to look like for the readback."""

    class Entry:
        def __init__(self, issue_id, journal_id, notes):
            self.issue_id, self.journal_id, self.notes = issue_id, journal_id, notes

    def __init__(self) -> None:
        self.entries: list = []
        self.reads = 0

    def read_entries(self, issue_id):
        self.reads += 1
        return [e for e in self.entries if e.issue_id == str(issue_id)]


class VerifierTest(unittest.TestCase):
    """The authority seam: bounded, accounted, and never caching an absence."""

    def setUp(self) -> None:
        self.source = _VerifierSource()
        self.row = _pending()

    def _append(self, journal_id: str) -> None:
        self.source.entries.append(
            _VerifierSource.Entry(
                REAL_ISSUE, journal_id, f"idempotency_key: {self.row.idempotency_key}"
            )
        )

    def test_an_absent_journal_is_not_cached(self) -> None:
        # The firing this pass is about to WRITE must not be unverifiable for the rest of
        # the pass because a lookup before the write recorded a miss.
        verify = build_journal_verifier(source=self.source)
        self.assertEqual(verify(self.row), "")
        self._append("110264")
        self.assertEqual(verify(self.row), "110264")

    def test_a_found_journal_is_cached(self) -> None:
        # A journal, once written, does not stop existing, so re-reading it costs a pass
        # budget nothing to skip.
        verify = build_journal_verifier(source=self.source)
        self._append("110264")
        self.assertEqual(verify(self.row), "110264")
        self.assertEqual(verify(self.row), "110264")
        self.assertEqual(self.source.reads, 1)

    def test_reads_are_counted_against_the_shared_pass_budget(self) -> None:
        budget = {"reads": 0, "mutated": False, "uncertain": False}
        self._append("110264")
        build_journal_verifier(source=self.source, budget=budget)(self.row)
        self.assertEqual(budget["reads"], 1)
        # A provider read is not an external mutation (Final Disposition j#87188).
        self.assertFalse(budget["mutated"])

    def test_past_the_read_cap_the_authority_answers_nothing(self) -> None:
        # Fail-closed AND visible: the wake is refused as unverified rather than granted on
        # a question nobody asked.
        budget = {"reads": 7, "mutated": False, "uncertain": False}
        self._append("110264")
        verify = build_journal_verifier(source=self.source, budget=budget, read_cap=7)
        self.assertEqual(verify(self.row), "")
        self.assertEqual(self.source.reads, 0)
        # A recorded row whose authority went unasked is refused as UNVERIFIED, which is
        # what makes a capped pass visible in the settle telemetry instead of silent.
        recorded = _pending(journal_id="110264")
        self.assertEqual(admit_wake(recorded, verify(recorded)), WAKE_JOURNAL_UNVERIFIED)

    def test_an_unreadable_source_answers_nothing_rather_than_raising(self) -> None:
        class _Broken:
            def read_entries(self, issue_id):
                raise RuntimeError("redmine unreachable")

        self.assertEqual(build_journal_verifier(source=_Broken())(self.row), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
