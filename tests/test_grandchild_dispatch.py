"""Tests for the grandchild (depth-2) dispatch decision primitive (#12458).

Covers the pure decision resolver
(:mod:`mozyo_bridge.domain.grandchild_dispatch`): the delegation policy
normalization / fail-closed clamp, the depth-2 policy gate (master gate /
grandchild flag / depth ceiling / active-lane capacity), the grandchild dispatch
decision reusing the #12457 launch/adopt selector (adopt / launch / fail-closed,
never a direct grandchild Claude, identity over display proximity), the explicit
no-dispatch (`grandchild_dispatch: avoided`) path, the multi-coordinator callback
coverage validation, the visible-lane (no hidden subagent) invariant, and the CLI
handler integration + parser surface.
"""

from __future__ import annotations

import contextlib
import io
import shlex
import types
import unittest
from unittest import mock

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.application.grandchild_dispatch import (
    _recommended_command,
    cmd_handoff_grandchild_dispatch,
)
from mozyo_bridge.domain.delegation_launch_adopt import (
    CONFIDENCE_STRONG,
    CallbackTarget,
    DelegationCandidate,
    DelegationLaunchAdoptError,
    PURPOSE_AUDIT_COORDINATOR,
    PURPOSE_DELEGATION_PARENT,
    PURPOSE_OWNING_US_COORDINATOR,
    REASON_AMBIGUOUS_CANDIDATES,
    REASON_MISSING_TARGET_REPO_IDENTITY,
    ROLE_CLAUDE,
    ROLE_CODEX,
)
from mozyo_bridge.domain.grandchild_dispatch import (
    DEFAULT_DELEGATED_COORDINATOR_DEPTH,
    HARD_CEILING_DEPTH,
    OUTCOME_DISPATCH_ADOPT,
    OUTCOME_DISPATCH_LAUNCH,
    OUTCOME_FAIL_CLOSED,
    OUTCOME_NO_DISPATCH,
    OWNING_COVERAGE_SAME_AS_PARENT,
    PURPOSE_PRESERVE_CONTEXT,
    REASON_ACTIVE_LANE_CAPACITY_EXHAUSTED,
    REASON_DEPTH_CEILING_EXCEEDED,
    REASON_GRANDCHILD_DISABLED,
    REASON_MASTER_GATE_DISABLED,
    DelegationPolicy,
    effective_policy,
    resolve_grandchild_dispatch,
    resolve_grandchild_policy_gate,
    resolve_no_dispatch,
    validate_grandchild_callback_targets,
)

GRANDCHILD_REPO = "/workspace/project-grandchild"


def _open_policy(**overrides) -> DelegationPolicy:
    """A policy that fully opens depth-2 grandchild dispatch (master+flag+depth 2)."""
    fields = dict(
        enable_delegated_coordinator=True,
        enable_grandchild_dispatch=True,
        max_delegation_depth=2,
        max_active_child_lanes=1,
    )
    fields.update(overrides)
    return DelegationPolicy(**fields)


def _codex(
    pane_id="%41",
    *,
    repo_root=GRANDCHILD_REPO,
    lane_id="lane-grandchild",
    confidence=CONFIDENCE_STRONG,
    ambiguous=False,
    session="cockpit",
    window_name="codex",
):
    return DelegationCandidate(
        pane_id=pane_id,
        role=ROLE_CODEX,
        repo_root=repo_root,
        workspace_id="ws-grandchild",
        workspace_label="grandchild",
        lane_id=lane_id,
        lane_label=lane_id,
        confidence=confidence,
        ambiguous=ambiguous,
        session=session,
        window_name=window_name,
    )


def _claude(pane_id="%42", *, repo_root=GRANDCHILD_REPO, lane_id="lane-grandchild"):
    return DelegationCandidate(
        pane_id=pane_id,
        role=ROLE_CLAUDE,
        repo_root=repo_root,
        workspace_id="ws-grandchild",
        workspace_label="grandchild",
        lane_id=lane_id,
        lane_label=lane_id,
        confidence=CONFIDENCE_STRONG,
        ambiguous=False,
        session="cockpit",
        window_name="claude",
    )


class EffectivePolicyTest(unittest.TestCase):
    def test_open_policy_permits_grandchild(self):
        eff = effective_policy(_open_policy())
        self.assertEqual(eff.effective_max_depth, 2)
        self.assertTrue(eff.grandchild_permitted)
        self.assertEqual(eff.diagnostics, ())

    def test_master_gate_off_clamps_effective_depth_to_zero(self):
        eff = effective_policy(_open_policy(enable_delegated_coordinator=False))
        self.assertEqual(eff.effective_max_depth, 0)
        self.assertFalse(eff.grandchild_permitted)
        self.assertTrue(any("master_gate" in d for d in eff.diagnostics))

    def test_depth_above_ceiling_clamps_to_zero_with_diagnostic(self):
        eff = effective_policy(_open_policy(max_delegation_depth=5))
        self.assertEqual(eff.effective_max_depth, 0)
        self.assertTrue(any("invalid_max_delegation_depth" in d for d in eff.diagnostics))

    def test_negative_depth_clamps_to_zero(self):
        eff = effective_policy(_open_policy(max_delegation_depth=-1))
        self.assertEqual(eff.effective_max_depth, 0)

    def test_bool_depth_is_invalid(self):
        # A bool is an int subclass; it must not be accepted as a depth value.
        eff = effective_policy(_open_policy(max_delegation_depth=True))
        self.assertEqual(eff.effective_max_depth, 0)
        self.assertTrue(any("invalid_max_delegation_depth" in d for d in eff.diagnostics))

    def test_active_lanes_below_one_clamps_to_one(self):
        eff = effective_policy(_open_policy(max_active_child_lanes=0))
        self.assertEqual(eff.effective_max_active_child_lanes, 1)
        self.assertTrue(
            any("invalid_max_active_child_lanes" in d for d in eff.diagnostics)
        )

    def test_unknown_record_policy_clamps_to_minimal(self):
        eff = effective_policy(_open_policy(decision_record_policy="loud"))
        self.assertEqual(eff.decision_record_policy, "minimal")
        self.assertTrue(
            any("invalid_decision_record_policy" in d for d in eff.diagnostics)
        )

    def test_grandchild_flag_alone_without_depth_two_not_permitted(self):
        # enable_grandchild_dispatch true but depth < 2 -> stricter side wins.
        eff = effective_policy(_open_policy(max_delegation_depth=1))
        self.assertFalse(eff.grandchild_permitted)


class PolicyGateTest(unittest.TestCase):
    def test_open_policy_permits_grandchild_lane(self):
        gate = resolve_grandchild_policy_gate(_open_policy())
        self.assertTrue(gate.permitted)
        self.assertIsNone(gate.reason)
        self.assertEqual(gate.new_lane_depth, 2)

    def test_master_gate_disabled_fails_closed(self):
        gate = resolve_grandchild_policy_gate(
            _open_policy(enable_delegated_coordinator=False)
        )
        self.assertFalse(gate.permitted)
        self.assertEqual(gate.reason, REASON_MASTER_GATE_DISABLED)

    def test_grandchild_flag_disabled_fails_closed(self):
        gate = resolve_grandchild_policy_gate(
            _open_policy(enable_grandchild_dispatch=False)
        )
        self.assertFalse(gate.permitted)
        self.assertEqual(gate.reason, REASON_GRANDCHILD_DISABLED)

    def test_depth_below_two_fails_closed(self):
        gate = resolve_grandchild_policy_gate(_open_policy(max_delegation_depth=1))
        self.assertFalse(gate.permitted)
        self.assertEqual(gate.reason, REASON_DEPTH_CEILING_EXCEEDED)

    def test_above_ceiling_depth_fails_closed_not_widened(self):
        # An out-of-range depth must not widen the ceiling; it clamps and fails.
        gate = resolve_grandchild_policy_gate(_open_policy(max_delegation_depth=9))
        self.assertFalse(gate.permitted)
        self.assertEqual(gate.reason, REASON_DEPTH_CEILING_EXCEEDED)

    def test_active_lane_capacity_exhausted_fails_closed(self):
        gate = resolve_grandchild_policy_gate(
            _open_policy(max_active_child_lanes=1), active_grandchild_lanes=1
        )
        self.assertFalse(gate.permitted)
        self.assertEqual(gate.reason, REASON_ACTIVE_LANE_CAPACITY_EXHAUSTED)

    def test_master_gate_checked_before_grandchild_flag(self):
        # Both off -> master gate reason wins (priority order).
        gate = resolve_grandchild_policy_gate(
            DelegationPolicy(
                enable_delegated_coordinator=False,
                enable_grandchild_dispatch=False,
                max_delegation_depth=2,
            )
        )
        self.assertEqual(gate.reason, REASON_MASTER_GATE_DISABLED)

    def test_higher_current_depth_pushes_past_ceiling(self):
        # A delegated coordinator already at depth 2 cannot open a depth-3 lane.
        gate = resolve_grandchild_policy_gate(_open_policy(), current_depth=2)
        self.assertFalse(gate.permitted)
        self.assertEqual(gate.reason, REASON_DEPTH_CEILING_EXCEEDED)
        self.assertEqual(gate.new_lane_depth, 3)


class ResolveGrandchildDispatchTest(unittest.TestCase):
    def test_permitted_unique_codex_is_dispatch_adopt(self):
        decision = resolve_grandchild_dispatch(
            policy=_open_policy(),
            mode="adopt_existing",
            candidates=[_codex(pane_id="%41")],
            target_repo_identity=GRANDCHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_DISPATCH_ADOPT)
        self.assertTrue(decision.is_adopt)
        self.assertEqual(decision.selected.pane_id, "%41")
        self.assertEqual(decision.delegation_depth, 2)
        self.assertTrue(decision.visible_lane_required)
        self.assertEqual(decision.purpose, PURPOSE_PRESERVE_CONTEXT)

    def test_permitted_zero_candidates_launch_or_adopt_is_dispatch_launch(self):
        decision = resolve_grandchild_dispatch(
            policy=_open_policy(),
            mode="launch_or_adopt",
            candidates=[],
            target_repo_identity=GRANDCHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_DISPATCH_LAUNCH)
        self.assertTrue(decision.is_launch)
        self.assertIsNone(decision.selected)
        self.assertTrue(decision.visible_lane_required)

    def test_policy_disabled_fails_closed_before_selection(self):
        # Even with a perfect unique candidate, a disabled policy fails closed and
        # never consults the candidate (decision recorded before any mutation).
        decision = resolve_grandchild_dispatch(
            policy=_open_policy(enable_grandchild_dispatch=False),
            mode="adopt_existing",
            candidates=[_codex(pane_id="%41")],
            target_repo_identity=GRANDCHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_GRANDCHILD_DISABLED)
        self.assertIsNone(decision.launch_adopt)
        self.assertFalse(decision.policy_gate.permitted)

    def test_ambiguous_candidates_fail_closed_inherited_reason(self):
        decision = resolve_grandchild_dispatch(
            policy=_open_policy(),
            mode="adopt_existing",
            candidates=[
                _codex(pane_id="%41", lane_id="lane-a"),
                _codex(pane_id="%43", lane_id="lane-b"),
            ],
            target_repo_identity=GRANDCHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_AMBIGUOUS_CANDIDATES)
        self.assertIsNotNone(decision.launch_adopt)

    def test_missing_target_repo_identity_fails_closed(self):
        decision = resolve_grandchild_dispatch(
            policy=_open_policy(),
            mode="adopt_existing",
            candidates=[_codex()],
            target_repo_identity=None,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_MISSING_TARGET_REPO_IDENTITY)

    def test_claude_candidate_never_dispatched(self):
        # A same-repo grandchild Claude pane must not satisfy the route.
        decision = resolve_grandchild_dispatch(
            policy=_open_policy(),
            mode="adopt_existing",
            candidates=[_claude(pane_id="%42")],
            target_repo_identity=GRANDCHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertNotEqual(getattr(decision.selected, "role", None), ROLE_CLAUDE)

    def test_codex_adopted_over_sibling_claude(self):
        decision = resolve_grandchild_dispatch(
            policy=_open_policy(),
            mode="adopt_existing",
            candidates=[_claude(pane_id="%42"), _codex(pane_id="%41")],
            target_repo_identity=GRANDCHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_DISPATCH_ADOPT)
        self.assertEqual(decision.selected.pane_id, "%41")

    def test_unknown_mode_raises(self):
        with self.assertRaises(DelegationLaunchAdoptError):
            resolve_grandchild_dispatch(
                policy=_open_policy(),
                mode="bogus",
                candidates=[_codex()],
                target_repo_identity=GRANDCHILD_REPO,
            )

    def test_selection_ignores_display_proximity(self):
        # Two codex candidates differing only in session/window are still
        # ambiguous: display facts are never a tie-breaker / selection signal.
        decision = resolve_grandchild_dispatch(
            policy=_open_policy(),
            mode="adopt_existing",
            candidates=[
                _codex(pane_id="%41", lane_id="lane-a", session="cockpit", window_name="codex"),
                _codex(pane_id="%43", lane_id="lane-b", session="other", window_name="codex-2"),
            ],
            target_repo_identity=GRANDCHILD_REPO,
        )
        self.assertEqual(decision.outcome, OUTCOME_FAIL_CLOSED)
        self.assertEqual(decision.reason, REASON_AMBIGUOUS_CANDIDATES)


class NoDispatchTest(unittest.TestCase):
    def test_no_dispatch_records_avoided_reason(self):
        decision = resolve_no_dispatch(
            policy=_open_policy(),
            no_dispatch_reason="context_cost_low",
        )
        self.assertEqual(decision.outcome, OUTCOME_NO_DISPATCH)
        self.assertTrue(decision.is_no_dispatch)
        self.assertEqual(decision.no_dispatch_reason, "context_cost_low")
        self.assertIsNone(decision.selected)

    def test_no_dispatch_allowed_even_when_policy_would_permit(self):
        # The delegated coordinator may always keep work in its own lane.
        decision = resolve_no_dispatch(
            policy=_open_policy(), no_dispatch_reason="single_pass_no_iteration"
        )
        self.assertTrue(decision.is_no_dispatch)

    def test_blank_reason_raises(self):
        for reason in ("", "   "):
            with self.assertRaises(DelegationLaunchAdoptError):
                resolve_no_dispatch(policy=_open_policy(), no_dispatch_reason=reason)


class CallbackCoverageTest(unittest.TestCase):
    def _parent(self):
        return CallbackTarget(purpose=PURPOSE_DELEGATION_PARENT, route="gk-parent-route")

    def _owning(self):
        return CallbackTarget(
            purpose=PURPOSE_OWNING_US_COORDINATOR, route="mozyo-coordinator-route"
        )

    def test_distinct_owning_target_satisfies_coverage(self):
        targets = validate_grandchild_callback_targets([self._parent(), self._owning()])
        self.assertEqual(len(targets), 2)

    def test_audit_target_satisfies_coverage(self):
        audit = CallbackTarget(
            purpose=PURPOSE_AUDIT_COORDINATOR, route="audit-route", required=False
        )
        targets = validate_grandchild_callback_targets([self._parent(), audit])
        self.assertEqual(len(targets), 2)

    def test_same_as_parent_declaration_satisfies_coverage(self):
        targets = validate_grandchild_callback_targets(
            [self._parent()], owning_coverage=OWNING_COVERAGE_SAME_AS_PARENT
        )
        self.assertEqual(len(targets), 1)

    def test_lone_parent_without_coverage_fails_closed(self):
        with self.assertRaises(DelegationLaunchAdoptError):
            validate_grandchild_callback_targets([self._parent()])

    def test_missing_parent_fails_closed(self):
        # Inherited #12457 invariant: delegation_parent is mandatory.
        with self.assertRaises(DelegationLaunchAdoptError):
            validate_grandchild_callback_targets([self._owning()])

    def test_unknown_owning_coverage_token_raises(self):
        with self.assertRaises(DelegationLaunchAdoptError):
            validate_grandchild_callback_targets(
                [self._parent()], owning_coverage="bogus"
            )


class DecisionShapeTest(unittest.TestCase):
    def test_to_dict_carries_policy_and_depth(self):
        decision = resolve_grandchild_dispatch(
            policy=_open_policy(),
            mode="adopt_existing",
            candidates=[_codex(pane_id="%41")],
            target_repo_identity=GRANDCHILD_REPO,
        )
        d = decision.to_dict()
        self.assertEqual(d["outcome"], OUTCOME_DISPATCH_ADOPT)
        self.assertEqual(d["delegation_depth"], 2)
        self.assertTrue(d["visible_lane_required"])
        self.assertTrue(d["policy_permitted"])
        self.assertEqual(d["effective_policy"]["effective_max_depth"], 2)
        self.assertEqual(d["selected"]["pane_id"], "%41")

    def test_hard_ceiling_and_default_depth_constants(self):
        self.assertEqual(HARD_CEILING_DEPTH, 2)
        self.assertEqual(DEFAULT_DELEGATED_COORDINATOR_DEPTH, 1)


def _target_row(**overrides):
    """A duck-typed ``agents targets`` candidate row for handler integration."""
    fields = dict(
        pane_id="%41",
        role=ROLE_CODEX,
        repo_root=GRANDCHILD_REPO,
        workspace_id="ws-grandchild",
        workspace_label="grandchild",
        lane_id="lane-grandchild",
        lane_label="lane-grandchild",
        confidence=CONFIDENCE_STRONG,
        ambiguous=False,
        session="cockpit",
        window_name="codex",
    )
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


class RecommendedCommandTest(unittest.TestCase):
    def test_only_adopt_yields_command(self):
        launch = resolve_grandchild_dispatch(
            policy=_open_policy(),
            mode="launch_new",
            candidates=[],
            target_repo_identity=GRANDCHILD_REPO,
        )
        self.assertIsNone(_recommended_command(launch, types.SimpleNamespace()))

    def test_adopt_command_is_shell_safe(self):
        space_repo = "/workspace/Google Drive/grandchild repo"
        decision = resolve_grandchild_dispatch(
            policy=_open_policy(),
            mode="adopt_existing",
            candidates=[_codex(pane_id="%41", repo_root=space_repo)],
            target_repo_identity=space_repo,
        )
        args = types.SimpleNamespace(source="redmine", child_issue="12458", journal="63949")
        cmd = _recommended_command(decision, args)
        self.assertIsNotNone(cmd)
        # The space-containing repo stays a single argv token through shlex.
        tokens = shlex.split(cmd)
        idx = tokens.index("--target-repo")
        self.assertEqual(tokens[idx + 1], space_repo)
        self.assertIn("implementation_gateway", tokens)
        self.assertIn("%41", tokens)


class HandlerIntegrationTest(unittest.TestCase):
    """Drive ``cmd_handoff_grandchild_dispatch`` with discovery patched out.

    Read-only and never sends — the assertion is that it resolves + prints + the
    right exit code from injected candidates without touching a pane.
    """

    def _run(self, argv, rows):
        ns = build_parser().parse_args(
            ["handoff", "delegate-grandchild-dispatch", *argv]
        )
        buf = io.StringIO()
        with mock.patch(
            "mozyo_bridge.application.commands._agents_target_candidates",
            return_value=rows,
        ), mock.patch(
            "mozyo_bridge.infrastructure.tmux_client.require_tmux",
            return_value=None,
        ), contextlib.redirect_stdout(buf):
            code = cmd_handoff_grandchild_dispatch(ns)
        return code, buf.getvalue()

    def _base(self, mode, *, open_policy=True):
        argv = [
            "--launch-adopt-mode", mode,
            "--target-repo", GRANDCHILD_REPO,
            "--parent-coordinator-route", "gk-parent-route",
            "--owning-coordinator-route", "mozyo-coordinator-route",
            "--child-project", "mozyo_bridge",
            "--child-issue", "12459",
            "--parent-issue", "12454",
        ]
        if open_policy:
            argv += [
                "--enable-delegated-coordinator",
                "--enable-grandchild-dispatch",
                "--max-delegation-depth", "2",
            ]
        return argv

    def test_adopt_prints_gated_command_and_records_exit_zero(self):
        code, out = self._run(self._base("adopt_existing"), [_target_row(pane_id="%41")])
        self.assertEqual(code, 0)
        self.assertIn("outcome: dispatch_adopt", out)
        self.assertIn("mozyo-bridge handoff send --to codex", out)
        self.assertIn("--target %41", out)
        self.assertIn(f"--target-repo {GRANDCHILD_REPO}", out)
        self.assertIn("--role-profile implementation_gateway", out)
        # §2 dispatch decision record + §4 callback targets record.
        self.assertIn("record_kind: delegated_dispatch_decision", out)
        self.assertIn("grandchild_dispatch: dispatched", out)
        self.assertIn("delegation_depth: 2", out)
        self.assertIn("record_kind: delegated_callback_targets", out)
        self.assertIn("purpose: delegation_parent", out)
        self.assertIn("purpose: owning_us_coordinator", out)
        self.assertIn("never a hidden subagent", out)

    def test_policy_disabled_fails_closed_nonzero(self):
        code, out = self._run(
            self._base("adopt_existing", open_policy=False), [_target_row(pane_id="%41")]
        )
        self.assertEqual(code, 3)
        self.assertIn("master_gate_disabled", out)
        self.assertNotIn("mozyo-bridge handoff send", out)

    def test_grandchild_flag_off_fails_closed(self):
        argv = self._base("adopt_existing", open_policy=False) + [
            "--enable-delegated-coordinator",
            "--max-delegation-depth", "2",
        ]
        code, out = self._run(argv, [_target_row(pane_id="%41")])
        self.assertEqual(code, 3)
        self.assertIn("grandchild_dispatch_disabled", out)

    def test_ambiguous_fails_closed_nonzero(self):
        code, out = self._run(
            self._base("adopt_existing"),
            [_target_row(pane_id="%41"), _target_row(pane_id="%43", lane_id="lane-b")],
        )
        self.assertEqual(code, 3)
        self.assertIn("ambiguous_candidates", out)

    def test_no_dispatch_path_skips_discovery(self):
        # --no-dispatch records avoided without any tmux discovery; patch nothing.
        ns = build_parser().parse_args(
            [
                "handoff", "delegate-grandchild-dispatch",
                "--parent-coordinator-route", "gk-parent-route",
                "--owning-same-as-parent",
                "--no-dispatch", "context_cost_low",
                "--child-project", "mozyo_bridge",
            ]
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cmd_handoff_grandchild_dispatch(ns)
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("outcome: no_dispatch", out)
        self.assertIn("grandchild_dispatch: avoided", out)
        self.assertIn("no_dispatch_reason: context_cost_low", out)

    def test_lone_parent_route_dies_on_coverage(self):
        ns = build_parser().parse_args(
            [
                "handoff", "delegate-grandchild-dispatch",
                "--launch-adopt-mode", "launch_or_adopt",
                "--target-repo", GRANDCHILD_REPO,
                "--parent-coordinator-route", "gk-parent-route",
                "--enable-delegated-coordinator",
                "--enable-grandchild-dispatch",
                "--max-delegation-depth", "2",
            ]
        )
        with mock.patch(
            "mozyo_bridge.infrastructure.tmux_client.require_tmux", return_value=None
        ), self.assertRaises(SystemExit):
            cmd_handoff_grandchild_dispatch(ns)

    def test_json_output_carries_decision_records_and_coverage(self):
        import json as _json

        code, out = self._run(
            self._base("launch_or_adopt") + ["--json"], [_target_row(pane_id="%41")]
        )
        self.assertEqual(code, 0)
        payload = _json.loads(out)
        self.assertEqual(payload["outcome"], "dispatch_adopt")
        self.assertEqual(payload["delegation_depth"], 2)
        self.assertTrue(payload["visible_lane_required"])
        self.assertEqual(payload["selected"]["pane_id"], "%41")
        purposes = {t["purpose"] for t in payload["callback_targets"]}
        self.assertIn("delegation_parent", purposes)
        self.assertIn("owning_us_coordinator", purposes)
        self.assertIn("implementation_gateway", payload["recommended_command"])
        self.assertIn("record_kind: delegated_dispatch_decision", payload["dispatch_decision_record"])


class ParserSurfaceTest(unittest.TestCase):
    def test_subcommand_registered_under_handoff(self):
        ns = build_parser().parse_args(
            [
                "handoff", "delegate-grandchild-dispatch",
                "--parent-coordinator-route", "r",
                "--owning-same-as-parent",
                "--no-dispatch", "context_cost_low",
            ]
        )
        self.assertEqual(ns.func, cmd_handoff_grandchild_dispatch)
        self.assertTrue(ns.owning_same_as_parent)
        self.assertEqual(ns.no_dispatch, "context_cost_low")

    def test_parent_route_is_required(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["handoff", "delegate-grandchild-dispatch", "--no-dispatch", "x"]
            )


if __name__ == "__main__":
    unittest.main()
