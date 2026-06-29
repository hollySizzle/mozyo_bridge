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

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from types import SimpleNamespace

from mozyo_bridge.application.agent_discovery_port import (
    AgentDiscoveryPort,
    LiveAgentDiscovery,
)
from mozyo_bridge.application.commands_agents import ResolveAgentTargetsUseCase
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    AgentRecord,
    build_target_candidates,
    discover_agents,
    fold_agents_by_pane,
)


def _pane(pane_id, location, *, command="node", cwd="/work/repo",
          window_name="cockpit", pane_active="1", agent_role="",
          lane_id="", lane_label="", lane_kind="", delegation_parent="",
          project_scope="", project_path="", project_label="",
          repo_root_stamp=""):
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
        "lane_kind": lane_kind,
        "delegation_parent": delegation_parent,
        "project_scope": project_scope,
        "project_path": project_path,
        "project_label": project_label,
        "repo_root_stamp": repo_root_stamp,
    }


class BuildTargetCandidatesTest(unittest.TestCase):
    def _records(self, panes):
        # Fold with a fixed repo root so workspace resolution can fire.
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.infer_repo_root",
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

    def test_stamped_project_scope_is_projected(self) -> None:
        # A pane carrying `@mozyo_project_scope` (cockpit-managed) projects the
        # project scope alongside the workspace identity (#12658).
        records = self._records([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="claude",
                  project_scope="giken-cloud-drive-management",
                  project_path="projects/giken-cloud-drive-management",
                  project_label="クラウドドライブ管理"),
        ])
        cands = build_target_candidates(
            records, resolve_workspace=lambda root: ("wsGK", "gk-3500-it-operations")
        )
        c = cands[0]
        self.assertEqual("gk-3500-it-operations", c.workspace_label)
        self.assertEqual("giken-cloud-drive-management", c.project_scope)
        self.assertEqual("projects/giken-cloud-drive-management", c.project_path)
        self.assertEqual("クラウドドライブ管理", c.project_label)
        # JSON projection nests project scope under identity; never an abs path.
        identity = c.to_dict()["identity"]
        self.assertEqual(identity["project_scope"], "giken-cloud-drive-management")
        self.assertFalse(c.project_path.startswith("/"))

    def test_unstamped_pane_derives_project_scope_from_cwd(self) -> None:
        # A normal `mozyo` pane (no stamp) derives its scope from its cwd via the
        # injected resolver, so it still projects project scope.
        records = self._records([
            _pane("%2", "repo:0.0", window_name="claude", command="claude"),
        ])
        def resolve_project(repo_root, cwd):
            return ("giken-cloud-drive-management", "projects/giken-cloud-drive-management", "クラウドドライブ管理")
        cands = build_target_candidates(records, resolve_project=resolve_project)
        self.assertEqual(cands[0].project_scope, "giken-cloud-drive-management")

    def test_stamped_repo_root_preserves_parent_workspace_for_project_pane(self) -> None:
        # Redmine #12658 j#66513: a project-scoped cockpit pane launches with its
        # cwd at the project workdir, but a stamped `@mozyo_repo_root` (Git root)
        # must keep its parent workspace identity instead of collapsing onto the
        # project subdir — so workspace identity and project scope show together.
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
            discover_agents,
        )

        panes = [
            _pane(
                "%28", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude",
                cwd="/ws/gk-3500-it-operations/projects/giken-cloud-drive-management",
                repo_root_stamp="/ws/gk-3500-it-operations",
                project_scope="giken-cloud-drive-management",
                project_path="projects/giken-cloud-drive-management",
                project_label="クラウドドライブ管理",
            ),
        ]
        # The record's repo_root is the STAMPED Git root, not the cwd-derived
        # project subdir (no infer_repo_root patch here — the stamp wins).
        records = discover_agents(panes)
        self.assertEqual(records[0].repo_root, "/ws/gk-3500-it-operations")
        # Workspace identity resolves off the Git root while project scope rides
        # alongside it.
        cands = build_target_candidates(
            records,
            resolve_workspace=lambda root: (
                ("wsGK", "gk-3500-it-operations")
                if root == "/ws/gk-3500-it-operations"
                else (None, None)
            ),
        )
        c = cands[0]
        self.assertEqual(c.repo_root, "/ws/gk-3500-it-operations")
        self.assertEqual(c.workspace_label, "gk-3500-it-operations")
        self.assertEqual(c.project_scope, "giken-cloud-drive-management")
        self.assertEqual(c.project_path, "projects/giken-cloud-drive-management")

    def test_single_repo_pane_has_no_project_scope(self) -> None:
        # No stamp and a resolver that finds nothing -> empty project scope, so a
        # single-repo workspace is unchanged (additive null in JSON).
        records = self._records([
            _pane("%2", "repo:0.0", window_name="claude", command="claude"),
        ])
        cands = build_target_candidates(records, resolve_project=lambda r, c: None)
        self.assertEqual(cands[0].project_scope, "")
        self.assertIsNone(cands[0].to_dict()["identity"]["project_scope"])

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
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.pane_lines", return_value=panes), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.infer_repo_root", return_value=repo_root), \
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

    def test_json_adds_additive_delegation_window_projection(self) -> None:
        # #12467: agents targets --json gains a `delegation_window` projection
        # sibling to the #12466 `delegation` record. With no repo-local config the
        # policy is the documented default `separate`, so a derived grandchild
        # (depth 2) projects to its own window.
        rc, out = self._run([
            _pane("%1", "mozyo-cockpit:0.0", window_name="codex",
                  agent_role="codex", lane_id="lane-root",
                  lane_kind="coordinator"),
            _pane("%2", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="codex", lane_id="lane-deleg",
                  lane_kind="delegated_coordinator",
                  delegation_parent="wsA/lane-root"),
            _pane("%3", "mozyo-cockpit:0.2", window_name="claude",
                  agent_role="claude", lane_id="lane-impl",
                  lane_kind="implementation",
                  delegation_parent="wsA/lane-deleg"),
        ], as_json=True)
        self.assertEqual(0, rc)
        payload = json.loads(out)
        by_pane = {c["runtime"]["pane_id"]: c for c in payload}
        # #12466 record stays present and unchanged alongside the new sibling.
        self.assertIn("delegation", by_pane["%3"])
        win = by_pane["%3"]["delegation_window"]
        self.assertEqual("separate", win["window_policy"])
        self.assertTrue(win["window_separated"])
        self.assertEqual("wsA/lane-impl", win["window_group"])
        self.assertEqual("resolved", win["window_status"])
        # Root coordinator is its own top-of-tree window under any policy.
        self.assertTrue(by_pane["%1"]["delegation_window"]["window_separated"])

    def test_gateway_projection_distinguishes_project_gateway_from_root(self) -> None:
        # #12708: agents targets surfaces the live gateway identity. A
        # project-scoped Codex projects `project_gateway`; a Codex with no project
        # scope projects `workspace_root` (the department root the GK3500 smoke
        # mistook for a gateway). Text gains a final TARGET_KIND column.
        rc, out = self._run([
            _pane("%gw", "mozyo-cockpit:0.0", window_name="codex",
                  agent_role="codex", project_scope="giken-cloud-drive-management",
                  project_path="projects/giken-cloud-drive-management",
                  project_label="クラウドドライブ管理"),
            _pane("%root", "mozyo-cockpit:0.1", window_name="codex",
                  agent_role="codex"),
        ])
        self.assertEqual(0, rc)
        self.assertIn("\tTARGET_KIND", out)  # header column appended
        lines = {ln.split("\t", 1)[0]: ln for ln in out.splitlines()}
        self.assertTrue(lines["%gw"].endswith("\tproject_gateway"))
        self.assertTrue(lines["%root"].endswith("\tworkspace_root"))

    def test_json_gateway_record_flags_the_project_gateway(self) -> None:
        # #12708: --json gains an additive `gateway` record per target.
        rc, out = self._run([
            _pane("%gw", "mozyo-cockpit:0.0", window_name="codex",
                  agent_role="codex", project_scope="giken-cloud-drive-management",
                  project_label="クラウドドライブ管理"),
        ], as_json=True)
        self.assertEqual(0, rc)
        c = json.loads(out)[0]
        self.assertEqual("project_gateway", c["gateway"]["target_kind"])
        self.assertTrue(c["gateway"]["is_project_gateway"])
        self.assertEqual("giken-cloud-drive-management", c["gateway"]["project_scope"])
        # Non-authoritative: the gateway record is additive only, never folded
        # into the canonical TargetRecord routing projection.
        self.assertNotIn("gateway", c["identity"])

    def test_delegation_window_is_not_in_canonical_target_record(self) -> None:
        # Non-authoritative: the window projection rides as an additive sibling
        # only; the canonical TargetRecord (host/runtime/identity/repo/view) that
        # routing/preflight consume never carries a window policy field (#12467).
        rc, out = self._run([
            _pane("%3", "mozyo-cockpit:0.2", window_name="claude",
                  agent_role="claude", lane_id="lane-impl",
                  lane_kind="implementation",
                  delegation_parent="wsA/lane-deleg"),
        ], as_json=True)
        self.assertEqual(0, rc)
        c = json.loads(out)[0]
        self.assertIn("delegation_window", c)  # additive sibling present
        for section in ("host", "runtime", "identity", "repo", "view"):
            self.assertNotIn("window_policy", c[section])
            self.assertNotIn("window_separated", c[section])
            self.assertNotIn("window_group", c[section])

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


class AgentsTargetsUnregisteredDefaultsTest(unittest.TestCase):
    """`agents targets` must not read unrelated workspace defaults (#12038).

    A pane in a never-registered workspace previously fell through
    ``resolve_canonical_session`` to ``derive_session_name``, which opens the
    workspace-local ``project-defaults.yaml`` / legacy ``workspace-defaults.yaml``.
    When that file is a dataless CloudStorage placeholder the ``read`` blocks
    forever and the whole listing hangs. These tests run the *real*
    ``resolve_canonical_session`` (not the stub the other command tests use) and
    pin that discovery degrades to the path-hash fallback instead of reading
    defaults.
    """

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "mozyo-home"
        self.home.mkdir()
        self.repo = base / "unrelated-cloud-repo"
        self.repo.mkdir()
        env_patch = patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run(self):
        from mozyo_bridge.application import commands
        from mozyo_bridge import workspace_registry

        args = argparse.Namespace(session=None, agent=None, as_json=False)
        # `derive_session_name` is the defaults-reading branch; make it explode
        # so the test fails loudly if the hot path ever reaches it again. The
        # registry is empty (fresh home) and the repo is unregistered, so the
        # only thing standing between discovery and a hang is the fix.
        with patch.object(commands, "require_tmux"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.pane_lines",
                  return_value=[_pane("%9", "mozyo-cockpit:0.1",
                                      window_name="codex", agent_role="claude",
                                      cwd=str(self.repo))]), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.infer_repo_root",
                  return_value=str(self.repo)), \
            patch.object(workspace_registry, "derive_session_name",
                         side_effect=AssertionError(
                             "must not read workspace defaults")), \
            patch.object(commands, "_probe_checkout_facts",
                         return_value={"branch": "main"}), \
            contextlib.redirect_stdout(io.StringIO()) as out:
            rc = commands.cmd_agents_targets(args)
        return rc, out.getvalue()

    def test_unregistered_workspace_degrades_instead_of_reading_defaults(self) -> None:
        rc, out = self._run()
        self.assertEqual(0, rc)
        # The pane is still listed (degraded, not dropped); the path-hash
        # fallback session name is what surfaces in the WORKSPACE column.
        self.assertIn("%9\tclaude", out)
        self.assertIn("mozyo-unrelated-cloud-repo-", out)

    def test_present_defaults_file_is_not_opened(self) -> None:
        # Even with a defaults file physically present, the hot path must not
        # open it (the real risk is a placeholder whose read blocks).
        defaults = self.repo / ".mozyo-bridge" / "workspace-defaults.yaml"
        defaults.parent.mkdir(parents=True, exist_ok=True)
        defaults.write_text(
            "redmine:\n  default_project:\n    identifier: unrelated\n",
            encoding="utf-8",
        )
        rc, out = self._run()
        self.assertEqual(0, rc)
        # The identifier-derived name (`mozyo-unrelated`) must NOT appear; the
        # path-hash fallback wins because defaults were never read.
        self.assertIn("mozyo-unrelated-cloud-repo-", out)
        self.assertNotIn("\tmozyo-unrelated\t", out)


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
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.pane_lines", return_value=panes), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.infer_repo_root",
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

    def test_duplicate_group_window_names_stay_healthy_not_contradictory(self):
        # #12336 end-to-end: two Project Group windows sharing one display name
        # (#12330) each hold strong pane-option panes in distinct lanes. The
        # duplicate display name must not flip them to AMBIG=1 /
        # contradictory_sources; every pane stays healthy with a clean identity.
        rc, out = self._run([
            _pane("%81", "mozyo-cockpit:2.0", window_name="mozyo_bridge",
                  agent_role="codex", lane_id="lane-A"),
            _pane("%82", "mozyo-cockpit:2.1", window_name="mozyo_bridge",
                  agent_role="claude", lane_id="lane-A"),
            _pane("%83", "mozyo-cockpit:3.0", window_name="mozyo_bridge",
                  agent_role="codex", lane_id="lane-B"),
            _pane("%84", "mozyo-cockpit:3.1", window_name="mozyo_bridge",
                  agent_role="claude", lane_id="lane-B"),
        ], as_json=True)
        self.assertEqual(0, rc)
        candidates = json.loads(out)
        self.assertEqual(4, len(candidates))
        for c in candidates:
            self.assertFalse(c["identity"]["ambiguous"], c["runtime"]["pane_id"])
            self.assertEqual("pane_option", c["identity"]["role_source"])
            self.assertEqual("strong", c["identity"]["confidence"])
            self.assertEqual("healthy", c["attention"]["attention_state"])
            self.assertEqual("no_attention_source", c["attention"]["reason_code"])


class AttentionForCandidateHelperTest(unittest.TestCase):
    """Conservative extraction mapping for one target (Redmine #11952)."""

    def _candidate(self, **over):
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import TargetCandidate

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


class DeriveTargetsDelegationTest(unittest.TestCase):
    """Display-only delegated-coordinator-tree projection (Redmine #12466).

    Pins :func:`derive_targets_delegation`, which consumes the closed #12465
    ``delegation_projection`` foundation. Derivation is fail-soft and strictly
    non-authoritative: it never raises, never blocks the table, and carries no
    routing / handoff / approval / close field.
    """

    def _cand(self, pane_id, *, lane_id, lane_kind="", delegation_parent="",
              workspace_id="wsA", role="codex"):
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import TargetCandidate

        return TargetCandidate(
            pane_id=pane_id,
            role=role,
            role_source="pane_option",
            confidence="strong",
            ambiguous=False,
            session="mozyo-cockpit",
            window_name="cockpit",
            window_index="0",
            pane_index="0",
            active=True,
            workspace_id=workspace_id,
            workspace_label="mozyo-bridge",
            lane_id=lane_id,
            lane_label=None,
            repo_short="repo",
            repo_root="/work/repo",
            cwd="/work/repo",
            host="local",
            view_kind="cockpit_pane",
            branch="main",
            lane_kind=lane_kind,
            delegation_parent=delegation_parent,
        )

    def _three_level_tree(self):
        # parent (0) -> delegated (1) -> grandchild (2); the parent pointer uses
        # the `<workspace_id>/<lane_id>` unit format the display defines.
        return [
            self._cand("%1", lane_id="root", lane_kind="coordinator"),
            self._cand("%2", lane_id="deleg", lane_kind="delegated_coordinator",
                       delegation_parent="wsA/root"),
            self._cand("%3", lane_id="gc", lane_kind="implementation",
                       delegation_parent="wsA/deleg"),
        ]

    def test_no_delegation_facts_is_blank_none_status(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display import derive_targets_delegation

        out = derive_targets_delegation([self._cand("%1", lane_id="default")])
        deleg = out["%1"]
        self.assertEqual("none", deleg.status)
        self.assertEqual("", deleg.lane_kind)
        self.assertIsNone(deleg.delegation_depth)
        self.assertEqual("", deleg.delegation_parent)
        self.assertEqual("", deleg.delegation_root)

    def test_derives_kind_depth_parent_root_for_three_level_tree(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display import derive_targets_delegation

        out = derive_targets_delegation(self._three_level_tree())

        self.assertEqual("coordinator", out["%1"].lane_kind)
        self.assertEqual(0, out["%1"].delegation_depth)
        self.assertEqual("", out["%1"].delegation_parent)
        self.assertEqual("wsA/root", out["%1"].delegation_root)
        self.assertEqual("derived", out["%1"].status)

        self.assertEqual("delegated_coordinator", out["%2"].lane_kind)
        self.assertEqual(1, out["%2"].delegation_depth)
        self.assertEqual("wsA/root", out["%2"].delegation_parent)
        self.assertEqual("wsA/root", out["%2"].delegation_root)

        self.assertEqual("implementation", out["%3"].lane_kind)
        self.assertEqual(2, out["%3"].delegation_depth)
        self.assertEqual("wsA/deleg", out["%3"].delegation_parent)
        self.assertEqual("wsA/root", out["%3"].delegation_root)

    def test_two_panes_in_one_lane_share_the_derived_unit(self) -> None:
        # A lane's codex gateway + claude worker share `<workspace_id>/<lane_id>`;
        # both panes resolve to the same derived breadcrumb (no duplicate-unit
        # failure in the foundation).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display import derive_targets_delegation

        cands = [
            self._cand("%1", lane_id="root", lane_kind="coordinator", role="codex"),
            self._cand("%2", lane_id="deleg", lane_kind="delegated_coordinator",
                       delegation_parent="wsA/root", role="codex"),
            self._cand("%3", lane_id="deleg", lane_kind="delegated_coordinator",
                       delegation_parent="wsA/root", role="claude"),
        ]
        out = derive_targets_delegation(cands)
        self.assertEqual(out["%2"].as_payload(), out["%3"].as_payload())
        self.assertEqual(1, out["%2"].delegation_depth)

    def test_off_contract_lane_kind_is_diagnostic_not_authoritative(self) -> None:
        # An off-contract kind must not be emitted as a healthy projection value
        # (mirrors the foundation's fail-closed boundary) but must not crash the
        # display either: it degrades to a diagnostic row.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display import derive_targets_delegation

        out = derive_targets_delegation([self._cand("%1", lane_id="x", lane_kind="manager")])
        deleg = out["%1"]
        self.assertEqual("diagnostic", deleg.status)
        self.assertEqual("", deleg.lane_kind)  # off-contract kind is withheld
        self.assertIsNone(deleg.delegation_depth)

    def test_unknown_parent_pointer_fails_soft_to_diagnostic(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display import derive_targets_delegation

        out = derive_targets_delegation([
            self._cand("%1", lane_id="deleg", lane_kind="delegated_coordinator",
                       delegation_parent="wsA/ghost"),
        ])
        self.assertEqual("diagnostic", out["%1"].status)
        self.assertIsNone(out["%1"].delegation_depth)
        # The contract kind is still shown so the broken breadcrumb is visible.
        self.assertEqual("delegated_coordinator", out["%1"].lane_kind)

    def test_depth_beyond_shallow_maximum_fails_soft(self) -> None:
        # parent -> delegated -> grandchild -> great-grandchild (depth 3 > 2).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display import derive_targets_delegation

        cands = self._three_level_tree() + [
            self._cand("%4", lane_id="ggc", lane_kind="implementation",
                       delegation_parent="wsA/gc"),
        ]
        out = derive_targets_delegation(cands)
        # The over-deep tree is rejected wholesale -> every in-tree unit diagnostic.
        self.assertEqual("diagnostic", out["%4"].status)
        self.assertIsNone(out["%4"].delegation_depth)

    def test_conflicting_panes_in_one_lane_are_diagnostic(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display import derive_targets_delegation

        cands = [
            self._cand("%1", lane_id="deleg", lane_kind="coordinator"),
            self._cand("%2", lane_id="deleg", lane_kind="implementation"),
        ]
        out = derive_targets_delegation(cands)
        self.assertEqual("diagnostic", out["%1"].status)
        self.assertEqual("diagnostic", out["%2"].status)

    def test_payload_carries_no_routing_or_close_authority_field(self) -> None:
        # The display breadcrumb must never grow a routing / approval / close key
        # (same non-authoritative contract as the #12465 foundation).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display import derive_targets_delegation

        payload = derive_targets_delegation(self._three_level_tree())["%2"].as_payload()
        self.assertEqual(
            {"lane_kind", "delegation_depth", "delegation_parent",
             "delegation_root", "status"},
            set(payload),
        )
        forbidden = ("target", "route", "routing", "send", "approval",
                     "close", "gateway", "preflight")
        for key in payload:
            self.assertFalse(
                any(token in key for token in forbidden),
                f"display field {key!r} must not look like a routing key",
            )

    def test_canonical_target_record_excludes_delegation(self) -> None:
        # The routing-facing TargetRecord projection (`to_dict`) is unchanged: the
        # delegation breadcrumb is additive (like attention), never folded into
        # the identity/repo/view vocabulary used for handoff target selection.
        cand = self._cand("%2", lane_id="deleg", lane_kind="delegated_coordinator",
                          delegation_parent="wsA/root")
        record = cand.to_dict()
        flat = json.dumps(record)
        self.assertNotIn("delegation_depth", flat)
        self.assertNotIn("delegation_root", flat)
        self.assertNotIn("lane_kind", flat)


class AgentsTargetsDelegationColumnsTest(unittest.TestCase):
    """The ``agents targets`` CLI surfaces the delegation breadcrumb (#12466)."""

    def _run(self, panes, *, as_json=False, workspace_id="wsA",
             label="mozyo-bridge", repo_root="/work/repo", branch="issue_12466"):
        from mozyo_bridge.application import commands

        canon = argparse.Namespace(name=label, workspace_id=workspace_id)
        args = argparse.Namespace(session=None, agent=None, as_json=as_json)
        with patch.object(commands, "require_tmux"), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.pane_lines", return_value=panes), \
            patch("mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.infer_repo_root", return_value=repo_root), \
            patch.object(commands, "resolve_canonical_session", return_value=canon), \
            patch.object(commands, "_probe_checkout_facts", return_value={"branch": branch}), \
            contextlib.redirect_stdout(io.StringIO()) as out:
            rc = commands.cmd_agents_targets(args)
        return rc, out.getvalue()

    def _tree_panes(self):
        return [
            _pane("%1", "mozyo-cockpit:0.0", window_name="codex", agent_role="codex",
                  lane_id="root", lane_kind="coordinator"),
            _pane("%2", "mozyo-cockpit:1.0", window_name="codex", agent_role="codex",
                  lane_id="deleg", lane_kind="delegated_coordinator",
                  delegation_parent="wsA/root"),
            _pane("%3", "mozyo-cockpit:2.0", window_name="claude", agent_role="claude",
                  lane_id="gc", lane_kind="implementation",
                  delegation_parent="wsA/deleg"),
        ]

    def test_text_appends_kind_depth_parent_columns(self) -> None:
        rc, out = self._run(self._tree_panes())
        self.assertEqual(0, rc)
        # Appended after the existing ATTENTION / REASON run so existing column
        # positions stay valid for current parsers.
        self.assertIn("ATTENTION\tREASON\tKIND\tDEPTH\tPARENT", out)
        self.assertIn("\tdelegated_coordinator\t1\twsA/root", out)
        self.assertIn("\timplementation\t2\twsA/deleg", out)
        # The root coordinator shows depth 0 and a blank parent cell.
        self.assertIn("\tcoordinator\t0\t-", out)

    def test_text_blank_columns_when_no_delegation_facts(self) -> None:
        rc, out = self._run([
            _pane("%9", "mozyo-cockpit:0.1", window_name="codex", agent_role="claude"),
        ])
        self.assertEqual(0, rc)
        # No delegation / project facts -> PARENT / PROJECT / PROJECT_PATH cells
        # are blank; the #12708 TARGET_KIND column then names the derived kind
        # (a Claude pane is a `worker`), so it is the final trailing column.
        self.assertTrue(out.rstrip().endswith("\t-\t-\t-\tworker"))

    def test_json_adds_additive_delegation_record(self) -> None:
        rc, out = self._run(self._tree_panes(), as_json=True)
        self.assertEqual(0, rc)
        payload = json.loads(out)
        by_pane = {row["runtime"]["pane_id"]: row for row in payload}
        deleg = by_pane["%3"]["delegation"]
        self.assertEqual("implementation", deleg["lane_kind"])
        self.assertEqual(2, deleg["delegation_depth"])
        self.assertEqual("wsA/deleg", deleg["delegation_parent"])
        self.assertEqual("wsA/root", deleg["delegation_root"])
        self.assertEqual("derived", deleg["status"])


class _FakeAgentDiscovery:
    """Fake :class:`AgentDiscoveryPort` for use-case specs (Redmine #12785).

    Replaces the read-discovery monkeypatch seam (``commands.resolve_canonical_session``
    / ``commands._probe_checkout_facts`` + live discovery): the four external reads
    are injected, so ``ResolveAgentTargetsUseCase`` is exercised without patching
    the ``commands`` module. ``discover`` builds raw records from explicit panes
    (``discover_agents`` accepts a panes argument), so no live tmux read occurs.
    """

    def __init__(self, panes, *, canon, branch, project=None):
        self._panes = panes
        self._canon = canon
        self._branch = branch
        self._project = project

    def discover(self):
        return discover_agents(self._panes)

    def canonical_session(self, repo_root):
        return self._canon

    def checkout_facts(self, repo_root):
        return {"branch": self._branch}

    def project_scope(self, cwd, repo_root):
        return self._project


class ResolveAgentTargetsUseCaseFakePortTest(unittest.TestCase):
    def test_fake_and_live_satisfy_port(self) -> None:
        fake = _FakeAgentDiscovery([], canon=None, branch=None)
        self.assertIsInstance(fake, AgentDiscoveryPort)
        self.assertIsInstance(LiveAgentDiscovery(), AgentDiscoveryPort)

    def test_invalid_agent_filter_dies_before_discovery(self) -> None:
        use_case = ResolveAgentTargetsUseCase(
            _FakeAgentDiscovery([], canon=None, branch=None)
        )
        with self.assertRaises(SystemExit):
            use_case.resolve(agent_filter="bogus", session_filter=None)

    def test_resolves_candidate_through_injected_port(self) -> None:
        canon = SimpleNamespace(name="mozyo-bridge", workspace_id="wsA")
        discovery = _FakeAgentDiscovery(
            [
                _pane("%9", "mozyo-cockpit:0.1", window_name="codex",
                      agent_role="claude", lane_id="lane-abc", lane_label="feature/x"),
            ],
            canon=canon,
            branch="main",
        )
        # infer_repo_root is a fold internal (filesystem walk), not the external
        # discovery boundary; stub it so the fold has a deterministic repo root.
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.infer_repo_root",
            return_value="/work/repo",
        ):
            cands = ResolveAgentTargetsUseCase(discovery).resolve(
                agent_filter=None, session_filter=None
            )
        self.assertEqual(1, len(cands))
        c = cands[0]
        self.assertEqual("%9", c.pane_id)
        self.assertEqual("claude", c.role)
        # workspace from canonical_session port read; branch from checkout_facts port.
        self.assertEqual("wsA", c.workspace_id)
        self.assertEqual("mozyo-bridge", c.workspace_label)
        self.assertEqual("main", c.branch)

    def test_unknown_panes_yield_no_candidates(self) -> None:
        discovery = _FakeAgentDiscovery(
            [_pane("%1", "s:0.0", window_name="shell", command="zsh")],
            canon=SimpleNamespace(name="mozyo-bridge", workspace_id="wsA"),
            branch=None,
        )
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery.infer_repo_root",
            return_value="/work/repo",
        ):
            cands = ResolveAgentTargetsUseCase(discovery).resolve(
                agent_filter=None, session_filter=None
            )
        self.assertEqual([], cands)


if __name__ == "__main__":
    unittest.main()
