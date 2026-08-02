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

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ci_source import (  # noqa: E501
    CI_STATE_SUCCESS,
    CiVerdict,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_action_registry import (  # noqa: E501
    DurableIntegrationAction,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition import (  # noqa: E501
    AutoIntegrationCompositionError,
    LaneBinding,
    build_auto_integration_use_case,
    declared_integration_branches,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_live_authority import (  # noqa: E501
    LaneCallbackScope,
    LiveDurableAuthorityReader,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_ledger import (  # noqa: E501
    AutoIntegrationLedgerError,
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
        authorizing_action_fn=lambda record, proof_head: "",
        source_branch="issue_14825",
        ci_verdict_fn=lambda head, **scope: CiVerdict(
            CI_STATE_SUCCESS,
            "fixed",
            run=scope.get("attested_run") or "target-run",
            workflow=scope.get("workflow") or "",
            commit=head,
            branch=scope.get("branch") or "",
            conclusion="success",
        ),
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
        review_generation=REQ,
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
        self.assertEqual(authority.review_generation, REQ)
        self.assertEqual(authority.reviewed_head, SOURCE)
        self.assertTrue(authority.target_identity_known)
        self.assertTrue(authority.callbacks_drained)
        self.assertIsNotNone(authority.source_ci)
        self.assertEqual(authority.source_ci.run, "299")

    def test_a_caller_selected_review_generation_is_not_admissible(self) -> None:
        authority = _reader(_approved_journals()).read_integration_authority(
            record=_action(review_generation="forged-review-generation")
        )
        self.assertFalse(authority.review_generation_admissible)
        self.assertEqual(authority.review_generation, REQ)
        self.assertEqual(authority.reviewed_head, SOURCE)

    def test_fast_forward_integration_ci_uses_the_target_branch_provider_run(self) -> None:
        journals = _approved_journals() + [
            _journal("96530", _ci_note(head=SOURCE), ISSUER_COORDINATOR)
        ]
        calls = []

        def verdict(head, **scope):
            calls.append((head, scope))
            return CiVerdict(
                CI_STATE_SUCCESS,
                "fixed",
                run="target-run-300",
                workflow=scope["workflow"],
                commit=head,
                branch=scope["branch"],
                conclusion="success",
            )

        evidence = _reader(journals, ci_verdict_fn=verdict).read_integration_ci(
            record=_action(), integration_head=SOURCE
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.run, "target-run-300")
        self.assertEqual(calls[0][1]["branch"], TARGET_REF)
        self.assertEqual(calls[0][1]["attested_run"], "")

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
                admission_record=_action(),
            )
        self.assertIn("unconfigured target", str(raised.exception))

    def test_a_forged_review_generation_refuses_before_the_ledger_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch(
                "mozyo_bridge.e_110_execution_platform."
                "f_140_delegated_coordinator_nested_handoff.application."
                "auto_integration_composition.live_journal_reader",
                return_value=lambda issue: _approved_journals(),
            ):
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
                        config=AutoIntegrationConfig(
                            mode="auto", integration_branch=TARGET_REF
                        ),
                        repo_root=Path("."),
                        admission_record=_action(
                            review_generation="forged-review-generation"
                        ),
                        home=home,
                    )
            self.assertIn("review generation", str(raised.exception))
            self.assertFalse((home / "auto_integration_ledger.sqlite3").exists())

    def test_a_valid_composition_cannot_register_a_second_forged_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch(
                "mozyo_bridge.e_110_execution_platform."
                "f_140_delegated_coordinator_nested_handoff.application."
                "auto_integration_composition.live_journal_reader",
                return_value=lambda issue: _approved_journals(),
            ):
                use_case = build_auto_integration_use_case(
                    binding=LaneBinding(
                        issue=ISSUE,
                        workspace=WS,
                        lane=LANE,
                        lane_generation=GEN,
                        branch="issue_14825",
                        worktree="/tmp/wt",
                    ),
                    config=AutoIntegrationConfig(
                        mode="auto", integration_branch=TARGET_REF
                    ),
                    repo_root=Path("."),
                    inventory_ops=object(),
                    callback_outbox=object(),
                    admission_record=_action(),
                    home=home,
                )
            admitted = _action()
            use_case.register_durable_action(
                DurableIntegrationAction(
                    action_key=admitted.action_key,
                    issue=ISSUE,
                    workspace=WS,
                    lane=LANE,
                    lane_generation=GEN,
                    branch="issue_14825",
                    worktree="/tmp/wt",
                    repo_root=".",
                    source_head=SOURCE,
                    target_ref=TARGET_REF,
                    expected_target_head=OTHER,
                    review_generation=admitted.review_generation,
                )
            )
            self.assertIsNotNone(use_case.ledger.action(admitted.action_key))
            forged = _action(review_generation="forged-review-generation")
            frame = DurableIntegrationAction(
                action_key=forged.action_key,
                issue=ISSUE,
                workspace=WS,
                lane=LANE,
                lane_generation=GEN,
                branch="issue_14825",
                worktree="/tmp/wt",
                repo_root=".",
                source_head=SOURCE,
                target_ref=TARGET_REF,
                expected_target_head=OTHER,
                review_generation=forged.review_generation,
            )
            with self.assertRaises(AutoIntegrationLedgerError):
                use_case.register_durable_action(frame)
            self.assertIsNone(use_case.ledger.action(forged.action_key))
            with self.assertRaises(AutoIntegrationLedgerError):
                use_case.run_integration(forged)
            with self.assertRaises(AutoIntegrationLedgerError):
                use_case.mark_action_awaiting_ci(
                    action_key=forged.action_key,
                    landed_head=SOURCE,
                    ci_workflow="Test",
                )

    def test_production_composition_reads_the_exact_live_lane_callback_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = object()
            scope = LaneCallbackScope(WS, ISSUE, LANE, GEN, 7)
            with mock.patch(
                "mozyo_bridge.e_110_execution_platform."
                "f_140_delegated_coordinator_nested_handoff.application."
                "auto_integration_composition.live_journal_reader",
                return_value=lambda issue: _approved_journals(),
            ), mock.patch(
                "mozyo_bridge.e_110_execution_platform."
                "f_140_delegated_coordinator_nested_handoff.application."
                "auto_integration_composition.live_lane_callback_scope",
                return_value=scope,
            ) as live_scope, mock.patch(
                "mozyo_bridge.e_110_execution_platform."
                "f_140_delegated_coordinator_nested_handoff.application."
                "auto_integration_composition.unresolved_lane_callback_debt",
                return_value=0,
            ) as debt:
                use_case = build_auto_integration_use_case(
                    binding=LaneBinding(
                        issue=ISSUE,
                        workspace=WS,
                        lane=LANE,
                        lane_generation=GEN,
                        branch="issue_14825",
                        worktree="/tmp/wt",
                    ),
                    config=AutoIntegrationConfig(
                        mode="auto", integration_branch=TARGET_REF
                    ),
                    repo_root=Path("."),
                    inventory_ops=object(),
                    callback_outbox=outbox,
                    admission_record=_action(),
                    home=Path(tmp),
                )
                self.assertEqual(use_case.authority.callback_debt_fn(), 0)
            live_scope.assert_called_once_with(
                mock.ANY,
                workspace_id=WS,
                issue=ISSUE,
                lane=LANE,
                lane_generation=GEN,
            )
            debt.assert_called_once_with(outbox, scope=scope)


class ActionTimeFreshReadTest(unittest.TestCase):
    """Review j#96611 finding 2: the two reads that were snapshots, and now are not."""

    def test_the_declared_target_is_asked_again_on_every_read(self) -> None:
        # R1 tupled the branches at construction and closed over them, so an actuator built
        # before a config change kept answering from the value it was born with — while the
        # docstring promised the repository's CURRENT declaration.
        answers = iter([(TARGET_REF,), ("some-other-branch",)])
        reader = _reader(
            _approved_journals(), integration_branches_fn=lambda: next(answers)
        )
        first = reader.read_integration_authority(record=_action())
        second = reader.read_integration_authority(record=_action())
        self.assertTrue(first.target_identity_known)
        self.assertFalse(
            second.target_identity_known,
            "the second read must reflect the repository's new declaration, not the first "
            "read's answer",
        )

    def test_an_unreadable_declaration_declares_nothing(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition import (  # noqa: E501
            _declared_branches_now,
        )
        import pathlib

        # A path with no repo-local config at all: the loader's missing-file default carries no
        # branch, so nothing is declared and the gate stays closed.
        self.assertEqual(
            _declared_branches_now(pathlib.Path("/nonexistent-repo-root-14825")), ()
        )

    def test_policy_reads_each_committed_head_and_ignores_the_worktree_file(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.auto_integration_composition import (  # noqa: E501
            _declared_branches_now,
            _policy_now,
            load_committed_repo_local_config,
        )

        initial = """\
version: 2
auto_integration:
  mode: auto
  integration_branch: main
  ff_only: false
"""
        tightened = """\
version: 2
auto_integration:
  mode: disabled
  integration_branch: release
  ff_only: true
"""
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)

            def git(*args: str) -> None:
                subprocess.run(
                    ["git", "-C", str(repo), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init", "-q")
            git("config", "user.name", "mozyo-bridge test")
            git("config", "user.email", "test@example.invalid")
            path = repo / ".mozyo-bridge" / "config.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(initial, encoding="utf-8")
            git("add", ".mozyo-bridge/config.yaml")
            git("commit", "-qm", "initial config")

            # A dirty working-tree edit has no review/integration provenance and is not policy.
            path.write_text(tightened, encoding="utf-8")
            committed = load_committed_repo_local_config(repo)
            self.assertEqual(committed.auto_integration.integration_branch, "main")
            before = _policy_now(repo)
            self.assertEqual(
                (before.mode, before.integration_branch, before.ff_only),
                ("auto", "main", False),
            )
            self.assertEqual(_declared_branches_now(repo), ("main",))

            # Once the exact blob becomes HEAD, a later action-time read observes it.
            git("add", ".mozyo-bridge/config.yaml")
            git("commit", "-qm", "tighten config")
            after = _policy_now(repo)
            self.assertEqual(
                (after.mode, after.integration_branch, after.ff_only),
                ("disabled", "release", True),
            )
            self.assertEqual(_declared_branches_now(repo), ("release",))

    def test_the_issuer_anchor_is_resolved_per_read(self) -> None:
        # The anchor binds a writer to a role. R1 resolved it once when the reader was built.
        import pathlib
        from unittest import mock
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
            auto_integration_composition as composition,
        )

        seen: list[str] = []
        with mock.patch.object(
            composition, "committed_config_policy_pointer", side_effect=lambda root: (
                seen.append(str(root)) or "git:.mozyo-bridge/config.yaml@" + "a" * 40
            )
        ), mock.patch.object(
            composition.LiveRedmineJournalSource,
            "from_environment",
            side_effect=composition.LiveRedmineJournalError("unconfigured"),
        ):
            read = composition.live_journal_reader(repo_root=pathlib.Path("."))
            self.assertEqual(seen, [], "building the reader must resolve nothing yet")
            read("14825")
            read("14825")
        self.assertEqual(len(seen), 2, "each read resolves the anchor again")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
