"""Read-only lifecycle binding for hibernate candidates, end-to-end (Redmine #14219, tranche T1).

Drives ``bind_active_lifecycle_anchor`` against a REAL lane lifecycle store seeded through the
write API, proving:

- a single active lane binds to its exact ``(repo_workspace_id, lane_id, lane_generation,
  revision)`` re-read from disk;
- a hibernated lane (no active record) folds to ``absent``, never a stale bind;
- two active lanes for one issue fold to ``ambiguous`` (fail-closed, matching the glance
  ``len(recs) != 1 -> drop`` guard);
- an absent store folds to ``absent`` (the ``()`` no-rows read), and a malformed store to
  ``unreadable`` (the ``None`` fail-closed downgrade), never a created store — the read is
  ``mode=ro`` and non-creating.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mozyo_bridge.core.state.lane_lifecycle import (
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    load_lane_lifecycle_readonly,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
)
from mozyo_bridge.core.state.lane_lifecycle_schema import lane_lifecycle_path
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_candidate_source import (  # noqa: E501
    bind_active_lifecycle_anchor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    hibernate_candidate as hc,
)

ISSUE = "14219"
WS = "ws-alpha"
LANE = "lane-alpha"


def _decision(journal: str = "85466") -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=journal)


def _selected(**over) -> hc.SelectedLane:
    base = dict(
        issue_id=ISSUE, repo_workspace_id=WS, lane_id=LANE, lane_generation=1, revision=1
    )
    base.update(over)
    return hc.SelectedLane(**base)


class HibernateCandidateBindingTests(unittest.TestCase):
    def test_single_active_lane_binds_the_exact_anchor(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = LaneLifecycleStore(home=home)
            store.declare_active(
                LaneLifecycleKey(WS, LANE),
                decision=_decision(),
                issue_id=ISSUE,
                worktree_identity="wt_14219alpha",
            )
            got = bind_active_lifecycle_anchor(_selected(), home=home)
            self.assertIsInstance(got, hc.LifecycleAnchor)
            self.assertEqual(got.repo_workspace_id, WS)
            self.assertEqual(got.lane_id, LANE)
            self.assertEqual(got.lane_generation, 1)
            self.assertEqual(got.revision, 1)
            self.assertEqual(got.issue_id, ISSUE)

    def test_hibernated_lane_is_absent_not_a_stale_bind(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = LaneLifecycleStore(home=home)
            store.declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            rec = store.get(LaneLifecycleKey(WS, LANE))
            store.transition_disposition(
                LaneLifecycleKey(WS, LANE),
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=rec.revision,
                target=DISPOSITION_HIBERNATED,
                decision=_decision("85467"),
            )
            got = bind_active_lifecycle_anchor(_selected(), home=home)
            self.assertIsInstance(got, hc.HibernateNonCandidate)
            self.assertEqual(got.reason, hc.NON_CANDIDATE_LIFECYCLE_ABSENT)

    def test_a_single_active_lane_that_is_not_the_selected_one_is_rejected(self):
        # R1-F1 end-to-end: the store's only active row for the issue is a DIFFERENT lane than the
        # enumeration selected. Deriving "the active lane for the issue" would wrongly accept it.
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = LaneLifecycleStore(home=home)
            store.declare_active(
                LaneLifecycleKey(WS, "lane-actually-present"), decision=_decision(), issue_id=ISSUE
            )
            got = bind_active_lifecycle_anchor(_selected(lane_id="lane-enumeration-chose"), home=home)
            self.assertIsInstance(got, hc.HibernateNonCandidate)
            self.assertEqual(got.reason, hc.NON_CANDIDATE_LANE_IDENTITY_MISMATCH)

    def test_two_active_lanes_for_one_issue_are_ambiguous(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = LaneLifecycleStore(home=home)
            store.declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            store.declare_active(
                LaneLifecycleKey("ws-beta", "lane-beta"), decision=_decision(), issue_id=ISSUE
            )
            got = bind_active_lifecycle_anchor(_selected(), home=home)
            self.assertIsInstance(got, hc.HibernateNonCandidate)
            self.assertEqual(got.reason, hc.NON_CANDIDATE_LANE_AMBIGUOUS)

    def test_absent_store_is_absent_and_creates_nothing(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            got = bind_active_lifecycle_anchor(_selected(), home=home)
            self.assertIsInstance(got, hc.HibernateNonCandidate)
            self.assertEqual(got.reason, hc.NON_CANDIDATE_LIFECYCLE_ABSENT)
            # the readonly read created nothing.
            self.assertFalse(lane_lifecycle_path(home).exists())

    def test_malformed_store_is_unreadable_fail_closed(self):
        with TemporaryDirectory() as raw:
            home = Path(raw)
            store = LaneLifecycleStore(home=home)
            store.declare_active(
                LaneLifecycleKey(WS, LANE), decision=_decision(), issue_id=ISSUE
            )
            path = lane_lifecycle_path(home)
            self.assertTrue(path.exists())
            path.write_bytes(b"this is not a sqlite database")
            # sanity: the raw readonly read itself fails closed to None on a corrupt store.
            self.assertIsNone(load_lane_lifecycle_readonly(home=home))
            got = bind_active_lifecycle_anchor(_selected(), home=home)
            self.assertIsInstance(got, hc.HibernateNonCandidate)
            self.assertEqual(got.reason, hc.NON_CANDIDATE_LIFECYCLE_UNREADABLE)


if __name__ == "__main__":
    unittest.main()
