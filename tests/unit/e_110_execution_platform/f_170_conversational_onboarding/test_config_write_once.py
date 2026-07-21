"""Typed write-once config tool (Redmine #13498)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.config_write import (
    CONFIG_WRITE_CREATED,
    CONFIG_WRITE_NO_OP,
    ConfigWriteError,
    write_once_config,
)


class ConfigWriteOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self) -> Path:
        return self.root / ".mozyo-bridge" / "config.yaml"

    def test_absent_creates_typed_record(self) -> None:
        result = write_once_config(self.root)
        self.assertEqual(result.outcome, CONFIG_WRITE_CREATED)
        parsed = yaml.safe_load(self._config().read_text(encoding="utf-8"))
        self.assertEqual(parsed, {"version": 1, "terminal_transport": {"backend": "herdr"}})

    def test_second_write_is_no_op(self) -> None:
        write_once_config(self.root)
        result = write_once_config(self.root)
        self.assertEqual(result.outcome, CONFIG_WRITE_NO_OP)

    def test_equivalent_without_version_is_no_op(self) -> None:
        cfg = self._config()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("terminal_transport:\n  backend: herdr\n", encoding="utf-8")
        result = write_once_config(self.root)
        self.assertEqual(result.outcome, CONFIG_WRITE_NO_OP)

    def test_divergent_config_fails_closed(self) -> None:
        cfg = self._config()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("version: 1\nproviders:\n  redmine: {}\n", encoding="utf-8")
        with self.assertRaises(ConfigWriteError) as ctx:
            write_once_config(self.root)
        self.assertEqual(ctx.exception.code, "existing_config_requires_separate_merge")
        # The divergent config was not overwritten.
        self.assertIn("providers", cfg.read_text(encoding="utf-8"))

    def test_different_backend_fails_closed(self) -> None:
        cfg = self._config()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("terminal_transport:\n  backend: tmux\n", encoding="utf-8")
        with self.assertRaises(ConfigWriteError) as ctx:
            write_once_config(self.root)
        self.assertEqual(ctx.exception.code, "existing_config_requires_separate_merge")

    def test_unreadable_config_fails_closed(self) -> None:
        cfg = self._config()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("this: : : not valid yaml: [\n", encoding="utf-8")
        with self.assertRaises(ConfigWriteError) as ctx:
            write_once_config(self.root)
        self.assertEqual(ctx.exception.code, "existing_config_unreadable")


if __name__ == "__main__":
    unittest.main()
