"""Redmine #14701: an EQUAL journal ordinal minted a new proxy generation past a terminal row.

``CoordinatorProxyFence.reserve``'s contract, the cataloged spec
(``external-client-coordinator-proxy.md`` §3b / §4) and the delivery-terminal design answer
(j#90329 contract 1) all say the same thing: past a terminal generation, only a **strictly newer**
canonical decision mints the next one. The comparison shipped as ``want_ordinal < prior_ordinal``
(cause commit ``c25b1352``, the fence's introduction), which refuses only a SMALLER ordinal. Equal
was therefore admitted — a fresh ``reserved`` generation and a real delivery for a decision that
does not supersede the delegated one. Fixed by ``<=``.

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
caller zero-write AND zero-send, and only driving the rail can assert the second one.

**Every test in this file detects the symptom's return: restore ``<`` and all of them fail.**
Measured, not asserted — the first version of this file also carried the fix's positive controls
(strictly newer still mints) and its verdict-precedence claims (duplicate / in-flight still classify
before the ordinal). Those stayed GREEN under the restored defect because they state the module's
public contract rather than this symptom's return, which is what
``tests-placement-discovery-policy.md`` `### regressions` R3-b forbids in a regressions file (review
j#94494 F1, verdict j#94497). They now live where the decision tree puts them: the fence-level ones
in ``tests/unit/core/state/test_coordinator_proxy_fence.py`` (branch 4) and the rail-level one in
``tests/integration/.../test_coordinator_proxy_send.py`` (branch 5). The boundary triple's other two
arms and the exact-repeat duplicate were already covered there and were not duplicated.
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

    def _stale_without_writing(self, *, issue: str, journal: str):
        """Reserve ``(issue, journal)``, and assert it was refused as stale, writing nothing."""
        before = self._snapshot()
        result = self.fence.reserve(ROUTE, issue=issue, journal=journal)
        self.assertFalse(result.won, result)
        self.assertEqual(result.verdict, RESERVE_STALE, result)
        self.assertEqual(self._snapshot(), before, "a refusal must write nothing")
        return result


class EqualOrdinalIsNotStrictlyNewerTest(StrictlyNewerFenceTestBase):
    """The defect itself: equal is not strictly newer, whatever issue carries it."""

    def test_an_equal_ordinal_on_a_different_issue_is_stale(self):
        # Pre-fix this returned `won=True` with a fresh action id and moved the row to `reserved`.
        self._terminal()
        result = self._stale_without_writing(issue=OTHER_ISSUE, journal=PRIOR_JOURNAL)
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
                self._stale_without_writing(issue=OTHER_ISSUE, journal=PRIOR_JOURNAL)

    def test_an_equal_ordinal_spelled_with_leading_zeros_is_stale(self):
        # Same issue, same ordinal, different string: not caught by the exact-anchor duplicate
        # check, so `<` admitted it as a superseding decision.
        self._terminal()
        self._stale_without_writing(issue=PRIOR_ISSUE, journal=PADDED_JOURNAL)

    def test_a_padded_stored_journal_is_not_superseded_by_its_bare_spelling(self):
        # The same collision from the other side: the stored value carries the padding.
        first = self.fence.reserve(ROUTE, issue=PRIOR_ISSUE, journal=PADDED_JOURNAL)
        self.assertTrue(first.won, first)
        self.assertTrue(
            self.fence.mark_delivered(
                ROUTE, first.action_id, issue=PRIOR_ISSUE, journal=PADDED_JOURNAL
            )
        )
        self._stale_without_writing(issue=PRIOR_ISSUE, journal=PRIOR_JOURNAL)

    def test_the_stale_detail_names_the_decision_that_blocks_this_one(self):
        self._terminal()
        result = self.fence.reserve(ROUTE, issue=OTHER_ISSUE, journal=PRIOR_JOURNAL)
        self.assertFalse(result.won, result)
        self.assertIn(PRIOR_JOURNAL, result.detail)
        self.assertIn("supersede", result.detail)

    def test_the_boundary_admits_only_a_greater_ordinal_on_a_different_issue(self):
        # The equal arm is the symptom; the +1 arm is here because it is what makes the equal arm a
        # BOUNDARY claim rather than a blanket refusal — the two are one assertion about where the
        # line sits, and pre-fix the pair could not both hold.
        self._terminal()
        equal = self.fence.reserve(ROUTE, issue=OTHER_ISSUE, journal=PRIOR_JOURNAL)
        self.assertEqual(equal.verdict, RESERVE_STALE, equal)
        greater = str(int(PRIOR_JOURNAL) + 1)
        admitted = self.fence.reserve(ROUTE, issue=OTHER_ISSUE, journal=greater)
        self.assertEqual(admitted.verdict, RESERVE_WON, admitted)
        self.assertEqual(self.fence.active(ROUTE).journal, greater)


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

    def test_an_equal_ordinal_from_another_issue_delivers_nothing(self):
        # The canonical context's journal (`S.CURRENT_JOURNAL`) equals the delegated ordinal while
        # naming a different issue: every non-fence link verifies, and the fence must still refuse.
        # Pre-fix `result.sent` was True — a real second delivery, measured on the shipped head.
        self.fence.bootstrap()
        route = self._route()
        first = self.fence.reserve(route, issue=OTHER_ISSUE, journal=S.CURRENT_JOURNAL)
        self.assertTrue(first.won, first)
        self.assertTrue(
            self.fence.mark_delivered(
                route, first.action_id, issue=OTHER_ISSUE, journal=S.CURRENT_JOURNAL
            )
        )

        result, port = self._execute(self._context())
        self.assertFalse(result.sent)
        self.assertEqual(result.decision, ZERO_SEND)
        self.assertEqual(result.reason, REASON_STALE)
        self.assertEqual(result.fence_state, FENCE_STALE)
        self.assertEqual(port.calls, [], "a stale delegation must not reach the send port")
        row = self.fence.active(route)
        self.assertEqual(
            (row.action_id, row.state, row.issue, row.journal),
            (first.action_id, PROXY_DELIVERED, OTHER_ISSUE, S.CURRENT_JOURNAL),
        )


if __name__ == "__main__":
    unittest.main()
