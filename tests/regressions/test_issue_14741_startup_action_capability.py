"""Versioned startup action + atomic sibling manifest (Redmine #14741, j#96917 step 1+2).

The capability marker for identity receipts is the SHAPE OF THE ACTION ID, and it lives in
``startup_actions.action_id`` — outside the identity-receipt sidecar. That placement is the
whole design (j#96892): if capability were recorded in the sidecar, deleting or corrupting
the sidecar would delete the capability too, a receipt-capable action would read as a
pre-feature legacy one, and the self-heal would fail OPEN into exactly the generic relaunch
this ticket exists to stop.

These tests pin that property, the byte-preservation of every existing legacy id, and the
atomicity of the action+manifest write.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.startup_transaction_fence import (  # noqa: E402
    CAPABILITY_IDENTITY_RECEIPT,
    CAPABILITY_LEGACY,
    REASON_RECEIPT_REQUIREMENT_UNAVAILABLE,
    IdentityManifest,
    IdentityManifestSlot,
    StartupTransactionError,
    StartupTransactionFence,
    StartupUnit,
    action_capability,
    requires_identity_receipt,
    startup_action_id,
    startup_action_id_matching,
)

from mozyo_bridge.core.state.startup_action_capability import (  # noqa: E402
    MIGRATION_ALREADY_V2,
    MIGRATION_OK,
    REASON_OFFLINE_UPGRADE_REQUIRED,
    StartupStoreMigrationRefused,
    migrate_startup_store_v1_to_v2,
    startup_store_migration_plan_digest,
)

UNIT = StartupUnit("wA", "issue_14741", ("codex", "claude"))


def _approved_digest(fence):
    """The digest an operator would have approved for the store as it stands."""
    with sqlite3.connect(fence.path) as conn:
        return startup_store_migration_plan_digest(conn)


def _to_v2(fence, tmpdir, seed_nonce="seed"):
    """Stand the store up and take it to v2 the only way a store may get there."""
    fence.reserve(UNIT, seed_nonce)
    return migrate_startup_store_v1_to_v2(
        fence,
        backup_path=Path(tmpdir) / "backup.sqlite",
        expected_plan_digest=_approved_digest(fence),
    )
DIGEST = "mzb1:" + "a" * 64


def _manifest(workspace="wA", lane="issue_14741", required=True):
    return IdentityManifest(
        workspace_id=workspace,
        lane_id=lane,
        slots=(
            IdentityManifestSlot("codex", "mzb1_wA_codex_lane", required, DIGEST if required else ""),
            IdentityManifestSlot("claude", "mzb1_wA_claude_lane", False, ""),
        ),
    )


class LegacyBytePreservationTest(unittest.TestCase):
    """Every pre-#14741 caller and every stored id must be unchanged."""

    def test_the_default_call_reproduces_the_legacy_id_exactly(self) -> None:
        # The literal pre-#14741 derivation, recomputed here independently so this test
        # fails if the legacy formula is ever touched — not merely if it changes shape.
        import hashlib
        import json

        canonical = UNIT.canonical()
        values = (
            canonical.workspace_id,
            canonical.lane_id,
            ",".join(canonical.providers),
            "nonce-1",
        )
        encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
        expected = "startup-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

        self.assertEqual(startup_action_id(UNIT, "nonce-1"), expected)
        self.assertEqual(action_capability(expected), CAPABILITY_LEGACY)
        self.assertFalse(requires_identity_receipt(expected))

    def test_a_legacy_reserve_writes_no_manifest_and_stays_untagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fence = StartupTransactionFence(Path(tmp) / "s.sqlite")
            action = fence.reserve(UNIT, "nonce-1")
            self.assertEqual(action_capability(action.action_id), CAPABILITY_LEGACY)
            # A legacy action promises nothing, so an absent manifest is benign — and it is
            # decided from the ID SHAPE, never from whether a row happens to exist.
            self.assertIsNone(fence.read_identity_manifest(action.action_id))


class CapabilityTaggingTest(unittest.TestCase):
    def test_a_tagged_id_is_content_bound_to_its_manifest(self) -> None:
        manifest = _manifest()
        tagged = startup_action_id(
            UNIT,
            "nonce-1",
            capability=CAPABILITY_IDENTITY_RECEIPT,
            manifest_digest=manifest.digest(),
        )
        self.assertTrue(tagged.startswith("startup-ir1-"))
        self.assertEqual(action_capability(tagged), CAPABILITY_IDENTITY_RECEIPT)
        self.assertTrue(requires_identity_receipt(tagged))
        # A different plan is a different action id, even with the same unit + nonce.
        other = _manifest()
        other = IdentityManifest(
            workspace_id="wA",
            lane_id="issue_14741",
            slots=(IdentityManifestSlot("codex", "mzb1_wA_codex_lane", True, "mzb1:zzz"),),
        )
        self.assertNotEqual(
            tagged,
            startup_action_id(
                UNIT,
                "nonce-1",
                capability=CAPABILITY_IDENTITY_RECEIPT,
                manifest_digest=other.digest(),
            ),
        )

    def test_a_tag_without_a_manifest_digest_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            startup_action_id(UNIT, "n", capability=CAPABILITY_IDENTITY_RECEIPT)

    def test_a_legacy_call_may_not_smuggle_a_manifest_digest(self) -> None:
        with self.assertRaises(ValueError):
            startup_action_id(UNIT, "n", manifest_digest="d" * 64)

    def test_an_unknown_capability_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            startup_action_id(UNIT, "n", capability="ir9", manifest_digest="d" * 64)

    def test_an_unclassifiable_action_id_is_never_assumed_legacy(self) -> None:
        """The fail-open j#96892 forbids: 'I cannot classify it' must not mean 'legacy'."""
        for bad in (
            "startup-",
            "startup-notahexdigest",
            "startup-ir1-",
            "startup-ir9-" + "a" * 64,
            "startup-" + "a" * 63,
            "startup-" + "A" * 64,
            "",
            None,
            12345,
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(StartupTransactionError):
                    action_capability(bad)


class ReDerivationSurfaceTest(unittest.TestCase):
    """The common helper every re-derivation surface goes through (j#96917 item 1)."""

    def test_a_legacy_observed_id_re_derives_byte_identically(self) -> None:
        legacy = startup_action_id(UNIT, "nonce-1")
        self.assertEqual(startup_action_id_matching(UNIT, "nonce-1", legacy), legacy)

    def test_a_tagged_observed_id_is_reported_as_not_re_derivable(self) -> None:
        """Old runtime + new action = fail-closed, and explicitly so.

        A tagged id is content-bound to a manifest these callers do not hold, so the honest
        answer is "" (no match) rather than a legacy id that would fail to match by
        coincidence of hashing. The callers treat "" as no-match.
        """
        tagged = startup_action_id(
            UNIT,
            "nonce-1",
            capability=CAPABILITY_IDENTITY_RECEIPT,
            manifest_digest=_manifest().digest(),
        )
        self.assertEqual(startup_action_id_matching(UNIT, "nonce-1", tagged), "")

    def test_an_unclassifiable_observed_id_raises(self) -> None:
        with self.assertRaises(StartupTransactionError):
            startup_action_id_matching(UNIT, "nonce-1", "startup-garbage")


class AtomicManifestCoCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.sqlite"
        self.fence = StartupTransactionFence(self.path)
        _to_v2(self.fence, self.tmp.name)

    def _reserve_tagged(self, nonce="nonce-1"):
        return self.fence.reserve(UNIT, nonce, manifest=_manifest())

    def test_reserve_writes_the_action_and_its_manifest_together(self) -> None:
        action = self._reserve_tagged()
        self.assertTrue(requires_identity_receipt(action.action_id))
        manifest = self.fence.read_identity_manifest(action.action_id)
        self.assertEqual(
            [(s.provider, s.identity_receipt_required) for s in manifest.slots],
            [("codex", True), ("claude", False)],
        )
        # The WHOLE plan is recorded: the unbound provider is present and explicitly
        # not-required, so "no obligation" is a written fact rather than an absence.
        self.assertEqual(len(manifest.required_slots()), 1)

    def test_a_manifest_that_cannot_be_canonicalised_reserves_nothing(self) -> None:
        """Zero-write: a bad plan must not leave an action row behind."""
        broken = IdentityManifest(
            workspace_id="wA",
            lane_id="issue_14741",
            # required with no pinned identity — a requirement nothing could satisfy
            slots=(IdentityManifestSlot("codex", "mzb1_wA_codex_lane", True, ""),),
        )
        with sqlite3.connect(self.path) as conn:
            before = conn.execute("SELECT COUNT(*) FROM startup_actions").fetchone()[0]
        with self.assertRaises(StartupTransactionError):
            self.fence.reserve(UNIT, "nonce-1", manifest=broken)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM startup_actions").fetchone()[0],
                before,
                "a non-canonical plan writes no action row",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM startup_identity_manifests"
                ).fetchone()[0],
                0,
            )

    def test_a_manifest_for_a_different_lane_is_refused(self) -> None:
        with self.assertRaises(StartupTransactionError):
            self.fence.reserve(UNIT, "nonce-1", manifest=_manifest(lane="other_lane"))

    def test_a_tagged_action_whose_manifest_row_is_gone_is_zero_actuation(self) -> None:
        """The capability survives sidecar loss — that is the point of tagging the id."""
        action = self._reserve_tagged()
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM startup_identity_manifests")
        # The action id STILL says receipt-capable, so this can never decay to legacy.
        self.assertTrue(requires_identity_receipt(action.action_id))
        with self.assertRaises(StartupTransactionError) as ctx:
            self.fence.read_identity_manifest(action.action_id)
        self.assertIn(REASON_RECEIPT_REQUIREMENT_UNAVAILABLE, str(ctx.exception))

    def test_a_v2_store_missing_the_manifest_table_is_a_partial_schema(self) -> None:
        """Under v2 the table is REQUIRED, so its absence is a partial schema, not 'empty'.

        That is the difference v2 buys: under v1 the sibling is additive and its absence
        means "no manifests here", which is benign. A v2 store cannot claim the capability
        contract while carrying none of the structure that contract is about.
        """
        action = self._reserve_tagged()
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE startup_identity_manifests")
        with self.assertRaises(StartupTransactionError):
            self.fence.read_identity_manifest(action.action_id)
        with self.assertRaises(StartupTransactionError):
            self.fence.read(action.action_id)

    def test_a_tampered_manifest_payload_is_detected(self) -> None:
        """The id is the digest's witness, so editing the plan is detectable."""
        action = self._reserve_tagged()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE startup_identity_manifests SET slots = ?",
                ('["ir1","wA","issue_14741",[["codex","x",false,""]]]',),
            )
        with self.assertRaises(StartupTransactionError) as ctx:
            self.fence.read_identity_manifest(action.action_id)
        self.assertIn(REASON_RECEIPT_REQUIREMENT_UNAVAILABLE, str(ctx.exception))

    def test_a_manifest_moved_between_actions_is_detected(self) -> None:
        """A consistent payload+digest pair filed under the wrong action still fails."""
        action = self._reserve_tagged()
        foreign = _manifest(workspace="wB", lane="other_lane")
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE startup_identity_manifests SET slots = ?, manifest_digest = ?",
                (foreign.canonical_payload(), foreign.digest()),
            )
        with self.assertRaises(StartupTransactionError) as ctx:
            self.fence.read_identity_manifest(action.action_id)
        self.assertIn(REASON_RECEIPT_REQUIREMENT_UNAVAILABLE, str(ctx.exception))

    def test_a_tagged_action_against_an_absent_store_is_zero_actuation(self) -> None:
        tagged = startup_action_id(
            UNIT,
            "nonce-1",
            capability=CAPABILITY_IDENTITY_RECEIPT,
            manifest_digest=_manifest().digest(),
        )
        with self.assertRaises(StartupTransactionError) as ctx:
            StartupTransactionFence(
                Path(self.tmp.name) / "absent.sqlite"
            ).read_identity_manifest(tagged)
        self.assertIn(REASON_RECEIPT_REQUIREMENT_UNAVAILABLE, str(ctx.exception))

    def test_the_existing_startup_action_surface_is_unaffected(self) -> None:
        """The sibling table must not disturb the v1 authority it lives beside."""
        action = self._reserve_tagged()
        read_back = self.fence.read(action.action_id)
        self.assertIsNotNone(read_back)
        self.assertEqual(read_back.action_id, action.action_id)
        self.assertEqual(read_back.unit.workspace_id, "wA")
        with sqlite3.connect(self.path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(startup_actions)").fetchall()
            }
        self.assertEqual(version, 2, "migrated store declares v2")
        self.assertEqual(
            columns,
            {
                "action_id", "workspace_id", "lane_id", "providers", "phase", "revision",
                "participants", "reserved_at", "updated_at",
            },
            "the v1 nine columns are untouched",
        )

    def test_an_exact_identical_replay_is_idempotent(self) -> None:
        """j#96917 / audit F4: one action retried, not a nonce reused."""
        first = self._reserve_tagged()
        again = self._reserve_tagged()
        self.assertEqual(again.action_id, first.action_id)
        self.assertEqual(again.phase, first.phase)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM startup_identity_manifests"
                ).fetchone()[0],
                1,
                "a replay must not write a second manifest",
            )

    def test_a_divergent_replay_is_still_refused(self) -> None:
        """Same unit+nonce but a DIFFERENT plan is not a replay; it is a reuse."""
        self._reserve_tagged()
        divergent = IdentityManifest(
            workspace_id="wA",
            lane_id="issue_14741",
            slots=(
                IdentityManifestSlot("codex", "mzb1_wA_codex_lane", True, "mzb1:CHANGED"),
                IdentityManifestSlot("claude", "mzb1_wA_claude_lane", False, ""),
            ),
        )
        # Audit j#96946 C2: a nonce names ONE action. A different plan hashes to a
        # different id, so the id lookup alone let this through as a SECOND action for the
        # same invocation. It is a zero-write conflict.
        with sqlite3.connect(self.path) as conn:
            before = conn.execute("SELECT COUNT(*) FROM startup_actions").fetchone()[0]
        with self.assertRaises(StartupTransactionError):
            self.fence.reserve(UNIT, "nonce-1", manifest=divergent)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM startup_actions").fetchone()[0], before
            )

    def test_a_replay_after_the_action_started_is_refused(self) -> None:
        """A reservation with effects in flight must never be handed to a second caller."""
        from mozyo_bridge.core.state.startup_transaction_fence import Participant

        action = self._reserve_tagged()
        self.fence.record_participant(
            action.action_id,
            Participant(
                assigned_name="mzb1_wA_codex_lane",
                role="codex",
                locator="wA:p1",
                receipt="wA",
            ),
        )
        with self.assertRaises(StartupTransactionError):
            self._reserve_tagged()

    def test_a_legacy_reserve_keeps_refusing_a_reused_nonce(self) -> None:
        """The legacy contract is untouched: a repeat is a nonce reuse."""
        self.fence.reserve(UNIT, "legacy-nonce")
        with self.assertRaises(StartupTransactionError):
            self.fence.reserve(UNIT, "legacy-nonce")


class AuditedDefectRegressionTest(unittest.TestCase):
    """The exact defects the j#96928 / j#96931 adversarial audit reproduced."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.sqlite"
        self.fence = StartupTransactionFence(self.path)
        _to_v2(self.fence, self.tmp.name)

    def _tagged(self):
        return self.fence.reserve(UNIT, "nonce-1", manifest=_manifest())

    def test_f1_payload_and_digest_rewritten_together_is_detected(self) -> None:
        """The digest column is mutable, so matching it proves nothing on its own.

        What makes the binding real is that the action id is a hash PREIMAGE of the digest:
        the reader recomputes the id from the stored row, which a coordinated rewrite —
        same workspace, same lane — cannot survive.
        """
        action = self._tagged()
        forged = IdentityManifest(
            workspace_id="wA",
            lane_id="issue_14741",
            slots=(
                IdentityManifestSlot("codex", "mzb1_wA_codex_lane", False, ""),
                IdentityManifestSlot("claude", "mzb1_wA_claude_lane", False, ""),
            ),
        )
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE startup_identity_manifests SET slots = ?, manifest_digest = ?",
                (forged.canonical_payload(), forged.digest()),
            )
        with self.assertRaises(StartupTransactionError) as ctx:
            self.fence.read_identity_manifest(action.action_id)
        self.assertIn(REASON_RECEIPT_REQUIREMENT_UNAVAILABLE, str(ctx.exception))

    def test_f2_the_generic_read_refuses_a_tagged_action_with_no_manifest(self) -> None:
        """rollback / status / current-action all consume THIS read."""
        action = self._tagged()
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM startup_identity_manifests")
        with self.assertRaises(StartupTransactionError) as ctx:
            self.fence.read(action.action_id)
        self.assertIn(REASON_RECEIPT_REQUIREMENT_UNAVAILABLE, str(ctx.exception))

    def test_f2_a_legacy_action_read_is_unaffected(self) -> None:
        action = self.fence.reserve(UNIT, "legacy-nonce")
        self.assertIsNotNone(self.fence.read(action.action_id))

    def test_f3_the_manifest_must_be_the_whole_plan(self) -> None:
        codex_only = IdentityManifest(
            workspace_id="wA",
            lane_id="issue_14741",
            slots=(IdentityManifestSlot("codex", "mzb1_wA_codex_lane", True, DIGEST),),
        )
        extra = IdentityManifest(
            workspace_id="wA",
            lane_id="issue_14741",
            slots=(
                IdentityManifestSlot("codex", "mzb1_wA_codex_lane", True, DIGEST),
                IdentityManifestSlot("claude", "mzb1_wA_claude_lane", False, ""),
                IdentityManifestSlot("fakex", "mzb1_wA_fakex_lane", False, ""),
            ),
        )
        duplicate = IdentityManifest(
            workspace_id="wA",
            lane_id="issue_14741",
            slots=(
                IdentityManifestSlot("codex", "mzb1_wA_codex_lane", True, DIGEST),
                IdentityManifestSlot("codex", "mzb1_wA_codex_two", False, ""),
                IdentityManifestSlot("claude", "mzb1_wA_claude_lane", False, ""),
            ),
        )
        for label, bad in (("partial", codex_only), ("extra", extra), ("dup", duplicate)):
            with self.subTest(label=label):
                with self.assertRaises(StartupTransactionError):
                    self.fence.reserve(UNIT, f"nonce-{label}", manifest=bad)

    def test_f5_a_foreign_named_table_is_never_written_into(self) -> None:
        """Zero-mutation: the named table's shape must be exactly what this build creates."""
        for label, ddl in (
            ("extra column", "CREATE TABLE startup_identity_manifests (action_id TEXT NOT NULL PRIMARY KEY, workspace_id TEXT NOT NULL, lane_id TEXT NOT NULL, protocol TEXT NOT NULL, slots TEXT NOT NULL, manifest_digest TEXT NOT NULL, nonce TEXT NOT NULL, recorded_at TEXT NOT NULL, extra TEXT)"),
            ("no primary key", "CREATE TABLE startup_identity_manifests (action_id TEXT NOT NULL, workspace_id TEXT NOT NULL, lane_id TEXT NOT NULL, protocol TEXT NOT NULL, slots TEXT NOT NULL, manifest_digest TEXT NOT NULL, nonce TEXT NOT NULL, recorded_at TEXT NOT NULL)"),
            ("type drift", "CREATE TABLE startup_identity_manifests (action_id TEXT NOT NULL PRIMARY KEY, workspace_id TEXT NOT NULL, lane_id TEXT NOT NULL, protocol TEXT NOT NULL, slots BLOB NOT NULL, manifest_digest TEXT NOT NULL, nonce TEXT NOT NULL, recorded_at TEXT NOT NULL)"),
            ("notnull drift", "CREATE TABLE startup_identity_manifests (action_id TEXT NOT NULL PRIMARY KEY, workspace_id TEXT NOT NULL, lane_id TEXT NOT NULL, protocol TEXT NOT NULL, slots TEXT, manifest_digest TEXT NOT NULL, nonce TEXT NOT NULL, recorded_at TEXT NOT NULL)"),
        ):
            with self.subTest(label=label):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                path = Path(tmp.name) / "s.sqlite"
                fence = StartupTransactionFence(path)
                _to_v2(fence, tmp.name)
                with sqlite3.connect(path) as conn:
                    conn.execute("DROP TABLE startup_identity_manifests")
                    conn.execute(ddl)
                    before = conn.execute(
                        "SELECT COUNT(*) FROM startup_identity_manifests"
                    ).fetchone()[0]
                with self.assertRaises(StartupTransactionError):
                    fence.reserve(UNIT, "nonce-1", manifest=_manifest())
                with sqlite3.connect(path) as conn:
                    after = conn.execute(
                        "SELECT COUNT(*) FROM startup_identity_manifests"
                    ).fetchone()[0]
                    actions = conn.execute(
                        "SELECT COUNT(*) FROM startup_actions"
                    ).fetchone()[0]
                self.assertEqual(after, before, "zero mutation of a foreign table")
                self.assertEqual(actions, 1, "and no action row was added either")

    def test_f5_an_unrelated_sibling_table_is_tolerated(self) -> None:
        """Scope correction (j#96931): only the NAMED table's shape is policed."""
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE somebody_elses_sidecar (a TEXT)")
        action = self.fence.reserve(UNIT, "nonce-1", manifest=_manifest())
        self.assertIsNotNone(self.fence.read_identity_manifest(action.action_id))

    def test_f6_a_trailing_newline_or_control_character_is_not_a_valid_id(self) -> None:
        legacy = startup_action_id(UNIT, "nonce-1")
        tagged = startup_action_id(
            UNIT,
            "nonce-1",
            capability=CAPABILITY_IDENTITY_RECEIPT,
            manifest_digest=_manifest().digest(),
        )
        for bad in (legacy + "\n", tagged + "\n", legacy + "\t", " " + tagged, tagged + "\x00"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(StartupTransactionError):
                    action_capability(bad)

    def test_f9_an_unknown_manifest_protocol_is_refused(self) -> None:
        future = IdentityManifest(
            workspace_id="wA",
            lane_id="issue_14741",
            protocol="future-v9",
            slots=(
                IdentityManifestSlot("codex", "mzb1_wA_codex_lane", True, DIGEST),
                IdentityManifestSlot("claude", "mzb1_wA_claude_lane", False, ""),
            ),
        )
        with self.assertRaises(ValueError):
            future.canonical_payload()
        with self.assertRaises(StartupTransactionError):
            self.fence.reserve(UNIT, "nonce-1", manifest=future)


if __name__ == "__main__":
    unittest.main()


class SchemaV2CutoverTest(unittest.TestCase):
    """Design Answer j#96936: the store version, not the action tag, is the old-runtime fence.

    The R11 j#96933 proof stands — an old runtime never inspects an action id, so a
    per-action marker can never make it fail closed. What it DOES enforce is an exact
    ``user_version == 1`` check. So tags are only ever written into v2 stores, and a v2
    store is one an old runtime rejects wholesale at the DB door.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "s.sqlite"
        self.fence = StartupTransactionFence(self.path)

    def _v1(self):
        self.fence.reserve(UNIT, "seed")
        return self.fence

    def test_a_fresh_store_is_still_v1_and_normal_startup_never_migrates(self) -> None:
        """No implicit migration: a normal startup leaves the version exactly as it found it."""
        self._v1()
        for nonce in ("a", "b", "c"):
            self.fence.reserve(UNIT, nonce)
            self.fence.read(startup_action_id(UNIT, nonce))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                    " AND name='startup_identity_manifests'"
                ).fetchone()[0],
                0,
                "and it creates no manifest table either",
            )

    def test_a_tagged_reserve_on_a_v1_store_is_typed_and_zero_write(self) -> None:
        self._v1()
        with sqlite3.connect(self.path) as conn:
            before = conn.execute("SELECT COUNT(*) FROM startup_actions").fetchone()[0]
        with self.assertRaises(StartupTransactionError) as ctx:
            self.fence.reserve(UNIT, "nonce-1", manifest=_manifest())
        self.assertIn(REASON_OFFLINE_UPGRADE_REQUIRED, str(ctx.exception))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM startup_actions").fetchone()[0], before
            )
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_legacy_actions_still_read_on_a_v1_store(self) -> None:
        self._v1()
        self.assertIsNotNone(self.fence.read(startup_action_id(UNIT, "seed")))

    def test_after_migration_tagged_reserve_read_and_replay_all_work(self) -> None:
        self._v1()
        self.assertEqual(
            migrate_startup_store_v1_to_v2(
                self.fence,
                backup_path=self.dir / "b.sqlite",
                expected_plan_digest=_approved_digest(self.fence),
            ).outcome,
            MIGRATION_OK,
        )
        action = self.fence.reserve(UNIT, "nonce-1", manifest=_manifest())
        self.assertTrue(requires_identity_receipt(action.action_id))
        self.assertIsNotNone(self.fence.read_identity_manifest(action.action_id))
        self.assertEqual(
            self.fence.reserve(UNIT, "nonce-1", manifest=_manifest()).action_id,
            action.action_id,
        )
        # And the legacy action written before the cutover is still there.
        self.assertIsNotNone(self.fence.read(startup_action_id(UNIT, "seed")))

    def test_migration_is_idempotent(self) -> None:
        self._v1()
        migrate_startup_store_v1_to_v2(
            self.fence,
            backup_path=self.dir / "b.sqlite",
            expected_plan_digest=_approved_digest(self.fence),
        )
        # Audit j#96966 C10: "already v2" is not "I already did this". Without the
        # external completion receipt for THIS plan and target, replay is unverified —
        # and the old test asserting an arbitrary digest succeeded was itself the defect.
        with self.assertRaises(StartupStoreMigrationRefused) as ctx:
            migrate_startup_store_v1_to_v2(
                self.fence,
                backup_path=self.dir / "b2.sqlite",
                expected_plan_digest="0" * 64,
            )
        self.assertEqual(ctx.exception.reason, "already_v2_unverified")

    def test_migration_refuses_plan_drift_with_zero_mutation(self) -> None:
        self._v1()
        with sqlite3.connect(self.path) as conn:
            approved = startup_store_migration_plan_digest(conn)
        # The store moves on after the plan was approved.
        self.fence.reserve(UNIT, "unplanned")
        with self.assertRaises(StartupStoreMigrationRefused) as ctx:
            migrate_startup_store_v1_to_v2(
                self.fence,
                backup_path=self.dir / "b.sqlite",
                expected_plan_digest=approved,
            )
        self.assertEqual(ctx.exception.reason, "plan_drift")
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_migration_refuses_a_foreign_sibling_table_with_zero_mutation(self) -> None:
        self._v1()
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE startup_identity_manifests (wrong TEXT)")
        # A well-formed but arbitrary digest: the sibling refusal is reached first, and
        # computing the real one would itself trip over the foreign table.
        with self.assertRaises(StartupStoreMigrationRefused) as ctx:
            migrate_startup_store_v1_to_v2(
                self.fence,
                backup_path=self.dir / "b.sqlite",
                expected_plan_digest="0" * 64,
            )
        self.assertEqual(ctx.exception.reason, "foreign_sibling_schema")
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_migration_refuses_a_backup_it_cannot_write(self) -> None:
        self._v1()
        with self.assertRaises(StartupStoreMigrationRefused) as ctx:
            migrate_startup_store_v1_to_v2(
                self.fence,
                backup_path=self.dir / "s.sqlite" / "nested" / "b.sqlite",
                expected_plan_digest=_approved_digest(self.fence),
            )
        self.assertEqual(ctx.exception.reason, "backup_failed")
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_migration_refuses_a_store_already_holding_tagged_rows(self) -> None:
        """Nothing this build wrote could be in that state, so the history is not as claimed."""
        self._v1()
        forged = startup_action_id(
            UNIT,
            "x",
            capability=CAPABILITY_IDENTITY_RECEIPT,
            manifest_digest=_manifest().digest(),
        )
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO startup_actions (action_id, workspace_id, lane_id, providers,"
                " phase, revision, participants, reserved_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (forged, "wA", "issue_14741", "claude,codex", "planned", 1, "[]", "t", "t"),
            )
        with self.assertRaises(StartupStoreMigrationRefused) as ctx:
            migrate_startup_store_v1_to_v2(
                self.fence,
                backup_path=self.dir / "b.sqlite",
                expected_plan_digest=_approved_digest(self.fence),
            )
        self.assertEqual(ctx.exception.reason, "tagged_rows_present")
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)


class ParentRuntimeRejectsV2Test(unittest.TestCase):
    """The old-runtime fail-closed EVIDENCE (j#96936 item 4).

    Runs the ACTUAL parent runtime's fence module, vendored verbatim from `4867fa0a`, against
    a v2 store. This is the proof R11 could not give with a per-action tag: the old code is
    executed, not described, and it rejects the store at its own `user_version` check —
    which is the check every one of its surfaces (rollback, status, current-action) goes
    through, because they all open the store the same way.
    """

    FIXTURE = (
        Path(__file__).resolve().parents[1]
        / "support"
        / "fixtures"
        / "parent_runtime_startup_fence_4867fa0a.py.txt"
    )

    def _parent_module(self):
        import importlib.util

        name = "parent_fence_4867fa0a"
        spec = importlib.util.spec_from_loader(name, loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(self.FIXTURE)
        # `@dataclass` resolves its own module out of `sys.modules`, so the module must be
        # registered before the body executes.
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        exec(compile(self.FIXTURE.read_text(), str(self.FIXTURE), "exec"), module.__dict__)
        return module

    def test_the_vendored_fixture_is_the_parent_runtime(self) -> None:
        """A stale fixture would make this whole proof vacuous."""
        self.assertTrue(self.FIXTURE.exists())
        text = self.FIXTURE.read_text()
        self.assertIn("STARTUP_TRANSACTION_FENCE_SCHEMA_VERSION = 1", text)
        self.assertNotIn("startup_identity_manifests", text)
        self.assertNotIn("CAPABILITY_IDENTITY_RECEIPT", text)

    def test_the_parent_runtime_reads_a_v1_store_but_rejects_a_v2_store(self) -> None:
        parent = self._parent_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.sqlite"
            fence = StartupTransactionFence(path)
            action = fence.reserve(UNIT, "seed")

            # v1: the parent runtime is perfectly happy — mixed-runtime still works.
            self.assertIsNotNone(
                parent.StartupTransactionFence(path).read(action.action_id)
            )

            migrate_startup_store_v1_to_v2(
                fence,
                backup_path=Path(tmp) / "b.sqlite",
                expected_plan_digest=_approved_digest(fence),
            )

            # v2: the parent rejects the WHOLE store, so every surface it has — rollback,
            # status, current-action — fails closed, not merely the tagged actions.
            old = parent.StartupTransactionFence(path)
            for surface in (
                lambda: old.read(action.action_id),
                lambda: old.store_shape() and old.read(action.action_id),
            ):
                with self.assertRaises(parent.StartupTransactionError) as ctx:
                    surface()
                self.assertIn("schema", str(ctx.exception).lower())


class AuditJ96946RegressionTest(unittest.TestCase):
    """The j#96946 adversarial findings (C2-C7), each reproduced before being fixed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "s.sqlite"
        self.fence = StartupTransactionFence(self.path)
        _to_v2(self.fence, self.tmp.name)

    def _rows(self, table="startup_actions"):
        with sqlite3.connect(self.path) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # C2 -------------------------------------------------------------------------------
    def test_c2_a_legacy_action_blocks_a_tagged_reserve_on_the_same_nonce(self) -> None:
        """The nonce authority must be digest-INDEPENDENT in both directions."""
        self.fence.reserve(UNIT, "shared-nonce")
        before = self._rows()
        with self.assertRaises(StartupTransactionError):
            self.fence.reserve(UNIT, "shared-nonce", manifest=_manifest())
        self.assertEqual(self._rows(), before)

    # C3 -------------------------------------------------------------------------------
    def test_c3_constraint_index_and_trigger_drift_are_zero_write(self) -> None:
        """`PRAGMA table_info` cannot see any of these, so the stored DDL is compared."""
        base = (
            "action_id TEXT NOT NULL PRIMARY KEY, workspace_id TEXT NOT NULL,"
            " lane_id TEXT NOT NULL, protocol TEXT NOT NULL, slots TEXT NOT NULL,"
            " manifest_digest TEXT NOT NULL, nonce TEXT NOT NULL, recorded_at TEXT NOT NULL"
        )
        for label, setup in (
            (
                "extra CHECK",
                [f"CREATE TABLE startup_identity_manifests ({base}, CHECK (length(nonce) > 0))"],
            ),
            (
                "extra UNIQUE",
                [f"CREATE TABLE startup_identity_manifests ({base}, UNIQUE (nonce))"],
            ),
            (
                "attached index",
                [
                    f"CREATE TABLE startup_identity_manifests ({base})",
                    "CREATE INDEX ix_mzb ON startup_identity_manifests (nonce)",
                ],
            ),
            (
                "attached trigger",
                [
                    f"CREATE TABLE startup_identity_manifests ({base})",
                    "CREATE TRIGGER tg_mzb AFTER INSERT ON startup_identity_manifests"
                    " BEGIN SELECT 1; END",
                ],
            ),
        ):
            with self.subTest(label=label):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                path = Path(tmp.name) / "s.sqlite"
                fence = StartupTransactionFence(path)
                _to_v2(fence, tmp.name)
                with sqlite3.connect(path) as conn:
                    conn.execute("DROP TABLE startup_identity_manifests")
                    for ddl in setup:
                        conn.execute(ddl)
                    before = conn.execute(
                        "SELECT COUNT(*) FROM startup_identity_manifests"
                    ).fetchone()[0]
                with self.assertRaises(StartupTransactionError):
                    fence.reserve(UNIT, "nonce-1", manifest=_manifest())
                with sqlite3.connect(path) as conn:
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM startup_identity_manifests"
                        ).fetchone()[0],
                        before,
                        "zero mutation of a foreign table",
                    )

    # C4 -------------------------------------------------------------------------------
    def test_c4_a_non_boolean_receipt_flag_is_refused_on_the_way_in(self) -> None:
        for flag in (1, 0, "true", "", None):
            with self.subTest(flag=flag):
                with self.assertRaises(ValueError):
                    IdentityManifestSlot("codex", "cn", flag, DIGEST).canonical()

    def test_c4_a_non_boolean_receipt_flag_is_refused_on_the_way_out(self) -> None:
        """`bool(1)` is True — a stored integer must not decode into an obligation."""
        action = self.fence.reserve(UNIT, "nonce-1", manifest=_manifest())
        with sqlite3.connect(self.path) as conn:
            payload = conn.execute(
                "SELECT slots FROM startup_identity_manifests"
            ).fetchone()[0]
            conn.execute(
                "UPDATE startup_identity_manifests SET slots = ?",
                (payload.replace("true", "1"),),
            )
        with self.assertRaises(StartupTransactionError):
            self.fence.read_identity_manifest(action.action_id)

    def test_c4_a_padded_stored_witness_is_refused(self) -> None:
        action = self.fence.reserve(UNIT, "nonce-1", manifest=_manifest())
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE startup_identity_manifests SET nonce = ?", (" nonce-1 ",))
        with self.assertRaises(StartupTransactionError) as ctx:
            self.fence.read_identity_manifest(action.action_id)
        self.assertIn(REASON_RECEIPT_REQUIREMENT_UNAVAILABLE, str(ctx.exception))

    def test_c4_two_providers_may_not_share_one_assigned_name(self) -> None:
        """An assigned name is a host-unique herdr identity."""
        clash = IdentityManifest(
            workspace_id="wA",
            lane_id="issue_14741",
            slots=(
                IdentityManifestSlot("codex", "same_name", True, DIGEST),
                IdentityManifestSlot("claude", "same_name", False, ""),
            ),
        )
        before = self._rows()
        with self.assertRaises(StartupTransactionError):
            self.fence.reserve(UNIT, "nonce-1", manifest=clash)
        self.assertEqual(self._rows(), before)

    # C5 -------------------------------------------------------------------------------
    def test_c5_a_whitespace_padded_action_id_is_never_laundered_by_the_authority_read(self):
        action = self.fence.reserve(UNIT, "nonce-1", manifest=_manifest())
        legacy = self.fence.reserve(UNIT, "legacy-nonce")
        for padded in (
            action.action_id + "\n",
            action.action_id + "\t",
            " " + action.action_id,
            legacy.action_id + "\n",
        ):
            with self.subTest(padded=repr(padded)):
                # Either a typed refusal (unclassifiable id) or simply no such row — never
                # the canonical row.
                try:
                    self.assertIsNone(self.fence.read(padded))
                except StartupTransactionError:
                    pass

    # C6 -------------------------------------------------------------------------------
    def test_c6_a_tampered_protocol_column_is_refused(self) -> None:
        action = self.fence.reserve(UNIT, "nonce-1", manifest=_manifest())
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE startup_identity_manifests SET protocol = ?", ("future-v9",))
        with self.assertRaises(StartupTransactionError) as ctx:
            self.fence.read_identity_manifest(action.action_id)
        self.assertIn(REASON_RECEIPT_REQUIREMENT_UNAVAILABLE, str(ctx.exception))

    # C7 -------------------------------------------------------------------------------
    def test_c7_a_database_error_surfaces_as_a_typed_authority_error(self) -> None:
        """The handler referenced `sqlite3` — a missing import would raise NameError here."""
        import mozyo_bridge.core.state.startup_action_capability as capability

        action = self.fence.reserve(UNIT, "nonce-1", manifest=_manifest())

        class _Boom:
            def execute(self, *a, **k):
                raise sqlite3.DatabaseError("synthetic")

        # The rollback helper is the handler that names sqlite3; it must swallow a DB error
        # rather than explode with NameError.
        capability._rollback_quietly if hasattr(capability, "_rollback_quietly") else None
        from mozyo_bridge.core.state.startup_transaction_fence import _rollback_quietly

        _rollback_quietly(_Boom())  # must not raise

        # And a read against a store whose table read fails is a typed authority error.
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE startup_identity_manifests")
        with self.assertRaises(StartupTransactionError):
            self.fence.read_identity_manifest(action.action_id)


class MigrationBackupAndDigestTest(unittest.TestCase):
    """j#96959 C8/C9: the recovery point must be real, and the plan must be pre-approved.

    Every case runs against a temp isolated store. No shared home is touched.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "s.sqlite"
        self.fence = StartupTransactionFence(self.path)
        self.fence.reserve(UNIT, "seed")

    def _migrate(self, **kw):
        from mozyo_bridge.core.state.startup_store_migration import (
            migrate_startup_store_v1_to_v2 as run,
        )

        kw.setdefault("backup_path", self.dir / "backup.sqlite")
        kw.setdefault("expected_plan_digest", _approved_digest(self.fence))
        return run(self.fence, **kw)

    # C8 -------------------------------------------------------------------------------
    def _wal_with_uncheckpointed_commit(self) -> int:
        """Commit an action that stays in the WAL, and return the true row count."""
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        conn = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            # lower(hex(...)): the classifier requires lowercase hex, and an uppercase id
            # is (correctly) unclassifiable — which is a different test than this one.
            "INSERT INTO startup_actions VALUES"
            " ('startup-'||substr(lower(hex(randomblob(32))),1,64),'wA','issue_14741',"
            "'claude,codex','planned',1,'[]','t','t')"
        )
        conn.execute("COMMIT")
        return conn.execute("SELECT COUNT(*) FROM startup_actions").fetchone()[0]

    def test_c8_an_uncheckpointed_committed_action_survives_into_the_snapshot(self) -> None:
        """A raw file copy loses it; the backup API keeps it. Measured, not assumed."""
        expected = self._wal_with_uncheckpointed_commit()
        self.assertEqual(expected, 2)

        # What the OLD implementation would have preserved, for contrast.
        import shutil

        raw = self.dir / "raw.sqlite"
        shutil.copy2(self.path, raw)
        raw_rows = sqlite3.connect(raw).execute(
            "SELECT COUNT(*) FROM startup_actions"
        ).fetchone()[0]
        self.assertLess(raw_rows, expected, "a raw copy really does drop WAL commits")

        result = self._migrate()
        self.assertEqual(result.outcome, MIGRATION_OK)
        backup_rows = sqlite3.connect(result.backup_path).execute(
            "SELECT COUNT(*) FROM startup_actions"
        ).fetchone()[0]
        self.assertEqual(backup_rows, expected, "the snapshot is a real recovery point")

    def test_c8_a_failed_backup_publishes_nothing_and_migrates_nothing(self) -> None:
        import mozyo_bridge.core.state.startup_store_migration as migration

        for stage in ("staged_backup", "publish_backup"):
            with self.subTest(stage=stage):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                path = Path(tmp.name) / "s.sqlite"
                fence = StartupTransactionFence(path)
                fence.reserve(UNIT, "seed")
                backup = Path(tmp.name) / "backup.sqlite"

                def boom(*a, **k):
                    raise OSError("synthetic failure")

                real = getattr(migration, stage)
                setattr(migration, stage, boom)
                try:
                    with self.assertRaises(StartupStoreMigrationRefused) as ctx:
                        migration.migrate_startup_store_v1_to_v2(
                            fence,
                            backup_path=backup,
                            expected_plan_digest=_approved_digest(fence),
                        )
                finally:
                    setattr(migration, stage, real)
                self.assertEqual(ctx.exception.reason, "backup_failed")
                self.assertFalse(backup.exists(), "no partial backup is published")
                self.assertFalse(
                    backup.with_name(backup.name + ".staging").exists(),
                    "and no staging file is left behind",
                )
                with sqlite3.connect(path) as conn:
                    self.assertEqual(
                        conn.execute("PRAGMA user_version").fetchone()[0],
                        1,
                        "the source store was not migrated",
                    )

    def test_c8_a_snapshot_that_does_not_read_back_is_refused(self) -> None:
        """The snapshot is verified against the source before it is published."""
        import mozyo_bridge.core.state.startup_store_migration as migration

        backup = self.dir / "backup.sqlite"
        real = migration._store_facts
        calls = {"n": 0}

        def drifting(conn):
            calls["n"] += 1
            facts = real(conn)
            # Make the SNAPSHOT readback disagree with the source.
            return facts if calls["n"] == 1 else (facts[0], facts[1] + 1, facts[2])

        migration._store_facts = drifting
        try:
            with self.assertRaises(StartupStoreMigrationRefused) as ctx:
                self._migrate(backup_path=backup)
        finally:
            migration._store_facts = real
        self.assertEqual(ctx.exception.reason, "backup_failed")
        self.assertFalse(backup.exists())

    # C9 -------------------------------------------------------------------------------
    def test_c9_a_missing_padded_or_malformed_digest_is_zero_write(self) -> None:
        good = _approved_digest(self.fence)
        for label, digest in (
            ("empty", ""),
            ("padded", " " + good),
            ("trailing newline", good + "\n"),
            ("uppercase", good.upper()),
            ("truncated", good[:-1]),
            ("not a digest", "not-a-digest"),
            ("wrong type", None),
        ):
            with self.subTest(label=label):
                with self.assertRaises(StartupStoreMigrationRefused) as ctx:
                    self._migrate(expected_plan_digest=digest)
                self.assertEqual(ctx.exception.reason, "plan_digest_required")
                with sqlite3.connect(self.path) as conn:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertFalse((self.dir / "backup.sqlite").exists())

    def test_c9_only_an_exact_digest_on_a_clean_v1_store_proceeds(self) -> None:
        result = self._migrate()
        self.assertEqual(result.outcome, MIGRATION_OK)
        self.assertEqual(result.schema_version, 2)
        self.assertTrue(result.backup_path)
        self.assertTrue(result.content_digest)
        self.assertEqual(result.action_count, 1)


class MigrationReplayAndRecoveryArtifactTest(unittest.TestCase):
    """j#96966 C10/C11: an unproven replay is not a success, and a DB alone is not a backup."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "s.sqlite"
        self.fence = StartupTransactionFence(self.path)
        self.fence.reserve(UNIT, "seed")
        self.plan = _approved_digest(self.fence)

    def _run(self, **kw):
        from mozyo_bridge.core.state.startup_store_migration import (
            migrate_startup_store_v1_to_v2 as run,
        )

        kw.setdefault("backup_path", self.dir / "backup.sqlite")
        kw.setdefault("expected_plan_digest", self.plan)
        return run(self.fence, **kw)

    # C10 ------------------------------------------------------------------------------
    def test_c10_replay_without_a_completion_receipt_is_refused(self) -> None:
        self.assertEqual(self._run().outcome, MIGRATION_OK)
        with self.assertRaises(StartupStoreMigrationRefused) as ctx:
            self._run(backup_path=self.dir / "b2.sqlite")
        self.assertEqual(ctx.exception.reason, "already_v2_unverified")

    def test_c10_a_foreign_or_malformed_receipt_does_not_prove_completion(self) -> None:
        self._run()
        for label, receipt in (
            ("wrong plan", {"plan_digest": "1" * 64, "target_path": str(self.path),
                            "outcome": MIGRATION_OK}),
            ("wrong target", {"plan_digest": self.plan, "target_path": "/elsewhere.sqlite",
                              "outcome": MIGRATION_OK}),
            ("not completed", {"plan_digest": self.plan, "target_path": str(self.path),
                               "outcome": "in_progress"}),
            ("not a mapping", "receipt"),
            ("absent", None),
        ):
            with self.subTest(label=label):
                with self.assertRaises(StartupStoreMigrationRefused) as ctx:
                    self._run(backup_path=self.dir / "b3.sqlite", completion_receipt=receipt)
                self.assertEqual(ctx.exception.reason, "already_v2_unverified")

    def test_c10_the_matching_completion_receipt_replays_idempotently(self) -> None:
        first = self._run()
        replay = self._run(
            backup_path=self.dir / "b4.sqlite",
            completion_receipt={
                "plan_digest": self.plan,
                "target_path": str(self.path),
                "outcome": MIGRATION_OK,
            },
        )
        self.assertEqual(replay.outcome, MIGRATION_ALREADY_V2)
        self.assertEqual(replay.schema_version, 2)
        self.assertEqual(replay.content_digest, first.content_digest)

    # C11 ------------------------------------------------------------------------------
    def test_c11_the_artifact_carries_the_seal_and_restores_a_usable_authority(self) -> None:
        """A DB-only restore is a DAMAGED authority; the pair restores a working one."""
        result = self._run()
        self.assertTrue(result.backup_seal_path)
        self.assertTrue(result.seal_nonce_verified)

        # DB alone: the fence requires its external seal, so this refuses.
        db_only = self.dir / "restore_db_only.sqlite"
        db_only.write_bytes(Path(result.backup_path).read_bytes())
        with self.assertRaises(StartupTransactionError):
            StartupTransactionFence(db_only).read(startup_action_id(UNIT, "seed"))

        # DB + seal: a real recovery point.
        paired = self.dir / "restore_paired.sqlite"
        paired.write_bytes(Path(result.backup_path).read_bytes())
        paired.with_name(paired.name + ".seal").write_bytes(
            Path(result.backup_seal_path).read_bytes()
        )
        restored = StartupTransactionFence(paired).read(startup_action_id(UNIT, "seed"))
        self.assertIsNotNone(restored, "the DB+seal artifact restores a usable authority")

    def test_c11_a_missing_seal_refuses_the_migration_with_zero_mutation(self) -> None:
        Path(self.fence.seal_path).unlink()
        with self.assertRaises(StartupTransactionError):
            self._run()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
