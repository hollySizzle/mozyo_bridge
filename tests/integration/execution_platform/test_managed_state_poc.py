"""Managed-marker / desired-state event PoC tests (Redmine #11699/#11700/#11701).

Pins the managed-state-model boundaries: marker classification
(registry anchor primary, tmux option secondary, unmanaged coexistence,
prefix never an authority), and the desired-state event log (append-only,
schema v1, pane_id identity, socket extension point, NFD repo_root,
best-effort). Real tmux is never invoked — the option helpers are mocked.
No liveness / handoff path is touched; a guard test pins that the PoC
modules import nothing from the handoff/resolver surfaces.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.domain.managed_marker import (
    MANAGED,
    MANAGED_OPTION,
    SOURCE_REGISTRY_ANCHOR,
    SOURCE_TMUX_OPTION,
    UNMANAGED,
    classify_managed,
    mark_target,
    read_target_marker,
)
from mozyo_bridge.managed_events import (
    DEFAULT_SOCKET,
    KIND_CREATED,
    MANAGED_EVENTS_SCHEMA_VERSION,
    ManagedEvent,
    ManagedEventLog,
    managed_events_path,
    record_managed_event,
)
from mozyo_bridge.workspace_registry import register_workspace


class ManagedMarkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir(parents=True)

    def test_registry_anchor_is_primary(self) -> None:
        register_workspace(self.repo, home=self.home)
        # Anchor present → managed via primary, regardless of tmux marker.
        result = classify_managed(
            repo_root=str(self.repo), tmux_marker=None
        )
        self.assertEqual(MANAGED, result.state)
        self.assertEqual(SOURCE_REGISTRY_ANCHOR, result.source)
        self.assertFalse(result.runtime_only)

    def test_tmux_option_is_secondary_when_no_anchor(self) -> None:
        # No anchor, but a runtime marker → managed via secondary.
        result = classify_managed(
            repo_root=str(self.repo), tmux_marker="1"
        )
        self.assertEqual(MANAGED, result.state)
        self.assertEqual(SOURCE_TMUX_OPTION, result.source)

    def test_no_marker_is_unmanaged_runtime_only(self) -> None:
        result = classify_managed(repo_root=str(self.repo), tmux_marker=None)
        self.assertEqual(UNMANAGED, result.state)
        self.assertTrue(result.runtime_only)

    def test_anchor_wins_over_absent_option_and_none_repo(self) -> None:
        # No repo root at all and no marker → unmanaged, never a crash.
        result = classify_managed(repo_root=None, tmux_marker=None)
        self.assertEqual(UNMANAGED, result.state)

    def test_name_prefix_is_not_an_authority_signal(self) -> None:
        # A session whose name starts with `mozyo-` but has neither anchor
        # nor option must classify unmanaged: prefix is never consulted.
        result = classify_managed(
            repo_root=str(self.repo), tmux_marker=None
        )
        self.assertEqual(UNMANAGED, result.state)
        # The classifier has no parameter for a name/prefix at all.
        import inspect

        params = set(inspect.signature(classify_managed).parameters)
        self.assertEqual({"repo_root", "tmux_marker"}, params)

    def test_mark_and_read_use_the_user_option(self) -> None:
        calls: list[tuple] = []

        def fake_set(target, option, value):
            calls.append(("set", target, option, value))
            return True

        def fake_get(target, option):
            calls.append(("get", target, option))
            return "1"

        with patch(
            "mozyo_bridge.infrastructure.tmux_client.set_user_option",
            side_effect=fake_set,
        ), patch(
            "mozyo_bridge.infrastructure.tmux_client.get_user_option",
            side_effect=fake_get,
        ):
            self.assertTrue(mark_target("%1"))
            self.assertEqual("1", read_target_marker("%1"))
        self.assertEqual(("set", "%1", MANAGED_OPTION, "1"), calls[0])
        self.assertEqual(("get", "%1", MANAGED_OPTION), calls[1])


class ManagedEventLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.db = managed_events_path(self.home)

    def test_append_creates_schema_v1_and_round_trips(self) -> None:
        log = ManagedEventLog(home=self.home)
        appended = log.append(
            ManagedEvent(
                command="mozyo",
                event_kind=KIND_CREATED,
                pane_id="%1",
                mozyo_session="mozyo-demo",
                workspace_id="ws-1",
                repo_root="/repo",
                intent={"agent": "claude", "window": "claude"},
            )
        )
        self.assertTrue(self.db.exists())
        self.assertEqual(DEFAULT_SOCKET, appended.socket)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                MANAGED_EVENTS_SCHEMA_VERSION,
                conn.execute("PRAGMA user_version").fetchone()[0],
            )
        finally:
            conn.close()
        events = log.events_for_pane("%1")
        self.assertEqual(1, len(events))
        self.assertEqual(KIND_CREATED, events[0].event_kind)
        self.assertEqual("claude", events[0].intent["agent"])

    def test_append_only_keeps_every_record(self) -> None:
        log = ManagedEventLog(home=self.home)
        for kind in (KIND_CREATED, "renamed", "observed"):
            log.append(
                ManagedEvent(command="mozyo", event_kind=kind, pane_id="%1")
            )
        events = log.events_for_pane("%1")
        self.assertEqual(3, len(events))
        # Order preserved (append-only, ascending id).
        self.assertEqual(
            [KIND_CREATED, "renamed", "observed"],
            [e.event_kind for e in events],
        )

    def test_repo_root_is_nfd_normalized_at_write(self) -> None:
        nfc = unicodedata.normalize("NFC", "/ws/動画ドライブ")
        nfd = unicodedata.normalize("NFD", "/ws/動画ドライブ")
        self.assertNotEqual(nfc, nfd)
        log = ManagedEventLog(home=self.home)
        appended = log.append(
            ManagedEvent(
                command="mozyo", event_kind=KIND_CREATED,
                pane_id="%1", repo_root=nfc,
            )
        )
        # Stored form is NFD, regardless of the NFC input.
        self.assertEqual(nfd, appended.repo_root)
        self.assertEqual(nfd, log.events_for_pane("%1")[0].repo_root)

    def test_pane_id_is_identity_key_session_is_attribute(self) -> None:
        log = ManagedEventLog(home=self.home)
        # Same pane, two different session names (grouped / renamed): both
        # rows belong to the pane, session is just an attribute.
        log.append(
            ManagedEvent(
                command="mozyo", event_kind=KIND_CREATED,
                pane_id="%7", mozyo_session="alias-view",
            )
        )
        log.append(
            ManagedEvent(
                command="mozyo", event_kind="observed",
                pane_id="%7", mozyo_session="mozyo-demo",
            )
        )
        events = log.events_for_pane("%7")
        self.assertEqual(2, len(events))
        self.assertEqual(
            {"alias-view", "mozyo-demo"},
            {e.mozyo_session for e in events},
        )

    def test_socket_extension_point_defaults_and_is_kept(self) -> None:
        log = ManagedEventLog(home=self.home)
        log.append(
            ManagedEvent(
                command="mozyo", event_kind=KIND_CREATED,
                pane_id="%1", socket="alt-socket",
            )
        )
        self.assertEqual("alt-socket", log.events_for_pane("%1")[0].socket)

    def test_record_helper_is_best_effort_on_failure(self) -> None:
        # A store failure must return None, never raise into the caller
        # (command boundary must not break on a desired-state log error).
        with patch(
            "mozyo_bridge.managed_events.ManagedEventLog.append",
            side_effect=OSError("disk full"),
        ):
            result = record_managed_event(
                command="mozyo", event_kind=KIND_CREATED,
                pane_id="%1", home=self.home,
            )
        self.assertIsNone(result)

    def test_record_helper_appends_and_normalizes(self) -> None:
        nfc = unicodedata.normalize("NFC", "/ws/動画ドライブ")
        event = record_managed_event(
            command="mozyo", event_kind=KIND_CREATED,
            pane_id="%1", repo_root=nfc, home=self.home,
        )
        self.assertIsNotNone(event)
        self.assertEqual(
            unicodedata.normalize("NFD", "/ws/動画ドライブ"), event.repo_root
        )

    def test_missing_or_corrupt_log_reads_empty(self) -> None:
        absent = ManagedEventLog(home=Path(self._tmp.name) / "nope")
        self.assertEqual([], absent.recent())
        corrupt = managed_events_path(self.home)
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("not a database")
        self.assertEqual([], ManagedEventLog(home=self.home).recent())


class PocBoundaryGuardTest(unittest.TestCase):
    def test_poc_modules_do_not_import_liveness_or_handoff(self) -> None:
        # #11698 invariant: the desired-state PoC must not reach into
        # liveness / handoff / resolver surfaces. Pin that the source
        # carries no such imports.
        forbidden = ("handoff", "pane_resolver", "agent_discovery")
        for module in (
            "src/mozyo_bridge/managed_events.py",
            "src/mozyo_bridge/domain/managed_marker.py",
        ):
            text = (ROOT / module).read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(
                    f"import {token}", text, f"{module} imports {token}"
                )
                self.assertNotIn(
                    f"{token} import", text, f"{module} imports {token}"
                )


if __name__ == "__main__":
    unittest.main()
