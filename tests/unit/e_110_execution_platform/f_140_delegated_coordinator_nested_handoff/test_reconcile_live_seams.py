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
    latest_dispatch_journal_from_entries,
)


class DispatchAnchorTest(unittest.TestCase):
    """review R4-F3: the exact dispatch anchor is the latest implementation_request handoff journal."""

    def _entry(self, jid, kind, anchor_journal):
        return RedmineJournalEntry(
            issue_id="13758",
            journal_id=str(jid),
            notes=(
                f"## Gate: Implementation Request\n"
                f"[mozyo:handoff:source=redmine:issue=13758:journal={anchor_journal}:"
                f"kind={kind}:to=claude]"
            ),
        )

    def test_latest_implementation_request_journal_is_the_anchor(self):
        entries = [
            self._entry("78056", "implementation_request", "78056"),
            self._entry("79337", "implementation_request", "79337"),  # a later re-dispatch
            self._entry("79340", "review_request", "79340"),  # not a dispatch kind
        ]
        self.assertEqual(latest_dispatch_journal_from_entries(entries), "79337")

    def test_no_dispatch_marker_yields_blank(self):
        entries = [self._entry("79340", "review_request", "79340")]
        self.assertEqual(latest_dispatch_journal_from_entries(entries), "")

    def test_prose_note_is_ignored(self):
        entries = [
            RedmineJournalEntry(issue_id="13758", journal_id="1", notes="just prose, no marker"),
            self._entry("79337", "implementation_request", "79337"),
        ]
        self.assertEqual(latest_dispatch_journal_from_entries(entries), "79337")


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
