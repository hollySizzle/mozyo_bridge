"""Time-to-drain observability (Redmine #14219 T3 review j#87196 R2-F2(a); ruling j#87182 / j#87181).

``drain_ready_at`` START = the basis decision journal's provider ``created_on``; END = the injected
supervisor clock read at the attempt's terminal disposition. The report exposes a closed status enum
(``completed | pending | uncertain | unavailable``) + a nullable ``time_to_drain_ms`` (a completed
fresh actuation / terminal successful redrive only) + a nullable ``time_to_disposition_ms``. A
missing / malformed / clock-skewed pair is ``unavailable`` — never a guessed 0. Only DERIVED,
redaction-safe durations reach the payload; the raw provider timestamp is never surfaced.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    hibernate_actuation_leg as leg,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_actuation_leg import (  # noqa: E501
    HibernateAttempt,
    drain_metrics,
    stamp_drain_metrics,
    TTD_COMPLETED,
    TTD_PENDING,
    TTD_UNCERTAIN,
    TTD_UNAVAILABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
    hibernate_report_rollup as roll,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SUPERVISION_BOUNDED_RECONCILIATION,
    SupervisorReport,
    WorkspaceSupervisionOutcome,
)

A = "2026-07-24T00:00:00+00:00"
B5 = "2026-07-24T00:00:05+00:00"  # +5s


class DrainMetricsTest(unittest.TestCase):
    def test_completed_actuation_measures_latency(self) -> None:
        self.assertEqual(drain_metrics("actuated", A, B5), (TTD_COMPLETED, 5000, 5000))

    def test_terminal_redrive_is_completed(self) -> None:
        self.assertEqual(drain_metrics("redriven", A, B5), (TTD_COMPLETED, 5000, 5000))

    def test_blocked_is_pending_with_disposition_only(self) -> None:
        self.assertEqual(drain_metrics("blocked", A, B5), (TTD_PENDING, None, 5000))

    def test_partial_and_withheld_are_pending(self) -> None:
        for kind in ("actuated_release_incomplete", "redriven_success_withheld", "deferred"):
            with self.subTest(kind=kind):
                self.assertEqual(drain_metrics(kind, A, B5)[0], TTD_PENDING)

    def test_lease_lost_is_uncertain_no_drain_ms(self) -> None:
        status, drain_ms, disp = drain_metrics("lease_lost", A, B5)
        self.assertEqual(status, TTD_UNCERTAIN)
        self.assertIsNone(drain_ms)  # no trusted drain completion
        self.assertEqual(disp, 5000)

    def test_missing_start_makes_a_completed_actuation_unavailable(self) -> None:
        # A completed actuation with no start authority reports UNAVAILABLE, never a guessed 0.
        self.assertEqual(drain_metrics("actuated", "", B5), (TTD_UNAVAILABLE, None, None))

    def test_malformed_timestamp_is_unavailable(self) -> None:
        self.assertEqual(drain_metrics("actuated", "not-a-date", B5), (TTD_UNAVAILABLE, None, None))

    def test_clock_skew_end_before_start_is_unavailable(self) -> None:
        self.assertEqual(drain_metrics("actuated", B5, A), (TTD_UNAVAILABLE, None, None))

    def test_stamp_keeps_only_derived_values(self) -> None:
        stamped = stamp_drain_metrics(HibernateAttempt("1", "l", "actuated", released=1), A, B5)
        self.assertEqual(stamped.time_to_drain_status, TTD_COMPLETED)
        self.assertEqual(stamped.time_to_drain_ms, 5000)
        payload = stamped.as_payload()
        # Secret-safe: only the derived status + durations; NEVER the raw provider timestamps.
        self.assertNotIn("drain_ready_at", payload)
        self.assertNotIn("completed_at", payload)
        self.assertEqual(payload["time_to_drain_status"], TTD_COMPLETED)


def _ws(kind, *, status="", drain_ms=None, disp_ms=None, mutations=0):
    return WorkspaceSupervisionOutcome(
        workspace_id="wsA", lease_acquired=True, lease_reason="g",
        hibernate_ran=True, hibernate_mutations=mutations,
        hibernate_attempts=(
            {"issue": "1", "lane": "l", "kind": kind, "reason": "", "revision": 0, "released": 0,
             "time_to_drain_status": status, "time_to_drain_ms": drain_ms,
             "time_to_disposition_ms": disp_ms},
        ),
    )


class RollupTimeToDrainTest(unittest.TestCase):
    def _report(self, *ws):
        return SupervisorReport(mode=SUPERVISION_BOUNDED_RECONCILIATION, holder="h", workspaces=ws)

    def test_completed_surfaces_status_and_ms(self) -> None:
        r = self._report(_ws("actuated", status="completed", drain_ms=5000, disp_ms=5000, mutations=1))
        self.assertEqual(r.hibernate_time_to_drain_status, "completed")
        self.assertEqual(r.hibernate_time_to_drain_ms, 5000)
        self.assertEqual(r.hibernate_time_to_disposition_ms, 5000)

    def test_pending_has_null_drain_ms(self) -> None:
        r = self._report(_ws("blocked", status="pending", drain_ms=None, disp_ms=3000))
        self.assertEqual(r.hibernate_time_to_drain_status, "pending")
        self.assertIsNone(r.hibernate_time_to_drain_ms)
        self.assertEqual(r.hibernate_time_to_disposition_ms, 3000)

    def test_status_precedence_completed_over_pending(self) -> None:
        r = self._report(
            _ws("blocked", status="pending"),
            _ws("actuated", status="completed", drain_ms=1000, disp_ms=1000, mutations=1),
        )
        self.assertEqual(r.hibernate_time_to_drain_status, "completed")

    def test_uncertain_status_wins_over_pending(self) -> None:
        r = self._report(_ws("lease_lost", status="uncertain"), _ws("blocked", status="pending"))
        self.assertEqual(r.hibernate_time_to_drain_status, "uncertain")

    def test_payload_is_redaction_safe(self) -> None:
        payload = self._report(
            _ws("actuated", status="completed", drain_ms=5000, disp_ms=5000, mutations=1)
        ).hibernate_payload()
        self.assertEqual(payload["time_to_drain_status"], "completed")
        self.assertEqual(payload["time_to_drain_ms"], 5000)
        self.assertIn("time_to_disposition_ms", payload)
        # No raw timestamp / provider created_on leaks into the report payload.
        for value in payload.values():
            self.assertNotIn("2026-07-24T", str(value))


class TimeToDrainDriftGuardTest(unittest.TestCase):
    def test_rollup_status_literals_match_the_leg_constants(self) -> None:
        self.assertEqual(roll._TTD_COMPLETED, leg.TTD_COMPLETED)
        self.assertEqual(roll._TTD_PENDING, leg.TTD_PENDING)
        self.assertEqual(roll._TTD_UNCERTAIN, leg.TTD_UNCERTAIN)
        self.assertEqual(roll._TTD_UNAVAILABLE, leg.TTD_UNAVAILABLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
