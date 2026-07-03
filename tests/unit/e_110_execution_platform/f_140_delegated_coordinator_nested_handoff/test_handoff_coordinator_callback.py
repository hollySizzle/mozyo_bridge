"""Coordinator-Codex callback resolution (Redmine #12015).

#12011 added same-lane narrowing so a bare ``--to codex`` resolves to the
sender lane's own Codex. A sublane calling *back* to the main coordinator Codex
is a cross-lane operation that same-lane narrowing deliberately will not pick —
it still needed a hand-picked ``%pane``. These tests pin the additive
``coordinator`` pseudo-target: it resolves the sender workspace's default-lane
(coordinator) Codex, stays workspace-scoped, and fails closed with concrete
guidance when the coordinator cannot be uniquely resolved. No live tmux server
is required — ``pane_lines`` and the sender ``TMUX_PANE`` are patched.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver import (  # noqa: E402
    _no_coordinator_message,
    coordinator_codex_candidates,
    resolve_coordinator_codex,
    resolve_target,
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


WS_BRIDGE = "ws-mozyo-bridge"
WS_DRIVE = "ws-cloud-drive"

# Main coordinator lane (primary checkout -> DEFAULT_LANE "default").
MAIN_CODEX = _pane(
    "%953", "mozyo-cockpit:0.1",
    agent_role="codex", workspace_id=WS_BRIDGE, lane_id="default", lane_label="main",
)
MAIN_CLAUDE = _pane(
    "%954", "mozyo-cockpit:0.2",
    agent_role="claude", workspace_id=WS_BRIDGE, lane_id="default", lane_label="main",
)
# Sublane (issue_12011) — sender lives here.
SUBLANE_CLAUDE = _pane(
    "%1072", "mozyo-cockpit:0.7",
    agent_role="claude", workspace_id=WS_BRIDGE, lane_id="lane-12011", lane_label="issue_12011",
)
SUBLANE_CODEX = _pane(
    "%1071", "mozyo-cockpit:0.6",
    agent_role="codex", workspace_id=WS_BRIDGE, lane_id="lane-12011", lane_label="issue_12011",
)
# A second workspace's coordinator (must never be selected for WS_BRIDGE).
DRIVE_CODEX = _pane(
    "%1063", "mozyo-cockpit:0.3",
    agent_role="codex", workspace_id=WS_DRIVE, lane_id="", lane_label="",
)

FULL_COCKPIT = [MAIN_CODEX, MAIN_CLAUDE, SUBLANE_CLAUDE, SUBLANE_CODEX, DRIVE_CODEX]


@contextlib.contextmanager
def _runtime(panes, *, sender_pane_id):
    env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
    if sender_pane_id is not None:
        env["TMUX_PANE"] = sender_pane_id
    with patch(
        "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes
    ), patch(
        "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.current_session_name",
        return_value="mozyo-cockpit",
    ), patch.dict(os.environ, env, clear=True):
        yield


class CoordinatorCandidatesTest(unittest.TestCase):
    def test_only_default_lane_codex_in_workspace(self) -> None:
        cands = coordinator_codex_candidates(FULL_COCKPIT, WS_BRIDGE)
        self.assertEqual(["%953"], [p["id"] for p in cands])

    def test_excludes_foreign_workspace_default_lane_codex(self) -> None:
        # The cloud-drive coordinator is default-lane Codex too, but a different
        # workspace; it must not appear for WS_BRIDGE.
        cands = coordinator_codex_candidates(FULL_COCKPIT, WS_BRIDGE)
        self.assertNotIn("%1063", [p["id"] for p in cands])

    def test_excludes_default_lane_claude(self) -> None:
        cands = coordinator_codex_candidates(FULL_COCKPIT, WS_BRIDGE)
        self.assertNotIn("%954", [p["id"] for p in cands])

    def test_excludes_sublane_codex(self) -> None:
        cands = coordinator_codex_candidates(FULL_COCKPIT, WS_BRIDGE)
        self.assertNotIn("%1071", [p["id"] for p in cands])


class ResolveCoordinatorCodexTest(unittest.TestCase):
    def test_sublane_sender_resolves_main_coordinator(self) -> None:
        self.assertEqual(
            "%953", resolve_coordinator_codex(FULL_COCKPIT, SUBLANE_CLAUDE)["id"]
        )

    def test_cross_workspace_sender_resolves_its_own_coordinator(self) -> None:
        # A cloud-drive sender resolves the cloud-drive coordinator, not %953.
        drive_claude = _pane(
            "%1064", "mozyo-cockpit:0.4",
            agent_role="claude", workspace_id=WS_DRIVE, lane_id="",
        )
        self.assertEqual(
            "%1063", resolve_coordinator_codex(FULL_COCKPIT, drive_claude)["id"]
        )

    def test_no_default_lane_codex_returns_none(self) -> None:
        panes = [SUBLANE_CLAUDE, SUBLANE_CODEX]  # coordinator lane absent
        self.assertIsNone(resolve_coordinator_codex(panes, SUBLANE_CLAUDE))

    def test_ambiguous_multiple_default_lane_codex_returns_none(self) -> None:
        dup_main_codex = _pane(
            "%955", "mozyo-cockpit:0.9",
            agent_role="codex", workspace_id=WS_BRIDGE, lane_id="default",
        )
        panes = FULL_COCKPIT + [dup_main_codex]
        self.assertIsNone(resolve_coordinator_codex(panes, SUBLANE_CLAUDE))

    def test_sender_none_returns_none(self) -> None:
        self.assertIsNone(resolve_coordinator_codex(FULL_COCKPIT, None))

    def test_sender_without_workspace_identity_returns_none(self) -> None:
        plain = _pane("%5", "mozyo-cockpit:0.5", agent_role="claude")
        self.assertIsNone(resolve_coordinator_codex(FULL_COCKPIT, plain))


class ResolveTargetCoordinatorTest(unittest.TestCase):
    def test_coordinator_label_resolves_to_main_codex(self) -> None:
        with _runtime(FULL_COCKPIT, sender_pane_id="%1072"):
            self.assertEqual("%953", resolve_target("coordinator"))

    def test_same_lane_codex_behavior_unchanged(self) -> None:
        # #12011 coexistence: bare `codex` still resolves the sender lane's own
        # Codex, not the coordinator.
        with _runtime(FULL_COCKPIT, sender_pane_id="%1072"):
            self.assertEqual("%1071", resolve_target("codex"))

    def test_unknown_sender_fails_closed(self) -> None:
        err = io.StringIO()
        with _runtime(FULL_COCKPIT, sender_pane_id="%nope"):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    resolve_target("coordinator")
        message = err.getvalue()
        self.assertIn("sender pane is unknown", message)
        self.assertIn("--target %pane", message)

    def test_missing_coordinator_fails_closed_with_workspace(self) -> None:
        panes = [SUBLANE_CLAUDE, SUBLANE_CODEX]
        err = io.StringIO()
        with _runtime(panes, sender_pane_id="%1072"):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    resolve_target("coordinator")
        message = err.getvalue()
        self.assertIn("no default-lane (coordinator) Codex", message)
        self.assertIn(WS_BRIDGE, message)

    def test_ambiguous_coordinator_fails_closed_with_candidates(self) -> None:
        dup_main_codex = _pane(
            "%955", "mozyo-cockpit:0.9",
            agent_role="codex", workspace_id=WS_BRIDGE, lane_id="default",
        )
        panes = FULL_COCKPIT + [dup_main_codex]
        err = io.StringIO()
        with _runtime(panes, sender_pane_id="%1072"):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    resolve_target("coordinator")
        message = err.getvalue()
        self.assertIn("multiple default-lane Codex", message)
        self.assertIn("%953", message)
        self.assertIn("%955", message)


class NoCoordinatorCanonicalHintTest(unittest.TestCase):
    """The no-coordinator message points at the real cause (#13152)."""

    # A workspace whose only panes are the sublane — no default-lane Codex.
    PANES = [SUBLANE_CLAUDE, SUBLANE_CODEX]

    def test_generic_hint_without_canonical_state(self) -> None:
        message = _no_coordinator_message(self.PANES, SUBLANE_CLAUDE)
        self.assertIn("no default-lane (coordinator) Codex pane", message)
        self.assertNotIn("registry defect", message)

    def test_dead_canonical_names_the_registry_defect(self) -> None:
        canonical_state = {
            "canonical_path": "/gone/main",
            "exists": False,
            "is_dir": False,
            "is_git": None,
            "is_main_worktree": None,
        }
        message = _no_coordinator_message(
            self.PANES, SUBLANE_CLAUDE, canonical_state=canonical_state
        )
        self.assertIn("registry defect", message)
        self.assertIn("#13152", message)
        self.assertIn("/gone/main", message)
        self.assertIn("workspace register", message)

    def test_worktree_canonical_names_the_registry_defect(self) -> None:
        canonical_state = {
            "canonical_path": "/repo/.git/worktrees/wt",
            "exists": True,
            "is_dir": True,
            "is_git": True,
            "is_main_worktree": False,
        }
        message = _no_coordinator_message(
            self.PANES, SUBLANE_CLAUDE, canonical_state=canonical_state
        )
        self.assertIn("registry defect", message)
        self.assertIn("#13152", message)

    def test_healthy_canonical_keeps_generic_hint(self) -> None:
        canonical_state = {
            "canonical_path": "/repo",
            "exists": True,
            "is_dir": True,
            "is_git": True,
            "is_main_worktree": True,
        }
        message = _no_coordinator_message(
            self.PANES, SUBLANE_CLAUDE, canonical_state=canonical_state
        )
        self.assertIn("no default-lane (coordinator) Codex pane", message)
        self.assertNotIn("registry defect", message)


if __name__ == "__main__":
    unittest.main()
