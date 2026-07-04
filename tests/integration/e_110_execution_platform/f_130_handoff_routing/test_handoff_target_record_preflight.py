"""Explicit-pane handoff preflight projected onto the canonical TargetRecord.

Redmine #11908 (`vibes/docs/logics/unit-target-model.md` "Resolver priority"):
the handoff explicit-pane preflight resolves its target through the SAME role /
view projection `agents targets` uses (#11907), so normal-local and cockpit
panes share one resolver. The pane option (`@mozyo_agent_role` / workspace /
lane) is primary; the window name is a compatibility fallback tagged
`role_source == window_name`; ambiguous / unknown fails closed.

These tests pin the pure `project_preflight_target` projection and the
queue-enter Step 9 role binding it now feeds, all without a live tmux server.
The decisive guard against regression is that `window_name == role` is never
promoted back to a primary identity: a cockpit pane whose role marker disagrees
with its (layout) window name binds the marker's role, not the window's.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

# tmux-rail transport isolation (Redmine #13254): this fake-tmux module is a
# tmux send/capture-rail test, independent of the workspace terminal_transport
# backend. Import the package fixture so unittest pins resolve_handoff_transport_
# binding to the tmux default and the committed herdr cutover config does not
# drive these sends through the herdr shim.
from . import (  # noqa: E402,F401
    setUpModule,
    tearDownModule,
)

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
    AGENT_KIND_UNKNOWN,
    CONFIDENCE_NONE,
    CONFIDENCE_STRONG,
    CONFIDENCE_WEAK,
    ROLE_SOURCE_INFERRED,
    ROLE_SOURCE_PANE_OPTION,
    ROLE_SOURCE_WINDOW_NAME,
    VIEW_KIND_COCKPIT_PANE,
    VIEW_KIND_NORMAL_WINDOW,
    project_preflight_target,
)


def _pane(pane_id="%2", *, location="repo:0.0", command="node", cwd="/repo",
          window_name="cockpit", pane_active="1", agent_role="",
          workspace_id="", lane_id=""):
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
        "lane_label": "",
    }


class ProjectPreflightTargetTest(unittest.TestCase):
    def test_cockpit_pane_role_from_option_not_window_name(self) -> None:
        # Cockpit pane: role marker `claude`, layout window observed as `codex`.
        # The option is primary -> role=claude, cockpit_pane projection, and the
        # window name is NOT promoted to a rival identity.
        t = project_preflight_target(
            _pane(window_name="codex", agent_role="claude", command="claude")
        )
        self.assertEqual(AGENT_KIND_CLAUDE, t.role)
        self.assertEqual(ROLE_SOURCE_PANE_OPTION, t.role_source)
        self.assertEqual(CONFIDENCE_STRONG, t.confidence)
        self.assertFalse(t.ambiguous)
        self.assertEqual(VIEW_KIND_COCKPIT_PANE, t.view_kind)

    def test_cockpit_pane_binds_marker_role_only(self) -> None:
        # The window-name-not-primary contract: a marker=claude pane in a
        # window named `codex` binds claude, never codex.
        t = project_preflight_target(
            _pane(window_name="codex", agent_role="claude", command="claude")
        )
        self.assertTrue(t.binds_receiver("claude"))
        self.assertFalse(t.binds_receiver("codex"))

    def test_normal_window_fallback_is_maintained(self) -> None:
        # Compatibility projection: no pane option, role from the window name,
        # tagged role_source=window_name and normal_window view.
        t = project_preflight_target(
            _pane(window_name="claude", command="claude", agent_role="")
        )
        self.assertEqual(AGENT_KIND_CLAUDE, t.role)
        self.assertEqual(ROLE_SOURCE_WINDOW_NAME, t.role_source)
        self.assertEqual(CONFIDENCE_STRONG, t.confidence)
        self.assertEqual(VIEW_KIND_NORMAL_WINDOW, t.view_kind)
        self.assertTrue(t.binds_receiver("claude"))

    def test_weak_process_hint_does_not_bind(self) -> None:
        # A claude process in a non-agent window is weak/inferred and must NOT
        # bind under the relaxed rail (fail-closed).
        t = project_preflight_target(
            _pane(window_name="shell", command="claude", agent_role="")
        )
        self.assertEqual(AGENT_KIND_CLAUDE, t.role)
        self.assertEqual(ROLE_SOURCE_INFERRED, t.role_source)
        self.assertEqual(CONFIDENCE_WEAK, t.confidence)
        self.assertEqual(VIEW_KIND_NORMAL_WINDOW, t.view_kind)
        self.assertFalse(t.binds_receiver("claude"))

    def test_unknown_pane_is_retained_and_unbound(self) -> None:
        # Unlike build_target_candidates, an unknown-role pane is kept so the
        # caller can fail closed with provenance instead of losing the target.
        t = project_preflight_target(
            _pane(window_name="shell", command="node", agent_role="")
        )
        self.assertEqual(AGENT_KIND_UNKNOWN, t.role)
        self.assertEqual(CONFIDENCE_NONE, t.confidence)
        self.assertEqual("%2", t.pane_id)
        self.assertFalse(t.binds_receiver("claude"))
        self.assertFalse(t.binds_receiver("codex"))

    def test_workspace_and_lane_ride_the_projection(self) -> None:
        t = project_preflight_target(
            _pane(agent_role="codex", workspace_id="ws-7", lane_id="lane-a")
        )
        self.assertEqual("ws-7", t.workspace_id)
        self.assertEqual("lane-a", t.lane_id)

    def test_missing_lane_defaults_to_default(self) -> None:
        t = project_preflight_target(_pane(agent_role="codex", lane_id=""))
        self.assertEqual("default", t.lane_id)


class QueueEnterPreflightBindingTest(unittest.TestCase):
    """Step 9 binding via the projection, exercised through orchestrate_handoff."""

    def _run_handoff(self, argv, pane, *, sender_session, allow_exit=False):
        parser = build_parser()
        args = parser.parse_args(argv)
        sent: list[tuple[str, ...]] = []
        pane_text = ""

        def fake_capture(_target: str, _lines: int) -> str:
            return pane_text

        def fake_run_tmux(*tmux_args: str, check: bool = True):
            nonlocal pane_text
            if tmux_args[:4] == ("send-keys", "-t", pane["id"], "-l"):
                pane_text += tmux_args[-1]
            sent.append(tmux_args)
            return argparse.Namespace(returncode=0, stdout="", stderr="")

        with patch("mozyo_bridge.application.commands.require_tmux"), \
            patch("mozyo_bridge.application.commands.current_pane", return_value="%1"), \
            patch(
                "mozyo_bridge.application.commands.current_session_name",
                return_value=sender_session,
            ), \
            patch("mozyo_bridge.application.commands.capture_pane", side_effect=fake_capture), \
            patch("mozyo_bridge.application.commands.run_tmux", side_effect=fake_run_tmux), \
            patch("mozyo_bridge.application.commands.time.sleep"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.validate_target"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.pane_resolver.pane_lines", return_value=[pane]), \
            contextlib.redirect_stdout(io.StringIO()) as stdout, \
            contextlib.redirect_stderr(io.StringIO()):
            try:
                result = args.func(args)
            except SystemExit as exc:
                if not allow_exit:
                    raise
                result = exc

        return result, sent, stdout.getvalue()

    @staticmethod
    def _argv(receiver):
        return [
            "handoff", "send",
            "--to", receiver,
            "--target", "%2",
            "--source", "redmine",
            "--issue", "11908",
            "--journal", "58149",
            "--kind", "implementation_request",
            "--submit-delay", "0",
        ]

    def _last_outcome(self, stdout):
        lines = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
        self.assertTrue(lines, "expected a structured outcome line")
        return json.loads(lines[-1])

    def test_cockpit_marker_role_admits_matching_receiver(self) -> None:
        # Cockpit pane: marker=claude, layout window=codex. `--to claude` binds
        # via the pane option and submits Enter under the default queue-enter
        # rail; the layout window name does not block it. A non-default lane
        # keeps this a legitimate same-lane *sublane* Claude dispatch so the
        # #12441 main-lane guard (cockpit + default lane + implementation_request)
        # does not apply — this test isolates the role-binding behavior.
        pane = _pane(
            pane_id="%2", location="mozyo-cockpit:0.2", window_name="codex",
            command="claude", agent_role="claude", lane_id="lane-sub",
        )
        result, sent, _stdout = self._run_handoff(
            self._argv("claude"), pane, sender_session="mozyo-cockpit"
        )
        self.assertEqual(0, result)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])

    def test_cockpit_marker_role_rejects_window_name_receiver(self) -> None:
        # Same pane, but `--to codex` (which only the layout window name would
        # suggest). The projection binds claude, so codex is fail-closed with
        # `invalid_args` and no Enter — proving window_name is not primary.
        pane = _pane(
            pane_id="%2", location="mozyo-cockpit:0.2", window_name="codex",
            command="claude", agent_role="claude",
        )
        result, sent, stdout = self._run_handoff(
            self._argv("codex"), pane, sender_session="mozyo-cockpit",
            allow_exit=True,
        )
        self.assertIsInstance(result, SystemExit)
        self.assertFalse(any(c[:3] == ("send-keys", "-t", "%2") and c[-1] == "Enter" for c in sent))
        outcome = self._last_outcome(stdout)
        self.assertEqual("blocked", outcome["status"])
        self.assertEqual("invalid_args", outcome["reason"])

    def test_normal_window_pane_still_admits(self) -> None:
        # Compatibility rail: a normal `mozyo` codex window (no marker) still
        # binds codex via the window-name fallback and submits.
        pane = _pane(
            pane_id="%2", location="agents:0.0", window_name="codex",
            command="codex", agent_role="",
        )
        result, sent, _stdout = self._run_handoff(
            self._argv("codex"), pane, sender_session="agents"
        )
        self.assertEqual(0, result)
        self.assertEqual(("send-keys", "-t", "%2", "Enter"), sent[-1])


if __name__ == "__main__":
    unittest.main()
