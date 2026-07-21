"""Redmine #14213 — workflow glance must not mis-project leaf reviews / deferred integration.

Two defects, both reproduced here from the SHAPES OF THE REAL DURABLE JOURNALS named in the
issue's dogfood evidence, not from invented fixtures:

1. **#14192 type** (safety-critical). Review j#84265 concluded ``approved``; the coordinator
   then recorded Integration Disposition j#84323 with ``explicit_deferral`` (canonical
   integration NOT performed, ``unlock: #14148``). The glance nevertheless projected
   ``coordinator: collect owner close approval`` — it steered a **main-unmerged** issue toward
   close. The old fold discarded deferrals entirely: only a *completion* value set the single
   ``integration_recorded`` boolean, so "deferred" and "never mentioned" were indistinguishable.

2. **#14150 type**. Latest canonical gate was Review Request j#84320 on a ``leaf_issue`` work
   unit, and the row's own ``reconcile.expected_owner`` said ``implementation_gateway`` — yet
   ``next_owner`` said ``auditor`` / "US-level audit". The row contradicted itself because the
   review owner was a constant in a static table with no work-unit input at all.

Also pinned: the disposition supersession that made a boolean fold unsafe in BOTH directions
(#14150 recorded ``explicit_deferral`` j#84424 and later ``merged`` j#84605), the
structured-fields-only rule (acceptance 3), and the #13952 review-heading contract (acceptance 7).
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_integration_disposition import (
    fold_integration_disposition,
    fold_work_unit,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (
    fold_issue_gate_facts,
    lane_signal_from_gate_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    INTEGRATION_BLOCKED,
    INTEGRATION_EXPLICIT_DEFERRAL,
    INTEGRATION_MERGE,
    INTEGRATION_NONE,
    INTEGRATION_PATCH_EQUIVALENT,
    INTEGRATION_UNKNOWN,
    classify_lane_state,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_glance import (
    OWNER_AUDITOR,
    OWNER_COORDINATOR,
    OWNER_IMPLEMENTATION_GATEWAY,
    IssueGlanceSnapshot,
    fold_glance_row,
    next_owner_contradicts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_authority_projection import (
    ReconcileFacts,
)

# ---------------------------------------------------------------------------
# Journal bodies in the exact governed shapes the real records use.
# ---------------------------------------------------------------------------

#: #14192 j#84262 — a leaf Review Request carrying the work-unit declaration and a commit.
J_14192_REVIEW_REQUEST = """## Gate: Review Request — foreign-sender preflight R2 (F1 correction)

`[mozyo:workflow-event:gate=review_request:head=1a5d7d67b79085014cf2b880d92b7c0518b3a7cd]`

- work unit: leaf_issue（worker-dispatch exactly-once fence / external-side-effect safety）
- commit_or_diff: **`1a5d7d67b79085014cf2b880d92b7c0518b3a7cd`**
- 受信 agent: independent same-lane Codex gateway
"""

#: #14192 j#84265 — the approved audit review.
J_14192_REVIEW_APPROVED = """## Gate: Review — R2

`[mozyo:workflow-event:gate=review_result:conclusion=approved:head=1a5d7d67b79085014cf2b880d92b7c0518b3a7cd:req=84262]`

- 結論: 承認
"""

#: #14192 j#84323 — the disposition that the pre-#14213 fold silently dropped. Note the heading
#: is the ``## Coordinator Integration Disposition — <narrative>`` shape (NOT the inline
#: ``## Integration disposition: <value>`` form), and the value lives in a structured field.
J_14192_DEFERRAL = """## Coordinator Integration Disposition — pre-config candidateへ包含 / canonical統合は明示保留

`[mozyo:workflow-event:gate=integration_disposition]`

- same-lane Review Gate: j#84265 **approved**
- disposition: `explicit_deferral`（canonical `origin/main-next` / `origin/main` への統合は未実施）
- unlock: #14148 provider-neutral config v2のsame-lane approval後にcanonical stagingへ統合
- next_owner: coordinator
"""

#: #14150 j#83413 / j#84320 — leaf work unit declared at dispatch, review requested later.
J_14150_DISPATCH = """## Gate: Implementation Request — local drain / provider reconciliation split R1

[mozyo:workflow-event:gate=implementation_request]

- work unit: `leaf_issue`
"""
J_14150_REVIEW_REQUEST = """## Gate: Review Request — R3 完了（F1+F2+F3）

`[mozyo:workflow-event:gate=review_request:head=8934219ef0bf6d2c689f7882e1e5ec29dab1a4df]`

- target_head: `8934219ef0bf6d2c689f7882e1e5ec29dab1a4df`
"""

#: #14150 j#84424 -> j#84605 — a deferral later resolved by a canonical merge.
J_14150_DEFERRAL = """## Coordinator Integration Disposition — explicit_deferral after green pre-config integration

`[mozyo:workflow-event:gate=integration_disposition:disposition=explicit_deferral:head=0c3dd1e1a289a4ad386c7a5f0ec4a80aee2e6eb3]`

- disposition: `explicit_deferral`（canonical integration pending）
- unlock: #14148 provider-neutral config v2のsame-lane approval後
"""
J_14150_MERGED = """## Integration Disposition — canonical staging merged

[mozyo:workflow-event:gate=progress_log]

- disposition: `merged`（pre-config explicit_deferral解除）
- canonical target: `origin/main-next@35b81be8a381309b1d1095001beb6fad4287645d`
"""


def _fold(*journals):
    """Fold ``(journal_id, notes)`` pairs -> ``(facts, lane_state, row)`` (issue open)."""
    facts = fold_issue_gate_facts(list(journals))
    assert facts is not None, "fixture must produce a recognized gate"
    signal = lane_signal_from_gate_facts("x", facts, issue_open=True)
    row = fold_glance_row(
        IssueGlanceSnapshot(
            issue_id="x",
            signal=signal,
            latest_gate_journal=facts.latest_gate_journal,
            work_unit=facts.work_unit,
            integration=facts.integration,
        )
    )
    return facts, classify_lane_state(signal), row


class Issue14192ApprovedThenDeferredTest(unittest.TestCase):
    """Acceptance 1/2/3: an approved review with a recorded deferral is integration-owed."""

    def test_approved_review_with_explicit_deferral_is_not_owner_close(self):
        facts, state, row = _fold(
            ("84262", J_14192_REVIEW_REQUEST),
            ("84265", J_14192_REVIEW_APPROVED),
            ("84323", J_14192_DEFERRAL),
        )
        # The disposition is folded as a typed token, not a discarded non-completion.
        self.assertEqual(facts.integration.disposition, INTEGRATION_EXPLICIT_DEFERRAL)
        self.assertFalse(facts.integration_recorded)  # a deferral is NOT integrated
        self.assertTrue(facts.integration.recorded)  # ...but it IS recorded

        # The defect: this used to be ``owner_waiting`` / "collect owner close approval".
        self.assertEqual(state, "integration_waiting")
        self.assertNotIn("owner close", row.next_action)
        self.assertNotIn("close approval", row.next_action)
        self.assertIn("do not close", row.next_action)
        self.assertIn(INTEGRATION_EXPLICIT_DEFERRAL, row.next_action)

    def test_defer_unlock_and_next_owner_come_from_structured_fields(self):
        # Acceptance 3: projected from the structured fields, never inferred from prose.
        _, _, row = _fold(
            ("84262", J_14192_REVIEW_REQUEST),
            ("84265", J_14192_REVIEW_APPROVED),
            ("84323", J_14192_DEFERRAL),
        )
        self.assertIn("#14148", row.integration.unlock)
        self.assertEqual(row.integration.next_owner, OWNER_COORDINATOR)
        self.assertEqual(row.next_owner, OWNER_COORDINATOR)
        self.assertIn("unlock:", row.next_action)
        self.assertEqual(row.integration.journal, "84323")

    def test_no_structured_reason_is_empty_not_guessed(self):
        # The real j#84323 puts its rationale under a prose ``### 保留理由 / next action``
        # section. Acceptance 3 forbids guessing: an absent structured field stays empty.
        deferral = J_14192_DEFERRAL + "\n### 保留理由 / next action\n\n#14148 が authority を所有するため。\n"
        _, _, row = _fold(
            ("84262", J_14192_REVIEW_REQUEST),
            ("84265", J_14192_REVIEW_APPROVED),
            ("84323", deferral),
        )
        self.assertEqual(row.integration.reason, "")
        self.assertNotIn("reason:", row.next_action)

    def test_approved_review_without_any_disposition_still_reaches_owner_close(self):
        # Non-regression: the fix must not make every approved review look integration-owed.
        _, state, row = _fold(
            ("84262", J_14192_REVIEW_REQUEST),
            ("84265", J_14192_REVIEW_APPROVED),
        )
        self.assertEqual(state, "owner_waiting")
        self.assertEqual(row.integration.disposition, INTEGRATION_NONE)
        self.assertIn("owner close approval", row.next_action)


class Issue14150LeafReviewOwnerTest(unittest.TestCase):
    """Acceptance 4/5: a leaf Review Gate is owed by the same-lane gateway."""

    def test_leaf_review_request_routes_to_the_same_lane_gateway(self):
        facts, state, row = _fold(
            ("83413", J_14150_DISPATCH),
            ("84320", J_14150_REVIEW_REQUEST),
        )
        self.assertEqual(facts.work_unit, "leaf_issue")
        self.assertEqual(state, "review_waiting")
        self.assertEqual(row.next_owner, OWNER_IMPLEMENTATION_GATEWAY)
        self.assertNotIn("US-level audit", row.next_action)

    def test_next_owner_does_not_contradict_reconcile_expected_owner(self):
        # Acceptance 5: the exact contradiction the dogfood row showed —
        # next_owner=auditor beside reconcile.expected_owner=implementation_gateway.
        facts = fold_issue_gate_facts(
            [("83413", J_14150_DISPATCH), ("84320", J_14150_REVIEW_REQUEST)]
        )
        row = fold_glance_row(
            IssueGlanceSnapshot(
                issue_id="14150",
                signal=lane_signal_from_gate_facts("14150", facts, issue_open=True),
                latest_gate_journal=facts.latest_gate_journal,
                work_unit=facts.work_unit,
                integration=facts.integration,
                reconcile=ReconcileFacts(
                    expected_gate="review_result",
                    expected_owner=OWNER_IMPLEMENTATION_GATEWAY,
                ),
            )
        )
        self.assertEqual(row.next_owner, row.reconcile.expected_owner)
        self.assertFalse(row.next_owner_conflicts_reconcile)
        self.assertFalse(row.as_payload()["next_owner_conflicts_reconcile"])

    def test_us_level_audit_is_claimed_only_on_positive_evidence(self):
        # Acceptance 4: a declared ``user_story`` unit — and ONLY that — routes to the auditor.
        us_dispatch = "## Gate: Implementation Request — US lane\n\n- work_unit: `user_story`\n"
        _, state, row = _fold(("83413", us_dispatch), ("84320", J_14150_REVIEW_REQUEST))
        self.assertEqual(state, "review_waiting")
        self.assertEqual(row.next_owner, OWNER_AUDITOR)
        self.assertIn("US-level audit", row.next_action)

    def test_undeclared_work_unit_does_not_assume_us_audit(self):
        _, _, row = _fold(("84320", J_14150_REVIEW_REQUEST))
        self.assertEqual(row.work_unit, "")
        self.assertEqual(row.next_owner, OWNER_IMPLEMENTATION_GATEWAY)
        self.assertNotIn("US-level audit", row.next_action)

    def test_out_of_vocabulary_work_unit_folds_to_undeclared(self):
        bogus = "## Gate: Implementation Request\n\n- work_unit: `whatever_unit`\n"
        _, _, row = _fold(("83413", bogus), ("84320", J_14150_REVIEW_REQUEST))
        self.assertEqual(row.work_unit, "")
        self.assertEqual(row.next_owner, OWNER_IMPLEMENTATION_GATEWAY)


class DispositionSupersessionTest(unittest.TestCase):
    """A later disposition wins — the boolean fold was unsafe in both directions."""

    def test_deferral_then_merged_resolves_to_integrated(self):
        facts, state, _ = _fold(
            ("84262", J_14192_REVIEW_REQUEST),
            ("84265", J_14192_REVIEW_APPROVED),
            ("84424", J_14150_DEFERRAL),
            ("84605", J_14150_MERGED),
        )
        self.assertEqual(facts.integration.disposition, INTEGRATION_MERGE)
        self.assertTrue(facts.integration_recorded)
        self.assertEqual(state, "owner_waiting")  # integration no longer owed

    def test_merged_then_reverted_to_deferral_reopens_integration(self):
        facts, state, _ = _fold(
            ("84262", J_14192_REVIEW_REQUEST),
            ("84265", J_14192_REVIEW_APPROVED),
            ("84605", J_14150_MERGED),
            ("84700", J_14150_DEFERRAL),
        )
        self.assertEqual(facts.integration.disposition, INTEGRATION_EXPLICIT_DEFERRAL)
        self.assertEqual(state, "integration_waiting")


class DispositionVocabularyTest(unittest.TestCase):
    """Acceptance 1: the four canonical kinds all fold; unreadable fails closed to pending."""

    def _disposition(self, value: str) -> str:
        note = f"## Integration disposition: {value}\n"
        return fold_integration_disposition([("10", note)]).disposition

    def test_canonical_kinds(self):
        self.assertEqual(self._disposition("merge"), INTEGRATION_MERGE)
        self.assertEqual(self._disposition("patch_equivalent"), INTEGRATION_PATCH_EQUIVALENT)
        self.assertEqual(self._disposition("explicit_deferral"), INTEGRATION_EXPLICIT_DEFERRAL)
        self.assertEqual(self._disposition("integration_blocked"), INTEGRATION_BLOCKED)

    def test_legacy_spellings_still_fold(self):
        # Acceptance 7 / historical records: ``merged`` etc. must keep folding.
        self.assertEqual(self._disposition("merged"), INTEGRATION_MERGE)
        self.assertEqual(self._disposition("cherry_picked"), INTEGRATION_PATCH_EQUIVALENT)
        self.assertEqual(self._disposition("deferred"), INTEGRATION_EXPLICIT_DEFERRAL)

    def test_unreadable_disposition_is_pending_not_complete(self):
        # A disposition journal we cannot read must never project as "integration done".
        self.assertEqual(self._disposition("something_new"), INTEGRATION_UNKNOWN)
        _, state, _ = _fold(
            ("84262", J_14192_REVIEW_REQUEST),
            ("84265", J_14192_REVIEW_APPROVED),
            ("84323", "## Coordinator Integration Disposition — narrative only\n"),
        )
        self.assertEqual(state, "integration_waiting")

    def test_integration_blocked_keeps_the_lane_out_of_close(self):
        blocked = (
            "## Integration disposition: integration_blocked\n"
            "- reason: exact-head CI red\n"
            "- unlock: rerun exact-head CI green\n"
        )
        _, state, row = _fold(
            ("84262", J_14192_REVIEW_REQUEST),
            ("84265", J_14192_REVIEW_APPROVED),
            ("84323", blocked),
        )
        self.assertEqual(state, "integration_waiting")
        self.assertIn("reason: exact-head CI red", row.next_action)

    def test_a_passing_mention_is_not_a_disposition(self):
        # Structural gate: only an integration-disposition heading / marker qualifies, so a
        # stray ``disposition:`` line in an unrelated journal cannot steer the fold.
        stray = "## Gate: Progress\n\n- disposition: `merged` (talking about another issue)\n"
        self.assertEqual(
            fold_integration_disposition([("10", stray)]).disposition, INTEGRATION_NONE
        )


class ReadOnlyAndContractTest(unittest.TestCase):
    """Acceptance 7/8: no regression of the review-heading contract; projection stays pure."""

    def test_13952_review_heading_contract_is_not_regressed(self):
        # The #13952 suffixed heading + explicit conclusion must still fold to an approved review.
        facts, state, _ = _fold(
            ("100", "## Gate: Review Request — R1\n\n- commit: `deadbeef1234567`\n"),
            ("101", "## Review Gate — 承認\n\n- 結論: 承認\n"),
        )
        self.assertEqual((facts.latest_gate, facts.review_conclusion), ("review", "approved"))
        self.assertEqual(state, "owner_waiting")

    def test_review_finding_verdict_collision_guard_holds(self):
        self.assertIsNone(
            fold_issue_gate_facts([("100", "## Gate: Review Finding Verdicts\n- 結論: 承認")])
        )

    def test_contradiction_helper_treats_role_aliases_as_equivalent(self):
        # ``worker`` and ``implementation_worker`` name the same role; not a contradiction.
        self.assertFalse(next_owner_contradicts("worker", "implementation_worker"))
        self.assertFalse(next_owner_contradicts("implementation_gateway", "implementation_gateway"))
        self.assertFalse(next_owner_contradicts("coordinator", ""))  # no expectation recorded
        self.assertTrue(next_owner_contradicts("auditor", "implementation_gateway"))

    def test_fold_does_not_mutate_its_inputs(self):
        journals = [
            ("84262", J_14192_REVIEW_REQUEST),
            ("84265", J_14192_REVIEW_APPROVED),
            ("84323", J_14192_DEFERRAL),
        ]
        before = [(j, n) for j, n in journals]
        fold_issue_gate_facts(journals)
        fold_integration_disposition(journals)
        fold_work_unit(journals)
        self.assertEqual(journals, before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
