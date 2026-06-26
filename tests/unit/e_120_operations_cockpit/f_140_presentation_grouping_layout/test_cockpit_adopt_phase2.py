"""Cockpit adopt Phase 2 — confirm-gated explicit pane move (#11898).

Redmine #11816 j#57823 split cockpit adopt into a non-destructive Phase 1
(detect + advisory, #11897) and an explicit, confirm-gated Phase 2 (this US).
Phase 2 moves a co-existing normal `mozyo` session's live codex/claude panes
into the cockpit as a column via `join-pane` (which preserves the pane id and
the running agent), atomically with best-effort rollback. These tests pin the
pure adopt-plan builder, the role->pane pairing, the atomic executor's rollback,
and the `mozyo cockpit adopt --confirm` command (execution + every fail-closed
gate), all hermetic (no live tmux). Synthetic, neutral identifiers only.
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

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    ROLE_CLAUDE,
    ROLE_CODEX,
    CockpitWorkspace,
    NormalSessionObservation,
    adopt_pane_pair,
    build_cockpit_adopt_plan,
    detect_adopt_candidates,
)


def _ws(**over):
    base = dict(
        workspace_id="wsX", label="mozyo-ws", repo_root="/workspace/project-alpha",
        lane_id="default", lane_label=None,
    )
    base.update(over)
    return CockpitWorkspace(**base)


def _candidate(observations, *, workspace_id="wsX", lane_id="default"):
    advisory = detect_adopt_candidates(
        workspace_id=workspace_id, lane_id=lane_id, observations=observations
    )
    return advisory.candidates[0]


def _obs(session, role, *, workspace_id="wsX", lane_id="default", pane_id="%1"):
    return NormalSessionObservation(
        session=session, workspace_id=workspace_id, lane_id=lane_id,
        role=role, pane_id=pane_id,
    )


class AdoptPanePairTest(unittest.TestCase):
    def test_pairs_one_codex_one_claude(self) -> None:
        cand = _candidate(
            [_obs("mozyo-ws", "codex", pane_id="%2"),
             _obs("mozyo-ws", "claude", pane_id="%3")]
        )
        self.assertEqual(("%2", "%3"), adopt_pane_pair(cand))

    def test_none_when_role_has_two_panes(self) -> None:
        # Two codex panes in one normal session -> the role->pane pairing is
        # ambiguous, so adopt must fail closed (#11898 "role unknown").
        cand = _candidate(
            [_obs("mozyo-ws", "codex", pane_id="%2"),
             _obs("mozyo-ws", "codex", pane_id="%4"),
             _obs("mozyo-ws", "claude", pane_id="%3")]
        )
        self.assertIsNone(adopt_pane_pair(cand))

    def test_none_when_role_missing(self) -> None:
        cand = _candidate([_obs("mozyo-ws", "codex", pane_id="%2")])
        self.assertIsNone(adopt_pane_pair(cand))


class BuildAdoptPlanTest(unittest.TestCase):
    def _plan(self, **over):
        kw = dict(
            source_session="mozyo-ws", source_codex_pane="%2",
            source_claude_pane="%3", anchor_pane="%9", column_index=1,
            codex_ratio=70, session="mozyo-cockpit",
        )
        kw.update(over)
        return build_cockpit_adopt_plan(_ws(), **kw)

    def test_join_commands_move_live_panes_full_height(self) -> None:
        plan = self._plan()
        self.assertEqual(2, len(plan.join_commands))
        # Codex becomes a full-height column anchored on the rightmost cockpit
        # codex pane, sized to its fair 1/N share (even_column_share(2) = 50%).
        self.assertEqual(
            ("join-pane", "-h", "-f", "-l", "50%", "-s", "%2", "-t", "%9"),
            plan.join_commands[0].argv,
        )
        # Claude is split in below the now-joined codex pane (referenced by its
        # preserved id %2) at the claude ratio.
        self.assertEqual(
            ("join-pane", "-v", "-l", "30%", "-s", "%3", "-t", "%2"),
            plan.join_commands[1].argv,
        )

    def test_restamps_identity_on_moved_panes(self) -> None:
        plan = self._plan()
        stamp_argvs = [c.argv for c in plan.stamp_commands]
        # Both moved panes (referenced by their preserved ids) get workspace /
        # role / lane re-stamped after the join (#11898 acceptance).
        self.assertIn(
            ("set-option", "-p", "-t", "%2", "@mozyo_workspace_id", "wsX"),
            stamp_argvs,
        )
        self.assertIn(
            ("set-option", "-p", "-t", "%2", "@mozyo_agent_role", "codex"),
            stamp_argvs,
        )
        self.assertIn(
            ("set-option", "-p", "-t", "%3", "@mozyo_agent_role", "claude"),
            stamp_argvs,
        )
        self.assertIn(
            ("set-option", "-p", "-t", "%2", "@mozyo_lane_id", "default"),
            stamp_argvs,
        )

    def test_lane_label_stamped_when_present(self) -> None:
        plan = build_cockpit_adopt_plan(
            _ws(lane_id="lane-abc123", lane_label="feature/x"),
            source_session="mozyo-ws", source_codex_pane="%2",
            source_claude_pane="%3", anchor_pane="%9", column_index=1,
        )
        stamp_argvs = [c.argv for c in plan.stamp_commands]
        self.assertIn(
            ("set-option", "-p", "-t", "%2", "@mozyo_lane_id", "lane-abc123"),
            stamp_argvs,
        )
        self.assertIn(
            ("set-option", "-p", "-t", "%2", "@mozyo_lane_label", "feature/x"),
            stamp_argvs,
        )

    def test_as_dict_is_json_safe(self) -> None:
        payload = self._plan().as_dict()
        json.dumps(payload)  # must not raise
        self.assertEqual("mozyo-ws", payload["source_session"])
        self.assertEqual("%2", payload["source_codex_pane"])
        self.assertEqual(2, len(payload["join_commands"]))

    def test_rejects_same_source_panes(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(source_claude_pane="%2")

    def test_rejects_missing_anchor(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(anchor_pane="")


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRun:
    """A run_tmux-style callable recording argv, optionally failing on a predicate."""

    def __init__(self, fail_when=None):
        self.calls = []
        self.fail_when = fail_when or (lambda argv: False)

    def __call__(self, *argv, check=False):
        self.calls.append(tuple(argv))
        rc = 1 if self.fail_when(argv) else 0
        return FakeResult(returncode=rc, stderr="boom" if rc else "")


class ExecuteAdoptPlanTest(unittest.TestCase):
    def _plan(self):
        return build_cockpit_adopt_plan(
            _ws(), source_session="mozyo-ws", source_codex_pane="%2",
            source_claude_pane="%3", anchor_pane="%9", column_index=1,
        )

    def _execute(self, run):
        from mozyo_bridge.application.commands import execute_cockpit_adopt_plan

        return execute_cockpit_adopt_plan(self._plan(), run)

    _ROLLBACK = ("join-pane", "-h", "-s", "%2", "-t", "%3")

    def test_success_runs_joins_and_stamps_no_rollback(self) -> None:
        run = RecordingRun()
        result = self._execute(run)
        self.assertEqual([], result["stamp_warnings"])
        # Both joins issued, in order.
        joins = [c for c in run.calls if c and c[0] == "join-pane"]
        self.assertEqual("-s", joins[0][joins[0].index("-s")])
        self.assertIn("%2", joins[0])  # codex join
        self.assertIn("%3", joins[1])  # claude join
        # Identity re-stamped.
        self.assertTrue(any(c[0] == "set-option" for c in run.calls))
        # No rollback on the happy path.
        self.assertNotIn(self._ROLLBACK, run.calls)

    def test_rollback_moves_codex_back_when_claude_join_fails(self) -> None:
        # The 2nd join (claude, `-v`) fails after codex joined -> best-effort
        # rollback moves codex back beside the still-present source claude pane,
        # and the command fails closed (#11898 atomic pair).
        run = RecordingRun(fail_when=lambda argv: "join-pane" in argv and "-v" in argv)
        with self.assertRaises(SystemExit):
            self._execute(run)
        self.assertIn(self._ROLLBACK, run.calls)

    def test_no_rollback_when_first_join_fails(self) -> None:
        # Codex join itself fails -> nothing was moved, so there is nothing to
        # roll back (both panes still in the source session).
        run = RecordingRun(fail_when=lambda argv: "join-pane" in argv and "-f" in argv)
        with self.assertRaises(SystemExit):
            self._execute(run)
        self.assertNotIn(self._ROLLBACK, run.calls)
        # Only the failed codex join ran; the claude join never started.
        joins = [c for c in run.calls if c and c[0] == "join-pane"]
        self.assertEqual(1, len(joins))

    def test_stamp_failure_is_best_effort_not_rollback(self) -> None:
        # Both joins land (pair adopted); a later identity stamp failing is
        # reported as a warning, never rolled back, never fatal.
        run = RecordingRun(fail_when=lambda argv: argv and argv[0] == "set-option")
        result = self._execute(run)
        self.assertTrue(result["stamp_warnings"])
        self.assertNotIn(self._ROLLBACK, run.calls)


class CockpitAdoptConfirmFlowTest(unittest.TestCase):
    """`mozyo cockpit adopt --confirm` — the only path that moves panes (#11898)."""

    def _args(self, **over):
        base = dict(
            action="adopt", repo="/workspace/project-alpha", codex_ratio=70,
            cockpit_session=None, dry_run=False, json_output=False, no_attach=False,
            confirm=True,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def _candidate_advisory(self):
        return detect_adopt_candidates(
            workspace_id="wsX", lane_id="default",
            observations=[
                _obs("mozyo-ws", "codex", pane_id="%2"),
                _obs("mozyo-ws", "claude", pane_id="%3"),
            ],
        )

    def _cockpit_columns(self):
        return [{"pane_id": "%9", "workspace_id": "wsOTHER", "role": "codex",
                 "lane_id": "default", "pane_left": 0, "pane_width": 80}]

    @contextlib.contextmanager
    def _patched(self, *, columns, advisory, attached=(), run=None,
                 session_present=True):
        from mozyo_bridge.application import commands
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import DEFAULT_LANE, LaneIdentity

        canon = argparse.Namespace(name="mozyo-ws", workspace_id="wsX")
        run = run or RecordingRun()
        with patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_resolve_workspace_lane",
                         return_value=LaneIdentity(DEFAULT_LANE, None)), \
            patch.object(commands, "require_tmux") as require_tmux, \
            patch.object(commands, "_read_cockpit_columns", return_value=columns), \
            patch.object(commands, "_cockpit_adopt_advisory", return_value=advisory), \
            patch.object(commands, "_session_attached_clients", return_value=attached), \
            patch.object(commands, "session_exists", return_value=session_present), \
            patch.object(commands, "run_tmux", side_effect=run), \
            patch.object(commands.os, "execvp", side_effect=RuntimeError("attach")) as execvp:
            yield require_tmux, run, execvp

    def _run(self, args, **patched):
        from mozyo_bridge.application.commands import cmd_cockpit

        with self._patched(**patched) as (require_tmux, run, execvp):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    rc = cmd_cockpit(args)
                except SystemExit as exc:
                    rc = exc.code
        return rc, out.getvalue(), require_tmux, run, execvp

    def test_confirm_executes_the_join_move(self) -> None:
        run = RecordingRun()
        rc, out, require_tmux, _run, execvp = self._run(
            self._args(), columns=self._cockpit_columns(),
            advisory=self._candidate_advisory(), run=run, session_present=True,
        )
        self.assertEqual(0, rc)
        joins = [c for c in run.calls if c and c[0] == "join-pane"]
        self.assertTrue(any("%2" in c for c in joins))  # codex moved
        self.assertTrue(any("%3" in c for c in joins))  # claude moved
        self.assertTrue(any(c[0] == "set-option" for c in run.calls))  # re-stamped
        self.assertIn("adopted", out)
        # Source-session cleanup is explicit + logged, never an implicit kill.
        self.assertIn("source session", out)
        self.assertNotIn(("kill-session",), [c[:1] for c in run.calls])
        require_tmux.assert_called_once()
        execvp.assert_not_called()

    def test_confirm_fails_closed_on_attached_client(self) -> None:
        rc, out, _rt, run, _ev = self._run(
            self._args(), columns=self._cockpit_columns(),
            advisory=self._candidate_advisory(), attached=("/dev/ttys003",),
        )
        self.assertEqual(2, rc)  # die()
        self.assertNotIn("join-pane", [c[0] for c in run.calls if c])

    def test_confirm_fails_closed_when_no_cockpit(self) -> None:
        rc, out, _rt, run, _ev = self._run(
            self._args(), columns=None,
            advisory=self._candidate_advisory(), session_present=False,
        )
        self.assertEqual(2, rc)
        self.assertNotIn("join-pane", [c[0] for c in run.calls if c])

    def test_confirm_fails_closed_on_partial_pair(self) -> None:
        partial = detect_adopt_candidates(
            workspace_id="wsX", lane_id="default",
            observations=[_obs("mozyo-ws", "codex", pane_id="%2")],
        )
        rc, out, _rt, run, _ev = self._run(
            self._args(), columns=self._cockpit_columns(), advisory=partial,
        )
        self.assertEqual(2, rc)
        self.assertNotIn("join-pane", [c[0] for c in run.calls if c])

    def test_dry_run_outranks_confirm_and_previews_only(self) -> None:
        run = RecordingRun()
        rc, out, _rt, _run, _ev = self._run(
            self._args(dry_run=True), columns=self._cockpit_columns(),
            advisory=self._candidate_advisory(), run=run,
        )
        self.assertEqual(0, rc)
        self.assertIn("adopt plan", out)
        # --dry-run is a safe preview even with --confirm: no tmux mutation.
        self.assertEqual([], run.calls)


if __name__ == "__main__":
    unittest.main()
