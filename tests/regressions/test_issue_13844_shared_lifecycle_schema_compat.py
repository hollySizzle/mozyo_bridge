"""Redmine #13844 — shared lifecycle schema cross-lane read compatibility.

Parallel repo lanes each run a source CLI of a different schema generation, but the
home-scoped ``lane_lifecycle`` authority is shared. Before this fix, EVERY lifecycle read
ran the migrating schema-ensure (``LaneLifecycleStore.get`` -> ``_connect`` ->
``ensure_lane_lifecycle_schema``), so a newer-schema source CLI forward-migrated the shared
store on a mere READ (status / handoff / review / callback / drain routing). A concurrent
older-schema reader then (correctly) refused to downgrade the now-newer store and its
``standard`` handoff stopped with ``gateway_route_blocked`` (live: #13842 ``56d3a32``
migrated to v6, then #13813 j#79382 could not send).

The fix is a read-compatible / write-migrating split:

- :class:`LaneLifecycleReader` / :func:`load_lane_lifecycle_readonly` read the authority
  read-only and version-compatibly — a newer build reads an older KNOWN additive shape by
  padding the missing columns with their in-memory migration defaults, touching no byte;
- only a mutating use case runs :func:`ensure_lane_lifecycle_schema` (backup-first, typed
  outcome);
- an unknown / newer / partial / malformed shape still fails closed (no downgrade / no
  misread), with the specific NEWER sub-case named ``reader_upgrade_required``.

All state lives under an isolated home — never the shared ``$HOME/.mozyo_bridge``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state import lane_lifecycle as ll  # noqa: E402
from mozyo_bridge.core.state import lane_lifecycle_schema as sch  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    BINDING_KIND_ISSUE,
    DISPOSITION_ACTIVE,
    DecisionPointer,
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleReader,
    LaneLifecycleStore,
    OWNER_ABSENT,
    OWNER_RESOLVED,
    lane_lifecycle_path,
    lifecycle_migration_preflight,
    load_lane_lifecycle_readonly,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import (  # noqa: E402
    LANE_LIFECYCLE_COMPONENT,
    LANE_LIFECYCLE_SCHEMA_VERSION,
    SCHEMA_CREATED,
    SCHEMA_INTACT,
    SCHEMA_MIGRATED,
    ensure_lane_lifecycle_schema,
    reader_upgrade_required,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    gateway_route_gate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.gateway_route_gate import (  # noqa: E402,E501
    _resolve_target_disposition,
    enforce_gateway_route,
)

WS = "wProj"
LANE = "issue_13813_startup_exactly_once_resume"
ISSUE = "13813"


def _issue_decision() -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="79382")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recorded_version(home: Path) -> object:
    conn = sqlite3.connect(lane_lifecycle_path(home))
    try:
        row = conn.execute(
            "SELECT schema_version FROM state_schema_components WHERE component = ?",
            (LANE_LIFECYCLE_COMPONENT,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _columns(home: Path) -> set:
    conn = sqlite3.connect(lane_lifecycle_path(home))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(lane_lifecycle_records)")}
    finally:
        conn.close()


def _seed_v5(home: Path, *, disposition: str = DISPOSITION_ACTIVE) -> Path:
    """A healthy current store rewound to a genuine v5 signature (a real pre-#13842 store).

    Keeps every v5 column and BOTH owner indexes; ONLY ``reconcile_phase`` is absent and the
    recorded component version is 5 — the exact ``_SHAPE_V5`` branch a v6 build must read
    compatibly (and a v6 writer must migrate additively).
    """
    store = LaneLifecycleStore(home=home)
    store.declare_active(
        LaneLifecycleKey(WS, LANE),
        decision=_issue_decision(),
        issue_id=ISSUE,
        worktree_identity="wt_13813bound",
    )
    if disposition != DISPOSITION_ACTIVE:
        rec = store.get(LaneLifecycleKey(WS, LANE))
        store.transition_disposition(
            LaneLifecycleKey(WS, LANE),
            to_disposition=disposition,
            expected_revision=rec.revision,
            decision=_issue_decision(),
        )
    path = lane_lifecycle_path(home)
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE lane_lifecycle_records DROP COLUMN reconcile_phase")
        conn.execute(
            "UPDATE state_schema_components SET schema_version = 5 WHERE component = ?",
            (LANE_LIFECYCLE_COMPONENT,),
        )
        conn.commit()
    finally:
        conn.close()
    return path


@dataclass
class _Target:
    workspace_id: str
    lane_id: str
    role: str = "codex"
    repo_root: object = None


class _FakeBinding:
    def provider_for(self, role):
        return "codex" if role == "coordinator" else "claude"


class _Die(Exception):
    pass


# -- read-compatible access: a newer build reads an older store, no migration ------------


class ReadCompatibleAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_v6_reader_reads_v5_store_without_migrating(self) -> None:
        # Required regression 1: faithful v5 store + v6 source read -> bytes / version
        # unchanged, backup 0, and the missing v6 field padded with its default.
        path = _seed_v5(self.home)
        before = _digest(path)
        self.assertEqual(_recorded_version(self.home), 5)

        reader = LaneLifecycleReader(home=self.home)
        rec = reader.get(LaneLifecycleKey(WS, LANE))
        self.assertIsNotNone(rec)
        self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)
        self.assertEqual(rec.worktree_identity, "wt_13813bound")
        self.assertEqual(rec.reconcile_phase, "")  # padded additive default, not guessed
        self.assertEqual(len(reader.records()), 1)
        owner = reader.resolve_owner(WS, ISSUE)
        self.assertEqual(owner.status, OWNER_RESOLVED)
        self.assertEqual(owner.lane_id, LANE)

        # The read touched no byte: version stays 5, reconcile_phase still absent, no backup.
        self.assertEqual(_digest(path), before)
        self.assertEqual(_recorded_version(self.home), 5)
        self.assertNotIn("reconcile_phase", _columns(self.home))
        self.assertFalse((self.home / "backups").exists())

    def test_load_readonly_reads_v5_without_migrating(self) -> None:
        path = _seed_v5(self.home)
        before = _digest(path)
        rows = load_lane_lifecycle_readonly(home=self.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reconcile_phase, "")
        self.assertEqual(_digest(path), before)
        self.assertEqual(_recorded_version(self.home), 5)

    def test_reader_reads_current_v6_store(self) -> None:
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(
            LaneLifecycleKey(WS, LANE), decision=_issue_decision(), issue_id=ISSUE
        )
        rec = LaneLifecycleReader(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)

    def test_absent_store_reads_empty_and_creates_nothing(self) -> None:
        reader = LaneLifecycleReader(home=self.home)
        self.assertIsNone(reader.get(LaneLifecycleKey(WS, LANE)))
        self.assertEqual(reader.records(), ())
        self.assertEqual(reader.resolve_owner(WS, ISSUE).status, OWNER_ABSENT)
        self.assertEqual(load_lane_lifecycle_readonly(home=self.home), ())
        # nothing was created just to read
        self.assertFalse(lane_lifecycle_path(self.home).exists())


# -- fail-closed: unknown / newer / partial / malformed -> no downgrade, no write --------


class FailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _current_store(self) -> Path:
        LaneLifecycleStore(home=self.home).declare_active(
            LaneLifecycleKey(WS, LANE), decision=_issue_decision(), issue_id=ISSUE
        )
        return lane_lifecycle_path(self.home)

    def _stamp_version(self, path: Path, value: object) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = ? WHERE component = ?",
                (value, LANE_LIFECYCLE_COMPONENT),
            )
            conn.commit()
        finally:
            conn.close()

    def test_newer_component_version_fails_closed_upgrade_required(self) -> None:
        path = self._current_store()
        self._stamp_version(path, LANE_LIFECYCLE_SCHEMA_VERSION + 1)  # a future build's shape
        before = _digest(path)
        reader = LaneLifecycleReader(home=self.home)
        with self.assertRaises(LaneLifecycleError):
            reader.get(LaneLifecycleKey(WS, LANE))
        with self.assertRaises(LaneLifecycleError):
            reader.records()
        # the specific NEWER sub-case is actionable: route to a newer reader, do not downgrade
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            self.assertTrue(reader_upgrade_required(conn))
        finally:
            conn.close()
        self.assertEqual(_digest(path), before)  # no write / no downgrade

    def test_newer_container_version_fails_closed(self) -> None:
        path = self._current_store()
        conn = sqlite3.connect(path)
        try:
            conn.execute(f"PRAGMA user_version = {sch.STATE_CONTAINER_VERSION + 5}")
            conn.commit()
        finally:
            conn.close()
        before = _digest(path)
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleReader(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertEqual(_digest(path), before)

    def test_malformed_version_fails_closed(self) -> None:
        path = self._current_store()
        self._stamp_version(path, 2.5)  # a REAL, not an exact integer
        before = _digest(path)
        reader = LaneLifecycleReader(home=self.home)
        with self.assertRaises(LaneLifecycleError):
            reader.records()
        # malformed is NOT "just upgrade" — it is unsupported, not reader_upgrade_required
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            self.assertFalse(reader_upgrade_required(conn))
        finally:
            conn.close()
        self.assertEqual(_digest(path), before)

    def test_partial_recognized_shape_fails_closed(self) -> None:
        # A recognized version (v6) whose live shape is NOT a known v6 signature (an extra
        # column) is a corrupt / partial authority shape -> fail closed, never adopted.
        path = self._current_store()
        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE lane_lifecycle_records ADD COLUMN junk TEXT DEFAULT ''")
            conn.commit()
        finally:
            conn.close()
        before = _digest(path)
        with self.assertRaises(LaneLifecycleError):
            LaneLifecycleReader(home=self.home).records()
        self.assertEqual(_digest(path), before)

    def test_v5_build_reading_migrated_v6_store_fails_closed(self) -> None:
        # The other side of the live incident: once a store is v6, a genuine v5 build (one
        # whose recognized set stops at 5) must fail closed on it (never downgrade) — which is
        # exactly WHY a read must not migrate the shared store out from under it. Emulate the
        # v5 build by shrinking the recognized-version set.
        path = self._current_store()  # a real v6 store
        before = _digest(path)
        with patch.object(sch, "_RECOGNIZED_SCHEMA_VERSIONS", frozenset({1, 2, 3, 4, 5})):
            reader = LaneLifecycleReader(home=self.home)
            with self.assertRaises(LaneLifecycleError):
                reader.get(LaneLifecycleKey(WS, LANE))
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                self.assertTrue(reader_upgrade_required(conn))
            finally:
                conn.close()
        self.assertEqual(_digest(path), before)


# -- write-migrating gate: only a mutating use case migrates, backup-first, typed ---------


class WriteMigratingGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fresh_store_created_outcome(self) -> None:
        out = LaneLifecycleStore(home=self.home).ensure_schema()
        self.assertEqual(out.action, SCHEMA_CREATED)
        self.assertIsNone(out.from_version)
        self.assertEqual(out.to_version, LANE_LIFECYCLE_SCHEMA_VERSION)

    def test_v5_writer_migrates_backup_first_typed_outcome(self) -> None:
        # Required regression 3: an explicit writer migrates v5 -> v6 backup-first with a
        # typed outcome (the mutating use case, unlike the read paths above).
        path = _seed_v5(self.home)
        before = path.read_bytes()
        out = ensure_lane_lifecycle_schema(path)
        self.assertEqual(out.action, SCHEMA_MIGRATED)
        self.assertEqual(out.from_version, 5)
        self.assertEqual(out.to_version, 6)
        self.assertIsNotNone(out.backup_dir)
        self.assertEqual(_recorded_version(self.home), 6)
        self.assertIn("reconcile_phase", _columns(self.home))
        backups = sorted((self.home / "backups").glob("state-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "state.sqlite").read_bytes(), before)

    def test_migration_then_reensure_is_intact_and_idempotent(self) -> None:
        # Required regression 5: restart idempotency — re-running ensure after a migration is
        # intact (no second migration, no second backup).
        path = _seed_v5(self.home)
        ensure_lane_lifecycle_schema(path)
        after_first = _digest(path)
        out2 = ensure_lane_lifecycle_schema(path)
        self.assertEqual(out2.action, SCHEMA_INTACT)
        self.assertEqual(out2.from_version, LANE_LIFECYCLE_SCHEMA_VERSION)
        self.assertEqual(_digest(path), after_first)  # intact re-ensure writes nothing
        self.assertEqual(len(list((self.home / "backups").glob("state-*"))), 1)

    def test_read_between_writes_sees_pre_migration_shape(self) -> None:
        # A concurrent reader before the explicit migration reads the v5 store compatibly; it
        # neither triggers nor is corrupted by the migration.
        path = _seed_v5(self.home)
        rec = LaneLifecycleReader(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertEqual(rec.reconcile_phase, "")
        self.assertEqual(_recorded_version(self.home), 5)  # read did not migrate
        ensure_lane_lifecycle_schema(path)  # now an explicit writer migrates
        self.assertEqual(_recorded_version(self.home), 6)

    def test_writer_still_fails_closed_on_newer_store(self) -> None:
        path = _seed_v5(self.home)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = ? WHERE component = ?",
                (LANE_LIFECYCLE_SCHEMA_VERSION + 3, LANE_LIFECYCLE_COMPONENT),
            )
            conn.commit()
        finally:
            conn.close()
        before = _digest(path)
        with self.assertRaises(LaneLifecycleError):
            ensure_lane_lifecycle_schema(path)
        self.assertEqual(_digest(path), before)  # downgrade-safe: untouched


# -- standard handoff lookup: read-only API only, never the mutation / migration API -----


class StandardHandoffReadOnlyGuardBiteTest(unittest.TestCase):
    """The mandatory guard-bite (Redmine #13844): the standard-handoff lifecycle lookup reads
    through the read-only API and NEVER reaches the mutation / migration API."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_handoff_lookup_on_v5_store_does_not_migrate(self) -> None:
        # Required regression: the live-shaped core — a v5 active lane + a v6 source handoff
        # lookup resolves the disposition WITHOUT migrating the shared store (so the concurrent
        # v5 reader keeps working).
        path = _seed_v5(self.home)
        before = _digest(path)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            self.assertEqual(
                _resolve_target_disposition(_Target(WS, LANE)),
                (DISPOSITION_ACTIVE, False),
            )
        self.assertEqual(_digest(path), before)
        self.assertEqual(_recorded_version(self.home), 5)
        self.assertFalse((self.home / "backups").exists())

    def test_handoff_lookup_never_reaches_migration_api(self) -> None:
        # Guard-bite: booby-trap the migration API. The read-only reader path must not touch it;
        # the OLD migrating store path WOULD — proving the seam actually changed (adversarial).
        _seed_v5(self.home)
        sentinel = RuntimeError("migration API reached")

        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            with patch.object(ll, "ensure_lane_lifecycle_schema", side_effect=sentinel) as m:
                # The read-only handoff lookup succeeds and never calls the migration API.
                self.assertEqual(
                    _resolve_target_disposition(_Target(WS, LANE)),
                    (DISPOSITION_ACTIVE, False),
                )
                m.assert_not_called()
                # Adversarial: the old migrating read (store.get) DOES reach it — the guard bites.
                with self.assertRaises(RuntimeError):
                    LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
                m.assert_called()


# -- live-shaped integration: v5 lane active + v6 command + v5 review delivery ------------


class LiveShapedReviewDeliveryTest(unittest.TestCase):
    """Acceptance A4: v5 lane active + v6 source command + v5 same-lane review delivery routes
    over the exact route, and the v6 command does not migrate the shared store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enforce(self, kind: str, target: _Target):
        emitted = []
        args = argparse.Namespace(allow_direct_worker=False)
        with patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        ), patch.object(
            gateway_route_gate, "load_workflow_binding", return_value=(_FakeBinding(), [])
        ), patch.object(gateway_route_gate, "die", side_effect=_Die):
            enforce_gateway_route(
                kind=kind,
                receiver="codex",
                preflight_target=target,
                source="redmine",
                mode="queue-enter",
                anchor=None,
                target="wProj:p9",
                record_format="text",
                record_command=None,
                emit=lambda outcome, **kw: emitted.append(outcome),
                allow_direct_worker=bool(getattr(args, "allow_direct_worker", False)),
                sender_lane_unit=(None, None),
            )
        return emitted

    def test_v6_command_routes_active_v5_lane_without_migrating(self) -> None:
        path = _seed_v5(self.home)
        before = _digest(path)
        # A v6 source governed delivery to the active v5 lane is NOT blocked by the lifecycle
        # authority (disposition active), and does not migrate the shared store.
        emitted = self._enforce("implementation_request", _Target(WS, LANE))
        self.assertEqual(emitted, [])
        self.assertEqual(_digest(path), before)
        self.assertEqual(_recorded_version(self.home), 5)

    def test_preflight_reports_peer_active_lanes(self) -> None:
        # The compatibility preflight (read-only) reports the active peer lanes a schema-changing
        # write would fail-close, computed without migrating.
        _seed_v5(self.home)
        pf = lifecycle_migration_preflight(
            home=self.home, writer_workspace_id=WS, writer_lane_id="some_other_lane"
        )
        self.assertFalse(pf.unreadable)
        self.assertEqual(pf.current_version, 5)
        self.assertEqual(pf.peer_active_lanes, (LANE,))
        # the writer's own lane is excluded
        pf_self = lifecycle_migration_preflight(
            home=self.home, writer_workspace_id=WS, writer_lane_id=LANE
        )
        self.assertEqual(pf_self.peer_active_lanes, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
