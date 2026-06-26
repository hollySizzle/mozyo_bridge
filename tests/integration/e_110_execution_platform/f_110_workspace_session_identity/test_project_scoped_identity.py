"""Project-scoped workspace identity discovery (Redmine #12658).

Pins the policy from ``vibes/docs/logics/project-scoped-workspace-identity.md``:

- a project ``project.yaml`` candidate never replaces the Git repo root;
- only an explicit ``runtime_identity.enabled: true`` marker adopts a project
  scope (scan is advisory, adoption is explicit);
- the generated root discovery cache is keyed by stable repo-relative identity
  and a cache/source disagreement surfaces as fail-closed drift;
- a cwd is resolved to the deepest containing adopted project (workspace root ->
  no project scope, preserving single-repo display).

Everything runs against temp dirs — no tmux, no real ``~/.mozyo_bridge``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.project_scope import (
    DRIFT_FINGERPRINT,
    DRIFT_UNCACHED_SOURCE,
    ProjectScope,
    adopt_scopes,
    build_discovery_cache,
    cache_key,
    detect_cache_drift,
    parse_project_document,
    repo_relative_path,
    resolve_project_scope_for_path,
    path_under_project,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application import (
    project_discovery as pd,
)


_ENABLED_DOC = """\
schema: mozyo.project/v1
redmine_project: giken-cloud-drive-management
runtime_identity:
  enabled: true
  kind: project_scope
  display_label: "クラウドドライブ管理"
  parent_workspace: gk-3500-it-operations
  workdir: "."
"""

_ADVISORY_DOC = """\
schema: mozyo.project/v1
redmine_project: giken-some-advisory-project
display_label: Advisory Only
runtime_identity:
  enabled: false
"""

_UNMARKED_DOC = """\
name: not-a-mozyo-project
some_tool: config
"""

# Existing GK monorepo router shape (Redmine #12658 j#66473): top-level
# `schema_version` + nested `project.*`, NO runtime_identity -> discovered but not
# adopted (adoption stays explicit).
_GK_UNADOPTED_DOC = """\
schema_version: 1
project:
  redmine_project: giken-cloud-drive-management
  path: projects/giken-cloud-drive-management
  status: active
"""

# GK shape that opts in explicitly via a nested runtime_identity marker.
_GK_ADOPTED_DOC = """\
schema_version: 1
project:
  redmine_project: giken-cloud-drive-management
  path: projects/giken-cloud-drive-management
  status: active
  display_label: "クラウドドライブ管理"
  runtime_identity:
    enabled: true
    parent_workspace: gk-3500-it-operations
"""


class ParseAdoptionTests(unittest.TestCase):
    def test_enabled_marker_adopts_project_scope_with_label(self):
        candidate = parse_project_document(
            {
                "schema": "mozyo.project/v1",
                "redmine_project": "giken-cloud-drive-management",
                "runtime_identity": {
                    "enabled": True,
                    "display_label": "クラウドドライブ管理",
                    "parent_workspace": "gk-3500-it-operations",
                    "workdir": ".",
                },
            },
            path="projects/giken-cloud-drive-management",
            source="projects/giken-cloud-drive-management/project.yaml",
            raw_text=_ENABLED_DOC,
        )
        self.assertIsNotNone(candidate)
        self.assertTrue(candidate.runtime_identity_enabled)
        scope = candidate.as_scope()
        self.assertEqual(scope.scope, "giken-cloud-drive-management")
        self.assertEqual(scope.label, "クラウドドライブ管理")
        self.assertEqual(scope.parent_workspace, "gk-3500-it-operations")
        self.assertEqual(scope.workdir, "projects/giken-cloud-drive-management")

    def test_unmarked_document_is_not_a_candidate(self):
        # A file named project.yaml without the schema marker is ignored.
        self.assertIsNone(
            parse_project_document(
                {"name": "not-a-mozyo-project"},
                path="vendor/thing",
                source="vendor/thing/project.yaml",
                raw_text=_UNMARKED_DOC,
            )
        )

    def test_gk_nested_shape_discovered_but_not_adopted_without_optin(self):
        # The existing GK `schema_version: 1` + nested `project.*` shape is a
        # recognized candidate, but with no runtime_identity it is NOT adopted —
        # an existing project is never silently routable (#12658 j#66473).
        import yaml as _yaml

        candidate = parse_project_document(
            _yaml.safe_load(_GK_UNADOPTED_DOC),
            path="projects/giken-cloud-drive-management",
            source="projects/giken-cloud-drive-management/project.yaml",
            raw_text=_GK_UNADOPTED_DOC,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.scope, "giken-cloud-drive-management")
        self.assertFalse(candidate.runtime_identity_enabled)
        self.assertEqual(adopt_scopes([candidate]), [])

    def test_gk_nested_shape_adopts_with_explicit_optin(self):
        import yaml as _yaml

        candidate = parse_project_document(
            _yaml.safe_load(_GK_ADOPTED_DOC),
            path="projects/giken-cloud-drive-management",
            source="projects/giken-cloud-drive-management/project.yaml",
            raw_text=_GK_ADOPTED_DOC,
        )
        self.assertIsNotNone(candidate)
        self.assertTrue(candidate.runtime_identity_enabled)
        scope = candidate.as_scope()
        self.assertEqual(scope.scope, "giken-cloud-drive-management")
        self.assertEqual(scope.label, "クラウドドライブ管理")
        self.assertEqual(scope.parent_workspace, "gk-3500-it-operations")

    def test_advisory_candidate_is_discovered_but_not_adopted(self):
        candidate = parse_project_document(
            {
                "schema": "mozyo.project/v1",
                "redmine_project": "giken-some-advisory-project",
                "display_label": "Advisory Only",
                "runtime_identity": {"enabled": False},
            },
            path="projects/advisory",
            source="projects/advisory/project.yaml",
            raw_text=_ADVISORY_DOC,
        )
        self.assertIsNotNone(candidate)
        self.assertFalse(candidate.runtime_identity_enabled)
        self.assertEqual(adopt_scopes([candidate]), [])


class RepoRelativeTests(unittest.TestCase):
    def test_path_below_root_is_relative(self):
        self.assertEqual(
            repo_relative_path("/workspace/repo/projects/x", "/workspace/repo"),
            "projects/x",
        )

    def test_root_itself_is_dot(self):
        self.assertEqual(repo_relative_path("/workspace/repo", "/workspace/repo"), ".")

    def test_path_above_root_does_not_leak(self):
        # A path outside the repo never produces an absolute private leak.
        self.assertIsNone(repo_relative_path("/workspace/other", "/workspace/repo"))


class CwdResolutionTests(unittest.TestCase):
    def setUp(self):
        self.inner = ProjectScope(
            scope="giken-cloud-drive-management",
            path="projects/giken-cloud-drive-management",
            label="クラウドドライブ管理",
            workdir="projects/giken-cloud-drive-management",
            parent_workspace="gk-3500-it-operations",
            source="projects/giken-cloud-drive-management/project.yaml",
            fingerprint="sha256:deadbeef",
        )

    def test_cwd_inside_project_resolves_to_scope(self):
        scope = resolve_project_scope_for_path(
            "/ws/repo/projects/giken-cloud-drive-management/src",
            repo_root="/ws/repo",
            adopted=[self.inner],
        )
        self.assertIsNotNone(scope)
        self.assertEqual(scope.scope, "giken-cloud-drive-management")

    def test_repo_root_has_no_project_scope(self):
        # The Git repo root is the workspace, never a project — single-repo compat.
        self.assertIsNone(
            resolve_project_scope_for_path(
                "/ws/repo", repo_root="/ws/repo", adopted=[self.inner]
            )
        )

    def test_deepest_project_wins(self):
        outer = ProjectScope(
            scope="outer",
            path="projects",
            label="outer",
            workdir="projects",
            parent_workspace=None,
            source="projects/project.yaml",
            fingerprint="sha256:1",
        )
        scope = resolve_project_scope_for_path(
            "/ws/repo/projects/giken-cloud-drive-management/x",
            repo_root="/ws/repo",
            adopted=[outer, self.inner],
        )
        self.assertEqual(scope.scope, "giken-cloud-drive-management")

    def test_path_under_project_gate(self):
        self.assertTrue(
            path_under_project(
                "/ws/repo/projects/giken-cloud-drive-management/sub",
                repo_root="/ws/repo",
                scope=self.inner,
            )
        )
        self.assertFalse(
            path_under_project(
                "/ws/repo/projects/other",
                repo_root="/ws/repo",
                scope=self.inner,
            )
        )


class DriftTests(unittest.TestCase):
    def _candidate(self):
        return parse_project_document(
            {
                "schema": "mozyo.project/v1",
                "redmine_project": "giken-cloud-drive-management",
                "runtime_identity": {"enabled": True, "display_label": "クラウドドライブ管理"},
            },
            path="projects/giken-cloud-drive-management",
            source="projects/giken-cloud-drive-management/project.yaml",
            raw_text=_ENABLED_DOC,
        )

    def test_matching_cache_has_no_drift(self):
        cand = self._candidate()
        cache = build_discovery_cache([cand], generated_at="2026-06-27T00:00:00Z")
        self.assertEqual(detect_cache_drift(cache["entries"], [cand]), [])

    def test_fingerprint_change_surfaces_drift(self):
        cand = self._candidate()
        cache = build_discovery_cache([cand], generated_at="2026-06-27T00:00:00Z")
        stale = list(cache["entries"])
        stale[0] = {**stale[0], "fingerprint": "sha256:stale"}
        drift = detect_cache_drift(stale, [cand])
        self.assertTrue(any(d.kind == DRIFT_FINGERPRINT for d in drift))

    def test_uncached_source_surfaces_drift(self):
        cand = self._candidate()
        # A non-empty cache that lacks this project's entry must surface drift.
        drift = detect_cache_drift(
            [{"cache_key": cache_key("other", "projects/other"), "fingerprint": "x"}],
            [cand],
        )
        self.assertTrue(any(d.kind == DRIFT_UNCACHED_SOURCE for d in drift))


class FilesystemScanTests(unittest.TestCase):
    def setUp(self):
        pd.clear_discovery_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".git").mkdir()
        proj = self.repo / "projects" / "giken-cloud-drive-management"
        proj.mkdir(parents=True)
        (proj / "project.yaml").write_text(_ENABLED_DOC, encoding="utf-8")
        advisory = self.repo / "projects" / "advisory"
        advisory.mkdir(parents=True)
        (advisory / "project.yaml").write_text(_ADVISORY_DOC, encoding="utf-8")
        # A skipped directory must not contribute a candidate.
        vendored = self.repo / "node_modules" / "pkg"
        vendored.mkdir(parents=True)
        (vendored / "project.yaml").write_text(_ENABLED_DOC, encoding="utf-8")

    def tearDown(self):
        pd.clear_discovery_cache()
        self._tmp.cleanup()

    def test_scan_discovers_marked_projects_only(self):
        candidates = pd.discover_project_candidates(str(self.repo))
        scopes = {c.scope for c in candidates}
        self.assertIn("giken-cloud-drive-management", scopes)
        self.assertIn("giken-some-advisory-project", scopes)
        # node_modules is pruned.
        self.assertEqual(
            sum(c.scope == "giken-cloud-drive-management" for c in candidates), 1
        )

    def test_only_enabled_marker_is_adopted(self):
        adopted, drift = pd.resolve_project_scopes(str(self.repo))
        self.assertEqual([s.scope for s in adopted], ["giken-cloud-drive-management"])
        self.assertEqual(drift, [])

    def test_cwd_resolution_via_filesystem(self):
        cwd = self.repo / "projects" / "giken-cloud-drive-management" / "src"
        cwd.mkdir(parents=True)
        scope = pd.project_scope_for_cwd(str(cwd), str(self.repo))
        self.assertIsNotNone(scope)
        self.assertEqual(scope.scope, "giken-cloud-drive-management")
        self.assertEqual(scope.label, "クラウドドライブ管理")

    def test_repo_root_cwd_has_no_scope(self):
        self.assertIsNone(pd.project_scope_for_cwd(str(self.repo), str(self.repo)))


class NestedScaffoldGitRootTests(unittest.TestCase):
    """A nested project-local scaffold must not collapse the workspace (j#66499).

    The GK project subdir carries its own ``.mozyo-bridge/scaffold.json``; marker
    resolution would stop there. Project-scoped resolution must prefer the real
    Git worktree root so cockpit dry-run emits the Git root + a repo-relative
    project path — while a genuinely non-git scaffolded workspace still resolves
    to its scaffold root.
    """

    _DOC = (
        "schema_version: 1\n"
        "project:\n"
        "  redmine_project: giken-cloud-drive-management\n"
        "  path: projects/giken-cloud-drive-management\n"
        "  status: active\n"
        "  display_label: \"クラウドドライブ管理\"\n"
        "  runtime_identity:\n"
        "    enabled: true\n"
    )

    def setUp(self):
        pd.clear_discovery_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "gk-3500-it-operations"
        self.proj = self.repo / "projects" / "giken-cloud-drive-management"
        self.proj.mkdir(parents=True)
        (self.repo / ".git").mkdir()  # Git worktree root
        (self.proj / ".mozyo-bridge").mkdir()
        (self.proj / ".mozyo-bridge" / "scaffold.json").write_text(
            "{}", encoding="utf-8"
        )  # nested project-local scaffold marker
        (self.proj / "project.yaml").write_text(self._DOC, encoding="utf-8")

    def tearDown(self):
        pd.clear_discovery_cache()
        self._tmp.cleanup()

    def test_workspace_root_prefers_git_over_nested_scaffold(self):
        root = pd.resolve_workspace_root(str(self.proj))
        self.assertEqual(Path(root), self.repo.resolve())

    def test_cockpit_resolution_emits_git_root_and_repo_relative_project_path(self):
        # Mirrors `mozyo cockpit --repo <project subdir> --dry-run`: the resolved
        # workspace re-roots to the Git root and carries a repo-relative path, and
        # the launch cwd is the project workdir (#12658 j#66505) so the pane cwd is
        # under the project path for a `--target-project` gate.
        from mozyo_bridge.application.commands import _resolve_project_scope_fields

        cwd = str(self.proj)
        effective_root, (scope, path, label), launch_cwd = _resolve_project_scope_fields(
            cwd, cwd
        )
        self.assertEqual(Path(effective_root), self.repo.resolve())
        self.assertEqual(scope, "giken-cloud-drive-management")
        self.assertEqual(path, "projects/giken-cloud-drive-management")
        self.assertFalse(path.startswith("/"))  # repo-relative, no abs leak
        self.assertEqual(label, "クラウドドライブ管理")
        # launch cwd is the absolute project workdir (under the Git root).
        self.assertEqual(Path(launch_cwd), self.proj.resolve())

    def test_cockpit_launch_command_uses_project_workdir_not_git_root(self):
        # j#66505: a project-scoped cockpit column launches its panes at the
        # project workdir (so `--target-project` can pass) while the stamped
        # repo_root stays the Git worktree root.
        from mozyo_bridge.application.commands import _resolve_project_scope_fields
        from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
            CockpitWorkspace,
            build_cockpit_plan,
        )

        cwd = str(self.proj)
        effective_root, (scope, path, label), launch_cwd = _resolve_project_scope_fields(
            cwd, cwd
        )
        ws = CockpitWorkspace(
            workspace_id="gk-3500-it-operations",
            label="gk-3500-it-operations",
            repo_root=effective_root,
            project_scope=scope,
            project_path=path,
            project_label=label,
            launch_cwd=launch_cwd,
        )
        plan = build_cockpit_plan([ws])
        # Every pane-creating command launches with -c <project workdir>, NOT the
        # Git root.
        c_dirs = [
            cmd.argv[cmd.argv.index("-c") + 1]
            for cmd in plan.commands
            if "-c" in cmd.argv
        ]
        self.assertTrue(c_dirs)
        for d in c_dirs:
            self.assertEqual(Path(d), self.proj.resolve())
            self.assertNotEqual(Path(d), self.repo.resolve())
        # repo_root identity stays the Git root.
        self.assertEqual(Path(plan.panes[0].repo_root), self.repo.resolve())

    def test_non_git_scaffold_workspace_behavior_preserved(self):
        # A genuinely non-git scaffolded workspace (no `.git` anywhere) still
        # resolves to its own scaffold root — the fallback is preserved.
        with tempfile.TemporaryDirectory() as tmp2:
            ws = Path(tmp2) / "scaffolded-workspace"
            (ws / ".mozyo-bridge").mkdir(parents=True)
            (ws / ".mozyo-bridge" / "scaffold.json").write_text("{}", encoding="utf-8")
            (ws / "src").mkdir()
            root = pd.resolve_workspace_root(str(ws / "src"))
            self.assertEqual(Path(root), ws.resolve())


class DriftFailClosedTests(unittest.TestCase):
    """Runtime lookup must fail closed on generated-cache drift (j#66481 blocker 3)."""

    _DOC = _ENABLED_DOC

    def setUp(self):
        pd.clear_discovery_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".git").mkdir()
        self.proj = self.repo / "projects" / "giken-cloud-drive-management"
        self.proj.mkdir(parents=True)
        (self.proj / "project.yaml").write_text(self._DOC, encoding="utf-8")
        (self.proj / "src").mkdir()

    def tearDown(self):
        pd.clear_discovery_cache()
        self._tmp.cleanup()

    def _write_cache(self, fingerprint: str):
        # A generated root projects.yaml whose discovery_cache entry disagrees
        # with the live source fingerprint.
        (self.repo / "projects.yaml").write_text(
            "projects: {}\n"
            "discovery_cache:\n"
            "  generated_by: mozyo-bridge project discovery\n"
            "  generated_at: \"2026-06-27T00:00:00Z\"\n"
            "  entries:\n"
            "    - cache_key: \"project:giken-cloud-drive-management@projects/giken-cloud-drive-management\"\n"
            "      source: projects/giken-cloud-drive-management/project.yaml\n"
            "      path: projects/giken-cloud-drive-management\n"
            "      redmine_project: giken-cloud-drive-management\n"
            "      display_label: \"クラウドドライブ管理\"\n"
            "      runtime_identity_enabled: true\n"
            f"      fingerprint: \"{fingerprint}\"\n",
            encoding="utf-8",
        )

    def test_no_cache_projects_scope_normally(self):
        # Sanity: no generated cache -> no drift -> scope projects.
        scope = pd.project_scope_for_cwd(str(self.proj / "src"), str(self.repo))
        self.assertIsNotNone(scope)

    def test_drifted_cache_fails_closed_no_projection(self):
        import contextlib
        import io

        self._write_cache("sha256:stale-does-not-match")
        pd.clear_discovery_cache()
        # resolve_project_scopes reports drift...
        adopted, drift = pd.resolve_project_scopes(str(self.repo))
        self.assertTrue(drift)
        # ...and the runtime-facing lookup refuses to project a scope (the
        # expected fail-closed diagnostic goes to stderr; capture it for clean
        # test output).
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pd.adopted_scopes_for_repo(str(self.repo)), ())
            self.assertIsNone(
                pd.project_scope_for_cwd(str(self.proj / "src"), str(self.repo))
            )
        self.assertIn("cache drift", err.getvalue())


if __name__ == "__main__":
    unittest.main()
