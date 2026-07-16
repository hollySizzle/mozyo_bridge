"""Unit tests for the reconcile live-source seams (Redmine #13758 review R4-F1 / R4-F3).

Pins the two production live-source readers with production-shape inputs (no live inventory /
Redmine): the exact dispatch anchor from the raw handoff markers, and the expected owner's
runtime match from the observed-agent inventory.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.reconcile_live_source import (
    lane_worker_runtime,
    match_lane_worker_runtime,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    render_dispatch_note,
    resolve_dispatch_entry_journal,
)


class DispatchAnchorTest(unittest.TestCase):
    """j#79507 Q2: the anchor is the OWNING entry journal id of the current IR dispatch marker."""

    def _ir(self, jid, *, lane, generation):
        # A canonical IR journal: prose body + the embedded dispatch marker (the writer).
        return RedmineJournalEntry(
            issue_id="13758",
            journal_id=str(jid),
            notes=render_dispatch_note(
                "## Gate: Implementation Request\nbody...", lane=lane, lane_generation=generation
            ),
        )

    def test_owning_entry_journal_is_the_anchor(self):
        # The anchor is the entry's OWN journal id (79337), NOT any self-reported field.
        entries = [self._ir("79337", lane="lane-a", generation=1)]
        self.assertEqual(
            resolve_dispatch_entry_journal(entries, lane="lane-a", lane_generation=1), "79337"
        )

    def test_legacy_prose_only_ir_is_fail_closed_blank(self):
        # review R5-F3: a real prose-only IR (no marker) is NEVER guessed -> blank.
        entries = [
            RedmineJournalEntry(
                issue_id="13758", journal_id="78056",
                notes="## Gate: Implementation Request — event-driven reconciliation state machine",
            )
        ]
        self.assertEqual(
            resolve_dispatch_entry_journal(entries, lane="lane-a", lane_generation=1), ""
        )

    def test_generation_or_lane_mismatch_is_blank(self):
        entries = [self._ir("79337", lane="lane-a", generation=1)]
        self.assertEqual(
            resolve_dispatch_entry_journal(entries, lane="lane-a", lane_generation=2), ""
        )
        self.assertEqual(
            resolve_dispatch_entry_journal(entries, lane="lane-b", lane_generation=1), ""
        )

    def test_two_distinct_entries_same_generation_is_ambiguous_blank(self):
        # A foreign / duplicate structured dispatch for the same generation -> zero-send.
        entries = [
            self._ir("79337", lane="lane-a", generation=1),
            self._ir("79999", lane="lane-a", generation=1),
        ]
        self.assertEqual(
            resolve_dispatch_entry_journal(entries, lane="lane-a", lane_generation=1), ""
        )

    def test_same_entry_reread_dedups_to_one(self):
        ir = self._ir("79337", lane="lane-a", generation=1)
        self.assertEqual(
            resolve_dispatch_entry_journal([ir, ir], lane="lane-a", lane_generation=1), "79337"
        )

    def test_new_ir_journal_new_generation_is_fresh_identity(self):
        entries = [
            self._ir("79337", lane="lane-a", generation=1),
            self._ir("80001", lane="lane-a", generation=2),  # a new-generation re-dispatch
        ]
        self.assertEqual(
            resolve_dispatch_entry_journal(entries, lane="lane-a", lane_generation=2), "80001"
        )


@dataclass(frozen=True)
class _Agent:
    workspace_id: str
    lane_id: str
    role: str
    runtime_state: str


class RuntimeMatchTest(unittest.TestCase):
    """review R4-F1: the expected owner's runtime is matched by (workspace, lane, provider)."""

    def _agents(self):
        return [
            _Agent("ws1", "lane-a", "claude", "turn_ended"),  # the worker
            _Agent("ws1", "lane-a", "codex", "busy"),  # the gateway
            _Agent("ws2", "lane-a", "claude", "busy"),  # a different workspace
        ]

    def test_matches_worker_provider(self):
        self.assertEqual(
            match_lane_worker_runtime(
                self._agents(), workspace_id="ws1", lane_id="lane-a", provider="claude"
            ),
            "turn_ended",
        )

    def test_matches_gateway_provider(self):
        self.assertEqual(
            match_lane_worker_runtime(
                self._agents(), workspace_id="ws1", lane_id="lane-a", provider="codex"
            ),
            "busy",
        )

    def test_no_match_is_blank(self):
        self.assertEqual(
            match_lane_worker_runtime(
                self._agents(), workspace_id="ws9", lane_id="lane-a", provider="claude"
            ),
            "",
        )

    def test_ambiguous_two_matches_fail_closed_blank(self):
        # review R5-F2: a duplicate / replacement overlap (two matching agents) must NOT resolve
        # to one runtime by iteration order and fabricate an edge -> blank (no edge).
        dupes = [
            _Agent("ws1", "lane-a", "claude", "turn_ended"),
            _Agent("ws1", "lane-a", "claude", "busy"),  # overlapping duplicate
        ]
        self.assertEqual(
            match_lane_worker_runtime(
                dupes, workspace_id="ws1", lane_id="lane-a", provider="claude"
            ),
            "",
        )

    def test_lane_worker_runtime_resolves_owner_role_to_provider(self):
        # the worker role -> claude slot's runtime, via an injected agents_fn (no live inventory).
        rt = lane_worker_runtime(
            "ws1", "lane-a", "implementation_worker", agents_fn=self._agents
        )
        self.assertEqual(rt, "turn_ended")
        # the gateway role -> codex slot.
        rt2 = lane_worker_runtime(
            "ws1", "lane-a", "implementation_gateway", agents_fn=self._agents
        )
        self.assertEqual(rt2, "busy")

    def test_lane_worker_runtime_fail_closed_on_reader_error(self):
        def boom():
            raise RuntimeError("herdr down")

        self.assertEqual(
            lane_worker_runtime("ws1", "lane-a", "implementation_worker", agents_fn=boom), ""
        )


if __name__ == "__main__":
    unittest.main()
