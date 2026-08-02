"""C14: update evidence binds to the EXACT generation, never to a locator search (#14741).

The defect this closes (audit j#96966 C14): the receipt store keeps a row for every
generation, so searching IT by locator could attach a live update screen to a stale attested
row from an earlier generation that happened to reuse the pane. The fix is to ask a
different store — the launch-generation authority, where a new reservation atomically
supersedes the old row — not to search the same one more carefully.

These tests also fail on a HALF landing: the helper alone would leave the gate unbound
(`test_the_gate_is_wired_to_the_helper`), and the gate alone would have nothing to resolve
with (`test_the_helper_resolves_the_exact_generation`).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_launch_generation import (  # noqa: E402
    HerdrLaunchGenerationStore,
)
from mozyo_bridge.core.state.launch_identity_receipt import (  # noqa: E402
    EVIDENCE_BOUND,
    GenerationKey,
    LaunchIdentityReceiptStore,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.startup_admission_composition import (  # noqa: E402,E501
    record_update_evidence,
    resolve_generation_key,
)

DIGEST = "mzb1:" + "a" * 64
ACTION_OLD = "startup-" + "1" * 64
ACTION_NEW = "startup-" + "2" * 64
LOCATOR = "wA:p1"


def _generation(store, *, assigned, action_id, locator, role="codex"):
    """Reserve and attest one launch generation, the way the launcher does."""
    store.reserve_pending(
        assigned_name=assigned,
        startup_action_id=action_id,
        workspace_id="wA",
        role=role,
        lane_id="issue_14741",
    )
    store.finalize(
        assigned_name=assigned,
        startup_action_id=action_id,
        workspace_id="wA",
        role=role,
        lane_id="issue_14741",
        locator=locator,
        verdict="present",
        observed_at="2026-08-02T00:00:00.000000+00:00",
    )


class ExactGenerationResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.generations = HerdrLaunchGenerationStore(home=self.home)

    def test_the_helper_resolves_the_exact_generation(self) -> None:
        _generation(
            self.generations, assigned="mzb1_wA_codex_lane", action_id=ACTION_NEW,
            locator=LOCATOR,
        )
        key = resolve_generation_key("codex", LOCATOR, home=self.home)
        self.assertIsNotNone(key)
        self.assertEqual(key.startup_action_id, ACTION_NEW)
        self.assertEqual(key.assigned_name, "mzb1_wA_codex_lane")
        self.assertEqual(key.workspace_id, "wA")
        self.assertEqual(key.lane_id, "issue_14741")
        self.assertEqual(key.provider, "codex")

    def test_a_superseded_generation_is_not_the_answer(self) -> None:
        """A reused pane must resolve to the CURRENT generation, not a historical one."""
        _generation(
            self.generations, assigned="mzb1_wA_codex_lane", action_id=ACTION_OLD,
            locator=LOCATOR,
        )
        # The slot relaunches: the new reservation supersedes the old row atomically.
        _generation(
            self.generations, assigned="mzb1_wA_codex_lane", action_id=ACTION_NEW,
            locator=LOCATOR,
        )
        key = resolve_generation_key("codex", LOCATOR, home=self.home)
        self.assertEqual(key.startup_action_id, ACTION_NEW)

    def test_an_unresolvable_locator_is_none_not_a_guess(self) -> None:
        for label, args in (
            ("no generations at all", ("codex", LOCATOR)),
            ("blank locator", ("codex", "")),
            ("blank provider", ("", LOCATOR)),
        ):
            with self.subTest(label=label):
                self.assertIsNone(resolve_generation_key(*args, home=self.home))

    def test_a_different_provider_at_the_same_pane_does_not_match(self) -> None:
        _generation(
            self.generations, assigned="mzb1_wA_claude_lane", action_id=ACTION_NEW,
            locator=LOCATOR, role="claude",
        )
        self.assertIsNone(resolve_generation_key("codex", LOCATOR, home=self.home))

    def test_a_pending_generation_is_not_attested_enough_to_bind_to(self) -> None:
        self.generations.reserve_pending(
            assigned_name="mzb1_wA_codex_lane",
            startup_action_id=ACTION_NEW,
            workspace_id="wA",
            role="codex",
            lane_id="issue_14741",
        )
        self.assertIsNone(resolve_generation_key("codex", LOCATOR, home=self.home))


class EvidenceProductionTest(unittest.TestCase):
    """The producer binds on the exact key, and only for an update-derived screen."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.key = GenerationKey(
            "wA", "issue_14741", "codex", "mzb1_wA_codex_lane", ACTION_NEW
        )
        self.receipts = LaunchIdentityReceiptStore(home=self.home)
        self._patch = patch(
            "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application"
            ".startup_admission_composition.resolve_generation_key",
            lambda receiver, target, home=None: self.key if target == LOCATOR else None,
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._store = patch(
            "mozyo_bridge.core.state.launch_identity_receipt.launch_identity_receipt_path",
            lambda home=None: self.home / "launch-identity-receipt.sqlite",
        )
        self._store.start()
        self.addCleanup(self._store.stop)

    def _attested_receipt(self):
        self.receipts.reserve(self.key, identity_digest=DIGEST)
        self.receipts.finalize(
            self.key,
            identity_digest=DIGEST,
            locator=LOCATOR,
            lane_generation=ACTION_NEW,
            lifecycle_revision="7",
            composite_proof=True,
        )

    def _live(self):
        return self.receipts.read_bound_evidence(
            workspace_id="wA",
            lane_id="issue_14741",
            provider="codex",
            lane_generation=ACTION_NEW,
            lifecycle_revision="7",
        )

    def test_an_update_screen_binds_evidence_to_the_exact_generation(self) -> None:
        self._attested_receipt()
        record_update_evidence("codex", LOCATOR, "update_prompt_available")
        found = self._live()
        self.assertIsNotNone(found)
        self.assertEqual(found.phase, EVIDENCE_BOUND)
        self.assertEqual(found.key.startup_action_id, ACTION_NEW)
        self.assertEqual(found.blocker_id, "update_prompt_available")

    def test_a_non_update_screen_produces_no_evidence(self) -> None:
        """A trust or login prompt says nothing about which binary an update would reach."""
        self._attested_receipt()
        record_update_evidence("codex", LOCATOR, "trust_confirmation")
        self.assertIsNone(self._live())

    def test_an_unbound_provider_produces_no_evidence(self) -> None:
        self._attested_receipt()
        record_update_evidence("claude", LOCATOR, "update_prompt_available")
        self.assertIsNone(self._live())

    def test_an_unresolvable_generation_binds_nothing(self) -> None:
        self._attested_receipt()
        record_update_evidence("codex", "wA:pOTHER", "update_prompt_available")
        self.assertIsNone(self._live())

    def test_a_receipt_that_is_not_attested_binds_nothing(self) -> None:
        self.receipts.reserve(self.key, identity_digest=DIGEST)
        record_update_evidence("codex", LOCATOR, "update_prompt_available")
        self.assertIsNone(self._live())


class GateWiringTest(unittest.TestCase):
    """The gate half of the pair: a blocked admission must reach the producer."""

    def test_the_gate_is_wired_to_the_helper(self) -> None:
        """Guards against landing the helper without the call site (a dead-code half)."""
        import inspect

        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import (  # noqa: E501
            startup_admission_composition as composition,
            startup_admission_gate as gate,
        )

        self.assertIn(
            "on_startup_blocker",
            inspect.signature(gate.admit_receiver_startup_or_die).parameters,
            "the gate must accept the observation sink",
        )
        source = inspect.getsource(composition.admit_receiver_startup_or_die)
        self.assertIn(
            "record_update_evidence",
            source,
            "the composition must supply the producer as the gate's default sink",
        )

    def test_the_gate_hands_the_typed_observation_to_the_sink(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import (  # noqa: E501
            startup_admission_gate as gate,
        )

        seen = []
        with self.assertRaises(SystemExit):
            gate.admit_receiver_startup_or_die(
                herdr_send=True,
                receiver="codex",
                target=LOCATOR,
                read_lines=40,
                capture_pane=lambda *a, **k: (
                    "✨ Update available!  0.146.0 -> 99.0.0\n"
                    "› 1. Update now (runs `npm install -g @openai/codex`)\n"
                ),
                emit=lambda *a, **k: None,
                record_format="text",
                record_command=None,
                anchor=None,
                mode=None,
                kind=None,
                source=None,
                execution_root=None,
                on_startup_blocker=lambda *args: seen.append(args),
            )
        self.assertEqual(
            seen, [("codex", LOCATOR, "update_prompt_available")],
            "the sink receives the provider, the resolved target and the typed blocker id",
        )

    def test_the_sink_failure_is_not_swallowed(self) -> None:
        """C12/C14: no best-effort wrapper. A swallow that hides a failure hides anything."""
        from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import (  # noqa: E501
            startup_admission_gate as gate,
        )

        def boom(*args):
            raise RuntimeError("synthetic sink failure")

        with self.assertRaises(RuntimeError):
            gate.admit_receiver_startup_or_die(
                herdr_send=True,
                receiver="codex",
                target=LOCATOR,
                read_lines=40,
                capture_pane=lambda *a, **k: (
                    "✨ Update available!  0.146.0 -> 99.0.0\n"
                    "› 1. Update now (runs `npm install -g @openai/codex`)\n"
                ),
                emit=lambda *a, **k: None,
                record_format="text",
                record_command=None,
                anchor=None,
                mode=None,
                kind=None,
                source=None,
                execution_root=None,
                on_startup_blocker=boom,
            )


if __name__ == "__main__":
    unittest.main()
