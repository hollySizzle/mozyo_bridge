"""Session boundary UX tests (Redmine #12122, parent US #12113).

Covers the pure domain helpers — boundary signal classification, the
next-session boundary prompt formatter (including the public/private redaction
contract so no absolute path leaks into pasteable text), and the guarded Claude
pane lifecycle decision — plus the two CLI subcommands' exit codes and output.
Everything runs pure; no tmux, git, or Redmine I/O.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.commands import (
    cmd_session_boundary_prompt,
    cmd_session_pane_decision,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import build_execution_root
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_boundary import (
    PRESERVATION_SIGNALS,
    SESSION_BOUNDARY_SIGNALS,
    BoundaryPrompt,
    PaneLifecycleState,
    SessionBoundaryError,
    build_boundary_prompt,
    classify_boundary,
    decide_pane_lifecycle,
)


class ClassifyBoundaryTests(unittest.TestCase):
    def test_no_signals_is_not_a_boundary(self) -> None:
        result = classify_boundary([])
        self.assertFalse(result.is_boundary)
        self.assertEqual(result.fired, ())
        self.assertFalse(result.preservation_required)
        self.assertIn("continue", result.recommended_action)

    def test_scope_signal_is_a_boundary(self) -> None:
        result = classify_boundary(["gate_transition"])
        self.assertTrue(result.is_boundary)
        self.assertEqual(result.fired, ("gate_transition",))
        self.assertFalse(result.preservation_required)
        self.assertIn("next-session prompt", result.recommended_action)

    def test_preservation_only_is_not_a_scope_boundary_but_requires_record(self) -> None:
        result = classify_boundary(["dirty_diff"])
        self.assertFalse(result.is_boundary)
        self.assertTrue(result.preservation_required)
        self.assertIn("before any", result.recommended_action)

    def test_mixed_scope_and_preservation(self) -> None:
        result = classify_boundary(["dirty_diff", "active_issue_change"])
        self.assertTrue(result.is_boundary)
        self.assertTrue(result.preservation_required)
        self.assertIn("do not reset/kill", result.recommended_action)

    def test_fired_is_canonical_order_regardless_of_input_order(self) -> None:
        result = classify_boundary(["gate_transition", "active_issue_change"])
        # active_issue_change precedes gate_transition in canonical order.
        self.assertEqual(result.fired, ("active_issue_change", "gate_transition"))

    def test_duplicate_signals_collapse(self) -> None:
        result = classify_boundary(["compact_event", "compact_event"])
        self.assertEqual(result.fired, ("compact_event",))

    def test_unknown_signal_raises(self) -> None:
        with self.assertRaises(SessionBoundaryError):
            classify_boundary(["not_a_real_signal"])


class BoundaryPromptTests(unittest.TestCase):
    def _full_prompt(self, **overrides: object) -> BoundaryPrompt:
        base = dict(
            issue="12122",
            journal="59709",
            repo_pointer="mozyo-giken-3800-mozyo-bridge",
            parent_issue="12113",
            commit="abc1234",
            target_lane="issue_12122_session_boundary_ux",
            gate_state="implementation_done",
            verification_state="unit tests green",
            residual_risks=("two new CLI subcommands",),
            pending_action="create US review_request",
            next_actor="codex",
            signals=("gate_transition",),
        )
        base.update(overrides)
        return BoundaryPrompt(**base)  # type: ignore[arg-type]

    def test_prompt_carries_all_anchor_fields(self) -> None:
        text = build_boundary_prompt(self._full_prompt())
        self.assertIn("#12122 j#59709", text)
        self.assertIn("parent US `#12113`", text)
        self.assertIn("abc1234", text)
        self.assertIn("issue_12122_session_boundary_ux", text)
        self.assertIn("implementation_done", text)
        self.assertIn("unit tests green", text)
        self.assertIn("two new CLI subcommands", text)
        self.assertIn("create US review_request", text)
        self.assertIn("(codex)", text)
        self.assertIn("gate_transition", text)
        self.assertIn("read it from the source-of-truth system", text)

    def test_minimal_prompt_only_requires_issue_and_journal(self) -> None:
        text = build_boundary_prompt(
            BoundaryPrompt(issue="1", journal="2", repo_pointer="proj")
        )
        self.assertIn("#1 j#2", text)
        self.assertIn("Commit: none", text)
        self.assertIn("Residual risks: none recorded", text)

    def test_missing_anchor_raises(self) -> None:
        with self.assertRaises(SessionBoundaryError):
            BoundaryPrompt(issue="", journal="2", repo_pointer="proj")
        with self.assertRaises(SessionBoundaryError):
            BoundaryPrompt(issue="1", journal="", repo_pointer="proj")

    def test_absolute_repo_pointer_is_rejected(self) -> None:
        # The redaction contract: a pasteable pointer must be a portable
        # identifier, never an absolute / home path.
        for leaked in ("/workspace/project-alpha", "~/dev/proj", "\\\\srv\\share"):
            with self.assertRaises(SessionBoundaryError):
                BoundaryPrompt(issue="1", journal="2", repo_pointer=leaked)

    def test_invalid_next_actor_raises(self) -> None:
        with self.assertRaises(SessionBoundaryError):
            BoundaryPrompt(
                issue="1", journal="2", repo_pointer="proj", next_actor="manager"
            )

    def test_nested_execution_root_renders_portable_pointer(self) -> None:
        execution_root = build_execution_root(
            "/workspace/project-alpha/nested/service",
            repo_root_abs="/workspace/project-alpha",
        )
        text = build_boundary_prompt(
            self._full_prompt(execution_root=execution_root)
        )
        self.assertIn("`nested/service` (relative to the target repo root)", text)
        # No absolute path leaks into the pasteable prompt.
        self.assertNotIn("/workspace/project-alpha", text)

    def test_out_of_tree_execution_root_omits_absolute(self) -> None:
        execution_root = build_execution_root(
            "/somewhere/else", repo_root_abs="/workspace/project-alpha"
        )
        text = build_boundary_prompt(
            self._full_prompt(execution_root=execution_root)
        )
        self.assertNotIn("/somewhere/else", text)
        self.assertIn("execution_root.workdir", text)


class DecidePaneLifecycleTests(unittest.TestCase):
    def test_default_request_is_new(self) -> None:
        decision = decide_pane_lifecycle(PaneLifecycleState())
        self.assertEqual(decision.decision, "new")
        self.assertFalse(decision.is_blocked)

    def test_reuse_same_lane(self) -> None:
        decision = decide_pane_lifecycle(
            PaneLifecycleState(requested="reuse", same_lane=True)
        )
        self.assertEqual(decision.decision, "reuse")

    def test_reuse_cross_lane_falls_back_to_new(self) -> None:
        decision = decide_pane_lifecycle(
            PaneLifecycleState(requested="reuse", same_lane=False)
        )
        self.assertEqual(decision.decision, "new")
        self.assertIn("different lane", decision.rationale)

    def test_orphan_is_always_allowed_and_non_destructive(self) -> None:
        decision = decide_pane_lifecycle(
            PaneLifecycleState(requested="orphan", dirty_diff=True)
        )
        self.assertEqual(decision.decision, "orphan")
        self.assertIn("dirty_diff", decision.blockers)

    def test_kill_blocked_by_each_preservation_signal(self) -> None:
        for signal in PRESERVATION_SIGNALS:
            state = PaneLifecycleState(
                requested="kill", owner_approved_kill=True, **{signal: True}
            )
            decision = decide_pane_lifecycle(state)
            self.assertEqual(decision.decision, "blocked", signal)
            self.assertIn(signal, decision.blockers)

    def test_kill_blocked_without_owner_approval(self) -> None:
        decision = decide_pane_lifecycle(PaneLifecycleState(requested="kill"))
        self.assertTrue(decision.is_blocked)
        self.assertIn("owner-approval gated", decision.rationale)
        self.assertEqual(decision.blockers, ())

    def test_kill_allowed_when_clean_and_owner_approved(self) -> None:
        decision = decide_pane_lifecycle(
            PaneLifecycleState(requested="kill", owner_approved_kill=True)
        )
        self.assertEqual(decision.decision, "guarded_kill")

    def test_discard_follows_same_guard_as_kill(self) -> None:
        decision = decide_pane_lifecycle(
            PaneLifecycleState(requested="discard", unrecorded_journal=True)
        )
        self.assertTrue(decision.is_blocked)

    def test_invalid_request_raises(self) -> None:
        with self.assertRaises(SessionBoundaryError):
            decide_pane_lifecycle(PaneLifecycleState(requested="nuke"))

    def test_decision_values_are_in_the_documented_set(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_boundary import PANE_LIFECYCLE_DECISIONS

        for state in (
            PaneLifecycleState(),
            PaneLifecycleState(requested="reuse", same_lane=True),
            PaneLifecycleState(requested="orphan"),
            PaneLifecycleState(requested="kill"),
            PaneLifecycleState(requested="kill", owner_approved_kill=True),
        ):
            self.assertIn(decide_pane_lifecycle(state).decision, PANE_LIFECYCLE_DECISIONS)


class CliTests(unittest.TestCase):
    def _run(self, func, **kwargs) -> tuple[int, str]:
        ns = argparse.Namespace(**kwargs)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = func(ns)
        return code, buf.getvalue()

    def test_boundary_prompt_cli_emits_markdown_without_absolute_path(self) -> None:
        code, out = self._run(
            cmd_session_boundary_prompt,
            repo=str(ROOT),
            issue="12122",
            journal="59709",
            parent="12113",
            commit=None,
            target_lane="issue_12122_session_boundary_ux",
            execution_root=None,
            gate="implementation_done",
            verification="green",
            residual=["risk one"],
            pending_action="review_request",
            next_actor="codex",
            signal=["gate_transition"],
            as_json=False,
        )
        self.assertEqual(code, 0)
        self.assertIn("#12122 j#59709", out)
        self.assertIn("risk one", out)
        # The absolute repo root must never appear in the pasteable prompt.
        self.assertNotIn(str(ROOT), out)

    def test_boundary_prompt_cli_json_carries_absolute_repo_root(self) -> None:
        code, out = self._run(
            cmd_session_boundary_prompt,
            repo=str(ROOT),
            issue="12122",
            journal="59709",
            parent=None,
            commit=None,
            target_lane=None,
            execution_root=None,
            gate=None,
            verification=None,
            residual=None,
            pending_action=None,
            next_actor=None,
            signal=None,
            as_json=True,
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        # Structured output is allowed to carry the absolute root for automation.
        self.assertEqual(payload["repo_root"], str(ROOT))
        self.assertIn("prompt_markdown", payload)
        self.assertNotIn(str(ROOT), payload["prompt_markdown"])

    def test_pane_decision_cli_exits_three_when_blocked(self) -> None:
        code, out = self._run(
            cmd_session_pane_decision,
            requested="kill",
            same_lane=False,
            dirty_diff=True,
            running_process=False,
            pending_approval=False,
            unrecorded_journal=False,
            owner_approved_kill=False,
            as_json=False,
        )
        self.assertEqual(code, 3)
        self.assertIn("blocked", out)

    def test_pane_decision_cli_exits_zero_when_allowed(self) -> None:
        code, out = self._run(
            cmd_session_pane_decision,
            requested="kill",
            same_lane=False,
            dirty_diff=False,
            running_process=False,
            pending_approval=False,
            unrecorded_journal=False,
            owner_approved_kill=True,
            as_json=True,
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], "guarded_kill")


if __name__ == "__main__":
    unittest.main()
