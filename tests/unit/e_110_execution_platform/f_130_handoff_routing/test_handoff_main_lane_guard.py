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
            target_binds_implementer=True,
            implementer_provider="claude",
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
        # A plain unmanaged-repo agent (normal_window) carries no sublane role,
        # so the cockpit/sublane guard does not apply.
        self.assertFalse(self._blocked(target_is_cockpit_pane=False))

    def test_pane_not_binding_implementer_left_to_binding_gate(self) -> None:
        # A cockpit pane that does not strongly bind the implementer provider
        # is a role-mismatch for the binding gate, not a main-lane block.
        self.assertFalse(self._blocked(target_binds_implementer=False))

    def test_codex_gateway_dispatch_allowed(self) -> None:
        # Under the default binding the implementer is `claude`; a dispatch to any
        # other provider (the gateway route) is not the guarded implementer send.
        self.assertFalse(self._blocked(receiver="codex"))

    def test_non_implementation_main_lane_notification_allowed(self) -> None:
        for kind in ("design_consultation", "custom", "reply", "review_request"):
            self.assertFalse(
                self._blocked(kind=kind),
                f"kind={kind} to main-lane Claude must not be blocked",
            )

    def test_explicit_exception_allows_main_lane(self) -> None:
        self.assertFalse(self._blocked(has_main_lane_exception=True))

    # --- Role-based rebind (Redmine #13174) -------------------------------------
    #
    # The guard reasons about the implementer *role*, whose runtime provider the
    # caller resolves from the binding. These cases pin that the predicate keys on
    # `implementer_provider`, not a hard-coded `claude`, so a rebind (e.g. a
    # coordinator-on-claude topology that moves the implementer to codex) neither
    # mis-blocks the non-implementer provider nor misses the real implementer pane.

    def test_rebound_implementer_provider_blocks_that_receiver(self) -> None:
        # implementer rebound to codex: an implementation_request to the main-lane
        # codex pane is now the guarded send and fails closed.
        self.assertTrue(
            self._blocked(receiver="codex", implementer_provider="codex")
        )

    def test_rebound_implementer_leaves_other_provider_unblocked(self) -> None:
        # With the implementer bound to codex, a `--to claude` send (claude is now
        # e.g. the coordinator seat, not the implementer) is NOT a main-lane block.
        self.assertFalse(
            self._blocked(receiver="claude", implementer_provider="codex")
        )

    def test_receiver_provider_mismatch_not_blocked(self) -> None:
        # A receiver that is not the resolved implementer provider is never guarded.
        self.assertFalse(
            self._blocked(receiver="claude", implementer_provider="codex")
        )
        self.assertFalse(
            self._blocked(receiver="codex", implementer_provider="claude")
        )

    def test_empty_implementer_provider_fails_open_not_crash(self) -> None:
        # Defensive: an empty resolved provider never keys the guard on `""` (the
        # boundary always resolves a real provider; this only pins the predicate).
        self.assertFalse(self._blocked(receiver="", implementer_provider=""))


if __name__ == "__main__":
    unittest.main()
