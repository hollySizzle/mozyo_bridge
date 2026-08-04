"""Regression for Redmine #14981: initial gateway worker dispatch routing.

The initial anchored implementation request uses the existing high-level same-lane
``sublane dispatch-worker`` rail.  It must not be mistaken for the separate
authorization-gated ``workflow step`` auto-dispatch leg.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    ROLE_PROFILE_VERSION,
    resolve_role_profile,
)


REQUIRED_ROUTE = (
    "mozyo-bridge sublane dispatch-worker --issue ISSUE --lane-label LANE "
    "--journal JOURNAL --execute"
)
AUTHORITY_BOUNDARY = (
    "authorization-gated な `workflow step` auto-dispatch leg へ置き換えず、"
    "authorization marker を推測生成しない"
)


class GatewayInitialDispatchProfileTest(unittest.TestCase):
    def test_packaged_gateway_profile_names_high_level_initial_route(self) -> None:
        profile = resolve_role_profile(
            "implementation_gateway",
            {
                "lane": "issue_14981_e2e",
                "durable_anchor": "redmine:issue=14981:journal=99244",
                "upstream_coordinator": "coordinator",
            },
        )
        self.assertEqual(ROLE_PROFILE_VERSION, "2026-08-04")
        self.assertIn(REQUIRED_ROUTE, profile.resolved_text)
        self.assertIn(AUTHORITY_BOUNDARY, profile.resolved_text)
        self.assertEqual(profile.unresolved_placeholders, ())

    def test_portable_skill_and_plugin_mirror_pin_the_same_route(self) -> None:
        paths = (
            ROOT / "skills/mozyo-bridge-agent/references/workflow.md",
            ROOT
            / "plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md",
        )
        texts = [path.read_text(encoding="utf-8") for path in paths]
        for text in texts:
            self.assertEqual(text.count(REQUIRED_ROUTE), 1)
            self.assertEqual(text.count(AUTHORITY_BOUNDARY), 1)
        self.assertEqual(texts[0], texts[1])


if __name__ == "__main__":
    unittest.main()
