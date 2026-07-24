"""Enveloped integration-disposition evidence tests (Redmine #14219 T2b, step 3b).

Pins the ruling's Q2 grammar (#14219 j#85530):

- **source head vs integration head are separate values** — the envelope's ``head`` is the reviewed
  lane head, ``integration_head`` the exact staging commit, and a patch-equivalent record carries
  two DIFFERENT commits without either being ambiguous;
- **strict / fail-closed** — every missing or malformed field is a distinct typed reason, and only
  ``merge`` / ``patch_equivalent`` mean integrated (``explicit_deferral`` / ``integration_blocked``
  are a typed zero, not evidence);
- **legacy markers are a hard error, not skipped** — a lane-unbound marker of this gate must never
  be dropped, or a newer deferral would lose to an older enveloped merge;
- **#14213 glance is untouched** — the additive fields are invisible to
  ``fold_integration_disposition``, which folds an enveloped marker exactly as it folds the legacy
  one.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    hibernate_evidence_integration as ie,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_integration_disposition import (  # noqa: E501
    fold_integration_disposition,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    ENVELOPE_MISSING_HEAD,
    ENVELOPE_MISSING_WORKSPACE,
    ENVELOPE_MALFORMED_GENERATION,
    LaneEvidenceEnvelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    marker_fields_in_note,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
    INTEGRATION_MERGE,
    INTEGRATION_PATCH_EQUIVALENT,
)

WS = "ws-1"
LANE = "lane-abc"
SOURCE_HEAD = "a" * 40
INTEGRATION_HEAD = "b" * 40
BRANCH = "main-next"


def _env(head=SOURCE_HEAD, gen=4):
    return LaneEvidenceEnvelope(workspace=WS, lane=LANE, lane_generation=gen, head=head)


def _marker(**overrides) -> str:
    kwargs = {
        "envelope": _env(),
        "integration_head": INTEGRATION_HEAD,
        "integration_branch": BRANCH,
        "disposition": INTEGRATION_MERGE,
    }
    kwargs.update(overrides)
    return ie.render_integration_evidence(**kwargs)


def _fields(marker: str) -> dict:
    (_, fields), = marker_fields_in_note(marker)
    return fields


def _parse(**field_overrides):
    """Parse the canonical marker's fields with ``field_overrides`` applied (None deletes a key)."""
    fields = _fields(_marker())
    for key, value in field_overrides.items():
        if value is None:
            fields.pop(key, None)
        else:
            fields[key] = value
    return ie.parse_integration_evidence(fields)


class RenderParseRoundTripTests(unittest.TestCase):
    def test_merge_round_trips_with_both_heads_distinct(self):
        got = ie.parse_integration_evidence(_fields(_marker()))
        self.assertIsInstance(got, ie.IntegrationEvidence)
        self.assertEqual(got.envelope, _env())
        self.assertEqual(got.source_head, SOURCE_HEAD)
        self.assertEqual(got.integration_head, INTEGRATION_HEAD)
        self.assertNotEqual(got.source_head, got.integration_head)
        self.assertEqual(got.integration_branch, BRANCH)
        self.assertEqual(got.disposition, INTEGRATION_MERGE)

    def test_patch_equivalent_round_trips(self):
        got = ie.parse_integration_evidence(
            _fields(_marker(disposition=INTEGRATION_PATCH_EQUIVALENT))
        )
        self.assertIsInstance(got, ie.IntegrationEvidence)
        self.assertEqual(got.disposition, INTEGRATION_PATCH_EQUIVALENT)

    def test_marker_is_a_single_workflow_event_token(self):
        marker = _marker()
        self.assertTrue(marker.startswith("[mozyo:workflow-event:gate=integration_disposition:"))
        self.assertEqual(len(marker_fields_in_note("prose\n" + marker + "\nprose")), 1)

    def test_payload_exposes_both_heads(self):
        got = ie.parse_integration_evidence(_fields(_marker()))
        payload = got.as_payload()
        self.assertEqual(payload["envelope"]["head"], SOURCE_HEAD)
        self.assertEqual(payload["integration_head"], INTEGRATION_HEAD)
        self.assertEqual(payload["integration_branch"], BRANCH)


class RendererRefusesProducerErrorTests(unittest.TestCase):
    """An unrenderable evidence marker must never reach a journal."""

    def test_head_less_envelope_refused(self):
        with self.assertRaises(ValueError):
            _marker(envelope=_env(head=""))

    def test_abbreviated_integration_head_refused(self):
        with self.assertRaises(ValueError):
            _marker(integration_head=INTEGRATION_HEAD[:12])

    def test_uppercase_integration_head_refused(self):
        with self.assertRaises(ValueError):
            _marker(integration_head=INTEGRATION_HEAD.upper())

    def test_branch_with_marker_separator_refused(self):
        # ``:`` would split into a bogus extra field; ``]`` would truncate the token.
        for bad in ("refs:main", "main]next", "main next", ""):
            with self.assertRaises(ValueError):
                _marker(integration_branch=bad)

    def test_non_integrated_disposition_refused(self):
        for bad in ("explicit_deferral", "integration_blocked", "merged", "", "unknown"):
            with self.assertRaises(ValueError):
                _marker(disposition=bad)


class ParseFailClosedTests(unittest.TestCase):
    def test_missing_envelope_fields_are_typed(self):
        self.assertEqual(_parse(workspace=None).reason, ENVELOPE_MISSING_WORKSPACE)
        self.assertEqual(_parse(lane_generation="0").reason, ENVELOPE_MALFORMED_GENERATION)

    def test_lane_unbound_legacy_marker_is_refused(self):
        legacy = {"gate": "integration_disposition", "disposition": "merged"}
        got = ie.parse_integration_evidence(legacy)
        self.assertIsInstance(got, ie.IntegrationEvidenceError)
        self.assertEqual(got.reason, ENVELOPE_MISSING_WORKSPACE)

    def test_missing_source_head_is_refused(self):
        self.assertEqual(_parse(head=None).reason, ENVELOPE_MISSING_HEAD)

    def test_missing_integration_head_is_typed(self):
        self.assertEqual(
            _parse(integration_head=None).reason, ie.INTEGRATION_MISSING_INTEGRATION_HEAD
        )

    def test_malformed_integration_head_is_typed(self):
        for bad in (INTEGRATION_HEAD[:12], INTEGRATION_HEAD.upper(), "z" * 40):
            self.assertEqual(
                _parse(integration_head=bad).reason, ie.INTEGRATION_MALFORMED_INTEGRATION_HEAD
            )

    def test_missing_branch_is_typed(self):
        self.assertEqual(_parse(integration_branch=None).reason, ie.INTEGRATION_MISSING_BRANCH)

    def test_malformed_branch_is_typed(self):
        self.assertEqual(
            _parse(integration_branch="main]next").reason, ie.INTEGRATION_MALFORMED_BRANCH
        )

    def test_missing_disposition_is_typed(self):
        self.assertEqual(_parse(disposition=None).reason, ie.INTEGRATION_MISSING_DISPOSITION)

    def test_deferral_and_blocked_are_not_evidence(self):
        for token in ("explicit_deferral", "deferred", "integration_blocked", "blocked"):
            got = _parse(disposition=token)
            self.assertIsInstance(got, ie.IntegrationEvidenceError, token)
            self.assertEqual(got.reason, ie.INTEGRATION_NOT_INTEGRATED, token)

    def test_unreadable_disposition_is_not_evidence(self):
        self.assertEqual(_parse(disposition="probably-merged?").reason, ie.INTEGRATION_NOT_INTEGRATED)

    def test_durable_alias_spelling_canonicalizes(self):
        # ``merged`` is the spelling real governed journals carry (#14150 j#84605). One vocabulary
        # is shared with the glance, so the reader accepts it and stores the canonical token.
        got = _parse(disposition="merged")
        self.assertIsInstance(got, ie.IntegrationEvidence)
        self.assertEqual(got.disposition, INTEGRATION_MERGE)


class ResolutionTests(unittest.TestCase):
    def test_absent_is_typed(self):
        got = ie.resolve_integration_evidence([])
        self.assertEqual(got.reason, ie.INTEGRATION_EVIDENCE_ABSENT)

    def test_other_gates_are_ignored(self):
        got = ie.resolve_integration_evidence([{"gate": "review_result", "head": SOURCE_HEAD}])
        self.assertEqual(got.reason, ie.INTEGRATION_EVIDENCE_ABSENT)

    def test_identical_duplicates_collapse(self):
        fields = _fields(_marker())
        got = ie.resolve_integration_evidence([fields, dict(fields)])
        self.assertIsInstance(got, ie.IntegrationEvidence)
        self.assertEqual(got.integration_head, INTEGRATION_HEAD)

    def test_differing_markers_conflict(self):
        other = _fields(_marker(integration_head="c" * 40))
        got = ie.resolve_integration_evidence([_fields(_marker()), other])
        self.assertEqual(got.reason, ie.INTEGRATION_EVIDENCE_CONFLICT)

    def test_cross_lane_marker_conflicts(self):
        other = _fields(_marker(envelope=LaneEvidenceEnvelope(
            workspace=WS, lane="other-lane", lane_generation=4, head=SOURCE_HEAD
        )))
        got = ie.resolve_integration_evidence([_fields(_marker()), other])
        self.assertEqual(got.reason, ie.INTEGRATION_EVIDENCE_CONFLICT)

    def test_legacy_marker_is_a_hard_error_not_a_skip(self):
        """A newer legacy deferral must not be skipped so an older enveloped merge survives."""
        legacy = {"gate": "integration_disposition", "disposition": "explicit_deferral"}
        got = ie.resolve_integration_evidence([_fields(_marker()), legacy])
        self.assertIsInstance(got, ie.IntegrationEvidenceError)
        self.assertEqual(got.reason, ENVELOPE_MISSING_WORKSPACE)


class GlanceProjectionUnchangedTests(unittest.TestCase):
    """#14213 acceptance: the additive fields are invisible to the glance fold."""

    LEGACY_NOTE = (
        "## Integration Disposition — canonical staging merged\n\n"
        "[mozyo:workflow-event:gate=integration_disposition:disposition=merge]\n\n"
        "- next_owner: coordinator\n"
    )

    def _enveloped_note(self) -> str:
        return self.LEGACY_NOTE.replace(
            "[mozyo:workflow-event:gate=integration_disposition:disposition=merge]", _marker()
        )

    def test_enveloped_marker_folds_identically_to_legacy(self):
        legacy = fold_integration_disposition([("84605", self.LEGACY_NOTE)])
        enveloped = fold_integration_disposition([("84605", self._enveloped_note())])
        self.assertEqual(enveloped.as_payload(), legacy.as_payload())
        self.assertEqual(enveloped.disposition, INTEGRATION_MERGE)
        self.assertTrue(enveloped.complete)

    def test_enveloped_patch_equivalent_folds_to_patch_equivalent(self):
        note = self.LEGACY_NOTE.replace(
            "[mozyo:workflow-event:gate=integration_disposition:disposition=merge]",
            _marker(disposition=INTEGRATION_PATCH_EQUIVALENT),
        )
        got = fold_integration_disposition([("84605", note)])
        self.assertEqual(got.disposition, INTEGRATION_PATCH_EQUIVALENT)

    def test_glance_ignores_the_additive_head_fields(self):
        # The glance payload has no notion of integration_head / branch / lane envelope; the
        # enveloped marker must not add or shift any projected field.
        got = fold_integration_disposition([("84605", self._enveloped_note())])
        self.assertEqual(
            set(got.as_payload()), {"disposition", "journal", "reason", "unlock", "next_owner"}
        )
        self.assertEqual(got.next_owner, "coordinator")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
