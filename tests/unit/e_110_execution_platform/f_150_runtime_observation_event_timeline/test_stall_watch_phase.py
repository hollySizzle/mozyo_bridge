"""Stall-watch discovery join and cadence tests (Redmine #15855).

Discovery is a join, not a scan (j#110121-4). Each of the four filters is checked for the
distinct reason it exists, and the drop *counts* are checked too — the blind spot filter 4
creates is deliberate, so it has to be visible rather than silent.

Cadence is the watcher's own watermark (j#110121-2), separate from the OS tick and from the
supervisor's provider watermark. The two "run anyway" cases (never ran / unparseable
watermark) are checked as deliberate, because their opposite is a watcher that is silently
off forever.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.backend_neutral_resolver import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_phase import (  # noqa: E501
    CADENCE_DISABLED,
    CADENCE_DUE,
    CADENCE_NEVER_RAN,
    CADENCE_UNREADABLE_WATERMARK,
    CADENCE_WAITING,
    DROP_FOREIGN_WORKSPACE,
    DROP_NO_GENERATION,
    DROP_NO_ISSUE_ANCHOR,
    DROP_NO_LOCATOR,
    DROP_OUT_OF_SCOPE,
    discover_watch_units,
    stall_watch_due,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_watch_policy import (  # noqa: E501
    StallWatchPolicy,
)

WS = "wsA"
T0 = datetime(2026, 8, 22, 9, 0, 0, tzinfo=timezone.utc)


def _row(lane="lane_a", role="claude", workspace=WS, locator="w1V:pK"):
    return {"name": encode_assigned_name(workspace, role, lane), "pane_id": locator}


def _discover(rows, *, policy=None, generation="g1", issue="15855", **kwargs):
    policy = policy or StallWatchPolicy.from_record({"all_managed_lanes": True})
    return discover_watch_units(
        rows,
        workspace_id=WS,
        policy=policy,
        generation_for=kwargs.pop("generation_for", lambda lane: generation),
        issue_for=kwargs.pop("issue_for", lambda lane: issue),
        **kwargs,
    )


class ScopeFilterTest(unittest.TestCase):
    def test_a_disabled_policy_watches_nothing_and_calls_no_resolver(self) -> None:
        # "Watches nothing" must be true of the I/O, not only of the output: a host with no
        # stall_watch block performs no lane lookups at all.
        calls = []

        def _boom(lane):
            calls.append(lane)
            return "g1"

        discovery = discover_watch_units(
            [_row()],
            workspace_id=WS,
            policy=StallWatchPolicy.default(),
            generation_for=_boom,
            issue_for=_boom,
        )
        self.assertEqual(discovery.watched, 0)
        self.assertEqual(discovery.candidates, 0)
        self.assertEqual(calls, [])

    def test_an_unmanaged_row_is_dropped_before_any_filter(self) -> None:
        # Delegated to herdr_inventory rather than re-derived: a row this repo would not
        # route to is a row this watcher does not read either.
        discovery = _discover([{"name": "not-an-mzb1-name", "pane_id": "w9:p1"}])
        self.assertEqual(discovery.candidates, 0)
        self.assertEqual(discovery.watched, 0)

    def test_a_foreign_workspace_row_is_dropped(self) -> None:
        discovery = _discover([_row(workspace="wsB")])
        self.assertEqual(discovery.dropped, {DROP_FOREIGN_WORKSPACE: 1})
        self.assertEqual(discovery.watched, 0)

    def test_a_lane_outside_the_declared_scope_is_dropped(self) -> None:
        policy = StallWatchPolicy.from_record({"lanes": ["lane_a"]})
        discovery = _discover([_row(lane="lane_a"), _row(lane="lane_z")], policy=policy)
        self.assertEqual(discovery.watched, 1)
        self.assertEqual(discovery.dropped, {DROP_OUT_OF_SCOPE: 1})

    def test_a_role_outside_the_declared_scope_is_dropped(self) -> None:
        policy = StallWatchPolicy.from_record(
            {"lanes": ["lane_a"], "roles": ["claude"]}
        )
        discovery = _discover(
            [_row(role="claude"), _row(role="codex")], policy=policy
        )
        self.assertEqual(discovery.watched, 1)
        self.assertEqual(discovery.units[0].identity.role, "claude")


class AnchorFilterTest(unittest.TestCase):
    def test_an_unresolved_generation_drops_the_unit(self) -> None:
        discovery = _discover([_row()], generation_for=lambda lane: "")
        self.assertEqual(discovery.dropped, {DROP_NO_GENERATION: 1})

    def test_a_raising_generation_resolver_drops_the_unit(self) -> None:
        def _boom(lane):
            raise RuntimeError("lifecycle store unavailable")

        self.assertEqual(_discover([_row()], generation_for=_boom).watched, 0)

    def test_an_unresolved_issue_anchor_drops_the_unit(self) -> None:
        # The deliberate blind spot: guessing which issue a stall belongs to would write a
        # coordinator-facing record onto the wrong one.
        discovery = _discover([_row()], issue_for=lambda lane: "")
        self.assertEqual(discovery.dropped, {DROP_NO_ISSUE_ANCHOR: 1})

    def test_no_resolvers_wired_watches_nothing(self) -> None:
        discovery = discover_watch_units(
            [_row()],
            workspace_id=WS,
            policy=StallWatchPolicy.from_record({"all_managed_lanes": True}),
        )
        self.assertEqual(discovery.watched, 0)

    def test_a_slot_with_no_live_locator_is_dropped(self) -> None:
        # There is no screen to read; the routing layer refuses a blank target for sends
        # and reading is the same boundary.
        discovery = _discover([_row(locator="")])
        self.assertEqual(discovery.dropped, {DROP_NO_LOCATOR: 1})

    def test_the_blind_spot_is_counted_not_hidden(self) -> None:
        discovery = _discover(
            [_row(lane="lane_a"), _row(lane="lane_b", locator=""), _row(workspace="wsB")],
            issue_for=lambda lane: "15855" if lane == "lane_a" else "",
        )
        self.assertEqual(discovery.watched, 1)
        # The foreign row is somebody else's business; the rest are units this watcher
        # genuinely cannot escalate about.
        self.assertEqual(discovery.out_of_reach, 1)
        self.assertEqual(discovery.telemetry()["candidates"], 3)


class AdmittedUnitTest(unittest.TestCase):
    def test_an_admitted_unit_carries_the_joined_identity(self) -> None:
        (unit,) = _discover([_row(lane="lane_a", role="claude")], generation="g7").units
        self.assertEqual(unit.identity.workspace_id, WS)
        self.assertEqual(unit.identity.lane_id, "lane_a")
        self.assertEqual(unit.identity.role, "claude")
        self.assertEqual(unit.identity.generation, "g7")
        self.assertEqual(unit.identity.target, "w1V:pK")
        self.assertEqual(unit.issue, "15855")
        self.assertEqual(unit.locator, "w1V:pK")

    def test_the_role_is_the_provider_fallback(self) -> None:
        (unit,) = _discover([_row(role="codex")]).units
        self.assertEqual(unit.provider_id, "codex")

    def test_an_explicit_provider_resolver_wins(self) -> None:
        (unit,) = _discover([_row()], provider_for=lambda lane: "claude-opus").units
        self.assertEqual(unit.provider_id, "claude-opus")

    def test_a_raising_provider_resolver_leaves_the_unit_observable(self) -> None:
        def _boom(lane):
            raise RuntimeError("no profile")

        # An unprofiled unit still falls through to the patient indeterminate class rather
        # than being dropped: not knowing the provider is not a reason to stop watching.
        (unit,) = _discover([_row()], provider_for=_boom).units
        self.assertEqual(unit.provider_id, "claude")


class CadenceTest(unittest.TestCase):
    def _policy(self, cadence=300):
        return StallWatchPolicy.from_record(
            {"all_managed_lanes": True, "cadence_seconds": cadence}
        )

    def test_a_disabled_policy_is_never_due(self) -> None:
        verdict = stall_watch_due(
            policy=StallWatchPolicy.default(), last_pass_at="", now=T0
        )
        self.assertFalse(verdict.due)
        self.assertEqual(verdict.reason, CADENCE_DISABLED)

    def test_a_never_run_watcher_is_due_immediately(self) -> None:
        # The first tick after an operator configures the watcher should observe, not wait
        # out a full cadence for a cockpit that may already be stuck.
        verdict = stall_watch_due(policy=self._policy(), last_pass_at="", now=T0)
        self.assertTrue(verdict.due)
        self.assertEqual(verdict.reason, CADENCE_NEVER_RAN)

    def test_within_the_cadence_it_is_not_due(self) -> None:
        verdict = stall_watch_due(
            policy=self._policy(300),
            last_pass_at=T0.isoformat(),
            now=T0 + timedelta(seconds=299),
        )
        self.assertFalse(verdict.due)
        self.assertEqual(verdict.reason, CADENCE_WAITING)
        self.assertEqual(
            verdict.next_due_at, (T0 + timedelta(seconds=300)).isoformat(timespec="seconds")
        )

    def test_at_the_cadence_boundary_it_is_due(self) -> None:
        verdict = stall_watch_due(
            policy=self._policy(300),
            last_pass_at=T0.isoformat(),
            now=T0 + timedelta(seconds=300),
        )
        self.assertTrue(verdict.due)
        self.assertEqual(verdict.reason, CADENCE_DUE)

    def test_an_unparseable_watermark_runs_rather_than_stays_off_forever(self) -> None:
        verdict = stall_watch_due(
            policy=self._policy(), last_pass_at="not-a-timestamp", now=T0
        )
        self.assertTrue(verdict.due)
        self.assertEqual(verdict.reason, CADENCE_UNREADABLE_WATERMARK)

    def test_a_naive_watermark_is_read_as_utc_not_rejected(self) -> None:
        verdict = stall_watch_due(
            policy=self._policy(300),
            last_pass_at="2026-08-22T09:00:00",
            now=T0 + timedelta(seconds=100),
        )
        self.assertFalse(verdict.due)

    def test_the_operator_cadence_is_honoured(self) -> None:
        verdict = stall_watch_due(
            policy=self._policy(900),
            last_pass_at=T0.isoformat(),
            now=T0 + timedelta(seconds=600),
        )
        self.assertFalse(verdict.due)
        self.assertEqual(verdict.cadence_seconds, 900)

    def test_next_due_is_labelled_as_a_threshold_not_a_schedule(self) -> None:
        # The phase only runs when the host tick runs, so the realized period is quantized
        # against a cadence that is not a multiple of the tick.
        payload = stall_watch_due(
            policy=self._policy(), last_pass_at=T0.isoformat(), now=T0
        ).telemetry()
        self.assertTrue(payload["next_due_is_a_threshold_not_a_schedule"])

    def test_the_cadence_is_not_the_os_tick(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
            DEFAULT_OS_TICK_INTERVAL_SECONDS,
        )
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_watch_policy import (  # noqa: E501
            DEFAULT_STALL_WATCH_CADENCE_SECONDS,
        )

        # j#110121-2: the OS tick stays where it is so the callback supervisor's local
        # cadence is not degraded; the ~5 minute period is this watcher's own watermark.
        self.assertEqual(DEFAULT_OS_TICK_INTERVAL_SECONDS, 180)
        self.assertEqual(DEFAULT_STALL_WATCH_CADENCE_SECONDS, 300)
        self.assertGreater(
            DEFAULT_STALL_WATCH_CADENCE_SECONDS, DEFAULT_OS_TICK_INTERVAL_SECONDS
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
