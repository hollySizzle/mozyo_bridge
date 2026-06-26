"""Tests for the delegated coordinator launch/adopt decision primitive (#12457).

Covers the pure decision resolver
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_launch_adopt`) across every mode / outcome,
the fail-closed invariants (disabled, mandatory repo identity gate, candidate
uniqueness, weak/ambiguous identity, never a direct Claude route), the callback
target validation, and the CLI parser surface registration.
"""

from __future__ import annotations

import contextlib
import io
import shlex
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.application.delegation_launch_adopt import (
    _recommended_command,
    cmd_handoff_delegate_launch_adopt,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_launch_adopt import (
    CONFIDENCE_STRONG,
    CallbackTarget,
    DelegationCandidate,
    DelegationLaunchAdoptError,
    LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
    LAUNCH_ADOPT_MODE_DISABLED,
    LAUNCH_ADOPT_MODE_LAUNCH_NEW,
    LAUNCH_ADOPT_MODE_LAUNCH_OR_ADOPT,
    OUTCOME_ADOPT,
    OUTCOME_FAIL_CLOSED,
    OUTCOME_LAUNCH,
    PURPOSE_AUDIT_COORDINATOR,
    PURPOSE_DELEGATION_PARENT,
    PURPOSE_OWNING_US_COORDINATOR,
    REASON_AMBIGUOUS_CANDIDATES,
    REASON_DELEGATION_DISABLED,
    REASON_MISSING_TARGET_REPO_IDENTITY,
    REASON_NO_CANDIDATE,
    REASON_UNSAFE_CANDIDATE_IDENTITY,
    ROLE_CLAUDE,
    ROLE_CODEX,
    repo_identity_matches,
    resolve_launch_adopt,
    validate_callback_targets,
)

CHILD_REPO = "/workspace/project-child"


def _codex(
    pane_id="%19",
    *,
    repo_root=CHILD_REPO,
    lane_id="lane-child",
    confidence=CONFIDENCE_STRONG,
    ambiguous=False,
    workspace_label="child",
):
    return DelegationCandidate(
        pane_id=pane_id,
        role=ROLE_CODEX,
        repo_root=repo_root,
        workspace_id="ws-child",
        workspace_label=workspace_label,
        lane_id=lane_id,
        lane_label=lane_id,
        confidence=confidence,
        ambiguous=ambiguous,
        session="cockpit",
        window_name="codex",
    )


def _claude(pane_id="%20", *, repo_root=CHILD_REPO, lane_id="lane-child"):
    return DelegationCandidate(
        pane_id=pane_id,
        role=ROLE_CLAUDE,
        repo_root=repo_root,
        workspace_id="ws-child",
        workspace_label="child",
        lane_id=lane_id,
        lane_label=lane_id,
        confidence=CONFIDENCE_STRONG,
        ambiguous=False,
        session="cockpit",
        window_name="claude",
    )


class CallerErrorTest(unittest.TestCase):
    def test_unknown_mode_raises(self):
        with self.assertRaises(DelegationLaunchAdoptError):
            resolve_launch_adopt(
                mode="bogus",
                candidates=[_codex()],
                target_repo_identity=CHILD_REPO,
            )

    def test_claude_required_role_raises(self):
        # The route may never land directly at a child Claude — a caller error,
        # not a fail-closed outcome.
        with self.assertRaises(DelegationLaunchAdoptError):
            resolve_launch_adopt(
                mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
                candidates=[_codex()],
                target_repo_identity=CHILD_REPO,
                required_role=ROLE_CLAUDE,
            )


class DisabledAndIdentityGateTest(unittest.TestCase):
    def test_disabled_fails_closed_with_explicit_reason(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_DISABLED,
            candidates=[_codex()],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_DELEGATION_DISABLED)
        self.assertIsNone(decision.selected)

    def test_missing_target_repo_identity_fails_closed(self):
        for identity in (None, "", "   "):
            decision = resolve_launch_adopt(
                mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
                candidates=[_codex()],
                target_repo_identity=identity,
            )
            self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
            self.assertEqual(decision.reason, REASON_MISSING_TARGET_REPO_IDENTITY)
            # No candidate is even matched without a canonical identity anchor.
            self.assertEqual(decision.matched_candidates, ())


class AdoptExistingTest(unittest.TestCase):
    def test_unique_strong_codex_is_adopted(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[_codex(pane_id="%19")],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_ADOPT)
        self.assertIsNotNone(decision.selected)
        self.assertEqual(decision.selected.pane_id, "%19")
        self.assertEqual(decision.required_role, ROLE_CODEX)

    def test_zero_candidates_fail_closed_no_candidate(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_NO_CANDIDATE)

    def test_multiple_matches_fail_closed_ambiguous(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[
                _codex(pane_id="%19", lane_id="lane-a"),
                _codex(pane_id="%21", lane_id="lane-b"),
            ],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_AMBIGUOUS_CANDIDATES)
        self.assertEqual(len(decision.matched_candidates), 2)

    def test_ambiguous_candidate_identity_fails_closed(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[_codex(ambiguous=True)],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_UNSAFE_CANDIDATE_IDENTITY)

    def test_weak_confidence_candidate_fails_closed(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[_codex(confidence="weak")],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_UNSAFE_CANDIDATE_IDENTITY)


class CandidateFilterTest(unittest.TestCase):
    def test_claude_candidate_is_never_selected(self):
        # A same-repo Claude pane must not satisfy the route; only the codex
        # gateway is adoptable. With one codex + one claude, the codex adopts.
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[_claude(pane_id="%20"), _codex(pane_id="%19")],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_ADOPT)
        self.assertEqual(decision.selected.pane_id, "%19")
        self.assertTrue(all(c.role == ROLE_CODEX for c in decision.matched_candidates))

    def test_claude_only_pool_yields_no_candidate(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[_claude()],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_NO_CANDIDATE)

    def test_repo_identity_mismatch_excludes_candidate(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[_codex(repo_root="/workspace/some-other-repo")],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_NO_CANDIDATE)

    def test_excluded_lane_is_filtered(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[_codex(lane_id="lane-retired")],
            target_repo_identity=CHILD_REPO,
            excluded_lane_ids=("lane-retired",),
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_NO_CANDIDATE)


class LaunchNewTest(unittest.TestCase):
    def test_launch_new_launches_even_with_a_candidate(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_LAUNCH_NEW,
            candidates=[_codex()],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_LAUNCH)
        self.assertIsNone(decision.selected)
        # The existing candidate is still surfaced for the audit record.
        self.assertEqual(len(decision.matched_candidates), 1)

    def test_launch_new_still_fails_closed_on_unsafe_identity(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_LAUNCH_NEW,
            candidates=[_codex(ambiguous=True)],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_UNSAFE_CANDIDATE_IDENTITY)


class LaunchOrAdoptTest(unittest.TestCase):
    def test_unique_match_adopts(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_LAUNCH_OR_ADOPT,
            candidates=[_codex(pane_id="%19")],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_ADOPT)
        self.assertEqual(decision.selected.pane_id, "%19")

    def test_zero_matches_launches(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_LAUNCH_OR_ADOPT,
            candidates=[],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_LAUNCH)

    def test_multiple_matches_fail_closed(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_LAUNCH_OR_ADOPT,
            candidates=[_codex(pane_id="%19"), _codex(pane_id="%21", lane_id="lane-b")],
            target_repo_identity=CHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_AMBIGUOUS_CANDIDATES)


class RepoIdentityMatchTest(unittest.TestCase):
    def test_trailing_slash_normalized(self):
        self.assertTrue(repo_identity_matches("/repo/child/", "/repo/child"))
        self.assertTrue(repo_identity_matches("/repo/child", "/repo/child/"))

    def test_distinct_roots_do_not_match(self):
        self.assertFalse(repo_identity_matches("/repo/child", "/repo/other"))

    def test_missing_side_never_matches(self):
        self.assertFalse(repo_identity_matches(None, "/repo/child"))
        self.assertFalse(repo_identity_matches("/repo/child", None))


class CallbackTargetTest(unittest.TestCase):
    def test_requires_delegation_parent(self):
        with self.assertRaises(DelegationLaunchAdoptError):
            validate_callback_targets(
                [CallbackTarget(purpose=PURPOSE_OWNING_US_COORDINATOR, route="r")]
            )

    def test_empty_set_fails_closed(self):
        with self.assertRaises(DelegationLaunchAdoptError):
            validate_callback_targets([])

    def test_unknown_purpose_rejected(self):
        with self.assertRaises(DelegationLaunchAdoptError):
            validate_callback_targets([CallbackTarget(purpose="bogus", route="r")])

    def test_empty_route_rejected(self):
        with self.assertRaises(DelegationLaunchAdoptError):
            validate_callback_targets(
                [CallbackTarget(purpose=PURPOSE_DELEGATION_PARENT, route="  ")]
            )

    def test_valid_multi_target_set_passes(self):
        targets = validate_callback_targets(
            [
                CallbackTarget(purpose=PURPOSE_DELEGATION_PARENT, route="parent-route"),
                CallbackTarget(
                    purpose=PURPOSE_AUDIT_COORDINATOR, route="audit-route", required=False
                ),
            ]
        )
        self.assertEqual(len(targets), 2)


class DecisionProjectionTest(unittest.TestCase):
    def test_to_dict_and_summary_have_no_absolute_path(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[_codex()],
            target_repo_identity=CHILD_REPO,
            child_project="child-proj",
        )
        payload = decision.to_dict()
        self.assertEqual(payload["outcome"], OUTCOME_ADOPT)
        self.assertEqual(payload["child_project"], "child-proj")
        selected = payload["selected"]
        # The compact summary projects the repo basename, never the abs path.
        self.assertEqual(selected["repo_short"], "project-child")
        self.assertNotIn("repo_root", selected)


class ParserSurfaceTest(unittest.TestCase):
    def _parse(self, argv):
        return build_parser().parse_args(
            ["handoff", "delegate-launch-adopt", *argv]
        )

    def _base(self, **overrides):
        argv = [
            "--launch-adopt-mode", "launch_or_adopt",
            "--target-repo", CHILD_REPO,
            "--parent-coordinator-route", "parent-route",
        ]
        return argv

    def test_minimal_valid_args_parse(self):
        ns = self._parse(self._base())
        self.assertEqual(ns.launch_adopt_mode, "launch_or_adopt")
        self.assertEqual(ns.target_repo, CHILD_REPO)
        self.assertEqual(ns.parent_coordinator_route, "parent-route")
        self.assertEqual(ns.func.__name__, "cmd_handoff_delegate_launch_adopt")

    def test_missing_required_mode_rejected(self):
        with self.assertRaises(SystemExit):
            self._parse(["--target-repo", CHILD_REPO, "--parent-coordinator-route", "r"])

    def test_missing_target_repo_rejected(self):
        with self.assertRaises(SystemExit):
            self._parse(
                ["--launch-adopt-mode", "disabled", "--parent-coordinator-route", "r"]
            )

    def test_missing_parent_route_rejected(self):
        with self.assertRaises(SystemExit):
            self._parse(
                ["--launch-adopt-mode", "disabled", "--target-repo", CHILD_REPO]
            )

    def test_invalid_mode_choice_rejected(self):
        with self.assertRaises(SystemExit):
            self._parse(
                [
                    "--launch-adopt-mode", "bogus",
                    "--target-repo", CHILD_REPO,
                    "--parent-coordinator-route", "r",
                ]
            )

    def test_repeatable_callback_and_excluded_lane(self):
        ns = self._parse(
            self._base()
            + [
                "--callback-target", "owning_us_coordinator=us-route",
                "--callback-target", "audit_coordinator=audit-route",
                "--excluded-lane", "lane-a",
                "--excluded-lane", "lane-b",
                "--json",
            ]
        )
        self.assertEqual(
            ns.callback_target,
            ["owning_us_coordinator=us-route", "audit_coordinator=audit-route"],
        )
        self.assertEqual(ns.excluded_lane, ["lane-a", "lane-b"])
        self.assertTrue(ns.as_json)


class RecommendedCommandQuotingTest(unittest.TestCase):
    """The pasteable adopt recommendation must be genuinely shell-safe (#12457 j#63780)."""

    def _adopt_decision(self, repo, child_project=None):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_ADOPT_EXISTING,
            candidates=[_codex(pane_id="%19", repo_root=repo)],
            target_repo_identity=repo,
            child_project=child_project,
        )
        self.assertEqual(decision.outcome, OUTCOME_ADOPT)
        return decision

    def test_space_repo_path_stays_a_single_token(self):
        # A canonical child repo can be a Google Drive path with spaces; the
        # mandatory --target-repo gate must survive the round-trip as one token.
        space_repo = "/Users/me/Google Drive/child repo"
        decision = self._adopt_decision(space_repo, child_project="child proj")
        args = types.SimpleNamespace(
            source="redmine",
            child_issue="12457",
            journal=None,
            parent_project=None,
        )
        cmd = _recommended_command(decision, args)
        tokens = shlex.split(cmd)
        idx = tokens.index("--target-repo")
        self.assertEqual(tokens[idx + 1], space_repo)
        # The whole command re-parses to exactly the tokens it was built from.
        self.assertEqual(shlex.join(tokens), cmd)

    def test_shell_metacharacters_in_profile_values_are_quoted(self):
        decision = self._adopt_decision("/repo/child", child_project="child&proj")
        args = types.SimpleNamespace(
            source="redmine",
            child_issue="12457",
            journal=None,
            parent_project="parent; rm -rf x",
        )
        cmd = _recommended_command(decision, args)
        tokens = shlex.split(cmd)
        # Each --profile-field value survives as one KEY=VALUE argv token, so a
        # metacharacter never starts a second shell command.
        self.assertIn("parent_project=parent; rm -rf x", tokens)
        self.assertIn("child_project=child&proj", tokens)
        self.assertNotIn("rm", [t for t in tokens if t == "rm"])

    def test_non_adopt_outcome_has_no_command(self):
        decision = resolve_launch_adopt(
            mode=LAUNCH_ADOPT_MODE_DISABLED,
            candidates=[_codex()],
            target_repo_identity=CHILD_REPO,
        )
        self.assertIsNone(_recommended_command(decision, types.SimpleNamespace()))


def _target_row(**overrides):
    """A duck-typed ``agents targets`` candidate row for handler integration."""
    fields = dict(
        pane_id="%19",
        role=ROLE_CODEX,
        repo_root=CHILD_REPO,
        workspace_id="ws-child",
        workspace_label="child",
        lane_id="lane-child",
        lane_label="lane-child",
        confidence=CONFIDENCE_STRONG,
        ambiguous=False,
        session="cockpit",
        window_name="codex",
    )
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


class HandlerIntegrationTest(unittest.TestCase):
    """Drive ``cmd_handoff_delegate_launch_adopt`` with discovery patched out.

    The command is read-only and never sends, so there is nothing to mock on the
    send side — the assertion is that it resolves + prints + returns the right
    exit code from injected candidates without touching a pane.
    """

    def _run(self, argv, rows):
        ns = build_parser().parse_args(["handoff", "delegate-launch-adopt", *argv])
        buf = io.StringIO()
        with mock.patch(
            "mozyo_bridge.application.commands._agents_target_candidates",
            return_value=rows,
        ), mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.require_tmux",
            return_value=None,
        ), contextlib.redirect_stdout(buf):
            code = cmd_handoff_delegate_launch_adopt(ns)
        return code, buf.getvalue()

    def _base(self, mode):
        return [
            "--launch-adopt-mode", mode,
            "--target-repo", CHILD_REPO,
            "--parent-coordinator-route", "parent-route",
            "--child-project", "child-proj",
            "--child-issue", "12457",
        ]

    def test_adopt_prints_gated_command_and_record_exit_zero(self):
        code, out = self._run(
            self._base("adopt_existing"), [_target_row(pane_id="%19")]
        )
        self.assertEqual(code, 0)
        self.assertIn("outcome: adopt", out)
        # The only send is the recommended gated codex-gateway command, printed
        # for the operator to run — the command itself never sends.
        self.assertIn("mozyo-bridge handoff send --to codex", out)
        self.assertIn("--target %19", out)
        self.assertIn(f"--target-repo {CHILD_REPO}", out)
        self.assertIn("--role-profile delegated_coordinator", out)
        self.assertIn("record_kind: parent_delegation_decision", out)
        self.assertIn("purpose: delegation_parent", out)

    def test_ambiguous_fails_closed_nonzero_exit(self):
        code, out = self._run(
            self._base("adopt_existing"),
            [_target_row(pane_id="%19"), _target_row(pane_id="%21", lane_id="lane-b")],
        )
        self.assertEqual(code, 3)
        self.assertIn("ambiguous_candidates", out)
        # No gated send command for a non-adopt outcome.
        self.assertNotIn("mozyo-bridge handoff send", out)

    def test_disabled_fails_closed_records_avoided(self):
        code, out = self._run(self._base("disabled"), [_target_row()])
        self.assertEqual(code, 3)
        self.assertIn("delegation_disabled", out)
        self.assertIn("child_delegation: avoided", out)

    def test_missing_parent_route_dies(self):
        # Build args without the parent route by bypassing the required parser
        # flag (set to empty) to exercise the handler-side validation die path.
        ns = build_parser().parse_args(
            [
                "handoff", "delegate-launch-adopt",
                "--launch-adopt-mode", "launch_or_adopt",
                "--target-repo", CHILD_REPO,
                "--parent-coordinator-route", "x",
            ]
        )
        ns.parent_coordinator_route = ""
        with mock.patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.require_tmux", return_value=None
        ), self.assertRaises(SystemExit):
            cmd_handoff_delegate_launch_adopt(ns)

    def test_json_output_carries_decision_and_record(self):
        import json as _json

        code, out = self._run(
            self._base("launch_or_adopt") + ["--json"], [_target_row(pane_id="%19")]
        )
        self.assertEqual(code, 0)
        payload = _json.loads(out)
        self.assertEqual(payload["outcome"], "adopt")
        self.assertEqual(payload["selected"]["pane_id"], "%19")
        self.assertTrue(
            any(
                t["purpose"] == "delegation_parent"
                for t in payload["callback_targets"]
            )
        )
        self.assertIn("mozyo-bridge handoff send --to codex", payload["recommended_command"])


if __name__ == "__main__":
    unittest.main()
