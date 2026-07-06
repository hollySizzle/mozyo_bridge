"""herdr real-send route-authority convergence tests (Redmine #13305).

Pins the #13305 j#73008 ruling: the real ``handoff send`` path resolves its herdr
target through the single backend-neutral route authority — lane-in-match
``(workspace_id, lane_id, role, pane_name)`` with a deterministically derived lane —
never the lane-less ``(workspace_id, role)`` projection.

Three things the design record requires are pinned here:

- **lane derivation precedence** (explicit > sender same-lane > coordinator default >
  legacy default), pinned tier-by-tier so a regression in the order is caught;
- **multi-lane resolution**: with the same role live in several lanes, an explicit /
  derived lane resolves the *right* one; a lane whose slot is not live fails closed
  with the #13302 ledger vocabulary and **never** falls back to an all-lane scan;
- **tmux byte-invariance characterization**: for the tmux backend the backend-neutral
  resolver picks the *same* pane id the existing ``pane_info`` send path would target
  (``resolve_route_neutral(tmux)`` == ``resolve_route``), so converging the herdr side
  onto the shared authority leaves the tmux side unchanged.

Hermetic: no live herdr, no tmux, no Redmine. herdr names are minted through the real
deterministic encoder; only neutral placeholder locators are used.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver import (  # noqa: E402
    BACKEND_TMUX,
    resolve_route_neutral,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (  # noqa: E402
    RESOLVE_OK,
    ROUTE_LOCATOR_MISSING,
    TARGET_AMBIGUOUS,
    TARGET_UNAVAILABLE,
    RouteIdentity,
    resolve_route,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_route_authority import (  # noqa: E402
    resolve_herdr_route_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E402
    LANE_BASIS_COORDINATOR_DEFAULT,
    LANE_BASIS_EXPLICIT,
    LANE_BASIS_LEGACY_DEFAULT,
    LANE_BASIS_SENDER_SAME_LANE,
    SenderIdentity,
    derive_target_lane,
)

WS = "ws-13305"


def _row(ws, role, lane, locator):
    return {"name": encode_assigned_name(ws, role, lane), "pane_id": locator}


class DeriveTargetLaneTest(unittest.TestCase):
    """Pin the lane-derivation precedence tier-by-tier (Redmine #13305 j#73008)."""

    def setUp(self) -> None:
        self.sublane_sender = SenderIdentity(workspace_id=WS, role="codex", lane_id="lane-1")
        self.default_sender = SenderIdentity(workspace_id=WS, role="codex", lane_id="default")

    def test_tier1_explicit_lane_wins_over_everything(self) -> None:
        # Explicit lane beats sender same-lane AND the coordinator default.
        d = derive_target_lane("codex", self.sublane_sender, explicit_lane="lane-9")
        self.assertEqual((d.lane, d.basis), ("lane-9", LANE_BASIS_EXPLICIT))
        d_coord = derive_target_lane("coordinator", self.sublane_sender, explicit_lane="lane-9")
        self.assertEqual((d_coord.lane, d_coord.basis), ("lane-9", LANE_BASIS_EXPLICIT))

    def test_tier2_peer_provider_is_sender_same_lane(self) -> None:
        for receiver in ("claude", "codex"):
            d = derive_target_lane(receiver, self.sublane_sender)
            self.assertEqual(
                (d.lane, d.basis), ("lane-1", LANE_BASIS_SENDER_SAME_LANE), msg=receiver
            )

    def test_tier3_coordinator_is_workspace_default(self) -> None:
        # A coordinator target is the workspace parent, never the sender's sublane.
        d = derive_target_lane("coordinator", self.sublane_sender)
        self.assertEqual((d.lane, d.basis), ("default", LANE_BASIS_COORDINATOR_DEFAULT))

    def test_tier4_peer_with_default_sender_is_legacy_default(self) -> None:
        d = derive_target_lane("claude", self.default_sender)
        self.assertEqual((d.lane, d.basis), ("default", LANE_BASIS_LEGACY_DEFAULT))


class ResolveHerdrRouteTargetTest(unittest.TestCase):
    """The real-send authority: lane-in-match resolution over live agent rows."""

    def setUp(self) -> None:
        # A codex gateway sender in lane-1 (the common coordinator->worker case).
        self.sender = SenderIdentity(workspace_id=WS, role="codex", lane_id="lane-1")

    def test_same_lane_worker_resolves(self) -> None:
        rows = [_row(WS, "claude", "lane-1", "wT:pT")]
        res = resolve_herdr_route_target(
            "claude", self.sender, rows, coordinator_provider="codex"
        )
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.status, RESOLVE_OK)
        self.assertEqual(res.locator, "wT:pT")
        self.assertEqual(res.lane, "lane-1")
        self.assertEqual(res.lane_basis, LANE_BASIS_SENDER_SAME_LANE)
        # The canonical assigned name is the ledger pane_name / durable label.
        self.assertEqual(res.assigned_name, encode_assigned_name(WS, "claude", "lane-1"))

    def test_coordinator_resolves_default_lane(self) -> None:
        # coordinator -> codex provider, default lane. A worker replying to the
        # workspace parent addresses it in `default`, not its own sublane.
        rows = [
            _row(WS, "codex", "default", "wC:pC"),
            _row(WS, "codex", "lane-1", "wS:pS"),  # the sublane gateway, NOT the target
        ]
        worker = SenderIdentity(workspace_id=WS, role="claude", lane_id="lane-1")
        res = resolve_herdr_route_target(
            "coordinator", worker, rows, coordinator_provider="codex"
        )
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.locator, "wC:pC")
        self.assertEqual((res.lane, res.lane_basis), ("default", LANE_BASIS_COORDINATOR_DEFAULT))

    def test_multi_lane_explicit_resolves_the_named_lane(self) -> None:
        # Same role live in two lanes: an explicit lane resolves the RIGHT one
        # instead of failing ambiguous (the whole point of lane-in-match).
        rows = [
            _row(WS, "codex", "lane-1", "w1:p1"),
            _row(WS, "codex", "lane-2", "w2:p2"),
        ]
        res = resolve_herdr_route_target(
            "codex", self.sender, rows, coordinator_provider="codex", explicit_lane="lane-2"
        )
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.locator, "w2:p2")
        self.assertEqual((res.lane, res.lane_basis), ("lane-2", LANE_BASIS_EXPLICIT))

    def test_multi_lane_derived_lane_resolves_uniquely_not_ambiguous(self) -> None:
        # Two claude workers live (lane-1, lane-2); the lane-1 sender derives lane-1
        # and resolves it uniquely — the OLD lane-less `(ws, role)` match would have
        # been ambiguous across the two.
        rows = [
            _row(WS, "claude", "lane-1", "w1:p1"),
            _row(WS, "claude", "lane-2", "w2:p2"),
        ]
        res = resolve_herdr_route_target(
            "claude", self.sender, rows, coordinator_provider="codex"
        )
        self.assertTrue(res.ok, msg=res.detail)
        self.assertEqual(res.locator, "w1:p1")

    def test_cross_lane_fails_closed_no_all_lane_scan(self) -> None:
        # The derived slot (lane-1) is not live; the only claude is in lane-x. The
        # authority must fail closed rather than scan all lanes for a claude.
        rows = [_row(WS, "claude", "lane-x", "wX:pX")]
        res = resolve_herdr_route_target(
            "claude", self.sender, rows, coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.status, TARGET_UNAVAILABLE)
        self.assertEqual(res.reason, TARGET_UNAVAILABLE)
        self.assertEqual(res.locator, "")

    def test_duplicate_slot_fails_closed_ambiguous(self) -> None:
        # Two live agents in the SAME slot (a herdr uniqueness violation) fail closed.
        rows = [
            _row(WS, "claude", "lane-1", "wA:pA"),
            _row(WS, "claude", "lane-1", "wB:pB"),
        ]
        res = resolve_herdr_route_target(
            "claude", self.sender, rows, coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.status, TARGET_AMBIGUOUS)

    def test_missing_locator_fails_closed(self) -> None:
        # The slot is live but its row carries no usable locator -> refuse a blank target.
        rows = [{"name": encode_assigned_name(WS, "claude", "lane-1")}]
        res = resolve_herdr_route_target(
            "claude", self.sender, rows, coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.status, ROUTE_LOCATOR_MISSING)

    def test_unknown_receiver_fails_before_resolution(self) -> None:
        res = resolve_herdr_route_target(
            "operator", self.sender, [], coordinator_provider="codex"
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "unknown_receiver")

    def test_coordinator_binding_unresolved_fails_closed(self) -> None:
        res = resolve_herdr_route_target(
            "coordinator", self.sender, [], coordinator_provider=None
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "coordinator_binding_unresolved")


class TmuxByteInvarianceCharacterizationTest(unittest.TestCase):
    """tmux path: the neutral resolver targets the same pane id as ``pane_info`` (#13305).

    The tmux send path resolves its target through ``pane_info`` and hands
    ``orchestrate_handoff`` the pane dict whose ``id`` is the send target. Converging
    the herdr side onto the backend-neutral authority must leave that tmux behaviour
    byte-invariant: for a managed tmux pane row, ``resolve_route_neutral(tmux)`` recovers
    exactly the same pane id, and is byte-for-byte the ledger's ``resolve_route``.
    """

    def test_tmux_neutral_resolution_matches_pane_info_target(self) -> None:
        # The `try_pane_lines` row shape a managed tmux gateway pane produces — its `id`
        # is what `pane_info` returns as the send target (`target_info["id"]`).
        pane_row = {
            "id": "%42",
            "workspace_id": WS,
            "lane_id": "lane-1",
            "agent_role": "codex",
            "route_label": "gw-codex-lane-1",
        }
        identity = RouteIdentity(
            workspace_id=WS,
            lane_id="lane-1",
            role="codex",
            pane_name="gw-codex-lane-1",
            route_id="r-42",
        )
        neutral = resolve_route_neutral(identity, [pane_row], backend=BACKEND_TMUX)
        self.assertEqual(neutral.status, RESOLVE_OK)
        # Same pane id the tmux `pane_info` send path would target.
        self.assertEqual(neutral.resolved_pane_id, pane_row["id"])
        # And byte-for-byte the ledger's own resolve_route (tmux 不変).
        direct = resolve_route(identity, [pane_row])
        self.assertEqual(neutral.status, direct.status)
        self.assertEqual(neutral.resolved_pane_id, direct.resolved_pane_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
