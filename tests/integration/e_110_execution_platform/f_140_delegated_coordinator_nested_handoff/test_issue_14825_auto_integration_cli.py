"""``workflow auto-integration`` reachability (Redmine #14825 review j#96611 finding 1).

The finding was that the composition root and the asynchronous continuation had **zero runtime
references** — they existed, and nothing could invoke them. So the first thing this file asserts
is the thing ``grep`` was asked: the parser is registered, and each subcommand reaches the live
composition root for real.

The refusal path is the one that can be exercised without a live Redmine / remote, and it is not
a lesser test: it drives argument parsing, identity resolution, the repo-local config read and
the composition root's item-6 gate through the actual CLI, and asserts the process exits on the
refusal rather than on an exception. A command that raises instead of refusing is a command an
operator cannot script against.
"""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_auto_integration import (  # noqa: E501
    EXIT_REFUSED,
)

SOURCE = "a" * 40
TARGET = "b" * 40


def _argv(command: str, repo_root: Path) -> list:
    return [
        "workflow",
        "auto-integration",
        command,
        "--issue",
        "14825",
        "--workspace",
        "ws-1",
        "--lane",
        "lane-14825",
        "--lane-generation",
        "1",
        "--branch",
        "issue_14825",
        "--worktree",
        str(repo_root),
        "--source-head",
        SOURCE,
        "--expected-target-head",
        TARGET,
        "--review-generation",
        "r1",
        "--repo-root",
        str(repo_root),
    ]


class AutoIntegrationCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._git("init", "-q")
        self._git("config", "user.name", "mozyo-bridge test")
        self._git("config", "user.email", "test@example.invalid")
        self.parser = build_parser()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _config(self, body: str) -> None:
        config_dir = self.repo_root / ".mozyo-bridge"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
        self._git("add", ".mozyo-bridge/config.yaml")
        self._git("commit", "-qm", "test config")

    def _run(self, command: str) -> tuple:
        args = self.parser.parse_args(_argv(command, self.repo_root))
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = args.func(args)
        return code, json.loads(stream.getvalue())

    def test_every_subcommand_is_registered_and_reaches_the_composition_root(self) -> None:
        # The finding, inverted: every operation is invocable and each one lands in the real
        # composition root (proved by the refusal it produces there, which no other layer emits).
        self._config("auto_integration:\n  mode: auto\n")
        for command in (
            "run",
            "continue",
            "settle",
            "reconcile",
            "cleanup",
            "reconcile-cleanup",
        ):
            with self.subTest(command=command):
                code, payload = self._run(command)
                self.assertEqual(code, EXIT_REFUSED)
                self.assertEqual(payload["reason"], "composition_refused")
                self.assertIn("unconfigured target", payload["detail"])

    def test_an_unconfigured_target_refuses_rather_than_raising(self) -> None:
        self._config("auto_integration:\n  mode: auto\n")
        code, payload = self._run("run")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(payload["status"], "refused")

    def test_a_malformed_action_identity_is_refused_before_anything_runs(self) -> None:
        self._config("auto_integration:\n  mode: auto\n  integration_branch: main\n")
        args = self.parser.parse_args(
            [
                *_argv("run", self.repo_root)[:-2],
                "--repo-root",
                str(self.repo_root),
            ]
        )
        args.source_head = "not-a-sha"
        stream = io.StringIO()
        with mock.patch(
            "mozyo_bridge.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.application."
            "cli_workflow_auto_integration.load_committed_repo_local_config",
            side_effect=AssertionError("composition must not run"),
        ), redirect_stdout(stream):
            code = args.func(args)
        payload = json.loads(stream.getvalue())
        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(payload["reason"], "invalid_action_record")
        self.assertIn("source_head", payload["detail"])

    def test_the_target_ref_cannot_be_named_by_the_caller(self) -> None:
        # There is deliberately no `--target-ref`. Accepting one would BE the runtime resolution
        # item 6 withdrew: the target of a push, named by the caller. Asserted on the parser
        # rather than by running an action, so this stays hermetic — no live tracker, no remote.
        for command in (
            "run",
            "continue",
            "settle",
            "reconcile",
            "cleanup",
            "reconcile-cleanup",
        ):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit):
                    self.parser.parse_args(
                        _argv(command, self.repo_root) + ["--target-ref", "main"]
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
