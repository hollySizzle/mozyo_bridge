"""Classical acceptance oracle for the 3-window delegated coordinator smoke (Redmine #12547).

#12547 (parent #12499, Feature #12386 ``Delegated Coordinator / Nested Handoff``)
asks for *classical* hermetic tests that catch route / display / profile failures
**before** the expensive real-machine 3-window smoke (#12546) is ever run. The
acceptance source of truth and the classical-test obligations live in:

- ``vibes/docs/logics/delegated-coordinator-real-machine-acceptance.md``
  (``## Acceptance Criteria`` / ``## Failure Classification`` /
  ``## Classical Test Obligations``)
- ``vibes/docs/logics/delegated-coordinator-smoke-test-frame.md``
  (``## Classical tests へ落とすべき層``)

Per the #12547 start gate (j#64743) the runtime does **not** yet expose a
``delegate-launch-adopt`` / ``delegate-grandchild-dispatch`` actuator or an
acceptance classifier, so this module *pins the oracle* as minimal, pure,
self-contained scaffolding (explicitly allowed by j#64743: "tests may first pin
an oracle/classifier rather than full actuator behavior"). #12549 (resolver) and
#12550 (planner / actuator) must later conform to the vocabulary and precedence
fixed here; this oracle is the executable spec they are measured against.

The oracle is anchored to *shipped* code where the vocabulary already exists: the
role-profile chain it expects (``delegated_coordinator`` ->
``implementation_gateway`` -> ``implementation_worker``) is validated against the
real :mod:`mozyo_bridge.domain.role_profile` tokens and resolver, so the
hermetic oracle cannot silently drift from the role taxonomy the runtime sends.

Hermetic by construction: no live tmux, no Redmine reads/writes, no private pane
ids, no host paths, no cockpit composition. Fixtures use neutral placeholder
identifiers only.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.role_profile import (  # noqa: E402
    ROLE_COORDINATOR,
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_GATEWAY,
    ROLE_IMPLEMENTATION_WORKER,
    ROLE_PROFILE_SOURCE,
    ROLE_PROFILE_TOKENS,
    ROLE_PROFILE_VERSION,
    resolve_role_profile,
)

# ---------------------------------------------------------------------------
# Classification vocabulary (the oracle's fixed output language).
#
# These are the only verdicts the real-machine acceptance doc admits
# (``## Failure Classification``) plus ``PASS``. The classical tests gate the
# real smoke by guaranteeing none of the non-PASS scenarios below can be
# mislabelled ``PASS``.
# ---------------------------------------------------------------------------
CLASS_PASS = "PASS"
CLASS_FAILED_ACCEPTANCE = "failed_acceptance"
CLASS_INSUFFICIENT = "insufficient"
CLASS_CONTAMINATED = "contaminated"
CLASS_BLOCKED = "blocked"
CLASS_ENVIRONMENTAL = "environmental"

ACCEPTANCE_CLASSIFICATIONS = frozenset(
    {
        CLASS_PASS,
        CLASS_FAILED_ACCEPTANCE,
        CLASS_INSUFFICIENT,
        CLASS_CONTAMINATED,
        CLASS_BLOCKED,
        CLASS_ENVIRONMENTAL,
    }
)

# Test models (``## Test Models`` of the acceptance doc). Only ``autonomous_parent``
# (and the read-boundary check variant ``bounded_read``) can reach a full PASS;
# ``context_rich`` is a debugging model and never a full-acceptance PASS.
MODEL_AUTONOMOUS_PARENT = "autonomous_parent"
MODEL_BOUNDED_READ = "bounded_read"
MODEL_CONTEXT_RICH = "context_rich"

# Read surfaces the receiver may legitimately consult vs. surfaces that
# contaminate a bounded/autonomous run (``## Parent Prompt Boundary`` /
# ``### context-rich contamination``).
ALLOWED_READ_SURFACES = frozenset(
    {"parent_description", "child_project_config", "start_anchor"}
)
FORBIDDEN_READ_SURFACES = frozenset(
    {"parent_journals", "sibling_issue", "prior_smoke", "injected_route_context"}
)

# Hints that must never be pre-loaded into the parent prompt. Any of these makes
# the run harness-assisted / context-rich rather than autonomous
# (``## Parent Prompt Boundary`` "渡してはいけないもの").
FORBIDDEN_PARENT_PROMPT_HINTS = frozenset(
    {
        "expected_child_route",
        "child_pane_id",
        "grandchild_pane_id",
        "worktree_path",
        "expected_answer",
        "prior_smoke_verdict",
        "test_oracle_token",
        "precomputed_project_config_summary",
    }
)

# The downstream handoff role chain a full 3-window route must realize. Pinned to
# the *real* shipped role tokens so this oracle cannot drift from the runtime.
EXPECTED_ROLE_PROFILE_CHAIN = (
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_GATEWAY,
    ROLE_IMPLEMENTATION_WORKER,
)

# Hard delegation depth ceiling (``spec-delegation-policy-project-config``:
# hard ceiling 2 == 3 audit-visible layers; depth 0 parent, 1 delegated, 2
# grandchild). A projection deeper than this is invalid, not a silent PASS.
MAX_DELEGATION_DEPTH = 2


@dataclass(frozen=True)
class AcceptanceScenario:
    """A single 3-window acceptance run attempt, as the oracle sees it.

    Every field is a durable-record-safe token (no pane ids, no paths). The
    baseline produced by :func:`clean_autonomous_pass_scenario` is a full PASS;
    each test perturbs exactly one dimension to assert the matching verdict.
    """

    test_model: str
    # Forbidden hints actually present in the parent prompt (empty == clean).
    parent_prompt_hints: frozenset = field(default_factory=frozenset)
    # Durable surfaces the receiver actually read.
    read_surface: frozenset = field(default_factory=frozenset)
    # Child project candidate resolution from the parent project config.
    child_candidate: str = "resolved"  # resolved | missing | ambiguous
    # What the parent grounded its delegation decision in (empty == not grounded).
    parent_decision_grounded_in: frozenset = field(default_factory=frozenset)
    # Resolver / planner output shape.
    resolver_output: str = "executable_handoff"  # executable_handoff | read_only_recommendation
    # Target role of the parent -> child handoff.
    route_target_role: str = "codex_gateway"  # codex_gateway | claude_direct
    cross_project: bool = True
    # Is the child delegated-coordinator window visibly launched/adopted?
    child_window_launched: bool = True
    # Grandchild requirement + how it was realized.
    grandchild_requirement: str = "required"  # required | not_required
    grandchild_realization: str = "visible_lane"
    # visible_lane | same_lane_fallback | not_launched | avoided_with_reason | not_applicable
    # KIND/DEPTH/PARENT live projection chain validity.
    projection_chain_valid: bool = True
    # Freshness of the evidence used to prove success.
    evidence_freshness: str = "fresh"  # fresh | stale
    # Environmental fault that invalidates the attempt (None == clean run).
    environmental_fault: Optional[str] = None
    # marker_timeout | tmux_focus_lost | network_error | redmine_write_error


@dataclass(frozen=True)
class AcceptanceVerdict:
    """Oracle output: a classification token plus an auditable reason."""

    classification: str
    reason: str

    @property
    def is_pass(self) -> bool:
        return self.classification == CLASS_PASS


def classify_acceptance_run(scenario: AcceptanceScenario) -> AcceptanceVerdict:
    """Classify a 3-window acceptance run attempt.

    Precedence (first match wins) — fixed so mixed scenarios are deterministic:

    1. ``environmental`` — an environmental fault means the attempt never really
       exercised the route; its result must not be read as PASS/FAIL.
    2. ``contaminated`` — the receiver read a forbidden surface; excluded from
       PASS/FAIL entirely (``### context-rich contamination``).
    3. ``blocked`` — a required visible lane could not be realized or a required
       durable substrate is missing, so PASS/FAIL is undecidable. The
       grandchild-required-but-same-lane-fallback case carries the specific
       reason ``grandchild_required_but_not_realized``.
    4. ``failed_acceptance`` — a hard invariant was violated (no autonomous
       delegation, cross-project direct Claude send, required window not
       launched, invalid projection, stale evidence used as proof).
    5. ``insufficient`` — the run is not the autonomous model (context-rich /
       harness-assisted prompt) or the resolver only produced a read-only
       recommendation; a partial result that must not count as PASS.
    6. ``PASS`` — none of the above.

    The function is pure and deterministic over its input.
    """
    # 1. Environmental faults invalidate the attempt outright.
    if scenario.environmental_fault is not None:
        return AcceptanceVerdict(
            CLASS_ENVIRONMENTAL, f"environmental_fault:{scenario.environmental_fault}"
        )

    # 2. Contaminated read surface — never mix into PASS/FAIL.
    contaminating = scenario.read_surface & FORBIDDEN_READ_SURFACES
    if contaminating:
        return AcceptanceVerdict(
            CLASS_CONTAMINATED,
            "read_surface_contamination:" + ",".join(sorted(contaminating)),
        )

    # 3. Required visible-lane realization gaps -> blocked (undecidable display).
    if scenario.grandchild_requirement == "required":
        if scenario.grandchild_realization == "same_lane_fallback":
            return AcceptanceVerdict(
                CLASS_BLOCKED, "grandchild_required_but_not_realized"
            )

    # 4. Hard invariant violations -> failed_acceptance.
    failed = _first_failed_acceptance_reason(scenario)
    if failed is not None:
        return AcceptanceVerdict(CLASS_FAILED_ACCEPTANCE, failed)

    # 5. Wrong model / read-only resolver -> insufficient (not a real PASS).
    insufficient = _first_insufficient_reason(scenario)
    if insufficient is not None:
        return AcceptanceVerdict(CLASS_INSUFFICIENT, insufficient)

    # 6. Clean autonomous (or bounded-read) full route.
    return AcceptanceVerdict(CLASS_PASS, "autonomous_three_window_route")


def _first_failed_acceptance_reason(scenario: AcceptanceScenario) -> Optional[str]:
    # Parent cannot ground an autonomous delegation: missing / ambiguous child
    # candidate fails closed (never fabricated into a PASS).
    if scenario.child_candidate == "missing":
        return "child_candidate_missing"
    if scenario.child_candidate == "ambiguous":
        return "child_candidate_ambiguous"
    # Cross-project (or any) parent -> child direct Claude send is forbidden;
    # the route must target a Codex gateway.
    if scenario.route_target_role == "claude_direct":
        return "cross_project_claude_direct_send"
    # The child delegated-coordinator window must be visibly launched/adopted.
    if not scenario.child_window_launched:
        return "child_window_not_launched"
    # Grandchild required but no lane realized at all (distinct from same-lane
    # fallback, which is handled as blocked earlier).
    if (
        scenario.grandchild_requirement == "required"
        and scenario.grandchild_realization == "not_launched"
    ):
        return "grandchild_window_not_launched"
    # Stale pane / worktree / journal cannot satisfy route/display/profile.
    if scenario.evidence_freshness == "stale":
        return "stale_evidence_used_as_proof"
    # Invalid KIND/DEPTH/PARENT projection chain.
    if not scenario.projection_chain_valid:
        return "invalid_projection_chain"
    # An autonomous/bounded model with no grounded decision did not delegate.
    if (
        scenario.test_model in (MODEL_AUTONOMOUS_PARENT, MODEL_BOUNDED_READ)
        and not scenario.parent_decision_grounded_in
    ):
        return "parent_not_autonomous"
    return None


def _first_insufficient_reason(scenario: AcceptanceScenario) -> Optional[str]:
    # Context-rich / harness-assisted runs are never a full-acceptance PASS.
    if scenario.test_model == MODEL_CONTEXT_RICH:
        return "context_rich_not_autonomous"
    if scenario.test_model not in (MODEL_AUTONOMOUS_PARENT, MODEL_BOUNDED_READ):
        return "unknown_test_model"
    # Any forbidden hint pre-loaded into the parent prompt makes the run
    # harness-assisted, i.e. not an autonomous-parent PASS.
    if scenario.parent_prompt_hints:
        return "parent_prompt_harness_assisted"
    # Resolver / planner that only recommends (read-only) without an
    # executable/adoptable child handoff + lane realization plan is insufficient.
    if scenario.resolver_output == "read_only_recommendation":
        return "read_only_recommendation_only"
    return None


def classify_projection_chain(kind: str, depth: int, parent_kind: str) -> bool:
    """Validate a synthetic ``KIND`` / ``DEPTH`` / ``PARENT`` projection row.

    A worker lane realization must project ``KIND=implementation_worker`` at
    ``DEPTH=2`` with ``PARENT`` being the delegated coordinator. Depth beyond the
    hard ceiling, a non-positive depth, or a wrong parent are rejected
    (``### Discovery / projection fixture`` of the smoke-test frame).
    """
    if depth < 1 or depth > MAX_DELEGATION_DEPTH:
        return False
    if kind == ROLE_IMPLEMENTATION_WORKER:
        return depth == MAX_DELEGATION_DEPTH and parent_kind == ROLE_DELEGATED_COORDINATOR
    if kind == ROLE_DELEGATED_COORDINATOR:
        return depth == 1
    return False


def clean_autonomous_pass_scenario(**overrides) -> AcceptanceScenario:
    """Baseline full-PASS autonomous-parent scenario; override one field per test."""
    base = AcceptanceScenario(
        test_model=MODEL_AUTONOMOUS_PARENT,
        parent_prompt_hints=frozenset(),
        read_surface=frozenset({"parent_description", "child_project_config", "start_anchor"}),
        child_candidate="resolved",
        parent_decision_grounded_in=frozenset({"project_config"}),
        resolver_output="executable_handoff",
        route_target_role="codex_gateway",
        cross_project=True,
        child_window_launched=True,
        grandchild_requirement="required",
        grandchild_realization="visible_lane",
        projection_chain_valid=True,
        evidence_freshness="fresh",
        environmental_fault=None,
    )
    return replace(base, **overrides)


class AcceptanceOracleBaselineTest(unittest.TestCase):
    """The clean baseline must be a full PASS, else every negative test is vacuous."""

    def test_clean_autonomous_run_passes(self) -> None:
        verdict = classify_acceptance_run(clean_autonomous_pass_scenario())
        self.assertTrue(verdict.is_pass, verdict)
        self.assertEqual(CLASS_PASS, verdict.classification)

    def test_bounded_read_clean_run_passes(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(test_model=MODEL_BOUNDED_READ)
        )
        self.assertTrue(verdict.is_pass, verdict)

    def test_grandchild_not_required_and_avoided_with_reason_passes(self) -> None:
        # Coordinator may legitimately avoid a grandchild lane with a recorded
        # reason; that is still a PASS, not a forced 3-window.
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                grandchild_requirement="not_required",
                grandchild_realization="avoided_with_reason",
            )
        )
        self.assertTrue(verdict.is_pass, verdict)

    def test_every_verdict_is_in_the_fixed_vocabulary(self) -> None:
        # Guard against the oracle inventing classifications outside the doc set.
        scenarios = [
            clean_autonomous_pass_scenario(),
            clean_autonomous_pass_scenario(test_model=MODEL_CONTEXT_RICH),
            clean_autonomous_pass_scenario(child_candidate="missing"),
            clean_autonomous_pass_scenario(read_surface=frozenset({"prior_smoke"})),
            clean_autonomous_pass_scenario(grandchild_realization="same_lane_fallback"),
            clean_autonomous_pass_scenario(environmental_fault="marker_timeout"),
            clean_autonomous_pass_scenario(resolver_output="read_only_recommendation"),
        ]
        for scenario in scenarios:
            verdict = classify_acceptance_run(scenario)
            self.assertIn(verdict.classification, ACCEPTANCE_CLASSIFICATIONS, verdict)


class AutonomousParentBoundaryTest(unittest.TestCase):
    """Obligation 1 & 2: context-rich / harness-assisted prompts are not PASS."""

    def test_context_rich_run_is_not_autonomous_pass(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(test_model=MODEL_CONTEXT_RICH)
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_INSUFFICIENT, verdict.classification)
        self.assertEqual("context_rich_not_autonomous", verdict.reason)

    def test_parent_prompt_with_explicit_child_route_is_non_autonomous(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                parent_prompt_hints=frozenset({"expected_child_route"})
            )
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_INSUFFICIENT, verdict.classification)
        self.assertEqual("parent_prompt_harness_assisted", verdict.reason)

    def test_parent_prompt_with_grandchild_pane_id_is_non_autonomous(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                parent_prompt_hints=frozenset({"grandchild_pane_id", "worktree_path"})
            )
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_INSUFFICIENT, verdict.classification)

    def test_parent_prompt_with_expected_answer_oracle_is_non_autonomous(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                parent_prompt_hints=frozenset({"expected_answer", "test_oracle_token"})
            )
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_INSUFFICIENT, verdict.classification)

    def test_every_forbidden_hint_blocks_autonomous_pass(self) -> None:
        for hint in FORBIDDEN_PARENT_PROMPT_HINTS:
            verdict = classify_acceptance_run(
                clean_autonomous_pass_scenario(parent_prompt_hints=frozenset({hint}))
            )
            self.assertFalse(verdict.is_pass, f"hint {hint!r} should block PASS")

    def test_parent_with_no_grounded_decision_failed_acceptance(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(parent_decision_grounded_in=frozenset())
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_FAILED_ACCEPTANCE, verdict.classification)
        self.assertEqual("parent_not_autonomous", verdict.reason)


class ChildCandidateResolutionTest(unittest.TestCase):
    """Obligation 3 & 4: missing / ambiguous child candidate fails closed."""

    def test_missing_child_candidate_fails_closed(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(child_candidate="missing")
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_FAILED_ACCEPTANCE, verdict.classification)
        self.assertEqual("child_candidate_missing", verdict.reason)

    def test_ambiguous_child_candidate_fails_closed(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(child_candidate="ambiguous")
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_FAILED_ACCEPTANCE, verdict.classification)
        self.assertEqual("child_candidate_ambiguous", verdict.reason)


class ResolverReadinessTest(unittest.TestCase):
    """Obligation 5: read-only recommendation without executable handoff is insufficient."""

    def test_read_only_recommendation_is_insufficient(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(resolver_output="read_only_recommendation")
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_INSUFFICIENT, verdict.classification)
        self.assertEqual("read_only_recommendation_only", verdict.reason)

    def test_executable_handoff_resolver_is_not_insufficient(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(resolver_output="executable_handoff")
        )
        self.assertTrue(verdict.is_pass, verdict)


class RouteDeliveryTest(unittest.TestCase):
    """Obligation 6: cross-project Claude direct send is never generated."""

    def test_cross_project_claude_direct_send_failed_acceptance(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                cross_project=True, route_target_role="claude_direct"
            )
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_FAILED_ACCEPTANCE, verdict.classification)
        self.assertEqual("cross_project_claude_direct_send", verdict.reason)

    def test_codex_gateway_route_is_accepted(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(route_target_role="codex_gateway")
        )
        self.assertTrue(verdict.is_pass, verdict)


class GrandchildRealizationTest(unittest.TestCase):
    """Obligation 7: grandchild required but same-lane fallback is blocked."""

    def test_same_lane_fallback_is_grandchild_required_but_not_realized(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                grandchild_requirement="required",
                grandchild_realization="same_lane_fallback",
            )
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_BLOCKED, verdict.classification)
        self.assertEqual("grandchild_required_but_not_realized", verdict.reason)

    def test_grandchild_required_but_window_not_launched_failed_acceptance(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                grandchild_requirement="required",
                grandchild_realization="not_launched",
            )
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_FAILED_ACCEPTANCE, verdict.classification)
        self.assertEqual("grandchild_window_not_launched", verdict.reason)

    def test_grandchild_required_and_visible_lane_passes(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                grandchild_requirement="required",
                grandchild_realization="visible_lane",
            )
        )
        self.assertTrue(verdict.is_pass, verdict)


class ReadSurfaceContaminationTest(unittest.TestCase):
    """Obligation 8: parent journals / sibling / prior smoke reads are contaminated."""

    def test_prior_smoke_read_is_contaminated(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                read_surface=frozenset(
                    {"parent_description", "prior_smoke"}
                )
            )
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_CONTAMINATED, verdict.classification)

    def test_parent_journals_read_is_contaminated(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                read_surface=frozenset({"parent_description", "parent_journals"})
            )
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_CONTAMINATED, verdict.classification)

    def test_injected_route_context_read_is_contaminated(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                read_surface=frozenset({"parent_description", "injected_route_context"})
            )
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_CONTAMINATED, verdict.classification)

    def test_every_forbidden_surface_contaminates(self) -> None:
        for surface in FORBIDDEN_READ_SURFACES:
            verdict = classify_acceptance_run(
                clean_autonomous_pass_scenario(
                    read_surface=frozenset({"parent_description", surface})
                )
            )
            self.assertEqual(
                CLASS_CONTAMINATED, verdict.classification, f"{surface!r} must contaminate"
            )

    def test_allowed_surfaces_only_do_not_contaminate(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(read_surface=ALLOWED_READ_SURFACES)
        )
        self.assertTrue(verdict.is_pass, verdict)


class StaleEvidenceTest(unittest.TestCase):
    """Obligation 9: stale pane / worktree / journal cannot satisfy success."""

    def test_stale_evidence_is_failed_acceptance(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(evidence_freshness="stale")
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_FAILED_ACCEPTANCE, verdict.classification)
        self.assertEqual("stale_evidence_used_as_proof", verdict.reason)


class EnvironmentalFaultTest(unittest.TestCase):
    """Environmental faults are non-PASS attempts kept out of success evidence."""

    def test_marker_timeout_is_environmental(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(environmental_fault="marker_timeout")
        )
        self.assertEqual(CLASS_ENVIRONMENTAL, verdict.classification)
        self.assertFalse(verdict.is_pass)

    def test_environmental_fault_outranks_other_findings(self) -> None:
        # An attempt that timed out must not be reported as failed_acceptance
        # even if it also carries a route defect: the attempt never really ran.
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(
                environmental_fault="network_error",
                route_target_role="claude_direct",
            )
        )
        self.assertEqual(CLASS_ENVIRONMENTAL, verdict.classification)


class ProjectionChainTest(unittest.TestCase):
    """KIND / DEPTH / PARENT derivation and invalid-chain rejection."""

    def test_worker_at_depth_two_under_delegated_coordinator_is_valid(self) -> None:
        self.assertTrue(
            classify_projection_chain(
                ROLE_IMPLEMENTATION_WORKER, 2, ROLE_DELEGATED_COORDINATOR
            )
        )

    def test_delegated_coordinator_at_depth_one_is_valid(self) -> None:
        self.assertTrue(
            classify_projection_chain(ROLE_DELEGATED_COORDINATOR, 1, ROLE_COORDINATOR)
        )

    def test_worker_with_wrong_parent_is_invalid(self) -> None:
        self.assertFalse(
            classify_projection_chain(
                ROLE_IMPLEMENTATION_WORKER, 2, ROLE_IMPLEMENTATION_WORKER
            )
        )

    def test_depth_beyond_hard_ceiling_is_invalid(self) -> None:
        self.assertFalse(
            classify_projection_chain(
                ROLE_IMPLEMENTATION_WORKER, 3, ROLE_DELEGATED_COORDINATOR
            )
        )

    def test_non_positive_depth_is_invalid(self) -> None:
        self.assertFalse(
            classify_projection_chain(ROLE_DELEGATED_COORDINATOR, 0, ROLE_COORDINATOR)
        )

    def test_invalid_projection_in_scenario_is_failed_acceptance(self) -> None:
        verdict = classify_acceptance_run(
            clean_autonomous_pass_scenario(projection_chain_valid=False)
        )
        self.assertFalse(verdict.is_pass)
        self.assertEqual(CLASS_FAILED_ACCEPTANCE, verdict.classification)
        self.assertEqual("invalid_projection_chain", verdict.reason)


class RoleProfileChainAnchorTest(unittest.TestCase):
    """Anchor the hermetic oracle to the *shipped* role-profile vocabulary.

    If the runtime role tokens or template source/version drift, these fail so
    the oracle is updated in lockstep rather than silently diverging.
    """

    def test_expected_chain_tokens_are_real_role_profiles(self) -> None:
        for role in EXPECTED_ROLE_PROFILE_CHAIN:
            self.assertIn(role, ROLE_PROFILE_TOKENS, role)

    def test_expected_chain_resolves_with_pinned_source_and_version(self) -> None:
        for role in EXPECTED_ROLE_PROFILE_CHAIN:
            resolution = resolve_role_profile(role)
            self.assertEqual(role, resolution.role_profile)
            self.assertEqual(ROLE_PROFILE_SOURCE, resolution.profile_source)
            self.assertEqual(ROLE_PROFILE_VERSION, resolution.profile_version)

    def test_chain_is_delegated_then_gateway_then_worker(self) -> None:
        self.assertEqual(
            (
                ROLE_DELEGATED_COORDINATOR,
                ROLE_IMPLEMENTATION_GATEWAY,
                ROLE_IMPLEMENTATION_WORKER,
            ),
            EXPECTED_ROLE_PROFILE_CHAIN,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
