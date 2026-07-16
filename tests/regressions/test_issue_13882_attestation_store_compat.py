"""Redmine #13882: shared-home attestation-store schema admission + compat.

The incident: an isolated 0.12.0a2 launcher passed the #13847 capability probe, launched a
fresh pair against a shared ``MOZYO_BRIDGE_HOME``, and both slots came up with **no**
post-launch attestation — ``partial_pair_recovery_required``, and every recovery path
(re-adopt, ``recover-pair``) refusing. The cause was not the launcher: the selected home
held the pre-0.12 **v1** attestation shape while the runtime required v2. The probe could
not see it, because it joins the launcher's *advertised* schema against the *source
runtime's required* schema — both **code**. Nothing opened the store on disk.

The regression matrix required by acceptance 5, plus the two guard bites that would let
the fix regress silently:

- ``StoreSchemaJoinTest`` — old-store / new-launcher and the launcher-set join (acc. 1);
- ``ReadCompatibleTest`` — old-store / new-reader, and reads never migrating (acc. 2);
- ``WriteConservativeTest`` — normal vs replacement launch onto v1 (acc. 2);
- ``OldReaderTest`` — new-store / old-reader fails closed, never silently (acc. 4);
- ``MaintenanceCommandTest`` — backup-first / idempotent / consumer-gated (acc. 3);
- ``PartialWriteAndReplayTest`` — partial-write failure + crash/replay (acc. 5);
- ``ZeroSideEffectTest`` — an incompatible store creates no workspace/tab/agent (acc. 1).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT.parent / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    HerdrIdentityAttestationError,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    herdr_identity_attestation_path,
)
import mozyo_bridge.core.state.herdr_identity_attestation_schema as sch  # noqa: E402
from mozyo_bridge.core.state.state_store import StateStoreError  # noqa: E402
from mozyo_bridge.core.state.herdr_identity_attestation_schema import (  # noqa: E402
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    RECOGNIZED_SCHEMA_VERSIONS,
    STORE_ABSENT,
    STORE_RECOGNIZED,
    STORE_UNREADABLE,
    STORE_UNSUPPORTED,
    StoreSchemaObservation,
    probe_store_schema,
)
import mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_attestation_store_maintenance as mnt  # noqa: E402,E501
from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_attestation_store_maintenance import (  # noqa: E402,E501
    ALREADY_CURRENT,
    APPLIED,
    BLOCKED_CONSUMERS_LIVE,
    BLOCKED_CONSUMERS_UNMEASURABLE,
    BLOCKED_INVENTORY_UNREADABLE,
    BLOCKED_FAILED,
    BLOCKED_MIGRATE_INSTEAD,
    PLANNED,
    run_attestation_store_migrate,
    run_attestation_store_rebuild,
    run_attestation_store_status,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E402,E501
    STORE_JOIN_OK,
    STORE_LAUNCHER_CANNOT_WRITE,
    STORE_REPLACEMENT_UNSUPPORTED,
    STORE_UNREADABLE as VERDICT_STORE_UNREADABLE,
    STORE_UNSUPPORTED as VERDICT_STORE_UNSUPPORTED,
    LauncherCapabilityObservation,
    build_attest_capability_stores_line,
    decide_store_compatibility,
    parse_launcher_capability_output,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E402,E501
    HerdrLauncherIncompatibleError,
    preflight_attest_store_schema,
)

_V2 = HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
_NAME = "mzb1_ws1_claude_default"
_ACTION_ID = "recover:l:worker:claude:wk:w2"

# A #13882 build: advertises the writable SET. A pre-#13882 build advertises the native
# schema only — the distinction the store join rests on.
_NEW_LAUNCHER = LauncherCapabilityObservation(True, _V2, frozenset(RECOGNIZED_SCHEMA_VERSIONS))
_OLD_LAUNCHER = LauncherCapabilityObservation(True, _V2, None)

_V1_DDL = (
    "CREATE TABLE herdr_identity_attestations ("
    "assigned_name TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, role TEXT NOT NULL, "
    "lane_id TEXT NOT NULL, locator TEXT NOT NULL, verdict TEXT NOT NULL, "
    "detail TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL)"
)


def _seed_v1(home: Path, *, rows: int = 1) -> Path:
    """A genuine v1 store — the exact shape measured on the real shared home."""
    path = herdr_identity_attestation_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA user_version = 1")
        conn.execute(_V1_DDL)
        for i in range(rows):
            conn.execute(
                "INSERT INTO herdr_identity_attestations VALUES (?,?,?,?,?,?,?,?)",
                (f"mzb1_ws1_claude_lane{i}" if i else _NAME, "ws1", "claude",
                 f"lane{i}" if i else "default", "wY:p2", "present", "", "t0"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _rec(**over) -> IdentityAttestationRecord:
    base = dict(
        assigned_name=_NAME, workspace_id="ws1", role="claude", lane_id="default",
        locator="wY:p2", verdict="present",
    )
    base.update(over)
    return IdentityAttestationRecord(**base)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _store_content(path: Path) -> tuple:
    """``(user_version, row_count)`` — what a recovery point must actually preserve."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        rows = conn.execute("SELECT count(*) FROM herdr_identity_attestations").fetchone()[0]
    finally:
        conn.close()
    return version, rows


def _seed_v1_wal(home: Path) -> tuple:
    """A v1 store in WAL mode with a committed row still living in the ``-wal`` sidecar.

    Returns ``(path, open_conn)`` — the caller MUST keep the connection open, because
    closing the last connection checkpoints the WAL into the main DB and dissolves the
    very condition under test (the first attempt to reproduce review j#80000 finding 1
    failed for exactly that reason).
    """
    path = herdr_identity_attestation_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("PRAGMA user_version = 1")
    conn.execute(_V1_DDL)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # schema lands in the main DB
    conn.execute(
        "INSERT INTO herdr_identity_attestations VALUES (?,?,?,?,?,?,?,?)",
        (_NAME, "ws1", "claude", "default", "wY:p2", "present", "", "t0"),
    )
    conn.commit()  # committed, but the page lives in -wal
    return path, conn


class _View:
    """A liveness view stand-in (the shape `read_herdr_inventory` returns)."""

    def __init__(self, *, backend_selected=True, ok=True, agents=(), reason=None, detail=""):
        self.backend_selected = backend_selected
        self.ok = ok
        self.reason = reason
        self.detail = detail
        self.managed_agents = tuple(agents)


class _Agent:
    def __init__(self, name):
        self.name = name


_NO_CONSUMERS = _View(agents=())


class StoreSchemaJoinTest(unittest.TestCase):
    """Acceptance 1: the preflight joins launcher capability with the REAL store shape."""

    def test_v1_store_with_pre_13882_launcher_is_refused(self) -> None:
        # THE incident, exactly: both sides say schema v2, so the #13847 probe passes —
        # but the store on disk is v1 and this launcher can only write v2. Before #13882
        # this launched and boots live-but-unattested; now it is refused before any write.
        verdict = decide_store_compatibility(
            _OLD_LAUNCHER,
            StoreSchemaObservation(STORE_RECOGNIZED, 1),
            required_schema_version=_V2,
            replacement_launch=False,
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, STORE_LAUNCHER_CANNOT_WRITE)

    def test_v1_store_with_compatible_launcher_normal_launch_is_admitted(self) -> None:
        # Acceptance 2: a normal launch may use the proven backward-compatible path.
        verdict = decide_store_compatibility(
            _NEW_LAUNCHER,
            StoreSchemaObservation(STORE_RECOGNIZED, 1),
            required_schema_version=_V2,
            replacement_launch=False,
        )
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.reason, STORE_JOIN_OK)

    def test_v1_store_replacement_launch_is_refused_even_when_compatible(self) -> None:
        # `action_id` must never be dropped — the v1 shape cannot carry it.
        verdict = decide_store_compatibility(
            _NEW_LAUNCHER,
            StoreSchemaObservation(STORE_RECOGNIZED, 1),
            required_schema_version=_V2,
            replacement_launch=True,
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, STORE_REPLACEMENT_UNSUPPORTED)

    def test_absent_store_is_admitted(self) -> None:
        verdict = decide_store_compatibility(
            _NEW_LAUNCHER,
            StoreSchemaObservation(STORE_ABSENT, None),
            required_schema_version=_V2,
            replacement_launch=True,
        )
        self.assertTrue(verdict.ok)

    def test_unreadable_and_unsupported_stores_are_refused(self) -> None:
        for observation, reason in (
            (StoreSchemaObservation(STORE_UNREADABLE, None), VERDICT_STORE_UNREADABLE),
            (StoreSchemaObservation(STORE_UNSUPPORTED, 9, True), VERDICT_STORE_UNSUPPORTED),
        ):
            with self.subTest(state=observation.state):
                verdict = decide_store_compatibility(
                    _NEW_LAUNCHER, observation,
                    required_schema_version=_V2, replacement_launch=False,
                )
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.reason, reason)

    def test_newer_store_names_upgrade_not_corruption(self) -> None:
        # Honest operator guidance: "too old a runtime" and "corrupt file" are different
        # problems with different fixes, and only one of them is the operator's to repair.
        newer = decide_store_compatibility(
            _NEW_LAUNCHER, StoreSchemaObservation(STORE_UNSUPPORTED, 9, True),
            required_schema_version=_V2, replacement_launch=False,
        )
        corrupt = decide_store_compatibility(
            _NEW_LAUNCHER, StoreSchemaObservation(STORE_UNSUPPORTED, 2, False),
            required_schema_version=_V2, replacement_launch=False,
        )
        self.assertIn("newer runtime", newer.detail)
        self.assertNotIn("newer runtime", corrupt.detail)
        self.assertIn("corrupt", corrupt.detail)

    def test_launcher_advertises_writable_set_and_it_survives_help_rendering(self) -> None:
        # GUARD BITE: without the SET token a pre-#13882 build and a v1-compatible one
        # are indistinguishable (both advertise `2`), and the join above silently
        # re-admits the incident. The token must also survive argparse's help wrapping —
        # the #13847 lesson that a split token reads as "incapable".
        line = build_attest_capability_stores_line(RECOGNIZED_SCHEMA_VERSIONS)
        self.assertNotIn(" ", line)
        self.assertNotIn("-", line.split("=")[1])
        parsed = parse_launcher_capability_output(f"usage: x --assigned-name N\n{line}\n")
        self.assertEqual(parsed.advertised_store_versions, frozenset(RECOGNIZED_SCHEMA_VERSIONS))

    def test_pre_13882_launcher_is_credited_only_with_its_native_schema(self) -> None:
        self.assertEqual(_OLD_LAUNCHER.writable_store_versions, frozenset({_V2}))

    def test_real_cli_help_advertises_a_set_that_matches_this_build(self) -> None:
        # The advertised set must be the shapes this build ACTUALLY writes, or the join
        # is authoritative over a lie.
        import contextlib
        import io

        from mozyo_bridge.application.cli import build_parser

        buf = io.StringIO()
        with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
            build_parser().parse_args(["herdr", "agent-attest", "--help"])
        parsed = parse_launcher_capability_output(buf.getvalue())
        self.assertEqual(
            parsed.advertised_store_versions, frozenset(RECOGNIZED_SCHEMA_VERSIONS)
        )


class ReadCompatibleTest(unittest.TestCase):
    """Acceptance 2/5: old-store / new-reader."""

    def test_v1_rows_decode_instead_of_reading_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1(home)
            got = HerdrIdentityAttestationStore(home=home).read(_NAME)
            self.assertIsNotNone(got)
            self.assertEqual(got.verdict, "present")
            self.assertEqual(got.replacement_action_id, "")

    def test_read_never_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            before = _digest(path)
            HerdrIdentityAttestationStore(home=home).read(_NAME)
            self.assertEqual(_digest(path), before)
            self.assertFalse((home / "backups").exists())

    def test_probe_creates_nothing_on_an_absent_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            observation = probe_store_schema(herdr_identity_attestation_path(home))
            self.assertEqual(observation.state, STORE_ABSENT)
            self.assertFalse(herdr_identity_attestation_path(home).exists())

    def test_shape_disagreeing_with_version_is_unsupported(self) -> None:
        # A recognized version whose columns disagree is partial/corrupt: compatibility is
        # judged by the shape table, never by the version number alone.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            conn = sqlite3.connect(str(path))
            try:
                conn.execute("ALTER TABLE herdr_identity_attestations ADD COLUMN junk TEXT")
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(probe_store_schema(path).state, STORE_UNSUPPORTED)
            self.assertIsNone(HerdrIdentityAttestationStore(home=home).read(_NAME))


class WriteConservativeTest(unittest.TestCase):
    """Acceptance 2: normal launch writes v1-shaped; replacement launch refuses."""

    def test_normal_launch_writes_v1_shaped_without_migrating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            HerdrIdentityAttestationStore(home=home).upsert(_rec(locator="wZ:p9"))
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                # GUARD BITE: auto-migrating the shared home here would break every older
                # installed launcher — the #13882 defect inverted onto the old runtimes.
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(herdr_identity_attestations)")}
                self.assertNotIn("replacement_action_id", cols)
            finally:
                conn.close()
            self.assertEqual(HerdrIdentityAttestationStore(home=home).read(_NAME).locator, "wZ:p9")

    def test_replacement_launch_onto_v1_refuses_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            before = _digest(path)
            with self.assertRaises(HerdrIdentityAttestationError) as ctx:
                HerdrIdentityAttestationStore(home=home).upsert(
                    _rec(locator="wZ:p9", replacement_action_id=_ACTION_ID)
                )
            self.assertIn("replacement", str(ctx.exception))
            self.assertEqual(_digest(path), before)  # untouched, not partially written

    def test_fresh_store_is_created_at_current_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = HerdrIdentityAttestationStore(home=home)
            store.upsert(_rec(replacement_action_id=_ACTION_ID))
            self.assertEqual(
                probe_store_schema(herdr_identity_attestation_path(home)).version, _V2
            )
            self.assertEqual(store.read(_NAME).replacement_action_id, _ACTION_ID)

    def test_best_effort_writer_still_never_raises_into_a_boot(self) -> None:
        # An agent boot must never be blocked by a store failure — the refusal above is
        # surfaced by the PREFLIGHT (before launch), not by crashing the child.
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            record_identity_attestation,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1(home)
            self.assertIsNone(
                record_identity_attestation(
                    _rec(replacement_action_id=_ACTION_ID), home=home
                )
            )


class OldReaderTest(unittest.TestCase):
    """Acceptance 4/5: new-store / old-reader fails closed, never silently."""

    def test_v1_only_build_reading_a_migrated_v2_store_fails_closed(self) -> None:
        # Emulate a genuine pre-#13806 build by narrowing the recognized set, exactly the
        # #13844 technique. This is WHY a launch must not migrate the shared home: an old
        # reader gets nothing rather than a wrong answer, but it gets nothing.
        import mozyo_bridge.core.state.herdr_identity_attestation_schema as sch

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            HerdrIdentityAttestationStore(home=home).upsert(_rec())
            with mock.patch.object(sch, "RECOGNIZED_SCHEMA_VERSIONS", frozenset({1})):
                self.assertIsNone(HerdrIdentityAttestationStore(home=home).read(_NAME))

    def test_old_reader_sees_a_v1_store_unchanged_after_a_normal_launch(self) -> None:
        # Acceptance 4: old CLI behavior stays compatible on an unmigrated home.
        import mozyo_bridge.core.state.herdr_identity_attestation_schema as sch

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1(home)
            HerdrIdentityAttestationStore(home=home).upsert(_rec(locator="wZ:p9"))
            with mock.patch.object(sch, "RECOGNIZED_SCHEMA_VERSIONS", frozenset({1})):
                got = HerdrIdentityAttestationStore(home=home).read(_NAME)
            self.assertIsNotNone(got)
            self.assertEqual(got.locator, "wZ:p9")


class MaintenanceCommandTest(unittest.TestCase):
    """Acceptance 3: backup-first, idempotent, consumer-gated, no raw SQLite."""

    def test_status_is_read_only_and_reports_the_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            before = _digest(path)
            result = run_attestation_store_status(home=home)
            self.assertTrue(result.ok)
            self.assertEqual(result.store_version, 1)
            self.assertEqual(_digest(path), before)

    def test_migrate_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            before = _digest(path)
            result = run_attestation_store_migrate(home=home, view=_NO_CONSUMERS)
            self.assertEqual(result.state, PLANNED)
            self.assertFalse(result.executed)
            self.assertEqual(_digest(path), before)
            self.assertFalse((home / "backups").exists())

    def test_migrate_is_backup_first_and_preserves_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home, rows=3)
            result = run_attestation_store_migrate(home=home, view=_NO_CONSUMERS, write=True)
            self.assertEqual(result.state, APPLIED)
            self.assertEqual(probe_store_schema(path).version, _V2)
            # Content equality, not byte equality (review j#80000 finding 1): the snapshot
            # is a SQLite backup-API copy, so it is logically identical but not byte-wise.
            # Content is the property that matters — a byte-equal snapshot that lost a
            # WAL-committed row is worthless.
            self.assertEqual(_store_content(result.backup_dir / path.name), (1, 3))
            self.assertEqual(_store_content(path), (_V2, 3))

    def test_migrate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1(home)
            run_attestation_store_migrate(home=home, view=_NO_CONSUMERS, write=True)
            again = run_attestation_store_migrate(home=home, view=_NO_CONSUMERS, write=True)
            self.assertEqual(again.state, ALREADY_CURRENT)
            self.assertTrue(again.ok)
            self.assertEqual(len(list((home / "backups").iterdir())), 1)  # no second backup

    def test_migrate_refuses_while_consumers_are_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            before = _digest(path)
            result = run_attestation_store_migrate(
                home=home, view=_View(agents=[_Agent("mzb1_ws1_claude_default")]), write=True
            )
            self.assertEqual(result.state, BLOCKED_CONSUMERS_LIVE)
            self.assertFalse(result.ok)
            self.assertEqual(_digest(path), before)

    def test_migrate_refuses_when_liveness_is_unmeasurable(self) -> None:
        # GUARD BITE: an unreadable inventory is NOT an empty one. Folding it to "no
        # consumers" is the #13682 R1-F1 / #13754 anti-pattern.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            before = _digest(path)
            result = run_attestation_store_migrate(
                home=home,
                view=_View(ok=False, reason="transport_error", detail="herdr down"),
                write=True,
            )
            self.assertEqual(result.state, BLOCKED_INVENTORY_UNREADABLE)
            self.assertEqual(_digest(path), before)

    def test_live_zero_read_precedes_the_idempotent_success(self) -> None:
        # A replay must never report success while consumers are live (#13841 j#79150 f2).
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            HerdrIdentityAttestationStore(home=home).upsert(_rec())  # already v2
            result = run_attestation_store_migrate(
                home=home, view=_View(agents=[_Agent(_NAME)]), write=True
            )
            self.assertEqual(result.state, BLOCKED_CONSUMERS_LIVE)

    def test_consumers_are_scoped_to_agents_holding_a_record_in_this_store(self) -> None:
        # A live agent that never attested into THIS home is not a consumer of it. herdr
        # cannot reveal a live process's injected home, so a stored row is the only
        # evidence tying an agent to this store; counting every agent on the server would
        # refuse an unrelated scratch home forever (measured live: 18 agents blocked a
        # home none of them had ever written).
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1(home)  # holds a record for _NAME only
            other = run_attestation_store_migrate(
                home=home, view=_View(agents=[_Agent("mzb1_other_codex_default")]), write=True
            )
            self.assertEqual(other.state, APPLIED)

    def test_a_live_agent_holding_a_record_here_still_blocks(self) -> None:
        # The other side of the same rule: proven consumer -> refuse. This is what keeps
        # the narrower scope from becoming a fail-open.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1(home)
            blocked = run_attestation_store_migrate(
                home=home, view=_View(agents=[_Agent(_NAME), _Agent("mzb1_other_codex_default")]),
                write=True,
            )
            self.assertEqual(blocked.state, BLOCKED_CONSUMERS_LIVE)
            self.assertEqual(blocked.live_consumers, (_NAME,))  # only the proven one

    def test_rebuild_refuses_a_healthy_store_that_migrate_can_handle(self) -> None:
        # Rebuild discards rows; migrate preserves them. Offering rebuild as the easy path
        # would quietly destroy real attestations.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            before = _digest(path)
            result = run_attestation_store_rebuild(home=home, view=_NO_CONSUMERS, write=True)
            self.assertEqual(result.state, BLOCKED_MIGRATE_INSTEAD)
            self.assertEqual(_digest(path), before)

    def test_rebuild_rotates_an_unreadable_store_aside_preserving_a_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"this is not a sqlite database at all")
            before = path.read_bytes()
            result = run_attestation_store_rebuild(home=home, view=_NO_CONSUMERS, write=True)
            self.assertEqual(result.state, APPLIED)
            self.assertFalse(path.exists())
            self.assertEqual((result.backup_dir / path.name).read_bytes(), before)

    def test_rebuild_refuses_a_newer_store(self) -> None:
        # Never destroy a newer runtime's authority to make this build happy.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            conn = sqlite3.connect(str(path))
            try:
                conn.execute("PRAGMA user_version = 99")
                conn.commit()
            finally:
                conn.close()
            before = _digest(path)
            result = run_attestation_store_rebuild(home=home, view=_NO_CONSUMERS, write=True)
            self.assertFalse(result.ok)
            self.assertEqual(_digest(path), before)

    def test_migration_never_closes_or_launches_a_process(self) -> None:
        # GUARD BITE: acceptance 3 forbids process actuation. The use case must not even
        # reach a close / send / launch surface.
        import mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_attestation_store_maintenance as mod

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1(home)
            src = Path(mod.__file__).read_text(encoding="utf-8")
            for forbidden in ("session_start", "agent start", "close_pane", "send_keys"):
                self.assertNotIn(forbidden, src)
            run_attestation_store_migrate(home=home, view=_NO_CONSUMERS, write=True)


class PartialWriteAndReplayTest(unittest.TestCase):
    """Acceptance 5: partial-write failure and crash / replay."""

    #: DDL that SQLite genuinely rejects ("Cannot add a NOT NULL column with default
    #: value NULL"), so the ALTER fails inside the migration transaction exactly as a
    #: partial write would — without patching sqlite3 internals (immutable in 3.14).
    _BAD_DDL = {"replacement_action_id": "TEXT NOT NULL"}

    def test_failed_migration_rolls_back_and_keeps_the_backup(self) -> None:
        import mozyo_bridge.core.state.herdr_identity_attestation_schema as sch

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            before = _digest(path)
            with mock.patch.object(sch, "_COLUMN_MIGRATION_DDL", self._BAD_DDL):
                with self.assertRaises(sqlite3.DatabaseError):
                    sch.migrate_attestation_store(path)
            # Rolled back to the predecessor, with the pre-migration snapshot preserved.
            self.assertEqual(_digest(path), before)
            self.assertEqual(probe_store_schema(path).version, 1)
            backups = list((home / "backups").iterdir())
            self.assertEqual(len(backups), 1)
            # Content, not bytes: the snapshot is a SQLite backup-API copy (finding 1).
            self.assertEqual(_store_content(backups[0] / path.name), _store_content(path))

    def test_replay_after_a_failed_migration_succeeds(self) -> None:
        import mozyo_bridge.core.state.herdr_identity_attestation_schema as sch

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            with mock.patch.object(sch, "_COLUMN_MIGRATION_DDL", self._BAD_DDL):
                with self.assertRaises(sqlite3.DatabaseError):
                    sch.migrate_attestation_store(path)
            outcome = sch.migrate_attestation_store(path)  # replay, unpatched
            self.assertTrue(outcome.migrated)
            self.assertEqual(probe_store_schema(path).version, _V2)
            # The v1 row survived the failed attempt and the replay.
            self.assertEqual(HerdrIdentityAttestationStore(home=home).read(_NAME).locator, "wY:p2")

    def test_backup_never_overwrites_a_prior_snapshot(self) -> None:
        # A second-precision stamp can collide; a clobbered backup is an unrecoverable
        # migration (#13754 R4-F1).
        import mozyo_bridge.core.state.herdr_identity_attestation_schema as sch

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            with mock.patch.object(sch, "_backup_stamp", lambda now: "SAME"):
                first = sch.backup_attestation_store(path)
                second = sch.backup_attestation_store(path)
            self.assertNotEqual(first, second)
            self.assertTrue(first.exists() and second.exists())


class ReviewJ80000FindingsTest(unittest.TestCase):
    """The three code findings from review j#80000, each pinned by its own repro."""

    # --- F1: the migration snapshot must preserve WAL-committed rows ------------------
    def test_f1_wal_committed_rows_survive_the_migration_backup(self) -> None:
        # A `shutil.copy2` duplicates only the main DB file, so a WAL store's committed
        # pages (still in -wal) were silently absent from the recovery point: the backup
        # read version=1 rows=0 while the live store held the row. A backup that is
        # trusted and incomplete is worse than no backup.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path, conn = _seed_v1_wal(home)
            try:
                self.assertTrue(path.with_name(path.name + "-wal").exists())  # precondition
                outcome = sch.migrate_attestation_store(path)
                self.assertTrue(outcome.migrated)
                self.assertEqual(_store_content(path), (_V2, 1))
                self.assertEqual(_store_content(outcome.backup_dir / path.name), (1, 1))
            finally:
                conn.close()

    def test_f1_backup_never_mutates_the_store_it_preserves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home, rows=2)
            before = _digest(path)
            sch.backup_attestation_store(path)
            self.assertEqual(_digest(path), before)

    def test_f1_corrupt_store_snapshot_preserves_bytes_for_rebuild(self) -> None:
        # A non-SQLite file has no logical snapshot; the bytes ARE the evidence, so the
        # rebuild rail must still preserve them exactly.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = b"corrupt, not a sqlite database"
            path.write_bytes(payload)
            backup = sch.quarantine_attestation_store_artifacts(path)
            self.assertEqual((backup / path.name).read_bytes(), payload)

    # --- R2-F1 (review j#80029): the fix for F1 must not regenerate F1 ----------------
    def test_r2f1_valid_store_backup_failure_fails_closed(self) -> None:
        # The first F1 fix caught sqlite3.DatabaseError and fell back to a byte copy,
        # reasoning it meant "not a database". But that exception is raised just as
        # readily when a VALID database is busy or its I/O fails, and the type cannot
        # tell the two apart. Injecting a lock error into a valid WAL store's backup()
        # made the migration report success while writing a rows=0 recovery point — the
        # original defect, regenerated through its own fix. Corruption is decided by the
        # caller's intent from probe_store_schema, never inferred from an exception type.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path, conn = _seed_v1_wal(home)
            try:
                before = _digest(path)

                class _FailingBackup(sqlite3.Connection):
                    def backup(self, *a, **k):
                        raise sqlite3.OperationalError("simulated: database is locked")

                real_connect = sqlite3.connect

                def _patched(target, *a, **k):
                    if "mode=ro" in str(target):
                        k["factory"] = _FailingBackup
                    return real_connect(target, *a, **k)

                with mock.patch.object(sch.sqlite3, "connect", _patched):
                    with self.assertRaises(sch.HerdrIdentityAttestationSchemaError):
                        sch.migrate_attestation_store(path)
                # Fail-closed: no migration, no silent byte-copy recovery point.
                self.assertEqual(_digest(path), before)
                self.assertEqual(probe_store_schema(path).version, 1)
            finally:
                conn.close()

    def test_r2f1_backup_never_substitutes_a_byte_copy(self) -> None:
        # GUARD BITE on the split itself: the logical snapshot rail must have no byte-copy
        # escape hatch at all, whatever the error.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt, not a sqlite database")
            with self.assertRaises(StateStoreError):
                sch.backup_attestation_store(path)

    def test_r2f1_quarantine_preserves_the_whole_artifact_set(self) -> None:
        # A crashed WAL writer leaves -wal / -shm beside a corrupt main DB. Copying only
        # the main file stranded that evidence in place while rebuild removed its sibling.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main file")
            path.with_name(path.name + "-wal").write_bytes(b"WAL SIDECAR EVIDENCE")
            path.with_name(path.name + "-shm").write_bytes(b"SHM SIDECAR")
            backup = sch.quarantine_attestation_store_artifacts(path)
            self.assertEqual(
                (backup / (path.name + "-wal")).read_bytes(), b"WAL SIDECAR EVIDENCE"
            )
            self.assertTrue((backup / (path.name + "-shm")).exists())
            self.assertEqual((backup / path.name).read_bytes(), b"corrupt main file")

    def test_r2f1_rebuild_rotates_every_artifact_away(self) -> None:
        # A stranded -wal would let a later open resurrect a partial store from the
        # orphaned sidecar, so the rotation must be whole-artifact too.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main file")
            path.with_name(path.name + "-wal").write_bytes(b"WAL SIDECAR")
            result = run_attestation_store_rebuild(home=home, view=_View(agents=[]), write=True)
            self.assertEqual(result.state, APPLIED)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name(path.name + "-wal").exists())
            self.assertTrue((result.backup_dir / (path.name + "-wal")).exists())

    # --- F2: unreadable store + live agents must not fail open ------------------------
    def test_f2_unreadable_store_with_live_agents_refuses_destructive_rebuild(self) -> None:
        # The rebuild path's entire target set is unreadable stores, so folding
        # "cannot enumerate rows" into "no consumers" failed open exactly where it hurts.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt, not a sqlite database")
            before = _digest(path)
            result = run_attestation_store_rebuild(
                home=home, view=_View(agents=[_Agent(_NAME)]), write=True
            )
            self.assertEqual(result.state, BLOCKED_CONSUMERS_UNMEASURABLE)
            self.assertFalse(result.ok)
            self.assertEqual(result.live_consumers, (_NAME,))
            self.assertEqual(_digest(path), before)
            self.assertFalse((home / "backups").exists())

    def test_f2_empty_fleet_proves_no_consumers_even_for_an_unreadable_store(self) -> None:
        # The other half of the rule: nothing can consume a store when nothing is
        # running, so rebuild stays usable rather than being permanently unreachable.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt, not a sqlite database")
            result = run_attestation_store_rebuild(home=home, view=_View(agents=[]), write=True)
            self.assertEqual(result.state, APPLIED)
            self.assertFalse(path.exists())

    def test_f2_unmeasurable_store_is_distinct_from_proven_empty(self) -> None:
        from mozyo_bridge.core.state.herdr_identity_attestation import (
            HerdrIdentityAttestationStore as S,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(S(home=home).assigned_names(), frozenset())  # absent = proven empty
            path = herdr_identity_attestation_path(home)
            path.write_bytes(b"corrupt")
            self.assertIsNone(S(home=home).assigned_names())  # unreadable = unmeasurable

    # --- F3: only whole canonical capability tokens are credited ----------------------
    def test_f3_malformed_capability_tokens_are_never_credited(self) -> None:
        v1 = StoreSchemaObservation(STORE_RECOGNIZED, 1)
        malformed = (
            "mozyo_attest_capability_stores=1__2",     # empty segment
            "mozyo_attest_capability_stores=_1_2_",    # leading/trailing separator
            "mozyo_attest_capability_stores=1_2junk",  # trailing garbage
        )
        for token in malformed:
            with self.subTest(token=token):
                obs = parse_launcher_capability_output(
                    f"usage: x --assigned-name N\nmozyo_attest_capability_schema=2\n{token}\n"
                )
                self.assertIsNone(obs.advertised_store_versions)
                # Falls back to the native schema only -> a v1 store is refused.
                verdict = decide_store_compatibility(
                    obs, v1, required_schema_version=_V2, replacement_launch=False
                )
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.reason, STORE_LAUNCHER_CANNOT_WRITE)

    def test_f3_malformed_schema_token_is_unprovable(self) -> None:
        obs = parse_launcher_capability_output(
            "usage: x --assigned-name N\nmozyo_attest_capability_schema=2x\n"
        )
        self.assertIsNone(obs.advertised_schema_version)
        self.assertEqual(obs.writable_store_versions, frozenset())

    def test_f3_conflicting_advertisements_are_unprovable(self) -> None:
        # A launcher declaring two different schemas has clearly declared neither; the
        # admission must not arbitrate by picking whichever matched first.
        obs = parse_launcher_capability_output(
            "usage: x --assigned-name N\nmozyo_attest_capability_schema=2\n"
            "mozyo_attest_capability_schema=3\n"
        )
        self.assertIsNone(obs.advertised_schema_version)
        obs2 = parse_launcher_capability_output(
            "usage: x --assigned-name N\nmozyo_attest_capability_schema=2\n"
            "mozyo_attest_capability_stores=1_2\nmozyo_attest_capability_stores=2\n"
        )
        self.assertIsNone(obs2.advertised_store_versions)

    def test_f3_prefix_glued_token_is_not_credited(self) -> None:
        obs = parse_launcher_capability_output(
            "usage: x --assigned-name N\nxmozyo_attest_capability_schema=2\n"
        )
        self.assertIsNone(obs.advertised_schema_version)

    def test_f3_wellformed_token_is_still_credited(self) -> None:
        # The strict grammar must not break the honest advertisement it exists to protect.
        obs = parse_launcher_capability_output(
            "usage: x --assigned-name N\nmozyo_attest_capability_schema=2\n"
            "mozyo_attest_capability_stores=1_2\n"
        )
        self.assertEqual(obs.advertised_schema_version, 2)
        self.assertEqual(obs.advertised_store_versions, frozenset({1, 2}))


class ReviewJ80045FindingsTest(unittest.TestCase):
    """Review j#80045: the failure paths of the backup/rotation rails."""

    @staticmethod
    def _published(home: Path) -> list:
        backups = home / "backups"
        return sorted(f.name for f in backups.iterdir()) if backups.exists() else []

    # --- R3-F1: a failure must never publish a partial recovery point -----------------
    def test_r3f1_partial_logical_snapshot_is_never_published(self) -> None:
        # backups/<ts>/ is the namespace an operator trusts, so writing directly into it
        # published a partial recovery point on every failure: a snapshot that raised
        # mid-backup() left an 8192-byte DB with user_version=0 sitting there. Staging +
        # atomic publish means a reader sees the whole set or nothing.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            before = _digest(path)

            class _PartialBackup(sqlite3.Connection):
                def backup(self, dest, *a, **k):
                    dest.execute("CREATE TABLE partial_backup_marker (x INTEGER)")
                    dest.commit()
                    raise sqlite3.OperationalError("simulated: failure mid-backup")

            real_connect = sqlite3.connect

            def _patched(target, *a, **k):
                if "mode=ro" in str(target):
                    k["factory"] = _PartialBackup
                return real_connect(target, *a, **k)

            with mock.patch.object(sch.sqlite3, "connect", _patched):
                with self.assertRaises(StateStoreError):
                    sch.backup_attestation_store(path)
            self.assertEqual(self._published(home), [])
            self.assertEqual(_digest(path), before)

    def test_r3f1_partial_quarantine_is_never_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main")
            path.with_name(path.name + "-wal").write_bytes(b"WAL EVIDENCE")
            real_copy = shutil.copy2

            def _flaky(src, dst, *a, **k):
                if str(src).endswith("-wal"):
                    raise OSError("simulated: sidecar copy failed")
                return real_copy(src, dst, *a, **k)

            with mock.patch.object(sch.shutil, "copy2", _flaky):
                with self.assertRaises(StateStoreError):
                    sch.quarantine_attestation_store_artifacts(path)
            # A "whole-artifact" recovery point holding only the main file is exactly the
            # trusted-but-incomplete snapshot this component refuses to produce.
            self.assertEqual(self._published(home), [])
            self.assertTrue(path.exists())  # source artifacts untouched

    def test_r3f1_staging_lives_outside_the_published_namespace(self) -> None:
        # Even a failed cleanup must leave nothing discoverable under backups/.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            staging = sch._stage_backup(path)
            try:
                self.assertNotIn("backups", staging.parts)
                self.assertIn(sch._STAGING_DIRNAME, staging.parts)
            finally:
                sch._discard_staging(staging)

    def test_r3f1_successful_backup_is_still_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home, rows=2)
            backup = sch.backup_attestation_store(path)
            self.assertTrue(backup.exists())
            self.assertIn("backups", backup.parts)
            self.assertEqual(_store_content(backup / path.name), (1, 2))

    # --- R3-F2: rotation order makes an interruption recoverable ----------------------
    def test_r3f2_interrupted_rotation_leaves_main_and_retry_completes(self) -> None:
        # Removing the main file first made a half-done rotation indistinguishable from a
        # finished one: the retry probed STORE_ABSENT and reported already_current while
        # the orphaned -wal persisted forever. The main file is the completion sentinel,
        # so it goes last.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main")
            wal = path.with_name(path.name + "-wal")
            wal.write_bytes(b"WAL EVIDENCE")
            real_unlink = Path.unlink

            def _flaky(self_path, *a, **k):
                if self_path.name.endswith("-wal"):
                    raise OSError("simulated: unlink failed")
                return real_unlink(self_path, *a, **k)

            with mock.patch.object(Path, "unlink", _flaky):
                first = run_attestation_store_rebuild(
                    home=home, view=_View(agents=[]), write=True
                )
            self.assertFalse(first.ok)
            self.assertTrue(path.exists(), "main must survive as the completion sentinel")
            # The report must not DENY a side effect that happened. It previously said
            # "the store is left untouched" while the main file was already gone.
            self.assertIn("NOT untouched", first.detail)
            self.assertNotIn("left untouched", first.detail)
            self.assertIn("Re-run", first.detail)
            self.assertTrue(first.executed)

            retry = run_attestation_store_rebuild(home=home, view=_View(agents=[]), write=True)
            self.assertEqual(retry.state, APPLIED)  # not a false already_current
            self.assertFalse(path.exists())
            self.assertFalse(wal.exists(), "the orphan sidecar must not survive")

    def test_r3f2_sidecars_are_removed_before_the_main_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            for suffix in ("-wal", "-shm", "-journal"):
                path.with_name(path.name + suffix).write_bytes(b"y")
            order: list = []
            real_unlink = Path.unlink

            def _record(self_path, *a, **k):
                order.append(self_path.name)
                return real_unlink(self_path, *a, **k)

            with mock.patch.object(Path, "unlink", _record):
                sch.remove_attestation_store_artifacts(path)
            self.assertEqual(order[-1], path.name, "the main file must be removed LAST")

    def test_r3f2_quarantine_failure_still_reports_untouched_truthfully(self) -> None:
        # "untouched" is only true before any removal begins — that branch must keep it.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main")
            with mock.patch.object(
                sch.shutil, "copy2", mock.Mock(side_effect=OSError("no space"))
            ):
                result = run_attestation_store_rebuild(
                    home=home, view=_View(agents=[]), write=True
                )
            self.assertFalse(result.ok)
            self.assertIn("untouched", result.detail)
            self.assertTrue(path.exists())


class ReviewJ80081FindingsTest(unittest.TestCase):
    """Review j#80081 R4-F1: the staging allocator must be concurrency-safe."""

    _GENUINE = b"GENUINE-FULL-CORRUPT-STORE-CONTENT"

    def _corrupt_store(self, home: Path) -> Path:
        path = herdr_identity_attestation_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._GENUINE)
        return path

    def test_r4f1_concurrent_quarantine_publishes_only_complete_artifacts(self) -> None:
        # Deriving the staging name from a second-resolution stamp and rmtree-ing that
        # guessed path let two same-second quarantines share one staging dir: the later
        # deleted the earlier's active tree, and the earlier published the later's partial
        # bytes as a complete recovery point while returning success. A published recovery
        # point must be whole or absent — under concurrency too, not just sequentially.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self._corrupt_store(home)
            a_copied = threading.Event()
            b_done = threading.Event()
            real_copy = shutil.copy2

            def _barrier_copy(src, dst, *a, **k):
                result = real_copy(src, dst, *a, **k)
                if threading.current_thread().name == "A":
                    a_copied.set()      # A has copied main...
                    b_done.wait(5)      # ...and waits while a peer runs the same rail
                return result

            results: dict = {}

            def _run(name):
                try:
                    results[name] = sch.quarantine_attestation_store_artifacts(path)
                except Exception as exc:  # noqa: BLE001 - recorded, asserted below
                    results[name] = exc

            with mock.patch.object(sch, "_backup_stamp", lambda now: "SAME"), \
                    mock.patch.object(sch.shutil, "copy2", _barrier_copy):
                a = threading.Thread(target=_run, args=("A",), name="A")
                a.start()
                self.assertTrue(a_copied.wait(5))

                def _run_b():
                    try:
                        _run("B")
                    finally:
                        b_done.set()

                b = threading.Thread(target=_run_b, name="B")
                b.start()
                b.join(10)
                a.join(10)

            for name in ("A", "B"):
                self.assertIsInstance(results[name], Path, f"{name}: {results[name]!r}")
            published = sorted((home / "backups").iterdir())
            self.assertEqual(len(published), 2, "each operation gets its own recovery point")
            self.assertEqual(len({d.name for d in published}), 2, "no shared destination")
            for directory in published:
                self.assertEqual(
                    (directory / path.name).read_bytes(),
                    self._GENUINE,
                    "a published artifact must never hold another operation's partial bytes",
                )

    def test_r4f1_staging_is_unique_per_operation(self) -> None:
        # Unique by atomic reservation, not by a guessed name.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self._corrupt_store(home)
            with mock.patch.object(sch, "_backup_stamp", lambda now: "SAME"):
                dirs = [sch._stage_backup(path) for _ in range(5)]
            try:
                self.assertEqual(len({str(d) for d in dirs}), 5)
                for d in dirs:
                    self.assertTrue(d.is_dir())
            finally:
                for d in dirs:
                    sch._discard_staging(d)

    def test_r4f1_staging_never_deletes_a_peers_tree(self) -> None:
        # GUARD BITE: the old allocator rmtree'd the path it was about to use, which is
        # how a live peer's staging got destroyed. Reserving must touch nothing existing.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self._corrupt_store(home)
            with mock.patch.object(sch, "_backup_stamp", lambda now: "SAME"):
                peer = sch._stage_backup(path)
                (peer / "peer-in-flight.bin").write_bytes(b"PEER WORK IN PROGRESS")
                mine = sch._stage_backup(path)
            try:
                self.assertNotEqual(peer, mine)
                self.assertTrue(
                    (peer / "peer-in-flight.bin").exists(),
                    "reserving staging must not delete another operation's active tree",
                )
            finally:
                sch._discard_staging(peer)
                sch._discard_staging(mine)

    def test_r4f1_publish_never_overwrites_a_peers_recovery_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self._corrupt_store(home)
            with mock.patch.object(sch, "_backup_stamp", lambda now: "SAME"):
                first = sch.quarantine_attestation_store_artifacts(path)
                second = sch.quarantine_attestation_store_artifacts(path)
            self.assertNotEqual(first, second)
            self.assertEqual((first / path.name).read_bytes(), self._GENUINE)
            self.assertEqual((second / path.name).read_bytes(), self._GENUINE)

    def test_r4f1_logical_rail_shares_the_safe_allocator(self) -> None:
        # Both rails use _stage_backup, so the concurrency property must hold there too.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home, rows=2)
            with mock.patch.object(sch, "_backup_stamp", lambda now: "SAME"):
                first = sch.backup_attestation_store(path)
                second = sch.backup_attestation_store(path)
            self.assertNotEqual(first, second)
            self.assertEqual(_store_content(first / path.name), (1, 2))
            self.assertEqual(_store_content(second / path.name), (1, 2))


class ReviewJ80103FindingsTest(unittest.TestCase):
    """Review j#80103 R5-F1: the preserved artifact SET must be pinned, not re-observed."""

    def _corrupt_with_sidecars(self, home: Path) -> Path:
        path = herdr_identity_attestation_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"corrupt main")
        path.with_name(path.name + "-wal").write_bytes(b"WAL EVIDENCE")
        path.with_name(path.name + "-shm").write_bytes(b"SHM EVIDENCE")
        return path

    def test_r5f1_concurrent_rebuild_never_publishes_a_main_only_recovery_point(self) -> None:
        # Evaluating each sidecar's exists() just before copying it put a TOCTOU between
        # CHOOSING what to preserve and preserving it: a peer rotating the store in that
        # window made this operation skip sidecars that were present at its start and
        # publish a main-only directory as a complete recovery point with state=applied.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self._corrupt_with_sidecars(home)
            b_copied_main = threading.Event()
            a_done = threading.Event()
            real_copy = shutil.copy2

            def _barrier_copy(src, dst, *a, **k):
                result = real_copy(src, dst, *a, **k)
                if threading.current_thread().name == "B" and str(src).endswith(".sqlite"):
                    b_copied_main.set()
                    a_done.wait(5)
                return result

            results: dict = {}

            def _run(name):
                try:
                    results[name] = run_attestation_store_rebuild(
                        home=home, view=_View(agents=[]), write=True
                    )
                except Exception as exc:  # noqa: BLE001 - asserted below
                    results[name] = exc

            with mock.patch.object(sch.shutil, "copy2", _barrier_copy):
                b = threading.Thread(target=_run, args=("B",), name="B")
                b.start()
                self.assertTrue(b_copied_main.wait(5))
                a = threading.Thread(target=_run, args=("A",), name="A")
                a.start()
                a.join(10)
                a_done.set()
                b.join(10)

            published = sorted((home / "backups").iterdir())
            for directory in published:
                names = {f.name for f in directory.iterdir()}
                self.assertEqual(
                    names,
                    {path.name, path.name + "-wal", path.name + "-shm"},
                    "a published recovery point must hold every artifact observed at its "
                    "own start — never a main-only remainder",
                )
            # The racer must fail closed rather than publish a partial set.
            states = {n: getattr(results[n], "state", results[n]) for n in ("A", "B")}
            self.assertIn(APPLIED, states.values())
            self.assertEqual(len(published), sum(1 for s in states.values() if s == APPLIED))

    def test_r5f1_vanished_artifact_fails_closed_and_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self._corrupt_with_sidecars(home)
            real_copy = shutil.copy2

            def _peer_rotated(src, dst, *a, **k):
                if str(src).endswith("-wal"):
                    raise FileNotFoundError(2, "No such file or directory", str(src))
                return real_copy(src, dst, *a, **k)

            with mock.patch.object(sch.shutil, "copy2", _peer_rotated):
                with self.assertRaises(StateStoreError) as ctx:
                    sch.quarantine_attestation_store_artifacts(path)
            self.assertIn("observed at its start", str(ctx.exception))
            self.assertFalse((home / "backups").exists(), "nothing may be published")
            self.assertTrue(path.exists(), "and nothing may be removed")

    def test_r5f1_manifest_is_pinned_before_the_first_copy(self) -> None:
        # GUARD BITE: a sidecar created AFTER the manifest is pinned is not part of this
        # operation's promise, and its absence from the recovery point is correct. What
        # matters is that the pinned set is preserved whole.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main")
            wal = path.with_name(path.name + "-wal")
            wal.write_bytes(b"WAL EVIDENCE")
            backup = sch.quarantine_attestation_store_artifacts(path)
            self.assertEqual(
                {f.name for f in backup.iterdir()}, {path.name, wal.name}
            )

    # --- self-found axis: the removal loop's own exists()->unlink() window -------------
    def test_removal_is_idempotent_when_a_peer_already_removed_an_artifact(self) -> None:
        # An already-absent artifact IS this function's goal state. Guarding with exists()
        # left a window where a peer's unlink between the check and ours turned a fully
        # achieved goal state into a false "interrupted / NOT untouched / re-run" report —
        # a side effect denied in the opposite direction.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main")
            path.with_name(path.name + "-wal").write_bytes(b"WAL")
            real_unlink = Path.unlink

            def _peer_wins_window(self_path, *a, **k):
                if self_path.name.endswith("-wal") and not k.get("missing_ok"):
                    raise FileNotFoundError(2, "No such file", str(self_path))
                return real_unlink(self_path, *a, **k)

            with mock.patch.object(Path, "unlink", _peer_wins_window):
                result = run_attestation_store_rebuild(
                    home=home, view=_View(agents=[]), write=True
                )
            self.assertEqual(result.state, APPLIED)
            self.assertTrue(result.ok)

    def test_removal_reaches_goal_state_when_artifacts_are_already_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Nothing exists: removal must be a silent no-op, not an error.
            sch.remove_attestation_store_artifacts(path)
            self.assertFalse(path.exists())


class ReviewJ80129FindingsTest(unittest.TestCase):
    """Review j#80129 R6-F1: never claim a backup-first rotation that did not happen."""

    def _rebuild(self, home: Path):
        return run_attestation_store_rebuild(home=home, view=_View(agents=[]), write=True)

    def _vanishing_quarantine(self):
        """The real quarantine, with the store deleted just before it runs."""
        real = sch.quarantine_attestation_store_artifacts

        def _vanish(path):
            if path.exists():
                path.unlink()
            return real(path)   # genuinely returns None: there is nothing left to preserve

        return _vanish

    def test_r6f1_external_disappearance_fails_closed_without_fabricating_a_backup(self) -> None:
        # `quarantine(...) -> None` after a non-absent probe does NOT mean "nothing to
        # preserve"; it means backup-first could not be PROVEN. Flowing to APPLIED reported
        # `rotated ... into backups/` while no backup directory existed at all — fabricated
        # recovery evidence, the mirror of the denial R3-F2 closed.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main")
            with mock.patch.object(
                mnt, "quarantine_attestation_store_artifacts", self._vanishing_quarantine()
            ):
                result = self._rebuild(home)
            self.assertEqual(result.state, BLOCKED_FAILED)
            self.assertFalse(result.ok)
            self.assertFalse(result.executed)
            self.assertIsNone(result.backup_dir)
            self.assertFalse((home / "backups").exists(), "no recovery point exists")
            self.assertNotIn(
                "rotated the unsupported store into backups/",
                result.detail,
                "must never claim a rotation it did not perform",
            )
            self.assertIn("backup-first cannot be proven", result.detail)

    def test_r6f1_retry_after_disappearance_converges_to_already_current(self) -> None:
        # Fail-closed must not mean stuck: the retry's probe sees STORE_ABSENT.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main")
            with mock.patch.object(
                mnt, "quarantine_attestation_store_artifacts", self._vanishing_quarantine()
            ):
                self._rebuild(home)
            retry = self._rebuild(home)
            self.assertEqual(retry.state, ALREADY_CURRENT)
            self.assertTrue(retry.ok)

    def test_r6f1_public_peer_completion_race_leaves_one_true_recovery_point(self) -> None:
        # The same code path is reached when a public peer legitimately completes first.
        # The loser must not claim a rotation either; the winner's recovery point stands.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main")
            path.with_name(path.name + "-wal").write_bytes(b"WAL")
            real = sch.quarantine_attestation_store_artifacts
            peer_ran = []

            def _peer_completes_first(p):
                # A peer completes the real preserve+rotate exactly once, right in our
                # probe->quarantine window (using the real primitives, not a nested
                # rebuild, which would re-enter this patch).
                if not peer_ran:
                    peer_ran.append(real(p))
                    sch.remove_attestation_store_artifacts(p)
                return real(p)                      # our quarantine now finds nothing

            with mock.patch.object(
                mnt, "quarantine_attestation_store_artifacts", _peer_completes_first
            ):
                loser = self._rebuild(home)
            self.assertTrue(peer_ran and peer_ran[0] is not None)

            self.assertEqual(loser.state, BLOCKED_FAILED)
            self.assertFalse(loser.executed)
            published = sorted((home / "backups").iterdir())
            self.assertEqual(len(published), 1, "exactly the winner's recovery point")
            # The set includes `-shm`: opening a store beside a `-wal` makes SQLite itself
            # materialize the index even under mode=ro, so by quarantine time it is a real
            # artifact and preserving it is correct (see probe_store_schema's caveat).
            self.assertEqual(
                {f.name for f in published[0].iterdir()},
                {path.name, path.name + "-wal", path.name + "-shm"},
                "and it is whole",
            )

    def test_probe_never_creates_a_store_but_sqlite_may_materialize_shm(self) -> None:
        # Honest pinning of the probe's real footprint (self-found while fixing R6-F1).
        # The docstring used to claim it "creates nothing"; opening a store beside a -wal
        # makes SQLite materialize the -shm index even under mode=ro. No store, row, shape
        # or version is created -- and immutable=1 would suppress it only by ignoring the
        # -wal, which would resurrect the WAL blindness of F1.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            # No store at all -> the probe must create nothing whatsoever.
            self.assertEqual(probe_store_schema(path).state, STORE_ABSENT)
            self.assertFalse(path.exists())
            self.assertEqual(sorted(home.iterdir()), [])
            # Store + -wal -> SQLite's own -shm may appear; the store itself is unchanged.
            path.write_bytes(b"corrupt main")
            path.with_name(path.name + "-wal").write_bytes(b"WAL")
            before = _digest(path)
            probe_store_schema(path)
            self.assertEqual(_digest(path), before, "the store itself must be untouched")

    def test_r6f1_applied_always_carries_a_real_backup_dir(self) -> None:
        # GUARD BITE on the invariant itself: APPLIED implies a recovery point on disk.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = herdr_identity_attestation_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"corrupt main")
            result = self._rebuild(home)
            self.assertEqual(result.state, APPLIED)
            self.assertIsNotNone(result.backup_dir)
            self.assertTrue(result.backup_dir.exists())
            self.assertTrue(any(result.backup_dir.iterdir()))


class ZeroSideEffectTest(unittest.TestCase):
    """Acceptance 1: an incompatible store creates no workspace / tab / agent."""

    def test_preflight_raises_typed_blocker_before_any_actuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1(home)
            with self.assertRaises(HerdrLauncherIncompatibleError) as ctx:
                preflight_attest_store_schema(
                    _OLD_LAUNCHER, store_home=home, replacement_launch=False
                )
            self.assertEqual(ctx.exception.reason, STORE_LAUNCHER_CANNOT_WRITE)
            self.assertIn("No workspace / tab / agent was created", str(ctx.exception))

    def test_preflight_admits_a_normal_launch_onto_v1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1(home)
            self.assertIsNone(
                preflight_attest_store_schema(
                    _NEW_LAUNCHER, store_home=home, replacement_launch=False
                )
            )

    def test_preflight_refuses_a_replacement_launch_onto_v1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _seed_v1(home)
            with self.assertRaises(HerdrLauncherIncompatibleError) as ctx:
                preflight_attest_store_schema(
                    _NEW_LAUNCHER, store_home=home, replacement_launch=True
                )
            self.assertEqual(ctx.exception.reason, STORE_REPLACEMENT_UNSUPPORTED)

    def test_preflight_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = _seed_v1(home)
            before = _digest(path)
            with self.assertRaises(HerdrLauncherIncompatibleError):
                preflight_attest_store_schema(
                    _NEW_LAUNCHER, store_home=home, replacement_launch=True
                )
            self.assertEqual(_digest(path), before)

    def test_session_start_runs_the_store_preflight_before_any_herdr_write(self) -> None:
        # GUARD BITE: the join is only worth anything if the launch path actually calls
        # it. A future refactor that drops the call would otherwise pass every test above.
        import inspect

        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start,
        )

        src = inspect.getsource(herdr_session_start.prepare_session)
        self.assertIn("preflight_attest_store_schema", src)
        self.assertLess(
            src.index("preflight_attest_store_schema"),
            src.index("_create_workspace"),
            "the store preflight must precede the first herdr workspace write",
        )


if __name__ == "__main__":
    unittest.main()
