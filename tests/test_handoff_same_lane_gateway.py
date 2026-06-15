"""Same-lane Codex gateway auto-resolution (Redmine #12011).

A cockpit session that hosts several workspace / lane Codex panes used to fail
closed on a bare ``--to codex`` (no explicit ``--target``): ``find_agent_window``
saw more than one Codex-role pane and died, forcing the operator to hand-pick a
``%pane``. These tests pin the additive same-lane narrowing: the sender's own
``(workspace_id, lane_id)`` resolves a unique same-lane gateway, while every
ambiguous / cross-lane / identity-less case stays fail-closed with concrete
candidate guidance. No live tmux server is required — ``pane_lines`` and the
sender ``TMUX_PANE`` are patched.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.pane_resolver import (  # noqa: E402
    find_agent_window,
    narrow_to_sender_lane,
)


def _pane(
    pane_id,
    location,
    *,
    agent_role="",
    workspace_id="",
    lane_id="",
    lane_label="",
    window_name="cockpit",
    command="node",
    cwd="/repo",
    pane_active="1",
):
    return {
        "id": pane_id,
        "location": location,
        "command": command,
        "cwd": cwd,
        "window_name": window_name,
        "pane_active": pane_active,
        "agent_role": agent_role,
        "workspace_id": workspace_id,
        "lane_id": lane_id,
        "lane_label": lane_label,
    }


# Sender Claude pane in the issue_12011 lane; its same-lane Codex gateway is
# %1071. The other Codex panes are foreign lanes / workspaces in one cockpit.
SENDER_CLAUDE = _pane(
    "%1072",
    "mozyo-cockpit:0.7",
    agent_role="claude",
    workspace_id="ws-mozyo-bridge",
    lane_id="lane-12011",
    lane_label="issue_12011",
)
SAME_LANE_CODEX = _pane(
    "%1071",
    "mozyo-cockpit:0.6",
    agent_role="codex",
    workspace_id="ws-mozyo-bridge",
    lane_id="lane-12011",
    lane_label="issue_12011",
)
MAIN_CODEX = _pane(
    "%953",
    "mozyo-cockpit:0.1",
    agent_role="codex",
    workspace_id="ws-mozyo-bridge",
    lane_id="",  # main coordinator -> default lane
    lane_label="",
)
SUBLANE_12007_CODEX = _pane(
    "%1069",
    "mozyo-cockpit:0.5",
    agent_role="codex",
    workspace_id="ws-mozyo-bridge",
    lane_id="lane-12007",
    lane_label="issue_12007",
)
FOREIGN_WS_CODEX = _pane(
    "%1063",
    "mozyo-cockpit:0.3",
    agent_role="codex",
    workspace_id="ws-cloud-drive",
    lane_id="lane-3500",
    lane_label="3500_drive",
)


@contextlib.contextmanager
def _runtime(panes, *, sender_pane_id):
    env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
    if sender_pane_id is not None:
        env["TMUX_PANE"] = sender_pane_id
    with patch(
        "mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes
    ), patch.dict(os.environ, env, clear=True):
        yield


class NarrowToSenderLaneTest(unittest.TestCase):
    def test_same_workspace_same_lane_unique(self) -> None:
        targets = [SAME_LANE_CODEX, MAIN_CODEX, SUBLANE_12007_CODEX, FOREIGN_WS_CODEX]
        narrowed = narrow_to_sender_lane(targets, SENDER_CLAUDE)
        self.assertEqual(["%1071"], [pane["id"] for pane in narrowed])

    def test_cross_lane_panes_are_excluded(self) -> None:
        # The sublane #12007 Codex shares the workspace but not the lane: it must
        # not be selected for the issue_12011 sender.
        targets = [SUBLANE_12007_CODEX, MAIN_CODEX]
        self.assertEqual([], narrow_to_sender_lane(targets, SENDER_CLAUDE))

    def test_cross_workspace_default_lane_is_isolated_by_workspace(self) -> None:
        # Two `default`-lane Codex panes in different workspaces: a sender whose
        # only identity is its workspace still narrows to its own workspace.
        sender = _pane(
            "%900",
            "mozyo-cockpit:0.9",
            agent_role="claude",
            workspace_id="ws-mozyo-bridge",
            lane_id="",
        )
        foreign_default = _pane(
            "%200",
            "mozyo-cockpit:0.2",
            agent_role="codex",
            workspace_id="ws-cloud-drive",
            lane_id="",
        )
        targets = [MAIN_CODEX, foreign_default]
        narrowed = narrow_to_sender_lane(targets, sender)
        self.assertEqual(["%953"], [pane["id"] for pane in narrowed])

    def test_sender_without_concrete_identity_returns_targets_unchanged(self) -> None:
        # No workspace marker + default lane: nothing to disambiguate on, so the
        # caller keeps its fail-closed behavior (targets returned unchanged).
        sender = _pane("%5", "mozyo-cockpit:0.5", agent_role="claude")
        targets = [MAIN_CODEX, SUBLANE_12007_CODEX]
        self.assertEqual(targets, narrow_to_sender_lane(targets, sender))

    def test_sender_none_returns_targets_unchanged(self) -> None:
        targets = [MAIN_CODEX, SUBLANE_12007_CODEX]
        self.assertEqual(targets, narrow_to_sender_lane(targets, None))


class FindAgentWindowSameLaneTest(unittest.TestCase):
    def test_same_lane_codex_auto_resolves_without_explicit_target(self) -> None:
        panes = [
            SENDER_CLAUDE,
            SAME_LANE_CODEX,
            MAIN_CODEX,
            SUBLANE_12007_CODEX,
            FOREIGN_WS_CODEX,
        ]
        with _runtime(panes, sender_pane_id="%1072"):
            pane = find_agent_window("codex", "mozyo-cockpit")
        self.assertIsNotNone(pane)
        self.assertEqual("%1071", pane["id"])

    def test_single_candidate_still_resolves(self) -> None:
        # One Codex in the session: narrowing never runs, classic behavior holds.
        panes = [SENDER_CLAUDE, SAME_LANE_CODEX]
        with _runtime(panes, sender_pane_id="%1072"):
            pane = find_agent_window("codex", "mozyo-cockpit")
        self.assertEqual("%1071", pane["id"])

    def test_sender_lane_without_matching_codex_fails_closed(self) -> None:
        # Sender is in lane-12011 but that lane has no Codex pane; do NOT fall
        # back to a foreign lane's Codex.
        panes = [SENDER_CLAUDE, MAIN_CODEX, SUBLANE_12007_CODEX, FOREIGN_WS_CODEX]
        err = io.StringIO()
        with _runtime(panes, sender_pane_id="%1072"):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    find_agent_window("codex", "mozyo-cockpit")
        message = err.getvalue()
        self.assertIn("no unique same-lane", message)
        # Candidates and the explicit-target retry hint are surfaced.
        self.assertIn("%953", message)
        self.assertIn("--target %pane", message)

    def test_unknown_sender_fails_closed_with_candidates(self) -> None:
        # Sender pane is not in the snapshot (e.g. run from outside the lane):
        # cannot narrow, so fail closed naming the candidates.
        panes = [SAME_LANE_CODEX, MAIN_CODEX, FOREIGN_WS_CODEX]
        err = io.StringIO()
        with _runtime(panes, sender_pane_id="%does-not-exist"):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    find_agent_window("codex", "mozyo-cockpit")
        message = err.getvalue()
        self.assertIn("sender pane is unknown", message)
        self.assertIn("--target %pane", message)

    def test_sender_without_identity_fails_closed(self) -> None:
        # Sender carries no workspace/lane identity: narrowing stays off and the
        # multi-candidate resolution fails closed with the reason.
        plain_sender = _pane("%50", "mozyo-cockpit:0.50", agent_role="claude")
        panes = [plain_sender, MAIN_CODEX, SUBLANE_12007_CODEX]
        err = io.StringIO()
        with _runtime(panes, sender_pane_id="%50"):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    find_agent_window("codex", "mozyo-cockpit")
        message = err.getvalue()
        self.assertIn("no workspace/lane identity", message)

    def test_cross_lane_claude_resolves_only_same_lane(self) -> None:
        # The same narrowing protects `--to claude`: a sender Codex in lane-12011
        # resolves to its own lane's Claude, never another lane's Claude.
        sender_codex = SAME_LANE_CODEX
        other_lane_claude = _pane(
            "%1080",
            "mozyo-cockpit:0.8",
            agent_role="claude",
            workspace_id="ws-mozyo-bridge",
            lane_id="lane-12007",
            lane_label="issue_12007",
        )
        panes = [sender_codex, SENDER_CLAUDE, other_lane_claude]
        with _runtime(panes, sender_pane_id="%1071"):
            pane = find_agent_window("claude", "mozyo-cockpit")
        self.assertEqual("%1072", pane["id"])


if __name__ == "__main__":
    unittest.main()
