"""Onboarding regression guards (Redmine #13498 / #13503).

- the #13379 home refusal policy is unchanged (bare `mozyo` still refuses home);
- a broken existing config is a preflight hard block (fail-closed);
- the bare-`mozyo` reroute hook never intercepts a fully adopted or an
  unadopted root (only adoption_in_progress), so the adopted launch path and the
  existing adoption gate are untouched.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.application.launch_adoption_gate import adoption_refusal
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.commands_onboarding import (
    maybe_resume_bare_mozyo,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.inspect_usecase import (
    inspect_onboarding,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.preflight import (
    STATE_BLOCKED,
)


class HomeRefusalUnchangedTests(unittest.TestCase):
    def test_launch_adoption_gate_still_refuses_home(self) -> None:
        # #13379 policy is untouched by the onboarding work: home is refused even
        # with a marker.
        home = Path("/home/someone")
        self.assertIsNotNone(adoption_refusal(home, ".mozyo-bridge/config.yaml", home=home))
        self.assertIsNotNone(adoption_refusal(home, None, home=home))

    def test_preflight_blocks_home_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            preflight = inspect_onboarding(home, home=home, sync_roots=()).preflight
            self.assertEqual(preflight.state, STATE_BLOCKED)


class BrokenConfigFailClosedTests(unittest.TestCase):
    def test_broken_config_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            (root / ".mozyo-bridge").mkdir(parents=True)
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "not: : valid: [\n", encoding="utf-8"
            )
            preflight = inspect_onboarding(
                root, home=Path(tmp) / "home", sync_roots=()
            ).preflight
            self.assertEqual(preflight.state, STATE_BLOCKED)


class BareMozyoRerouteScopeTests(unittest.TestCase):
    def _hook_returns_none_in(self, root: Path) -> None:
        cwd = os.getcwd()
        try:
            os.chdir(root)
            import argparse

            self.assertIsNone(maybe_resume_bare_mozyo(argparse.Namespace(json=False)))
        finally:
            os.chdir(cwd)

    def test_reroute_ignores_unadopted_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            self._hook_returns_none_in(root)

    def test_reroute_ignores_adopted_root(self) -> None:
        # A hand-adopted (config marker, no in-progress receipt) root must not be
        # intercepted — the adopted launch path stays intact.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            (root / ".mozyo-bridge").mkdir(parents=True)
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "terminal_transport:\n  backend: herdr\n", encoding="utf-8"
            )
            self._hook_returns_none_in(root)


if __name__ == "__main__":
    unittest.main()
