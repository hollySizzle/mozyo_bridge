"""Regression: recovery journal pointers are locators, never approvals by themselves."""

from __future__ import annotations

import unittest
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.recovery_owner_approval_live import (  # noqa: E501
    verify_live_recovery_owner_approval,
)

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    GATE_GATEWAY_RECOVERY_OWNER_APPROVAL,
    GATE_STALE_WORKER_RECOVERY_OWNER_APPROVAL,
    ISSUER_COORDINATOR,
    ISSUER_LANE_WORKER,
    RECOVERY_OWNER_APPROVAL_RULING,
    ResolvedIssuer,
    contract_ruling_pointer,
    contract_writer_role,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.recovery_owner_approval import (  # noqa: E501
    GATEWAY_RECOVERY_APPROVAL_EFFECT,
    GATEWAY_RECOVERY_APPROVAL_GATE,
    STALE_WORKER_RECOVERY_APPROVAL_EFFECT,
    STALE_WORKER_RECOVERY_APPROVAL_GATE,
    RecoveryOwnerApprovalError,
    render_recovery_owner_approval_marker,
    verify_recovery_owner_approval,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.worker_refresh_approval import (  # noqa: E501
    parse_strict_approval_markers,
    render_worker_refresh_approval_marker,
)


ISSUE = "14663"
JOURNAL = "99195"
LANE = "issue_14663_recovery"


def _gateway_operation(**overrides: object) -> dict[str, object]:
    operation: dict[str, object] = {
        "action_id": "refresh-gateway:lane:codex:codex:name:locator:r7",
        "action_generation": 3,
        "role": "codex",
        "provider": "codex",
        "assigned_name": "mzb1_gateway",
        "locator": "workspace:pane",
        "participant_revision": "7",
        "lane_revision": "11",
        "lane_generation": "5",
        "anchor_issue": ISSUE,
        "resume_anchor_journal": "99190",
        "resume_gate": "review_request",
    }
    operation.update(overrides)
    return operation


def _approval(**overrides: object) -> dict[str, object]:
    approval: dict[str, object] = {
        "gate": GATEWAY_RECOVERY_APPROVAL_GATE,
        "effect": GATEWAY_RECOVERY_APPROVAL_EFFECT,
        "issue": ISSUE,
        "lane": LANE,
        "operation": _gateway_operation(),
    }
    approval.update(overrides)
    return approval


def _issuer(role: str = ISSUER_COORDINATOR, *, anchored: bool = True) -> ResolvedIssuer:
    return ResolvedIssuer(
        role=role,
        authority_anchor=("policy+evidence" if anchored else ""),
    )


class RecoveryApprovalContractTests(unittest.TestCase):
    def test_both_gates_have_the_gate_specific_durable_ruling(self):
        for gate in (
            GATE_GATEWAY_RECOVERY_OWNER_APPROVAL,
            GATE_STALE_WORKER_RECOVERY_OWNER_APPROVAL,
        ):
            self.assertEqual(contract_writer_role(gate), ISSUER_COORDINATOR)
            self.assertEqual(contract_ruling_pointer(gate), RECOVERY_OWNER_APPROVAL_RULING)

    def test_one_canonical_marker_verifies(self):
        marker = render_recovery_owner_approval_marker(**_approval())
        entry = RedmineJournalEntry(ISSUE, JOURNAL, marker)
        fields = verify_recovery_owner_approval(
            [entry], journal=JOURNAL, anchor_issue=ISSUE,
            issuer=_issuer(), **_approval()
        )
        self.assertEqual(fields["decision"], "approved")
        self.assertEqual(fields["approval_source"], "direct_owner")

    def test_pointer_shape_without_marker_is_not_approval(self):
        entry = RedmineJournalEntry(ISSUE, JOURNAL, "owner approval mentioned in prose")
        with self.assertRaises(RecoveryOwnerApprovalError):
            verify_recovery_owner_approval(
                [entry], journal=JOURNAL, anchor_issue=ISSUE,
                issuer=_issuer(), **_approval()
            )

    def test_cross_issue_journal_is_not_owned_by_the_anchor_issue(self):
        marker = render_recovery_owner_approval_marker(**_approval())
        entry = RedmineJournalEntry("14244", JOURNAL, marker)
        with self.assertRaises(RecoveryOwnerApprovalError):
            verify_recovery_owner_approval(
                [entry], journal=JOURNAL, anchor_issue=ISSUE,
                issuer=_issuer(), **_approval()
            )

    def test_wrong_generation_is_another_operation(self):
        marker = render_recovery_owner_approval_marker(**_approval())
        changed = _approval(operation=_gateway_operation(action_generation=4))
        with self.assertRaises(RecoveryOwnerApprovalError):
            verify_recovery_owner_approval(
                [RedmineJournalEntry(ISSUE, JOURNAL, marker)],
                journal=JOURNAL, anchor_issue=ISSUE, issuer=_issuer(), **changed
            )

    def test_another_recovery_gate_cannot_authorize_gateway_close(self):
        stale = _approval(
            gate=STALE_WORKER_RECOVERY_APPROVAL_GATE,
            effect=STALE_WORKER_RECOVERY_APPROVAL_EFFECT,
        )
        marker = render_recovery_owner_approval_marker(**stale)
        with self.assertRaises(RecoveryOwnerApprovalError):
            verify_recovery_owner_approval(
                [RedmineJournalEntry(ISSUE, JOURNAL, marker)],
                journal=JOURNAL, anchor_issue=ISSUE,
                issuer=_issuer(), **_approval()
            )

    def test_quoted_marker_and_duplicate_decision_both_fail_closed(self):
        marker = render_recovery_owner_approval_marker(**_approval())
        for notes in (
            f"```text\n{marker}\n```",
            marker.replace(
                ":decision=approved:",
                ":decision=declined:decision=approved:",
            ),
        ):
            with self.subTest(notes=notes[:30]):
                with self.assertRaises(RecoveryOwnerApprovalError):
                    verify_recovery_owner_approval(
                        [RedmineJournalEntry(ISSUE, JOURNAL, notes)],
                        journal=JOURNAL, anchor_issue=ISSUE,
                        issuer=_issuer(), **_approval()
                    )

    def test_unanchored_or_noncoordinator_writer_is_not_authority(self):
        marker = render_recovery_owner_approval_marker(**_approval())
        entry = RedmineJournalEntry(ISSUE, JOURNAL, marker)
        for issuer in (_issuer(anchored=False), _issuer(ISSUER_LANE_WORKER)):
            with self.subTest(issuer=issuer):
                with self.assertRaises(RecoveryOwnerApprovalError):
                    verify_recovery_owner_approval(
                        [entry], journal=JOURNAL, anchor_issue=ISSUE,
                        issuer=issuer, **_approval()
                    )

    def test_worker_refresh_marker_bytes_still_use_the_shared_strict_scan(self):
        operation = {
            "issue": "14661",
            "lane": "issue_14661_worker",
            "action_id": "refresh-worker:lane:claude:claude:name:locator:r1",
            "action_generation": 1,
            "lane_revision": "2",
            "lane_generation": "3",
            "anchor_issue": "14661",
            "resume_anchor_journal": "92443",
            "resume_gate": "implementation_request",
        }
        marker = render_worker_refresh_approval_marker(**operation)
        self.assertEqual(len(parse_strict_approval_markers(marker)), 1)

    def test_real_live_adapter_resolves_the_gate_specific_ruling(self):
        marker = render_recovery_owner_approval_marker(**_approval())
        entry = RedmineJournalEntry(ISSUE, JOURNAL, marker)
        verified = verify_live_recovery_owner_approval(
            repo_root=Path(__file__).resolve().parents[4],
            journal_reader=lambda issue: [entry],
            journal_reader_fresh=True,
            journal=JOURNAL,
            anchor_issue=ISSUE,
            **_approval(),
        )
        self.assertTrue(verified)

    def test_nonfresh_live_reader_never_authorizes(self):
        marker = render_recovery_owner_approval_marker(**_approval())
        entry = RedmineJournalEntry(ISSUE, JOURNAL, marker)
        verified = verify_live_recovery_owner_approval(
            repo_root=Path(__file__).resolve().parents[4],
            journal_reader=lambda issue: [entry],
            journal_reader_fresh=False,
            journal=JOURNAL,
            anchor_issue=ISSUE,
            **_approval(),
        )
        self.assertFalse(verified)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
