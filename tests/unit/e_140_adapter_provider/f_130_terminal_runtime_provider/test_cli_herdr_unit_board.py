"""CLI and runtime tests for the Herdr coordinator Unit board (Redmine #15114)."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    ParsedRoleBindings,
    WorkflowRoleBinding,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    format_board,
    unavailable_snapshot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_unit_board import (
    register_herdr_unit_board_parser,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    valid_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_unit_board_runtime import (
    HerdrUnitBoardRuntime,
    METADATA_SYNC_LOCK_FILENAME,
    METADATA_TOKEN_KEYS,
    MetadataSyncLockError,
    unit_board_metadata_lock,
)


WORKSPACE_ID = "a" * 32


class FakeLister:
    def __init__(self, rows) -> None:
        self.rows = rows

    def list_agent_rows(self):
        return self.rows


def workspace_record():
    return SimpleNamespace(
        project_name="giken-3800-mozyo-bridge",
        canonical_path="/reviewable/repo",
    )


def role_bindings() -> ParsedRoleBindings:
    return ParsedRoleBindings.valid(
        (
            WorkflowRoleBinding(
                role="coordinator",
                project_scope="giken-3800-mozyo-bridge",
                lane_id="default",
                source_pointer="redmine:#15114",
            ),
        )
    )


def row(provider: str, pane: str, *, name: str = "") -> dict[str, object]:
    return {
        "name": name or encode_assigned_name(WORKSPACE_ID, provider, "default"),
        "pane_id": pane,
        "status": "idle",
        "interactive_ready": True,
    }


def pane_rows(*pane_ids: str, tokens=None) -> tuple[dict[str, object], ...]:
    token_map = {} if tokens is None else dict(tokens)
    return tuple(
        {"pane_id": pane_id, "tokens": dict(token_map)} for pane_id in pane_ids
    )


def runtime(rows, *, runner=None, parsed=None) -> HerdrUnitBoardRuntime:
    rows = tuple(rows)
    return HerdrUnitBoardRuntime(
        "/bin/herdr",
        lister=FakeLister(rows),
        runner=runner,
        workspace_loader=lambda workspace_id: (
            workspace_record() if workspace_id == WORKSPACE_ID else None
        ),
        role_loader=lambda repo: parsed if parsed is not None else role_bindings(),
        lane_records_loader=lambda: {},
        pane_rows_loader=lambda: pane_rows(
            *dict.fromkeys(
                row.get("pane_id")
                for row in rows
                if isinstance(row, dict) and valid_target(row.get("pane_id"))
            )
        ),
        sync_lock_factory=nullcontext,
    )


class HerdrUnitBoardRuntimeTests(unittest.TestCase):
    def test_snapshot_joins_declared_project_role_and_responsibility(self) -> None:
        snapshot = runtime(
            (row("claude", "w1:p1"), row("codex", "w1:p2"))
        ).snapshot()

        self.assertTrue(snapshot.ok)
        self.assertEqual(len(snapshot.units), 1)
        unit = snapshot.units[0]
        self.assertEqual(unit.project_label, "giken-3800-mozyo-bridge")
        self.assertEqual(unit.workflow_role, "coordinator")
        self.assertEqual(unit.responsibility, "giken-3800-mozyo-bridge")
        self.assertEqual(unit.authority_state, "resolved")
        self.assertNotIn("w1:p1", repr(snapshot.as_payload()))

    def test_missing_binding_stays_unknown_instead_of_guessing_from_provider(self) -> None:
        snapshot = runtime(
            (row("codex", "w1:p2"),), parsed=ParsedRoleBindings.empty()
        ).snapshot()

        unit = snapshot.units[0]
        self.assertEqual(unit.workflow_role, "unknown")
        self.assertEqual(unit.authority_state, "missing")
        self.assertNotEqual(unit.workflow_role, "codex")

    def test_surrogate_authority_is_removed_from_all_public_surfaces(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        parsed = ParsedRoleBindings.valid(
            (
                WorkflowRoleBinding(
                    role="coordinator",
                    project_scope="responsibility-\ud800-public",
                    lane_id="default",
                ),
            )
        )
        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=FakeLister((row("codex", "w1:p2"),)),
            runner=runner,
            workspace_loader=lambda workspace_id: SimpleNamespace(
                project_name="project-\ud800-public",
                canonical_path="/reviewable/repo",
            ),
            role_loader=lambda repo: parsed,
            lane_records_loader=lambda: {},
            pane_rows_loader=lambda: pane_rows("w1:p2"),
        )

        snapshot = board.snapshot()
        public = json.dumps(snapshot.as_payload(), ensure_ascii=False) + format_board(
            snapshot, width=80
        )
        report = board.sync_metadata()

        self.assertTrue(report.ok)
        self.assertNotIn("\ud800", public)
        self.assertNotIn("\ud800", repr(calls))

    def test_duplicate_lane_binding_is_invalid_instead_of_missing(self) -> None:
        binding = WorkflowRoleBinding(
            role="coordinator",
            project_scope="giken-3800-mozyo-bridge",
            lane_id="default",
        )
        snapshot = runtime(
            (row("codex", "w1:p2"),),
            parsed=ParsedRoleBindings.valid((binding, binding)),
        ).snapshot()

        unit = snapshot.units[0]
        self.assertEqual(unit.workflow_role, "unknown")
        self.assertEqual(unit.authority_state, "invalid")

    def test_pair_uses_one_consistent_authority_read_per_unit(self) -> None:
        workspace_calls = 0
        role_calls = 0

        def load_workspace(workspace_id):
            nonlocal workspace_calls
            workspace_calls += 1
            return SimpleNamespace(
                project_name=f"/synthetic/private/{workspace_calls}",
                canonical_path="/reviewable/repo",
            )

        def load_roles(repo):
            nonlocal role_calls
            role_calls += 1
            return ParsedRoleBindings.valid(
                (
                    WorkflowRoleBinding(
                        role="coordinator",
                        project_scope=("x" * 80) + str(role_calls),
                        lane_id="default",
                    ),
                )
            )

        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=FakeLister(
                (row("claude", "w1:p1"), row("codex", "w1:p2"))
            ),
            workspace_loader=load_workspace,
            role_loader=load_roles,
            lane_records_loader=lambda: {},
            pane_rows_loader=lambda: pane_rows("w1:p1", "w1:p2"),
        )

        snapshot = board.snapshot()

        self.assertEqual(workspace_calls, 1)
        self.assertEqual(role_calls, 1)
        self.assertEqual(snapshot.units[0].identity_state, "resolved")

    def test_malformed_managed_identity_requires_reload(self) -> None:
        snapshot = runtime((row("codex", "w1:p2", name="mzb1_bad"),)).snapshot()

        self.assertFalse(snapshot.ok)
        self.assertEqual(snapshot.source_state, "reload_required")
        self.assertEqual(snapshot.units, ())

    def test_sync_uses_only_display_metadata_command(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        report = runtime(
            (row("claude", "w1:p1"), row("codex", "w1:p2")), runner=runner
        ).sync_metadata()

        self.assertTrue(report.ok)
        self.assertEqual(report.updated, 2)
        self.assertEqual(len(calls), 2)
        for argv in calls:
            self.assertEqual(argv[:3], ["/bin/herdr", "pane", "report-metadata"])
            self.assertIn(argv[3], {"w1:p1", "w1:p2"})
            self.assertIn("--source", argv)
            self.assertIn("--title", argv)
            self.assertIn("--display-agent", argv)
            self.assertNotIn("--seq", argv)
            self.assertNotIn("send-text", argv)
            self.assertNotIn("send-keys", argv)
            self.assertNotIn("prompt", argv)
        self.assertEqual({argv[3] for argv in calls}, {"w1:p1", "w1:p2"})

    def test_sync_clears_fixed_metadata_from_unmanaged_live_pane(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        report = runtime(
            (
                row("codex", "w1:p2"),
                {"name": "manual-agent", "pane_id": "w1:p9"},
            ),
            runner=runner,
        ).sync_metadata()

        self.assertTrue(report.ok)
        self.assertEqual(report.attempted, 2)
        clear = next(argv for argv in calls if argv[3] == "w1:p9")
        self.assertIn("--clear-title", clear)
        self.assertIn("--clear-display-agent", clear)
        self.assertNotIn("--seq", clear)
        cleared = {
            clear[index + 1]
            for index, value in enumerate(clear)
            if value == "--clear-token"
        }
        self.assertEqual(cleared, set(METADATA_TOKEN_KEYS))
        self.assertNotIn("--token", clear)

    def test_sync_clears_metadata_after_managed_agent_disappears_from_agent_list(self) -> None:
        tokens = {
            **{
                key: f"previous-{index}"
                for index, key in enumerate(METADATA_TOKEN_KEYS)
            },
            "unrelated": "preserved",
        }
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            if argv[1:3] == ["pane", "list"]:
                payload = {
                    "id": "cli:pane:list",
                    "result": {
                        "type": "pane_list",
                        "panes": [{"pane_id": "w1:p2", "tokens": dict(tokens)}],
                    },
                }
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(payload), stderr=""
                )
            self.assertEqual(
                argv[:4], ["/bin/herdr", "pane", "report-metadata", "w1:p2"]
            )
            for index, value in enumerate(argv):
                if value == "--clear-token":
                    tokens.pop(argv[index + 1], None)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=FakeLister(()),
            runner=runner,
            lane_records_loader=lambda: {},
            sync_lock_factory=nullcontext,
        )

        report = board.sync_metadata()

        self.assertTrue(report.ok)
        self.assertEqual(report.attempted, 1)
        self.assertEqual(report.updated, 1)
        clear_calls = [
            argv for argv in calls if argv[1:3] == ["pane", "report-metadata"]
        ]
        self.assertEqual(len(clear_calls), 1)
        self.assertIn("--clear-title", clear_calls[0])
        cleared = {
            clear_calls[0][index + 1]
            for index, value in enumerate(clear_calls[0])
            if value == "--clear-token"
        }
        self.assertEqual(cleared, set(METADATA_TOKEN_KEYS))
        self.assertTrue(all(key not in tokens for key in METADATA_TOKEN_KEYS))
        self.assertEqual(tokens["unrelated"], "preserved")

    def test_sync_reconciles_a_new_managed_identity_that_starts_before_stale_clear(self) -> None:
        managed = (row("codex", "w1:p2"),)
        agent_snapshots = [(), managed, managed, managed]
        tokens = {key: "stale-value" for key in METADATA_TOKEN_KEYS}
        calls: list[list[str]] = []

        class CurrentLister:
            def list_agent_rows(self):
                return agent_snapshots.pop(0)

        def runner(argv, **kwargs):
            calls.append(list(argv))
            if "--clear-token" in argv:
                self.fail("a new managed identity must be re-observed before stale clear")
            else:
                for index, value in enumerate(argv):
                    if value == "--token":
                        key, token_value = argv[index + 1].split("=", 1)
                        tokens[key] = token_value
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=CurrentLister(),
            runner=runner,
            workspace_loader=lambda workspace_id: workspace_record(),
            role_loader=lambda repo: role_bindings(),
            lane_records_loader=lambda: {},
            pane_rows_loader=lambda: pane_rows("w1:p2", tokens=tokens),
            sync_lock_factory=nullcontext,
        )

        report = board.sync_metadata()

        self.assertTrue(report.ok)
        self.assertEqual(report.attempted, 1)
        self.assertEqual(report.updated, 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--clear-token", calls[0])
        self.assertIn("--token", calls[0])
        self.assertTrue(all(tokens[key] != "stale-value" for key in METADATA_TOKEN_KEYS))

    def test_duplicate_complete_pane_locator_fails_closed_without_metadata_write(self) -> None:
        runner = mock.Mock()
        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=FakeLister(()),
            runner=runner,
            pane_rows_loader=lambda: pane_rows("w1:p2", "w1:p2"),
            sync_lock_factory=nullcontext,
        )

        report = board.sync_metadata()

        self.assertFalse(report.ok)
        self.assertEqual(report.attempted, 0)
        runner.assert_not_called()

    def test_unreadable_complete_pane_inventory_fails_closed_without_metadata_write(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="not-json", stderr="")

        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=FakeLister((row("codex", "w1:p2"),)),
            runner=runner,
            lane_records_loader=lambda: {},
            sync_lock_factory=nullcontext,
        )

        report = board.sync_metadata()

        self.assertFalse(report.ok)
        self.assertEqual(report.attempted, 0)
        self.assertEqual(calls, [["/bin/herdr", "pane", "list"]])

    def test_non_string_pane_token_value_fails_closed_without_metadata_write(self) -> None:
        for tokens in ({"mozyo_unit": 7}, {"unrelated": 7}):
            with self.subTest(tokens=tokens):
                calls: list[list[str]] = []

                def runner(argv, **kwargs):
                    calls.append(list(argv))
                    payload = {
                        "id": "cli:pane:list",
                        "result": {
                            "type": "pane_list",
                            "panes": [{"pane_id": "w1:p2", "tokens": tokens}],
                        },
                    }
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps(payload), stderr=""
                    )

                board = HerdrUnitBoardRuntime(
                    "/bin/herdr",
                    lister=FakeLister(()),
                    runner=runner,
                    lane_records_loader=lambda: {},
                    sync_lock_factory=nullcontext,
                )

                report = board.sync_metadata()

                self.assertFalse(report.ok)
                self.assertEqual(report.attempted, 0)
                self.assertEqual(report.updated, 0)
                self.assertEqual(calls, [["/bin/herdr", "pane", "list"]])

    def test_sync_holds_writer_lock_across_observe_report_and_verification(self) -> None:
        events: list[str] = []

        class OrderedLister:
            def list_agent_rows(self):
                events.append("inventory")
                return (row("codex", "w1:p2"),)

        @contextmanager
        def lock():
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")

        def runner(argv, **kwargs):
            events.append("metadata")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=OrderedLister(),
            runner=runner,
            workspace_loader=lambda workspace_id: workspace_record(),
            role_loader=lambda repo: role_bindings(),
            lane_records_loader=lambda: {},
            pane_rows_loader=lambda: (
                events.append("panes") or pane_rows("w1:p2")
            ),
            sync_lock_factory=lock,
        )

        self.assertTrue(board.sync_metadata().ok)
        self.assertEqual(
            events,
            [
                "lock-enter",
                "inventory",
                "panes",
                "inventory",
                "panes",
                "metadata",
                "inventory",
                "panes",
                "lock-exit",
            ],
        )

    def test_sync_lock_failure_is_typed_and_runs_no_inventory_or_metadata_io(self) -> None:
        lister = mock.Mock()

        @contextmanager
        def unavailable_lock():
            raise MetadataSyncLockError("acquire")
            yield

        runner = mock.Mock()
        pane_rows_loader = mock.Mock()
        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=lister,
            runner=runner,
            pane_rows_loader=pane_rows_loader,
            sync_lock_factory=unavailable_lock,
        )

        report = board.sync_metadata()

        self.assertFalse(report.ok)
        self.assertEqual(report.attempted, 0)
        self.assertEqual(report.failures[0].reason, "metadata_sync_lock_acquire_failed")
        lister.list_agent_rows.assert_not_called()
        pane_rows_loader.assert_not_called()
        runner.assert_not_called()

    def test_sync_lock_release_failure_preserves_already_attempted_updates(self) -> None:
        @contextmanager
        def release_failure():
            yield
            raise MetadataSyncLockError("release")

        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=FakeLister((row("codex", "w1:p2"),)),
            runner=runner,
            workspace_loader=lambda workspace_id: workspace_record(),
            role_loader=lambda repo: role_bindings(),
            lane_records_loader=lambda: {},
            pane_rows_loader=lambda: pane_rows("w1:p2"),
            sync_lock_factory=release_failure,
        )

        report = board.sync_metadata()

        self.assertFalse(report.ok)
        self.assertEqual(report.source_state, "unavailable")
        self.assertEqual(report.attempted, 1)
        self.assertEqual(report.updated, 1)
        self.assertEqual(report.failures[-1].reason, "metadata_sync_lock_release_failed")
        self.assertEqual(len(calls), 1)

    def test_real_writer_lock_serializes_independent_holders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()

            def first():
                with unit_board_metadata_lock(home):
                    first_entered.set()
                    release_first.wait(timeout=2)

            def second():
                first_entered.wait(timeout=2)
                with unit_board_metadata_lock(home):
                    second_entered.set()

            first_thread = threading.Thread(target=first)
            second_thread = threading.Thread(target=second)
            first_thread.start()
            second_thread.start()
            self.assertTrue(first_entered.wait(timeout=2))
            time.sleep(0.05)
            self.assertFalse(second_entered.is_set())
            release_first.set()
            first_thread.join(timeout=2)
            second_thread.join(timeout=2)
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertTrue(second_entered.is_set())
            self.assertTrue((home / METADATA_SYNC_LOCK_FILENAME).is_file())

    def test_real_writer_lock_refuses_a_symlink_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            target = Path(temp) / "target"
            target.write_text("unchanged", encoding="utf-8")
            (home / METADATA_SYNC_LOCK_FILENAME).symlink_to(target)

            with self.assertRaises(MetadataSyncLockError) as raised:
                with unit_board_metadata_lock(home):
                    self.fail("a symlink lock must never be acquired")

            self.assertEqual(raised.exception.phase, "acquire")
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_sync_reconciles_agent_transition_observed_during_metadata_write(self) -> None:
        old = (row("codex", "w1:p2"),)
        new_workspace = "b" * 32
        new = (
            row(
                "codex",
                "w1:p2",
                name=encode_assigned_name(new_workspace, "codex", "default"),
            ),
        )
        snapshots = [old, new, new, new]

        class TransitioningLister:
            def list_agent_rows(self):
                return snapshots.pop(0)

        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=TransitioningLister(),
            runner=runner,
            workspace_loader=lambda workspace_id: (
                workspace_record() if workspace_id == WORKSPACE_ID else None
            ),
            role_loader=lambda repo: role_bindings(),
            lane_records_loader=lambda: {},
            pane_rows_loader=lambda: pane_rows("w1:p2"),
            sync_lock_factory=nullcontext,
        )

        report = board.sync_metadata()

        self.assertTrue(report.ok)
        self.assertEqual(report.attempted, 1)
        self.assertEqual(report.updated, 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all("--seq" not in argv for argv in calls))
        unit_tokens = [
            value
            for argv in calls
            for index, value in enumerate(argv)
            if index > 0
            and argv[index - 1] == "--token"
            and value.startswith("mozyo_unit=")
        ]
        self.assertEqual(len(unit_tokens), 1)

    def test_concurrent_hook_processes_converge_to_the_newest_live_identity(self) -> None:
        old = (row("codex", "w1:p2"),)
        new_workspace = "b" * 32
        new = (
            row(
                "codex",
                "w1:p2",
                name=encode_assigned_name(new_workspace, "codex", "default"),
            ),
        )
        current = [old]
        first_report_started = threading.Event()
        release_first_report = threading.Event()
        calls: list[list[str]] = []
        call_guard = threading.Lock()

        class LiveLister:
            def list_agent_rows(self):
                return current[0]

        def runner(argv, **kwargs):
            with call_guard:
                calls.append(list(argv))
                call_number = len(calls)
            if call_number == 1:
                first_report_started.set()
                release_first_report.wait(timeout=2)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)

            def build_runtime():
                return HerdrUnitBoardRuntime(
                    "/bin/herdr",
                    lister=LiveLister(),
                    runner=runner,
                    workspace_loader=lambda workspace_id: (
                        workspace_record() if workspace_id == WORKSPACE_ID else None
                    ),
                    role_loader=lambda repo: role_bindings(),
                    lane_records_loader=lambda: {},
                    pane_rows_loader=lambda: pane_rows("w1:p2"),
                    sync_lock_factory=lambda: unit_board_metadata_lock(home),
                )

            reports: list[object] = []
            first_thread = threading.Thread(
                target=lambda: reports.append(build_runtime().sync_metadata())
            )
            second_thread = threading.Thread(
                target=lambda: reports.append(build_runtime().sync_metadata())
            )
            first_thread.start()
            self.assertTrue(first_report_started.wait(timeout=2))
            current[0] = new
            second_thread.start()
            time.sleep(0.05)
            release_first_report.set()
            first_thread.join(timeout=3)
            second_thread.join(timeout=3)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(report.ok for report in reports))
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(all("--seq" not in argv for argv in calls))
        unit_tokens = [
            next(
                argv[index + 1]
                for index, value in enumerate(argv)
                if value == "--token"
                and argv[index + 1].startswith("mozyo_unit=")
            )
            for argv in calls
        ]
        self.assertNotEqual(unit_tokens[0], unit_tokens[-1])
        self.assertEqual(unit_tokens[-1], unit_tokens[-2])

    def test_sync_fails_visibly_when_inventory_never_stabilizes(self) -> None:
        other_workspace = "b" * 32
        rows = [
            (
                row(
                    "codex",
                    "w1:p2",
                    name=encode_assigned_name(
                        WORKSPACE_ID if index % 2 == 0 else other_workspace,
                        "codex",
                        "default",
                    ),
                ),
            )
            for index in range(4)
        ]

        class OscillatingLister:
            def list_agent_rows(self):
                return rows.pop(0)

        board = HerdrUnitBoardRuntime(
            "/bin/herdr",
            lister=OscillatingLister(),
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, stdout="", stderr=""
            ),
            workspace_loader=lambda workspace_id: None,
            role_loader=lambda repo: role_bindings(),
            lane_records_loader=lambda: {},
            pane_rows_loader=lambda: pane_rows("w1:p2"),
            sync_lock_factory=nullcontext,
        )

        report = board.sync_metadata()

        self.assertFalse(report.ok)
        self.assertEqual(report.source_state, "reload_required")
        self.assertEqual(report.attempted, 0)
        self.assertEqual(report.updated, 0)
        self.assertEqual(report.failures[-1].reason, "inventory_changed_during_sync")

    def test_sync_failure_is_nonzero_quality_signal_without_path_disclosure(self) -> None:
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="/private/x")

        report = runtime((row("codex", "w1:p2"),), runner=runner).sync_metadata()

        self.assertFalse(report.ok)
        self.assertEqual(report.updated, 0)
        self.assertEqual(report.failures[0].reason, "metadata_update_failed")
        self.assertNotIn("/private/x", repr(report.as_payload()))

    def test_invalid_pane_locator_requires_reload_and_never_runs_metadata_command(self) -> None:
        for locator in ("--help", "w1:p1 extra", "w1:p1;next", "w1:p1\n"):
            with self.subTest(locator=locator):
                runner = mock.Mock()
                board = runtime((row("codex", locator),), runner=runner)

                snapshot = board.snapshot()
                report = board.sync_metadata()

                self.assertFalse(snapshot.ok)
                self.assertEqual(snapshot.source_state, "reload_required")
                self.assertFalse(report.ok)
                self.assertEqual(report.source_state, "reload_required")
                self.assertEqual(report.attempted, 0)
                self.assertEqual(report.updated, 0)
                runner.assert_not_called()

    def test_duplicate_managed_pane_locator_requires_reload_without_metadata_io(self) -> None:
        other_workspace = "b" * 32
        runner = mock.Mock()
        board = runtime(
            (
                row("codex", "w1:p1"),
                row(
                    "claude",
                    "w1:p1",
                    name=encode_assigned_name(
                        other_workspace, "claude", "default"
                    ),
                ),
            ),
            runner=runner,
        )

        snapshot = board.snapshot()
        report = board.sync_metadata()

        self.assertFalse(snapshot.ok)
        self.assertEqual(snapshot.source_state, "reload_required")
        self.assertFalse(report.ok)
        self.assertEqual(report.attempted, 0)
        runner.assert_not_called()

    def test_runtime_redacts_unsafe_authority_from_json_text_and_metadata_argv(self) -> None:
        private_path = "/" + "/".join(("synthetic", "private", "project"))
        jwe_header = base64.urlsafe_b64encode(
            b'{"alg":"dir","enc":"A256GCM"}'
        ).decode().rstrip("=")
        encrypted_jwt = ".".join(
            (jwe_header, "", "aXY", "Y2lwaGVy", "dGFn")
        )
        nested_header = base64.urlsafe_b64encode(
            b'{"alg":"HS256","cty":"JWT"}'
        ).decode().rstrip("=")
        nested_payload = base64.urlsafe_b64encode(
            b"eyJhbGciOiJub25lIn0.e30."
        ).decode().rstrip("=")
        nested_signed_jwt = ".".join((nested_header, nested_payload, "c2ln"))
        credential_shapes = (
            "=".join(("_".join(("AUTH", "TOKEN")), "synthetic-value")),
            "=".join(
                (
                    "_".join(("AWS", "ACCESS", "KEY", "ID")),
                    "synthetic-material-123456",
                )
            ),
            ": ".join(
                (
                    "".join(("Author", "ization")),
                    " ".join(("Ba" + "sic", "c3ludGhldGljLW1hdGVyaWFs")),
                )
            ),
            "=".join(
                (
                    "session",
                    ".".join(
                        (
                            "eyJzeW50aGV0aWMiOiJ0ZXN0In0",
                            "eyJ2YWx1ZSI6InRlc3QifQ",
                            "c3ludGhldGljc2lnbmF0dXJl",
                        )
                    ),
                )
            ),
            " = ".join(("AWS ACCESS KEY ID", "synthetic-material-765432")),
            "=".join(
                (
                    "session",
                    ".".join(
                        (
                            "eyJhbGciOiJub25lIn0",
                            "eyJzdWIiOiJzeW50aGV0aWMifQ",
                            "",
                        )
                    ),
                )
            ),
            "session=eyJhbGciOiJub25lIn0.e30.",
            "session=eyJhbGciOiJIUzI1NiJ9.e30.c2ln",
            encrypted_jwt,
            nested_signed_jwt,
            '{"token":"synthetic-material-112233"}',
            '{"password": "synthetic-material-223344"}',
            "{'api_key':'synthetic-material-334455'}",
            "session=synthetic-material-445566",
            "session_id=synthetic-material-556677",
            "auth=synthetic-material-667788",
            '{"sessionId":"synthetic-material-778899"}',
            r'{"to\u006ben":"synthetic-material-889900"}',
            r'{"public":[{"pass\u0077ord":"synthetic-material-990011"}]}',
            r'{"public":"Bearer\u0020synthetic-material-12345678"}',
            r'{"public":"ghp_\u0078xxxxxxxxxxxxxxxxxxxxxxx"}',
            r'{"\uff54oken":"synthetic-material-101112"}',
            r'prefix={"to\u006ben":"synthetic-material-121314"}',
            r'{"public":"session\u003dsynthetic-material-141516"}',
            r'{"public":"api_key\u003dsynthetic-material-161718"}',
            r'{"public":"{\"token\":\"synthetic-material-181920\"}"}',
            "Bearer\tsynthetic-material-20212223",
            "Basic\tc3ludGhldGljLW1hdGVyaWFs",
            r'{"public":"Bearer\u0009synthetic-material-24252627"}',
            (
                r'{"to\u006ben":"synthetic-material-282930","n":'
                + ("9" * 5_000)
                + "}"
            ),
            r'{"\u002fsynthetic\u002fprivate\u002fproject":"value"}',
            r'{"\u0043\u003a\u005csynthetic\u005cprivate":"value"}',
            r'{"ghp_\u0078xxxxxxxxxxxxxxxxxxxxxxx":"value"}',
            "MYSQL_PWD=synthetic-material-313233",
            "DB_PASS=synthetic-material-343536",
            "pwd=synthetic-material-373839",
            "pass=synthetic-material-404142",
            "dbPass=synthetic-material-434445",
            "mysqlPwd=synthetic-material-464748",
            '{"dbPass":"synthetic-material-495051"}',
            '{"mysqlPwd":"synthetic-material-525354"}',
            "~synthetic-user/private/project",
            "~\\synthetic\\private\\project",
            "xoxb-" + ("s" * 20),
            "AIza" + ("g" * 35),
            "glpat-" + ("g" * 20),
            "npm_" + ("n" * 20),
            "pypi-" + ("p" * 20),
            "sk_live_" + ("s" * 20),
            "github_pat_" + ("g" * 70),
            "ghs_123456_" + ("g" * 30),
            "rk_live_" + ("r" * 20),
            "sk_test_" + ("t" * 20),
            "xapp-1-" + ("a" * 20),
            "ASIA" + ("A" * 16),
            r'"Bearer\u0020synthetic-material-55565758"',
            r'"session\u003dsynthetic-material-59606162"',
            r'"{\"token\":\"synthetic-material-63646566\"}"',
        )

        for credential_shape in credential_shapes:
            with self.subTest(shape=credential_shape.split("=", 1)[0][:8]):
                calls: list[list[str]] = []

                def runner(argv, **kwargs):
                    calls.append(list(argv))
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="", stderr=""
                    )

                parsed = ParsedRoleBindings.valid(
                    (
                        WorkflowRoleBinding(
                            role="coordinator",
                            project_scope=credential_shape,
                            lane_id="default",
                        ),
                    )
                )
                board = HerdrUnitBoardRuntime(
                    "/bin/herdr",
                    lister=FakeLister((row("codex", "w1:p2"),)),
                    runner=runner,
                    workspace_loader=lambda workspace_id: SimpleNamespace(
                        project_name=private_path,
                        canonical_path="/reviewable/repo",
                    ),
                    role_loader=lambda repo: parsed,
                    lane_records_loader=lambda: {},
                    pane_rows_loader=lambda: pane_rows("w1:p2"),
                )

                snapshot = board.snapshot()
                public_output = repr(snapshot.as_payload()) + format_board(
                    snapshot, width=80
                )
                report = board.sync_metadata()
                metadata_argv = repr(calls)
                self.assertTrue(report.ok)
                for private_value in (private_path, credential_shape):
                    self.assertNotIn(private_value, public_output)
                    self.assertNotIn(private_value, metadata_argv)
                self.assertIn("mozyo_responsibility=[redacted]", metadata_argv)


class HerdrUnitBoardCliTests(unittest.TestCase):
    def parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="group", required=True)
        herdr = sub.add_parser("herdr")
        register_herdr_unit_board_parser(
            herdr.add_subparsers(dest="herdr_command", required=True)
        )
        return parser

    def test_show_json_is_public_safe_and_returns_success(self) -> None:
        args = self.parser().parse_args(["herdr", "unit-board", "show", "--json"])
        board = runtime((row("codex", "w1:p2"),))
        output = StringIO()
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_unit_board._runtime",
            return_value=board,
        ), redirect_stdout(output):
            result = args.func(args)

        self.assertEqual(result, 0)
        self.assertIn('"workflow_role": "coordinator"', output.getvalue())
        self.assertNotIn("w1:p2", output.getvalue())

    def test_show_rejects_extreme_width_during_argument_parsing(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.parser().parse_args(
                ["herdr", "unit-board", "show", "--width", str(2**63 - 1)]
            )

        self.assertEqual(raised.exception.code, 2)

    def test_watch_rejects_unsafe_refresh_rate_before_runtime_io(self) -> None:
        args = self.parser().parse_args(
            ["herdr", "unit-board", "watch", "--interval", "0.1"]
        )
        output = StringIO()
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_unit_board._runtime"
        ) as factory, redirect_stdout(output):
            result = args.func(args)
        self.assertEqual(result, 2)
        factory.assert_not_called()

    def test_show_json_turns_runtime_resolution_error_into_path_free_failure(self) -> None:
        args = self.parser().parse_args(["herdr", "unit-board", "show", "--json"])
        private_path = "/" + "/".join(("synthetic", "private", "herdr"))
        output = StringIO()
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_unit_board._runtime",
            side_effect=ValueError(private_path),
        ), redirect_stdout(output):
            result = args.func(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(payload["source_state"], "unavailable")
        self.assertNotIn(private_path, output.getvalue())

    def test_sync_json_turns_runtime_resolution_error_into_structured_failure(self) -> None:
        args = self.parser().parse_args(["herdr", "unit-board", "sync", "--json"])
        private_path = "/" + "/".join(("synthetic", "private", "herdr"))
        output = StringIO()
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_unit_board._runtime",
            side_effect=OSError(private_path),
        ), redirect_stdout(output):
            result = args.func(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["source_state"], "unavailable")
        self.assertEqual(payload["failures"][0]["reason"], "runtime_unavailable")
        self.assertNotIn(private_path, output.getvalue())

    def test_watch_exits_nonzero_on_unavailable_snapshot(self) -> None:
        args = self.parser().parse_args(
            ["herdr", "unit-board", "watch", "--interval", "0.5"]
        )
        board = mock.Mock()
        board.snapshot.return_value = unavailable_snapshot(
            "unavailable", observed_at="now", detail="inventory unavailable"
        )
        output = StringIO()
        with mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_unit_board._runtime",
            return_value=board,
        ), mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_unit_board.time.sleep"
        ) as sleep, redirect_stdout(output):
            result = args.func(args)

        self.assertEqual(result, 1)
        sleep.assert_not_called()
        self.assertIn("source=unavailable", output.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
