from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
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
    AGENT_WINDOW_STATUS_COLORS,
    apply_window_subtle_style,
    cmd_init,
    cmd_list,
    cmd_mozyo,
    cmd_status,
    ensure_repo_session_windows,
    load_tmux_conf_for,
    notify_agent,
    resolve_status_session,
    session_cwd_mismatch,
)
from mozyo_bridge.infrastructure import tmux_client
from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.domain.notification import build_prompt, landing_marker, validate_notify_gate
from mozyo_bridge.domain.pane_resolver import (
    clear_read,
    ensure_agent_target,
    find_agent_window,
    is_agent_process,
    is_receiver_agent_process,
    is_tmux_target,
    mark_read,
    require_read,
    resolve_agent_label,
    resolve_target,
)
import mozyo_bridge.domain.pane_resolver as pane_resolver
from mozyo_bridge.domain.handoff import (
    AnchorError,
    AsanaAnchor,
    KIND_LABELS,
    LastInputProjection,
    MODE_PENDING,
    MODE_QUEUE_ENTER,
    MODE_STANDARD,
    RECORD_FORMAT_BOTH,
    RECORD_FORMAT_JSON,
    RECORD_FORMAT_TEXT,
    RedmineAnchor,
    build_delivery_record,
    build_marker,
    build_notification_body,
    make_outcome,
    next_action_for,
    normalize_anchor,
    project_last_input,
)
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

    def test_ensure_agent_target_accepts_node_for_codex_window(self) -> None:
        pane = {"command": "node"}

        ensure_agent_target(pane, "codex")

    def test_ensure_agent_target_accepts_versioned_native_binary_for_claude_window(self) -> None:
        pane = {"command": "2.1.138"}

        ensure_agent_target(pane, "claude")

    def test_is_agent_process_accepts_versioned_native_binary(self) -> None:
        self.assertTrue(is_agent_process("2.1.138"))

    def test_is_receiver_agent_process_strong_identity_for_claude(self) -> None:
        # Step 12 strong identity: only literal `claude` counts as strong
        # identity for receiver=claude. Literal `codex` is rejected outright.
        self.assertTrue(is_receiver_agent_process("claude", "claude"))
        self.assertTrue(is_receiver_agent_process("/usr/local/bin/claude", "claude"))
        self.assertFalse(is_receiver_agent_process("codex", "claude"))

    def test_is_receiver_agent_process_strong_identity_for_codex(self) -> None:
        # Step 12 strong identity, symmetric case: only literal `codex` counts
        # as strong identity for receiver=codex. Literal `claude` is rejected.
        self.assertTrue(is_receiver_agent_process("codex", "codex"))
        self.assertTrue(is_receiver_agent_process("/opt/codex/bin/codex", "codex"))
        self.assertFalse(is_receiver_agent_process("claude", "codex"))

    def test_is_receiver_agent_process_node_is_weak_identity_for_both(self) -> None:
        # Step 12 weak identity (contract Open Question 8): both Claude Code
        # and the Codex CLI are Node-based applications, so a `node`
        # foreground process can belong to either receiver. The function
        # admits `node` for both receivers but treats it as weak identity;
        # callers must not advertise it as strong receiver confirmation.
        self.assertTrue(is_receiver_agent_process("node", "claude"))
        self.assertTrue(is_receiver_agent_process("node", "codex"))
        self.assertTrue(is_receiver_agent_process("/usr/local/bin/node", "claude"))
        self.assertTrue(is_receiver_agent_process("/usr/local/bin/node", "codex"))

    def test_is_receiver_agent_process_versioned_native_is_weak_identity(self) -> None:
        # Step 12 weak identity: versioned native binary basenames are
        # receiver-agnostic by design — `1.0.32-arm64` passes for both
        # `claude` and `codex`. This is honest weakness, not a bug;
        # cross-binding protection retreats to Step 9 + Layer A here.
        self.assertTrue(is_receiver_agent_process("1.0.32-arm64", "claude"))
        self.assertTrue(is_receiver_agent_process("1.0.32-arm64", "codex"))
        self.assertTrue(is_receiver_agent_process("2.1.138", "claude"))
        self.assertTrue(is_receiver_agent_process("2.1.138", "codex"))

    def test_is_receiver_agent_process_rejects_shells_and_empty(self) -> None:
        # queue-enter is never admitted to shells or to a pane whose
        # foreground process tmux reports as empty.
        for receiver in ("claude", "codex"):
            for command in ("zsh", "bash", "fish", "sh", "vim", "less", ""):
                self.assertFalse(
                    is_receiver_agent_process(command, receiver),
                    msg=f"{command!r} must not satisfy receiver={receiver!r}",
                )

    def test_is_receiver_agent_process_rejects_unknown_receiver_strong_only(self) -> None:
        # CLI args already restrict `--to` to RECEIVERS, but the function
        # must not silently grant the *strong* identity branch to an unknown
        # receiver. Literal receiver basenames fail the strong check and
        # fall through to the weak branch, which is receiver-agnostic by
        # design — only weak-branch matches admit for an unknown receiver.
        self.assertFalse(is_receiver_agent_process("claude", "operator"))
        self.assertFalse(is_receiver_agent_process("codex", "operator"))
        # Weak-branch matches admit even for unknown receivers; this is
        # documented as receiver-agnostic and is not a receiver-identity
        # claim. Test for both `node` and versioned-native cases.
        self.assertTrue(is_receiver_agent_process("node", "operator"))
        self.assertTrue(is_receiver_agent_process("1.0.32-arm64", "operator"))

    def test_ensure_agent_target_rejects_shell_without_force(self) -> None:
        pane = {"command": "bash"}

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

    def test_find_agent_window_returns_pane_for_window_named_agent(self) -> None:
        panes = [
            {
                "id": "%1",
                "location": "repo:0.0",
                "window_name": "claude",
                "pane_active": "1",
            },
            {
                "id": "%2",
                "location": "repo:1.0",
                "window_name": "codex",
                "pane_active": "1",
            },
        ]

        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            window = find_agent_window("codex", "repo")

        self.assertIsNotNone(window)
        self.assertEqual("%2", window["id"])

    def test_find_agent_window_returns_none_when_no_window_named_agent(self) -> None:
        panes = [
            {
                "id": "%1",
                "location": "repo:0.0",
                "window_name": "agents",
                "pane_active": "1",
            },
        ]

        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            self.assertIsNone(find_agent_window("claude", "repo"))

    def test_find_agent_window_dies_on_duplicate_window_name(self) -> None:
        panes = [
            {
                "id": "%1",
                "location": "repo:0.0",
                "window_name": "claude",
                "pane_active": "1",
            },
            {
                "id": "%2",
                "location": "repo:1.0",
                "window_name": "claude",
                "pane_active": "1",
            },
        ]

        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    find_agent_window("claude", "repo")

    def test_find_agent_window_prefers_active_pane_in_split_window(self) -> None:
        panes = [
            {
                "id": "%1",
                "location": "repo:0.0",
                "window_name": "claude",
                "pane_active": "0",
            },
            {
                "id": "%2",
                "location": "repo:0.1",
                "window_name": "claude",
                "pane_active": "1",
            },
        ]

        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            window = find_agent_window("claude", "repo")

        self.assertEqual("%2", window["id"])

    def test_find_agent_window_ignores_other_sessions(self) -> None:
        panes = [
            {
                "id": "%1",
                "location": "elsewhere:0.0",
                "window_name": "claude",
                "pane_active": "1",
            },
        ]

        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            self.assertIsNone(find_agent_window("claude", "repo"))

    def test_resolve_agent_label_uses_window_name_only(self) -> None:
        # Single-rail: no `@agent_name` label fallback. A pane in a non-agent
        # window does not resolve under the new model even when the operator
        # set the legacy label on it.
        panes = [
            {
                "id": "%1",
                "location": "repo:0.0",
                "window_name": "agents",
                "pane_active": "1",
            },
        ]

        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            self.assertIsNone(resolve_agent_label("claude", "repo"))

    def test_resolve_agent_label_never_falls_back_cross_session(self) -> None:
        panes = [
            {
                "id": "%1",
                "location": "elsewhere:0.0",
                "window_name": "claude",
                "pane_active": "1",
            },
        ]

        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            self.assertIsNone(resolve_agent_label("claude", "repo"))

    def test_resolve_agent_label_returns_none_when_session_unknown(self) -> None:
        with patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[]):
            self.assertIsNone(resolve_agent_label("claude", None))

    def test_resolve_target_for_agent_label_dies_outside_tmux(self) -> None:
        with patch("mozyo_bridge.domain.pane_resolver.current_session_name", return_value=None), \
            contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                resolve_target("codex")

    def test_resolve_target_for_agent_label_dies_when_not_in_current_session(self) -> None:
        panes = [
            {
                "id": "%1",
                "location": "elsewhere:0.0",
                "window_name": "codex",
                "pane_active": "1",
            },
        ]

        with patch("mozyo_bridge.domain.pane_resolver.current_session_name", return_value="repo"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes), \
            contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                resolve_target("codex")

    def test_resolve_target_for_agent_label_returns_window_pane(self) -> None:
        panes = [
            {
                "id": "%9",
                "location": "repo:1.0",
                "window_name": "codex",
                "pane_active": "1",
            },
        ]

        with patch("mozyo_bridge.domain.pane_resolver.current_session_name", return_value="repo"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes):
            self.assertEqual("%9", resolve_target("codex"))

    def test_resolve_target_rejects_non_agent_string(self) -> None:
        # Custom strings used to fall through to the `@agent_name` label
        # lookup; under the window-only model they fail closed at resolve
        # time with a hint to pass a tmux pane id or an agent label.
        with patch(
            "mozyo_bridge.domain.pane_resolver.current_session_name",
            return_value="repo",
        ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                resolve_target("operator_pane")

        self.assertIn("operator_pane", stderr.getvalue())
        self.assertIn("claude", stderr.getvalue())


class CliTest(unittest.TestCase):
    def test_primary_commands_parse(self) -> None:
        parser = build_parser()

        self.assertEqual("status", parser.parse_args(["status"]).command)
        self.assertEqual("init", parser.parse_args(["init", "codex"]).command)
        self.assertEqual("notify-codex-review", parser.parse_args(["notify-codex-review", "--issue", "9020"]).command)
        self.assertEqual("rules", parser.parse_args(["rules", "install"]).command)
        self.assertEqual("scaffold", parser.parse_args(["scaffold", "apply", "asana"]).command)
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

    def test_legacy_pane_split_subcommands_are_rejected(self) -> None:
        parser = build_parser()
        for command in (
            "open-here",
            "tmux-ui-open",
            "tmux-ui-setup",
            "tmux-ui-ensure",
            "tmux-ui-ensure-pair",
            "tmux-ui-spawn",
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args([command])

    def test_legacy_task_notification_is_separate_command(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["notify-codex-legacy-task", "--issue", "9020", "--task-id", "legacy-task", "--type", "review_request"]
        )

        self.assertEqual("notify-codex-legacy-task", args.command)
        self.assertEqual("legacy-task", args.task_id)

    def test_status_session_defaults_to_none_for_runtime_resolution(self) -> None:
        # Hard-coding the default to "agents" was the root cause of the
        # misleading `session: agents (missing)` symptom under bare `mozyo`
        # (Asana task 1214758916882465). Bare `status` must auto-resolve the
        # session at runtime instead.
        parser = build_parser()

        args = parser.parse_args(["status"])

        self.assertEqual("status", args.command)
        self.assertIsNone(args.session)

    def test_status_accepts_explicit_session_override(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["status", "--session", "agents"])

        self.assertEqual("agents", args.session)

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

        args = parser.parse_args(["status", "--repo", "/repo"])

        self.assertEqual("status", args.command)
        self.assertEqual("/repo", args.repo)

    def test_bare_mozyo_accepts_session_override(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["--session", "alt"])

        self.assertIsNone(args.command)
        self.assertEqual("alt", args.session)

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

    def test_scaffold_apply_rejects_unknown_preset(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["scaffold", "apply", "jira"])

    def test_scaffold_diff_rejects_unknown_preset(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["scaffold", "diff", "jira"])

    def test_legacy_scaffold_subcommand_is_removed(self) -> None:
        """Breaking change: the legacy `rules` subcommand under `scaffold` is no longer parsable.

        The v0.3 scaffold redesign removed it entirely. There is no
        compatibility alias; the official entrypoint is
        `scaffold apply <preset>` for write and `scaffold diff <preset>` for
        preview. Asserting the parser rejects the argv pair locks the
        breaking change so a future revert cannot silently bring the alias
        back. The rejected argv is built from parts so this source file does
        not carry the old command name as contiguous prose.
        """
        parser = build_parser()
        legacy_subcommand = "ru" + "les"  # split to avoid the literal phrase in source

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["scaffold", legacy_subcommand, "asana"])


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
                # Chat-surface boundary added to reduce noisy chat output for
                # un-notified / pending-operator-action handoffs.
                "Chat surface boundary",
                "Chat output is a notification only",
                "Do not duplicate the comment body in chat",
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
            # The central preset is the scaffold for arbitrary downstream
            # projects; team-specific tools (Notion in this team's flow) must
            # not leak into the generated guidance.
            self.assertNotIn("Notion", installed_workflow)
            self.assertIn(
                "Do not ask the user directly when the task, project notes, or repository docs",
                installed_workflow,
            )
            self.assertIn(
                "Do not store credentials, tokens, personal data, or private internal URLs",
                installed_workflow,
            )

            result, output = self.run_cli(["scaffold", "apply", "asana", "--target", str(project), "--home", str(home)])

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
            self.assertIn("active な `Asana task / comment`", agents)
            self.assertIn("router に本文を複製しない", agents)
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
            self.assertIn("implementation done / implementation_done は completion ではない", claude)
            self.assertIn("Asana task / comment", claude)
            self.assertIn("受領方法", claude)
            # Chat surface stays thin: durable receive method lives in the task
            # comment, chat reports stay to a state + task-id pointer.
            self.assertIn("最小ポインタ", claude)
            self.assertIn("chat に貼り直さない", claude)
            # CLAUDE.md stays thin even with the Claude-specific reminder
            # block AND the Project-Local Additions marker block + boilerplate
            # (~9 lines) shipped from the router template.
            self.assertLess(len(claude.splitlines()), 50)
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
            self.assertEqual("2026.05.17.2", state["preset_version"])
            self.assertIn("AGENTS.md", state["files"])

            # The audit-owned commit policy belongs in the central preset only.
            # Root routers stay thin and must not duplicate the policy body.
            self.assertNotIn("Audit-Owned Commit Authority", agents)
            self.assertNotIn("Audit-Owned Commit Authority", claude)
            self.assertNotIn("Refs: Asana task", agents)
            self.assertNotIn("Refs: Asana task", claude)

            # Tool-specific router split: the rendered routers must not import
            # each other. CLAUDE.md previously imported AGENTS.md via the
            # Claude Code `@AGENTS.md` file-import directive; the split makes
            # each tool's entry standalone so the central preset path and the
            # active ticket anchor are reachable without touching the peer
            # file. This rendered-output assertion catches a future template
            # regression that template-level tests would miss if the import
            # ever leaked through substitution.
            self.assertNotIn("@AGENTS.md", claude)
            self.assertNotIn("@CLAUDE.md", agents)
            self.assertIn("tool-specific", agents)
            self.assertIn("tool-specific", claude)
            self.assertIn("import しない", agents)
            self.assertIn("import しない", claude)

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
                "Design Consultation Answer Gate",
                "Implementation Done Gate",
                "Review Request Gate",
                "Review Gate",
                "QA Verification Gate",
                "Production Verification Gate",
                "Close Gate",
                "Pane Notification",
                "Handoff Startup Decision",
                "Review Quality Hierarchy",
                "Test / QA Role Boundary",
                "Close Gate Checklist",
                "事実姿勢",
                "実装者 / 監査者境界",
                "判断の routing",
                "Scope Integrity",
                "Verification Discipline",
                "notify-codex-review",
                "mozyo-bridge init",
                "Receiver pane unavailable",
                "Notification fails",
                "`Implementation Done` は `完了` ではない",
                "Stop hook handoff wait",
                "迎合より事実を優先する",
                # Ticket-ID entrypoint runtime reflection.
                "Ticket-ID Entrypoint",
                "入力が Redmine issue id",
                "Redmine issue record",
                "journal id と gate 順序が監査 replay の鍵",
                # Audit-owned commit authority codified in this task.
                "Audit-Owned Commit Authority",
                "commit authority であって implementation authority ではない",
                "Refs: Redmine #<issue_id>",
                "Journal: <journal_id>",
                "Review Gate journal",
                "git diff --cached --stat",
                "git add -A",
                "Close Gate journal",
                # Chat-surface boundary added to reduce noisy chat output for
                # un-notified / pending-operator-action handoffs.
                "chat には issue / journal id",
                "durable 手順を chat に再掲しない",
                "durable 手順",
                # Field-tested Redmine review payload details.
                "Review Request Gate",
                "target commit / diff",
                "changed files",
                "期待する read / ack path",
                "[事実]",
                "[仮説]",
                "是正条件",
                "仕様・設計整合",
                "bug / spec misunderstanding / unnecessary work",
                "reproduction",
                "expected",
                "actual",
                "version consistency",
                "`catalog.yaml` / docs resolver / nagger file conventions tooling の標準化は別タスク",
                # Close Approval Separation codified in 2026.05.18.3: review
                # approval and owner close approval are distinct gates; the
                # reviewer (audit role / Codex equivalent) records a separate
                # journal asking the owner about close after review approval,
                # and the implementer must not close from review approval
                # alone.
                "Close Approval Separation",
                "owner close approval",
                "Review Gate approval を owner close approval と読み替えない",
                "Review Gate とは別 journal",
                "owner close approval が未取得のまま close してはならない",
                "同一 issue の **別 journal** を作成し、owner にクローズ可否を確認する",
                "Review Gate approval だけで issue を close へ進めない",
            ):
                self.assertIn(marker, installed)
            self.assertIn(
                "`Claude Code が常に実装、Codex が常に監査` のような固定 role split",
                installed,
            )
            self.assertNotIn("python3 vibes/tools/mozyo_bridge", installed)
            self.assertNotIn(".claude-nagger/file_conventions.yaml", installed)
            self.assertNotIn("resolve_audit_docs.py", installed)
            # The central preset is the scaffold for arbitrary downstream
            # projects; team-specific tools (Notion in this team's flow) must
            # not leak into the generated guidance.
            self.assertNotIn("Notion", installed)
            self.assertNotIn("vibes/docs/catalog.yaml", installed)
            self.assertNotIn("manual_spec", installed)
            self.assertNotIn("FeatureListDsl", installed)
            self.assertNotIn("/myapp/Source/rails", installed)
            self.assertNotIn("tmux-integrated", installed)
            self.assertNotIn("VS Code", installed)
            # Implementation Done Gate is a distinct durable gate, not a Progress Log.
            # The Factual Posture wording must not downgrade it.
            self.assertNotIn("self-verification is a Progress Log", installed)
            self.assertIn("review input であり completion ではない", installed)

            result, output = self.run_cli(
                ["scaffold", "apply", "redmine", "--target", str(project), "--home", str(home)]
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
            self.assertIn("active な `Redmine issue / journal`", agents)
            self.assertIn("durable state、handoff、review、verification、close 条件", agents)
            self.assertIn("router に本文を複製しない", agents)
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
            self.assertIn("implementation done / implementation_done は completion ではない", claude)
            self.assertIn("Redmine issue / journal", claude)
            self.assertIn("handoff startup decision", claude)
            # Chat surface stays thin: durable receive method lives in Redmine,
            # chat reports stay to a state + issue/journal-id pointer.
            self.assertIn("最小ポインタ", claude)
            self.assertIn("chat に貼り直さない", claude)
            # Router stays thin: well below the central preset's depth, even
            # with the Project-Local Additions marker block + boilerplate
            # (~9 lines) shipped from the router template.
            self.assertLess(len(claude.splitlines()), 50)
            self.assertNotIn("Redmine Gate Lifecycle", claude)
            self.assertNotIn("Implementer / Auditor Role Boundary", claude)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("central", state["mode"])
            self.assertEqual("redmine", state["preset"])
            self.assertIn("AGENTS.md", state["files"])
            self.assertEqual("2026.05.18.3", state["preset_version"])

            # The audit-owned commit policy belongs in the central preset only.
            # Root routers stay thin and must not duplicate the policy body.
            self.assertNotIn("Audit-Owned Commit Authority", agents)
            self.assertNotIn("Audit-Owned Commit Authority", claude)
            self.assertNotIn("Refs: Redmine #", agents)
            self.assertNotIn("Refs: Redmine #", claude)

    def test_rules_install_and_scaffold_redmine_rails_layered_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            result, _ = self.run_cli(["rules", "install", "--home", str(home)])
            self.assertEqual(0, result)
            rails_workflow = home / "rules" / "presets" / "redmine-rails" / "agent-workflow.md"
            self.assertTrue(rails_workflow.exists())
            installed = rails_workflow.read_text(encoding="utf-8")
            for marker in (
                "Redmine Rails Agent Workflow",
                "rules/presets/redmine/agent-workflow.md",
                "Rails Scope Posture",
                "Rails Design Consultation Triggers",
                "Rails Implementation Done Additions",
                "Rails Review Focus",
                "Data / migration safety",
                "Authorization / tenant boundary",
                "Hotwire / UI behavior",
                "Rails Verification Discipline",
                "Rails QA / Production Verification",
                # Project-Local Layer section added in 2026.05.18.3.
                # The scaffold preset must explicitly tell operators which
                # categories of project-local facts stay in the target repo
                # and must not be overwritten by `scaffold apply`.
                "Project-Local Layer",
                "Project-Local Layer Apply Discipline",
                "do not erase on scaffold apply",
                "App stack identity",
                "Rails extension conventions",
                "Read-only documentation areas",
                "Project-specific safety commands",
                "Project docs governance",
                "Local role-boundary overrides",
                "Project tooling and private convention",
                "scaffold diff redmine-rails",
                "--backup",
                # New marker-bounded preservation guidance + concrete
                # category sections added in 2026.05.18.4.
                "Project-Local Additions マーカー",
                "<!-- mozyo-bridge:project-local-additions:begin -->",
                "<!-- mozyo-bridge:project-local-additions:end -->",
                "Active-Doc Resolver Concept",
                "Dangerous DB / Test Command Category",
                "Presenter / YAML / Doc-Readonly Category",
                "Project Tooling / Local Skill / Role-Boundary Override Category",
            ):
                self.assertIn(marker, installed)
            # Regression rails: the scaffold preset must not import team-specific
            # paths or convention names, even when describing what stays in
            # project-local docs. Existing-repo examples are described in
            # generic terms only.
            self.assertNotIn("/myapp/Source/rails", installed)
            self.assertNotIn("vibes/docs/catalog.yaml", installed)
            self.assertNotIn(".claude-nagger/file_conventions.yaml", installed)
            self.assertNotIn("resolve_audit_docs.py", installed)
            self.assertNotIn("bin/recreate_db.sh", installed)
            self.assertNotIn("bin/sync-mozyo-bridge-skill", installed)
            self.assertNotIn("RAILS_ENV=test", installed)
            self.assertNotIn("app/presenters/", installed)

            result, _ = self.run_cli(
                ["scaffold", "apply", "redmine-rails", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(0, result)
            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-rails/agent-workflow.md",
                agents,
            )
            self.assertIn("active な `Redmine issue / journal と Rails project docs`", agents)
            self.assertIn("router に本文を複製しない", agents)
            self.assertNotIn("Data / migration safety", agents)
            self.assertNotIn("Rails Review Focus", claude)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("redmine-rails", state["preset"])
            self.assertEqual("2026.05.18.4", state["preset_version"])

    def test_rules_install_and_scaffold_redmine_governed_full_package(self) -> None:
        """The non-Rails governed preset ships the governance package.

        It must extend the generic Redmine workflow, not the Rails layer, and
        its catalog skeleton must stay framework-neutral.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            result, _ = self.run_cli(["rules", "install", "--home", str(home)])
            self.assertEqual(0, result)
            governed_workflow = (
                home / "rules" / "presets" / "redmine-governed" / "agent-workflow.md"
            )
            self.assertTrue(governed_workflow.exists())
            installed = governed_workflow.read_text(encoding="utf-8")

            for marker in (
                "Redmine Governed Agent Workflow",
                "rules/presets/redmine/agent-workflow.md",
                "Scaffolded Repo-Local Artifacts",
                ".mozyo-bridge/rules/llm_rule_authoring.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
                "mozyo-bridge docs validate",
                "mozyo-bridge docs resolve",
                "mozyo-bridge docs generate-file-conventions",
                "mozyo-bridge docs audit-impact",
                "Gate Schema",
                "Codex Direct Edit Gate",
                "codex_direct_edit",
                "Governed Mode Prohibitions",
            ):
                self.assertIn(marker, installed)

            for forbidden in (
                "rules/presets/redmine-rails/agent-workflow.md",
                "redmine-rails-governed",
                "bundle exec",
                "rspec",
                "rubocop",
                "brakeman",
                "db/migrate",
                "app/**/*.rb",
                "spec/**/*.rb",
                "fc-rails",
                "NIPT",
                "nihonidenshi",
            ):
                self.assertNotIn(forbidden, installed)

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            self.assertEqual(0, result)
            for expected_path in (
                ".mozyo-bridge/rules/llm_rule_authoring.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
                ".mozyo-bridge/tmux/agent-ui.conf",
                ".claude-nagger/config.yaml.example",
            ):
                self.assertTrue((project / expected_path).exists())

            catalog_example = (
                project / ".mozyo-bridge/docs/catalog.yaml.example"
            ).read_text(encoding="utf-8")
            self.assertIn("fc-implementation-source", catalog_example)
            self.assertIn("fc-tests", catalog_example)
            for forbidden in (
                "fc-rails",
                "Rails app",
                "app/**/*.rb",
                "db/migrate",
                "spec/**/*.rb",
            ):
                self.assertNotIn(forbidden, catalog_example)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("redmine-governed", state["preset"])
            tracked_files = set(state["files"].keys())
            for expected in (
                "AGENTS.md",
                "CLAUDE.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
                ".mozyo-bridge/tmux/agent-ui.conf",
                ".claude-nagger/config.yaml.example",
            ):
                self.assertIn(expected, tracked_files)

    def test_rules_install_and_scaffold_redmine_rails_governed_full_package(self) -> None:
        """The governed preset must ship a full guardrail package.

        The central preset must surface strong governance language —
        gate schema, Codex direct edit gate, docs catalog governance,
        LLM rule authoring — without leaking nihonidenshi-specific names,
        paths, or business-domain identifiers. `scaffold apply` must
        write the repo-local rules / catalog skeleton into the
        target repository so the package is usable out of the box.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            result, _ = self.run_cli(["rules", "install", "--home", str(home)])
            self.assertEqual(0, result)
            governed_workflow = (
                home / "rules" / "presets" / "redmine-rails-governed" / "agent-workflow.md"
            )
            self.assertTrue(governed_workflow.exists())
            installed = governed_workflow.read_text(encoding="utf-8")

            # Strong governance language must survive the de-domain pass.
            for marker in (
                "Redmine Rails Governed Agent Workflow",
                "rules/presets/redmine/agent-workflow.md",
                "rules/presets/redmine-rails/agent-workflow.md",
                "Scaffolded Repo-Local Artifacts",
                ".mozyo-bridge/rules/llm_rule_authoring.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
                # docs catalog tooling lives in the mozyo-bridge package now,
                # not vendor-copied into the target repo. The workflow doc
                # references it by CLI name.
                "mozyo-bridge docs validate",
                "mozyo-bridge docs resolve",
                "mozyo-bridge docs generate-file-conventions",
                "mozyo-bridge docs audit-impact",
                "Gate Schema",
                "Codex Direct Edit Gate",
                "codex_direct_edit",
                "allowed_paths",
                "follow_up_review",
                "Docs Catalog Governance",
                "Active-Doc Resolver",
                "LLM Rule Authoring",
                "Required Verification",
                "Close Approval Separation",
                "Governed Mode Prohibitions",
            ):
                self.assertIn(marker, installed)

            # Regression rails: nihonidenshi-specific business domain,
            # paths, and project identifiers must not leak into the
            # generalized preset.
            for forbidden in (
                "nihonidenshi",
                "idenshi_youbou",
                "jgmlife",
                "/myapp/Source/rails",
                "/myapp/Doc",
                "NIPT",
                "検査依頼",
                "検体",
                "帳票",
                "判定",
                "集荷",
                "_機能リスト.json",
                "FeatureList",
                "vibes/docs/tools",
                "bin/recreate_db.sh",
                "bin/sync-mozyo-bridge-skill",
            ):
                self.assertNotIn(forbidden, installed)

            # `scaffold apply` writes the repo-local governance artifacts under
            # .mozyo-bridge/ in the target repo so the package is
            # immediately usable. The main gate / role contract now lives
            # in the preset agent-workflow.md itself rather than a second
            # shipped development_flow.md file.
            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            self.assertEqual(0, result)
            for expected_path in (
                ".mozyo-bridge/rules/llm_rule_authoring.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
            ):
                self.assertTrue(
                    (project / expected_path).exists(),
                    msg=f"governed scaffold did not write {expected_path}",
                )
            # Vendor-copied Python tools must not ship anymore — the
            # docs catalog tooling lives inside the mozyo-bridge package
            # and is invoked through `mozyo-bridge docs ...` instead.
            self.assertFalse(
                (project / ".mozyo-bridge/tools").exists(),
                msg=(
                    "governed scaffold should no longer vendor-copy "
                    ".mozyo-bridge/tools/*.py — those tools now live "
                    "inside the mozyo-bridge package."
                ),
            )

            for marker in (
                "codex_direct_edit",
                "allowed_paths",
                "implementation_done",
                "owner_close_approval",
                "禁止_並行表現",
            ):
                self.assertIn(marker, installed)
            self.assertFalse(
                (project / ".mozyo-bridge/rules/development_flow.md").exists(),
                msg=(
                    "development_flow.md should not ship; governed agent "
                    "execution contract is merged into agent-workflow.md"
                ),
            )

            # The catalog example references the shipped rule files only,
            # never the nihonidenshi domain catalog ids.
            catalog_example = (
                project / ".mozyo-bridge/docs/catalog.yaml.example"
            ).read_text(encoding="utf-8")
            for marker in (
                "rule-llm-rule-authoring",
                "rule-docs-catalog-governance",
            ):
                self.assertIn(marker, catalog_example)
            self.assertNotIn("rule-mozyo-bridge-development-flow", catalog_example)
            self.assertNotIn(".mozyo-bridge/rules/development_flow.md", catalog_example)
            for forbidden in ("NIPT", "_機能リスト", "nihonidenshi"):
                self.assertNotIn(forbidden, catalog_example)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("redmine-rails-governed", state["preset"])
            # Every shipped extra is tracked in the manifest so `scaffold
            # status` can detect drift after operators edit the file.
            tracked_files = set(state["files"].keys())
            for expected in (
                "AGENTS.md",
                "CLAUDE.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
            ):
                self.assertIn(expected, tracked_files)
            # And the manifest must not claim any `.mozyo-bridge/tools/*`
            # entries — they moved into the mozyo-bridge package.
            self.assertFalse(
                any(p.startswith(".mozyo-bridge/tools/") for p in tracked_files),
                msg=(
                    "manifest still references .mozyo-bridge/tools/*; "
                    "the governed preset should no longer ship those."
                ),
            )

    def test_governed_scaffold_refuses_to_silently_overwrite_shipped_artifacts(self) -> None:
        """Shipped governance artifacts are protected from silent overwrite.

        Operators must opt in with `--backup` or `--force`, same as the
        router pair, because the file body may carry local edits even
        though the preset side is the source of truth.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            self.assertEqual(0, result)

            # Second apply without --backup / --force must refuse rather
            # than clobber the shipped artifacts the operator may have
            # touched between applies.
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            "redmine-rails-governed",
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )
            err = stderr.getvalue()
            self.assertIn("refusing to overwrite existing scaffold files", err)
            self.assertIn(".mozyo-bridge/rules/llm_rule_authoring.md", err)

            # --backup re-runs the apply and stashes the pre-existing file.
            backup_result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--backup",
                ]
            )
            self.assertEqual(0, backup_result)
            self.assertTrue(
                list((project / ".mozyo-bridge/rules").glob("llm_rule_authoring.md.bak.*"))
            )

    def test_governed_scaffold_status_clean_after_fresh_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, result)
            self.assertIn("preset: redmine-rails-governed", output)
            self.assertIn("result: clean", output)
            # Manifest now tracks router + repo-local artifacts. The
            # label must reflect that scope rather than misleadingly
            # calling everything a router file.
            self.assertIn("tracked files:", output)
            self.assertNotIn("router files:", output)

    def test_docs_validate_coverage_roots_precedence(self) -> None:
        """coverage_roots: CLI overrides catalog overrides default.

        The docs catalog tooling now ships as the ``mozyo-bridge docs``
        CLI inside the package. Precedence stays as before:

        1. ``--coverage-root`` CLI flag — wins when present.
        2. ``catalog.coverage_roots`` field — used when CLI absent.
        3. Built-in Rails-flavoured default — fallback when neither.

        The validator prints which source it used as the first
        ``notice:`` so operators can see the resolution from stdout.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            catalog_path = project / ".mozyo-bridge/docs/catalog.yaml"
            base_catalog = (
                project / ".mozyo-bridge/docs/catalog.yaml.example"
            ).read_text(encoding="utf-8")

            def run_coverage(*extra: str) -> tuple[int, str]:
                return self.run_cli(
                    [
                        "docs",
                        "validate",
                        "--check-file-coverage",
                        "--repo",
                        str(project),
                        *extra,
                    ]
                )

            # (3) No catalog field, no CLI flag — default Rails roots.
            catalog_path.write_text(base_catalog, encoding="utf-8")
            default_code, default_output = run_coverage()
            self.assertEqual(0, default_code)
            self.assertIn("coverage_roots source: default", default_output)

            # (2) Catalog declares coverage_roots — used when no CLI flag.
            catalog_path.write_text(
                base_catalog + "\ncoverage_roots:\n  - .mozyo-bridge\n",
                encoding="utf-8",
            )
            catalog_code, catalog_output = run_coverage()
            self.assertEqual(0, catalog_code)
            self.assertIn("coverage_roots source: catalog", catalog_output)
            self.assertNotIn("coverage_roots source: default", catalog_output)

            # (1) CLI overrides catalog. Non-existent root → notice only.
            cli_code, cli_output = run_coverage("--coverage-root", "unknown_layer")
            self.assertEqual(0, cli_code)
            self.assertIn("coverage_roots source: cli", cli_output)
            self.assertIn("coverage root does not exist", cli_output)

            # Bad shape: catalog with a string-typed coverage_roots
            # must fail validation (not silently ignored).
            catalog_path.write_text(
                base_catalog + "\ncoverage_roots: app\n", encoding="utf-8"
            )
            bad_code, bad_output = self.run_cli(
                ["docs", "validate", "--repo", str(project)]
            )
            self.assertEqual(1, bad_code)
            self.assertIn("coverage_roots must be a list", bad_output)

            # An unmatched file inside an *existing* coverage root is
            # still exit 1. This guards against the manifest-driven
            # refactor accidentally swallowing real coverage gaps.
            unmatched_root = project / "fresh_app"
            unmatched_root.mkdir()
            (unmatched_root / "orphan.rb").write_text("# orphan\n", encoding="utf-8")
            catalog_path.write_text(
                base_catalog + "\ncoverage_roots:\n  - fresh_app\n",
                encoding="utf-8",
            )
            real_gap_code, real_gap_output = run_coverage()
            self.assertEqual(1, real_gap_code)
            self.assertIn(
                "no file_convention matched: fresh_app/orphan.rb",
                real_gap_output,
            )

    def test_docs_cli_round_trips_against_shipped_catalog_example(self) -> None:
        """The packaged `docs ...` CLI must work on the catalog skeleton.

        After `scaffold apply`, copying `catalog.yaml.example` to
        `catalog.yaml` should immediately let every docs subcommand run
        cleanly. This is the operator's first-day experience after
        installing mozyo-bridge — if it fails, the governance package
        is unusable straight out of `scaffold apply`.
        """
        import shutil as _shutil

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            example = project / ".mozyo-bridge/docs/catalog.yaml.example"
            catalog = project / ".mozyo-bridge/docs/catalog.yaml"
            _shutil.copyfile(example, catalog)

            # docs validate
            validate_code, validate_output = self.run_cli(
                ["docs", "validate", "--repo", str(project)]
            )
            self.assertEqual(0, validate_code, msg=validate_output)
            self.assertIn("catalog validation passed", validate_output)

            # docs validate --check-file-coverage: missing roots ok.
            coverage_code, coverage_output = self.run_cli(
                [
                    "docs",
                    "validate",
                    "--check-file-coverage",
                    "--repo",
                    str(project),
                ]
            )
            self.assertEqual(0, coverage_code)
            self.assertIn("notice:", coverage_output)
            self.assertIn("catalog validation passed", coverage_output)

            # docs resolve — surfaces the catalog's rule docs via the
            # agent-guardrail file_convention's document_refs.
            resolve_code, resolve_output = self.run_cli(
                [
                    "docs",
                    "resolve",
                    "--repo",
                    str(project),
                    "--format",
                    "json",
                    "AGENTS.md",
                ]
            )
            self.assertEqual(0, resolve_code, msg=resolve_output)
            results = json.loads(resolve_output)
            self.assertEqual(1, len(results))
            resolved_ids = {doc["id"] for doc in results[0]["documents"]}
            self.assertIn("rule-docs-catalog-governance", resolved_ids)
            self.assertIn("rule-llm-rule-authoring", resolved_ids)

            # docs generate-file-conventions writes the output and a
            # follow-up --check confirms the round-trip is clean.
            gen_code, gen_output = self.run_cli(
                [
                    "docs",
                    "generate-file-conventions",
                    "--repo",
                    str(project),
                ]
            )
            self.assertEqual(0, gen_code, msg=gen_output)
            gen_path = project / ".mozyo-bridge/docs/file_conventions.generated.yaml"
            self.assertTrue(gen_path.exists())

            drift_code, drift_output = self.run_cli(
                [
                    "docs",
                    "generate-file-conventions",
                    "--check",
                    "--repo",
                    str(project),
                ]
            )
            self.assertEqual(0, drift_code, msg=drift_output)

    def test_docs_validate_check_file_coverage_canonical_cli_shape(self) -> None:
        """Pin `docs validate --check-file-coverage` as the coverage entrypoint.

        The governed agent workflow advertises a single canonical
        invocation for the coverage check::

            mozyo-bridge docs validate --repo <path> --check-file-coverage

        This regression test guards three CLI contracts that the workflow
        relies on:

        1. ``--check-file-coverage`` is a flag on the ``validate``
           subcommand (not a separate ``docs coverage`` subcommand).
        2. The flag's position relative to ``--repo`` does not matter —
           argparse keyword order must stay flexible so the workflow's
           advertised form keeps working alongside other phrasings.
        3. Plain ``docs validate`` (no flag) does **not** silently emit
           the coverage-source notice; the check is opt-in via the flag.
        """
        import shutil as _shutil

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            _shutil.copyfile(
                project / ".mozyo-bridge/docs/catalog.yaml.example",
                project / ".mozyo-bridge/docs/catalog.yaml",
            )

            # (1) Canonical order: --repo before --check-file-coverage.
            canonical_code, canonical_output = self.run_cli(
                [
                    "docs",
                    "validate",
                    "--repo",
                    str(project),
                    "--check-file-coverage",
                ]
            )
            self.assertEqual(0, canonical_code, msg=canonical_output)
            self.assertIn("coverage_roots source:", canonical_output)
            self.assertIn("catalog validation passed", canonical_output)

            # (2) Reversed order must produce the same outcome — argparse
            # keyword ordering is part of the public CLI contract.
            reversed_code, reversed_output = self.run_cli(
                [
                    "docs",
                    "validate",
                    "--check-file-coverage",
                    "--repo",
                    str(project),
                ]
            )
            self.assertEqual(0, reversed_code, msg=reversed_output)
            self.assertIn("coverage_roots source:", reversed_output)

            # (3) Without the flag, the validator must not emit the
            # coverage-source notice — coverage checking is opt-in.
            plain_code, plain_output = self.run_cli(
                ["docs", "validate", "--repo", str(project)]
            )
            self.assertEqual(0, plain_code, msg=plain_output)
            self.assertNotIn("coverage_roots source:", plain_output)
            self.assertIn("catalog validation passed", plain_output)

            # (4) The coverage check lives on `validate`, not as a
            # separate `coverage` subcommand. argparse must reject it.
            # Suppress argparse's usage/error text — matching the
            # parser-rejection convention used elsewhere in this file.
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_cli(["docs", "coverage", "--repo", str(project)])

    def test_preset_files_walker_skips_pycache_and_pyc(self) -> None:
        """The scaffold walker must drop pip-generated bytecode cruft.

        The governed preset no longer ships ``.py`` source files, so a
        normal wheel install will not generate ``__pycache__`` next to
        the remaining artifacts. The defence-in-depth still matters —
        a future preset that ships ``.py`` artifacts (or an operator
        who unpacks a wheel into the preset tree by accident) should
        not break the walker. We inject the cruft next to an existing
        shipped artifact and confirm the walker still surfaces the real
        files while dropping ``__pycache__`` / ``.pyc`` entries.
        """
        from mozyo_bridge.scaffold.rules import render_preset_extra_files

        rules_dir = (
            Path(__file__).resolve().parents[1]
            / "src/mozyo_bridge/scaffold/presets"
            / "redmine-rails-governed/files/.mozyo-bridge/rules"
        )
        self.assertTrue(rules_dir.exists(), msg=f"rules dir missing: {rules_dir}")
        fake_pycache = rules_dir / "__pycache__"
        fake_pyc = fake_pycache / "fake_module.cpython-314.pyc"
        fake_pycache.mkdir(exist_ok=True)
        try:
            fake_pyc.write_bytes(b"\x82\x82\x82bogus pyc bytes")
            extras = render_preset_extra_files("redmine-rails-governed")
            paths = {item.path.as_posix() for item in extras}
            # No __pycache__ entry and no .pyc entry leak through.
            self.assertFalse(
                any("__pycache__" in p for p in paths),
                msg=f"walker leaked __pycache__/* entries: {sorted(paths)}",
            )
            self.assertFalse(
                any(p.endswith(".pyc") for p in paths),
                msg=f"walker leaked .pyc entries: {sorted(paths)}",
            )
            # The legitimate rule files under the same directory still
            # surface — we only filter cache cruft, not real artifacts.
            self.assertIn(".mozyo-bridge/rules/llm_rule_authoring.md", paths)
        finally:
            import shutil as _shutil

            _shutil.rmtree(fake_pycache, ignore_errors=True)

    def test_governed_scaffold_apply_succeeds_after_wheel_install(self) -> None:
        """End-to-end: build wheel, pip install to a venv, run scaffold apply.

        Earlier iterations passed when running from the source tree but
        crashed under a real pip install because pip wrote `__pycache__/*.pyc`
        files next to the shipped catalog tools and the scaffold walker
        tried to decode them as UTF-8. This test mirrors that exact path
        so we don't regress.
        """
        import subprocess
        import venv as _venv

        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            build_proc = subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if build_proc.returncode != 0:
                self.skipTest(
                    "python -m build failed (probably missing build backend deps); "
                    f"stderr={build_proc.stderr[:500]}"
                )
            wheels = list(dist.glob("mozyo_bridge-*.whl"))
            self.assertEqual(1, len(wheels), msg=f"unexpected wheels: {wheels}")

            venv_dir = Path(tmp) / "venv"
            try:
                _venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
            except subprocess.CalledProcessError as exc:
                # Some Python distributions (e.g. uv-managed runtimes) abort
                # in ensurepip. The integration test still has value on CI
                # where venv works; skip when it doesn't rather than mask
                # the underlying regression.
                self.skipTest(f"venv with pip could not be created: {exc}")
            venv_python = venv_dir / "bin" / "python"
            venv_bin = venv_dir / "bin" / "mozyo-bridge"
            self.assertTrue(venv_python.exists(), msg=f"venv python missing: {venv_python}")

            install_proc = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "-q", str(wheels[0])],
                capture_output=True,
                text=True,
            )
            if install_proc.returncode != 0:
                self.skipTest(
                    "pip install of the built wheel failed (no network or build deps "
                    f"missing): stderr={install_proc.stderr[:500]}"
                )
            self.assertTrue(venv_bin.exists(), msg=f"mozyo-bridge entry-point missing: {venv_bin}")

            home_dir = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            rules_proc = subprocess.run(
                [str(venv_bin), "rules", "install", "--home", str(home_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                0,
                rules_proc.returncode,
                msg=(
                    "rules install failed post-wheel-install:\n"
                    f"stdout={rules_proc.stdout}\nstderr={rules_proc.stderr}"
                ),
            )

            apply_proc = subprocess.run(
                [
                    str(venv_bin),
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home_dir),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                0,
                apply_proc.returncode,
                msg=(
                    "scaffold apply failed post-wheel-install (this is the path "
                    "where pip's __pycache__/*.pyc files break the walker):\n"
                    f"stdout={apply_proc.stdout}\nstderr={apply_proc.stderr}"
                ),
            )

            # Every shipped governance artifact must land in the target
            # after a real wheel install, not just under the source tree.
            # Docs catalog tooling no longer ships as `.mozyo-bridge/tools/*.py`
            # — it lives inside the mozyo-bridge package and runs via the
            # `mozyo-bridge docs ...` CLI on the installed venv. We assert
            # the target tree does not carry the legacy vendor copy.
            for expected_path in (
                ".mozyo-bridge/rules/llm_rule_authoring.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
            ):
                self.assertTrue(
                    (project / expected_path).exists(),
                    msg=f"post-install scaffold did not write {expected_path}",
                )
            self.assertFalse(
                (project / ".mozyo-bridge/tools").exists(),
                msg=(
                    "post-install scaffold still wrote .mozyo-bridge/tools/ "
                    "(legacy vendor copy) — the tooling should live in the "
                    "mozyo-bridge package now."
                ),
            )

            # And no `.pyc` / `__pycache__` cruft should leak into the
            # target tree — the walker skips them.
            stray_pyc = list(project.rglob("*.pyc"))
            stray_cache = list(project.rglob("__pycache__"))
            self.assertEqual([], stray_pyc, msg=f"unexpected .pyc copied: {stray_pyc}")
            self.assertEqual(
                [], stray_cache, msg=f"unexpected __pycache__ copied: {stray_cache}"
            )

            # Smoke: the packaged docs CLI runs against the catalog
            # skeleton straight after install. This is what operators
            # actually use; the previous vendor-copy test verified the
            # wrong thing once the tools moved into the package.
            import shutil as _shutil

            _shutil.copyfile(
                project / ".mozyo-bridge/docs/catalog.yaml.example",
                project / ".mozyo-bridge/docs/catalog.yaml",
            )
            validate_proc = subprocess.run(
                [
                    str(venv_bin),
                    "docs",
                    "validate",
                    "--repo",
                    str(project),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                0,
                validate_proc.returncode,
                msg=(
                    "post-install `mozyo-bridge docs validate` failed:\n"
                    f"stdout={validate_proc.stdout}\nstderr={validate_proc.stderr}"
                ),
            )

    def test_governed_scaffold_ships_tmux_ui_and_nagger_artifacts_by_default(self) -> None:
        """Default `scaffold apply` writes the tmux-ui + Claude Nagger artifacts.

        Both are default-on so a fresh `redmine-rails-governed` install
        carries the agent-window status snippet and the Claude Nagger
        skeleton. The artifacts land under the standard governed paths
        and are tracked in the scaffold manifest.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            self.assertEqual(0, result)
            for expected_path in (
                ".mozyo-bridge/tmux/agent-ui.conf",
                ".claude-nagger/config.yaml.example",
                ".claude-nagger/command_conventions.yaml.example",
                ".claude-nagger/mcp_conventions.yaml.example",
                ".claude-nagger/.gitignore",
            ):
                self.assertTrue(
                    (project / expected_path).exists(),
                    msg=f"default scaffold did not write {expected_path}",
                )

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            tracked = set(state["files"].keys())
            for tracked_path in (
                ".mozyo-bridge/tmux/agent-ui.conf",
                ".claude-nagger/config.yaml.example",
                ".claude-nagger/command_conventions.yaml.example",
                ".claude-nagger/mcp_conventions.yaml.example",
                ".claude-nagger/.gitignore",
            ):
                self.assertIn(
                    tracked_path,
                    tracked,
                    msg=f"manifest does not track {tracked_path}",
                )

    def test_governed_doctor_reports_skipped_after_skip_with_backup(self) -> None:
        """`--skip-* --backup` opt-out: doctor must use the manifest, not disk.

        `--backup` leaves `.bak.<timestamp>` files inside the
        `.claude-nagger/` directory after the opt-out unlink. A doctor
        that checks disk state alone would see the directory still
        exists with the original example files missing and report
        `incomplete`, even though the operator deliberately opted out.
        Reading the manifest as source-of-truth keeps the diagnosis
        consistent with operator intent.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            # Default install first so the next apply has something to
            # reconcile away under --backup.
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            # Opt-out under --backup. The reconcile path stashes the
            # tracked artifacts as `.bak.<timestamp>` and unlinks the
            # originals, leaving the directory in a "backups only"
            # state that confuses any disk-only check.
            apply_result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--skip-tmux-ui",
                    "--skip-nagger",
                    "--backup",
                ]
            )
            self.assertEqual(0, apply_result)

            # Backup files must still be there (--backup contract); the
            # manifest must NOT track the .claude-nagger/* / tmux/* paths.
            self.assertTrue(
                list((project / ".claude-nagger").glob("*.bak.*")),
                msg="--backup did not leave any backup files",
            )
            self.assertFalse(
                (project / ".claude-nagger/config.yaml.example").exists()
            )
            state = scaffold_state(project)
            assert state is not None
            tracked = set(state["files"].keys())
            self.assertFalse(
                any(p.startswith(".claude-nagger/") for p in tracked)
            )
            self.assertNotIn(".mozyo-bridge/tmux/agent-ui.conf", tracked)

            # Doctor must read manifest, see no nagger / no tmux UI
            # tracked, and report `skipped` for both. Overall doctor
            # must stay `ok` — opt-out is not a failure mode.
            _, output = self.run_cli(
                [
                    "doctor",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            payload = json.loads(output)
            self.assertEqual(
                "skipped",
                payload["sections"]["claude_nagger"]["status"],
                msg=(
                    "doctor reported nagger status based on disk debris "
                    "instead of the manifest: "
                    f"{payload['sections']['claude_nagger']}"
                ),
            )
            self.assertEqual(
                "skipped",
                payload["sections"]["tmux"]["artifact"]["status"],
            )
            # The new manifest_tracks_* booleans expose the source-of-
            # truth signal so downstream tooling can rely on it too.
            self.assertFalse(
                payload["sections"]["claude_nagger"]["manifest_tracks_nagger"]
            )
            self.assertFalse(
                payload["sections"]["tmux"]["artifact"]["manifest_tracks_tmux_ui"]
            )

    def test_governed_doctor_reports_incomplete_for_real_drift(self) -> None:
        """Manifest tracks the artifact but it was removed → `incomplete`.

        Confirms that the manifest-driven doctor still catches genuine
        drift (operator deleted a tracked file by accident), not just
        the opt-out case. Without this assertion the move to manifest
        source-of-truth could silently swallow real failures.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            # Delete one tracked nagger example and the tmux snippet.
            (project / ".claude-nagger/config.yaml.example").unlink()
            (project / ".mozyo-bridge/tmux/agent-ui.conf").unlink()

            _, output = self.run_cli(
                [
                    "doctor",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            payload = json.loads(output)
            self.assertEqual(
                "incomplete",
                payload["sections"]["claude_nagger"]["status"],
            )
            self.assertEqual(
                "incomplete",
                payload["sections"]["tmux"]["artifact"]["status"],
            )
            self.assertFalse(payload["ok"])  # overall non-ok per BAD set

    def test_governed_scaffold_skip_after_default_apply_removes_stale_artifacts(self) -> None:
        """Re-applying with `--skip-*` must clean up previously-installed files.

        Earlier behaviour dropped the entries from the new manifest but
        left the on-disk artifacts in place, so `scaffold status`
        falsely reported `clean` while `.claude-nagger/` and
        `.mozyo-bridge/tmux/` files still existed. The reconcile path
        compares the previous manifest's tracked set with the new
        render and treats the gap as outgoing files. `--backup` /
        `--force` gates the destructive removal, same as the
        overwrite path for routers and extras.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            # Default apply lays down both governed default-on bundles.
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            for tracked_path in (
                ".mozyo-bridge/tmux/agent-ui.conf",
                ".claude-nagger/config.yaml.example",
            ):
                self.assertTrue((project / tracked_path).exists())

            # A bare re-apply with the opt-out flags must refuse to
            # remove existing files silently. This is the same
            # contract as overwriting routers without --backup/--force.
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            "redmine-rails-governed",
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                            "--skip-tmux-ui",
                            "--skip-nagger",
                        ]
                    )
            err = stderr.getvalue()
            self.assertIn("refusing to overwrite existing scaffold files", err)
            self.assertIn(".claude-nagger/config.yaml.example", err)
            self.assertIn(".mozyo-bridge/tmux/agent-ui.conf", err)

            # With --backup, the removal proceeds and previously-tracked
            # artifacts are stashed to `.bak.<timestamp>` next to the
            # original paths before deletion. The backups stay on disk
            # so the directories may still exist, but the originals
            # themselves must be gone (otherwise the opt-out is a no-op).
            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--skip-tmux-ui",
                    "--skip-nagger",
                    "--backup",
                ]
            )
            self.assertEqual(0, result)
            self.assertFalse(
                (project / ".claude-nagger/config.yaml.example").exists()
            )
            self.assertFalse(
                (project / ".mozyo-bridge/tmux/agent-ui.conf").exists()
            )
            # Backups landed next to the original paths.
            self.assertTrue(
                list((project / ".claude-nagger").glob("config.yaml.example.bak.*"))
            )
            self.assertTrue(
                list((project / ".mozyo-bridge/tmux").glob("agent-ui.conf.bak.*"))
            )

            # Manifest reflects reality: only the kept categories.
            state = scaffold_state(project)
            assert state is not None
            tracked = set(state["files"].keys())
            self.assertNotIn(".claude-nagger/config.yaml.example", tracked)
            self.assertNotIn(".mozyo-bridge/tmux/agent-ui.conf", tracked)
            self.assertIn(".mozyo-bridge/rules/llm_rule_authoring.md", tracked)

            # And status is genuinely clean — not just nominally clean
            # while stale files remain.
            status_result, status_output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, status_result)
            self.assertIn("result: clean", status_output)

    def test_governed_tmux_snippet_evaluates_without_duplicate_status(self) -> None:
        """Sourcing the snippet under tmux must not duplicate status output.

        The earlier `set -wga` revision left the default fallback
        (`#I:#W`) in place when the codex-conditional line appended,
        producing strings like `0:codex#[fg=colour67]0:codex#[default]`
        on codex windows. The nested-conditional form must render
        exactly one branch per window name. Skip when no tmux binary
        is available on PATH (CI environments without tmux still keep
        the unit-level expectations green via the static checks above).
        """
        import shutil as _shutil
        import subprocess

        if _shutil.which("tmux") is None:
            self.skipTest("tmux binary not on PATH")

        snippet = (
            Path(__file__).resolve().parents[1]
            / "src/mozyo_bridge/scaffold/presets/redmine-rails-governed/files/.mozyo-bridge/tmux/agent-ui.conf"
        )
        self.assertTrue(snippet.exists(), msg=f"snippet missing: {snippet}")

        # Use a dedicated tmux socket so concurrent test runs / the
        # operator's real tmux server are not touched.
        socket = f"mozyo-audit-{os.getpid()}"
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)

        def tmux(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["tmux", "-L", socket, *argv],
                capture_output=True,
                text=True,
                check=check,
                env=env,
            )

        # Best-effort cleanup of any prior server on the same socket.
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            capture_output=True,
            text=True,
            env=env,
        )
        try:
            tmux("-f", "/dev/null", "new-session", "-d", "-s", "audit", "-n", "codex", "sleep 60")
            tmux("source-file", str(snippet))
            fmt = tmux("show-options", "-gqv", "window-status-format").stdout.strip()
            self.assertTrue(fmt, msg="window-status-format was empty after source-file")

            def render(window_name: str) -> str:
                tmux("rename-window", "-t", "audit:0", window_name)
                return tmux("display-message", "-p", "-t", f"audit:{window_name}", fmt).stdout.strip()

            codex = render("codex")
            other = render("other")
            claude = render("claude")
        finally:
            subprocess.run(
                ["tmux", "-L", socket, "kill-server"],
                capture_output=True,
                text=True,
                env=env,
            )

        # No duplicated output: each rendered string contains the
        # window name exactly once. The earlier bug yielded
        # "0:codex#[fg=colour67]0:codex#[default]" — count == 2.
        self.assertEqual(1, codex.count(":codex"), msg=f"codex render duplicated: {codex!r}")
        self.assertEqual(1, claude.count(":claude"), msg=f"claude render duplicated: {claude!r}")
        self.assertEqual(1, other.count(":other"), msg=f"other render duplicated: {other!r}")

        # Agent windows carry the expected colour code; non-agent
        # windows render plain (no colour escape).
        self.assertIn("colour67", codex)
        self.assertIn("colour108", claude)
        self.assertNotIn("colour", other)

    def test_governed_scaffold_skip_flags_omit_artifacts_and_manifest_entries(self) -> None:
        """`--skip-tmux-ui` / `--skip-nagger` opt-outs drop the category.

        The dropped category neither lands on disk nor appears in the
        manifest, so a clean `scaffold status` afterwards confirms the
        opt-out is consistent (no drift detected from the missing files
        because the manifest never claimed them).
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--skip-tmux-ui",
                    "--skip-nagger",
                ]
            )
            self.assertEqual(0, result)

            # Skipped artifacts never landed.
            self.assertFalse(
                (project / ".mozyo-bridge/tmux/agent-ui.conf").exists()
            )
            self.assertFalse((project / ".claude-nagger").exists())

            # Non-skipped artifacts still ship — opt-outs are scoped.
            self.assertTrue(
                (project / ".mozyo-bridge/rules/llm_rule_authoring.md").exists()
            )
            self.assertTrue(
                (project / ".mozyo-bridge/rules/docs_catalog_governance.yaml").exists()
            )

            # Manifest tracks only what was written; status stays clean
            # because the manifest never claimed the skipped files.
            state = scaffold_state(project)
            assert state is not None
            tracked = set(state["files"].keys())
            self.assertNotIn(".mozyo-bridge/tmux/agent-ui.conf", tracked)
            for nagger in (
                ".claude-nagger/config.yaml.example",
                ".claude-nagger/command_conventions.yaml.example",
                ".claude-nagger/mcp_conventions.yaml.example",
                ".claude-nagger/.gitignore",
            ):
                self.assertNotIn(nagger, tracked)
            # Non-skipped categories still tracked.
            self.assertIn(".mozyo-bridge/rules/llm_rule_authoring.md", tracked)

            status_result, status_output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, status_result)
            self.assertIn("result: clean", status_output)

    def test_governed_doctor_reports_nagger_and_tmux_ui_artifact_state(self) -> None:
        """`doctor` surfaces tmux-ui + Claude Nagger artifact state.

        After a default `scaffold apply`, both the new `claude_nagger`
        section and the artifact attachment on the `tmux` section show
        the skeleton + snippet as present.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            result, output = self.run_cli(
                [
                    "doctor",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            payload = json.loads(output)
            sections = payload["sections"]
            self.assertIn("claude_nagger", sections)
            nagger = sections["claude_nagger"]
            # Skeleton landed but config.yaml is not yet copied — the
            # default-on apply puts the example next to the target's
            # config slot.
            self.assertEqual("skeleton-only", nagger["status"])
            for name in (
                "config.yaml.example",
                "command_conventions.yaml.example",
                "mcp_conventions.yaml.example",
            ):
                self.assertTrue(
                    nagger["examples"][name]["present"],
                    msg=f"doctor did not see {name}",
                )
            self.assertFalse(nagger["config_yaml"]["present"])

            tmux = sections["tmux"]
            self.assertIn("artifact", tmux)
            self.assertTrue(tmux["artifact"]["present"])
            self.assertEqual("ok", tmux["artifact"]["status"])

            # And after opt-out, doctor reports `skipped`.
            project_skip = Path(tmp) / "project_skip"
            project_skip.mkdir()
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project_skip),
                    "--home",
                    str(home),
                    "--skip-nagger",
                    "--skip-tmux-ui",
                ]
            )
            _, skip_output = self.run_cli(
                [
                    "doctor",
                    "--target",
                    str(project_skip),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            skip_payload = json.loads(skip_output)
            self.assertEqual(
                "skipped", skip_payload["sections"]["claude_nagger"]["status"]
            )
            self.assertEqual(
                "skipped",
                skip_payload["sections"]["tmux"]["artifact"]["status"],
            )

    def test_governed_scaffold_reconciles_legacy_governed_artifacts_on_reapply(self) -> None:
        """Re-apply with the new preset must clean up legacy governed artifacts.

        Prior governed-scaffold releases vendor-copied
        ``.mozyo-bridge/tools/*.py`` and a separate
        ``.mozyo-bridge/rules/development_flow.md`` into the target and
        recorded those paths in the scaffold manifest. The new preset
        does not ship those files, so the next `scaffold apply` must
        reconcile them as outgoing files: refuse to overwrite silently,
        then remove them when ``--backup`` (or ``--force``) is provided.
        This guards the upgrade path for existing operators.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            # First, run a real apply to lay down routers, manifest,
            # and the rule/doc files we will keep.
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            # Simulate a legacy scaffold state by writing tool source
            # files and patching the manifest to claim them. This is
            # exactly what an older governed release left behind.
            tools_dir = project / ".mozyo-bridge/tools"
            tools_dir.mkdir(parents=True, exist_ok=True)
            legacy_dev_flow = project / ".mozyo-bridge/rules/development_flow.md"
            legacy_files = {
                legacy_dev_flow: "# legacy development flow\n",
                tools_dir / "docs_catalog.py": "# legacy vendor copy\n",
                tools_dir / "validate_catalog.py": "# legacy vendor copy\n",
                tools_dir / "resolve_audit_docs.py": "# legacy vendor copy\n",
                tools_dir / "generate_file_conventions.py": "# legacy vendor copy\n",
                tools_dir / "audit_doc_impact.py": "# legacy vendor copy\n",
            }
            for path, body in legacy_files.items():
                path.write_text(body, encoding="utf-8")

            manifest_path = project / ".mozyo-bridge/scaffold.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for path in legacy_files:
                rel = path.relative_to(project).as_posix()
                manifest["files"][rel] = {
                    "sha256": "0" * 64,  # arbitrary; reconcile only consults the key set
                }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            # Re-applying without --backup / --force must refuse: the
            # outgoing legacy files would be silently destroyed otherwise.
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            "redmine-rails-governed",
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )
            err = stderr.getvalue()
            self.assertIn("refusing to overwrite existing scaffold files", err)
            self.assertIn(".mozyo-bridge/rules/development_flow.md", err)
            self.assertIn(".mozyo-bridge/tools/validate_catalog.py", err)

            # With --backup, the reconcile path stashes each legacy tool
            # to `.bak.<timestamp>` and removes the original. The new
            # manifest no longer tracks them.
            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--backup",
                ]
            )
            self.assertEqual(0, result)
            for path in legacy_files:
                self.assertFalse(path.exists(), msg=f"legacy tool not removed: {path}")
            # And `.bak.<timestamp>` files landed next to where the
            # originals lived.
            backups = list(tools_dir.glob("*.bak.*")) if tools_dir.exists() else []
            self.assertTrue(backups, msg="--backup did not stash any legacy tool files")

            state = scaffold_state(project)
            assert state is not None
            tracked = set(state["files"].keys())
            self.assertFalse(
                any(p.startswith(".mozyo-bridge/tools/") for p in tracked),
                msg=(
                    "post-reconcile manifest still references legacy "
                    f".mozyo-bridge/tools/ entries: "
                    f"{[p for p in tracked if p.startswith('.mozyo-bridge/tools/')]}"
                ),
            )
            self.assertNotIn(".mozyo-bridge/rules/development_flow.md", tracked)

            # scaffold status reports clean after reconcile.
            status_result, status_output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, status_result)
            self.assertIn("result: clean", status_output)

    def test_governed_preset_artifacts_ship_in_built_wheel(self) -> None:
        """The governed preset's repo-local artifacts must end up in the wheel.

        setuptools' glob for `package-data` skips hidden directories by
        default, so the package-data spec must enumerate the `.mozyo-bridge/`
        subtree explicitly. We build a real wheel via `python -m build`
        and assert every shipped artifact ends up inside the wheel; this
        guards against regressions where the source tree builds locally
        but the wheel released to PyPI is silently missing the governance
        package.
        """
        import subprocess
        import zipfile

        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "dist"
            out_dir.mkdir()
            build_proc = subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if build_proc.returncode != 0:
                self.skipTest(
                    "python -m build failed (probably missing build backend deps); "
                    f"stderr={build_proc.stderr[:500]}"
                )
            wheels = list(out_dir.glob("mozyo_bridge-*.whl"))
            self.assertEqual(
                1,
                len(wheels),
                msg=f"expected exactly one wheel under {out_dir}, found {wheels}",
            )

            with zipfile.ZipFile(wheels[0]) as wheel:
                names = set(wheel.namelist())

            governed_prefix = (
                "mozyo_bridge/scaffold/presets/redmine-rails-governed/"
            )
            mb_prefix = governed_prefix + "files/.mozyo-bridge/"
            nagger_prefix = governed_prefix + "files/.claude-nagger/"
            expected = [
                governed_prefix + "VERSION",
                governed_prefix + "agent-workflow.md",
                mb_prefix + "rules/llm_rule_authoring.md",
                mb_prefix + "rules/docs_catalog_governance.yaml",
                mb_prefix + "docs/catalog.yaml.example",
                mb_prefix + "tmux/agent-ui.conf",
                nagger_prefix + "config.yaml.example",
                nagger_prefix + "command_conventions.yaml.example",
                nagger_prefix + "mcp_conventions.yaml.example",
                nagger_prefix + ".gitignore",
            ]
            missing = [entry for entry in expected if entry not in names]
            self.assertEqual(
                [],
                missing,
                msg=(
                    "wheel is missing governed preset artifacts (release would ship "
                    "an empty governance package):\n  " + "\n  ".join(missing)
                ),
            )
            # Docs catalog tooling lives in mozyo_bridge.docs_tools now,
            # not in the preset's `files/` tree. The wheel must NOT ship
            # any vendor-copied tools under that prefix anymore.
            legacy_tools = [
                name for name in names if mb_prefix + "tools/" in name
            ]
            self.assertEqual(
                [],
                legacy_tools,
                msg=(
                    "wheel still carries vendor-copied .mozyo-bridge/tools/ "
                    f"entries: {legacy_tools}"
                ),
            )
            # And the docs_tools package itself must ship.
            docs_tools_prefix = "mozyo_bridge/docs_tools/"
            for expected_module in (
                "__init__.py",
                "catalog.py",
                "validate.py",
                "resolve.py",
                "generate.py",
                "impact.py",
            ):
                self.assertIn(
                    docs_tools_prefix + expected_module,
                    names,
                    msg=(
                        f"wheel is missing the docs_tools module "
                        f"`{expected_module}` — the docs CLI cannot run."
                    ),
                )

    def test_scaffold_requires_installed_central_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(["scaffold", "apply", "redmine", "--target", str(project), "--home", str(home)])

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

                result, output = self.run_cli(["scaffold", "apply", "asana", "--home", str(home)])

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
                    result, _ = self.run_cli(["scaffold", "apply", "none", "--home", str(home)])

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
            self.assertEqual(["ok"] * len(rows), [row["status"] for row in rows])
            self.assertIn(f"asana\tok\t{package_version('asana')}\t{package_version('asana')}\t", output)
            self.assertIn(
                f"redmine-rails\tok\t{package_version('redmine-rails')}\t{package_version('redmine-rails')}\t",
                output,
            )
            self.assertIn(
                f"redmine-governed\tok\t{package_version('redmine-governed')}\t"
                f"{package_version('redmine-governed')}\t",
                output,
            )
            self.assertIn(
                f"redmine-rails-governed\tok\t{package_version('redmine-rails-governed')}\t"
                f"{package_version('redmine-rails-governed')}\t",
                output,
            )
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

    def test_rules_home_default_prints_portable_expression_only(self) -> None:
        # Spoof env values via a tempdir so the fixture itself never carries
        # a literal personal-home-shaped path (the release tree scanner
        # rejects `/Users/<name>/` in tracked source). The assertions still
        # prove the default output cannot leak the env override or HOME.
        with tempfile.TemporaryDirectory() as tmp:
            spoofed_home = Path(tmp) / "fake-home"
            spoofed_home.mkdir()
            spoofed_override = Path(tmp) / "mozyo-bridge-override"
            with patch.dict(
                os.environ,
                {"HOME": str(spoofed_home), "MOZYO_BRIDGE_HOME": str(spoofed_override)},
            ):
                result, output = self.run_cli(["rules", "home"])

            self.assertEqual(0, result)
            self.assertEqual("${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}\n", output)
            self.assertNotIn(str(spoofed_home), output)
            self.assertNotIn(str(spoofed_override), output)
            self.assertNotIn(str(Path.home()), output)

    def test_rules_home_resolved_honors_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "custom_home"
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(override)}):
                result, output = self.run_cli(["rules", "home", "--resolved"])

            self.assertEqual(0, result)
            self.assertEqual(f"{override.resolve()}\n", output)

    def test_rules_home_resolved_expands_tilde_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "fake_home"
            fake_home.mkdir()
            env = {"HOME": str(fake_home)}
            env_clear = {"MOZYO_BRIDGE_HOME": ""}
            with patch.dict(os.environ, env), patch.dict(os.environ, env_clear, clear=False):
                os.environ.pop("MOZYO_BRIDGE_HOME", None)
                result, output = self.run_cli(["rules", "home", "--resolved"])

            self.assertEqual(0, result)
            self.assertEqual(f"{(fake_home / '.mozyo_bridge').resolve()}\n", output)

    def test_rules_home_help_text_distinguishes_portable_and_resolved(self) -> None:
        parser = build_parser()
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit):
                parser.parse_args(["rules", "home", "--help"])
        help_text = stdout.getvalue()

        # argparse wraps long descriptions; normalize whitespace before
        # checking that the portable-vs-resolved distinction is documented.
        flat = " ".join(help_text.split())
        self.assertIn("${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}", flat)
        self.assertIn("committed docs", flat)
        self.assertIn("--resolved", flat)
        self.assertIn("local diagnostics", flat)

    def test_scaffold_refuses_overwrite_by_default_and_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(["scaffold", "apply", "none", "--target", str(project), "--home", str(home)])

            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.run_cli(["scaffold", "apply", "none", "--target", str(project), "--home", str(home)])

            fresh = Path(tmp) / "fresh"
            fresh.mkdir()
            result, output = self.run_cli(
                ["scaffold", "apply", "none", "--target", str(fresh), "--home", str(home), "--dry-run"]
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

            result, _ = self.run_cli(["scaffold", "apply", "redmine", "--target", str(project), "--home", str(home), "--backup"])

            self.assertEqual(0, result)
            self.assertIn("active な `Redmine issue / journal`", (project / "AGENTS.md").read_text(encoding="utf-8"))
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

                self.run_cli(["scaffold", "apply", "asana", "--target", str(project), "--home", "home"])

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
        from mozyo_bridge.scaffold.rules import PRESETS

        for preset in PRESETS:
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / "home"
                    project = Path(tmp) / "project"
                    project.mkdir()

                    self.run_cli(["rules", "install", "--home", str(home)])
                    result, _ = self.run_cli(
                        ["scaffold", "apply", preset, "--target", str(project), "--home", str(home)]
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


class ScaffoldRepoLocalModeTest(unittest.TestCase):
    """Repo-local guardrail rules mode for Dev Container / ephemeral-home workspaces.

    Asana task 1214948474095217. Covers `rules install --repo-local`,
    `rules status --repo-local`, `scaffold apply --repo-local`, `scaffold diff
    --repo-local`, the auto-detecting `scaffold status` path, the manifest
    `mode` field, the repo-local portable `rule_path`, the host-path leak
    guard for repo-local artifacts, and the `--home` / `--repo-local`
    mutual exclusion.
    """

    REPO_LOCAL_RULE_PATH_TEMPLATE = (
        ".mozyo-bridge/rules/presets/{preset}/agent-workflow.md"
    )

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def test_rules_install_repo_local_writes_into_target_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()

            result, output = self.run_cli(
                ["rules", "install", "--repo-local", str(project)]
            )

            self.assertEqual(0, result)
            from mozyo_bridge.scaffold.rules import PRESETS

            for preset in PRESETS:
                workflow = (
                    project
                    / ".mozyo-bridge"
                    / "rules"
                    / "presets"
                    / preset
                    / "agent-workflow.md"
                )
                version = (
                    project / ".mozyo-bridge" / "rules" / "presets" / preset / "VERSION"
                )
                self.assertTrue(workflow.exists(), f"missing workflow for {preset}")
                self.assertTrue(version.exists(), f"missing VERSION for {preset}")
                self.assertIn(str(workflow), output)

    def test_rules_status_repo_local_reports_target_repo_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--repo-local", str(project)])

            result, output = self.run_cli(
                ["rules", "status", "--repo-local", str(project)]
            )

            self.assertEqual(0, result)
            self.assertIn("asana\tok", output)
            self.assertIn(
                str(project.resolve() / ".mozyo-bridge" / "rules" / "presets" / "asana"),
                output,
            )

    def test_rules_status_repo_local_flags_uninstalled_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()

            result, output = self.run_cli(
                ["rules", "status", "--repo-local", str(project)]
            )

            self.assertEqual(1, result)
            self.assertIn("asana\tmissing", output)

    def test_rules_install_repo_local_and_home_are_mutually_exclusive(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["rules", "install", "--home", "/tmp/x", "--repo-local", "/tmp/y"]
                )

    def test_scaffold_apply_repo_local_uses_relative_rule_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--repo-local", str(project)])

            result, output = self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--repo-local"]
            )

            self.assertEqual(0, result)
            expected_rule_path = self.REPO_LOCAL_RULE_PATH_TEMPLATE.format(preset="asana")
            for filename in ("AGENTS.md", "CLAUDE.md", ".mozyo-bridge/scaffold.json"):
                text = (project / filename).read_text(encoding="utf-8")
                self.assertIn(expected_rule_path, text)
                # The portable repo-local form must not carry the central
                # ${MOZYO_BRIDGE_HOME:...} expansion — Dev Container users
                # have no such home to resolve against.
                self.assertNotIn("${MOZYO_BRIDGE_HOME", text)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("repo-local", state["mode"])
            self.assertEqual(expected_rule_path, state["rule_path"])
            self.assertEqual(2, state["schema_version"])

    def test_scaffold_apply_repo_local_does_not_leak_host_paths_for_any_preset(self) -> None:
        # Repo-local artifacts must never carry an absolute host path. The
        # whole point of the Dev Container mode is portability across hosts.
        from mozyo_bridge.scaffold.rules import PRESETS

        for preset in PRESETS:
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp) / "project"
                    project.mkdir()
                    self.run_cli(["rules", "install", "--repo-local", str(project)])

                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            preset,
                            "--target",
                            str(project),
                            "--repo-local",
                        ]
                    )

                    expected_rule_path = self.REPO_LOCAL_RULE_PATH_TEMPLATE.format(
                        preset=preset
                    )
                    resolved_project = project.resolve()
                    for filename in ("AGENTS.md", "CLAUDE.md", ".mozyo-bridge/scaffold.json"):
                        text = (project / filename).read_text(encoding="utf-8")
                        self.assertNotIn("/Users/", text)
                        self.assertNotIn(str(resolved_project), text)
                        self.assertIn(expected_rule_path, text)

    def test_scaffold_apply_repo_local_rejects_combined_home_flag(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "scaffold",
                        "apply",
                        "asana",
                        "--target",
                        "/tmp/x",
                        "--home",
                        "/tmp/y",
                        "--repo-local",
                    ]
                )

    def test_scaffold_diff_repo_local_clean_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--repo-local", str(project)])
            self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--repo-local"]
            )

            result, output = self.run_cli(
                ["scaffold", "diff", "asana", "--target", str(project), "--repo-local"]
            )

            self.assertEqual(0, result)
            self.assertIn("scaffold diff: clean", output)

    def test_scaffold_diff_repo_local_detects_router_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--repo-local", str(project)])
            self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--repo-local"]
            )
            agents = project / "AGENTS.md"
            agents.write_text(
                agents.read_text(encoding="utf-8") + "\nlocal hand edit\n",
                encoding="utf-8",
            )

            result, output = self.run_cli(
                ["scaffold", "diff", "asana", "--target", str(project), "--repo-local"]
            )

            self.assertEqual(1, result)
            self.assertIn("local hand edit", output)

    def test_scaffold_status_auto_detects_repo_local_mode(self) -> None:
        # Status takes no --repo-local flag; the manifest's `mode` field is
        # the source of truth so a single status command works for either
        # mode without operator bookkeeping.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--repo-local", str(project)])
            self.run_cli(
                ["scaffold", "apply", "redmine", "--target", str(project), "--repo-local"]
            )

            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project)]
            )

            self.assertEqual(0, result)
            self.assertIn("mode: repo-local", output)
            self.assertIn("result: clean", output)

    def test_scaffold_status_repo_local_manifest_with_home_flag_is_invalid(self) -> None:
        # Passing --home against a repo-local manifest is operator error;
        # status surfaces it as an invalid manifest rather than silently
        # comparing against the wrong store.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            unused_home = Path(tmp) / "unused-home"
            self.run_cli(["rules", "install", "--repo-local", str(project)])
            self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--repo-local"]
            )

            result, output = self.run_cli(
                [
                    "scaffold",
                    "status",
                    "--target",
                    str(project),
                    "--home",
                    str(unused_home),
                ]
            )

            self.assertEqual(1, result)
            self.assertIn("repo-local mode; --home is unused", output)

    def test_scaffold_apply_repo_local_requires_repo_local_rules_install(self) -> None:
        # The repo-local store is read from <target>/.mozyo-bridge, so a
        # central-mode `rules install` does NOT satisfy `scaffold apply
        # --repo-local`. The error must point operators at the repo-local
        # install command, not the central one.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            "asana",
                            "--target",
                            str(project),
                            "--repo-local",
                        ]
                    )

            err_text = stderr.getvalue()
            self.assertIn("rules preset is not installed", err_text)
            self.assertIn("--repo-local", err_text)

    def test_scaffold_apply_central_mode_default_remains_unchanged(self) -> None:
        # Backward compatibility: without --repo-local, scaffold apply must
        # still emit the central ${MOZYO_BRIDGE_HOME:...} portable form and
        # manifest mode "central". Default behavior is the load-bearing
        # contract for existing users.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--home", str(home)]
            )

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("central", state["mode"])
            self.assertEqual(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md",
                state["rule_path"],
            )
            for filename in ("AGENTS.md", "CLAUDE.md"):
                text = (project / filename).read_text(encoding="utf-8")
                self.assertIn("${MOZYO_BRIDGE_HOME", text)


class ScaffoldDiffTest(unittest.TestCase):
    """Coverage for the new `scaffold diff <preset>` breaking-change entrypoint."""

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def test_diff_detects_unapplied_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            result, output = self.run_cli(
                ["scaffold", "diff", "asana", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(1, result)
            self.assertIn("+++ b/AGENTS.md", output)
            self.assertIn("+++ b/CLAUDE.md", output)
            self.assertIn("+++ b/.mozyo-bridge/scaffold.json", output)
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertFalse((project / "CLAUDE.md").exists())
            self.assertFalse((project / ".mozyo-bridge" / "scaffold.json").exists())

    def test_diff_is_clean_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                ["scaffold", "apply", "redmine", "--target", str(project), "--home", str(home)]
            )

            result, output = self.run_cli(
                ["scaffold", "diff", "redmine", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(0, result)
            self.assertIn("scaffold diff: clean", output)
            self.assertNotIn("--- a/", output)

    def test_diff_detects_local_edit_against_rendered_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--home", str(home)]
            )

            agents = project / "AGENTS.md"
            agents.write_text(
                agents.read_text(encoding="utf-8") + "\nlocal hand edit\n",
                encoding="utf-8",
            )

            result, output = self.run_cli(
                ["scaffold", "diff", "asana", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(1, result)
            self.assertIn("--- a/AGENTS.md", output)
            self.assertIn("+++ b/AGENTS.md", output)
            self.assertIn("local hand edit", output)

    def test_diff_requires_installed_central_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        ["scaffold", "diff", "asana", "--target", str(project), "--home", str(home)]
                    )

            self.assertIn("rules preset is not installed", stderr.getvalue())


class ScaffoldProjectLocalAdditionsPreservationTest(unittest.TestCase):
    """Marker-bounded preservation contract for project-local additions.

    Operators put project-local layer body (Rails / Ruby version, dangerous
    DB / test commands, Presenter / YAML conventions, docs catalog
    governance, role-boundary overrides, etc.) between the marker pair shipped
    inside scaffold-generated AGENTS.md / CLAUDE.md. `scaffold apply` and
    `scaffold diff` must mechanically preserve that body across re-sync so
    mature target repos do not lose project-local guardrails when a new
    preset version lands.
    """

    BEGIN = "<!-- mozyo-bridge:project-local-additions:begin -->"
    END = "<!-- mozyo-bridge:project-local-additions:end -->"

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def _setup(self, tmp: Path, preset: str = "redmine-rails") -> tuple[Path, Path]:
        home = tmp / "home"
        project = tmp / "project"
        project.mkdir()
        self.run_cli(["rules", "install", "--home", str(home)])
        self.run_cli(
            ["scaffold", "apply", preset, "--target", str(project), "--home", str(home)]
        )
        return home, project

    def _insert_project_local_body(self, file_path: Path, body: str) -> str:
        """Replace the marker-bounded block in `file_path` with `body`.

        Returns the raw text written to disk so callers can assert against it.
        """
        text = file_path.read_text(encoding="utf-8")
        begin_idx = text.find(self.BEGIN)
        end_idx = text.find(self.END, begin_idx)
        assert begin_idx >= 0 and end_idx >= 0, "marker pair must be present"
        new_text = (
            text[: begin_idx + len(self.BEGIN)]
            + "\n"
            + body
            + "\n"
            + text[end_idx:]
        )
        file_path.write_text(new_text, encoding="utf-8")
        return new_text

    def test_router_templates_carry_marker_pair(self) -> None:
        """Both AGENTS.md and CLAUDE.md ship with the marker pair on fresh apply."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                ["scaffold", "apply", "redmine-rails", "--target", str(project), "--home", str(home)]
            )

            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
            for text in (agents, claude):
                self.assertIn(self.BEGIN, text)
                self.assertIn(self.END, text)

    def test_backup_apply_preserves_project_local_body(self) -> None:
        """--backup re-apply preserves project-local body inside the markers."""
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup(Path(tmp))
            project_local_body = (
                "## Project-Local Layer\n\n"
                "- Ruby 3.1.5 / Rails 7.0.8.1.\n"
                "- DB safety: TEST_DB_ENV=test must be set when running rspec.\n"
                "- Read-only docs directory: Doc/ (edit forbidden).\n"
            )
            agents_path = project / "AGENTS.md"
            claude_path = project / "CLAUDE.md"
            self._insert_project_local_body(agents_path, project_local_body)
            self._insert_project_local_body(
                claude_path, "## Project-Local Reminder\n\nRAILS_ENV=test is mandatory.\n"
            )

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--backup",
                ]
            )

            self.assertEqual(0, result)
            agents_after = agents_path.read_text(encoding="utf-8")
            claude_after = claude_path.read_text(encoding="utf-8")
            # Project-local body must survive re-apply, byte-for-byte.
            self.assertIn("Ruby 3.1.5 / Rails 7.0.8.1.", agents_after)
            self.assertIn("TEST_DB_ENV=test", agents_after)
            self.assertIn("Read-only docs directory: Doc/ (edit forbidden).", agents_after)
            self.assertIn("RAILS_ENV=test is mandatory.", claude_after)
            # Markers still present after re-apply.
            self.assertIn(self.BEGIN, agents_after)
            self.assertIn(self.END, agents_after)
            self.assertIn(self.BEGIN, claude_after)
            self.assertIn(self.END, claude_after)
            # .bak.<timestamp> files retain the pre-apply state as safety net.
            self.assertTrue(list(project.glob("AGENTS.md.bak.*")))
            self.assertTrue(list(project.glob("CLAUDE.md.bak.*")))

    def test_force_apply_preserves_project_local_body(self) -> None:
        """--force re-apply also preserves project-local body inside the markers."""
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup(Path(tmp))
            agents_path = project / "AGENTS.md"
            self._insert_project_local_body(
                agents_path, "- project-local addition that must survive --force.\n"
            )

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--force",
                ]
            )

            self.assertEqual(0, result)
            agents_after = agents_path.read_text(encoding="utf-8")
            self.assertIn("project-local addition that must survive --force.", agents_after)
            self.assertIn(self.BEGIN, agents_after)
            self.assertIn(self.END, agents_after)
            # --force does not produce a .bak.* file (this is the documented
            # difference between --force and --backup; preservation does not
            # change that).
            self.assertFalse(list(project.glob("AGENTS.md.bak.*")))

    def test_diff_is_clean_after_preserving_project_local_body(self) -> None:
        """scaffold diff returns clean (exit 0) once project-local body is inside markers.

        Once the operator has put their additions between the markers AND
        re-applied so the manifest records the post-substitution hash, a
        subsequent `scaffold diff` against the same preset version must not
        report any pending changes — the rendered router (with substituted
        project-local body) matches the on-disk router byte-for-byte.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup(Path(tmp))
            agents_path = project / "AGENTS.md"
            self._insert_project_local_body(
                agents_path, "- project-local fact preserved across re-sync.\n"
            )
            # Re-apply so the manifest records the post-substitution hash.
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--backup",
                ]
            )

            result, output = self.run_cli(
                [
                    "scaffold",
                    "diff",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            self.assertEqual(0, result)
            self.assertIn("scaffold diff: clean", output)

    def test_preservation_skipped_when_markers_absent_on_disk(self) -> None:
        """Legacy on-disk routers without markers fall through unchanged.

        When the operator's AGENTS.md does NOT contain the marker pair (legacy
        scaffold or hand-edited content with markers removed), preservation
        does not fire — re-apply with `--force` overwrites the file with the
        fresh scaffold base, exactly as the existing safety contract intended.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup(Path(tmp))
            agents_path = project / "AGENTS.md"
            # Replace the file entirely with a legacy-style router that has
            # no marker pair.
            legacy_body = "# AGENTS (legacy)\n\n- no marker pair here.\n"
            agents_path.write_text(legacy_body, encoding="utf-8")

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--force",
                ]
            )

            self.assertEqual(0, result)
            agents_after = agents_path.read_text(encoding="utf-8")
            # Marker pair restored (fresh template).
            self.assertIn(self.BEGIN, agents_after)
            self.assertIn(self.END, agents_after)
            # Legacy body without markers is overwritten — no preservation
            # fallback for that case (operator must move content into markers
            # first, as documented in the preset's Apply Discipline section).
            self.assertNotIn("no marker pair here.", agents_after)

    def test_status_clean_after_preserving_body_and_reapplying(self) -> None:
        """scaffold status reports clean after preservation + re-apply cycle."""
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup(Path(tmp))
            self._insert_project_local_body(
                project / "AGENTS.md", "- project fact A\n- project fact B\n"
            )
            self._insert_project_local_body(
                project / "CLAUDE.md", "- claude reminder X\n"
            )
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--backup",
                ]
            )

            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(0, result)
            self.assertIn("result: clean", output)

    def test_extract_and_substitute_helpers(self) -> None:
        """Unit-level coverage for the extract/substitute primitives."""
        from mozyo_bridge.scaffold.rules import (
            extract_project_local_block,
            substitute_project_local_block,
        )

        on_disk = (
            "header\n"
            + self.BEGIN
            + "\nproject additions\n"
            + self.END
            + "\ntrailer\n"
        )
        rendered = (
            "header\n"
            + self.BEGIN
            + "\nboilerplate\n"
            + self.END
            + "\ntrailer\n"
        )
        block = extract_project_local_block(on_disk)
        self.assertEqual("\nproject additions\n", block)
        new_rendered = substitute_project_local_block(rendered, block)
        self.assertIn("\nproject additions\n", new_rendered)
        self.assertNotIn("boilerplate", new_rendered)

        # Missing markers on either side returns/keeps the input unchanged.
        self.assertIsNone(extract_project_local_block("no markers here"))
        self.assertEqual(
            "no markers here",
            substitute_project_local_block("no markers here", "ignored"),
        )


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

    def test_workflow_lifecycle_anchors_at_handoff_primitive(self) -> None:
        # Regression rail for Asana 1214760806178471: the Handoff Lifecycle
        # section must name the high-level primitive (`mozyo-bridge handoff
        # send` / `handoff reply` / top-level `reply` alias) as the standard
        # send. If a future refactor of the skill reference drops these names
        # in favor of caller-assembled `read` + `message` shell choreography,
        # this assertion catches it before agents start drifting back to the
        # old path. The presence of these names alongside the explicit
        # operator/debug paragraph also locks the boundary between standard
        # handoff and low-level primitives in a single section.
        section_start = self.workflow.index("## Handoff Lifecycle")
        section_end = self.workflow.index(
            "## Claude / Codex Role Boundary", section_start
        )
        section = self.workflow[section_start:section_end]
        self.assertIn("`mozyo-bridge handoff send`", section)
        self.assertIn("`mozyo-bridge handoff reply`", section)
        self.assertIn("`mozyo-bridge reply`", section)
        self.assertIn("compatibility", section)
        # The operator/debug paragraph must explicitly call out the low-level
        # commands so the boundary survives doc refactors.
        self.assertIn("`mozyo-bridge read`", section)
        self.assertIn("`mozyo-bridge message`", section)
        self.assertIn("operator/debug", section)
        self.assertIn("`notify-*-legacy-task`", section)
        # Receiver step must explicitly forbid scrollback / status / doctor
        # inference when a durable anchor exists; this is the rule that
        # collapsed in the failure modes recorded on Asana 1214760517082054.
        self.assertIn("`mozyo-bridge status`", section)
        self.assertIn("doctor", section)
        self.assertIn("pane scrollback", section)
        self.assertIn("durable", section)

    def test_audit_owned_commit_section_notes_owner_close_approval_separation(
        self,
    ) -> None:
        """The shared Audit-Owned Commit Authority section must call out that
        review approval is not the same as owner close approval on systems
        where the central preset distinguishes them. This keeps Redmine
        implementers from closing on review approval alone even when they
        only read the shared workflow reference and not the Redmine central
        preset directly.
        """
        section_start = self.workflow.index("## Audit-Owned Commit Authority")
        section_end = self.workflow.index(
            "## Workflow Change Verification", section_start
        )
        section = self.workflow[section_start:section_end]
        self.assertIn("owner close approval", section)
        self.assertIn("Review approval alone is not close approval", section)
        self.assertIn("Close Approval Separation", section)

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


class ScaffoldPresetHandoffPrimitiveDocsTest(unittest.TestCase):
    """Regression rails for Asana 1214760806178471: the scaffold presets must
    document the high-level handoff primitive as the standard handoff/reply
    path. If a future refactor accidentally restores the older "Standard
    notification command: `mozyo-bridge notify-* --issue --journal`" wording
    in either Asana or Redmine, or drops the explicit operator/debug boundary
    around `read` / `message` / `type` / `keys`, these tests catch it before
    operators install the drifted preset."""

    def setUp(self) -> None:
        presets_root = ROOT / "src" / "mozyo_bridge" / "scaffold" / "presets"
        self.asana_workflow = (presets_root / "asana" / "agent-workflow.md").read_text(
            encoding="utf-8"
        )
        self.redmine_workflow = (
            presets_root / "redmine" / "agent-workflow.md"
        ).read_text(encoding="utf-8")
        self.router_claude = (presets_root / "_router" / "CLAUDE.md").read_text(
            encoding="utf-8"
        )
        self.router_agents = (presets_root / "_router" / "AGENTS.md").read_text(
            encoding="utf-8"
        )
        self.redmine_rails_workflow = (
            presets_root / "redmine-rails" / "agent-workflow.md"
        ).read_text(encoding="utf-8")

    def test_asana_preset_standard_path_anchors_at_primitive(self) -> None:
        # Standard path bullet must name the high-level primitive.
        self.assertIn("**Standard path (required default)**", self.asana_workflow)
        self.assertIn("`mozyo-bridge handoff send", self.asana_workflow)
        self.assertIn("`mozyo-bridge handoff reply", self.asana_workflow)
        self.assertIn("`mozyo-bridge reply", self.asana_workflow)
        # `notify-*` are compatibility wrappers, not standard-path peers.
        self.assertIn("compatibility", self.asana_workflow)
        # `read` / `message` / `type` / `keys` are explicitly operator/debug.
        self.assertIn("operator/debug primitives", self.asana_workflow)
        # The retired-queue legacy notify wrapper must be cleanup-only.
        self.assertIn("`notify-*-legacy-task`", self.asana_workflow)

    def test_asana_preset_receiver_forbids_status_doctor_scrollback_inference(
        self,
    ) -> None:
        self.assertIn("`mozyo-bridge status`", self.asana_workflow)
        self.assertIn("`mozyo-bridge doctor`", self.asana_workflow)
        self.assertIn("pane scrollback", self.asana_workflow)
        self.assertIn("operator/debug aids", self.asana_workflow)

    def test_redmine_preset_pane_notification_anchors_at_primitive(self) -> None:
        # The Pane Notification section must name the primitive as the
        # standard command and the `notify-*` wrappers as compatibility.
        section_start = self.redmine_workflow.index("## Pane Notification")
        section_end = self.redmine_workflow.index(
            "## Handoff Startup Decision", section_start
        )
        section = self.redmine_workflow[section_start:section_end]
        self.assertIn("`mozyo-bridge handoff send", section)
        self.assertIn("`mozyo-bridge handoff reply", section)
        self.assertIn("`mozyo-bridge reply", section)
        self.assertIn("互換 entrypoint", section)
        self.assertIn("operator/debug primitives", section)
        self.assertNotIn(
            "Standard notification command: `mozyo-bridge notify-* --issue",
            section,
            msg="redmine preset still recommends the old notify-* shell as the standard",
        )
        # The retired-queue wrapper must be tagged cleanup-only here too.
        self.assertIn("retired-queue cleanup wrapper", section)

    def test_redmine_preset_handoff_startup_anchors_at_primitive(self) -> None:
        # The "Standard path" entry of the Handoff Startup Decision must
        # name the primitive, not the legacy notify-* shell.
        section_start = self.redmine_workflow.index("## Handoff Startup Decision")
        section_end = self.redmine_workflow.index(
            "## 実装者 / 監査者境界", section_start
        )
        section = self.redmine_workflow[section_start:section_end]
        # The Standard path bullet must lead with the primitive.
        self.assertIn("**Standard path**", section)
        self.assertIn("`mozyo-bridge handoff send", section)
        # `notify-*` must be described as compatibility, not the standard.
        self.assertIn("compatibility", section)

    def test_redmine_preset_receiver_forbids_status_doctor_scrollback_inference(
        self,
    ) -> None:
        # The Pane Notification section's recipient bullet must explicitly
        # forbid inferring receiver / issue state from status / doctor /
        # scrollback when a durable Redmine anchor exists.
        section_start = self.redmine_workflow.index("## Pane Notification")
        section_end = self.redmine_workflow.index(
            "## Handoff Startup Decision", section_start
        )
        section = self.redmine_workflow[section_start:section_end]
        self.assertIn("`mozyo-bridge status`", section)
        self.assertIn("`mozyo-bridge doctor`", section)
        self.assertIn("pane scrollback", section)
        self.assertIn("operator/debug aids", section)

    def test_shared_router_reminder_stays_thin_and_points_to_preset(self) -> None:
        self.assertIn("${rule_path}", self.router_claude)
        self.assertIn("${ticket_anchor_label}", self.router_claude)
        self.assertIn("handoff startup decision", self.router_claude)
        self.assertIn("operator/debug", self.router_claude)
        self.assertIn("`mozyo-bridge status`", self.router_claude)
        self.assertIn("`mozyo-bridge doctor`", self.router_claude)
        self.assertNotIn("`mozyo-bridge handoff send --to", self.router_claude)

    def test_shared_agents_router_stays_thin(self) -> None:
        self.assertIn("${rule_path}", self.router_agents)
        self.assertIn("${ticket_anchor_label}", self.router_agents)
        self.assertIn("router に本文を複製しない", self.router_agents)
        self.assertIn("operator/debug", self.router_agents)
        self.assertNotIn("Redmine Gate Lifecycle", self.router_agents)
        self.assertNotIn("Audit-Owned Commit Authority", self.router_agents)

    def test_router_templates_are_tool_specific_and_independent(self) -> None:
        """Generated AGENTS.md and CLAUDE.md must be independent tool-specific
        thin routers. CLAUDE.md must not import AGENTS.md (and vice versa) so
        each tool can read its own router as the standalone entry, and so a
        future refactor cannot accidentally restore the old shared-import
        layout where CLAUDE.md depended on AGENTS.md for session-start framing.
        """
        # No cross-import in either direction. The `@AGENTS.md` form is the
        # Claude Code-style file-import directive that previously made
        # CLAUDE.md depend on AGENTS.md for its session-start content.
        self.assertNotIn("@AGENTS.md", self.router_claude)
        self.assertNotIn("@CLAUDE.md", self.router_agents)
        # Each router announces its tool identity in the body so a reader (and
        # an auditor reading the rendered file) can see it stands alone.
        self.assertIn("Codex", self.router_agents)
        self.assertIn("tool-specific", self.router_agents)
        self.assertIn("import しない", self.router_agents)
        self.assertIn("Claude Code", self.router_claude)
        self.assertIn("tool-specific", self.router_claude)
        self.assertIn("import しない", self.router_claude)
        # Each router must independently reach the central preset and the
        # active ticket anchor without referencing the other file.
        for router in (self.router_agents, self.router_claude):
            self.assertIn("${rule_path}", router)
            self.assertIn("${ticket_anchor_label}", router)
        # Marker-bounded preservation must remain on both sides so project-
        # local additions survive re-sync after the tool-specific split.
        for router in (self.router_agents, self.router_claude):
            self.assertIn(
                "<!-- mozyo-bridge:project-local-additions:begin -->", router
            )
            self.assertIn(
                "<!-- mozyo-bridge:project-local-additions:end -->", router
            )

    def test_redmine_rails_preset_layers_on_redmine(self) -> None:
        self.assertIn("rules/presets/redmine/agent-workflow.md", self.redmine_rails_workflow)
        self.assertIn("Rails Design Consultation Triggers", self.redmine_rails_workflow)
        self.assertIn("Data / migration safety", self.redmine_rails_workflow)
        self.assertIn("Hotwire / UI behavior", self.redmine_rails_workflow)
        self.assertNotIn("/myapp/Source/rails", self.redmine_rails_workflow)

    def test_redmine_preset_separates_review_and_owner_close_approval(self) -> None:
        """Review Gate approval and owner close approval are distinct durable
        gates on Redmine projects, ported from Rails commit 8645c4d19.

        The reviewer (audit role / Codex equivalent) records `指摘事項なし` or
        re-review approval on the Review Gate journal, then must record a
        separate journal asking the owner whether close is permitted. The
        implementer must NOT close from review approval alone; it must wait
        for the owner close approval journal. The shared Redmine preset has
        to make all three responsibilities explicit so a future doc refactor
        cannot collapse them back into a single review-and-close gate.
        """
        # The dedicated section must exist.
        self.assertIn("## Close Approval Separation", self.redmine_workflow)

        # Section body must name the three responsibilities distinctly.
        section_start = self.redmine_workflow.index("## Close Approval Separation")
        section_end = self.redmine_workflow.index("## Close Gate Checklist", section_start)
        section = self.redmine_workflow[section_start:section_end]
        # Reviewer side: review approval is not close approval, and reviewer
        # has the post-approval owner-confirmation responsibility.
        self.assertIn("これだけで issue を close してはならない", section)
        self.assertIn("**別 journal**", section)
        self.assertIn("owner にクローズ可否を確認する", section)
        self.assertIn("レビュー結果と owner close approval を 1 journal にまとめない", section)
        # Implementer side: do not advance from review approval alone.
        self.assertIn("Review Gate approval だけで issue を close へ進めない", section)
        self.assertIn("owner の close approval journal を読み", section)
        # Collapsed-roles caveat preserves the record discipline.
        self.assertIn(
            "reviewer と owner を同一人物に collapse している場合でも", section
        )

        # Review Gate bullet must explicitly route the reviewer to the
        # separation section after a no-blockers verdict.
        review_gate = self.redmine_workflow[
            self.redmine_workflow.index("7. **Review Gate**") :
            self.redmine_workflow.index("8. **QA Verification Gate**")
        ]
        self.assertIn("これは close approval ではない", review_gate)
        self.assertIn("owner にクローズ可否を確認する責務", review_gate)
        self.assertIn("Close Approval Separation", review_gate)

        # Close Gate bullet must name owner close approval as separate from
        # Review Gate, not just "owner approval".
        close_gate = self.redmine_workflow[
            self.redmine_workflow.index("10. **Close Gate**") :
            self.redmine_workflow.index("\n\nproject 固有 status / tracker")
        ]
        self.assertIn("owner close approval", close_gate)
        self.assertIn("Review Gate とは別 journal", close_gate)
        self.assertIn(
            "passing Review Gate、owner close approval、commit hash record の三つが揃うまで",
            close_gate,
        )

        # Close Gate Checklist must add a dedicated bullet for the owner
        # close approval journal so a future Close Gate cannot be "passed"
        # against only the Review Gate.
        checklist_start = self.redmine_workflow.index("## Close Gate Checklist")
        checklist_end = self.redmine_workflow.index("## Pane Notification", checklist_start)
        checklist = self.redmine_workflow[checklist_start:checklist_end]
        self.assertIn(
            "**owner close approval** が Review Gate とは別 journal として記録されている",
            checklist,
        )
        self.assertIn("Review Gate approval だけで checklist を満たさない", checklist)

        # Completion section must tell the implementer to wait for the owner
        # close approval journal — review approval alone does not advance to
        # close.
        completion_start = self.redmine_workflow.index("## Completion")
        completion_end = self.redmine_workflow.index("## Audit-Owned Commit Authority", completion_start)
        completion = self.redmine_workflow[completion_start:completion_end]
        self.assertIn(
            "Review Gate approval を owner close approval と読み替えない",
            completion,
        )
        self.assertIn("owner close approval journal が記録されてから close へ進む", completion)

    def test_asana_completion_section_has_no_duplicate_numbered_steps(self) -> None:
        """Asana preset Completion section must be a contiguous 1..N numbered
        list with no duplicate numbers and no duplicate body text. A prior
        generated/rendered output exhibited a duplicated completion
        requirement line, and a regression here would re-introduce the same
        ambiguity for any downstream Asana project.
        """
        section_start = self.asana_workflow.index("## Completion")
        section_end = self.asana_workflow.index("## Audit-Owned Commit Authority", section_start)
        section = self.asana_workflow[section_start:section_end]

        numbered = re.findall(r"^(\d+)\.\s+(.+)$", section, flags=re.MULTILINE)
        self.assertGreaterEqual(
            len(numbered),
            3,
            msg=f"Completion section should contain a numbered list; got {numbered!r}",
        )
        # Contiguous 1..N, no duplicate numeric prefix.
        numbers = [int(num) for num, _ in numbered]
        self.assertEqual(
            numbers,
            list(range(1, len(numbers) + 1)),
            msg=(
                "Completion numbered list must be contiguous 1..N with no "
                f"duplicate numeric prefix; got {numbers!r}"
            ),
        )
        # No body appears twice (catches a duplicated completion requirement
        # line even if it were renumbered to keep the list "contiguous").
        bodies = [body.strip() for _, body in numbered]
        duplicates = [body for body in bodies if bodies.count(body) > 1]
        self.assertEqual(
            duplicates,
            [],
            msg=(
                "Completion section contains duplicate body text: "
                f"{duplicates!r}"
            ),
        )
        # Defensive: no two consecutive bodies are identical (cheaper to
        # catch if a future edit accidentally pastes the same line twice
        # in adjacent steps).
        for i in range(1, len(bodies)):
            self.assertNotEqual(
                bodies[i],
                bodies[i - 1],
                msg=(
                    f"Completion step {i + 1} duplicates the previous step "
                    f"body verbatim: {bodies[i]!r}"
                ),
            )


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
        self.run_cli(["scaffold", "apply", preset, "--target", str(project), "--home", str(home)])
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

    def test_scaffold_status_reports_missing_extended_base_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp), preset="redmine-rails")
            shutil.rmtree(home / "rules" / "presets" / "redmine")

            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(1, result)
            self.assertIn("preset: redmine-rails", output)
            self.assertIn("central status: missing", output)
            self.assertIn("`mozyo-bridge rules install`", output)

    def test_scaffold_refuses_when_extended_base_preset_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            shutil.rmtree(home / "rules" / "presets" / "redmine")

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            "redmine-rails",
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )

            self.assertIn("rules preset is not installed: redmine", stderr.getvalue())

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

        pane = {"id": "%2", "location": "agents:0.1", "command": "node", "cwd": "/repo", "window_name": "codex", "pane_active": "1"}

        # v0.4: the standard notify wrappers default to `--mode queue-enter`,
        # so the Layer B preflight runs. Patch `current_session_name` so the
        # Step 10 same-session binding can compare sender vs target without
        # invoking real tmux.
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value="agents"), \
            patch("mozyo_bridge.application.commands.pane_window_name", return_value="claude"), \
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
        # `notify-codex` is a thin wrapper over the new handoff primitive
        # (Codex audit: `1214760803593547`). The marker and body shape come
        # from `mozyo_bridge.domain.handoff`; the legacy `[mozyo:notify:...]`
        # marker is reserved for the legacy queue subcommands. v0.4: the
        # standard notify wrappers default to `--mode queue-enter`, so the
        # Layer B preflight admits the send under the fake's codex pane.
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
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn(
            "[mozyo:handoff:source=redmine:issue=9020:journal=46005:kind=review_request:to=codex]",
            pane_text,
        )
        self.assertIn("review request ready for codex", pane_text)
        self.assertIn("Redmine #9020 journal #46005", pane_text)
        outcome_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(outcome_lines)
        outcome = json.loads(outcome_lines[-1])
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("redmine", outcome["source"])
        self.assertEqual("review_request", outcome["kind"])
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

    def test_legacy_task_notification_does_not_emit_structured_outcome(self) -> None:
        # Regression rail for the handoff-primitive split (Asana 1214760806178471):
        # `notify-*-legacy-task` is the retired-queue cleanup wrapper and must
        # NOT route through `orchestrate_handoff`. It therefore must not emit
        # the structured JSON outcome line nor the markdown delivery record,
        # because callers of the legacy wrapper have no durable Asana / Redmine
        # anchor to anchor that record at. If a future refactor accidentally
        # unifies the legacy queue path with the standard primitive, callers
        # would start seeing structured-outcome bytes that name a stale queue
        # task as the anchor.
        task = {"id": "legacy-task", "issue_id": 9596, "commit": "abc123", "type": "design_consultation_result"}
        with patch("mozyo_bridge.application.commands.find_handoff_task", return_value=task):
            result, _sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
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
        outcome_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertEqual([], outcome_lines, msg=f"legacy wrapper emitted structured outcome: {stdout!r}")
        self.assertNotIn("Delivery result —", stdout)
        self.assertNotIn("Durable anchor:", stdout)
        self.assertNotIn("`receiver`", stdout)

    def test_notify_submits_under_queue_enter_default_even_when_marker_missed(
        self,
    ) -> None:
        # v0.4 contract pivot (Asana 1214824751741628): the standard notify
        # wrappers default to `--mode queue-enter`, so marker miss must NOT
        # roll back — Enter is issued and the durable outcome is `sent` /
        # `queue_enter`. Strict-rail rollback on marker miss is still covered
        # by `RelaxedQueueEnterRailTest.test_strict_standard_still_rolls_back_on_marker_timeout`;
        # notify-* wrappers cannot opt into strict by design (no `--mode` flag
        # is exposed on them).
        result, sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
            [
                "notify-codex",
                "--issue",
                "9020",
                "--journal",
                "46005",
                "--target",
                "%2",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            captures=["", "", ""],
        )

        self.assertEqual(0, result)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))
        outcome_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(outcome_lines)
        outcome = json.loads(outcome_lines[-1])
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("queue_enter", outcome["reason"])
        self.assertEqual("queue-enter", outcome["mode"])
        self.assertEqual("redmine", outcome["source"])

    def test_notify_submit_delay_default_is_classic_short_tui_delay(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["notify-codex", "--issue", "9020", "--journal", "1"])

        self.assertEqual(0.2, args.submit_delay)

    def test_standard_notify_wrapper_preserves_legacy_success_line(self) -> None:
        # Codex audit finding 1 on task 1214760547941073: the wrapper must
        # keep printing `notified <agent>: journal=... target=... read_lines=...`
        # so the in-repo smoke and external scripts that grep that line
        # continue to work after the handoff-primitive retrofit. v0.4: the
        # notify wrappers default to queue-enter; the legacy success line is
        # still printed when the Layer B preflight admits and Enter is sent.
        result, _sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
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
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn("notified codex: journal=46005 target=%2 read_lines=20", stdout)

    def test_standard_notify_wrapper_omits_success_line_on_failure(self) -> None:
        # The legacy success line is a courtesy that must only fire on real
        # success. v0.4 routes the standard notify wrappers through
        # `--mode queue-enter`, which rejects `--force` before any typing;
        # the wrapper must not have printed `notified codex: ...` before the
        # forced exit. Pre-v0.4 this test exercised the strict-rail
        # marker_timeout path; the success-line invariant is the same under
        # any wrapper failure, so we keep the regression on the new path.
        with contextlib.redirect_stderr(io.StringIO()):
            result, _sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
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
        self.assertNotIn("notified codex:", stdout)

    def test_standard_notify_accepts_record_format_json(self) -> None:
        # Codex audit finding 2 on task 1214760547941073: the wrapper parser
        # must accept the same --record-format / --record-command knobs as
        # `handoff send/reply`, so callers using the compatibility wrapper
        # can still ask for json-only output.
        parser = build_parser()

        args = parser.parse_args(
            [
                "notify-codex",
                "--issue",
                "9020",
                "--journal",
                "1",
                "--record-format",
                "json",
            ]
        )

        self.assertEqual("json", args.record_format)

    def test_standard_notify_record_format_json_suppresses_record(self) -> None:
        # End-to-end through the wrapper: --record-format json suppresses
        # the markdown block but keeps the JSON outcome and the legacy
        # success line. v0.4: notify wrappers default to queue-enter; the
        # fake fixture passes Layer B preflight so Enter is issued and the
        # legacy success line still fires.
        result, _sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
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
                "--submit-delay",
                "0",
                "--record-format",
                "json",
            ]
        )

        self.assertEqual(0, result)
        self.assertNotIn("Delivery result —", stdout)
        self.assertIn("notified codex: journal=46005", stdout)
        json_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertEqual(1, len(json_lines))

    def test_notify_review_wrapper_accepts_record_command(self) -> None:
        # --record-command flows through the review wrappers too (issue
        # required path). End-to-end: the record block shows the literal
        # command and the legacy success line still fires. v0.4: review
        # wrappers default to queue-enter; the fake fixture passes Layer B
        # preflight.
        result, _sent, stdout, _pane_text = self.run_notify_with_fake_tmux(
            [
                "notify-codex-review",
                "--issue",
                "9020",
                "--journal",
                "46005",
                "--target",
                "%2",
                "--submit-delay",
                "0",
                "--record-command",
                "mozyo-bridge notify-codex-review --issue 9020 --journal 46005",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn(
            "- Command: `mozyo-bridge notify-codex-review --issue 9020 --journal 46005`",
            stdout,
        )
        self.assertIn("notified codex: journal=46005", stdout)


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
            patch("mozyo_bridge.application.commands.pane_window_name", return_value="codex"), \
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

    def test_message_submits_enter_when_marker_wraps_in_capture(self) -> None:
        # Receiver TUI (codex / claude code) word-wraps long input at the
        # visible pane width, inserting a literal newline + continuation
        # indent inside the marker. capture-pane -J cannot rejoin these
        # (the wrap is TUI-emitted, not tmux-display-wrap), so a raw
        # `marker in capture` search misses it. The landing gate must
        # still observe the marker and proceed to Enter.
        wrapped_capture = (
            "› [mozyo-bridge from:codex pane:%1\n"
            "  at:agents:0.0] hello body\n"
        )
        result, sent, _pane_text, _sleep = self.run_message_with_fake_tmux(
            ["message", "%2", "hello body", "--submit-delay", "0"],
            captures=[wrapped_capture],
        )

        self.assertEqual(0, result)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

    def test_message_still_rolls_back_when_capture_lacks_marker(self) -> None:
        # Safety lock: even after wrap-tolerant normalization, a capture
        # that does not actually contain the marker must trigger C-u
        # rollback and skip Enter. Fail-closed is non-negotiable.
        unrelated_capture = "› unrelated placeholder\n  with continuation indent\n"
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
                captures=[unrelated_capture, unrelated_capture, unrelated_capture],
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "Enter") for call in sent))
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)


class MessageGateGuidanceTest(unittest.TestCase):
    """Regression coverage for Asana task 1214779823377861.

    The CLI must emit a structured stderr trailer after `mozyo-bridge message`
    read-marker / marker-observation gate failures so agents see the literal
    retry path and the per-preset `--no-submit` retry budget. Without this
    trailer, agents have been observed conflating the `--no-submit` budget
    with the `handoff send` retry pool and jumping straight to the preset's
    `Notification fails` branch after a single transient failure (see Asana
    task 1214774670696760 comment 1214778979254677 for the failure example).
    """

    def _run_message_with_gate_failure(
        self,
        argv: list[str],
        *,
        require_read_side_effect=None,
        suppress_marker_in_capture: bool = False,
    ):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []
        pane_text = ""

        def fake_capture(_target: str, _lines: int) -> str:
            # When the test exercises the wait_for_text rollback path, the
            # capture must not echo the typed marker even though
            # `fake_run_tmux` accumulates it into `pane_text`. Otherwise the
            # gate would observe the marker (because the marker is in
            # `pane_text`) and the rollback branch never fires.
            if suppress_marker_in_capture:
                return ""
            return pane_text

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                pane_text += tmux_args[-1]
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        require_read_patch = (
            patch(
                "mozyo_bridge.application.commands.require_read",
                side_effect=require_read_side_effect,
            )
            if require_read_side_effect is not None
            else patch("mozyo_bridge.application.commands.require_read")
        )

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            require_read_patch, \
            patch("mozyo_bridge.application.commands.clear_read"), \
            patch("mozyo_bridge.application.commands.resolve_target", return_value="%2"), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.pane_window_name", return_value="codex"), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.0"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            try:
                result = args.func(args)
            except SystemExit as exc:
                result = exc

        return result, sent, stderr.getvalue()

    def test_no_submit_read_marker_failure_emits_retry_path_and_budget(self) -> None:
        # require_read dies with the literal next-action verb ("read target
        # again before interacting"). The CLI must augment stderr with an
        # explicit retry path and the per-preset --no-submit budget so the
        # agent does not need to pattern-match from memory (failure mode #1 in
        # the task body).
        result, _sent, stderr = self._run_message_with_gate_failure(
            ["message", "%2", "pending body", "--no-submit"],
            require_read_side_effect=SystemExit(2),
        )

        self.assertIsInstance(result, SystemExit)
        self.assertIn("hint: retry path:", stderr)
        self.assertIn("mozyo-bridge read %2", stderr)
        self.assertIn("--no-submit retry budget", stderr)
        # The base preset cap is 3; if this assertion ever fails, double-check
        # NO_SUBMIT_RETRY_BUDGET in domain/handoff.py and update the preset
        # `Notification fails` branch in lockstep.
        self.assertIn("3", stderr)
        self.assertIn(
            "handoff send",
            stderr,
            "stderr must name the `handoff send` pool to prevent budget conflation (failure mode #2 in task 1214779823377861)",
        )

    def test_no_submit_read_marker_failure_with_attempt_reports_remaining(
        self,
    ) -> None:
        # --attempt N parameterizes the budget reporting so the agent knows
        # exactly how many --no-submit retries remain. Operator-tracked
        # because the CLI is stateless across invocations.
        result, _sent, stderr = self._run_message_with_gate_failure(
            [
                "message",
                "%2",
                "pending body",
                "--no-submit",
                "--attempt",
                "2",
            ],
            require_read_side_effect=SystemExit(2),
        )

        self.assertIsInstance(result, SystemExit)
        self.assertIn("attempt 2/3 just failed", stderr)
        self.assertIn("1/3 attempts remaining", stderr)

    def test_submit_marker_timeout_still_rolls_back_and_emits_guidance(
        self,
    ) -> None:
        # Safety-gate regression: the existing fail-closed contract (no Enter
        # when marker is not observed; C-u rollback) must remain intact, and
        # the new guidance trailer must fire alongside it. This is the test
        # for failure mode #3 in the task body ("Notification fails" used as
        # escape hatch after transient failure).
        result, sent, stderr = self._run_message_with_gate_failure(
            [
                "message",
                "%2",
                "lost body",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            suppress_marker_in_capture=True,
        )

        self.assertIsInstance(result, SystemExit)
        # Fail-closed contract: Enter not pressed, C-u issued.
        self.assertFalse(
            any(call == ("send-keys", "-t", "%2", "Enter") for call in sent)
        )
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)
        # New trailer present.
        self.assertIn("hint: retry path:", stderr)
        self.assertIn("mozyo-bridge read %2", stderr)
        # In default (submit) mode no --no-submit budget trailer is emitted —
        # `--no-submit` was not requested. The retry-path line is enough; the
        # budget line is gated on --no-submit so we do not over-promise a
        # budget that does not apply here.
        self.assertNotIn("--no-submit retry budget:", stderr)

    def test_no_submit_message_happy_path_emits_no_gate_guidance(self) -> None:
        # Anti-regression: the trailer must NOT fire when require_read
        # succeeds. The happy path must remain silent on stderr.
        parser = build_parser()
        args = parser.parse_args(["message", "%2", "ok body", "--no-submit"])
        sent: list[tuple[str, ...]] = []

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.require_read"), \
            patch("mozyo_bridge.application.commands.clear_read"), \
            patch("mozyo_bridge.application.commands.resolve_target", return_value="%2"), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch("mozyo_bridge.application.commands.pane_window_name", return_value="codex"), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:0.0"), \
            patch("mozyo_bridge.application.commands.capture_pane", return_value=""), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = args.func(args)

        self.assertEqual(0, result)
        self.assertNotIn("hint: retry path:", stderr.getvalue())
        self.assertNotIn("--no-submit retry budget", stderr.getvalue())


class WaitForTextContractTest(unittest.TestCase):
    def test_detects_marker_split_by_tui_wrap(self) -> None:
        """word-boundary wrap compat: short marker that contains whitespace,
        wrapped at a space and re-joined via the `\\n\\s+` -> ' ' normalize.
        Covers the original `mozyo-bridge message` marker shape."""
        from mozyo_bridge.application import commands as commands_mod

        marker = "[mozyo-bridge from:codex pane:%1 at:agents:0.0]"
        wrapped = (
            "› [mozyo-bridge from:codex pane:%1\n"
            "  at:agents:0.0] hello body\n"
        )
        with patch.object(commands_mod, "capture_pane", return_value=wrapped), \
                patch.object(commands_mod.time, "sleep"):
            self.assertTrue(commands_mod.wait_for_text("%2", marker, 200, 0.01))

    def test_detects_long_no_space_marker_split_by_character_wrap(self) -> None:
        """character-wrap compat: long no-space marker (`mozyo-bridge handoff`
        primitive shape) wrapped at arbitrary character boundaries by codex
        TUI, re-joined via the `\\n\\s+` -> '' normalize. Reproducer hex shape
        was confirmed against the real codex pane at 2026-05-13 (pane width
        50, wrap separator `\\n` + 3 spaces). The space-substitution path
        cannot match this shape because the original marker contains no
        whitespace; only empty-substitution reconstructs the original."""
        from mozyo_bridge.application import commands as commands_mod

        marker = (
            "[mozyo:handoff:source=asana:task=1214760547941073:"
            "comment=1214764579019987:kind=review_request:to=codex]"
        )
        wrapped = (
            "› [mozyo:handoff:source=asana:task=12147605479410\n"
            "   73:comment=1214764579019987:kind=review_request\n"
            "   :to=codex]\n"
        )
        with patch.object(commands_mod, "capture_pane", return_value=wrapped), \
                patch.object(commands_mod.time, "sleep"):
            self.assertTrue(commands_mod.wait_for_text("%2", marker, 200, 0.01))

    def test_returns_false_when_marker_genuinely_absent(self) -> None:
        """fail-closed maintained: when the marker is not present in any of
        raw / space-normalized / empty-normalized captures, the function
        returns False so the rollback contract still triggers."""
        from mozyo_bridge.application import commands as commands_mod

        marker = "[mozyo-bridge from:codex pane:%1 at:agents:0.0]"
        unrelated = "› unrelated pane\n  with indent\n"
        with patch.object(commands_mod, "capture_pane", return_value=unrelated), \
                patch.object(commands_mod.time, "sleep"):
            self.assertFalse(commands_mod.wait_for_text("%2", marker, 200, 0.01))

    def test_returns_false_when_long_handoff_marker_genuinely_absent(self) -> None:
        """fail-closed maintained on the new character-wrap path: empty-string
        substitution must not create accidental matches against unrelated
        wrapped content that just happens to share substrings around indent
        boundaries."""
        from mozyo_bridge.application import commands as commands_mod

        marker = (
            "[mozyo:handoff:source=asana:task=1214760547941073:"
            "comment=1214764579019987:kind=review_request:to=codex]"
        )
        unrelated_wrapped = (
            "› [mozyo:handoff:source=asana:task=99999999999999\n"
            "   99:comment=88888888888888:kind=reply:to=claude]\n"
        )
        with patch.object(commands_mod, "capture_pane", return_value=unrelated_wrapped), \
                patch.object(commands_mod.time, "sleep"):
            self.assertFalse(commands_mod.wait_for_text("%2", marker, 200, 0.01))

    def test_matches_raw_unwrapped_marker_unchanged(self) -> None:
        """raw substring fast-path: when the marker is present without wrap,
        the function returns True via the raw `in` check before normalization
        runs, preserving the cheapest match path."""
        from mozyo_bridge.application import commands as commands_mod

        marker = "[mozyo-bridge from:codex pane:%1 at:agents:0.0]"
        captured = f"some scrollback\n› {marker} body text\n"
        with patch.object(commands_mod, "capture_pane", return_value=captured), \
                patch.object(commands_mod.time, "sleep"):
            self.assertTrue(commands_mod.wait_for_text("%2", marker, 200, 0.01))


class AgentWindowSubtleStyleTest(unittest.TestCase):
    """Subtle per-window status-bar styling for agent windows.

    Asana task 1214949940121288. Window names must stay exactly `claude` /
    `codex` (resolver / notification routing keys), so identification is
    done via window-scoped tmux options rather than name changes. Colors
    are intentionally muted (256-color palette fg-only, no background fill,
    no blink, no glyphs).
    """

    def test_color_table_only_covers_known_agents_and_uses_muted_palette(self) -> None:
        # Locks the contract: only `claude` / `codex` get tinted. Anything
        # else stays at the user's global window-status-style so unrelated
        # windows in the session do not get touched.
        self.assertEqual({"claude", "codex"}, set(AGENT_WINDOW_STATUS_COLORS))
        # Restrained palette: 256-color foreground numbers only. No `#RRGGBB`
        # bright hues, no `,bg=...`, no flashing attributes.
        for window_name, color in AGENT_WINDOW_STATUS_COLORS.items():
            with self.subTest(window=window_name):
                self.assertRegex(color, r"^colour\d{1,3}$", f"{window_name} color must be a 256-palette `colourN`")

    def test_apply_window_subtle_style_emits_window_scoped_options_for_claude(self) -> None:
        calls: list[tuple] = []

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            calls.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux):
            self.assertTrue(apply_window_subtle_style("my-project", "claude"))

        # Window-scoped target (`session:window_name`), not session-scoped.
        # The user's global `set -g window-status-style` from .tmux.conf
        # stays in effect for every other window.
        self.assertEqual(
            [
                ("set-window-option", "-t", "my-project:claude", "window-status-style", "fg=colour108"),
                ("set-window-option", "-t", "my-project:claude", "window-status-current-style", "fg=colour108,bold"),
            ],
            calls,
        )

    def test_apply_window_subtle_style_emits_window_scoped_options_for_codex(self) -> None:
        calls: list[tuple] = []

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            calls.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux):
            self.assertTrue(apply_window_subtle_style("my-project", "codex"))

        self.assertEqual(
            [
                ("set-window-option", "-t", "my-project:codex", "window-status-style", "fg=colour67"),
                ("set-window-option", "-t", "my-project:codex", "window-status-current-style", "fg=colour67,bold"),
            ],
            calls,
        )

    def test_apply_window_subtle_style_is_noop_for_unknown_window_name(self) -> None:
        # Legacy / operator-owned windows are not retinted, even if they
        # share the agent session. The task's prohibition on "派手な配色"
        # would otherwise be at risk if a future caller ran this helper
        # over every window in the session.
        calls: list[tuple] = []

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            calls.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux):
            self.assertFalse(apply_window_subtle_style("my-project", "zsh"))
            self.assertFalse(apply_window_subtle_style("my-project", "shell"))

        self.assertEqual([], calls)

    def test_ensure_repo_session_windows_applies_subtle_style_to_both_agents(self) -> None:
        # Wiring assertion: bare-`mozyo` startup tints both agent windows
        # exactly once, regardless of whether the windows existed already.
        args = argparse.Namespace(
            session="my-project",
            cwd="/repo",
            config=False,
            ready_timeout=0,
            force=False,
        )
        claude_pane = {"id": "%1", "command": "claude", "window_name": "claude"}
        codex_pane = {"id": "%2", "command": "node", "window_name": "codex"}
        style_calls: list[tuple] = []

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
            patch("mozyo_bridge.application.commands.new_agent_session_window") as new_session_window, \
            patch("mozyo_bridge.application.commands.new_agent_window") as new_window, \
            patch("mozyo_bridge.application.commands.list_session_windows", return_value=["claude", "codex"]), \
            patch(
                "mozyo_bridge.application.commands.find_agent_window",
                side_effect=[claude_pane, codex_pane],
            ), \
            patch("mozyo_bridge.application.commands.ensure_agent_target"), \
            patch(
                "mozyo_bridge.application.commands.apply_window_subtle_style",
                side_effect=lambda session, name: style_calls.append((session, name)) or True,
            ):
            ensure_repo_session_windows(args)

        new_session_window.assert_not_called()
        new_window.assert_not_called()
        # One tint call per agent window, in claude-then-codex order so the
        # default-window flip in bare-`mozyo` lands on the tinted claude
        # window.
        self.assertEqual([("my-project", "claude"), ("my-project", "codex")], style_calls)

    def test_cmd_init_applies_subtle_style_after_rename(self) -> None:
        # `cmd_init` is the second entry point that promotes a pane into the
        # agent window rail. It must apply the same subtle style so panes
        # brought in via `init` look identical to panes created via
        # bare-`mozyo` in the status bar.
        args = argparse.Namespace(agent="claude", target="%5")
        panes = [
            {"id": "%2", "location": "agents:0.0", "command": "zsh", "window_name": "zsh", "cwd": "/repo"},
            {"id": "%5", "location": "agents:1.0", "command": "zsh", "window_name": "zsh", "cwd": "/repo"},
        ]
        style_calls: list[tuple] = []

        def fake_run_tmux(*tmux_args, **_):
            if tmux_args[:1] == ("display-message",):
                return argparse.Namespace(returncode=0, stdout="%5\n", stderr="")
            if tmux_args[:1] == ("set-window-option",):
                style_calls.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux args: {tmux_args}")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:1.0"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            patch("mozyo_bridge.application.commands.rename_window"), \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd_init(args))

        # Two style options per init (window-status-style and current-style)
        # targeting the renamed claude window in the right session.
        self.assertEqual(2, len(style_calls))
        for call in style_calls:
            self.assertEqual(("set-window-option", "-t", "agents:claude"), call[:3])


class CommandTest(unittest.TestCase):
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
        pane = {"id": "%2", "command": "node"}

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
        pane = {"id": "%2", "command": "node"}

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
        pane = {"id": "%2", "command": "node"}

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
        claude_pane = {"id": "%1", "command": "claude", "window_name": "claude"}
        codex_pane = {"id": "%2", "command": "node", "window_name": "codex"}

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", side_effect=[False, False]), \
            patch("mozyo_bridge.application.commands.new_agent_session_window", return_value="%1") as new_session_window, \
            patch("mozyo_bridge.application.commands.source_tmux_conf") as source_conf, \
            patch("mozyo_bridge.application.commands.list_session_windows", return_value=["claude"]), \
            patch(
                "mozyo_bridge.application.commands.find_agent_window",
                side_effect=[claude_pane, codex_pane],
            ) as find_agent, \
            patch("mozyo_bridge.application.commands.new_agent_window", return_value="%2") as new_window, \
            patch("mozyo_bridge.application.commands.ensure_agent_target"):
            created = ensure_repo_session_windows(args)

        new_session_window.assert_called_once_with("claude", "my-project", cwd="/repo")
        new_window.assert_called_once_with("codex", "my-project", cwd="/repo")
        source_conf.assert_called_once_with("/repo/.tmux.conf", optional=False)
        self.assertEqual(["claude:%1", "codex:%2"], created)
        self.assertEqual(
            [("claude", "my-project"), ("codex", "my-project")],
            [(call.args[0], call.args[1]) for call in find_agent.call_args_list],
        )

    def test_ensure_repo_session_windows_skips_creation_when_windows_exist(self) -> None:
        args = argparse.Namespace(
            session="my-project",
            cwd="/repo",
            config=False,
            ready_timeout=0,
            force=False,
        )
        claude_pane = {"id": "%1", "command": "claude", "window_name": "claude"}
        codex_pane = {"id": "%2", "command": "node", "window_name": "codex"}

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
            patch("mozyo_bridge.application.commands.new_agent_session_window") as new_session_window, \
            patch("mozyo_bridge.application.commands.new_agent_window") as new_window, \
            patch("mozyo_bridge.application.commands.list_session_windows", return_value=["claude", "codex"]), \
            patch(
                "mozyo_bridge.application.commands.find_agent_window",
                side_effect=[claude_pane, codex_pane],
            ) as find_agent, \
            patch("mozyo_bridge.application.commands.ensure_agent_target"):
            created = ensure_repo_session_windows(args)

        new_session_window.assert_not_called()
        new_window.assert_not_called()
        self.assertEqual([], created)
        # Window-model resolution: post-existence attach must consult the
        # named window. (The `@agent_name` label path is retired.)
        self.assertEqual(2, find_agent.call_count)
        self.assertEqual(("claude", "my-project"), find_agent.call_args_list[0].args)
        self.assertEqual(("codex", "my-project"), find_agent.call_args_list[1].args)

    def test_ensure_repo_session_windows_creates_missing_agent_windows_alongside_legacy_windows(self) -> None:
        # Sessions that pre-exist with non-agent windows (e.g. a VS Code
        # pane-split session named `agents`) are no longer rejected — bare
        # `mozyo` just appends the missing agent windows. The operator can
        # decide what to do with the leftover non-agent windows.
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
            patch(
                "mozyo_bridge.application.commands.new_agent_window",
                side_effect=["%2", "%3"],
            ) as new_window, \
            patch(
                "mozyo_bridge.application.commands.list_session_windows",
                return_value=["agents"],
            ), \
            patch(
                "mozyo_bridge.application.commands.find_agent_window",
                return_value=None,
            ), \
            patch("mozyo_bridge.application.commands.ensure_agent_target"):
            created = ensure_repo_session_windows(args)

        new_session_window.assert_not_called()
        self.assertEqual(2, new_window.call_count)
        self.assertEqual(("claude", "my-project"), new_window.call_args_list[0].args)
        self.assertEqual(("codex", "my-project"), new_window.call_args_list[1].args)
        self.assertEqual(["claude:%2", "codex:%3"], created)

    def test_ensure_repo_session_windows_skips_label_attachment_when_pane_lookup_returns_none(self) -> None:
        args = argparse.Namespace(
            session="my-project",
            cwd="/repo",
            config=False,
            ready_timeout=5.0,
            force=False,
        )

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
            patch("mozyo_bridge.application.commands.new_agent_session_window") as new_session_window, \
            patch("mozyo_bridge.application.commands.new_agent_window") as new_window, \
            patch("mozyo_bridge.application.commands.list_session_windows", return_value=["claude", "codex"]), \
            patch(
                "mozyo_bridge.application.commands.find_agent_window",
                return_value=None,
            ) as find_agent, \
            patch("mozyo_bridge.application.commands.ensure_agent_target") as ensure_target, \
            patch("mozyo_bridge.application.commands.wait_for_agent_terminal_pane") as wait_ready:
            created = ensure_repo_session_windows(args)

        new_session_window.assert_not_called()
        new_window.assert_not_called()
        ensure_target.assert_not_called()
        wait_ready.assert_not_called()
        self.assertEqual(2, find_agent.call_count)
        self.assertEqual([], created)

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
        # The disambiguation hint must point operators at the bare-`mozyo`
        # `--session` flag, not at the retired `open-here` subcommand.
        self.assertIn("--session", stderr.getvalue())
        self.assertNotIn("open-here", stderr.getvalue())

    def test_cmd_mozyo_explicit_session_skips_cwd_mismatch_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            args = argparse.Namespace(
                repo=str(repo),
                session="explicit-session",
                cwd=None,
                config_path=None,
                ready_timeout=0,
                force=False,
                no_attach=True,
            )
            list_result = argparse.Namespace(returncode=0, stdout="", stderr="")
            captured: dict[str, argparse.Namespace] = {}

            def fake_ensure(inner: argparse.Namespace) -> list[str]:
                captured["args"] = inner
                return []

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
                patch("mozyo_bridge.application.commands.session_cwd_mismatch") as cwd_check, \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows", side_effect=fake_ensure), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.commands.os.execvp", side_effect=AssertionError("must not attach")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_mozyo(args))

        cwd_check.assert_not_called()
        self.assertEqual("explicit-session", captured["args"].session)
        self.assertEqual(str(repo), captured["args"].cwd)
        self.assertIn("attach: tmux attach -t explicit-session", stdout.getvalue())

    def test_cmd_mozyo_propagates_explicit_config_path_with_was_default_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            custom_conf = (Path(tmp) / "custom.tmux.conf").resolve()
            custom_conf.write_text("")
            args = argparse.Namespace(
                repo=str(repo),
                session=None,
                cwd=None,
                config_path=str(custom_conf),
                ready_timeout=0,
                force=False,
                no_attach=True,
            )
            list_result = argparse.Namespace(returncode=0, stdout="", stderr="")
            captured: dict[str, argparse.Namespace] = {}

            def fake_ensure(inner: argparse.Namespace) -> list[str]:
                captured["args"] = inner
                return []

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows", side_effect=fake_ensure), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.commands.os.execvp", side_effect=AssertionError("must not attach")), \
                contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cmd_mozyo(args))

        self.assertEqual(str(custom_conf), captured["args"].config_path)
        self.assertFalse(captured["args"].config_path_was_default)

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

    def test_session_cwd_mismatch_returns_empty_when_no_panes_in_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            panes = [{"id": "%1", "location": "elsewhere:0.0", "command": "zsh", "cwd": "/tmp"}]
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
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%2",
                    "location": "agents:1.0",
                    "command": "node",
                    "label": "codex",
                    "cwd": str(repo),
                    "window_name": "codex",
                    "pane_active": "1",
                },
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
                {
                    "id": "%1",
                    "location": "agents:0.0",
                    "command": "2.1.138",
                    "label": "claude",
                    "cwd": str(repo),
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%2",
                    "location": "agents:1.0",
                    "command": "node",
                    "label": "codex",
                    "cwd": str(repo),
                    "window_name": "codex",
                    "pane_active": "1",
                },
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

        output = stdout.getvalue()
        self.assertIn("claude_window: %1", output)
        self.assertIn("process=2.1.138", output)
        self.assertIn("status=ok", output)

    def test_doctor_does_not_flag_cross_session_labeled_panes_as_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            args = argparse.Namespace(repo=str(repo), queue=str(repo / ".agent_handoff" / "tasks.json"))
            panes = [
                {
                    "id": "%1",
                    "location": "mozyo_bridge:0.0",
                    "command": "2.1.138",
                    "label": "claude",
                    "cwd": str(repo),
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%2",
                    "location": "mozyo_bridge:1.0",
                    "command": "node",
                    "label": "codex",
                    "cwd": str(repo),
                    "window_name": "codex",
                    "pane_active": "1",
                },
                {
                    "id": "%3",
                    "location": "other_project:0.0",
                    "command": "2.1.138",
                    "label": "claude",
                    "cwd": str(repo),
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%4",
                    "location": "other_project:1.0",
                    "command": "node",
                    "label": "codex",
                    "cwd": str(repo),
                    "window_name": "codex",
                    "pane_active": "1",
                },
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
        self.assertIn("claude_window: %1", output)
        self.assertIn("session=mozyo_bridge", output)
        self.assertIn("codex_window: %2", output)
        # Cross-session windows (other_project) are still listed in pane_lines
        # but no longer flagged anywhere — the window-only resolver scopes by
        # current session, so cross-session panes are inert.
        self.assertNotIn("duplicate", output)
        self.assertNotIn("other_sessions", output)

    def test_doctor_flags_missing_agent_window_as_warning(self) -> None:
        # Pure pane-split sessions (no agent-named windows) are no longer
        # supported as a runtime compatibility path. Doctor flips to
        # `warning` and prints a `next_action` suggesting bare `mozyo` or
        # `mozyo-bridge init <agent>` so the operator can recover.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            args = argparse.Namespace(repo=str(repo), queue=str(repo / ".agent_handoff" / "tasks.json"))
            panes = [
                {
                    "id": "%1",
                    "location": "agents:0.0",
                    "command": "claude",
                    "cwd": str(repo),
                    "window_name": "tmux:0",
                    "pane_active": "1",
                },
                {
                    "id": "%2",
                    "location": "agents:1.0",
                    "command": "node",
                    "cwd": str(repo),
                    "window_name": "tmux:1",
                    "pane_active": "1",
                },
            ]
            list_result = argparse.Namespace(returncode=0, stdout="%1\n%2\n", stderr="")
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
                self.assertEqual(1, cmd_doctor(args))

        output = stdout.getvalue()
        self.assertIn("tmux: warning", output)
        self.assertIn("claude_window: missing session=agents", output)
        self.assertIn("codex_window: missing session=agents", output)
        self.assertIn("`mozyo`", output)
        self.assertIn("mozyo-bridge init claude", output)
        self.assertNotIn("(compat)", output)
        self.assertNotIn("legacy_pane_split", output)

    def test_doctor_flips_to_warning_on_duplicate_agent_window(self) -> None:
        # Resolver fails closed on a session with two windows named
        # `claude`. Doctor surfaces this as a `warning` with the window
        # indexes the operator must resolve.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            args = argparse.Namespace(repo=str(repo), queue=str(repo / ".agent_handoff" / "tasks.json"))
            panes = [
                {
                    "id": "%1",
                    "location": "mozyo_bridge:0.0",
                    "command": "claude",
                    "cwd": str(repo),
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%2",
                    "location": "mozyo_bridge:3.0",
                    "command": "claude",
                    "cwd": str(repo),
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%3",
                    "location": "mozyo_bridge:1.0",
                    "command": "node",
                    "cwd": str(repo),
                    "window_name": "codex",
                    "pane_active": "1",
                },
            ]
            list_result = argparse.Namespace(returncode=0, stdout="%1\n%2\n%3\n", stderr="")
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
                self.assertEqual(1, cmd_doctor(args))

        output = stdout.getvalue()
        self.assertIn("tmux: warning", output)
        self.assertIn("claude_window: duplicate session=mozyo_bridge windows=0,3", output)
        self.assertIn("resolve duplicate `claude` windows", output)

    def test_doctor_reports_unscoped_when_invoked_outside_tmux_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            args = argparse.Namespace(repo=str(repo), queue=str(repo / ".agent_handoff" / "tasks.json"))
            panes = [
                {
                    "id": "%1",
                    "location": "mozyo_bridge:0.0",
                    "command": "2.1.138",
                    "cwd": str(repo),
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%2",
                    "location": "mozyo_bridge:1.0",
                    "command": "node",
                    "cwd": str(repo),
                    "window_name": "codex",
                    "pane_active": "1",
                },
            ]
            list_result = argparse.Namespace(returncode=0, stdout="%1\n%2\n", stderr="")
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
        self.assertIn("claude_window: unscoped", output)
        self.assertIn("codex_window: unscoped", output)
        self.assertNotIn("(compat)", output)
        self.assertNotIn("duplicate", output)

    def _init_run_tmux_side_effect(
        self,
        target_pane_id: str,
        *,
        rename_observer: list | None = None,
        style_observer: list | None = None,
    ):
        # display-message resolves the pane reference to its canonical id.
        # rename-window is the rename mutation init makes.
        # set-window-option is the subtle window-status-style applied to the
        # newly-renamed agent window so the tmux status bar entry for that
        # window is colored without changing the window name.
        def side_effect(*tmux_args, **_):
            if tmux_args[:1] == ("display-message",):
                return argparse.Namespace(returncode=0, stdout=f"{target_pane_id}\n", stderr="")
            if tmux_args[:1] == ("rename-window",):
                if rename_observer is not None:
                    rename_observer.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            if tmux_args[:1] == ("set-window-option",):
                if style_observer is not None:
                    style_observer.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux args: {tmux_args}")
        return side_effect

    def test_cmd_init_renames_target_window_to_agent_name(self) -> None:
        args = argparse.Namespace(agent="claude", target="%5")
        panes = [
            {"id": "%2", "location": "agents:0.0", "command": "zsh", "window_name": "zsh", "cwd": "/repo"},
            {"id": "%5", "location": "agents:1.0", "command": "zsh", "window_name": "zsh", "cwd": "/repo"},
        ]
        rename_calls: list[tuple] = []

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.application.commands.run_tmux",
                side_effect=self._init_run_tmux_side_effect("%5"),
            ), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:1.0"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            patch(
                "mozyo_bridge.application.commands.rename_window",
                side_effect=lambda target, name: rename_calls.append((target, name)),
            ), \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, cmd_init(args))

        self.assertEqual([("agents:1", "claude")], rename_calls)
        self.assertIn("agents:1 -> claude", stdout.getvalue())

    def test_cmd_init_refuses_when_window_name_collides_in_same_session(self) -> None:
        # Another window already named `claude` in the same session means the
        # resolver would die on duplicate names. init refuses up-front so the
        # operator can rename / kill that window first.
        args = argparse.Namespace(agent="claude", target="%5")
        panes = [
            {"id": "%1", "location": "agents:0.0", "command": "claude", "window_name": "claude", "cwd": "/repo"},
            {"id": "%5", "location": "agents:2.0", "command": "zsh", "window_name": "zsh", "cwd": "/repo"},
        ]
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.application.commands.run_tmux",
                side_effect=self._init_run_tmux_side_effect("%5"),
            ), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:2.0"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            patch("mozyo_bridge.application.commands.rename_window") as rename, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                cmd_init(args)

        rename.assert_not_called()
        self.assertIn("agents:0(%1)", stderr.getvalue())
        self.assertIn("'claude'", stderr.getvalue())

    def test_cmd_init_allows_same_agent_name_in_a_different_session(self) -> None:
        # Cross-session `claude` windows are legitimate (one per repo). init
        # only refuses same-session collisions.
        args = argparse.Namespace(agent="claude", target="%5")
        panes = [
            {"id": "%2", "location": "other:0.0", "command": "claude", "window_name": "claude", "cwd": "/repo"},
            {"id": "%5", "location": "agents:1.0", "command": "zsh", "window_name": "zsh", "cwd": "/repo"},
        ]
        rename_calls: list[tuple] = []
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.application.commands.run_tmux",
                side_effect=self._init_run_tmux_side_effect("%5"),
            ), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:1.0"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            patch(
                "mozyo_bridge.application.commands.rename_window",
                side_effect=lambda target, name: rename_calls.append((target, name)),
            ), \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd_init(args))

        self.assertEqual([("agents:1", "claude")], rename_calls)

    def test_cmd_init_no_target_uses_current_pane(self) -> None:
        args = argparse.Namespace(agent="codex", target=None)
        panes = [
            {"id": "%9", "location": "agents:2.0", "command": "node", "window_name": "zsh", "cwd": "/repo"},
        ]
        rename_calls: list[tuple] = []
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.application.commands.current_pane",
                return_value="%9",
            ), \
            patch(
                "mozyo_bridge.application.commands.run_tmux",
                side_effect=self._init_run_tmux_side_effect("%9"),
            ), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:2.0"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            patch(
                "mozyo_bridge.application.commands.rename_window",
                side_effect=lambda target, name: rename_calls.append((target, name)),
            ), \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd_init(args))

        self.assertEqual([("agents:2", "codex")], rename_calls)

    def test_cmd_list_emits_window_column_in_place_of_label(self) -> None:
        panes = [
            {
                "id": "%1",
                "location": "repo:0.0",
                "command": "claude",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
            {
                "id": "%2",
                "location": "repo:1.0",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
        ]
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, cmd_list(argparse.Namespace()))

        output = stdout.getvalue()
        self.assertIn("WINDOW\tCWD", output)
        self.assertIn("\tclaude\t/repo", output)
        self.assertIn("\tcodex\t/repo", output)
        self.assertNotIn("LABEL", output)

    def test_cmd_init_rejects_label_as_target(self) -> None:
        args = argparse.Namespace(agent="claude", target="claude")
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                cmd_init(args)
        self.assertIn("not a label", stderr.getvalue())

    def test_resolve_status_session_prefers_current_tmux_session(self) -> None:
        args = argparse.Namespace(session=None, repo=None)

        with patch("mozyo_bridge.application.commands.current_session_name", return_value="mozyo_bridge"):
            self.assertEqual("mozyo_bridge", resolve_status_session(args))

    def test_resolve_status_session_falls_back_to_repo_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "my_project"
            repo.mkdir()
            args = argparse.Namespace(session=None, repo=str(repo))

            with patch("mozyo_bridge.application.commands.current_session_name", return_value=None):
                self.assertEqual("my_project", resolve_status_session(args))

    def test_resolve_status_session_respects_explicit_session(self) -> None:
        args = argparse.Namespace(session="custom", repo=None)

        with patch("mozyo_bridge.application.commands.current_session_name", return_value="other"):
            self.assertEqual("custom", resolve_status_session(args))

    def test_cmd_status_omits_misleading_missing_message_under_window_model(self) -> None:
        # Regression: bare `mozyo` session named after the repo basename must
        # not be reported as `agents (missing)` (Asana task 1214758916882465).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "my_project"
            repo.mkdir()
            args = argparse.Namespace(
                session=None,
                repo=str(repo),
                home=None,
                json=False,
                queue=None,
            )
            list_panes_result = argparse.Namespace(
                returncode=0,
                stdout=(
                    "0\tclaude\t%1\t1\tclaude\t" + str(repo) + "\n"
                    "1\tcodex\t%2\t1\tnode\t" + str(repo) + "\n"
                ),
                stderr="",
            )
            doctor_ok = {"sections": {"tmux": {"status": "ok", "next_action": []}}, "ok": True}

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.current_session_name", return_value="my_project"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
                patch("mozyo_bridge.application.commands.list_session_windows", return_value=["claude", "codex"]), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_panes_result), \
                patch("mozyo_bridge.application.commands.run_doctor", return_value=doctor_ok), \
                patch("mozyo_bridge.application.commands.format_doctor_text", return_value="tmux: ok\n"), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_status(args))

        output = stdout.getvalue()
        self.assertIn("session: my_project", output)
        self.assertNotIn("(missing)", output)
        self.assertIn("WINDOW\tNAME\tTARGET", output)

    def test_cmd_status_prints_init_hint_when_no_agent_windows_in_session(self) -> None:
        # Under the window-only model, a session without agent-named windows
        # is not a separate compat path — it is a session the operator must
        # migrate (via bare `mozyo`) or partially adopt (via `mozyo-bridge
        # init`). status surfaces one informational hint, not the old
        # "compat / pane-split" / "mixed:" branching.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "agents"
            repo.mkdir()
            args = argparse.Namespace(
                session="agents",
                repo=str(repo),
                home=None,
                json=False,
                queue=None,
            )
            doctor_warning = {
                "sections": {"tmux": {"status": "warning", "next_action": []}},
                "ok": False,
            }

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
                patch("mozyo_bridge.application.commands.list_session_windows", return_value=["zsh"]), \
                patch("mozyo_bridge.application.commands.run_doctor", return_value=doctor_warning), \
                patch(
                    "mozyo_bridge.application.commands.format_doctor_text",
                    return_value="tmux: warning\n",
                ), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                cmd_status(args)

        output = stdout.getvalue()
        self.assertIn("no agent windows in this session", output)
        self.assertIn("mozyo-bridge init claude|codex", output)
        self.assertNotIn("(compat)", output)
        self.assertNotIn("mixed:", output)

    def test_notify_agent_resolves_via_window_when_label_absent(self) -> None:
        # Regression: with the window model, an agent's tmux window is the
        # authoritative target. A notification request for `codex` must reach
        # the codex window even if the pane has not been `init`-labeled yet.
        panes = [
            {
                "id": "%9",
                "location": "mozyo_bridge:1.0",
                "command": "node",
                "label": "",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
        ]
        args = argparse.Namespace(
            issue="9020",
            journal="1",
            task_id=None,
            queue="unused",
            target=None,
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

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.find_handoff_task", return_value=None), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes), \
            patch("mozyo_bridge.domain.pane_resolver.current_session_name", return_value="mozyo_bridge"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            patch("mozyo_bridge.application.commands.ensure_agent_target"), \
            patch("mozyo_bridge.application.commands.cmd_read"), \
            patch("mozyo_bridge.application.commands.cmd_message"), \
            patch("mozyo_bridge.application.commands.wait_for_text", return_value=True), \
            patch("mozyo_bridge.application.commands.cmd_keys") as cmd_keys, \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, notify_agent(args, "codex"))

        cmd_keys.assert_called_once()

    def test_notify_agent_refuses_cross_session_label_fallback(self) -> None:
        # Regression: a `codex` label only present in another session must not
        # be used as a notification target from a different current session
        # (Asana task 1214743574772820 comment 1214746077864452).
        panes = [
            {
                "id": "%99",
                "location": "other:0.0",
                "command": "node",
                "label": "codex",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
        ]
        args = argparse.Namespace(
            issue="9020",
            journal="1",
            task_id=None,
            queue="unused",
            target=None,
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

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.find_handoff_task", return_value=None), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes), \
            patch("mozyo_bridge.domain.pane_resolver.current_session_name", return_value="mozyo_bridge"), \
            contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                notify_agent(args, "codex")


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
            self.assertEqual(
                {
                    "asana": "missing",
                    "redmine": "missing",
                    "redmine-governed": "missing",
                    "redmine-rails": "missing",
                    "redmine-rails-governed": "missing",
                    "none": "missing",
                },
                statuses,
            )
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

    def test_scaffold_section_unscaffolded_suggests_scaffold_apply(self) -> None:
        from mozyo_bridge.application.doctor import doctor_scaffold_section

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            args = self._stub_args(repo=str(target), home=str(Path(tmp) / "mb-home"))
            section = doctor_scaffold_section(args)
            self.assertEqual("missing", section["status"])
            actions = section["next_action"]
            self.assertTrue(
                any("mozyo-bridge scaffold apply" in action for action in actions)
            )
            # The legacy subcommand wording must not leak back into doctor.
            # The forbidden literal is built from parts so this source file
            # does not carry the old command name as contiguous prose.
            legacy_phrase = "scaffold " + "ru" + "les"
            self.assertFalse(
                any(legacy_phrase in action for action in actions)
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


class HandoffDomainTest(unittest.TestCase):
    def test_normalize_anchor_builds_asana_with_comment_id(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")

        self.assertIsInstance(anchor, AsanaAnchor)
        self.assertEqual("T1", anchor.task_id)
        self.assertEqual("C1", anchor.comment_id)
        self.assertIsNone(anchor.anchor_url)

    def test_normalize_anchor_builds_asana_with_anchor_url(self) -> None:
        anchor = normalize_anchor(
            "asana", task_id="T1", anchor_url="https://app.asana.com/0/0/T1#2026-05"
        )

        self.assertIsInstance(anchor, AsanaAnchor)
        self.assertEqual("https://app.asana.com/0/0/T1#2026-05", anchor.anchor_url)
        self.assertIsNone(anchor.comment_id)

    def test_normalize_anchor_rejects_asana_with_both_comment_and_url(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor(
                "asana", task_id="T1", comment_id="C1", anchor_url="https://example/x"
            )

    def test_normalize_anchor_rejects_asana_with_neither_comment_nor_url(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor("asana", task_id="T1")

    def test_normalize_anchor_rejects_asana_without_task_id(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor("asana", comment_id="C1")

    def test_normalize_anchor_builds_redmine(self) -> None:
        anchor = normalize_anchor("redmine", issue="9020", journal="46005")

        self.assertIsInstance(anchor, RedmineAnchor)
        self.assertEqual("9020", anchor.issue)
        self.assertEqual("46005", anchor.journal)

    def test_normalize_anchor_rejects_redmine_with_asana_fields(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor(
                "redmine", issue="9020", journal="46005", task_id="T1"
            )

    def test_normalize_anchor_rejects_asana_with_redmine_fields(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor(
                "asana", task_id="T1", comment_id="C1", journal="46005"
            )

    def test_normalize_anchor_rejects_unknown_source(self) -> None:
        with self.assertRaises(AnchorError):
            normalize_anchor("jira", task_id="T1")

    def test_build_marker_for_asana_with_comment(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")

        self.assertEqual(
            "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=reply:to=claude]",
            build_marker(anchor, "reply", "claude"),
        )

    def test_build_marker_for_asana_with_anchor_url(self) -> None:
        anchor = normalize_anchor(
            "asana", task_id="T1", anchor_url="https://example/x"
        )

        self.assertEqual(
            "[mozyo:handoff:source=asana:task=T1:anchor=https://example/x:kind=review_result:to=codex]",
            build_marker(anchor, "review_result", "codex"),
        )

    def test_build_marker_for_redmine(self) -> None:
        anchor = normalize_anchor("redmine", issue="9020", journal="46005")

        self.assertEqual(
            "[mozyo:handoff:source=redmine:issue=9020:journal=46005:kind=review_request:to=codex]",
            build_marker(anchor, "review_request", "codex"),
        )

    def test_build_notification_body_requires_summary_for_custom_kind(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")

        with self.assertRaises(AnchorError):
            build_notification_body(anchor, "custom", None, "claude")

    def test_build_notification_body_appends_durable_pointer(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")

        body = build_notification_body(anchor, "implementation_request", None, "claude")

        self.assertIn("implementation request ready for claude", body)
        self.assertIn("Asana task T1", body)
        self.assertIn("comment C1", body)
        self.assertIn("durable anchor", body)

    def test_build_notification_body_uses_summary_when_provided(self) -> None:
        anchor = normalize_anchor("redmine", issue="9020", journal="46005")

        body = build_notification_body(
            anchor, "custom", "ship hotfix to staging", "codex"
        )

        self.assertIn("ship hotfix to staging", body)
        self.assertIn("Redmine #9020", body)
        self.assertIn("journal #46005", body)

    def test_build_notification_body_rejects_unknown_kind(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")

        with self.assertRaises(AnchorError):
            build_notification_body(anchor, "shipping_notice", None, "claude")

    def test_make_outcome_sent_attributes_action_to_receiver(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker="[marker]",
        )

        self.assertEqual("receiver", outcome.next_action_owner)
        self.assertIn("durable anchor", outcome.next_action)
        payload = json.loads(outcome.to_json())
        self.assertEqual("sent", payload["status"])
        self.assertEqual("[marker]", payload["notification_marker"])
        self.assertEqual({"source": "asana", "task_id": "T1", "comment_id": "C1"}, payload["anchor"])

    def test_make_outcome_preserves_source_even_when_anchor_is_none(self) -> None:
        # Failure paths like invalid_anchor / invalid_args call make_outcome
        # without a normalized anchor. The structured contract still requires
        # `source`, so downstream durable-record integration (task
        # 1214760547941073) does not need to recover it out of band.
        outcome = make_outcome(
            status="blocked",
            reason="invalid_anchor",
            receiver="claude",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker=None,
            source="asana",
        )

        self.assertEqual("asana", outcome.source)
        payload = json.loads(outcome.to_json())
        self.assertEqual("asana", payload["source"])
        self.assertIsNone(payload["anchor"])

    def test_make_outcome_pending_attributes_action_to_operator(self) -> None:
        anchor = normalize_anchor("redmine", issue="9020", journal="46005")
        outcome = make_outcome(
            status="pending_input",
            reason="ok",
            receiver="codex",
            target="%2",
            anchor=anchor,
            mode=MODE_PENDING,
            kind="reply",
            notification_marker="[marker]",
        )

        self.assertEqual("operator", outcome.next_action_owner)
        self.assertIn("pending prompt", outcome.next_action)

    def test_next_action_for_marker_timeout_attributes_to_sender(self) -> None:
        owner, action = next_action_for("blocked", "marker_timeout", "claude")

        self.assertEqual("sender", owner)
        # The terminal escalation label is preserved so audit tooling and the
        # preset's `Notification fails` branch keep grepping the same word,
        # but the action now spells out the retry budget that must precede it
        # (Asana task 1214779823377861).
        self.assertIn("un-notified", action)
        self.assertIn("mozyo-bridge read claude", action)
        self.assertIn("--no-submit", action)
        self.assertIn("3", action)
        self.assertIn("next-action verb", action)

    def test_kind_labels_contract_is_stable(self) -> None:
        self.assertEqual(
            {
                "implementation_request",
                "design_consultation",
                "review_request",
                "review_result",
                "implementation_done",
                "reply",
                "custom",
            },
            set(KIND_LABELS),
        )


class ProjectLastInputTest(unittest.TestCase):
    """Cover the inspector ``last_input`` projection helper.

    The mapping table is fixed by the receiver-state inspector contract at
    ``mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md``
    section "Receiver Inspector and Existing DeliveryOutcome" and the upstream
    transport-agnostic ACK contract section "Existing DeliveryOutcome との対応".
    Both prohibit translating ACK terminal states (``blocked + *``) into
    runtime/process state, and tmux-path outcomes never claim ``acknowledged``.
    """

    def _build_outcome(
        self,
        *,
        status,
        reason,
        receiver: str = "claude",
        mode: str = MODE_STANDARD,
    ):
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        return make_outcome(
            status=status,
            reason=reason,
            receiver=receiver,
            target="%2",
            anchor=anchor,
            mode=mode,
            kind="implementation_request",
            notification_marker="[marker]",
        )

    def test_sent_ok_projects_submitted_ack_status(self) -> None:
        outcome = self._build_outcome(status="sent", reason="ok")

        projection = project_last_input(
            outcome,
            submitted_at="2026-05-13T13:20:28Z",
            input_kind="prompt",
            prompt_turn_id="turn-1",
            input_id="input-1",
        )

        self.assertEqual(
            LastInputProjection(
                submitted_at="2026-05-13T13:20:28Z",
                acknowledged_at=None,
                ack_status="submitted",
                input_kind="prompt",
                prompt_turn_id="turn-1",
                input_id="input-1",
            ),
            projection,
        )

    def test_sent_ok_does_not_claim_acknowledged_on_tmux_path(self) -> None:
        # The tmux compatibility layer cannot observe runtime.input.ack; the
        # helper must not synthesize an `acknowledged` claim from a `sent`
        # outcome even when the caller forgets to pass `submitted_at`.
        outcome = self._build_outcome(status="sent", reason="ok")

        projection = project_last_input(outcome)

        assert projection is not None
        self.assertEqual("submitted", projection.ack_status)
        self.assertIsNone(projection.acknowledged_at)
        self.assertIsNone(projection.submitted_at)

    def test_pending_input_ok_projects_unobserved_ack_status(self) -> None:
        # Per the inspector contract, `pending_input/ok` carries input staged
        # at the prompt but the receiver runtime has not received the turn.
        # `submitted_at` stays null and `ack_status` is `unobserved`.
        outcome = self._build_outcome(
            status="pending_input", reason="ok", mode=MODE_PENDING
        )

        projection = project_last_input(
            outcome,
            submitted_at="2026-05-13T13:20:28Z",
            input_kind="prompt",
            prompt_turn_id="turn-7",
        )

        assert projection is not None
        self.assertIsNone(projection.submitted_at)
        self.assertIsNone(projection.acknowledged_at)
        self.assertEqual("unobserved", projection.ack_status)
        self.assertEqual("prompt", projection.input_kind)
        self.assertEqual("turn-7", projection.prompt_turn_id)

    def test_blocked_marker_timeout_yields_no_projection(self) -> None:
        # `marker_timeout` is an ACK terminal state, not a receiver-runtime
        # fact. Refusing to project it prevents callers from inferring
        # `process.exited` or any runtime_phase value from a rollback.
        outcome = self._build_outcome(status="blocked", reason="marker_timeout")

        self.assertIsNone(project_last_input(outcome))

    def test_blocked_target_unavailable_yields_no_projection(self) -> None:
        outcome = self._build_outcome(status="blocked", reason="target_unavailable")

        self.assertIsNone(project_last_input(outcome))

    def test_blocked_target_not_agent_yields_no_projection(self) -> None:
        outcome = self._build_outcome(status="blocked", reason="target_not_agent")

        self.assertIsNone(project_last_input(outcome))

    def test_blocked_invalid_anchor_yields_no_projection(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="invalid_anchor",
            receiver="claude",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker=None,
            source="asana",
        )

        self.assertIsNone(project_last_input(outcome))

    def test_blocked_invalid_args_yields_no_projection(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="invalid_args",
            receiver="claude",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker=None,
            source="asana",
        )

        self.assertIsNone(project_last_input(outcome))

    def test_outcome_method_matches_projection_helper(self) -> None:
        outcome = self._build_outcome(status="sent", reason="ok")

        via_method = outcome.to_last_input_projection(
            submitted_at="2026-05-13T13:20:28Z",
            input_kind="prompt",
            prompt_turn_id="turn-1",
        )
        via_helper = project_last_input(
            outcome,
            submitted_at="2026-05-13T13:20:28Z",
            input_kind="prompt",
            prompt_turn_id="turn-1",
        )

        self.assertEqual(via_helper, via_method)

    def test_projection_dataclass_is_serialisable_to_dict(self) -> None:
        # Inspector consumers serialise projections into the ReceiverState
        # snapshot; ensure the dataclass exposes all the expected fields.
        outcome = self._build_outcome(status="sent", reason="ok")

        projection = project_last_input(
            outcome, submitted_at="2026-05-13T13:20:28Z"
        )
        assert projection is not None

        self.assertEqual(
            {
                "submitted_at": "2026-05-13T13:20:28Z",
                "acknowledged_at": None,
                "ack_status": "submitted",
                "input_kind": None,
                "prompt_turn_id": None,
                "input_id": None,
            },
            projection.to_dict(),
        )


class HandoffCliParserTest(unittest.TestCase):
    def test_handoff_send_requires_kind(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "handoff",
                        "send",
                        "--to",
                        "claude",
                        "--source",
                        "asana",
                        "--task-id",
                        "T1",
                        "--comment-id",
                        "C1",
                    ]
                )

    def test_handoff_send_parses_full_args(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--mode",
                "standard",
            ]
        )

        self.assertEqual("handoff", args.command)
        self.assertEqual("send", args.handoff_command)
        self.assertEqual("claude", args.to)
        self.assertEqual("asana", args.source)
        self.assertEqual("implementation_request", args.kind)
        self.assertEqual("T1", args.task_id)
        self.assertEqual("C1", args.comment_id)
        self.assertEqual(MODE_STANDARD, args.mode)

    def test_handoff_reply_allows_omitted_kind(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "handoff",
                "reply",
                "--to",
                "codex",
                "--source",
                "redmine",
                "--issue",
                "9020",
                "--journal",
                "46005",
            ]
        )

        self.assertEqual("handoff", args.command)
        self.assertEqual("reply", args.handoff_command)
        self.assertIsNone(args.kind)

    def test_reply_alias_shares_handoff_reply_func(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "reply",
                "--to",
                "claude",
                "--source",
                "asana",
                "--task-id",
                "T1",
                "--anchor-url",
                "https://example/x",
                "--mode",
                "pending",
            ]
        )

        self.assertEqual("reply", args.command)
        self.assertEqual("https://example/x", args.anchor_url)
        self.assertEqual(MODE_PENDING, args.mode)
        from mozyo_bridge.application.commands import cmd_handoff_reply

        self.assertIs(cmd_handoff_reply, args.func)

    def test_handoff_send_rejects_unknown_source(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "handoff",
                        "send",
                        "--to",
                        "claude",
                        "--source",
                        "jira",
                        "--kind",
                        "reply",
                    ]
                )

    def test_handoff_send_rejects_unknown_kind(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "handoff",
                        "send",
                        "--to",
                        "claude",
                        "--source",
                        "asana",
                        "--kind",
                        "ship_it",
                    ]
                )


class HandoffOrchestratorTest(unittest.TestCase):
    def run_handoff_with_fake_tmux(
        self,
        argv: list[str],
        captures: list[str] | None = None,
        allow_exit: bool = False,
        pane: dict[str, str] | None = None,
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
            if tmux_args[:3] == ("send-keys", "-t", "%2"):
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        default_pane = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": "/repo",
            "window_name": "claude",
        }
        pane_value = pane if pane is not None else default_pane

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value=None,
            ), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane_value]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue(), stderr.getvalue(), pane_text

    def _outcome_from_stdout(self, stdout: str) -> dict:
        lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(lines, f"no JSON outcome found in stdout: {stdout!r}")
        return json.loads(lines[-1])

    def test_standard_mode_sends_marker_body_and_enter(self) -> None:
        # Strict `--mode standard` happy path: marker observed → Enter pressed,
        # outcome `sent` / `ok` / mode=`standard`. v0.4 default is queue-enter,
        # so this test exercises the explicit strict fallback rail.
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        expected_marker = "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=implementation_request:to=claude]"
        self.assertIn(expected_marker, pane_text)
        self.assertIn("Asana task T1", pane_text)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("ok", outcome["reason"])
        self.assertEqual("claude", outcome["receiver"])
        self.assertEqual("%2", outcome["target"])
        self.assertEqual("asana", outcome["source"])
        self.assertEqual("implementation_request", outcome["kind"])
        self.assertEqual(MODE_STANDARD, outcome["mode"])
        self.assertEqual(expected_marker, outcome["notification_marker"])
        self.assertEqual("receiver", outcome["next_action_owner"])

    def test_pending_mode_leaves_input_unsubmitted_and_emits_pending_outcome(self) -> None:
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "reply",
                "--to",
                "codex",
                "--source",
                "redmine",
                "--issue",
                "9020",
                "--journal",
                "46005",
                "--target",
                "%2",
                "--force",
                "--mode",
                "pending",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
            },
        )

        self.assertEqual(0, result)
        self.assertIn(
            "[mozyo:handoff:source=redmine:issue=9020:journal=46005:kind=reply:to=codex]",
            pane_text,
        )
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "Enter") for call in sent))
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("pending_input", outcome["status"])
        self.assertEqual("reply", outcome["kind"])
        self.assertEqual("operator", outcome["next_action_owner"])

    def test_marker_timeout_rolls_back_and_emits_blocked_outcome(self) -> None:
        # Strict `--mode standard` fail-closed regression: marker miss must
        # roll back via `C-u` and emit `blocked` / `marker_timeout`. v0.4
        # default (queue-enter) deliberately does NOT roll back; that contract
        # is covered separately.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "review_result",
                "--task-id",
                "T1",
                "--anchor-url",
                "https://example/x",
                "--target",
                "%2",
                "--mode",
                "standard",
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

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])
        self.assertEqual("sender", outcome["next_action_owner"])

        # Asana task 1214779823377861: the rollback path must emit the
        # `--no-submit` fallback hint on stderr so agents do not jump to the
        # preset's `Notification fails` branch after a single transient
        # marker_timeout. Names the receiver and the per-preset cap so the
        # budget is unambiguous and not borrowed from the `handoff send`
        # retry pool.
        self.assertIn("hint: fallback path:", stderr)
        self.assertIn("mozyo-bridge read claude", stderr)
        self.assertIn("mozyo-bridge message claude", stderr)
        self.assertIn("--no-submit", stderr)
        self.assertIn("3", stderr)
        self.assertIn("separate budgets", stderr)
        self.assertIn("next-action verb", stderr)

    def test_invalid_anchor_emits_blocked_invalid_anchor_outcome(self) -> None:
        # Anchor normalization fires before rail-specific preflight, so this
        # test holds for both rails. Pinned to `--mode standard` so the v0.4
        # queue-enter force-rejection cannot eclipse the invalid_anchor exit.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--target",
                "%2",
                "--mode",
                "standard",
            ],
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_anchor", outcome["reason"])
        self.assertIsNone(outcome["target"])
        # Source must survive anchor-normalization failure so task
        # 1214760547941073 can persist the outcome without re-deriving it.
        self.assertEqual("asana", outcome["source"])
        self.assertIn("asana anchor", stderr)

    def test_non_agent_pane_without_force_emits_target_not_agent(self) -> None:
        # Strict-rail agent gate (`ensure_agent_target`) must still reject a
        # non-agent foreground process when `--force` is absent. Pinned to
        # `--mode standard` because queue-enter's Layer B preflight rejects on
        # `target_not_agent` via a different code path (Step 12) and would
        # surface a different `Reason` ordering.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "zsh",
                "cwd": "/repo",
                "window_name": "claude",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_not_agent", outcome["reason"])
        self.assertEqual("%2", outcome["target"])
        self.assertIn("target pane does not look like an agent pane", stderr)

    def test_target_unavailable_emits_blocked_target_unavailable(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
            ]
        )

        # No agent window in the session, no explicit --target. resolve_target
        # should die; the orchestrator must emit a structured outcome first.
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.domain.pane_resolver.current_session_name", return_value="my-project"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                args.func(args)

        out = stdout.getvalue()
        outcome_lines = [line for line in out.splitlines() if line.strip().startswith("{")]
        self.assertTrue(outcome_lines, f"no JSON outcome found in stdout: {out!r}")
        outcome = json.loads(outcome_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_unavailable", outcome["reason"])
        self.assertIsNone(outcome["target"])
        self.assertIn("no claude window found", stderr.getvalue())


class RelaxedQueueEnterRailTest(unittest.TestCase):
    """Coverage for the relaxed `queue-enter` rail.

    Implements the v0.2 contract section `## Relaxed Queue-Enter Rail` in
    ``vibes/docs/logics/tmux-send-safety-contract.md``. Strict `--mode standard`
    behavior must remain unchanged (covered by ``HandoffOrchestratorTest``);
    these tests focus on what the new rail adds and what it deliberately
    refuses to do.
    """

    def run_handoff_with_fake_tmux(
        self,
        argv,
        captures=None,
        allow_exit: bool = False,
        pane=None,
        current_session: str | None = "agents",
    ):
        # Mirror of HandoffOrchestratorTest.run_handoff_with_fake_tmux so this
        # class can drive the CLI end-to-end without launching tmux.
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

        def fake_run_tmux(*tmux_args, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", "%2", "-l"):
                pane_text += tmux_args[-1]
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            if tmux_args[:3] == ("send-keys", "-t", "%2"):
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        default_pane = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }
        pane_value = pane if pane is not None else default_pane

        # Step 10 (v0.3 deterministic preflight) reads the sender's tmux
        # session via `current_session_name`. Default to "agents" so the
        # default pane's location prefix (`agents:0.1`) matches; individual
        # tests can override by patching the same symbol inside the body.
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.application.commands.current_session_name", return_value=current_session), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane_value]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue(), stderr.getvalue(), pane_text

    def _outcome_from_stdout(self, stdout: str) -> dict:
        lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(lines, f"no JSON outcome found in stdout: {stdout!r}")
        return json.loads(lines[-1])

    # --- mode parsing / validation -------------------------------------------------

    def test_cli_accepts_mode_queue_enter(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--mode",
                "queue-enter",
            ]
        )

        self.assertEqual(MODE_QUEUE_ENTER, args.mode)

    def test_cli_default_mode_is_queue_enter_since_v0_4(self) -> None:
        # v0.4 contract pivot (Asana 1214824751741628) flipped the CLI default
        # for agent-pane handoff to queue-enter. Strict `--mode standard`
        # remains explicitly selectable; its regression coverage lives in
        # `test_strict_standard_still_rolls_back_on_marker_timeout` and
        # `test_strict_standard_admits_cross_receiver_process_unchanged`.
        parser = build_parser()
        args = parser.parse_args(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
            ]
        )

        self.assertEqual(MODE_QUEUE_ENTER, args.mode)

    def test_cli_rejects_unknown_mode(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(
                    [
                        "handoff",
                        "send",
                        "--to",
                        "claude",
                        "--source",
                        "asana",
                        "--kind",
                        "reply",
                        "--task-id",
                        "T1",
                        "--comment-id",
                        "C1",
                        "--mode",
                        "relaxed",
                    ]
                )

    # --- queue-enter behavior split ------------------------------------------------

    def test_queue_enter_observed_marker_emits_sent_ok_with_queue_enter_mode(self) -> None:
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        expected_marker = "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=implementation_request:to=claude]"
        self.assertIn(expected_marker, pane_text)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        # Marker was observed (default capture returns pane_text), so no
        # rollback occurred.
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("ok", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertEqual("receiver", outcome["next_action_owner"])

    def test_queue_enter_unobserved_marker_emits_sent_queue_enter_without_rollback(self) -> None:
        # Force capture to return empty so wait_for_text returns False.
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "review_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            captures=["", "", ""],
        )

        self.assertEqual(0, result)
        # Body was typed.
        self.assertIn(
            "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=review_request:to=claude]",
            pane_text,
        )
        # Enter WAS pressed (the rail's whole point).
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        # No rollback.
        self.assertFalse(any(call == ("send-keys", "-t", "%2", "C-u") for call in sent))

        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("queue_enter", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        # Per the contract, next_action_owner stays receiver-owned even when
        # the marker was unobserved.
        self.assertEqual("receiver", outcome["next_action_owner"])
        self.assertIn("durable anchor", outcome["next_action"])

    def test_strict_standard_still_rolls_back_on_marker_timeout(self) -> None:
        # Regression: the v0.4 default flip to queue-enter must not weaken
        # strict `standard`. Strict is now an explicit fallback (`--mode
        # standard`); its fail-closed `C-u` rollback on marker_timeout stays
        # exactly as it was in v0.1.
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "review_result",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
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
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])
        self.assertEqual(MODE_STANDARD, outcome["mode"])

    # --- agent-target restriction --------------------------------------------------

    def test_queue_enter_rejects_force_flag(self) -> None:
        # Per contract, `--force` cannot bypass agent-gate under queue-enter.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
                "--force",
            ],
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        # No typing should have occurred.
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("--force", stderr)
        self.assertIn("queue-enter", stderr)

    def test_queue_enter_rejects_explicit_target_in_other_receiver_window(self) -> None:
        # Per Codex audit finding on task 1214782240686275 comment 1214783754107198:
        # `ensure_agent_target` only checks that the pane is running *some*
        # agent-looking process (claude/codex/node), not that the pane belongs
        # to the intended receiver. Under strict, marker_timeout rollback caps
        # the blast radius. Under queue-enter, marker miss does NOT roll back,
        # so an explicit `--target %X` in the wrong receiver's window would
        # silently press Enter into the wrong agent. The queue-enter preflight
        # must reject this mismatch before typing.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
            ],
            pane={
                "id": "%2",
                "location": "agents:1.0",
                "command": "node",
                "cwd": "/repo",
                # Pane is a real agent process, but lives in the codex window.
                # Strict (`ensure_agent_target`) would currently accept this;
                # queue-enter must not.
                "window_name": "codex",
                "pane_active": "1",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        # No typing or Enter against the mismatched pane.
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        # Target id should still survive the outcome (typing was not done but
        # the pane resolved); helps audit which mismatched pane was rejected.
        self.assertEqual("%2", outcome["target"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("--target", stderr)
        self.assertIn("'codex'", stderr)
        self.assertIn("'claude'", stderr)

    def test_queue_enter_allows_explicit_target_in_matching_receiver_window(self) -> None:
        # Sanity check that the new preflight does not over-fire: an explicit
        # --target in the receiver's own window must still be accepted.
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
                "--submit-delay",
                "0",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
        )

        self.assertEqual(0, result)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

    def test_queue_enter_blocks_non_agent_pane(self) -> None:
        # Non-agent process in a claude window: the standard agent-gate fires
        # and emits target_not_agent. Under queue-enter this stays blocked
        # (and `--force` is not even available to override).
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "queue-enter",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "zsh",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_not_agent", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    # --- v0.3 deterministic preflight (Step 10 / 11 / 12) -------------------------

    def _queue_enter_argv(self, *, kind: str = "implementation_request") -> list[str]:
        return [
            "handoff",
            "send",
            "--to",
            "claude",
            "--source",
            "asana",
            "--kind",
            kind,
            "--task-id",
            "T1",
            "--comment-id",
            "C1",
            "--target",
            "%2",
            "--mode",
            "queue-enter",
        ]

    def test_queue_enter_step10_rejects_foreign_session_target(self) -> None:
        # Step 10 (v0.3): same-session binding. An explicit `--target %X` whose
        # pane lives in a different tmux session than the sender must be
        # rejected before typing — under queue-enter marker miss does not roll
        # back, so cross-session delivery could otherwise land in the wrong
        # repo's agent.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            self._queue_enter_argv(),
            pane={
                "id": "%2",
                "location": "other:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
            current_session="agents",
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("'agents'", stderr)
        self.assertIn("'other'", stderr)

    def test_queue_enter_step10_rejects_when_sender_outside_tmux(self) -> None:
        # Step 10 (v0.3): when invoked outside tmux, `current_session_name`
        # returns None. queue-enter must refuse rather than admit a comparison
        # against a missing sender session.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            self._queue_enter_argv(),
            current_session=None,
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("'<unset>'", stderr)

    def test_queue_enter_step11_rejects_inactive_pane(self) -> None:
        # Step 11 (v0.3): the target pane must be the active split of its
        # window. An inactive split would still accept keystrokes typed via
        # `send-keys -t %X`, but the receiver agent is by construction not the
        # foreground process the operator is looking at; queue-enter rejects
        # before typing.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            self._queue_enter_argv(),
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "0",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("pane_active", stderr)

    def test_queue_enter_step12_rejects_cross_receiver_literal_to_claude(self) -> None:
        # Step 12 (v0.3) strong identity: a literal `codex` process foregrounded
        # in a `claude` window cannot satisfy the per-receiver allowlist for
        # `claude`. Step 9 already enforces `window_name == receiver` for
        # explicit `--target`; this guards the case where the window itself was
        # renamed but the foreground process betrays a different receiver.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            self._queue_enter_argv(),
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "codex",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_not_agent", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("'codex'", stderr)
        self.assertIn("claude agent", stderr)

    def test_queue_enter_step12_rejects_cross_receiver_literal_to_codex(self) -> None:
        # Step 12 strong identity, symmetric case: a literal `claude` process
        # in a `codex` window is rejected for receiver=`codex`. (Literal
        # `node` is weak identity for both receivers because both Claude
        # Code and the Codex CLI are Node-based, so `node` does NOT exhibit
        # cross-binding rejection here; this test exercises the *strong*
        # branch only.)
        argv = self._queue_enter_argv(kind="reply")
        argv[argv.index("claude")] = "codex"
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            argv,
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "claude",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_not_agent", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertIn("queue-enter", stderr)
        self.assertIn("'claude'", stderr)
        self.assertIn("codex agent", stderr)

    def test_queue_enter_step12_admits_node_for_codex_weak_identity(self) -> None:
        # Both Claude Code and the Codex CLI surface as `node` in tmux
        # (Node-based runtimes). Step 12 admits `node` for receiver=`codex`
        # under the weak-identity branch — Step 9 (`window_name == receiver`)
        # plus Layer A operator discipline carry cross-binding protection
        # here. This test pins admission to keep real codex panes deliverable
        # under queue-enter.
        argv = self._queue_enter_argv(kind="reply") + ["--submit-delay", "0"]
        argv[argv.index("claude")] = "codex"
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            argv,
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
        )

        self.assertEqual(0, result)
        self.assertIn(
            "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=reply:to=codex]",
            pane_text,
        )
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_step12_admits_versioned_native_binary_weak_identity(self) -> None:
        # Step 12 weak identity (Open Question 8 in the contract): a versioned
        # native binary basename (e.g. `1.0.32-arm64`) is receiver-agnostic by
        # design. queue-enter admits it because the pane is at least running
        # *some* versioned native agent binary, and Step 9 + Layer A operator
        # discipline carry cross-binding protection in this branch. The
        # contract explicitly concedes the weakness; do not pretend Step 12
        # confirms receiver identity here.
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            self._queue_enter_argv() + ["--submit-delay", "0"],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "1.0.32-arm64",
                "cwd": "/repo",
                "window_name": "claude",
                "pane_active": "1",
            },
        )

        self.assertEqual(0, result)
        self.assertIn(
            "[mozyo:handoff:source=asana:task=T1:comment=C1:kind=implementation_request:to=claude]",
            pane_text,
        )
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_strict_standard_admits_cross_receiver_process_unchanged(self) -> None:
        # Regression: neither the v0.3 per-receiver process gate nor the v0.4
        # default flip to queue-enter must bleed into strict `--mode standard`.
        # Strict's behavior (admit any agent-looking process, rely on
        # marker_timeout + C-u rollback) stays as-is. This test pins that
        # boundary by sending strict to a pane whose foreground process is
        # `claude` while --to=codex; strict admits typing and rolls back on
        # marker miss, as it already did pre-v0.3.
        argv = [
            "handoff",
            "send",
            "--to",
            "codex",
            "--source",
            "asana",
            "--kind",
            "review_result",
            "--task-id",
            "T1",
            "--comment-id",
            "C1",
            "--target",
            "%2",
            "--mode",
            "standard",
            "--landing-timeout",
            "0.01",
            "--submit-delay",
            "0",
        ]
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            argv,
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "claude",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
            captures=["", "", ""],
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        # Strict still types and then rolls back; the new v0.3 gates must not
        # have fired here.
        self.assertTrue(any(call[:4] == ("send-keys", "-t", "%2", "-l") for call in sent))
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])
        self.assertEqual(MODE_STANDARD, outcome["mode"])

    # --- projection / wording ------------------------------------------------------

    def test_project_last_input_for_queue_enter_matches_strict_sent_ok(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome_strict = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker="[m]",
        )
        outcome_queue_enter = make_outcome(
            status="sent",
            reason="queue_enter",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="reply",
            notification_marker="[m]",
        )

        proj_strict = project_last_input(
            outcome_strict, submitted_at="2026-05-13T17:30:00Z"
        )
        proj_queue = project_last_input(
            outcome_queue_enter, submitted_at="2026-05-13T17:30:00Z"
        )

        # Per contract, queue-enter projection MUST equal strict sent/ok
        # projection. Returning ack_status="unobserved" or submitted_at=None
        # would violate the upstream inspector contract derive rule.
        self.assertEqual(proj_strict, proj_queue)
        assert proj_queue is not None
        self.assertEqual("submitted", proj_queue.ack_status)
        self.assertEqual("2026-05-13T17:30:00Z", proj_queue.submitted_at)
        self.assertIsNone(proj_queue.acknowledged_at)

    def test_project_last_input_for_queue_enter_mode_with_ok_reason_also_matches(self) -> None:
        # Marker-observed queue-enter (status=sent, reason=ok, mode=queue-enter)
        # must also project identically to strict sent/ok.
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="reply",
            notification_marker="[m]",
        )
        projection = project_last_input(outcome, submitted_at="2026-05-13T17:30:00Z")
        assert projection is not None
        self.assertEqual("submitted", projection.ack_status)
        self.assertEqual("2026-05-13T17:30:00Z", projection.submitted_at)

    def test_make_outcome_for_queue_enter_keeps_receiver_owned_next_action(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="sent",
            reason="queue_enter",
            receiver="codex",
            target="%2",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="implementation_request",
            notification_marker="[m]",
        )

        owner, action = next_action_for(outcome.status, outcome.reason, outcome.receiver)

        self.assertEqual("receiver", owner)
        self.assertEqual("receiver", outcome.next_action_owner)
        self.assertIn("durable anchor", action)
        self.assertIn("durable anchor", outcome.next_action)

    def test_delivery_record_for_queue_enter_unobserved_includes_operator_note(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="sent",
            reason="queue_enter",
            receiver="codex",
            target="%111",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="review_request",
            notification_marker="[mozyo:handoff:source=asana:task=T1:comment=C1:kind=review_request:to=codex]",
        )

        record = build_delivery_record(outcome)

        self.assertIn(
            "Delivery result — sent (queue-enter, marker unobserved)", record
        )
        self.assertIn("Mode: `queue-enter`", record)
        self.assertIn("Status: `sent` (reason: `queue_enter`)", record)
        # Receiver-side primary contract is identical to strict sent.
        self.assertIn("Next action owner: `receiver`", record)
        self.assertIn("Receiver-side contract", record)
        self.assertIn("durable anchor", record)
        # Operator note is the only place the queue-enter fallback is surfaced.
        self.assertIn("Operator note", record)
        self.assertIn("--mode standard", record)
        self.assertIn("not observed before Enter", record)

    def test_delivery_record_for_queue_enter_observed_marks_rail_but_no_operator_note(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_QUEUE_ENTER,
            kind="implementation_request",
            notification_marker="[m]",
        )

        record = build_delivery_record(outcome)

        self.assertIn(
            "Delivery result — sent (queue-enter, marker observed)", record
        )
        self.assertIn("Mode: `queue-enter`", record)
        self.assertIn("Next action owner: `receiver`", record)
        # No operator escalation note when the marker was actually observed.
        self.assertNotIn("Operator note", record)


class DeliveryRecordTest(unittest.TestCase):
    """Coverage for the durable delivery-record generator.

    The structured outcome contract guarantees every field the record needs
    (after the source-preservation fix on task ``1214760548032349``), so
    ``build_delivery_record`` must be a pure function over a ``DeliveryOutcome``
    and produce a deterministic, source-of-truth-pastable text block for every
    status/reason permutation the primitive can emit.
    """

    def _sent_outcome(self):
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        return make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[mozyo:handoff:source=asana:task=T1:comment=C1:kind=implementation_request:to=claude]",
        )

    def test_sent_record_includes_receiver_target_marker_anchor_and_contract(self) -> None:
        record = build_delivery_record(self._sent_outcome())

        self.assertIn("Delivery result — sent", record)
        self.assertIn("Receiver: `claude`", record)
        self.assertIn("Source: `asana`", record)
        self.assertIn("Kind: `implementation_request`", record)
        self.assertIn("Mode: `standard`", record)
        self.assertIn("Target pane: `%2`", record)
        self.assertIn(
            "Notification marker: "
            "`[mozyo:handoff:source=asana:task=T1:comment=C1:kind=implementation_request:to=claude]`",
            record,
        )
        self.assertIn("Asana task T1", record)
        self.assertIn("comment C1", record)
        self.assertIn("Landing marker observed", record)
        self.assertIn("Enter was pressed", record)
        self.assertIn("Next action owner: `receiver`", record)
        self.assertIn("Receiver-side contract", record)
        self.assertIn("durable anchor", record)

    def test_sent_record_includes_command_line_when_supplied(self) -> None:
        record = build_delivery_record(
            self._sent_outcome(),
            command="mozyo-bridge handoff send --to claude --source asana --task-id T1 --comment-id C1 --kind implementation_request",
        )

        self.assertIn(
            "- Command: `mozyo-bridge handoff send --to claude --source asana "
            "--task-id T1 --comment-id C1 --kind implementation_request`",
            record,
        )

    def test_pending_input_record_labels_operator_action(self) -> None:
        anchor = normalize_anchor("redmine", issue="9020", journal="46005")
        outcome = make_outcome(
            status="pending_input",
            reason="ok",
            receiver="codex",
            target="%111",
            anchor=anchor,
            mode=MODE_PENDING,
            kind="reply",
            notification_marker="[mozyo:handoff:source=redmine:issue=9020:journal=46005:kind=reply:to=codex]",
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — pending input", record)
        self.assertIn("Mode: `pending`", record)
        self.assertIn("intentionally not pressed", record)
        self.assertIn("Redmine #9020", record)
        self.assertIn("journal #46005", record)
        self.assertIn("Next action owner: `operator`", record)
        self.assertNotIn("Receiver-side contract", record)

    def test_marker_timeout_record_states_rollback_and_sender_action(self) -> None:
        anchor = normalize_anchor("asana", task_id="T1", comment_id="C1")
        outcome = make_outcome(
            status="blocked",
            reason="marker_timeout",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="review_result",
            notification_marker="[marker]",
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — not delivered (marker_timeout)", record)
        self.assertIn("input was cleared via C-u", record)
        self.assertIn("Enter was not pressed", record)
        self.assertIn("Receiver-side contract", record)
        self.assertIn("manually if action is still required", record)
        self.assertIn("Next action owner: `sender`", record)
        self.assertIn("un-notified", record)
        # Asana task 1214779823377861: the durable record must also surface
        # the ordered fallback path so an auditor (or any agent re-reading
        # the comment later) sees the retry budget before the un-notified
        # terminal label.
        self.assertIn("- Fallback path:", record)
        self.assertIn("mozyo-bridge read claude", record)
        self.assertIn("mozyo-bridge message claude", record)
        self.assertIn("--no-submit", record)
        self.assertIn("Notification fails", record)

    def test_target_unavailable_record_lacks_target_and_marker(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="target_unavailable",
            receiver="claude",
            target=None,
            anchor=normalize_anchor("asana", task_id="T1", comment_id="C1"),
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker=None,
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — not delivered (target_unavailable)", record)
        self.assertIn("Target pane: `—`", record)
        self.assertIn("Notification marker: `—`", record)
        self.assertIn("no notification was typed", record)
        self.assertIn("Next action owner: `sender`", record)
        self.assertIn("mozyo-bridge init claude", record)

    def test_target_not_agent_record_keeps_target_but_no_marker(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="target_not_agent",
            receiver="claude",
            target="%2",
            anchor=normalize_anchor("asana", task_id="T1", comment_id="C1"),
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker=None,
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — not delivered (target_not_agent)", record)
        self.assertIn("Target pane: `%2`", record)
        self.assertIn("Notification marker: `—`", record)
        self.assertIn("not running an agent process", record)
        self.assertIn("--force", record)
        self.assertIn("Next action owner: `sender`", record)

    def test_invalid_anchor_record_preserves_source_without_anchor_payload(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="invalid_anchor",
            receiver="claude",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind="reply",
            notification_marker=None,
            source="asana",
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — not delivered (invalid_anchor)", record)
        self.assertIn("Source: `asana`", record)
        self.assertIn("Durable anchor: —", record)
        self.assertIn("aborted before resolving the receiver pane", record)
        self.assertIn("supply a valid durable anchor", record)

    def test_invalid_args_record_states_arg_validation_failure(self) -> None:
        outcome = make_outcome(
            status="blocked",
            reason="invalid_args",
            receiver="codex",
            target=None,
            anchor=None,
            mode=MODE_STANDARD,
            kind=None,
            notification_marker=None,
            source="redmine",
        )

        record = build_delivery_record(outcome)

        self.assertIn("Delivery result — not delivered (invalid_args)", record)
        self.assertIn("Source: `redmine`", record)
        self.assertIn("Kind: `—`", record)
        self.assertIn("missing or invalid", record)
        self.assertIn("Next action owner: `sender`", record)

    def test_record_is_deterministic_for_same_outcome(self) -> None:
        outcome = self._sent_outcome()

        self.assertEqual(build_delivery_record(outcome), build_delivery_record(outcome))


class HandoffRecordEmissionTest(unittest.TestCase):
    """The orchestrator must emit the delivery record alongside the structured
    outcome so callers do not have to invent phrasing or re-read the pane to
    describe what happened.
    """

    def run_handoff_with_fake_tmux(
        self,
        argv: list[str],
        captures: list[str] | None = None,
        allow_exit: bool = False,
        pane: dict[str, str] | None = None,
    ):
        # Mirrors HandoffOrchestratorTest's helper so we can drive the CLI end
        # to end without launching tmux.
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
            if tmux_args[:3] == ("send-keys", "-t", "%2"):
                sent.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux call: {tmux_args}")

        default_pane = {
            "id": "%2",
            "location": "agents:0.1",
            "command": "node",
            "cwd": "/repo",
            "window_name": "claude",
        }
        pane_value = pane if pane is not None else default_pane

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value=None,
            ), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=[pane_value]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()):
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue()

    def test_standard_mode_emits_record_then_json_outcome_by_default(self) -> None:
        # Pinned to `--mode standard` so the record/JSON ordering is verified
        # against the strict-rail happy path (queue-enter has its own coverage
        # in `RelaxedQueueEnterRailTest`). v0.4 default is queue-enter.
        result, _sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--submit-delay",
                "0",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn("Delivery result — sent", stdout)
        self.assertIn("Asana task T1", stdout)
        self.assertIn("Next action owner: `receiver`", stdout)
        json_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertEqual(1, len(json_lines), f"expected exactly one JSON outcome line, got: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("sent", outcome["status"])

    def test_record_format_json_suppresses_markdown_record(self) -> None:
        # Pinned to `--mode standard` so the format-suppression test is not
        # eclipsed by the v0.4 queue-enter force-rejection.
        result, _sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--submit-delay",
                "0",
                "--record-format",
                "json",
            ]
        )

        self.assertEqual(0, result)
        self.assertNotIn("Delivery result —", stdout)
        json_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertEqual(1, len(json_lines))

    def test_record_format_text_suppresses_json_outcome(self) -> None:
        # Pinned to `--mode standard` so the format-suppression test is not
        # eclipsed by the v0.4 queue-enter force-rejection.
        result, _sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "implementation_request",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--submit-delay",
                "0",
                "--record-format",
                "text",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn("Delivery result — sent", stdout)
        json_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        self.assertEqual([], json_lines)

    def test_record_command_is_included_when_provided(self) -> None:
        # Pinned to `--mode standard` so the record-command trailer is verified
        # against the strict-rail happy path without the v0.4 queue-enter
        # force-rejection.
        result, _sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--comment-id",
                "C1",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--submit-delay",
                "0",
                "--record-command",
                "mozyo-bridge handoff send --to claude --source asana --kind reply",
            ]
        )

        self.assertEqual(0, result)
        self.assertIn(
            "- Command: `mozyo-bridge handoff send --to claude --source asana --kind reply`",
            stdout,
        )

    def test_marker_timeout_emits_record_describing_rollback(self) -> None:
        # Pinned to `--mode standard` so the rollback narrative is verified on
        # the strict rail. v0.4 queue-enter does not roll back on marker miss;
        # that contract is covered in `RelaxedQueueEnterRailTest`.
        result, sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "review_result",
                "--task-id",
                "T1",
                "--anchor-url",
                "https://example/x",
                "--target",
                "%2",
                "--mode",
                "standard",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            captures=["", "", ""],
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertIn(("send-keys", "-t", "%2", "C-u"), sent)
        self.assertIn("Delivery result — not delivered (marker_timeout)", stdout)
        self.assertIn("input was cleared via C-u", stdout)
        self.assertIn("Next action owner: `sender`", stdout)

    def test_invalid_anchor_emits_record_preserving_source(self) -> None:
        # Pinned to `--mode standard` so the invalid_anchor narrative is not
        # eclipsed by the v0.4 queue-enter force-rejection (which would emit
        # `invalid_args` first).
        result, _sent, stdout = self.run_handoff_with_fake_tmux(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "asana",
                "--kind",
                "reply",
                "--task-id",
                "T1",
                "--target",
                "%2",
                "--mode",
                "standard",
            ],
            allow_exit=True,
        )

        self.assertIsInstance(result, SystemExit)
        self.assertIn("Delivery result — not delivered (invalid_anchor)", stdout)
        self.assertIn("Source: `asana`", stdout)
        self.assertIn("Durable anchor: —", stdout)


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

    # ------------------------------------------------------------------
    # Redmine #10663: pin the sync script's --check mode so CI can gate
    # on plugin mirror drift without modifying the worktree. The Python
    # walker in test_plugin_skill_mirror_matches_canonical above does the
    # same check via a different code path; both must agree.
    # ------------------------------------------------------------------

    SYNC_SCRIPT_PATH = ROOT / "scripts" / "sync_plugin_skill.sh"

    def test_sync_script_check_mode_clean_exits_zero(self) -> None:
        """`scripts/sync_plugin_skill.sh --check` exits 0 when in sync.

        This pins the operator-facing CI gate for plugin mirror drift.
        If the check mode regresses to silently writing to the mirror,
        or to always-exit-0, this test fails.
        """
        self.assertTrue(self.SYNC_SCRIPT_PATH.is_file())
        result = subprocess.run(
            ["sh", str(self.SYNC_SCRIPT_PATH), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            0,
            result.returncode,
            msg=(
                f"sync_plugin_skill.sh --check exited {result.returncode}; "
                f"stdout={result.stdout!r} stderr={result.stderr!r}. "
                "Either the mirror drifted or the --check mode regressed."
            ),
        )
        self.assertIn("up to date", result.stdout)

    def test_sync_script_check_mode_detects_drift(self) -> None:
        """`--check` must exit non-zero on drift and name the recovery command."""
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / "scripts").mkdir()
            shutil.copy(self.SYNC_SCRIPT_PATH, stage / "scripts" / "sync_plugin_skill.sh")
            (stage / "scripts" / "sync_plugin_skill.sh").chmod(0o755)
            shutil.copytree(self.canonical_skill_dir, stage / "skills" / "mozyo-bridge-agent")
            shutil.copytree(
                self.plugin_skill_dir,
                stage / "plugins" / "mozyo-bridge-agent" / "skills" / "mozyo-bridge-agent",
            )

            tampered = (
                stage
                / "plugins"
                / "mozyo-bridge-agent"
                / "skills"
                / "mozyo-bridge-agent"
                / "references"
                / "workflow.md"
            )
            tampered.write_text(
                tampered.read_text(encoding="utf-8") + "\nTAMPER\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                ["sh", str(stage / "scripts" / "sync_plugin_skill.sh"), "--check"],
                cwd=str(stage),
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                1,
                result.returncode,
                msg=(
                    f"--check did not flag drift; stdout={result.stdout!r} "
                    f"stderr={result.stderr!r}"
                ),
            )
            # Recovery hint must be copy-paste runnable from the repo root,
            # not just the basename. Codex review #50344 caught a regression
            # where `$(basename "$0")` printed only `sync_plugin_skill.sh`,
            # which fails with `command not found` when pasted into a
            # repo-root shell. Pin the full `scripts/<name>` form so a
            # future edit cannot quietly drop the directory prefix.
            self.assertIn("scripts/sync_plugin_skill.sh", result.stderr)
            self.assertIn("from the repo root", result.stderr)
            self.assertIn("references/workflow.md", result.stderr)
            # And the bare basename without a directory prefix must never
            # appear as a standalone recovery command.
            self.assertNotIn("'sync_plugin_skill.sh'", result.stderr)

    def test_sync_script_check_mode_does_not_modify_worktree(self) -> None:
        """`--check` must be read-only — no rsync to disk."""
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / "scripts").mkdir()
            shutil.copy(self.SYNC_SCRIPT_PATH, stage / "scripts" / "sync_plugin_skill.sh")
            (stage / "scripts" / "sync_plugin_skill.sh").chmod(0o755)
            shutil.copytree(self.canonical_skill_dir, stage / "skills" / "mozyo-bridge-agent")
            shutil.copytree(
                self.plugin_skill_dir,
                stage / "plugins" / "mozyo-bridge-agent" / "skills" / "mozyo-bridge-agent",
            )

            mirror_workflow = (
                stage
                / "plugins"
                / "mozyo-bridge-agent"
                / "skills"
                / "mozyo-bridge-agent"
                / "references"
                / "workflow.md"
            )
            # Force a drift the script's check would report.
            mirror_workflow.write_text(
                mirror_workflow.read_text(encoding="utf-8") + "\nTAMPER\n",
                encoding="utf-8",
            )
            before = mirror_workflow.read_bytes()

            subprocess.run(
                ["sh", str(stage / "scripts" / "sync_plugin_skill.sh"), "--check"],
                cwd=str(stage),
                capture_output=True,
                text=True,
            )

            after = mirror_workflow.read_bytes()
            self.assertEqual(
                before,
                after,
                msg=(
                    "--check modified the mirror file; the recovery command "
                    "is the rewrite path, not --check."
                ),
            )

    def test_sync_script_rejects_unknown_flag(self) -> None:
        """Reject typos to avoid silently running the wrong mode."""
        result = subprocess.run(
            ["sh", str(self.SYNC_SCRIPT_PATH), "--bogus"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(64, result.returncode)
        self.assertIn("unknown argument", result.stderr)


class ReleaseHelperParserTest(unittest.TestCase):
    """The contract-admitted release helper subcommands must round-trip
    through ``build_parser``. Argparse will raise SystemExit if a required
    flag is missing or a subparser was wired wrong, so this is a cheap
    structural check that ``release check`` / ``release workflow`` exist as
    documented in `release-helper-contract.md`.
    """

    def parse(self, *argv: str) -> argparse.Namespace:
        return build_parser().parse_args(list(argv))

    def test_release_check_tree(self) -> None:
        args = self.parse("release", "check", "tree")
        from mozyo_bridge.application.release import cmd_release_check_tree

        self.assertIs(args.func, cmd_release_check_tree)

    def test_release_check_scaffold(self) -> None:
        args = self.parse("release", "check", "scaffold")
        from mozyo_bridge.application.release import cmd_release_check_scaffold

        self.assertIs(args.func, cmd_release_check_scaffold)

    def test_release_check_artifact(self) -> None:
        args = self.parse("release", "check", "artifact")
        from mozyo_bridge.application.release import cmd_release_check_artifact

        self.assertIs(args.func, cmd_release_check_artifact)

    def test_release_check_drift(self) -> None:
        args = self.parse("release", "check", "drift")
        from mozyo_bridge.application.release import cmd_release_check_drift

        self.assertIs(args.func, cmd_release_check_drift)

    def test_release_check_workflow_requires_run_id(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "check", "workflow")
        args = self.parse("release", "check", "workflow", "--run-id", "42")
        from mozyo_bridge.application.release import cmd_release_check_workflow

        self.assertIs(args.func, cmd_release_check_workflow)
        self.assertEqual("42", args.run_id)

    def test_release_workflow_runs_requires_workflow(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "workflow", "runs")
        args = self.parse("release", "workflow", "runs", "--workflow", "testpypi.yml")
        from mozyo_bridge.application.release import cmd_release_workflow_runs

        self.assertIs(args.func, cmd_release_workflow_runs)
        self.assertEqual("testpypi.yml", args.workflow)
        self.assertEqual(10, args.limit)

    def test_release_workflow_wait_requires_run_id_and_timeout(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "workflow", "wait", "--run-id", "42")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "workflow", "wait", "--timeout", "10")
        args = self.parse(
            "release",
            "workflow",
            "wait",
            "--run-id",
            "42",
            "--timeout",
            "30",
        )
        from mozyo_bridge.application.release import cmd_release_workflow_wait

        self.assertIs(args.func, cmd_release_workflow_wait)
        self.assertEqual("42", args.run_id)
        self.assertEqual(30.0, args.timeout)

    def test_release_check_subparser_requires_subcommand(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "check")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "workflow")


class ReleaseCheckTreeTest(unittest.TestCase):
    """`release check tree` runs three git probes inside a real git repo and
    is strict-fail on the git grep blocker pattern. The tests build a tiny
    git checkout with `subprocess`, then verify both clean and blocker exit
    codes against real git behavior — no subprocess mocking, so the regex
    and pathspec wiring stay honest.
    """

    def _init_repo(self, root: Path) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "--allow-empty", "-m", "init", "-q"],
            check=True,
            env=env,
        )

    def _commit_file(self, root: Path, rel: str, body: str) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", rel], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", f"add {rel}", "-q"],
            check=True,
            env=env,
        )

    def test_clean_tree_returns_zero(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            self._commit_file(root, "README.md", "Hello world\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("result: clean", out.getvalue())

    def test_personal_path_in_tracked_file_is_blocker(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            personal_path = "/Users" + "/example/project"
            self._commit_file(root, "AGENTS.md", f"see {personal_path} for context\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn(personal_path, out.getvalue())
            self.assertIn("result: blocker", out.getvalue())

    def test_secret_value_shape_in_tracked_file_is_blocker(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            fake_secret = "REDMINE" + "_API_KEY=" + "abc123"
            self._commit_file(root, "AGENTS.md", f"{fake_secret}\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn(fake_secret, out.getvalue())
            self.assertIn("result: blocker", out.getvalue())

    def test_secret_guidance_words_do_not_block_tree_check(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            self._commit_file(
                root,
                "README.md",
                "Do not store credentials, tokens, secrets, or passwords.\n",
            )
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("result: clean", out.getvalue())

    def test_pathspec_excludes_skip_generated_trees(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            # Files inside excluded pathspecs (build/, dist/, tmp/) must not
            # trigger the blocker even if they contain personal paths, so
            # the helper does not flag artifacts that will be rebuilt or
            # excluded from publication anyway.
            personal_path = "/Users" + "/example/leak"
            self._commit_file(root, "build/log.txt", f"{personal_path}\n")
            args = argparse.Namespace(repo=str(root))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_tree(args)
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("result: clean", out.getvalue())


class ReleaseCheckScaffoldTest(unittest.TestCase):
    def test_scaffold_check_uses_isolated_home_and_targets(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = release_mod.cmd_release_check_scaffold(argparse.Namespace())
        # Fresh scaffold smoke runs against an isolated home / target every
        # invocation. On a healthy package it must report clean for all
        # presets and exit zero.
        self.assertEqual(release_mod.EXIT_CLEAN, rc, msg=out.getvalue())
        text = out.getvalue()
        from mozyo_bridge.scaffold.rules import PRESETS

        for preset in PRESETS:
            self.assertIn(f"scaffold status: clean ({preset})", text)


class ReleaseCheckArtifactTest(unittest.TestCase):
    """The `release check` family is contractually read-only: invocations
    must not mutate the repo worktree (including the repo's ``dist/``
    directory). This test locks in that invariant by setting up a sentinel
    file in a fake repo's dist/, mocking ``sys.executable -m build``, and asserting
    (a) the sentinel survives, (b) ``--outdir`` is passed to build, and
    (c) the outdir lives outside the repo root.
    """

    def test_artifact_secret_pattern_matches_values_not_guidance_words(self) -> None:
        from mozyo_bridge.application import release as release_mod

        pattern = re.compile(release_mod._artifact_grep_pattern())
        fake_secret = "REDMINE" + "_API_KEY=" + "abc123"
        self.assertIsNone(pattern.search("Do not store tokens or secrets."))
        self.assertIsNotNone(pattern.search(fake_secret))

    def test_does_not_mutate_repo_dist_directory(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as repo_str:
            repo = Path(repo_str).resolve()
            (repo / "dist").mkdir()
            sentinel = repo / "dist" / "preexisting.whl"
            sentinel.write_bytes(b"preexisting")

            recorded: list[dict] = []

            def fake_run(argv, cwd=None, check=False, env=None):
                recorded.append(
                    {"argv": list(argv), "cwd": str(cwd) if cwd else None}
                )
                # Pretend build succeeded but wrote nothing to the outdir.
                # The helper's no-mutation invariant is what we're testing;
                # producing no artifacts just routes us through the
                # `no artifacts` blocker path, which is fine for this test.
                outdir = argv[argv.index("--outdir") + 1]
                Path(outdir).mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = release_mod.cmd_release_check_artifact(
                        argparse.Namespace(repo=str(repo))
                    )

            self.assertTrue(
                sentinel.exists(),
                "release check artifact mutated the repo's dist/ directory",
            )
            build_calls = [c for c in recorded if "build" in c["argv"]]
            self.assertEqual(1, len(build_calls), msg=recorded)
            argv = build_calls[0]["argv"]
            self.assertEqual(sys.executable, argv[0])
            self.assertIn("--outdir", argv)
            outdir = Path(argv[argv.index("--outdir") + 1]).resolve()
            try:
                outdir.relative_to(repo)
                inside_repo = True
            except ValueError:
                inside_repo = False
            self.assertFalse(
                inside_repo,
                f"--outdir {outdir} must not live inside repo {repo}",
            )
            # rc is blocker because the mocked build produced no artifacts;
            # the load-bearing assertions are the sentinel + outdir checks
            # above.
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)


class ReleaseCheckWorkflowTest(unittest.TestCase):
    def test_success_exits_zero(self) -> None:
        from mozyo_bridge.application import release as release_mod

        payload = {
            "status": "completed",
            "conclusion": "success",
            "workflowName": "Test",
            "headSha": "abc123",
            "url": "https://example/run/42",
        }
        with patch.object(release_mod, "_gh_run_view", return_value=payload):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_check_workflow(
                    argparse.Namespace(run_id="42")
                )
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn("status: completed", out.getvalue())
        self.assertIn("conclusion: success", out.getvalue())

    def test_failure_exits_non_zero(self) -> None:
        from mozyo_bridge.application import release as release_mod

        payload = {
            "status": "completed",
            "conclusion": "failure",
            "workflowName": "Test",
            "headSha": "abc123",
            "url": "https://example/run/42",
        }
        with patch.object(release_mod, "_gh_run_view", return_value=payload):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = release_mod.cmd_release_check_workflow(
                    argparse.Namespace(run_id="42")
                )
        self.assertEqual(release_mod.EXIT_BLOCKER, rc)

    def test_in_progress_exits_non_zero(self) -> None:
        from mozyo_bridge.application import release as release_mod

        payload = {
            "status": "in_progress",
            "conclusion": None,
            "workflowName": "Test",
            "headSha": "abc123",
            "url": "https://example/run/42",
        }
        with patch.object(release_mod, "_gh_run_view", return_value=payload):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = release_mod.cmd_release_check_workflow(
                    argparse.Namespace(run_id="42")
                )
        self.assertEqual(release_mod.EXIT_BLOCKER, rc)


class ReleaseWorkflowRunsTest(unittest.TestCase):
    def test_runs_listing_renders_columns(self) -> None:
        from mozyo_bridge.application import release as release_mod

        runs = [
            {
                "databaseId": 1,
                "createdAt": "2026-05-14T00:00:00Z",
                "status": "completed",
                "conclusion": "success",
                "headSha": "abc",
                "url": "https://example/1",
            },
            {
                "databaseId": 2,
                "createdAt": "2026-05-14T01:00:00Z",
                "status": "in_progress",
                "conclusion": None,
                "headSha": "def",
                "url": "https://example/2",
            },
        ]
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(runs), stderr=""
        )
        with patch.object(release_mod, "_run", return_value=completed):
            with patch.object(release_mod, "_require_command"):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    rc = release_mod.cmd_release_workflow_runs(
                        argparse.Namespace(workflow="testpypi.yml", limit=10)
                    )
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        text = out.getvalue()
        self.assertIn("RUN_ID\tCREATED_AT\tSTATUS\tCONCLUSION\tHEAD_SHA\tHTML_URL", text)
        self.assertIn("1\t2026-05-14T00:00:00Z\tcompleted\tsuccess\tabc\thttps://example/1", text)
        self.assertIn("2\t2026-05-14T01:00:00Z\tin_progress\t\tdef\thttps://example/2", text)


class ReleaseWorkflowWaitTest(unittest.TestCase):
    def test_wait_returns_zero_when_run_completes_successfully(self) -> None:
        from mozyo_bridge.application import release as release_mod

        sequence = [
            {"status": "in_progress", "conclusion": None},
            {"status": "completed", "conclusion": "success"},
        ]
        with patch.object(release_mod, "_gh_run_view", side_effect=sequence):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod.time, "sleep"):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        rc = release_mod.cmd_release_workflow_wait(
                            argparse.Namespace(run_id="42", timeout=30.0, poll=0.0)
                        )
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertIn("conclusion: success", out.getvalue())

    def test_wait_returns_timeout_code_when_deadline_elapses(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with patch.object(
            release_mod,
            "_gh_run_view",
            return_value={"status": "in_progress", "conclusion": None},
        ):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod.time, "sleep"):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        rc = release_mod.cmd_release_workflow_wait(
                            argparse.Namespace(
                                run_id="42", timeout=0.0, poll=0.0
                            )
                        )
        self.assertEqual(release_mod.EXIT_TIMEOUT, rc)
        self.assertIn("timeout: exceeded", out.getvalue())

    def test_wait_returns_blocker_when_run_fails(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with patch.object(
            release_mod,
            "_gh_run_view",
            return_value={"status": "completed", "conclusion": "failure"},
        ):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod.time, "sleep"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = release_mod.cmd_release_workflow_wait(
                            argparse.Namespace(
                                run_id="42", timeout=30.0, poll=0.0
                            )
                        )
        self.assertEqual(release_mod.EXIT_BLOCKER, rc)


class ReleaseBumpPublishParserTest(unittest.TestCase):
    """The bump/publish CLI must enforce mutually-exclusive mode flags and
    pass through per-mode args. Argparse will raise on the missing/
    conflicting-mode cases below if the wiring is wrong, so this is a cheap
    structural check.
    """

    def parse(self, *argv: str) -> argparse.Namespace:
        return build_parser().parse_args(list(argv))

    def test_release_bump_requires_mode(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "bump")

    def test_release_bump_mode_is_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "bump", "--check", "--to", "0.3.0")

    def test_release_bump_check(self) -> None:
        args = self.parse("release", "bump", "--check")
        from mozyo_bridge.application.release import cmd_release_bump

        self.assertIs(args.func, cmd_release_bump)
        self.assertTrue(args.check)
        self.assertIsNone(args.to)

    def test_release_bump_to(self) -> None:
        args = self.parse("release", "bump", "--to", "0.3.0a1")
        from mozyo_bridge.application.release import cmd_release_bump

        self.assertIs(args.func, cmd_release_bump)
        self.assertFalse(args.check)
        self.assertEqual("0.3.0a1", args.to)

    def test_release_publish_requires_mode(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "publish")

    def test_release_publish_mode_is_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parse("release", "publish", "--testpypi", "--pypi")

    def test_release_publish_testpypi(self) -> None:
        args = self.parse(
            "release", "publish", "--testpypi", "--version", "0.3.0a1"
        )
        self.assertTrue(args.testpypi)
        self.assertEqual("0.3.0a1", args.version)
        self.assertFalse(args.execute)

    def test_release_publish_pypi_dryrun(self) -> None:
        args = self.parse(
            "release",
            "publish",
            "--pypi",
            "--tag",
            "v0.3.0",
            "--notes-file",
            "/tmp/notes.md",
        )
        self.assertTrue(args.pypi)
        self.assertEqual("v0.3.0", args.tag)
        self.assertEqual("/tmp/notes.md", args.notes_file)
        self.assertFalse(args.execute)

    def test_release_publish_pypi_execute(self) -> None:
        args = self.parse(
            "release",
            "publish",
            "--pypi",
            "--tag",
            "v0.3.0",
            "--notes-file",
            "/tmp/notes.md",
            "--execute",
        )
        self.assertTrue(args.execute)

    def test_release_publish_plan(self) -> None:
        args = self.parse("release", "publish", "--plan")
        self.assertTrue(args.plan)


class ReleaseBumpCheckTest(unittest.TestCase):
    """`release bump --check` must (a) read the mirror set from the contract
    doc, (b) report version literals from each mirror file, (c) strict-fail
    when the mirror values disagree. Tests build a fake repo with both a
    contract doc and the mirror-set files.
    """

    def _build_fake_repo(
        self,
        root: Path,
        *,
        pyproject_version: str = "0.3.0",
        module_version: str = "0.3.0",
    ) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "fake"\nversion = "{pyproject_version}"\n',
            encoding="utf-8",
        )
        module_dir = root / "src" / "mozyo_bridge"
        module_dir.mkdir(parents=True)
        (module_dir / "__init__.py").write_text(
            f'__version__ = "{module_version}"\n', encoding="utf-8"
        )
        contract_dir = root / "vibes" / "docs" / "logics"
        contract_dir.mkdir(parents=True)
        (contract_dir / "release-helper-contract.md").write_text(
            "# Contract\n\n"
            "release-version mirror set は以下の 2 file に固定する。\n\n"
            "- `pyproject.toml` の `[project].version`\n"
            "- `src/mozyo_bridge/__init__.py` の `__version__`\n\n"
            "Other section.\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(root), "add", "."],
            check=True,
            env=env,
        )
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "Release v" + pyproject_version, "-q"],
            check=True,
            env=env,
        )

    def test_clean_check_reports_each_mirror_file(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root, pyproject_version="0.3.0", module_version="0.3.0")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_bump(
                    argparse.Namespace(repo=str(root), check=True, to=None)
                )
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            text = out.getvalue()
            self.assertIn("pyproject.toml", text)
            self.assertIn("[project].version", text)
            self.assertIn("src/mozyo_bridge/__init__.py", text)
            self.assertIn("__version__", text)
            self.assertIn("0.3.0", text)
            self.assertIn("result: clean", text)

    def test_mirror_set_drift_is_blocker(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(
                root, pyproject_version="0.3.0", module_version="0.2.9"
            )
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_bump(
                    argparse.Namespace(repo=str(root), check=True, to=None)
                )
            self.assertEqual(release_mod.EXIT_BLOCKER, rc)
            self.assertIn("mirror set values disagree", out.getvalue())

    def test_contract_missing_anchor_is_fatal(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root)
            # Strip the anchor sentence from the contract doc. The helper
            # must refuse to operate rather than guess at the mirror set.
            contract_path = root / "vibes" / "docs" / "logics" / "release-helper-contract.md"
            contract_path.write_text(
                "# Contract\n\n"
                "(mirror-set section removed for this test)\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    release_mod.cmd_release_bump(
                        argparse.Namespace(repo=str(root), check=True, to=None)
                    )


class ReleaseBumpToTest(unittest.TestCase):
    """`release bump --to` must rewrite every mirror-set file in the
    worktree and never commit/push/tag. Tests assert (a) post-bump file
    contents, (b) absence of any new commits in the fake repo, (c)
    idempotency when called with the existing version.
    """

    def _build_fake_repo(
        self,
        root: Path,
        *,
        pyproject_version: str = "0.3.0",
        module_version: str = "0.3.0",
    ) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "fake"\nversion = "{pyproject_version}"\n',
            encoding="utf-8",
        )
        module_dir = root / "src" / "mozyo_bridge"
        module_dir.mkdir(parents=True)
        (module_dir / "__init__.py").write_text(
            f'__version__ = "{module_version}"\n', encoding="utf-8"
        )
        contract_dir = root / "vibes" / "docs" / "logics"
        contract_dir.mkdir(parents=True)
        (contract_dir / "release-helper-contract.md").write_text(
            "release-version mirror set は以下の 2 file に固定する。\n\n"
            "- `pyproject.toml` の `[project].version`\n"
            "- `src/mozyo_bridge/__init__.py` の `__version__`\n\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "init", "-q"],
            check=True,
            env=env,
        )
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()

    def test_rewrites_every_mirror_file_without_committing(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial_head = self._build_fake_repo(root)

            with contextlib.redirect_stdout(io.StringIO()):
                rc = release_mod.cmd_release_bump(
                    argparse.Namespace(repo=str(root), check=False, to="0.4.0")
                )
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn(
                '"0.4.0"',
                (root / "pyproject.toml").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '"0.4.0"',
                (root / "src" / "mozyo_bridge" / "__init__.py").read_text(
                    encoding="utf-8"
                ),
            )
            head_after = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(
                initial_head,
                head_after,
                "release bump --to created a commit; helper must leave commit "
                "authority with the operator",
            )

    def test_same_version_is_idempotent_noop(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(
                root, pyproject_version="0.4.0", module_version="0.4.0"
            )
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = release_mod.cmd_release_bump(
                    argparse.Namespace(repo=str(root), check=False, to="0.4.0")
                )
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("already at 0.4.0", out.getvalue())
            self.assertIn(
                "no-op (mirror set was already at 0.4.0)", out.getvalue()
            )

    def test_invalid_version_shape_is_rejected(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    release_mod.cmd_release_bump(
                        argparse.Namespace(
                            repo=str(root), check=False, to="not-a-version"
                        )
                    )

    def test_missing_version_literal_strict_fails(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root)
            # Drop the __version__ literal from the python mirror file so
            # the helper cannot find it. The helper must strict-fail rather
            # than partially rewrite the mirror set — pyproject.toml must
            # still carry the pre-bump version.
            pyproject_before = (root / "pyproject.toml").read_text(encoding="utf-8")
            (root / "src" / "mozyo_bridge" / "__init__.py").write_text(
                "# version moved elsewhere\n", encoding="utf-8"
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        release_mod.cmd_release_bump(
                            argparse.Namespace(
                                repo=str(root), check=False, to="0.4.0"
                            )
                        )
            self.assertEqual(
                pyproject_before,
                (root / "pyproject.toml").read_text(encoding="utf-8"),
                "release bump --to partially rewrote the mirror set on strict-fail",
            )


class ReleasePublishTest(unittest.TestCase):
    """`release publish --pypi` must default to dry-run; `--execute` must
    be required to invoke `gh release create`. `--testpypi` and `--plan`
    are smoke-tested for argv shape via mock.
    """

    def test_pypi_dry_run_does_not_invoke_gh(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes.md"
            notes.write_text("# v0.3.0\nNotes\n", encoding="utf-8")
            recorded = []

            def fake_run(argv, cwd=None, check=False, env=None):
                recorded.append(list(argv))
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    rc = release_mod.cmd_release_publish(
                        argparse.Namespace(
                            testpypi=False,
                            pypi=True,
                            plan=False,
                            tag="v0.3.0",
                            notes_file=str(notes),
                            execute=False,
                            version=None,
                            repo=None,
                        )
                    )
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertIn("(dry-run)", out.getvalue())
            self.assertEqual(
                recorded,
                [],
                "dry-run must NOT invoke `gh release create`",
            )
            self.assertIn("Re-run with `--execute`", out.getvalue())

    def test_pypi_execute_invokes_gh_release_create(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes.md"
            notes.write_text("# v0.3.0\nNotes\n", encoding="utf-8")
            recorded = []

            def fake_run(argv, cwd=None, check=False, env=None):
                recorded.append(list(argv))
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="created\n", stderr=""
                )

            with patch.object(release_mod, "_run", side_effect=fake_run):
                with patch.object(release_mod, "_require_command"):
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = release_mod.cmd_release_publish(
                            argparse.Namespace(
                                testpypi=False,
                                pypi=True,
                                plan=False,
                                tag="v0.3.0",
                                notes_file=str(notes),
                                execute=True,
                                version=None,
                                repo=None,
                            )
                        )
            self.assertEqual(release_mod.EXIT_CLEAN, rc)
            self.assertEqual(1, len(recorded))
            argv = recorded[0]
            self.assertEqual(argv[0], "gh")
            self.assertEqual(argv[1:4], ["release", "create", "v0.3.0"])
            self.assertIn("--verify-tag", argv)
            self.assertIn("--notes-file", argv)

    def test_pypi_rejects_missing_notes_file(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.md"
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    release_mod.cmd_release_publish(
                        argparse.Namespace(
                            testpypi=False,
                            pypi=True,
                            plan=False,
                            tag="v0.3.0",
                            notes_file=str(missing),
                            execute=False,
                            version=None,
                            repo=None,
                        )
                    )

    def test_pypi_rejects_invalid_tag(self) -> None:
        from mozyo_bridge.application import release as release_mod

        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes.md"
            notes.write_text("notes", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    release_mod.cmd_release_publish(
                        argparse.Namespace(
                            testpypi=False,
                            pypi=True,
                            plan=False,
                            tag="0.3.0",  # missing `v` prefix
                            notes_file=str(notes),
                            execute=False,
                            version=None,
                            repo=None,
                        )
                    )

    def test_testpypi_dispatch_validates_version_without_workflow_input(self) -> None:
        from mozyo_bridge.application import release as release_mod

        dispatch_call = []

        def fake_run(argv, cwd=None, check=False, env=None):
            dispatch_call.append(list(argv))
            if "workflow" in argv and "run" in argv:
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="", stderr=""
                )
            # gh run list response
            payload = json.dumps(
                [
                    {
                        "databaseId": 9999,
                        "url": "https://example/run/9999",
                        "createdAt": "2026-05-14T11:00:00Z",
                        "headSha": "abc",
                        "status": "queued",
                    }
                ]
            )
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=payload, stderr=""
            )

        with patch.object(release_mod, "_run", side_effect=fake_run):
            with patch.object(release_mod, "_require_command"):
                with patch.object(release_mod.time, "sleep"):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        rc = release_mod.cmd_release_publish(
                            argparse.Namespace(
                                testpypi=True,
                                pypi=False,
                                plan=False,
                                tag=None,
                                notes_file=None,
                                execute=False,
                                version="0.3.0a1",
                                repo=None,
                            )
                        )
        self.assertEqual(release_mod.EXIT_CLEAN, rc)
        self.assertEqual(2, len(dispatch_call))
        dispatch_argv = dispatch_call[0]
        self.assertEqual(
            dispatch_argv,
            [
                "gh",
                "workflow",
                "run",
                "testpypi.yml",
                "--ref",
                "main",
            ],
        )
        self.assertIn("9999", out.getvalue())


class CodexDirectEditGuardrailHardeningTest(unittest.TestCase):
    """Guardrail: distributed surfaces must agree that guardrail / docs /
    catalog changes require a Redmine ``codex_direct_edit`` gate journal
    (with ``allowed_paths``) before Codex creates new repo diffs, and
    that ``.mozyo-bridge/docs/file_conventions.generated.yaml`` is
    generator-only.

    Motivation: hardening prompted by a past incident pattern where
    Codex committed guardrail / docs / catalog changes without the gate
    journal. The wording across the central preset, the project routers,
    the canonical skill reference, and the project-local rules drifts
    easily; this test pins each surface to the post-hardening wording.

    Each assertion below names the surface and the exact requirement so
    that a regression failure points at one file and one fix.
    """

    GUARDRAIL_HARDENING_PATHS = (
        ".mozyo-bridge/docs/catalog.yaml",
        ".mozyo-bridge/docs/file_conventions.generated.yaml",
        "vibes/docs/**",
        "README.md",
    )

    PRESET_REQUIRED_MARKERS = (
        # The guardrail-edit condition must be tied to the gate, not to
        # a chat-level "user said it's fine".
        "codex_direct_edit gate が有効 (allowed_paths にガードレール path を明示)",
        # generated artifacts must be called out as generator-only.
        ".mozyo-bridge/docs/file_conventions.generated.yaml",
        "手編集: 禁止",
        "mozyo-bridge docs generate-file-conventions",
        # The Codex Direct Edit Gate must explicitly cover guardrail
        # paths now, not only implementation files.
        "ガードレールおよび docs/catalog 周辺",
    )

    PRESET_FORBIDDEN_MARKERS = (
        # The pre-hardening wording allowed Codex to edit guardrails on
        # bare user authorization. This exact phrase, used alone as the
        # guardrail-editor condition, must not reappear.
        "codex編集条件: ユーザーがガードレール変更を明示\n",
    )

    def _packaged_preset(self, preset: str) -> str:
        path = (
            ROOT
            / "src"
            / "mozyo_bridge"
            / "scaffold"
            / "presets"
            / preset
            / "agent-workflow.md"
        )
        self.assertTrue(path.is_file(), f"missing packaged preset: {path}")
        return path.read_text(encoding="utf-8")

    def test_packaged_redmine_governed_preset_has_hardened_wording(self) -> None:
        body = self._packaged_preset("redmine-governed")
        for marker in self.PRESET_REQUIRED_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"redmine-governed preset is missing hardened marker "
                    f"{marker!r}; see Codex direct edit hardening."
                ),
            )
        for forbidden in self.PRESET_FORBIDDEN_MARKERS:
            self.assertNotIn(
                forbidden,
                body,
                msg=(
                    f"redmine-governed preset still carries pre-hardening "
                    f"permissive wording {forbidden!r}; Codex direct edit "
                    f"on guardrails must require the gate journal."
                ),
            )

    def test_packaged_redmine_rails_governed_preset_has_hardened_wording(
        self,
    ) -> None:
        body = self._packaged_preset("redmine-rails-governed")
        for marker in self.PRESET_REQUIRED_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"redmine-rails-governed preset is missing hardened "
                    f"marker {marker!r}; see Codex direct edit hardening."
                ),
            )
        for forbidden in self.PRESET_FORBIDDEN_MARKERS:
            self.assertNotIn(
                forbidden,
                body,
                msg=(
                    f"redmine-rails-governed preset still carries "
                    f"pre-hardening permissive wording {forbidden!r}."
                ),
            )

    def test_canonical_skill_reference_workflow_aligns_with_hardening(
        self,
    ) -> None:
        ref = (
            ROOT
            / "skills"
            / "mozyo-bridge-agent"
            / "references"
            / "workflow.md"
        )
        body = ref.read_text(encoding="utf-8")
        # Must explicitly name the Redmine gate journal as the durable
        # record for Redmine projects.
        for marker in (
            "Redmine `codex_direct_edit` gate journal",
            "allowed_paths",
            "role: 実装者",
            "follow_up_review",
            # Generator-only artifact rule must live in the skill reference too.
            ".mozyo-bridge/docs/file_conventions.generated.yaml",
            # The hardening must be tied to a concrete failure mode
            # without publishing internal ticket identifiers.
            "Past incident pattern",
            "Review Gate-approved audit-owned commit path",
        ):
            self.assertIn(
                marker,
                body,
                msg=(
                    f"skills/mozyo-bridge-agent/references/workflow.md is "
                    f"missing hardened marker {marker!r}."
                ),
            )

    def test_project_local_agent_workflow_aligns_with_hardening(self) -> None:
        body = (
            ROOT / "vibes" / "docs" / "rules" / "agent-workflow.md"
        ).read_text(encoding="utf-8")
        for marker in (
            "Redmine `codex_direct_edit` gate journal",
            "allowed_paths",
            "follow_up_review",
            ".mozyo-bridge/docs/file_conventions.generated.yaml",
            "過去 incident pattern",
            "Review Gate 承認済み audit-owned commit path",
        ):
            self.assertIn(
                marker,
                body,
                msg=(
                    f"vibes/docs/rules/agent-workflow.md is missing "
                    f"hardened marker {marker!r}."
                ),
            )

    def test_root_routers_name_full_guardrail_scope(self) -> None:
        """AGENTS.md and CLAUDE.md must enumerate the docs / guardrail
        paths the hardening covers, so a reader of the router alone
        cannot mistake `.mozyo-bridge/docs/catalog.yaml` or
        `vibes/docs/**` for "free to direct-edit on chat instruction"."""
        for router_name in ("AGENTS.md", "CLAUDE.md"):
            body = (ROOT / router_name).read_text(encoding="utf-8")
            for marker in (
                "vibes/docs/**",
                ".mozyo-bridge/docs/catalog.yaml",
                "file_conventions.generated.yaml",
                "codex_direct_edit",
                "allowed_paths",
            ):
                self.assertIn(
                    marker,
                    body,
                    msg=(
                        f"{router_name} Project-Local Additions is missing "
                        f"hardened marker {marker!r}; the router pair must "
                        f"agree on the hardening scope."
                    ),
                )

    def test_plugin_skill_mirror_carries_hardened_workflow_reference(
        self,
    ) -> None:
        """The plugin marketplace mirror must reflect the same hardened
        skill reference as the canonical body. The drift test
        (``PluginMarketplaceTest``) already enforces byte equality, but
        we keep an explicit marker check here so a regression points at
        ``Codex Direct Edit Gate hardening`` rather than at the generic
        "mirror drifted" message."""
        mirror = (
            ROOT
            / "plugins"
            / "mozyo-bridge-agent"
            / "skills"
            / "mozyo-bridge-agent"
            / "references"
            / "workflow.md"
        )
        body = mirror.read_text(encoding="utf-8")
        self.assertIn("Redmine `codex_direct_edit` gate journal", body)
        self.assertIn(".mozyo-bridge/docs/file_conventions.generated.yaml", body)


class TmuxUiHostWiringTest(unittest.TestCase):
    """Direct unit tests for the tmux UI host wiring install/uninstall/status.

    Covers the byte-stability contract (no surrounding content changes,
    no extra blank lines on round-trip), idempotency, drift detection,
    --force replacement, backup, dry-run, and the snippet-missing
    precondition.
    """

    def _make_repo(self, tmp: Path) -> Path:
        repo = tmp / "repo"
        snippet = repo / ".mozyo-bridge" / "tmux" / "agent-ui.conf"
        snippet.parent.mkdir(parents=True, exist_ok=True)
        snippet.write_text("# repo snippet\n", encoding="utf-8")
        return repo

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def test_install_fresh_creates_managed_block_only(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"
            host_conf.write_text(
                "# user existing content\nset -g status on\n",
                encoding="utf-8",
            )

            result = tmux_ui.apply_install(
                repo_root=repo, tmux_conf=host_conf
            )
            self.assertTrue(result.changed)
            self.assertEqual("appended", result.action)
            text = host_conf.read_text(encoding="utf-8")
            # Existing content survives untouched at the head.
            self.assertTrue(text.startswith("# user existing content\nset -g status on\n"))
            # Managed block present at the tail.
            self.assertIn(tmux_ui.MANAGED_BLOCK_BEGIN, text)
            self.assertIn(tmux_ui.MANAGED_BLOCK_END, text)
            absolute = str((repo / ".mozyo-bridge/tmux/agent-ui.conf").resolve())
            self.assertIn(absolute, text)
            self.assertIn("if-shell", text)

    def test_install_into_missing_file_creates_minimal_file(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"  # does not exist

            result = tmux_ui.apply_install(
                repo_root=repo, tmux_conf=host_conf
            )
            self.assertEqual("created", result.action)
            text = host_conf.read_text(encoding="utf-8")
            # File contains only the managed block (no user content).
            self.assertTrue(text.startswith(tmux_ui.MANAGED_BLOCK_BEGIN))
            self.assertTrue(text.rstrip("\n").endswith(tmux_ui.MANAGED_BLOCK_END))

    def test_install_is_idempotent(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"
            host_conf.write_text("# user\n", encoding="utf-8")

            first = tmux_ui.apply_install(repo_root=repo, tmux_conf=host_conf)
            after_first = host_conf.read_text(encoding="utf-8")
            second = tmux_ui.apply_install(repo_root=repo, tmux_conf=host_conf)
            after_second = host_conf.read_text(encoding="utf-8")

            self.assertEqual("appended", first.action)
            self.assertEqual("noop", second.action)
            self.assertFalse(second.changed)
            self.assertEqual(after_first, after_second)

    def test_install_drift_requires_force(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo_a = self._make_repo(tmp)
            # Second repo with its own snippet to trigger drift.
            repo_b = tmp / "repo_b"
            (repo_b / ".mozyo-bridge" / "tmux").mkdir(parents=True)
            (repo_b / ".mozyo-bridge" / "tmux" / "agent-ui.conf").write_text(
                "# other snippet\n", encoding="utf-8"
            )
            host_conf = tmp / ".tmux.conf"

            tmux_ui.apply_install(repo_root=repo_a, tmux_conf=host_conf)
            with self.assertRaises(tmux_ui.TmuxUiError):
                tmux_ui.apply_install(repo_root=repo_b, tmux_conf=host_conf)

            forced = tmux_ui.apply_install(
                repo_root=repo_b, tmux_conf=host_conf, force=True
            )
            self.assertEqual("replaced", forced.action)
            text = host_conf.read_text(encoding="utf-8")
            self.assertIn(
                str((repo_b / ".mozyo-bridge/tmux/agent-ui.conf").resolve()),
                text,
            )
            self.assertNotIn(
                str((repo_a / ".mozyo-bridge/tmux/agent-ui.conf").resolve()),
                text,
            )

    def test_install_dry_run_does_not_write(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"
            host_conf.write_text("# untouched\n", encoding="utf-8")

            result = tmux_ui.apply_install(
                repo_root=repo, tmux_conf=host_conf, dry_run=True
            )
            self.assertTrue(result.changed)
            self.assertTrue(result.dry_run)
            self.assertEqual(
                "# untouched\n", host_conf.read_text(encoding="utf-8")
            )

    def test_install_backup_preserves_original(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"
            host_conf.write_text("# original\nset -g foo on\n", encoding="utf-8")

            result = tmux_ui.apply_install(
                repo_root=repo, tmux_conf=host_conf, backup=True
            )
            self.assertIsNotNone(result.backup_path)
            assert result.backup_path is not None
            self.assertEqual(
                "# original\nset -g foo on\n",
                result.backup_path.read_text(encoding="utf-8"),
            )

    def test_install_rejects_missing_snippet(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            empty_repo = tmp / "empty"
            empty_repo.mkdir()
            host_conf = tmp / ".tmux.conf"

            with self.assertRaises(tmux_ui.TmuxUiError):
                tmux_ui.apply_install(
                    repo_root=empty_repo, tmux_conf=host_conf
                )
            # No partial file written.
            self.assertFalse(host_conf.exists())

    def test_uninstall_round_trip_is_byte_stable(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"
            original = "# user content\nset -g status on\nset -g foo bar\n"
            host_conf.write_text(original, encoding="utf-8")

            tmux_ui.apply_install(repo_root=repo, tmux_conf=host_conf)
            uninstall = tmux_ui.apply_uninstall(tmux_conf=host_conf)
            self.assertEqual("removed", uninstall.action)
            self.assertEqual(
                original,
                host_conf.read_text(encoding="utf-8"),
                msg="install → uninstall did not round-trip byte-for-byte",
            )

    def test_uninstall_when_not_installed_is_noop(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            host_conf = tmp / ".tmux.conf"
            host_conf.write_text("# user\n", encoding="utf-8")

            result = tmux_ui.apply_uninstall(tmux_conf=host_conf)
            self.assertEqual("noop", result.action)
            self.assertEqual("# user\n", host_conf.read_text(encoding="utf-8"))

    def test_uninstall_dry_run_does_not_write(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"
            host_conf.write_text("# user\n", encoding="utf-8")
            tmux_ui.apply_install(repo_root=repo, tmux_conf=host_conf)
            before = host_conf.read_text(encoding="utf-8")

            result = tmux_ui.apply_uninstall(tmux_conf=host_conf, dry_run=True)
            self.assertTrue(result.changed)
            self.assertTrue(result.dry_run)
            self.assertEqual(before, host_conf.read_text(encoding="utf-8"))

    def test_uninstall_backup_preserves_original(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"
            host_conf.write_text("# user\n", encoding="utf-8")
            tmux_ui.apply_install(repo_root=repo, tmux_conf=host_conf)
            with_block = host_conf.read_text(encoding="utf-8")

            result = tmux_ui.apply_uninstall(tmux_conf=host_conf, backup=True)
            self.assertIsNotNone(result.backup_path)
            assert result.backup_path is not None
            self.assertEqual(
                with_block,
                result.backup_path.read_text(encoding="utf-8"),
            )

    def test_status_states(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"

            # not-installed when host conf is missing.
            info = tmux_ui.compute_status(repo, host_conf)
            self.assertEqual("not-installed", info["state"])
            self.assertFalse(info["tmux_conf_exists"])

            tmux_ui.apply_install(repo_root=repo, tmux_conf=host_conf)
            info = tmux_ui.compute_status(repo, host_conf)
            self.assertEqual("installed", info["state"])
            self.assertEqual(
                str((repo / ".mozyo-bridge/tmux/agent-ui.conf").resolve()),
                info["current_source_path"],
            )

            # Simulate drift: rename the snippet so its absolute path
            # no longer matches the managed block.
            moved = tmp / "moved" / "agent-ui.conf"
            moved.parent.mkdir(parents=True)
            (repo / ".mozyo-bridge" / "tmux" / "agent-ui.conf").rename(moved)
            info = tmux_ui.compute_status(repo, host_conf)
            self.assertEqual("drift", info["state"])
            self.assertIsNotNone(info["drift_reason"])

    def test_cli_install_status_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"
            host_conf.write_text("# user\n", encoding="utf-8")

            code, out = self._run_cli(
                [
                    "tmux-ui",
                    "install",
                    "--repo",
                    str(repo),
                    "--tmux-conf",
                    str(host_conf),
                ]
            )
            self.assertEqual(0, code)
            self.assertIn("appended", out)

            code, out = self._run_cli(
                [
                    "tmux-ui",
                    "status",
                    "--repo",
                    str(repo),
                    "--tmux-conf",
                    str(host_conf),
                ]
            )
            self.assertEqual(0, code)
            self.assertIn("installed", out)

            code, out = self._run_cli(
                [
                    "tmux-ui",
                    "uninstall",
                    "--tmux-conf",
                    str(host_conf),
                ]
            )
            self.assertEqual(0, code)
            self.assertIn("removed", out)
            self.assertEqual("# user\n", host_conf.read_text(encoding="utf-8"))

    def test_cli_status_json_drift_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo = self._make_repo(tmp)
            host_conf = tmp / ".tmux.conf"

            self._run_cli(
                [
                    "tmux-ui",
                    "install",
                    "--repo",
                    str(repo),
                    "--tmux-conf",
                    str(host_conf),
                ]
            )
            # Drift: delete the snippet so the managed block points at
            # a missing path.
            (repo / ".mozyo-bridge" / "tmux" / "agent-ui.conf").unlink()

            code, out = self._run_cli(
                [
                    "tmux-ui",
                    "status",
                    "--repo",
                    str(repo),
                    "--tmux-conf",
                    str(host_conf),
                    "--json",
                ]
            )
            payload = json.loads(out)
            self.assertEqual("drift", payload["state"])
            self.assertEqual(1, code)

    def test_doctor_reports_host_wiring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            home = tmp / "home"
            project = tmp / "project"
            project.mkdir()
            host_conf = tmp / ".tmux.conf"

            # Bring the governed preset into the project so the manifest
            # tracks agent-ui.conf and doctor reports host_wiring.
            parser = build_parser()
            for argv in (
                ["rules", "install", "--home", str(home)],
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ],
            ):
                args = parser.parse_args(argv)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, args.func(args))

            # Before install: host_wiring=not-installed (and exists fine).
            with patch.object(
                __import__(
                    "mozyo_bridge.application.tmux_ui",
                    fromlist=["default_host_tmux_conf"],
                ),
                "default_host_tmux_conf",
                return_value=host_conf,
            ):
                args = parser.parse_args(
                    [
                        "doctor",
                        "--target",
                        str(project),
                        "--home",
                        str(home),
                        "--json",
                    ]
                )
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    args.func(args)
                payload = json.loads(buf.getvalue())
                wiring = payload["sections"]["tmux"]["artifact"]["host_wiring"]
                self.assertEqual("not-installed", wiring["state"])

                # Install via CLI, doctor now reports `installed`.
                install_args = parser.parse_args(
                    [
                        "tmux-ui",
                        "install",
                        "--repo",
                        str(project),
                        "--tmux-conf",
                        str(host_conf),
                    ]
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    install_args.func(install_args)

                args = parser.parse_args(
                    [
                        "doctor",
                        "--target",
                        str(project),
                        "--home",
                        str(home),
                        "--json",
                    ]
                )
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    args.func(args)
                payload = json.loads(buf.getvalue())
                wiring = payload["sections"]["tmux"]["artifact"]["host_wiring"]
                self.assertEqual("installed", wiring["state"])

    def _install_repo_at_dir(self, parent: Path, dir_name: str) -> tuple[Path, Path, str]:
        repo = parent / dir_name
        snippet = repo / ".mozyo-bridge" / "tmux" / "agent-ui.conf"
        snippet.parent.mkdir(parents=True, exist_ok=True)
        snippet.write_text("# repo snippet\n", encoding="utf-8")
        return repo, snippet, str(snippet.resolve())

    def test_render_managed_block_rejects_single_quote_path(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            _, snippet, _ = self._install_repo_at_dir(tmp, "repo-with-'quote'")
            with self.assertRaises(tmux_ui.TmuxUiError):
                tmux_ui.render_managed_block(snippet)

    def test_install_rejects_single_quote_path_without_writing(self) -> None:
        from mozyo_bridge.application import tmux_ui

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            repo, _, _ = self._install_repo_at_dir(tmp, "repo-with-'quote'")
            host_conf = tmp / ".tmux.conf"
            host_conf.write_text("# untouched\n", encoding="utf-8")

            with self.assertRaises(tmux_ui.TmuxUiError):
                tmux_ui.apply_install(repo_root=repo, tmux_conf=host_conf)
            self.assertEqual(
                "# untouched\n", host_conf.read_text(encoding="utf-8")
            )

    def test_render_managed_block_escapes_special_chars(self) -> None:
        """Each path-significant tmux byte is escaped exactly once.

        The directive line is the parsed-once tmux form, so:
        - ``"`` → ``\\"``
        - ``$`` → ``\\$``
        - ``#`` → ``##`` (the ``\\#`` escape is not recognised by tmux
          and would silently leak a ``#`` through format substitution).
        The byte-literal comment line carries the unescaped path so
        comparators are not coupled to the escape table.
        """
        from mozyo_bridge.application import tmux_ui

        for dir_name in (
            'repo with spaces',
            'repo-with-"dquote"',
            'repo-with-$dollar',
            'repo-with-#hash',
        ):
            with tempfile.TemporaryDirectory() as tmp_str:
                tmp = Path(tmp_str)
                _, snippet, absolute = self._install_repo_at_dir(tmp, dir_name)
                block = tmux_ui.render_managed_block(snippet)
                self.assertIn(
                    f"{tmux_ui.SOURCE_COMMENT_PREFIX}{absolute}\n",
                    block,
                )
                directive = next(
                    line for line in block.splitlines() if line.startswith("if-shell ")
                )
                if '"' in absolute:
                    self.assertIn('\\"', directive)
                if '$' in absolute:
                    self.assertIn('\\$', directive)
                if '#' in absolute:
                    self.assertIn('##', directive)
                    # Lone backslash-hash must not appear: tmux does not
                    # recognise it and would leak a live ``#``.
                    self.assertNotIn('\\#', directive)

    def test_install_uninstall_round_trip_with_special_chars(self) -> None:
        from mozyo_bridge.application import tmux_ui

        original_host = "# user content\nset -g status on\n"
        for dir_name in (
            'repo with spaces',
            'repo-with-"dquote"',
            'repo-with-$dollar',
            'repo-with-#hash',
        ):
            with tempfile.TemporaryDirectory() as tmp_str:
                tmp = Path(tmp_str)
                repo, _, absolute = self._install_repo_at_dir(tmp, dir_name)
                host_conf = tmp / ".tmux.conf"
                host_conf.write_text(original_host, encoding="utf-8")

                tmux_ui.apply_install(repo_root=repo, tmux_conf=host_conf)
                status = tmux_ui.compute_status(repo, host_conf)
                self.assertEqual(
                    "installed",
                    status["state"],
                    msg=f"install state for path={dir_name!r}",
                )
                # Source comment is the byte-literal path, so status
                # parses it back regardless of the escape table.
                self.assertEqual(absolute, status["current_source_path"])

                tmux_ui.apply_uninstall(tmux_conf=host_conf)
                self.assertEqual(
                    original_host,
                    host_conf.read_text(encoding="utf-8"),
                    msg=f"round-trip did not restore byte-for-byte for path={dir_name!r}",
                )


class AgentDiscoveryTest(unittest.TestCase):
    """Read-only cross-workspace discovery (Redmine #10332).

    Coverage for ``mozyo_bridge.domain.agent_discovery``: classification by
    window-name agent rail, per-session ambiguity detection, repo-root
    inference via PROJECT_MARKERS, and the ``mozyo-bridge agents list``
    CLI surface (text + JSON).
    """

    def test_discover_agents_classifies_by_window_name(self) -> None:
        from mozyo_bridge.domain.agent_discovery import discover_agents

        records = discover_agents(
            panes=[
                {
                    "id": "%1",
                    "location": "sess_a:0.0",
                    "command": "claude",
                    "cwd": "/repo",
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%2",
                    "location": "sess_b:1.0",
                    "command": "node",
                    "cwd": "/repo",
                    "window_name": "codex",
                    "pane_active": "1",
                },
                {
                    "id": "%3",
                    "location": "sess_a:2.0",
                    "command": "zsh",
                    "cwd": "/tmp",
                    "window_name": "shell",
                    "pane_active": "0",
                },
            ]
        )
        kinds = {(r.pane_id, r.agent_kind) for r in records}
        self.assertEqual(
            {("%1", "claude"), ("%2", "codex"), ("%3", "unknown")},
            kinds,
        )

    def test_discover_agents_flags_same_session_duplicate_window_name(self) -> None:
        from mozyo_bridge.domain.agent_discovery import discover_agents

        records = discover_agents(
            panes=[
                {
                    "id": "%1",
                    "location": "sess_a:0.0",
                    "command": "claude",
                    "cwd": "/repo",
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%2",
                    "location": "sess_a:2.0",
                    "command": "node",
                    "cwd": "/repo",
                    "window_name": "claude",
                    "pane_active": "1",
                },
            ]
        )
        # Both panes in the same session sharing `claude` window name surface
        # the ambiguity flag so callers can fail closed before issuing a
        # handoff. find_agent_window already raises on this within a single
        # session; discovery surfaces it without raising so cross-workspace
        # readers can see it.
        self.assertTrue(all(r.ambiguous for r in records))

    def test_discover_agents_does_not_cross_flag_same_window_in_different_sessions(
        self,
    ) -> None:
        from mozyo_bridge.domain.agent_discovery import discover_agents

        records = discover_agents(
            panes=[
                {
                    "id": "%1",
                    "location": "sess_a:0.0",
                    "command": "claude",
                    "cwd": "/repo",
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%2",
                    "location": "sess_b:0.0",
                    "command": "claude",
                    "cwd": "/repo",
                    "window_name": "claude",
                    "pane_active": "1",
                },
            ]
        )
        # Each session has exactly one `claude` window — not ambiguous. The
        # ambiguity flag is per-session, not global; cross-session same-name
        # windows are the *expected* shape under bare-`mozyo`.
        self.assertFalse(any(r.ambiguous for r in records))

    def test_infer_repo_root_walks_up_to_project_markers(self) -> None:
        from mozyo_bridge.domain.agent_discovery import infer_repo_root

        with tempfile.TemporaryDirectory() as tmp_str:
            repo = Path(tmp_str) / "repo"
            nested = repo / "src" / "deep" / "leaf"
            nested.mkdir(parents=True)
            (repo / "pyproject.toml").write_text("", encoding="utf-8")
            self.assertEqual(str(repo.resolve()), infer_repo_root(str(nested)))

    def test_infer_repo_root_returns_none_when_no_markers_above(self) -> None:
        from mozyo_bridge.domain.agent_discovery import infer_repo_root

        with tempfile.TemporaryDirectory() as tmp_str:
            no_markers = Path(tmp_str) / "no_markers"
            no_markers.mkdir(parents=True)
            self.assertIsNone(infer_repo_root(str(no_markers)))

    def test_filter_agents_session_and_kind(self) -> None:
        from mozyo_bridge.domain.agent_discovery import (
            discover_agents,
            filter_agents,
        )

        records = discover_agents(
            panes=[
                {
                    "id": "%1",
                    "location": "sess_a:0.0",
                    "command": "claude",
                    "cwd": "/repo",
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%2",
                    "location": "sess_b:0.0",
                    "command": "node",
                    "cwd": "/repo",
                    "window_name": "codex",
                    "pane_active": "1",
                },
            ]
        )
        self.assertEqual(
            ["%2"],
            [r.pane_id for r in filter_agents(records, session="sess_b")],
        )
        self.assertEqual(
            ["%1"],
            [r.pane_id for r in filter_agents(records, agent_kind="claude")],
        )

    def test_cmd_agents_list_text_output_emits_structured_columns(self) -> None:
        from mozyo_bridge.application.commands import cmd_agents_list

        panes = [
            {
                "id": "%1",
                "location": "sess_a:0.0",
                "command": "claude",
                "cwd": "/no/such/path",
                "window_name": "claude",
                "pane_active": "1",
            },
        ]
        args = argparse.Namespace(session=None, agent=None, as_json=False)
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.domain.agent_discovery.pane_lines",
                return_value=panes,
            ), \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, cmd_agents_list(args))
        output = stdout.getvalue()
        self.assertIn(
            "SESSION\tWINDOW\tIDX\tPANE\tACTIVE\tKIND\tPROCESS\tREPO_ROOT\tCWD\tAMBIGUOUS",
            output,
        )
        self.assertIn("sess_a\tclaude\t0\t%1\t1\tclaude\tclaude", output)

    def test_cmd_agents_list_json_output_carries_all_fields(self) -> None:
        from mozyo_bridge.application.commands import cmd_agents_list

        panes = [
            {
                "id": "%1",
                "location": "sess_a:0.0",
                "command": "claude",
                "cwd": "/no/such/path",
                "window_name": "claude",
                "pane_active": "1",
            },
        ]
        args = argparse.Namespace(session=None, agent=None, as_json=True)
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.domain.agent_discovery.pane_lines",
                return_value=panes,
            ), \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, cmd_agents_list(args))
        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, len(payload))
        record = payload[0]
        self.assertEqual("%1", record["pane_id"])
        self.assertEqual("sess_a", record["session"])
        self.assertEqual("claude", record["window_name"])
        self.assertEqual("claude", record["agent_kind"])
        self.assertEqual(True, record["pane_active"])
        self.assertEqual(False, record["ambiguous"])
        # repo_root is informational only; it is None when the pane cwd
        # cannot be walked to a PROJECT_MARKERS-bearing directory.
        self.assertIn("repo_root", record)

    def test_cmd_agents_list_rejects_unknown_agent_filter(self) -> None:
        from mozyo_bridge.application.commands import cmd_agents_list

        args = argparse.Namespace(session=None, agent="bogus", as_json=False)
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.domain.agent_discovery.pane_lines",
                return_value=[],
            ), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                cmd_agents_list(args)
        self.assertIn("--agent must be one of", stderr.getvalue())


class CrossWorkspaceHandoffGateTest(unittest.TestCase):
    """Cross-workspace handoff gate (Redmine #10332).

    Origin Codex must not deliver directly to a foreign workspace's Claude
    pane. The gate enforces: cross-session `--to claude` is rejected; the
    sender must route through the target session's Codex window with
    `--to codex`. The optional `--target-repo` flag adds a repo-mismatch
    fail-closed check on top.
    """

    def run_handoff(self, argv, pane, sender_session="local"):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []

        def fake_run_tmux(*tmux_args, check: bool = True):
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch(
                "mozyo_bridge.application.commands.capture_pane",
                return_value="",
            ), \
            patch(
                "mozyo_bridge.application.commands.run_tmux",
                side_effect=fake_run_tmux,
            ), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value=sender_session,
            ), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch(
                "mozyo_bridge.domain.pane_resolver.pane_lines",
                return_value=[pane],
            ), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as exit_ctx:
                args.func(args)
        return exit_ctx.exception, sent, stdout.getvalue(), stderr.getvalue()

    def test_cross_session_claude_handoff_is_rejected(self) -> None:
        # Origin lives in `local`, target pane lives in `other`; receiver is
        # claude — the gate must fail closed with `cross_session_claude` and
        # no tmux send-keys must be issued.
        pane = {
            "id": "%9",
            "location": "other:1.0",
            "command": "claude",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }
        _exc, sent, stdout, stderr = self.run_handoff(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "redmine",
                "--issue",
                "10332",
                "--journal",
                "49623",
                "--kind",
                "implementation_request",
                "--target",
                "%9",
                "--mode",
                "standard",
            ],
            pane=pane,
            sender_session="local",
        )

        # No tmux input was typed into the target pane — fail-closed before
        # any send-keys runs.
        self.assertFalse(
            any(
                call[:2] == ("send-keys", "-t")
                for call in sent
            ),
            f"unexpected send-keys: {sent}",
        )
        # Outcome JSON carries the new reason and no notification_marker
        # (no body was assembled past the gate).
        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("cross_session_claude", outcome["reason"])
        self.assertEqual("claude", outcome["receiver"])
        self.assertIn(
            "cross-session handoff to Claude is not allowed", stderr
        )

    def test_cross_session_codex_handoff_is_allowed_through_the_gate(self) -> None:
        # `--to codex` is the gateway path: routing into a foreign workspace
        # through that workspace's Codex window is what the gate permits.
        # The handoff dies later (no marker observed) under `standard`, but
        # NOT with `cross_session_claude`. Different `reason` proves the
        # cross-session gate let it through.
        pane = {
            "id": "%9",
            "location": "other:1.0",
            "command": "codex",
            "cwd": "/repo",
            "window_name": "codex",
            "pane_active": "1",
        }
        _exc, _sent, stdout, _stderr = self.run_handoff(
            [
                "handoff",
                "send",
                "--to",
                "codex",
                "--source",
                "redmine",
                "--issue",
                "10332",
                "--journal",
                "49623",
                "--kind",
                "review_request",
                "--target",
                "%9",
                "--mode",
                "standard",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            pane=pane,
            sender_session="local",
        )

        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        # The gate did NOT trigger. The handoff dies on marker_timeout
        # (strict mode, no marker observed) instead.
        self.assertEqual("blocked", outcome["status"])
        self.assertNotEqual("cross_session_claude", outcome["reason"])

    def test_same_session_claude_handoff_is_not_blocked_by_the_gate(self) -> None:
        # In-session `--to claude` is the existing window-only resolver path;
        # the cross-workspace gate must not regress it. The handoff itself
        # still dies on marker_timeout under strict standard mode.
        pane = {
            "id": "%9",
            "location": "local:1.0",
            "command": "claude",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }
        _exc, _sent, stdout, _stderr = self.run_handoff(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "redmine",
                "--issue",
                "10332",
                "--journal",
                "49623",
                "--kind",
                "implementation_request",
                "--target",
                "%9",
                "--mode",
                "standard",
                "--landing-timeout",
                "0.01",
                "--submit-delay",
                "0",
            ],
            pane=pane,
            sender_session="local",
        )

        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertNotEqual("cross_session_claude", outcome["reason"])

    def test_default_queue_enter_cross_session_codex_is_rejected_as_invalid_args(
        self,
    ) -> None:
        # Regression for Redmine #10332 review #49646. Cross-session `--to
        # codex` is the documented gateway path, but the v0.4 default
        # `queue-enter` rail rejects every cross-session target — including
        # `--to codex` — because its no-rollback contract is bound to the
        # sender's tmux session. Omitting `--mode` therefore breaks the
        # gateway send too. The CLI guidance (cross_session_claude
        # next_action and skill workflow) must point at `--mode standard`
        # explicitly, and this test pins the underlying queue-enter
        # invariant so the guidance does not drift.
        pane = {
            "id": "%9",
            "location": "other:1.0",
            "command": "codex",
            "cwd": "/repo",
            "window_name": "codex",
            "pane_active": "1",
        }
        _exc, sent, stdout, stderr = self.run_handoff(
            [
                "handoff",
                "send",
                "--to",
                "codex",
                "--source",
                "redmine",
                "--issue",
                "10332",
                "--journal",
                "49623",
                "--kind",
                "review_request",
                "--target",
                "%9",
                # No `--mode` → default queue-enter (since v0.4).
            ],
            pane=pane,
            sender_session="local",
        )

        # No tmux input typed before the rail rejects the cross-session
        # target.
        self.assertFalse(
            any(call[:2] == ("send-keys", "-t") for call in sent),
            f"unexpected send-keys: {sent}",
        )
        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertIn(
            "queue-enter requires the target pane to live in the sender's tmux session",
            stderr,
        )

    def test_cross_session_claude_outcome_guides_to_mode_standard(self) -> None:
        # Regression for Redmine #10332 review #49646. The recovery path
        # from a `cross_session_claude` block must steer the sender to
        # `--to codex --mode standard` (or `--mode pending`); naming
        # `--to codex` without `--mode` re-fails under the queue-enter
        # default. The next_action_for / outcome narrative / die() message
        # must all carry the explicit mode hint.
        pane = {
            "id": "%9",
            "location": "other:1.0",
            "command": "claude",
            "cwd": "/repo",
            "window_name": "claude",
            "pane_active": "1",
        }
        _exc, _sent, stdout, stderr = self.run_handoff(
            [
                "handoff",
                "send",
                "--to",
                "claude",
                "--source",
                "redmine",
                "--issue",
                "10332",
                "--journal",
                "49623",
                "--kind",
                "implementation_request",
                "--target",
                "%9",
                "--mode",
                "standard",
            ],
            pane=pane,
            sender_session="local",
        )

        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("cross_session_claude", outcome["reason"])
        # The structured outcome's next_action must spell out the mode flag
        # so the sender's next attempt does not re-fail under queue-enter
        # default. The die() trailer on stderr must carry the same hint.
        self.assertIn("--mode standard", outcome["next_action"])
        self.assertIn("--to codex", outcome["next_action"])
        self.assertIn("--mode standard", stderr)
        # The durable record (markdown) must repeat the mode hint so
        # auditors and downstream agents see it even when the structured
        # outcome is consumed and discarded.
        self.assertIn("--mode standard", stdout)

    def test_target_repo_mismatch_is_rejected(self) -> None:
        # `--target-repo` opts the sender into a repo-mismatch fail-closed
        # gate. When the target pane's cwd does not walk up to the named
        # repo root, the handoff is rejected before any send-keys.
        with tempfile.TemporaryDirectory() as tmp_str:
            expected_repo = Path(tmp_str) / "expected"
            other_repo = Path(tmp_str) / "other"
            (expected_repo / "src").mkdir(parents=True)
            (other_repo / "src").mkdir(parents=True)
            (expected_repo / "pyproject.toml").write_text("", encoding="utf-8")
            (other_repo / "pyproject.toml").write_text("", encoding="utf-8")

            pane = {
                "id": "%9",
                "location": "local:1.0",
                "command": "claude",
                "cwd": str(other_repo / "src"),
                "window_name": "claude",
                "pane_active": "1",
            }
            _exc, sent, stdout, stderr = self.run_handoff(
                [
                    "handoff",
                    "send",
                    "--to",
                    "claude",
                    "--source",
                    "redmine",
                    "--issue",
                    "10332",
                    "--journal",
                    "49623",
                    "--kind",
                    "implementation_request",
                    "--target",
                    "%9",
                    "--target-repo",
                    str(expected_repo),
                    "--mode",
                    "standard",
                ],
                pane=pane,
                sender_session="local",
            )

        self.assertFalse(
            any(call[:2] == ("send-keys", "-t") for call in sent),
            f"unexpected send-keys: {sent}",
        )
        json_lines = [
            line for line in stdout.splitlines() if line.strip().startswith("{")
        ]
        self.assertTrue(json_lines, f"no JSON outcome: {stdout!r}")
        outcome = json.loads(json_lines[-1])
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_repo_mismatch", outcome["reason"])
        self.assertIn("target pane is not in the expected repo", stderr)


class CodexAutonomousGuardrailLaneTest(unittest.TestCase):
    """Pin Repo-Local Guardrail Autonomous Lane wording (Redmine #10338).

    Product-wide policy distributed via the governed presets, recorded in
    the project-local rule doc, registered in the catalog, and surfaced in
    the root routers + canonical skill body. Each assertion below names the
    surface so a regression failure points at one file.
    """

    PRESET_AUTONOMOUS_LANE_MARKERS = (
        # Section heading must be present in distributed preset.
        "### Repo-Local Guardrail Autonomous Lane",
        # Default lane paths must be enumerated in the preset.
        "vibes/docs/rules/**",
        "vibes/docs/logics/**",
        "vibes/docs/specs/**",
        ".mozyo-bridge/docs/catalog.yaml",
        # Journal vocabulary and required fields must be named verbatim.
        "codex_autonomous_edit",
        "follow_up_review_required",
        "Codex Direct Edit Gate の carve-out",
        # Required verification command surface for catalog edits.
        "mozyo-bridge docs generate-file-conventions --check",
    )

    def _packaged_preset(self, preset: str) -> str:
        path = (
            ROOT
            / "src"
            / "mozyo_bridge"
            / "scaffold"
            / "presets"
            / preset
            / "agent-workflow.md"
        )
        self.assertTrue(path.is_file(), f"missing packaged preset: {path}")
        return path.read_text(encoding="utf-8")

    def _packaged_preset_version(self, preset: str) -> str:
        path = (
            ROOT
            / "src"
            / "mozyo_bridge"
            / "scaffold"
            / "presets"
            / preset
            / "VERSION"
        )
        return path.read_text(encoding="utf-8").strip()

    def test_redmine_governed_preset_ships_autonomous_lane(self) -> None:
        body = self._packaged_preset("redmine-governed")
        for marker in self.PRESET_AUTONOMOUS_LANE_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"redmine-governed preset is missing autonomous-lane "
                    f"marker {marker!r}; see Redmine #10338."
                ),
            )

    def test_redmine_rails_governed_preset_ships_autonomous_lane(self) -> None:
        body = self._packaged_preset("redmine-rails-governed")
        for marker in self.PRESET_AUTONOMOUS_LANE_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"redmine-rails-governed preset is missing autonomous-lane "
                    f"marker {marker!r}; see Redmine #10338."
                ),
            )

    def test_governed_preset_versions_were_bumped(self) -> None:
        # The autonomous-lane change is a workflow / guardrail change, so
        # both governed presets must be bumped beyond their last-hardened
        # 2026.05.25.1 version. The scaffold manifest's preset_hash drift
        # check is what enforces consumers re-install; this assertion pins
        # the version label so a future preset edit that forgets to bump
        # fails loudly.
        for preset in ("redmine-governed", "redmine-rails-governed"):
            version = self._packaged_preset_version(preset)
            self.assertNotEqual(
                "2026.05.25.1",
                version,
                msg=(
                    f"{preset} VERSION is still pre-#10338; the autonomous-lane "
                    f"distribution requires a bump."
                ),
            )

    def test_project_local_lane_doc_is_registered_in_catalog(self) -> None:
        catalog_text = (
            ROOT / ".mozyo-bridge" / "docs" / "catalog.yaml"
        ).read_text(encoding="utf-8")
        # Document entry must be registered so the resolver can pull the
        # policy from any lane path. file_convention must exist so the
        # autonomous-lane paths actually resolve to it.
        self.assertIn("id: rule-codex-autonomous-guardrail-lane", catalog_text)
        self.assertIn(
            "vibes/docs/rules/codex-autonomous-guardrail-lane.md",
            catalog_text,
        )
        self.assertIn("fc-codex-autonomous-guardrail-lane", catalog_text)
        # The lane policy file itself must exist (catalog references it).
        lane_doc = (
            ROOT / "vibes" / "docs" / "rules" / "codex-autonomous-guardrail-lane.md"
        )
        self.assertTrue(lane_doc.is_file(), f"lane doc missing: {lane_doc}")

    def test_lane_resolves_from_each_autonomous_path_via_catalog(self) -> None:
        # The lane policy must be reachable from every default-lane path
        # via `mozyo-bridge docs resolve`. We exercise the resolver
        # directly so a future catalog edit that drops the path coverage
        # fails this test, not just a manual `docs resolve` invocation.
        try:
            from mozyo_bridge.docs_tools import CatalogContext, resolve_paths
        except ImportError as exc:
            self.skipTest(f"docs_tools not importable: {exc}")
        context = CatalogContext.build(str(ROOT), None)
        for path in (
            "vibes/docs/rules/codex-autonomous-guardrail-lane.md",
            "vibes/docs/logics/scaffold-rules.md",
            "vibes/docs/specs/project-map.md",
            ".mozyo-bridge/docs/catalog.yaml",
        ):
            results = resolve_paths(context, [path])
            ids = {
                doc["id"]
                for entry in results
                for doc in entry.get("documents", [])
            }
            self.assertIn(
                "rule-codex-autonomous-guardrail-lane",
                ids,
                msg=(
                    f"`mozyo-bridge docs resolve {path}` did not surface the "
                    f"autonomous-lane rule; catalog file_convention coverage "
                    f"regressed."
                ),
            )

    def test_root_routers_name_autonomous_lane(self) -> None:
        for router_name in ("AGENTS.md", "CLAUDE.md"):
            body = (ROOT / router_name).read_text(encoding="utf-8")
            for marker in (
                "Repo-Local Guardrail Autonomous Lane",
                "codex_autonomous_edit",
                "vibes/docs/rules/codex-autonomous-guardrail-lane.md",
                # Routers must restate the gate-still-applies surfaces so
                # an autonomous-lane reader does not assume the carve-out
                # covers everything.
                "skills",
                "src/**",
            ):
                self.assertIn(
                    marker,
                    body,
                    msg=(
                        f"{router_name} is missing autonomous-lane marker "
                        f"{marker!r}; see Redmine #10338."
                    ),
                )

    def test_canonical_skill_reference_carries_autonomous_lane(self) -> None:
        body = (
            ROOT
            / "skills"
            / "mozyo-bridge-agent"
            / "references"
            / "workflow.md"
        ).read_text(encoding="utf-8")
        for marker in (
            "Repo-Local Guardrail Autonomous Lane",
            "codex_autonomous_edit",
            "lane: autonomous",
            "vibes/docs/rules/**",
            "vibes/docs/logics/**",
            "vibes/docs/specs/**",
            ".mozyo-bridge/docs/catalog.yaml",
        ):
            self.assertIn(
                marker,
                body,
                msg=(
                    f"skills/.../workflow.md is missing autonomous-lane "
                    f"marker {marker!r}; see Redmine #10338."
                ),
            )

    def test_plugin_skill_mirror_carries_autonomous_lane(self) -> None:
        # PluginMarketplaceTest already enforces byte equality, but a
        # marker check here points a future regression at "lane wording
        # missing from mirror" rather than a generic mirror-drift error.
        mirror = (
            ROOT
            / "plugins"
            / "mozyo-bridge-agent"
            / "skills"
            / "mozyo-bridge-agent"
            / "references"
            / "workflow.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Repo-Local Guardrail Autonomous Lane", mirror)
        self.assertIn("codex_autonomous_edit", mirror)

    def test_readme_advertises_autonomous_lane(self) -> None:
        body = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Repo-Local Guardrail Autonomous Lane", body)
        self.assertIn("codex_autonomous_edit", body)
        self.assertIn("vibes/docs/rules/codex-autonomous-guardrail-lane.md", body)


class DocsAuditImpactDirtyFileTest(unittest.TestCase):
    """Pin docs audit-impact + --check-generated behavior on unrelated dirty files.

    Workflow-change verification target for Redmine #10338 lane policy
    (parent #10338, this task #10344). `mozyo-bridge docs audit-impact
    --all-changed --check-generated` must surface every git-changed path,
    including unrelated dirty files that the catalog does not map to any
    document, while still returning 0 when the generated drift check is
    clean. Otherwise the operator pre-commit gate would block every
    commit that happens to share a worktree with one stray untracked
    file (e.g., `.claude/settings.local.json`), and the
    `codex_autonomous_edit` verification command list in the lane
    policy would be impossible to run cleanly.

    The test sets up a real `git init` repo with a catalog +
    fresh-regenerated file_conventions, drops an untracked file outside
    every file_convention pattern, and drives `cmd_docs_audit_impact`
    end-to-end.
    """

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def _run_git(self, repo: Path, *cmd: str) -> None:
        subprocess.run(
            ["git", *cmd],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_audit_impact_returns_clean_on_unrelated_dirty_file_when_generated_check_passes(
        self,
    ) -> None:
        import shutil as _shutil

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            # Bring up a governed scaffold so the catalog skeleton ships.
            self._run_cli(["rules", "install", "--home", str(home)])
            self._run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            # The skeleton is the safe minimal catalog to drive resolver +
            # generator against; we promote it to the live catalog as the
            # docs `## Quick Start` invocation does.
            example = project / ".mozyo-bridge" / "docs" / "catalog.yaml.example"
            catalog = project / ".mozyo-bridge" / "docs" / "catalog.yaml"
            _shutil.copyfile(example, catalog)

            # Regenerate the generated file so the drift check is clean on
            # the first audit-impact call. Without this the test would
            # measure missing-output behavior instead of the dirty-file
            # interaction.
            gen_code, _ = self._run_cli(
                [
                    "docs",
                    "generate-file-conventions",
                    "--repo",
                    str(project),
                ]
            )
            self.assertEqual(0, gen_code)

            # Initialize a git repo so `audit_doc_impact` can read the
            # all-changed listing via `git ls-files --others --exclude-standard`.
            self._run_git(project, "init", "--initial-branch=main")
            self._run_git(project, "config", "user.email", "test@example.invalid")
            self._run_git(project, "config", "user.name", "Test")
            # Commit the scaffold so subsequent untracked files are the
            # only unstaged work; otherwise every scaffold file would
            # also report and noise the assertion.
            self._run_git(project, "add", ".")
            self._run_git(project, "commit", "-m", "scaffold")

            # Drop an unrelated dirty file. It is intentionally outside
            # every governed-preset file_convention so the resolver
            # surfaces `documents_to_read: - none`, mirroring the real
            # `.claude/settings.local.json` operator pattern that prompted
            # this verification (see Redmine #10338 review #49720 note 3
            # and #49743 note 4 — both treated such a dirty file as
            # unrelated).
            unrelated = project / "untracked_notes.txt"
            unrelated.write_text("scratch\n", encoding="utf-8")

            code, output = self._run_cli(
                [
                    "docs",
                    "audit-impact",
                    "--all-changed",
                    "--check-generated",
                    "--repo",
                    str(project),
                ]
            )

            # Contract pin: exit 0 even when an unrelated dirty file is
            # reported, provided --check-generated is clean.
            self.assertEqual(0, code, msg=output)
            # The unrelated file MUST appear in the output — surfacing it
            # is the whole point of `--all-changed`; silently swallowing
            # the path would be the real regression.
            self.assertIn("[untracked_notes.txt]", output)
            self.assertIn("documents_to_read:", output)
            # `documents_to_read: - none` is the expected shape for an
            # unrelated path; if a future catalog edit accidentally
            # broadened a file_convention to catch `untracked_notes.txt`,
            # this assertion would tighten the test before it could rot.
            self.assertIn("- none", output)
            # The generated check trailer must confirm cleanliness so the
            # 0 exit was not from missing-output suppression.
            self.assertIn("is up to date", output)


class SkillCrossWorkspaceGuidanceTest(unittest.TestCase):
    """Pin the #10332 cross-workspace `--mode` guidance in the skill body.

    The canonical skill workflow and its plugin mirror previously regressed
    to the pre-correction wording (Redmine #10338 review #49720). The
    `cross_session_claude` recovery path must always name `--mode standard`
    (or `--mode pending`) because the default `queue-enter` rail rejects
    every cross-session target. README and the runtime CLI already agree;
    this test pins the skill side so a future rule-edit cannot silently
    drop the mode hint again.
    """

    REQUIRED_GUIDANCE_MARKERS = (
        # Cross-Workspace Handoff section heading must be present.
        "## Cross-Workspace Handoff",
        # The gateway-path bullet must spell out the mode flag verbatim.
        "--mode standard",
        "--mode pending",
        # The reason the mode flag is required must remain in the bullet.
        "queue-enter",
        # The window-only resolver naming convention for the gateway
        # target must stay so callers can copy-paste it.
        "--to codex --target <target_session>:codex",
    )

    def _skill_workflow_body(self, *parts: str) -> str:
        return (ROOT.joinpath(*parts) / "references" / "workflow.md").read_text(
            encoding="utf-8"
        )

    def test_canonical_skill_keeps_mode_standard_gateway_guidance(self) -> None:
        body = self._skill_workflow_body("skills", "mozyo-bridge-agent")
        for marker in self.REQUIRED_GUIDANCE_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"skills/mozyo-bridge-agent/references/workflow.md is "
                    f"missing #10332 marker {marker!r}; gateway guidance "
                    f"would re-fail under default queue-enter."
                ),
            )

    def test_plugin_mirror_keeps_mode_standard_gateway_guidance(self) -> None:
        body = self._skill_workflow_body(
            "plugins", "mozyo-bridge-agent", "skills", "mozyo-bridge-agent"
        )
        for marker in self.REQUIRED_GUIDANCE_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"plugin skill mirror is missing #10332 marker "
                    f"{marker!r}; sync_plugin_skill.sh drift or upstream "
                    f"canonical regressed."
                ),
            )


class SkillWorkflowSemanticAnchorsTest(unittest.TestCase):
    """Pin Redmine #10663: broaden semantic anchors beyond #10332.

    `PluginMarketplaceTest::test_plugin_skill_mirror_matches_canonical`
    detects *byte* drift between the canonical skill body and the
    plugin mirror. `SkillCrossWorkspaceGuidanceTest` pins the #10332
    cross-workspace marker subset.

    This class extends the semantic anchor set to cover the rest of
    the workflow body's load-bearing sections — handoff lifecycle,
    role boundary, Codex direct-edit gate, autonomous lane, audit-
    owned commit authority, workflow-change verification. A future
    canonical edit that quietly drops one of these sections passes the
    byte-drift gate (canonical + mirror in sync) but would still need
    to clear this test, so a single missing marker fails CI loudly.

    Markers are deliberately verbatim substrings from the canonical
    body. Wording changes that intentionally rename a section MUST
    update this list in the same commit; the explicit failure surfaces
    the intent.
    """

    SECTION_MARKERS: tuple[str, ...] = (
        # Major section headings — drop any of these and the workflow
        # body has lost a primary topic.
        "## Start Of Work",
        "## Ticket-ID Entrypoint",
        "## Ticket System Conventions",
        "## Handoff Lifecycle",
        "## Cross-Workspace Handoff",
        "## Claude / Codex Role Boundary",
        "## Policy / Skill Authoring Boundary",
        "### Repo-Local Guardrail Autonomous Lane",
        "## Audit-Owned Commit Authority",
        "## Workflow Change Verification",
    )

    PHRASE_MARKERS: tuple[str, ...] = (
        # Role boundary — Claude implements, Codex audits, and the
        # gateway can't be reframed by short imperatives.
        "Claude owns implementation for normal development tasks",
        "Codex does not directly implement normal development tasks",
        "are not by themselves authorization for Codex to perform a direct edit",
        # Codex direct-edit gate vocabulary (Redmine path).
        "`codex_direct_edit` gate journal",
        "role: 実装者",
        "direct_edit: true",
        "allowed_paths",
        # Autonomous lane — the carve-out and its required journal.
        "Repo-Local Guardrail Autonomous Lane",
        "codex_autonomous_edit",
        "vibes/docs/rules/**",
        "vibes/docs/logics/**",
        "vibes/docs/specs/**",
        # Audit-owned commit authority — close approval separation
        # and the per-system commit message contracts must stay
        # verbatim so operators can copy-paste them.
        "Audit-Owned Commit Authority",
        "Codex audit-owned commit",
        "Refs: Redmine #<issue_id>",
        "Journal: <journal_id>",
        "Refs: Asana task <task_id>",
        "Audit: Asana comment <comment_id>",
        # Close-Approval-Separation reminder pulled from the central
        # preset is the load-bearing distinction between Review Gate
        # and Close Gate.
        "Review approval alone is not close approval",
        "owner close approval journal",
        # Handoff Lifecycle vocabulary — durable record is the source
        # of truth, pane is a pointer.
        "the durable source of truth",
        "pane notification is still only the pointer",
        # Workflow Change Verification policy.
        "Workflow Change Verification",
        "Claude implements the normal development task",
        # Redmine default-project resolution (Redmine #10689). The
        # workspace-local snippet path and the "explicit wins over
        # default" / "UNVERIFIED escalates" rules must stay in the
        # skill body so agents pick them up at session start.
        "Default project resolution",
        ".mozyo-bridge/redmine-defaults.md",
        ".mozyo-bridge/workspace-defaults.yaml",
        "An explicit `project_id` always wins over the default",
        "UNVERIFIED",
    )

    SKILL_PATH = (
        "skills",
        "mozyo-bridge-agent",
        "references",
        "workflow.md",
    )
    PLUGIN_MIRROR_PATH = (
        "plugins",
        "mozyo-bridge-agent",
        "skills",
        "mozyo-bridge-agent",
        "references",
        "workflow.md",
    )

    def _body(self, *parts: str) -> str:
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def _check_markers(self, body: str, *, label: str) -> None:
        for marker in self.SECTION_MARKERS + self.PHRASE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(
                    marker,
                    body,
                    msg=(
                        f"{label} is missing workflow semantic anchor "
                        f"{marker!r}. Either the canonical skill body lost a "
                        f"load-bearing section / phrase, or this anchor list "
                        f"needs an intentional update in the same commit."
                    ),
                )

    def test_canonical_skill_carries_workflow_semantic_anchors(self) -> None:
        self._check_markers(
            self._body(*self.SKILL_PATH),
            label="skills/mozyo-bridge-agent/references/workflow.md",
        )

    def test_plugin_mirror_carries_workflow_semantic_anchors(self) -> None:
        self._check_markers(
            self._body(*self.PLUGIN_MIRROR_PATH),
            label="plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md",
        )


class CanonicalRendererTest(unittest.TestCase):
    """Cover the Redmine #10345 single-source conditional renderer.

    The canonical YAML under `src/mozyo_bridge/scaffold/canonical_sources/`
    is the source of truth for the router pair templates at
    `src/mozyo_bridge/scaffold/presets/_router/{AGENTS,CLAUDE}.md`. These
    tests pin:

    - byte-equal round-trip between canonical render and the committed
      template files (drift is the only thing `--check` should report);
    - tool-conditional dispatch (codex vs claude fragments land in the
      right output);
    - Project-Local Additions marker preservation through the render
      pipeline so the downstream `apply_project_local_preservation` in
      `scaffold.rules` continues to work;
    - the CLI `scaffold canonical [--check]` surface returns the
      expected exit codes on clean state, drift, and missing files.
    """

    SOURCE_RELATIVE = Path("src/mozyo_bridge/scaffold/canonical_sources/router.yaml")
    AGENTS_RELATIVE = Path("src/mozyo_bridge/scaffold/presets/_router/AGENTS.md")
    CLAUDE_RELATIVE = Path("src/mozyo_bridge/scaffold/presets/_router/CLAUDE.md")
    BEGIN_MARKER = "<!-- mozyo-bridge:project-local-additions:begin -->"
    END_MARKER = "<!-- mozyo-bridge:project-local-additions:end -->"

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = args.func(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_committed_templates_match_canonical_render(self) -> None:
        from mozyo_bridge.scaffold.canonical import collect_render_results

        results = collect_render_results(ROOT)
        self.assertGreater(len(results), 0, "expected at least one canonical output")
        for result in results:
            self.assertEqual(
                result.rendered,
                result.on_disk,
                msg=(
                    f"{result.output_path} drifted from canonical source "
                    f"{result.source_id!r}; rerun `mozyo-bridge scaffold "
                    f"canonical` (no flag = render) and recommit."
                ),
            )

    def test_conditional_dispatch_isolates_tool_specific_fragments(self) -> None:
        from mozyo_bridge.scaffold.canonical import (
            load_canonical_source,
            render_for_context,
        )

        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)
        codex = render_for_context(source, {"tool": "codex"})
        claude = render_for_context(source, {"tool": "claude"})

        # Title and tool intro split. Codex carries the cross-tool agents.md
        # framing; Claude does not. Claude carries the ClaudeCode reminder
        # heading; Codex does not.
        self.assertIn("# AGENTS (Codex 入口)", codex)
        self.assertNotIn("# Claude Code Router", codex)
        self.assertIn("cross-tool agents.md", codex)

        self.assertIn("# Claude Code Router", claude)
        self.assertNotIn("# AGENTS (Codex 入口)", claude)
        self.assertIn("ClaudeCode 起動時の最小 reminder", claude)
        self.assertNotIn("## Preset", claude)

        # Codex body holds the Preset + Guardrails block; Claude body does
        # not. Each side keeps the other tool's body out.
        self.assertIn("## Preset", codex)
        self.assertIn("## Guardrails", codex)
        self.assertNotIn("ClaudeCode 起動時の最小 reminder", codex)

        # The shared session-start opening (steps 1-2 + `${rule_path}`) is
        # byte-shared between both renders.
        shared_opening = (
            "## セッション開始\n\n"
            "1. 現在の working directory がこの project root またはその配下であることを確認する。\n"
            "2. mozyo-bridge の central preset rules を読む:\n"
            "   - `${rule_path}`\n"
        )
        self.assertIn(shared_opening, codex)
        self.assertIn(shared_opening, claude)

    def test_render_preserves_project_local_marker_pair(self) -> None:
        from mozyo_bridge.scaffold.canonical import (
            load_canonical_source,
            render_for_context,
        )

        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)
        for tool in ("codex", "claude"):
            with self.subTest(tool=tool):
                rendered = render_for_context(source, {"tool": tool})
                begin = rendered.find(self.BEGIN_MARKER)
                end = rendered.find(self.END_MARKER)
                self.assertNotEqual(
                    -1,
                    begin,
                    msg=f"{tool}: begin marker missing from canonical render",
                )
                self.assertNotEqual(
                    -1,
                    end,
                    msg=f"{tool}: end marker missing from canonical render",
                )
                self.assertLess(
                    begin,
                    end,
                    msg=f"{tool}: marker pair is out of order in canonical render",
                )

    def test_check_clean_then_drift_then_recover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(
                ROOT / "src",
                repo / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )

            # Pristine copy: --check passes.
            result, stdout, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(0, result, msg=stdout + stderr)
            self.assertIn("AGENTS.md is up to date", stdout)
            self.assertIn("CLAUDE.md is up to date", stdout)
            self.assertEqual("", stderr)

            # Mutating the committed template must surface drift.
            agents_path = repo / self.AGENTS_RELATIVE
            agents_path.write_text(
                agents_path.read_text(encoding="utf-8") + "\nDRIFT MARKER\n",
                encoding="utf-8",
            )
            result, stdout, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            self.assertIn("is out of date", stderr)
            self.assertIn("AGENTS.md", stderr)

            # `render` (no --check) rewrites the file from canonical source.
            result, stdout, stderr = self.run_cli(
                ["scaffold", "canonical", "--repo", str(repo)]
            )
            self.assertEqual(0, result, msg=stdout + stderr)

            # And the next --check is clean again.
            result, stdout, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(0, result, msg=stdout + stderr)
            self.assertEqual("", stderr)

    def test_drift_recovery_message_names_only_valid_subcommand(self) -> None:
        """Pin the drift stderr message to a runnable CLI invocation.

        Codex review #49845 caught the regression where the recovery
        message named `mozyo-bridge scaffold canonical render` — a
        non-existent sub-subcommand (the actual surface is `scaffold
        canonical` for render and `scaffold canonical --check` for the
        gate). A drifted router would still fail correctly, but a copy-
        pasted recovery command would error out with `unrecognized
        arguments: render`, defeating the operator-recovery half of the
        review focus "drift detection の実用性".
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(
                ROOT / "src",
                repo / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            agents_path = repo / self.AGENTS_RELATIVE
            agents_path.write_text(
                agents_path.read_text(encoding="utf-8") + "\nDRIFT\n",
                encoding="utf-8",
            )
            result, _, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            # The valid invocation must appear in the recovery hint so an
            # operator can copy-paste it.
            self.assertIn(
                "mozyo-bridge scaffold canonical",
                stderr,
                msg="drift stderr must name the actual `scaffold canonical` CLI",
            )
            # And the invalid `canonical render` shape must not be
            # reintroduced. Use a substring check that allows
            # `scaffold canonical` (alone) and `scaffold canonical --check`
            # but rejects the sub-subcommand wording explicitly.
            self.assertNotIn(
                "canonical render",
                stderr,
                msg=(
                    "drift stderr regressed to the invalid sub-subcommand "
                    "wording; `scaffold canonical render` is not a real CLI"
                ),
            )
            self.assertNotIn(
                "canonical check",
                stderr,
                msg=(
                    "drift stderr names a non-existent `canonical check` "
                    "sub-subcommand; the real surface is `--check` flag"
                ),
            )

    def test_check_reports_missing_output_as_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(
                ROOT / "src",
                repo / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            (repo / self.CLAUDE_RELATIVE).unlink()
            result, stdout, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            self.assertIn("CLAUDE.md is missing", stderr)

    def test_canonical_render_survives_body_file_edit(self) -> None:
        """A canonical body-file edit must show up in the rendered output.

        Concretely: editing a body file rotates the canonical render and
        the committed `_router/*.md` template stops matching. This
        confirms the body files are the source of truth — not a stale
        copy that happens to share content.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(
                ROOT / "src",
                repo / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            body_path = (
                repo
                / "src/mozyo_bridge/scaffold/canonical_sources/router/bodies/title_codex.md"
            )
            body_path.write_text(
                body_path.read_text(encoding="utf-8") + "EDITED\n",
                encoding="utf-8",
            )

            # --check must detect AGENTS.md drift (codex output); CLAUDE.md
            # stays clean because the edit only touches a codex-when fragment.
            result, _, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            self.assertIn("AGENTS.md", stderr)
            self.assertNotIn("CLAUDE.md", stderr)

            # `render` rewrites AGENTS.md so the new body lands on disk.
            result, _, _ = self.run_cli(
                ["scaffold", "canonical", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            updated = (repo / self.AGENTS_RELATIVE).read_text(encoding="utf-8")
            self.assertIn("EDITED", updated)

    def test_check_fails_when_canonical_source_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "src/mozyo_bridge/scaffold").mkdir(parents=True)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["scaffold", "canonical", "--check", "--repo", str(repo)]
                )


class GovernedWorkflowCanonicalTest(unittest.TestCase):
    """Pin Redmine #10426: governed preset agent-workflow.md canonicalization.

    `governed-workflow.yaml` renders both `redmine-governed/agent-workflow.md`
    and `redmine-rails-governed/agent-workflow.md` from a single body file
    plus per-output `{{name}}` substitutions. These tests pin:

    - byte-equal render for both governed presets (drift gate);
    - critical workflow keywords survive in both renders (gate / role /
      autonomous lane / cross-workspace / close approval), so a future
      placeholder rename or fragment edit cannot silently drop a gate or
      a role boundary clause;
    - the substitution engine itself: undefined placeholders die loudly
      and missing-substitutions-with-placeholders fails before write.
    """

    SOURCE_RELATIVE = Path(
        "src/mozyo_bridge/scaffold/canonical_sources/governed-workflow.yaml"
    )
    REDMINE_GOVERNED_RELATIVE = Path(
        "src/mozyo_bridge/scaffold/presets/redmine-governed/agent-workflow.md"
    )
    REDMINE_RAILS_GOVERNED_RELATIVE = Path(
        "src/mozyo_bridge/scaffold/presets/redmine-rails-governed/agent-workflow.md"
    )

    # Semantic anchors that MUST appear in both governed renders. If a
    # future canonical edit drops any of these, governance behavior
    # silently weakens — exactly the drift this canonicalization is
    # meant to prevent. Each marker is a verbatim substring pulled from
    # the workflow body; quoting is preserved so a partial-rename does
    # not pass.
    GOVERNED_KEYWORD_MARKERS: tuple[str, ...] = (
        # Gate vocabulary — separation of Implementation Done vs Close
        # is the governed preset's central promise.
        "Implementation Done は completion ではない",
        "Review Gate approval も Close ではない",
        "owner_close_approval",
        "Close Approval Separation",
        # Role boundary — implementer vs auditor distinction must stay
        # legible in the preset body.
        "claude_code: 実装者",
        "codex: 監査者",
        "owner: 最終判断者",
        # Codex Direct Edit Gate — the gate-vs-short-imperative
        # distinction is the wording that prior #10332 / #10338 reviews
        # required to stay verbatim.
        "Codex Direct Edit Gate",
        "短い命令は file edit 許可ではない",
        "Repo-Local Guardrail Autonomous Lane",
        "codex_autonomous_edit",
        # Docs catalog governance contract.
        "catalog 駆動の docs 解決",
        ".mozyo-bridge/docs/catalog.yaml",
        # The recovery wording corrected in #10345 must stay verifiable.
        "mozyo-bridge docs generate-file-conventions",
    )

    def _governed_outputs(self):
        from mozyo_bridge.scaffold.canonical import (
            load_canonical_source,
            render_output,
        )

        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)
        return {
            output.target.name: render_output(source, output)
            for output in source.outputs
        }

    def test_both_governed_outputs_match_canonical_render(self) -> None:
        from mozyo_bridge.scaffold.canonical import collect_render_results

        results_by_target = {
            result.output_path.relative_to(ROOT).as_posix(): result
            for result in collect_render_results(ROOT)
        }
        for relative in (
            self.REDMINE_GOVERNED_RELATIVE,
            self.REDMINE_RAILS_GOVERNED_RELATIVE,
        ):
            with self.subTest(target=relative.as_posix()):
                key = relative.as_posix()
                self.assertIn(key, results_by_target)
                result = results_by_target[key]
                self.assertEqual(
                    result.rendered,
                    result.on_disk,
                    msg=(
                        f"{relative.as_posix()} drifted from canonical source; "
                        f"rerun `mozyo-bridge scaffold canonical` and recommit."
                    ),
                )

    def test_governed_renders_carry_framework_specific_phrases(self) -> None:
        """Confirm the per-output `substitutions` actually swap framework markers.

        This is the inter-preset drift gate: if `redmine-governed` ever
        accidentally renders Rails-only paths (or vice versa), the
        canonical source has lost its conditional contract.
        """
        rendered = self._governed_outputs()
        rg = rendered["agent-workflow.md"]  # there are two; reload by preset

        # Re-resolve by full path keys to disambiguate the two outputs.
        from mozyo_bridge.scaffold.canonical import (
            load_canonical_source,
            render_output,
        )

        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)
        by_preset = {}
        for output in source.outputs:
            preset_name = output.target.parts[-2]
            by_preset[preset_name] = render_output(source, output)
        del rg

        redmine = by_preset["redmine-governed"]
        rails = by_preset["redmine-rails-governed"]

        # Title: each preset names itself.
        self.assertIn("# Redmine Governed Agent Workflow", redmine)
        self.assertNotIn("# Redmine Governed Agent Workflow", rails)
        self.assertIn("# Redmine Rails Governed Agent Workflow", rails)

        # Implementation paths: redmine ships generic; rails ships Rails layout.
        self.assertIn("    - src/**", redmine)
        self.assertIn("    - tests/**", redmine)
        self.assertNotIn("    - app/**", redmine)
        self.assertNotIn("    - spec/**", redmine)

        self.assertIn("    - app/**", rails)
        self.assertIn("    - spec/**", rails)
        self.assertNotIn("    - src/**", rails)
        self.assertNotIn("    - tests/**", rails)

        # Layered Source: rails references both base layers.
        self.assertIn(
            "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md`",
            redmine,
        )
        self.assertIn(
            "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md`",
            rails,
        )
        self.assertIn(
            "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-rails/agent-workflow.md`",
            rails,
        )
        self.assertNotIn("redmine-rails/agent-workflow.md", redmine)

        # Required-Verification framework-specific commands.
        self.assertIn("project の authoritative test command", redmine)
        self.assertNotIn("bundle exec rspec", redmine)
        self.assertIn("bundle exec rspec", rails)
        self.assertIn("rubocop / brakeman", rails)

        # Governed Mode Prohibitions last bullet names the base preset.
        self.assertIn(
            "shared preset の `redmine` だけで完了報告すること",
            redmine,
        )
        self.assertIn(
            "shared preset の `redmine-rails` だけで完了報告すること",
            rails,
        )

    def test_both_governed_outputs_preserve_governance_keywords(self) -> None:
        rendered = {}
        from mozyo_bridge.scaffold.canonical import (
            load_canonical_source,
            render_output,
        )

        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)
        for output in source.outputs:
            rendered[output.target.parts[-2]] = render_output(source, output)

        for preset_name, body in rendered.items():
            for marker in self.GOVERNED_KEYWORD_MARKERS:
                with self.subTest(preset=preset_name, marker=marker):
                    self.assertIn(
                        marker,
                        body,
                        msg=(
                            f"{preset_name}/agent-workflow.md lost governance "
                            f"marker {marker!r}; a substitution or fragment "
                            f"edit silently weakened the preset body."
                        ),
                    )

    def test_unresolved_placeholder_dies_loudly(self) -> None:
        """An undefined `{{name}}` placeholder must fail before any write."""
        from mozyo_bridge.scaffold.canonical import (
            CanonicalSource,
            Fragment,
            OutputSpec,
            render_output,
        )

        source = CanonicalSource(
            id="probe",
            source_path=Path("/tmp/probe.yaml"),
            outputs=(
                OutputSpec(
                    target=Path("probe.md"),
                    context={},
                    substitutions={"GOOD": "value"},
                ),
            ),
            fragments=(
                Fragment(id="f", when={}, body="hello {{GOOD}} and {{MISSING}}\n"),
            ),
        )
        with self.assertRaises(SystemExit):
            render_output(source, source.outputs[0])

    def test_placeholder_without_substitutions_mapping_dies(self) -> None:
        """A body with `{{name}}` but no `substitutions` mapping must die."""
        from mozyo_bridge.scaffold.canonical import (
            CanonicalSource,
            Fragment,
            OutputSpec,
            render_output,
        )

        source = CanonicalSource(
            id="probe",
            source_path=Path("/tmp/probe.yaml"),
            outputs=(OutputSpec(target=Path("probe.md"), context={}, substitutions={}),),
            fragments=(
                Fragment(id="f", when={}, body="hello {{ANY}}\n"),
            ),
        )
        with self.assertRaises(SystemExit):
            render_output(source, source.outputs[0])

    def test_substitution_is_single_pass(self) -> None:
        """A value containing a placeholder string must NOT be re-substituted.

        Without this guarantee, a substitution value of `{{A}}` would
        recurse into `A`'s mapping, producing surprising output.
        Single-pass keeps the renderer deterministic.
        """
        from mozyo_bridge.scaffold.canonical import (
            CanonicalSource,
            Fragment,
            OutputSpec,
            render_output,
        )

        source = CanonicalSource(
            id="probe",
            source_path=Path("/tmp/probe.yaml"),
            outputs=(
                OutputSpec(
                    target=Path("probe.md"),
                    context={},
                    substitutions={"A": "{{B}}", "B": "REPLACED"},
                ),
            ),
            fragments=(Fragment(id="f", when={}, body="A is {{A}}\n"),),
        )
        rendered = render_output(source, source.outputs[0])
        self.assertEqual("A is {{B}}\n", rendered)
        self.assertNotIn("REPLACED", rendered)


class ReleaseCheckDriftTest(unittest.TestCase):
    """Pin Redmine #10688: `mozyo-bridge release check drift` runs both
    pre-existing drift gates and strict-fails on either side.

    The unittest suite already gates each drift surface independently:
    - `CanonicalRendererTest::test_committed_templates_match_canonical_render`
      and `GovernedWorkflowCanonicalTest::test_both_governed_outputs_match_canonical_render`
      for `scaffold canonical --check`;
    - `PluginMarketplaceTest::test_plugin_skill_mirror_matches_canonical`
      and `test_sync_script_check_mode_*` for the plugin mirror.

    This class pins the *release helper* surface: the operator-facing
    command that bundles both checks into one call (mirroring the
    `release check tree` / `release check scaffold` / `release check
    artifact` pattern). A future helper edit that, for example, swallows
    a sub-check's non-zero exit and reports `result: clean` would slip
    past the per-surface tests but fails here.
    """

    SOURCE_TREE_PATHS = (
        Path("src/mozyo_bridge"),
        Path("scripts/sync_plugin_skill.sh"),
        Path("skills/mozyo-bridge-agent"),
        Path("plugins/mozyo-bridge-agent"),
        Path("vibes/docs/logics"),
        Path(".mozyo-bridge/docs/catalog.yaml"),
        Path(".mozyo-bridge/docs/file_conventions.generated.yaml"),
        Path(".mozyo-bridge/scaffold.json"),
        Path("AGENTS.md"),
        Path("CLAUDE.md"),
        Path("pyproject.toml"),
        Path("README.md"),
        Path(".claude-plugin"),
    )

    def _stage_repo(self, dest: Path) -> Path:
        """Copy just the slices the drift helper needs into ``dest``.

        Copying the full repo is wasteful when the helper only consumes
        the source tree, canonical sources, presets, scaffold, sync
        script, skill body, plugin mirror, and the docs catalog. A
        minimal stage also keeps the test fast.
        """
        for relative in self.SOURCE_TREE_PATHS:
            src = ROOT / relative
            if not src.exists():
                continue
            target = dest / relative
            if src.is_dir():
                shutil.copytree(
                    src,
                    target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
        return dest

    def _run_helper(self, repo: Path) -> tuple[int, str, str]:
        parser = build_parser()
        args = parser.parse_args(
            ["release", "check", "drift", "--repo", str(repo)]
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = args.func(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_clean_tree_exits_zero_and_reports_both_checks(self) -> None:
        result, stdout, stderr = self._run_helper(ROOT)
        self.assertEqual(0, result, msg=stdout + stderr)
        # Both sub-check section headers must appear so operators can
        # see what ran without re-reading the source.
        self.assertIn("scaffold canonical --check", stdout)
        self.assertIn("sync_plugin_skill.sh --check", stdout)
        # Both sub-checks must report up-to-date on a clean tree.
        self.assertIn("AGENTS.md is up to date", stdout)
        self.assertIn("plugin skill mirror is up to date", stdout)
        self.assertIn("result: clean", stdout)

    def test_canonical_drift_causes_strict_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            agents = repo / "src/mozyo_bridge/scaffold/presets/_router/AGENTS.md"
            agents.write_text(
                agents.read_text(encoding="utf-8") + "\nDRIFT\n",
                encoding="utf-8",
            )
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("AGENTS.md is out of date", stdout)
            self.assertIn("result: blocker", stdout)
            # Recovery hint must name the real CLI verbatim so the
            # operator can copy-paste from the release-flow doc.
            self.assertIn("mozyo-bridge scaffold canonical", stdout)
            # The mirror check must still have run; its section header
            # is the proof.
            self.assertIn("sync_plugin_skill.sh --check", stdout)

    def test_mirror_drift_causes_strict_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            mirror = (
                repo
                / "plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md"
            )
            mirror.write_text(
                mirror.read_text(encoding="utf-8") + "\nDRIFT\n",
                encoding="utf-8",
            )
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("plugin skill mirror drift detected", stdout)
            self.assertIn("result: blocker", stdout)
            # Recovery hint must be repo-root runnable per Codex review
            # #50344 (correction landed in #10663 commit 867396a).
            self.assertIn("scripts/sync_plugin_skill.sh", stdout)
            self.assertIn("from the repo root", stdout)
            # The canonical check must still have run on the same
            # invocation; failing fast on one side without reporting
            # the other defeats the bundled-helper purpose.
            self.assertIn("scaffold canonical --check", stdout)

    def test_helper_reports_both_drifts_in_one_run(self) -> None:
        """When both sides drift, the operator sees both findings in
        one run rather than chasing two separate failures."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            agents = repo / "src/mozyo_bridge/scaffold/presets/_router/AGENTS.md"
            agents.write_text(
                agents.read_text(encoding="utf-8") + "\nDRIFT-A\n",
                encoding="utf-8",
            )
            mirror = (
                repo
                / "plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md"
            )
            mirror.write_text(
                mirror.read_text(encoding="utf-8") + "\nDRIFT-B\n",
                encoding="utf-8",
            )
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("AGENTS.md is out of date", stdout)
            self.assertIn("plugin skill mirror drift detected", stdout)
            # Two blocker bullets, one per side.
            self.assertIn("scaffold canonical drift detected", stdout)
            self.assertIn("plugin skill mirror drift detected", stdout)

    def test_missing_sync_script_is_release_blocker(self) -> None:
        """The helper must fail loudly when the sync script is absent,
        not silently pass the mirror gate."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo")
            (repo / "scripts/sync_plugin_skill.sh").unlink()
            result, stdout, _stderr = self._run_helper(repo)
            self.assertEqual(1, result)
            self.assertIn("missing sync script", stdout)
            self.assertIn("result: blocker", stdout)


class WorkspaceDefaultsRendererTest(unittest.TestCase):
    """Pin Redmine #10689: workspace-local Redmine default-project renderer.

    Single source: `<repo>/.mozyo-bridge/workspace-defaults.yaml`.
    Default output: `<repo>/.mozyo-bridge/redmine-defaults.md`.

    Tests pin:
    - clean repo (mozyo_bridge itself) round-trips byte-equal through
      the renderer (the committed output IS the canonical render);
    - drift detection (mutation, missing-output, body-edit recovery);
    - schema validation (missing required fields, malformed url,
      missing outputs);
    - secret rejection on both key names and value shapes;
    - unverified default surfaces an UNVERIFIED warning in the output,
      and verified default does not;
    - the cloud-drive-management acceptance fixture renders without
      leaking the fixture into distributed source.
    """

    INPUT_RELATIVE = Path(".mozyo-bridge/workspace-defaults.yaml")
    OUTPUT_RELATIVE = Path(".mozyo-bridge/redmine-defaults.md")
    CLOUD_DRIVE_FIXTURE = {
        "identifier": "giken-cloud-drive-management",
        "name": "クラウドドライブ管理",
        "url": "https://redmine.giken.or.jp/projects/giken-cloud-drive-management",
        "parent_label": "3800_情報処理促進部",
    }

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = args.func(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def _stage_repo(self, dest: Path, *, yaml_body: str) -> Path:
        (dest / ".mozyo-bridge").mkdir(parents=True)
        (dest / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
            yaml_body, encoding="utf-8"
        )
        return dest

    def _yaml_for(
        self,
        *,
        identifier: str = "giken-3800-mozyo-bridge",
        name: str = "mozyo_bridge",
        url: str = "https://redmine.giken.or.jp/projects/giken-3800-mozyo-bridge",
        parent_label: str = "3800_情報処理促進部",
        verified: bool = True,
        verification_date: str = "2026-05-28",
        verified_by: str = "hollySizzle",
        outputs: tuple[tuple[str, str], ...] = (
            ("redmine_markdown", ".mozyo-bridge/redmine-defaults.md"),
        ),
        schema_version: int = 1,
        extra: str = "",
    ) -> str:
        output_lines: list[str] = []
        for kind, target in outputs:
            output_lines.append(f"  - kind: {kind}")
            output_lines.append(f"    target: {target}")
        outputs_block = "\n".join(output_lines)
        return (
            f"schema_version: {schema_version}\n"
            "redmine:\n"
            "  default_project:\n"
            f"    identifier: {identifier}\n"
            f"    name: {name}\n"
            f"    url: {url}\n"
            f"    parent_label: {parent_label}\n"
            "  verification:\n"
            f"    verified: {str(verified).lower()}\n"
            f'    verification_date: "{verification_date}"\n'
            f"    verified_by: {verified_by}\n"
            "outputs:\n"
            f"{outputs_block}\n"
            f"{extra}"
        )

    # ------------------------------------------------------------------
    # Round-trip + CLI surface
    # ------------------------------------------------------------------

    def test_committed_repo_renders_byte_equal(self) -> None:
        from mozyo_bridge.workspace_defaults import collect_render_results

        results = collect_render_results(ROOT)
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual(
            result.rendered,
            result.on_disk,
            msg=(
                f"{result.output_path} drifted from workspace-defaults source; "
                "rerun `mozyo-bridge workspace-defaults` and recommit."
            ),
        )

    def test_cli_check_clean_then_drift_then_recover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=self._yaml_for())
            # First render seeds the output.
            result, _, _ = self.run_cli(
                ["workspace-defaults", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            # Clean --check.
            result, stdout, stderr = self.run_cli(
                ["workspace-defaults", "--check", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            self.assertIn("redmine-defaults.md is up to date", stdout)
            self.assertEqual("", stderr)
            # Tamper.
            output = repo / self.OUTPUT_RELATIVE
            output.write_text(
                output.read_text(encoding="utf-8") + "\nTAMPER\n",
                encoding="utf-8",
            )
            result, _, stderr = self.run_cli(
                ["workspace-defaults", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            self.assertIn("is out of date", stderr)
            # Recovery command must be the actual CLI; #10345 / #10663
            # correction precedent. Reject bare-basename or non-runnable
            # forms.
            self.assertIn("mozyo-bridge workspace-defaults", stderr)
            self.assertIn("from the repo root", stderr)
            # Recovery and check is clean again.
            result, _, _ = self.run_cli(
                ["workspace-defaults", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            result, _, stderr = self.run_cli(
                ["workspace-defaults", "--check", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            self.assertEqual("", stderr)

    def test_cli_check_reports_missing_output_as_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=self._yaml_for())
            # Do not render first; just --check. Output is missing.
            result, _, stderr = self.run_cli(
                ["workspace-defaults", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            self.assertIn("is missing", stderr)

    def test_render_survives_yaml_body_edit(self) -> None:
        """Editing the YAML must rotate the rendered output deterministically."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=self._yaml_for())
            self.run_cli(["workspace-defaults", "--repo", str(repo)])
            before = (repo / self.OUTPUT_RELATIVE).read_text(encoding="utf-8")

            (repo / self.INPUT_RELATIVE).write_text(
                self._yaml_for(name="renamed"),
                encoding="utf-8",
            )
            result, _, _ = self.run_cli(
                ["workspace-defaults", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            after = (repo / self.OUTPUT_RELATIVE).read_text(encoding="utf-8")
            self.assertNotEqual(before, after)
            self.assertIn("- name: renamed", after)

    # ------------------------------------------------------------------
    # Schema validation
    # ------------------------------------------------------------------

    def test_missing_input_yaml_dies_with_actionable_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".mozyo-bridge").mkdir(parents=True)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--check", "--repo", str(repo)]
                )

    def test_missing_required_field_dies(self) -> None:
        body = (
            "schema_version: 1\n"
            "redmine:\n"
            "  default_project:\n"
            "    identifier: foo\n"
            # name + url missing.
            "    parent_label: bar\n"
            "  verification:\n"
            "    verified: true\n"
            '    verification_date: "2026-01-01"\n'
            "    verified_by: tester\n"
            "outputs:\n"
            "  - target: .mozyo-bridge/redmine-defaults.md\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_wrong_schema_version_dies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(
                Path(tmp) / "repo", yaml_body=self._yaml_for(schema_version=99)
            )
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_non_http_url_dies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(
                Path(tmp) / "repo",
                yaml_body=self._yaml_for(url="file:///etc/passwd"),
            )
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_outputs_must_be_repo_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(
                Path(tmp) / "repo",
                yaml_body=self._yaml_for(
                    outputs=(("redmine_markdown", "../escape.md"),)
                ),
            )
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    # ------------------------------------------------------------------
    # Typed outputs (Codex review #50989 correction)
    # ------------------------------------------------------------------

    def test_unknown_output_kind_is_rejected(self) -> None:
        """A bare target with a foreign extension cannot inherit the
        Markdown body. The schema requires an explicit `kind` and only
        accepts the kinds the renderer supports.

        Codex review #50989 reproduced the original footgun: adding
        `.codex/config.toml` as a target wrote Markdown into a TOML
        file. Typed outputs make that unreachable from the schema.
        """
        body = self._yaml_for(
            outputs=(("codex_toml", ".codex/config.toml"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_missing_output_kind_is_rejected(self) -> None:
        """Outputs without a `kind` field cannot fall back to Markdown."""
        body = (
            "schema_version: 1\n"
            "redmine:\n"
            "  default_project:\n"
            "    identifier: foo\n"
            "    name: foo\n"
            "    url: https://example.invalid/\n"
            "    parent_label: ''\n"
            "  verification:\n"
            "    verified: true\n"
            '    verification_date: "2026-01-01"\n'
            "    verified_by: tester\n"
            "outputs:\n"
            "  - target: .mozyo-bridge/redmine-defaults.md\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_supported_kinds_list_is_pinned(self) -> None:
        """The supported kinds set is part of the public contract.

        Extending it without updating the design doc / dispatch table
        is the regression Codex review #50989 surfaced. If this
        assertion fails, also update
        `vibes/docs/logics/workspace-defaults-renderer.md` and the
        `_render_for_kind` dispatch in the same commit.
        """
        from mozyo_bridge.workspace_defaults import (
            KNOWN_OUTPUT_KINDS,
            KIND_REDMINE_MARKDOWN,
        )

        self.assertEqual(
            {KIND_REDMINE_MARKDOWN},
            set(KNOWN_OUTPUT_KINDS),
            msg=(
                "KNOWN_OUTPUT_KINDS changed; update "
                "vibes/docs/logics/workspace-defaults-renderer.md, "
                "the `_render_for_kind` dispatch arms, and the typed "
                "renderer for the new kind in the same commit."
            ),
        )

    # ------------------------------------------------------------------
    # Kind ↔ target suffix compatibility (Codex correction-review #50995)
    # ------------------------------------------------------------------

    def test_redmine_markdown_kind_rejects_toml_target(self) -> None:
        """Codex correction review #50995 reproduced the residual footgun.

        Even with typed kinds, `kind: redmine_markdown` + target
        `.codex/config.toml` passed and wrote Markdown body into a
        TOML path. The kind→suffix gate must reject the mismatch at
        load time so an operator cannot silently generate invalid
        config by selecting the only documented kind and pointing it
        at a non-Markdown path.
        """
        body = self._yaml_for(
            outputs=(("redmine_markdown", ".codex/config.toml"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )
            # The TOML file must not have been created by a half-completed
            # run before the validation error fired.
            self.assertFalse(
                (repo / ".codex" / "config.toml").exists(),
                "load-time validation must run before any write",
            )

    def test_redmine_markdown_kind_rejects_json_target(self) -> None:
        """Same gate must block `.mcd.json` (the other documented MCP
        config candidate Codex called out as the motivating use case)."""
        body = self._yaml_for(
            outputs=(("redmine_markdown", ".mcd.json"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )
            self.assertFalse(
                (repo / ".mcd.json").exists(),
                "load-time validation must run before any write",
            )

    def test_redmine_markdown_kind_rejects_extensionless_target(self) -> None:
        """A target with no suffix at all is also unsafe — the renderer
        could write Markdown body to e.g. `README` and the operator
        would see content that looks intentional."""
        body = self._yaml_for(
            outputs=(("redmine_markdown", "docs/README"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_redmine_markdown_kind_accepts_markdown_suffix_alias(self) -> None:
        """Both `.md` and `.markdown` are valid Markdown suffixes; the
        gate must accept the alias so operators don't get a false-positive
        rejection on a legitimate Markdown target."""
        body = self._yaml_for(
            outputs=(("redmine_markdown", ".mozyo-bridge/redmine-defaults.markdown"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            result, _, _ = self.run_cli(
                ["workspace-defaults", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            self.assertTrue(
                (repo / ".mozyo-bridge" / "redmine-defaults.markdown").is_file()
            )

    def test_kind_allowed_suffixes_table_is_pinned(self) -> None:
        """Per-kind suffix sets are part of the public contract.

        If the renderer learns to emit a new format for an existing
        kind (or a new kind is added), the table must be updated in
        the same commit and this test refreshed alongside.
        """
        from mozyo_bridge.workspace_defaults import (
            KIND_ALLOWED_SUFFIXES,
            KIND_REDMINE_MARKDOWN,
        )

        self.assertEqual(
            {KIND_REDMINE_MARKDOWN: {".md", ".markdown"}},
            {k: set(v) for k, v in KIND_ALLOWED_SUFFIXES.items()},
            msg=(
                "KIND_ALLOWED_SUFFIXES changed; sync the design doc's "
                "Supported Output Kinds table and add regression tests "
                "for the new accept / reject cases in the same commit."
            ),
        )

    # ------------------------------------------------------------------
    # Secret rejection
    # ------------------------------------------------------------------

    def test_credential_shape_key_is_rejected(self) -> None:
        body = self._yaml_for(extra="api_key: AKIA0000000000000000\n")
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_credential_shape_value_is_rejected(self) -> None:
        # Even with a non-credential key name, a value matching a
        # secret assignment pattern must die. This catches operators
        # pasting `REDMINE_API_KEY=abc123` into a free-form note.
        body = self._yaml_for(
            extra='note: "REDMINE_API_KEY=abc123secretvalue"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_nested_credential_key_is_rejected(self) -> None:
        body = (
            "schema_version: 1\n"
            "redmine:\n"
            "  default_project:\n"
            "    identifier: foo\n"
            "    name: foo\n"
            "    url: https://example.invalid/\n"
            "    parent_label: ''\n"
            "    extra:\n"
            "      client_secret: nope\n"
            "  verification:\n"
            "    verified: true\n"
            '    verification_date: "2026-01-01"\n'
            "    verified_by: tester\n"
            "outputs:\n"
            "  - target: .mozyo-bridge/redmine-defaults.md\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    # ------------------------------------------------------------------
    # Verified vs unverified rendering
    # ------------------------------------------------------------------

    def test_verified_default_renders_without_warning(self) -> None:
        from mozyo_bridge.workspace_defaults import (
            load_workspace_defaults,
            render_redmine_defaults_markdown,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=self._yaml_for())
            defaults = load_workspace_defaults(repo / self.INPUT_RELATIVE)
            rendered = render_redmine_defaults_markdown(defaults)
            self.assertNotIn("(UNVERIFIED)", rendered)
            self.assertIn("- verified: yes", rendered)
            self.assertIn("Verified default", rendered)
            # Should NOT warn against using the default.
            self.assertNotIn("default is unverified", rendered)

    def test_unverified_default_surfaces_warning_in_output(self) -> None:
        from mozyo_bridge.workspace_defaults import (
            load_workspace_defaults,
            render_redmine_defaults_markdown,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(
                Path(tmp) / "repo",
                yaml_body=self._yaml_for(verified=False),
            )
            defaults = load_workspace_defaults(repo / self.INPUT_RELATIVE)
            rendered = render_redmine_defaults_markdown(defaults)
            self.assertIn("(UNVERIFIED)", rendered)
            self.assertIn("Default is NOT yet verified", rendered)
            self.assertIn("**NO**", rendered)
            self.assertIn("Do NOT use this default for issue creation", rendered)

    def test_verified_true_but_empty_date_is_treated_as_unverified(self) -> None:
        from mozyo_bridge.workspace_defaults import (
            load_workspace_defaults,
            render_redmine_defaults_markdown,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(
                Path(tmp) / "repo",
                yaml_body=self._yaml_for(verification_date=""),
            )
            defaults = load_workspace_defaults(repo / self.INPUT_RELATIVE)
            rendered = render_redmine_defaults_markdown(defaults)
            self.assertIn("(UNVERIFIED)", rendered)

    # ------------------------------------------------------------------
    # Acceptance fixture: cloud-drive-management is test-only
    # ------------------------------------------------------------------

    def test_cloud_drive_fixture_renders_cleanly(self) -> None:
        from mozyo_bridge.workspace_defaults import (
            load_workspace_defaults,
            render_redmine_defaults_markdown,
        )

        body = self._yaml_for(
            identifier=self.CLOUD_DRIVE_FIXTURE["identifier"],
            name=self.CLOUD_DRIVE_FIXTURE["name"],
            url=self.CLOUD_DRIVE_FIXTURE["url"],
            parent_label=self.CLOUD_DRIVE_FIXTURE["parent_label"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            defaults = load_workspace_defaults(repo / self.INPUT_RELATIVE)
            rendered = render_redmine_defaults_markdown(defaults)
            self.assertIn(
                f"identifier: `{self.CLOUD_DRIVE_FIXTURE['identifier']}`",
                rendered,
            )
            self.assertIn(self.CLOUD_DRIVE_FIXTURE["name"], rendered)
            self.assertIn(self.CLOUD_DRIVE_FIXTURE["url"], rendered)

    def test_distributed_source_does_not_carry_cloud_drive_identifier(self) -> None:
        """The acceptance fixture must NOT appear in distributed source.

        Per #10689 constraint: do not hardcode `giken-cloud-drive-management`
        into distributed mozyo_bridge defaults. The fixture is allowed
        only in test code (this file) and in workspace-local docs that
        ship to a workspace, not to the package.
        """
        forbidden = self.CLOUD_DRIVE_FIXTURE["identifier"]
        distributed_roots = [
            ROOT / "src" / "mozyo_bridge",
            ROOT / "skills",
            ROOT / "plugins",
            ROOT / "vibes" / "docs",
            ROOT / ".mozyo-bridge" / "workspace-defaults.yaml",
            ROOT / ".mozyo-bridge" / "redmine-defaults.md",
        ]
        hits: list[str] = []
        for root in distributed_roots:
            if root.is_file():
                paths = [root]
            elif root.is_dir():
                paths = [
                    p
                    for p in root.rglob("*")
                    if p.is_file()
                    and not p.name.endswith(".pyc")
                    and "__pycache__" not in p.parts
                ]
            else:
                continue
            for path in paths:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if forbidden in text:
                    hits.append(path.relative_to(ROOT).as_posix())
        self.assertFalse(
            hits,
            msg=(
                f"distributed source carries the acceptance-fixture "
                f"identifier {forbidden!r}: {hits}. Move the value to "
                "test code or a workspace-local example only."
            ),
        )


class InstallCommandConsistencyTest(unittest.TestCase):
    """Pin Redmine #10699: install-command snippets stay byte-equal across docs.

    Investigation cataloged the install guidance duplication. The
    operator-facing install commands (plugin marketplace add / plugin
    install / pipx install / rules install / Codex `$skill-installer`)
    appear verbatim in README.md, skill-distribution.md, and bootstrap.md
    — multiple occurrences in each. These are *exact-string* copies,
    not audience-specific variants: if one drifts (e.g. a marketplace
    name change updates README but not skill-distribution), users get
    inconsistent copy-paste recipes.

    Owner decision explicitly excludes whole-file README / ReleaseDocs
    canonicalization. So drift is gated at the test layer — the lightest
    available mechanic — mirroring the `SkillCrossWorkspaceGuidanceTest`
    / `SkillWorkflowSemanticAnchorsTest` pattern.

    Codex correction review #51114 caught the soundness gap in the
    original `assertIn`-based gate: a doc with N occurrences whose
    single occurrence drifts still satisfies `assertIn` because the
    other (N-1) occurrences remain. The fix is to pin **exact
    occurrence counts** per (command, doc). One occurrence drifting
    flips the count by 1 and fails the gate. The counts are small
    (<10 per doc) and stable enough that intentional doc edits update
    the same map in the same commit, mirroring how
    `SkillWorkflowSemanticAnchorsTest` adds new markers.

    The intentionally audience-specific variants (`pipx install --force
    git+https://...` for Beta Tester Install, `claude plugin install
    --scope <other>` for fallback paths) are pinned separately so a
    future edit cannot collapse them into the canonical form.
    """

    # Per (canonical command, doc) → expected exact occurrence count.
    # Counts come from `str.count(command)` over the doc body. A 0 means
    # the command must NOT appear in that doc (audience scope guard).
    #
    # Updating counts: when a doc legitimately gains or loses an
    # install-command mention (prose addition, section removal, etc.),
    # update the count in the same commit. The test failure message
    # spells out the expected vs actual count to make the diff obvious.
    PINNED_INSTALL_OCCURRENCES: tuple[tuple[str, dict[str, int]], ...] = (
        (
            "claude plugin marketplace add hollySizzle/mozyo_bridge",
            {
                "README.md": 2,
                "vibes/docs/logics/skill-distribution.md": 4,
                "vibes/docs/logics/bootstrap.md": 1,
            },
        ),
        (
            "claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user",
            {
                "README.md": 2,
                "vibes/docs/logics/skill-distribution.md": 4,
                "vibes/docs/logics/bootstrap.md": 1,
            },
        ),
        (
            "pipx install mozyo-bridge",
            {
                "README.md": 1,
                "vibes/docs/logics/skill-distribution.md": 3,
                "vibes/docs/logics/bootstrap.md": 2,
                "vibes/docs/logics/scaffold-rules.md": 1,
            },
        ),
        (
            "mozyo-bridge rules install",
            {
                "README.md": 5,
                "vibes/docs/logics/skill-distribution.md": 5,
                "vibes/docs/logics/bootstrap.md": 9,
                "vibes/docs/logics/scaffold-rules.md": 9,
            },
        ),
        # Codex `$skill-installer` invocation against the canonical
        # GitHub skill path. The `$` shell sigil is included so the
        # full operator-pasted command is pinned. The single
        # skill-distribution occurrence is in the Install Command
        # Drift subsection that records this gate's policy.
        (
            "$skill-installer https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent",
            {
                "README.md": 1,
                "vibes/docs/logics/skill-distribution.md": 1,
                "vibes/docs/logics/bootstrap.md": 1,
            },
        ),
        # The canonical-path string Codex must call the installer
        # against. The URL is the most drift-prone token (a repo move
        # would invalidate every occurrence at once) and appears in
        # multiple wording shapes per doc; pinning the exact count
        # catches a single-occurrence rename.
        (
            "https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent",
            {
                "README.md": 2,
                "vibes/docs/logics/skill-distribution.md": 4,
                "vibes/docs/logics/bootstrap.md": 1,
            },
        ),
    )

    # Intentional audience-specific variants. Each (variant, doc) →
    # expected count. The variant must remain present at the recorded
    # count so an accidental collapse to the canonical PyPI form fails
    # loudly. Beta Tester / GitHub main install + Fresh Install Smoke
    # are intentionally distinguishable from the standard PyPI form.
    INTENTIONAL_VARIANT_OCCURRENCES: tuple[tuple[str, dict[str, int]], ...] = (
        (
            "pipx install --force git+https://github.com/hollySizzle/mozyo_bridge.git",
            {"README.md": 1},
        ),
    )

    def _read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def _assert_count(self, body: str, command: str, *, doc: str, expected: int) -> None:
        actual = body.count(command)
        self.assertEqual(
            expected,
            actual,
            msg=(
                f"{doc} occurrence count for {command!r} drifted: "
                f"expected {expected}, found {actual}. "
                f"Either one occurrence was rewritten while others stayed "
                f"intact (single-occurrence drift — fix the rewrite), or "
                f"a doc edit intentionally added/removed a mention "
                f"(update PINNED_INSTALL_OCCURRENCES in the same commit). "
                f"Codex review #51114 introduced count-pinning specifically "
                f"to catch single-occurrence drift that assertIn missed."
            ),
        )

    def test_canonical_install_commands_have_pinned_occurrence_counts(self) -> None:
        for command, doc_counts in self.PINNED_INSTALL_OCCURRENCES:
            for doc, expected in doc_counts.items():
                with self.subTest(command=command, doc=doc):
                    self._assert_count(
                        self._read(doc), command, doc=doc, expected=expected
                    )

    def test_intentional_install_variants_have_pinned_occurrence_counts(self) -> None:
        for variant, doc_counts in self.INTENTIONAL_VARIANT_OCCURRENCES:
            for doc, expected in doc_counts.items():
                with self.subTest(variant=variant, doc=doc):
                    self._assert_count(
                        self._read(doc), variant, doc=doc, expected=expected
                    )

    def test_count_gate_catches_single_occurrence_drift(self) -> None:
        """Regression meta-test: prove the count-pinning gate detects
        single-occurrence drift that the prior `assertIn` gate missed.

        Codex correction review #51114 requested explicit proof that a
        single-occurrence rewrite (one of N copies drifts while the
        others stay verbatim) fails the gate. This test takes a real
        doc with N > 1 occurrences of a pinned command, mutates the
        FIRST occurrence in memory, and asserts the count delta would
        fail the gate's equality check.
        """
        # Pick a command whose pinned count is > 1 in at least one doc.
        # README.md has marketplace_add count == 2.
        command = "claude plugin marketplace add hollySizzle/mozyo_bridge"
        doc = "README.md"
        expected = next(
            counts[doc]
            for cmd, counts in self.PINNED_INSTALL_OCCURRENCES
            if cmd == command and doc in counts
        )
        self.assertGreater(
            expected,
            1,
            msg="meta-test premise: pick a (command, doc) with count > 1",
        )

        body = self._read(doc)
        # Mutate FIRST occurrence only (the typo a real reviewer might
        # introduce when only updating one mention).
        drifted_form = "claude plugin marketplace add hollyizzle/mozyo_bridge"
        mutated = body.replace(command, drifted_form, 1)

        # Sanity: the mutation was applied AND only the first occurrence
        # was changed.
        self.assertEqual(expected - 1, mutated.count(command))
        self.assertEqual(1, mutated.count(drifted_form))

        # The count gate fires on the (expected vs expected-1) mismatch.
        # The prior `assertIn(command, mutated)` would still pass
        # because (expected - 1) >= 1 (one intact occurrence remains).
        self.assertNotEqual(expected, mutated.count(command))
        self.assertIn(command, mutated)  # documents the gap assertIn left


if __name__ == "__main__":
    unittest.main()
