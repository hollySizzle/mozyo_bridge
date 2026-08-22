"""Stall escalation note body tests (Redmine #15855).

The prose half of the durable escalation record. The properties under test are the three
the module is responsible for: it carries no pane text, it claims an observation rather
than a conclusion, and it states the operator policy that produced it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_escalation_note import (  # noqa: E501
    STALL_ESCALATION_GATE,
    STALL_ESCALATION_REASON,
    render_escalation_body,
    render_policy_id,
)


def _body(**overrides):
    base = dict(
        issue="15855",
        slot_label="wsA/issue_15855_stall_wiring/claude",
        generation="g1",
        target="w1V:pK",
        provider_id="claude",
        stall_class="content_refusal",
        prescription="context_reset_reinjection",
        consecutive=2,
        first_observed_at="2026-08-22T09:00:00+00:00",
        last_observed_at="2026-08-22T09:05:00+00:00",
        policy_id="cadence=300s;threshold=2;source=portable_default",
        idempotency_key="stallesc1_abc123",
    )
    base.update(overrides)
    return render_escalation_body(**base)


class GateTokenTest(unittest.TestCase):
    def test_the_gate_is_an_existing_callback_required_kind(self) -> None:
        # No new gate token: `blocked` already means "wake the coordinator to read this,
        # it authorizes nothing", which is exactly this rail's semantics.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            GATE_BEARING_KINDS,
        )

        self.assertIn(STALL_ESCALATION_GATE, GATE_BEARING_KINDS)

    def test_the_heading_uses_the_canonical_gate_literal(self) -> None:
        self.assertTrue(_body().startswith(f"## Gate: {STALL_ESCALATION_GATE}"))

    def test_the_reason_token_is_fixed(self) -> None:
        # A consumer filtering "which blocked journals came from the stall watcher" must
        # not have to match on wording.
        self.assertIn(f"- reason: {STALL_ESCALATION_REASON}", _body())


class RequiredFieldsTest(unittest.TestCase):
    def test_every_required_field_is_rendered(self) -> None:
        body = _body(matched_id="sig-7", evidence_tier="rendered_confirmed")
        for fragment in (
            "- issue: 15855",
            "- slot: wsA/issue_15855_stall_wiring/claude",
            "- generation: g1",
            "- provider_id: claude",
            "- stall_class: content_refusal",
            "- prescription: context_reset_reinjection",
            "- consecutive_detections: 2",
            "- first_observed_at: 2026-08-22T09:00:00+00:00",
            "- last_observed_at: 2026-08-22T09:05:00+00:00",
            "- matched_id: sig-7",
            "- evidence_tier: rendered_confirmed",
            "- policy: cadence=300s;threshold=2;source=portable_default",
            "- idempotency_key: stallesc1_abc123",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, body)

    def test_the_transient_locator_is_labelled_as_evidence(self) -> None:
        # It is not the identity, and a reader must not treat it as one.
        self.assertIn("- last_seen_target: w1V:pK (transient locator; evidence only)", _body())

    def test_optional_fields_are_omitted_rather_than_rendered_blank(self) -> None:
        body = _body(generation="", target="", provider_id="", matched_id="", evidence_tier="")
        for absent in ("- generation:", "- last_seen_target:", "- provider_id:",
                       "- matched_id:", "- evidence_tier:"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, body)
        # The required ones survive.
        self.assertIn("- stall_class: content_refusal", body)


class ClaimBoundaryTest(unittest.TestCase):
    def test_the_prescription_is_marked_present_only(self) -> None:
        self.assertIn("posture: present_only", _body())
        self.assertIn("recommended to a human, not applied", _body())

    def test_the_note_states_that_no_action_was_taken(self) -> None:
        body = _body()
        self.assertIn("The watcher took no action", body)
        for verb in ("type", "press Enter", "reset a session", "relaunch"):
            with self.subTest(verb=verb):
                self.assertIn(verb, body)

    def test_the_note_claims_neither_death_nor_completion(self) -> None:
        # ADR-0014: the upper layer recovers facts and never guesses completion.
        body = _body()
        self.assertIn("asserts nothing about whether the unit is dead", body)
        self.assertIn("whether its work is complete", body)


class HygieneTest(unittest.TestCase):
    def test_there_is_no_parameter_through_which_pane_text_could_enter(self) -> None:
        import inspect

        params = set(inspect.signature(render_escalation_body).parameters)
        for forbidden in ("screen", "content", "pane", "visible", "text", "evidence"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, params)

    def test_the_note_says_so_explicitly(self) -> None:
        self.assertIn("No pane content is carried in this record", _body())


class PolicyIdTest(unittest.TestCase):
    def test_policy_id_records_values_and_where_they_came_from(self) -> None:
        # "N consecutive" means nothing to a later reader without the N, and a configured
        # cadence must be distinguishable from a shipped one.
        self.assertEqual(
            render_policy_id(cadence_seconds=300, threshold=2, source="portable_default"),
            "cadence=300s;threshold=2;source=portable_default",
        )
        self.assertEqual(
            render_policy_id(cadence_seconds=600, threshold=3, source="workspace_config"),
            "cadence=600s;threshold=3;source=workspace_config",
        )

    def test_policy_id_normalizes_numeric_input(self) -> None:
        self.assertEqual(
            render_policy_id(cadence_seconds=300.0, threshold=2.0, source="x"),
            "cadence=300s;threshold=2;source=x",
        )


class ExtraNotesTest(unittest.TestCase):
    def test_extra_notes_are_appended_as_fields(self) -> None:
        body = _body(extra_notes=["attempts: 3", "last_reason: write_optin_unset"])
        self.assertIn("- attempts: 3", body)
        self.assertIn("- last_reason: write_optin_unset", body)

    def test_blank_extra_notes_are_dropped(self) -> None:
        body = _body(extra_notes=["", None, "kept"])
        self.assertIn("- kept", body)
        self.assertNotIn("- \n", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
