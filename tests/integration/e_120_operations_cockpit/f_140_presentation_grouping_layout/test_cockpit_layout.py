"""Cockpit layout preset (Redmine #11788).

The layout's tmux command generation is the unit under test: it is a pure
planner, so these tests assert the column / vertical-split structure, the
Codex/Claude ratio, pane titles carrying workspace id + role, and the executor
token resolution — all without a live tmux server. The CLI tests cover the
inspectable `--dry-run` / `--json` paths and the reuse-over-rebuild policy with
tmux fully mocked.
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

from mozyo_bridge.domain.cockpit_layout import (
    COCKPIT_SESSION_DEFAULT,
    CockpitWorkspace,
    build_cockpit_plan,
    normalize_ratio,
)


def _ws(n):
    return CockpitWorkspace(
        workspace_id=f"id{n}", label=f"sess{n}", repo_root=f"/repo{n}"
    )


class PlannerStructureTest(unittest.TestCase):
    def test_single_workspace_has_one_column_split_vertically(self) -> None:
        plan = build_cockpit_plan([_ws(0)])
        self.assertEqual(1, plan.columns)
        # codex + claude pane only.
        self.assertEqual(
            ["codex", "claude"], [p.role for p in plan.panes]
        )
        ops = [c.argv[0] for c in plan.commands]
        self.assertEqual("new-session", ops[0])
        # one vertical split for claude, no horizontal split, no relayout.
        self.assertIn("split-window", ops)
        self.assertNotIn("select-layout", ops)
        self.assertEqual(1, sum(1 for c in plan.commands if c.argv[0] == "split-window"))

    def test_two_workspaces_make_two_columns_each_split(self) -> None:
        plan = build_cockpit_plan([_ws(0), _ws(1)])
        self.assertEqual(2, plan.columns)
        # column 0 codex (new-session), column 1 codex (split -h), then a
        # vertical split per column for claude.
        h_splits = [c for c in plan.commands if c.argv[:2] == ("split-window", "-h")]
        v_splits = [c for c in plan.commands if c.argv[:2] == ("split-window", "-v")]
        self.assertEqual(1, len(h_splits))  # second column
        self.assertEqual(2, len(v_splits))  # one claude per column
        # widths equalized.
        self.assertTrue(any(c.argv[0] == "select-layout" for c in plan.commands))
        # left-to-right: column 1 splits from column 0's codex pane.
        self.assertIn("@col0_codex", h_splits[0].argv)

    def test_columns_capture_logical_pane_tokens(self) -> None:
        plan = build_cockpit_plan([_ws(0), _ws(1)])
        captured = [c.captures for c in plan.commands if c.captures]
        self.assertEqual(
            ["@col0_codex", "@col1_codex", "@col0_claude", "@col1_claude"],
            captured,
        )


class PlannerRatioTest(unittest.TestCase):
    def test_default_ratio_is_70_30(self) -> None:
        plan = build_cockpit_plan([_ws(0)])
        self.assertEqual(70, plan.codex_ratio)
        self.assertEqual(30, plan.claude_ratio)
        codex = next(p for p in plan.panes if p.role == "codex")
        claude = next(p for p in plan.panes if p.role == "claude")
        self.assertEqual(70, codex.height_pct)
        self.assertEqual(30, claude.height_pct)

    def test_custom_ratio_drives_claude_split_size(self) -> None:
        plan = build_cockpit_plan([_ws(0)], codex_ratio=60)
        self.assertEqual(40, plan.claude_ratio)
        v_split = next(c for c in plan.commands if c.argv[:2] == ("split-window", "-v"))
        self.assertIn("40%", v_split.argv)

    def test_ratio_is_clamped(self) -> None:
        self.assertEqual(10, normalize_ratio(2))
        self.assertEqual(90, normalize_ratio(99))
        self.assertEqual(55, normalize_ratio(55))


class PlannerTitleAndLaunchTest(unittest.TestCase):
    def test_pane_titles_carry_workspace_and_role(self) -> None:
        ws = CockpitWorkspace(
            workspace_id="wsX",
            label="alpha",
            repo_root="/a",
            codex_anchor="#100 j200",
        )
        plan = build_cockpit_plan([ws])
        titles = {c.argv[-1] for c in plan.commands if c.argv[0] == "select-pane"}
        self.assertIn("alpha · codex · #100 j200", titles)
        self.assertIn("alpha · claude", titles)

    def test_launch_command_is_appended_per_role(self) -> None:
        plan = build_cockpit_plan(
            [_ws(0)], launch=lambda role, ws: f"run-{role}-{ws.workspace_id}"
        )
        new_session = plan.commands[0]
        self.assertEqual("run-codex-id0", new_session.argv[-1])
        claude = next(c for c in plan.commands if c.argv[:2] == ("split-window", "-v"))
        self.assertEqual("run-claude-id0", claude.argv[-1])

    def test_empty_workspaces_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_cockpit_plan([])


class ExecutorTokenResolutionTest(unittest.TestCase):
    def test_logical_tokens_resolve_to_captured_pane_ids(self) -> None:
        from mozyo_bridge.application.commands import execute_cockpit_plan

        plan = build_cockpit_plan([_ws(0), _ws(1)])
        seq = iter(["%10", "%11", "%12", "%13"])
        seen: list[tuple] = []

        def fake_run(*argv, check=True):
            seen.append(argv)
            out = next(seq) if "-P" in argv else ""
            return argparse.Namespace(returncode=0, stdout=out, stderr="")

        ids = execute_cockpit_plan(plan, fake_run)
        self.assertEqual(
            {"@col0_codex": "%10", "@col1_codex": "%11",
             "@col0_claude": "%12", "@col1_claude": "%13"},
            ids,
        )
        # The horizontal split targeted the *resolved* col0 pane id, not the
        # logical token.
        h_split = next(a for a in seen if a[:2] == ("split-window", "-h"))
        self.assertIn("%10", h_split)
        self.assertNotIn("@col0_codex", h_split)


class LayoutCliTest(unittest.TestCase):
    def _args(self, **over):
        base = dict(
            preset="cockpit",
            codex_ratio=70,
            cockpit_session=None,
            layout_repos=["/repoA", "/repoB"],
            dry_run=False,
            json_output=False,
            cc=False,
            no_attach=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    @contextlib.contextmanager
    def _patched(self, *, session_exists=False):
        from mozyo_bridge.application import commands

        def fake_resolve(repo, **_k):
            name = "sess-" + Path(repo).name
            return argparse.Namespace(name=name, workspace_id="id-" + name)

        with patch.object(commands, "resolve_canonical_session", side_effect=fake_resolve), \
            patch.object(commands, "_agent_launch_command", side_effect=lambda role, s, cwd, **_: f"{role}-cmd"), \
            patch.object(commands, "require_tmux"), \
            patch.object(commands, "session_exists", return_value=session_exists), \
            patch.object(commands, "run_tmux") as run_tmux, \
            patch.object(commands.os, "execvp", side_effect=RuntimeError("attach")) as execvp:
            yield run_tmux, execvp

    def test_dry_run_prints_commands_without_touching_tmux(self) -> None:
        from mozyo_bridge.application.commands import cmd_layout_apply

        with self._patched() as (run_tmux, execvp):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = cmd_layout_apply(self._args(dry_run=True))
        self.assertEqual(0, rc)
        run_tmux.assert_not_called()
        execvp.assert_not_called()
        text = out.getvalue()
        self.assertIn("cockpit plan:", text)
        self.assertIn("tmux new-session", text)
        self.assertIn("attach: tmux attach -t mozyo-cockpit", text)

    def test_json_emits_plan_and_does_not_attach(self) -> None:
        from mozyo_bridge.application.commands import cmd_layout_apply

        with self._patched() as (run_tmux, execvp):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                cmd_layout_apply(self._args(json_output=True, cc=True))
        run_tmux.assert_not_called()
        execvp.assert_not_called()
        payload = json.loads(out.getvalue())
        self.assertEqual(2, payload["columns"])
        self.assertEqual(70, payload["codex_ratio"])
        self.assertTrue(payload["control_mode"])
        self.assertEqual("tmux -CC attach -t mozyo-cockpit", payload["attach"])
        roles = [p["role"] for p in payload["panes"]]
        self.assertEqual(roles.count("codex"), 2)
        self.assertEqual(roles.count("claude"), 2)

    def test_build_then_attach_runs_tmux_and_execs(self) -> None:
        from mozyo_bridge.application.commands import cmd_layout_apply

        with self._patched(session_exists=False) as (run_tmux, execvp):
            run_tmux.return_value = argparse.Namespace(returncode=0, stdout="%1", stderr="")
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "attach"):
                    cmd_layout_apply(self._args())
        self.assertTrue(run_tmux.called)  # the plan executed
        execvp.assert_called_once()
        self.assertEqual(
            ["tmux", "attach", "-t", "mozyo-cockpit"],
            list(execvp.call_args.args[1]),
        )

    def test_existing_cockpit_session_is_reused_not_rebuilt(self) -> None:
        from mozyo_bridge.application.commands import cmd_layout_apply

        with self._patched(session_exists=True) as (run_tmux, execvp):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaisesRegex(RuntimeError, "attach"):
                    cmd_layout_apply(self._args())
        # No layout commands were run — reuse over duplicate panes.
        run_tmux.assert_not_called()
        self.assertIn("already exists", out.getvalue())
        execvp.assert_called_once()

    def test_cc_attaches_via_control_mode(self) -> None:
        from mozyo_bridge.application.commands import cmd_layout_apply

        with self._patched(session_exists=True) as (run_tmux, execvp):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "attach"):
                    cmd_layout_apply(self._args(cc=True))
        self.assertEqual(
            ["tmux", "-CC", "attach", "-t", "mozyo-cockpit"],
            list(execvp.call_args.args[1]),
        )

    def test_tmux_step_failure_aborts_without_attach(self) -> None:
        # Redmine #11788 review (Major): a failed tmux step must not be
        # swallowed into a "built" + attach of a broken layout.
        from mozyo_bridge.application.commands import cmd_layout_apply

        with self._patched(session_exists=False) as (run_tmux, execvp):
            run_tmux.return_value = argparse.Namespace(
                returncode=1, stdout="", stderr="tmux: boom"
            )
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cmd_layout_apply(self._args())
        self.assertNotIn("cockpit built", out.getvalue())
        execvp.assert_not_called()
        # The partial session is torn down so a retry rebuilds cleanly.
        kill_calls = [
            c for c in run_tmux.call_args_list if c.args[:1] == ("kill-session",)
        ]
        self.assertTrue(kill_calls, "expected a kill-session cleanup")

    def test_empty_pane_capture_aborts_without_attach(self) -> None:
        from mozyo_bridge.application.commands import cmd_layout_apply

        with self._patched(session_exists=False) as (run_tmux, execvp):
            # Exit 0 but no pane id captured -> still fatal (broken target).
            run_tmux.return_value = argparse.Namespace(
                returncode=0, stdout="", stderr=""
            )
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cmd_layout_apply(self._args())
        self.assertNotIn("cockpit built", out.getvalue())
        execvp.assert_not_called()

    def test_no_active_workspace_fails_closed(self) -> None:
        from mozyo_bridge.application.commands import cmd_layout_apply
        from mozyo_bridge.application import commands

        with patch.object(commands, "_resolve_cockpit_workspaces", return_value=[]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cmd_layout_apply(self._args(layout_repos=None))


class ResolveWorkspacesLaneTest(unittest.TestCase):
    """`_resolve_cockpit_workspaces` live-inventory path keys on workspace+lane.

    Regression for the #11820 US-audit Major finding (journal #57044): keying
    the inventory dedupe by `workspace_id` alone collapsed a same-workspace /
    different-lane pair (e.g. main worktree + linked worktree) into one column,
    contradicting the append-as-separate-column contract.
    """

    def _rec(self, *, agent_kind, workspace_id, repo_root, session):
        from mozyo_bridge.session_inventory import InventoryRecord, WorkspaceIdentity

        return InventoryRecord(
            pane_id=f"%{abs(hash((repo_root, agent_kind))) % 1000}",
            session=session,
            window_index="0",
            window_name=agent_kind,
            pane_index="0",
            pane_active=True,
            process=agent_kind,
            cwd=repo_root,
            repo_root=repo_root,
            agent_kind=agent_kind,
            workspace=WorkspaceIdentity(
                workspace_id=workspace_id,
                canonical_session=session,
                project_name=None,
                source="test",
            ),
            views=(),
        )

    def test_same_workspace_different_lane_makes_two_columns(self) -> None:
        from mozyo_bridge.application import commands
        from mozyo_bridge.domain.agent_discovery import (
            AGENT_KIND_CLAUDE,
            AGENT_KIND_CODEX,
        )
        from mozyo_bridge.domain.cockpit_layout import LaneIdentity

        # Two checkouts of the SAME workspace_id at different paths/lanes; each
        # carries a codex + claude pane (4 records total).
        records = [
            self._rec(agent_kind=AGENT_KIND_CODEX, workspace_id="ws-same",
                      repo_root="/repo/main", session="main-session"),
            self._rec(agent_kind=AGENT_KIND_CLAUDE, workspace_id="ws-same",
                      repo_root="/repo/main", session="main-session"),
            self._rec(agent_kind=AGENT_KIND_CODEX, workspace_id="ws-same",
                      repo_root="/repo/wt", session="wt-session"),
            self._rec(agent_kind=AGENT_KIND_CLAUDE, workspace_id="ws-same",
                      repo_root="/repo/wt", session="wt-session"),
        ]
        snapshot = argparse.Namespace(records=tuple(records))

        lane_by_repo = {
            "/repo/main": LaneIdentity("default", None),
            "/repo/wt": LaneIdentity("lane-worktree", "feature"),
        }

        with patch("mozyo_bridge.session_inventory.take_inventory", return_value=snapshot), \
            patch.object(
                commands, "_resolve_workspace_lane",
                side_effect=lambda repo_root, ws: lane_by_repo[repo_root],
            ):
            workspaces = commands._resolve_cockpit_workspaces(
                argparse.Namespace(layout_repos=None)
            )

        self.assertEqual(2, len(workspaces))
        lanes = sorted(w.lane_id for w in workspaces)
        self.assertEqual(["default", "lane-worktree"], lanes)
        # both columns are the same workspace, distinguished only by lane.
        self.assertEqual({"ws-same"}, {w.workspace_id for w in workspaces})

    def test_same_workspace_same_lane_dedupes_to_one_column(self) -> None:
        # codex + claude panes of one checkout collapse to a single column.
        from mozyo_bridge.application import commands
        from mozyo_bridge.domain.agent_discovery import (
            AGENT_KIND_CLAUDE,
            AGENT_KIND_CODEX,
        )
        from mozyo_bridge.domain.cockpit_layout import LaneIdentity

        records = [
            self._rec(agent_kind=AGENT_KIND_CODEX, workspace_id="ws-1",
                      repo_root="/repo/main", session="s"),
            self._rec(agent_kind=AGENT_KIND_CLAUDE, workspace_id="ws-1",
                      repo_root="/repo/main", session="s"),
        ]
        snapshot = argparse.Namespace(records=tuple(records))

        with patch("mozyo_bridge.session_inventory.take_inventory", return_value=snapshot), \
            patch.object(
                commands, "_resolve_workspace_lane",
                return_value=LaneIdentity("default", None),
            ):
            workspaces = commands._resolve_cockpit_workspaces(
                argparse.Namespace(layout_repos=None)
            )

        self.assertEqual(1, len(workspaces))


if __name__ == "__main__":
    unittest.main()
