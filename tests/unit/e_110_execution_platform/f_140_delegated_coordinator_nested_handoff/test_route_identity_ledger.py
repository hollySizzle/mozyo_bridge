"""Classical tests for the route identity ledger / live re-resolution (#12553).

These are hermetic, no-side-effect tests for the pure ledger seam
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger`). They pin the contract that a
``pane_id`` is a cache/snapshot only and is never the route authority: every
resolution re-matches a live inventory snapshot against the stable identity
tuple ``(workspace_id, lane_id, role, pane_name)`` and fails closed — with
distinct diagnostics — when that match is not unique.

Hermetic by construction: no live tmux, no Redmine, no private pane ids beyond
neutral ``%N`` placeholders, no host paths, no cockpit composition, no private
project names. Fixtures use neutral placeholder identifiers only.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_route_planner import (  # noqa: E402
    DelegationRoutePlanError,
    TARGET_CHILD_GATEWAY,
    TARGET_SAME_LANE_WORKER,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (  # noqa: E402
    DEFAULT_LANE,
    RESOLVE_OK,
    ROUTE_LABEL_MISSING,
    TARGET_AMBIGUOUS,
    TARGET_STALE,
    TARGET_UNAVAILABLE,
    PaneObservation,
    RouteIdentity,
    RouteIdentityError,
    RouteIdentityLedger,
    observe_pane,
    resolve_for_route_target,
    resolve_route,
)


def _identity(
    *,
    workspace_id="ws-alpha",
    lane_id="lane-1",
    role="claude",
    pane_name="ws-alpha/lane-1/claude",
    route_id="route-1",
    observed_at="2026-06-25T00:00:00Z",
    last_seen_pane_id="%10",
) -> RouteIdentity:
    return RouteIdentity(
        workspace_id=workspace_id,
        lane_id=lane_id,
        role=role,
        pane_name=pane_name,
        route_id=route_id,
        observed_at=observed_at,
        last_seen_pane_id=last_seen_pane_id,
    )


def _pane(
    *,
    pane_id,
    workspace_id="ws-alpha",
    lane_id="lane-1",
    agent_role="claude",
    route_label="ws-alpha/lane-1/claude",
) -> dict[str, str]:
    """A live ``try_pane_lines`` row shape."""
    return {
        "id": pane_id,
        "workspace_id": workspace_id,
        "lane_id": lane_id,
        "agent_role": agent_role,
        "route_label": route_label,
    }


class RouteIdentityConstructionTest(unittest.TestCase):
    def test_stable_fields_required(self):
        for missing in ("workspace_id", "role", "pane_name", "route_id"):
            with self.subTest(missing=missing):
                kwargs = dict(
                    workspace_id="ws",
                    lane_id="lane-1",
                    role="claude",
                    pane_name="pn",
                    route_id="r",
                )
                kwargs[missing] = "   "
                with self.assertRaises(RouteIdentityError):
                    RouteIdentity(**kwargs)

    def test_pane_id_may_be_empty(self):
        # last_seen_pane_id is cache-only and never required.
        ident = _identity(last_seen_pane_id="")
        self.assertEqual(ident.last_seen_pane_id, "")

    def test_empty_lane_normalizes_to_default(self):
        ident = _identity(lane_id="")
        self.assertEqual(ident.lane_id, DEFAULT_LANE)
        self.assertEqual(ident.lane_role_key, ("ws-alpha", DEFAULT_LANE, "claude"))

    def test_fields_are_trimmed(self):
        ident = _identity(workspace_id="  ws-alpha  ", route_id=" route-1 ")
        self.assertEqual(ident.workspace_id, "ws-alpha")
        self.assertEqual(ident.route_id, "route-1")

    def test_record_round_trip(self):
        ident = _identity()
        rebuilt = RouteIdentity.from_record(ident.to_record())
        self.assertEqual(rebuilt, ident)

    def test_public_pointer_omits_pane_id(self):
        ident = _identity(last_seen_pane_id="%4242")
        pointer = ident.public_pointer()
        self.assertNotIn("%4242", pointer)
        self.assertIn("route-1", pointer)
        self.assertIn("ws-alpha", pointer)


class ObservePaneTest(unittest.TestCase):
    def test_route_label_alias_pane_name(self):
        obs = observe_pane(
            {
                "id": "%3",
                "workspace_id": "ws",
                "lane_id": "lane-1",
                "agent_role": "codex",
                "pane_name": "label-via-alias",
            }
        )
        self.assertEqual(obs.pane_name, "label-via-alias")
        self.assertTrue(obs.has_route_label)

    def test_missing_label_has_no_route_label(self):
        obs = observe_pane({"id": "%3", "workspace_id": "ws", "agent_role": "claude"})
        self.assertFalse(obs.has_route_label)
        self.assertEqual(obs.lane_id, DEFAULT_LANE)


class ResolveRouteTest(unittest.TestCase):
    def test_successful_resolution_through_stable_identity(self):
        ident = _identity(last_seen_pane_id="%10")
        resolution = resolve_route(ident, [_pane(pane_id="%10")])
        self.assertEqual(resolution.status, RESOLVE_OK)
        self.assertTrue(resolution.is_resolved)
        self.assertEqual(resolution.resolved_pane_id, "%10")
        self.assertFalse(resolution.pane_id_refreshed)

    def test_moved_pane_is_recovered_and_refreshed(self):
        # The cached snapshot pane (%10) is gone; the same stable identity now
        # lives on %20. Re-resolution must recover it, not fail.
        ident = _identity(last_seen_pane_id="%10")
        resolution = resolve_route(ident, [_pane(pane_id="%20")])
        self.assertEqual(resolution.status, RESOLVE_OK)
        self.assertEqual(resolution.resolved_pane_id, "%20")
        self.assertTrue(resolution.pane_id_refreshed)
        self.assertIsNotNone(resolution.identity)
        self.assertEqual(resolution.identity.last_seen_pane_id, "%20")

    def test_zero_matches_is_target_unavailable(self):
        ident = _identity()
        # A pane in a different lane never matches.
        resolution = resolve_route(ident, [_pane(pane_id="%9", lane_id="lane-other")])
        self.assertEqual(resolution.status, TARGET_UNAVAILABLE)
        self.assertTrue(resolution.is_fail_closed)
        self.assertEqual(resolution.resolved_pane_id, "")

    def test_empty_inventory_is_target_unavailable(self):
        self.assertEqual(resolve_route(_identity(), []).status, TARGET_UNAVAILABLE)

    def test_multiple_matches_is_target_ambiguous(self):
        ident = _identity()
        resolution = resolve_route(
            ident, [_pane(pane_id="%10"), _pane(pane_id="%11")]
        )
        self.assertEqual(resolution.status, TARGET_AMBIGUOUS)
        self.assertEqual(resolution.resolved_pane_id, "")

    def test_stale_cached_pane_id_now_other_identity(self):
        # The cached pane id (%10) is still live, but it now carries a different
        # identity (a different lane). Trusting the snapshot would mis-route.
        ident = _identity(last_seen_pane_id="%10")
        inventory = [_pane(pane_id="%10", lane_id="lane-foreign")]
        resolution = resolve_route(ident, inventory)
        self.assertEqual(resolution.status, TARGET_STALE)
        self.assertEqual(resolution.resolved_pane_id, "")

    def test_stale_takes_priority_over_unavailable(self):
        # No identity match anywhere, but the cached pane id is live under a
        # different identity -> stale (the more specific, dangerous signal).
        ident = _identity(last_seen_pane_id="%10")
        inventory = [
            _pane(pane_id="%10", agent_role="codex", route_label="other"),
            _pane(pane_id="%30", lane_id="lane-zzz"),
        ]
        self.assertEqual(resolve_route(ident, inventory).status, TARGET_STALE)

    def test_missing_route_label_does_not_fall_back_to_pane_id(self):
        # A lane/role pane exists at the cached pane id, but it carries no stable
        # route label. It must fail closed, never resolve by pane id.
        ident = _identity(last_seen_pane_id="%10")
        inventory = [_pane(pane_id="%10", route_label="")]
        resolution = resolve_route(ident, inventory)
        self.assertEqual(resolution.status, ROUTE_LABEL_MISSING)
        self.assertEqual(resolution.resolved_pane_id, "")

    def test_labeled_but_different_name_is_unavailable_not_label_missing(self):
        ident = _identity()
        inventory = [_pane(pane_id="%77", route_label="some-other-pane")]
        self.assertEqual(resolve_route(ident, inventory).status, TARGET_UNAVAILABLE)

    def test_considered_counts_lane_role_candidates(self):
        ident = _identity()
        inventory = [
            _pane(pane_id="%10"),
            _pane(pane_id="%11", route_label="other-label"),
            _pane(pane_id="%12", lane_id="lane-other"),  # different slot
        ]
        # %10 and %11 share the lane/role slot; %12 does not.
        resolution = resolve_route(ident, inventory)
        self.assertEqual(resolution.considered, 2)


class ResolveForRouteTargetTest(unittest.TestCase):
    def test_same_lane_worker_cross_project_is_rejected(self):
        ident = _identity(role="claude")
        with self.assertRaises(DelegationRoutePlanError):
            resolve_for_route_target(
                TARGET_SAME_LANE_WORKER,
                ident,
                [_pane(pane_id="%10")],
                cross_project=True,
            )

    def test_same_lane_worker_same_project_resolves(self):
        ident = _identity(role="claude")
        resolution = resolve_for_route_target(
            TARGET_SAME_LANE_WORKER, ident, [_pane(pane_id="%10")], cross_project=False
        )
        self.assertEqual(resolution.status, RESOLVE_OK)

    def test_role_mismatch_is_rejected(self):
        # A gateway token must resolve to a Codex pane; a Claude identity here is
        # a malformed re-resolution request.
        ident = _identity(role="claude")
        with self.assertRaises(DelegationRoutePlanError):
            resolve_for_route_target(
                TARGET_CHILD_GATEWAY, ident, [_pane(pane_id="%10")]
            )

    def test_gateway_resolves_codex_identity(self):
        ident = _identity(
            role="codex",
            pane_name="ws-alpha/lane-1/codex",
            last_seen_pane_id="%5",
        )
        inventory = [
            _pane(
                pane_id="%5",
                agent_role="codex",
                route_label="ws-alpha/lane-1/codex",
            )
        ]
        resolution = resolve_for_route_target(TARGET_CHILD_GATEWAY, ident, inventory)
        self.assertEqual(resolution.status, RESOLVE_OK)
        self.assertEqual(resolution.resolved_pane_id, "%5")

    def test_unknown_token_is_rejected(self):
        with self.assertRaises(DelegationRoutePlanError):
            resolve_for_route_target("not_a_target", _identity(), [])


class RouteIdentityLedgerTest(unittest.TestCase):
    def test_record_get_remove(self):
        ledger = RouteIdentityLedger()
        ident = _identity()
        ledger.record(ident)
        self.assertEqual(ledger.get("route-1"), ident)
        self.assertEqual(ledger.identities(), (ident,))
        ledger.remove("route-1")
        self.assertIsNone(ledger.get("route-1"))

    def test_unknown_route_id_is_target_unavailable(self):
        resolution = RouteIdentityLedger().resolve("missing", [_pane(pane_id="%10")])
        self.assertEqual(resolution.status, TARGET_UNAVAILABLE)

    def test_resolve_delegates_to_live_inventory(self):
        ledger = RouteIdentityLedger()
        ledger.record(_identity(last_seen_pane_id="%10"))
        self.assertEqual(
            ledger.resolve("route-1", [_pane(pane_id="%10")]).status, RESOLVE_OK
        )

    def test_refresh_persists_moved_pane(self):
        ledger = RouteIdentityLedger()
        ledger.record(_identity(last_seen_pane_id="%10"))
        resolution = ledger.refresh("route-1", [_pane(pane_id="%20")])
        self.assertEqual(resolution.status, RESOLVE_OK)
        # The stored cache advanced to the recovered pane id.
        self.assertEqual(ledger.get("route-1").last_seen_pane_id, "%20")

    def test_refresh_leaves_identity_untouched_on_fail_closed(self):
        ledger = RouteIdentityLedger()
        ledger.record(_identity(last_seen_pane_id="%10"))
        ledger.refresh("route-1", [])  # unavailable
        self.assertEqual(ledger.get("route-1").last_seen_pane_id, "%10")

    def test_records_round_trip(self):
        ledger = RouteIdentityLedger()
        ledger.record(_identity(route_id="route-1"))
        ledger.record(_identity(route_id="route-2", role="codex", pane_name="cdx"))
        rebuilt = RouteIdentityLedger.from_records(ledger.to_records())
        self.assertEqual(rebuilt.identities(), ledger.identities())


if __name__ == "__main__":
    unittest.main()
