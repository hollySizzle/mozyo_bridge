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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.stall_escalation import (  # noqa: E402
    PendingEscalation,
    StallEscalationStore,
    escalation_idempotency_key,
)
from mozyo_bridge.core.state.stall_pending_contract import (  # noqa: E402
    PENDING_UNRENDERABLE,
    StallPendingContractError,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_escalation_pass import (  # noqa: E402,E501
    SETTLE_NOTHING_PENDING,
    WRITE_RECORDED,
    JournalWriteResult,
    settle_pending_escalations,
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
    """Records every pending row it is asked to write, and which issue it was aimed at."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, pending):
        self.calls.append(pending)
        return JournalWriteResult(
            outcome=WRITE_RECORDED, journal_id="110200", reason="recorded"
        )

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
