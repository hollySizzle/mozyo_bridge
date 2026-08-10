"""Generation-mismatch disposition decision tests (Redmine #15193).

This is the rail that breaks the #15110 / #15140 / #15195 deadlock, so its refusals are the
safety boundary: every state that is ambiguous, foreign, actively working, or unreadable must
refuse WITHOUT rendering a template, and a rendered template must always state — in words and
in a machine-checkable token — what becomes of the pending input.
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.generation_mismatch_disposition import (  # noqa: E501
    DISPOSITION_AGENT_NOT_SETTLED,
    DISPOSITION_AGENT_WORKING,
    DISPOSITION_ATTESTATION_UNREADABLE,
    DISPOSITION_AXES_UNATTRIBUTED,
    DISPOSITION_COMPOSER_GENERATION_UNAVAILABLE,
    DISPOSITION_COMPOSER_UNREADABLE,
    DISPOSITION_DUPLICATE_RECEIVER,
    DISPOSITION_INVENTORY_UNREADABLE,
    DISPOSITION_KNOWN_MARKER_REQUIRES_Q_ENTER,
    DISPOSITION_LIFECYCLE_ABSENT,
    DISPOSITION_LIFECYCLE_PINS_INVALID,
    DISPOSITION_LIFECYCLE_UNREADABLE,
    DISPOSITION_NOT_MISMATCH_WITH_PENDING,
    DISPOSITION_READY,
    DISPOSITION_REASONS,
    DISPOSITION_RECEIVER_ABSENT,
    DISPOSITION_REVISION_UNREADABLE,
    DISPOSITION_WORKSPACE_UNRESOLVED,
    DRIFT_AGENT_REVISION,
    DRIFT_ASSIGNED_NAME,
    DRIFT_GENERATION_AXES,
    DRIFT_LANE_GENERATION,
    DRIFT_LIFECYCLE_REVISION,
    DRIFT_LOCATOR,
    DRIFT_PENDING_IDENTITY,
    PENDING_EFFECT_DISCARDED_ON_REPLACE,
    PENDING_EFFECT_PRESERVED,
    PENDING_IDENTITY_UNBOUND,
    DispositionFacts,
    decide_disposition_readiness,
    disposition_command,
    observed_facts_match,
    pending_identity,
    render_disposition_template,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    AGENT_WORKING,
    CORRELATED_KNOWN_MARKER,
    GENERATION_MISMATCH,
    NO_PENDING_COMPOSER,
    UNCORRELATED,
    PendingComposerClassification,
    PendingComposerSignal,
    classify_pending_composer,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine_disposition import (  # noqa: E501
    disposition_agent_state_settled,
)

MARKER = "[mozyo:handoff:source=redmine:issue=15193:journal=102219:kind=implementation_request:to=claude]"
PROVIDER_GENERATION = "opaque-provider-draft-generation-1"
PENDING_ID = pending_identity(
    pending_observed=True,
    correlated_marker_ids=(),
    provider_generation=PROVIDER_GENERATION,
)


def _facts(**kw) -> DispositionFacts:
    """The exact #15193 shape: a readable, attested, stopped, generation-mismatched receiver."""
    base = dict(
        issue="15193",
        lane="issue_15193_generation_mismatch_disposition",
        role="claude",
        workspace_id="ws-1",
        assigned_name="mozyo-ws-1-issue_15193-claude",
        locator="w1B:p18",
        agent_revision=4,
        lane_generation=1,
        lifecycle_revision=7,
        attested_at="2026-08-10T07:00:00+00:00",
        action_generation="quarantine-abc123",
        generation_axes=("pair",),
        pending_identity=PENDING_ID,
        pending_effect=PENDING_EFFECT_DISCARDED_ON_REPLACE,
        observed_at="2026-08-10T07:30:00+00:00",
    )
    base.update(kw)
    return DispositionFacts(**base)


def _mismatch_with_pending(**kw) -> PendingComposerClassification:
    base = dict(
        label=GENERATION_MISMATCH, pending_observed=True, generation_axes=("pair",)
    )
    base.update(kw)
    return PendingComposerClassification(**base)


def _decide(**kw) -> str:
    base = dict(
        facts=_facts(),
        classification=_mismatch_with_pending(),
        receiver_present=True,
        inventory_readable=True,
        composer_readable=True,
        agent_working=False,
        agent_settled=True,
        duplicate_receiver=False,
    )
    base.update(kw)
    return decide_disposition_readiness(**base)


class ReadinessTest(unittest.TestCase):
    def test_shared_settled_state_predicate_is_closed(self) -> None:
        for state in ("awaiting_input", "turn_ended", " AWAITING_INPUT "):
            with self.subTest(state=state):
                self.assertTrue(disposition_agent_state_settled(state))
        for state in ("blocked", "unknown", "", "novel"):
            with self.subTest(state=state):
                self.assertFalse(disposition_agent_state_settled(state))

    def test_the_exact_15193_shape_is_ready(self) -> None:
        # #15110 j#102068 / #15140 j#102064 / #15195 j#102193 all reduce to this state, and
        # it is the ONLY state this rail admits.
        self.assertEqual(_decide(), DISPOSITION_READY)

    def test_every_reason_is_in_the_closed_vocabulary(self) -> None:
        for reason in (
            _decide(),
            _decide(facts=_facts(workspace_id="")),
            _decide(inventory_readable=False),
            _decide(duplicate_receiver=True),
            _decide(receiver_present=False),
            _decide(composer_readable=False),
            _decide(facts=_facts(agent_revision=-1)),
            _decide(facts=_facts(attested_at="")),
            _decide(agent_working=True),
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, DISPOSITION_REASONS)


class ZeroMutationRefusalTest(unittest.TestCase):
    """Acceptance: ambiguous / foreign / active work / unreadable authority -> zero mutation."""

    def test_unresolved_workspace_refuses(self) -> None:
        self.assertEqual(_decide(facts=_facts(workspace_id="")), DISPOSITION_WORKSPACE_UNRESOLVED)

    def test_unreadable_inventory_refuses_and_outranks_everything(self) -> None:
        self.assertEqual(
            _decide(inventory_readable=False, duplicate_receiver=True),
            DISPOSITION_INVENTORY_UNREADABLE,
        )

    def test_duplicate_receiver_refuses_rather_than_picking_one(self) -> None:
        self.assertEqual(_decide(duplicate_receiver=True), DISPOSITION_DUPLICATE_RECEIVER)

    def test_absent_receiver_refuses(self) -> None:
        self.assertEqual(_decide(receiver_present=False), DISPOSITION_RECEIVER_ABSENT)

    def test_unproven_presence_refuses_because_a_discard_needs_positive_evidence(self) -> None:
        # `None` means the inventory could not prove presence either way. Stricter than the
        # sibling quarantine approval on purpose: this rail also authorizes discarding an
        # unsent input, and an input may only be discarded over a receiver we positively
        # observed — not over one the inventory merely failed to disprove.
        self.assertEqual(_decide(receiver_present=None), DISPOSITION_RECEIVER_ABSENT)

    def test_unreadable_composer_refuses_and_never_assumes_empty(self) -> None:
        self.assertEqual(_decide(composer_readable=False), DISPOSITION_COMPOSER_UNREADABLE)

    def test_unknown_pending_fact_refuses_even_when_the_probe_reported_readable(self) -> None:
        # A readable probe that could not decide the pending fact is still unprovable.
        self.assertEqual(
            _decide(classification=_mismatch_with_pending(pending_observed=None)),
            DISPOSITION_COMPOSER_UNREADABLE,
        )

    def test_unreadable_revision_refuses(self) -> None:
        self.assertEqual(_decide(facts=_facts(agent_revision=-1)), DISPOSITION_REVISION_UNREADABLE)

    def test_unreadable_attestation_refuses(self) -> None:
        self.assertEqual(_decide(facts=_facts(attested_at="")), DISPOSITION_ATTESTATION_UNREADABLE)

    def test_non_settled_state_refuses(self) -> None:
        self.assertEqual(_decide(agent_settled=False), DISPOSITION_AGENT_NOT_SETTLED)

    def test_unbound_uncorrelated_generation_refuses(self) -> None:
        self.assertEqual(
            _decide(facts=_facts(pending_identity=PENDING_IDENTITY_UNBOUND)),
            DISPOSITION_COMPOSER_GENERATION_UNAVAILABLE,
        )

    def test_unreadable_lifecycle_refuses(self) -> None:
        self.assertEqual(
            _decide(lifecycle_reason=DISPOSITION_LIFECYCLE_UNREADABLE),
            DISPOSITION_LIFECYCLE_UNREADABLE,
        )

    def test_absent_lifecycle_refuses(self) -> None:
        self.assertEqual(
            _decide(lifecycle_reason=DISPOSITION_LIFECYCLE_ABSENT),
            DISPOSITION_LIFECYCLE_ABSENT,
        )

    def test_non_positive_lifecycle_pins_refuse(self) -> None:
        for changes in ({"lane_generation": 0}, {"lifecycle_revision": -1}):
            with self.subTest(changes=changes):
                self.assertEqual(
                    _decide(facts=_facts(**changes)),
                    DISPOSITION_LIFECYCLE_PINS_INVALID,
                )

    def test_working_agent_refuses_regardless_of_everything_else(self) -> None:
        self.assertEqual(_decide(agent_working=True), DISPOSITION_AGENT_WORKING)

    def test_working_classification_is_not_the_disposition_shape(self) -> None:
        self.assertEqual(
            _decide(classification=PendingComposerClassification(AGENT_WORKING, pending_observed=True)),
            DISPOSITION_NOT_MISMATCH_WITH_PENDING,
        )


class NeverDestroyKnownInputTest(unittest.TestCase):
    def test_known_delivery_marker_routes_to_q_enter_not_replacement(self) -> None:
        # A correlated marker is re-submittable; replacing the receiver would destroy a real
        # queued handoff. Same rule the quarantine approval already enforces.
        classification = PendingComposerClassification(
            CORRELATED_KNOWN_MARKER, correlated_marker_id=MARKER, pending_observed=True
        )
        self.assertEqual(
            _decide(classification=classification),
            DISPOSITION_KNOWN_MARKER_REQUIRES_Q_ENTER,
        )

    def test_known_marker_wins_even_when_the_generation_also_mismatched(self) -> None:
        classification = _mismatch_with_pending(correlated_marker_id=MARKER)
        self.assertEqual(
            _decide(classification=classification),
            DISPOSITION_KNOWN_MARKER_REQUIRES_Q_ENTER,
        )

    def test_real_classifier_preserves_known_marker_through_mismatch_precedence(self) -> None:
        classification = classify_pending_composer(
            PendingComposerSignal(
                inventory_readable=True,
                has_pending=True,
                agent_state="awaiting_input",
                identity_attested=True,
                generation_matches=False,
                correlated_marker_ids=(MARKER,),
                generation_axes=("pair",),
            )
        )
        self.assertEqual(classification.label, GENERATION_MISMATCH)
        self.assertTrue(classification.q_enter_recommended)
        self.assertEqual(
            _decide(classification=classification),
            DISPOSITION_KNOWN_MARKER_REQUIRES_Q_ENTER,
        )

    def test_real_classifier_never_admits_ambiguous_markers_to_disposition(self) -> None:
        classification = classify_pending_composer(
            PendingComposerSignal(
                inventory_readable=True,
                has_pending=True,
                agent_state="awaiting_input",
                identity_attested=True,
                generation_matches=False,
                correlated_marker_ids=(MARKER, "second-marker"),
                generation_axes=("pair",),
            )
        )
        self.assertFalse(classification.generation_mismatch_with_pending)
        self.assertNotEqual(_decide(classification=classification), DISPOSITION_READY)


class ScopeTest(unittest.TestCase):
    """This rail must not duplicate the canonical ones."""

    def test_matching_generation_is_not_this_rail(self) -> None:
        self.assertEqual(
            _decide(classification=PendingComposerClassification(UNCORRELATED, pending_observed=True)),
            DISPOSITION_NOT_MISMATCH_WITH_PENDING,
        )

    def test_empty_composer_is_not_this_rail(self) -> None:
        self.assertEqual(
            _decide(
                classification=PendingComposerClassification(
                    NO_PENDING_COMPOSER, pending_observed=False
                )
            ),
            DISPOSITION_NOT_MISMATCH_WITH_PENDING,
        )

    def test_unattributed_mismatch_refuses_rather_than_binding_an_unnamed_condition(self) -> None:
        self.assertEqual(
            _decide(
                facts=_facts(generation_axes=()),
                classification=_mismatch_with_pending(generation_axes=()),
            ),
            DISPOSITION_AXES_UNATTRIBUTED,
        )

    def test_missing_action_generation_refuses(self) -> None:
        self.assertEqual(_decide(facts=_facts(action_generation="")), DISPOSITION_AXES_UNATTRIBUTED)


class PendingEffectTest(unittest.TestCase):
    """Acceptance: the pending input is never silently discarded."""

    def test_template_states_the_discard_in_words(self) -> None:
        rendered = render_disposition_template(_facts())
        self.assertIn("未送信 composer input を破棄する", rendered)
        self.assertIn(f"pending_effect: `{PENDING_EFFECT_DISCARDED_ON_REPLACE}`", rendered)
        self.assertEqual(
            rendered.count(
                "[mozyo:workflow-event:gate="
                "generation_mismatch_disposition_owner_approval"
            ),
            1,
        )
        self.assertIn("approval_source=direct_owner", rendered)

    def test_template_states_preservation_when_nothing_is_discarded(self) -> None:
        rendered = render_disposition_template(_facts(pending_effect=PENDING_EFFECT_PRESERVED))
        self.assertIn("破棄しない", rendered)

    def test_template_binds_every_exact_token(self) -> None:
        facts = _facts()
        rendered = render_disposition_template(facts)
        for token in (
            facts.assigned_name,
            facts.locator,
            facts.action_generation,
            facts.attested_at,
            facts.pending_identity,
            str(facts.agent_revision),
            str(facts.lane_generation),
            str(facts.lifecycle_revision),
        ):
            with self.subTest(token=token):
                self.assertIn(token, rendered)

    def test_command_carries_the_disposition_only_flags(self) -> None:
        argv = disposition_command(_facts(), journal="102900")
        self.assertIn("--approved-generation-axes", argv)
        self.assertIn("--approved-pending-identity", argv)
        self.assertIn("--approved-pending-effect", argv)
        self.assertIn("--approved-lane-generation", argv)
        self.assertIn("--approved-lifecycle-revision", argv)
        self.assertEqual(argv[argv.index("--approved-lane-generation") + 1], "1")
        self.assertEqual(argv[argv.index("--approved-lifecycle-revision") + 1], "7")
        self.assertIn("--execute", argv)
        self.assertEqual(argv[argv.index("--journal") + 1], "102900")

    def test_journal_id_is_a_placeholder_never_fabricated(self) -> None:
        argv = disposition_command(_facts())
        self.assertEqual(argv[argv.index("--journal") + 1], "<approval-journal-id>")


class PendingIdentityTest(unittest.TestCase):
    def test_absent_and_unreadable_are_distinguishable(self) -> None:
        absent = pending_identity(pending_observed=False, correlated_marker_ids=())
        unreadable = pending_identity(pending_observed=None, correlated_marker_ids=())
        self.assertNotEqual(absent, unreadable)
        self.assertEqual(absent, "")
        self.assertEqual(unreadable, "unreadable")

    def test_marker_order_does_not_change_the_identity(self) -> None:
        a = pending_identity(pending_observed=True, correlated_marker_ids=(MARKER, "m2"))
        b = pending_identity(pending_observed=True, correlated_marker_ids=("m2", MARKER))
        self.assertEqual(a, b)

    def test_a_different_marker_set_is_a_different_input(self) -> None:
        a = pending_identity(pending_observed=True, correlated_marker_ids=(MARKER,))
        b = pending_identity(pending_observed=True, correlated_marker_ids=("m2",))
        self.assertNotEqual(a, b)

    def test_identity_carries_no_composer_body(self) -> None:
        # Derived only from the pending flag and ledger marker identities.
        token = pending_identity(pending_observed=True, correlated_marker_ids=())
        self.assertEqual(token, PENDING_IDENTITY_UNBOUND)

    def test_provider_generation_distinguishes_uncorrelated_drafts(self) -> None:
        first = pending_identity(
            pending_observed=True,
            correlated_marker_ids=(),
            provider_generation="opaque-1",
        )
        second = pending_identity(
            pending_observed=True,
            correlated_marker_ids=(),
            provider_generation="opaque-2",
        )
        self.assertTrue(first.startswith("pending:provider:"))
        self.assertNotEqual(first, second)


class ActionTimeRevalidationTest(unittest.TestCase):
    """Acceptance: exact-bind, re-verified at action time; different generation is refused."""

    def test_identical_observation_has_no_drift_so_reexecution_is_idempotent(self) -> None:
        self.assertEqual(observed_facts_match(_facts(), _facts()), ())

    def test_advanced_agent_revision_is_refused(self) -> None:
        self.assertEqual(
            observed_facts_match(_facts(), _facts(agent_revision=5)), (DRIFT_AGENT_REVISION,)
        )

    def test_advanced_lifecycle_revision_is_refused(self) -> None:
        self.assertEqual(
            observed_facts_match(_facts(), _facts(lifecycle_revision=8)),
            (DRIFT_LIFECYCLE_REVISION,),
        )

    def test_new_lane_generation_is_refused(self) -> None:
        self.assertEqual(
            observed_facts_match(_facts(), _facts(lane_generation=2)), (DRIFT_LANE_GENERATION,)
        )

    def test_recycled_locator_is_refused(self) -> None:
        self.assertEqual(observed_facts_match(_facts(), _facts(locator="w1B:p20")), (DRIFT_LOCATOR,))

    def test_foreign_assigned_name_is_refused(self) -> None:
        self.assertEqual(
            observed_facts_match(_facts(), _facts(assigned_name="mozyo-ws-1-issue_9999-claude")),
            (DRIFT_ASSIGNED_NAME,),
        )

    def test_healed_mismatch_axis_is_refused(self) -> None:
        # The owner approved a disposition over a `pair` mismatch. If the pair healed, the
        # approved condition no longer exists — the canonical quarantine rail applies now.
        self.assertEqual(
            observed_facts_match(_facts(), _facts(generation_axes=())), (DRIFT_GENERATION_AXES,)
        )

    def test_new_mismatch_axis_is_refused(self) -> None:
        self.assertEqual(
            observed_facts_match(_facts(), _facts(generation_axes=("pair", "identity"))),
            (DRIFT_GENERATION_AXES,),
        )

    def test_axis_reordering_is_not_drift(self) -> None:
        approved = _facts(generation_axes=("pair", "identity"))
        observed = _facts(generation_axes=("identity", "pair"))
        self.assertEqual(observed_facts_match(approved, observed), ())

    def test_a_different_pending_input_is_refused(self) -> None:
        # The whole point: discarding an input the owner never saw is the silent discard
        # this rail exists to prevent.
        self.assertEqual(
            observed_facts_match(_facts(), _facts(pending_identity="pending:markers:deadbeef")),
            (DRIFT_PENDING_IDENTITY,),
        )

    def test_a_vanished_pending_input_is_refused(self) -> None:
        self.assertEqual(
            observed_facts_match(_facts(), _facts(pending_identity="")), (DRIFT_PENDING_IDENTITY,)
        )

    def test_every_drifted_axis_is_reported_not_only_the_first(self) -> None:
        drift = observed_facts_match(
            _facts(), _facts(agent_revision=5, locator="w1B:p20", generation_axes=())
        )
        self.assertEqual(set(drift), {DRIFT_AGENT_REVISION, DRIFT_LOCATOR, DRIFT_GENERATION_AXES})

    def test_unpinned_lane_generation_is_drift(self) -> None:
        approved = _facts(lane_generation=-1)
        self.assertEqual(
            observed_facts_match(approved, _facts(lane_generation=3)),
            (DRIFT_LANE_GENERATION,),
        )


class BodyFenceTest(unittest.TestCase):
    def test_facts_carry_no_composer_body_field(self) -> None:
        fields = {f.name for f in dataclasses.fields(DispositionFacts)}
        self.assertEqual(
            fields,
            {
                "issue",
                "lane",
                "role",
                "workspace_id",
                "assigned_name",
                "locator",
                "agent_revision",
                "lane_generation",
                "lifecycle_revision",
                "attested_at",
                "action_generation",
                "generation_axes",
                "pending_identity",
                "pending_effect",
                "observed_at",
            },
        )

    def test_payload_exposes_tokens_only(self) -> None:
        payload = _facts().as_payload()
        self.assertEqual(set(payload), {f.name for f in dataclasses.fields(DispositionFacts)})


if __name__ == "__main__":
    unittest.main()
