"""Hibernate-evidence marker grammar tests (Redmine #14219 T2b, step 3).

Pins the dedicated CI / dogfood / park evidence renderer + strict parser:

- **render / parse round-trip** per kind, carrying the common lane envelope + kind-specific fields;
- **head requirement** — CI and dogfood are head-bearing (a head-less envelope is refused); park is
  lane-anchored (no head);
- **kind-specific fail-closed** — CI needs a run and ``conclusion=success`` (a missing run or a
  non-success conclusion is refused); dogfood needs a release_issue;
- **resolution** — absent evidence is typed, differing markers conflict, a malformed marker of the
  kind is a hard parse error (never silently skipped).
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    hibernate_evidence_marker as ev,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    LaneEvidenceEnvelope,
)

WS = "ws-1"
LANE = "lane-abc"
HEAD = "a" * 40


def _env(head=HEAD, gen=3):
    return LaneEvidenceEnvelope(workspace=WS, lane=LANE, lane_generation=gen, head=head)


def _fields(marker: str) -> dict:
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
        marker_fields_in_note,
    )
    (_, fields), = marker_fields_in_note(marker)
    return fields


class RenderParseRoundTripTests(unittest.TestCase):
    def test_ci_green_round_trips(self):
        marker = ev.render_hibernate_evidence(
            ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=_env(), run="29860030313"
        )
        got = ev.parse_hibernate_evidence(_fields(marker), kind=ev.EVIDENCE_REQUIRED_CI_GREEN)
        self.assertIsInstance(got, ev.HibernateEvidence)
        self.assertEqual(got.envelope, _env())
        self.assertEqual(got.extra, {"run": "29860030313", "conclusion": "success"})

    def test_dogfood_round_trips(self):
        marker = ev.render_hibernate_evidence(
            ev.EVIDENCE_DOGFOOD_DELEGATED, envelope=_env(), release_issue="14184"
        )
        got = ev.parse_hibernate_evidence(_fields(marker), kind=ev.EVIDENCE_DOGFOOD_DELEGATED)
        self.assertIsInstance(got, ev.HibernateEvidence)
        self.assertEqual(got.extra, {"release_issue": "14184"})
        self.assertEqual(got.envelope.head, HEAD)

    def test_park_round_trips_without_head(self):
        marker = ev.render_hibernate_evidence(
            ev.EVIDENCE_PARK_DECLARED, envelope=_env(head="")
        )
        self.assertNotIn("head=", marker)
        got = ev.parse_hibernate_evidence(_fields(marker), kind=ev.EVIDENCE_PARK_DECLARED)
        self.assertIsInstance(got, ev.HibernateEvidence)
        self.assertEqual(got.envelope.head, "")
        self.assertEqual(got.extra, {})


class RenderValidationTests(unittest.TestCase):
    def test_ci_without_head_raises(self):
        with self.assertRaises(ValueError):
            ev.render_hibernate_evidence(
                ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=_env(head=""), run="1"
            )

    def test_ci_without_run_raises(self):
        with self.assertRaises(ValueError):
            ev.render_hibernate_evidence(ev.EVIDENCE_REQUIRED_CI_GREEN, envelope=_env())

    def test_dogfood_without_release_issue_raises(self):
        with self.assertRaises(ValueError):
            ev.render_hibernate_evidence(ev.EVIDENCE_DOGFOOD_DELEGATED, envelope=_env())

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            ev.render_hibernate_evidence("whatever", envelope=_env())


class ParseFailClosedTests(unittest.TestCase):
    def test_ci_missing_run_is_typed(self):
        fields = {"gate": ev.EVIDENCE_REQUIRED_CI_GREEN, "workspace": WS, "lane": LANE,
                  "lane_generation": "3", "head": HEAD, "conclusion": "success"}
        got = ev.parse_hibernate_evidence(fields, kind=ev.EVIDENCE_REQUIRED_CI_GREEN)
        self.assertEqual(got.reason, ev.EVIDENCE_MISSING_RUN)

    def test_ci_non_success_conclusion_is_typed(self):
        fields = {"gate": ev.EVIDENCE_REQUIRED_CI_GREEN, "workspace": WS, "lane": LANE,
                  "lane_generation": "3", "head": HEAD, "run": "1", "conclusion": "failure"}
        got = ev.parse_hibernate_evidence(fields, kind=ev.EVIDENCE_REQUIRED_CI_GREEN)
        self.assertEqual(got.reason, ev.EVIDENCE_CI_NOT_SUCCESS)

    def test_ci_head_less_envelope_is_refused(self):
        fields = {"gate": ev.EVIDENCE_REQUIRED_CI_GREEN, "workspace": WS, "lane": LANE,
                  "lane_generation": "3", "run": "1", "conclusion": "success"}
        got = ev.parse_hibernate_evidence(fields, kind=ev.EVIDENCE_REQUIRED_CI_GREEN)
        # the envelope's own missing-head reason surfaces.
        self.assertEqual(got.reason, "envelope_missing_head")

    def test_dogfood_missing_release_issue_is_typed(self):
        fields = {"gate": ev.EVIDENCE_DOGFOOD_DELEGATED, "workspace": WS, "lane": LANE,
                  "lane_generation": "3", "head": HEAD}
        got = ev.parse_hibernate_evidence(fields, kind=ev.EVIDENCE_DOGFOOD_DELEGATED)
        self.assertEqual(got.reason, ev.EVIDENCE_MISSING_RELEASE_ISSUE)

    def test_unknown_kind_is_typed(self):
        got = ev.parse_hibernate_evidence({}, kind="nope")
        self.assertEqual(got.reason, ev.EVIDENCE_UNKNOWN_KIND)


class ResolveTests(unittest.TestCase):
    def _ci(self, **over):
        base = {"gate": ev.EVIDENCE_REQUIRED_CI_GREEN, "workspace": WS, "lane": LANE,
                "lane_generation": "3", "head": HEAD, "run": "1", "conclusion": "success"}
        base.update(over)
        return base

    def test_absent_is_typed(self):
        got = ev.resolve_hibernate_evidence([], kind=ev.EVIDENCE_REQUIRED_CI_GREEN)
        self.assertEqual(got.reason, ev.EVIDENCE_ABSENT)

    def test_other_kinds_are_ignored(self):
        park = {"gate": ev.EVIDENCE_PARK_DECLARED, "workspace": WS, "lane": LANE, "lane_generation": "3"}
        got = ev.resolve_hibernate_evidence([park], kind=ev.EVIDENCE_REQUIRED_CI_GREEN)
        self.assertEqual(got.reason, ev.EVIDENCE_ABSENT)

    def test_one_resolves(self):
        got = ev.resolve_hibernate_evidence([self._ci()], kind=ev.EVIDENCE_REQUIRED_CI_GREEN)
        self.assertIsInstance(got, ev.HibernateEvidence)

    def test_identical_duplicates_collapse(self):
        got = ev.resolve_hibernate_evidence([self._ci(), self._ci()], kind=ev.EVIDENCE_REQUIRED_CI_GREEN)
        self.assertIsInstance(got, ev.HibernateEvidence)

    def test_differing_generation_conflicts(self):
        got = ev.resolve_hibernate_evidence(
            [self._ci(), self._ci(lane_generation="4")], kind=ev.EVIDENCE_REQUIRED_CI_GREEN
        )
        self.assertEqual(got.reason, ev.EVIDENCE_CONFLICT)

    def test_differing_run_conflicts(self):
        got = ev.resolve_hibernate_evidence(
            [self._ci(), self._ci(run="2")], kind=ev.EVIDENCE_REQUIRED_CI_GREEN
        )
        self.assertEqual(got.reason, ev.EVIDENCE_CONFLICT)

    def test_a_malformed_marker_of_the_kind_is_a_hard_error(self):
        got = ev.resolve_hibernate_evidence(
            [self._ci(conclusion="failure")], kind=ev.EVIDENCE_REQUIRED_CI_GREEN
        )
        self.assertEqual(got.reason, ev.EVIDENCE_CI_NOT_SUCCESS)


if __name__ == "__main__":
    unittest.main()
