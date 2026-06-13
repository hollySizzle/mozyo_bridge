"""Unified agent role identity model (Redmine #11822).

`normal mozyo` carries an agent's role on its window name; `mozyo cockpit`
carries it on the `@mozyo_agent_role` pane option (window named `cockpit`).
These tests pin the pure role resolver, the discovery classification that now
consumes it, and the role-aware handoff target resolution — all without a live
tmux server. The resolver always exposes the winning signal (`role_source`) and
its strength (`confidence`) so automatic targeting can fail closed on weak or
ambiguous signals.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.agent_discovery import (
    AGENT_KIND_CLAUDE,
    AGENT_KIND_CODEX,
    AGENT_KIND_UNKNOWN,
    CONFIDENCE_NONE,
    CONFIDENCE_STRONG,
    CONFIDENCE_WEAK,
    ROLE_SOURCE_INFERRED,
    ROLE_SOURCE_PANE_OPTION,
    ROLE_SOURCE_UNKNOWN,
    ROLE_SOURCE_WINDOW_NAME,
    discover_agents,
    resolve_agent_role,
)


def _pane(pane_id, location, *, command="node", cwd="/repo",
          window_name="cockpit", pane_active="1", agent_role="", lane_id=""):
    return {
        "id": pane_id,
        "location": location,
        "command": command,
        "cwd": cwd,
        "window_name": window_name,
        "pane_active": pane_active,
        "agent_role": agent_role,
        "workspace_id": "",
        "lane_id": lane_id,
    }


class ResolveAgentRoleTest(unittest.TestCase):
    def test_pane_option_is_strong_even_in_cockpit_window(self) -> None:
        r = resolve_agent_role(
            pane_option_role="claude", window_name="cockpit", process="node"
        )
        self.assertEqual(AGENT_KIND_CLAUDE, r.role)
        self.assertEqual(ROLE_SOURCE_PANE_OPTION, r.role_source)
        self.assertEqual(CONFIDENCE_STRONG, r.confidence)
        self.assertFalse(r.ambiguous)

    def test_window_name_is_strong_legacy_signal(self) -> None:
        r = resolve_agent_role(window_name="codex", process="node")
        self.assertEqual(AGENT_KIND_CODEX, r.role)
        self.assertEqual(ROLE_SOURCE_WINDOW_NAME, r.role_source)
        self.assertEqual(CONFIDENCE_STRONG, r.confidence)
        self.assertFalse(r.ambiguous)

    def test_pane_option_is_authoritative_over_layout_window_name(self) -> None:
        # #57116: a cockpit/managed pane's explicit role marker wins over a
        # layout/auto-named window (here `codex`); it must stay strong &
        # non-ambiguous so the pane remains reachable, not fail-closed.
        r = resolve_agent_role(pane_option_role="claude", window_name="codex")
        self.assertEqual(AGENT_KIND_CLAUDE, r.role)
        self.assertEqual(ROLE_SOURCE_PANE_OPTION, r.role_source)
        self.assertEqual(CONFIDENCE_STRONG, r.confidence)
        self.assertFalse(r.ambiguous)

    def test_pane_option_agreeing_with_window_is_not_ambiguous(self) -> None:
        r = resolve_agent_role(pane_option_role="claude", window_name="claude")
        self.assertEqual(AGENT_KIND_CLAUDE, r.role)
        self.assertFalse(r.ambiguous)

    def test_process_is_weak_only(self) -> None:
        r = resolve_agent_role(window_name="shell", process="codex")
        self.assertEqual(AGENT_KIND_CODEX, r.role)
        self.assertEqual(ROLE_SOURCE_INFERRED, r.role_source)
        self.assertEqual(CONFIDENCE_WEAK, r.confidence)

    def test_node_process_is_receiver_agnostic_unknown(self) -> None:
        r = resolve_agent_role(window_name="shell", process="node")
        self.assertEqual(AGENT_KIND_UNKNOWN, r.role)
        self.assertEqual(CONFIDENCE_NONE, r.confidence)

    def test_no_signal_is_unknown(self) -> None:
        r = resolve_agent_role(window_name="shell", process="zsh")
        self.assertEqual(AGENT_KIND_UNKNOWN, r.role)
        self.assertEqual(ROLE_SOURCE_UNKNOWN, r.role_source)
        self.assertEqual(CONFIDENCE_NONE, r.confidence)


class DiscoverAgentsRoleTest(unittest.TestCase):
    def test_cockpit_pane_classified_by_role_option_not_unknown(self) -> None:
        # The acceptance core: a cockpit pane (window `cockpit`, role option)
        # is claude/codex, NOT unknown.
        recs = discover_agents([
            _pane("%1", "mozyo-cockpit:0.0", window_name="cockpit", agent_role="codex"),
        ])
        self.assertEqual(AGENT_KIND_CODEX, recs[0].agent_kind)
        self.assertEqual(ROLE_SOURCE_PANE_OPTION, recs[0].role_source)
        self.assertEqual(CONFIDENCE_STRONG, recs[0].confidence)

    def test_normal_window_pane_still_classified_by_window_name(self) -> None:
        recs = discover_agents([
            _pane("%1", "repo:0.0", window_name="claude", command="claude"),
        ])
        self.assertEqual(AGENT_KIND_CLAUDE, recs[0].agent_kind)
        self.assertEqual(ROLE_SOURCE_WINDOW_NAME, recs[0].role_source)

    def test_weak_process_hint_does_not_promote_agent_kind(self) -> None:
        # A claude process in a non-agent window stays `unknown` (weak hint is
        # surfaced via role_source/confidence but never auto-classifies).
        recs = discover_agents([
            _pane("%1", "repo:0.0", window_name="shell", command="claude"),
        ])
        self.assertEqual(AGENT_KIND_UNKNOWN, recs[0].agent_kind)
        self.assertEqual(ROLE_SOURCE_INFERRED, recs[0].role_source)
        self.assertEqual(CONFIDENCE_WEAK, recs[0].confidence)

    def test_pane_option_overrides_layout_window_name_not_ambiguous(self) -> None:
        # #57116 regression: a cockpit Claude pane whose window is observed as
        # `codex` (tmux layout / auto-naming) is classified claude, strong, and
        # NOT ambiguous — the explicit marker is authoritative.
        recs = discover_agents([
            _pane("%1", "mozyo-cockpit:0.0", window_name="codex", agent_role="claude"),
        ])
        self.assertEqual(AGENT_KIND_CLAUDE, recs[0].agent_kind)
        self.assertEqual(ROLE_SOURCE_PANE_OPTION, recs[0].role_source)
        self.assertEqual(CONFIDENCE_STRONG, recs[0].confidence)
        self.assertFalse(recs[0].ambiguous)


class FindAgentWindowRoleTest(unittest.TestCase):
    def _patch(self, panes):
        return patch(
            "mozyo_bridge.domain.pane_resolver.pane_lines", return_value=panes
        )

    def test_resolves_cockpit_pane_by_role_option(self) -> None:
        from mozyo_bridge.domain.pane_resolver import find_agent_window

        panes = [
            _pane("%9", "mozyo-cockpit:0.2", window_name="cockpit", agent_role="claude"),
        ]
        with self._patch(panes):
            pane = find_agent_window("claude", "mozyo-cockpit")
        self.assertIsNotNone(pane)
        self.assertEqual("%9", pane["id"])

    def test_multiple_cockpit_role_panes_same_session_fail_closed(self) -> None:
        # Cockpit packs several workspaces' agents into one window; `--to claude`
        # with no explicit pane must fail closed, not pick one silently.
        from mozyo_bridge.domain.pane_resolver import find_agent_window

        panes = [
            _pane("%9", "mozyo-cockpit:0.2", window_name="cockpit", agent_role="claude"),
            _pane("%10", "mozyo-cockpit:0.4", window_name="cockpit", agent_role="claude"),
        ]
        with self._patch(panes):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    find_agent_window("claude", "mozyo-cockpit")

    def test_window_name_session_still_resolves(self) -> None:
        from mozyo_bridge.domain.pane_resolver import find_agent_window

        panes = [
            _pane("%1", "repo:0.0", window_name="claude", command="claude"),
        ]
        with self._patch(panes):
            pane = find_agent_window("claude", "repo")
        self.assertEqual("%1", pane["id"])

    def test_weak_process_pane_is_not_targeted(self) -> None:
        from mozyo_bridge.domain.pane_resolver import find_agent_window

        panes = [
            _pane("%1", "repo:0.0", window_name="shell", command="claude"),
        ]
        with self._patch(panes):
            self.assertIsNone(find_agent_window("claude", "repo"))

    def test_cockpit_pane_with_layout_window_name_still_resolves(self) -> None:
        # #57116 regression: the live cockpit was observed with a Claude-role
        # pane (`@mozyo_agent_role=claude`) in a window named `codex`. The
        # explicit marker is authoritative, so `--to claude` must resolve this
        # pane (the release-blocker) rather than fail-closed on the layout name.
        from mozyo_bridge.domain.pane_resolver import find_agent_window

        panes = [
            _pane("%9", "mozyo-cockpit:0.2", window_name="codex", agent_role="claude"),
        ]
        with self._patch(panes):
            pane = find_agent_window("claude", "mozyo-cockpit")
        self.assertIsNotNone(pane)
        self.assertEqual("%9", pane["id"])

    def test_window_name_codex_without_option_does_not_resolve_to_claude(self) -> None:
        # A normal pane (no marker) in a `codex` window is codex, not claude —
        # the window-name rail is unchanged when no pane option is present.
        from mozyo_bridge.domain.pane_resolver import find_agent_window

        panes = [
            _pane("%1", "repo:0.0", window_name="codex", command="node"),
        ]
        with self._patch(panes):
            self.assertIsNone(find_agent_window("claude", "repo"))


if __name__ == "__main__":
    unittest.main()
