"""Onboarding regression guards (Redmine #13498 / #13503 / #13501).

- the #13379 home refusal policy is unchanged (bare `mozyo` still refuses home);
- a broken / unverifiable receipt and an unreadable config are preflight blocks;
- the bare-`mozyo` entry hook is NOT wired by this US (#13497 scope, review F5).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.application import cli as cli_module
from mozyo_bridge.application.launch_adoption_gate import adoption_refusal
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.inspect_usecase import (
    inspect_onboarding,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.preflight import (
    STATE_BLOCKED,
)

_SECRET = "regression-gate-secret"


class HomeRefusalUnchangedTests(unittest.TestCase):
    def test_launch_adoption_gate_still_refuses_home(self) -> None:
        home = Path("/home/someone")
        self.assertIsNotNone(adoption_refusal(home, ".mozyo-bridge/config.yaml", home=home))
        self.assertIsNotNone(adoption_refusal(home, None, home=home))

    def test_preflight_blocks_home_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            preflight = inspect_onboarding(
                home, home=home, sync_roots=(), gate_secret=_SECRET
            ).preflight
            self.assertEqual(preflight.state, STATE_BLOCKED)


class BrokenConfigAndReceiptFailClosedTests(unittest.TestCase):
    def _local_probe(self):
        from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.path_safety import (
            MOUNT_LOCAL,
            MountFacts,
        )

        class _Local:
            def classify_mount(self, path):
                return MountFacts(state=MOUNT_LOCAL)

        return _Local()

    def test_broken_config_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            (root / ".mozyo-bridge").mkdir(parents=True)
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "not: : valid: [\n", encoding="utf-8"
            )
            preflight = inspect_onboarding(
                root, home=Path(tmp) / "home", sync_roots=(),
                mount_probe=self._local_probe(), gate_secret=_SECRET,
            ).preflight
            self.assertEqual(preflight.state, STATE_BLOCKED)

    def test_unverifiable_receipt_is_blocked(self) -> None:
        # A hand-forged onboarding receipt with no valid signature blocks the root.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            (root / ".mozyo-bridge").mkdir(parents=True)
            (root / ".mozyo-bridge" / "onboarding-receipt.json").write_text(
                '{"version":1,"root_fingerprint":"x","plan_id":"y",'
                '"scaffold_preset":"none","rules_store":"central",'
                '"state":"adoption_in_progress","step_status":{},'
                '"failed_step":null,"failed_reason":null}',
                encoding="utf-8",
            )
            preflight = inspect_onboarding(
                root, home=Path(tmp) / "home", sync_roots=(),
                mount_probe=self._local_probe(), gate_secret=_SECRET,
            ).preflight
            self.assertEqual(preflight.state, STATE_BLOCKED)


class BareEntryScopeTests(unittest.TestCase):
    def test_bare_mozyo_does_not_wire_onboarding(self) -> None:
        # The bare entry hook belongs to #13497; the #13501 CLI must not import a
        # bare-launch onboarding hook into the launch path (review F5).
        import inspect as _inspect

        source = _inspect.getsource(cli_module.main)
        self.assertNotIn("maybe_resume_bare_mozyo", source)
        self.assertNotIn("f_170_conversational_onboarding", source)


if __name__ == "__main__":
    unittest.main()
