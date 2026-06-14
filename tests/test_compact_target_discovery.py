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
        # Role resolved from the pane option -> managed/cockpit projection.
        self.assertEqual("cockpit_pane", c.view_kind)

    def test_normal_window_pane_projects_normal_view_kind(self) -> None:
        # Role from the window name (no `@mozyo_agent_role`) is the normal-`mozyo`
        # compatibility rail, so it projects as `normal_window` (#11907).
        records = self._records([
            _pane("%2", "repo:0.0", window_name="claude", command="claude"),
        ])
        cands = build_target_candidates(records)
        self.assertEqual(1, len(cands))
        c = cands[0]
        self.assertEqual("claude", c.role)
        self.assertEqual("window_name", c.role_source)
        self.assertEqual("normal_window", c.view_kind)

    def test_branch_resolved_once_per_repo_root(self) -> None:
        records = self._records([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
            _pane("%10", "mozyo-cockpit:0.3", window_name="codex", agent_role="codex"),
        ])
        calls: list[str] = []

        def branch_resolver(root):
            calls.append(root)
            return "issue_11907"

        cands = build_target_candidates(records, resolve_branch=branch_resolver)
        self.assertEqual({"issue_11907"}, {c.branch for c in cands})
        self.assertEqual(["/work/repo"], calls)  # cached per distinct root

    def test_branch_defaults_to_none_without_resolver(self) -> None:
        records = self._records([
            _pane("%1", "repo:0.0", window_name="claude", command="claude"),
        ])
        self.assertIsNone(build_target_candidates(records)[0].branch)

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
             workspace_id="wsA", label="mozyo-bridge", repo_root="/work/repo",
             branch="issue_11907"):
        from mozyo_bridge.application import commands

        canon = argparse.Namespace(name=label, workspace_id=workspace_id)
        args = argparse.Namespace(session=session, agent=agent, as_json=as_json)
        with patch.object(commands, "require_tmux"), \
            patch("mozyo_bridge.domain.agent_discovery.pane_lines", return_value=panes), \
            patch("mozyo_bridge.domain.agent_discovery.infer_repo_root", return_value=repo_root), \
            patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_probe_checkout_facts",
                         return_value={"branch": branch}), \
            contextlib.redirect_stdout(io.StringIO()) as out:
            rc = commands.cmd_agents_targets(args)
        return rc, out.getvalue()

    def test_compact_text_lists_cockpit_candidate_with_disambiguators(self) -> None:
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude", lane_id="lane-abc", lane_label="feat"),
        ])
        self.assertEqual(0, rc)
        # Original column run is preserved; VIEW_KIND / BRANCH are appended (#11907).
        self.assertIn(
            "PANE\tROLE\tROLE_SOURCE\tCONF\tAMBIG\tWORKSPACE\tLANE\tREPO\tACTIVE\t"
            "SESSION\tWINDOW\tVIEW_KIND\tBRANCH",
            out,
        )
        self.assertIn("%9\tclaude\tpane_option\tstrong\t0\tmozyo-bridge\t"
                      "lane-abc(feat)\trepo\t1\tmozyo-cockpit\tcodex\t"
                      "cockpit_pane\tissue_11907", out)

    def test_normal_window_text_uses_one_vocabulary(self) -> None:
        # A normal-`mozyo` pane lists with the same columns as a cockpit pane,
        # carrying `normal_window` so both share one target vocabulary (#11907).
        rc, out = self._run([
            _pane("%2", "repo:0.0", window_name="claude", command="claude"),
        ])
        self.assertEqual(0, rc)
        self.assertIn(
            "%2\tclaude\twindow_name\tstrong\t0\tmozyo-bridge\tdefault\trepo\t1\t"
            "repo\tclaude\tnormal_window\tissue_11907",
            out,
        )

    def test_compact_text_hides_absolute_paths(self) -> None:
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude"),
        ], repo_root="/private/secret/repo")
        self.assertEqual(0, rc)
        self.assertNotIn("/private/secret/repo", out)  # only the basename shows
        self.assertIn("\trepo\t", out)

    def test_json_renders_nested_target_record_projection(self) -> None:
        # --json is the nested canonical TargetRecord projection (#11907):
        # host / runtime / identity / repo / view, not a flat dict.
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude", lane_id="lane-abc", lane_label="feat"),
        ], as_json=True, branch="issue_11907")
        self.assertEqual(0, rc)
        payload = json.loads(out)
        self.assertEqual(1, len(payload))
        c = payload[0]
        self.assertEqual("local", c["host"]["id"])
        self.assertEqual("local", c["host"]["kind"])
        self.assertEqual("tmux", c["runtime"]["provider"])
        self.assertEqual("%9", c["runtime"]["pane_id"])
        self.assertEqual("mozyo-cockpit", c["runtime"]["session"])
        self.assertEqual("/work/repo", c["runtime"]["cwd"])
        self.assertEqual("claude", c["identity"]["role"])
        self.assertEqual("pane_option", c["identity"]["role_source"])
        self.assertEqual("wsA", c["identity"]["workspace_id"])
        self.assertEqual("lane-abc", c["identity"]["lane_id"])
        self.assertFalse(c["identity"]["ambiguous"])
        self.assertEqual("/work/repo", c["repo"]["root"])  # JSON keeps the full path
        self.assertEqual("repo", c["repo"]["label"])
        self.assertEqual("issue_11907", c["repo"]["branch"])
        self.assertEqual("cockpit_pane", c["view"]["kind"])
        self.assertEqual("mozyo-cockpit", c["view"]["group"])
        self.assertTrue(c["view"]["active"])

    def test_json_normal_window_has_no_display_group(self) -> None:
        # A normal_window target has no cross-workspace cockpit group (#11907).
        rc, out = self._run([
            _pane("%2", "repo:0.0", window_name="claude", command="claude"),
        ], as_json=True)
        self.assertEqual(0, rc)
        c = json.loads(out)[0]
        self.assertEqual("normal_window", c["view"]["kind"])
        self.assertIsNone(c["view"]["group"])

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


class AgentsTargetsAttentionTest(unittest.TestCase):
    """Additive attention projection in `agents targets` (Redmine #11952).

    Pins that attention is additive (existing text columns / JSON keys stay),
    that the conservative pre-wiring default is `healthy` (reason
    `no_attention_source`) / `unknown` — never a fabricated owner/review signal —
    and the non-routing boundary.
    """

    def _run(self, panes, *, as_json=False):
        from mozyo_bridge.application import commands

        canon = argparse.Namespace(name="mozyo-bridge", workspace_id="wsA")
        args = argparse.Namespace(session=None, agent=None, as_json=as_json)
        with patch.object(commands, "require_tmux"), \
            patch("mozyo_bridge.domain.agent_discovery.pane_lines", return_value=panes), \
            patch("mozyo_bridge.domain.agent_discovery.infer_repo_root",
                  return_value="/work/repo"), \
            patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_probe_checkout_facts",
                         return_value={"branch": "main"}), \
            contextlib.redirect_stdout(io.StringIO()) as out:
            rc = commands.cmd_agents_targets(args)
        return rc, out.getvalue()

    def test_text_appends_attention_columns_with_healthy_default(self) -> None:
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude"),
        ])
        self.assertEqual(0, rc)
        # Header keeps the prior columns and appends ATTENTION / REASON.
        self.assertIn("\tVIEW_KIND\tBRANCH\tATTENTION\tREASON", out)
        # A cleanly-identified pane with no wired source derives healthy.
        self.assertIn("\thealthy\tno_attention_source", out)

    def test_json_adds_additive_attention_record(self) -> None:
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude"),
        ], as_json=True)
        self.assertEqual(0, rc)
        c = json.loads(out)[0]
        # Existing nested TargetRecord keys remain stable (additive only).
        self.assertEqual("claude", c["identity"]["role"])
        self.assertEqual("%9", c["runtime"]["pane_id"])
        # New additive attention record.
        att = c["attention"]
        self.assertEqual("healthy", att["attention_state"])
        self.assertEqual("no_attention_source", att["reason_code"])
        self.assertEqual("tmux:local:%9", att["target_key"])
        self.assertEqual("unit:local:wsA:default", att["unit_id"])
        self.assertIn("severity", att)
        self.assertIn("observed_at", att)

    def test_json_attention_never_fabricates_owner_or_review(self) -> None:
        # Conservative: with no durable source connected, no target may show an
        # owner/review/blocked/stalled state.
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude"),
            _pane("%10", "mozyo-cockpit:0.3", window_name="codex",
                  agent_role="codex"),
        ], as_json=True)
        self.assertEqual(0, rc)
        states = {c["attention"]["attention_state"] for c in json.loads(out)}
        self.assertTrue(states <= {"healthy", "unknown"}, states)


class AttentionForCandidateHelperTest(unittest.TestCase):
    """Conservative extraction mapping for one target (Redmine #11952)."""

    def _candidate(self, **over):
        from mozyo_bridge.domain.agent_discovery import TargetCandidate

        base = dict(
            pane_id="%9",
            role="claude",
            role_source="pane_option",
            confidence="strong",
            ambiguous=False,
            session="mozyo-cockpit",
            window_name="codex",
            window_index="0",
            pane_index="1",
            active=True,
            workspace_id="wsA",
            workspace_label="mozyo-bridge",
            lane_id="default",
            lane_label=None,
            repo_short="repo",
            repo_root="/work/repo",
            cwd="/work/repo",
            host="local",
            view_kind="cockpit_pane",
            branch="main",
        )
        base.update(over)
        return TargetCandidate(**base)

    def _attention(self, candidate):
        from mozyo_bridge.application.commands import _attention_for_candidate

        return _attention_for_candidate(candidate, "2026-06-15T00:00:00Z")

    def test_clean_candidate_is_healthy_no_source(self) -> None:
        rec = self._attention(self._candidate())
        self.assertEqual("healthy", rec.attention_state)
        self.assertEqual("no_attention_source", rec.reason_code)
        self.assertEqual("tmux:local:%9", rec.target_key)

    def test_ambiguous_candidate_is_unknown(self) -> None:
        rec = self._attention(self._candidate(ambiguous=True))
        self.assertEqual("unknown", rec.attention_state)
        self.assertEqual("contradictory_sources", rec.reason_code)

    def test_weak_identity_is_unknown_not_healthy(self) -> None:
        rec = self._attention(
            self._candidate(role_source="unknown", confidence="none")
        )
        self.assertEqual("unknown", rec.attention_state)
        self.assertEqual("source_unreadable", rec.reason_code)

    def test_helper_never_emits_active_attention_signal(self) -> None:
        # No durable source: the only possible derived states are healthy/unknown.
        for over in ({}, {"ambiguous": True}, {"confidence": "none",
                                               "role_source": "unknown"}):
            rec = self._attention(self._candidate(**over))
            self.assertIn(rec.attention_state, {"healthy", "unknown"})


if __name__ == "__main__":
    unittest.main()
