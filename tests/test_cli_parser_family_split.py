"""Characterization tests for the residual CLI parser family split (Redmine #12153).

The residual `build_parser()` parser registration moved out of
``application/cli.py`` into feature-family modules (agents / cockpit / handoff /
observability / runtime-config / session). These tests pin the behavior that the
move must preserve:

- the top-level subcommand *order* (observable in ``--help``),
- the ``func`` binding and key ``dest`` for representative moved commands,
- the family modules' ``register`` entry points,
- the backward-compatible ``application.cli`` import surface (the #12138 scope
  guard "do not retire legacy import paths").
"""
from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.application import (
    cli,
    cli_agents,
    cli_cockpit,
    cli_handoff,
    cli_observability,
    cli_runtime_config,
    cli_session,
)
from mozyo_bridge.application.cli import build_parser


def _top_level_subcommands(parser: argparse.ArgumentParser) -> list[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices.keys())
    raise AssertionError("no subparsers action on top-level parser")


def _bound_func_name(parser: argparse.ArgumentParser, argv: list[str]) -> str:
    args = parser.parse_args(argv)
    return args.func.__name__


# The exact pre-split top-level subcommand order. Reordering changes the
# ``--help`` positional metavar and body, which is a public-CLI change the
# #12153 non-goals forbid.
EXPECTED_TOP_LEVEL_ORDER = [
    "status",
    "list",
    "layout",
    "cockpit",
    "agents",
    "presentation",
    "tmux-ui-config",
    "tmux-ui",
    "id",
    "resolve",
    "read",
    "type",
    "message",
    "keys",
    "notify-codex",
    "notify-claude",
    "notify-codex-review",
    "notify-claude-review-result",
    "notify-codex-legacy-task",
    "notify-claude-legacy-task",
    "handoff",
    "reply",
    "init",
    "doctor",
    "sublane",
    "runtime-config",
    "instruction",
    "rules",
    "scaffold",
    "docs",
    "events",
    "otel",
    "observe",
    "session",
    "workspace",
    "workspace-defaults",
    "state",
    "release",
    "health",
]


class TopLevelOrderTest(unittest.TestCase):
    def test_top_level_subcommand_order_is_preserved(self) -> None:
        self.assertEqual(
            _top_level_subcommands(build_parser()),
            EXPECTED_TOP_LEVEL_ORDER,
        )


class FamilyModuleRegistrationTest(unittest.TestCase):
    def test_family_modules_expose_register_entry_points(self) -> None:
        self.assertTrue(callable(cli_agents.register))
        self.assertTrue(callable(cli_cockpit.register))
        self.assertTrue(callable(cli_cockpit.register_tmux_ui))
        self.assertTrue(callable(cli_handoff.register_message))
        self.assertTrue(callable(cli_handoff.register))
        self.assertTrue(callable(cli_observability.register))
        self.assertTrue(callable(cli_runtime_config.register))
        self.assertTrue(callable(cli_session.register))


class MovedCommandFuncBindingTest(unittest.TestCase):
    """Representative parse-time `func` bindings for the moved families."""

    def setUp(self) -> None:
        self.parser = build_parser()

    def test_agents_bindings(self) -> None:
        self.assertEqual(_bound_func_name(self.parser, ["agents", "list"]), "cmd_agents_list")
        self.assertEqual(_bound_func_name(self.parser, ["agents", "targets"]), "cmd_agents_targets")
        self.assertEqual(
            _bound_func_name(self.parser, ["agents", "attention-project"]),
            "cmd_agents_attention_project",
        )

    def test_cockpit_family_bindings(self) -> None:
        self.assertEqual(
            _bound_func_name(self.parser, ["layout", "apply", "cockpit"]),
            "cmd_layout_apply",
        )
        self.assertEqual(_bound_func_name(self.parser, ["cockpit"]), "cmd_cockpit")
        self.assertEqual(_bound_func_name(self.parser, ["tmux-ui-config"]), "cmd_config")
        self.assertEqual(
            _bound_func_name(self.parser, ["tmux-ui", "install"]),
            "cmd_tmux_ui_install",
        )

    def test_handoff_family_bindings(self) -> None:
        self.assertEqual(_bound_func_name(self.parser, ["message", "%1", "hi"]), "cmd_message")
        self.assertEqual(
            _bound_func_name(self.parser, ["notify-codex"]),
            "cmd_notify_codex",
        )
        self.assertEqual(
            _bound_func_name(self.parser, ["handoff", "send", "--to", "claude", "--source", "redmine", "--kind", "reply"]),
            "cmd_handoff_send",
        )
        self.assertEqual(
            _bound_func_name(self.parser, ["reply", "--to", "claude", "--source", "redmine"]),
            "cmd_handoff_reply",
        )

    def test_observability_bindings(self) -> None:
        self.assertEqual(_bound_func_name(self.parser, ["events", "tail"]), "cmd_events_tail")
        self.assertEqual(_bound_func_name(self.parser, ["otel", "serve"]), "cmd_otel_serve")
        self.assertEqual(
            _bound_func_name(self.parser, ["otel", "launchd", "status"]),
            "cmd_otel_launchd",
        )
        self.assertEqual(
            _bound_func_name(self.parser, ["observe", "reload"]),
            "cmd_observe_reload",
        )

    def test_session_bindings(self) -> None:
        self.assertEqual(_bound_func_name(self.parser, ["session", "name"]), "cmd_session_name")
        self.assertEqual(
            _bound_func_name(self.parser, ["session", "boundary-prompt", "--issue", "1", "--journal", "2"]),
            "cmd_session_boundary_prompt",
        )

    def test_runtime_config_bindings_and_deprecation_metadata(self) -> None:
        self.assertEqual(
            _bound_func_name(self.parser, ["runtime-config", "check"]),
            "cmd_instruction_doctor",
        )
        self.assertEqual(
            _bound_func_name(self.parser, ["runtime-config", "install"]),
            "cmd_instruction_install",
        )
        # The deprecated `instruction` alias keeps its warn-before-dispatch metadata.
        canonical = self.parser.parse_args(["instruction", "doctor"])
        self.assertEqual(canonical.deprecated_alias, "mozyo-bridge instruction doctor")
        self.assertEqual(canonical.canonical_command, "mozyo-bridge runtime-config check")


class BackwardCompatibleImportSurfaceTest(unittest.TestCase):
    """The moved handler/helper/constant symbols stay importable from `cli`."""

    def test_moved_handlers_remain_module_attributes(self) -> None:
        for name in [
            "cmd_agents_list",
            "cmd_cockpit",
            "cmd_config",
            "cmd_layout_apply",
            "cmd_tmux_ui_install",
            "cmd_message",
            "cmd_notify_codex",
            "cmd_handoff_send",
            "cmd_events_tail",
            "cmd_otel_serve",
            "cmd_session_name",
            "cmd_instruction_doctor",
            "cmd_instruction_install",
        ]:
            self.assertTrue(hasattr(cli, name), f"cli.{name} missing")

    def test_moved_helpers_remain_module_attributes(self) -> None:
        for name in [
            "add_notify_options",
            "add_notify_delivery_options",
            "add_legacy_notify_options",
            "_add_runtime_config_check_parser",
            "_add_runtime_config_install_parser",
        ]:
            self.assertTrue(hasattr(cli, name), f"cli.{name} missing")

    def test_moved_constants_remain_module_attributes(self) -> None:
        for name in [
            "AGENT_KINDS",
            "KIND_LABELS",
            "MODES",
            "MODE_QUEUE_ENTER",
            "RECORD_FORMATS",
            "RECORD_FORMAT_BOTH",
            "SOURCES",
            "SESSION_BOUNDARY_SIGNALS",
        ]:
            self.assertTrue(hasattr(cli, name), f"cli.{name} missing")


if __name__ == "__main__":
    unittest.main()
