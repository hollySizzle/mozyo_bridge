"""Fail-closed Redmine Version operation preflight tests (Redmine #12651).

Covers: package-vs-planning name classification, the operation-and-target
confirmation token, and the per-operation guards for rename / close / lock /
delete — including the fail-closed defaults (unknown operation, missing
confirmation, non-empty delete, package-numbered rename, open-issue close).
Pure domain; no I/O.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.domain.redmine_version_operation import (
    VERSION_OPERATIONS,
    RedmineVersionWrites,
    VersionOperationError,
    VersionOperationRequest,
    VersionState,
    classify_version_name,
    confirmation_token_for,
    decide_version_operation,
)


def _state(
    version_id="281",
    name="delegated coordinator cockpit UX follow-up",
    status="open",
    issues_count=0,
    open_issues_count=0,
    closed_issues_count=0,
    counts_known=True,
) -> VersionState:
    return VersionState(
        version_id=version_id,
        name=name,
        status=status,
        issues_count=issues_count,
        open_issues_count=open_issues_count,
        closed_issues_count=closed_issues_count,
        counts_known=counts_known,
    )


class NamePolicyTest(unittest.TestCase):
    def test_package_numbered_names_detected(self) -> None:
        for name in (
            "v0.10.22",
            "v0.10.13 test architecture / bounded-context refactor",
            "0.9.0 production",
            "v1.2",
        ):
            self.assertEqual(classify_version_name(name), "package_numbered", name)

    def test_planning_bucket_names_detected(self) -> None:
        for name in (
            "roadmap metadata cleanup",
            "production PyPI release readiness",
            "ワークフロー管制基盤整備枠",
        ):
            self.assertEqual(classify_version_name(name), "planning_bucket", name)

    def test_confirmation_token_is_operation_and_target_specific(self) -> None:
        self.assertEqual(confirmation_token_for("delete", "281"), "delete:281")
        self.assertNotEqual(
            confirmation_token_for("delete", "281"),
            confirmation_token_for("close", "281"),
        )


class ConfirmationGateTest(unittest.TestCase):
    def test_every_operation_requires_confirmation(self) -> None:
        for op in VERSION_OPERATIONS:
            request = VersionOperationRequest(operation=op, state=_state(), new_name="ok bucket")
            decision = decide_version_operation(request)
            self.assertFalse(decision.allowed, op)
            self.assertIn("confirmation_required", decision.blocked_reasons, op)
            self.assertFalse(decision.confirmation_satisfied, op)

    def test_wrong_token_still_blocks(self) -> None:
        request = VersionOperationRequest(
            operation="delete", state=_state(), confirmation="delete:999"
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("confirmation_required", decision.blocked_reasons)


class UnknownOperationTest(unittest.TestCase):
    def test_unknown_operation_fails_closed_with_no_step(self) -> None:
        request = VersionOperationRequest(
            operation="purge", state=_state(), confirmation="purge:281"
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("unknown_operation:purge", decision.blocked_reasons)
        self.assertIsNone(decision.rest_step)
        self.assertIsNone(decision.operator_ui_step)


class DeleteGuardTest(unittest.TestCase):
    def test_empty_version_delete_is_allowed_and_emits_step(self) -> None:
        request = VersionOperationRequest(
            operation="delete", state=_state(), confirmation="delete:281"
        )
        decision = decide_version_operation(request)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.rest_step, "DELETE /versions/281.json")
        self.assertIsNotNone(decision.operator_ui_step)

    def test_non_empty_version_delete_is_blocked(self) -> None:
        request = VersionOperationRequest(
            operation="delete",
            state=_state(issues_count=11, open_issues_count=4, closed_issues_count=7),
            confirmation="delete:281",
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("version_not_empty", decision.blocked_reasons)
        self.assertIsNone(decision.rest_step)

    def test_historical_protected_version_delete_is_blocked(self) -> None:
        request = VersionOperationRequest(
            operation="delete",
            state=_state(),
            confirmation="delete:281",
            historical_protected=True,
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("historical_protected", decision.blocked_reasons)

    def test_delete_with_unknown_counts_fails_closed(self) -> None:
        # Regression (j#69311 finding 1): a missing/defaulted count must never be
        # read as an empty Version for the one irreversible operation.
        request = VersionOperationRequest(
            operation="delete",
            state=_state(counts_known=False),
            confirmation="delete:281",
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("counts_required", decision.blocked_reasons)
        self.assertNotIn("version_not_empty", decision.blocked_reasons)
        self.assertIsNone(decision.rest_step)

    def test_delete_blocked_when_open_count_nonzero_but_issues_count_zero(self) -> None:
        # Inconsistent snapshot: issues_count==0 but open_issues_count>0 must
        # still block (all three counts are checked).
        request = VersionOperationRequest(
            operation="delete",
            state=_state(issues_count=0, open_issues_count=3, closed_issues_count=0),
            confirmation="delete:281",
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("version_not_empty", decision.blocked_reasons)

    def test_delete_blocked_when_only_closed_count_nonzero(self) -> None:
        request = VersionOperationRequest(
            operation="delete",
            state=_state(issues_count=0, open_issues_count=0, closed_issues_count=5),
            confirmation="delete:281",
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("version_not_empty", decision.blocked_reasons)


class RenameGuardTest(unittest.TestCase):
    def test_rename_to_planning_name_allowed(self) -> None:
        request = VersionOperationRequest(
            operation="rename",
            state=_state(name="old bucket"),
            new_name="roadmap metadata cleanup",
            confirmation="rename:281",
        )
        decision = decide_version_operation(request)
        self.assertTrue(decision.allowed)
        self.assertIn("name", decision.rest_step)

    def test_rename_to_package_numbered_name_blocked(self) -> None:
        request = VersionOperationRequest(
            operation="rename",
            state=_state(),
            new_name="v0.11.0 next",
            confirmation="rename:281",
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("new_name_package_numbered", decision.blocked_reasons)

    def test_rename_requires_new_name(self) -> None:
        request = VersionOperationRequest(
            operation="rename", state=_state(), confirmation="rename:281"
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("new_name_required", decision.blocked_reasons)

    def test_rename_unchanged_name_blocked(self) -> None:
        request = VersionOperationRequest(
            operation="rename",
            state=_state(name="same bucket"),
            new_name="same bucket",
            confirmation="rename:281",
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("new_name_unchanged", decision.blocked_reasons)


class CloseLockGuardTest(unittest.TestCase):
    def test_close_empty_open_version_allowed(self) -> None:
        request = VersionOperationRequest(
            operation="close", state=_state(), confirmation="close:281"
        )
        decision = decide_version_operation(request)
        self.assertTrue(decision.allowed)
        self.assertIn('"status": "closed"', decision.rest_step)

    def test_close_with_open_issues_blocked_without_override(self) -> None:
        request = VersionOperationRequest(
            operation="close",
            state=_state(issues_count=4, open_issues_count=4),
            confirmation="close:281",
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("open_issues_present", decision.blocked_reasons)

    def test_close_with_open_issues_allowed_with_override_but_warns(self) -> None:
        request = VersionOperationRequest(
            operation="close",
            state=_state(issues_count=4, open_issues_count=4),
            confirmation="close:281",
            allow_open_issues=True,
        )
        decision = decide_version_operation(request)
        self.assertTrue(decision.allowed)
        self.assertIn("open_issues_present", decision.warnings)

    def test_already_closed_close_blocked(self) -> None:
        request = VersionOperationRequest(
            operation="close", state=_state(status="closed"), confirmation="close:281"
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("already_closed", decision.blocked_reasons)

    def test_already_locked_lock_blocked(self) -> None:
        request = VersionOperationRequest(
            operation="lock", state=_state(status="locked"), confirmation="lock:281"
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("already_locked", decision.blocked_reasons)

    def test_unknown_status_close_fails_closed(self) -> None:
        request = VersionOperationRequest(
            operation="close", state=_state(status="bogus"), confirmation="close:281"
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("unknown_status:bogus", decision.blocked_reasons)

    def test_close_with_unknown_counts_fails_closed(self) -> None:
        # Regression (j#69311): the open-issue guard trusts open_issues_count, so
        # an absent reading must block rather than assume zero open issues.
        request = VersionOperationRequest(
            operation="close",
            state=_state(counts_known=False),
            confirmation="close:281",
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("counts_required", decision.blocked_reasons)

    def test_lock_with_unknown_counts_fails_closed(self) -> None:
        request = VersionOperationRequest(
            operation="lock",
            state=_state(counts_known=False),
            confirmation="lock:281",
        )
        decision = decide_version_operation(request)
        self.assertFalse(decision.allowed)
        self.assertIn("counts_required", decision.blocked_reasons)


class VersionStateParseTest(unittest.TestCase):
    def test_from_mapping_parses_list_versions_entry(self) -> None:
        state = VersionState.from_mapping(
            {
                "id": "248",
                "name": "v0.10.14 ...",
                "status": "OPEN",
                "issues_count": 11,
                "open_issues_count": 4,
                "closed_issues_count": 7,
            }
        )
        self.assertEqual(state.version_id, "248")
        self.assertEqual(state.status, "open")
        self.assertEqual(state.open_issues_count, 4)
        self.assertTrue(state.counts_known)

    def test_from_mapping_without_counts_is_counts_unknown(self) -> None:
        state = VersionState.from_mapping({"id": "281", "name": "bucket", "status": "open"})
        self.assertFalse(state.counts_known)

    def test_from_mapping_with_unparseable_count_is_counts_unknown(self) -> None:
        # Regression (j#69325): a present-but-non-numeric count must NOT be
        # trusted as a genuine 0 (which would let delete pass).
        state = VersionState.from_mapping(
            {
                "id": "999",
                "name": "bad counts",
                "status": "open",
                "issues_count": "not-a-number",
                "open_issues_count": "not-a-number",
                "closed_issues_count": "not-a-number",
            }
        )
        self.assertFalse(state.counts_known)
        decision = decide_version_operation(
            VersionOperationRequest(
                operation="delete", state=state, confirmation="delete:999"
            )
        )
        self.assertFalse(decision.allowed)
        self.assertIn("counts_required", decision.blocked_reasons)
        self.assertIsNone(decision.rest_step)

    def test_from_mapping_with_partial_unparseable_count_is_counts_unknown(self) -> None:
        state = VersionState.from_mapping(
            {
                "id": "999",
                "status": "open",
                "issues_count": 0,
                "open_issues_count": "x",
                "closed_issues_count": 0,
            }
        )
        self.assertFalse(state.counts_known)

    def test_from_mapping_with_negative_count_is_counts_unknown(self) -> None:
        # Regression (j#69343): a negative count is nonsensical/malformed and
        # must not be trusted as known (a negative also slips past `> 0`, so it
        # would otherwise let delete pass).
        state = VersionState.from_mapping(
            {
                "id": "999",
                "status": "open",
                "issues_count": -1,
                "open_issues_count": 0,
                "closed_issues_count": 0,
            }
        )
        self.assertFalse(state.counts_known)
        decision = decide_version_operation(
            VersionOperationRequest(
                operation="delete", state=state, confirmation="delete:999"
            )
        )
        self.assertFalse(decision.allowed)
        self.assertIn("counts_required", decision.blocked_reasons)
        self.assertIsNone(decision.rest_step)

    def test_from_mapping_with_negative_string_count_is_counts_unknown(self) -> None:
        state = VersionState.from_mapping(
            {
                "id": "999",
                "status": "open",
                "issues_count": 0,
                "open_issues_count": "-3",
                "closed_issues_count": 0,
            }
        )
        self.assertFalse(state.counts_known)

    def test_from_mapping_with_string_numeric_counts_is_known(self) -> None:
        # Real list_versions returns numeric strings sometimes; these parse fine.
        state = VersionState.from_mapping(
            {
                "id": "999",
                "status": "open",
                "issues_count": "0",
                "open_issues_count": "0",
                "closed_issues_count": "0",
            }
        )
        self.assertTrue(state.counts_known)

    def test_from_mapping_without_id_fails_closed(self) -> None:
        with self.assertRaises(VersionOperationError):
            VersionState.from_mapping({"name": "no id"})


class WritePortTest(unittest.TestCase):
    def test_writes_port_is_a_runtime_checkable_protocol(self) -> None:
        # The seam exists but ships without a live implementation; a structural
        # stub satisfies it, proving the port shape a future adapter must meet.
        class _Stub:
            def rename(self, version_id, new_name):
                ...

            def close(self, version_id):
                ...

            def lock(self, version_id):
                ...

            def delete(self, version_id):
                ...

        self.assertIsInstance(_Stub(), RedmineVersionWrites)
        self.assertNotIsInstance(object(), RedmineVersionWrites)


if __name__ == "__main__":
    unittest.main()
