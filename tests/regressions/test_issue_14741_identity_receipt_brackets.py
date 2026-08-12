"""The three-bracket identity receipt wiring (Redmine #14741, j#96899 / j#96966 C12-C13).

The receipt needs three moments, not two, because `prepare_session`'s launch-generation
finalize runs strictly BEFORE the actuator declares the lane's lifecycle row (measured,
j#97001). These tests pin the contract each bracket carries.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.launch_identity_receipt import (  # noqa: E402
    RECEIPT_ATTESTED,
    RECEIPT_UNBOUND_PENDING,
    GenerationKey,
    LaunchIdentityReceiptStore,
)
from mozyo_bridge.core.state.startup_transaction_fence import (  # noqa: E402
    CAPABILITY_IDENTITY_RECEIPT,
    IdentityManifest,
    IdentityManifestSlot,
    StartupUnit,
    startup_action_id,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_identity_binding import (  # noqa: E402,E501
    finalize_lane_identity_receipts,
    reserve_session_launch_identities,
)

UNIT = StartupUnit("wA", "issue_14741", ("codex", "claude"))
DIGEST = "mzb1:" + "a" * 64


def _legacy_action() -> str:
    return startup_action_id(UNIT, "nonce-1")


def _tagged_action() -> str:
    manifest = IdentityManifest(
        workspace_id="wA",
        lane_id="issue_14741",
        slots=(
            IdentityManifestSlot("codex", "mzb1_wA_codex_lane", True, DIGEST),
            IdentityManifestSlot("claude", "mzb1_wA_claude_lane", False, ""),
        ),
    )
    return startup_action_id(
        UNIT,
        "nonce-1",
        capability=CAPABILITY_IDENTITY_RECEIPT,
        manifest_digest=manifest.digest(),
    )


def _tagged_two_bound_action() -> str:
    manifest = IdentityManifest(
        workspace_id="wA",
        lane_id="issue_14741",
        slots=(
            IdentityManifestSlot("codex", "mzb1_wA_codex_lane", True, DIGEST),
            IdentityManifestSlot("claude", "mzb1_wA_claude_lane", True, DIGEST),
        ),
    )
    return startup_action_id(
        UNIT,
        "nonce-two-bound",
        capability=CAPABILITY_IDENTITY_RECEIPT,
        manifest_digest=manifest.digest(),
    )


class _Plan:
    def __init__(self, provider, assigned):
        self.provider = provider
        self.assigned_name = assigned


class Bracket1ReserveTest(unittest.TestCase):
    """Bracket 1: pre-side-effect reservation, fail-closed, and only when capable."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.plans = [_Plan("codex", "mzb1_wA_codex_lane")]
        self.resolved = {"codex": SimpleNamespace(exec_target="/nowhere/codex")}

    def _reserve(self, action_id, **kw):
        kw.setdefault("attest_launcher", "/usr/bin/attest")
        return reserve_session_launch_identities(
            store_home=self.home,
            transaction=SimpleNamespace(action_id=action_id),
            launch_plans=self.plans,
            workspace_id="wA",
            lane_id="issue_14741",
            resolved=self.resolved,
            **kw,
        )

    def test_a_legacy_action_never_touches_the_receipt_store(self) -> None:
        """Byte-invariance: the pre-#14741 launch must not gain a new dependency."""
        self._reserve(_legacy_action())
        self.assertFalse(
            (self.home / "launch-identity-receipt.sqlite").exists(),
            "a legacy launch creates no receipt authority at all",
        )

    def test_an_unwrapped_or_planless_run_reserves_nothing(self) -> None:
        self._reserve(_tagged_action(), attest_launcher="")
        self.assertFalse((self.home / "launch-identity-receipt.sqlite").exists())

    def test_a_capable_action_whose_identity_cannot_be_pinned_reserves_nothing(self) -> None:
        """An unbound / unresolvable provider carries no obligation the manifest did not state."""
        self.resolved = {"codex": SimpleNamespace(exec_target="")}
        self._reserve(_tagged_action())
        self.assertFalse((self.home / "launch-identity-receipt.sqlite").exists())

    def test_a_capable_action_reserves_unbound_pending_from_the_pinned_identity(self) -> None:
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_identity_binding as binding

        real = binding._pinned_identity
        binding._pinned_identity = lambda provider, resolved: DIGEST
        try:
            self._reserve(_tagged_action())
        finally:
            binding._pinned_identity = real
        receipt = LaunchIdentityReceiptStore(home=self.home).read_receipt(
            GenerationKey("wA", "issue_14741", "codex", "mzb1_wA_codex_lane", _tagged_action())
        )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.phase, RECEIPT_UNBOUND_PENDING)
        self.assertEqual(receipt.identity_digest, DIGEST)
        self.assertEqual(receipt.lane_generation, "", "bracket 1 claims no generation")

    def test_a_store_failure_on_a_capable_action_is_a_zero_actuation_refusal(self) -> None:
        """C12: the launch refuses rather than starting a lane it could never prove."""
        import mozyo_bridge.core.state.launch_identity_receipt as receipts
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_identity_binding as binding
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
            HerdrSessionStartError,
        )

        real_identity = binding._pinned_identity
        real_reserve = receipts.LaunchIdentityReceiptStore.reserve

        def boom(self, *a, **k):
            raise receipts.LaunchIdentityReceiptError("synthetic authority failure")

        binding._pinned_identity = lambda provider, resolved: DIGEST
        receipts.LaunchIdentityReceiptStore.reserve = boom
        try:
            with self.assertRaises(HerdrSessionStartError) as ctx:
                self._reserve(_tagged_action())
        finally:
            binding._pinned_identity = real_identity
            receipts.LaunchIdentityReceiptStore.reserve = real_reserve
        self.assertIn("nothing was actuated", str(ctx.exception))

    def test_each_identity_reserve_row_has_its_own_immediate_effect_fence(self) -> None:
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_identity_binding as binding

        action_id = _tagged_two_bound_action()
        self.plans = [
            _Plan("codex", "mzb1_wA_codex_lane"),
            _Plan("claude", "mzb1_wA_claude_lane"),
        ]
        self.resolved["claude"] = SimpleNamespace(exec_target="/nowhere/claude")
        calls = 0

        def effect_fence():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("partition drift before second identity reserve")

        real = binding._pinned_identity
        binding._pinned_identity = lambda _provider, _resolved: DIGEST
        try:
            with self.assertRaisesRegex(RuntimeError, "second identity reserve"):
                self._reserve(action_id, effect_fence=effect_fence)
        finally:
            binding._pinned_identity = real

        store = LaunchIdentityReceiptStore(home=self.home)
        self.assertIsNotNone(
            store.read_receipt(
                GenerationKey(
                    "wA",
                    "issue_14741",
                    "codex",
                    "mzb1_wA_codex_lane",
                    action_id,
                )
            )
        )
        self.assertIsNone(
            store.read_receipt(
                GenerationKey(
                    "wA",
                    "issue_14741",
                    "claude",
                    "mzb1_wA_claude_lane",
                    action_id,
                )
            )
        )


class Bracket3FinalizeTest(unittest.TestCase):
    """Bracket 3: attest only against a declared lifecycle row and the composite proof."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.action = _tagged_action()
        self.key = GenerationKey(
            "wA", "issue_14741", "codex", "mzb1_wA_codex_lane", self.action
        )
        LaunchIdentityReceiptStore(home=self.home).reserve(
            self.key, identity_digest=DIGEST
        )
        self.result = SimpleNamespace(
            action_id=self.action,
            workspace_id="wA",
            lane_id="issue_14741",
            slots=[
                SimpleNamespace(
                    provider="codex",
                    assigned_name="mzb1_wA_codex_lane",
                    locator="wA:p1",
                )
            ],
        )

    def _finalize(self, **kw):
        finalize_lane_identity_receipts(store_home=self.home, result=self.result, **kw)

    def _phase(self):
        return LaunchIdentityReceiptStore(home=self.home).read_receipt(self.key).phase

    def test_without_the_launch_generation_proof_nothing_is_attested(self) -> None:
        """C13: an identity never becomes authority after its generation finalize failed."""
        self._finalize(lane_generation=self.action, lifecycle_revision="7")
        self.assertEqual(self._phase(), RECEIPT_UNBOUND_PENDING)

    def test_with_the_generation_proof_and_a_declared_revision_it_attests(self) -> None:
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_identity_binding as binding

        real = binding._generation_attested
        binding._generation_attested = lambda *a, **k: True
        try:
            self._finalize(lane_generation=self.action, lifecycle_revision="7")
        finally:
            binding._generation_attested = real
        receipt = LaunchIdentityReceiptStore(home=self.home).read_receipt(self.key)
        self.assertEqual(receipt.phase, RECEIPT_ATTESTED)
        self.assertEqual(receipt.lane_generation, self.action)
        self.assertEqual(receipt.lifecycle_revision, "7")
        self.assertEqual(receipt.locator, "wA:p1")

    def test_an_undeclared_lifecycle_revision_leaves_it_non_authority(self) -> None:
        """The whole reason bracket 3 exists: no declared revision, no attestation."""
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_identity_binding as binding

        real = binding._generation_attested
        binding._generation_attested = lambda *a, **k: True
        try:
            # No lifecycle store at all -> the readonly load yields nothing.
            self._finalize()
        finally:
            binding._generation_attested = real
        self.assertEqual(self._phase(), RECEIPT_UNBOUND_PENDING)

    def test_a_legacy_action_attests_nothing(self) -> None:
        self.result.action_id = _legacy_action()
        self._finalize(lane_generation="g", lifecycle_revision="7")
        self.assertEqual(self._phase(), RECEIPT_UNBOUND_PENDING)

    def test_a_slot_with_no_live_locator_is_skipped(self) -> None:
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_identity_binding as binding

        self.result.slots[0].locator = ""
        real = binding._generation_attested
        binding._generation_attested = lambda *a, **k: True
        try:
            self._finalize(lane_generation=self.action, lifecycle_revision="7")
        finally:
            binding._generation_attested = real
        self.assertEqual(self._phase(), RECEIPT_UNBOUND_PENDING)


if __name__ == "__main__":
    unittest.main()
