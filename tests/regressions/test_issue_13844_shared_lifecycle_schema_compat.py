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
import threading
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
    LaneLifecycleReaderUpgradeRequired,
    LaneLifecycleStore,
    OWNER_ABSENT,
    OWNER_RESOLVED,
    lane_lifecycle_path,
    lifecycle_migration_preflight,
    load_lane_lifecycle_readonly,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
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
                (DISPOSITION_ACTIVE, False, False),
            )
        self.assertEqual(_digest(path), before)
        self.assertEqual(_recorded_version(self.home), 5)
        self.assertFalse((self.home / "backups").exists())

    def test_handoff_lookup_never_reaches_migration_api(self) -> None:
        # Guard-bite: booby-trap the migration API. The read-only handoff lookup must not touch
        # it; a WRITE (the only thing allowed to migrate) DOES — proving the read/write seam.
        _seed_v5(self.home)
        sentinel = RuntimeError("migration API reached")

        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            with patch.object(ll, "ensure_lane_lifecycle_schema", side_effect=sentinel) as m:
                # The read-only handoff lookup succeeds and never calls the migration API.
                self.assertEqual(
                    _resolve_target_disposition(_Target(WS, LANE)),
                    (DISPOSITION_ACTIVE, False, False),
                )
                m.assert_not_called()
                # A store READ (get) also never migrates now (Redmine #13844 R2 read-not-migrate).
                LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))
                m.assert_not_called()
                # Adversarial contrast: a WRITE reaches the migration API — the guard bites.
                with self.assertRaises(RuntimeError):
                    LaneLifecycleStore(home=self.home).transition_disposition(
                        LaneLifecycleKey(WS, LANE),
                        expected_disposition=DISPOSITION_ACTIVE,
                        expected_revision=1,
                        target="hibernated",
                        decision=_issue_decision(),
                    )
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


# -- F1: explicit write gate — preflight + typed outcome wired to production ------------


class WriteGatePreparationTest(unittest.TestCase):
    """Review j#79471 F1: the schema-changing WRITE gate runs the preflight and surfaces the
    typed migration outcome; ``_connect`` never silently discards it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_captures_migration_outcome_read_does_not(self) -> None:
        # Guard-bite (Redmine #13844 R2): a WRITE (_connect_write) that migrates a v5 store must
        # RECORD the typed outcome, never silently discard it — AND a READ never migrates at all.
        _seed_v5(self.home)
        reader_store = LaneLifecycleStore(home=self.home)
        reader_store.get(LaneLifecycleKey(WS, LANE))  # a READ must not migrate / not record
        self.assertIsNone(reader_store.last_schema_outcome)
        self.assertEqual(_recorded_version(self.home), 5)  # still v5 after the read

        store = LaneLifecycleStore(home=self.home)
        self.assertIsNone(store.last_schema_outcome)  # nothing written yet
        rec = store.get(LaneLifecycleKey(WS, LANE))
        store.transition_disposition(  # a WRITE runs the gate and migrates
            LaneLifecycleKey(WS, LANE),
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target="hibernated",
            decision=_issue_decision(),
        )
        self.assertIsNotNone(store.last_schema_outcome)
        self.assertEqual(store.last_schema_outcome.action, SCHEMA_MIGRATED)
        self.assertEqual(store.last_schema_outcome.from_version, 5)

    def test_prepare_write_reads_preflight_before_migrating(self) -> None:
        # The explicit gate reads the peer preflight on the PRE-migration store, then migrates —
        # returning both, typed, so the migration is a visible act.
        _seed_v5(self.home)  # active LANE present, v5
        store = LaneLifecycleStore(home=self.home)
        prep = store.prepare_write(
            writer_workspace_id=WS, writer_lane_id="a_different_writer_lane"
        )
        self.assertEqual(prep.outcome.action, SCHEMA_MIGRATED)
        self.assertEqual(prep.outcome.from_version, 5)
        self.assertIsNotNone(prep.outcome.backup_dir)
        # the peer was read from the PRE-migration (v5) store, and version 5 recorded there
        self.assertEqual(prep.preflight.current_version, 5)
        self.assertEqual(prep.preflight.peer_active_lanes, (LANE,))
        self.assertTrue(prep.migrated)
        self.assertTrue(prep.peer_reader_risk)
        # and the store is now migrated (the gate performed the mutation)
        self.assertEqual(_recorded_version(self.home), 6)

    def test_declare_lane_surfaces_write_preparation(self) -> None:
        # Redmine #13844 F1: the production declaration write gate (LaneDeclarationStore) runs the
        # preflight + typed outcome, reachable on last_write_preparation — lifecycle_migration_
        # preflight now has a real production call site, not dead code.
        _seed_v5(self.home)  # LANE active, v5
        decl = LaneDeclarationStore(home=self.home)
        self.assertIsNone(decl.last_write_preparation)
        other = LaneLifecycleKey(WS, "issue_99999_other_lane")
        decl.declare_lane(
            other,
            decision=DecisionPointer(source="redmine", issue_id="99999", journal_id="1"),
            binding_kind=BINDING_KIND_ISSUE,
            issue_id="99999",
        )
        prep = decl.last_write_preparation
        self.assertIsNotNone(prep)
        self.assertEqual(prep.outcome.action, SCHEMA_MIGRATED)
        self.assertEqual(prep.outcome.from_version, 5)
        # the pre-existing active LANE is surfaced as a peer at risk from this migration
        self.assertIn(LANE, prep.preflight.peer_active_lanes)
        self.assertTrue(prep.peer_reader_risk)

    def test_prepare_write_on_current_store_is_intact_no_peer_risk(self) -> None:
        LaneLifecycleStore(home=self.home).ensure_schema()  # a fresh current v6 store
        store = LaneLifecycleStore(home=self.home)
        prep = store.prepare_write(writer_workspace_id=WS, writer_lane_id=LANE)
        self.assertEqual(prep.outcome.action, SCHEMA_INTACT)
        self.assertFalse(prep.migrated)
        self.assertFalse(prep.peer_reader_risk)  # no migration => no peer risk


# -- F2: typed reader_upgrade_required routing (newer schema -> facade, not generic block) -


class ReaderUpgradeRequiredRoutingTest(unittest.TestCase):
    """Review j#79471 F2: a NEWER-schema store is a TYPED reader_upgrade_required — routed to
    the current facade — distinct from a generic block; malformed/partial stays generic."""

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

    def test_reader_raises_typed_subclass_for_newer_store(self) -> None:
        self._current_store()  # a real v6 store
        # Emulate a v5 build (recognizes only 1..5) reading the v6 store.
        with patch.object(sch, "_RECOGNIZED_SCHEMA_VERSIONS", frozenset({1, 2, 3, 4, 5})):
            reader = LaneLifecycleReader(home=self.home)
            with self.assertRaises(LaneLifecycleReaderUpgradeRequired):
                reader.get(LaneLifecycleKey(WS, LANE))

    def test_malformed_store_is_generic_not_upgrade_required(self) -> None:
        path = self._current_store()
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "UPDATE state_schema_components SET schema_version = 2.5 WHERE component = ?",
                (LANE_LIFECYCLE_COMPONENT,),
            )
            conn.commit()
        finally:
            conn.close()
        reader = LaneLifecycleReader(home=self.home)
        with self.assertRaises(LaneLifecycleError) as ctx:
            reader.records()
        # a malformed store is NOT the actionable upgrade case — it stays the generic error
        self.assertNotIsInstance(ctx.exception, LaneLifecycleReaderUpgradeRequired)

    def test_handoff_gate_emits_reader_upgrade_required_outcome(self) -> None:
        # The end-to-end typed routing: a newer store yields a DISTINCT reader_upgrade_required
        # outcome + die, NOT a generic gateway_route_blocked.
        self._current_store()
        emitted = []
        with patch.object(sch, "_RECOGNIZED_SCHEMA_VERSIONS", frozenset({1, 2, 3, 4, 5})):
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
            ), patch.object(
                gateway_route_gate,
                "load_workflow_binding",
                return_value=(_FakeBinding(), []),
            ), patch.object(gateway_route_gate, "die", side_effect=_Die):
                with self.assertRaises(_Die):
                    enforce_gateway_route(
                        kind="review_request",
                        receiver="codex",
                        preflight_target=_Target(WS, LANE),
                        source="redmine",
                        mode="queue-enter",
                        anchor=None,
                        target="wProj:p9",
                        record_format="text",
                        record_command=None,
                        emit=lambda outcome, **kw: emitted.append(outcome),
                        allow_direct_worker=False,
                        sender_lane_unit=(None, None),
                    )
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].reason, "reader_upgrade_required")
        self.assertNotEqual(emitted[0].reason, "gateway_route_blocked")

    def test_resolve_disposition_flags_upgrade_required(self) -> None:
        self._current_store()
        with patch.object(sch, "_RECOGNIZED_SCHEMA_VERSIONS", frozenset({1, 2, 3, 4, 5})):
            with patch.dict(
                os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
            ):
                self.assertEqual(
                    gateway_route_gate._resolve_target_disposition(_Target(WS, LANE)),
                    (None, True, True),
                )


# -- F3: faithful review_request delivery + deterministic concurrency --------------------


class FaithfulReviewAndConcurrencyTest(unittest.TestCase):
    """Review j#79471 F3: exercise the CLAIMED paths — a same-lane review_request exact-route
    delivery over a v5 store from a v6 source, and a real concurrent read + migration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enforce_review(self):
        emitted = []
        with patch.dict(
            os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False
        ), patch.object(
            gateway_route_gate, "load_workflow_binding", return_value=(_FakeBinding(), [])
        ), patch.object(gateway_route_gate, "die", side_effect=_Die):
            enforce_gateway_route(
                kind="review_request",
                receiver="codex",
                preflight_target=_Target(WS, LANE),
                source="redmine",
                mode="standard",
                anchor=None,
                target="wProj:p9",
                record_format="text",
                record_command=None,
                emit=lambda outcome, **kw: emitted.append(outcome),
                allow_direct_worker=False,
                sender_lane_unit=(None, None),
            )
        return emitted

    def test_v6_source_review_request_routes_active_v5_lane_no_migrate(self) -> None:
        # Faithful to the acceptance path: kind=review_request (not a stand-in), a v5 active
        # lane, a v6 source command. The exact-route delivery is NOT blocked by the lifecycle
        # authority, and the v6 read does NOT migrate the shared store — so the concurrent v5
        # reader keeps working (version stays 5).
        path = _seed_v5(self.home)
        before = _digest(path)
        emitted = self._enforce_review()
        self.assertEqual(emitted, [])  # not blocked; delivery proceeds to the exact route
        self.assertEqual(_digest(path), before)
        self.assertEqual(_recorded_version(self.home), 5)
        # a subsequent v5-shaped read still resolves the exact lane (transport not stalled)
        rec = LaneLifecycleReader(home=self.home).get(LaneLifecycleKey(WS, LANE))
        self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)

    def test_concurrent_read_during_migration_is_consistent(self) -> None:
        # A deterministic barrier-synchronized race: one thread runs the explicit v5->v6
        # migration while another reads the same store. The reader must never see a torn shape
        # (it waits out the migration commit via busy_timeout), and the store ends at v6.
        path = _seed_v5(self.home)
        barrier = threading.Barrier(2)
        errors: list = []
        read_dispositions: list = []

        def _migrate():
            try:
                barrier.wait()
                ensure_lane_lifecycle_schema(path)
            except Exception as exc:  # noqa: BLE001 - surface any race failure to the assert
                errors.append(("migrate", exc))

        def _read():
            try:
                barrier.wait()
                for _ in range(20):
                    rec = LaneLifecycleReader(path=path).get(LaneLifecycleKey(WS, LANE))
                    # whether it reads the pre- or post-migration committed shape, the record is
                    # whole and the disposition is the same authoritative value.
                    read_dispositions.append(rec.lane_disposition if rec else None)
            except Exception as exc:  # noqa: BLE001
                errors.append(("read", exc))

        threads = [threading.Thread(target=_migrate), threading.Thread(target=_read)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])  # no torn read, no spurious lock failure
        self.assertTrue(read_dispositions)
        self.assertTrue(all(d == DISPOSITION_ACTIVE for d in read_dispositions))
        self.assertEqual(_recorded_version(self.home), 6)  # the migration committed

    def test_migration_restart_idempotent_under_repeated_ensure(self) -> None:
        # Restart idempotency: repeated explicit ensures after the first migration are intact,
        # never a second migration or a second backup.
        path = _seed_v5(self.home)
        first = ensure_lane_lifecycle_schema(path)
        self.assertEqual(first.action, SCHEMA_MIGRATED)
        for _ in range(3):
            again = ensure_lane_lifecycle_schema(path)
            self.assertEqual(again.action, SCHEMA_INTACT)
        self.assertEqual(len(list((self.home / "backups").glob("state-*"))), 1)


# -- R2-F1: explicit write gate wired to EVERY mutation; reads never migrate --------------


class UniversalWriteGateTest(unittest.TestCase):
    """Review R2 j#79512 F1: the explicit write gate (preflight FIRST + typed outcome) runs for
    EVERY schema-needing mutation, not only declaration; reads never migrate."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_v5_with_peer(self):
        """A v5 store with LANE active AND a second active peer lane."""
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(
            LaneLifecycleKey(WS, LANE), decision=_issue_decision(), issue_id=ISSUE
        )
        store.declare_active(
            LaneLifecycleKey(WS, "issue_13800_peer_lane"),
            decision=DecisionPointer(source="redmine", issue_id="13800", journal_id="1"),
            issue_id="13800",
        )
        path = lane_lifecycle_path(self.home)
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

    def test_disposition_mutation_runs_gate_preflight_before_migration(self) -> None:
        # A hibernate/resume commit (transition_disposition) — NOT declaration — runs the gate:
        # preflight peers on the PRE-migration store, then migrate, typed outcome captured.
        self._seed_v5_with_peer()
        store = LaneLifecycleStore(home=self.home)
        rec = store.get(LaneLifecycleKey(WS, LANE))  # read: must not migrate
        self.assertEqual(_recorded_version(self.home), 5)
        out = store.transition_disposition(
            LaneLifecycleKey(WS, LANE),
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target="hibernated",
            decision=_issue_decision(),
        )
        self.assertTrue(out.applied)
        prep = store.last_write_preparation
        self.assertTrue(prep.migrated)
        self.assertEqual(prep.outcome.from_version, 5)
        self.assertEqual(prep.preflight.current_version, 5)  # read BEFORE migration
        self.assertIn("issue_13800_peer_lane", prep.preflight.peer_active_lanes)
        self.assertNotIn(LANE, prep.preflight.peer_active_lanes)  # writer excluded
        self.assertTrue(prep.peer_reader_risk)
        self.assertEqual(_recorded_version(self.home), 6)

    def test_release_mutation_runs_gate(self) -> None:
        # A release open (request_release) on a non-active lane also runs the gate.
        self._seed_v5_with_peer()
        store = LaneLifecycleStore(home=self.home)
        rec = store.get(LaneLifecycleKey(WS, LANE))
        store.transition_disposition(  # -> hibernated (this migrates the store)
            LaneLifecycleKey(WS, LANE),
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target="hibernated",
            decision=_issue_decision(),
        )
        self.assertEqual(_recorded_version(self.home), 6)
        # request_release still goes through the gate (intact now); last_write_preparation set.
        rec2 = store.get(LaneLifecycleKey(WS, LANE))
        from mozyo_bridge.core.state.lane_lifecycle import ReleasePin

        store.request_release(
            LaneLifecycleKey(WS, LANE),
            expected_revision=rec2.revision,
            action_id="act-1",
            pins=[ReleasePin(role="codex", assigned_name="n", locator="wProj:p2")],
        )
        self.assertIsNotNone(store.last_write_preparation)
        self.assertEqual(store.last_write_preparation.outcome.action, SCHEMA_INTACT)

    def test_composing_store_mutation_runs_gate(self) -> None:
        # A composing store (declaration) mutation also opens through the shared gate and
        # surfaces the preparation on the wrapped store.
        self._seed_v5_with_peer()
        decl = LaneDeclarationStore(home=self.home)
        decl.declare_lane(
            LaneLifecycleKey(WS, "issue_13900_new_lane"),
            decision=DecisionPointer(source="redmine", issue_id="13900", journal_id="2"),
            binding_kind=BINDING_KIND_ISSUE,
            issue_id="13900",
        )
        prep = decl.last_write_preparation
        self.assertTrue(prep.migrated)
        self.assertEqual(prep.preflight.current_version, 5)
        self.assertTrue(prep.peer_reader_risk)

    def test_all_store_reads_are_non_migrating(self) -> None:
        # Redmine #13844 R2 condition 4: get / records / resolve_owner never migrate — even
        # inside a mutating flow, the read comes before the write's gate without migrating.
        path = self._seed_v5_with_peer()
        before = _digest(path)
        store = LaneLifecycleStore(home=self.home)
        store.get(LaneLifecycleKey(WS, LANE))
        store.records()
        store.resolve_owner(WS, ISSUE)
        self.assertEqual(_digest(path), before)
        self.assertEqual(_recorded_version(self.home), 5)
        self.assertFalse((self.home / "backups").exists())

    def test_advisory_helper_formats_only_on_peer_risk(self) -> None:
        from mozyo_bridge.core.state.lane_lifecycle_readonly import (
            format_lifecycle_migration_advisory,
        )

        self._seed_v5_with_peer()
        store = LaneLifecycleStore(home=self.home)
        rec = store.get(LaneLifecycleKey(WS, LANE))
        store.transition_disposition(
            LaneLifecycleKey(WS, LANE),
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target="hibernated",
            decision=_issue_decision(),
        )
        # Redmine #13844 R3: the advisory is PRE-migration and takes the preflight (read on the
        # still-old store), whose current_version is 5 -> peers_at_risk -> advisory.
        msg = format_lifecycle_migration_advisory(store.last_write_preparation.preflight)
        self.assertIsNotNone(msg)
        self.assertIn("issue_13800_peer_lane", msg)
        self.assertIn("13844", msg)
        self.assertIn("ABOUT TO", msg)  # pre-migration wording, not post-hoc
        # a current store with no pending migration -> no advisory
        fresh = LaneLifecycleStore(home=self.home)
        fresh.prepare_write(writer_workspace_id=WS, writer_lane_id=LANE)
        self.assertIsNone(
            format_lifecycle_migration_advisory(fresh.last_write_preparation.preflight)
        )


class RealCommandMigrationAdvisoryTest(unittest.TestCase):
    """Review R2 j#79512 F1 condition 3: a REAL non-declaration command (hibernate) run on a v5
    store with an active peer runs the preflight before migration and emits the typed advisory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_hibernate_command_emits_peer_advisory_and_migrates_after_preflight(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            encode_assigned_name,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E501
            HerdrRetireCloseResult,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
            HibernateAssertions,
            HibernateRequest,
            SublaneHibernateUseCase,
        )

        hib_issue, hib_lane, hib_journal = "13441", "issue_13441_provider", "77485"

        class _FakeOps:
            def __init__(self, rows):
                self._rows = rows

            def workspace_id(self):
                return WS

            def read_inventory(self):
                return list(self._rows), True

            def execute_close(self, plan):
                return HerdrRetireCloseResult(
                    workspace_id=plan.workspace_id,
                    lane_id=plan.lane_id,
                    closed=tuple(plan.close_targets),
                    failed=(),
                    foreign_names=plan.foreign_names,
                )

        # A v5 store: the lane to hibernate is active, AND a second active peer lane exists.
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(
            LaneLifecycleKey(WS, hib_lane),
            decision=DecisionPointer(source="redmine", issue_id=hib_issue, journal_id=hib_journal),
            issue_id=hib_issue,
        )
        store.declare_active(
            LaneLifecycleKey(WS, "issue_13800_peer_lane"),
            decision=DecisionPointer(source="redmine", issue_id="13800", journal_id="1"),
            issue_id="13800",
        )
        path = lane_lifecycle_path(self.home)
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
        self.assertEqual(_recorded_version(self.home), 5)

        ops = _FakeOps(
            [
                {"name": encode_assigned_name(WS, "codex", hib_lane), "pane_id": f"{WS}:p2"},
                {"name": encode_assigned_name(WS, "claude", hib_lane), "pane_id": f"{WS}:p3"},
            ]
        )
        request = HibernateRequest(
            issue=hib_issue,
            lane=hib_lane,
            journal=hib_journal,
            assertions=HibernateAssertions(
                explicitly_parked=True,
                callbacks_drained=True,
                no_review_pending=True,
                no_owner_approval_pending=True,
                no_integration_pending=True,
                no_pending_prompt=True,
                not_working=True,
                worktree_clean=True,
                boundary_recorded=False,
            ),
        )

        import io
        import contextlib

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            outcome = SublaneHibernateUseCase(ops=ops, store=store).run(
                request, execute=True
            )

        self.assertTrue(outcome.transition.applied)
        # The advisory is emitted at the transition commit (the schema-changing write), naming
        # the peer read from the PRE-migration (v5) store and the 5 -> 6 forward migration — proof
        # the preflight ran before the migration. (Note store.last_write_preparation at the END
        # reflects the LATER release write on the now-v6 store, not the migrating transition; the
        # emitted advisory is the authoritative signal.)
        advisory = err.getvalue()
        self.assertIn("advisory (Redmine #13844)", advisory)
        self.assertIn("ABOUT TO forward-migrate", advisory)  # pre-migration wording
        self.assertIn("issue_13800_peer_lane", advisory)
        self.assertIn("v5 -> v6", advisory)
        self.assertEqual(_recorded_version(self.home), 6)


# -- R3-F1: the advisory is emitted BEFORE the migration (time-series) -------------------


class PreMigrationAdvisoryTimingTest(unittest.TestCase):
    """Review R3 j#79534 F1: the operator advisory is a PRE-migration preflight — at the moment
    it is emitted the store is still v5 / backup 0; only AFTER does it become v6 / backup 1."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_v5_with_peer(self) -> Path:
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(
            LaneLifecycleKey(WS, LANE), decision=_issue_decision(), issue_id=ISSUE
        )
        store.declare_active(
            LaneLifecycleKey(WS, "issue_13800_peer_lane"),
            decision=DecisionPointer(source="redmine", issue_id="13800", journal_id="1"),
            issue_id="13800",
        )
        path = lane_lifecycle_path(self.home)
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

    def test_advisory_fires_at_v5_backup0_then_migration_makes_v6_backup1(self) -> None:
        self._seed_v5_with_peer()

        # Snapshot the on-disk state AT THE MOMENT the advisory is emitted — it must still be the
        # OLD version with no backup (a genuine PRE-migration preflight, not a post-hoc notice).
        snap = {}
        orig = ll.emit_lifecycle_migration_advisory

        def _traced(preflight, **kw):
            fired = orig(preflight, **kw)
            if fired:
                snap["version_at_emit"] = _recorded_version(self.home)
                snap["backups_at_emit"] = (
                    len(list((self.home / "backups").glob("state-*")))
                    if (self.home / "backups").exists()
                    else 0
                )
            return fired

        store = LaneLifecycleStore(home=self.home)
        rec = store.get(LaneLifecycleKey(WS, LANE))
        with patch.object(ll, "emit_lifecycle_migration_advisory", _traced):
            store.transition_disposition(
                LaneLifecycleKey(WS, LANE),
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=rec.revision,
                target="hibernated",
                decision=_issue_decision(),
            )

        # The advisory fired while the store was STILL v5 with no backup taken yet ...
        self.assertEqual(snap.get("version_at_emit"), 5)
        self.assertEqual(snap.get("backups_at_emit"), 0)
        # ... and only the subsequent migration made it v6 with the backup-first snapshot.
        self.assertEqual(_recorded_version(self.home), 6)
        self.assertEqual(len(list((self.home / "backups").glob("state-*"))), 1)


# -- R3-F2: composing-store mutations carry the typed migration to their command outcomes -


class ComposingStoreMigrationSurfaceTest(unittest.TestCase):
    """Review R3 j#79534 F2: replacement / retire / reconcile mutations run through the same
    gate AND their typed migration reaches the command surface (structured JSON/text)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_v5_with_peer(self) -> Path:
        store = LaneLifecycleStore(home=self.home)
        store.declare_active(
            LaneLifecycleKey(WS, LANE), decision=_issue_decision(), issue_id=ISSUE
        )
        store.declare_active(
            LaneLifecycleKey(WS, "issue_13800_peer_lane"),
            decision=DecisionPointer(source="redmine", issue_id="13800", journal_id="1"),
            issue_id="13800",
        )
        path = lane_lifecycle_path(self.home)
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

    def test_retire_migration_store_cas_migrates_and_exposes_preparation(self) -> None:
        from mozyo_bridge.core.state.lane_retire_migration import LaneRetireMigrationStore
        from mozyo_bridge.core.state.lane_lifecycle_readonly import (
            lifecycle_migration_payload,
        )

        self._seed_v5_with_peer()
        store = LaneRetireMigrationStore(home=self.home)
        # The row is ACTIVE (not released/hibernated) so this CAS REFUSES — but the gate still
        # migrates the shared store (in _connect_write, before the CAS transaction), and exposes
        # the typed preparation the command threads into its verdict.
        store.retire_released_hibernated_legacy(
            LaneLifecycleKey(WS, LANE),
            expected_revision=1,
            issue_id=ISSUE,
            decision=_issue_decision(),
        )
        prep = store.last_write_preparation
        self.assertTrue(prep.migrated)
        self.assertEqual(prep.preflight.current_version, 5)  # read pre-migration
        self.assertIn("issue_13800_peer_lane", prep.preflight.peer_active_lanes)
        payload = lifecycle_migration_payload(prep)
        self.assertEqual(payload["from_version"], 5)
        self.assertIn("issue_13800_peer_lane", payload["peer_active_lanes"])

    def test_reconcile_store_cas_migrates_and_exposes_preparation(self) -> None:
        from mozyo_bridge.core.state.lane_reconcile_binding import (
            LaneReconcileBindingStore,
        )

        from mozyo_bridge.core.state.lane_lifecycle import ProcessGenerationPin

        self._seed_v5_with_peer()
        store = LaneReconcileBindingStore(home=self.home)
        # Valid pins so arg-validation passes and the CAS opens (via _connect_write, which
        # migrates) before it refuses on the row's state.
        store.retire_reconciled_hibernated_legacy(
            LaneLifecycleKey(WS, LANE),
            expected_revision=1,
            issue_id=ISSUE,
            worktree_identity="wt_x",
            declared_slots=(
                ProcessGenerationPin(
                    role="codex", provider="codex", assigned_name="n", locator="wProj:p2"
                ),
            ),
            decision=_issue_decision(),
        )
        prep = store.last_write_preparation
        self.assertTrue(prep.migrated)
        self.assertEqual(prep.preflight.current_version, 5)

    def test_replacement_store_cas_migrates_and_exposes_preparation(self) -> None:
        from mozyo_bridge.core.state.lane_replacement import LaneReplacementStore
        from mozyo_bridge.core.state.lane_lifecycle import ReleasePin

        self._seed_v5_with_peer()
        store = LaneReplacementStore(home=self.home)
        store.request_replacement(
            LaneLifecycleKey(WS, LANE),
            expected_revision=1,
            action_id="act",
            pins=[ReleasePin(role="codex", assigned_name="n", locator="wProj:p2")],
            decision=_issue_decision(),
        )
        prep = store.last_write_preparation
        self.assertTrue(prep.migrated)
        self.assertEqual(prep.preflight.current_version, 5)

    def test_command_verdicts_serialize_lifecycle_migration(self) -> None:
        # The three composing-store commands' outcomes carry lifecycle_migration in JSON (R3-F2
        # condition 1: auditable in JSON/text, not only stderr).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_live_reconcile import (  # noqa: E501
            HibernatedLiveReconcileVerdict,
            RECONCILE_RECONCILED,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_legacy_retire import (  # noqa: E501
            HibernatedLegacyRetireVerdict,
            MIGRATE_RETIRED,
        )

        rec = {"from_version": 5, "to_version": 6, "backup_dir": "/b", "peer_active_lanes": ["p"], "peer_reader_risk": True}
        v1 = HibernatedLiveReconcileVerdict(state=RECONCILE_RECONCILED, lifecycle_migration=rec)
        self.assertEqual(v1.as_payload()["lifecycle_migration"], rec)
        v2 = HibernatedLegacyRetireVerdict(state=MIGRATE_RETIRED, lifecycle_migration=rec)
        self.assertEqual(v2.as_payload()["lifecycle_migration"], rec)
        # QuarantineOutcome carries it too (its as_payload includes the key); verified end-to-end
        # by the quarantine command suite. The reconcile / retire verdicts are checked here.


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
