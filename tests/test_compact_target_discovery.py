"""Compact target discovery for LLM / operator handoff (Redmine #11811).

A read-only surface that lists classified agent panes as candidate handoff
targets with just enough structured fields to pick an explicit ``pane_id``
without parsing titles. It reuses the #11822 role resolver and #11820 lane
facts so cockpit panes are represented by their pane options, and stays
non-selecting: same-role candidates remain distinguishable by workspace / lane
/ pane, so a natural name can never auto-cross a safety boundary. These tests
pin the pure builder and the CLI command (text + JSON) with tmux mocked.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.agent_discovery import (
    AgentRecord,
    build_target_candidates,
    discover_agents,
    fold_agents_by_pane,
)


def _pane(pane_id, location, *, command="node", cwd="/work/repo",
          window_name="cockpit", pane_active="1", agent_role="",
          lane_id="", lane_label=""):
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
        "lane_label": lane_label,
    }


class BuildTargetCandidatesTest(unittest.TestCase):
    def _records(self, panes):
        # Fold with a fixed repo root so workspace resolution can fire.
        with patch(
            "mozyo_bridge.domain.agent_discovery.infer_repo_root",
            return_value="/work/repo",
        ):
            return fold_agents_by_pane(discover_agents(panes))

    def test_excludes_unknown_panes(self) -> None:
        records = self._records([
            _pane("%1", "s:0.0", window_name="shell", command="zsh"),
        ])
        self.assertEqual([], build_target_candidates(records))

    def test_cockpit_pane_projected_from_role_option(self) -> None:
        records = self._records([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude", lane_id="lane-abc", lane_label="feature/x"),
        ])
        cands = build_target_candidates(
            records,
            resolve_workspace=lambda root: ("wsA", "mozyo-bridge"),
        )
        self.assertEqual(1, len(cands))
        c = cands[0]
        self.assertEqual("%9", c.pane_id)
        self.assertEqual("claude", c.role)
        self.assertEqual("pane_option", c.role_source)
        self.assertEqual("strong", c.confidence)
        self.assertFalse(c.ambiguous)
        self.assertEqual("wsA", c.workspace_id)
        self.assertEqual("mozyo-bridge", c.workspace_label)
        self.assertEqual("lane-abc", c.lane_id)
        self.assertEqual("feature/x", c.lane_label)
        self.assertEqual("repo", c.repo_short)  # basename of /work/repo
        self.assertEqual("local", c.host)

    def test_same_workspace_different_lane_is_distinguishable(self) -> None:
        records = self._records([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude", lane_id="lane-main"),
            _pane("%10", "mozyo-cockpit:0.3", window_name="codex",
                  agent_role="claude", lane_id="lane-wt", lane_label="feat"),
        ])
        cands = build_target_candidates(
            records, resolve_workspace=lambda root: ("wsA", "ws")
        )
        self.assertEqual(2, len(cands))
        # same workspace + role, but distinct lanes and distinct panes.
        self.assertEqual({"wsA"}, {c.workspace_id for c in cands})
        self.assertEqual({"claude"}, {c.role for c in cands})
        self.assertEqual({"lane-main", "lane-wt"}, {c.lane_id for c in cands})
        self.assertEqual({"%9", "%10"}, {c.pane_id for c in cands})

    def test_missing_lane_normalizes_to_default(self) -> None:
        records = self._records([
            _pane("%1", "repo:0.0", window_name="claude", command="claude"),
        ])
        cands = build_target_candidates(records)
        self.assertEqual("default", cands[0].lane_id)

    def test_workspace_resolver_invoked_once_per_repo_root(self) -> None:
        records = self._records([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
            _pane("%10", "mozyo-cockpit:0.3", window_name="codex", agent_role="codex"),
        ])
        calls: list[str] = []

        def resolver(root):
            calls.append(root)
            return ("wsA", "ws")

        build_target_candidates(records, resolve_workspace=resolver)
        self.assertEqual(["/work/repo"], calls)  # cached per distinct root


class AgentsTargetsCommandTest(unittest.TestCase):
    def _run(self, panes, *, as_json=False, session=None, agent=None,
             workspace_id="wsA", label="mozyo-bridge", repo_root="/work/repo"):
        from mozyo_bridge.application import commands

        canon = argparse.Namespace(name=label, workspace_id=workspace_id)
        args = argparse.Namespace(session=session, agent=agent, as_json=as_json)
        with patch.object(commands, "require_tmux"), \
            patch("mozyo_bridge.domain.agent_discovery.pane_lines", return_value=panes), \
            patch("mozyo_bridge.domain.agent_discovery.infer_repo_root", return_value=repo_root), \
            patch.object(commands, "resolve_canonical_session", return_value=canon), \
            contextlib.redirect_stdout(io.StringIO()) as out:
            rc = commands.cmd_agents_targets(args)
        return rc, out.getvalue()

    def test_compact_text_lists_cockpit_candidate_with_disambiguators(self) -> None:
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude", lane_id="lane-abc", lane_label="feat"),
        ])
        self.assertEqual(0, rc)
        self.assertIn(
            "PANE\tROLE\tROLE_SOURCE\tCONF\tAMBIG\tWORKSPACE\tLANE\tREPO\tACTIVE\t"
            "SESSION\tWINDOW",
            out,
        )
        self.assertIn("%9\tclaude\tpane_option\tstrong\t0\tmozyo-bridge\t"
                      "lane-abc(feat)\trepo\t1\tmozyo-cockpit\tcodex", out)

    def test_compact_text_hides_absolute_paths(self) -> None:
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude"),
        ], repo_root="/private/secret/repo")
        self.assertEqual(0, rc)
        self.assertNotIn("/private/secret/repo", out)  # only the basename shows
        self.assertIn("\trepo\t", out)

    def test_json_carries_structured_fields_including_repo_root(self) -> None:
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude", lane_id="lane-abc", lane_label="feat"),
        ], as_json=True)
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertEqual(1, len(payload))
        c = payload[0]
        self.assertEqual("%9", c["pane_id"])
        self.assertEqual("claude", c["role"])
        self.assertEqual("pane_option", c["role_source"])
        self.assertEqual("wsA", c["workspace_id"])
        self.assertEqual("lane-abc", c["lane_id"])
        self.assertEqual("/work/repo", c["repo_root"])  # JSON keeps the full path
        self.assertEqual("local", c["host"])

    def test_unknown_panes_are_not_listed(self) -> None:
        rc, out = self._run([
            _pane("%5", "s:0.0", window_name="shell", command="zsh"),
        ])
        self.assertEqual(0, rc)
        lines = [ln for ln in out.splitlines() if ln and not ln.startswith("PANE")]
        self.assertEqual([], lines)

    def test_agent_filter_restricts_role(self) -> None:
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
            _pane("%10", "mozyo-cockpit:0.3", window_name="codex", agent_role="codex"),
        ], agent="codex")
        self.assertEqual(0, rc)
        self.assertIn("%10\tcodex", out)
        self.assertNotIn("%9\t", out)


if __name__ == "__main__":
    unittest.main()
