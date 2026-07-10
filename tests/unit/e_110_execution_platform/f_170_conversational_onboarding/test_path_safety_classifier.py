"""Pure path-safety classifier matrix (Redmine #13508).

The classifier is carved out for independent task-level review, so its matrix
lives in its own file: home / normal / sync / symlink-ambiguity / Git / non-Git,
plus the adoption-marker probe. All fixtures use ``tempfile`` and an injected
``home`` so nothing depends on the runner's real home directory.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.path_safety import (
    ADOPTION_ABSENT,
    ADOPTION_CONFIG,
    ADOPTION_ONBOARDING_RECEIPT,
    ADOPTION_SCAFFOLD,
    ADOPTION_WORKSPACE_ANCHOR,
    MOUNT_CONFLICTING,
    MOUNT_LOCAL,
    MOUNT_NETWORK,
    MOUNT_SYNC_CLOUD,
    MOUNT_UNAVAILABLE,
    PATH_RISK_AMBIGUOUS,
    PATH_RISK_HOME,
    PATH_RISK_NORMAL,
    PATH_RISK_SYNC_OR_CLOUD,
    ROOT_KIND_GIT,
    ROOT_KIND_NON_GIT,
    MountFacts,
    classify_path_safety,
    platform_sync_roots,
)


class _FakeMountProbe:
    """A deterministic MountProbe returning a fixed classification."""

    def __init__(self, state: str, detail: str = "") -> None:
        self._facts = MountFacts(state=state, source="fake", detail=detail)

    def classify_mount(self, path):  # noqa: ANN001 - Port shape
        return self._facts


def _mk(base: Path, rel: str) -> None:
    target = base / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")


class PathSafetyRootKindTests(unittest.TestCase):
    def test_git_root_is_classified_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            (root / ".git").mkdir(parents=True)
            safety = classify_path_safety(root, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.root_kind, ROOT_KIND_GIT)
            self.assertEqual(safety.path_risk, PATH_RISK_NORMAL)

    def test_non_git_root_is_classified_non_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir(parents=True)
            safety = classify_path_safety(root, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.root_kind, ROOT_KIND_NON_GIT)


class PathSafetyRiskTests(unittest.TestCase):
    def test_home_directory_is_hard_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            safety = classify_path_safety(home, home=home, sync_roots=())
            self.assertEqual(safety.path_risk, PATH_RISK_HOME)
            self.assertTrue(safety.is_hard_block)

    def test_home_wins_even_when_home_is_under_a_sync_root(self) -> None:
        # Home is a hard block regardless of sync membership.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            safety = classify_path_safety(home, home=home, sync_roots=(home,))
            self.assertEqual(safety.path_risk, PATH_RISK_HOME)

    def test_sync_root_prefix_is_caution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sync = Path(tmp) / "home" / "Library" / "CloudStorage" / "GoogleDrive-x"
            root = sync / "project"
            root.mkdir(parents=True)
            safety = classify_path_safety(
                root, home=Path(tmp) / "home", sync_roots=(Path(tmp) / "home" / "Library" / "CloudStorage",)
            )
            self.assertEqual(safety.path_risk, PATH_RISK_SYNC_OR_CLOUD)
            self.assertTrue(safety.requires_caution_ack)
            self.assertFalse(safety.is_hard_block)

    def test_sync_detected_by_provider_component_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Dropbox" / "project"
            root.mkdir(parents=True)
            # No sync_roots supplied — name-based detection must still fire.
            safety = classify_path_safety(root, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.path_risk, PATH_RISK_SYNC_OR_CLOUD)

    def test_normal_root_is_normal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "code" / "project"
            root.mkdir(parents=True)
            safety = classify_path_safety(root, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.path_risk, PATH_RISK_NORMAL)


class PathSafetyAmbiguityTests(unittest.TestCase):
    def test_dangling_symlink_is_ambiguous_not_normal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link"
            os.symlink(Path(tmp) / "does_not_exist", link)
            safety = classify_path_safety(link, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.path_risk, PATH_RISK_AMBIGUOUS)
            self.assertTrue(safety.is_hard_block)

    def test_missing_path_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            safety = classify_path_safety(
                Path(tmp) / "nope", home=Path(tmp) / "home", sync_roots=()
            )
            self.assertEqual(safety.path_risk, PATH_RISK_AMBIGUOUS)

    def test_file_target_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "afile"
            f.write_text("x", encoding="utf-8")
            safety = classify_path_safety(f, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.path_risk, PATH_RISK_AMBIGUOUS)

    def test_symlink_to_real_dir_resolves_and_is_not_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            os.symlink(real, link)
            safety = classify_path_safety(link, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.path_risk, PATH_RISK_NORMAL)
            self.assertEqual(safety.root, real.resolve())


class PathSafetyAdoptionMarkerTests(unittest.TestCase):
    def _classify(self, tmp: str, rel: str | None) -> str:
        root = Path(tmp) / "proj"
        root.mkdir(parents=True, exist_ok=True)
        if rel:
            _mk(root, rel)
        return classify_path_safety(
            root, home=Path(tmp) / "home", sync_roots=()
        ).adoption_marker

    def test_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._classify(tmp, None), ADOPTION_ABSENT)

    def test_config_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self._classify(tmp, ".mozyo-bridge/config.yaml"), ADOPTION_CONFIG
            )

    def test_scaffold_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self._classify(tmp, ".mozyo-bridge/scaffold.json"), ADOPTION_SCAFFOLD
            )

    def test_workspace_anchor_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self._classify(tmp, ".mozyo-bridge/workspace-anchor.json"),
                ADOPTION_WORKSPACE_ANCHOR,
            )

    def test_onboarding_receipt_is_most_specific(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir(parents=True)
            _mk(root, ".mozyo-bridge/config.yaml")
            _mk(root, ".mozyo-bridge/onboarding-receipt.json")
            marker = classify_path_safety(
                root, home=Path(tmp) / "home", sync_roots=()
            ).adoption_marker
            self.assertEqual(marker, ADOPTION_ONBOARDING_RECEIPT)


class GitAncestryTests(unittest.TestCase):
    """F2: root_kind follows Git worktree ancestry, not just a `.git` at root."""

    def test_git_at_root_is_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / ".git").mkdir(parents=True)
            safety = classify_path_safety(root, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.root_kind, ROOT_KIND_GIT)

    def test_nested_cwd_under_git_root_is_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            nested = repo / "pkg" / "sub"
            nested.mkdir(parents=True)
            safety = classify_path_safety(nested, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.root_kind, ROOT_KIND_GIT)

    def test_linked_worktree_dot_git_file_is_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lw = Path(tmp) / "linked"
            lw.mkdir()
            (lw / ".git").write_text("gitdir: /somewhere/.git/worktrees/lw\n", encoding="utf-8")
            safety = classify_path_safety(lw, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.root_kind, ROOT_KIND_GIT)

    def test_non_git_tree_is_non_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plain" / "nested"
            root.mkdir(parents=True)
            safety = classify_path_safety(root, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.root_kind, ROOT_KIND_NON_GIT)


class MountMetadataTests(unittest.TestCase):
    """F1: mount metadata is a sync signal, and unavailable/conflicting → ambiguous."""

    def _classify(self, tmp, state):
        root = Path(tmp) / "proj"
        root.mkdir(parents=True, exist_ok=True)
        return classify_path_safety(
            root,
            home=Path(tmp) / "home",
            sync_roots=(),
            mount_probe=_FakeMountProbe(state),
        )

    def test_sync_cloud_mount_is_caution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            safety = self._classify(tmp, MOUNT_SYNC_CLOUD)
            self.assertEqual(safety.path_risk, PATH_RISK_SYNC_OR_CLOUD)

    def test_network_mount_is_caution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            safety = self._classify(tmp, MOUNT_NETWORK)
            self.assertEqual(safety.path_risk, PATH_RISK_SYNC_OR_CLOUD)

    def test_known_local_mount_is_normal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            safety = self._classify(tmp, MOUNT_LOCAL)
            self.assertEqual(safety.path_risk, PATH_RISK_NORMAL)

    def test_unavailable_metadata_is_ambiguous_not_normal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            safety = self._classify(tmp, MOUNT_UNAVAILABLE)
            self.assertEqual(safety.path_risk, PATH_RISK_AMBIGUOUS)
            self.assertTrue(safety.is_hard_block)

    def test_conflicting_metadata_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            safety = self._classify(tmp, MOUNT_CONFLICTING)
            self.assertEqual(safety.path_risk, PATH_RISK_AMBIGUOUS)

    def test_path_prefix_signal_wins_over_local_mount(self) -> None:
        # A provider-name path signal is authoritative sync even if the mount
        # probe reports local (deliberate positive signal, not a conflict).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Dropbox" / "project"
            root.mkdir(parents=True)
            safety = classify_path_safety(
                root,
                home=Path(tmp) / "home",
                sync_roots=(),
                mount_probe=_FakeMountProbe(MOUNT_LOCAL),
            )
            self.assertEqual(safety.path_risk, PATH_RISK_SYNC_OR_CLOUD)

    def test_no_probe_falls_back_to_path_signals(self) -> None:
        # Backward-compatible: without a probe, a plain local path is normal.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "code"
            root.mkdir()
            safety = classify_path_safety(root, home=Path(tmp) / "home", sync_roots=())
            self.assertEqual(safety.path_risk, PATH_RISK_NORMAL)


class PlatformSyncRootsTests(unittest.TestCase):
    def test_includes_known_mac_roots(self) -> None:
        home = Path("/Users/example")
        roots = platform_sync_roots(home)
        self.assertIn(home / "Library" / "CloudStorage", roots)
        self.assertIn(home / "Library" / "Mobile Documents", roots)


if __name__ == "__main__":
    unittest.main()
