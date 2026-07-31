"""The hibernate-evidence producers' PUBLIC contract: what a clean call renders, and reads back.

These are contract and change-safety claims, not the recurrence pin for one defect — which is why
they live here and not beside the #14694 regression file (``tests-placement-discovery-policy.md``
``### regressions`` R3-b: a regressions file's tests must ALL be that symptom's recurrence
detection, and a file mixing the two kinds of claim falls to this bucket instead).

What is pinned here:

- the exact marker each kind renders from a clean call, byte for byte;
- which fields each kind's marker carries, read off the rendered marker rather than off the
  producer's own table;
- the round trip: what the producer writes, the STRICT reader every authority consumer shares can
  read, and it parses back to the values the caller passed;
- that tokens which merely look unusual — ``a=b``, ``%2F``, non-ASCII — still render, so a
  tightening of the grammar cannot quietly become a narrowing of what a legitimate id may be;
- that the PARSE side is unchanged: the producer-side work of #14694 deliberately did not touch
  the reader's acceptance, its typed reasons, or its duplicate / conflict resolution.
"""

from __future__ import annotations

import dataclasses
import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
    hibernate_evidence_integration as ie,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
    hibernate_evidence_marker as ev,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    LaneEvidenceEnvelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    strict_marker_fields_in_note,
)
from tests.support.hibernate_evidence_producer_corpus import (
    CARRIED_BY_KIND,
    HEAD,
    INTEGRATION_HEAD,
    LANE,
    PRODUCER_FIELDS,
    VALID_TOKENS,
    WS,
    assert_population_is_closed,
    clean_kwargs,
    envelope,
    envelope_for,
)


class ProducerSurfaceShapeTests(unittest.TestCase):
    def test_the_producer_population_is_closed(self):
        assert_population_is_closed(self)

    def test_the_envelope_population_is_the_documented_four(self):
        self.assertEqual(
            tuple(f.name for f in dataclasses.fields(LaneEvidenceEnvelope)),
            ("workspace", "lane", "lane_generation", "head"),
        )

    def test_each_kind_carries_exactly_the_fields_its_contract_names(self):
        # Read off the rendered marker, not off the producer's table: the table is the thing under
        # test, so believing it would make this assertion circular.
        assert_population_is_closed(self)
        for kind, carried in sorted(CARRIED_BY_KIND.items()):
            with self.subTest(kind=kind):
                marker = ev.render_hibernate_evidence(
                    kind, envelope=envelope_for(kind), **clean_kwargs(kind)
                )
                for field in PRODUCER_FIELDS:
                    self.assertEqual(
                        f"{field}=" in marker, field in carried, f"{field} in {marker}"
                    )


class CleanOutputTests(unittest.TestCase):
    """Byte pins. A producer-side tightening may only remove markers that should not exist."""

    def test_ci_green_marker(self):
        self.assertEqual(
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN,
                envelope=envelope(),
                workflow="test.yml",
                run="29860030313",
            ),
            "[mozyo:workflow-event:gate=required_ci_green:"
            f"workspace={WS}:lane={LANE}:lane_generation=3:head={HEAD}:"
            "workflow=test.yml:run=29860030313:conclusion=success]",
        )

    def test_an_explicitly_supplied_success_conclusion_changes_nothing(self):
        self.assertEqual(
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN,
                envelope=envelope(),
                workflow="test.yml",
                run="1",
                conclusion="success",
            ),
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=envelope(), workflow="test.yml", run="1"
            ),
        )

    def test_dogfood_marker(self):
        self.assertEqual(
            ev.render_hibernate_evidence(
                ev.EVIDENCE_DOGFOOD_DELEGATED,
                envelope=envelope(),
                release_issue="14184",
                acceptance="85431",
            ),
            "[mozyo:workflow-event:gate=dogfood_delegated:"
            f"workspace={WS}:lane={LANE}:lane_generation=3:head={HEAD}:"
            "release_issue=14184:acceptance=85431]",
        )

    def test_park_marker(self):
        self.assertEqual(
            ev.render_hibernate_evidence(ev.EVIDENCE_PARK_DECLARED, envelope=envelope(head="")),
            f"[mozyo:workflow-event:gate=park_declared:workspace={WS}:lane={LANE}:"
            "lane_generation=3]",
        )

    def test_integration_marker(self):
        self.assertEqual(
            ie.render_integration_evidence(
                envelope=envelope(),
                integration_head=INTEGRATION_HEAD,
                integration_branch="main-next",
                disposition="merge",
            ),
            "[mozyo:workflow-event:gate=integration_disposition:"
            f"workspace={WS}:lane={LANE}:lane_generation=3:head={HEAD}:"
            f"integration_head={INTEGRATION_HEAD}:integration_branch=main-next:"
            "disposition=merge]",
        )


class RoundTripTests(unittest.TestCase):
    """A rendered marker is readable by the strict reader and means what the caller said."""

    def _parse(self, marker: str, *, kind: str) -> ev.HibernateEvidence:
        read = strict_marker_fields_in_note(marker)
        self.assertIsNotNone(read, f"the strict reader refused its own producer's {marker!r}")
        (_, fields), = read
        parsed = ev.parse_hibernate_evidence(fields, kind=kind)
        self.assertIsInstance(parsed, ev.HibernateEvidence, f"unreadable evidence {marker!r}")
        return parsed

    def test_kind_specific_fields_read_back_unchanged(self):
        for token in VALID_TOKENS:
            with self.subTest(token=token):
                parsed = self._parse(
                    ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=envelope(),
                        workflow=token,
                        run=token,
                    ),
                    kind=ev.EVIDENCE_REQUIRED_CI_GREEN,
                )
                self.assertEqual(parsed.extra[ev.FIELD_WORKFLOW], token)
                self.assertEqual(parsed.extra[ev.FIELD_RUN], token)

    def test_the_envelope_reads_back_unchanged(self):
        for token in VALID_TOKENS:
            with self.subTest(token=token):
                parsed = self._parse(
                    ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=envelope(workspace=token, lane=token),
                        workflow="check",
                        run="run",
                    ),
                    kind=ev.EVIDENCE_REQUIRED_CI_GREEN,
                )
                self.assertEqual(parsed.envelope.workspace, token)
                self.assertEqual(parsed.envelope.lane, token)

    def test_unusual_but_valid_tokens_still_render(self):
        for token in VALID_TOKENS:
            with self.subTest(token=token):
                self.assertIn(
                    f"{ev.FIELD_WORKFLOW}={token}",
                    ev.render_hibernate_evidence(
                        ev.EVIDENCE_REQUIRED_CI_GREEN,
                        envelope=envelope(),
                        workflow=token,
                        run="run",
                    ),
                )

    def test_a_clean_integration_marker_still_renders(self):
        self.assertIn(
            "integration_branch=main-next",
            ie.render_integration_evidence(
                envelope=envelope(),
                integration_head=INTEGRATION_HEAD,
                integration_branch="main-next",
                disposition="merge",
            ),
        )


class ParseSideIsUnchangedTests(unittest.TestCase):
    """#14694 is producer-side. A tightened READER would be a different change (a stated non-goal)."""

    def _ci_fields(self, **over) -> dict:
        fields = {
            "gate": ev.EVIDENCE_REQUIRED_CI_GREEN,
            "workspace": WS,
            "lane": LANE,
            "lane_generation": "3",
            "head": HEAD,
            "workflow": "test.yml",
            "run": "1",
            "conclusion": "success",
        }
        fields.update(over)
        return fields

    def test_the_parser_still_accepts_what_it_accepted(self):
        self.assertIsInstance(
            ev.parse_hibernate_evidence(self._ci_fields(), kind=ev.EVIDENCE_REQUIRED_CI_GREEN),
            ev.HibernateEvidence,
        )

    def test_the_parser_still_refuses_with_the_same_typed_reasons(self):
        for over, reason in (
            ({"run": ""}, ev.EVIDENCE_MISSING_RUN),
            ({"workflow": ""}, ev.EVIDENCE_MISSING_WORKFLOW),
            ({"conclusion": "failure"}, ev.EVIDENCE_CI_NOT_SUCCESS),
        ):
            with self.subTest(over=over):
                got = ev.parse_hibernate_evidence(
                    self._ci_fields(**over), kind=ev.EVIDENCE_REQUIRED_CI_GREEN
                )
                self.assertEqual(got.reason, reason)

    def test_duplicate_and_conflict_resolution_is_unchanged(self):
        one = self._ci_fields()
        self.assertIsInstance(
            ev.resolve_hibernate_evidence([one, dict(one)], kind=ev.EVIDENCE_REQUIRED_CI_GREEN),
            ev.HibernateEvidence,
        )
        self.assertEqual(
            ev.resolve_hibernate_evidence(
                [one, self._ci_fields(run="2")], kind=ev.EVIDENCE_REQUIRED_CI_GREEN
            ).reason,
            ev.EVIDENCE_CONFLICT,
        )
        self.assertEqual(
            ev.resolve_hibernate_evidence([], kind=ev.EVIDENCE_REQUIRED_CI_GREEN).reason,
            ev.EVIDENCE_ABSENT,
        )

    def test_the_parse_side_branch_rule_still_accepts_what_the_renderer_now_refuses(self):
        # The renderer refuses ANY whitespace; the reader keeps its own (narrower) punctuation
        # rule. Refusing to write more than you refuse to read is the safe direction, and this
        # pins that the reader was not dragged along with the producer.
        fields = {
            "gate": "integration_disposition",
            "workspace": WS,
            "lane": LANE,
            "lane_generation": "3",
            "head": HEAD,
            "integration_head": INTEGRATION_HEAD,
            "integration_branch": "main\xa0next",
            "disposition": "merge",
        }
        self.assertIsInstance(ie.parse_integration_evidence(fields), ie.IntegrationEvidence)


if __name__ == "__main__":
    unittest.main()
