"""Coordinator-proxy delegation fence tests (Redmine #14546).

Pins the exactly-once contract an external client's delegation depends on. The caller has no
runtime of its own, so "run the command again" is the normal way a retry happens — which makes the
two properties below the whole safety story:

- a **completed** generation does NOT re-open the route for the same decision. Every sibling fence
  in this codebase re-mints after completion (that is correct for a repeating forward); this one
  must not, because a durable decision is delegated once. A fence that re-opened would turn an
  innocent re-run into a second delivery of an action the coordinator already performed;
- a journal that does not **supersede** the delegated one is stale, compared numerically. Redmine
  journal ids are integers, so a string comparison would sort ``"9"`` after ``"10"`` and let an old
  decision through as if it were newer.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# `parents[4]` is the repo root from `tests/unit/core/state/`, matching every sibling in this
# directory. It read `parents[3]` — i.e. `tests/` — so the self-insert added a `tests/src` that does
# not exist. A full discovery run hid it: a sibling module imported earlier had already put the real
# `src` on `sys.path`, so this module free-rode on that side effect and only single-file isolated
# discovery failed (review j#95727 F2). Each test module must stand on its own.
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.coordinator_proxy_fence import (  # noqa: E501
    PROXY_COMPLETED,
    PROXY_DELIVERED,
    PROXY_RESERVED,
    PROXY_ABANDONED,
    PROXY_UNCERTAIN,
    RESERVE_DUPLICATE,
    RESERVE_NEEDS_RECONCILE,
    RESERVE_STALE,
    RESERVE_WON,
    CoordinatorProxyFence,
    CoordinatorProxyFenceError,
    ProxyRouteKey,
    journal_ordinal,
)

ROUTE = ProxyRouteKey(
    workspace_id="ws1", lane_id="default", role="coordinator", action="dispatch_next"
)
OTHER_ACTION = ProxyRouteKey(
    workspace_id="ws1", lane_id="default", role="coordinator", action="workflow_step"
)
OTHER_WS = ProxyRouteKey(
    workspace_id="ws2", lane_id="default", role="coordinator", action="dispatch_next"
)


class FenceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fence = CoordinatorProxyFence(Path(self._tmp.name) / "proxy.sqlite")

    def _bootstrapped(self) -> CoordinatorProxyFence:
        self.fence.bootstrap()
        return self.fence


class JournalOrdinalTest(unittest.TestCase):
    def test_numeric_journals_order_numerically(self):
        self.assertEqual(journal_ordinal("9"), 9)
        self.assertEqual(journal_ordinal(" 10 "), 10)
        self.assertLess(journal_ordinal("9"), journal_ordinal("10"))

    def test_non_numeric_is_none(self):
        for value in ("", None, "j89688", "12a", "-1", "1.0"):
            self.assertIsNone(journal_ordinal(value), value)


class BootstrapTest(FenceTestBase):
    def test_execution_path_sees_an_unbootstrapped_store(self):
        self.assertFalse(self.fence.is_bootstrapped())
        with self.assertRaises(CoordinatorProxyFenceError):
            self.fence.reserve(ROUTE, issue="1", journal="1")

    def test_bootstrap_is_idempotent(self):
        self.fence.bootstrap()
        self.fence.bootstrap()
        self.assertTrue(self.fence.is_bootstrapped())

    def test_a_lost_db_with_a_surviving_sidecar_fails_closed(self):
        self._bootstrapped()
        self.fence.path.unlink()
        self.assertFalse(self.fence.is_bootstrapped())
        with self.assertRaises(CoordinatorProxyFenceError):
            self.fence.bootstrap()  # never silently re-creates
        with self.assertRaises(CoordinatorProxyFenceError):
            self.fence.reserve(ROUTE, issue="1", journal="1")

    def test_a_replaced_store_fails_closed_on_the_nonce(self):
        self._bootstrapped()
        self.fence.sidecar_path.write_text("a-different-nonce", encoding="utf-8")
        self.assertFalse(self.fence.is_bootstrapped())
        with self.assertRaises(CoordinatorProxyFenceError):
            self.fence.reserve(ROUTE, issue="1", journal="1")

    def test_recover_mints_a_fresh_usable_store(self):
        self._bootstrapped()
        self.fence.sidecar_path.write_text("mismatched", encoding="utf-8")
        self.fence.recover()
        self.assertTrue(self.fence.is_bootstrapped())
        self.assertTrue(self.fence.reserve(ROUTE, issue="1", journal="1").won)


class ExactlyOnceTest(FenceTestBase):
    def test_first_reserve_wins_and_mints_an_opaque_id(self):
        fence = self._bootstrapped()
        result = fence.reserve(ROUTE, issue="14546", journal="89688")
        self.assertTrue(result.won)
        self.assertEqual(result.verdict, RESERVE_WON)
        self.assertTrue(result.action_id.startswith("pxy_"))
        self.assertEqual(fence.active(ROUTE).state, PROXY_RESERVED)

    def test_a_delivered_generation_is_terminal_and_admits_a_newer_decision(self):
        # Design Answer j#90329 contract 1: the proxy delivers a decision and cannot prove the
        # coordinator acted, so a positively recorded delivery IS its terminal success. It no
        # longer holds the route — only an in-flight (reserved / uncertain) generation does.
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="89688")
        second = fence.reserve(ROUTE, issue="14546", journal="89999")
        self.assertTrue(second.won)
        self.assertEqual(second.prior_state, PROXY_DELIVERED)

    def test_an_inflight_generation_refuses_a_second_reserve(self):
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        fence.mark_uncertain(ROUTE, first.action_id, issue="14546", journal="89688")
        second = fence.reserve(ROUTE, issue="14546", journal="89999")
        self.assertFalse(second.won)
        self.assertEqual(second.verdict, RESERVE_DUPLICATE)
        self.assertEqual(second.prior_state, PROXY_UNCERTAIN)

    def test_the_same_decision_is_duplicate_after_delivery(self):
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="89688")
        repeat = fence.reserve(ROUTE, issue="14546", journal="89688")
        self.assertFalse(repeat.won)
        self.assertEqual(repeat.verdict, RESERVE_DUPLICATE)

    def test_an_outcome_write_naming_a_foreign_anchor_changes_nothing(self):
        # Contract 3: every outcome write joins the generation's exact stored issue+journal.
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        self.assertFalse(
            fence.mark_delivered(ROUTE, first.action_id, issue="99999", journal="89688")
        )
        self.assertFalse(
            fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="99999")
        )
        self.assertEqual(fence.active(ROUTE).state, PROXY_RESERVED)
        self.assertTrue(
            fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="89688")
        )

    def test_proven_not_sent_abandons_and_reopens_delegation(self):
        # Contract 4: the strongest reconcile disposition, and the only one that re-opens retry.
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        fence.mark_uncertain(ROUTE, first.action_id, issue="14546", journal="89688")
        self.assertTrue(
            fence.mark_abandoned(
                ROUTE, first.action_id, detail="proven not sent",
                issue="14546", journal="89688",
            )
        )
        self.assertEqual(fence.active(ROUTE).state, PROXY_ABANDONED)
        self.assertTrue(fence.reserve(ROUTE, issue="14546", journal="89999").won)
        # ...but never the decision that was abandoned.
        self.assertFalse(fence.reserve(ROUTE, issue="14546", journal="89688").won)

    def test_confirmed_delivered_resolves_uncertain_to_the_terminal_success(self):
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        fence.mark_uncertain(ROUTE, first.action_id, issue="14546", journal="89688")
        self.assertTrue(
            fence.confirm_delivered(
                ROUTE, first.action_id, detail="proven landed", issue="14546", journal="89688"
            )
        )
        self.assertEqual(fence.active(ROUTE).state, PROXY_DELIVERED)
        self.assertTrue(fence.reserve(ROUTE, issue="14546", journal="89999").won)

    def test_a_disposition_naming_a_foreign_anchor_changes_nothing(self):
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        fence.mark_uncertain(ROUTE, first.action_id, issue="14546", journal="89688")
        self.assertFalse(
            fence.mark_abandoned(
                ROUTE, first.action_id, detail="e", issue="99999", journal="89688"
            )
        )
        self.assertEqual(fence.active(ROUTE).state, PROXY_UNCERTAIN)

    def test_a_legacy_completed_generation_stays_readable_and_terminal(self):
        # `complete()` is the withdrawn acknowledgement transition (j#90329 contract 2). Nothing
        # produces it any more; rows that already carry it must still read as a terminal state.
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="89688")
        self.assertTrue(fence.complete(ROUTE, first.action_id))
        self.assertEqual(fence.active(ROUTE).state, PROXY_COMPLETED)

        repeat = fence.reserve(ROUTE, issue="14546", journal="89688")
        self.assertFalse(repeat.won)
        self.assertEqual(repeat.verdict, RESERVE_DUPLICATE)
        self.assertTrue(fence.reserve(ROUTE, issue="14546", journal="89999").won)

    def test_a_terminal_generation_admits_a_strictly_newer_decision(self):
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="89688")

        newer = fence.reserve(ROUTE, issue="14546", journal="89736")
        self.assertTrue(newer.won)
        self.assertNotEqual(newer.action_id, first.action_id)
        self.assertEqual(fence.active(ROUTE).journal, "89736")

    def test_an_older_journal_is_stale_not_a_new_generation(self):
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89736")
        fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="89736")

        older = fence.reserve(ROUTE, issue="14546", journal="89688")
        self.assertFalse(older.won)
        self.assertEqual(older.verdict, RESERVE_STALE)
        self.assertEqual(older.prior_journal, "89736")

    def test_staleness_is_numeric_not_lexicographic(self):
        # "9" > "10" as strings; the comparison must be on integers.
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="10")
        fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="10")

        older = fence.reserve(ROUTE, issue="14546", journal="9")
        self.assertFalse(older.won)
        self.assertEqual(older.verdict, RESERVE_STALE)

    def test_a_non_numeric_journal_never_wins(self):
        fence = self._bootstrapped()
        result = fence.reserve(ROUTE, issue="14546", journal="j89688")
        self.assertFalse(result.won)
        self.assertEqual(result.verdict, RESERVE_STALE)

    def test_an_unresolved_prior_reserve_needs_reconcile_and_never_auto_retries(self):
        fence = self._bootstrapped()
        fence.reserve(ROUTE, issue="14546", journal="89688")  # crash before marking the outcome
        again = fence.reserve(ROUTE, issue="14546", journal="89688")
        self.assertFalse(again.won)
        self.assertEqual(again.verdict, RESERVE_NEEDS_RECONCILE)
        self.assertEqual(fence.active(ROUTE).state, PROXY_UNCERTAIN)
        # And an uncertain generation still refuses (it is never blind-retried).
        third = fence.reserve(ROUTE, issue="14546", journal="89688")
        self.assertFalse(third.won)
        self.assertEqual(third.verdict, RESERVE_DUPLICATE)

    def test_a_different_issue_on_a_completed_route_still_needs_to_supersede(self):
        # The route is per (workspace, lane, role, action): a different issue is not automatically
        # newer, so it must still carry a superseding journal ordinal.
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89736")
        fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="89736")

        older_other_issue = fence.reserve(ROUTE, issue="14500", journal="89626")
        self.assertFalse(older_other_issue.won)
        self.assertEqual(older_other_issue.verdict, RESERVE_STALE)

    def test_a_superseding_journal_on_a_different_issue_mints_the_next_generation(self):
        # The other half of the rule above: a different issue is not a refusal ground of its own.
        # The route carries decisions, not issues, so a genuinely newer ordinal proceeds whoever
        # raised it, and the row advances to that decision.
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89736")
        fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="89736")

        newer_other_issue = fence.reserve(ROUTE, issue="14500", journal="89999")
        self.assertTrue(newer_other_issue.won)
        self.assertNotEqual(newer_other_issue.action_id, first.action_id)
        row = fence.active(ROUTE)
        self.assertEqual((row.state, row.issue, row.journal), (PROXY_RESERVED, "14500", "89999"))

    def test_an_inflight_generation_refuses_whatever_the_candidates_ordinal(self):
        # The in-flight refusal is classified BEFORE the ordinal comparison, so an `uncertain` row
        # answers `duplicate` (a send whose fate is unknown) and never the weaker `stale` — for a
        # newer candidate, and equally for one whose ordinal does not supersede.
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89736")
        fence.mark_uncertain(ROUTE, first.action_id, issue="14546", journal="89736")

        for issue, journal in (("14500", "89736"), ("14500", "89626"), ("14546", "89999")):
            with self.subTest(issue=issue, journal=journal):
                result = fence.reserve(ROUTE, issue=issue, journal=journal)
                self.assertFalse(result.won, result)
                self.assertEqual(result.verdict, RESERVE_DUPLICATE, result)
                self.assertEqual(result.prior_state, PROXY_UNCERTAIN)

    def test_a_non_numeric_journal_never_wins_against_a_terminal_row_either(self):
        # `test_a_non_numeric_journal_never_wins` covers the fresh route (no row). The terminal
        # branch reaches the same fail-closed answer through its own arm of the comparison, so an
        # unreadable candidate cannot supersede a real delegated decision.
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89736")
        fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="89736")

        for journal in ("", "j89736", "89736a", "-89736", "89736.0"):
            with self.subTest(journal=journal):
                result = fence.reserve(ROUTE, issue="14500", journal=journal)
                self.assertFalse(result.won, result)
                self.assertEqual(result.verdict, RESERVE_STALE, result)
                self.assertEqual(fence.active(ROUTE).journal, "89736")


class RouteIsolationTest(FenceTestBase):
    def test_distinct_actions_hold_independent_generations(self):
        fence = self._bootstrapped()
        a = fence.reserve(ROUTE, issue="14546", journal="89688")
        b = fence.reserve(OTHER_ACTION, issue="14546", journal="89688")
        self.assertTrue(a.won)
        self.assertTrue(b.won)
        self.assertNotEqual(a.action_id, b.action_id)

    def test_distinct_workspaces_hold_independent_generations(self):
        fence = self._bootstrapped()
        self.assertTrue(fence.reserve(ROUTE, issue="14546", journal="89688").won)
        self.assertTrue(fence.reserve(OTHER_WS, issue="14546", journal="89688").won)


class GuardedWritesTest(FenceTestBase):
    def test_a_stale_action_id_never_clobbers_a_newer_generation(self):
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        fence.mark_delivered(ROUTE, first.action_id, issue="14546", journal="89688")
        newer = fence.reserve(ROUTE, issue="14546", journal="89736")

        self.assertFalse(fence.mark_delivered(ROUTE, first.action_id))
        self.assertFalse(fence.complete(ROUTE, first.action_id))
        self.assertEqual(fence.active(ROUTE).action_id, newer.action_id)
        self.assertEqual(fence.active(ROUTE).state, PROXY_RESERVED)

    def test_complete_only_advances_a_delivered_generation(self):
        fence = self._bootstrapped()
        first = fence.reserve(ROUTE, issue="14546", journal="89688")
        self.assertFalse(fence.complete(ROUTE, first.action_id))  # still reserved
        fence.mark_uncertain(ROUTE, first.action_id)
        self.assertFalse(fence.complete(ROUTE, first.action_id))  # uncertain needs a reconcile


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
