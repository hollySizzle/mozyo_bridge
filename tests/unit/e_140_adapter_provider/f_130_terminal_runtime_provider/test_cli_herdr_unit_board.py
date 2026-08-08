"""CLI and runtime tests for the Herdr coordinator Unit board (Redmine #15114)."""

from __future__ import annotations

import argparse
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_unit_board_runtime import (
    HerdrUnitBoardRuntime,
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


def runtime(rows, *, runner=None, parsed=None) -> HerdrUnitBoardRuntime:
    return HerdrUnitBoardRuntime(
        "/bin/herdr",
        lister=FakeLister(rows),
        runner=runner,
        workspace_loader=lambda workspace_id: (
            workspace_record() if workspace_id == WORKSPACE_ID else None
        ),
        role_loader=lambda repo: parsed if parsed is not None else role_bindings(),
        lane_records_loader=lambda: {},
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
            self.assertIn("--source", argv)
            self.assertIn("--title", argv)
            self.assertIn("--display-agent", argv)
            self.assertNotIn("send-text", argv)
            self.assertNotIn("send-keys", argv)
            self.assertNotIn("prompt", argv)
        self.assertEqual({argv[-1] for argv in calls}, {"w1:p1", "w1:p2"})

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

    def test_runtime_redacts_unsafe_authority_from_json_text_and_metadata_argv(self) -> None:
        private_path = "/" + "/".join(("synthetic", "private", "project"))
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
