"""Cockpit peer adopt — bind a role-less pane as a Unit's missing peer (Redmine #12133).

`mozyo cockpit peer-adopt` is the first safe repair slice of US #12132: it adopts a
role-less cockpit pane as the *missing peer role* of an existing Unit by binding the
pane's identity options only — never a pane move / kill / split / rebalance. These
tests pin the pure, fail-closed planner (:func:`plan_peer_adopt`) across the happy
case and every guard, the apply executor (including its role-less rollback on a
mid-bind failure), and the read-only CLI wiring (preview / json / confirmed apply),
all hermetic (no live tmux). The load-bearing case is the #12130 manual-recovery
drift: a half-bound role-less pane (`%1106`) is adopted as its workspace Unit's
missing claude, resolving both the `missing_claude` and `role_less_pane` findings.
Synthetic, neutral identifiers only.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.cockpit_geometry import (
    PEER_ADOPT_CANDIDATE_NOT_IN_COCKPIT,
    PEER_ADOPT_CANDIDATE_NOT_ROLE_LESS,
    PEER_ADOPT_COCKPIT_ABSENT,
    PEER_ADOPT_CWD_CONTRADICTS_LANE,
    PEER_ADOPT_CWD_CONTRADICTS_WORKSPACE,
    PEER_ADOPT_INVALID_ROLE,
    PEER_ADOPT_NO_PEER_ANCHOR,
    PEER_ADOPT_OK,
    PEER_ADOPT_PROCESS_CONTRADICTS_ROLE,
    PEER_ADOPT_ROLE_ALREADY_PRESENT,
    PEER_ADOPT_UNIT_NOT_FOUND,
    PeerAdoptCandidate,
    PeerAdoptTarget,
    diagnose_cockpit_geometry,
    format_peer_adopt_text,
    plan_peer_adopt,
)


def _pane(
    pane_id,
    *,
    workspace_id="",
    role="",
    lane_id="default",
    left=0,
    top=0,
    width=80,
    height=40,
):
    return {
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "role": role,
        "lane_id": lane_id,
        "pane_left": left,
        "pane_top": top,
        "pane_width": width,
        "pane_height": height,
    }


def _drift_panes():
    """The #12130 drift: Unit `video` has a codex pane and a role-less `%1106`."""
    return [
        _pane("%1104", workspace_id="video", role="codex", left=0, top=0, width=41, height=39),
        _pane("%1106", left=0, top=39, width=41, height=17),  # role-less
    ]


def _diagnose(panes=None, session="mozyo-cockpit"):
    return diagnose_cockpit_geometry(
        session=session, panes=_drift_panes() if panes is None else panes
    )


class PlannerTest(unittest.TestCase):
    """The pure fail-closed planner: happy path + every guard (Redmine #12133)."""

    def _target(self, workspace_id="video", lane_id="default", lane_label=None):
        return PeerAdoptTarget(
            workspace_id=workspace_id, lane_id=lane_id, lane_label=lane_label, label=workspace_id
        )

    def _candidate(self, **kw):
        kw.setdefault("pane_id", "%1106")
        return PeerAdoptCandidate(**kw)

    def test_happy_adopts_missing_claude_with_identity_binds(self) -> None:
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(lane_label="feature-x"),
            pane_id="%1106",
            role="claude",
            candidate=self._candidate(cwd_workspace_id="video", cwd_lane_id="default"),
        )
        self.assertTrue(decision.ok)
        self.assertEqual(PEER_ADOPT_OK, decision.reason_code)
        plan = decision.plan
        self.assertIsNotNone(plan)
        self.assertEqual("%1106", plan.pane_id)
        self.assertEqual("claude", plan.role)
        self.assertEqual(("%1104",), plan.peer_panes)
        # Identity binds: workspace / role / lane (+ lane_label) — nothing else.
        argvs = [c.argv for c in plan.stamp_commands]
        self.assertIn(("set-option", "-p", "-t", "%1106", "@mozyo_workspace_id", "video"), argvs)
        self.assertIn(("set-option", "-p", "-t", "%1106", "@mozyo_agent_role", "claude"), argvs)
        self.assertIn(("set-option", "-p", "-t", "%1106", "@mozyo_lane_id", "default"), argvs)
        self.assertIn(("set-option", "-p", "-t", "%1106", "@mozyo_lane_label", "feature-x"), argvs)
        # No join / kill / split / move / rebalance commands anywhere.
        verbs = {c.argv[0] for c in plan.stamp_commands}
        self.assertTrue(verbs <= {"select-pane", "set-option"}, verbs)

    def test_missing_codex_unit_adopts_codex_peer(self) -> None:
        # Symmetric direction: a Unit with only a claude pane adopts a codex peer.
        panes = [
            _pane("%200", workspace_id="audio", role="claude", left=0, top=20, width=40, height=20),
            _pane("%201", left=0, top=0, width=40, height=19),  # role-less
        ]
        decision = plan_peer_adopt(
            diagnosis=_diagnose(panes),
            target=self._target(workspace_id="audio"),
            pane_id="%201",
            role="codex",
            candidate=self._candidate(pane_id="%201"),
        )
        self.assertTrue(decision.ok)
        self.assertEqual(("%200",), decision.plan.peer_panes)

    def test_block_cockpit_absent(self) -> None:
        decision = plan_peer_adopt(
            diagnosis=diagnose_cockpit_geometry(session="mozyo-cockpit", panes=None),
            target=self._target(),
            pane_id="%1106",
            role="claude",
            candidate=self._candidate(),
        )
        self.assertFalse(decision.ok)
        self.assertEqual(PEER_ADOPT_COCKPIT_ABSENT, decision.reason_code)

    def test_block_invalid_role(self) -> None:
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(),
            pane_id="%1106",
            role="owner",
            candidate=self._candidate(),
        )
        self.assertEqual(PEER_ADOPT_INVALID_ROLE, decision.reason_code)

    def test_block_candidate_not_in_cockpit(self) -> None:
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(),
            pane_id="%9999",
            role="claude",
            candidate=self._candidate(pane_id="%9999"),
        )
        self.assertEqual(PEER_ADOPT_CANDIDATE_NOT_IN_COCKPIT, decision.reason_code)

    def test_block_candidate_not_role_less(self) -> None:
        # An already-identified pane is never re-homed.
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(),
            pane_id="%1104",
            role="claude",
            candidate=self._candidate(pane_id="%1104"),
        )
        self.assertEqual(PEER_ADOPT_CANDIDATE_NOT_ROLE_LESS, decision.reason_code)

    def test_block_unit_not_found(self) -> None:
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(workspace_id="ghost"),
            pane_id="%1106",
            role="claude",
            candidate=self._candidate(),
        )
        self.assertEqual(PEER_ADOPT_UNIT_NOT_FOUND, decision.reason_code)

    def test_block_role_already_present(self) -> None:
        # The Unit already has codex; there is no missing codex peer.
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(),
            pane_id="%1106",
            role="codex",
            candidate=self._candidate(),
        )
        self.assertEqual(PEER_ADOPT_ROLE_ALREADY_PRESENT, decision.reason_code)

    def test_block_cwd_contradicts_workspace(self) -> None:
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(),
            pane_id="%1106",
            role="claude",
            candidate=self._candidate(cwd_workspace_id="audio"),
        )
        self.assertEqual(PEER_ADOPT_CWD_CONTRADICTS_WORKSPACE, decision.reason_code)

    def test_block_cwd_contradicts_lane(self) -> None:
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(lane_id="default"),
            pane_id="%1106",
            role="claude",
            candidate=self._candidate(cwd_workspace_id="video", cwd_lane_id="worktree-2"),
        )
        self.assertEqual(PEER_ADOPT_CWD_CONTRADICTS_LANE, decision.reason_code)

    def test_unknown_cwd_is_permitted(self) -> None:
        # An unresolvable cwd is "unknown", not a contradiction — it must not block.
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(),
            pane_id="%1106",
            role="claude",
            candidate=self._candidate(cwd_workspace_id="", cwd_lane_id=""),
        )
        self.assertTrue(decision.ok)

    def test_block_process_contradicts_role(self) -> None:
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(),
            pane_id="%1106",
            role="claude",
            candidate=self._candidate(process_role="codex", process_name="codex"),
        )
        self.assertEqual(PEER_ADOPT_PROCESS_CONTRADICTS_ROLE, decision.reason_code)

    def test_matching_process_is_permitted(self) -> None:
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(),
            pane_id="%1106",
            role="claude",
            candidate=self._candidate(process_role="claude", process_name="claude"),
        )
        self.assertTrue(decision.ok)

    def test_blocked_text_names_reason(self) -> None:
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=self._target(),
            pane_id="%1106",
            role="codex",
            candidate=self._candidate(),
        )
        text = format_peer_adopt_text(decision)
        self.assertIn("blocked", text)
        self.assertIn(PEER_ADOPT_ROLE_ALREADY_PRESENT, text)


class NoPeerAnchorTest(unittest.TestCase):
    """The no_peer_anchor guard via a hand-built diagnosis (Redmine #12133)."""

    def test_no_peer_anchor_guard(self) -> None:
        # Build a GeometryUnit that carries neither role's pane for the request:
        # a unit present in `units` but with empty codex/claude tuples cannot arise
        # from the real diagnoser, so we synthesize one to pin the backstop guard.
        from mozyo_bridge.domain.cockpit_geometry import (
            GeometryDiagnosis,
            GeometryUnit,
            PaneGeometry,
        )

        pane = PaneGeometry(
            pane_id="%400", workspace_id="", role="", lane_id="default",
            pane_left=0, pane_top=0, pane_width=40, pane_height=40,
        )
        empty_unit = GeometryUnit(
            workspace_id="empty", lane_id="default",
            codex_panes=(), claude_panes=(), columns=(0,),
        )
        diagnosis = GeometryDiagnosis(
            session="mozyo-cockpit", cockpit_present=True,
            panes=(pane,), columns=(), units=(empty_unit,), findings=(),
        )
        decision = plan_peer_adopt(
            diagnosis=diagnosis,
            target=PeerAdoptTarget(workspace_id="empty", lane_id="default"),
            pane_id="%400",
            role="claude",
            candidate=PeerAdoptCandidate(pane_id="%400"),
        )
        self.assertEqual(PEER_ADOPT_NO_PEER_ANCHOR, decision.reason_code)


class ExecutorTest(unittest.TestCase):
    """The apply executor: runs binds, rolls back role-less on failure (Redmine #12133)."""

    def _plan(self):
        decision = plan_peer_adopt(
            diagnosis=_diagnose(),
            target=PeerAdoptTarget(workspace_id="video", lane_id="default", label="video"),
            pane_id="%1106",
            role="claude",
            candidate=PeerAdoptCandidate(pane_id="%1106"),
        )
        self.assertTrue(decision.ok)
        return decision.plan

    def test_runs_every_bind(self) -> None:
        from mozyo_bridge.application.commands import execute_peer_adopt_plan

        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        calls = []

        def run(*argv, check=True):
            calls.append(argv)
            return _Ok()

        execute_peer_adopt_plan(self._plan(), run)
        # Every stamp command ran; none were unset (no rollback on success).
        self.assertTrue(any(a[0] == "set-option" and a[-2] == "@mozyo_agent_role" for a in calls))
        self.assertFalse(any("-u" in a for a in calls))

    def test_rolls_back_role_less_on_failure(self) -> None:
        from mozyo_bridge.application import commands

        class _Res:
            def __init__(self, rc):
                self.returncode = rc
                self.stdout = ""
                self.stderr = "boom" if rc else ""

        calls = []

        def run(*argv, check=True):
            calls.append(argv)
            # Fail when binding the lane option, after workspace + role bound.
            if argv[0] == "set-option" and argv[-2] == "@mozyo_lane_id":
                return _Res(1)
            return _Res(0)

        with self.assertRaises(SystemExit):
            commands.execute_peer_adopt_plan(self._plan(), run)
        # The two successfully-bound options are unset (reverse order) to restore
        # the pane's role-less state — never left half-bound.
        unsets = [a for a in calls if "set-option" == a[0] and "-u" in a]
        unset_options = [a[-1] for a in unsets]
        self.assertEqual(["@mozyo_agent_role", "@mozyo_workspace_id"], unset_options)


class CliWiringTest(unittest.TestCase):
    """`mozyo cockpit peer-adopt` preview / json / confirmed apply (Redmine #12133)."""

    def _args(self, **kw):
        base = dict(
            action="peer-adopt",
            cockpit_session=None,
            json_output=False,
            dry_run=False,
            confirm=False,
            peer_pane="%1106",
            peer_unit="video/default",
            peer_role="claude",
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _runtime(self, pane_id):
        # Hermetic: unknown cwd / neutral process / no lane label for every pane,
        # so the preflight is "unknown" (permitted) and no filesystem resolution runs.
        return {"cwd": "", "process": "", "lane_label": ""}

    def test_preview_without_confirm_mutates_nothing(self) -> None:
        from mozyo_bridge.application import commands

        buf = io.StringIO()
        with patch.object(commands, "_read_cockpit_geometry", return_value=_drift_panes()):
            with patch.object(commands, "_read_cockpit_pane_runtime", side_effect=lambda s, p: self._runtime(p)):
                with patch.object(commands, "require_tmux") as req:
                    with contextlib.redirect_stdout(buf):
                        rc = commands.cmd_cockpit(self._args())
        self.assertEqual(0, rc)
        self.assertIn("preview", buf.getvalue())
        self.assertIn("--confirm", buf.getvalue())
        req.assert_not_called()  # read-only preview never gates on a mutable server

    def test_confirm_applies_identity_binds(self) -> None:
        from mozyo_bridge.application import commands

        class _Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        calls = []

        def fake_run(*argv, check=True):
            calls.append(argv)
            return _Ok()

        buf = io.StringIO()
        with patch.object(commands, "_read_cockpit_geometry", return_value=_drift_panes()):
            with patch.object(commands, "_read_cockpit_pane_runtime", side_effect=lambda s, p: self._runtime(p)):
                with patch.object(commands, "require_tmux"):
                    with patch.object(commands, "run_tmux", side_effect=fake_run):
                        with contextlib.redirect_stdout(buf):
                            rc = commands.cmd_cockpit(self._args(confirm=True))
        self.assertEqual(0, rc)
        self.assertIn("applied", buf.getvalue())
        self.assertTrue(
            any(a[0] == "set-option" and a[-2] == "@mozyo_agent_role" and a[-1] == "claude" for a in calls)
        )

    def test_blocked_exits_nonzero(self) -> None:
        from mozyo_bridge.application import commands

        buf = io.StringIO()
        with patch.object(commands, "_read_cockpit_geometry", return_value=_drift_panes()):
            with patch.object(commands, "_read_cockpit_pane_runtime", side_effect=lambda s, p: self._runtime(p)):
                with contextlib.redirect_stdout(buf):
                    rc = commands.cmd_cockpit(self._args(peer_role="codex"))  # already present
        self.assertEqual(1, rc)
        self.assertIn("blocked", buf.getvalue())

    def test_json_emits_decision(self) -> None:
        from mozyo_bridge.application import commands

        buf = io.StringIO()
        with patch.object(commands, "_read_cockpit_geometry", return_value=_drift_panes()):
            with patch.object(commands, "_read_cockpit_pane_runtime", side_effect=lambda s, p: self._runtime(p)):
                with contextlib.redirect_stdout(buf):
                    rc = commands.cmd_cockpit(self._args(json_output=True))
        self.assertEqual(0, rc)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual("%1106", payload["plan"]["pane_id"])
        self.assertFalse(payload["applied"])

    def test_missing_args_dies(self) -> None:
        from mozyo_bridge.application import commands

        with patch.object(commands, "_read_cockpit_geometry", return_value=_drift_panes()):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stdout(io.StringIO()):
                    commands.cmd_cockpit(self._args(peer_pane=None))


if __name__ == "__main__":
    unittest.main()
