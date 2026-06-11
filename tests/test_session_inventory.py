"""Cross-workspace session inventory tests (Redmine #11422).

Covers: runtime collection from synthetic tmux pane lines, pane_id identity
folding for grouped sessions (Redmine #11628), Unicode-normalization-safe
registry matching (Redmine #11625), identity layering (registry → anchor →
derivation), the SQLite cache (first creation, reuse, corrupt recreation,
newer-schema protection), the tmux-unavailable degraded path, and the CLI
JSON schema. Everything runs against temp dirs — no tmux, no real
``~/.mozyo_bridge``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge import session_inventory
from mozyo_bridge.application.commands import cmd_session_list
from mozyo_bridge.domain.session_naming import derive_session_name
from mozyo_bridge.session_inventory import (
    SOURCE_CACHE,
    SOURCE_RUNTIME,
    collect_runtime_inventory,
    inventory_path,
    load_snapshot,
    save_snapshot,
    take_inventory,
)
from mozyo_bridge.workspace_registry import (
    SOURCE_HOME_REGISTRY,
    SOURCE_WORKSPACE_ANCHOR,
    register_workspace,
    registry_path,
)


def pane(
    pane_id: str,
    location: str,
    *,
    command: str = "claude",
    cwd: str = "",
    window_name: str = "claude",
    pane_active: str = "1",
) -> dict[str, str]:
    return {
        "id": pane_id,
        "location": location,
        "command": command,
        "cwd": cwd,
        "window_name": window_name,
        "pane_active": pane_active,
    }


class SessionInventoryBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "mozyo-home"
        self.repo = base / "workspaces" / "demo-repo"
        self.repo.mkdir(parents=True)
        (self.repo / ".git").mkdir()
        env_patch = patch.dict(
            "os.environ", {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)


class CollectRuntimeInventoryTest(SessionInventoryBase):
    def test_collects_pane_fields_and_derivation_identity(self) -> None:
        records = collect_runtime_inventory(
            [
                pane(
                    "%1",
                    "mozyo-demo:1.0",
                    cwd=str(self.repo),
                    window_name="claude",
                ),
                pane(
                    "%2",
                    "mozyo-demo:2.0",
                    command="node",
                    cwd=str(self.repo),
                    window_name="codex",
                ),
            ],
            home=self.home,
        )
        self.assertEqual([record.pane_id for record in records], ["%1", "%2"])
        first = records[0]
        self.assertEqual(first.session, "mozyo-demo")
        self.assertEqual(first.window_index, "1")
        self.assertEqual(first.pane_index, "0")
        self.assertTrue(first.pane_active)
        self.assertEqual(first.agent_kind, "claude")
        self.assertEqual(records[1].agent_kind, "codex")
        self.assertEqual(first.repo_root, str(self.repo.resolve()))
        # Never-registered workspace: derivation identity, no workspace id.
        assert first.workspace is not None
        self.assertIsNone(first.workspace.workspace_id)
        self.assertEqual(
            first.workspace.canonical_session, derive_session_name(self.repo).name
        )
        self.assertEqual(
            first.workspace.source, derive_session_name(self.repo).source
        )

    def test_registered_workspace_resolves_from_registry(self) -> None:
        registered = register_workspace(self.repo, home=self.home)
        records = collect_runtime_inventory(
            [pane("%1", "any-session:1.0", cwd=str(self.repo))], home=self.home
        )
        workspace = records[0].workspace
        assert workspace is not None
        self.assertEqual(workspace.source, SOURCE_HOME_REGISTRY)
        self.assertEqual(
            workspace.workspace_id, registered.record.workspace_id
        )
        self.assertEqual(workspace.project_name, "demo-repo")

    def test_anchor_restores_identity_after_registry_loss(self) -> None:
        registered = register_workspace(self.repo, home=self.home)
        registry_path(self.home).unlink()
        records = collect_runtime_inventory(
            [pane("%1", "any-session:1.0", cwd=str(self.repo))], home=self.home
        )
        workspace = records[0].workspace
        assert workspace is not None
        self.assertEqual(workspace.source, SOURCE_WORKSPACE_ANCHOR)
        self.assertEqual(workspace.workspace_id, registered.record.workspace_id)
        self.assertEqual(
            workspace.canonical_session, registered.record.canonical_session
        )

    def test_grouped_session_folds_to_one_record(self) -> None:
        """Redmine #11628: the same pane in two sessions is one agent."""
        registered = register_workspace(self.repo, home=self.home)
        canonical = registered.record.canonical_session
        records = collect_runtime_inventory(
            [
                pane("%7", "1750-codex-view:1.0", cwd=str(self.repo)),
                pane("%7", f"{canonical}:2.0", cwd=str(self.repo)),
            ],
            home=self.home,
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.pane_id, "%7")
        # The canonical view is the session matching the workspace identity,
        # not the first one seen.
        self.assertEqual(record.session, canonical)
        self.assertEqual(len(record.views), 2)
        flags = {view.session: view.canonical for view in record.views}
        self.assertTrue(flags[canonical])
        self.assertFalse(flags["1750-codex-view"])

    def test_grouped_session_without_identity_match_is_deterministic(self) -> None:
        records = collect_runtime_inventory(
            [
                pane("%7", "zzz-view:1.0", cwd=str(self.repo)),
                pane("%7", "aaa-view:1.0", cwd=str(self.repo)),
            ],
            home=self.home,
        )
        self.assertEqual(records[0].session, "aaa-view")

    def test_unicode_normalization_difference_still_matches_registry(self) -> None:
        """Redmine #11625: NFD (filesystem) vs NFC (documents) is one path."""
        # Needs characters that actually decompose (dakuten katakana, as in
        # the real shared-drive paths from the bug report); kanji do not.
        nfd_name = unicodedata.normalize("NFD", "動画ドライブ")
        repo = Path(self._tmp.name) / "workspaces" / nfd_name
        repo.mkdir(parents=True)
        registered = register_workspace(repo, home=self.home)
        nfc_root = unicodedata.normalize("NFC", str(repo.resolve()))
        self.assertNotEqual(nfc_root, str(repo.resolve()))
        with patch.object(
            session_inventory, "infer_repo_root", return_value=nfc_root
        ):
            records = collect_runtime_inventory(
                [pane("%1", "s:1.0", cwd=nfc_root)], home=self.home
            )
        workspace = records[0].workspace
        assert workspace is not None
        self.assertEqual(workspace.source, SOURCE_HOME_REGISTRY)
        self.assertEqual(workspace.workspace_id, registered.record.workspace_id)

    def test_pane_without_id_is_skipped_and_no_cwd_has_no_identity(self) -> None:
        records = collect_runtime_inventory(
            [
                pane("", "ghost:1.0", cwd=str(self.repo)),
                pane("%9", "bare:1.0", cwd="", window_name="zsh"),
            ],
            home=self.home,
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.pane_id, "%9")
        self.assertIsNone(record.repo_root)
        self.assertIsNone(record.workspace)
        self.assertEqual(record.agent_kind, "unknown")


class SnapshotCacheTest(SessionInventoryBase):
    def _records(self) -> list:
        return collect_runtime_inventory(
            [
                pane("%1", "mozyo-demo:1.0", cwd=str(self.repo)),
                pane("%1", "mozyo-view:1.0", cwd=str(self.repo)),
                pane("%2", "mozyo-demo:2.0", command="node", window_name="codex"),
            ],
            home=self.home,
        )

    def test_save_creates_db_and_load_round_trips(self) -> None:
        self.assertFalse(inventory_path(self.home).exists())
        records = self._records()
        path, notes = save_snapshot(
            records, home=self.home, now="2026-06-11T00:00:00+00:00"
        )
        self.assertTrue(path.exists())
        self.assertEqual(notes, [])
        loaded = load_snapshot(home=self.home)
        assert loaded is not None
        cached_records, collected_at = loaded
        self.assertEqual(collected_at, "2026-06-11T00:00:00+00:00")
        self.assertEqual(
            sorted(record.pane_id for record in cached_records), ["%1", "%2"]
        )
        by_id = {record.pane_id: record for record in cached_records}
        self.assertEqual(len(by_id["%1"].views), 2)
        self.assertEqual(by_id["%1"].workspace, records[0].workspace)
        self.assertIsNone(by_id["%2"].workspace)

    def test_save_replaces_previous_snapshot(self) -> None:
        save_snapshot(self._records(), home=self.home)
        save_snapshot([], home=self.home, now="2026-06-11T01:00:00+00:00")
        loaded = load_snapshot(home=self.home)
        assert loaded is not None
        self.assertEqual(loaded[0], [])
        self.assertEqual(loaded[1], "2026-06-11T01:00:00+00:00")

    def test_corrupt_cache_is_recreated_on_save_and_unreadable_on_load(self) -> None:
        path = inventory_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text("this is not a sqlite database, not even close")
        self.assertIsNone(load_snapshot(home=self.home))
        _, notes = save_snapshot(self._records(), home=self.home)
        self.assertTrue(any("recreated" in note for note in notes))
        loaded = load_snapshot(home=self.home)
        assert loaded is not None
        self.assertEqual(len(loaded[0]), 2)

    def test_newer_schema_cache_is_left_untouched(self) -> None:
        path = inventory_path(self.home)
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 99")
        conn.execute("CREATE TABLE future (x TEXT)")
        conn.commit()
        conn.close()
        self.assertIsNone(load_snapshot(home=self.home))
        _, notes = save_snapshot(self._records(), home=self.home)
        self.assertTrue(any("schema version 99" in note for note in notes))
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 99
            )
        finally:
            conn.close()


class TakeInventoryTest(SessionInventoryBase):
    def test_runtime_snapshot_refreshes_cache(self) -> None:
        snapshot = take_inventory(
            home=self.home,
            panes=[pane("%1", "mozyo-demo:1.0", cwd=str(self.repo))],
        )
        self.assertEqual(snapshot.source, SOURCE_RUNTIME)
        self.assertFalse(snapshot.stale)
        self.assertEqual(len(snapshot.records), 1)
        self.assertTrue(inventory_path(self.home).exists())
        loaded = load_snapshot(home=self.home)
        assert loaded is not None
        self.assertEqual(loaded[1], snapshot.collected_at)

    def test_tmux_unavailable_degrades_to_stale_cache(self) -> None:
        take_inventory(
            home=self.home,
            panes=[pane("%1", "mozyo-demo:1.0", cwd=str(self.repo))],
        )
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=None,
        ):
            snapshot = take_inventory(home=self.home)
        self.assertEqual(snapshot.source, SOURCE_CACHE)
        self.assertTrue(snapshot.stale)
        self.assertEqual(len(snapshot.records), 1)
        self.assertTrue(any("cached snapshot" in note for note in snapshot.notes))

    def test_tmux_unavailable_without_cache_yields_empty_stale(self) -> None:
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=None,
        ):
            snapshot = take_inventory(home=self.home)
        self.assertEqual(snapshot.source, SOURCE_CACHE)
        self.assertTrue(snapshot.stale)
        self.assertEqual(snapshot.records, ())
        self.assertIsNone(snapshot.collected_at)


class SessionListCliTest(SessionInventoryBase):
    def capture(self, func, args: argparse.Namespace) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(args)
        return code, out.getvalue(), err.getvalue()

    def test_json_schema(self) -> None:
        registered = register_workspace(self.repo, home=self.home)
        canonical = registered.record.canonical_session
        panes = [
            pane("%1", f"{canonical}:1.0", cwd=str(self.repo)),
            pane("%1", "grouped-view:1.0", cwd=str(self.repo)),
        ]
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=panes,
        ):
            code, out, _ = self.capture(
                cmd_session_list, argparse.Namespace(as_json=True)
            )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["source"], "runtime")
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["inventory_path"], str(inventory_path()))
        self.assertEqual(len(payload["panes"]), 1)
        record = payload["panes"][0]
        self.assertEqual(
            sorted(record),
            [
                "agent_kind",
                "cwd",
                "pane_active",
                "pane_id",
                "pane_index",
                "process",
                "repo_root",
                "session",
                "views",
                "window_index",
                "window_name",
                "workspace",
            ],
        )
        self.assertEqual(record["session"], canonical)
        self.assertEqual(len(record["views"]), 2)
        self.assertEqual(
            sorted(record["workspace"]),
            ["canonical_session", "project_name", "source", "workspace_id"],
        )
        self.assertEqual(record["workspace"]["source"], SOURCE_HOME_REGISTRY)

    def test_text_output_marks_stale_cache(self) -> None:
        take_inventory(
            home=self.home,
            panes=[pane("%1", "mozyo-demo:1.0", cwd=str(self.repo))],
        )
        with patch(
            "mozyo_bridge.infrastructure.tmux_client.try_pane_lines",
            return_value=None,
        ):
            code, out, err = self.capture(
                cmd_session_list, argparse.Namespace(as_json=False)
            )
        self.assertEqual(code, 0)
        self.assertIn("stale:", err)
        self.assertIn("PANE\tSESSION", out)
        self.assertIn("%1", out)


if __name__ == "__main__":
    unittest.main()
