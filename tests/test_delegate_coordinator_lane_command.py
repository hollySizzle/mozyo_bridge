"""Command-level tests for `handoff delegate-coordinator-lane` (Redmine #12447).

These exercise :func:`cmd_handoff_delegate_coordinator_lane` without live tmux by
patching the tmux preflight and the candidate discovery. They cover the launch
plan emission and the fail-closed paths (no auto mode, missing canonical root).
The adopt path's actual delivery reuses the proven #12438 ``orchestrate_handoff``
send and is not re-exercised here (covered by the project-router tests + the
separate live verification issue).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import commands  # noqa: E402


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        source="redmine",
        issue="12447",
        journal="63531",
        mode="standard",
        kind=None,
        lane=None,
        projects_config=None,
        target_project="giken-3800-mozyo-bridge",
        target=None,
        adopt_target=None,
        child_issue=None,
        branch=None,
        worktree=None,
        lane_id=None,
        parent_project=None,
        parent_issue="12437",
        parent_callback_target="%8",
        delegation_root=None,
        delegation_parent=None,
        profile_field=None,
        record_format="both",
        record_command=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class DelegateCoordinatorLaneCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        # Patch the tmux preflight and candidate discovery so the command runs
        # without a live tmux server.
        self._orig_require = commands.require_tmux
        self._orig_candidates = commands._agents_target_candidates
        commands.require_tmux = lambda: None
        commands._agents_target_candidates = lambda args: []

    def tearDown(self) -> None:
        commands.require_tmux = self._orig_require
        commands._agents_target_candidates = self._orig_candidates

    def _write_config(self, canonical_repo_root: str) -> str:
        # Redaction-safe gk-style projects.yaml mapping with a canonical root.
        config = (
            "project: gk-3500-it-operations\n"
            "projects:\n"
            "  giken-3800-mozyo-bridge:\n"
            "    classification: external-submodule\n"
            "    canonical:\n"
            f"      repo_root: {canonical_repo_root}\n"
            "      redmine_project: giken-3800-mozyo-bridge\n"
        )
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        tmp.write(config)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_launch_emits_replayable_record(self) -> None:
        with tempfile.TemporaryDirectory() as canonical_root:
            config_path = self._write_config(canonical_root)
            args = _args(
                lane="launch",
                projects_config=config_path,
                child_issue="12448",
                branch="issue_12448_live_verify",
                worktree="mozyo_bridge-12448",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = commands.cmd_handoff_delegate_coordinator_lane(args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("delegated coordinator lane decision", out)
            # The JSON block is replayable and carries the launch identity.
            payload = json.loads(out[out.index("{") : out.rindex("}") + 1])
            self.assertEqual(payload["lane_decision"], "launch")
            self.assertEqual(payload["child_issue"], "12448")
            self.assertEqual(payload["branch"], "issue_12448_live_verify")
            self.assertEqual(payload["worktree"], "mozyo_bridge-12448")
            self.assertEqual(payload["parent_issue"], "12437")
            self.assertEqual(payload["callback_route"], "%8")
            self.assertTrue(payload["no_hidden_subagent"])
            # Launch never sends; it instructs the operator to materialize a
            # visible lane (no hidden subagent).
            self.assertIn("never auto-launches", out)

    def _expect_blocked(self, args) -> str:
        """Run the command expecting a fail-closed exit; return captured stderr.

        The structured outcome (reason on a machine field) prints to stdout while
        the human-readable fail-closed message prints to stderr via ``die``; the
        pure-core tests pin the stable ``.code`` values, so here we assert the
        operator-facing message.
        """
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                commands.cmd_handoff_delegate_coordinator_lane(args)
        self.assertEqual(ctx.exception.code, 2)
        return err.getvalue()

    def test_launch_fails_closed_when_canonical_root_absent(self) -> None:
        # Canonical root points at a path that does not exist locally.
        config_path = self._write_config("/workspace/project-alpha-absent")
        args = _args(
            lane="launch",
            projects_config=config_path,
            child_issue="12448",
            branch="issue_12448_live_verify",
        )
        self.assertIn("not present locally", self._expect_blocked(args))

    def test_omitted_decision_fails_closed(self) -> None:
        # Defense in depth: even bypassing the argparse `required=True`, the core
        # refuses to auto-reuse an existing lane.
        with tempfile.TemporaryDirectory() as canonical_root:
            config_path = self._write_config(canonical_root)
            args = _args(lane=None, projects_config=config_path)
            self.assertIn(
                "explicit lane decision is required", self._expect_blocked(args)
            )

    def test_launch_incomplete_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as canonical_root:
            config_path = self._write_config(canonical_root)
            args = _args(
                lane="launch",
                projects_config=config_path,
                child_issue="12448",
                # neither branch nor worktree
            )
            self.assertIn(
                "replayable lane identity", self._expect_blocked(args)
            )


if __name__ == "__main__":
    unittest.main()
