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
from unittest import mock

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
    HOLD_REASON_DELIVERY_ANOMALY,
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


def _active_row(
    issue="1",
    lane="l",
    state="idle",
    next_owner="x",
    anomaly="none",
    stale=False,
    active=None,
):
    """A canonical `workflow glance` active row (with the full delivery-anomaly triple).

    Mirrors ``WorkflowGlanceRow.as_payload``: equal ``workflow_state``/``state_class`` and the
    ``delivery_anomaly`` / ``delivery_anomaly_stale`` / ``has_active_anomaly`` transport
    dimension (``has_active_anomaly == anomaly != none and not stale`` unless overridden).
    """
    if active is None:
        active = anomaly != "none" and not stale
    return {
        "issue_id": issue,
        "lane": lane,
        "workflow_state": state,
        "state_class": state,
        "next_owner": next_owner,
        "delivery_anomaly": anomaly,
        "delivery_anomaly_stale": stale,
        "has_active_anomaly": active,
    }


def _glance_env(rows=None, diag=None, **overrides):
    """A canonical `workflow glance --json` envelope: rows + count + active_anomaly_issues +
    degraded + lifecycle_diagnostic, all derived to be self-consistent unless overridden.

    ``glance_payload`` always emits ``count == len(rows)`` and
    ``active_anomaly_issues == [r.issue_id for r in rows if r.has_active_anomaly]`` (Redmine
    #13967 R9), so a faithful fixture must too; a test then overrides exactly the one
    dimension it probes.
    """
    rows = rows or []
    env = {
        "rows": rows,
        "count": len(rows),
        "active_anomaly_issues": [
            r["issue_id"] for r in rows if r.get("has_active_anomaly")
        ],
        "degraded": False,
        "notes": [],
        "lifecycle_diagnostic": diag or [],
    }
    env.update(overrides)
    return env


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

    def test_live_delivery_anomaly_holds_without_rewinding_state(self):
        # Redmine #13967 R9-F1: a lane carrying a live (non-stale) delivery anomaly forces a
        # PROCESS_HOLD as an orthogonal transport-repair obligation — even when its durable
        # state (here idle) is otherwise releasable — and never rewinds that state.
        projection = project_drain_queue(
            [
                DrainLane(
                    issue="1",
                    state_class=LANE_STATE_IDLE,
                    actionability=ACTIONABILITY_NON_ACTIONABLE_WAIT,
                    delivery_anomaly_active=True,
                )
            ]
        )
        self.assertEqual(projection.process_retention, PROCESS_HOLD)
        self.assertIn(HOLD_REASON_DELIVERY_ANOMALY, projection.hold_buckets)
        self.assertEqual(projection.delivery_anomaly_pending, 1)
        # state not rolled back: the lane is still bucketed idle (a non-drain bucket).
        self.assertIsNotNone(projection.bucket(BUCKET_IDLE))

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
        # A canonical glance envelope: rows carry equal workflow_state/state_class, diagnostics
        # carry a non-active disposition + in-vocabulary process_release, degraded=false.
        glance_env = _glance_env(
            rows=[
                _active_row(
                    issue="200",
                    lane="lane-200",
                    state=LANE_STATE_REVIEW_WAITING,
                    next_owner="auditor",
                )
            ],
            diag=[
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
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "glance.json"
            path.write_text(json.dumps(glance_env), encoding="utf-8")
            payload = self._run(from_glance=str(path))
        # The active review row holds; the requested-release lane is a delegated dogfood
        # bucket entry; the already-released lane is not pending, so it is not counted.
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertEqual(payload["release_dogfood_pending"], 1)

    def test_glance_type_confusion_fails_closed(self):
        # Redmine #13967 R4-F2: non-string identity is never str-coerced, and a non-list
        # container never crashes — every malformed shape holds (durable-incomplete).
        cases = [
            {"rows": [], "lifecycle_diagnostic": [
                {"process_release": "requested", "issue": {"x": 1}, "lane": ["lane"]}]},
            {"rows": 1, "lifecycle_diagnostic": []},
            {"rows": [], "lifecycle_diagnostic": 1},
            {"rows": [{"issue_id": "1", "workflow_state": ["review_waiting"]}]},
            {"rows": [{"issue_id": {"x": 1}, "workflow_state": "review_waiting"}]},
        ]
        for env in cases:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "g.json"
                path.write_text(json.dumps(env), encoding="utf-8")
                payload = self._run(from_glance=str(path))
            self.assertEqual(payload["process_retention"], PROCESS_HOLD, env)
            self.assertFalse(payload["durable_complete"], env)

    def test_from_glance_collection_identity_invariants(self):
        # Redmine #13967 R8-F1: the active roster and the non-active lifecycle diagnostic are
        # disjoint by contract (a lane is active XOR non-active), and identities are unique
        # within each collection. A cross-collection collision, a duplicate diagnostic
        # identity (with possibly conflicting state), or a duplicate active identity is
        # contradictory durable state -> hold.
        active_row = _active_row(issue="1", lane="l", state="idle")

        def env(rows, diag):
            return _glance_env(rows=rows, diag=diag)

        hold_cases = [
            # same (issue,lane) in both active and diagnostic
            env([active_row], [{"issue": "1", "lane": "l", "lane_disposition": "hibernated", "process_release": "released"}]),
            env([active_row], [{"issue": "1", "lane": "l", "lane_disposition": "hibernated", "process_release": "requested"}]),
            # same diagnostic identity twice with conflicting disposition/release
            env([], [
                {"issue": "1", "lane": "l", "lane_disposition": "hibernated", "process_release": "requested"},
                {"issue": "1", "lane": "l", "lane_disposition": "retired", "process_release": "released"},
            ]),
            # duplicated active identity
            env([active_row, active_row], []),
        ]
        for e in hold_cases:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "g.json"
                path.write_text(json.dumps(e), encoding="utf-8")
                payload = self._run(from_glance=str(path))
            self.assertEqual(payload["process_retention"], PROCESS_HOLD, e)
            self.assertFalse(payload["durable_complete"], e)
        # Disjoint active + diagnostic identities are complete/releasable.
        ok = env([active_row], [{"issue": "2", "lane": "l2", "lane_disposition": "retired", "process_release": "released"}])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ok.json"
            path.write_text(json.dumps(ok), encoding="utf-8")
            payload = self._run(from_glance=str(path))
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)

    def test_from_glance_row_authority_contract(self):
        # Redmine #13967 R7: the canonical glance row contract — active rows carry equal
        # workflow_state/state_class; diagnostic rows carry issue/lane/lane_disposition
        # (non-active) + process_release (closed RELEASE_STATES). A violation is a
        # contradictory/malformed canonical row -> hold, never trusted one-sided.
        def env(rows=None, diag=None):
            return _glance_env(rows=rows or [], diag=diag or [])

        hold_cases = [
            # R7-F1: workflow_state/state_class conflict, or missing/null/non-string state_class.
            env(rows=[{"issue_id": "1", "workflow_state": "idle", "state_class": "review_waiting", "lane": "l", "next_owner": "x"}]),
            env(rows=[{"issue_id": "1", "workflow_state": "idle", "lane": "l", "next_owner": "x"}]),
            env(rows=[{"issue_id": "1", "workflow_state": "idle", "state_class": None, "lane": "l", "next_owner": "x"}]),
            # R7-F2: unknown / missing-identity / null-disposition / active-disposition diagnostics.
            env(diag=[{"issue": "1", "lane": "l", "lane_disposition": "hibernated", "process_release": "bogus"}]),
            env(diag=[{"lane_disposition": "retired", "process_release": "released"}]),
            env(diag=[{"issue": "1", "lane": "l", "lane_disposition": None, "process_release": "released"}]),
            env(diag=[{"issue": "1", "lane": "l", "lane_disposition": "active", "process_release": "requested"}]),
        ]
        for e in hold_cases:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "g.json"
                path.write_text(json.dumps(e), encoding="utf-8")
                payload = self._run(from_glance=str(path))
            self.assertEqual(payload["process_retention"], PROCESS_HOLD, e)
            self.assertFalse(payload["durable_complete"], e)
        # A valid canonical row with equal states and an in-vocabulary non-active diagnostic
        # (released, not pending) is complete and releasable.
        ok = env(
            rows=[_active_row(issue="1", lane="l", state="idle")],
            diag=[{"issue": "2", "lane": "l2", "lane_disposition": "retired", "process_release": "released"}],
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ok.json"
            path.write_text(json.dumps(ok), encoding="utf-8")
            payload = self._run(from_glance=str(path))
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)

    def test_from_glance_envelope_completeness(self):
        # Redmine #13967 R6-F2: the canonical `workflow glance --json` producer always emits
        # rows + an exact-bool degraded and always appends lifecycle_diagnostic. A missing /
        # present-null / wrong-type rows/lifecycle_diagnostic/degraded, or degraded=true, is a
        # partially-read envelope -> hold. ONLY the canonical empty envelope is releasable.
        hold_cases = [
            {},
            {"rows": []},
            {"rows": [], "lifecycle_diagnostic": []},  # degraded absent
            {"rows": [], "lifecycle_diagnostic": [], "degraded": None},
            {"rows": [], "lifecycle_diagnostic": [], "degraded": 0},
            {"rows": [], "lifecycle_diagnostic": [], "degraded": True},
            {"rows": None, "lifecycle_diagnostic": [], "degraded": False},
            {"rows": [], "lifecycle_diagnostic": None, "degraded": False},
        ]
        for env in hold_cases:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "g.json"
                path.write_text(json.dumps(env), encoding="utf-8")
                payload = self._run(from_glance=str(path))
            self.assertEqual(payload["process_retention"], PROCESS_HOLD, env)
            self.assertFalse(payload["durable_complete"], env)
        # The canonical empty envelope (rows + count + active_anomaly_issues + degraded +
        # lifecycle_diagnostic, all self-consistent) is releasable.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ok.json"
            path.write_text(json.dumps(_glance_env()), encoding="utf-8")
            payload = self._run(from_glance=str(path))
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)
        self.assertTrue(payload["durable_complete"])

    def test_from_glance_live_delivery_anomaly_holds(self):
        # Redmine #13967 R9-F1: a live (non-stale) delivery anomaly on an otherwise
        # non-blocking (idle) lane is a coordinator transport-repair obligation -> hold,
        # WITHOUT rewinding the durable state_class (the glance non-rollback invariant): the
        # envelope is fully readable (durable_complete=True), yet the projection holds and
        # surfaces the anomaly count + hold reason.
        env = _glance_env(
            rows=[_active_row(issue="1", lane="l", state="idle", anomaly="callback_delivery_failed", stale=False)]
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "g.json"
            path.write_text(json.dumps(env), encoding="utf-8")
            payload = self._run(from_glance=str(path))
        self.assertTrue(payload["durable_complete"])  # the envelope was readable...
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)  # ...but a live anomaly holds
        self.assertIn(HOLD_REASON_DELIVERY_ANOMALY, payload["hold_buckets"])
        self.assertEqual(payload["delivery_anomaly_pending"], 1)
        # The durable state was NOT rolled back: the lane is still bucketed idle.
        idle = next(b for b in payload["buckets"] if b["bucket"] == BUCKET_IDLE)
        self.assertEqual(idle["total"], 1)

    def test_from_glance_stale_anomaly_is_releasable(self):
        # A stale (superseded) anomaly is NOT active -> it does not hold: has_active_anomaly
        # is false, so the idle lane is releasable.
        env = _glance_env(
            rows=[_active_row(issue="1", lane="l", state="idle", anomaly="callback_delivery_failed", stale=True)]
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "g.json"
            path.write_text(json.dumps(env), encoding="utf-8")
            payload = self._run(from_glance=str(path))
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)
        self.assertEqual(payload["delivery_anomaly_pending"], 0)

    def test_from_glance_anomaly_contract_violations_hold(self):
        # Redmine #13967 R9-F1: the delivery-anomaly triple is validated exact-type + closed
        # vocab + mutual consistency, and the envelope active_anomaly_issues must be a
        # duplicate-free string list whose set matches the row-derived active-anomaly issues.
        hold_cases = [
            # out-of-vocabulary anomaly token
            _glance_env(rows=[_active_row(anomaly="bogus", stale=False, active=True)]),
            # non-string anomaly
            _glance_env(rows=[_active_row(anomaly=1, stale=False, active=True)]),
            # non-bool stale
            _glance_env(rows=[_active_row(anomaly="none", stale="false", active=False)]),
            # non-bool has_active_anomaly
            _glance_env(rows=[_active_row(anomaly="none", stale=False, active="false")]),
            # has_active_anomaly inconsistent with anomaly/stale (claims active for none)
            _glance_env(rows=[_active_row(anomaly="none", stale=False, active=True)]),
            # has_active_anomaly inconsistent (claims inactive for a live anomaly)
            _glance_env(rows=[_active_row(anomaly="callback_delivery_failed", stale=False, active=False)]),
            # missing anomaly triple entirely
            _glance_env(rows=[{"issue_id": "1", "lane": "l", "workflow_state": "idle",
                               "state_class": "idle", "next_owner": "x"}]),
            # envelope active_anomaly_issues disagrees with rows (row is live, summary empty)
            _glance_env(
                rows=[_active_row(anomaly="callback_delivery_failed", stale=False)],
                active_anomaly_issues=[],
            ),
            # envelope active_anomaly_issues lists an issue the rows do not
            _glance_env(rows=[_active_row(state="idle")], active_anomaly_issues=["1"]),
            # active_anomaly_issues with a duplicate (contradictory summary)
            _glance_env(
                rows=[_active_row(issue="1", anomaly="callback_delivery_failed", stale=False)],
                active_anomaly_issues=["1", "1"],
            ),
            # active_anomaly_issues non-list / non-string member / present-null
            _glance_env(rows=[], active_anomaly_issues="1"),
            _glance_env(rows=[_active_row(anomaly="callback_delivery_failed", stale=False)],
                        active_anomaly_issues=[1]),
            _glance_env(rows=[], active_anomaly_issues=None),
        ]
        for env in hold_cases:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "g.json"
                path.write_text(json.dumps(env), encoding="utf-8")
                payload = self._run(from_glance=str(path))
            self.assertEqual(payload["process_retention"], PROCESS_HOLD, env)
            self.assertFalse(payload["durable_complete"], env)

    def test_from_glance_envelope_cardinality_and_active_issue_uniqueness(self):
        # Redmine #13967 R9-F2: the envelope `count` must be an exact int == len(rows), and
        # the active roster is one row per ISSUE (not merely per (issue, lane)).
        hold_cases = [
            # count disagrees with rows (empty rows, count=1)
            _glance_env(rows=[], count=1),
            # count disagrees (one row, count=2)
            _glance_env(rows=[_active_row()], count=2),
            # count as bool True (==1) must not satisfy the int contract
            _glance_env(rows=[_active_row()], count=True),
            # count present-null / non-int
            _glance_env(rows=[], count=None),
            _glance_env(rows=[], count="0"),
            # count absent entirely
            {k: v for k, v in _glance_env(rows=[]).items() if k != "count"},
            # two active rows for the SAME issue (different lanes) -> ambiguous ownership
            _glance_env(rows=[_active_row(issue="7", lane="l1"), _active_row(issue="7", lane="l2")]),
        ]
        for env in hold_cases:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "g.json"
                path.write_text(json.dumps(env), encoding="utf-8")
                payload = self._run(from_glance=str(path))
            self.assertEqual(payload["process_retention"], PROCESS_HOLD, env)
            self.assertFalse(payload["durable_complete"], env)

    def test_from_glance_notes_source_health_contract(self):
        # Redmine #13967 R11-F1: the canonical envelope ALWAYS emits `notes: list[str]` bound to
        # `degraded` by the invariant `degraded == bool(notes)` (GlanceCollection.degraded is
        # bool(self.notes); every source error appends a note AND sets degraded). The reader
        # must validate `notes` and that invariant — a missing / null / non-list / non-string
        # notes, or `degraded: false` with a non-empty notes (a reported source failure the
        # envelope did not flag degraded), is a contradictory / lost-field envelope -> hold.
        hold_cases = [
            {k: v for k, v in _glance_env().items() if k != "notes"},  # notes absent
            _glance_env(notes=None),  # present-null
            _glance_env(notes=1),  # non-list
            _glance_env(notes=[1]),  # non-string member
            _glance_env(notes=["source failed"]),  # degraded=false + non-empty -> invariant break
            _glance_env(degraded=True, notes=[]),  # degraded=true + empty -> invariant break
        ]
        for env in hold_cases:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "g.json"
                path.write_text(json.dumps(env), encoding="utf-8")
                payload = self._run(from_glance=str(path))
            self.assertEqual(payload["process_retention"], PROCESS_HOLD, env)
            self.assertFalse(payload["durable_complete"], env)
        # Healthy source-health: degraded=false + notes=[] is releasable.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ok.json"
            path.write_text(json.dumps(_glance_env(notes=[])), encoding="utf-8")
            payload = self._run(from_glance=str(path))
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)

    def test_snapshot_delivery_anomaly_active_roundtrips(self):
        # Redmine #13967 R10-F1: `DrainLane.as_payload()` emits delivery_anomaly_active, so the
        # deterministic --snapshot-json reader must read it back on exact-bool terms — a
        # self-emitted anomaly hold must survive the roundtrip, a present non-bool is malformed
        # (hold), and an absent field defaults to false (evaluable).
        lane = DrainLane(issue="1", lane="l", state_class=LANE_STATE_IDLE, delivery_anomaly_active=True)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(json.dumps({"lanes": [lane.as_payload()]}), encoding="utf-8")
            payload = self._run(snapshot_json=str(path))
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertIn(HOLD_REASON_DELIVERY_ANOMALY, payload["hold_buckets"])
        self.assertEqual(payload["delivery_anomaly_pending"], 1)
        # present non-bool -> malformed -> hold (never coerced)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(
                json.dumps({"lanes": [{"issue": "1", "lane": "l", "state_class": "idle",
                                       "delivery_anomaly_active": "true"}]}),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])
        # absent -> default false -> evaluable / releasable
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(
                json.dumps({"lanes": [{"issue": "1", "lane": "l", "state_class": LANE_STATE_IDLE}]}),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)

    def test_snapshot_duplicate_identity_holds(self):
        # Redmine #13967 R10-F4: a snapshot is an already-composed active lane set, so the same
        # (issue, lane) twice is contradictory input -> hold (durable-incomplete), matching the
        # glance path's one-lane-one-bucket invariant.
        row = {"issue": "7", "lane": "l1", "state_class": LANE_STATE_IDLE}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(json.dumps({"lanes": [row, dict(row)]}), encoding="utf-8")
            payload = self._run(snapshot_json=str(path))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])
        # distinct ISSUES are complete/releasable
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(
                json.dumps({"lanes": [
                    {"issue": "7", "lane": "l1", "state_class": LANE_STATE_IDLE},
                    {"issue": "8", "lane": "l2", "state_class": LANE_STATE_IDLE},
                ]}),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)

    def test_snapshot_active_issue_uniqueness_holds(self):
        # Redmine #13967 R13-F1: two ACTIVE rows for the SAME issue (even with different lanes)
        # is ambiguous active ownership -> hold, symmetric with from-glance (R9-F2) and
        # default-live (R12-F1). The canonical drain roster is one active lane per issue.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(
                json.dumps({"lanes": [
                    {"issue": "7", "lane": "l1", "state_class": LANE_STATE_IDLE},
                    {"issue": "7", "lane": "l2", "state_class": LANE_STATE_IDLE},
                ]}),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])

    def test_snapshot_active_plus_same_issue_release_row_is_allowed(self):
        # Redmine #13967 R14-F3: active-issue uniqueness applies ONLY to active rows. A valid
        # active recovery lane and its same-issue NON-ACTIVE historical release row
        # (release_pending=true) co-exist — symmetric with the from-glance active-roster /
        # lifecycle-diagnostic split — and must NOT be over-rejected.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(
                json.dumps({"lanes": [
                    {"issue": "7", "lane": "recovery", "state_class": LANE_STATE_IDLE},
                    {"issue": "7", "lane": "original", "state_class": LANE_STATE_IDLE,
                     "release_pending": True},
                ]}),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)
        self.assertEqual(payload["release_dogfood_pending"], 1)
        # but the SAME (issue, lane) as both active and release is a contradiction -> hold
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(
                json.dumps({"lanes": [
                    {"issue": "7", "lane": "l1", "state_class": LANE_STATE_IDLE},
                    {"issue": "7", "lane": "l1", "state_class": LANE_STATE_IDLE,
                     "release_pending": True},
                ]}),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])

    def test_snapshot_release_row_requires_non_empty_lane(self):
        # Redmine #13967 R14-F1: a release row (release_pending=true, a non-active
        # diagnostic-derived row) carries release authority and requires a non-empty lane, like
        # the from-glance / default-live diagnostic contract. An active row's lane stays optional.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(
                json.dumps({"lanes": [{"issue": "7", "state_class": LANE_STATE_IDLE,
                                       "release_pending": True}]}),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])
        # a release row WITH a lane is fine
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(
                json.dumps({"lanes": [{"issue": "7", "lane": "l1", "state_class": LANE_STATE_IDLE,
                                       "release_pending": True}]}),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["release_dogfood_pending"], 1)
        # an ACTIVE row with an empty lane stays valid (optional lane)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.json"
            path.write_text(
                json.dumps({"lanes": [{"issue": "7", "state_class": LANE_STATE_IDLE}]}),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertTrue(payload["durable_complete"])
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)

    def test_live_path_joins_delivery_ledger_for_anomaly_hold(self):
        # Redmine #13967 R10-F2: the default live path must join the SAME home-scoped delivery
        # ledger `workflow glance` joins, so a live anomaly on the existing ledger holds the
        # process (it is NOT hardwired to ledger=None). Seam test: capture the ledger passed to
        # active_lane_snapshots and fold a row with a live anomaly.
        mod = "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
        gss = mod + ".application.glance_snapshot_source"
        wg = mod + ".domain.workflow_glance"
        wrs = "mozyo_bridge.core.state.workflow_runtime_store"
        sentinel_ledger = object()
        captured = {}

        def fake_snapshots(roster, *, redmine_source, store, ledger, reconcile_store, authority_index):
            captured["ledger"] = ledger

            class _Coll:
                snapshots = ("SNAP",)
                notes = []
                degraded = False

            return _Coll()

        class _Row:
            issue_id = "9"
            lane = "lane9"
            workflow_state = LANE_STATE_IDLE
            next_owner = "x"
            has_active_anomaly = True

        with tempfile.TemporaryDirectory() as td, \
                mock.patch("mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_drain._delivery_ledger", return_value=sentinel_ledger), \
                mock.patch(gss + ".enumerate_active_lanes", return_value=((("9", "lane9"),), None)), \
                mock.patch(gss + ".enumerate_lifecycle_diagnostic", return_value=((), None)), \
                mock.patch(gss + ".active_lane_snapshots", fake_snapshots), \
                mock.patch(wg + ".fold_glance_rows", return_value=[_Row()]), \
                mock.patch(wrs + ".WorkflowRuntimeStore", lambda *a, **k: object()), \
                mock.patch(wrs + ".workflow_runtime_store_path", return_value=Path(td) / "rt.json"):
            payload = self._run(repo=td)
        # the joined ledger was the sentinel, NOT None (F2 wiring proven)
        self.assertIs(captured["ledger"], sentinel_ledger)
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertIn(HOLD_REASON_DELIVERY_ANOMALY, payload["hold_buckets"])
        self.assertEqual(payload["delivery_anomaly_pending"], 1)

    def test_delivery_ledger_helper_is_fail_open(self):
        # Redmine #13967 R10-F2: an unreadable ledger degrades to None (fail-open), never a
        # crash — exactly as glance's _ledger_from_args does.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_drain import (
            _delivery_ledger,
        )

        with mock.patch(
            "mozyo_bridge.core.state.herdr_delivery_ledger.HerdrDeliveryLedger",
            side_effect=RuntimeError("unreadable"),
        ):
            self.assertIsNone(_delivery_ledger())

    def test_live_diagnostic_report_pure(self):
        # Redmine #13967 R12-F1 / R14-F2 / R15-F1: the raw normalize+validate pass mirrors the
        # --from-glance reader's collection invariants on the live roster + diagnostic AND emits
        # the normalized release rows the projection folds — one parse, no drift. Returns
        # (notes, release_rows). Healthy distinct identities raise no note; each contradiction
        # (dup active issue / identity, dup diagnostic identity, active/diagnostic collision,
        # empty identity, out-of-vocab disposition/release) raises a note (-> degraded -> hold).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_drain import (
            _live_diagnostic_report,
        )

        def notes(roster, diag):
            return _live_diagnostic_report(roster, diag)[0]

        def releases(roster, diag):
            return _live_diagnostic_report(roster, diag)[1]

        self.assertEqual(notes((("9", "la"),), (("8", "lb", "retired", "released"),)), [])
        self.assertTrue(notes((("9", "la"), ("9", "lb")), ()))  # dup active issue
        self.assertTrue(notes((("9", "la"), ("9", "la")), ()))  # dup active identity
        self.assertTrue(  # dup diagnostic identity
            notes(
                (("9", "la"),),
                (("8", "lb", "retired", "requested"), ("8", "lb", "hibernated", "released")),
            )
        )
        self.assertTrue(  # active/diagnostic collision
            notes((("9", "la"),), (("9", "la", "hibernated", "requested"),))
        )
        # Redmine #13967 R13-F2: an empty active issue, or an empty diagnostic issue/lane (a
        # real unbound lane), is an unattributable identity -> flagged.
        self.assertTrue(notes((("", "la"),), ()))  # empty active issue
        self.assertTrue(notes((("9", "la"),), (("", "unbound-lane", "hibernated", "requested"),)))
        self.assertTrue(notes((("9", "la"),), (("8", "", "retired", "requested"),)))
        # Redmine #13967 R14-F2: an out-of-vocabulary lane_disposition or process_release is
        # flagged, symmetric with the from-glance _NON_ACTIVE_DISPOSITIONS / RELEASE_STATES.
        self.assertTrue(notes((("9", "la"),), (("8", "lb", "bogus", "requested"),)))
        self.assertTrue(notes((("9", "la"),), (("8", "lb", "hibernated", "bogus_release"),)))
        self.assertTrue(notes((("9", "la"),), (("8", "lb", "active", "requested"),)))
        self.assertEqual(notes((("9", "la"),), (("8", "lb", "retired", "released"),)), [])

        # Redmine #13967 R15-F1: the release rows are produced from the SAME normalized pass, so
        # a whitespace-padded requested row validates clean AND surfaces as a normalized release
        # row (no drift). A released/not_requested row is valid but yields NO release row; an
        # invalid-vocab row yields a note and no release row.
        n, rel = _live_diagnostic_report(
            (("9", "la"),), ((" 7 ", " lane ", " hibernated ", " requested "),)
        )
        self.assertEqual(n, [])
        self.assertEqual(rel, [("7", "lane")])  # normalized tokens, not raw
        self.assertEqual(releases((("9", "la"),), (("8", "lb", "hibernated", "partial"),)), [("8", "lb")])
        self.assertEqual(releases((("9", "la"),), (("8", "lb", "retired", "released"),)), [])
        self.assertEqual(releases((("9", "la"),), (("8", "lb", "bogus", "requested"),)), [])

    def test_live_path_identity_contradiction_holds_with_durable_facts(self):
        # Redmine #13967 R12-F1: with durable facts present (so the fold is NOT degraded on its
        # own), an active/diagnostic collision must still hold — it must NOT be silently merged
        # into a release_dogfood bucket and read releasable. A healthy distinct roster releases.
        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore

        mod = "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
        gss = mod + ".application.glance_snapshot_source"
        drain = mod + ".application.cli_workflow_drain"
        wrs = "mozyo_bridge.core.state.workflow_runtime_store"

        def run(roster, diag):
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "rt.json"
                # gate=progress -> the folded lane is `implementing` (durable facts present, not
                # degraded by itself), so only the identity contradiction can hold it.
                WorkflowRuntimeStore(path=p).append_events(
                    [{"event_id": "e", "issue": "9", "gate": "progress"}]
                )
                with mock.patch(gss + ".enumerate_active_lanes", return_value=(roster, None)), \
                        mock.patch(gss + ".enumerate_lifecycle_diagnostic", return_value=(diag, None)), \
                        mock.patch(drain + "._delivery_ledger", return_value=None), \
                        mock.patch(wrs + ".workflow_runtime_store_path", return_value=p):
                    return self._run(repo=td)

        # active (9, la) folds to implementing; a colliding diagnostic (9, la) requested must
        # NOT launder it into release_dogfood -> hold, durable-incomplete.
        payload = run((("9", "la"),), (("9", "la", "hibernated", "requested"),))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])
        # healthy distinct active + diagnostic -> releasable
        payload = run((("9", "la"),), (("8", "lb", "retired", "released"),))
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)
        self.assertTrue(payload["durable_complete"])
        # Redmine #13967 R14-F2: a diagnostic row with an out-of-vocabulary disposition or
        # release must hold (never launder into a healthy release row) -- the lifecycle store
        # has no CHECK constraint, so a corrupted/legacy row is readable and the reader guards it.
        payload = run((("9", "la"),), (("8", "lb", "bogus", "requested"),))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])
        payload = run((("9", "la"),), (("8", "lb", "hibernated", "bogus_release"),))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])
        # a fully-valid non-active requested row still folds into a release_dogfood lane
        payload = run((("9", "la"),), (("8", "lb", "hibernated", "requested"),))
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)
        self.assertEqual(payload["release_dogfood_pending"], 1)
        # Redmine #13967 R15-F1: a whitespace-padded requested row validates clean AND still
        # surfaces as a release_dogfood lane (the validator and the release projection share one
        # normalized pass) -- it must NOT silently vanish.
        payload = run((("9", "la"),), ((" 8 ", " lb ", " hibernated ", " requested "),))
        self.assertEqual(payload["release_dogfood_pending"], 1)
        self.assertEqual(payload["process_retention"], PROCESS_RELEASABLE)

    def test_live_path_holds_on_real_empty_issue_diagnostic(self):
        # Redmine #13967 R13-F2: a REAL unbound lane (LaneLifecycleStore.declare_active with
        # issue_id="") surfaces in the real enumerate_lifecycle_diagnostic with an EMPTY issue.
        # The default-live drain path must hold on it (never launder it into a healthy
        # release_dogfood row), symmetric with the from-glance reader's required-identity
        # contract. Part 1 captures the genuine producer tuple; part 2 drives it through
        # _lanes_live with durable facts present so only the empty identity can hold it.
        import os
        from unittest.mock import patch

        from mozyo_bridge.core.state.lane_lifecycle import (
            DISPOSITION_ACTIVE,
            DISPOSITION_HIBERNATED,
            DecisionPointer,
            LaneLifecycleKey,
            LaneLifecycleStore,
        )
        from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.glance_snapshot_source import (
            enumerate_lifecycle_diagnostic,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (
            herdr_session_start as hss,
        )

        ws = "wProj"
        dec = lambda j: DecisionPointer(source="redmine", issue_id="13583", journal_id=j)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = LaneLifecycleStore(home=home)
            key = LaneLifecycleKey(ws, "unbound-lane")
            # A legitimate unbound lane: the lane owns no issue (issue_id=""), the decision is
            # a real anchor. Hibernate it so it lands on the (non-active) diagnostic roster.
            self.assertTrue(store.declare_active(key, decision=dec("1"), issue_id="").applied)
            self.assertTrue(
                store.transition_disposition(
                    key,
                    expected_disposition=DISPOSITION_ACTIVE,
                    expected_revision=1,
                    target=DISPOSITION_HIBERNATED,
                    decision=dec("2"),
                ).applied
            )
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False), \
                    patch.object(hss, "herdr_workspace_segment", return_value=ws):
                real_diag, err = enumerate_lifecycle_diagnostic(Path("."))
            self.assertIsNone(err)
            # The real producer emitted an EMPTY-issue diagnostic row.
            self.assertTrue(any(str(d[0] or "") == "" for d in real_diag), real_diag)

        # Part 2: drive the genuine real_diag through _lanes_live with a healthy active lane
        # whose durable facts ARE present (so it is not degraded on its own) -> must still hold.
        mod = "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"
        gss = mod + ".application.glance_snapshot_source"
        drain = mod + ".application.cli_workflow_drain"
        wrs = "mozyo_bridge.core.state.workflow_runtime_store"
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rt.json"
            WorkflowRuntimeStore(path=p).append_events(
                [{"event_id": "e", "issue": "9", "gate": "progress"}]
            )
            with mock.patch(gss + ".enumerate_active_lanes", return_value=((("9", "la"),), None)), \
                    mock.patch(gss + ".enumerate_lifecycle_diagnostic", return_value=(real_diag, None)), \
                    mock.patch(drain + "._delivery_ledger", return_value=None), \
                    mock.patch(wrs + ".workflow_runtime_store_path", return_value=p):
                payload = self._run(repo=td)
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])

    def test_present_null_is_malformed_not_absent(self):
        # Redmine #13967 R5-F1: an explicit JSON null is a present non-string value (malformed
        # -> hold), NOT the same as an absent key (which takes the default). An absent field
        # still yields the default and stays evaluable.
        malformed = [
            {"lanes": [{"issue": "1", "state_class": "idle", "lane": None, "release_pending": True}]},
        ]
        for env in malformed:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "s.json"
                path.write_text(json.dumps(env), encoding="utf-8")
                payload = self._run(snapshot_json=str(path))
            self.assertEqual(payload["process_retention"], PROCESS_HOLD, env)
            self.assertFalse(payload["durable_complete"], env)
        glance_null = [
            {"rows": [{"issue_id": "1", "workflow_state": "idle", "lane": None}]},
            {"rows": [{"issue_id": "1", "workflow_state": "review_waiting", "next_owner": None}]},
            {"rows": [], "lifecycle_diagnostic": [{"process_release": None, "issue": "1", "lane": "l"}]},
        ]
        for env in glance_null:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "g.json"
                path.write_text(json.dumps(env), encoding="utf-8")
                payload = self._run(from_glance=str(path))
            self.assertEqual(payload["process_retention"], PROCESS_HOLD, env)
            self.assertFalse(payload["durable_complete"], env)
        # An absent optional lane is still evaluable (default), not forced to hold.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s2.json"
            path.write_text(
                json.dumps({"lanes": [{"issue": "1", "state_class": LANE_STATE_IMPLEMENTING}]}),
                encoding="utf-8",
            )
            payload = self._run(snapshot_json=str(path))
        self.assertTrue(payload["durable_complete"])

    def test_snapshot_type_confusion_fails_closed(self):
        cases = [
            {"lanes": 1},
            {"lanes": [{"issue": {"x": 1}, "state_class": "review_waiting"}]},
            {"lanes": [{"issue": "1", "state_class": ["review_waiting"]}]},
        ]
        for env in cases:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "s.json"
                path.write_text(json.dumps(env), encoding="utf-8")
                payload = self._run(snapshot_json=str(path))
            self.assertEqual(payload["process_retention"], PROCESS_HOLD, env)
            self.assertFalse(payload["durable_complete"], env)

    def test_from_glance_identity_missing_release_row_holds(self):
        # R3-F2: a release-pending lifecycle_diagnostic row with no durable identity must not
        # become a phantom release_dogfood lane that reads releasable — it fails closed to
        # durable-incomplete (hold).
        glance_env = {
            "rows": [],
            "lifecycle_diagnostic": [{"process_release": "requested"}],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "glance.json"
            path.write_text(json.dumps(glance_env), encoding="utf-8")
            payload = self._run(from_glance=str(path))
        self.assertEqual(payload["process_retention"], PROCESS_HOLD)
        self.assertFalse(payload["durable_complete"])
        self.assertEqual(payload["release_dogfood_pending"], 0)

    def test_merge_release_pending_by_identity(self):
        # Redmine #13967 F3: the identity-merge helper is where the "one lane, one bucket"
        # invariant lives. A canonical glance envelope cannot place the same lane in both the
        # active roster and the (non-active-only) lifecycle diagnostic — R7-F2 now rejects an
        # `active` diagnostic disposition — so the merge is exercised at the helper directly
        # (composed/malformed defensive hardening): a release-pending identity that matches an
        # active lane sets its flag on the SAME row (counted once), and its coordinator-
        # blocking base bucket still wins over release_dogfood.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_drain import (
            _merge_release_pending,
        )

        active = [DrainLane(issue="300", lane="lane-300", state_class=LANE_STATE_REVIEW_WAITING)]
        merged = _merge_release_pending(active, [("300", "lane-300")])
        self.assertEqual(len(merged), 1)  # merged, not double-counted
        self.assertTrue(merged[0].release_pending)
        projection = project_drain_queue(merged)
        self.assertEqual(projection.release_dogfood_pending, 0)  # blocking base bucket wins
        review = projection.bucket(BUCKET_REVIEW)
        self.assertEqual(review.total, 1)
        self.assertEqual(projection.process_retention, PROCESS_HOLD)
        # A release identity with no matching active lane appends a fresh release_dogfood lane.
        merged2 = _merge_release_pending(active, [("999", "lane-999")])
        self.assertEqual(len(merged2), 2)


if __name__ == "__main__":
    unittest.main()
