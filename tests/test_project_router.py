"""Project-router delegation resolution tests (Redmine #12438).

US #12437 delegates external-submodule work to a canonical project's Codex as a
``delegated_coordinator``. These tests pin the pure, fail-closed core in
``mozyo_bridge.domain.project_router``:

- ``resolve_delegation_target`` reads gk-style ``projects.yaml`` routing metadata
  (mapping and list shapes, key aliases) and fails closed on absent /
  non-external-submodule / no-canonical-root projects.
- ``select_delegation_codex_pane`` resolves the unique Codex gateway by
  canonical-repo-root match and fails closed on absent (operator action) /
  ambiguous targets, never selecting an ambiguous candidate.
- ``delegated_coordinator_profile_fields`` derives the role-profile placeholder
  values with explicit overrides winning.

No live tmux / filesystem is required: the config is an in-memory mapping and the
candidates are lightweight fakes that duck-type the selector's contract.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.project_router import (  # noqa: E402
    CODE_AMBIGUOUS_TARGET,
    CODE_MALFORMED_CONFIG,
    CODE_NOT_EXTERNAL_SUBMODULE,
    CODE_NO_CANONICAL_ROOT,
    CODE_NO_TARGET,
    CODE_PROJECT_NOT_FOUND,
    DelegationTarget,
    ProjectRouterError,
    delegated_coordinator_profile_fields,
    normalize_repo_root,
    resolve_delegation_target,
    select_delegation_codex_pane,
)


@dataclass(frozen=True)
class FakeCandidate:
    """Minimal duck-typed stand-in for a discovered target candidate."""

    role: str
    repo_root: str | None
    pane_id: str
    ambiguous: bool = False


# A canonical, redaction-safe routing config in the mapping shape.
MAPPING_CONFIG = {
    "project": "gk-3500-it-operations",
    "projects": {
        "giken-3800-mozyo-bridge": {
            "classification": "external-submodule",
            "reference_kind": "external_dependency_reference",
            "canonical": {
                "repo_root": "/workspace/project-alpha",
                "project": "giken-3800-mozyo-bridge",
                "redmine_project": "giken-3800-mozyo-bridge",
            },
        },
        "internal-thing": {
            "classification": "internal",
        },
    },
}


class ResolveDelegationTargetTest(unittest.TestCase):
    def test_mapping_shape_resolves_canonical_metadata(self) -> None:
        target = resolve_delegation_target(MAPPING_CONFIG, "giken-3800-mozyo-bridge")
        self.assertEqual(target.target_project, "giken-3800-mozyo-bridge")
        self.assertEqual(target.classification, "external-submodule")
        self.assertEqual(target.canonical_repo_root, "/workspace/project-alpha")
        self.assertEqual(target.child_project, "giken-3800-mozyo-bridge")
        self.assertEqual(target.redmine_project, "giken-3800-mozyo-bridge")
        self.assertEqual(target.parent_project, "gk-3500-it-operations")

    def test_list_shape_and_flat_canonical_keys(self) -> None:
        config = {
            "id": "gk-3500-it-operations",
            "projects": [
                {
                    "id": "giken-3800-mozyo-bridge",
                    "kind": "external_submodule",
                    "canonical_repo_root": "/workspace/project-alpha",
                    "redmine_project": "giken-3800-mozyo-bridge",
                }
            ],
        }
        target = resolve_delegation_target(config, "giken-3800-mozyo-bridge")
        self.assertEqual(target.canonical_repo_root, "/workspace/project-alpha")
        self.assertEqual(target.redmine_project, "giken-3800-mozyo-bridge")
        self.assertEqual(target.parent_project, "gk-3500-it-operations")

    def test_classification_with_spaces_and_case(self) -> None:
        config = {
            "projects": {
                "x": {
                    "classification": "External-Submodule",
                    "canonical": {"repo_root": "/workspace/project-alpha"},
                }
            }
        }
        target = resolve_delegation_target(config, "x")
        self.assertEqual(target.canonical_repo_root, "/workspace/project-alpha")
        # child_project defaults to the entry id when not separately declared.
        self.assertEqual(target.child_project, "x")
        self.assertIsNone(target.redmine_project)

    def test_absent_project_fails_closed(self) -> None:
        with self.assertRaises(ProjectRouterError) as ctx:
            resolve_delegation_target(MAPPING_CONFIG, "no-such-project")
        self.assertEqual(ctx.exception.code, CODE_PROJECT_NOT_FOUND)

    def test_non_external_submodule_fails_closed(self) -> None:
        with self.assertRaises(ProjectRouterError) as ctx:
            resolve_delegation_target(MAPPING_CONFIG, "internal-thing")
        self.assertEqual(ctx.exception.code, CODE_NOT_EXTERNAL_SUBMODULE)

    def test_missing_canonical_root_fails_closed(self) -> None:
        config = {
            "projects": {
                "x": {"classification": "external-submodule"},
            }
        }
        with self.assertRaises(ProjectRouterError) as ctx:
            resolve_delegation_target(config, "x")
        self.assertEqual(ctx.exception.code, CODE_NO_CANONICAL_ROOT)

    def test_no_classification_fails_closed(self) -> None:
        config = {"projects": {"x": {"canonical": {"repo_root": "/workspace/a"}}}}
        with self.assertRaises(ProjectRouterError) as ctx:
            resolve_delegation_target(config, "x")
        self.assertEqual(ctx.exception.code, CODE_NOT_EXTERNAL_SUBMODULE)

    def test_malformed_config_fails_closed(self) -> None:
        with self.assertRaises(ProjectRouterError) as ctx:
            resolve_delegation_target(None, "x")
        self.assertEqual(ctx.exception.code, CODE_MALFORMED_CONFIG)

        with self.assertRaises(ProjectRouterError) as ctx:
            resolve_delegation_target({"nothing": True}, "x")
        self.assertEqual(ctx.exception.code, CODE_MALFORMED_CONFIG)

    def test_empty_target_project_fails_closed(self) -> None:
        with self.assertRaises(ProjectRouterError) as ctx:
            resolve_delegation_target(MAPPING_CONFIG, "")
        self.assertEqual(ctx.exception.code, CODE_MALFORMED_CONFIG)


class SelectDelegationCodexPaneTest(unittest.TestCase):
    CANON = "/workspace/project-alpha"

    def test_unique_codex_in_canonical_repo_selected(self) -> None:
        candidates = [
            FakeCandidate("claude", self.CANON, "%1"),
            FakeCandidate("codex", self.CANON, "%2"),
            FakeCandidate("codex", "/workspace/project-beta", "%3"),
        ]
        chosen = select_delegation_codex_pane(
            candidates, canonical_repo_root=self.CANON
        )
        self.assertEqual(chosen.pane_id, "%2")

    def test_no_codex_in_repo_is_no_target(self) -> None:
        candidates = [
            FakeCandidate("claude", self.CANON, "%1"),
            FakeCandidate("codex", "/workspace/project-beta", "%3"),
        ]
        with self.assertRaises(ProjectRouterError) as ctx:
            select_delegation_codex_pane(candidates, canonical_repo_root=self.CANON)
        self.assertEqual(ctx.exception.code, CODE_NO_TARGET)

    def test_multiple_codex_in_repo_is_ambiguous(self) -> None:
        candidates = [
            FakeCandidate("codex", self.CANON, "%2"),
            FakeCandidate("codex", self.CANON, "%4"),
        ]
        with self.assertRaises(ProjectRouterError) as ctx:
            select_delegation_codex_pane(candidates, canonical_repo_root=self.CANON)
        self.assertEqual(ctx.exception.code, CODE_AMBIGUOUS_TARGET)

    def test_ambiguous_candidate_is_never_selected(self) -> None:
        # The only matching Codex pane is ambiguous -> fail closed, not selected.
        candidates = [
            FakeCandidate("codex", self.CANON, "%2", ambiguous=True),
        ]
        with self.assertRaises(ProjectRouterError) as ctx:
            select_delegation_codex_pane(candidates, canonical_repo_root=self.CANON)
        self.assertEqual(ctx.exception.code, CODE_AMBIGUOUS_TARGET)

    def test_ambiguous_excluded_then_unique_usable_selected(self) -> None:
        candidates = [
            FakeCandidate("codex", self.CANON, "%2", ambiguous=True),
            FakeCandidate("codex", self.CANON, "%5"),
        ]
        chosen = select_delegation_codex_pane(
            candidates, canonical_repo_root=self.CANON
        )
        self.assertEqual(chosen.pane_id, "%5")

    def test_repo_root_match_normalizes_trailing_slash(self) -> None:
        candidates = [
            FakeCandidate("codex", self.CANON + "/", "%2"),
        ]
        chosen = select_delegation_codex_pane(
            candidates, canonical_repo_root=self.CANON
        )
        self.assertEqual(chosen.pane_id, "%2")

    def test_candidate_without_repo_root_ignored(self) -> None:
        candidates = [
            FakeCandidate("codex", None, "%2"),
        ]
        with self.assertRaises(ProjectRouterError) as ctx:
            select_delegation_codex_pane(candidates, canonical_repo_root=self.CANON)
        self.assertEqual(ctx.exception.code, CODE_NO_TARGET)


class NormalizeRepoRootTest(unittest.TestCase):
    def test_trailing_slash_and_dotdot_normalized(self) -> None:
        self.assertEqual(
            normalize_repo_root("/workspace/project-alpha/"),
            normalize_repo_root("/workspace/project-alpha"),
        )
        self.assertEqual(
            normalize_repo_root("/workspace/x/../project-alpha"),
            normalize_repo_root("/workspace/project-alpha"),
        )

    def test_empty_is_none(self) -> None:
        self.assertIsNone(normalize_repo_root(""))
        self.assertIsNone(normalize_repo_root(None))


class DelegatedCoordinatorProfileFieldsTest(unittest.TestCase):
    def _target(self, **overrides) -> DelegationTarget:
        base = dict(
            target_project="giken-3800-mozyo-bridge",
            classification="external-submodule",
            canonical_repo_root="/workspace/project-alpha",
            child_project="giken-3800-mozyo-bridge",
            redmine_project="giken-3800-mozyo-bridge",
            parent_project="gk-3500-it-operations",
        )
        base.update(overrides)
        return DelegationTarget(**base)

    def test_fields_derived_from_target(self) -> None:
        fields = delegated_coordinator_profile_fields(self._target())
        self.assertEqual(fields["child_project"], "giken-3800-mozyo-bridge")
        self.assertEqual(fields["parent_project"], "gk-3500-it-operations")
        self.assertEqual(fields["redmine_project"], "giken-3800-mozyo-bridge")
        # parent_issue / parent_callback_target are runtime-supplied, not in config.
        self.assertNotIn("parent_issue", fields)
        self.assertNotIn("parent_callback_target", fields)

    def test_explicit_overrides_win(self) -> None:
        fields = delegated_coordinator_profile_fields(
            self._target(),
            parent_project="override-parent",
            parent_issue="12437",
            parent_callback_target="coordinator",
        )
        self.assertEqual(fields["parent_project"], "override-parent")
        self.assertEqual(fields["parent_issue"], "12437")
        self.assertEqual(fields["parent_callback_target"], "coordinator")

    def test_missing_optional_config_fields_omitted(self) -> None:
        fields = delegated_coordinator_profile_fields(
            self._target(redmine_project=None, parent_project=None)
        )
        self.assertEqual(set(fields), {"child_project"})


if __name__ == "__main__":
    unittest.main()
