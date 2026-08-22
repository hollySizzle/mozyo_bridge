"""Pure classifier for Version-scoped drain tracking (Redmine #15844).

Isolated: the subject is ``domain/version_lane_tracking`` alone — no Redmine, no store,
no filesystem. The facts records are built directly, and the ``LaneBucketIssue`` join is
exercised through a minimal stand-in that carries the same published attributes.
"""

import unittest
from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.version_lane_tracking import (  # noqa: E501
    ATTENTION_DISPOSITIONS,
    DISPOSITION_DRAIN_OWED,
    DISPOSITION_IN_FLIGHT,
    DISPOSITION_LANE_TERMINAL_ISSUE_OPEN,
    DISPOSITION_SETTLED,
    DISPOSITION_UMBRELLA_OPEN,
    DISPOSITION_UNDISPATCHED,
    DISPOSITION_UNKNOWN_ISSUE_STATE,
    TERMINAL_LANE_DISPOSITIONS,
    VERSION_ISSUE_DISPOSITIONS,
    TrackedLane,
    UnscopedLane,
    VersionIssueFacts,
    build_version_tracking,
    classify_version_issue,
    is_terminal_lane_disposition,
    join_version_issues,
    render_version_tracking_text,
)


@dataclass(frozen=True)
class _BucketIssue:
    """The published ``LaneBucketIssue`` attributes the join reads (#12919)."""

    issue_id: str
    is_closed: bool = False
    is_leaf: bool = False
    tracker: Optional[str] = None
    status_name: Optional[str] = "未着手"
    parent_id: Optional[str] = None


def _facts(**kwargs) -> VersionIssueFacts:
    base = {
        "issue_id": "1",
        "is_closed": False,
        "is_leaf": True,
        "status_name": "未着手",
        "lanes": (),
    }
    base.update(kwargs)
    return VersionIssueFacts(**base)


ACTIVE = TrackedLane(lane_id="issue_1_x", lane_disposition="active")
HIBERNATED = TrackedLane(lane_id="issue_1_h", lane_disposition="hibernated")
RETIRED = TrackedLane(lane_id="issue_1_r", lane_disposition="retired")
SUPERSEDED = TrackedLane(lane_id="issue_1_s", lane_disposition="superseded")


class TerminalSetTest(unittest.TestCase):
    def test_terminal_set_is_retired_and_superseded_only(self):
        self.assertEqual(TERMINAL_LANE_DISPOSITIONS, ("retired", "superseded"))

    def test_hibernated_is_not_terminal(self):
        # A hibernated lane released its process but still owns its issue, so the drain
        # is still owed. Folding it into the terminal set would make every parked lane
        # silently disappear from tracking.
        self.assertFalse(is_terminal_lane_disposition("hibernated"))
        self.assertFalse(HIBERNATED.is_terminal)

    def test_unknown_disposition_is_not_terminal(self):
        # Fail-safe direction: an unreadable disposition keeps the lane in the owed
        # population rather than retiring it by default.
        self.assertFalse(is_terminal_lane_disposition("something_new"))
        self.assertFalse(is_terminal_lane_disposition(""))


class ClassifyVersionIssueTest(unittest.TestCase):
    def test_closed_issue_with_active_lane_is_drain_owed(self):
        """THE #15789 shape: work landed, issue closed, lane never terminalized."""
        tracking = classify_version_issue(
            _facts(is_closed=True, status_name="クローズ", lanes=(ACTIVE,))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_DRAIN_OWED)
        self.assertEqual(tracking.reason, "issue_closed_lane_not_terminal")

    def test_drain_owed_names_the_lane_and_the_existing_rail_entry_point(self):
        # It names the lane and hands off; it must not name a specific recovery rail —
        # `reboot-audit` owns that judgement on a four-authority join (#14499 / #15841).
        tracking = classify_version_issue(
            _facts(is_closed=True, status_name="クローズ", lanes=(ACTIVE,))
        )
        self.assertEqual(
            tracking.next_steps,
            ("mozyo-bridge sublane reboot-audit --lane-label issue_1_x",),
        )

    def test_closed_issue_with_hibernated_lane_is_also_drain_owed(self):
        tracking = classify_version_issue(
            _facts(is_closed=True, status_name="クローズ", lanes=(HIBERNATED,))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_DRAIN_OWED)

    def test_closed_issue_with_mixed_lanes_reports_only_the_nonterminal_one(self):
        tracking = classify_version_issue(
            _facts(is_closed=True, status_name="クローズ", lanes=(RETIRED, ACTIVE))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_DRAIN_OWED)
        self.assertEqual(len(tracking.next_steps), 1)
        self.assertIn("issue_1_x", tracking.next_steps[0])

    def test_open_issue_with_active_lane_is_in_flight(self):
        tracking = classify_version_issue(
            _facts(is_closed=False, status_name="着手中", lanes=(ACTIVE,))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_IN_FLIGHT)
        self.assertEqual(tracking.next_steps, ())

    def test_closed_issue_with_only_terminal_lanes_is_settled(self):
        tracking = classify_version_issue(
            _facts(is_closed=True, status_name="クローズ", lanes=(RETIRED, SUPERSEDED))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_SETTLED)

    def test_closed_issue_with_no_lane_is_settled(self):
        tracking = classify_version_issue(_facts(is_closed=True, status_name="クローズ"))
        self.assertEqual(tracking.disposition, DISPOSITION_SETTLED)

    def test_open_issue_whose_lanes_all_terminalized_is_surfaced(self):
        # The spine calls a close-ready issue left at 着手中 a durable-state
        # inconsistency, not harmless bookkeeping — so it is a finding, not `settled`.
        tracking = classify_version_issue(
            _facts(is_closed=False, status_name="着手中", lanes=(RETIRED,))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_LANE_TERMINAL_ISSUE_OPEN)

    def test_open_nonleaf_without_lane_is_umbrella_not_a_finding(self):
        tracking = classify_version_issue(_facts(is_closed=False, is_leaf=False))
        self.assertEqual(tracking.disposition, DISPOSITION_UMBRELLA_OPEN)
        self.assertFalse(tracking.needs_attention)

    def test_open_leaf_without_lane_is_undispatched(self):
        tracking = classify_version_issue(_facts(is_closed=False, is_leaf=True))
        self.assertEqual(tracking.disposition, DISPOSITION_UNDISPATCHED)


class UnreadableIssueStateTest(unittest.TestCase):
    def test_missing_status_name_is_unknown_not_open(self):
        """The normalizer defaults ``is_closed`` to False, so False alone proves nothing.

        ``_lane_bucket_issue_from_mapping`` reads ``bool(status.get("is_closed", False))``.
        An issue whose status object could not be read therefore arrives byte-identical
        to a genuinely open one. Reading ``status_name`` is what separates them; without
        it an unread issue joins the in-flight population silently.
        """
        tracking = classify_version_issue(_facts(is_closed=False, status_name=None))
        self.assertEqual(tracking.disposition, DISPOSITION_UNKNOWN_ISSUE_STATE)
        self.assertEqual(tracking.reason, "issue_status_unreadable")

    def test_blank_status_name_is_unknown(self):
        tracking = classify_version_issue(_facts(status_name="   "))
        self.assertEqual(tracking.disposition, DISPOSITION_UNKNOWN_ISSUE_STATE)

    def test_unreadable_state_wins_over_every_lane_shape(self):
        # Rule 1 is first for a reason: an unread issue must never be reported as
        # settled, which would pass off a finding about the READ as a finding about the
        # Version.
        for lanes in ((), (ACTIVE,), (RETIRED,), (RETIRED, ACTIVE)):
            with self.subTest(lanes=lanes):
                tracking = classify_version_issue(
                    _facts(status_name=None, is_closed=True, lanes=lanes)
                )
                self.assertEqual(
                    tracking.disposition, DISPOSITION_UNKNOWN_ISSUE_STATE
                )


class DecisionTableTotalityTest(unittest.TestCase):
    """The table is total and every rule is reachable (design spec `## 3.1`)."""

    def _product(self):
        for readable in (True, False):
            for closed in (True, False):
                for lanes in ((), (ACTIVE,), (RETIRED,), (RETIRED, ACTIVE)):
                    for leaf in (True, False):
                        yield _facts(
                            is_closed=closed,
                            is_leaf=leaf,
                            status_name="着手中" if readable else None,
                            lanes=lanes,
                        )

    def test_every_axis_combination_yields_a_declared_disposition(self):
        for facts in self._product():
            with self.subTest(
                readable=facts.issue_state_readable,
                closed=facts.is_closed,
                lanes=[lane.lane_id for lane in facts.lanes],
                leaf=facts.is_leaf,
            ):
                self.assertIn(
                    classify_version_issue(facts).disposition,
                    VERSION_ISSUE_DISPOSITIONS,
                )

    def test_every_declared_disposition_is_reachable(self):
        """No dead token in the vocabulary — an unreachable one is a lie in the counts."""
        reached = {classify_version_issue(f).disposition for f in self._product()}
        self.assertEqual(reached, set(VERSION_ISSUE_DISPOSITIONS))


class UmbrellaLaneIntersectionTest(unittest.TestCase):
    """The one named intersection (spec `## 3.1`, ``role_precedence``).

    Measured 2026-08-22: #15631 is a non-leaf of Version #329 *and* owns the lane
    ``issue_15631_trial``. Umbrella-ness is only ever the discriminant for "should an
    open issue with no lane count as undispatched?", so a row that has lanes must not
    consult it.
    """

    def test_umbrella_holding_an_active_lane_is_in_flight_not_umbrella_open(self):
        tracking = classify_version_issue(
            _facts(is_closed=False, is_leaf=False, lanes=(ACTIVE,))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_IN_FLIGHT)

    def test_closed_umbrella_holding_an_active_lane_is_drain_owed(self):
        # The failure this ordering prevents: collapsing it to `umbrella_open` would
        # make a roll-up lane's left-behind state permanently invisible.
        tracking = classify_version_issue(
            _facts(
                is_closed=True, is_leaf=False, status_name="クローズ", lanes=(ACTIVE,)
            )
        )
        self.assertEqual(tracking.disposition, DISPOSITION_DRAIN_OWED)


class AttentionSetTest(unittest.TestCase):
    def test_attention_is_exactly_the_three_owed_classes(self):
        self.assertEqual(
            ATTENTION_DISPOSITIONS,
            (
                DISPOSITION_DRAIN_OWED,
                DISPOSITION_LANE_TERMINAL_ISSUE_OPEN,
                DISPOSITION_UNKNOWN_ISSUE_STATE,
            ),
        )

    def test_in_flight_and_undispatched_are_not_attention(self):
        # Work in progress is not a finding, and an undispatched leaf is the *dispatch*
        # question `workflow dispatch-plan` already owns.
        for facts in (
            _facts(lanes=(ACTIVE,)),
            _facts(is_leaf=True),
            _facts(is_closed=True, status_name="クローズ"),
        ):
            with self.subTest(disposition=classify_version_issue(facts).disposition):
                self.assertFalse(classify_version_issue(facts).needs_attention)


class SnapshotTest(unittest.TestCase):
    def _snapshot(self):
        return build_version_tracking(
            version_id="329",
            version_name="v2.2.0",
            issues=(
                _facts(issue_id="15842", is_closed=True, status_name="クローズ",
                       lanes=(TrackedLane("issue_15842_x", "active"),)),
                _facts(issue_id="15844", status_name="未着手",
                       lanes=(TrackedLane("issue_15844_x", "active"),)),
                _facts(issue_id="15841", is_closed=True, status_name="クローズ",
                       lanes=(TrackedLane("issue_15841_x", "retired"),)),
            ),
            unscoped_lanes=(
                UnscopedLane("issue_15110_x", "15110", "active"),
            ),
        )

    def test_counts_include_every_disposition_including_the_zeroes(self):
        # An absent key would let a reader infer a zero from silence — the same mistake
        # as reading an unread authority as an empty one.
        counts = self._snapshot().counts
        self.assertEqual(set(counts), set(VERSION_ISSUE_DISPOSITIONS))
        self.assertEqual(counts[DISPOSITION_DRAIN_OWED], 1)
        self.assertEqual(counts[DISPOSITION_IN_FLIGHT], 1)
        self.assertEqual(counts[DISPOSITION_SETTLED], 1)
        self.assertEqual(counts[DISPOSITION_UNDISPATCHED], 0)

    def test_attention_holds_only_the_owed_rows(self):
        attention = self._snapshot().attention
        self.assertEqual([row.issue_id for row in attention], ["15842"])

    def test_payload_emits_no_composite_readiness_verdict(self):
        """The roll-up is a count, not a button (ADR-0011: the Version's integration
        disposition is a decision the project coordinator owns, and Version close needs
        owner approval on top)."""
        payload = self._snapshot().as_payload()
        self.assertEqual(payload["state"], "tracked")
        for forbidden in ("integration_ready", "verdict", "ready", "readiness"):
            self.assertNotIn(forbidden, payload)

    def test_payload_carries_no_issue_text(self):
        # Output hygiene (#15843 `## 出力の hygiene`): tokens and identifiers only, so a
        # snapshot can be pasted into a durable journal.
        payload = self._snapshot().as_payload()
        self._assert_no_key(payload, "subject")
        self._assert_no_key(payload, "description")

    def _assert_no_key(self, node, key):
        if isinstance(node, dict):
            self.assertNotIn(key, node)
            for value in node.values():
                self._assert_no_key(value, key)
        elif isinstance(node, list):
            for value in node:
                self._assert_no_key(value, key)

    def test_unscoped_lanes_survive_into_the_payload(self):
        payload = self._snapshot().as_payload()
        self.assertEqual(payload["unscoped_lane_count"], 1)
        self.assertEqual(payload["unscoped_lanes"][0]["issue_id"], "15110")


class RenderTest(unittest.TestCase):
    def test_unscoped_section_is_rendered_even_when_empty(self):
        """Scoping to a Version creates a fresh blind spot; the section renders
        unconditionally so "I ran version-track" never reads as "I saw every lane"."""
        text = render_version_tracking_text(
            build_version_tracking(
                version_id="1", version_name="v", issues=(_facts(),), unscoped_lanes=()
            )
        )
        self.assertIn("unscoped_lanes: 0", text)

    def test_render_names_the_rail_entry_point_for_an_owed_lane(self):
        text = render_version_tracking_text(
            build_version_tracking(
                version_id="1",
                version_name="v",
                issues=(
                    _facts(is_closed=True, status_name="クローズ", lanes=(ACTIVE,)),
                ),
            )
        )
        self.assertIn("sublane reboot-audit --lane-label issue_1_x", text)

    def test_render_reports_no_attention_explicitly(self):
        text = render_version_tracking_text(
            build_version_tracking(version_id="1", version_name="v", issues=(_facts(),))
        )
        self.assertIn("attention: none", text)


class JoinTest(unittest.TestCase):
    def test_join_reads_the_published_bucket_issue_attributes(self):
        facts = join_version_issues(
            (
                _BucketIssue("15842", is_closed=True, status_name="クローズ"),
                _BucketIssue("15844", is_leaf=True, tracker="開発"),
            ),
            {"15842": [ACTIVE]},
        )
        self.assertEqual([f.issue_id for f in facts], ["15842", "15844"])
        self.assertEqual(facts[0].lanes, (ACTIVE,))
        self.assertEqual(facts[1].lanes, ())
        self.assertEqual(facts[1].tracker, "開発")

    def test_join_drops_only_rows_with_no_identity(self):
        facts = join_version_issues((_BucketIssue(""), _BucketIssue("7")), {})
        self.assertEqual([f.issue_id for f in facts], ["7"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
