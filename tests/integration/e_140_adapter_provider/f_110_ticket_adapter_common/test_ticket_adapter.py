"""Ticket adapter normalized record seam tests (Redmine #12034).

Covers the first concrete cut of the built-in ticket adapter boundary
(Redmine #12001 design doc): the built-in Redmine provider normalizing
Redmine API JSON / handoff anchors into the core-facing records, the
core-owned workflow-gate vocabulary, the core-owned owner-approval decision,
and the proof that routing the cockpit Redmine read model through the seam
leaves the existing minimized payload behavior unchanged. No network is
touched anywhere here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import RedmineAnchor
from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.ticket_adapter import (
    WORKFLOW_GATE_KINDS,
    CommentRef,
    IssueRef,
    JournalRef,
    OwnerApproval,
    TicketProvider,
    TicketRecordError,
    WorkflowGate,
    classify_workflow_gate,
    owner_approval,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_ticket_provider import (
    REDMINE_TICKET_PROVIDER,
    RedmineTicketProvider,
)
from mozyo_bridge.redmine_context import _latest_issue_payload


# A representative Redmine issue object as returned by /issues.json, with the
# nested status object and a confidential-looking subject the seam must drop.
REDMINE_ISSUE = {
    "id": 11999,
    "subject": "CONFIDENTIAL-SUMMARY",
    "status": {"id": 2, "name": "着手中"},
    "updated_on": "2026-06-12T00:00:00Z",
}

# A representative journals array: a field-change-only entry (no notes) and a
# commented entry.
REDMINE_JOURNALS = [
    {"id": 59143, "created_on": "2026-06-15T16:42:12Z", "notes": ""},
    {
        "id": 59144,
        "created_on": "2026-06-15T16:42:52Z",
        "notes": "## Handoff Preparation",
    },
]


class RedmineProviderNormalizeTest(unittest.TestCase):
    def test_provider_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(REDMINE_TICKET_PROVIDER, TicketProvider)
        self.assertEqual("redmine", REDMINE_TICKET_PROVIDER.name)

    def test_normalize_issue_drops_subject_and_flattens_status(self) -> None:
        ref = REDMINE_TICKET_PROVIDER.normalize_issue(REDMINE_ISSUE)
        self.assertEqual(
            IssueRef(
                provider="redmine",
                id="11999",
                status="着手中",
                updated_on="2026-06-12T00:00:00Z",
            ),
            ref,
        )
        # Surface minimization: the confidential subject never lands anywhere
        # on the normalized record.
        self.assertNotIn("CONFIDENTIAL-SUMMARY", repr(ref))

    def test_normalize_issue_is_lenient_on_partial_objects(self) -> None:
        ref = REDMINE_TICKET_PROVIDER.normalize_issue({"id": 5})
        self.assertEqual("5", ref.id)
        self.assertIsNone(ref.status)
        self.assertIsNone(ref.updated_on)
        # A wholly empty object yields an empty id rather than raising.
        self.assertEqual("", REDMINE_TICKET_PROVIDER.normalize_issue({}).id)

    def test_normalize_journals(self) -> None:
        journals = REDMINE_TICKET_PROVIDER.normalize_journals(
            "11999", REDMINE_JOURNALS
        )
        self.assertEqual(
            [
                JournalRef(
                    provider="redmine",
                    issue_id="11999",
                    id="59143",
                    created_on="2026-06-15T16:42:12Z",
                ),
                JournalRef(
                    provider="redmine",
                    issue_id="11999",
                    id="59144",
                    created_on="2026-06-15T16:42:52Z",
                ),
            ],
            journals,
        )

    def test_normalize_comments_only_returns_entries_with_notes(self) -> None:
        comments = REDMINE_TICKET_PROVIDER.normalize_comments(
            "11999", REDMINE_JOURNALS
        )
        self.assertEqual(
            [
                CommentRef(
                    provider="redmine",
                    issue_id="11999",
                    notes="## Handoff Preparation",
                    journal_id="59144",
                )
            ],
            comments,
        )

    def test_refs_from_anchor_bridges_handoff_to_records(self) -> None:
        anchor = RedmineAnchor(issue="12034", journal="59143")
        issue_ref, journal_ref = REDMINE_TICKET_PROVIDER.refs_from_anchor(anchor)
        self.assertEqual(IssueRef(provider="redmine", id="12034"), issue_ref)
        self.assertEqual(
            JournalRef(provider="redmine", issue_id="12034", id="59143"),
            journal_ref,
        )

    def test_issue_url_formatting_is_provider_owned(self) -> None:
        self.assertEqual(
            "https://redmine.example.test/issues/12034",
            RedmineTicketProvider.issue_url(
                "https://redmine.example.test/", 12034
            ),
        )


class WorkflowGateVocabularyTest(unittest.TestCase):
    def test_gate_vocabulary_is_the_durable_subset(self) -> None:
        self.assertEqual(
            {"implementation_done", "review_request", "review_result"},
            set(WORKFLOW_GATE_KINDS),
        )

    def test_classify_recognized_gate(self) -> None:
        journal = JournalRef(provider="redmine", issue_id="12034", id="59143")
        gate = classify_workflow_gate(
            "review_request", "12034", journal_ref=journal
        )
        self.assertEqual(
            WorkflowGate(
                name="review_request", issue_id="12034", journal_ref=journal
            ),
            gate,
        )

    def test_non_gate_kinds_do_not_classify(self) -> None:
        for kind in ("implementation_request", "design_consultation", "reply", "custom", "bogus"):
            self.assertIsNone(
                classify_workflow_gate(kind, "12034"),
                msg=f"{kind} must not be a workflow gate",
            )

    def test_direct_construction_rejects_unrecognized_name(self) -> None:
        # The gate vocabulary is core-owned: even direct construction (not via
        # classify_workflow_gate) cannot smuggle in an unknown gate name, so a
        # provider can never widen the vocabulary.
        with self.assertRaises(TicketRecordError):
            WorkflowGate(name="bogus", issue_id="12034")
        with self.assertRaises(TicketRecordError):
            # A non-gate handoff kind is still rejected at the type boundary.
            WorkflowGate(name="implementation_request", issue_id="12034")

    def test_direct_construction_accepts_recognized_name(self) -> None:
        for name in WORKFLOW_GATE_KINDS:
            gate = WorkflowGate(name=name, issue_id="12034")
            self.assertEqual(name, gate.name)


class OwnerApprovalDecisionTest(unittest.TestCase):
    def test_owner_approval_is_a_core_decision(self) -> None:
        approval = owner_approval(
            "12034", approved=True, approver="owner"
        )
        self.assertEqual(
            OwnerApproval(issue_id="12034", approved=True, approver="owner"),
            approval,
        )

    def test_provider_does_not_decide_approval(self) -> None:
        # The boundary: a provider may expose close mechanics, but it must not
        # own the approval decision. The built-in provider therefore offers no
        # approval/gate API at all.
        self.assertFalse(hasattr(REDMINE_TICKET_PROVIDER, "owner_approval"))
        self.assertFalse(hasattr(REDMINE_TICKET_PROVIDER, "approve"))
        self.assertFalse(hasattr(REDMINE_TICKET_PROVIDER, "classify_workflow_gate"))


class CockpitPayloadProjectionTest(unittest.TestCase):
    """The seam drives the existing cockpit payload without changing it."""

    def test_numeric_id_projects_back_to_int(self) -> None:
        payload = _latest_issue_payload(
            REDMINE_TICKET_PROVIDER.normalize_issue(REDMINE_ISSUE)
        )
        self.assertEqual(
            {
                "id": 11999,
                "status": "着手中",
                "updated_on": "2026-06-12T00:00:00Z",
            },
            payload,
        )
        # The minimized contract carries no subject.
        self.assertNotIn("subject", payload)

    def test_missing_id_projects_to_none(self) -> None:
        payload = _latest_issue_payload(IssueRef(provider="redmine", id=""))
        self.assertIsNone(payload["id"])


if __name__ == "__main__":
    unittest.main()
