"""CLI/integration regression for same-session local Claude auto-resolution (#12072).

Resolver-level narrowing already has focused fixtures in
``tests/test_handoff_same_lane_gateway.py`` (Redmine #12070). These tests pin the
behavior one layer up: the wired ``mozyo-bridge handoff send --to claude`` CLI
path (no explicit ``--target``) drives the real
``orchestrate_handoff`` -> ``pane_info`` -> ``resolve_target`` ->
``find_agent_window`` -> ``narrow_to_local_claude`` chain end to end and emits a
structured outcome. The point is that the #12070 narrowing, once wired into the
command, keeps the routing boundary intact:

- exactly one same-session same ``(workspace_id, lane_id)`` / repo-root Claude
  among several Claude panes auto-resolves and the send goes through;
- an unrelated-workspace Claude in the same session is never auto-selected;
- two same-lane Claude panes sharing the repo root (the #12098-adjacent nested
  execution-root case) stay ambiguous and fail closed;
- a sender with no machine-checkable ``workspace_id`` fails closed;
- the implicit ``--to claude`` resolver never reaches into another tmux session,
  and an explicit cross-session ``--to claude`` stays blocked
  (``cross_session_claude``);
- the Codex gateway path (#12011 same-lane narrowing) is untouched by the
  Claude-only repo gate.

All hermetic: ``pane_lines`` / ``current_session_name`` / ``infer_repo_root`` and
the typing seams are patched, the sender pane is the patched ``TMUX_PANE``. No
live tmux server is required.
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

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))


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


SESSION = "mozyo-cockpit"

# The sender is a Codex pane handing off to its own local Claude. Identity:
# workspace ws-it-donyu, default coordinator lane, repo root /ws/it-donyu.
SENDER_CODEX = _pane(
    "%900",
    f"{SESSION}:0.9",
    agent_role="codex",
    workspace_id="ws-it-donyu",
    lane_id="lane-main",
    command="codex",
    cwd="/ws/it-donyu",
)
# The sender's own local Claude: same workspace/lane, repo root /ws/it-donyu,
# checked out under .../app. This is the pane a happy-path send must reach.
LOCAL_CLAUDE = _pane(
    "%901",
    f"{SESSION}:0.1",
    agent_role="claude",
    workspace_id="ws-it-donyu",
    lane_id="lane-main",
    command="claude",
    cwd="/ws/it-donyu/app",
)

# Repo roots the patched ``infer_repo_root`` returns per cwd. A cwd absent from
# the map infers ``None`` (no marker reachable), matching the real resolver.
REPO_ROOTS = {
    "/ws/it-donyu": "/ws/it-donyu",
    "/ws/it-donyu/app": "/ws/it-donyu",
    "/ws/it-donyu/api": "/ws/it-donyu",
    "/ws/it-donyu-clone": "/ws/it-donyu-clone",
    "/ws/other": "/ws/other",
}


def _fake_infer_repo_root(cwd):
    return REPO_ROOTS.get(cwd)


def _outcome_from(stdout: str):
    """The last JSON outcome line emitted on stdout, or ``None``."""
    outcome = None
    for line in stdout.splitlines():
        if line.strip().startswith("{"):
            try:
                outcome = json.loads(line)
            except ValueError:
                pass
    return outcome


class HandoffLocalClaudeCliTest(unittest.TestCase):
    """`handoff send --to claude` end to end with the #12070 narrowing wired in."""

    def _run_send(
        self,
        panes,
        *,
        sender_pane_id="%900",
        sender_session=SESSION,
        receiver="claude",
        target=None,
        mode="standard",
        marker_lands=True,
    ):
        from mozyo_bridge.application import commands
        from mozyo_bridge.application.cli import build_parser

        argv = [
            "handoff", "send", "--to", receiver,
            "--source", "redmine", "--issue", "12072", "--journal", "59608",
            "--kind", "implementation_request",
            "--mode", mode,
            "--landing-timeout", "0.01", "--submit-delay", "0",
            "--summary", "regression fixture",
        ]
        if target is not None:
            argv += ["--target", target]
        args = build_parser().parse_args(argv)

        def fake_run_tmux(*a, check: bool = True):
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        env = {k: v for k, v in os.environ.items() if k != "TMUX_PANE"}
        if sender_pane_id is not None:
            env["TMUX_PANE"] = sender_pane_id

        with patch.object(commands, "require_tmux"), \
            patch.object(commands, "capture_pane", return_value=""), \
            patch.object(commands, "run_tmux", side_effect=fake_run_tmux), \
            patch.object(commands, "wait_for_text", return_value=marker_lands), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch.object(
                commands, "current_session_name", return_value=sender_session
            ), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.current_session_name",
                return_value=sender_session,
            ), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=panes
            ), \
            patch(
                "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.infer_repo_root",
                _fake_infer_repo_root,
            ), \
            patch.dict(os.environ, env, clear=True), \
            contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()) as err:
            with contextlib.suppress(SystemExit):
                args.func(args)
        return _outcome_from(out.getvalue()), out.getvalue(), err.getvalue()

    # --- happy path: exactly one same ws/lane/repo Claude auto-selects ------

    def test_unique_same_ws_lane_repo_claude_resolves_and_sends(self) -> None:
        # A second Claude shares the (workspace, lane) but lives in a different
        # repo checkout (/ws/it-donyu-clone): the repo gate drops it, leaving the
        # sender's own local Claude as the unique target.
        clone_claude = _pane(
            "%903",
            f"{SESSION}:0.3",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            command="claude",
            cwd="/ws/it-donyu-clone",
        )
        panes = [SENDER_CODEX, LOCAL_CLAUDE, clone_claude]
        outcome, _out, _err = self._run_send(panes)
        self.assertIsNotNone(outcome)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("%901", outcome["target"])
        self.assertEqual("claude", outcome["receiver"])

    def test_unrelated_workspace_claude_not_selected(self) -> None:
        # An unrelated-workspace Claude shares the cockpit session but neither the
        # workspace nor the lane: it must not be auto-selected.
        foreign_claude = _pane(
            "%910",
            f"{SESSION}:0.10",
            agent_role="claude",
            workspace_id="ws-other",
            lane_id="lane-x",
            command="claude",
            cwd="/ws/other",
        )
        panes = [SENDER_CODEX, LOCAL_CLAUDE, foreign_claude]
        outcome, _out, _err = self._run_send(panes)
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("%901", outcome["target"])

    # --- fail-closed cases --------------------------------------------------

    def test_ambiguous_same_repo_same_lane_fails_closed(self) -> None:
        # Two Claude panes share the (workspace, lane) AND the repo root: this is
        # the #12098-adjacent nested execution-root case. Pane selection stays
        # ambiguous and must fail closed, never auto-pick one silently.
        sibling_claude = _pane(
            "%904",
            f"{SESSION}:0.4",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            command="claude",
            cwd="/ws/it-donyu/api",
        )
        panes = [SENDER_CODEX, LOCAL_CLAUDE, sibling_claude]
        outcome, _out, err = self._run_send(panes)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_unavailable", outcome["reason"])
        # Concrete candidates and the explicit-target retry hint are surfaced.
        self.assertIn("%901", err)
        self.assertIn("%904", err)
        self.assertIn("--target %pane", err)

    def test_missing_sender_workspace_identity_fails_closed(self) -> None:
        # Sender carries no machine-checkable workspace_id: narrowing stays off,
        # so multiple Claude candidates cannot be disambiguated and fail closed.
        plain_sender = _pane(
            "%50", f"{SESSION}:0.50", agent_role="codex", command="codex"
        )
        sibling_claude = _pane(
            "%904",
            f"{SESSION}:0.4",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            command="claude",
            cwd="/ws/it-donyu/api",
        )
        panes = [plain_sender, LOCAL_CLAUDE, sibling_claude]
        outcome, _out, err = self._run_send(panes, sender_pane_id="%50")
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_unavailable", outcome["reason"])
        self.assertIn("--target %pane", err)

    # --- cross-session Claude direct stays blocked --------------------------

    def test_implicit_claude_does_not_cross_session(self) -> None:
        # The only same-identity Claude lives in another tmux session. The
        # implicit `--to claude` resolver is scoped to the sender's session, so it
        # finds no local Claude and fails closed — it never reaches across the
        # session boundary to auto-select the foreign-session Claude.
        foreign_session_claude = _pane(
            "%920",
            "other-sess:0.1",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            command="claude",
            cwd="/ws/it-donyu/app",
        )
        panes = [SENDER_CODEX, foreign_session_claude]
        outcome, _out, err = self._run_send(panes)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("target_unavailable", outcome["reason"])
        self.assertNotEqual("%920", outcome.get("target"))
        self.assertIn(f"no claude window found in session '{SESSION}'", err)

    def test_explicit_cross_session_claude_still_blocked(self) -> None:
        # Even named explicitly, a cross-session `--to claude` stays blocked: the
        # #12070 narrowing only ever shrinks the same-session set, it never opens
        # the direct cross-session route.
        foreign_session_claude = _pane(
            "%920",
            "other-sess:0.1",
            agent_role="claude",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            command="claude",
            cwd="/ws/it-donyu/app",
        )
        panes = [SENDER_CODEX, foreign_session_claude]
        outcome, _out, err = self._run_send(panes, target="%920")
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("cross_session_claude", outcome["reason"])
        self.assertIn("cross-session handoff to Claude is not allowed", err)

    # --- #12011 Codex gateway path is untouched by the Claude repo gate -----

    def test_codex_gateway_same_lane_resolution_unaffected(self) -> None:
        # The Claude-only repo gate must not bleed into `--to codex`: the Codex
        # path keeps its #12011 same-lane narrowing. A Claude sender in a
        # multi-lane cockpit resolves its own lane's Codex gateway, ignoring a
        # foreign-lane Codex — same-lane narrowing, no repo gate.
        same_lane_codex = _pane(
            "%911",
            f"{SESSION}:0.11",
            agent_role="codex",
            workspace_id="ws-it-donyu",
            lane_id="lane-main",
            command="codex",
            cwd="/ws/it-donyu/app",
        )
        other_lane_codex = _pane(
            "%912",
            f"{SESSION}:0.12",
            agent_role="codex",
            workspace_id="ws-it-donyu",
            lane_id="lane-12007",
            command="codex",
            cwd="/ws/it-donyu-clone",
        )
        # Sender is the local Claude (%901), so it is not itself a Codex
        # candidate; the two Codex panes are the candidates to narrow.
        panes = [LOCAL_CLAUDE, same_lane_codex, other_lane_codex]
        outcome, _out, _err = self._run_send(
            panes, sender_pane_id="%901", receiver="codex"
        )
        self.assertEqual("sent", outcome["status"])
        self.assertEqual("%911", outcome["target"])


if __name__ == "__main__":
    unittest.main()
