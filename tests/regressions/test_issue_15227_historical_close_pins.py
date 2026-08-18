"""j#103467: historical destructive replay requires private current-generation pins."""

from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.lane_lifecycle_model import ReleasePin
from mozyo_bridge.core.state.lane_release_observation import (
    ReleaseObservation,
    ReleaseObservationError,
    build_release_observation,
    decode_release_observation,
    encode_release_observation,
)
from mozyo_bridge.core.state.lane_reconcile_close_pin import (
    ReconcileClosePinError,
    decode_reconcile_close_pin,
)
from mozyo_bridge.core.state.lane_release_pin import (
    ReleasePinError,
    decode_release_pin_projection,
    encode_release_pins,
)
from mozyo_bridge.core.state.scratch_retirement_fence import (
    RetirementUnit,
    ScratchRetirementFence,
    ScratchRetirementFenceError,
    _META_TABLE_SQL,
    _TABLE_SQL,
)
from mozyo_bridge.core.state.scratch_retirement_pin import (
    ScratchRetirementPin,
    ScratchRetirementPinError,
    decode_scratch_retirement_pin_projection,
    encode_scratch_retirement_pins,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_destructive_close_identity import (  # noqa: E501
    pinned_generation_partition,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import encode_assigned_name  # noqa: E501
from tests.support.current_launch_authority import seed_completed_current_generation


class ScratchPinCodecTest(unittest.TestCase):
    def test_v2_is_closed_canonical_and_nonsecret(self):
        pin = ScratchRetirementPin("codex", "mzb1_ws_codex_lane", "w:p1", "startup-a")
        encoded = encode_scratch_retirement_pins((pin,))
        projection = decode_scratch_retirement_pin_projection(encoded)
        self.assertEqual(projection.version, 2)
        self.assertEqual(projection.pins, (pin,))
        self.assertNotIn("terminal", encoded)
        for bad in (
            '{"v":true,"pins":[]}',
            '{"v":2.0,"pins":[]}',
            '{"v":2,"pins":[],"extra":0}',
            '{"v":2,"pins":[{"role":" codex","assigned_name":"n",'
            '"locator":"l","startup_action_id":"a"}]}',
        ):
            with self.subTest(bad=bad), self.assertRaises(ScratchRetirementPinError):
                decode_scratch_retirement_pin_projection(bad)

    def test_release_v2_requires_each_public_axis_globally_unique(self):
        baseline = (
            ReleasePin("codex", "name-a", "w:p1", "startup-a"),
            ReleasePin("claude", "name-b", "w:p2", "startup-a"),
        )
        self.assertEqual(
            decode_release_pin_projection(encode_release_pins(baseline)).pins,
            tuple(sorted(baseline, key=lambda pin: pin.role)),
        )
        for pins in (
            (baseline[0], ReleasePin("codex", "name-b", "w:p2", "startup-b")),
            (baseline[0], ReleasePin("claude", "name-a", "w:p2", "startup-b")),
            (baseline[0], ReleasePin("claude", "name-b", "w:p1", "startup-b")),
        ):
            with self.subTest(pins=pins), self.assertRaises(ReleasePinError):
                encode_release_pins(pins)
            with self.subTest(observation=pins), self.assertRaises(ValueError):
                build_release_observation(pins)

    def test_legacy_bytes_keep_provenance_but_never_gain_a_token(self):
        projection = decode_scratch_retirement_pin_projection("codex\tw:p1")
        self.assertEqual(projection.version, 1)
        self.assertFalse(projection.current_authority)
        self.assertEqual(projection.pins, ())
        self.assertEqual(projection.legacy_pairs, (("codex", "w:p1"),))

    def test_release_pin_absence_is_only_the_exact_empty_legacy_bytes(self):
        self.assertEqual(decode_release_pin_projection("").version, 0)
        for raw in (" ", "\n", "\t"):
            with self.subTest(raw=raw), self.assertRaises(ReleasePinError):
                decode_release_pin_projection(raw)

    def test_release_observation_writer_rejects_bool_and_float_versions(self):
        for version in (True, 2.0):
            with self.subTest(version=version), self.assertRaises(
                ReleaseObservationError
            ):
                encode_release_observation(ReleaseObservation(version=version))

    def test_current_close_pin_decoders_require_exact_closed_envelopes(self):
        cases = (
            (
                decode_release_pin_projection,
                ReleasePinError,
                (
                    '{"v":true,"pins":[]}',
                    '{"v":2.0,"pins":[]}',
                    '{"v":2,"pins":[],"extra":0}',
                    '{"v":2}',
                ),
            ),
            (
                decode_release_observation,
                ReleaseObservationError,
                (
                    '{"v":true,"slots":[]}',
                    '{"v":2.0,"slots":[]}',
                    '{"v":2,"slots":[],"extra":0}',
                    '{"v":2}',
                ),
            ),
            (
                decode_reconcile_close_pin,
                ReconcileClosePinError,
                (
                    '{"v":true,"slots":[]}',
                    '{"v":1.0,"slots":[]}',
                    '{"v":1,"slots":[],"extra":0}',
                    '{"v":1}',
                ),
            ),
        )
        for decoder, error, payloads in cases:
            for payload in payloads:
                with self.subTest(decoder=decoder.__name__, payload=payload):
                    with self.assertRaises(error):
                        decoder(payload)


class ScratchMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.fence = ScratchRetirementFence(home=self.home)

    def _v1(self, *, exact_index=True):
        conn = sqlite3.connect(self.fence.path)
        if exact_index:
            conn.execute(_TABLE_SQL)
        else:
            conn.execute(
                "CREATE TABLE scratch_retirement (workspace_id TEXT NOT NULL,lane_id TEXT "
                "NOT NULL,slot_digest TEXT NOT NULL,attempt_id TEXT NOT NULL,revision INTEGER "
                "NOT NULL,state TEXT NOT NULL,pinned_json TEXT NOT NULL DEFAULT '',closed_json "
                "TEXT NOT NULL DEFAULT '',detail TEXT NOT NULL DEFAULT '',reserved_at TEXT NOT "
                "NULL,updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX not_unique ON scratch_retirement"
                "(workspace_id,lane_id,slot_digest)"
            )
        conn.execute(_META_TABLE_SQL)
        conn.execute("INSERT INTO store_meta VALUES ('store_nonce','nonce')")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        self.fence.seal_path.write_text("nonce", encoding="utf-8")
        os.chmod(self.fence.path, 0o644)
        os.chmod(self.fence.seal_path, 0o644)

    def test_normal_transaction_never_implicitly_migrates_v1(self):
        self._v1()
        unit = RetirementUnit("ws", "lane", "digest")
        with self.assertRaises(ScratchRetirementFenceError):
            with self.fence.transaction(unit, live_pair_present=True) as txn:
                txn.reserve(pinned=())
        with sqlite3.connect(self.fence.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertFalse(self.fence.path.with_name(self.fence.path.name + ".v1.backup").exists())

    def test_explicit_backup_first_migration_keeps_mode_and_replays_partial_publish(self):
        self._v1()
        module = (
            "mozyo_bridge.core.state.scratch_retirement_migration._publish_pinned_link"
        )
        from mozyo_bridge.core.state import scratch_retirement_migration as migration
        real_publish = migration._publish_pinned_link
        calls = 0
        def fail_second(source, pin, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("crash between backup pair publishes")
            return real_publish(source, pin, target)
        with patch(module, side_effect=fail_second):
            with self.assertRaises(ScratchRetirementFenceError):
                self.fence.migrate_v1_backup_first()
        self.assertEqual(self.fence.migrate_v1_backup_first(), 2)
        # #15653: the migration must not chmod the existing primary DB or seal — the
        # legacy 0644 mode survives the migration untouched.
        self.assertEqual(self.fence.path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.fence.seal_path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.fence.store_shape().state, "present")
        unit = RetirementUnit("ws", "lane", "digest")
        with self.fence.transaction(unit, live_pair_present=True) as txn:
            self.assertIsNone(txn.current())

    def test_mount_portable_path_fallback_replays_after_partial_publish(self):
        import errno

        self._v1()
        from mozyo_bridge.core.state import scratch_retirement_migration as migration

        real_link = migration.os.link
        private_publish_calls = 0
        pseudo_fd_calls = 0
        fail_second_private_publish = True

        class UnprivilegedLinkAt:
            def __call__(self, *_args):
                migration.ctypes.set_errno(errno.EPERM)
                return -1

        def mount_portable_link(source, target, *args, **kwargs):
            nonlocal private_publish_calls, pseudo_fd_calls
            source_path = Path(source)
            if source_path.parent == Path("/proc/self/fd"):
                pseudo_fd_calls += 1
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), source_path)
            if source_path.name in {"store.sqlite3", "store.seal"}:
                private_publish_calls += 1
                if fail_second_private_publish and private_publish_calls == 2:
                    raise OSError("crash after first private-path publish")
            return real_link(source, target, *args, **kwargs)

        fake_libc = SimpleNamespace(linkat=UnprivilegedLinkAt())
        real_is_dir = migration.Path.is_dir

        def fd_root_is_dir(path):
            if path == Path("/proc/self/fd"):
                return True
            if path == Path("/dev/fd"):
                return False
            return real_is_dir(path)

        patches = (
            patch.object(migration.ctypes, "CDLL", return_value=fake_libc),
            patch.object(migration.sys, "platform", "linux"),
            patch.object(migration.Path, "is_dir", fd_root_is_dir),
            patch.object(migration.os, "link", side_effect=mount_portable_link),
        )
        prior_errno = migration.ctypes.get_errno()
        try:
            with patches[0], patches[1], patches[2], patches[3]:
                with self.assertRaises(ScratchRetirementFenceError):
                    self.fence.migrate_v1_backup_first()

                backup = self.fence.path.with_name(
                    self.fence.path.name + ".v1.backup"
                )
                backup_seal = backup.with_name(backup.name + ".seal")
                control = backup.with_name(backup.name + ".migration")
                self.assertTrue(backup.exists())
                self.assertFalse(backup_seal.exists())
                self.assertTrue(control.exists())

                fail_second_private_publish = False
                self.assertEqual(self.fence.migrate_v1_backup_first(), 2)
        finally:
            migration.ctypes.set_errno(prior_errno)

        authority = json.loads(control.read_text(encoding="utf-8"))
        staging_root = control.parent / authority["staging_name"]
        marker = staging_root / "authority.json"
        self.assertEqual(
            (marker.stat().st_dev, marker.stat().st_ino),
            (control.stat().st_dev, control.stat().st_ino),
        )
        for name, final in (
            ("store.sqlite3", backup),
            ("store.seal", backup_seal),
        ):
            expected_pin = tuple(authority["artifact_pins"][name])
            staging = staging_root / name
            self.assertEqual(
                (staging.stat().st_dev, staging.stat().st_ino), expected_pin
            )
            self.assertEqual((final.stat().st_dev, final.stat().st_ino), expected_pin)
        self.assertEqual(private_publish_calls, 3)
        self.assertEqual(pseudo_fd_calls, 3)
        self.assertEqual(self.fence.store_shape().state, "present")
        with sqlite3.connect(self.fence.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_partial_private_staging_seal_is_rebuilt(self):
        self._v1()
        module = "mozyo_bridge.core.state.scratch_retirement_migration._write_seal"
        def partial(path, _nonce, _pin):
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
            os.write(fd, b"non")
            os.fsync(fd)
            os.close(fd)
            raise OSError("kill during seal write")
        with patch(module, side_effect=partial):
            with self.assertRaises(ScratchRetirementFenceError):
                self.fence.migrate_v1_backup_first()
        self.assertEqual(self.fence.migrate_v1_backup_first(), 2)

    def test_pinned_publish_falls_back_without_fd_pseudo_filesystem(self):
        self._v1()
        from mozyo_bridge.core.state import scratch_retirement_migration as migration

        with patch.object(migration, "_link_open_inode", return_value=False):
            self.assertEqual(self.fence.migrate_v1_backup_first(), 2)
        self.assertEqual(self.fence.store_shape().state, "present")

    def test_open_inode_link_falls_back_only_for_capability_errors(self):
        import errno

        from mozyo_bridge.core.state import scratch_retirement_migration as migration

        source = self.home / "open-inode-source"
        target = self.home / "open-inode-target"
        source.write_bytes(b"source")
        fd = os.open(source, os.O_RDONLY)

        class FakeLinkAt:
            def __init__(self, error):
                self.error = error

            def __call__(self, *_args):
                migration.ctypes.set_errno(self.error)
                return -1

        try:
            for error in (errno.EPERM, errno.EXDEV):
                fake = SimpleNamespace(linkat=FakeLinkAt(error))
                with self.subTest(error=error), patch.object(
                    migration.ctypes, "CDLL", return_value=fake
                ), patch.object(
                    migration.sys, "platform", "linux"
                ), patch.object(migration.Path, "is_dir", return_value=False):
                    self.assertFalse(migration._link_open_inode(fd, target))

            for error in (errno.EEXIST, errno.EIO):
                fake = SimpleNamespace(linkat=FakeLinkAt(error))
                with self.subTest(error=error), patch.object(
                    migration.ctypes, "CDLL", return_value=fake
                ), patch.object(
                    migration.sys, "platform", "linux"
                ), patch.object(migration.Path, "is_dir", return_value=False):
                    with self.assertRaises(OSError) as raised:
                        migration._link_open_inode(fd, target)
                    self.assertEqual(raised.exception.errno, error)
        finally:
            os.close(fd)

    def test_path_fallback_never_stamps_a_replaced_staging_inode(self):
        self._v1()
        from mozyo_bridge.core.state import scratch_retirement_migration as migration

        real_link = migration.os.link
        captured = {}
        backup = self.fence.path.with_name(self.fence.path.name + ".v1.backup")
        control = backup.with_name(backup.name + ".migration")

        def replace_during_link(source, target, *args, **kwargs):
            source_path = Path(source)
            if source_path.name == "store.sqlite3":
                foreign = source_path.with_name("foreign-during-publish")
                foreign.write_bytes(b"same-user-foreign-during-publish")
                os.chmod(foreign, 0o600)
                os.replace(foreign, source_path)
                captured["source"] = source_path
            return real_link(source, target, *args, **kwargs)

        with patch.object(
            migration, "_link_open_inode", return_value=False
        ), patch.object(migration.os, "link", side_effect=replace_during_link):
            with self.assertRaises(ScratchRetirementFenceError):
                self.fence.migrate_v1_backup_first()
        self.assertEqual(
            captured["source"].read_bytes(), b"same-user-foreign-during-publish"
        )
        self.assertEqual(backup.read_bytes(), b"same-user-foreign-during-publish")
        self.assertEqual(
            (backup.stat().st_dev, backup.stat().st_ino),
            (captured["source"].stat().st_dev, captured["source"].stat().st_ino),
        )
        self.assertTrue(control.exists())
        self.assertEqual(self.fence.store_shape().state, "damaged")
        with self.assertRaises(ScratchRetirementFenceError):
            self.fence.migrate_v1_backup_first()
        self.assertEqual(backup.read_bytes(), b"same-user-foreign-during-publish")
        with sqlite3.connect(self.fence.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_db_only_backup_with_partial_staging_seal_replays(self):
        self._v1()
        backup = self.fence.path.with_name(self.fence.path.name + ".v1.backup")
        backup_seal = backup.with_name(backup.name + ".seal")
        module = (
            "mozyo_bridge.core.state.scratch_retirement_migration._publish_pinned_link"
        )
        from mozyo_bridge.core.state import scratch_retirement_migration as migration
        real_publish = migration._publish_pinned_link
        calls = 0
        def fail_second(source, pin, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("kill after database publish")
            return real_publish(source, pin, target)
        with patch(module, side_effect=fail_second):
            with self.assertRaises(ScratchRetirementFenceError):
                self.fence.migrate_v1_backup_first()
        control = backup.with_name(backup.name + ".migration")
        authority = json.loads(control.read_text(encoding="utf-8"))
        staging_seal = control.parent / authority["staging_name"] / "store.seal"
        staging_seal.write_bytes(b"n")
        self.assertEqual(self.fence.migrate_v1_backup_first(), 2)
        self.assertTrue(backup_seal.exists())

    def test_nonunique_lookalike_v1_is_byte_zero_refused(self):
        self._v1(exact_index=False)
        before = self.fence.path.read_bytes()
        with self.assertRaises(ScratchRetirementFenceError):
            self.fence.migrate_v1_backup_first()
        self.assertEqual(self.fence.path.read_bytes(), before)

    def test_trigger_bearing_v1_is_byte_zero_refused(self):
        self._v1()
        with sqlite3.connect(self.fence.path) as conn:
            conn.execute(
                "CREATE TRIGGER erase_attempt AFTER INSERT ON scratch_retirement "
                "BEGIN DELETE FROM scratch_retirement; END"
            )
        before = self.fence.path.read_bytes()
        with self.assertRaises(ScratchRetirementFenceError):
            self.fence.migrate_v1_backup_first()
        self.assertEqual(self.fence.path.read_bytes(), before)

    def test_noncanonical_v1_row_codecs_are_byte_zero_refused(self):
        cases = (
            ("codex\tw:p1\n", "", ""),
            ("codex\tw:p1", "\n", ""),
            ("codex\tw:p1", "", sqlite3.Binary(b"blob-detail")),
        )
        for pinned, closed, detail in cases:
            with self.subTest(pinned=pinned, closed=closed, detail=detail):
                case_temp = tempfile.TemporaryDirectory()
                self.addCleanup(case_temp.cleanup)
                original = self.fence
                self.fence = ScratchRetirementFence(home=Path(case_temp.name))
                try:
                    self._v1()
                    with sqlite3.connect(self.fence.path) as conn:
                        conn.execute(
                            "INSERT INTO scratch_retirement VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            ("ws", "lane", "digest", "attempt", 1, "pending", pinned,
                             closed, detail, "2026-08-11T00:00:00Z",
                             "2026-08-11T00:00:00Z"),
                        )
                    before = self.fence.path.read_bytes()
                    with self.assertRaises(ScratchRetirementFenceError):
                        self.fence.migrate_v1_backup_first()
                    self.assertEqual(self.fence.path.read_bytes(), before)
                    self.assertFalse(self.fence.path.with_name(
                        self.fence.path.name + ".v1.backup"
                    ).exists())
                finally:
                    self.fence = original

    def test_backup_evidence_prevents_lost_primary_from_becoming_absent(self):
        self._v1()
        self.assertEqual(self.fence.migrate_v1_backup_first(), 2)
        self.fence.path.unlink()
        self.fence.seal_path.unlink()
        self.assertEqual(self.fence.store_shape().state, "damaged")
        unit = RetirementUnit("ws", "lane", "digest")
        with self.assertRaises(ScratchRetirementFenceError):
            self.fence.peek(unit)
        with self.assertRaises(ScratchRetirementFenceError):
            with self.fence.transaction(unit, live_pair_present=True):
                pass
        self.assertFalse(self.fence.path.exists())

    def test_retained_backup_symlink_drift_makes_primary_recovery_required(self):
        self._v1()
        self.assertEqual(self.fence.migrate_v1_backup_first(), 2)
        backup = self.fence.path.with_name(self.fence.path.name + ".v1.backup")
        # #15653: a mode change alone is not drift — the backup stays evidence.
        os.chmod(backup, 0o640)
        self.assertEqual(self.fence.store_shape().state, "present")

        moved = backup.with_name(backup.name + ".foreign")
        backup.rename(moved)
        backup.symlink_to(moved)
        self.assertEqual(self.fence.store_shape().state, "damaged")

    def test_retained_backup_version_and_seal_drift_are_not_present(self):
        self._v1()
        self.assertEqual(self.fence.migrate_v1_backup_first(), 2)
        backup = self.fence.path.with_name(self.fence.path.name + ".v1.backup")
        with sqlite3.connect(backup) as conn:
            conn.execute("PRAGMA user_version=2")
        self.assertEqual(self.fence.store_shape().state, "damaged")

        # Restore a fresh fixture to isolate seal drift from the version mutation.
        second_temp = tempfile.TemporaryDirectory()
        self.addCleanup(second_temp.cleanup)
        other = Path(second_temp.name)
        second = ScratchRetirementFence(home=other)
        original = self.fence
        self.fence = second
        try:
            self._v1()
            self.assertEqual(second.migrate_v1_backup_first(), 2)
        finally:
            self.fence = original
        second_backup = second.path.with_name(second.path.name + ".v1.backup")
        second_backup.with_name(second_backup.name + ".seal").write_text(
            "different-nonce", encoding="utf-8"
        )
        self.assertEqual(second.store_shape().state, "damaged")

    def test_same_uid_staging_replacement_is_left_untouched_and_v1_is_not_stamped(self):
        self._v1()
        from mozyo_bridge.core.state import scratch_retirement_migration as migration

        real = migration._staging_root
        captured = {}
        def replace_after_control(*args):
            root, pins = real(*args)
            target = root / "store.sqlite3"
            foreign = root / "foreign"
            foreign.write_bytes(b"same-user-foreign-bytes")
            os.chmod(foreign, 0o600)
            os.replace(foreign, target)
            captured["target"] = target
            return root, pins

        with patch.object(migration, "_staging_root", side_effect=replace_after_control):
            with self.assertRaises(ScratchRetirementFenceError):
                self.fence.migrate_v1_backup_first()
        self.assertEqual(captured["target"].read_bytes(), b"same-user-foreign-bytes")
        with sqlite3.connect(self.fence.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_post_copy_path_replacement_is_never_chmoded_or_published(self):
        self._v1()
        from mozyo_bridge.core.state import scratch_retirement_migration as migration

        real_copy = migration._copy_sqlite
        captured = {}

        def replace_after_copy(source, staging, pin):
            real_copy(source, staging, pin)
            foreign = staging.with_name("foreign-after-copy")
            foreign.write_bytes(b"same-user-foreign-after-copy")
            os.chmod(foreign, 0o640)
            os.replace(foreign, staging)
            captured["target"] = staging

        with patch.object(migration, "_copy_sqlite", side_effect=replace_after_copy):
            with self.assertRaises(ScratchRetirementFenceError):
                self.fence.migrate_v1_backup_first()
        target = captured["target"]
        self.assertEqual(target.read_bytes(), b"same-user-foreign-after-copy")
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        with sqlite3.connect(self.fence.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_control_publish_failure_retains_unproven_root_without_touching_source(self):
        self._v1()
        from mozyo_bridge.core.state import scratch_retirement_migration as migration

        real_link = migration.os.link

        def fail_control_link(source, target, *args, **kwargs):
            if Path(target).name.endswith(".migration"):
                raise OSError("control publish failed")
            return real_link(source, target, *args, **kwargs)

        with patch.object(migration.os, "link", side_effect=fail_control_link):
            with self.assertRaises(ScratchRetirementFenceError):
                self.fence.migrate_v1_backup_first()
        backup = self.fence.path.with_name(self.fence.path.name + ".v1.backup")
        self.assertFalse(backup.exists())
        self.assertFalse(backup.with_name(backup.name + ".migration").exists())
        roots = tuple(self.home.glob(f".{backup.name}.staging-*"))
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.fence.store_shape().state, "damaged")
        with sqlite3.connect(self.fence.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)


class HistoricalPartitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ws, self.lane, self.role = "ws", "lane", "codex"
        self.name = encode_assigned_name(self.ws, self.role, self.lane)
        self.locator, self.terminal = "w:p1", "terminal-one"
        self.action = seed_completed_current_generation(
            self.home,
            workspace_id=self.ws,
            lane_id=self.lane,
            role=self.role,
            assigned_name=self.name,
            locator=self.locator,
            terminal_id=self.terminal,
        )
        HerdrIdentityAttestationStore(home=self.home).upsert(
            IdentityAttestationRecord(
                assigned_name=self.name,
                workspace_id=self.ws,
                role=self.role,
                lane_id=self.lane,
                locator=self.locator,
                terminal_id=self.terminal,
                verdict="present",
                observed_at="2026-08-11T00:00:00+00:00",
            )
        )
        self.pin = ReleasePin(
            self.role, self.name, self.locator, self.action
        )

    def _row(self, name=None, locator=None, terminal=None):
        return {
            "name": name or self.name,
            "pane_id": locator or self.locator,
            "terminal_id": terminal or self.terminal,
        }

    def test_exact_live_and_completed_positive_absence(self):
        live = pinned_generation_partition(
            (self.pin,), (self._row(),), home=self.home,
            workspace_id=self.ws, lane_id=self.lane)
        self.assertEqual(live, ((self.pin,), ()))
        absent = pinned_generation_partition(
            (self.pin,), (), home=self.home,
            workspace_id=self.ws, lane_id=self.lane)
        self.assertEqual(absent, ((), (self.pin,)))

    def test_terminal_reclaimed_at_another_name_is_not_absence(self):
        moved = self._row(name=encode_assigned_name("other", self.role, "elsewhere"),
                          locator="w:p9")
        self.assertIsNone(pinned_generation_partition(
            (self.pin,), (moved,), home=self.home,
            workspace_id=self.ws, lane_id=self.lane))


class HistoricalRollbackAbsenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ws, self.lane, self.role = "ws", "lane", "codex"
        self.name = encode_assigned_name(self.ws, self.role, self.lane)
        self.locator, self.terminal = "w:p1", "terminal-rollback"
        self.receipt = pane_bound_receipt(
            target_workspace="w2G",
            target_tab="w2G:t1",
            native_name=native_name_for(self.name),
            terminal_id=self.terminal,
        )
        self.action_id = seed_completed_current_generation(
            self.home, workspace_id=self.ws, lane_id=self.lane, role=self.role,
            assigned_name=self.name, locator=self.locator, terminal_id=self.terminal,
            receipt=self.receipt,
        )
        HerdrIdentityAttestationStore(home=self.home).upsert(
            IdentityAttestationRecord(
                self.name, self.ws, self.role, self.lane, self.locator, "present",
                observed_at="2026-08-11T00:00:00+00:00",
                terminal_id=self.terminal,
            )
        )
        self.participant = SimpleNamespace(
            role=self.role,
            assigned_name=self.name,
            locator=self.locator,
            receipt=self.receipt,
        )
        self.action = SimpleNamespace(
            action_id=self.action_id,
            unit=SimpleNamespace(workspace_id=self.ws, lane_id=self.lane),
            participants=(self.participant,),
        )

    @staticmethod
    def _ops(rows, *, prepared_state="absent"):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
            PreparedPaneObservation,
        )

        return SimpleNamespace(
            agent_rows=lambda: tuple(rows),
            prepared_pane=lambda **_kwargs: PreparedPaneObservation(
                state=prepared_state,
                terminal_reclaimed=(False if prepared_state == "absent" else None),
            ),
        )

    def test_completed_normal_requires_generation_and_private_terminal_absence(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
            _completed_rollback_absent,
        )

        self.assertTrue(_completed_rollback_absent(
            self.action, self._ops(()), self.home
        ))
        moved = ({
            "name": encode_assigned_name("other", self.role, "other-lane"),
            "pane_id": "w:p9", "terminal_id": self.terminal,
        },)
        self.assertFalse(_completed_rollback_absent(
            self.action, self._ops(moved), self.home
        ))
        missing = SimpleNamespace(
            action_id="startup-missing",
            unit=self.action.unit,
            participants=self.action.participants,
        )
        self.assertFalse(_completed_rollback_absent(
            missing, self._ops(()), self.home
        ))

    def test_completed_prepared_requires_exact_receipt_and_positive_pane_absence(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
            _completed_rollback_absent,
        )
        prepared_role = "claude"
        prepared_name = encode_assigned_name(self.ws, prepared_role, self.lane)
        prepared_locator = "w:p2"
        prepared_terminal = "terminal-prepared"
        prepared_receipt = pane_bound_receipt(
            target_workspace="w2G",
            target_tab="w2G:t1",
            native_name=native_name_for(prepared_name),
            terminal_id=prepared_terminal,
        )
        prepared = SimpleNamespace(
            role=prepared_role,
            assigned_name=prepared_name,
            locator=prepared_locator,
            receipt=prepared_receipt,
        )
        action = SimpleNamespace(
            action_id="startup-prepared",
            unit=self.action.unit,
            participants=(prepared,),
        )
        self.assertTrue(_completed_rollback_absent(
            action, self._ops((), prepared_state="absent"), self.home
        ))
        self.assertFalse(_completed_rollback_absent(
            action, self._ops((), prepared_state="present"), self.home
        ))
        malformed = SimpleNamespace(
            action_id=self.action_id, unit=self.action.unit,
            participants=(SimpleNamespace(
                role=prepared_role,
                assigned_name=prepared_name,
                locator=prepared_locator,
                receipt="pane_bound_v1 malformed",
            ),),
        )
        self.assertFalse(_completed_rollback_absent(
            malformed, self._ops((), prepared_state="absent"), self.home
        ))


if __name__ == "__main__":
    unittest.main()
