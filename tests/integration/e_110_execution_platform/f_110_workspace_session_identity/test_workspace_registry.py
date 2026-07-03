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
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge import workspace_registry
from mozyo_bridge.application.commands import (
    cmd_session_name,
    cmd_workspace_inspect,
    cmd_workspace_list,
    cmd_workspace_register,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_naming import (
    SOURCE_REPO_FALLBACK,
    SOURCE_WORKSPACE_DEFAULTS,
    derive_session_name,
)
from mozyo_bridge.application.doctor import (
    doctor_workspace_registry_section,
    format_doctor_text,
)
from mozyo_bridge.shared.paths import find_repo_root
from mozyo_bridge.workspace_registry import (
    ANCHOR_RELATIVE,
    REGISTER_CREATED,
    REGISTER_RESTORED,
    REGISTER_UPDATED,
    REGISTRY_HEALTH_INVALID_SCHEMA,
    REGISTRY_HEALTH_MISSING,
    REGISTRY_HEALTH_OK,
    REGISTRY_HEALTH_UNREADABLE,
    SOURCE_HOME_REGISTRY,
    SOURCE_WORKSPACE_ANCHOR,
    inspect_registry_health,
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

    def test_derive_unregistered_false_skips_defaults_read(self) -> None:
        """Hot discovery degrades an unregistered workspace to the path hash.

        Redmine #12038: ``agents targets`` must not open a never-registered
        workspace's ``project-defaults.yaml`` / legacy ``workspace-defaults.yaml``
        — that ``read`` can block forever on a dataless CloudStorage placeholder.
        ``derive_unregistered=False`` must therefore never call the
        defaults-reading ``derive_session_name``.
        """
        # Even with a present defaults file that would yield an identifier name,
        # the lightweight path returns the path-hash fallback instead.
        defaults = self.repo / ".mozyo-bridge" / "workspace-defaults.yaml"
        defaults.parent.mkdir(parents=True, exist_ok=True)
        defaults.write_text(
            "redmine:\n  default_project:\n    identifier: some-project\n",
            encoding="utf-8",
        )
        with patch.object(
            workspace_registry,
            "derive_session_name",
            side_effect=AssertionError("must not read workspace defaults"),
        ):
            resolved = resolve_canonical_session(
                self.repo, home=self.home, derive_unregistered=False
            )
        self.assertEqual(resolved.source, SOURCE_REPO_FALLBACK)
        self.assertIsNone(resolved.workspace_id)
        self.assertIsNone(resolved.identifier)
        self.assertTrue(resolved.name.startswith("mozyo-demo-repo-"))

    def test_derive_unregistered_false_still_prefers_registry(self) -> None:
        """The flag only affects the never-registered branch."""
        registered = register_workspace(self.repo, home=self.home)
        resolved = resolve_canonical_session(
            self.repo, home=self.home, derive_unregistered=False
        )
        self.assertEqual(resolved.source, SOURCE_HOME_REGISTRY)
        self.assertEqual(resolved.workspace_id, registered.record.workspace_id)
        self.assertEqual(resolved.name, registered.record.canonical_session)

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
                "anchor_legacy_path",
                "anchor_name_state",
                "anchor_path",
                "derived_fallback",
                "registered",
                "registry_path",
                "repo_root",
                "resolved",
            ],
        )
        self.assertEqual(payload["anchor_name_state"], "new")
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


class InspectRegistryHealthTest(WorkspaceRegistryBase):
    """Read-only registry health probe for doctor (#11426)."""

    def test_missing_registry_is_missing_not_error(self) -> None:
        health = inspect_registry_health(self.home)
        self.assertEqual(health["status"], REGISTRY_HEALTH_MISSING)
        self.assertFalse(health["exists"])
        self.assertIsNone(health["schema_version"])

    def test_registered_registry_is_ok(self) -> None:
        register_workspace(self.repo, home=self.home)
        health = inspect_registry_health(self.home)
        self.assertEqual(health["status"], REGISTRY_HEALTH_OK)
        self.assertTrue(health["exists"])
        self.assertEqual(
            health["schema_version"], health["expected_schema_version"]
        )

    def test_corrupt_registry_is_unreadable(self) -> None:
        register_workspace(self.repo, home=self.home)
        registry_path(self.home).write_bytes(b"not a sqlite database at all")
        health = inspect_registry_health(self.home)
        self.assertEqual(health["status"], REGISTRY_HEALTH_UNREADABLE)
        self.assertIn("error", health)

    def test_future_schema_is_invalid(self) -> None:
        register_workspace(self.repo, home=self.home)
        import sqlite3

        conn = sqlite3.connect(registry_path(self.home))
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        health = inspect_registry_health(self.home)
        self.assertEqual(health["status"], REGISTRY_HEALTH_INVALID_SCHEMA)
        self.assertEqual(health["schema_version"], 99)

    def test_probe_does_not_create_registry(self) -> None:
        inspect_registry_health(self.home)
        self.assertFalse(registry_path(self.home).exists())


class DoctorWorkspaceRegistrySectionTest(WorkspaceRegistryBase):
    """The registry-aware doctor section (#11426). Read-only and additive."""

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(repo=str(self.repo), home=str(self.home))

    def _section(self, *, live: object = None) -> dict:
        # Liveness is a tmux question; patch it so the suite is hermetic and
        # never touches a real tmux server (CI parity).
        with patch(
            "mozyo_bridge.application.doctor._live_session_names",
            return_value=live,
        ):
            return doctor_workspace_registry_section(self._args())

    def test_unregistered_workspace_is_ok(self) -> None:
        section = self._section()
        self.assertEqual(section["status"], "ok")
        self.assertEqual(section["home_registry"]["status"], REGISTRY_HEALTH_MISSING)
        self.assertFalse(section["registration"]["registered"])
        self.assertEqual(section["consistency"]["status"], "unregistered")
        self.assertTrue(
            any("workspace register" in a for a in section["next_action"])
        )

    def test_registered_workspace_is_ok_and_consistent(self) -> None:
        result = register_workspace(self.repo, home=self.home)
        section = self._section()
        self.assertEqual(section["status"], "ok")
        self.assertEqual(section["home_registry"]["status"], REGISTRY_HEALTH_OK)
        self.assertTrue(section["registration"]["registered"])
        self.assertEqual(
            section["registration"]["canonical_session"],
            result.record.canonical_session,
        )
        self.assertTrue(section["anchor"]["present"])
        self.assertEqual(section["consistency"]["status"], "ok")
        self.assertEqual(section["runtime"]["last_seen"], result.record.last_seen)

    def test_anchor_only_after_registry_deletion_stays_ok(self) -> None:
        register_workspace(self.repo, home=self.home)
        registry_path(self.home).unlink()
        section = self._section()
        # Resolution still works from the anchor, so this is recoverable, not red.
        self.assertEqual(section["status"], "ok")
        self.assertEqual(
            section["home_registry"]["status"], REGISTRY_HEALTH_MISSING
        )
        self.assertFalse(section["registration"]["registered"])
        self.assertTrue(section["anchor"]["present"])
        self.assertEqual(section["consistency"]["status"], "anchor-only")
        self.assertTrue(
            any("restore it from the anchor" in a for a in section["next_action"])
        )

    def test_registry_anchor_mismatch_drifts(self) -> None:
        register_workspace(self.repo, home=self.home)
        anchor_file = self.repo / ANCHOR_RELATIVE
        raw = json.loads(anchor_file.read_text(encoding="utf-8"))
        raw["workspace_id"] = "f" * 32  # diverge from the registry row
        anchor_file.write_text(json.dumps(raw), encoding="utf-8")
        section = self._section()
        self.assertEqual(section["status"], "drifted")
        self.assertEqual(section["consistency"]["status"], "drift")
        self.assertTrue(any("reconcile" in a for a in section["next_action"]))

    def test_missing_anchor_is_registry_only_and_ok(self) -> None:
        register_workspace(self.repo, home=self.home)
        (self.repo / ANCHOR_RELATIVE).unlink()
        section = self._section()
        self.assertEqual(section["status"], "ok")
        self.assertEqual(section["consistency"]["status"], "registry-only")
        self.assertTrue(
            any("rewrite it" in a for a in section["next_action"])
        )

    def test_corrupt_registry_is_error(self) -> None:
        register_workspace(self.repo, home=self.home)
        registry_path(self.home).write_bytes(b"not a sqlite database at all")
        section = self._section()
        self.assertEqual(section["status"], "error")
        self.assertEqual(
            section["home_registry"]["status"], REGISTRY_HEALTH_UNREADABLE
        )
        # Registration is unknown (None), not falsely "unregistered".
        self.assertIsNone(section["registration"]["registered"])

    def test_future_schema_is_invalid(self) -> None:
        register_workspace(self.repo, home=self.home)
        import sqlite3

        conn = sqlite3.connect(registry_path(self.home))
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        section = self._section()
        self.assertEqual(section["status"], "invalid")
        self.assertEqual(
            section["home_registry"]["status"], REGISTRY_HEALTH_INVALID_SCHEMA
        )

    def test_runtime_active_when_session_live(self) -> None:
        result = register_workspace(self.repo, home=self.home)
        section = self._section(live={result.record.canonical_session})
        self.assertEqual(section["runtime"]["status"], "active")
        self.assertTrue(section["runtime"]["session_live"])
        # Liveness never makes the section red.
        self.assertEqual(section["status"], "ok")

    def test_runtime_stale_when_session_not_live(self) -> None:
        register_workspace(self.repo, home=self.home)
        section = self._section(live=set())
        self.assertEqual(section["runtime"]["status"], "stale")
        self.assertFalse(section["runtime"]["session_live"])
        self.assertEqual(section["status"], "ok")

    def test_runtime_unknown_when_tmux_unavailable(self) -> None:
        register_workspace(self.repo, home=self.home)
        section = self._section(live=None)
        self.assertEqual(section["runtime"]["status"], "unknown")
        self.assertIsNone(section["runtime"]["session_live"])

    def test_section_json_key_stability(self) -> None:
        register_workspace(self.repo, home=self.home)
        section = self._section()
        self.assertEqual(
            sorted(section),
            [
                "anchor",
                "consistency",
                "home_registry",
                "identity",
                "next_action",
                "registration",
                "runtime",
                "status",
                "target",
            ],
        )
        self.assertEqual(
            sorted(section["registration"]),
            [
                "canonical_session",
                "display_path",
                "preset",
                "preset_version",
                "registered",
                "workspace_id",
            ],
        )
        self.assertEqual(
            sorted(section["runtime"]),
            ["canonical_session", "last_seen", "reason", "session_live", "status"],
        )

    def test_text_output_renders_section_and_is_actionable(self) -> None:
        register_workspace(self.repo, home=self.home)
        section = self._section(live=set())
        result = {
            "ok": True,
            "sections": {
                "workspace_registry": section,
                # format_doctor_text requires a tmux section.
                "tmux": {"status": "ok", "next_action": []},
            },
        }
        text = format_doctor_text(result)
        self.assertIn("workspace_registry:", text)
        self.assertIn("home_registry:", text)
        self.assertIn("runtime:", text)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class RegisterCanonicalMoveGuardTest(WorkspaceRegistryBase):
    """Canonical-path move guard (Redmine #13152).

    Covers the three branches: a linked git worktree is refused outright; a
    move off a still-live plain checkout needs ``--move``; a move off a dead
    (removed) canonical_path is allowed.
    """

    def _init_git_repo(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        _git(path, "init", "-q")
        _git(path, "config", "user.email", "t@example.com")
        _git(path, "config", "user.name", "Test")
        (path / "README").write_text("x\n", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "init")

    def _copy_anchor(self, src: Path, dst: Path) -> None:
        """Duplicate ``src``'s anchor into ``dst`` — the tracked-anchor bug shape."""
        dst_anchor = dst / ANCHOR_RELATIVE
        dst_anchor.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / ANCHOR_RELATIVE, dst_anchor)

    def test_linked_worktree_register_is_refused(self) -> None:
        main = Path(self._tmp.name) / "gitmain"
        self._init_git_repo(main)
        first = register_workspace(main, home=self.home)

        worktree = Path(self._tmp.name) / "gitwt"
        _git(main, "worktree", "add", "-q", str(worktree))
        # The bug's shape: the worktree carries a duplicated anchor (same id).
        self._copy_anchor(main, worktree)

        with self.assertRaises(SystemExit):
            register_workspace(worktree, home=self.home)
        # Canonical path unmoved: still the main checkout.
        row = load_workspace_by_path(main, home=self.home)
        self.assertIsNotNone(row)
        self.assertEqual(row.canonical_path, str(main.resolve()))
        self.assertEqual(row.workspace_id, first.record.workspace_id)

    def test_live_canonical_move_needs_move_flag(self) -> None:
        register_workspace(self.repo, home=self.home)
        clone = Path(self._tmp.name) / "workspaces" / "clone-repo"
        clone.mkdir(parents=True)
        self._copy_anchor(self.repo, clone)

        # self.repo still exists -> refuse without --move.
        with self.assertRaises(SystemExit):
            register_workspace(clone, home=self.home)
        self.assertEqual(
            load_workspace_by_path(self.repo, home=self.home).canonical_path,
            str(self.repo.resolve()),
        )

        # Explicit --move relocates the identity.
        result = register_workspace(clone, home=self.home, allow_move=True)
        self.assertEqual(result.record.canonical_path, str(clone.resolve()))
        self.assertIn("workspace moved", " ".join(result.notes))
        self.assertIsNone(load_workspace_by_path(self.repo, home=self.home))

    def test_probe_canonical_liveness_classifies_checkouts(self) -> None:
        from mozyo_bridge.workspace_registry import probe_canonical_liveness

        # Dead path.
        dead = probe_canonical_liveness(str(Path(self._tmp.name) / "nope"))
        self.assertFalse(dead["exists"])
        self.assertFalse(dead["is_dir"])

        # Plain (non-git) directory: exists, not a git checkout.
        plain = probe_canonical_liveness(str(self.repo))
        self.assertTrue(plain["is_dir"])
        self.assertFalse(plain["is_git"])
        self.assertIsNone(plain["is_main_worktree"])

        # Real git main checkout vs a linked worktree.
        main = Path(self._tmp.name) / "gitmain2"
        self._init_git_repo(main)
        worktree = Path(self._tmp.name) / "gitwt2"
        _git(main, "worktree", "add", "-q", str(worktree))

        main_state = probe_canonical_liveness(str(main))
        self.assertTrue(main_state["is_git"])
        self.assertTrue(main_state["is_main_worktree"])

        wt_state = probe_canonical_liveness(str(worktree))
        self.assertTrue(wt_state["is_git"])
        self.assertFalse(wt_state["is_main_worktree"])

    def test_dead_canonical_move_is_allowed(self) -> None:
        first = register_workspace(self.repo, home=self.home)
        relocated = Path(self._tmp.name) / "workspaces" / "relocated-repo"
        relocated.mkdir(parents=True)
        self._copy_anchor(self.repo, relocated)
        # The original checkout is gone (genuine relocation) -> move allowed.
        shutil.rmtree(self.repo)

        result = register_workspace(relocated, home=self.home)
        self.assertEqual(result.outcome, REGISTER_UPDATED)
        self.assertEqual(result.record.workspace_id, first.record.workspace_id)
        self.assertEqual(result.record.canonical_path, str(relocated.resolve()))
        self.assertEqual(len(list_workspaces(home=self.home)), 1)


if __name__ == "__main__":
    unittest.main()
