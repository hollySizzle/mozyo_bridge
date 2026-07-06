"""Classical tests for the backend-neutral live resolver (#13297).

Hermetic, no-side-effect tests for the pure backend-neutral seam
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver`).
They pin three things the US 拘束 requires:

- **tmux 不変**: for the tmux backend, :func:`resolve_route_neutral` is
  byte-for-byte the existing :func:`resolve_route` — same status, same recovered
  pane id, same refreshed identity — across every outcome.
- **herdr resolve**: a herdr ``agent list`` inventory re-resolves through the
  same stable-identity matching, with the assigned name (not the tmux pane id) as
  the identity source and the transient locator as cache / evidence only.
- **fail-closed 曖昧性**: ambiguous, absent, stale-cache, and (herdr-only)
  missing-locator cases all fail closed with distinct diagnostics.

Hermetic by construction: no live tmux, no live herdr, no Redmine, no private
pane ids beyond neutral placeholders, no host paths, no cockpit composition.
Fixtures use neutral placeholder identifiers only; herdr names are minted through
the real deterministic encoder.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger import (  # noqa: E402
    RESOLVE_OK,
    ROUTE_LABEL_MISSING,
    ROUTE_LOCATOR_MISSING,
    TARGET_AMBIGUOUS,
    TARGET_STALE,
    TARGET_UNAVAILABLE,
    RouteIdentity,
    resolve_route,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver import (  # noqa: E402
    BACKEND_HERDR,
    BACKEND_TMUX,
    BackendNeutralResolverError,
    herdr_agent_to_pane_row,
    herdr_inventory,
    herdr_route_identity,
    neutral_inventory,
    resolve_route_neutral,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402
    encode_assigned_name,
)

WS = "ws-alpha"
LANE = "lane-1"


# ---------------------------------------------------------------------------
# tmux fixtures (the existing try_pane_lines row shape).
# ---------------------------------------------------------------------------
def _tmux_identity(
    *,
    role="claude",
    pane_name="ws-alpha/lane-1/claude",
    route_id="route-1",
    last_seen_pane_id="%10",
) -> RouteIdentity:
    return RouteIdentity(
        workspace_id=WS,
        lane_id=LANE,
        role=role,
        pane_name=pane_name,
        route_id=route_id,
        observed_at="2026-07-06T00:00:00Z",
        last_seen_pane_id=last_seen_pane_id,
    )


def _tmux_pane(
    *,
    pane_id,
    role="claude",
    route_label="ws-alpha/lane-1/claude",
    lane_id=LANE,
) -> dict[str, str]:
    return {
        "id": pane_id,
        "workspace_id": WS,
        "lane_id": lane_id,
        "agent_role": role,
        "route_label": route_label,
    }


# ---------------------------------------------------------------------------
# herdr fixtures (the agent list row shape).
# ---------------------------------------------------------------------------
def _herdr_row(*, workspace_id=WS, role="claude", lane_id=LANE, pane_id="w1:p1"):
    """A live herdr ``agent list`` row whose name is a canonical mzb1 name."""
    row = {"name": encode_assigned_name(workspace_id, role, lane_id)}
    if pane_id is not None:
        row["pane_id"] = pane_id
    return row


class TmuxBackendByteInvarianceTests(unittest.TestCase):
    """The tmux path must be byte-for-byte the existing resolve_route."""

    def _assert_same(self, identity, inventory):
        direct = resolve_route(identity, inventory)
        neutral = resolve_route_neutral(identity, inventory, backend=BACKEND_TMUX)
        self.assertEqual(neutral, direct)

    def test_resolve_ok_matches_direct(self):
        self._assert_same(_tmux_identity(), [_tmux_pane(pane_id="%10")])

    def test_moved_pane_recovered_matches_direct(self):
        # Cache says %10, live pane is %42 -> recovered, pane_id_refreshed True.
        identity = _tmux_identity(last_seen_pane_id="%10")
        result = resolve_route_neutral(
            identity, [_tmux_pane(pane_id="%42")], backend=BACKEND_TMUX
        )
        self.assertEqual(result.status, RESOLVE_OK)
        self.assertEqual(result.resolved_pane_id, "%42")
        self.assertTrue(result.pane_id_refreshed)
        self._assert_same(identity, [_tmux_pane(pane_id="%42")])

    def test_unavailable_matches_direct(self):
        self._assert_same(_tmux_identity(), [_tmux_pane(pane_id="%9", role="codex")])

    def test_ambiguous_matches_direct(self):
        self._assert_same(
            _tmux_identity(),
            [_tmux_pane(pane_id="%10"), _tmux_pane(pane_id="%11")],
        )

    def test_stale_matches_direct(self):
        # Cached %10 is live but now carries a different labelled identity.
        identity = _tmux_identity(last_seen_pane_id="%10")
        inventory = [_tmux_pane(pane_id="%10", route_label="ws-alpha/lane-1/other")]
        result = resolve_route_neutral(identity, inventory, backend=BACKEND_TMUX)
        self.assertEqual(result.status, TARGET_STALE)
        self._assert_same(identity, inventory)

    def test_label_missing_matches_direct(self):
        inventory = [_tmux_pane(pane_id="%10", route_label="")]
        result = resolve_route_neutral(
            _tmux_identity(), inventory, backend=BACKEND_TMUX
        )
        self.assertEqual(result.status, ROUTE_LABEL_MISSING)
        self._assert_same(_tmux_identity(), inventory)

    def test_tmux_never_emits_locator_missing(self):
        # A try_pane_lines row always carries a pane id, so the herdr-only
        # ROUTE_LOCATOR_MISSING is unreachable on the tmux path.
        result = resolve_route_neutral(
            _tmux_identity(), [_tmux_pane(pane_id="%10")], backend=BACKEND_TMUX
        )
        self.assertEqual(result.status, RESOLVE_OK)


class HerdrAdapterTests(unittest.TestCase):
    """The herdr agent-list -> ledger-row adapter."""

    def test_decodes_slot_and_carries_locator(self):
        row = herdr_agent_to_pane_row(_herdr_row(pane_id="w1:p7"))
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["workspace_id"], WS)
        self.assertEqual(row["lane_id"], LANE)
        self.assertEqual(row["agent_role"], "claude")
        # Identity source is the decoded name, not the locator.
        self.assertEqual(row["route_label"], encode_assigned_name(WS, "claude", LANE))
        # Locator is carried as the transient cache id.
        self.assertEqual(row["id"], "w1:p7")

    def test_foreign_agent_dropped(self):
        self.assertIsNone(herdr_agent_to_pane_row({"name": "poc_claude"}))
        self.assertIsNone(herdr_agent_to_pane_row({"name": ""}))
        self.assertIsNone(herdr_agent_to_pane_row({"not_name": "x"}))

    def test_inventory_filters_foreign(self):
        rows = herdr_inventory(
            [_herdr_row(), {"name": "some_foreign_agent"}, _herdr_row(role="codex")]
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {r["agent_role"] for r in rows}, {"claude", "codex"}
        )

    def test_locator_aliases(self):
        # `pane` and `location` are accepted locator aliases (no pane_id key).
        row = herdr_agent_to_pane_row(
            {"name": encode_assigned_name(WS, "claude", LANE), "pane": "w2:p1"}
        )
        assert row is not None
        self.assertEqual(row["id"], "w2:p1")


class HerdrRouteIdentityTests(unittest.TestCase):
    """The herdr slot -> RouteIdentity bridge."""

    def test_pane_name_is_canonical_assigned_name(self):
        identity = herdr_route_identity(
            workspace_id=WS, role="claude", lane_id=LANE, route_id="route-h"
        )
        self.assertEqual(identity.pane_name, encode_assigned_name(WS, "claude", LANE))
        self.assertEqual(identity.identity_key, (WS, LANE, "claude", identity.pane_name))

    def test_empty_lane_defaults(self):
        identity = herdr_route_identity(
            workspace_id=WS, role="codex", route_id="route-h"
        )
        self.assertEqual(identity.lane_id, "default")
        self.assertEqual(
            identity.pane_name, encode_assigned_name(WS, "codex", "default")
        )

    def test_missing_required_component_fails_closed(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            HerdrIdentityError,
        )

        with self.assertRaises(HerdrIdentityError):
            herdr_route_identity(workspace_id="", role="claude", route_id="route-h")


class HerdrResolveTests(unittest.TestCase):
    """Backend-neutral resolution against a live herdr inventory."""

    def _identity(self, *, role="claude", last_seen_locator=""):
        return herdr_route_identity(
            workspace_id=WS,
            role=role,
            lane_id=LANE,
            route_id="route-h",
            observed_at="2026-07-06T00:00:00Z",
            last_seen_locator=last_seen_locator,
        )

    def test_single_match_resolves(self):
        result = resolve_route_neutral(
            self._identity(), [_herdr_row(pane_id="w1:p1")], backend=BACKEND_HERDR
        )
        self.assertEqual(result.status, RESOLVE_OK)
        self.assertEqual(result.resolved_pane_id, "w1:p1")

    def test_locator_is_evidence_not_authority(self):
        # Cached locator is stale (w1:p1); the live agent now sits at w9:p9. The
        # stable assigned-name identity still resolves and the pane id is
        # transparently refreshed — the cache never blocked the resolve.
        identity = self._identity(last_seen_locator="w1:p1")
        result = resolve_route_neutral(
            identity, [_herdr_row(pane_id="w9:p9")], backend=BACKEND_HERDR
        )
        self.assertEqual(result.status, RESOLVE_OK)
        self.assertEqual(result.resolved_pane_id, "w9:p9")
        self.assertTrue(result.pane_id_refreshed)

    def test_no_match_unavailable(self):
        # Only a codex agent is live; the claude route has no match.
        result = resolve_route_neutral(
            self._identity(role="claude"),
            [_herdr_row(role="codex")],
            backend=BACKEND_HERDR,
        )
        self.assertEqual(result.status, TARGET_UNAVAILABLE)
        self.assertEqual(result.resolved_pane_id, "")

    def test_other_workspace_unavailable(self):
        result = resolve_route_neutral(
            self._identity(),
            [_herdr_row(workspace_id="ws-beta")],
            backend=BACKEND_HERDR,
        )
        self.assertEqual(result.status, TARGET_UNAVAILABLE)

    def test_duplicate_slot_ambiguous(self):
        # Two live agents decode to the same slot -> refuse to guess.
        result = resolve_route_neutral(
            self._identity(),
            [_herdr_row(pane_id="w1:p1"), _herdr_row(pane_id="w2:p2")],
            backend=BACKEND_HERDR,
        )
        self.assertEqual(result.status, TARGET_AMBIGUOUS)
        self.assertEqual(result.resolved_pane_id, "")

    def test_matched_but_no_locator_is_locator_missing(self):
        # A decoded agent whose row carries no usable locator must fail closed,
        # not resolve to a blank target (parity with herdr rebind_missing_locator).
        result = resolve_route_neutral(
            self._identity(last_seen_locator="w1:p1"),
            [_herdr_row(pane_id=None)],
            backend=BACKEND_HERDR,
        )
        self.assertEqual(result.status, ROUTE_LOCATOR_MISSING)
        self.assertEqual(result.resolved_pane_id, "")
        self.assertTrue(result.is_fail_closed)
        # The cache must not advance to a blank locator.
        self.assertIsNotNone(result.identity)
        assert result.identity is not None
        self.assertEqual(result.identity.last_seen_pane_id, "w1:p1")

    def test_foreign_agents_ignored_in_resolution(self):
        result = resolve_route_neutral(
            self._identity(),
            [{"name": "foreign_thing"}, _herdr_row(pane_id="w1:p1")],
            backend=BACKEND_HERDR,
        )
        self.assertEqual(result.status, RESOLVE_OK)
        self.assertEqual(result.resolved_pane_id, "w1:p1")


class NeutralInventoryTests(unittest.TestCase):
    def test_tmux_passthrough(self):
        rows = neutral_inventory([_tmux_pane(pane_id="%10")], backend=BACKEND_TMUX)
        self.assertEqual(rows, [_tmux_pane(pane_id="%10")])

    def test_herdr_adapted(self):
        rows = neutral_inventory([_herdr_row(pane_id="w1:p1")], backend=BACKEND_HERDR)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route_label"], encode_assigned_name(WS, "claude", LANE))

    def test_unsupported_backend_fails_closed(self):
        with self.assertRaises(BackendNeutralResolverError):
            neutral_inventory([], backend="ssh")

    def test_unsupported_backend_via_resolve_fails_closed(self):
        with self.assertRaises(BackendNeutralResolverError):
            resolve_route_neutral(_tmux_identity(), [], backend="ssh")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
