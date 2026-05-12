from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge import __version__
from mozyo_bridge.application.commands import (
    cmd_doctor,
    cmd_ensure_pair,
    cmd_init,
    cmd_mozyo,
    cmd_open,
    cmd_open_here,
    ensure_repo_session_windows,
    load_tmux_conf_for,
    notify_agent,
    session_cwd_mismatch,
)
from mozyo_bridge.infrastructure import tmux_client
from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.domain.notification import build_prompt, landing_marker, validate_notify_gate
from mozyo_bridge.domain.pane_resolver import (
    clear_read,
    ensure_agent_target,
    find_labeled_pane,
    is_agent_process,
    is_tmux_target,
    mark_read,
    require_read,
)
import mozyo_bridge.domain.pane_resolver as pane_resolver
from mozyo_bridge.infrastructure.queue_reader import find_handoff_task, load_queue
from mozyo_bridge.scaffold.rules import package_version, rules_status, scaffold_state
from mozyo_bridge.shared.paths import default_queue_path, default_tmux_conf, find_repo_root, resolve_repo_root


class NotificationTest(unittest.TestCase):
    def assert_exits_cleanly(self, callback) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                callback()

    def test_validate_notify_gate_requires_issue(self) -> None:
        args = argparse.Namespace(issue=None, journal="1", task_id=None)

        self.assert_exits_cleanly(lambda: validate_notify_gate(args))

    def test_validate_notify_gate_requires_journal_or_task(self) -> None:
        args = argparse.Namespace(issue="9020", journal=None, task_id=None)

        self.assert_exits_cleanly(lambda: validate_notify_gate(args))

    def test_build_prompt_uses_redmine_gate(self) -> None:
        args = argparse.Namespace(issue="9020", journal="46005", type="review_request", commit="abc123", prompt=None)

        prompt = build_prompt(args, "codex", None)

        self.assertIn("[mozyo:notify:issue=9020:journal=46005:type=review_request]", prompt)
        self.assertIn("Redmine #9020 journal #46005", prompt)
        self.assertIn("Stop-hook handoff waiting is disabled", prompt)
        self.assertEqual("[mozyo:notify:issue=9020:journal=46005:type=review_request]", landing_marker(args, None))

    def test_prompt_override_keeps_machine_landing_marker(self) -> None:
        args = argparse.Namespace(prompt="custom operator prompt", issue="9020", journal="1", type="review_request")

        self.assertEqual(
            "[mozyo:notify:issue=9020:journal=1:type=review_request] custom operator prompt",
            build_prompt(args, "codex", None),
        )
        self.assertEqual("[mozyo:notify:issue=9020:journal=1:type=review_request]", landing_marker(args, None))

    def test_build_prompt_uses_handoff_task(self) -> None:
        args = argparse.Namespace(prompt=None)
        task = {"id": "task-1", "issue_id": 9020, "commit": "abc123", "type": "review_request"}

        prompt = build_prompt(args, "codex", task)

        self.assertIn("[mozyo:notify:task=task-1:issue=9020]", prompt)
        self.assertIn("handoff task task-1 is ready for codex", prompt)
        self.assertIn("issue=#9020", prompt)
        self.assertEqual("[mozyo:notify:task=task-1:issue=9020]", landing_marker(args, task))

    def test_journal_takes_precedence_over_legacy_task(self) -> None:
        args = argparse.Namespace(
            prompt=None,
            issue="9020",
            journal="46005",
            type="review_request",
            commit="abc123",
        )
        task = {"id": "task-1", "issue_id": 9020, "commit": "abc123", "type": "review_request"}

        prompt = build_prompt(args, "codex", task)

        self.assertIn("Redmine #9020 journal #46005", prompt)
        self.assertNotIn("handoff task", prompt)
        self.assertEqual("[mozyo:notify:issue=9020:journal=46005:type=review_request]", landing_marker(args, task))


class QueueReaderTest(unittest.TestCase):
    def test_load_queue_returns_empty_tasks_when_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "missing" / "tasks.json"

            self.assertEqual({"tasks": []}, load_queue(queue))

    def test_load_queue_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "tasks.json"
            queue.write_text("tasks:\n  - id: yaml-is-not-json\n", encoding="utf-8")

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    load_queue(queue)

        self.assertIn("queue must be JSON", stderr.getvalue())

    def test_load_queue_rejects_non_mapping_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "tasks.json"
            queue.write_text(json.dumps([{"id": "not-a-root-object"}]), encoding="utf-8")

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    load_queue(queue)

        self.assertIn("queue root must be a mapping", stderr.getvalue())

    def test_load_queue_rejects_non_list_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "tasks.json"
            queue.write_text(json.dumps({"tasks": {"id": "not-a-list"}}), encoding="utf-8")

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    load_queue(queue)

        self.assertIn("queue tasks must be a list", stderr.getvalue())

    def test_find_handoff_task_filters_pending_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "tasks.json"
            queue.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "old",
                                "to": "codex",
                                "issue_id": 9020,
                                "type": "review_request",
                                "status": "completed",
                                "created_at": "2026-05-01T00:00:00Z",
                            },
                            {
                                "id": "wanted",
                                "to": "codex",
                                "issue_id": 9020,
                                "type": "review_request",
                                "status": "pending",
                                "created_at": "2026-05-02T00:00:00Z",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(queue=str(queue), task_id="wanted", issue="9020", type="review_request")

            task = find_handoff_task(args, "codex")

            self.assertEqual("wanted", task["id"])

    def test_find_handoff_task_raises_for_completed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "tasks.json"
            queue.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "done",
                                "to": "codex",
                                "issue_id": 9020,
                                "type": "review_request",
                                "status": "completed",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(queue=str(queue), task_id="done", issue="9020", type="review_request")

            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    find_handoff_task(args, "codex")


class PathResolutionTest(unittest.TestCase):
    def test_find_repo_root_walks_up_to_project_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            (root / ".tmux.conf").write_text("", encoding="utf-8")

            self.assertEqual(root.resolve(), find_repo_root(nested))

    def test_resolve_repo_root_prefers_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(Path(tmp).resolve(), resolve_repo_root(tmp))

    def test_default_paths_are_relative_to_resolved_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".tmux.conf").write_text("", encoding="utf-8")

            self.assertEqual(root / ".agent_handoff" / "tasks.json", default_queue_path(root))
            self.assertEqual(root / ".tmux.conf", default_tmux_conf(root))


class PaneResolverTest(unittest.TestCase):
    def test_is_tmux_target(self) -> None:
        self.assertTrue(is_tmux_target("%1"))
        self.assertTrue(is_tmux_target("agents:0"))
        self.assertTrue(is_tmux_target("agents:0.1"))
        self.assertFalse(is_tmux_target("codex"))

    def test_find_labeled_pane_rejects_duplicates(self) -> None:
        panes = [
            {"id": "%1", "location": "agents:0.0", "label": "codex"},
            {"id": "%2", "location": "agents:0.1", "label": "codex"},
        ]

        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    find_labeled_pane("codex", session="agents", fallback=False)

    def test_find_labeled_pane_prefers_current_session(self) -> None:
        panes = [
            {"id": "%1", "location": "other:0.0", "label": "codex"},
            {"id": "%2", "location": "agents:0.1", "label": "codex"},
        ]

        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            self.assertEqual("%2", find_labeled_pane("codex", session="agents")["id"])

    def test_find_labeled_pane_no_fallback_returns_none(self) -> None:
        panes = [{"id": "%1", "location": "other:0.0", "label": "codex"}]

        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            self.assertIsNone(find_labeled_pane("codex", session="agents", fallback=False))

    def test_ensure_agent_target_accepts_node_for_labeled_codex(self) -> None:
        pane = {"label": "codex", "command": "node"}

        ensure_agent_target(pane, "codex")

    def test_ensure_agent_target_accepts_versioned_native_binary_for_labeled_claude(self) -> None:
        pane = {"label": "claude", "command": "2.1.138"}

        ensure_agent_target(pane, "claude")

    def test_is_agent_process_accepts_versioned_native_binary(self) -> None:
        self.assertTrue(is_agent_process("2.1.138"))

    def test_ensure_agent_target_rejects_shell_without_force(self) -> None:
        pane = {"label": "", "command": "bash"}

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                ensure_agent_target(pane, "codex")

    def test_read_marker_allows_recent_matching_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_prefix = pane_resolver.READ_MARK_PREFIX
            pane_resolver.READ_MARK_PREFIX = str(Path(tmp) / "read-")
            try:
                mark_read("%2")

                require_read("%2")

                clear_read("%2")
            finally:
                pane_resolver.READ_MARK_PREFIX = original_prefix

    def test_read_marker_rejects_expired_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original_prefix = pane_resolver.READ_MARK_PREFIX
            pane_resolver.READ_MARK_PREFIX = str(Path(tmp) / "read-")
            marker = pane_resolver.read_mark_path("%2")
            marker.write_text(json.dumps({"pane_id": "%2", "created_at": time.time() - 1000}), encoding="utf-8")
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        require_read("%2")

                self.assertFalse(marker.exists())
            finally:
                pane_resolver.READ_MARK_PREFIX = original_prefix


class CliTest(unittest.TestCase):
    def test_primary_commands_parse(self) -> None:
        parser = build_parser()

        self.assertEqual("status", parser.parse_args(["status"]).command)
        self.assertEqual("init", parser.parse_args(["init", "codex"]).command)
        self.assertEqual("notify-codex-review", parser.parse_args(["notify-codex-review", "--issue", "9020"]).command)
        self.assertEqual("rules", parser.parse_args(["rules", "install"]).command)
        self.assertEqual("scaffold", parser.parse_args(["scaffold", "rules", "asana"]).command)
        self.assertEqual("doctor", parser.parse_args(["doctor"]).command)

    def test_notify_codex_accepts_type(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["notify-codex", "--issue", "9020", "--journal", "1", "--type", "review_request"])

        self.assertEqual("notify-codex", args.command)
        self.assertEqual("review_request", args.type)

    def test_standard_notify_rejects_legacy_task_id(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["notify-codex", "--issue", "9020", "--task-id", "legacy-task"])

    def test_standard_notify_rejects_tmux_ui_options(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["notify-codex", "--issue", "9020", "--journal", "1", "--ensure"])

    def test_legacy_task_notification_is_separate_command(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["notify-codex-legacy-task", "--issue", "9020", "--task-id", "legacy-task", "--type", "review_request"]
        )

        self.assertEqual("notify-codex-legacy-task", args.command)
        self.assertEqual("legacy-task", args.task_id)

    def test_tmux_ui_open_accepts_setup_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["tmux-ui-open", "--session", "agents", "--cwd", "/repo", "--ready-timeout", "0"])

        self.assertEqual("tmux-ui-open", args.command)
        self.assertEqual("agents", args.session)
        self.assertEqual("/repo", args.cwd)

    def test_normalize_paths_marks_default_config_path(self) -> None:
        from mozyo_bridge.application.cli import normalize_paths

        parsed = build_parser().parse_args(["tmux-ui-open"])
        normalized = normalize_paths(parsed)
        self.assertTrue(normalized.config_path_was_default)
        self.assertIsNotNone(normalized.config_path)

    def test_normalize_paths_marks_explicit_config_path(self) -> None:
        from mozyo_bridge.application.cli import normalize_paths

        parsed = build_parser().parse_args(["tmux-ui-open", "--config-path", "/explicit/.tmux.conf"])
        normalized = normalize_paths(parsed)
        self.assertFalse(normalized.config_path_was_default)
        self.assertEqual("/explicit/.tmux.conf", normalized.config_path)

    def test_open_here_defaults_session_and_cwd_to_none_before_normalization(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["open-here"])

        self.assertEqual("open-here", args.command)
        self.assertIsNone(args.session)
        self.assertIsNone(args.cwd)
        self.assertTrue(args.config)

    def test_open_here_accepts_explicit_session_override(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["open-here", "--session", "custom", "--repo", "/repo"])

        self.assertEqual("open-here", args.command)
        self.assertEqual("custom", args.session)
        self.assertEqual("/repo", args.repo)

    def test_bare_mozyo_parses_with_no_subcommand(self) -> None:
        parser = build_parser()

        args = parser.parse_args([])

        self.assertIsNone(args.command)
        self.assertFalse(args.no_attach)

    def test_bare_mozyo_accepts_no_attach_flag(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["--no-attach"])

        self.assertIsNone(args.command)
        self.assertTrue(args.no_attach)

    def test_bare_mozyo_accepts_top_level_repo_override(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["--repo", "/explicit/repo"])

        self.assertIsNone(args.command)
        self.assertEqual("/explicit/repo", args.repo)

    def test_subcommand_repo_still_works_after_top_level_repo_added(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["open-here", "--repo", "/repo"])

        self.assertEqual("open-here", args.command)
        self.assertEqual("/repo", args.repo)

    def test_version_flag_prints_version_and_exits_cleanly(self) -> None:
        parser = build_parser()
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as ctx:
                parser.parse_args(["--version"])

        self.assertEqual(0, ctx.exception.code)
        self.assertIn(__version__, stdout.getvalue())
        self.assertIn("mozyo-bridge", stdout.getvalue())

    def test_module_version_matches_pyproject_version(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"', pyproject, flags=re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), __version__)

    def test_scaffold_rules_rejects_unknown_preset(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["scaffold", "rules", "jira"])


class ScaffoldRulesTest(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def test_rules_install_and_scaffold_asana_thin_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            result, _ = self.run_cli(["rules", "install", "--home", str(home)])
            self.assertEqual(0, result)
            asana_workflow = home / "rules" / "presets" / "asana" / "agent-workflow.md"
            self.assertTrue(asana_workflow.exists())
            installed_workflow = asana_workflow.read_text(encoding="utf-8")
            self.assertIn("User Interaction And Escalation", installed_workflow)
            self.assertIn("designated coordinator", installed_workflow)
            self.assertIn("Role Boundaries", installed_workflow)
            self.assertIn("coordinating/auditing agent must not directly implement", installed_workflow)
            # Asana-native guardrails added in this task.
            for marker in (
                "Factual Posture",
                "Prioritize factual correctness",
                "review input, not completion",
                "Handoff Startup Decision",
                "Receiver pane unavailable",
                "Notification fails",
                "mozyo-bridge init",
                "Receive method id",
                "Asana API",
                "Scope Preservation",
                "residual scope",
                "Decision Routing",
                # Ticket-ID entrypoint runtime reflection.
                "Ticket-ID Entrypoint",
                'ticket-ID only',
                "pane / chat body looks fully framed",
                "task comment / story id",
                # Audit-owned commit authority codified in this task.
                "Audit-Owned Commit Authority",
                "commit authority, not an implementation authority",
                "Refs: Asana task <task_id>",
                "Audit: Asana comment <comment_id>",
                "git diff --cached --stat",
                "git add -A",
                "commit-hash comment",
            ):
                self.assertIn(marker, installed_workflow)
            # Asana central preset must NOT import Redmine journal / gate semantics.
            for forbidden in (
                "Redmine journal",
                "Review Gate",
                "Implementation Done Gate",
                "Close Gate",
                "Design Consultation Gate",
            ):
                self.assertNotIn(forbidden, installed_workflow)

            result, output = self.run_cli(["scaffold", "rules", "asana", "--target", str(project), "--home", str(home)])

            self.assertEqual(0, result)
            self.assertIn("AGENTS.md", output)
            self.assertTrue((project / "AGENTS.md").exists())
            self.assertTrue((project / "CLAUDE.md").exists())
            self.assertFalse((project / "vibes" / "docs" / "rules" / "asana-agent-workflow.md").exists())
            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md",
                agents,
            )
            # The router must not leak the host-resolved home path or any
            # user-specific absolute home path.
            self.assertNotIn(str(home), agents)
            self.assertNotIn("/Users/", agents)
            self.assertIn("Asana task state と task comment", agents)
            # Generated routers must not name vibes/docs/* paths as runtime context.
            # vibes/docs/ is this repo's design/spec source, not an external scaffold
            # target's runtime convention.
            self.assertNotIn("vibes/docs/specs/project-map.md", agents)
            self.assertNotIn("vibes/docs/rules/agent-workflow.md", agents)
            self.assertNotIn("vibes/docs/", agents)
            # The Project-Local Context heading was folded into step 3.
            self.assertNotIn("## Project-Local Context", agents)
            self.assertIn("target project 側の任意の convention", agents)
            self.assertIn("mozyo-bridge の runtime 必須参照ではない", agents)

            claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md",
                claude,
            )
            self.assertNotIn(str(home), claude)
            self.assertNotIn("/Users/", claude)
            self.assertIn("ClaudeCode 起動時の最小 reminder", claude)
            self.assertIn("迎合せず", claude)
            self.assertIn("implementation done は task complete ではない", claude)
            self.assertIn("Asana task comment", claude)
            self.assertIn("受領方法", claude)
            # CLAUDE.md stays thin even with the Claude-specific reminder block.
            self.assertLess(len(claude.splitlines()), 30)
            # Asana CLAUDE.md must not import Redmine-specific vocabulary.
            for forbidden in ("Redmine journal", "Review Gate", "Implementation Done Gate"):
                self.assertNotIn(forbidden, claude)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("central", state["mode"])
            self.assertEqual("asana", state["preset"])
            # Schema v2 + preset_hash from the previous task must still be in effect.
            self.assertEqual(2, state["schema_version"])
            self.assertEqual(
                hashlib.sha256(asana_workflow.read_bytes()).hexdigest(),
                state["preset_hash"],
            )
            self.assertEqual("2026.05.12.1", state["preset_version"])
            self.assertIn("AGENTS.md", state["files"])

            # The audit-owned commit policy belongs in the central preset only.
            # Root routers stay thin and must not duplicate the policy body.
            self.assertNotIn("Audit-Owned Commit Authority", agents)
            self.assertNotIn("Audit-Owned Commit Authority", claude)
            self.assertNotIn("Refs: Asana task", agents)
            self.assertNotIn("Refs: Asana task", claude)

    def test_rules_install_and_scaffold_redmine_thin_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            result, _ = self.run_cli(["rules", "install", "--home", str(home)])
            self.assertEqual(0, result)
            redmine_workflow = home / "rules" / "presets" / "redmine" / "agent-workflow.md"
            self.assertTrue(redmine_workflow.exists())
            installed = redmine_workflow.read_text(encoding="utf-8")
            for marker in (
                "Redmine Gate Lifecycle",
                "Start Gate",
                "Progress Log Gate",
                "Design Consultation Gate",
                "Implementation Done Gate",
                "Review Request Gate",
                "Review Gate",
                "Close Gate",
                "Pane Notification",
                "Handoff Startup Decision",
                "Factual Posture",
                "Implementer / Auditor Role Boundary",
                "Decision Routing",
                "Scope Integrity",
                "Verification Discipline",
                "mozyo-bridge notify-",
                "mozyo-bridge init",
                "Receiver pane unavailable",
                "Notification fails",
                "Implementation Done is not",
                "Stop hook handoff waits",
                "Prioritize factual correctness",
                # Ticket-ID entrypoint runtime reflection.
                "Ticket-ID Entrypoint",
                'ticket-ID only',
                "pane / chat body looks fully framed",
                "canonical handoff id is the Redmine journal",
                # Audit-owned commit authority codified in this task.
                "Audit-Owned Commit Authority",
                "commit authority, not an implementation authority",
                "Refs: Redmine #<issue_id>",
                "Journal: <journal_id>",
                "Review Gate journal recording approval",
                "git diff --cached --stat",
                "git add -A",
                "Close Gate journal on the same issue",
            ):
                self.assertIn(marker, installed)
            self.assertIn(
                'Do not hard-code a fixed agent role split such as "Claude Code implements, Codex only audits"',
                installed,
            )
            self.assertNotIn("python3 vibes/tools/mozyo_bridge", installed)
            self.assertNotIn(".claude-nagger/file_conventions.yaml", installed)
            self.assertNotIn("resolve_audit_docs.py", installed)
            self.assertNotIn("vibes/docs/catalog.yaml", installed)
            self.assertNotIn("manual_spec", installed)
            self.assertNotIn("FeatureListDsl", installed)
            self.assertNotIn("/myapp/Source/rails", installed)
            self.assertNotIn("tmux-integrated", installed)
            self.assertNotIn("VS Code", installed)
            # Implementation Done Gate is a distinct durable gate, not a Progress Log.
            # The Factual Posture wording must not downgrade it.
            self.assertNotIn("self-verification is a Progress Log", installed)
            self.assertIn("review input, not completion", installed)

            result, output = self.run_cli(
                ["scaffold", "rules", "redmine", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(0, result)
            self.assertIn("AGENTS.md", output)
            self.assertTrue((project / "AGENTS.md").exists())
            self.assertTrue((project / "CLAUDE.md").exists())
            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md",
                agents,
            )
            self.assertNotIn(str(home), agents)
            self.assertNotIn("/Users/", agents)
            self.assertIn("Redmine issue と journal state", agents)
            self.assertIn("Redmine gate lifecycle", agents)
            self.assertIn("mozyo-bridge notify-", agents)
            # Generated routers must not name vibes/docs/* paths as runtime context.
            # vibes/docs/ is this repo's design/spec source, not an external scaffold
            # target's runtime convention.
            self.assertNotIn("vibes/docs/specs/project-map.md", agents)
            self.assertNotIn("vibes/docs/rules/agent-workflow.md", agents)
            self.assertNotIn("vibes/docs/", agents)
            self.assertNotIn("## Project-Local Context", agents)
            self.assertIn("target project 側の任意の convention", agents)
            self.assertIn("mozyo-bridge の runtime 必須参照ではない", agents)

            claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md",
                claude,
            )
            self.assertNotIn(str(home), claude)
            self.assertNotIn("/Users/", claude)
            self.assertIn("ClaudeCode 起動時の最小 reminder", claude)
            self.assertIn("迎合せず", claude)
            self.assertIn("implementation_done は completion ではない", claude)
            self.assertIn("Codex受領方法", claude)
            # Router stays thin: keep CLAUDE.md well below the central preset's depth.
            self.assertLess(len(claude.splitlines()), 30)
            self.assertNotIn("Redmine Gate Lifecycle", claude)
            self.assertNotIn("Implementer / Auditor Role Boundary", claude)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("central", state["mode"])
            self.assertEqual("redmine", state["preset"])
            self.assertIn("AGENTS.md", state["files"])
            self.assertEqual("2026.05.12.1", state["preset_version"])

            # The audit-owned commit policy belongs in the central preset only.
            # Root routers stay thin and must not duplicate the policy body.
            self.assertNotIn("Audit-Owned Commit Authority", agents)
            self.assertNotIn("Audit-Owned Commit Authority", claude)
            self.assertNotIn("Refs: Redmine #", agents)
            self.assertNotIn("Refs: Redmine #", claude)

    def test_scaffold_requires_installed_central_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(["scaffold", "rules", "redmine", "--target", str(project), "--home", str(home)])

            self.assertIn("rules preset is not installed", stderr.getvalue())
            self.assertFalse((project / "AGENTS.md").exists())

    def test_scaffold_without_target_writes_to_current_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            parent = Path(tmp) / "parent"
            nested = parent / "nested"
            nested.mkdir(parents=True)
            (parent / "pyproject.toml").write_text("[project]\nname = \"parent\"\n", encoding="utf-8")
            self.run_cli(["rules", "install", "--home", str(home)])
            cwd = Path.cwd()
            try:
                os.chdir(nested)

                result, output = self.run_cli(["scaffold", "rules", "asana", "--home", str(home)])

                self.assertEqual(0, result)
                self.assertIn(str(nested / "AGENTS.md"), output)
                self.assertTrue((nested / "AGENTS.md").exists())
                self.assertTrue((nested / "CLAUDE.md").exists())
                self.assertFalse((parent / "AGENTS.md").exists())
            finally:
                os.chdir(cwd)

    def test_scaffold_without_target_ignores_mozyo_repo_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            env_repo = Path(tmp) / "env-repo"
            cwd_project = Path(tmp) / "cwd-project"
            env_repo.mkdir()
            cwd_project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            cwd = Path.cwd()
            try:
                os.chdir(cwd_project)
                with patch.dict(os.environ, {"MOZYO_REPO": str(env_repo)}):
                    result, _ = self.run_cli(["scaffold", "rules", "none", "--home", str(home)])

                self.assertEqual(0, result)
                self.assertTrue((cwd_project / "AGENTS.md").exists())
                self.assertFalse((env_repo / "AGENTS.md").exists())
            finally:
                os.chdir(cwd)

    def test_rules_status_reports_installed_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"

            self.run_cli(["rules", "install", "--home", str(home)])

            result, output = self.run_cli(["rules", "status", "--home", str(home)])
            rows = rules_status(home)

            self.assertEqual(0, result)
            self.assertIn("PRESET\tSTATUS\tINSTALLED\tPACKAGED\tPATH", output)
            self.assertEqual(["ok", "ok", "ok"], [row["status"] for row in rows])
            self.assertIn(f"asana\tok\t{package_version('asana')}\t{package_version('asana')}\t", output)
            self.assertIn(str(home / "rules" / "presets" / "asana" / "agent-workflow.md"), output)

    def test_rules_status_reports_missing_and_outdated_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"

            self.run_cli(["rules", "install", "--home", str(home)])
            (home / "rules" / "presets" / "redmine" / "agent-workflow.md").unlink()
            (home / "rules" / "presets" / "none" / "VERSION").write_text("0.0.0\n", encoding="utf-8")

            result, output = self.run_cli(["rules", "status", "--home", str(home)])
            rows = {row["preset"]: row for row in rules_status(home)}

            self.assertEqual(1, result)
            self.assertEqual("ok", rows["asana"]["status"])
            self.assertEqual("missing", rows["redmine"]["status"])
            self.assertEqual("-", rows["redmine"]["installed"])
            self.assertEqual("outdated", rows["none"]["status"])
            self.assertEqual("0.0.0", rows["none"]["installed"])
            self.assertIn(f"redmine\tmissing\t-\t{package_version('redmine')}\t", output)
            self.assertIn(f"none\toutdated\t0.0.0\t{package_version('none')}\t", output)

    def test_scaffold_refuses_overwrite_by_default_and_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(["scaffold", "rules", "none", "--target", str(project), "--home", str(home)])

            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.run_cli(["scaffold", "rules", "none", "--target", str(project), "--home", str(home)])

            fresh = Path(tmp) / "fresh"
            fresh.mkdir()
            result, output = self.run_cli(
                ["scaffold", "rules", "none", "--target", str(fresh), "--home", str(home), "--dry-run"]
            )

            self.assertEqual(0, result)
            self.assertIn("would write", output)
            self.assertFalse((fresh / "AGENTS.md").exists())

    def test_scaffold_backup_replaces_existing_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "AGENTS.md").write_text("old agents\n", encoding="utf-8")
            (project / "CLAUDE.md").write_text("old claude\n", encoding="utf-8")
            self.run_cli(["rules", "install", "--home", str(home)])

            result, _ = self.run_cli(["scaffold", "rules", "redmine", "--target", str(project), "--home", str(home), "--backup"])

            self.assertEqual(0, result)
            self.assertIn("Redmine issue と journal state", (project / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertTrue(list(project.glob("AGENTS.md.bak.*")))
            self.assertTrue(list(project.glob("CLAUDE.md.bak.*")))

    def test_relative_home_does_not_leak_into_router_or_manifest(self) -> None:
        # Even when --home resolves to a host-specific absolute path, the
        # generated router and the scaffold manifest must record the portable
        # ${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge} symbolic form. This guards
        # against personal-home leakage in committed artifacts.
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            try:
                os.chdir(tmp)
                project = Path(tmp) / "project"
                project.mkdir()
                self.run_cli(["rules", "install", "--home", "home"])

                self.run_cli(["scaffold", "rules", "asana", "--target", str(project), "--home", "home"])

                state = scaffold_state(project)
                self.assertIsNotNone(state)
                assert state is not None
                self.assertEqual(
                    "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md",
                    state["rule_path"],
                )

                resolved_home = (Path(tmp) / "home").resolve()
                for filename in ("AGENTS.md", "CLAUDE.md", ".mozyo-bridge/scaffold.json"):
                    text = (project / filename).read_text(encoding="utf-8")
                    self.assertNotIn(str(resolved_home), text)
                    self.assertIn(
                        "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md",
                        text,
                    )
            finally:
                os.chdir(cwd)

    def test_scaffold_does_not_leak_home_path_for_any_preset(self) -> None:
        # Fresh scaffold for every supported preset must avoid leaking the
        # resolved host home path into AGENTS.md, CLAUDE.md, or the manifest,
        # and must instead reference the portable symbolic form. The MOZYO_BRIDGE_HOME
        # override semantics are preserved because consumers expand the env var
        # when they read the router, not when the router is generated.
        for preset in ("asana", "redmine", "none"):
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / "home"
                    project = Path(tmp) / "project"
                    project.mkdir()

                    self.run_cli(["rules", "install", "--home", str(home)])
                    result, _ = self.run_cli(
                        ["scaffold", "rules", preset, "--target", str(project), "--home", str(home)]
                    )
                    self.assertEqual(0, result)

                    resolved_home = home.resolve()
                    expected_rule_path = (
                        f"${{MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}}/rules/presets/{preset}/agent-workflow.md"
                    )
                    for filename in ("AGENTS.md", "CLAUDE.md", ".mozyo-bridge/scaffold.json"):
                        text = (project / filename).read_text(encoding="utf-8")
                        self.assertNotIn("/Users/", text)
                        self.assertNotIn(str(resolved_home), text)
                        self.assertIn(expected_rule_path, text)

                    state = scaffold_state(project)
                    self.assertIsNotNone(state)
                    assert state is not None
                    self.assertEqual(expected_rule_path, state["rule_path"])

                    status_result, status_output = self.run_cli(
                        ["scaffold", "status", "--target", str(project), "--home", str(home)]
                    )
                    self.assertEqual(0, status_result)
                    self.assertIn("clean", status_output)


class SharedSkillWorkflowTest(unittest.TestCase):
    """The shared mozyo-bridge-agent skill reference must carry the cross-system
    audit-owned commit policy so Codex behavior stays consistent across Asana
    and Redmine projects."""

    def setUp(self) -> None:
        self.workflow_path = (
            ROOT / "skills" / "mozyo-bridge-agent" / "references" / "workflow.md"
        )
        self.workflow = self.workflow_path.read_text(encoding="utf-8")

    def test_audit_owned_commit_section_present(self) -> None:
        # Section header and policy headline.
        self.assertIn("## Audit-Owned Commit Authority", self.workflow)
        # Cross-system boundary statement.
        self.assertIn("commit authority, not an implementation authority", self.workflow)
        self.assertIn("Codex direct implementation edit", self.workflow)
        self.assertIn("Codex audit-owned commit", self.workflow)

    def test_audit_owned_commit_has_preflight_steps(self) -> None:
        self.assertIn("git status", self.workflow)
        self.assertIn("git diff --cached --stat", self.workflow)
        self.assertIn("git add -A", self.workflow)
        # Per-system commit message reference contract.
        self.assertIn("Refs: Asana task <task_id>", self.workflow)
        self.assertIn("Audit: Asana comment <comment_id>", self.workflow)
        self.assertIn("Refs: Redmine #<issue_id>", self.workflow)
        self.assertIn("Journal: <journal_id>", self.workflow)
        # Commit hash must be recorded in the durable record, not pane chat.
        self.assertIn("Record the commit hash", self.workflow)

    def test_audit_owned_commit_does_not_grant_direct_implementation(self) -> None:
        # The audit-owned commit section must NOT contain wording that could be
        # read as permission for Codex to write implementation diffs as part of
        # the commit step. We isolate the new section to keep this test from
        # tripping on the legitimate Codex direct-edit *exception* phrasing in
        # the Policy / Skill Authoring Boundary section.
        section_start = self.workflow.index("## Audit-Owned Commit Authority")
        section_end = self.workflow.index("## Workflow Change Verification", section_start)
        section = self.workflow[section_start:section_end]
        self.assertNotIn("Codex may edit", section)
        self.assertNotIn("Codex may implement", section)
        self.assertNotIn("Codex implements normal", section)
        # The section must explicitly preserve the prohibition on Codex
        # producing new diffs while granting the commit-only authority.
        self.assertIn("Codex must not edit implementation files", section)
        self.assertIn("commit authority, not an implementation authority", section)
        # The section must NOT silently waive the implementer / auditor
        # boundary defined elsewhere.
        self.assertIn("does not waive the implementer / auditor boundary", section)


class ScaffoldStatusTest(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def _setup_scaffold(self, tmp: Path, preset: str = "redmine") -> tuple[Path, Path]:
        home = tmp / "home"
        project = tmp / "project"
        project.mkdir()
        self.run_cli(["rules", "install", "--home", str(home)])
        self.run_cli(["scaffold", "rules", preset, "--target", str(project), "--home", str(home)])
        return home, project

    def test_manifest_records_preset_hash_and_schema_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp), "redmine")
            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(2, state["schema_version"])
            workflow = home / "rules" / "presets" / "redmine" / "agent-workflow.md"
            expected_hash = hashlib.sha256(workflow.read_bytes()).hexdigest()
            self.assertEqual(expected_hash, state["preset_hash"])
            self.assertIn("AGENTS.md", state["files"])
            self.assertIn("sha256", state["files"]["AGENTS.md"])

    def test_scaffold_status_reports_clean_after_fresh_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, result)
            self.assertIn("manifest: present", output)
            self.assertIn("central status: ok", output)
            self.assertIn("result: clean", output)

    def test_scaffold_status_reports_clean_after_fresh_asana_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp), preset="asana")
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, result)
            self.assertIn("manifest: present", output)
            self.assertIn("central status: ok", output)
            self.assertIn("result: clean", output)

    def test_scaffold_status_detects_central_preset_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            workflow = home / "rules" / "presets" / "redmine" / "agent-workflow.md"
            workflow.write_text(
                workflow.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8"
            )
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("central status: drifted-content", output)
            self.assertIn("result: drift detected", output)
            self.assertIn("central preset content has changed", output)

    def test_scaffold_status_detects_router_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            agents_path = project / "AGENTS.md"
            agents_path.write_text(
                agents_path.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8"
            )
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("AGENTS.md: drifted", output)
            self.assertIn("router AGENTS.md was modified locally", output)

    def test_scaffold_status_reports_missing_central_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            preset_dir = home / "rules" / "presets" / "redmine"
            shutil.rmtree(preset_dir)
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("central status: missing", output)
            self.assertIn("`mozyo-bridge rules install`", output)

    def test_scaffold_status_reports_missing_manifest_for_empty_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            home = Path(tmp) / "home"
            self.run_cli(["rules", "install", "--home", str(home)])
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(empty), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("manifest: missing", output)
            self.assertIn("no scaffold manifest", output)

    def test_scaffold_status_handles_schema_v1_manifest_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            manifest_path = project / ".mozyo-bridge" / "scaffold.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            # Simulate a manifest written by a pre-hash version of the scaffolder.
            data["schema_version"] = 1
            data.pop("preset_hash", None)
            manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            # Same version + no hash means we can't prove content drift, but the
            # known router hashes still verify; treat as drift so the user upgrades.
            self.assertEqual(1, result)
            self.assertIn("central status: ok-version-only", output)
            self.assertIn("schema v1 (no preset_hash)", output)

    def test_scaffold_status_reports_invalid_manifest_on_bad_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            manifest_path = project / ".mozyo-bridge" / "scaffold.json"
            manifest_path.write_text("{bad json", encoding="utf-8")
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("manifest: invalid", output)
            self.assertIn("manifest is not valid JSON", output)

    def test_scaffold_status_json_output_for_invalid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            manifest_path = project / ".mozyo-bridge" / "scaffold.json"
            manifest_path.write_text("{bad json", encoding="utf-8")
            result, output = self.run_cli(
                [
                    "scaffold",
                    "status",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            self.assertEqual(1, result)
            payload = json.loads(output)
            self.assertEqual("invalid", payload["manifest"])
            self.assertFalse(payload["clean"])
            self.assertIn("error", payload)

    def test_scaffold_status_rejects_schema_v2_manifest_with_missing_router_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            manifest_path = project / ".mozyo-bridge" / "scaffold.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["files"] = {}
            manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("manifest: invalid", output)
            self.assertIn("schema v2 manifest is missing router hash entries", output)
            self.assertIn("AGENTS.md", output)
            self.assertIn("CLAUDE.md", output)

    def test_scaffold_status_rejects_schema_v2_manifest_with_partial_router_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            manifest_path = project / ".mozyo-bridge" / "scaffold.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["files"].pop("CLAUDE.md", None)
            manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("manifest: invalid", output)
            self.assertIn("CLAUDE.md", output)
            self.assertNotIn("result: clean", output)

    def test_scaffold_status_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            result, output = self.run_cli(
                [
                    "scaffold",
                    "status",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            self.assertEqual(0, result)
            payload = json.loads(output)
            self.assertTrue(payload["clean"])
            self.assertEqual("redmine", payload["preset"])
            self.assertEqual(2, payload["schema_version"])
            self.assertEqual("ok", payload["central_status"])
            self.assertEqual(
                payload["manifest_preset_hash"], payload["installed_preset_hash"]
            )


class NotifyContractTest(unittest.TestCase):
    def run_notify_with_fake_tmux(
        self,
        argv: list[str],
        captures: list[str] | None = None,
        allow_exit: bool = False,
    ):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []
        pane_text = ""
        forced_captures = captures is not None
        capture_outputs = list(captures or [])

        def fake_capture(_target: str, _lines: int) -> str:
            if capture_outputs:
                return capture_outputs.pop(0)
            if forced_captures:
                return ""
            return pane_text

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                text = tmux_args[-1]
                pane_text += text
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            if tmux_args[:3] == ("send-keys", "-t", "%2"):
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        pane = {"id": "%2", "location": "agents:0.1", "label": "codex", "command": "node", "cwd": "/repo"}

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.pane_label", return_value="claude"), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.0"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue(), pane_text

    def test_notify_by_journal_types_observed_text_then_submits(self) -> None:
        result, sent, stdout, pane_text = self.run_notify_with_fake_tmux(
            [
                "notify-codex",
                "--issue",
                "9020",
                "--journal",
                "46005",
                "--type",
                "review_request",
                "--target",
                "%2",
                "--force",
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn("[mozyo:notify:issue=9020:journal=46005:type=review_request]", pane_text)
        self.assertIn("Redmine #9020 journal #46005 is ready for codex", pane_text)
        self.assertIn("notified codex: journal=46005 target=%2 read_lines=20", stdout)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

    def test_legacy_task_notification_uses_separate_contract(self) -> None:
        task = {"id": "legacy-task", "issue_id": 9596, "commit": "abc123", "type": "design_consultation_result"}
        with patch("mozyo_bridge.application.commands.find_handoff_task", return_value=task) as find_task:
            result, _sent, _stdout, pane_text = self.run_notify_with_fake_tmux(
                [
                    "notify-claude-legacy-task",
                    "--issue",
                    "9596",
                    "--task-id",
                    "legacy-task",
                    "--type",
                    "design_consultation_result",
                    "--target",
                    "%2",
                    "--force",
                    "--submit-delay",
                    "0",
                ]
            )

        self.assertEqual(0, result)
        find_task.assert_called_once()
        self.assertIn("[mozyo:notify:task=legacy-task:issue=9596]", pane_text)
        self.assertIn("handoff task legacy-task is ready for claude", pane_text)
        self.assertIn("legacy queue fallback", pane_text)

    def test_notify_does_not_submit_when_marker_is_not_observed_contract(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            result, sent, _stdout, _pane_text = self.run_notify_with_fake_tmux(
                [
                    "notify-codex",
                    "--issue",
                    "9020",
                    "--journal",
                    "46005",
                    "--target",
                    "%2",
                    "--force",
                    "--landing-timeout",
                    "0.01",
                    "--submit-delay",
                    "0",
                ],
                captures=["", "", ""],
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "Enter") for call in sent))
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)

    def test_notify_submit_delay_default_is_classic_short_tui_delay(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["notify-codex", "--issue", "9020", "--journal", "1"])

        self.assertEqual(0.2, args.submit_delay)


class MessageContractTest(unittest.TestCase):
    def run_message_with_fake_tmux(
        self,
        argv: list[str],
        captures: list[str] | None = None,
        allow_exit: bool = False,
    ):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []
        pane_text = ""
        forced_captures = captures is not None
        capture_outputs = list(captures or [])

        def fake_capture(_target: str, _lines: int) -> str:
            if capture_outputs:
                return capture_outputs.pop(0)
            if forced_captures:
                return ""
            return pane_text

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                pane_text += tmux_args[-1]
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.require_read"), \
            patch("mozyo_bridge.application.commands.clear_read"), \
            patch("mozyo_bridge.application.commands.resolve_target", return_value="%2"), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.pane_label", return_value="codex"), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.0"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep") as sleep, \
            contextlib.redirect_stdout(io.StringIO()):
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, pane_text, sleep

    def test_message_submits_enter_after_marker_by_default(self) -> None:
        result, sent, pane_text, _sleep = self.run_message_with_fake_tmux(
            ["message", "%2", "hello body", "--submit-delay", "0"]
        )

        self.assertEqual(0, result)
        self.assertIn("[mozyo-bridge from:codex pane:%1 at:agents:0.0]", pane_text)
        self.assertIn("hello body", pane_text)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

    def test_message_no_submit_leaves_input_pending(self) -> None:
        result, sent, pane_text, _sleep = self.run_message_with_fake_tmux(
            ["message", "%2", "pending body", "--no-submit"]
        )

        self.assertEqual(0, result)
        self.assertIn("pending body", pane_text)
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "Enter") for call in sent))
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

    def test_message_rolls_back_when_marker_is_not_observed(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            result, sent, _pane_text, _sleep = self.run_message_with_fake_tmux(
                [
                    "message",
                    "%2",
                    "lost body",
                    "--landing-timeout",
                    "0.01",
                    "--submit-delay",
                    "0",
                ],
                captures=["", "", ""],
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "Enter") for call in sent))
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)

    def test_message_waits_submit_delay_after_marker(self) -> None:
        _result, _sent, _pane_text, sleep = self.run_message_with_fake_tmux(
            ["message", "%2", "delayed body", "--submit-delay", "0.2"]
        )

        sleep.assert_called_once_with(0.2)

    def test_message_submit_defaults_to_true(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["message", "%2", "hi"])

        self.assertTrue(args.submit)
        self.assertEqual(0.2, args.submit_delay)
        self.assertEqual(5.0, args.landing_timeout)


class CommandTest(unittest.TestCase):
    def test_ensure_pair_loads_config_after_creating_missing_session(self) -> None:
        claude = {"id": "%1", "label": "claude", "command": "claude"}
        codex = {"id": "%2", "label": "codex", "command": "node"}
        args = argparse.Namespace(
            config=True,
            config_path="/repo/.tmux.conf",
            session="agents",
            cwd="/repo",
            vertical=False,
            force=False,
            ready_timeout=0,
        )
        run_result = argparse.Namespace(stdout="%1\t0\tclaude\tclaude\n%2\t1\tnode\tcodex\n")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", side_effect=[False, False]), \
            patch("mozyo_bridge.application.commands.new_agent_session", return_value="%1") as new_session, \
            patch("mozyo_bridge.application.commands.source_tmux_conf") as source_conf, \
            patch("mozyo_bridge.application.commands.find_labeled_pane", side_effect=[claude, None, claude, codex]), \
            patch("mozyo_bridge.application.commands.spawn_agent_terminal_pane", return_value="%2") as spawn, \
            patch("mozyo_bridge.application.commands.run_tmux", return_value=run_result), \
            patch("mozyo_bridge.application.commands.ensure_agent_target"), \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd_ensure_pair(args))

        new_session.assert_called_once_with("claude", "agents", cwd="/repo")
        source_conf.assert_called_once_with("/repo/.tmux.conf", optional=False)
        spawn.assert_called_once_with("codex", cwd="/repo", vertical=False, target="agents:0")

    def test_notify_agent_rejects_custom_label_with_ensure(self) -> None:
        args = argparse.Namespace(
            issue="9020",
            journal="1",
            task_id=None,
            queue="unused",
            target="codex2",
            ensure=True,
            config=False,
        )

        with patch("mozyo_bridge.application.commands.require_tmux"), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                notify_agent(args, "codex")

    def test_notify_agent_waits_for_marker_before_enter_on_existing_pane(self) -> None:
        args = argparse.Namespace(
            issue="9020",
            journal="1",
            task_id=None,
            queue="unused",
            target="%2",
            ensure=False,
            config=False,
            force=True,
            read_lines=20,
            prompt="custom prompt marker",
            cwd="/repo",
            vertical=False,
            ready_timeout=0,
            landing_timeout=5,
            submit_delay=0,
            type="review_request",
            commit="abc123",
        )
        pane = {"id": "%2", "label": "codex", "command": "node"}

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.find_handoff_task", return_value=None), \
            patch("mozyo_bridge.application.commands.pane_info", return_value=pane), \
            patch("mozyo_bridge.application.commands.ensure_agent_target"), \
            patch("mozyo_bridge.application.commands.cmd_read"), \
            patch("mozyo_bridge.application.commands.cmd_message"), \
            patch("mozyo_bridge.application.commands.wait_for_text", return_value=True) as wait_for_text, \
            patch("mozyo_bridge.application.commands.cmd_keys") as cmd_keys, \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, notify_agent(args, "codex"))

        wait_for_text.assert_called_once_with("%2", "[mozyo:notify:issue=9020:journal=1:type=review_request]", 200, 5)
        cmd_keys.assert_called_once()

    def test_notify_agent_does_not_press_enter_when_marker_missing(self) -> None:
        args = argparse.Namespace(
            issue="9020",
            journal="1",
            task_id=None,
            queue="unused",
            target="%2",
            ensure=False,
            config=False,
            force=True,
            read_lines=20,
            prompt="custom prompt marker",
            cwd="/repo",
            vertical=False,
            ready_timeout=0,
            landing_timeout=5,
            submit_delay=0,
            type="review_request",
            commit="abc123",
        )
        pane = {"id": "%2", "label": "codex", "command": "node"}

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.find_handoff_task", return_value=None), \
            patch("mozyo_bridge.application.commands.pane_info", return_value=pane), \
            patch("mozyo_bridge.application.commands.ensure_agent_target"), \
            patch("mozyo_bridge.application.commands.cmd_read"), \
            patch("mozyo_bridge.application.commands.cmd_message"), \
            patch("mozyo_bridge.application.commands.wait_for_text", return_value=False), \
            patch("mozyo_bridge.application.commands.rollback_unsubmitted_input") as rollback, \
            patch("mozyo_bridge.application.commands.cmd_keys") as cmd_keys, \
            contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                notify_agent(args, "codex")

        cmd_keys.assert_not_called()
        rollback.assert_called_once_with("%2")

    def test_notify_agent_waits_submit_delay_after_marker(self) -> None:
        args = argparse.Namespace(
            issue="9020",
            journal="1",
            task_id=None,
            queue="unused",
            target="%2",
            ensure=False,
            config=False,
            force=True,
            read_lines=20,
            prompt="custom prompt marker",
            cwd="/repo",
            vertical=False,
            ready_timeout=0,
            landing_timeout=5,
            submit_delay=0.2,
            type="review_request",
            commit="abc123",
        )
        pane = {"id": "%2", "label": "codex", "command": "node"}

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.find_handoff_task", return_value=None), \
            patch("mozyo_bridge.application.commands.pane_info", return_value=pane), \
            patch("mozyo_bridge.application.commands.ensure_agent_target"), \
            patch("mozyo_bridge.application.commands.cmd_read"), \
            patch("mozyo_bridge.application.commands.cmd_message"), \
            patch("mozyo_bridge.application.commands.wait_for_text", return_value=True), \
            patch("mozyo_bridge.application.commands.time.sleep") as sleep, \
            patch("mozyo_bridge.application.commands.cmd_keys"), \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, notify_agent(args, "codex"))

        sleep.assert_called_once_with(0.2)

    def test_open_creates_missing_session_before_attach(self) -> None:
        args = argparse.Namespace(
            session="agents",
            cwd="/repo",
            vertical=False,
            config=True,
            config_path="/repo/.tmux.conf",
            ready_timeout=0,
            force=False,
        )

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
            patch("mozyo_bridge.application.commands.cmd_ensure_pair", return_value=0) as ensure_pair, \
            patch("mozyo_bridge.application.commands.os.execvp", side_effect=RuntimeError("attached")) as execvp:
            with self.assertRaisesRegex(RuntimeError, "attached"):
                cmd_open(args)

        ensure_pair.assert_called_once()
        setup_args = ensure_pair.call_args.args[0]
        self.assertEqual("agents", setup_args.session)
        self.assertTrue(setup_args.config)
        execvp.assert_called_once_with("tmux", ["tmux", "attach", "-t", "agents"])

    def test_open_existing_session_reloads_config_before_attach(self) -> None:
        args = argparse.Namespace(
            session="agents",
            cwd="/repo",
            vertical=False,
            config=True,
            config_path="/repo/.tmux.conf",
            ready_timeout=0,
            force=False,
        )

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
            patch("mozyo_bridge.application.commands.source_tmux_conf") as source_conf, \
            patch("mozyo_bridge.application.commands.os.execvp", side_effect=RuntimeError("attached")):
            with self.assertRaisesRegex(RuntimeError, "attached"):
                cmd_open(args)

        source_conf.assert_called_once_with("/repo/.tmux.conf", optional=False)

    def _open_here_args(self, repo: Path, session=None, cwd=None) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(repo),
            session=session,
            cwd=cwd,
            vertical=False,
            config=True,
            config_path=str(repo / ".tmux.conf"),
            ready_timeout=0,
            force=False,
        )

    def test_open_here_defaults_session_to_repo_basename_and_attaches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            args = self._open_here_args(repo)

            captured: dict[str, argparse.Namespace] = {}

            def fake_cmd_open(inner: argparse.Namespace) -> int:
                captured["args"] = inner
                raise RuntimeError("attached")

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.cmd_open", side_effect=fake_cmd_open):
                with self.assertRaisesRegex(RuntimeError, "attached"):
                    cmd_open_here(args)

        self.assertEqual("my-project", captured["args"].session)
        self.assertEqual(str(repo), captured["args"].cwd)

    def test_open_here_preserves_user_supplied_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            args = self._open_here_args(repo, session="custom")
            captured: dict[str, argparse.Namespace] = {}

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=True) as sess, \
                patch("mozyo_bridge.application.commands.cmd_open",
                      side_effect=lambda inner: (captured.__setitem__("args", inner), 0)[1]):
                self.assertEqual(0, cmd_open_here(args))

        self.assertEqual("custom", captured["args"].session)
        sess.assert_not_called()  # explicit --session skips the collision probe

    def test_open_here_refuses_when_existing_session_panes_are_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            other = (Path(tmp) / "other-project").resolve()
            other.mkdir()
            args = self._open_here_args(repo)
            panes = [
                {"id": "%1", "location": "my-project:0.0", "command": "zsh", "label": "", "cwd": str(other)},
                {"id": "%2", "location": "my-project:0.1", "command": "zsh", "label": "", "cwd": str(other)},
            ]

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.cmd_open") as opened, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    cmd_open_here(args)

        opened.assert_not_called()
        self.assertIn("--session", stderr.getvalue())
        self.assertIn("my-project", stderr.getvalue())

    def test_open_here_attaches_when_existing_session_has_pane_under_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            other = (Path(tmp) / "other-project").resolve()
            other.mkdir()
            args = self._open_here_args(repo)
            panes = [
                {"id": "%1", "location": "my-project:0.0", "command": "zsh", "label": "", "cwd": str(repo)},
                {"id": "%2", "location": "my-project:0.1", "command": "zsh", "label": "", "cwd": str(other)},
            ]
            captured: dict[str, argparse.Namespace] = {}

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.cmd_open",
                      side_effect=lambda inner: (captured.__setitem__("args", inner), 0)[1]):
                self.assertEqual(0, cmd_open_here(args))

        self.assertEqual("my-project", captured["args"].session)

    def test_ensure_repo_session_windows_creates_session_and_codex_window(self) -> None:
        args = argparse.Namespace(
            session="my-project",
            cwd="/repo",
            config=True,
            config_path="/repo/.tmux.conf",
            config_path_was_default=False,
            ready_timeout=0,
            force=False,
        )
        claude_pane = {"id": "%1", "label": "claude", "command": "claude"}
        codex_pane = {"id": "%2", "label": "codex", "command": "node"}

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", side_effect=[False, False]), \
            patch("mozyo_bridge.application.commands.new_agent_session_window", return_value="%1") as new_session_window, \
            patch("mozyo_bridge.application.commands.source_tmux_conf") as source_conf, \
            patch("mozyo_bridge.application.commands.detect_legacy_pane_split", return_value=[]), \
            patch("mozyo_bridge.application.commands.list_session_windows", return_value=["claude"]), \
            patch("mozyo_bridge.application.commands.find_labeled_pane", side_effect=[claude_pane, codex_pane]), \
            patch("mozyo_bridge.application.commands.new_agent_window", return_value="%2") as new_window, \
            patch("mozyo_bridge.application.commands.ensure_agent_target"):
            created = ensure_repo_session_windows(args)

        new_session_window.assert_called_once_with("claude", "my-project", cwd="/repo")
        new_window.assert_called_once_with("codex", "my-project", cwd="/repo")
        source_conf.assert_called_once_with("/repo/.tmux.conf", optional=False)
        self.assertEqual(["claude:%1", "codex:%2"], created)

    def test_ensure_repo_session_windows_skips_creation_when_windows_exist(self) -> None:
        args = argparse.Namespace(
            session="my-project",
            cwd="/repo",
            config=False,
            ready_timeout=0,
            force=False,
        )
        claude_pane = {"id": "%1", "label": "claude", "command": "claude"}
        codex_pane = {"id": "%2", "label": "codex", "command": "node"}

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
            patch("mozyo_bridge.application.commands.new_agent_session_window") as new_session_window, \
            patch("mozyo_bridge.application.commands.new_agent_window") as new_window, \
            patch("mozyo_bridge.application.commands.detect_legacy_pane_split", return_value=[]), \
            patch("mozyo_bridge.application.commands.list_session_windows", return_value=["claude", "codex"]), \
            patch("mozyo_bridge.application.commands.find_labeled_pane",
                  side_effect=[claude_pane, codex_pane]), \
            patch("mozyo_bridge.application.commands.ensure_agent_target"):
            created = ensure_repo_session_windows(args)

        new_session_window.assert_not_called()
        new_window.assert_not_called()
        self.assertEqual([], created)

    def test_ensure_repo_session_windows_rejects_legacy_pane_split_layout(self) -> None:
        args = argparse.Namespace(
            session="my-project",
            cwd="/repo",
            config=False,
            ready_timeout=0,
            force=False,
        )

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
            patch("mozyo_bridge.application.commands.new_agent_session_window") as new_session_window, \
            patch("mozyo_bridge.application.commands.new_agent_window") as new_window, \
            patch(
                "mozyo_bridge.application.commands.detect_legacy_pane_split",
                return_value=["window=agents pane=%9 label=claude", "window=agents pane=%10 label=codex"],
            ), \
            patch("mozyo_bridge.application.commands.list_session_windows", return_value=["agents"]), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                ensure_repo_session_windows(args)

        new_session_window.assert_not_called()
        new_window.assert_not_called()
        message = stderr.getvalue()
        self.assertIn("legacy pane-split layout", message)
        self.assertIn("tmux kill-session -t my-project", message)
        self.assertIn("mozyo-bridge open-here", message)

    def test_detect_legacy_pane_split_flags_labels_in_unnamed_windows(self) -> None:
        from mozyo_bridge.application.commands import detect_legacy_pane_split

        list_panes_output = "\n".join(
            [
                "0\tagents\t%9\tclaude",
                "0\tagents\t%10\tcodex",
                "1\tlogs\t%11\t",
            ]
        )
        result = argparse.Namespace(returncode=0, stdout=list_panes_output, stderr="")

        with patch("mozyo_bridge.application.commands.run_tmux", return_value=result):
            conflicts = detect_legacy_pane_split("my-project")

        self.assertEqual(2, len(conflicts))
        self.assertTrue(any("label=claude" in c for c in conflicts))
        self.assertTrue(any("label=codex" in c for c in conflicts))

    def test_detect_legacy_pane_split_is_silent_when_labels_match_window_names(self) -> None:
        from mozyo_bridge.application.commands import detect_legacy_pane_split

        list_panes_output = "0\tclaude\t%1\tclaude\n1\tcodex\t%2\tcodex\n"
        result = argparse.Namespace(returncode=0, stdout=list_panes_output, stderr="")

        with patch("mozyo_bridge.application.commands.run_tmux", return_value=result):
            self.assertEqual([], detect_legacy_pane_split("my-project"))

    def test_cmd_mozyo_attaches_after_ensuring_repo_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            args = argparse.Namespace(
                repo=str(repo),
                session=None,
                cwd=None,
                config_path=None,
                ready_timeout=0,
                force=False,
                no_attach=False,
            )
            captured: dict[str, argparse.Namespace] = {}

            def fake_ensure(inner: argparse.Namespace) -> list[str]:
                captured["args"] = inner
                return ["claude:%1", "codex:%2"]

            list_result = argparse.Namespace(returncode=0, stdout="0\tclaude\tclaude\n1\tcodex\tnode\n", stderr="")

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows", side_effect=fake_ensure), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.commands.os.execvp", side_effect=RuntimeError("attached")), \
                contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "attached"):
                    cmd_mozyo(args)

        self.assertEqual("my-project", captured["args"].session)
        self.assertEqual(str(repo), captured["args"].cwd)
        self.assertTrue(captured["args"].config)
        self.assertTrue(captured["args"].config_path_was_default)

    def test_cmd_mozyo_no_attach_skips_execvp_and_prints_attach_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            args = argparse.Namespace(
                repo=str(repo),
                session=None,
                cwd=None,
                config_path=None,
                ready_timeout=0,
                force=False,
                no_attach=True,
            )
            list_result = argparse.Namespace(returncode=0, stdout="", stderr="")

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows", return_value=[]), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.commands.os.execvp", side_effect=AssertionError("must not attach")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_mozyo(args))

        self.assertIn("attach: tmux attach -t my-project", stdout.getvalue())

    def test_cmd_mozyo_dies_when_claude_window_select_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            args = argparse.Namespace(
                repo=str(repo),
                session=None,
                cwd=None,
                config_path=None,
                ready_timeout=0,
                force=False,
                no_attach=False,
            )
            select_failure = argparse.Namespace(returncode=1, stdout="", stderr="can't find window: claude")

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows", return_value=[]), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=select_failure), \
                patch("mozyo_bridge.application.commands.os.execvp") as execvp, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    cmd_mozyo(args)

        execvp.assert_not_called()
        self.assertIn("window-model guarantee", stderr.getvalue())
        self.assertIn("claude", stderr.getvalue())

    def test_cmd_mozyo_refuses_when_existing_session_panes_are_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            other = (Path(tmp) / "other-project").resolve()
            other.mkdir()
            args = argparse.Namespace(
                repo=str(repo),
                session=None,
                cwd=None,
                config_path=None,
                ready_timeout=0,
                force=False,
                no_attach=False,
            )
            panes = [
                {"id": "%1", "location": "my-project:0.0", "command": "zsh", "label": "", "cwd": str(other)},
            ]

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows") as ensure, \
                patch("mozyo_bridge.application.commands.os.execvp") as execvp, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    cmd_mozyo(args)

        ensure.assert_not_called()
        execvp.assert_not_called()
        self.assertIn("my-project", stderr.getvalue())
        self.assertIn("outside repo root", stderr.getvalue())

    def test_open_here_ignores_unrelated_session_panes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            other = (Path(tmp) / "other-project").resolve()
            other.mkdir()
            args = self._open_here_args(repo)
            panes = [
                {"id": "%9", "location": "other:0.0", "command": "zsh", "label": "", "cwd": str(other)},
            ]
            captured: dict[str, argparse.Namespace] = {}

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.cmd_open",
                      side_effect=lambda inner: (captured.__setitem__("args", inner), 0)[1]):
                self.assertEqual(0, cmd_open_here(args))

        self.assertEqual("my-project", captured["args"].session)

    def test_load_tmux_conf_skips_silently_when_default_path_missing(self) -> None:
        args = argparse.Namespace(
            config_path="/nonexistent/.tmux.conf",
            config_path_was_default=True,
            repo=None,
        )

        with patch("mozyo_bridge.application.commands.source_tmux_conf", wraps=tmux_client.source_tmux_conf) as wrapped, \
            patch("mozyo_bridge.infrastructure.tmux_client.run_tmux") as run:
            self.assertFalse(load_tmux_conf_for(args))

        wrapped.assert_called_once_with("/nonexistent/.tmux.conf", optional=True)
        run.assert_not_called()

    def test_load_tmux_conf_errors_when_explicit_config_path_missing(self) -> None:
        args = argparse.Namespace(
            config_path="/nonexistent/.tmux.conf",
            config_path_was_default=False,
            repo=None,
        )

        with patch("mozyo_bridge.application.commands.source_tmux_conf", wraps=tmux_client.source_tmux_conf), \
            patch("mozyo_bridge.infrastructure.tmux_client.run_tmux") as run, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                load_tmux_conf_for(args)

        run.assert_not_called()
        self.assertIn("tmux config not found", stderr.getvalue())

    def test_load_tmux_conf_sources_when_default_path_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / ".tmux.conf"
            conf.write_text("# placeholder", encoding="utf-8")
            args = argparse.Namespace(
                config_path=str(conf),
                config_path_was_default=True,
                repo=None,
            )
            ok = argparse.Namespace(returncode=0, stdout="", stderr="")
            with patch("mozyo_bridge.infrastructure.tmux_client.run_tmux", return_value=ok) as run:
                self.assertTrue(load_tmux_conf_for(args))

        run.assert_called_once_with("source-file", str(conf), check=False)

    def test_cmd_open_propagates_config_path_was_default_to_ensure_pair(self) -> None:
        args = argparse.Namespace(
            session="agents",
            cwd="/repo",
            vertical=False,
            config=True,
            config_path="/repo/.tmux.conf",
            config_path_was_default=True,
            ready_timeout=0,
            force=False,
        )
        captured: dict[str, argparse.Namespace] = {}

        def fake_ensure_pair(inner: argparse.Namespace) -> int:
            captured["args"] = inner
            return 0

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
            patch("mozyo_bridge.application.commands.cmd_ensure_pair", side_effect=fake_ensure_pair), \
            patch("mozyo_bridge.application.commands.os.execvp", side_effect=RuntimeError("attached")):
            with self.assertRaisesRegex(RuntimeError, "attached"):
                cmd_open(args)

        self.assertTrue(captured["args"].config_path_was_default)

    def test_session_cwd_mismatch_returns_empty_when_no_panes_in_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            panes = [{"id": "%1", "location": "elsewhere:0.0", "command": "zsh", "label": "", "cwd": "/tmp"}]
            with patch("mozyo_bridge.application.commands.pane_lines", return_value=panes):
                self.assertEqual([], session_cwd_mismatch("my-project", repo))

    def test_doctor_warns_when_claude_pane_cwd_is_outside_repo_with_project_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".claude" / "skills").mkdir(parents=True)
            args = argparse.Namespace(repo=str(repo), queue=str(repo / ".agent_handoff" / "tasks.json"))
            panes = [
                {
                    "id": "%1",
                    "location": "agents:0.0",
                    "command": "claude",
                    "label": "claude",
                    "cwd": str(Path(tmp) / "outside"),
                },
                {"id": "%2", "location": "agents:0.1", "command": "node", "label": "codex", "cwd": str(repo)},
            ]
            list_result = argparse.Namespace(returncode=0, stdout="%1 claude\n%2 codex\n", stderr="")

            ok_stub = {"status": "ok", "next_action": []}

            with patch("mozyo_bridge.application.doctor.subprocess.run", return_value=argparse.Namespace(returncode=0)), \
                patch("mozyo_bridge.application.doctor.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.doctor.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.doctor._in_tmux", return_value=True), \
                patch.dict(os.environ, {"TMUX_PANE": "%1"}), \
                patch("mozyo_bridge.application.doctor.doctor_cli_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_rules_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_codex_skill_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_claude_skill_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_scaffold_section", return_value=ok_stub), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(1, cmd_doctor(args))

        output = stdout.getvalue()
        self.assertIn("warning: claude_pane cwd is outside repo root; project skills may not resolve", output)
        self.assertIn(f"repo={repo.resolve()}", output)

    def test_doctor_accepts_versioned_claude_native_binary_as_agent_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            args = argparse.Namespace(repo=str(repo), queue=str(repo / ".agent_handoff" / "tasks.json"))
            panes = [
                {"id": "%1", "location": "agents:0.0", "command": "2.1.138", "label": "claude", "cwd": str(repo)},
                {"id": "%2", "location": "agents:0.1", "command": "node", "label": "codex", "cwd": str(repo)},
            ]
            list_result = argparse.Namespace(returncode=0, stdout="%1 claude\n%2 codex\n", stderr="")

            ok_stub = {"status": "ok", "next_action": []}

            with patch("mozyo_bridge.application.doctor.subprocess.run", return_value=argparse.Namespace(returncode=0)), \
                patch("mozyo_bridge.application.doctor.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.doctor.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.doctor._in_tmux", return_value=True), \
                patch.dict(os.environ, {"TMUX_PANE": "%1"}), \
                patch("mozyo_bridge.application.doctor.doctor_cli_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_rules_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_codex_skill_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_claude_skill_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_scaffold_section", return_value=ok_stub), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_doctor(args))

        self.assertIn("claude_pane: %1 process=2.1.138 status=ok", stdout.getvalue())

    def test_doctor_does_not_flag_cross_session_labeled_panes_as_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            args = argparse.Namespace(repo=str(repo), queue=str(repo / ".agent_handoff" / "tasks.json"))
            panes = [
                {"id": "%1", "location": "mozyo_bridge:0.0", "command": "2.1.138", "label": "claude", "cwd": str(repo)},
                {"id": "%2", "location": "mozyo_bridge:0.1", "command": "node", "label": "codex", "cwd": str(repo)},
                {"id": "%3", "location": "other_project:0.0", "command": "2.1.138", "label": "claude", "cwd": str(repo)},
                {"id": "%4", "location": "other_project:0.1", "command": "node", "label": "codex", "cwd": str(repo)},
            ]
            list_result = argparse.Namespace(
                returncode=0,
                stdout="%1 claude\n%2 codex\n%3 claude\n%4 codex\n",
                stderr="",
            )
            ok_stub = {"status": "ok", "next_action": []}

            with patch("mozyo_bridge.application.doctor.subprocess.run", return_value=argparse.Namespace(returncode=0)), \
                patch("mozyo_bridge.application.doctor.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.doctor.pane_lines", return_value=panes), \
                patch.dict(os.environ, {"TMUX_PANE": "%1"}), \
                patch("mozyo_bridge.application.doctor.doctor_cli_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_rules_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_codex_skill_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_claude_skill_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_scaffold_section", return_value=ok_stub), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_doctor(args))

        output = stdout.getvalue()
        self.assertIn("claude_pane: %1 process=2.1.138 status=ok", output)
        self.assertIn("codex_pane: %2 process=node status=ok", output)
        self.assertIn("other_sessions=1", output)
        self.assertNotIn("duplicate", output)

    def test_doctor_flags_within_session_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            args = argparse.Namespace(repo=str(repo), queue=str(repo / ".agent_handoff" / "tasks.json"))
            panes = [
                {"id": "%1", "location": "agents:0.0", "command": "zsh", "label": "claude", "cwd": str(repo)},
                {"id": "%2", "location": "agents:0.1", "command": "2.1.138", "label": "claude", "cwd": str(repo)},
                {"id": "%3", "location": "agents:0.2", "command": "node", "label": "codex", "cwd": str(repo)},
            ]
            list_result = argparse.Namespace(returncode=0, stdout="%1 claude\n%2 claude\n%3 codex\n", stderr="")
            ok_stub = {"status": "ok", "next_action": []}

            with patch("mozyo_bridge.application.doctor.subprocess.run", return_value=argparse.Namespace(returncode=0)), \
                patch("mozyo_bridge.application.doctor.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.doctor.pane_lines", return_value=panes), \
                patch.dict(os.environ, {"TMUX_PANE": "%2"}), \
                patch("mozyo_bridge.application.doctor.doctor_cli_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_rules_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_codex_skill_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_claude_skill_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_scaffold_section", return_value=ok_stub), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(1, cmd_doctor(args))

        output = stdout.getvalue()
        self.assertIn("claude_pane: duplicate (2) session=agents panes=%1,%2", output)
        self.assertIn("codex_pane: %3 process=node status=ok", output)

    def test_doctor_reports_unscoped_when_invoked_outside_tmux_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            args = argparse.Namespace(repo=str(repo), queue=str(repo / ".agent_handoff" / "tasks.json"))
            panes = [
                {"id": "%1", "location": "mozyo_bridge:0.0", "command": "2.1.138", "label": "claude", "cwd": str(repo)},
                {"id": "%2", "location": "mozyo_bridge:0.1", "command": "node", "label": "codex", "cwd": str(repo)},
            ]
            list_result = argparse.Namespace(returncode=0, stdout="%1 claude\n%2 codex\n", stderr="")
            ok_stub = {"status": "ok", "next_action": []}

            env_without_tmux_pane = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
            with patch("mozyo_bridge.application.doctor.subprocess.run", return_value=argparse.Namespace(returncode=0)), \
                patch("mozyo_bridge.application.doctor.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.doctor.pane_lines", return_value=panes), \
                patch.dict(os.environ, env_without_tmux_pane, clear=True), \
                patch("mozyo_bridge.application.doctor.doctor_cli_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_rules_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_codex_skill_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_claude_skill_section", return_value=ok_stub), \
                patch("mozyo_bridge.application.doctor.doctor_scaffold_section", return_value=ok_stub), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_doctor(args))

        output = stdout.getvalue()
        self.assertIn("claude_pane: unscoped (1) panes=%1", output)
        self.assertIn("codex_pane: unscoped (1) panes=%2", output)
        self.assertNotIn("duplicate", output)

    def _init_run_tmux_side_effect(self, target_pane_id: str):
        # display-message resolves any pane reference to the canonical pane id;
        # set-option (used to clear siblings) is a no-op for the test.
        def side_effect(*tmux_args, **_):
            if tmux_args[:1] == ("display-message",):
                return argparse.Namespace(returncode=0, stdout=f"{target_pane_id}\n", stderr="")
            if tmux_args[:1] == ("set-option",):
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux args: {tmux_args}")
        return side_effect

    def test_cmd_init_refuses_within_session_collision_without_force(self) -> None:
        args = argparse.Namespace(agent="claude", target="%5", force=False)
        panes = [
            {"id": "%2", "location": "agents:0.0", "command": "2.1.138", "label": "claude", "cwd": "/repo"},
            {"id": "%5", "location": "agents:0.2", "command": "zsh", "label": "", "cwd": "/repo"},
        ]
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=self._init_run_tmux_side_effect("%5")), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.2"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            patch("mozyo_bridge.application.commands.set_pane_label") as set_label, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                cmd_init(args)

        self.assertIn("already labeled 'claude'", stderr.getvalue())
        self.assertIn("%2", stderr.getvalue())
        set_label.assert_not_called()

    def test_cmd_init_with_force_clears_sibling_label_and_relabels_target(self) -> None:
        args = argparse.Namespace(agent="claude", target="%5", force=True)
        panes = [
            {"id": "%2", "location": "agents:0.0", "command": "2.1.138", "label": "claude", "cwd": "/repo"},
            {"id": "%5", "location": "agents:0.2", "command": "zsh", "label": "", "cwd": "/repo"},
        ]
        clear_calls: list[tuple] = []

        def run_tmux_side_effect(*tmux_args, **_):
            if tmux_args[:1] == ("display-message",):
                return argparse.Namespace(returncode=0, stdout="%5\n", stderr="")
            if tmux_args[:1] == ("set-option",):
                clear_calls.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux args: {tmux_args}")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=run_tmux_side_effect), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.2"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            patch("mozyo_bridge.application.commands.set_pane_label") as set_label, \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, cmd_init(args))

        self.assertEqual(1, len(clear_calls), clear_calls)
        clear_args = clear_calls[0]
        self.assertEqual("set-option", clear_args[0])
        self.assertIn("%2", clear_args)
        self.assertIn("@agent_name", clear_args)
        self.assertIn("", clear_args)
        set_label.assert_called_once_with("%5", "claude")
        self.assertIn("cleared=%2", stdout.getvalue())

    def test_cmd_init_allows_cross_session_label(self) -> None:
        args = argparse.Namespace(agent="claude", target="%5", force=False)
        panes = [
            {"id": "%2", "location": "other:0.0", "command": "2.1.138", "label": "claude", "cwd": "/repo"},
            {"id": "%5", "location": "agents:0.2", "command": "zsh", "label": "", "cwd": "/repo"},
        ]
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=self._init_run_tmux_side_effect("%5")), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.2"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            patch("mozyo_bridge.application.commands.set_pane_label") as set_label, \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd_init(args))

        set_label.assert_called_once_with("%5", "claude")

    def test_cmd_init_rejects_label_as_target(self) -> None:
        args = argparse.Namespace(agent="claude", target="claude", force=False)
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                cmd_init(args)
        self.assertIn("not a label", stderr.getvalue())


class DoctorEnvironmentTest(unittest.TestCase):
    """Diagnostic sections for `mozyo-bridge doctor` (cli/rules/skills/scaffold)."""

    def _stub_args(self, **kwargs) -> argparse.Namespace:
        defaults = {"repo": None, "home": None, "json": False}
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def _seed_codex_skill(self, codex_home: Path, *, complete: bool = True) -> Path:
        skill_dir = codex_home / "skills" / "mozyo-bridge-agent"
        (skill_dir / "references").mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("stub\n", encoding="utf-8")
        if complete:
            for ref in ("workflow.md", "safety.md", "project-map.md", "release.md"):
                (skill_dir / "references" / ref).write_text("stub\n", encoding="utf-8")
        return skill_dir

    def _seed_claude_global_skill(self, claude_home: Path, *, complete: bool = True) -> Path:
        skill_dir = claude_home / "skills" / "mozyo-bridge-agent"
        (skill_dir / "references").mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("stub\n", encoding="utf-8")
        if complete:
            for ref in ("workflow.md", "safety.md", "project-map.md", "release.md"):
                (skill_dir / "references" / ref).write_text("stub\n", encoding="utf-8")
        return skill_dir

    def _seed_claude_project_skill(self, project: Path) -> Path:
        skill_dir = project / ".claude" / "skills" / "mozyo-bridge-agent"
        (skill_dir / "references").mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("stub\n", encoding="utf-8")
        for ref in ("workflow.md", "safety.md", "project-map.md", "release.md"):
            (skill_dir / "references" / ref).write_text("stub\n", encoding="utf-8")
        return skill_dir

    def _seed_claude_plugin_skill(
        self, claude_home: Path, *, version: str = "abc12345"
    ) -> Path:
        plugin_dir = (
            claude_home
            / "plugins"
            / "cache"
            / "mozyo-bridge"
            / "mozyo-bridge-agent"
            / version
        )
        skill_dir = plugin_dir / "skills" / "mozyo-bridge-agent"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("plugin stub\n", encoding="utf-8")
        return plugin_dir

    def test_cli_section_reports_version_and_subcommands(self) -> None:
        from mozyo_bridge.application.doctor import doctor_cli_section

        section = doctor_cli_section()
        self.assertEqual("ok", section["status"])
        self.assertEqual(__version__, section["version"])
        for cmd in ("doctor", "rules", "scaffold"):
            self.assertIn(cmd, section["subcommands"])
        self.assertTrue(section["package_path"].endswith("mozyo_bridge"))

    def test_rules_section_reports_missing_when_not_installed(self) -> None:
        from mozyo_bridge.application.doctor import doctor_rules_section

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "mb-home"
            section = doctor_rules_section(home)
            self.assertEqual("missing-or-outdated", section["status"])
            statuses = {row["preset"]: row["status"] for row in section["presets"]}
            self.assertEqual({"asana": "missing", "redmine": "missing", "none": "missing"}, statuses)
            # Custom home was diagnosed, so next_action must target the same home.
            self.assertEqual(
                [f"mozyo-bridge rules install --home {home}"], section["next_action"]
            )

    def test_rules_section_reports_ok_when_installed(self) -> None:
        from mozyo_bridge.application.doctor import doctor_rules_section
        from mozyo_bridge.scaffold.rules import install_rules

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "mb-home"
            install_rules(home)
            section = doctor_rules_section(home)
            self.assertEqual("ok", section["status"])
            for row in section["presets"]:
                self.assertEqual("ok", row["status"])
            self.assertEqual([], section["next_action"])

    def test_codex_skill_section_reports_missing_with_install_hint(self) -> None:
        from mozyo_bridge.application.doctor import doctor_codex_skill_section

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"CODEX_HOME": str(Path(tmp) / "codex-home")}, clear=False):
                section = doctor_codex_skill_section()
                self.assertEqual("missing", section["status"])
                self.assertFalse(section["present"])
                self.assertTrue(section["next_action"])
                self.assertTrue(
                    any("install_codex_skill.sh" in action for action in section["next_action"])
                )

    def test_codex_skill_section_reports_ok_when_skill_present(self) -> None:
        from mozyo_bridge.application.doctor import doctor_codex_skill_section

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            self._seed_codex_skill(codex_home, complete=True)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                section = doctor_codex_skill_section()
                self.assertEqual("ok", section["status"])
                self.assertTrue(section["present"])
                self.assertEqual([], section["references_missing"])

    def test_codex_skill_section_flags_incomplete_when_references_missing(self) -> None:
        from mozyo_bridge.application.doctor import doctor_codex_skill_section

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            self._seed_codex_skill(codex_home, complete=False)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                section = doctor_codex_skill_section()
                self.assertEqual("incomplete", section["status"])
                self.assertTrue(section["references_missing"])

    def test_claude_skill_section_reports_missing_when_neither_present(self) -> None:
        from mozyo_bridge.application.doctor import doctor_claude_skill_section

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            env = {
                "MOZYO_BRIDGE_CLAUDE_HOME": str(Path(tmp) / "claude-home"),
                "MOZYO_BRIDGE_CLAUDE_PROJECT_DIR": str(project),
            }
            with patch.dict(os.environ, env, clear=False):
                args = self._stub_args(repo=str(project))
                section = doctor_claude_skill_section(args)
                self.assertEqual("missing", section["status"])
                self.assertFalse(section["global"]["present"])
                self.assertFalse(section["project"]["present"])
                self.assertEqual([], section["warnings"])
                self.assertTrue(section["next_action"])

    def test_claude_skill_section_warns_when_global_and_project_collide(self) -> None:
        from mozyo_bridge.application.doctor import doctor_claude_skill_section

        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / "claude-home"
            project = Path(tmp) / "project"
            project.mkdir()
            self._seed_claude_global_skill(claude_home, complete=True)
            self._seed_claude_project_skill(project)
            env = {
                "MOZYO_BRIDGE_CLAUDE_HOME": str(claude_home),
                "MOZYO_BRIDGE_CLAUDE_PROJECT_DIR": str(project),
            }
            with patch.dict(os.environ, env, clear=False):
                args = self._stub_args(repo=str(project))
                section = doctor_claude_skill_section(args)
                self.assertEqual("warning", section["status"])
                self.assertEqual(1, len(section["warnings"]))
                self.assertIn("overrides project skill", section["warnings"][0])

    def test_claude_skill_section_global_only_is_ok(self) -> None:
        from mozyo_bridge.application.doctor import doctor_claude_skill_section

        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / "claude-home"
            project = Path(tmp) / "project"
            project.mkdir()
            self._seed_claude_global_skill(claude_home, complete=True)
            env = {
                "MOZYO_BRIDGE_CLAUDE_HOME": str(claude_home),
                "MOZYO_BRIDGE_CLAUDE_PROJECT_DIR": str(project),
            }
            with patch.dict(os.environ, env, clear=False):
                args = self._stub_args(repo=str(project))
                section = doctor_claude_skill_section(args)
                self.assertEqual("ok", section["status"])
                self.assertTrue(section["global"]["present"])
                self.assertFalse(section["project"]["present"])
                self.assertEqual([], section["warnings"])

    def test_claude_skill_section_plugin_only_reports_plugin_managed(self) -> None:
        from mozyo_bridge.application.doctor import (
            CLAUDE_GLOBAL_SKILL_INSTALL_HINT,
            doctor_claude_skill_section,
        )

        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / "claude-home"
            project = Path(tmp) / "project"
            project.mkdir()
            self._seed_claude_plugin_skill(claude_home, version="abc12345")
            env = {
                "MOZYO_BRIDGE_CLAUDE_HOME": str(claude_home),
                "MOZYO_BRIDGE_CLAUDE_PROJECT_DIR": str(project),
            }
            with patch.dict(os.environ, env, clear=False):
                args = self._stub_args(repo=str(project))
                section = doctor_claude_skill_section(args)
            self.assertEqual("plugin-managed", section["status"])
            self.assertFalse(section["global"]["present"])
            self.assertFalse(section["project"]["present"])
            self.assertTrue(section["plugin"]["present"])
            self.assertEqual(
                "abc12345", section["plugin"]["versions"][0]["version"]
            )
            self.assertEqual([], section["next_action"])
            self.assertNotIn(
                CLAUDE_GLOBAL_SKILL_INSTALL_HINT, section["next_action"]
            )

    def test_claude_skill_section_plugin_with_legacy_global_keeps_ok(self) -> None:
        """When both plugin and legacy global skill are installed, plugin
        namespace is separate so status stays ok (existing precedence rules
        for warning only fire on legacy+legacy collision)."""
        from mozyo_bridge.application.doctor import doctor_claude_skill_section

        with tempfile.TemporaryDirectory() as tmp:
            claude_home = Path(tmp) / "claude-home"
            project = Path(tmp) / "project"
            project.mkdir()
            self._seed_claude_global_skill(claude_home, complete=True)
            self._seed_claude_plugin_skill(claude_home, version="abc12345")
            env = {
                "MOZYO_BRIDGE_CLAUDE_HOME": str(claude_home),
                "MOZYO_BRIDGE_CLAUDE_PROJECT_DIR": str(project),
            }
            with patch.dict(os.environ, env, clear=False):
                args = self._stub_args(repo=str(project))
                section = doctor_claude_skill_section(args)
            self.assertEqual("ok", section["status"])
            self.assertTrue(section["global"]["present"])
            self.assertTrue(section["plugin"]["present"])

    def test_run_doctor_reports_ok_for_plugin_only_state(self) -> None:
        """Top-level run_doctor must treat plugin-managed as healthy (overall
        result["ok"] == True) so the misleading legacy install hint does not
        flip the aggregate status."""
        from mozyo_bridge.application.doctor import run_doctor
        from mozyo_bridge.scaffold.rules import install_rules, write_scaffold

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "mb-home"
            claude_home = Path(tmp) / "claude-home"
            codex_home = Path(tmp) / "codex-home"
            target = Path(tmp) / "project"
            target.mkdir()
            install_rules(home)
            write_scaffold("asana", target, home=home)
            self._seed_codex_skill(codex_home, complete=True)
            self._seed_claude_plugin_skill(claude_home, version="abc12345")
            env = {
                "MOZYO_BRIDGE_HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "MOZYO_BRIDGE_CLAUDE_HOME": str(claude_home),
                "MOZYO_BRIDGE_CLAUDE_PROJECT_DIR": str(target),
            }
            with patch.dict(os.environ, env, clear=False), \
                patch(
                    "mozyo_bridge.application.doctor.doctor_tmux_section",
                    return_value={"status": "skipped", "next_action": []},
                ):
                args = self._stub_args(repo=str(target), home=str(home))
                result = run_doctor(args)
            self.assertTrue(result["ok"])
            self.assertEqual(
                "plugin-managed", result["sections"]["claude_skill"]["status"]
            )

    def test_scaffold_section_unscaffolded_suggests_scaffold_rules(self) -> None:
        from mozyo_bridge.application.doctor import doctor_scaffold_section

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            args = self._stub_args(repo=str(target), home=str(Path(tmp) / "mb-home"))
            section = doctor_scaffold_section(args)
            self.assertEqual("missing", section["status"])
            self.assertTrue(
                any("mozyo-bridge scaffold rules" in action for action in section["next_action"])
            )

    def test_scaffold_section_reports_ok_after_fresh_asana_scaffold(self) -> None:
        from mozyo_bridge.application.doctor import doctor_scaffold_section
        from mozyo_bridge.scaffold.rules import install_rules, write_scaffold

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "mb-home"
            target = Path(tmp) / "project"
            target.mkdir()
            install_rules(home)
            write_scaffold("asana", target, home=home)
            args = self._stub_args(repo=str(target), home=str(home))
            section = doctor_scaffold_section(args)
            self.assertEqual("ok", section["status"])
            self.assertEqual("asana", section["detail"].get("preset"))
            self.assertEqual([], section["next_action"])

    def test_scaffold_section_reports_ok_after_fresh_redmine_scaffold(self) -> None:
        from mozyo_bridge.application.doctor import doctor_scaffold_section
        from mozyo_bridge.scaffold.rules import install_rules, write_scaffold

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "mb-home"
            target = Path(tmp) / "project"
            target.mkdir()
            install_rules(home)
            write_scaffold("redmine", target, home=home)
            args = self._stub_args(repo=str(target), home=str(home))
            section = doctor_scaffold_section(args)
            self.assertEqual("ok", section["status"])
            self.assertEqual("redmine", section["detail"].get("preset"))

    def test_doctor_json_output_is_structured(self) -> None:
        from mozyo_bridge.application.doctor import run_doctor
        from mozyo_bridge.scaffold.rules import install_rules, write_scaffold

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "mb-home"
            claude_home = Path(tmp) / "claude-home"
            codex_home = Path(tmp) / "codex-home"
            target = Path(tmp) / "project"
            target.mkdir()
            install_rules(home)
            write_scaffold("asana", target, home=home)
            self._seed_codex_skill(codex_home, complete=True)
            self._seed_claude_global_skill(claude_home, complete=True)
            env = {
                "MOZYO_BRIDGE_HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "MOZYO_BRIDGE_CLAUDE_HOME": str(claude_home),
                "MOZYO_BRIDGE_CLAUDE_PROJECT_DIR": str(target),
            }
            with patch.dict(os.environ, env, clear=False), \
                patch("mozyo_bridge.application.doctor.doctor_tmux_section", return_value={"status": "skipped", "next_action": []}):
                args = self._stub_args(repo=str(target), home=str(home), json=True)
                result = run_doctor(args)
            self.assertTrue(result["ok"])
            self.assertIn("cli", result["sections"])
            self.assertIn("rules", result["sections"])
            self.assertIn("codex_skill", result["sections"])
            self.assertIn("claude_skill", result["sections"])
            self.assertIn("scaffold", result["sections"])
            self.assertIn("tmux", result["sections"])
            self.assertEqual("ok", result["sections"]["scaffold"]["status"])
            self.assertEqual("asana", result["sections"]["scaffold"]["detail"]["preset"])

            # Round-trip through json so we can assert the public payload is serializable.
            payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
            self.assertTrue(payload)

    def test_doctor_is_read_only_for_isolated_environment(self) -> None:
        """Doctor must not install, repair, or contact external systems."""
        from mozyo_bridge.application.doctor import run_doctor

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "mb-home"
            claude_home = Path(tmp) / "claude-home"
            codex_home = Path(tmp) / "codex-home"
            target = Path(tmp) / "project"
            target.mkdir()
            env = {
                "MOZYO_BRIDGE_HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "MOZYO_BRIDGE_CLAUDE_HOME": str(claude_home),
                "MOZYO_BRIDGE_CLAUDE_PROJECT_DIR": str(target),
            }
            with patch.dict(os.environ, env, clear=False), \
                patch("mozyo_bridge.application.doctor.doctor_tmux_section", return_value={"status": "skipped", "next_action": []}):
                args = self._stub_args(repo=str(target), home=str(home))
                run_doctor(args)
            # None of the homes should have been auto-created/populated by doctor.
            self.assertFalse((home / "rules").exists())
            self.assertFalse((claude_home / "skills").exists())
            self.assertFalse((codex_home / "skills").exists())

    def test_cli_parser_accepts_doctor_target_home_and_json_flags(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(
            ["doctor", "--target", "/tmp/a", "--home", "/tmp/b", "--json"]
        )
        self.assertEqual("doctor", ns.command)
        self.assertEqual("/tmp/a", ns.repo)
        self.assertEqual("/tmp/b", ns.home)
        self.assertTrue(ns.json)

    def test_claude_skill_install_hint_sets_scope_for_the_downstream_shell(self) -> None:
        """next_action must place `MOZYO_BRIDGE_CLAUDE_SCOPE=global` before `sh`,
        not before `curl`, so the env var actually reaches the install script."""
        from mozyo_bridge.application.doctor import (
            CLAUDE_GLOBAL_SKILL_INSTALL_HINT,
            doctor_claude_skill_section,
        )

        # The hint itself: env var must immediately precede the executor (`sh`),
        # not the pipeline producer (`curl`).
        self.assertIn("| MOZYO_BRIDGE_CLAUDE_SCOPE=global sh", CLAUDE_GLOBAL_SKILL_INSTALL_HINT)
        self.assertNotIn("MOZYO_BRIDGE_CLAUDE_SCOPE=global curl", CLAUDE_GLOBAL_SKILL_INSTALL_HINT)

        # And the section actually emits that hint when both global and project are absent.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            env = {
                "MOZYO_BRIDGE_CLAUDE_HOME": str(Path(tmp) / "claude-home"),
                "MOZYO_BRIDGE_CLAUDE_PROJECT_DIR": str(project),
            }
            with patch.dict(os.environ, env, clear=False):
                section = doctor_claude_skill_section(self._stub_args(repo=str(project)))
            self.assertEqual("missing", section["status"])
            self.assertTrue(
                any("| MOZYO_BRIDGE_CLAUDE_SCOPE=global sh" in action for action in section["next_action"]),
                f"expected pipe-then-env install hint, got {section['next_action']!r}",
            )

    def test_rules_next_action_propagates_custom_home(self) -> None:
        from mozyo_bridge.application.doctor import doctor_rules_section

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "mb-home"
            section = doctor_rules_section(home)
            self.assertEqual("missing-or-outdated", section["status"])
            self.assertEqual(
                [f"mozyo-bridge rules install --home {home}"], section["next_action"]
            )

    def test_rules_next_action_omits_home_flag_for_default_home(self) -> None:
        from mozyo_bridge.application.doctor import doctor_rules_section

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {"MOZYO_BRIDGE_HOME": str(Path(tmp) / "mb-home")},
                clear=False,
            ):
                section = doctor_rules_section(None)
            self.assertEqual("missing-or-outdated", section["status"])
            self.assertEqual(["mozyo-bridge rules install"], section["next_action"])

    def test_scaffold_next_action_propagates_custom_home(self) -> None:
        from mozyo_bridge.application.doctor import doctor_scaffold_section

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            home = Path(tmp) / "mb-home"
            args = self._stub_args(repo=str(target), home=str(home))
            section = doctor_scaffold_section(args)
            self.assertEqual("missing", section["status"])
            self.assertTrue(section["next_action"])
            action = section["next_action"][0]
            self.assertIn(f"--target {target.resolve()}", action)
            self.assertIn(f"--home {home.resolve()}", action)


class PluginMarketplaceTest(unittest.TestCase):
    """Guardrails for the Claude plugin marketplace packaging.

    The repo ships a `.claude-plugin/marketplace.json` at the root and a plugin
    at `plugins/mozyo-bridge-agent/`. The plugin bundles its own copy of the
    shared skill body so it works after Claude Code copies the plugin
    directory into its cache (plugin install cannot reach outside the plugin
    root). This test class enforces:

    1. Marketplace and plugin manifests load and carry the required fields.
    2. The plugin skill mirror at `plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/`
       stays in lockstep with the canonical `skills/mozyo-bridge-agent/`. Drift
       must be resolved by running `scripts/sync_plugin_skill.sh`, not by
       hand-editing the mirror.
    """

    def setUp(self) -> None:
        self.marketplace_path = ROOT / ".claude-plugin" / "marketplace.json"
        self.plugin_manifest_path = (
            ROOT / "plugins" / "mozyo-bridge-agent" / ".claude-plugin" / "plugin.json"
        )
        self.canonical_skill_dir = ROOT / "skills" / "mozyo-bridge-agent"
        self.plugin_skill_dir = (
            ROOT / "plugins" / "mozyo-bridge-agent" / "skills" / "mozyo-bridge-agent"
        )

    def test_marketplace_manifest_present_and_valid(self) -> None:
        self.assertTrue(
            self.marketplace_path.is_file(),
            f"expected marketplace manifest at {self.marketplace_path}",
        )
        data = json.loads(self.marketplace_path.read_text(encoding="utf-8"))
        # Required top-level fields per
        # https://code.claude.com/docs/en/plugin-marketplaces#marketplace-schema
        self.assertIn("name", data)
        self.assertIn("owner", data)
        self.assertIn("plugins", data)
        self.assertIsInstance(data["owner"], dict)
        self.assertIn("name", data["owner"], "owner.name is required")
        self.assertIsInstance(data["plugins"], list)
        self.assertEqual(
            "mozyo-bridge",
            data["name"],
            "marketplace name pins the install command `@mozyo-bridge` suffix",
        )
        # Marketplace name must not impersonate Anthropic-reserved names.
        reserved = {
            "claude-code-marketplace",
            "claude-code-plugins",
            "claude-plugins-official",
            "anthropic-marketplace",
            "anthropic-plugins",
            "agent-skills",
            "knowledge-work-plugins",
            "life-sciences",
        }
        self.assertNotIn(data["name"], reserved)

    def test_marketplace_lists_mozyo_bridge_agent_plugin(self) -> None:
        data = json.loads(self.marketplace_path.read_text(encoding="utf-8"))
        names = [entry.get("name") for entry in data["plugins"]]
        self.assertIn("mozyo-bridge-agent", names)
        entry = next(p for p in data["plugins"] if p.get("name") == "mozyo-bridge-agent")
        self.assertIn("source", entry)
        # Relative paths must start with "./" per plugin source rules.
        source = entry["source"]
        if isinstance(source, str):
            self.assertTrue(
                source.startswith("./"),
                "relative plugin source must start with './'",
            )
            # When metadata.pluginRoot is set, the source is resolved under it.
            plugin_root = data.get("metadata", {}).get("pluginRoot")
            if plugin_root:
                base = (
                    ROOT / plugin_root.lstrip("./").rstrip("/")
                    if plugin_root.startswith("./")
                    else ROOT / plugin_root
                )
                resolved = (base / source.lstrip("./").rstrip("/")).resolve()
            else:
                resolved = (ROOT / source.lstrip("./").rstrip("/")).resolve()
            self.assertTrue(
                resolved.is_dir(),
                f"plugin source path does not resolve to a directory: {resolved}",
            )

    def test_plugin_manifest_present_and_valid(self) -> None:
        self.assertTrue(
            self.plugin_manifest_path.is_file(),
            f"expected plugin manifest at {self.plugin_manifest_path}",
        )
        data = json.loads(self.plugin_manifest_path.read_text(encoding="utf-8"))
        # `name` is the only required plugin manifest field.
        self.assertIn("name", data)
        self.assertEqual("mozyo-bridge-agent", data["name"])

    def test_plugin_skill_mirror_matches_canonical(self) -> None:
        """The plugin's skill copy must be byte-identical to the canonical
        skill body. Run `scripts/sync_plugin_skill.sh` to regenerate the mirror
        whenever you edit `skills/mozyo-bridge-agent/`."""

        def relative_files(base: Path) -> dict[str, str]:
            mapping: dict[str, str] = {}
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(base).as_posix()
                mapping[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            return mapping

        self.assertTrue(
            self.canonical_skill_dir.is_dir(),
            f"canonical skill missing: {self.canonical_skill_dir}",
        )
        self.assertTrue(
            self.plugin_skill_dir.is_dir(),
            f"plugin skill mirror missing: {self.plugin_skill_dir}",
        )

        canonical = relative_files(self.canonical_skill_dir)
        mirror = relative_files(self.plugin_skill_dir)

        missing = sorted(set(canonical) - set(mirror))
        extra = sorted(set(mirror) - set(canonical))
        differing = sorted(
            rel for rel in canonical.keys() & mirror.keys() if canonical[rel] != mirror[rel]
        )

        hint = "run scripts/sync_plugin_skill.sh to regenerate the mirror"
        self.assertFalse(missing, f"plugin mirror missing files: {missing}; {hint}")
        self.assertFalse(extra, f"plugin mirror has unexpected files: {extra}; {hint}")
        self.assertFalse(
            differing, f"plugin mirror content differs from canonical: {differing}; {hint}"
        )

    def test_plugin_skill_mirror_has_skill_md(self) -> None:
        self.assertTrue(
            (self.plugin_skill_dir / "SKILL.md").is_file(),
            "plugin must ship SKILL.md so Claude Code can discover the skill after install",
        )


if __name__ == "__main__":
    unittest.main()
