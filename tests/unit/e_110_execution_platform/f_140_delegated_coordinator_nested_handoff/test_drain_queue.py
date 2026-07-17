"""Coordinator dependency drain-queue projection tests (Redmine #13967 item 5).

Pins the pure fold (:func:`project_drain_queue`) + the CLI derivations that back
``workflow drain-queue``:

- **the bucket vocabulary is the drain order** — each lane state maps to its fixed drain
  bucket, and every one of the eight drain buckets is always emitted (stable contract);
- **release_dogfood is the delegated terminal bucket** — a release-pending lane routes there
  ONLY when it carries no coordinator-blocking drain (a delegated dogfood never masks a live
  review / callback / owner / integration / close / blocker);
- **process retention is earned** — PROCESS_HOLD fires only for a coordinator_actionable lane
  in a holding bucket; retirement / release_dogfood alone never force a hold (the invariant
  that lets a review-approved + integrated lane hibernate early);
- **fail-closed** — an out-of-vocabulary actionability folds to coordinator_actionable, an
  unrecognized state class is surfaced as the ``unknown`` bucket (never dropped);
- the CLI derivations: structured ``--snapshot-json`` and the ``--from-glance`` fold
  (glance rows -> drain lanes, release-pending diagnostic rows -> delegated dogfood lanes).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_drain import (
    cmd_workflow_drain_queue,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.drain_queue import (
    BUCKET_BLOCKED,
    BUCKET_CALLBACK,
    BUCKET_CLOSE,
    BUCKET_IDLE,
    BUCKET_IMPLEMENTING,
    BUCKET_INTEGRATION,
    BUCKET_OWNER,
    BUCKET_RELEASE_DOGFOOD,
    BUCKET_RETIREMENT,
    BUCKET_REVIEW,
    BUCKET_UNKNOWN,
    DRAIN_BUCKETS,
    HOLD_REASON_DURABLE_INCOMPLETE,
    HOLD_REASON_UNKNOWN_STATE,
    PROCESS_HOLD,
    PROCESS_RELEASABLE,
    DrainLane,
    bucket_for_state,
    project_drain_queue,
    render_drain_queue_table,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_actionability import (
    ACTIONABILITY_COORDINATOR_ACTIONABLE,
    ACTIONABILITY_DELEGATED_IN_FLIGHT,
    ACTIONABILITY_NON_ACTIONABLE_WAIT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_fill_decision import (
    LANE_STATE_BLOCKED,
    LANE_STATE_CALLBACK_DELIVERY_FAILED,
    LANE_STATE_CALLBACK_DUE,
    LANE_STATE_CLOSE_WAITING,
    LANE_STATE_IDLE,
    LANE_STATE_IMPLEMENTING,
    LANE_STATE_INTEGRATION_WAITING,
    LANE_STATE_OWNER_WAITING,
    LANE_STATE_RETIRE_READY,
    LANE_STATE_REVIEW_WAITING,
)


class BucketMappingTests(unittest.TestCase):
    def test_state_to_bucket_covers_the_drain_order(self):
        cases = {
            LANE_STATE_CALLBACK_DUE: BUCKET_CALLBACK,
            LANE_STATE_CALLBACK_DELIVERY_FAILED: BUCKET_CALLBACK,
            LANE_STATE_REVIEW_WAITING: BUCKET_REVIEW,
            LANE_STATE_OWNER_WAITING: BUCKET_OWNER,
            LANE_STATE_INTEGRATION_WAITING: BUCKET_INTEGRATION,
            LANE_STATE_CLOSE_WAITING: BUCKET_CLOSE,
            LANE_STATE_BLOCKED: BUCKET_BLOCKED,
            LANE_STATE_RETIRE_READY: BUCKET_RETIREMENT,
            LANE_STATE_IMPLEMENTING: BUCKET_IMPLEMENTING,
            LANE_STATE_IDLE: BUCKET_IDLE,
        }
        for state, bucket in cases.items():
            self.assertEqual(bucket_for_state(state), bucket, state)

    def test_unrecognized_state_is_surfaced_as_unknown(self):
        self.assertEqual(bucket_for_state("bogus_state"), BUCKET_UNKNOWN)

    def test_release_pending_routes_idle_to_release_dogfood(self):
        self.assertEqual(
            bucket_for_state(LANE_STATE_IDLE, release_pending=True),
            BUCKET_RELEASE_DOGFOOD,
        )
        self.assertEqual(
            bucket_for_state(LANE_STATE_RETIRE_READY, release_pending=True),
            BUCKET_RELEASE_DOGFOOD,
        )

    def test_release_pending_never_launders_unknown_state(self):
        # Redmine #13967 R2-F2: a release flag must not route an unreadable state into
        # release_dogfood (which would bypass the fail-closed unknown hold).
        self.assertEqual(
            bucket_for_state("mystery", release_pending=True), BUCKET_UNKNOWN
        )

    def test_release_pending_never_masks_a_blocking_drain(self):
        # A lane that still owes a review is bucketed by the review, not hidden behind a
        # delegated dogfood flag.
        self.assertEqual(
            bucket_for_state(LANE_STATE_REVIEW_WAITING, release_pending=True),
            BUCKET_REVIEW,
        )
        self.assertEqual(
            bucket_for_state(LANE_STATE_CALLBACK_DUE, release_pending=True),
            BUCKET_CALLBACK,
        )


class ProjectionTests(unittest.TestCase):
    def test_all_eight_drain_buckets_always_emitted(self):
        projection = project_drain_queue(())
        emitted = [b.bucket for b in projection.buckets]
        for bucket in DRAIN_BUCKETS:
            self.assertIn(bucket, emitted)
        # empty non-drain buckets are omitted
        self.assertNotIn(BUCKET_IDLE, emitted)

    def test_hold_when_coordinator_actionable_review_present(self):
        projection = project_drain_queue(
            [DrainLane(issue="1", state_class=LANE_STATE_REVIEW_WAITING)]
        )
        self.assertEqual(projection.process_retention, PROCESS_HOLD)
        self.assertIn(BUCKET_REVIEW, projection.hold_buckets)
        self.assertEqual(projection.coordinator_actionable_total, 1)

    def test_delegated_review_does_not_hold_the_process(self):
        # A review delivered to a dedicated gateway (delegated_in_flight) is not a
        # coordinator hold reason.
        projection = project_drain_queue(
            [
                DrainLane(
                    issue="1",
                    state_class=LANE_STATE_REVIEW_WAITING,
                    actionability=ACTIONABILITY_DELEGATED_IN_FLIGHT,
                )
            ]
        )
        self.assertEqual(projection.process_retention, PROCESS_RELEASABLE)
        self.assertEqual(projection.hold_buckets, ())

    def test_retirement_and_release_dogfood_alone_are_releasable(self):
        # The early-hibernate invariant: a review-approved + integrated lane whose only
        # residue is retirement cleanup + delegated dogfood does NOT force a process hold.
        projection = project_drain_queue(
            [
                DrainLane(issue="1", state_class=LANE_STATE_RETIRE_READY),
                DrainLane(
                    issue="2",
                    state_class=LANE_STATE_IDLE,
                    actionability=ACTIONABILITY_NON_ACTIONABLE_WAIT,
                    release_pending=True,
                ),
            ]
        )
        self.assertEqual(projection.process_retention, PROCESS_RELEASABLE)
        self.assertEqual(projection.retirement_pending, 1)
        self.assertEqual(projection.release_dogfood_pending, 1)

    def test_out_of_vocabulary_actionability_fails_closed_to_coordinator(self):
        projection = project_drain_queue(
            [DrainLane(issue="1", state_class=LANE_STATE_BLOCKED, actionability="bogus")]
        )
        self.assertEqual(projection.process_retention, PROCESS_HOLD)
        blocked = projection.bucket(BUCKET_BLOCKED)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.coordinator_actionable, 1)

    def test_implementing_is_never_a_hold_reason(self):
        projection = project_drain_queue(
            [DrainLane(issue="1", state_class=LANE_STATE_IMPLEMENTING)]
        )
        self.assertEqual(projection.process_retention, PROCESS_RELEASABLE)
        # implementing is surfaced as a non-drain bucket.
        self.assertIsNotNone(projection.bucket(BUCKET_IMPLEMENTING))

    def test_unknown_state_holds_fail_closed_and_is_visible(self):
        # Redmine #13967 F2: an unreadable durable state must fail closed to hold — a
        # retention verdict that gates early hibernate never releases from state it could
        # not read.
        projection = project_drain_queue(
            [DrainLane(issue="9", state_class="mystery")]
        )
        self.assertEqual(projection.process_retention, PROCESS_HOLD)
        self.assertIn(HOLD_REASON_UNKNOWN_STATE, projection.hold_buckets)
        self.assertIsNotNone(projection.bucket(BUCKET_UNKNOWN))

    def test_unknown_with_release_pending_still_holds(self):
        # Redmine #13967 R2-F2: a release flag cannot launder an unknown state past the hold.
        projection = project_drain_queue(
            [DrainLane(issue="9", state_class="mystery", release_pending=True)]
        )
        self.assertEqual(projection.process_retention, PROCESS_HOLD)
        self.assertIn(HOLD_REASON_UNKNOWN_STATE, projection.hold_buckets)
        self.assertEqual(projection.release_dogfood_pending, 0)

    def test_durable_incomplete_forces_hold(self):
        # A source the caller could not fully read (degraded) forces hold even with no
        # coordinator-blocking lane.
        projection = project_drain_queue(
            [DrainLane(issue="1", state_class=LANE_STATE_IMPLEMENTING)],
            durable_complete=False,
        )
        self.assertEqual(projection.process_retention, PROCESS_HOLD)
        self.assertIn(HOLD_REASON_DURABLE_INCOMPLETE, projection.hold_buckets)
        self.assertFalse(projection.durable_complete)

    def test_ownership_split_counts(self):
        projection = project_drain_queue(
            [
                DrainLane(issue="1", state_class=LANE_STATE_BLOCKED),
                DrainLane(
                    issue="2",
                    state_class=LANE_STATE_BLOCKED,
                    actionability=ACTIONABILITY_NON_ACTIONABLE_WAIT,
                ),
            ]
        )
        blocked = projection.bucket(BUCKET_BLOCKED)
        self.assertEqual(blocked.total, 2)
        self.assertEqual(blocked.coordinator_actionable, 1)
        self.assertEqual(blocked.non_actionable_wait, 1)
        # Still a hold: one blocked lane is coordinator-actionable.
        self.assertEqual(projection.process_retention, PROCESS_HOLD)

    def test_render_table_smoke(self):
        projection = project_drain_queue(
            [DrainLane(issue="1", state_class=LANE_STATE_REVIEW_WAITING)]
        )
        text = render_drain_queue_table(projection)
        self.assertIn("process_retention: hold", text)
        self.assertIn(BUCKET_REVIEW, text)


class CliTests(unittest.TestCase):
    def _run(self, **kwargs) -> dict:
        args = argparse.Namespace(
            snapshot_json=None, from_glance=None, as_json=True, repo=None
        )
        for k, v in kwargs.items():
            setattr(args, k, v)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_workflow_drain_queue(args)
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_snapshot_json_projection(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lanes.json"
            path.write_text(
                json.dumps(
                    {
                        "lanes": [
                            {"issue": "100", "state_class": LANE_STATE_REVIEW_WAITING},
                            {
                                "issue": "101",
                                "state_class": LANE_STATE_INTEGRATION_WAITING,
                                "actionability": ACTIONABILITY_COORDINATOR_ACTIONABLE,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertIn(BUCKET_REVIEW, payload["hold_buckets"])
        self.assertIn(BUCKET_INTEGRATION, payload["hold_buckets"])

    def test_snapshot_malformed_row_forces_hold(self):
        # Redmine #13967 R2-F2: a malformed row (here a non-bool release_pending) is not
        # silently dropped — it makes the snapshot durable-incomplete -> hold.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lanes.json"
            path.write_text(
                json.dumps(
                    {
                        "lanes": [
                            {"issue": "1", "state_class": LANE_STATE_IMPLEMENTING},
                            {"issue": "2", "state_class": LANE_STATE_IDLE, "release_pending": "false"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])

    def test_from_glance_derives_lanes_and_release_dogfood(self):
        glance_env = {
            "rows": [
                {
                    "issue_id": "200",
                    "lane": "lane-200",
                    "workflow_state": LANE_STATE_REVIEW_WAITING,
                    "next_owner": "auditor",
                }
            ],
            "lifecycle_diagnostic": [
                {
                    "issue": "201",
                    "lane": "lane-201",
                    "lane_disposition": "hibernated",
                    "process_release": "requested",
                },
                {
                    "issue": "202",
                    "lane": "lane-202",
                    "lane_disposition": "retired",
                    "process_release": "released",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "glance.json"
            path.write_text(json.dumps(glance_env), encoding="utf-8")
            payload = self._run(from_glance=str(path))
        # The active review row holds; the requested-release lane is a delegated dogfood
        # bucket entry; the already-released lane is not pending, so it is not counted.
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertEqual(payload["release_dogfood_pending"], 1)

    def test_from_glance_merges_release_flag_by_identity(self):
        # Redmine #13967 F3: a lane that is both an active row AND a release-pending
        # diagnostic row is counted ONCE, and its coordinator-blocking base bucket wins
        # over release_dogfood (a delegated dogfood never hides live review drain).
        glance_env = {
            "rows": [
                {
                    "issue_id": "300",
                    "lane": "lane-300",
                    "workflow_state": LANE_STATE_REVIEW_WAITING,
                    "next_owner": "auditor",
                }
            ],
            "lifecycle_diagnostic": [
                {
                    "issue": "300",
                    "lane": "lane-300",
                    "lane_disposition": "active",
                    "process_release": "requested",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "glance.json"
            path.write_text(json.dumps(glance_env), encoding="utf-8")
            payload = self._run(from_glance=str(path))
        self.assertEqual(payload["lane_count"], 1)  # merged, not double-counted
        self.assertEqual(payload["release_dogfood_pending"], 0)  # blocking base bucket wins
        review = next(b for b in payload["buckets"] if b["bucket"] == BUCKET_REVIEW)
        self.assertEqual(review["total"], 1)
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)


if __name__ == "__main__":
    unittest.main()
