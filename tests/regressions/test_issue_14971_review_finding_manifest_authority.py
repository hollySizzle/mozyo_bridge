"""Redmine #14971: canonical finding manifest + append-only legacy authority."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_gate_record import (  # noqa: E501
    emit_gate_record,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    ISSUER_COORDINATOR,
    ISSUER_REVIEW_GATEWAY,
    ResolvedIssuer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    RedmineJournalEntry,
    extract_markers_from_note,
    render_gate_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_finding_legacy_authority import (  # noqa: E501
    AUTHORITY_SOURCE_LEGACY,
    AUTHORITY_SOURCE_MANIFEST,
    GATE_REVIEW_FINDING_LEGACY_RULING,
    REASON_ATTESTATION_CONFLICTING,
    REASON_ATTESTATION_DUPLICATE,
    REASON_ATTESTATION_MALFORMED,
    REASON_ATTESTATION_MISSING,
    REASON_ATTESTATION_STALE,
    REASON_ATTESTATION_UNAUTHORIZED,
    REASON_ATTESTATION_UNKNOWN,
    REASON_MANIFEST_LEGACY_CONFLICT,
    REASON_REVIEW_JOURNAL_UNRESOLVED,
    REASON_RULING_SUPERSESSION_INVALID,
    REASON_RULING_UNAUTHORIZED,
    REASON_RULING_UNKNOWN,
    REVIEW_FINDING_LEGACY_RULING,
    legacy_review_finding_digest,
    legacy_ruling_pointer,
    legacy_ruling_writer_role,
    render_legacy_review_finding_attestation,
    render_legacy_review_finding_ruling,
    resolve_legacy_review_findings,
    resolve_review_finding_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_finding_manifest import (  # noqa: E501
    MANIFEST_INVALID,
    MANIFEST_VALID,
    REASON_APPROVED_WITH_FINDINGS,
    REASON_CHANGES_WITHOUT_FINDINGS,
    REASON_FINDINGS_INPUT_INVALID,
    REASON_MANIFEST_CONTEXT_MISMATCH,
    REASON_MANIFEST_DIGEST_MISMATCH,
    REASON_MANIFEST_DUPLICATE,
    REASON_MANIFEST_MALFORMED,
    REASON_MANIFEST_PROSE_MISMATCH,
    REASON_REVIEW_BODY_RESERVED_CONTROL,
    ReviewFinding,
    ReviewFindingManifestError,
    read_review_finding_manifest,
    render_review_finding_manifest,
    render_review_result_note,
    review_findings_from_payload,
)


ISSUE = "14971"
REVIEW = "93648"
REQ = "93640"
HEAD = "a" * 40
LEGACY_ISSUE = "14577"
LEGACY_REQ = "93628"
LEGACY_HEAD = "735a5f88e7fa66a46f3da9316586f19ebb50bec0"


def entry(journal: str, notes: str, *, issue: str = ISSUE) -> RedmineJournalEntry:
    return RedmineJournalEntry(issue_id=issue, journal_id=journal, notes=notes, author_id="5")


def result_fields(conclusion: str = "changes_requested") -> dict[str, object]:
    return {
        "conclusion": conclusion,
        "target_head": HEAD,
        "review_request_journal": REQ,
    }


def findings(*ids: str) -> tuple[ReviewFinding, ...]:
    return tuple(
        ReviewFinding(identity=identity, summary=f"summary {identity}", details=f"detail {identity}")
        for identity in ids
    )


COORDINATOR = ResolvedIssuer(
    role=ISSUER_COORDINATOR,
    authority_anchor="redmine:#14971:j#99084",
)


class ManifestProducerTest(unittest.TestCase):
    def test_two_findings_render_prose_and_exact_manifest_from_one_input(self):
        notes = render_review_result_note(
            issue=ISSUE,
            body="R2 review: changes requested",
            findings=findings("1", "2"),
            marker_fields=result_fields(),
        )
        facts = read_review_finding_manifest(entry(REVIEW, notes))
        self.assertEqual(facts.state, MANIFEST_VALID)
        self.assertEqual(facts.findings, ("1", "2"))
        self.assertIn("### finding_1 — summary 1", notes)
        self.assertIn("### finding_2 — summary 2", notes)

    def test_approved_review_emits_an_explicit_empty_manifest(self):
        notes = render_review_result_note(
            issue=ISSUE,
            body="approved",
            findings=(),
            marker_fields=result_fields("approved"),
        )
        facts = read_review_finding_manifest(entry(REVIEW, notes))
        self.assertTrue(facts.valid)
        self.assertEqual(facts.findings, ())
        self.assertIn("## Findings\n\n- none", notes)

    def test_conclusion_and_finding_cardinality_are_fail_closed(self):
        with self.assertRaises(ReviewFindingManifestError) as approved:
            render_review_result_note(
                issue=ISSUE,
                body="bad",
                findings=findings("1"),
                marker_fields=result_fields("approved"),
            )
        self.assertEqual(approved.exception.reason, REASON_APPROVED_WITH_FINDINGS)
        with self.assertRaises(ReviewFindingManifestError) as changes:
            render_review_result_note(
                issue=ISSUE,
                body="bad",
                findings=(),
                marker_fields=result_fields(),
            )
        self.assertEqual(changes.exception.reason, REASON_CHANGES_WITHOUT_FINDINGS)

    def test_summary_cannot_inject_a_finding_or_machine_marker(self):
        for body in (
            "### finding_9 — hidden",
            "#### F2 — hidden legacy finding",
            "### R10-F1 — hidden round finding",
            "## Findings",
            "[mozyo:review-finding-manifest:version=1]",
        ):
            with self.subTest(body=body), self.assertRaises(ReviewFindingManifestError) as caught:
                render_review_result_note(
                    issue=ISSUE,
                    body=body,
                    findings=findings("1"),
                    marker_fields=result_fields(),
                )
            self.assertEqual(caught.exception.reason, REASON_REVIEW_BODY_RESERVED_CONTROL)

    def test_json_shape_is_closed_and_identity_is_canonical(self):
        parsed = review_findings_from_payload(
            {"version": 1, "findings": [{"id": "1", "summary": "one"}]}
        )
        self.assertEqual(parsed, (ReviewFinding("1", "one", ""),))
        bad = (
            {"version": 2, "findings": []},
            {"version": 1, "findings": [], "extra": True},
            {"version": 1, "findings": [{"id": "F1", "summary": "one"}]},
            {"version": 1, "findings": [{"id": "1", "summary": "one", "extra": 1}]},
        )
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ReviewFindingManifestError) as caught:
                review_findings_from_payload(payload)
            self.assertEqual(caught.exception.reason, REASON_FINDINGS_INPUT_INVALID)

    def test_manifest_identity_sequence_is_not_a_bare_string(self):
        with self.assertRaises(ReviewFindingManifestError) as caught:
            render_review_finding_manifest(
                issue=ISSUE,
                review_request_journal=REQ,
                target_head=HEAD,
                findings="12",
            )
        self.assertEqual(caught.exception.reason, REASON_FINDINGS_INPUT_INVALID)

    def test_carriage_return_in_prose_is_rejected(self):
        with self.assertRaises(ReviewFindingManifestError) as caught:
            render_review_result_note(
                issue=ISSUE,
                body="summary\rhidden",
                findings=findings("1"),
                marker_fields=result_fields(),
            )
        self.assertEqual(caught.exception.reason, REASON_FINDINGS_INPUT_INVALID)

    def test_marker_fields_cannot_smuggle_a_second_body_or_unknown_field(self):
        for injected in (
            {**result_fields(), "body": "### finding_9 — smuggled"},
            {**result_fields(), "unknown": "value"},
        ):
            with self.subTest(fields=injected), self.assertRaises(
                ReviewFindingManifestError
            ) as caught:
                render_review_result_note(
                    issue=ISSUE,
                    body="summary",
                    findings=findings("1"),
                    marker_fields=injected,
                )
            self.assertEqual(caught.exception.reason, REASON_FINDINGS_INPUT_INVALID)

    def test_application_posts_prose_and_manifest_with_one_transport_call(self):
        class Transport:
            def __init__(self):
                self.posts = []

            def post_issue_note(self, issue_id, notes):
                self.posts.append((issue_id, notes))
                return f"redmine:issue={issue_id}"

        transport = Transport()
        receipt = emit_gate_record(
            ISSUE,
            "review_result",
            body="changes",
            transport=transport,
            marker_fields=result_fields(),
            review_findings=findings("1", "2"),
        )
        self.assertTrue(receipt.recorded)
        self.assertEqual(len(transport.posts), 1)
        facts = read_review_finding_manifest(entry(REVIEW, transport.posts[0][1]))
        self.assertEqual(facts.findings, ("1", "2"))

    def test_application_refuses_pre_contract_review_writer_before_transport(self):
        class Transport:
            posts = []

            def post_issue_note(self, issue_id, notes):
                self.posts.append((issue_id, notes))
                return "never"

        transport = Transport()
        with self.assertRaises(ReviewFindingManifestError):
            emit_gate_record(
                ISSUE,
                "review_result",
                body="changes",
                transport=transport,
                marker_fields=result_fields(),
            )
        self.assertEqual(transport.posts, [])


class ManifestReaderAdversarialTest(unittest.TestCase):
    def setUp(self):
        self.notes = render_review_result_note(
            issue=ISSUE,
            body="changes",
            findings=findings("1"),
            marker_fields=result_fields(),
        )

    def test_two_prose_findings_with_manifest_one_is_rejected(self):
        forged = self.notes.replace(
            "detail 1\n\n[mozyo:workflow-event:",
            "detail 1\n\n### finding_2 — unmanifested\n\n[mozyo:workflow-event:",
        )
        facts = read_review_finding_manifest(entry(REVIEW, forged))
        self.assertEqual((facts.state, facts.reason), (MANIFEST_INVALID, REASON_MANIFEST_PROSE_MISMATCH))

    def test_duplicate_and_malformed_sidecars_are_rejected(self):
        sidecar = self.notes.rsplit("\n\n", 1)[1]
        duplicate = read_review_finding_manifest(entry(REVIEW, self.notes + "\n\n" + sidecar))
        self.assertEqual(duplicate.reason, REASON_MANIFEST_DUPLICATE)
        malformed = read_review_finding_manifest(
            entry(REVIEW, self.notes.replace("version=1:issue=", "version=2:issue="))
        )
        self.assertEqual(malformed.reason, REASON_MANIFEST_MALFORMED)

    def test_digest_and_context_tampering_are_rejected(self):
        tampered = self.notes.replace("set_digest=", "set_digest=f", 1)
        self.assertEqual(
            read_review_finding_manifest(entry(REVIEW, tampered)).reason,
            REASON_MANIFEST_MALFORMED,
        )
        other_sidecar = render_review_finding_manifest(
            issue="14972", review_request_journal=REQ, target_head=HEAD, findings=("1",)
        )
        context = self.notes.rsplit("\n\n", 1)[0] + "\n\n" + other_sidecar
        self.assertEqual(
            read_review_finding_manifest(entry(REVIEW, context)).reason,
            REASON_MANIFEST_CONTEXT_MISMATCH,
        )

    def test_generic_review_result_projection_is_byte_compatible(self):
        old = render_gate_note("review_result", body="changes", **result_fields())
        before = extract_markers_from_note(ISSUE, REVIEW, old)
        after = extract_markers_from_note(ISSUE, REVIEW, self.notes)
        self.assertEqual(before, after)
        self.assertEqual(len(after), 1)  # the dedicated channel is invisible to generic intake

    def test_quoted_sidecar_is_not_authority(self):
        old = render_gate_note("review_result", body="changes", **result_fields())
        quoted = old + "\n\n> " + self.notes.rsplit("\n\n", 1)[1]
        facts = read_review_finding_manifest(entry(REVIEW, quoted))
        self.assertEqual(facts.state, "missing")


class LegacyAuthorityTest(unittest.TestCase):
    def _review(self):
        # #14577 j#93648 fixture: its real marker identity and non-normalized F1/F2 prose shape.
        return entry(
            REVIEW,
            "\n\n".join(
                (
                    "## Gate: review",
                    render_gate_note(
                        "review_result",
                        conclusion="changes_requested",
                        target_head=LEGACY_HEAD,
                        review_request_journal=LEGACY_REQ,
                    ),
                    "### Findings\n\n#### F1 — blocker\nfirst\n\n#### F2 — 要修正\nsecond",
                )
            ),
            issue=LEGACY_ISSUE,
        )

    def _attestation(
        self,
        journal="99001",
        ids=("1", "2"),
        *,
        issue=LEGACY_ISSUE,
    ):
        return entry(
            journal,
            render_legacy_review_finding_attestation(
                issue=issue, review_journal=REVIEW, findings=ids
            ),
            issue=issue,
        )

    def _ruling(
        self,
        journal="99002",
        attestation="99001",
        ids=("1", "2"),
        supersedes="none",
        *,
        issue=LEGACY_ISSUE,
    ):
        return entry(
            journal,
            render_legacy_review_finding_ruling(
                issue=issue,
                review_journal=REVIEW,
                attestation_journal=attestation,
                findings=ids,
                supersedes_ruling_journal=supersedes,
            ),
            issue=issue,
        )

    def test_contract_writer_and_ruling_pointer_are_single_sourced(self):
        self.assertEqual(GATE_REVIEW_FINDING_LEGACY_RULING, "review_finding_legacy_ruling")
        self.assertEqual(legacy_ruling_writer_role(), ISSUER_COORDINATOR)
        self.assertEqual(legacy_ruling_pointer(), REVIEW_FINDING_LEGACY_RULING)
        self.assertEqual(REVIEW_FINDING_LEGACY_RULING, "redmine:#14971:j#99084")

    def test_owner_ruled_attestation_migrates_real_pre_contract_shape(self):
        entries = [self._review(), self._attestation(), self._ruling()]
        facts = resolve_legacy_review_findings(
            entries,
            review_journal=REVIEW,
            ruling_issuers={"99002": COORDINATOR},
        )
        self.assertTrue(facts.valid)
        self.assertEqual(facts.findings, ("1", "2"))
        self.assertEqual((facts.attestation_journal, facts.ruling_journal), ("99001", "99002"))

    def test_attestation_alone_and_unanchored_ruling_are_unauthorized(self):
        alone = resolve_legacy_review_findings(
            [self._review(), self._attestation()], review_journal=REVIEW
        )
        self.assertEqual(alone.reason, REASON_ATTESTATION_UNAUTHORIZED)
        for issuer in (
            ResolvedIssuer(role=ISSUER_COORDINATOR),
            ResolvedIssuer(role=ISSUER_COORDINATOR, authority_anchor="some-anchor"),
            ResolvedIssuer(role=ISSUER_REVIEW_GATEWAY, authority_anchor="some-anchor"),
        ):
            with self.subTest(issuer=issuer):
                facts = resolve_legacy_review_findings(
                    [self._review(), self._attestation(), self._ruling()],
                    review_journal=REVIEW,
                    ruling_issuers={"99002": issuer},
                )
                self.assertEqual(facts.reason, REASON_RULING_UNAUTHORIZED)

    def test_attestation_cannot_upgrade_a_non_review_or_an_approved_review(self):
        for target in (
            entry(REVIEW, "ordinary journal", issue=LEGACY_ISSUE),
            entry(
                REVIEW,
                render_gate_note(
                    "review_result", body="approved", **result_fields("approved")
                ),
                issue=LEGACY_ISSUE,
            ),
        ):
            with self.subTest(notes=target.notes):
                facts = resolve_legacy_review_findings(
                    [target, self._attestation(), self._ruling()],
                    review_journal=REVIEW,
                    ruling_issuers={"99002": COORDINATOR},
                )
                self.assertEqual(facts.reason, REASON_REVIEW_JOURNAL_UNRESOLVED)

    def test_duplicate_and_unruled_conflict_fail_closed(self):
        duplicate = self._attestation("99002")
        facts = resolve_legacy_review_findings(
            [self._review(), self._attestation(), duplicate], review_journal=REVIEW
        )
        self.assertEqual(facts.reason, REASON_ATTESTATION_DUPLICATE)

        conflict = self._attestation("99002", ("1", "2", "3"))
        ruling = self._ruling("99003", "99001", ("1", "2"))
        facts = resolve_legacy_review_findings(
            [self._review(), self._attestation(), conflict, ruling],
            review_journal=REVIEW,
            ruling_issuers={"99003": COORDINATOR},
        )
        self.assertEqual(facts.reason, REASON_ATTESTATION_CONFLICTING)

    def test_missing_unknown_and_malformed_legacy_records_fail_closed(self):
        missing = resolve_legacy_review_findings(
            [self._review()], review_journal=REVIEW
        )
        self.assertEqual(missing.reason, REASON_ATTESTATION_MISSING)

        attestation = self._attestation()
        unknown_attestation = entry(
            attestation.journal_id,
            attestation.notes.replace("version=1", "version=2", 1),
            issue=LEGACY_ISSUE,
        )
        facts = resolve_legacy_review_findings(
            [self._review(), unknown_attestation], review_journal=REVIEW
        )
        self.assertEqual(facts.reason, REASON_ATTESTATION_UNKNOWN)

        malformed_attestation = entry(
            attestation.journal_id,
            attestation.notes.replace("count=2", "count=02", 1),
            issue=LEGACY_ISSUE,
        )
        facts = resolve_legacy_review_findings(
            [self._review(), malformed_attestation], review_journal=REVIEW
        )
        self.assertEqual(facts.reason, REASON_ATTESTATION_MALFORMED)

        ruling = self._ruling()
        unknown_ruling = entry(
            ruling.journal_id,
            ruling.notes.replace("version=1", "version=2", 1),
            issue=LEGACY_ISSUE,
        )
        facts = resolve_legacy_review_findings(
            [self._review(), attestation, unknown_ruling],
            review_journal=REVIEW,
            ruling_issuers={"99002": COORDINATOR},
        )
        self.assertEqual(facts.reason, REASON_RULING_UNKNOWN)

    def test_newer_unruled_attestation_makes_old_ruling_stale(self):
        newer = self._attestation("99003", ("1", "2", "3"))
        facts = resolve_legacy_review_findings(
            [self._review(), self._attestation(), self._ruling(), newer],
            review_journal=REVIEW,
            ruling_issuers={"99002": COORDINATOR},
        )
        self.assertEqual(facts.reason, REASON_ATTESTATION_STALE)

    def test_explicit_replacement_chain_selects_latest_distinct_attestation(self):
        attestation2 = self._attestation("99003", ("1", "2", "3"))
        ruling2 = self._ruling("99004", "99003", ("1", "2", "3"), supersedes="99002")
        entries = [self._review(), self._attestation(), self._ruling(), attestation2, ruling2]
        facts = resolve_legacy_review_findings(
            entries,
            review_journal=REVIEW,
            ruling_issuers={"99002": COORDINATOR, "99004": COORDINATOR},
        )
        self.assertTrue(facts.valid)
        self.assertEqual(facts.findings, ("1", "2", "3"))
        self.assertEqual(facts.ruling_journal, "99004")

        broken = self._ruling("99004", "99003", ("1", "2", "3"), supersedes="none")
        facts = resolve_legacy_review_findings(
            [self._review(), self._attestation(), self._ruling(), attestation2, broken],
            review_journal=REVIEW,
            ruling_issuers={"99002": COORDINATOR, "99004": COORDINATOR},
        )
        self.assertEqual(facts.reason, REASON_RULING_SUPERSESSION_INVALID)

    def test_unified_reader_prefers_manifest_and_refuses_downgrade_markers(self):
        notes = render_review_result_note(
            issue=ISSUE,
            body="changes",
            findings=findings("1"),
            marker_fields=result_fields(),
        )
        manifest_entry = entry(REVIEW, notes)
        good = resolve_review_finding_authority([manifest_entry], review_journal=REVIEW)
        self.assertTrue(good.valid)
        self.assertEqual(good.source, AUTHORITY_SOURCE_MANIFEST)

        conflict = resolve_review_finding_authority(
            [
                manifest_entry,
                self._attestation(issue=ISSUE),
                self._ruling(issue=ISSUE),
            ],
            review_journal=REVIEW,
            ruling_issuers={"99002": COORDINATOR},
        )
        self.assertEqual(conflict.reason, REASON_MANIFEST_LEGACY_CONFLICT)

        malformed = entry(
            "99003",
            f"[mozyo:review-finding-attestation:version=1:issue={ISSUE}:review={REVIEW}",
        )
        conflict = resolve_review_finding_authority(
            [manifest_entry, malformed], review_journal=REVIEW
        )
        self.assertEqual(conflict.reason, REASON_MANIFEST_LEGACY_CONFLICT)

    def test_unified_legacy_result_names_ruling_as_authority(self):
        facts = resolve_review_finding_authority(
            [self._review(), self._attestation(), self._ruling()],
            review_journal=REVIEW,
            ruling_issuers={"99002": COORDINATOR},
        )
        self.assertTrue(facts.valid)
        self.assertEqual(facts.source, AUTHORITY_SOURCE_LEGACY)
        self.assertEqual(facts.authority_journal, "99002")

    def test_digest_binds_issue_review_and_exact_sequence(self):
        base = legacy_review_finding_digest(
            issue=LEGACY_ISSUE, review_journal=REVIEW, findings=("1", "2")
        )
        self.assertNotEqual(
            base,
            legacy_review_finding_digest(
                issue=LEGACY_ISSUE, review_journal=REVIEW, findings=("2", "1")
            ),
        )


if __name__ == "__main__":
    unittest.main()
