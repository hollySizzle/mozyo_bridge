"""Main-lane implementation_request fail-closed predicate (Redmine #12441).

Implementation-shaped work defaults to a cockpit-visible sublane
(`vibes/docs/logics/coordinator-sublane-development-flow.md`); a direct
`handoff send --to claude --kind implementation_request` into the repo's
default/main-lane Claude is a process gap (#12438 j#63432/j#63434). These tests
pin the pure predicate `main_lane_implementation_request_blocked`, which decides
the cases the dispatch requires (main-lane Claude impl blocked; sublane Claude
impl, Codex gateway, normal-window Claude, role-mismatch pane, and
non-implementation main-lane notification all allowed) plus the
`--main-lane-exception` escape hatch.

The end-to-end orchestration wiring is exercised by the sibling integration test
`tests/integration/.../test_handoff_main_lane_guard.py`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: E402
    MAIN_LANE_ID,
    main_lane_implementation_request_blocked,
)


class MainLanePredicateTest(unittest.TestCase):
    def _blocked(self, **overrides) -> bool:
        kwargs = dict(
            receiver="claude",
            kind="implementation_request",
            target_lane_id="default",
            target_is_cockpit_pane=True,
            target_binds_claude=True,
            has_main_lane_exception=False,
        )
        kwargs.update(overrides)
        return main_lane_implementation_request_blocked(**kwargs)

    def test_cockpit_main_lane_claude_implementation_request_blocked(self) -> None:
        self.assertTrue(self._blocked())

    def test_empty_or_missing_lane_normalizes_to_main(self) -> None:
        for lane in (None, "", "  "):
            self.assertTrue(
                self._blocked(target_lane_id=lane),
                f"lane={lane!r} should normalize to {MAIN_LANE_ID}",
            )

    def test_sublane_claude_implementation_request_allowed(self) -> None:
        self.assertFalse(self._blocked(target_lane_id="lane-5ba25a56f773"))

    def test_normal_window_main_lane_not_blocked(self) -> None:
        # A plain unmanaged-repo Claude (normal_window) carries no sublane role,
        # so the cockpit/sublane guard does not apply.
        self.assertFalse(self._blocked(target_is_cockpit_pane=False))

    def test_pane_not_binding_claude_left_to_binding_gate(self) -> None:
        # A cockpit pane that does not strongly bind claude (e.g. marked codex)
        # is a role-mismatch for the binding gate, not a main-lane block.
        self.assertFalse(self._blocked(target_binds_claude=False))

    def test_codex_gateway_dispatch_allowed(self) -> None:
        self.assertFalse(self._blocked(receiver="codex"))

    def test_non_implementation_main_lane_notification_allowed(self) -> None:
        for kind in ("design_consultation", "custom", "reply", "review_request"):
            self.assertFalse(
                self._blocked(kind=kind),
                f"kind={kind} to main-lane Claude must not be blocked",
            )

    def test_explicit_exception_allows_main_lane(self) -> None:
        self.assertFalse(self._blocked(has_main_lane_exception=True))


if __name__ == "__main__":
    unittest.main()
