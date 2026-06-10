"""Home-registry-first workspace registration tests (Redmine #11429 / #11430).

Covers: registry creation on first register, canonical-session reuse over
path re-derivation, anchor-based restore after home-registry loss, Japanese /
long / non-git workspace paths, JSON schema stability, and read-only
resolution behavior. Everything runs against temp dirs — no tmux, no real
``~/.mozyo_bridge``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge import workspace_registry
from mozyo_bridge.application.commands import (
    cmd_session_name,
    cmd_workspace_inspect,
    cmd_workspace_list,
    cmd_workspace_register,
)
from mozyo_bridge.domain.session_naming import (
    SOURCE_REPO_FALLBACK,
    SOURCE_WORKSPACE_DEFAULTS,
    derive_session_name,
)
from mozyo_bridge.shared.paths import find_repo_root
from mozyo_bridge.workspace_registry import (
    ANCHOR_RELATIVE,
    REGISTER_CREATED,
    REGISTER_RESTORED,
    REGISTER_UPDATED,
    SOURCE_HOME_REGISTRY,
    SOURCE_WORKSPACE_ANCHOR,
    list_workspaces,
    load_workspace_by_path,
    read_anchor,
    register_workspace,
    registry_path,
    resolve_canonical_session,
)


class WorkspaceRegistryBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "mozyo-home"
        self.repo = base / "workspaces" / "demo-repo"
        self.repo.mkdir(parents=True)
        # CLI-level calls resolve the home from the environment.
        env_patch = patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def capture(self, func, args: argparse.Namespace) -> tuple[int, str]:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = func(args)
        return code, stream.getvalue()


class RegisterWorkspaceTest(WorkspaceRegistryBase):
    def test_first_register_creates_registry_and_anchor(self) -> None:
        self.assertFalse(registry_path(self.home).exists())
        result = register_workspace(self.repo, home=self.home)
        self.assertEqual(result.outcome, REGISTER_CREATED)
        self.assertTrue(registry_path(self.home).exists())
        self.assertTrue((self.repo / ANCHOR_RELATIVE).exists())
        record = result.record
        self.assertEqual(record.canonical_path, str(self.repo.resolve()))
        self.assertEqual(record.project_name, "demo-repo")
        # First registration derives the session name from the path.
        self.assertEqual(
            record.canonical_session, derive_session_name(self.repo).name
        )
        self.assertTrue(record.workspace_id)
        self.assertTrue(record.created_at)
        self.assertEqual(record.last_seen, record.updated_at)

    def test_reregister_is_idempotent_and_keeps_identity(self) -> None:
        first = register_workspace(self.repo, home=self.home)
        second = register_workspace(self.repo, home=self.home)
        self.assertEqual(second.outcome, REGISTER_UPDATED)
        self.assertEqual(second.record.workspace_id, first.record.workspace_id)
        self.assertEqual(
            second.record.canonical_session, first.record.canonical_session
        )
        self.assertEqual(second.record.created_at, first.record.created_at)
        self.assertEqual(len(list_workspaces(home=self.home)), 1)

    def test_canonical_session_survives_derivation_input_change(self) -> None:
        """The registered name wins even when re-derivation would now differ."""
        first = register_workspace(self.repo, home=self.home)
        self.assertEqual(first.record.canonical_session[:6], "mozyo-")
        # The operator later adds workspace-defaults: path derivation would
        # now produce a different (identifier-based) name.
        defaults = self.repo / ".mozyo-bridge" / "workspace-defaults.yaml"
        defaults.parent.mkdir(parents=True, exist_ok=True)
        defaults.write_text(
            "redmine:\n  default_project:\n    identifier: some-new-project\n",
            encoding="utf-8",
        )
        derived_now = derive_session_name(self.repo)
        self.assertEqual(derived_now.source, SOURCE_WORKSPACE_DEFAULTS)
        self.assertNotEqual(derived_now.name, first.record.canonical_session)
        resolved = resolve_canonical_session(self.repo, home=self.home)
        self.assertEqual(resolved.name, first.record.canonical_session)
        self.assertEqual(resolved.source, SOURCE_HOME_REGISTRY)
        # Re-registering also keeps the original identity.
        again = register_workspace(self.repo, home=self.home)
        self.assertEqual(
            again.record.canonical_session, first.record.canonical_session
        )

    def test_restore_from_anchor_after_registry_loss(self) -> None:
        first = register_workspace(self.repo, home=self.home)
        registry_path(self.home).unlink()
        restored = register_workspace(self.repo, home=self.home)
        self.assertEqual(restored.outcome, REGISTER_RESTORED)
        self.assertEqual(restored.record.workspace_id, first.record.workspace_id)
        self.assertEqual(
            restored.record.canonical_session, first.record.canonical_session
        )
        self.assertEqual(len(list_workspaces(home=self.home)), 1)

    def test_anchor_rewritten_when_missing(self) -> None:
        first = register_workspace(self.repo, home=self.home)
        (self.repo / ANCHOR_RELATIVE).unlink()
        second = register_workspace(self.repo, home=self.home)
        self.assertEqual(second.outcome, REGISTER_UPDATED)
        self.assertEqual(second.record.workspace_id, first.record.workspace_id)
        anchor = read_anchor(self.repo)
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["workspace_id"], first.record.workspace_id)
        self.assertIn("anchor was missing", " ".join(second.notes))

    def test_moved_workspace_keeps_identity_via_anchor(self) -> None:
        first = register_workspace(self.repo, home=self.home)
        moved = self.repo.parent / "demo-repo-moved"
        self.repo.rename(moved)
        result = register_workspace(moved, home=self.home)
        self.assertEqual(result.outcome, REGISTER_UPDATED)
        self.assertEqual(result.record.workspace_id, first.record.workspace_id)
        self.assertEqual(
            result.record.canonical_session, first.record.canonical_session
        )
        self.assertEqual(result.record.canonical_path, str(moved.resolve()))
        self.assertIn("workspace moved", " ".join(result.notes))
        self.assertEqual(len(list_workspaces(home=self.home)), 1)

    def test_stale_path_row_yields_to_anchored_identity(self) -> None:
        """A new workspace occupying an old workspace's path replaces its row."""
        old = register_workspace(self.repo, home=self.home)
        # Simulate a different workspace landing at the same path: replace the
        # anchor with a foreign identity (e.g. restored from a backup).
        anchor_file = self.repo / ANCHOR_RELATIVE
        anchor = json.loads(anchor_file.read_text(encoding="utf-8"))
        anchor["workspace_id"] = "f" * 32
        anchor_file.write_text(json.dumps(anchor), encoding="utf-8")
        result = register_workspace(self.repo, home=self.home)
        self.assertEqual(result.record.workspace_id, "f" * 32)
        records = list_workspaces(home=self.home)
        self.assertEqual(len(records), 1)
        self.assertNotEqual(records[0].workspace_id, old.record.workspace_id)
        self.assertIn("replaced stale registry row", " ".join(result.notes))

    def test_japanese_and_long_paths_register_and_restore(self) -> None:
        base = Path(self._tmp.name)
        japanese = base / "2026PBL_ローカル"
        japanese.mkdir()
        long_dir = base / ("a" * 60) / ("b" * 60) / ("c" * 60)
        long_dir.mkdir(parents=True)
        for repo in (japanese, long_dir):
            with self.subTest(repo=repo):
                created = register_workspace(repo, home=self.home)
                self.assertEqual(created.outcome, REGISTER_CREATED)
                # Non-ASCII basenames keep readable names but never produce
                # an unsafe session name (Redmine #10796 contract holds).
                self.assertRegex(
                    created.record.canonical_session, r"^mozyo-[a-z0-9-]+$"
                )
                registry_path(self.home).unlink()
                restored = register_workspace(repo, home=self.home)
                self.assertEqual(restored.outcome, REGISTER_RESTORED)
                self.assertEqual(
                    restored.record.workspace_id, created.record.workspace_id
                )
        self.assertEqual(
            json.loads(
                (japanese / ANCHOR_RELATIVE).read_text(encoding="utf-8")
            )["project_name"],
            "2026PBL_ローカル",
        )

    def test_non_git_workspace_registers(self) -> None:
        # self.repo has no .git / pyproject / scaffold marker at all.
        self.assertFalse((self.repo / ".git").exists())
        result = register_workspace(self.repo, home=self.home)
        self.assertEqual(result.outcome, REGISTER_CREATED)
        resolved = resolve_canonical_session(self.repo, home=self.home)
        self.assertEqual(resolved.source, SOURCE_HOME_REGISTRY)

    def test_subdirectory_of_registered_non_git_workspace_resolves_root(self) -> None:
        """The anchor is a workspace-root marker (Codex review #54760).

        Without it, running `mozyo-bridge session name` from a subdirectory
        of a registered non-git workspace inferred the subdirectory as the
        repo root and derived a different session name.
        """
        first = register_workspace(self.repo, home=self.home)
        sub = self.repo / "sub" / "dir"
        sub.mkdir(parents=True)
        inferred_root = find_repo_root(sub)
        self.assertEqual(inferred_root, self.repo.resolve())
        resolved = resolve_canonical_session(inferred_root, home=self.home)
        self.assertEqual(resolved.name, first.record.canonical_session)
        self.assertEqual(resolved.source, SOURCE_HOME_REGISTRY)
        # Anchor-only restore path keeps working from the subdirectory too.
        registry_path(self.home).unlink()
        resolved = resolve_canonical_session(find_repo_root(sub), home=self.home)
        self.assertEqual(resolved.name, first.record.canonical_session)
        self.assertEqual(resolved.source, SOURCE_WORKSPACE_ANCHOR)

    def test_register_records_scaffold_preset_when_available(self) -> None:
        manifest = self.repo / ".mozyo-bridge" / "scaffold.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {"preset": "redmine-governed", "preset_version": "2026.06.02.1"}
            ),
            encoding="utf-8",
        )
        record = register_workspace(self.repo, home=self.home).record
        self.assertEqual(record.preset, "redmine-governed")
        self.assertEqual(record.preset_version, "2026.06.02.1")

    def test_register_name_override(self) -> None:
        record = register_workspace(
            self.repo, home=self.home, project_name="読みやすい名前"
        ).record
        self.assertEqual(record.project_name, "読みやすい名前")
        # Re-register without override keeps the recorded name.
        again = register_workspace(self.repo, home=self.home).record
        self.assertEqual(again.project_name, "読みやすい名前")

    def test_register_rejects_missing_directory(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                register_workspace(self.repo / "nope", home=self.home)


class ResolveCanonicalSessionTest(WorkspaceRegistryBase):
    def test_unregistered_workspace_matches_pre_registry_derivation(self) -> None:
        resolved = resolve_canonical_session(self.repo, home=self.home)
        derived = derive_session_name(self.repo)
        self.assertEqual(resolved.name, derived.name)
        self.assertEqual(resolved.source, SOURCE_REPO_FALLBACK)
        self.assertIsNone(resolved.workspace_id)
        # Resolution is read-only: nothing was created.
        self.assertFalse(registry_path(self.home).exists())
        self.assertFalse((self.repo / ANCHOR_RELATIVE).exists())

    def test_anchor_wins_when_registry_missing(self) -> None:
        first = register_workspace(self.repo, home=self.home)
        registry_path(self.home).unlink()
        resolved = resolve_canonical_session(self.repo, home=self.home)
        self.assertEqual(resolved.name, first.record.canonical_session)
        self.assertEqual(resolved.source, SOURCE_WORKSPACE_ANCHOR)
        self.assertEqual(resolved.workspace_id, first.record.workspace_id)
        # Read-only: resolution does not resurrect the registry.
        self.assertFalse(registry_path(self.home).exists())

    def test_corrupt_anchor_falls_back_to_derivation(self) -> None:
        anchor_file = self.repo / ANCHOR_RELATIVE
        anchor_file.parent.mkdir(parents=True, exist_ok=True)
        anchor_file.write_text("{not json", encoding="utf-8")
        resolved = resolve_canonical_session(self.repo, home=self.home)
        self.assertEqual(resolved.name, derive_session_name(self.repo).name)

    def test_unsafe_anchor_session_name_is_rejected(self) -> None:
        anchor_file = self.repo / ANCHOR_RELATIVE
        anchor_file.parent.mkdir(parents=True, exist_ok=True)
        anchor_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace_id": "a" * 32,
                    "canonical_session": "bad:name.here",
                }
            ),
            encoding="utf-8",
        )
        resolved = resolve_canonical_session(self.repo, home=self.home)
        self.assertEqual(resolved.name, derive_session_name(self.repo).name)

    def test_corrupt_registry_read_falls_back(self) -> None:
        register_workspace(self.repo, home=self.home)
        registry_path(self.home).write_bytes(b"not a sqlite database at all")
        resolved = resolve_canonical_session(self.repo, home=self.home)
        # Anchor still carries the identity even with a damaged registry.
        self.assertEqual(resolved.source, SOURCE_WORKSPACE_ANCHOR)


class WorkspaceCliTest(WorkspaceRegistryBase):
    def _args(self, **kwargs) -> argparse.Namespace:
        defaults = {"repo": str(self.repo), "as_json": False, "name": None}
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_register_json_schema(self) -> None:
        code, out = self.capture(cmd_workspace_register, self._args(as_json=True))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(
            sorted(payload),
            ["anchor_path", "notes", "outcome", "registry_path", "workspace"],
        )
        self.assertEqual(payload["outcome"], REGISTER_CREATED)
        self.assertEqual(
            sorted(payload["workspace"]),
            [
                "canonical_path",
                "canonical_session",
                "created_at",
                "display_path",
                "last_seen",
                "preset",
                "preset_version",
                "project_name",
                "updated_at",
                "workspace_id",
            ],
        )

    def test_list_json_schema(self) -> None:
        register_workspace(self.repo, home=self.home)
        code, out = self.capture(cmd_workspace_list, self._args(as_json=True))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(sorted(payload), ["registry_path", "workspaces"])
        self.assertEqual(len(payload["workspaces"]), 1)
        self.assertEqual(
            payload["workspaces"][0]["canonical_path"], str(self.repo.resolve())
        )

    def test_list_human_output_mentions_empty_registry(self) -> None:
        code, out = self.capture(cmd_workspace_list, self._args())
        self.assertEqual(code, 0)
        self.assertIn("no workspaces registered", out)

    def test_inspect_json_shows_all_layers(self) -> None:
        register_workspace(self.repo, home=self.home)
        code, out = self.capture(cmd_workspace_inspect, self._args(as_json=True))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(
            sorted(payload),
            [
                "anchor",
                "anchor_path",
                "derived_fallback",
                "registered",
                "registry_path",
                "repo_root",
                "resolved",
            ],
        )
        self.assertEqual(payload["resolved"]["source"], SOURCE_HOME_REGISTRY)
        self.assertEqual(
            payload["resolved"]["name"],
            payload["registered"]["canonical_session"],
        )
        self.assertEqual(
            payload["anchor"]["workspace_id"],
            payload["registered"]["workspace_id"],
        )

    def test_inspect_unregistered_workspace(self) -> None:
        code, out = self.capture(cmd_workspace_inspect, self._args(as_json=True))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIsNone(payload["registered"])
        self.assertIsNone(payload["anchor"])
        self.assertEqual(
            payload["resolved"]["name"], payload["derived_fallback"]["name"]
        )

    def test_session_name_json_includes_registry_source(self) -> None:
        register_workspace(self.repo, home=self.home)
        args = argparse.Namespace(repo=str(self.repo), as_json=True)
        code, out = self.capture(cmd_session_name, args)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(
            sorted(payload),
            ["identifier", "name", "repo_root", "source", "workspace_id"],
        )
        self.assertEqual(payload["source"], SOURCE_HOME_REGISTRY)
        record = load_workspace_by_path(self.repo, home=self.home)
        self.assertEqual(payload["name"], record.canonical_session)
        self.assertEqual(payload["workspace_id"], record.workspace_id)

    def test_session_name_unregistered_keeps_legacy_shape(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), as_json=False)
        code, out = self.capture(cmd_session_name, args)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), derive_session_name(self.repo).name)


class RegistrySchemaGuardTest(WorkspaceRegistryBase):
    def test_future_schema_version_dies_on_write(self) -> None:
        register_workspace(self.repo, home=self.home)
        import sqlite3

        conn = sqlite3.connect(registry_path(self.home))
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                register_workspace(self.repo, home=self.home)

    def test_corrupt_registry_dies_on_write(self) -> None:
        registry_path(self.home).parent.mkdir(parents=True, exist_ok=True)
        registry_path(self.home).write_bytes(b"\x00" * 64)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                register_workspace(self.repo, home=self.home)


if __name__ == "__main__":
    unittest.main()
