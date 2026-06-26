"""Characterize the otel/event command-handler facade split (Redmine #12154).

The OTel / event-timeline handlers were moved out of the giant
``application/commands.py`` into the focused ``application/commands_otel``
family module. ``commands.py`` re-exports them so the historical import and
monkeypatch surface (``mozyo_bridge.application.commands.cmd_otel_*`` /
``cmd_events_*``) keeps resolving.

These tests pin the compatibility contract, not the handler behavior (the
latter is covered by ``test_otel_store`` / ``test_event_timeline``):

- every moved symbol is importable from the ``commands`` facade;
- the facade symbol is the *same object* as the family-module symbol (so a
  monkeypatch on either resolves to one definition, and the CLI parser binds
  the same callable it always did);
- the family module owns exactly the moved family and no stragglers.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import commands as commands_facade
from mozyo_bridge.application import commands_otel

# The full re-export surface the facade must preserve. Public handlers plus the
# two private renderer/store helpers that were importable as
# ``commands._events_store`` / ``commands._render_timeline`` before the split.
MOVED_SYMBOLS = (
    "cmd_otel_serve",
    "cmd_otel_status",
    "cmd_otel_events",
    "cmd_otel_activity",
    "cmd_otel_launchd",
    "cmd_events_tail",
    "cmd_events_query",
    "_events_store",
    "_render_timeline",
)


class OtelFacadeCompatibilityTest(unittest.TestCase):
    def test_facade_reexports_every_moved_symbol(self) -> None:
        missing = [name for name in MOVED_SYMBOLS if not hasattr(commands_facade, name)]
        self.assertEqual([], missing, f"facade lost re-exports: {missing}")

    def test_facade_symbol_is_same_object_as_family_module(self) -> None:
        # Identity (not just equality) so a monkeypatch target like
        # `mozyo_bridge.application.commands.cmd_otel_status` resolves to the
        # one definition that now lives in commands_otel.
        for name in MOVED_SYMBOLS:
            with self.subTest(symbol=name):
                self.assertIs(
                    getattr(commands_facade, name),
                    getattr(commands_otel, name),
                )

    def test_handlers_are_defined_in_the_family_module(self) -> None:
        # Behavior-preserving move means the definitions now physically live in
        # the commands_otel family module, not that they are aliases pointing
        # back at commands. Redmine #12624 (US #12622) relocated that family body
        # into the Redmine-numbered execution_platform layout; the legacy import
        # path ``mozyo_bridge.application.commands_otel`` stays valid via the
        # ``sys.modules`` facade, so ``__module__`` now reports the numbered home.
        for name in MOVED_SYMBOLS:
            with self.subTest(symbol=name):
                self.assertEqual(
                    getattr(commands_otel, name).__module__,
                    "mozyo_bridge.e_110_execution_platform"
                    ".f_150_runtime_observation_event_timeline.application.commands_otel",
                )

    def test_cli_parser_binds_the_facade_handlers(self) -> None:
        # The CLI import path is the real consumer of the facade; pin that the
        # parser dispatches the otel/event subcommands to the moved callables.
        from mozyo_bridge.application.cli import build_parser

        parser = build_parser()
        cases = {
            ("otel", "serve"): commands_otel.cmd_otel_serve,
            ("otel", "status"): commands_otel.cmd_otel_status,
            ("otel", "events"): commands_otel.cmd_otel_events,
            ("otel", "activity"): commands_otel.cmd_otel_activity,
            ("events", "tail"): commands_otel.cmd_events_tail,
            ("events", "query"): commands_otel.cmd_events_query,
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                args = parser.parse_args(list(argv))
                self.assertIs(args.func, expected)


if __name__ == "__main__":
    unittest.main()
