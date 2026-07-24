"""Hibernate-evidence lane-envelope producer fields on the review gate marker (Redmine #14219 T2b, step 2).

Pins the ADDITIVE lane envelope on ``render_workflow_event_marker`` and the ``--emit-gate`` review
builder:

- the envelope (``workspace`` / ``lane`` / ``lane_generation``) is emitted ONLY when supplied, after
  the existing ``head`` / ``req`` fields; a bare / legacy marker is byte-unchanged;
- the marker round-trips: the parser reads the envelope AND the existing ``conclusion`` / ``head`` /
  ``req`` unchanged (additive-safe, no consumer regression);
- the ``--emit-gate`` review builder treats the envelope as all-or-nothing: a partial or
  malformed-generation envelope is a fixed refusal (never a half-bound marker); a fully-absent one
  is a legacy marker.
"""

from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_callbacks import (  # noqa: E501
    _review_gate_marker_fields,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    marker_fields_in_note,
    render_workflow_event_marker,
)

HEAD = "a" * 40
WS = "ws-1"
LANE = "lane-abc"


def _parse(marker: str) -> dict:
    (_, fields), = marker_fields_in_note(marker)
    return fields


class RendererTests(unittest.TestCase):
    def test_a_review_result_without_envelope_is_unchanged(self):
        got = render_workflow_event_marker(
            "review_result", conclusion="approved", target_head=HEAD, review_request_journal="42"
        )
        self.assertEqual(
            got, f"[mozyo:workflow-event:gate=review_result:conclusion=approved:head={HEAD}:req=42]"
        )

    def test_a_bare_marker_is_unchanged(self):
        self.assertEqual(
            render_workflow_event_marker("review_request"),
            "[mozyo:workflow-event:gate=review_request]",
        )

    def test_the_envelope_is_appended_after_req(self):
        got = render_workflow_event_marker(
            "review_result", conclusion="approved", target_head=HEAD, review_request_journal="42",
            evidence_workspace=WS, evidence_lane=LANE, evidence_lane_generation=3,
        )
        self.assertEqual(
            got,
            f"[mozyo:workflow-event:gate=review_result:conclusion=approved:head={HEAD}:req=42:"
            f"workspace={WS}:lane={LANE}:lane_generation=3]",
        )

    def test_the_marker_round_trips_without_disturbing_known_fields(self):
        got = render_workflow_event_marker(
            "review_result", conclusion="approved", target_head=HEAD, review_request_journal="42",
            evidence_workspace=WS, evidence_lane=LANE, evidence_lane_generation=3,
        )
        fields = _parse(got)
        # the existing consumers' fields are intact...
        self.assertEqual(fields["gate"], "review_result")
        self.assertEqual(fields["conclusion"], "approved")
        self.assertEqual(fields["head"], HEAD)
        self.assertEqual(fields["req"], "42")
        # ...and the envelope is readable.
        self.assertEqual(fields["workspace"], WS)
        self.assertEqual(fields["lane"], LANE)
        self.assertEqual(fields["lane_generation"], "3")


class BuilderEnvelopeTests(unittest.TestCase):
    def _args(self, **over):
        base = dict(
            target_head=HEAD, review_request_journal="42", review_decision="approval",
            evidence_workspace=None, evidence_lane=None, evidence_lane_generation=None,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def test_no_envelope_is_a_legacy_marker(self):
        fields, refusal = _review_gate_marker_fields(self._args(), "review_result")
        self.assertIsNone(refusal)
        self.assertNotIn("evidence_workspace", fields)

    def test_a_full_envelope_is_included(self):
        fields, refusal = _review_gate_marker_fields(
            self._args(evidence_workspace=WS, evidence_lane=LANE, evidence_lane_generation="3"),
            "review_result",
        )
        self.assertIsNone(refusal)
        self.assertEqual(fields["evidence_workspace"], WS)
        self.assertEqual(fields["evidence_lane"], LANE)
        self.assertEqual(fields["evidence_lane_generation"], 3)

    def test_a_partial_envelope_is_refused(self):
        fields, refusal = _review_gate_marker_fields(
            self._args(evidence_workspace=WS, evidence_lane=LANE), "review_result"
        )
        self.assertEqual(refusal, "evidence_envelope_incomplete")
        self.assertEqual(fields, {})

    def test_a_separator_bearing_identity_is_a_typed_refusal(self):
        # The CLI is operator input, so it gets a fixed refusal token rather than the renderer's
        # exception — but it refuses the same values (checkpoint j#86443 R2-F3).
        for bad in ({"evidence_workspace": "ws:forged"}, {"evidence_lane": "lane]cut"}):
            with self.subTest(**bad):
                args = dict(
                    evidence_workspace=WS, evidence_lane=LANE, evidence_lane_generation="4"
                )
                args.update(bad)
                fields, refusal = _review_gate_marker_fields(
                    self._args(**args), "review_result"
                )
                self.assertEqual(refusal, "evidence_envelope_malformed_identity")
                self.assertEqual(fields, {})

    def test_a_non_positive_generation_is_refused(self):
        _, refusal = _review_gate_marker_fields(
            self._args(evidence_workspace=WS, evidence_lane=LANE, evidence_lane_generation="0"),
            "review_result",
        )
        self.assertEqual(refusal, "evidence_envelope_malformed_generation")

    def test_a_non_numeric_generation_is_refused(self):
        _, refusal = _review_gate_marker_fields(
            self._args(evidence_workspace=WS, evidence_lane=LANE, evidence_lane_generation="two"),
            "review_result",
        )
        self.assertEqual(refusal, "evidence_envelope_malformed_generation")

    def test_a_missing_head_still_refuses_before_the_envelope(self):
        # the existing v2 head fence is unchanged.
        _, refusal = _review_gate_marker_fields(
            self._args(target_head="", evidence_workspace=WS, evidence_lane=LANE,
                       evidence_lane_generation="3"),
            "review_result",
        )
        self.assertEqual(refusal, "review_marker_missing_target_head")


class ReviewMarkerStrictRendererTests(unittest.TestCase):
    """Checkpoint j#86443 R2-F3: review_result is an evidence kind, so its renderer is strict too.

    Before this, the envelope fields were concatenated straight into the marker body, so an
    identity carrying a separator injected a second field ahead of the real one and closed the
    marker early — from a producer-supplied id.
    """

    HEAD = "a" * 40

    def _render(self, **envelope):
        return render_workflow_event_marker(
            "review_result",
            target_head=self.HEAD,
            review_request_journal="85400",
            conclusion="approved",
            **envelope,
        )

    def test_a_valid_envelope_still_renders(self):
        marker = self._render(
            evidence_workspace="ws-1", evidence_lane="lane-abc", evidence_lane_generation=4
        )
        self.assertIn(":workspace=ws-1:lane=lane-abc:lane_generation=4]", marker)

    def test_a_separator_bearing_identity_is_refused(self):
        # The exact reproduction from the finding: this used to render
        # `workspace=ws:lane=forged:lane=lane]truncated` — a forged `lane` field, then truncation.
        with self.assertRaises(ValueError):
            self._render(
                evidence_workspace="ws:lane=forged",
                evidence_lane="lane-abc",
                evidence_lane_generation=4,
            )
        with self.assertRaises(ValueError):
            self._render(
                evidence_workspace="ws-1",
                evidence_lane="lane]truncated",
                evidence_lane_generation=4,
            )

    def test_a_non_positive_generation_is_refused(self):
        for generation in (0, -3):
            with self.subTest(generation=generation):
                with self.assertRaises(ValueError):
                    self._render(
                        evidence_workspace="ws-1",
                        evidence_lane="lane-abc",
                        evidence_lane_generation=generation,
                    )

    def test_a_partial_envelope_is_refused(self):
        # Each shape separately: without the all-or-none guard, a missing LANE renders the literal
        # `lane=None` (str(None) is non-empty), i.e. silent garbage rather than a refusal.
        for partial in (
            dict(evidence_workspace="ws-1"),
            dict(evidence_workspace="ws-1", evidence_lane_generation=4),
            dict(evidence_lane="lane-abc", evidence_lane_generation=4),
        ):
            with self.subTest(**partial):
                with self.assertRaises(ValueError):
                    self._render(**partial)

    def test_no_envelope_still_renders_a_legacy_marker(self):
        marker = self._render()
        self.assertNotIn("workspace=", marker)
        self.assertIn("req=85400", marker)


if __name__ == "__main__":
    unittest.main()
