"""Specs for the core-owned typed composer-render observation (Redmine #14065).

Pins the closed vocabulary and the fail-closed invariants of
:class:`PaneRenderObservation` — the #14065 phase-1 measurement instrument's
result shape. No body / hash / length / excerpt / raw ANSI is ever exposed; a
readable observation must carry reason ``ok`` and a concrete provenance, and
every fail-closed outcome is ``readable=False`` with ``style_provenance=unknown``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.pane_render_observation import (  # noqa: E501
    CURSOR_RELATION_COMPOSER,
    CURSOR_RELATION_UNKNOWN,
    RENDER_REASON_ANSI_ABSENT,
    RENDER_REASON_OK,
    STYLE_PROVENANCE_DIM,
    STYLE_PROVENANCE_NORMAL,
    STYLE_PROVENANCE_UNKNOWN,
    PaneRenderObservation,
    PaneRenderObservationError,
)


class ClassifiedFactoryTest(unittest.TestCase):
    def test_classified_is_readable_ok_with_concrete_provenance(self) -> None:
        obs = PaneRenderObservation.classified(
            STYLE_PROVENANCE_DIM, cursor_relation=CURSOR_RELATION_COMPOSER
        )
        self.assertTrue(obs.readable)
        self.assertEqual(STYLE_PROVENANCE_DIM, obs.style_provenance)
        self.assertEqual(CURSOR_RELATION_COMPOSER, obs.cursor_relation)
        self.assertEqual(RENDER_REASON_OK, obs.reason)
        self.assertTrue(obs.prompt_present)

    def test_normal_provenance_classified(self) -> None:
        obs = PaneRenderObservation.classified(STYLE_PROVENANCE_NORMAL)
        self.assertTrue(obs.readable)
        self.assertEqual(STYLE_PROVENANCE_NORMAL, obs.style_provenance)
        self.assertEqual(CURSOR_RELATION_UNKNOWN, obs.cursor_relation)


class FailedFactoryTest(unittest.TestCase):
    def test_failed_is_unreadable_unknown_with_reason(self) -> None:
        obs = PaneRenderObservation.failed(RENDER_REASON_ANSI_ABSENT)
        self.assertFalse(obs.readable)
        self.assertEqual(STYLE_PROVENANCE_UNKNOWN, obs.style_provenance)
        self.assertEqual(CURSOR_RELATION_UNKNOWN, obs.cursor_relation)
        self.assertEqual(RENDER_REASON_ANSI_ABSENT, obs.reason)

    def test_failed_may_carry_prompt_present(self) -> None:
        obs = PaneRenderObservation.failed(RENDER_REASON_ANSI_ABSENT, prompt_present=True)
        self.assertFalse(obs.readable)
        self.assertTrue(obs.prompt_present)


class InvariantTest(unittest.TestCase):
    def test_readable_must_carry_ok_reason(self) -> None:
        with self.assertRaises(PaneRenderObservationError):
            PaneRenderObservation(
                readable=True,
                style_provenance=STYLE_PROVENANCE_DIM,
                reason=RENDER_REASON_ANSI_ABSENT,
            )

    def test_readable_may_not_be_unknown_provenance(self) -> None:
        with self.assertRaises(PaneRenderObservationError):
            PaneRenderObservation(
                readable=True,
                style_provenance=STYLE_PROVENANCE_UNKNOWN,
                reason=RENDER_REASON_OK,
            )

    def test_unreadable_may_not_carry_ok_reason(self) -> None:
        with self.assertRaises(PaneRenderObservationError):
            PaneRenderObservation(
                readable=False,
                style_provenance=STYLE_PROVENANCE_UNKNOWN,
                reason=RENDER_REASON_OK,
            )

    def test_unknown_style_provenance_rejected(self) -> None:
        with self.assertRaises(PaneRenderObservationError):
            PaneRenderObservation(readable=False, style_provenance="bright", reason=RENDER_REASON_ANSI_ABSENT)

    def test_unknown_cursor_relation_rejected(self) -> None:
        with self.assertRaises(PaneRenderObservationError):
            PaneRenderObservation(
                readable=False, cursor_relation="floating", reason=RENDER_REASON_ANSI_ABSENT
            )

    def test_unknown_reason_rejected(self) -> None:
        with self.assertRaises(PaneRenderObservationError):
            PaneRenderObservation(readable=False, reason="mysterious")

    def test_non_bool_readable_rejected(self) -> None:
        with self.assertRaises(PaneRenderObservationError):
            PaneRenderObservation(readable="yes", reason=RENDER_REASON_ANSI_ABSENT)


class RedactionTest(unittest.TestCase):
    def test_record_is_closed_enums_and_bools_only(self) -> None:
        obs = PaneRenderObservation.classified(STYLE_PROVENANCE_DIM)
        record = obs.to_record()
        self.assertEqual(
            {"readable", "style_provenance", "cursor_relation", "reason", "prompt_present"},
            set(record),
        )
        # No body / hash / length / excerpt / ansi field can appear.
        for key in ("body", "content", "text", "ansi", "hash", "length", "excerpt"):
            self.assertNotIn(key, record)


if __name__ == "__main__":
    unittest.main()
