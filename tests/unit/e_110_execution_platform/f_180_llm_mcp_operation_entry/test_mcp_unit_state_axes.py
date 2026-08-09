"""Unit state axis-separation specifications (Redmine #15162 / #15163).

These are the tests for the defect this User Story was opened over (#15151
j#101743): "the Redmine journal has not moved" plus "the worker is mid-implementation"
were folded into one ``blocked``. Both inputs were absences.

So the acceptance is asserted as *refusals*, not as features:

- Redmine-not-updated + worker-implementing does not become ``blocked``;
- an unknown runtime does not fabricate a workflow state, and an unknown workflow
  does not fabricate a runtime state;
- ``blocked`` is never produced without an authoritative blocker source, a reason,
  and a resume condition;
- every reported field carries ``source`` / ``observed_at`` / ``freshness``;
- an unread source is ``unknown`` and an unlanded dispatch is ``unconfirmed`` —
  neither is a state.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.runtime_observation import (  # noqa: E402,E501
    FRESHNESS_EXPIRED,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    READABILITY_READABLE,
    READABILITY_UNREADABLE,
    SOURCE_HERDR,
    SOURCE_REDMINE,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.unit_state_tool import (  # noqa: E402,E501
    UnitFacts,
    compose_unit_state,
    run_unit_state,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.application.read_plan_tools import (  # noqa: E402,E501
    ReadPlanContext,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_selector import (  # noqa: E402,E501
    UnitRecord,
)
from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_state import (  # noqa: E402,E501
    AXES,
    DELIVERY_BLOCKER_SOURCES,
    FORBIDDEN_INFERENCE_BASES,
    UNDERIVABLE_STATES,
    VALUE_UNCONFIRMED,
    VALUE_UNKNOWN,
    WORKFLOW_BLOCKER_SOURCES,
    BlockedClaim,
    ObservedField,
    admit_blocked,
    derive_health,
    worst_freshness,
)

UNIT = UnitRecord(
    workspace_id="mzb1-giken-3800",
    lane_id="issue_15151",
    project_id="giken-3800-mozyo-bridge",
    roles=("gateway", "worker"),
)

GOOD_CLAIM = BlockedClaim(
    blocker_source=SOURCE_REDMINE,
    reason="dependency #15149 is not closed",
    resume_condition="close #15149 and re-dispatch j#102124",
    durable_anchor="#15151 j#102123",
    freshness=FRESHNESS_FRESH,
)


def axis_fields(axis_payload: dict) -> list:
    """Every ``ObservedField`` payload in one axis payload."""
    found = []
    for value in axis_payload.values():
        if isinstance(value, dict) and "value" in value:
            found.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and isinstance(item.get("observation"), dict):
                    found.append(item["observation"])
    return found


class ProvenanceTests(unittest.TestCase):
    def test_every_reported_field_carries_source_observed_at_and_freshness(self) -> None:
        facts = UnitFacts(
            workflow_state="review_waiting",
            issue_status="open",
            latest_gate="review_request",
            latest_journal="102124",
            workflow_readable=True,
            workflow_observed_at="2026-08-10T00:00:00Z",
            workflow_freshness=FRESHNESS_FRESH,
            runtime_backend="busy",
            runtime_readable=True,
            runtime_observed_at="2026-08-10T00:00:05Z",
            runtime_freshness=FRESHNESS_FRESH,
            delivery_outcome="landed",
            delivery_anomaly="none",
            delivery_anomaly_stale=False,
            delivery_readable=True,
            delivery_observed_at="2026-08-10T00:00:07Z",
            delivery_freshness=FRESHNESS_FRESH,
        )
        payload = compose_unit_state(UNIT, facts).as_payload()
        for axis in ("workflow", "runtime", "delivery", "health"):
            fields = axis_fields(payload[axis])
            self.assertTrue(fields, axis)
            for observed in fields:
                self.assertIn("source", observed)
                self.assertIn("observed_at", observed)
                self.assertIn("freshness", observed)

    def test_a_bare_scalar_state_cannot_be_represented(self) -> None:
        """There is no path that reports a value without provenance."""
        payload = compose_unit_state(UNIT, UnitFacts()).as_payload()
        self.assertIsInstance(payload["workflow"]["state"], dict)
        self.assertEqual(payload["workflow"]["state"]["value"], VALUE_UNKNOWN)

    def test_the_four_axes_are_reported_side_by_side(self) -> None:
        payload = compose_unit_state(UNIT, UnitFacts()).as_payload()
        for axis in AXES:
            self.assertIn(axis, payload)

    def test_axes_can_be_narrowed_without_affecting_the_others(self) -> None:
        facts = UnitFacts(
            workflow_state="idle", workflow_readable=True, workflow_freshness=FRESHNESS_FRESH
        )
        report = compose_unit_state(UNIT, facts, axes=["workflow"])
        self.assertEqual(report.workflow.state.value, "idle")
        self.assertEqual(report.runtime.backend.value, VALUE_UNKNOWN)


class BlockedAdmissionTests(unittest.TestCase):
    def test_blocked_is_withheld_without_a_blocker_record(self) -> None:
        facts = UnitFacts(
            workflow_state="blocked",
            workflow_readable=True,
            workflow_freshness=FRESHNESS_FRESH,
        )
        report = compose_unit_state(UNIT, facts)
        self.assertEqual(report.workflow.state.value, VALUE_UNKNOWN)
        self.assertIsNone(report.workflow.blocked)
        self.assertIn("withheld", report.workflow.state.note or "")

    def test_blocked_is_reported_with_a_complete_authoritative_claim(self) -> None:
        facts = UnitFacts(
            workflow_state="blocked",
            workflow_readable=True,
            workflow_freshness=FRESHNESS_FRESH,
            workflow_blocked=GOOD_CLAIM,
        )
        report = compose_unit_state(UNIT, facts)
        self.assertEqual(report.workflow.state.value, "blocked")
        claim = report.workflow.blocked
        self.assertIsNotNone(claim)
        self.assertTrue(claim.reason)
        self.assertTrue(claim.resume_condition)
        self.assertTrue(claim.durable_anchor)

    def test_each_missing_claim_part_alone_defeats_admission(self) -> None:
        import dataclasses

        for missing in ("reason", "resume_condition", "durable_anchor"):
            partial = dataclasses.replace(GOOD_CLAIM, **{missing: ""})
            self.assertIsNone(
                admit_blocked(partial, authoritative_sources=WORKFLOW_BLOCKER_SOURCES),
                missing,
            )

    def test_a_whitespace_only_claim_part_is_not_a_part(self) -> None:
        import dataclasses

        blank = dataclasses.replace(GOOD_CLAIM, reason="   ")
        self.assertIsNone(
            admit_blocked(blank, authoritative_sources=WORKFLOW_BLOCKER_SOURCES)
        )

    def test_a_runtime_source_cannot_declare_the_workflow_blocked(self) -> None:
        import dataclasses

        runtime_claim = dataclasses.replace(GOOD_CLAIM, blocker_source=SOURCE_HERDR)
        self.assertIsNone(
            admit_blocked(runtime_claim, authoritative_sources=WORKFLOW_BLOCKER_SOURCES)
        )

    def test_only_the_durable_record_may_declare_the_workflow_blocked(self) -> None:
        self.assertEqual(WORKFLOW_BLOCKER_SOURCES, frozenset({SOURCE_REDMINE}))

    def test_the_delivery_axis_admits_its_own_ledger_as_a_blocker_source(self) -> None:
        self.assertIn(SOURCE_HERDR, DELIVERY_BLOCKER_SOURCES)


class NoInferenceFromAbsenceTests(unittest.TestCase):
    def test_the_prohibited_inference_bases_are_pinned(self) -> None:
        for absence in (
            "journal_not_updated",
            "pane_text",
            "stdout_silence",
            "turn_ended",
        ):
            self.assertIn(absence, FORBIDDEN_INFERENCE_BASES)
        for state in ("blocked", "idle", "completed"):
            self.assertIn(state, UNDERIVABLE_STATES)

    def test_unmoved_journal_plus_implementing_worker_is_not_blocked(self) -> None:
        """The exact #15151 j#101743 correction, as an executable assertion."""
        facts = UnitFacts(
            # Redmine readable, but no new gate recorded: the journal has not moved.
            workflow_state="unknown",
            issue_status="open",
            latest_gate="implementation_request",
            latest_journal="102124",
            workflow_readable=True,
            workflow_freshness=FRESHNESS_FRESH,
            # The worker is mid-implementation: busy, and nothing has come back.
            runtime_backend="busy",
            runtime_readable=True,
            runtime_freshness=FRESHNESS_FRESH,
            delivery_outcome="",
            delivery_readable=True,
            delivery_freshness=FRESHNESS_FRESH,
        )
        report = compose_unit_state(UNIT, facts)
        self.assertNotEqual(report.workflow.state.value, "blocked")
        self.assertIsNone(report.workflow.blocked)
        # And the two facts stay visible and separate rather than being fused.
        self.assertEqual(report.workflow.latest_gate.value, "implementation_request")
        self.assertEqual(report.runtime.backend.value, "busy")

    def test_a_turn_that_ended_is_not_completion(self) -> None:
        facts = UnitFacts(
            workflow_readable=False,
            runtime_backend="turn_ended",
            runtime_readable=True,
            runtime_freshness=FRESHNESS_FRESH,
        )
        report = compose_unit_state(UNIT, facts)
        self.assertEqual(report.workflow.state.value, VALUE_UNKNOWN)
        self.assertEqual(report.runtime.backend.value, "turn_ended")
        self.assertNotIn("completed", str(report.workflow.as_payload()))

    def test_an_unknown_runtime_does_not_fabricate_a_workflow_state(self) -> None:
        facts = UnitFacts(
            workflow_state="review_waiting",
            workflow_readable=True,
            workflow_freshness=FRESHNESS_FRESH,
            runtime_readable=False,
        )
        report = compose_unit_state(UNIT, facts)
        self.assertEqual(report.workflow.state.value, "review_waiting")
        self.assertEqual(report.runtime.backend.value, VALUE_UNKNOWN)
        self.assertEqual(report.runtime.backend.readability, READABILITY_UNREADABLE)

    def test_an_unreadable_redmine_does_not_fabricate_a_runtime_state(self) -> None:
        facts = UnitFacts(
            workflow_readable=False,
            runtime_backend="awaiting_input",
            runtime_readable=True,
            runtime_freshness=FRESHNESS_FRESH,
        )
        report = compose_unit_state(UNIT, facts)
        self.assertEqual(report.workflow.state.value, VALUE_UNKNOWN)
        self.assertEqual(report.runtime.backend.value, "awaiting_input")

    def test_an_unread_field_never_carries_a_freshness_or_timestamp(self) -> None:
        facts = UnitFacts(
            workflow_state="idle",
            workflow_readable=False,
            workflow_observed_at="2026-08-10T00:00:00Z",
            workflow_freshness=FRESHNESS_FRESH,
        )
        state = compose_unit_state(UNIT, facts).workflow.state
        self.assertEqual(state.value, VALUE_UNKNOWN)
        self.assertIsNone(state.observed_at)
        self.assertEqual(state.freshness, FRESHNESS_UNKNOWN)


class DeliveryUnconfirmedTests(unittest.TestCase):
    def test_a_dispatch_with_no_observed_landing_is_unconfirmed_not_failed(self) -> None:
        facts = UnitFacts(
            delivery_outcome="",
            delivery_readable=True,
            delivery_freshness=FRESHNESS_FRESH,
        )
        outcome = compose_unit_state(UNIT, facts).delivery.outcome
        self.assertEqual(outcome.value, VALUE_UNCONFIRMED)
        self.assertIn("never observed", outcome.note or "")

    def test_an_unread_delivery_source_is_unknown_not_unconfirmed(self) -> None:
        """'We did not look' is a different fact from 'we looked and did not see'."""
        outcome = compose_unit_state(UNIT, UnitFacts(delivery_readable=False)).delivery.outcome
        self.assertEqual(outcome.value, VALUE_UNKNOWN)

    def test_the_delivery_axis_states_it_is_not_completion(self) -> None:
        payload = compose_unit_state(UNIT, UnitFacts()).delivery.as_payload()
        self.assertIn("not imply", payload["authority_note"])

    def test_the_runtime_axis_states_it_is_not_workflow_truth(self) -> None:
        payload = compose_unit_state(UNIT, UnitFacts()).runtime.as_payload()
        self.assertIn("never workflow truth", payload["authority_note"])


class FreshnessAndHealthTests(unittest.TestCase):
    def test_expired_freshness_is_reported_and_degrades_health(self) -> None:
        facts = UnitFacts(
            workflow_state="review_waiting",
            workflow_readable=True,
            workflow_observed_at="2026-08-01T00:00:00Z",
            workflow_freshness=FRESHNESS_EXPIRED,
        )
        report = compose_unit_state(UNIT, facts)
        self.assertEqual(report.workflow.state.freshness, FRESHNESS_EXPIRED)
        self.assertTrue(report.health.degraded)
        self.assertFalse(report.workflow.state.is_current)

    def test_a_readable_field_with_no_timestamp_is_not_claimed_fresh(self) -> None:
        """An age class with no age is not a claim anyone can check."""
        facts = UnitFacts(
            workflow_state="review_waiting",
            workflow_readable=True,
            workflow_observed_at=None,
            workflow_freshness=FRESHNESS_FRESH,
        )
        state = compose_unit_state(UNIT, facts).workflow.state
        # The VALUE still stands — the durable record was readable and said this.
        self.assertEqual(state.value, "review_waiting")
        # The currency claim does not.
        self.assertEqual(state.freshness, FRESHNESS_UNKNOWN)
        self.assertFalse(state.is_current)

    def test_a_delivery_outcome_with_no_timestamp_is_not_claimed_fresh(self) -> None:
        facts = UnitFacts(
            delivery_outcome="landed",
            delivery_readable=True,
            delivery_freshness=FRESHNESS_FRESH,
        )
        outcome = compose_unit_state(UNIT, facts).delivery.outcome
        self.assertEqual(outcome.value, "landed")
        self.assertEqual(outcome.freshness, FRESHNESS_UNKNOWN)

    def test_health_is_not_degraded_only_when_everything_is_fresh_and_readable(self) -> None:
        fresh = ObservedField.observed(
            "x", source=SOURCE_REDMINE, observed_at="t", freshness=FRESHNESS_FRESH
        )
        self.assertFalse(derive_health((fresh, fresh)).degraded)
        stale = ObservedField.observed(
            "x", source=SOURCE_REDMINE, observed_at="t", freshness=FRESHNESS_STALE
        )
        self.assertTrue(derive_health((fresh, stale)).degraded)

    def test_no_fields_at_all_is_degraded_not_healthy(self) -> None:
        self.assertTrue(derive_health(()).degraded)
        self.assertEqual(derive_health(()).freshness, FRESHNESS_UNKNOWN)

    def test_worst_freshness_ranks_unknown_below_expired(self) -> None:
        def f(freshness: str) -> ObservedField:
            return ObservedField(
                value="x",
                source=SOURCE_REDMINE,
                observed_at="t",
                freshness=freshness,
                readability=READABILITY_READABLE,
            )

        self.assertEqual(
            worst_freshness((f(FRESHNESS_FRESH), f(FRESHNESS_EXPIRED))),
            FRESHNESS_EXPIRED,
        )
        self.assertEqual(
            worst_freshness((f(FRESHNESS_EXPIRED), f(FRESHNESS_UNKNOWN))),
            FRESHNESS_UNKNOWN,
        )

    def test_source_health_notes_are_carried_so_empty_is_never_read_as_fine(self) -> None:
        facts = UnitFacts(notes=("Redmine source unavailable (TimeoutError)",))
        report = compose_unit_state(UNIT, facts)
        self.assertTrue(report.health.degraded)
        self.assertIn("Redmine source unavailable (TimeoutError)", report.health.notes)


class ToolResultTests(unittest.TestCase):
    class _Source:
        def __init__(self, units, facts, scope=("mzb1-giken-3800",)) -> None:
            self._units, self._facts, self._scope = units, facts, scope

        def unit_index(self):
            return self._units

        def authorized_workspace_ids(self):
            return self._scope

        def unit_facts(self, unit):
            return self._facts

    def _context(self) -> ReadPlanContext:
        return ReadPlanContext(repo_root=Path("/nonexistent"))

    def test_a_resolved_unit_returns_a_read_only_report(self) -> None:
        outcome = run_unit_state(
            {
                "unit": {
                    "workspace_id": UNIT.workspace_id,
                    "lane_id": UNIT.lane_id,
                    "project_id": UNIT.project_id,
                }
            },
            self._context(),
            source=self._Source((UNIT,), UnitFacts()),
        )
        self.assertFalse(outcome.is_error)
        self.assertTrue(outcome.payload["read_only"])
        self.assertEqual(outcome.payload["unit"]["unit_id"], UNIT.unit_id())

    def test_a_refused_selector_is_a_structured_tool_error(self) -> None:
        outcome = run_unit_state(
            {
                "unit": {
                    "workspace_id": "elsewhere",
                    "lane_id": "l",
                    "project_id": "p",
                }
            },
            self._context(),
            source=self._Source((UNIT,), UnitFacts()),
        )
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.payload["error"], "unit_selector")
        self.assertEqual(outcome.payload["reason"], "unknown")

    def test_the_report_returns_no_routing_target_or_permission(self) -> None:
        outcome = run_unit_state(
            {
                "unit": {
                    "workspace_id": UNIT.workspace_id,
                    "lane_id": UNIT.lane_id,
                    "project_id": UNIT.project_id,
                }
            },
            self._context(),
            source=self._Source((UNIT,), UnitFacts(runtime_backend="busy")),
        )
        rendered = repr(outcome.payload).lower()
        for forbidden in ("pane_id", "%", "tmux", "worktree", "api_key", "token"):
            self.assertNotIn(forbidden, rendered, forbidden)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
