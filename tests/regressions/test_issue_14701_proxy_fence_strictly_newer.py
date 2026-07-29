"""Redmine #14701: an EQUAL journal ordinal minted a new proxy generation past a terminal row.

``CoordinatorProxyFence.reserve``'s contract, the cataloged spec
(``external-client-coordinator-proxy.md`` §3b / §4) and the delivery-terminal design answer
(j#90329 contract 1) all say the same thing: past a terminal generation, only a **strictly newer**
canonical decision mints the next one. The comparison shipped as ``want_ordinal < prior_ordinal``
(cause commit ``c25b1352``, the fence's introduction), which refuses only a SMALLER ordinal. Equal
was therefore admitted — a fresh ``reserved`` generation and a real delivery for a decision that
does not supersede the delegated one.

The exact ``(issue, journal)`` repeat is caught earlier as a permanent duplicate, so what the ``<``
let through was an equal ordinal reached with a *different anchor string*:

- **a different issue** — the input the fix is named for. Redmine journal ids are unique across the
  instance, so one ordinal naming two issues is not a newer decision but an unresolvable anchor, and
  the fence is the wrong place to guess which one moved the record forward;
- **the same issue written differently** — ``"089736"`` against a stored ``"89736"``.
  ``is_redmine_id`` admits leading zeros, so two spellings of one journal are not string-equal and
  slid past the duplicate check into the ordinal comparison.

Both are pinned here, at the fence and (for the different-issue case) through the public delegation
path with a counting send port, because the defect's consequence is a **send**: `stale` owes the
caller zero-write AND zero-send, and only driving the rail can assert the second one. The positive
controls are part of the same pin — a fix that refused everything, or one that re-admitted a
different issue on some other ground, would break the rail while passing a refusal-only test — so
the boundary is asserted as the triple (prior-1 stale / prior stale / prior+1 wins) and the verdict
PRECEDENCE (duplicate and in-flight refusals still classify before the ordinal) is asserted too.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.coordinator_proxy_fence import (  # noqa: E501
    PROXY_ABANDONED,
    PROXY_COMPLETED,
    PROXY_DELIVERED,
    PROXY_RESERVED,
    PROXY_UNCERTAIN,
    RESERVE_DUPLICATE,
    RESERVE_STALE,
    RESERVE_WON,
    CoordinatorProxyFence,
    ProxyRouteKey,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E501
    FENCE_STALE,
    REASON_STALE,
    ZERO_SEND,
)

# The delegation path's own harness (context resolution + counting send port). Standing in for it
# with a local copy would let the copy drift from the rail this file is making claims about;
# importing a sibling suite's harness into a regression pin is established here (see
# test_issue_14753_unicode_digit_typed_refusal importing the hibernate unit harness).
from tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff import (  # noqa: E501
    test_coordinator_proxy_send as S,
)

ROUTE = ProxyRouteKey(
    workspace_id="ws1", lane_id="default", role="coordinator", action="dispatch_next"
)

#: The delegated decision every case below is measured against.
PRIOR_ISSUE = "14546"
PRIOR_JOURNAL = "89736"
#: A different issue carrying the SAME journal ordinal — the admission this issue closes.
OTHER_ISSUE = "14500"
#: The same journal ordinal spelled differently, so it is not string-equal to the stored value.
PADDED_JOURNAL = "089736"


class StrictlyNewerFenceTestBase(unittest.TestCase):
    """A fence whose route already holds a TERMINAL generation for ``(PRIOR_*)``."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fence = CoordinatorProxyFence(Path(tmp.name) / "proxy.sqlite")
        self.fence.bootstrap()

    def _snapshot(self):
        row = self.fence.active(ROUTE)
        return (row.action_id, row.state, row.issue, row.journal)

    def _terminal(self, state: str = PROXY_DELIVERED):
        """Drive the route to ``state`` through the real transitions, never a hand-written row."""
        first = self.fence.reserve(ROUTE, issue=PRIOR_ISSUE, journal=PRIOR_JOURNAL)
        self.assertTrue(first.won, first)
        anchor = dict(issue=PRIOR_ISSUE, journal=PRIOR_JOURNAL)
        if state == PROXY_ABANDONED:
            self.assertTrue(self.fence.mark_uncertain(ROUTE, first.action_id, **anchor))
            self.assertTrue(
                self.fence.mark_abandoned(
                    ROUTE, first.action_id, detail="proven not sent", **anchor
                )
            )
        else:
            self.assertTrue(self.fence.mark_delivered(ROUTE, first.action_id, **anchor))
            if state == PROXY_COMPLETED:
                self.assertTrue(self.fence.complete(ROUTE, first.action_id))
        self.assertEqual(self.fence.active(ROUTE).state, state)
        return first

    def _refused_without_writing(self, verdict: str, *, issue: str, journal: str):
        """Reserve ``(issue, journal)``, and assert the refusal changed nothing on the route."""
        before = self._snapshot()
        result = self.fence.reserve(ROUTE, issue=issue, journal=journal)
        self.assertFalse(result.won, result)
        self.assertEqual(result.verdict, verdict, result)
        self.assertEqual(self._snapshot(), before, "a refusal must write nothing")
        return result


class EqualOrdinalIsNotStrictlyNewerTest(StrictlyNewerFenceTestBase):
    """The defect itself: equal is not strictly newer, whatever issue carries it."""

    def test_an_equal_ordinal_on_a_different_issue_is_stale(self):
        # Pre-fix this returned `won=True` with a fresh action id and moved the row to `reserved`.
        self._terminal()
        result = self._refused_without_writing(
            RESERVE_STALE, issue=OTHER_ISSUE, journal=PRIOR_JOURNAL
        )
        self.assertEqual(result.prior_state, PROXY_DELIVERED)
        self.assertEqual(result.prior_issue, PRIOR_ISSUE)
        self.assertEqual(result.prior_journal, PRIOR_JOURNAL)

    def test_it_is_stale_on_every_terminal_state_not_just_delivered(self):
        # `delivered` / `abandoned` / legacy `completed` are one terminal class for admission; a fix
        # applied to the state split rather than to the comparison would pass only the first.
        for state in (PROXY_DELIVERED, PROXY_ABANDONED, PROXY_COMPLETED):
            with self.subTest(prior_state=state):
                self.setUp()
                self._terminal(state)
                self._refused_without_writing(
                    RESERVE_STALE, issue=OTHER_ISSUE, journal=PRIOR_JOURNAL
                )

    def test_an_equal_ordinal_spelled_with_leading_zeros_is_stale(self):
        # Same issue, same ordinal, different string: not caught by the exact-anchor duplicate
        # check, so `<` admitted it as a superseding decision.
        self._terminal()
        self._refused_without_writing(
            RESERVE_STALE, issue=PRIOR_ISSUE, journal=PADDED_JOURNAL
        )

    def test_a_padded_stored_journal_is_not_superseded_by_its_bare_spelling(self):
        # The same collision from the other side: the stored value carries the padding.
        first = self.fence.reserve(ROUTE, issue=PRIOR_ISSUE, journal=PADDED_JOURNAL)
        self.assertTrue(first.won, first)
        self.assertTrue(
            self.fence.mark_delivered(
                ROUTE, first.action_id, issue=PRIOR_ISSUE, journal=PADDED_JOURNAL
            )
        )
        self._refused_without_writing(
            RESERVE_STALE, issue=PRIOR_ISSUE, journal=PRIOR_JOURNAL
        )

    def test_the_stale_detail_names_the_decision_that_blocks_this_one(self):
        self._terminal()
        result = self.fence.reserve(ROUTE, issue=OTHER_ISSUE, journal=PRIOR_JOURNAL)
        self.assertIn(PRIOR_JOURNAL, result.detail)
        self.assertIn("supersede", result.detail)


class AdmissionBoundaryTest(StrictlyNewerFenceTestBase):
    """The boundary sits exactly at strictly-greater — in BOTH directions."""

    def _reserve_at(self, offset: int, *, issue: str):
        self.setUp()
        self._terminal()
        journal = str(int(PRIOR_JOURNAL) + offset)
        return self.fence.reserve(ROUTE, issue=issue, journal=journal), journal

    def test_the_triple_around_the_prior_ordinal_on_the_same_issue(self):
        for offset, expected in ((-1, RESERVE_STALE), (0, RESERVE_DUPLICATE), (1, RESERVE_WON)):
            with self.subTest(offset=offset):
                # offset 0 IS the same `(issue, journal)`, so its verdict is duplicate, not stale:
                # the exact-anchor check must keep classifying before the ordinal comparison.
                result, _ = self._reserve_at(offset, issue=PRIOR_ISSUE)
                self.assertEqual(result.verdict, expected, result)
                self.assertIs(result.won, expected == RESERVE_WON, result)

    def test_the_triple_around_the_prior_ordinal_on_a_different_issue(self):
        for offset, expected in ((-1, RESERVE_STALE), (0, RESERVE_STALE), (1, RESERVE_WON)):
            with self.subTest(offset=offset):
                result, _ = self._reserve_at(offset, issue=OTHER_ISSUE)
                self.assertEqual(result.verdict, expected, result)
                self.assertIs(result.won, expected == RESERVE_WON, result)

    def test_a_strictly_newer_decision_on_a_different_issue_still_mints(self):
        # The positive control the fix must not break: a different issue is not a refusal ground of
        # its own, so a genuinely newer ordinal proceeds and the row advances to it.
        first = self._terminal()
        newer = str(int(PRIOR_JOURNAL) + 1)
        result = self.fence.reserve(ROUTE, issue=OTHER_ISSUE, journal=newer)
        self.assertTrue(result.won, result)
        self.assertNotEqual(result.action_id, first.action_id)
        row = self.fence.active(ROUTE)
        self.assertEqual((row.state, row.issue, row.journal), (PROXY_RESERVED, OTHER_ISSUE, newer))


class RefusalPrecedenceIsUnchangedTest(StrictlyNewerFenceTestBase):
    """`<=` must not swallow the refusals that classify BEFORE the ordinal comparison."""

    def test_the_exact_decision_stays_a_permanent_duplicate(self):
        self._terminal()
        result = self._refused_without_writing(
            RESERVE_DUPLICATE, issue=PRIOR_ISSUE, journal=PRIOR_JOURNAL
        )
        self.assertIn("delegated once", result.detail)

    def test_an_in_flight_generation_still_refuses_as_duplicate_not_stale(self):
        # An `uncertain` row is not terminal: an equal ordinal on another issue must be refused for
        # the stronger reason (a send whose fate is unknown), never reclassified as merely stale.
        first = self.fence.reserve(ROUTE, issue=PRIOR_ISSUE, journal=PRIOR_JOURNAL)
        self.assertTrue(
            self.fence.mark_uncertain(
                ROUTE, first.action_id, issue=PRIOR_ISSUE, journal=PRIOR_JOURNAL
            )
        )
        self.assertEqual(self.fence.active(ROUTE).state, PROXY_UNCERTAIN)
        self._refused_without_writing(
            RESERVE_DUPLICATE, issue=OTHER_ISSUE, journal=PRIOR_JOURNAL
        )

    def test_a_non_numeric_journal_still_fails_closed_as_stale(self):
        self._terminal()
        for token in ("", "j89736", "89736a", "-89736", "89736.0"):
            with self.subTest(journal=token):
                self._refused_without_writing(
                    RESERVE_STALE, issue=OTHER_ISSUE, journal=token
                )


class EqualOrdinalSendsNothingTest(S.ProxySendTestBase):
    """Through the PUBLIC rail: the refusal owes the caller zero-write AND zero-send.

    The fence is the last link, so an admitted equal ordinal was not a bookkeeping slip — it reached
    the send port and delivered a decision that does not supersede the delegated one. The counting
    port makes that a measured claim rather than an inferred one.
    """

    def _route(self):
        return ProxyRouteKey(
            workspace_id=S.WS, lane_id="default", role=S.ROLE_COORDINATOR,
            action=S.ACTION_DISPATCH_NEXT,
        )

    def _deliver_other_issue_at(self, journal: str):
        """Put a DELIVERED generation for a different issue at ``journal`` on the route."""
        self.fence.bootstrap()
        route = self._route()
        first = self.fence.reserve(route, issue=OTHER_ISSUE, journal=journal)
        self.assertTrue(first.won, first)
        self.assertTrue(
            self.fence.mark_delivered(
                route, first.action_id, issue=OTHER_ISSUE, journal=journal
            )
        )
        return first

    def test_an_equal_ordinal_from_another_issue_delivers_nothing(self):
        # The canonical context's journal (`S.CURRENT_JOURNAL`) equals the delegated ordinal while
        # naming a different issue: every non-fence link verifies, and the fence must still refuse.
        first = self._deliver_other_issue_at(S.CURRENT_JOURNAL)
        result, port = self._execute(self._context())
        self.assertFalse(result.sent)
        self.assertEqual(result.decision, ZERO_SEND)
        self.assertEqual(result.reason, REASON_STALE)
        self.assertEqual(result.fence_state, FENCE_STALE)
        self.assertEqual(port.calls, [], "a stale delegation must not reach the send port")
        row = self.fence.active(self._route())
        self.assertEqual(
            (row.action_id, row.state, row.issue, row.journal),
            (first.action_id, PROXY_DELIVERED, OTHER_ISSUE, S.CURRENT_JOURNAL),
        )

    def test_a_strictly_newer_ordinal_from_another_issue_still_delivers_once(self):
        # The rail-level positive control: the fix refuses `equal`, not `different issue`.
        self._deliver_other_issue_at(S.OLDER_JOURNAL)
        result, port = self._execute(self._context())
        self.assertTrue(result.sent, result)
        self.assertEqual(len(port.calls), 1)
        self.assertEqual(port.calls[0][1], S.CURRENT_JOURNAL)


if __name__ == "__main__":
    unittest.main()
