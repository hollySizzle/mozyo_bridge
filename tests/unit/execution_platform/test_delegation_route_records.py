"""Classical tests for the live executor Redmine record package (#12558).

Focused, executor-independent tests of the structured record builders, the
ordered package, and the fail-closed final-classification vocabulary fixed by
``vibes/docs/logics/delegated-coordinator-real-machine-acceptance.md``
(``## Failure Classification`` / ``## Redmine Record Package``) and
``vibes/docs/specs/delegated-coordinator-decision-records.md``.

The three invariants under test: the classification buckets are never conflated
(precedence + fail-closed validation), notification success alone is never
evidence (callback completeness + classification inputs), and no private pane id
leaks into a public record.

Hermetic: pure dataclass construction; no tmux, no Redmine, no host paths.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.delegation_route_records import (  # noqa: E402
    CALLBACK_PENDING,
    CALLBACK_SENT,
    CLASS_BLOCKED,
    CLASS_CONTAMINATED,
    CLASS_ENVIRONMENTAL,
    CLASS_FAILED_ACCEPTANCE,
    CLASS_INSUFFICIENT,
    CLASS_PASS,
    NON_PASS_CLASSIFICATIONS,
    RECORD_BASELINE,
    RECORD_CHILD_DELIVERY,
    RECORD_FINAL_CLASSIFICATION,
    VALID_CLASSIFICATIONS,
    CallbackOutcome,
    ClassificationInputs,
    NullRouteRecordSink,
    RouteExecutionRecord,
    RouteRecordError,
    RouteRecordPackage,
    all_required_callbacks_recorded,
    baseline_record,
    child_delivery_record,
    classify_final,
    final_classification_record,
    validate_classification,
)
from mozyo_bridge.domain.route_identity_ledger import (  # noqa: E402
    RESOLVE_OK,
    RouteIdentity,
    RouteResolution,
)


def _resolution(status=RESOLVE_OK, pane_id="%42"):
    identity = RouteIdentity(
        workspace_id="ws-child",
        lane_id="lane-deleg",
        role="codex",
        pane_name="child-gw",
        route_id="rt-child",
        last_seen_pane_id="%10",
    )
    return RouteResolution(
        status=status,
        route_id="rt-child",
        resolved_pane_id=pane_id,
        identity=identity,
        pane_id_refreshed=True,
        considered=1,
        detail="ok",
    )


class ClassificationVocabularyTest(unittest.TestCase):
    def test_buckets_are_distinct(self):
        self.assertEqual(len(VALID_CLASSIFICATIONS), 6)
        self.assertNotIn(CLASS_PASS, NON_PASS_CLASSIFICATIONS)
        self.assertEqual(NON_PASS_CLASSIFICATIONS, VALID_CLASSIFICATIONS - {CLASS_PASS})

    def test_validate_classification_fails_closed(self):
        for token in VALID_CLASSIFICATIONS:
            self.assertEqual(validate_classification(token), token)
        with self.assertRaises(RouteRecordError):
            validate_classification("almost_pass")
        with self.assertRaises(RouteRecordError):
            validate_classification("")


class ClassificationPrecedenceTest(unittest.TestCase):
    def test_clean_route_passes(self):
        cls, _ = classify_final(ClassificationInputs())
        self.assertEqual(cls, CLASS_PASS)

    def test_precedence_order(self):
        # contaminated outranks every other signal.
        cls, _ = classify_final(
            ClassificationInputs(contaminated=True, invariant_violation=True,
                                 blocked=True, environmental=True)
        )
        self.assertEqual(cls, CLASS_CONTAMINATED)
        # invariant outranks blocked / environmental.
        cls, _ = classify_final(
            ClassificationInputs(invariant_violation=True, blocked=True,
                                 environmental=True)
        )
        self.assertEqual(cls, CLASS_FAILED_ACCEPTANCE)
        # blocked outranks environmental.
        cls, _ = classify_final(
            ClassificationInputs(blocked=True, environmental=True)
        )
        self.assertEqual(cls, CLASS_BLOCKED)

    def test_write_failure_is_environmental_non_pass(self):
        cls, reason = classify_final(ClassificationInputs(redmine_write_failed=True))
        self.assertEqual(cls, CLASS_ENVIRONMENTAL)
        self.assertEqual(reason, "redmine_record_write_failed")

    def test_partial_route_and_unrecorded_callback_are_insufficient(self):
        self.assertEqual(
            classify_final(ClassificationInputs(route_fully_realized=False))[0],
            CLASS_INSUFFICIENT,
        )
        self.assertEqual(
            classify_final(ClassificationInputs(callbacks_recorded=False))[0],
            CLASS_INSUFFICIENT,
        )
        self.assertEqual(
            classify_final(ClassificationInputs(insufficient_read=True))[0],
            CLASS_INSUFFICIENT,
        )


class CallbackCompletenessTest(unittest.TestCase):
    def test_required_pending_is_not_recorded(self):
        targets = (CallbackOutcome("delegation_parent", "r:#1", True, CALLBACK_PENDING),)
        self.assertFalse(all_required_callbacks_recorded(targets))

    def test_required_sent_is_recorded(self):
        targets = (CallbackOutcome("delegation_parent", "r:#1", True, CALLBACK_SENT),)
        self.assertTrue(all_required_callbacks_recorded(targets))

    def test_empty_targets_is_fail_closed(self):
        # An empty callback set is not a satisfied contract (never a vacuous PASS).
        self.assertFalse(all_required_callbacks_recorded(()))

    def test_callback_requires_purpose_and_route(self):
        with self.assertRaises(RouteRecordError):
            CallbackOutcome("", "r:#1", True)
        with self.assertRaises(RouteRecordError):
            CallbackOutcome("delegation_parent", "", True)


class RecordPublicSafetyTest(unittest.TestCase):
    def test_resolved_pane_id_stays_out_of_public_surface(self):
        record = child_delivery_record(
            source_issue="#12557",
            resolution=_resolution(pane_id="%42"),
            role_profile="delegated_coordinator",
            send_outcome="sent",
        )
        self.assertIn(("resolved_pane_id", "%42"), record.runtime_evidence)
        self.assertNotIn("%42", record.public_markdown())
        self.assertNotIn("%42", str(record.to_record()))
        # The public route-identity resolution still names the stable route.
        self.assertIn("route=rt-child", record.public_markdown())

    def test_unknown_record_kind_fails_closed(self):
        with self.assertRaises(RouteRecordError):
            RouteExecutionRecord(kind="not_a_kind", source_issue="#1")

    def test_record_requires_source_issue(self):
        with self.assertRaises(RouteRecordError):
            RouteExecutionRecord(kind=RECORD_BASELINE, source_issue="")


class RecordPackageOrderTest(unittest.TestCase):
    def test_out_of_order_append_fails_closed(self):
        pkg = RouteRecordPackage(source_issue="#12557")
        pkg.append(final_classification_record(
            source_issue="#12557", classification=CLASS_PASS, reason="r"))
        with self.assertRaises(RouteRecordError):
            pkg.append(baseline_record(
                source_issue="#12557", test_model="autonomous_parent",
                fresh_panes=True, base_commit="ddb0a29"))

    def test_source_issue_mismatch_fails_closed(self):
        pkg = RouteRecordPackage(source_issue="#12557")
        with self.assertRaises(RouteRecordError):
            pkg.append(baseline_record(
                source_issue="#9999", test_model="x", fresh_panes=True,
                base_commit="c"))

    def test_in_order_appends_accumulate(self):
        pkg = RouteRecordPackage(source_issue="#12557")
        pkg.append(baseline_record(
            source_issue="#12557", test_model="autonomous_parent",
            fresh_panes=True, base_commit="ddb0a29"))
        pkg.append(child_delivery_record(
            source_issue="#12557", resolution=_resolution(),
            role_profile="delegated_coordinator", send_outcome="sent"))
        self.assertEqual(pkg.kinds(), (RECORD_BASELINE, RECORD_CHILD_DELIVERY))
        self.assertTrue(pkg.has_kind(RECORD_CHILD_DELIVERY))


class NullSinkTest(unittest.TestCase):
    def test_null_sink_reports_disabled_non_persisted(self):
        receipt = NullRouteRecordSink().persist(
            baseline_record(source_issue="#1", test_model="x", fresh_panes=True,
                            base_commit="c"))
        self.assertFalse(receipt.persisted)


if __name__ == "__main__":
    unittest.main()
