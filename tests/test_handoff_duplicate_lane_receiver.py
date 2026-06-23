"""Handoff rollback residual prompt + duplicate same-lane receiver consistency (#12229).

Source incident: #12226 j#61221 / j#61224 / j#61228 / j#61249. During a same-lane
Codex -> Claude dispatch a cockpit gateway repair (#12226 j#61213) left TWO live
same-lane Claude panes (`%14` and `%16`). The records then diverged: the delivery
record named `%14` as receiver (j#61224) while Implementation Done named `%16` as
actor (j#61228), and an earlier failed `--mode standard` send left residual prompt
text in `%16` despite the `C-u` rollback.

These tests pin two boundaries, both consistent with
``vibes/docs/logics/tmux-send-safety-contract.md``:

1. **Residual prompt after a strict `marker_timeout` rollback** — the standard rail
   still issues `C-u`, does NOT press Enter, and the durable narrative claims only
   that a `C-u` rollback was issued / the composer clearing is unverifiable (the
   #12188 wording, kept honest here). Residual prompt text is the expected
   ``rolled_back`` model, not a "cleared" claim.
2. **Duplicate same-lane receiver pane surfacing** — when a live same-lane pane
   resolves to the same receiver role as the resolved target, the durable delivery
   record names it as a duplicate so the receiver pane and any stale-input
   duplicate stay both visible and the receiver/actor record cannot silently
   diverge. This is diagnostic, never a block: an explicit ``--target %pane`` is the
   documented escape hatch and queue-enter's Step 11 active-split gate already
   fail-closes the inactive duplicate.

All hermetic: ``pane_lines`` / ``current_session_name`` / ``validate_target`` and
the typing seams are patched; no live tmux server is required.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain import pane_resolver
from mozyo_bridge.domain.pane_resolver import (
    duplicate_pane_record_row,
    same_lane_receiver_duplicates,
)
from mozyo_bridge.domain.handoff import (
    MODE_STANDARD,
    build_delivery_record,
    make_outcome,
    normalize_anchor,
)


def _pane(
    pane_id,
    *,
    agent_role="",
    workspace_id="",
    lane_id="",
    lane_label="",
    window_name="claude",
    command="claude",
    cwd="/repo",
    pane_active="1",
    location=None,
):
    return {
        "id": pane_id,
        "location": location or f"mozyo-cockpit:0{pane_id[-1]}",
        "command": command,
        "cwd": cwd,
        "window_name": window_name,
        "pane_active": pane_active,
        "agent_role": agent_role,
        "workspace_id": workspace_id,
        "lane_id": lane_id,
        "lane_label": lane_label,
    }


# Two live same-lane Claude panes: the #12226 j#61213 shape after a cockpit
# gateway repair. `%14` is the receiver of a send; `%16` is the inactive
# duplicate that held residual text from earlier failed sends.
CLAUDE_14 = _pane(
    "%14",
    agent_role="claude",
    workspace_id="ws-12226",
    lane_id="lane-369a873af1b2",
    lane_label="issue_12226",
    command="claude",
    pane_active="1",
)
CLAUDE_16 = _pane(
    "%16",
    agent_role="claude",
    workspace_id="ws-12226",
    lane_id="lane-369a873af1b2",
    lane_label="issue_12226",
    command="claude",
    pane_active="0",
)
# Same-lane Codex gateway (role=codex). Must NOT be treated as a duplicate
# Claude receiver even though it shares the lane and runs a node-based CLI.
CODEX_15 = _pane(
    "%15",
    agent_role="codex",
    workspace_id="ws-12226",
    lane_id="lane-369a873af1b2",
    lane_label="issue_12226",
    command="codex",
    window_name="codex",
    pane_active="0",
)


class SameLaneReceiverDuplicatesUnitTest(unittest.TestCase):
    def test_detects_same_lane_same_receiver_duplicate(self) -> None:
        dupes = same_lane_receiver_duplicates(
            CLAUDE_14, [CLAUDE_14, CLAUDE_16, CODEX_15], "claude"
        )
        self.assertEqual(["%16"], [p["id"] for p in dupes])

    def test_excludes_the_target_pane_itself(self) -> None:
        dupes = same_lane_receiver_duplicates(CLAUDE_14, [CLAUDE_14], "claude")
        self.assertEqual([], dupes)

    def test_excludes_same_lane_codex_gateway(self) -> None:
        # receiver=claude must not flag the same-lane codex gateway as a
        # duplicate (role mismatch), even though it shares (workspace, lane).
        dupes = same_lane_receiver_duplicates(
            CLAUDE_14, [CLAUDE_14, CODEX_15], "claude"
        )
        self.assertEqual([], dupes)

    def test_excludes_different_lane(self) -> None:
        other_lane = _pane(
            "%20",
            agent_role="claude",
            workspace_id="ws-12226",
            lane_id="lane-other",
            command="claude",
        )
        dupes = same_lane_receiver_duplicates(
            CLAUDE_14, [CLAUDE_14, other_lane], "claude"
        )
        self.assertEqual([], dupes)

    def test_excludes_different_workspace(self) -> None:
        other_ws = _pane(
            "%30",
            agent_role="claude",
            workspace_id="ws-other",
            lane_id="lane-369a873af1b2",
            command="claude",
        )
        dupes = same_lane_receiver_duplicates(
            CLAUDE_14, [CLAUDE_14, other_ws], "claude"
        )
        self.assertEqual([], dupes)

    def test_returns_empty_when_target_has_no_concrete_lane_identity(self) -> None:
        # A default-lane / no-workspace target has nothing to disambiguate
        # against, matching `_has_concrete_lane_identity`'s fail-closed posture.
        bare = _pane("%1", agent_role="claude", command="claude")
        sibling = _pane("%2", agent_role="claude", command="claude")
        self.assertEqual(
            [], same_lane_receiver_duplicates(bare, [bare, sibling], "claude")
        )

    def test_unknown_receiver_returns_empty(self) -> None:
        self.assertEqual(
            [],
            same_lane_receiver_duplicates(CLAUDE_14, [CLAUDE_14, CLAUDE_16], "bogus"),
        )

    def test_record_row_carries_identity_without_absolute_paths(self) -> None:
        # The durable record is pasted into Redmine; the row must carry the
        # disambiguating identity (pane id, active split, lane) but never the
        # absolute cwd / repo_root (public-private boundary).
        secret = _pane(
            "%16",
            agent_role="claude",
            workspace_id="ws-12226",
            lane_id="lane-369a873af1b2",
            lane_label="issue_12226",
            command="claude",
            cwd="/workspace/project-alpha/private/checkout",
            pane_active="0",
        )
        row = duplicate_pane_record_row(secret)
        self.assertIn("%16", row)
        self.assertIn("inactive", row)
        self.assertIn("issue_12226", row)
        self.assertNotIn("/workspace/", row)
        self.assertNotIn("project-alpha", row)
        self.assertNotIn("/private/", row)


class BuildDeliveryRecordDuplicateAdvisoryTest(unittest.TestCase):
    def _sent_outcome(self):
        anchor = normalize_anchor("redmine", issue="12226", journal="61219")
        return make_outcome(
            status="sent",
            reason="ok",
            receiver="claude",
            target="%14",
            anchor=anchor,
            mode=MODE_STANDARD,
            kind="implementation_request",
            notification_marker="[mozyo:handoff:...]",
        )

    def test_advisory_rendered_when_duplicates_present(self) -> None:
        record = build_delivery_record(
            self._sent_outcome(),
            duplicate_lane_panes=[
                "%16 (workspace=ws-12226, lane=issue_12226, role_source=pane_option, inactive)"
            ],
        )
        self.assertIn("Duplicate same-lane pane(s):", record)
        self.assertIn("%16", record)
        # The receiver of THIS send is the target pane, not the duplicate.
        self.assertIn("the receiver is `%14`", record)
        # Residual-text caveat ties the duplicate to the rollback model.
        self.assertIn("residual prompt text", record)
        self.assertIn("do not diverge", record)

    def test_no_advisory_when_absent(self) -> None:
        record = build_delivery_record(self._sent_outcome())
        self.assertNotIn("Duplicate same-lane pane(s):", record)
        record_empty = build_delivery_record(
            self._sent_outcome(), duplicate_lane_panes=[]
        )
        self.assertNotIn("Duplicate same-lane pane(s):", record_empty)


class _OrchestratorHarness(unittest.TestCase):
    """Drive the wired `handoff send` CLI hermetically (mirror of the #12072 harness)."""

    def _run_send(self, panes, *, target, mode="standard", marker_lands=True):
        from mozyo_bridge.application import commands
        from mozyo_bridge.application.cli import build_parser

        argv = [
            "handoff", "send", "--to", "claude",
            "--source", "redmine", "--issue", "12226", "--journal", "61219",
            "--kind", "implementation_request",
            "--target", target,
            "--mode", mode,
            "--landing-timeout", "0.01", "--submit-delay", "0",
            "--summary", "characterization fixture",
        ]
        args = build_parser().parse_args(argv)

        sent: list[tuple] = []

        def fake_run_tmux(*a, check: bool = True):
            if a[:2] == ("send-keys", "-t"):
                sent.append(a)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
        env["TMUX_PANE"] = "%15"  # sender = same-lane codex gateway

        with patch.object(commands, "require_tmux"), \
            patch.object(commands, "capture_pane", return_value=""), \
            patch.object(commands, "run_tmux", side_effect=fake_run_tmux), \
            patch.object(commands, "wait_for_text", return_value=marker_lands), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch.object(commands, "current_session_name", return_value="mozyo-cockpit"), \
            patch("mozyo_bridge.domain.pane_resolver.current_session_name", return_value="mozyo-cockpit"), \
            patch("mozyo_bridge.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes), \
            patch.dict(os.environ, env, clear=True), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()) as err:
            with contextlib.suppress(SystemExit):
                args.func(args)
        return out.getvalue(), err.getvalue(), sent


class OrchestratorDuplicateSurfacingTest(_OrchestratorHarness):
    def test_sent_record_names_same_lane_duplicate(self) -> None:
        # Explicit-target standard send to %14 with %16 alive in the same lane:
        # the send succeeds, and the durable record names %16 as a duplicate so
        # the receiver/actor record cannot silently diverge.
        stdout, _stderr, _sent = self._run_send(
            [CLAUDE_14, CLAUDE_16, CODEX_15], target="%14", marker_lands=True
        )
        self.assertIn("Duplicate same-lane pane(s):", stdout)
        self.assertIn("%16", stdout)
        # The single-pane case (no duplicate) must NOT emit the advisory.
        stdout_solo, _e, _s = self._run_send(
            [CLAUDE_14, CODEX_15], target="%14", marker_lands=True
        )
        self.assertNotIn("Duplicate same-lane pane(s):", stdout_solo)

    def test_marker_timeout_residual_model_and_duplicate_surfaced(self) -> None:
        # Strict marker_timeout: C-u issued, Enter never pressed, narrative
        # claims only that clearing is unverifiable (the #12188 honest wording),
        # AND the same-lane duplicate is surfaced because residual prompt text
        # can linger in it after the rollback.
        stdout, stderr, sent = self._run_send(
            [CLAUDE_14, CLAUDE_16, CODEX_15], target="%14", marker_lands=False
        )
        self.assertIn(("send-keys", "-t", "%14", "C-u"), sent)
        self.assertNotIn(("send-keys", "-t", "%14", "Enter"), sent)

        outcome = None
        for line in stdout.splitlines():
            if line.strip().startswith("{"):
                outcome = json.loads(line)
        self.assertIsNotNone(outcome)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("marker_timeout", outcome["reason"])

        # #12450 supersedes the #12188 "cannot verify" wording: marker_timeout now
        # means the C-u rollback was re-captured and the composer no longer shows
        # the marker (rollback verified); Enter is still not pressed.
        self.assertIn("C-u rollback was issued", stdout)
        self.assertIn("Enter was not pressed", stdout)
        self.assertIn("rollback verified", stdout)
        # Duplicate surfaced on the rollback path (residual-text home).
        self.assertIn("Duplicate same-lane pane(s):", stdout)
        self.assertIn("%16", stdout)


if __name__ == "__main__":
    unittest.main()
