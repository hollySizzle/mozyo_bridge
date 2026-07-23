"""The supervisor's auto-hibernate mode leg (Redmine #14219 T2c step 1).

The `hibernate` run_once mode is a distinct early-return leg with the local_drain shape: the
same per-workspace lease fence (acquire -> try -> finally release), zero callback / outbox /
provider side effects, and the T2a bounded pass run through an injected leg seam. These tests
pin the choreography over a REAL lease store (temp sqlite), a booby-trapped provider source,
and a recording leg fake.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.callback_outbox import CallbackOutbox
from mozyo_bridge.core.state.supervisor_lease import SupervisorLeaseStore
from mozyo_bridge.core.state.workflow_runtime_store import WorkflowRuntimeStore
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.hibernate_actuation_leg import (  # noqa: E501
    HibernateAttempt,
    HibernatePassResult,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workspace_callback_supervisor import (  # noqa: E501
    SupervisedWorkspace,
    WorkspaceCallbackSupervisor,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    SKIP_HIBERNATE_LEG_ERROR,
    SKIP_HIBERNATE_UNWIRED,
    SKIP_LEASE_REFUSED,
    SUPERVISION_HIBERNATE,
    SUPERVISION_LOCAL_DRAIN,
    SUPERVISION_MODES,
)

CLOCK = "2026-07-24T00:00:00+00:00"


def _boom_source(ws):  # pragma: no cover - invoked only on a bug
    raise AssertionError("the hibernate leg must NEVER resolve a ticket-provider source")


class _RecordingLeg:
    """A fake hibernate leg: records (workspace, renew) calls, returns a canned pass result."""

    def __init__(self, result: HibernatePassResult | None = None, raises: bool = False) -> None:
        self.calls: list = []
        self.renew_results: list = []
        self.raises = raises
        self.result = result or HibernatePassResult(attempts=(), mutations=0, empty_pass=True)

    def __call__(self, ws, renew):
        # Renew from WITHIN the held lease — the T2a pass renews before each execute.
        self.renew_results.append(renew())
        self.calls.append(ws.workspace_id)
        if self.raises:
            raise RuntimeError("leg exploded")
        return self.result


class _HibernateModeHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        store_path = self.dir / "workflow-runtime.sqlite"
        self.store = WorkflowRuntimeStore(path=store_path)
        self.outbox = CallbackOutbox(path=store_path)
        self.lease_store = SupervisorLeaseStore(path=self.dir / "supervisor-lease.sqlite")
        self.ws = SupervisedWorkspace(
            workspace_id="wsA", canonical_path=str(self.dir / "repoA")
        )

    def _supervisor(self, *, leg=None, holder="superX", release_after=True, workspaces=None):
        return WorkspaceCallbackSupervisor(
            holder=holder,
            lease_store=self.lease_store,
            store=self.store,
            outbox=self.outbox,
            workspaces_fn=lambda: workspaces if workspaces is not None else [self.ws],
            roster_fn=lambda ws: ((), ""),
            redmine_source_fn=_boom_source,
            sender_fn=lambda ws: (lambda row: "delivered"),
            clock=lambda: CLOCK,
            release_after=release_after,
            hibernate_leg_fn=leg,
        )


class HibernateModeTest(_HibernateModeHarness):
    def test_the_mode_is_canonical_vocabulary(self) -> None:
        self.assertIn(SUPERVISION_HIBERNATE, SUPERVISION_MODES)

    def test_a_wired_leg_runs_under_the_held_lease_and_reports_the_pass(self) -> None:
        leg = _RecordingLeg(
            result=HibernatePassResult(
                attempts=(
                    HibernateAttempt(
                        issue="14219", lane="laneA", kind="hibernated", revision=7
                    ),
                ),
                mutations=1,
                empty_pass=False,
            )
        )
        report = self._supervisor(leg=leg).run_once(mode=SUPERVISION_HIBERNATE)
        self.assertEqual(report.mode, SUPERVISION_HIBERNATE)
        self.assertEqual(leg.calls, ["wsA"])
        # The renew callable renews THIS holder's freshly-acquired lease.
        self.assertEqual(leg.renew_results, [True])
        outcome = report.workspaces[0]
        self.assertTrue(outcome.lease_acquired)
        self.assertTrue(outcome.hibernate_ran)
        self.assertEqual(outcome.hibernate_mutations, 1)
        self.assertEqual(
            outcome.hibernate_attempts,
            (
                {
                    "issue": "14219",
                    "lane": "laneA",
                    "kind": "hibernated",
                    "reason": "",
                    "revision": 7,
                },
            ),
        )
        self.assertEqual(outcome.skipped_reason, "")
        # finally-release: a different supervisor can acquire immediately afterwards.
        self.assertTrue(
            self.lease_store.acquire("wsA", "other", now=CLOCK, ttl_seconds=60).acquired
        )

    def test_an_empty_pass_still_reports_that_it_ran(self) -> None:
        leg = _RecordingLeg()
        report = self._supervisor(leg=leg).run_once(mode=SUPERVISION_HIBERNATE)
        outcome = report.workspaces[0]
        self.assertTrue(outcome.hibernate_ran)
        self.assertEqual(outcome.hibernate_mutations, 0)
        self.assertEqual(outcome.hibernate_attempts, ())

    def test_an_unwired_leg_fails_closed_without_touching_the_lease(self) -> None:
        report = self._supervisor(leg=None).run_once(mode=SUPERVISION_HIBERNATE)
        outcome = report.workspaces[0]
        self.assertFalse(outcome.lease_acquired)
        self.assertFalse(outcome.hibernate_ran)
        self.assertEqual(outcome.skipped_reason, SKIP_HIBERNATE_UNWIRED)
        # Nothing was acquired: a foreign holder acquires with no takeover contention.
        self.assertTrue(
            self.lease_store.acquire("wsA", "other", now=CLOCK, ttl_seconds=60).acquired
        )

    def test_a_refused_lease_skips_the_leg_entirely(self) -> None:
        self.assertTrue(
            self.lease_store.acquire("wsA", "duplicate", now=CLOCK, ttl_seconds=600).acquired
        )
        leg = _RecordingLeg()
        report = self._supervisor(leg=leg).run_once(mode=SUPERVISION_HIBERNATE)
        outcome = report.workspaces[0]
        self.assertFalse(outcome.lease_acquired)
        self.assertEqual(outcome.skipped_reason, SKIP_LEASE_REFUSED)
        self.assertEqual(leg.calls, [])
        self.assertFalse(outcome.hibernate_ran)

    def test_a_raising_leg_releases_the_lease_and_the_sweep_continues(self) -> None:
        ws_b = SupervisedWorkspace(
            workspace_id="wsB", canonical_path=str(self.dir / "repoB")
        )
        calls: list = []

        def leg(ws, renew):
            calls.append(ws.workspace_id)
            if ws.workspace_id == "wsA":
                raise RuntimeError("boom")
            return HibernatePassResult(attempts=(), mutations=0, empty_pass=True)

        report = self._supervisor(leg=leg, workspaces=[self.ws, ws_b]).run_once(
            mode=SUPERVISION_HIBERNATE
        )
        self.assertEqual(calls, ["wsA", "wsB"])
        first, second = report.workspaces
        self.assertEqual(first.skipped_reason, SKIP_HIBERNATE_LEG_ERROR)
        self.assertFalse(first.hibernate_ran)
        self.assertTrue(second.hibernate_ran)
        # finally released even on the raise: a foreign holder can acquire wsA now.
        self.assertTrue(
            self.lease_store.acquire("wsA", "other", now=CLOCK, ttl_seconds=60).acquired
        )

    def test_release_after_false_keeps_the_lease_held(self) -> None:
        leg = _RecordingLeg()
        self._supervisor(leg=leg, holder="keeper", release_after=False).run_once(
            mode=SUPERVISION_HIBERNATE
        )
        refused = self.lease_store.acquire("wsA", "other", now=CLOCK, ttl_seconds=60)
        self.assertFalse(refused.acquired)

    def test_other_modes_never_invoke_the_leg(self) -> None:
        leg = _RecordingLeg()
        self._supervisor(leg=leg).run_once(mode=SUPERVISION_LOCAL_DRAIN)
        self.assertEqual(leg.calls, [])

    def test_the_pass_makes_zero_outbox_or_provider_side_effects(self) -> None:
        # The provider source is a booby trap (raises on resolve) and the leg is a pure fake:
        # a green run IS the zero-provider proof, and the outbox stays empty.
        leg = _RecordingLeg()
        self._supervisor(leg=leg).run_once(mode=SUPERVISION_HIBERNATE)
        self.assertEqual(self.outbox.read(), ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
