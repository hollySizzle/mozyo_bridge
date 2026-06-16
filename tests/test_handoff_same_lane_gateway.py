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
    narrow_to_local_claude,
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


def _fake_infer_repo_root(roots):
    """Patchable :func:`infer_repo_root` mapping a pane cwd to an inferred root.

    ``roots`` maps a cwd string to the repo root it infers (or ``None``). A cwd
    absent from the map infers ``None`` (no marker reachable), matching the real
    resolver's permissive behavior.
    """

    def _infer(cwd):
        return roots.get(cwd)

    return _infer


class NarrowToLocalClaudeTest(unittest.TestCase):
    """Same-session local Claude auto-select narrowing (Redmine #12070)."""

    SENDER = _pane(
        "%900",
        "mozyo-cockpit:0.9",
        agent_role="codex",
        workspace_id="ws-it-donyu",
        lane_id="lane-main",
        cwd="/ws/it-donyu",
    )

    def test_same_workspace_lane_and_repo_is_unique(self) -> None:
        local_claude = _pane(
            "%901",
            "mozyo-cockpit:0.1",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu/app",
        )
        foreign = _pane(
            "%902",
            "mozyo-cockpit:0.2",
            agent_role="claude",
            workspace_id="ws-other",
            lane_id="lane-x",
            cwd="/ws/other",
        )
        roots = {
            "/ws/it-donyu": "/ws/it-donyu",
            "/ws/it-donyu/app": "/ws/it-donyu",
            "/ws/other": "/ws/other",
        }
        with patch(
            "mozyo_bridge.domain.pane_resolver.infer_repo_root",
            _fake_infer_repo_root(roots),
        ):
            narrowed = narrow_to_local_claude([local_claude, foreign], self.SENDER)
        self.assertEqual(["%901"], [pane["id"] for pane in narrowed])

    def test_same_lane_different_repo_root_is_excluded(self) -> None:
        # Two Claude panes share the sender's (workspace_id, lane_id) but live in
        # different repo checkouts: only the sender's own repo survives.
        same_repo = _pane(
            "%901",
            "mozyo-cockpit:0.1",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu/app",
        )
        other_repo = _pane(
            "%903",
            "mozyo-cockpit:0.3",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu-clone",
        )
        roots = {
            "/ws/it-donyu": "/ws/it-donyu",
            "/ws/it-donyu/app": "/ws/it-donyu",
            "/ws/it-donyu-clone": "/ws/it-donyu-clone",
        }
        with patch(
            "mozyo_bridge.domain.pane_resolver.infer_repo_root",
            _fake_infer_repo_root(roots),
        ):
            narrowed = narrow_to_local_claude([same_repo, other_repo], self.SENDER)
        self.assertEqual(["%901"], [pane["id"] for pane in narrowed])

    def test_non_git_workspace_falls_back_to_workspace_identity(self) -> None:
        # Neither cwd infers a repo root: the shared registered workspace_id
        # carries the match so a non-git scaffolded workspace still resolves.
        local_claude = _pane(
            "%901",
            "mozyo-cockpit:0.1",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu/docs",
        )
        with patch(
            "mozyo_bridge.domain.pane_resolver.infer_repo_root",
            _fake_infer_repo_root({}),
        ):
            narrowed = narrow_to_local_claude([local_claude], self.SENDER)
        self.assertEqual(["%901"], [pane["id"] for pane in narrowed])

    def test_one_sided_repo_root_is_fail_closed(self) -> None:
        # Sender infers a repo root but the candidate does not (or vice versa):
        # identity cannot be established -> drop the candidate.
        candidate = _pane(
            "%901",
            "mozyo-cockpit:0.1",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu/app",
        )
        roots = {"/ws/it-donyu": "/ws/it-donyu"}  # candidate cwd infers None
        with patch(
            "mozyo_bridge.domain.pane_resolver.infer_repo_root",
            _fake_infer_repo_root(roots),
        ):
            narrowed = narrow_to_local_claude([candidate], self.SENDER)
        self.assertEqual([], narrowed)

    def test_sender_without_workspace_id_returns_unchanged(self) -> None:
        # No machine-checkable workspace identity: do not narrow, leave the
        # caller's fail-closed handling to fire.
        plain_sender = _pane("%50", "mozyo-cockpit:0.50", agent_role="codex")
        candidate = _pane(
            "%901",
            "mozyo-cockpit:0.1",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
        )
        targets = [candidate]
        self.assertEqual(targets, narrow_to_local_claude(targets, plain_sender))

    def test_sender_none_returns_unchanged(self) -> None:
        targets = [self.SENDER]
        self.assertEqual(targets, narrow_to_local_claude(targets, None))


class FindAgentWindowLocalClaudeTest(unittest.TestCase):
    """`find_agent_window('claude', ...)` end-to-end with the repo gate (#12070)."""

    def _sender_and_panes(self):
        sender = _pane(
            "%900",
            "mozyo-cockpit:0.9",
            agent_role="codex",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu",
        )
        local_claude = _pane(
            "%901",
            "mozyo-cockpit:0.1",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu/app",
        )
        return sender, local_claude

    def test_same_lane_diff_repo_auto_resolves_local_claude(self) -> None:
        sender, local_claude = self._sender_and_panes()
        clone_claude = _pane(
            "%903",
            "mozyo-cockpit:0.3",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu-clone",
        )
        roots = {
            "/ws/it-donyu": "/ws/it-donyu",
            "/ws/it-donyu/app": "/ws/it-donyu",
            "/ws/it-donyu-clone": "/ws/it-donyu-clone",
        }
        panes = [sender, local_claude, clone_claude]
        with _runtime(panes, sender_pane_id="%900"), patch(
            "mozyo_bridge.domain.pane_resolver.infer_repo_root",
            _fake_infer_repo_root(roots),
        ):
            pane = find_agent_window("claude", "mozyo-cockpit")
        self.assertEqual("%901", pane["id"])

    def test_unrelated_workspace_claude_not_selected(self) -> None:
        sender, local_claude = self._sender_and_panes()
        foreign_claude = _pane(
            "%910",
            "mozyo-cockpit:0.10",
            agent_role="claude",
            workspace_id="ws-other",
            lane_id="lane-x",
            cwd="/ws/other",
        )
        roots = {
            "/ws/it-donyu": "/ws/it-donyu",
            "/ws/it-donyu/app": "/ws/it-donyu",
            "/ws/other": "/ws/other",
        }
        panes = [sender, local_claude, foreign_claude]
        with _runtime(panes, sender_pane_id="%900"), patch(
            "mozyo_bridge.domain.pane_resolver.infer_repo_root",
            _fake_infer_repo_root(roots),
        ):
            pane = find_agent_window("claude", "mozyo-cockpit")
        self.assertEqual("%901", pane["id"])

    def test_ambiguous_same_repo_same_lane_fails_closed(self) -> None:
        # Two Claude panes share lane AND repo root: genuinely ambiguous (the
        # nested execution root is #12098, not pane selection) -> fail closed.
        sender, local_claude = self._sender_and_panes()
        sibling_claude = _pane(
            "%904",
            "mozyo-cockpit:0.4",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu/api",
        )
        roots = {
            "/ws/it-donyu": "/ws/it-donyu",
            "/ws/it-donyu/app": "/ws/it-donyu",
            "/ws/it-donyu/api": "/ws/it-donyu",
        }
        panes = [sender, local_claude, sibling_claude]
        err = io.StringIO()
        with _runtime(panes, sender_pane_id="%900"), patch(
            "mozyo_bridge.domain.pane_resolver.infer_repo_root",
            _fake_infer_repo_root(roots),
        ):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    find_agent_window("claude", "mozyo-cockpit")
        message = err.getvalue()
        self.assertIn("%901", message)
        self.assertIn("%904", message)
        self.assertIn("--target %pane", message)

    def test_ambiguity_message_surfaces_candidate_diagnostics(self) -> None:
        # Redmine #12071: the fail-closed candidate rows carry the identity an
        # operator needs to pick by hand — role source, workspace, lane, repo
        # root, cwd, active state — and the retry hint prefers the explicit pane
        # plus the auto repo-identity gate.
        sender, local_claude = self._sender_and_panes()
        sibling_claude = _pane(
            "%904",
            "mozyo-cockpit:0.4",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu/api",
            pane_active="0",
        )
        roots = {
            "/ws/it-donyu": "/ws/it-donyu",
            "/ws/it-donyu/app": "/ws/it-donyu",
            "/ws/it-donyu/api": "/ws/it-donyu",
        }
        panes = [sender, local_claude, sibling_claude]
        err = io.StringIO()
        with _runtime(panes, sender_pane_id="%900"), patch(
            "mozyo_bridge.domain.pane_resolver.infer_repo_root",
            _fake_infer_repo_root(roots),
        ):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    find_agent_window("claude", "mozyo-cockpit")
        message = err.getvalue()
        # Per-candidate diagnostics.
        self.assertIn("role_source=pane_option", message)
        self.assertIn("workspace=ws-it-donyu", message)
        self.assertIn("lane=lane-main", message)
        self.assertIn("repo_root=/ws/it-donyu", message)
        self.assertIn("cwd=/ws/it-donyu/app", message)
        self.assertIn("cwd=/ws/it-donyu/api", message)
        # Both the active and the inactive split are labelled.
        self.assertIn(", active)", message)
        self.assertIn(", inactive)", message)
        # Retry hint prefers the explicit pane + auto repo-identity gate.
        self.assertIn("--target %pane --target-repo auto", message)

    def test_sender_without_workspace_identity_fails_closed(self) -> None:
        plain_sender = _pane("%50", "mozyo-cockpit:0.50", agent_role="codex")
        _sender, local_claude = self._sender_and_panes()
        other_claude = _pane(
            "%905",
            "mozyo-cockpit:0.5",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            cwd="/ws/it-donyu/api",
        )
        panes = [plain_sender, local_claude, other_claude]
        err = io.StringIO()
        with _runtime(panes, sender_pane_id="%50"):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    find_agent_window("claude", "mozyo-cockpit")
        self.assertIn("--target %pane", err.getvalue())


if __name__ == "__main__":
    unittest.main()
