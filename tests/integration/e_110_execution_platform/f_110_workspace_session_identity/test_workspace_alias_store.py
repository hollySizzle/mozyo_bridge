"""Filesystem-level tests for the nested-workspace alias rail (#15190).

Covers the acceptance matrix that the pure unit tests cannot: real directories,
real git topology (including the submodule-shaped cross-repository case the
observed defect actually had), identity that survives registry loss, and the
``herdr session-start`` chokepoint itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.workspace_registry import (
    ANCHOR_SCHEMA_VERSION,
    register_workspace,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.workspace_alias import (  # noqa: E501
    git_binding,
    resolve_launch_root,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    GIT_BINDING_DIFFERENT,
    GIT_BINDING_NOT_MEASURABLE,
    GIT_BINDING_SAME,
    MODE_ALIAS,
    MODE_DISABLED,
    REASON_ALIAS_CYCLE,
    REASON_CROSS_REPOSITORY,
    REASON_DECLARATION_UNREADABLE,
    REASON_TARGET_IDENTITY_MISMATCH,
    REASON_TARGET_MISSING,
    STATE_ALIASED,
    STATE_LAUNCH_DISABLED,
    STATE_NO_DECLARATION,
    STATE_REFUSED,
    WorkspaceAliasDeclaration,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.infrastructure.workspace_alias_store import (  # noqa: E501
    alias_path,
    clear_declaration,
    read_declaration,
    write_declaration,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _write_anchor(root: Path, workspace_id: str, session: str) -> None:
    """Write an identity anchor directly.

    Deliberately anchor-only for most fixtures: the anchor is what survives home
    registry loss, so a fixture built from anchors alone is simultaneously the
    "registry recovered / registry absent" regression case.
    """
    path = root / ".mozyo-bridge" / "workspace-anchor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": ANCHOR_SCHEMA_VERSION,
                "workspace_id": workspace_id,
                "canonical_session": session,
                "project_name": root.name,
                "created_at": "2026-08-09T00:00:00+00:00",
                "updated_at": "2026-08-09T00:00:00+00:00",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


CANONICAL_ID = "ddd145b984ea4bc6ba842f72d4d4161f"
NESTED_ID = "262468a689664fb08615f56b5ef1afe1"


class AliasFixtureTestCase(unittest.TestCase):
    """One git repository holding a canonical root and a nested app root."""

    def setUp(self) -> None:
        if not _git_available():  # pragma: no cover - environment guard
            self.skipTest("git is required for workspace alias topology tests")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()

        self.canonical = self.base / "repo"
        self.canonical.mkdir()
        _git("init", "-q", cwd=self.canonical)
        self.nested = self.canonical / "Source" / "rails"
        self.nested.mkdir(parents=True)

        _write_anchor(self.canonical, CANONICAL_ID, "mozyo-repo-aaaaaaaa")
        _write_anchor(self.nested, NESTED_ID, "mozyo-rails-bbbbbbbb")

    def _declare_alias(self, *, canonical_workspace_id: str = CANONICAL_ID) -> Path:
        return write_declaration(
            self.nested,
            WorkspaceAliasDeclaration(
                mode=MODE_ALIAS,
                canonical_path=str(self.canonical),
                canonical_workspace_id=canonical_workspace_id,
                reason="rails app root is not a coordinator workspace",
            ),
        )


class ResolutionTests(AliasFixtureTestCase):
    def test_no_declaration_keeps_the_nested_root(self) -> None:
        resolution = resolve_launch_root(self.nested, home=self.home)
        self.assertEqual(resolution.state, STATE_NO_DECLARATION)
        self.assertEqual(Path(resolution.launch_root), self.nested)

    def test_declared_alias_resolves_to_the_canonical_root(self) -> None:
        self._declare_alias()
        resolution = resolve_launch_root(self.nested, home=self.home)
        self.assertEqual(resolution.state, STATE_ALIASED)
        self.assertEqual(Path(resolution.launch_root), self.canonical)
        self.assertTrue(resolution.redirected)

    def test_alias_survives_an_absent_home_registry(self) -> None:
        """Registry loss / recovery must not silently re-enable the nested pair.

        ``home`` here points at a directory with no ``registry.sqlite`` at all,
        which is exactly the state after the documented recovery procedure
        (move the registry aside, re-register from anchors).
        """
        self._declare_alias()
        empty_home = self.base / "empty-home"
        empty_home.mkdir()
        resolution = resolve_launch_root(self.nested, home=empty_home)
        self.assertEqual(resolution.state, STATE_ALIASED)
        self.assertEqual(Path(resolution.launch_root), self.canonical)

    def test_alias_holds_when_the_canonical_root_is_registry_backed(self) -> None:
        register_workspace(self.canonical, home=self.home)
        anchor = json.loads(
            (self.canonical / ".mozyo-bridge" / "workspace-anchor.json").read_text(
                encoding="utf-8"
            )
        )
        write_declaration(
            self.nested,
            WorkspaceAliasDeclaration(
                mode=MODE_ALIAS,
                canonical_path=str(self.canonical),
                canonical_workspace_id=anchor["workspace_id"],
            ),
        )
        resolution = resolve_launch_root(self.nested, home=self.home)
        self.assertEqual(resolution.state, STATE_ALIASED)
        self.assertEqual(Path(resolution.launch_root), self.canonical)

    def test_launch_disabled_is_zero_launch(self) -> None:
        write_declaration(
            self.nested,
            WorkspaceAliasDeclaration(mode=MODE_DISABLED, reason="app root only"),
        )
        resolution = resolve_launch_root(self.nested, home=self.home)
        self.assertEqual(resolution.state, STATE_LAUNCH_DISABLED)
        self.assertFalse(resolution.ok)
        self.assertEqual(resolution.launch_root, "")


class FailClosedTests(AliasFixtureTestCase):
    def _assert_refused(self, reason: str) -> None:
        resolution = resolve_launch_root(self.nested, home=self.home)
        self.assertEqual(resolution.state, STATE_REFUSED)
        self.assertEqual(resolution.reason, reason)
        self.assertEqual(resolution.launch_root, "")

    def test_missing_canonical_target(self) -> None:
        write_declaration(
            self.nested,
            WorkspaceAliasDeclaration(
                mode=MODE_ALIAS,
                canonical_path=str(self.base / "does-not-exist"),
                canonical_workspace_id=CANONICAL_ID,
            ),
        )
        self._assert_refused(REASON_TARGET_MISSING)

    def test_identity_drift_at_the_canonical_path(self) -> None:
        """The declaration named an identity; the path now resolves another."""
        self._declare_alias(canonical_workspace_id="f" * 32)
        self._assert_refused(REASON_TARGET_IDENTITY_MISMATCH)

    def test_alias_chain_is_refused(self) -> None:
        self._declare_alias()
        write_declaration(
            self.canonical,
            WorkspaceAliasDeclaration(mode=MODE_DISABLED, reason="chain"),
        )
        self._assert_refused(REASON_ALIAS_CYCLE)

    def test_corrupt_declaration_does_not_degrade_to_launching(self) -> None:
        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")
        self._assert_refused(REASON_DECLARATION_UNREADABLE)

    def test_submodule_target_is_cross_repository(self) -> None:
        """A nested checkout inside the tree is a different repository.

        This is the shape of the observed #15190 workspace pair's *parent*
        relationship, and the reason containment alone is not a sufficient test.
        """
        inner_repo = self.canonical / "vendor" / "engine"
        inner_repo.mkdir(parents=True)
        _git("init", "-q", cwd=inner_repo)
        inner_nested = inner_repo / "app"
        inner_nested.mkdir()
        _write_anchor(inner_nested, "a" * 32, "mozyo-inner-cccccccc")
        write_declaration(
            inner_nested,
            WorkspaceAliasDeclaration(
                mode=MODE_ALIAS,
                canonical_path=str(self.canonical),
                canonical_workspace_id=CANONICAL_ID,
            ),
        )
        resolution = resolve_launch_root(inner_nested, home=self.home)
        self.assertEqual(resolution.state, STATE_REFUSED)
        self.assertEqual(resolution.reason, REASON_CROSS_REPOSITORY)
        self.assertEqual(resolution.launch_root, "")


class GitBindingTests(AliasFixtureTestCase):
    def test_same_repository(self) -> None:
        self.assertEqual(git_binding(self.nested, self.canonical), GIT_BINDING_SAME)

    def test_separate_repository(self) -> None:
        other = self.base / "other-repo"
        other.mkdir()
        _git("init", "-q", cwd=other)
        self.assertEqual(git_binding(self.nested, other), GIT_BINDING_DIFFERENT)

    def test_linked_worktree_shares_the_repository(self) -> None:
        """A sublane worktree is the same repository and must stay launchable."""
        _git("config", "user.email", "t@example.invalid", cwd=self.canonical)
        _git("config", "user.name", "t", cwd=self.canonical)
        _git("add", "-A", cwd=self.canonical)
        _git("commit", "-qm", "seed", cwd=self.canonical)
        worktree = self.base / "wt"
        _git("worktree", "add", "-q", str(worktree), "-b", "lane", cwd=self.canonical)
        self.assertEqual(git_binding(worktree, self.canonical), GIT_BINDING_SAME)

    def test_two_non_git_roots_are_not_measurable(self) -> None:
        plain_parent = self.base / "plain"
        plain_child = plain_parent / "child"
        plain_child.mkdir(parents=True)
        self.assertEqual(
            git_binding(plain_child, plain_parent), GIT_BINDING_NOT_MEASURABLE
        )


class StoreTests(AliasFixtureTestCase):
    def test_round_trip(self) -> None:
        self._declare_alias()
        parsed = read_declaration(self.nested)
        self.assertIsInstance(parsed, WorkspaceAliasDeclaration)
        self.assertEqual(parsed.mode, MODE_ALIAS)
        self.assertEqual(parsed.canonical_path, str(self.canonical))

    def test_rewrite_preserves_created_at(self) -> None:
        self._declare_alias()
        first = read_declaration(self.nested)
        self._declare_alias()
        second = read_declaration(self.nested)
        self.assertEqual(first.created_at, second.created_at)

    def test_clear_removes_only_the_declaration(self) -> None:
        self._declare_alias()
        self.assertTrue(clear_declaration(self.nested))
        self.assertIsNone(read_declaration(self.nested))
        # Identity and tracked workspace content are untouched.
        self.assertTrue(
            (self.nested / ".mozyo-bridge" / "workspace-anchor.json").is_file()
        )
        self.assertFalse(clear_declaration(self.nested))

    def test_absent_declaration_reads_as_none(self) -> None:
        self.assertIsNone(read_declaration(self.nested))


class LaunchChokepointTests(AliasFixtureTestCase):
    """The `herdr session-start` entry itself, ahead of any side effect."""

    def _apply(self, root: Path) -> Path:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_alias import (  # noqa: E501
            apply_workspace_alias,
        )

        return apply_workspace_alias(root)

    def _error(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
            HerdrSessionStartError,
        )

        return HerdrSessionStartError

    def test_undeclared_root_is_passed_through_unchanged(self) -> None:
        self.assertEqual(Path(self._apply(self.nested)), self.nested)

    def test_declared_alias_is_folded_into_the_canonical_root(self) -> None:
        self._declare_alias()
        self.assertEqual(Path(self._apply(self.nested)), self.canonical)

    def test_launch_disabled_raises_before_any_side_effect(self) -> None:
        write_declaration(
            self.nested, WorkspaceAliasDeclaration(mode=MODE_DISABLED)
        )
        with self.assertRaises(self._error()) as caught:
            self._apply(self.nested)
        self.assertIn("launch-disabled", str(caught.exception))

    def test_unverifiable_alias_raises_instead_of_using_the_nested_root(self) -> None:
        self._declare_alias(canonical_workspace_id="f" * 32)
        with self.assertRaises(self._error()) as caught:
            self._apply(self.nested)
        message = str(caught.exception)
        self.assertIn(REASON_TARGET_IDENTITY_MISMATCH, message)
        self.assertNotIn("was used as a fallback", message)


class OrdinaryResolutionRegressionTests(AliasFixtureTestCase):
    """The paths that were already correct must stay correct."""

    def test_cwd_auto_resolution_is_still_git_root_first(self) -> None:
        from mozyo_bridge.shared.paths import find_repo_root

        self.assertEqual(find_repo_root(self.nested), self.canonical)

    def test_cwd_auto_resolution_unaffected_by_a_declaration(self) -> None:
        from mozyo_bridge.shared.paths import find_repo_root

        self._declare_alias()
        self.assertEqual(find_repo_root(self.nested), self.canonical)

    def test_explicit_mozyo_repo_env_is_folded_at_the_launch_chokepoint(self) -> None:
        """`MOZYO_REPO` short-circuits resolution, so the rail must catch it too."""
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_alias import (  # noqa: E501
            apply_workspace_alias,
        )
        from mozyo_bridge.shared.paths import resolve_repo_root

        self._declare_alias()
        previous = os.environ.get("MOZYO_REPO")
        os.environ["MOZYO_REPO"] = str(self.nested)
        try:
            requested = resolve_repo_root()
            self.assertEqual(requested, self.nested)
            self.assertEqual(Path(apply_workspace_alias(requested)), self.canonical)
        finally:
            if previous is None:
                os.environ.pop("MOZYO_REPO", None)
            else:
                os.environ["MOZYO_REPO"] = previous

    def test_canonical_root_itself_is_never_redirected(self) -> None:
        self._declare_alias()
        resolution = resolve_launch_root(self.canonical, home=self.home)
        self.assertEqual(resolution.state, STATE_NO_DECLARATION)
        self.assertEqual(Path(resolution.launch_root), self.canonical)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
