"""Pure sublane Git worktree / retire-merge policy tests (Redmine #12604).

Pins the decision core of the config-driven sublane lifecycle knob
(``vibes/docs/logics/worktree-lifecycle-boundary.md``, parent #12603):

- :func:`decide_worktree_launch` — the launch default path for a Git workspace, a
  non-Git directory scaffold (the "Git なし" path), the opt-out, reuse, and the
  fail-closed unknown-target case;
- :func:`decide_retire_integration` — the four acceptance triggers (dirty worktree,
  merge conflict, unresolved target branch, verification failure) recorded as
  fail-closed ``integration_blocked``, plus the two hard invariants: the runtime
  preflight is the final authority over the config, and the owner-approval / close /
  callback / durable-anchor invariants cannot be disabled by config;
- the durable-record / coordinator-callback renderer.

Pure decisions only — no IO, no git, no use case (those are the integration tests).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (
    BLOCKED_DIRTY_WORKTREE,
    BLOCKED_DURABLE_RECORD_MISSING,
    BLOCKED_ISSUE_NOT_CLOSED,
    BLOCKED_MERGE_CONFLICT,
    BLOCKED_OWNER_APPROVAL_MISSING,
    BLOCKED_PREFLIGHT_FAILURE,
    BLOCKED_TARGET_BRANCH_UNRESOLVED,
    BLOCKED_UNRESOLVED_CALLBACK,
    BLOCKED_VERIFICATION_FAILURE,
    INTEGRATION_BLOCKED,
    LAUNCH_BLOCKED,
    LAUNCH_CREATE_WORKTREE,
    LAUNCH_REUSE_WORKTREE,
    LAUNCH_SKIP_DISABLED,
    LAUNCH_SKIP_NO_GIT,
    RETIRE_OK,
    LaunchPreflight,
    RetireDecision,
    RetirePreflight,
    SublaneIntegrationPolicy,
    decide_retire_integration,
    decide_worktree_launch,
    render_integration_decision_journal,
)


def _all_invariants_ok() -> dict[str, bool]:
    return dict(
        target_identity_known=True,
        verification_passed=True,
        issue_closed=True,
        owner_approval_present=True,
        callbacks_drained=True,
        durable_record_recorded=True,
        latest_generation_admissible=True,
    )


class WorktreeLaunchDecisionTest(unittest.TestCase):
    def test_git_workspace_default_path_creates_worktree(self) -> None:
        decision = decide_worktree_launch(
            SublaneIntegrationPolicy.default(),
            LaunchPreflight(is_git_workspace=True),
        )
        self.assertEqual(decision.action, LAUNCH_CREATE_WORKTREE)
        self.assertTrue(decision.creates_worktree)

    def test_non_git_workspace_skips_worktree(self) -> None:
        # The "Git なし" acceptance path: a directory scaffold sublane runs without a
        # worktree rather than failing.
        decision = decide_worktree_launch(
            SublaneIntegrationPolicy.default(),
            LaunchPreflight(is_git_workspace=False),
        )
        self.assertEqual(decision.action, LAUNCH_SKIP_NO_GIT)
        self.assertFalse(decision.creates_worktree)

    def test_manage_worktree_false_opts_out(self) -> None:
        decision = decide_worktree_launch(
            SublaneIntegrationPolicy(manage_worktree=False),
            LaunchPreflight(is_git_workspace=True),
        )
        self.assertEqual(decision.action, LAUNCH_SKIP_DISABLED)

    def test_existing_worktree_is_reused_not_clobbered(self) -> None:
        decision = decide_worktree_launch(
            SublaneIntegrationPolicy.default(),
            LaunchPreflight(is_git_workspace=True, worktree_exists=True),
        )
        self.assertEqual(decision.action, LAUNCH_REUSE_WORKTREE)

    def test_unknown_target_identity_fails_closed(self) -> None:
        decision = decide_worktree_launch(
            SublaneIntegrationPolicy.default(),
            LaunchPreflight(is_git_workspace=True, target_identity_known=False),
        )
        self.assertEqual(decision.action, LAUNCH_BLOCKED)

    def test_unresolved_branch_fails_closed(self) -> None:
        decision = decide_worktree_launch(
            SublaneIntegrationPolicy.default(),
            LaunchPreflight(is_git_workspace=True, branch_resolved=False),
        )
        self.assertEqual(decision.action, LAUNCH_BLOCKED)


class RetireIntegrationHappyPathTest(unittest.TestCase):
    def test_git_clean_all_invariants_retires_with_merge(self) -> None:
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(is_git_workspace=True, **_all_invariants_ok()),
        )
        self.assertEqual(decision.state, RETIRE_OK)
        self.assertTrue(decision.may_retire)
        self.assertTrue(decision.merge_attempted)
        self.assertTrue(decision.merge_performed)
        self.assertEqual(decision.blocked_reasons, ())

    def test_non_git_retires_on_invariants_alone(self) -> None:
        # Git なし: no worktree / merge gates apply; the invariants carry the decision.
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(is_git_workspace=False, **_all_invariants_ok()),
        )
        self.assertEqual(decision.state, RETIRE_OK)
        self.assertFalse(decision.merge_attempted)
        self.assertFalse(decision.merge_performed)


class RetireIntegrationBlockedTriggersTest(unittest.TestCase):
    """The four acceptance triggers each fail closed to integration_blocked."""

    def test_inadmissible_latest_generation_blocks(self) -> None:
        # #13518 R2-F7 / R3-F2: the actual retire/integration decision fences a stale / unclean
        # latest review generation — not only the non-CLI use case.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E501
            INTEGRATION_STALE_REVIEW_GENERATION,
        )

        ok = _all_invariants_ok()
        ok["latest_generation_admissible"] = False
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(is_git_workspace=True, **ok),
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(INTEGRATION_STALE_REVIEW_GENERATION, decision.blocked_reasons)
        self.assertFalse(decision.merge_performed)

    def test_omitting_generation_field_blocks_fail_closed(self) -> None:
        # #13518 R4-F3: the pure decision default for latest_generation_admissible is fail-closed —
        # a caller that OMITS it (every other invariant satisfied) is blocked, never default-admitted.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E501
            INTEGRATION_STALE_REVIEW_GENERATION,
        )

        ok = _all_invariants_ok()
        ok.pop("latest_generation_admissible")  # omit it -> must default fail-closed
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(is_git_workspace=True, **ok),
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(INTEGRATION_STALE_REVIEW_GENERATION, decision.blocked_reasons)

    def test_dirty_worktree_blocks(self) -> None:
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(
                is_git_workspace=True, worktree_dirty=True, **_all_invariants_ok()
            ),
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(BLOCKED_DIRTY_WORKTREE, decision.blocked_reasons)
        self.assertFalse(decision.merge_performed)

    def test_merge_conflict_blocks(self) -> None:
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(
                is_git_workspace=True, merge_conflict=True, **_all_invariants_ok()
            ),
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertEqual(decision.primary_reason, BLOCKED_MERGE_CONFLICT)

    def test_unresolved_target_branch_blocks(self) -> None:
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(
                is_git_workspace=True,
                integration_branch_resolved=False,
                **_all_invariants_ok(),
            ),
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(BLOCKED_TARGET_BRANCH_UNRESOLVED, decision.blocked_reasons)

    def test_verification_failure_blocks(self) -> None:
        invariants = _all_invariants_ok()
        invariants["verification_passed"] = False
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(is_git_workspace=True, **invariants),
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(BLOCKED_VERIFICATION_FAILURE, decision.blocked_reasons)

    def test_unknown_target_identity_is_preflight_failure(self) -> None:
        invariants = _all_invariants_ok()
        invariants["target_identity_known"] = False
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(is_git_workspace=True, **invariants),
        )
        self.assertEqual(decision.primary_reason, BLOCKED_PREFLIGHT_FAILURE)


class RuntimePreflightAuthorityTest(unittest.TestCase):
    """The runtime preflight is the final authority; config can only opt out of merge."""

    def test_merge_opt_out_skips_merge_but_keeps_other_gates(self) -> None:
        # merge_on_retire: false means an unresolved branch / would-be conflict no longer
        # matters, but a clean retire still happens on the other gates.
        decision = decide_retire_integration(
            SublaneIntegrationPolicy(merge_on_retire=False),
            RetirePreflight(
                is_git_workspace=True,
                integration_branch_resolved=False,
                merge_conflict=True,
                **_all_invariants_ok(),
            ),
        )
        self.assertEqual(decision.state, RETIRE_OK)
        self.assertFalse(decision.merge_attempted)
        self.assertFalse(decision.merge_performed)

    def test_merge_opt_out_cannot_disable_dirty_worktree_gate(self) -> None:
        decision = decide_retire_integration(
            SublaneIntegrationPolicy(merge_on_retire=False),
            RetirePreflight(
                is_git_workspace=True, worktree_dirty=True, **_all_invariants_ok()
            ),
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(BLOCKED_DIRTY_WORKTREE, decision.blocked_reasons)


class ConfigUndisableableInvariantsTest(unittest.TestCase):
    """owner approval / close / callback / durable anchor cannot be disabled by config."""

    def test_issue_not_closed_blocks_even_with_merge_opt_out(self) -> None:
        invariants = _all_invariants_ok()
        invariants["issue_closed"] = False
        decision = decide_retire_integration(
            SublaneIntegrationPolicy(merge_on_retire=False),
            RetirePreflight(is_git_workspace=True, **invariants),
        )
        self.assertEqual(decision.primary_reason, BLOCKED_ISSUE_NOT_CLOSED)

    def test_owner_approval_missing_blocks(self) -> None:
        invariants = _all_invariants_ok()
        invariants["owner_approval_present"] = False
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(is_git_workspace=True, **invariants),
        )
        self.assertIn(BLOCKED_OWNER_APPROVAL_MISSING, decision.blocked_reasons)

    def test_unresolved_callback_blocks(self) -> None:
        invariants = _all_invariants_ok()
        invariants["callbacks_drained"] = False
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(is_git_workspace=True, **invariants),
        )
        self.assertIn(BLOCKED_UNRESOLVED_CALLBACK, decision.blocked_reasons)

    def test_durable_record_missing_blocks(self) -> None:
        invariants = _all_invariants_ok()
        invariants["durable_record_recorded"] = False
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(is_git_workspace=True, **invariants),
        )
        self.assertIn(BLOCKED_DURABLE_RECORD_MISSING, decision.blocked_reasons)

    def test_invariants_apply_even_in_non_git_workspace(self) -> None:
        invariants = _all_invariants_ok()
        invariants["owner_approval_present"] = False
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(is_git_workspace=False, **invariants),
        )
        self.assertEqual(decision.state, INTEGRATION_BLOCKED)
        self.assertIn(BLOCKED_OWNER_APPROVAL_MISSING, decision.blocked_reasons)


class MultipleBlockersTest(unittest.TestCase):
    def test_all_blockers_reported_primary_by_precedence(self) -> None:
        invariants = _all_invariants_ok()
        invariants["owner_approval_present"] = False
        decision = decide_retire_integration(
            SublaneIntegrationPolicy.default(),
            RetirePreflight(
                is_git_workspace=True, worktree_dirty=True, merge_conflict=True, **invariants
            ),
        )
        # Owner approval (an invariant) precedes the worktree / merge gates.
        self.assertEqual(decision.primary_reason, BLOCKED_OWNER_APPROVAL_MISSING)
        self.assertIn(BLOCKED_DIRTY_WORKTREE, decision.blocked_reasons)
        self.assertIn(BLOCKED_MERGE_CONFLICT, decision.blocked_reasons)


class RenderJournalTest(unittest.TestCase):
    def test_blocked_journal_lists_reasons(self) -> None:
        decision = RetireDecision(
            state=INTEGRATION_BLOCKED,
            blocked_reasons=(BLOCKED_MERGE_CONFLICT,),
            primary_reason=BLOCKED_MERGE_CONFLICT,
            merge_attempted=True,
            merge_performed=False,
        )
        text = render_integration_decision_journal(
            decision, issue="12604", integration_branch="main"
        )
        self.assertIn("## integration_blocked", text)
        self.assertIn("- issue: #12604", text)
        self.assertIn("- state: integration_blocked", text)
        self.assertIn("- integration_branch: main", text)
        self.assertIn(f"- primary_reason: {BLOCKED_MERGE_CONFLICT}", text)
        self.assertIn("coordinator callback", text)

    def test_ok_journal_authorizes_retire(self) -> None:
        decision = RetireDecision(
            state=RETIRE_OK, merge_attempted=True, merge_performed=True
        )
        text = render_integration_decision_journal(decision, issue="12604")
        self.assertIn("## retire integration decision", text)
        self.assertIn("- state: retire_ok", text)
        self.assertIn("- integration_branch: runtime-resolved", text)
        self.assertIn("- blocked_reasons: none", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
