"""Project-scoped workspace identity discovery (Redmine #12658).

Pins the policy from ``vibes/docs/logics/project-scoped-workspace-identity.md``:

- a project ``project.env`` candidate never replaces the Git repo root;
- only an explicit ``PROJECT_RUNTIME_IDENTITY_ENABLED=true`` marker adopts a project
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
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
    parse_project_environment,
    repo_relative_path,
    resolve_project_scope_for_path,
    path_under_project,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application import (
    project_discovery as pd,
)

_ENABLED_DOC = """\
PROJECT_SCHEMA=mozyo.project/v1
PROJECT_REDMINE_PROJECT=giken-cloud-drive-management
PROJECT_RUNTIME_IDENTITY_ENABLED=true
PROJECT_RUNTIME_IDENTITY_KIND=project_scope
PROJECT_DISPLAY_LABEL=クラウドドライブ管理
PROJECT_PARENT_WORKSPACE=gk-3500-it-operations
PROJECT_WORKDIR=.
"""

_ADVISORY_DOC = """\
PROJECT_SCHEMA=mozyo.project/v1
PROJECT_REDMINE_PROJECT=giken-some-advisory-project
PROJECT_DISPLAY_LABEL="Advisory Only"
PROJECT_RUNTIME_IDENTITY_ENABLED=false
"""

_UNMARKED_DOC = """\
PROJECT_NAME=not-a-mozyo-project
SOME_TOOL=config
"""

_UNADOPTED_DOC = """\
PROJECT_SCHEMA=mozyo.project/v1
PROJECT_REDMINE_PROJECT=giken-cloud-drive-management
PROJECT_DISPLAY_LABEL=クラウドドライブ管理
PROJECT_RUNTIME_IDENTITY_ENABLED=false
"""


class ParseAdoptionTests(unittest.TestCase):
    def test_enabled_marker_adopts_project_scope_with_label(self):
        candidate = parse_project_environment(
            {
                "PROJECT_SCHEMA": "mozyo.project/v1",
                "PROJECT_REDMINE_PROJECT": "giken-cloud-drive-management",
                "PROJECT_RUNTIME_IDENTITY_ENABLED": "true",
                "PROJECT_DISPLAY_LABEL": "クラウドドライブ管理",
                "PROJECT_PARENT_WORKSPACE": "gk-3500-it-operations",
                "PROJECT_WORKDIR": ".",
            },
            path="projects/giken-cloud-drive-management",
            source="projects/giken-cloud-drive-management/project.env",
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
        # A file named project.env without the schema marker is ignored.
        self.assertIsNone(
            parse_project_environment(
                {"PROJECT_NAME": "not-a-mozyo-project"},
                path="vendor/thing",
                source="vendor/thing/project.env",
                raw_text=_UNMARKED_DOC,
            )
        )

    def test_descriptor_is_discovered_but_not_adopted_without_optin(self):
        candidate = parse_project_environment(
            {
                "PROJECT_SCHEMA": "mozyo.project/v1",
                "PROJECT_REDMINE_PROJECT": "giken-cloud-drive-management",
                "PROJECT_RUNTIME_IDENTITY_ENABLED": "false",
            },
            path="projects/giken-cloud-drive-management",
            source="projects/giken-cloud-drive-management/project.env",
            raw_text=_UNADOPTED_DOC,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.scope, "giken-cloud-drive-management")
        self.assertFalse(candidate.runtime_identity_enabled)
        self.assertEqual(adopt_scopes([candidate]), [])

    def test_advisory_candidate_is_discovered_but_not_adopted(self):
        candidate = parse_project_environment(
            {
                "PROJECT_SCHEMA": "mozyo.project/v1",
                "PROJECT_REDMINE_PROJECT": "giken-some-advisory-project",
                "PROJECT_DISPLAY_LABEL": "Advisory Only",
                "PROJECT_RUNTIME_IDENTITY_ENABLED": "false",
            },
            path="projects/advisory",
            source="projects/advisory/project.env",
            raw_text=_ADVISORY_DOC,
        )
        self.assertIsNotNone(candidate)
        self.assertFalse(candidate.runtime_identity_enabled)
        self.assertEqual(adopt_scopes([candidate]), [])


class EnvFileParserTests(unittest.TestCase):
    def test_comments_blank_lines_and_quoted_values_are_supported(self):
        parsed = pd._parse_project_env(
            "# identity\n\n"
            "PROJECT_SCHEMA=mozyo.project/v1\n"
            'PROJECT_DISPLAY_LABEL="社内 基盤"\n'
        )
        self.assertEqual(
            parsed,
            {
                "PROJECT_SCHEMA": "mozyo.project/v1",
                "PROJECT_DISPLAY_LABEL": "社内 基盤",
            },
        )

    def test_duplicate_key_is_rejected(self):
        self.assertIsNone(
            pd._parse_project_env(
                "PROJECT_SCHEMA=mozyo.project/v1\n" "PROJECT_SCHEMA=mozyo.project/v2\n"
            )
        )

    def test_interpolation_is_rejected(self):
        self.assertIsNone(
            pd._parse_project_env(
                "PROJECT_SCHEMA=mozyo.project/v1\n"
                "PROJECT_PARENT_WORKSPACE=${WORKSPACE}\n"
            )
        )

    def test_legacy_yaml_is_not_a_project_env(self):
        self.assertIsNone(
            pd._parse_project_env(
                "schema_version: 1\n" "project:\n" "  redmine_project: legacy\n"
            )
        )


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
            source="projects/giken-cloud-drive-management/project.env",
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
            source="projects/project.env",
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
        return parse_project_environment(
            {
                "PROJECT_SCHEMA": "mozyo.project/v1",
                "PROJECT_REDMINE_PROJECT": "giken-cloud-drive-management",
                "PROJECT_RUNTIME_IDENTITY_ENABLED": "true",
                "PROJECT_DISPLAY_LABEL": "クラウドドライブ管理",
            },
            path="projects/giken-cloud-drive-management",
            source="projects/giken-cloud-drive-management/project.env",
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
        (proj / "project.env").write_text(_ENABLED_DOC, encoding="utf-8")
        advisory = self.repo / "projects" / "advisory"
        advisory.mkdir(parents=True)
        (advisory / "project.env").write_text(_ADVISORY_DOC, encoding="utf-8")
        # A skipped directory must not contribute a candidate.
        vendored = self.repo / "node_modules" / "pkg"
        vendored.mkdir(parents=True)
        (vendored / "project.env").write_text(_ENABLED_DOC, encoding="utf-8")

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
        "PROJECT_SCHEMA=mozyo.project/v1\n"
        "PROJECT_REDMINE_PROJECT=giken-cloud-drive-management\n"
        "PROJECT_DISPLAY_LABEL=クラウドドライブ管理\n"
        "PROJECT_RUNTIME_IDENTITY_ENABLED=true\n"
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
        (self.proj / "project.env").write_text(self._DOC, encoding="utf-8")

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
        effective_root, (scope, path, label), launch_cwd = (
            _resolve_project_scope_fields(cwd, cwd)
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
        effective_root, (scope, path, label), launch_cwd = (
            _resolve_project_scope_fields(cwd, cwd)
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
        (self.proj / "project.env").write_text(self._DOC, encoding="utf-8")
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
            '  generated_at: "2026-06-27T00:00:00Z"\n'
            "  entries:\n"
            '    - cache_key: "project:giken-cloud-drive-management@projects/giken-cloud-drive-management"\n'
            "      source: projects/giken-cloud-drive-management/project.env\n"
            "      path: projects/giken-cloud-drive-management\n"
            "      redmine_project: giken-cloud-drive-management\n"
            '      display_label: "クラウドドライブ管理"\n'
            "      runtime_identity_enabled: true\n"
            f'      fingerprint: "{fingerprint}"\n',
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


class ScanProgressTests(unittest.TestCase):
    """#12985: the live bounded scan is observable through the injectable listener.

    The previously-silent `os.walk` behind `agents targets` project-scope
    enrichment emits `scan_start` / `scan_slow` / `scan_done` events to an
    installed listener; the memoized cache-hit path and every caller that
    installs no listener stay exactly as quiet as before, and a broken listener
    can never change discovery results.
    """

    def setUp(self):
        pd.clear_discovery_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".git").mkdir()
        proj = self.repo / "projects" / "giken-cloud-drive-management"
        proj.mkdir(parents=True)
        (proj / "project.env").write_text(_ENABLED_DOC, encoding="utf-8")

    def tearDown(self):
        pd.clear_discovery_cache()
        self._tmp.cleanup()

    def test_live_scan_emits_start_and_done_and_memoized_hit_is_silent(self):
        events = []
        with pd.scan_progress(events.append):
            first = pd.adopted_scopes_for_repo(str(self.repo))
            second = pd.adopted_scopes_for_repo(str(self.repo))  # memoized hit
        self.assertEqual(first, second)
        self.assertEqual(
            [e.kind for e in events],
            [pd.SCAN_PROGRESS_START, pd.SCAN_PROGRESS_DONE],
        )
        self.assertEqual(events[0].repo_root, str(self.repo))
        self.assertEqual(events[0].elapsed_seconds, 0.0)
        self.assertEqual(events[1].adopted_count, 1)
        self.assertGreaterEqual(events[1].elapsed_seconds, 0.0)

    def test_no_listener_emits_nothing_and_scan_still_adopts(self):
        scopes = pd.adopted_scopes_for_repo(str(self.repo))
        self.assertEqual([s.scope for s in scopes], ["giken-cloud-drive-management"])

    def test_slow_scan_emits_one_still_scanning_event(self):
        events = []
        real_resolve = pd.resolve_project_scopes

        def slow_resolve(repo_root, *, max_depth):
            time.sleep(0.25)
            return real_resolve(repo_root, max_depth=max_depth)

        with patch.object(pd, "resolve_project_scopes", slow_resolve):
            with pd.scan_progress(events.append, slow_after=0.01):
                pd.adopted_scopes_for_repo(str(self.repo))
        self.assertEqual(
            [e.kind for e in events],
            [pd.SCAN_PROGRESS_START, pd.SCAN_PROGRESS_SLOW, pd.SCAN_PROGRESS_DONE],
        )
        self.assertGreater(events[1].elapsed_seconds, 0.0)

    def test_fast_scan_never_emits_still_scanning(self):
        events = []
        with pd.scan_progress(events.append, slow_after=30.0):
            pd.adopted_scopes_for_repo(str(self.repo))
        self.assertNotIn(pd.SCAN_PROGRESS_SLOW, [e.kind for e in events])

    def test_listener_exception_never_breaks_discovery(self):
        def broken(_event):
            raise RuntimeError("listener boom")

        with pd.scan_progress(broken):
            scopes = pd.adopted_scopes_for_repo(str(self.repo))
        self.assertEqual([s.scope for s in scopes], ["giken-cloud-drive-management"])

    def test_listener_uninstalled_after_block(self):
        events = []
        with pd.scan_progress(events.append):
            pass
        pd.adopted_scopes_for_repo(str(self.repo))
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
