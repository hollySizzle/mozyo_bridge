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

ROOT = Path(__file__).resolve().parents[3]
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
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure import tmux_client
from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.notification import build_prompt, landing_marker, validate_notify_gate
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (
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
import mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver as pane_resolver
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
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
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.queue_reader import find_handoff_task, load_queue
from mozyo_bridge.scaffold.rules import package_version, rules_status, scaffold_state
from mozyo_bridge.shared.paths import default_queue_path, default_tmux_conf, find_repo_root, resolve_repo_root


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

    def test_find_repo_root_uses_config_only_adoption_marker(self) -> None:
        # A non-Git project adopted by hand-writing `.mozyo-bridge/config.yaml`
        # alone (no scaffold manifest / anchor) is a first-class root: the walk
        # from a child cwd must stop at the adopted root, not fall through to
        # the child / an incidental ancestor (Redmine #13379 review j#73711).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            (root / ".mozyo-bridge").mkdir()
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 1\n", encoding="utf-8"
            )

            resolved = find_repo_root(nested)
            self.assertEqual(root.resolve(), resolved)

            from mozyo_bridge.shared.paths import workspace_adoption_marker

            self.assertEqual(
                ".mozyo-bridge/config.yaml", workspace_adoption_marker(resolved)
            )

    def test_config_only_root_selects_herdr_backend_from_child_cwd(self) -> None:
        # The same walk feeds the repo-local config load at the entrypoint: a
        # config-only adopted root must have its `terminal_transport.backend:
        # herdr` selection read from a child cwd (Redmine #13379 j#73711).
        from mozyo_bridge.application.repo_local_config_loader import (
            load_repo_local_config,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            (root / ".mozyo-bridge").mkdir()
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 1\nterminal_transport:\n  backend: herdr\n",
                encoding="utf-8",
            )

            config = load_repo_local_config(None, start=nested)
            self.assertTrue(config.terminal_transport.herdr_enabled)

    def test_find_repo_root_prefers_git_root_over_nested_scaffold(self) -> None:
        # Git-root-first (Redmine #13641): a monorepo project subtree carrying its
        # own `.mozyo-bridge/scaffold.json` must NOT collapse the workspace onto
        # the subtree. When a Git worktree root is reachable above, it wins.
        with tempfile.TemporaryDirectory() as tmp:
            git_root = Path(tmp) / "gk-3500-it-operations"
            proj = git_root / "projects" / "giken-cloud-drive-management"
            nested = proj / "src"
            nested.mkdir(parents=True)
            (git_root / ".git").mkdir()  # Git worktree root
            (proj / ".mozyo-bridge").mkdir()
            (proj / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )  # nested project-local scaffold marker

            self.assertEqual(git_root.resolve(), find_repo_root(nested))

    def test_git_root_config_shadows_nested_scaffold_backend_from_subtree(
        self,
    ) -> None:
        # The same walk feeds the bare-`mozyo` config/backend selection: with a
        # Git root `.mozyo-bridge/config.yaml: terminal_transport.backend: herdr`
        # above a nested `scaffold.json`-only subtree, the config load from the
        # subtree cwd must read the Git root's herdr selection, not fall through
        # to the (absent) subtree config and its tmux default (Redmine #13641).
        from mozyo_bridge.application.repo_local_config_loader import (
            load_repo_local_config,
        )

        with tempfile.TemporaryDirectory() as tmp:
            git_root = Path(tmp) / "gk-3500-it-operations"
            proj = git_root / "projects" / "giken-cloud-drive-management"
            nested = proj / "src"
            nested.mkdir(parents=True)
            (git_root / ".git").mkdir()
            (git_root / ".mozyo-bridge").mkdir()
            (git_root / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 1\nterminal_transport:\n  backend: herdr\n",
                encoding="utf-8",
            )
            (proj / ".mozyo-bridge").mkdir()
            (proj / ".mozyo-bridge" / "scaffold.json").write_text(
                "{}", encoding="utf-8"
            )

            config = load_repo_local_config(None, start=nested)
            self.assertTrue(config.terminal_transport.herdr_enabled)

    def test_find_repo_root_non_git_nested_scaffold_fallback_preserved(self) -> None:
        # Behavior-preserving fallback (Redmine #13641 acceptance): with NO Git
        # root anywhere above, a nested `scaffold.json` still resolves to its own
        # scaffold root via the marker walk — the non-git contract is unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "scaffolded-workspace"
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

    def test_workspace_adoption_marker_names_the_explicit_adoption_file(self) -> None:
        # Adoption (Redmine #13379) is a property of the resolved root itself:
        # only files written by an explicit adoption action count, and a bare
        # `.mozyo-bridge/` directory (tooling side effect) is NOT adoption.
        from mozyo_bridge.shared.paths import workspace_adoption_marker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(workspace_adoption_marker(root))
            (root / ".mozyo-bridge").mkdir()
            self.assertIsNone(workspace_adoption_marker(root))
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "", encoding="utf-8"
            )
            self.assertEqual(
                ".mozyo-bridge/config.yaml", workspace_adoption_marker(root)
            )

    def test_workspace_adoption_marker_accepts_workspace_anchors(self) -> None:
        from mozyo_bridge.shared.paths import workspace_adoption_marker

        for anchor in ("scaffold.json", "workspace-anchor.json", "workspace.json"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / ".mozyo-bridge").mkdir()
                (root / ".mozyo-bridge" / anchor).write_text("{}", encoding="utf-8")
                self.assertEqual(
                    f".mozyo-bridge/{anchor}", workspace_adoption_marker(root)
                )

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

        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes):
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

        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes):
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

        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes):
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

        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes):
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

        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes):
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

        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes):
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

        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes):
            self.assertIsNone(resolve_agent_label("claude", "repo"))

    def test_resolve_agent_label_returns_none_when_session_unknown(self) -> None:
        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=[]):
            self.assertIsNone(resolve_agent_label("claude", None))

    def test_resolve_target_for_agent_label_dies_outside_tmux(self) -> None:
        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.current_session_name", return_value=None), \
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

        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.current_session_name", return_value="repo"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes), \
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

        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.current_session_name", return_value="repo"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes):
            self.assertEqual("%9", resolve_target("codex"))

    def test_resolve_target_normalizes_location_form_to_pane_id(self) -> None:
        # Redmine #11666: a `session:window` location used to be returned
        # verbatim, so pane_info()'s pane-id match never succeeded and every
        # location target died with `pane disappeared after resolve`.
        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.resolve_pane_id",
                return_value="%9",
            ) as resolver:
            self.assertEqual("%9", resolve_target("repo:codex"))
        resolver.assert_called_once_with("repo:codex")

    def test_resolve_target_passes_pane_id_through_unchanged(self) -> None:
        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.resolve_pane_id"
            ) as resolver:
            self.assertEqual("%9", resolve_target("%9"))
        resolver.assert_not_called()

    def test_pane_info_finds_pane_for_location_target(self) -> None:
        # End-to-end through pane_info: the normalized id must match the
        # pane_lines() entry, where the raw location string never did.
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import pane_info

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
        with patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.resolve_pane_id",
                return_value="%9",
            ), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines",
                return_value=panes,
            ):
            self.assertEqual("%9", pane_info("repo:codex")["id"])

    def test_resolve_pane_id_resolves_location_and_rejects_invalid(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import resolve_pane_id

        with patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.run_tmux",
            return_value=argparse.Namespace(returncode=0, stdout="%42\n", stderr=""),
        ):
            self.assertEqual("%42", resolve_pane_id("repo:codex"))
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.run_tmux",
            return_value=argparse.Namespace(returncode=1, stdout="", stderr="no such window"),
        ), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                resolve_pane_id("repo:nope")

    def test_resolve_target_rejects_non_agent_string(self) -> None:
        # Custom strings used to fall through to the `@agent_name` label
        # lookup; under the window-only model they fail closed at resolve
        # time with a hint to pass a tmux pane id or an agent label.
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.current_session_name",
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
        # Section header and policy headline. Prose markers pin the
        # Japanese skill body (Redmine #13050 translation).
        self.assertIn("## Audit-Owned Commit Authority", self.workflow)
        # Cross-system boundary statement.
        self.assertIn("これは commit 権限であって、実装権限ではない", self.workflow)
        self.assertIn("Codex による直接実装編集", self.workflow)
        self.assertIn("Codex による audit-owned commit", self.workflow)

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
        self.assertIn("commit hash を durable な正本に記録する", self.workflow)

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
        section_start = self.workflow.index("## Handoff ライフサイクル")
        section_end = self.workflow.index(
            "## Claude / Codex 役割境界", section_start
        )
        section = self.workflow[section_start:section_end]
        self.assertIn("`mozyo-bridge handoff send`", section)
        self.assertIn("`mozyo-bridge handoff reply`", section)
        self.assertIn("`mozyo-bridge reply`", section)
        self.assertIn("互換 entrypoint", section)
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
            "## Workflow 変更の反映確認", section_start
        )
        section = self.workflow[section_start:section_end]
        self.assertIn("owner close approval", section)
        self.assertIn("review approval 単独は close approval ではない", section)
        self.assertIn("Close Approval Separation", section)

    def test_audit_owned_commit_does_not_grant_direct_implementation(self) -> None:
        # The audit-owned commit section must NOT contain wording that could be
        # read as permission for Codex to write implementation diffs as part of
        # the commit step. We isolate the new section to keep this test from
        # tripping on the legitimate Codex direct-edit *exception* phrasing in
        # the Policy / Skill Authoring Boundary section.
        section_start = self.workflow.index("## Audit-Owned Commit Authority")
        section_end = self.workflow.index("## Workflow 変更の反映確認", section_start)
        section = self.workflow[section_start:section_end]
        self.assertNotIn("Codex may edit", section)
        self.assertNotIn("Codex may implement", section)
        self.assertNotIn("Codex implements normal", section)
        # The section must explicitly preserve the prohibition on Codex
        # producing new diffs while granting the commit-only authority.
        self.assertIn(
            "audit 承認済み diff を「手直し」するために実装 file を編集してはならない",
            section,
        )
        self.assertIn("これは commit 権限であって、実装権限ではない", section)
        # The section must NOT silently waive the implementer / auditor
        # boundary defined elsewhere.
        self.assertIn("実装者 / 監査者の境界を免除しない", section)

    def test_main_unit_claude_safe_use_section_present(self) -> None:
        """Redmine #11858: the shared skill reference must carry the main-unit
        Claude safe-use boundary so a main coordinator unit that places a
        Claude pane beside the coordinator Codex knows what it may offload to
        save Codex context and what stays owner-facing. The boundary is the
        portable workflow risk, not an operator's private offload list."""
        section_start = self.workflow.index("## Main-unit Claude の安全使用境界")
        section_end = self.workflow.index(
            "\n## Claude / Codex 役割境界", section_start
        )
        section = self.workflow[section_start:section_end]
        # Anchored to the durable record and framed as observed risk, not a
        # fixed judgement about any model.
        self.assertIn("#11858", section)
        self.assertIn(
            "観測された workflow 上の risk から引かれたものであり、"
            "特定 model の能力についての固定的な判断ではない",
            section,
        )
        # Output is input/draft, never evidence the coordinator can act on
        # without confirming against the source of truth.
        self.assertIn("draft / input であって決して evidence ではない", section)
        # The two explicit buckets the acceptance criteria require.
        self.assertIn("### 許可される用途 (安全な Codex context 節約)", section)
        self.assertIn("### 禁止される用途 (coordinator Codex に残すもの)", section)
        # Concrete Codex-context-saving safe tasks.
        self.assertIn(
            "長い Redmine journal、diff、log、command transcript を、"
            "coordinator がその後検証する短い brief に要約する",
            section,
        )
        self.assertIn("candidate の抽出", section)
        # Owner-facing / gate actions that must NOT be delegated.
        self.assertIn("owner close approval", section)
        self.assertIn("Review Gate", section)
        self.assertIn("durable な routing 判断", section)
        # The difference from a sublane Claude must be explicit.
        self.assertIn("### sublane Claude との違い", section)
        # Portable vs private operator preference separation.
        self.assertIn("public-private-boundary.md", section)

    def test_main_unit_claude_safe_use_does_not_grant_owner_or_gate_authority(
        self,
    ) -> None:
        """The main-unit Claude section saves coordinator context but must not
        read as moving any owner-facing / gate boundary onto the Claude pane.
        A future edit that softened the prohibition into an allowance would be
        caught here."""
        section_start = self.workflow.index("## Main-unit Claude の安全使用境界")
        section_end = self.workflow.index(
            "\n## Claude / Codex 役割境界", section_start
        )
        section = self.workflow[section_start:section_end]
        # The assistant framing and the non-relaxation clause must both stand.
        self.assertIn("assistant であり並列 coordinator ではない", section)
        self.assertIn(
            "owner 窓口と gate 判断は coordinator Codex に残る",
            section,
        )
        # It must defer owner approval to the single aggregation point, not the
        # Claude pane.
        self.assertIn("決して Claude pane にではない", section)

    def test_issue_subject_description_separation_section_present(self) -> None:
        """Redmine #11856: the shared skill reference must carry the
        creation-time subject / description separation convention so agents
        pass an explicit concise subject instead of letting a long Markdown
        body produce a subject like `## 背景` (the #11850 j#57294 observation).
        It must also carry the immediate-correction rule for a malformed
        subject and stay anchored to the durable record."""
        section_start = self.workflow.index(
            "### Issue の subject / description 分離"
        )
        section_end = self.workflow.index("\n## Local docs", section_start)
        section = self.workflow[section_start:section_end]
        # Anchored to the durable record and the concrete observed failure.
        self.assertIn("#11856", section)
        self.assertIn("## 背景", section)
        # The two acceptance-criteria halves: explicit subject on create, and
        # an immediate-correction rule for a bad subject.
        self.assertIn("explicit-subject-on-create", section)
        self.assertIn("即時修正規則", section)
        # Concrete creation-time discipline: always pass an explicit subject and
        # never let the body derive it.
        self.assertIn("常に明示の `subject` を渡す", section)
        self.assertIn("description 本文から subject を生成させない", section)
        # The correction names the actual repair tool and lands on the durable
        # record.
        self.assertIn("update_issue_subject_tool", section)
        # Must not claim to change gate vocabulary / hierarchy / required fields.
        self.assertIn("gate 語彙、階層 semantics、必須 field は一切変えない", section)
        # Portable rule vs operator's private subject style.
        self.assertIn("public-private-boundary.md", section)


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

    @staticmethod
    def _adopt(repo: Path) -> None:
        """Mark a temp repo as mozyo-adopted (scaffold manifest, #13379).

        Bare `mozyo` fails closed on an unadopted root, so the launch-flow
        tests below adopt their temp repos first.
        """
        (repo / ".mozyo-bridge").mkdir(exist_ok=True)
        (repo / ".mozyo-bridge" / "scaffold.json").write_text("{}", encoding="utf-8")

    def test_cmd_mozyo_attaches_after_ensuring_repo_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            self._adopt(repo)
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
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import derive_session_name

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
            self._adopt(repo)
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

        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import derive_session_name

        expected = derive_session_name(repo).name
        self.assertIn(f"attach: tmux attach -t {expected}", stdout.getvalue())

    def test_cmd_mozyo_json_emits_ready_payload_for_created_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = (Path(tmp) / "my-project").resolve()
            repo.mkdir()
            self._adopt(repo)
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

        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import derive_session_name

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
        self._adopt(repo)
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
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import derive_session_name

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
            self._adopt(repo)
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
            self._adopt(repo)
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
            self._adopt(repo)
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
            self._adopt(repo)
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

        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import derive_session_name

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
            self._adopt(repo)
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
            self._adopt(repo)
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
            from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import derive_session_name

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
            self._adopt(repo)
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
            self._adopt(repo)
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
            patch("mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.run_tmux") as run:
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
            patch("mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.run_tmux") as run, \
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
            with patch("mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client.run_tmux", return_value=ok) as run:
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
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import derive_session_name

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
        with patch("mozyo_bridge.application.commands_agents.require_tmux"), \
            patch("mozyo_bridge.application.commands_agents.pane_lines", return_value=panes), \
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
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import derive_session_name

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
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.current_session_name", return_value="mozyo_bridge"), \
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
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.current_session_name", return_value="mozyo_bridge"), \
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
        # record for Redmine projects. Prose markers pin the Japanese
        # skill body (Redmine #13050 translation).
        for marker in (
            "Redmine の `codex_direct_edit` gate journal",
            "allowed_paths",
            "role: 実装者",
            "follow_up_review",
            # Generator-only artifact rule must live in the skill reference too.
            ".mozyo-bridge/docs/file_conventions.generated.yaml",
            # The hardening must be tied to a concrete failure mode
            # without publishing internal ticket identifiers.
            "過去の incident pattern",
            "Review Gate で承認された audit-owned commit 経路",
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
        self.assertIn("Redmine の `codex_direct_edit` gate journal", body)
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

    Coverage for ``mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery``: classification by
    window-name agent rail, per-session ambiguity detection, repo-root
    inference via REPO_ROOT_MARKERS, and the ``mozyo-bridge agents list``
    CLI surface (text + JSON).
    """

    def test_discover_agents_classifies_by_window_name(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import discover_agents

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
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import discover_agents

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
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import discover_agents

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
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import infer_repo_root

        with tempfile.TemporaryDirectory() as tmp_str:
            repo = Path(tmp_str) / "repo"
            nested = repo / "src" / "deep" / "leaf"
            nested.mkdir(parents=True)
            (repo / "pyproject.toml").write_text("", encoding="utf-8")
            self.assertEqual(str(repo.resolve()), infer_repo_root(str(nested)))

    def test_infer_repo_root_returns_none_when_no_markers_above(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import infer_repo_root

        with tempfile.TemporaryDirectory() as tmp_str:
            no_markers = Path(tmp_str) / "no_markers"
            no_markers.mkdir(parents=True)
            self.assertIsNone(infer_repo_root(str(no_markers)))

    def test_infer_repo_root_uses_scaffolded_workspace_marker(self) -> None:
        # Redmine #11301: a non-git scaffolded workspace must report its own
        # root from a pane cwd under it, instead of leaking up to the home
        # directory (which fail-closes the cross-workspace --target-repo gate).
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import infer_repo_root

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
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import infer_repo_root

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
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
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
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.pane_lines",
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
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.pane_lines",
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
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
            discover_agents,
            fold_agents_by_pane,
        )

        resolved_roots: list[str] = []

        def resolver(root: str) -> str:
            resolved_roots.append(root)
            return "mozyo-giken-1750-labor"

        with patch(
            "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.infer_repo_root",
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
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
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
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
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
        from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import derive_session_name

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
                    "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.pane_lines",
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
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.pane_lines",
                return_value=[],
            ), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                cmd_agents_list(args)
        self.assertIn("--agent must be one of", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
