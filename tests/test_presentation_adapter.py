"""Presentation adapter boundary seam tests (Redmine #12156).

Pins the first concrete cut of the built-in presentation adapter boundary
(Redmine #12001 design doc, "Candidate 2"): the core-owned surface vocabulary,
the projection-only invariant (a projection cannot carry a core-owned
authority), the pure :class:`SurfaceProjection` / :class:`ProjectionField`
records, and the built-in tmux attention projection provider. The provider is
read/projection-first and reuses the canonical ``@mozyo_attention_*`` option
names, which this test cross-checks against ``attention_projection`` so the
classified projection and the executed plan cannot drift. No tmux / network is
exercised here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.attention_projection import (
    ATTENTION_OPTION_NAMES,
    ATTENTION_REASON_OPTION,
    ATTENTION_SEVERITY_OPTION,
    ATTENTION_STATE_OPTION,
    ATTENTION_UPDATED_AT_OPTION,
    build_attention_option_plan,
)
from mozyo_bridge.application.tmux_attention_presentation_provider import (
    PROVIDER_NAME,
    TMUX_ATTENTION_PRESENTATION_PROVIDER,
    TmuxAttentionPresentationProvider,
)
from mozyo_bridge.domain.attention import AttentionRecord
from mozyo_bridge.domain.presentation_adapter import (
    FORBIDDEN_PROJECTION_FIELDS,
    PRESENTATION_SURFACES,
    SURFACE_TEXT,
    SURFACE_TMUX_USER_OPTION,
    PresentationProvider,
    PresentationRecordError,
    ProjectionField,
    SurfaceProjection,
)
from mozyo_bridge.domain.provider_registry import FORBIDDEN_PROVIDER_AUTHORITIES


def _attention_record() -> AttentionRecord:
    return AttentionRecord(
        unit_id="unit:local:ws1:lane-a",
        host_id="local",
        workspace_id="ws1",
        lane_id="lane-a",
        role="claude",
        target_key="tmux:local:%78",
        attention_state="review_waiting",
        severity="notice",
        reason_code="review_request_pending",
        observed_at="2026-06-18T00:00:00Z",
    )


class SurfaceVocabularyTest(unittest.TestCase):
    def test_surfaces_are_the_design_doc_presentation_surfaces(self) -> None:
        # The MVP names "tmux user options and text output".
        self.assertEqual(
            {"tmux_user_option", "text"}, set(PRESENTATION_SURFACES)
        )
        self.assertIn(SURFACE_TMUX_USER_OPTION, PRESENTATION_SURFACES)
        self.assertIn(SURFACE_TEXT, PRESENTATION_SURFACES)

    def test_unknown_surface_is_rejected(self) -> None:
        with self.assertRaises(PresentationRecordError):
            SurfaceProjection(
                provider="tmux-presentation",
                surface="iterm_color",  # not a recognized surface
                source_unit_id="unit:local:ws1:lane-a",
            )

    def test_empty_provider_is_rejected(self) -> None:
        with self.assertRaises(PresentationRecordError):
            SurfaceProjection(
                provider="",
                surface=SURFACE_TMUX_USER_OPTION,
                source_unit_id="unit:local:ws1:lane-a",
            )


class ProjectionFieldTest(unittest.TestCase):
    def test_key_must_be_non_empty_string(self) -> None:
        with self.assertRaises(PresentationRecordError):
            ProjectionField("", "value")

    def test_value_must_be_string(self) -> None:
        with self.assertRaises(PresentationRecordError):
            ProjectionField("@mozyo_attention_state", 1)  # type: ignore[arg-type]

    def test_empty_value_is_allowed(self) -> None:
        # An absent timestamp projects as "" rather than raising.
        field = ProjectionField("@mozyo_attention_updated_at", "")
        self.assertEqual("", field.value)


class ProjectionOnlyInvariantTest(unittest.TestCase):
    def test_forbidden_fields_match_the_core_owned_authorities(self) -> None:
        # The projection-only invariant reuses the single authority vocabulary
        # from the registry seam so the two can never drift apart.
        self.assertEqual(
            set(FORBIDDEN_PROVIDER_AUTHORITIES), set(FORBIDDEN_PROJECTION_FIELDS)
        )

    def test_projection_cannot_carry_a_core_owned_authority(self) -> None:
        for authority in FORBIDDEN_PROJECTION_FIELDS:
            with self.assertRaises(PresentationRecordError, msg=authority):
                SurfaceProjection(
                    provider="rogue",
                    surface=SURFACE_TMUX_USER_OPTION,
                    source_unit_id="unit:local:ws1:lane-a",
                    fields=(
                        ProjectionField("@mozyo_attention_state", "review_waiting"),
                        ProjectionField(authority, "yes"),
                    ),
                )

    def test_non_projection_field_entries_are_rejected(self) -> None:
        with self.assertRaises(PresentationRecordError):
            SurfaceProjection(
                provider="rogue",
                surface=SURFACE_TMUX_USER_OPTION,
                source_unit_id="unit:local:ws1:lane-a",
                fields=("@mozyo_attention_state",),  # type: ignore[arg-type]
            )

    def test_as_mapping_returns_field_pairs(self) -> None:
        projection = SurfaceProjection(
            provider="tmux-presentation",
            surface=SURFACE_TMUX_USER_OPTION,
            source_unit_id="unit:local:ws1:lane-a",
            fields=(ProjectionField("@mozyo_attention_state", "healthy"),),
        )
        self.assertEqual({"@mozyo_attention_state": "healthy"}, projection.as_mapping())


class TmuxAttentionProviderTest(unittest.TestCase):
    def test_satisfies_presentation_provider_protocol(self) -> None:
        self.assertIsInstance(
            TMUX_ATTENTION_PRESENTATION_PROVIDER, PresentationProvider
        )
        self.assertEqual(PROVIDER_NAME, TMUX_ATTENTION_PRESENTATION_PROVIDER.name)
        self.assertEqual(
            SURFACE_TMUX_USER_OPTION, TMUX_ATTENTION_PRESENTATION_PROVIDER.surface
        )

    def test_projects_attention_record_onto_tmux_option_fields(self) -> None:
        record = _attention_record()
        projection = TMUX_ATTENTION_PRESENTATION_PROVIDER.project(record)

        self.assertEqual(PROVIDER_NAME, projection.provider)
        self.assertEqual(SURFACE_TMUX_USER_OPTION, projection.surface)
        # source_unit_id is provenance, carried straight from the record.
        self.assertEqual(record.unit_id, projection.source_unit_id)
        self.assertEqual(
            {
                ATTENTION_STATE_OPTION: "review_waiting",
                ATTENTION_SEVERITY_OPTION: "notice",
                ATTENTION_REASON_OPTION: "review_request_pending",
                ATTENTION_UPDATED_AT_OPTION: "2026-06-18T00:00:00Z",
            },
            projection.as_mapping(),
        )

    def test_field_keys_match_canonical_attention_option_names(self) -> None:
        # Single source of truth: the provider must project exactly the option
        # names attention_projection executes, so the classified projection and
        # the executed plan cannot drift apart.
        projection = TMUX_ATTENTION_PRESENTATION_PROVIDER.project(_attention_record())
        self.assertEqual(
            list(ATTENTION_OPTION_NAMES),
            [cell.key for cell in projection.fields],
        )

    def test_projection_values_agree_with_executed_plan(self) -> None:
        record = _attention_record()
        projection = TMUX_ATTENTION_PRESENTATION_PROVIDER.project(record)
        plan = build_attention_option_plan("%78", record)
        # Each set-option argv ends with (name, value); the normalized projection
        # carries the same name -> value pairs.
        plan_pairs = {argv[-2]: argv[-1] for argv in plan}
        self.assertEqual(plan_pairs, projection.as_mapping())

    def test_missing_observed_at_projects_empty_value(self) -> None:
        record = AttentionRecord(
            unit_id="unit:local:ws1:lane-a",
            host_id="local",
            workspace_id="ws1",
            lane_id="lane-a",
            role="claude",
            target_key=None,
            attention_state="unknown",
            severity="warning",
            reason_code="source_unreadable",
            observed_at="",
        )
        projection = TMUX_ATTENTION_PRESENTATION_PROVIDER.project(record)
        self.assertEqual("", projection.as_mapping()[ATTENTION_UPDATED_AT_OPTION])

    def test_provider_exposes_no_routing_or_approval_method(self) -> None:
        # Read/projection-first: the provider must not grow a send/route/approve
        # surface that would make display a routing or approval authority.
        for forbidden in ("send", "route", "approve", "close", "resolve_target"):
            self.assertFalse(
                hasattr(TmuxAttentionPresentationProvider, forbidden), msg=forbidden
            )


if __name__ == "__main__":
    unittest.main()
