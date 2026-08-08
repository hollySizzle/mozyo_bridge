"""Static integration contract for the Herdr Unit board plugin (Redmine #15114)."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
MANIFEST = ROOT / "herdr-plugins" / "mozyo-unit-board" / "herdr-plugin.toml"
ALLOWED_PREFIX = ["mozyo-bridge", "herdr", "unit-board"]


class HerdrUnitBoardPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_declares_reviewable_presentation_consumer(self) -> None:
        self.assertEqual(self.document["id"], "mozyo.unit-board")
        self.assertEqual(self.document["version"], "0.2.0")
        self.assertEqual(self.document["min_herdr_version"], "0.8.0")
        self.assertNotIn("build", self.document)
        self.assertEqual(self.document["panes"][0]["placement"], "popup")
        self.assertEqual(self.document["panes"][0]["command"][-1], "interact")

    def test_every_hook_invokes_only_the_public_unit_board_cli(self) -> None:
        records = (
            self.document.get("startup", [])
            + self.document.get("actions", [])
            + self.document.get("events", [])
            + self.document.get("panes", [])
        )
        self.assertGreaterEqual(len(records), 4)
        for record in records:
            command = record["command"]
            self.assertEqual(command[:3], ALLOWED_PREFIX)
            rendered = " ".join(command)
            self.assertNotIn("send", rendered)
            self.assertNotIn("move", rendered)
            self.assertNotIn("workflow", rendered)
            self.assertNotIn("redmine", rendered)

    def test_events_are_idempotent_display_refresh_triggers(self) -> None:
        events = {event["on"]: event["command"] for event in self.document["events"]}
        self.assertEqual(
            set(events),
            {"pane.created", "pane.agent_detected", "pane.exited"},
        )
        self.assertTrue(all(command[-1] == "--quiet" for command in events.values()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
