"""Correlated review_result return — application wiring (Redmine #13684).

The owning-lane binding reader, the independent live-generation reader, and the resolver's
live-generation seam, exercised against a fake lifecycle store so the fail-closed wiring is pinned
without a live registry / Herdr:

- ``owning_lane_binding`` resolves the target lane / generation / gateway receiver from the durable
  owning-lane binding (fail-closed on absent / ambiguous / unreadable);
- ``owning_lane_generation_reader`` is the send-time INDEPENDENT live authority: it yields the live
  owning-lane revision only for a ``review_return`` row whose recorded lane is still the current
  owner — an owner switch (supersession) or any other route yields blank (-> generation mismatch ->
  zero-send), never a copy of the row's expected value (correction 1).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.callback_outbox import CallbackOutboxRow
from mozyo_bridge.core.state.lane_lifecycle_model import (
    OWNER_ABSENT,
    OWNER_AMBIGUOUS,
    OWNER_RESOLVED,
    OwnerResolution,
)
from mozyo_bridge.core.state.workflow_runtime_store import CALLBACK_INFLIGHT
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.background_service_sender import (
    BackendNeutralTargetResolver,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (
    owning_lane_binding,
    owning_lane_generation_reader,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    RoleProviderBinding,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.review_return_route import (
    review_return_callback_route,
)

WS = "wsA"
LANE = "issue_13684"


class _Rec:
    def __init__(self, revision: int) -> None:
        self.revision = revision


class _FakeLifecycle:
    """A fake owning-lane store: ``owner_by_issue`` + ``revision_by_lane`` drive the two reads."""

    def __init__(self, *, owner_by_issue=None, revision_by_lane=None, raise_on=None) -> None:
        self.owner_by_issue = owner_by_issue or {}
        self.revision_by_lane = revision_by_lane or {}
        self.raise_on = raise_on or set()

    def resolve_owner(self, ws, issue):
        if "resolve_owner" in self.raise_on:
            raise RuntimeError("unreadable")
        got = self.owner_by_issue.get((ws, issue))
        if got is None:
            return OwnerResolution(status=OWNER_ABSENT)
        return got

    def get(self, key):
        if "get" in self.raise_on:
            raise RuntimeError("unreadable")
        rev = self.revision_by_lane.get((key.repo_workspace_id, key.lane_id))
        return _Rec(rev) if rev is not None else None


def _row(*, route: str, target_lane: str = LANE, issue: str = "13684", generation: str = "1"):
    return CallbackOutboxRow(
        source="redmine", issue=issue, journal="20", normalized_gate="review",
        callback_route=route, state=CALLBACK_INFLIGHT, attempts=0, max_attempts=3,
        send_attempted=True, notification_kind="review_result", notification_summary="",
        gate_mismatch=False, detail="", payload="", claim_token="tok", workspace_id=WS,
        target_lane=target_lane, target_receiver="codex", target_generation=generation,
    )


class OwningLaneBindingTest(unittest.TestCase):
    def test_resolved_owner_yields_lane_generation_and_gateway_receiver(self) -> None:
        life = _FakeLifecycle(
            owner_by_issue={(WS, "13684"): OwnerResolution(status=OWNER_RESOLVED, lane_id=LANE)},
            revision_by_lane={(WS, LANE): 4},
        )
        binding = owning_lane_binding(WS, "13684", RoleProviderBinding.default(), lifecycle_store=life)
        self.assertTrue(binding.resolved)
        self.assertEqual(binding.lane_id, LANE)
        self.assertEqual(binding.generation, "4")
        self.assertEqual(binding.gateway_receiver, "codex")  # project_gateway -> codex

    def test_absent_owner_is_not_resolved(self) -> None:
        life = _FakeLifecycle()
        binding = owning_lane_binding(WS, "13684", RoleProviderBinding.default(), lifecycle_store=life)
        self.assertFalse(binding.resolved)
        self.assertEqual(binding.status, OWNER_ABSENT)

    def test_ambiguous_owner_is_surfaced(self) -> None:
        life = _FakeLifecycle(
            owner_by_issue={(WS, "13684"): OwnerResolution(status=OWNER_AMBIGUOUS)}
        )
        binding = owning_lane_binding(WS, "13684", RoleProviderBinding.default(), lifecycle_store=life)
        self.assertFalse(binding.resolved)
        self.assertEqual(binding.status, OWNER_AMBIGUOUS)

    def test_unreadable_store_fails_closed_unknown(self) -> None:
        life = _FakeLifecycle(raise_on={"resolve_owner"})
        binding = owning_lane_binding(WS, "13684", RoleProviderBinding.default(), lifecycle_store=life)
        self.assertFalse(binding.resolved)


class OwningLaneGenerationReaderTest(unittest.TestCase):
    def _life(self, owner_lane=LANE, revision=1):
        return _FakeLifecycle(
            owner_by_issue={(WS, "13684"): OwnerResolution(status=OWNER_RESOLVED, lane_id=owner_lane)},
            revision_by_lane={(WS, owner_lane): revision},
        )

    def test_live_generation_for_current_owner_return_row(self) -> None:
        read = owning_lane_generation_reader(WS, lifecycle_store=self._life(revision=7))
        row = _row(route=review_return_callback_route(LANE))
        self.assertEqual(read(row), "7")

    def test_owner_switched_lane_yields_blank(self) -> None:
        # A supersession switched the active owner to a different lane; the row's recorded lane is
        # no longer the owner -> blank live generation -> zero-send.
        read = owning_lane_generation_reader(WS, lifecycle_store=self._life(owner_lane="recovery_x"))
        row = _row(route=review_return_callback_route(LANE))  # still recorded as LANE
        self.assertEqual(read(row), "")

    def test_non_return_route_yields_blank(self) -> None:
        read = owning_lane_generation_reader(WS, lifecycle_store=self._life())
        self.assertEqual(read(_row(route="coordinator")), "")

    def test_absent_owner_yields_blank(self) -> None:
        read = owning_lane_generation_reader(WS, lifecycle_store=_FakeLifecycle())
        self.assertEqual(read(_row(route=review_return_callback_route(LANE))), "")

    def test_unreadable_store_yields_blank(self) -> None:
        read = owning_lane_generation_reader(WS, lifecycle_store=_FakeLifecycle(raise_on={"resolve_owner"}))
        self.assertEqual(read(_row(route=review_return_callback_route(LANE))), "")


class ResolverLiveGenerationTest(unittest.TestCase):
    """The resolver reads the DeliveryTarget generation from the injected live authority, never the row."""

    def _inventory(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
            encode_assigned_name,
        )

        row = {"name": encode_assigned_name(WS, "codex", LANE), "pane_id": "%gw"}
        return lambda: ([row], "herdr")

    def test_generation_comes_from_live_authority_not_the_row(self) -> None:
        # The row claims generation "1"; the live authority says "9". The resolved target must carry
        # the LIVE value (independence) — the delivery authority then mismatches the row's "1".
        resolver = BackendNeutralTargetResolver(
            workspace_id=WS,
            inventory=self._inventory(),
            live_generation_fn=lambda r: "9",
        )
        res = resolver.resolve(_row(route=review_return_callback_route(LANE), generation="1"))
        self.assertEqual(len(res.targets), 1)
        self.assertEqual(res.targets[0].generation, "9")

    def test_no_live_generation_fn_leaves_generation_blank(self) -> None:
        # Phase A behaviour preserved: without an authority the live generation is blank (fail-closed).
        resolver = BackendNeutralTargetResolver(workspace_id=WS, inventory=self._inventory())
        res = resolver.resolve(_row(route="coordinator"))
        self.assertEqual(res.targets[0].generation, "")

    def test_live_generation_fn_raising_is_fail_closed_blank(self) -> None:
        def _boom(_row):
            raise RuntimeError("unreadable")

        resolver = BackendNeutralTargetResolver(
            workspace_id=WS, inventory=self._inventory(), live_generation_fn=_boom
        )
        res = resolver.resolve(_row(route=review_return_callback_route(LANE)))
        self.assertEqual(res.targets[0].generation, "")


if __name__ == "__main__":
    unittest.main()
