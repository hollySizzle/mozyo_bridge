"""Production-path tests for the canonical IR dispatch writer (Redmine #13758 R6-F4 / R7 / j#79507 Q2).

Drives the real write -> readback -> handoff sequence of
:func:`...application.reconcile_dispatch_writer.dispatch_implementation_request` with injected
Redmine + handoff seams (a capturing ``post_note``, a production-shape journals ``read_entries``,
and an EXECUTED ``handoff_send`` port) — the production path the review required, NOT a
``render_dispatch_note`` helper unit test:

- the written IR note carries the machine dispatch marker; the anchor is the readback's OWNING entry
  journal id (never a self-reported field); the handoff is EXECUTED with that anchor (R7-F2);
- pre-read idempotency: an existing marker is recovered with NO new write, and a readback-failure
  retry converges to the same anchor without a duplicate marker (R7-F3);
- required route identity is validated before any write; a handoff that does not deliver is not
  sendable (R7-F4 / R7-F2).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_dispatch_writer import (
    DISPATCH_ANCHOR_UNRESOLVED,
    DISPATCH_HANDOFF_FAILED,
    DISPATCH_INPUT_INVALID,
    DISPATCH_READBACK_FAILED,
    DISPATCH_RECOVERED,
    DISPATCH_WRITE_FAILED,
    DISPATCH_WRITTEN,
    DispatchRoute,
    HandoffOutcome,
    build_ir_handoff_argv,
    build_live_vocabulary,
    dispatch_implementation_request,
    validate_dispatch_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    dispatch_entry_journals,
    render_dispatch_marker,
)

# Use the REAL handoff vocabulary (packaged role templates + receiver/source sets) so the tests
# pin the production route contract, not a hand-rolled stand-in (review R8-F3).
_VOCAB = build_live_vocabulary()
_ROUTE = DispatchRoute(
    to="claude", target="mzb1_ws1_claude_la", target_repo="/repos/mozyo", lane="lane-a",
    gateway_callback_target="mzb1_ws1_codex_la",
)


class _FakeRedmine:
    """A Redmine double: ``post_note`` records the note as a server-assigned journal entry."""

    def __init__(self, *, assigned_journal_id="79600", preexisting=(), fail_read_calls=()):
        self.assigned_journal_id = assigned_journal_id
        self.entries = list(preexisting)
        self.posted = []
        self._fail_read_calls = set(fail_read_calls)  # 1-based read_entries call indices to fail
        self._read_calls = 0

    def post_note(self, issue, note):
        self.posted.append((issue, note))
        self.entries.append(
            RedmineJournalEntry(issue_id=str(issue), journal_id=self.assigned_journal_id, notes=note)
        )
        return ""  # Redmine 204: no journal id in the write response

    def read_entries(self, issue):
        self._read_calls += 1
        if self._read_calls in self._fail_read_calls:
            raise RuntimeError("transient readback error")
        return list(self.entries)


class _Handoff:
    """A handoff port double that records the anchor it was executed with."""

    def __init__(self, *, delivered=True):
        self.calls = []
        self._delivered = delivered

    def __call__(self, anchor):
        self.calls.append(anchor)
        return HandoffOutcome(delivered=self._delivered, detail="" if self._delivered else "blocked")


def _dispatch(redmine, handoff, *, route=_ROUTE, vocab=_VOCAB, lane="lane-a", generation=1, issue="13758"):
    return dispatch_implementation_request(
        issue=issue, lane=lane, lane_generation=generation,
        body="## Gate: Implementation Request\nevent-driven reconcile",
        route=route, vocab=vocab, post_note=redmine.post_note, read_entries=redmine.read_entries,
        handoff_send=handoff,
    )


class FreshDispatchTest(unittest.TestCase):
    def test_marker_written_anchor_from_readback_handoff_executed(self):
        redmine, handoff = _FakeRedmine(assigned_journal_id="79600"), _Handoff()
        result = _dispatch(redmine, handoff)
        self.assertEqual(result.status, DISPATCH_WRITTEN)
        self.assertTrue(result.sendable)
        self.assertTrue(result.handoff_delivered)
        # (1) exactly one marker-bearing note was written.
        self.assertEqual(len(redmine.posted), 1)
        self.assertIn(render_dispatch_marker("lane-a", 1), redmine.posted[0][1])
        # (2) the anchor is the readback entry's OWN server-assigned journal id.
        self.assertEqual(result.dispatch_journal, "79600")
        # (3) the handoff was EXECUTED with that anchor (not merely a printed string).
        self.assertEqual(handoff.calls, ["79600"])

    def test_handoff_not_delivered_is_not_sendable(self):
        redmine, handoff = _FakeRedmine(), _Handoff(delivered=False)
        result = _dispatch(redmine, handoff)
        self.assertEqual(result.status, DISPATCH_HANDOFF_FAILED)
        self.assertFalse(result.sendable)
        self.assertEqual(result.dispatch_journal, "79600")  # marker persists for a retry
        self.assertEqual(handoff.calls, ["79600"])  # it was attempted


class IdempotencyTest(unittest.TestCase):
    def test_existing_marker_is_recovered_without_a_new_write(self):
        note = f"body\n\n{render_dispatch_marker('lane-a', 1)}"
        pre = [RedmineJournalEntry(issue_id="13758", journal_id="79337", notes=note)]
        redmine, handoff = _FakeRedmine(preexisting=pre), _Handoff()
        result = _dispatch(redmine, handoff)
        self.assertEqual(result.status, DISPATCH_RECOVERED)
        self.assertTrue(result.sendable)
        self.assertEqual(result.dispatch_journal, "79337")
        self.assertEqual(redmine.posted, [])  # NO new write (idempotent recover)
        self.assertEqual(handoff.calls, ["79337"])

    def test_readback_failure_then_retry_recovers_same_anchor_no_duplicate(self):
        # review R7-F3: first attempt pre-reads ok (call#1), writes, then its POST-write readback
        # (call#2) fails; the durable marker persists. A same-input retry pre-reads (call#3), finds
        # the one marker, and RECOVERS it — no 2nd write, no duplicate marker, same anchor. (The old
        # writer wrote unconditionally and created a 2nd marker -> permanently ambiguous.)
        redmine, handoff = _FakeRedmine(assigned_journal_id="79600", fail_read_calls={2}), _Handoff()
        first = _dispatch(redmine, handoff)
        self.assertEqual(first.status, DISPATCH_READBACK_FAILED)
        self.assertFalse(first.sendable)
        self.assertEqual(len(redmine.posted), 1)  # the write happened once
        self.assertEqual(handoff.calls, [])  # no anchor -> no handoff yet
        # retry: pre-read now succeeds, finds the single marker, recovers it.
        second = _dispatch(redmine, handoff)
        self.assertEqual(second.status, DISPATCH_RECOVERED)
        self.assertEqual(second.dispatch_journal, "79600")
        self.assertEqual(len(redmine.posted), 1)  # STILL one write -> no duplicate marker
        self.assertEqual(
            dispatch_entry_journals(redmine.entries, lane="lane-a", lane_generation=1), ("79600",)
        )
        self.assertEqual(handoff.calls, ["79600"])

    def test_pre_read_failure_does_not_write(self):
        # a pre-read (call#1) failure fails closed WITHOUT writing -> a retry can't create a duplicate.
        redmine, handoff = _FakeRedmine(fail_read_calls={1}), _Handoff()
        result = _dispatch(redmine, handoff)
        self.assertEqual(result.status, DISPATCH_READBACK_FAILED)
        self.assertEqual(redmine.posted, [])  # nothing written
        self.assertEqual(handoff.calls, [])

    def test_ambiguous_preexisting_never_adds_another_marker(self):
        note = f"body\n\n{render_dispatch_marker('lane-a', 1)}"
        pre = [
            RedmineJournalEntry(issue_id="13758", journal_id="79111", notes=note),
            RedmineJournalEntry(issue_id="13758", journal_id="79222", notes=note),
        ]
        redmine, handoff = _FakeRedmine(preexisting=pre), _Handoff()
        result = _dispatch(redmine, handoff)
        self.assertEqual(result.status, DISPATCH_ANCHOR_UNRESOLVED)
        self.assertEqual(redmine.posted, [])  # never adds a 3rd marker
        self.assertEqual(handoff.calls, [])


class FailClosedTest(unittest.TestCase):
    def test_write_failure_fails_closed(self):
        redmine, handoff = _FakeRedmine(), _Handoff()

        def boom_post(issue, note):
            raise RuntimeError("redmine write down")

        result = dispatch_implementation_request(
            issue="13758", lane="lane-a", lane_generation=1, body="body", route=_ROUTE, vocab=_VOCAB,
            post_note=boom_post, read_entries=redmine.read_entries, handoff_send=handoff,
        )
        self.assertEqual(result.status, DISPATCH_WRITE_FAILED)
        self.assertFalse(result.sendable)
        self.assertEqual(handoff.calls, [])

    def test_missing_route_identity_fails_closed_no_write(self):
        # review R7-F4: an empty --target / --target-repo never writes and is never sendable.
        redmine, handoff = _FakeRedmine(), _Handoff()
        bad = DispatchRoute(
            to="claude", target="", target_repo="", lane="lane-a", gateway_callback_target="gw",
        )
        result = _dispatch(redmine, handoff, route=bad)
        self.assertEqual(result.status, DISPATCH_INPUT_INVALID)
        self.assertFalse(result.sendable)
        self.assertEqual(redmine.posted, [])  # validated BEFORE any write
        self.assertEqual(handoff.calls, [])
        self.assertIn("target", result.detail)

    def test_missing_gateway_callback_target_fails_closed_no_write(self):
        # review R8-F3: the implementation_worker role template requires <gateway_callback_target>;
        # omitting it is an unresolved placeholder that must fail closed BEFORE any write.
        redmine, handoff = _FakeRedmine(), _Handoff()
        bad = DispatchRoute(
            to="claude", target="mzb1_ws1_claude_la", target_repo="/repos/mozyo", lane="lane-a",
            gateway_callback_target="",
        )
        result = _dispatch(redmine, handoff, route=bad)
        self.assertEqual(result.status, DISPATCH_INPUT_INVALID)
        self.assertEqual(redmine.posted, [])
        self.assertIn("gateway_callback_target", result.detail)

    def test_invalid_vocabulary_fails_closed_before_write(self):
        # review R8-F3: an unknown receiver/source/role the handoff parser would reject must NOT
        # write a marker journal first (the old non-empty-only check let it through).
        redmine, handoff = _FakeRedmine(), _Handoff()
        for bad in (
            DispatchRoute(to="not-a-provider", target="t", target_repo="/r", lane="lane-a",
                          gateway_callback_target="gw"),
            DispatchRoute(to="claude", target="t", target_repo="/r", lane="lane-a",
                          gateway_callback_target="gw", source="not-a-source"),
            DispatchRoute(to="claude", target="t", target_repo="/r", lane="lane-a",
                          gateway_callback_target="gw", role_profile="not-a-role"),
        ):
            redmine.posted.clear()
            result = _dispatch(redmine, handoff, route=bad)
            self.assertEqual(result.status, DISPATCH_INPUT_INVALID)
            self.assertEqual(redmine.posted, [])  # never wrote a marker for a parser-rejected route
        self.assertEqual(handoff.calls, [])


class DeliveryOutcomeTest(unittest.TestCase):
    def test_only_a_positively_delivered_handoff_is_sendable(self):
        # review R8-F1: a handoff whose structured outcome is not positive (e.g. pending_input /
        # queue_enter, both rc 0) must NOT be reported delivered — the worker never got the input.
        redmine, handoff = _FakeRedmine(), _Handoff(delivered=False)
        result = _dispatch(redmine, handoff)
        self.assertEqual(result.status, DISPATCH_HANDOFF_FAILED)
        self.assertFalse(result.sendable)
        self.assertFalse(result.handoff_delivered)
        self.assertEqual(result.dispatch_journal, "79600")  # marker persists for a retry
        self.assertEqual(handoff.calls, ["79600"])


class RouteValidationTest(unittest.TestCase):
    """review R8-F3: validate_dispatch_route pins the vocabulary + placeholder contract (pure)."""

    def test_valid_worker_route_has_no_problems(self):
        self.assertEqual(validate_dispatch_route(_ROUTE, _VOCAB), ())

    def test_unknown_role_short_circuits(self):
        bad = DispatchRoute(to="claude", target="t", target_repo="/r", lane="lane-a",
                            gateway_callback_target="gw", role_profile="not-a-role")
        problems = validate_dispatch_route(bad, _VOCAB)
        self.assertIn("role_profile:not-a-role", problems)


class HandoffArgvTest(unittest.TestCase):
    def test_argv_carries_anchor_target_repo_and_role_profile_fields(self):
        argv = build_ir_handoff_argv("79600", _ROUTE, issue="13758")
        self.assertEqual(argv[:3], ["handoff", "send", "--to"])
        self.assertIn("--journal", argv)
        self.assertEqual(argv[argv.index("--journal") + 1], "79600")
        self.assertEqual(argv[argv.index("--target-repo") + 1], "/repos/mozyo")
        self.assertEqual(argv[argv.index("--role-profile") + 1], "implementation_worker")
        self.assertEqual(argv[argv.index("--kind") + 1], "implementation_request")
        # both role placeholders are carried (lane + the required gateway_callback_target).
        pf = [argv[i + 1] for i, v in enumerate(argv) if v == "--profile-field"]
        self.assertIn("lane=lane-a", pf)
        self.assertIn("gateway_callback_target=mzb1_ws1_codex_la", pf)


if __name__ == "__main__":
    unittest.main()
