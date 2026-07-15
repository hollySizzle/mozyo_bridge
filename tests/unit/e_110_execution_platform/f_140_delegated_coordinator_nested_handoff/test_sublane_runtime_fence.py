"""Mutating-actuation runtime / placement-contract fence tests (Redmine #13705).

Pins the pure fence a mutating heal evaluates BEFORE any pane side effect: an
incompatible / unknown-provenance runtime — the source/installed skew that split
a #13411 lane's pair across tabs — fails closed, a compatible runtime proceeds,
and an already-split live pair is refused (a heal cannot repair a live split).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E501
    FENCE_OK,
    FENCE_PAIR_ALREADY_SPLIT,
    FENCE_PROVENANCE_UNKNOWN,
    FENCE_RUNTIME_LACKS_CONTRACT,
    PLACEMENT_CONTRACT_SAME_TAB_PAIR,
    RUNTIME_PLACEMENT_CAPABILITIES,
    RuntimePlacementFingerprint,
    evaluate_heal_runtime_fence,
    production_placement_fingerprint,
)


def _compatible(version="0.11.0"):
    return RuntimePlacementFingerprint(
        version=version, capabilities=RUNTIME_PLACEMENT_CAPABILITIES
    )


class HealRuntimeFenceTest(unittest.TestCase):
    def test_compatible_runtime_single_provider_heal_proceeds(self) -> None:
        # The ordinary heal: one live slot (existing_pair_colocated=None) + a runtime
        # that advertises the contract -> ok.
        verdict = evaluate_heal_runtime_fence(_compatible(), existing_pair_colocated=None)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.reason, FENCE_OK)

    def test_incompatible_older_runtime_fails_closed(self) -> None:
        # The measured #13705 incident: an installed 0.10.0 lacking the #13411 same-tab
        # contract heals a lane built under it -> incompatible, fail closed BEFORE any
        # side effect. The version is known, so it is the contract-lacking reason (not
        # provenance-unknown).
        old = RuntimePlacementFingerprint(version="0.10.0", capabilities=frozenset())
        verdict = evaluate_heal_runtime_fence(old)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, FENCE_RUNTIME_LACKS_CONTRACT)
        self.assertIn("0.10.0", verdict.detail)
        self.assertIn(PLACEMENT_CONTRACT_SAME_TAB_PAIR, verdict.detail)

    def test_unknown_provenance_runtime_fails_closed(self) -> None:
        # A runtime with no resolvable build version cannot attest its provenance.
        for version in ("", "   ", None):
            fp = RuntimePlacementFingerprint(
                version=version or "", capabilities=RUNTIME_PLACEMENT_CAPABILITIES
            )
            verdict = evaluate_heal_runtime_fence(fp)
            self.assertFalse(verdict.ok)
            self.assertEqual(verdict.reason, FENCE_PROVENANCE_UNKNOWN)

    def test_already_split_live_pair_is_refused(self) -> None:
        # Both slots live but not co-located: a heal cannot repair a live split.
        verdict = evaluate_heal_runtime_fence(
            _compatible(), existing_pair_colocated=False
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, FENCE_PAIR_ALREADY_SPLIT)

    def test_colocated_live_pair_proceeds(self) -> None:
        verdict = evaluate_heal_runtime_fence(
            _compatible(), existing_pair_colocated=True
        )
        self.assertTrue(verdict.ok)

    def test_provenance_precedes_capability_precedes_split(self) -> None:
        # Order: unknown provenance wins even when the pair is also split.
        blank = RuntimePlacementFingerprint(version="", capabilities=frozenset())
        self.assertEqual(
            evaluate_heal_runtime_fence(blank, existing_pair_colocated=False).reason,
            FENCE_PROVENANCE_UNKNOWN,
        )
        # Known version but no capability wins over the split check too.
        old = RuntimePlacementFingerprint(version="0.10.0", capabilities=frozenset())
        self.assertEqual(
            evaluate_heal_runtime_fence(old, existing_pair_colocated=False).reason,
            FENCE_RUNTIME_LACKS_CONTRACT,
        )

    def test_production_fingerprint_advertises_the_contract(self) -> None:
        fp = production_placement_fingerprint()
        self.assertIn(PLACEMENT_CONTRACT_SAME_TAB_PAIR, fp.capabilities)
        self.assertTrue(fp.version)  # the running build has a version
        self.assertTrue(evaluate_heal_runtime_fence(fp).ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
