"""CLI tests for the multi-source Unit board surfaces (Redmine #15138)."""

from __future__ import annotations

import argparse
import json
import unittest
from datetime import datetime, timezone
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    UnitBoardSourceError,
    UnitBoardSourcesConfig,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (
    cli_herdr_unit_board as cli,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_multi_source_unit_board import (
    REMOTE_BOARD_ARGS,
    REMOTE_WORKSPACE_ARGS,
    MultiSourceUnitBoardRuntime,
)

from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_remote_unit_action import (
    delivery_record,
)
from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_multi_source_unit_board import (
    REMOTE_CONFIG,
    WORKSPACE_PAYLOAD,
    FakeLocalRuntime,
    RecordingRunner,
    local_snapshot,
    remote_board_payload,
)


GATEWAY_ARGS = ("project-gateway", "handoff")


def parse(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="herdr_command", required=True)
    cli.register_herdr_unit_board_parser(sub)
    return parser.parse_args(argv)


def fresh_remote_board():
    """The remote answer stamped now: the CLI runs on the real wall clock."""
    payload = remote_board_payload()
    payload["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return payload


def answers(overrides=None):
    base = {
        REMOTE_BOARD_ARGS: fresh_remote_board(),
        REMOTE_WORKSPACE_ARGS: WORKSPACE_PAYLOAD,
        GATEWAY_ARGS: delivery_record(),
    }
    base.update(overrides or {})
    return base


class _Wiring:
    """Bind the CLI to an injected multi-source runtime and source config."""

    def __init__(self, config=REMOTE_CONFIG, answer_map=None, local=None) -> None:
        self.runner = RecordingRunner(answer_map if answer_map is not None else answers())
        self.config = config
        self.local = local if local is not None else FakeLocalRuntime(
            local_snapshot(
                observed_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
        )
        self.runtime = MultiSourceUnitBoardRuntime(
            config,
            local_runtime=self.local,
            # Real wall clock: the CLI builds its own action rail with the
            # production clock, so a frozen observation clock would make every
            # apply read as a stale preview for reasons the code never has.
            runner=self.runner,
        )

    def __enter__(self):
        self._patches = [
            mock.patch.object(cli, "load_unit_board_sources", return_value=self.config),
            mock.patch.object(
                cli, "_multi_source_runtime", return_value=self.runtime
            ),
            # The single-server path resolves the LOCAL runtime, not the
            # aggregating one; conflating them would hide whether `--local-only`
            # really bypasses the configured sources.
            mock.patch.object(
                cli, "_load_runtime", return_value=(self.local, None)
            ),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self._patches:
            patch.stop()
        return False


def run(handler, args):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = handler(args)
    return code, out.getvalue(), err.getvalue()


class ShowTests(unittest.TestCase):
    def test_multi_source_show_renders_every_source(self) -> None:
        with _Wiring():
            code, out, _ = run(
                cli.cmd_herdr_unit_board_show, parse(["unit-board", "show"])
            )

        self.assertEqual(code, 0)
        self.assertIn("source local [local] live", out)
        self.assertIn("source dev host [ssh] live", out)
        self.assertIn("[dev host]", out)

    def test_multi_source_json_carries_the_source_envelope(self) -> None:
        with _Wiring():
            code, out, _ = run(
                cli.cmd_herdr_unit_board_show, parse(["unit-board", "show", "--json"])
            )

        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["sources"]), 2)
        self.assertNotIn("SSH-DESTINATION-SENTINEL", out)

    def test_broken_source_file_stops_the_board_instead_of_defaulting(self) -> None:
        with mock.patch.object(
            cli,
            "load_unit_board_sources",
            side_effect=UnitBoardSourceError("bad source file"),
        ):
            code, out, err = run(
                cli.cmd_herdr_unit_board_show, parse(["unit-board", "show"])
            )

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("bad source file", err)

    def test_local_only_flag_ignores_configured_sources_entirely(self) -> None:
        # A client aggregating this host asks with the flag; the answer must
        # describe exactly one server even though sources are configured, and
        # must not reach out to any of them.
        wiring = _Wiring()
        with wiring:
            code, out, _ = run(
                cli.cmd_herdr_unit_board_show,
                parse(["unit-board", "show", "--json", "--local-only"]),
            )

        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertNotIn("sources", payload)
        self.assertEqual(wiring.runner.argvs, [])

    def test_local_only_show_keeps_the_single_server_rendering(self) -> None:
        wiring = _Wiring(config=UnitBoardSourcesConfig.default())
        with wiring:
            code, out, _ = run(
                cli.cmd_herdr_unit_board_show, parse(["unit-board", "show", "--json"])
            )

        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertNotIn("sources", payload)


class SourcesTests(unittest.TestCase):
    def test_sources_lists_identity_without_connection_values(self) -> None:
        with _Wiring():
            code, out, _ = run(
                cli.cmd_herdr_unit_board_sources, parse(["unit-board", "sources"])
            )

        self.assertEqual(code, 0)
        self.assertIn("ok local [local] live", out)
        self.assertIn("ok dev host [ssh] live", out)
        self.assertNotIn("SSH-DESTINATION-SENTINEL", out)
        self.assertNotIn("ssh_target", out)

    def test_sources_exits_non_zero_when_one_source_is_not_live(self) -> None:
        with _Wiring(answer_map=answers({REMOTE_BOARD_ARGS: OSError("no route")})):
            code, out, _ = run(
                cli.cmd_herdr_unit_board_sources, parse(["unit-board", "sources"])
            )

        self.assertEqual(code, 1)
        self.assertIn("!! dev host [ssh] unavailable", out)

    def test_unreachable_local_server_does_not_hide_the_remote_sources(self) -> None:
        with _Wiring(local=FakeLocalRuntime(error=RuntimeError("no local herdr"))):
            code, out, _ = run(
                cli.cmd_herdr_unit_board_sources, parse(["unit-board", "sources"])
            )

        self.assertEqual(code, 1)
        self.assertIn("!! local [local] unavailable", out)
        self.assertIn("ok dev host [ssh] live", out)

    def test_action_requires_an_explicit_project_scope(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            parse(
                [
                    "unit-board",
                    "action",
                    "--unit",
                    "unit-x",
                    "--issue",
                    "1",
                    "--journal",
                    "2",
                    "--summary",
                    "s",
                ]
            )

    def test_sources_json_is_public_safe(self) -> None:
        with _Wiring():
            code, out, _ = run(
                cli.cmd_herdr_unit_board_sources,
                parse(["unit-board", "sources", "--json"]),
            )

        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(
            {source["host_id"] for source in payload["sources"]}, {"local", "devbox"}
        )
        self.assertNotIn("SSH-DESTINATION-SENTINEL", out)


class ActionTests(unittest.TestCase):
    def _remote_unit(self, wiring) -> str:
        return next(
            unit.unit_id
            for unit in wiring.runtime.snapshot().units
            if unit.host_id == "devbox"
        )

    def test_preview_is_the_default_and_sends_nothing(self) -> None:
        wiring = _Wiring()
        with wiring:
            unit_id = self._remote_unit(wiring)
            code, out, _ = run(
                cli.cmd_herdr_unit_board_action,
                parse(
                    [
                        "unit-board",
                        "action",
                        "--unit",
                        unit_id,
                        "--issue",
                        "15138",
                        "--journal",
                        "101633",
                        "--summary",
                        "board pointer",
                        "--target-project",
                        "scope-alpha",
                    ]
                ),
            )

        self.assertEqual(code, 0)
        self.assertIn("remote Unit action: preview", out)
        self.assertFalse(
            [argv for argv in wiring.runner.argvs if "project-gateway" in argv[-1]]
        )

    def test_apply_delivers_once_through_the_source_gateway(self) -> None:
        wiring = _Wiring()
        with wiring:
            unit_id = self._remote_unit(wiring)
            code, out, _ = run(
                cli.cmd_herdr_unit_board_action,
                parse(
                    [
                        "unit-board",
                        "action",
                        "--unit",
                        unit_id,
                        "--issue",
                        "15138",
                        "--journal",
                        "101633",
                        "--summary",
                        "board pointer",
                        "--target-project",
                        "scope-alpha",
                        "--apply",
                    ]
                ),
            )

        self.assertEqual(code, 0)
        self.assertIn("delivered", out)
        gateway = [
            argv for argv in wiring.runner.argvs if "project-gateway" in argv[-1]
        ]
        self.assertEqual(len(gateway), 1)

    def test_unresolvable_unit_exits_non_zero_and_sends_nothing(self) -> None:
        wiring = _Wiring()
        with wiring:
            code, out, _ = run(
                cli.cmd_herdr_unit_board_action,
                parse(
                    [
                        "unit-board",
                        "action",
                        "--unit",
                        "unit-absent",
                        "--issue",
                        "15138",
                        "--journal",
                        "101633",
                        "--summary",
                        "board pointer",
                        "--target-project",
                        "scope-alpha",
                        "--apply",
                    ]
                ),
            )

        self.assertEqual(code, 1)
        self.assertIn("refused", out)
        self.assertFalse(
            [argv for argv in wiring.runner.argvs if "project-gateway" in argv[-1]]
        )

    def test_claude_is_not_an_addressable_receiver_on_this_surface(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            parse(
                [
                    "unit-board",
                    "action",
                    "--unit",
                    "unit-x",
                    "--issue",
                    "1",
                    "--journal",
                    "2",
                    "--summary",
                    "s",
                    "--target-project",
                    "scope-alpha",
                    "--to",
                    "claude",
                ]
            )


if __name__ == "__main__":
    unittest.main()
