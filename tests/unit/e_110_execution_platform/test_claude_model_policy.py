"""Managed Claude launch-model flag policy (Redmine #13155).

Covers the pure flag renderer in ``domain.claude_model_policy``:

- a Claude pane with a valid token renders `` --model <token>`` (leading space,
  the same concat convention as the permission-mode flag);
- a Codex pane always renders ``""`` (Claude-only);
- ``model=None`` renders ``""`` (the historical launch command, byte-for-byte);
- an invalid token (space / empty / shell metachar / flag-shaped / non-string)
  raises ``InvalidClaudeModel`` so a typo / injection cannot reach a launch
  command.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_model_policy import (
    InvalidClaudeModel,
    claude_model_flag,
)


class ClaudeModelFlagTest(unittest.TestCase):
    def test_claude_valid_token_renders_flag(self) -> None:
        self.assertEqual(
            " --model claude-opus-4-8",
            claude_model_flag("claude", "claude-opus-4-8"),
        )
        self.assertEqual(" --model sonnet", claude_model_flag("claude", "sonnet"))

    def test_codex_never_gets_a_flag(self) -> None:
        self.assertEqual("", claude_model_flag("codex", "claude-opus-4-8"))

    def test_none_model_renders_nothing(self) -> None:
        self.assertEqual("", claude_model_flag("claude", None))
        self.assertEqual("", claude_model_flag("codex", None))

    def test_invalid_token_raises(self) -> None:
        for bad in ("", "   ", "opus 4", "opus;rm", "-model", "a b", "$(x)"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidClaudeModel):
                    claude_model_flag("claude", bad)

    def test_non_string_token_raises(self) -> None:
        for bad in (5, True, ["sonnet"]):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidClaudeModel):
                    claude_model_flag("claude", bad)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
