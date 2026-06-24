"""Text presentation provider tests (Redmine #12185).

Pins the projection-only ``text`` surface implementation added on top of the
presentation adapter boundary (Redmine #12156, design doc #12001 "Candidate 2"):
the built-in text attention provider, the stable text-surface field labels and
order, the empty-timestamp behaviour, the pure ``render_surface_text`` renderer,
and the read/projection-first invariant (no send / route / approve surface). It
also cross-checks that the text projection carries exactly the same four logical
attention cells, in the same order, as the tmux projection, so the two surfaces
cannot drift. No tmux / file / network is exercised here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.attention_projection import ATTENTION_OPTION_NAMES
from mozyo_bridge.application.text_attention_presentation_provider import (
    PROVIDER_NAME,
    TEXT_ATTENTION_PRESENTATION_PROVIDER,
    TEXT_FIELD_LABELS,
    TEXT_REASON_LABEL,
    TEXT_SEVERITY_LABEL,
    TEXT_STATE_LABEL,
    TEXT_UPDATED_AT_LABEL,
    TextAttentionPresentationProvider,
    render_surface_text,
)
from mozyo_bridge.application.tmux_attention_presentation_provider import (
    TMUX_ATTENTION_PRESENTATION_PROVIDER,
)
from mozyo_bridge.domain.attention import AttentionRecord
from mozyo_bridge.domain.presentation_adapter import (
    SURFACE_TEXT,
    PresentationProvider,
    ProjectionField,
    SurfaceProjection,
)


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


class TextProviderProtocolTest(unittest.TestCase):
    def test_satisfies_presentation_provider_protocol(self) -> None:
        self.assertIsInstance(
            TEXT_ATTENTION_PRESENTATION_PROVIDER, PresentationProvider
        )
        self.assertEqual(PROVIDER_NAME, TEXT_ATTENTION_PRESENTATION_PROVIDER.name)
        self.assertEqual(
            SURFACE_TEXT, TEXT_ATTENTION_PRESENTATION_PROVIDER.surface
        )


class TextProjectionTest(unittest.TestCase):
    def test_projects_attention_record_onto_text_surface(self) -> None:
        record = _attention_record()
        projection = TEXT_ATTENTION_PRESENTATION_PROVIDER.project(record)

        self.assertEqual(PROVIDER_NAME, projection.provider)
        self.assertEqual(SURFACE_TEXT, projection.surface)
        # source_unit_id is provenance, carried straight from the record.
        self.assertEqual(record.unit_id, projection.source_unit_id)
        self.assertEqual(
            {
                TEXT_STATE_LABEL: "review_waiting",
                TEXT_SEVERITY_LABEL: "notice",
                TEXT_REASON_LABEL: "review_request_pending",
                TEXT_UPDATED_AT_LABEL: "2026-06-18T00:00:00Z",
            },
            projection.as_mapping(),
        )

    def test_field_order_is_stable(self) -> None:
        projection = TEXT_ATTENTION_PRESENTATION_PROVIDER.project(_attention_record())
        self.assertEqual(
            list(TEXT_FIELD_LABELS), [cell.key for cell in projection.fields]
        )

    def test_text_cells_track_the_tmux_projection_one_to_one(self) -> None:
        # Same record -> the text surface shows the same four logical cells, in
        # the same order, as the tmux surface, so the two cannot drift in which
        # facts they present.
        record = _attention_record()
        text = TEXT_ATTENTION_PRESENTATION_PROVIDER.project(record)
        tmux = TMUX_ATTENTION_PRESENTATION_PROVIDER.project(record)

        self.assertEqual(len(TEXT_FIELD_LABELS), len(ATTENTION_OPTION_NAMES))
        self.assertEqual(
            [cell.value for cell in text.fields],
            [cell.value for cell in tmux.fields],
        )

    def test_text_labels_are_not_tmux_option_names(self) -> None:
        # The text surface uses human labels, not the @mozyo_attention_* options.
        self.assertEqual((), tuple(set(TEXT_FIELD_LABELS) & set(ATTENTION_OPTION_NAMES)))

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
        projection = TEXT_ATTENTION_PRESENTATION_PROVIDER.project(record)
        self.assertEqual("", projection.as_mapping()[TEXT_UPDATED_AT_LABEL])


class RenderSurfaceTextTest(unittest.TestCase):
    def test_renders_text_projection_as_key_value_lines(self) -> None:
        projection = TEXT_ATTENTION_PRESENTATION_PROVIDER.project(_attention_record())
        self.assertEqual(
            "\n".join(
                (
                    "state: review_waiting",
                    "severity: notice",
                    "reason: review_request_pending",
                    "updated_at: 2026-06-18T00:00:00Z",
                )
            ),
            render_surface_text(projection),
        )

    def test_renders_any_surface_projection(self) -> None:
        # The renderer reads only the projection's fields, so it works for the
        # tmux projection too (no derivation, no I/O).
        projection = TMUX_ATTENTION_PRESENTATION_PROVIDER.project(_attention_record())
        rendered = render_surface_text(projection)
        for cell in projection.fields:
            self.assertIn(f"{cell.key}: {cell.value}", rendered)
        self.assertEqual(len(projection.fields), len(rendered.splitlines()))

    def test_empty_value_keeps_a_stable_trailing_space_line(self) -> None:
        projection = SurfaceProjection(
            provider=PROVIDER_NAME,
            surface=SURFACE_TEXT,
            source_unit_id="unit:local:ws1:lane-a",
            fields=(ProjectionField(TEXT_UPDATED_AT_LABEL, ""),),
        )
        self.assertEqual("updated_at: ", render_surface_text(projection))

    def test_no_fields_renders_empty_string(self) -> None:
        projection = SurfaceProjection(
            provider=PROVIDER_NAME,
            surface=SURFACE_TEXT,
            source_unit_id="unit:local:ws1:lane-a",
        )
        self.assertEqual("", render_surface_text(projection))


class TextProviderHasNoRoutingSurfaceTest(unittest.TestCase):
    def test_provider_exposes_no_routing_or_approval_method(self) -> None:
        # Read/projection-first: the provider and its module must not grow a
        # send/route/approve surface that would make display a routing or
        # approval authority.
        import mozyo_bridge.application.text_attention_presentation_provider as mod

        for forbidden in ("send", "route", "approve", "close", "resolve_target"):
            self.assertFalse(
                hasattr(TextAttentionPresentationProvider, forbidden), msg=forbidden
            )
            self.assertFalse(hasattr(mod, forbidden), msg=f"module:{forbidden}")


if __name__ == "__main__":
    unittest.main()
