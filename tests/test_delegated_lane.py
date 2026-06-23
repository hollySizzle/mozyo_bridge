"""Delegated coordinator lane launch/adopt decision tests (Redmine #12447).

US #12437 wants a parent project to start (launch) or explicitly adopt a visible
child ``delegated_coordinator`` lane rather than treating a route smoke to a
pre-existing Codex pane as completion (#12437 j#63530 scope correction). These
tests pin the pure, fail-closed decision core in
``mozyo_bridge.domain.delegated_lane``:

- ``decide_delegation_lane`` requires an explicit launch/adopt decision and never
  auto-reuses an existing lane as PASS; adopt resolves / fails closed on the
  existing canonical Codex lane; launch requires a present canonical root and a
  replayable lane identity.
- ``build_delegation_lane_record`` projects the decision onto a replayable durable
  record.
- ``build_delegation_display_record`` projects onto the cockpit delegation-display
  schema (parent → child relationship).

No live tmux / filesystem is required: the target is an in-memory dataclass and
candidates are lightweight fakes that duck-type the selector's contract.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.delegated_lane import (  # noqa: E402
    CALLBACK_OUTCOME_BLOCKED,
    CALLBACK_OUTCOME_NOT_APPLICABLE,
    CALLBACK_OUTCOME_SENT,
    CALLBACK_PURPOSE_AUDIT_COORDINATOR,
    CALLBACK_PURPOSE_DELEGATION_PARENT,
    CALLBACK_PURPOSE_OWNING_US_COORDINATOR,
    CODE_ADOPT_NO_EXISTING_LANE,
    CODE_ADOPT_TARGET_AMBIGUOUS,
    CODE_DECISION_REQUIRED,
    CODE_LAUNCH_IDENTITY_INCOMPLETE,
    CODE_LAUNCH_ROOT_ABSENT,
    DELEGATED_COORDINATOR_DEPTH,
    LANE_ADOPT,
    LANE_LAUNCH,
    DelegationLaneError,
    build_delegation_display_record,
    build_delegation_lane_record,
    build_required_callback_targets,
    decide_delegation_lane,
    evaluate_callback_coverage,
)
from mozyo_bridge.domain.project_router import DelegationTarget  # noqa: E402
from mozyo_bridge.domain.role_profile import ROLE_DELEGATED_COORDINATOR  # noqa: E402


@dataclass(frozen=True)
class FakeCandidate:
    """Minimal duck-typed stand-in for a discovered target candidate."""

    role: str
    repo_root: str | None
    pane_id: str
    ambiguous: bool = False
    lane_id: str | None = None


CANON = "/workspace/project-alpha"


def _target(**overrides) -> DelegationTarget:
    base = dict(
        target_project="giken-3800-mozyo-bridge",
        classification="external-submodule",
        canonical_repo_root=CANON,
        child_project="giken-3800-mozyo-bridge",
        redmine_project="giken-3800-mozyo-bridge",
        parent_project="gk-3500-it-operations",
    )
    base.update(overrides)
    return DelegationTarget(**base)


class DecisionRequiredTest(unittest.TestCase):
    """The core never silently reuses an existing lane as PASS (#12437 j#63530)."""

    def test_omitted_decision_fails_closed(self) -> None:
        with self.assertRaises(DelegationLaneError) as ctx:
            decide_delegation_lane(_target(), mode=None)
        self.assertEqual(ctx.exception.code, CODE_DECISION_REQUIRED)

    def test_blank_decision_fails_closed(self) -> None:
        with self.assertRaises(DelegationLaneError) as ctx:
            decide_delegation_lane(_target(), mode="  ")
        self.assertEqual(ctx.exception.code, CODE_DECISION_REQUIRED)

    def test_existing_lane_is_not_auto_adopted(self) -> None:
        # A unique usable canonical Codex pane exists, but omitting the decision
        # still fails closed: an existing lane route is not launch/adopt PASS.
        candidates = [FakeCandidate("codex", CANON, "%2")]
        with self.assertRaises(DelegationLaneError) as ctx:
            decide_delegation_lane(_target(), mode=None, candidates=candidates)
        self.assertEqual(ctx.exception.code, CODE_DECISION_REQUIRED)
        # The existing pane is surfaced as NOT auto-adopted.
        self.assertIn("%2", str(ctx.exception))


class AdoptDecisionTest(unittest.TestCase):
    def test_adopt_selects_unique_existing_lane(self) -> None:
        candidates = [
            FakeCandidate("claude", CANON, "%1"),
            FakeCandidate("codex", CANON, "%2", lane_id="lane-2123ef563427"),
            FakeCandidate("codex", "/workspace/project-beta", "%3"),
        ]
        decision = decide_delegation_lane(
            _target(), mode=LANE_ADOPT, candidates=candidates, parent_issue="12437"
        )
        self.assertEqual(decision.mode, LANE_ADOPT)
        self.assertEqual(decision.adopt_target, "%2")
        self.assertEqual(decision.lane_id, "lane-2123ef563427")
        self.assertEqual(decision.lane_kind, ROLE_DELEGATED_COORDINATOR)
        self.assertEqual(decision.delegation_depth, DELEGATED_COORDINATOR_DEPTH)

    def test_explicit_adopt_target_wins(self) -> None:
        # An explicit operator-named pane is adopted without consulting candidates.
        decision = decide_delegation_lane(
            _target(),
            mode=LANE_ADOPT,
            explicit_adopt_target="%9",
            candidates=[FakeCandidate("codex", CANON, "%2")],
        )
        self.assertEqual(decision.adopt_target, "%9")

    def test_adopt_no_existing_lane_fails_closed(self) -> None:
        candidates = [FakeCandidate("codex", "/workspace/project-beta", "%3")]
        with self.assertRaises(DelegationLaneError) as ctx:
            decide_delegation_lane(_target(), mode=LANE_ADOPT, candidates=candidates)
        self.assertEqual(ctx.exception.code, CODE_ADOPT_NO_EXISTING_LANE)

    def test_adopt_ambiguous_fails_closed(self) -> None:
        candidates = [
            FakeCandidate("codex", CANON, "%2"),
            FakeCandidate("codex", CANON, "%4"),
        ]
        with self.assertRaises(DelegationLaneError) as ctx:
            decide_delegation_lane(_target(), mode=LANE_ADOPT, candidates=candidates)
        self.assertEqual(ctx.exception.code, CODE_ADOPT_TARGET_AMBIGUOUS)

    def test_adopt_does_not_require_canonical_root_present(self) -> None:
        # Adopt targets a live pane, so a missing local checkout is irrelevant.
        candidates = [FakeCandidate("codex", CANON, "%2")]
        decision = decide_delegation_lane(
            _target(),
            mode=LANE_ADOPT,
            candidates=candidates,
            canonical_root_present=False,
        )
        self.assertEqual(decision.adopt_target, "%2")


class LaunchDecisionTest(unittest.TestCase):
    def test_launch_requires_local_canonical_root(self) -> None:
        with self.assertRaises(DelegationLaneError) as ctx:
            decide_delegation_lane(
                _target(),
                mode=LANE_LAUNCH,
                canonical_root_present=False,
                child_issue="12447",
                branch="issue_12447_lane",
            )
        self.assertEqual(ctx.exception.code, CODE_LAUNCH_ROOT_ABSENT)

    def test_launch_requires_child_issue_and_branch_or_worktree(self) -> None:
        with self.assertRaises(DelegationLaneError) as ctx:
            decide_delegation_lane(
                _target(),
                mode=LANE_LAUNCH,
                canonical_root_present=True,
                child_issue="12447",
                # neither branch nor worktree
            )
        self.assertEqual(ctx.exception.code, CODE_LAUNCH_IDENTITY_INCOMPLETE)

        with self.assertRaises(DelegationLaneError) as ctx:
            decide_delegation_lane(
                _target(),
                mode=LANE_LAUNCH,
                canonical_root_present=True,
                branch="issue_12447_lane",
                # no child_issue
            )
        self.assertEqual(ctx.exception.code, CODE_LAUNCH_IDENTITY_INCOMPLETE)

    def test_launch_with_identity_resolves(self) -> None:
        decision = decide_delegation_lane(
            _target(),
            mode=LANE_LAUNCH,
            canonical_root_present=True,
            child_issue="12447",
            branch="issue_12447_delegated_lane",
            worktree="mozyo_bridge-12447",
            parent_issue="12437",
            parent_callback_target="%8",
        )
        self.assertEqual(decision.mode, LANE_LAUNCH)
        self.assertIsNone(decision.adopt_target)
        self.assertEqual(decision.child_issue, "12447")
        self.assertEqual(decision.branch, "issue_12447_delegated_lane")
        self.assertEqual(decision.worktree, "mozyo_bridge-12447")
        self.assertEqual(decision.callback_route, "%8")
        self.assertTrue(decision.no_hidden_subagent)

    def test_launch_never_reuses_existing_candidates(self) -> None:
        # Even with a usable existing canonical Codex pane, launch produces a fresh
        # lane identity and does not adopt the existing pane.
        candidates = [FakeCandidate("codex", CANON, "%2")]
        decision = decide_delegation_lane(
            _target(),
            mode=LANE_LAUNCH,
            candidates=candidates,
            canonical_root_present=True,
            child_issue="12447",
            branch="issue_12447_lane",
        )
        self.assertIsNone(decision.adopt_target)


class DelegationBreadcrumbTest(unittest.TestCase):
    def test_parent_pointer_composed_from_project_and_issue(self) -> None:
        decision = decide_delegation_lane(
            _target(),
            mode=LANE_ADOPT,
            explicit_adopt_target="%2",
            parent_issue="12437",
        )
        self.assertEqual(decision.delegation_root, "gk-3500-it-operations#12437")
        self.assertEqual(decision.delegation_parent, "gk-3500-it-operations#12437")
        # The delegated lane's retire owner is its parent coordinator.
        self.assertEqual(decision.retire_owner, "gk-3500-it-operations#12437")

    def test_explicit_delegation_pointers_win(self) -> None:
        decision = decide_delegation_lane(
            _target(),
            mode=LANE_ADOPT,
            explicit_adopt_target="%2",
            parent_issue="12437",
            delegation_root="gk/coordinator",
            delegation_parent="gk/coordinator",
        )
        self.assertEqual(decision.delegation_root, "gk/coordinator")
        self.assertEqual(decision.delegation_parent, "gk/coordinator")

    def test_parent_project_falls_back_to_target(self) -> None:
        # parent_project defaults to the config-declared parent on the target.
        decision = decide_delegation_lane(
            _target(), mode=LANE_ADOPT, explicit_adopt_target="%2"
        )
        self.assertEqual(decision.parent_project, "gk-3500-it-operations")


class RecordProjectionTest(unittest.TestCase):
    def _adopt_decision(self):
        return decide_delegation_lane(
            _target(),
            mode=LANE_ADOPT,
            explicit_adopt_target="%2",
            parent_issue="12437",
            parent_callback_target="%8",
            owning_us_coordinator="%6",
            lane_id="lane-2123ef563427",
        )

    def test_lane_record_is_replayable(self) -> None:
        record = build_delegation_lane_record(self._adopt_decision())
        self.assertEqual(record["lane_decision"], LANE_ADOPT)
        self.assertEqual(record["lane_kind"], ROLE_DELEGATED_COORDINATOR)
        self.assertEqual(record["target_project"], "giken-3800-mozyo-bridge")
        self.assertEqual(record["canonical_repo_root"], CANON)
        self.assertEqual(record["parent_issue"], "12437")
        self.assertEqual(record["adopt_target"], "%2")
        self.assertEqual(record["lane_id"], "lane-2123ef563427")
        self.assertTrue(record["no_hidden_subagent"])
        # The record carries purpose-tagged callback targets, not one route (#12449).
        self.assertNotIn("callback_route", record)
        self.assertEqual(
            record["callback_targets"],
            [
                {"route": "%8", "purposes": ["delegation_parent"], "required": True},
                {
                    "route": "%6",
                    "purposes": ["owning_us_coordinator"],
                    "required": True,
                },
            ],
        )

    def test_lane_record_omits_unset_optionals(self) -> None:
        decision = decide_delegation_lane(
            _target(redmine_project=None, parent_project=None),
            mode=LANE_ADOPT,
            explicit_adopt_target="%2",
        )
        record = build_delegation_lane_record(decision)
        # adopt has no launch branch/worktree; those keys are omitted.
        self.assertNotIn("branch", record)
        self.assertNotIn("worktree", record)
        self.assertNotIn("redmine_project", record)
        # The decision and boundary markers are always present.
        self.assertIn("lane_decision", record)
        self.assertIn("no_hidden_subagent", record)

    def test_display_record_matches_cockpit_schema(self) -> None:
        record = build_delegation_display_record(
            self._adopt_decision(),
            unit_id="mozyo/lane-2123ef563427",
            source_refs=[
                "redmine:#12447#journal-63531",
                "tmux:%2@2026-06-23",
                "",  # falsy refs are dropped
            ],
        )
        self.assertEqual(record["unit_id"], "mozyo/lane-2123ef563427")
        self.assertEqual(record["lane_kind"], ROLE_DELEGATED_COORDINATOR)
        self.assertEqual(record["delegation_depth"], DELEGATED_COORDINATOR_DEPTH)
        self.assertEqual(record["delegation_root"], "gk-3500-it-operations#12437")
        self.assertEqual(record["retire_owner"], "gk-3500-it-operations#12437")
        self.assertEqual(
            record["source_refs"],
            ["redmine:#12447#journal-63531", "tmux:%2@2026-06-23"],
        )


class MultiRecipientCallbackTest(unittest.TestCase):
    """Purpose-tagged required callback targets + coverage (Redmine #12449)."""

    def test_distinct_routes_become_separate_required_targets(self) -> None:
        targets = build_required_callback_targets(
            delegation_parent_route="%8", owning_us_coordinator_route="%6"
        )
        self.assertEqual([t.route for t in targets], ["%8", "%6"])
        self.assertEqual(targets[0].purposes, (CALLBACK_PURPOSE_DELEGATION_PARENT,))
        self.assertEqual(
            targets[1].purposes, (CALLBACK_PURPOSE_OWNING_US_COORDINATOR,)
        )
        self.assertTrue(all(t.required for t in targets))

    def test_shared_route_records_both_purposes_explicitly(self) -> None:
        # If both purposes resolve to the same route, neither purpose is dropped.
        targets = build_required_callback_targets(
            delegation_parent_route="%8", owning_us_coordinator_route="%8"
        )
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].route, "%8")
        self.assertEqual(
            targets[0].purposes,
            (CALLBACK_PURPOSE_DELEGATION_PARENT, CALLBACK_PURPOSE_OWNING_US_COORDINATOR),
        )

    def test_audit_coordinator_is_a_third_target(self) -> None:
        targets = build_required_callback_targets(
            delegation_parent_route="%8",
            owning_us_coordinator_route="%6",
            audit_coordinator_route="%4",
        )
        self.assertEqual([t.route for t in targets], ["%8", "%6", "%4"])
        self.assertEqual(targets[2].purposes, (CALLBACK_PURPOSE_AUDIT_COORDINATOR,))

    def test_purpose_without_route_is_omitted(self) -> None:
        targets = build_required_callback_targets(delegation_parent_route="%8")
        self.assertEqual([t.route for t in targets], ["%8"])

    def test_decision_carries_callback_targets(self) -> None:
        decision = decide_delegation_lane(
            _target(),
            mode=LANE_ADOPT,
            explicit_adopt_target="%2",
            parent_callback_target="%8",
            owning_us_coordinator="%6",
        )
        self.assertEqual([t.route for t in decision.callback_targets], ["%8", "%6"])
        # The back-compat alias still returns only the delegation-parent route.
        self.assertEqual(decision.callback_route, "%8")

    def test_parent_only_outcome_does_not_satisfy_coverage(self) -> None:
        # The #12448 regression: notifying only the parent coordinator while a
        # distinct owning-US coordinator is also required must NOT pass.
        targets = build_required_callback_targets(
            delegation_parent_route="%8", owning_us_coordinator_route="%6"
        )
        coverage = evaluate_callback_coverage(targets, {"%8": CALLBACK_OUTCOME_SENT})
        self.assertFalse(coverage.satisfied)
        self.assertEqual(coverage.pending_routes, ("%6",))
        self.assertIn(CALLBACK_PURPOSE_OWNING_US_COORDINATOR, coverage.pending_purposes)

    def test_all_required_outcomes_satisfy_coverage(self) -> None:
        targets = build_required_callback_targets(
            delegation_parent_route="%8", owning_us_coordinator_route="%6"
        )
        coverage = evaluate_callback_coverage(
            targets,
            {"%8": CALLBACK_OUTCOME_SENT, "%6": CALLBACK_OUTCOME_BLOCKED},
        )
        self.assertTrue(coverage.satisfied)
        self.assertEqual(coverage.pending_routes, ())

    def test_explicit_not_applicable_satisfies_a_target(self) -> None:
        # not_applicable is an explicit operator decision, not silent omission.
        targets = build_required_callback_targets(
            delegation_parent_route="%8", owning_us_coordinator_route="%6"
        )
        coverage = evaluate_callback_coverage(
            targets,
            {"%8": CALLBACK_OUTCOME_SENT, "%6": CALLBACK_OUTCOME_NOT_APPLICABLE},
        )
        self.assertTrue(coverage.satisfied)

    def test_shared_route_single_outcome_satisfies_both_purposes(self) -> None:
        targets = build_required_callback_targets(
            delegation_parent_route="%8", owning_us_coordinator_route="%8"
        )
        coverage = evaluate_callback_coverage(targets, {"%8": CALLBACK_OUTCOME_SENT})
        self.assertTrue(coverage.satisfied)


if __name__ == "__main__":
    unittest.main()
