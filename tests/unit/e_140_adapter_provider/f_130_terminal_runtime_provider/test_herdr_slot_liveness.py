"""Composite slot-liveness classifier tests (Redmine #13518 host-restart recovery, j#75329).

The reconciler's runtime authority: a name-matched ``agent list`` row is adoptable only when a
live agent backs it. These cover the reproduced reboot residue (name survives, no detected
agent, ``agent_status=unknown``) classifying stale, and — critically — that a minimal / legacy
row with no liveness signal still reads live so the legitimate self-heal adopt path is unchanged.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (
    SLOT_LIVE,
    SLOT_STALE,
    classify_named_slot,
)


class ClassifyNamedSlotTest(unittest.TestCase):
    def test_reboot_residue_unknown_status_no_agent_is_stale(self) -> None:
        # The reproduced host-restart shape (j#75328): durable name survives, foreground is a
        # bare `-zsh`, no detected agent, agent_status reports unknown.
        row = {"name": "mzb1_ws_codex_lane-1", "pane_id": "w19:p3", "agent_status": "unknown"}
        self.assertEqual(classify_named_slot(row), SLOT_STALE)

    def test_detected_agent_field_present_but_blank_is_stale(self) -> None:
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                row = {"name": "mzb1_ws_claude_lane-1", "pane_id": "w19:p4", "agent": blank}
                self.assertEqual(classify_named_slot(row), SLOT_STALE)

    def test_live_detected_agent_is_live(self) -> None:
        row = {"name": "mzb1_ws_codex_lane-1", "pane_id": "w19:pC", "agent": "codex"}
        self.assertEqual(classify_named_slot(row), SLOT_LIVE)

    def test_detected_agent_overrides_unknown_status(self) -> None:
        # A positively detected provider is not clobbered by a transient unknown status read.
        row = {"name": "n", "pane_id": "w1:p1", "agent": "claude", "agent_status": "unknown"}
        self.assertEqual(classify_named_slot(row), SLOT_LIVE)

    def test_recognised_live_status_is_live(self) -> None:
        for status in ("idle", "working", "blocked", "done"):
            with self.subTest(status=status):
                row = {"name": "n", "pane_id": "w1:p1", "agent_status": status}
                self.assertEqual(classify_named_slot(row), SLOT_LIVE)

    def test_alternate_status_keys_are_read(self) -> None:
        self.assertEqual(classify_named_slot({"name": "n", "status": "unknown"}), SLOT_STALE)
        self.assertEqual(classify_named_slot({"name": "n", "state": "unknown"}), SLOT_STALE)
        self.assertEqual(classify_named_slot({"name": "n", "status": "idle"}), SLOT_LIVE)

    def test_minimal_legacy_row_with_no_liveness_signal_is_live(self) -> None:
        # Backward compatibility: a row that carries neither a detected-agent nor a status field
        # adopts unchanged (the pre-#13518 self-heal path must stay byte-for-byte).
        self.assertEqual(
            classify_named_slot({"name": "mzb1_ws_codex_lane-1", "pane_id": "w1:p1"}), SLOT_LIVE
        )


if __name__ == "__main__":
    unittest.main()
