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
    ExecutionRoot,
    build_delivery_record,
    build_execution_root,
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

    def test_find_repo_root_uses_scaffolded_workspace_marker(self) -> None:
        # A non-git scaffolded workspace (only `.mozyo-bridge/scaffold.json`,
        # no git / pyproject / tmux marker) is a first-class identity root
        # (Redmine #11301). The walk stops at the workspace, not the home dir.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "人形使い"
            nested = workspace / "a" / "b"
            nested.mkdir(parents=True)
            (workspace / ".mozyo-bridge").mkdir()
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )

            self.assertEqual(workspace.resolve(), find_repo_root(nested))

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

    def test_resolve_target_normalizes_location_form_to_pane_id(self) -> None:
        # Redmine #11666: a `session:window` location used to be returned
        # verbatim, so pane_info()'s pane-id match never succeeded and every
        # location target died with `pane disappeared after resolve`.
        with patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch(
                "mozyo_bridge.domain.pane_resolver.resolve_pane_id",
                return_value="%9",
            ) as resolver:
            self.assertEqual("%9", resolve_target("repo:codex"))
        resolver.assert_called_once_with("repo:codex")

    def test_resolve_target_passes_pane_id_through_unchanged(self) -> None:
        with patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch(
                "mozyo_bridge.domain.pane_resolver.resolve_pane_id"
            ) as resolver:
            self.assertEqual("%9", resolve_target("%9"))
        resolver.assert_not_called()

    def test_pane_info_finds_pane_for_location_target(self) -> None:
        # End-to-end through pane_info: the normalized id must match the
        # pane_lines() entry, where the raw location string never did.
        from mozyo_bridge.domain.pane_resolver import pane_info

        panes = [
            {
                "id": "%9",
                "location": "repo:2.0",
                "command": "codex",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
        ]
        with patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch(
                "mozyo_bridge.domain.pane_resolver.resolve_pane_id",
                return_value="%9",
            ), \
            patch(
                "mozyo_bridge.domain.pane_resolver.pane_lines",
                return_value=panes,
            ):
            self.assertEqual("%9", pane_info("repo:codex")["id"])

    def test_resolve_pane_id_resolves_location_and_rejects_invalid(self) -> None:
        from mozyo_bridge.infrastructure.tmux_client import resolve_pane_id

        with patch(
            "mozyo_bridge.infrastructure.tmux_client.run_tmux",
            return_value=argparse.Namespace(returncode=0, stdout="%42\n", stderr=""),
        ):
            self.assertEqual("%42", resolve_pane_id("repo:codex"))
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.run_tmux",
            return_value=argparse.Namespace(returncode=1, stdout="", stderr="no such window"),
        ), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                resolve_pane_id("repo:nope")

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

    def _help_text(self, argv: list[str]) -> str:
        parser = build_parser()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                parser.parse_args(argv)
        return stdout.getvalue()

    def test_top_level_instruction_help_is_not_falsely_read_only(self) -> None:
        # Regression (Redmine #10932): once `instruction install --write` landed
        # (#10930), calling the whole `instruction` group "(read-only)" became
        # false and blocked the v0.5.5 production publish. Pin that the stale
        # group summary cannot return, and that the summary now signals the
        # write-capable subcommand.
        top = self._help_text(["--help"])
        self.assertNotIn(
            "Opt-in checks for repo-local LLM runtime config (read-only)", top
        )
        self.assertIn("write-capable", top)

    def test_instruction_subcommands_keep_responsibility_split(self) -> None:
        instruction = self._help_text(["instruction", "--help"])
        # Both subcommands are listed.
        self.assertIn("doctor", instruction)
        self.assertIn("install", instruction)
        # doctor stays described as read-only; install as write-capable / dry-run.
        self.assertIn("read-only", instruction)
        self.assertIn("dry-run", instruction)
        # Redmine #11051: the whole `instruction` group is now a deprecated alias
        # for `runtime-config`. The help must say so without breaking the split.
        self.assertIn("deprecated alias", instruction.lower())
        self.assertIn("runtime-config", instruction)

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

    def test_bare_mozyo_defaults_cc_false(self) -> None:
        args = build_parser().parse_args([])

        self.assertFalse(args.cc)

    def test_bare_mozyo_accepts_cc_flag(self) -> None:
        args = build_parser().parse_args(["--cc"])

        self.assertIsNone(args.command)
        self.assertTrue(args.cc)

    def test_bare_mozyo_defaults_json_output_false(self) -> None:
        parser = build_parser()

        args = parser.parse_args([])

        self.assertFalse(args.json_output)

    def test_bare_mozyo_accepts_json_flag_with_no_attach(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["--no-attach", "--json"])

        self.assertIsNone(args.command)
        self.assertTrue(args.no_attach)
        self.assertTrue(args.json_output)

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


class DoctorInstructionTaxonomyTest(unittest.TestCase):
    """Redmine #11051: `doctor instruction` runbook + `runtime-config` rename.

    Design consultation answer #53306 fixed the taxonomy: option A (rename the
    top-level `instruction` group to `runtime-config`, add a read-only
    `doctor instruction` runbook), a 1-cycle deprecated alias that warns on
    stderr, and additive JSON.
    """

    def _help_text(self, argv: list[str]) -> str:
        parser = build_parser()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                parser.parse_args(argv)
        return stdout.getvalue()

    def test_doctor_instruction_is_a_doctor_subcommand(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["doctor", "instruction"])
        self.assertEqual("doctor", args.command)
        self.assertEqual("instruction", args.doctor_command)
        self.assertEqual("cmd_doctor_instruction", args.func.__name__)

    def test_bare_doctor_still_runs_diagnostics(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["doctor"])
        self.assertEqual("doctor", args.command)
        self.assertIsNone(args.doctor_command)
        self.assertEqual("cmd_doctor", args.func.__name__)

    def test_runtime_config_group_parses(self) -> None:
        parser = build_parser()
        check = parser.parse_args(["runtime-config", "check"])
        self.assertEqual("runtime-config", check.command)
        self.assertEqual("check", check.runtime_config_command)
        self.assertEqual("cmd_instruction_doctor", check.func.__name__)
        install = parser.parse_args(["runtime-config", "install", "--write"])
        self.assertEqual("install", install.runtime_config_command)
        self.assertEqual("cmd_instruction_install", install.func.__name__)
        self.assertTrue(install.write)

    def test_canonical_runtime_config_is_not_a_deprecated_alias(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["runtime-config", "check"])
        self.assertIsNone(getattr(args, "deprecated_alias", None))

    def test_instruction_alias_carries_deprecation_metadata(self) -> None:
        parser = build_parser()
        doctor_alias = parser.parse_args(["instruction", "doctor"])
        self.assertEqual(
            "mozyo-bridge instruction doctor", doctor_alias.deprecated_alias
        )
        self.assertEqual(
            "mozyo-bridge runtime-config check", doctor_alias.canonical_command
        )
        # Same underlying implementation as the canonical command.
        self.assertEqual("cmd_instruction_doctor", doctor_alias.func.__name__)
        install_alias = parser.parse_args(["instruction", "install"])
        self.assertEqual(
            "mozyo-bridge instruction install", install_alias.deprecated_alias
        )
        self.assertEqual(
            "mozyo-bridge runtime-config install", install_alias.canonical_command
        )

    def test_deprecated_alias_warns_on_stderr_only(self) -> None:
        from mozyo_bridge.application.cli import _warn_deprecated_alias

        # Alias -> warning on stderr.
        args = argparse.Namespace(
            deprecated_alias="mozyo-bridge instruction doctor",
            canonical_command="mozyo-bridge runtime-config check",
        )
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            _warn_deprecated_alias(args)
        self.assertIn("deprecated", stderr.getvalue())
        self.assertIn("runtime-config check", stderr.getvalue())
        # stdout untouched so JSON consumers stay additive.
        self.assertEqual("", stdout.getvalue())

        # Canonical command -> no warning.
        quiet = io.StringIO()
        with contextlib.redirect_stderr(quiet):
            _warn_deprecated_alias(argparse.Namespace(deprecated_alias=None))
        self.assertEqual("", quiet.getvalue())

    def test_runtime_config_help_describes_responsibility_split(self) -> None:
        help_text = self._help_text(["runtime-config", "--help"])
        self.assertIn("check", help_text)
        self.assertIn("install", help_text)
        self.assertIn("read-only", help_text)
        self.assertIn("dry-run", help_text)

    def _run_func(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = args.func(args)
        return rc, stdout.getvalue()

    def test_canonical_runtime_config_text_uses_new_names(self) -> None:
        # Review Gate #53340 finding: the canonical commands must not print the
        # legacy `instruction doctor/install` names on stdout. The check command
        # is exercised end-to-end; the install header is asserted on its
        # formatter (the command body needs a workspace-defaults source).
        from mozyo_bridge.application.instruction_install import (
            format_instruction_install_text,
        )

        with tempfile.TemporaryDirectory() as tmp:
            _, check_out = self._run_func(["runtime-config", "check", "--target", tmp])
            self.assertIn("runtime-config check:", check_out)
            self.assertNotIn("instruction doctor:", check_out)

        install_text = format_instruction_install_text(
            {
                "ok": True,
                "profile": "redmine-codex",
                "action": "up-to-date",
                "target": "/repo",
                "messages": [],
            }
        )
        self.assertIn("runtime-config install:", install_text)
        self.assertNotIn("instruction install:", install_text)


class DoctorInstructionRunbookTest(unittest.TestCase):
    """The runbook synthesis (`build_runbook`) is pure given doctor results."""

    def _doctor_result(self, **overrides: str) -> dict:
        sections = {
            "cli": {"status": "ok", "version": "9.9.9", "executable": "/x/mozyo-bridge"},
            "rules": {"status": "ok", "next_action": []},
            "codex_skill": {"status": "ok"},
            "claude_skill": {"status": "plugin-managed"},
            "scaffold": {"status": "ok", "detail": {"preset": "redmine-governed"}},
            "claude_nagger": {"status": "ok"},
            "tmux": {"status": "ok", "artifact": {"host_wiring": {"next_action": []}}},
        }
        for key, status in overrides.items():
            sections[key] = {**sections.get(key, {}), "status": status}
        ok = all(
            s.get("status") not in {"missing", "drifted", "warning", "incomplete"}
            for s in sections.values()
        )
        return {"ok": ok, "sections": sections}

    def test_runbook_order_and_migration_present(self) -> None:
        from mozyo_bridge.application.doctor_instruction import build_runbook

        steps = build_runbook(
            self._doctor_result(), {"ok": True}, "/repo", "redmine-codex"
        )
        ids = [s["id"] for s in steps]
        self.assertEqual(
            ids,
            [
                "cli",
                "rules",
                "agent_skills",
                "scaffold",
                "runtime_config",
                "optional_utilities",
                "final_verification",
            ],
        )

    def test_clean_environment_needs_no_action(self) -> None:
        from mozyo_bridge.application.doctor_instruction import (
            STATUS_ACTION,
            build_runbook,
        )

        steps = build_runbook(
            self._doctor_result(), {"ok": True}, "/repo", "redmine-codex"
        )
        self.assertFalse([s for s in steps if s["status"] == STATUS_ACTION])

    def test_skill_step_labels_primary_and_fallback(self) -> None:
        from mozyo_bridge.application.doctor_instruction import build_runbook

        steps = build_runbook(
            self._doctor_result(claude_skill="missing", codex_skill="missing"),
            {"ok": True},
            "/repo",
            "redmine-codex",
        )
        skills = next(s for s in steps if s["id"] == "agent_skills")
        self.assertEqual("action", skills["status"])
        roles = {c["role"] for c in skills["commands"]}
        self.assertIn("primary", roles)
        self.assertIn("fallback", roles)
        # Claude primary path is the plugin marketplace, not curl.
        primary_claude = next(
            c for c in skills["commands"]
            if c["role"] == "primary" and "claude" in c.get("for", "")
        )
        self.assertIn("plugin", primary_claude["command"])

    def test_cli_step_surfaces_stale_source_drift_note(self) -> None:
        """Redmine #11855: a stale-installed-CLI warning in the cli section
        surfaces the repo-local invocation in the CLI readiness step."""
        from mozyo_bridge.application.doctor_instruction import (
            STATUS_ACTION,
            build_runbook,
        )

        doctor_result = self._doctor_result()
        doctor_result["sections"]["cli"] = {
            "status": "warning",
            "version": "9.9.9",
            "executable": "/x/mozyo-bridge",
            "source_drift": {
                "relation": "version-differs",
                "repo_local_invocation": "PYTHONPATH=src python3 -m mozyo_bridge",
            },
            "next_action": ["use repo-local CLI"],
        }
        steps = build_runbook(doctor_result, {"ok": True}, "/repo", "redmine-codex")
        cli_step = next(s for s in steps if s["id"] == "cli")
        self.assertEqual(STATUS_ACTION, cli_step["status"])
        self.assertTrue(
            any(
                "PYTHONPATH=src python3 -m mozyo_bridge" in note
                for note in cli_step["notes"]
            )
        )

    def test_scaffold_drift_is_review_before_restore(self) -> None:
        from mozyo_bridge.application.doctor_instruction import build_runbook

        steps = build_runbook(
            self._doctor_result(scaffold="drifted"),
            {"ok": True},
            "/repo",
            "redmine-codex",
        )
        scaffold = next(s for s in steps if s["id"] == "scaffold")
        self.assertEqual("action", scaffold["status"])
        commands = scaffold["commands"]
        # status/diff are primary; apply --backup is the fallback that comes last.
        self.assertEqual("primary", commands[0]["role"])
        self.assertIn("scaffold status", commands[0]["command"])
        self.assertEqual("fallback", commands[-1]["role"])
        self.assertIn("--backup", commands[-1]["command"])

    def test_result_reports_pending_and_migrations(self) -> None:
        from mozyo_bridge.application.doctor_instruction import run_doctor_instruction

        with patch(
            "mozyo_bridge.application.doctor_instruction.run_doctor",
            return_value=self._doctor_result(rules="missing"),
        ), patch(
            "mozyo_bridge.application.doctor_instruction.run_instruction_doctor",
            return_value={"ok": True},
        ), patch(
            "mozyo_bridge.application.doctor_instruction.doctor_target",
            return_value=Path("/repo"),
        ):
            result = run_doctor_instruction(argparse.Namespace(repo="/repo"))
        self.assertFalse(result["ok"])
        self.assertIn("rules", result["pending_step_ids"])
        olds = {m["old"] for m in result["migrations"]}
        self.assertIn("mozyo-bridge instruction doctor", olds)
        self.assertIn("mozyo-bridge instruction install", olds)


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

    def test_main_unit_claude_safe_use_section_present(self) -> None:
        """Redmine #11858: the shared skill reference must carry the main-unit
        Claude safe-use boundary so a main coordinator unit that places a
        Claude pane beside the coordinator Codex knows what it may offload to
        save Codex context and what stays owner-facing. The boundary is the
        portable workflow risk, not an operator's private offload list."""
        section_start = self.workflow.index("## Main-Unit Claude Safe-Use Boundary")
        section_end = self.workflow.index(
            "\n## Claude / Codex Role Boundary", section_start
        )
        section = self.workflow[section_start:section_end]
        # Anchored to the durable record and framed as observed risk, not a
        # fixed judgement about any model.
        self.assertIn("#11858", section)
        self.assertIn(
            "observed workflow risk, not a fixed judgement about any model",
            section,
        )
        # Output is input/draft, never evidence the coordinator can act on
        # without confirming against the source of truth.
        self.assertIn("draft / input, never evidence", section)
        # The two explicit buckets the acceptance criteria require.
        self.assertIn("### Allowed uses (safe Codex-context savings)", section)
        self.assertIn("### Prohibited uses (stay with the coordinator Codex)", section)
        # Concrete Codex-context-saving safe tasks.
        self.assertIn("Summarizing long Redmine journals", section)
        self.assertIn("Extracting candidates", section)
        # Owner-facing / gate actions that must NOT be delegated.
        self.assertIn("owner close approval", section)
        self.assertIn("Review Gate", section)
        self.assertIn("durable routing decisions", section)
        # The difference from a sublane Claude must be explicit.
        self.assertIn("### Difference from a sublane Claude", section)
        # Portable vs private operator preference separation.
        self.assertIn("public-private-boundary.md", section)

    def test_main_unit_claude_safe_use_does_not_grant_owner_or_gate_authority(
        self,
    ) -> None:
        """The main-unit Claude section saves coordinator context but must not
        read as moving any owner-facing / gate boundary onto the Claude pane.
        A future edit that softened the prohibition into an allowance would be
        caught here."""
        section_start = self.workflow.index("## Main-Unit Claude Safe-Use Boundary")
        section_end = self.workflow.index(
            "\n## Claude / Codex Role Boundary", section_start
        )
        section = self.workflow[section_start:section_end]
        # The assistant framing and the non-relaxation clause must both stand.
        self.assertIn("assistant, not a parallel coordinator", section)
        self.assertIn(
            "Owner-facing and gate decisions stay with the coordinator Codex",
            section,
        )
        # It must defer owner approval to the single aggregation point, not the
        # Claude pane.
        self.assertIn("never a Claude pane", section)

    def test_issue_subject_description_separation_section_present(self) -> None:
        """Redmine #11856: the shared skill reference must carry the
        creation-time subject / description separation convention so agents
        pass an explicit concise subject instead of letting a long Markdown
        body produce a subject like `## 背景` (the #11850 j#57294 observation).
        It must also carry the immediate-correction rule for a malformed
        subject and stay anchored to the durable record."""
        section_start = self.workflow.index(
            "### Issue Subject / Description Separation"
        )
        section_end = self.workflow.index("\n## Local Documentation", section_start)
        section = self.workflow[section_start:section_end]
        # Anchored to the durable record and the concrete observed failure.
        self.assertIn("#11856", section)
        self.assertIn("## 背景", section)
        # The two acceptance-criteria halves: explicit subject on create, and
        # an immediate-correction rule for a bad subject.
        self.assertIn("explicit-subject-on-create", section)
        self.assertIn("Immediate-correction rule", section)
        # Concrete creation-time discipline: always pass an explicit subject and
        # never let the body derive it.
        self.assertIn("Always pass an explicit `subject`", section)
        self.assertIn("Never let the description body produce the subject", section)
        # The correction names the actual repair tool and lands on the durable
        # record.
        self.assertIn("update_issue_subject_tool", section)
        # Must not claim to change gate vocabulary / hierarchy / required fields.
        self.assertIn("does not change any gate vocabulary", section)
        # Portable rule vs operator's private subject style.
        self.assertIn("public-private-boundary.md", section)


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
        self.assertEqual(8.0, args.landing_timeout)

    def test_landing_timeout_default_is_eight_seconds_for_tui_redraw(self) -> None:
        # Redmine #10756: the landing-timeout default was raised 5.0 -> 8.0 to
        # absorb Claude/Codex TUI redraw delay. The marker rail still returns
        # as soon as the marker is observed, so this does not add success-path
        # latency. read-lines and submit-delay defaults are intentionally
        # unchanged. Pin the default across the parser surfaces that share the
        # timing flags (message / notify-delivery / handoff send).
        parser = build_parser()

        message_args = parser.parse_args(["message", "%2", "hi"])
        self.assertEqual(8.0, message_args.landing_timeout)
        self.assertEqual(0.2, message_args.submit_delay)

        notify_args = parser.parse_args(["notify-codex", "--target", "%2"])
        self.assertEqual(8.0, notify_args.landing_timeout)
        self.assertEqual(0.2, notify_args.submit_delay)
        self.assertEqual(20, notify_args.read_lines)

        handoff_args = parser.parse_args(
            ["handoff", "send", "--to", "codex", "--source", "redmine", "--kind", "reply"]
        )
        self.assertEqual(8.0, handoff_args.landing_timeout)
        self.assertEqual(0.2, handoff_args.submit_delay)
        self.assertEqual(50, handoff_args.read_lines)

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
        # Exercised via the legacy `--window-only` path so the assertion stays
        # focused on the style application (smart adoption is covered elsewhere).
        args = argparse.Namespace(
            agent="claude", target="%5", window_only=True, no_vscode_settings=False
        )
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

        # Bare `mozyo` now derives a collision-safe session name (Redmine
        # #10796) instead of using the raw repo basename.
        from mozyo_bridge.domain.session_naming import derive_session_name

        expected = derive_session_name(repo).name
        self.assertEqual(expected, captured["args"].session)
        self.assertTrue(expected.startswith("mozyo-my-project-"))
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

        from mozyo_bridge.domain.session_naming import derive_session_name

        expected = derive_session_name(repo).name
        self.assertIn(f"attach: tmux attach -t {expected}", stdout.getvalue())

    def test_cmd_mozyo_json_emits_ready_payload_for_created_windows(self) -> None:
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
                json_output=True,
            )
            list_result = argparse.Namespace(
                returncode=0,
                stdout="0\tclaude\tclaude\n1\tcodex\tnode\n",
                stderr="",
            )

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows", return_value=["claude:%1", "codex:%2"]), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.commands.os.execvp", side_effect=AssertionError("must not attach")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_mozyo(args))

        from mozyo_bridge.domain.session_naming import derive_session_name

        expected = derive_session_name(repo).name
        payload = json.loads(stdout.getvalue())
        self.assertEqual(expected, payload["session"])
        self.assertEqual(str(repo), payload["repo_root"])
        self.assertEqual(str(repo), payload["cwd"])
        self.assertEqual(["claude:%1", "codex:%2"], payload["created"])
        self.assertTrue(payload["ready"])
        self.assertEqual(f"tmux attach -t {expected}", payload["attach"])
        self.assertEqual(expected, payload["attach_target"])
        self.assertFalse(payload["attached"])
        self.assertEqual(
            [
                {"index": 0, "name": "claude", "process": "claude"},
                {"index": 1, "name": "codex", "process": "node"},
            ],
            payload["windows"],
        )

    def _run_cmd_mozyo_capturing_execvp(self, *, cc, no_attach=False, json_output=False):
        """Run cmd_mozyo with tmux mocked; capture os.execvp argv (or None)."""
        tmp_ctx = tempfile.TemporaryDirectory()
        tmp = tmp_ctx.__enter__()
        self.addCleanup(tmp_ctx.__exit__, None, None, None)
        repo = (Path(tmp) / "my-project").resolve()
        repo.mkdir()
        ns = dict(
            repo=str(repo),
            session=None,
            cwd=None,
            config_path=None,
            ready_timeout=0,
            force=False,
            no_attach=no_attach,
            cc=cc,
        )
        if json_output:
            ns["json_output"] = True
        args = argparse.Namespace(**ns)
        list_result = argparse.Namespace(
            returncode=0, stdout="0\tclaude\tclaude\n1\tcodex\tnode\n", stderr=""
        )
        calls: list[list[str]] = []

        def rec_execvp(_file, argv):
            calls.append(list(argv))
            raise RuntimeError("attached")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
            patch("mozyo_bridge.application.commands.ensure_repo_session_windows", return_value=["claude:%1", "codex:%2"]), \
            patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
            patch("mozyo_bridge.application.commands.os.execvp", side_effect=rec_execvp), \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
            if no_attach or json_output:
                rc = cmd_mozyo(args)
                self.assertEqual(0, rc)
            else:
                with self.assertRaisesRegex(RuntimeError, "attached"):
                    cmd_mozyo(args)
        from mozyo_bridge.domain.session_naming import derive_session_name

        return derive_session_name(repo).name, calls, stdout.getvalue()

    def test_cmd_mozyo_default_attach_uses_plain_tmux_attach(self) -> None:
        session, calls, _out = self._run_cmd_mozyo_capturing_execvp(cc=False)
        self.assertEqual([["tmux", "attach", "-t", session]], calls)

    def test_cmd_mozyo_cc_attaches_via_control_mode(self) -> None:
        session, calls, _out = self._run_cmd_mozyo_capturing_execvp(cc=True)
        # `-CC` is a tmux global option and must precede the `attach` command.
        self.assertEqual([["tmux", "-CC", "attach", "-t", session]], calls)

    def test_cmd_mozyo_cc_no_attach_prints_control_mode_hint_without_exec(self) -> None:
        session, calls, out = self._run_cmd_mozyo_capturing_execvp(
            cc=True, no_attach=True
        )
        self.assertEqual([], calls)  # --no-attach wins: never exec
        self.assertIn(f"attach: tmux -CC attach -t {session}", out)

    def test_cmd_mozyo_cc_json_reports_control_mode_without_attaching(self) -> None:
        session, calls, out = self._run_cmd_mozyo_capturing_execvp(
            cc=True, json_output=True
        )
        self.assertEqual([], calls)  # --json never attaches
        payload = json.loads(out)
        self.assertTrue(payload["control_mode"])
        self.assertEqual(f"tmux -CC attach -t {session}", payload["attach"])
        self.assertFalse(payload["attached"])
        self.assertTrue(payload["no_attach"])

    def test_cmd_mozyo_default_json_reports_control_mode_false(self) -> None:
        session, _calls, out = self._run_cmd_mozyo_capturing_execvp(
            cc=False, json_output=True
        )
        payload = json.loads(out)
        self.assertFalse(payload["control_mode"])
        self.assertEqual(f"tmux attach -t {session}", payload["attach"])

    def test_cmd_mozyo_json_reports_ready_when_windows_reused(self) -> None:
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
                json_output=True,
            )
            # Reused session: both agent windows already exist, so nothing is
            # newly created, yet readiness must still be true.
            list_result = argparse.Namespace(
                returncode=0,
                stdout="0\tclaude\tclaude\n1\tcodex\tcodex\n2\tnotes\tzsh\n",
                stderr="",
            )

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
                patch("mozyo_bridge.application.commands.session_cwd_mismatch", return_value=[]), \
                patch("mozyo_bridge.application.commands.legacy_basename_session_notice", return_value=None), \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows", return_value=[]), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.commands.os.execvp", side_effect=AssertionError("must not attach")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_mozyo(args))

        payload = json.loads(stdout.getvalue())
        self.assertEqual([], payload["created"])
        self.assertTrue(payload["ready"])
        self.assertEqual(
            {"claude", "codex", "notes"},
            {window["name"] for window in payload["windows"]},
        )

    def test_cmd_mozyo_json_not_ready_when_codex_window_absent(self) -> None:
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
                json_output=True,
            )
            list_result = argparse.Namespace(
                returncode=0,
                stdout="0\tclaude\tclaude\n",
                stderr="",
            )

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows", return_value=["claude:%1"]), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.commands.os.execvp", side_effect=AssertionError("must not attach")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_mozyo(args))

        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ready"])

    def test_cmd_mozyo_json_without_no_attach_flag_implies_no_attach(self) -> None:
        # `--json` without an explicit `--no-attach` must still not attach and
        # the payload must report the *effective* no-attach behavior, not the
        # raw flag (Redmine #11313 review #54111).
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
                json_output=True,
            )
            list_result = argparse.Namespace(
                returncode=0,
                stdout="0\tclaude\tclaude\n1\tcodex\tnode\n",
                stderr="",
            )

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows", return_value=["claude:%1", "codex:%2"]), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.commands.os.execvp", side_effect=AssertionError("must not attach")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_mozyo(args))

        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["no_attach"])
        self.assertFalse(payload["attached"])

    def test_cmd_mozyo_human_output_unchanged_without_json_flag(self) -> None:
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
                json_output=False,
            )
            list_result = argparse.Namespace(
                returncode=0,
                stdout="0\tclaude\tclaude\n1\tcodex\tnode\n",
                stderr="",
            )

            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.ensure_repo_session_windows", return_value=["claude:%1", "codex:%2"]), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch("mozyo_bridge.application.commands.os.execvp", side_effect=AssertionError("must not attach")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_mozyo(args))

        from mozyo_bridge.domain.session_naming import derive_session_name

        expected = derive_session_name(repo).name
        output = stdout.getvalue()
        self.assertEqual(
            f"session={expected} created=claude:%1,codex:%2\n"
            "INDEX\tNAME\tPROCESS\n"
            "0\tclaude\tclaude\n1\tcodex\tnode\n"
            f"attach: tmux attach -t {expected}\n",
            output,
        )

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
            # The mismatch guard keys on the derived session name (Redmine
            # #10796), so the lingering pane must live in that same session.
            from mozyo_bridge.domain.session_naming import derive_session_name

            derived = derive_session_name(repo).name
            panes = [
                {"id": "%1", "location": f"{derived}:0.0", "command": "zsh", "label": "", "cwd": str(other)},
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
        self.assertIn(derived, stderr.getvalue())
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
        option_observer: list | None = None,
    ):
        # display-message resolves the pane reference to its canonical id.
        # rename-window is the rename mutation init makes.
        # set-window-option is the subtle window-status-style applied to the
        # newly-renamed agent window so the tmux status bar entry for that
        # window is colored without changing the window name.
        # set-option -p stamps the pane identity markers (@mozyo_agent_role /
        # @mozyo_workspace_id) that stabilize role binding (Redmine #11427).
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
            if tmux_args[:1] == ("set-option",):
                if option_observer is not None:
                    option_observer.append(tmux_args)
                return argparse.Namespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux args: {tmux_args}")
        return side_effect

    def _stage_jp_workspace(
        self,
        parent: Path,
        *,
        identifier: str = "giken-3500-jgmlife",
        basename: str = "2026PBL_ローカル",
    ) -> Path:
        # A Japanese-named workspace whose defaults declare a Redmine identifier;
        # `.mozyo-bridge/scaffold.json` marks the root so find_repo_root stops
        # here. derive_session_name then yields `mozyo-<identifier>`.
        workspace = (parent / basename).resolve()
        (workspace / ".mozyo-bridge").mkdir(parents=True)
        (workspace / ".mozyo-bridge" / "scaffold.json").write_text("{}", encoding="utf-8")
        (workspace / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
            "redmine:\n"
            "  default_project:\n"
            f"    identifier: {identifier}\n",
            encoding="utf-8",
        )
        return workspace

    def _isolate_home(self, tmp: str) -> Path:
        """Point MOZYO_BRIDGE_HOME at a temp dir for the duration of the test.

        Smart `init` now registers the workspace (Redmine #11427), which writes
        the home registry. Without this, registration would pollute the real
        ``~/.mozyo_bridge/registry.sqlite`` with throwaway temp-workspace rows.
        """
        home = Path(tmp) / "mozyo-home"
        patcher = patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return home

    def test_cmd_init_window_only_renames_window_in_place(self) -> None:
        # `--window-only` keeps the legacy low-level behavior: rename the window,
        # no session rename, no vscode write, no smart guard.
        args = argparse.Namespace(
            agent="claude", target="%5", window_only=True, no_vscode_settings=False
        )
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
            patch("mozyo_bridge.application.commands.rename_session") as rename_session, \
            patch(
                "mozyo_bridge.application.commands.rename_window",
                side_effect=lambda target, name: rename_calls.append((target, name)),
            ), \
            contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, cmd_init(args))

        self.assertEqual([("agents:1", "claude")], rename_calls)
        rename_session.assert_not_called()
        self.assertIn("agents:1 -> claude", stdout.getvalue())
        self.assertIn("window-only", stdout.getvalue())

    def test_cmd_init_no_target_uses_current_pane(self) -> None:
        args = argparse.Namespace(
            agent="codex", target=None, window_only=True, no_vscode_settings=False
        )
        panes = [
            {"id": "%9", "location": "agents:2.0", "command": "node", "window_name": "zsh", "cwd": "/repo"},
        ]
        rename_calls: list[tuple] = []
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%9"), \
            patch(
                "mozyo_bridge.application.commands.run_tmux",
                side_effect=self._init_run_tmux_side_effect("%9"),
            ), \
            patch("mozyo_bridge.application.commands.pane_location", return_value="agents:2.0"), \
            patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
            patch("mozyo_bridge.application.commands.rename_session"), \
            patch(
                "mozyo_bridge.application.commands.rename_window",
                side_effect=lambda target, name: rename_calls.append((target, name)),
            ), \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd_init(args))

        self.assertEqual([("agents:2", "codex")], rename_calls)

    def test_cmd_init_adopts_fallback_session_into_workspace(self) -> None:
        # Headline smart adoption (Redmine #11367 design #54505): a Japanese
        # workspace in a tmux-integrated fallback session `___________` is
        # adopted into `mozyo-giken-3500-jgmlife` — session renamed, vscode
        # settings pinned, window renamed, style applied.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            self._isolate_home(tmp)
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=False, no_vscode_settings=False
            )
            panes = [
                {"id": "%5", "location": "___________:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            session_calls: list[tuple] = []
            window_calls: list[tuple] = []
            style_calls: list[tuple] = []
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="___________:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch(
                    "mozyo_bridge.application.commands.rename_session",
                    side_effect=lambda old, new: session_calls.append((old, new)),
                ), \
                patch(
                    "mozyo_bridge.application.commands.rename_window",
                    side_effect=lambda target, name: window_calls.append((target, name)),
                ), \
                patch(
                    "mozyo_bridge.application.commands.apply_window_subtle_style",
                    side_effect=lambda session, agent: style_calls.append((session, agent)),
                ), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_init(args))

            self.assertEqual([("___________", "mozyo-giken-3500-jgmlife")], session_calls)
            self.assertEqual([("mozyo-giken-3500-jgmlife:1", "claude")], window_calls)
            self.assertEqual([("mozyo-giken-3500-jgmlife", "claude")], style_calls)

            settings = json.loads(
                (workspace / ".vscode" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "mozyo-giken-3500-jgmlife",
                settings["tmux-integrated.sessionName"],
            )

            out = stdout.getvalue()
            self.assertIn("adopted %5 into session 'mozyo-giken-3500-jgmlife' as claude", out)
            self.assertIn("renamed session '___________' -> 'mozyo-giken-3500-jgmlife'", out)

    def test_cmd_init_in_expected_session_pins_vscode_without_session_rename(self) -> None:
        # When the pane is already in the expected session, smart init pins the
        # vscode setting and renames the window but does not rename the session.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            self._isolate_home(tmp)
            args = argparse.Namespace(
                agent="codex", target="%5", window_only=False, no_vscode_settings=False
            )
            panes = [
                {"id": "%5", "location": "mozyo-giken-3500-jgmlife:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            window_calls: list[tuple] = []
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="mozyo-giken-3500-jgmlife:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.rename_session") as rename_session, \
                patch(
                    "mozyo_bridge.application.commands.rename_window",
                    side_effect=lambda target, name: window_calls.append((target, name)),
                ), \
                patch("mozyo_bridge.application.commands.apply_window_subtle_style"), \
                contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cmd_init(args))

            rename_session.assert_not_called()
            self.assertEqual([("mozyo-giken-3500-jgmlife:1", "codex")], window_calls)
            settings = json.loads(
                (workspace / ".vscode" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual("mozyo-giken-3500-jgmlife", settings["tmux-integrated.sessionName"])

    def test_cmd_init_no_vscode_settings_skips_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            self._isolate_home(tmp)
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=False, no_vscode_settings=True
            )
            panes = [
                {"id": "%5", "location": "___________:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            session_calls: list[tuple] = []
            window_calls: list[tuple] = []
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="___________:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch(
                    "mozyo_bridge.application.commands.rename_session",
                    side_effect=lambda old, new: session_calls.append((old, new)),
                ), \
                patch(
                    "mozyo_bridge.application.commands.rename_window",
                    side_effect=lambda target, name: window_calls.append((target, name)),
                ), \
                patch("mozyo_bridge.application.commands.apply_window_subtle_style"), \
                contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cmd_init(args))

            self.assertEqual([("___________", "mozyo-giken-3500-jgmlife")], session_calls)
            self.assertEqual([("mozyo-giken-3500-jgmlife:1", "claude")], window_calls)
            self.assertFalse((workspace / ".vscode" / "settings.json").exists())

    def test_cmd_init_refuses_when_window_name_collides_in_same_session(self) -> None:
        # Another window already named `claude` in the same session is ambiguous
        # for the resolver. Smart init refuses before any mutation.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=False, no_vscode_settings=False
            )
            panes = [
                {"id": "%1", "location": "mozyo-giken-3500-jgmlife:0.0", "command": "claude", "window_name": "claude", "cwd": str(workspace)},
                {"id": "%5", "location": "mozyo-giken-3500-jgmlife:2.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="mozyo-giken-3500-jgmlife:2.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.rename_session") as rename_session, \
                patch("mozyo_bridge.application.commands.rename_window") as rename_window, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    cmd_init(args)

            rename_window.assert_not_called()
            rename_session.assert_not_called()
            self.assertIn("mozyo-giken-3500-jgmlife:0(%1)", stderr.getvalue())
            self.assertIn("'claude'", stderr.getvalue())

    def test_cmd_init_allows_same_agent_name_in_a_different_session(self) -> None:
        # Cross-session `claude` windows are legitimate (one per repo). init only
        # refuses same-session collisions.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            self._isolate_home(tmp)
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=False, no_vscode_settings=True
            )
            panes = [
                {"id": "%2", "location": "other:0.0", "command": "claude", "window_name": "claude", "cwd": "/elsewhere"},
                {"id": "%5", "location": "mozyo-giken-3500-jgmlife:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            window_calls: list[tuple] = []
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="mozyo-giken-3500-jgmlife:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.rename_session"), \
                patch(
                    "mozyo_bridge.application.commands.rename_window",
                    side_effect=lambda target, name: window_calls.append((target, name)),
                ), \
                patch("mozyo_bridge.application.commands.apply_window_subtle_style"), \
                contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cmd_init(args))

            self.assertEqual([("mozyo-giken-3500-jgmlife:1", "claude")], window_calls)

    def test_cmd_init_registers_unregistered_workspace(self) -> None:
        # Redmine #11427: smart init converts an unregistered workspace into a
        # durable registration — a home-registry row and a local anchor — and
        # adopts the registered canonical session.
        from mozyo_bridge.workspace_registry import (
            load_workspace_by_path,
            read_anchor,
            registry_path,
        )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            home = self._isolate_home(tmp)
            self.assertFalse(registry_path(home).exists())
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=False, no_vscode_settings=True
            )
            panes = [
                {"id": "%5", "location": "___________:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="___________:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.rename_session"), \
                patch("mozyo_bridge.application.commands.rename_window"), \
                patch("mozyo_bridge.application.commands.apply_window_subtle_style"), \
                contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cmd_init(args))

            record = load_workspace_by_path(workspace, home=home)
            self.assertIsNotNone(record)
            self.assertEqual(record.canonical_session, "mozyo-giken-3500-jgmlife")
            anchor = read_anchor(workspace)
            self.assertIsNotNone(anchor)
            self.assertEqual(anchor["workspace_id"], record.workspace_id)

    def test_cmd_init_reuses_registered_canonical_session_not_path_derivation(self) -> None:
        # A registered workspace keeps its canonical session even when the
        # derivation input later changes: smart init must read the registry,
        # not re-derive from the (now different) workspace-defaults identifier.
        from mozyo_bridge.workspace_registry import (
            load_workspace_by_path,
            register_workspace,
        )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            home = self._isolate_home(tmp)
            registered = register_workspace(workspace, home=home)
            canonical = registered.record.canonical_session
            # Mutate the derivation input so a re-derive WOULD yield a new name.
            (workspace / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
                "redmine:\n  default_project:\n    identifier: giken-9999-changed\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                agent="codex", target="%5", window_only=False, no_vscode_settings=True
            )
            panes = [
                {"id": "%5", "location": f"{canonical}:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            window_calls: list[tuple] = []
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value=f"{canonical}:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch(
                    "mozyo_bridge.application.commands.rename_session"
                ) as rename_session, \
                patch(
                    "mozyo_bridge.application.commands.rename_window",
                    side_effect=lambda target, name: window_calls.append((target, name)),
                ), \
                patch("mozyo_bridge.application.commands.apply_window_subtle_style"), \
                contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cmd_init(args))

            rename_session.assert_not_called()
            self.assertEqual([(f"{canonical}:1", "codex")], window_calls)
            record = load_workspace_by_path(workspace, home=home)
            self.assertEqual(record.canonical_session, canonical)
            self.assertEqual(record.workspace_id, registered.record.workspace_id)

    def test_cmd_init_window_only_does_not_register(self) -> None:
        # `--window-only` stays a low-level escape hatch: no registry row, no
        # anchor (Redmine #11427).
        from mozyo_bridge.workspace_registry import anchor_path, registry_path

        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            home = self._isolate_home(tmp)
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=True, no_vscode_settings=True
            )
            panes = [
                {"id": "%5", "location": "agents:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="agents:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.rename_window"), \
                patch("mozyo_bridge.application.commands.apply_window_subtle_style"), \
                contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cmd_init(args))

            self.assertFalse(registry_path(home).exists())
            self.assertFalse(anchor_path(workspace).exists())

    def test_cmd_init_does_not_register_when_conflict_detected(self) -> None:
        # Fail-closed ordering: a detectable same-session window conflict aborts
        # BEFORE any registry write, so no durable identity is left behind.
        from mozyo_bridge.workspace_registry import registry_path

        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            home = self._isolate_home(tmp)
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=False, no_vscode_settings=True
            )
            panes = [
                {"id": "%3", "location": "___________:2.0", "command": "claude", "window_name": "claude", "cwd": str(workspace)},
                {"id": "%5", "location": "___________:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="___________:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cmd_init(args)

            self.assertFalse(registry_path(home).exists())

    def test_cmd_init_binds_agent_role_pane_marker(self) -> None:
        # Redmine #11427 / #11822: smart init stamps @mozyo_agent_role (+ the
        # registered @mozyo_workspace_id) on the adopted pane so the resolver
        # reports a strong pane_option role rather than inferring from the window
        # name alone.
        from mozyo_bridge.workspace_registry import load_workspace_by_path

        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            home = self._isolate_home(tmp)
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=False, no_vscode_settings=True
            )
            panes = [
                {"id": "%5", "location": "___________:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            option_calls: list[tuple] = []
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect(
                        "%5", option_observer=option_calls
                    ),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="___________:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch("mozyo_bridge.application.commands.rename_session"), \
                patch("mozyo_bridge.application.commands.rename_window"), \
                patch("mozyo_bridge.application.commands.apply_window_subtle_style"), \
                contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cmd_init(args))

            self.assertIn(
                ("set-option", "-p", "-t", "%5", "@mozyo_agent_role", "claude"),
                option_calls,
            )
            record = load_workspace_by_path(workspace, home=home)
            self.assertIn(
                ("set-option", "-p", "-t", "%5", "@mozyo_workspace_id", record.workspace_id),
                option_calls,
            )

    def test_cmd_init_fails_closed_on_meaningful_foreign_session(self) -> None:
        # A meaningful (non-fallback) session name different from expected is not
        # renamed; the error preserves the explicit target in the --window-only
        # suggestion (Redmine #11367 review #54498).
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=False, no_vscode_settings=False
            )
            panes = [
                {"id": "%5", "location": "human-work:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="human-work:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.rename_session") as rename_session, \
                patch("mozyo_bridge.application.commands.rename_window") as rename_window, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    cmd_init(args)

            rename_session.assert_not_called()
            rename_window.assert_not_called()
            message = stderr.getvalue()
            self.assertIn("human-work", message)
            self.assertIn("mozyo-giken-3500-jgmlife", message)
            self.assertIn("mozyo-bridge init claude %5 --window-only", message)

    def test_cmd_init_fails_closed_when_expected_session_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=False, no_vscode_settings=False
            )
            panes = [
                {"id": "%5", "location": "___________:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(workspace)},
            ]
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="___________:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=True), \
                patch("mozyo_bridge.application.commands.rename_session") as rename_session, \
                patch("mozyo_bridge.application.commands.rename_window") as rename_window, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    cmd_init(args)

            rename_session.assert_not_called()
            rename_window.assert_not_called()
            message = stderr.getvalue()
            self.assertIn("already exists", message)
            self.assertIn("mozyo-giken-3500-jgmlife", message)

    def test_cmd_init_fails_closed_when_workspace_root_unconfident(self) -> None:
        # A cwd with no repo / workspace marker cannot be confidently adopted.
        with tempfile.TemporaryDirectory() as tmp:
            bare = (Path(tmp) / "bare").resolve()
            bare.mkdir()
            args = argparse.Namespace(
                agent="claude", target="%5", window_only=False, no_vscode_settings=False
            )
            panes = [
                {"id": "%5", "location": "___________:1.0", "command": "zsh", "window_name": "zsh", "cwd": str(bare)},
            ]
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.application.commands.run_tmux",
                    side_effect=self._init_run_tmux_side_effect("%5"),
                ), \
                patch("mozyo_bridge.application.commands.pane_location", return_value="___________:1.0"), \
                patch("mozyo_bridge.application.commands.pane_lines", return_value=panes), \
                patch("mozyo_bridge.application.commands.rename_session") as rename_session, \
                patch("mozyo_bridge.application.commands.rename_window") as rename_window, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    cmd_init(args)

            rename_session.assert_not_called()
            rename_window.assert_not_called()
            self.assertIn("workspace root", stderr.getvalue())

    def test_confident_workspace_root_and_japanese_derivation(self) -> None:
        # AC fixation: a Japanese-named workspace whose defaults declare the
        # Redmine identifier `giken-3500-jgmlife` resolves to a confident root
        # that derives `mozyo-giken-3500-jgmlife` (Redmine #11367).
        from mozyo_bridge.application.commands import _confident_workspace_root
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._stage_jp_workspace(Path(tmp))
            root = _confident_workspace_root(str(workspace))
            self.assertEqual(workspace, root)
            self.assertEqual("mozyo-giken-3500-jgmlife", derive_session_name(root).name)

            # A marker-less cwd and an empty cwd are not confident.
            bare = (Path(tmp) / "bare").resolve()
            bare.mkdir()
            self.assertIsNone(_confident_workspace_root(str(bare)))
            self.assertIsNone(_confident_workspace_root(""))

    def test_is_fallback_session_name_only_matches_all_underscore(self) -> None:
        from mozyo_bridge.application.commands import _is_fallback_session_name

        self.assertTrue(_is_fallback_session_name("___________"))
        self.assertTrue(_is_fallback_session_name("__"))
        self.assertFalse(_is_fallback_session_name(""))
        self.assertFalse(_is_fallback_session_name("mozyo-giken-3500-jgmlife"))
        self.assertFalse(_is_fallback_session_name("2026PBL_____"))

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

    def test_resolve_status_session_falls_back_to_derived_name(self) -> None:
        # The non-tmux fallback now matches what bare `mozyo` creates: the
        # derived collision-safe session name, not the raw repo basename
        # (Redmine #10796). Keeping these in sync lets `status` find the
        # bare-`mozyo` session by name.
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "my_project"
            repo.mkdir()
            args = argparse.Namespace(session=None, repo=str(repo))

            with patch("mozyo_bridge.application.commands.current_session_name", return_value=None):
                resolved = resolve_status_session(args)

            self.assertEqual(derive_session_name(repo).name, resolved)
            self.assertTrue(resolved.startswith("mozyo-my-project-"))

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
        # No target supplied → no stale-source drift check (backward compatible).
        self.assertNotIn("source_drift", section)

    def _write_fake_source_checkout(self, root: Path, *, version: str) -> Path:
        """Lay down a checkout-shaped src/mozyo_bridge/__init__.py for drift tests."""
        pkg = root / "src" / "mozyo_bridge"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            f'"""stub."""\n__version__ = "{version}"\n', encoding="utf-8"
        )
        return pkg

    def test_cli_section_quiet_when_no_repo_local_source(self) -> None:
        """Redmine #11855: post-release normal usage (no checkout) must stay quiet.

        A target with no src/mozyo_bridge is the normal released-CLI case;
        there is nothing to be stale against, so doctor neither warns nor
        attaches a drift record.
        """
        from mozyo_bridge.application.doctor import doctor_cli_section

        with tempfile.TemporaryDirectory() as tmp:
            section = doctor_cli_section(Path(tmp))
        self.assertEqual("ok", section["status"])
        self.assertNotIn("source_drift", section)
        self.assertEqual([], section["next_action"])

    def test_cli_section_flags_stale_installed_cli_vs_repo_local_source(self) -> None:
        """Inside a checkout whose source version differs from the running
        install, doctor warns and points at the repo-local invocation."""
        from mozyo_bridge.application.doctor import doctor_cli_section

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Pick a version guaranteed to differ from the running __version__.
            self._write_fake_source_checkout(root, version=__version__ + ".dev999")
            section = doctor_cli_section(root)

        self.assertEqual("warning", section["status"])
        drift = section["source_drift"]
        self.assertEqual("version-differs", drift["relation"])
        self.assertEqual(__version__ + ".dev999", drift["source_version"])
        self.assertEqual(__version__, drift["running_version"])
        self.assertEqual(
            "PYTHONPATH=src python3 -m mozyo_bridge", drift["repo_local_invocation"]
        )
        self.assertTrue(section["next_action"])
        self.assertIn(
            "PYTHONPATH=src python3 -m mozyo_bridge", section["next_action"][0]
        )

    def test_repo_local_source_drift_none_when_running_is_the_source(self) -> None:
        """An editable install / PYTHONPATH=src run *is* the source → no drift."""
        from mozyo_bridge.application.doctor import repo_local_source_drift

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = self._write_fake_source_checkout(root, version="1.2.3")
            # running package path equals the checkout's source package.
            self.assertIsNone(
                repo_local_source_drift(root, pkg, "1.2.3")
            )

    def test_repo_local_source_drift_same_version_still_warns(self) -> None:
        """Redmine #11855 review j#57416: equal version, different commits.

        During active dogfooding the package version is not bumped until
        release, so an installed CLI and the checkout source can share
        __version__ yet differ by commits (the originating `agents targets`
        case). A same-version drift inside a checkout must therefore still
        warn and point at the repo-local invocation — version equality does
        not clear it.
        """
        from mozyo_bridge.application.doctor import (
            doctor_cli_section,
            repo_local_source_drift,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_fake_source_checkout(root, version=__version__)
            running_elsewhere = Path("/opt/site-packages/mozyo_bridge")
            drift = repo_local_source_drift(root, running_elsewhere, __version__)
            self.assertIsNotNone(drift)
            self.assertEqual("same-version", drift["relation"])

            # The real running install path is not the temp checkout source,
            # so the section warns even though versions are equal.
            section = doctor_cli_section(root)
            self.assertEqual("warning", section["status"])
            self.assertEqual("same-version", section["source_drift"]["relation"])
            self.assertTrue(section["next_action"])
            action = section["next_action"][0]
            self.assertIn("PYTHONPATH=src python3 -m mozyo_bridge", action)
            # The message explains that equal versions do not imply equal commits.
            self.assertIn("same version string does not guarantee", action)

    def test_repo_local_source_drift_unknown_version_warns(self) -> None:
        """Source present but __version__ unparseable → unknown relation warns."""
        from mozyo_bridge.application.doctor import doctor_cli_section

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "src" / "mozyo_bridge"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text('"""no version here."""\n', encoding="utf-8")
            section = doctor_cli_section(root)

        self.assertEqual("warning", section["status"])
        self.assertEqual("unknown", section["source_drift"]["relation"])

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

    def test_queue_enter_allows_cockpit_pane_via_role_option(self) -> None:
        # Redmine #11822: a cockpit pane lives in window `cockpit` but carries
        # its role on `@mozyo_agent_role`. The role-aware receiver binding must
        # accept it under queue-enter WITHOUT `--force` (the prior window-name
        # gate forced `--mode standard --force`).
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff", "send", "--to", "claude", "--source", "asana",
                "--kind", "reply", "--task-id", "T1", "--comment-id", "C1",
                "--target", "%2", "--mode", "queue-enter", "--submit-delay", "0",
            ],
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "cockpit",
                "pane_active": "1",
                "agent_role": "claude",
            },
        )
        self.assertEqual(0, result)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

    def test_queue_enter_rejects_cockpit_pane_with_mismatched_role_option(self) -> None:
        # Cockpit pane explicitly marked `codex` must not accept a `--to claude`
        # queue-enter send: role resolves to codex, fail-closed.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff", "send", "--to", "claude", "--source", "asana",
                "--kind", "implementation_request", "--task-id", "T1",
                "--comment-id", "C1", "--target", "%2", "--mode", "queue-enter",
            ],
            pane={
                "id": "%2",
                "location": "agents:1.0",
                "command": "node",
                "cwd": "/repo",
                "window_name": "cockpit",
                "pane_active": "1",
                "agent_role": "codex",
            },
            allow_exit=True,
        )
        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_allows_cockpit_pane_despite_layout_window_name(self) -> None:
        # Redmine #11822 audit regression (journal #57116): the live cockpit was
        # observed with a Claude-role pane (`@mozyo_agent_role=claude`) in a
        # window named `codex` (tmux layout / auto-naming). The explicit marker
        # is authoritative, so a `--to claude` queue-enter send must be ALLOWED
        # (no `--force`) — the layout window name is not a conflicting signal.
        result, sent, stdout, _stderr, _pane_text = self.run_handoff_with_fake_tmux(
            [
                "handoff", "send", "--to", "claude", "--source", "asana",
                "--kind", "reply", "--task-id", "T1", "--comment-id", "C1",
                "--target", "%2", "--mode", "queue-enter", "--submit-delay", "0",
            ],
            pane={
                "id": "%2",
                "location": "agents:1.0",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
                "agent_role": "claude",
            },
        )
        self.assertEqual(0, result)
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

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

    # --- Step 10 constrained cross-session admission (Redmine #11301) ---------------

    def _cross_session_codex_argv(self, target_repo, extra=None):
        # `--to codex` is the cross-session gateway receiver; queue-enter
        # cross-session admission requires an explicit pane and --target-repo.
        argv = [
            "handoff",
            "send",
            "--to",
            "codex",
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
        ]
        if target_repo is not None:
            argv += ["--target-repo", str(target_repo)]
        if extra:
            argv += extra
        return argv

    def test_queue_enter_cross_session_admitted_with_explicit_target_and_repo(
        self,
    ) -> None:
        # The constrained cross-session rail: an explicit pane in a foreign
        # session whose cwd resolves under the asserted scaffolded workspace
        # (`.mozyo-bridge/scaffold.json`) and passes the --target-repo gate is
        # admitted under queue-enter — no manual --mode standard fallback.
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "人形使い"
            (workspace / ".mozyo-bridge").mkdir(parents=True)
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            (workspace / "src").mkdir()

            result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
                self._cross_session_codex_argv(workspace),
                pane={
                    "id": "%2",
                    "location": "other:0.1",
                    "command": "node",
                    "cwd": str(workspace / "src"),
                    "window_name": "codex",
                    "pane_active": "1",
                },
                current_session="agents",
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

    def test_queue_enter_cross_session_blocked_without_target_repo(self) -> None:
        # Cross-session without the identity gate stays fail-closed: even the
        # gateway receiver (codex) with an explicit pane must assert
        # --target-repo to leave the same-session rail.
        result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
            self._cross_session_codex_argv(None),
            pane={
                "id": "%2",
                "location": "other:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
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
        self.assertIn("target_repo=unset", stderr)

    def test_queue_enter_cross_session_blocked_on_target_repo_mismatch(self) -> None:
        # Cross-session admission at Step 10 only opens the door; the
        # --target-repo gate still fails closed when the target pane's inferred
        # workspace root differs from the asserted one.
        with tempfile.TemporaryDirectory() as tmp_str:
            expected = Path(tmp_str) / "人形使い"
            (expected / ".mozyo-bridge").mkdir(parents=True)
            (expected / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            other = Path(tmp_str) / "other_repo"
            (other / "src").mkdir(parents=True)
            (other / "pyproject.toml").write_text("", encoding="utf-8")

            result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
                self._cross_session_codex_argv(expected),
                pane={
                    "id": "%2",
                    "location": "other:0.1",
                    "command": "node",
                    "cwd": str(other / "src"),
                    "window_name": "codex",
                    "pane_active": "1",
                },
                current_session="agents",
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_repo_mismatch", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_cross_session_non_agent_target_still_blocks(self) -> None:
        # The admitted cross-session rail does not bypass Step 12: a foreground
        # process that is not agent-compatible is still rejected before typing,
        # even with a passing --target-repo identity gate.
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "人形使い"
            (workspace / ".mozyo-bridge").mkdir(parents=True)
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            (workspace / "src").mkdir()

            result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
                self._cross_session_codex_argv(workspace),
                pane={
                    "id": "%2",
                    "location": "other:0.1",
                    "command": "vim",
                    "cwd": str(workspace / "src"),
                    "window_name": "codex",
                    "pane_active": "1",
                },
                current_session="agents",
                allow_exit=True,
            )

        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(call[:3] == ("send-keys", "-t", "%2") for call in sent))
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_not_agent", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_cross_session_to_claude_still_routes_through_gateway(
        self,
    ) -> None:
        # Cross-session admission must not let `--to claude` deliver directly
        # into a foreign workspace's Claude pane. Step 10 admits (explicit
        # target + --target-repo), but the cross-session Claude gate then fails
        # closed and points back to the codex-gateway path.
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "人形使い"
            (workspace / ".mozyo-bridge").mkdir(parents=True)
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            (workspace / "src").mkdir()

            argv = self._cross_session_codex_argv(workspace)
            argv[argv.index("codex")] = "claude"
            result, sent, stdout, stderr, _pane_text = self.run_handoff_with_fake_tmux(
                argv,
                pane={
                    "id": "%2",
                    "location": "other:0.1",
                    "command": "claude",
                    "cwd": str(workspace / "src"),
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
        self.assertEqual("cross_session_claude", outcome["reason"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

    def test_queue_enter_same_session_still_admits_on_current_rail(self) -> None:
        # Regression: the constrained cross-session admission must not change
        # the same-session default. A same-session codex pane is still admitted
        # without requiring --target-repo.
        result, sent, stdout, _stderr, pane_text = self.run_handoff_with_fake_tmux(
            self._cross_session_codex_argv(None),
            pane={
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": "/repo",
                "window_name": "codex",
                "pane_active": "1",
            },
            current_session="agents",
        )

        self.assertEqual(0, result)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])
        outcome = self._outcome_from_stdout(stdout)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual(MODE_QUEUE_ENTER, outcome["mode"])

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


class ExecutionRootPropagationTest(unittest.TestCase):
    """Nested project execution-root / workdir propagation (Redmine #12098).

    A handoff must be able to carry an explicit target execution root distinct
    from the pane cwd / cross-workspace repo root, so a receiver recovers a
    nested project root from the durable record instead of pane scrollback.
    Abstract `/workspace/...` placeholders are used deliberately — no personal
    home path or private project absolute path in tracked test files
    (`vibes/docs/rules/public-private-boundary.md`).
    """

    def test_build_execution_root_derives_relative_pointer_under_repo(self) -> None:
        er = build_execution_root(
            "/workspace/project-alpha/services/api",
            repo_root_abs="/workspace/project-alpha",
        )

        self.assertEqual("/workspace/project-alpha/services/api", er.workdir)
        self.assertEqual("/workspace/project-alpha", er.repo_root)
        self.assertEqual("services/api", er.relative)
        self.assertTrue(er.is_nested)
        self.assertEqual(
            "`services/api` (relative to the target repo root)", er.portable_pointer()
        )

    def test_build_execution_root_resolves_nested_unicode_path(self) -> None:
        # The #12098 reproduction nested a Japanese checkout below a Japanese
        # workspace root; NFC/NFD spelling drift must still yield a relative
        # pointer rather than collapsing to absolute-only.
        import unicodedata

        repo = unicodedata.normalize("NFC", "/workspace/IT導入/anchor")
        workdir = unicodedata.normalize("NFD", "/workspace/IT導入/anchor/rovoice/shinsei_llm")

        er = build_execution_root(workdir, repo_root_abs=repo)

        self.assertEqual("rovoice/shinsei_llm", er.relative)
        self.assertTrue(er.is_nested)

    def test_build_execution_root_out_of_tree_omits_absolute_from_pasteable(self) -> None:
        # An out-of-tree workdir has no repo-relative form. The absolute path
        # must NOT surface in the pane body or the pasteable record (Redmine
        # #12098 review j#59662); it stays only in the structured outcome.
        er = build_execution_root(
            "/workspace/other/checkout", repo_root_abs="/workspace/project-alpha"
        )

        self.assertIsNone(er.relative)
        self.assertFalse(er.is_nested)
        self.assertIsNone(er.portable_pointer())
        self.assertNotIn("/workspace/other/checkout", er.record_pointer())
        self.assertNotIn("/workspace/other/checkout", er.notification_clause())
        self.assertIn("execution_root.workdir", er.record_pointer())
        # The absolute is still retained as a structured runtime fact.
        self.assertEqual("/workspace/other/checkout", er.workdir)

    def test_build_execution_root_equal_to_repo_root_is_not_nested(self) -> None:
        er = build_execution_root(
            "/workspace/project-alpha", repo_root_abs="/workspace/project-alpha"
        )

        self.assertEqual(".", er.relative)
        self.assertFalse(er.is_nested)

    def test_build_execution_root_without_anchor_omits_absolute_from_pasteable(self) -> None:
        er = build_execution_root("/workspace/project-alpha/services/api")

        self.assertIsNone(er.repo_root)
        self.assertIsNone(er.relative)
        # No anchor → no portable form → absolute is kept out of pasteable text.
        self.assertIsNone(er.portable_pointer())
        self.assertNotIn(
            "/workspace/project-alpha/services/api", er.record_pointer()
        )
        self.assertEqual("/workspace/project-alpha/services/api", er.workdir)

    def test_notification_body_appends_execution_root_clause(self) -> None:
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")
        er = build_execution_root(
            "/workspace/project-alpha/services/api",
            repo_root_abs="/workspace/project-alpha",
        )

        body = build_notification_body(
            anchor, "implementation_request", None, "claude", execution_root=er
        )

        # The durable-anchor contract is preserved verbatim ...
        self.assertIn("durable anchor", body)
        self.assertIn("read it from the source-of-truth", body)
        # ... and the execution-root pointer is appended as a portable,
        # confirm-from-anchor hint (not a new authority).
        self.assertIn("Target execution root: `services/api`", body)
        self.assertIn("confirm it from the durable anchor", body)

    def test_notification_body_unchanged_without_execution_root(self) -> None:
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")

        with_none = build_notification_body(
            anchor, "implementation_request", None, "claude"
        )
        explicit_none = build_notification_body(
            anchor, "implementation_request", None, "claude", execution_root=None
        )

        self.assertEqual(with_none, explicit_none)
        self.assertNotIn("Target execution root", with_none)

    def test_make_outcome_carries_execution_root_in_json(self) -> None:
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")
        er = build_execution_root(
            "/workspace/project-alpha/services/api",
            repo_root_abs="/workspace/project-alpha",
        )

        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[marker]",
            execution_root=er,
        )

        payload = json.loads(outcome.to_json())
        self.assertEqual(
            {
                "workdir": "/workspace/project-alpha/services/api",
                "repo_root": "/workspace/project-alpha",
                "relative": "services/api",
            },
            payload["execution_root"],
        )

    def test_make_outcome_execution_root_defaults_to_none(self) -> None:
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[marker]",
        )

        self.assertIsNone(outcome.execution_root)
        self.assertIsNone(json.loads(outcome.to_json())["execution_root"])

    def test_delivery_record_shows_relative_pointer_without_absolute(self) -> None:
        # Pasteable record carries the portable repo-relative pointer only;
        # the absolute workdir must never land in a Redmine-pastable record
        # (Redmine #12098 review j#59662; public-private-boundary.md).
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")
        er = build_execution_root(
            "/workspace/project-alpha/services/api",
            repo_root_abs="/workspace/project-alpha",
        )
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[marker]",
            execution_root=er,
        )

        record = build_delivery_record(outcome)

        self.assertIn(
            "- Target execution root: `services/api` (relative to the target repo root)",
            record,
        )
        # No absolute path leaks into the pasteable markdown record ...
        self.assertNotIn("/workspace/project-alpha", record)
        self.assertNotIn("abs ", record)
        # ... while the structured outcome still retains it for tooling/replay.
        self.assertEqual(
            "/workspace/project-alpha/services/api",
            json.loads(outcome.to_json())["execution_root"]["workdir"],
        )

    def test_delivery_record_execution_root_dash_when_absent(self) -> None:
        anchor = normalize_anchor("redmine", issue="12098", journal="59652")
        outcome = make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%2",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[marker]",
        )

        record = build_delivery_record(outcome)

        self.assertIn("- Target execution root: —", record)


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

    def test_workdir_propagates_nested_execution_root_to_record_and_body(self) -> None:
        # Redmine #12098: an explicit --workdir below the pane cwd / repo root
        # must surface a repo-relative execution-root pointer in both the typed
        # notification body and the durable delivery record, so the receiver
        # recovers the nested project root without pane scrollback.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp).resolve()
            (repo_root / ".git").mkdir()
            nested = repo_root / "services" / "api"
            nested.mkdir(parents=True)
            pane = {
                "id": "%2",
                "location": "agents:0.1",
                "command": "node",
                "cwd": str(repo_root),
                "window_name": "claude",
            }

            result, sent, stdout = self.run_handoff_with_fake_tmux(
                [
                    "handoff",
                    "send",
                    "--to",
                    "claude",
                    "--source",
                    "redmine",
                    "--kind",
                    "implementation_request",
                    "--issue",
                    "12098",
                    "--journal",
                    "59652",
                    "--target",
                    "%2",
                    "--target-repo",
                    str(repo_root),
                    "--workdir",
                    str(nested),
                    "--mode",
                    "standard",
                    "--submit-delay",
                    "0",
                ],
                pane=pane,
            )

        self.assertEqual(0, result)
        # Durable record carries the portable relative pointer only — the
        # absolute nested path must not leak into a Redmine-pastable record
        # (Redmine #12098 review j#59662).
        self.assertIn(
            "- Target execution root: `services/api` (relative to the target repo root)",
            stdout,
        )
        self.assertNotIn(str(nested), stdout.split("{", 1)[0])
        # The typed pane body carries the portable pointer and keeps the
        # confirm-from-anchor contract, without the absolute path.
        typed = "".join(call[-1] for call in sent if call[:4] == ("send-keys", "-t", "%2", "-l"))
        self.assertIn("Target execution root: `services/api`", typed)
        self.assertIn("confirm it from the durable anchor", typed)
        self.assertNotIn(str(nested), typed)
        # Structured outcome retains both the relative pointer and the absolute
        # workdir for tooling/replay (the runtime fact, separate from the
        # pasteable markdown).
        json_lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
        outcome = json.loads(json_lines[-1])
        self.assertEqual("services/api", outcome["execution_root"]["relative"])
        self.assertEqual(str(nested), outcome["execution_root"]["workdir"])

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
    inference via REPO_ROOT_MARKERS, and the ``mozyo-bridge agents list``
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

    def test_infer_repo_root_uses_scaffolded_workspace_marker(self) -> None:
        # Redmine #11301: a non-git scaffolded workspace must report its own
        # root from a pane cwd under it, instead of leaking up to the home
        # directory (which fail-closes the cross-workspace --target-repo gate).
        from mozyo_bridge.domain.agent_discovery import infer_repo_root

        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "gk-0999" / "人形使い"
            nested = workspace / "notes" / "deep"
            nested.mkdir(parents=True)
            (workspace / ".mozyo-bridge").mkdir()
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            self.assertEqual(
                str(workspace.resolve()), infer_repo_root(str(nested))
            )

    def test_infer_repo_root_prefers_deeper_scaffold_over_git_ancestor(self) -> None:
        # A scaffolded workspace nested inside a git repo is a distinct
        # workspace identity; the deeper scaffold marker wins. Existing git /
        # pyproject behavior for non-scaffolded trees is unchanged because the
        # walk still returns the deepest marker-bearing ancestor.
        from mozyo_bridge.domain.agent_discovery import infer_repo_root

        with tempfile.TemporaryDirectory() as tmp_str:
            outer = Path(tmp_str) / "outer_git"
            (outer / ".git").mkdir(parents=True)
            workspace = outer / "embedded_workspace"
            nested = workspace / "src"
            nested.mkdir(parents=True)
            (workspace / ".mozyo-bridge").mkdir()
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            self.assertEqual(
                str(workspace.resolve()), infer_repo_root(str(nested))
            )

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
            "SESSION\tWINDOW\tIDX\tPANE\tACTIVE\tKIND\tROLE_SOURCE\tCONFIDENCE\t"
            "PROCESS\tREPO_ROOT\tCWD\tAMBIGUOUS\tOTHER_VIEWS",
            output,
        )
        # window-name rail: role classified from the `claude` window, strong.
        self.assertIn(
            "sess_a\tclaude\t0\t%1\t1\tclaude\twindow_name\tstrong\tclaude", output
        )

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
        # cannot be walked to a REPO_ROOT_MARKERS-bearing directory.
        self.assertIn("repo_root", record)

    def test_fold_agents_by_pane_collapses_grouped_sessions(self) -> None:
        # Redmine #11628: the same pane in two grouped sessions is ONE agent.
        # The canonical view is the one matching the resolver's canonical
        # session name; the other membership lands in `views`.
        from mozyo_bridge.domain.agent_discovery import (
            discover_agents,
            fold_agents_by_pane,
        )

        resolved_roots: list[str] = []

        def resolver(root: str) -> str:
            resolved_roots.append(root)
            return "mozyo-giken-1750-labor"

        with patch(
            "mozyo_bridge.domain.agent_discovery.infer_repo_root",
            return_value="/repo",
        ):
            records = discover_agents(
                panes=[
                    {
                        "id": "%851",
                        "location": "1750-codex-view:1.0",
                        "command": "claude",
                        "cwd": "/repo",
                        "window_name": "claude",
                        "pane_active": "1",
                    },
                    {
                        "id": "%851",
                        "location": "mozyo-giken-1750-labor:2.0",
                        "command": "claude",
                        "cwd": "/repo",
                        "window_name": "claude",
                        "pane_active": "0",
                    },
                ]
            )
        folded = fold_agents_by_pane(records, resolve_canonical=resolver)

        self.assertEqual(1, len(folded))
        agent = folded[0]
        self.assertEqual("%851", agent.pane_id)
        self.assertEqual("mozyo-giken-1750-labor", agent.session)
        self.assertEqual("claude", agent.agent_kind)
        self.assertEqual(2, len(agent.views))
        flags = {view.session: view.canonical for view in agent.views}
        self.assertTrue(flags["mozyo-giken-1750-labor"])
        self.assertFalse(flags["1750-codex-view"])
        # The resolver runs once per distinct repo root, not per view.
        self.assertEqual(["/repo"], resolved_roots)

    def test_fold_agents_by_pane_without_resolver_is_deterministic(self) -> None:
        from mozyo_bridge.domain.agent_discovery import (
            discover_agents,
            fold_agents_by_pane,
        )

        records = discover_agents(
            panes=[
                {
                    "id": "%7",
                    "location": "zzz-view:1.0",
                    "command": "claude",
                    "cwd": "",
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%7",
                    "location": "aaa-view:1.0",
                    "command": "claude",
                    "cwd": "",
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "",
                    "location": "ghost:1.0",
                    "command": "zsh",
                    "cwd": "",
                    "window_name": "zsh",
                    "pane_active": "1",
                },
            ]
        )
        folded = fold_agents_by_pane(records)

        # The empty-pane_id line cannot carry a stable identity and is
        # dropped; the canonical view falls back to session sort order.
        self.assertEqual(1, len(folded))
        self.assertEqual("aaa-view", folded[0].session)

    def test_filter_agents_session_matches_grouped_view(self) -> None:
        # A folded pane is a member of every session it appears in, so the
        # --session filter must match alias views, not only the canonical one.
        from mozyo_bridge.domain.agent_discovery import (
            discover_agents,
            fold_agents_by_pane,
            filter_agents,
        )

        records = fold_agents_by_pane(
            discover_agents(
                panes=[
                    {
                        "id": "%7",
                        "location": "alias-view:1.0",
                        "command": "claude",
                        "cwd": "",
                        "window_name": "claude",
                        "pane_active": "1",
                    },
                    {
                        "id": "%7",
                        "location": "canonical-session:1.0",
                        "command": "claude",
                        "cwd": "",
                        "window_name": "claude",
                        "pane_active": "1",
                    },
                ]
            ),
            resolve_canonical=None,
        )
        self.assertEqual(
            ["%7"],
            [r.pane_id for r in filter_agents(records, session="alias-view")],
        )
        self.assertEqual(
            [],
            [r.pane_id for r in filter_agents(records, session="absent")],
        )

    def test_cmd_agents_list_json_folds_grouped_sessions(self) -> None:
        # End-to-end #11628: `agents list --json` emits ONE record for a
        # grouped pane, with both memberships in `views` and the canonical
        # session (the workspace's derived session name) at the top level.
        from mozyo_bridge.application.commands import cmd_agents_list
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp_str:
            home = Path(tmp_str) / "home"
            repo = Path(tmp_str) / "repo"
            (repo / ".git").mkdir(parents=True)
            canonical = derive_session_name(repo).name
            panes = [
                {
                    "id": "%851",
                    "location": "grouped-view:1.0",
                    "command": "claude",
                    "cwd": str(repo),
                    "window_name": "claude",
                    "pane_active": "1",
                },
                {
                    "id": "%851",
                    "location": f"{canonical}:2.0",
                    "command": "claude",
                    "cwd": str(repo),
                    "window_name": "claude",
                    "pane_active": "1",
                },
            ]
            args = argparse.Namespace(session=None, agent=None, as_json=True)
            with patch.dict(
                "os.environ", {"MOZYO_BRIDGE_HOME": str(home)}, clear=False
            ), patch("mozyo_bridge.application.commands.require_tmux"), \
                patch(
                    "mozyo_bridge.domain.agent_discovery.pane_lines",
                    return_value=panes,
                ), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cmd_agents_list(args))
        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, len(payload))
        record = payload[0]
        self.assertEqual("%851", record["pane_id"])
        self.assertEqual(canonical, record["session"])
        self.assertEqual(2, len(record["views"]))
        canonical_flags = {
            view["session"]: view["canonical"] for view in record["views"]
        }
        self.assertTrue(canonical_flags[canonical])
        self.assertFalse(canonical_flags["grouped-view"])

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
        # Regression for Redmine #10332 review #49646, updated for #11301.
        # Cross-session `--to codex` is the documented gateway path. Since
        # #11301 the default `queue-enter` rail admits a cross-session target
        # only under the constrained identity gate (explicit `--target` PLUS a
        # passing `--target-repo`). This send supplies the explicit `--target`
        # but NO `--target-repo`, so the identity gate is not satisfied and the
        # rail still fails closed with `invalid_args` before any typing. This
        # pins that cross-session admission is not granted without the
        # workspace identity assertion.
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

    def test_cross_session_claude_outcome_guides_to_codex_gateway(self) -> None:
        # Regression for Redmine #10332 review #49646, updated for #11301.
        # The recovery path from a `cross_session_claude` block must steer the
        # sender to the codex-gateway path with workspace identity:
        # `--to codex --target <target_session>:codex --target-repo <root>`.
        # Since #11301 that gateway send is admitted on the *default*
        # queue-enter rail when --target is explicit and --target-repo passes,
        # so the guidance must NOT present `--mode standard` as required; it is
        # only a fallback. The next_action_for / outcome narrative / die()
        # message must all carry the gateway + --target-repo hint.
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
        # The structured outcome's next_action must steer to the codex gateway
        # with the workspace identity gate so the sender's next attempt is
        # admitted on the default queue-enter rail. The die() trailer on stderr
        # must carry the same hint.
        self.assertIn("--to codex", outcome["next_action"])
        self.assertIn("--target-repo", outcome["next_action"])
        self.assertIn("--to codex", stderr)
        self.assertIn("--target-repo", stderr)
        # `--mode standard` must read as a fallback, not a requirement.
        self.assertIn("fallback", outcome["next_action"])
        # The durable record (markdown) must repeat the gateway hint so
        # auditors and downstream agents see it even when the structured
        # outcome is consumed and discarded.
        self.assertIn("--target-repo", stdout)

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

    def test_target_repo_gate_passes_for_scaffolded_non_git_workspace(self) -> None:
        # Redmine #11301: a non-git scaffolded workspace (only
        # `.mozyo-bridge/scaffold.json`) is a first-class identity root, so a
        # pane whose cwd is under it satisfies `--target-repo <workspace>`.
        # The gate must NOT fire; the handoff dies later on marker_timeout.
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "人形使い"
            (workspace / ".mozyo-bridge").mkdir(parents=True)
            (workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            (workspace / "src").mkdir()

            pane = {
                "id": "%8",
                "location": "local:1.0",
                "command": "claude",
                "cwd": str(workspace / "src"),
                "window_name": "claude",
                "pane_active": "1",
            }
            _exc, sent, stdout, _stderr = self.run_handoff(
                [
                    "handoff",
                    "send",
                    "--to",
                    "claude",
                    "--source",
                    "redmine",
                    "--issue",
                    "11301",
                    "--journal",
                    "54071",
                    "--kind",
                    "implementation_request",
                    "--target",
                    "%8",
                    "--target-repo",
                    str(workspace),
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
        # The identity gate let it through. It dies on marker_timeout (strict
        # standard mode, no marker observed), NOT on the repo gate.
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])

    def test_location_form_target_resolves_instead_of_pane_disappeared(self) -> None:
        # Redmine #11666: `--target '<session>:codex'` — the exact form the
        # cross-session guidance tells operators to use — used to die with
        # `pane disappeared after resolve` even though the pane existed.
        # With location→pane-id normalization it must reach the same
        # endpoint as a pane-id target (marker_timeout in standard mode),
        # not the resolver death.
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / "other-repo"
            (workspace / "src").mkdir(parents=True)
            (workspace / "pyproject.toml").write_text("", encoding="utf-8")

            pane = {
                "id": "%9",
                "location": "other:1.0",
                "command": "codex",
                "cwd": str(workspace / "src"),
                "window_name": "codex",
                "pane_active": "1",
            }
            with patch(
                "mozyo_bridge.domain.pane_resolver.resolve_pane_id",
                return_value="%9",
            ):
                _exc, _sent, stdout, _stderr = self.run_handoff(
                    [
                        "handoff",
                        "send",
                        "--to",
                        "codex",
                        "--source",
                        "redmine",
                        "--issue",
                        "11666",
                        "--journal",
                        "56072",
                        "--kind",
                        "implementation_request",
                        "--target",
                        "other:codex",
                        "--target-repo",
                        str(workspace),
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
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])

    def test_target_repo_gate_passes_across_unicode_normal_forms(self) -> None:
        # Redmine #11625: the pane cwd arrives in macOS NFD bytes while the
        # operator's `--target-repo` is typically NFC (copied from docs /
        # Redmine). Same directory, different bytes — the identity gate must
        # compare through Unicode normalization, not raw strings, so the
        # handoff proceeds (and then dies on marker_timeout, NOT the gate).
        import unicodedata as _ud

        nfd_name = _ud.normalize("NFD", "動画ドライブ")
        nfc_name = _ud.normalize("NFC", "動画ドライブ")
        self.assertNotEqual(nfd_name, nfc_name)
        with tempfile.TemporaryDirectory() as tmp_str:
            workspace = Path(tmp_str) / nfd_name
            (workspace / "src").mkdir(parents=True)
            (workspace / "pyproject.toml").write_text("", encoding="utf-8")
            nfc_spelling = str(Path(tmp_str) / nfc_name)

            pane = {
                "id": "%8",
                "location": "local:1.0",
                "command": "claude",
                "cwd": str(workspace / "src"),
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
                    "11625",
                    "--journal",
                    "55992",
                    "--kind",
                    "implementation_request",
                    "--target",
                    "%8",
                    "--target-repo",
                    nfc_spelling,
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
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])

    def test_target_repo_mismatch_hints_setup_when_identity_unestablished(
        self,
    ) -> None:
        # Redmine #11301 error UX: when the target cwd walks up to NO identity
        # marker at all, stay fail-closed but return a concrete setup hint
        # (scaffold the workspace) rather than forcing the operator to reason
        # about repo-root heuristics.
        with tempfile.TemporaryDirectory() as tmp_str:
            expected_workspace = Path(tmp_str) / "人形使い"
            (expected_workspace / ".mozyo-bridge").mkdir(parents=True)
            (expected_workspace / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )
            bare = Path(tmp_str) / "bare_no_marker"
            bare.mkdir()

            pane = {
                "id": "%8",
                "location": "local:1.0",
                "command": "claude",
                "cwd": str(bare),
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
                    "11301",
                    "--journal",
                    "54071",
                    "--kind",
                    "implementation_request",
                    "--target",
                    "%8",
                    "--target-repo",
                    str(expected_workspace),
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
        # The hint names the concrete setup action, not repo-root internals.
        self.assertIn("scaffold", stderr)
        self.assertIn(".mozyo-bridge/scaffold.json", stderr)


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


class SkillCrossWorkspaceGuidanceTest(unittest.TestCase):
    """Pin the cross-workspace gateway contract in the skill body.

    Updated for Redmine #11301. The earlier #10332 wording said the
    `cross_session_claude` recovery path must always name `--mode standard`
    (or `--mode pending`) because the default `queue-enter` rail rejected
    every cross-session target. Since #11301 that is no longer true: the
    default queue-enter rail admits a cross-session `--to codex` gateway
    send under a constrained identity gate (an explicit `--target` PLUS a
    passing `--target-repo`). `--mode standard` / `--mode pending` are now
    fallbacks, not a requirement. This test pins the new contract — gateway
    target form with `--target-repo`, the constrained-admission wording, and
    the non-git scaffold identity root — so a future rule-edit cannot
    silently revert to the stale "always pass --mode" guidance.
    """

    REQUIRED_GUIDANCE_MARKERS = (
        # Cross-Workspace Handoff section heading must be present.
        "## Cross-Workspace Handoff",
        # The gateway target form must stay copy-pasteable and now carries
        # the workspace identity gate that admits the send on the default
        # rail.
        "--to codex --target <target_session>:codex --target-repo",
        # The constrained cross-session admission contract (Redmine #11301).
        "constrained identity gate",
        "no `--mode` needed",
        # `--mode standard` / `--mode pending` must read as a fallback, not
        # as mandatory-because-queue-enter-rejects-all-cross-session.
        "remain available as fallbacks",
        # A scaffolded non-git workspace is a first-class identity root.
        ".mozyo-bridge/scaffold.json",
    )

    def _skill_workflow_body(self, *parts: str) -> str:
        return (ROOT.joinpath(*parts) / "references" / "workflow.md").read_text(
            encoding="utf-8"
        )

    def test_canonical_skill_keeps_constrained_gateway_guidance(self) -> None:
        body = self._skill_workflow_body("skills", "mozyo-bridge-agent")
        for marker in self.REQUIRED_GUIDANCE_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"skills/mozyo-bridge-agent/references/workflow.md is "
                    f"missing #11301 marker {marker!r}; cross-workspace "
                    f"gateway guidance regressed to stale wording."
                ),
            )

    def test_plugin_mirror_keeps_constrained_gateway_guidance(self) -> None:
        body = self._skill_workflow_body(
            "plugins", "mozyo-bridge-agent", "skills", "mozyo-bridge-agent"
        )
        for marker in self.REQUIRED_GUIDANCE_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"plugin skill mirror is missing #11301 marker "
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
        "## Sublane Coordinator Callback",
        "## Named Cockpit Groups And Multiple Local Cockpit Sessions",
        "## Coordinator Stop And Next-Action Standard",
        "## Owner Approval Aggregation",
        "## Stall And No-Progress Detection Standard",
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
        # Sublane coordinator callback (Redmine #11852). A sublane must
        # report handoff-worthy states back to the coordinator lane's
        # Codex with a durable anchor, cross-lane Codex-to-Codex, so the
        # work does not look stalled from the coordinator cockpit.
        "send a concise callback to the coordinator lane",
        "owner close approval requested",
        "The sublane's Codex owns the cross-lane callback",
        # Named cockpit groups — grouping vs identity separation
        # (Redmine #11853). A multi-cockpit layout must not become an
        # implicit cross-group send shortcut, and the cross-group rail
        # must route through the target group's Codex gateway.
        "A **cockpit group is a named tmux session**",
        "not the routing or identity source of truth",
        "route it through the **target group's Codex** pane",
        "Multiple cockpit sessions do not create a cross-session Claude shortcut",
        # Coordinator stop and next-action standard (Redmine #11860). Every
        # coordinator stop records a durable reason plus a three-part
        # next-action proposal and returns ready work to the queue, without
        # relaxing Close Approval Separation or self-authorizing a carve-out.
        "make every stop carry a next-action proposal",
        "A stop is justified only when the *only* remaining next actions are in the owner-approval range",
        "A next-action proposal is not self-authorization",
        "Hand gated work back to the queue, not to a held pane",
        # Owner approval aggregation (Redmine #11867). Owner-approval-waiting
        # always converges on the single main coordinator Codex, is never
        # resolved inside the sublane, and the waiting queue is enumerable
        # from the durable record independent of pane count.
        "The single owner-facing aggregation point is the main coordinator Codex",
        "A sublane never resolves owner approval inside its own lane",
        "owner-action-needed",
        "the owner-approval-waiting set is a property of the durable record, enumerable from the durable record, not by scanning panes",
        "Aggregation is not self-authorization",
        # Stall and no-progress detection (Redmine #11880). The coordinator
        # defines a stall candidate from the durable record, classifies it into
        # four states, treats a stale CLI as a distinct callback-delivery
        # failure, and records every stall check and re-notification.
        "A **stall candidate is a unit of work whose handoff was delivered but whose expected next durable journal has not appeared**",
        "`no_progress_after_handoff`",
        "`progress_without_callback`",
        "`callback_delivery_failed`",
        "`callback_not_attempted`",
        "Stale CLI is a distinct stall mode during a handoff or callback",
        "it records that fact on the issue",
        "Detection is not re-dispatch of completed work",
        # Workflow Change Verification policy.
        "Workflow Change Verification",
        "Claude implements the normal development task",
        # Redmine default-project resolution (Redmine #10689). The
        # workspace-local snippet path and the "explicit wins over
        # default" / "UNVERIFIED escalates" rules must stay in the
        # skill body so agents pick them up at session start.
        "Default project resolution",
        ".mozyo-bridge/redmine-defaults.md",
        ".mozyo-bridge/project-defaults.yaml",
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


class InstructionDoctorTest(unittest.TestCase):
    """Redmine #10854: opt-in `instruction doctor --profile redmine-codex`.

    Read-only, profile-aware check that a Redmine/Codex workspace carries
    the repo-root runtime config the bootstrap docs require. Pins the
    completion conditions: missing config fails, valid config passes,
    X-Default-Project mismatch fails, credential-shape values fail, and
    `.mcp.json` is parsed + secret-scanned while staying non-authoritative.
    """

    VALID_TOML = (
        "[redmine]\n"
        'default_project = "giken-3800-mozyo-bridge"\n'
        'default_project_name = "mozyo-bridge"\n'
        'default_project_url = "https://redmine.example.invalid/projects/x"\n'
        "\n"
        "[mcp_servers.redmine_epic_grid]\n"
        'url = "https://redmine.example.invalid/mcp/rpc"\n'
        'http_headers = { X-Default-Project = "giken-3800-mozyo-bridge" }\n'
    )

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def _write_codex(self, project: Path, toml_text: str) -> None:
        (project / ".codex").mkdir(parents=True, exist_ok=True)
        (project / ".codex" / "config.toml").write_text(toml_text, encoding="utf-8")

    def _result(self, project: Path) -> dict:
        rc, out = self.run_cli(
            ["instruction", "doctor", "--target", str(project), "--json"]
        )
        payload = json.loads(out)
        return {"rc": rc, "payload": payload}

    def _check_status(self, payload: dict, name: str) -> str | None:
        for check in payload["checks"]:
            if check["name"] == name:
                return check["status"]
        return None

    def test_missing_codex_config_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            r = self._result(project)
            self.assertEqual(1, r["rc"])
            self.assertFalse(r["payload"]["ok"])
            self.assertEqual(
                "fail", self._check_status(r["payload"], "codex_config_present")
            )

    def test_valid_repo_root_config_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._write_codex(project, self.VALID_TOML)
            r = self._result(project)
            self.assertEqual(0, r["rc"])
            self.assertTrue(r["payload"]["ok"])
            self.assertEqual(
                "ok",
                self._check_status(r["payload"], "codex_default_project_consistent"),
            )
            # No .mcp.json: deferral keeps it informational, not a failure.
            self.assertEqual(
                "info", self._check_status(r["payload"], "mcp_json_present")
            )

    def test_default_project_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            mismatched = self.VALID_TOML.replace(
                'X-Default-Project = "giken-3800-mozyo-bridge"',
                'X-Default-Project = "some-other-project"',
            )
            self._write_codex(project, mismatched)
            r = self._result(project)
            self.assertEqual(1, r["rc"])
            self.assertEqual(
                "fail",
                self._check_status(r["payload"], "codex_default_project_consistent"),
            )

    def test_credential_shaped_value_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            # Build the secret-shaped assignment at runtime so the test file
            # itself carries no release-tree-blocking credential literal.
            secret_line = "api_key" + " = " + '"' + "x" * 24 + '"\n'
            self._write_codex(project, self.VALID_TOML + secret_line)
            r = self._result(project)
            self.assertEqual(1, r["rc"])
            self.assertEqual(
                "fail",
                self._check_status(r["payload"], "codex_config_no_credentials"),
            )

    def test_invalid_toml_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._write_codex(project, "[redmine\nnot valid toml")
            r = self._result(project)
            self.assertEqual(1, r["rc"])
            self.assertEqual(
                "fail", self._check_status(r["payload"], "codex_config_parse")
            )

    def test_mcp_json_present_is_parsed_and_secret_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._write_codex(project, self.VALID_TOML)

            # Clean .mcp.json: parsed, no secrets, stays non-authoritative
            # (present check is info, not fail) -> overall ok.
            (project / ".mcp.json").write_text(
                json.dumps({"mcpServers": {"redmine": {"url": "https://x.invalid"}}}),
                encoding="utf-8",
            )
            r = self._result(project)
            self.assertEqual(0, r["rc"])
            self.assertEqual("info", self._check_status(r["payload"], "mcp_json_present"))
            self.assertEqual("ok", self._check_status(r["payload"], "mcp_json_parse"))
            self.assertEqual(
                "ok", self._check_status(r["payload"], "mcp_json_no_credentials")
            )

            # Malformed .mcp.json fails the parse check.
            (project / ".mcp.json").write_text("{not json", encoding="utf-8")
            r2 = self._result(project)
            self.assertEqual(1, r2["rc"])
            self.assertEqual("fail", self._check_status(r2["payload"], "mcp_json_parse"))

            # Credential-shaped key in .mcp.json fails the secret scan. Use a
            # key name the shared workspace-defaults heuristic flags
            # (`client_secret`) so this stays consistent with the release-tree
            # hygiene gate rather than inventing a second heuristic. Build the
            # value at runtime so the test file carries no literal secret.
            secret_key = "client_secret"
            (project / ".mcp.json").write_text(
                json.dumps({"servers": {"redmine": {secret_key: "x" * 30}}}),
                encoding="utf-8",
            )
            r3 = self._result(project)
            self.assertEqual(1, r3["rc"])
            self.assertEqual(
                "fail", self._check_status(r3["payload"], "mcp_json_no_credentials")
            )

    def test_text_output_is_human_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            rc, out = self.run_cli(
                ["instruction", "doctor", "--target", str(project)]
            )
            self.assertEqual(1, rc)
            # Redmine #11051: stdout uses the canonical command name even when
            # invoked through the deprecated `instruction doctor` alias.
            self.assertIn("runtime-config check: FAIL", out)
            self.assertNotIn("instruction doctor:", out)
            self.assertIn("codex_config_present", out)

    def test_target_defaults_to_mozyo_repo_env(self) -> None:
        # Regression for Codex review #52114 Finding 2: with no --target, the
        # command must honour MOZYO_REPO (matching the --target help text and
        # the rest of the CLI's repo resolution), not just cwd.
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as cwd_tmp:
            project = Path(repo_tmp)
            self._write_codex(project, self.VALID_TOML)
            prev_repo = os.environ.get("MOZYO_REPO")
            prev_cwd = os.getcwd()
            try:
                os.environ["MOZYO_REPO"] = str(project)
                os.chdir(cwd_tmp)  # cwd is a DIFFERENT dir with no .codex config
                rc, out = self.run_cli(
                    ["instruction", "doctor", "--profile", "redmine-codex", "--json"]
                )
                payload = json.loads(out)
            finally:
                os.chdir(prev_cwd)
                if prev_repo is None:
                    os.environ.pop("MOZYO_REPO", None)
                else:
                    os.environ["MOZYO_REPO"] = prev_repo
            self.assertEqual(0, rc)
            self.assertTrue(payload["ok"])
            self.assertEqual(str(project.resolve()), payload["target"])

    def test_toml_parser_falls_back_for_python_310(self) -> None:
        # Regression for Codex review #52114 Finding 1: the package supports
        # Python >=3.10 but `tomllib` is stdlib only on 3.11+. The module must
        # bind a TOML parser (tomllib on 3.11+, tomli on 3.10) rather than
        # importing tomllib unconditionally.
        from mozyo_bridge.application import instruction_doctor as mod

        self.assertIn(mod._toml.__name__, ("tomllib", "tomli"))
        self.assertTrue(hasattr(mod._toml, "loads"))
        self.assertIs(mod._TOMLDecodeError, mod._toml.TOMLDecodeError)


class BootstrapEntrypointDocsTest(unittest.TestCase):
    """Redmine #10857: README is the install/bootstrap entrypoint.

    Pins the refactor's intent so the docs cannot silently regress to
    routing first-time readers straight into the deep bootstrap doc:
    - README routes through `doctor` + `runtime-config check` first
      (renamed from `instruction doctor` in Redmine #11051);
    - README no longer calls bootstrap.md the canonical / read-first
      entrypoint;
    - bootstrap.md no longer self-describes as the canonical entrypoint
      to read before README;
    - the runtime-config-check FAQ lives in bootstrap.md;
    - the CLI taxonomy migration (old `instruction` names) is documented.
    """

    def setUp(self) -> None:
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.bootstrap = (
            ROOT / "vibes" / "docs" / "logics" / "bootstrap.md"
        ).read_text(encoding="utf-8")

    def test_readme_quick_start_routes_through_both_doctors(self) -> None:
        quick_start = self.readme.split("## Quick Start", 1)[1].split("\n## ", 1)[0]
        self.assertIn("mozyo-bridge doctor --target .", quick_start)
        self.assertIn(
            "mozyo-bridge runtime-config check --target . --profile redmine-codex",
            quick_start,
        )
        # The new read-only recovery runbook is advertised in the Quick Start.
        self.assertIn("mozyo-bridge doctor instruction --target .", quick_start)
        # The README states it is the entrypoint.
        self.assertIn("entrypoint for install and bootstrap", quick_start)

    def test_readme_documents_taxonomy_migration(self) -> None:
        # Redmine #11051: README must teach the rename + deprecation so users
        # are not stranded on the old names.
        quick_start = self.readme.split("## Quick Start", 1)[1].split("\n## ", 1)[0]
        self.assertIn("runtime-config check", quick_start)
        self.assertIn("runtime-config install", quick_start)
        self.assertIn("deprecated", quick_start.lower())

    def test_readme_does_not_call_bootstrap_the_canonical_entrypoint(self) -> None:
        self.assertNotIn("canonical LLM-first bootstrap guide", self.readme)
        self.assertNotIn("Read this first for end-to-end setup", self.readme)

    def test_readme_links_runtime_config_check_failures_to_faq(self) -> None:
        self.assertIn("runtime-config check` failures", self.readme)
        for symptom in ("`<repo>/.codex/config.toml` is missing", "X-Default-Project"):
            self.assertIn(symptom, self.readme)

    def test_bootstrap_demoted_from_canonical_entrypoint(self) -> None:
        # Must not re-assert "canonical entrypoint ... Read this BEFORE README".
        self.assertNotIn("canonical entrypoint for", self.bootstrap)
        self.assertIn("detailed stage-order / FAQ / troubleshooting reference", self.bootstrap)

    def test_bootstrap_has_runtime_config_check_faq(self) -> None:
        faq = self.bootstrap.split("### `runtime-config check` FAQ", 1)
        self.assertEqual(2, len(faq), msg="runtime-config check FAQ section missing")
        section = faq[1]
        for needle in (
            "config.toml is missing",
            "X-Default-Project` mismatch",
            ".mcp.json` is `info",
            "home config must not",
            "auto-fix vs",
        ):
            self.assertIn(needle, section, msg=f"FAQ missing {needle!r}")

    def test_bootstrap_documents_taxonomy_migration_and_runbook(self) -> None:
        # The migration section names old -> new and points at the runbook.
        self.assertIn("CLI taxonomy migration", self.bootstrap)
        self.assertIn("mozyo-bridge doctor instruction", self.bootstrap)
        self.assertIn("runtime-config check", self.bootstrap)


class SessionNamingTest(unittest.TestCase):
    """Pin Redmine #10796: collision-safe ASCII tmux session name derivation.

    A non-ASCII workspace basename (e.g. `2026PBL_ローカル`) must never collapse
    to a low-information `____`-style name, and two distinct repos that share a
    basename must get distinct session names so the `--target-repo` handoff
    gate keeps a recoverable repo identity. The workspace-defaults Redmine
    identifier is the preferred, stable source.
    """

    from mozyo_bridge.domain.session_naming import (  # noqa: E402 (test-local import)
        SOURCE_REPO_FALLBACK,
        SOURCE_WORKSPACE_DEFAULTS,
    )

    def _write_workspace_defaults(self, repo: Path, *, identifier: str) -> None:
        (repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
        (repo / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
            "schema_version: 1\n"
            "redmine:\n"
            "  default_project:\n"
            f"    identifier: {identifier}\n"
            "    name: Example\n"
            "    url: https://redmine.example.test/projects/example\n"
            "    parent_label: parent\n"
            "  verification:\n"
            "    verified: false\n"
            '    verification_date: ""\n'
            "    verified_by: \"\"\n"
            "outputs:\n"
            "  - kind: redmine_markdown\n"
            "    target: .mozyo-bridge/redmine-defaults.md\n",
            encoding="utf-8",
        )

    def test_workspace_defaults_identifier_is_preferred(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")

            result = derive_session_name(repo)

            self.assertEqual("mozyo-giken-3800-mozyo-bridge", result.name)
            self.assertEqual(self.SOURCE_WORKSPACE_DEFAULTS, result.source)
            self.assertEqual("giken-3800-mozyo-bridge", result.identifier)

    def test_unverified_identifier_is_still_used(self) -> None:
        # Session naming is a display/grouping identity, not an issue-creation
        # decision, so it intentionally does NOT gate on the verification flag.
        # The fixture above is written with `verified: false`.
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="some-project")

            result = derive_session_name(repo)

            self.assertEqual("mozyo-some-project", result.name)
            self.assertEqual(self.SOURCE_WORKSPACE_DEFAULTS, result.source)

    def test_japanese_basename_is_not_collapsed_to_underscores(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "2026PBL_ローカル"
            repo.mkdir()

            result = derive_session_name(repo)

            self.assertEqual(self.SOURCE_REPO_FALLBACK, result.source)
            self.assertIsNone(result.identifier)
            # Must NOT be the `____`-style low-information name.
            self.assertNotIn("_", result.name)
            self.assertNotRegex(result.name, r"-{2,}")
            # The ASCII-recoverable part is preserved, plus a hash suffix.
            self.assertTrue(
                result.name.startswith("mozyo-2026pbl-"),
                msg=f"unexpected fallback name {result.name!r}",
            )

    def test_nfc_and_nfd_path_spellings_derive_the_same_name(self) -> None:
        # Redmine #11625: the same directory is spelled NFD by macOS readdir /
        # shell completion but NFC by documents, Redmine, and agents. Hashing
        # the raw bytes derived two session names for one workspace.
        import unicodedata as _ud

        from mozyo_bridge.domain.session_naming import derive_session_name

        nfd_spelling = "/ws/" + _ud.normalize("NFD", "動画ドライブ")
        nfc_spelling = "/ws/" + _ud.normalize("NFC", "動画ドライブ")
        self.assertNotEqual(nfd_spelling, nfc_spelling)

        nfd_result = derive_session_name(nfd_spelling)
        nfc_result = derive_session_name(nfc_spelling)

        self.assertEqual(nfd_result.name, nfc_result.name)
        self.assertEqual(self.SOURCE_REPO_FALLBACK, nfd_result.source)

    def test_repo_path_hash_is_pinned_to_the_nfd_form(self) -> None:
        # Compatibility pin: NFD is the macOS filesystem form, so session
        # names historically derived from real filesystem paths must keep
        # their value after the #11625 fix. The hash of any spelling must
        # equal the hash of the NFD bytes.
        import hashlib as _hashlib
        import unicodedata as _ud

        from mozyo_bridge.domain.session_naming import (
            REPO_HASH_LENGTH,
            derive_session_name,
        )

        nfc_spelling = Path("/ws/" + _ud.normalize("NFC", "動画ドライブ"))
        resolved_nfd = _ud.normalize("NFD", str(nfc_spelling.resolve()))
        expected_hash = _hashlib.sha256(
            resolved_nfd.encode("utf-8")
        ).hexdigest()[:REPO_HASH_LENGTH]

        result = derive_session_name(nfc_spelling)

        self.assertTrue(
            result.name.endswith(f"-{expected_hash}"),
            msg=f"{result.name!r} does not carry the NFD-form hash {expected_hash!r}",
        )

    def test_all_non_ascii_basename_yields_hash_only_name(self) -> None:
        from mozyo_bridge.domain.session_naming import (
            REPO_HASH_LENGTH,
            derive_session_name,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "動画制作"
            repo.mkdir()

            result = derive_session_name(repo)

            # No ASCII slug to keep, so the name is just `mozyo-<hash>` — still
            # non-empty, ASCII, and never a bare `____`.
            self.assertRegex(result.name, rf"^mozyo-[0-9a-f]{{{REPO_HASH_LENGTH}}}$")
            self.assertEqual(self.SOURCE_REPO_FALLBACK, result.source)

    def test_same_basename_in_different_paths_is_collision_safe(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a" / "2026PBL_ローカル"
            second = Path(tmp) / "b" / "2026PBL_ローカル"
            first.mkdir(parents=True)
            second.mkdir(parents=True)

            name_a = derive_session_name(first).name
            name_b = derive_session_name(second).name

            self.assertNotEqual(name_a, name_b)
            # Both share the recoverable basename slug but differ by hash.
            self.assertTrue(name_a.startswith("mozyo-2026pbl-"))
            self.assertTrue(name_b.startswith("mozyo-2026pbl-"))

    def test_derivation_is_deterministic_for_same_repo(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "2026PBL_ローカル"
            repo.mkdir()

            self.assertEqual(
                derive_session_name(repo).name, derive_session_name(repo).name
            )

    def test_missing_workspace_defaults_falls_back(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "plain_repo"
            repo.mkdir()

            result = derive_session_name(repo)

            self.assertEqual(self.SOURCE_REPO_FALLBACK, result.source)
            self.assertTrue(result.name.startswith("mozyo-plain-repo-"))

    def test_absent_or_malformed_identifier_falls_back_without_raising(self) -> None:
        from mozyo_bridge.domain.session_naming import (
            derive_session_name,
            read_redmine_identifier,
        )

        bodies = {
            "not a mapping": "- just\n- a\n- list\n",
            "no redmine key": "schema_version: 1\nother: value\n",
            "identifier absent": (
                "redmine:\n  default_project:\n    name: X\n"
            ),
            "identifier non-string": (
                "redmine:\n  default_project:\n    identifier: 12345\n"
            ),
            "broken yaml": "redmine: [unterminated\n",
            "empty identifier": (
                "redmine:\n  default_project:\n    identifier: '   '\n"
            ),
        }
        for label, body in bodies.items():
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = Path(tmp) / "repo_dir"
                    (repo / ".mozyo-bridge").mkdir(parents=True)
                    (repo / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
                        body, encoding="utf-8"
                    )
                    self.assertIsNone(read_redmine_identifier(repo.resolve()))
                    result = derive_session_name(repo)
                    self.assertEqual(self.SOURCE_REPO_FALLBACK, result.source)
                    self.assertTrue(result.name.startswith("mozyo-repo-dir-"))

    def test_non_ascii_only_identifier_falls_back(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo_dir"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="動画制作")

            result = derive_session_name(repo)

            # An identifier that slugs to empty cannot anchor identity, so we
            # fall back rather than emit a bare `mozyo-` prefix.
            self.assertEqual(self.SOURCE_REPO_FALLBACK, result.source)
            self.assertTrue(result.name.startswith("mozyo-repo-dir-"))

    def test_derived_name_never_contains_tmux_illegal_chars(self) -> None:
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            # `.` and `:` are tmux window/pane separators; whitespace is unsafe.
            self._write_workspace_defaults(
                repo, identifier="Foo.Bar:Baz Qux_2026"
            )

            name = derive_session_name(repo).name

            for illegal in (".", ":", " "):
                self.assertNotIn(illegal, name)
            self.assertEqual("mozyo-foo-bar-baz-qux-2026", name)

    def test_session_name_cli_parses(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["session", "name", "--repo", "/some/repo"])

        self.assertEqual("session", args.command)
        self.assertEqual("name", args.session_command)
        self.assertEqual("/some/repo", args.repo)
        self.assertFalse(args.as_json)

    def test_session_subcommand_requires_action(self) -> None:
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["session"])

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def test_session_name_cli_prints_bare_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")

            code, out = self._run_cli(["session", "name", "--repo", str(repo)])

            self.assertEqual(0, code)
            self.assertEqual("mozyo-giken-3800-mozyo-bridge", out.strip())

    def test_session_name_cli_json_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")

            code, out = self._run_cli(
                ["session", "name", "--repo", str(repo), "--json"]
            )

            self.assertEqual(0, code)
            payload = json.loads(out)
            self.assertEqual("mozyo-giken-3800-mozyo-bridge", payload["name"])
            self.assertEqual(
                self.SOURCE_WORKSPACE_DEFAULTS, payload["source"]
            )
            self.assertEqual("giken-3800-mozyo-bridge", payload["identifier"])
            self.assertEqual(str(repo.resolve()), payload["repo_root"])

    # ------------------------------------------------------------------
    # Bare `mozyo` / status unification (Redmine #10796 follow-up #52324)
    # ------------------------------------------------------------------

    def _run_bare_mozyo_capture_session(self, repo: Path) -> str:
        """Run bare `mozyo --no-attach` with tmux mocked; return the session.

        Captures the session name handed to `ensure_repo_session_windows` so
        the test asserts the derivation without touching a real tmux server.
        """
        args = argparse.Namespace(
            repo=str(repo),
            session=None,
            cwd=None,
            config_path=None,
            ready_timeout=0,
            force=False,
            no_attach=True,
        )
        captured: dict[str, argparse.Namespace] = {}

        def fake_ensure(inner: argparse.Namespace) -> list[str]:
            captured["args"] = inner
            return []

        list_result = argparse.Namespace(returncode=0, stdout="", stderr="")
        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
            patch(
                "mozyo_bridge.application.commands.ensure_repo_session_windows",
                side_effect=fake_ensure,
            ), \
            patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
            patch(
                "mozyo_bridge.application.commands.os.execvp",
                side_effect=AssertionError("must not attach"),
            ), \
            contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd_mozyo(args))
        return captured["args"].session

    def test_bare_mozyo_uses_workspace_defaults_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "any-basename").resolve()
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")

            self.assertEqual(
                "mozyo-giken-3800-mozyo-bridge",
                self._run_bare_mozyo_capture_session(repo),
            )

    def test_bare_mozyo_japanese_basename_is_not_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "2026PBL_ローカル").resolve()
            repo.mkdir()

            session = self._run_bare_mozyo_capture_session(repo)

            self.assertNotIn("_", session)
            self.assertTrue(session.startswith("mozyo-2026pbl-"))

    def test_bare_mozyo_respects_explicit_session_override(self) -> None:
        # The explicit `--session` override must still win over the derived
        # name; the derivation only fills the default.
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "2026PBL_ローカル").resolve()
            repo.mkdir()
            args = argparse.Namespace(
                repo=str(repo),
                session="explicit-name",
                cwd=None,
                config_path=None,
                ready_timeout=0,
                force=False,
                no_attach=True,
            )
            captured: dict[str, argparse.Namespace] = {}

            def fake_ensure(inner: argparse.Namespace) -> list[str]:
                captured["args"] = inner
                return []

            list_result = argparse.Namespace(returncode=0, stdout="", stderr="")
            with patch("mozyo_bridge.application.commands.require_tmux"), \
                patch("mozyo_bridge.application.commands.session_exists", return_value=False), \
                patch(
                    "mozyo_bridge.application.commands.ensure_repo_session_windows",
                    side_effect=fake_ensure,
                ), \
                patch("mozyo_bridge.application.commands.run_tmux", return_value=list_result), \
                patch(
                    "mozyo_bridge.application.commands.os.execvp",
                    side_effect=AssertionError("must not attach"),
                ), \
                contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cmd_mozyo(args))

            self.assertEqual("explicit-name", captured["args"].session)

    def test_resolve_status_session_fallback_uses_derived_name(self) -> None:
        from mozyo_bridge.application.commands import resolve_status_session
        from mozyo_bridge.domain.session_naming import derive_session_name

        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "2026PBL_ローカル").resolve()
            repo.mkdir()
            args = argparse.Namespace(repo=str(repo), session=None)

            # Not inside tmux (no current session) and no explicit --session:
            # the fallback must match what bare `mozyo` creates.
            with patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value=None,
            ):
                resolved = resolve_status_session(args)

            self.assertEqual(derive_session_name(repo).name, resolved)

    def test_legacy_basename_session_notice_cases(self) -> None:
        from mozyo_bridge.application.commands import legacy_basename_session_notice

        repo = Path("/tmp/some/2026PBL_ローカル")
        derived = "mozyo-2026pbl-deadbeef"

        # Legacy session exists and belongs to this repo -> notice.
        with patch(
            "mozyo_bridge.application.commands.session_exists", return_value=True
        ), patch(
            "mozyo_bridge.application.commands.session_cwd_mismatch", return_value=[]
        ):
            notice = legacy_basename_session_notice(repo, derived)
        self.assertIsNotNone(notice)
        self.assertIn("2026PBL_ローカル", notice)
        self.assertIn(derived, notice)

        # No legacy session -> no notice.
        with patch(
            "mozyo_bridge.application.commands.session_exists", return_value=False
        ):
            self.assertIsNone(legacy_basename_session_notice(repo, derived))

        # Legacy-named session belongs to another repo (cwd mismatch) -> no notice.
        with patch(
            "mozyo_bridge.application.commands.session_exists", return_value=True
        ), patch(
            "mozyo_bridge.application.commands.session_cwd_mismatch",
            return_value=["/elsewhere"],
        ):
            self.assertIsNone(legacy_basename_session_notice(repo, derived))

        # Derived name equals the basename (ASCII repo) -> nothing to migrate.
        with patch(
            "mozyo_bridge.application.commands.session_exists", return_value=True
        ):
            self.assertIsNone(
                legacy_basename_session_notice(Path("/tmp/foo"), "foo")
            )

    # ------------------------------------------------------------------
    # VS Code `tmux-integrated.sessionName` writer (#52324 mechanization)
    # ------------------------------------------------------------------

    def test_merge_vscode_session_name_creates_and_preserves(self) -> None:
        from mozyo_bridge.domain.session_naming import merge_vscode_session_name

        # Empty / None -> fresh object with just the key.
        for empty in (None, "", "   \n"):
            created = json.loads(merge_vscode_session_name(empty, "mozyo-x"))
            self.assertEqual({"tmux-integrated.sessionName": "mozyo-x"}, created)

        # Existing keys are preserved; the session key is updated in place.
        existing = json.dumps(
            {"editor.tabSize": 2, "tmux-integrated.sessionName": "old"}
        )
        merged = json.loads(merge_vscode_session_name(existing, "mozyo-new"))
        self.assertEqual(2, merged["editor.tabSize"])
        self.assertEqual("mozyo-new", merged["tmux-integrated.sessionName"])

    def test_merge_vscode_session_name_refuses_jsonc_and_non_object(self) -> None:
        from mozyo_bridge.domain.session_naming import merge_vscode_session_name

        with self.assertRaises(ValueError):
            merge_vscode_session_name('{\n  // comment\n  "a": 1\n}', "mozyo-x")
        with self.assertRaises(ValueError):
            merge_vscode_session_name("[1, 2, 3]", "mozyo-x")

    def test_vscode_settings_cli_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")

            code, out = self._run_cli(
                ["session", "vscode-settings", "--repo", str(repo)]
            )

            self.assertEqual(0, code)
            self.assertIn("mozyo-giken-3800-mozyo-bridge", out)
            self.assertFalse((repo / ".vscode" / "settings.json").exists())

    def test_vscode_settings_cli_write_creates_and_merges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._write_workspace_defaults(repo, identifier="giken-3800-mozyo-bridge")
            settings = repo / ".vscode" / "settings.json"

            code, _ = self._run_cli(
                ["session", "vscode-settings", "--repo", str(repo), "--write"]
            )
            self.assertEqual(0, code)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(
                "mozyo-giken-3800-mozyo-bridge",
                data["tmux-integrated.sessionName"],
            )

            # A second write preserving an unrelated key.
            settings.write_text(
                json.dumps(
                    {
                        "editor.tabSize": 4,
                        "tmux-integrated.sessionName": "stale",
                    }
                ),
                encoding="utf-8",
            )
            code, _ = self._run_cli(
                ["session", "vscode-settings", "--repo", str(repo), "--write"]
            )
            self.assertEqual(0, code)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(4, data["editor.tabSize"])
            self.assertEqual(
                "mozyo-giken-3800-mozyo-bridge",
                data["tmux-integrated.sessionName"],
            )

    def test_vscode_settings_cli_refuses_to_clobber_jsonc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            settings = repo / ".vscode"
            settings.mkdir()
            jsonc = settings / "settings.json"
            original = '{\n  // a comment\n  "editor.tabSize": 2\n}\n'
            jsonc.write_text(original, encoding="utf-8")

            parser = build_parser()
            args = parser.parse_args(
                ["session", "vscode-settings", "--repo", str(repo), "--write"]
            )
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    args.func(args)

            # The JSONC file must be left byte-for-byte untouched.
            self.assertEqual(original, jsonc.read_text(encoding="utf-8"))
            self.assertIn("JSONC", stderr.getvalue())

    def test_session_vscode_settings_cli_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["session", "vscode-settings", "--repo", "/r", "--write"]
        )
        self.assertEqual("vscode-settings", args.session_command)
        self.assertTrue(args.write)


class InstructionInstallTest(unittest.TestCase):
    """Pin Redmine #10930: project workspace-defaults into Codex runtime config.

    The single source of truth stays `<repo>/.mozyo-bridge/workspace-defaults.yaml`;
    `instruction install` renders/merges the verified Redmine default project into
    `<repo>/.codex/config.toml` so `instruction doctor` turns green, without ever
    touching home config or generating credentials.
    """

    def _stage(self, repo: Path, *, verified: bool = True, identifier: str = "giken-3800-mozyo-bridge") -> None:
        (repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
        verification_date = "2026-05-28" if verified else ""
        verified_by = "hollySizzle" if verified else '""'
        (repo / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
            "schema_version: 1\n"
            "redmine:\n"
            "  default_project:\n"
            f"    identifier: {identifier}\n"
            "    name: mozyo_bridge\n"
            f"    url: https://redmine.giken.or.jp/projects/{identifier}\n"
            "    parent_label: parent\n"
            "  verification:\n"
            f"    verified: {str(verified).lower()}\n"
            f'    verification_date: "{verification_date}"\n'
            f"    verified_by: {verified_by}\n"
            "outputs:\n"
            "  - kind: redmine_markdown\n"
            "    target: .mozyo-bridge/redmine-defaults.md\n",
            encoding="utf-8",
        )

    def _run(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = args.func(args)
        return code, stdout.getvalue()

    def _doctor_green(self, repo: Path) -> bool:
        from mozyo_bridge.application.instruction_doctor import run_instruction_doctor

        return bool(
            run_instruction_doctor(
                argparse.Namespace(target=str(repo), profile="redmine-codex")
            )["ok"]
        )

    def test_missing_config_dry_run_then_write_makes_doctor_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            config = repo / ".codex" / "config.toml"

            # Dry-run must not write.
            code, out = self._run(
                ["instruction", "install", "--target", str(repo)]
            )
            self.assertEqual(0, code)
            self.assertIn("would write", out)
            self.assertFalse(config.exists())
            self.assertFalse(self._doctor_green(repo))

            # Write makes the doctor green.
            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(0, code)
            self.assertTrue(config.exists())
            # Redmine #11051: post-write message uses the canonical command name.
            self.assertIn("runtime-config check is green", out)
            self.assertTrue(self._doctor_green(repo))

    def test_generated_config_has_consistent_default_project(self) -> None:
        from mozyo_bridge.application.instruction_install import _toml

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo, identifier="giken-3800-mozyo-bridge")
            self._run(["instruction", "install", "--target", str(repo), "--write"])

            parsed = _toml.loads(
                (repo / ".codex" / "config.toml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "giken-3800-mozyo-bridge", parsed["redmine"]["default_project"]
            )
            self.assertEqual(
                "https://redmine.giken.or.jp/mcp/rpc",
                parsed["mcp_servers"]["redmine_epic_grid"]["url"],
            )
            self.assertEqual(
                "giken-3800-mozyo-bridge",
                parsed["mcp_servers"]["redmine_epic_grid"]["http_headers"][
                    "X-Default-Project"
                ],
            )

    def test_existing_unrelated_table_is_preserved_on_append(self) -> None:
        from mozyo_bridge.application.instruction_install import _toml

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            config = repo / ".codex"
            config.mkdir()
            (config / "config.toml").write_text(
                "[history]\nmax_size = 1000\n", encoding="utf-8"
            )

            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(0, code)
            parsed = _toml.loads((config / "config.toml").read_text(encoding="utf-8"))
            # Unrelated table preserved AND managed block added.
            self.assertEqual(1000, parsed["history"]["max_size"])
            self.assertEqual(
                "giken-3800-mozyo-bridge", parsed["redmine"]["default_project"]
            )
            self.assertTrue(self._doctor_green(repo))

    def test_conflict_fails_and_does_not_write_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            config = repo / ".codex"
            config.mkdir()
            original = '[redmine]\ndefault_project = "other-proj"\n[history]\nx = 1\n'
            (config / "config.toml").write_text(original, encoding="utf-8")

            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(1, code)
            self.assertIn("--force", out)
            # File must be left untouched.
            self.assertEqual(
                original, (config / "config.toml").read_text(encoding="utf-8")
            )

    def test_force_regenerates_managed_tables_and_preserves_others(self) -> None:
        from mozyo_bridge.application.instruction_install import _toml

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            config = repo / ".codex"
            config.mkdir()
            (config / "config.toml").write_text(
                '[redmine]\ndefault_project = "other-proj"\n\n[history]\nx = 1\n',
                encoding="utf-8",
            )

            code, _ = self._run(
                ["instruction", "install", "--target", str(repo), "--write", "--force"]
            )
            self.assertEqual(0, code)
            parsed = _toml.loads((config / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(
                "giken-3800-mozyo-bridge", parsed["redmine"]["default_project"]
            )
            self.assertEqual(1, parsed["history"]["x"])  # unrelated table preserved
            self.assertTrue(self._doctor_green(repo))

    def test_already_up_to_date_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            self._run(["instruction", "install", "--target", str(repo), "--write"])
            before = (repo / ".codex" / "config.toml").read_text(encoding="utf-8")

            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(0, code)
            self.assertIn("already matches", out)
            self.assertEqual(
                before, (repo / ".codex" / "config.toml").read_text(encoding="utf-8")
            )

    def test_unverified_default_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo, verified=False)

            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(1, code)
            self.assertIn("verification is incomplete", out)
            self.assertFalse((repo / ".codex" / "config.toml").exists())

    def test_credential_shape_in_workspace_defaults_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".mozyo-bridge").mkdir(parents=True)
            # A credential-shape value must make load fail (die -> SystemExit),
            # so no runtime config is ever generated from it.
            secret_key = "api" + "_key"
            (repo / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
                "schema_version: 1\n"
                "redmine:\n"
                "  default_project:\n"
                "    identifier: giken-3800-mozyo-bridge\n"
                "    name: mozyo_bridge\n"
                "    url: https://redmine.giken.or.jp/projects/giken-3800-mozyo-bridge\n"
                "    parent_label: parent\n"
                f"    {secret_key}: AKIAEXAMPLEEXAMPLE12\n"
                "  verification:\n"
                "    verified: true\n"
                '    verification_date: "2026-05-28"\n'
                "    verified_by: hollySizzle\n"
                "outputs:\n"
                "  - kind: redmine_markdown\n"
                "    target: .mozyo-bridge/redmine-defaults.md\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self._run(["instruction", "install", "--target", str(repo), "--write"])
            self.assertFalse((repo / ".codex" / "config.toml").exists())

    def test_invalid_existing_toml_is_not_clobbered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            config = repo / ".codex"
            config.mkdir()
            original = "this is = not valid = toml ]["
            (config / "config.toml").write_text(original, encoding="utf-8")

            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(1, code)
            self.assertIn("not valid TOML", out)
            self.assertEqual(
                original, (config / "config.toml").read_text(encoding="utf-8")
            )

    def test_instruction_install_cli_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["instruction", "install", "--target", "/r", "--write", "--force"]
        )
        self.assertEqual("instruction", args.command)
        self.assertEqual("install", args.instruction_command)
        self.assertEqual("/r", args.target)
        self.assertTrue(args.write)
        self.assertTrue(args.force)


if __name__ == "__main__":
    unittest.main()
