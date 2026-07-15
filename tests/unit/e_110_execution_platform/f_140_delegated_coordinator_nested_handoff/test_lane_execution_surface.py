"""Lane execution-surface taxonomy tests (Redmine #13756 j#78320).

Pins the closed product term and the projection that renders lane counts:

- a ``managed_sublane`` claim is only honoured when its provenance verifies — each
  required identity field is knocked out individually, because a guard that only checks
  *some* fields passes every happy-path test;
- an ACK is checked at the level it asserts: a ``worker_confirmed`` lane that cannot name
  its worker is an unfalsifiable claim and fails closed;
- an unrecognized surface or ACK token resolves to ``unknown``, never to a permissive
  default;
- internal task agents are counted separately and never appear in any sublane count.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_execution_surface import (
    DISPATCH_ACK_GATEWAY_ACKED,
    DISPATCH_ACK_NONE,
    DISPATCH_ACK_WORKER_CONFIRMED,
    SURFACE_COORDINATOR_LOCAL,
    SURFACE_DETACHED_WORKTREE,
    SURFACE_INTERNAL_TASK_AGENT,
    SURFACE_MANAGED_SUBLANE,
    SURFACE_UNKNOWN,
    SURFACE_UNSPECIFIED,
    CapacityProjection,
    LaneProvenance,
    SurfaceItem,
    is_verified_managed_sublane,
    missing_sublane_provenance,
    project_capacity,
    resolve_execution_surface,
)


def _verifying(**overrides) -> LaneProvenance:
    base = dict(
        execution_surface=SURFACE_MANAGED_SUBLANE,
        workspace="w19",
        lane="issue_13756_fill_actionability",
        issue_generation="1",
        lifecycle_revision="4",
        durable_anchor="13756#77986",
        gateway_identity="w28:pF",
        worker_identity="w28:pG",
        dispatch_ack=DISPATCH_ACK_WORKER_CONFIRMED,
    )
    base.update(overrides)
    return LaneProvenance(**base)


class ProvenanceVerificationTest(unittest.TestCase):
    def test_complete_provenance_verifies(self):
        self.assertEqual(missing_sublane_provenance(_verifying()), ())
        self.assertTrue(is_verified_managed_sublane(_verifying()))

    def test_each_identity_field_is_individually_required(self):
        # A guard that checks only some of the identity fields still passes the happy
        # path, so knock each one out on its own.
        for field in ("workspace", "lane", "lifecycle_revision", "durable_anchor"):
            with self.subTest(field=field):
                provenance = _verifying(**{field: ""})
                self.assertIn(field, missing_sublane_provenance(provenance))
                self.assertFalse(is_verified_managed_sublane(provenance))
                self.assertEqual(
                    resolve_execution_surface(provenance), SURFACE_UNKNOWN
                )

    def test_whitespace_only_identity_field_does_not_satisfy(self):
        self.assertFalse(is_verified_managed_sublane(_verifying(lane="   ")))

    def test_issue_generation_is_required_for_verification(self):
        # Review j#78471 finding 2: generation is what distinguishes a superseded lane
        # from its recovery lane, so a managed_sublane claim without it cannot verify —
        # failing it closed is the safe direction, not silently accepting it.
        provenance = _verifying(issue_generation="")
        self.assertIn("issue_generation", missing_sublane_provenance(provenance))
        self.assertFalse(is_verified_managed_sublane(provenance))

    def test_gateway_identity_is_required_at_every_ack_level(self):
        # A managed sublane always has a resolved gateway; the ACK level records whether
        # dispatch happened, it does not excuse naming the pair.
        for ack in (
            DISPATCH_ACK_NONE,
            DISPATCH_ACK_GATEWAY_ACKED,
            DISPATCH_ACK_WORKER_CONFIRMED,
        ):
            with self.subTest(ack=ack):
                provenance = _verifying(dispatch_ack=ack, gateway_identity="")
                self.assertIn(
                    "gateway_identity", missing_sublane_provenance(provenance)
                )

    def test_worker_identity_is_required_at_every_ack_level(self):
        # Review j#78471 finding 2: "worker confirmed" with no worker was the original
        # unfalsifiable claim, but even a resident-but-undispatched managed sublane has a
        # resolved worker, so the worker must be named regardless of ACK.
        for ack in (
            DISPATCH_ACK_NONE,
            DISPATCH_ACK_GATEWAY_ACKED,
            DISPATCH_ACK_WORKER_CONFIRMED,
        ):
            with self.subTest(ack=ack):
                provenance = _verifying(dispatch_ack=ack, worker_identity="")
                self.assertIn(
                    "worker_identity", missing_sublane_provenance(provenance)
                )
                self.assertFalse(is_verified_managed_sublane(provenance))

    def test_no_ack_still_requires_the_full_pair(self):
        # A resident-but-undispatched sublane (ack=none) is legitimate, but it must still
        # name both halves of its pair.
        self.assertEqual(missing_sublane_provenance(_verifying()), ())
        self.assertEqual(
            missing_sublane_provenance(_verifying(dispatch_ack=DISPATCH_ACK_NONE)), ()
        )

    def test_unknown_dispatch_ack_token_fails_closed(self):
        provenance = _verifying(dispatch_ack="probably_fine")
        self.assertIn("dispatch_ack", missing_sublane_provenance(provenance))
        self.assertEqual(resolve_execution_surface(provenance), SURFACE_UNKNOWN)


class SurfaceResolutionTest(unittest.TestCase):
    def test_recognized_non_sublane_surfaces_resolve_to_themselves(self):
        for surface in (
            SURFACE_INTERNAL_TASK_AGENT,
            SURFACE_COORDINATOR_LOCAL,
            SURFACE_DETACHED_WORKTREE,
            SURFACE_UNSPECIFIED,
        ):
            with self.subTest(surface=surface):
                self.assertEqual(
                    resolve_execution_surface(
                        LaneProvenance(execution_surface=surface)
                    ),
                    surface,
                )

    def test_task_agent_cannot_be_promoted_by_adding_provenance(self):
        # Even with a full sublane provenance block attached, a task agent stays one.
        provenance = _verifying(execution_surface=SURFACE_INTERNAL_TASK_AGENT)
        self.assertEqual(
            resolve_execution_surface(provenance), SURFACE_INTERNAL_TASK_AGENT
        )
        self.assertFalse(is_verified_managed_sublane(provenance))

    def test_free_form_surface_resolves_to_unknown(self):
        for claimed in ("lane", "parallel agent", "sublane", ""):
            with self.subTest(claimed=claimed):
                self.assertEqual(
                    resolve_execution_surface(
                        LaneProvenance(execution_surface=claimed)
                    ),
                    SURFACE_UNKNOWN,
                )

    def test_default_provenance_is_the_legacy_unspecified_surface(self):
        # The pre-#13756 caller made no claim at all: that is `unspecified`, which is not
        # a sublane but is also not a misread.
        self.assertEqual(resolve_execution_surface(LaneProvenance()), SURFACE_UNSPECIFIED)
        self.assertFalse(is_verified_managed_sublane(LaneProvenance()))


class CapacityProjectionTest(unittest.TestCase):
    def test_empty_projection(self):
        projection = project_capacity([])
        self.assertEqual(projection, CapacityProjection())

    def test_task_agents_never_enter_any_sublane_count(self):
        items = [
            SurfaceItem(
                provenance=LaneProvenance(
                    execution_surface=SURFACE_INTERNAL_TASK_AGENT
                ),
                coordinator_blocking=False,
            )
            for _ in range(5)
        ]
        projection = project_capacity(items)
        self.assertEqual(projection.internal_task_agents, 5)
        self.assertEqual(projection.resident_managed_sublanes, 0)
        self.assertEqual(projection.gateway_dispatched_sublanes, 0)
        self.assertEqual(projection.worker_confirmed_productive_sublanes, 0)
        self.assertEqual(projection.blocked_or_undispatched_sublanes, 0)

    def test_ack_ladder(self):
        # Three distinct lanes (distinct generations) at ascending ACK levels; each names
        # its full pair, only the ACK differs.
        items = [
            SurfaceItem(
                provenance=_verifying(
                    issue_generation="1", dispatch_ack=DISPATCH_ACK_NONE
                ),
                coordinator_blocking=False,
            ),
            SurfaceItem(
                provenance=_verifying(
                    issue_generation="2", dispatch_ack=DISPATCH_ACK_GATEWAY_ACKED
                ),
                coordinator_blocking=False,
            ),
            SurfaceItem(
                provenance=_verifying(issue_generation="3"), coordinator_blocking=False
            ),
        ]
        projection = project_capacity(items)
        self.assertEqual(projection.resident_managed_sublanes, 3)
        # Worker confirmation implies the gateway leg succeeded.
        self.assertEqual(projection.gateway_dispatched_sublanes, 2)
        self.assertEqual(projection.worker_confirmed_productive_sublanes, 1)
        self.assertEqual(projection.blocked_or_undispatched_sublanes, 2)

    def test_duplicate_identity_is_counted_once(self):
        # Review j#78471 finding 3: the same lane listed twice must not double-count.
        item = SurfaceItem(provenance=_verifying(), coordinator_blocking=False)
        projection = project_capacity([item, item])
        self.assertEqual(projection.resident_managed_sublanes, 1)
        self.assertEqual(projection.worker_confirmed_productive_sublanes, 1)

    def test_conflicting_duplicate_identity_fails_closed(self):
        # Same canonical identity, contradictory facts (different revision / blocking):
        # the coordinator holds two readings of one lane, so it fails closed.
        a = SurfaceItem(
            provenance=_verifying(lifecycle_revision="4"), coordinator_blocking=False
        )
        b = SurfaceItem(
            provenance=_verifying(lifecycle_revision="9"), coordinator_blocking=True
        )
        projection = project_capacity([a, b])
        self.assertEqual(projection.resident_managed_sublanes, 0)
        self.assertEqual(projection.unverified_surface, 1)

    def test_distinct_generations_are_distinct_residents(self):
        superseded = SurfaceItem(
            provenance=_verifying(issue_generation="1"), coordinator_blocking=True
        )
        recovery = SurfaceItem(
            provenance=_verifying(issue_generation="2"), coordinator_blocking=False
        )
        projection = project_capacity([superseded, recovery])
        self.assertEqual(projection.resident_managed_sublanes, 2)
        self.assertEqual(projection.worker_confirmed_productive_sublanes, 1)

    def test_blocking_lane_is_resident_but_not_productive(self):
        projection = project_capacity(
            [SurfaceItem(provenance=_verifying(), coordinator_blocking=True)]
        )
        self.assertEqual(projection.resident_managed_sublanes, 1)
        self.assertEqual(projection.worker_confirmed_productive_sublanes, 0)
        self.assertEqual(projection.blocked_or_undispatched_sublanes, 1)

    def test_resident_equals_productive_plus_blocked_or_undispatched(self):
        # The projection is meant to be narrated from, so its counts must reconcile.
        # Distinct generations so these are three distinct residents, not duplicates.
        items = [
            SurfaceItem(
                provenance=_verifying(issue_generation="1"), coordinator_blocking=False
            ),
            SurfaceItem(
                provenance=_verifying(issue_generation="2"), coordinator_blocking=True
            ),
            SurfaceItem(
                provenance=_verifying(
                    issue_generation="3", dispatch_ack=DISPATCH_ACK_NONE
                ),
                coordinator_blocking=False,
            ),
            SurfaceItem(
                provenance=LaneProvenance(
                    execution_surface=SURFACE_INTERNAL_TASK_AGENT
                ),
                coordinator_blocking=False,
            ),
        ]
        projection = project_capacity(items)
        self.assertEqual(
            projection.resident_managed_sublanes,
            projection.worker_confirmed_productive_sublanes
            + projection.blocked_or_undispatched_sublanes,
        )

    def test_unverified_and_other_surfaces_are_counted_apart(self):
        items = [
            SurfaceItem(
                provenance=LaneProvenance(execution_surface="free form"),
                coordinator_blocking=True,
            ),
            SurfaceItem(
                provenance=LaneProvenance(execution_surface=SURFACE_DETACHED_WORKTREE),
                coordinator_blocking=True,
            ),
            SurfaceItem(provenance=LaneProvenance(), coordinator_blocking=False),
        ]
        projection = project_capacity(items)
        self.assertEqual(projection.unverified_surface, 1)
        self.assertEqual(projection.other_surface, 2)
        self.assertEqual(projection.resident_managed_sublanes, 0)

    def test_payload_exposes_every_count(self):
        payload = project_capacity(
            [SurfaceItem(provenance=_verifying(), coordinator_blocking=False)]
        ).as_payload()
        self.assertEqual(payload["resident_managed_sublanes"], 1)
        self.assertEqual(payload["worker_confirmed_productive_sublanes"], 1)
        self.assertIn("internal_task_agents", payload)
        self.assertIn("unverified_surface", payload)


if __name__ == "__main__":
    unittest.main()
