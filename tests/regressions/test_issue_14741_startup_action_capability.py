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

UNIT = StartupUnit("wA", "issue_14741", ("codex", "claude"))
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
        with self.assertRaises(StartupTransactionError):
            self.fence.reserve(UNIT, "nonce-1", manifest=broken)
        self.assertFalse(self.path.exists(), "nothing was created")

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

    def test_a_tagged_action_whose_manifest_table_is_gone_is_zero_actuation(self) -> None:
        action = self._reserve_tagged()
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE startup_identity_manifests")
        with self.assertRaises(StartupTransactionError) as ctx:
            self.fence.read_identity_manifest(action.action_id)
        self.assertIn(REASON_RECEIPT_REQUIREMENT_UNAVAILABLE, str(ctx.exception))

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
        self.assertEqual(version, 1, "no schema bump")
        self.assertEqual(
            columns,
            {
                "action_id", "workspace_id", "lane_id", "providers", "phase", "revision",
                "participants", "reserved_at", "updated_at",
            },
            "the v1 nine columns are untouched",
        )

    def test_a_reused_nonce_is_still_refused_for_a_tagged_action(self) -> None:
        self._reserve_tagged()
        with self.assertRaises(StartupTransactionError):
            self._reserve_tagged()


if __name__ == "__main__":
    unittest.main()
