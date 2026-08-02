"""The live durable authority the #13686 actuator reads (Redmine #14825, items 1 / 5 / 6).

Pins what the fold establishes and — more of the file — what it refuses:

- an approval satisfies the review gate only when the SHARED producer's rules pass (correlated
  to the request it answers, unsuperseded, coordinator-of-the-right-kind) AND the evidence's lane
  envelope is this actuator's own;
- CI is read PER HEAD, so the source-CI gate keeps answering after the integration SHA's run
  lands — the case that would otherwise make the asynchronous continuation unable to finish;
- the reader refuses an action record naming another issue or another generation before it reads
  anything at all;
- an unset ``integration_branch`` is an unconfigured target, not a runtime-resolved one.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition import (  # noqa: E501
    AutoIntegrationCompositionError,
    LaneBinding,
    build_auto_integration_use_case,
    declared_integration_branches,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_authority import (  # noqa: E501
    LiveDurableAuthorityReader,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_authority import (  # noqa: E501
    GAP_CI_DECLARATION_UNREADABLE,
    GAP_CI_HEAD_ABSENT,
    GAP_LANE_SCOPE_MISMATCH,
    CiRecord,
    LaneScope,
    ci_record_for_head,
    fold_durable_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.auto_integration_records import (  # noqa: E501
    IntegrationActionRecord,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_candidate import (  # noqa: E501
    CONJUNCT_REQUIRED_CI_GREEN,
    CONJUNCT_REVIEW_APPROVED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    ISSUER_COORDINATOR,
    ISSUER_LANE_WORKER,
    ISSUER_REVIEW_GATEWAY,
    EvidenceJournal,
    ResolvedIssuer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    LaneEvidenceEnvelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_integration import (  # noqa: E501
    render_integration_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_marker import (  # noqa: E501
    EVIDENCE_REQUIRED_CI_GREEN,
    render_hibernate_evidence,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    render_workflow_event_marker,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_records import (  # noqa: E501
    AutoIntegrationConfig,
)

ISSUE = "14825"
WS = "ws-1"
LANE = "lane-14825"
GEN = 2
SOURCE = "a" * 40
INTEGRATION = "b" * 40
OTHER = "c" * 40
TARGET_REF = "main"
REQ = "96500"


def _env(head=SOURCE, *, lane=LANE, gen=GEN, workspace=WS):
    return LaneEvidenceEnvelope(
        workspace=workspace, lane=lane, lane_generation=gen, head=head
    )


def _issuer(role, *, lane=LANE, gen=GEN, workspace=WS):
    return ResolvedIssuer(
        role=role,
        workspace=workspace,
        lane=lane,
        lane_generation=gen,
        authority_anchor="j#96400",
    )


def _journal(journal_id, notes, role, **over):
    return EvidenceJournal(
        journal_id=journal_id, notes=notes, issuer=_issuer(role, **over)
    )


def _request_note(head=SOURCE):
    return "review request\n" + render_workflow_event_marker(
        "review_request", target_head=head
    )


def _review_note(*, conclusion="approved", head=SOURCE, lane=LANE, gen=GEN, req=REQ):
    return "review\n" + render_workflow_event_marker(
        "review_result",
        target_head=head,
        review_request_journal=req,
        conclusion=conclusion,
        evidence_workspace=WS,
        evidence_lane=lane,
        evidence_lane_generation=gen,
    )


def _ci_note(*, head=SOURCE, run="299", workflow="required-ci", **over):
    return "ci\n" + render_hibernate_evidence(
        EVIDENCE_REQUIRED_CI_GREEN,
        envelope=over.pop("envelope", _env(head)),
        workflow=workflow,
        run=run,
    )


def _integration_note(*, source_head=SOURCE, integration_head=INTEGRATION):
    return "integration\n" + render_integration_evidence(
        envelope=_env(source_head),
        integration_head=integration_head,
        integration_branch=TARGET_REF,
        disposition="merge",
    )


def _scope(**over):
    fields = {"workspace": WS, "lane": LANE, "lane_generation": GEN}
    fields.update(over)
    return LaneScope(**fields)  # type: ignore[arg-type]


def _approved_journals():
    return [
        _journal(REQ, _request_note(), ISSUER_REVIEW_GATEWAY),
        _journal("96510", _review_note(), ISSUER_REVIEW_GATEWAY),
    ]


class ReviewAuthorityTest(unittest.TestCase):
    """The review generation, and the exact head it approved."""

    def test_an_approval_for_this_lane_is_admissible_and_names_its_head(self) -> None:
        facts = fold_durable_authority(_approved_journals(), scope=_scope())
        self.assertTrue(facts.review.admissible)
        self.assertEqual(facts.review.head, SOURCE)
        self.assertEqual(facts.review.journal, "96510")

    def test_an_explicit_non_approval_is_legible_and_not_admissible(self) -> None:
        journals = [
            _journal(REQ, _request_note(), ISSUER_REVIEW_GATEWAY),
            _journal(
                "96510",
                _review_note(conclusion="changes_requested"),
                ISSUER_REVIEW_GATEWAY,
            ),
        ]
        facts = fold_durable_authority(journals, scope=_scope())
        self.assertFalse(facts.review.admissible)
        # The head is still transcribed: the record is readable, it just says no.
        self.assertEqual(facts.review.head, SOURCE)

    def test_another_lanes_approval_is_a_scope_mismatch_not_an_approval(self) -> None:
        # The evidence is well-formed and its writer holds the gateway role over ITS lane. What
        # it is not is about ours, and that is a different refusal from "no record exists".
        journals = [
            _journal(REQ, _request_note(), ISSUER_REVIEW_GATEWAY, lane="other-lane"),
            _journal(
                "96510",
                _review_note(lane="other-lane"),
                ISSUER_REVIEW_GATEWAY,
                lane="other-lane",
            ),
        ]
        facts = fold_durable_authority(journals, scope=_scope())
        self.assertFalse(facts.review.admissible)
        gap = facts.gap(CONJUNCT_REVIEW_APPROVED)
        self.assertIsNotNone(gap)
        self.assertEqual(gap.reason, GAP_LANE_SCOPE_MISMATCH)

    def test_a_superseded_generation_is_not_this_generations_approval(self) -> None:
        journals = [
            _journal(REQ, _request_note(), ISSUER_REVIEW_GATEWAY, gen=GEN - 1),
            _journal(
                "96510", _review_note(gen=GEN - 1), ISSUER_REVIEW_GATEWAY, gen=GEN - 1
            ),
        ]
        facts = fold_durable_authority(journals, scope=_scope())
        self.assertFalse(facts.review.admissible)


class IntegrationRecordTest(unittest.TestCase):
    """The coordinator's statement that the work reached the target."""

    def test_a_merge_disposition_splits_the_source_and_integration_heads(self) -> None:
        journals = _approved_journals() + [
            _journal("96520", _integration_note(), ISSUER_COORDINATOR)
        ]
        facts = fold_durable_authority(journals, scope=_scope())
        self.assertTrue(facts.integration.confirmed)
        self.assertEqual(facts.integration.source_head, SOURCE)
        self.assertEqual(facts.integration.integration_head, INTEGRATION)
        self.assertEqual(facts.integration.integration_branch, TARGET_REF)

    def test_a_worker_written_integration_record_is_not_the_coordinators(self) -> None:
        journals = _approved_journals() + [
            _journal("96520", _integration_note(), ISSUER_LANE_WORKER)
        ]
        facts = fold_durable_authority(journals, scope=_scope())
        self.assertFalse(facts.integration.confirmed)


class CiPerHeadTest(unittest.TestCase):
    """CI is asked about one exact commit, not about "the issue's latest CI verdict"."""

    def test_a_green_record_for_the_head_answers(self) -> None:
        journals = [_journal("96530", _ci_note(head=SOURCE), ISSUER_COORDINATOR)]
        found = ci_record_for_head(journals, head=SOURCE, scope=_scope())
        self.assertIsInstance(found, CiRecord)
        self.assertEqual(found.head, SOURCE)
        self.assertEqual(found.workflow, "required-ci")
        self.assertEqual(found.run, "299")

    def test_the_source_ci_still_answers_after_the_integration_ci_lands(self) -> None:
        # THE continuation case. Under a latest-declaration-wins read the newer integration-SHA
        # record would shadow the source one, the source-CI gate would start failing at exactly
        # the moment the continuation re-enters, and the action could never reach `integrated`.
        journals = [
            _journal("96530", _ci_note(head=SOURCE, run="src-1"), ISSUER_COORDINATOR),
            _journal(
                "96540",
                _ci_note(head=INTEGRATION, run="int-1"),
                ISSUER_COORDINATOR,
            ),
        ]
        source = ci_record_for_head(journals, head=SOURCE, scope=_scope())
        landed = ci_record_for_head(journals, head=INTEGRATION, scope=_scope())
        self.assertIsInstance(source, CiRecord)
        self.assertIsInstance(landed, CiRecord)
        self.assertEqual(source.run, "src-1")
        self.assertEqual(landed.run, "int-1")

    def test_a_head_no_record_names_is_absent_not_green(self) -> None:
        journals = [_journal("96530", _ci_note(head=SOURCE), ISSUER_COORDINATOR)]
        found = ci_record_for_head(journals, head=OTHER, scope=_scope())
        self.assertFalse(isinstance(found, CiRecord))
        self.assertEqual(found.reason, GAP_CI_HEAD_ABSENT)

    def test_two_differing_green_records_for_one_head_conflict(self) -> None:
        journals = [
            _journal("96530", _ci_note(head=SOURCE, run="299"), ISSUER_COORDINATOR),
            _journal("96540", _ci_note(head=SOURCE, run="300"), ISSUER_COORDINATOR),
        ]
        found = ci_record_for_head(journals, head=SOURCE, scope=_scope())
        self.assertFalse(isinstance(found, CiRecord))
        self.assertEqual(found.reason, "evidence_conflict")

    def test_an_unreadable_current_declaration_refuses_rather_than_searching_past_it(
        self,
    ) -> None:
        # A journal that DECLARES the gate but whose marker does not parse. Reading past it to an
        # older readable record would let a stale green outlive a declaration nobody can read.
        unreadable = "ci\n[mozyo:workflow-event:gate=required_ci_green:head=]"
        journals = [
            _journal("96530", _ci_note(head=SOURCE), ISSUER_COORDINATOR),
            _journal("96540", unreadable, ISSUER_COORDINATOR),
        ]
        found = ci_record_for_head(journals, head=SOURCE, scope=_scope())
        self.assertFalse(isinstance(found, CiRecord))
        self.assertEqual(found.reason, GAP_CI_DECLARATION_UNREADABLE)

    def test_a_green_record_about_another_lane_is_a_scope_mismatch(self) -> None:
        journals = [
            _journal(
                "96530",
                _ci_note(envelope=_env(SOURCE, lane="other-lane")),
                ISSUER_COORDINATOR,
            )
        ]
        found = ci_record_for_head(journals, head=SOURCE, scope=_scope())
        self.assertFalse(isinstance(found, CiRecord))
        self.assertEqual(found.reason, GAP_LANE_SCOPE_MISMATCH)


class _Reader(LiveDurableAuthorityReader):
    """Only to give the reader a shorter constructor in these tests."""


def _reader(journals, **over):
    fields = dict(
        scope=_scope(),
        lane_issue=ISSUE,
        journals_fn=lambda issue: journals,
        integration_branches_fn=lambda: (TARGET_REF,),
        callback_debt_fn=lambda: 0,
        issue_closed_fn=lambda issue: True,
        authorizing_action_fn=lambda record: "",
    )
    fields.update(over)
    return LiveDurableAuthorityReader(**fields)  # type: ignore[arg-type]


def _action(**over):
    fields = dict(
        issue=ISSUE,
        lane_generation=GEN,
        source_head=SOURCE,
        target_ref=TARGET_REF,
        expected_target_head=OTHER,
        review_generation="r1",
    )
    fields.update(over)
    return IntegrationActionRecord(**fields)  # type: ignore[arg-type]


class ReaderIdentityFenceTest(unittest.TestCase):
    """A record for another action establishes nothing, and is refused before any read."""

    def test_a_matching_record_reads_the_durable_gates(self) -> None:
        journals = _approved_journals() + [
            _journal("96530", _ci_note(head=SOURCE), ISSUER_COORDINATOR)
        ]
        authority = _reader(journals).read_integration_authority(record=_action())
        self.assertTrue(authority.review_generation_admissible)
        self.assertEqual(authority.reviewed_head, SOURCE)
        self.assertTrue(authority.target_identity_known)
        self.assertTrue(authority.callbacks_drained)
        self.assertIsNotNone(authority.source_ci)
        self.assertEqual(authority.source_ci.run, "299")

    def test_another_issues_record_never_reaches_the_journals(self) -> None:
        reads: list = []

        def journals_fn(issue):
            reads.append(issue)
            return _approved_journals()

        authority = _reader(None, journals_fn=journals_fn).read_integration_authority(
            record=_action(issue="99999")
        )
        self.assertFalse(authority.review_generation_admissible)
        self.assertEqual(reads, [])

    def test_another_generations_record_is_refused(self) -> None:
        authority = _reader(_approved_journals()).read_integration_authority(
            record=_action(lane_generation=GEN + 1)
        )
        self.assertFalse(authority.review_generation_admissible)

    def test_an_unreadable_source_leaves_every_gate_closed(self) -> None:
        authority = _reader(None).read_integration_authority(record=_action())
        self.assertFalse(authority.review_generation_admissible)
        self.assertFalse(authority.target_identity_known)
        self.assertFalse(authority.callbacks_drained)
        self.assertIsNone(authority.source_ci)

    def test_an_undeclared_target_ref_is_not_a_known_integration_branch(self) -> None:
        authority = _reader(_approved_journals()).read_integration_authority(
            record=_action(target_ref="some-other-branch")
        )
        self.assertFalse(authority.target_identity_known)

    def test_an_unreadable_callback_outbox_is_not_a_drained_one(self) -> None:
        authority = _reader(
            _approved_journals(), callback_debt_fn=lambda: None
        ).read_integration_authority(record=_action())
        self.assertFalse(authority.callbacks_drained)

    def test_the_ref_spelling_does_not_change_the_target_identity(self) -> None:
        authority = _reader(
            _approved_journals(), integration_branches_fn=lambda: ("refs/heads/main",)
        ).read_integration_authority(record=_action())
        self.assertTrue(authority.target_identity_known)


class DeclaredIntegrationBranchTest(unittest.TestCase):
    """Item 6: an unset branch is an unconfigured target, not a runtime-resolved one."""

    def test_a_declared_branch_is_the_one_target(self) -> None:
        config = AutoIntegrationConfig(mode="auto", integration_branch="refs/heads/main")
        self.assertEqual(declared_integration_branches(config), ("main",))

    def test_an_unset_branch_declares_nothing(self) -> None:
        self.assertEqual(
            declared_integration_branches(AutoIntegrationConfig(mode="auto")), ()
        )

    def test_composing_an_actuator_without_a_target_is_refused(self) -> None:
        with self.assertRaises(AutoIntegrationCompositionError) as raised:
            build_auto_integration_use_case(
                binding=LaneBinding(
                    issue=ISSUE,
                    workspace=WS,
                    lane=LANE,
                    lane_generation=GEN,
                    branch="issue_14825",
                    worktree="/tmp/wt",
                ),
                config=AutoIntegrationConfig(mode="auto"),
                repo_root=__import__("pathlib").Path("."),
            )
        self.assertIn("unconfigured target", str(raised.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
