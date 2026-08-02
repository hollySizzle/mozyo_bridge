"""Launch identity receipt + update-relaunch evidence store (#14741).

Reworked to the corrected contract: load-bearing fail-closed (j#96966 C12), exact schema
signature and insert-or-identical CAS (C16), two-phase `unbound_pending` -> `attested` with
the composite proof (C13/j#96899), evidence keyed on the exact generation (C14), and
consumption only after a verified relaunch, by a durable action id (C15).

Everything runs against a real store in a temp home: the properties under test are
properties of the SQL, and a fake would only restate the assertions.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.launch_identity_receipt import (  # noqa: E402
    BIND_ALREADY_BOUND,
    BIND_ALREADY_CONSUMED,
    BIND_IDENTITY_MISMATCH,
    BIND_NO_ATTESTED_RECEIPT,
    BIND_OK,
    CONSUME_ABSENT,
    CONSUME_FOREIGN,
    CONSUME_OK,
    CONSUME_REPLAY,
    FINALIZE_NO_PENDING_MATCH,
    FINALIZE_OK,
    RECEIPT_ATTESTED,
    RECEIPT_UNBOUND_PENDING,
    RESERVE_IDENTICAL_REPLAY,
    RESERVE_OK,
    GenerationKey,
    LaunchIdentityReceiptError,
    LaunchIdentityReceiptStore,
    launch_identity_receipt_path,
)

DIGEST = "mzb1:" + "a" * 64
OTHER = "mzb1:" + "b" * 64
GEN = "startup-ir1-" + "c" * 64
REV = "7"


def _key(action="act-1", assigned="mzb1_wA_codex_lane", lane="issue_14741", provider="codex"):
    return GenerationKey("wA", lane, provider, assigned, action)


class ReceiptStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = LaunchIdentityReceiptStore(home=self.home)

    def _attest(self, key=None, digest=DIGEST, generation=GEN, revision=REV):
        key = key or _key()
        self.store.reserve(key, identity_digest=digest)
        self.assertEqual(
            self.store.finalize(
                key,
                identity_digest=digest,
                locator="wA:p1",
                lane_generation=generation,
                lifecycle_revision=revision,
                composite_proof=True,
            ),
            FINALIZE_OK,
        )
        return key

    # --- load-bearing (C12) -----------------------------------------------------------

    def test_an_absent_authority_is_an_error_not_an_absence_of_evidence(self) -> None:
        fresh = LaunchIdentityReceiptStore(home=self.home)
        with self.assertRaises(LaunchIdentityReceiptError) as ctx:
            fresh.read_receipt(_key())
        self.assertIn("zero-actuation", str(ctx.exception))

    def test_a_corrupt_authority_is_an_error(self) -> None:
        launch_identity_receipt_path(self.home).write_bytes(b"not a database")
        with self.assertRaises(LaunchIdentityReceiptError):
            self.store.read_receipt(_key())

    # --- exact schema signature (C16) -------------------------------------------------

    def test_a_wrong_schema_version_is_refused(self) -> None:
        self._attest()
        with sqlite3.connect(launch_identity_receipt_path(self.home)) as conn:
            conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(LaunchIdentityReceiptError):
            self.store.read_receipt(_key())

    def test_constraint_drift_and_attached_objects_are_refused(self) -> None:
        """Table NAMES are not a schema (audit j#96966 C16)."""
        for label, ddl in (
            ("extra column", "ALTER TABLE update_relaunch_evidence ADD COLUMN extra TEXT"),
            ("attached index", "CREATE INDEX ix ON launch_identity_receipts (provider)"),
            (
                "attached trigger",
                "CREATE TRIGGER tg AFTER INSERT ON update_relaunch_evidence"
                " BEGIN SELECT 1; END",
            ),
        ):
            with self.subTest(label=label):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                store = LaunchIdentityReceiptStore(home=Path(tmp.name))
                store.reserve(_key(), identity_digest=DIGEST)
                with sqlite3.connect(launch_identity_receipt_path(Path(tmp.name))) as conn:
                    conn.execute(ddl)
                with self.assertRaises(LaunchIdentityReceiptError):
                    store.read_receipt(_key())

    # --- two-phase (C13 / j#96899) ----------------------------------------------------

    def test_a_reservation_is_unbound_pending_and_claims_no_generation(self) -> None:
        self.assertEqual(self.store.reserve(_key(), identity_digest=DIGEST), RESERVE_OK)
        receipt = self.store.read_receipt(_key())
        self.assertEqual(receipt.phase, RECEIPT_UNBOUND_PENDING)
        self.assertFalse(receipt.attested)
        self.assertEqual(receipt.lane_generation, "")
        self.assertEqual(receipt.lifecycle_revision, "")

    def test_finalize_requires_the_composite_proof(self) -> None:
        key = _key()
        self.store.reserve(key, identity_digest=DIGEST)
        with self.assertRaises(LaunchIdentityReceiptError):
            self.store.finalize(
                key,
                identity_digest=DIGEST,
                locator="wA:p1",
                lane_generation=GEN,
                lifecycle_revision=REV,
                composite_proof=False,
            )
        self.assertEqual(self.store.read_receipt(key).phase, RECEIPT_UNBOUND_PENDING)

    def test_finalize_refuses_a_blank_axis(self) -> None:
        """A blank axis is not a weaker authority; it is a row nothing can join against."""
        key = _key()
        self.store.reserve(key, identity_digest=DIGEST)
        for label, kwargs in (
            ("no generation", {"lane_generation": ""}),
            ("no revision", {"lifecycle_revision": ""}),
            ("no locator", {"locator": ""}),
        ):
            with self.subTest(label=label):
                args = dict(
                    identity_digest=DIGEST,
                    locator="wA:p1",
                    lane_generation=GEN,
                    lifecycle_revision=REV,
                    composite_proof=True,
                )
                args.update(kwargs)
                with self.assertRaises(LaunchIdentityReceiptError):
                    self.store.finalize(key, **args)
        self.assertEqual(self.store.read_receipt(key).phase, RECEIPT_UNBOUND_PENDING)

    def test_finalize_is_a_cas_on_the_reserved_identity(self) -> None:
        key = _key()
        self.store.reserve(key, identity_digest=DIGEST)
        self.assertEqual(
            self.store.finalize(
                key,
                identity_digest=OTHER,
                locator="wA:p1",
                lane_generation=GEN,
                lifecycle_revision=REV,
                composite_proof=True,
            ),
            FINALIZE_NO_PENDING_MATCH,
        )
        self.assertEqual(self.store.read_receipt(key).phase, RECEIPT_UNBOUND_PENDING)

    def test_attested_carries_the_actual_generation_and_revision(self) -> None:
        key = self._attest()
        receipt = self.store.read_receipt(key)
        self.assertEqual(receipt.phase, RECEIPT_ATTESTED)
        self.assertEqual(receipt.lane_generation, GEN)
        self.assertEqual(receipt.lifecycle_revision, REV)
        self.assertEqual(receipt.locator, "wA:p1")

    def test_finalize_is_not_repeatable(self) -> None:
        key = self._attest()
        self.assertEqual(
            self.store.finalize(
                key,
                identity_digest=DIGEST,
                locator="wA:p1",
                lane_generation=GEN,
                lifecycle_revision=REV,
                composite_proof=True,
            ),
            FINALIZE_NO_PENDING_MATCH,
        )

    # --- insert-or-identical CAS (C16) ------------------------------------------------

    def test_an_identical_reservation_replays_and_a_divergent_one_is_refused(self) -> None:
        key = _key()
        self.assertEqual(self.store.reserve(key, identity_digest=DIGEST), RESERVE_OK)
        self.assertEqual(
            self.store.reserve(key, identity_digest=DIGEST), RESERVE_IDENTICAL_REPLAY
        )
        with self.assertRaises(LaunchIdentityReceiptError):
            self.store.reserve(key, identity_digest=OTHER)
        self.assertEqual(self.store.read_receipt(key).identity_digest, DIGEST)

    def test_a_reservation_never_downgrades_an_attested_receipt(self) -> None:
        key = self._attest()
        with self.assertRaises(LaunchIdentityReceiptError):
            self.store.reserve(key, identity_digest=DIGEST)
        self.assertEqual(self.store.read_receipt(key).phase, RECEIPT_ATTESTED)

    # --- evidence keyed on the exact generation (C14) ---------------------------------

    def test_binding_against_an_absent_authority_is_an_error_not_a_soft_no(self) -> None:
        with self.assertRaises(LaunchIdentityReceiptError):
            self.store.bind_evidence(
                _key(), blocker_id="update_prompt_available", identity_digest=DIGEST
            )

    def test_evidence_binds_only_onto_an_attested_matching_receipt(self) -> None:
        key = _key()
        # Seed the authority with an unrelated reservation so the store exists; a missing
        # receipt for THIS generation is then a typed no, not an absent-store error.
        self.store.reserve(_key(action="seed"), identity_digest=DIGEST)
        self.assertEqual(
            self.store.bind_evidence(
                key, blocker_id="update_prompt_available", identity_digest=DIGEST
            ),
            BIND_NO_ATTESTED_RECEIPT,
        )
        self.store.reserve(key, identity_digest=DIGEST)
        self.assertEqual(
            self.store.bind_evidence(
                key, blocker_id="update_prompt_available", identity_digest=DIGEST
            ),
            BIND_NO_ATTESTED_RECEIPT,
            "an unbound_pending receipt is not authority",
        )
        self._attest(key)
        self.assertEqual(
            self.store.bind_evidence(
                key, blocker_id="update_prompt_available", identity_digest=OTHER
            ),
            BIND_IDENTITY_MISMATCH,
        )
        self.assertEqual(
            self.store.bind_evidence(
                key, blocker_id="update_prompt_available", identity_digest=DIGEST
            ),
            BIND_OK,
        )

    def test_a_re_observation_is_idempotent_and_a_divergent_one_is_refused(self) -> None:
        key = self._attest()
        self.store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )
        self.assertEqual(
            self.store.bind_evidence(
                key, blocker_id="update_prompt_available", identity_digest=DIGEST
            ),
            BIND_ALREADY_BOUND,
        )
        with self.assertRaises(LaunchIdentityReceiptError):
            self.store.bind_evidence(
                key, blocker_id="update_in_progress", identity_digest=DIGEST
            )

    def test_a_foreign_generation_never_sees_this_evidence(self) -> None:
        key = self._attest()
        self.store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )
        for label, other in (
            ("other lane", _key(lane="issue_other")),
            ("other action", _key(action="act-2")),
            ("other provider", _key(provider="claude")),
            ("other assigned name", _key(assigned="mzb1_wA_codex_two")),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    self.store.bind_evidence(
                        other, blocker_id="update_prompt_available", identity_digest=DIGEST
                    ),
                    BIND_NO_ATTESTED_RECEIPT,
                )

    # --- staleness is a join, not a clock (C16) ---------------------------------------

    def _live(self, generation=GEN, revision=REV):
        return self.store.read_bound_evidence(
            workspace_id="wA",
            lane_id="issue_14741",
            provider="codex",
            lane_generation=generation,
            lifecycle_revision=revision,
        )

    def test_evidence_is_live_only_for_the_current_generation_and_revision(self) -> None:
        key = self._attest()
        self.store.bind_evidence(
            key, blocker_id="update_in_progress", identity_digest=DIGEST
        )
        found = self._live()
        self.assertIsNotNone(found)
        self.assertEqual(found.key.startup_action_id, key.startup_action_id)
        self.assertEqual(found.blocker_id, "update_in_progress")

        # One axis different in either direction is stale — no timestamps involved.
        self.assertIsNone(self._live(generation="startup-ir1-" + "d" * 64))
        self.assertIsNone(self._live(revision="8"))

    def test_consumed_evidence_is_never_live(self) -> None:
        key = self._attest()
        self.store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )
        self.store.consume_evidence(key, consumed_by="replacement-action-1")
        self.assertIsNone(self._live())

    def test_evidence_on_an_unattested_receipt_is_never_live(self) -> None:
        """Belt and braces: bind already refuses, and the read join refuses too."""
        key = self._attest()
        self.store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )
        with sqlite3.connect(launch_identity_receipt_path(self.home)) as conn:
            conn.execute(
                "UPDATE launch_identity_receipts SET phase = ?", (RECEIPT_UNBOUND_PENDING,)
            )
        self.assertIsNone(self._live())

    # --- consumption (C15) ------------------------------------------------------------

    def test_consume_is_replay_safe_for_the_same_actor_and_closed_to_others(self) -> None:
        key = self._attest()
        self.store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )
        self.assertEqual(
            self.store.consume_evidence(key, consumed_by="replacement-action-1"), CONSUME_OK
        )
        self.assertEqual(
            self.store.consume_evidence(key, consumed_by="replacement-action-1"),
            CONSUME_REPLAY,
        )
        self.assertEqual(
            self.store.consume_evidence(key, consumed_by="replacement-action-2"),
            CONSUME_FOREIGN,
        )

    def test_a_consumed_observation_never_re_arms(self) -> None:
        key = self._attest()
        self.store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )
        self.store.consume_evidence(key, consumed_by="replacement-action-1")
        self.assertEqual(
            self.store.bind_evidence(
                key, blocker_id="update_prompt_available", identity_digest=DIGEST
            ),
            BIND_ALREADY_CONSUMED,
        )

    def test_consuming_absent_evidence_is_typed_not_an_error(self) -> None:
        self._attest()
        self.assertEqual(
            self.store.consume_evidence(_key(), consumed_by="replacement-action-1"),
            CONSUME_ABSENT,
        )

    def test_the_actor_must_be_a_durable_action_id_not_a_path(self) -> None:
        key = self._attest()
        self.store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )
        with self.assertRaises(LaunchIdentityReceiptError):
            self.store.consume_evidence(key, consumed_by="/tmp/lane/worktree")

    # --- privacy ----------------------------------------------------------------------

    def test_the_stored_rows_carry_no_path_or_version(self) -> None:
        key = self._attest()
        self.store.bind_evidence(
            key, blocker_id="update_prompt_available", identity_digest=DIGEST
        )
        blob = launch_identity_receipt_path(self.home).read_bytes()
        self.assertNotIn(b"/usr/", blob)
        self.assertNotIn(b"node_modules", blob)
        self.assertNotIn(b"0.146.0", blob)


if __name__ == "__main__":
    unittest.main()
